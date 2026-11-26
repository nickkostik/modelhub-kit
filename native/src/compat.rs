use std::{
    env,
    ffi::OsString,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

#[cfg(unix)]
use std::os::unix::process::CommandExt;
#[cfg(windows)]
use std::os::windows::process::CommandExt;

use anyhow::Result;
use serde_json::json;

struct Python {
    path: PathBuf,
    manager: bool,
}

impl Python {
    fn command(&self) -> Command {
        let mut command = Command::new(&self.path);
        if self.manager {
            command.arg("-3");
        }
        command
            .env("PYTHON_MANAGER_AUTOMATIC_INSTALL", "false")
            .env_remove("PYLAUNCHER_ALLOW_INSTALL")
            .env_remove("PYLAUNCHER_ALWAYS_INSTALL");
        #[cfg(windows)]
        command.creation_flags(0x0800_0000);
        command
    }
}

fn wait_probe(mut child: Child, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(25)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return false;
            }
        }
    }
}

fn find_python() -> Option<Python> {
    let path = env::var_os("PATH").unwrap_or_default();
    let mut directories: Vec<_> = env::split_paths(&path)
        .filter(|path| path.is_absolute())
        .collect();
    if cfg!(target_os = "macos") {
        directories.extend([
            PathBuf::from("/opt/homebrew/bin"),
            PathBuf::from("/usr/local/bin"),
        ]);
    }
    let names: &[&str] = if cfg!(windows) {
        &["py.exe", "python.exe", "python3.exe"]
    } else {
        &[
            "python3",
            "python3.14",
            "python3.13",
            "python3.12",
            "python3.11",
        ]
    };
    for folder in directories {
        for name in names {
            let path = folder.join(name);
            let manager = *name == "py.exe";
            let alias = cfg!(windows)
                && folder
                    .to_string_lossy()
                    .to_lowercase()
                    .ends_with("microsoft\\windowsapps");
            if (!manager && alias) || !path.is_file() {
                continue;
            }
            let python = Python { path, manager };
            let probe = python
                .command()
                .args([
                    "-I",
                    "-B",
                    "-c",
                    "import sys; raise SystemExit(sys.version_info < (3, 11))",
                ])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn();
            if probe.is_ok_and(|child| wait_probe(child, Duration::from_secs(3))) {
                return Some(python);
            }
        }
    }
    None
}

pub fn previously_idle(source: &Path, app: &Path, command: &Path) -> bool {
    let Some(python) = find_python() else {
        return false;
    };
    let code = "import pathlib,sys; sys.path.insert(0,sys.argv[1]); from modelhub_kit.lifecycle import _idle; raise SystemExit(not _idle(pathlib.Path(sys.argv[2]),pathlib.Path(sys.argv[3])))";
    python
        .command()
        .args(["-I", "-B", "-c", code])
        .args([source, app, command])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .is_ok_and(|child| wait_probe(child, Duration::from_secs(20)))
}

pub fn run(app: &Path, arguments: &[OsString]) -> Result<u8> {
    #[cfg(windows)]
    if arguments.iter().any(|value| value == "uninstall")
        && arguments.iter().any(|value| value == "--apply")
    {
        // The cmd launcher resumes only after the loaded image has exited.
        return Ok(86);
    }
    let Some(python) = find_python() else {
        println!(
            "{}",
            json!({"status":"queued","reason":"python-adapter-unavailable"})
        );
        return Ok(75);
    };
    let mut command = python.command();
    command
        .args(["-I", "-B"])
        .arg(app.join("launch.py"))
        .args(arguments);
    #[cfg(unix)]
    return Err(command.exec().into());
    #[cfg(windows)]
    return Ok(command
        .status()?
        .code()
        .and_then(|code| u8::try_from(code).ok())
        .unwrap_or(1));
}

#[cfg(windows)]
pub fn print_python_selection() {
    if let Some(python) = find_python() {
        if let Some(path) = python.path.to_str() {
            let kind = if python.manager {
                "manager"
            } else {
                "interpreter"
            };
            println!("{kind}|{path}");
        }
    }
}
