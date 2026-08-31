"""hypothesis_view — prediction-accuracy + calibration projection readers.

Pure, additive projection over the SAME stamped flag the static HTML
report renders. The predicted-vs-actual movement verdict
(``hypothesis_match``) is computed once, at outcome-write time, onto each
:class:`zicato.core.experiment.DriftMovementActual` /
:class:`~zicato.core.experiment.MetricMovementActual`. These readers
**lift that stamped flag verbatim** — they NEVER re-derive a match from
the raw ``from``/``to`` values — so the dashboard data and the HTML
report's "Expected vs actual" table can never disagree.

Two surfaces:

* :func:`build_hypothesis_accuracy` — per-experiment scorecard for one
  ``(epoch, generation)``: every falsifiable movement claim joined
  against its realised movement, with the stamped verdict, plus the
  ``hits/total/fraction`` rollup and the free-text pass-rate claim.
* :func:`build_calibration_trend` — the per-generation score fraction
  over the lineage order, with rolling aggregates. Explicitly
  DIAGNOSTIC: it surfaces calibration drift, it never feeds the gate.

Both are best-effort: a missing / malformed ``experiment.json`` degrades
to an empty scorecard / empty trend rather than raising, matching every
other reader in this package.
"""

from __future__ import annotations

from typing import Any

from zicato.query.decisions import experiment_decision as _experiment_decision
from zicato.query.paths import (
    WorkspacePaths,
    _natural_key,
    _read_json_value,
    _resolve_epoch_id,
    layout_of,
)

# Plateau band: a realised movement whose magnitude is within this of zero
# is read as "neutral" / flat when deriving the OBSERVED direction. Mirrors
# :data:`zicato.tournament.detail.PLATEAU_EPSILON` so the observed-direction
# label this reader surfaces lines up with the canonical grader's sign test.
# This is ONLY used to LABEL the observed direction for display — the
# match/miss verdict is always lifted from the stamped flag, never recomputed.
_PLATEAU_EPSILON = 1e-9


def _as_opt_float(value: Any) -> float | None:
    """Coerce a JSON value to ``float``; non-numeric / ``None`` -> ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _observed_direction(from_value: float | None, to_value: float | None) -> str | None:
    """Label the realised direction of a movement for display.

    ``None`` when either endpoint is missing (the proposer predicted a
    movement the outcome never recorded). Otherwise ``"decrease"`` /
    ``"increase"`` / ``"neutral"`` by the sign of ``to - from`` against the
    plateau band. Purely a display label — never the verdict.
    """
    if from_value is None or to_value is None:
        return None
    delta = to_value - from_value
    if delta < -_PLATEAU_EPSILON:
        return "decrease"
    if delta > _PLATEAU_EPSILON:
        return "increase"
    return "neutral"


def _expected_index(hypothesis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{target: {kind, predicted_direction, predicted_magnitude}}``.

    Indexes the proposer's falsifiable movement claims. Both namespaced
    metric movements (``expected_metric_movements``, keyed by
    ``metric_name``) and drift movements (``expected_drift_movements``,
    keyed by ``kind``) are folded into one target → claim map. When the
    same target appears in both, the metric-movement claim wins (it is the
    superset surface), matching the grader's precedence — the first writer
    for a target is kept.
    """
    out: dict[str, dict[str, Any]] = {}
    for mv in hypothesis.get("expected_metric_movements", []) or []:
        if not isinstance(mv, dict):
            continue
        name = mv.get("metric_name")
        if isinstance(name, str) and name:
            out.setdefault(
                name,
                {
                    "kind": "metric",
                    "predicted_direction": str(mv.get("direction", "")),
                    "predicted_magnitude": str(mv.get("magnitude", "")),
                },
            )
    for mv in hypothesis.get("expected_drift_movements", []) or []:
        if not isinstance(mv, dict):
            continue
        kind = mv.get("kind")
        if isinstance(kind, str) and kind:
            out.setdefault(
                kind,
                {
                    "kind": "drift",
                    "predicted_direction": str(mv.get("direction", "")),
                    "predicted_magnitude": str(mv.get("magnitude", "")),
                },
            )
    return out


def _actual_index(outcome: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{target: {kind, from, to, hypothesis_match, note}}``.

    Indexes the realised movements off the stamped outcome. ``from``/``to``
    are the per-run aggregates; ``hypothesis_match`` is the STAMPED verdict
    lifted verbatim (``None`` only if the persisted record omitted it).
    Metric movements (keyed by ``metric_name``) and drift movements (keyed
    by ``kind``) fold into one target map, metric-first like the expected
    index so the two join on the same key space.
    """
    out: dict[str, dict[str, Any]] = {}
    for mv in outcome.get("metric_movements", []) or []:
        if not isinstance(mv, dict):
            continue
        name = mv.get("metric_name")
        if not isinstance(name, str) or not name:
            continue
        match = mv.get("hypothesis_match")
        out.setdefault(
            name,
            {
                "kind": "metric",
                "from": _as_opt_float(mv.get("from_value")),
                "to": _as_opt_float(mv.get("to_value")),
                "hypothesis_match": bool(match) if isinstance(match, bool) else None,
                "note": str(mv.get("note", "")),
            },
        )
    for mv in outcome.get("drift_movements", []) or []:
        if not isinstance(mv, dict):
            continue
        kind = mv.get("kind")
        if not isinstance(kind, str) or not kind:
            continue
        match = mv.get("hypothesis_match")
        out.setdefault(
            kind,
            {
                "kind": "drift",
                "from": _as_opt_float(mv.get("from_rate")),
                "to": _as_opt_float(mv.get("to_rate")),
                "hypothesis_match": bool(match) if isinstance(match, bool) else None,
                "note": str(mv.get("note", "")),
            },
        )
    return out


def _scorecard_from_experiment(experiment: dict[str, Any]) -> dict[str, Any]:
    """Project ONE ``experiment.json`` dict into the accuracy scorecard.

    The pure core of :func:`build_hypothesis_accuracy`, factored out so the
    calibration-trend walk can score each generation through the SAME path
    (one stamped-flag join, never two divergent ones). Returns
    ``{claims, score, pass_rate}`` — see :func:`build_hypothesis_accuracy`
    for the field shapes.
    """
    hypothesis = experiment.get("hypothesis")
    hypothesis = hypothesis if isinstance(hypothesis, dict) else {}
    outcome = experiment.get("outcome")
    outcome = outcome if isinstance(outcome, dict) else {}

    expected = _expected_index(hypothesis)
    actual = _actual_index(outcome)

    claims: list[dict[str, Any]] = []
    hits = 0
    total = 0
    # Predicted claims first, in a stable target order, then any realised
    # movement the proposer did not predict (``unpredicted``). Only PREDICTED
    # claims count toward hits/total — the unpredicted rows are surfaced for
    # context (the runner may flag a large movement nobody claimed) but never
    # score the proposer's calibration.
    for target in sorted(expected.keys(), key=_natural_key):
        claim_pred = expected[target]
        act = actual.get(target)
        from_v = act["from"] if act is not None else None
        to_v = act["to"] if act is not None else None
        match = act["hypothesis_match"] if act is not None else None
        note = act["note"] if act is not None else ""
        signed_error = (to_v - from_v) if (from_v is not None and to_v is not None) else None
        claims.append(
            {
                "target": target,
                "kind": claim_pred["kind"],
                "predicted_direction": claim_pred["predicted_direction"],
                "predicted_magnitude": claim_pred["predicted_magnitude"],
                "from_rate": from_v,
                "to_rate": to_v,
                "observed_direction": _observed_direction(from_v, to_v),
                "signed_error": signed_error,
                "hypothesis_match": match,
                "unpredicted": False,
                "note": note,
            }
        )
        total += 1
        if match is True:
            hits += 1

    for target in sorted(set(actual.keys()) - set(expected.keys()), key=_natural_key):
        act = actual[target]
        from_v = act["from"]
        to_v = act["to"]
        signed_error = (to_v - from_v) if (from_v is not None and to_v is not None) else None
        claims.append(
            {
                "target": target,
                "kind": act["kind"],
                "predicted_direction": None,
                "predicted_magnitude": None,
                "from_rate": from_v,
                "to_rate": to_v,
                "observed_direction": _observed_direction(from_v, to_v),
                "signed_error": signed_error,
                "hypothesis_match": act["hypothesis_match"],
                "unpredicted": True,
                "note": act["note"],
            }
        )

    fraction = (hits / total) if total else None
    score = {
        "hits": hits,
        "total": total,
        "fraction": fraction,
        # Brier is null: the schema carries no probabilistic forecast to
        # score against (predictions are direction and magnitude buckets
        # rather than probabilities). The key is present so callers can
        # light up a Brier
        # column the moment a probabilistic forecast field lands.
        "brier": None,
    }

    # The free-text pass-rate claim is NOT a stamped match (there is no
    # per-pass-rate hypothesis_match flag), so it rides alongside the scored
    # movement claims rather than inside hits/total. ``predicted`` is the
    # proposer's free text; ``observed`` is the realised board-wide delta.
    pred_pr = hypothesis.get("expected_pass_rate_delta")
    observed_pr = _as_opt_float(outcome.get("pass_rate_delta")) if outcome else None
    pass_rate = {
        "predicted": str(pred_pr) if isinstance(pred_pr, str) else "",
        "observed": observed_pr,
    }

    return {"claims": claims, "score": score, "pass_rate": pass_rate}


def build_hypothesis_accuracy(
    paths: WorkspacePaths, epoch_id: str, generation_id: str
) -> dict[str, Any]:
    """``GET /api/hypothesis-accuracy/{epoch}/{gen}`` — per-experiment scorecard.

    Reads ``epochs/{epoch}/generations/{gen}/experiment.json`` and joins the
    proposer's falsifiable movement claims (``expected_metric_movements`` /
    ``expected_drift_movements``) against the realised movements
    (``metric_movements`` / ``drift_movements``). Each claim lifts the
    STAMPED ``hypothesis_match`` verdict verbatim — the same flag the static
    HTML report renders — so this data can never disagree with the report.

    Returns::

        {
          "epoch_id", "generation_id",
          "claims": [
            {"target", "kind", "predicted_direction", "predicted_magnitude",
             "from_rate", "to_rate", "observed_direction", "signed_error",
             "hypothesis_match", "unpredicted", "note"}
          ],
          "score": {"hits", "total", "fraction"|null, "brier"|null},
          "pass_rate": {"predicted", "observed"|null}
        }

    ``hypothesis_match`` is the stamped bool, or ``null`` when the proposer
    predicted a movement the outcome never recorded (no realised pairing).
    ``unpredicted`` claims are realised movements the proposer did NOT claim;
    they are surfaced for context but never counted in ``score``.

    Best-effort: a missing / malformed ``experiment.json`` degrades to an
    empty scorecard (HTTP 200), never a 500.
    """
    empty: dict[str, Any] = {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "claims": [],
        "score": {"hits": 0, "total": 0, "fraction": None, "brier": None},
        "pass_rate": {"predicted": "", "observed": None},
    }
    exp_path = layout_of(paths).experiment(epoch_id, generation_id)
    experiment = _read_json_value(exp_path)
    if not isinstance(experiment, dict):
        return empty
    card = _scorecard_from_experiment(experiment)
    return {
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        **card,
    }


def _round_index_of(experiment: dict[str, Any]) -> int | None:
    """Read a generation's birth round off ``experiment.json`` (int only).

    ``None`` when the stamp is absent or non-integer: a record written
    before the ``round_index`` stamp existed carries none, and this read is
    as tolerant of that as the lineage reader is.
    """
    raw = experiment.get("round_index")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        return int(raw.strip())
    return None


def build_calibration_trend(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """``GET /api/calibration-trend[?epoch=<id>]`` — score fraction over lineage.

    DIAGNOSTIC ONLY. Walks the epoch's generations in lineage order
    (numeric-aware id order, matching the lineage ledger) and scores each
    one's predictions through the SAME stamped-flag join
    :func:`build_hypothesis_accuracy` uses, then summarises the trend. This
    surfaces whether the proposer's calibration is drifting; it NEVER feeds
    the promote gate.

    ``epoch_id`` defaults to the current epoch; a given id is validated
    against the on-disk epoch set (a path-unsafe / unknown id raises
    ``ValueError``, which the endpoint maps to 404).

    Returns::

        {
          "epoch_id",
          "points": [
            {"generation_id", "epoch_id", "round_index"|null,
             "score_fraction"|null, "total_claims", "decision"}
          ],
          "rolling_mean"|null, "n_scored", "latest_fraction"|null, "trend_sign"
        }

    * ``score_fraction`` is ``hits/total`` for that generation, or ``null``
      when it made no falsifiable claims (``total_claims == 0``).
    * ``rolling_mean`` is the mean of every non-null ``score_fraction``
      (``null`` when nothing scored).
    * ``n_scored`` is the count of generations that carried claims.
    * ``latest_fraction`` is the most recent non-null fraction in lineage
      order (``null`` when nothing scored).
    * ``trend_sign`` compares the mean of the latter half of the scored
      fractions to the former half: ``+1`` improving (later half higher),
      ``-1`` regressing, ``0`` flat or too few points. A higher fraction is
      better calibration, so a positive sign means calibration improved.
    """
    resolved = _resolve_epoch_id(paths, epoch_id)
    empty: dict[str, Any] = {
        "epoch_id": resolved,
        "points": [],
        "rolling_mean": None,
        "n_scored": 0,
        "latest_fraction": None,
        "trend_sign": 0,
    }
    if resolved is None:
        return empty

    gens_dir = layout_of(paths).generations_dir(resolved)
    if not gens_dir.is_dir():
        return empty

    points: list[dict[str, Any]] = []
    scored_fractions: list[float] = []
    for gen_dir in sorted(gens_dir.iterdir(), key=lambda p: _natural_key(p.name)):
        if not gen_dir.is_dir():
            continue
        experiment = _read_json_value(gen_dir / "experiment.json")
        if not isinstance(experiment, dict):
            continue
        card = _scorecard_from_experiment(experiment)
        total = card["score"]["total"]
        fraction = card["score"]["fraction"]
        decision = _experiment_decision(experiment)
        points.append(
            {
                "generation_id": gen_dir.name,
                "epoch_id": resolved,
                "round_index": _round_index_of(experiment),
                "score_fraction": fraction,
                "total_claims": total,
                "decision": decision,
            }
        )
        if fraction is not None:
            scored_fractions.append(float(fraction))

    n_scored = len(scored_fractions)
    rolling_mean = (sum(scored_fractions) / n_scored) if n_scored else None
    latest_fraction = scored_fractions[-1] if scored_fractions else None
    trend_sign = _trend_sign(scored_fractions)

    return {
        "epoch_id": resolved,
        "points": points,
        "rolling_mean": rolling_mean,
        "n_scored": n_scored,
        "latest_fraction": latest_fraction,
        "trend_sign": trend_sign,
    }


def _trend_sign(fractions: list[float]) -> int:
    """Sign of the calibration trend: later-half mean vs former-half mean.

    ``+1`` when the latter half of the scored fractions has a strictly
    higher mean than the former half (calibration improving), ``-1`` when
    strictly lower, ``0`` when equal or there are fewer than two scored
    points to compare. A higher fraction is better calibration, so a
    positive sign means calibration improved.
    """
    n = len(fractions)
    if n < 2:
        return 0
    mid = n // 2
    former = fractions[:mid]
    latter = fractions[mid:]
    if not former or not latter:
        return 0
    former_mean = sum(former) / len(former)
    latter_mean = sum(latter) / len(latter)
    if latter_mean > former_mean:
        return 1
    if latter_mean < former_mean:
        return -1
    return 0


__all__ = [
    "build_calibration_trend",
    "build_hypothesis_accuracy",
]
