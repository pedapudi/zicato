"""Mint the candidates of a queued operator round as the round's first challengers.

``zicato proposer submit`` queues the operator's edits to the champion they
copied. A round takes an item only while that generation is still the
champion (:func:`~zicato.runtime.operator_queue.next_operator_round`), so
the queued edits are patches against the round's own parent and this module
parses them as a proposer's response is parsed. The caller then runs the
shared manifest check and post-apply validator on the result.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER
from zicato.core.types import Experiment, MutationPoint, Patch
from zicato.proposer.recombine import RECOMBINED_HYPOTHESIS_MARKER
from zicato.proposer.structured import ExperimentParseError, parse_experiment_json
from zicato.runtime.operator_queue import QueuedCandidate, QueuedRound

#: Prefixes of ``core_idea`` that mark a candidate the loop minted itself.
#: Consumers read the placebo prefix as the arm's identity, so an operator
#: hypothesis cannot open with one.
_RESERVED_MARKERS = (PLACEBO_HYPOTHESIS_MARKER, RECOMBINED_HYPOTHESIS_MARKER)


def minted_operator_rounds(workspace_root: Any, epoch_id: str) -> dict[str, dict[str, bool]]:
    """Map each queue id minted in this epoch to its generations.

    Each generation maps to whether it has an outcome. A generation behind
    an unreadable experiment record is missing from the map.
    """
    from zicato.epoch.journal import read_epoch_experiment_bodies  # noqa: PLC0415

    bodies, _errors = read_epoch_experiment_bodies(workspace_root, epoch_id)
    minted: dict[str, dict[str, bool]] = {}
    for generation_id, body in bodies:
        operator = body.get("operator")
        if isinstance(operator, dict) and isinstance(operator.get("queue_id"), str):
            minted.setdefault(operator["queue_id"], {})[generation_id] = isinstance(
                body.get("outcome"), dict
            )
    return minted


def queued_patch_body(patch: Patch) -> dict[str, Any]:
    """The queue form of one patch: its target and payload, without id or rationale."""
    return {
        "mutation_id": patch.mutation_id,
        "op": patch.op,
        "new_content": patch.new_content,
        "new_numeric": patch.new_numeric,
        "new_enum": patch.new_enum,
    }


def patch_documents(
    hypothesis: Mapping[str, Any], patches: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The queued ``patches`` in the proposer response schema.

    Every patch carries the capped core idea as its rationale, as the Foe
    proposer stamps its episode's core idea onto its patches
    (``_episode_rationale`` in :mod:`zicato.proposer.foe_agent`).
    """
    from zicato.proposer.foe_agent import PATCH_RATIONALE_CAP  # noqa: PLC0415
    from zicato.proposer.foe_scratch import PROJECTED_RATIONALE  # noqa: PLC0415

    core_idea = " ".join(str(hypothesis.get("core_idea", "")).split())
    rationale = core_idea[:PATCH_RATIONALE_CAP] or PROJECTED_RATIONALE
    payload = {"replace": "new_content", "set_numeric": "new_numeric", "set_enum": "new_enum"}
    return [
        {
            "mutation_id": patch["mutation_id"],
            "op": patch["op"],
            "rationale": rationale,
            payload[patch["op"]]: patch[payload[patch["op"]]],
        }
        for patch in patches
    ]


def parse_operator_experiment(
    hypothesis: Mapping[str, Any],
    patches: Sequence[Mapping[str, Any]],
    *,
    epoch_id: str,
    parent_id: str,
    next_id: str,
    mutations: Sequence[MutationPoint],
    custom_judge_names: frozenset[str],
) -> Experiment:
    """Parse an operator hypothesis and queued patches with the proposer's own parser.

    Raises :class:`~zicato.proposer.structured.ExperimentParseError` with the
    message a proposer would get.
    """
    if str(hypothesis.get("core_idea", "")).lstrip().startswith(_RESERVED_MARKERS):
        raise ExperimentParseError(
            "hypothesis.core_idea: must not open with a marker zicato reserves for its own "
            f"candidates ({', '.join(_RESERVED_MARKERS)})"
        )
    response = json.dumps(
        {"hypothesis": dict(hypothesis), "patches": patch_documents(hypothesis, patches)}
    )
    return parse_experiment_json(
        response,
        epoch_id,
        parent_id,
        next_id,
        {point.id: point for point in mutations},
        custom_judge_names,
    )


def mint_operator_experiment(
    *,
    epoch_id: str,
    parent_id: str,
    next_id: str,
    round_index: int,
    queued: QueuedRound,
    candidate: QueuedCandidate,
    mutations: Sequence[MutationPoint],
    custom_judge_names: frozenset[str],
) -> Experiment:
    """The experiment for ``candidate``, as a child of the champion ``parent_id``.

    Raises :class:`~zicato.proposer.structured.ExperimentParseError` when the
    hypothesis or a patch does not parse against the round's mutation points.
    """
    experiment = parse_operator_experiment(
        candidate.hypothesis,
        candidate.patches,
        epoch_id=epoch_id,
        parent_id=parent_id,
        next_id=next_id,
        mutations=mutations,
        custom_judge_names=custom_judge_names,
    )
    return replace(experiment, round_index=round_index, operator=queued.provenance())


__all__ = [
    "mint_operator_experiment",
    "minted_operator_rounds",
    "parse_operator_experiment",
    "patch_documents",
    "queued_patch_body",
]
