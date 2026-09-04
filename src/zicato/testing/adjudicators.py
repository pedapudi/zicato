"""Scripted adjudicator doubles — deterministic meta-judges for tests.

The board-reflection adjudicator
(:mod:`zicato.reflection.adjudicator`) talks to a meta-judge through the
standard ``CallLLM`` shape ``(system, user, model) -> str`` and expects a strict
JSON verdict back. Tests may not call a live endpoint, so these doubles stand
in: each is a callable that parses the adjudicator's DE-ANCHORED user prompt (the
machine-readable ``JUDGE UNDER REVIEW`` / ``DECISION REF`` header + the judge's
criterion, plus the ``<<<TRANSCRIPT … TRANSCRIPT>>>`` block — the prompt never
carries what the judge DID) and returns a deterministic verdict.

Because the prompt is de-anchored (v2), a double CANNOT know the judge's own
verdict, so the confirm/refute doubles decide BLIND — exactly the discipline a
real independent adjudicator obeys:

* :class:`AlwaysConfirm` — always ``should_fire=True`` ("the transcript exhibits
  the failure"): the correct meta-judge for a PLANTED-violation corpus (a judge
  that fired ⇒ TP, one that stayed silent ⇒ FN).
* :class:`AlwaysRefute` — always ``should_fire=False`` ("the transcript is
  clean"): the correct meta-judge for a CLEAN corpus (a judge that fired ⇒ FP,
  one that stayed silent ⇒ TN).
* :class:`SpanQuoting` — a fixed blind verdict that quotes an EXACT substring of
  the transcript it saw into ``evidence_span``, so a test can prove the
  adjudicator received the verbatim ``judge_io`` bytes (the quoted span appears
  in the sidecar's ``reasoning_text``).
* :class:`ScriptedTable` — a per-``(judge_name, run_ref)`` verdict table for
  hand-built oracle corpora — the way to script DIFFERENT verdicts per
  decision, given that the prompt reveals nothing of the judge's action.
* :class:`MalformedThenValid` — returns garbage on its first call and valid JSON
  thereafter, exercising the adjudicator's one-retry-then-ambiguous path.

Every double counts its calls (``.calls``) so a test can assert the
cache-idempotency invariant (second pass ⇒ zero adjudicator calls) and records
each user prompt it saw (``.prompts``) so a test can assert the retry appended
its corrective suffix. All quote a verbatim span so any of them can ground a
finding.
"""

from __future__ import annotations

import json

# The adjudicator protocol markers + JSON verdict shape, inlined so these test
# doubles carry NO import edge into :mod:`zicato.reflection`. ``zicato.testing``
# is a source module of the modelling-and-execution import contract, whose
# forbidden list names :mod:`zicato.reflection`; a real import here would red
# it. They mirror the constants of the same names in
# :mod:`zicato.reflection.adjudicator`; a consistency test in
# ``tests/test_reflection_adjudicator.py`` pins them equal so neither can drift.
OBSERVED_FIRED = "fired"
OBSERVED_SILENT = "silent"
TRANSCRIPT_OPEN = "<<<TRANSCRIPT"
TRANSCRIPT_CLOSE = "TRANSCRIPT>>>"
#: The strict-JSON verdict keys every double emits + the severity vocabulary it
#: draws from — pinned equal to production so the protocol shape cannot drift.
VERDICT_JSON_KEYS = ("should_fire", "severity", "evidence_span", "rationale")
SEVERITY_VOCAB = ("none", "info", "warning", "critical")


def _parse_header(user: str) -> dict[str, str]:
    """Extract ``judge_name`` / ``run_ref`` from the de-anchored user prompt.

    The de-anchored prompt carries no ``THE JUDGE OBSERVED`` line, so the
    doubles cannot — and must not — read the judge's action from it.
    """
    judge_name = ""
    run_ref = ""
    for line in user.splitlines():
        if line.startswith("JUDGE UNDER REVIEW:"):
            judge_name = line.split(":", 1)[1].strip()
        elif line.startswith("DECISION REF:"):
            run_ref = line.split(":", 1)[1].strip()
    return {"judge_name": judge_name, "run_ref": run_ref}


def _transcript_text(user: str) -> str:
    """The verbatim transcript slice between the prompt's transcript delimiters."""
    start = user.find(TRANSCRIPT_OPEN)
    end = user.find(TRANSCRIPT_CLOSE)
    if start < 0 or end <= start:
        return ""
    return user[start + len(TRANSCRIPT_OPEN) : end].strip()


def _verdict_json(*, should_fire: bool, severity: str, evidence_span: str, rationale: str) -> str:
    """Serialize a strict-JSON adjudicator verdict."""
    return json.dumps(
        {
            "should_fire": should_fire,
            "severity": severity,
            "evidence_span": evidence_span,
            "rationale": rationale,
        }
    )


class _BaseDouble:
    """Shared call-counting + prompt-recording + span-quoting for the doubles."""

    def __init__(self, *, severity: str = "warning", quote_len: int = 120) -> None:
        self.severity = severity
        self.quote_len = quote_len
        self.calls = 0
        self.last_transcript = ""
        self.prompts: list[str] = []

    def _span(self, user: str) -> str:
        self.prompts.append(user)
        text = _transcript_text(user)
        self.last_transcript = text
        return text[: self.quote_len]


class AlwaysConfirm(_BaseDouble):
    """Blind ``should_fire=True`` — "the transcript exhibits the failure".

    The correct meta-judge for a PLANTED-violation corpus: a judge that fired ⇒
    TP, one that stayed silent ⇒ FN. It cannot (and does not) read the judge's
    action — the de-anchored prompt withholds it.
    """

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.calls += 1
        return _verdict_json(
            should_fire=True,
            severity=self.severity,
            evidence_span=self._span(user),
            rationale="the transcript exhibits the failure this judge guards",
        )


class AlwaysRefute(_BaseDouble):
    """Blind ``should_fire=False`` — "the transcript is clean".

    The correct meta-judge for a CLEAN corpus: a judge that fired ⇒ FP, one that
    stayed silent ⇒ TN. Blind by construction — the de-anchored prompt never
    reveals what the judge did.
    """

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.calls += 1
        return _verdict_json(
            should_fire=False,
            severity=self.severity,
            evidence_span=self._span(user),
            rationale="the transcript is clean; the guarded failure is absent",
        )


class SpanQuoting(_BaseDouble):
    """Quotes an exact transcript substring — proves the verbatim path.

    ``should_fire`` is fixed (default ``True`` — "the transcript exhibits the
    failure"); ``evidence_span`` is a verbatim slice of the transcript the
    adjudicator was handed, so a test can assert it appears in the ``judge_io``
    sidecar's ``reasoning_text``.
    """

    def __init__(
        self, *, should_fire: bool = True, severity: str = "warning", quote_len: int = 120
    ) -> None:
        super().__init__(severity=severity, quote_len=quote_len)
        self.should_fire = should_fire

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.calls += 1
        return _verdict_json(
            should_fire=self.should_fire,
            severity=self.severity,
            evidence_span=self._span(user),
            rationale="quoting the transcript span verbatim",
        )


class ScriptedTable(_BaseDouble):
    """Per-``(judge_name, run_ref)`` verdict table.

    ``table`` maps ``(judge_name, run_ref)`` to a ``should_fire`` bool. A call
    whose key is absent falls back to ``default`` when provided, else raises —
    the :class:`ScriptedCallLLM` precedent of failing loudly on an unscripted
    call so a mis-authored oracle points at the exact decision.
    """

    def __init__(
        self,
        table: dict[tuple[str, str], bool],
        *,
        default: bool | None = None,
        severity: str = "warning",
        quote_len: int = 120,
    ) -> None:
        super().__init__(severity=severity, quote_len=quote_len)
        self.table = dict(table)
        self.default = default

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.calls += 1
        header = _parse_header(user)
        key = (header["judge_name"], header["run_ref"])
        if key in self.table:
            should_fire = self.table[key]
        elif self.default is not None:
            should_fire = self.default
        else:
            raise RuntimeError(
                f"ScriptedTable: no verdict scripted for {key!r} and no default supplied"
            )
        return _verdict_json(
            should_fire=should_fire,
            severity=self.severity,
            evidence_span=self._span(user),
            rationale="scripted-table verdict",
        )


class MalformedThenValid(_BaseDouble):
    """Garbage on the first call, valid JSON thereafter — exercises the retry."""

    def __init__(
        self,
        *,
        should_fire: bool = True,
        severity: str = "warning",
        garbage: str = "this is not JSON at all, sorry",
        quote_len: int = 120,
    ) -> None:
        super().__init__(severity=severity, quote_len=quote_len)
        self.should_fire = should_fire
        self.garbage = garbage

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.calls += 1
        if self.calls == 1:
            # Record the span so the second (valid) call still quotes verbatim.
            self._span(user)
            return self.garbage
        return _verdict_json(
            should_fire=self.should_fire,
            severity=self.severity,
            evidence_span=self._span(user),
            rationale="valid on retry",
        )


__all__ = [
    "AlwaysConfirm",
    "AlwaysRefute",
    "MalformedThenValid",
    "ScriptedTable",
    "SpanQuoting",
]
