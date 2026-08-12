"""The Textual app — the thin layer between :class:`Console` and a terminal.

Deliberately thin. Every decision about what to show and whether to show it
again lives in :mod:`zicato.tui.console`; this module owns pixels, keys and the
SSE worker, and nothing else.

Three regions, as the design calls for: a one-line **status band**, a **rail**
of lenses that collapses to a top strip under ~100 columns, and the **content
region** with an **evidence drawer** that opens on selection. No box drawing
around every panel — whitespace and typographic weight do the separating, the
way the browser Console does it with type rather than borders.

Repaint discipline: content rows are individual widgets keyed by their
view-unique key, and a repaint updates only the ones whose text changed. A
no-op SSE heartbeat never gets that far — :meth:`Console.refresh` folds an
identical digest and returns without touching a widget.
"""

from __future__ import annotations

import os
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from zicato.tui.client import Client, HttpClient
from zicato.tui.console import Console
from zicato.tui.lenses import LENSES
from zicato.tui.routes import Route
from zicato.tui.view import Row, to_ascii

#: Semantic token to Textual style. ONE accent (the zicato green) is spent on
#: champion / promotion / "what is happening now"; the rest are the verdict
#: vocabulary's severities and a dim for provisional and degraded. Never
#: decorative — every colour here is redundant with a word on the same line.
STYLE = {
    "plain": "",
    "bold": "bold",
    "faint": "dim",
    "accent": "bold #2f9e44",
    "good": "#2f9e44",
    "bad": "#e03131",
    "warn": "#e8590c",
}

CSS = """
Screen { background: $surface; }
#band { height: 1; padding: 0 1; background: $panel; color: $text; }
#rail { width: 16; padding: 1 1; }
#rail.narrow { width: 100%; height: 1; padding: 0 1; }
#body { height: 1fr; }
#content { padding: 0 2; height: 1fr; }
#drawer { height: auto; max-height: 9; padding: 0 2; background: $panel; }
.railitem { height: 1; }
.cursor { background: $boost; }
"""


class ZicatoTui(App[None]):
    """``zicato tui`` — the Console in the terminal."""

    CSS = CSS
    TITLE = "zicato"

    BINDINGS = [
        Binding("j,down", "move(1)", "down", show=False),
        Binding("k,up", "move(-1)", "up", show=False),
        Binding("enter", "drill", "open"),
        Binding("b,escape", "go_back", "back"),
        Binding("slash", "filter", "filter"),
        Binding("a", "apply", "apply"),
        Binding("r", "reload", "reload"),
        Binding("question_mark", "help", "help"),
        Binding("q,ctrl+c", "quit", "quit"),
        *[
            Binding(str(i + 1), f"jump({i + 1})", lens.label, show=i < 6)
            for i, lens in enumerate(LENSES)
        ],
    ]

    def __init__(
        self,
        client: Client,
        *,
        route: Route | None = None,
        url: str = "",
        workspace: str = "",
        ascii_only: bool | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        super().__init__()
        self.console_model = Console(
            client=client,
            route=route or Route(),
            ascii_only=ascii_only if ascii_only is not None else degrade_to_ascii(),
        )
        self.url = url
        self.workspace = workspace
        self.poll_seconds = poll_seconds
        self.connection = "connecting"
        self._row_widgets: dict[str, Static] = {}
        # The text each row widget currently shows. Kept HERE rather than read
        # back off the widget: the comparison that decides whether to patch is
        # the repaint discipline itself, and it must not depend on a
        # framework's internal renderable representation.
        self._row_text: dict[str, str] = {}
        self._cursor_key: str | None = None
        self._help_open = False

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="band")
        with Horizontal(id="body"):
            with Vertical(id="rail"):
                for i, lens in enumerate(LENSES):
                    yield Static(
                        f"{i + 1} {lens.label}", id=f"rail-{lens.name}", classes="railitem"
                    )
            with Vertical():
                yield VerticalScroll(id="content")
                yield Static("", id="drawer")

    def on_mount(self) -> None:
        self.reload()
        self.set_interval(self.poll_seconds, self.reload)
        # The SSE stream is the LIVE signal; the poll above is the fallback for
        # a service that closed the stream. Both funnel into the same digest
        # gate, so the two together never repaint more than the data changes.
        self.run_worker(self._stream, thread=True, exclusive=True, name="sse")

    def on_resize(self) -> None:
        self.console_model.resize(self.size.width)
        rail = self.query_one("#rail")
        rail.set_class(self.console_model.context.narrow, "narrow")
        self.reload()

    # -- data --------------------------------------------------------------

    def reload(self) -> None:
        """One refresh pass. Repaints only when the digest moved."""
        changed = self.console_model.refresh()
        view = self.console_model.view
        if view is not None:
            self.connection = "degraded" if view.degraded else "live"
        self._paint_band()
        if changed:
            self._paint_content()
        self._paint_drawer()
        self._paint_rail()

    def _stream(self) -> None:
        client = self.console_model.client
        if not isinstance(client, HttpClient):
            return
        while not self._is_closing():
            for _event in client.events():
                if self._is_closing():
                    return
                self.call_from_thread(self.reload)
            if self._is_closing():
                return
            # The stream ended. The interval poll keeps the screen honest while
            # we wait to reconnect, and the band already says "polling".
            self.connection = "polling"
            return

    def _is_closing(self) -> bool:
        return not self.is_running

    # -- painting ----------------------------------------------------------

    def _paint_band(self) -> None:
        view = self.console_model.view
        parts = [
            self.workspace or self.url or "zicato",
            str(view.title) if view else "…",
            f"[{self.connection}]",
        ]
        if self.console_model.filter:
            parts.append(f"/{self.console_model.filter}")
        if self.console_model.route.unsupported:
            parts.append(f"{self.console_model.route.unsupported} stays in the browser")
        text = " · ".join(parts)
        self.query_one("#band", Static).update(self._text(text))

    def _paint_rail(self) -> None:
        for lens in LENSES:
            widget = self.query_one(f"#rail-{lens.name}", Static)
            widget.set_class(lens.name == self.console_model.lens_name, "cursor")

    def _paint_content(self) -> None:
        """Reconcile row widgets by key — update only what actually changed."""
        view = self.console_model.view
        if view is None:
            return
        container = self.query_one("#content", VerticalScroll)
        wanted = view.lines()
        wanted_keys = [key for key, _ in wanted]

        # A row that MOVED cannot be patched in place without reordering
        # widgets, which costs more than a clean remount and risks a wrong
        # order on screen. Reordering is rare (a re-sorted table); a pure
        # value change — the common case, and the one the discipline is about —
        # keeps every widget and patches only the lines that differ.
        kept = [key for key in self._row_widgets if key in set(wanted_keys)]
        if kept != [key for key in wanted_keys if key in self._row_widgets]:
            self._reset_rows()

        for key in list(self._row_widgets):
            if key not in set(wanted_keys):
                self._row_widgets.pop(key).remove()
                self._row_text.pop(key, None)

        for index, (key, row) in enumerate(wanted):
            renderable = self._row_renderable(row)
            widget = self._row_widgets.get(key)
            if widget is None:
                widget = Static(renderable)
                self._row_widgets[key] = widget
                if index < len(container.children):
                    container.mount(widget, before=index)
                else:
                    container.mount(widget)
            elif self._row_text.get(key) != str(renderable):
                widget.update(renderable)
            self._row_text[key] = str(renderable)
        self._paint_cursor()

    def _paint_cursor(self) -> None:
        selected = self.console_model.selected
        key = None
        if selected is not None:
            view = self.console_model.view
            key = next(
                (k for k, r in (view.lines() if view else []) if r is selected),
                None,
            )
        if key == self._cursor_key:
            return
        if self._cursor_key and self._cursor_key in self._row_widgets:
            self._row_widgets[self._cursor_key].set_class(False, "cursor")
        if key and key in self._row_widgets:
            self._row_widgets[key].set_class(True, "cursor")
        self._cursor_key = key

    def _paint_drawer(self) -> None:
        selected = self.console_model.selected
        drawer = self.query_one("#drawer", Static)
        if self._help_open:
            drawer.update(self._text(HELP))
            return
        if selected is None or not selected.evidence:
            drawer.update(self._text(""))
            return
        lines = [f"{label:<13} {value}" for label, value in selected.evidence]
        drawer.update(self._text("\n".join(lines)))

    def _row_renderable(self, row: Row) -> Any:
        text = Text()
        text.append(" " * (row.indent * 2))
        for span in row.spans:
            text.append(self._chars(span.text), STYLE.get(span.style, ""))
        return text

    def _text(self, value: str) -> Any:
        return Text(self._chars(value))

    def _chars(self, value: str) -> str:
        return to_ascii(value) if self.console_model.ascii_only else value

    # -- actions -----------------------------------------------------------

    def action_move(self, delta: int) -> None:
        self.console_model.move(delta)
        self._paint_cursor()
        self._paint_drawer()

    def action_jump(self, index: int) -> None:
        self.console_model.jump(index)
        self._reset_rows()
        self.reload()

    def action_go_back(self) -> None:
        # NOT ``action_back``: Textual's App already owns that name for screen
        # navigation, and shadowing it with a sync method breaks the override.
        if self._help_open:
            self._help_open = False
            self._paint_drawer()
            return
        if self.console_model.back():
            self._reset_rows()
            self.reload()

    def action_drill(self) -> None:
        command = self.console_model.drill()
        if command is None:
            self._reset_rows()
            self.reload()
            return
        self._run_command(command)

    def action_apply(self) -> None:
        selected = self.console_model.selected
        if selected is None or not (selected.action or "").startswith("!"):
            self.bell()
            return
        self.action_drill()

    def action_reload(self) -> None:
        self.console_model.view = None
        self._reset_rows()
        self.reload()

    def action_filter(self) -> None:
        self.push_screen(FilterScreen(self.console_model.filter), self._filter_done)

    def _filter_done(self, text: str | None) -> None:
        if text is None:
            return
        self.console_model.set_filter(text)
        self._paint_cursor()
        self._paint_drawer()
        self._paint_band()

    def action_help(self) -> None:
        self._help_open = not self._help_open
        self._paint_drawer()

    def _reset_rows(self) -> None:
        for widget in self._row_widgets.values():
            widget.remove()
        self._row_widgets.clear()
        self._row_text.clear()
        self._cursor_key = None

    def _run_command(self, command: str) -> None:
        """Shell out to the CLI — the ONLY way this surface mutates anything.

        Suspending hands the terminal back so the command's own output and
        prompts belong to the operator, and the audit trail is identical to
        having typed it.
        """
        import subprocess

        self.console_model.take_command()
        with self.suspend():
            subprocess.run(command, shell=True, check=False)  # noqa: S602
        self.action_reload()


HELP = """\
j / k or arrows   move      enter  open        b / esc  back
/  filter         a  apply  r  reload          ?  help   q  quit
1-6               Home · Standings · Candidate · Board · Instrument · Health
apply shells to the CLI: the TUI never mutates the workspace itself."""


class FilterScreen(ModalScreen[str | None]):
    """``/`` — a one-line filter over the current lens's selectable rows.

    Filtering is a VIEW operation: it hides rows, it never re-queries and never
    changes what a number means. Escape dismisses without changing the filter,
    so a mistyped ``/`` costs nothing.
    """

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, initial: str = "") -> None:
        super().__init__()
        self.initial = initial

    def compose(self) -> ComposeResult:
        yield Input(value=self.initial, placeholder="filter rows…", id="filter-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


def degrade_to_ascii() -> bool:
    """True when this terminal should get the ASCII, weight-only rendering.

    ``NO_COLOR`` is honoured for colour, and a non-UTF-8 locale for glyphs; the
    two travel together here because the same operators set both, and the text
    must read correctly either way. Colour is always redundant encoding, so
    nothing is lost — only the braille and box glyphs transliterate.
    """
    if os.environ.get("ZICATO_TUI_ASCII"):
        return True
    encoding = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    return "utf-8" not in encoding and "utf8" not in encoding


__all__ = ["CSS", "HELP", "STYLE", "FilterScreen", "ZicatoTui", "degrade_to_ascii"]
