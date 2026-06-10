"""Best-of-N sampling + a self-critique pass over the proposer step.

FUNCTIONALITY-RECOMMENDATIONS.md §4.1 — the top proposal-quality lever. Today
the proposer takes ONE sample, and the retry loop only fires on *invalid*
output: a valid-but-mediocre proposal is never reconsidered. This module wraps
any :class:`~zicato.proposer.agent.ProposerAgent` so that, per propose-step,
it samples ``best_of_n`` candidate experiments and then a cheap self-critique
pass picks (or, by heuristic, selects) the best against a quality bar —
grounded in a tool call? targets a real failure mode? minimal diff?

The DEFAULT is byte-identical to today: ``best_of_n == 1`` short-circuits to a
single inner ``propose`` call with NO critique and NO extra work
(:meth:`BestOfNProposerAgent.propose`). The wrapper is only interposed when an
operator opts in (``proposer_quality.best_of_n > 1`` in ``scoring.json``).

Overfitting discipline (LOAD-BEARING)
-------------------------------------
The self-critique pass sees ONLY the SAME restricted prompt context the
proposer itself sees — the train-slice patterns (aggregated when
``restrict_visibility``), the banded experiment memory, the bucketed
failure-mode profile — assembled by the same
:func:`~zicato.proposer.prompts.render_user_prompt` renderer under the same
``restrict_visibility`` flag. It NEVER sees the holdout and never sees a
per-entry identity the proposer is not already allowed to see. The critic is
inside the same overfitting-visibility envelope as the proposer
(OVERFITTING.md §11); it cannot widen what the proposer learns about the
board. The candidates it ranks are the proposer's own outputs, which are
already inside that envelope.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from zicato.core.types import Experiment, ProposerQualityConfig
from zicato.proposer.agent import ProposerAgent, ProposerContext
from zicato.proposer.prompts import render_user_prompt
from zicato.proposer.proposer import ProposerError

log = logging.getLogger("zicato.proposer.best_of_n")


def _diff_size(experiment: Experiment) -> int:
    """A cheap proxy for the description-length / diff size of an experiment.

    Counts the total characters of replacement content plus a small constant
    per patch, so a parsimony tie-break prefers the SMALLER edit (MDL /
    OVERFITTING.md §5: a shorter-description edit provably overfits the board
    less). A ``set_numeric`` / ``set_enum`` patch has no ``new_content`` but
    still counts its per-patch constant.
    """
    total = 0
    for patch in experiment.patches:
        total += 16  # per-patch constant so an extra patch is never "free"
        if patch.new_content is not None:
            total += len(patch.new_content)
    return total


def _targets_observed_failure(experiment: Experiment, ctx: ProposerContext) -> bool:
    """Whether the experiment touches a mutation id an observed pattern flags.

    A candidate that targets a mutation point the detector patterns name as
    *affected* is grounded in a real, observed failure mode rather than a
    speculative edit — the §4.1 quality bar ("targets a real failure mode?").
    Uses only the restricted, train-slice patterns already on the context, so
    the heuristic stays inside the overfitting-visibility envelope.
    """
    affected: set[str] = set()
    for pattern in ctx.patterns:
        affected.update(pattern.affected_mutation_ids)
    if not affected:
        return False
    touched = {p.mutation_id for p in experiment.patches}
    touched.update(experiment.hypothesis.modulating)
    return bool(touched & affected)


def _heuristic_best_index(candidates: list[Experiment], ctx: ProposerContext) -> int:
    """Deterministically select the best candidate by the §4.1 quality bar.

    The no-LLM selection (used when critique is disabled, or as the fallback
    when the critique call fails / is unparseable). Ranks by, in order:

    1. **Grounded in an observed failure mode** — a candidate that touches a
       pattern-flagged mutation id beats one that does not.
    2. **Minimal diff** — among equally-grounded candidates, the smaller
       edit wins (MDL parsimony; OVERFITTING.md §5).
    3. **Stable order** — ties break toward the earlier-sampled candidate, so
       the selection is deterministic for a fixed slate.

    Returns the index into ``candidates``. ``candidates`` is non-empty by
    contract (the caller handles the empty slate).
    """
    best_index = 0
    best_key = (
        not _targets_observed_failure(candidates[0], ctx),  # False (grounded) sorts first
        _diff_size(candidates[0]),
        0,
    )
    for i in range(1, len(candidates)):
        key = (
            not _targets_observed_failure(candidates[i], ctx),
            _diff_size(candidates[i]),
            i,
        )
        if key < best_key:
            best_key = key
            best_index = i
    return best_index


def _render_candidate_slate(candidates: list[Experiment]) -> str:
    """Render the sampled candidates compactly for the critic's user prompt.

    One block per candidate: its 0-based index, the hypothesis core idea, the
    targeted mutation ids, a per-patch op + rationale summary, and the diff
    size. Deliberately compact — the critic ranks the candidates, it does not
    re-derive them — and carries no per-entry board identity (only the
    proposer's own declared targets + rationale, already inside the
    visibility envelope).
    """
    blocks: list[str] = []
    for i, exp in enumerate(candidates):
        hyp = exp.hypothesis
        ids = ", ".join(hyp.modulating) if hyp.modulating else "—"
        patch_lines = [
            f"    - {p.op} {p.mutation_id}: {p.rationale or '(no rationale)'}" for p in exp.patches
        ]
        patch_block = "\n".join(patch_lines) if patch_lines else "    (no patches)"
        blocks.append(
            f"### Candidate {i}\n"
            f"core_idea: {hyp.core_idea}\n"
            f"targets: [{ids}]\n"
            f"diff_size: {_diff_size(exp)} chars across {len(exp.patches)} patch(es)\n"
            f"patches:\n{patch_block}"
        )
    return "\n\n".join(blocks)


_CRITIC_SYSTEM_PROMPT = (
    "You are a strict reviewer selecting the single best improvement proposal "
    "from a slate of candidates for a multi-agent system. You see ONLY the "
    "same restricted, board-anonymized context the proposer saw — never any "
    "held-out evaluation data. Judge each candidate against this quality bar:\n"
    "  1. Does it target a REAL, observed failure mode (a mutation point the "
    "observed patterns / failure-mode profile flag), not a speculative edit?\n"
    "  2. Is it the MINIMAL diff that achieves its goal (prefer a small, "
    "general edit over a large or special-cased one)?\n"
    "  3. Is its hypothesis grounded and falsifiable (a clear expected "
    "movement), not vague?\n"
    "Pick the candidate that best satisfies the bar. Respond with ONLY the "
    "integer index of the best candidate (e.g. `0`) — no prose, no JSON, no "
    "explanation."
)


def _parse_critic_choice(response: str, n: int) -> int | None:
    """Parse the critic's chosen index out of its raw response.

    Tolerant: the critic is asked for a bare integer, but a stray fence /
    prose is salvaged by scanning for the first integer token. Returns the
    chosen index when it is in ``range(n)``, else ``None`` (the caller falls
    back to the heuristic).
    """
    import re  # noqa: PLC0415

    if not response:
        return None
    match = re.search(r"-?\d+", response)
    if match is None:
        return None
    try:
        choice = int(match.group(0))
    except ValueError:
        return None
    if 0 <= choice < n:
        return choice
    return None


@dataclass
class BestOfNProposerAgent:
    """Wrap a :class:`ProposerAgent` with best-of-N sampling + self-critique.

    Construct it around the epoch's resolved inner agent and the contract's
    :class:`~zicato.core.types.ProposerQualityConfig`. With ``best_of_n == 1``
    (the default) :meth:`propose` is a transparent pass-through to the inner
    agent — no extra sampling, no critique call — so a contract that does not
    opt in behaves byte-identically to before this wrapper existed.

    The wrapper preserves the inner agent's failure contract: when no
    candidate can be sampled it re-raises the inner
    :class:`~zicato.proposer.proposer.ProposerError`, exactly as a single
    ``propose`` would, so every call site that already handles a failed
    propose is unaffected.
    """

    inner: ProposerAgent
    config: ProposerQualityConfig

    async def propose(self, ctx: ProposerContext) -> Experiment:
        n = self.config.best_of_n
        if n <= 1:
            # Byte-identical to today: one inner sample, no critique.
            return await self.inner.propose(ctx)

        candidates: list[Experiment] = []
        last_error: ProposerError | None = None
        for _sample in range(n):
            try:
                candidates.append(await self.inner.propose(ctx))
            except ProposerError as exc:
                # A candidate the inner proposer could not produce simply
                # narrows the slate; remember the error so an all-failed
                # slate can re-raise the real failure.
                last_error = exc

        if not candidates:
            # The whole slate failed — surface the inner failure exactly as a
            # single propose would (the caller's rejected-outcome path handles
            # it). ``last_error`` is set because n >= 2 means the loop ran.
            if last_error is not None:
                raise last_error
            raise ProposerError(["best-of-N produced no candidates"])  # pragma: no cover

        if len(candidates) == 1:
            return candidates[0]

        chosen = await self._select_best(candidates, ctx)
        return candidates[chosen]

    async def _select_best(self, candidates: list[Experiment], ctx: ProposerContext) -> int:
        """Return the index of the best candidate against the §4.1 quality bar.

        Runs the self-critique LLM pass when it is enabled and an auxiliary
        callable is available; otherwise (or on any critique failure) falls
        back to the deterministic :func:`_heuristic_best_index`. Either way
        the selection sees ONLY the restricted proposer context.
        """
        if not self.config.critique_enabled:
            return _heuristic_best_index(candidates, ctx)

        aux_call_llm = ctx.aux_call_llm
        if aux_call_llm is None:  # pragma: no cover — orchestrator always wires it
            return _heuristic_best_index(candidates, ctx)

        choice = await self._critique(aux_call_llm, candidates, ctx)
        if choice is None:
            # Critique failed / unparseable — fall back to the heuristic so a
            # flaky critic never blocks the step.
            return _heuristic_best_index(candidates, ctx)
        return choice

    async def _critique(
        self,
        aux_call_llm: Callable[[str, str, str], Awaitable[str]],
        candidates: list[Experiment],
        ctx: ProposerContext,
    ) -> int | None:
        """One cheap self-critique LLM call; returns the chosen index or None.

        Builds the critic's user prompt from the SAME restricted context the
        proposer saw — rendered through :func:`render_user_prompt` under the
        same ``restrict_visibility`` flag, so the patterns are aggregated and
        the experiment memory banded exactly as for the proposer — plus the
        compact candidate slate. The critic NEVER receives the holdout or any
        identity the proposer did not already see. Best-effort: a raising /
        timing-out / unparseable critic returns ``None`` and the caller falls
        back to the heuristic.
        """
        restricted_context = render_user_prompt(
            current_loss_summary=ctx.current_loss_summary,
            patterns=ctx.patterns,
            mutations=ctx.mutations,
            prior_experiments=ctx.prior_experiments,
            restrict_visibility=ctx.restrict_visibility,
            custom_judge_names=ctx.custom_judge_names or frozenset(),
            failure_profile=ctx.failure_profile,
        )
        slate = _render_candidate_slate(candidates)
        user_prompt = (
            "Select the single best candidate proposal.\n\n"
            "## Round context (the SAME restricted view the proposer saw — "
            "no held-out data)\n"
            f"{restricted_context}\n\n"
            "## Candidate proposals\n"
            f"{slate}\n\n"
            f"Respond with ONLY the integer index (0..{len(candidates) - 1}) "
            "of the best candidate."
        )
        try:
            response = await aux_call_llm(_CRITIC_SYSTEM_PROMPT, user_prompt, ctx.model)
        except Exception as exc:  # noqa: BLE001 — opaque LLM errors are common
            log.debug("best-of-N critique call failed (%s); using heuristic", exc)
            return None
        return _parse_critic_choice(response or "", len(candidates))


def wrap_with_proposer_quality(
    inner: ProposerAgent, config: ProposerQualityConfig
) -> ProposerAgent:
    """Interpose best-of-N + self-critique only when an operator opts in.

    Returns ``inner`` UNCHANGED when ``config.best_of_n <= 1`` (the default),
    so a contract that does not opt in pays nothing and behaves
    byte-identically — there is not even a wrapper object in the call path.
    Otherwise wraps ``inner`` in a :class:`BestOfNProposerAgent`. The
    orchestrator calls this once per evolve invocation, right after it builds
    the epoch's proposer agent.
    """
    if config.best_of_n <= 1:
        return inner
    return BestOfNProposerAgent(inner=inner, config=config)


__all__ = [
    "BestOfNProposerAgent",
    "wrap_with_proposer_quality",
]
