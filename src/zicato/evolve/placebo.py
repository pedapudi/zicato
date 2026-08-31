"""Random-baseline (placebo) challenger — OVERFITTING.md #7's control arm.

A/B methodology's placebo: every Nth round
(``overfitting.random_baseline_every_n``, default off) the orchestrator
fields ONE additional challenger whose patch is a **semantics-preserving
no-op** — the FIRST enumerated mutation point's current value re-emitted
unchanged. The baseline tree behaves identically to the champion, so under
a working decision procedure the gate MUST reject it (no improvement can
clear ``promote_margin`` between identical behaviours). The arm therefore
measures the gate itself:

* **rejected** (the expected outcome, every time) — the gate can still
  tell "no change" from "improvement"; the placebo quietly recalibrates
  that fact each cadence tick.
* **promoted** — the alarm. A no-op that wins a tournament means the
  decision procedure is promoting noise (margin under the noise floor,
  a broken reducer, a rigged gate): recent real "wins" are suspect. The
  loop-health channel raises the CRITICAL ``placebo_promoted`` finding
  (:func:`zicato.health.diagnostics.detect_placebo_promoted`).

The placebo's hypothesis ``core_idea`` is prefixed with
:data:`~zicato.core.experiment.PLACEBO_HYPOTHESIS_MARKER` so every
consumer can recognise the arm; loop-health detectors treat placebo
experiments as calibration probes, never as part of the optimization
stream. The placebo NEVER moves the champion pointer on the gauntlet path
(it is an extra scheduled duel after the round rather than a contender); on a
multi-challenger field it enters as one extra slate slot and flows
through the unchanged strategy + gate.

No-op construction (what the applier accepts as a valid no-op)
--------------------------------------------------------------

``validate_patches`` never compares ``new_content`` against the point's
current content, so a re-emission is a valid ``replace``. The exact
payload is kind-dependent:

* ``"file"`` / ``"code"`` points: ``point.content`` verbatim — the
  applier writes the same bytes back (byte-identical no-op).
* ``"span"`` points in ``.py`` files: ``point.content`` is the WHOLE
  source line(s) (``NAME = "..."``), which the applier would wrap as a
  string literal — an assignment echo, NOT a no-op. Instead the span's
  literal is re-resolved through the applier's own node resolution and
  its **value** is re-emitted; the applier re-wraps it as an equivalent
  literal, so the resulting module evaluates to the identical value
  (semantics-preserving, byte-representation may differ).
* non-``.py`` spans (manifest-bridged prompt bodies): the content IS the
  raw value — verbatim re-emission is byte-identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from zicato.core.experiment import (
    PLACEBO_HYPOTHESIS_MARKER,
    Experiment,
    HypothesisSpec,
)
from zicato.core.mutation import MutationPoint, Patch
from zicato.util.iso_time import now_iso as _now_iso


def placebo_round_due(every_n: int, round_n: int | None) -> bool:
    """Whether THIS round fields the placebo arm. Pure.

    ``every_n <= 0`` (the default) never fields one; otherwise every
    round whose epoch-cumulative round number is a multiple of
    ``every_n`` does (``every_n == 1`` ⇒ every round). ``round_n`` is the
    epoch-cumulative round the caller derives from the minted generation
    id (``vN`` ⇒ ``N``); an unresolvable round (``None``) fields nothing
    — the cadence must stay stable across evolve re-invocations, so it
    keys off the persistent numbering only.
    """
    if every_n <= 0 or round_n is None:
        return False
    return round_n % every_n == 0


def placebo_noop_content(point: MutationPoint) -> str:
    """The ``new_content`` that re-emits ``point``'s value unchanged.

    See the module docstring for the per-kind rules. The span path
    re-resolves the literal through the SAME applier helpers a real
    ``replace`` uses, so the value it re-emits is the value the applier
    would be replacing. Falls back to ``point.content`` when the literal
    cannot be re-resolved (an unparseable file); that is still a valid
    patch, though it is no longer guaranteed to preserve semantics. The
    caller's
    best-effort wrapper tolerates the degenerate case.
    """
    if point.kind in ("file", "code") or point.file.suffix != ".py":
        return point.content
    from zicato.mutation.applier import (  # noqa: PLC0415
        _resolve_marker_line,
        _resolve_string_literal_node,
    )

    marker_line = _resolve_marker_line(point.file, point.id)
    node = _resolve_string_literal_node(point.file, marker_line) if marker_line else None
    if node is not None and isinstance(node.value, str):
        return node.value
    return point.content


def placebo_noop_patch(point: MutationPoint) -> Patch:
    """The semantics-preserving no-op :class:`Patch` for one point."""
    return Patch(
        id=uuid4().hex,
        mutation_id=point.id,
        op="replace",
        new_content=placebo_noop_content(point),
        new_numeric=None,
        new_enum=None,
        rationale=(
            "random-baseline placebo arm: re-emit the mutation point's current "
            "value unchanged; the gate must reject this no-op"
        ),
    )


def build_placebo_experiment(
    *,
    epoch_id: str,
    generation_id: str,
    parent_id: str,
    point: MutationPoint,
    round_index: int,
) -> Experiment:
    """The placebo challenger's :class:`Experiment` (outcome pending).

    The hypothesis is clearly marked as the baseline arm — the
    ``core_idea`` opens with :data:`PLACEBO_HYPOTHESIS_MARKER`, the
    stable string the health detector and the loop-health input filter
    key on — and predicts nothing at all (a no-op has no expected
    movement; its only falsifiable claim is "the gate rejects me").
    """
    hypothesis = HypothesisSpec(
        core_idea=(
            f"{PLACEBO_HYPOTHESIS_MARKER} no-op re-emission of {point.id!r} — "
            "a control arm the tournament gate must reject"
        ),
        modulating=(point.id,),
        why=(
            "OVERFITTING.md #7: a semantics-preserving no-op measures the gate's "
            "discrimination — a promoted placebo means the loop is promoting noise"
        ),
        expected_drift_movements=(),
        expected_pass_rate_delta="0 (no-op by construction)",
        risks="none — the tree is behaviourally identical to the champion",
    )
    return Experiment(
        id=f"exp_{epoch_id}_{generation_id}",
        epoch_id=epoch_id,
        generation_id=generation_id,
        parent_generation_id=parent_id,
        proposed_at=_now_iso(),
        hypothesis=hypothesis,
        patches=(placebo_noop_patch(point),),
        outcome=None,
        round_index=round_index,
    )


def derive_placebo_snapshot(
    workspace_root: Path,
    *,
    epoch_id: str,
    parent_id: str,
    generation_id: str,
    patches: list[Patch] | tuple[Patch, ...],
) -> Path:
    """Materialise the placebo child tree through the generation store.

    The same :meth:`GenerationStore.derive_generation` seam every real
    challenger goes through (copy the parent snapshot, apply the no-op
    patch atomically), so the placebo is a genuine lineage child with a
    genuine snapshot — not a synthetic score injection. Returns the child
    snapshot root.
    """
    from zicato.epoch.genstore import default_generation_store  # noqa: PLC0415

    return default_generation_store(workspace_root).derive_generation(
        epoch_id=epoch_id,
        parent_generation_id=parent_id,
        child_generation_id=generation_id,
        patches=list(patches),
    )


def is_placebo_experiment(experiment: Any) -> bool:
    """Whether an experiment (typed or ``experiment.json`` dict) is the arm.

    Reads ``hypothesis.core_idea`` through the same object-or-mapping shim
    discipline the health detectors use and checks for the stable
    :data:`PLACEBO_HYPOTHESIS_MARKER` prefix. Tolerant of missing fields
    (⇒ ``False``).
    """

    def _get(obj: Any, name: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    hypothesis = _get(experiment, "hypothesis")
    if hypothesis is None:
        return False
    core_idea = _get(hypothesis, "core_idea")
    return isinstance(core_idea, str) and core_idea.startswith(PLACEBO_HYPOTHESIS_MARKER)


__all__ = [
    "PLACEBO_HYPOTHESIS_MARKER",
    "build_placebo_experiment",
    "derive_placebo_snapshot",
    "is_placebo_experiment",
    "placebo_noop_content",
    "placebo_noop_patch",
    "placebo_round_due",
]
