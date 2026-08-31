"""Run-record / lineage types: persistence-side and transcript-shape results.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Run record / lineage
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactFile:
    """One regular file captured from a run's writable artifact root."""

    path: str
    size: int
    sha256: str
    media_type: str


@dataclass(frozen=True, slots=True)
class ArtifactSet:
    """Deterministic inventory of files a harness produced during one run."""

    root: Path
    manifest_path: Path
    files: tuple[ArtifactFile, ...]
    total_bytes: int
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class RunResult:
    """The transcript-shape result of executing one board entry under one generation.

    Returned by the runner to expectation evaluators and to the reducer's
    multi-turn-pattern detectors. Carries only the user-facing surface —
    internal agent reasoning, tool calls, and goldfive events are stored
    elsewhere (the events JSONL) and intentionally not exposed here so
    the emulator and the judge cannot trivially collude with the inner
    harness.

    Fields
    ------
    run_id:
        Unique id of the run.
    entry_id:
        The :class:`BoardEntry.id` executed.
    final_output:
        The last assistant turn's user-facing output as a string. For
        single-turn entries this is the only assistant output. For
        multi-turn entries this is the final assistant turn.
    transcript:
        All assistant user-facing turns in order. For single-turn entries
        this is a length-1 tuple matching :attr:`final_output`. For
        multi-turn entries this is the full conversation from the user's
        view. User turns are NOT included — the entry already carries
        them (scripted) or the emulator produced them (emulated) and the
        reducer fetches them from goldfive's transcript if needed.
    runtime_ms:
        Total wall-clock duration in milliseconds.
    aborted:
        ``True`` iff the runner force-terminated this run.
    abort_reason:
        Short symbolic reason when :attr:`aborted` is true.
    artifacts:
        Files discovered under the worker-provided run scratch directory,
        captured before that temporary directory is removed. ``None`` only
        for a caller that did not run through artifact capture.
    """

    run_id: str
    entry_id: str
    final_output: str
    transcript: tuple[str, ...]
    runtime_ms: int
    aborted: bool = False
    abort_reason: str = ""
    artifacts: ArtifactSet | None = None
