//! Tamper-evident, hash-chained audit ledger owned by the supervisor.
//!
//! INTEGRITY NOTARY — record #1. The watchdog escalates against workers and
//! the orchestrator, and the loop transitions through promote/reject/
//! contract-change decisions; until now the only durable record of any of
//! that was the orchestrator's OWN mutable state under `.zicato/`, which the
//! orchestrator (the very thing being audited) can rewrite. This module
//! gives the supervisor an independent, append-only ledger it alone writes,
//! placed OUTSIDE the orchestrator's mutable trees (under the supervisor's
//! own runtime dir), and HASH-CHAINS each record to its predecessor so that
//! removing, reordering, or editing any record breaks the chain and is
//! detectable.
//!
//! Shape: one JSON object per line (JSONL), each:
//!
//! ```text
//! {"seq":N,"prev":"<hex>","ts":"<rfc3339>","kind":"...","payload":{...},"digest":"<hex>"}
//! ```
//!
//! where `digest = SHA-256(seq ‖ prev ‖ ts ‖ kind ‖ canonical(payload))`
//! and `prev` is the previous record's `digest` (the genesis record links to
//! 64 zeros). [`verify_chain`] walks the file, recomputes every digest, and
//! checks each `prev` link; any mismatch is a tamper finding pinned to the
//! offending `seq`.
//!
//! ALARM-ONLY / READ-ONLY-to-the-orchestrator (v1): the ledger never blocks
//! a promotion or writes the orchestrator's `index.db`; it only records and
//! surfaces. Writing is OPT-IN — a supervisor started without a ledger path
//! behaves exactly as before (nothing is written), so the ledger is purely
//! additive.

use crate::sha256;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use tracing::warn;

/// The all-zero digest the genesis record links back to.
pub const GENESIS_PREV: &str = "0000000000000000000000000000000000000000000000000000000000000000";

/// The append-only ledger file's basename inside the ledger directory.
pub const LEDGER_FILE: &str = "audit_ledger.jsonl";

/// A typed ledger record kind. Serialized in `snake_case`; new kinds are
/// purely additive (an older verifier still hash-checks an unknown kind —
/// the digest covers the raw kind string, not an enum discriminant).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecordKind {
    /// The supervisor started and opened (or created) this ledger.
    SupervisorStart,
    /// A watchdog escalation (SIGTERM/SIGKILL) was taken.
    WatchdogAction,
    /// An observed promote/reject decision transition.
    DecisionObserved,
    /// An observed epoch contract-hash change.
    ContractChange,
    /// A diff-containment quarantine finding (record #2).
    DiffContainmentAlert,
    /// A promotion-gatekeeping contradiction (record #3).
    PromotionContradiction,
    /// An index-vs-canonical divergence finding (record #4).
    DivergenceFinding,
}

impl RecordKind {
    pub fn as_str(self) -> &'static str {
        match self {
            RecordKind::SupervisorStart => "supervisor_start",
            RecordKind::WatchdogAction => "watchdog_action",
            RecordKind::DecisionObserved => "decision_observed",
            RecordKind::ContractChange => "contract_change",
            RecordKind::DiffContainmentAlert => "diff_containment_alert",
            RecordKind::PromotionContradiction => "promotion_contradiction",
            RecordKind::DivergenceFinding => "divergence_finding",
        }
    }
}

/// One persisted ledger record, exactly as it is serialized to a line.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Record {
    pub seq: u64,
    /// The previous record's `digest`; genesis links to [`GENESIS_PREV`].
    pub prev: String,
    /// RFC-3339 UTC timestamp when the record was appended.
    pub ts: String,
    pub kind: RecordKind,
    pub payload: serde_json::Value,
    /// `SHA-256(seq ‖ prev ‖ ts ‖ kind ‖ canonical(payload))`, hex.
    pub digest: String,
}

/// Compute a record's digest over its chained, canonical preimage.
///
/// The payload is serialized with `serde_json` (which emits object keys in
/// insertion order; the supervisor always builds the same payloads the same
/// way, so the canonical form is stable for a given input). The preimage
/// binds `seq`, the `prev` link, `ts`, and the `kind` string so that none of
/// them can be altered without changing the digest.
fn compute_digest(
    seq: u64,
    prev: &str,
    ts: &str,
    kind: RecordKind,
    payload: &serde_json::Value,
) -> String {
    let payload_bytes = serde_json::to_vec(payload).unwrap_or_default();
    let mut preimage = Vec::with_capacity(payload_bytes.len() + 96);
    preimage.extend_from_slice(&seq.to_be_bytes());
    preimage.push(0x1f); // unit separator between fields
    preimage.extend_from_slice(prev.as_bytes());
    preimage.push(0x1f);
    preimage.extend_from_slice(ts.as_bytes());
    preimage.push(0x1f);
    preimage.extend_from_slice(kind.as_str().as_bytes());
    preimage.push(0x1f);
    preimage.extend_from_slice(&payload_bytes);
    sha256::hex_digest(&preimage)
}

/// A persisted, append-only, hash-chained audit ledger the supervisor owns.
///
/// Thread-safe (a `Mutex` guards the append cursor) and shared by `Arc`. The
/// in-memory `tail` (last seq + last digest) lets a new record chain onto the
/// previous one without re-reading the whole file each append; it is seeded
/// from the file on construction so a restart continues the existing chain.
#[derive(Debug)]
pub struct AuditLedger {
    path: PathBuf,
    state: Mutex<Tail>,
}

#[derive(Debug, Clone)]
struct Tail {
    /// The seq the NEXT appended record will carry.
    next_seq: u64,
    /// The digest the next record links to (genesis or the last record's).
    prev: String,
}

impl AuditLedger {
    /// Open (or create) the ledger at `dir/audit_ledger.jsonl`, seeding the
    /// chain tail from any existing file so a restart continues the chain.
    ///
    /// Boot sequence, in order:
    ///
    /// 1. **Torn-tail repair** ([`repair_torn_tail`]) — a crash mid-append
    ///    can leave a trailing half-written line; it is truncated BEFORE
    ///    anything reads or chains onto the file, so a post-crash chain
    ///    verifies clean and the next append continues from the last
    ///    complete record.
    /// 2. **Verify-on-startup** — [`verify_chain`] walks the (repaired)
    ///    file; a broken chain is surfaced as a WARN carrying
    ///    `first_break_seq` + the reason. Alarm-only: the supervisor still
    ///    starts, and appends still chain onto the persisted tail — the
    ///    ledger records, it never gates.
    /// 3. **Tail seeding** — as before.
    ///
    /// Best-effort: a directory that cannot be created, or a file that cannot
    /// be read, degrades to an in-memory tail starting at genesis (the next
    /// append re-creates the file). The supervisor never fails to start over
    /// a ledger problem — the ledger is an alarm surface, not a gate.
    pub fn open(dir: &Path) -> Self {
        if let Err(e) = std::fs::create_dir_all(dir) {
            warn!(?dir, error=%e, "could not create audit-ledger dir; ledger degraded");
        }
        let path = dir.join(LEDGER_FILE);
        if let Some(dropped) = repair_torn_tail(&path) {
            warn!(
                ?path,
                dropped_bytes = dropped,
                "audit-ledger torn tail truncated (crash mid-append); chain continues from the last complete record",
            );
        }
        let report = verify_chain(&path);
        if !report.intact {
            warn!(
                ?path,
                first_break_seq = ?report.first_break_seq,
                reason = ?report.break_reason,
                records = report.records,
                "audit-ledger chain verification FAILED at startup — possible tampering",
            );
        }
        let tail = Self::seed_tail(&path);
        Self {
            path,
            state: Mutex::new(tail),
        }
    }

    /// Read the existing file (if any) and derive the chain tail: the seq the
    /// next record should carry and the digest it should link to. A missing
    /// or empty file seeds genesis.
    fn seed_tail(path: &Path) -> Tail {
        let genesis = Tail {
            next_seq: 0,
            prev: GENESIS_PREV.to_string(),
        };
        let text = match std::fs::read_to_string(path) {
            Ok(t) => t,
            Err(_) => return genesis,
        };
        let mut tail = genesis;
        for line in text.lines().filter(|l| !l.trim().is_empty()) {
            if let Ok(rec) = serde_json::from_str::<Record>(line) {
                tail = Tail {
                    next_seq: rec.seq + 1,
                    prev: rec.digest,
                };
            }
        }
        tail
    }

    /// The ledger file path.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Append one record, chaining it onto the current tail. Returns the
    /// appended record's digest on success.
    ///
    /// Best-effort durability: the line is written and flushed; an I/O error
    /// is logged and `None` returned, and the in-memory tail is NOT advanced
    /// (so the next append retries the same seq/prev rather than chaining onto
    /// a record that never reached disk).
    pub fn append(&self, kind: RecordKind, payload: serde_json::Value) -> Option<String> {
        let mut state = match self.state.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        };
        let seq = state.next_seq;
        let prev = state.prev.clone();
        let ts = Utc::now().to_rfc3339();
        let digest = compute_digest(seq, &prev, &ts, kind, &payload);
        let record = Record {
            seq,
            prev,
            ts,
            kind,
            payload,
            digest: digest.clone(),
        };
        let mut line = match serde_json::to_string(&record) {
            Ok(s) => s,
            Err(e) => {
                warn!(error=%e, "audit-ledger record serialization failed");
                return None;
            }
        };
        line.push('\n');
        match self.write_line(line.as_bytes()) {
            Ok(()) => {
                state.next_seq = seq + 1;
                state.prev = digest.clone();
                Some(digest)
            }
            Err(e) => {
                warn!(path=?self.path, error=%e, "audit-ledger append failed; chain tail not advanced");
                None
            }
        }
    }

    /// Append-write `bytes` to the ledger file, creating it if needed.
    ///
    /// `sync_all` after the write: the ledger is a LOW-FREQUENCY audit
    /// artifact (a handful of records per run), and durability is the
    /// whole point of an audit trail — an escalation record that
    /// evaporates on power loss defeats the notary. The fsync cost is
    /// negligible at this write rate. The existing best-effort append
    /// semantics are unchanged: on any I/O error (including the fsync)
    /// the caller logs and does NOT advance the in-memory tail, so the
    /// next append retries the same seq/prev; a half-written line left
    /// by a crash is truncated by [`repair_torn_tail`] at the next open.
    fn write_line(&self, bytes: &[u8]) -> std::io::Result<()> {
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        f.write_all(bytes)?;
        f.flush()?;
        f.sync_all()
    }

    /// Walk the persisted chain and verify every record's digest and `prev`
    /// link. Reads the file fresh (not the in-memory tail) so it detects
    /// out-of-band tampering since the last append.
    pub fn verify(&self) -> VerifyReport {
        verify_chain(&self.path)
    }
}

/// The outcome of verifying a ledger chain.
#[derive(Debug, Clone, Serialize, Default, PartialEq, Eq)]
pub struct VerifyReport {
    /// `true` when every record's digest recomputes and every `prev` link
    /// matches its predecessor (an empty/absent ledger is trivially intact).
    pub intact: bool,
    /// Number of records walked.
    pub records: u64,
    /// The seq of the first record that failed verification, when any did.
    pub first_break_seq: Option<u64>,
    /// A human-readable reason for the first break, when any.
    pub break_reason: Option<String>,
}

/// Truncate a torn (half-written) FINAL line off the ledger file.
///
/// A crash between `write_all` and the bytes reaching disk can leave the
/// file ending in a partial record — the one shape that is *provably* a
/// torn append rather than tampering, because [`AuditLedger::append`]
/// writes exactly one `line + '\n'` per record and never rewrites earlier
/// bytes. Repairing it at open time means a post-crash chain verifies
/// clean and the next append chains onto the last COMPLETE record instead
/// of stacking a valid record after garbage (which would poison
/// [`verify_chain`] forever).
///
/// Deliberately surgical: only the TRAILING unparseable line is dropped.
/// An unparseable line in the *middle* of the file cannot be a torn
/// append and is left in place for `verify_chain` to flag as a break.
/// Returns the number of bytes truncated, or `None` when the file is
/// absent, empty, or ends in a complete record. Best-effort: an I/O
/// failure leaves the file untouched (the ledger never blocks boot).
pub fn repair_torn_tail(path: &Path) -> Option<u64> {
    // Operate on BYTES: a torn append can split a multi-byte UTF-8
    // character, which would make a string read fail outright.
    let bytes = std::fs::read(path).ok()?;
    if bytes.is_empty() {
        return None;
    }
    // Find the byte offset where the final non-empty line starts.
    let mut tail_start: Option<usize> = None;
    let mut offset = 0usize;
    for line in bytes.split_inclusive(|b| *b == b'\n') {
        if line.iter().any(|b| !b.is_ascii_whitespace()) {
            tail_start = Some(offset);
        }
        offset += line.len();
    }
    let start = tail_start?;
    let mut tail = &bytes[start..];
    while let [rest @ .., last] = tail {
        if *last == b'\n' || last.is_ascii_whitespace() {
            tail = rest;
        } else {
            break;
        }
    }
    if serde_json::from_slice::<Record>(tail).is_ok() {
        return None; // complete final record — nothing torn.
    }
    let file = std::fs::OpenOptions::new().write(true).open(path).ok()?;
    file.set_len(start as u64).ok()?;
    let _ = file.sync_all();
    Some((bytes.len() - start) as u64)
}

/// Verify the hash-chain in the ledger file at `path`.
///
/// An absent or empty file is reported intact with zero records (there is
/// nothing to tamper with). A line that fails to parse, a digest that does
/// not recompute, a `prev` link that does not match the prior digest, or a
/// non-contiguous `seq` are all chain breaks pinned to the offending seq.
pub fn verify_chain(path: &Path) -> VerifyReport {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return VerifyReport {
                intact: true,
                ..Default::default()
            };
        }
        Err(e) => {
            return VerifyReport {
                intact: false,
                records: 0,
                first_break_seq: None,
                break_reason: Some(format!("ledger unreadable: {e}")),
            };
        }
    };

    let mut records: u64 = 0;
    let mut expected_prev = GENESIS_PREV.to_string();
    let mut expected_seq: u64 = 0;

    for (lineno, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let rec: Record = match serde_json::from_str(line) {
            Ok(r) => r,
            Err(e) => {
                return VerifyReport {
                    intact: false,
                    records,
                    first_break_seq: Some(expected_seq),
                    break_reason: Some(format!("line {} is not a valid record: {e}", lineno + 1)),
                };
            }
        };
        // seq must be contiguous from 0.
        if rec.seq != expected_seq {
            return VerifyReport {
                intact: false,
                records,
                first_break_seq: Some(rec.seq),
                break_reason: Some(format!(
                    "seq discontinuity: expected {expected_seq}, found {}",
                    rec.seq
                )),
            };
        }
        // prev must link to the previous record's digest (or genesis).
        if rec.prev != expected_prev {
            return VerifyReport {
                intact: false,
                records,
                first_break_seq: Some(rec.seq),
                break_reason: Some(format!("seq {} prev-link broken", rec.seq)),
            };
        }
        // The recorded digest must recompute over the record's own fields.
        let recomputed = compute_digest(rec.seq, &rec.prev, &rec.ts, rec.kind, &rec.payload);
        if recomputed != rec.digest {
            return VerifyReport {
                intact: false,
                records,
                first_break_seq: Some(rec.seq),
                break_reason: Some(format!("seq {} digest mismatch (record altered)", rec.seq)),
            };
        }
        expected_prev = rec.digest;
        expected_seq = rec.seq + 1;
        records += 1;
    }

    VerifyReport {
        intact: true,
        records,
        first_break_seq: None,
        break_reason: None,
    }
}

/// A stateful observer that records promote/reject decision transitions and
/// epoch contract-hash changes into the ledger the first time it sees each.
///
/// The supervisor cannot trust the orchestrator's own state for an audit
/// trail (the orchestrator can rewrite it), so the INTEGRITY NOTARY watches
/// the same observable surfaces — the per-generation `promoted` flag and the
/// epoch `contract_hash` — and stamps each transition into the independent,
/// hash-chained ledger as it resolves. De-duplicated: a generation's decision
/// is recorded once (the first time it resolves from in-flight), and a
/// contract hash once per distinct value, so a steady-state poll appends
/// nothing.
#[derive(Debug, Default)]
pub struct TransitionObserver {
    /// Generations whose resolved decision has already been recorded, keyed
    /// by `(epoch_id, generation_id)`.
    recorded_decisions: std::collections::HashSet<(String, String)>,
    /// The last contract hash recorded, per epoch.
    recorded_contract: std::collections::HashMap<String, String>,
}

impl TransitionObserver {
    pub fn new() -> Self {
        Self::default()
    }

    /// Record any newly-resolved generation decision into the ledger.
    ///
    /// `generations` is `(epoch_id, generation_id, promoted)` where `promoted`
    /// is `None` while in flight and `Some(bool)` once resolved. Only the
    /// transition into a resolved decision is recorded, once per generation.
    /// Returns the number of decisions newly recorded.
    pub fn observe_decisions<'a, I>(&mut self, ledger: &AuditLedger, generations: I) -> usize
    where
        I: IntoIterator<Item = (&'a str, &'a str, Option<bool>)>,
    {
        let mut recorded = 0;
        for (epoch_id, generation_id, promoted) in generations {
            let Some(promoted) = promoted else {
                continue; // still in flight — no decision yet.
            };
            let key = (epoch_id.to_string(), generation_id.to_string());
            if self.recorded_decisions.contains(&key) {
                continue;
            }
            self.recorded_decisions.insert(key);
            ledger.append(
                RecordKind::DecisionObserved,
                serde_json::json!({
                    "epoch_id": epoch_id,
                    "generation_id": generation_id,
                    "decision": if promoted { "promote" } else { "reject" },
                }),
            );
            recorded += 1;
        }
        recorded
    }

    /// Record an epoch contract-hash change into the ledger, once per distinct
    /// `(epoch_id, contract_hash)`. Returns `true` when a new value was
    /// recorded.
    pub fn observe_contract(
        &mut self,
        ledger: &AuditLedger,
        epoch_id: &str,
        contract_hash: &str,
    ) -> bool {
        if contract_hash.is_empty() {
            return false;
        }
        if self.recorded_contract.get(epoch_id).map(String::as_str) == Some(contract_hash) {
            return false;
        }
        let previous = self
            .recorded_contract
            .insert(epoch_id.to_string(), contract_hash.to_string());
        ledger.append(
            RecordKind::ContractChange,
            serde_json::json!({
                "epoch_id": epoch_id,
                "contract_hash": contract_hash,
                "previous_contract_hash": previous,
            }),
        );
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn ledger_dir() -> (TempDir, PathBuf) {
        let tmp = TempDir::new().unwrap();
        let dir = tmp.path().join("super-runtime");
        (tmp, dir)
    }

    #[test]
    fn append_then_verify_is_intact() {
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        led.append(RecordKind::SupervisorStart, serde_json::json!({"v": 1}));
        led.append(
            RecordKind::WatchdogAction,
            serde_json::json!({"pid": 42, "outcome": "killed_forcefully"}),
        );
        let report = led.verify();
        assert!(report.intact, "fresh chain must verify: {report:?}");
        assert_eq!(report.records, 2);
        assert!(report.first_break_seq.is_none());
    }

    #[test]
    fn empty_or_absent_ledger_is_intact() {
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        let report = led.verify();
        assert!(report.intact);
        assert_eq!(report.records, 0);
    }

    #[test]
    fn chain_links_each_record_to_the_prior_digest() {
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        let d0 = led
            .append(RecordKind::SupervisorStart, serde_json::json!({}))
            .unwrap();
        let d1 = led
            .append(RecordKind::WatchdogAction, serde_json::json!({"pid": 7}))
            .unwrap();
        assert_ne!(d0, d1);
        let text = std::fs::read_to_string(led.path()).unwrap();
        let recs: Vec<Record> = text
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(recs[0].prev, GENESIS_PREV);
        assert_eq!(recs[1].prev, d0);
        assert_eq!(recs[0].seq, 0);
        assert_eq!(recs[1].seq, 1);
    }

    #[test]
    fn editing_a_payload_breaks_the_chain() {
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        led.append(RecordKind::SupervisorStart, serde_json::json!({"v": 1}));
        led.append(RecordKind::WatchdogAction, serde_json::json!({"pid": 42}));
        // Tamper: rewrite the second record's payload but keep its digest.
        let text = std::fs::read_to_string(led.path()).unwrap();
        let mut lines: Vec<String> = text.lines().map(str::to_string).collect();
        lines[1] = lines[1].replace("\"pid\":42", "\"pid\":99999");
        std::fs::write(led.path(), lines.join("\n") + "\n").unwrap();

        let report = verify_chain(led.path());
        assert!(!report.intact, "an edited record must break the chain");
        assert_eq!(report.first_break_seq, Some(1));
    }

    #[test]
    fn dropping_a_record_breaks_the_chain() {
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        led.append(RecordKind::SupervisorStart, serde_json::json!({}));
        led.append(RecordKind::WatchdogAction, serde_json::json!({"pid": 1}));
        led.append(
            RecordKind::DecisionObserved,
            serde_json::json!({"d": "promote"}),
        );
        // Remove the middle record: seq jumps 0 -> 2 and the prev link breaks.
        let text = std::fs::read_to_string(led.path()).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        let kept = format!("{}\n{}\n", lines[0], lines[2]);
        std::fs::write(led.path(), kept).unwrap();

        let report = verify_chain(led.path());
        assert!(!report.intact, "a dropped record must break the chain");
        assert_eq!(report.first_break_seq, Some(2));
    }

    #[test]
    fn observer_records_each_decision_once() {
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        let mut obs = TransitionObserver::new();
        // First pass: v1 in flight (None), v2 promoted, v3 rejected.
        let n = obs.observe_decisions(
            &led,
            vec![
                ("e1", "v1", None),
                ("e1", "v2", Some(true)),
                ("e1", "v3", Some(false)),
            ],
        );
        assert_eq!(n, 2, "two resolved decisions recorded; v1 still in flight");
        // Second pass: v1 now resolves; v2/v3 unchanged → only v1 is new.
        let n2 = obs.observe_decisions(
            &led,
            vec![
                ("e1", "v1", Some(true)),
                ("e1", "v2", Some(true)),
                ("e1", "v3", Some(false)),
            ],
        );
        assert_eq!(n2, 1, "only the newly-resolved v1 is recorded");
        // A steady-state re-poll records nothing.
        let n3 = obs.observe_decisions(&led, vec![("e1", "v2", Some(true))]);
        assert_eq!(n3, 0);
        assert!(led.verify().intact);
        assert_eq!(led.verify().records, 3);
    }

    #[test]
    fn observer_records_contract_change_once_per_value() {
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        let mut obs = TransitionObserver::new();
        assert!(obs.observe_contract(&led, "e1", "hashA"));
        // Same value again → no new record.
        assert!(!obs.observe_contract(&led, "e1", "hashA"));
        // A changed value → recorded.
        assert!(obs.observe_contract(&led, "e1", "hashB"));
        // Empty hash is ignored.
        assert!(!obs.observe_contract(&led, "e1", ""));
        let report = led.verify();
        assert!(report.intact);
        assert_eq!(report.records, 2);
    }

    // ---- torn-tail truncation + verify-on-startup -------------------

    #[test]
    fn torn_tail_is_truncated_on_reopen_and_chain_verifies_clean() {
        let (_t, dir) = ledger_dir();
        {
            let led = AuditLedger::open(&dir);
            led.append(RecordKind::SupervisorStart, serde_json::json!({"v": 1}));
            led.append(RecordKind::WatchdogAction, serde_json::json!({"pid": 42}));
            // Simulate a crash mid-append: a trailing half-written line.
            let mut f = std::fs::OpenOptions::new()
                .append(true)
                .open(led.path())
                .unwrap();
            f.write_all(b"{\"seq\":2,\"prev\":\"abc").unwrap();
        }
        let led2 = AuditLedger::open(&dir);
        // The torn line is gone and the surviving chain verifies clean.
        let report = led2.verify();
        assert!(report.intact, "post-crash chain must verify: {report:?}");
        assert_eq!(report.records, 2);
        // The next append continues at seq 2, chaining onto record 1.
        led2.append(
            RecordKind::DecisionObserved,
            serde_json::json!({"d": "promote"}),
        );
        let report = led2.verify();
        assert!(report.intact);
        assert_eq!(report.records, 3);
        let text = std::fs::read_to_string(led2.path()).unwrap();
        let recs: Vec<Record> = text
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(recs[2].seq, 2);
        assert_eq!(recs[2].prev, recs[1].digest);
    }

    #[test]
    fn torn_tail_repair_handles_invalid_utf8() {
        // A torn append can split a multi-byte character; the repair
        // must still truncate (a string read would fail outright).
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        led.append(RecordKind::SupervisorStart, serde_json::json!({}));
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(led.path())
            .unwrap();
        // 0xE2 0x82 is a truncated 3-byte UTF-8 sequence.
        f.write_all(b"{\"seq\":1,\"ts\":\"\xE2\x82").unwrap();
        drop(f);

        assert!(repair_torn_tail(led.path()).is_some());
        let report = verify_chain(led.path());
        assert!(report.intact, "{report:?}");
        assert_eq!(report.records, 1);
    }

    #[test]
    fn repair_leaves_a_complete_tail_alone() {
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        led.append(RecordKind::SupervisorStart, serde_json::json!({}));
        led.append(RecordKind::WatchdogAction, serde_json::json!({"pid": 9}));
        let before = std::fs::read(led.path()).unwrap();
        assert_eq!(repair_torn_tail(led.path()), None);
        assert_eq!(
            std::fs::read(led.path()).unwrap(),
            before,
            "no bytes may change"
        );
        // Absent / empty files are equally untouched.
        assert_eq!(repair_torn_tail(&dir.join("no-such-file")), None);
    }

    #[test]
    fn repair_only_truncates_the_trailing_line_not_midfile_garbage() {
        // Garbage in the MIDDLE of the file cannot be a torn append —
        // it must be LEFT for verify_chain to flag as a break.
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        led.append(RecordKind::SupervisorStart, serde_json::json!({}));
        led.append(RecordKind::WatchdogAction, serde_json::json!({"pid": 1}));
        let text = std::fs::read_to_string(led.path()).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        let tampered = format!("{}\nnot-a-record\n{}\n", lines[0], lines[1]);
        std::fs::write(led.path(), tampered).unwrap();

        assert_eq!(
            repair_torn_tail(led.path()),
            None,
            "mid-file garbage is not a torn tail"
        );
        let report = verify_chain(led.path());
        assert!(
            !report.intact,
            "mid-file garbage must stay a verify failure"
        );
    }

    #[test]
    fn open_survives_a_tampered_chain_and_still_appends() {
        // Verify-on-startup is ALARM-ONLY: a broken chain is warned
        // about, but the supervisor still opens the ledger and appends.
        let (_t, dir) = ledger_dir();
        {
            let led = AuditLedger::open(&dir);
            led.append(RecordKind::SupervisorStart, serde_json::json!({"v": 1}));
            led.append(RecordKind::WatchdogAction, serde_json::json!({"pid": 42}));
            let text = std::fs::read_to_string(led.path()).unwrap();
            let mut lines: Vec<String> = text.lines().map(str::to_string).collect();
            lines[0] = lines[0].replace("\"v\":1", "\"v\":2");
            std::fs::write(led.path(), lines.join("\n") + "\n").unwrap();
        }
        let led2 = AuditLedger::open(&dir);
        assert!(!led2.verify().intact);
        // Appending still works and chains onto the persisted tail.
        assert!(led2
            .append(
                RecordKind::DecisionObserved,
                serde_json::json!({"d": "reject"})
            )
            .is_some());
        let text = std::fs::read_to_string(led2.path()).unwrap();
        assert_eq!(text.lines().count(), 3);
    }

    #[test]
    fn reopening_continues_the_existing_chain() {
        let (_t, dir) = ledger_dir();
        {
            let led = AuditLedger::open(&dir);
            led.append(RecordKind::SupervisorStart, serde_json::json!({}));
            led.append(RecordKind::WatchdogAction, serde_json::json!({"pid": 5}));
        }
        // A fresh open (simulating a supervisor restart) must continue the
        // chain at the next seq, linking to the persisted tail digest.
        let led2 = AuditLedger::open(&dir);
        led2.append(
            RecordKind::DecisionObserved,
            serde_json::json!({"d": "reject"}),
        );
        let report = led2.verify();
        assert!(report.intact, "reopened chain must stay intact: {report:?}");
        assert_eq!(report.records, 3);
        let text = std::fs::read_to_string(led2.path()).unwrap();
        let recs: Vec<Record> = text
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(recs[2].seq, 2);
    }
}
