"""zicato.judge_runtime — turn ``JudgeSpec`` declarations into live judges.

zicato describes the quality signals it wants armed on a board entry
*declaratively*: a :class:`~zicato.core.JudgeSpec` is a name, a mode
(``"inline"`` / ``"python"``), a ``body`` (a natural-language criterion
or a dotted import path), and a :class:`goldfive.DriftSeverity`. goldfive,
on the other hand, runs *executable* judges — objects conforming to its
:class:`~goldfive.judges.Judge` protocol (a stable ``.name`` plus an
async ``evaluate(ctx) -> JudgeVerdict``).

This package is the runtime bridge between the two:

* :func:`judge_spec_to_goldfive` turns one :class:`JudgeSpec` into one
  live goldfive ``Judge``. Inline specs become an LLM-as-a-judge driven
  by zicato's *evaluation* callable (the two-callable rule — a judge must
  not share the inner agent's LLM surface); python specs become a thin
  wrapper around operator-supplied code.
* :func:`assemble_judges` composes the full judge list for one board
  entry run: goldfive's default built-ins (minus any a board's
  ``disable_drift`` suppressed) plus the entry's custom
  :class:`JudgeSpec` judges. The ADK adapter passes the result straight
  into ``goldfive.run(..., judges=...)``.
* :mod:`zicato.judge_runtime.error_register` is the process-wide count of
  judge calls that RAISED. Both judge kinds swallow their callable's
  exceptions by hard contract (a judge must not crash a run), which made a
  broken judge endpoint byte-identical, in every persisted artifact, to a
  judge that ran and found nothing; the register is the counter that
  survives the catch and rides out to
  :attr:`~zicato.core.loss.LossProfile.judge_errors` and loop health.
* :mod:`zicato.judge_runtime.io_capture` is the verbatim judge-I/O
  capture seam for board reflection: an optional
  :class:`~zicato.judge_runtime.io_capture.JudgeIOSink` threaded through
  the builder retains each inline judge call's exact input + raw
  response + verdict as a ``judge_io.jsonl`` sidecar beside the run's
  ``loss.json``. Best-effort by contract; ``None`` (the default)
  captures nothing and changes nothing.

The enum->string boundary (zicato carries :class:`goldfive.DriftKind` /
:class:`goldfive.DriftSeverity` enum members; goldfive's
:class:`JudgeVerdict` wants lowercase wire strings) is handled *inside*
this package, at the verdict-construction site, so the string form
never leaks upward into zicato code.
"""

from __future__ import annotations

from zicato.judge_runtime.assemble import assemble_judges
from zicato.judge_runtime.builder import JudgeSpecLike, judge_spec_to_goldfive
from zicato.judge_runtime.disable import (
    builtin_judge_names_to_suppress,
    default_judges_minus,
)
from zicato.judge_runtime.error_register import (
    clear_judge_errors,
    judge_error_snapshot,
    record_judge_error,
    record_judge_invocation,
)
from zicato.judge_runtime.io_capture import (
    JUDGE_IO_ERROR_KIND,
    JudgeIOFileSink,
    JudgeIOSink,
    judge_io_path_for_loss,
    read_judge_io,
)
from zicato.judge_runtime.reliability import (
    JudgeReliability,
    test_retest,
    test_retest_board,
)

__all__ = [
    "JUDGE_IO_ERROR_KIND",
    "JudgeIOFileSink",
    "JudgeIOSink",
    "JudgeReliability",
    "JudgeSpecLike",
    "assemble_judges",
    "builtin_judge_names_to_suppress",
    "clear_judge_errors",
    "default_judges_minus",
    "judge_error_snapshot",
    "judge_io_path_for_loss",
    "judge_spec_to_goldfive",
    "read_judge_io",
    "record_judge_error",
    "record_judge_invocation",
    "test_retest",
    "test_retest_board",
]
