"""Canonical content changes remain visible after progress has stopped."""

from __future__ import annotations

import asyncio
import json

import pytest

from zicato.dashboard import sse
from zicato.query import WorkspacePaths


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("active_tournament.events.jsonl", "active_tournament"),
        ("active_tournament.json", "unknown"),
        ("progress.events.jsonl", "progress"),
    ],
)
def test_runtime_change_classification_uses_canonical_logs(tmp_path, name, kind):
    paths = WorkspacePaths(tmp_path)
    path = paths.progress_log if kind == "progress" else paths.runtime / name
    assert sse._classify(path, paths) == kind


@pytest.mark.parametrize("watchdog", [True, False])
async def test_content_rewrite_advances_revision_without_progress(tmp_path, monkeypatch, watchdog):
    if watchdog and not sse._HAVE_WATCHDOG:
        pytest.skip("watchdog is unavailable")
    monkeypatch.setattr(sse, "_HAVE_WATCHDOG", watchdog)
    monkeypatch.setattr(sse, "_POLL_INTERVAL_S", 0.01)
    paths = WorkspacePaths(tmp_path)
    paths.runtime.mkdir()
    epoch = paths.epochs / "e1"
    epoch.mkdir(parents=True)
    record = epoch / "config.json"
    record.write_text('{"goal":"first"}')
    monkeypatch.setattr(sse, "_progress_signal", lambda _: (17, True))
    broker = sse.ChangeBroker(paths)
    await broker.start()
    queue = broker.subscribe()
    try:
        record.write_text('{"goal":"second"}')
        frame = await asyncio.wait_for(queue.get(), 3)
        assert frame["data"]["content_revision"] > 0
        assert frame["data"]["seq"] == 17
        assert frame["data"]["terminal"] is True
        assert json.loads(record.read_text())["goal"] == "second"
        # Reading the canonical record must not generate refresh feedback.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.get(), 0.4)
        paths.heartbeat.write_text('{"seq":17}')
        heartbeat = await asyncio.wait_for(queue.get(), 3)
        assert heartbeat["data"]["content_revision"] == frame["data"]["content_revision"]
        record.unlink()
        removed = await asyncio.wait_for(queue.get(), 3)
        assert removed["data"]["content_revision"] > frame["data"]["content_revision"]
    finally:
        await broker.stop()
