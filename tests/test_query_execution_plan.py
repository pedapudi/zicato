"""The served execution plan — the epoch's loop as one tree.

``build_execution_plan`` joins the round logs (the stage/step spine), the
per-unit loss files (the work units), the frozen board, lineage, and the
experiment outcomes. These tests drive it over fixture workspaces that
mirror the shapes a real run leaves behind: a complete round, a round
still open, a round that died in validation, a round that never released
the holdout, a run directory with no loss file, and a round log whose
tail was torn by a crash.

The load-bearing ones are the two audits. :func:`test_unit_count_equals_the
_loss_files_on_disk` pins the work units to the scoring slots on disk, and
:func:`test_every_loss_file_on_disk_is_named_once_by_the_plan` widens that
to the whole epoch: units and measurement-band draws together must name
every non-attempt loss file, so a band the builder forgets renders as a
missing file rather than as a plausible, incomplete tree. The plan's whole
promise is that a node the reader opens describes work that happened, so a
replicate silently dropped — or one invented from a configured count — has
to fail a test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from zicato.core.loss import LossProfile
from zicato.core.workspace import WorkspaceLayout, loss_profile_path
from zicato.dashboard.server import create_app
from zicato.epoch.round_log import (
    DecisionRecorded,
    ExperimentMinted,
    GateEvaluated,
    HarnessLoaded,
    HoldoutReleased,
    PatchesApplied,
    ProposalAttempted,
    RoundClosed,
    RoundLog,
    RoundOpened,
    ValidationFailed,
    round_log_path,
)
from zicato.query import WorkspacePaths, build_execution_plan
from zicato.query.contracts import ENDPOINT_PAYLOADS
from zicato.query.execution_plan import PlanNode
from zicato.query.replicate_scores import band_of, measurement_bands
from zicato.telemetry import reducer
from zicato.tournament.unit_cache import unit_result_path

EPOCH = "2026-08-18_plan"
ENTRIES = ("login", "search")
PLAN_ROUTE = "/api/epoch/{epoch_id}/execution-plan"


# ---------------------------------------------------------------------------
# Fixture workspace
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / ".zicato"
    layout = WorkspaceLayout.from_root(root)
    layout.epoch_dir(EPOCH).mkdir(parents=True, exist_ok=True)
    (root / "current_epoch").write_text(EPOCH, encoding="utf-8")
    layout.epoch_config(EPOCH).write_text(
        json.dumps({"id": EPOCH, "created_at": "2026-08-18T00:00:00Z"}), encoding="utf-8"
    )
    layout.board(EPOCH).write_text(
        "\n".join(json.dumps({"id": entry, "input": "go"}) for entry in ENTRIES) + "\n",
        encoding="utf-8",
    )
    return root


def _loss_path(root: Path, generation_id: str, entry_id: str, replicate: int) -> Path:
    base = loss_profile_path(root, EPOCH, generation_id, entry_id)
    return base if replicate == 0 else base.with_name(f"loss.r{replicate}.json")


def _write_loss(
    root: Path,
    generation_id: str,
    entry_id: str,
    *,
    replicate: int = 0,
    path: Path | None = None,
    passes: bool | None = True,
    match_id: str = "rung0_m0",
    timed: bool = True,
    not_completed_reason: str | None = None,
) -> Path:
    """One per-replicate loss profile, exactly as the worker writes it."""
    profile = LossProfile(
        run_id=f"{generation_id}:{entry_id}:r{replicate}",
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id=EPOCH,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1200,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=0.25,
        pass_fail=passes,
        match_id=match_id,
        not_completed_reason=not_completed_reason,
        started_at="2026-08-18T00:01:00Z" if timed else None,
        ended_at="2026-08-18T00:01:01Z" if timed else None,
    )
    target = path if path is not None else _loss_path(root, generation_id, entry_id, replicate)
    reducer.write_loss_profile(profile, target)
    return target


def _write_result(loss_path: Path, *, aborted: bool, abort_reason: str) -> None:
    unit_result_path(loss_path).write_text(
        json.dumps(
            {
                "format_version": 1,
                "run_id": "r",
                "entry_id": "e",
                "final_output": "",
                "transcript": [],
                "runtime_ms": 1200,
                "aborted": aborted,
                "abort_reason": abort_reason,
                "clipped": False,
            }
        ),
        encoding="utf-8",
    )


def _complete_round(root: Path, index: int = 0, *, challenger: str = "v1") -> None:
    log = RoundLog(root, EPOCH, index)
    log.append(RoundOpened(contract_hash="hash-1"))
    log.append(ProposalAttempted(errors=(), slot_index=0))
    log.append(ProposalAttempted(errors=("credential lapse",), slot_index=1))
    log.append(ExperimentMinted(experiment_id="exp-1"))
    log.append(PatchesApplied(generation_id=challenger))
    log.append(
        GateEvaluated(
            rule_fired="",
            decision="promote",
            champion_scalar=0.5,
            challenger_scalar=0.3,
            margin_required=0.01,
        )
    )
    log.append(HoldoutReleased(confirmed=True))
    log.append(DecisionRecorded(decision="promote", provenance={"gate": "margin"}))
    log.append(RoundClosed())


@pytest.fixture
def complete_run(tmp_path: Path) -> Path:
    """A settled epoch: a baseline champion and one closed round with 3 replicates."""
    root = _workspace(tmp_path)
    for entry in ENTRIES:
        _write_loss(root, "v0", entry)
        for replicate in (0, 1, 2):
            _write_loss(root, "v1", entry, replicate=replicate, match_id=f"rung0_m{replicate}")
    _complete_round(root)
    return root


def _plan(root: Path) -> dict[str, Any]:
    return build_execution_plan(WorkspacePaths(root), EPOCH)


def _stage(plan: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(stage for stage in plan["stages"] if stage["id"].endswith(node_id))


def _find(node: dict[str, Any], node_id: str) -> dict[str, Any]:
    """The node with this id anywhere under ``node`` (the tree is small)."""
    if node["id"] == node_id:
        return node
    for child in node["children"]:
        try:
            return _find(child, node_id)
        except KeyError:
            continue
    raise KeyError(node_id)


def _walk(plan: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        out.append(node)
        for child in node["children"]:
            visit(child)

    for stage in plan["stages"]:
        visit(stage)
    return out


def _of_kind(plan: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [node for node in _walk(plan) if node["kind"] == kind]


# ---------------------------------------------------------------------------
# The spine
# ---------------------------------------------------------------------------


def test_complete_run_serves_baseline_then_the_round_and_its_five_steps(
    complete_run: Path,
) -> None:
    plan = _plan(complete_run)

    assert [stage["kind"] for stage in plan["stages"]] == ["baseline", "round"]
    baseline, round_zero = plan["stages"]
    assert baseline["status"] == "done"
    assert round_zero["status"] == "done"
    assert round_zero["outcome"]["contract_hash"] == "hash-1"
    # The steps are the loop's order, not the log's event order.
    assert [child["kind"] for child in round_zero["children"]] == [
        "propose_step",
        "apply_step",
        "run_step",
        "gate_step",
        "decide_step",
    ]
    # The round's span comes from the log's own timestamps.
    assert round_zero["started_at"] is not None
    assert round_zero["ended_at"] >= round_zero["started_at"]


def test_a_candidate_belongs_to_the_round_that_minted_it(complete_run: Path) -> None:
    """The baseline holds the generation no round log claims — and only that one."""
    plan = _plan(complete_run)

    baseline = _stage(plan, "/baseline")
    assert [sweep["coordinates"]["generation_id"] for sweep in baseline["children"]] == ["v0"]
    run_step = _find(_stage(plan, "/round:0"), f"e:{EPOCH}/round:0/run")
    assert [sweep["coordinates"]["generation_id"] for sweep in run_step["children"]] == ["v1"]


def test_proposal_attempts_carry_their_slot_and_their_errors(complete_run: Path) -> None:
    plan = _plan(complete_run)
    propose = _find(_stage(plan, "/round:0"), f"e:{EPOCH}/round:0/propose")

    settled, failed = propose["children"]
    assert (settled["status"], settled["coordinates"]["slot_index"]) == ("done", 0)
    assert (failed["status"], failed["coordinates"]["slot_index"]) == ("failed", 1)
    assert failed["outcome"]["errors"] == ["credential lapse"]
    assert propose["outcome"]["experiment_ids"] == ["exp-1"]


def test_the_gate_names_the_rule_and_the_scalars_it_compared(complete_run: Path) -> None:
    plan = _plan(complete_run)
    gate = _find(_stage(plan, "/round:0"), f"e:{EPOCH}/round:0/gate")

    evaluation = gate["children"][0]
    assert evaluation["outcome"]["decision"] == "promote"
    # A clean promotion fires no REJECTING rule, so the field is empty
    # rather than a rule name the server made up.
    assert evaluation["outcome"]["deciding_rule"] == ""
    assert evaluation["outcome"]["champion_scalar"] == 0.5
    assert evaluation["outcome"]["margin_required"] == 0.01


# ---------------------------------------------------------------------------
# Work units — the per-unit files
# ---------------------------------------------------------------------------


def test_three_replicates_give_three_nodes_with_their_own_match_ids(
    complete_run: Path,
) -> None:
    """The tree's replicate count comes from the files, never from a config."""
    plan = _plan(complete_run)
    sweep = _find(_stage(plan, "/round:0"), f"e:{EPOCH}/round:0/run/v1")

    login = [node for node in sweep["children"] if node["coordinates"]["entry_id"] == "login"]
    assert [node["coordinates"]["replicate"] for node in login] == [0, 1, 2]
    assert [node["coordinates"]["match_id"] for node in login] == [
        "rung0_m0",
        "rung0_m1",
        "rung0_m2",
    ]
    assert {node["id"] for node in login} == {
        f"e:{EPOCH}/round:0/run/v1/login/r{r}" for r in (0, 1, 2)
    }


def _loss_files_on_disk(root: Path) -> set[Path]:
    """Every non-attempt ``loss*.json`` the epoch holds."""
    return {
        path
        for path in (root / "epochs" / EPOCH / "generations").rglob("loss*.json")
        if ".a" not in path.stem[len("loss") :]
    }


def _files_the_plan_names(root: Path) -> set[Path]:
    """The loss file each work unit and each band draw in the plan describes."""
    plan = _plan(root)
    named: set[Path] = set()
    for node in _of_kind(plan, "board_entry_run") + _of_kind(plan, "measurement_draw"):
        where = node["coordinates"]
        named.add(_loss_path(root, where["generation_id"], where["entry_id"], where["replicate"]))
    return named


def test_unit_count_equals_the_loss_files_on_disk(complete_run: Path) -> None:
    """The audit: every scoring slot on disk is one node, and nothing else is.

    A plan that quietly drops a replicate still renders — which is exactly
    why the count is asserted against the filesystem rather than against a
    number the builder also computes.
    """
    plan = _plan(complete_run)
    on_disk = _loss_files_on_disk(complete_run)

    assert len(on_disk) == 8  # 2 entries x (1 baseline + 3 replicates)
    assert len(_of_kind(plan, "board_entry_run")) == len(on_disk)
    assert _files_the_plan_names(complete_run) == on_disk


def test_an_attempt_hangs_under_its_unit_and_is_not_a_work_unit(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    canonical = _write_loss(root, "v0", "login")
    _write_loss(root, "v0", "login", path=canonical.with_name("loss.a1.json"))
    _write_loss(root, "v0", "login", replicate=1)
    _write_loss(root, "v0", "login", path=canonical.with_name("loss.r1.a1.json"))

    plan = _plan(root)

    assert len(_of_kind(plan, "board_entry_run")) == 2
    attempts = _of_kind(plan, "board_entry_attempt")
    assert [node["id"] for node in attempts] == [
        f"e:{EPOCH}/baseline/v0/login/r0/a1",
        f"e:{EPOCH}/baseline/v0/login/r1/a1",
    ]
    # An attempt belongs to the replicate slot its filename names.
    assert attempts[1]["coordinates"]["replicate"] == 1


def test_an_aborted_unit_reads_failed_and_carries_its_reason(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    clean = _write_loss(root, "v0", "login")
    aborted = _write_loss(
        root, "v0", "search", passes=None, not_completed_reason="harness_exception:ValueError"
    )
    _write_result(clean, aborted=False, abort_reason="")
    _write_result(aborted, aborted=True, abort_reason="wall clock exceeded")

    units = {
        node["coordinates"]["entry_id"]: node for node in _of_kind(_plan(root), "board_entry_run")
    }

    assert units["login"]["status"] == "done"
    assert units["search"]["status"] == "failed"
    assert units["search"]["outcome"]["aborted"] is True
    assert units["search"]["outcome"]["abort_reason"] == "wall clock exceeded"
    assert units["search"]["outcome"]["not_completed_reason"] == "harness_exception:ValueError"


def test_a_unit_without_a_recorded_span_reads_partial(tmp_path: Path) -> None:
    """Duration without position: the node says so instead of inventing a time."""
    root = _workspace(tmp_path)
    _write_loss(root, "v0", "login", timed=False)

    unit = _of_kind(_plan(root), "board_entry_run")[0]

    assert (unit["started_at"], unit["ended_at"]) == (None, None)
    assert unit["duration_ms"] == 1200
    assert unit["provenance"] == "partial"
    # Provenance propagates up, so an unopened branch still says so.
    assert _stage(_plan(root), "/baseline")["provenance"] == "partial"


# ---------------------------------------------------------------------------
# Measurement bands — the executions that are not a cell's evidence
# ---------------------------------------------------------------------------


def _bands(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The plan's band steps keyed by band key (ids are unique per stage)."""
    return {node["coordinates"]["band"]: node for node in _of_kind(plan, "measurement_band")}


def test_the_band_ledger_tracks_the_bases_its_owners_stamp(tmp_path: Path) -> None:
    """The bands are the owners' own constants, not numbers copied beside them."""
    from zicato.epoch.preflight import PREFLIGHT_REPLICATE_BASE
    from zicato.epoch.screen import SCREEN_REPLICATE_BASE
    from zicato.reflection.admission import SYNTHESIS_REPLICATE_BASE
    from zicato.reflection.corpus import REFLECTION_REPLICATE_BASE
    from zicato.tournament.calibration import CALIBRATION_REPLICATE_BASE

    starts = {band.key: band.start for band in measurement_bands()}

    assert starts == {
        "calibration": CALIBRATION_REPLICATE_BASE,
        "contract_preflight": PREFLIGHT_REPLICATE_BASE,
        "candidate_screen": SCREEN_REPLICATE_BASE,
        "board_reflection": REFLECTION_REPLICATE_BASE,
        "eval_synthesis_admission": SYNTHESIS_REPLICATE_BASE,
    }
    # Every band an owner claims is disjoint from the evidence a cell reads.
    for band in measurement_bands():
        assert band_of(band.start) is band
        assert band_of(band.stop - 1) is band


def test_every_reserved_band_executed_against_the_champion_gets_its_own_step(
    tmp_path: Path,
) -> None:
    """Calibration, pre-flight, reflection and admission draws all surface."""
    root = _workspace(tmp_path)
    _write_loss(root, "v0", "login")
    for replicate, match_id in (
        (1000, "aa-calibration:0"),
        (1001, "aa-calibration:1"),
        (2000, "contract-preflight:degraded:tool_description"),
        (5000, "reflection:refl-1:r0"),
        (6000, "admission-noise:0"),
    ):
        _write_loss(root, "v0", "login", replicate=replicate, match_id=match_id)

    bands = _bands(_plan(root))

    assert {key: node["outcome"]["draw_count"] for key, node in bands.items()} == {
        "calibration": 2,
        "contract_preflight": 1,
        "board_reflection": 1,
        "eval_synthesis_admission": 1,
    }
    assert bands["calibration"]["id"] == f"e:{EPOCH}/baseline/band:calibration"
    assert bands["calibration"]["outcome"]["replicate_range"] == [1000, 1999]
    assert bands["calibration"]["outcome"]["generation_ids"] == ["v0"]
    # A band draw is never a work unit: the cell still holds exactly one.
    assert len(_of_kind(_plan(root), "board_entry_run")) == 1
    draw = _find(_stage(_plan(root), "/baseline"), f"{bands['calibration']['id']}/v0/login/r1000")
    assert draw["coordinates"]["match_id"] == "aa-calibration:0"
    assert draw["status"] == "done"


def test_the_preflight_band_says_its_probes_are_degraded(tmp_path: Path) -> None:
    """A probe failure must never read as champion behaviour — parent or not."""
    root = _workspace(tmp_path)
    _write_loss(root, "v0", "login", replicate=2000, passes=False)

    band = _bands(_plan(root))["contract_preflight"]

    assert "degraded" in band["label"].lower()
    assert "DELIBERATELY DEGRADED" in band["purpose"]
    # The caveat rides on the draw too: a client may render it standalone.
    assert "DELIBERATELY DEGRADED" in band["children"][0]["purpose"]
    assert band["children"][0]["outcome"]["pass_fail"] is False


def test_a_band_with_no_draws_yields_no_step(complete_run: Path) -> None:
    """An empty band step would assert a measurement nothing on disk records."""
    assert _bands(_plan(complete_run)) == {}


def test_an_unclaimed_replicate_index_is_visible_as_unclaimed(tmp_path: Path) -> None:
    """The ledger is an allow-list: an index no owner claims is shown, not admitted."""
    root = _workspace(tmp_path)
    _write_loss(root, "v0", "login", replicate=7000, match_id="")

    bands = _bands(_plan(root))

    assert set(bands) == {"unclaimed"}
    assert bands["unclaimed"]["outcome"]["draw_count"] == 1
    assert "never counted" in bands["unclaimed"]["purpose"]
    # Never a work unit, and never inside a claimed band.
    assert _of_kind(_plan(root), "board_entry_run") == []


def test_a_band_states_that_the_files_record_no_round(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _write_loss(root, "v0", "login", replicate=1000)

    band = _bands(_plan(root))["calibration"]

    assert band["provenance"] == "partial"
    assert "record no round" in band["outcome"]["attribution"]


def test_a_leftover_screen_snapshot_hangs_under_the_round_its_name_states(
    tmp_path: Path,
) -> None:
    """The ephemeral screen id is the anchor: it names the round it served."""
    root = _workspace(tmp_path)
    for entry in ENTRIES:
        _write_loss(root, "v0", entry)
        _write_loss(root, "v1", entry)
    _complete_round(root)
    _write_loss(root, "v0-screen-r0c1", "login", replicate=3000, match_id="candidate-screen:r0:c1")

    plan = _plan(root)
    band = _bands(plan)["candidate_screen"]

    assert band["id"] == f"e:{EPOCH}/round:0/band:candidate_screen"
    assert band["provenance"] == "exact"
    assert band["outcome"]["attribution"] == "the generation id names the round these draws served"
    # It never ran the board, so it gets no board sweep and no baseline home.
    assert _stage(plan, "/baseline")["outcome"]["generation_ids"] == ["v0"]
    assert [node["coordinates"]["generation_id"] for node in _of_kind(plan, "board_sweep")] == [
        "v0",
        "v1",
    ]
    # The round's five loop steps are unchanged; the band follows them.
    assert [child["kind"] for child in _stage(plan, "/round:0")["children"]] == [
        "propose_step",
        "apply_step",
        "run_step",
        "gate_step",
        "decide_step",
        "measurement_band",
    ]
    assert _stage(plan, "/round:0")["progress"] == {"done": 5, "total": 5}


def test_a_screen_snapshot_naming_a_round_with_no_stage_falls_back_to_the_baseline(
    tmp_path: Path,
) -> None:
    """No stage to move to means no stated home — the band says so."""
    root = _workspace(tmp_path)
    _write_loss(root, "v0-screen-r9c0", "login", replicate=3000)

    band = _bands(_plan(root))["candidate_screen"]

    assert band["id"] == f"e:{EPOCH}/baseline/band:candidate_screen"
    assert band["provenance"] == "partial"
    assert "record no round" in band["outcome"]["attribution"]


def test_every_loss_file_on_disk_is_named_once_by_the_plan(tmp_path: Path) -> None:
    """The extended audit: units and band draws together cover the whole epoch.

    The evidence ranges and the bands partition the replicate namespace, so a
    band the builder forgets to render is a file no node names — which this
    fails on rather than rendering a plausible, incomplete tree.
    """
    root = _workspace(tmp_path)
    for entry in ENTRIES:
        _write_loss(root, "v0", entry)
        _write_loss(root, "v0", entry, replicate=1000)
        _write_loss(root, "v0", entry, replicate=2000)
        _write_loss(root, "v0", entry, replicate=5000)
        _write_loss(root, "v0", entry, replicate=7000)
        _write_loss(root, "v1", entry)
        _write_loss(root, "v1", entry, replicate=4000)
        # A superseded attempt is provenance, never a draw of either kind.
        _write_loss(
            root, "v1", entry, path=_loss_path(root, "v1", entry, 0).with_name("loss.a1.json")
        )
    _complete_round(root)
    _write_loss(root, "v0-screen-r0c1", "login", replicate=3000)

    on_disk = _loss_files_on_disk(root)

    assert len(on_disk) == 15  # 2 entries x 7 slots, plus the one screen draw
    assert _files_the_plan_names(root) == on_disk


# ---------------------------------------------------------------------------
# The shapes a run actually leaves behind
# ---------------------------------------------------------------------------


def test_a_round_still_open_reads_running_with_its_unreached_steps_planned(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    for entry in ENTRIES:
        _write_loss(root, "v0", entry)
    _write_loss(root, "v1", "login")  # half the board swept so far
    log = RoundLog(root, EPOCH, 0)
    log.append(RoundOpened(contract_hash="hash-1"))
    log.append(ProposalAttempted(errors=()))
    log.append(ExperimentMinted(experiment_id="exp-1"))
    log.append(PatchesApplied(generation_id="v1"))

    round_zero = _stage(_plan(root), "/round:0")

    assert round_zero["status"] == "running"
    statuses = {child["kind"]: child["status"] for child in round_zero["children"]}
    assert statuses == {
        "propose_step": "done",
        "apply_step": "done",
        "run_step": "running",
        "gate_step": "planned",
        "decide_step": "planned",
    }
    sweep = _find(round_zero, f"e:{EPOCH}/round:0/run/v1")
    assert sweep["progress"] == {"done": 1, "total": 2}
    assert sweep["outcome"]["entries_without_units"] == ["search"]


def test_a_failed_validation_fails_the_apply_step_and_names_its_findings(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    log = RoundLog(root, EPOCH, 0)
    log.append(RoundOpened(contract_hash="hash-1"))
    log.append(ProposalAttempted(errors=()))
    log.append(ExperimentMinted(experiment_id="exp-1"))
    log.append(PatchesApplied(generation_id="v1"))
    log.append(ValidationFailed(findings=("import of a banned module",)))
    log.append(DecisionRecorded(decision="reject", provenance={}))
    log.append(RoundClosed())

    round_zero = _stage(_plan(root), "/round:0")
    apply_step = _find(round_zero, f"e:{EPOCH}/round:0/apply")

    assert apply_step["status"] == "failed"
    assert apply_step["outcome"]["validation_findings"] == ["import of a banned module"]
    validation = _find(apply_step, f"e:{EPOCH}/round:0/apply/validation")
    assert validation["status"] == "failed"
    # The round closed without running or gating: those steps are stated
    # absences, not gaps in the tree.
    statuses = {child["kind"]: child["status"] for child in round_zero["children"]}
    assert statuses["run_step"] == "skipped"
    assert statuses["gate_step"] == "skipped"


def test_a_holdout_never_released_reads_skipped(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    log = RoundLog(root, EPOCH, 0)
    log.append(RoundOpened(contract_hash="hash-1"))
    log.append(PatchesApplied(generation_id="v1"))
    log.append(GateEvaluated(rule_fired="insufficient improvement", decision="reject"))
    log.append(DecisionRecorded(decision="reject", provenance={}))
    log.append(RoundClosed())

    holdout = _find(_stage(_plan(root), "/round:0"), f"e:{EPOCH}/round:0/gate/holdout")

    assert holdout["status"] == "skipped"
    assert holdout["outcome"]["confirmed"] is None


def test_a_run_directory_with_no_loss_file_leaves_the_sweep_partial(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _write_loss(root, "v0", "login")
    WorkspaceLayout.from_root(root).run_dir(EPOCH, "v0", "search").mkdir(parents=True)

    sweep = _stage(_plan(root), "/baseline")["children"][0]

    assert sweep["progress"] == {"done": 1, "total": 2}
    assert sweep["outcome"]["entries_without_units"] == ["search"]
    assert sweep["provenance"] == "partial"
    assert len(sweep["children"]) == 1


def test_a_run_directory_for_an_entry_the_board_dropped_is_named_not_counted(
    tmp_path: Path,
) -> None:
    """Real work off the current board: shown, named, and never over-counted."""
    root = _workspace(tmp_path)
    for entry in (*ENTRIES, "retired_entry"):
        _write_loss(root, "v0", entry)

    sweep = _stage(_plan(root), "/baseline")["children"][0]

    assert sweep["progress"] == {"done": 2, "total": 2}
    assert sweep["outcome"]["entries_not_on_board"] == ["retired_entry"]
    assert sweep["outcome"]["unit_count"] == 3
    assert sweep["provenance"] == "partial"


def test_a_candidate_two_rounds_name_is_counted_once(tmp_path: Path) -> None:
    """A champion carried into the next round keeps ONE home in the tree.

    Its units are on disk once, so counting them under every round that
    loaded it would report work that never happened twice over.
    """
    root = _workspace(tmp_path)
    for entry in ENTRIES:
        _write_loss(root, "v1", entry)
    _complete_round(root, 0, challenger="v1")
    log = RoundLog(root, EPOCH, 1)
    log.append(RoundOpened(contract_hash="hash-2"))
    log.append(HarnessLoaded(generation_id="v1", entrypoint_file="agent/agent.py"))
    log.append(RoundClosed())

    plan = _plan(root)

    sweeps = _of_kind(plan, "board_sweep")
    assert [sweep["id"] for sweep in sweeps] == [f"e:{EPOCH}/round:0/run/v1"]
    assert len(_of_kind(plan, "board_entry_run")) == 2


def test_a_torn_round_log_tail_still_folds_the_round(complete_run: Path) -> None:
    """A crash mid-append loses the partial line, not the round."""
    path = round_log_path(complete_run, EPOCH, 0)
    path.write_text(path.read_text(encoding="utf-8") + '{"seq":10,"ty', encoding="utf-8")

    round_zero = _stage(_plan(complete_run), "/round:0")

    assert round_zero["status"] == "done"
    assert round_zero["provenance"] == "exact"
    assert round_zero["outcome"]["decision"] == "promote"


def test_a_corrupt_round_log_degrades_the_round_to_partial(complete_run: Path) -> None:
    """Interior corruption means the spine cannot be trusted — and says so."""
    path = round_log_path(complete_run, EPOCH, 0)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = "{not json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    round_zero = _stage(_plan(complete_run), "/round:0")

    assert round_zero["provenance"] == "partial"
    assert "unreadable" in round_zero["outcome"]["note"]


def test_a_planned_round_appears_only_when_a_total_is_recorded(complete_run: Path) -> None:
    """A future round shows its existence — no board, no candidates, no children."""
    assert [stage["kind"] for stage in _plan(complete_run)["stages"]] == ["baseline", "round"]

    (complete_run / "runtime").mkdir(parents=True, exist_ok=True)
    (complete_run / "runtime" / "active_tournament.json").write_text(
        json.dumps(
            {
                "tournament_id": "t1",
                "parent_generation_id": "v0",
                "child_generation_id": "v1",
                "epoch_id": EPOCH,
                "started_at": "2026-08-18T00:00:00Z",
                "total_rounds": 3,
            }
        ),
        encoding="utf-8",
    )

    stages = _plan(complete_run)["stages"]
    assert [stage["id"] for stage in stages[2:]] == [
        f"e:{EPOCH}/round:1",
        f"e:{EPOCH}/round:2",
    ]
    for planned in stages[2:]:
        assert planned["status"] == "planned"
        assert planned["provenance"] == "partial"
        assert planned["children"] == []


def test_the_board_is_carried_as_a_digest_not_as_entries(complete_run: Path) -> None:
    """A client fetches the board once; the plan never copies an entry onto a unit."""
    plan = _plan(complete_run)

    assert plan["board"]["entry_count"] == 2
    assert len(plan["board"]["digest"]) == 64
    assert "input" not in json.dumps(plan["stages"])


# ---------------------------------------------------------------------------
# Degrade (DQ3) — one shape, every missing input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "removed",
    ["epochs", "lineage.json", "current_epoch"],
    ids=["no epoch tree", "no lineage", "no current epoch marker"],
)
def test_every_missing_input_degrades_to_the_plan_shape(complete_run: Path, removed: str) -> None:
    target = complete_run / removed
    if target.is_dir():
        for path in sorted(target.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        target.rmdir()
    elif target.exists():
        target.unlink()

    plan = build_execution_plan(WorkspacePaths(complete_run), EPOCH)

    assert set(plan) == {"epoch_id", "generated_at", "board", "note", "stages"}
    assert isinstance(plan["stages"], list)


def test_an_empty_board_still_serves_the_rounds(complete_run: Path) -> None:
    WorkspaceLayout.from_root(complete_run).board(EPOCH).write_text("", encoding="utf-8")

    plan = _plan(complete_run)

    assert plan["board"] == {"digest": "", "entry_count": 0}
    sweep = _find(_stage(plan, "/round:0"), f"e:{EPOCH}/round:0/run/v1")
    # Without a board there is no total to report, but the units are real.
    assert sweep["progress"] is None
    assert len(sweep["children"]) == 6


def test_an_unreadable_board_row_does_not_empty_the_plan(complete_run: Path) -> None:
    board = WorkspaceLayout.from_root(complete_run).board(EPOCH)
    board.write_text(board.read_text(encoding="utf-8") + "{ not json\n", encoding="utf-8")

    assert _plan(complete_run)["board"]["entry_count"] == 2


def test_an_unknown_epoch_degrades_to_the_empty_plan(complete_run: Path) -> None:
    plan = build_execution_plan(WorkspacePaths(complete_run), "../escape")

    assert plan == {
        "epoch_id": "../escape",
        "generated_at": plan["generated_at"],
        "board": {"digest": "", "entry_count": 0},
        "note": "unknown epoch",
        "stages": [],
    }


def test_a_workspace_with_no_epoch_degrades_to_the_empty_plan(tmp_path: Path) -> None:
    root = tmp_path / ".zicato"
    root.mkdir()

    plan = build_execution_plan(WorkspacePaths(root))

    assert (plan["epoch_id"], plan["stages"], plan["note"]) == (None, [], "no epoch")


# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------


def test_an_unknown_kind_survives_the_wire(complete_run: Path) -> None:
    """``kind`` is open: a level a newer server adds must round-trip intact."""
    node = PlanNode(
        id=f"e:{EPOCH}/round:0/run/v1/login/r0/shard:2",
        kind="a_level_this_reader_has_never_heard_of",
        label="Shard 2",
        purpose="A level introduced after this client shipped.",
        status="done",
        children=(PlanNode(id="x", kind="another_new_kind", label="X", purpose="", status="done"),),
    )

    decoded = json.loads(json.dumps(node.payload()))

    assert decoded["kind"] == "a_level_this_reader_has_never_heard_of"
    assert decoded["children"][0]["kind"] == "another_new_kind"
    assert set(decoded) == {
        "id",
        "kind",
        "label",
        "purpose",
        "status",
        "provenance",
        "started_at",
        "ended_at",
        "duration_ms",
        "progress",
        "coordinates",
        "outcome",
        "children",
    }


def test_every_json_get_route_declares_a_payload_contract(tmp_path: Path) -> None:
    """DQ13: ``ENDPOINT_PAYLOADS`` is the exhaustive inventory of JSON GETs."""
    static = tmp_path / "static"
    static.mkdir()
    app = create_app(_workspace(tmp_path), static)
    routes = {
        route.path
        for route in app.routes
        if "GET" in (getattr(route, "methods", None) or set())
        and route.path.startswith("/api/")
        # The two text routes serve markdown / HTML, not a JSON payload.
        and not route.path.endswith((".md", ".html"))
    }

    assert PLAN_ROUTE in routes
    assert routes - set(ENDPOINT_PAYLOADS) == set()


def _client(root: Path, tmp_path: Path) -> TestClient:
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    return TestClient(create_app(root, static))


def test_the_endpoint_serves_the_plan(complete_run: Path, tmp_path: Path) -> None:
    payload = _client(complete_run, tmp_path).get(f"/api/epoch/{EPOCH}/execution-plan").json()

    assert payload["epoch_id"] == EPOCH
    assert [stage["kind"] for stage in payload["stages"]] == ["baseline", "round"]


@pytest.mark.parametrize("epoch_id", ["bad id", "a..b", "never_ran"])
def test_a_malformed_or_unknown_coordinate_answers_200_with_the_empty_plan(
    complete_run: Path, tmp_path: Path, epoch_id: str
) -> None:
    """DQ12 / DQ3: no traversal, no 500 — the same shape at HTTP 200."""
    response = _client(complete_run, tmp_path).get(f"/api/epoch/{epoch_id}/execution-plan")

    assert response.status_code == 200
    payload = response.json()
    assert payload["stages"] == []
    assert set(payload) == {"epoch_id", "generated_at", "board", "note", "stages"}
