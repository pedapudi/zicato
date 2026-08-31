"""Tournament-execution hardening: abort-cause + infra-abort cache discipline.

Covers three of the four §1/§2 functionality-recommendation changes:

* ``abort_cause`` is stamped onto a synthesised aborted :class:`LossProfile`,
  survives the ``loss.json`` round-trip, and is ingested into the analytical
  index's ``loss_profiles.abort_cause`` column (so loop-health can later tell
  a parent kill / supervisor kill / crash from a genuine budget exhaustion —
  the "is our own watchdog over-firing?" signal);
* an INFRA-cause abort (parent/supervisor kill, crash) is NOT persisted to the
  unit cache, so a transient blip never poisons a board unit's score for the
  rest of the epoch — re-running re-attempts the unit; a genuine
  wall-clock-budget exhaustion IS cached;
* ``run_tournament`` cache-READS the immutable champion (parent) side by
  default (reusing a prior-round / seed evaluation) while still force-freshing
  the challenger — and ``champion_force_fresh=True`` (``--mode full``)
  re-samples the champion too.

The serde change (change 4) is covered in :mod:`tests.test_subprocess_workers`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import zicato.tournament.runner as runner_mod
from tests._runtime_builders import runtime_config
from zicato.core import (
    BUDGET_ABORT_CAUSE,
    BoardEntry,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
    is_infra_abort_cause,
)
from zicato.core.workspace import loss_profile_path
from zicato.telemetry.reducer import read_loss_profile, write_loss_profile
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament.runner import (
    _aborted_loss_profile,
    _resolve_cached_unit,
    _run_unit_cache_first,
    run_tournament,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _entry(entry_id: str = "entry_a") -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )


def _generation(tmp_path: Path, gen_id: str, parent: str | None) -> Generation:
    return Generation(
        id=gen_id,
        epoch_id="e0",
        parent_id=parent,
        snapshot_root=tmp_path / f"snap_{gen_id}",
        created_at="2024-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Change 1 — abort_cause is stamped + serialised + ingested
# ---------------------------------------------------------------------------


def test_aborted_profile_stamps_abort_cause() -> None:
    """``_aborted_loss_profile`` carries the cause it was handed; ``None`` default."""
    entry = _entry()

    for cause in ("parent_kill", "gone_no_result", "nonzero_exit:1", BUDGET_ABORT_CAUSE):
        profile = _aborted_loss_profile(
            run_id="r",
            entry=entry,
            generation_id="v0",
            epoch_id="e0",
            runtime_ms=0,
            abort_cause=cause,
        )
        assert profile.abort_cause == cause
        assert profile.wall_clock_budget_exceeded is True

    # Back-compat: no cause passed -> field is unset (an ad-hoc caller).
    default = _aborted_loss_profile(
        run_id="r",
        entry=entry,
        generation_id="v0",
        epoch_id="e0",
        runtime_ms=0,
    )
    assert default.abort_cause is None


def test_abort_cause_survives_loss_json_round_trip(tmp_path: Path) -> None:
    """``abort_cause`` round-trips through ``write_loss_profile`` / read; absence
    on a legacy profile loads as ``None`` (readers tolerate it)."""
    profile = make_loss_profile(abort_cause="parent_kill", wall_clock_budget_exceeded=True)
    path = tmp_path / "loss.json"
    write_loss_profile(profile, path)
    assert read_loss_profile(path).abort_cause == "parent_kill"

    # A cleanly-reduced run leaves the field unset and still loads.
    clean = make_loss_profile()
    clean_path = tmp_path / "loss_clean.json"
    write_loss_profile(clean, clean_path)
    assert read_loss_profile(clean_path).abort_cause is None


def test_abort_cause_is_ingested_into_index(tmp_path: Path) -> None:
    """An aborted run's ``abort_cause`` lands in ``loss_profiles.abort_cause`` so
    loop-health can query infra aborts WITHOUT re-parsing the loss_json blob."""
    import sqlite3

    from zicato.epoch.lifecycle import new_epoch
    from zicato.index.ingest import ingest_run

    ws = tmp_path / ".zicato"
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# rubric\n", encoding="utf-8")
    cfg = new_epoch(ws, "alpha", board, rubric, ScoringWeights())
    epoch_id = cfg.id

    # An infra-aborted run (parent kill) — write its loss.json then ingest.
    infra = make_loss_profile(
        run_id="run_infra",
        entry_id="e1",
        generation_id="v0",
        epoch_id=epoch_id,
        wall_clock_budget_exceeded=True,
        abort_cause="parent_kill",
    )
    write_loss_profile(infra, loss_profile_path(ws, epoch_id, "v0", "e1"))
    ingest_run(ws, None, epoch_id, "v0", "e1")

    from zicato.index.ingest import _default_db_path

    db_path = _default_db_path(ws)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT abort_cause FROM loss_profiles WHERE run_id = ?", ("run_infra",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "parent_kill"


# ---------------------------------------------------------------------------
# Change 2 — infra aborts are NOT cached; budget aborts ARE
# ---------------------------------------------------------------------------


def _stub_run_single_returning(monkeypatch: pytest.MonkeyPatch, profile: LossProfile) -> list[str]:
    """Replace ``_run_single`` with one that returns ``profile``; log gen ids."""
    call_log: list[str] = []

    async def fake_run_single(
        *,
        adapter: Any,
        generation: Generation,
        entry: BoardEntry,
        weights: ScoringWeights,
        config: RuntimeConfig,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, workspace_root, epoch_id, side, match_id
        call_log.append(generation.id)
        return profile

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)
    return call_log


def test_infra_abort_is_not_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A parent/supervisor-kill / crash abort must NOT be persisted to the unit
    cache — the next need is a clean MISS so re-running re-attempts the unit."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    gen = _generation(tmp_path, "v0", None)
    entry = _entry()

    infra = make_loss_profile(
        run_id=f"{gen.id}--{entry.id}",
        entry_id=entry.id,
        generation_id=gen.id,
        epoch_id="e0",
        wall_clock_budget_exceeded=True,
        abort_cause="parent_kill",
    )
    assert is_infra_abort_cause(infra.abort_cause) is True
    _stub_run_single_returning(monkeypatch, infra)

    loss = asyncio.run(
        _run_unit_cache_first(
            adapter=object(),
            generation=gen,
            entry=entry,
            weights=ScoringWeights(),
            config=runtime_config(ws),
            workspace_root=ws,
            epoch_id="e0",
            side="parent",
        )
    )
    # The unit ran and returned the infra-abort profile...
    assert loss.abort_cause == "parent_kill"
    # ...but it was NOT persisted: the next cache lookup is a MISS.
    assert (
        _resolve_cached_unit(
            workspace_root=ws,
            epoch_id="e0",
            generation_id=gen.id,
            entry_id=entry.id,
            replicate_index=0,
        )
        is None
    )


def test_budget_abort_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A GENUINE wall-clock-budget exhaustion IS cached — re-running would
    re-hit the same budget, so reusing it saves a wasted run."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    gen = _generation(tmp_path, "v0", None)
    entry = _entry()

    budget = make_loss_profile(
        run_id=f"{gen.id}--{entry.id}",
        entry_id=entry.id,
        generation_id=gen.id,
        epoch_id="e0",
        wall_clock_budget_exceeded=True,
        abort_cause=BUDGET_ABORT_CAUSE,
    )
    assert is_infra_abort_cause(budget.abort_cause) is False
    _stub_run_single_returning(monkeypatch, budget)

    asyncio.run(
        _run_unit_cache_first(
            adapter=object(),
            generation=gen,
            entry=entry,
            weights=ScoringWeights(),
            config=runtime_config(ws),
            workspace_root=ws,
            epoch_id="e0",
            side="parent",
        )
    )
    cached = _resolve_cached_unit(
        workspace_root=ws,
        epoch_id="e0",
        generation_id=gen.id,
        entry_id=entry.id,
        replicate_index=0,
    )
    assert cached is not None
    assert cached.abort_cause == BUDGET_ABORT_CAUSE


def test_clean_run_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A cleanly-reduced run (no abort_cause) is always cache-eligible —
    is_infra_abort_cause(None) is False."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    gen = _generation(tmp_path, "v0", None)
    entry = _entry()

    clean = make_loss_profile(
        run_id=f"{gen.id}--{entry.id}",
        entry_id=entry.id,
        generation_id=gen.id,
        epoch_id="e0",
        drift_loss=1.0,
        pass_fail=True,
    )
    _stub_run_single_returning(monkeypatch, clean)

    asyncio.run(
        _run_unit_cache_first(
            adapter=object(),
            generation=gen,
            entry=entry,
            weights=ScoringWeights(),
            config=runtime_config(ws),
            workspace_root=ws,
            epoch_id="e0",
            side="parent",
        )
    )
    assert (
        _resolve_cached_unit(
            workspace_root=ws,
            epoch_id="e0",
            generation_id=gen.id,
            entry_id=entry.id,
            replicate_index=0,
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Change 3 — run_tournament cache-reads the immutable champion side
# ---------------------------------------------------------------------------


def _seed_champion_cache(ws: Path, parent_gen: Generation, board: list[BoardEntry]) -> None:
    """Persist a per-board champion loss.json so the parent side is a cache HIT."""
    for entry in board:
        profile = make_loss_profile(
            run_id=f"{parent_gen.id}--{entry.id}",
            entry_id=entry.id,
            generation_id=parent_gen.id,
            epoch_id="e0",
            drift_loss=2.0,
            pass_fail=True,
        )
        write_loss_profile(profile, loss_profile_path(ws, "e0", parent_gen.id, entry.id))


def _stub_run_single_logging(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Stub ``_run_single`` to log ``(generation_id, entry_id)`` and return a
    fresh passing profile — so a test can see which side actually RAN."""
    call_log: list[tuple[str, str]] = []

    async def fake_run_single(
        *,
        adapter: Any,
        generation: Generation,
        entry: BoardEntry,
        weights: ScoringWeights,
        config: RuntimeConfig,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, workspace_root, epoch_id, side, match_id
        call_log.append((generation.id, entry.id))
        return make_loss_profile(
            run_id=f"{generation.id}--{entry.id}",
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id="e0",
            drift_loss=1.0,
            pass_fail=True,
        )

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)
    return call_log


def test_run_tournament_cache_reads_champion_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the champion already scored this epoch, ``run_tournament`` reuses the
    champion's per-board cache and runs ONLY the challenger — the §2-item-3 win."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    parent_gen = _generation(tmp_path, "v0", None)
    child_gen = _generation(tmp_path, "v1", "v0")
    board = [_entry("entry_a"), _entry("entry_b")]

    _seed_champion_cache(ws, parent_gen, board)
    call_log = _stub_run_single_logging(monkeypatch)

    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=runtime_config(ws),
            workspace_root=ws,
            epoch_id="e0",
        )
    )

    ran_gens = {gen for gen, _entry_id in call_log}
    # The champion (v0) was cache-read for every entry -> it never RAN.
    assert "v0" not in ran_gens
    # The challenger (v1) was force-fresh -> it ran for every entry.
    assert ran_gens == {"v1"}
    assert {e for g, e in call_log if g == "v1"} == {"entry_a", "entry_b"}
    # The champion aggregate still reflects the cached per-board scalars.
    assert result.parent_agg["drift_loss_mean"] == 2.0


def test_run_tournament_full_mode_resamples_champion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``champion_force_fresh=True`` (``--mode full``) re-samples the champion
    even when its cache exists — the noise-resampling path is preserved."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    parent_gen = _generation(tmp_path, "v0", None)
    child_gen = _generation(tmp_path, "v1", "v0")
    board = [_entry("entry_a"), _entry("entry_b")]

    _seed_champion_cache(ws, parent_gen, board)
    call_log = _stub_run_single_logging(monkeypatch)

    asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=runtime_config(ws),
            workspace_root=ws,
            epoch_id="e0",
            champion_force_fresh=True,
        )
    )

    ran_gens = {gen for gen, _entry_id in call_log}
    # Both sides re-ran despite the champion cache being present.
    assert ran_gens == {"v0", "v1"}
    assert {e for g, e in call_log if g == "v0"} == {"entry_a", "entry_b"}


def test_run_tournament_first_round_still_runs_champion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fresh epoch (no champion cache) is a clean MISS — the champion is
    scored exactly once, so the seeding round is behaviour-unchanged."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    parent_gen = _generation(tmp_path, "v0", None)
    child_gen = _generation(tmp_path, "v1", "v0")
    board = [_entry("entry_a"), _entry("entry_b")]

    # No champion cache seeded -> the parent side MISSes and runs.
    call_log = _stub_run_single_logging(monkeypatch)

    asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=runtime_config(ws),
            workspace_root=ws,
            epoch_id="e0",
        )
    )

    ran_gens = {gen for gen, _entry_id in call_log}
    assert ran_gens == {"v0", "v1"}
    # And the champion now caches for the NEXT round.
    assert (
        _resolve_cached_unit(
            workspace_root=ws,
            epoch_id="e0",
            generation_id="v0",
            entry_id="entry_a",
            replicate_index=0,
        )
        is not None
    )
