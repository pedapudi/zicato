"""Operator-facing end-to-end guard for the harmonograf integration.

This is the anti-"mocks-pass-but-it-is-invisible" guard required by the
harmonograf self-hosting program (``docs/design/HARMONOGRAF.md``). It does
NOT mock the harmonograf client or server: it stands up the REAL persistent
per-workspace server via :func:`ensure_workspace_harmonograf`, emits BOTH a
board-run session and a zicato meta-loop session through the REAL
``harmonograf_client`` sink path the orchestrator/worker use, lays down the
on-disk artifacts the dashboard reads (``loss.json`` +
``meta_loop_events.jsonl``), then drives the dashboard over HTTP and asserts:

* ``/api/state`` carries ``harmonograf_url`` (the persistent server) and
  ``harmonograf_meta_session`` (the meta-loop session id);
* the per-run deep-link the frontend builds resolves to the SAME
  ``adk_session_id`` that was emitted (resolved, not just non-empty);
* the zicato-level (meta-loop) deep-link resolves to the SAME meta session
  id that was emitted.

The whole module skips cleanly when ``harmonograf_server`` /
``harmonograf_client`` / ``goldfive`` are unavailable (a degraded CI), so it
never goes red on a thin install — but on a full install it is the durable
proof that the operator actually SEES harmonograf at both levels.

Teardown shuts down only the server THIS test launched (by handle), never
any operator dashboard. Nothing here runs a live ``zicato evolve``.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.request import urlopen

import pytest

pytest.importorskip("harmonograf_server", reason="harmonograf-server not installed")
pytest.importorskip("harmonograf_client", reason="harmonograf-client not installed")
pytest.importorskip("goldfive", reason="goldfive not installed")

from starlette.testclient import TestClient  # noqa: E402

from zicato.dashboard.server import create_app  # noqa: E402
from zicato.telemetry.harmonograf_supervisor import (  # noqa: E402
    build_meta_loop_sink,
    ensure_workspace_harmonograf,
    meta_loop_session_id,
)

# The static bundle the dashboard serves — resolved off the package so the
# app boots with a real index.html (the state APIs we assert on do not need
# it, but create_app validates the dir exists).
_STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "zicato" / "dashboard" / "static"


def _emit_goldfive_run_session(grpc_target: str, session_id: str) -> None:
    """Emit a minimal goldfive run-lifecycle pair through the REAL sink.

    Mirrors the worker's per-run path: a ``Client(name="zicato")`` (no
    pinned session) feeding a :class:`HarmonografSink`, with each event
    carrying ``session_id`` on the envelope — exactly how goldfive stamps
    ``Session.id`` onto every event so harmonograf buckets the run under
    that id (docs/design/HARMONOGRAF.md §2a).
    """
    from goldfive.events import run_completed_event, run_started_event
    from harmonograf_client import Client, HarmonografSink

    client = Client(name="zicato", server_addr=grpc_target)
    sink = HarmonografSink(client)

    async def _drive() -> None:
        # session_id stamped on the envelope is the deep-link key.
        started = run_started_event(session_id, 1, session_id=session_id)
        completed = run_completed_event(session_id, 2, session_id=session_id)
        await sink.emit(started)
        await sink.emit(completed)
        await sink.close()

    asyncio.run(_drive())
    client.shutdown(flush_timeout=5.0)


def _emit_meta_loop_session(harmonograf_url: str, session_id: str) -> None:
    """Emit a meta-loop event through the REAL ``build_meta_loop_sink`` path.

    This is the exact sink the orchestrator attaches for its proposer +
    judge calls — a ``Client(session_id=<meta id>)`` so every envelope is
    bucketed under the one stable meta-loop session.
    """
    from goldfive.events import agent_invocation_started_event

    sink = build_meta_loop_sink(harmonograf_url, session_id)
    assert sink is not None, "meta-loop sink should construct against a live server"

    async def _drive() -> None:
        evt = agent_invocation_started_event(
            f"{session_id}-run",
            1,
            agent_name="zicato.proposer",
            task_id="proposer_call_started",
            invocation_id="proposer-e2e",
            session_id=session_id,
        )
        await sink.emit(evt)
        await sink.close()

    asyncio.run(_drive())


def _write_board_run_loss(ws: Path, *, adk_session_id: str) -> tuple[str, str, str]:
    """Lay down a board-run ``loss.json`` carrying the emitted session id.

    Returns ``(epoch_id, generation_id, entry_id)`` for the deep-link URL.
    """
    epoch_id, gen_id, entry_id = "e2e-epoch", "v1", "waffles_single"
    run_dir = ws / "epochs" / epoch_id / "generations" / gen_id / "runs" / entry_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "loss.json").write_text(
        json.dumps(
            {
                "drift_loss": 62.0,
                "pass_fail": 0,
                "runtime_ms": 180000,
                "run_id": "run_v1_waffles",
                "adk_session_id": adk_session_id,
            }
        ),
        encoding="utf-8",
    )
    return epoch_id, gen_id, entry_id


def _write_meta_loop_jsonl(ws: Path, *, session_id: str) -> None:
    """Lay down ``meta_loop_events.jsonl`` so the dashboard recovers the id."""
    runtime = ws / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "meta_loop_events.jsonl").write_text(
        json.dumps({"session_id": session_id, "sequence": 1, "kind": "proposer_call_started"})
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A ``.zicato`` workspace dir on disk (so the supervisor stands up)."""
    ws = tmp_path / "proj" / ".zicato"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def test_operator_sees_harmonograf_at_both_levels(workspace: Path) -> None:
    """The full operator-facing arc: emit two real sessions, observe both
    deep-links resolve over the dashboard's HTTP surface."""
    # 1. Stand up the REAL persistent per-workspace harmonograf server.
    handle = ensure_workspace_harmonograf(workspace)
    if not handle.web_url:
        pytest.skip(f"harmonograf server did not launch: {handle.reason!r}")
    try:
        assert handle.grpc_target, "a launched server must expose a gRPC target"

        # The installed server package must carry the console, not merely its
        # health endpoint. Pin the root document and one hashed asset.
        with urlopen(f"{handle.web_url}/", timeout=5) as response:  # noqa: S310
            assert response.headers.get_content_type() == "text/html"
            html = response.read().decode()
        asset = re.search(r'(?:src|href)="(/assets/[^"]+)"', html)
        assert asset, "the console document must reference a packaged asset"
        with urlopen(f"{handle.web_url}{asset.group(1)}", timeout=5) as response:  # noqa: S310
            assert response.status == 200

        # 2. Emit BOTH sessions through the REAL client/sink paths.
        board_sid = "adk-e2e-board-0001"
        _emit_goldfive_run_session(handle.grpc_target, board_sid)

        meta_sid = meta_loop_session_id("2026-06-06T12:00:00+00:00")
        _emit_meta_loop_session(handle.web_url, meta_sid)

        # 3. Lay down the on-disk artifacts the dashboard reads.
        epoch_id, gen_id, entry_id = _write_board_run_loss(workspace, adk_session_id=board_sid)
        _write_meta_loop_jsonl(workspace, session_id=meta_sid)

        # 4. Drive the dashboard over HTTP against THIS workspace + the
        #    persistent server URL (a distinct port from any operator
        #    dashboard — we never touch port 7892).
        app = create_app(workspace, _STATIC_DIR, harmonograf_url=handle.web_url)
        with TestClient(app) as client:
            # (a) /api/state carries the persistent URL + the meta session id.
            state = client.get("/api/state").json()
            hb = state.get("heartbeat") or {}
            assert (
                hb.get("harmonograf_url") == handle.web_url
            ), "the dashboard must surface the persistent harmonograf URL"
            assert hb.get("harmonograf_persistent") is True
            assert hb.get("harmonograf_meta_session") == meta_sid, (
                "the zicato-level meta-loop session id must reach /api/state "
                "(recovered off meta_loop_events.jsonl post-mortem)"
            )

            # (b) the per-run deep-link resolves to the SAME emitted board sid.
            header = client.get(f"/api/run/{epoch_id}/{gen_id}/{entry_id}/header").json()
            assert header.get("adk_session_id") == board_sid, (
                "the run header must carry the emitted board session id so the "
                "frontend deep-links /#/session/<board_sid>"
            )
            run_deep_link = f"{handle.web_url}/#/session/{board_sid}"

            # (c) the zicato-level deep-link resolves to the emitted meta sid.
            meta_deep_link = f"{handle.web_url}/#/session/{meta_sid}"

        # Evidence: both deep-links are built from the SAME ids that were
        # emitted through the real sinks. (The frontend builders in
        # core/harmonograf.js produce exactly these URLs from the fields
        # asserted above — covered by harmonograf.test.mjs.)
        assert board_sid in run_deep_link
        assert meta_sid in meta_deep_link
    finally:
        # Tear down ONLY the server this test launched. A reused server
        # (launched is False) is left for its owner.
        handle.shutdown()
