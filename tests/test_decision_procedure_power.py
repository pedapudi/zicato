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
    TournamentDecision,
)
from zicato.core.types import DriftCount, ExpectationResult, TournamentStructure
from zicato.core.workspace import loss_profile_path
from zicato.import_path import import_dotted_path
from zicato.selection.driver import EvidencePreGate, resolve_tournament
from zicato.selection.evidence_gate import EVIDENCE_REPLICATE_BASE
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

    def install(self, monkeypatch: pytest.MonkeyPatch, *, persist: bool = False) -> None:
        monkeypatch.setattr(runner_mod, "_run_single", self._fake_run_single)
        # The dashboard-facing live-state appends and the per-unit cache
        # persist are best-effort side channels orthogonal to the decision
        # procedure; silencing them keeps thousands of seeded trials lean.
        # ``persist=True`` keeps the REAL per-unit cache persistence so the
        # canonical-slot-integrity tests can watch the ``loss.json`` files.
        monkeypatch.setattr(scheduling_mod, "_runtime_state", lambda: None)
        if not persist:
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

    evidence_counter = itertools.count()

    async def _replicate_duel(left_id: str, right_id: str) -> MatchupResult:
        # Mirrors the orchestrator's replicate-duel wiring — one extra
        # crowning-pair duel through the SAME runner + gate — except each
        # evidence duel carries the contract's replication too: the
        # pre-gate's CI separation needs a ~37-duel win streak, and only a
        # replicated (low-variance) duel makes that streak sustainable for
        # a small true effect. This is the deterministic analogue of the
        # racing contract's ``promote_confidence_replicates`` measurement
        # budget. Per the ReplicateDuel contract each call mints a UNIQUE
        # matchup id (the driver's audit guard drops re-presented ids); the
        # per-duel INDEPENDENCE that production buys with the reserved
        # replicate base (EVIDENCE_REPLICATE_BASE + j) is supplied here by
        # advancing the workspace seed per duel — the harness fakes the
        # worker boundary and persists nothing, so no cache slot exists to
        # collide with. The measured numbers below are therefore unchanged
        # by the reserved-base fix: this harness always drew fresh.
        return await _run(
            Matchup(
                matchup_id=f"bt-replicate:{next(evidence_counter)}:{left_id}:{right_id}",
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
# The PRODUCTION evidence-replicate wiring, under seeded noise
#
# The orchestrator's gauntlet confirm (`_confirm_gauntlet_promotion`) is the
# real seam the evolve loop drives: its replicate duels must be INDEPENDENT
# samples — each at a reserved replicate index (EVIDENCE_REPLICATE_BASE + j),
# both sides drawn fresh — never cache replays of, or force-fresh clobbers
# over, the canonical replicate-0 slots the tournament scored.
# ---------------------------------------------------------------------------


def _seed_crowning_decision(
    workspace: Path, *, seed: int, fast: bool
) -> tuple[SelectionDecision, Any]:
    """Run one real crowning duel and wrap it as a promoted decision.

    The decision is FORCED to ``promoted`` (the confirm only adjudicates
    promotions); under the A/A world the pre-gate must then hold it, and the
    audit trail it accumulates is the object under test.
    """
    result = asyncio.run(
        run_matchup(
            adapter=object(),
            left_gen=_gen("champion"),
            right_gen=_gen("challenger"),
            board=list(_board()),
            weights=NAIVE_WEIGHTS,
            config=_config(workspace, seed),
            workspace_root=workspace,
            epoch_id="e0",
            match_id="crowning",
            fast=fast,
        )
    )
    crowning = MatchupResult(
        matchup_id="crowning",
        left_id="champion",
        right_id="challenger",
        left_agg=result.parent_agg,
        right_agg=result.child_agg,
        outcome=result.outcome,
    )
    decision = SelectionDecision(
        promoted_generation_id="challenger",
        decision=TournamentDecision.PROMOTED,
        reason="forced promote for the evidence loop",
        matchups=(crowning,),
        crowning_matchup_id="crowning",
    )
    return decision, result


def _confirm(
    workspace: Path,
    decision: SelectionDecision,
    *,
    seed: int,
    fast_mode: bool,
    budget: int,
) -> tuple[Any, dict[str, Any] | None]:
    from zicato.evolve.gate import _confirm_gauntlet_promotion

    spec = TournamentStructure(
        structure="gauntlet",
        params={
            "promote_confidence_threshold": EFFECTIVE_THRESHOLD,
            "promote_confidence_replicates": budget,
        },
    )
    return asyncio.run(
        _confirm_gauntlet_promotion(
            decision,
            tournament_spec=spec,
            adapter=object(),
            parent_gen=_gen("champion"),
            child_gen=_gen("challenger"),
            train_board=list(_board()),
            weights=NAIVE_WEIGHTS,
            config=_config(workspace, seed),
            workspace_root=workspace,
            epoch_id="e0",
            disable_drift=(),
            judge_only=False,
            fast_mode=fast_mode,
            round_index=0,
            total_rounds=1,
            beater=None,
        )
    )


def test_evidence_replicates_are_independent_draws(monkeypatch, tmp_path):
    """Consecutive evidence replicates are DISTINCT noise draws of BOTH sides.

    Fast mode, A/A world: before the reserved-base fix every "replicate" ran
    at replicate slot 0 — the same stamped index ⇒ the same seeded draw (and,
    with a warm cache, a byte-identical replay) — so the audit's deltas had
    ZERO variance and repetition alone shrank the Bradley--Terry SE. Now each
    replicate ``j`` runs at ``EVIDENCE_REPLICATE_BASE + j`` under a matchup
    id that encodes the slot, and the deltas — and the CHAMPION's own
    scalars — genuinely vary.
    """
    _NoisyWorld(_aa_world(), NOISE_SIGMA).install(monkeypatch)
    decision, _ = _seed_crowning_decision(tmp_path, seed=101, fast=True)
    budget = 6
    confirmed, evidence = _confirm(tmp_path, decision, seed=101, fast_mode=True, budget=budget)

    # A/A never separates within a small budget ⇒ the hold is terminal and
    # the full audit (crowning + every replicate) is stamped on the decision.
    assert confirmed.decision == TournamentDecision.DEFERRED
    assert evidence is not None
    assert len(evidence["ci_history"]) == budget + 1

    replicates = list(confirmed.matchups[1:])
    assert len(replicates) == budget
    # Every replicate ran at its own RESERVED slot, encoded in the id.
    assert [r.matchup_id for r in replicates] == [
        f"bt-replicate:r{EVIDENCE_REPLICATE_BASE + j}:champion:challenger" for j in range(budget)
    ]
    # (a) Distinct draws: the audit deltas have variance > 0.
    deltas = [r.outcome.delta_scalar for r in replicates]
    assert len(set(deltas)) > 1, f"evidence replicates drew identically: {deltas}"
    # (c) The CHAMPION side is re-drawn per replicate, not replayed.
    champion_scalars = {float(r.left_agg["scalar"]) for r in replicates}
    assert len(champion_scalars) > 1, f"champion never re-drawn: {champion_scalars}"


def test_full_mode_evidence_loop_never_touches_canonical_slots(monkeypatch, tmp_path):
    """(b) The child's (and champion's) canonical ``loss.json`` is
    byte-identical across the evidence loop, and the evidence draws persist
    under the reserved base instead.

    Full mode: before the fix each replicate force-fresh re-ran at slot 0
    and RE-PERSISTED there, clobbering the canonical files that reindex and
    crash-resume key on. The confirm below runs under a DIFFERENT workspace
    seed than the seeding duel (the deterministic analogue of an LLM
    re-sampling on a later re-run), so any slot-0 rewrite would change the
    bytes.
    """
    _NoisyWorld(_aa_world(), NOISE_SIGMA).install(monkeypatch, persist=True)
    decision, _ = _seed_crowning_decision(tmp_path, seed=1, fast=False)

    canonical: dict[tuple[str, str], bytes] = {}
    for gid in ("champion", "challenger"):
        for entry in _board():
            path = loss_profile_path(tmp_path, "e0", gid, entry.id)
            canonical[(gid, entry.id)] = path.read_bytes()

    budget = 4
    confirmed, _evidence = _confirm(tmp_path, decision, seed=2, fast_mode=False, budget=budget)
    assert confirmed.decision == TournamentDecision.DEFERRED  # A/A: held

    # Canonical replicate-0 slots: byte-identical before/after the loop.
    for (gid, entry_id), before in canonical.items():
        after = loss_profile_path(tmp_path, "e0", gid, entry_id).read_bytes()
        assert after == before, f"canonical loss.json clobbered for {gid}/{entry_id}"

    # The evidence draws persisted under the RESERVED base — for BOTH sides.
    for gid in ("champion", "challenger"):
        for j in range(budget):
            slot = EVIDENCE_REPLICATE_BASE + j
            for entry in _board():
                reserved = loss_profile_path(tmp_path, "e0", gid, entry.id).with_name(
                    f"loss.r{slot}.json"
                )
                assert reserved.exists(), f"missing reserved draw {gid}/{entry.id} r{slot}"

    # (c) The champion's reserved draws are fresh samples, not copies of its
    # canonical slot: at least one entry's bytes differ from canonical r0.
    redrawn = any(
        loss_profile_path(tmp_path, "e0", "champion", entry.id)
        .with_name(f"loss.r{EVIDENCE_REPLICATE_BASE}.json")
        .read_bytes()
        != canonical[("champion", entry.id)]
        for entry in _board()
    )
    assert redrawn, "champion evidence draws replicate its canonical slot byte-for-byte"


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


# ===========================================================================
# WS-S — pre-tournament candidate screening (tryouts): operating
# characteristics of the veto-first screen over the SAME seeded noise model.
#
# The screen runs each best-of-N slate candidate on a small champion-passing
# train panel BEFORE selection (reserved replicate 3000; the confirm re-run
# of a pass-flip at 3001) and vetoes only a CONFIRMED catastrophic
# regression. These tests measure, on the Tier-2 noise harness:
#
# * the deterministic contract — a broken candidate (one that breaks an
#   entry the champion passes) is vetoed and the best survivor is chosen;
# * the MEASURED false-veto rate of confirm-before-veto vs the naive
#   any-flip rule (the failing alternative): one flip vetoes at ~sigma per
#   flip-capable panel entry, the confirmed rule at ~sigma^2 — at the
#   moderate sigma=0.10 harness the measured confirmed rate is <= ~2%
#   while naive any-flip runs an order of magnitude hotter; at the
#   deliberately-extreme Tier-2 sigma=0.22 the squaring still holds
#   (measured ~sigma^2 ~ 5%) but NO single-confirm rule can reach 2%
#   there — that harness plants sigma large enough that one full defect is
#   only ~1x the A/A floor, far noisier than a usable contract;
# * the survivors' panel-scalar tiebreak — better than random selection at
#   a large planted delta, no worse than random at a small one;
# * screened-vs-naive composition: a slate containing a broken candidate
#   NEVER sends it to the tournament, while the unscreened heuristic would.
# ===========================================================================


def _screen_truth_parent_losses() -> list[LossProfile]:
    """The champion's TRUE per-entry baseline under BASE_TOKENS.

    conv_body always passes (structural); conv_no_fabrication passes
    (fabricate-metrics absent from BASE_TOKENS); the three defect entries
    fail. This is the clean replicate-0 baseline the panel selector reads;
    the false-veto measurements below are therefore ENGINE operating
    characteristics (candidate-side noise only). A measured (noisy)
    baseline can also admit a truly-failing entry into the panel as
    "champion-passing" — that failure mode belongs to the baseline
    measurement, not to the confirm rule, and is bounded by the same
    replicate-0 canonicalization the promote gate itself trusts.
    """
    truth_pass = {"conv_body": True, "conv_no_fabrication": True}
    losses = []
    for entry in _board():
        losses.append(
            LossProfile(
                run_id=f"run-v0-{entry.id}",
                entry_id=entry.id,
                generation_id="v0",
                epoch_id="e0",
                drift_counts=(),
                plan_revisions=0,
                task_failure_ratio=0.0,
                runtime_ms=1,
                wall_clock_budget_exceeded=False,
                expectation_result=None,
                drift_loss=float(len(BASE_TOKENS)),
                pass_fail=truth_pass.get(entry.id, False),
            )
        )
    return losses


def _screen_runner_for(
    tmp_path: Path,
    *,
    seed: int,
    sigma: float,
    candidate_tokens: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
    veto_only: bool = False,
) -> Any:
    """One trial's real orchestrator-built screen closure over _NoisyWorld."""
    from zicato.core.types import ProposerQualityConfig
    from zicato.evolve.round_context import _build_candidate_screen_runner

    snap = tmp_path / "champion_snapshot"
    if not snap.exists():
        snap.mkdir(parents=True)
        (snap / "policy.txt").write_text("champion\n")
    parent = replace(_gen("v0"), snapshot_root=snap)
    tokens_by_gen: dict[str, tuple[str, ...]] = {"v0": BASE_TOKENS}
    for i, tokens in enumerate(candidate_tokens):
        tokens_by_gen[f"v0-screen-r0c{i}"] = tokens
    world = _NoisyWorld(tokens_by_gen, sigma)
    world.install(monkeypatch)
    weights = ScoringWeights(
        proposer_quality=ProposerQualityConfig(
            best_of_n=max(2, len(candidate_tokens)),
            critique_enabled=False,
            screen_entries=2,
            screen_veto_only=veto_only,
        )
    )
    runner = _build_candidate_screen_runner(
        weights=weights,
        adapter=object(),
        parent_gen=parent,
        train_board=list(_board()),
        parent_losses=_screen_truth_parent_losses(),
        config=_config(tmp_path, seed),
        workspace_root=tmp_path,
        epoch_id="e0",
        round_index=0,
        disable_drift=(),
        judge_only=False,
        beater=None,
    )
    assert runner is not None
    return runner


def _screen_experiment(exp_id: str) -> Any:
    """A patch-free slate candidate (equal diff size across the slate, so
    the heuristic's screen-scalar tiebreak — not parsimony — decides)."""
    from zicato.core.types import Experiment, HypothesisSpec

    return Experiment(
        id=exp_id,
        epoch_id="e0",
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-01-01T00:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea=f"candidate {exp_id}",
            modulating=(),
            why="screen acceptance",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.0",
        ),
        patches=(),
        outcome=None,
    )


def _fab_metrics_measured(seed: int, gen_key: str, replicate: int, sigma: float) -> bool:
    """Whether ``fabricate-metrics`` is MEASURED present for one draw —
    the same stable-seeded draw the engine's fake worker makes, so the
    naive any-flip alternative is computed on the identical sample."""
    rng = random.Random(
        stable_noise_seed(
            workspace_seed=seed,
            generation_key=gen_key,
            entry_id="conv_no_fabrication",
            replicate_index=replicate,
        )
    )
    measured = draw_measured_tokens(list(BASE_TOKENS), rng, sigma)
    return "fabricate-metrics" in measured


def test_screen_deterministic_slate_vetoes_broken_selects_best(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sigma=0: good / mediocre / broken slate — broken vetoed, best chosen."""
    from zicato.core.types import ProposerQualityConfig
    from zicato.proposer.agent import ProposerContext
    from zicato.proposer.best_of_n import BestOfNProposerAgent

    good = ()  # fixes every defect: passes all five entries, zero drift
    mediocre = BASE_TOKENS  # champion-identical
    broken = (*BASE_TOKENS, "fabricate-metrics")  # breaks a champion-passing entry
    runner = _screen_runner_for(
        tmp_path,
        seed=7,
        sigma=0.0,
        candidate_tokens=[broken, mediocre, good],
        monkeypatch=monkeypatch,
    )
    candidates = [
        _screen_experiment("broken"),
        _screen_experiment("mediocre"),
        _screen_experiment("good"),
    ]
    results = asyncio.run(runner(candidates))
    assert [r.vetoed for r in results] == [True, False, False]
    assert results[0].confirmed is True  # the flip re-confirmed at 3001
    assert results[0].scalar is not None  # measured, selection-biased
    # Panel scalars order the survivors: the full fix beats champion-equal.
    assert results[2].scalar is not None and results[1].scalar is not None
    assert results[2].scalar < results[1].scalar

    # Composed through the wrapper: broken filtered, GOOD chosen (equal
    # diffs, so the screen-scalar tiebreak decides among the survivors).
    class _Inner:
        def __init__(self) -> None:
            self.queue = list(candidates)

        async def propose(self, ctx: ProposerContext) -> Any:
            return self.queue.pop(0)

    async def _no_llm(system: str, user: str, model: str) -> str:
        return "0"

    agent = BestOfNProposerAgent(
        inner=_Inner(),
        config=ProposerQualityConfig(best_of_n=3, critique_enabled=False, screen_entries=2),
    )
    ctx = ProposerContext(
        epoch_id="e0",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=(),
        mutations=(),
        brief_text="",
        current_loss_summary="",
        aux_call_llm=_no_llm,
        screen_candidates=runner,
    )
    chosen = asyncio.run(agent.propose(ctx))
    assert chosen.id == "good"


def _measure_screen_false_veto_rates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sigma: float,
    trials: int,
) -> tuple[float, float]:
    """(confirmed-rule rate, naive any-flip rate) for an A/A candidate.

    The candidate's TRUE tokens equal the champion's, so ANY veto is
    false. The naive alternative is computed on the identical seeded
    draws the engine consumed (one flip at replicate 3000 = veto), so
    the two rates compare the RULES, not the samples.
    """
    confirmed_vetoes = 0
    naive_vetoes = 0
    for trial in range(trials):
        runner = _screen_runner_for(
            tmp_path,
            seed=trial,
            sigma=sigma,
            candidate_tokens=[BASE_TOKENS],
            monkeypatch=monkeypatch,
        )
        (result,) = asyncio.run(runner([_screen_experiment("aa")]))
        confirmed_vetoes += 1 if result.vetoed else 0
        # conv_body cannot flip (structural pass); the panel's only
        # flip-capable entry is conv_no_fabrication.
        naive_vetoes += 1 if _fab_metrics_measured(trial, "v0-screen-r0c0", 3000, sigma) else 0
    return confirmed_vetoes / trials, naive_vetoes / trials


def test_screen_false_veto_rate_confirm_beats_naive_any_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trials = 200
    # Moderate harness noise (sigma=0.10): the confirm-before-veto rule
    # measures at ~sigma^2 = 1% — inside the <= ~2% acceptance bar — while
    # the NAIVE any-flip rule (the failing alternative) measures at ~sigma
    # = 10%: one noisy re-roll would disqualify a healthy candidate ten
    # times as often as the confirmed rule.
    confirmed, naive = _measure_screen_false_veto_rates(
        tmp_path, monkeypatch, sigma=0.10, trials=trials
    )
    assert confirmed <= 0.02, f"confirmed false-veto rate {confirmed:.3f} > 2%"
    assert naive >= 0.05, f"naive any-flip rate unexpectedly low: {naive:.3f}"
    assert confirmed <= naive / 3, (confirmed, naive)

    # The deliberately-extreme Tier-2 sigma (0.22 — one FULL defect is only
    # ~1x the A/A floor): the squaring still holds (measured ~sigma^2), but
    # no single-confirm rule can reach 2% here — sigma^2 is already 4.8%.
    # Pinned as documentation of the failing alternative's shape, not as
    # the acceptance bar.
    confirmed_hot, naive_hot = _measure_screen_false_veto_rates(
        tmp_path, monkeypatch, sigma=NOISE_SIGMA, trials=trials
    )
    assert confirmed_hot <= naive_hot / 2.5, (confirmed_hot, naive_hot)
    assert confirmed_hot <= 0.10, f"confirmed rate at hot sigma: {confirmed_hot:.3f}"


def test_screen_tiebreak_beats_random_at_large_delta_safe_at_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Survivor tiebreak: >> random at a large planted delta; ~random at a
    small one (never systematically WORSE than random)."""
    from zicato.core.types import ProposerQualityConfig
    from zicato.proposer.agent import ProposerContext
    from zicato.proposer.best_of_n import BestOfNProposerAgent

    async def _no_llm(system: str, user: str, model: str) -> str:
        return "0"

    def _pick_rate(better_tokens: tuple[str, ...], trials: int) -> float:
        picked_better = 0
        for trial in range(trials):
            # Alternate slate order so the stable-index tiebreak cannot
            # hand the better candidate free wins.
            better_first = trial % 2 == 0
            slate_tokens = (
                [better_tokens, BASE_TOKENS] if better_first else [BASE_TOKENS, better_tokens]
            )
            runner = _screen_runner_for(
                tmp_path,
                seed=1000 + trial,
                sigma=NOISE_SIGMA,
                candidate_tokens=slate_tokens,
                monkeypatch=monkeypatch,
            )
            candidates = (
                [_screen_experiment("better"), _screen_experiment("equal")]
                if better_first
                else [_screen_experiment("equal"), _screen_experiment("better")]
            )

            class _Inner:
                def __init__(self, queue: list[Any]) -> None:
                    self.queue = list(queue)

                async def propose(self, ctx: ProposerContext) -> Any:
                    return self.queue.pop(0)

            agent = BestOfNProposerAgent(
                inner=_Inner(candidates),
                config=ProposerQualityConfig(best_of_n=2, critique_enabled=False, screen_entries=2),
            )
            ctx = ProposerContext(
                epoch_id="e0",
                parent_generation_id="v0",
                new_generation_id="v1",
                patterns=(),
                mutations=(),
                brief_text="",
                current_loss_summary="",
                aux_call_llm=_no_llm,
                screen_candidates=runner,
            )
            chosen = asyncio.run(agent.propose(ctx))
            picked_better += 1 if chosen.id == "better" else 0
        return picked_better / trials

    # Large delta (all three defects fixed, ~3x the A/A floor): the panel
    # scalar separates far beyond the noise — picked (essentially) always.
    large_rate = _pick_rate(DELTA_CASES["large"][0], trials=24)
    assert large_rate >= 0.75, f"large-delta pick rate {large_rate:.2f}"

    # Small delta (~0.5x floor): the 2-entry panel cannot reliably resolve
    # it — the requirement is only NO WORSE than the 0.5 random baseline
    # (the tiebreak must never invert a real ordering systematically).
    small_rate = _pick_rate(DELTA_CASES["small"][0], trials=24)
    assert small_rate >= 0.35, f"small-delta pick rate {small_rate:.2f} < random band"


def test_screened_slate_never_sends_broken_to_the_tournament(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance headline: with screening, a slate containing a broken
    candidate NEVER forwards it; the unscreened heuristic (same slate,
    same contract otherwise) would have.

    Pinned at sigma=0 because "never" is a DETERMINISTIC contract: the
    broken candidate's regression is real, so the screen draw and its
    confirm re-run both observe it and the veto always fires. Under
    measurement noise no single-confirm rule can promise "never" — at
    sigma the defect is measured ABSENT with probability sigma per draw,
    so the veto misses ~sigma*(2-sigma) of slates and only the panel
    scalar tiebreak (and the downstream tournament gate) backstops it;
    the noise-side operating characteristics live in the rate tests
    above, not in this guarantee.
    """
    from zicato.core.types import ProposerQualityConfig
    from zicato.proposer.agent import ProposerContext
    from zicato.proposer.best_of_n import BestOfNProposerAgent

    async def _no_llm(system: str, user: str, model: str) -> str:
        return "0"

    broken = (*BASE_TOKENS, "fabricate-metrics")
    forwarded_broken_screened = 0
    forwarded_broken_unscreened = 0
    trials = 12
    for trial in range(trials):
        runner = _screen_runner_for(
            tmp_path,
            seed=5000 + trial,
            sigma=0.0,
            candidate_tokens=[broken, BASE_TOKENS],
            monkeypatch=monkeypatch,
        )
        candidates = [_screen_experiment("broken"), _screen_experiment("ok")]

        class _Inner:
            def __init__(self, queue: list[Any]) -> None:
                self.queue = list(queue)

            async def propose(self, ctx: ProposerContext) -> Any:
                return self.queue.pop(0)

        def _ctx(screen: Any) -> ProposerContext:
            return ProposerContext(
                epoch_id="e0",
                parent_generation_id="v0",
                new_generation_id="v1",
                patterns=(),
                mutations=(),
                brief_text="",
                current_loss_summary="",
                aux_call_llm=_no_llm,
                screen_candidates=screen,
            )

        config = ProposerQualityConfig(best_of_n=2, critique_enabled=False, screen_entries=2)
        screened_agent = BestOfNProposerAgent(inner=_Inner(candidates), config=config)
        chosen = asyncio.run(screened_agent.propose(_ctx(runner)))
        forwarded_broken_screened += 1 if chosen.id == "broken" else 0

        # The naive alternative: same slate, no screen runner — the
        # heuristic ties on grounding/diff and falls to the stable index,
        # forwarding the broken candidate every time.
        unscreened_agent = BestOfNProposerAgent(inner=_Inner(candidates), config=config)
        chosen_naive = asyncio.run(unscreened_agent.propose(_ctx(None)))
        forwarded_broken_unscreened += 1 if chosen_naive.id == "broken" else 0

    assert forwarded_broken_screened == 0, (
        f"screened slate forwarded the broken candidate "
        f"{forwarded_broken_screened}/{trials} times"
    )
    assert forwarded_broken_unscreened == trials  # the failing alternative


# ===========================================================================
# WS-REC — the recombination slot: operating characteristics of the UNION
# under the SAME seeded noise model.
#
# The mechanical mint exists because two complementary fixes that are each
# real-but-sub-margin can only clear the gate TOGETHER. These tests measure
# both halves of that claim on the Tier-2 harness: (1) through the real
# matchup + gate, the union's promotion probability strictly dominates a
# single fix's under seeded noise; (2) the best-of-N heuristic — the
# selection the short-circuit bypasses — would systematically STARVE the
# union (its diff is larger by construction), which is WHY
# selection_mode="recombined" exists.
# ===========================================================================

#: The two-defect champion of the recombination OC (the two-marker policy).
REC_BASE_TOKENS = ("omit-summary", "skip-citations")
#: One fix applied — one defect remains. True Δ = 1.2.
REC_SINGLE_FIX_TOKENS = ("skip-citations",)
#: Both fixes applied — the union. True Δ = 2.4.
REC_UNION_TOKENS: tuple[str, ...] = ()

#: The OC contract's margin — strictly between the single fix's true Δ
#: (1.2) and the union's (2.4). Monotonicity off to isolate the margin
#: rule (the same isolation the margin-vs-noise test uses).
REC_MARGIN_WEIGHTS = ScoringWeights(promote_margin=1.5, pass_rate_monotonicity=False)

REC_SIGMA = 0.10
REC_TRIALS = 40


def _rec_promotion_rate(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, challenger_tokens: tuple[str, ...]
) -> float:
    world = {"champion": REC_BASE_TOKENS, "challenger": challenger_tokens}
    _NoisyWorld(world, REC_SIGMA).install(monkeypatch)
    promoted = sum(
        1
        for trial in range(REC_TRIALS)
        if _naive_outcome(workspace, seed=trial, weights=REC_MARGIN_WEIGHTS).decision == "promoted"
    )
    return promoted / REC_TRIALS


def test_union_promotion_probability_dominates_single_fix(monkeypatch, tmp_path):
    """P(promote | union) > P(promote | single fix) through the real gate.

    Same seeds, same noise model, same margin — only the challenger
    differs. The single fix's measured delta (1.2·(1−2σ) ≈ 0.96) sits
    UNDER the 1.5 margin, so only noise can push it over; the union's
    (2.4·(1−2σ) ≈ 1.92) sits OVER it, so only noise can pull it under.
    The union is not merely a fresh sample of the same idea — it is a
    categorically stronger effect the gate resolves at a higher rate.
    """
    single_rate = _rec_promotion_rate(monkeypatch, tmp_path, REC_SINGLE_FIX_TOKENS)
    union_rate = _rec_promotion_rate(monkeypatch, tmp_path, REC_UNION_TOKENS)
    print(
        f"\n[recombination power] sigma={REC_SIGMA} margin="
        f"{REC_MARGIN_WEIGHTS.promote_margin} trials={REC_TRIALS}: "
        f"P(promote|single fix)={single_rate:.2f} P(promote|union)={union_rate:.2f}"
    )
    # The single fix is sub-margin: noise alone must carry it, rarely.
    assert single_rate <= 0.5
    # The union clears the margin: promoted in a decisive majority.
    assert union_rate >= 0.5
    # The headline: strict dominance, by a wide, seeded-deterministic gap.
    assert union_rate >= single_rate + 0.25


def test_heuristic_over_slate_starves_the_union(monkeypatch, tmp_path):
    """Executable documentation: WHY selection_mode="recombined" exists.

    Put the union mint on an ordinary best-of-N slate next to one of its
    own single-fix parents and let the deterministic heuristic choose
    (no short-circuit): the minimal-diff key picks the single fix EVERY
    time — the union's diff is larger by construction (it carries both
    parents' patches), so the parsimony bias the heuristic rightly applies
    to speculative LLM samples systematically starves the one candidate
    grounded in two rounds of measured evidence. The short-circuit is the
    fix; the tournament gate remains the arbiter either way.
    """
    del monkeypatch, tmp_path
    from zicato.core.types import Experiment, HypothesisSpec, Patch
    from zicato.proposer.agent import ProposerContext
    from zicato.proposer.best_of_n import _heuristic_best_index

    def _patch(pid: str, mid: str) -> Patch:
        return Patch(
            id=pid,
            mutation_id=mid,
            op="replace",
            new_content="x" * 40,
            new_numeric=None,
            new_enum=None,
            rationale="fix",
        )

    def _exp(exp_id: str, patches: tuple[Patch, ...], recombined_from=()) -> Experiment:
        return Experiment(
            id=exp_id,
            epoch_id="e0",
            generation_id="v3",
            parent_generation_id="v0",
            proposed_at="2026-07-11T00:00:00+00:00",
            hypothesis=HypothesisSpec(
                core_idea=exp_id,
                modulating=tuple(p.mutation_id for p in patches),
                why="",
                expected_drift_movements=(),
                expected_pass_rate_delta="",
            ),
            patches=patches,
            outcome=None,
            recombined_from=tuple(recombined_from),
        )

    single = _exp("single-fix", (_patch("p1", "style_rules"),))
    union = _exp(
        "union-mint",
        (_patch("p2", "style_rules"), _patch("p3", "style_rules_extra")),
        recombined_from=("v1", "v2"),
    )

    async def _no_llm(system: str, user: str, model: str) -> str:
        return "0"

    ctx = ProposerContext(
        epoch_id="e0",
        parent_generation_id="v0",
        new_generation_id="v3",
        patterns=(),
        mutations=(),
        brief_text="",
        current_loss_summary="",
        aux_call_llm=_no_llm,
    )
    # Both slate orders: the union's larger diff loses to parsimony every
    # time — never a stable-index accident.
    assert _heuristic_best_index([single, union], ctx) == 0
    assert _heuristic_best_index([union, single], ctx) == 1
