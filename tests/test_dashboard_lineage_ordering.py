"""The dashboard ledger orders epochs and generations by their RECORDED
creation timestamp, with the numeric-aware ``eN``/``vN`` id as a tiebreaker
and fallback.

Regression guard for the ledger-ordering bug where a lexical ``sorted()`` on
the directory names produced ``v1, v10, v11, v2`` and ``e0, e1, e10, e11, e2``.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.query import WorkspacePaths, build_lineage_view
from zicato.query.paths import _natural_key, list_epoch_ids


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _epoch(
    ws: Path,
    epoch_id: str,
    created_at: str | None,
    gens: list[tuple[str, str | None]],
) -> None:
    edir = ws / "epochs" / epoch_id
    if created_at is not None:
        _write(edir / "config.json", {"id": epoch_id, "created_at": created_at})
    for gid, proposed_at in gens:
        gdir = edir / "generations" / gid
        if proposed_at is not None:
            _write(
                gdir / "experiment.json",
                {"generation_id": gid, "proposed_at": proposed_at},
            )
        else:
            gdir.mkdir(parents=True, exist_ok=True)


def test_natural_key_orders_numerically() -> None:
    assert sorted(["v0", "v1", "v2", "v10", "v11", "v9"], key=_natural_key) == [
        "v0",
        "v1",
        "v2",
        "v9",
        "v10",
        "v11",
    ]
    assert sorted(["e0", "e1", "e2", "e10", "e11"], key=_natural_key) == [
        "e0",
        "e1",
        "e2",
        "e10",
        "e11",
    ]


def test_epochs_ordered_by_timestamp(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    # Created in id order e0 -> e2 -> e10, with ascending timestamps.
    _epoch(ws, "e0", "2026-01-01T00:00:00Z", [("v0", "2026-01-01T00:00:00Z")])
    _epoch(ws, "e2", "2026-02-01T00:00:00Z", [("v0", "2026-02-01T00:00:00Z")])
    _epoch(ws, "e10", "2026-03-01T00:00:00Z", [("v0", "2026-03-01T00:00:00Z")])
    assert list_epoch_ids(WorkspacePaths(ws)) == ["e0", "e2", "e10"]


def test_timestamp_overrides_id_order(tmp_path: Path) -> None:
    # The recorded timestamp, not the id, decides order: e10 was created
    # BEFORE e2 here, so it must sort first even though 10 > 2.
    ws = tmp_path / ".zicato"
    _epoch(ws, "e10", "2026-01-01T00:00:00Z", [("v0", "2026-01-01T00:00:00Z")])
    _epoch(ws, "e2", "2026-02-01T00:00:00Z", [("v0", "2026-02-01T00:00:00Z")])
    assert list_epoch_ids(WorkspacePaths(ws)) == ["e10", "e2"]


def test_generations_ordered_numerically_within_epoch(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    _epoch(
        ws,
        "e0",
        "2026-01-01T00:00:00Z",
        [
            ("v0", "2026-01-01T00:00:00Z"),
            ("v1", "2026-01-01T00:01:00Z"),
            ("v2", "2026-01-01T00:02:00Z"),
            ("v10", "2026-01-01T00:10:00Z"),
            ("v11", "2026-01-01T00:11:00Z"),
        ],
    )
    view = build_lineage_view(WorkspacePaths(ws))
    gens = [g["generation_id"] for g in view["generations"]]
    assert gens == ["v0", "v1", "v2", "v10", "v11"]


def test_fallback_to_numeric_id_without_timestamps(tmp_path: Path) -> None:
    # No config.json / experiment.json anywhere: the numeric id fallback must
    # still avoid the lexical v1, v10, v11, v2 ordering.
    ws = tmp_path / ".zicato"
    for epoch in ("e0", "e2", "e10"):
        _epoch(ws, epoch, None, [(g, None) for g in ("v0", "v1", "v2", "v10", "v11")])
    assert list_epoch_ids(WorkspacePaths(ws)) == ["e0", "e2", "e10"]
    view = build_lineage_view(WorkspacePaths(ws))
    order = [(g["epoch_id"], g["generation_id"]) for g in view["generations"]]
    assert order == [(e, v) for e in ("e0", "e2", "e10") for v in ("v0", "v1", "v2", "v10", "v11")]
