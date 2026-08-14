"""Champion self-containment: materialise carried-over results (Task 2).

When an epoch carries the champion forward (cross-epoch baseline seed),
the champion's per-board ``loss.json`` files + the ``gen_score.json``
aggregate are MATERIALISED into the new epoch's gen dir, each tagged
``cached: true`` with ``source_epoch`` / ``source_run`` provenance — so
the epoch is self-contained (the champion is no longer a hollow shell) and
the index reads it as scored-but-cached without double-counting it as a
fresh evaluation. A fresh run still writes ``cached: false``.
"""

from __future__ import annotations

from pathlib import Path

from zicato.core.types import LossProfile
from zicato.core.workspace import loss_profile_path
from zicato.evolve.round_baseline import (
    _materialize_carried_champion,
    _source_epoch_generation,
)
from zicato.index.ingest import ingest_run
from zicato.index.query import loss_profiles_for_generation
from zicato.telemetry.reducer import read_loss_profile, write_loss_profile


def _fresh_profile(*, epoch: str, gen: str, entry: str, drift: float) -> LossProfile:
    return LossProfile(
        run_id=f"{gen}--{entry}",
        entry_id=entry,
        generation_id=gen,
        epoch_id=epoch,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=100,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=drift,
        pass_fail=True,
    )


def _write_source_epoch(ws: Path, *, epoch: str, gen: str, entries: dict[str, float]) -> None:
    """Write a champion's per-board loss.json + gen_score.json in a source epoch."""
    import json

    for entry, drift in entries.items():
        write_loss_profile(
            _fresh_profile(epoch=epoch, gen=gen, entry=entry, drift=drift),
            loss_profile_path(ws, epoch, gen, entry),
        )
    gen_dir = ws / "epochs" / epoch / "generations" / gen
    gen_dir.mkdir(parents=True, exist_ok=True)
    (gen_dir / "gen_score.json").write_text(
        json.dumps({"generation_id": gen, "scalar": 0.42}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# _source_epoch_generation — derive (epoch, gen) from the roll-seed path
# ---------------------------------------------------------------------------


def test_source_epoch_generation_from_seed_path(tmp_path: Path) -> None:
    seed = tmp_path / "epochs" / "e1" / "generations" / "v3" / "snapshot"
    assert _source_epoch_generation(seed) == ("e1", "v3")


def test_source_epoch_generation_rejects_unexpected_layout(tmp_path: Path) -> None:
    assert _source_epoch_generation(tmp_path / "somewhere" / "else") is None


# ---------------------------------------------------------------------------
# (c) a carried champion is materialised with cached / source_* provenance
# ---------------------------------------------------------------------------


def test_materialize_carried_champion_writes_cached_losses(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    _write_source_epoch(ws, epoch="e1", gen="v2", entries={"task-a": 2.0, "task-b": 3.0})

    _materialize_carried_champion(
        ws,
        epoch_id="e2",
        generation_id="v0",
        source_epoch="e1",
        source_generation="v2",
    )

    # Both board entries are now materialised into the NEW epoch under v0.
    for entry, drift in {"task-a": 2.0, "task-b": 3.0}.items():
        dst = loss_profile_path(ws, "e2", "v0", entry)
        assert dst.exists(), f"{entry} must be materialised into e2/v0"
        profile = read_loss_profile(dst)
        assert profile.cached is True
        assert profile.source_epoch == "e1"
        assert profile.source_run == f"v2--{entry}", "source_run names the original run"
        assert profile.epoch_id == "e2"
        assert profile.generation_id == "v0"
        assert profile.drift_loss == drift, "the carried scalar is preserved"

    # The aggregate is carried with provenance so a fast first round reuses it.
    import json

    score = json.loads((ws / "epochs" / "e2" / "generations" / "v0" / "gen_score.json").read_text())
    assert score["cached"] is True
    assert score["source_epoch"] == "e1"
    assert score["source_run"] == "v2"
    assert score["generation_id"] == "v0"


def test_materialized_champion_reads_as_cached_in_index(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    db = ws / "index.db"
    _write_source_epoch(ws, epoch="e1", gen="v2", entries={"task-a": 2.0})

    _materialize_carried_champion(
        ws,
        epoch_id="e2",
        generation_id="v0",
        source_epoch="e1",
        source_generation="v2",
    )
    # The materialisation already dual-wrote into the index; re-ingest is
    # idempotent and also covers the no-index-at-materialise-time case.
    ingest_run(ws, db, "e2", "v0", "task-a")

    rows = loss_profiles_for_generation(db, "e2", "v0")
    assert len(rows) == 1
    row = rows[0]
    assert row["cached"] == 1, "the index marks the champion row cached"
    assert row["source_epoch"] == "e1"
    assert row["source_run"] == "v2--task-a"


# ---------------------------------------------------------------------------
# (d) a fresh run still writes cached: false
# ---------------------------------------------------------------------------


def test_fresh_profile_defaults_to_not_cached(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    db = ws / "index.db"
    path = loss_profile_path(ws, "e1", "v1", "task-a")
    write_loss_profile(_fresh_profile(epoch="e1", gen="v1", entry="task-a", drift=1.0), path)

    # On-disk profile is not cached.
    profile = read_loss_profile(path)
    assert profile.cached is False
    assert profile.source_epoch == ""
    assert profile.source_run == ""

    # And the index row reflects fresh (cached 0, NULL sources).
    ingest_run(ws, db, "e1", "v1", "task-a")
    rows = loss_profiles_for_generation(db, "e1", "v1")
    assert len(rows) == 1
    assert rows[0]["cached"] == 0
    assert rows[0]["source_epoch"] is None
    assert rows[0]["source_run"] is None
