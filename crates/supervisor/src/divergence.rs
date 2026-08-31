//! Index-vs-canonical divergence auditor.
//!
//! INTEGRITY NOTARY — record #4 (read-only). One audit tick joining the
//! canonical, directory-derived view (`reader::build_lineage_view` +
//! `epoch::build_epoch_view`) against the SQLite analytical index
//! (`index_db`). The index is rebuilt from the canonical files, so the two
//! should always agree; a divergence means a stale/corrupt index, a
//! half-written canonical state, or a mismatch worth surfacing.
//!
//! Flags:
//!
//!  (a) per-generation `promoted` / `parent_generation_id` divergence between
//!      the canonical lineage and the index `generations` row;
//!  (b) epoch `contract_hash` divergence between the epoch config and the
//!      index `epochs` row, plus any contract hash that is non-empty but not
//!      64-hex (a malformed hash);
//!  (c) an in-flight generation whose owning worker pid is dead and whose
//!      decision never resolved past an age threshold (a stuck generation the
//!      orchestrator's reaper missed).
//!
//! A field written as an empty string and a field absent state the same fact,
//! so every side-to-side comparison normalizes empty to absent first
//! (`present`). Only the empty / malformed hash checks read the raw value: a
//! config that names no contract is worth reporting whatever the index holds.
//!
//! UNRESOLVED in-flight generations are SKIPPED for the (a) join: while a
//! generation is mid-tournament the canonical decision is legitimately
//! `None` and the index may not have caught up (an in-flight reindex), so
//! comparing them would be a false positive. Only RESOLVED generations are
//! cross-checked for (a). The (c) check then catches an in-flight generation
//! that is stuck — dead worker, never resolved, past the age threshold.
//!
//! Read-only throughout: this never writes the index or the canonical trees;
//! it only reports. Fail-open: a missing index degrades to "nothing to
//! cross-check" (no findings), never a false alarm.

use crate::index_db::{self, IndexError};
use crate::reader::WorkspacePaths;
use serde::Serialize;
use std::collections::HashMap;

/// The age (seconds) past which an unresolved, dead-worker in-flight
/// generation is reported as stuck (check (c)).
pub const DEFAULT_STUCK_AGE_SECONDS: i64 = 3600;

/// A single divergence finding.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Finding {
    /// A stable machine code for the finding class.
    pub code: &'static str,
    pub epoch_id: String,
    /// The generation this finding is about, when generation-scoped.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub generation_id: Option<String>,
    /// A human-readable description.
    pub detail: String,
}

/// The latest divergence-audit result, surfaced on `/statusz`.
#[derive(Debug, Clone, Default, Serialize)]
pub struct DivergenceView {
    /// `true` once an audit has run.
    pub scanned: bool,
    /// Number of resolved generations cross-checked against the index.
    pub generations_checked: u64,
    /// The findings (empty when canonical and index agree).
    pub findings: Vec<Finding>,
}

/// Whether `h` is a well-formed 64-char lowercase/uppercase hex digest.
fn is_64_hex(h: &str) -> bool {
    h.len() == 64 && h.chars().all(|c| c.is_ascii_hexdigit())
}

/// The value a field states, with an empty string read as absent.
///
/// A field that carries no value has been written two ways: as an empty
/// string by the older writers, and as null by the current ones. Comparing
/// the two spellings directly reports a divergence where both sides state the
/// same fact, so every side-to-side comparison here goes through this first.
/// The Python readers already collapse the two — `zicato.index.elo` and the
/// dashboard's file-tree view each fall back to `""` when the field is absent
/// — so this is the same equivalence, held from the other end.
fn present(value: Option<&str>) -> Option<&str> {
    value.filter(|v| !v.is_empty())
}

/// Cross-check the canonical lineage / epoch config against the index for the
/// current epoch and return the divergence findings.
///
/// Pure-ish: reads the workspace files + the index. `now` and
/// `stuck_age_seconds` are injected so check (c) is deterministic in tests.
/// Read-only and fail-open: a missing/stale index yields the scanned-but-empty
/// view (nothing to cross-check), never a finding.
pub fn audit(
    paths: &WorkspacePaths,
    now: chrono::DateTime<chrono::Utc>,
    stuck_age_seconds: i64,
) -> DivergenceView {
    let epoch_id = match crate::reader::read_current_epoch(paths) {
        Some(e) => e,
        None => return DivergenceView::default(),
    };

    let conn = match index_db::open(&paths.index_db()) {
        Ok(c) => c,
        Err(IndexError::Absent)
        | Err(IndexError::Query(_))
        | Err(IndexError::StaleSchema { .. }) => {
            // No usable index → nothing to cross-check (fail-open).
            return DivergenceView {
                scanned: true,
                ..Default::default()
            };
        }
    };

    let mut findings = Vec::new();

    // ---- (b) contract-hash divergence + malformed hashes ----------------
    // Compared through `present`: a field written as an empty string and a
    // field absent are the same fact, and only their spelling differs.
    let epoch_view = crate::epoch::build_epoch_view(paths);
    let canonical_hash = epoch_view.contract_hash.clone();
    let index_hash = index_db::epoch_contract_hash(&conn, &epoch_id);
    if let (Some(canon), Some(idx)) = (
        present(canonical_hash.as_deref()),
        present(index_hash.as_deref()),
    ) {
        if canon != idx {
            findings.push(Finding {
                code: "contract_hash_divergence",
                epoch_id: epoch_id.clone(),
                generation_id: None,
                detail: format!(
                    "epoch config contract_hash ({canon}) != index contract_hash ({idx})"
                ),
            });
        }
    }
    for (source, hash) in [
        ("epoch config", canonical_hash.as_deref()),
        ("index", index_hash.as_deref()),
    ] {
        if let Some(h) = hash {
            if h.is_empty() {
                findings.push(Finding {
                    code: "empty_contract_hash",
                    epoch_id: epoch_id.clone(),
                    generation_id: None,
                    detail: format!("{source} contract_hash is empty"),
                });
            } else if !is_64_hex(h) {
                findings.push(Finding {
                    code: "malformed_contract_hash",
                    epoch_id: epoch_id.clone(),
                    generation_id: None,
                    detail: format!("{source} contract_hash is not 64-hex: {h:?}"),
                });
            }
        }
    }

    // ---- (a) per-generation promoted / parent divergence ----------------
    // Index generations keyed by id for the join.
    let index_gens: HashMap<String, index_db::GenerationRow> =
        index_db::generations_for_epoch(&conn, &epoch_id)
            .into_iter()
            .filter_map(|g| g.generation_id.clone().map(|id| (id, g)))
            .collect();

    let lineage = crate::reader::build_lineage_view(paths);
    let mut generations_checked = 0u64;
    for gen in &lineage.generations {
        if gen.epoch_id != epoch_id {
            continue;
        }
        // SKIP unresolved in-flight generations for the (a) join: their
        // canonical decision is legitimately None and the index may not have
        // caught up — comparing them is a false positive. The (c) check below
        // catches an in-flight generation that is actually stuck.
        let Some(canon_promoted) = gen.promoted else {
            continue;
        };
        let Some(idx_gen) = index_gens.get(&gen.generation_id) else {
            // The canonical generation resolved but the index has no row for
            // it — a stale index that never ingested this generation.
            findings.push(Finding {
                code: "generation_missing_from_index",
                epoch_id: epoch_id.clone(),
                generation_id: Some(gen.generation_id.clone()),
                detail: "resolved generation has no index row (stale index)".to_string(),
            });
            continue;
        };
        generations_checked += 1;
        if idx_gen.promoted != canon_promoted {
            findings.push(Finding {
                code: "promoted_divergence",
                epoch_id: epoch_id.clone(),
                generation_id: Some(gen.generation_id.clone()),
                detail: format!(
                    "promoted divergence: canonical={canon_promoted}, index={}",
                    idx_gen.promoted
                ),
            });
        }
        // parent_generation_id divergence. Both-absent is agreement, and an
        // empty string counts as absent: the seed generation's parent was once
        // written as "" and is now written as null, so a legacy workspace can
        // hold "" on disk against a null index projection. They state the same
        // fact — this generation has no parent — and are not a divergence.
        let canon_parent = present(gen.parent_generation_id.as_deref());
        let idx_parent = present(idx_gen.parent_generation_id.as_deref());
        if canon_parent != idx_parent {
            findings.push(Finding {
                code: "parent_divergence",
                epoch_id: epoch_id.clone(),
                generation_id: Some(gen.generation_id.clone()),
                detail: format!(
                    "parent divergence: canonical={canon_parent:?}, index={idx_parent:?}"
                ),
            });
        }
    }

    // ---- (c) stuck in-flight generation (dead worker, never resolved) ---
    // An active run names its owning generation + worker pid; an in-flight
    // generation whose worker is dead and whose decision never resolved past
    // the age threshold is stuck.
    let resolved: std::collections::HashSet<&str> = lineage
        .generations
        .iter()
        .filter(|g| g.promoted.is_some())
        .map(|g| g.generation_id.as_str())
        .collect();
    for run in crate::reader::read_active_runs(paths) {
        let Some(generation_id) = run.generation_id.as_deref() else {
            continue;
        };
        // Only an UNRESOLVED generation can be stuck; a resolved one is done.
        if resolved.contains(generation_id) {
            continue;
        }
        // The worker must be confirmably dead (identity-checked against the
        // recorded start time when present) — a slow-but-alive worker is fine.
        let worker_dead = match run.pid {
            Some(pid) => !crate::signal::is_same_process(pid, run.pid_start_time),
            None => false, // no pid recorded → cannot declare it dead.
        };
        if !worker_dead {
            continue;
        }
        // And it must be old enough that a normal resolution would have
        // happened — avoids flagging a worker that just died and whose
        // orchestrator reaper is about to run.
        let age = run
            .started_at
            .map(|s| now.signed_duration_since(s).num_seconds())
            .unwrap_or(0);
        if age < stuck_age_seconds {
            continue;
        }
        findings.push(Finding {
            code: "stuck_in_flight_generation",
            epoch_id: epoch_id.clone(),
            generation_id: Some(generation_id.to_string()),
            detail: format!(
                "in-flight generation's worker pid {:?} is dead and the decision \
                 never resolved ({age}s old, threshold {stuck_age_seconds}s)",
                run.pid
            ),
        });
    }

    DivergenceView {
        scanned: true,
        generations_checked,
        findings,
    }
}

/// The latest divergence-audit result, shared with `/statusz`.
#[derive(Debug, Default)]
pub struct DivergenceFindings {
    inner: std::sync::Mutex<DivergenceView>,
}

impl DivergenceFindings {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn record(&self, view: DivergenceView) {
        let mut g = match self.inner.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        };
        *g = view;
    }

    pub fn view(&self) -> DivergenceView {
        match self.inner.lock() {
            Ok(g) => g.clone(),
            Err(p) => p.into_inner().clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;
    use rusqlite::Connection;
    use tempfile::TempDir;

    fn ws() -> (TempDir, WorkspacePaths) {
        let tmp = TempDir::new().unwrap();
        let ws = tmp.path().to_path_buf();
        std::fs::create_dir_all(ws.join("runtime/active_runs")).unwrap();
        std::fs::create_dir_all(ws.join("epochs/e1/generations")).unwrap();
        let p = WorkspacePaths::new(ws);
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        (tmp, p)
    }

    /// Build an index with an `epochs` row and the given generation rows.
    fn write_index(p: &WorkspacePaths, contract_hash: &str, gens: &[(&str, Option<&str>, i64)]) {
        let conn = Connection::open(p.index_db()).unwrap();
        conn.execute_batch(
            "CREATE TABLE epochs(epoch_id TEXT PRIMARY KEY, contract_hash TEXT, \
                 created_at TEXT, closed INTEGER, goal TEXT, parent_epoch_id TEXT);
             CREATE TABLE generations(epoch_id TEXT, generation_id TEXT, \
                 parent_generation_id TEXT, promoted INTEGER);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO epochs VALUES('e1', ?1, NULL, 0, NULL, NULL)",
            [contract_hash],
        )
        .unwrap();
        for (id, parent, promoted) in gens {
            conn.execute(
                "INSERT INTO generations VALUES('e1', ?1, ?2, ?3)",
                rusqlite::params![id, parent, promoted],
            )
            .unwrap();
        }
        conn.execute_batch(&format!(
            "PRAGMA user_version = {}",
            index_db::EXPECTED_SCHEMA_VERSION
        ))
        .unwrap();
    }

    /// Materialise the canonical side: epoch config contract_hash + a resolved
    /// generation's experiment.json.
    fn write_epoch_config(p: &WorkspacePaths, contract_hash: &str) {
        std::fs::write(
            p.epochs.join("e1").join("config.json"),
            serde_json::json!({"contract_hash": contract_hash}).to_string(),
        )
        .unwrap();
    }

    fn write_canonical_gen(p: &WorkspacePaths, gen: &str, parent: Option<&str>, decision: &str) {
        let dir = p.epochs.join("e1").join("generations").join(gen);
        std::fs::create_dir_all(&dir).unwrap();
        let mut exp = serde_json::json!({"outcome": {"decision": decision}});
        if let Some(parent) = parent {
            exp["parent_generation_id"] = serde_json::json!(parent);
        }
        std::fs::write(dir.join("experiment.json"), exp.to_string()).unwrap();
        std::fs::write(
            p.lineage(),
            serde_json::json!({"epochs": [{"id": "e1", "generations": [{
                "id": gen,
                "parent_id": parent,
                "promoted": decision == "promoted"
            }]}]})
            .to_string(),
        )
        .unwrap();
    }

    const HASH_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const HASH_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    #[test]
    fn no_index_is_fail_open_empty() {
        let (_t, p) = ws();
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        assert!(view.scanned);
        assert!(
            view.findings.is_empty(),
            "no index → nothing to cross-check"
        );
    }

    #[test]
    fn agreeing_canonical_and_index_have_no_findings() {
        let (_t, p) = ws();
        write_epoch_config(&p, HASH_A);
        write_canonical_gen(&p, "v1", Some("v0"), "promoted");
        write_index(&p, HASH_A, &[("v1", Some("v0"), 1)]);
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        assert!(
            view.findings.is_empty(),
            "agreement → no findings: {view:?}"
        );
        assert_eq!(view.generations_checked, 1);
    }

    #[test]
    fn empty_canonical_parent_agrees_with_a_null_index_parent() {
        // The seed generation's parent was once written as an empty string and
        // is now written as null. A workspace carrying the old spelling on disk
        // against an index that projects the new one states one fact twice, so
        // the auditor must report nothing.
        let (_t, p) = ws();
        write_epoch_config(&p, HASH_A);
        write_canonical_gen(&p, "v0", Some(""), "rejected");
        write_index(&p, HASH_A, &[("v0", None, 0)]);
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        assert!(
            !view.findings.iter().any(|f| f.code == "parent_divergence"),
            "empty and absent state the same parent: {view:?}"
        );
        assert_eq!(view.generations_checked, 1);
    }

    #[test]
    fn a_real_parent_disagreeing_with_the_index_is_still_flagged() {
        // The equivalence is between empty and absent only: a parent the two
        // sides genuinely disagree on is still a divergence.
        let (_t, p) = ws();
        write_epoch_config(&p, HASH_A);
        write_canonical_gen(&p, "v2", Some("v1"), "rejected");
        write_index(&p, HASH_A, &[("v2", Some("v0"), 0)]);
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        assert!(view.findings.iter().any(|f| f.code == "parent_divergence"));
    }

    #[test]
    fn an_empty_canonical_contract_hash_is_not_divergence_from_the_index() {
        // Same equivalence for the epoch's contract hash: a config that states
        // no hash has nothing to disagree with the index about. Reporting a
        // mismatch between a hash and no hash names the wrong problem — the
        // empty hash itself, still reported below, is the problem.
        let (_t, p) = ws();
        write_epoch_config(&p, "");
        write_index(&p, HASH_A, &[]);
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        assert!(
            !view
                .findings
                .iter()
                .any(|f| f.code == "contract_hash_divergence"),
            "an empty hash states no contract to diverge from: {view:?}"
        );
        assert!(
            view.findings
                .iter()
                .any(|f| f.code == "empty_contract_hash"),
            "the empty hash itself is still reported: {view:?}"
        );
    }

    #[test]
    fn contract_hash_divergence_is_flagged() {
        let (_t, p) = ws();
        write_epoch_config(&p, HASH_A);
        write_index(&p, HASH_B, &[]);
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        assert!(view
            .findings
            .iter()
            .any(|f| f.code == "contract_hash_divergence"));
    }

    #[test]
    fn malformed_contract_hash_is_flagged() {
        let (_t, p) = ws();
        write_epoch_config(&p, "not-a-real-hash");
        write_index(&p, "not-a-real-hash", &[]);
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        assert!(view
            .findings
            .iter()
            .any(|f| f.code == "malformed_contract_hash"));
        // No divergence finding: both sides carry the same (malformed) value.
        assert!(!view
            .findings
            .iter()
            .any(|f| f.code == "contract_hash_divergence"));
    }

    #[test]
    fn promoted_divergence_is_flagged() {
        let (_t, p) = ws();
        write_epoch_config(&p, HASH_A);
        // Canonical says promoted; index says not.
        write_canonical_gen(&p, "v1", Some("v0"), "promoted");
        write_index(&p, HASH_A, &[("v1", Some("v0"), 0)]);
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        let f = view
            .findings
            .iter()
            .find(|f| f.code == "promoted_divergence")
            .expect("promoted divergence");
        assert_eq!(f.generation_id.as_deref(), Some("v1"));
    }

    #[test]
    fn parent_divergence_is_flagged() {
        let (_t, p) = ws();
        write_epoch_config(&p, HASH_A);
        write_canonical_gen(&p, "v1", Some("v0"), "promoted");
        // Index records a different parent.
        write_index(&p, HASH_A, &[("v1", Some("vX"), 1)]);
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        assert!(view.findings.iter().any(|f| f.code == "parent_divergence"));
    }

    #[test]
    fn unresolved_in_flight_generation_is_not_a_join_finding() {
        // An in-flight generation (no decision yet) must NOT be cross-checked
        // for promoted/parent divergence — that would be an in-flight-reindex
        // false positive.
        let (_t, p) = ws();
        write_epoch_config(&p, HASH_A);
        // Canonical generation directory exists but its experiment has NO
        // outcome → in flight.
        let dir = p.epochs.join("e1").join("generations").join("v1");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("experiment.json"),
            serde_json::json!({"parent_generation_id": "v0"}).to_string(),
        )
        .unwrap();
        // Index disagrees on parent — but the generation is in flight, so the
        // (a) join must skip it.
        write_index(&p, HASH_A, &[("v1", Some("vX"), 0)]);
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        assert!(
            !view.findings.iter().any(|f| f.code == "parent_divergence"),
            "in-flight generation must not produce a join finding: {view:?}",
        );
        assert_eq!(view.generations_checked, 0);
    }

    #[test]
    fn stuck_in_flight_generation_with_dead_worker_is_flagged() {
        let (_t, p) = ws();
        write_epoch_config(&p, HASH_A);
        write_index(&p, HASH_A, &[]);
        // An active run for an unresolved generation whose worker pid is dead
        // (a huge unused pid) and old enough to be past the threshold.
        let started = Utc::now() - chrono::Duration::seconds(7200);
        std::fs::write(
            p.active_runs_dir().join("run-stuck.json"),
            serde_json::json!({
                "run_id": "run-stuck",
                "generation_id": "v9",
                "pid": 99_999_999,
                "started_at": started,
            })
            .to_string(),
        )
        .unwrap();
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        let f = view
            .findings
            .iter()
            .find(|f| f.code == "stuck_in_flight_generation")
            .expect("stuck generation");
        assert_eq!(f.generation_id.as_deref(), Some("v9"));
    }

    #[test]
    fn recently_started_dead_worker_is_not_yet_stuck() {
        // The same dead worker but only just started → below the age
        // threshold → not flagged (the reaper may still be about to run).
        let (_t, p) = ws();
        write_epoch_config(&p, HASH_A);
        write_index(&p, HASH_A, &[]);
        let started = Utc::now() - chrono::Duration::seconds(10);
        std::fs::write(
            p.active_runs_dir().join("run-fresh.json"),
            serde_json::json!({
                "run_id": "run-fresh",
                "generation_id": "v9",
                "pid": 99_999_999,
                "started_at": started,
            })
            .to_string(),
        )
        .unwrap();
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        assert!(!view
            .findings
            .iter()
            .any(|f| f.code == "stuck_in_flight_generation"));
    }

    #[test]
    fn live_worker_in_flight_generation_is_not_flagged() {
        // A live worker (our own pid) for an in-flight generation is NOT stuck,
        // no matter how old — only a dead worker is.
        let (_t, p) = ws();
        write_epoch_config(&p, HASH_A);
        write_index(&p, HASH_A, &[]);
        let started = Utc::now() - chrono::Duration::seconds(7200);
        std::fs::write(
            p.active_runs_dir().join("run-live.json"),
            serde_json::json!({
                "run_id": "run-live",
                "generation_id": "v9",
                "pid": std::process::id(),
                "started_at": started,
            })
            .to_string(),
        )
        .unwrap();
        let view = audit(&p, Utc::now(), DEFAULT_STUCK_AGE_SECONDS);
        assert!(!view
            .findings
            .iter()
            .any(|f| f.code == "stuck_in_flight_generation"));
    }

    #[test]
    fn findings_store_round_trips() {
        let store = DivergenceFindings::new();
        assert!(!store.view().scanned);
        store.record(DivergenceView {
            scanned: true,
            generations_checked: 4,
            findings: vec![],
        });
        assert!(store.view().scanned);
        assert_eq!(store.view().generations_checked, 4);
    }
}
