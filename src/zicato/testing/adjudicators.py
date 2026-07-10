"""Scripted adjudicator doubles — deterministic meta-judges for tests.

The board-reflection adjudicator
(:mod:`zicato.reflection.adjudicator`) talks to a meta-judge through the
standard ``CallLLM`` shape ``(system, user, model) -> str`` and expects a strict
JSON verdict back. G3 forbids live endpoints, so these doubles stand in: each
is a callable that parses the adjudicator's user prompt (the machine-readable
``JUDGE UNDER REVIEW`` / ``DECISION REF`` / ``THE JUDGE OBSERVED`` header plus
the ``<<<TRANSCRIPT … TRANSCRIPT>>>`` block) and returns a deterministic
verdict.

* :class:`AlwaysConfirm` — AGREES with the judge (fired ⇒ should_fire, silent ⇒
  should_be_silent): a judge that is always right ⇒ a TP/TN corpus.
* :class:`AlwaysRefute` — DISAGREES with the judge (fired ⇒ should_be_silent,
  silent ⇒ should_fire): a judge that is always wrong ⇒ an FP/FN corpus.
* :class:`SpanQuoting` — quotes an EXACT substring of the transcript it saw into
  ``evidence_span``, so a test can prove the adjudicator received the verbatim
  ``judge_io`` bytes (the quoted span appears in the sidecar's
  ``reasoning_text``).
* :class:`ScriptedTable` — a per-``(judge_name, run_ref)`` verdict table for
  hand-built oracle corpora.
* :class:`MalformedThenValid` — returns garbage on its first call and valid JSON
  thereafter, exercising the adjudicator's one-retry-then-ambiguous path.

Every double counts its calls (``.calls``) so a test can assert the
cache-idempotency invariant (second pass ⇒ zero adjudicator calls). All quote a
verbatim span so any of them can ground a finding.
"""

from __future__ import annotations

import json

# The adjudicator protocol markers, inlined so these test doubles carry NO
# import edge into :mod:`zicato.reflection` (which reaches
# :mod:`zicato.dashboard.transcript` for the preview-fidelity fallback — a
# forbidden edge for anything the library import-linter contract covers, and
# ``zicato.testing`` is covered). They mirror the constants of the same names in
# :mod:`zicato.reflection.adjudicator`; a consistency test in
# ``tests/test_reflection_adjudicator.py`` pins them equal so neither can drift.
OBSERVED_FIRED = "fired"
OBSERVED_SILENT = "silent"
TRANSCRIPT_OPEN = "<<<TRANSCRIPT"
TRANSCRIPT_CLOSE = "TRANSCRIPT>>>"


def _parse_header(user: str) -> dict[str, str]:
    """Extract ``judge_name`` / ``run_ref`` / ``observed`` from the user prompt."""
    judge_name = ""
    run_ref = ""
    observed = OBSERVED_SILENT
    for line in user.splitlines():
        if line.startswith("JUDGE UNDER REVIEW:"):
            judge_name = line.split(":", 1)[1].strip()
        elif line.startswith("DECISION REF:"):
            run_ref = line.split(":", 1)[1].strip()
        elif line.startswith("THE JUDGE OBSERVED:"):
            observed = OBSERVED_FIRED if "fired" in line else OBSERVED_SILENT
    return {"judge_name": judge_name, "run_ref": run_ref, "observed": observed}


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
    """Shared call-counting + span-quoting for the scripted doubles."""

    def __init__(self, *, severity: str = "warning", quote_len: int = 120) -> None:
        self.severity = severity
        self.quote_len = quote_len
        self.calls = 0
        self.last_transcript = ""

    def _span(self, user: str) -> str:
        text = _transcript_text(user)
        self.last_transcript = text
        return text[: self.quote_len]


class AlwaysConfirm(_BaseDouble):
    """Agrees with the judge: fired ⇒ should_fire, silent ⇒ should_be_silent."""

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.calls += 1
        header = _parse_header(user)
        should_fire = header["observed"] == OBSERVED_FIRED
        return _verdict_json(
            should_fire=should_fire,
            severity=self.severity,
            evidence_span=self._span(user),
            rationale="the judge's decision is consistent with the transcript",
        )


class AlwaysRefute(_BaseDouble):
    """Disagrees with the judge: fired ⇒ should_be_silent, silent ⇒ should_fire."""

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.calls += 1
        header = _parse_header(user)
        should_fire = header["observed"] == OBSERVED_SILENT
        return _verdict_json(
            should_fire=should_fire,
            severity=self.severity,
            evidence_span=self._span(user),
            rationale="the transcript contradicts the judge's decision",
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
