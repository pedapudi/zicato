"""The canonical decision surface the dashboard serves (WS4 Track B).

The server owns every decision the frontend renders:

* ``readers.decisions`` — the ONE classifier (canonical token + tri-state
  ``promoted``) shared by the lineage view and the epoch experiments feed.
* ``build_epoch_view`` stamps ``decision`` / ``promoted`` on every
  experiment record and carries a ``current_champion`` pointer (the end of
  the promoted spine, or the seed).
* ``build_lineage_view`` accepts an ``epoch_id`` scope (the epoch-scoped
  generations feed) and ``/api/lineage?epoch=`` serves it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.readers.decisions import (
    canonical_decision,
    experiment_decision,
    promoted_tristate,
)
from zicato.dashboard.server import create_app
from zicato.dashboard.state_reader import (
    WorkspacePaths,
    build_epoch_view,
    build_lineage_view,
)

EPOCH = "2026-06-01_e0"
OTHER = "2026-06-02_e1"


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _seed_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    gens = ws / "epochs" / EPOCH / "generations"
    # v0: the seed (no parent, no decision recorded).
    _write_json(gens / "v0" / "experiment.json", {"parent_generation_id": None})
    # v1: promoted challenger.
    _write_json(
        gens / "v1" / "experiment.json",
        {"parent_generation_id": "v0", "outcome": {"tournament_decision": "promoted"}},
    )
    # v2: rejected challenger of the NEW champion v1.
    _write_json(
        gens / "v2" / "experiment.json",
        {"parent_generation_id": "v1", "outcome": {"tournament_decision": "rejected"}},
    )
    # v3: promoted from v1 — the spine is v1 -> v3, so v3 REIGNS.
    _write_json(
        gens / "v3" / "experiment.json",
        {"parent_generation_id": "v1", "outcome": {"tournament_decision": "promoted"}},
    )
    # v4: still in flight (no outcome at all).
    _write_json(gens / "v4" / "experiment.json", {"parent_generation_id": "v3"})
    # A second epoch with one generation (for the ?epoch= scoping test).
    _write_json(
        ws / "epochs" / OTHER / "generations" / "w0" / "experiment.json",
        {"parent_generation_id": None},
    )
    return ws


# ---------------------------------------------------------------------------
# The classifier itself.
# ---------------------------------------------------------------------------


def test_canonical_decision_vocabulary() -> None:
    assert canonical_decision("promoted") == "promoted"
    assert canonical_decision("promote") == "promoted"
    assert canonical_decision("won") == "promoted"
    assert canonical_decision("rejected") == "rejected"
    assert canonical_decision("Reject") == "rejected"
    assert canonical_decision("deferred") == "deferred"
    # unknown tokens pass through lowercased — never guessed into a verdict.
    assert canonical_decision("baseline") == "baseline"
    assert canonical_decision(None) is None
    assert canonical_decision("  ") is None


def test_promoted_tristate_never_defaults_absent_to_false() -> None:
    assert promoted_tristate(None) is None
    assert promoted_tristate("") is None
    assert promoted_tristate("promoted") is True
    assert promoted_tristate("rejected") is False
    assert promoted_tristate("deferred") is False


def test_experiment_decision_reads_outcome_shapes() -> None:
    assert experiment_decision({"outcome": "promoted"}) == "promoted"
    assert experiment_decision({"outcome": {"tournament_decision": "rejected"}}) == "rejected"
    assert experiment_decision({"outcome": {"decision": "deferred"}}) == "deferred"
    assert experiment_decision({"outcome": None}) is None
    assert experiment_decision({}) is None


# ---------------------------------------------------------------------------
# The epoch payload: stamped experiments + the current_champion pointer.
# ---------------------------------------------------------------------------


def test_epoch_experiments_are_stamped(tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    view = build_epoch_view(WorkspacePaths(ws))
    by_gen = {e["generation_id"]: e for e in view["experiments"]}
    assert by_gen["v1"]["decision"] == "promoted"
    assert by_gen["v1"]["promoted"] is True
    assert by_gen["v2"]["decision"] == "rejected"
    assert by_gen["v2"]["promoted"] is False
    # An in-flight record is stamped tri-state None, never False (Class B).
    assert by_gen["v4"]["decision"] is None
    assert by_gen["v4"]["promoted"] is None
    assert by_gen["v0"]["decision"] is None
    assert by_gen["v0"]["promoted"] is None


def test_epoch_experiments_agree_with_lineage(tmp_path: Path) -> None:
    """The two feeds share ONE classifier — they can never disagree."""
    ws = _seed_workspace(tmp_path)
    view = build_epoch_view(WorkspacePaths(ws))
    lineage = build_lineage_view(WorkspacePaths(ws), EPOCH)
    lin_by_gen = {g["generation_id"]: g["promoted"] for g in lineage["generations"]}
    for exp in view["experiments"]:
        assert exp["promoted"] == lin_by_gen[exp["generation_id"]]


def test_current_champion_is_the_spine_end(tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    view = build_epoch_view(WorkspacePaths(ws))
    # spine: v1 -> v3; the REIGNING champion is v3, never the first-promoted.
    assert view["current_champion"] == "v3"


def test_current_champion_falls_back_to_the_seed(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    gens = ws / "epochs" / EPOCH / "generations"
    _write_json(gens / "v0" / "experiment.json", {"parent_generation_id": None})
    _write_json(
        gens / "v1" / "experiment.json",
        {"parent_generation_id": "v0", "outcome": {"tournament_decision": "rejected"}},
    )
    view = build_epoch_view(WorkspacePaths(ws))
    assert view["current_champion"] == "v0"


def test_current_champion_none_when_epoch_is_empty(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    (ws / "epochs" / EPOCH).mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    view = build_epoch_view(WorkspacePaths(ws))
    assert view["current_champion"] is None


# ---------------------------------------------------------------------------
# The epoch-scoped generations feed.
# ---------------------------------------------------------------------------


def test_lineage_view_scopes_to_one_epoch(tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    scoped = build_lineage_view(WorkspacePaths(ws), EPOCH)
    ids = {g["generation_id"] for g in scoped["generations"]}
    assert ids == {"v0", "v1", "v2", "v3", "v4"}
    other = build_lineage_view(WorkspacePaths(ws), OTHER)
    assert {g["generation_id"] for g in other["generations"]} == {"w0"}
    # unscoped stays workspace-global.
    everything = build_lineage_view(WorkspacePaths(ws))
    assert {g["generation_id"] for g in everything["generations"]} == ids | {"w0"}


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    return d


def test_lineage_endpoint_scopes_and_rejects_bad_epoch(tmp_path: Path, static_dir: Path) -> None:
    ws = _seed_workspace(tmp_path)
    client = TestClient(create_app(ws, static_dir, read_only=True))
    scoped = client.get(f"/api/lineage?epoch={OTHER}")
    assert scoped.status_code == 200
    assert [g["generation_id"] for g in scoped.json()["generations"]] == ["w0"]
    bad = client.get("/api/lineage?epoch=../../etc")
    assert bad.status_code == 404


def test_epoch_endpoint_serves_decision_surface(tmp_path: Path, static_dir: Path) -> None:
    ws = _seed_workspace(tmp_path)
    client = TestClient(create_app(ws, static_dir, read_only=True))
    payload = client.get("/api/epoch").json()
    assert payload["current_champion"] == "v3"
    by_gen = {e["generation_id"]: e for e in payload["experiments"]}
    assert by_gen["v2"]["decision"] == "rejected"
    assert by_gen["v4"]["promoted"] is None
