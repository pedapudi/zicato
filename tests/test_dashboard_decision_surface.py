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

from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.query import (
    WorkspacePaths,
    build_epoch_view,
    build_lineage_view,
)
from zicato.query.decisions import (
    canonical_decision,
    experiment_decision,
    promoted_tristate,
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
    # v0: the promoted seed (no parent).
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
    _write_json(
        ws / "lineage.json",
        {
            "epochs": [
                {
                    "id": EPOCH,
                    "generations": [
                        {"id": "v0", "parent_id": None, "promoted": True},
                        {"id": "v1", "parent_id": "v0", "promoted": True},
                        {"id": "v2", "parent_id": "v1", "promoted": False},
                        {"id": "v3", "parent_id": "v1", "promoted": True},
                        {"id": "v4", "parent_id": "v3", "promoted": None},
                    ],
                }
            ]
        },
    )
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
    # An in-flight record is stamped tri-state None, never False (Class B);
    # its decision is the classifier's ``pending``, the same token the
    # lineage feed serves for a candidate that has not settled.
    assert by_gen["v4"]["decision"] == "pending"
    assert by_gen["v4"]["decision_label"] == "undecided"
    assert by_gen["v4"]["promoted"] is None
    # The seed faced no gate. It carries ``promoted: true`` (it is the
    # incoming champion of round 0) but its decision is the classifier's
    # ``baseline``, never a win it never contested.
    assert by_gen["v0"]["decision"] == "baseline"
    assert by_gen["v0"]["decision_label"] == "seed (v0)"
    assert by_gen["v0"]["promoted"] is True


def test_epoch_experiments_agree_with_lineage(tmp_path: Path) -> None:
    """Every operator feed uses lineage as its one decision authority.

    Pins the whole disagreement class, not one node: for EVERY generation
    of the epoch — the seed included — the epoch feed and the lineage feed
    must serve the identical (promoted, decision, decision_label) triple.
    A feed that derives any part of that triple locally instead of copying
    the classifier's output fails here.
    """
    ws = _seed_workspace(tmp_path)
    view = build_epoch_view(WorkspacePaths(ws))
    lineage = build_lineage_view(WorkspacePaths(ws), EPOCH)

    def surface(rec: dict[str, object]) -> tuple[object, ...]:
        return tuple(rec.get(k) for k in ("promoted", "decision", "decision_label"))

    lin_by_gen = {g["generation_id"]: surface(g) for g in lineage["generations"]}
    epoch_by_gen = {e["generation_id"]: surface(e) for e in view["experiments"]}
    assert epoch_by_gen.keys() == lin_by_gen.keys()
    assert epoch_by_gen == lin_by_gen
    # And the seed's triple is the honest one both feeds now agree on.
    assert epoch_by_gen["v0"] == (True, "baseline", "seed (v0)")


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
