"""The one builder of a Foe proposal request, for the loop and the CLI.

A proposal request is two things: the execution contract Foe resolves —
instructions, tools, grants, budget, completion rule — and the task, which
is this round's evidence. Both the evolve round and ``zicato proposer
propose`` build theirs here, so the request an operator debugs is the
request the loop runs rather than a second stitching of the same inputs
that drifts from it.

What the contract says is fixed and hashed. What the task says is the
round's: the loss summary, the detector patterns, the mutation manifest,
the failure-mode profile, the contract's scored targets, the process
exemplars, the genealogy, the calibration record, and the experiment
memory — every block already banded and redacted by its own renderer in
:mod:`zicato.proposer.prompts`. Nothing here reads the board, and nothing
here can reach the holdout: the caller assembles the evidence from the
train slice and this module only orders it.

The completion rule pairs a returned hypothesis with a verifier. The
proposer edits a disposable copy of the parent snapshot
(:mod:`zicato.proposer.foe_scratch`), calls ``validate_patches`` to have
the copy read back as a patch set and linted, and returns the hypothesis
that explains the change. Findings go back to the model under Foe's own
retry rule, so a broken edit costs a turn rather than a tournament round.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import foe

from zicato.core.types import MutationPoint, Pattern, PriorExperiment, ProposerSkill
from zicato.proposer.foe_config import FoeProposerConfig
from zicato.proposer.prompts import (
    render_calibration_block,
    render_genealogy_block,
    render_metric_targets_block,
    render_mutation_block,
    render_pattern_block,
    render_prior_experiments_block,
    render_skills_block,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from zicato.index.query import MutationTrackRecord
    from zicato.proposer.calibration import CalibrationSummary
    from zicato.proposer.genealogy import GenealogyItem

#: The name of the contract, recorded in every episode log.
CONTRACT_NAME = "zicato-proposer"

#: What the model may do inside a proposal episode. Two built-in tools read
#: the snapshot, one writes the disposable copy, one reports a block, and
#: two host tools are answered by zicato. Widening this list widens what
#: every proposer can do, so it is asserted by name rather than counted.
SANCTIONED_TOOLS: tuple[str, ...] = (
    "read",
    "grep",
    "edit",
    "block",
    "mutation_usage",
    "validate_patches",
)

#: The model-visible description of each host tool. Held here as a
#: constant because a tool's description is part of what Foe fingerprints:
#: rewording one rolls the epoch, which is the intended reading, and doing
#: it by accident is what naming them in one place prevents.
MUTATION_USAGE_DESCRIPTION = (
    "Report where a mutation point's symbol and current value are referenced "
    "across the parent snapshot, so an edit can be grounded in how the value "
    "is actually used."
)
VALIDATE_PATCHES_DESCRIPTION = (
    "Read your working copy back as a patch set over the declared mutation "
    "points and lint it. Returns one finding per problem and nothing when the "
    "patch set is well-formed and the tree still loads."
)

#: The direction and magnitude vocabularies a predicted movement may use,
#: which are the ones the journal grades against.
_DIRECTIONS = [
    "decrease",
    "increase",
    "neutral",
    "decrease_or_neutral",
    "increase_or_neutral",
]
_MAGNITUDES = ["small", "medium", "large"]


def _movement_schema(name_key: str) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [name_key, "direction", "magnitude"],
        "properties": {
            name_key: {"type": "string"},
            "direction": {"enum": list(_DIRECTIONS)},
            "magnitude": {"enum": list(_MAGNITUDES)},
        },
    }


#: What a completed episode returns: the hypothesis, and only the
#: hypothesis. The patches are read back from the working copy, so the
#: model never restates an edit it has already made and the two can never
#: disagree. The schema is in the subset ``foe/docs/config.md`` implements.
#:
#: The ``anyOf`` states the rule
#: :func:`~zicato.proposer.structured.parse_experiment_json` enforces: a
#: hypothesis predicts at least one movement, of either kind. It is
#: written here as well as there because the runtime checks the returned
#: value at the boundary, and a hypothesis the runtime accepts and zicato
#: then rejects costs the whole episode rather than one turn.
HYPOTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["core_idea", "modulating", "why", "expected_pass_rate_delta"],
    "properties": {
        "core_idea": {"type": "string", "minLength": 1},
        "modulating": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "why": {"type": "string", "minLength": 1},
        "expected_pass_rate_delta": {"type": "string"},
        "risks": {"type": "string"},
        "expected_drift_movements": {"type": "array", "items": _movement_schema("kind")},
        "expected_metric_movements": {
            "type": "array",
            "items": _movement_schema("metric_name"),
        },
    },
    "anyOf": [
        {
            "required": ["expected_drift_movements"],
            "properties": {"expected_drift_movements": {"minItems": 1}},
        },
        {
            "required": ["expected_metric_movements"],
            "properties": {"expected_metric_movements": {"minItems": 1}},
        },
    ],
}

#: The instructions every proposal episode runs under, before the epoch's
#: own brief and skills are appended. Sections are keyed so Foe orders them
#: lexicographically and so a later section can be revised without moving
#: the others. Every word here is model-visible and hashed.
CHARTER_SECTIONS: Mapping[str, str] = {
    "10-charter": (
        "You improve a system that is measured against an evaluation board. "
        "One episode produces one experiment: a change to the system, and a "
        "falsifiable hypothesis about how that change will move the board's "
        "measurements. You never see the board's entries and you never run "
        "them; what you are given is aggregate evidence about how the current "
        "generation performs."
    ),
    "20-working-copy": (
        "You are given a disposable writable copy of the current generation's "
        "source tree, and read access to the generation itself. Both trees are "
        "named at the top of the task. Change files in the copy with the edit "
        "tool. The generation is not writable and an attempt to write it is "
        "refused. The copy is discarded when the episode ends; what survives "
        "is the patch set your changes are read back as."
    ),
    "30-mutation-points": (
        "Only the declared mutation points may change. The task lists every "
        "point with its id, its file, its line range and its current content. "
        "Each change you make must fall entirely inside one point's line "
        "range. A change anywhere else — a new file, an import, a line above "
        "or below a point — ends the episode with nothing proposed, so keep "
        "your edits inside the ranges you were given. Use mutation_usage to "
        "see how a point's value is consumed before you rewrite it."
    ),
    "40-verify": (
        "Call validate_patches when you believe the copy is ready. It reads "
        "the copy back as a patch set and lints it: shape, application, "
        "declared static checks, and an import of the harness entry point. "
        "Fix what it reports and call it again. It consumes no board data, "
        "runs no board entry and produces no score."
    ),
    "50-hypothesis": (
        "Finish by returning the hypothesis: what you changed in one "
        "sentence, the mutation-point ids you touched, why you expect the "
        "change to help, the movements you predict, and the risks you see. "
        "Predict only movements the task lists as valid targets. A hypothesis "
        "written before the measurement is what makes the result readable, so "
        "state what would falsify it rather than what would be flattering."
    ),
    "60-blocked": (
        "If no declared mutation point can address what the evidence shows, "
        "call block with goal-unreachable and say what surface would be "
        "needed. If the brief admits incompatible readings, call block with "
        "ambiguous-task. A reported block is more useful than a change made "
        "for its own sake."
    ),
}

#: The section keys the epoch's own text occupies. Named so the charter
#: above and the per-epoch text cannot collide as they are merged.
BRIEF_SECTION = "70-brief"
SKILLS_SECTION = "80-skills"

#: What a workspace with no epoch text looks like to the fingerprint. The
#: identity contract uses these so an identity can be computed without an
#: epoch: the brief and the skills are hashed separately by the contract
#: canonicalizer, so folding them here too would say the same thing twice.
_IDENTITY_BRIEF = "(the epoch's proposer brief)"
_IDENTITY_ROOT = "/zicato/identity"


@dataclass(frozen=True, slots=True)
class ProposalEvidence:
    """Training evidence for proposal and critique requests.

    Callers supply the training slice. The shared renderer projects diagnostic
    patterns and bands experiment history according to ``restrict_visibility``;
    other channels arrive as rendered blocks or aggregate values.
    """

    loss_summary: str = ""
    patterns: tuple[Pattern, ...] = ()
    mutations: tuple[MutationPoint, ...] = ()
    prior_experiments: tuple[PriorExperiment, ...] = ()
    custom_judge_names: tuple[str, ...] = ()
    metric_priorities: str = ""
    failure_profile: str = ""
    process_exemplars: str = ""
    genealogy: tuple[GenealogyItem, ...] = ()
    calibration: CalibrationSummary | None = None
    insights: str = ""
    sample_hint: str = ""
    revise_feedback: str = ""
    restrict_visibility: bool = False
    mutation_track_records: Mapping[str, MutationTrackRecord] | None = None
    #: Which candidate this episode is producing, and the generation it
    #: descends from. Lineage coordinates rather than board data: they name the
    #: episode in the round log and in its own transcript, and they are
    #: what distinguishes one challenger's episode from its siblings' when
    #: a field proposes several against the same evidence.
    candidate_id: str = ""
    parent_id: str = ""
    #: Which slot of a best-of-N slate this episode is filling, when it is
    #: filling one. The slate's slots share a candidate and are told apart
    #: only by their edit-class hint, so the slot is named for the same
    #: reason the hint is given: the slate is worth running only if its
    #: episodes explore different edits.
    slot_index: int | None = None


@dataclass(frozen=True, slots=True)
class FoeProposalRequest:
    """One proposal episode's contract and task, ready to run."""

    contract: foe.ExecutionContract
    task: str
    #: The host tools whose implementations service this episode, in the
    #: order the contract lists them.
    host_tools: tuple[foe.HostTool, ...] = ()

    def document(self) -> dict[str, Any]:
        """The complete configuration document, task included."""
        return self.contract.to_dict(self.task)


def instruction_sections(brief_text: str, skills: Sequence[ProposerSkill]) -> dict[str, str]:
    """The charter, the epoch's brief, and the proposer's skills.

    The brief and the skills are the operator's steering for this epoch
    and are already folded into the contract hash by the contract
    canonicalizer; they are here because the model must read them. The
    section keys order them: the brief is ``70-brief`` and the skills
    ``80-skills``, so the operator's goal is read before the procedures
    for reaching it.
    """
    sections = dict(CHARTER_SECTIONS)
    sections[BRIEF_SECTION] = (
        "Proposer brief for this epoch, written by the operator:\n\n"
        f"{brief_text.strip() or '(no brief)'}"
    )
    rendered_skills = render_skills_block(skills)
    if rendered_skills:
        sections[SKILLS_SECTION] = f"Operating procedures for this epoch:\n\n{rendered_skills}"
    return sections


def render_episode_block(evidence: ProposalEvidence, *, read_root: Path, write_root: Path) -> str:
    """Where this episode works, and which candidate it is producing.

    The charter tells the episode it holds a writable copy of the parent
    snapshot; this names both trees, because a grant the model cannot
    address is a grant it cannot use. The candidate and parent generation
    ids are here for the same reason the episode's log is filed under
    them: they say which point of the lineage this episode is extending,
    which is what tells one challenger's episode from its siblings' when a
    field proposes several against the same evidence.

    Nothing here is fingerprinted — the task never is — so naming a
    per-round path cannot move the proposer's contract identity.
    """
    lines = [
        f"Read-only parent snapshot: {read_root}",
        f"Your writable working copy: {write_root}",
    ]
    if evidence.candidate_id:
        slot = "" if evidence.slot_index is None else f" (slate slot {evidence.slot_index})"
        descends = f" from {evidence.parent_id}" if evidence.parent_id else ""
        lines.append(f"You are producing candidate {evidence.candidate_id}{slot}{descends}.")
    return "\n".join(lines)


def render_evidence(evidence: ProposalEvidence) -> str:
    """This round's evidence, in the order it is meant to be read.

    What to fix first, then what is already known, then what may be
    changed. Every block is omitted when it is empty, so a workspace that
    opts into none of the optional channels gets the three that are
    always present.

    Rendered here rather than inside :func:`render_task` because the
    proposal episode is not its only reader: the best-of-N critic ranks
    candidates against the same evidence, under the same restricted
    visibility, and that invariant holds by construction only while one
    function renders it.
    """
    blocks: list[str] = []
    if evidence.revise_feedback.strip():
        blocks.append(
            "## Why the previous attempt was set aside\n" f"{evidence.revise_feedback.strip()}"
        )
    if evidence.sample_hint.strip():
        blocks.append(f"## Edit-class hint (this sample)\n{evidence.sample_hint.strip()}")
    if evidence.insights.strip():
        blocks.append(f"## Recent telemetry insights\n{evidence.insights.strip()}")
    if evidence.failure_profile.strip():
        blocks.append(
            "## Failure-mode profile (this round, aggregate — train slice)\n"
            f"{evidence.failure_profile.strip()}"
        )
    if evidence.process_exemplars.strip():
        blocks.append(
            "## Process exemplars (train slice — redacted event windows)\n"
            "Entry ids and task text are stripped, task ids are anonymized per "
            "window, and model outputs are withheld. These show HOW a detected "
            "failure unfolds, never WHICH board entry it unfolded on.\n"
            f"{evidence.process_exemplars.strip()}"
        )
    genealogy = render_genealogy_block(evidence.genealogy)
    if genealogy.strip():
        blocks.append(
            "## Candidate genealogy (this reign — in-context evolution)\n"
            "Promoted ancestors to build on and diverse rejected ideas to "
            "re-frame. Outcomes are banded; diffs are the proposer's own "
            "edits, excerpted.\n"
            f"{genealogy.strip()}"
        )
    calibration = render_calibration_block(evidence.calibration)
    if calibration.strip():
        blocks.append(
            "## Prediction calibration (this reign — your own settled hypotheses)\n"
            "How your movement predictions landed against realized outcomes. "
            "Predict more honestly rather than more boldly.\n"
            f"{calibration.strip()}"
        )
    prior = render_prior_experiments_block(
        evidence.prior_experiments, restrict=evidence.restrict_visibility
    )
    if prior.strip():
        blocks.append(
            "## What's already been tried (this epoch)\n"
            "Avoid repeating failures; build on wins.\n"
            f"{prior.strip()}"
        )
    blocks.append(
        f"## Current loss summary\n{evidence.loss_summary.strip() or '(no loss summary)'}"
    )
    blocks.append(
        "## Valid expectation targets (what a predicted movement may reference)\n"
        + render_metric_targets_block(evidence.custom_judge_names, evidence.metric_priorities)
    )
    blocks.append(
        "## Patterns observed (advisory; address none, some, or all)\n"
        + render_pattern_block(evidence.patterns, restrict=evidence.restrict_visibility)
    )
    blocks.append(
        "## Mutation points (only these may change)\n"
        + render_mutation_block(evidence.mutations, track_records=evidence.mutation_track_records)
    )
    return "\n\n".join(blocks)


def rebase_mutations(
    evidence: ProposalEvidence, *, read_root: Path, write_root: Path
) -> ProposalEvidence:
    """Re-address the mutation manifest to the tree the episode may write.

    Mutation points are enumerated against the parent snapshot, so
    :attr:`MutationPoint.file` is an absolute path inside ``read_root``.
    That is the right address for everything that reads them -- but the
    manifest is also the ONLY place the episode is handed concrete file
    paths, and the charter tells it to change the working copy. Rendered
    unchanged, every path the model is given names the one tree it may not
    write: it edits the snapshot, ``edit`` refuses each call as
    out-of-grants, and the episode ends blocked with nothing proposed.

    Rebasing onto ``write_root`` makes the manifest address the copy. Ids,
    line ranges and content are untouched, and a point whose file is somehow
    not under ``read_root`` is left exactly as it is.
    """

    def _rebase(path: Path) -> Path:
        try:
            return write_root / path.relative_to(read_root)
        except ValueError:
            return path

    rebased = tuple(
        replace(mp, file=_rebase(mp.file), source_root=_rebase(mp.source_root))
        for mp in evidence.mutations
    )
    return replace(evidence, mutations=rebased)


def render_task(evidence: ProposalEvidence, *, read_root: Path, write_root: Path) -> str:
    """The one text a proposal episode is given: where it works, and why.

    The episode block comes first because it names the trees every later
    instruction refers to, and the closing line is the ask.
    """
    evidence = rebase_mutations(evidence, read_root=read_root, write_root=write_root)
    return "\n\n".join(
        [
            "## This episode\n"
            + render_episode_block(evidence, read_root=read_root, write_root=write_root),
            render_evidence(evidence),
            "Change the working copy now, verify it, and return your hypothesis.",
        ]
    )


def build_contract(
    config: FoeProposerConfig,
    *,
    instructions: Mapping[str, str],
    host_tools: Sequence[foe.HostTool],
    read_root: Path,
    write_root: Path,
    verify_retries: int,
) -> foe.ExecutionContract:
    """The execution contract one proposal episode runs.

    The read grant is the generation snapshot and the write grant is the
    disposable copy, and nothing else is reachable: Foe compiles the two
    into a kernel ruleset that every process of the episode inherits.
    """
    by_name = {t.name: t for t in host_tools}
    ordered: list[str | foe.HostTool] = [by_name.get(name, name) for name in SANCTIONED_TOOLS]
    return foe.ExecutionContract(
        name=CONTRACT_NAME,
        instructions=dict(instructions),
        tools=ordered,
        # The working copy is granted for BOTH read and write, and FIRST.
        #
        # Read, because a ``grants.write`` directory confers write, truncate,
        # create, remove, rename and link -- and no read (Foe's
        # docs/sandbox.md). The built-in ``edit`` is read-modify-write: it has
        # to see the span it is replacing. Granted write-only, the copy cannot
        # be edited at all, and ``read`` / ``grep`` cannot inspect what an
        # earlier step of the same episode already changed.
        #
        # First, because a relative path argument is taken from the first read
        # root and ``grep`` defaults its search there (Foe's docs/tools.md).
        # The episode's work happens in the copy, so a bare ``agent/agent.py``
        # has to mean the copy; anchored to the snapshot instead, every
        # relative edit is refused as out-of-grants.
        #
        # The parent snapshot stays read-only -- that is the boundary that
        # matters -- and stays reachable by absolute path for reference.
        grants=foe.Grants(read=[write_root, read_root], write=[write_root]),
        budget=foe.Budget(
            model_calls=config.budget.model_calls,
            seconds=config.budget.seconds,
            input_tokens=config.budget.input_tokens,
            output_tokens=config.budget.output_tokens,
        ),
        done_when=foe.Verified(
            verify=by_name["validate_patches"],
            retries=verify_retries,
            returns=HYPOTHESIS_SCHEMA,
        ),
        model=foe.Model(
            provider=config.model.provider,
            model=config.model.model,
            options=dict(config.model.options),
        ),
    )


def build_request(
    config: FoeProposerConfig,
    *,
    brief_text: str,
    skills: Sequence[ProposerSkill],
    evidence: ProposalEvidence,
    host_tools: Sequence[foe.HostTool],
    read_root: Path,
    write_root: Path,
    verify_retries: int = 2,
) -> FoeProposalRequest:
    """Build one proposal request — the single builder both paths use."""
    contract = build_contract(
        config,
        instructions=instruction_sections(brief_text, skills),
        host_tools=host_tools,
        read_root=read_root,
        write_root=write_root,
        verify_retries=verify_retries,
    )
    return FoeProposalRequest(
        contract=contract,
        task=render_task(evidence, read_root=read_root, write_root=write_root),
        host_tools=tuple(host_tools),
    )


def identity_contract(
    config: FoeProposerConfig, host_tools: Sequence[foe.HostTool]
) -> foe.ExecutionContract:
    """The contract Foe fingerprints to state the proposer's identity.

    Everything a round varies is replaced by a placeholder, because Foe's
    fingerprint excludes exactly those things: the task, the model route,
    and the paths in the resolved permission set, of which only the SHAPE
    is hashed. What remains is what decides how the proposer reasons — the
    charter, every tool's name, description and schema, the budget, the
    completion rule, and the runtime's own version and build.

    The epoch's brief and skills are placeholders here because the
    contract canonicalizer already hashes them; folding them in twice
    would say one thing in two places.
    """
    return build_contract(
        config,
        instructions=instruction_sections(_IDENTITY_BRIEF, ()),
        host_tools=host_tools,
        read_root=Path(_IDENTITY_ROOT) / "read",
        write_root=Path(_IDENTITY_ROOT) / "write",
        verify_retries=2,
    )


__all__ = [
    "BRIEF_SECTION",
    "CHARTER_SECTIONS",
    "CONTRACT_NAME",
    "HYPOTHESIS_SCHEMA",
    "MUTATION_USAGE_DESCRIPTION",
    "SANCTIONED_TOOLS",
    "SKILLS_SECTION",
    "VALIDATE_PATCHES_DESCRIPTION",
    "FoeProposalRequest",
    "ProposalEvidence",
    "build_contract",
    "build_request",
    "identity_contract",
    "instruction_sections",
    "render_episode_block",
    "render_evidence",
    "render_task",
]
