//! Assemble the tournament bracket and per-matchup detail for the
//! dashboard from the SQLite analytical index, plus the latest
//! loop-health report.
//!
//! Everything here is best-effort: a missing `index.db`, a missing
//! current-epoch marker, or missing rows all degrade to an empty/`null`
//! payload. No path in this module returns an error to its HTTP caller.

use crate::index_db::{self, IndexError};
use crate::reader::WorkspacePaths;
use serde::Serialize;
use tracing::warn;

/// The tournament bracket for the current epoch — the champion lineage
/// (the promoted chain) plus a summary of every matchup. Served by
/// `GET /api/tournaments`.
#[derive(Debug, Clone, Default, Serialize)]
pub struct BracketView {
    /// `null` when there is no current-epoch marker.
    pub epoch_id: Option<String>,
    /// The promoted chain, root-first (e.g. `["v0","v2"]`).
    pub champion_lineage: Vec<String>,
    /// One entry per tournament that ran this epoch.
    pub matchups: Vec<MatchupSummary>,
    /// Present only when `index.db` is absent.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// One row of the bracket: a champion-vs-challenger matchup.
#[derive(Debug, Clone, Default, Serialize)]
pub struct MatchupSummary {
    pub champion: Option<String>,
    pub challenger: Option<String>,
    pub decision: Option<String>,
    pub delta_scalar: Option<f64>,
    pub rejection_reason: Option<String>,
    pub hypothesis_core_idea: Option<String>,
    pub ran_at: Option<String>,
}

/// Full per-matchup detail. Served by `GET /api/tournaments/:generation_id`
/// where `generation_id` is the *challenger* (child) generation.
#[derive(Debug, Clone, Default, Serialize)]
pub struct MatchupDetail {
    /// `null` when there is no current-epoch marker.
    pub epoch_id: Option<String>,
    /// The challenger generation this detail describes.
    pub generation_id: Option<String>,
    pub champion: Option<String>,
    pub decision: Option<String>,
    pub rejection_reason: Option<String>,
    pub ran_at: Option<String>,
    /// Scalar score of champion / challenger and the delta between them.
    pub parent_scalar: Option<f64>,
    pub child_scalar: Option<f64>,
    pub delta_scalar: Option<f64>,
    /// Additional deltas pulled from the `experiments` row.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub drift_loss_delta: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pass_rate_delta: Option<f64>,
    /// The proposer's hypothesis.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hypothesis: Option<Hypothesis>,
    /// The patches that made up the challenger.
    pub patches: Vec<PatchView>,
    /// Per-board-entry A/B grid (parent vs child drift loss + verdict).
    pub ab_grid: Vec<AbCell>,
    /// Present only when `index.db` is absent.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
}

/// The proposer hypothesis behind a challenger.
#[derive(Debug, Clone, Default, Serialize)]
pub struct Hypothesis {
    pub core_idea: Option<String>,
    pub why: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw: Option<serde_json::Value>,
}

/// One patch within a challenger generation.
#[derive(Debug, Clone, Default, Serialize)]
pub struct PatchView {
    pub patch_id: Option<String>,
    pub mutation_id: Option<String>,
    pub op: Option<String>,
    pub rationale: Option<String>,
}

/// One board-entry row of the A/B grid: the same entry run against the
/// champion and the challenger, with a verdict.
#[derive(Debug, Clone, Default, Serialize)]
pub struct AbCell {
    pub entry_id: Option<String>,
    pub parent_drift_loss: Option<f64>,
    pub child_drift_loss: Option<f64>,
    pub parent_pass_fail: Option<String>,
    pub child_pass_fail: Option<String>,
    /// `"improved"`, `"regressed"`, or `"flat"`.
    pub verdict: &'static str,
}

/// The loop-health report shape (`LoopHealth`), served verbatim by
/// `GET /api/health-report`.
#[derive(Debug, Clone, Default, Serialize)]
pub struct LoopHealth {
    pub epoch_id: Option<String>,
    pub findings: Vec<serde_json::Value>,
    pub healthy: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub checked_at: Option<String>,
}

/// Lower drift loss is better; classify a parent->child move.
fn verdict(parent: Option<f64>, child: Option<f64>) -> &'static str {
    match (parent, child) {
        (Some(p), Some(c)) if c < p => "improved",
        (Some(p), Some(c)) if c > p => "regressed",
        (Some(_), Some(_)) => "flat",
        _ => "flat",
    }
}

/// Build the champion lineage: the chain of promoted generations,
/// root-first. Starts from the promoted root (a promoted generation with
/// no promoted parent) and follows `parent_generation_id` links forward.
fn champion_lineage(generations: &[index_db::GenerationRow]) -> Vec<String> {
    use std::collections::{HashMap, HashSet};
    let promoted: HashSet<&str> = generations
        .iter()
        .filter(|g| g.promoted)
        .filter_map(|g| g.generation_id.as_deref())
        .collect();
    if promoted.is_empty() {
        return Vec::new();
    }
    // child -> parent, restricted to promoted generations.
    let parent: HashMap<&str, Option<&str>> = generations
        .iter()
        .filter(|g| g.promoted)
        .filter_map(|g| {
            g.generation_id
                .as_deref()
                .map(|id| (id, g.parent_generation_id.as_deref()))
        })
        .collect();
    // The root is a promoted generation whose parent is not itself
    // promoted (or is absent). If there are several, pick the smallest
    // id for a stable result.
    let mut roots: Vec<&str> = promoted
        .iter()
        .copied()
        .filter(|id| {
            parent
                .get(id)
                .copied()
                .flatten()
                .map(|p| !promoted.contains(p))
                .unwrap_or(true)
        })
        .collect();
    roots.sort_unstable();
    let root = match roots.first() {
        Some(r) => *r,
        None => return Vec::new(),
    };
    // child lookup: parent -> child within the promoted set.
    let child_of: HashMap<&str, &str> = generations
        .iter()
        .filter(|g| g.promoted)
        .filter_map(|g| {
            match (
                g.parent_generation_id.as_deref(),
                g.generation_id.as_deref(),
            ) {
                (Some(p), Some(c)) if promoted.contains(p) => Some((p, c)),
                _ => None,
            }
        })
        .collect();
    let mut chain = vec![root.to_string()];
    let mut seen: HashSet<&str> = HashSet::new();
    seen.insert(root);
    let mut cur = root;
    while let Some(&next) = child_of.get(cur) {
        if !seen.insert(next) {
            // Cycle guard: malformed parent links must not loop forever.
            break;
        }
        chain.push(next.to_string());
        cur = next;
    }
    chain
}

/// Assemble `GET /api/tournaments` for the current epoch.
pub fn build_bracket(paths: &WorkspacePaths) -> BracketView {
    let epoch_id = crate::reader::read_current_epoch(paths);
    let conn = match index_db::open(&paths.index_db()) {
        Ok(c) => c,
        Err(IndexError::Absent) => {
            return BracketView {
                epoch_id,
                champion_lineage: Vec::new(),
                matchups: Vec::new(),
                note: Some("index not built; run zicato reindex".to_string()),
            };
        }
        Err(IndexError::Query(e)) => {
            warn!(error = %e, "index.db open failed; serving empty bracket");
            return BracketView {
                epoch_id,
                ..Default::default()
            };
        }
    };

    // With no current epoch there is nothing to scope the query to.
    let epoch = match &epoch_id {
        Some(e) => e.clone(),
        None => {
            return BracketView {
                epoch_id,
                ..Default::default()
            }
        }
    };

    let generations = index_db::generations_for_epoch(&conn, &epoch);
    let lineage = champion_lineage(&generations);

    let matchups = index_db::tournaments_for_epoch(&conn, &epoch)
        .into_iter()
        .map(|t| MatchupSummary {
            champion: t.parent_generation_id,
            challenger: t.child_generation_id,
            decision: t.decision,
            delta_scalar: t.delta_scalar,
            rejection_reason: t.rejection_reason,
            hypothesis_core_idea: t.hypothesis_core_idea,
            ran_at: t.ran_at,
        })
        .collect();

    BracketView {
        epoch_id,
        champion_lineage: lineage,
        matchups,
        note: None,
    }
}

/// Assemble `GET /api/tournaments/:generation_id` — full matchup detail
/// for the challenger `generation_id`.
pub fn build_matchup_detail(paths: &WorkspacePaths, generation_id: &str) -> MatchupDetail {
    let epoch_id = crate::reader::read_current_epoch(paths);
    let conn = match index_db::open(&paths.index_db()) {
        Ok(c) => c,
        Err(IndexError::Absent) => {
            return MatchupDetail {
                epoch_id,
                generation_id: Some(generation_id.to_string()),
                note: Some("index not built; run zicato reindex".to_string()),
                ..Default::default()
            };
        }
        Err(IndexError::Query(e)) => {
            warn!(error = %e, "index.db open failed; serving empty matchup");
            return MatchupDetail {
                epoch_id,
                generation_id: Some(generation_id.to_string()),
                ..Default::default()
            };
        }
    };

    let tournament = index_db::tournament_for_child(&conn, generation_id);
    let experiment = index_db::experiment_for_generation(&conn, generation_id);

    // The champion this challenger was measured against.
    let champion = tournament
        .as_ref()
        .and_then(|t| t.parent_generation_id.clone());

    // A/B grid: the challenger's per-entry losses vs the champion's.
    let child_losses = index_db::loss_profiles_for_generation(&conn, generation_id);
    let parent_losses = match &champion {
        Some(c) => index_db::loss_profiles_for_generation(&conn, c),
        None => Vec::new(),
    };
    let ab_grid = build_ab_grid(&parent_losses, &child_losses);

    let hypothesis = experiment.as_ref().map(|e| Hypothesis {
        core_idea: e.hypothesis_core_idea.clone(),
        why: e.hypothesis_why.clone(),
        raw: e.hypothesis_json.clone(),
    });

    let patches = index_db::patches_for_generation(&conn, generation_id)
        .into_iter()
        .map(|p| PatchView {
            patch_id: p.patch_id,
            mutation_id: p.mutation_id,
            op: p.op,
            rationale: p.rationale,
        })
        .collect();

    // Decision / rejection prefer the tournament row, falling back to the
    // experiment row when no tournament row exists yet.
    let decision = tournament
        .as_ref()
        .and_then(|t| t.decision.clone())
        .or_else(|| {
            experiment
                .as_ref()
                .and_then(|e| e.tournament_decision.clone())
        });
    let rejection_reason = tournament
        .as_ref()
        .and_then(|t| t.rejection_reason.clone())
        .or_else(|| experiment.as_ref().and_then(|e| e.rejection_reason.clone()));

    MatchupDetail {
        epoch_id,
        generation_id: Some(generation_id.to_string()),
        champion,
        decision,
        rejection_reason,
        ran_at: tournament.as_ref().and_then(|t| t.ran_at.clone()),
        parent_scalar: tournament.as_ref().and_then(|t| t.parent_scalar),
        child_scalar: tournament.as_ref().and_then(|t| t.child_scalar),
        delta_scalar: tournament
            .as_ref()
            .and_then(|t| t.delta_scalar)
            .or_else(|| experiment.as_ref().and_then(|e| e.scalar_score_delta)),
        drift_loss_delta: experiment.as_ref().and_then(|e| e.drift_loss_delta),
        pass_rate_delta: experiment.as_ref().and_then(|e| e.pass_rate_delta),
        hypothesis,
        patches,
        ab_grid,
        note: None,
    }
}

/// Join parent and child loss profiles on `entry_id` into an A/B grid.
fn build_ab_grid(
    parent: &[index_db::LossProfileRow],
    child: &[index_db::LossProfileRow],
) -> Vec<AbCell> {
    use std::collections::BTreeMap;
    let mut by_entry: BTreeMap<String, AbCell> = BTreeMap::new();
    for p in parent {
        let key = p.entry_id.clone().unwrap_or_default();
        let cell = by_entry.entry(key.clone()).or_default();
        cell.entry_id = p.entry_id.clone();
        cell.parent_drift_loss = p.drift_loss;
        cell.parent_pass_fail = p.pass_fail.clone();
    }
    for c in child {
        let key = c.entry_id.clone().unwrap_or_default();
        let cell = by_entry.entry(key.clone()).or_default();
        cell.entry_id = c.entry_id.clone();
        cell.child_drift_loss = c.drift_loss;
        cell.child_pass_fail = c.pass_fail.clone();
    }
    by_entry
        .into_values()
        .map(|mut cell| {
            cell.verdict = verdict(cell.parent_drift_loss, cell.child_drift_loss);
            cell
        })
        .collect()
}

/// Read the latest loop-health report for the current epoch.
///
/// Reports live at `.zicato/epochs/{epoch}/health/round_*.json`; the one
/// with the highest `N` wins. When there is no current epoch or no
/// report at all the result is a healthy empty report.
pub fn build_health_report(paths: &WorkspacePaths) -> LoopHealth {
    let epoch_id = crate::reader::read_current_epoch(paths);
    let epoch = match &epoch_id {
        Some(e) => e.clone(),
        None => {
            return LoopHealth {
                epoch_id,
                findings: Vec::new(),
                healthy: true,
                checked_at: None,
            }
        }
    };

    let dir = paths.epoch_health_dir(&epoch);
    let latest = match latest_round_report(&dir) {
        Some(p) => p,
        None => {
            return LoopHealth {
                epoch_id,
                findings: Vec::new(),
                healthy: true,
                checked_at: None,
            }
        }
    };

    let bytes = match std::fs::read(&latest) {
        Ok(b) if !b.is_empty() => b,
        _ => {
            return LoopHealth {
                epoch_id,
                findings: Vec::new(),
                healthy: true,
                checked_at: None,
            }
        }
    };
    let value: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(e) => {
            warn!(?latest, error = %e, "health report failed to parse; treating as healthy");
            return LoopHealth {
                epoch_id,
                findings: Vec::new(),
                healthy: true,
                checked_at: None,
            };
        }
    };

    LoopHealth {
        // Prefer the report's own epoch_id, falling back to the marker.
        epoch_id: value
            .get("epoch_id")
            .and_then(|v| v.as_str())
            .map(str::to_string)
            .or(epoch_id),
        findings: value
            .get("findings")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default(),
        // A report with no explicit `healthy` flag is treated as healthy.
        healthy: value
            .get("healthy")
            .and_then(|v| v.as_bool())
            .unwrap_or(true),
        checked_at: value
            .get("checked_at")
            .and_then(|v| v.as_str())
            .map(str::to_string),
    }
}

/// Find `round_{N}.json` with the highest `N` in `dir`. Files that do not
/// match the pattern are ignored.
fn latest_round_report(dir: &std::path::Path) -> Option<std::path::PathBuf> {
    let entries = std::fs::read_dir(dir).ok()?;
    let mut best: Option<(u64, std::path::PathBuf)> = None;
    for entry in entries.flatten() {
        let path = entry.path();
        let name = match path.file_name().and_then(|s| s.to_str()) {
            Some(n) => n,
            None => continue,
        };
        let n = match name
            .strip_prefix("round_")
            .and_then(|rest| rest.strip_suffix(".json"))
            .and_then(|num| num.parse::<u64>().ok())
        {
            Some(n) => n,
            None => continue,
        };
        if best.as_ref().map(|(b, _)| n > *b).unwrap_or(true) {
            best = Some((n, path));
        }
    }
    best.map(|(_, p)| p)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index_db::{GenerationRow, LossProfileRow};
    use tempfile::TempDir;

    fn gen(id: &str, parent: Option<&str>, promoted: bool) -> GenerationRow {
        GenerationRow {
            epoch_id: Some("e1".into()),
            generation_id: Some(id.into()),
            parent_generation_id: parent.map(str::to_string),
            promoted,
        }
    }

    #[test]
    fn champion_lineage_follows_promoted_chain() {
        let gens = vec![
            gen("v0", None, true),
            gen("v1", Some("v0"), false),
            gen("v2", Some("v0"), true),
            gen("v3", Some("v2"), false),
            gen("v4", Some("v2"), true),
        ];
        assert_eq!(champion_lineage(&gens), vec!["v0", "v2", "v4"]);
    }

    #[test]
    fn champion_lineage_empty_when_nothing_promoted() {
        let gens = vec![gen("v0", None, false), gen("v1", Some("v0"), false)];
        assert!(champion_lineage(&gens).is_empty());
    }

    #[test]
    fn champion_lineage_tolerates_cycle() {
        // Malformed: v0 <-> v1 both promoted, parents point at each other.
        let gens = vec![gen("v0", Some("v1"), true), gen("v1", Some("v0"), true)];
        // Must terminate, not loop forever.
        let chain = champion_lineage(&gens);
        assert!(chain.len() <= 2);
    }

    #[test]
    fn verdict_classifies_drift_moves() {
        assert_eq!(verdict(Some(0.5), Some(0.2)), "improved");
        assert_eq!(verdict(Some(0.2), Some(0.5)), "regressed");
        assert_eq!(verdict(Some(0.3), Some(0.3)), "flat");
        assert_eq!(verdict(None, Some(0.3)), "flat");
    }

    #[test]
    fn ab_grid_joins_on_entry_id() {
        let parent = vec![
            LossProfileRow {
                entry_id: Some("b1".into()),
                drift_loss: Some(0.5),
                pass_fail: Some("fail".into()),
                ..Default::default()
            },
            LossProfileRow {
                entry_id: Some("b2".into()),
                drift_loss: Some(0.1),
                pass_fail: Some("pass".into()),
                ..Default::default()
            },
        ];
        let child = vec![LossProfileRow {
            entry_id: Some("b1".into()),
            drift_loss: Some(0.2),
            pass_fail: Some("pass".into()),
            ..Default::default()
        }];
        let grid = build_ab_grid(&parent, &child);
        assert_eq!(grid.len(), 2);
        assert_eq!(grid[0].entry_id.as_deref(), Some("b1"));
        assert_eq!(grid[0].verdict, "improved");
        // b2 only ran for the parent: child side is null, verdict flat.
        assert_eq!(grid[1].entry_id.as_deref(), Some("b2"));
        assert!(grid[1].child_drift_loss.is_none());
        assert_eq!(grid[1].verdict, "flat");
    }

    #[test]
    fn health_report_picks_highest_round() {
        let tmp = TempDir::new().unwrap();
        let dir = tmp.path();
        std::fs::write(
            dir.join("round_1.json"),
            r#"{"epoch_id":"e1","healthy":true}"#,
        )
        .unwrap();
        std::fs::write(
            dir.join("round_10.json"),
            r#"{"epoch_id":"e1","healthy":false,"findings":[{"code":"x"}]}"#,
        )
        .unwrap();
        std::fs::write(dir.join("round_2.json"), r#"{"healthy":true}"#).unwrap();
        std::fs::write(dir.join("not_a_report.json"), "garbage").unwrap();
        let latest = latest_round_report(dir).unwrap();
        assert!(latest.ends_with("round_10.json"));
    }

    #[test]
    fn no_health_dir_yields_healthy_empty() {
        let tmp = TempDir::new().unwrap();
        let p = WorkspacePaths::new(tmp.path().to_path_buf());
        std::fs::create_dir_all(&p.epochs).unwrap();
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        let report = build_health_report(&p);
        assert!(report.healthy);
        assert!(report.findings.is_empty());
        assert_eq!(report.epoch_id.as_deref(), Some("e1"));
    }
}
