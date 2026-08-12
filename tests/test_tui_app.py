"""The Textual app, driven headless through Pilot.

Everything about WHAT to show is tested in ``test_tui_lenses.py`` and
``test_tui_console.py``; this file tests the wiring — that keys reach the
controller, that the drawer follows the cursor, and above all that the repaint
guard survives the trip into widgets: a no-op refresh must not update a single
``Static``.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from tests.tui_fixture import EPOCH, PAYLOADS
from zicato.tui.client import SnapshotClient
from zicato.tui.routes import Route

textual = pytest.importorskip("textual")

from textual.widgets import Static  # noqa: E402

from zicato.tui.app import ZicatoTui, degrade_to_ascii  # noqa: E402


def make_app(route: Route | None = None, **kwargs: Any) -> ZicatoTui:
    return ZicatoTui(
        client=SnapshotClient(deepcopy(PAYLOADS)),
        route=route or Route(lens="home", params={"epoch": EPOCH}),
        workspace="/tmp/ws/.zicato",
        ascii_only=False,
        # A long interval: these tests drive refreshes explicitly, so a timer
        # firing mid-assertion would make the repaint counts non-deterministic.
        poll_seconds=3600.0,
        **kwargs,
    )


async def test_app_boots_and_paints_the_home_lens() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        text = "\n".join(str(w.content) for w in app.query(Static))
        assert "plateaued" in text
        assert "2026-07-04_e2" in text
        assert "[live]" in str(app.query_one("#band", Static).content)


async def test_a_no_op_refresh_updates_zero_widgets() -> None:
    """The digest guard, end to end: no change means no widget is touched."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        updates: list[str] = []
        for key, widget in app._row_widgets.items():  # noqa: SLF001
            original = widget.update

            def spy(renderable: object, _key: str = key, _original: Any = original) -> None:
                updates.append(_key)
                _original(renderable)

            widget.update = spy  # type: ignore[method-assign]

        # A resize event during mount also drives a reload, so the count is
        # asserted as a DELTA — what matters is that ten refreshes produced ten
        # no-ops and zero widget updates.
        before = app.console_model.noops
        for _ in range(10):
            app.reload()
            await pilot.pause()
        assert updates == []
        assert app.console_model.noops - before == 10
        assert app.console_model.repaints == 1


async def test_number_keys_jump_lenses_and_b_goes_back() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        assert app.console_model.lens_name == "standings"
        await pilot.press("4")
        await pilot.pause()
        assert app.console_model.lens_name == "board"
        await pilot.press("b")
        await pilot.pause()
        assert app.console_model.lens_name == "standings"


async def test_the_drawer_follows_the_cursor_with_five_slots() -> None:
    from zicato.tui.lenses.base import EVIDENCE_SLOTS

    app = make_app(route=Route(lens="standings", params={"epoch": EPOCH}))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
        drawer = str(app.query_one("#drawer", Static).content)
        assert drawer
        for slot in EVIDENCE_SLOTS:
            assert slot in drawer
        selected = app.console_model.selected
        assert selected is not None
        before = selected.key
        await pilot.press("j")
        await pilot.pause()
        assert app.console_model.selected is not None
        assert app.console_model.selected.key != before


async def test_enter_drills_into_the_candidate_dossier() -> None:
    app = make_app(route=Route(lens="standings", params={"epoch": EPOCH}))
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = app.console_model.rows
        target = next(i for i, r in enumerate(rows) if r.action == "candidate/v4")
        app.console_model.cursor = target
        await pilot.press("enter")
        await pilot.pause()
        assert app.console_model.lens_name == "candidate"
        text = "\n".join(str(w.content) for w in app.query(Static))
        assert "Gate" in text


async def test_apply_on_a_non_command_row_does_nothing() -> None:
    """``a`` must never fire on a row that carries no apply command."""
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        lens_before = app.console_model.lens_name
        await pilot.press("a")
        await pilot.pause()
        assert app.console_model.pending_command is None
        assert app.console_model.lens_name == lens_before


async def test_help_overlay_toggles_in_the_drawer() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert "apply shells to the CLI" in str(app.query_one("#drawer", Static).content)
        await pilot.press("question_mark")
        await pilot.pause()
        assert "apply shells to the CLI" not in str(app.query_one("#drawer", Static).content)


async def test_narrow_terminal_collapses_the_rail() -> None:
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.console_model.context.narrow
        assert app.query_one("#rail").has_class("narrow")


async def test_ascii_mode_emits_no_non_ascii() -> None:
    app = ZicatoTui(
        client=SnapshotClient(deepcopy(PAYLOADS)),
        route=Route(lens="candidate", params={"epoch": EPOCH, "gen": "v4"}),
        ascii_only=True,
        poll_seconds=3600.0,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        text = "\n".join(str(w.content) for w in app.query(Static))
        assert text.isascii(), text


def test_ascii_degrade_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZICATO_TUI_ASCII", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    assert degrade_to_ascii() is False
    monkeypatch.setenv("LANG", "C")
    assert degrade_to_ascii() is True
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("ZICATO_TUI_ASCII", "1")
    assert degrade_to_ascii() is True
