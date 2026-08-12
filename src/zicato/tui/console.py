"""The console controller — navigation and the repaint discipline.

Everything the TUI *does* lives here, with no Textual anywhere in sight: which
lens is showing, where the cursor is, what the back stack holds, and — the part
that matters most — whether a refresh should touch the screen at all.

**The digest discipline.** The browser's rule is "never rebuild DOM on a no-op
heartbeat". Here it is stated twice over:

* :attr:`Console.repaints` counts refreshes that changed the view's digest. A
  no-op SSE heartbeat re-fetches, folds an identical digest, and repaints zero
  times.
* :attr:`Console.row_patches` counts the individual LINES whose text changed.
  Even a real change patches only the rows that moved, so one candidate's
  scalar landing does not rewrite the whole standings table.

A TUI that flickers on every tick has failed the same way the dashboard once
did, and these two counters are what the test asserts on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zicato.tui import present
from zicato.tui.client import Client
from zicato.tui.lenses import BY_NAME, LENSES, LensContext, safe_render
from zicato.tui.routes import Route, parse_route
from zicato.tui.view import Row, View


@dataclass
class Console:
    """The whole interactive model: lens, route, cursor, history, counters."""

    client: Client
    route: Route = field(default_factory=Route)
    width: int = 100
    ascii_only: bool = False

    view: View | None = None
    cursor: int = 0
    #: Refreshes that actually changed the painted content.
    repaints: int = 0
    #: Individual lines rewritten across every repaint.
    row_patches: int = 0
    #: Refreshes that found nothing to do. The INNER (digest) guard.
    noops: int = 0
    #: ``state_change`` frames dropped without a single fetch. The OUTER
    #: (seq) guard — the one that makes a no-op beat genuinely free.
    skipped_frames: int = 0
    #: The highest progress cursor seen. ``-1`` until the first frame.
    last_seq: int = -1
    #: The served terminal flag: True once the tail event marks a settled run.
    #: Distinguishes a finished loop from one stalled with a frozen ``seq``.
    terminal: bool = False
    #: Where ``b`` goes back to.
    history: list[Route] = field(default_factory=list)

    # -- rendering ---------------------------------------------------------

    @property
    def lens_name(self) -> str:
        return self.route.lens

    @property
    def context(self) -> LensContext:
        return LensContext(route=self.route, width=self.width, ascii_only=self.ascii_only)

    def note_progress(self, seq: Any, terminal: Any = None) -> bool:
        """Fold a ``state_change`` frame's cursor. True ⇒ this frame is work.

        THE OUTER GATE. The SSE stream carries no digest, so ``seq`` is the
        only thing that can tell a real change from a no-op beat *before* any
        HTTP request is made. A repeated ``seq`` returns False and the caller
        does not fetch at all — which is the difference between "we refetched
        the whole workspace and then decided not to repaint" and "we did
        nothing". The digest gate in :meth:`refresh` is the inner guard behind
        it, for the changes ``seq`` cannot see (a reindex, an operator edit).
        """
        progress = present.note_progress(seq, terminal, self.last_seq)
        if progress.present:
            if progress.rollover:
                # The run RESTARTED: the log was cleared and seq begins at 1
                # again. Adopt the low cursor and force a full re-apply —
                # holding the old high-water mark would freeze the screen on
                # the finished run for the whole of the next one.
                self.last_seq = int(present.num(seq) or 0)
                self.view = None
            elif progress.advanced:
                self.last_seq = int(present.num(seq) or 0)
        if terminal is not None:
            self.terminal = bool(terminal)
        if not progress.should_refresh:
            self.skipped_frames += 1
            return False
        return True

    def refresh(self) -> bool:
        """Re-fetch and re-render. Returns True when the screen changed.

        The digest comparison happens BEFORE anything is handed to a renderer,
        so an unchanged payload costs one fetch pass and zero screen work.
        """
        begin = getattr(self.client, "begin_pass", None)
        if callable(begin):
            begin()
        lens = BY_NAME[self.route.lens]
        fresh = safe_render(lens, self.client, self.context)
        previous = self.view
        if previous is not None and previous.digest == fresh.digest:
            self.noops += 1
            return False
        self.row_patches += _changed_rows(previous, fresh)
        self.repaints += 1
        self.view = fresh
        self._clamp_cursor()
        return True

    def _clamp_cursor(self) -> None:
        rows = self.rows
        if not rows:
            self.cursor = 0
            return
        self.cursor = max(0, min(self.cursor, len(rows) - 1))

    # -- selection ---------------------------------------------------------

    @property
    def rows(self) -> list[Row]:
        """The rows the cursor can land on."""
        return self.view.selectable_rows() if self.view is not None else []

    @property
    def selected(self) -> Row | None:
        rows = self.rows
        return rows[self.cursor] if rows and 0 <= self.cursor < len(rows) else None

    def move(self, delta: int) -> None:
        """Move the cursor, clamped at both ends — never wrapping.

        Wrapping in a long table loses the operator's place; a cursor that
        stops at the end says "this is the end" without costing a keystroke.
        """
        rows = self.rows
        if not rows:
            self.cursor = 0
            return
        self.cursor = max(0, min(self.cursor + delta, len(rows) - 1))

    # -- navigation --------------------------------------------------------

    def goto(self, route: Route | str, *, remember: bool = True) -> None:
        """Switch lens/route. Resets the cursor; the previous route is pushed."""
        target = parse_route(route) if isinstance(route, str) else route
        if remember and (target.lens, target.params) != (self.route.lens, self.route.params):
            self.history.append(self.route)
        self.route = target
        self.view = None
        self.cursor = 0

    def jump(self, index: int) -> None:
        """``1``-``6``: jump to the lens at that rail position, keeping the epoch."""
        if not 1 <= index <= len(LENSES):
            return
        lens = LENSES[index - 1]
        epoch = self.route.params.get("epoch")
        self.goto(Route(lens=lens.name, params={"epoch": epoch} if epoch else {}))

    def back(self) -> bool:
        """Pop the history. Returns False when there is nowhere to go back to."""
        if not self.history:
            return False
        self.route = self.history.pop()
        self.view = None
        self.cursor = 0
        return True

    def drill(self) -> None:
        """``enter`` on the selected row — open the route it names.

        A row's action is ALWAYS a route. This build executes nothing: the
        Instrument queue prints its apply command for the operator to run, so
        there is no code path here that can mutate a workspace.
        """
        selected = self.selected
        if selected is None or selected.action is None:
            return
        route = parse_route(selected.action)
        epoch = self.route.params.get("epoch")
        if epoch and "epoch" not in route.params:
            route = Route(
                lens=route.lens,
                params={**route.params, "epoch": epoch},
                unsupported=route.unsupported,
            )
        self.goto(route)

    def resize(self, width: int) -> None:
        """Adopt a new terminal width, re-rendering only if the layout changes.

        Width is a RENDER input (columns collapse under ~100), so a width
        change must invalidate the cached view — but only when it crosses the
        narrow threshold or changes a column width, which the digest cannot
        see. Forcing the next refresh to repaint is the honest, cheap answer.
        """
        if width == self.width:
            return
        self.width = width
        self.view = None


def _changed_rows(previous: View | None, fresh: View) -> int:
    """Count the LINES a repaint must rewrite.

    Rows are matched by their stable key, so a row whose neighbours moved is
    not counted as changed. A first paint counts every row: there is nothing on
    screen yet, and pretending otherwise would make the counter a lie.
    """
    new_rows = fresh.lines()
    if previous is None:
        return len(new_rows)
    old = {key: r.text for key, r in previous.lines()}
    changed = sum(1 for key, r in new_rows if old.get(key) != r.text)
    dropped = len(old.keys() - {key for key, _ in new_rows})
    return changed + dropped


__all__ = ["Console"]
