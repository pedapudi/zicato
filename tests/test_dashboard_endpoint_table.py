"""The declarative dashboard read-endpoint table and what it must agree with.

Most dashboard read routes are one row of
:data:`zicato.dashboard.endpoints.READ_ENDPOINTS` — a path, the query-library
reader behind it, the coordinates it takes, and the shape it serves when a
coordinate is rejected — built into a handler by one factory. Three things
have to hold for that to be safe, and each is a test here:

* **The wire is unchanged.** Every route in the table is probed against the
  standard fixture workspace, with a coordinate the fixture holds and with a
  coordinate the guard rejects, and compared byte for byte (key order
  included) against a snapshot recorded before the table existed.
* **The registry agrees both ways.** Every table row has a declared payload
  contract, and every declared contract belongs to a route the app binds.
* **A degrade is a real response.** Each canned degrade satisfies the payload
  contract its route declares, field by field, so a client that meets a
  rejected coordinate reads the same field types it reads from a resolved one.
"""

from __future__ import annotations

import json
import os
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

import pytest
from starlette.routing import Match, Route

from tests._endpoint_snapshot_harness import (
    ROUTE_PROBES,
    capture_route_snapshot,
    snapshot_text,
)
from zicato.dashboard.endpoints import (
    READ_ENDPOINTS,
    SCOPE_IGNORE_MALFORMED_EPOCH,
    ReadEndpoint,
    _degrade_drift_movements,
    _degrade_matchup_detail,
    route_name,
)
from zicato.dashboard.server import create_app
from zicato.query.contracts import ENDPOINT_PAYLOADS

_GOLDEN = Path(__file__).parent / "data" / "endpoint_route_snapshot.json"

#: The stand-in a rendered degrade puts in place of each path coordinate. It
#: never reaches a workspace — the degrade is the response a rejected
#: coordinate gets, so nothing is read to produce it.
_SAMPLE_COORDINATE = "sample"

#: The two degrades that name the workspace's current epoch, and so cannot be
#: rendered without one. The response snapshot is what pins their shape.
_WORKSPACE_READING_DEGRADES = (_degrade_drift_movements, _degrade_matchup_detail)


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    return d


def _routes(tmp_path: Path, static_dir: Path) -> list[Route]:
    app = create_app(tmp_path / "ws", static_dir, read_only=True)
    return [route for route in app.routes if isinstance(route, Route)]


def _resolved_path(routes: list[Route], url: str) -> str | None:
    """The route path a request URL binds to, as Starlette resolves it."""
    scope = {"type": "http", "method": "GET", "path": url.split("?")[0], "headers": []}
    for route in routes:
        if route.matches(scope)[0] is Match.FULL:
            return route.path
    return None


# ---------------------------------------------------------------------------
# The wire is unchanged
# ---------------------------------------------------------------------------


def test_table_driven_routes_serve_the_recorded_responses(tmp_path: Path, static_dir: Path) -> None:
    """Every probe answers what it answered before the table existed."""
    snapshot = capture_route_snapshot(tmp_path / "capture", static_dir)
    if os.environ.get("ZICATO_ENDPOINT_SNAPSHOT_UPDATE") == "1":
        _GOLDEN.write_text(snapshot_text(snapshot), encoding="utf-8")
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))

    assert sorted(snapshot) == sorted(golden), "the probe set changed"
    for label in golden:
        # Compared as encoded text so a reordered body fails: the degrade and
        # the resolved read must present one field order, and a client that
        # renders the response in order must not see it shift.
        assert json.dumps(snapshot[label]) == json.dumps(
            golden[label]
        ), f"{label} no longer serves its recorded response"


def test_every_table_route_is_probed(tmp_path: Path, static_dir: Path) -> None:
    """No row of the table escapes the response snapshot."""
    routes = _routes(tmp_path, static_dir)
    probed = {_resolved_path(routes, url) for _label, url in ROUTE_PROBES}
    unprobed = sorted(entry.path for entry in READ_ENDPOINTS if entry.path not in probed)
    assert unprobed == [], f"table routes with no probe: {unprobed}"


# ---------------------------------------------------------------------------
# The registry agrees both ways
# ---------------------------------------------------------------------------


def test_every_table_route_declares_a_payload_contract() -> None:
    missing = sorted(entry.path for entry in READ_ENDPOINTS if entry.path not in ENDPOINT_PAYLOADS)
    assert missing == [], f"table routes with no ENDPOINT_PAYLOADS entry: {missing}"


def test_every_declared_payload_contract_belongs_to_a_bound_route(
    tmp_path: Path, static_dir: Path
) -> None:
    """A declared contract with no route behind it is a stale registry entry."""
    bound = {route.path for route in _routes(tmp_path, static_dir)}
    orphans = sorted(path for path in ENDPOINT_PAYLOADS if path not in bound)
    assert orphans == [], f"declared payloads with no route: {orphans}"


def test_every_table_route_is_bound_exactly_once(tmp_path: Path, static_dir: Path) -> None:
    bound = [route.path for route in _routes(tmp_path, static_dir)]
    for entry in READ_ENDPOINTS:
        count = bound.count(entry.path)
        assert count == 1, f"{entry.path} is bound {count} times"


def test_route_names_are_distinct() -> None:
    """Two rows must not resolve to the same handler identifier."""
    names = [route_name(entry.path) for entry in READ_ENDPOINTS]
    assert len(set(names)) == len(names)


def test_a_degrade_is_declared_exactly_where_a_request_can_be_refused() -> None:
    for entry in READ_ENDPOINTS:
        if entry.rejects_coordinates:
            assert entry.degrade is not None, f"{entry.path} can refuse but declares no degrade"
        else:
            assert entry.degrade is None, f"{entry.path} declares a degrade it can never serve"


# ---------------------------------------------------------------------------
# A degrade is a real response
# ---------------------------------------------------------------------------


def _fits(annotation: Any, value: Any) -> bool:
    """Whether one JSON value satisfies one payload-contract annotation."""
    if annotation is Any:
        return True
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return any(_fits(arg, value) for arg in get_args(annotation))
    if annotation is type(None):
        return value is None
    if origin is not None:
        return isinstance(value, origin)
    if annotation is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, annotation)


@pytest.mark.parametrize(
    "entry",
    [
        entry
        for entry in READ_ENDPOINTS
        if entry.degrade is not None and entry.degrade not in _WORKSPACE_READING_DEGRADES
    ],
    ids=lambda entry: entry.path,
)
def test_each_degrade_satisfies_its_payload_contract(entry: ReadEndpoint) -> None:
    """Every field the contract declares holds the type the contract declares.

    The rows whose degrade calls the reader's own empty-shape helper are
    checked here too, so a reader that changes an empty shape into something
    its route's contract does not describe fails here rather than in a browser.
    """
    degrade = entry.degrade
    assert degrade is not None
    body = degrade(None, {name: _SAMPLE_COORDINATE for name in entry.params})  # type: ignore[arg-type]
    assert isinstance(body, dict)
    declared = get_type_hints(ENDPOINT_PAYLOADS[entry.path])
    for field, value in body.items():
        if field not in declared:
            continue
        assert _fits(declared[field], value), (
            f"{entry.path}: degrade field {field!r} is {value!r}, "
            f"which the contract declares as {declared[field]!r}"
        )


def test_an_ignored_epoch_scope_carries_no_coordinates() -> None:
    """A route that reads a malformed scope as no scope has nothing to reject."""
    for entry in READ_ENDPOINTS:
        if entry.epoch_scope == SCOPE_IGNORE_MALFORMED_EPOCH:
            assert not entry.params, f"{entry.path} mixes an ignored scope with coordinates"
