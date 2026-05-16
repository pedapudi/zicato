//! Tail recent goldfive events for the dashboard's "Log tail" panel.
//!
//! goldfive writes one JSON object per line to `events.jsonl`. Two
//! envelope shapes occur on disk:
//!
//!   1. The common camelCase shape — `MessageToJson` without
//!      `preserving_proto_field_name`: the payload kind is a top-level
//!      envelope key (`steeringDecisionMade`, `taskProgress`, ...)
//!      alongside `eventId` / `runId` / `sequence` / `emittedAt`.
//!   2. A normalized shape — `{kind, payload, emitted_at, event_id,
//!      run_id, session_id}` — emitted by the reducer's proto-reparse
//!      path, where `emitted_at` is a `{seconds, nanos}` proto timestamp.
//!
//! Both are handled. Kinds are normalized to snake_case (the same fix as
//! zicato#1) so the dashboard keys on one stable vocabulary.
//!
//! Everything here is best-effort: a missing file, a truncated tail line,
//! or an unparseable record degrades to fewer/zero events rather than an
//! error. The endpoint never 500s.

use crate::reader::WorkspacePaths;
use serde::Serialize;
use std::path::{Path, PathBuf};
use tracing::warn;
use walkdir::WalkDir;

/// Default number of trailing events returned when `?limit=` is absent.
pub const DEFAULT_LIMIT: usize = 40;

/// Hard ceiling on `?limit=` so a hostile query can't ask us to
/// materialise an unbounded slice.
const MAX_LIMIT: usize = 500;

/// Envelope keys that are *not* the payload kind in the camelCase shape.
const ENVELOPE_KEYS: &[&str] = &[
    "emittedAt",
    "emitted_at",
    "eventId",
    "event_id",
    "runId",
    "run_id",
    "sessionId",
    "session_id",
    "sequence",
    "seq",
    "kind",
    "payload",
];

/// One compact log record returned by `GET /api/run-log`.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct LogRecord {
    /// The event's sequence number when present, else `null`.
    pub seq: Option<i64>,
    /// The goldfive payload kind, snake_cased.
    pub kind: String,
    /// Emission timestamp as an ISO-8601 / RFC-3339 string when known.
    pub ts: Option<String>,
    /// A short human-readable summary; falls back to `kind`.
    pub summary: String,
}

/// The `GET /api/run-log` response body.
#[derive(Debug, Clone, Serialize)]
pub struct RunLog {
    pub events: Vec<LogRecord>,
}

/// Convert a `camelCase` or `PascalCase` identifier to `snake_case`.
/// Idempotent on input that is already snake_case.
fn to_snake(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 4);
    let mut prev_lower_or_digit = false;
    for ch in s.chars() {
        if ch.is_ascii_uppercase() {
            if prev_lower_or_digit {
                out.push('_');
            }
            out.push(ch.to_ascii_lowercase());
            prev_lower_or_digit = false;
        } else {
            out.push(ch);
            prev_lower_or_digit = ch.is_ascii_lowercase() || ch.is_ascii_digit();
        }
    }
    out
}

/// Clamp a requested limit into `1..=MAX_LIMIT`, defaulting an absent or
/// zero value to `DEFAULT_LIMIT`.
pub fn clamp_limit(requested: Option<usize>) -> usize {
    match requested {
        None => DEFAULT_LIMIT,
        Some(0) => DEFAULT_LIMIT,
        Some(n) => n.min(MAX_LIMIT),
    }
}

/// Pull a string field from a JSON object regardless of camel/snake
/// spelling.
fn str_either(obj: &serde_json::Value, camel: &str, snake: &str) -> Option<String> {
    obj.get(camel)
        .or_else(|| obj.get(snake))
        .and_then(|v| v.as_str())
        .map(str::to_string)
}

/// Extract the sequence number, tolerating both an integer and the
/// stringified integer goldfive sometimes writes.
fn extract_seq(obj: &serde_json::Value) -> Option<i64> {
    let v = obj.get("sequence").or_else(|| obj.get("seq"))?;
    v.as_i64()
        .or_else(|| v.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
}

/// Extract an emission timestamp as a display string. Handles an RFC-3339
/// string and the proto `{seconds, nanos}` shape.
fn extract_ts(obj: &serde_json::Value) -> Option<String> {
    let raw = obj.get("emittedAt").or_else(|| obj.get("emitted_at"))?;
    if let Some(s) = raw.as_str() {
        return Some(s.to_string());
    }
    // Proto Timestamp: {"seconds": <i64>, "nanos": <i32>}.
    if let Some(obj) = raw.as_object() {
        let secs = obj.get("seconds").and_then(|v| {
            v.as_i64()
                .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
        })?;
        let nanos = obj
            .get("nanos")
            .and_then(|v| {
                v.as_i64()
                    .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
            })
            .unwrap_or(0);
        if let Some(dt) = chrono::DateTime::from_timestamp(secs, nanos as u32) {
            return Some(dt.to_rfc3339());
        }
    }
    None
}

/// Identify the payload kind and payload object for one event envelope.
///
/// Shape 2 (`{kind, payload, ...}`) wins when an explicit `kind` is
/// present; otherwise the first non-envelope top-level key is the kind
/// and its value the payload (shape 1).
fn kind_and_payload(obj: &serde_json::Value) -> (String, Option<&serde_json::Value>) {
    if let Some(kind) = obj.get("kind").and_then(|v| v.as_str()) {
        return (to_snake(kind), obj.get("payload"));
    }
    let map = match obj.as_object() {
        Some(m) => m,
        None => return ("unknown".to_string(), None),
    };
    for (k, v) in map {
        if ENVELOPE_KEYS.contains(&k.as_str()) {
            continue;
        }
        return (to_snake(k), Some(v));
    }
    ("unknown".to_string(), None)
}

/// Best-effort one-line summary for a payload. Pulls the most
/// informative field available (agent name, drift kind, task detail,
/// goal summary, ...) and otherwise falls back to the kind itself.
fn summarize(kind: &str, payload: Option<&serde_json::Value>) -> String {
    let p = match payload.and_then(|v| v.as_object()) {
        Some(p) => p,
        None => return kind.to_string(),
    };
    let obj = serde_json::Value::Object(p.clone());
    let get = |camel: &str, snake: &str| str_either(&obj, camel, snake);

    let detail = match kind {
        "run_started" => get("goalSummary", "goal_summary"),
        "conversation_started" => get("conversationId", "conversation_id"),
        "goal_derived" => obj
            .get("goals")
            .and_then(|g| g.as_array())
            .and_then(|a| a.first())
            .and_then(|g| g.get("summary").and_then(|v| v.as_str()))
            .map(str::to_string),
        "drift_detected" => {
            let agent = get("currentAgentId", "current_agent_id");
            let what = get("detail", "detail");
            match (agent, what) {
                (Some(a), Some(d)) => Some(format!("{a}: {d}")),
                (Some(a), None) => Some(a),
                (None, d) => d,
            }
        }
        "steering_decision_made" => {
            let agent = get("agentName", "agent_name");
            let outcome = get("outcome", "outcome");
            match (agent, outcome) {
                (Some(a), Some(o)) => Some(format!("{a}: {o}")),
                (Some(a), None) => Some(a),
                (None, o) => o,
            }
        }
        "reasoning_judge_invoked" => {
            get("classification", "classification").or_else(|| get("reason", "reason"))
        }
        "task_progress" => {
            let task = get("taskId", "task_id");
            let frac = obj.get("fraction").and_then(|v| v.as_f64());
            match (task, frac) {
                (Some(t), Some(f)) => Some(format!("{t} ({:.0}%)", f * 100.0)),
                (Some(t), None) => Some(t),
                (None, Some(f)) => Some(format!("{:.0}%", f * 100.0)),
                (None, None) => None,
            }
        }
        "task_started" | "task_completed" => get("detail", "detail")
            .or_else(|| get("summary", "summary"))
            .or_else(|| get("taskId", "task_id")),
        "task_transitioned" => {
            let task = get("taskId", "task_id");
            let to = get("toStatus", "to_status");
            match (task, to) {
                (Some(t), Some(s)) => Some(format!("{t} -> {s}")),
                (Some(t), None) => Some(t),
                (None, s) => s,
            }
        }
        "delegation_observed" => {
            let from = get("fromAgent", "from_agent");
            let to = get("toAgent", "to_agent");
            match (from, to) {
                (Some(f), Some(t)) => Some(format!("{f} -> {t}")),
                _ => None,
            }
        }
        "agent_invocation_started"
        | "agent_invocation_completed"
        | "invocation_boundary_entered"
        | "invocation_boundary_exited" => get("agentName", "agent_name"),
        "goldfive_llm_call_start" | "goldfive_llm_call_end" => get("name", "name"),
        "pin_resolved" => get("agentName", "agent_name").or_else(|| get("taskId", "task_id")),
        _ => None,
    };

    // A generic fallback chain for kinds we don't special-case.
    let detail = detail
        .or_else(|| get("agentName", "agent_name"))
        .or_else(|| get("detail", "detail"))
        .or_else(|| get("summary", "summary"))
        .or_else(|| get("reason", "reason"))
        .or_else(|| get("taskId", "task_id"));

    match detail {
        Some(d) if !d.trim().is_empty() => {
            let d = d.trim();
            // Keep the line short — this is a tail panel, not a log viewer.
            if d.chars().count() > 100 {
                let cut: String = d.chars().take(100).collect();
                format!("{kind}: {cut}...")
            } else {
                format!("{kind}: {d}")
            }
        }
        _ => kind.to_string(),
    }
}

/// Parse one `events.jsonl` line into a compact record. Returns `None`
/// for blank lines and unparseable JSON.
fn parse_line(line: &str) -> Option<LogRecord> {
    let line = line.trim();
    if line.is_empty() {
        return None;
    }
    let obj: serde_json::Value = serde_json::from_str(line).ok()?;
    let (kind, payload) = kind_and_payload(&obj);
    Some(LogRecord {
        seq: extract_seq(&obj),
        ts: extract_ts(&obj),
        summary: summarize(&kind, payload),
        kind,
    })
}

/// Read the last `limit` parseable events from `events.jsonl` at `path`.
fn tail_events(path: &Path, limit: usize) -> Vec<LogRecord> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Vec::new(),
        Err(e) => {
            warn!(?path, error=%e, "failed to read events.jsonl");
            return Vec::new();
        }
    };
    let mut records: Vec<LogRecord> = text.lines().filter_map(parse_line).collect();
    if records.len() > limit {
        records.drain(0..records.len() - limit);
    }
    records
}

/// Locate the `events.jsonl` to tail.
///
/// Prefers the newest `active_runs/*.json` (by file mtime) and uses its
/// `events_jsonl_path`. When there is no active run, falls back to the
/// most recently modified `events.jsonl` anywhere under `epochs/`.
fn locate_events_file(paths: &WorkspacePaths) -> Option<PathBuf> {
    if let Some(p) = newest_active_run_events(paths) {
        if p.exists() {
            return Some(p);
        }
    }
    newest_epoch_events(paths)
}

/// `events_jsonl_path` of the most recently written `active_runs/*.json`.
fn newest_active_run_events(paths: &WorkspacePaths) -> Option<PathBuf> {
    let dir = paths.active_runs_dir();
    let entries = std::fs::read_dir(&dir).ok()?;
    let mut newest: Option<(std::time::SystemTime, PathBuf)> = None;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|s| s.to_str()) != Some("json") {
            continue;
        }
        let mtime = entry
            .metadata()
            .and_then(|m| m.modified())
            .unwrap_or(std::time::UNIX_EPOCH);
        if newest.as_ref().map(|(t, _)| mtime > *t).unwrap_or(true) {
            newest = Some((mtime, path));
        }
    }
    let (_, run_file) = newest?;
    let run: crate::state::ActiveRun = crate::reader::read_json_opt(&run_file)?;
    run.events_jsonl_path.map(PathBuf::from)
}

/// The most recently modified `events.jsonl` under `epochs/`.
fn newest_epoch_events(paths: &WorkspacePaths) -> Option<PathBuf> {
    if !paths.epochs.exists() {
        return None;
    }
    let mut newest: Option<(std::time::SystemTime, PathBuf)> = None;
    for entry in WalkDir::new(&paths.epochs)
        .into_iter()
        .filter_map(Result::ok)
    {
        if entry.file_name() != "events.jsonl" {
            continue;
        }
        let mtime = entry
            .metadata()
            .ok()
            .and_then(|m| m.modified().ok())
            .unwrap_or(std::time::UNIX_EPOCH);
        if newest.as_ref().map(|(t, _)| mtime > *t).unwrap_or(true) {
            newest = Some((mtime, entry.path().to_path_buf()));
        }
    }
    newest.map(|(_, p)| p)
}

/// Assemble `GET /api/run-log`. Never fails: a missing/absent log file
/// yields an empty `events` array.
pub fn build_run_log(paths: &WorkspacePaths, limit: usize) -> RunLog {
    let events = match locate_events_file(paths) {
        Some(path) => tail_events(&path, limit),
        None => Vec::new(),
    };
    RunLog { events }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn ws() -> (TempDir, WorkspacePaths) {
        let tmp = TempDir::new().unwrap();
        let p = WorkspacePaths::new(tmp.path().to_path_buf());
        std::fs::create_dir_all(p.active_runs_dir()).unwrap();
        std::fs::create_dir_all(&p.epochs).unwrap();
        (tmp, p)
    }

    #[test]
    fn snake_case_conversion() {
        assert_eq!(to_snake("steeringDecisionMade"), "steering_decision_made");
        assert_eq!(to_snake("taskProgress"), "task_progress");
        assert_eq!(to_snake("goldfiveLlmCallStart"), "goldfive_llm_call_start");
        // Already snake_case is untouched.
        assert_eq!(to_snake("pin_resolved"), "pin_resolved");
        assert_eq!(to_snake("RunStarted"), "run_started");
    }

    #[test]
    fn clamp_limit_defaults_and_caps() {
        assert_eq!(clamp_limit(None), DEFAULT_LIMIT);
        assert_eq!(clamp_limit(Some(0)), DEFAULT_LIMIT);
        assert_eq!(clamp_limit(Some(10)), 10);
        assert_eq!(clamp_limit(Some(10_000)), MAX_LIMIT);
    }

    #[test]
    fn parses_camelcase_envelope() {
        let line = r#"{"emittedAt":"2026-05-16T04:36:54Z","eventId":"x:1:y","runId":"x","sequence":"1","steeringDecisionMade":{"agentName":"coordinator_agent","outcome":"no_drift"}}"#;
        let rec = parse_line(line).unwrap();
        assert_eq!(rec.kind, "steering_decision_made");
        assert_eq!(rec.seq, Some(1));
        assert_eq!(rec.ts.as_deref(), Some("2026-05-16T04:36:54Z"));
        assert_eq!(
            rec.summary,
            "steering_decision_made: coordinator_agent: no_drift"
        );
    }

    #[test]
    fn parses_normalized_kind_payload_envelope() {
        let line = r#"{"emitted_at":{"seconds":1778906222,"nanos":939036939},"event_id":"x:12:y","kind":"pin_resolved","payload":{"agent_name":"research_agent","task_id":"research_transformers"},"run_id":"x"}"#;
        let rec = parse_line(line).unwrap();
        assert_eq!(rec.kind, "pin_resolved");
        assert_eq!(rec.summary, "pin_resolved: research_agent");
        assert!(rec.ts.is_some());
    }

    #[test]
    fn unknown_kind_summary_falls_back_to_kind() {
        let line = r#"{"sequence":3,"someBrandNewEvent":{}}"#;
        let rec = parse_line(line).unwrap();
        assert_eq!(rec.kind, "some_brand_new_event");
        assert_eq!(rec.summary, "some_brand_new_event");
    }

    #[test]
    fn malformed_line_is_skipped() {
        assert!(parse_line("{not json").is_none());
        assert!(parse_line("").is_none());
        assert!(parse_line("   ").is_none());
    }

    #[test]
    fn tail_returns_last_n() {
        let (_t, p) = ws();
        let f = p.epochs.join("events.jsonl");
        let mut body = String::new();
        for i in 0..10 {
            body.push_str(&format!(
                "{{\"sequence\":{i},\"taskProgress\":{{\"taskId\":\"t{i}\"}}}}\n"
            ));
        }
        std::fs::write(&f, body).unwrap();
        let recs = tail_events(&f, 3);
        assert_eq!(recs.len(), 3);
        assert_eq!(recs[0].seq, Some(7));
        assert_eq!(recs[2].seq, Some(9));
    }

    #[test]
    fn missing_file_yields_empty() {
        let (_t, p) = ws();
        let log = build_run_log(&p, 40);
        assert!(log.events.is_empty());
    }

    #[test]
    fn active_run_events_file_is_preferred() {
        let (_t, p) = ws();
        // An epoch-level events.jsonl exists...
        let epoch_log = p.epochs.join("old_events.jsonl");
        std::fs::write(&epoch_log, "{\"sequence\":1,\"taskStarted\":{}}\n").unwrap();
        // ...but an active run points at a different file.
        let run_log = p.workspace.join("run_events.jsonl");
        std::fs::write(
            &run_log,
            "{\"sequence\":99,\"runStarted\":{\"goalSummary\":\"do the thing\"}}\n",
        )
        .unwrap();
        let run = serde_json::json!({
            "run_id": "v1--entry",
            "events_jsonl_path": run_log.display().to_string(),
        });
        std::fs::write(
            p.active_runs_dir().join("v1--entry.json"),
            serde_json::to_vec(&run).unwrap(),
        )
        .unwrap();
        let log = build_run_log(&p, 40);
        assert_eq!(log.events.len(), 1);
        assert_eq!(log.events[0].seq, Some(99));
        assert_eq!(log.events[0].kind, "run_started");
    }

    #[test]
    fn falls_back_to_epoch_events_when_no_active_run() {
        let (_t, p) = ws();
        let gen_dir = p
            .epochs
            .join("e0")
            .join("generations")
            .join("v0")
            .join("runs")
            .join("r1");
        std::fs::create_dir_all(&gen_dir).unwrap();
        std::fs::write(
            gen_dir.join("events.jsonl"),
            "{\"sequence\":5,\"taskCompleted\":{\"summary\":\"done\"}}\n",
        )
        .unwrap();
        let log = build_run_log(&p, 40);
        assert_eq!(log.events.len(), 1);
        assert_eq!(log.events[0].kind, "task_completed");
    }
}
