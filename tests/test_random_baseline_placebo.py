"""Random-baseline (placebo) challenger — OVERFITTING.md #7 tests.

The placebo arm's whole point is measurable here: with the deterministic
target_0 adapter the no-op tree scores identically to the champion, so
the gate rejects it every cadence tick and the ``placebo_promoted``
finding never fires; with a rigged always-promote gate stub the finding
fires CRITICAL. No live models anywhere — scripted proposers, canned
telemetry, or the deterministic planted-defect harness.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import zicato_examples.target_0_convergence as _t0_pkg
from tests._contract_pins import pin_deterministic
from tests.test_orchestrator import (
    _harness_call_llm,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
    _make_aux_responder,
)
from tests.test_orchestrator_multi_challenger import _distinct_field_responses
from zicato.core.experiment import PLACEBO_HYPOTHESIS_MARKER
from zicato.core.types import OverfittingConfig, ScoringWeights, TournamentStructure
from zicato.epoch.lifecycle import _scoring_from_dict, new_epoch
from zicato.evolve.placebo import (
    build_placebo_experiment,
    is_placebo_experiment,
    placebo_noop_content,
    placebo_noop_patch,
    placebo_round_due,
)
from zicato.health.diagnostics import (
    assess_loop_health,
    detect_placebo_promoted,
)
from zicato.mutation.enumerator import enumerate_mutations
from zicato_examples.target_0_convergence import mocks as t0_mocks
from zicato_examples.target_0_convergence.harness import parse_style_tokens

EXAMPLE_DIR = Path(_t0_pkg.__file__).resolve().parent
AGENT_DIR = EXAMPLE_DIR / "agent"
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"
SCORING_PATH = EXAMPLE_DIR / "scoring.json"


# ---------------------------------------------------------------------------
# Pure cadence + no-op construction
# ---------------------------------------------------------------------------


def test_placebo_round_due_math() -> None:
    assert placebo_round_due(0, 1) is False  # default: never
    assert placebo_round_due(-1, 3) is False
    assert placebo_round_due(1, 1) is True  # every round
    assert placebo_round_due(1, 7) is True
    assert placebo_round_due(3, 3) is True
    assert placebo_round_due(3, 4) is False
    assert placebo_round_due(3, 6) is True
    assert placebo_round_due(2, None) is False  # unresolvable round


def test_noop_content_preserves_span_value() -> None:
    """On target_0's real span point, the no-op re-emits the literal's
    VALUE (not the whole assignment line — an assignment echo would NOT
    be a no-op after the applier's literal wrap)."""
    (point,) = enumerate_mutations([AGENT_DIR])
    assert point.id == "style_rules"
    assert placebo_noop_content(point) == "verbose-prose; omit-summary; skip-citations"


def test_noop_content_verbatim_for_file_code_and_non_py() -> None:
    from zicato.core.mutation import MutationPoint

    def _point(kind: str, content: str, suffix: str = ".py") -> MutationPoint:
        return MutationPoint(
            id="p",
            kind=kind,  # type: ignore[arg-type]
            file=Path(f"/tmp/x{suffix}"),
            source_root=Path("/tmp"),
            line_start=1,
            line_end=1,
            content=content,
            content_hash="",
        )

    assert placebo_noop_content(_point("file", "X = 1\n")) == "X = 1\n"
    assert placebo_noop_content(_point("code", "if a:\n    b()\n")) == "if a:\n    b()\n"
    assert placebo_noop_content(_point("span", "raw prompt body", suffix=".md")) == (
        "raw prompt body"
    )


def test_noop_patch_applies_semantics_preserving(tmp_path: Path) -> None:
    """The applier accepts the no-op patch and the resulting tree parses
    to the IDENTICAL policy value — the placebo tree behaves exactly like
    its parent."""
    from zicato.mutation.applier import apply_patches

    (point,) = enumerate_mutations([AGENT_DIR])
    patch = placebo_noop_patch(point)
    target = tmp_path / "placebo-tree"
    apply_patches(AGENT_DIR, [patch], target)

    original = parse_style_tokens((AGENT_DIR / "policy.py").read_text())
    rewritten = parse_style_tokens((target / "policy.py").read_text())
    assert rewritten == original == ["verbose-prose", "omit-summary", "skip-citations"]


def test_placebo_experiment_is_marked() -> None:
    (point,) = enumerate_mutations([AGENT_DIR])
    exp = build_placebo_experiment(
        epoch_id="e1", generation_id="v9", parent_id="v0", point=point, round_index=9
    )
    assert exp.hypothesis.core_idea.startswith(PLACEBO_HYPOTHESIS_MARKER)
    assert exp.hypothesis.modulating == ("style_rules",)
    assert is_placebo_experiment(exp)
    # The dict form (experiment.json shape) is recognised too.
    as_dict = {"hypothesis": {"core_idea": exp.hypothesis.core_idea}}
    assert is_placebo_experiment(as_dict)
    assert not is_placebo_experiment({"hypothesis": {"core_idea": "a real idea"}})
    assert not is_placebo_experiment({})


# ---------------------------------------------------------------------------
# Knob: contract field + omit-at-default canonicalization
# ---------------------------------------------------------------------------


def test_knob_validation_and_canon_omission() -> None:
    from zicato.epoch.contract import scoring_to_canon

    with pytest.raises(ValueError):
        OverfittingConfig(random_baseline_every_n=-1)

    default_canon = scoring_to_canon(ScoringWeights())
    assert "random_baseline_every_n" not in default_canon["overfitting"]

    on = ScoringWeights(overfitting=OverfittingConfig(random_baseline_every_n=4))
    on_canon = scoring_to_canon(on)
    assert on_canon["overfitting"]["random_baseline_every_n"] == 4
    assert json.dumps(on_canon, sort_keys=True, default=str) != json.dumps(
        default_canon, sort_keys=True, default=str
    )


# ---------------------------------------------------------------------------
# Detector + loop-health input filtering
# ---------------------------------------------------------------------------


def _exp_dict(gen: str, decision: str | None, *, placebo: bool = False) -> dict:
    core = f"{PLACEBO_HYPOTHESIS_MARKER} no-op arm" if placebo else f"tweak {gen}"
    out = (
        None
        if decision is None
        else {"tournament_decision": decision, "scalar_score_delta": -0.001}
    )
    return {"generation_id": gen, "hypothesis": {"core_idea": core}, "outcome": out}


def test_detector_fires_only_on_promoted_placebo() -> None:
    assert detect_placebo_promoted([]) == []
    assert detect_placebo_promoted([_exp_dict("v1", "promoted")]) == []  # real win: fine
    assert detect_placebo_promoted([_exp_dict("v2", "rejected", placebo=True)]) == []
    assert detect_placebo_promoted([_exp_dict("v3", None, placebo=True)]) == []

    (finding,) = detect_placebo_promoted([_exp_dict("v4", "promoted", placebo=True)])
    assert finding.code == "placebo_promoted"
    assert finding.severity == "critical"
    assert finding.detail["generation_id"] == "v4"


def test_placebo_experiments_are_calibration_probes_not_stream() -> None:
    """An always-rejected placebo fielded each round must NOT read as a
    stalled loop: assess_loop_health splits it out of the stream
    detectors, while a promoted placebo still raises the alarm."""
    # Two real trailing rejects + one placebo reject: threshold 3 must NOT
    # trip (the placebo is not part of the optimization stream).
    experiments = [
        _exp_dict("v1", "rejected"),
        _exp_dict("v2", "rejected"),
        _exp_dict("v2-placebo", "rejected", placebo=True),
    ]
    health = assess_loop_health({}, experiments, [], "e1")
    assert not any(f.code == "stalled_loop" for f in health.findings)
    assert not any(f.code == "placebo_promoted" for f in health.findings)

    # Three REAL trailing rejects still trip, placebo interleaved or not.
    experiments.insert(2, _exp_dict("v3", "rejected"))
    health = assess_loop_health({}, experiments, [], "e1")
    (stall,) = (f for f in health.findings if f.code == "stalled_loop")
    assert "placebo" not in " ".join(stall.detail["rejected_generation_ids"])

    # A promoted placebo raises the CRITICAL alarm through the same call.
    experiments.append(_exp_dict("v4-placebo", "promoted", placebo=True))
    health = assess_loop_health({}, experiments, [], "e1")
    assert any(f.code == "placebo_promoted" and f.severity == "critical" for f in health.findings)
    assert health.healthy is False


# ---------------------------------------------------------------------------
# Gauntlet integration — target_0 deterministic adapter, real duels
# ---------------------------------------------------------------------------


def _bootstrap_t0(tmp_path: Path, *, every_n: int) -> tuple[Path, str]:
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "default",
                "created_at": "2026-07-01T00:00:00Z",
                "generation_source_backend": "directory",
                "adapter": {
                    "kind": "import",
                    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
                },
                "mutable_trees": [str(AGENT_DIR)],
            }
        )
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Placebo brief\n- Remove defect tokens.\n")
    raw = json.loads(SCORING_PATH.read_text())
    raw.setdefault("overfitting", {})["random_baseline_every_n"] = every_n
    weights = _scoring_from_dict(raw)
    cfg = new_epoch(
        workspace,
        name="t0-placebo",
        board_source=BOARD_PATH,
        brief_source=brief,
        weights=weights,
        auto_close_previous=False,
        proposer_path=EXAMPLE_DIR / "proposer",
    )
    return workspace, cfg.id


def _run_one_round(workspace: Path, epoch_id: str) -> list:
    from zicato.evolve.loop import evolve_n_rounds

    t0_mocks.reset()
    return asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=t0_mocks.harness_llm,
            auxiliary_call_llm=t0_mocks.aux_llm,
            auto_epoch=False,
            fast_mode=True,
        )
    )


def _round_health(workspace: Path, epoch_id: str, round_n: int) -> dict:
    path = workspace / "epochs" / epoch_id / "health" / f"round_{round_n}.json"
    return json.loads(path.read_text())


def test_gauntlet_placebo_always_rejected_and_no_alarm(tmp_path: Path) -> None:
    """Deterministic world: the no-op scores exactly the champion's scalar,
    the gate rejects it, the champion pointer is untouched, and the
    placebo_promoted finding never fires."""
    workspace, epoch_id = _bootstrap_t0(tmp_path, every_n=1)
    outcomes = _run_one_round(workspace, epoch_id)

    # The REAL round is unchanged: v1 promoted per the scripted proposer.
    assert outcomes[0].tournament_decision == "promoted"
    assert outcomes[0].proposed_generation_id == "v1"

    # The placebo arm ran as an extra scheduled duel with a marked
    # hypothesis and a rejected outcome (identical trees ⇒ no improvement).
    exp_path = workspace / "epochs" / epoch_id / "generations" / "v1-placebo" / "experiment.json"
    assert exp_path.exists()
    exp = json.loads(exp_path.read_text())
    assert exp["hypothesis"]["core_idea"].startswith(PLACEBO_HYPOTHESIS_MARKER)
    assert exp["outcome"]["tournament_decision"] == "rejected"
    assert exp["outcome"]["scalar_score_delta"] == 0.0

    # The champion pointer advanced to the REAL winner, never the placebo.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v1"

    # Lineage records the placebo as a dead branch of the round's parent.
    lineage = json.loads((workspace / "lineage.json").read_text())
    nodes = {
        n["id"]: n
        for ep in lineage.get("epochs", [])
        if ep.get("id") == epoch_id
        for n in ep.get("generations", [])
    }
    assert nodes["v1-placebo"]["promoted"] is False
    assert nodes["v1-placebo"]["parent_id"] == "v0"

    # The finding never fires; the report exists for the round.
    report = _round_health(workspace, epoch_id, 1)
    assert not any(f["code"] == "placebo_promoted" for f in report["findings"])
    # And the placebo did not pollute the stream detectors (no stall from
    # a single always-rejected control).
    assert not any(f["code"] == "stalled_loop" for f in report["findings"])


def test_gauntlet_cadence_off_means_no_placebo(tmp_path: Path) -> None:
    workspace, epoch_id = _bootstrap_t0(tmp_path, every_n=0)
    _run_one_round(workspace, epoch_id)
    gens = workspace / "epochs" / epoch_id / "generations"
    assert not any(p.name.endswith("-placebo") for p in gens.iterdir())


def test_gauntlet_rigged_gate_fires_critical_alarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rigged always-promote gate promotes the no-op ⇒ the CRITICAL
    placebo_promoted finding fires in that round's health report — and the
    champion pointer STILL never advances to the placebo."""
    from zicato.core.types import TournamentDecision
    from zicato.tournament.gate import GateOutcome

    def _rigged_gate(parent_agg, child_agg, weights, **kwargs):  # noqa: ANN001, ANN003
        del parent_agg, child_agg, weights, kwargs
        return GateOutcome(
            decision=TournamentDecision.PROMOTED,
            reason="rigged always-promote stub",
            delta_scalar=-0.001,
            delta_pass_rate=0.0,
        )

    monkeypatch.setattr("zicato.tournament.runner.evaluate_gate", _rigged_gate)

    workspace, epoch_id = _bootstrap_t0(tmp_path, every_n=1)
    _run_one_round(workspace, epoch_id)

    exp = json.loads(
        (
            workspace / "epochs" / epoch_id / "generations" / "v1-placebo" / "experiment.json"
        ).read_text()
    )
    assert exp["outcome"]["tournament_decision"] == "promoted"

    report = _round_health(workspace, epoch_id, 1)
    (alarm,) = (f for f in report["findings"] if f["code"] == "placebo_promoted")
    assert alarm["severity"] == "critical"
    assert alarm["detail"]["generation_id"] == "v1-placebo"
    assert report["has_critical"] is True

    # Even a promoted placebo never becomes the champion.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() != "v1-placebo"


# ---------------------------------------------------------------------------
# Multi-challenger field — one extra slate slot (stubbed world)
# ---------------------------------------------------------------------------


def _bootstrap_swiss_with_placebo(tmp_path: Path, *, field_size: int) -> tuple[Path, str]:
    """A swiss workspace whose contract fields the placebo every round.

    Mirrors ``test_orchestrator_multi_challenger._bootstrap_swiss_workspace``
    with ``overfitting.random_baseline_every_n=1`` stamped on the frozen
    weights.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "created_at": "2026-05-31T00:00:00Z",
                "generation_source_backend": "directory",
                "adapter": {"kind": "stub"},
            }
        )
    )
    board_src = tmp_path / "board.jsonl"
    board_src.write_text(
        json.dumps(
            {
                "id": "entry_a",
                "kind": "single_turn",
                "wall_clock_budget_seconds": 60,
                "input": "hello",
            }
        )
        + "\n"
    )
    brief_src = tmp_path / "brief.md"
    brief_src.write_text("# Proposer brief\n- Be careful.\n")
    cfg = new_epoch(
        workspace,
        name="swiss-placebo",
        board_source=board_src,
        brief_source=brief_src,
        weights=pin_deterministic(
            ScoringWeights(
                promote_margin=0.01,
                tournament_structure=TournamentStructure(
                    structure="swiss",
                    params={"field_size": field_size, "rounds_n": 1, "replicates": 1},
                ),
                overfitting=OverfittingConfig(random_baseline_every_n=1),
            )
        ),
        auto_close_previous=False,
    )
    v0_dir = workspace / "epochs" / cfg.id / "generations" / "v0"
    snap = v0_dir / "snapshot"
    snap.mkdir(parents=True)
    (snap / "agent.py").write_text(
        '"""Stub harness source for tests."""\n'
        "\n"
        '# zicato:mutable id="greeting"\n'
        'GREETING = "hello"\n'
    )
    (workspace / "epochs" / cfg.id / "current_generation").write_text("v0\n")
    return workspace, cfg.id


def test_multi_challenger_field_gets_extra_placebo_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 2-challenger swiss field carries a third, placebo slot: it runs
    through the unchanged strategy + gate, is rejected (its canned score
    equals the champion's), and the real winner is crowned."""
    workspace, epoch_id = _bootstrap_swiss_with_placebo(tmp_path, field_size=2)
    _install_stub_adapter_factory(monkeypatch)
    # v1 is the strongest; v3 (the placebo, minted after v1/v2) scores
    # exactly like the champion v0 — identical trees, identical measure.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5, "v3": 2.0},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(_distinct_field_responses(2)),
        )
    )

    assert outcome.tournament_decision == "promoted"
    assert outcome.proposed_generation_id == "v1"

    gens = workspace / "epochs" / epoch_id / "generations"
    placebo = json.loads((gens / "v3" / "experiment.json").read_text())
    assert placebo["hypothesis"]["core_idea"].startswith(PLACEBO_HYPOTHESIS_MARKER)
    assert placebo["outcome"]["tournament_decision"] == "rejected"

    # The placebo entered the live envelope as a real competitor slot.
    from zicato.runtime.state import read_active_tournament

    active = read_active_tournament(workspace)
    assert active is not None
    assert {c["generation_id"] for c in active.competitors} >= {"v0", "v1", "v2"}
    placebo_slots = [s for s in active.field_status if s.get("reason") == "random_baseline"]
    assert [s["generation_id"] for s in placebo_slots] == ["v3"]

    # No alarm: the gate rejected the placebo. (The stubbed telemetry world
    # writes no health report file, so assert at the detector level over the
    # persisted experiments — exactly what the report would have folded in.)
    experiments = [
        json.loads((gens / gid / "experiment.json").read_text()) for gid in ("v1", "v2", "v3")
    ]
    assert detect_placebo_promoted(experiments) == []
