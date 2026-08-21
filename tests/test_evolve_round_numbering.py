"""Epoch round numbering is CUMULATIVE across evolve invocations.

Re-running ``zicato evolve`` on an existing (un-rolled) epoch must continue the
epoch's round numbering rather than restart the invocation-local loop counter at
0 — otherwise the new field's generations collide with a prior invocation's
rounds in the round-grouped dashboard view (the "v9 lands in Round 0 next to
v1–v4" bug). :func:`zicato.evolve.loop._epoch_round_base` computes the next
round index for the pinned epoch.

The count is over MINTED generations only. The seed is carried — copied from the
registered trees, or from a rolled predecessor's promoted head — and is not a
round that was spent, even though it persists ``round_index: 0``. Every fixture
here therefore gives a challenger the parent a real one has; a parentless record
is a seed.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.evolve.loop import _epoch_round_base


def _write_gen(ws: Path, epoch: str, gid: str, round_index: int, parent: str = "v0") -> None:
    """A MINTED challenger: it carries the parent every real challenger has."""
    d = ws / "epochs" / epoch / "generations" / gid
    d.mkdir(parents=True, exist_ok=True)
    (d / "experiment.json").write_text(
        json.dumps(
            {"generation_id": gid, "round_index": round_index, "parent_generation_id": parent}
        )
    )


def _write_seed(ws: Path, epoch: str, gid: str = "v0") -> None:
    """The synthetic seed marker, shaped like ``write_seed_experiment`` writes it.

    Parentless, and stamped ``round_index: 0`` by the ``Experiment.round_index``
    default — the pair that made the seed look like a spent round.
    """
    d = ws / "epochs" / epoch / "generations" / gid
    d.mkdir(parents=True, exist_ok=True)
    (d / "experiment.json").write_text(
        json.dumps({"generation_id": gid, "round_index": 0, "parent_generation_id": None})
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
    (d / "experiment.json").write_text(
        json.dumps({"parent_generation_id": "v0", "round_index": "oops"})
    )
    assert _epoch_round_base(ws, epoch) == 1


def test_seeded_but_unrun_epoch_starts_at_round_zero(tmp_path: Path) -> None:
    """A seed alone is not a round: the first real field must still be round 0.

    ``write_seed_experiment`` persists ``round_index: 0`` on the parentless seed,
    so counting it returned base 1 and the epoch's first real field was stamped
    1 — which the round timeline then rendered as a phantom round 0 (the seed's
    own bucket, emptied downstream) above the real rounds. Reachable whenever an
    epoch is seeded and mints no field: a pre-flight refusal, a crash before the
    field lands, a budget stop, an operator interrupt.
    """
    ws = tmp_path / ".zicato"
    epoch = "2026-06-15_rolled"
    _write_seed(ws, epoch)
    assert _epoch_round_base(ws, epoch) == 0


def test_seed_does_not_inflate_a_running_epochs_numbering(tmp_path: Path) -> None:
    """The seed is skipped, and the MINTED rounds still decide the base."""
    ws = tmp_path / ".zicato"
    epoch = "2026-06-15_rolled"
    _write_seed(ws, epoch)
    _write_gen(ws, epoch, "v1", 0)
    _write_gen(ws, epoch, "v2", 1)
    # Rounds 0 and 1 were spent, so the next is 2 — the seed adds nothing.
    assert _epoch_round_base(ws, epoch) == 2


def test_seed_skip_preserves_cumulative_numbering(tmp_path: Path) -> None:
    """The standing proof: a re-run still CONTINUES past the highest round.

    Skipping the seed must lower the base only when the seed is the sole
    experiment. An epoch whose challengers all sit in round 4 keeps continuing at
    5, so the "v9 lands in Round 0 next to v1-v4" collision stays fixed.
    """
    ws = tmp_path / ".zicato"
    epoch = "2026-06-15_demo"
    _write_seed(ws, epoch)
    for gid in ("v7", "v8", "v9"):
        _write_gen(ws, epoch, gid, 4, parent="v6")
    assert _epoch_round_base(ws, epoch) == 5
