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
    #[serde(default)]
    pub phase: Option<String>,
    #[serde(default)]
    pub epoch_id: Option<String>,
    #[serde(default)]
    pub generation_id: Option<String>,
    #[serde(default)]
    pub round: Option<u64>,
}

/// `.zicato/runtime/active_runs/{run_id}.json`
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ActiveRun {
    #[serde(default)]
    pub run_id: String,
    #[serde(default)]
    pub pid: Option<i32>,
    #[serde(default)]
    pub entry_id: Option<String>,
    #[serde(default)]
    pub generation_id: Option<String>,
    #[serde(default)]
    pub started_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub last_progress: Option<DateTime<Utc>>,
    #[serde(default)]
    pub deadline: Option<DateTime<Utc>>,
    #[serde(default)]
    pub phase: Option<String>,
    #[serde(default)]
    pub progress: Option<f64>,
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
    pub generated_at: DateTime<Utc>,
}
