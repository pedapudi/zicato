//! Axum HTTP + SSE server. Binds to the first available port in the
//! `--port..=--port+10` range to avoid clashing with a previous run.

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

pub async fn serve(
    paths: WorkspacePaths,
    bind: IpAddr,
    preferred_port: u16,
    read_only: bool,
    watch_tx: broadcast::Sender<WatchEvent>,
    shutdown: broadcast::Sender<()>,
) -> std::io::Result<ServerHandle> {
    let listener = build_listener(bind, preferred_port, 10).await?;
    let addr = listener.local_addr()?;
    info!(?addr, "supervisor http server listening");

    let state = AppState {
        paths,
        watch_tx,
        read_only,
        started: Arc::new(Instant::now()),
        build_version: env!("CARGO_PKG_VERSION"),
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
