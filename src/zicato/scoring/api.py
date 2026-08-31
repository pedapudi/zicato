"""Typed, frozen contexts for zicato's two scoring seams.

zicato's scoring pipeline has two stages that would otherwise absorb a core
edit whenever a new scoring *shape* is needed:

* **Seam 1 — per-run drift reduction** (``telemetry/reducer.py``): turns a
  run's drift counts + plan revisions into a single ``drift_loss`` scalar.
  Runs INSIDE the killable worker subprocess.
* **Seam 2 — per-generation scalar synthesis** (``tournament/scoring.py``):
  turns ``pass_rate`` / ``mean_score`` + ``drift_loss_mean`` + namespace
  aggregates + per-judge loss into the per-generation ``scalar``. Runs in
  the orchestrator.

This module defines the **read-only typed contexts** that flow into each
seam. Each context carries the BUILT-IN result (``builtin_loss`` /
``builtin_scalar``) so a future plugin can *wrap/adjust* the default rather
than reimplement it from scratch.

With no transform and no plugin configured, the live paths compute the
built-in result and return it unchanged, threaded through small dispatchers
in :mod:`zicato.scoring.dispatch`. The contexts are the stable surface that
declarative transforms and dotted-spec plugins build on without rewriting
the seams.

The contexts are frozen so a plugin cannot mutate the inputs another part of
the pipeline already read, and so they hash cleanly. They carry only plain
data — no callables, no I/O handles — because scoring is a pure,
deterministic, no-LLM, no-wall-clock computation by contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from zicato.core import DriftCount, ScoringWeights

# Provenance marker threaded out of each dispatcher. An unconfigured seam
# emits ``"builtin"`` (the default formula produced the value). A declarative
# transform or a dotted-spec plugin enriches it — for example
# ``"transform:pow"`` or ``"plugin:mypkg.contract.scoring:my_scalar"``, and
# ``"builtin (fallback: plugin raised)"`` for the fail-open path. Kept a
# bare string so it serialises into ``loss.json`` without a custom codec.
ScoringProvenance = str

#: The unconfigured-seam provenance value: the built-in formula produced the
#: result, with no transform and no plugin involved. A single greppable
#: constant so the dispatchers, the transforms, the plugins, and the tests
#: all agree on the token.
PROVENANCE_BUILTIN: ScoringProvenance = "builtin"


@dataclass(frozen=True)
class DriftContext:
    """Read-only inputs to **Seam 1** (per-run drift reduction).

    Carries the run facts Seam 1 is computed from, plus ``builtin_loss``, the
    value the built-in formula returns for them.

    Fields
    ------
    drift_counts:
        Per ``(kind, severity)`` drift counts for the run.
    plan_revisions:
        Count of plan-revision events.
    task_failure_ratio:
        Fatally-failed-to-started task ratio in ``[0.0, 1.0]`` (already
        floored to ``1.0`` by the reducer for a not-completed run, before
        this context is built). NOT part of the drift formula — it is the
        ``failure:tasks`` channel member — and carried here so a drift plugin
        can see the outcome of the run it is scoring.
    runtime_ms:
        Total wall-clock duration in milliseconds. Likewise not part of the
        drift formula; it is the ``runtime:seconds`` channel member.
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
        ``drift:`` channel's aggregate BEFORE its namespace coefficient.
        Carried for display / plugin visibility; the built-in scalar reads
        the already-weighted ``"drift:"`` entry of ``namespace_aggregates``
        instead, so this must not be added a second time.
    namespace_aggregates:
        ``{namespace: weighted_aggregate}``, ALREADY weight-multiplied by
        :func:`zicato.tournament.scoring.aggregate_namespaced_metrics`. The
        scalar adds EVERY namespace value directly, in sorted key order —
        drift included, with no privileged channel.
    per_judge_loss:
        ``{judge_name: weighted_loss}`` per-judge attribution for the
        generation. Carried for plugin/provenance visibility; the built-in
        scalar does not add it separately — the same per-judge values reach
        the scalar as the ``judge:<name>`` metrics of the ``judge:``
        namespace, and adding them here as well would double-count.
    weights:
        The epoch's frozen :class:`~zicato.core.ScoringWeights`.
    builtin_scalar:
        What :func:`zicato.scoring.builtins.builtin_scalar` returns for the
        fields above — the default a wrapping plugin starts from.
    diff_size:
        The CHALLENGER's ``{added, removed, patches}`` diff size (see
        :func:`zicato.scoring.diff_complexity.diff_size`), or ``None`` for a
        side with no challenger experiment / a caller that does not thread it.
        Carried for plugin/provenance visibility and as the input the built-in
        ``diff_complexity`` term reads. ``None`` (the default) leaves the
        scalar byte-identical — the opt-in term is gated on
        :attr:`~zicato.core.types.ScoringWeights.diff_complexity_weight` being
        ``> 0`` AND this being present.
    """

    pass_rate: float
    mean_score: float
    drift_loss_mean: float
    namespace_aggregates: Mapping[str, float]
    per_judge_loss: Mapping[str, float]
    weights: ScoringWeights
    builtin_scalar: float
    diff_size: Mapping[str, int] | None = None


__all__ = [
    "DriftContext",
    "ScalarContext",
    "ScoringProvenance",
    "PROVENANCE_BUILTIN",
]
