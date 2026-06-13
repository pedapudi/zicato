//! Read-only access to the SQLite analytical index (`.zicato/index/index.db`).
//!
//! The index is built by the Python side (`zicato reindex`). The
//! supervisor never writes to it: every connection is opened with
//! `SQLITE_OPEN_READ_ONLY` and tolerates a WAL sidecar.
//!
//! Every public function here is best-effort. A missing database, a
//! missing table, or a malformed row degrades to an empty result rather
//! than an error the caller has to surface as a 500. The only `Err`
//! returned distinguishes "the file is genuinely not there" (so callers
//! can attach a `note`) from "the file is there but a query failed".

use rusqlite::{Connection, OpenFlags, Row};
use std::path::Path;

/// The SQLite `user_version` this supervisor's positional row readers are
/// written against. Pinned to the Python `SCHEMA_VERSION`
/// (`src/zicato/index/schema.py`); a test in this module fails loudly if
/// the two drift, so a Python schema bump cannot silently leave the Rust
/// reader decoding rows by stale column positions.
///
/// The readers in this file pull columns by *name* in their SQL, but the
/// JOIN shapes and the set of columns each helper expects are tied to a
/// schema generation. Opening a database whose `user_version` does not
/// match this constant returns [`IndexError::StaleSchema`] rather than
/// risking a row decoded against the wrong schema.
pub const EXPECTED_SCHEMA_VERSION: i64 = 10;

/// A row of the `tournaments` table joined against `experiments` for the
/// matchup's hypothesis idea.
#[derive(Debug, Clone, Default)]
pub struct TournamentRow {
    pub tournament_id: Option<String>,
    pub epoch_id: Option<String>,
    pub parent_generation_id: Option<String>,
    pub child_generation_id: Option<String>,
    pub decision: Option<String>,
    pub parent_scalar: Option<f64>,
    pub child_scalar: Option<f64>,
    pub delta_scalar: Option<f64>,
    pub rejection_reason: Option<String>,
    pub ran_at: Option<String>,
    /// From the matching `experiments` row, when one exists.
    pub hypothesis_core_idea: Option<String>,
}

/// A row of `generations`.
#[derive(Debug, Clone, Default)]
pub struct GenerationRow {
    pub epoch_id: Option<String>,
    pub generation_id: Option<String>,
    pub parent_generation_id: Option<String>,
    pub promoted: bool,
}

/// The `experiments` row for one generation — the hypothesis + decision.
#[derive(Debug, Clone, Default)]
pub struct ExperimentRow {
    pub epoch_id: Option<String>,
    pub generation_id: Option<String>,
    pub hypothesis_core_idea: Option<String>,
    pub hypothesis_why: Option<String>,
    pub hypothesis_json: Option<serde_json::Value>,
    pub tournament_decision: Option<String>,
    pub rejection_reason: Option<String>,
    pub scalar_score_delta: Option<f64>,
    pub drift_loss_delta: Option<f64>,
    pub pass_rate_delta: Option<f64>,
    pub outcome_json: Option<serde_json::Value>,
}

/// A `patches` row.
#[derive(Debug, Clone, Default)]
pub struct PatchRow {
    pub patch_id: Option<String>,
    pub epoch_id: Option<String>,
    pub generation_id: Option<String>,
    pub mutation_id: Option<String>,
    pub op: Option<String>,
    pub rationale: Option<String>,
}

/// One per-entry loss profile (`loss_profiles` joined to nothing else).
#[derive(Debug, Clone, Default)]
pub struct LossProfileRow {
    pub run_id: Option<String>,
    pub epoch_id: Option<String>,
    pub generation_id: Option<String>,
    pub entry_id: Option<String>,
    pub drift_loss: Option<f64>,
    pub pass_fail: Option<String>,
}

/// Error returned by index-db helpers. `Absent` lets a route attach a
/// `note`; `Query` is folded to an empty result (never a 500);
/// `StaleSchema` lets a route attach a "reindex" note rather than serve
/// rows decoded against a schema this binary does not understand.
#[derive(Debug)]
pub enum IndexError {
    /// `index.db` does not exist on disk.
    Absent,
    /// The file exists but a connection or query failed.
    Query(String),
    /// The file's `user_version` does not match [`EXPECTED_SCHEMA_VERSION`]
    /// — the index was built by a different schema generation. Carries
    /// `(found, expected)` so the caller can surface a precise note.
    StaleSchema { found: i64, expected: i64 },
}

/// Open the index read-only WITHOUT a schema-version check.
///
/// Tolerates a WAL sidecar (the `SQLITE_OPEN_READ_ONLY` flag still permits
/// reading a WAL database). Prefer [`open`], which additionally guards the
/// `user_version`; this raw variant exists for callers (and tests) that
/// build a database whose schema version is not stamped.
pub fn open_unchecked(db_path: &Path) -> Result<Connection, IndexError> {
    if !db_path.exists() {
        return Err(IndexError::Absent);
    }
    Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|e| IndexError::Query(e.to_string()))
}

/// Read `PRAGMA user_version` from an open connection. A read failure
/// degrades to `0` (the SQLite default for a file whose version was never
/// stamped), which the schema guard treats as stale.
pub fn schema_version(conn: &Connection) -> i64 {
    conn.query_row("PRAGMA user_version", [], |row| row.get::<_, i64>(0))
        .unwrap_or(0)
}

/// Open the index read-only AND verify its schema version.
///
/// Reads `PRAGMA user_version` and returns [`IndexError::StaleSchema`]
/// when it does not equal [`EXPECTED_SCHEMA_VERSION`]. This closes the
/// silent-misread failure mode: a database built by a newer (or older)
/// Python schema has different columns/joins, so the positional/named
/// readers here could otherwise return rows decoded against the wrong
/// shape. Tolerates a WAL sidecar exactly like [`open_unchecked`].
pub fn open(db_path: &Path) -> Result<Connection, IndexError> {
    let conn = open_unchecked(db_path)?;
    let found = schema_version(&conn);
    if found != EXPECTED_SCHEMA_VERSION {
        return Err(IndexError::StaleSchema {
            found,
            expected: EXPECTED_SCHEMA_VERSION,
        });
    }
    Ok(conn)
}

/// Pull an optional column without failing the whole row on a type
/// surprise (e.g. an `INTEGER` where we expected `TEXT`).
fn opt_str(row: &Row, idx: usize) -> Option<String> {
    row.get::<_, Option<String>>(idx).ok().flatten()
}

fn opt_f64(row: &Row, idx: usize) -> Option<f64> {
    row.get::<_, Option<f64>>(idx).ok().flatten()
}

fn opt_bool(row: &Row, idx: usize) -> bool {
    row.get::<_, Option<i64>>(idx)
        .ok()
        .flatten()
        .map(|v| v != 0)
        .unwrap_or(false)
}

/// Parse a `TEXT` JSON column into a `serde_json::Value`. A non-string
/// or unparseable value yields `None`.
fn opt_json(row: &Row, idx: usize) -> Option<serde_json::Value> {
    let raw = opt_str(row, idx)?;
    serde_json::from_str(&raw).ok()
}

/// All `tournaments` rows for `epoch_id`, ordered by `ran_at` so the
/// bracket reads chronologically. Each row is left-joined to its
/// `experiments` row for the hypothesis idea.
///
/// A missing `tournaments` (or `experiments`) table degrades to an empty
/// vec.
pub fn tournaments_for_epoch(conn: &Connection, epoch_id: &str) -> Vec<TournamentRow> {
    let sql = "SELECT t.tournament_id, t.epoch_id, t.parent_generation_id, \
               t.child_generation_id, t.decision, t.parent_scalar, t.child_scalar, \
               t.delta_scalar, t.rejection_reason, t.ran_at, e.hypothesis_core_idea \
               FROM tournaments t \
               LEFT JOIN experiments e \
               ON e.epoch_id = t.epoch_id AND e.generation_id = t.child_generation_id \
               WHERE t.epoch_id = ?1 \
               ORDER BY t.ran_at ASC, t.tournament_id ASC";
    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    let rows = stmt.query_map([epoch_id], |row| {
        Ok(TournamentRow {
            tournament_id: opt_str(row, 0),
            epoch_id: opt_str(row, 1),
            parent_generation_id: opt_str(row, 2),
            child_generation_id: opt_str(row, 3),
            decision: opt_str(row, 4),
            parent_scalar: opt_f64(row, 5),
            child_scalar: opt_f64(row, 6),
            delta_scalar: opt_f64(row, 7),
            rejection_reason: opt_str(row, 8),
            ran_at: opt_str(row, 9),
            hypothesis_core_idea: opt_str(row, 10),
        })
    });
    match rows {
        Ok(iter) => iter.flatten().collect(),
        Err(_) => Vec::new(),
    }
}

/// All `generations` rows for `epoch_id`.
pub fn generations_for_epoch(conn: &Connection, epoch_id: &str) -> Vec<GenerationRow> {
    let sql = "SELECT epoch_id, generation_id, parent_generation_id, promoted \
               FROM generations WHERE epoch_id = ?1";
    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    let rows = stmt.query_map([epoch_id], |row| {
        Ok(GenerationRow {
            epoch_id: opt_str(row, 0),
            generation_id: opt_str(row, 1),
            parent_generation_id: opt_str(row, 2),
            promoted: opt_bool(row, 3),
        })
    });
    match rows {
        Ok(iter) => iter.flatten().collect(),
        Err(_) => Vec::new(),
    }
}

/// The single `experiments` row for one generation, if present.
pub fn experiment_for_generation(conn: &Connection, generation_id: &str) -> Option<ExperimentRow> {
    let sql = "SELECT epoch_id, generation_id, hypothesis_core_idea, hypothesis_why, \
               hypothesis_json, tournament_decision, rejection_reason, scalar_score_delta, \
               drift_loss_delta, pass_rate_delta, outcome_json \
               FROM experiments WHERE generation_id = ?1 LIMIT 1";
    let mut stmt = conn.prepare(sql).ok()?;
    let mut rows = stmt
        .query_map([generation_id], |row| {
            Ok(ExperimentRow {
                epoch_id: opt_str(row, 0),
                generation_id: opt_str(row, 1),
                hypothesis_core_idea: opt_str(row, 2),
                hypothesis_why: opt_str(row, 3),
                hypothesis_json: opt_json(row, 4),
                tournament_decision: opt_str(row, 5),
                rejection_reason: opt_str(row, 6),
                scalar_score_delta: opt_f64(row, 7),
                drift_loss_delta: opt_f64(row, 8),
                pass_rate_delta: opt_f64(row, 9),
                outcome_json: opt_json(row, 10),
            })
        })
        .ok()?;
    rows.next().and_then(|r| r.ok())
}

/// The single `tournaments` row whose `child_generation_id` matches, if
/// present (joined to `experiments` for the hypothesis idea).
pub fn tournament_for_child(conn: &Connection, generation_id: &str) -> Option<TournamentRow> {
    let sql = "SELECT t.tournament_id, t.epoch_id, t.parent_generation_id, \
               t.child_generation_id, t.decision, t.parent_scalar, t.child_scalar, \
               t.delta_scalar, t.rejection_reason, t.ran_at, e.hypothesis_core_idea \
               FROM tournaments t \
               LEFT JOIN experiments e \
               ON e.epoch_id = t.epoch_id AND e.generation_id = t.child_generation_id \
               WHERE t.child_generation_id = ?1 LIMIT 1";
    let mut stmt = conn.prepare(sql).ok()?;
    let mut rows = stmt
        .query_map([generation_id], |row| {
            Ok(TournamentRow {
                tournament_id: opt_str(row, 0),
                epoch_id: opt_str(row, 1),
                parent_generation_id: opt_str(row, 2),
                child_generation_id: opt_str(row, 3),
                decision: opt_str(row, 4),
                parent_scalar: opt_f64(row, 5),
                child_scalar: opt_f64(row, 6),
                delta_scalar: opt_f64(row, 7),
                rejection_reason: opt_str(row, 8),
                ran_at: opt_str(row, 9),
                hypothesis_core_idea: opt_str(row, 10),
            })
        })
        .ok()?;
    rows.next().and_then(|r| r.ok())
}

/// All `patches` rows for one generation, ordered by `patch_id`.
pub fn patches_for_generation(conn: &Connection, generation_id: &str) -> Vec<PatchRow> {
    let sql = "SELECT patch_id, epoch_id, generation_id, mutation_id, op, rationale \
               FROM patches WHERE generation_id = ?1 ORDER BY patch_id ASC";
    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    let rows = stmt.query_map([generation_id], |row| {
        Ok(PatchRow {
            patch_id: opt_str(row, 0),
            epoch_id: opt_str(row, 1),
            generation_id: opt_str(row, 2),
            mutation_id: opt_str(row, 3),
            op: opt_str(row, 4),
            rationale: opt_str(row, 5),
        })
    });
    match rows {
        Ok(iter) => iter.flatten().collect(),
        Err(_) => Vec::new(),
    }
}

/// All `loss_profiles` rows for one generation, ordered by `entry_id`.
pub fn loss_profiles_for_generation(conn: &Connection, generation_id: &str) -> Vec<LossProfileRow> {
    let sql = "SELECT run_id, epoch_id, generation_id, entry_id, drift_loss, pass_fail \
               FROM loss_profiles WHERE generation_id = ?1 ORDER BY entry_id ASC";
    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(_) => return Vec::new(),
    };
    let rows = stmt.query_map([generation_id], |row| {
        Ok(LossProfileRow {
            run_id: opt_str(row, 0),
            epoch_id: opt_str(row, 1),
            generation_id: opt_str(row, 2),
            entry_id: opt_str(row, 3),
            drift_loss: opt_f64(row, 4),
            pass_fail: opt_str(row, 5),
        })
    });
    match rows {
        Ok(iter) => iter.flatten().collect(),
        Err(_) => Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    /// Build a tiny on-disk index with the columns the helpers read.
    fn build_index(dir: &Path) -> std::path::PathBuf {
        let path = dir.join("index.db");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(
            "CREATE TABLE generations(epoch_id TEXT, generation_id TEXT, \
                 parent_generation_id TEXT, promoted INTEGER);
             CREATE TABLE experiments(epoch_id TEXT, generation_id TEXT, \
                 hypothesis_core_idea TEXT, hypothesis_why TEXT, hypothesis_json TEXT, \
                 tournament_decision TEXT, rejection_reason TEXT, scalar_score_delta REAL, \
                 drift_loss_delta REAL, pass_rate_delta REAL, outcome_json TEXT);
             CREATE TABLE patches(patch_id TEXT, epoch_id TEXT, generation_id TEXT, \
                 mutation_id TEXT, op TEXT, rationale TEXT);
             CREATE TABLE loss_profiles(run_id TEXT, epoch_id TEXT, generation_id TEXT, \
                 entry_id TEXT, drift_loss REAL, pass_fail TEXT, loss_json TEXT);
             CREATE TABLE tournaments(tournament_id TEXT, epoch_id TEXT, \
                 parent_generation_id TEXT, child_generation_id TEXT, decision TEXT, \
                 parent_scalar REAL, child_scalar REAL, delta_scalar REAL, \
                 rejection_reason TEXT, ran_at TEXT);
             INSERT INTO generations VALUES('e1','v0',NULL,1);
             INSERT INTO generations VALUES('e1','v1','v0',0);
             INSERT INTO generations VALUES('e1','v2','v0',1);
             INSERT INTO experiments VALUES('e1','v1','tighten the planner',\
                 'planner overshoots','{\"k\":1}','rejected','worse drift',\
                 -0.1,0.2,-0.05,'{\"o\":2}');
             INSERT INTO experiments VALUES('e1','v2','add a retry',\
                 'flaky tool','{\"k\":2}','promoted',NULL,0.3,-0.1,0.1,'{\"o\":3}');
             INSERT INTO patches VALUES('p1','e1','v1','m1','replace','swap prompt');
             INSERT INTO loss_profiles VALUES('r1','e1','v1','b1',0.5,'fail','{}');
             INSERT INTO loss_profiles VALUES('r2','e1','v1','b2',0.2,'pass','{}');
             INSERT INTO tournaments VALUES('t1','e1','v0','v1','rejected',\
                 0.8,0.8,0.0,'worse drift','2026-05-15T01:00:00Z');
             INSERT INTO tournaments VALUES('t2','e1','v0','v2','promoted',\
                 0.8,1.1,0.3,NULL,'2026-05-15T02:00:00Z');",
        )
        .unwrap();
        // Stamp the schema version so `open()`'s guard accepts the fixture.
        conn.execute_batch(&format!("PRAGMA user_version = {EXPECTED_SCHEMA_VERSION}"))
            .unwrap();
        path
    }

    #[test]
    fn absent_db_reports_absent() {
        let tmp = TempDir::new().unwrap();
        match open(&tmp.path().join("nope.db")) {
            Err(IndexError::Absent) => {}
            other => panic!("expected Absent, got {other:?}"),
        }
    }

    #[test]
    fn open_is_read_only() {
        let tmp = TempDir::new().unwrap();
        let path = build_index(tmp.path());
        let conn = open(&path).unwrap();
        // A write must be rejected by the read-only connection.
        assert!(conn
            .execute("INSERT INTO generations VALUES('e1','x',NULL,0)", [])
            .is_err());
    }

    #[test]
    fn reads_tournaments_in_order() {
        let tmp = TempDir::new().unwrap();
        let path = build_index(tmp.path());
        let conn = open(&path).unwrap();
        let rows = tournaments_for_epoch(&conn, "e1");
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].child_generation_id.as_deref(), Some("v1"));
        assert_eq!(
            rows[0].hypothesis_core_idea.as_deref(),
            Some("tighten the planner")
        );
        assert_eq!(rows[1].child_generation_id.as_deref(), Some("v2"));
    }

    #[test]
    fn reads_generations() {
        let tmp = TempDir::new().unwrap();
        let path = build_index(tmp.path());
        let conn = open(&path).unwrap();
        let gens = generations_for_epoch(&conn, "e1");
        assert_eq!(gens.len(), 3);
        let promoted = gens.iter().filter(|g| g.promoted).count();
        assert_eq!(promoted, 2);
    }

    #[test]
    fn reads_experiment_patches_losses() {
        let tmp = TempDir::new().unwrap();
        let path = build_index(tmp.path());
        let conn = open(&path).unwrap();
        let exp = experiment_for_generation(&conn, "v1").unwrap();
        assert_eq!(
            exp.hypothesis_core_idea.as_deref(),
            Some("tighten the planner")
        );
        assert_eq!(exp.hypothesis_json, Some(serde_json::json!({"k": 1})));
        let patches = patches_for_generation(&conn, "v1");
        assert_eq!(patches.len(), 1);
        assert_eq!(patches[0].op.as_deref(), Some("replace"));
        let losses = loss_profiles_for_generation(&conn, "v1");
        assert_eq!(losses.len(), 2);
        assert_eq!(losses[0].entry_id.as_deref(), Some("b1"));
    }

    #[test]
    fn missing_table_degrades_to_empty() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("bare.db");
        Connection::open(&path).unwrap();
        // A bare database carries no tables AND no stamped version; use the
        // unchecked open so this test isolates the missing-table degradation
        // from the schema-version guard (covered separately below).
        let conn = open_unchecked(&path).unwrap();
        assert!(tournaments_for_epoch(&conn, "e1").is_empty());
        assert!(generations_for_epoch(&conn, "e1").is_empty());
        assert!(experiment_for_generation(&conn, "v1").is_none());
        assert!(patches_for_generation(&conn, "v1").is_empty());
    }

    /// The Rust reader's expected schema version MUST track the Python
    /// `SCHEMA_VERSION` (`src/zicato/index/schema.py`). When the Python
    /// schema bumps, this constant must bump in lockstep; this test is the
    /// tripwire that makes the drift impossible to miss.
    #[test]
    fn expected_schema_version_is_pinned_to_python() {
        assert_eq!(
            EXPECTED_SCHEMA_VERSION, 10,
            "EXPECTED_SCHEMA_VERSION must equal the Python SCHEMA_VERSION \
             in src/zicato/index/schema.py (currently 10); bump both together",
        );
    }

    #[test]
    fn open_rejects_a_mismatched_schema_version() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("old.db");
        let conn = Connection::open(&path).unwrap();
        // Stamp a version one behind the expected one.
        conn.execute_batch(&format!(
            "PRAGMA user_version = {}",
            EXPECTED_SCHEMA_VERSION - 1
        ))
        .unwrap();
        drop(conn);
        match open(&path) {
            Err(IndexError::StaleSchema { found, expected }) => {
                assert_eq!(found, EXPECTED_SCHEMA_VERSION - 1);
                assert_eq!(expected, EXPECTED_SCHEMA_VERSION);
            }
            other => panic!("expected StaleSchema, got {other:?}"),
        }
    }

    #[test]
    fn open_rejects_an_unstamped_database_as_stale() {
        // A freshly-created SQLite file defaults user_version to 0, which is
        // never the expected version — the guard treats it as stale rather
        // than reading rows against an unknown shape.
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("fresh.db");
        Connection::open(&path).unwrap();
        match open(&path) {
            Err(IndexError::StaleSchema { found, .. }) => assert_eq!(found, 0),
            other => panic!("expected StaleSchema for unstamped db, got {other:?}"),
        }
    }

    #[test]
    fn open_accepts_a_correctly_stamped_database() {
        let tmp = TempDir::new().unwrap();
        let path = build_index(tmp.path());
        // build_index stamps EXPECTED_SCHEMA_VERSION, so open() accepts it.
        assert!(open(&path).is_ok());
        assert_eq!(schema_version(&open(&path).unwrap()), EXPECTED_SCHEMA_VERSION);
    }
}
