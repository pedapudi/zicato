//! Read runtime state files from disk and assemble in-memory snapshots.
//!
//! Files are small JSON blobs written atomically by the Python side
//! (`.tmp` + `rename`). Reads are best-effort: a missing or
//! transiently-truncated file returns `None` rather than panicking.

use crate::state::{ActiveRun, ActiveTournament, Heartbeat, Lineage, Lock, Snapshot};
use chrono::Utc;
use serde::de::DeserializeOwned;
use std::path::{Path, PathBuf};
use tracing::warn;

/// Layout of the state files inside a workspace.
#[derive(Debug, Clone)]
pub struct WorkspacePaths {
    pub workspace: PathBuf,
    pub runtime: PathBuf,
    pub epochs: PathBuf,
}

impl WorkspacePaths {
    pub fn new(workspace: PathBuf) -> Self {
        let runtime = workspace.join("runtime");
        let epochs = workspace.join("epochs");
        Self {
            workspace,
            runtime,
            epochs,
        }
    }

    pub fn heartbeat(&self) -> PathBuf {
        self.runtime.join("heartbeat.json")
    }

    pub fn lock(&self) -> PathBuf {
        self.runtime.join("lock.json")
    }

    pub fn active_runs_dir(&self) -> PathBuf {
        self.runtime.join("active_runs")
    }

    pub fn active_tournament(&self) -> PathBuf {
        self.runtime.join("active_tournament.json")
    }

    pub fn control_dir(&self) -> PathBuf {
        self.runtime.join("control")
    }

    pub fn current_epoch_marker(&self) -> PathBuf {
        self.workspace.join("current_epoch")
    }

    pub fn lineage(&self) -> PathBuf {
        self.workspace.join("lineage.json")
    }

    /// SQLite analytical index built by `zicato reindex`
    /// (`.zicato/index/index.db`). May be absent.
    pub fn index_db(&self) -> PathBuf {
        self.workspace.join("index").join("index.db")
    }

    /// Per-epoch loop-health report directory
    /// (`.zicato/epochs/{epoch_id}/health/`).
    pub fn epoch_health_dir(&self, epoch_id: &str) -> PathBuf {
        self.epochs.join(epoch_id).join("health")
    }
}

fn read_json<T: DeserializeOwned>(path: &Path) -> Option<T> {
    let bytes = match std::fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return None,
        Err(e) => {
            warn!(?path, error=%e, "failed to read state file");
            return None;
        }
    };
    if bytes.is_empty() {
        // Mid-rename: writer may have created an empty tmp briefly.
        return None;
    }
    match serde_json::from_slice::<T>(&bytes) {
        Ok(v) => Some(v),
        Err(e) => {
            warn!(?path, error=%e, "state file failed to parse; ignoring");
            None
        }
    }
}

pub fn read_heartbeat(paths: &WorkspacePaths) -> Option<Heartbeat> {
    read_json(&paths.heartbeat())
}

pub fn read_lock(paths: &WorkspacePaths) -> Option<Lock> {
    read_json(&paths.lock())
}

pub fn read_active_tournament(paths: &WorkspacePaths) -> Option<ActiveTournament> {
    read_json(&paths.active_tournament())
}

pub fn read_lineage(paths: &WorkspacePaths) -> Option<Lineage> {
    read_json(&paths.lineage())
}

pub fn read_active_runs(paths: &WorkspacePaths) -> Vec<ActiveRun> {
    let dir = paths.active_runs_dir();
    let entries = match std::fs::read_dir(&dir) {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Vec::new(),
        Err(e) => {
            warn!(?dir, error=%e, "failed to list active_runs");
            return Vec::new();
        }
    };

    let mut out = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|s| s.to_str()) != Some("json") {
            continue;
        }
        if let Some(run) = read_json::<ActiveRun>(&path) {
            out.push(run);
        }
    }
    // Stable ordering: by run_id so the UI doesn't shuffle.
    out.sort_by(|a, b| a.run_id.cmp(&b.run_id));
    out
}

pub fn read_current_epoch(paths: &WorkspacePaths) -> Option<String> {
    let marker = paths.current_epoch_marker();
    match std::fs::read_to_string(&marker) {
        Ok(s) => {
            let s = s.trim().to_string();
            if s.is_empty() {
                None
            } else {
                Some(s)
            }
        }
        Err(_) => None,
    }
}

pub fn build_snapshot(paths: &WorkspacePaths) -> Snapshot {
    Snapshot {
        heartbeat: read_heartbeat(paths),
        lock: read_lock(paths),
        active_runs: read_active_runs(paths),
        active_tournament: read_active_tournament(paths),
        lineage: read_lineage(paths),
        epoch_id: read_current_epoch(paths),
        epoch: crate::epoch::build_epoch_view(paths),
        generated_at: Utc::now(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn make_ws() -> (TempDir, WorkspacePaths) {
        let tmp = TempDir::new().unwrap();
        let ws = tmp.path().to_path_buf();
        std::fs::create_dir_all(ws.join("runtime/active_runs")).unwrap();
        std::fs::create_dir_all(ws.join("runtime/control")).unwrap();
        let p = WorkspacePaths::new(ws);
        (tmp, p)
    }

    #[test]
    fn missing_files_yield_none() {
        let (_t, p) = make_ws();
        assert!(read_heartbeat(&p).is_none());
        assert!(read_active_tournament(&p).is_none());
        assert!(read_active_runs(&p).is_empty());
    }

    #[test]
    fn malformed_heartbeat_does_not_panic() {
        let (_t, p) = make_ws();
        std::fs::write(p.heartbeat(), "{not-json").unwrap();
        assert!(read_heartbeat(&p).is_none());
    }

    #[test]
    fn empty_file_is_treated_as_missing() {
        let (_t, p) = make_ws();
        std::fs::write(p.heartbeat(), "").unwrap();
        assert!(read_heartbeat(&p).is_none());
    }

    #[test]
    fn active_runs_are_collected_and_sorted() {
        let (_t, p) = make_ws();
        let dir = p.active_runs_dir();
        std::fs::write(dir.join("b.json"), r#"{"run_id":"b","pid":42}"#).unwrap();
        std::fs::write(dir.join("a.json"), r#"{"run_id":"a","pid":7}"#).unwrap();
        let runs = read_active_runs(&p);
        assert_eq!(runs.len(), 2);
        assert_eq!(runs[0].run_id, "a");
        assert_eq!(runs[1].run_id, "b");
    }

    #[test]
    fn snapshot_assembles_full_payload() {
        let (_t, p) = make_ws();
        std::fs::write(p.heartbeat(), r#"{"pid":1,"phase":"running"}"#).unwrap();
        std::fs::write(
            p.active_tournament(),
            r#"{"tournament_id":"t1","entries":[]}"#,
        )
        .unwrap();
        std::fs::write(p.current_epoch_marker(), "2026-05-14_test").unwrap();

        let snap = build_snapshot(&p);
        assert_eq!(snap.heartbeat.as_ref().unwrap().pid, Some(1));
        assert_eq!(
            snap.active_tournament
                .as_ref()
                .unwrap()
                .tournament_id
                .as_deref(),
            Some("t1")
        );
        assert_eq!(snap.epoch_id.as_deref(), Some("2026-05-14_test"));
    }
}
