//! SSE broker.
//!
//! New clients receive a `snapshot` event built from the current state
//! files, then live `state_change` events as files mutate. A bounded
//! `tokio::sync::broadcast` channel drops slow clients without ever
//! blocking the file-watcher or other clients.

use crate::reader::{self, WorkspacePaths};
use crate::watcher::WatchEvent;
use axum::response::sse::{Event as SseEvent, KeepAlive, Sse};
use futures::stream::Stream;
use futures::StreamExt;
use std::convert::Infallible;
use std::time::Duration;
use tokio::sync::broadcast;
use tokio_stream::wrappers::BroadcastStream;

pub fn build_sse(
    paths: WorkspacePaths,
    rx: broadcast::Receiver<WatchEvent>,
) -> Sse<impl Stream<Item = Result<SseEvent, Infallible>>> {
    let snapshot = reader::build_snapshot(&paths);
    let initial_payload = serde_json::json!({
        "type": "snapshot",
        "data": snapshot,
    });
    let initial_payload = serde_json::to_string(&initial_payload).unwrap_or_else(|_| "{}".into());

    let initial = futures::stream::once(async move {
        Ok::<SseEvent, Infallible>(SseEvent::default().event("snapshot").data(initial_payload))
    });

    let live = BroadcastStream::new(rx)
        .filter_map(|res| async move { res.ok() })
        .map(|ev| {
            let payload = serde_json::json!({
                "type": "state_change",
                "kind": ev.kind,
                "path": ev.path,
                "ts": ev.ts,
            });
            let payload = serde_json::to_string(&payload).unwrap_or_else(|_| "{}".into());
            Ok::<SseEvent, Infallible>(SseEvent::default().event("state_change").data(payload))
        });

    let merged = initial.chain(live);
    Sse::new(merged).keep_alive(
        KeepAlive::new()
            .interval(Duration::from_secs(15))
            .text("ping"),
    )
}

#[cfg(test)]
mod tests {
    use crate::watcher::{ChangeKind, WatchEvent};
    use chrono::Utc;

    #[test]
    fn watch_event_serializes_with_kind() {
        let ev = WatchEvent {
            kind: ChangeKind::Heartbeat,
            path: "/tmp/x".into(),
            ts: Utc::now(),
        };
        let s = serde_json::to_string(&ev).unwrap();
        assert!(s.contains("\"kind\":\"heartbeat\""));
        assert!(s.contains("/tmp/x"));
    }
}
