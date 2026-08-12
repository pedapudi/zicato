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
    #: Refreshes that found nothing to do. The no-op-heartbeat guard.
    noops: int = 0
    #: Where ``b`` goes back to.
    history: list[Route] = field(default_factory=list)
    #: The last command a drill handed to the shell, for the app to run.
    pending_command: str | None = None
    filter: str = ""

    # -- rendering ---------------------------------------------------------

    @property
    def lens_name(self) -> str:
        return self.route.lens

    @property
    def context(self) -> LensContext:
        return LensContext(route=self.route, width=self.width, ascii_only=self.ascii_only)

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
        """The selectable rows, after the filter."""
        if self.view is None:
            return []
        rows = self.view.selectable_rows()
        if not self.filter:
            return rows
        needle = self.filter.lower()
        return [r for r in rows if needle in r.text.lower()]

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

    def set_filter(self, text: str) -> None:
        self.filter = text
        self.cursor = 0

    # -- navigation --------------------------------------------------------

    def goto(self, route: Route | str, *, remember: bool = True) -> None:
        """Switch lens/route. Resets the cursor; the previous route is pushed."""
        target = parse_route(route) if isinstance(route, str) else route
        if remember and (target.lens, target.params) != (self.route.lens, self.route.params):
            self.history.append(self.route)
        self.route = target
        self.view = None
        self.cursor = 0
        self.filter = ""

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
        self.filter = ""
        return True

    def drill(self) -> str | None:
        """``enter`` on the selected row.

        A row's action is either a route (open that lens) or a shell command
        (prefixed ``!``). A command is NOT run here — it is parked on
        :attr:`pending_command` for the app to run in the operator's shell, so
        the one place that executes anything stays the app's own suspend path.
        """
        selected = self.selected
        if selected is None or selected.action is None:
            return None
        action = selected.action
        if action.startswith("!"):
            self.pending_command = action[1:]
            return self.pending_command
        route = parse_route(action)
        epoch = self.route.params.get("epoch")
        if epoch and "epoch" not in route.params:
            route = Route(
                lens=route.lens,
                params={**route.params, "epoch": epoch},
                unsupported=route.unsupported,
            )
        self.goto(route)
        return None

    def take_command(self) -> str | None:
        """Hand the parked shell command to the caller, exactly once."""
        command, self.pending_command = self.pending_command, None
        return command

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
