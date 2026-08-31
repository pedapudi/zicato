"""Regression-suite gate: run the candidate snapshot's own test suite.

The promote gate evaluates a child generation against a parent generation
using scalar score + pass-rate monotonicity. That signal is necessary
but not sufficient: a patch can trivially improve drift_loss / pass_rate
on the board while breaking the inner harness's regression invariants.
The regression gate fixes that hole by running the snapshot's own
``pytest`` suite as a subprocess; any failure forces the tournament to
reject regardless of how strong the scoring signal is.

The runner side wires this in via :meth:`run_regression_suite`. The
contract:

* Best-effort discovery — if the snapshot does not contain a ``tests/``
  directory under the named mutable tree the function returns
  ``passed=True`` with an explanatory summary. The gate is opt-in via
  :attr:`ScoringWeights.regression_gate_enabled`; operators who turn it
  on for an adapter that doesn't ship tests get a silent skip rather
  than a stalled tournament.
* Subprocess isolation — pytest runs as ``asyncio`` subprocess so the
  zicato runtime's event loop stays responsive (the live dashboard
  keeps polling state) and the test process's globals can't leak into
  the runner's process.
* Bounded wall clock — the suite is killed after ``timeout_s`` seconds.
  A timeout is reported as ``passed=False`` with the summary keyword
  ``"timeout"`` so the gate-side wiring can surface it distinctly from
  ordinary test failures.
* Parsed failure ids — pytest's ``-q --tb=line`` output emits per-failed
  test ids in ``FAILED tests/test_x.py::test_y - …`` lines. The parser
  captures the id up to the dash; the human-readable rest of the line
  stays in :attr:`RegressionResult.summary`.

The dataclass is frozen + slotted so it round-trips through
``dataclasses.asdict`` for journaling.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RegressionResult:
    """Outcome of one regression-suite invocation.

    Fields
    ------
    passed:
        ``True`` when the suite ran (or was silently skipped) without
        any failure. ``False`` for any failure, timeout, or non-zero
        exit code we cannot otherwise classify.
    failed_tests:
        Tuple of test ids (e.g. ``"tests/test_foo.py::test_bar"``) the
        parser extracted from the pytest output. Empty when no failures
        were detected — including on timeout, since pytest may not have
        flushed any FAILED line before being killed.
    summary:
        Short human-readable line for the journal. On success this is
        ``"all tests passed"`` (or the skip reason); on failure it
        identifies the failure mode (``"N tests failed"`` /
        ``"timeout after Ns"`` / ``"pytest exit code N"``).
    elapsed_s:
        Wall-clock seconds spent in the subprocess. Useful in the
        journal even when the suite passed.
    """

    passed: bool
    failed_tests: tuple[str, ...]
    summary: str
    elapsed_s: float


# Match pytest's ``-q`` failure line. The id is captured up to the
# first " - " separator or end-of-line; this is the same shape pytest
# uses for ``--tb=line`` and the default short-traceback mode.
_FAILED_LINE = re.compile(r"^FAILED\s+(\S+?)(?:\s+-\s+.*)?$", re.MULTILINE)


def _parse_failed_tests(output: str) -> tuple[str, ...]:
    """Pull failed test ids out of pytest's stdout.

    Returns the ids in document order (the order pytest prints them).
    Duplicates are squashed: a parametrised test that fails twice on
    different params will already have distinct ids; an exact duplicate
    here would be a parser artifact rather than real signal.
    """
    seen: list[str] = []
    for match in _FAILED_LINE.finditer(output):
        test_id = match.group(1)
        if test_id not in seen:
            seen.append(test_id)
    return tuple(seen)


def _resolve_test_root(snapshot_root: Path) -> Path | None:
    """Find the directory whose ``tests/`` we want pytest to run in.

    Search order (first match wins):

    1. ``snapshot_root`` itself — used when the mutable tree IS the
       snapshot (adapters that don't narrow).
    2. Each immediate child directory containing a ``tests/`` subdir —
       e.g. the goldfive checkout living at ``snapshot_root/goldfive/``.

    Returns the chosen root (the cwd we'll hand pytest), or ``None``
    when no ``tests/`` could be located.
    """
    if (snapshot_root / "tests").is_dir():
        return snapshot_root
    if not snapshot_root.is_dir():
        return None
    for child in sorted(snapshot_root.iterdir()):
        if child.is_dir() and (child / "tests").is_dir():
            return child
    return None


def _classify_completed_run(output: str, exit_code: int, elapsed_s: float) -> RegressionResult:
    """Map a finished pytest run's stdout + exit code onto a result.

    Pure parse/classification seam — no subprocess, no clock. The
    subprocess-facing :func:`run_regression_suite` delegates here after
    ``communicate()`` returns, and tests can drive the summary /
    exit-code / failed-id mapping directly with canned output instead of
    booting a real ``pytest`` child per case.
    """
    failed = _parse_failed_tests(output)

    if exit_code == 0:
        return RegressionResult(
            passed=True,
            failed_tests=(),
            summary="all tests passed",
            elapsed_s=elapsed_s,
        )

    if failed:
        summary = f"{len(failed)} tests failed"
    else:
        summary = f"pytest exit code {exit_code}"
    return RegressionResult(
        passed=False,
        failed_tests=failed,
        summary=summary,
        elapsed_s=elapsed_s,
    )


async def run_regression_suite(
    snapshot_root: Path,
    *,
    test_command: tuple[str, ...] = ("pytest", "tests/", "-q", "--tb=line"),
    timeout_s: int = 600,
) -> RegressionResult:
    """Run the snapshot's regression suite as a subprocess.

    Returns a :class:`RegressionResult` capturing pass/fail, the list of
    failed test ids parsed from pytest's stdout, a short human summary,
    and the elapsed wall-clock seconds.

    If no ``tests/`` directory can be located under ``snapshot_root``
    the function returns ``passed=True`` immediately — a silent skip is
    preferable to a stall when the adapter ships no regression suite.
    The summary field carries ``"no tests/ directory; skipped"`` so the
    journal records the skip explicitly.

    Timeout handling: when the subprocess outlives ``timeout_s`` seconds
    we kill it and return ``passed=False`` with summary
    ``"timeout after <N>s"`` and an empty failed-tests tuple (pytest may
    not have flushed any FAILED line before being killed).
    """
    started = time.monotonic()

    test_root = _resolve_test_root(snapshot_root)
    if test_root is None:
        return RegressionResult(
            passed=True,
            failed_tests=(),
            summary="no tests/ directory; skipped",
            elapsed_s=0.0,
        )

    proc = await asyncio.create_subprocess_exec(
        *test_command,
        cwd=str(test_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # Drain so the transport closes cleanly; ignore the bytes.
        try:
            await proc.communicate()
        except Exception:  # noqa: BLE001
            pass
        elapsed = time.monotonic() - started
        return RegressionResult(
            passed=False,
            failed_tests=(),
            summary=f"timeout after {timeout_s}s",
            elapsed_s=elapsed,
        )

    elapsed = time.monotonic() - started
    output = stdout_bytes.decode("utf-8", errors="replace")
    exit_code = proc.returncode if proc.returncode is not None else -1
    return _classify_completed_run(output, exit_code, elapsed)


__all__ = ["RegressionResult", "run_regression_suite"]
