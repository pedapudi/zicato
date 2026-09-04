"""Judge test-retest reliability — how self-consistent is each judge?

A process judge folds into the loss as a ``custom:<judge_name>`` drift
count, weighted by ``ScoringWeights.per_judge_weights``. A judge that
disagrees with ITSELF — different verdicts for byte-identical input —
injects pure noise into every scalar it touches, and the operator has no
way to see that from a single run. This module measures it directly with
the psychometric **test–retest** protocol: judge the SAME frozen
transcript ``k`` times and report the disagreement rate of the resulting
drift-emission verdicts.

The measurement runs entirely OUTSIDE a tournament (no agent runs, no
scoring): a live goldfive judge is built from the board's declarative
:class:`~zicato.core.JudgeSpec` through the SAME builder every real run
uses (:func:`zicato.judge_runtime.judge_spec_to_goldfive`), then its
``evaluate`` is called ``k`` times with one frozen
:class:`goldfive.judges.JudgeContext`.

The ``aux_call_llm`` parameter is **the endpoint seam**: it is zicato's
standard ``CallLLM`` callable ``(system, user, model) -> str``, exactly
what :meth:`zicato.core.RuntimeConfig.effective_judge_call_llm` returns.
Tests script it (a deterministic or flip-flopping double); a REAL
evaluation endpoint slots in unchanged later (endpoint-gated — this
module never chooses or contacts an endpoint itself).

Disagreement is scored pairwise (:func:`pairwise_disagreement`): the
fraction of unordered pairs among the ``k`` re-judgements whose
``drift_emitted`` flags differ. A deterministic judge scores ``0.0``; a
coin-flip judge tends to ``~0.5``; a strict alternator at ``k=2`` scores
``1.0``. The matching health finding is
:func:`zicato.health.diagnostics.detect_noisy_judge` (WARNING above
threshold, recommend-only), whose recommendation points at
``per_judge_weights`` — the contract's routing knob for exactly this
signal (:mod:`zicato.scoring.builtins`).

Surface: ``zicato board judges --test-retest`` (see
:mod:`zicato.cli.commands.board`), which runs the protocol over the
board's declared judges against a canned transcript — a settled
transcript file from a prior run, or the synthetic
:data:`FIXTURE_TRANSCRIPT`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from zicato.judge_runtime.builder import judge_spec_to_goldfive

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from collections.abc import Awaitable, Callable

    AuxCallLLM = Callable[[str, str, str], Awaitable[str]]

#: Default number of re-judgements per judge. Two is the minimum that can
#: disagree; three catches an alternator without tripling the endpoint
#: bill. Operators probing a genuinely stochastic judge can raise it.
DEFAULT_RETEST_K: int = 3

#: Pairwise disagreement rate above which a judge counts as *noisy* (the
#: ``noisy_judge`` health finding fires). ``0.25`` tolerates one stray
#: flip in a k=8 probe while flagging anything that flips a third of its
#: pairs. Mirrored by ``zicato.health.diagnostics.NOISY_JUDGE_DISAGREEMENT``.
NOISY_JUDGE_DISAGREEMENT_THRESHOLD: float = 0.25

#: The synthetic fixture transcript ``zicato board judges --test-retest``
#: falls back to when the operator supplies no settled transcript. A
#: neutral, plausibly-judgeable reasoning trace: it mentions planning,
#: verification, citations, and brevity so common criteria have SOMETHING
#: to grip, without baiting any particular verdict.
FIXTURE_TRANSCRIPT: str = (
    "Plan: restate the operator's request in one line, verify each factual "
    "claim against the provided sources before using it, then draft a "
    "concise reply that cites those sources inline. I will skip any metric "
    "I cannot trace to an input document and close with a one-sentence "
    "summary of what was done."
)


@dataclass(frozen=True, slots=True)
class JudgeReliability:
    """Test–retest measurement for ONE judge over one frozen transcript.

    Fields
    ------
    judge_name:
        The judge's stable name — the same name ``custom:<judge_name>``
        drift attribution and ``per_judge_weights`` route on.
    k:
        How many times the frozen transcript was re-judged — calls
        ATTEMPTED, so ``k == len(verdicts) + errors``.
    fired:
        How many of the returned verdicts emitted drift.
    verdicts:
        The ``drift_emitted`` flags of the calls that ANSWERED, in call
        order. Shorter than ``k`` when a call raised.
    disagreement_rate:
        :func:`pairwise_disagreement` over the verdicts — ``0.0`` for a
        perfectly self-consistent judge, ``1.0`` for a k=2 alternator.
    details:
        The per-call verdict ``detail`` strings (empty for non-firing
        calls), kept for the operator's post-mortem.
    errors:
        How many of the ``k`` calls RAISED instead of returning a verdict
        (issue #121). Both judge kinds swallow their callable's exception
        by hard contract and return an empty verdict, so without this
        count a judge whose endpoint 404s on every call reports ``fired
        0/k`` at ``disagreement_rate 0.0`` — indistinguishable from, and
        flattering relative to, a healthy judge that consistently found
        no violation. ``errors == k`` means the probe measured nothing.

    Note that ``disagreement_rate`` is computed over the calls that
    ANSWERED: a judge that raised on some calls is not thereby
    "inconsistent", and pairing a real verdict against a non-verdict
    would manufacture disagreement out of an outage.
    """

    judge_name: str
    k: int
    fired: int
    verdicts: tuple[bool, ...]
    disagreement_rate: float
    details: tuple[str, ...]
    errors: int = 0

    def to_json(self) -> dict[str, Any]:
        """A JSON-friendly dict (the shape the health detector reads)."""
        return {
            "judge_name": self.judge_name,
            "k": self.k,
            "fired": self.fired,
            "verdicts": list(self.verdicts),
            "disagreement_rate": self.disagreement_rate,
            "details": list(self.details),
            "errors": self.errors,
        }


def pairwise_disagreement(fired: int, k: int) -> float:
    """Fraction of unordered verdict pairs that disagree on drift emission.

    Pure. With ``fired`` of ``k`` re-judgements emitting drift, exactly
    ``fired * (k - fired)`` of the ``k*(k-1)/2`` unordered pairs disagree.
    ``0.0`` when all verdicts agree (or fewer than two verdicts exist);
    ``1.0`` only for the maximally-inconsistent ``k=2`` split.
    """
    if k < 2:
        return 0.0
    pairs = k * (k - 1) / 2
    return (fired * (k - fired)) / pairs


def _freeze_context(transcript: Any) -> Any:
    """Build the ONE frozen :class:`JudgeContext` every re-judgement sees.

    Accepts an already-built ``JudgeContext`` (returned as-is — it is a
    frozen dataclass, safely shared), a single transcript string (becomes
    both ``reasoning_text`` and the one-element ``transcript`` tuple), or
    a sequence of turn strings (the last turn is the reasoning under
    judgement, mirroring how the steerer hands judges the block just
    emitted plus the recent window).
    """
    from goldfive.judges import JudgeContext  # noqa: PLC0415

    if isinstance(transcript, JudgeContext):
        return transcript
    if isinstance(transcript, str):
        return JudgeContext(reasoning_text=transcript, transcript=(transcript,))
    turns = tuple(str(t) for t in transcript)
    return JudgeContext(reasoning_text=turns[-1] if turns else "", transcript=turns)


async def test_retest(
    judge: Any,
    transcript: Any,
    aux_call_llm: AuxCallLLM,
    k: int = DEFAULT_RETEST_K,
) -> JudgeReliability:
    """Judge the SAME frozen transcript ``k`` times; measure self-agreement.

    Parameters
    ----------
    judge:
        A declarative :class:`~zicato.judge_runtime.JudgeSpecLike` (built
        into a live judge via :func:`judge_spec_to_goldfive` with
        ``aux_call_llm`` — the path every real run takes), or an
        already-live goldfive Judge (anything exposing an async
        ``evaluate``; ``aux_call_llm`` is then unused).
    transcript:
        The frozen input — see :func:`_freeze_context`. The SAME context
        object is passed to every call; any verdict variance is the
        judge's own.
    aux_call_llm:
        The judge endpoint callable, zicato's ``CallLLM`` shape. Scripted
        in tests; a real evaluation endpoint
        (:meth:`RuntimeConfig.effective_judge_call_llm`) slots in
        unchanged.
    k:
        Number of re-judgements (>= 2).

    Notes
    -----
    The drift-emission flag is the quantity compared — it is the bit that
    becomes (or does not become) a ``custom:<judge_name>``
    :class:`DriftCount` on a real run's loss, so its test–retest delta is
    exactly the noise the judge injects into the scalar. Calls are
    sequential (not gathered) so a stateful scripted double sees a
    deterministic call order.
    """
    if k < 2:
        raise ValueError(f"test-retest needs at least 2 judgements, got {k!r}")
    live = judge if callable(getattr(judge, "evaluate", None)) else None
    if live is None:
        live = judge_spec_to_goldfive(judge, aux_call_llm)

    ctx = _freeze_context(transcript)
    verdicts: list[bool] = []
    details: list[str] = []
    errors = 0
    for _ in range(k):
        verdict = await live.evaluate(ctx)
        # A call that RAISED is counted rather than scored. zicato's judge boundary
        # swallows the exception and hands back an errored verdict; treating
        # its empty drift flag as a verdict would report a broken endpoint as
        # a perfectly self-consistent judge (issue #121).
        if getattr(verdict, "errored", False):
            errors += 1
            details.append(str(getattr(verdict, "error", "") or ""))
            continue
        verdicts.append(bool(getattr(verdict, "drift_emitted", False)))
        details.append(str(getattr(verdict, "detail", "") or ""))
    fired = sum(verdicts)
    return JudgeReliability(
        judge_name=str(getattr(live, "name", "") or ""),
        k=k,
        fired=fired,
        verdicts=tuple(verdicts),
        disagreement_rate=pairwise_disagreement(fired, len(verdicts)),
        details=tuple(details),
        errors=errors,
    )


def declared_judge_specs(board_entries: Iterable[Any]) -> list[Any]:
    """The board's declared judges, unique by name, in first-seen order.

    A judge name is one attribution stream (``custom:<name>``) and one
    ``per_judge_weights`` key no matter how many entries declare it, so
    reliability is measured once per NAME; the first declaration wins
    (re-declarations of the same name are conventionally identical).
    """
    seen: set[str] = set()
    specs: list[Any] = []
    for entry in board_entries:
        for spec in getattr(entry, "judges", ()) or ():
            name = str(getattr(spec, "name", "") or "")
            if not name or name in seen:
                continue
            seen.add(name)
            specs.append(spec)
    return specs


async def test_retest_board(
    board_entries: Sequence[Any],
    transcript: Any,
    aux_call_llm: AuxCallLLM,
    k: int = DEFAULT_RETEST_K,
) -> list[JudgeReliability]:
    """Run :func:`test_retest` over every judge the board declares.

    Returns one :class:`JudgeReliability` per unique judge name, in the
    board's first-seen order. An empty list when no entry declares a
    judge (a drift-/expectation-only board has nothing to retest).
    """
    out: list[JudgeReliability] = []
    for spec in declared_judge_specs(board_entries):
        out.append(await test_retest(spec, transcript, aux_call_llm, k=k))
    return out


__all__ = [
    "DEFAULT_RETEST_K",
    "FIXTURE_TRANSCRIPT",
    "NOISY_JUDGE_DISAGREEMENT_THRESHOLD",
    "JudgeReliability",
    "declared_judge_specs",
    "pairwise_disagreement",
    "test_retest",
    "test_retest_board",
]
