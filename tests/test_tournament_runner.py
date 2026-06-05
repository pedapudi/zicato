"""Tests for ``zicato.tournament.runner`` generation-level orchestration.

Since the L3 subprocess-isolation refactor, the per-entry run mechanism
(:func:`zicato.tournament.runner._run_single`) spawns a worker
subprocess. The end-to-end subprocess behaviour — args-file shape,
result-file shape, parent-side budget escalation, supervisor-kill
handling — is covered by :mod:`tests.test_subprocess_workers`.

These tests focus on what the runner module still owns *in-process*:
``run_tournament`` / ``run_fast_mode`` iterating the board across
generations, aggregating per-entry :class:`LossProfile` instances, and
running the gate. They stub :func:`_run_single` with a canned-loss
lookup so the orchestration logic is exercised without spawning real
subprocesses.

The two-callable invariant on :class:`RuntimeConfig` is honored by
constructing two distinct callables (identity-unequal) for
``harness_call_llm`` and ``auxiliary_call_llm``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

import zicato.tournament.runner as runner_mod
from zicato.core import (
    BoardEntry,
    DriftCount,
    ExpectationResult,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.core import BoardEntry as _BoardEntry
from zicato.tournament.gate import GateOutcome
from zicato.tournament.runner import (
    TournamentResult,
    _stamp_judge_only,
    run_fast_mode,
    run_tournament,
)


def test_stamp_judge_only_sets_context_key() -> None:
    """``_stamp_judge_only`` stamps ``context['judge_only']='true'`` per entry."""
    from zicato.tournament.runner import _JUDGE_ONLY_CONTEXT_KEY

    entries = [
        _BoardEntry(id="a", kind="single_turn", wall_clock_budget_seconds=5, input="x"),
        _BoardEntry(id="b", kind="single_turn", wall_clock_budget_seconds=5, input="y"),
    ]
    stamped = _stamp_judge_only(entries, True)
    assert all(e.context[_JUDGE_ONLY_CONTEXT_KEY] == "true" for e in stamped)


def test_stamp_judge_only_false_leaves_board_unchanged() -> None:
    """``judge_only=False`` returns the board untouched (byte-identical path)."""
    entries = [
        _BoardEntry(id="a", kind="single_turn", wall_clock_budget_seconds=5, input="x"),
    ]
    result = _stamp_judge_only(entries, False)
    assert result is entries
    assert "judge_only" not in result[0].context


# ---------------------------------------------------------------------------
# Fixtures and stubs
# ---------------------------------------------------------------------------


def _loss(
    *,
    generation_id: str,
    entry_id: str,
    drift_loss: float,
    pass_fail: bool | None,
) -> LossProfile:
    expectation = (
        ExpectationResult(kind="predicate", passed=bool(pass_fail))
        if pass_fail is not None
        else None
    )
    return LossProfile(
        run_id=f"run-{generation_id}-{entry_id}",
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id="e0",
        drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=expectation,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
    )


def _stub_run_single(
    monkeypatch: pytest.MonkeyPatch,
    *,
    canned: dict[tuple[str, str], LossProfile],
) -> list[tuple[str, str]]:
    """Replace ``_run_single`` with a canned-loss lookup.

    Returns the call log — a list of ``(generation_id, entry_id)`` tuples
    in the order ``_run_single`` was invoked, so tests can assert the
    runner iterates the board in order, parent generation first.
    """
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
        del adapter, weights, config, workspace_root, epoch_id, side
        call_log.append((generation.id, entry.id))
        return canned[(generation.id, entry.id)]

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)
    return call_log


def _make_board() -> list[BoardEntry]:
    return [
        BoardEntry(
            id="entry_a",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="hello",
        ),
        BoardEntry(
            id="entry_b",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="world",
        ),
    ]


def _make_runtime_config(tmp_path: Path) -> RuntimeConfig:
    # Two distinct callables (identity-unequal) — the runner re-checks
    # the two-callable invariant as defense in depth.
    async def harness_call(system: str, user: str, model: str) -> str:
        return ""

    async def aux_call(system: str, user: str, model: str) -> str:
        return ""

    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        harness_call_llm=harness_call,
        auxiliary_call_llm=aux_call,
    )


def _make_generation(tmp_path: Path, gen_id: str, parent: str | None) -> Generation:
    return Generation(
        id=gen_id,
        epoch_id="e0",
        parent_id=parent,
        snapshot_root=tmp_path / f"snap_{gen_id}",
        created_at="2024-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# run_tournament — full mode
# ---------------------------------------------------------------------------


def test_run_tournament_iterates_board_for_both_generations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Full mode runs every entry under both generations and aggregates correctly."""
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")

    canned = {
        ("v0", "entry_a"): _loss(
            generation_id="v0", entry_id="entry_a", drift_loss=2.0, pass_fail=True
        ),
        ("v0", "entry_b"): _loss(
            generation_id="v0", entry_id="entry_b", drift_loss=2.0, pass_fail=False
        ),
        ("v1", "entry_a"): _loss(
            generation_id="v1", entry_id="entry_a", drift_loss=1.0, pass_fail=True
        ),
        ("v1", "entry_b"): _loss(
            generation_id="v1", entry_id="entry_b", drift_loss=1.0, pass_fail=True
        ),
    }
    call_log = _stub_run_single(monkeypatch, canned=canned)

    board = _make_board()
    weights = ScoringWeights(promote_margin=0.01)
    config = _make_runtime_config(tmp_path)

    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=weights,
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    # Board-unit scheduling: the runner drove all 4 (generation, entry)
    # combinations — the champion (v0) and challenger (v1) of one entry
    # in one board unit. Completion order across units / sides is not
    # contractual, so assert on the set, not a sequence.
    assert set(call_log) == {
        ("v0", "entry_a"),
        ("v0", "entry_b"),
        ("v1", "entry_a"),
        ("v1", "entry_b"),
    }

    # Result wires generation ids through.
    assert isinstance(result, TournamentResult)
    assert result.parent_generation_id == "v0"
    assert result.child_generation_id == "v1"

    # Per-entry losses contains tuples (parent_loss, child_loss).
    assert set(result.per_entry_losses) == {"entry_a", "entry_b"}
    parent_a, child_a = result.per_entry_losses["entry_a"]
    assert parent_a.drift_loss == 2.0
    assert child_a.drift_loss == 1.0

    # Aggregate dicts carry expected fields.
    assert result.parent_agg["drift_loss_mean"] == 2.0
    assert result.child_agg["drift_loss_mean"] == 1.0
    assert result.parent_agg["pass_rate"] == 0.5
    assert result.child_agg["pass_rate"] == 1.0

    # Gate: child improves both drift and pass-rate → promoted.
    assert result.outcome.decision == "promoted"
    assert result.outcome.delta_scalar < 0
    assert result.outcome.delta_pass_rate == 0.5


def test_run_tournament_rejects_when_child_regresses_pass_rate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child that improves drift but regresses pass-rate must be rejected."""
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")

    canned = {
        ("v0", "entry_a"): _loss(
            generation_id="v0", entry_id="entry_a", drift_loss=5.0, pass_fail=True
        ),
        ("v0", "entry_b"): _loss(
            generation_id="v0", entry_id="entry_b", drift_loss=5.0, pass_fail=True
        ),
        ("v1", "entry_a"): _loss(
            generation_id="v1", entry_id="entry_a", drift_loss=0.0, pass_fail=True
        ),
        ("v1", "entry_b"): _loss(
            generation_id="v1", entry_id="entry_b", drift_loss=0.0, pass_fail=False
        ),
    }
    _stub_run_single(monkeypatch, canned=canned)

    config = _make_runtime_config(tmp_path)
    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=_make_board(),
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    assert result.outcome.decision == "rejected"
    assert "pass-rate regression on entries" in result.outcome.reason
    assert "entry_b" in result.outcome.reason


def test_run_tournament_rejects_two_callable_invariant(tmp_path: Path) -> None:
    """The runner re-checks that harness/auxiliary callables differ."""
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")

    async def shared(system: str, user: str, model: str) -> str:
        return ""

    config = RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        harness_call_llm=shared,
        auxiliary_call_llm=shared,
    )

    with pytest.raises(RuntimeError):
        asyncio.run(
            run_tournament(
                adapter=object(),
                parent_gen=parent_gen,
                child_gen=child_gen,
                board=_make_board(),
                weights=ScoringWeights(),
                config=config,
                workspace_root=tmp_path,
                epoch_id="e0",
            )
        )


def test_run_tournament_stamps_each_entry_on_the_correct_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An 8-row tournament ends with exactly 4 parent + 4 child rows, each
    transitioned on its OWN side.

    The runner threads ``side`` ("parent" / "child") into ``_run_single``
    explicitly per generation. This stub mirrors what the real
    ``_run_single`` does to the live ``ActiveTournament`` grid — a
    ``running`` then ``completed`` transition keyed on
    ``(entry_id, side)`` — so the test proves the runner passes the
    correct side and the per-side rows never collide (the 6/2
    mislabeling bug).
    """
    from zicato.runtime.state import read_active_tournament, update_tournament_entry

    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")

    # 4-entry board -> 8 tournament rows (4 parent + 4 child).
    board = [
        BoardEntry(
            id=f"entry_{n}",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="x",
        )
        for n in range(4)
    ]
    canned = {
        (gen, e.id): _loss(generation_id=gen, entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for gen in ("v0", "v1")
        for e in board
    }

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
        del adapter, weights, config, epoch_id
        # Exactly the transitions the real _run_single performs, keyed on
        # (entry_id, side).
        update_tournament_entry(workspace_root, entry.id, side, status="running")
        update_tournament_entry(workspace_root, entry.id, side, status="completed")
        return canned[(generation.id, entry.id)]

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)

    # Inspect the ActiveTournament grid right before run_tournament clears
    # it: monkeypatch clear_active_tournament to snapshot the final state.
    captured: dict[str, Any] = {}
    real_clear = runner_mod._runtime_state()[0].clear_active_tournament

    def capturing_clear(workspace_root: Path) -> None:
        snap = read_active_tournament(workspace_root)
        if snap is not None:
            captured["tournament"] = snap
        real_clear(workspace_root)

    import zicato.runtime.state as _state_mod

    monkeypatch.setattr(_state_mod, "clear_active_tournament", capturing_clear)

    config = _make_runtime_config(tmp_path)
    asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    tournament = captured["tournament"]
    parents = [e for e in tournament.entries if e.side == "parent"]
    children = [e for e in tournament.entries if e.side == "child"]
    # Exactly 4 parent + 4 child — NOT 6/2.
    assert len(parents) == 4
    assert len(children) == 4
    # Every row reached "completed", and crucially each side carries its
    # OWN four entries (no parent-side update bled onto a child row).
    assert all(e.status == "completed" for e in tournament.entries)
    assert {e.entry_id for e in parents} == {e.id for e in board}
    assert {e.entry_id for e in children} == {e.id for e in board}


# ---------------------------------------------------------------------------
# run_fast_mode
# ---------------------------------------------------------------------------


def test_run_fast_mode_runs_only_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fast mode runs only the child generation and compares to historical agg."""
    child_gen = _make_generation(tmp_path, "v1", "v0")

    canned = {
        ("v1", "entry_a"): _loss(
            generation_id="v1", entry_id="entry_a", drift_loss=0.5, pass_fail=True
        ),
        ("v1", "entry_b"): _loss(
            generation_id="v1", entry_id="entry_b", drift_loss=0.5, pass_fail=True
        ),
    }
    call_log = _stub_run_single(monkeypatch, canned=canned)

    # Historical aggregate the operator saved off some time ago for v0.
    parent_historical = {
        "drift_loss_mean": 2.0,
        "pass_rate": 1.0,
        "expectation_count": 2,
        "entry_count": 2,
        "scalar": 2.0,
        "per_entry": {
            "entry_a": {"drift_loss": 2.0, "pass_fail": True},
            "entry_b": {"drift_loss": 2.0, "pass_fail": True},
        },
        "generation_id": "v0",
    }

    config = _make_runtime_config(tmp_path)
    result = asyncio.run(
        run_fast_mode(
            adapter=object(),
            child_gen=child_gen,
            board=_make_board(),
            weights=ScoringWeights(promote_margin=0.01),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
            parent_historical_agg=parent_historical,
        )
    )

    # Only the child was run.
    assert set(call_log) == {("v1", "entry_a"), ("v1", "entry_b")}

    # Parent agg passes through unchanged; child agg fresh from this run.
    assert result.parent_agg is parent_historical
    assert result.child_agg["scalar"] == 0.5
    assert result.parent_generation_id == "v0"
    assert result.child_generation_id == "v1"
    # Per-entry tuples are not produced in fast mode.
    assert result.per_entry_losses == {}
    # Big win for the child here.
    assert result.outcome.decision == "promoted"


def test_run_fast_mode_never_runs_the_champion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fast-mode board unit runs ONLY the challenger — no champion run.

    The champion's cached ``gen_score.json`` aggregate is reused, so the
    champion (parent) generation must never be handed to ``_run_single``.
    """
    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _board_of(4)
    canned = {
        ("v1", e.id): _loss(generation_id="v1", entry_id=e.id, drift_loss=0.5, pass_fail=True)
        for e in board
    }
    call_log = _stub_run_single(monkeypatch, canned=canned)

    parent_historical = {
        "drift_loss_mean": 2.0,
        "pass_rate": 1.0,
        "expectation_count": len(board),
        "entry_count": len(board),
        "scalar": 2.0,
        "per_entry": {e.id: {"drift_loss": 2.0, "pass_fail": True} for e in board},
        "generation_id": "v0",
    }

    config = _make_runtime_config(tmp_path)
    asyncio.run(
        run_fast_mode(
            adapter=object(),
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
            parent_historical_agg=parent_historical,
        )
    )

    # Every dispatched run was a challenger run — the champion (v0) was
    # never executed (its cached aggregate stood in for it).
    assert all(gen == "v1" for gen, _ in call_log)
    assert {entry for _, entry in call_log} == {e.id for e in board}
    assert len(call_log) == len(board)


def test_no_cache_first_round_runs_champion_via_full_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The no-cache-first-round fallback runs the champion (full path).

    Fast mode reuses the champion's cached ``gen_score.json``; a fresh
    epoch's first round has no cache, so the orchestrator degrades to
    :func:`run_tournament` (the full path). This pins the runner-side
    contract the fallback depends on: the full path DOES execute the
    champion (parent) run for every entry — exactly what seeds the cache
    that later fast rounds reuse — whereas fast mode never would.
    """
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _board_of(3)
    canned = {
        (gen, e.id): _loss(generation_id=gen, entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for gen in ("v0", "v1")
        for e in board
    }
    call_log = _stub_run_single(monkeypatch, canned=canned)

    config = _make_runtime_config(tmp_path)
    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    # The champion (v0) was run for every board entry — the full path's
    # parent_agg is what _cache_gen_score persists for fast rounds.
    champion_runs = {entry for gen, entry in call_log if gen == "v0"}
    assert champion_runs == {e.id for e in board}
    # And the result carries a real per-entry champion aggregate.
    assert set(result.per_entry_losses) == {e.id for e in board}
    assert result.parent_agg["entry_count"] == 3


def test_run_fast_mode_respects_parallelism_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fast mode keeps at most ``parallelism`` board units (challenger runs) in flight.

    A fast-mode board unit runs ONLY the challenger, so the knob caps
    the number of ``_run_single`` runs at exactly ``parallelism`` — half
    the full-mode ceiling.
    """
    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _board_of(6)
    canned = {
        ("v1", e.id): _loss(generation_id="v1", entry_id=e.id, drift_loss=0.5, pass_fail=True)
        for e in board
    }
    stub = _ConcurrencyStub(canned)
    monkeypatch.setattr(runner_mod, "_run_single", stub.run_single)

    parent_historical = {
        "drift_loss_mean": 2.0,
        "pass_rate": 1.0,
        "expectation_count": len(board),
        "entry_count": len(board),
        "scalar": 2.0,
        "per_entry": {e.id: {"drift_loss": 2.0, "pass_fail": True} for e in board},
        "generation_id": "v0",
    }

    config = dataclasses.replace(_make_runtime_config(tmp_path), parallelism=3)
    result = asyncio.run(
        run_fast_mode(
            adapter=object(),
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
            parent_historical_agg=parent_historical,
        )
    )

    # 3 board units, one challenger run each -> at most 3 runs in flight.
    assert stub.peak == 3
    # All 6 challenger runs executed (no champion runs).
    assert len(stub.call_log) == 6
    assert all(gen == "v1" for gen, _ in stub.call_log)
    assert result.child_agg["entry_count"] == 6


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_tournament_result_is_json_serializable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``dataclasses.asdict`` + ``json.dumps(default=str)`` round-trips."""
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    canned = {
        ("v0", "entry_a"): _loss(
            generation_id="v0", entry_id="entry_a", drift_loss=1.0, pass_fail=True
        ),
        ("v0", "entry_b"): _loss(
            generation_id="v0", entry_id="entry_b", drift_loss=1.0, pass_fail=True
        ),
        ("v1", "entry_a"): _loss(
            generation_id="v1", entry_id="entry_a", drift_loss=0.5, pass_fail=True
        ),
        ("v1", "entry_b"): _loss(
            generation_id="v1", entry_id="entry_b", drift_loss=0.5, pass_fail=True
        ),
    }
    _stub_run_single(monkeypatch, canned=canned)
    config = _make_runtime_config(tmp_path)

    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=_make_board(),
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    payload = dataclasses.asdict(result)
    encoded = json.dumps(payload, default=str)
    decoded: dict[str, Any] = json.loads(encoded)

    assert decoded["parent_generation_id"] == "v0"
    assert decoded["child_generation_id"] == "v1"
    assert decoded["outcome"]["decision"] == "promoted"
    # Per-entry losses round-tripped through asdict — paths string-ified.
    assert "entry_a" in decoded["per_entry_losses"]


# ---------------------------------------------------------------------------
# Bounded-concurrency board execution
# ---------------------------------------------------------------------------


def _board_of(n: int) -> list[BoardEntry]:
    """A board of ``n`` distinct single-turn entries."""
    return [
        BoardEntry(
            id=f"entry_{i}",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input=f"in-{i}",
        )
        for i in range(n)
    ]


class _ConcurrencyStub:
    """Instrumented ``_run_single`` replacement that tracks in-flight runs.

    Each fake run yields control to the event loop a few times via
    ``asyncio.sleep(0)`` so that, when the runner schedules more than one
    at a time, they genuinely overlap. ``peak`` records the highest
    observed number of simultaneously-in-flight runs.
    """

    def __init__(self, canned: dict[tuple[str, str], LossProfile]) -> None:
        self._canned = canned
        self.in_flight = 0
        self.peak = 0
        self.call_log: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str]] = []

    async def run_single(
        self,
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
        del adapter, weights, config, workspace_root, epoch_id, side
        self.call_log.append((generation.id, entry.id))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            # A handful of yields so overlapping runs actually interleave.
            for _ in range(5):
                await asyncio.sleep(0)
            return self._canned[(generation.id, entry.id)]
        finally:
            self.in_flight -= 1
            self.completed.append((generation.id, entry.id))


def test_run_generation_respects_parallelism_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 6-entry board with ``parallelism=3`` keeps 3 board units in flight.

    The knob counts BOARD UNITS, not subprocesses. In full mode a board
    unit runs its champion + challenger runs concurrently, so 3 board
    units mean up to ``2 * 3 == 6`` ``_run_single`` runs alive at once.
    """
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _board_of(6)
    canned = {
        (gen, e.id): _loss(generation_id=gen, entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for gen in ("v0", "v1")
        for e in board
    }
    stub = _ConcurrencyStub(canned)
    monkeypatch.setattr(runner_mod, "_run_single", stub.run_single)

    config = dataclasses.replace(_make_runtime_config(tmp_path), parallelism=3)
    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    # 3 board units, each running champion + challenger concurrently ->
    # at most 6 _run_single runs in flight at once, and that ceiling is
    # actually reached (proving the runner parallelises board units).
    assert stub.peak == 6
    # All 12 runs (6 entries x 2 generations) executed.
    assert len(stub.call_log) == 12
    # The result mapping is complete and order-independent.
    assert set(result.per_entry_losses) == {e.id for e in board}


def test_run_tournament_runs_champion_and_challenger_concurrently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A full-mode board unit runs its champion + challenger simultaneously.

    With ``parallelism=1`` exactly ONE board unit is admitted at a time,
    yet that unit must still have BOTH its champion (parent) and
    challenger (child) runs in flight at once — a board unit is the
    parent run AND the child run running together.
    """
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _board_of(3)
    canned = {
        (gen, e.id): _loss(generation_id=gen, entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for gen in ("v0", "v1")
        for e in board
    }

    # Track, per board entry, the widest set of sides seen in flight
    # together at any single observation point.
    class _SideOverlapStub:
        def __init__(self) -> None:
            self.in_flight: dict[str, set[str]] = {}
            self.widest: dict[str, set[str]] = {}

        async def run_single(
            self,
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
            del adapter, weights, config, workspace_root, epoch_id
            live = self.in_flight.setdefault(entry.id, set())
            live.add(generation.id)
            try:
                for _ in range(5):
                    # On every yield, snapshot how many sides of THIS
                    # entry are concurrently in flight.
                    seen = self.widest.setdefault(entry.id, set())
                    if len(live) > len(seen):
                        self.widest[entry.id] = set(live)
                    await asyncio.sleep(0)
                return canned[(generation.id, entry.id)]
            finally:
                live.discard(generation.id)

    stub = _SideOverlapStub()
    monkeypatch.setattr(runner_mod, "_run_single", stub.run_single)

    config = dataclasses.replace(_make_runtime_config(tmp_path), parallelism=1)
    asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    # For every board entry, the champion (v0) and the challenger (v1)
    # were both in flight at the same moment — they ran concurrently
    # within the one board unit even though parallelism=1.
    assert stub.widest, "no runs were observed"
    assert set(stub.widest) == {e.id for e in board}
    assert all(sides == {"v0", "v1"} for sides in stub.widest.values())


def test_run_generation_result_matches_sequential_under_parallelism(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``parallelism=3`` yields the SAME entry.id -> LossProfile as ``parallelism=1``."""
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _board_of(6)
    # Distinct drift_loss per (gen, entry) so a mis-mapped result is caught.
    canned = {
        (gen, f"entry_{i}"): _loss(
            generation_id=gen,
            entry_id=f"entry_{i}",
            drift_loss=float(i) + (0.0 if gen == "v0" else 100.0),
            pass_fail=True,
        )
        for gen in ("v0", "v1")
        for i in range(6)
    }

    def _run(parallelism: int) -> dict[str, tuple[float, float]]:
        stub = _ConcurrencyStub(canned)
        monkeypatch.setattr(runner_mod, "_run_single", stub.run_single)
        config = dataclasses.replace(_make_runtime_config(tmp_path), parallelism=parallelism)
        result = asyncio.run(
            run_tournament(
                adapter=object(),
                parent_gen=parent_gen,
                child_gen=child_gen,
                board=board,
                weights=ScoringWeights(),
                config=config,
                workspace_root=tmp_path,
                epoch_id="e0",
            )
        )
        return {
            eid: (parent.drift_loss, child.drift_loss)
            for eid, (parent, child) in result.per_entry_losses.items()
        }

    sequential = _run(1)
    parallel = _run(3)
    assert parallel == sequential
    # And the values are the canned ones, correctly keyed per side.
    assert sequential["entry_2"] == (2.0, 102.0)


def test_run_generation_parallelism_one_runs_one_board_unit_at_a_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``parallelism=1`` admits one board unit at a time, in board order.

    A board unit is a board entry's champion + challenger pair. With
    ``parallelism=1`` exactly one unit runs at a time — so at most 2
    ``_run_single`` runs are in flight (the two sides of the one
    admitted entry), and entry *k*'s pair fully settles before entry
    *k+1*'s pair starts. The two sides of a single entry still run
    concurrently within the unit.
    """
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _board_of(5)
    canned = {
        (gen, e.id): _loss(generation_id=gen, entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for gen in ("v0", "v1")
        for e in board
    }
    stub = _ConcurrencyStub(canned)
    monkeypatch.setattr(runner_mod, "_run_single", stub.run_single)

    config = dataclasses.replace(_make_runtime_config(tmp_path), parallelism=1)
    asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    # At most 2 runs in flight at once — one board unit's champion +
    # challenger pair — never a third (the next unit is gated).
    assert stub.peak == 2
    # Board units start in board order; each unit's two runs are the
    # champion + challenger of the SAME entry, and entry k's unit fully
    # settles before entry k+1's unit starts. The call log is therefore
    # entry-grouped, even though the two sides within a unit may start
    # in either order.
    grouped = [set(stub.call_log[i : i + 2]) for i in range(0, len(stub.call_log), 2)]
    expected = [{("v0", e.id), ("v1", e.id)} for e in board]
    assert grouped == expected
    # Each board unit also fully COMPLETED before the next one started.
    grouped_done = [set(stub.completed[i : i + 2]) for i in range(0, len(stub.completed), 2)]
    assert grouped_done == expected


def test_run_generation_surfaces_failure_under_concurrency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing run under parallelism>1 raises, and siblings still finish.

    The exception must propagate (not be swallowed by ``gather``), and
    every sibling run that was already in flight must run to completion
    — its ``finally`` cleanup must not be skipped by a cancellation.
    """
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _board_of(4)

    started: list[str] = []
    finished: list[str] = []

    async def failing_run_single(
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
        del adapter, weights, config, workspace_root, epoch_id, side
        started.append(entry.id)
        try:
            for _ in range(3):
                await asyncio.sleep(0)
            if entry.id == "entry_1":
                raise RuntimeError("worker blew up")
            return _loss(
                generation_id=generation.id,
                entry_id=entry.id,
                drift_loss=1.0,
                pass_fail=True,
            )
        finally:
            # Mirrors _run_single's own finally cleanup block.
            finished.append(entry.id)

    monkeypatch.setattr(runner_mod, "_run_single", failing_run_single)

    config = dataclasses.replace(_make_runtime_config(tmp_path), parallelism=4)
    with pytest.raises(RuntimeError, match="worker blew up"):
        asyncio.run(
            run_tournament(
                adapter=object(),
                parent_gen=parent_gen,
                child_gen=child_gen,
                board=board,
                weights=ScoringWeights(),
                config=config,
                workspace_root=tmp_path,
                epoch_id="e0",
            )
        )

    # Every parent-side run that started also reached its finally block —
    # no in-flight sibling was cancelled mid-run / had cleanup skipped.
    assert set(started) == {e.id for e in board}
    assert set(finished) == set(started)


# ---------------------------------------------------------------------------
# Board-level disable_drift threading — runner -> adapter -> assemble_judges
# ---------------------------------------------------------------------------
#
# These cover the previously-missing middle of the judges integration: a
# board author sets ``disable_drift`` in the board's ``board_meta``
# header, and that suppression set must reach the per-entry adapter call.
# The runner stamps it onto each entry's ``context`` (the only per-entry
# channel that survives the subprocess-worker round-trip); the adapter
# reads it back and assemble_judges drops the matching built-in judge.


def _capture_run_single(monkeypatch: pytest.MonkeyPatch) -> list[BoardEntry]:
    """Replace ``_run_single`` with a capture of the entry it is handed.

    Returns a list that accumulates, in call order, every
    :class:`BoardEntry` the runner dispatched — i.e. the entries AFTER
    the runner's board-level ``disable_drift`` stamping. Each call still
    returns a benign passing :class:`LossProfile` so the tournament
    completes normally.
    """
    seen: list[BoardEntry] = []

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
        del adapter, weights, config, workspace_root, epoch_id, side
        seen.append(entry)
        return _loss(
            generation_id=generation.id,
            entry_id=entry.id,
            drift_loss=1.0,
            pass_fail=True,
        )

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)
    return seen


def test_runner_stamps_board_disable_drift_onto_entry_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run_tournament`` threads board-level ``disable_drift`` onto every entry.

    The runner is handed a plain board plus a board-level ``disable_drift``
    tuple; it must stamp the suppression set onto each dispatched entry's
    ``context['disable_drift']`` so the (subprocess) adapter can read it.
    A frozen :class:`BoardEntry` is rebuilt, not mutated — and the
    caller's original board entries are left untouched.
    """
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _make_board()
    seen = _capture_run_single(monkeypatch)
    config = _make_runtime_config(tmp_path)

    asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
            disable_drift=("tool_error", "agent_refusal"),
        )
    )

    # Every dispatched entry (4 = 2 entries x 2 generations) carries the
    # board-level suppression set on its context, as a space-joined list.
    assert len(seen) == 4
    for entry in seen:
        assert entry.context["disable_drift"] == "tool_error agent_refusal"
    # The caller's original board entries were NOT mutated in place.
    assert all("disable_drift" not in e.context for e in board)


def test_runner_empty_disable_drift_leaves_entries_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty board-level ``disable_drift`` leaves each entry's context alone.

    A board with no ``board_meta`` header yields an empty ``disable_drift``
    tuple; the runner must then leave the entries exactly as supplied, so
    a board author's own per-entry ``context`` is never clobbered.
    """
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    # An entry that already carries an unrelated context key.
    board = [
        BoardEntry(
            id="entry_ctx",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="hello",
            context={"attachments": "doc.pdf"},
        )
    ]
    seen = _capture_run_single(monkeypatch)
    config = _make_runtime_config(tmp_path)

    asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
            # disable_drift defaults to ().
        )
    )

    for entry in seen:
        assert "disable_drift" not in entry.context
        assert entry.context == {"attachments": "doc.pdf"}


def test_board_disable_drift_excludes_suppressed_builtin_judge_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: a board's ``board_meta`` ``disable_drift`` drops a built-in judge.

    This is the integration test that was missing. It exercises the FULL
    chain a board author relies on:

    1. a real JSONL board file with a leading ``{"board_meta": true,
       "disable_drift": [...]}`` header, parsed by
       :func:`zicato.board.jsonl.load_board_with_meta`;
    2. :func:`run_tournament` threading that board-level ``disable_drift``
       onto each dispatched entry's ``context``;
    3. the runner's subprocess-worker entry (de)serialisation round-trip
       (:func:`zicato.tournament.runner._entry_to_dict` ->
       :func:`zicato.core.validate_board_entry`) — proving the
       suppression set survives the OS-process boundary;
    4. the adapter's read side
       (:func:`zicato.adapters.adk._entry_disable_drift`); and
    5. :func:`zicato.judge_runtime.assemble_judges` actually producing a
       goldfive judge list with the suppressed built-in REMOVED.

    Needs goldfive for the real built-in judge set; skipped without it.
    """
    goldfive = pytest.importorskip("goldfive")
    from zicato.adapters.adk import _entry_disable_drift
    from zicato.board.jsonl import load_board_with_meta
    from zicato.core import validate_board_entry
    from zicato.judge_runtime import assemble_judges

    # --- 1. A real board file: board_meta header suppresses tool_error. ---
    board_path = tmp_path / "board.jsonl"
    board_path.write_text(
        json.dumps({"board_meta": True, "disable_drift": ["tool_error"]})
        + "\n"
        + json.dumps(
            {
                "id": "entry_e2e",
                "kind": "single_turn",
                "budget_s": 60,
                "input": "hello",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    entries, disable_drift, _judge_only = load_board_with_meta(board_path)
    assert len(entries) == 1
    # The header parsed into a board-level DriftKind tuple.
    assert tuple(str(k) for k in disable_drift) == ("tool_error",)

    # --- 2. Run the board through run_tournament, capturing entries. ---
    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    seen = _capture_run_single(monkeypatch)
    config = _make_runtime_config(tmp_path)
    asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=entries,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
            disable_drift=disable_drift,
        )
    )
    assert seen, "runner dispatched no entries"
    dispatched = seen[0]

    # --- 3. Subprocess-worker round-trip: the runner serialises the
    # entry to the worker args file and the worker re-parses it. The
    # suppression set rides on ``context``, which survives intact. ---
    reparsed = validate_board_entry(runner_mod._entry_to_dict(dispatched))
    assert reparsed.context.get("disable_drift") == "tool_error"

    # --- 4 + 5. The adapter reads disable_drift off the re-parsed entry
    # and assemble_judges produces a judge list with tool_error dropped. ---
    suppressed = _entry_disable_drift(reparsed)
    assert suppressed == ("tool_error",)

    async def _aux(system: str, user: str, model: str) -> str:
        return ""

    judges = assemble_judges(
        entry_judges=None,
        disable_drift=suppressed,
        aux_call_llm=_aux,
    )
    names = {j.name for j in judges}
    full = {j.name for j in goldfive.builtin_judges.default_judges()}
    assert "tool_error" in full, "baseline: tool_error is normally a built-in"
    # THE assertion: the suppressed built-in is gone from the run's list.
    assert "tool_error" not in names
    # Every other built-in stays default-on.
    assert names == full - {"tool_error"}


# ---------------------------------------------------------------------------
# Incremental per-board scoring — a board's score is available ASAP
# ---------------------------------------------------------------------------
#
# A board unit's run(s) are reduced/scored the instant that unit settles
# — concurrently with the sibling board units still running — and the
# running partial aggregate is persisted onto the live ActiveTournament
# so a reader (the dashboard) sees the scalar climb as the tournament
# runs, rather than 0.00 until the round ends.


def test_partial_aggregate_is_written_as_each_board_unit_completes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Full mode persists a running partial aggregate per finished board unit.

    A 4-entry board scored at ``parallelism=1`` (one board unit at a
    time) must leave the active-tournament record carrying a
    ``partial_*_agg`` whose ``entry_count`` grows by exactly one per
    settled board unit — proving the runner scores each board the
    instant its runs finish, not in a batch at round end.
    """
    import zicato.runtime.state as state_mod
    from zicato.runtime.state import read_active_tournament

    _real_update = state_mod.update_tournament_partial_aggregate

    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _board_of(4)
    canned = {
        (gen, e.id): _loss(generation_id=gen, entry_id=e.id, drift_loss=2.0, pass_fail=True)
        for gen in ("v0", "v1")
        for e in board
    }
    _stub_run_single(monkeypatch, canned=canned)

    # Snapshot the running partial aggregate after every persist.
    counts: list[tuple[int, int]] = []

    def _capturing_update(workspace_root: Path, **kw: Any) -> None:
        _real_update(workspace_root, **kw)
        t = read_active_tournament(workspace_root)
        assert t is not None
        counts.append(
            (
                int(t.partial_champion_agg.get("entry_count", 0)),
                int(t.partial_challenger_agg.get("entry_count", 0)),
            )
        )

    monkeypatch.setattr(state_mod, "update_tournament_partial_aggregate", _capturing_update)

    config = dataclasses.replace(_make_runtime_config(tmp_path), parallelism=1)
    result = asyncio.run(
        run_tournament(
            adapter=object(),
            parent_gen=parent_gen,
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
        )
    )

    # One persist per board unit: the entry counts climb 1,2,3,4 on each
    # side as each board unit settles — incremental, not a single
    # round-end batch.
    assert counts == [(1, 1), (2, 2), (3, 3), (4, 4)]
    # The last partial aggregate equals the full TournamentResult
    # aggregate — incremental scoring converges on the same number the
    # gate decides on.
    assert result.child_agg["entry_count"] == 4


def test_partial_aggregate_is_visible_before_all_boards_finish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A finished board's score is readable while sibling boards still run.

    With ``parallelism=4`` all four board units run concurrently, but
    they are released to settle one at a time via a controlled gate. As
    EACH board unit settles, the runner must persist its score — so a
    reader observes a non-empty partial aggregate while the remaining
    boards are still in flight, not only after the last one finishes.
    """
    from zicato.runtime.state import read_active_tournament

    parent_gen = _make_generation(tmp_path, "v0", None)
    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _board_of(4)
    canned = {
        (gen, e.id): _loss(generation_id=gen, entry_id=e.id, drift_loss=1.0, pass_fail=True)
        for gen in ("v0", "v1")
        for e in board
    }

    # Per-entry gates: a board unit's two runs may only finish once the
    # entry's gate is set. The test releases entries in board order and
    # snapshots the partial aggregate between releases.
    gates: dict[str, asyncio.Event] = {e.id: asyncio.Event() for e in board}

    async def gated_run_single(
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
        del adapter, weights, config, workspace_root, epoch_id, side
        await gates[entry.id].wait()
        return canned[(generation.id, entry.id)]

    monkeypatch.setattr(runner_mod, "_run_single", gated_run_single)

    observed: list[int] = []

    async def _driver() -> TournamentResult:
        config = dataclasses.replace(_make_runtime_config(tmp_path), parallelism=4)
        task = asyncio.ensure_future(
            run_tournament(
                adapter=object(),
                parent_gen=parent_gen,
                child_gen=child_gen,
                board=board,
                weights=ScoringWeights(),
                config=config,
                workspace_root=tmp_path,
                epoch_id="e0",
            )
        )
        # Let the tournament reach the point where all four board units
        # are parked on their gates.
        for _ in range(10):
            await asyncio.sleep(0)
        # Release the boards one at a time; between releases the partial
        # aggregate must reflect exactly the boards settled so far.
        for e in board[:-1]:
            gates[e.id].set()
            for _ in range(20):
                await asyncio.sleep(0)
            t = read_active_tournament(tmp_path)
            assert t is not None
            observed.append(int(t.partial_challenger_agg.get("entry_count", 0)))
            assert not task.done(), "tournament finished before the last board released"
        # Release the final board so the tournament can complete.
        gates[board[-1].id].set()
        return await task

    result = asyncio.run(_driver())

    # The partial aggregate was visible mid-tournament: after releasing
    # 1, 2, 3 boards (with one still in flight) the running child-side
    # entry_count read 1, 2, 3 — a finished board's score was available
    # ASAP, not deferred to round end.
    assert observed == [1, 2, 3]
    assert result.child_agg["entry_count"] == 4


def test_fast_mode_persists_running_partial_aggregate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fast mode scores each challenger-only board unit as it settles.

    Fast mode publishes its OWN ActiveTournament before running (so the
    dashboard's Tournament hall renders the live board entries instead
    of staying blank). Each challenger-only board unit's loss folds into
    the running partial-challenger aggregate via _IncrementalScorer; the
    partial-champion aggregate is pre-seeded with the cached
    parent_historical_agg so the dashboard's running partial table is
    meaningful from the first frame. The tournament is cleared on
    ``finally`` exit — capture it right before the clear runs.
    """
    from zicato.runtime.state import read_active_tournament

    child_gen = _make_generation(tmp_path, "v1", "v0")
    board = _board_of(3)
    canned = {
        ("v1", e.id): _loss(generation_id="v1", entry_id=e.id, drift_loss=1.5, pass_fail=True)
        for e in board
    }
    _stub_run_single(monkeypatch, canned=canned)

    parent_historical = {
        "drift_loss_mean": 3.0,
        "pass_rate": 1.0,
        "scalar": 3.0,
        "entry_count": 3,
        "expectation_count": 3,
        "per_entry": {e.id: {"drift_loss": 3.0, "pass_fail": True} for e in board},
        "namespace_aggregates": {},
        "scalar_components": {},
        "generation_id": "v0",
    }

    # The runner clears the active tournament on ``finally`` exit;
    # monkeypatch clear_active_tournament to snapshot the final state.
    captured: dict[str, Any] = {}
    import zicato.runtime.state as _state_mod

    real_clear = _state_mod.clear_active_tournament

    def capturing_clear(workspace_root: Path) -> None:
        snap = read_active_tournament(workspace_root)
        if snap is not None:
            captured["tournament"] = snap
        real_clear(workspace_root)

    monkeypatch.setattr(_state_mod, "clear_active_tournament", capturing_clear)

    config = _make_runtime_config(tmp_path)
    result = asyncio.run(
        run_fast_mode(
            adapter=object(),
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
            parent_historical_agg=parent_historical,
        )
    )

    t = captured["tournament"]
    # The running partial aggregate accumulated the challenger side; the
    # champion side was pre-seeded from the cached historical aggregate.
    assert t.partial_challenger_agg.get("entry_count") == 3
    assert t.partial_champion_agg.get("entry_count") == 3
    assert t.partial_champion_agg.get("scalar") == 3.0
    # The persisted partial challenger aggregate converges on the final
    # fast-mode aggregate.
    assert t.partial_challenger_agg.get("scalar") == result.child_agg["scalar"]


# ---------------------------------------------------------------------------
# Fast-mode ActiveTournament publication (regression: a fast round must
# not leave the dashboard's Tournament hall blank).
# ---------------------------------------------------------------------------


def test_run_fast_mode_publishes_active_tournament(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fast mode publishes an ActiveTournament for the dashboard.

    Regression: ``_run_board_units_fast`` did not seed
    :class:`~zicato.runtime.state.ActiveTournament`, so the dashboard's
    Tournament hall stayed blank during a fast round and the gauntlet
    mislabelled the actively-running challenger as ``INCOMPLETE``. The
    runner now mirrors :func:`run_tournament`: it writes the record
    before the first run and clears it on ``finally`` exit. The
    challenger side is queued at start (the existing ``_run_single``
    transitions drive it to ``running``/``completed``); the champion
    side is stamped ``cached`` with the per-entry scalar already known
    from ``parent_historical_agg["per_entry"]``.
    """
    from zicato.runtime.state import read_active_tournament, update_tournament_entry

    child_gen = _make_generation(tmp_path, "v2", "v0")
    board = _board_of(2)
    canned = {
        ("v2", e.id): _loss(generation_id="v2", entry_id=e.id, drift_loss=0.4, pass_fail=True)
        for e in board
    }

    # Spy on ``_run_single``: record the ActiveTournament shape just
    # after the challenger has gone "running" — i.e. mid-round, with at
    # least one challenger entry actively running. This is the exact
    # shape the dashboard's /api/environment would observe live.
    midflight: dict[str, Any] = {}

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
        del adapter, weights, config, epoch_id
        # Mirror the real ``_run_single``'s state writes.
        update_tournament_entry(workspace_root, entry.id, side, status="running")
        if "snapshot" not in midflight:
            snap = read_active_tournament(workspace_root)
            if snap is not None:
                midflight["snapshot"] = snap
        update_tournament_entry(workspace_root, entry.id, side, status="completed")
        return canned[(generation.id, entry.id)]

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)

    parent_historical = {
        "drift_loss_mean": 2.0,
        "pass_rate": 1.0,
        "scalar": 2.0,
        "entry_count": 2,
        "expectation_count": 2,
        "per_entry": {
            board[0].id: {"drift_loss": 2.2, "pass_fail": True},
            board[1].id: {"drift_loss": 1.8, "pass_fail": False},
        },
        "namespace_aggregates": {},
        "scalar_components": {},
        "generation_id": "v0",
    }

    config = _make_runtime_config(tmp_path)
    asyncio.run(
        run_fast_mode(
            adapter=object(),
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
            parent_historical_agg=parent_historical,
            round_index=1,
            total_rounds=3,
        )
    )

    # An ActiveTournament was visible while at least one challenger run
    # was running — the dashboard's Tournament hall has data to render.
    assert (
        "snapshot" in midflight
    ), "fast-mode must publish ActiveTournament before any challenger run"
    snap = midflight["snapshot"]
    assert snap.parent_generation_id == "v0"
    assert snap.child_generation_id == "v2"
    assert snap.round_index == 1
    assert snap.total_rounds == 3
    # Both sides populated: 2 parent (cached) + 2 child rows.
    parents = [e for e in snap.entries if e.side == "parent"]
    children = [e for e in snap.entries if e.side == "child"]
    assert {e.entry_id for e in parents} == {e.id for e in board}
    assert {e.entry_id for e in children} == {e.id for e in board}
    # Champion-side rows are cached with the per-entry scalar surfaced
    # in ``loss_summary`` — the dashboard renders a head-to-head delta
    # against the live challenger result without re-running the parent.
    assert all(e.status == "cached" for e in parents)
    by_id = {e.entry_id: e for e in parents}
    assert by_id[board[0].id].loss_summary.get("drift_loss") == 2.2
    assert by_id[board[0].id].loss_summary.get("pass_fail") == 1.0
    assert by_id[board[1].id].loss_summary.get("drift_loss") == 1.8
    assert by_id[board[1].id].loss_summary.get("pass_fail") == 0.0
    # At least one challenger row is actively running mid-flight.
    assert any(
        e.status == "running" for e in children
    ), "mid-flight snapshot must capture a running challenger row"

    # The tournament is cleared on exit — the runner owns the lifecycle.
    assert read_active_tournament(tmp_path) is None


def test_run_fast_mode_challenger_progresses_through_running_then_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fast-mode challenger row reaches `completed` by the time the
    round finishes — and the live ``update_tournament_entry`` calls
    succeed because the runner published the record up-front.
    """
    from zicato.runtime.state import read_active_tournament, update_tournament_entry

    child_gen = _make_generation(tmp_path, "v2", "v0")
    board = _board_of(2)
    canned = {
        ("v2", e.id): _loss(generation_id="v2", entry_id=e.id, drift_loss=0.4, pass_fail=True)
        for e in board
    }

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
        del adapter, weights, config, epoch_id
        # The runner published the record up-front — these updates land.
        update_tournament_entry(workspace_root, entry.id, side, status="running")
        update_tournament_entry(workspace_root, entry.id, side, status="completed")
        return canned[(generation.id, entry.id)]

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)

    # Snapshot the tournament right before clear runs.
    captured: dict[str, Any] = {}
    import zicato.runtime.state as _state_mod

    real_clear = _state_mod.clear_active_tournament

    def capturing_clear(workspace_root: Path) -> None:
        snap = read_active_tournament(workspace_root)
        if snap is not None:
            captured["tournament"] = snap
        real_clear(workspace_root)

    monkeypatch.setattr(_state_mod, "clear_active_tournament", capturing_clear)

    parent_historical = {
        "drift_loss_mean": 2.0,
        "pass_rate": 1.0,
        "scalar": 2.0,
        "entry_count": 2,
        "expectation_count": 2,
        "per_entry": {e.id: {"drift_loss": 2.0, "pass_fail": True} for e in board},
        "namespace_aggregates": {},
        "scalar_components": {},
        "generation_id": "v0",
    }

    config = _make_runtime_config(tmp_path)
    asyncio.run(
        run_fast_mode(
            adapter=object(),
            child_gen=child_gen,
            board=board,
            weights=ScoringWeights(),
            config=config,
            workspace_root=tmp_path,
            epoch_id="e0",
            parent_historical_agg=parent_historical,
        )
    )

    t = captured["tournament"]
    children = [e for e in t.entries if e.side == "child"]
    parents = [e for e in t.entries if e.side == "parent"]
    # Every challenger row reached "completed"; every champion row stays
    # "cached" — the runner only ever updates child rows in fast mode.
    assert all(e.status == "completed" for e in children)
    assert all(e.status == "cached" for e in parents)


# ---------------------------------------------------------------------------
# Ladder-mediated holdout confirmation (OVERFITTING.md §4 / §12 #2). The
# Phase-A holdout confirmation now flows through the Ladder governor: it only
# *counts* under the release rule + per-epoch budget, and the round's
# decision record carries the stable ``holdout`` block.
# ---------------------------------------------------------------------------


def _ladder_agg(scalar: float, *, pass_fail: bool = True, entry: str = "h") -> dict[str, Any]:
    return {
        "scalar": scalar,
        "pass_rate": 1.0 if pass_fail else 0.0,
        "per_entry": {entry: {"pass_fail": pass_fail}},
    }


def _promote_outcome(delta_scalar: float = -0.5) -> GateOutcome:
    return GateOutcome(
        decision="promoted", reason="", delta_scalar=delta_scalar, delta_pass_rate=0.0
    )


def _reject_outcome() -> GateOutcome:
    return GateOutcome(
        decision="rejected",
        reason="insufficient improvement: ...",
        delta_scalar=-0.001,
        delta_pass_rate=0.0,
    )


def test_ladder_no_holdout_is_byte_identical(tmp_path: Path) -> None:
    # No holdout slice → the Ladder is a no-op: the train outcome is returned
    # unchanged and ``holdout`` is None (Phase-A / pre-split behaviour).
    train = _promote_outcome()
    parent = _ladder_agg(1.0)
    child = _ladder_agg(0.5)
    outcome, block = runner_mod._ladder_mediated_outcome(
        train_outcome=train,
        parent_agg=parent,
        child_agg=child,
        holdout_parent_agg=None,
        holdout_child_agg=None,
        weights=ScoringWeights(promote_margin=0.1),
        workspace_root=tmp_path,
        epoch_id="e0",
    )
    assert outcome is train
    assert block is None


def test_ladder_released_confirmation_keeps_promote(tmp_path: Path) -> None:
    # A train-win that clears the threshold, with a confirming holdout, stays
    # promoted and populates the block with the shape the dashboard reads.
    train = _promote_outcome()  # train improvement 0.5 >> 0.1 threshold
    outcome, block = runner_mod._ladder_mediated_outcome(
        train_outcome=train,
        parent_agg=_ladder_agg(1.0),
        child_agg=_ladder_agg(0.5),
        holdout_parent_agg=_ladder_agg(1.0),
        holdout_child_agg=_ladder_agg(0.8),  # holdout improved → confirms
        weights=ScoringWeights(promote_margin=0.1),
        workspace_root=tmp_path,
        epoch_id="e0",
    )
    assert outcome.decision == "promoted"
    assert block is not None
    assert set(block) == {
        "confirmed",
        "train_scalar",
        "holdout_scalar",
        "ladder_released",
        "ladder_budget_total",
        "ladder_budget_remaining",
        "threshold",
    }
    assert block["confirmed"] is True
    assert block["ladder_released"] is True
    assert block["train_scalar"] == pytest.approx(0.5)
    assert block["holdout_scalar"] == pytest.approx(0.8)
    assert block["ladder_budget_remaining"] == block["ladder_budget_total"] - 1


def test_ladder_released_nonconfirmation_flips_to_reject(tmp_path: Path) -> None:
    # A released holdout that does NOT confirm flips the train-promote to a
    # holdout reject. The champion stands on reject.
    train = _promote_outcome()
    outcome, block = runner_mod._ladder_mediated_outcome(
        train_outcome=train,
        parent_agg=_ladder_agg(1.0),
        child_agg=_ladder_agg(0.5),
        holdout_parent_agg=_ladder_agg(1.0),
        holdout_child_agg=_ladder_agg(5.0),  # holdout regressed hard → reject
        weights=ScoringWeights(promote_margin=0.1),
        workspace_root=tmp_path,
        epoch_id="e0",
    )
    assert outcome.decision == "rejected"
    assert "holdout_not_confirmed" in outcome.reason
    assert block is not None
    assert block["confirmed"] is False
    assert block["ladder_released"] is True


def test_ladder_train_reject_does_not_consult_holdout(tmp_path: Path) -> None:
    # A train reject fires first; the holdout is not consulted (no budget
    # charged) and the block records confirmed=None with full budget.
    train = _reject_outcome()
    outcome, block = runner_mod._ladder_mediated_outcome(
        train_outcome=train,
        parent_agg=_ladder_agg(1.0),
        child_agg=_ladder_agg(0.999),
        holdout_parent_agg=_ladder_agg(1.0),
        holdout_child_agg=_ladder_agg(0.5),
        weights=ScoringWeights(promote_margin=0.1),
        workspace_root=tmp_path,
        epoch_id="e0",
    )
    assert outcome is train
    assert block is not None
    assert block["confirmed"] is None
    assert block["ladder_released"] is False
    assert block["ladder_budget_remaining"] == block["ladder_budget_total"]


def test_ladder_budget_exhaustion_lets_champion_stand(tmp_path: Path) -> None:
    # With budget=1, the first release consumes it; a second train-win is no
    # longer holdout-gated (it promotes on the train rules alone), even when
    # the holdout would have rejected it.
    from zicato.core.types import LadderConfig, OverfittingConfig

    weights = ScoringWeights(
        promote_margin=0.1,
        overfitting=OverfittingConfig(ladder=LadderConfig(budget=1)),
    )
    train = _promote_outcome()

    out1, _ = runner_mod._ladder_mediated_outcome(
        train_outcome=train,
        parent_agg=_ladder_agg(1.0),
        child_agg=_ladder_agg(0.5),
        holdout_parent_agg=_ladder_agg(1.0),
        holdout_child_agg=_ladder_agg(0.8),
        weights=weights,
        workspace_root=tmp_path,
        epoch_id="e0",
    )
    assert out1.decision == "promoted"

    # Budget now 0: a holdout that WOULD reject is never released → champion
    # stands → the train-promote survives.
    out2, block2 = runner_mod._ladder_mediated_outcome(
        train_outcome=train,
        parent_agg=_ladder_agg(1.0),
        child_agg=_ladder_agg(0.5),
        holdout_parent_agg=_ladder_agg(1.0),
        holdout_child_agg=_ladder_agg(99.0),
        weights=weights,
        workspace_root=tmp_path,
        epoch_id="e0",
    )
    assert out2.decision == "promoted"
    assert block2 is not None
    assert block2["ladder_released"] is False
    assert block2["ladder_budget_remaining"] == 0


def test_ladder_disabled_runs_raw_phase_a_confirmation(tmp_path: Path) -> None:
    # ladder.enabled=False ⇒ raw Phase-A confirmation: every holdout query
    # counts, no budget, no release rule. A regressing holdout rejects.
    from zicato.core.types import LadderConfig, OverfittingConfig

    weights = ScoringWeights(
        promote_margin=0.1,
        overfitting=OverfittingConfig(ladder=LadderConfig(enabled=False)),
    )
    outcome, block = runner_mod._ladder_mediated_outcome(
        train_outcome=_promote_outcome(),
        parent_agg=_ladder_agg(1.0),
        child_agg=_ladder_agg(0.5),
        holdout_parent_agg=_ladder_agg(1.0),
        holdout_child_agg=_ladder_agg(5.0),
        weights=weights,
        workspace_root=tmp_path,
        epoch_id="e0",
    )
    assert outcome.decision == "rejected"
    assert "holdout_not_confirmed" in outcome.reason
    assert block is not None
    # No budget machinery in disabled mode: budget stays at total.
    assert block["ladder_budget_remaining"] == block["ladder_budget_total"]


def test_ladder_withhold_within_band_keeps_promote(tmp_path: Path) -> None:
    # A train-win whose improvement is WITHIN the noise band is withheld: the
    # holdout result does not count this round, so a regressing holdout cannot
    # reject — the train-promote stands and the prior best is re-reported.
    weights = ScoringWeights(promote_margin=0.5)
    # First, a clear release establishes a confirming best.
    out1, _ = runner_mod._ladder_mediated_outcome(
        train_outcome=_promote_outcome(delta_scalar=-0.9),
        parent_agg=_ladder_agg(1.0),
        child_agg=_ladder_agg(0.1),  # improvement 0.9 >= 0.5 → released
        holdout_parent_agg=_ladder_agg(1.0),
        holdout_child_agg=_ladder_agg(0.2),
        weights=weights,
        workspace_root=tmp_path,
        epoch_id="e0",
    )
    assert out1.decision == "promoted"

    # Now a tiny train-win (improvement 0.1 < 0.5 band) with a bad holdout:
    # withheld → not released → cannot reject → promote stands.
    out2, block2 = runner_mod._ladder_mediated_outcome(
        train_outcome=_promote_outcome(delta_scalar=-0.1),
        parent_agg=_ladder_agg(1.0),
        child_agg=_ladder_agg(0.9),
        holdout_parent_agg=_ladder_agg(1.0),
        holdout_child_agg=_ladder_agg(50.0),
        weights=weights,
        workspace_root=tmp_path,
        epoch_id="e0",
    )
    assert out2.decision == "promoted"
    assert block2 is not None
    assert block2["ladder_released"] is False
    assert block2["confirmed"] is True  # the prior best, re-reported
