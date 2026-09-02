"""How a proposal episode ends, and the hook that can send it back.

A proposal is produced by one Foe episode
(:mod:`zicato.proposer.foe_agent`). What lives here is everything the
round needs to *talk about* that episode without knowing how it ran: the
four ways it can end, and the post-apply validation hook that decides
whether the experiment it produced is usable.

The endings are a hierarchy rather than a flag, so a caller that only
handles failure keeps working: :class:`ProposerError` is the failure, and
:class:`ProposerBlocked` and :class:`ProposerExhausted` are the two
endings that are not failures but still produced no experiment. Each
carries a :class:`~zicato.core.types.ProposerEpisodeOutcome`, which is
what the round log records and what the scorecard counts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from zicato.core.types import (
    PROPOSER_BLOCKED_CODES,
    Experiment,
    ProposerBlockedCode,
    ProposerEpisodeOutcome,
)

#: An optional post-parse validation hook. The proposer calls it with a
#: fully-parsed, forbidden-id-clean :class:`Experiment` and expects back
#: a list of human-readable error strings — empty when the experiment is
#: acceptable. A non-empty list fails the proposal: the episode has
#: already ended, so its findings are raised as a
#: :class:`ProposerError` and the round decides what to do with the
#: candidate. Findings the episode could still have acted on are reported
#: earlier, by the ``validate_patches`` host tool inside it. The orchestrator
#: supplies a hook that applies the patch set to a child snapshot and
#: runs :func:`zicato.mutation.validator.validate_post_apply`, so a
#: destructive patch (a dropped import, a vanished marker) costs one
#: retry instead of a wasted tournament round.
ExperimentValidator = Callable[[Experiment], Awaitable[list[str]]]


class ProposerError(RuntimeError):
    """Raised when the proposer fails to produce a schema-valid Experiment.

    Carries the accumulated list of attempt failures so the operator
    (or the calling CLI) can render a sensible error message rather
    than a one-line "the proposer gave up". The :attr:`attempts` field
    is the per-attempt error message in call order; the human-readable
    rendering joins them.

    This is the **failed** outcome of the four a proposal episode can
    reach: a crash or a protocol failure, something defective. The other
    two non-completing outcomes are :class:`ProposerBlocked` and
    :class:`ProposerExhausted`, which derive from this class so that every
    caller written against the one exception still degrades a round the
    same way, while a caller that wants to route on the ending reads
    :attr:`outcome`.
    """

    def __init__(self, attempts: list[str]) -> None:
        self.attempts = list(attempts)
        joined = "\n".join(f"  attempt {i + 1}: {msg}" for i, msg in enumerate(self.attempts))
        super().__init__(f"proposer failed after {len(self.attempts)} attempt(s):\n{joined}")

    @property
    def outcome(self) -> ProposerEpisodeOutcome:
        """How the episode ended, for the round log and the scorecard."""
        return ProposerEpisodeOutcome(kind="failed", message=self._last_attempt())

    def _last_attempt(self) -> str:
        return self.attempts[-1] if self.attempts else ""


class ProposerBlocked(ProposerError):
    """The proposer recognized that it cannot produce an experiment.

    A block carries information a failure does not: it names what the
    proposer found impossible, which is a signal about the mutation
    surface, the brief, or the budget rather than about a defect.
    :attr:`code` is one of
    :data:`~zicato.core.types.PROPOSER_BLOCKED_CODES`, so a round routes
    on it and the scorecard counts it by cause.

    The message is subject to the same redaction as every other
    proposer-facing string: no board-entry id, no entry text.
    """

    def __init__(self, code: ProposerBlockedCode, message: str = "") -> None:
        if code not in PROPOSER_BLOCKED_CODES:
            raise ValueError(
                f"{code!r} is not a proposer blocked code; the vocabulary is closed "
                f"({', '.join(sorted(PROPOSER_BLOCKED_CODES))})"
            )
        self.code: str = code
        self.message: str = message
        super().__init__([f"blocked ({code}): {message}" if message else f"blocked ({code})"])

    @property
    def outcome(self) -> ProposerEpisodeOutcome:
        return ProposerEpisodeOutcome(kind="blocked", code=self.code, message=self.message)


class ProposerExhausted(ProposerError):
    """The episode's budget ended it with work still in progress.

    Neither a block — the proposer did not find the task impossible — nor
    a failure: nothing is defective. :attr:`limit` names the budget
    dimension that ran out, one of
    :data:`~zicato.core.types.PROPOSER_BUDGET_DIMENSIONS`, so the remedy
    is the allowance rather than the brief or the code.
    """

    def __init__(self, limit: str, message: str = "") -> None:
        self.limit = limit
        self.message: str = message
        super().__init__([f"exhausted ({limit})"])

    @property
    def outcome(self) -> ProposerEpisodeOutcome:
        return ProposerEpisodeOutcome(kind="exhausted", code=self.limit, message=self.message)


__all__ = [
    "ExperimentValidator",
    "ProposerBlocked",
    "ProposerError",
    "ProposerExhausted",
]
