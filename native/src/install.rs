use std::{
    collections::{BTreeMap, BTreeSet},
    env,
    ffi::OsString,
    fs,
    path::Path,
};

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::{
    application::{self, BINARY, Locations, MARKER, RECEIPT},
    files::{self, Guard, Snapshot},
};

const JOURNAL: &str = ".install-pending.json";
const MAX_RECEIPT: usize = 64 * 1024;
const MAX_EXECUTABLE: usize = 32 * 1024 * 1024;
const MAX_MODULE: usize = 1024 * 1024;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Bundle {
    schema_version: u8,
    version: String,
    platform: String,
    architecture: String,
    filename: String,
    sha256: String,
    git_commit: String,
    files: BTreeMap<String, String>,
}

#[derive(Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Receipt {
    marker: String,
    files: BTreeMap<String, String>,
    launcher_sha256: String,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Transaction {
    previous_receipt: Option<String>,
    previous: Option<Receipt>,
    next: Receipt,
    node_binding: String,
}

fn payload_path(path: &str) -> bool {
    const DOCUMENTS: &[&str] = &[
        "LICENSE",
        "AGENTS.md",
        "README.md",
        "docs/INSTALL.md",
        "docs/OPERATIONS.md",
        "docs/SUPPORT.md",
        "docs/INTEGRATIONS.md",
        "docs/FLEET.md",
    ];
    if DOCUMENTS.contains(&path)
        || matches!(
            path,
            "modelhub_kit/tools.json"
                | "modelhub_kit/default-config.json"
                | "modelhub_kit/windows-launcher.cmd"
        )
    {
        return true;
    }
    let Some(mut module) = path
        .strip_prefix("modelhub_kit/")
        .and_then(|name| name.strip_suffix(".py"))
    else {
        return false;
    };
    if let Some(child) = module.strip_prefix("native_helpers/") {
        module = child;
    }
    !module.is_empty()
        && module.len() <= 64
        && module
            .bytes()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_')
}

fn valid_receipt(receipt: &Receipt) -> bool {
    receipt.marker == MARKER
        && receipt.files.len() <= 64
        && !receipt.files.is_empty()
        && files::hex_digest(&receipt.launcher_sha256)
        && receipt.files.iter().all(|(name, hash)| {
            (matches!(name.as_str(), BINARY | "launch.py") || payload_path(name))
                && files::hex_digest(hash)
        })
}

fn load_bundle(source: &Path) -> Result<(Bundle, BTreeMap<String, Vec<u8>>)> {
    let index = files::snapshot(&source.join("native/bundle.json"), MAX_RECEIPT)?
        .context("bundle descriptor missing")?;
    let descriptor: Bundle = serde_json::from_slice(&index.bytes)?;
    let compiler = option_env!("MODELHUB_KIT_BUILD_ID").unwrap_or("");
    let version = env!("CARGO_PKG_VERSION");
    let identity = format!("modelhub-kit-catalog-v{version}+{}:", descriptor.git_commit);
    ensure!(
        descriptor.schema_version == 1
            && descriptor.version == version
            && descriptor.filename == BINARY
            && descriptor.platform == env::consts::OS
            && descriptor.architecture == env::consts::ARCH
            && descriptor.git_commit.len() == 40
            && descriptor
                .git_commit
                .bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
            && compiler.starts_with(&identity)
            && files::hex_digest(&descriptor.sha256),
        Guard("invalid-native-bundle-identity")
    );
    let checksum = files::snapshot(&source.join("native/bundle.sha256"), 128)?
        .context("bundle checksum missing")?;
    ensure!(
        std::str::from_utf8(&checksum.bytes)?.trim() == descriptor.sha256,
        Guard("bundle-checksum-mismatch")
    );
    for required in [
        "modelhub_kit/__init__.py",
        "modelhub_kit/__main__.py",
        "modelhub_kit/core.py",
        "modelhub_kit/default-config.json",
        "modelhub_kit/windows-launcher.cmd",
    ] {
        ensure!(
            descriptor.files.contains_key(required),
            Guard("incomplete-bundle")
        );
    }
    ensure!(
        descriptor.files.len() <= 62 && descriptor.files.keys().all(|name| payload_path(name)),
        Guard("unsafe-bundle-membership")
    );
    let mut content = BTreeMap::new();
    for (name, hash) in descriptor
        .files
        .iter()
        .map(|(key, value)| (key.as_str(), value))
        .chain([(BINARY, &descriptor.sha256)])
    {
        let bytes = files::snapshot(
            &source.join(name),
            if name == BINARY {
                MAX_EXECUTABLE
            } else {
                MAX_MODULE
            },
        )?
        .context("bundle member missing")?;
        ensure!(
            bytes.hash() == *hash,
            Guard("bundle-member-checksum-mismatch")
        );
        content.insert(name.to_owned(), bytes.bytes);
    }
    Ok((descriptor, content))
}

fn selected_node(old: Option<&Snapshot>) -> String {
    // Preserve the owned binding in an existing compatibility entry point.
    if let Some(value) = old
        .and_then(|file| std::str::from_utf8(&file.bytes).ok())
        .and_then(|text| {
            text.lines()
                .find_map(|line| line.strip_prefix("node_bin = "))
        })
    {
        return value.to_owned();
    }
    if !cfg!(windows) {
        if let Some(home) = env::home_dir() {
            let local = home.join(".local/bin");
            if local.join("node").is_file() {
                return serde_json::to_string(&local.to_string_lossy()).unwrap();
            }
        }
    }
    "None".to_owned()
}

fn compatibility_entry(node: &str) -> Vec<u8> {
    format!(
        r#"#!/usr/bin/env python3
"""Installed application entry; configuration and state live outside this tree."""
import pathlib
import runpy
import sys
import os
sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
node_bin = {node}
if node_bin:
    os.environ["PATH"] = node_bin + os.pathsep + os.environ.get("PATH", "")
from modelhub_kit.core import installed_application
with installed_application(pathlib.Path(__file__).parent.parent):
    runpy.run_module("modelhub_kit", run_name="__main__", alter_sys=True)
"#
    )
    .into_bytes()
}

fn command(places: &Locations) -> Result<Vec<u8>> {
    let path = places.app.join(BINARY);
    let binary = path.to_str().context("launcher requires a text path")?;
    let data = if cfg!(windows) {
        let compatibility = places.app.join("launch.py");
        let compatibility = compatibility
            .to_str()
            .context("launcher requires a text path")?;
        ensure!(
            !binary.contains(['\r', '\n', '"']) && !compatibility.contains(['\r', '\n', '"']),
            Guard("unsupported-launcher-path")
        );
        let binary = binary.replace('/', "\\").replace('%', "%%");
        let compatibility = compatibility.replace('/', "\\").replace('%', "%%");
        include_str!("../../modelhub_kit/windows-launcher.cmd")
            .replace("\r\n", "\n")
            .replace("__BINARY__", &binary)
            .replace("__COMPAT__", &compatibility)
            .replace('\n', "\r\n")
    } else {
        let binary = binary.replace('\'', "'\\''");
        format!("#!/bin/sh\n# modelhub-kit:launcher:v1\nexec '{binary}' --cli \"$@\"\n")
    };
    Ok(data.into_bytes())
}

fn present(file: Option<&Snapshot>, hashes: [Option<&String>; 2]) -> bool {
    file.is_none_or(|value| {
        let hash = value.hash();
        hashes
            .into_iter()
            .flatten()
            .any(|expected| *expected == hash)
    })
}

pub fn run(arguments: &[OsString]) -> Result<u8> {
    if arguments == ["--help"] || arguments == ["-h"] {
        println!(
            "Install the verified ModelHub Kit bundle\n\nUsage: scripts/install.sh [--dry-run]"
        );
        return Ok(0);
    }
    ensure!(
        arguments.is_empty() || arguments == ["--dry-run"],
        Guard("unsupported-native-install-option")
    );
    let dry_run = !arguments.is_empty();
    let exe = env::current_exe()?;
    let source = files::canonical(
        exe.parent()
            .and_then(Path::parent)
            .context("bundle source missing")?,
    )?;
    ensure!(
        files::canonical(&exe)? == files::canonical(&source.join(BINARY))?,
        Guard("unverified-native-bundle-entry")
    );
    let (bundle, mut content) = load_bundle(&source)?;
    let places = Locations::current()?;
    let launch = command(&places)?;
    let _application = (!dry_run)
        .then(|| files::lock(&places.base.join(".application.lock"), false))
        .transpose()?;
    let _writer = (!dry_run)
        .then(|| files::lock(&places.state.join("writer.lock"), false))
        .transpose()?;

    let receipt_path = places.app.join(RECEIPT);
    let pending_path = places.base.join(JOURNAL);
    let active = files::snapshot(&receipt_path, MAX_RECEIPT)?;
    let active_digest = active.as_ref().map(Snapshot::hash);
    let observed: Option<Receipt> = active
        .as_ref()
        .map(|bytes| serde_json::from_slice(&bytes.bytes))
        .transpose()?;
    ensure!(
        observed.as_ref().is_none_or(valid_receipt),
        Guard("unowned-install-receipt")
    );
    let journal = files::snapshot(&pending_path, MAX_RECEIPT)?;
    let pending: Option<Transaction> = journal
        .as_ref()
        .map(|value| serde_json::from_slice(&value.bytes))
        .transpose()?;
    if observed.is_none() && pending.is_none() && fs::exists(&places.app)? {
        ensure!(
            fs::read_dir(&places.app)?.next().is_none(),
            Guard("nonempty-unmanaged-application-preserved")
        );
    }
    let previous = if let Some(pending) = &pending {
        ensure!(
            pending.previous.as_ref().is_none_or(valid_receipt)
                && valid_receipt(&pending.next)
                && (pending.previous_receipt == active_digest
                    || observed.as_ref() == Some(&pending.next)),
            Guard("installation-recovery-required")
        );
        pending.previous.as_ref()
    } else {
        observed.as_ref()
    };
    let previous_launch = files::snapshot(&places.app.join("launch.py"), MAX_MODULE)?;
    ensure!(
        present(
            previous_launch.as_ref(),
            [
                previous.and_then(|old| old.files.get("launch.py")),
                pending
                    .as_ref()
                    .and_then(|state| state.next.files.get("launch.py"))
            ]
        ),
        Guard("modified-application-file-preserved")
    );
    let binding = pending.as_ref().map_or_else(
        || selected_node(previous_launch.as_ref()),
        |state| state.node_binding.clone(),
    );
    content.insert("launch.py".to_owned(), compatibility_entry(&binding));
    let desired = Receipt {
        marker: MARKER.to_owned(),
        launcher_sha256: files::digest(&launch),
        files: content
            .iter()
            .map(|(name, value)| (name.clone(), files::digest(value)))
            .collect(),
    };
    ensure!(
        pending.as_ref().is_none_or(|state| state.next == desired),
        Guard("installation-recovery-required")
    );
    ensure!(
        previous.is_none_or(|prior| prior.files.keys().all(|name| desired
            .files
            .contains_key(name)
            || matches!(
                name.as_str(),
                "LICENSE"
                    | "AGENTS.md"
                    | "README.md"
                    | "docs/INSTALL.md"
                    | "docs/OPERATIONS.md"
                    | "docs/SUPPORT.md"
                    | "docs/INTEGRATIONS.md"
                    | "docs/FLEET.md"
            ))),
        Guard("unknown-application-entry-preserved")
    );
    if files::snapshot(&places.config, crate::MAX_CACHE)?.is_none() {
        files::checked_parent(
            places
                .config
                .parent()
                .context("configuration parent missing")?,
            false,
        )?;
    }

    let command_before = files::snapshot(&places.command, MAX_MODULE)?;
    ensure!(
        present(
            command_before.as_ref(),
            [
                previous.map(|old| &old.launcher_sha256),
                pending.as_ref().map(|state| &state.next.launcher_sha256)
            ]
        ),
        Guard("modified-launcher-preserved")
    );
    let entries: BTreeSet<_> = desired
        .files
        .keys()
        .chain(previous.into_iter().flat_map(|old| old.files.keys()))
        .collect();
    let mut before = BTreeMap::new();
    let mut changed = !command_before
        .as_ref()
        .is_some_and(|value| value.matches(&launch, 0o755));
    for name in entries {
        let path = places.app.join(name);
        files::checked_parent(path.parent().context("application path missing")?, false)?;
        let limit = if name == BINARY {
            MAX_EXECUTABLE
        } else {
            MAX_MODULE
        };
        let mode = if name == BINARY { 0o700 } else { 0o600 };
        let current = files::snapshot(&path, limit)?;
        ensure!(
            present(
                current.as_ref(),
                [
                    previous.and_then(|old| old.files.get(name)),
                    pending
                        .as_ref()
                        .and_then(|state| state.next.files.get(name))
                ]
            ),
            Guard("modified-application-file-preserved")
        );
        changed |= match content.get(name) {
            Some(goal) => !current
                .as_ref()
                .is_some_and(|value| value.matches(goal, mode)),
            None => current.is_some(),
        };
        before.insert(name.clone(), current);
    }
    files::checked_parent(
        places.command.parent().context("command path missing")?,
        false,
    )?;
    let mut result = json!({"status":"planned","implementation":"rust","application":places.app,
        "command":places.command,"filesChanged":0,"backupsCreated":0,"scheduleRequested":false,"nativeCatalogSha256":bundle.sha256});
    if dry_run {
        result["allowlistedFiles"] = json!(content.keys().collect::<Vec<_>>());
        println!("{result}");
        return Ok(0);
    }
    // Releases that predate the shared application lease need an independent
    // compatibility activity check once before replacing their launch path.
    if changed
        && previous.is_some_and(|old| old.launcher_sha256 != desired.launcher_sha256)
        && !crate::compat::previously_idle(&source, &places.app, &places.command)
    {
        return Err(Guard("active-or-unknown-kit-runtime").into());
    }
    let desired_bytes = application::pretty(&desired)?;
    let transaction = if changed && journal.is_none() {
        let bytes = application::pretty(&Transaction {
            previous_receipt: active_digest,
            previous: observed,
            next: desired,
            node_binding: binding,
        })?;
        files::replace(&pending_path, &bytes, 0o600, None, MAX_RECEIPT)?;
        files::snapshot(&pending_path, MAX_RECEIPT)?
    } else {
        journal
    };
    let mut count = 0;
    if changed {
        for (name, snapshot) in before {
            let path = places.app.join(&name);
            let (limit, mode) = if name == BINARY {
                (MAX_EXECUTABLE, 0o700)
            } else {
                (MAX_MODULE, 0o600)
            };
            if let Some(body) = content.get(&name) {
                count += usize::from(files::replace(&path, body, mode, snapshot.as_ref(), limit)?);
            } else if let Some(snapshot) = snapshot {
                ensure!(
                    files::snapshot(&path, limit)?.as_ref() == Some(&snapshot),
                    Guard("concurrent-file-change")
                );
                fs::remove_file(path)?;
                count += 1;
            }
        }
        count += usize::from(files::replace(
            &places.command,
            &launch,
            0o755,
            command_before.as_ref(),
            MAX_MODULE,
        )?);
    }
    files::replace(
        &receipt_path,
        &desired_bytes,
        0o600,
        active.as_ref(),
        MAX_RECEIPT,
    )?;
    application::initialize(&places.config)?;
    if let Some(transaction) = transaction {
        ensure!(
            files::snapshot(&pending_path, MAX_RECEIPT)?.as_ref() == Some(&transaction),
            Guard("concurrent-file-change")
        );
        fs::remove_file(pending_path)?;
    }
    result["status"] = json!(if count == 0 { "current" } else { "installed" });
    result["filesChanged"] = json!(count);
    result["initExit"] = json!(0);
    println!("{result}");
    Ok(0)
}
