mod application;
mod catalog;
mod compat;
mod files;
mod install;
mod metadata;

use std::io::{self, BufRead, BufWriter, Read, Write};

use anyhow::{Result, ensure};
use serde_json::json;

const MAX_CONTROL: u64 = 64 * 1024;
const MAX_CACHE: usize = 8 * 1024 * 1024;

fn main() -> Result<()> {
    let arguments = std::env::args_os().skip(1).collect::<Vec<_>>();
    #[cfg(windows)]
    if arguments == ["--find-python-adapter"] {
        compat::print_python_selection();
        return Ok(());
    }
    if let Some(command) = arguments.first().and_then(|value| value.to_str()) {
        let result = match command {
            "--cli" => Some(application::cli(&arguments[1..])),
            "--install" => Some(install::run(&arguments[1..])),
            _ => None,
        };
        if let Some(result) = result {
            let exit = match result {
                Ok(code) => code,
                Err(error) => {
                    let guard = error.downcast_ref::<files::Guard>();
                    let reason = guard
                        .map_or("operation-failed-current-settings-retained", |guard| {
                            guard.0
                        });
                    let queued = matches!(
                        reason,
                        "writer-busy"
                            | "active-or-unknown-kit-runtime"
                            | "installation-recovery-required"
                            | "unsafe-installation-directory-preserved"
                    );
                    let status = if queued {
                        "queued"
                    } else if guard.is_some() {
                        "refused"
                    } else {
                        "error"
                    };
                    println!("{}", json!({"status":status,"reason":reason}));
                    if queued {
                        75
                    } else if guard.is_some() {
                        78
                    } else {
                        1
                    }
                }
            };
            std::process::exit(i32::from(exit));
        }
    }
    match std::env::args().nth(1).as_deref() {
        Some("--version") => {
            println!("modelhub-kit-catalog {}", env!("CARGO_PKG_VERSION"));
            return Ok(());
        }
        Some("--build-id") => {
            println!(
                "{}",
                option_env!("MODELHUB_KIT_BUILD_ID").unwrap_or("local-build")
            );
            return Ok(());
        }
        _ => {}
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let mut input = io::stdin().lock();
    let mut output = BufWriter::new(io::stdout().lock());
    let mut client = None;
    let mut message = Vec::new();
    let mut cached = Vec::new();
    loop {
        message.clear();
        let count = (&mut input)
            .take(MAX_CONTROL + 1)
            .read_until(b'\n', &mut message)?;
        if count == 0 {
            break;
        }
        ensure!(
            (count as u64) <= MAX_CONTROL && message.ends_with(b"\n"),
            "invalid native catalog input length"
        );
        let request = serde_json::from_slice::<catalog::Request>(&message)?;
        ensure!(
            request.prior_bytes <= MAX_CACHE,
            "invalid catalog cache length"
        );
        cached.resize(request.prior_bytes, 0);
        input.read_exact(&mut cached)?;
        let prior = serde_json::from_slice(&cached).ok();
        match runtime.block_on(request.process(&mut client, prior)) {
            Ok(reply) => serde_json::to_writer(&mut output, &reply)?,
            Err(_) => serde_json::to_writer(
                &mut output,
                &json!({"error":"native-catalog-operation-invalid"}),
            )?,
        }
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}
