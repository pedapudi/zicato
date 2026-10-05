//! The audit ledger against the running integrity loop: digests that survive
//! decimal payloads, restarts that record nothing twice, alarms on rewritten
//! or erased decisions and contract hashes, and reports of a broken chain,
//! records removed while the supervisor runs, or a partial final line.

use serde_json::Value;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tempfile::TempDir;
use tokio::sync::broadcast;
use zicato_supervisor::ledger::{verify_chain, AuditLedger, RecordKind};
use zicato_supervisor::{action_log::WatchdogLog, reader::WorkspacePaths, watchdog};

/// A workspace whose epoch `e1` holds a promoted `v1` and a rejected `v2`.
fn workspace() -> (TempDir, WorkspacePaths, PathBuf) {
    let tmp = TempDir::new().unwrap();
    let ws = tmp.path().join("ws");
    for dir in [
        "runtime/active_runs",
        "runtime/control",
        "epochs/e1/generations/v1",
    ] {
        std::fs::create_dir_all(ws.join(dir)).unwrap();
    }
    std::fs::create_dir_all(ws.join("epochs/e1/generations/v2")).unwrap();
    std::fs::write(ws.join("current_epoch"), "e1").unwrap();
    write_contract(&ws, "e1", "hash-a");
    write_lineage(&ws, false);
    let ledger_dir = tmp.path().join("ledger");
    (tmp, WorkspacePaths::new(ws), ledger_dir)
}

fn write_contract(ws: &Path, epoch: &str, hash: &str) {
    let body = serde_json::json!({"id": epoch, "contract_hash": hash});
    std::fs::create_dir_all(ws.join("epochs").join(epoch)).unwrap();
    std::fs::write(
        ws.join("epochs").join(epoch).join("config.json"),
        body.to_string(),
    )
    .unwrap();
}

fn write_lineage(ws: &Path, v2_promoted: bool) {
    let body = serde_json::json!({"epochs": [{"id": "e1", "generations": [
        {"id": "v1", "promoted": true},
        {"id": "v2", "promoted": v2_promoted},
    ]}]});
    std::fs::write(ws.join("lineage.json"), body.to_string()).unwrap();
}

type Running = (tokio::task::JoinHandle<()>, broadcast::Sender<()>);

/// Start the supervisor's loops with a ledger and 50 ms integrity ticks.
fn start(paths: &WorkspacePaths, ledger_dir: &Path) -> Running {
    start_with(paths, ledger_dir, false)
}

/// Like [`start`], with the promotion-gate and divergence audits on when
/// `audits` is set.
fn start_with(paths: &WorkspacePaths, ledger_dir: &Path, audits: bool) -> Running {
    let ledger = Arc::new(AuditLedger::open(ledger_dir));
    let (shutdown, _) = broadcast::channel(4);
    let task = tokio::spawn(watchdog::runs_loop(
        paths.clone(),
        watchdog::Thresholds::default(),
        Duration::from_millis(50),
        Arc::new(WatchdogLog::new()),
        Some(ledger),
        watchdog::DiffContainmentConfig {
            enabled: false,
            findings: Default::default(),
        },
        watchdog::PromotionGateConfig {
            enabled: audits,
            findings: Default::default(),
        },
        watchdog::DivergenceConfig {
            enabled: audits,
            findings: Default::default(),
            stuck_age_seconds: 3600,
        },
        shutdown.clone(),
    ));
    (task, shutdown)
}

async fn stop((task, shutdown): Running) {
    shutdown.send(()).unwrap();
    task.await.unwrap();
}

/// Run the supervisor's loops for `ticks` integrity ticks.
async fn supervise(paths: &WorkspacePaths, ledger_dir: &Path, ticks: u64) {
    let running = start(paths, ledger_dir);
    tokio::time::sleep(Duration::from_millis(50 * ticks)).await;
    stop(running).await;
}

fn records(ledger_dir: &Path) -> Vec<Value> {
    let text = std::fs::read_to_string(ledger_dir.join("audit_ledger.jsonl")).unwrap();
    text.lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

fn of_kind(ledger_dir: &Path, kind: &str) -> Vec<Value> {
    let all = records(ledger_dir);
    all.into_iter().filter(|r| r["kind"] == kind).collect()
}

#[test]
fn decimal_payloads_verify_intact() {
    // 20,000 pseudo-random decimals of loss-delta scale, 100 per record. A
    // digest recomputed from a re-parsed payload fails for some of them.
    let tmp = TempDir::new().unwrap();
    let ledger = AuditLedger::open(tmp.path());
    let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
    for _ in 0..200 {
        let values: Vec<f64> = (0..100)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                (state as f64 / u64::MAX as f64) * 4.0 - 2.0
            })
            .collect();
        let payload = serde_json::json!({"delta_scalar": values, "promote_margin": 0.01});
        ledger
            .append(RecordKind::PromotionContradiction, payload)
            .unwrap();
    }
    for x in [
        0.1 + 0.2,
        -1.2000000000000002,
        1.0 / 3.0,
        5e-324,
        1.7976931348623157e308,
    ] {
        let payload = serde_json::json!({"delta_scalar": x});
        ledger
            .append(RecordKind::PromotionContradiction, payload)
            .unwrap();
    }
    let report = verify_chain(ledger.path());
    assert!(report.intact, "{report:?}");
    assert_eq!(report.records, 205);
}

#[tokio::test]
async fn a_restarted_supervisor_records_each_decision_and_contract_once() {
    let (_t, paths, ledger_dir) = workspace();
    supervise(&paths, &ledger_dir, 4).await;
    assert_eq!(of_kind(&ledger_dir, "decision_observed").len(), 2);
    assert_eq!(of_kind(&ledger_dir, "contract_change").len(), 1);

    supervise(&paths, &ledger_dir, 4).await;
    assert_eq!(of_kind(&ledger_dir, "decision_observed").len(), 2);
    assert_eq!(of_kind(&ledger_dir, "contract_change").len(), 1);
    assert!(verify_chain(&ledger_dir.join("audit_ledger.jsonl")).intact);
}

#[tokio::test]
async fn a_rewritten_decision_raises_one_alarm_across_restarts() {
    let (_t, paths, ledger_dir) = workspace();
    supervise(&paths, &ledger_dir, 4).await;
    // Rewrite the rejected v2 as a promotion, as a forger who also rewrites
    // the scores would.
    write_lineage(&paths.workspace, true);
    supervise(&paths, &ledger_dir, 4).await;
    supervise(&paths, &ledger_dir, 4).await;

    let alarms = of_kind(&ledger_dir, "history_changed");
    assert_eq!(alarms.len(), 1, "{alarms:?}");
    let alarm = &alarms[0]["payload"];
    assert_eq!(alarm["field"], "decision");
    assert_eq!(alarm["generation_id"], "v2");
    assert_eq!(alarm["recorded"], "reject");
    assert_eq!(alarm["observed"], "promote");
    assert_eq!(of_kind(&ledger_dir, "decision_observed").len(), 2);
}

#[tokio::test]
async fn a_rewritten_contract_hash_raises_an_alarm_for_past_epochs_too() {
    let (_t, paths, ledger_dir) = workspace();
    supervise(&paths, &ledger_dir, 4).await;
    // A later epoch becomes current, then the first epoch's frozen contract
    // is rewritten.
    write_contract(&paths.workspace, "e2", "hash-c");
    std::fs::write(paths.workspace.join("current_epoch"), "e2").unwrap();
    write_contract(&paths.workspace, "e1", "hash-b");
    supervise(&paths, &ledger_dir, 4).await;

    let contracts = of_kind(&ledger_dir, "contract_change");
    let epochs: Vec<&Value> = contracts
        .iter()
        .map(|r| &r["payload"]["epoch_id"])
        .collect();
    assert_eq!(epochs, ["e1", "e2"]);
    let alarms = of_kind(&ledger_dir, "history_changed");
    assert_eq!(alarms.len(), 1, "{alarms:?}");
    let alarm = &alarms[0]["payload"];
    assert_eq!(alarm["field"], "contract_hash");
    assert_eq!(alarm["epoch_id"], "e1");
    assert_eq!(alarm["recorded"], "hash-a");
    assert_eq!(alarm["observed"], "hash-b");
}

#[tokio::test]
async fn an_edited_record_is_reported_once_while_running() {
    let (_t, paths, ledger_dir) = workspace();
    let running = start(&paths, &ledger_dir);
    tokio::time::sleep(Duration::from_millis(200)).await;
    // Flip the recorded decision of v1 in place, keeping its digest.
    let path = ledger_dir.join("audit_ledger.jsonl");
    let text = std::fs::read_to_string(&path).unwrap();
    assert_eq!(text.matches("\"decision\":\"promote\"").count(), 1);
    std::fs::write(
        &path,
        text.replace("\"decision\":\"promote\"", "\"decision\":\"reject\""),
    )
    .unwrap();
    tokio::time::sleep(Duration::from_millis(300)).await;
    stop(running).await;

    let breaks = of_kind(&ledger_dir, "ledger_integrity");
    assert_eq!(breaks.len(), 1, "{breaks:?}");
    assert_eq!(breaks[0]["payload"]["finding"], "chain_break");
}

#[test]
fn a_partial_final_line_is_recorded_when_the_ledger_opens() {
    let tmp = TempDir::new().unwrap();
    {
        let ledger = AuditLedger::open(tmp.path());
        ledger.append(RecordKind::SupervisorStart, serde_json::json!({}));
        ledger.append(RecordKind::WatchdogAction, serde_json::json!({"pid": 7}));
    }
    let path = tmp.path().join("audit_ledger.jsonl");
    let mut bytes = std::fs::read(&path).unwrap();
    bytes.extend_from_slice(b"{\"seq\":2,\"prev\":\"tor");
    std::fs::write(&path, bytes).unwrap();

    let ledger = AuditLedger::open(tmp.path());
    let found = of_kind(tmp.path(), "ledger_integrity");
    assert_eq!(found.len(), 1, "{found:?}");
    assert_eq!(found[0]["payload"]["finding"], "partial_final_line");
    assert_eq!(
        found[0]["payload"]["removed_text"],
        "{\"seq\":2,\"prev\":\"tor"
    );
    let report = serde_json::to_value(ledger.verify()).unwrap();
    assert_eq!(report["intact"], true);
    assert!(report["partial_final_line"].is_string(), "{report}");
}

/// An index that disagrees with the canonical files and records a promotion
/// whose loss rose, so both audits report standing findings.
fn write_contradicting_index(paths: &WorkspacePaths) {
    let conn = rusqlite::Connection::open(paths.index_db()).unwrap();
    conn.execute_batch(&format!(
        "CREATE TABLE epochs(epoch_id TEXT PRIMARY KEY, contract_hash TEXT, created_at TEXT, \
             closed INTEGER, goal TEXT, parent_epoch_id TEXT);
         CREATE TABLE generations(epoch_id TEXT, generation_id TEXT, \
             parent_generation_id TEXT, promoted INTEGER);
         CREATE TABLE experiments(epoch_id TEXT, generation_id TEXT, hypothesis_core_idea TEXT);
         CREATE TABLE tournaments(tournament_id TEXT, epoch_id TEXT, parent_generation_id TEXT, \
             child_generation_id TEXT, decision TEXT, parent_scalar REAL, child_scalar REAL, \
             delta_scalar REAL, rejection_reason TEXT, ran_at TEXT);
         INSERT INTO epochs VALUES('e1', 'hash-z', NULL, 0, NULL, NULL);
         INSERT INTO generations VALUES('e1', 'v1', NULL, 0);
         INSERT INTO tournaments VALUES('t1', 'e1', 'v0', 'v1', 'promoted', 1.0, 2.0, 1.0, '', \
             '2026-01-01T00:00:00Z');
         PRAGMA user_version = {};",
        zicato_supervisor::index_db::EXPECTED_SCHEMA_VERSION
    ))
    .unwrap();
}

async fn supervise_with_audits(paths: &WorkspacePaths, ledger_dir: &Path) {
    let running = start_with(paths, ledger_dir, true);
    tokio::time::sleep(Duration::from_millis(200)).await;
    stop(running).await;
}

#[tokio::test]
async fn restarts_record_no_standing_finding_or_break_again() {
    let (_t, paths, ledger_dir) = workspace();
    write_contradicting_index(&paths);
    supervise_with_audits(&paths, &ledger_dir).await;
    assert!(!of_kind(&ledger_dir, "promotion_contradiction").is_empty());
    assert!(!of_kind(&ledger_dir, "divergence_finding").is_empty());
    // Edit one finding's text in place so the chain stands broken too.
    let path = ledger_dir.join("audit_ledger.jsonl");
    let text = std::fs::read_to_string(&path).unwrap();
    assert!(text.contains("\"detail\":\""));
    let edited = text.replacen("\"detail\":\"", "\"detail\":\"edited ", 1);
    std::fs::write(&path, edited).unwrap();

    supervise_with_audits(&paths, &ledger_dir).await;
    let after_first_restart = records(&ledger_dir).len();
    for _ in 0..3 {
        supervise_with_audits(&paths, &ledger_dir).await;
    }
    let all = records(&ledger_dir);
    assert_eq!(
        all.len(),
        after_first_restart,
        "{:?}",
        &all[after_first_restart..]
    );
    assert_eq!(of_kind(&ledger_dir, "ledger_integrity").len(), 1);
}

#[tokio::test]
async fn an_erased_decision_and_contract_hash_raise_one_alarm_each() {
    let (_t, paths, ledger_dir) = workspace();
    supervise(&paths, &ledger_dir, 4).await;
    // v2's decision no longer resolves, and e1's config states no hash.
    let body = serde_json::json!({"epochs": [{"id": "e1", "generations": [
        {"id": "v1", "promoted": true},
        {"id": "v2", "promoted": null},
    ]}]});
    std::fs::write(paths.lineage(), body.to_string()).unwrap();
    let config = paths.epochs.join("e1/config.json");
    std::fs::write(config, serde_json::json!({"id": "e1"}).to_string()).unwrap();
    supervise(&paths, &ledger_dir, 6).await;
    supervise(&paths, &ledger_dir, 6).await;

    let alarms = of_kind(&ledger_dir, "history_changed");
    let mut seen: Vec<(String, String, String)> = alarms
        .iter()
        .map(|r| {
            let p = &r["payload"];
            let text = |k: &str| p[k].as_str().unwrap_or_default().to_string();
            (text("field"), text("recorded"), text("observed"))
        })
        .collect();
    seen.sort();
    let expected = [
        ("contract_hash", "hash-a", "absent"),
        ("decision", "reject", "absent"),
    ]
    .map(|(a, b, c)| (a.to_string(), b.to_string(), c.to_string()));
    assert_eq!(seen, expected);
}

#[tokio::test]
async fn records_removed_while_running_are_reported_once() {
    for delete in [false, true] {
        let (_t, paths, ledger_dir) = workspace();
        let running = start(&paths, &ledger_dir);
        tokio::time::sleep(Duration::from_millis(200)).await;
        let path = ledger_dir.join("audit_ledger.jsonl");
        if delete {
            std::fs::remove_file(&path).unwrap();
        } else {
            let text = std::fs::read_to_string(&path).unwrap();
            let kept: Vec<&str> = text.lines().take(1).collect();
            std::fs::write(&path, kept.join("\n") + "\n").unwrap();
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
        stop(running).await;

        let found = of_kind(&ledger_dir, "ledger_integrity");
        assert_eq!(found.len(), 1, "{found:?}");
        let detail = found[0]["payload"]["detail"].as_str().unwrap();
        let expected = if delete {
            "is missing"
        } else {
            "removed from the end"
        };
        assert!(detail.contains(expected), "{detail}");
        assert!(!verify_chain(&path).intact);
    }
}
