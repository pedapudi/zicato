"""WS-ADMIT — the eval-synthesis admission pipeline (EVAL-SYNTHESIS.md §5).

Every probe is driven against the REAL board-unit runner over a SEEDED
``_run_single`` (the cascade-OC / power-harness precedent), so every statistic
is a known answer with zero live spend. The suite pins:

* the **OC proof** (§8): a planted coverage gap where a LIVE drafted entry
  measurably discriminates (``separated > 0``) and a deliberately-DEAD entry
  does not (``= 0``), flip rates measured at the reserved base ``6000``, r0
  never touched, and the reserved-base ledger disjoint;
* the **plan vs spend** split — plan mode runs NOTHING (zero ``_run_single``
  calls) and returns the up-front cost estimate;
* the **per-stage honest degrades** — a cold workspace / no settled candidates
  leaves discrimination ``unmeasured``, an invalid draft marks execution
  ``ran=false``, a single usable draw leaves the flip rate ``unmeasured``;
* the **leakage / collusion** checks — the §4 rotation rule, the emulator guard,
  and the self-preference flag.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import zicato.tournament.runner as runner_mod
from zicato.core import (
    BoardEntry,
    Generation,
    JudgeSpec,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.core.board import (
    Expectation,
    ExpectationKind,
    JudgeMode,
    UserPersona,
)
from zicato.core.workspace import generation_dir, loss_profile_path, run_dir
from zicato.reflection.admission import (
    DEFAULT_NOISE_RUNS,
    SYNTHESIS_REPLICATE_BASE,
    AdmissionRequest,
    admit_suggestion,
    estimate_cost,
)
from zicato.reflection.mining import (
    HINT_COVERAGE_ENTRY,
    HINT_JUDGE,
    HINT_REGRESSION_ENTRY,
)

EPOCH = "epoch-1"
CHAMPION = "champ-v0"


# ---------------------------------------------------------------------------
# The seeded noise model — a scripted ``_run_single`` with known answers.
# ---------------------------------------------------------------------------


class _ScriptedRunner:
    """A seeded ``_run_single``: verdict is a pure function of (entry, gen, slot).

    * ``live_probe`` — verdict tracks the GENERATION (``champ*`` pass, ``child*``
      fail), so it discriminates a champion/challenger pair.
    * ``dead_probe`` — always passes, so it separates nothing.
    * ``noisy_probe`` — verdict tracks the replicate-index PARITY, so its A/A
      series flips and the flip rate is nonzero.
    * ``abort_probe`` — always aborts (a loud non-execution).

    Records every ``(generation, entry, replicate_index)`` slot it is asked to
    draw, so a test can prove the reserved base is used and r0 is untouched.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.slots: list[tuple[str, str, int]] = []

    async def __call__(
        self,
        *,
        adapter: object,
        generation: Generation,
        entry: BoardEntry,
        weights: object,
        config: object,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        self.calls += 1
        replicate_index = int(entry.context.get("replicate_index", "0"))
        self.slots.append((generation.id, entry.id, replicate_index))
        abort_cause = "wall_clock_budget" if entry.id == "abort_probe" else None
        return LossProfile(
            run_id=f"run-{generation.id}-{entry.id}-{replicate_index}",
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id=epoch_id,
            drift_counts=(),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=1,
            wall_clock_budget_exceeded=False,
            expectation_result=None,
            drift_loss=0.0,
            pass_fail=self._verdict(entry.id, generation.id, replicate_index),
            abort_cause=abort_cause,
        )

    @staticmethod
    def _verdict(entry_id: str, gen_id: str, replicate_index: int) -> bool:
        if entry_id == "live_probe":
            return gen_id.startswith("champ")
        if entry_id == "noisy_probe":
            return replicate_index % 2 == 0
        # dead_probe / everything else: constant pass — never separates.
        return True


def _entry(entry_id: str, *, tags: tuple[str, ...] = ()) -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=30,
        tags=tags,
        input="probe",
        expectation=Expectation(kind=ExpectationKind.EXPECTED_TEXT, spec="ok"),
    )


def _config(workspace: Path) -> RuntimeConfig:
    return RuntimeConfig(
        instance_id="default",
        workspace_root=workspace,
        harness_call_llm=None,
        auxiliary_call_llm=None,
    )


def _generation(gen_id: str, snapshot: Path) -> Generation:
    return Generation(
        id=gen_id, epoch_id=EPOCH, parent_id=None, snapshot_root=snapshot, created_at=""
    )


def _settled(champ: str, child: str, decision: str = "promoted") -> dict:
    return {
        "generation_id": child,
        "parent_generation_id": champ,
        "outcome": decision,
    }


# The reign's settled matchups for the OC proof: two champ-vs-child pairs (the
# live probe separates) and one child-vs-child pair (it does not).
_MATCHUPS = [
    ("champ-v0", "child-v1"),
    ("champ-v0", "child-v2"),
    ("child-v1", "child-v3"),
]
_EXPERIMENTS = [
    _settled("champ-v0", "child-v1"),
    _settled("champ-v0", "child-v2", "rejected"),
    _settled("child-v1", "child-v3"),
]


def _materialize_snapshots(workspace: Path) -> None:
    """Create the on-disk snapshot trees the discrimination probe reconstructs."""
    for gid in {CHAMPION, *(g for pair in _MATCHUPS for g in pair)}:
        snap = generation_dir(workspace, EPOCH, gid) / "snapshot"
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "entrypoint.py").write_text("# stub\n", encoding="utf-8")


def _run(coro):
    return asyncio.run(coro)


def _admit(request: AdmissionRequest, workspace: Path, *, spend: bool, board=None):
    champ_snap = generation_dir(workspace, EPOCH, CHAMPION) / "snapshot"
    return _run(
        admit_suggestion(
            request,
            champion=_generation(CHAMPION, champ_snap),
            board=board if board is not None else [request.entry],
            experiments=_EXPERIMENTS,
            weights=ScoringWeights(),
            config=_config(workspace),
            adapter=object(),
            workspace_root=workspace,
            epoch_id=EPOCH,
            spend=spend,
        )
    )


# ---------------------------------------------------------------------------
# The OC proof (§8)
# ---------------------------------------------------------------------------


def test_oc_proof_live_entry_discriminates_dead_entry_does_not(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / ".zicato"
    _materialize_snapshots(workspace)
    stub = _ScriptedRunner()
    monkeypatch.setattr(runner_mod, "_run_single", stub)

    live = _admit(
        AdmissionRequest(
            entry=_entry("live_probe", tags=("holdout",)),
            suggestion_type=HINT_COVERAGE_ENTRY,
        ),
        workspace,
        spend=True,
    )
    dead = _admit(
        AdmissionRequest(
            entry=_entry("dead_probe", tags=("holdout",)),
            suggestion_type=HINT_COVERAGE_ENTRY,
        ),
        workspace,
        spend=True,
    )

    # The planted coverage gap: the live entry separates the champ/child pairs,
    # the dead entry separates nothing — the whole point of the probe.
    assert live.discrimination["measured"] is True
    assert live.discrimination["pairs"] == 3
    assert live.discrimination["separated"] == 2
    assert dead.discrimination["separated"] == 0
    assert dead.discrimination["pairs"] == 3

    # Flip rates are MEASURED at the reserved base 6000.
    assert live.noise["measured"] is True
    assert live.noise["base"] == SYNTHESIS_REPLICATE_BASE
    assert live.noise["flip_rate"] == 0.0  # champion verdict is constant
    assert live.execution["ran"] is True

    payload = live.to_json()
    assert payload["executed"] is True
    assert payload["flip_rate"] == 0.0
    assert payload["discrimination"] == {"separated": 2, "pairs": 3, "measured": True}


def test_oc_proof_noise_draws_land_at_base_6000_and_never_touch_r0(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / ".zicato"
    _materialize_snapshots(workspace)
    stub = _ScriptedRunner()
    monkeypatch.setattr(runner_mod, "_run_single", stub)

    _admit(
        AdmissionRequest(
            entry=_entry("noisy_probe", tags=("holdout",)),
            suggestion_type=HINT_COVERAGE_ENTRY,
        ),
        workspace,
        spend=True,
    )

    # A/A noise draws for the drafted entry on the CHAMPION land at 6000 + j.
    champ_noise = {ri for gid, eid, ri in stub.slots if gid == CHAMPION and eid == "noisy_probe"}
    assert champ_noise == {SYNTHESIS_REPLICATE_BASE + j for j in range(DEFAULT_NOISE_RUNS)}
    # Every synthesis draw is at or above the reserved base — r0 is never keyed.
    assert all(ri >= SYNTHESIS_REPLICATE_BASE for _, _, ri in stub.slots)

    # The canonical r0 loss.json is never written for the drafted entry; the
    # reserved 6000 cache slots are.
    rundir = run_dir(workspace, EPOCH, CHAMPION, "noisy_probe")
    assert not loss_profile_path(workspace, EPOCH, CHAMPION, "noisy_probe").exists()
    assert (rundir / f"loss.r{SYNTHESIS_REPLICATE_BASE}.json").exists()


def test_oc_proof_noisy_entry_has_nonzero_measured_flip_rate(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / ".zicato"
    _materialize_snapshots(workspace)
    monkeypatch.setattr(runner_mod, "_run_single", _ScriptedRunner())

    record = _admit(
        AdmissionRequest(
            entry=_entry("noisy_probe", tags=("holdout",)),
            suggestion_type=HINT_COVERAGE_ENTRY,
        ),
        workspace,
        spend=True,
    )
    # 5 draws: 6000/6002/6004 pass, 6001/6003 fail → min(3,2)/5 = 0.4.
    assert record.noise["flip_rate"] == pytest.approx(0.4)
    assert record.noise["runs"] == DEFAULT_NOISE_RUNS


# ---------------------------------------------------------------------------
# Plan vs spend
# ---------------------------------------------------------------------------


def test_plan_mode_runs_nothing(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / ".zicato"
    _materialize_snapshots(workspace)
    stub = _ScriptedRunner()
    monkeypatch.setattr(runner_mod, "_run_single", stub)

    record = _admit(
        AdmissionRequest(
            entry=_entry("live_probe", tags=("holdout",)),
            suggestion_type=HINT_COVERAGE_ENTRY,
        ),
        workspace,
        spend=False,
    )

    # ZERO board runs in plan mode.
    assert stub.calls == 0
    assert not stub.slots
    assert record.spent is False
    # Every live stage is unmeasured; the free leakage check still ran.
    assert record.execution["measured"] is False
    assert record.noise["measured"] is False
    assert record.discrimination["measured"] is False
    assert record.leakage["checked"] is True
    # The up-front cost estimate: noise = K, discrimination = 2 * 3 pairs.
    assert record.cost.total_units == DEFAULT_NOISE_RUNS + 2 * 3
    assert record.cost.discrimination_units == 6


def test_estimate_cost_matches_settled_matchups() -> None:
    cost = estimate_cost(experiments=_EXPERIMENTS, noise_runs=4, discrimination_candidates=2)
    # discrimination capped at min(3 settled, 2 candidates) = 2 pairs → 4 sides.
    assert cost.noise_units == 4
    assert cost.discrimination_units == 4
    assert cost.total_units == 8
    # A cold workspace (no settled matchups) costs only the A/A series.
    cold = estimate_cost(experiments=[], noise_runs=5)
    assert cold.discrimination_units == 0
    assert cold.total_units == 5


# ---------------------------------------------------------------------------
# Per-stage honest degrades
# ---------------------------------------------------------------------------


def test_discrimination_unmeasured_on_cold_workspace(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / ".zicato"
    generation_dir(workspace, EPOCH, CHAMPION).mkdir(parents=True, exist_ok=True)
    stub = _ScriptedRunner()
    monkeypatch.setattr(runner_mod, "_run_single", stub)

    record = _run(
        admit_suggestion(
            AdmissionRequest(
                entry=_entry("live_probe", tags=("holdout",)),
                suggestion_type=HINT_COVERAGE_ENTRY,
            ),
            champion=_generation(CHAMPION, generation_dir(workspace, EPOCH, CHAMPION) / "snapshot"),
            board=[_entry("live_probe", tags=("holdout",))],
            experiments=[],  # no settled candidates
            weights=ScoringWeights(),
            config=_config(workspace),
            adapter=object(),
            workspace_root=workspace,
            epoch_id=EPOCH,
            spend=True,
        )
    )
    # No settled candidates → discrimination is honestly unmeasured (no fabricated 0).
    assert record.discrimination == {
        "separated": 0,
        "pairs": 0,
        "measured": False,
        "note": "unmeasured",
    }
    # Noise still measures on the champion.
    assert record.noise["measured"] is True


def test_discrimination_unmeasured_when_trees_unreconstructable(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / ".zicato"
    # Champion snapshot only; the settled candidates have NO snapshot tree.
    (generation_dir(workspace, EPOCH, CHAMPION) / "snapshot").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runner_mod, "_run_single", _ScriptedRunner())

    record = _admit(
        AdmissionRequest(
            entry=_entry("live_probe", tags=("holdout",)),
            suggestion_type=HINT_COVERAGE_ENTRY,
        ),
        workspace,
        spend=True,
    )
    assert record.discrimination["measured"] is False


def test_invalid_draft_marks_execution_not_ran(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / ".zicato"
    _materialize_snapshots(workspace)
    stub = _ScriptedRunner()
    monkeypatch.setattr(runner_mod, "_run_single", stub)

    # A single_turn entry with no ``input`` fails BoardEntry.validate().
    bad = BoardEntry(
        id="bad_probe", kind="single_turn", wall_clock_budget_seconds=30, tags=("holdout",)
    )
    record = _admit(
        AdmissionRequest(entry=bad, suggestion_type=HINT_COVERAGE_ENTRY),
        workspace,
        spend=True,
    )
    assert record.execution["ran"] is False
    assert "invalid draft" in record.execution["reason"]
    assert record.noise["measured"] is False
    # A rejected-at-validation draft never reached the runner.
    assert stub.calls == 0


def test_aborted_draw_marks_execution_aborted_and_noise_unmeasured(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / ".zicato"
    _materialize_snapshots(workspace)
    monkeypatch.setattr(runner_mod, "_run_single", _ScriptedRunner())

    record = _admit(
        AdmissionRequest(
            entry=_entry("abort_probe", tags=("holdout",)),
            suggestion_type=HINT_COVERAGE_ENTRY,
        ),
        workspace,
        spend=True,
    )
    assert record.execution["ran"] is False
    assert record.execution["aborted"] is True
    # Draw 0 aborted → the A/A series stops → flip rate unmeasured (never a lie).
    assert record.noise["measured"] is False
    assert record.noise["flip_rate"] is None


# ---------------------------------------------------------------------------
# Leakage / collusion (§4)
# ---------------------------------------------------------------------------


def test_rotation_rule_coverage_entry_must_land_in_holdout(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    # A coverage entry WITHOUT the holdout tag on a small board degrades to
    # train (empty holdout) → the rotation rule is not satisfied.
    untagged = _admit(
        AdmissionRequest(entry=_entry("cov_probe"), suggestion_type=HINT_COVERAGE_ENTRY),
        workspace,
        spend=False,
    )
    assert untagged.leakage["actual_slice"] == "train"
    assert untagged.leakage["target_slice_ok"] is False

    # Tagged holdout → lands in the incoming rotation slice → satisfied.
    tagged = _admit(
        AdmissionRequest(
            entry=_entry("cov_probe", tags=("holdout",)), suggestion_type=HINT_COVERAGE_ENTRY
        ),
        workspace,
        spend=False,
    )
    assert tagged.leakage["actual_slice"] == "incoming_rotation"
    assert tagged.leakage["target_slice_ok"] is True


def test_rotation_rule_regression_entry_may_target_train(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    record = _admit(
        AdmissionRequest(
            entry=_entry("reg_probe"),
            suggestion_type=HINT_REGRESSION_ENTRY,
            target_slice="train",
        ),
        workspace,
        spend=False,
    )
    # A regression test pinning a past failure is working as intended on train.
    assert record.leakage["actual_slice"] == "train"
    assert record.leakage["target_slice_ok"] is True


def test_self_preference_flag_when_families_match(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    judge = JudgeSpec(
        name="drift_judge", mode=JudgeMode.INLINE, body="criterion", severity="warning"
    )
    flagged = _admit(
        AdmissionRequest(
            entry=_entry("judge_probe", tags=("holdout",)),
            suggestion_type=HINT_JUDGE,
            judge=judge,
            expected_answer_family="family-x",
            judge_family="family-x",
        ),
        workspace,
        spend=False,
    )
    assert flagged.leakage["self_preference_flag"] is True

    clean = _admit(
        AdmissionRequest(
            entry=_entry("judge_probe", tags=("holdout",)),
            suggestion_type=HINT_JUDGE,
            judge=judge,
            expected_answer_family="family-x",
            judge_family="family-y",
        ),
        workspace,
        spend=False,
    )
    assert clean.leakage["self_preference_flag"] is False


def test_emulator_guard_flags_persona_less_emulated_draft(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    persona = UserPersona(goal="g", constraints="c", stop_when="s")
    ok = BoardEntry(
        id="emu_ok",
        kind="multi_turn_emulated",
        wall_clock_budget_seconds=30,
        tags=("holdout",),
        user_persona=persona,
        max_turns=3,
    )
    record = _admit(
        AdmissionRequest(entry=ok, suggestion_type=HINT_COVERAGE_ENTRY), workspace, spend=False
    )
    assert record.leakage["emulator_guard_ok"] is True


# ---------------------------------------------------------------------------
# Reserved-base ledger self-audit (§8 / CASCADE §4.5)
# ---------------------------------------------------------------------------


def test_reserved_bases_are_disjoint() -> None:
    from zicato.epoch.preflight import PREFLIGHT_REPLICATE_BASE
    from zicato.epoch.screen import SCREEN_REPLICATE_BASE
    from zicato.reflection.corpus import REFLECTION_REPLICATE_BASE
    from zicato.selection.evidence_gate import EVIDENCE_REPLICATE_BASE
    from zicato.tournament.calibration import CALIBRATION_REPLICATE_BASE

    bases = {
        CALIBRATION_REPLICATE_BASE,
        PREFLIGHT_REPLICATE_BASE,
        SCREEN_REPLICATE_BASE,
        EVIDENCE_REPLICATE_BASE,
        REFLECTION_REPLICATE_BASE,
        SYNTHESIS_REPLICATE_BASE,
    }
    # Every reserved base is distinct, and the new synthesis base claims 6000
    # without disturbing the calibration / evidence / screen / reflection bases.
    assert len(bases) == 6
    assert SYNTHESIS_REPLICATE_BASE == 6000
    assert SYNTHESIS_REPLICATE_BASE not in {
        CALIBRATION_REPLICATE_BASE,
        PREFLIGHT_REPLICATE_BASE,
        SCREEN_REPLICATE_BASE,
        EVIDENCE_REPLICATE_BASE,
        REFLECTION_REPLICATE_BASE,
    }
    # Bands are a full thousand apart, so no plausible K walks one into another.
    ordered = sorted(bases)
    assert all(b - a >= 1000 for a, b in zip(ordered, ordered[1:], strict=False))
