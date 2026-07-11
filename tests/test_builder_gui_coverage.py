"""Machine-pin of invariant L2: every builder WRITE / LIFECYCLE op has a GUI.

The copilot's :data:`~zicato.builder.copilot_tools.DEFAULT_BUILDER_TOOLS` is
already machine-pinned to the full op surface (see
``test_builder_copilot.py::test_default_builder_tools_registry_covers_every_op``).
This test builds on that pin to close L2's *GUI* surface: it derives the set of
mutating + lifecycle ops from the SAME registry, then asserts each op name is
wired to a control in the builder frontend source — as ``runOp('<op>'`` or
``postOp('<op>'`` in ``views/builder.js`` or ``builder/entry_form.js`` — OR
carries an explicit, justified entry in :data:`GUI_EXCEPTIONS`.

A NEW op added to ``operations.py`` + the copilot registry without either a GUI
control or a documented exception REDS this suite, with a failure message that
names the missing op and its two remedies. This is the enforcement seam the
dev-guide §10.7 surface-4 column points at.

Read-only doctrine: the pure READ tools (cost / validation / preflight
measurement) and the READ-only lifecycle tools (list / compare / dry-run
preview) are NOT required to have a mutating control — several happen to have
one anyway (the Review pane runs preflight + compare), but the pin does not
demand it, since they change nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

# Locate the builder frontend source via the installed dashboard package, the
# same way the other dashboard-static tests do (works from an installed wheel
# or the source tree).
import zicato.dashboard as _dashboard_pkg
from zicato.builder.copilot_tools import DEFAULT_BUILDER_TOOLS

STATIC_DIR = Path(_dashboard_pkg.__file__).resolve().parent / "static"
_BUILDER_JS = STATIC_DIR / "js" / "views" / "builder.js"
_ENTRY_FORM_JS = STATIC_DIR / "js" / "builder" / "entry_form.js"

#: The PURE-READ + READ-ONLY-LIFECYCLE tools, excluded from the GUI-control
#: requirement (they mutate nothing, so a mutating control is not owed). Each
#: name MUST be a real member of ``DEFAULT_BUILDER_TOOLS`` — a rename that drops
#: one from the registry is caught by :func:`test_pure_read_tools_are_registry_members`.
PURE_READ_TOOLS = frozenset(
    {
        "estimate_cost",  # cost meter — read
        "validate",  # advisory warnings — read
        "preflight",  # A/A-floor measurement — read (spends budget, mutates nothing)
        "list_drafts",  # enumerate slots — read
        "compare",  # keyed draft diff — read
        "preview_apply",  # dry-run-only apply preview — read
    }
)

#: Ops that GENUINELY lack a dedicated GUI control, each with a one-line
#: justification. Any op here must be a real write/lifecycle op (guarded below)
#: and must NOT actually have a control (guarded below) — a stale exception reds
#: the suite just like a missing control, so the doctrine can never rot.
GUI_EXCEPTIONS: dict[str, str] = {
    "add_judge": (
        "judge authoring rides the whole-entry edit_board_entry round-trip — the "
        "board editor's judges list editor (builder/entry_form.js) mutates the "
        "entry buffer and Save posts edit_board_entry, so the granular add_judge "
        "op stays copilot/CLI-only rather than opening a SECOND judge-authoring "
        "path (an L1 smell). remove_judge keeps a direct entry-badge × control as "
        "an at-a-glance convenience; adding one needs the full JudgeSpec form."
    ),
}


def _tool_names() -> set[str]:
    return {t.__name__ for t in DEFAULT_BUILDER_TOOLS}


def _required_ops() -> set[str]:
    """The write + lifecycle ops that must have a GUI control or an exception."""
    return _tool_names() - PURE_READ_TOOLS


def _frontend_source() -> str:
    js = _BUILDER_JS.read_text(encoding="utf-8")
    entry_form = _ENTRY_FORM_JS.read_text(encoding="utf-8")
    return js + "\n" + entry_form


def _has_control(op: str, source: str) -> bool:
    """True iff the op is wired as ``runOp('<op>'`` / ``postOp('<op>'``.

    Matches the single- OR double-quoted form so a future switch of quote style
    does not silently drop the pin.
    """
    pattern = re.compile(r"""(runOp|postOp)\(\s*['"]""" + re.escape(op) + r"""['"]""")
    return bool(pattern.search(source))


def test_pure_read_tools_are_registry_members() -> None:
    """The read-tool exclusion list can only name real registry tools.

    Guards against a rename that would silently over-exclude (drop a now-write op
    from the required set by leaving its old name in PURE_READ_TOOLS)."""
    names = _tool_names()
    stale = PURE_READ_TOOLS - names
    assert not stale, (
        f"PURE_READ_TOOLS names non-registry tool(s) {sorted(stale)} — a rename "
        "left a stale exclusion; update PURE_READ_TOOLS to the current tool name."
    )


def test_every_registry_tool_is_classified() -> None:
    """No tool escapes classification — every registry name is either a required
    write/lifecycle op or an excluded pure-read. A new tool that is neither reds
    here so it cannot slip past the coverage pin unnoticed."""
    names = _tool_names()
    classified = _required_ops() | PURE_READ_TOOLS
    assert names == classified, f"unclassified tool(s): {sorted(names - classified)}"


def test_gui_exceptions_are_real_uncontrolled_write_ops() -> None:
    """Every GUI_EXCEPTIONS entry is a real write/lifecycle op that TRULY lacks a
    control — a stale exception (op that has since gained a control, or a
    read/unknown op) reds the suite so the exception list cannot rot."""
    required = _required_ops()
    source = _frontend_source()
    for op, justification in GUI_EXCEPTIONS.items():
        assert op in required, (
            f"GUI_EXCEPTIONS names {op!r}, which is not a write/lifecycle op "
            f"(known: {sorted(required)}) — drop it or fix the name."
        )
        assert justification.strip(), f"GUI_EXCEPTIONS[{op!r}] needs a justification."
        assert not _has_control(op, source), (
            f"GUI_EXCEPTIONS still lists {op!r}, but the frontend now wires a "
            "control for it — REMOVE the stale exception (the op is covered)."
        )


def test_every_write_op_has_a_gui_control_or_exception() -> None:
    """THE PIN: every builder write / lifecycle op is reachable from the GUI.

    Each mutating op must appear as ``runOp('<op>'`` / ``postOp('<op>'`` in the
    builder frontend source, or carry a justified GUI_EXCEPTIONS entry. A new op
    added to operations.py + the copilot registry without either reds here."""
    source = _frontend_source()
    missing = [
        op
        for op in sorted(_required_ops())
        if op not in GUI_EXCEPTIONS and not _has_control(op, source)
    ]
    assert not missing, (
        "builder op(s) with no GUI control and no exception: "
        f"{missing}. Two remedies — (1) wire a control in views/builder.js or "
        "builder/entry_form.js that drives the op via runOp('<op>', …) / "
        "postOp('<op>', …); or (2) if the op is deliberately copilot/CLI-only, "
        "add it to GUI_EXCEPTIONS in this file with a one-line justification."
    )
