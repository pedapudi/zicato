"""Which member of a promoted SET the served surfaces name as the head.

``lineage.json`` owns topology and the tri-state promotion flag, and on a
round that promotes a set it flags EVERY member — only one headed the round
and defended afterwards. Both readers used to re-derive that head by taking
the first flagged member, which is an ordering accident:

* ``build_round_timeline`` reported ``gate.gen`` = whichever promoted member
  came first in bucket order, contradicting the champion its own next round
  served from the record (issue #287);
* ``build_epoch_view``'s ``current_champion`` walked the branch with a
  lexicographic tiebreak, so ``v11`` beat ``v2`` for reasons of spelling
  (issue #281).

These tests pin the resolution: the head is the one the runner RECORDED
(``zicato.query.promoted_head``), the reconstruction is the fallback, and a
disagreement between the two is logged rather than served silently.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from zicato.query import WorkspacePaths, build_epoch_view, build_round_timeline

EPOCH = "2026-06-01_e0"

#: round 0 promotes v2 AND v11; v11 is the recorded head and defends round 1.
HEAD = "v11"
OTHER_MEMBER = "v2"


def _write_json(path: Path, obj: object) -> None:
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
    """One round's durable field-tournament snapshot, as the runner writes it."""
    _write_json(
        ws / "epochs" / EPOCH / "tournaments" / f"field-{first_challenger}.json",
        {
            "tournament_id": f"{EPOCH}:field:{first_challenger}",
            "epoch_id": EPOCH,
            "structure": "swiss",
            "competitors": json.loads(_competitors(champion, challengers)),
            "promoted_generation_id": head,
            "champion_generation_id": champion,
            "decision": "promoted" if head else "held",
            "state": "settled",
        },
    )


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
            {"parent_generation_id": parent, "outcome": {"tournament_decision": decision}},
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
    conn.executescript(
        """
        CREATE TABLE generations(epoch_id TEXT, generation_id TEXT,
            parent_generation_id TEXT, promoted INTEGER, created_at TEXT,
            PRIMARY KEY(epoch_id, generation_id));
        CREATE TABLE experiments(epoch_id TEXT, generation_id TEXT,
            hypothesis_core_idea TEXT, PRIMARY KEY(epoch_id, generation_id));
        CREATE TABLE tournaments(tournament_id TEXT PRIMARY KEY, epoch_id TEXT,
            parent_generation_id TEXT, child_generation_id TEXT, decision TEXT,
            parent_scalar REAL, child_scalar REAL, delta_scalar REAL,
            rejection_reason TEXT, ran_at TEXT,
            structure TEXT, structure_params_json TEXT, competitors_json TEXT,
            rounds_json TEXT, standings_json TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO generations VALUES(?,?,?,?,?)",
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
                "held",
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


def test_a_head_disagreement_is_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A record that overrides the reconstruction says so in the log.

    The disagreement is an operator signal, never a payload field: the served
    answer is the record either way, and the round timeline's wire shape is
    unchanged.
    """
    ws = _multi_promote_workspace(tmp_path)
    with caplog.at_level(logging.INFO, logger="zicato.query"):
        rounds = _rounds(ws)
    lines = [r.getMessage() for r in caplog.records if "recorded head" in r.getMessage()]
    assert len(lines) == 1
    assert "round 0" in lines[0]
    assert HEAD in lines[0] and OTHER_MEMBER in lines[0]
    assert "field_record" in lines[0]
    assert "note" not in rounds[0] and "disagreement" not in rounds[0]


def test_an_agreeing_record_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A single-promotion round agrees with its reconstruction and stays quiet."""
    ws = _multi_promote_workspace(tmp_path)
    lineage = json.loads((ws / "lineage.json").read_text(encoding="utf-8"))
    for gen in lineage["epochs"][0]["generations"]:
        if gen["id"] == OTHER_MEMBER:
            gen["promoted"] = False
    _write_json(ws / "lineage.json", lineage)
    with caplog.at_level(logging.INFO, logger="zicato.query"):
        rounds = _rounds(ws)
    assert rounds[0]["gate"] == {"kind": "promoted", "gen": HEAD}
    assert [r.getMessage() for r in caplog.records if "disagrees" in r.getMessage()] == []


def test_head_falls_back_to_the_next_rounds_defending_champion(tmp_path: Path) -> None:
    """Round N's snapshot is gone; round N+1 still names who defended it."""
    ws = _multi_promote_workspace(tmp_path)
    (ws / "epochs" / EPOCH / "tournaments" / "field-v1.json").unlink()
    rounds = _rounds(ws)
    assert rounds[0]["gate"] == {"kind": "promoted", "gen": HEAD}


def test_head_falls_back_to_the_lineage_flags_without_any_record(tmp_path: Path) -> None:
    """No record survives: the first flagged member is the honest guess.

    Constructed by dropping round 1 as well — while a later round exists, its
    record still names the defender, so the reconstruction is genuinely the
    LAST resort. It is a deterministic tiebreak, not a correct answer; pinned
    so the degrade is visible rather than accidental.
    """
    ws = _multi_promote_workspace(tmp_path)
    for record in (ws / "epochs" / EPOCH / "tournaments").glob("field-*.json"):
        record.unlink()
    conn = sqlite3.connect(ws / "index.db")
    conn.execute("DELETE FROM tournaments WHERE tournament_id LIKE ?", (f"{EPOCH}:%v12",))
    conn.commit()
    conn.close()
    rounds = _rounds(ws)
    assert [r["round_index"] for r in rounds] == [0]
    assert rounds[0]["gate"] == {"kind": "promoted", "gen": OTHER_MEMBER}


def test_a_record_outside_the_promoted_set_never_adds_a_promotion(tmp_path: Path) -> None:
    """Lineage owns WHETHER; the record only disambiguates WITHIN the set.

    A record naming a generation the lineage did not flag promoted is skipped
    for the next source rather than crowning an unpromoted challenger.
    """
    ws = _multi_promote_workspace(tmp_path)
    _field_record(ws, "v1", "v0", ["v1", "v2", "v11"], "v1")
    rounds = _rounds(ws)
    assert rounds[0]["gate"] == {"kind": "promoted", "gen": HEAD}  # from round 1's record
    assert rounds[0]["challengers"][0] == {"id": "v1", "scalar": None, "promoted": False}


# ---------------------------------------------------------------------------
# The epoch view's reigning-champion pointer.
# ---------------------------------------------------------------------------


def test_current_champion_resolves_the_branch_by_the_recorded_head(tmp_path: Path) -> None:
    """The reigning champion is the recorded head of the branching round."""
    view = build_epoch_view(WorkspacePaths(_multi_promote_workspace(tmp_path)), epoch_id=EPOCH)
    assert view["current_champion"] == HEAD


def test_current_champion_agrees_with_the_round_timeline(tmp_path: Path) -> None:
    """One head, two readers: the epoch pointer IS the last round's winner."""
    ws = _multi_promote_workspace(tmp_path)
    view = build_epoch_view(WorkspacePaths(ws), epoch_id=EPOCH)
    assert view["current_champion"] == _rounds(ws)[-1]["champion"]["id"]


def test_current_champion_tiebreaks_naturally_without_a_record(tmp_path: Path) -> None:
    """No record survives the branch: natural order decides, so v2 wins.

    The lexicographic sort this walk used answered ``v11`` here — the right
    id for the wrong reason. Natural order is the deterministic fallback and
    it is honest about being a tiebreak, not a resolution.
    """
    ws = _multi_promote_workspace(tmp_path)
    for record in (ws / "epochs" / EPOCH / "tournaments").glob("field-*.json"):
        record.unlink()
    view = build_epoch_view(WorkspacePaths(ws), epoch_id=EPOCH)
    assert view["current_champion"] == OTHER_MEMBER
