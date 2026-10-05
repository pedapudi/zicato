//! End-to-end tests: spin up the supervisor server against a synthetic
//! workspace, exercise the watchdog's HTTP surface (`/statusz`,
//! `/statusz.json`, `/api/audit/verify`), and check signal escalation
//! against a real child process.

use chrono::{Duration as ChDuration, Utc};
use serde_json::Value;
use std::net::{IpAddr, Ipv4Addr};
use std::sync::Arc;
use std::time::Duration;
use tempfile::TempDir;
use tokio::sync::broadcast;
use zicato_supervisor::{
    action_log::WatchdogLog, reader, server, signal as sigutil, state, watchdog,
};

fn make_workspace() -> (TempDir, reader::WorkspacePaths) {
    let tmp = TempDir::new().unwrap();
    let ws = tmp.path().to_path_buf();
    std::fs::create_dir_all(ws.join("runtime/active_runs")).unwrap();
    std::fs::create_dir_all(ws.join("runtime/control")).unwrap();
    std::fs::create_dir_all(ws.join("epochs")).unwrap();
    (tmp, reader::WorkspacePaths::new(ws))
}

fn serve_opts() -> server::ServeOptions {
    server::ServeOptions {
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        ledger: None,
        diff_findings: Arc::new(
            zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
        ),
        promotion_gate_findings: Arc::new(
            zicato_supervisor::promotion_gate::PromotionGateFindings::new(),
        ),
        divergence_findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
    }
}

async fn start_server(
    paths: reader::WorkspacePaths,
) -> (server::ServerHandle, broadcast::Sender<()>) {
    let (shutdown_tx, _) = broadcast::channel(4);
    let handle = server::serve(
        paths,
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        0, // ephemeral
        serve_opts(),
        shutdown_tx.clone(),
    )
    .await
    .unwrap();
    (handle, shutdown_tx)
}

/// Like `start_server` but with a fully-specified `ServeOptions`, so
/// `/statusz` tests can share an action log, ledger, or findings store.
async fn start_server_with(
    paths: reader::WorkspacePaths,
    options: server::ServeOptions,
) -> (server::ServerHandle, broadcast::Sender<()>) {
    let (shutdown_tx, _) = broadcast::channel(4);
    let handle = server::serve(
        paths,
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        0,
        options,
        shutdown_tx.clone(),
    )
    .await
    .unwrap();
    (handle, shutdown_tx)
}

/// Lay down a full epoch (board / brief / scoring / config / mutations)
/// plus the workspace adapter config under `epochs/{id}/`.
fn write_full_epoch(paths: &reader::WorkspacePaths, id: &str) {
    let dir = paths.epochs.join(id);
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(paths.current_epoch_marker(), id).unwrap();

    let cfg = serde_json::json!({
        "id": id,
        "contract_hash": "abc123hash",
        "created_at": "2026-05-15T23:42:25+00:00",
        "closed": false,
    });
    std::fs::write(dir.join("config.json"), cfg.to_string()).unwrap();

    let long_input = format!("Make a presentation about waffles {}", "x".repeat(200));
    let board = format!(
        "{}\n{}\n",
        serde_json::json!({
            "id": "waffles_single",
            "kind": "single_turn",
            "wall_clock_budget_seconds": 900,
            "weight": 1.0,
            "tags": ["presentation"],
            "input": long_input,
            "expectation": {"kind": "predicate", "spec": "x:y"},
        }),
        serde_json::json!({
            "id": "short_task",
            "kind": "single_turn",
            "wall_clock_budget_seconds": 120,
            "weight": 0.5,
            "input": "short input",
        }),
    );
    std::fs::write(dir.join("board.jsonl"), board).unwrap();

    std::fs::write(dir.join("brief.md"), "# full brief text\nbody").unwrap();

    let scoring = serde_json::json!({
        "pass_weight": 1.0,
        "namespace_weights": {"drift:": 1.0, "failure:": 1.0},
        "promote_margin": 0.01,
    });
    std::fs::write(dir.join("scoring.json"), scoring.to_string()).unwrap();

    let muts = serde_json::json!([
        {"id": "researcher_instruction", "kind": "span", "file": "agent/agent.py",
         "line_start": 12, "line_end": 34, "content": "You are a research specialist"},
    ]);
    std::fs::write(dir.join("mutations.json"), muts.to_string()).unwrap();

    let ws_cfg = serde_json::json!({"adapter": {
        "kind": "adk",
        "entrypoint": "kossel_run:root_agent",
        "mutable_trees": ["/abs/path/to/agent"],
    }});
    std::fs::write(paths.workspace.join("config.json"), ws_cfg.to_string()).unwrap();
}

/// The current epoch's contract as the integrity audits read it.
fn epoch_view(paths: &reader::WorkspacePaths) -> Value {
    serde_json::to_value(zicato_supervisor::epoch::build_epoch_view(paths)).unwrap()
}

#[test]
fn epoch_view_reads_the_full_definition() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "2026-05-15_e0");
    let r = epoch_view(&paths);

    assert_eq!(r["epoch_id"], "2026-05-15_e0");
    assert_eq!(r["contract_hash"], "abc123hash");
    assert_eq!(r["created_at"], "2026-05-15T23:42:25+00:00");
    assert_eq!(r["closed"], false);

    assert_eq!(r["harness"]["entrypoint"], "kossel_run:root_agent");
    assert_eq!(r["harness"]["mutable_trees"][0], "/abs/path/to/agent");

    let board = r["board"].as_array().unwrap();
    assert_eq!(board.len(), 2);
    assert_eq!(board[0]["entry_id"], "waffles_single");
    assert_eq!(board[0]["kind"], "single_turn");
    assert_eq!(board[0]["expectation_kind"], "predicate");
    assert_eq!(board[0]["wall_clock_budget_seconds"], 900.0);
    assert_eq!(board[0]["weight"], 1.0);
    assert_eq!(board[0]["tags"][0], "presentation");
    // input_preview is truncated.
    let preview = board[0]["input_preview"].as_str().unwrap();
    assert!(preview.ends_with("..."), "got: {preview}");
    assert!(preview.chars().count() <= 123);
    // A missing expectation remains null.
    assert_eq!(board[1]["wall_clock_budget_seconds"], 120.0);
    assert!(board[1]["expectation_kind"].is_null());

    assert_eq!(r["brief"], "# full brief text\nbody");
    assert_eq!(r["scoring"]["namespace_weights"]["drift:"], 1.0);
    assert_eq!(r["scoring"]["pass_weight"], 1.0);

    let muts = r["mutations"].as_array().unwrap();
    assert_eq!(muts.len(), 1);
    assert_eq!(muts[0]["id"], "researcher_instruction");
    assert_eq!(muts[0]["kind"], "span");
    assert_eq!(muts[0]["file"], "agent/agent.py");
    assert_eq!(muts[0]["lines"], "12-34");
    assert_eq!(muts[0]["preview"], "You are a research specialist");
}

#[test]
fn epoch_view_missing_mutations_yields_empty_list() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "e_no_muts");
    std::fs::remove_file(paths.epochs.join("e_no_muts").join("mutations.json")).unwrap();
    assert_eq!(epoch_view(&paths)["mutations"], serde_json::json!([]));
}

#[test]
fn epoch_view_missing_brief_yields_empty_string() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "e_no_brief");
    std::fs::remove_file(paths.epochs.join("e_no_brief").join("brief.md")).unwrap();
    assert_eq!(epoch_view(&paths)["brief"], "");
}

#[test]
fn epoch_view_no_current_epoch_yields_null_id() {
    let (_t, paths) = make_workspace();
    assert!(epoch_view(&paths)["epoch_id"].is_null());
}

/// Build a small `<workspace>/index.db` with the tables the promotion-gate
/// and divergence audits read.
fn write_index_db(paths: &reader::WorkspacePaths) {
    use rusqlite::Connection;
    std::fs::create_dir_all(&paths.workspace).unwrap();
    let conn = Connection::open(paths.index_db()).unwrap();
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
         INSERT INTO generations VALUES('2026-05-15_e0','v0',NULL,1);
         INSERT INTO generations VALUES('2026-05-15_e0','v1','v0',0);
         INSERT INTO generations VALUES('2026-05-15_e0','v2','v0',1);
         INSERT INTO experiments VALUES('2026-05-15_e0','v1',\
             'tighten the planner','planner overshoots','{\"k\":1}',\
             'rejected','worse drift overall',-0.1,0.2,-0.05,'{\"o\":2}');
         INSERT INTO experiments VALUES('2026-05-15_e0','v2',\
             'add a retry on tool error','tool calls are flaky','{\"k\":2}',\
             'promoted',NULL,0.3,-0.1,0.1,'{\"o\":3}');
         INSERT INTO patches VALUES('p1','2026-05-15_e0','v1','m1',\
             'replace','swap the planner prompt');
         INSERT INTO loss_profiles VALUES('r0a','2026-05-15_e0','v0','b1',0.4,'pass','{}');
         INSERT INTO loss_profiles VALUES('r0b','2026-05-15_e0','v0','b2',0.1,'pass','{}');
         INSERT INTO loss_profiles VALUES('r1a','2026-05-15_e0','v1','b1',0.6,'fail','{}');
         INSERT INTO loss_profiles VALUES('r1b','2026-05-15_e0','v1','b2',0.1,'pass','{}');
         INSERT INTO tournaments VALUES('t1','2026-05-15_e0','v0','v1',\
             'rejected',0.8,0.8,0.0,'worse drift overall','2026-05-15T01:00:00Z');
         INSERT INTO tournaments VALUES('t2','2026-05-15_e0','v0','v2',\
             'promoted',0.8,1.1,0.3,NULL,'2026-05-15T02:00:00Z');",
    )
    .unwrap();
    // Stamp the schema version so the supervisor's schema guard accepts
    // this fixture (it rejects an unstamped / mismatched index.db).
    conn.execute_batch(&format!(
        "PRAGMA user_version = {}",
        zicato_supervisor::index_db::EXPECTED_SCHEMA_VERSION
    ))
    .unwrap();
}

#[tokio::test]
async fn watchdog_escalates_to_sigkill_when_sigterm_ignored() {
    // Spawn a child that traps SIGTERM and stays alive.
    let mut child = std::process::Command::new("sh")
        .arg("-c")
        .arg("trap '' TERM; while true; do sleep 1; done")
        .spawn()
        .unwrap();
    let pid = child.id() as i32;
    // Give the shell time to install the trap.
    tokio::time::sleep(Duration::from_millis(100)).await;

    let outcome = sigutil::escalate(pid, Duration::from_millis(400)).await;
    assert_eq!(outcome, sigutil::EscalationOutcome::KilledForcefully);

    // Reap the child.
    let _ = child.wait();
}

#[tokio::test]
async fn watchdog_never_kills_orchestrator_on_stale_heartbeat() {
    // A deeply-stale orchestrator heartbeat must NEVER produce a kill — the
    // watchdog escalates the warning (`Stale`) and leaves the restart
    // decision to an out-of-band process supervisor (RUNTIME.md §3.2,
    // ROBUSTNESS.md §2.4). The `HeartbeatAction` enum has no `Kill`
    // variant by construction.
    use zicato_supervisor::watchdog::{decide_heartbeat, HeartbeatAction, Thresholds};
    let thresholds = Thresholds {
        heartbeat_stale_warn: Duration::from_secs(1),
        heartbeat_stale_kill: Duration::from_secs(2),
        run_stale_warn: Duration::from_secs(10),
        run_stale_kill: Duration::from_secs(20),
        grace: Duration::from_millis(200),
        run_kill_grace: Duration::from_millis(200),
        run_deadline_kill_disabled: false,
        max_run_seconds: Duration::from_secs(6 * 3600),
    };
    let now = Utc::now();
    // 10s stale, far past the 2s "deep stale" boundary.
    let hb = state::Heartbeat {
        pid: Some(424242),
        last_heartbeat: Some(now - ChDuration::seconds(10)),
        ..Default::default()
    };
    let action = decide_heartbeat(Some(&hb), now, &thresholds);
    assert_eq!(
        action,
        HeartbeatAction::Stale,
        "stale orchestrator heartbeat must warn, never kill",
    );
}

// ---------------------------------------------------------------------
// Per-run wall-clock deadline enforcement.
//
// These exercise the real `watchdog::runs_loop` against a real workspace
// and a real child process: when an `active_runs/{run_id}.json` carries a
// `deadline` in the past, the supervisor must SIGTERM (then SIGKILL) the
// worker pid named in that file — independent of any orchestrator.
// ---------------------------------------------------------------------

/// Spawn a long-lived child that ignores SIGTERM until it is SIGKILLed.
fn spawn_sigterm_trapping_child() -> std::process::Child {
    std::process::Command::new("sh")
        .arg("-c")
        .arg("trap '' TERM; while true; do sleep 1; done")
        .spawn()
        .unwrap()
}

/// Spawn a long-lived child that exits cleanly on SIGTERM (shell default).
fn spawn_plain_sleeper() -> std::process::Child {
    std::process::Command::new("sh")
        .arg("-c")
        .arg("exec sleep 600")
        .spawn()
        .unwrap()
}

/// Write an `active_runs/{run_id}.json` for `run_id` whose worker is `pid`
/// and whose `deadline` is `deadline_offset` from now (negative = past).
fn write_active_run(
    paths: &reader::WorkspacePaths,
    run_id: &str,
    pid: i32,
    deadline_offset: ChDuration,
) {
    let now = Utc::now();
    let ar = serde_json::json!({
        "run_id": run_id,
        "pid": pid,
        "pid_start_time": sigutil::pid_start_time(pid),
        "entry_id": "e1",
        "started_at": now - ChDuration::seconds(900),
        "last_progress": now, // fresh progress: deadline is the only trigger
        "deadline": now + deadline_offset,
        "wall_clock_budget_seconds": 900.0,
        "phase": "running",
        "reported_progress": 0.5,
    });
    std::fs::write(
        paths.active_runs_dir().join(format!("{run_id}.json")),
        serde_json::to_vec(&ar).unwrap(),
    )
    .unwrap();
}

/// Poll until `child` has exited, or `deadline` elapses.
///
/// A child of this process becomes a zombie when it dies until it is
/// reaped, and a zombie still answers `kill(pid, 0)` as "alive" — so we
/// must `try_wait()` the actual `Child` handle rather than probe the pid.
async fn wait_for_exit(child: &mut std::process::Child, deadline: Duration) -> bool {
    let stop = std::time::Instant::now() + deadline;
    while std::time::Instant::now() < stop {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) => {}
            Err(_) => return true,
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    matches!(child.try_wait(), Ok(Some(_)))
}

fn fast_thresholds(disable_deadline: bool) -> watchdog::Thresholds {
    watchdog::Thresholds {
        heartbeat_stale_warn: Duration::from_secs(3600),
        heartbeat_stale_kill: Duration::from_secs(3600),
        run_stale_warn: Duration::from_secs(3600),
        run_stale_kill: Duration::from_secs(3600),
        grace: Duration::from_millis(300),
        run_kill_grace: Duration::from_millis(300),
        run_deadline_kill_disabled: disable_deadline,
        // Generous ceiling: far above these tests' second-scale deadlines so
        // the untrusted-deadline clamp never interferes with them.
        max_run_seconds: Duration::from_secs(3600),
    }
}

#[tokio::test]
async fn watchdog_sigterms_run_past_its_deadline() {
    let (_t, paths) = make_workspace();

    // A child that exits on plain SIGTERM.
    let mut child = spawn_plain_sleeper();
    let pid = child.id() as i32;
    assert!(sigutil::is_alive(pid));

    // Its run blew the wall-clock budget 30s ago.
    write_active_run(&paths, "run-late", pid, ChDuration::seconds(-30));

    let (shutdown_tx, _) = broadcast::channel(4);
    let loop_paths = paths.clone();
    let loop_shutdown = shutdown_tx.clone();
    tokio::spawn(async move {
        watchdog::runs_loop(
            loop_paths,
            fast_thresholds(false),
            Duration::from_millis(50),
            Arc::new(WatchdogLog::new()),
            None,
            watchdog::DiffContainmentConfig {
                enabled: false,
                findings: Arc::new(
                    zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
                ),
            },
            watchdog::PromotionGateConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::promotion_gate::PromotionGateFindings::new()),
            },
            watchdog::DivergenceConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
                stuck_age_seconds: 3600,
            },
            loop_shutdown,
        )
        .await
    });

    assert!(
        wait_for_exit(&mut child, Duration::from_secs(5)).await,
        "watchdog did not kill the over-deadline run worker",
    );
    let _ = shutdown_tx.send(());

    // The watchdog must NOT delete the state file — lifecycle is the
    // orchestrator's.
    assert!(
        paths.active_runs_dir().join("run-late.json").exists(),
        "watchdog deleted the active_runs file; it must leave it for the orchestrator",
    );
}

#[tokio::test]
async fn watchdog_escalates_to_sigkill_when_run_ignores_sigterm() {
    let (_t, paths) = make_workspace();

    // A child that traps (ignores) SIGTERM: only SIGKILL stops it.
    let mut child = spawn_sigterm_trapping_child();
    let pid = child.id() as i32;
    // Give the shell time to install the trap.
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert!(sigutil::is_alive(pid));

    write_active_run(&paths, "run-stubborn", pid, ChDuration::seconds(-60));

    let (shutdown_tx, _) = broadcast::channel(4);
    let loop_paths = paths.clone();
    let loop_shutdown = shutdown_tx.clone();
    tokio::spawn(async move {
        watchdog::runs_loop(
            loop_paths,
            fast_thresholds(false),
            Duration::from_millis(50),
            Arc::new(WatchdogLog::new()),
            None,
            watchdog::DiffContainmentConfig {
                enabled: false,
                findings: Arc::new(
                    zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
                ),
            },
            watchdog::PromotionGateConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::promotion_gate::PromotionGateFindings::new()),
            },
            watchdog::DivergenceConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
                stuck_age_seconds: 3600,
            },
            loop_shutdown,
        )
        .await
    });

    assert!(
        wait_for_exit(&mut child, Duration::from_secs(5)).await,
        "watchdog did not escalate to SIGKILL for a SIGTERM-trapping run",
    );
    let _ = shutdown_tx.send(());
}

#[tokio::test]
async fn watchdog_does_not_kill_run_when_deadline_disabled() {
    let (_t, paths) = make_workspace();

    let mut child = spawn_plain_sleeper();
    let pid = child.id() as i32;
    write_active_run(&paths, "run-late", pid, ChDuration::seconds(-120));

    let (shutdown_tx, _) = broadcast::channel(4);
    let loop_paths = paths.clone();
    let loop_shutdown = shutdown_tx.clone();
    tokio::spawn(async move {
        watchdog::runs_loop(
            loop_paths,
            fast_thresholds(true), // --run-deadline-kill-disabled
            Duration::from_millis(50),
            Arc::new(WatchdogLog::new()),
            None,
            watchdog::DiffContainmentConfig {
                enabled: false,
                findings: Arc::new(
                    zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
                ),
            },
            watchdog::PromotionGateConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::promotion_gate::PromotionGateFindings::new()),
            },
            watchdog::DivergenceConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
                stuck_age_seconds: 3600,
            },
            loop_shutdown,
        )
        .await
    });

    // Give the loop several ticks; the child must survive.
    tokio::time::sleep(Duration::from_millis(800)).await;
    assert!(
        sigutil::is_alive(pid),
        "deadline killing was disabled but the run worker was signalled anyway",
    );

    let _ = shutdown_tx.send(());
    child.kill().ok();
    let _ = child.wait();
}

#[tokio::test]
async fn watchdog_never_signals_orchestrator_or_init_pids() {
    let (_t, paths) = make_workspace();

    // The heartbeat carries the orchestrator pid; here it is THIS test
    // process. Even though we also point an over-deadline run at it, the
    // watchdog must never SIGKILL it (we are still running afterwards).
    let orchestrator = std::process::id() as i32;
    let hb = serde_json::json!({
        "pid": orchestrator,
        "last_heartbeat": Utc::now(),
        "phase": "running",
    });
    std::fs::write(paths.heartbeat(), serde_json::to_vec(&hb).unwrap()).unwrap();

    // Run #1: pid is the orchestrator (protected). Run #2: pid 1 (init).
    write_active_run(&paths, "run-orch", orchestrator, ChDuration::seconds(-300));
    write_active_run(&paths, "run-init", 1, ChDuration::seconds(-300));

    let (shutdown_tx, _) = broadcast::channel(4);
    let loop_paths = paths.clone();
    let loop_shutdown = shutdown_tx.clone();
    tokio::spawn(async move {
        watchdog::runs_loop(
            loop_paths,
            fast_thresholds(false),
            Duration::from_millis(50),
            Arc::new(WatchdogLog::new()),
            None,
            watchdog::DiffContainmentConfig {
                enabled: false,
                findings: Arc::new(
                    zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
                ),
            },
            watchdog::PromotionGateConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::promotion_gate::PromotionGateFindings::new()),
            },
            watchdog::DivergenceConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
                stuck_age_seconds: 3600,
            },
            loop_shutdown,
        )
        .await
    });

    // Run several ticks. Survival of our own process proves the guard
    // held (a SIGKILL would have ended this test process).
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert!(sigutil::is_alive(orchestrator), "orchestrator pid survives");
    assert!(sigutil::is_alive(1), "init pid is untouched");

    let _ = shutdown_tx.send(());
}

#[tokio::test]
async fn watchdog_deadline_decision_is_pure_and_separate_from_staleness() {
    use std::collections::HashSet;
    use zicato_supervisor::watchdog::{
        decide_run, decide_run_deadline, RunAction, RunDeadlineAction,
    };

    let now = Utc::now();
    let pid = std::process::id() as i32; // alive
    let protected: HashSet<i32> = HashSet::new();
    let t = fast_thresholds(false);

    // Fresh progress, but the deadline blew 30s ago: staleness says
    // Nothing, the deadline trigger says Sigkill (>grace overrun).
    let run = state::ActiveRun {
        run_id: "r1".into(),
        // pid is the test process; the safety guard will refuse it, so
        // use a different, definitely-dead pid for the timing assertions.
        pid: Some(pid),
        last_progress: Some(now),
        deadline: Some(now - ChDuration::seconds(30)),
        ..Default::default()
    };
    assert_eq!(decide_run(&run, now, &t), RunAction::Nothing);
    // Own pid is guarded -> None even though the deadline is blown.
    assert_eq!(
        decide_run_deadline(
            &run,
            now,
            Duration::from_secs(5),
            Duration::from_secs(6 * 3600),
            &protected
        ),
        RunDeadlineAction::None,
    );
}

#[test]
fn lineage_view_includes_in_flight_generation() {
    let (_t, paths) = make_workspace();
    let epoch_dir = paths.epochs.join("2026-05-15_e0");
    let gens = epoch_dir.join("generations");

    // v0: the root — a generation directory with no experiment.json.
    std::fs::create_dir_all(gens.join("v0")).unwrap();

    // v1: proposed, experiment.json present but outcome still null —
    // the tournament has not resolved, so it is still in flight.
    std::fs::create_dir_all(gens.join("v1")).unwrap();
    let exp_v1 = serde_json::json!({
        "epoch_id": "2026-05-15_e0",
        "generation_id": "v1",
        "parent_generation_id": "v0",
        "proposed_at": "2026-05-15T10:00:00+00:00",
        "outcome": null,
    });
    std::fs::write(gens.join("v1").join("experiment.json"), exp_v1.to_string()).unwrap();

    // v2: resolved and rejected.
    std::fs::create_dir_all(gens.join("v2")).unwrap();
    let exp_v2 = serde_json::json!({
        "epoch_id": "2026-05-15_e0",
        "generation_id": "v2",
        "parent_generation_id": "v0",
        "proposed_at": "2026-05-15T11:00:00+00:00",
        "outcome": {"decision": "rejected"},
    });
    std::fs::write(gens.join("v2").join("experiment.json"), exp_v2.to_string()).unwrap();

    // Canonical lineage owns every node's topology and tri-state decision.
    let lineage = serde_json::json!({
        "epochs": [{
            "id": "2026-05-15_e0",
            "generations": [
                {"id": "v0", "parent_id": null, "promoted": true,
                 "created_at": "2026-05-15T09:00:00+00:00"},
                {"id": "v1", "parent_id": "v0", "promoted": null,
                 "created_at": "2026-05-15T10:00:00+00:00"},
                {"id": "v2", "parent_id": "v0", "promoted": false,
                 "created_at": "2026-05-15T11:00:00+00:00"},
            ],
        }],
    });
    std::fs::write(paths.lineage(), lineage.to_string()).unwrap();

    let r = serde_json::to_value(reader::build_lineage_view(&paths)).unwrap();
    let nodes = r["generations"].as_array().unwrap();
    // All three generation directories appear — not only the promoted v0.
    assert_eq!(nodes.len(), 3, "got: {r}");

    let by_id = |id: &str| {
        nodes
            .iter()
            .find(|n| n["generation_id"] == id)
            .unwrap()
            .clone()
    };

    let v0 = by_id("v0");
    assert_eq!(v0["epoch_id"], "2026-05-15_e0");
    assert_eq!(v0["promoted"], true);
    assert!(v0["parent_generation_id"].is_null());
    assert_eq!(v0["created_at"], "2026-05-15T09:00:00+00:00");

    // v1 has no decision yet -> promoted is null (still in flight).
    let v1 = by_id("v1");
    assert!(v1["promoted"].is_null(), "in-flight v1 must be null: {v1}");
    assert_eq!(v1["parent_generation_id"], "v0");
    assert_eq!(v1["created_at"], "2026-05-15T10:00:00+00:00");

    // v2 resolved-but-rejected -> promoted false.
    let v2 = by_id("v2");
    assert_eq!(v2["promoted"], false);
    assert_eq!(v2["parent_generation_id"], "v0");
}

// ---------------------------------------------------------------------
// /statusz — the watchdog's own minimal operational surface.
// ---------------------------------------------------------------------

/// Write a fresh heartbeat plus two active runs: one comfortably within
/// its deadline, one already past it.
fn write_statusz_state(paths: &reader::WorkspacePaths) {
    let now = Utc::now();
    let hb = serde_json::json!({
        "pid": std::process::id(),
        "last_heartbeat": now,
        "started_at": now - ChDuration::seconds(300),
        "phase": "proposing:round_1:v2",
    });
    std::fs::write(paths.heartbeat(), serde_json::to_vec(&hb).unwrap()).unwrap();

    // Within deadline.
    let ok_run = serde_json::json!({
        "run_id": "run-ok",
        "pid": 4242,
        "started_at": now - ChDuration::seconds(60),
        "deadline": now + ChDuration::seconds(600),
        "wall_clock_budget_seconds": 660.0,
    });
    std::fs::write(
        paths.active_runs_dir().join("run-ok.json"),
        serde_json::to_vec(&ok_run).unwrap(),
    )
    .unwrap();

    // Over deadline by ~120s.
    let late_run = serde_json::json!({
        "run_id": "run-late",
        "pid": 4343,
        "started_at": now - ChDuration::seconds(800),
        "deadline": now - ChDuration::seconds(120),
        "wall_clock_budget_seconds": 680.0,
    });
    std::fs::write(
        paths.active_runs_dir().join("run-late.json"),
        serde_json::to_vec(&late_run).unwrap(),
    )
    .unwrap();
}

#[tokio::test]
async fn statusz_json_carries_identity_and_per_run_deadlines() {
    let (_t, paths) = make_workspace();
    write_statusz_state(&paths);
    let (handle, shutdown) = start_server(paths.clone()).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/statusz.json"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    assert_eq!(
        resp.headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok()),
        Some("application/json"),
    );
    let r: Value = resp.json().await.unwrap();

    // Supervisor identity.
    let sup = &r["supervisor"];
    assert!(!sup["version"].as_str().unwrap().is_empty());
    assert!(!sup["build"].as_str().unwrap().is_empty());
    assert_eq!(sup["port"].as_u64().unwrap(), handle.addr.port() as u64);
    assert!(sup["pid"].as_i64().unwrap() > 1);
    assert!(r["supervisor"]["workspace"].as_str().unwrap().contains('/'));

    // Heartbeat freshness.
    assert_eq!(r["heartbeat"]["present"], true);
    assert_eq!(r["heartbeat"]["stale"], false);
    assert_eq!(
        r["heartbeat"]["orchestrator_pid"].as_u64().unwrap(),
        std::process::id() as u64
    );

    // Per-run deadline rows: one within, one over.
    let runs = r["runs"].as_array().unwrap();
    assert_eq!(runs.len(), 2);
    let by_id = |id: &str| runs.iter().find(|x| x["run_id"] == id).unwrap();

    let ok = by_id("run-ok");
    assert_eq!(ok["over_deadline"], false);
    assert!(ok["remaining_seconds"].as_i64().unwrap() > 0);
    assert!(ok["started_at"].as_str().is_some());
    assert!(ok["deadline"].as_str().is_some());

    let late = by_id("run-late");
    assert_eq!(late["over_deadline"], true);
    assert!(late["remaining_seconds"].as_i64().unwrap() < 0);
    assert!(late["over_by_seconds"].as_i64().unwrap() >= 110);

    // Summary flags the over-deadline run.
    assert_eq!(r["runs_over_deadline"].as_u64().unwrap(), 1);
    let summary = r["summary"].as_str().unwrap();
    assert!(summary.contains("OVER deadline"), "got: {summary}");

    let _ = shutdown.send(());
}

#[tokio::test]
async fn statusz_html_serves_non_empty_page() {
    let (_t, paths) = make_workspace();
    write_statusz_state(&paths);
    let (handle, shutdown) = start_server(paths.clone()).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client.get(format!("{base}/statusz")).send().await.unwrap();
    assert_eq!(resp.status(), 200);
    let ct = resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    assert!(ct.contains("text/html"), "got content-type: {ct}");

    let body = resp.text().await.unwrap();
    assert!(!body.is_empty());
    assert!(body.starts_with("<!doctype html>"));
    assert!(body.contains("/statusz"));
    // The over-deadline run is surfaced and flagged.
    assert!(body.contains("run-late"));
    assert!(body.contains("OVER"));
    // Self-contained: no external script reference.
    assert!(!body.contains("<script"));

    let _ = shutdown.send(());
}

#[tokio::test]
async fn only_the_watchdog_surface_is_mounted() {
    let (_t, paths) = make_workspace();
    write_statusz_state(&paths);
    let (handle, shutdown) = start_server(paths.clone()).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    for path in ["/statusz", "/statusz.json", "/api/audit/verify"] {
        let response = client.get(format!("{base}{path}")).send().await.unwrap();
        assert_eq!(response.status(), 200, "{path}");
    }
    for path in ["/", "/api/state", "/api/health", "/events"] {
        let response = client.get(format!("{base}{path}")).send().await.unwrap();
        assert_eq!(response.status(), 404, "{path}");
    }
    let response = client
        .post(format!("{base}/api/control/pause"))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 404);

    let _ = shutdown.send(());
}

#[tokio::test]
async fn statusz_no_runs_summary_is_clean() {
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone()).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let r: Value = client
        .get(format!("{base}/statusz.json"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r["runs"].as_array().unwrap().len(), 0);
    assert_eq!(r["runs_over_deadline"].as_u64().unwrap(), 0);
    assert_eq!(r["heartbeat"]["present"], false);
    assert_eq!(r["watchdog_actions"].as_array().unwrap().len(), 0);

    let _ = shutdown.send(());
}

#[tokio::test]
async fn statusz_surfaces_recorded_watchdog_actions() {
    use zicato_supervisor::action_log::{Action, Outcome, Trigger};

    let (_t, paths) = make_workspace();
    write_statusz_state(&paths);

    // A shared action log seeded with one escalation, as the watchdog
    // loop would have recorded it.
    let action_log = Arc::new(WatchdogLog::new());
    action_log.record(Action {
        ts: Utc::now(),
        trigger: Trigger::RunDeadline,
        pid: 4343,
        run_id: Some("run-late".into()),
        outcome: Outcome::KilledForcefully,
    });

    let opts = server::ServeOptions {
        heartbeat_stale_threshold_seconds: 30,
        action_log: action_log.clone(),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        ledger: None,
        diff_findings: Arc::new(
            zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
        ),
        promotion_gate_findings: Arc::new(
            zicato_supervisor::promotion_gate::PromotionGateFindings::new(),
        ),
        divergence_findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
    };
    let (handle, shutdown) = start_server_with(paths.clone(), opts).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let r: Value = client
        .get(format!("{base}/statusz.json"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let actions = r["watchdog_actions"].as_array().unwrap();
    assert_eq!(actions.len(), 1);
    assert_eq!(actions[0]["trigger"], "run_deadline");
    assert_eq!(actions[0]["pid"].as_i64().unwrap(), 4343);
    assert_eq!(actions[0]["run_id"], "run-late");
    assert_eq!(actions[0]["outcome"], "killed_forcefully");

    let _ = shutdown.send(());
}

// ---- audit ledger (INTEGRITY NOTARY record #1) --------------------------

#[tokio::test]
async fn audit_verify_reports_not_configured_without_a_ledger() {
    // No --ledger-dir → the verify endpoint reports the ledger absent and
    // /statusz shows it not-configured, exactly as a pre-ledger supervisor.
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone()).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let v: Value = client
        .get(format!("{base}/api/audit/verify"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(v["configured"], false);

    let s: Value = client
        .get(format!("{base}/statusz.json"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(s["audit_ledger"]["configured"], false);
    assert_eq!(s["audit_ledger"]["intact"], true);

    let _ = shutdown.send(());
}

#[tokio::test]
async fn audit_verify_reports_intact_chain_and_statusz_surfaces_it() {
    use zicato_supervisor::ledger::{AuditLedger, RecordKind};
    let (tmp, paths) = make_workspace();
    // The ledger lives OUTSIDE the orchestrator's mutable trees — a
    // supervisor-owned dir alongside the workspace.
    let ledger_dir = tmp.path().join("super-runtime");
    let ledger = Arc::new(AuditLedger::open(&ledger_dir));
    ledger.append(RecordKind::SupervisorStart, serde_json::json!({"v": 1}));
    ledger.append(
        RecordKind::WatchdogAction,
        serde_json::json!({"pid": 4242, "outcome": "killed_forcefully"}),
    );

    let opts = server::ServeOptions {
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        ledger: Some(ledger.clone()),
        diff_findings: Arc::new(
            zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
        ),
        promotion_gate_findings: Arc::new(
            zicato_supervisor::promotion_gate::PromotionGateFindings::new(),
        ),
        divergence_findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
    };
    let (handle, shutdown) = start_server_with(paths.clone(), opts).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let v: Value = client
        .get(format!("{base}/api/audit/verify"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(v["configured"], true);
    assert_eq!(v["intact"], true);
    assert_eq!(v["records"].as_u64().unwrap(), 2);

    let s: Value = client
        .get(format!("{base}/statusz.json"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(s["audit_ledger"]["configured"], true);
    assert_eq!(s["audit_ledger"]["intact"], true);
    assert_eq!(s["audit_ledger"]["records"].as_u64().unwrap(), 2);

    let _ = shutdown.send(());
}

#[tokio::test]
async fn audit_verify_detects_a_tampered_chain() {
    use zicato_supervisor::ledger::{AuditLedger, RecordKind};
    let (tmp, paths) = make_workspace();
    let ledger_dir = tmp.path().join("super-runtime");
    let ledger = Arc::new(AuditLedger::open(&ledger_dir));
    ledger.append(RecordKind::SupervisorStart, serde_json::json!({}));
    ledger.append(RecordKind::WatchdogAction, serde_json::json!({"pid": 7}));
    // Tamper with the persisted ledger out of band: edit the second record's
    // payload while leaving its digest, which the hash-chain must catch.
    let path = ledger.path().to_path_buf();
    let text = std::fs::read_to_string(&path).unwrap();
    let mut lines: Vec<String> = text.lines().map(str::to_string).collect();
    lines[1] = lines[1].replace("\"pid\":7", "\"pid\":13");
    std::fs::write(&path, lines.join("\n") + "\n").unwrap();

    let opts = server::ServeOptions {
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        ledger: Some(ledger.clone()),
        diff_findings: Arc::new(
            zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
        ),
        promotion_gate_findings: Arc::new(
            zicato_supervisor::promotion_gate::PromotionGateFindings::new(),
        ),
        divergence_findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
    };
    let (handle, shutdown) = start_server_with(paths.clone(), opts).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let v: Value = client
        .get(format!("{base}/api/audit/verify"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(v["intact"], false);
    assert_eq!(v["first_break_seq"].as_u64().unwrap(), 1);

    let s: Value = client
        .get(format!("{base}/statusz.json"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(s["audit_ledger"]["intact"], false);
    // The terse HTML surfaces the break loudly.
    let html = client
        .get(format!("{base}/statusz"))
        .send()
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(html.contains("CHAIN BREAK"));

    let _ = shutdown.send(());
}

// ---- diff containment (INTEGRITY NOTARY record #2) ----------------------

/// Materialise a generation snapshot under epochs/{e}/generations/{g}/.
fn write_gen_snapshot(
    paths: &reader::WorkspacePaths,
    epoch: &str,
    gen: &str,
    parent: Option<&str>,
    files: &[(&str, &[u8])],
) {
    let gen_dir = paths.epochs.join(epoch).join("generations").join(gen);
    std::fs::create_dir_all(&gen_dir).unwrap();
    if let Some(parent) = parent {
        std::fs::write(
            gen_dir.join("experiment.json"),
            serde_json::json!({"parent_generation_id": parent}).to_string(),
        )
        .unwrap();
        std::fs::write(
            paths.lineage(),
            serde_json::json!({"epochs": [{"id": epoch, "generations": [{
                "id": gen, "parent_id": parent, "promoted": false
            }]}]})
            .to_string(),
        )
        .unwrap();
    }
    for (rel, contents) in files {
        let p = gen_dir.join("snapshot").join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, contents).unwrap();
    }
}

#[tokio::test]
async fn diff_containment_quarantines_an_out_of_bounds_child_end_to_end() {
    let (_t, paths) = make_workspace();
    // Harness: the only mutable tree is "agent".
    std::fs::write(
        paths.workspace.join("config.json"),
        serde_json::json!({"adapter": {
            "kind": "adk", "entrypoint": "m:a", "mutable_trees": ["/reg/agent"]
        }})
        .to_string(),
    )
    .unwrap();
    std::fs::write(paths.current_epoch_marker(), "e1").unwrap();
    // v0 parent + v1 child; v1 tampers with an out-of-bounds support file.
    write_gen_snapshot(
        &paths,
        "e1",
        "v0",
        None,
        &[("agent/main.py", b"x=1\n"), ("support/lib.py", b"shared\n")],
    );
    write_gen_snapshot(
        &paths,
        "e1",
        "v1",
        Some("v0"),
        &[
            ("agent/main.py", b"x=2\n"),
            ("support/lib.py", b"TAMPERED\n"),
        ],
    );

    // A shared findings store the loop fills and the server reads.
    let findings = Arc::new(zicato_supervisor::diff_containment::DiffContainmentFindings::new());

    let (shutdown_tx, _) = broadcast::channel(4);
    let loop_paths = paths.clone();
    let loop_shutdown = shutdown_tx.clone();
    let loop_findings = findings.clone();
    tokio::spawn(async move {
        watchdog::runs_loop(
            loop_paths,
            fast_thresholds(false),
            Duration::from_millis(50),
            Arc::new(WatchdogLog::new()),
            None,
            watchdog::DiffContainmentConfig {
                enabled: true,
                findings: loop_findings,
            },
            watchdog::PromotionGateConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::promotion_gate::PromotionGateFindings::new()),
            },
            watchdog::DivergenceConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
                stuck_age_seconds: 3600,
            },
            loop_shutdown,
        )
        .await
    });

    // Give the loop a few ticks to scan.
    tokio::time::sleep(Duration::from_millis(300)).await;

    // The shared store now holds the quarantine; serve /statusz over it.
    let opts = server::ServeOptions {
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        ledger: None,
        diff_findings: findings.clone(),
        promotion_gate_findings: Arc::new(
            zicato_supervisor::promotion_gate::PromotionGateFindings::new(),
        ),
        divergence_findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
    };
    let (handle, server_shutdown) = start_server_with(paths.clone(), opts).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let s: Value = client
        .get(format!("{base}/statusz.json"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let dc = &s["diff_containment"];
    assert_eq!(dc["scanned"], true);
    let quarantined = dc["quarantined"].as_array().unwrap();
    assert_eq!(
        quarantined.len(),
        1,
        "the out-of-bounds child is quarantined"
    );
    assert_eq!(quarantined[0]["generation_id"], "v1");
    assert_eq!(
        quarantined[0]["violations"][0]["path"], "support/lib.py",
        "the out-of-bounds file is named"
    );

    // The terse HTML raises the hard ALERT.
    let html = client
        .get(format!("{base}/statusz"))
        .send()
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(html.contains("OUT-OF-BOUNDS MUTATIONS"));

    // A durable quarantine finding was written into the epoch health dir.
    let finding = paths
        .epoch_health_dir("e1")
        .join("diff_containment_v1.json");
    assert!(finding.exists(), "a quarantine finding must be persisted");

    let _ = shutdown_tx.send(());
    let _ = server_shutdown.send(());
}

#[tokio::test]
async fn diff_containment_passes_an_in_bounds_child_end_to_end() {
    let (_t, paths) = make_workspace();
    std::fs::write(
        paths.workspace.join("config.json"),
        serde_json::json!({"adapter": {
            "kind": "adk", "entrypoint": "m:a", "mutable_trees": ["/reg/agent"]
        }})
        .to_string(),
    )
    .unwrap();
    std::fs::write(paths.current_epoch_marker(), "e1").unwrap();
    write_gen_snapshot(
        &paths,
        "e1",
        "v0",
        None,
        &[("agent/main.py", b"x=1\n"), ("support/lib.py", b"shared\n")],
    );
    // v1 only edits the mutable agent tree — fully contained.
    write_gen_snapshot(
        &paths,
        "e1",
        "v1",
        Some("v0"),
        &[("agent/main.py", b"x=2\n"), ("support/lib.py", b"shared\n")],
    );

    let findings = Arc::new(zicato_supervisor::diff_containment::DiffContainmentFindings::new());
    let (shutdown_tx, _) = broadcast::channel(4);
    let loop_paths = paths.clone();
    let loop_shutdown = shutdown_tx.clone();
    let loop_findings = findings.clone();
    tokio::spawn(async move {
        watchdog::runs_loop(
            loop_paths,
            fast_thresholds(false),
            Duration::from_millis(50),
            Arc::new(WatchdogLog::new()),
            None,
            watchdog::DiffContainmentConfig {
                enabled: true,
                findings: loop_findings,
            },
            watchdog::PromotionGateConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::promotion_gate::PromotionGateFindings::new()),
            },
            watchdog::DivergenceConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
                stuck_age_seconds: 3600,
            },
            loop_shutdown,
        )
        .await
    });
    tokio::time::sleep(Duration::from_millis(300)).await;

    let view = findings.view();
    assert!(view.scanned);
    assert_eq!(view.pairs_scanned, 1);
    assert!(
        view.quarantined.is_empty(),
        "an in-bounds child must not be quarantined"
    );

    let _ = shutdown_tx.send(());
}

// ---- promotion gatekeeping (INTEGRITY NOTARY record #3) -----------------

#[tokio::test]
async fn promotion_gate_alarms_on_a_decision_that_contradicts_the_scores() {
    let (_t, paths) = make_workspace();
    // The shared index fixture records t2 as `promoted` with child_scalar 1.1
    // vs parent 0.8 (delta +0.3) — the loss ROSE, so the promotion contradicts
    // its own recorded scores. The marker scopes the scan to that epoch.
    write_index_db(&paths);
    std::fs::write(paths.current_epoch_marker(), "2026-05-15_e0").unwrap();

    let findings = Arc::new(zicato_supervisor::promotion_gate::PromotionGateFindings::new());
    let (shutdown_tx, _) = broadcast::channel(4);
    let loop_paths = paths.clone();
    let loop_shutdown = shutdown_tx.clone();
    let loop_findings = findings.clone();
    tokio::spawn(async move {
        watchdog::runs_loop(
            loop_paths,
            fast_thresholds(false),
            Duration::from_millis(50),
            Arc::new(WatchdogLog::new()),
            None,
            watchdog::DiffContainmentConfig {
                enabled: false,
                findings: Arc::new(
                    zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
                ),
            },
            watchdog::PromotionGateConfig {
                enabled: true,
                findings: loop_findings,
            },
            watchdog::DivergenceConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
                stuck_age_seconds: 3600,
            },
            loop_shutdown,
        )
        .await
    });
    tokio::time::sleep(Duration::from_millis(300)).await;

    // Serve /statusz over the same store and confirm the contradiction shows.
    let opts = server::ServeOptions {
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        ledger: None,
        diff_findings: Arc::new(
            zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
        ),
        promotion_gate_findings: findings.clone(),
        divergence_findings: Arc::new(zicato_supervisor::divergence::DivergenceFindings::new()),
    };
    let (handle, server_shutdown) = start_server_with(paths.clone(), opts).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let s: Value = client
        .get(format!("{base}/statusz.json"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let pg = &s["promotion_gate"];
    assert_eq!(pg["scanned"], true);
    let contradictions = pg["contradictions"].as_array().unwrap();
    assert_eq!(
        contradictions.len(),
        1,
        "the unsupported promotion is flagged"
    );
    assert_eq!(contradictions[0]["challenger_generation_id"], "v2");
    assert_eq!(contradictions[0]["champion_generation_id"], "v0");

    let html = client
        .get(format!("{base}/statusz"))
        .send()
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(html.contains("DECISION CONTRADICTS SCORES"));

    let _ = shutdown_tx.send(());
    let _ = server_shutdown.send(());
}

// ---- index-vs-canonical divergence audit (INTEGRITY NOTARY record #4) ---

#[tokio::test]
async fn divergence_audit_flags_a_promoted_mismatch_end_to_end() {
    let (_t, paths) = make_workspace();
    // The shared index fixture marks v2 promoted=1. Make the CANONICAL side
    // disagree: canonical lineage records v2 as rejected. The audit
    // must flag the promoted divergence.
    write_index_db(&paths);
    std::fs::write(paths.current_epoch_marker(), "2026-05-15_e0").unwrap();
    // Epoch config contract_hash matching the index's (the fixture's epochs
    // table is absent, so no contract-hash finding — isolate the promoted one).
    let gen_dir = paths
        .epochs
        .join("2026-05-15_e0")
        .join("generations")
        .join("v2");
    std::fs::create_dir_all(&gen_dir).unwrap();
    std::fs::write(
        gen_dir.join("experiment.json"),
        serde_json::json!({"parent_generation_id": "v0", "outcome": {"decision": "rejected"}})
            .to_string(),
    )
    .unwrap();
    std::fs::write(
        paths.lineage(),
        serde_json::json!({"epochs": [{"id": "2026-05-15_e0", "generations": [{
            "id": "v2", "parent_id": "v0", "promoted": false
        }]}]})
        .to_string(),
    )
    .unwrap();

    let findings = Arc::new(zicato_supervisor::divergence::DivergenceFindings::new());
    let (shutdown_tx, _) = broadcast::channel(4);
    let loop_paths = paths.clone();
    let loop_shutdown = shutdown_tx.clone();
    let loop_findings = findings.clone();
    tokio::spawn(async move {
        watchdog::runs_loop(
            loop_paths,
            fast_thresholds(false),
            Duration::from_millis(50),
            Arc::new(WatchdogLog::new()),
            None,
            watchdog::DiffContainmentConfig {
                enabled: false,
                findings: Arc::new(
                    zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
                ),
            },
            watchdog::PromotionGateConfig {
                enabled: false,
                findings: Arc::new(zicato_supervisor::promotion_gate::PromotionGateFindings::new()),
            },
            watchdog::DivergenceConfig {
                enabled: true,
                findings: loop_findings,
                stuck_age_seconds: 3600,
            },
            loop_shutdown,
        )
        .await
    });
    tokio::time::sleep(Duration::from_millis(300)).await;

    let opts = server::ServeOptions {
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        ledger: None,
        diff_findings: Arc::new(
            zicato_supervisor::diff_containment::DiffContainmentFindings::new(),
        ),
        promotion_gate_findings: Arc::new(
            zicato_supervisor::promotion_gate::PromotionGateFindings::new(),
        ),
        divergence_findings: findings.clone(),
    };
    let (handle, server_shutdown) = start_server_with(paths.clone(), opts).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let s: Value = client
        .get(format!("{base}/statusz.json"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let dv = &s["divergence"];
    assert_eq!(dv["scanned"], true);
    let codes: Vec<&str> = dv["findings"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["code"].as_str().unwrap())
        .collect();
    assert!(
        codes.contains(&"promoted_divergence"),
        "expected a promoted_divergence finding, got {codes:?}",
    );

    let html = client
        .get(format!("{base}/statusz"))
        .send()
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(html.contains("DIVERGENCE"));

    let _ = shutdown_tx.send(());
    let _ = server_shutdown.send(());
}

#[test]
fn lineage_reads_the_committed_round_outcome() {
    let (_tmp, paths) = make_workspace();
    let generation = paths.epochs.join("epoch/generations/v1");
    std::fs::create_dir_all(&generation).unwrap();
    std::fs::write(
        generation.join("experiment.json"),
        serde_json::json!({"id": "proposal", "round_index": 0}).to_string(),
    )
    .unwrap();
    std::fs::write(
        paths.lineage(),
        serde_json::json!({"epochs": [{"id": "epoch", "generations": [
            {"id": "v1", "parent_id": "v0", "promoted": null}
        ]}]})
        .to_string(),
    )
    .unwrap();
    let round_dir = paths.epochs.join("epoch/rounds/0");
    std::fs::create_dir_all(&round_dir).unwrap();
    let mut receipt = serde_json::json!({
        "epoch_id": "epoch", "round_index": 0, "state": "pending",
        "candidates": [{"generation_id": "v1", "experiment_id": "proposal",
            "outcome": {"tournament_decision": "promoted"}}]
    });
    let path = round_dir.join("field_settlement.json");
    std::fs::write(&path, receipt.to_string()).unwrap();
    assert_eq!(
        reader::build_lineage_view(&paths).generations[0].promoted,
        None
    );
    receipt["state"] = serde_json::json!("committed");
    std::fs::write(&path, receipt.to_string()).unwrap();
    let view = reader::build_lineage_view(&paths);
    assert_eq!(view.generations[0].promoted, Some(true));
    assert_eq!(
        view.generations[0].parent_generation_id.as_deref(),
        Some("v0")
    );
}

// ---------------------------------------------------------------------
// The binary: address announcement and the final scan on SIGTERM.
// ---------------------------------------------------------------------

fn write_lineage_decisions(paths: &reader::WorkspacePaths, decisions: &[(&str, bool)]) {
    let mut generations = Vec::new();
    for (id, promoted) in decisions {
        std::fs::create_dir_all(paths.epochs.join("e0/generations").join(id)).unwrap();
        generations.push(serde_json::json!({"id": id, "promoted": promoted}));
    }
    let lineage = serde_json::json!({"epochs": [{"id": "e0", "generations": generations}]});
    std::fs::write(paths.lineage(), lineage.to_string()).unwrap();
}

fn ledger_decisions(ledger: &std::path::Path) -> Vec<String> {
    std::fs::read_to_string(ledger)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|record| record["payload"]["decision"].is_string())
        .map(|record| {
            record["payload"]["generation_id"]
                .as_str()
                .unwrap()
                .to_owned()
        })
        .collect()
}

/// Kills the child if the test ends before it exits on its own.
struct KillOnDrop(std::process::Child);

impl Drop for KillOnDrop {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[tokio::test]
async fn binary_announces_its_address_and_scans_once_more_on_sigterm() {
    use std::io::{BufRead, Read};

    let (tmp, paths) = make_workspace();
    let ledger_dir = tmp.path().join("proctor");
    write_lineage_decisions(&paths, &[("v0", true)]);
    let mut child = KillOnDrop(
        std::process::Command::new(env!("CARGO_BIN_EXE_zicato-supervisor"))
            .arg("--workspace")
            .arg(&paths.workspace)
            .args(["--port", "0", "--interval", "3600", "--ledger-dir"])
            .arg(&ledger_dir)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .spawn()
            .unwrap(),
    );
    let mut stdout = std::io::BufReader::new(child.0.stdout.take().unwrap());
    // Lines before the address line are collected and must be absent.
    let mut before_address = Vec::new();
    loop {
        let mut line = String::new();
        assert_ne!(stdout.read_line(&mut line).unwrap(), 0, "no address line");
        if line.starts_with("zicato-supervisor listening on http://127.0.0.1:") {
            break;
        }
        before_address.push(line);
    }

    // The first tick records v0; the hour-long interval means only the
    // shutdown can start the scan that records v1.
    let ledger = ledger_dir.join("audit_ledger.jsonl");
    tokio::time::timeout(Duration::from_secs(10), async {
        while ledger_decisions(&ledger).is_empty() {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("the first scan did not record the seed decision");
    write_lineage_decisions(&paths, &[("v0", true), ("v1", false)]);
    // SAFETY: the pid names the child this test spawned and has not reaped.
    assert_eq!(unsafe { libc::kill(child.0.id() as i32, libc::SIGTERM) }, 0);
    assert!(wait_for_exit(&mut child.0, Duration::from_secs(10)).await);

    assert_eq!(ledger_decisions(&ledger), ["v0", "v1"]);
    let mut after_address = String::new();
    stdout.read_to_string(&mut after_address).unwrap();
    assert_eq!(
        (before_address, after_address),
        (Vec::new(), String::new()),
        "standard output carries only the address line"
    );
}
