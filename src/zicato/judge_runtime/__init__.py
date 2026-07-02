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
  by zicato's *auxiliary* callable (the two-callable rule — a judge must
  not share the inner agent's LLM surface); python specs become a thin
  wrapper around operator-supplied code.
* :func:`assemble_judges` composes the full judge list for one board
  entry run: goldfive's default built-ins (minus any a board's
  ``disable_drift`` suppressed) plus the entry's custom
  :class:`JudgeSpec` judges. The ADK adapter passes the result straight
  into ``goldfive.run(..., judges=...)``.

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
from zicato.judge_runtime.reliability import (
    JudgeReliability,
    test_retest,
    test_retest_board,
)

__all__ = [
    "JudgeReliability",
    "JudgeSpecLike",
    "assemble_judges",
    "builtin_judge_names_to_suppress",
    "default_judges_minus",
    "judge_spec_to_goldfive",
    "test_retest",
    "test_retest_board",
]
