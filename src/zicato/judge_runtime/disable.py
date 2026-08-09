"""Translate a board's ``disable_drift`` into goldfive's judge config.

A :class:`zicato.core.Board` carries
``disable_drift: tuple[goldfive.DriftKind, ...]`` — the drift kinds the
operator wants *suppressed* for that board (e.g. a board that
deliberately exercises tool failures does not want ``tool_error`` drift
counted against the agent's loss).

goldfive's pluggable-judge surface (``goldfive#437``) has exactly one
operator-facing lever for *which* signals are armed: the ``judges=``
list passed to :func:`goldfive.wrap`. There is no per-drift-kind
"off switch" on the legacy detector path. So "suppress drift kind K"
translates to: **build the goldfive judge list from
``builtin_judges.default_judges()`` minus the built-in judge that
emits K** — dropping that judge removes its
:class:`JudgementEmitted` envelope (and excludes it from the custom-judge
dispatch path) for the run.

goldfive's built-in judges otherwise stay default-on: this module starts
from the *full* default set and removes only the judges the board
explicitly named, then the adapter appends the entry's custom judges.

:data:`_DRIFT_KIND_TO_BUILTIN` is the mapping from a drift-kind wire
string to the built-in judge factory name that emits it. It is the one
place that knows the correspondence; it is intentionally conservative
(only the kinds a *built-in* judge can emit appear). A ``disable_drift``
entry naming a kind no built-in judge emits is a no-op — the adapter
logs it and moves on.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    import goldfive

log = logging.getLogger("zicato.judge_runtime.disable")


# ---------------------------------------------------------------------------
# drift-kind -> built-in judge name
# ---------------------------------------------------------------------------
#
# Keyed on the lowercase wire string of a ``goldfive.DriftKind`` member.
# Values are the ``goldfive.builtin_judges`` factory names (== each
# judge's ``.name``). Derived from goldfive's built-in detector wrappers
# (``goldfive/judges/builtins.py``):
#
#   * refusal()          -> classify_refusal()        -> AGENT_REFUSAL
#   * tool_error()       -> classify_tool_error()     -> TOOL_ERROR
#   * stop_reason()      -> classify_stop_reason()    -> CONTEXT_PRESSURE
#   * looping_reasoning()-> detect_looping_reasoning()-> LOOPING_REASONING
#   * goal_drift()       -> classify_goal_drift()     -> GOAL_DRIFT
#   * reasoning_drift()  -> reasoning judge           -> OFF_TOPIC /
#                            INTENT_DIVERGENCE / JUSTIFIED_DEVIATION
#   * looping_tool()     -> ToolLoopTracker           -> LOOPING_TOOL_CALL
#
# A kind not listed here is not emitted by any built-in judge, so there
# is no built-in to drop for it (the adapter treats that as a no-op).
_DRIFT_KIND_TO_BUILTIN: dict[str, str] = {
    "agent_refusal": "refusal",
    "tool_error": "tool_error",
    "context_pressure": "stop_reason",
    "looping_reasoning": "looping_reasoning",
    "reasoning_cluster_tightening": "looping_reasoning",
    "goal_drift": "goal_drift",
    "off_topic": "reasoning_drift",
    "intent_divergence": "reasoning_drift",
    "justified_deviation": "reasoning_drift",
    "looping_tool_call": "looping_tool",
}


def kind_to_wire_string(kind: Any) -> str:
    """Project a :class:`goldfive.DriftKind` (or string) to its wire form.

    ``DriftKind`` is a :class:`enum.StrEnum`, so ``str(member)`` is the
    lowercase canonical value. A bare string is accepted unchanged
    (idempotent); a stray ``"DriftKind.TOOL_ERROR"`` repr is normalised
    to its last dotted segment, lowercased.

    Public so the epoch contract canonicalizer reduces a board's
    ``disable_drift`` to the same wire form this module dispatches on.
    """
    text = str(kind).strip()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text.lower()


def builtin_judge_names_to_suppress(
    disable_drift: tuple[Any, ...] | list[Any] | None,
) -> set[str]:
    """Map a ``disable_drift`` tuple to the built-in judge names to drop.

    Parameters
    ----------
    disable_drift:
        ``Board.disable_drift`` — an iterable of
        :class:`goldfive.DriftKind` members (strings tolerated). ``None``
        / empty yields an empty set.

    Returns
    -------
    set[str]
        The ``goldfive.builtin_judges`` factory names whose judges emit
        a drift kind named in ``disable_drift``. Drift kinds that no
        built-in judge emits are skipped (logged at DEBUG); they are not
        an error — they simply have no built-in to suppress.
    """
    if not disable_drift:
        return set()
    suppress: set[str] = set()
    for kind in disable_drift:
        wire = kind_to_wire_string(kind)
        judge_name = _DRIFT_KIND_TO_BUILTIN.get(wire)
        if judge_name is None:
            log.debug(
                "judge_runtime: disable_drift kind %r is not emitted by any "
                "built-in judge; nothing to suppress",
                kind,
            )
            continue
        suppress.add(judge_name)
    return suppress


def default_judges_minus(
    suppressed_names: set[str],
) -> list[goldfive.Judge]:
    """Return ``builtin_judges.default_judges()`` minus ``suppressed_names``.

    Starts from goldfive's full default judge set — built-ins stay
    default-on — and drops every judge whose ``.name`` is in
    ``suppressed_names`` (the output of
    :func:`builtin_judge_names_to_suppress`). The returned list is the
    built-in half of what the adapter passes to
    ``goldfive.wrap(judges=[...])``; the adapter appends the board
    entry's custom judges to it.
    """
    from goldfive import builtin_judges

    return [
        judge
        for judge in builtin_judges.default_judges()
        if str(getattr(judge, "name", "") or "") not in suppressed_names
    ]


__all__ = [
    "builtin_judge_names_to_suppress",
    "default_judges_minus",
    "kind_to_wire_string",
]
