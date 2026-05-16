//! Assemble the current epoch's full evaluation contract for the dashboard.
//!
//! An epoch is defined by a handful of files under
//! `.zicato/epochs/{epoch_id}/` (`board.jsonl`, `brief.md`,
//! `scoring.json`, `config.json`, optional `mutations.json`) plus the
//! workspace-level `.zicato/config.json` (registered harness entrypoint
//! and mutable trees).
//!
//! Every component degrades gracefully: a missing or malformed file
//! yields an empty/`null` value for that component rather than failing
//! the whole response. When there is no current epoch at all the result
//! is `{ "epoch_id": null }`.

use crate::reader::WorkspacePaths;
use serde::Serialize;
use tracing::warn;

/// Maximum length of the truncated text previews (`input_preview`,
/// mutation `preview`).
const PREVIEW_CHARS: usize = 120;

/// Full epoch definition returned by `/api/epoch` and embedded in the
/// `/api/state` snapshot under the `epoch` key.
#[derive(Debug, Clone, Default, Serialize)]
pub struct EpochView {
    /// `None` when there is no current epoch marker.
    pub epoch_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub contract_hash: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub closed: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub harness: Option<Harness>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub board: Option<Vec<BoardEntry>>,
    /// The epoch's frozen proposer brief. Serialized as `brief`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub brief: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scoring: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mutations: Option<Vec<Mutation>>,
}

/// Registered harness: the adapter entrypoint and the trees a proposer
/// is allowed to mutate.
#[derive(Debug, Clone, Default, Serialize)]
pub struct Harness {
    pub entrypoint: Option<String>,
    pub mutable_trees: Vec<String>,
}

/// One board entry, summarised for display.
#[derive(Debug, Clone, Default, Serialize)]
pub struct BoardEntry {
    pub id: Option<String>,
    pub kind: Option<String>,
    pub input_preview: Option<String>,
    pub expectation_kind: Option<String>,
    pub budget_s: Option<f64>,
    pub weight: Option<f64>,
    pub tags: Vec<String>,
}

/// One mutation surface span exposed to the proposer.
#[derive(Debug, Clone, Default, Serialize)]
pub struct Mutation {
    pub id: Option<String>,
    pub kind: Option<String>,
    pub file: Option<String>,
    /// `"12-34"`, or just `"12"` when start == end, or `None`.
    pub lines: Option<String>,
    pub preview: Option<String>,
}

/// Truncate `s` to at most `PREVIEW_CHARS` characters (char-aware, so
/// multi-byte input never panics), appending an ellipsis if cut.
fn preview(s: &str) -> String {
    let s = s.trim();
    if s.chars().count() <= PREVIEW_CHARS {
        return s.to_string();
    }
    let truncated: String = s.chars().take(PREVIEW_CHARS).collect();
    format!("{truncated}...")
}

/// Best-effort JSON read; returns `None` (with a warning) on any error.
fn read_json_value(path: &std::path::Path) -> Option<serde_json::Value> {
    let bytes = match std::fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return None,
        Err(e) => {
            warn!(?path, error=%e, "failed to read epoch file");
            return None;
        }
    };
    if bytes.is_empty() {
        return None;
    }
    match serde_json::from_slice(&bytes) {
        Ok(v) => Some(v),
        Err(e) => {
            warn!(?path, error=%e, "epoch file failed to parse; ignoring");
            None
        }
    }
}

/// Pull a string field from a JSON object, ignoring non-strings.
fn str_field(obj: &serde_json::Value, key: &str) -> Option<String> {
    obj.get(key).and_then(|v| v.as_str()).map(str::to_string)
}

/// Pull a numeric field as `f64` (accepts integer or float JSON numbers).
fn num_field(obj: &serde_json::Value, key: &str) -> Option<f64> {
    obj.get(key).and_then(serde_json::Value::as_f64)
}

/// Derive a single-line input preview for a board entry. For single-turn
/// kinds this is the `input` string; for scripted / multi-turn kinds it
/// falls back to the first scripted turn or a persona goal.
fn board_input_preview(entry: &serde_json::Value) -> Option<String> {
    if let Some(input) = entry.get("input").and_then(|v| v.as_str()) {
        return Some(preview(input));
    }
    // Multi-turn: first scripted turn.
    if let Some(turns) = entry.get("turns").and_then(|v| v.as_array()) {
        for turn in turns {
            let text = turn
                .as_str()
                .map(str::to_string)
                .or_else(|| str_field(turn, "input"))
                .or_else(|| str_field(turn, "text"))
                .or_else(|| str_field(turn, "content"));
            if let Some(t) = text {
                return Some(preview(&t));
            }
        }
    }
    // Persona-driven: the persona's goal.
    if let Some(goal) = entry
        .get("persona")
        .and_then(|p| p.get("goal"))
        .and_then(|v| v.as_str())
    {
        return Some(preview(goal));
    }
    if let Some(goal) = entry.get("goal").and_then(|v| v.as_str()) {
        return Some(preview(goal));
    }
    None
}

/// Parse `epochs/{id}/board.jsonl` into display-ready entries. Each line
/// is an independent JSON object; a malformed line is skipped rather
/// than failing the whole board.
fn parse_board(path: &std::path::Path) -> Option<Vec<BoardEntry>> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return None,
        Err(e) => {
            warn!(?path, error=%e, "failed to read board.jsonl");
            return None;
        }
    };
    let mut entries = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let obj: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(e) => {
                warn!(?path, error=%e, "skipping malformed board line");
                continue;
            }
        };
        let expectation_kind = obj
            .get("expectation")
            .and_then(|e| e.get("kind"))
            .and_then(|v| v.as_str())
            .map(str::to_string);
        // `wall_clock_budget_seconds` is canonical; `budget_s` is an alias.
        let budget_s =
            num_field(&obj, "wall_clock_budget_seconds").or_else(|| num_field(&obj, "budget_s"));
        let tags = obj
            .get("tags")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|t| t.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        entries.push(BoardEntry {
            id: str_field(&obj, "id"),
            kind: str_field(&obj, "kind"),
            input_preview: board_input_preview(&obj),
            expectation_kind,
            budget_s,
            weight: num_field(&obj, "weight"),
            tags,
        });
    }
    Some(entries)
}

/// Parse `epochs/{id}/mutations.json` (a JSON array of mutation spans).
/// Absent file -> `None`; the caller maps that to an empty list.
fn parse_mutations(path: &std::path::Path) -> Option<Vec<Mutation>> {
    let value = read_json_value(path)?;
    let arr = value.as_array()?;
    let mut out = Vec::with_capacity(arr.len());
    for m in arr {
        let line_start = num_field(m, "line_start");
        let line_end = num_field(m, "line_end");
        let lines = match (line_start, line_end) {
            (Some(s), Some(e)) if s == e => Some(format!("{}", s as i64)),
            (Some(s), Some(e)) => Some(format!("{}-{}", s as i64, e as i64)),
            (Some(s), None) => Some(format!("{}", s as i64)),
            (None, Some(e)) => Some(format!("{}", e as i64)),
            (None, None) => None,
        };
        let preview_text = m.get("content").and_then(|v| v.as_str()).map(preview);
        out.push(Mutation {
            id: str_field(m, "id"),
            kind: str_field(m, "kind"),
            file: str_field(m, "file"),
            lines,
            preview: preview_text,
        });
    }
    Some(out)
}

/// Read the registered harness from the workspace `.zicato/config.json`.
///
/// Supports both the nested `adapter.{entrypoint,mutable_trees}` shape
/// and the flat top-level `adk_entrypoint` / `mutable_trees` shape.
fn read_harness(paths: &WorkspacePaths) -> Option<Harness> {
    let cfg = read_json_value(&paths.workspace.join("config.json"))?;
    let adapter = cfg.get("adapter");
    let entrypoint = adapter
        .and_then(|a| str_field(a, "entrypoint"))
        .or_else(|| str_field(&cfg, "adk_entrypoint"))
        .or_else(|| str_field(&cfg, "entrypoint"));
    let trees_value = adapter
        .and_then(|a| a.get("mutable_trees"))
        .or_else(|| cfg.get("mutable_trees"));
    let mutable_trees = trees_value
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|t| t.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_default();
    Some(Harness {
        entrypoint,
        mutable_trees,
    })
}

/// Assemble the full epoch view. Never panics; missing files degrade to
/// empty/`null` for their component.
pub fn build_epoch_view(paths: &WorkspacePaths) -> EpochView {
    let epoch_id = match crate::reader::read_current_epoch(paths) {
        Some(id) => id,
        // No current epoch at all.
        None => return EpochView::default(),
    };

    let epoch_dir = paths.epochs.join(&epoch_id);

    // Epoch-level config.json -> contract_hash / created_at / closed.
    let (contract_hash, created_at, closed) = match read_json_value(&epoch_dir.join("config.json"))
    {
        Some(cfg) => (
            str_field(&cfg, "contract_hash"),
            str_field(&cfg, "created_at"),
            cfg.get("closed").and_then(|v| v.as_bool()),
        ),
        None => (None, None, None),
    };

    let board = parse_board(&epoch_dir.join("board.jsonl"));

    // Proposer brief: `brief.md` post-rename, with the legacy
    // `rubric.md` read as a fallback so pre-rename epochs still
    // display. A missing file (either name) degrades to an empty
    // string; any other read error does too, with a warning.
    let brief = match std::fs::read_to_string(epoch_dir.join("brief.md")) {
        Ok(t) => Some(t),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            match std::fs::read_to_string(epoch_dir.join("rubric.md")) {
                Ok(t) => Some(t),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => Some(String::new()),
                Err(e) => {
                    warn!(error=%e, "failed to read legacy rubric.md");
                    Some(String::new())
                }
            }
        }
        Err(e) => {
            warn!(error=%e, "failed to read brief.md");
            Some(String::new())
        }
    };

    let scoring = read_json_value(&epoch_dir.join("scoring.json"));

    // mutations.json is optional; absent -> empty list (never `null`).
    let mutations = parse_mutations(&epoch_dir.join("mutations.json")).or(Some(Vec::new()));

    EpochView {
        epoch_id: Some(epoch_id),
        contract_hash,
        created_at,
        closed,
        harness: read_harness(paths),
        board,
        brief,
        scoring,
        mutations,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn ws() -> (TempDir, WorkspacePaths) {
        let tmp = TempDir::new().unwrap();
        let p = WorkspacePaths::new(tmp.path().to_path_buf());
        std::fs::create_dir_all(&p.epochs).unwrap();
        (tmp, p)
    }

    #[test]
    fn preview_truncates_long_text() {
        let long = "x".repeat(300);
        let out = preview(&long);
        assert!(out.ends_with("..."));
        assert_eq!(out.chars().count(), PREVIEW_CHARS + 3);
    }

    #[test]
    fn preview_keeps_short_text() {
        assert_eq!(preview("  hello  "), "hello");
    }

    #[test]
    fn preview_is_char_safe() {
        // Multi-byte chars must not panic on a byte boundary.
        let long = "é".repeat(300);
        let out = preview(&long);
        assert!(out.ends_with("..."));
    }

    #[test]
    fn no_current_epoch_yields_null_id() {
        let (_t, p) = ws();
        let view = build_epoch_view(&p);
        assert!(view.epoch_id.is_none());
        let json = serde_json::to_value(&view).unwrap();
        assert_eq!(json["epoch_id"], serde_json::Value::Null);
    }

    #[test]
    fn board_alias_and_truncation() {
        let (_t, p) = ws();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        let dir = p.epochs.join("e1");
        std::fs::create_dir_all(&dir).unwrap();
        let long_input = "w".repeat(300);
        let board = format!(
            "{}\n{}\n",
            serde_json::json!({
                "id": "a",
                "kind": "single_turn",
                "input": long_input,
                "wall_clock_budget_seconds": 900,
                "weight": 1.0,
                "tags": ["t1"],
                "expectation": {"kind": "predicate"},
            }),
            serde_json::json!({
                "id": "b",
                "kind": "single_turn",
                "input": "short",
                "budget_s": 42,
            }),
        );
        std::fs::write(dir.join("board.jsonl"), board).unwrap();
        let view = build_epoch_view(&p);
        let entries = view.board.unwrap();
        assert_eq!(entries.len(), 2);
        assert!(entries[0].input_preview.as_ref().unwrap().ends_with("..."));
        assert_eq!(entries[0].expectation_kind.as_deref(), Some("predicate"));
        assert_eq!(entries[0].budget_s, Some(900.0));
        // `budget_s` alias is honoured.
        assert_eq!(entries[1].budget_s, Some(42.0));
        assert!(entries[1].expectation_kind.is_none());
    }

    #[test]
    fn missing_mutations_is_empty_list() {
        let (_t, p) = ws();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        std::fs::create_dir_all(p.epochs.join("e1")).unwrap();
        let view = build_epoch_view(&p);
        assert!(view.mutations.unwrap().is_empty());
    }

    #[test]
    fn missing_brief_is_empty_string() {
        let (_t, p) = ws();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        std::fs::create_dir_all(p.epochs.join("e1")).unwrap();
        let view = build_epoch_view(&p);
        assert_eq!(view.brief.as_deref(), Some(""));
    }

    #[test]
    fn brief_is_read_from_brief_md() {
        let (_t, p) = ws();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        let dir = p.epochs.join("e1");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("brief.md"), "# proposer brief").unwrap();
        let view = build_epoch_view(&p);
        assert_eq!(view.brief.as_deref(), Some("# proposer brief"));
    }

    #[test]
    fn brief_falls_back_to_legacy_rubric_md() {
        let (_t, p) = ws();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        let dir = p.epochs.join("e1");
        std::fs::create_dir_all(&dir).unwrap();
        // Pre-rename epoch: only the legacy `rubric.md` exists.
        std::fs::write(dir.join("rubric.md"), "# legacy brief").unwrap();
        let view = build_epoch_view(&p);
        assert_eq!(view.brief.as_deref(), Some("# legacy brief"));
    }

    #[test]
    fn brief_md_wins_over_legacy_rubric_md() {
        let (_t, p) = ws();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        let dir = p.epochs.join("e1");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("brief.md"), "current").unwrap();
        std::fs::write(dir.join("rubric.md"), "legacy").unwrap();
        let view = build_epoch_view(&p);
        assert_eq!(view.brief.as_deref(), Some("current"));
    }

    #[test]
    fn mutations_line_range_formatting() {
        let (_t, p) = ws();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        let dir = p.epochs.join("e1");
        std::fs::create_dir_all(&dir).unwrap();
        let muts = serde_json::json!([
            {"id": "m1", "kind": "span", "file": "agent/a.py",
             "line_start": 12, "line_end": 34, "content": "hello"},
            {"id": "m2", "kind": "span", "file": "agent/b.py",
             "line_start": 7, "line_end": 7, "content": "x"},
        ]);
        std::fs::write(dir.join("mutations.json"), muts.to_string()).unwrap();
        let view = build_epoch_view(&p);
        let m = view.mutations.unwrap();
        assert_eq!(m[0].lines.as_deref(), Some("12-34"));
        assert_eq!(m[1].lines.as_deref(), Some("7"));
        assert_eq!(m[0].preview.as_deref(), Some("hello"));
    }

    #[test]
    fn harness_flat_shape() {
        let (_t, p) = ws();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        std::fs::create_dir_all(p.epochs.join("e1")).unwrap();
        let cfg = serde_json::json!({
            "adk_entrypoint": "mod:agent",
            "mutable_trees": ["/abs/agent"],
        });
        std::fs::write(p.workspace.join("config.json"), cfg.to_string()).unwrap();
        let view = build_epoch_view(&p);
        let h = view.harness.unwrap();
        assert_eq!(h.entrypoint.as_deref(), Some("mod:agent"));
        assert_eq!(h.mutable_trees, vec!["/abs/agent".to_string()]);
    }

    #[test]
    fn harness_nested_adapter_shape() {
        let (_t, p) = ws();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        std::fs::create_dir_all(p.epochs.join("e1")).unwrap();
        let cfg = serde_json::json!({
            "adapter": {"entrypoint": "mod:root", "mutable_trees": ["/x"]},
        });
        std::fs::write(p.workspace.join("config.json"), cfg.to_string()).unwrap();
        let view = build_epoch_view(&p);
        let h = view.harness.unwrap();
        assert_eq!(h.entrypoint.as_deref(), Some("mod:root"));
        assert_eq!(h.mutable_trees, vec!["/x".to_string()]);
    }

    #[test]
    fn epoch_config_fields_extracted() {
        let (_t, p) = ws();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        let dir = p.epochs.join("e1");
        std::fs::create_dir_all(&dir).unwrap();
        let cfg = serde_json::json!({
            "contract_hash": "abc123",
            "created_at": "2026-05-15T00:00:00Z",
            "closed": false,
        });
        std::fs::write(dir.join("config.json"), cfg.to_string()).unwrap();
        let view = build_epoch_view(&p);
        assert_eq!(view.contract_hash.as_deref(), Some("abc123"));
        assert_eq!(view.created_at.as_deref(), Some("2026-05-15T00:00:00Z"));
        assert_eq!(view.closed, Some(false));
    }

    #[test]
    fn multi_turn_input_preview_from_turns() {
        let (_t, p) = ws();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        let dir = p.epochs.join("e1");
        std::fs::create_dir_all(&dir).unwrap();
        let board = serde_json::json!({
            "id": "mt",
            "kind": "multi_turn",
            "turns": [{"input": "first turn"}, {"input": "second turn"}],
        });
        std::fs::write(dir.join("board.jsonl"), board.to_string()).unwrap();
        let view = build_epoch_view(&p);
        assert_eq!(
            view.board.unwrap()[0].input_preview.as_deref(),
            Some("first turn")
        );
    }
}
