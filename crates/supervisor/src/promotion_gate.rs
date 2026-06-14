//! Promotion gatekeeping — does the recorded decision match the recorded
//! scores?
//!
//! INTEGRITY NOTARY — record #3 (alarm-only). The orchestrator records, per
//! tournament, a `decision` (promoted / rejected) and the scalar evidence
//! (`parent_scalar`, `child_scalar`, `delta_scalar`). The supervisor
//! independently re-applies the gate's headline rule to that evidence and
//! ALARMS when a `promoted` decision is not actually supported by the scores.
//!
//! The gate's scalar rule (`zicato.tournament.gate`): the scalar is a LOSS,
//! lower is better, and a promotion needs the challenger's loss to drop by at
//! least `promote_margin`:
//!
//! ```text
//!   delta_scalar = child_scalar - parent_scalar
//!   promote  requires  delta_scalar <= -promote_margin
//!   child_scalar > parent_scalar - promote_margin  →  reject
//! ```
//!
//! DIRECTION-PRECISE alarming: a promotion is gated by ALL of the gate's rules
//! (scalar margin AND pass-rate AND namespace monotonicity), so if the SCALAR
//! rule alone is not satisfied, the promotion is definitively unsupported — a
//! hard contradiction. The reject direction is NOT alarmed: a reject can be
//! driven by the pass-rate / namespace rules even when the scalar margin
//! cleared, so a scalar-only recompute cannot prove a reject wrong. v1 raises
//! only the high-confidence "promoted-but-scores-don't-support-it" alarm.
//!
//! Read-only / alarm-only: this never blocks a promotion and never writes the
//! orchestrator's trees — it only flags. Fail-open: a row with no usable
//! scalar evidence is SKIPPED (not enough to judge), never a false alarm.

use crate::index_db::{self, IndexError, TournamentRow};
use crate::reader::WorkspacePaths;
use serde::Serialize;

/// The default `promote_margin` when `scoring.json` does not record one. Must
/// track `ScoringWeights.promote_margin`'s default in
/// `src/zicato/core/scoring_config.py` (currently `0.01`).
pub const DEFAULT_PROMOTE_MARGIN: f64 = 0.01;

/// One promotion-gatekeeping contradiction: a recorded `promoted` decision
/// that the recorded scalar evidence does not support.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Contradiction {
    pub epoch_id: String,
    pub challenger_generation_id: String,
    pub champion_generation_id: String,
    /// The decision recorded by the orchestrator (a promote variant).
    pub recorded_decision: String,
    /// `child_scalar - parent_scalar` (the loss change; negative is better).
    pub delta_scalar: f64,
    /// The promotion threshold the supervisor re-applied.
    pub promote_margin: f64,
    /// A human-readable statement of the contradiction.
    pub detail: String,
}

/// The latest promotion-gatekeeping scan, surfaced on `/statusz`.
#[derive(Debug, Clone, Default, Serialize)]
pub struct PromotionGateView {
    /// `true` once a scan has run.
    pub scanned: bool,
    /// Number of resolved promotions checked in the latest scan.
    pub promotions_checked: u64,
    /// Number skipped for want of usable scalar evidence (fail-open).
    pub skipped: u64,
    /// The contradictions found (empty when every promotion is supported).
    pub contradictions: Vec<Contradiction>,
}

/// Whether a decision string denotes a promotion. Mirrors the lenient mapping
/// `reader::decision_to_promoted` uses for the lineage view.
fn is_promote_decision(decision: &str) -> bool {
    matches!(
        decision.trim().to_ascii_lowercase().as_str(),
        "promoted" | "promote" | "accepted" | "accept" | "win" | "won"
    )
}

/// The `delta_scalar` for a tournament row: the recorded delta when present,
/// else derived from the absolute child/parent scalars. `None` when neither is
/// available (the row carries no usable scalar evidence).
fn row_delta_scalar(row: &TournamentRow) -> Option<f64> {
    if let Some(d) = row.delta_scalar {
        return Some(d);
    }
    match (row.child_scalar, row.parent_scalar) {
        (Some(c), Some(p)) => Some(c - p),
        _ => None,
    }
}

/// Re-apply the gate's scalar rule to one row and return a [`Contradiction`]
/// when the recorded decision is a promotion the scalars do not support.
///
/// A promotion is supported by the scalar rule iff
/// `delta_scalar <= -promote_margin` (the loss dropped by at least the
/// margin). `None` when the decision is not a promotion, when there is no
/// usable scalar evidence (skip — fail-open), or when the promotion IS
/// supported.
pub fn check_row(row: &TournamentRow, promote_margin: f64) -> RowVerdict {
    let decision = match &row.decision {
        Some(d) => d,
        None => return RowVerdict::NotAPromotion,
    };
    if !is_promote_decision(decision) {
        return RowVerdict::NotAPromotion;
    }
    let delta = match row_delta_scalar(row) {
        Some(d) => d,
        None => return RowVerdict::SkippedNoEvidence,
    };
    // Supported iff the loss dropped by at least the margin.
    if delta <= -promote_margin {
        return RowVerdict::Supported;
    }
    let champion = row.parent_generation_id.clone().unwrap_or_default();
    let challenger = row.child_generation_id.clone().unwrap_or_default();
    let epoch_id = row.epoch_id.clone().unwrap_or_default();
    let detail = if delta > 0.0 {
        format!(
            "recorded PROMOTE but challenger regressed: loss rose by {delta:.6} \
             (a promotion needs the loss to drop by at least {promote_margin:.6})"
        )
    } else {
        format!(
            "recorded PROMOTE but improvement was insufficient: loss fell by only \
             {:.6} (a promotion needs a drop of at least {promote_margin:.6})",
            -delta
        )
    };
    RowVerdict::Contradiction(Contradiction {
        epoch_id,
        challenger_generation_id: challenger,
        champion_generation_id: champion,
        recorded_decision: decision.clone(),
        delta_scalar: delta,
        promote_margin,
        detail,
    })
}

/// The outcome of checking one tournament row.
#[derive(Debug, Clone, PartialEq)]
pub enum RowVerdict {
    /// The row is not a promotion (a reject / unknown decision) — not checked.
    NotAPromotion,
    /// No usable scalar evidence — skipped (fail-open).
    SkippedNoEvidence,
    /// A promotion the recorded scalars support — clean.
    Supported,
    /// A promotion the recorded scalars do NOT support — alarm.
    Contradiction(Contradiction),
}

/// Read `promote_margin` from the current epoch's `scoring.json`, defaulting
/// to [`DEFAULT_PROMOTE_MARGIN`] when absent or unparseable.
pub fn promote_margin_for_epoch(paths: &WorkspacePaths) -> f64 {
    crate::epoch::build_epoch_view(paths)
        .scoring
        .and_then(|s| s.get("promote_margin").and_then(serde_json::Value::as_f64))
        .unwrap_or(DEFAULT_PROMOTE_MARGIN)
}

/// Scan every resolved tournament in the current epoch and flag any recorded
/// promotion the recorded scalar evidence does not support.
///
/// Read-only against the index. A missing/absent/stale index degrades to an
/// empty (scanned, no-contradiction) view — never an error, never a false
/// alarm. Returns the view; the caller records it and may ledger-alarm each
/// contradiction.
pub fn scan_current_epoch(paths: &WorkspacePaths) -> PromotionGateView {
    let epoch_id = match crate::reader::read_current_epoch(paths) {
        Some(e) => e,
        None => return PromotionGateView::default(),
    };
    let conn = match index_db::open(&paths.index_db()) {
        Ok(c) => c,
        // No / unreadable / stale index → nothing to check (fail-open).
        Err(IndexError::Absent)
        | Err(IndexError::Query(_))
        | Err(IndexError::StaleSchema { .. }) => {
            return PromotionGateView {
                scanned: true,
                ..Default::default()
            };
        }
    };

    let promote_margin = promote_margin_for_epoch(paths);
    let rows = index_db::tournaments_for_epoch(&conn, &epoch_id);

    let mut promotions_checked = 0u64;
    let mut skipped = 0u64;
    let mut contradictions = Vec::new();
    for row in &rows {
        match check_row(row, promote_margin) {
            RowVerdict::NotAPromotion => {}
            RowVerdict::SkippedNoEvidence => skipped += 1,
            RowVerdict::Supported => promotions_checked += 1,
            RowVerdict::Contradiction(c) => {
                promotions_checked += 1;
                contradictions.push(c);
            }
        }
    }

    PromotionGateView {
        scanned: true,
        promotions_checked,
        skipped,
        contradictions,
    }
}

/// The latest promotion-gate scan, shared with `/statusz`.
#[derive(Debug, Default)]
pub struct PromotionGateFindings {
    inner: std::sync::Mutex<PromotionGateView>,
}

impl PromotionGateFindings {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn record(&self, view: PromotionGateView) {
        let mut g = match self.inner.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        };
        *g = view;
    }

    pub fn view(&self) -> PromotionGateView {
        match self.inner.lock() {
            Ok(g) => g.clone(),
            Err(p) => p.into_inner().clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn promote_row(delta: Option<f64>) -> TournamentRow {
        TournamentRow {
            epoch_id: Some("e1".into()),
            parent_generation_id: Some("v0".into()),
            child_generation_id: Some("v1".into()),
            decision: Some("promoted".into()),
            delta_scalar: delta,
            ..Default::default()
        }
    }

    #[test]
    fn promotion_with_sufficient_drop_is_supported() {
        // delta = -0.05, margin = 0.01 → -0.05 <= -0.01 → supported.
        let row = promote_row(Some(-0.05));
        assert_eq!(check_row(&row, 0.01), RowVerdict::Supported);
    }

    #[test]
    fn promotion_with_regression_is_a_contradiction() {
        // delta = +0.03 (loss ROSE) but recorded as promoted → contradiction.
        let row = promote_row(Some(0.03));
        match check_row(&row, 0.01) {
            RowVerdict::Contradiction(c) => {
                assert_eq!(c.challenger_generation_id, "v1");
                assert_eq!(c.champion_generation_id, "v0");
                assert!(c.detail.contains("regressed"));
            }
            other => panic!("expected contradiction, got {other:?}"),
        }
    }

    #[test]
    fn promotion_with_insufficient_drop_is_a_contradiction() {
        // delta = -0.005, margin = 0.01 → improved but not enough → contra.
        let row = promote_row(Some(-0.005));
        match check_row(&row, 0.01) {
            RowVerdict::Contradiction(c) => {
                assert!(c.detail.contains("insufficient"), "{}", c.detail);
                assert!((c.delta_scalar - (-0.005)).abs() < 1e-9);
            }
            other => panic!("expected contradiction, got {other:?}"),
        }
    }

    #[test]
    fn promotion_exactly_at_the_margin_is_supported() {
        // delta == -margin exactly clears (the gate uses <=).
        let row = promote_row(Some(-0.01));
        assert_eq!(check_row(&row, 0.01), RowVerdict::Supported);
    }

    #[test]
    fn reject_decisions_are_never_alarmed() {
        // A reject is gated by other rules too; a scalar-only recompute cannot
        // contradict it, so it is never flagged (NotAPromotion path).
        let mut row = promote_row(Some(-0.5));
        row.decision = Some("rejected".into());
        assert_eq!(check_row(&row, 0.01), RowVerdict::NotAPromotion);
    }

    #[test]
    fn promotion_without_scalar_evidence_is_skipped() {
        // No delta and no absolute scalars → cannot judge → skip (fail-open).
        let row = promote_row(None);
        assert_eq!(check_row(&row, 0.01), RowVerdict::SkippedNoEvidence);
    }

    #[test]
    fn delta_is_derived_from_absolute_scalars_when_no_delta() {
        // child 0.5, parent 0.8 → delta -0.3 → supported at margin 0.01.
        let mut row = promote_row(None);
        row.child_scalar = Some(0.5);
        row.parent_scalar = Some(0.8);
        assert_eq!(check_row(&row, 0.01), RowVerdict::Supported);
        // child 0.8, parent 0.5 → delta +0.3 → contradiction.
        let mut bad = promote_row(None);
        bad.child_scalar = Some(0.8);
        bad.parent_scalar = Some(0.5);
        assert!(matches!(
            check_row(&bad, 0.01),
            RowVerdict::Contradiction(_)
        ));
    }

    #[test]
    fn findings_store_round_trips() {
        let store = PromotionGateFindings::new();
        assert!(!store.view().scanned);
        store.record(PromotionGateView {
            scanned: true,
            promotions_checked: 2,
            skipped: 1,
            contradictions: vec![],
        });
        let v = store.view();
        assert!(v.scanned);
        assert_eq!(v.promotions_checked, 2);
        assert_eq!(v.skipped, 1);
    }
}
