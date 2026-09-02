"""The proposer: one Foe episode per candidate, in its own process.

`FoeProposerAgent` implements the retained `ExternalProposerAgent`
protocol (:mod:`zicato.proposer.external`) and is its default and only
supported implementation. One call to :meth:`FoeProposerAgent.propose`
runs one Foe episode: Foe is the subprocess, so there is one lifecycle
authority over it and one process to cancel, and no second Python worker
wraps it.

The division of ownership is the one ``docs/design/PROPOSER.md`` states.
Zicato owns the authorized proposal context, the holdout redaction, the
mutation-point rules, the experiment validation and everything downstream
of it. Foe owns the bounded agent loop, the model call, the editing tools,
the kernel-enforced grants, the budget and the transcript. Two host tools
cross that line in the other direction: ``mutation_usage``, so an edit can
be grounded in how a value is used, and ``validate_patches``, which reads
the working copy back as a patch set and lints it.

The episode is registered in ``active_runs`` with the Foe process's own
process id before its first model request, so the supervisor watchdog can
end a wedged proposal by the same escalation it uses for a wedged
tournament worker; the wall-clock budget is also enforced here, because
the host holding the pipe is the first to notice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import foe

from zicato.core.types import (
    FOE_BLOCKED_CODES,
    Experiment,
    MutationPoint,
    ProposerSpec,
)
from zicato.proposer.foe_config import (
    FoeProposerConfig,
    ProposerConfigError,
    load_foe_proposer_config,
)
from zicato.proposer.foe_request import (
    MUTATION_USAGE_DESCRIPTION,
    SANCTIONED_TOOLS,
    VALIDATE_PATCHES_DESCRIPTION,
    ProposalEvidence,
    build_request,
    identity_contract,
)
from zicato.proposer.foe_scratch import (
    EditOutsideMutationPointError,
    changed_ranges,
    project_onto_mutation_points,
    scratch_working_copy,
)
from zicato.proposer.proposer import ProposerBlocked, ProposerError, ProposerExhausted
from zicato.proposer.structured import ExperimentParseError, parse_experiment_json

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from zicato.proposer.agent import ProposerContext
    from zicato.proposer.external import ExternalProposerConfig

log = logging.getLogger("zicato.proposer.foe")

#: How long a cancelled episode gets to settle before the host stops
#: waiting on it. Foe closes its own obligations on cancel, so this bounds
#: the pathological case rather than the ordinary one.
_CANCEL_GRACE_S = 30.0


@dataclass(frozen=True, slots=True)
class EpisodeTools:
    """The two host tools one episode is served by, with their context.

    Built per episode because both answer about *this* round's snapshot,
    manifest and working copy, while their specifications — the names,
    descriptions and schemas the model sees and the fingerprint hashes —
    are the module-level constants they are constructed from.
    """

    mutation_usage: foe.HostTool
    validate_patches: foe.HostTool

    def as_sequence(self) -> tuple[foe.HostTool, ...]:
        return (self.mutation_usage, self.validate_patches)


def build_episode_tools(
    *,
    workspace_root: Path,
    generation_root: Path,
    scratch_root: Path,
    epoch_id: str,
    generation_id: str,
    mutations: Sequence[MutationPoint],
) -> EpisodeTools:
    """The host tools for one episode, bound to that episode's context.

    ``mutation_usage`` delegates to the read-only tool registry, so the
    snapshot-containment guard and the match cap apply unchanged.
    ``validate_patches`` projects the working copy onto the declared
    mutation points and runs the existing patch linter over the result:
    the model never drafts a patch document, so the linter and the tree
    cannot describe different changes.
    """
    from zicato.proposer.tool_context import (  # noqa: PLC0415 - avoids an import cycle
        ProposerToolContext,
        bind_proposer_tool_context,
    )

    context = ProposerToolContext(
        workspace_root=workspace_root,
        generation_root=generation_root,
        epoch_id=epoch_id,
        mutations=tuple(mutations),
        generation_id=generation_id,
    )

    def usage(mutation_id: str) -> str:
        from zicato.proposer.tools import mutation_usage as read_usage  # noqa: PLC0415

        with bind_proposer_tool_context(context):
            return read_usage(mutation_id)

    def verify(candidate: dict[str, Any]) -> list[str]:
        del candidate  # the working copy is the candidate; see this module's docstring
        from zicato.proposer.validate import validate_patches as lint  # noqa: PLC0415

        try:
            patches = project_onto_mutation_points(
                changed_ranges(generation_root, scratch_root),
                mutations,
                scratch_root,
            )
        except EditOutsideMutationPointError as exc:
            return list(exc.findings)
        if not patches:
            return [
                "the working copy is unchanged; change a declared mutation point "
                "before returning, or report a block"
            ]
        with bind_proposer_tool_context(context):
            report = json.loads(lint(json.dumps([_patch_to_dict(p) for p in patches])))
        return [str(finding) for finding in report.get("errors") or ()]

    return EpisodeTools(
        mutation_usage=foe.tool(name="mutation_usage", description=MUTATION_USAGE_DESCRIPTION)(
            usage
        ),
        validate_patches=foe.tool(
            name="validate_patches", description=VALIDATE_PATCHES_DESCRIPTION
        )(verify),
    )


def _patch_to_dict(patch: Any) -> dict[str, Any]:
    """One projected patch in the shape the linter reads."""
    return {
        "mutation_id": patch.mutation_id,
        "op": patch.op,
        "new_content": patch.new_content,
        "rationale": patch.rationale,
    }


@dataclass(frozen=True)
class FoeProposerAgent:
    """The proposer, backed by one Foe episode per candidate.

    Constructed by :func:`~zicato.proposer.agent.build_proposer_agent`
    with the spec that was hashed and the configuration it was hashed
    from, so the identity in the epoch's contract and the episode that
    runs are resolved from the same inputs.
    """

    spec: ProposerSpec
    config: ExternalProposerConfig

    #: Spells the ``external:foe`` agent id in the contract canon.
    external_id = "foe"

    # -- identity ---------------------------------------------------------

    @classmethod
    def contract_identity(cls, config: ExternalProposerConfig) -> Mapping[str, Any]:
        """What decides how this proposer reasons, as Foe reports it.

        Foe already hashes the instruction text, every tool's name,
        description, instruction and schema, the permission shape, the
        budget, the termination condition, and its own version and build.
        Asking the runtime for that hash closes the gap an outside
        reconstruction cannot: a reworded description inside the runtime's
        own contributed strings moves it, and this side cannot see them.

        Computing it starts no episode, opens no socket and reads no
        credential — ``foe plan`` reads the document and the files it
        names, and nothing else — so an epoch's contract hashes on a
        machine that could never run a round.
        """
        foe_config = resolve_foe_config(config)
        tools = build_episode_tools(
            workspace_root=Path("/zicato/identity"),
            generation_root=Path("/zicato/identity/read"),
            scratch_root=Path("/zicato/identity/write"),
            epoch_id="",
            generation_id="",
            mutations=(),
        )
        contract = identity_contract(foe_config, tools.as_sequence())
        try:
            fingerprint = contract.fingerprint(foe_config.binary)
        except foe.BinaryError as exc:
            raise ProposerConfigError(
                f"proposer.binary: {foe_config.binary} could not report the "
                f"proposer's contract identity ({exc}). The epoch's contract "
                "hash folds in the Foe build that runs it, so the configured "
                "binary must be present and runnable to freeze an epoch."
            ) from exc
        return {
            "kind": "foe",
            "contract_fingerprint": fingerprint,
            "tools": list(SANCTIONED_TOOLS),
        }

    # -- proposing --------------------------------------------------------

    async def propose(self, ctx: ProposerContext) -> Experiment:
        """Run one proposal episode and return the experiment it produced."""
        config = resolve_foe_config(self.config)
        generation_root = _require_generation_root(ctx)
        workspace_root = config.workspace_root or ctx.workspace_root or generation_root

        with scratch_working_copy(generation_root) as scratch_root:
            tools = build_episode_tools(
                workspace_root=workspace_root,
                generation_root=generation_root,
                scratch_root=scratch_root,
                epoch_id=ctx.epoch_id,
                generation_id=ctx.parent_generation_id,
                mutations=ctx.mutations,
            )
            request = build_request(
                config,
                brief_text=ctx.brief_text,
                skills=self.spec.skills,
                evidence=evidence_from_context(ctx),
                host_tools=tools.as_sequence(),
                read_root=generation_root,
                write_root=scratch_root,
                verify_retries=ctx.max_retries,
            )
            outcome = await self._run_episode(ctx, config, request, workspace_root)
            experiment = self._experiment_from(ctx, outcome, scratch_root, generation_root)

        if ctx.validate_experiment is not None:
            findings = await ctx.validate_experiment(experiment)
            if findings:
                raise ProposerError(
                    ["patches failed post-apply validation: " + "; ".join(findings)]
                )
        return experiment

    async def _run_episode(
        self,
        ctx: ProposerContext,
        config: FoeProposerConfig,
        request: Any,
        workspace_root: Path,
    ) -> foe.Outcome:
        """Launch the episode, police its deadline, and return its outcome."""
        log_dir = _episode_log_dir(workspace_root, ctx)
        try:
            handle = await foe.start_config(
                request.document(),
                binary=config.binary,
                log_dir=log_dir,
                tools=request.host_tools,
            )
        except (foe.BinaryError, foe.CompatibilityError, foe.ConfigError, ValueError) as exc:
            raise ProposerError([f"the proposal episode could not start: {exc}"]) from exc

        run_id = f"propose:{ctx.epoch_id}:{ctx.new_generation_id}"
        _register_active_run(workspace_root, run_id, handle, config, ctx, log_dir)
        try:
            deadline = config.budget.seconds
            if deadline is None:
                return await handle.wait()
            try:
                return await asyncio.wait_for(handle.wait(), timeout=deadline)
            except TimeoutError:
                # The budget Foe was given is the same one; reaching here
                # means the process did not honor it, so the host that
                # holds the pipe ends it. Foe closes its own obligations
                # on cancel, so the log stays readable.
                log.warning(
                    "proposal episode for %s outlived its %ss budget; cancelling pid %s",
                    ctx.new_generation_id,
                    deadline,
                    handle.pid,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(handle.cancel(), timeout=_CANCEL_GRACE_S)
                raise ProposerExhausted(
                    "seconds", f"the episode outlived its {deadline}s budget"
                ) from None
        finally:
            _remove_active_run(workspace_root, run_id)

    def _blocked_as(
        self,
        code: str,
        message: str,
        ctx: ProposerContext,
        scratch_root: Path,
        generation_root: Path,
    ) -> ProposerBlocked:
        """The zicato code for one Foe block, refined by the working copy.

        Foe reports ``verification-unsatisfiable`` whenever the verifier's
        retries are spent, which is true of every way the copy can fail to
        become a patch set. The copy itself says which way, and the round
        wants the specific cause: an edit outside the declared surface is
        a fact about the edit, and a copy the episode never changed is a
        fact about the mutation surface or the brief. Every other Foe code
        already names its own cause and maps by the table.
        """
        if code == "verification-unsatisfiable":
            try:
                patches = project_onto_mutation_points(
                    changed_ranges(generation_root, scratch_root), ctx.mutations, scratch_root
                )
            except EditOutsideMutationPointError as exc:
                return ProposerBlocked("edit-outside-mutation-point", "; ".join(exc.findings))
            if not patches:
                return ProposerBlocked(
                    "no-groundable-mutation-point",
                    "the episode changed no declared mutation point",
                )
        return ProposerBlocked(FOE_BLOCKED_CODES.get(code, "missing-capability"), message)

    def _experiment_from(
        self,
        ctx: ProposerContext,
        outcome: foe.Outcome,
        scratch_root: Path,
        generation_root: Path,
    ) -> Experiment:
        """Turn one episode's outcome into an experiment, or into a refusal."""
        match outcome:
            case foe.Blocked(code, message):
                raise self._blocked_as(code, message, ctx, scratch_root, generation_root)
            case foe.Exhausted(limit):
                raise ProposerExhausted(limit)
            case foe.Failed(error):
                raise ProposerError([f"the proposal episode failed: {error}"])
            case foe.Completed(value):
                hypothesis = value
            case _:  # pragma: no cover - the union is closed
                raise ProposerError([f"unrecognized episode outcome {outcome!r}"])

        if not isinstance(hypothesis, dict):
            raise ProposerError(
                [f"the episode returned a {type(hypothesis).__name__}, expected a hypothesis"]
            )
        try:
            patches = project_onto_mutation_points(
                changed_ranges(generation_root, scratch_root),
                ctx.mutations,
                scratch_root,
            )
        except EditOutsideMutationPointError as exc:
            raise ProposerBlocked("edit-outside-mutation-point", "; ".join(exc.findings)) from exc
        if not patches:
            raise ProposerBlocked(
                "no-groundable-mutation-point",
                "the episode completed without changing any declared mutation point",
            )

        payload = json.dumps(
            {"hypothesis": hypothesis, "patches": [_patch_to_dict(p) for p in patches]}
        )
        try:
            return parse_experiment_json(
                payload,
                epoch_id=ctx.epoch_id,
                parent_gen=ctx.parent_generation_id,
                new_gen=ctx.new_generation_id,
                mutations_by_id={mp.id: mp for mp in ctx.mutations},
                custom_judge_names=ctx.custom_judge_names,
            )
        except ExperimentParseError as exc:
            raise ProposerError([str(exc)]) from exc


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def resolve_foe_config(config: ExternalProposerConfig) -> FoeProposerConfig:
    """The workspace's ``proposer`` block, off the resolved binding."""
    return load_foe_proposer_config(config.workspace_config, config.workspace_root)


def evidence_from_context(ctx: ProposerContext) -> ProposalEvidence:
    """The round's evidence, read off the context the orchestrator built.

    A projection rather than a re-derivation: every field is already
    assembled, banded and redacted by the caller, so this cannot widen
    what the proposer sees.
    """
    return ProposalEvidence(
        loss_summary=ctx.current_loss_summary,
        patterns=ctx.patterns,
        mutations=ctx.mutations,
        prior_experiments=ctx.prior_experiments,
        custom_judge_names=tuple(sorted(ctx.custom_judge_names or ())),
        metric_priorities=ctx.metric_priorities,
        failure_profile=ctx.failure_profile,
        process_exemplars=ctx.process_exemplars,
        genealogy=ctx.genealogy,
        calibration=ctx.calibration,
        sample_hint=ctx.sample_hint,
        revise_feedback=ctx.revise_feedback,
        restrict_visibility=ctx.restrict_visibility,
        mutation_track_records=ctx.mutation_track_records,
    )


def _require_generation_root(ctx: ProposerContext) -> Path:
    if ctx.generation_root is None:
        raise ProposerError(
            [
                "the proposal context carries no generation_root; the episode "
                "reads the parent snapshot and writes a copy of it, so there is "
                "nothing to propose against"
            ]
        )
    return ctx.generation_root


def _episode_log_dir(workspace_root: Path, ctx: ProposerContext) -> Path:
    """Where this episode's transcript lands, under the epoch's records."""
    from zicato.workspace import WorkspaceLayout  # noqa: PLC0415 - avoids an import cycle

    layout = WorkspaceLayout.from_root(workspace_root)
    slot = "" if ctx.slot_index is None else f"-{ctx.slot_index}"
    return layout.epoch_dir(ctx.epoch_id) / "episodes" / f"{ctx.new_generation_id}{slot}"


def _register_active_run(
    workspace_root: Path,
    run_id: str,
    handle: foe.Handle,
    config: FoeProposerConfig,
    ctx: ProposerContext,
    log_dir: Path,
) -> None:
    """Record the episode's own pid so the watchdog can end it.

    Best-effort by contract, like every other observability write on the
    propose path: a workspace whose runtime tree cannot be written still
    proposes, it just loses the supervisor's reach over this episode.
    """
    from zicato.runtime.state import ActiveRun, write_active_run  # noqa: PLC0415
    from zicato.util import best_effort  # noqa: PLC0415

    seconds = config.budget.seconds or 0
    started = datetime.now(UTC)
    with best_effort(
        "proposal-episode active-run record",
        on_error=lambda exc: log.debug("proposal-episode active-run record skipped: %s", exc),
    ):
        pgid: int | None = None
        with contextlib.suppress(OSError, AttributeError):
            pgid = os.getpgid(handle.pid)
        write_active_run(
            workspace_root,
            ActiveRun(
                run_id=run_id,
                pid=handle.pid,
                started_at=started.isoformat(),
                last_progress=started.isoformat(),
                wall_clock_budget_seconds=int(seconds),
                deadline=(started + timedelta(seconds=seconds)).isoformat(),
                events_jsonl_path=str(log_dir / "episode.jsonl"),
                entry_id="",
                generation_id=ctx.new_generation_id,
                epoch_id=ctx.epoch_id,
                pgid=pgid,
            ),
        )


def _remove_active_run(workspace_root: Path, run_id: str) -> None:
    from zicato.runtime.state import remove_active_run  # noqa: PLC0415
    from zicato.util import best_effort  # noqa: PLC0415

    with best_effort(
        "proposal-episode active-run cleanup",
        on_error=lambda exc: log.debug("proposal-episode active-run cleanup skipped: %s", exc),
    ):
        remove_active_run(workspace_root, run_id)


__all__ = [
    "EpisodeTools",
    "FoeProposerAgent",
    "build_episode_tools",
    "evidence_from_context",
    "resolve_foe_config",
]
