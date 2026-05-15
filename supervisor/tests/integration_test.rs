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
