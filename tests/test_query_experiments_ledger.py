"""Known-answer tests for the EXPERIMENTS LEDGER reader (issue #194 §3).

``build_experiments_ledger`` is a pure join over the analytical index:
``experiments`` (idea + verdict + deltas) × ``patches`` (the sites touched)
× ``generations`` (the birth round). The tests pin the three things a join
can get wrong — the SHAPE of a row, the CORRECTNESS of the join (sites land
on their own experiment; the round comes from the generation), and the
DEGRADES (no index / no epoch / unknown epoch / a legacy index with no
``round_index`` column) — plus the round ordering the ledger renders in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from tests._workspace_support import seed_index, workspace, write_epoch
from zicato.dashboard.server import create_app
from zicato.query import build_experiments_ledger
from zicato.query.paths import WorkspacePaths
from zicato.workspace import WorkspaceLayout

EPOCH = "e_ledger"


def _paths(layout: WorkspaceLayout) -> WorkspacePaths:
    return WorkspacePaths(layout.root)


def _seed_workspace(tmp_path: Path) -> WorkspaceLayout:
    """The on-disk epoch the id resolver validates against."""
    layout = workspace(tmp_path)
    write_epoch(
        layout,
        EPOCH,
        config={"id": EPOCH, "created_at": "2026-07-01", "closed": True},
        current=True,
    )
    return layout


def _gen(
    gid: str,
    parent: str | None,
    promoted: int | None,
    created: str,
    round_index: int | None,
) -> dict[str, Any]:
    return {
        "epoch_id": EPOCH,
        "generation_id": gid,
        "parent_generation_id": parent,
        "promoted": promoted,
        "created_at": created,
        "round_index": round_index,
    }


def _exp(
    gid: str,
    core_idea: str | None,
    decision: str | None,
    *,
    rejection: str = "",
    scalar: float | None = None,
    drift: float | None = None,
    pass_rate: float | None = None,
) -> dict[str, Any]:
    return {
        "epoch_id": EPOCH,
        "generation_id": gid,
        "hypothesis_core_idea": core_idea,
        "hypothesis_why": "because",
        "hypothesis_json": None,
        "tournament_decision": decision,
        "rejection_reason": rejection,
        "scalar_score_delta": scalar,
        "drift_loss_delta": drift,
        "pass_rate_delta": pass_rate,
        "outcome_json": None,
    }


def _patch(gid: str, patch_id: str, mutation_id: str) -> dict[str, Any]:
    return {
        "patch_id": patch_id,
        "epoch_id": EPOCH,
        "generation_id": gid,
        "mutation_id": mutation_id,
        "op": "replace",
        "rationale": "r",
    }


def _seed_index(layout: WorkspaceLayout) -> None:
    seed_index(
        layout,
        {
            "epochs": [
                {
                    "epoch_id": EPOCH,
                    "contract_hash": "h",
                    "created_at": "2026-07-01",
                    "closed": 1,
                }
            ],
            "generations": [
                # v0 seed (round 0), then round 1 mints v2 and v1 — v2 CREATED
                # FIRST, so the round-1 tie must break on created_at, not on the id.
                _gen("v0", None, 1, "2026-07-01T00:00:00Z", 0),
                _gen("v2", "v0", 0, "2026-07-02T00:00:00Z", 1),
                _gen("v1", "v0", 1, "2026-07-02T00:01:00Z", 1),
                # ... and round 2 mints v3, which is still in flight (no decision).
                _gen("v3", "v1", None, "2026-07-03T00:00:00Z", 2),
            ],
            "experiments": [
                _exp("v0", "the seed", "promoted", scalar=None),
                _exp(
                    "v2",
                    "widen the outline rubric",
                    "rejected",
                    rejection="insufficient improvement: 0.73 vs 0.72 (margin 0.0200)",
                    scalar=0.014,
                    drift=0.01,
                    pass_rate=-0.25,
                ),
                _exp("v1", "name the audience up front", "promoted", scalar=-0.08, drift=-0.06),
                _exp("v3", "cut the preamble", None),
            ],
            # v2 touched two sites; one of them TWICE (a re-edit within one proposal).
            "patches": [
                _patch("v2", "p1", "prompt.system"),
                _patch("v2", "p2", "prompt.system"),
                _patch("v2", "p3", "agent.temperature"),
                _patch("v1", "p4", "prompt.audience"),
            ],
        },
    )


def _by_gen(ledger: dict) -> dict[str, dict]:
    return {r["generation_id"]: r for r in ledger["experiments"]}


# ---------------------------------------------------------------------------
# Shape + join correctness
# ---------------------------------------------------------------------------


def test_ledger_row_shape_and_join(tmp_path: Path) -> None:
    """One row per experiment, carrying idea + sites + verdict + deltas + round."""
    layout = _seed_workspace(tmp_path)
    _seed_index(layout)
    ledger = build_experiments_ledger(_paths(layout), EPOCH)

    assert ledger["epoch_id"] == EPOCH
    # a healthy read carries NO note (omit-when-default).
    assert "note" not in ledger

    rows = _by_gen(ledger)
    assert set(rows) == {"v0", "v1", "v2", "v3"}
    assert set(rows["v2"]) == {
        "generation_id",
        "parent_generation_id",
        "round_index",
        "core_idea",
        "mutation_ids",
        "decision",
        "promoted",
        "rejection_reason",
        "scalar_score_delta",
        "drift_loss_delta",
        "pass_rate_delta",
    }

    v2 = rows["v2"]
    assert v2["core_idea"] == "widen the outline rubric"
    assert v2["decision"] == "rejected"
    assert v2["promoted"] is False
    assert v2["rejection_reason"].startswith("insufficient improvement")
    assert v2["scalar_score_delta"] == 0.014
    assert v2["pass_rate_delta"] == -0.25
    # the birth round comes from the GENERATION row, not the experiment.
    assert v2["round_index"] == 1
    # sites are DEDUPED (two patches against prompt.system name it once) and
    # scoped to their own experiment.
    assert v2["mutation_ids"] == ["agent.temperature", "prompt.system"]
    assert rows["v1"]["mutation_ids"] == ["prompt.audience"]
    # an experiment with no patch rows reads as an empty site list, not null.
    assert rows["v0"]["mutation_ids"] == []
    # the parent is carried so the renderer can name the parentless SEED as the
    # baseline rather than a candidate still racing (it recorded no decision
    # because it never faced a gate).
    assert rows["v2"]["parent_generation_id"] == "v0"
    assert rows["v0"]["parent_generation_id"] is None


def test_unsettled_experiment_keeps_its_row(tmp_path: Path) -> None:
    """An in-flight experiment degrades FIELD-BY-FIELD — it never vanishes."""
    layout = _seed_workspace(tmp_path)
    _seed_index(layout)
    v3 = _by_gen(build_experiments_ledger(_paths(layout), EPOCH))["v3"]

    assert v3["core_idea"] == "cut the preamble"
    assert v3["decision"] is None
    # tri-state: NEVER a default False on a candidate that has not raced.
    assert v3["promoted"] is None
    assert v3["rejection_reason"] is None
    assert v3["scalar_score_delta"] is None
    assert v3["round_index"] == 2


def test_rows_are_in_round_order(tmp_path: Path) -> None:
    """Round ascending; within a round, the index's own (created_at) order."""
    layout = _seed_workspace(tmp_path)
    _seed_index(layout)
    ledger = build_experiments_ledger(_paths(layout), EPOCH)

    assert [r["generation_id"] for r in ledger["experiments"]] == ["v0", "v2", "v1", "v3"]
    assert [r["round_index"] for r in ledger["experiments"]] == [0, 1, 1, 2]


def test_unstamped_round_sorts_last(tmp_path: Path) -> None:
    """A generation with no birth round trails the stamped sequence."""
    layout = _seed_workspace(tmp_path)
    _seed_index(layout)
    seed_index(
        layout,
        {
            # the earliest generation, and the only unstamped one
            "generations": [_gen("v9", "v1", 0, "2026-06-01T00:00:00Z", None)],
            "experiments": [_exp("v9", "a legacy experiment", "rejected")],
        },
    )

    ledger = build_experiments_ledger(_paths(layout), EPOCH)
    assert [r["generation_id"] for r in ledger["experiments"]][-1] == "v9"
    assert ledger["experiments"][-1]["round_index"] is None


def test_legacy_decision_spellings_are_canonicalised(tmp_path: Path) -> None:
    """The ledger speaks the ONE canonical verdict vocabulary."""
    layout = _seed_workspace(tmp_path)
    _seed_index(layout)
    seed_index(
        layout,
        {
            "generations": [_gen("v4", "v1", 1, "2026-07-04T00:00:00Z", 3)],
            "experiments": [_exp("v4", "an old record", "accept")],
        },
    )

    v4 = _by_gen(build_experiments_ledger(_paths(layout), EPOCH))["v4"]
    assert v4["decision"] == "promoted"
    assert v4["promoted"] is True


# ---------------------------------------------------------------------------
# Degrades
# ---------------------------------------------------------------------------


def test_absent_index_degrades_with_a_note(tmp_path: Path) -> None:
    """No index → an empty ledger + the honest reindex note, never a raise."""
    layout = _seed_workspace(tmp_path)
    ledger = build_experiments_ledger(_paths(layout), EPOCH)
    assert ledger == {
        "epoch_id": EPOCH,
        "experiments": [],
        "note": "index not built; run zicato repair index",
    }


def test_no_epoch_and_unknown_epoch_degrade_to_the_empty_ledger(tmp_path: Path) -> None:
    """No current epoch / an unknown id reads empty — the endpoint never 500s."""
    empty = workspace(tmp_path)
    assert build_experiments_ledger(_paths(empty)) == {"epoch_id": None, "experiments": []}
    layout = _seed_workspace(tmp_path)
    _seed_index(layout)
    assert build_experiments_ledger(_paths(layout), "nope") == {
        "epoch_id": None,
        "experiments": [],
    }


def test_legacy_index_without_round_index_still_reads(tmp_path: Path) -> None:
    """A pre-v7 index (no ``round_index`` column) reads with null rounds.

    The regression this guards: naming ``round_index`` in the SELECT would
    fail the whole query on a legacy index and blank the ledger, rather than
    degrading the ONE field that is genuinely absent.
    """
    layout = _seed_workspace(tmp_path)
    legacy_generation = _gen("v1", "v0", 1, "2026-07-02T00:00:00Z", None)
    del legacy_generation["round_index"]
    seed_index(
        layout,
        {
            "generations": [legacy_generation],
            "experiments": [_exp("v1", "a legacy idea", "promoted", scalar=-0.05)],
            "patches": [_patch("v1", "p1", "prompt.system")],
        },
        without_columns=(("generations", "round_index"),),
    )

    ledger = build_experiments_ledger(_paths(layout), EPOCH)
    assert [r["generation_id"] for r in ledger["experiments"]] == ["v1"]
    assert ledger["experiments"][0]["round_index"] is None
    assert ledger["experiments"][0]["mutation_ids"] == ["prompt.system"]
    assert ledger["experiments"][0]["scalar_score_delta"] == -0.05


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_endpoint_serves_the_ledger_and_degrades_at_200(tmp_path: Path) -> None:
    layout = _seed_workspace(tmp_path)
    _seed_index(layout)
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    with TestClient(create_app(layout.root, static_dir, read_only=True)) as client:
        res = client.get(f"/api/epoch/{EPOCH}/experiments-ledger")
        assert res.status_code == 200
        assert [r["generation_id"] for r in res.json()["experiments"]] == ["v0", "v2", "v1", "v3"]

        # a malformed id degrades at 200 like every other coordinate handler.
        bad = client.get("/api/epoch/not a real epoch!/experiments-ledger")
        assert bad.status_code == 200
        assert bad.json()["experiments"] == []

        # ... and so does a well-formed id for an epoch that does not exist.
        unknown = client.get("/api/epoch/e_nope/experiments-ledger")
        assert unknown.status_code == 200
        assert unknown.json() == {"epoch_id": None, "experiments": []}
