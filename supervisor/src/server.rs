//! Axum HTTP + SSE server. Binds to the first available port in the
//! `--port..=--port+10` range to avoid clashing with a previous run.

use crate::action_log::WatchdogLog;
use crate::reader::WorkspacePaths;
use crate::routes::{router, AppState};
use crate::watcher::WatchEvent;
use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::time::Instant;
use tokio::net::TcpListener;
use tokio::sync::broadcast;
use tower_http::cors::{Any, CorsLayer};
use tracing::info;

/// Outcome of binding the listener.
pub struct ServerHandle {
    pub addr: SocketAddr,
}

/// A non-empty build identifier for the dashboard footer.
///
/// The crate version, suffixed with a short git SHA when the build
/// script (`build.rs`) could resolve one. Falls back to the bare version
/// for source-tarball builds where `git` was unavailable — but is never
/// empty.
pub fn build_id() -> &'static str {
    // `concat!` requires literals, so the SHA-suffixed form is a const
    // string when `ZICATO_GIT_SHA` is populated, and the bare version
    // otherwise. `env!` of an empty-valued var yields "".
    const SHA: &str = env!("ZICATO_GIT_SHA");
    const VERSION: &str = env!("CARGO_PKG_VERSION");
    if SHA.is_empty() {
        VERSION
    } else {
        // Built once at first call; `concat!` cannot interpolate env at
        // compile time across both branches, so format into a leaked
        // 'static — done a single time for the process lifetime.
        use std::sync::OnceLock;
        static ID: OnceLock<String> = OnceLock::new();
        ID.get_or_init(|| format!("{VERSION}+{SHA}")).as_str()
    }
}

pub async fn build_listener(
    bind: IpAddr,
    preferred_port: u16,
    max_retries: u16,
) -> std::io::Result<TcpListener> {
    let mut last_err = None;
    for offset in 0..=max_retries {
        let port = preferred_port.saturating_add(offset);
        let addr = SocketAddr::new(bind, port);
        match TcpListener::bind(addr).await {
            Ok(l) => return Ok(l),
            Err(e) => last_err = Some(e),
        }
    }
    Err(last_err
        .unwrap_or_else(|| std::io::Error::new(std::io::ErrorKind::AddrInUse, "no port available")))
}

/// Tunables for the HTTP server that are not derived from the listener.
///
/// Grouped into a struct so `serve` keeps a small, stable signature as the
/// watchdog-only surface (`/statusz`) grows its inputs.
#[derive(Clone)]
pub struct ServeOptions {
    pub read_only: bool,
    /// `--no-dashboard`: serve only the watchdog surface (`/statusz`).
    pub dashboard_disabled: bool,
    /// Heartbeat staleness threshold the watchdog enforces (seconds).
    pub heartbeat_stale_threshold_seconds: u64,
    /// Shared in-memory ring buffer of recent watchdog escalations.
    pub action_log: Arc<WatchdogLog>,
}

pub async fn serve(
    paths: WorkspacePaths,
    bind: IpAddr,
    preferred_port: u16,
    options: ServeOptions,
    watch_tx: broadcast::Sender<WatchEvent>,
    shutdown: broadcast::Sender<()>,
) -> std::io::Result<ServerHandle> {
    let listener = build_listener(bind, preferred_port, 10).await?;
    let addr = listener.local_addr()?;
    info!(?addr, "supervisor http server listening");

    let state = AppState {
        paths,
        watch_tx,
        read_only: options.read_only,
        started: Arc::new(Instant::now()),
        build_version: env!("CARGO_PKG_VERSION"),
        // The port actually bound (which may differ from `preferred_port`
        // after the retry walk).
        port: addr.port(),
        build_id: build_id(),
        dashboard_disabled: options.dashboard_disabled,
        heartbeat_stale_threshold_seconds: options.heartbeat_stale_threshold_seconds,
        action_log: options.action_log,
    };

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    let app = router(state).layer(cors);

    let mut shutdown_rx = shutdown.subscribe();
    tokio::spawn(async move {
        let _ = axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                let _ = shutdown_rx.recv().await;
            })
            .await;
    });

    Ok(ServerHandle { addr })
}
