"""Blocking reads yield to sibling requests and preserve snapshot invalidation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from zicato.dashboard import endpoints, sse
from zicato.dashboard.server import create_app
from zicato.query import WorkspacePaths


@pytest.mark.parametrize("route", ["/api/workspace", "/api/environment"])
async def test_held_reader_keeps_health_responsive(tmp_path, monkeypatch, route):
    entered = threading.Event()
    health_finished = threading.Event()
    released = threading.Event()
    reader_threads = []

    def read(*args, **kwargs):
        reader_threads.append(threading.get_ident())
        connection = sqlite3.connect(":memory:")
        try:
            entered.set()
            assert released.wait(3)
            return {"value": connection.execute("SELECT 1").fetchone()[0]}
        finally:
            connection.close()

    # A helper releases even when a broken handler blocks the entire event loop.
    def release():
        health_finished.wait(1)
        released.set()

    if route == "/api/environment":
        monkeypatch.setattr(endpoints.query, "build_environment", read)
    else:
        monkeypatch.setattr(
            endpoints,
            "READ_ENDPOINTS",
            tuple(
                replace(entry, reader=read) if entry.path == route else entry
                for entry in endpoints.READ_ENDPOINTS
            ),
        )
    app = create_app(tmp_path, static_dir=Path(endpoints.__file__).parent / "static")
    helper = threading.Thread(target=release)
    helper.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            pending = asyncio.create_task(client.get(route))
            assert await asyncio.to_thread(entered.wait, 2)
            response = await client.get("/api/health")
            responsive = not released.is_set()
            health_finished.set()
            result = await pending
            assert response.status_code == 200
            assert result.json() == {"value": 1}
            assert responsive, "health waited for the held reader to finish"
            assert len(reader_threads) == 1
            assert reader_threads[0] != threading.get_ident()
    finally:
        health_finished.set()
        helper.join(2)


async def test_mutation_during_snapshot_remains_newer_than_snapshot(tmp_path, monkeypatch):
    paths = WorkspacePaths(tmp_path)
    broker = sse.ChangeBroker(paths)
    broker._loop = asyncio.get_running_loop()
    entered = threading.Event()
    changed = threading.Event()

    def snapshot(_):
        entered.set()
        assert changed.wait(1)
        return {"epoch_id": "before"}

    monkeypatch.setattr(sse, "build_snapshot", snapshot)
    monkeypatch.setattr(sse, "_progress_signal", lambda _: (17, True))
    stream = sse.sse_event_stream(broker, paths)
    pending = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        broker._accumulate_kind("epoch")
        changed.set()
        frame = json.loads((await pending).split("data: ", 1)[1])
        assert frame["content_revision"] == 0
        change = json.loads((await asyncio.wait_for(anext(stream), 2)).split("data: ", 1)[1])
        assert change["content_revision"] > frame["content_revision"]
        assert change["seq"] == frame["seq"] == 17
    finally:
        changed.set()
        await stream.aclose()
        await broker.stop()


async def test_cancelled_request_leaves_connection_cleanup_with_reader(tmp_path):
    from starlette.requests import Request

    entered = threading.Event()
    released = threading.Event()
    closed = threading.Event()

    def read(_):
        connection = sqlite3.connect(":memory:")
        try:
            entered.set()
            assert released.wait(3)
            return {"value": connection.execute("SELECT 1").fetchone()[0]}
        finally:
            connection.close()
            closed.set()

    entry = endpoints.ReadEndpoint(path="/api/workspace", reader=read, serves="Workspace facts")
    handler = endpoints._read_handler(WorkspacePaths(tmp_path), entry)
    request = Request({"type": "http", "path_params": {}, "query_string": b""})
    pending = asyncio.create_task(handler(request))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not closed.is_set()
    finally:
        released.set()
    assert await asyncio.to_thread(closed.wait, 2)
