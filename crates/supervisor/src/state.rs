//! Strongly-typed wire structs for the runtime state files.
//!
//! All fields are `#[serde(default)]` so the supervisor tolerates the
//! Python side adding new fields or renaming optional ones without
//! crashing. The Python writer is the source of truth for shapes; this
//! file mirrors the field names used by the matching dataclasses.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// `.zicato/runtime/lock.json`
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Lock {
    #[serde(default)]
    pub pid: Option<i32>,
    #[serde(default)]
    pub instance_id: Option<String>,
    #[serde(default)]
    pub started_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub workspace: Option<String>,
}

/// `.zicato/runtime/heartbeat.json`
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Heartbeat {
    #[serde(default)]
    pub pid: Option<i32>,
    #[serde(default)]
    pub instance_id: Option<String>,
    #[serde(default)]
    pub last_heartbeat: Option<DateTime<Utc>>,
    /// When the orchestrator process started. Used by `/statusz` to report
    /// the orchestrator's own uptime; absent in older writers.
    #[serde(default)]
    pub started_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub phase: Option<String>,
    #[serde(default)]
    pub epoch_id: Option<String>,
    #[serde(default)]
    pub generation_id: Option<String>,
    #[serde(default)]
    pub round: Option<u64>,
    /// The orchestrator's progress cursor (RUNTIME-V2 Phase 4): the tail
    /// `seq` of the progress event log, stamped here only at a genuine
    /// transition. The periodic heartbeat-timer bump RE-WRITES the same
    /// value, so `seq` advances iff the loop actually made progress — it
    /// does not move on the timer alone. That makes seq-change age a truer
    /// liveness signal than the heartbeat timestamp (which a healthy timer
    /// keeps fresh even when the loop is wedged). Absent in heartbeats
    /// written before Phase 4 → the watchdog falls back to timestamp age.
    #[serde(default)]
    pub seq: Option<u64>,
}

/// `.zicato/runtime/active_runs/{run_id}.json`
///
/// Each board-entry run executes in its own subprocess worker. The worker
/// writes this file with `pid` = the *worker's own* pid (not the
/// orchestrator's), so signalling `pid` affects exactly one run. `deadline`
/// is `started_at + wall_clock_budget_seconds` (ISO-8601 UTC).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ActiveRun {
    #[serde(default)]
    pub run_id: String,
    #[serde(default)]
    pub pid: Option<i32>,
    /// The worker process's start time (Linux `/proc/<pid>/stat` field 22),
    /// recorded by the worker when it writes this record. Paired with `pid`
    /// it defeats pid reuse: the watchdog only signals a pid whose start
    /// time still matches, so a recycled pid (the worker died and the kernel
    /// reissued its number to an unrelated process) is never mis-targeted.
    /// Absent for legacy writers → the watchdog degrades to bare liveness.
    /// Carried as `f64` to match the Python writer (which serializes the
    /// `/proc` tick count as a float, e.g. `116371304.0`); the values are
    /// integer-valued so equality comparison is exact.
    #[serde(default)]
    pub pid_start_time: Option<f64>,
    /// OS process-group id of the run's own worker. The worker is spawned in
    /// its own session/process-group (`start_new_session`), so it is the
    /// group leader and `pgid == pid`. Recording it lets the watchdog
    /// GROUP-kill the worker AND any grandchildren the system under test spawned
    /// (shells, helper tools) by negating this id, rather than leaking them
    /// when it kills the leader pid alone. `None` for a legacy record (or a
    /// platform without process groups) → the watchdog falls back to the
    /// single-pid kill.
    #[serde(default)]
    pub pgid: Option<i32>,
    /// Absolute path to the run's ephemeral snapshot working copy (the
    /// `ztw-snap-*` temp directory the runner copytrees the code snapshot
    /// into for this run). Discarded by the runner on a clean run-end, but
    /// orphaned if the orchestrator dies mid-run; recording it here lets the
    /// supervisor GC the leftover `ztw-snap-*` tree after a confirmed
    /// orchestrator death. `None` for a legacy record or a run that mounted
    /// no ephemeral snapshot.
    #[serde(default)]
    pub snapshot_path: Option<String>,
    #[serde(default)]
    pub entry_id: Option<String>,
    #[serde(default)]
    pub generation_id: Option<String>,
    #[serde(default)]
    pub epoch_id: Option<String>,
    #[serde(default)]
    pub started_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub last_progress: Option<DateTime<Utc>>,
    #[serde(default)]
    pub deadline: Option<DateTime<Utc>>,
    /// Per-board-entry wall-clock budget in seconds (informational; the
    /// authoritative cutoff is `deadline`).
    #[serde(default)]
    pub wall_clock_budget_seconds: Option<f64>,
    #[serde(default)]
    pub events_jsonl_path: Option<String>,
    #[serde(default)]
    pub phase: Option<String>,
    /// Worker-reported task progress, when the worker chooses to write
    /// one. Distinct from the supervisor's computed deadline fraction
    /// (`ActiveRunView::progress`); serialized as `reported_progress` so
    /// the two never collide in `/api/active-runs`. The worker may write
    /// it under either key.
    #[serde(default, rename = "reported_progress", alias = "progress")]
    pub reported_progress: Option<f64>,
    #[serde(default)]
    pub message: Option<String>,
}

/// `.zicato/runtime/active_tournament.json`
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ActiveTournament {
    #[serde(default)]
    pub tournament_id: Option<String>,
    #[serde(default)]
    pub generation_id: Option<String>,
    #[serde(default)]
    pub parent_generation_id: Option<String>,
    #[serde(default)]
    pub round: Option<u64>,
    #[serde(default)]
    pub started_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub entries: Vec<TournamentEntry>,
    #[serde(default)]
    pub gate: Option<serde_json::Value>,
    #[serde(default)]
    pub partial_aggregate: Option<serde_json::Value>,
    #[serde(default)]
    pub predicted_verdict: Option<String>,
    /// The tournament structure token (`"single_elim"` / `"double_elim"` /
    /// `"swiss"` / `"racing"` / `"gauntlet"`). Read so the elim fold
    /// (`crate::elim_states`) knows when to attach the served elim model;
    /// omitted from the payload when the producer never wrote it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub structure: Option<String>,
    /// The raw published `rounds[]` blob, passed through opaquely. For an
    /// elim structure it is replaced by the CANONICALIZED copy (sorted /
    /// deduped / `bracket_side`+`loser`-stamped) before serving — the
    /// Rust half of the Python `attach_elim_states` wiring.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rounds: Option<serde_json::Value>,
    /// The DERIVED per-generation elim states (`crate::elim_states`) —
    /// never read from disk, always recomputed from `rounds` at serve
    /// time, so a producer cannot ship a stale fold.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub gen_states: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct TournamentEntry {
    #[serde(default)]
    pub entry_id: String,
    #[serde(default)]
    pub patch_id: Option<String>,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub score: Option<f64>,
    #[serde(default)]
    pub run_id: Option<String>,
}

/// Lineage view emitted by the Python side (compatible with the existing
/// `lineage.json` cross-cutting DAG in `epochs/`).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Lineage {
    #[serde(default)]
    pub generations: Vec<LineageNode>,
    #[serde(default)]
    pub edges: Vec<LineageEdge>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct LineageNode {
    #[serde(default)]
    pub generation_id: String,
    #[serde(default)]
    pub parent_id: Option<String>,
    #[serde(default)]
    pub epoch_id: Option<String>,
    #[serde(default)]
    pub round: Option<u64>,
    #[serde(default)]
    pub created_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub score: Option<f64>,
    #[serde(default, flatten)]
    pub extra: BTreeMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct LineageEdge {
    #[serde(default)]
    pub from: String,
    #[serde(default)]
    pub to: String,
    #[serde(default)]
    pub kind: Option<String>,
}

/// Composite snapshot returned by `/api/state`.
#[derive(Debug, Clone, Default, Serialize)]
pub struct Snapshot {
    pub heartbeat: Option<Heartbeat>,
    pub lock: Option<Lock>,
    pub active_runs: Vec<ActiveRun>,
    pub active_tournament: Option<ActiveTournament>,
    pub lineage: Option<Lineage>,
    pub epoch_id: Option<String>,
    /// The current epoch's full definition (board, proposer brief,
    /// scoring, registered harness, mutation surface). Embedded here so the UI
    /// gets it in the initial snapshot without a second fetch; the same
    /// object is served standalone by `/api/epoch`.
    pub epoch: crate::epoch::EpochView,
    pub generated_at: DateTime<Utc>,
}
