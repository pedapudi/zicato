"""Epoch round numbering is CUMULATIVE across evolve invocations.

Re-running ``zicato evolve`` on an existing (un-rolled) epoch must continue the
epoch's round numbering rather than restart the invocation-local loop counter at
0 — otherwise the new field's generations collide with a prior invocation's
rounds in the round-grouped dashboard view (the "v9 lands in Round 0 next to
v1–v4" bug). :func:`zicato.evolve.loop._epoch_round_base` computes the next
round index for the pinned epoch.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.evolve.loop import _epoch_round_base


def _write_gen(ws: Path, epoch: str, gid: str, round_index: int) -> None:
    d = ws / "epochs" / epoch / "generations" / gid
    d.mkdir(parents=True, exist_ok=True)
    (d / "experiment.json").write_text(
        json.dumps({"generation_id": gid, "round_index": round_index})
    )


def test_epoch_round_base_continues_existing_numbering(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    epoch = "2026-06-15_demo"
    # A prior invocation ran round 0 (v1) and round 1 (v2, v3) on this epoch.
    _write_gen(ws, epoch, "v1", 0)
    _write_gen(ws, epoch, "v2", 1)
    _write_gen(ws, epoch, "v3", 1)
    # The next invocation must CONTINUE at round 2 — not restart at 0 (which
    # would stack the new field under the prior invocation's Round 0).
    assert _epoch_round_base(ws, epoch) == 2


def test_epoch_round_base_single_round_epoch(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    epoch = "e"
    _write_gen(ws, epoch, "v1", 0)
    assert _epoch_round_base(ws, epoch) == 1


def test_epoch_round_base_fresh_or_missing_epoch_is_zero(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    # A brand-new / unreadable epoch (no generations dir) → base 0: the first
    # round of a fresh epoch is round 0, the historical behaviour.
    assert _epoch_round_base(ws, "never-created") == 0
    assert _epoch_round_base(ws, None) == 0


def test_epoch_round_base_ignores_non_integer_round_index(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    epoch = "e"
    _write_gen(ws, epoch, "v1", 0)
    # A malformed round_index must not crash or inflate the base.
    d = ws / "epochs" / epoch / "generations" / "v2"
    d.mkdir(parents=True, exist_ok=True)
    (d / "experiment.json").write_text(json.dumps({"round_index": "oops"}))
    assert _epoch_round_base(ws, epoch) == 1
