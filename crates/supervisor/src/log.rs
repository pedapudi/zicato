//! Tracing subscriber initialization.
//!
//! Honors the `--log` CLI flag (default `info`). The env filter is
//! constructed from the level plus a fallback to `RUST_LOG` so operators
//! can override targets at runtime without touching the CLI.
//!
//! Log lines go to standard error: standard output carries only the
//! listening-address line `main` prints once the server has bound.

use tracing_subscriber::{fmt, prelude::*, EnvFilter};

pub fn init(level: &str) {
    let env_filter = EnvFilter::try_from_default_env()
        .or_else(|_| EnvFilter::try_new(level))
        .unwrap_or_else(|_| EnvFilter::new("info"));

    let fmt_layer = fmt::layer()
        .with_target(false)
        .with_thread_ids(false)
        .with_thread_names(false)
        .with_level(true)
        .with_writer(std::io::stderr);

    let _ = tracing_subscriber::registry()
        .with(env_filter)
        .with(fmt_layer)
        .try_init();
}
