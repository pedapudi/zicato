"""Readers select the primary promotion named by a completed tournament.

A round may retain several promising candidates. These tests ensure the
recorded primary supplies the reigning champion and the tournament timeline,
regardless of generation ordering or other candidates' promotion status.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests._workspace_support import write_generation, write_lineage
from zicato.index.schema import apply_schema
from zicato.query import WorkspacePaths, build_epoch_view, build_round_timeline
from zicato.workspace import WorkspaceLayout

EPOCH = "2026-06-01_e0"

#: round 0 promotes v2 AND v11; v11 is the recorded head and defends round 1.
HEAD = "v11"
OTHER_MEMBER = "v2"


def _write_json(path: Path, obj: object) -> None:
    if path.name == "lineage.json":
        assert isinstance(obj, dict)
        write_lineage(WorkspaceLayout.from_root(path.parent), obj)
    elif path.name == "experiment.json":
        assert isinstance(obj, dict)
        write_generation(
            WorkspaceLayout.from_root(path.parents[4]),
            path.parents[2].name,
            path.parent.name,
            experiment=obj,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")


def _competitors(champion: str, challengers: list[str]) -> str:
    rows: list[dict[str, object]] = [{"generation_id": champion, "seed": 1, "role": "champion"}]
    rows += [
        {"generation_id": gid, "seed": i + 2, "role": "challenger"}
        for i, gid in enumerate(challengers)
    ]
    return json.dumps(rows)


def _field_record(
    ws: Path, first_challenger: str, champion: str, challengers: list[str], head: str
) -> None:
    """Publish one round's complete snapshot through the canonical record owner."""
    body = {
        "tournament_id": f"{EPOCH}:field:{first_challenger}",
        "epoch_id": EPOCH,
        "structure": "swiss",
        "competitors": json.loads(_competitors(champion, challengers)),
        "promoted_generation_id": head,
        "champion_generation_id": champion,
        "decision": "promoted" if head else "rejected",
        "state": "settled",
        "structure_params": {},
        "ran_at": "2026-06-01T00:00:00Z",
        "reason": "",
        "rounds": [],
        "standings": [],
        "field_status": [],
    }
    if head == HEAD:
        body["promoted_generation_ids"] = [OTHER_MEMBER, HEAD]
    from tests._workspace_support import complete_round

    complete_round(ws, EPOCH, challengers, primary_id=head or None, field_record=body)


def _multi_promote_workspace(tmp_path: Path) -> Path:
    """Two swiss rounds; round 0 promotes a SET whose head is v11.

    v2 sorts first both lexicographically and naturally, so every ordering
    fallback in either reader answers ``v2`` — only reading the record gives
    ``v11``, which is the generation the runner actually crowned and the one
    round 1's own record names as its defending champion.
    """
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    edir = ws / "epochs" / EPOCH
    _write_json(edir / "config.json", {"contract_hash": "h", "closed": False})
    _write_json(edir / "scoring.json", {"tournament": {"structure": "swiss", "params": {}}})

    gens = edir / "generations"
    _write_json(gens / "v0" / "experiment.json", {"parent_generation_id": None})
    for gid, parent, decision in (
        ("v1", "v0", "rejected"),
        ("v2", "v0", "promoted"),
        ("v11", "v0", "promoted"),
        ("v12", "v11", "rejected"),
    ):
        _write_json(
            gens / gid / "experiment.json",
            {
                "parent_generation_id": parent,
                "round_index": 1 if gid == "v12" else 0,
                "outcome": {"tournament_decision": decision, "structure": "swiss"},
            },
        )
    _write_json(
        ws / "lineage.json",
        {
            "epochs": [
                {
                    "id": EPOCH,
                    "generations": [
                        {"id": "v0", "parent_id": None, "promoted": True},
                        {"id": "v1", "parent_id": "v0", "promoted": False},
                        {"id": "v2", "parent_id": "v0", "promoted": True},
                        {"id": "v11", "parent_id": "v0", "promoted": True},
                        {"id": "v12", "parent_id": "v11", "promoted": False},
                    ],
                }
            ]
        },
    )

    conn = sqlite3.connect(ws / "index.db")
    apply_schema(conn)
    conn.executemany(
        "INSERT INTO generations(epoch_id,generation_id,parent_generation_id,promoted,created_at) "
        "VALUES(?,?,?,?,?)",
        [
            (EPOCH, "v0", None, 1, "2026-06-01T00:00:00Z"),
            (EPOCH, "v1", "v0", 0, "2026-06-01T01:00:00Z"),
            (EPOCH, "v2", "v0", 1, "2026-06-01T01:01:00Z"),
            (EPOCH, "v11", "v0", 1, "2026-06-01T01:02:00Z"),
            (EPOCH, "v12", "v11", 0, "2026-06-01T02:00:00Z"),
        ],
    )
    conn.executemany(
        "INSERT INTO tournaments(tournament_id, epoch_id, parent_generation_id, "
        "child_generation_id, decision, parent_scalar, ran_at, structure, "
        "competitors_json, rounds_json, standings_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            # round 0: v11's crowning duel, then the round's own field row.
            # A field row's parent/child columns are empty by design.
            (
                f"{EPOCH}:v0->v11",
                EPOCH,
                "v0",
                "v11",
                "promoted",
                0.5,
                "2026-06-01T01:05:00Z",
                "swiss",
                _competitors("v0", ["v11"]),
                None,
                None,
            ),
            (
                f"{EPOCH}:field:v1",
                EPOCH,
                "",
                "",
                "promoted",
                None,
                "2026-06-01T01:06:00Z",
                "swiss",
                _competitors("v0", ["v1", "v2", "v11"]),
                "[]",
                "[]",
            ),
            # round 1: v11 defends and holds.
            (
                f"{EPOCH}:v11->v12",
                EPOCH,
                "v11",
                "v12",
                "rejected",
                0.2,
                "2026-06-01T02:05:00Z",
                "swiss",
                _competitors("v11", ["v12"]),
                None,
                None,
            ),
            (
                f"{EPOCH}:field:v12",
                EPOCH,
                "",
                "",
                "rejected",
                None,
                "2026-06-01T02:06:00Z",
                "swiss",
                _competitors("v11", ["v12"]),
                "[]",
                "[]",
            ),
        ],
    )
    conn.commit()
    conn.close()

    _field_record(ws, "v1", "v0", ["v1", "v2", "v11"], HEAD)
    _field_record(ws, "v12", "v11", ["v12"], "")
    return ws


def _rounds(ws: Path) -> list[dict]:
    return build_round_timeline(WorkspacePaths(ws), EPOCH)["rounds"]


# ---------------------------------------------------------------------------
# The round timeline.
# ---------------------------------------------------------------------------


def test_gate_names_the_recorded_head_not_the_first_flagged_member(tmp_path: Path) -> None:
    """``gate.gen`` is the generation that took the title, not v2.

    The reported regression: round 0 served ``gate={'kind': 'promoted',
    'gen': 'v2'}`` while its own round 1 served champion ``v11`` — the
    timeline contradicting itself inside one payload.
    """
    rounds = _rounds(_multi_promote_workspace(tmp_path))
    assert [r["round_index"] for r in rounds] == [0, 1]
    assert rounds[0]["gate"] == {"kind": "promoted", "gen": HEAD}
    assert rounds[1]["champion"]["id"] == HEAD
    # both members stay flagged promoted — the record picks the head, it does
    # not rewrite what lineage recorded about the set.
    assert {c["id"]: c["promoted"] for c in rounds[0]["challengers"]} == {
        "v1": False,
        OTHER_MEMBER: True,
        HEAD: True,
    }


def test_waterfall_step_credits_the_recorded_head(tmp_path: Path) -> None:
    """The loss-floor step names the same generation the gate does."""
    tl = build_round_timeline(WorkspacePaths(_multi_promote_workspace(tmp_path)), EPOCH)
    assert tl["waterfall"][0]["gen"] == HEAD


def test_current_champion_resolves_the_branch_by_the_recorded_head(tmp_path: Path) -> None:
    """The reigning champion is the recorded head of the branching round."""
    view = build_epoch_view(WorkspacePaths(_multi_promote_workspace(tmp_path)), epoch_id=EPOCH)
    assert view["current_champion"] == HEAD


def test_current_champion_agrees_with_the_round_timeline(tmp_path: Path) -> None:
    """One head, two readers: the epoch pointer IS the last round's winner."""
    ws = _multi_promote_workspace(tmp_path)
    view = build_epoch_view(WorkspacePaths(ws), epoch_id=EPOCH)
    assert view["current_champion"] == _rounds(ws)[-1]["champion"]["id"]


def test_current_champion_does_not_infer_a_winner_without_a_record(tmp_path: Path) -> None:
    """Without a committed promotion, the baseline remains the champion."""
    ws = _multi_promote_workspace(tmp_path)
    for record in (ws / "epochs" / EPOCH / "rounds").glob("*/field_settlement.json"):
        record.unlink()
    view = build_epoch_view(WorkspacePaths(ws), epoch_id=EPOCH)
    assert view["current_champion"] == "v0"
