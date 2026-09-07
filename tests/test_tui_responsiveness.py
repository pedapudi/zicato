"""Terminal requests cannot hold the input loop or publish an abandoned route."""

from __future__ import annotations

import asyncio
import threading
from copy import deepcopy

import pytest

from tests.tui_fixture import EPOCH, PAYLOADS
from zicato.tui.client import HttpClient, ServiceError, SnapshotClient
from zicato.tui.routes import Route
from zicato.tui.view import View

pytest.importorskip("textual")
from zicato.tui.app import ZicatoTui  # noqa: E402


async def test_held_terminal_request_keeps_navigation_responsive():
    entered = threading.Event()
    finished = threading.Event()
    released = threading.Event()
    navigation_while_waiting = []

    class HeldClient(SnapshotClient):
        hold = False

        def get(self, path):
            if self.hold:
                self.hold = False
                entered.set()
                try:
                    assert released.wait(3)
                finally:
                    finished.set()
            return super().get(path)

    class ObservedTui(ZicatoTui):
        def action_jump(self, index):
            super().action_jump(index)
            navigation_while_waiting.append(entered.is_set() and not finished.is_set())
            released.set()

    client = HeldClient(deepcopy(PAYLOADS))
    app = ObservedTui(client, route=Route("home", {"epoch": EPOCH}), poll_seconds=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        client.hold = True

        try:
            app.reload()
            assert await asyncio.to_thread(entered.wait, 2)
            await pilot.press("2")
            await pilot.pause()
            assert navigation_while_waiting == [True], "navigation waited for the held HTTP request"
            assert app.console_model.route.lens == "standings"
        finally:
            released.set()


async def test_navigation_away_and_back_rejects_late_view_and_coalesces(monkeypatch):
    app = ZicatoTui(SnapshotClient(PAYLOADS), poll_seconds=3600)
    entered = threading.Event()
    released = threading.Event()
    contexts = []
    applied = []

    def build(context):
        contexts.append(context)
        if len(contexts) == 1:
            entered.set()
            assert released.wait(3)
            return View(title="abandoned", digest="abandoned")
        return View(title="requested", digest="requested")

    async with app.run_test() as pilot:
        await pilot.pause()
        original_apply = app.console_model.apply_view

        def apply(view):
            applied.append(view.title)
            return original_apply(view)

        monkeypatch.setattr(app.console_model, "build_view", build)
        monkeypatch.setattr(app.console_model, "apply_view", apply)
        app.reload()
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            app.action_jump(2)
            app.action_jump(1)
            assert app.console_model.context == contexts[0]
            for _ in range(20):
                app.reload()
            assert len(contexts) == 1
        finally:
            released.set()
        await asyncio.wait_for(app._refresh_task, 2)
        assert len(contexts) == 2
        assert applied == ["requested"]


async def test_shutdown_ignores_running_reader_result(monkeypatch):
    app = ZicatoTui(SnapshotClient(PAYLOADS), poll_seconds=3600)
    entered = threading.Event()
    released = threading.Event()
    finished = threading.Event()

    def build(context):
        entered.set()
        assert released.wait(3)
        finished.set()
        return View(title="late", digest="late")

    try:
        async with app.run_test() as pilot:
            await pilot.pause()
            monkeypatch.setattr(app.console_model, "build_view", build)
            app.reload()
            assert await asyncio.to_thread(entered.wait, 2)
            app.exit()
        assert not finished.is_set(), "UI shutdown waited for the held reader"
        previous = app.console_model.view
    finally:
        released.set()
    assert await asyncio.to_thread(finished.wait, 2)
    await asyncio.sleep(0)
    assert app.console_model.view is previous


def test_http_client_bounds_stream_wait_and_stops_subsequent_reads(monkeypatch):
    import io

    timeouts = []
    closed = []

    class Response(io.BytesIO):
        def close(self):
            closed.append(True)
            super().close()

    def open_request(request, *, timeout):
        timeouts.append(timeout)
        return Response(b'event: state_change\ndata: {"content_revision":1}\n\n')

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    client = HttpClient("http://test")
    assert len(list(client.events())) == 1
    assert timeouts == [20.0]
    assert closed == [True]
    client.close()
    assert list(client.events()) == []
    with pytest.raises(ServiceError, match="closed"):
        client.get("/api/environment")
    assert len(timeouts) == 1
