"""Execution seeds identify durable draws without overwriting earlier evidence."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests._contract_pins import deterministic_weights
from tests._runtime_builders import make_generation, runtime_config
from tests._subprocess_worker_support import (
    CompletingAdapter,
    evaluation_call_llm,
    target_call_llm,
)
from zicato.core import BoardEntry
from zicato.core.measurement import (
    UNKNOWN_SEED,
    MeasurementDraw,
    iter_measurement_artifacts,
    iter_measurement_attempts,
    measurement_artifact_path,
    recorded_artifact_measurement,
)
from zicato.core.workspace import measurement_from_run_id, run_dir, run_id_for_unit
from zicato.judge_runtime.io_capture import judge_io_path_for_loss, read_judge_io
from zicato.telemetry.reducer import read_loss_profile, write_loss_profile
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament.artifacts import archive_unit_artifacts
from zicato.tournament.runner import _run_single, run_fast_mode
from zicato.tournament.scoring import aggregate_generation_score
from zicato.tournament.unit_cache import (
    _average_losses,
    _resolve_cached_unit,
    own_code_board_draws,
    read_run_result,
    unit_result_path,
)


class _CaptureSession:
    async def run(self, entry, sinks, config):
        from goldfive.events import emit, run_started_event

        run_id = run_id_for_unit(entry.context["generation_id"], entry.id, base_seed=config.seed)
        await emit(
            list(sinks), run_started_event(run_id=run_id, sequence=1, goal_summary="capture")
        )
        result = await CompletingAdapter().load(Path()).run(entry, sinks, config)
        return replace(result, run_id=run_id)


class _CaptureAdapter(CompletingAdapter):
    def load(self, generation_root):
        return _CaptureSession()

    def worker_spec(self):
        return {
            "kind": "import",
            "factory": "tests.test_measurement_seed_identity:make_capture_adapter",
        }


def make_capture_adapter():
    return _CaptureAdapter()


@pytest.mark.parametrize("seed", [UNKNOWN_SEED, None, 0, 17, -17])
def test_seed_identity_agrees_with_path_and_preserves_unknown_history(tmp_path: Path, seed) -> None:
    draw = MeasurementDraw.from_index(4001, base_seed=seed)
    run_id = run_id_for_unit("v0", "entry", 4001, base_seed=seed)
    assert measurement_from_run_id("v0", "entry", run_id) == draw
    assert MeasurementDraw.from_json(draw.to_json()) == draw
    assert ("base_seed" in draw.to_json()) == (seed is not UNKNOWN_SEED)
    path = measurement_artifact_path(tmp_path, "loss", 4001, base_seed=seed)
    write_loss_profile(make_loss_profile(measurement=draw), path)
    assert list(iter_measurement_artifacts(tmp_path)) == [path]
    assert (
        recorded_artifact_measurement(tmp_path, path, read_loss_profile(path).measurement) == draw
    )
    other = MeasurementDraw.from_index(4001, base_seed=29)
    with pytest.raises(ValueError, match="seed conflicts"):
        recorded_artifact_measurement(tmp_path, path, other)


@pytest.mark.parametrize("seed", [True, 1.5, "17", "unknown"])
def test_seed_identity_refuses_non_integer_values(seed) -> None:
    with pytest.raises(ValueError, match="base seed"):
        MeasurementDraw.from_json({"purpose": "tournament", "draw": 0, "base_seed": seed})


def test_unknown_historical_seed_cannot_satisfy_an_unseeded_request(tmp_path: Path) -> None:
    coordinates = dict(workspace_root=tmp_path, epoch_id="e0", generation_id="v0", entry_id="entry")
    directory = run_dir(tmp_path, "e0", "v0", "entry")
    historical = measurement_artifact_path(directory, "loss", 0)
    write_loss_profile(
        make_loss_profile(epoch_id="e0", generation_id="v0", entry_id="entry"), historical
    )
    original = historical.read_bytes()
    assert _resolve_cached_unit(**coordinates, replicate_index=0) is not None
    assert _resolve_cached_unit(**coordinates, replicate_index=0, base_seed=None) is None
    assert historical.read_bytes() == original


@pytest.mark.parametrize("field", ["epoch_id", "generation_id", "entry_id"])
def test_cache_refuses_misplaced_measurement_coordinates(tmp_path: Path, field: str) -> None:
    coordinates = dict(epoch_id="e0", generation_id="v0", entry_id="entry")
    profile_coordinates = {**coordinates, field: "other"}
    path = measurement_artifact_path(run_dir(tmp_path, **coordinates), "loss", 0, base_seed=17)
    write_loss_profile(
        make_loss_profile(
            **profile_coordinates, measurement=MeasurementDraw.from_index(0, base_seed=17)
        ),
        path,
    )
    original = path.read_bytes()
    assert (
        _resolve_cached_unit(
            workspace_root=tmp_path, **coordinates, replicate_index=0, base_seed=17
        )
        is None
    )
    assert own_code_board_draws(run_dir(tmp_path, **coordinates)) == []
    assert path.read_bytes() == original


def test_descriptive_draws_do_not_count_filename_aliases_twice(tmp_path: Path) -> None:
    for name in ("loss.json", "loss.r0.json", "loss.r00.json"):
        path = tmp_path / "seed-17" / name
        write_loss_profile(
            make_loss_profile(measurement=MeasurementDraw.from_index(0, base_seed=17)), path
        )
    other_seed = measurement_artifact_path(tmp_path, "loss", 0, base_seed=29)
    write_loss_profile(
        make_loss_profile(measurement=MeasurementDraw.from_index(0, base_seed=29)), other_seed
    )
    assert own_code_board_draws(tmp_path) == [
        (0, tmp_path / "seed-17" / "loss.json"),
        (0, other_seed),
    ]


@pytest.mark.parametrize(
    "run_id", ["seed-017.v0--entry", "seed-17.seed-17.v0--entry", "seed-17.r0.v0--entry"]
)
def test_runtime_identity_refuses_noncanonical_aliases(run_id: str) -> None:
    assert measurement_from_run_id("v0", "entry", run_id) is None


def _artifact_set(root: Path) -> dict[str, bytes]:
    return {
        "loss.json": b'{"run_id":"measured"}\n',
        "events.jsonl": b'{"event":"complete"}\n',
        "result.json": b'{"final_output":"captured"}\n',
        "judge_io.jsonl": b'{"verdict":"accepted"}\n',
        "artifacts.json": b'{"files":["report.txt"]}\n',
        "artifacts/report.txt": b"produced file\n",
    }


def _write_artifacts(root: Path, contents: dict[str, bytes]) -> None:
    for name, body in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


@pytest.mark.parametrize("interruption", ["copy", "clear"])
def test_interrupted_attempt_archival_retains_a_complete_recoverable_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    contents = _artifact_set(tmp_path)
    _write_artifacts(tmp_path, contents)
    if interruption == "copy":
        original = shutil.copyfile

        def interrupted(source, destination, **kwargs):
            if Path(source).name == "result.json":
                raise OSError("copy interrupted")
            return original(source, destination, **kwargs)

        monkeypatch.setattr(shutil, "copyfile", interrupted)
    else:
        original_unlink = Path.unlink

        def interrupted_unlink(path, *args, **kwargs):
            if path == tmp_path / "events.jsonl":
                raise OSError("clear interrupted")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", interrupted_unlink)
    with pytest.raises(OSError, match="interrupted"):
        archive_unit_artifacts(tmp_path / "loss.json")
    archives = list((tmp_path / "attempts").glob("loss-*"))
    if interruption == "copy":
        assert not archives
        assert list(iter_measurement_attempts(tmp_path / "loss.json")) == []
        assert all((tmp_path / name).read_bytes() == body for name, body in contents.items())
    else:
        assert not (tmp_path / "loss.json").exists()
        assert len(archives) == 1
        assert list(iter_measurement_attempts(tmp_path / "loss.json")) == [
            archives[0] / "loss.json"
        ]
        assert all((archives[0] / name).read_bytes() == body for name, body in contents.items())
    monkeypatch.undo()
    archive_unit_artifacts(tmp_path / "loss.json")
    archives = list((tmp_path / "attempts").glob("loss-*"))
    assert any(
        all(
            (archive / name).is_file() and (archive / name).read_bytes() == body
            for name, body in contents.items()
        )
        for archive in archives
    )
    assert not any((tmp_path / name).exists() for name in contents)


def test_attempt_reader_excludes_unpublished_and_incomplete_archives(tmp_path: Path) -> None:
    _write_artifacts(tmp_path, _artifact_set(tmp_path))
    archive = archive_unit_artifacts(tmp_path / "loss.json")
    assert archive is not None
    pending = tmp_path / "attempts" / ".pending-copy"
    _write_artifacts(pending, {"loss.json": b"unpublished"})
    incomplete = tmp_path / "attempts" / ("loss-" + "a" * 64)
    _write_artifacts(incomplete, {"events.jsonl": b"interrupted execution"})
    for attempt in (2, 1):
        (tmp_path / f"loss.a{attempt}.json").write_text("{}")
    assert list(iter_measurement_attempts(tmp_path / "loss.json")) == [
        tmp_path / "loss.a1.json",
        tmp_path / "loss.a2.json",
        archive / "loss.json",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_workers_preserve_other_seeds_and_complete_attempt_captures(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    generation = make_generation(workspace, "v0")
    entry = BoardEntry(id="entry", kind="single_turn", input="x", wall_clock_budget_seconds=5)
    config = replace(
        runtime_config(workspace),
        target_call_llm=target_call_llm,
        evaluation_call_llm=evaluation_call_llm,
        worker_permit_dir=tmp_path / "permits",
    )
    directory = run_dir(workspace, "e0", "v0", "entry")
    snapshots: dict[object, dict[str, bytes]] = {}
    identities = set()
    for seed in (17, 29, None):
        loss = await _run_single(
            adapter=_CaptureAdapter(),
            generation=generation,
            entry=entry,
            weights=deterministic_weights(),
            config=replace(config, seed=seed),
            workspace_root=workspace,
            epoch_id="e0",
            side="child",
        )
        assert loss.measurement == MeasurementDraw.from_index(0, base_seed=seed)
        identities.add(loss.run_id)
        path = measurement_artifact_path(directory, "loss", 0, base_seed=seed)
        snapshots[seed] = {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
        assert {"loss.json", "events.jsonl", "result.json", "judge_io.jsonl"} <= snapshots[
            seed
        ].keys()
        assert (
            json.loads(snapshots[seed]["result.json"])["measurement"] == loss.measurement.to_json()
        )
        assert read_run_result(unit_result_path(path), expected=loss) is not None
        captured_judges = read_judge_io(judge_io_path_for_loss(path), expected=loss)
        assert captured_judges
        assert all(
            row["measurement"] == loss.measurement.to_json() and row["run_id"] == loss.run_id
            for row in captured_judges
        )
    assert len(identities) == 3
    assert not (directory / "loss.json").exists()
    await _run_single(
        adapter=_CaptureAdapter(),
        generation=generation,
        entry=entry,
        weights=deterministic_weights(),
        config=replace(config, seed=17),
        workspace_root=workspace,
        epoch_id="e0",
        side="child",
    )
    for seed in (29, None):
        parent = measurement_artifact_path(directory, "loss", 0, base_seed=seed).parent
        assert all((parent / name).read_bytes() == body for name, body in snapshots[seed].items())
    parent = measurement_artifact_path(directory, "loss", 0, base_seed=17).parent
    archives = list((parent / "attempts").glob("loss-*"))
    assert len(archives) == 1
    assert all((archives[0] / name).read_bytes() == body for name, body in snapshots[17].items())
    archived_path = archives[0] / "loss.json"
    archived_loss = read_loss_profile(archived_path)
    assert read_run_result(unit_result_path(archived_path), expected=archived_loss) is not None
    assert read_judge_io(judge_io_path_for_loss(archived_path), expected=archived_loss)


def test_replicate_fold_retains_seed_provenance_without_claiming_unknown_draws() -> None:
    profiles = [
        make_loss_profile(measurement=MeasurementDraw.from_index(index, base_seed=17))
        for index in (0, 1)
    ]
    folded = _average_losses([{p.entry_id: p} for p in profiles])[profiles[0].entry_id]
    assert folded.measurement is None
    assert folded.source_measurements == tuple(p.measurement for p in profiles)
    assert aggregate_generation_score([folded], deterministic_weights())["base_seed"] == 17
    ambiguous = _average_losses(
        [
            {profiles[0].entry_id: profiles[0]},
            {profiles[0].entry_id: make_loss_profile()},
        ]
    )[profiles[0].entry_id]
    assert None in ambiguous.source_measurements
    assert "base_seed" not in aggregate_generation_score([ambiguous], deterministic_weights())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "aggregate",
    [
        {"generation_id": "v0"},
        {"generation_id": "v0", "base_seed": 29},
        {"generation_id": "v2", "base_seed": 17},
    ],
)
async def test_cached_champion_requires_requested_parent_and_seed(
    tmp_path: Path, aggregate
) -> None:
    with pytest.raises(ValueError, match="generation and seed"):
        await run_fast_mode(
            adapter=object(),
            child_gen=make_generation(tmp_path, "v1"),
            board=[],
            weights=deterministic_weights(),
            config=replace(runtime_config(tmp_path), seed=17),
            workspace_root=tmp_path,
            epoch_id="e0",
            parent_historical_agg=aggregate,
            parent_generation_id="v0",
        )


@pytest.mark.asyncio
async def test_forced_reruns_serialize_the_same_physical_slot(tmp_path: Path, monkeypatch) -> None:
    from zicato.tournament import scheduling

    active = 0
    calls = 0

    async def measured(**kwargs):
        nonlocal active, calls
        active += 1
        calls += 1
        assert active == 1
        await asyncio.sleep(0.01)
        active -= 1
        return make_loss_profile(drift_loss=float(calls))

    monkeypatch.setattr(scheduling, "_run_single", measured)
    generation = make_generation(tmp_path, "v0")
    entry = BoardEntry(id="entry", kind="single_turn", input="x", wall_clock_budget_seconds=1)

    async def run():
        return await scheduling._run_unit_cache_first(
            adapter=object(),
            generation=generation,
            entry=entry,
            weights=deterministic_weights(),
            config=replace(runtime_config(tmp_path), seed=17),
            workspace_root=tmp_path,
            epoch_id="e0",
            side="child",
            force_fresh=True,
        )

    first, second = await asyncio.gather(run(), run())
    assert calls == 2
    assert [first.drift_loss, second.drift_loss] == [1.0, 2.0]
    directory = run_dir(tmp_path, "e0", "v0", "entry")
    path = measurement_artifact_path(directory, "loss", 0, base_seed=17)
    archived = list((path.parent / "attempts").glob("loss-*/loss.json"))
    assert len(archived) == 1
    assert read_loss_profile(archived[0]).drift_loss == 1
