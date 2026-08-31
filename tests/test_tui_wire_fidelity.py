"""The TUI reads the REAL service, not a fixture that has drifted from it.

``tests/tui_fixture.py`` states the served payloads as data so the lenses can be
tested without a server. That is only safe while the fixture still describes
what the service actually serves — so this module runs every lens against the
live ASGI app over a real workspace, and cross-checks the fixture's key sets
against the live ones.

A failure here means the fixture (or a lens) is reading a key the service no
longer serves, which is exactly the drift that would otherwise be discovered by
an operator staring at a column of em-dashes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.test_dashboard_server import _populate_workspace
from tests.tui_fixture import PAYLOADS
from zicato.dashboard.server import create_app
from zicato.dashboard.static_assets import resolve_static_dir
from zicato.tui.client import ServiceError, SnapshotClient
from zicato.tui.lenses import LENSES, LensContext, safe_render
from zicato.tui.routes import Route
from zicato.tui.view import render_text


class LiveClient:
    """A :class:`~zicato.tui.client.Client` backed by the real ASGI app."""

    def __init__(self, client: TestClient, root: Path) -> None:
        self._client = client
        #: The workspace the app serves, so a test can write an artefact the
        #: populated fixture does not carry and then read it back over HTTP.
        self.root = root
        self.requested: list[str] = []

    def begin_pass(self) -> None:
        return None

    def get(self, path: str) -> Any:
        self.requested.append(path)
        response = self._client.get(path)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ServiceError(f"{path} answered HTTP {response.status_code}")
        return response.json()


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> LiveClient:
    ws = tmp_path_factory.mktemp("tui-wire") / ".zicato"
    _populate_workspace(ws)
    app = create_app(workspace_root=ws, static_dir=resolve_static_dir(None), read_only=True)
    return LiveClient(TestClient(app), ws)


@pytest.fixture(scope="module")
def epoch_id(live: LiveClient) -> str:
    return str(live.get("/api/workspace")["current_epoch_id"])


@pytest.mark.parametrize("lens", LENSES, ids=lambda lens: lens.name)
def test_every_lens_renders_against_the_live_service(
    lens: object, live: LiveClient, epoch_id: str
) -> None:
    """No lens may crash, and none may fail for a reason it cannot explain."""
    ctx = LensContext(route=Route(lens=lens.name, params={"epoch": epoch_id}))
    view = safe_render(lens, live, ctx)
    assert "failed to render" not in (view.degraded or "")
    assert "cannot reach" not in (view.degraded or "")
    assert render_text(view).strip()


def test_every_path_a_lens_requests_is_a_path_the_service_serves(
    live: LiveClient, epoch_id: str
) -> None:
    """A typo'd endpoint would silently render as "no data"; this catches it.

    The service answers 404 for an ABSENT artefact, which is legitimate, so the
    check is that the route EXISTS — an unrouted path falls through to the
    static handler and comes back as HTML, never JSON.
    """
    for lens in LENSES:
        safe_render(
            lens, live, LensContext(route=Route(lens=lens.name, params={"epoch": epoch_id}))
        )
    for path in sorted(set(live.requested)):
        response = live._client.get(path)  # noqa: SLF001 - the module's own double
        assert response.status_code < 400, path
        assert response.headers["content-type"].startswith("application/json"), (
            f"{path} is not a JSON API route — the lens is asking for something "
            "the service does not serve"
        )


def test_the_fixture_keys_are_keys_the_service_really_serves(
    live: LiveClient, epoch_id: str
) -> None:
    """Every top-level key in the hand-written fixture must exist for real."""
    checked = 0
    for path, payload in PAYLOADS.items():
        if not isinstance(payload, dict):
            continue
        live_path = _retarget(path, epoch_id)
        if live_path is None:
            continue
        actual = live.get(live_path)
        if not isinstance(actual, dict):
            continue
        missing = set(payload) - set(actual)
        assert (
            not missing
        ), f"{live_path}: fixture invents keys the service does not serve: {missing}"
        checked += 1
    assert checked >= 8, "the fixture/service cross-check covered too little to be meaningful"


def test_the_practice_review_keys_are_the_serializer_s_own(live: LiveClient, epoch_id: str) -> None:
    """Per-check keys, which the top-level cross-check above cannot reach.

    Reflection paths carry a fixture-specific id, so they are skipped by the
    retargeting check and every key inside them went unverified. That is how a
    fixture came to state ``id`` / ``name`` / ``detail`` for a practice check
    while the wire carried ``check_id`` / ``headline`` / ``rationale``: the
    lens read the fixture's names, agreed with the fixture, and rendered two
    empty columns against the real service.

    So this writes a review the real serializer produced into the live
    workspace, reads it back over HTTP, and holds three key sets equal: what
    :meth:`PracticeCheck.to_json` writes, what the service serves, and what the
    fixture states.
    """
    from zicato.core.workspace import reflection_practices_path
    from zicato.reflection.practices import PracticeCheck, PracticeReview

    check = PracticeCheck(
        check_id="oracle_mix",
        verdict="sound",
        headline="The board mixes 3 structured oracles with 1 substring.",
        evidence={"n_strong": 3, "n_weak": 1},
        rationale="weak oracles saturate once a candidate clears them.",
    )
    reflection_id = "refl-practice-key-fidelity"
    path = reflection_practices_path(live.root, epoch_id, reflection_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(PracticeReview(checks=(check,)).to_json()), encoding="utf-8")

    served = live.get(f"/api/reflection/{reflection_id}/practices")
    assert served["found"] is True, "the service did not find the review just written"
    serializer_keys = set(check.to_json())
    assert set(served["checks"][0]) == serializer_keys

    fixture_checks = PAYLOADS["/api/reflection/refl-2026-07-04/practices"]["checks"]
    assert fixture_checks, "the fixture states no practice checks to cross-check"
    for fixture_check in fixture_checks:
        assert set(fixture_check) == serializer_keys


#: Fixture paths whose ids are fixture-specific (a generation, a reflection)
#: cannot be retargeted onto a different workspace; the shape checks above
#: cover the ones that can.
_UNRETARGETABLE = ("/api/generation/", "/api/round/", "/api/reflection/")


def _retarget(path: str, epoch_id: str) -> str | None:
    """Rewrite a fixture path onto the live workspace's epoch, or skip it."""
    from tests.tui_fixture import EPOCH

    if any(path.startswith(prefix) for prefix in _UNRETARGETABLE):
        return None
    return path.replace(EPOCH, epoch_id)


def test_gate_and_generation_payloads_answer_for_a_real_pair(
    live: LiveClient, epoch_id: str
) -> None:
    """The per-pair routes exist and answer JSON for a workspace's own ids."""
    lineage = live.get("/api/lineage")["generations"]
    child = next((g for g in lineage if g.get("parent_generation_id")), None)
    if child is None:
        pytest.skip("the dashboard fixture workspace has no parented generation")
    parent = child["parent_generation_id"]
    gen = child["generation_id"]
    for path in (
        f"/api/round/{epoch_id}/{parent}/{gen}/gate",
        f"/api/generation/{epoch_id}/{gen}/per-entry",
        f"/api/generation/{epoch_id}/{gen}/per-judge",
    ):
        assert isinstance(live.get(path), dict), path


# ---------------------------------------------------------------------------
# Served shapes that are NOT objects
# ---------------------------------------------------------------------------
#
# A lens reads payloads with ``as_dict`` / ``as_list``, which coerce anything
# unexpected to an empty container. That is the right default — a lens must
# degrade, never raise — but it also means a wrong assumption about a served
# shape fails SILENTLY, as a screen full of em-dashes rather than an error.
# These pin the three shapes most likely to be assumed wrongly.


@pytest.mark.parametrize("path", ["/api/heartbeat", "/api/active-tournament"])
def test_a_json_null_payload_does_not_break_any_lens(path: str, epoch_id: str) -> None:
    """Two endpoints legitimately serve JSON ``null``, not an object.

    ``null`` means "no loop is running" / "no tournament in flight" — both
    ordinary states, not errors. Every lens must render through them.
    """
    payloads: dict[str, Any] = {
        "/api/heartbeat": None,
        "/api/active-tournament": None,
        "/api/workspace": {"current_epoch_id": epoch_id},
    }
    assert path in payloads, "the parametrisation must name a payload under test"
    for lens in LENSES:
        view = safe_render(
            lens,
            SnapshotClient(payloads),
            LensContext(route=Route(lens=lens.name, params={"epoch": epoch_id})),
        )
        assert "failed to render" not in (view.degraded or ""), (lens.name, path)
        assert render_text(view).strip()


def test_active_runs_is_a_bare_array_not_an_object(live: LiveClient) -> None:
    """``/api/active-runs`` serves a LIST at the top level.

    Every other collection endpoint wraps its rows in an object, so this one is
    the standing trap. Pinned here so a future lens that reaches for it starts
    from the served truth rather than from the house pattern.
    """
    assert isinstance(live.get("/api/active-runs"), list)
