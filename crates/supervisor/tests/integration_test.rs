//! End-to-end tests: spin up the supervisor server against a synthetic
//! workspace, exercise GET endpoints, verify control-file writes, and
//! check signal escalation against a real child process.

use chrono::{Duration as ChDuration, Utc};
use serde_json::Value;
use std::net::{IpAddr, Ipv4Addr};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tempfile::TempDir;
use tokio::sync::broadcast;
use zicato_supervisor::{
    action_log::WatchdogLog, reader, server, signal as sigutil, state, watchdog, watcher,
};

fn make_workspace() -> (TempDir, reader::WorkspacePaths) {
    let tmp = TempDir::new().unwrap();
    let ws = tmp.path().to_path_buf();
    std::fs::create_dir_all(ws.join("runtime/active_runs")).unwrap();
    std::fs::create_dir_all(ws.join("runtime/control")).unwrap();
    std::fs::create_dir_all(ws.join("epochs")).unwrap();
    (tmp, reader::WorkspacePaths::new(ws))
}

fn serve_opts(read_only: bool) -> server::ServeOptions {
    server::ServeOptions {
        read_only,
        dashboard_disabled: false,
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        fold_diagnostics: Arc::new(zicato_supervisor::fold_stats::FoldDiagnostics::new()),
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
    read_only: bool,
) -> (server::ServerHandle, broadcast::Sender<()>) {
    let (watch_tx, _) = broadcast::channel(64);
    let (shutdown_tx, _) = broadcast::channel(4);
    let handle = server::serve(
        paths,
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        0, // ephemeral
        serve_opts(read_only),
        watch_tx,
        shutdown_tx.clone(),
    )
    .await
    .unwrap();
    (handle, shutdown_tx)
}

/// Like `start_server` but with a fully-specified `ServeOptions`, so
/// `/statusz` tests can drive `--no-dashboard` and a shared action log.
async fn start_server_with(
    paths: reader::WorkspacePaths,
    options: server::ServeOptions,
) -> (server::ServerHandle, broadcast::Sender<()>) {
    let (watch_tx, _) = broadcast::channel(64);
    let (shutdown_tx, _) = broadcast::channel(4);
    let handle = server::serve(
        paths,
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        0,
        options,
        watch_tx,
        shutdown_tx.clone(),
    )
    .await
    .unwrap();
    (handle, shutdown_tx)
}

fn write_state(paths: &reader::WorkspacePaths) {
    let now = Utc::now();
    let hb = serde_json::json!({
        "pid": std::process::id(),
        "instance_id": "test",
        "last_heartbeat": now,
        "phase": "running",
        "epoch_id": "2026-05-14_test",
        "round": 1u64,
    });
    std::fs::write(paths.heartbeat(), serde_json::to_vec(&hb).unwrap()).unwrap();

    let at = serde_json::json!({
        "tournament_id": "t1",
        "generation_id": "g1",
        "round": 1u64,
        "entries": [
            {"entry_id": "e1", "status": "running"},
            {"entry_id": "e2", "status": "queued"},
        ],
    });
    std::fs::write(paths.active_tournament(), serde_json::to_vec(&at).unwrap()).unwrap();

    let ar = serde_json::json!({
        "run_id": "run-1",
        "pid": std::process::id(),
        "entry_id": "e1",
        "started_at": now,
        "last_progress": now,
        "phase": "running",
        "progress": 0.25,
    });
    std::fs::write(
        paths.active_runs_dir().join("run-1.json"),
        serde_json::to_vec(&ar).unwrap(),
    )
    .unwrap();

    std::fs::write(paths.current_epoch_marker(), "2026-05-14_test").unwrap();
    std::fs::write(paths.lineage(), b"{\"generations\":[],\"edges\":[]}").unwrap();
}

#[tokio::test]
async fn get_endpoints_return_state() {
    let (_t, paths) = make_workspace();
    write_state(&paths);
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);

    let client = reqwest::Client::new();

    let r: Value = client
        .get(format!("{base}/api/state"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r["epoch_id"], "2026-05-14_test");
    assert_eq!(r["active_runs"][0]["run_id"], "run-1");

    let r: Value = client
        .get(format!("{base}/api/heartbeat"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r["phase"], "running");

    let r: Value = client
        .get(format!("{base}/api/active-runs"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r[0]["run_id"], "run-1");

    let r: Value = client
        .get(format!("{base}/api/active-tournament"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r["tournament_id"], "t1");

    let r: Value = client
        .get(format!("{base}/api/health"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r["status"], "ok");
    assert_eq!(r["read_only"], true);

    let resp = client.get(format!("{base}/")).send().await.unwrap();
    assert_eq!(resp.status(), 200);

    let _ = shutdown.send(());
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
            "id": "aliased_budget",
            "kind": "single_turn",
            "budget_s": 120,
            "weight": 0.5,
            "input": "short input",
        }),
    );
    std::fs::write(dir.join("board.jsonl"), board).unwrap();

    std::fs::write(dir.join("brief.md"), "# full brief text\nbody").unwrap();

    let scoring = serde_json::json!({
        "drift_weight": 1.0,
        "pass_weight": 1.0,
        "promote_margin": 0.01,
    });
    std::fs::write(dir.join("scoring.json"), scoring.to_string()).unwrap();

    let muts = serde_json::json!([
        {"id": "researcher_instruction", "kind": "span", "file": "agent/agent.py",
         "line_start": 12, "line_end": 34, "content": "You are a research specialist"},
    ]);
    std::fs::write(dir.join("mutations.json"), muts.to_string()).unwrap();

    let ws_cfg = serde_json::json!({
        "adk_entrypoint": "kossel_run:root_agent",
        "mutable_trees": ["/abs/path/to/agent"],
    });
    std::fs::write(paths.workspace.join("config.json"), ws_cfg.to_string()).unwrap();
}

#[tokio::test]
async fn epoch_endpoint_returns_full_definition() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "2026-05-15_e0");
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/epoch"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();

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
    assert_eq!(board[0]["budget_s"], 900.0);
    assert_eq!(board[0]["weight"], 1.0);
    assert_eq!(board[0]["tags"][0], "presentation");
    // input_preview is truncated.
    let preview = board[0]["input_preview"].as_str().unwrap();
    assert!(preview.ends_with("..."), "got: {preview}");
    assert!(preview.chars().count() <= 123);
    // budget_s alias resolved; missing expectation -> null.
    assert_eq!(board[1]["budget_s"], 120.0);
    assert!(board[1]["expectation_kind"].is_null());

    assert_eq!(r["brief"], "# full brief text\nbody");
    assert_eq!(r["scoring"]["drift_weight"], 1.0);
    assert_eq!(r["scoring"]["pass_weight"], 1.0);

    let muts = r["mutations"].as_array().unwrap();
    assert_eq!(muts.len(), 1);
    assert_eq!(muts[0]["id"], "researcher_instruction");
    assert_eq!(muts[0]["kind"], "span");
    assert_eq!(muts[0]["file"], "agent/agent.py");
    assert_eq!(muts[0]["lines"], "12-34");
    assert_eq!(muts[0]["preview"], "You are a research specialist");

    let _ = shutdown.send(());
}

#[tokio::test]
async fn epoch_endpoint_missing_mutations_yields_empty_list() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "e_no_muts");
    std::fs::remove_file(paths.epochs.join("e_no_muts").join("mutations.json")).unwrap();
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let r: Value = client
        .get(format!("{base}/api/epoch"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r["mutations"], serde_json::json!([]));

    let _ = shutdown.send(());
}

#[tokio::test]
async fn epoch_endpoint_missing_brief_yields_empty_string() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "e_no_brief");
    std::fs::remove_file(paths.epochs.join("e_no_brief").join("brief.md")).unwrap();
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let r: Value = client
        .get(format!("{base}/api/epoch"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r["brief"], "");

    let _ = shutdown.send(());
}

#[tokio::test]
async fn epoch_endpoint_no_current_epoch_yields_null_id() {
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/epoch"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();
    assert!(r["epoch_id"].is_null());

    let _ = shutdown.send(());
}

#[tokio::test]
async fn state_endpoint_includes_epoch_object() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "2026-05-15_e0");
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let r: Value = client
        .get(format!("{base}/api/state"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r["epoch"]["epoch_id"], "2026-05-15_e0");
    assert_eq!(r["epoch"]["contract_hash"], "abc123hash");
    assert_eq!(r["epoch"]["board"].as_array().unwrap().len(), 2);

    let _ = shutdown.send(());
}

#[tokio::test]
async fn read_only_blocks_post_endpoints() {
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let r = client
        .post(format!("{base}/api/control/pause"))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 403);

    let r = client
        .post(format!("{base}/api/control/kill/run-9"))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 403);

    let _ = shutdown.send(());
}

#[tokio::test]
async fn pause_writes_control_file_atomically() {
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone(), false).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let r = client
        .post(format!("{base}/api/control/pause"))
        .json(&serde_json::json!({"reason": "manual"}))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 202);

    let marker = paths.control_dir().join("pause_epoch");
    assert!(marker.exists(), "pause_epoch marker missing");
    let body: Value = serde_json::from_slice(&std::fs::read(&marker).unwrap()).unwrap();
    assert_eq!(body["reason"], "manual");

    // No .tmp leftover.
    let leftover: Vec<PathBuf> = std::fs::read_dir(paths.control_dir())
        .unwrap()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("tmp"))
        .collect();
    assert!(leftover.is_empty(), "tmp files left behind: {leftover:?}");

    let _ = shutdown.send(());
}

#[tokio::test]
async fn resume_deletes_pause_flag_and_is_idempotent() {
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone(), false).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    // Pause writes the flag; resume unlinks it.
    let r = client
        .post(format!("{base}/api/control/pause"))
        .json(&serde_json::json!({"reason": "hold"}))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 202);
    let marker = paths.control_dir().join("pause_epoch");
    assert!(marker.exists(), "pause_epoch marker missing after pause");

    let r = client
        .post(format!("{base}/api/control/resume"))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 202);
    let body: Value = r.json().await.unwrap();
    assert_eq!(body["accepted"], true);
    assert_eq!(body["removed"], true);
    assert!(!marker.exists(), "pause_epoch flag survived resume");

    // Idempotent: a second resume on an unpaused workspace is an accepted
    // no-op (removed: false), never an error.
    let r = client
        .post(format!("{base}/api/control/resume"))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 202);
    let body: Value = r.json().await.unwrap();
    assert_eq!(body["removed"], false);

    let _ = shutdown.send(());
}

#[tokio::test]
async fn resume_is_forbidden_when_read_only() {
    let (_t, paths) = make_workspace();
    // A pending pause flag must survive a read-only resume attempt.
    std::fs::write(paths.control_dir().join("pause_epoch"), b"{}").unwrap();
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let r = client
        .post(format!("{base}/api/control/resume"))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 403);
    assert!(
        paths.control_dir().join("pause_epoch").exists(),
        "read-only resume must not touch the flag"
    );

    let _ = shutdown.send(());
}

#[tokio::test]
async fn brief_post_writes_replacement_file() {
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone(), false).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let payload = "tighten the proposer brief\n";
    let r = client
        .post(format!("{base}/api/control/brief"))
        .body(payload)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 202);

    // The control file keeps its protocol name regardless of the
    // UI-facing endpoint rename.
    let marker = paths.control_dir().join("rubric_replacement.txt");
    let got = std::fs::read_to_string(&marker).unwrap();
    assert_eq!(got, payload);

    let _ = shutdown.send(());
}

#[tokio::test]
async fn epoch_endpoint_brief_falls_back_to_legacy_rubric_md() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "e_legacy");
    // Pre-rename epoch on disk: replace `brief.md` with the legacy
    // `rubric.md`. The endpoint must still surface it under `brief`.
    let dir = paths.epochs.join("e_legacy");
    std::fs::remove_file(dir.join("brief.md")).unwrap();
    std::fs::write(dir.join("rubric.md"), "# legacy brief text").unwrap();
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let r: Value = client
        .get(format!("{base}/api/epoch"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r["brief"], "# legacy brief text");

    let _ = shutdown.send(());
}

#[tokio::test]
async fn kill_endpoint_rejects_path_traversal() {
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone(), false).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let r = client
        .post(format!("{base}/api/control/kill/..%2Fevil"))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 400);

    let _ = shutdown.send(());
}

/// Build a small `<workspace>/index.db` with the tables the
/// tournament endpoints read.
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
async fn analytical_routes_are_not_mounted() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "2026-05-15_e0");
    write_index_db(&paths);
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    for path in [
        "/api/tournaments",
        "/api/tournaments/v1",
        "/api/health-report",
    ] {
        let response = client.get(format!("{base}{path}")).send().await.unwrap();
        assert_eq!(response.status(), 404, "{path}");
    }

    let _ = shutdown.send(());
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

#[tokio::test]
async fn sse_emits_snapshot_then_change_event() {
    let (_t, paths) = make_workspace();
    write_state(&paths);

    let (watch_tx, _) = broadcast::channel::<watcher::WatchEvent>(64);
    let (shutdown_tx, _) = broadcast::channel(4);
    let handle = server::serve(
        paths.clone(),
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        0,
        serve_opts(true),
        watch_tx.clone(),
        shutdown_tx.clone(),
    )
    .await
    .unwrap();
    let base = format!("http://{}", handle.addr);

    // Connect, read initial snapshot bytes.
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .unwrap();
    let mut resp = client.get(format!("{base}/events")).send().await.unwrap();

    let mut buf = Vec::new();
    // Read until we see the snapshot event terminator (blank line).
    while buf.windows(2).all(|w| w != b"\n\n") {
        if let Some(chunk) = resp.chunk().await.unwrap() {
            buf.extend_from_slice(&chunk);
        } else {
            break;
        }
        if buf.len() > 65536 {
            break;
        }
    }
    let text = String::from_utf8_lossy(&buf);
    assert!(text.contains("event: snapshot"), "got: {text}");
    assert!(text.contains("\"type\":\"snapshot\""), "got: {text}");

    // Fire a state change event.
    watch_tx
        .send(watcher::WatchEvent {
            kind: watcher::ChangeKind::Heartbeat,
            path: "/tmp/heartbeat.json".to_string(),
            ts: Utc::now(),
        })
        .ok();

    // Read more until we see state_change.
    buf.clear();
    let deadline = std::time::Instant::now() + Duration::from_secs(3);
    while std::time::Instant::now() < deadline {
        if let Some(chunk) = resp.chunk().await.unwrap_or(None) {
            buf.extend_from_slice(&chunk);
            let text = String::from_utf8_lossy(&buf);
            if text.contains("event: state_change") {
                assert!(text.contains("\"kind\":\"heartbeat\""), "got: {text}");
                break;
            }
        }
    }
    let text = String::from_utf8_lossy(&buf);
    assert!(
        text.contains("event: state_change"),
        "no state_change seen: {text}"
    );

    let _ = shutdown_tx.send(());
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
        "entry_id": "e1",
        "started_at": now - ChDuration::seconds(900),
        "last_progress": now, // fresh progress: deadline is the only trigger
        "deadline": now + deadline_offset,
        "wall_clock_budget_seconds": 900.0,
        "phase": "running",
        "progress": 0.5,
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

// ---------------------------------------------------------------------
// Dashboard API gaps: run-log tail, in-flight lineage, per-run progress,
// and the health-footer fields.
// ---------------------------------------------------------------------

#[tokio::test]
async fn run_log_endpoint_tails_active_run_events() {
    let (_t, paths) = make_workspace();

    // A synthetic active-run events.jsonl: 50 events, mixed envelope
    // shapes (camelCase + the normalized {kind,payload} shape).
    let events_path = paths.workspace.join("run_events.jsonl");
    let mut body = String::new();
    for i in 0..48 {
        body.push_str(&format!(
            "{{\"emittedAt\":\"2026-05-16T04:36:{:02}Z\",\"sequence\":{i},\
              \"taskProgress\":{{\"taskId\":\"t{i}\",\"fraction\":0.5}}}}\n",
            i % 60
        ));
    }
    // A camelCase steering decision and a normalized-shape event.
    body.push_str(
        "{\"emittedAt\":\"2026-05-16T04:37:00Z\",\"sequence\":48,\
          \"steeringDecisionMade\":{\"agentName\":\"coordinator_agent\",\"outcome\":\"no_drift\"}}\n",
    );
    body.push_str(
        "{\"emitted_at\":{\"seconds\":1778906222,\"nanos\":0},\"sequence\":49,\
          \"kind\":\"pinResolved\",\"payload\":{\"agent_name\":\"research_agent\"}}\n",
    );
    std::fs::write(&events_path, body).unwrap();

    let run = serde_json::json!({
        "run_id": "v1--entry",
        "events_jsonl_path": events_path.display().to_string(),
    });
    std::fs::write(
        paths.active_runs_dir().join("v1--entry.json"),
        serde_json::to_vec(&run).unwrap(),
    )
    .unwrap();

    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    // Default limit (40): last 40 of 50.
    let resp = client
        .get(format!("{base}/api/run-log"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();
    let events = r["events"].as_array().unwrap();
    assert_eq!(events.len(), 40, "default limit is 40");

    // The last two events: camelCase + normalized kinds, both snake_cased.
    let last = events.last().unwrap();
    assert_eq!(last["seq"], 49);
    assert_eq!(last["kind"], "pin_resolved");
    let penultimate = &events[events.len() - 2];
    assert_eq!(penultimate["kind"], "steering_decision_made");
    assert_eq!(
        penultimate["summary"],
        "steering_decision_made: coordinator_agent: no_drift"
    );

    // Explicit limit.
    let r: Value = client
        .get(format!("{base}/api/run-log?limit=5"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r["events"].as_array().unwrap().len(), 5);

    let _ = shutdown.send(());
}

#[tokio::test]
async fn run_log_endpoint_empty_when_no_events_file() {
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/run-log"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();
    assert_eq!(r["events"], serde_json::json!([]));

    let _ = shutdown.send(());
}

#[tokio::test]
async fn lineage_endpoint_includes_in_flight_generation() {
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

    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/lineage"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();
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

    let _ = shutdown.send(());
}

#[tokio::test]
async fn active_runs_endpoint_carries_computed_progress() {
    let (_t, paths) = make_workspace();
    let now = Utc::now();

    // A run a quarter of the way through a 1000s budget window.
    let ar = serde_json::json!({
        "run_id": "v1--entry",
        "pid": 4242,
        "entry_id": "entry",
        "started_at": now - ChDuration::seconds(250),
        "deadline": now + ChDuration::seconds(750),
        "wall_clock_budget_seconds": 1000.0,
    });
    std::fs::write(
        paths.active_runs_dir().join("v1--entry.json"),
        serde_json::to_vec(&ar).unwrap(),
    )
    .unwrap();

    // A run already past its deadline: fraction must clamp to 1.0.
    let ar_late = serde_json::json!({
        "run_id": "v2--late",
        "pid": 4343,
        "started_at": now - ChDuration::seconds(2000),
        "deadline": now - ChDuration::seconds(1000),
    });
    std::fs::write(
        paths.active_runs_dir().join("v2--late.json"),
        serde_json::to_vec(&ar_late).unwrap(),
    )
    .unwrap();

    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/active-runs"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();
    let runs = r.as_array().unwrap();
    assert_eq!(runs.len(), 2);

    // Sorted by run_id: v1--entry first.
    let entry = &runs[0];
    assert_eq!(entry["run_id"], "v1--entry");
    let progress = entry["progress"].as_f64().unwrap();
    assert!(
        (0.20..=0.30).contains(&progress),
        "expected ~0.25, got {progress}"
    );
    let budget = entry["budget_seconds"].as_i64().unwrap();
    assert!((990..=1010).contains(&budget), "got budget {budget}");
    let elapsed = entry["elapsed_seconds"].as_i64().unwrap();
    assert!((240..=260).contains(&elapsed), "got elapsed {elapsed}");

    // The over-deadline run: progress clamps to exactly 1.0.
    let late = &runs[1];
    assert_eq!(late["run_id"], "v2--late");
    assert_eq!(late["progress"].as_f64().unwrap(), 1.0);

    let _ = shutdown.send(());
}

#[tokio::test]
async fn health_endpoint_carries_port_and_build() {
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/health"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();

    // `port` is the actually-bound port and non-zero.
    let port = r["port"].as_u64().unwrap();
    assert_eq!(port, handle.addr.port() as u64);
    assert!(port > 0);

    // `build` is present and non-empty.
    let build = r["build"].as_str().unwrap();
    assert!(!build.is_empty(), "build identifier must be non-empty");

    let _ = shutdown.send(());
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
    let (handle, shutdown) = start_server(paths.clone(), true).await;
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
    assert_eq!(sup["read_only"], true);
    assert_eq!(sup["dashboard_disabled"], false);
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
async fn statusz_surfaces_fold_diagnostics_from_a_torn_log() {
    // Write an active-tournament event log with two torn (un-parseable)
    // lines and a seq gap. Hitting the canonical fold path
    // (/api/active-tournament) accumulates the counters, which /statusz then
    // surfaces — closing the Rust-drops-vs-Python-raises visibility gap.
    let (_t, paths) = make_workspace();
    write_statusz_state(&paths);
    let log = [
        r#"{"seq":1,"ts":"t","type":"Snapshot","payload":{"tournament_id":"t1","entries":[]}}"#,
        r#"{"seq":2,"ts":"t","type":"EntryUp"#, // torn mid-line
        r#"garbage line"#,                      // not json
        r#"{"seq":5,"ts":"t","type":"PartialAggregate","payload":{}}"#, // seq gap 1 -> 5 (over good lines)
    ]
    .join("\n");
    std::fs::write(paths.active_tournament_log(), log).unwrap();

    let (handle, shutdown) = start_server(paths.clone(), false).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    // Drive the fold path twice so the cumulative counters accumulate.
    for _ in 0..2 {
        let r = client
            .get(format!("{base}/api/active-tournament"))
            .send()
            .await
            .unwrap();
        assert_eq!(r.status(), 200);
    }

    let r: Value = client
        .get(format!("{base}/statusz.json"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let fd = &r["fold_diagnostics"];
    // Two torn lines per fold * two folds = 4 cumulative parse failures.
    assert_eq!(fd["parse_failures"].as_u64().unwrap(), 4);
    // One seq gap per fold (1 -> 5 across the dropped lines) * two folds = 2.
    assert_eq!(fd["seq_gaps"].as_u64().unwrap(), 2);
    assert_eq!(fd["folds"].as_u64().unwrap(), 2);

    // The HTML surface renders the same counters.
    let html = client
        .get(format!("{base}/statusz"))
        .send()
        .await
        .unwrap()
        .text()
        .await
        .unwrap();
    assert!(html.contains("fold diagnostics"));
    assert!(html.contains("torn writes"));

    let _ = shutdown.send(());
}

#[tokio::test]
async fn statusz_html_serves_non_empty_page() {
    let (_t, paths) = make_workspace();
    write_statusz_state(&paths);
    let (handle, shutdown) = start_server(paths.clone(), true).await;
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
async fn statusz_routes_reachable_with_no_dashboard() {
    let (_t, paths) = make_workspace();
    write_statusz_state(&paths);
    let opts = server::ServeOptions {
        read_only: true,
        dashboard_disabled: true, // --no-dashboard
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        fold_diagnostics: Arc::new(zicato_supervisor::fold_stats::FoldDiagnostics::new()),
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

    // Both /statusz surfaces are still served in watchdog-only mode.
    let html = client.get(format!("{base}/statusz")).send().await.unwrap();
    assert_eq!(html.status(), 200);
    assert!(!html.text().await.unwrap().is_empty());

    let json = client
        .get(format!("{base}/statusz.json"))
        .send()
        .await
        .unwrap();
    assert_eq!(json.status(), 200);
    let r: Value = json.json().await.unwrap();
    assert_eq!(r["supervisor"]["dashboard_disabled"], true);
    assert_eq!(r["runs"].as_array().unwrap().len(), 2);

    // The full dashboard routes are NOT mounted in this mode.
    let dash = client
        .get(format!("{base}/api/state"))
        .send()
        .await
        .unwrap();
    assert_eq!(dash.status(), 404);
    let root = client.get(format!("{base}/")).send().await.unwrap();
    assert_eq!(root.status(), 404);

    let _ = shutdown.send(());
}

#[tokio::test]
async fn statusz_no_runs_summary_is_clean() {
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone(), true).await;
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
        read_only: true,
        dashboard_disabled: false,
        heartbeat_stale_threshold_seconds: 30,
        action_log: action_log.clone(),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        fold_diagnostics: Arc::new(zicato_supervisor::fold_stats::FoldDiagnostics::new()),
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
    let (handle, shutdown) = start_server(paths.clone(), true).await;
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
        read_only: true,
        dashboard_disabled: false,
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        fold_diagnostics: Arc::new(zicato_supervisor::fold_stats::FoldDiagnostics::new()),
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
        read_only: true,
        dashboard_disabled: false,
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        fold_diagnostics: Arc::new(zicato_supervisor::fold_stats::FoldDiagnostics::new()),
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
        serde_json::json!({"adk_entrypoint": "m:a", "mutable_trees": ["/reg/agent"]}).to_string(),
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
        read_only: true,
        dashboard_disabled: false,
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        fold_diagnostics: Arc::new(zicato_supervisor::fold_stats::FoldDiagnostics::new()),
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
        serde_json::json!({"adk_entrypoint": "m:a", "mutable_trees": ["/reg/agent"]}).to_string(),
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
        read_only: true,
        dashboard_disabled: false,
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        fold_diagnostics: Arc::new(zicato_supervisor::fold_stats::FoldDiagnostics::new()),
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
        read_only: true,
        dashboard_disabled: false,
        heartbeat_stale_threshold_seconds: 30,
        action_log: Arc::new(WatchdogLog::new()),
        seq_liveness: Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new())),
        fold_diagnostics: Arc::new(zicato_supervisor::fold_stats::FoldDiagnostics::new()),
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
