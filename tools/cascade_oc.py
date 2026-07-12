"""The evaluation-cascade OC harness — measure a SIMULATED cascade before building it.

This is the instrument ``docs/design/CASCADE.md §4`` calls for: a seeded,
known-answer harness that measures the operating characteristics of a
*simulated composition* of the four shipped partial-cascade stages —

    screen (veto-first)  →  racing rungs (rank-and-halve on board slices)
                         →  the three-rule promote gate  →  holdout confirm

— and answers the build-decision the note defers. Nothing here builds the
cascade; it drives the ALREADY-SHIPPED decision code paths (the real
``RacingStrategy`` rung cut, the real ``evaluate_gate`` / ``holdout_confirms``,
the real ``measure_noise_floor`` calibration, the real evidence-gated
``resolve_tournament`` terminal, the real ``run_candidate_screen`` veto) under
the seeded target_0 noise model, and records what a cascade WOULD do.

Design (why this is not a reimplementation)
--------------------------------------------
Every board-unit measurement flows through ``runner._run_single`` — the
suite's documented monkeypatch anchor — replaced by the same in-process
seeded-noise evaluator (:class:`tests.test_decision_procedure_power._NoisyWorld`)
the shipped power harness uses. The *decision* on top of those measurements
is the shipped code in every case:

* the per-slice A/A floor is :func:`zicato.tournament.calibration.measure_noise_floor`
  restricted to the slice (Experiment A);
* the rung cut is :meth:`zicato.selection.strategies.racing.RacingStrategy._apply_cut`
  driven through ``run_matchup`` (Experiments A, B, C);
* the terminal is either the evidence-gated
  :func:`zicato.selection.driver.resolve_tournament` (Experiment B — the
  soundness headline, fact #4) or the three-rule
  :func:`zicato.tournament.gate.evaluate_gate` at a margin sized to the
  measured floor plus a holdout confirm (Experiment C — the budget curve);
* the screen veto is the real :func:`zicato.epoch.screen.run_candidate_screen`
  (Experiment A's veto-stage OC).

Seeds. Everything derives from ``stable_noise_seed(workspace_seed,
generation_id, entry_id, replicate_index)`` — no wall clock, no global RNG —
so every number here is an exact function of the seeds recorded in the JSON
report. See ``CASCADE.md §4.1`` and ``04-evaluation-statistics.md §13.1``.

Run it::

    uv run python -m tools.cascade_oc                 # all experiments, JSON + summary
    uv run python -m tools.cascade_oc --smoke         # the cheap end-to-end smoke config
    uv run python -m tools.cascade_oc --out report.json

The pinned assertions live in ``tests/test_cascade_oc_harness.py`` behind the
``cascade_oc`` marker (excluded from the default ``pytest`` run); one cheap
smoke test runs in the default suite so the harness cannot silently rot.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

# The seeded-noise substrate — reused verbatim from the shipped power harness
# so the cascade drives the SAME noise model, output synthesis, and real board
# predicates (CASCADE.md §4.1: "inherits the target_0 example world verbatim").
from tests.test_decision_procedure_power import (  # noqa: E402  (path set up by conftest)
    BASE_TOKENS,
    DELTA_CASES,
    EFFECTIVE_BUDGET,
    EFFECTIVE_REPLICATES,
    NOISE_SIGMA,
    _board,
    _config,
    _effective_decision,
    _gen,
    _NoisyWorld,
)
from zicato.core import ScoringWeights
from zicato.selection.evidence_gate import EVIDENCE_REPLICATE_BASE
from zicato.selection.strategies.racing import RacingStrategy
from zicato.selection.strategy import Contestant, MatchupResult
from zicato.tournament.calibration import (
    CALIBRATION_REPLICATE_BASE,
    measure_noise_floor,
)
from zicato.tournament.gate import evaluate_gate
from zicato.tournament.runner import run_matchup

# ---------------------------------------------------------------------------
# Parameters — the doc leaves rung field composition + draw counts
# underspecified (§4.2). These are the chosen defaults; every one is
# justified in the report and overridable from the CLI / the test harness.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HarnessParams:
    """All tunables, so the JSON report is a complete reproduction recipe."""

    sigma: float = NOISE_SIGMA
    #: A/A slice-floor draws. 60 is the shipped power harness's own precedent
    #: (``AA_TRIALS``); each draw is a single cheap one-side board sweep.
    floor_draws: int = 30
    #: Rung false-cut trials. A rung trial runs two duels (better vs champion,
    #: decoy vs champion) on the slice; 48 keeps each slice's wall-clock small
    #: while giving a rate resolved to ~2%.
    rung_trials: int = 48
    #: Screen veto false-veto trials (matches the shipped screen OC test's 200).
    screen_trials: int = 200
    #: End-to-end promotion trials for the Experiment B EFFECT conditions
    #: (evidence terminal, heavy).
    b_trials: int = 16
    #: End-to-end promotion trials for the Experiment B NULL (A/A) condition.
    #: The soundness bar (P(promote|null)) rests on this count, so it is raised
    #: to the doctrine's ``AA_TRIALS=60`` precedent — the null field never
    #: triggers the expensive evidence streak, so the extra trials stay cheap.
    null_trials: int = 60
    #: Experiment C trials per swept configuration (margin terminal, cheap).
    c_trials: int = 16
    #: Rung successive-halving ratio (racing's default).
    eta: int = 2
    #: Slice sizes the rung sweep measures, in board-entry counts. The board
    #: has 5 entries; m=5 is the full board (the terminal gate, not a rung),
    #: so rungs sweep {1,2,3,4}.
    rung_slice_sizes: tuple[int, ...] = (1, 2, 3, 4)
    #: Experiment C: the planted effects the budget curve is drawn at (§4.4 is
    #: "at a fixed planted δ" — we sweep the three so BOTH regimes show: the
    #: build signal at large δ and the power-loss / "do not build" outcome at
    #: small δ), and the field size the cascade prunes.
    c_deltas: tuple[str, ...] = ("small", "medium", "large")
    c_field_size: int = 6
    #: The representative rung slice (entry-count) the end-to-end cascade cuts
    #: on in Experiments B/C. On this 5-entry board a literal quarter-board
    #: rung is a single entry (pathologically noisy — see Exp A's m=1 column);
    #: a 2-entry (~40%) rung characterizes a realistic staged cut. Exp A sweeps
    #: the full m∈{1..4} range separately.
    b_rung_slice: int = 2
    #: Experiment C terminal replicate budget (the margin-terminal averaging).
    c_terminal_replicates: int = 16
    #: The Experiment C margin, as a MULTIPLE of the measured full-board floor
    #: (a legitimate operator choice per §13.8: set the margin at/above the
    #: floor). 0.55x sits below the 1x planted effect so power survives, above
    #: the R-averaged null noise so P(promote|null) stays small.
    c_margin_floor_multiple: float = 0.55
    #: Holdout slice for the terminal confirm (entry-count from the board tail).
    holdout_size: int = 1
    #: Master workspace seed offset — every derived seed is recorded.
    seed_base: int = 20260712


# ---------------------------------------------------------------------------
# Counting noise world — exact per-promotion board-unit budget accounting
# ---------------------------------------------------------------------------


class _CountingWorld(_NoisyWorld):
    """A :class:`_NoisyWorld` that counts every board-unit evaluation.

    ``calls`` is incremented once per ``runner._run_single`` — i.e. once per
    (generation, entry, replicate, side) board unit — so the exact total spend
    of a cascade or single-stage run is ``calls`` measured across the run. This
    is Experiment C's x-axis, counted rather than derived (§4.4).
    """

    def __init__(self, tokens_by_gen: dict[str, tuple[str, ...]], sigma: float) -> None:
        super().__init__(tokens_by_gen, sigma)
        self.calls = 0

    async def _fake_run_single(self, **kwargs: Any) -> Any:
        self.calls += 1
        return await super()._fake_run_single(**kwargs)


def _board_ids() -> list[str]:
    return [e.id for e in _board()]


def _slice_board(m: int) -> list[Any]:
    return list(_board())[:m]


_RUNG_WEIGHTS = ScoringWeights()  # rung cut is rank-based; gate weights are irrelevant here

# 95% two-sided normal quantile — the z for every Wilson interval below.
_WILSON_Z95 = 1.959963984540054


def _wilson_ci(successes: int, n: int, z: float = _WILSON_Z95) -> tuple[float, float]:
    """95% Wilson score interval for ``successes`` of ``n`` Bernoulli trials.

    The end-to-end promotion rates (Experiments B and C) are estimates from a
    finite trial count; this reports the sampling uncertainty around each so the
    doc can carry an interval instead of an unqualified point rate. The
    board-unit BUDGET numbers are exact counts (a deterministic function of the
    seeds), not sampled proportions, so they carry no interval.
    """
    if n == 0:
        return (0.0, 0.0)
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2.0 * n)) / denom
    half = z * ((phat * (1.0 - phat) / n + z2 / (4.0 * n * n)) ** 0.5) / denom
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


# ---------------------------------------------------------------------------
# The simulated stages — each drives the shipped decision code
# ---------------------------------------------------------------------------


async def _duel(
    *,
    workspace: Path,
    seed: int,
    left_id: str,
    right_id: str,
    weights: ScoringWeights,
    replicates: int,
    match_id: str,
    board_subset: tuple[str, ...] | None,
    fast: bool = True,
) -> Any:
    """One real ``run_matchup`` duel through the (installed) counting world."""
    return await run_matchup(
        adapter=object(),
        left_gen=_gen(left_id),
        right_gen=_gen(right_id),
        board=list(_board()),
        weights=weights,
        config=_config(workspace, seed),
        workspace_root=workspace,
        epoch_id="e0",
        replicates=replicates,
        match_id=match_id,
        board_subset=board_subset,
        fast=fast,
    )


def _rung_survivor(
    *,
    workspace: Path,
    seed: int,
    field: list[str],
    eta: int,
    rung0_size: int,
    replicates: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Drive the REAL racing rungs over ``field`` and return the survivor id.

    Runs racing rung-by-rung — the shipped ``RacingStrategy._apply_cut``
    rank-and-halve on escalating board slices — and STOPS at the moment a
    single survivor is chosen, before racing's own final full-board gate (the
    cascade's terminal stage is a separate, evidence/margin gate). Returns the
    survivor generation id and the per-rung cut records (audit for §4.5).
    """
    board_ids = _board_ids()
    strat = RacingStrategy(
        {
            "eta": eta,
            "rung0_board_size": rung0_size,
            "board_ids": board_ids,
            "replicates": replicates,
            "field_size": len(field),
        }
    )
    strat.seed(
        Contestant(generation_id="champion", role="champion"),
        [Contestant(generation_id=g, role="challenger") for g in field],
    )
    duel_counter = itertools.count()
    while strat._survivor is None:  # noqa: SLF001 — driving the shipped strategy
        matchups = strat.next_matchups()
        if not matchups:
            break
        for m in matchups:
            if m.matchup_id == strat._final_match_id:  # noqa: SLF001
                continue  # survivor already chosen; skip racing's own final gate
            res = asyncio.run(
                _duel(
                    workspace=workspace,
                    seed=seed + next(duel_counter),
                    left_id=m.left.generation_id,
                    right_id=m.right.generation_id,
                    weights=_RUNG_WEIGHTS,
                    replicates=m.replicates,
                    match_id=m.matchup_id,
                    board_subset=m.board_subset,
                )
            )
            strat.record_result(
                MatchupResult(
                    matchup_id=m.matchup_id,
                    left_id=m.left.generation_id,
                    right_id=m.right.generation_id,
                    left_agg=res.parent_agg,
                    right_agg=res.child_agg,
                    outcome=res.outcome,
                )
            )
    survivor = strat._survivor.generation_id if strat._survivor is not None else field[0]  # noqa: SLF001
    records = [
        {
            "rung": rec.stage_index,
            "survivors": list(rec.matches[0].survivors),
            "cut": list(rec.matches[0].cut),
            "board_fraction": rec.matches[0].board_fraction,
        }
        for rec in strat._records  # noqa: SLF001
    ]
    return survivor, records


def _margin_terminal(
    *,
    workspace: Path,
    seed: int,
    challenger_id: str,
    margin: float,
    replicates: int,
    holdout_ids: tuple[str, ...],
) -> bool:
    """The three-rule gate + holdout confirm at ``margin`` — the shipped ``evaluate_gate``.

    A full-board duel (``replicates`` averaged) supplies the train aggregates;
    a holdout-slice duel supplies the holdout aggregates; the promote decision
    is the real :func:`zicato.tournament.gate.evaluate_gate` with the holdout
    arms wired in. Aggregate-scope monotonicity is used (the documented policy
    for sampled/noisy boards).
    """
    weights = ScoringWeights(promote_margin=margin, pass_rate_monotonicity_scope="aggregate")
    full = asyncio.run(
        _duel(
            workspace=workspace,
            seed=seed,
            left_id="champion",
            right_id=challenger_id,
            weights=weights,
            replicates=replicates,
            match_id="cascade-full-gate",
            board_subset=None,
        )
    )
    holdout = asyncio.run(
        _duel(
            workspace=workspace,
            seed=seed + 500,
            left_id="champion",
            right_id=challenger_id,
            weights=weights,
            replicates=replicates,
            match_id="cascade-holdout",
            board_subset=holdout_ids,
        )
    )
    outcome = evaluate_gate(
        full.parent_agg,
        full.child_agg,
        weights,
        holdout_parent_agg=holdout.parent_agg,
        holdout_child_agg=holdout.child_agg,
    )
    return outcome.decision == "promoted"


# ---------------------------------------------------------------------------
# Experiment A — per-stage false-cut rate vs the slice-size floor (§4.2)
# ---------------------------------------------------------------------------


def _measure_slice_floor(workspace: Path, m: int, draws: int, sigma: float) -> float:
    """The A/A delta floor on an ``m``-entry slice, via the REAL calibration path.

    ``measure_noise_floor`` restricted to the first ``m`` board entries: K seeded
    A/A draws of the champion at reserved base ``CALIBRATION_REPLICATE_BASE``,
    aggregated by the same scorer the gate uses. Returns ``delta_std`` (the sd of
    the A/A delta_scalar) — the quantity a margin on this slice must clear.
    """
    world = _CountingWorld({"champion": BASE_TOKENS}, sigma)
    with pytest.MonkeyPatch.context() as mp:
        world.install(mp)
        floor = asyncio.run(
            measure_noise_floor(
                adapter=object(),
                generation=_gen("champion"),
                board=_slice_board(m),
                weights=ScoringWeights(),
                config=_config(workspace, 1),
                workspace_root=workspace,
                epoch_id="e0",
                runs=draws,
            )
        )
    return float(floor.delta_std)


def _rung_false_cut_rate(
    workspace: Path,
    m: int,
    better_tokens: tuple[str, ...],
    trials: int,
    eta: int,
    sigma: float,
) -> float:
    """P(the genuinely-better arm is cut at a single rung on an ``m``-entry slice).

    Field = one better arm (planted δ) + one champion-equal decoy; ``eta`` halving
    keeps exactly one, so a false cut is unambiguously "the better arm lost the
    rung to a champion-equal decoy on this noisy slice". Drives the real racing
    ``_apply_cut``.
    """
    world = _CountingWorld(
        {"champion": BASE_TOKENS, "better": better_tokens, "decoy": BASE_TOKENS}, sigma
    )
    false_cuts = 0
    with pytest.MonkeyPatch.context() as mp:
        world.install(mp)
        for trial in range(trials):
            # Alternate slate order so the stable-id tiebreak cannot hand the
            # better arm free wins.
            field = ["better", "decoy"] if trial % 2 == 0 else ["decoy", "better"]
            _survivor, records = _rung_survivor(
                workspace=workspace,
                seed=(trial + 1) * 7_000,
                field=field,
                eta=eta,
                rung0_size=m,
                replicates=1,
            )
            if records and "better" in records[0]["cut"]:
                false_cuts += 1
    return false_cuts / trials


def _screen_false_veto_rates(workspace: Path, sigma: float, trials: int) -> tuple[float, float]:
    """(confirmed-rule, naive any-flip) false-veto rates for an A/A candidate.

    Drives the REAL veto stage (``run_candidate_screen`` via the shipped
    ``_screen_runner_for`` seam) and the naive any-flip alternative on the
    identical seeded draws, so the comparison is between RULES not samples —
    the veto stage's confirm-before-veto squaring (≈ flip-rate²) is the §4.2.4
    coarse-cut discipline for the noisiest stage.
    """
    from tests.test_decision_procedure_power import _measure_screen_false_veto_rates

    with pytest.MonkeyPatch.context() as mp:
        return _measure_screen_false_veto_rates(workspace, mp, sigma=sigma, trials=trials)


def experiment_a(params: HarnessParams, workspace: Path) -> dict[str, Any]:
    """Per-stage false-cut rate vs each stage's own slice-size floor."""
    started = time.monotonic()
    board_n = len(_board_ids())

    # (1) slice floors — assert they grow ~1/sqrt(m) as m shrinks.
    floors = {
        m: _measure_slice_floor(workspace, m, params.floor_draws, params.sigma)
        for m in range(1, board_n + 1)
    }

    # (3)+(2) rung false-cut per (slice, planted δ), with δ sized against the
    # slice floor reported alongside.
    rung: dict[str, dict[str, Any]] = {}
    for name, (tokens, measured_delta) in DELTA_CASES.items():
        per_slice = {}
        for m in params.rung_slice_sizes:
            rate = _rung_false_cut_rate(
                workspace, m, tokens, params.rung_trials, params.eta, params.sigma
            )
            per_slice[m] = {
                "false_cut_rate": rate,
                "slice_floor": floors[m],
                "delta_over_slice_floor": measured_delta / floors[m] if floors[m] else None,
            }
        rung[name] = {"measured_delta": measured_delta, "by_slice": per_slice}

    # (4) veto-stage false-veto (confirm-before-veto squaring).
    confirmed, naive = _screen_false_veto_rates(workspace, params.sigma, params.screen_trials)

    return {
        "slice_floors": {str(m): floors[m] for m in floors},
        "full_board_floor": floors[board_n],
        "rung_false_cut": {
            k: {
                "measured_delta": v["measured_delta"],
                "by_slice": {str(m): s for m, s in v["by_slice"].items()},
            }
            for k, v in rung.items()
        },
        "veto_stage": {
            "sigma": params.sigma,
            "confirmed_false_veto_rate": confirmed,
            "naive_any_flip_rate": naive,
            "sigma_squared": params.sigma**2,
        },
        "wall_clock_s": round(time.monotonic() - started, 2),
    }


# ---------------------------------------------------------------------------
# Experiment B — end-to-end P(promote|·), cascade ON vs OFF (§4.3)
# ---------------------------------------------------------------------------


def _cascade_promote_evidence(
    workspace: Path,
    trial: int,
    field_tokens: dict[str, tuple[str, ...]],
    eta: int,
    sigma: float,
    rung_slice: int,
) -> bool:
    """Full cascade → evidence-gated terminal: rungs pick a survivor, the
    survivor faces the shipped ``resolve_tournament`` evidence pre-gate on a
    FRESH draw (§3.2's selection-independent re-measurement)."""
    field = list(field_tokens.keys())
    rung_world = _CountingWorld({"champion": BASE_TOKENS, **field_tokens}, sigma)
    with pytest.MonkeyPatch.context() as mp:
        rung_world.install(mp)
        survivor, _ = _rung_survivor(
            workspace=workspace,
            seed=(trial + 1) * 11_000,
            field=field,
            eta=eta,
            rung0_size=rung_slice,
            replicates=1,
        )
    survivor_tokens = field_tokens[survivor]
    # Terminal on a fresh world mapping the survivor onto the "challenger" id
    # the shipped evidence-gated decision procedure expects.
    term_world = _NoisyWorld({"champion": BASE_TOKENS, "challenger": survivor_tokens}, sigma)
    with pytest.MonkeyPatch.context() as mp:
        term_world.install(mp)
        decision = _effective_decision(workspace, trial)
    return decision.decision == "promoted"


def _naive_gate_rung_promote(
    workspace: Path,
    trial: int,
    field_tokens: dict[str, tuple[str, ...]],
    sigma: float,
    m: int,
) -> bool:
    """The FAILING alternative (§4.3): run each rung's cut as a GATE on the
    noisy board slice instead of a rank-halve, then hand the survivor to the
    SAME evidence-gated terminal the cascade column uses — on the IDENTICAL
    seeded draws (same ``(trial+1)*11_000`` rung stream, same
    ``_effective_decision(workspace, trial)`` terminal). The only thing that
    differs from the cascade column is the RULE: gate-at-every-rung vs
    rank-halve. The true candidate is cut whenever the noisy slice fails the
    margin gate — power bleeds at the rung — isolating what the wrong rung
    rule costs, samples held fixed."""
    field = list(field_tokens.keys())
    world = _CountingWorld({"champion": BASE_TOKENS, **field_tokens}, sigma)
    survivors: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        world.install(mp)
        weights = ScoringWeights(pass_rate_monotonicity_scope="aggregate")
        for cand_idx, cand in enumerate(field):
            res = asyncio.run(
                _duel(
                    workspace=workspace,
                    seed=(trial + 1) * 11_000 + cand_idx,
                    left_id="champion",
                    right_id=cand,
                    weights=weights,
                    replicates=1,
                    match_id=f"naive-rung-gate:{cand}",
                    board_subset=tuple(_board_ids()[:m]),
                )
            )
            # A rung run AS A GATE: the candidate is kept only if the slice gate
            # would promote it (that is the "cut every stage as a gate" rule).
            if res.outcome.decision == "promoted":
                survivors.append(cand)
    if not survivors:
        return False
    # Terminal = the SAME shipped evidence-gated decision the cascade column
    # runs, on the survivor mapped onto the "challenger" id, same trial seed.
    survivor_tokens = field_tokens[survivors[0]]
    term_world = _NoisyWorld({"champion": BASE_TOKENS, "challenger": survivor_tokens}, sigma)
    with pytest.MonkeyPatch.context() as mp:
        term_world.install(mp)
        return _effective_decision(workspace, trial).decision == "promoted"


def experiment_b(params: HarnessParams, workspace: Path) -> dict[str, Any]:
    """P(promote|null) and P(promote|true) at 0.5x/1x/3x floor, cascade ON vs OFF.

    ON  = full cascade (screen ~pass-through for non-broken fields — measured in
          Exp A — then real rungs → evidence-gated terminal).
    OFF = the single-stage contract: the shipped evidence-gated crowning of the
          candidate straight on the full board (no upstream rungs).
    """
    started = time.monotonic()

    conditions: dict[str, dict[str, tuple[str, ...]]] = {
        "null": {"true": BASE_TOKENS, "decoy": BASE_TOKENS},
    }
    for name, (tokens, _delta) in DELTA_CASES.items():
        conditions[name] = {"true": tokens, "decoy": BASE_TOKENS}

    results: dict[str, dict[str, Any]] = {}
    for cond, field_tokens in conditions.items():
        # The null (soundness) bar rests on a larger trial count than the
        # effect conditions (Fix 2): a cheap null field, a stiffer bound.
        n_trials = params.null_trials if cond == "null" else params.b_trials
        cascade_on = 0
        single_stage = 0
        naive = 0
        for trial in range(n_trials):
            if _cascade_promote_evidence(
                workspace, trial, field_tokens, params.eta, params.sigma, params.b_rung_slice
            ):
                cascade_on += 1
            # Single stage: terminal on the true candidate directly (the only
            # promotable arm), evidence-gated, identical seeded draws.
            true_world = _NoisyWorld(
                {"champion": BASE_TOKENS, "challenger": field_tokens["true"]}, params.sigma
            )
            with pytest.MonkeyPatch.context() as mp:
                true_world.install(mp)
                if _effective_decision(workspace, trial).decision == "promoted":
                    single_stage += 1
            if _naive_gate_rung_promote(
                workspace, trial, field_tokens, params.sigma, params.b_rung_slice
            ):
                naive += 1
        results[cond] = {
            "n": n_trials,
            "cascade_on": cascade_on / n_trials,
            "single_stage": single_stage / n_trials,
            "naive_gate_at_every_rung": naive / n_trials,
            "counts": {
                "cascade_on": cascade_on,
                "single_stage": single_stage,
                "naive_gate_at_every_rung": naive,
            },
            "cascade_on_ci95": _wilson_ci(cascade_on, n_trials),
            "single_stage_ci95": _wilson_ci(single_stage, n_trials),
            "naive_gate_at_every_rung_ci95": _wilson_ci(naive, n_trials),
        }

    return {
        "null_trials": params.null_trials,
        "effect_trials": params.b_trials,
        "sigma": params.sigma,
        "by_condition": results,
        "wall_clock_s": round(time.monotonic() - started, 2),
    }


# ---------------------------------------------------------------------------
# Experiment C — budget-savings-vs-power curve (§4.4) — the build artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CascadeConfig:
    label: str
    rung0_size: int
    eta: int
    terminal_replicates: int


def _run_cascade_margin(
    workspace: Path,
    trial: int,
    field_tokens: dict[str, tuple[str, ...]],
    cfg: _CascadeConfig,
    margin: float,
    holdout_ids: tuple[str, ...],
    sigma: float,
) -> tuple[bool, int]:
    """One cascade run under the MARGIN terminal; returns (promoted, board_units)."""
    field = list(field_tokens.keys())
    world = _CountingWorld({"champion": BASE_TOKENS, **field_tokens}, sigma)
    with pytest.MonkeyPatch.context() as mp:
        world.install(mp)
        survivor, _ = _rung_survivor(
            workspace=workspace,
            seed=(trial + 1) * 23_000,
            field=field,
            eta=cfg.eta,
            rung0_size=cfg.rung0_size,
            replicates=1,
        )
        promoted = _margin_terminal(
            workspace=workspace,
            seed=(trial + 1) * 29_000,
            challenger_id=survivor,
            margin=margin,
            replicates=cfg.terminal_replicates,
            holdout_ids=holdout_ids,
        )
    return promoted, world.calls


def _run_single_stage_margin(
    workspace: Path,
    trial: int,
    field_tokens: dict[str, tuple[str, ...]],
    terminal_replicates: int,
    margin: float,
    holdout_ids: tuple[str, ...],
    sigma: float,
) -> tuple[bool, int]:
    """Single-stage baseline: to SELECT among the field soundly, run the full
    board (× replicates × both sides) on EVERY candidate, then gate the true
    one. Returns (promoted, board_units) — the reference the cascade must beat
    on budget without losing power (§4.4)."""
    field = list(field_tokens.keys())
    world = _CountingWorld({"champion": BASE_TOKENS, **field_tokens}, sigma)
    promoted = False
    with pytest.MonkeyPatch.context() as mp:
        world.install(mp)
        # Full-board evaluate every candidate (the cost the cascade avoids).
        for cand_idx, cand in enumerate(field):
            asyncio.run(
                _duel(
                    workspace=workspace,
                    seed=(trial + 1) * 31_000 + cand_idx,
                    left_id="champion",
                    right_id=cand,
                    weights=ScoringWeights(
                        promote_margin=margin, pass_rate_monotonicity_scope="aggregate"
                    ),
                    replicates=terminal_replicates,
                    match_id=f"baseline-full:{cand}",
                    board_subset=None,
                )
            )
        # The promote decision is the true candidate's gate (+holdout); decoys
        # are champion-equal and cannot promote.
        promoted = _margin_terminal(
            workspace=workspace,
            seed=(trial + 1) * 37_000,
            challenger_id="true",
            margin=margin,
            replicates=terminal_replicates,
            holdout_ids=holdout_ids,
        )
    return promoted, world.calls


def _experiment_c_at_delta(
    params: HarnessParams,
    workspace: Path,
    floor: float,
    delta: str,
    margin: float,
    holdout_ids: tuple[str, ...],
    configs: list[_CascadeConfig],
) -> dict[str, Any]:
    """One δ slice of the budget-vs-power curve + the single-stage reference."""
    field_tokens = {"true": DELTA_CASES[delta][0]}
    for i in range(params.c_field_size - 1):
        field_tokens[f"decoy{i}"] = BASE_TOKENS

    curve: list[dict[str, Any]] = []
    for cfg in configs:
        promotes = 0
        spend = 0
        for trial in range(params.c_trials):
            p, calls = _run_cascade_margin(
                workspace, trial, field_tokens, cfg, margin, holdout_ids, params.sigma
            )
            promotes += 1 if p else 0
            spend += calls
        curve.append(
            {
                "label": cfg.label,
                "rung0_size": cfg.rung0_size,
                "eta": cfg.eta,
                "terminal_replicates": cfg.terminal_replicates,
                "n": params.c_trials,
                "power": promotes / params.c_trials,
                "power_count": promotes,
                "power_ci95": _wilson_ci(promotes, params.c_trials),
                # exact count, not a sampled proportion → no interval
                "mean_board_units": spend / params.c_trials,
            }
        )

    base_promotes = 0
    base_spend = 0
    for trial in range(params.c_trials):
        p, calls = _run_single_stage_margin(
            workspace,
            trial,
            field_tokens,
            params.c_terminal_replicates,
            margin,
            holdout_ids,
            params.sigma,
        )
        base_promotes += 1 if p else 0
        base_spend += calls
    baseline = {
        "label": "single_stage_baseline",
        "n": params.c_trials,
        "power": base_promotes / params.c_trials,
        "power_count": base_promotes,
        "power_ci95": _wilson_ci(base_promotes, params.c_trials),
        "mean_board_units": base_spend / params.c_trials,
    }

    # Report-only: a config that reaches the reference power (within 5pp) at
    # <=75% of the reference budget. The build verdict is the operator's.
    build_candidates = [
        c["label"]
        for c in curve
        if c["power"] >= baseline["power"] - 0.05
        and c["mean_board_units"] <= 0.75 * baseline["mean_board_units"]
    ]
    return {"curve": curve, "baseline": baseline, "build_candidate_configs": build_candidates}


def experiment_c(params: HarnessParams, workspace: Path, floor: float) -> dict[str, Any]:
    """Budget (board-units) vs power (P(promote|true)) across cascade configs,
    swept over the planted δ so both regimes — a build signal at large effects
    and a power-loss / "do not build" outcome at small effects — are visible."""
    started = time.monotonic()
    margin = params.c_margin_floor_multiple * floor
    holdout_ids = tuple(_board_ids()[-params.holdout_size :])
    configs = [
        _CascadeConfig(
            "aggressive-r1",
            rung0_size=1,
            eta=params.eta,
            terminal_replicates=params.c_terminal_replicates,
        ),
        _CascadeConfig(
            "quarter-r1",
            rung0_size=max(1, len(_board_ids()) // 4),
            eta=params.eta,
            terminal_replicates=params.c_terminal_replicates,
        ),
        _CascadeConfig(
            "half-r2",
            rung0_size=max(1, len(_board_ids()) // 2),
            eta=params.eta,
            terminal_replicates=params.c_terminal_replicates,
        ),
    ]
    by_delta = {
        delta: _experiment_c_at_delta(params, workspace, floor, delta, margin, holdout_ids, configs)
        for delta in params.c_deltas
    }
    return {
        "deltas": list(params.c_deltas),
        "field_size": params.c_field_size,
        "margin": margin,
        "margin_floor_multiple": params.c_margin_floor_multiple,
        "full_board_floor": floor,
        "by_delta": by_delta,
        "wall_clock_s": round(time.monotonic() - started, 2),
    }


# ---------------------------------------------------------------------------
# §4.5 — cross-stage slot-integrity / draw-independence proof
# ---------------------------------------------------------------------------


def slot_integrity_proof(params: HarnessParams, workspace: Path) -> dict[str, Any]:
    """Prove every stage draws under its OWN reserved base and none clobbers r0.

    Lifts ``test_full_mode_evidence_loop_never_touches_canonical_slots`` to the
    whole pipeline: a full-mode crowning duel (canonical r0) followed by a
    calibration draw (base 1000) and an evidence-confirm loop (base 4000), then
    assert (a) the champion's / challenger's canonical ``loss.json`` bytes are
    unchanged, (b) calibration draws persist under 1000, (c) evidence draws
    persist under 4000 for BOTH sides. The screen's base-3000 draws live under
    swept phantom dirs by design, so its isolation is proven by r0 being
    untouched rather than by a persisted slot.
    """
    from zicato.core.types import TournamentDecision, TournamentStructure
    from zicato.core.workspace import loss_profile_path
    from zicato.orchestrator import _confirm_gauntlet_promotion  # noqa: PLC0415
    from zicato.selection.strategy import SelectionDecision

    checks: dict[str, Any] = {}
    world = _CountingWorld({"champion": BASE_TOKENS, "challenger": BASE_TOKENS}, params.sigma)
    with pytest.MonkeyPatch.context() as mp:
        world.install(mp, persist=True)

        # (0) canonical crowning duel — writes the r0 loss.json for both sides.
        res = asyncio.run(
            _duel(
                workspace=workspace,
                seed=1,
                left_id="champion",
                right_id="challenger",
                weights=ScoringWeights(),
                replicates=1,
                match_id="crowning",
                board_subset=None,
                fast=False,
            )
        )
        canonical: dict[tuple[str, str], bytes] = {}
        for gid in ("champion", "challenger"):
            for entry in _board():
                canonical[(gid, entry.id)] = loss_profile_path(
                    workspace, "e0", gid, entry.id
                ).read_bytes()

        # (1) a calibration slice-floor draw at base 1000.
        asyncio.run(
            measure_noise_floor(
                adapter=object(),
                generation=_gen("champion"),
                board=_slice_board(2),
                weights=ScoringWeights(),
                config=_config(workspace, 9),
                workspace_root=workspace,
                epoch_id="e0",
                runs=3,
            )
        )

        # (2) an evidence-confirm loop at base 4000 (different workspace seed —
        # any slot-0 rewrite would change the canonical bytes).
        decision = SelectionDecision(
            promoted_generation_id="challenger",
            decision=TournamentDecision.PROMOTED,
            reason="forced promote for the slot-integrity proof",
            matchups=(
                MatchupResult(
                    matchup_id="crowning",
                    left_id="champion",
                    right_id="challenger",
                    left_agg=res.parent_agg,
                    right_agg=res.child_agg,
                    outcome=res.outcome,
                ),
            ),
            crowning_matchup_id="crowning",
        )
        budget = 3
        spec = TournamentStructure(
            structure="gauntlet",
            params={"promote_confidence_threshold": 0.8, "promote_confidence_replicates": budget},
        )
        confirmed, _evidence = asyncio.run(
            _confirm_gauntlet_promotion(
                decision,
                tournament_spec=spec,
                adapter=object(),
                parent_gen=_gen("champion"),
                child_gen=_gen("challenger"),
                train_board=list(_board()),
                weights=ScoringWeights(),
                config=_config(workspace, 2),
                workspace_root=workspace,
                epoch_id="e0",
                disable_drift=(),
                judge_only=False,
                fast_mode=False,
                round_index=0,
                total_rounds=1,
                beater=None,
            )
        )

        # Assertions.
        r0_unchanged = all(
            loss_profile_path(workspace, "e0", gid, entry_id).read_bytes() == before
            for (gid, entry_id), before in canonical.items()
        )
        calib_present = all(
            loss_profile_path(workspace, "e0", "champion", entry.id)
            .with_name(f"loss.r{CALIBRATION_REPLICATE_BASE}.json")
            .exists()
            for entry in _board()[:2]
        )
        evidence_present = all(
            loss_profile_path(workspace, "e0", gid, entry.id)
            .with_name(f"loss.r{EVIDENCE_REPLICATE_BASE + j}.json")
            .exists()
            for gid in ("champion", "challenger")
            for j in range(budget)
            for entry in _board()
        )
        # calibration base != evidence base != canonical (0) — bases disjoint.
        bases_disjoint = len({0, CALIBRATION_REPLICATE_BASE, EVIDENCE_REPLICATE_BASE}) == 3

        checks = {
            "canonical_r0_unchanged": bool(r0_unchanged),
            "calibration_draws_present_base_1000": bool(calib_present),
            "evidence_draws_present_base_4000_both_sides": bool(evidence_present),
            "reserved_bases_disjoint": bool(bases_disjoint),
            "evidence_confirm_terminal": str(confirmed.decision),
        }
    checks["all_pass"] = all(v is True for k, v in checks.items() if isinstance(v, bool))
    return checks


# ---------------------------------------------------------------------------
# Report assembly + CLI
# ---------------------------------------------------------------------------


def _seeds_used(params: HarnessParams) -> dict[str, Any]:
    return {
        "stable_noise_seed_tuple": "(workspace_seed, generation_id, entry_id, replicate_index)",
        "sigma": params.sigma,
        "seed_base": params.seed_base,
        "calibration_base": CALIBRATION_REPLICATE_BASE,
        "evidence_base": EVIDENCE_REPLICATE_BASE,
        "effective_replicates": EFFECTIVE_REPLICATES,
        "effective_budget": EFFECTIVE_BUDGET,
        "note": "all per-trial seeds are deterministic functions of the trial index; "
        "see the per-experiment seed multipliers in tools/cascade_oc.py",
    }


def _persistable(report: dict[str, Any]) -> dict[str, Any]:
    """The report minus the per-experiment ``wall_clock_s`` timings.

    Wall-clock varies run-to-run; stripping it from the persisted JSON makes
    the determinism claim (§5.1) literally true — the persisted report is
    byte-identical across runs and ``PYTHONHASHSEED`` values. The timings are
    still shown in the printed summary.
    """
    out = dict(report)
    for key in ("experiment_a", "experiment_b", "experiment_c"):
        sub = out.get(key)
        if isinstance(sub, dict) and "wall_clock_s" in sub:
            trimmed = dict(sub)
            trimmed.pop("wall_clock_s", None)
            out[key] = trimmed
    return out


def run_all(params: HarnessParams) -> dict[str, Any]:
    """Run every experiment and return the machine-readable report dict."""
    report: dict[str, Any] = {
        "harness": "cascade_oc",
        "doc": "docs/design/CASCADE.md §4",
        "params": asdict(params),
        "seeds": _seeds_used(params),
    }
    with tempfile.TemporaryDirectory(prefix="cascade-oc-A-") as ws:
        report["experiment_a"] = experiment_a(params, Path(ws))
    with tempfile.TemporaryDirectory(prefix="cascade-oc-B-") as ws:
        report["experiment_b"] = experiment_b(params, Path(ws))
    floor = report["experiment_a"]["full_board_floor"]
    with tempfile.TemporaryDirectory(prefix="cascade-oc-C-") as ws:
        report["experiment_c"] = experiment_c(params, Path(ws), floor)
    with tempfile.TemporaryDirectory(prefix="cascade-oc-slot-") as ws:
        report["slot_integrity"] = slot_integrity_proof(params, Path(ws))
    return report


def _smoke_params() -> HarnessParams:
    """A minimal end-to-end configuration for the default-suite smoke test."""
    return HarnessParams(
        floor_draws=3,
        rung_trials=2,
        screen_trials=3,
        b_trials=1,
        null_trials=1,
        c_trials=1,
        rung_slice_sizes=(2,),
        c_deltas=("large",),
        c_field_size=3,
        c_terminal_replicates=2,
    )


def _print_summary(report: dict[str, Any]) -> None:
    a = report["experiment_a"]
    b = report["experiment_b"]
    c = report["experiment_c"]
    slot = report["slot_integrity"]
    print("\n=== CASCADE OC HARNESS — summary ===")
    print(
        "(wall-clock, NOT persisted: "
        f"A={a.get('wall_clock_s')}s B={b.get('wall_clock_s')}s C={c.get('wall_clock_s')}s)"
    )
    print(f"full-board A/A floor (delta sd): {a['full_board_floor']:.3f}")
    print("\n[A] slice floors (grow ~1/sqrt(m) as m shrinks):")
    for m, fl in sorted(a["slice_floors"].items(), key=lambda kv: int(kv[0])):
        print(f"   m={m}: floor={fl:.3f}")
    print("[A] rung false-cut rate by (delta, slice):")
    for name, blk in a["rung_false_cut"].items():
        cells = ", ".join(
            f"m={m}:{s['false_cut_rate']:.2f}(δ/φ={s['delta_over_slice_floor']:.2f})"
            for m, s in sorted(blk["by_slice"].items(), key=lambda kv: int(kv[0]))
        )
        print(f"   {name:6s} (δ={blk['measured_delta']:.3f}): {cells}")
    v = a["veto_stage"]
    print(
        f"[A] veto stage: confirmed={v['confirmed_false_veto_rate']:.3f} "
        f"naive-any-flip={v['naive_any_flip_rate']:.3f} (σ²={v['sigma_squared']:.3f})"
    )
    print("\n[B] P(promote | ·) — cascade ON vs single-stage OFF vs naive-gate-rung:")
    for cond, row in b["by_condition"].items():
        print(
            f"   {cond:7s}: on={row['cascade_on']:.2f} "
            f"off={row['single_stage']:.2f} naive={row['naive_gate_at_every_rung']:.2f}"
        )
    print(f"\n[C] budget vs power (margin={c['margin']:.3f}, field={c['field_size']}):")
    for delta, blk in c["by_delta"].items():
        print(f"   δ={delta}:")
        for row in blk["curve"]:
            print(
                f"      {row['label']:14s} power={row['power']:.2f} "
                f"board_units={row['mean_board_units']:.0f}"
            )
        print(
            f"      {blk['baseline']['label']:14s} power={blk['baseline']['power']:.2f} "
            f"board_units={blk['baseline']['mean_board_units']:.0f}"
        )
        print(f"      build-candidate configs (report-only): {blk['build_candidate_configs']}")
    print(f"\n[§4.5] slot integrity: all_pass={slot['all_pass']}  {slot}")
    print("\n(Build/no-build is the operator's call — see CASCADE.md §5/§7.)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the cascade OC harness (CASCADE.md §4).")
    parser.add_argument("--smoke", action="store_true", help="minimal end-to-end config")
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--experiment", choices=["a", "b", "c", "slot", "all"], default="all")
    args = parser.parse_args(argv)

    params = _smoke_params() if args.smoke else HarnessParams()
    report = run_all(params)

    _print_summary(report)
    if args.out is not None:
        persisted = json.dumps(_persistable(report), indent=2, sort_keys=True)
        args.out.write_text(persisted, encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
