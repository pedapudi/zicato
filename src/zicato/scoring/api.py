"""Typed, frozen contexts for zicato's two scoring seams.

zicato's scoring pipeline has two stages that have historically absorbed
core edits whenever a new scoring *shape* was needed:

* **Seam 1 — per-run drift reduction** (``telemetry/reducer.py``): turns a
  run's drift counts + plan revisions + task-failure ratio + runtime into a
  single ``drift_loss`` scalar. Runs INSIDE the killable worker subprocess.
* **Seam 2 — per-generation scalar synthesis** (``tournament/scoring.py``):
  turns ``pass_rate`` / ``mean_score`` + ``drift_loss_mean`` + namespace
  aggregates + per-judge loss into the per-generation ``scalar``. Runs in
  the orchestrator.

This module defines the **read-only typed contexts** that flow into each
seam. Each context carries the BUILT-IN result (``builtin_loss`` /
``builtin_scalar``) so a future plugin can *wrap/adjust* the default rather
than reimplement it from scratch.

PHASE 1 (this change) is a **pure refactor**: the live paths compute the
built-in result and return it unchanged, threaded through small dispatchers
in :mod:`zicato.scoring.dispatch`. The contexts are the stable surface the
later phases (declarative transforms, dotted-spec plugins) build on without
rewriting the seams.

The contexts are frozen so a plugin cannot mutate the inputs another part of
the pipeline already read, and so they hash cleanly. They carry only plain
data — no callables, no I/O handles — because scoring is a pure,
deterministic, no-LLM, no-wall-clock computation by contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from zicato.core import DriftCount, ScoringWeights

# Provenance marker threaded out of each dispatcher. PHASE 1 only ever emits
# ``"builtin"`` (the extracted default formula produced the value). Phase 2
# (declarative transforms) and Phase 3 (dotted-spec plugins) enrich this —
# e.g. ``"transform:pow"`` or ``"plugin:mypkg.contract.scoring:my_scalar"``,
# and ``"builtin (fallback: plugin raised)"`` for the fail-open path. Kept a
# bare string so it serialises into ``loss.json`` without a custom codec.
ScoringProvenance = str

#: The PHASE 1 provenance value — the extracted built-in formula produced the
#: result, no transform/plugin involved. A single greppable constant so the
#: dispatchers, the tests, and the later phases all agree on the token.
PROVENANCE_BUILTIN: ScoringProvenance = "builtin"


@dataclass(frozen=True)
class DriftContext:
    """Read-only inputs to **Seam 1** (per-run drift reduction).

    Mirrors the real signature of
    :func:`zicato.telemetry.reducer.compute_drift_loss` — every field here
    is an input that formula consumes — plus ``builtin_loss``, the value the
    built-in formula returns for these inputs.

    Fields
    ------
    drift_counts:
        Per ``(kind, severity)`` drift counts for the run.
    plan_revisions:
        Count of plan-revision events.
    task_failure_ratio:
        Fatally-failed-to-started task ratio in ``[0.0, 1.0]`` (already
        floored to ``1.0`` by the reducer for a not-completed run, before
        this context is built).
    runtime_ms:
        Total wall-clock duration in milliseconds.
    weights:
        The epoch's frozen :class:`~zicato.core.ScoringWeights`.
    builtin_loss:
        What :func:`zicato.scoring.builtins.builtin_drift_loss` returns for
        the fields above — the default a wrapping plugin starts from.
    """

    drift_counts: tuple[DriftCount, ...]
    plan_revisions: int
    task_failure_ratio: float
    runtime_ms: int
    weights: ScoringWeights
    builtin_loss: float


@dataclass(frozen=True)
class ScalarContext:
    """Read-only inputs to **Seam 2** (per-generation scalar synthesis).

    Mirrors the real inputs of
    :func:`zicato.tournament.scoring.aggregate_generation_score`'s scalar
    composition — see :func:`zicato.scoring.builtins.builtin_scalar` for the
    formula — plus ``builtin_scalar``, the value the built-in formula returns.

    The scalar runs on the UNIFORM continuous outcome axis ``mean_score``
    (issue #18), NOT the binary ``pass_rate``; both are carried so a wrapping
    plugin (and the provenance breakdown) can see each. ``pass_rate`` is
    kept because the gate / display surface still read it and the issue's
    diagram lists it as a per-generation input.

    Fields
    ------
    pass_rate:
        Binary pass fraction over the expectation denominator. Display / gate
        view; equals ``mean_score`` byte-for-byte on an all-bool board.
    mean_score:
        The uniform continuous outcome axis (issue #18) the scalar's pass
        component actually runs on. Mean of each entry's clamped
        ``[0, 1]`` score over the expectation denominator.
    drift_loss_mean:
        Mean per-run ``drift_loss`` across the generation's entries — the
        input to the drift component.
    namespace_aggregates:
        ``{namespace: weighted_aggregate}``, ALREADY weight-multiplied by
        :func:`zicato.tournament.scoring.aggregate_namespaced_metrics`. The
        scalar adds every non-``"drift:"`` namespace value directly. The
        ``"drift:"`` entry is owned by the drift component and excluded from
        the sum to avoid double-counting.
    per_judge_loss:
        ``{judge_name: weighted_loss}`` per-judge attribution for the
        generation. Carried for plugin/provenance visibility; the built-in
        scalar does not add it separately (it is already folded into
        ``drift_loss_mean`` via the reducer's ``drift_loss``).
    weights:
        The epoch's frozen :class:`~zicato.core.ScoringWeights`.
    builtin_scalar:
        What :func:`zicato.scoring.builtins.builtin_scalar` returns for the
        fields above — the default a wrapping plugin starts from.
    """

    pass_rate: float
    mean_score: float
    drift_loss_mean: float
    namespace_aggregates: Mapping[str, float]
    per_judge_loss: Mapping[str, float]
    weights: ScoringWeights
    builtin_scalar: float


__all__ = [
    "DriftContext",
    "ScalarContext",
    "ScoringProvenance",
    "PROVENANCE_BUILTIN",
]
