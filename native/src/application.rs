use std::{
    collections::HashSet,
    env,
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, ensure};
use serde_json::{Map, Value, json};

use crate::{
    MAX_CACHE,
    catalog::{self, Operation, Request, Source},
    files::{self, Guard},
};

pub const BINARY: &str = if cfg!(windows) {
    "native/modelhub-kit-catalog.exe"
} else {
    "native/modelhub-kit-catalog"
};
pub const RECEIPT: &str = ".modelhub-kit-install.json";
pub const MARKER: &str = "modelhub-kit-install-v1";
const DEFAULT: &[u8] = include_bytes!("../../modelhub_kit/default-config.json");

pub struct Locations {
    pub base: PathBuf,
    pub app: PathBuf,
    pub command: PathBuf,
    pub config: PathBuf,
    pub state: PathBuf,
}

impl Locations {
    pub fn current() -> Result<Self> {
        let home = env::home_dir().context("home directory unavailable")?;
        let variable = |key, default| env::var_os(key).map_or(default, PathBuf::from);
        let (base, config, state, command) = if cfg!(windows) {
            let base = variable("LOCALAPPDATA", home.join("AppData/Local")).join("modelhub-kit");
            let config = base.join("config.json");
            let state = base.join("state");
            let command = base.join("bin/modelhub-kit.cmd");
            (base, config, state, command)
        } else {
            (
                home.join(".local/share/modelhub-kit"),
                variable("XDG_CONFIG_HOME", home.join(".config")).join("modelhub-kit/config.json"),
                variable("XDG_STATE_HOME", home.join(".local/state")).join("modelhub-kit"),
                home.join(".local/bin/modelhub-kit"),
            )
        };
        Ok(Self {
            app: base.join("app"),
            base,
            command,
            config: std::path::absolute(config)?,
            state: std::path::absolute(variable("MODELHUB_KIT_HOME", state))?,
        })
    }
}

pub fn initialize(config: &Path) -> Result<bool> {
    if files::snapshot(config, MAX_CACHE)?.is_some() {
        return Ok(false);
    }
    let data = serde_json::from_slice::<Value>(DEFAULT)?;
    files::replace(config, &pretty(&data)?, 0o600, None, MAX_CACHE)
}

pub fn pretty(value: &impl serde::Serialize) -> Result<Vec<u8>> {
    let mut encoded = serde_json::to_vec_pretty(value)?;
    encoded.push(b'\n');
    Ok(encoded)
}

fn checked_sources(path: &Path) -> Result<Vec<Source>> {
    let file =
        files::snapshot(path, MAX_CACHE)?.ok_or(Guard("run-init-or-provide-version1-config"))?;
    let config: Value = serde_json::from_slice(&file.bytes)?;
    ensure!(
        config.get("version").and_then(Value::as_u64) == Some(1),
        Guard("run-init-or-provide-version1-config")
    );
    let Some(sources) = config.get("catalog_sources") else {
        return Ok(Vec::new());
    };
    let sources = sources
        .as_array()
        .filter(|rows| rows.len() <= 16)
        .ok_or(Guard("invalid-catalog-sources"))?;
    let mut ids = HashSet::new();
    sources
        .iter()
        .map(|raw| {
            let source: Source = serde_json::from_value(raw.clone())
                .map_err(|_| Guard("catalog-url-and-environment-references-only"))?;
            ensure!(
                catalog::safe_id(&source.id, 64) && ids.insert(source.id.clone()),
                Guard("unique-safe-source-id-required")
            );
            ensure!(
                raw.get("provider_id").is_none()
                    || source
                        .provider_id
                        .as_deref()
                        .is_some_and(|value| catalog::safe_id(value, 128)),
                Guard("exact-catalog-provider-id-required")
            );
            Ok(source)
        })
        .collect()
}

fn reconcile(config: &Path, state: &Path, operation: Operation) -> Result<(Value, u8)> {
    let sources = checked_sources(config)?;
    let sync = operation == Operation::Sync;
    let _writer = sync
        .then(|| files::lock(&state.join("writer.lock"), false))
        .transpose()?;
    let cooldown_file = state.join("catalog-cooldowns.json");
    let cooldown_snapshot = if sync {
        files::snapshot(&cooldown_file, MAX_CACHE)?
    } else {
        None
    };
    let original: Map<String, Value> = cooldown_snapshot
        .as_ref()
        .map(|prior| serde_json::from_slice(&prior.bytes))
        .transpose()?
        .unwrap_or_default();
    let mut cooldowns = Map::new();
    let mut rows = Vec::new();
    let mut client = None;
    let runtime = (!sources.is_empty())
        .then(|| {
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
        })
        .transpose()?;
    for source in sources {
        let name = source.id.clone();
        let file = state.join("catalogs").join(format!("{name}.json"));
        let prior = match files::snapshot(&file, MAX_CACHE) {
            Ok(value) => value,
            Err(_) => {
                if let Some(cooldown) = original.get(&name) {
                    cooldowns.insert(name.clone(), cooldown.clone());
                }
                rows.push(if sync { json!({"source":name,"status":"refresh-failed","retained":false}) }
                    else { json!({"source":name,"provider_id":source.provider_id,"models":[],"status":"queued-metadata-unavailable"}) });
                continue;
            }
        };
        let request = Request {
            operation,
            prior_bytes: 0,
            source,
            cooldown: original.get(&name).cloned(),
        };
        let prior_value = prior
            .as_ref()
            .and_then(|value| serde_json::from_slice(&value.bytes).ok());
        let mut reply = runtime
            .as_ref()
            .context("catalog runtime unavailable")?
            .block_on(request.process(&mut client, prior_value))
            .map_err(|_| Guard("native-catalog-reply-invalid"))?;
        if let Some(record) = reply.record {
            let mut body = serde_json::to_vec(&record)?;
            body.push(b'\n');
            let wrote = (body.len() <= MAX_CACHE)
                .then(|| files::replace(&file, &body, 0o600, prior.as_ref(), MAX_CACHE))
                .transpose();
            reply.result = match wrote {
                Ok(Some(changed)) => {
                    reply.result["written"] = json!(changed);
                    reply.result
                }
                _ => json!({"source":name,"status":"catalog-write-deferred"}),
            };
        }
        if let Some(cooldown) = reply.cooldown {
            cooldowns.insert(name, cooldown);
        }
        rows.push(reply.result);
    }
    if sync && cooldowns != original {
        files::replace(
            &cooldown_file,
            &pretty(&cooldowns)?,
            0o600,
            cooldown_snapshot.as_ref(),
            MAX_CACHE,
        )?;
    }
    let statuses = rows.iter().map(|row| row["status"].as_str().unwrap_or(""));
    if sync {
        let (status, exit) = if statuses
            .clone()
            .all(|status| matches!(status, "ready" | "not-modified"))
        {
            ("completed", 0)
        } else if statuses
            .clone()
            .any(|status| status.starts_with("refresh-failed"))
        {
            ("failed", 1)
        } else {
            ("queued", 75)
        };
        Ok((json!({"catalogs":rows,"status":status}), exit))
    } else {
        let status = if statuses.clone().all(|status| status == "ready") {
            "ready"
        } else {
            "queued"
        };
        Ok((
            json!({"status":status,"readOnly":true,"modelCalls":0,"capacityProbed":false,"sources":rows}),
            0,
        ))
    }
}

pub fn cli(arguments: &[OsString]) -> Result<u8> {
    if arguments == ["--version"] {
        println!("{}", env!("CARGO_PKG_VERSION"));
        return Ok(0);
    }
    if arguments.is_empty() || arguments == ["--help"] || arguments == ["-h"] {
        println!(
            "ModelHub Kit\n\nmodelhub-kit [--config PATH] <init|sync|limits>\n\nCompatibility commands use Python 3.11+.\n  doctor update maintain integrate project register-acp export-acp cache fleet schedule uninstall"
        );
        return Ok(0);
    }
    let places = Locations::current()?;
    ensure!(
        files::canonical(&env::current_exe()?)? == files::canonical(&places.app.join(BINARY))?,
        Guard("unverified-application-entry")
    );
    let mut path = places.config.clone();
    let mut parts = arguments.iter().peekable();
    while let Some(arg) = parts.peek().and_then(|value| value.to_str()) {
        if arg == "--config" {
            parts.next();
            path = std::path::absolute(parts.next().context("config path missing")?)?;
        } else if let Some(value) = arg.strip_prefix("--config=") {
            path = std::path::absolute(value)?;
            parts.next();
        } else {
            break;
        }
    }
    let command = parts.next().and_then(|value| value.to_str());
    if !matches!(command, Some("init" | "sync" | "limits")) || parts.next().is_some() {
        return crate::compat::run(&places.app, arguments);
    }
    let _lease = files::lock(&places.base.join(".application.lock"), true)?;
    // A killed upgrade is recoverable by rerunning its checked bundle. Do not
    // interpret a partially written application as the current version.
    ensure!(
        !fs::exists(places.base.join(".install-pending.json"))?,
        Guard("installation-recovery-required")
    );
    let (result, exit) = match command {
        Some("init") => {
            let _writer = files::lock(&places.state.join("writer.lock"), false)?;
            let changed = initialize(&path)?;
            let status = if changed {
                "initialized"
            } else {
                "existing-config-preserved"
            };
            (json!({"status":status,"changed":changed}), 0)
        }
        Some("sync") => reconcile(&path, &places.state, Operation::Sync)?,
        Some("limits") => reconcile(&path, &places.state, Operation::Limits)?,
        _ => unreachable!(),
    };
    println!("{result}");
    Ok(exit)
}
