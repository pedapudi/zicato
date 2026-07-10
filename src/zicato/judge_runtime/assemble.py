"""Assemble the goldfive judge list for one board entry run.

The adapter (:mod:`zicato.adapters.adk`) drives a board entry through
``goldfive.run`` / ``goldfive.wrap``. goldfive#437 lets the caller pass
a custom judge list via ``judges=[...]``; goldfive installs that list
verbatim and emits a :class:`JudgementEmitted` for every populated
verdict.

This module is the single place that builds that list. It composes the
two halves:

#. **built-ins** — goldfive's default judge set, minus the built-ins a
   board's ``disable_drift`` named for suppression
   (:mod:`zicato.judge_runtime.disable`); and
#. **custom** — the board *entry*'s declared
   :class:`~zicato.core.JudgeSpec` judges, each turned into a live
   goldfive ``Judge`` (:mod:`zicato.judge_runtime.builder`).

The adapter calls :func:`assemble_judges` once per entry and forwards
the result straight into ``goldfive.run(..., judges=...)``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from zicato.judge_runtime.builder import JudgeSpecLike, judge_spec_to_goldfive
from zicato.judge_runtime.disable import (
    builtin_judge_names_to_suppress,
    default_judges_minus,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from collections.abc import Awaitable, Callable

    import goldfive

    AuxCallLLM = Callable[[str, str, str], Awaitable[str]]

log = logging.getLogger("zicato.judge_runtime.assemble")


def assemble_judges(
    *,
    entry_judges: tuple[JudgeSpecLike, ...] | list[JudgeSpecLike] | None,
    disable_drift: tuple[Any, ...] | list[Any] | None,
    aux_call_llm: AuxCallLLM,
    io_sink: Any = None,
) -> list[goldfive.Judge]:
    """Build the full goldfive judge list for one board-entry run.

    Parameters
    ----------
    entry_judges:
        ``BoardEntry.judges`` — the entry's declared
        :class:`~zicato.core.JudgeSpec` tuple. ``None`` / empty means
        the entry adds no custom judges.
    disable_drift:
        ``Board.disable_drift`` — the drift kinds the board wants
        suppressed. Translated into the built-in judges to drop from
        the default set (see :mod:`zicato.judge_runtime.disable`).
    aux_call_llm:
        zicato's auxiliary LLM callable (``RuntimeConfig.auxiliary_call_llm``).
        Inline judges use it; python judges ignore it. Per the
        two-callable rule this is NOT the harness callable — judges
        must not run on the same LLM surface as the agent they grade.
    io_sink:
        Optional :class:`zicato.judge_runtime.io_capture.JudgeIOSink`
        threaded into every CUSTOM inline judge built here (board
        reflection's verbatim-capture seam; best-effort, never affects
        a verdict). ``None`` (the default) captures nothing — the
        assembled list is byte-identical to before the parameter
        existed. goldfive's built-in judges are not wrapped (they are
        not zicato ``JudgeSpec`` judges); python-mode judges are
        inline-only for now (:mod:`zicato.judge_runtime.io_capture`).

    Returns
    -------
    list[goldfive.Judge]
        ``[<default built-ins minus suppressed>..., <custom judges>...]``.
        Hand this straight to ``goldfive.run(..., judges=...)`` /
        ``goldfive.wrap(judges=...)``.

        Built-ins stay default-on except those a ``disable_drift`` kind
        named. The list is never empty as long as goldfive ships
        default judges and nothing suppressed them all — but an empty
        list is a legal goldfive ``judges=`` value (opt-out token), so
        callers need no special-casing.
    """
    suppressed = builtin_judge_names_to_suppress(disable_drift)
    judges: list[Any] = default_judges_minus(suppressed)
    if suppressed:
        log.debug(
            "judge_runtime: suppressed %d built-in judge(s) per Board.disable_drift: %s",
            len(suppressed),
            sorted(suppressed),
        )

    for spec in entry_judges or ():
        judge = judge_spec_to_goldfive(spec, aux_call_llm, io_sink=io_sink)
        judges.append(judge)

    return judges


__all__ = ["assemble_judges"]
