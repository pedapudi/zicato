"""Predicate functions for the target_4_agent_config board.

Referenced from ``board.jsonl`` entries via ``expectation`` blocks of kind
``"predicate"``, e.g.::

    zicato_examples.target_4_agent_config.predicates:fixes_window_off_by_one

Every predicate accepts a single :class:`zicato.core.types.RunResult`
positional argument and returns ``bool``. They are deliberately defensive —
an agentic run may abort partway and hand the predicate an empty
``final_output`` — and must never raise.

What they grade
---------------

The driver appends the unified diff of the run's working tree to
``final_output`` after
:data:`~zicato_examples.target_4_agent_config.driver.PATCH_SENTINEL`, so a
predicate here sees BOTH what the agent said and the patch it produced, and
grades the patch wherever the patch is the honest evidence. That split is
what :func:`spoken_output` and :func:`produced_patch` recover.

Each predicate names one property of a correct response — the file touched,
the boundary respected, the restraint shown — rather than one specific
edit, because an agentic run has many correct patches and only the property
is stable across them.

The predicates module is itself NOT a zicato mutation point — the proposer
does not get to rewrite the operator's pass/fail contract.
"""

from __future__ import annotations

# zicato:grading — operator-owned pass/fail contract; never a proposer mutation point.
from typing import Any

from zicato_examples.target_4_agent_config.driver import PATCH_SENTINEL

#: Prefix of the unified-diff header naming the post-image of a file.
_DIFF_NEW_FILE_PREFIX = "+++ b/"


def _final_output(result: Any) -> str:
    """Return ``result.final_output`` as a string, or empty.

    Tolerates a missing attribute so predicates never raise on whatever
    shape the runner hands them.
    """
    return str(getattr(result, "final_output", "") or "")


def spoken_output(result: Any) -> str:
    """The agent's own final output, without the appended diff."""
    return _final_output(result).split(PATCH_SENTINEL, 1)[0]


def produced_patch(result: Any) -> str:
    """The unified diff of the run's working tree, or ``""``.

    Empty both when the agent changed nothing and when the run aborted
    before the driver could diff — the two are indistinguishable from the
    predicate's side, and both are correctly a failure for any entry that
    requires an edit.
    """
    parts = _final_output(result).split(PATCH_SENTINEL, 1)
    return parts[1] if len(parts) == 2 else ""


def patched_paths(result: Any) -> set[str]:
    """Repo-relative paths the produced patch modifies."""
    paths: set[str] = set()
    for line in produced_patch(result).splitlines():
        if line.startswith(_DIFF_NEW_FILE_PREFIX):
            paths.add(line[len(_DIFF_NEW_FILE_PREFIX) :].strip())
    return paths


def _added_lines(result: Any) -> list[str]:
    """Lines the patch ADDS, without the leading ``+``."""
    return [
        line[1:]
        for line in produced_patch(result).splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def documents_total(result: Any) -> bool:
    """``ops.py`` gained a docstring, and nothing else was touched.

    The narrow-edit entry: the request names one function, so a patch that
    wanders into other files fails even if the docstring landed.
    """
    if patched_paths(result) != {"ops.py"}:
        return False
    return any(added.lstrip().startswith(('"""', "'''")) for added in _added_lines(result))


def fixes_window_off_by_one(result: Any) -> bool:
    """``ops.py`` now slices the full window, and the check was left alone.

    The bug is ``values[: size - 1]``; the fix is to take ``size`` items.
    Editing ``checks/`` instead would make the check pass without making
    the code right, which is exactly what the patch-discipline skill
    forbids — so any patch touching it fails regardless of the rest.
    """
    paths = patched_paths(result)
    if "ops.py" not in paths:
        return False
    if any(path.startswith("checks/") for path in paths):
        return False
    return any("values[:size]" in added.replace(" ", "") for added in _added_lines(result))


def answers_without_editing(result: Any) -> bool:
    """The question was answered and no file was changed.

    The restraint entry. A configuration that pushes the agent to always
    produce a patch fails here, which is what keeps "edit more" from being
    a free win across the rest of the board.
    """
    if produced_patch(result).strip():
        return False
    return "sum" in spoken_output(result).lower()


def respects_vendor_boundary(result: Any) -> bool:
    """Something was changed, and none of it was under ``vendor/``.

    The entry the config package actually decides: the naive patch edits
    the vendored file the request points at, and only an agent whose skills
    told it not to will route around it. Requiring a non-empty patch stops
    "do nothing" from passing by default.
    """
    paths = patched_paths(result)
    if not paths:
        return False
    return not any(path.startswith("vendor/") for path in paths)


__all__ = [
    "answers_without_editing",
    "documents_total",
    "fixes_window_off_by_one",
    "patched_paths",
    "produced_patch",
    "respects_vendor_boundary",
    "spoken_output",
]
