"""Pure minter for the mechanical recombination slot. NO IO.

The counterpart to the pure selector (:mod:`zicato.epoch.recombine`): the
orchestrator's IO builder selects a pair of rejected complementary
parents, packs them into the envelope-clean :class:`RecombinationPair`
value below, and threads it on
:attr:`~zicato.proposer.agent.ProposerContext.recombine_pair`; the
best-of-N wrapper's LAST slot then calls
:func:`mint_recombined_experiment` instead of sampling the LLM. Both
halves here are pure functions of their inputs — the proposer stack
stays IO-free (dev-guide 05 §5.3.7: no storage/index reads below the
orchestrator).

Envelope discipline (LOAD-BEARING): a :class:`RecombinationPair` carries
COUNTS + patches + hypothesis text ONLY — never a board-entry id. The
improved/regressed sets the selector ranked on are reduced to counts
before they ride the context, so nothing here widens the proposer's
restricted visibility (dev-guide 05 §5.2: the context is the envelope
boundary; OVERFITTING.md §11).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import TypeVar

from zicato.core.types import (
    ExpectedDriftMovement,
    ExpectedMetricMovement,
    Experiment,
    HypothesisSpec,
    Patch,
)

#: Prefix stamped onto a recombined mint's ``hypothesis.core_idea`` — the
#: DISPLAY marker (the journal heading, the dashboard hero). Machine
#: consumers read :attr:`Experiment.recombined_from`, never this prefix
#: (the placebo-marker lesson applies only where no typed field exists).
RECOMBINED_HYPOTHESIS_MARKER = "[recombined]"

#: Per-parent budget for the composed core idea, keeping the whole line
#: within ~180 chars (marker + two 80-char halves + the join).
_CORE_IDEA_HALF_MAX = 80


@dataclass(frozen=True, slots=True)
class RecombinationPair:
    """The envelope-clean value the orchestrator threads per round.

    Everything the minter needs, and NOTHING the proposer stack may not
    see: the two parents' generation ids (lineage coordinates — already
    visible in the experiment memory), their patch tuples, their
    hypothesis text, and the selector's evidence reduced to COUNTS. No
    board-entry id ever rides this value (the holdout-leak closure).

    ``a`` precedes ``b`` in ascending generation-id order by the
    selector's contract; the minter relies on that for its fixed
    A-then-B patch order.
    """

    a_generation_id: str
    b_generation_id: str
    a_patches: tuple[Patch, ...]
    b_patches: tuple[Patch, ...]
    a_core_idea: str
    b_core_idea: str
    #: COUNTS ONLY — the selector's per-entry evidence, aggregated: how
    #: many TRAIN entries each parent improved, and the size of the pair's
    #: combined improved / regressed unions. Rendered into the mint's
    #: ``why`` so the journal records the grounds without an entry id.
    a_improved_count: int = 0
    b_improved_count: int = 0
    combined_improved_count: int = 0
    combined_regressed_count: int = 0
    #: Each parent's whole-candidate BANDED outcome — the ``improved`` /
    #: ``flat`` / ``regressed`` bucket of its settled Δscalar through the
    #: experiment-memory vocabulary (:func:`zicato.proposer.prompts
    #: ._bucket_scalar_delta`), or ``""`` when unsettled. The exact Δscalar
    #: never rides this value: the builder bands it before construction, so
    #: only the coarse label reaches the (envelope-clean) LLM merge prompt
    #: (PROPOSER.md §2.6.1). Unused by the mechanical mint.
    a_banded_outcome: str = ""
    b_banded_outcome: str = ""
    a_expected_drift_movements: tuple[ExpectedDriftMovement, ...] = ()
    b_expected_drift_movements: tuple[ExpectedDriftMovement, ...] = ()
    a_expected_metric_movements: tuple[ExpectedMetricMovement, ...] = ()
    b_expected_metric_movements: tuple[ExpectedMetricMovement, ...] = ()


def _half(text: str) -> str:
    """One parent's core idea clipped to its budget, single-line."""
    line = " ".join(text.strip().split())
    if len(line) <= _CORE_IDEA_HALF_MAX:
        return line
    return line[: _CORE_IDEA_HALF_MAX - 1].rstrip() + "…"


_T = TypeVar("_T")


def _dedup_first_wins(items: tuple[_T, ...], key: Callable[[_T], Hashable]) -> tuple[_T, ...]:
    """Concatenated movements deduped by ``key``, first occurrence winning.

    A-then-B order: where both parents predict the same axis, parent A's
    (lower-gid) prediction stands — deterministic and documented, matching
    the fixed A-then-B patch order.
    """
    seen: set[Hashable] = set()
    out: list[_T] = []
    for item in items:
        k = key(item)
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return tuple(out)


def mint_recombined_experiment(
    pair: RecombinationPair,
    *,
    epoch_id: str,
    parent_generation_id: str,
    new_generation_id: str,
    proposed_at: str,
) -> Experiment:
    """Mint the union experiment from a selected pair. Pure — no IO, no clock.

    * **Patch order** — A's patches then B's, in the pair's ascending-gid
      order. Under the disjointness predicate the order cannot change the
      applied tree; it is fixed anyway so the mint is byte-stable for
      tests and re-runs.
    * **Fresh patch ids** — every merged patch gets a NEW ``uuid4`` hex id
      (no aliasing with the parents' persisted patch files; provenance
      lives in :attr:`Experiment.recombined_from` rather than in shared ids).
    * **Hypothesis** — ``core_idea`` composes both parents' ideas under
      the :data:`RECOMBINED_HYPOTHESIS_MARKER` prefix (≤ ~180 chars);
      ``modulating`` is the union of the PATCH mutation-ids (manifest-valid
      by selector predicate #6, and exactly what the applier verifies);
      ``why`` is COUNTS-ONLY (envelope-clean); the expected movements are
      the parents' concatenated A-then-B and deduplicated first-wins (per
      axis: drift kind / metric name).
    * **Provenance** — ``recombined_from`` carries the ascending-gid pair.
      The ``outcome`` starts ``None`` like every proposer mint.
    """
    merged: list[Patch] = []
    for patch in (*pair.a_patches, *pair.b_patches):
        merged.append(
            Patch(
                id=uuid.uuid4().hex,
                mutation_id=patch.mutation_id,
                op=patch.op,
                new_content=patch.new_content,
                new_numeric=patch.new_numeric,
                new_enum=patch.new_enum,
                rationale=patch.rationale,
            )
        )

    core_idea = (
        f"{RECOMBINED_HYPOTHESIS_MARKER} {_half(pair.a_core_idea)} + {_half(pair.b_core_idea)}"
    )
    modulating = tuple(sorted({p.mutation_id for p in merged}))
    why = (
        "Mechanical recombination of two rejected complementary fixes: "
        f"parent A improved {pair.a_improved_count} train entr(y/ies), "
        f"parent B improved {pair.b_improved_count}; together they cover "
        f"{pair.combined_improved_count} distinct entries "
        f"({pair.combined_regressed_count} with an observed single-sample "
        "regression). Their patch sets touch disjoint mutation points, so "
        "the union applies both fixes intact."
    )
    drift_movements = _dedup_first_wins(
        (*pair.a_expected_drift_movements, *pair.b_expected_drift_movements),
        key=lambda m: m.kind,
    )
    metric_movements = _dedup_first_wins(
        (*pair.a_expected_metric_movements, *pair.b_expected_metric_movements),
        key=lambda m: m.metric_name,
    )

    return Experiment(
        id=f"exp_{epoch_id}_{new_generation_id}",
        epoch_id=epoch_id,
        generation_id=new_generation_id,
        parent_generation_id=parent_generation_id,
        proposed_at=proposed_at,
        hypothesis=HypothesisSpec(
            core_idea=core_idea,
            modulating=modulating,
            why=why,
            expected_drift_movements=drift_movements,
            expected_pass_rate_delta="",
            risks=(
                "The union's diff is larger than either parent's; the screen "
                "and the tournament gate remain the arbiters."
            ),
            expected_metric_movements=metric_movements,
        ),
        patches=tuple(merged),
        outcome=None,
        recombined_from=(pair.a_generation_id, pair.b_generation_id),
    )


__all__ = [
    "RECOMBINED_HYPOTHESIS_MARKER",
    "RecombinationPair",
    "mint_recombined_experiment",
]
