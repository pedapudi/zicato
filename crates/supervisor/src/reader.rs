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

    /// Stable inode shared with the Python invocation writer lease.
    pub fn lock_guard(&self) -> PathBuf {
        self.runtime.join("lock.guard")
    }

    pub fn active_runs_dir(&self) -> PathBuf {
        self.runtime.join("active_runs")
    }

    /// The active-tournament EVENT LOG (RUNTIME-V2 Phase 3): an
    /// append-only JSONL the orchestrator/runner publish live state onto.
    /// `read_active_tournament` folds it into the live view.
    pub fn active_tournament_log(&self) -> PathBuf {
        self.runtime.join("active_tournament.events.jsonl")
    }

    pub fn control_dir(&self) -> PathBuf {
        self.runtime.join("control")
    }

    /// Directory holding parent→supervisor kill-escalation requests. The
    /// Python parent writes `control/kill_requests/{run_id}` when a worker
    /// overran its budget; this supervisor is the single SIGTERM→grace→
    /// SIGKILL escalator that acts on them. Distinct from the operator's
    /// `control/kill_runs/` channel (consumed by the orchestrator).
    pub fn kill_requests_dir(&self) -> PathBuf {
        self.control_dir().join("kill_requests")
    }

    pub fn current_epoch_marker(&self) -> PathBuf {
        self.workspace.join("current_epoch")
    }

    pub fn lineage(&self) -> PathBuf {
        self.workspace.join("lineage.json")
    }

    /// SQLite analytical index built by `zicato repair index`
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

/// Fold the active-tournament event log into the live view.
///
/// The log is single-writer append-only JSONL: a full-envelope `Snapshot`
/// event (the authoritative base/reset) plus `EntryUpdate` /
/// `PartialAggregate` / `ProjectedUpdate` deltas. We fold from the LAST
/// `Snapshot` forward, applying the deltas that affect this view's
/// (coarse) fields — entry transitions + the partial aggregate — so the
/// supervisor's tournament panel reflects the recorded transitions.
/// A missing, empty or unusable log yields `None`.
pub fn read_active_tournament(paths: &WorkspacePaths) -> Option<ActiveTournament> {
    read_active_tournament_with_stats(paths).0
}

/// `read_active_tournament`, additionally returning the [`FoldStats`]
/// gathered while folding the event log (torn-write parse failures +
/// non-monotonic-`seq` gaps). The supervisor accumulates these into the
/// shared [`crate::fold_stats::FoldDiagnostics`] for `/statusz`; callers
/// that do not care can use the thin [`read_active_tournament`] wrapper.
pub fn read_active_tournament_with_stats(
    paths: &WorkspacePaths,
) -> (Option<ActiveTournament>, crate::fold_stats::FoldStats) {
    let (value, stats) = fold_active_tournament_value_with_stats(&paths.active_tournament_log());
    let tournament = value.and_then(|value| serde_json::from_value(value).ok());
    (tournament, stats)
}

/// Replay the event log into the folded envelope JSON `Value` (or `None`
/// when the log is absent/empty, so the caller can fall back to the
/// snapshot), tallying torn-write parse failures and non-monotonic-`seq`
/// gaps over the canonical fold.
///
/// Operates on raw JSON so the coarse `ActiveTournament` struct need not
/// model every delta field — the fold matches `(entry_id, side)` on the raw
/// rows exactly as the Python writer did. The lenient behavior is preserved
/// exactly — a bad line is still skipped, a republished snapshot still
/// resets the fold — but each skipped/torn line now increments
/// `parse_failures` and each `seq` that is not exactly one past its
/// predecessor increments `seq_gaps`, so the conditions the fold otherwise
/// hides become visible on `/statusz`.
fn fold_active_tournament_value_with_stats(
    log_path: &Path,
) -> (Option<serde_json::Value>, crate::fold_stats::FoldStats) {
    let mut stats = crate::fold_stats::FoldStats::default();
    let text = match std::fs::read_to_string(log_path) {
        Ok(t) => t,
        Err(_) => return (None, stats),
    };
    // Each line is one event record: {seq, ts, type, payload}. Parse every
    // non-blank line; count (not silently drop) the ones that fail.
    let mut events: Vec<serde_json::Value> = Vec::new();
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        match serde_json::from_str::<serde_json::Value>(line) {
            Ok(v) => events.push(v),
            Err(_) => stats.parse_failures += 1,
        }
    }

    // Count non-monotonic seq across the successfully-parsed events: each
    // event's `seq` should be exactly one past the previous one. A gap or a
    // backwards step (lost events / out-of-order republish) is tallied. The
    // first event of the log is never a gap. Events without a `seq` field do
    // not advance the cursor (they cannot be judged monotonic).
    let mut prev_seq: Option<i64> = None;
    for ev in &events {
        if let Some(seq) = ev.get("seq").and_then(|s| s.as_i64()) {
            if let Some(prev) = prev_seq {
                if seq != prev + 1 {
                    stats.seq_gaps += 1;
                }
            }
            prev_seq = Some(seq);
        }
    }

    if events.is_empty() {
        return (None, stats);
    }
    // Fold from the last Snapshot (a Snapshot is the authoritative reset).
    let base_idx = match events
        .iter()
        .rposition(|e| e.get("type").and_then(|t| t.as_str()) == Some("Snapshot"))
    {
        Some(i) => i,
        None => return (None, stats),
    };
    let mut current = match events[base_idx].get("payload") {
        Some(p) => p.clone(),
        None => return (None, stats),
    };
    for ev in &events[base_idx + 1..] {
        let ty = ev.get("type").and_then(|t| t.as_str()).unwrap_or("");
        let payload = match ev.get("payload") {
            Some(p) => p,
            None => continue,
        };
        match ty {
            "Snapshot" => current = payload.clone(),
            "Update" => {
                if let (Some(fields), Some(view)) = (
                    payload.get("fields").and_then(|v| v.as_object()),
                    current.as_object_mut(),
                ) {
                    view.extend(
                        fields
                            .iter()
                            .map(|(key, value)| (key.clone(), value.clone())),
                    );
                }
                if let (Some(updates), Some(entries)) = (
                    payload.get("entries").and_then(|v| v.as_object()),
                    current.get_mut("entries").and_then(|v| v.as_array_mut()),
                ) {
                    for (index, entry) in updates {
                        if let Some(row) =
                            index.parse::<usize>().ok().and_then(|i| entries.get_mut(i))
                        {
                            *row = entry.clone();
                        }
                    }
                }
            }
            _ => {}
        }
    }
    (Some(current), stats)
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

/// Parent relationships and birth metadata recorded before evaluation.
#[derive(Debug, Clone, Default)]
struct AncestryMetadata {
    parent_id: Option<String>,
    created_at: Option<String>,
    promoted: Option<bool>,
}

/// Read a committed outcome only when it names this proposal and round.
fn committed_outcome(
    paths: &WorkspacePaths,
    epoch_id: &str,
    generation_id: &str,
    experiment: &serde_json::Value,
) -> Option<serde_json::Value> {
    let round = experiment.get("round_index")?.as_u64()?;
    let experiment_id = experiment.get("id")?.as_str()?;
    let path = paths
        .workspace
        .join("epochs")
        .join(epoch_id)
        .join("rounds")
        .join(round.to_string())
        .join("field_settlement.json");
    let receipt: serde_json::Value = read_json(&path)?;
    if receipt.get("state")?.as_str()? != "committed"
        || receipt.get("epoch_id")?.as_str()? != epoch_id
        || receipt.get("round_index")?.as_u64()? != round
    {
        return None;
    }
    receipt
        .get("candidates")?
        .as_array()?
        .iter()
        .find(|candidate| {
            candidate.get("generation_id").and_then(|v| v.as_str()) == Some(generation_id)
                && candidate.get("experiment_id").and_then(|v| v.as_str()) == Some(experiment_id)
        })?
        .get("outcome")
        .cloned()
}

pub fn build_lineage_view(paths: &WorkspacePaths) -> LineageView {
    use std::collections::HashMap;

    // Ancestry supplies parents and birth metadata; rounds supply decisions.
    let mut lineage_meta: HashMap<(String, String), AncestryMetadata> = HashMap::new();
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
                        let meta = AncestryMetadata {
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

            let ancestry = lineage_meta.get(&(epoch_id.clone(), generation_id.clone()));

            // experiment.json — present once the generation has been
            // proposed; absent for the root `v0`.
            let experiment = read_json::<serde_json::Value>(&gen_path.join("experiment.json"));

            let parent_generation_id = ancestry.and_then(|m| m.parent_id.clone());
            let promoted = experiment
                .as_ref()
                .and_then(|e| committed_outcome(paths, &epoch_id, &generation_id, e))
                .and_then(|outcome| {
                    outcome
                        .get("tournament_decision")
                        .and_then(|v| v.as_str())
                        .map(|decision| decision == "promoted")
                })
                .or_else(|| ancestry.and_then(|m| m.promoted));

            let created_at = experiment
                .as_ref()
                .and_then(|e| {
                    e.get("proposed_at")
                        .or_else(|| e.get("created_at"))
                        .and_then(|v| v.as_str())
                        .filter(|s| !s.is_empty())
                        .map(str::to_string)
                })
                .or_else(|| ancestry.and_then(|m| m.created_at.clone()))
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

/// Read the set of run ids with a pending parent→supervisor kill request.
///
/// Each `control/kill_requests/{run_id}` marker (file basename = run id,
/// no extension) means the Python parent asked this supervisor to
/// escalate-kill that run's worker. A missing directory is the common
/// case (no kills requested) and yields an empty set, never an error.
pub fn read_kill_requests(paths: &WorkspacePaths) -> std::collections::HashSet<String> {
    let dir = paths.kill_requests_dir();
    let entries = match std::fs::read_dir(&dir) {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return std::collections::HashSet::new()
        }
        Err(e) => {
            warn!(?dir, error=%e, "failed to list kill_requests");
            return std::collections::HashSet::new();
        }
    };
    let mut out = std::collections::HashSet::new();
    for entry in entries.flatten() {
        let path = entry.path();
        // Skip any partial-write temp file the atomic writer may leave.
        if path.extension().and_then(|s| s.to_str()) == Some("tmp") {
            continue;
        }
        if let Some(name) = path.file_name().and_then(|s| s.to_str()) {
            out.insert(name.to_string());
        }
    }
    out
}

/// Remove a consumed kill-request marker. Best-effort: a vanished marker
/// (the parent's cleanup beat us, or a double tick) is not an error.
pub fn clear_kill_request(paths: &WorkspacePaths, run_id: &str) {
    let path = paths.kill_requests_dir().join(run_id);
    if let Err(e) = std::fs::remove_file(&path) {
        if e.kind() != std::io::ErrorKind::NotFound {
            warn!(?path, error=%e, "failed to clear kill_request marker");
        }
    }
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
    fn active_run_parses_python_pid_start_time_float() {
        // Cross-language contract: the Python worker serializes the pid
        // start time as a JSON float (the /proc tick count, e.g.
        // `116371304.0`). The Rust `ActiveRun.pid_start_time` must accept
        // that shape; a record without the field stays `None` (ancestry).
        let (_t, p) = make_ws();
        let dir = p.active_runs_dir();
        std::fs::write(
            dir.join("withstart.json"),
            r#"{"run_id":"withstart","pid":42,"pid_start_time":116371304.0}"#,
        )
        .unwrap();
        std::fs::write(
            dir.join("ancestry.json"),
            r#"{"run_id":"ancestry","pid":7}"#,
        )
        .unwrap();
        let runs = read_active_runs(&p);
        assert_eq!(runs.len(), 2);
        // ancestry (no field) → None
        assert_eq!(runs[0].run_id, "ancestry");
        assert_eq!(runs[0].pid_start_time, None);
        // withstart → the float parses through intact
        assert_eq!(runs[1].run_id, "withstart");
        assert_eq!(runs[1].pid_start_time, Some(116_371_304.0));
    }

    #[test]
    fn kill_requests_missing_dir_is_empty() {
        let (_t, p) = make_ws();
        assert!(read_kill_requests(&p).is_empty());
    }

    #[test]
    fn kill_requests_are_collected_by_run_id() {
        let (_t, p) = make_ws();
        let dir = p.kill_requests_dir();
        std::fs::create_dir_all(&dir).unwrap();
        // Markers are named by bare run id (no extension); a partial-write
        // .tmp file is ignored.
        std::fs::write(dir.join("run_a"), r#"{"run_id":"run_a"}"#).unwrap();
        std::fs::write(dir.join("run_b"), r#"{"run_id":"run_b"}"#).unwrap();
        std::fs::write(dir.join("run_c.tmp"), "partial").unwrap();
        let reqs = read_kill_requests(&p);
        assert_eq!(reqs.len(), 2);
        assert!(reqs.contains("run_a"));
        assert!(reqs.contains("run_b"));
        assert!(!reqs.contains("run_c.tmp"));
    }

    #[test]
    fn clear_kill_request_removes_the_marker() {
        let (_t, p) = make_ws();
        let dir = p.kill_requests_dir();
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("run_a"), r#"{"run_id":"run_a"}"#).unwrap();
        assert!(read_kill_requests(&p).contains("run_a"));
        clear_kill_request(&p, "run_a");
        assert!(!read_kill_requests(&p).contains("run_a"));
        // Clearing a vanished marker is a no-op, not an error.
        clear_kill_request(&p, "run_a");
    }

    #[test]
    fn folds_the_active_tournament_event_log() {
        // Published updates replace fields without recalculating display values.
        let (_t, p) = make_ws();
        let log = [
            r#"{"seq":1,"ts":"t","type":"Snapshot","payload":{"tournament_id":"t1","entries":[{"entry_id":"b0","side":"child","status":"queued"},{"entry_id":"b0","side":"parent","status":"queued"}]}}"#,
            r#"{"seq":2,"ts":"t","type":"Update","payload":{"fields":{},"entries":{"0":{"entry_id":"b0","side":"child","status":"running"},"1":{"entry_id":"b0","side":"parent","status":"queued"}}}}"#,
            r#"{"seq":3,"ts":"t","type":"Update","payload":{"fields":{"partial_challenger_agg":{"scalar":0.5},"standings":[{"generation_id":"v1","rank":1}],"projected":{"v1":{"scalar":0.25}},"gen_states":[{"generation_id":"v1","eliminated_at_round":null,"lost_rounds":[0]}]},"entries":{}}}"#,
        ]
        .join("\n");
        std::fs::write(p.active_tournament_log(), log).unwrap();

        let at = read_active_tournament(&p).expect("the folded tournament");
        assert_eq!(at.tournament_id.as_deref(), Some("t1"));
        let child = at.entries.iter().find(|e| e.entry_id == "b0").unwrap();
        assert_eq!(child.status.as_deref(), Some("running"));
        let published = serde_json::to_value(&at).unwrap();
        assert_eq!(published["standings"][0]["generation_id"], "v1");
        assert_eq!(published["projected"]["v1"]["scalar"], 0.25);
        assert_eq!(
            published["gen_states"][0]["lost_rounds"],
            serde_json::json!([0])
        );
        assert!(published["gen_states"][0]["eliminated_at_round"].is_null());
    }

    #[test]
    fn last_snapshot_event_resets_the_fold() {
        // A later Snapshot supersedes the earlier base (a republish).
        let (_t, p) = make_ws();
        let log = [
            r#"{"seq":1,"ts":"t","type":"Snapshot","payload":{"tournament_id":"old","entries":[]}}"#,
            r#"{"seq":2,"ts":"t","type":"Snapshot","payload":{"tournament_id":"new","entries":[]}}"#,
        ]
        .join("\n");
        std::fs::write(p.active_tournament_log(), log).unwrap();
        let at = read_active_tournament(&p).expect("the folded tournament");
        assert_eq!(at.tournament_id.as_deref(), Some("new"));
    }

    #[test]
    fn absent_log_ignores_saved_snapshot() {
        let (_t, p) = make_ws();
        let path = p.runtime.join("active_tournament.json");
        let saved = r#"{"tournament_id":"saved","entries":[]}"#;
        std::fs::write(&path, saved).unwrap();
        let (at, stats) = read_active_tournament_with_stats(&p);
        assert!(at.is_none());
        assert_eq!(stats, crate::fold_stats::FoldStats::default());
        assert_eq!(std::fs::read_to_string(path).unwrap(), saved);
    }

    #[test]
    fn snapshot_assembles_full_payload() {
        let (_t, p) = make_ws();
        std::fs::write(p.heartbeat(), r#"{"pid":1,"phase":"running"}"#).unwrap();
        std::fs::write(
            p.active_tournament_log(),
            r#"{"seq":1,"ts":"t","type":"Snapshot","payload":{"tournament_id":"t1","entries":[]}}"#,
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

    // ---- fold diagnostics (torn writes + non-monotonic seq) --------

    #[test]
    fn clean_log_reports_zero_fold_diagnostics() {
        let (_t, p) = make_ws();
        let log = [
            r#"{"seq":1,"ts":"t","type":"Snapshot","payload":{"tournament_id":"t1","entries":[]}}"#,
            r#"{"seq":2,"ts":"t","type":"Update","payload":{"fields":{},"entries":{}}}"#,
            r#"{"seq":3,"ts":"t","type":"Update","payload":{"fields":{"partial_challenger_agg":{"scalar":0.5}},"entries":{}}}"#,
        ]
        .join("\n");
        std::fs::write(p.active_tournament_log(), log).unwrap();
        let (at, stats) = read_active_tournament_with_stats(&p);
        assert!(at.is_some());
        assert_eq!(stats.parse_failures, 0);
        assert_eq!(stats.seq_gaps, 0);
    }

    #[test]
    fn torn_write_lines_are_counted_not_just_dropped() {
        // Two corrupt (un-parseable) lines interleaved with good events. The
        // fold still succeeds on the good lines (lenient), but the torn
        // writes are now COUNTED rather than silently dropped.
        let (_t, p) = make_ws();
        let log = [
            r#"{"seq":1,"ts":"t","type":"Snapshot","payload":{"tournament_id":"t1","entries":[]}}"#,
            r#"{"seq":2,"ts":"t","type":"EntryUp"#, // torn mid-line
            r#"not json at all"#,                   // garbage
            r#"{"seq":3,"ts":"t","type":"Update","payload":{"fields":{"partial_challenger_agg":{"scalar":0.9}},"entries":{}}}"#,
        ]
        .join("\n");
        std::fs::write(p.active_tournament_log(), log).unwrap();
        let (at, stats) = read_active_tournament_with_stats(&p);
        // The good Snapshot still folds through.
        assert_eq!(
            at.expect("good lines still fold").tournament_id.as_deref(),
            Some("t1")
        );
        assert_eq!(stats.parse_failures, 2);
        // seq jumped 1 -> 3 across the two dropped lines: one gap.
        assert_eq!(stats.seq_gaps, 1);
    }

    #[test]
    fn non_monotonic_seq_is_counted() {
        // seq goes 1, 2, 5, 3 — a forward gap (2->5) and a backward step
        // (5->3) are each a non-monotonic event: two gaps.
        let (_t, p) = make_ws();
        let log = [
            r#"{"seq":1,"ts":"t","type":"Snapshot","payload":{"tournament_id":"t1","entries":[]}}"#,
            r#"{"seq":2,"ts":"t","type":"Update","payload":{"fields":{},"entries":{}}}"#,
            r#"{"seq":5,"ts":"t","type":"Update","payload":{"fields":{},"entries":{}}}"#,
            r#"{"seq":3,"ts":"t","type":"Update","payload":{"fields":{},"entries":{}}}"#,
        ]
        .join("\n");
        std::fs::write(p.active_tournament_log(), log).unwrap();
        let (_at, stats) = read_active_tournament_with_stats(&p);
        assert_eq!(stats.parse_failures, 0);
        assert_eq!(stats.seq_gaps, 2);
    }
}
