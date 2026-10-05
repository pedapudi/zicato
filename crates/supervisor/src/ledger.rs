//! Tamper-evident, hash-chained audit ledger owned by the supervisor.
//!
//! The orchestrator keeps its decisions and contract hashes in its own state
//! under `.zicato/`, which it can rewrite. The ledger is a separate,
//! append-only record that only the supervisor writes. Each record is
//! hash-chained to its predecessor, so editing, reordering, or removing an
//! earlier record breaks the chain at a specific `seq`.
//!
//! Shape: one JSON object per line (JSONL), each:
//!
//! ```text
//! {"seq":N,"prev":"<hex>","ts":"<rfc3339>","kind":"...","payload":{...},"digest":"<hex>"}
//! ```
//!
//! where `digest = SHA-256(seq ‖ prev ‖ ts ‖ kind ‖ payload)`. The payload
//! term is the exact bytes of the `payload` value as written on the line.
//! Verification hashes those bytes and never re-serializes a parsed value,
//! because a decimal that is parsed and printed again can come out as
//! different digits. `prev` is the previous record's `digest`; the first
//! record links to 64 zeros.
//!
//! Editing a record, reordering records, or removing a record from the
//! middle breaks the chain at a specific `seq`. The chain does not detect
//! records removed from the end of the file, or the file being deleted,
//! because what remains is still a valid chain. A running supervisor knows
//! how many records the ledger held and reports a shorter or missing file;
//! that covers only removals during its lifetime. The digests use no secret,
//! so a writer who recomputes every later digest after an edit also produces
//! a valid chain.
//!
//! [`AuditLedger::check`] verifies the chain on every integrity tick and logs
//! and records each break once. [`TransitionObserver`] records each
//! generation's decision and each epoch's contract hash once, and records a
//! `history_changed` alarm when the orchestrator's records later state a
//! different value or no longer state one. [`LedgerHistory`] loads what the
//! ledger holds, so a restart records nothing twice.
//!
//! The ledger records and alarms; it never blocks a promotion or writes the
//! orchestrator's state. Writing is opt-in: a supervisor started without a
//! ledger directory writes nothing.

use crate::sha256;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;
use std::collections::{HashMap, HashSet};
use std::io::Write;
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use tracing::warn;

/// The all-zero digest the genesis record links back to.
pub const GENESIS_PREV: &str = "0000000000000000000000000000000000000000000000000000000000000000";

/// The append-only ledger file's basename inside the ledger directory.
pub const LEDGER_FILE: &str = "audit_ledger.jsonl";

/// The longest prefix of a removed partial line that its record keeps.
const REMOVED_TEXT_LIMIT: usize = 4096;

/// A typed ledger record kind. Serialized in `snake_case`; new kinds are
/// additive, because the digest covers the raw kind string and the verifier
/// checks a record whose kind it does not recognize.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecordKind {
    /// The supervisor started and opened (or created) this ledger.
    SupervisorStart,
    /// A watchdog escalation (SIGTERM/SIGKILL) was taken.
    WatchdogAction,
    /// A generation's promote/reject decision, recorded once.
    DecisionObserved,
    /// An epoch's contract hash, recorded once.
    ContractChange,
    /// A diff-containment quarantine finding.
    DiffContainmentAlert,
    /// A promotion-gatekeeping contradiction.
    PromotionContradiction,
    /// An index-vs-canonical divergence finding.
    DivergenceFinding,
    /// A recorded decision or contract hash that the orchestrator's records
    /// later state differently.
    HistoryChanged,
    /// A problem with the ledger file itself: a broken chain, or a partial
    /// final line removed when the ledger was opened.
    LedgerIntegrity,
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
            RecordKind::HistoryChanged => "history_changed",
            RecordKind::LedgerIntegrity => "ledger_integrity",
        }
    }
}

/// One persisted ledger record, parsed for reading its fields.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Record {
    pub seq: u64,
    /// The previous record's `digest`; genesis links to [`GENESIS_PREV`].
    pub prev: String,
    /// RFC-3339 UTC timestamp when the record was appended.
    pub ts: String,
    pub kind: RecordKind,
    pub payload: serde_json::Value,
    /// `SHA-256(seq ‖ prev ‖ ts ‖ kind ‖ payload)`, hex.
    pub digest: String,
}

/// One ledger line with its payload kept as the exact bytes that were hashed.
/// Appending writes this form and verification reads it, so both sides hash
/// the same bytes.
#[derive(Serialize, Deserialize)]
struct Line<'a> {
    seq: u64,
    prev: String,
    ts: String,
    kind: String,
    #[serde(borrow)]
    payload: &'a RawValue,
    digest: String,
}

/// Compute a record's digest over its chained preimage.
///
/// The preimage binds `seq`, the `prev` link, `ts`, the `kind` string and the
/// payload bytes, separated by `0x1f`, so none of them can be altered or
/// shifted across a field boundary without changing the digest.
fn compute_digest(seq: u64, prev: &str, ts: &str, kind: &str, payload: &[u8]) -> String {
    let mut preimage = Vec::with_capacity(payload.len() + 96);
    preimage.extend_from_slice(&seq.to_be_bytes());
    preimage.push(0x1f); // unit separator between fields
    preimage.extend_from_slice(prev.as_bytes());
    preimage.push(0x1f);
    preimage.extend_from_slice(ts.as_bytes());
    preimage.push(0x1f);
    preimage.extend_from_slice(kind.as_bytes());
    preimage.push(0x1f);
    preimage.extend_from_slice(payload);
    sha256::hex_digest(&preimage)
}

/// A persisted, append-only, hash-chained audit ledger the supervisor owns.
///
/// Thread-safe (a `Mutex` guards the append cursor) and shared by `Arc`. The
/// in-memory `tail` (next seq + last digest) lets a new record chain onto the
/// previous one without re-reading the file; it is seeded from the file on
/// construction so a restart continues the existing chain.
#[derive(Debug)]
pub struct AuditLedger {
    path: PathBuf,
    state: Mutex<Tail>,
    /// The partial final line removed when the ledger was opened, described
    /// for `/statusz` for the life of the process.
    partial_final_line: Option<String>,
    /// Set once [`AuditLedger::check`] has logged a break in this process.
    break_logged: AtomicBool,
    /// The `first_break_seq` of each break the ledger records, loaded at open,
    /// so a standing break is recorded once across restarts.
    recorded_breaks: Mutex<HashSet<Option<u64>>>,
}

#[derive(Debug, Clone)]
struct Tail {
    /// The seq the NEXT appended record will carry.
    next_seq: u64,
    /// The digest the next record links to (genesis or the last record's).
    prev: String,
}

impl AuditLedger {
    /// Open (or create) the ledger at `dir/audit_ledger.jsonl` and continue
    /// its chain.
    ///
    /// A final line that does not parse as a record is removed, because a
    /// crash in the middle of an append leaves one and the next record must
    /// chain onto the last complete record. The removal is reported: a WARN
    /// log line, a `ledger_integrity` record that keeps the removed bytes, and
    /// the `partial_final_line` field of every later [`VerifyReport`]. The
    /// chain is then verified once, through [`AuditLedger::check`], and the
    /// tail is seeded from the last record.
    ///
    /// Best-effort: a directory that cannot be created, or a file that cannot
    /// be read, degrades to an in-memory tail starting at genesis. The
    /// supervisor never fails to start over a ledger problem.
    pub fn open(dir: &Path) -> Self {
        if let Err(e) = std::fs::create_dir_all(dir) {
            warn!(?dir, error=%e, "could not create audit-ledger dir; ledger degraded");
        }
        let path = dir.join(LEDGER_FILE);
        let removed = repair_torn_tail(&path);
        let partial_final_line = removed.as_ref().map(|bytes| {
            format!(
                "removed a {}-byte partial final line when the ledger was opened",
                bytes.len()
            )
        });
        let recorded_breaks = read_records(&path)
            .into_iter()
            .filter(|rec| rec.kind == RecordKind::LedgerIntegrity)
            .filter_map(|rec| rec.payload.get("first_break_seq").map(|v| v.as_u64()))
            .collect();
        let ledger = Self {
            state: Mutex::new(Self::seed_tail(&path)),
            path,
            partial_final_line,
            break_logged: AtomicBool::new(false),
            recorded_breaks: Mutex::new(recorded_breaks),
        };
        if let Some(bytes) = removed {
            warn!(
                path=?ledger.path,
                removed_bytes = bytes.len(),
                "audit ledger ended in a partial line; removed it and recorded its bytes",
            );
            let text = String::from_utf8_lossy(&bytes[..bytes.len().min(REMOVED_TEXT_LIMIT)]);
            ledger.append(
                RecordKind::LedgerIntegrity,
                serde_json::json!({
                    "finding": "partial_final_line",
                    "removed_bytes": bytes.len(),
                    "removed_sha256": sha256::hex_digest(&bytes),
                    "removed_text": text,
                }),
            );
        }
        ledger.check();
        ledger
    }

    /// Read the existing file (if any) and derive the chain tail: the seq the
    /// next record should carry and the digest it should link to. A missing
    /// or empty file seeds genesis.
    fn seed_tail(path: &Path) -> Tail {
        let mut tail = Tail {
            next_seq: 0,
            prev: GENESIS_PREV.to_string(),
        };
        let bytes = std::fs::read(path).unwrap_or_default();
        for line in bytes.split(|b| *b == b'\n') {
            if let Ok(rec) = serde_json::from_slice::<Line>(line) {
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
    /// The payload is serialized once; those bytes are both hashed and
    /// written. Best-effort durability: the line is written and fsynced; an
    /// I/O error is logged and `None` returned, and the in-memory tail is NOT
    /// advanced (so the next append retries the same seq/prev rather than
    /// chaining onto a record that never reached disk).
    pub fn append(&self, kind: RecordKind, payload: serde_json::Value) -> Option<String> {
        let mut state = match self.state.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        };
        let seq = state.next_seq;
        let ts = Utc::now().to_rfc3339();
        let payload = match serde_json::value::to_raw_value(&payload) {
            Ok(p) => p,
            Err(e) => {
                warn!(error=%e, "audit-ledger payload serialization failed");
                return None;
            }
        };
        let digest = compute_digest(
            seq,
            &state.prev,
            &ts,
            kind.as_str(),
            payload.get().as_bytes(),
        );
        let line = Line {
            seq,
            prev: state.prev.clone(),
            ts,
            kind: kind.as_str().to_string(),
            payload: &payload,
            digest: digest.clone(),
        };
        let mut text = match serde_json::to_string(&line) {
            Ok(s) => s,
            Err(e) => {
                warn!(error=%e, "audit-ledger record serialization failed");
                return None;
            }
        };
        text.push('\n');
        match self.write_line(text.as_bytes()) {
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
    /// Every append ends in a newline, so a file that does not was changed by
    /// something else; a newline is written first so that foreign line stays
    /// a visible chain break and the new record stays parseable. On an I/O
    /// error the file is cut back to its length before the write, so a failed
    /// append leaves no partial line. `sync_all` after the write: the ledger
    /// appends a handful of records per run, and an audit record that is lost
    /// on power loss defeats its purpose.
    fn write_line(&self, bytes: &[u8]) -> std::io::Result<()> {
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .append(true)
            .open(&self.path)?;
        let len = f.metadata()?.len();
        let mut write = || {
            let mut last = *b"\n";
            if len > 0 {
                f.read_exact_at(&mut last, len - 1)?;
            }
            if last[0] != b'\n' {
                f.write_all(b"\n")?;
            }
            f.write_all(bytes)?;
            f.flush()?;
            f.sync_all()
        };
        let result = write();
        if result.is_err() {
            let _ = f.set_len(len);
        }
        result
    }

    /// Walk the persisted chain and verify every record's digest and `prev`
    /// link. Reads the file fresh (not the in-memory tail) so it detects
    /// out-of-band edits since the last append. The append lock is held for
    /// the walk, so a concurrent append is never read half-written.
    ///
    /// An intact chain that holds fewer records than this process knows the
    /// ledger held, or a missing file, is reported as records removed from the
    /// end. That comparison covers only removals while this supervisor runs:
    /// a restarted supervisor takes the file it finds as the full ledger.
    pub fn verify(&self) -> VerifyReport {
        let state = match self.state.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        };
        let mut report = VerifyReport {
            partial_final_line: self.partial_final_line.clone(),
            ..verify_chain(&self.path)
        };
        let held = state.next_seq;
        if report.intact && report.records < held {
            report.intact = false;
            report.first_break_seq = Some(report.records);
            report.break_reason = Some(if self.path.exists() {
                format!(
                    "ledger ends after {} records, but it held {held} while this supervisor ran; \
records were removed from the end",
                    report.records
                )
            } else {
                format!(
                    "ledger file is missing, but it held {held} records while this supervisor ran"
                )
            });
        }
        report
    }

    /// Verify the chain and report each break once.
    ///
    /// The integrity loop calls this every tick. A break whose
    /// `first_break_seq` the ledger does not yet record is logged as a WARN
    /// and recorded as a `ledger_integrity` record (`finding: chain_break`).
    /// A break the ledger already records is not recorded again, in this
    /// process or after a restart; it is logged once per process.
    pub fn check(&self) -> VerifyReport {
        let report = self.verify();
        if report.intact {
            self.break_logged.store(false, Ordering::Relaxed);
            return report;
        }
        let new = match self.recorded_breaks.lock() {
            Ok(mut g) => g.insert(report.first_break_seq),
            Err(p) => p.into_inner().insert(report.first_break_seq),
        };
        if !self.break_logged.swap(true, Ordering::Relaxed) || new {
            warn!(
                path=?self.path,
                first_break_seq = ?report.first_break_seq,
                reason = ?report.break_reason,
                records = report.records,
                "AUDIT-LEDGER CHAIN BREAK: the ledger file was changed outside the supervisor",
            );
        }
        if new {
            self.append(
                RecordKind::LedgerIntegrity,
                serde_json::json!({
                    "finding": "chain_break",
                    "first_break_seq": report.first_break_seq,
                    "detail": report.break_reason,
                }),
            );
        }
        report
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
    /// The partial final line removed when the ledger was opened, when there
    /// was one. Its bytes are kept in a `ledger_integrity` record.
    pub partial_final_line: Option<String>,
}

/// Truncate a torn (half-written) FINAL line off the ledger file and return
/// the removed bytes.
///
/// A crash between `write_all` and the bytes reaching disk can leave the
/// file ending in a partial record. Removing it lets the next append chain
/// onto the last COMPLETE record instead of stacking a valid record after
/// garbage. The caller reports what was removed.
///
/// Only the TRAILING unparseable line is dropped. An unparseable line in the
/// *middle* of the file cannot be a torn append and is left in place for
/// `verify_chain` to flag as a break. Returns `None` when the file is
/// absent, empty, or ends in a complete record. Best-effort: an I/O failure
/// leaves the file untouched (the ledger never blocks boot).
pub fn repair_torn_tail(path: &Path) -> Option<Vec<u8>> {
    // Operate on BYTES: a torn append can split a multi-byte UTF-8
    // character, which would make a string read fail outright.
    let bytes = std::fs::read(path).ok()?;
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
    if serde_json::from_slice::<Line>(bytes[start..].trim_ascii_end()).is_ok() {
        return None; // complete final record — nothing torn.
    }
    let file = std::fs::OpenOptions::new().write(true).open(path).ok()?;
    file.set_len(start as u64).ok()?;
    let _ = file.sync_all();
    Some(bytes[start..].to_vec())
}

/// Verify the hash-chain in the ledger file at `path`.
///
/// An absent or empty file is reported intact with zero records (there is
/// nothing to tamper with). A line that fails to parse, a digest that does
/// not recompute over the line's own payload bytes, a `prev` link that does
/// not match the prior digest, or a non-contiguous `seq` are all chain breaks
/// pinned to the first seq that is missing or fails.
pub fn verify_chain(path: &Path) -> VerifyReport {
    let bytes = match std::fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return VerifyReport {
                intact: true,
                ..Default::default()
            };
        }
        Err(e) => {
            return VerifyReport {
                break_reason: Some(format!("ledger unreadable: {e}")),
                ..Default::default()
            };
        }
    };

    let mut records: u64 = 0;
    let mut expected_prev = GENESIS_PREV.to_string();
    let broken = |records, seq, reason| VerifyReport {
        intact: false,
        records,
        first_break_seq: Some(seq),
        break_reason: Some(reason),
        partial_final_line: None,
    };

    for (index, raw) in bytes.split(|b| *b == b'\n').enumerate() {
        if raw.trim_ascii().is_empty() {
            continue;
        }
        let rec: Line = match serde_json::from_slice(raw) {
            Ok(r) => r,
            Err(e) => {
                let reason = format!("line {} is not a valid record: {e}", index + 1);
                return broken(records, records, reason);
            }
        };
        // seq must be contiguous from 0.
        if rec.seq != records {
            let reason = format!("seq discontinuity: expected {records}, found {}", rec.seq);
            return broken(records, records, reason);
        }
        // prev must link to the previous record's digest (or genesis).
        if rec.prev != expected_prev {
            return broken(
                records,
                rec.seq,
                format!("seq {} prev-link broken", rec.seq),
            );
        }
        // The recorded digest must recompute over the record's own bytes.
        let payload = rec.payload.get().as_bytes();
        if compute_digest(rec.seq, &rec.prev, &rec.ts, &rec.kind, payload) != rec.digest {
            let reason = format!("seq {} digest mismatch (record altered)", rec.seq);
            return broken(records, rec.seq, reason);
        }
        expected_prev = rec.digest;
        records += 1;
    }

    VerifyReport {
        intact: true,
        records,
        ..Default::default()
    }
}

/// Records each generation's decision and each epoch's contract hash once,
/// and alarms when the orchestrator's records later state a different value
/// or no longer state one.
///
/// The orchestrator can rewrite its own records, so the supervisor reads the
/// same values it publishes (the per-generation decision and the epoch
/// `contract_hash`) and stamps each into the ledger the first time it sees
/// it. A later different value is a rewritten history: the observer logs a
/// WARN and appends one `history_changed` record per distinct observed
/// value. The comparison sees only the disagreement, so an edit to the
/// ledger's own record raises the same alarm. A forgery that rewrites a
/// decision together with the scores that justify it passes the
/// promotion-gate audit, which checks only that the two agree; it does not
/// pass this comparison. A recorded generation whose decision no longer
/// resolves, or a recorded epoch that states no contract hash, raises the
/// alarm with observed value `absent` once it has been missing on two
/// consecutive ticks.
///
/// [`LedgerHistory::load`] fills an observer from an existing ledger, so a
/// restarted supervisor neither records a decision twice nor raises the same
/// alarm twice.
#[derive(Debug, Default)]
pub struct TransitionObserver {
    /// The decision first recorded per `(epoch_id, generation_id)`;
    /// `true` is a promotion.
    recorded_decisions: HashMap<(String, String), bool>,
    /// The contract hash first recorded per epoch.
    recorded_contract: HashMap<String, String>,
    /// Alarms already recorded, keyed by
    /// `(field, epoch_id, generation_id or "", observed value)`.
    alarms: HashSet<(String, String, String, String)>,
    /// Recorded values that did not resolve on the previous tick, keyed by
    /// `(field, epoch_id, generation_id or "")`.
    absent: HashSet<(String, String, String)>,
}

fn decision_label(promoted: bool) -> &'static str {
    if promoted {
        "promote"
    } else {
        "reject"
    }
}

impl TransitionObserver {
    pub fn new() -> Self {
        Self::default()
    }

    /// The epochs whose contract hash is recorded.
    pub fn recorded_epochs(&self) -> Vec<String> {
        self.recorded_contract.keys().cloned().collect()
    }

    /// Record each newly resolved generation decision, and alarm on a
    /// recorded decision that changed.
    ///
    /// `generations` is `(epoch_id, generation_id, promoted)` where `promoted`
    /// is `None` while in flight and `Some(bool)` once resolved. Returns the
    /// number of decisions newly recorded.
    pub fn observe_decisions<'a, I>(&mut self, ledger: &AuditLedger, generations: I) -> usize
    where
        I: IntoIterator<Item = (&'a str, &'a str, Option<bool>)>,
    {
        let mut recorded = 0;
        let mut resolved = HashSet::new();
        for (epoch_id, generation_id, promoted) in generations {
            let Some(promoted) = promoted else {
                continue; // still in flight — no decision yet.
            };
            let key = (epoch_id.to_string(), generation_id.to_string());
            resolved.insert(key.clone());
            match self.recorded_decisions.get(&key) {
                Some(&first) if first != promoted => self.alarm(
                    ledger,
                    "decision",
                    epoch_id,
                    Some(generation_id),
                    decision_label(first),
                    decision_label(promoted),
                ),
                Some(_) => {}
                None => {
                    self.recorded_decisions.insert(key, promoted);
                    ledger.append(
                        RecordKind::DecisionObserved,
                        serde_json::json!({
                            "epoch_id": epoch_id,
                            "generation_id": generation_id,
                            "decision": decision_label(promoted),
                        }),
                    );
                    recorded += 1;
                }
            }
        }
        let missing: Vec<_> = self
            .recorded_decisions
            .iter()
            .filter(|(key, _)| !resolved.contains(*key))
            .map(|((epoch_id, generation_id), &first)| {
                let recorded = decision_label(first).to_string();
                (epoch_id.clone(), Some(generation_id.clone()), recorded)
            })
            .collect();
        self.absences(ledger, "decision", missing);
        recorded
    }

    /// Observe the contract hash of each `(epoch_id, hash)` pair, where `hash`
    /// is `None` when the epoch states none. A recorded epoch that states no
    /// hash on two consecutive calls raises an alarm with observed value
    /// `absent`.
    pub fn observe_contracts<I>(&mut self, ledger: &AuditLedger, epochs: I)
    where
        I: IntoIterator<Item = (String, Option<String>)>,
    {
        let mut missing = Vec::new();
        for (epoch_id, hash) in epochs {
            match (
                hash.filter(|h| !h.is_empty()),
                self.recorded_contract.get(&epoch_id),
            ) {
                (Some(hash), _) => {
                    self.observe_contract(ledger, &epoch_id, &hash);
                }
                (None, Some(first)) => missing.push((epoch_id, None, first.clone())),
                (None, None) => {}
            }
        }
        self.absences(ledger, "contract_hash", missing);
    }

    /// Alarm, with observed value `absent`, on each recorded value of `field`
    /// that is missing now and was missing on the previous tick. Requiring two
    /// consecutive ticks keeps a read that races a writer from raising an
    /// alarm.
    fn absences(
        &mut self,
        ledger: &AuditLedger,
        field: &str,
        missing: Vec<(String, Option<String>, String)>,
    ) {
        let mut now = HashSet::new();
        for (epoch_id, generation_id, recorded) in missing {
            let gen_key = generation_id.clone().unwrap_or_default();
            let key = (field.to_string(), epoch_id.clone(), gen_key);
            if self.absent.contains(&key) {
                let generation_id = generation_id.as_deref();
                self.alarm(ledger, field, &epoch_id, generation_id, &recorded, "absent");
            }
            now.insert(key);
        }
        self.absent.retain(|key| key.0 != field);
        self.absent.extend(now);
    }

    /// Record an epoch's contract hash the first time it is seen, and alarm
    /// when a recorded epoch later states a different hash. An epoch's
    /// contract is frozen when the epoch is created, so a change is a
    /// rewrite. Returns `true` when the hash was newly recorded.
    pub fn observe_contract(
        &mut self,
        ledger: &AuditLedger,
        epoch_id: &str,
        contract_hash: &str,
    ) -> bool {
        if contract_hash.is_empty() {
            return false;
        }
        match self.recorded_contract.get(epoch_id).cloned() {
            Some(first) if first != contract_hash => {
                self.alarm(
                    ledger,
                    "contract_hash",
                    epoch_id,
                    None,
                    &first,
                    contract_hash,
                );
                false
            }
            Some(_) => false,
            None => {
                self.recorded_contract
                    .insert(epoch_id.to_string(), contract_hash.to_string());
                ledger.append(
                    RecordKind::ContractChange,
                    serde_json::json!({
                        "epoch_id": epoch_id,
                        "contract_hash": contract_hash,
                    }),
                );
                true
            }
        }
    }

    /// Log and record one `history_changed` alarm per distinct observed value.
    fn alarm(
        &mut self,
        ledger: &AuditLedger,
        field: &str,
        epoch_id: &str,
        generation_id: Option<&str>,
        recorded: &str,
        observed: &str,
    ) {
        let key = (
            field.to_string(),
            epoch_id.to_string(),
            generation_id.unwrap_or_default().to_string(),
            observed.to_string(),
        );
        if !self.alarms.insert(key) {
            return;
        }
        warn!(
            field,
            epoch_id,
            generation_id,
            recorded,
            observed,
            "LEDGER HISTORY ALERT: the orchestrator's records and the ledger state different values",
        );
        ledger.append(
            RecordKind::HistoryChanged,
            serde_json::json!({
                "field": field,
                "epoch_id": epoch_id,
                "generation_id": generation_id,
                "recorded": recorded,
                "observed": observed,
            }),
        );
    }
}

/// What the ledger already records, loaded when the integrity loop starts
/// and after a scan panics, so the loop records no decision, contract hash,
/// alarm or finding a second time.
#[derive(Debug, Default)]
pub struct LedgerHistory {
    /// The observer, holding each recorded decision, contract hash and
    /// `history_changed` alarm. The first recorded value for each generation
    /// or epoch is the reference later observations are compared against.
    pub observer: TransitionObserver,
    /// `(epoch_id, generation_id)` of each recorded diff-containment alert.
    pub diff_alerts: HashSet<(String, String)>,
    /// `(epoch_id, challenger_generation_id)` of each recorded promotion
    /// contradiction.
    pub contradictions: HashSet<(String, String)>,
    /// `(code, generation_id)` of each recorded divergence finding.
    pub divergences: HashSet<(String, Option<String>)>,
}

impl LedgerHistory {
    /// Read every parseable record of `ledger`. Lines that do not parse are
    /// skipped; the chain check reports them.
    pub fn load(ledger: &AuditLedger) -> Self {
        let mut history = Self::default();
        let observer = &mut history.observer;
        for rec in read_records(ledger.path()) {
            let text = |key: &str| rec.payload.get(key).and_then(|v| v.as_str());
            let field = |key: &str| text(key).unwrap_or_default().to_string();
            let pair = |a: &str, b: &str| (field(a), field(b));
            match rec.kind {
                RecordKind::DecisionObserved => {
                    let promoted = field("decision") == "promote";
                    let key = pair("epoch_id", "generation_id");
                    observer.recorded_decisions.entry(key).or_insert(promoted);
                }
                RecordKind::ContractChange if !field("contract_hash").is_empty() => {
                    let (epoch_id, hash) = pair("epoch_id", "contract_hash");
                    observer.recorded_contract.entry(epoch_id).or_insert(hash);
                }
                RecordKind::HistoryChanged => {
                    let (name, epoch_id) = pair("field", "epoch_id");
                    let (generation_id, observed) = pair("generation_id", "observed");
                    observer
                        .alarms
                        .insert((name, epoch_id, generation_id, observed));
                }
                RecordKind::DiffContainmentAlert => {
                    history
                        .diff_alerts
                        .insert(pair("epoch_id", "generation_id"));
                }
                RecordKind::PromotionContradiction => {
                    let key = pair("epoch_id", "challenger_generation_id");
                    history.contradictions.insert(key);
                }
                RecordKind::DivergenceFinding => {
                    let generation_id = text("generation_id").map(str::to_string);
                    history.divergences.insert((field("code"), generation_id));
                }
                _ => {}
            }
        }
        history
    }
}

/// Every line of the ledger at `path` that parses as a record, in file order.
fn read_records(path: &Path) -> Vec<Record> {
    let bytes = std::fs::read(path).unwrap_or_default();
    bytes
        .split(|b| *b == b'\n')
        .filter_map(|line| serde_json::from_slice(line).ok())
        .collect()
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
        assert_eq!(report.first_break_seq, Some(1), "the first missing seq");
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
    fn observer_records_contract_once_and_alarms_on_a_change() {
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        let mut obs = TransitionObserver::new();
        assert!(obs.observe_contract(&led, "e1", "hashA"));
        // Same value again → no new record.
        assert!(!obs.observe_contract(&led, "e1", "hashA"));
        // A changed value for the same epoch is a rewrite: one alarm, not a
        // second contract record, and no repeat on the next poll.
        assert!(!obs.observe_contract(&led, "e1", "hashB"));
        assert!(!obs.observe_contract(&led, "e1", "hashB"));
        // Empty hash is ignored.
        assert!(!obs.observe_contract(&led, "e1", ""));
        let report = led.verify();
        assert!(report.intact);
        let kinds: Vec<RecordKind> = std::fs::read_to_string(led.path())
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str::<Record>(l).unwrap().kind)
            .collect();
        assert_eq!(
            kinds,
            [RecordKind::ContractChange, RecordKind::HistoryChanged]
        );
    }

    #[test]
    fn an_absence_must_persist_two_ticks_to_alarm() {
        let (_t, dir) = ledger_dir();
        let led = AuditLedger::open(&dir);
        let mut obs = TransitionObserver::new();
        let both = || vec![("e1", "v1", Some(true)), ("e1", "v2", Some(false))];
        obs.observe_decisions(&led, both());
        // One tick without v2, then v2 again: a transient read, no alarm.
        obs.observe_decisions(&led, vec![("e1", "v1", Some(true))]);
        obs.observe_decisions(&led, both());
        // Two consecutive ticks without v2: one alarm, not repeated.
        for _ in 0..3 {
            obs.observe_decisions(&led, vec![("e1", "v1", Some(true)), ("e1", "v2", None)]);
        }
        let alarms: Vec<Record> = std::fs::read_to_string(led.path())
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str::<Record>(l).unwrap())
            .filter(|r| r.kind == RecordKind::HistoryChanged)
            .collect();
        assert_eq!(alarms.len(), 1);
        assert_eq!(alarms[0].payload["generation_id"], "v2");
        assert_eq!(alarms[0].payload["observed"], "absent");
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
        // The torn line is replaced by a record that keeps its bytes, the
        // report names the removal, and the chain verifies clean.
        let report = led2.verify();
        assert!(report.intact, "post-crash chain must verify: {report:?}");
        assert_eq!(report.records, 3);
        assert!(report.partial_final_line.is_some());
        let text = std::fs::read_to_string(led2.path()).unwrap();
        let recs: Vec<Record> = text
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(recs[2].kind, RecordKind::LedgerIntegrity);
        assert_eq!(recs[2].prev, recs[1].digest);
        assert_eq!(recs[2].payload["removed_text"], "{\"seq\":2,\"prev\":\"abc");
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
        // Opening recorded the break once; the append follows it.
        let text = std::fs::read_to_string(led2.path()).unwrap();
        let kinds: Vec<RecordKind> = text
            .lines()
            .map(|l| serde_json::from_str::<Record>(l).unwrap().kind)
            .collect();
        assert_eq!(kinds.len(), 4);
        assert_eq!(kinds[2], RecordKind::LedgerIntegrity);
        assert_eq!(led2.check().first_break_seq, Some(0));
        let text = std::fs::read_to_string(led2.path()).unwrap();
        assert_eq!(text.lines().count(), 4, "a standing break is recorded once");
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
