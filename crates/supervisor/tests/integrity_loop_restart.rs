//! An integrity scan that panics does not stop later scans, and the restarted
//! scans record no finding a second time. This test owns its binary's global
//! tracing subscriber, which panics on the first divergence warning.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tempfile::TempDir;
use tokio::sync::broadcast;
use tracing::field::{Field, Visit};
use tracing_subscriber::layer::{Context, Layer, SubscriberExt};
use zicato_supervisor::ledger::AuditLedger;
use zicato_supervisor::promotion_gate::PromotionGateFindings;
use zicato_supervisor::{action_log::WatchdogLog, reader::WorkspacePaths, watchdog};

/// The warning the divergence audit emits before it records a finding. The
/// promotion-gate scan runs earlier in the same tick, so its finding is
/// already in the ledger when the panic discards the scan state.
const TRIGGER: &str = "DIVERGENCE FINDING";

struct PanicOnce(AtomicBool);

struct Message(String);

impl Visit for Message {
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            self.0 = format!("{value:?}");
        }
    }
}

impl<S: tracing::Subscriber> Layer<S> for PanicOnce {
    fn on_event(&self, event: &tracing::Event<'_>, _: Context<'_, S>) {
        let mut message = Message(String::new());
        event.record(&mut message);
        if message.0.contains(TRIGGER) && !self.0.swap(true, Ordering::SeqCst) {
            panic!("injected integrity-scan panic");
        }
    }
}

#[tokio::test]
async fn an_integrity_scan_panic_does_not_stop_later_scans() {
    let subscriber = tracing_subscriber::registry().with(PanicOnce(AtomicBool::new(false)));
    tracing::subscriber::set_global_default(subscriber).unwrap();

    let tmp = TempDir::new().unwrap();
    let ws = tmp.path().join("ws");
    std::fs::create_dir_all(ws.join("runtime/active_runs")).unwrap();
    std::fs::create_dir_all(ws.join("epochs/e1")).unwrap();
    std::fs::write(
        ws.join("epochs/e1/config.json"),
        r#"{"contract_hash":"hash-a"}"#,
    )
    .unwrap();
    std::fs::write(ws.join("current_epoch"), "e1").unwrap();
    let paths = WorkspacePaths::new(ws);
    // An index whose contract hash disagrees with the epoch config, and which
    // records a promotion whose loss rose.
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
         INSERT INTO tournaments VALUES('t1', 'e1', 'v0', 'v1', 'promoted', 1.0, 2.0, 1.0, '', \
             '2026-01-01T00:00:00Z');
         PRAGMA user_version = {};",
        zicato_supervisor::index_db::EXPECTED_SCHEMA_VERSION
    ))
    .unwrap();
    drop(conn);
    let ledger_dir = tmp.path().join("ledger");

    let gate = Arc::new(PromotionGateFindings::new());
    let (shutdown, _) = broadcast::channel(4);
    let task = tokio::spawn(watchdog::runs_loop(
        paths,
        watchdog::Thresholds::default(),
        Duration::from_millis(50),
        Arc::new(WatchdogLog::new()),
        Some(Arc::new(AuditLedger::open(&ledger_dir))),
        watchdog::ContainmentConfig {
            enabled: false,
            findings: Default::default(),
        },
        watchdog::PromotionGateConfig {
            enabled: true,
            findings: gate.clone(),
        },
        watchdog::DivergenceConfig {
            enabled: true,
            findings: Default::default(),
            stuck_age_seconds: 3600,
        },
        shutdown.clone(),
    ));
    tokio::time::sleep(Duration::from_millis(400)).await;
    shutdown.send(()).unwrap();
    task.await.unwrap();

    let view = serde_json::to_value(gate.view()).unwrap();
    assert_eq!(view["scanned"], true, "no scan ran after the panic: {view}");
    assert!(view["scanned_at"].is_string(), "{view}");
    let text = std::fs::read_to_string(ledger_dir.join("audit_ledger.jsonl")).unwrap();
    let count = |kind: &str| text.matches(&format!("\"kind\":\"{kind}\"")).count();
    assert_eq!(count("promotion_contradiction"), 1, "{text}");
    assert!(count("divergence_finding") > 0, "{text}");
}
