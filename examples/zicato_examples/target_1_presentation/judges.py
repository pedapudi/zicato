"""Custom PROCESS judges for the target_1_presentation board.

Where :mod:`predicates` carries the OUTCOME pass/fail contract, this
module carries reusable PROCESS-judge factories — deterministic Python
process judges that observe a run *while it is in flight* and fold a
severity-bearing verdict into the drift loss.

The headline judge here is :func:`file_findability_judge`, which detects
the dominant failure mode of the presentation tree: the agent writes its
slides under one slug and then cannot reliably *find* them again, so the
reviewer's ``read_presentation_files(topic)`` returns
``files_not_found`` / ``<error reading …>``, the debugger's fuzzy
``find_presentation_files`` is invoked, and the reviewer loops on
``read_presentation_files`` against the wrong slug. ~93% of prior board
runs exhibited this. Folding it into the scalar loss (via the judge's
``custom`` drift, attributed to its ``judge_name`` and weighted through
:attr:`ScoringWeights.per_judge_weights`) gives the proposer a direct
gradient to optimise the write/read slug logic against.

Mechanism
---------
The detector is a **deterministic** Python process judge — the failure
signature is precise (specific tool names, a specific structured report,
a specific error marker), so a string/structural matcher is strictly
better than an LLM judge: no aux-LLM cost, no nondeterminism, no
collusion surface.

A board entry attaches it declaratively via the ``Judge.python``
authoring helper, pointing at :data:`FILE_FINDABILITY_JUDGE_PATH`::

    from zicato.board.judges import Judge
    from goldfive import DriftSeverity

    entry = BoardEntry(
        ...,
        judges=(
            Judge.python(
                FILE_FINDABILITY_NAME,
                FILE_FINDABILITY_JUDGE_PATH,
                severity=DriftSeverity.WARNING,
            ),
        ),
    )

and weights it in ``scoring.json`` under
``per_judge_weights["file_findability"]``.

Where the signal really lives (artifact fidelity)
-------------------------------------------------
This judge must grade what the agent *did* — the tool round-trips it
actually ran — NOT what its chain-of-thought *narrated*. goldfive does
not emit a standalone tool-call wire event and does not put a
``tool_event`` key on the :class:`~goldfive.judges.JudgeContext`; custom
judges are dispatched only at REASONING observation points
(``DriftObserver._dispatch_custom_judges`` runs from
``observe_reasoning``). So a judge that reads ``ctx.reasoning_text`` /
``ctx.transcript`` is reading the model's *narration*, and a judge that
reads ``ctx.extras["tool_event"]`` is reading a key that is never set.
That is the defect this detector previously tripped on: it fired
``read_presentation_files called 0× (retry loop)`` because the loop
signal came from the narration mentioning the tool name twice while the
structured read counter stayed at zero — a self-contradiction.

The REAL structured record of every tool call is the goldfive session's
``recent_events`` ring buffer (``session.note_tool_observation`` appends
one ``kind == "tool_observed"`` entry per call, carrying ``tool_name``,
``args_preview``, ``result_preview``, ``is_error`` and
``error_message``). It is reachable from the judge as
``ctx.session_state.recent_events``. This detector reads THAT — the
ground-truth tool ledger — as its primary, deterministic source. The
reasoning/transcript text is consulted only as a last-resort fallback
for the not-found / find markers when no structured tool ledger was
available at all, and is NEVER allowed to manufacture the retry-loop
signal (that signal is derived solely from the structured read count, so
the count and the reason can never contradict each other).

Stateful-by-observation-point contract
---------------------------------------
goldfive calls :meth:`Judge.evaluate` once per observation point with a
fresh :class:`~goldfive.judges.JudgeContext` snapshot. The detector
keeps its own accumulator state across calls — exactly as the
:class:`~goldfive.judges.Judge` protocol permits ("Implementations are
free to keep their own state across calls") — and counts the *distinct*
failure signals seen so far. The session ``recent_events`` buffer is a
bounded ring (default 16) re-snapshotted on every call, so each tool
observation is de-duplicated by a stable signature before it is folded
in — re-seeing the same entry across observation points never
double-counts a read. The detector emits a drift verdict only when a NEW
signal first fires, so the number of ``custom`` drift events the run
accrues equals the number of distinct failure signals (1–4), and the
verdict's severity escalates with that count. A clean run — writes once,
reads once, no not-found, no find — never trips a signal and the judge
stays silent (empty-default verdict, no event), exactly as a no-signal
judge should.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from goldfive import DriftKind, DriftSeverity
from goldfive.judges import JudgeVerdict

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from goldfive.judges import JudgeContext

# ---------------------------------------------------------------------------
# Public attach handles
# ---------------------------------------------------------------------------

#: Stable ``judge_name`` for the file-findability judge. This is the key
#: used in ``scoring.json``'s ``per_judge_weights`` and the
#: ``custom:file_findability`` attributed drift kind the reducer folds
#: into the loss.
FILE_FINDABILITY_NAME = "file_findability"

#: Dotted import path a ``Judge.python`` JudgeSpec points at to attach the
#: detector to a board entry. Colon form (``pkg.mod:attr``) — the style
#: the judge-runtime resolver and zicato adapters prefer.
FILE_FINDABILITY_JUDGE_PATH = "zicato_examples.target_1_presentation.judges:FileFindabilityJudge"

# ---------------------------------------------------------------------------
# The failure-signature tokens (from the real transcripts)
# ---------------------------------------------------------------------------

#: The reviewer's read tool — the slide-reading round-trip that fails
#: when the developer wrote under a different slug.
_READ_TOOL = "read_presentation_files"

#: The debugger's fuzzy-locate tool — a clean run NEVER needs it; any
#: invocation at all is itself a failure signal.
_FIND_TOOL = "find_presentation_files"

#: Per-file read error marker emitted by ``read_presentation_files`` when
#: the path it derived does not exist.
_READ_ERROR_MARKER = "<error reading"

#: The reviewer's structured "I could not find the files" report token.
_FILES_NOT_FOUND = "files_not_found"

#: goldfive ``Session.recent_events`` discriminator for a tool call
#: recorded by ``DriftObserver.note_tool_observation`` (mirrors
#: ``goldfive.types.RECENT_EVENT_KIND_TOOL_OBSERVED``). Inlined as the
#: literal rather than imported so the judge keeps resolving under a
#: goldfive revision that has not landed the constant — the wire value
#: ("tool_observed") is the stable contract.
_TOOL_OBSERVED_KIND = "tool_observed"


# ---------------------------------------------------------------------------
# Severity ladder
# ---------------------------------------------------------------------------
#
# The judge escalates as more distinct failure signals fire. One stray
# signal is INFO-worthy noise; two means the wrong-slug failure is real
# and forced rework; three-or-more is the full debugger-round-trip
# pathology the proposer must drive out.
_SEVERITY_LADDER: tuple[DriftSeverity, ...] = (
    DriftSeverity.INFO,
    DriftSeverity.WARNING,
    DriftSeverity.CRITICAL,
)


def _severity_for_signal_count(n: int) -> DriftSeverity:
    """Map a distinct-signal count (>=1) onto an escalating severity.

    1 signal -> INFO, 2 -> WARNING, 3+ -> CRITICAL. Clamped at the top of
    the ladder so a four-signal run still maps cleanly to CRITICAL.
    """
    idx = min(max(n, 1), len(_SEVERITY_LADDER)) - 1
    return _SEVERITY_LADDER[idx]


def _tool_event_fields(tool_event: Any) -> tuple[str, str]:
    """Extract ``(tool_name, result_text)`` from a goldfive tool_event.

    Tolerant of both the dict shape goldfive's tool-observation path uses
    (``{"tool"/"name": ..., "output"/"result"/"error": ...}``) and a
    bare object with those attributes. Always returns lowercase strings;
    a missing field becomes ``""`` so the caller's substring checks never
    raise on a partial event.
    """
    if tool_event is None:
        return "", ""
    if isinstance(tool_event, dict):
        name = tool_event.get("tool") or tool_event.get("name") or ""
        # The result lives under any of several conventional keys; we
        # fold every candidate into one searchable blob so the not-found
        # / error markers are found regardless of which key carried them.
        parts = [
            tool_event.get("output"),
            tool_event.get("result"),
            tool_event.get("error"),
            tool_event.get("error_message"),
        ]
    else:
        name = getattr(tool_event, "tool", "") or getattr(tool_event, "name", "") or ""
        parts = [
            getattr(tool_event, "output", None),
            getattr(tool_event, "result", None),
            getattr(tool_event, "error", None),
        ]
    blob = " ".join(_stringify(p) for p in parts if p is not None)
    return str(name).strip().lower(), blob.lower()


def _stringify(value: Any) -> str:
    """Render a tool-result fragment as searchable text.

    Dicts / lists are JSON-dumped so a structured ``{"files_not_found":
    [...]}`` payload still exposes its marker token to a substring scan;
    everything else is ``str()``-coerced.
    """
    if isinstance(value, dict | list):
        try:
            return json.dumps(value)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return str(value)
    return str(value)


class FileFindabilityJudge:
    """Deterministic process judge for the self-location failure mode.

    Conforms to goldfive's :class:`~goldfive.judges.Judge` protocol — a
    stable :attr:`name` plus an async :meth:`evaluate`. Instantiated with
    no arguments by :func:`zicato.judge_runtime.judge_spec_to_goldfive`
    when a board entry's ``Judge.python`` spec resolves to this class.

    The judge accumulates four boolean failure signals across the run's
    observation points (see module docstring) and emits a ``custom``
    drift verdict — attributed to :attr:`name`, so it weights through
    ``per_judge_weights["file_findability"]`` — each time a NEW signal
    first fires, at a severity that escalates with the running count of
    distinct signals. Net effect: a clean run accrues zero
    ``custom:file_findability`` drift; a fully-pathological run accrues
    four, the last few at CRITICAL — a monotone gradient the proposer can
    optimise the write/read slug logic against.
    """

    __slots__ = (
        "name",
        "_read_call_count",
        "_saw_find_tool",
        "_saw_read_not_found",
        "_saw_files_not_found_report",
        "_saw_read_loop",
        "_emitted_signals",
        "_seen_tool_obs",
        "_saw_structured_tools",
    )

    def __init__(self) -> None:
        self.name: str = FILE_FINDABILITY_NAME
        # Cumulative observation-point state.
        self._read_call_count: int = 0
        self._saw_find_tool: bool = False
        self._saw_read_not_found: bool = False
        self._saw_files_not_found_report: bool = False
        self._saw_read_loop: bool = False
        # How many distinct signals have already been *emitted* as drift,
        # so we emit exactly once per signal and escalate severity with
        # the running total.
        self._emitted_signals: int = 0
        # De-dup ledger for the structured tool-observation buffer. The
        # session ``recent_events`` ring is re-snapshotted on every
        # observation point, so the SAME tool call appears again on the
        # next call until it is trimmed; we count each one exactly once by
        # a stable per-entry signature.
        self._seen_tool_obs: set[tuple[Any, ...]] = set()
        # Whether ANY structured tool ledger was available this run. When
        # True, the free-text narration fallback stays disarmed — we have
        # ground truth and must not let narration manufacture signals.
        self._saw_structured_tools: bool = False

    def _active_signal_count(self) -> int:
        """Number of distinct failure signals observed so far (0–4)."""
        return sum(
            (
                self._saw_read_not_found,
                self._saw_find_tool,
                self._saw_read_loop,
                self._saw_files_not_found_report,
            )
        )

    def _fold_tool_call(self, tool_name: str, result_blob: str) -> None:
        """Fold one *real* tool call (name + searchable result blob) in.

        The single chokepoint every structured tool source — the goldfive
        session ledger and the legacy ``extras["tool_event"]`` — routes
        through, so the retry-loop signal is derived ONLY from the
        structured read count and the reason string can never contradict
        it (the "called 0× (retry loop)" defect).
        """
        if tool_name == _FIND_TOOL:
            self._saw_find_tool = True
        if tool_name == _READ_TOOL:
            self._read_call_count += 1
            if self._read_call_count >= 2:
                self._saw_read_loop = True
            if _READ_ERROR_MARKER in result_blob or _FILES_NOT_FOUND in result_blob:
                self._saw_read_not_found = True
        if _FILES_NOT_FOUND in result_blob:
            self._saw_files_not_found_report = True

    def _ingest(self, ctx: JudgeContext) -> None:
        """Fold one observation-point snapshot into the accumulators.

        Source precedence (highest fidelity first):

        1. ``ctx.session_state.recent_events`` — goldfive's real
           ``tool_observed`` ledger, the ground truth for what the agent
           actually ran. De-duplicated per entry across observation
           points.
        2. ``ctx.extras["tool_event"]`` — a structured tool event a
           runner may attach directly (kept for back-compat / test
           harnesses; goldfive itself does not set it today).
        3. Free-text narration fallback — armed ONLY when no structured
           ledger was seen for the whole run, and NEVER allowed to
           manufacture the retry-loop signal.
        """
        # 1. Structured tool ledger from the live goldfive session — the
        #    precise, deterministic signal source. Each ring-buffer entry
        #    is counted once via a stable signature.
        for tool_name, result_blob in self._new_session_tool_calls(ctx):
            self._saw_structured_tools = True
            self._fold_tool_call(tool_name, result_blob)

        # 2. Legacy structured tool_event path (a runner-attached event).
        if ctx.extras and ctx.extras.get("tool_event") is not None:
            self._saw_structured_tools = True
            tool_name, result_blob = _tool_event_fields(ctx.extras.get("tool_event"))
            self._fold_tool_call(tool_name, result_blob)

        # 3. Free-text fallback. Disarmed once any structured tool ledger
        #    has been seen this run — with ground truth in hand we must
        #    not let the model's chain-of-thought narration manufacture
        #    signals (the artifact-fidelity defect). It exists only for a
        #    runner that surfaces NO structured tool data at all.
        if self._saw_structured_tools:
            return
        text = " ".join(
            [ctx.reasoning_text or "", *(t or "" for t in (ctx.transcript or ()))]
        ).lower()
        if not text:
            return
        if _FIND_TOOL in text:
            self._saw_find_tool = True
        if _FILES_NOT_FOUND in text:
            self._saw_files_not_found_report = True
            self._saw_read_not_found = True
        if _READ_ERROR_MARKER in text:
            self._saw_read_not_found = True

    def _new_session_tool_calls(self, ctx: JudgeContext) -> list[tuple[str, str]]:
        """Yield ``(tool_name, result_blob)`` for unseen session tool calls.

        Reads goldfive's ``recent_events`` ring buffer off
        ``ctx.session_state`` and returns only entries with
        ``kind == "tool_observed"`` not seen on a prior observation point.
        The ring is re-snapshotted each call and trimmed (default 16), so
        de-dup is by a stable per-entry signature
        (``ts_ms``/``tool_name``/``args_preview``) rather than list index.
        Tolerant of a missing session / buffer / fields — any degenerate
        shape yields no calls so the detector never raises out of a judge.
        """
        session = getattr(ctx, "session_state", None)
        if session is None:
            return []
        events = getattr(session, "recent_events", None)
        if not events:
            return []
        out: list[tuple[str, str]] = []
        for entry in events:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") != _TOOL_OBSERVED_KIND:
                continue
            sig = (
                entry.get("ts_ms"),
                entry.get("tool_name"),
                entry.get("args_preview"),
            )
            if sig in self._seen_tool_obs:
                continue
            self._seen_tool_obs.add(sig)
            tool_name = str(entry.get("tool_name") or "").strip().lower()
            # Fold every result-bearing field into one searchable blob so
            # the not-found / error markers are found regardless of which
            # key carried them (matches ``_tool_event_fields``' approach).
            blob = " ".join(
                _stringify(entry.get(key))
                for key in ("result_preview", "error_message")
                if entry.get(key) is not None
            ).lower()
            out.append((tool_name, blob))
        return out

    async def evaluate(self, ctx: JudgeContext) -> JudgeVerdict:
        """Accumulate signals; emit escalating drift as new ones fire.

        Returns an empty-default :class:`~goldfive.judges.JudgeVerdict`
        (no event) on any observation point that does not push the
        distinct-signal count past what has already been emitted — so a
        clean run is silent and a failing run emits exactly one
        ``custom`` drift per distinct signal, at a severity that climbs
        with the running total.
        """
        self._ingest(ctx)
        active = self._active_signal_count()
        if active <= self._emitted_signals:
            # No new signal since the last emission — nothing to say.
            return JudgeVerdict()
        self._emitted_signals = active
        severity = _severity_for_signal_count(active)
        reason = self._reason(active)
        return JudgeVerdict(
            drift_emitted=True,
            drift_kind=DriftKind.CUSTOM,
            severity=severity,
            detail=f"file findability: {reason}",
        )

    def _reason(self, active: int) -> str:
        """Human-readable one-line reason naming the signals that fired."""
        fired: list[str] = []
        if self._saw_read_not_found:
            fired.append("read returned files_not_found / <error reading …>")
        if self._saw_find_tool:
            fired.append("find_presentation_files invoked (clean runs never need it)")
        if self._saw_read_loop:
            fired.append(f"read_presentation_files called {self._read_call_count}× (retry loop)")
        if self._saw_files_not_found_report:
            fired.append("files_not_found structured report emitted")
        return f"{active} failure signal(s): " + "; ".join(fired)


def file_findability_judge() -> FileFindabilityJudge:
    """Construct a fresh :class:`FileFindabilityJudge`.

    A bare factory provided for symmetry with the predicate module's
    callable style and for tests / callers that prefer a function over
    pointing a JudgeSpec at the class. ``Judge.python`` may point at
    either this callable or the class directly — the judge-runtime
    wrapper handles both.
    """
    return FileFindabilityJudge()


__all__ = [
    "FILE_FINDABILITY_JUDGE_PATH",
    "FILE_FINDABILITY_NAME",
    "FileFindabilityJudge",
    "file_findability_judge",
]
