//! End-to-end tests: spin up the supervisor server against a synthetic
//! workspace, exercise GET endpoints, verify control-file writes, and
//! check signal escalation against a real child process.

use chrono::{Duration as ChDuration, Utc};
use serde_json::Value;
use std::net::{IpAddr, Ipv4Addr};
use std::path::PathBuf;
use std::time::Duration;
use tempfile::TempDir;
use tokio::sync::broadcast;
use zicato_supervisor::{reader, server, signal as sigutil, state, watcher};

fn make_workspace() -> (TempDir, reader::WorkspacePaths) {
    let tmp = TempDir::new().unwrap();
    let ws = tmp.path().to_path_buf();
    std::fs::create_dir_all(ws.join("runtime/active_runs")).unwrap();
    std::fs::create_dir_all(ws.join("runtime/control")).unwrap();
    std::fs::create_dir_all(ws.join("epochs")).unwrap();
    (tmp, reader::WorkspacePaths::new(ws))
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
        read_only,
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

/// Lay down a full epoch (board / rubric / scoring / config / mutations)
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

    std::fs::write(dir.join("rubric.md"), "# full rubric text\nbody").unwrap();

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
    assert_eq!(board[0]["id"], "waffles_single");
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

    assert_eq!(r["rubric"], "# full rubric text\nbody");
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
async fn epoch_endpoint_missing_rubric_yields_empty_string() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "e_no_rubric");
    std::fs::remove_file(paths.epochs.join("e_no_rubric").join("rubric.md")).unwrap();
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
    assert_eq!(r["rubric"], "");

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
async fn rubric_post_writes_replacement_file() {
    let (_t, paths) = make_workspace();
    let (handle, shutdown) = start_server(paths.clone(), false).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let payload = "you are a better judge now\n";
    let r = client
        .post(format!("{base}/api/control/rubric"))
        .body(payload)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 202);

    let marker = paths.control_dir().join("rubric_replacement.txt");
    let got = std::fs::read_to_string(&marker).unwrap();
    assert_eq!(got, payload);

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

/// Build a small `.zicato/index/index.db` with the tables the
/// tournament endpoints read.
fn write_index_db(paths: &reader::WorkspacePaths) {
    use rusqlite::Connection;
    let dir = paths.workspace.join("index");
    std::fs::create_dir_all(&dir).unwrap();
    let conn = Connection::open(dir.join("index.db")).unwrap();
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
}

#[tokio::test]
async fn tournaments_endpoint_returns_bracket() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "2026-05-15_e0");
    write_index_db(&paths);
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/tournaments"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();

    assert_eq!(r["epoch_id"], "2026-05-15_e0");
    assert_eq!(
        r["champion_lineage"],
        serde_json::json!(["v0", "v2"]),
        "got: {r}"
    );
    let matchups = r["matchups"].as_array().unwrap();
    assert_eq!(matchups.len(), 2);
    assert_eq!(matchups[0]["champion"], "v0");
    assert_eq!(matchups[0]["challenger"], "v1");
    assert_eq!(matchups[0]["decision"], "rejected");
    assert_eq!(matchups[0]["delta_scalar"], 0.0);
    assert_eq!(matchups[0]["rejection_reason"], "worse drift overall");
    assert_eq!(matchups[0]["hypothesis_core_idea"], "tighten the planner");
    assert_eq!(matchups[0]["ran_at"], "2026-05-15T01:00:00Z");
    // No `note` when the index exists.
    assert!(r.get("note").is_none());

    let _ = shutdown.send(());
}

#[tokio::test]
async fn tournaments_endpoint_notes_missing_index() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "2026-05-15_e0");
    // No index.db written.
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/tournaments"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();
    assert_eq!(r["epoch_id"], "2026-05-15_e0");
    assert_eq!(r["champion_lineage"], serde_json::json!([]));
    assert_eq!(r["matchups"], serde_json::json!([]));
    assert_eq!(r["note"], "index not built; run zicato reindex");

    let _ = shutdown.send(());
}

#[tokio::test]
async fn tournament_detail_endpoint_returns_full_matchup() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "2026-05-15_e0");
    write_index_db(&paths);
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/tournaments/v1"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();

    assert_eq!(r["epoch_id"], "2026-05-15_e0");
    assert_eq!(r["generation_id"], "v1");
    assert_eq!(r["champion"], "v0");
    assert_eq!(r["decision"], "rejected");
    assert_eq!(r["rejection_reason"], "worse drift overall");
    assert_eq!(r["ran_at"], "2026-05-15T01:00:00Z");
    assert_eq!(r["parent_scalar"], 0.8);
    assert_eq!(r["child_scalar"], 0.8);
    assert_eq!(r["delta_scalar"], 0.0);

    assert_eq!(r["hypothesis"]["core_idea"], "tighten the planner");
    assert_eq!(r["hypothesis"]["why"], "planner overshoots");

    let patches = r["patches"].as_array().unwrap();
    assert_eq!(patches.len(), 1);
    assert_eq!(patches[0]["patch_id"], "p1");
    assert_eq!(patches[0]["op"], "replace");

    let grid = r["ab_grid"].as_array().unwrap();
    assert_eq!(grid.len(), 2);
    // b1: parent 0.4 -> child 0.6 == regressed.
    assert_eq!(grid[0]["entry_id"], "b1");
    assert_eq!(grid[0]["parent_drift_loss"], 0.4);
    assert_eq!(grid[0]["child_drift_loss"], 0.6);
    assert_eq!(grid[0]["verdict"], "regressed");
    // b2: parent 0.1 -> child 0.1 == flat.
    assert_eq!(grid[1]["entry_id"], "b2");
    assert_eq!(grid[1]["verdict"], "flat");

    let _ = shutdown.send(());
}

#[tokio::test]
async fn tournament_detail_missing_index_is_200() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "2026-05-15_e0");
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/tournaments/v1"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();
    assert_eq!(r["generation_id"], "v1");
    assert_eq!(r["note"], "index not built; run zicato reindex");
    assert_eq!(r["patches"], serde_json::json!([]));
    assert_eq!(r["ab_grid"], serde_json::json!([]));

    let _ = shutdown.send(());
}

#[tokio::test]
async fn health_report_endpoint_returns_latest_round() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "2026-05-15_e0");
    let health_dir = paths.epochs.join("2026-05-15_e0").join("health");
    std::fs::create_dir_all(&health_dir).unwrap();
    std::fs::write(
        health_dir.join("round_1.json"),
        r#"{"epoch_id":"2026-05-15_e0","healthy":true,"findings":[]}"#,
    )
    .unwrap();
    let report = serde_json::json!({
        "epoch_id": "2026-05-15_e0",
        "healthy": false,
        "checked_at": "2026-05-15T03:00:00Z",
        "findings": [
            {"code": "stalled_runs", "severity": "warn",
             "summary": "two runs idle", "detail": "no progress in 10m"},
        ],
    });
    std::fs::write(health_dir.join("round_7.json"), report.to_string()).unwrap();

    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/health-report"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();
    assert_eq!(r["epoch_id"], "2026-05-15_e0");
    assert_eq!(r["healthy"], false);
    assert_eq!(r["checked_at"], "2026-05-15T03:00:00Z");
    let findings = r["findings"].as_array().unwrap();
    assert_eq!(findings.len(), 1);
    assert_eq!(findings[0]["code"], "stalled_runs");
    assert_eq!(findings[0]["severity"], "warn");

    let _ = shutdown.send(());
}

#[tokio::test]
async fn health_report_endpoint_healthy_when_no_report() {
    let (_t, paths) = make_workspace();
    write_full_epoch(&paths, "2026-05-15_e0");
    let (handle, shutdown) = start_server(paths.clone(), true).await;
    let base = format!("http://{}", handle.addr);
    let client = reqwest::Client::new();

    let resp = client
        .get(format!("{base}/api/health-report"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let r: Value = resp.json().await.unwrap();
    assert_eq!(r["healthy"], true);
    assert_eq!(r["findings"], serde_json::json!([]));

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
async fn watchdog_kill_decision_fires_when_heartbeat_stale() {
    use zicato_supervisor::watchdog::{decide_heartbeat, HeartbeatAction, Thresholds};
    let thresholds = Thresholds {
        heartbeat_stale_warn: Duration::from_secs(1),
        heartbeat_stale_kill: Duration::from_secs(2),
        run_stale_warn: Duration::from_secs(10),
        run_stale_kill: Duration::from_secs(20),
        grace: Duration::from_millis(200),
    };
    let now = Utc::now();
    let hb = state::Heartbeat {
        pid: Some(424242),
        last_heartbeat: Some(now - ChDuration::seconds(10)),
        ..Default::default()
    };
    let action = decide_heartbeat(Some(&hb), now, &thresholds);
    assert_eq!(action, HeartbeatAction::Kill { pid: 424242 });
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
        true,
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
