"""Board-status surface: the server-side train/holdout split + ladder summary.

Pins the state_reader exposure the dashboard's Board-status panel reads:

* ``compute_board_split`` — the deterministic, seed-stable train/holdout
  selection (tag-held + fraction tail), mirroring the runtime's own
  ``board.split`` so the dashboard names the SAME slices the gate plays.
* ``build_epoch_view`` now carries ``board_split`` (always present, every
  entry ``train`` when no holdout is configured) and ``holdout`` (the
  latest decision's ladder summary, ``None`` until one is recorded) — both
  read DEFENSIVELY so an epoch that predates the overfitting feature, or a
  ``#2``/``#5`` field that has not landed, degrades cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.dashboard.state_reader import (
    WorkspacePaths,
    build_epoch_view,
    compute_board_split,
)

EPOCH = "2026-06-04_e0"


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _board() -> list[dict[str, object]]:
    # The PARSED board payload (``_parse_board``) — ONE spelling on the wire:
    # ``entry_id`` (the raw board.jsonl "id" is an input-format detail).
    return [
        {"entry_id": "b1", "weight": 1.0, "tags": ["adversarial"]},
        {"entry_id": "b2", "weight": 2.0, "tags": ["adversarial", "rare"]},
        {"entry_id": "b3", "weight": 1.0, "tags": []},
        {"entry_id": "b4", "weight": 1.0, "tags": []},
    ]


def _board_jsonl_rows() -> list[dict[str, object]]:
    # The RAW board.jsonl rows (the board input format keys the id as "id").
    return [{"id": e["entry_id"], "weight": e["weight"], "tags": e["tags"]} for e in _board()]


# ---------------------------------------------------------------------------
# compute_board_split
# ---------------------------------------------------------------------------


def test_split_no_config_is_all_train() -> None:
    split = compute_board_split(_board(), None)
    assert split["configured"] is False
    assert split["holdout_count"] == 0
    assert split["train_count"] == 4
    assert all(e["slice"] == "train" for e in split["entries"])


def test_split_disabled_is_all_train_but_keeps_count() -> None:
    cfg = {"enabled": False, "holdout_fraction": 0.5, "holdout_tags": ["rare"], "seed": 0}
    split = compute_board_split(_board(), cfg)
    # configured (a fraction/tags are set) but DISABLED ⇒ nothing held out.
    assert split["enabled"] is False
    assert split["holdout_count"] == 0
    assert split["total"] == 4


def test_split_holds_out_tag_matches() -> None:
    cfg = {"enabled": True, "holdout_fraction": 0.0, "holdout_tags": ["rare"], "seed": 0}
    split = compute_board_split(_board(), cfg)
    held = {e["entry_id"] for e in split["entries"] if e["slice"] == "holdout"}
    assert held == {"b2"}
    b2 = next(e for e in split["entries"] if e["entry_id"] == "b2")
    # the matching tag rides along as why-held-out provenance for the popover.
    assert b2["tag"] == "rare"
    assert b2["weight"] == 2.0


def test_split_fraction_holds_out_a_deterministic_tail() -> None:
    cfg = {"enabled": True, "holdout_fraction": 0.25, "holdout_tags": [], "seed": 7}
    split = compute_board_split(_board(), cfg)
    # 4 entries * 0.25 = 1 held out.
    assert split["holdout_count"] == 1
    assert split["train_count"] == 3
    # deterministic across calls (seed-stable hash, not Python's salted hash()).
    again = compute_board_split(_board(), cfg)
    assert [e["slice"] for e in split["entries"]] == [e["slice"] for e in again["entries"]]


def test_split_fraction_and_tags_combine_to_the_target() -> None:
    cfg = {"enabled": True, "holdout_fraction": 0.5, "holdout_tags": ["rare"], "seed": 1}
    split = compute_board_split(_board(), cfg)
    # 4 * 0.5 = 2 held out total; b2 is tag-held, the fraction tops up by one.
    assert split["holdout_count"] == 2
    held = {e["entry_id"] for e in split["entries"] if e["slice"] == "holdout"}
    assert "b2" in held


def test_split_malformed_fraction_clamps() -> None:
    cfg = {"enabled": True, "holdout_fraction": 9.0, "holdout_tags": [], "seed": 0}
    split = compute_board_split(_board(), cfg)
    # clamped to 1.0 ⇒ everything held out, never a negative / >100% slice.
    assert split["holdout_count"] == 4


def test_split_empty_board() -> None:
    split = compute_board_split(
        [], {"enabled": True, "holdout_fraction": 0.5, "holdout_tags": [], "seed": 0}
    )
    assert split["total"] == 0
    assert split["entries"] == []


# ---------------------------------------------------------------------------
# build_epoch_view — board_split + holdout
# ---------------------------------------------------------------------------


def _epoch_ws(tmp_path: Path, *, scoring: dict, experiments: list[dict] | None = None) -> Path:
    ws = tmp_path / ".zicato"
    (ws / "runtime").mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH, encoding="utf-8")
    edir = ws / "epochs" / EPOCH
    _write_json(edir / "config.json", {"closed": False, "goal": "g"})
    _write_json(edir / "scoring.json", scoring)
    board_lines = "\n".join(json.dumps(e) for e in _board_jsonl_rows())
    (edir / "board.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (edir / "board.jsonl").write_text(board_lines + "\n", encoding="utf-8")
    for exp in experiments or []:
        gid = str(exp["generation_id"])
        _write_json(edir / "generations" / gid / "experiment.json", exp)
    return ws


def test_epoch_view_always_carries_board_split(tmp_path: Path) -> None:
    ws = _epoch_ws(tmp_path, scoring={"weights": {"drift_loss": 1.0}})
    view = build_epoch_view(WorkspacePaths(ws))
    # present even with NO overfitting block — every entry reads as train.
    assert "board_split" in view
    assert view["board_split"]["configured"] is False
    assert view["board_split"]["total"] == 4
    assert view["board_split"]["holdout_count"] == 0


def test_epoch_view_board_split_reads_overfitting_block(tmp_path: Path) -> None:
    ws = _epoch_ws(
        tmp_path,
        scoring={
            "weights": {"drift_loss": 1.0},
            "overfitting": {
                "enabled": True,
                "holdout_fraction": 0.0,
                "holdout_tags": ["rare"],
                "seed": 0,
            },
        },
    )
    view = build_epoch_view(WorkspacePaths(ws))
    bs = view["board_split"]
    assert bs["configured"] is True
    assert bs["holdout_count"] == 1
    held = {e["entry_id"] for e in bs["entries"] if e["slice"] == "holdout"}
    assert held == {"b2"}


def test_epoch_view_holdout_is_none_without_a_decision(tmp_path: Path) -> None:
    ws = _epoch_ws(tmp_path, scoring={"weights": {"drift_loss": 1.0}})
    view = build_epoch_view(WorkspacePaths(ws))
    # no decision recorded a holdout step yet ⇒ the graceful "after a run" null.
    assert view["holdout"] is None


def test_epoch_view_holdout_summary_from_latest_decision(tmp_path: Path) -> None:
    ws = _epoch_ws(
        tmp_path,
        scoring={"weights": {"drift_loss": 1.0}},
        experiments=[
            {"generation_id": "v0"},  # no holdout block
            {
                "generation_id": "v1",
                "holdout": {
                    "confirmed": True,
                    "train_scalar": 0.4,
                    "holdout_scalar": 0.55,
                    "ladder_released": True,
                    "ladder_budget_total": 5,
                    "ladder_budget_remaining": 3,
                    "threshold": 0.1,
                },
            },
        ],
    )
    view = build_epoch_view(WorkspacePaths(ws))
    h = view["holdout"]
    assert h is not None
    assert h["generation_id"] == "v1"
    assert h["confirmed"] is True
    assert h["ladder_budget_remaining"] == 3
    assert h["ladder_budget_total"] == 5
    assert h["threshold"] == 0.1


def test_epoch_view_holdout_summary_defensive_on_garbage(tmp_path: Path) -> None:
    ws = _epoch_ws(
        tmp_path,
        scoring={"weights": {"drift_loss": 1.0}},
        experiments=[
            {
                "generation_id": "v1",
                # a malformed block: wrong types everywhere.
                "holdout": {
                    "confirmed": "yes",
                    "train_scalar": "lots",
                    "ladder_budget_remaining": None,
                    "ladder_budget_total": True,  # bool, NOT an int budget
                },
            },
        ],
    )
    view = build_epoch_view(WorkspacePaths(ws))
    h = view["holdout"]
    assert h is not None
    # every malformed field degrades to None rather than crashing.
    assert h["confirmed"] is None
    assert h["train_scalar"] is None
    assert h["ladder_budget_remaining"] is None
    assert h["ladder_budget_total"] is None
