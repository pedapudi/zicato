"""Capture fidelity belongs to the paired loss's complete measurement identity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.measurement import MeasurementDraw, measurement_artifact_path
from zicato.core.workspace import run_id_for_unit
from zicato.judge_runtime.io_capture import (
    JudgeIOFileSink,
    build_judge_io_record,
    judge_io_path_for_loss,
    read_judge_io,
)
from zicato.query.execution_plan import _run_result
from zicato.query.reflection_view import _transcript_from_judge_io, _transcript_from_result
from zicato.reflection.adjudicator import _result_context, _verbatim_context
from zicato.reflection.corpus import _read_sidecars
from zicato.telemetry.reducer import write_loss_profile
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament.unit_cache import read_run_result, unit_result_path


@pytest.mark.parametrize("base_seed", [17, None], ids=["seeded", "explicitly-unseeded"])
@pytest.mark.parametrize(
    "change",
    [
        "match",
        "seed",
        "purpose",
        "draw",
        "run",
        "missing",
        "missing-seed",
        "missing-run",
        "malformed",
    ],
)
def test_capture_fidelity_requires_complete_paired_identity(
    tmp_path: Path, base_seed: int | None, change: str
) -> None:
    draw = MeasurementDraw.from_index(0, base_seed=base_seed)
    run_id = run_id_for_unit("v0", "entry", base_seed=base_seed)
    loss = make_loss_profile(run_id=run_id, measurement=draw)
    loss_path = measurement_artifact_path(tmp_path, "loss", 0, base_seed=base_seed)
    write_loss_profile(loss, loss_path)
    provenance = {"measurement": draw.to_json(), "run_id": run_id}
    if change == "seed":
        provenance["measurement"]["base_seed"] = 29
    elif change == "purpose":
        provenance["measurement"]["purpose"] = "evidence_confirmation"
    elif change == "draw":
        provenance["measurement"]["draw"] = 1
    elif change == "run":
        provenance["run_id"] = "different-run"
    elif change == "missing":
        provenance.clear()
    elif change == "missing-seed":
        provenance["measurement"].pop("base_seed")
    elif change == "missing-run":
        provenance.pop("run_id")
    elif change == "malformed":
        provenance["measurement"]["base_seed"] = True
    result = {"format_version": 1, "transcript": ["captured result"], **provenance}
    unit_result_path(loss_path).write_text(json.dumps(result))
    judge = build_judge_io_record(
        judge_name="judge",
        call_index=0,
        reasoning_text="captured reasoning",
        transcript_window=("captured window",),
        raw_response="{}",
        drift_emitted=False,
        kind="",
        severity="",
        detail="",
    )
    judge.update(provenance)
    judge_io_path_for_loss(loss_path).write_text(json.dumps(judge) + "\n")

    available = change == "match"
    assert (_result_context(loss_path) is not None) is available
    assert (_verbatim_context(loss_path, "judge") is not None) is available
    assert (_transcript_from_result(str(loss_path)) is not None) is available
    assert (_transcript_from_judge_io(str(loss_path), "judge") is not None) is available
    result_present, judge_records = _read_sidecars(loss_path, loss)
    assert result_present is available
    assert bool(judge_records) is available
    assert bool(_run_result(loss_path, loss)) is available


def test_historical_capture_remains_visible_without_measurement_certainty(tmp_path: Path) -> None:
    loss = make_loss_profile()
    loss_path = tmp_path / "loss.json"
    write_loss_profile(loss, loss_path)
    payload = {"format_version": 1, "transcript": ["historical result"]}
    unit_result_path(loss_path).write_text(json.dumps(payload))
    sink = JudgeIOFileSink(judge_io_path_for_loss(loss_path))
    sink.record(
        "judge",
        reasoning_text="historical reasoning",
        transcript_window=(),
        raw_response="{}",
        drift_emitted=False,
        kind="",
        severity="",
        detail="",
    )
    assert read_run_result(unit_result_path(loss_path), expected=loss) == payload
    records = read_judge_io(sink.path, expected=loss)
    assert len(records) == 1 and "measurement" not in records[0]
    assert loss.measurement is None
    assert _result_context(loss_path) is not None
    assert _verbatim_context(loss_path, "judge") is not None
