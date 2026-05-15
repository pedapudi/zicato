//! File-system watcher (inotify on Linux, FSEvents on macOS).
//!
//! Emits a `WatchEvent` describing which state-file *kind* changed, with
//! per-file debounce so a noisy rename storm doesn't fan out to every
//! SSE client a hundred times.

use crate::reader::WorkspacePaths;
use notify::{Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde::Serialize;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use tokio::sync::broadcast;
use tracing::{debug, warn};

/// What kind of state changed. Matches the SSE `state_change.kind` field.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ChangeKind {
    Heartbeat,
    ActiveRuns,
    ActiveTournament,
    Lineage,
    Lock,
    Epoch,
    Control,
    Unknown,
}

#[derive(Debug, Clone, Serialize)]
pub struct WatchEvent {
    pub kind: ChangeKind,
    pub path: String,
    pub ts: chrono::DateTime<chrono::Utc>,
}

fn classify(path: &Path, paths: &WorkspacePaths) -> ChangeKind {
    if path == paths.heartbeat() {
        return ChangeKind::Heartbeat;
    }
    if path == paths.lock() {
        return ChangeKind::Lock;
    }
    if path == paths.active_tournament() {
        return ChangeKind::ActiveTournament;
    }
    if path == paths.lineage() {
        return ChangeKind::Lineage;
    }
    if path == paths.current_epoch_marker() {
        return ChangeKind::Epoch;
    }
    if path.starts_with(paths.active_runs_dir()) {
        return ChangeKind::ActiveRuns;
    }
    if path.starts_with(paths.control_dir()) {
        return ChangeKind::Control;
    }
    if path.starts_with(&paths.epochs) {
        return ChangeKind::Epoch;
    }
    ChangeKind::Unknown
}

/// Spawn the file-system watcher. Emits debounced `WatchEvent`s on `tx`.
///
/// Returns the live `notify::RecommendedWatcher` so the caller can keep it
/// alive for the lifetime of the program (dropping cancels watching).
pub fn spawn(
    paths: WorkspacePaths,
    tx: broadcast::Sender<WatchEvent>,
    debounce: Duration,
) -> notify::Result<RecommendedWatcher> {
    // Ensure the directories exist so notify doesn't fail on startup.
    for dir in [&paths.runtime, &paths.epochs] {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = std::fs::create_dir_all(paths.active_runs_dir());
    let _ = std::fs::create_dir_all(paths.control_dir());

    // We use a std::sync::mpsc inside the notify callback (it's sync); a
    // dedicated thread drains it and emits debounced events.
    let (raw_tx, raw_rx) = std::sync::mpsc::channel::<Event>();
    let mut watcher = notify::recommended_watcher(move |res: notify::Result<Event>| match res {
        Ok(ev) => {
            let _ = raw_tx.send(ev);
        }
        Err(e) => warn!(error=%e, "watcher error"),
    })?;

    watcher.watch(&paths.runtime, RecursiveMode::Recursive)?;
    if paths.epochs.exists() {
        watcher.watch(&paths.epochs, RecursiveMode::Recursive)?;
    }
    // Also watch the workspace root non-recursively for current_epoch / lineage.json
    watcher.watch(&paths.workspace, RecursiveMode::NonRecursive)?;

    let paths_for_thread = paths.clone();
    std::thread::spawn(move || debounce_loop(raw_rx, paths_for_thread, tx, debounce));

    Ok(watcher)
}

fn debounce_loop(
    rx: std::sync::mpsc::Receiver<Event>,
    paths: WorkspacePaths,
    tx: broadcast::Sender<WatchEvent>,
    debounce: Duration,
) {
    let mut last_emit: HashMap<PathBuf, Instant> = HashMap::new();
    while let Ok(ev) = rx.recv() {
        if !is_interesting(&ev.kind) {
            continue;
        }
        for p in ev.paths {
            let now = Instant::now();
            let too_soon = last_emit
                .get(&p)
                .map(|prev| now.duration_since(*prev) < debounce)
                .unwrap_or(false);
            if too_soon {
                continue;
            }
            last_emit.insert(p.clone(), now);

            let kind = classify(&p, &paths);
            let path_str = p.display().to_string();
            debug!(?kind, ?path_str, "file changed");
            let _ = tx.send(WatchEvent {
                kind,
                path: path_str,
                ts: chrono::Utc::now(),
            });
        }
    }
}

fn is_interesting(kind: &EventKind) -> bool {
    matches!(
        kind,
        EventKind::Create(_) | EventKind::Modify(_) | EventKind::Remove(_)
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn classify_heartbeat_path() {
        let tmp = TempDir::new().unwrap();
        let p = WorkspacePaths::new(tmp.path().to_path_buf());
        assert_eq!(classify(&p.heartbeat(), &p), ChangeKind::Heartbeat);
        assert_eq!(
            classify(&p.active_tournament(), &p),
            ChangeKind::ActiveTournament
        );
        assert_eq!(
            classify(&p.active_runs_dir().join("r1.json"), &p),
            ChangeKind::ActiveRuns
        );
        assert_eq!(
            classify(&p.control_dir().join("pause_epoch"), &p),
            ChangeKind::Control
        );
        assert_eq!(classify(Path::new("/nowhere"), &p), ChangeKind::Unknown);
    }
}
