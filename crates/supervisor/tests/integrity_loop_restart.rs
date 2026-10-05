//! An integrity scan that panics does not stop later scans. This test owns
//! its binary's global tracing subscriber, which panics on the first warning
//! an integrity scan emits.

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

/// The warning an epoch read emits when `config.json` is not a readable file.
const TRIGGER: &str = "failed to read epoch file";

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
    // A directory where the epoch's config.json belongs makes every epoch
    // read warn, and the first such warning panics.
    std::fs::create_dir_all(ws.join("epochs/e1/config.json")).unwrap();
    std::fs::write(ws.join("current_epoch"), "e1").unwrap();
    let paths = WorkspacePaths::new(ws);

    let gate = Arc::new(PromotionGateFindings::new());
    let (shutdown, _) = broadcast::channel(4);
    let task = tokio::spawn(watchdog::runs_loop(
        paths,
        watchdog::Thresholds::default(),
        Duration::from_millis(50),
        Arc::new(WatchdogLog::new()),
        Some(Arc::new(AuditLedger::open(&tmp.path().join("ledger")))),
        watchdog::DiffContainmentConfig {
            enabled: false,
            findings: Default::default(),
        },
        watchdog::PromotionGateConfig {
            enabled: true,
            findings: gate.clone(),
        },
        watchdog::DivergenceConfig {
            enabled: false,
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
}
