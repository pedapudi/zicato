//! Read runtime state files from disk and assemble in-memory snapshots.
//!
//! Files are small JSON blobs written atomically by the Python side
//! (`.tmp` + `rename`). Reads are best-effort: a missing or
//! transiently-truncated file returns `None` rather than panicking.

use crate::state::{ActiveRun, ActiveTournament, Heartbeat, Lineage, Lock, Snapshot};
use chrono::{DateTime, Utc};
use serde::de::DeserializeOwned;
use serde::Serialize;
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
    /// (`<workspace>/index.db`). May be absent.
    pub fn index_db(&self) -> PathBuf {
        self.workspace.join("index.db")
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

/// Public best-effort JSON read, used by sibling modules (`run_log`).
/// Mirrors `read_json`: missing/empty/malformed -> `None`.
pub fn read_json_opt<T: DeserializeOwned>(path: &Path) -> Option<T> {
    read_json(path)
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

/// One generation node in the directory-derived lineage view.
///
/// `promoted` is tri-state: `Some(true)` / `Some(false)` once the
/// tournament has resolved, and `None` while the generation is still in
/// flight (its `experiment.json` carries no decision yet). The dashboard
/// Tree needs the in-flight node so it can draw a generation that is
/// being scored *right now* — not only the promoted chain.
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct LineageGeneration {
    pub generation_id: String,
    pub epoch_id: String,
    pub parent_generation_id: Option<String>,
    /// `None` = still in flight (decision not yet recorded).
    pub promoted: Option<bool>,
    pub created_at: Option<String>,
}

/// The `GET /api/lineage` response: every generation directory under
/// every epoch, in flight or resolved.
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct LineageView {
    pub generations: Vec<LineageGeneration>,
}

/// Pull a decision string out of an `experiment.json` value. The Python
/// side records the tournament outcome either as a bare `outcome` string
/// or nested under `outcome.decision` / `outcome.tournament_decision`.
fn experiment_decision(exp: &serde_json::Value) -> Option<String> {
    let outcome = exp.get("outcome")?;
    if outcome.is_null() {
        return None;
    }
    if let Some(s) = outcome.as_str() {
        return Some(s.to_string());
    }
    outcome
        .get("decision")
        .or_else(|| outcome.get("tournament_decision"))
        .or_else(|| outcome.get("verdict"))
        .and_then(|v| v.as_str())
        .map(str::to_string)
}

/// Fallback metadata for one generation, harvested from the legacy
/// `lineage.json` (which lists only promoted generations).
#[derive(Debug, Clone, Default)]
struct LegacyGenMeta {
    parent_id: Option<String>,
    created_at: Option<String>,
    promoted: Option<bool>,
}

/// Map a decision string to a `promoted` flag. An unrecognised decision
/// is treated as resolved-but-not-promoted rather than in-flight.
fn decision_to_promoted(decision: &str) -> bool {
    matches!(
        decision.trim().to_ascii_lowercase().as_str(),
        "promoted" | "promote" | "accepted" | "accept" | "win" | "won"
    )
}

/// Build the directory-derived lineage view for `GET /api/lineage`.
///
/// Walks `epochs/{id}/generations/*` and emits one node per generation
/// directory — promoted, rejected, *and* not-yet-resolved. Per node:
///
///   * `parent_generation_id` — from the generation's `experiment.json`,
///     falling back to `lineage.json` (the root `v0` has no experiment).
///   * `promoted` — `Some(bool)` once the `experiment.json` outcome /
///     `lineage.json` records a decision, `None` while in flight.
///   * `created_at` — `experiment.json` `proposed_at`, else `lineage.json`
///     `created_at`, else the directory's filesystem creation time.
///
/// Best-effort throughout: a malformed `experiment.json` degrades that
/// one node's metadata rather than dropping it or failing the response.
pub fn build_lineage_view(paths: &WorkspacePaths) -> LineageView {
    use std::collections::HashMap;

    // Index lineage.json by (epoch_id, generation_id) for fallback data.
    // The legacy file only lists promoted generations, but it is a good
    // source for the root's `created_at` and `parent_id`.
    let mut lineage_meta: HashMap<(String, String), LegacyGenMeta> = HashMap::new();
    if let Some(value) = read_json::<serde_json::Value>(&paths.lineage()) {
        if let Some(epochs) = value.get("epochs").and_then(|v| v.as_array()) {
            for ep in epochs {
                let epoch_id = ep
                    .get("id")
                    .and_then(|v| v.as_str())
                    .unwrap_or_default()
                    .to_string();
                if let Some(gens) = ep.get("generations").and_then(|v| v.as_array()) {
                    for g in gens {
                        let gid = match g.get("id").and_then(|v| v.as_str()) {
                            Some(s) => s.to_string(),
                            None => continue,
                        };
                        let meta = LegacyGenMeta {
                            parent_id: g
                                .get("parent_id")
                                .and_then(|v| v.as_str())
                                .map(str::to_string),
                            created_at: g
                                .get("created_at")
                                .and_then(|v| v.as_str())
                                .filter(|s| !s.is_empty())
                                .map(str::to_string),
                            promoted: g.get("promoted").and_then(|v| v.as_bool()),
                        };
                        lineage_meta.insert((epoch_id.clone(), gid), meta);
                    }
                }
            }
        }
    }

    let mut generations = Vec::new();

    let epoch_entries = match std::fs::read_dir(&paths.epochs) {
        Ok(e) => e,
        Err(_) => return LineageView { generations },
    };
    for epoch_entry in epoch_entries.flatten() {
        if !epoch_entry.path().is_dir() {
            continue;
        }
        let epoch_id = match epoch_entry.file_name().into_string() {
            Ok(s) => s,
            Err(_) => continue,
        };
        let gens_dir = epoch_entry.path().join("generations");
        let gen_entries = match std::fs::read_dir(&gens_dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for gen_entry in gen_entries.flatten() {
            let gen_path = gen_entry.path();
            if !gen_path.is_dir() {
                continue;
            }
            let generation_id = match gen_entry.file_name().into_string() {
                Ok(s) => s,
                Err(_) => continue,
            };

            let legacy = lineage_meta.get(&(epoch_id.clone(), generation_id.clone()));

            // experiment.json — present once the generation has been
            // proposed; absent for the root `v0`.
            let experiment = read_json::<serde_json::Value>(&gen_path.join("experiment.json"));

            let parent_generation_id = experiment
                .as_ref()
                .and_then(|e| {
                    e.get("parent_generation_id")
                        .and_then(|v| v.as_str())
                        .map(str::to_string)
                })
                .or_else(|| legacy.and_then(|m| m.parent_id.clone()));

            // promoted: a recorded decision -> Some(bool); no decision and
            // no experiment metadata at all -> still in flight (None).
            let promoted = experiment
                .as_ref()
                .and_then(experiment_decision)
                .map(|d| decision_to_promoted(&d))
                .or_else(|| legacy.and_then(|m| m.promoted));

            let created_at = experiment
                .as_ref()
                .and_then(|e| {
                    e.get("proposed_at")
                        .or_else(|| e.get("created_at"))
                        .and_then(|v| v.as_str())
                        .filter(|s| !s.is_empty())
                        .map(str::to_string)
                })
                .or_else(|| legacy.and_then(|m| m.created_at.clone()))
                .or_else(|| dir_created_at(&gen_path));

            generations.push(LineageGeneration {
                generation_id,
                epoch_id: epoch_id.clone(),
                parent_generation_id,
                promoted,
                created_at,
            });
        }
    }

    // Stable ordering: epoch, then generation id.
    generations.sort_by(|a, b| {
        a.epoch_id
            .cmp(&b.epoch_id)
            .then_with(|| a.generation_id.cmp(&b.generation_id))
    });
    LineageView { generations }
}

/// The filesystem creation time of `dir` as an RFC-3339 string, when the
/// platform records it. A best-effort last resort for `created_at`.
fn dir_created_at(dir: &Path) -> Option<String> {
    let created = std::fs::metadata(dir).ok()?.created().ok()?;
    let dt: chrono::DateTime<Utc> = created.into();
    Some(dt.to_rfc3339())
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

/// An `active_runs/{run_id}.json` enriched with a computed deadline
/// fraction for the dashboard's per-entry progress bars.
///
/// `progress` is *not* true task progress — that signal does not exist.
/// It is `(now - started_at) / (deadline - started_at)`, clamped to
/// `0.0..=1.0`: a wall-clock-elapsed-vs-budget fraction. `elapsed_seconds`
/// and `budget_seconds` are exposed alongside so the UI can render
/// "01:56 / 15:00" and honestly label the bar "elapsed".
#[derive(Debug, Clone, Serialize)]
pub struct ActiveRunView {
    /// All fields of the on-disk `ActiveRun`, inlined.
    #[serde(flatten)]
    pub run: ActiveRun,
    /// Deadline fraction in `0.0..=1.0`; `null` when `started_at` or
    /// `deadline` is missing so a fraction cannot be computed.
    pub progress: Option<f64>,
    /// Whole seconds elapsed since `started_at` (clamped at 0).
    pub elapsed_seconds: Option<i64>,
    /// Total budget window `deadline - started_at` in whole seconds.
    pub budget_seconds: Option<i64>,
}

/// Compute the elapsed / budget / clamped-fraction triple for one run.
///
/// Pure and `now`-injected so it is unit-testable. Any missing input
/// degrades that field to `None` rather than guessing.
pub fn compute_run_progress(
    run: &ActiveRun,
    now: DateTime<Utc>,
) -> (Option<f64>, Option<i64>, Option<i64>) {
    let started = match run.started_at {
        Some(s) => s,
        None => return (None, None, None),
    };
    let elapsed = (now - started).num_seconds().max(0);
    let deadline = match run.deadline {
        Some(d) => d,
        // No deadline: we can report elapsed but not a fraction/budget.
        None => return (None, Some(elapsed), None),
    };
    let budget = (deadline - started).num_seconds();
    if budget <= 0 {
        // Degenerate window (deadline <= start): treat as fully elapsed.
        return (Some(1.0), Some(elapsed), Some(budget.max(0)));
    }
    let fraction = (elapsed as f64 / budget as f64).clamp(0.0, 1.0);
    (Some(fraction), Some(elapsed), Some(budget))
}

/// `read_active_runs`, enriched with the computed deadline fraction.
/// Backs `GET /api/active-runs`.
pub fn read_active_runs_view(paths: &WorkspacePaths) -> Vec<ActiveRunView> {
    let now = Utc::now();
    read_active_runs(paths)
        .into_iter()
        .map(|run| {
            let (progress, elapsed_seconds, budget_seconds) = compute_run_progress(&run, now);
            ActiveRunView {
                run,
                progress,
                elapsed_seconds,
                budget_seconds,
            }
        })
        .collect()
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
