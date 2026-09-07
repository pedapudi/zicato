"""Measurement intervals must stay within the purpose that owns their first draw."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests._contract_pins import deterministic_weights
from tests._runtime_builders import make_generation, runtime_config
from zicato.core import BoardEntry
from zicato.core.measurement import (
    MEASUREMENT_RANGES,
    MeasurementDraw,
    MeasurementPurpose,
    recorded_measurement,
    validate_measurement_interval,
)
from zicato.core.tournament import TournamentStructure
from zicato.core.workspace import run_id_for_unit
from zicato.query.paths import WorkspacePaths
from zicato.query.replicate_scores import (
    cell_replicate_draws_indexed,
    measurement_band_draws_indexed,
)
from zicato.telemetry.reducer import read_loss_profile, write_loss_profile
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament import scheduling
from zicato.tournament.unit_cache import _persist_unit_loss, _resolve_cached_unit, _unit_loss_path


@pytest.mark.asyncio
async def test_cache_reuse_requires_the_requested_execution_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = make_generation(tmp_path, "g0")
    entry = BoardEntry(id="entry", kind="single_turn", input="x", wall_clock_budget_seconds=1)
    calls: list[int | None] = []

    async def measured(**kwargs: Any) -> Any:
        seed = kwargs["config"].seed
        calls.append(seed)
        await asyncio.sleep(0)
        return make_loss_profile(
            run_id=run_id_for_unit("g0", "entry", base_seed=seed),
            drift_loss=float(seed or 0),
            epoch_id="e0",
            generation_id="g0",
            entry_id="entry",
        )

    monkeypatch.setattr(scheduling, "_run_single", measured)

    async def draw(seed: int | None) -> Any:
        return await scheduling._run_unit_cache_first(
            adapter=object(),
            generation=generation,
            entry=entry,
            weights=deterministic_weights(),
            config=replace(runtime_config(tmp_path), seed=seed),
            workspace_root=tmp_path,
            epoch_id="e0",
            side="child",
        )

    first = await draw(17)
    assert (await draw(17)).drift_loss == first.drift_loss
    second, repeated = await asyncio.gather(draw(29), draw(29))
    assert second.drift_loss == 29
    assert repeated == second
    assert (await draw(17)).drift_loss == 17
    assert calls == [17, 29]


@pytest.mark.parametrize("parameter", ["replicates", "promote_confidence_replicates"])
def test_authored_replication_refuses_crossing_measurement_ranges(parameter: str) -> None:
    with pytest.raises(ValueError, match="1000|measurement"):
        TournamentStructure(params={parameter: 1001})


@pytest.mark.asyncio
@pytest.mark.parametrize("base", [999, 1999, 2999, 3999, 4999, 5999, 6999])
async def test_offset_plus_count_is_validated_before_scheduling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: int
) -> None:
    parent = make_generation(tmp_path, "parent")
    child = make_generation(tmp_path, "child")
    existing = tmp_path / "existing-record.json"
    existing.write_bytes(b'{"measurement": "preserved"}\n')

    async def unexpected_schedule(**kwargs: Any) -> Any:
        pytest.fail("measurement validation must precede scheduling")

    monkeypatch.setattr(scheduling, "_run_board_units_full", unexpected_schedule)
    with pytest.raises(ValueError, match="measurement|range"):
        await scheduling._run_replicated(
            adapter=object(),
            left_gen=parent,
            right_gen=child,
            board=[],
            weights=deterministic_weights(),
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
            replicates=2,
            replicate_base=base,
            fast=False,
        )
    assert existing.read_bytes() == b'{"measurement": "preserved"}\n'
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == [
        "existing-record.json",
        "snap",
        "snap/child",
        "snap/parent",
    ]


@pytest.mark.parametrize("allocation", MEASUREMENT_RANGES, ids=lambda a: str(a.purpose))
def test_complete_interval_and_last_draw_round_trip(allocation: Any, tmp_path: Path) -> None:
    first = validate_measurement_interval(allocation.start, allocation.span)
    assert first == MeasurementDraw(allocation.purpose, 0)
    last = validate_measurement_interval(allocation.stop - 1, 1)
    assert last.draw == allocation.span - 1
    assert MeasurementDraw.from_json(last.to_json()) == last
    with pytest.raises(ValueError, match="measurement interval"):
        validate_measurement_interval(allocation.start, allocation.span + 1)
    with pytest.raises(ValueError, match="measurement draw"):
        MeasurementDraw(allocation.purpose, allocation.span)

    coordinates = dict(
        workspace_root=tmp_path,
        epoch_id="e0",
        generation_id="g0",
        entry_id="entry",
        replicate_index=last.replicate_index,
    )
    _persist_unit_loss(
        **coordinates, loss=make_loss_profile(epoch_id="e0", generation_id="g0", entry_id="entry")
    )
    path = _unit_loss_path(tmp_path, "e0", "g0", "entry", last.replicate_index)
    before = path.read_bytes()
    cached = _resolve_cached_unit(**coordinates)
    assert cached is not None and cached.measurement == last
    assert read_loss_profile(path).measurement == last
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "index,producer,purpose",
    [
        (0, "round-1", MeasurementPurpose.TOURNAMENT),
        (1000, "aa-calibration:0", MeasurementPurpose.CALIBRATION),
        (2000, "contract-preflight:degraded:instruction", MeasurementPurpose.PREFLIGHT),
        (3000, "candidate-screen", MeasurementPurpose.SCREEN),
        (4000, "bt-replicate:r4000:parent:child", MeasurementPurpose.CONFIRMATION),
        (5000, "reflection:record:r0", MeasurementPurpose.REFLECTION),
        (6000, "admission-noise:0", MeasurementPurpose.ADMISSION),
    ],
)
def test_historical_producer_establishes_purpose(index: int, producer: str, purpose: Any) -> None:
    assert recorded_measurement(index, measurement=None, match_id=producer) == MeasurementDraw(
        purpose, 0
    )


@pytest.mark.parametrize(
    "index,producer,identity",
    [
        (1000, "round-1", None),
        (2000, "aa-calibration:1000", None),
        (4000, "", None),
        (5000, "bt-replicate:r5000:parent:child", None),
        (
            4000,
            "bt-replicate:r4000:parent:child",
            MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
        ),
    ],
)
def test_collision_remains_auditable_but_is_not_cache_or_evidence(
    tmp_path: Path,
    index: int,
    producer: str,
    identity: MeasurementDraw | None,
) -> None:
    path = _unit_loss_path(tmp_path, "e0", "g0", "entry", index)
    write_loss_profile(make_loss_profile(match_id=producer, measurement=identity), path)
    before = path.read_bytes()
    assert (
        _resolve_cached_unit(
            workspace_root=tmp_path,
            epoch_id="e0",
            generation_id="g0",
            entry_id="entry",
            replicate_index=index,
        )
        is None
    )
    paths = WorkspacePaths(tmp_path)
    assert cell_replicate_draws_indexed(paths, "e0", "g0", "entry") == []
    bands = measurement_band_draws_indexed(paths, "e0", "g0", "entry")
    assert len(bands) == 1 and bands[0][0] == index and bands[0][1].key == "ambiguous"
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_colliding_history_is_preserved_beside_a_known_seed_draw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zicato.core import BoardEntry
    from zicato.tournament.unit_cache import read_unit_loss_history

    generation = make_generation(tmp_path, "g0")
    entry = BoardEntry(id="entry", kind="single_turn", input="input", wall_clock_budget_seconds=60)
    path = _unit_loss_path(tmp_path, "e0", "g0", "entry", 4000)
    write_loss_profile(make_loss_profile(match_id="candidate-screen"), path)
    calls = 0

    async def measured(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        assert kwargs["entry"].context["replicate_index"] == "4000"
        return make_loss_profile(
            run_id=run_id_for_unit("g0", "entry", 4000, base_seed=kwargs["config"].seed),
            match_id="bt-replicate:r4000:parent:child",
            score=0.75,
            epoch_id="e0",
            generation_id="g0",
            entry_id="entry",
        )

    monkeypatch.setattr(scheduling, "_run_single", measured)
    for _ in range(2):
        result = await scheduling._run_unit_cache_first(
            adapter=object(),
            generation=generation,
            entry=entry,
            weights=deterministic_weights(),
            config=runtime_config(tmp_path),
            workspace_root=tmp_path,
            epoch_id="e0",
            side="parent",
            replicate_index=4000,
        )
        assert result.measurement == MeasurementDraw(MeasurementPurpose.CONFIRMATION, 0, None)
        assert result.score == 0.75
    assert calls == 1
    history = read_unit_loss_history(tmp_path, "e0", "g0", "entry", 4000)
    assert [profile.match_id for profile in history] == ["candidate-screen"]
    selected = _unit_loss_path(tmp_path, "e0", "g0", "entry", 4000, base_seed=None)
    assert read_loss_profile(selected).match_id == "bt-replicate:r4000:parent:child"


@pytest.mark.asyncio
async def test_evidence_allocation_refuses_the_first_reflection_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zicato.selection import driver

    calls: list[int] = []

    async def measured(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["replicate_base"])
        return object()

    monkeypatch.setattr(driver, "EVIDENCE_REPLICATE_BASE", 4999)
    duel = driver.make_evidence_replicate_duel(measured)
    await duel("parent", "child")
    with pytest.raises(ValueError, match="belongs to"):
        await duel("parent", "child")
    assert calls == [4999]


def test_canonical_projection_readers_exclude_conflicting_measurement(tmp_path: Path) -> None:
    from zicato.workspace.layout import WorkspaceLayout
    from zicato.workspace.reads import read_loss

    path = _unit_loss_path(tmp_path, "e0", "g0", "entry", 0)
    write_loss_profile(
        make_loss_profile(measurement=MeasurementDraw(MeasurementPurpose.CALIBRATION, 0)),
        path,
    )
    before = path.read_bytes()
    assert read_loss(WorkspaceLayout.from_root(tmp_path), "e0", "g0", "entry") is None
    assert path.read_bytes() == before
