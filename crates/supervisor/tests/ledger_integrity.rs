//! The audit ledger against the running integrity loop: digests that survive
//! decimal payloads, restarts that do not record a decision twice, alarms on
//! rewritten decisions and contract hashes, and reports of a broken chain or
//! a partial final line.

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
            enabled: false,
            findings: Default::default(),
        },
        watchdog::DivergenceConfig {
            enabled: false,
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
