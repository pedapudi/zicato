//! The served ELIM MODEL fold — the Rust twin of
//! `zicato.query.tournament_view.derive_elim_states`.
//!
//! The dashboard's bracket figures render this model verbatim (DQ1: the
//! server computes, the client renders). The Python service attaches it
//! to the settled structure record and the live active-tournament
//! payload; the supervisor mirrors the LIVE half here so both dashboards
//! serve the SAME `gen_states` (the heartbeat-ts lesson: a served-model
//! change lands in both servers or the two dashboards skew).
//!
//! The two folds are pinned together by the shared fixture
//! `tests/data/elim_states_fixture.json` — asserted byte-for-byte by
//! `tests/test_tournament_view_elim_states.py` (Python) and this
//! module's test (Rust). Any behavioural divergence is a bug in ONE of
//! the twins, never grounds to re-derive in the client.

use serde_json::{Map, Value};

/// `"single_elim"` / `"double_elim"` — the structures the fold applies to.
fn is_elim_structure(structure: Option<&str>) -> bool {
    matches!(structure, Some("single_elim") | Some("double_elim"))
}

/// The temporal sort key: `round_index` (legacy) / `stage_index`, else
/// the round's original position (stable).
fn round_sort_key(round: &Map<String, Value>, position: usize) -> (f64, usize) {
    for key in ["round_index", "stage_index"] {
        if let Some(v) = round.get(key).and_then(Value::as_f64) {
            return (v, position);
        }
    }
    (position as f64, position)
}

/// A match's competitors: non-empty strings, `"tbd"` excluded. Numeric
/// ids are stringified exactly as the Python fold does.
fn match_competitors(m: &Map<String, Value>) -> Vec<String> {
    let Some(comps) = m.get("competitors").and_then(Value::as_array) else {
        return Vec::new();
    };
    comps
        .iter()
        .filter_map(|c| match c {
            Value::String(s) if !s.is_empty() && s != "tbd" => Some(s.clone()),
            Value::Number(n) => Some(n.to_string()),
            _ => None,
        })
        .collect()
}

/// A truthy JSON value, Python-semantics (`""`, `0`, `false`, `null` are
/// falsy) — the winner/bye/decision/pending reads all use it.
fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn match_winner(m: &Map<String, Value>) -> Option<String> {
    let w = m.get("winner")?;
    if !truthy(Some(w)) {
        return None;
    }
    match w {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    }
}

fn match_pending(m: &Map<String, Value>, winner: Option<&str>) -> bool {
    if truthy(m.get("pending")) {
        return true;
    }
    winner.is_none() && !truthy(m.get("bye")) && !truthy(m.get("decision"))
}

/// One competitor's accumulated state while folding the sorted columns.
#[derive(Default)]
struct GenAcc {
    played: Vec<usize>,
    advanced: Vec<usize>,
    lost: Vec<usize>,
    side_of: Vec<(usize, String)>,
    lb_entry: Option<usize>,
    projected: Option<Value>,
}

/// The fold: raw `rounds[]` → `{"rounds": [...], "gen_states": [...]}`.
///
/// Semantics are the Python fold's, line for line: rounds PRE-SORTED by
/// round index (temporal WB → LB → GF), per-round `bracket_side`,
/// per-column DEDUPE (key = `bracket_slot` + sorted competitors, keep the
/// most-decided), per-match `loser`, and the per-generation states with
/// the elimination-vs-drop rule (a loss with no later appearance is an
/// elimination there; with one, a winners→losers drop). Malformed input
/// degrades to empty lists — never panics.
pub fn derive_elim_states(rounds: &Value) -> Value {
    let raw: Vec<&Map<String, Value>> = rounds
        .as_array()
        .map(|a| a.iter().filter_map(Value::as_object).collect())
        .unwrap_or_default();
    let mut ordered: Vec<usize> = (0..raw.len()).collect();
    ordered.sort_by(|&a, &b| {
        let (ka, pa) = round_sort_key(raw[a], a);
        let (kb, pb) = round_sort_key(raw[b], b);
        ka.partial_cmp(&kb)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(pa.cmp(&pb))
    });

    let mut accs: Vec<(String, GenAcc)> = Vec::new();
    fn ensure<'a>(accs: &'a mut Vec<(String, GenAcc)>, gid: &str) -> &'a mut GenAcc {
        if let Some(pos) = accs.iter().position(|(g, _)| g == gid) {
            return &mut accs[pos].1;
        }
        accs.push((gid.to_string(), GenAcc::default()));
        &mut accs.last_mut().unwrap().1
    }

    let mut out_rounds: Vec<Value> = Vec::new();
    for (ci, &ri) in ordered.iter().enumerate() {
        let r = raw[ri];
        let matches_in: Vec<&Map<String, Value>> = r
            .get("matches")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_object).collect())
            .unwrap_or_default();

        // DEDUPE (ex-client): key on slot + the sorted competitor set;
        // keep the MOST-DECIDED duplicate, preserving first-seen order.
        let mut key_order: Vec<String> = Vec::new();
        let mut by_key: Vec<(String, &Map<String, Value>)> = Vec::new();
        for m in matches_in {
            let mut comps = match_competitors(m);
            comps.sort();
            let slot = m
                .get("bracket_slot")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let key = format!("{}|{}", slot, comps.join("/"));
            match by_key.iter_mut().find(|(k, _)| *k == key) {
                None => {
                    key_order.push(key.clone());
                    by_key.push((key, m));
                }
                Some((_, prev)) => {
                    let prev_winner = match_winner(prev);
                    let winner = match_winner(m);
                    if match_pending(prev, prev_winner.as_deref())
                        && !match_pending(m, winner.as_deref())
                    {
                        *prev = m;
                    }
                }
            }
        }

        let mut any_lb = false;
        let mut out_matches: Vec<Value> = Vec::new();
        for key in &key_order {
            let m = by_key.iter().find(|(k, _)| k == key).unwrap().1;
            let comps = match_competitors(m);
            let winner = match_winner(m);
            let pending = match_pending(m, winner.as_deref());
            let is_lb = m
                .get("bracket_slot")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .starts_with("LB");
            if is_lb {
                any_lb = true;
            }
            let bye = truthy(m.get("bye"));
            let loser: Option<String> = match (&winner, bye, comps.len() >= 2) {
                (Some(w), false, true) => comps.iter().find(|c| *c != w).cloned(),
                _ => None,
            };
            let proj_map = m.get("projected").and_then(Value::as_object);
            for c in &comps {
                let acc = ensure(&mut accs, c);
                if !acc.played.contains(&ci) {
                    acc.played.push(ci);
                }
                // last write wins for the column's side, like the Python dict.
                if let Some(pos) = acc.side_of.iter().position(|(k, _)| *k == ci) {
                    acc.side_of[pos].1 = if is_lb { "LB" } else { "WB" }.to_string();
                } else {
                    acc.side_of
                        .push((ci, if is_lb { "LB" } else { "WB" }.to_string()));
                }
                if is_lb && acc.lb_entry.is_none() {
                    acc.lb_entry = Some(ci);
                }
                if pending {
                    if let Some(pm) = proj_map {
                        if let Some(p) = pm.get(c.as_str()) {
                            let has_scalar = p
                                .as_object()
                                .and_then(|o| o.get("scalar"))
                                .and_then(Value::as_f64)
                                .is_some();
                            if has_scalar {
                                acc.projected = Some(p.clone());
                            }
                        }
                    }
                    continue;
                }
                if bye || winner.as_deref() == Some(c.as_str()) {
                    if !acc.advanced.contains(&ci) {
                        acc.advanced.push(ci);
                    }
                } else if winner.is_some() && !acc.lost.contains(&ci) {
                    acc.lost.push(ci);
                }
            }

            let mut out_m = m.clone();
            out_m.insert(
                "loser".to_string(),
                loser.map(Value::String).unwrap_or(Value::Null),
            );
            out_matches.push(Value::Object(out_m));
        }

        let mut out_r = r.clone();
        out_r.insert("matches".to_string(), Value::Array(out_matches));
        out_r.insert(
            "bracket_side".to_string(),
            Value::String(if any_lb { "LB" } else { "WB" }.to_string()),
        );
        out_rounds.push(Value::Object(out_r));
    }

    // ELIMINATION vs DROP: eliminated at the first loss with no LATER
    // appearance; an earlier loss followed by a later column is a drop.
    let mut gen_states: Vec<Value> = Vec::new();
    for (gid, acc) in &accs {
        let mut played = acc.played.clone();
        played.sort_unstable();
        let mut advanced = acc.advanced.clone();
        advanced.sort_unstable();
        let mut lost = acc.lost.clone();
        lost.sort_unstable();
        let last_played = played.last().copied();
        let eliminated_at = lost
            .iter()
            .find(|&&ci| last_played.is_none_or(|lp| ci >= lp))
            .copied();
        let mut side_by_round = Map::new();
        let mut side_sorted = acc.side_of.clone();
        side_sorted.sort_by_key(|(k, _)| *k);
        for (k, side) in side_sorted {
            side_by_round.insert(k.to_string(), Value::String(side));
        }
        let mut gs = Map::new();
        gs.insert("generation_id".into(), Value::String(gid.clone()));
        gs.insert(
            "played_rounds".into(),
            Value::Array(played.into_iter().map(|v| v.into()).collect()),
        );
        gs.insert(
            "advanced_rounds".into(),
            Value::Array(advanced.into_iter().map(|v| v.into()).collect()),
        );
        gs.insert(
            "lost_rounds".into(),
            Value::Array(lost.into_iter().map(|v| v.into()).collect()),
        );
        gs.insert(
            "eliminated_at_round".into(),
            eliminated_at.map(|v| v.into()).unwrap_or(Value::Null),
        );
        gs.insert("side_by_round".into(), Value::Object(side_by_round));
        gs.insert(
            "lb_entry_round".into(),
            acc.lb_entry.map(|v| v.into()).unwrap_or(Value::Null),
        );
        gs.insert(
            "projected".into(),
            acc.projected.clone().unwrap_or(Value::Null),
        );
        gen_states.push(Value::Object(gs));
    }

    let mut out = Map::new();
    out.insert("rounds".into(), Value::Array(out_rounds));
    out.insert("gen_states".into(), Value::Array(gen_states));
    Value::Object(out)
}

/// Enrich a live [`crate::state::ActiveTournament`] with the served elim
/// model — the Rust half of the Python `attach_elim_states` wiring: an
/// elim payload's `rounds` are replaced by the canonicalized copy and
/// `gen_states` attached; any other structure passes through untouched.
pub fn enrich_active_tournament(at: &mut crate::state::ActiveTournament) {
    if !is_elim_structure(at.structure.as_deref()) {
        return;
    }
    let Some(rounds) = at.rounds.as_ref().filter(|r| r.is_array()) else {
        return;
    };
    let derived = derive_elim_states(rounds);
    at.rounds = derived.get("rounds").cloned();
    at.gen_states = derived.get("gen_states").cloned();
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// The SHARED Python↔Rust fixture — byte-for-byte agreement with the
    /// Python fold (ch08: when a Python payload change requires Rust
    /// parity, the two land pinned together).
    #[test]
    fn shared_fixture_matches_the_python_fold() {
        let fixture: Value = serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/data/elim_states_fixture.json"
        )))
        .unwrap();
        let got = derive_elim_states(&fixture["input_rounds"]);
        assert_eq!(got, fixture["expected"]);
    }

    #[test]
    fn a_loss_with_a_later_appearance_is_a_drop_not_an_elimination() {
        let rounds = json!([
            {"round_index": 0, "matches": [
                {"bracket_slot": "WB-R0-0", "competitors": ["v1", "v2"], "winner": "v1"}]},
            {"round_index": 1, "matches": [
                {"bracket_slot": "LB-R1-0", "competitors": ["v2", "v3"], "winner": "v3"}]},
        ]);
        let out = derive_elim_states(&rounds);
        let v2 = &out["gen_states"][1];
        assert_eq!(v2["generation_id"], "v2");
        assert_eq!(v2["eliminated_at_round"], 1);
        assert_eq!(v2["lb_entry_round"], 1);
    }

    #[test]
    fn dedupe_keeps_the_most_decided_duplicate() {
        let rounds = json!([{"round_index": 0, "matches": [
            {"bracket_slot": "LB-R1-0", "competitors": ["v2", "v4"], "winner": null, "pending": true},
            {"bracket_slot": "LB-R1-0", "competitors": ["v2", "v4"], "winner": "v2"},
        ]}]);
        let out = derive_elim_states(&rounds);
        let matches = out["rounds"][0]["matches"].as_array().unwrap();
        assert_eq!(matches.len(), 1);
        assert_eq!(matches[0]["winner"], "v2");
        assert_eq!(matches[0]["loser"], "v4");
    }

    #[test]
    fn malformed_input_degrades_to_empty_never_panics() {
        for v in [json!(null), json!("junk"), json!([1, "x", null])] {
            let out = derive_elim_states(&v);
            assert_eq!(out["rounds"], json!([]));
            assert_eq!(out["gen_states"], json!([]));
        }
    }

    #[test]
    fn enrich_skips_non_elim_structures() {
        let mut at = crate::state::ActiveTournament {
            structure: Some("swiss".to_string()),
            rounds: Some(json!([{"round_index": 0, "matches": []}])),
            ..Default::default()
        };
        enrich_active_tournament(&mut at);
        assert!(at.gen_states.is_none());
    }

    #[test]
    fn enrich_attaches_gen_states_for_elim() {
        let mut at = crate::state::ActiveTournament {
            structure: Some("single_elim".to_string()),
            rounds: Some(json!([{"round_index": 0, "matches": [
                {"bracket_slot": "WB-R0-0", "competitors": ["v1", "v2"], "winner": "v1"}]}])),
            ..Default::default()
        };
        enrich_active_tournament(&mut at);
        let gs = at.gen_states.as_ref().unwrap().as_array().unwrap();
        assert_eq!(gs.len(), 2);
        assert_eq!(at.rounds.as_ref().unwrap()[0]["bracket_side"], "WB");
        assert_eq!(at.rounds.as_ref().unwrap()[0]["matches"][0]["loser"], "v2");
    }
}
