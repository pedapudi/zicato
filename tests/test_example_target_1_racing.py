"""End-to-end mock-harness test for the target_1_presentation example
run under the NON-GAUNTLET ``racing`` tournament structure.

This is the runnable counterpart to the example's gauntlet smoke recipe
(``examples/zicato_examples/target_1_presentation/RUN.md``): it drives the
*real* presentation example — its annotated ``agent/`` tree, its
``board.jsonl``, its ``scoring.racing.json`` contract, and its
``mocks.aux_llm`` proposer — through ``evolve_once`` under the racing
(successive-halving) strategy, with NO live LLM.

It mirrors ``tests/test_orchestrator_multi_challenger.py`` (the synthetic
Swiss field test) but, instead of a hand-built one-marker stub snapshot
and a canned ``_valid_proposer_response``, it:

* copies the vendored example ``agent/`` tree into the v0 snapshot so the
  *real* ``coordinator_instruction`` / ``researcher_instruction`` mutation
  markers are enumerated and the example's proposer patches actually
  apply;
* loads the example's ``scoring.racing.json`` so the frozen epoch contract
  carries the racing ``tournament`` block (field_size=4, eta=2). The
  contract does NOT pin ``board_ids``; the orchestrator defaults them to
  the epoch's full board, so this test also proves the board-slicing rungs
  run from the bare CLI-flag-style contract (no ids listed);
* uses the example's real ``mocks.aux_llm`` as the proposer/aux callable
  (it rotates ``researcher_instruction`` / ``coordinator_instruction``
  patches across the four challengers in the field).

The per-run harness (the ADK agent's inner LLM + the loss reducer) is
mocked exactly as the orchestrator-test suite mocks it — the L3
subprocess worker cannot see in-process harness mocks, so canned
per-generation losses stand in. That is the same fidelity the existing
multi-challenger end-to-end test runs at; here the *contract* (board +
scoring + agent tree + proposer) is the real example.

The test asserts the full multi-challenger racing path executes:
N challengers proposed + applied, racing rungs/cuts recorded, a champion
decision, and the persisted ``ActiveTournament`` envelope + per-match
``OutcomeRecord`` audit.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

# Reuse the fully-mocked harness from the gauntlet orchestrator tests.
# ``zicato_examples`` is resolved through the installed examples package so
# the test is independent of where the examples distribution lives on disk.
import zicato_examples.target_1_presentation as _t1_pkg
from tests._orchestrator_harness import (
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
    run_evolve_once,
)
from zicato.epoch.lifecycle import _scoring_from_dict, new_epoch
from zicato_examples.target_1_presentation import mocks as _t1_mocks


def _preseed_champion_cache(
    workspace: Path,
    epoch_id: str,
    *,
    champion_id: str,
    drift_loss: float,
    pass_fail: bool,
    replicates: int = 1,
) -> None:
    """Write the champion's full-board ``loss.json`` (the cache-first store).

    Models the per-board (per-replicate) ``loss.json`` a PRIOR full round
    under this epoch/contract would have persisted for the champion. The
    cache-first board-unit runner reuses each ``(gen, entry, replicate)``
    unit, so a duel that requests ``R`` replicates needs all ``R`` of the
    champion's replicate slots present to reuse it without a fresh run —
    exactly what a prior full round at the same ``replicates`` produced.
    ``replicates`` therefore defaults to 1 (single-slot) but is set to the
    contract's value for a replicated structure.

    Must run BEFORE ``_install_caching_telemetry_stubs`` swaps the reducer
    in ``sys.modules`` — it imports the REAL reducer's writer.
    """
    from zicato.board.jsonl import load_board as _load_board_file
    from zicato.core.types import LossProfile
    from zicato.core.workspace import board_path
    from zicato.telemetry.reducer import write_loss_profile
    from zicato.tournament.runner import _unit_loss_path

    for entry in _load_board_file(board_path(workspace, epoch_id)):
        for replicate_index in range(max(1, replicates)):
            write_loss_profile(
                LossProfile(
                    run_id=f"r-{champion_id}-{entry.id}-r{replicate_index}",
                    entry_id=entry.id,
                    generation_id=champion_id,
                    epoch_id=epoch_id,
                    drift_counts=(),
                    plan_revisions=0,
                    task_failure_ratio=0.0,
                    runtime_ms=100,
                    wall_clock_budget_exceeded=False,
                    expectation_result=None,
                    drift_loss=drift_loss,
                    pass_fail=pass_fail,
                ),
                _unit_loss_path(workspace, epoch_id, champion_id, entry.id, replicate_index),
            )


def _install_caching_telemetry_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    canned_loss_by_gen: dict[str, float],
    canned_pass_by_gen: dict[str, bool],
    champion_run_log: list[str] | None = None,
) -> None:
    """Telemetry stub that PERSISTS each run's ``loss.json`` and reads it back.

    The default ``_install_telemetry_stubs`` short-circuits
    ``read_loss_profile`` to always raise, so the fast-mode champion-cache
    resolver (which reads per-board ``loss.json`` from disk) can never find
    a cache there. This variant writes a real ``loss.json`` for every run
    via the canonical reducer and reads it back unchanged, so the
    structure-agnostic fast path can reuse a cached champion exactly as it
    will in production. ``champion_run_log`` (when supplied) records the
    generation id of every run that actually executed, so a test can assert
    the champion side did NOT run under fast mode.
    """
    import types as _types

    import zicato.tournament.runner as _runner_mod
    from zicato.core.types import DriftCount, ExpectationResult, LossProfile
    from zicato.core.workspace import loss_profile_path
    from zicato.telemetry.reducer import read_loss_profile, write_loss_profile

    # Keep the default stubs for sink path / harmonograf / adapter wiring.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen=canned_loss_by_gen,
        canned_pass_by_gen=canned_pass_by_gen,
    )

    async def _fake_run_single(
        *, adapter, generation, entry, weights, config, workspace_root, epoch_id, side, match_id=""
    ):
        del adapter, weights, config, side, match_id
        if champion_run_log is not None:
            champion_run_log.append(generation.id)
        expectation_result = (
            ExpectationResult(kind="predicate", passed=True)
            if entry.expectation is not None
            else None
        )
        profile = LossProfile(
            run_id=f"r-{generation.id}-{entry.id}",
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id=epoch_id,
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=100,
            wall_clock_budget_exceeded=False,
            expectation_result=expectation_result,
            drift_loss=canned_loss_by_gen.get(generation.id, 0.0),
            pass_fail=canned_pass_by_gen.get(generation.id),
        )
        # Persist the per-board loss.json so the fast champion-cache
        # resolver can read it back on a later round, exactly like the real
        # subprocess worker does.
        write_loss_profile(
            profile, loss_profile_path(workspace_root, epoch_id, generation.id, entry.id)
        )
        return profile

    monkeypatch.setattr(_runner_mod, "_run_single", _fake_run_single)

    # The runner resolves the reducer lazily via _telemetry_helpers(); point
    # its read_loss_profile at the REAL on-disk reader so cached champion
    # profiles round-trip (the default stub's reader always raises).
    _real_reducer = _types.SimpleNamespace(read_loss_profile=read_loss_profile)
    _sink_mod = __import__("sys").modules["zicato.telemetry.sink"]
    monkeypatch.setattr(_runner_mod, "_telemetry_helpers", lambda: (_sink_mod, _real_reducer))


EXAMPLE_DIR = Path(_t1_pkg.__file__).resolve().parent
AGENT_DIR = EXAMPLE_DIR / "agent"
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"
BRIEF_PATH = EXAMPLE_DIR / "rubric.md"
RACING_SCORING_PATH = EXAMPLE_DIR / "scoring.racing.json"

# The four challenger ids the racing field mints (v1..v4) off the v0
# champion. Distinct canned losses make the racing cuts deterministic.
_CHALLENGER_IDS = ("v1", "v2", "v3", "v4")


def _make_example_aux_responder() -> object:
    """Return a fresh async aux callable backed by the example's mock.

    The example's :func:`mocks.aux_llm` rotates proposer payloads across
    rounds; we reset its module-level round counter first so the field
    starts from challenger 0 regardless of test-ordering side effects.
    """
    _t1_mocks._AUX_STATE["proposer_round"] = 0

    async def _aux(system: str, user: str, model: str) -> str:
        reply: str = await _t1_mocks.aux_llm(system, user, model)
        return reply

    return _aux


def _bootstrap_racing_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Create a workspace + a racing epoch + a v0 snapshot of the example tree.

    Mirrors ``test_orchestrator_multi_challenger._bootstrap_swiss_workspace``
    but freezes the example's ``scoring.racing.json`` contract and seeds
    v0 with a copy of the *real* annotated ``agent/`` tree (so the example's
    proposer patches resolve against real mutation markers).
    """
    workspace, epoch_id = bootstrap_example_workspace(
        tmp_path, scoring_path=RACING_SCORING_PATH, epoch_name="t1-racing"
    )
    weights = _scoring_from_dict(json.loads(RACING_SCORING_PATH.read_text()))
    assert weights.tournament_structure.structure == "racing"
    assert weights.tournament_structure.params["field_size"] == 4
    # The example contract no longer pins board_ids: the orchestrator must
    # default them to the epoch's full board so the rungs still slice. This
    # is the regression the no-board_ids end-to-end path guards.
    assert "board_ids" not in weights.tournament_structure.params
    return workspace, epoch_id


def bootstrap_example_workspace(
    tmp_path: Path, *, scoring_path: Path, epoch_name: str
) -> tuple[Path, str]:
    """Create a workspace + an epoch on ``scoring_path`` + a v0 example snapshot.

    Structure-neutral: the frozen contract decides which tournament
    structure the round runs, so the same bootstrap seeds a racing epoch
    (``scoring.racing.json``) and a gauntlet one (``scoring.json``, which
    declares no ``tournament`` block). The parity capture harness
    (``tools/parity/lib/mock_evolve_capture.py``) drives both from here so
    every golden lane starts from one workspace definition.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "created_at": "2026-05-31T00:00:00Z",
                # Hand-built directory-backend snapshot layout below; pin it
                # so the git default does not look for git tags this fixture
                # never writes.
                "generation_source_backend": "directory",
                "adapter": {"kind": "stub"},
                # This e2e asserts the racing tournament's champion-caching
                # behaviour; opt out of the default-on achievable-signal
                # pre-flight (issue #84) whose A/A floor legitimately runs the
                # champion and would otherwise pollute that run tracking.
                "runtime": {"preflight_gate": "off"},
            }
        )
    )

    weights = _scoring_from_dict(json.loads(scoring_path.read_text()))

    cfg = new_epoch(
        workspace,
        name=epoch_name,
        board_source=BOARD_PATH,
        brief_source=BRIEF_PATH,
        weights=weights,
        auto_close_previous=False,
    )

    # v0 snapshot == a copy of the vendored example agent tree. The stub
    # adapter has no mutable_subpaths, so the orchestrator enumerates
    # markers across the whole snapshot — i.e. the real coordinator /
    # researcher / writer instructions and tool descriptions.
    v0_dir = workspace / "epochs" / cfg.id / "generations" / "v0"
    snap_agent = v0_dir / "snapshot" / "agent"
    snap_agent.parent.mkdir(parents=True)
    shutil.copytree(AGENT_DIR, snap_agent)

    # Pin the promoted head to v0 (the production seeding path writes this;
    # a hand-built v0 must too, else a rejected round's dir-scan fallback
    # resolves to the highest vN dir).
    (workspace / "epochs" / cfg.id / "current_generation").write_text("v0\n")
    return workspace, cfg.id


def test_presentation_racing_field_runs_end_to_end_and_promotes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The example runs under racing: a 4-challenger field is proposed +
    applied off v0, the racing rungs cut the field on board slices, the
    survivor clears the full-board champion gate, and the live
    ActiveTournament envelope + per-challenger OutcomeRecord audit persist."""
    workspace, epoch_id = _bootstrap_racing_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    # Strictly-descending challenger losses: v1 is the best arm and survives
    # every rung, then beats champion v0 on the full board. v4 is worst and
    # dies in rung 0. The cuts are by rank (best-arm identification), the
    # final crowning is the unchanged promote gate.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.4, "v2": 0.8, "v3": 1.2, "v4": 1.6},
        canned_pass_by_gen={gid: True for gid in ("v0", *_CHALLENGER_IDS)},
    )

    outcome = run_evolve_once(workspace, epoch_id, _make_example_aux_responder())

    # --- A challenger from the field was crowned over the champion.
    assert outcome.tournament_decision == "promoted"
    assert outcome.parent_generation_id == "v0"
    crowned = outcome.proposed_generation_id
    assert crowned == "v1", "the lowest-loss arm should survive the rungs and clear the gate"
    assert outcome.child_scalar < outcome.parent_scalar

    gens = workspace / "epochs" / epoch_id / "generations"

    # --- All four challengers were proposed + applied as real children of
    # v0, each carrying a snapshot of the patched agent tree. The example's
    # proposer rotates researcher_instruction / coordinator_instruction;
    # every applied snapshot is a real, validator-surviving edit.
    for gid in _CHALLENGER_IDS:
        gdir = gens / gid
        assert (gdir / "experiment.json").exists(), gid
        assert (gdir / "snapshot" / "agent" / "agent.py").exists(), gid

    # --- The crowned challenger carries a promoted outcome under the racing
    # structure with a non-empty match_record; the dead branches carry
    # rejected outcomes. Every challenger's outcome is stamped "racing".
    for gid in _CHALLENGER_IDS:
        oc = json.loads((gens / gid / "experiment.json").read_text())["outcome"]
        assert oc["structure"] == "racing", gid
        if gid == crowned:
            assert oc["tournament_decision"] == "promoted"
            assert oc["match_record"], "crowned generation should carry a match audit"
        else:
            assert oc["tournament_decision"] == "rejected", gid

    # --- current_generation advanced to the crowned challenger only.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == crowned

    # --- Lineage records every challenger as a child of v0; crowned promoted.
    lineage = json.loads((workspace / "lineage.json").read_text())
    gens_nodes: list[dict[str, object]] = []
    for ep in lineage.get("epochs", []):
        if ep.get("id") == epoch_id:
            gens_nodes = ep.get("generations", [])
    by_id = {n["id"]: n for n in gens_nodes}
    for gid in _CHALLENGER_IDS:
        assert by_id[gid]["parent_id"] == "v0", gid
        assert by_id[gid]["promoted"] is (gid == crowned), gid

    # --- The live ActiveTournament envelope persisted with the racing
    # structure + the full competitor field + the rung records (the
    # successive-halving ladder) + final standings.
    from zicato.runtime.state import read_active_tournament

    active = read_active_tournament(workspace)
    assert active is not None
    assert active.structure == "racing"
    comp_ids = {c["generation_id"] for c in active.competitors}
    assert comp_ids == {"v0", *_CHALLENGER_IDS}
    assert active.rounds, "settled racing envelope should carry the rung records"
    standings_ids = {s["generation_id"] for s in active.standings}
    assert standings_ids == {"v0", *_CHALLENGER_IDS}

    # --- The rungs actually cut the field: at least one rung records a
    # non-empty `cut`, proving successive halving ran (not a single
    # full-board final). The match audit names a racing rung match id.
    all_cuts: list[str] = []
    rung_match_ids: list[str] = []
    for rnd in active.rounds:
        for m in rnd.get("matches", []):
            rung_match_ids.append(str(m.get("match_id", "")))
            all_cuts.extend(m.get("cut", []) or [])
    assert any(mid.startswith("rung") for mid in rung_match_ids), rung_match_ids
    assert all_cuts, "successive halving should eliminate at least one arm at a rung"
    assert "v4" in all_cuts, "the worst arm should be cut in an early rung"

    # --- The crowning duel is the full-board champion-gate against v0.
    crowned_oc = json.loads((gens / crowned / "experiment.json").read_text())["outcome"]
    opponents = {m["opponent"] for m in crowned_oc["match_record"]}
    assert "v0" in opponents, "the crowned arm's audit must include the champion-gate duel"


def test_presentation_racing_field_rejects_when_no_arm_beats_champion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the racing survivor cannot clear the full-board champion gate,
    the champion stands and every challenger is a dead branch — the example
    contract's promote_margin gate is the unchanged final arbiter."""
    workspace, epoch_id = _bootstrap_racing_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    # Every challenger regresses vs the champion (higher loss), so even the
    # racing survivor cannot clear the champion gate on the full board.
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 0.2, "v1": 1.0, "v2": 1.4, "v3": 1.8, "v4": 2.2},
        canned_pass_by_gen={gid: True for gid in ("v0", *_CHALLENGER_IDS)},
    )

    outcome = run_evolve_once(workspace, epoch_id, _make_example_aux_responder())

    assert outcome.tournament_decision == "rejected"

    # Champion stands — the promoted head is still v0 (no marker advance).
    from zicato.evolve.generation_phase import current_generation

    assert current_generation(workspace, epoch_id) == "v0"

    gens = workspace / "epochs" / epoch_id / "generations"
    for gid in _CHALLENGER_IDS:
        oc = json.loads((gens / gid / "experiment.json").read_text())["outcome"]
        assert oc["tournament_decision"] == "rejected", gid
        assert oc["structure"] == "racing", gid


def test_fast_racing_reuses_cached_champion_and_records_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fast mode under RACING reuses the champion's cached per-board scalars.

    With the champion's per-board ``loss.json`` already on disk (it was
    scored on the full board when it became champion), a fast racing round
    runs ONLY the challengers across every rung — the champion side is
    never executed — yet still produces the correct rung cuts + final gate.
    The resolved champion-eval mode is recorded in the journal for
    provenance.
    """
    workspace, epoch_id = _bootstrap_racing_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    # Pre-seed the champion (v0) cache the way a prior full round would —
    # BEFORE the reducer stub is installed (it imports the real writer).
    # The racing contract requests replicates=2, so a prior full round
    # would have persisted BOTH replicate slots for every champion unit;
    # seed both so the cache-first runner reuses the champion outright.
    _preseed_champion_cache(
        workspace, epoch_id, champion_id="v0", drift_loss=2.0, pass_fail=True, replicates=2
    )
    # Strictly-descending challenger losses keep the rung cuts deterministic;
    # v1 is the best arm.
    champion_runs: list[str] = []
    _install_caching_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.4, "v2": 0.8, "v3": 1.2, "v4": 1.6},
        canned_pass_by_gen={gid: True for gid in ("v0", *_CHALLENGER_IDS)},
        champion_run_log=champion_runs,
    )

    outcome = run_evolve_once(workspace, epoch_id, _make_example_aux_responder(), fast_mode=True)

    # --- The champion (v0) was NEVER executed this round — every run that
    # fired was a challenger run. The cached per-board scalars stood in.
    assert "v0" not in champion_runs, "fast racing must not re-run the cached champion"
    assert champion_runs, "the challengers still ran"

    # --- A challenger still won the rungs + cleared the gate, unchanged.
    assert outcome.tournament_decision == "promoted"
    assert outcome.proposed_generation_id == "v1"
    crowned = outcome.proposed_generation_id

    # --- Provenance: the resolved champion-eval mode is recorded in the
    # journal (every challenger's OutcomeRecord carries it). Cache hit on
    # every rung → "fast".
    gens = workspace / "epochs" / epoch_id / "generations"
    crowned_oc = json.loads((gens / crowned / "experiment.json").read_text())["outcome"]
    assert crowned_oc["champion_eval_mode"] == "fast"


def test_fast_racing_degrades_to_full_without_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fast racing with NO cached champion runs the champion once (degraded)."""
    workspace, epoch_id = _bootstrap_racing_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    champion_runs: list[str] = []
    # No champion cache pre-seeded → the seed champion has no aggregate yet,
    # so fast must degrade to a full champion run (and cache it).
    _install_caching_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.4, "v2": 0.8, "v3": 1.2, "v4": 1.6},
        canned_pass_by_gen={gid: True for gid in ("v0", *_CHALLENGER_IDS)},
        champion_run_log=champion_runs,
    )

    outcome = run_evolve_once(workspace, epoch_id, _make_example_aux_responder(), fast_mode=True)

    # The champion ran live at least once (cache miss → degrade-to-full).
    assert "v0" in champion_runs, "the seed champion with no cache must run once"
    assert outcome.tournament_decision == "promoted"
    gens = workspace / "epochs" / epoch_id / "generations"
    crowned_oc = json.loads(
        (gens / outcome.proposed_generation_id / "experiment.json").read_text()
    )["outcome"]
    assert crowned_oc["champion_eval_mode"] == "fast-degraded"


def test_fast_mode_does_not_change_contract_hash(tmp_path: Path) -> None:
    """Flipping fast↔full is a RUNTIME knob — it must NOT roll the epoch.

    The contract hash is computed over board + brief + scoring + harness
    identity; ``fast_mode`` is an ``evolve_once`` argument, never a
    contract input. The hash for a given scoring.json is therefore
    identical regardless of the champion-eval mode chosen at runtime.
    """
    workspace, epoch_id = _bootstrap_racing_workspace(tmp_path)
    from zicato.epoch.contract import compute_contract_hash, resolve_contract_inputs

    inputs = resolve_contract_inputs(workspace)
    hash_a = compute_contract_hash(inputs)
    # Re-resolving the SAME contract inputs (fast vs full does not touch any
    # of them) yields the identical hash — there is no fast/full knob in the
    # contract surface to perturb.
    hash_b = compute_contract_hash(resolve_contract_inputs(workspace))
    assert hash_a == hash_b
    # The scoring contract carries the tournament STRUCTURE but no
    # champion-eval mode field — fast is not contracted.
    scoring = json.loads(RACING_SCORING_PATH.read_text())
    assert "fast" not in json.dumps(scoring).lower().replace("fast_", "")
    assert "champion_eval_mode" not in json.dumps(scoring)
