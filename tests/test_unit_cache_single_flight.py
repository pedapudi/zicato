"""Concurrent needs for ONE cold cacheable board unit.

A racing rung schedules several matchups under one ``asyncio.gather``,
and every matchup of the rung needs the SAME champion board unit. The
cache-first choke point resolves the on-disk slot, so two matchups that
look in the same event-loop turn both see a cold cache — the premise
these tests pin, together with the properties the coalescing must not
break: ``force_fresh`` re-sampling, replicate independence, and the
"an infra abort is never reused" invariant.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import zicato.tournament.runner as runner_mod
from zicato.core import BoardEntry, Generation, LossProfile, RuntimeConfig, ScoringWeights
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament.runner import _run_unit_cache_first
from zicato.tournament.unit_cache import _UnitProvenance


def _generation(tmp_path: Path, generation_id: str = "v0") -> Generation:
    return Generation(
        id=generation_id,
        epoch_id="e0",
        parent_id=None,
        snapshot_root=tmp_path / generation_id,
        created_at="2024-01-01T00:00:00Z",
    )


def _entry(entry_id: str = "entry_a") -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )


def _config(workspace: Path) -> RuntimeConfig:
    async def harness_call(system: str, user: str, model: str) -> str:
        return ""

    async def auxiliary_call(system: str, user: str, model: str) -> str:
        return ""

    return RuntimeConfig(
        instance_id="test",
        workspace_root=workspace,
        harness_call_llm=harness_call,
        auxiliary_call_llm=auxiliary_call,
    )


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    return workspace


async def _unit(
    *,
    workspace: Path,
    generation: Generation,
    entry: BoardEntry,
    match_id: str = "",
    replicate_index: int = 0,
    force_fresh: bool = False,
    provenance: dict[str, _UnitProvenance] | None = None,
) -> LossProfile:
    """One call through the cache-first choke point, as a rung matchup makes it."""
    return await _run_unit_cache_first(
        adapter=object(),
        generation=generation,
        entry=entry,
        weights=ScoringWeights(),
        config=_config(workspace),
        workspace_root=workspace,
        epoch_id="e0",
        side="parent",
        replicate_index=replicate_index,
        match_id=match_id,
        force_fresh=force_fresh,
        provenance=provenance,
    )


def _stub_run_single(
    monkeypatch: pytest.MonkeyPatch,
    *,
    started: list[str],
    hold: asyncio.Event | None = None,
    abort_cause: str = "",
) -> None:
    """Install a counting ``_run_single`` that yields before it returns."""

    async def fake_run_single(**kwargs: Any) -> LossProfile:
        started.append(str(kwargs["match_id"]))
        if hold is not None:
            await hold.wait()
        else:
            await asyncio.sleep(0)
        generation = kwargs["generation"]
        entry = kwargs["entry"]
        overrides: dict[str, Any] = {
            "run_id": f"{generation.id}--{entry.id}",
            "generation_id": generation.id,
            "entry_id": entry.id,
            "epoch_id": kwargs["epoch_id"],
            "drift_loss": 1.0,
            "pass_fail": True,
        }
        if abort_cause:
            overrides["abort_cause"] = abort_cause
        return make_loss_profile(**overrides)

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)


def test_concurrent_matchups_run_one_cold_unit_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T1: N matchups needing one cold champion unit start ONE worker."""
    workspace = _workspace(tmp_path)
    champion = _generation(tmp_path)
    entry = _entry()
    started: list[str] = []
    _stub_run_single(monkeypatch, started=started)

    async def rung() -> None:
        await asyncio.gather(
            *(
                _unit(workspace=workspace, generation=champion, entry=entry, match_id=f"rung0_m{i}")
                for i in range(4)
            )
        )

    asyncio.run(rung())

    assert started == ["rung0_m0"]


def test_force_fresh_units_are_never_coalesced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--mode full`` re-samples: two forced units are two runs."""
    workspace = _workspace(tmp_path)
    champion = _generation(tmp_path)
    entry = _entry()
    started: list[str] = []
    _stub_run_single(monkeypatch, started=started)

    async def both() -> None:
        await asyncio.gather(
            _unit(
                workspace=workspace,
                generation=champion,
                entry=entry,
                match_id="m0",
                force_fresh=True,
            ),
            _unit(
                workspace=workspace,
                generation=champion,
                entry=entry,
                match_id="m1",
                force_fresh=True,
            ),
        )

    asyncio.run(both())

    assert started == ["m0", "m1"]


def test_replicate_slots_are_independent_draws(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Replicates MEASURE noise: two slots of one unit never share a run."""
    workspace = _workspace(tmp_path)
    champion = _generation(tmp_path)
    entry = _entry()
    started: list[str] = []
    _stub_run_single(monkeypatch, started=started)

    async def both() -> None:
        await asyncio.gather(
            _unit(
                workspace=workspace,
                generation=champion,
                entry=entry,
                match_id="r0",
                replicate_index=0,
            ),
            _unit(
                workspace=workspace,
                generation=champion,
                entry=entry,
                match_id="r1",
                replicate_index=1,
            ),
        )

    asyncio.run(both())

    assert sorted(started) == ["r0", "r1"]


def test_distinct_units_are_never_coalesced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Different generations / entries key different units."""
    workspace = _workspace(tmp_path)
    started: list[str] = []
    _stub_run_single(monkeypatch, started=started)

    async def four() -> None:
        await asyncio.gather(
            _unit(
                workspace=workspace,
                generation=_generation(tmp_path, "v0"),
                entry=_entry("a"),
                match_id="v0a",
            ),
            _unit(
                workspace=workspace,
                generation=_generation(tmp_path, "v0"),
                entry=_entry("b"),
                match_id="v0b",
            ),
            _unit(
                workspace=workspace,
                generation=_generation(tmp_path, "v1"),
                entry=_entry("a"),
                match_id="v1a",
            ),
            _unit(
                workspace=workspace,
                generation=_generation(tmp_path, "v1"),
                entry=_entry("b"),
                match_id="v1b",
            ),
        )

    asyncio.run(four())

    assert sorted(started) == ["v0a", "v0b", "v1a", "v1b"]


def test_infra_abort_is_never_shared(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An infra abort is not cacheable — so it is not shareable either.

    The cache deliberately refuses to persist a worker crash so the next
    need re-attempts the unit. A coalesced sibling is exactly such a next
    need: fanning one transient crash out to every matchup of the rung
    would poison the whole rung off a single blip.
    """
    workspace = _workspace(tmp_path)
    champion = _generation(tmp_path)
    entry = _entry()
    started: list[str] = []
    _stub_run_single(monkeypatch, started=started, abort_cause="worker_crash")
    provenance: dict[str, _UnitProvenance] = {}

    async def rung() -> tuple[LossProfile, ...]:
        return await asyncio.gather(
            _unit(
                workspace=workspace,
                generation=champion,
                entry=entry,
                match_id="m0",
                provenance=provenance,
            ),
            _unit(
                workspace=workspace,
                generation=champion,
                entry=entry,
                match_id="m1",
                provenance=provenance,
            ),
        )

    asyncio.run(rung())

    assert started == ["m0", "m1"], "the aborted unit must be re-attempted, never reused"
    assert provenance["v0"] == _UnitProvenance(cached=0, fresh=2)


def test_a_failed_evaluation_is_never_shared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A raising evaluation is one matchup's failure, not the rung's."""
    workspace = _workspace(tmp_path)
    champion = _generation(tmp_path)
    entry = _entry()
    started: list[str] = []
    fail_first = True

    async def fake_run_single(**kwargs: Any) -> LossProfile:
        nonlocal fail_first
        started.append(str(kwargs["match_id"]))
        await asyncio.sleep(0)
        if fail_first:
            fail_first = False
            raise RuntimeError("worker transport blew up")
        return make_loss_profile(generation_id="v0", entry_id=entry.id, epoch_id="e0")

    monkeypatch.setattr(runner_mod, "_run_single", fake_run_single)

    async def rung() -> list[Any]:
        return await asyncio.gather(
            _unit(workspace=workspace, generation=champion, entry=entry, match_id="m0"),
            _unit(workspace=workspace, generation=champion, entry=entry, match_id="m1"),
            return_exceptions=True,
        )

    results = asyncio.run(rung())

    assert isinstance(results[0], RuntimeError)
    assert isinstance(results[1], LossProfile), "the waiter must run its own unit, not inherit"
    assert started == ["m0", "m1"]


def test_a_cancelled_evaluation_leaves_the_waiter_a_clean_miss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancelling the running caller must not strand or poison a waiter."""
    workspace = _workspace(tmp_path)
    champion = _generation(tmp_path)
    entry = _entry()
    started: list[str] = []
    hold = asyncio.Event()
    _stub_run_single(monkeypatch, started=started, hold=hold)

    async def rung() -> LossProfile:
        first = asyncio.create_task(
            _unit(workspace=workspace, generation=champion, entry=entry, match_id="m0")
        )
        await asyncio.sleep(0)
        waiter = asyncio.create_task(
            _unit(workspace=workspace, generation=champion, entry=entry, match_id="m1")
        )
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        hold.set()
        return await waiter

    loss = asyncio.run(rung())

    assert started == ["m0", "m1"]
    assert loss.generation_id == "v0"


def test_a_cancelled_waiter_leaves_the_running_unit_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancelling one waiter must not disturb the unit its siblings need."""
    workspace = _workspace(tmp_path)
    champion = _generation(tmp_path)
    entry = _entry()
    started: list[str] = []
    hold = asyncio.Event()
    _stub_run_single(monkeypatch, started=started, hold=hold)

    async def rung() -> LossProfile:
        leader = asyncio.create_task(
            _unit(workspace=workspace, generation=champion, entry=entry, match_id="m0")
        )
        await asyncio.sleep(0)
        follower = asyncio.create_task(
            _unit(workspace=workspace, generation=champion, entry=entry, match_id="m1")
        )
        await asyncio.sleep(0)
        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await follower
        hold.set()
        return await leader

    loss = asyncio.run(rung())

    assert started == ["m0"]
    assert loss.generation_id == "v0"


def test_provenance_counts_actual_worker_launches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cached-vs-fresh tally counts runs, not requests.

    Four matchups sharing one execution launched ONE worker, so the
    round's cost surface must read one fresh unit and three reuses — a
    tally of four fresh would bill work that never happened.
    """
    workspace = _workspace(tmp_path)
    champion = _generation(tmp_path)
    entry = _entry()
    started: list[str] = []
    _stub_run_single(monkeypatch, started=started)
    provenance: dict[str, _UnitProvenance] = {}

    async def rung() -> None:
        await asyncio.gather(
            *(
                _unit(
                    workspace=workspace,
                    generation=champion,
                    entry=entry,
                    match_id=f"m{i}",
                    provenance=provenance,
                )
                for i in range(4)
            )
        )

    asyncio.run(rung())

    assert len(started) == 1
    assert provenance["v0"] == _UnitProvenance(cached=3, fresh=1)
