mod catalog;
mod metadata;

use std::io::{self, BufRead, BufWriter, Read, Write};

use anyhow::{Result, ensure};
use serde_json::json;

const MAX_CONTROL: u64 = 64 * 1024;
const MAX_CACHE: usize = 8 * 1024 * 1024;

fn main() -> Result<()> {
    match std::env::args().nth(1).as_deref() {
        Some("--version") => {
            println!("modelhub-kit-catalog {}", env!("CARGO_PKG_VERSION"));
            return Ok(());
        }
        Some("--build-id") => {
            println!("{}", option_env!("MODELHUB_KIT_BUILD_ID").unwrap_or("local-build"));
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
