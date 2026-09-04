"""Tests for the L3 subprocess-isolation layer.

Every tournament run executes in its own OS process — a
``python -m zicato._tournament_worker`` subprocess. These tests cover:

* a worker run end-to-end with a stub adapter (no real LLM / goldfive):
  the worker writes ``active_runs/{run_id}.json`` with its OWN pid, runs
  the entry, writes ``loss.json`` + a result file, exits 0, and removes
  its ``active_runs`` file;
* the PARENT-side budget: a worker that blocks past
  ``wall_clock_budget_seconds + GRACE`` is SIGTERM'd then SIGKILL'd by
  :func:`zicato.tournament.runner._run_single`, which returns an aborted
  :class:`LossProfile` so the tournament continues;
* the SUPERVISOR-kill path: a worker killed externally mid-run leaves no
  result file; ``_run_single`` records an aborted run with no exception;
* a worker that ignores SIGTERM: the parent escalates to SIGKILL after
  the grace.

The stub adapter / callables live in
:mod:`tests._subprocess_worker_support` because the worker subprocess
must import them from a real dotted path — closures and
``sys.modules``-monkeypatched stubs are invisible across the process
boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

import zicato.tournament.runner as runner_mod
from tests._runtime_builders import make_generation
from tests._subprocess_worker_support import (
    CompletingAdapter,
    EmittingThenSleepingAdapter,
    SleepingAdapter,
    SnapshotWritingAdapter,
    StubAdapter,
    evaluation_call_llm,
    target_call_llm,
)
from zicato.core import (
    BoardEntry,
    DriftCount,
    Expectation,
    ExpectationKind,
    ExpectationResult,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
    is_infra_abort_cause,
)
from zicato.core.types import LadderConfig, OverfittingConfig
from zicato.core.workspace import (
    events_jsonl_path,
    ladder_state_path,
    loss_profile_path,
    run_id_for_unit,
)
from zicato.runtime.paths import active_run_path
from zicato.runtime.state import ActiveRun
from zicato.tournament.runner import _run_single, run_tournament
from zicato.tournament.worker_transport import _stamp_replicate_index

# Every test here spawns (or deliberately kills) real worker subprocesses —
# the process-isolation semantics ARE the coverage, so none of them may be
# stubbed. The whole module measures about 25 seconds across 18 tests, so it
# runs in the default tier.
pytestmark = [pytest.mark.integration]

# ---------------------------------------------------------------------------
# Hermeticity fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ``tempfile.tempdir`` into this test's ``tmp_path``.

    Several tests in this file glob ``tempfile.gettempdir()`` for leaked
    ``ztw-snap-<run_id>-*`` working copies. Two of them use the same
    ``run_id`` (``v0--entry_a``), so a leak from one — e.g. the
    SIGTERM->SIGKILL escalation path under load — pollutes the system
    ``/tmp`` and fails the next run on the *next* pytest invocation.
    Patching ``tempfile.tempdir`` redirects every ``mkdtemp`` /
    ``mkstemp`` call (including the runner's) AND every
    ``gettempdir()`` lookup the assertions perform into this test's own
    ``tmp_path``, so each test starts from an empty, private temp root.
    """
    isolated = tmp_path / "ztw-tmp"
    isolated.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(isolated))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(entry_id: str = "entry_a", budget_s: int = 60) -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=budget_s,
        input="hello",
    )


def _config(workspace: Path, *, supervisor_kill_wait_s: float = 20.0) -> RuntimeConfig:
    # The two callables are real, importable, module-level objects so the
    # worker subprocess can re-resolve them from a dotted path.
    #
    # supervisor_kill_wait_s: tests that drive an over-budget worker with NO
    # supervisor attached shrink this — the production default (20s) is the
    # no-supervisor abort-latency floor and would dominate the test's
    # wall-clock time for nothing.
    return RuntimeConfig(
        instance_id="test",
        workspace_root=workspace,
        target_call_llm=target_call_llm,
        evaluation_call_llm=evaluation_call_llm,
        supervisor_kill_wait_s=supervisor_kill_wait_s,
        worker_permit_dir=workspace.parent / "worker-permits",
    )


def _worker_env() -> dict[str, str]:
    """Env for a directly-spawned worker: worktree root on PYTHONPATH.

    ``_run_single``-driven tests inherit the parent's env automatically;
    a directly-spawned worker needs the worktree root explicitly so it
    can import ``tests._subprocess_worker_support``.
    """
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root if not existing else f"{root}{os.pathsep}{existing}"
    return env


def test_two_replicates_of_one_unit_hold_distinct_active_runs(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry(budget_s=5)
    entries = [_stamp_replicate_index([entry], replicate)[0] for replicate in (0, 1)]
    active_dir = workspace / "runtime" / "active_runs"

    async def _drive() -> list[LossProfile]:
        tasks = [
            asyncio.create_task(
                _run_single(
                    adapter=EmittingThenSleepingAdapter(),
                    generation=generation,
                    entry=replicate_entry,
                    weights=ScoringWeights(),
                    config=_config(workspace),
                    workspace_root=workspace,
                    epoch_id="e0",
                    side="parent",
                )
            )
            for replicate_entry in entries
        ]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(list(active_dir.glob("*.json"))) < 2:
            await asyncio.sleep(0.05)
        records = [
            json.loads(path.read_text(encoding="utf-8")) for path in active_dir.glob("*.json")
        ]
        assert len(records) == 2, "concurrent replicates collapsed into one active-run slot"
        assert len({record["run_id"] for record in records}) == 2
        assert {Path(record["events_jsonl_path"]).name for record in records} == {
            "events.jsonl",
            "events.r1.jsonl",
        }
        return list(await asyncio.gather(*tasks))

    losses = asyncio.run(asyncio.wait_for(_drive(), timeout=20))
    assert len(losses) == 2 and all(loss.wall_clock_budget_exceeded for loss in losses)
    assert not list((workspace / "runtime" / "active_runs").glob("*.json"))
    assert not list((workspace / "runtime" / "control" / "kill_requests").glob("*"))
    assert not list(Path(tempfile.gettempdir()).iterdir())


def _write_args_file(
    args_path: Path,
    *,
    workspace: Path,
    generation: Generation,
    entry: BoardEntry,
    result_path: Path,
    adapter_factory: str,
) -> None:
    """Write a worker args file pointing at a stub adapter."""
    sink_path = events_jsonl_path(workspace, "e0", generation.id, entry.id)
    loss_path = loss_profile_path(workspace, "e0", generation.id, entry.id)
    payload = {
        "workspace_root": str(workspace),
        "epoch_id": "e0",
        "generation_id": generation.id,
        "snapshot_root": str(generation.snapshot_root),
        "entry": {
            "id": entry.id,
            "kind": entry.kind,
            "wall_clock_budget_seconds": entry.wall_clock_budget_seconds,
            "input": entry.input,
        },
        "adapter": {"kind": "import", "factory": adapter_factory},
        "target_role": {"dotted": "tests._subprocess_worker_support:target_call_llm"},
        "evaluation_role": {"dotted": "tests._subprocess_worker_support:evaluation_call_llm"},
        "run_id": run_id_for_unit(generation.id, entry.id),
        "sink_events_path": str(sink_path),
        "loss_path": str(loss_path),
        "result_path": str(result_path),
        "instance_id": "test",
        "seed": None,
        "harmonograf_url": "",
        "weights": {},
    }
    args_path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Worker run end-to-end
# ---------------------------------------------------------------------------


def test_worker_runs_entry_end_to_end(tmp_path: Path) -> None:
    """A directly-spawned worker writes active_runs with its own pid, runs the
    entry, writes loss.json + result file, exits 0, and cleans up active_runs."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry()

    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    _write_args_file(
        args_path,
        workspace=workspace,
        generation=generation,
        entry=entry,
        result_path=result_path,
        adapter_factory="tests._subprocess_worker_support:make_stub_adapter",
    )

    proc = _spawn_worker_blocking(args_path)
    assert proc.returncode == 0, "worker should exit cleanly"

    run_id = f"{generation.id}--{entry.id}"

    # loss.json was written by the worker.
    loss_path = loss_profile_path(workspace, "e0", generation.id, entry.id)
    assert loss_path.exists()

    # The result file carries the agreed schema and points at loss.json.
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["schema"] == "zicato.tournament_worker.result/1"
    assert result["loss_profile_path"] == str(loss_path)
    assert result["aborted"] is False
    assert result["abort_reason"] == ""
    assert isinstance(result["runtime_ms"], int)

    # On a clean exit the worker removed its own active_runs file.
    assert not active_run_path(workspace, run_id).exists()


def test_worker_captures_unknown_files_before_grading(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry()
    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    scratch = tmp_path / "scratch"
    _write_args_file(
        args_path,
        workspace=workspace,
        generation=generation,
        entry=entry,
        result_path=result_path,
        adapter_factory="tests._subprocess_worker_support:make_artifact_writing_adapter",
    )
    payload = json.loads(args_path.read_text(encoding="utf-8"))
    payload["scratch_dir"] = str(scratch)
    payload["entry"]["expectation"] = {
        "kind": "predicate",
        "spec": "tests._subprocess_worker_support:artifact_inventory_is_visible",
    }
    args_path.write_text(json.dumps(payload), encoding="utf-8")

    proc = _spawn_worker_blocking(args_path)

    assert proc.returncode == 0, proc.stderr.decode()
    loss_path = loss_profile_path(workspace, "e0", generation.id, entry.id)
    manifest_path = loss_path.with_name("artifacts.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [item["path"] for item in manifest["files"]] == [
        "render.bin",
        "reports/entry_a/summary.html",
    ]
    assert (loss_path.parent / "artifacts" / "reports" / "entry_a" / "summary.html").exists()
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["run_result"]["artifacts"]["manifest_path"] == str(manifest_path)
    loss = json.loads(loss_path.read_text(encoding="utf-8"))
    assert loss["expectation_result"]["passed"] is True


def test_worker_penalises_aborted_run_in_loss_json(tmp_path: Path) -> None:
    """A run whose adapter returns an aborted RunResult is scored worst-case.

    Regression for F3: a run that crashes (here, the adapter returns a
    ``RunResult(aborted=True, abort_reason='harness_exception:...')``)
    finishes near-instantly with an empty events file. The worker still
    exits 0 with a clean result file, so the runner reads back the
    ``loss.json`` it wrote. That ``loss.json`` must carry the
    not-completed penalty — never ``drift_loss == 0.0`` — or a
    challenger generation could win a tournament by crashing fast.
    """
    from zicato.telemetry.reducer import read_loss_profile

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry()

    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    _write_args_file(
        args_path,
        workspace=workspace,
        generation=generation,
        entry=entry,
        result_path=result_path,
        adapter_factory="tests._subprocess_worker_support:make_aborting_adapter",
    )

    proc = _spawn_worker_blocking(args_path)
    # A crashed *run* is a clean *worker* exit — the worker caught the
    # adapter's aborted RunResult and wrote loss.json + result file.
    assert proc.returncode == 0, "an aborted-run worker still exits cleanly"

    result = json.loads(result_path.read_text(encoding="utf-8"))
    # The result-file ``aborted`` flag tracks the worker's *budget* abort
    # only; a harness crash is not a budget abort, so it stays False. The
    # run-level abort lives on the embedded RunResult.
    assert result["aborted"] is False
    assert result["run_result"]["aborted"] is True

    loss_path = loss_profile_path(workspace, "e0", generation.id, entry.id)
    assert loss_path.exists()
    profile = read_loss_profile(loss_path)
    # The crash is charged, not rewarded. The charge is the failure channel's
    # — not_completed_weight 50.0 + task_failure_weight 10.0 × the floored
    # ratio 1.0 = 60.0 under the default weights — and the run emitted no
    # drift, so drift_loss is honestly 0.0.
    assert profile.drift_loss == 0.0
    assert profile.not_completed is True
    assert profile.task_failure_ratio == pytest.approx(1.0)
    failure = {mc.name: mc.count for mc in profile.unified_metrics()}
    assert failure["failure:not_completed"] == 1.0
    assert failure["failure:tasks"] == pytest.approx(1.0)
    # ...and the charge is attributable: the adapter's own abort reason is
    # the only record of WHY an empty-drift profile costs 60.0.
    assert profile.not_completed_reason == "harness_exception:TypeError"
    # It is NOT stamped on abort_cause: any non-budget value there reads as an
    # infra abort, which would stop this scored failure from being cached at
    # all — turning a crash into "no evidence" instead of a worst-case score.
    assert profile.abort_cause is None
    assert is_infra_abort_cause(profile.abort_cause) is False
    # The unit's wall-clock span is recorded even on the aborted path.
    assert profile.started_at is not None
    assert profile.ended_at is not None
    assert profile.started_at <= profile.ended_at


def test_per_judge_weights_survive_worker_serialize_deserialize() -> None:
    """``per_judge_weights`` weight a custom-judge drift through the WORKER path.

    Regression for the P0 in which ``per_judge_weights`` and
    ``default_judge_weight`` were silently dropped when scoring weights
    were serialised for the subprocess worker — so every tournament run
    scored custom-judge drift at the dataclass default ``1.0`` instead of
    the operator's configured weight. The first-class ``per_kind_weights``
    were carried, masking the bug for non-judge drift.

    This walks the exact transport the worker uses: the serialise side
    (:func:`zicato.tournament.runner._weights_spec`) → a JSON round-trip
    (the args file is JSON) → the deserialise side
    (:func:`zicato._tournament_worker._weights_from_args`), then feeds the
    reconstructed weights into the same drift-loss consumer the worker
    invokes (:func:`compute_drift_loss` via ``reduce_loss``).

    Hand-check: a ``file_findability`` custom judge emitting raw drift 14
    (count 14 × ``info`` severity weight 1.0) configured at weight 2.0 must
    contribute 28 to the judge channel. The dropped-weight bug yields 14.
    """
    from zicato._tournament_worker import _weights_from_args
    from zicato.core import DriftCount
    from zicato.telemetry.reducer import compute_per_judge_loss
    from zicato.tournament.runner import _weights_spec

    weights = ScoringWeights(
        severity_weights={"info": 1.0, "warning": 3.0, "critical": 10.0},
        per_judge_weights={"file_findability": 2.0},
        default_judge_weight=1.0,
    )

    # Serialise → JSON → deserialise: exactly what the worker subprocess sees.
    spec = _weights_spec(weights)
    assert spec["per_judge_weights"] == {"file_findability": 2.0}
    assert spec["default_judge_weight"] == 1.0
    round_tripped = _weights_from_args({"weights": json.loads(json.dumps(spec))})
    assert round_tripped.per_judge_weights == {"file_findability": 2.0}
    assert round_tripped.default_judge_weight == 1.0

    # raw drift = count 14 × info severity 1.0 = 14.
    drift = (DriftCount(kind="custom:file_findability", severity="info", count=14),)

    weighted = sum(jl.weighted_loss for jl in compute_per_judge_loss(drift, round_tripped))
    # 14 raw × per_judge weight 2.0 = 28. The bug (weight dropped to the
    # default 1.0) would give 14.
    assert weighted == pytest.approx(28.0)

    # Counterfactual: the unconfigured worker-side path (weight dropped)
    # produces the under-counted 14 — the exact bug we are guarding.
    dropped = ScoringWeights(severity_weights={"info": 1.0, "warning": 3.0, "critical": 10.0})
    under_counted = sum(jl.weighted_loss for jl in compute_per_judge_loss(drift, dropped))
    assert under_counted == pytest.approx(14.0)
    assert weighted != under_counted


def test_pass_rate_monotonicity_scope_survives_worker_serialize_deserialize() -> None:
    """``pass_rate_monotonicity_scope`` survives the WORKER transport (issue #17).

    A field present in-process but missing from the worker's serialise /
    deserialise pair is silently reset to its default — the same defect
    class fixed for ``per_judge_weights``. Here that would silently downgrade
    an operator's ``aggregate`` scope to the default ``per_entry`` inside the
    subprocess, so a net-improving challenger that reshuffles entries would
    be (wrongly) rejected by the worker-side gate-view.

    This walks the exact transport the worker uses — the serialise side
    (:func:`zicato.tournament.runner._weights_spec`) → a JSON round-trip
    (the args file is JSON) → the deserialise side
    (:func:`zicato._tournament_worker._weights_from_args`) — then proves the
    reconstructed scope DRIVES the gate decision: the same parent/child pair
    that promotes under ``aggregate`` is rejected under ``per_entry``.
    """
    from zicato._tournament_worker import _weights_from_args
    from zicato.tournament.gate import evaluate_gate
    from zicato.tournament.runner import _weights_spec

    weights = ScoringWeights(
        promote_margin=0.02,
        pass_rate_monotonicity=True,
        pass_rate_monotonicity_scope="aggregate",
    )

    # Serialise → JSON → deserialise: exactly what the worker subprocess sees.
    spec = _weights_spec(weights)
    assert spec["pass_rate_monotonicity_scope"] == "aggregate"
    round_tripped = _weights_from_args({"weights": json.loads(json.dumps(spec))})
    assert round_tripped.pass_rate_monotonicity_scope == "aggregate"
    assert round_tripped.pass_rate_monotonicity is True

    # The scope must actually drive the gate after the round-trip. A net-
    # neutral pass-rate challenger that reshuffled which entries pass + clears
    # the scalar margin promotes under aggregate; the default per_entry would
    # reject it (the silent-downgrade bug this guards).
    parent = {
        "scalar": 9.17,
        "pass_rate": 4 / 9,
        "per_entry": {f"E{i}": {"pass_fail": i <= 4} for i in range(1, 10)},
    }
    child = {
        "scalar": 5.23,  # ~43% loss reduction, well past promote_margin
        "pass_rate": 4 / 9,
        "per_entry": {f"E{i}": {"pass_fail": i >= 6} for i in range(1, 10)},
    }
    assert evaluate_gate(parent, child, round_tripped).decision == "promoted"

    # Counterfactual: had the scope been dropped to the default per_entry
    # (the bug), the worker-side gate would reject the same challenger.
    dropped = _weights_from_args(
        {"weights": {k: v for k, v in spec.items() if k != "pass_rate_monotonicity_scope"}}
    )
    assert dropped.pass_rate_monotonicity_scope == "per_entry"
    assert evaluate_gate(parent, child, dropped).decision == "rejected"


def test_unknown_monotonicity_scope_token_coerces_to_default() -> None:
    """A malformed scope token in the args file coerces to the default rather
    than desyncing the worker's gate-view (defensive deserialise, issue #17)."""
    from zicato._tournament_worker import _weights_from_args

    rebuilt = _weights_from_args({"weights": {"pass_rate_monotonicity_scope": "bogus"}})
    assert rebuilt.pass_rate_monotonicity_scope == "per_entry"


def test_drift_kind_aggregation_survives_worker_serialize_deserialize() -> None:
    """``drift_kind_aggregation`` survives the WORKER transport (issue #19 ph 2).

    Seam 1 (the drift reducer) runs INSIDE the killable worker subprocess, so
    if the transform config does not cross the serialize→JSON→deserialize
    boundary the worker scores drift with neutral linear defaults while the
    orchestrator believes it is transformed — the same silent-downgrade class
    as the ``per_judge_weights`` / ``pass_rate_monotonicity_scope`` traps. This
    walks the exact transport — ``_weights_spec`` → JSON → ``_weights_from_args``
    — then DRIVES real drift scoring (``compute_drift_loss``, the consumer the
    worker invokes via ``reduce_loss``) with the reconstructed weights.

    Hand-check: a ``looping_reasoning`` warning-severity drift of count 4 under
    a ``harmonic`` aggregation contributes ``warning_sev × kind_weight ×
    (1 + 1/2 + 1/3 + 1/4)`` — STRICTLY less than the linear count-4 builtin.
    """
    import math

    from zicato._tournament_worker import _weights_from_args
    from zicato.core import DriftCount
    from zicato.telemetry.reducer import compute_drift_loss
    from zicato.tournament.runner import _weights_spec

    weights = ScoringWeights(
        severity_weights={"info": 1.0, "warning": 3.0, "critical": 10.0},
        per_kind_weights={"looping_reasoning": 2.0},
        pass_transform={"op": "pow", "exponent": 2.0},
        drift_kind_aggregation={"looping_reasoning": {"op": "harmonic"}},
    )

    # Serialise → JSON → deserialise: exactly what the worker subprocess sees.
    spec = _weights_spec(weights)
    assert spec["drift_kind_aggregation"] == {"looping_reasoning": {"op": "harmonic"}}
    assert spec["pass_transform"] == {"op": "pow", "exponent": 2.0}
    round_tripped = _weights_from_args({"weights": json.loads(json.dumps(spec))})
    assert round_tripped.drift_kind_aggregation == {"looping_reasoning": {"op": "harmonic"}}
    assert round_tripped.pass_transform == {"op": "pow", "exponent": 2.0}

    drift = (DriftCount(kind="looping_reasoning", severity="warning", count=4),)
    weighted = compute_drift_loss(drift, plan_revisions=0, weights=round_tripped)
    harmonic4 = 1.0 + 1.0 / 2.0 + 1.0 / 3.0 + 1.0 / 4.0
    assert weighted == pytest.approx(3.0 * 2.0 * harmonic4)  # sev × kind × H(4)

    # Counterfactual: the unconfigured worker-side path (aggregation dropped)
    # scores LINEARLY — the exact silent-downgrade this transport guards.
    dropped = _weights_from_args(
        {"weights": {k: v for k, v in spec.items() if k != "drift_kind_aggregation"}}
    )
    assert dropped.drift_kind_aggregation == {}
    linear = compute_drift_loss(drift, plan_revisions=0, weights=dropped)
    assert linear == pytest.approx(3.0 * 2.0 * 4)  # sev × kind × linear count
    assert weighted < linear  # the harmonic shape really survived + reshaped
    assert not math.isnan(weighted)


def test_scoring_weights_unified_serde_round_trips_every_field() -> None:
    """ONE serde for both sides: ``ScoringWeights.to_json`` /
    ``from_json`` (and the runner/worker delegators that wrap them) round-trip
    EVERY field — so adding a field can no longer silently desync the worker
    into scoring under defaults (the documented ``per_judge_weights`` desync).

    Replaces the former hand-aligned ``_weights_spec`` / ``_weights_from_args``
    field-list pair with one ``dataclasses.fields()``-driven serde. This walks
    the FULL transport — ``_weights_spec`` → JSON → ``_weights_from_args`` —
    over a weights instance with a non-default value on every field (reusing
    the contract-completeness guard table), asserting the reconstruction is
    field-identical including ``per_judge_weights``.
    """
    from dataclasses import fields

    from tests.test_contract_serializer_completeness import _all_fields_nondefault
    from zicato._tournament_worker import _weights_from_args
    from zicato.tournament.runner import _weights_spec

    weights = _all_fields_nondefault(ScoringWeights)
    # No field on the instance equals its default — a genuine non-default value
    # everywhere, so a dropped field would be observable.
    base = ScoringWeights()
    for f in fields(ScoringWeights):
        assert getattr(weights, f.name) != getattr(base, f.name)

    # The exact worker transport: runner serialises, JSON round-trips (the args
    # file is JSON), worker deserialises.
    spec = _weights_spec(weights)
    round_tripped = _weights_from_args({"weights": json.loads(json.dumps(spec))})

    # Field-identical — the single serde dropped nothing, including the
    # historically-desynced fields.
    assert round_tripped == weights
    assert round_tripped.per_judge_weights == weights.per_judge_weights
    assert round_tripped.per_judge_weights  # non-empty, a real per-judge map
    assert round_tripped.default_judge_weight == weights.default_judge_weight

    # The direct method form is the same single serde.
    assert ScoringWeights.from_json(weights.to_json()) == weights

    # Tolerance: a partial / absent payload falls back to defaults per field,
    # so a stub-adapter test that omits the weights block still gets a usable
    # default-weighted instance (back-compat with the old reader).
    assert _weights_from_args({}) == ScoringWeights()
    assert ScoringWeights.from_json(None) == ScoringWeights()
    assert ScoringWeights.from_json({"per_judge_weights": {"q": 5.0}}).per_judge_weights == {
        "q": 5.0
    }


def test_worker_stamps_its_own_pid_into_active_runs(tmp_path: Path) -> None:
    """The active_runs file the worker writes carries the WORKER's pid.

    The worker removes the file on a clean exit, so we capture it mid-run
    by giving the worker a sleeping session and reading the file before
    we kill the worker.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry(budget_s=3600)

    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    _write_args_file(
        args_path,
        workspace=workspace,
        generation=generation,
        entry=entry,
        result_path=result_path,
        adapter_factory="tests._subprocess_worker_support:make_sleeping_adapter",
    )

    run_id = f"{generation.id}--{entry.id}"
    run_path = active_run_path(workspace, run_id)

    # Spawn with a fresh session/process-group, exactly as the runner does
    # (``start_new_session=True``), so the worker leads its OWN group and the
    # pgid it records is the worker's group, not the test runner's.
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "zicato._tournament_worker", str(args_path)],
        env=_worker_env(),
        start_new_session=True,
    )
    try:
        # Wait for the worker to write its active_runs file.
        deadline = time.monotonic() + 15.0
        while not run_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert run_path.exists(), "worker never wrote its active_runs file"

        record = ActiveRun.from_dict(json.loads(run_path.read_text(encoding="utf-8")))
        # The key L3 invariant: the pid is the WORKER's own pid, not the
        # parent test process's.
        assert record.pid == proc.pid
        assert record.pid != os.getpid()
        assert record.run_id == run_id
        assert record.entry_id == entry.id
        assert record.deadline  # deadline = started_at + budget
        # Containment metadata for the supervisor: the worker stamps its own
        # process-group id (it leads its group, so pgid == pid here) and the
        # ephemeral snapshot directory it was mounted on.
        assert record.pgid == os.getpgid(proc.pid)
        assert record.pgid == proc.pid  # the worker is its own group leader
        assert record.snapshot_path == str(generation.snapshot_root)
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_run_single_spawns_worker_in_new_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The runner spawns the worker with ``start_new_session=True``.

    This is the producer side of the supervisor's group-containment: the
    worker must lead its own session/process-group so its recorded ``pgid``
    can be group-killed (worker + grandchildren), not just the worker pid.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry()

    captured: dict[str, object] = {}
    real_create = asyncio.create_subprocess_exec

    async def _spy_create(*args: object, **kwargs: object) -> object:
        captured["start_new_session"] = kwargs.get("start_new_session")
        captured["env"] = kwargs.get("env")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_create)

    loss = asyncio.run(
        _run_single(
            adapter=StubAdapter(),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            config=_config(workspace),
            workspace_root=workspace,
            epoch_id="e0",
            side="parent",
        )
    )

    assert isinstance(loss, LossProfile)
    assert captured["start_new_session"] is True
    assert captured["env"] is None


def test_full_tournament_persists_charge_before_real_holdout_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The subprocess launch boundary observes the holdout charge on disk."""
    from zicato.board.split import HOLDOUT_TAG
    from zicato.telemetry.reducer import write_loss_profile

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    parent = make_generation(workspace, "v0")
    child = make_generation(workspace, "v1")
    board = [
        replace(
            _entry("train"),
            expectation=Expectation(kind=ExpectationKind.REGEX, spec=".*"),
        ),
        BoardEntry(
            id="holdout",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="hello",
            expectation=Expectation(kind=ExpectationKind.REGEX, spec=".*"),
            tags=(HOLDOUT_TAG,),
        ),
    ]
    for entry in board:
        write_loss_profile(
            LossProfile(
                run_id=run_id_for_unit("v0", entry.id),
                entry_id=entry.id,
                generation_id="v0",
                epoch_id="e0",
                drift_counts=(DriftCount(kind="off_topic", severity="info", count=2),),
                plan_revisions=0,
                task_failure_ratio=0.0,
                runtime_ms=1,
                wall_clock_budget_exceeded=False,
                expectation_result=ExpectationResult(kind="predicate", passed=True),
                drift_loss=2.0,
                pass_fail=True,
            ),
            loss_profile_path(workspace, "e0", "v0", entry.id),
        )

    launches: list[tuple[str, int | None]] = []
    real_create = asyncio.create_subprocess_exec

    async def observed_create(*argv: object, **kwargs: object) -> object:
        args_path = Path(str(argv[3]))
        args = json.loads(args_path.read_text(encoding="utf-8"))
        entry_id = str(args["entry"]["id"])
        state_path = ladder_state_path(workspace, "e0")
        remaining = None
        if state_path.exists():
            remaining = int(json.loads(state_path.read_text(encoding="utf-8"))["budget_remaining"])
        launches.append((entry_id, remaining))
        return await real_create(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observed_create)
    weights = ScoringWeights(
        promote_margin=0.1,
        overfitting=OverfittingConfig(ladder=LadderConfig(budget=1)),
    )
    result = asyncio.run(
        run_tournament(
            adapter=CompletingAdapter(),
            parent_gen=parent,
            child_gen=child,
            board=board,
            weights=weights,
            config=_config(workspace),
            workspace_root=workspace,
            epoch_id="e0",
        )
    )

    assert launches == [("train", None), ("holdout", 0)]
    assert result.holdout is not None
    assert result.holdout["holdout_consulted"] is True
    assert result.holdout["ladder_query_reserved"] is True
    assert result.holdout["ladder_budget_before_query"] == 1
    assert result.holdout["ladder_budget_remaining"] == 0


def test_run_single_scrubs_worker_env_when_opted_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``scrub_worker_env=True`` spawns the worker with a minimal explicit env.

    The scrubbed env must (a) be an explicit dict, not full inheritance, and
    (b) drop a credential variable that no model role named, while keeping the
    process-essential PATH. This is the producer side of denying a mutated
    worker read-access to every credential in the orchestrator's process env.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry()

    # A stray secret in the orchestrator's env that no model role references.
    monkeypatch.setenv("LEAKY_UNRELATED_SECRET", "sk-should-not-cross")
    monkeypatch.setenv("PATH", "/usr/bin")

    config = replace(_config(workspace), scrub_worker_env=True)

    captured: dict[str, object] = {}
    real_create = asyncio.create_subprocess_exec

    async def _spy_create(*args: object, **kwargs: object) -> object:
        captured["env"] = kwargs.get("env")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_create)

    loss = asyncio.run(
        _run_single(
            adapter=StubAdapter(),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            config=config,
            workspace_root=workspace,
            epoch_id="e0",
            side="parent",
        )
    )

    assert isinstance(loss, LossProfile)
    env = captured["env"]
    assert isinstance(env, dict)  # explicit env, not full inheritance
    assert env.get("PATH") == "/usr/bin"  # essential floor preserved
    assert "LEAKY_UNRELATED_SECRET" not in env  # the scrub denied it


def _spawn_worker_blocking(args_path: Path) -> subprocess.CompletedProcess[bytes]:
    """Spawn the worker and block until it exits; return the finished proc."""
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "zicato._tournament_worker", str(args_path)],
        env=_worker_env(),
        timeout=60,
        check=False,
    )


# ---------------------------------------------------------------------------
# 2. Parent-side budget — SIGTERM -> SIGKILL escalation
# ---------------------------------------------------------------------------


def test_parent_kills_worker_that_blocks_past_budget_plus_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worker wedged past budget+GRACE is reaped; _run_single returns an
    aborted LossProfile and never raises.

    The single SIGTERM→grace→SIGKILL escalator lives in the supervisor:
    the parent writes a kill-request marker and waits for the supervisor to
    reap the worker. No supervisor is attached in this test, so the
    parent's LAST-RESORT fallback escalation fires after the config's
    ``supervisor_kill_wait_s`` window — shrunk here so the fallback is
    exercised fast.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    # Tiny budget; the SleepingAdapter wedges the worker's event loop with
    # a blocking sleep so its OWN cooperative budget cannot fire.
    entry = _entry(budget_s=1)

    # Shrink the parent's grace margins so the test is fast. With no
    # supervisor present, the parent waits supervisor_kill_wait_s before its
    # last-resort escalation — shrink that too so the fallback fires quickly.
    monkeypatch.setattr(runner_mod, "_PARENT_BUDGET_GRACE_S", 0.3)
    monkeypatch.setattr(runner_mod, "_SIGTERM_TO_SIGKILL_GRACE_S", 0.3)

    started = time.monotonic()
    loss = asyncio.run(
        _run_single(
            adapter=SleepingAdapter(),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            config=_config(workspace, supervisor_kill_wait_s=0.5),
            workspace_root=workspace,
            epoch_id="e0",
            side="parent",
        )
    )
    elapsed = time.monotonic() - started

    # Aborted, worst-case loss profile so the tournament can still aggregate.
    assert isinstance(loss, LossProfile)
    assert loss.wall_clock_budget_exceeded is True
    assert loss.entry_id == entry.id
    # The synthesised profile states the facts of the abort; the failure
    # channel turns them into the loss.
    assert loss.not_completed is True
    assert loss.task_failure_ratio == 1.0
    # The parent stamped the abort cause so loop-health can tell its OWN kill
    # of a wedged worker apart from a budget exhaustion or a supervisor kill.
    assert loss.abort_cause == "parent_kill"
    # The parent reaped at budget(1) + grace(0.3) + supervisor-wait(0.5) +
    # escalation(0.3) ≈ 2.1s, not 3600s.
    assert elapsed < 30.0
    # The parent cleaned up the worker's active_runs file...
    assert not active_run_path(workspace, f"{generation.id}--{entry.id}").exists()
    # ...and its own kill-request marker (a recycled run id must not inherit
    # a stale request the supervisor would act on).
    from zicato.runtime.paths import kill_request_path

    assert not kill_request_path(workspace, f"{generation.id}--{entry.id}").exists()
    # The per-run ephemeral snapshot working copy is discarded even on
    # the abort path — _run_single's finally block runs unconditionally.
    run_id = f"{generation.id}--{entry.id}"
    leaked = list(
        Path(tempfile.gettempdir()).glob(f"{runner_mod._EPHEMERAL_SNAPSHOT_PREFIX}{run_id}-*")
    )
    assert not leaked, f"ephemeral snapshot not cleaned up after abort: {leaked}"


def test_tournament_continues_after_a_budget_killed_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One budget-killed entry does NOT abort the whole tournament — a later
    entry still runs to a clean LossProfile.

    Mirrors what ``_run_generation`` does (one ``_run_single`` per entry,
    sequentially): the first entry wedges and is killed, the second runs
    the fast stub. The wedged run must not raise — if it did, the loop
    would never reach the second entry.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)

    monkeypatch.setattr(runner_mod, "_PARENT_BUDGET_GRACE_S", 0.3)
    monkeypatch.setattr(runner_mod, "_SIGTERM_TO_SIGKILL_GRACE_S", 0.3)

    losses: dict[str, LossProfile] = {}
    for entry, adapter in (
        (_entry("entry_wedged", budget_s=1), SleepingAdapter()),
        (_entry("entry_ok", budget_s=60), StubAdapter()),
    ):
        losses[entry.id] = asyncio.run(
            _run_single(
                adapter=adapter,
                generation=generation,
                entry=entry,
                weights=ScoringWeights(),
                config=_config(workspace, supervisor_kill_wait_s=0.5),
                workspace_root=workspace,
                epoch_id="e0",
                side="parent",
            )
        )

    # The wedged entry aborted; the next entry still produced a profile.
    assert losses["entry_wedged"].wall_clock_budget_exceeded is True
    assert losses["entry_ok"].entry_id == "entry_ok"


def test_parent_delegates_kill_to_supervisor_via_request_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On an over-budget worker the parent REQUESTS a kill (control marker) and
    waits for the supervisor to reap it — it does NOT self-escalate.

    This is the §3 consolidation: the single SIGTERM→grace→SIGKILL
    escalator lives in the supervisor. Here a fake supervisor task watches
    for the ``kill_requests/{run_id}`` marker and SIGKILLs the worker,
    standing in for the Rust ``runs_loop``. The parent's last-resort
    ``_terminate_worker`` must NOT fire, proving the kill went through the
    request channel rather than the parent racing the supervisor.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry(budget_s=1)
    run_id = f"{generation.id}--{entry.id}"

    monkeypatch.setattr(runner_mod, "_PARENT_BUDGET_GRACE_S", 0.3)

    # Trip a flag if the parent's last-resort escalation ever fires.
    fallback_fired = {"value": False}
    real_terminate = runner_mod._terminate_worker

    async def _spy_terminate(proc: object) -> None:
        fallback_fired["value"] = True
        await real_terminate(proc)

    monkeypatch.setattr(runner_mod, "_terminate_worker", _spy_terminate)

    from zicato.runtime.paths import kill_request_path
    from zicato.runtime.state import list_active_runs

    # Set when the fake supervisor resolves the pid and reaps the worker, so a
    # failure names which side broke rather than only that the fallback fired.
    reaped_by_supervisor = {"value": False}

    async def _fake_supervisor() -> None:
        """Stand in for the Rust runs_loop: on a kill-request, SIGKILL the pid.

        Polls until it has actually reaped, the way the runs_loop does. The
        two events it joins are written by DIFFERENT processes — the parent
        writes the kill-request marker on its own budget timer, while the
        WORKER writes the ``active_runs`` record the pid comes from — so the
        marker appears first whenever worker start-up outlasts budget+grace,
        which on a loaded runner it does. A supervisor that looked once and
        gave up turned that ordering into a flake: nothing reaped the worker,
        and the parent's last-resort escalation fired on a delegation that was
        working exactly as designed (issue #260).
        """
        marker = kill_request_path(workspace, run_id)
        while True:
            await asyncio.sleep(0.02)
            if not marker.exists():
                continue
            # Resolve the worker pid from its active_runs record and kill it,
            # exactly as the supervisor's escalator does.
            for run in list_active_runs(workspace):
                if run.run_id == run_id and run.pid:
                    try:
                        os.kill(run.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # already gone; the reap is accounted for either way
                    reaped_by_supervisor["value"] = True
                    return

    async def _drive() -> LossProfile:
        sup = asyncio.create_task(_fake_supervisor())
        try:
            return await _run_single(
                adapter=SleepingAdapter(),
                generation=generation,
                entry=entry,
                weights=ScoringWeights(),
                # Load-tolerant supervisor-wait: the parent returns the moment
                # the worker dies, so a healthy delegation never spends this
                # window — only a genuine regression waits it out. It has to
                # cover the worker's whole start-up, since the pid the
                # supervisor kills does not exist until the worker registers.
                config=_config(workspace, supervisor_kill_wait_s=60.0),
                workspace_root=workspace,
                epoch_id="e0",
                side="parent",
            )
        finally:
            sup.cancel()

    loss = asyncio.run(_drive())

    assert isinstance(loss, LossProfile)
    assert loss.wall_clock_budget_exceeded is True
    # The supervisor reaped the worker, so the parent's last-resort
    # escalation never ran — no parent↔supervisor race over the pid.
    assert reaped_by_supervisor["value"] is True
    assert fallback_fired["value"] is False
    # The kill-request marker was cleaned up on the run's finally block.
    assert not kill_request_path(workspace, run_id).exists()


# ---------------------------------------------------------------------------
# 3. Supervisor-kill path — worker gone, no result file
# ---------------------------------------------------------------------------


def test_run_single_handles_externally_killed_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worker SIGKILLed externally (the supervisor) mid-run leaves no result
    file; _run_single records an aborted run with no exception."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry(budget_s=3600)  # long budget so neither side's timeout fires

    run_id = f"{generation.id}--{entry.id}"

    # Patch create_subprocess_exec so that as soon as the worker has
    # written its active_runs file we SIGKILL it — simulating exactly
    # what the independent supervisor watchdog does to a wedged run.
    real_create = asyncio.create_subprocess_exec

    async def killing_create(*args: object, **kwargs: object) -> object:
        proc = await real_create(*args, **kwargs)
        run_path = active_run_path(workspace, run_id)

        async def _kill_when_started() -> None:
            deadline = time.monotonic() + 20.0
            while not run_path.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            # Simulate the supervisor's SIGKILL on a deadline-exceeded run.
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        asyncio.ensure_future(_kill_when_started())  # noqa: RUF006
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", killing_create)

    loss = asyncio.run(
        _run_single(
            adapter=SleepingAdapter(),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            config=_config(workspace),
            workspace_root=workspace,
            epoch_id="e0",
            side="parent",
        )
    )

    # Worker gone + missing result file == a normal aborted run, NOT a
    # crash. The tournament continues.
    assert isinstance(loss, LossProfile)
    assert loss.wall_clock_budget_exceeded is True
    assert loss.entry_id == entry.id
    # A supervisor SIGKILL leaves the worker gone with no result file —
    # distinguished from a parent kill by the abort cause.
    assert loss.abort_cause == "gone_no_result"
    # The parent cleaned up the active_runs file the killed worker left.
    assert not active_run_path(workspace, run_id).exists()


# ---------------------------------------------------------------------------
# 4. Worker ignores SIGTERM -> parent escalates to SIGKILL
# ---------------------------------------------------------------------------


def test_parent_escalates_to_sigkill_when_worker_ignores_sigterm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worker that installs SIG_IGN for SIGTERM survives the parent's
    SIGTERM; the parent escalates to SIGKILL after the grace and _run_single
    still returns an aborted LossProfile."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry(budget_s=1)

    monkeypatch.setattr(runner_mod, "_PARENT_BUDGET_GRACE_S", 0.3)
    monkeypatch.setattr(runner_mod, "_SIGTERM_TO_SIGKILL_GRACE_S", 0.3)

    started = time.monotonic()
    loss = asyncio.run(
        _run_single(
            # ignore_sigterm=True -> the worker installs SIG_IGN for
            # SIGTERM inside the subprocess, so only SIGKILL stops it.
            adapter=SleepingAdapter(ignore_sigterm=True),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            # No supervisor is attached, so the parent waits the full
            # supervisor_kill_wait_s window before its last-resort
            # SIGTERM->SIGKILL escalation — shrunk so the escalation
            # semantics (not the dead wait) dominate the test.
            config=_config(workspace, supervisor_kill_wait_s=0.3),
            workspace_root=workspace,
            epoch_id="e0",
            side="parent",
        )
    )
    elapsed = time.monotonic() - started

    assert isinstance(loss, LossProfile)
    assert loss.wall_clock_budget_exceeded is True
    # budget(1) + grace(0.3) + supervisor wait(0.3) + sigterm->sigkill
    # grace(0.3) ≈ 1.9s; the bound leaves headroom for a loaded machine
    # while still catching a reintroduced multi-second dead wait.
    assert elapsed < 10.0
    assert not active_run_path(workspace, f"{generation.id}--{entry.id}").exists()


# ---------------------------------------------------------------------------
# 5. Worker self-aborts on its own cooperative budget (clean exit)
# ---------------------------------------------------------------------------


def test_worker_cooperative_budget_produces_clean_aborted_result(
    tmp_path: Path,
) -> None:
    """When the worker's OWN asyncio.wait_for budget fires, the worker still
    exits 0 with a result file flagged aborted — the parent reads it back as a
    budget-exceeded LossProfile rather than treating it as a kill."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    # 1s budget; the cooperative-budget adapter sleeps via asyncio.sleep
    # (cancellable), so the worker's own wait_for cancels it cleanly.
    entry = _entry(budget_s=1)

    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    _write_args_file(
        args_path,
        workspace=workspace,
        generation=generation,
        entry=entry,
        result_path=result_path,
        adapter_factory="tests._subprocess_worker_support:make_cooperative_adapter",
    )

    proc = _spawn_worker_blocking(args_path)
    assert proc.returncode == 0, "a self-aborted worker still exits cleanly"

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["aborted"] is True
    assert result["abort_reason"] == "wall_clock_budget"

    # The loss.json the worker wrote carries the budget-exceeded flag.
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415

    loss = read_loss_profile(Path(result["loss_profile_path"]))
    assert loss.wall_clock_budget_exceeded is True
    # A genuine cooperative-budget exhaustion is the ONE cache-eligible abort
    # cause; the worker stamps it so the cache layer reuses it (re-running
    # re-hits the same budget) rather than treating it as an infra abort.
    from zicato.core import BUDGET_ABORT_CAUSE, is_infra_abort_cause  # noqa: PLC0415

    assert loss.abort_cause == BUDGET_ABORT_CAUSE
    assert is_infra_abort_cause(loss.abort_cause) is False
    # The budget path carries BOTH provenance fields: the cause the cache
    # reads, and the reason the run did not complete.
    assert loss.not_completed_reason == "wall_clock_budget"


# ---------------------------------------------------------------------------
# 6. Snapshot isolation — a run that writes near its own code does NOT
#    pollute the canonical generation snapshot.
# ---------------------------------------------------------------------------


def _hash_tree(root: Path) -> str:
    """Return a byte-level digest of a directory tree (paths + file bytes).

    Walks ``root`` deterministically (sorted) and folds every relative
    path and every file's bytes into a single SHA-256 digest. Two trees
    with the same digest are byte-identical in both structure and
    content — the assertion the snapshot-pollution test needs.
    """
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        if path.is_file():
            digest.update(b"\0F\0")
            digest.update(path.read_bytes())
        else:
            digest.update(b"\0D\0")
    return digest.hexdigest()


def _canonical_snapshot(workspace: Path, epoch_id: str, gen_id: str) -> Path:
    """Return (and create) a canonical ``generations/{id}/snapshot/`` dir.

    Lays the snapshot down at exactly the workspace path
    :class:`~zicato.epoch.genstore.DirectoryGenerationStore` resolves —
    ``epochs/{epoch}/generations/{gen}/snapshot/`` — with a tiny stub
    ``agent/agent.py`` so the tree is code-only to begin with.
    """
    snap = workspace / "epochs" / epoch_id / "generations" / gen_id / "snapshot"
    (snap / "agent").mkdir(parents=True, exist_ok=True)
    (snap / "agent" / "agent.py").write_text("# stub agent module — code only\n", encoding="utf-8")
    return snap


def test_run_does_not_pollute_canonical_generation_snapshot(tmp_path: Path) -> None:
    """A board-entry run whose agent writes ``output/`` near its own code does
    NOT mutate the canonical generation snapshot.

    The :class:`SnapshotWritingAdapter`'s session writes a runtime
    artifact under the snapshot root it is loaded from — exactly what the
    target-1 presentation agent's ``write_webpage`` tool does. ``_run_single``
    must execute that run against a per-run EPHEMERAL working copy, so:

    * the canonical ``generations/{id}/snapshot/`` tree is byte-identical
      before and after the run (zero pollution), and
    * a subsequent ``derive_generation`` copies a code-only snapshot
      forward — the runtime ``output/`` directory never appears in the
      child generation.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    epoch_id = "e0"

    snap = _canonical_snapshot(workspace, epoch_id, "v0")
    generation = Generation(
        id="v0",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=snap,
        created_at="2026-05-15T00:00:00Z",
    )
    entry = _entry(budget_s=60)

    # Digest the canonical snapshot BEFORE the run.
    before = _hash_tree(snap)

    loss = asyncio.run(
        _run_single(
            adapter=SnapshotWritingAdapter(),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            config=_config(workspace),
            workspace_root=workspace,
            epoch_id=epoch_id,
            side="parent",
        )
    )
    # The run itself completed and produced a loss profile.
    assert isinstance(loss, LossProfile)
    assert loss.entry_id == entry.id

    # Zero pollution: the canonical snapshot is byte-identical, and the
    # runtime ``output/`` directory the agent wrote is NOT in it.
    assert _hash_tree(snap) == before, "canonical snapshot was mutated by the run"
    assert not (
        snap / "agent" / "output"
    ).exists(), "runtime output/ leaked into the canonical generation snapshot"

    # No ephemeral working copy for THIS run was left behind in the
    # system temp dir — _run_single's finally block discards it. The
    # glob is scoped to this run's run_id so a concurrent test's temp
    # dir cannot cause a false failure.
    run_id = f"{generation.id}--{entry.id}"
    leaked = list(
        Path(tempfile.gettempdir()).glob(f"{runner_mod._EPHEMERAL_SNAPSHOT_PREFIX}{run_id}-*")
    )
    assert not leaked, f"ephemeral snapshot working copies were not cleaned up: {leaked}"

    # A subsequent derive_generation carries a code-only snapshot forward.
    from zicato.epoch.genstore import DirectoryGenerationStore  # noqa: PLC0415

    store = DirectoryGenerationStore(workspace)
    child_root = store.derive_generation(epoch_id, "v0", "v1", patches=[])
    assert (child_root / "agent" / "agent.py").is_file()
    assert not (
        child_root / "agent" / "output"
    ).exists(), "derive_generation carried runtime output/ forward into the child generation"
