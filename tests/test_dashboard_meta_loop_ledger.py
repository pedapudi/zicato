"""The cross-epoch COMPOSED META-LOOP LEDGER matrix (study opt 7).

Pins :func:`build_meta_loop_ledger` (and its surfacing as the
``/api/workspace`` ``ledger`` sibling field):

* the per-epoch row carries the held floor (derived from the index
  ``loss_profiles``), the champion generation that set it, the effort
  (``generation_count``), the frozen structure, lifecycle, and the
  per-component change MAP vs the predecessor;
* the change map SUPERSETS the L1 contract-diff with the two levers it
  omits — ``proposer`` (persisted in ``contract_components.json`` but
  dropped by the diff endpoint) and ``structure`` (derived from each
  epoch's frozen ``scoring.json`` ``tournament.structure``);
* a proposer/skills change AND a structure roll are each detected;
* an absent (legacy) component hash on one side is "no signal", not a
  spurious change;
* the first epoch has an all-unchanged map (nothing to diff against);
* the matrix degrades gracefully (empty list) on a workspace with no
  ``epochs/`` directory.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from zicato.dashboard.state_reader import (
    WorkspacePaths,
    build_meta_loop_ledger,
    build_workspace_view,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_index(path: Path, profiles: list[tuple[str, str, str, float]]) -> None:
    """Seed a minimal index with just the ``loss_profiles`` the floor reads.

    ``profiles`` is a list of ``(epoch_id, generation_id, entry_id,
    drift_loss)`` rows. The floor / champion derive from the per-entry
    mean drift loss, so one entry per generation is enough.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE loss_profiles(run_id TEXT PRIMARY KEY, epoch_id TEXT, "
        "generation_id TEXT, entry_id TEXT, drift_loss REAL, pass_fail INTEGER, "
        "runtime_ms INTEGER, tokens INTEGER, turns INTEGER, wall_clock_budget_exceeded INTEGER)"
    )
    for i, (epoch_id, gen_id, entry_id, loss) in enumerate(profiles):
        conn.execute(
            "INSERT INTO loss_profiles VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"run_{i}", epoch_id, gen_id, entry_id, loss, 0, 0, 0, 0, 0),
        )
    conn.commit()
    conn.close()


def _make_epoch(
    ws: Path,
    epoch_id: str,
    *,
    components: dict[str, str],
    structure: str | None,
    gens: list[str],
    closed: bool,
) -> None:
    epoch_dir = ws / "epochs" / epoch_id
    epoch_dir.mkdir(parents=True, exist_ok=True)
    _write_json(epoch_dir / "config.json", {"id": epoch_id, "closed": closed})
    _write_json(epoch_dir / "contract_components.json", components)
    scoring: dict[str, object] = {"weights": {"drift_loss": 1.0}}
    if structure is not None:
        scoring["tournament"] = {"structure": structure, "params": {}}
    _write_json(epoch_dir / "scoring.json", scoring)
    for g in gens:
        (epoch_dir / "generations" / g).mkdir(parents=True, exist_ok=True)


def _chain_workspace(tmp_path: Path) -> Path:
    """A realistic 3-epoch chain exercising every component-change kind.

    * e0 (racing)     — baseline, no diff.
    * e1 (racing)     — BOARD changed (a hash diff); structure held.
    * e2 (swiss)      — SCORING changed + PROPOSER changed + STRUCTURE rolled
                        (racing→swiss, a SOFT seam). brief held.
    """
    ws = tmp_path / "ws"
    (ws / "epochs").mkdir(parents=True, exist_ok=True)
    (ws / "current_epoch").write_text("e2\n", encoding="utf-8")

    _make_epoch(
        ws,
        "e0",
        components={
            "board": "hb0",
            "brief": "hr0",
            "scoring": "hs0",
            "entrypoint": "he0",
            "mutable_trees": "hm0",
            "proposer": "hp0",
        },
        structure="racing",
        gens=["v0", "v1"],
        closed=True,
    )
    _make_epoch(
        ws,
        "e1",
        components={
            "board": "hb1",
            "brief": "hr0",
            "scoring": "hs0",
            "entrypoint": "he0",
            "mutable_trees": "hm0",
            "proposer": "hp0",
        },
        structure="racing",
        gens=["v2", "v3"],
        closed=True,
    )
    _make_epoch(
        ws,
        "e2",
        components={
            "board": "hb1",
            "brief": "hr0",
            "scoring": "hs2",
            "entrypoint": "he0",
            "mutable_trees": "hm0",
            "proposer": "hp2",
        },
        structure="swiss",
        gens=["v4"],
        closed=False,
    )

    _build_index(
        ws / "index.db",
        [
            ("e0", "v0", "x", 0.50),
            ("e0", "v1", "x", 0.40),  # floor 0.40 @ v1
            ("e1", "v2", "x", 0.35),
            ("e1", "v3", "x", 0.28),  # floor 0.28 @ v3
            ("e2", "v4", "x", 0.33),  # floor 0.33 @ v4
        ],
    )
    return ws


def test_ledger_per_epoch_rows_and_floor(tmp_path: Path) -> None:
    ws = _chain_workspace(tmp_path)
    ledger = build_meta_loop_ledger(WorkspacePaths(ws))

    assert ledger["current_epoch_id"] == "e2"
    rows = ledger["epochs"]
    assert [r["epoch_id"] for r in rows] == ["e0", "e1", "e2"]

    by = {r["epoch_id"]: r for r in rows}
    # floor = lowest per-generation mean drift loss; champion = the gen that set it.
    assert by["e0"]["floor"] == 0.40
    assert by["e0"]["champion_gen"] == "v1"
    assert by["e1"]["floor"] == 0.28
    assert by["e1"]["champion_gen"] == "v3"
    assert by["e2"]["floor"] == 0.33
    assert by["e2"]["champion_gen"] == "v4"
    # effort = generation_count.
    assert by["e0"]["generation_count"] == 2
    assert by["e2"]["generation_count"] == 1
    # frozen structure per epoch.
    assert by["e0"]["structure"] == "racing"
    assert by["e2"]["structure"] == "swiss"
    # lifecycle.
    assert by["e2"]["open"] is True and by["e2"]["closed"] is False
    assert by["e0"]["closed"] is True and by["e0"]["open"] is False


def test_ledger_champion_index_matches_floor_setter_position(tmp_path: Path) -> None:
    """``champion_index`` is the 0-based ordinal of the floor-setting gen.

    In the chain workspace e0 (v0,v1) sets its floor at v1 → index 1; e1
    (v2,v3) at v3 → index 1; e2 (v4 only) at v4 → index 0. Each is the
    position of ``champion_gen`` in the epoch's sorted generation list.
    """
    ws = _chain_workspace(tmp_path)
    by = {r["epoch_id"]: r for r in build_meta_loop_ledger(WorkspacePaths(ws))["epochs"]}
    assert by["e0"]["champion_gen"] == "v1" and by["e0"]["champion_index"] == 1
    assert by["e1"]["champion_gen"] == "v3" and by["e1"]["champion_index"] == 1
    assert by["e2"]["champion_gen"] == "v4" and by["e2"]["champion_index"] == 0


def test_ledger_champion_index_early_vs_late(tmp_path: Path) -> None:
    """A floor set EARLY → a small index; set LATE → a large index.

    Two 4-generation epochs: ``ee`` sets its floor at the FIRST gen
    (index 0, early), ``el`` at the LAST gen (index 3, late). The index
    tracks the gen's ordinal among the epoch's sorted generations.
    """
    ws = tmp_path / "ws"
    (ws / "epochs").mkdir(parents=True, exist_ok=True)
    (ws / "current_epoch").write_text("el\n", encoding="utf-8")
    comp = {
        "board": "b",
        "brief": "r",
        "scoring": "s",
        "entrypoint": "e",
        "mutable_trees": "m",
        "proposer": "p",
    }
    _make_epoch(
        ws, "ee", components=comp, structure="racing", gens=["v0", "v1", "v2", "v3"], closed=True
    )
    _make_epoch(
        ws, "el", components=comp, structure="racing", gens=["v0", "v1", "v2", "v3"], closed=False
    )
    _build_index(
        ws / "index.db",
        [
            # ee: floor at the FIRST gen v0 (early).
            ("ee", "v0", "x", 0.10),
            ("ee", "v1", "x", 0.40),
            ("ee", "v2", "x", 0.30),
            ("ee", "v3", "x", 0.50),
            # el: floor at the LAST gen v3 (late).
            ("el", "v0", "x", 0.50),
            ("el", "v1", "x", 0.40),
            ("el", "v2", "x", 0.30),
            ("el", "v3", "x", 0.10),
        ],
    )
    by = {r["epoch_id"]: r for r in build_meta_loop_ledger(WorkspacePaths(ws))["epochs"]}
    assert by["ee"]["champion_gen"] == "v0"
    assert by["ee"]["champion_index"] == 0, "an early floor-setter → index 0 (left of the band)"
    assert by["el"]["champion_gen"] == "v3"
    assert by["el"]["champion_index"] == 3, "a late floor-setter → index 3 (right of the band)"


def test_ledger_champion_index_null_without_a_floor(tmp_path: Path) -> None:
    """No scored generation → no champion → ``champion_index`` is null.

    With no ``loss_profiles`` rows nothing sets a floor, so both
    ``champion_gen`` and ``champion_index`` are ``None`` (never a guess).
    """
    ws = tmp_path / "ws"
    (ws / "epochs").mkdir(parents=True, exist_ok=True)
    (ws / "current_epoch").write_text("e0\n", encoding="utf-8")
    _make_epoch(
        ws,
        "e0",
        components={
            "board": "b",
            "brief": "r",
            "scoring": "s",
            "entrypoint": "e",
            "mutable_trees": "m",
        },
        structure="gauntlet",
        gens=["v0", "v1"],
        closed=False,
    )
    _build_index(ws / "index.db", [])  # an index with NO loss_profiles rows
    e0 = build_meta_loop_ledger(WorkspacePaths(ws))["epochs"][0]
    assert e0["floor"] is None
    assert e0["champion_gen"] is None
    assert e0["champion_index"] is None


def test_ledger_champion_index_surfaced_on_workspace_view(tmp_path: Path) -> None:
    """``champion_index`` rides the same /api/workspace ledger the UI reads."""
    ws = _chain_workspace(tmp_path)
    view = build_workspace_view(WorkspacePaths(ws))
    by = {r["epoch_id"]: r for r in view["ledger"]}
    assert by["e0"]["champion_index"] == 1
    assert by["e2"]["champion_index"] == 0


def test_ledger_first_epoch_has_all_unchanged_map(tmp_path: Path) -> None:
    ws = _chain_workspace(tmp_path)
    rows = build_meta_loop_ledger(WorkspacePaths(ws))["epochs"]
    e0 = rows[0]
    assert e0["changed_list"] == []
    assert e0["soft"] is False
    assert all(v is False for v in e0["changed_components"].values())
    # the ledger surfaces the supersetted component set incl. proposer + structure.
    assert set(e0["changed_components"]) == {
        "board",
        "brief",
        "scoring",
        "entrypoint",
        "mutable_trees",
        "structure",
        "proposer",
    }


def test_ledger_detects_a_board_change_only(tmp_path: Path) -> None:
    ws = _chain_workspace(tmp_path)
    rows = build_meta_loop_ledger(WorkspacePaths(ws))["epochs"]
    e1 = rows[1]
    assert e1["changed_components"]["board"] is True
    assert e1["changed_list"] == ["board"]
    # structure held (racing→racing), so no SOFT seam.
    assert e1["changed_components"]["structure"] is False
    assert e1["soft"] is False
    # nothing else moved.
    for name in ("brief", "scoring", "entrypoint", "mutable_trees", "proposer"):
        assert e1["changed_components"][name] is False


def test_ledger_detects_proposer_and_structure_roll(tmp_path: Path) -> None:
    ws = _chain_workspace(tmp_path)
    rows = build_meta_loop_ledger(WorkspacePaths(ws))["epochs"]
    e2 = rows[2]
    # the PROPOSER column the contract-diff omits IS detected here (hp0 → hp2).
    assert e2["changed_components"]["proposer"] is True
    # the SCORING re-weight is detected (hs0 → hs2).
    assert e2["changed_components"]["scoring"] is True
    # the STRUCTURE roll (racing → swiss) is detected and marks a SOFT seam.
    assert e2["changed_components"]["structure"] is True
    assert e2["soft"] is True
    # ordered change list follows the surfaced component order (proposer last).
    assert e2["changed_list"] == ["scoring", "structure", "proposer"]
    # the held components did NOT move.
    assert e2["changed_components"]["board"] is False
    assert e2["changed_components"]["brief"] is False


def test_ledger_legacy_missing_proposer_hash_is_not_a_change(tmp_path: Path) -> None:
    """An absent (legacy) proposer hash on one side is "no signal", not changed.

    A pre-feature predecessor with NO proposer hash, followed by an epoch
    that DOES record one, must NOT read as a proposer change (there is no
    comparable baseline) — the same "both hashes present" rule the
    contract-diff uses.
    """
    ws = tmp_path / "ws"
    (ws / "epochs").mkdir(parents=True, exist_ok=True)
    (ws / "current_epoch").write_text("e1\n", encoding="utf-8")
    # e0: legacy — no proposer hash recorded.
    _make_epoch(
        ws,
        "e0",
        components={
            "board": "b",
            "brief": "r",
            "scoring": "s",
            "entrypoint": "e",
            "mutable_trees": "m",
        },
        structure="gauntlet",
        gens=["v0"],
        closed=True,
    )
    # e1: records a proposer hash for the first time.
    _make_epoch(
        ws,
        "e1",
        components={
            "board": "b",
            "brief": "r",
            "scoring": "s",
            "entrypoint": "e",
            "mutable_trees": "m",
            "proposer": "p1",
        },
        structure="gauntlet",
        gens=["v1"],
        closed=False,
    )
    rows = build_meta_loop_ledger(WorkspacePaths(ws))["epochs"]
    e1 = rows[1]
    assert (
        e1["changed_components"]["proposer"] is False
    ), "an absent predecessor hash is not a change"
    assert e1["changed_list"] == []


def test_ledger_surfaced_on_workspace_view(tmp_path: Path) -> None:
    """The ledger rides the SAME /api/workspace read the home view consumes."""
    ws = _chain_workspace(tmp_path)
    view = build_workspace_view(WorkspacePaths(ws))
    assert "ledger" in view
    assert [r["epoch_id"] for r in view["ledger"]] == ["e0", "e1", "e2"]
    # the workspace floor (best_scalar) and the ledger floor agree per epoch.
    ws_floor = {r["epoch_id"]: r["best_scalar"] for r in view["epochs"]}
    led_floor = {r["epoch_id"]: r["floor"] for r in view["ledger"]}
    assert ws_floor == led_floor
    # the proposer + structure roll on e2 is visible through the workspace read.
    e2 = next(r for r in view["ledger"] if r["epoch_id"] == "e2")
    assert e2["changed_components"]["proposer"] is True
    assert e2["soft"] is True


def test_ledger_degrades_without_epochs_dir(tmp_path: Path) -> None:
    ws = tmp_path / "empty"
    ws.mkdir()
    ledger = build_meta_loop_ledger(WorkspacePaths(ws))
    assert ledger["epochs"] == []
    # and the workspace view still surfaces an empty ledger sibling.
    assert build_workspace_view(WorkspacePaths(ws))["ledger"] == []
