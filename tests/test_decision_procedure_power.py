"""Operating characteristics of the DECISION PROCEDURE under seeded noise.

Tier 2 of the convergence harness: where ``test_convergence_known_answer``
proves the loop converges when every measurement is exact, this file
proves the decision procedure itself — margin gate, replication,
pass-rate monotonicity scope, and the Bradley--Terry evidence pre-gate —
has the right OPERATING CHARACTERISTICS when measurements are noisy, the
way they are in production (agent outputs vary, judges are LLMs).

Every trial is exactly reproducible: the noise model is the example
harness's own :func:`draw_measured_tokens`, seeded from the stable
identifier tuple ``(workspace seed, generation id, entry id, replicate
index)`` via :func:`stable_noise_seed`. Trials vary the workspace seed;
replicates vary the replicate index (stamped onto ``entry.context`` by
the replication loop); nothing derives from the clock or a global RNG —
so the "rates" asserted below are deterministic functions of the chosen
seeds, and the assertions are calibrated documentation of the procedure's
behaviour, not flaky statistics.

The statistical trials drive the REAL tournament machinery in-process —
``run_matchup`` (board-unit scheduling, replicate averaging, the
unchanged gate) and ``resolve_tournament`` (the gauntlet strategy + the
evidence pre-gate's defer→replicate loop) — swapping only the
subprocess-worker boundary ``runner._run_single`` (the test suite's
documented monkeypatch anchor) for an in-process evaluator built on the
SAME noise model, output synthesis, and REAL board predicates the noisy
adapter uses. One test at the bottom drives the actual
:class:`~zicato_examples.target_0_convergence.harness.NoisyPolicyAdapter`
through real subprocess workers to prove the seeded draw crosses the
process boundary intact.

Contracts under test
--------------------
* NAIVE (the shipped defaults): ``replicates=1``, fixed
  ``promote_margin=0.01``, per-entry pass-rate monotonicity, no evidence
  gate. One noisy sample decides the duel.
* EFFECTIVE: ``replicates=32`` (averaged — the same measurement budget
  the shipped racing example's ``promote_confidence_replicates: 32``
  buys), ``aggregate``-scope pass-rate monotonicity (the documented
  policy for sampled/noisy boards), and the Bradley--Terry evidence
  pre-gate (crown only at ``P(theta_child > theta_champion) >= 0.8``
  with SEPARATED rating CIs, defer→replicate up to the budget, terminal
  ``inconclusive`` otherwise).

A load-bearing measured fact about the shipped pre-gate: on a
two-contestant field the prior-regularised Bradley--Terry CIs only
separate after ~37 duels of an essentially unbroken win streak — ANY
mixed record never separates. The pre-gate is therefore a pure SOUNDNESS
device (noise cannot manufacture 37 consistent wins), and the POWER to
resolve a small true effect must be bought with replication: the
effective contract's per-duel averaging is what turns a 0.5x-floor
effect into a ~3-sigma-per-duel effect the win streak can actually
sustain. The tests below pin both halves of that trade.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import statistics
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import zicato.tournament.runner as runner_mod
import zicato.tournament.scheduling as scheduling_mod
import zicato_examples.target_0_convergence as _t0_pkg
from zicato.board.jsonl import load_board
from zicato.core import (
    BoardEntry,
    Generation,
    LossProfile,
    RuntimeConfig,
    ScoringWeights,
)
from zicato.core.types import DriftCount, ExpectationResult
from zicato.import_path import import_dotted_path
from zicato.selection.driver import EvidencePreGate, resolve_tournament
from zicato.selection.strategies.gauntlet import GauntletStrategy
from zicato.selection.strategy import Contestant, Matchup, MatchupResult, SelectionDecision
from zicato.tournament.runner import run_matchup
from zicato_examples.target_0_convergence import mocks as t0_mocks
from zicato_examples.target_0_convergence.harness import (
    GENERATION_ID_CONTEXT_KEY,
    REPLICATE_INDEX_CONTEXT_KEY,
    draw_measured_tokens,
    make_noisy_adapter,
    stable_noise_seed,
    synthesize_output,
)

EXAMPLE_DIR = Path(_t0_pkg.__file__).resolve().parent
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"

# ---------------------------------------------------------------------------
# The noise level + the planted effects, in MEASURED scalar units.
#
# With per-known-defect flip probability sigma, a slot whose true presence
# is p is MEASURED present with probability p + sigma*(1 - 2p), so a true
# per-token effect of 1.2 (one drift frame on all 5 entries + one predicate)
# is measured as 1.2*(1 - 2*sigma). The A/A noise floor (sd of the null
# delta_scalar) is analytically 1.6*sqrt(sigma*(1-sigma)). sigma = 0.22 is
# chosen so ONE full defect fix lands at ~1x the floor:
#
#   floor ~ 1.6*sqrt(0.22*0.78) = 0.663
#   one full token measured    = 1.2*0.56 = 0.672   (~1.0x)
#   sometimes-50 half a token  = 0.336               (~0.5x)
#   all three tokens           = 2.016               (~3.0x)
# ---------------------------------------------------------------------------

NOISE_SIGMA = 0.22

#: The champion's true defect tokens — exactly the Tier-1 seeded v0 policy.
BASE_TOKENS = ("verbose-prose", "omit-summary", "skip-citations")

#: Challenger token sets per planted effect, with the measured delta each
#: plants (see the arithmetic above).
DELTA_CASES: dict[str, tuple[tuple[str, ...], float]] = {
    # ~0.5x floor: half-fix one defect (it now manifests only half the time).
    "small": (("verbose-prose", "omit-summary", "sometimes-50-skip-citations"), 0.336),
    # ~1x floor: fully fix one defect.
    "medium": (("verbose-prose", "omit-summary"), 0.672),
    # ~3x floor: fix all three defects.
    "large": ((), 2.016),
}

#: The NAIVE default contract — the shipped ScoringWeights defaults:
#: replicates=1 (run_matchup's default), promote_margin=0.01, per-entry
#: pass-rate monotonicity, no evidence gate.
NAIVE_WEIGHTS = ScoringWeights()

#: The EFFECTIVE contract's weights: same margin, but aggregate-scope
#: monotonicity — the documented policy for sampled/noisy boards, where a
#: single noise-flipped entry must not veto a genuinely better challenger.
EFFECTIVE_WEIGHTS = ScoringWeights(pass_rate_monotonicity_scope="aggregate")

#: Effective-contract knobs. ``replicates=32`` averages every duel (the
#: crowning duel AND each evidence duel) — with the A/A noise floor at
#: ~0.66 per single sample, averaging 32 shrinks the per-duel delta sd to
#: ~0.12, which is what makes the 0.5x-floor effect (~0.34) a ~3-sigma
#: per-duel signal. The budget of 38 evidence duels is what the shipped
#: CI-separation rule actually costs: two-contestant CIs first separate
#: at 37 total duels of an unbroken win streak (the racing example's
#: ``promote_confidence_replicates: 32`` is this same bill), so a real
#: effect crowns at ~36 spends and a null burns the budget and terminates
#: ``inconclusive``.
EFFECTIVE_REPLICATES = 32
EFFECTIVE_THRESHOLD = 0.8
EFFECTIVE_BUDGET = 38

#: Trial counts. Deterministic given the seeds; sized to keep the whole
#: file's runtime small while making the measured rates meaningful. The
#: noise-floor measurement is cheap (single-sample duels) so it takes the
#: larger N; each effective-procedure trial runs up to ~39 replicated
#: duels (~12k board units in-process), so those take the smaller N.
AA_TRIALS = 60
AA_EFFECTIVE_TRIALS = 24
POWER_TRIALS = 12


@lru_cache(maxsize=1)
def _board() -> tuple[BoardEntry, ...]:
    return tuple(load_board(BOARD_PATH))


@lru_cache(maxsize=8)
def _predicate(spec: str) -> Any:
    return import_dotted_path(spec, label="board predicate")


def _gen(gen_id: str) -> Generation:
    return Generation(
        id=gen_id,
        epoch_id="e0",
        parent_id=None,
        snapshot_root=Path(f"/nonexistent/{gen_id}"),
        created_at="2026-01-01T00:00:00Z",
    )


def _config(workspace: Path, seed: int) -> RuntimeConfig:
    async def harness_call(system: str, user: str, model: str) -> str:
        return ""

    async def aux_call(system: str, user: str, model: str) -> str:
        return ""

    return RuntimeConfig(
        instance_id="test",
        workspace_root=workspace,
        harness_call_llm=harness_call,
        auxiliary_call_llm=aux_call,
        seed=seed,
        parallelism=8,
    )


class _NoisyWorld:
    """In-process stand-in for the subprocess worker, on the SAME noise model.

    Replaces ``runner._run_single`` (the suite's documented monkeypatch
    anchor) with an evaluator that reproduces exactly what a noisy-adapter
    worker run reduces to: draw the measured tokens with
    :func:`draw_measured_tokens` seeded from ``(config.seed, generation id,
    entry id, replicate index)``, synthesize the REAL output with
    :func:`synthesize_output`, evaluate the entry's REAL predicate on it,
    and score one info-severity drift frame per measured token (the exact
    reduction Tier 1 pinned end-to-end). The replicate index is read from
    ``entry.context`` — the same stamp the real worker path consumes — so
    the production replication threading is exercised, not bypassed.
    """

    def __init__(self, tokens_by_gen: dict[str, tuple[str, ...]], sigma: float) -> None:
        self.tokens_by_gen = dict(tokens_by_gen)
        self.sigma = float(sigma)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner_mod, "_run_single", self._fake_run_single)
        # The dashboard-facing live-state appends and the per-unit cache
        # persist are best-effort side channels orthogonal to the decision
        # procedure; silencing them keeps thousands of seeded trials lean.
        monkeypatch.setattr(scheduling_mod, "_runtime_state", lambda: None)
        monkeypatch.setattr(scheduling_mod, "_persist_unit_loss", lambda **_kw: None)

    async def _fake_run_single(
        self,
        *,
        adapter: Any,
        generation: Generation,
        entry: BoardEntry,
        weights: ScoringWeights,
        config: RuntimeConfig,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, workspace_root, side, match_id
        replicate = int(dict(entry.context).get(REPLICATE_INDEX_CONTEXT_KEY, "0") or 0)
        seed = stable_noise_seed(
            workspace_seed=int(config.seed or 0),
            generation_key=generation.id,
            entry_id=entry.id,
            replicate_index=replicate,
        )
        measured = draw_measured_tokens(
            list(self.tokens_by_gen[generation.id]), random.Random(seed), self.sigma
        )
        output = synthesize_output(str(entry.input or ""), measured)
        assert entry.expectation is not None
        passed = bool(_predicate(entry.expectation.spec)(SimpleNamespace(final_output=output)))
        return LossProfile(
            run_id=f"{generation.id}--{entry.id}--r{replicate}",
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id=epoch_id,
            drift_counts=(
                DriftCount(kind="unexpected_output", severity="info", count=len(measured)),
            ),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=1,
            wall_clock_budget_exceeded=False,
            expectation_result=ExpectationResult(kind="predicate", passed=passed),
            drift_loss=float(len(measured)),
            pass_fail=passed,
        )


def _naive_outcome(workspace: Path, seed: int, weights: ScoringWeights) -> Any:
    """One single-sample duel under the naive contract; returns the GateOutcome."""
    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen("champion"),
            right_gen=_gen("challenger"),
            board=list(_board()),
            weights=weights,
            config=_config(workspace, seed),
            workspace_root=workspace,
            epoch_id="e0",
        )
    )
    return result.outcome


def _effective_decision(
    workspace: Path,
    trial: int,
    weights: ScoringWeights = EFFECTIVE_WEIGHTS,
) -> SelectionDecision:
    """One trial of the EFFECTIVE decision procedure, end to end.

    Drives the REAL driver: gauntlet strategy (replicates=4 crowning duel)
    through ``resolve_tournament`` with the Bradley--Terry pre-gate and a
    replicate-duel runner mirroring the orchestrator's (one extra
    single-replicate duel of the crowning pair per defer). Each duel of
    the trial draws fresh seeded noise by advancing the workspace seed —
    the deterministic analogue of an LLM re-sampling on every re-run.
    """
    champion = Contestant(generation_id="champion", role="champion")
    challenger = Contestant(generation_id="challenger", role="challenger")
    duel_counter = itertools.count()

    async def _request_field(n: int) -> tuple[Contestant, list[Contestant]]:
        del n
        return champion, [challenger]

    async def _run(m: Matchup) -> MatchupResult:
        config = _config(workspace, trial * 10_000 + next(duel_counter))
        result = await run_matchup(
            adapter=object(),
            left_gen=_gen(m.left.generation_id),
            right_gen=_gen(m.right.generation_id),
            board=list(_board()),
            weights=weights,
            config=config,
            workspace_root=workspace,
            epoch_id="e0",
            replicates=m.replicates,
            match_id=m.matchup_id,
        )
        return MatchupResult(
            matchup_id=m.matchup_id,
            left_id=m.left.generation_id,
            right_id=m.right.generation_id,
            left_agg=result.parent_agg,
            right_agg=result.child_agg,
            outcome=result.outcome,
        )

    async def _replicate_duel(left_id: str, right_id: str) -> MatchupResult:
        # Mirrors the orchestrator's replicate-duel wiring — one extra
        # crowning-pair duel through the SAME runner + gate — except each
        # evidence duel carries the contract's replication too: the
        # pre-gate's CI separation needs a ~37-duel win streak, and only a
        # replicated (low-variance) duel makes that streak sustainable for
        # a small true effect. This is the deterministic analogue of the
        # racing contract's ``promote_confidence_replicates`` measurement
        # budget.
        return await _run(
            Matchup(
                matchup_id=f"bt-replicate:{left_id}:{right_id}",
                left=Contestant(generation_id=left_id, role="champion"),
                right=Contestant(generation_id=right_id, role="challenger"),
                replicates=EFFECTIVE_REPLICATES,
            )
        )

    strategy = GauntletStrategy({"replicates": EFFECTIVE_REPLICATES})
    return asyncio.run(
        resolve_tournament(
            strategy,
            request_field=_request_field,
            run_matchup=_run,
            pre_gate=EvidencePreGate(
                threshold=EFFECTIVE_THRESHOLD, replicate_budget=EFFECTIVE_BUDGET
            ),
            replicate_duel=_replicate_duel,
        )
    )


def _aa_world() -> dict[str, tuple[str, ...]]:
    """Champion vs itself: identical true tokens under two generation ids."""
    return {"champion": BASE_TOKENS, "challenger": BASE_TOKENS}


def _measure_noise_floor(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> tuple[float, float]:
    """Run the A/A duels under the naive contract; return (sd, max_abs)."""
    _NoisyWorld(_aa_world(), NOISE_SIGMA).install(monkeypatch)
    deltas = [
        _naive_outcome(workspace, seed=trial, weights=NAIVE_WEIGHTS).delta_scalar
        for trial in range(AA_TRIALS)
    ]
    return statistics.pstdev(deltas), max(abs(d) for d in deltas)


# ---------------------------------------------------------------------------
# A/A null calibration
# ---------------------------------------------------------------------------


def test_aa_null_calibration_measures_the_noise_floor(monkeypatch, tmp_path):
    """A generation dueling ITSELF: the A/A delta spread IS the noise floor.

    Identical true trees under two generation ids draw independent noise
    (the seed includes the generation id), so the naive single-sample
    delta_scalar is a pure noise variable. Its spread — recorded and
    printed here — is the floor every later effect size is compared to.
    """
    floor_sd, max_abs = _measure_noise_floor(monkeypatch, tmp_path)
    print(
        f"\n[A/A null calibration] trials={AA_TRIALS} sigma={NOISE_SIGMA} "
        f"noise floor (sd of A/A delta_scalar) = {floor_sd:.4f}, "
        f"max |delta| = {max_abs:.4f} (analytic sd ~ 0.663)"
    )
    # The measured floor must be a real, nonzero noise scale in the
    # neighbourhood the analytic model predicts (0.663); a floor of ~0
    # would mean the draws stopped varying (a seeding regression), a wild
    # floor would mean the noise model broke.
    assert 0.4 <= floor_sd <= 1.0
    # Independent draws per side: at least one trial must land nonzero.
    assert max_abs > 0.0


def test_aa_effective_contract_false_promotion_rate_is_zero(monkeypatch, tmp_path):
    """The evidence-gated contract does not promote a generation over itself.

    Under the effective contract every A/A trial must end with the
    champion standing: either the replicated crowning duel already fails
    the margin gate, or the pre-gate's defer→replicate loop fails to find
    P(theta_child > theta_champion) >= 0.8 with separated CIs and goes
    terminally inconclusive (folded to DEFERRED — kept for analysis, the
    lineage head unchanged). Deterministic over these seeded trials.
    """
    _NoisyWorld(_aa_world(), NOISE_SIGMA).install(monkeypatch)
    decisions = [
        _effective_decision(tmp_path, trial).decision for trial in range(AA_EFFECTIVE_TRIALS)
    ]
    false_promotions = sum(1 for d in decisions if d == "promoted")
    print(
        f"\n[A/A effective] trials={AA_EFFECTIVE_TRIALS} false promotions={false_promotions} "
        f"(decisions: { {d: decisions.count(d) for d in set(decisions)} })"
    )
    assert false_promotions == 0


# ---------------------------------------------------------------------------
# Margin-vs-noise invariant
# ---------------------------------------------------------------------------


def test_margin_below_noise_floor_without_evidence_gate_is_unsound(monkeypatch, tmp_path):
    """promote_margin < noise floor + no evidence gate ⇒ noise alone promotes.

    The unsound configuration: with the margin (0.01) far below the
    measured noise floor (~0.66) and no evidence requirement, a pure-noise
    A/A challenger clears the gate in a substantial fraction of seeded
    trials — pass-rate monotonicity is disabled here so the demonstration
    isolates the margin rule itself. The SAME trials under the evidence
    gate promote never: noise cannot manufacture three consistent,
    CI-separated wins.
    """
    floor_sd, _ = _measure_noise_floor(monkeypatch, tmp_path)
    margin_only = ScoringWeights(pass_rate_monotonicity=False)
    assert margin_only.promote_margin < floor_sd, "the premise: margin below the floor"

    _NoisyWorld(_aa_world(), NOISE_SIGMA).install(monkeypatch)
    unsound_promotions = sum(
        1
        for trial in range(AA_TRIALS)
        if _naive_outcome(tmp_path, seed=trial, weights=margin_only).decision == "promoted"
    )
    gated_promotions = sum(
        1
        for trial in range(AA_EFFECTIVE_TRIALS)
        if _effective_decision(tmp_path, trial, weights=margin_only).decision == "promoted"
    )
    print(
        f"\n[margin-vs-noise] margin={margin_only.promote_margin} "
        f"< floor={floor_sd:.4f}: unsound-config noise promotions="
        f"{unsound_promotions}/{AA_TRIALS}, "
        f"evidence-gated promotions={gated_promotions}/{AA_EFFECTIVE_TRIALS}"
    )
    # Noise alone clears an under-margined gate in a large fraction of
    # trials (the binomial null is ~50% minus the sliver the margin trims).
    assert unsound_promotions >= AA_TRIALS // 4
    # The evidence gate, over the same seeded noise, never crowns.
    assert gated_promotions == 0


# ---------------------------------------------------------------------------
# Power at planted effects
# ---------------------------------------------------------------------------


def _power(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    challenger_tokens: tuple[str, ...],
    *,
    effective: bool,
) -> float:
    """Promotion rate over POWER_TRIALS seeded trials for one planted delta."""
    world = {"champion": BASE_TOKENS, "challenger": challenger_tokens}
    _NoisyWorld(world, NOISE_SIGMA).install(monkeypatch)
    promoted = 0
    for trial in range(POWER_TRIALS):
        if effective:
            decision = _effective_decision(workspace, trial).decision
        else:
            decision = _naive_outcome(workspace, seed=trial, weights=NAIVE_WEIGHTS).decision
        promoted += 1 if decision == "promoted" else 0
    return promoted / POWER_TRIALS


def test_power_at_planted_deltas(monkeypatch, tmp_path):
    """The effective contract's power curve over 0.5x / 1x / 3x-floor effects.

    The planted true improvements land (in measured scalar units) at about
    half, one, and three times the A/A noise floor. The effective contract
    must promote the unmissable 3x effect in every seeded trial, and its
    power must be monotone in the effect size.
    """
    floor_sd, _ = _measure_noise_floor(monkeypatch, tmp_path)
    rates: dict[str, float] = {}
    for name, (tokens, measured_delta) in DELTA_CASES.items():
        rate = _power(monkeypatch, tmp_path, tokens, effective=True)
        rates[name] = rate
        print(
            f"\n[power/effective] case={name} measured-delta={measured_delta:.3f} "
            f"(~{measured_delta / floor_sd:.2f}x floor {floor_sd:.3f}) "
            f"power={rate:.2f} over {POWER_TRIALS} trials"
        )
    # The planted effects really do sit near their advertised multiples of
    # the measured floor (loose bands: the floor itself is an estimate).
    assert 0.3 <= DELTA_CASES["small"][1] / floor_sd <= 0.8
    assert 0.7 <= DELTA_CASES["medium"][1] / floor_sd <= 1.5
    assert 2.0 <= DELTA_CASES["large"][1] / floor_sd <= 5.0
    # A 3x-floor effect is unmissable: promoted on every seeded trial.
    assert rates["large"] == 1.0
    # Power is monotone in the effect size.
    assert rates["small"] <= rates["medium"] <= rates["large"]


def test_naive_default_misses_small_effects_the_evidence_gate_catches(monkeypatch, tmp_path):
    """Executable documentation: why the effective contract exists.

    A true improvement of ~0.5x the noise floor is real but small. The
    naive default contract (one sample, fixed margin, per-entry
    monotonicity, no evidence) rejects it in most seeded trials — a noisy
    single sample regularly measures the better challenger as worse, and
    one noise-flipped entry vetoes it besides. The effective contract
    (replication + aggregate monotonicity + evidence loop) recovers a
    large fraction of exactly those trials. Same seeds, same noise model,
    same planted effect — only the decision procedure differs.
    """
    small_tokens = DELTA_CASES["small"][0]
    naive_rate = _power(monkeypatch, tmp_path, small_tokens, effective=False)
    effective_rate = _power(monkeypatch, tmp_path, small_tokens, effective=True)
    print(
        f"\n[naive-vs-effective @ small delta] naive={naive_rate:.2f} "
        f"effective={effective_rate:.2f} over {POWER_TRIALS} seeded trials"
    )
    # The naive default demonstrably fails the small-effect case: it
    # misses the true improvement in at least half the trials.
    assert naive_rate <= 0.5
    # The effective procedure demonstrably catches what the naive one
    # misses: a decisively higher promotion rate on the same trials.
    assert effective_rate >= naive_rate + 0.25
    # And the effective procedure remains sound (see the A/A tests): its
    # extra power comes from evidence, not from a looser gate.


# ---------------------------------------------------------------------------
# The real adapter through real subprocess workers
# ---------------------------------------------------------------------------


def _write_snapshot(root: Path, tokens: tuple[str, ...]) -> None:
    agent_dir = root / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "policy.py").write_text(
        '"""Test policy for the noisy harness."""\n\n' f'STYLE_RULES = "{"; ".join(tokens)}"\n',
        encoding="utf-8",
    )


def _real_gen(tmp_path: Path, gen_id: str, tokens: tuple[str, ...]) -> Generation:
    snapshot = tmp_path / f"snap_{gen_id}"
    _write_snapshot(snapshot, tokens)
    return Generation(
        id=gen_id,
        epoch_id="e0",
        parent_id=None,
        snapshot_root=snapshot,
        created_at="2026-01-01T00:00:00Z",
    )


def _worker_config(workspace: Path, seed: int) -> RuntimeConfig:
    # Module-level callables: the subprocess worker re-imports each role
    # by dotted path, so a closure-local callable would be rejected.
    return RuntimeConfig(
        instance_id="test",
        workspace_root=workspace,
        harness_call_llm=t0_mocks.harness_llm,
        auxiliary_call_llm=t0_mocks.aux_llm,
        seed=seed,
        parallelism=4,
    )


def test_noisy_adapter_seeded_draws_cross_the_worker_boundary(tmp_path):
    """The REAL noisy adapter through REAL subprocess workers, twice.

    One replicated A/A duel (identical policy trees, two generation ids,
    replicates=2, a 2-entry board slice) is run through the full
    subprocess path — worker spawn, ``worker_spec()`` reconstruction with
    the sigma in its ``args``, goldfive frames, the real reducer — and
    then run AGAIN in a fresh workspace with the same workspace seed.

    Asserts the three seeded-noise properties end to end:

    * REPRODUCIBLE — the two independent executions produce byte-equal
      per-entry losses and aggregates (the seed derives only from stable
      identifiers, so nothing about process ids, tempdir names, or the
      clock leaks into the measurement);
    * side-INDEPENDENT — the two generation ids draw different noise even
      over identical trees (the A/A premise);
    * replicate-INDEPENDENT — replicate 0 and replicate 1 of the same
      unit draw differently (the production ``replicate_index`` stamp
      survives the runner -> args-file -> worker -> adapter round-trip).
    """
    subset = ("conv_summary", "conv_no_fabrication")
    sigma = 0.35
    adapter = make_noisy_adapter({"noise_sigma": sigma})

    def _duel(workspace: Path) -> Any:
        workspace.mkdir()
        return asyncio.run(
            run_matchup(
                adapter=adapter,
                left_gen=_real_gen(workspace, "aa-left", BASE_TOKENS),
                right_gen=_real_gen(workspace, "aa-right", BASE_TOKENS),
                board=list(_board()),
                weights=NAIVE_WEIGHTS,
                config=_worker_config(workspace, seed=7),
                workspace_root=workspace,
                epoch_id="e0",
                board_subset=subset,
                replicates=2,
                match_id="noisy-aa",
            )
        )

    first = _duel(tmp_path / "ws1")
    second = _duel(tmp_path / "ws2")

    # REPRODUCIBLE: independent executions, identical measurements.
    assert first.parent_agg["scalar"] == second.parent_agg["scalar"]
    assert first.child_agg["scalar"] == second.child_agg["scalar"]
    for entry_id in subset:
        f_left, f_right = first.per_entry_losses[entry_id]
        s_left, s_right = second.per_entry_losses[entry_id]
        assert f_left.drift_loss == s_left.drift_loss
        assert f_right.drift_loss == s_right.drift_loss
        assert f_left.pass_fail == s_left.pass_fail
        assert f_right.pass_fail == s_right.pass_fail

    # side-INDEPENDENT: identical trees, different generation ids ⇒ the
    # replicate-averaged per-entry measurements differ somewhere.
    left_view = [
        (eid, first.per_entry_losses[eid][0].drift_loss, first.per_entry_losses[eid][0].pass_fail)
        for eid in subset
    ]
    right_view = [
        (eid, first.per_entry_losses[eid][1].drift_loss, first.per_entry_losses[eid][1].pass_fail)
        for eid in subset
    ]
    assert left_view != right_view

    # replicate-INDEPENDENT: the two replicate cache slots of at least one
    # unit differ — replicate 1's stamped index reached the worker.
    from zicato.tournament.unit_cache import _resolve_cached_unit

    replicate_pairs = []
    for gen_id in ("aa-left", "aa-right"):
        for entry_id in subset:
            r0 = _resolve_cached_unit(
                workspace_root=tmp_path / "ws1",
                epoch_id="e0",
                generation_id=gen_id,
                entry_id=entry_id,
                replicate_index=0,
            )
            r1 = _resolve_cached_unit(
                workspace_root=tmp_path / "ws1",
                epoch_id="e0",
                generation_id=gen_id,
                entry_id=entry_id,
                replicate_index=1,
            )
            assert r0 is not None and r1 is not None
            replicate_pairs.append((r0.drift_loss, r0.pass_fail, r1.drift_loss, r1.pass_fail))
    assert any(
        (d0, p0) != (d1, p1) for d0, p0, d1, p1 in replicate_pairs
    ), f"replicates drew identically everywhere: {replicate_pairs}"


# ---------------------------------------------------------------------------
# Session-level seed derivation (fast, no workers)
# ---------------------------------------------------------------------------


def test_noisy_session_seed_derives_only_from_stable_identifiers(tmp_path):
    """Same identifiers ⇒ identical run; any component change ⇒ a fresh draw.

    Drives the noisy session directly (no sinks, no worker) with the
    context keys stamped the way the runner stamps them, and checks the
    seed tuple's four components each independently move the draw while
    everything ambient (process, path, call count) moves nothing.
    """
    snapshot = tmp_path / "snap"
    _write_snapshot(snapshot, BASE_TOKENS)
    adapter = make_noisy_adapter({"noise_sigma": 0.35})
    session = adapter.load(snapshot)

    def _run(seed: int, gen: str, replicate: str) -> tuple[str, ...]:
        # A whole-board vector of outputs: one entry's measured token set
        # has only 2^4 possibilities, so two independent seeds can collide
        # on a single entry — over five entries a collision means the draw
        # genuinely did not move.
        outputs: list[str] = []
        for board_entry in _board():
            entry = replace(
                board_entry,
                context={
                    GENERATION_ID_CONTEXT_KEY: gen,
                    REPLICATE_INDEX_CONTEXT_KEY: replicate,
                },
            )
            result = asyncio.run(session.run(entry, [], SimpleNamespace(seed=seed)))
            outputs.append(str(result.final_output))
        return tuple(outputs)

    base = _run(1, "vA", "0")
    # Ambient-free: repeating the identical coordinate reproduces the run.
    assert _run(1, "vA", "0") == base
    # Each identifier component independently re-seeds the draw; all are
    # pinned so a seeding regression in any single component fails loudly.
    variants = {
        "workspace seed": _run(2, "vA", "0"),
        "generation id": _run(1, "vB", "0"),
        "replicate index": _run(1, "vA", "1"),
    }
    changed = {name: out != base for name, out in variants.items()}
    assert all(changed.values()), f"stale draws: {changed}"
    # sigma=0 degrades to the deterministic harness exactly.
    det_session = make_noisy_adapter({"noise_sigma": 0.0}).load(snapshot)
    det_entry = replace(
        next(e for e in _board() if e.id == "conv_summary"),
        context={GENERATION_ID_CONTEXT_KEY: "vA"},
    )
    det = asyncio.run(det_session.run(det_entry, [], SimpleNamespace(seed=1)))
    plain = asyncio.run(
        make_noisy_adapter(None).load(snapshot).run(det_entry, [], SimpleNamespace(seed=99))
    )
    assert det.final_output == plain.final_output
