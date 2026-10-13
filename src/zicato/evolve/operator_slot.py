"""Mint the candidates of a queued operator round as the round's first challengers.

``zicato proposer submit`` queues the operator's edits relative to the
generation they copied (``based_on``). A promotion can happen between the
submit and the round, so this module never applies those edits to the
current champion directly. It rebuilds the operator's full tree from
``based_on``, then reads that tree back against the current champion with
:func:`~zicato.proposer.foe_scratch.project_working_copy`, the same
projection a proposal episode's working copy goes through.

Each projected patch replaces the whole content of one mutation point, and
neither the operator nor a proposer can add files or change file modes. So
the projection reproduces the operator's tree byte for byte, and its parent is the
current champion. When the champion moved, the patches also take back the
promoted change at each point where the operator's tree differs from it.
The experiment's lineage parent is always the round's champion, which keeps
every field one parent wide for recovery. The provenance names those
points as ``reverted_points``, so the record shows each promoted change that
the candidate takes back.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from zicato.core.types import Experiment, MutationPoint, Patch
from zicato.epoch.journal import read_epoch_experiment_bodies
from zicato.mutation.policy import MutationPolicy
from zicato.proposer.foe_scratch import (
    PROJECTED_RATIONALE,
    EditOutsideMutationPointError,
    project_working_copy,
)
from zicato.proposer.structured import ExperimentParseError, parse_experiment_json
from zicato.runtime.operator_queue import OperatorGeneration, QueuedCandidate, QueuedRound


class OperatorCandidateRejected(Exception):
    """A queued operator candidate cannot enter this round."""

    def __init__(self, findings: Sequence[str]) -> None:
        self.findings = tuple(findings)
        super().__init__("; ".join(self.findings))


def operator_generations(
    workspace_root: Path, epoch_id: str
) -> tuple[dict[str, list[OperatorGeneration]], list[str]]:
    """Map each queue id minted in this epoch to its generations, in id order.

    The second value lists the experiment records that could not be read.
    A generation behind such a record is missing from the map, so a caller
    that reports a round's state must say that its records were incomplete.
    """
    bodies, errors = read_epoch_experiment_bodies(workspace_root, epoch_id)
    minted: dict[str, list[OperatorGeneration]] = {}
    for generation_id, body in bodies:
        operator = body.get("operator")
        if not isinstance(operator, dict) or not isinstance(operator.get("queue_id"), str):
            continue
        outcome = body.get("outcome")
        result = outcome if isinstance(outcome, dict) else {}
        minted.setdefault(operator["queue_id"], []).append(
            OperatorGeneration(
                generation_id=generation_id,
                settled=isinstance(outcome, dict),
                based_on=str(operator.get("based_on") or ""),
                decision=result.get("tournament_decision"),
                rejection_reason=str(result.get("rejection_reason") or ""),
                reverted_points=tuple(operator.get("reverted_points") or ()),
            )
        )
    return minted, list(errors)


def queued_patch_body(patch: Patch) -> dict[str, Any]:
    """The queue form of one patch: its target and payload, without id or rationale."""
    return {
        "mutation_id": patch.mutation_id,
        "op": patch.op,
        "new_content": patch.new_content,
        "new_numeric": patch.new_numeric,
        "new_enum": patch.new_enum,
    }


def _queued_patch(raw: Mapping[str, Any]) -> Patch:
    return Patch(
        id=f"operator-{raw['mutation_id']}",
        mutation_id=str(raw["mutation_id"]),
        op=raw["op"],
        new_content=raw["new_content"],
        new_numeric=raw["new_numeric"],
        new_enum=raw["new_enum"],
        rationale=PROJECTED_RATIONALE,
    )


def patch_document(patch: Patch, rationale: str) -> dict[str, Any]:
    """One patch in the proposer response schema, with ``rationale``."""
    document: dict[str, Any] = {
        "mutation_id": patch.mutation_id,
        "op": patch.op,
        "rationale": rationale,
    }
    if patch.op == "replace":
        document["new_content"] = patch.new_content
    elif patch.op == "set_numeric":
        document["new_numeric"] = patch.new_numeric
    else:
        document["new_enum"] = patch.new_enum
    return document


def operator_rationale(hypothesis: Mapping[str, Any]) -> str:
    """The rationale every operator patch carries: the core idea, capped.

    The Foe proposer stamps its episode's core idea onto its patches in the
    same way (``_episode_rationale`` in :mod:`zicato.proposer.foe_agent`).
    """
    from zicato.proposer.foe_agent import PATCH_RATIONALE_CAP  # noqa: PLC0415

    core_idea = str(hypothesis.get("core_idea", ""))
    return " ".join(core_idea.split())[:PATCH_RATIONALE_CAP] or PROJECTED_RATIONALE


def parse_operator_experiment(
    hypothesis: Mapping[str, Any],
    patches: Sequence[Patch],
    *,
    epoch_id: str,
    parent_id: str,
    next_id: str,
    mutations: Sequence[MutationPoint],
    custom_judge_names: frozenset[str],
) -> Experiment:
    """Parse an operator hypothesis and patches with the proposer's own parser.

    Raises :class:`~zicato.proposer.structured.ExperimentParseError` with the
    message a proposer would get.
    """
    rationale = operator_rationale(hypothesis)
    response = json.dumps(
        {
            "hypothesis": dict(hypothesis),
            "patches": [patch_document(patch, rationale) for patch in patches],
        }
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
    genstore: Any,
    epoch_id: str,
    parent_id: str,
    next_id: str,
    round_index: int,
    queued: QueuedRound,
    candidate: QueuedCandidate,
    policy: MutationPolicy,
    mutations: Sequence[MutationPoint],
    custom_judge_names: frozenset[str],
    adapter: Any,
) -> Experiment:
    """The experiment for ``candidate``, as a child of the champion ``parent_id``.

    ``policy`` is the round's mutation policy for the champion. The edits are
    applied to ``based_on`` under the same adapter's mutable trees
    (``adapter``), so the round reads the points that ``submit`` read. Raises
    :class:`OperatorCandidateRejected` with the findings when the queued
    edits do not apply to ``based_on``, when the operator's tree cannot be
    read back under the round's policy, when it equals the champion, or when
    the hypothesis does not parse. The caller still runs the shared
    post-apply validator on the result.
    """
    from zicato.evolve.generation_phase import mutable_trees  # noqa: PLC0415

    edits = [_queued_patch(raw) for raw in candidate.patches]
    with tempfile.TemporaryDirectory(prefix="ztw-operator-") as temporary:
        tree = Path(temporary) / "tree"
        try:
            based_on_root = genstore.materialize_snapshot(epoch_id, candidate.based_on)
            genstore.derive_scratch(
                epoch_id,
                candidate.based_on,
                edits,
                tree,
                enumeration_roots=mutable_trees(adapter, based_on_root),
            )
        # A store failure (an OSError, or a git command's RuntimeError) is as
        # final for this candidate as a patch that does not apply: an error
        # that escaped here would fail every round that takes the item.
        except (OSError, RuntimeError, KeyError, TypeError, ValueError) as exc:
            raise OperatorCandidateRejected(
                [f"the queued edits do not apply to {candidate.based_on}: {exc}"]
            ) from exc
        try:
            projected = project_working_copy(policy, tree)
        except EditOutsideMutationPointError as exc:
            raise OperatorCandidateRejected(exc.findings) from exc
    if not projected:
        raise OperatorCandidateRejected(
            [f"the operator version is identical to the champion {parent_id}"]
        )
    try:
        experiment = parse_operator_experiment(
            candidate.hypothesis,
            projected,
            epoch_id=epoch_id,
            parent_id=parent_id,
            next_id=next_id,
            mutations=mutations,
            custom_judge_names=custom_judge_names,
        )
    except ExperimentParseError as exc:
        raise OperatorCandidateRejected([str(exc)]) from exc
    edited = {str(raw["mutation_id"]) for raw in candidate.patches}
    reverted = sorted(patch.mutation_id for patch in projected if patch.mutation_id not in edited)
    return replace(
        experiment,
        round_index=round_index,
        operator=queued.provenance(candidate, reverted_points=reverted),
    )


__all__ = [
    "OperatorCandidateRejected",
    "mint_operator_experiment",
    "operator_generations",
    "operator_rationale",
    "parse_operator_experiment",
    "patch_document",
    "queued_patch_body",
]
