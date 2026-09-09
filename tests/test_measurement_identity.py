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
    MeasurementDraw,
    MeasurementPurpose,
    recorded_artifact_measurement,
)
from zicato.core.tournament import TournamentStructure
from zicato.core.workspace import measurement_from_run_id, run_id_for_unit
from zicato.runtime.lock import acquire_workspace_lock
from zicato.telemetry.reducer import write_loss_profile
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament import scheduling
from zicato.tournament.unit_cache import _resolve_cached_unit, _unit_loss_path


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
            run_id=run_id_for_unit("g0", "entry", base_seed=seed, epoch_id="e0"),
            drift_loss=float(seed or 0),
            epoch_id="e0",
            generation_id="g0",
            entry_id="entry",
        )

    monkeypatch.setattr(scheduling, "_run_single", measured)

    with acquire_workspace_lock(tmp_path, "test") as writer:

        async def draw(seed: int | None) -> Any:
            return await scheduling._run_unit_cache_first(
                writer=writer,
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
def test_draw_counts_are_not_limited_by_other_measurement_purposes(parameter: str) -> None:
    structure = TournamentStructure("gauntlet", params={parameter: 1001})
    assert structure.params[parameter] == 1001


@pytest.mark.parametrize("purpose", list(MeasurementPurpose))
def test_explicit_measurements_round_trip_without_range_limits(
    purpose: MeasurementPurpose, tmp_path: Path
) -> None:
    draw = MeasurementDraw(purpose, 10001, 17)
    assert MeasurementDraw.from_json(draw.to_json()) == draw
    assert draw.offset(1) == MeasurementDraw(purpose, 10002, 17)
    runtime_id = run_id_for_unit("v0", "entry", draw, epoch_id="e0")
    assert measurement_from_run_id("v0", "entry", runtime_id, epoch_id="e0") == draw
    path = _unit_loss_path(tmp_path, "e0", "v0", "entry", draw)
    assert path.name == f"loss.{purpose}.r10001.json"
    assert recorded_artifact_measurement(path.parent.parent, path, draw) == draw


@pytest.mark.parametrize("value", [-1, True, 1.5, "1"])
def test_invalid_draws_are_rejected(value: Any) -> None:
    with pytest.raises(ValueError, match="draw"):
        MeasurementDraw(MeasurementPurpose.TOURNAMENT, value)


@pytest.mark.parametrize(
    "purpose", [MeasurementPurpose.CALIBRATION, MeasurementPurpose.CONFIRMATION]
)
def test_purposes_do_not_share_paths_or_runtime_ids(
    purpose: MeasurementPurpose, tmp_path: Path
) -> None:
    first = MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0, 17)
    second = MeasurementDraw(purpose, 0, 17)
    assert _unit_loss_path(tmp_path, "e0", "v0", "entry", first) != _unit_loss_path(
        tmp_path, "e0", "v0", "entry", second
    )
    assert run_id_for_unit("v0", "entry", first, epoch_id="e0") != run_id_for_unit(
        "v0", "entry", second, epoch_id="e0"
    )


@pytest.mark.parametrize("recorded", [None, MeasurementDraw(MeasurementPurpose.CALIBRATION, 0)])
def test_missing_or_conflicting_measurement_is_excluded_from_cache(
    tmp_path: Path, recorded: MeasurementDraw | None
) -> None:
    requested = MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0)
    path = _unit_loss_path(tmp_path, "e0", "v0", "entry", requested)
    write_loss_profile(make_loss_profile(measurement=recorded), path)
    before = path.read_bytes()
    assert (
        _resolve_cached_unit(
            workspace_root=tmp_path,
            epoch_id="e0",
            generation_id="v0",
            entry_id="entry",
            measurement=requested,
        )
        is None
    )
    assert path.read_bytes() == before
