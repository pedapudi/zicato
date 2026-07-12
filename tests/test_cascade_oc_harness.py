"""Pinned operating characteristics of the SIMULATED evaluation cascade.

The measurement suite for ``docs/design/CASCADE.md §4`` — Experiments A/B/C
and the §4.5 slot-integrity proof. The harness itself (which drives the
shipped screen / racing-rung / gate / holdout / evidence decision code paths
under seeded target_0 noise) lives in :mod:`tools.cascade_oc`; this file pins
its measured numbers.

The heavy experiments are behind the ``cascade_oc`` marker, EXCLUDED from the
default ``pytest`` run (see ``pyproject.toml`` ``addopts``) so the default
suite's runtime does not grow. Run them with::

    uv run pytest -m cascade_oc tests/test_cascade_oc_harness.py -q -s

One cheap smoke test (:func:`test_cascade_oc_smoke_end_to_end`) is UNMARKED
and runs a minimal configuration end-to-end in the default suite, so the
harness cannot silently rot.

Every number here is deterministic: the noise model is seeded from
``stable_noise_seed`` (no wall clock, no ``hash()``-randomised seeds), verified
identical across ``PYTHONHASHSEED`` values. Bounds are calibrated documentation
of the measured behaviour, loose enough to survive a re-seed but tight enough
to catch a regression — never widen a bound to make a moved rate pass (a
silently widened bound is a deleted measurement; see
``04-evaluation-statistics.md §13.6``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.cascade_oc import (
    HarnessParams,
    _smoke_params,
    experiment_a,
    experiment_b,
    experiment_c,
    run_all,
    slot_integrity_proof,
)

# ---------------------------------------------------------------------------
# The default-suite smoke test — minimal config, end to end, UNMARKED.
# ---------------------------------------------------------------------------


def test_cascade_oc_smoke_end_to_end() -> None:
    """A minimal cascade run through every stage — the anti-rot guard.

    Runs the whole harness (screen veto → racing rungs → gate → holdout →
    evidence terminal, plus the slot-integrity proof) on the smallest
    configuration and checks only that the pipeline produces a well-formed
    report with the load-bearing invariants intact: the unmissable large
    effect promotes, the A/A null never promotes, and every reserved base
    stays isolated. Deliberately cheap — no rate is pinned here.
    """
    report = run_all(_smoke_params())

    a = report["experiment_a"]
    assert set(a) >= {"slice_floors", "full_board_floor", "rung_false_cut", "veto_stage"}
    assert a["full_board_floor"] > 0.0  # a real, nonzero noise scale

    b = report["experiment_b"]["by_condition"]
    assert b["null"]["cascade_on"] == 0.0  # noise never promotes through the cascade
    assert b["large"]["cascade_on"] == 1.0  # the unmissable effect promotes

    c = report["experiment_c"]["by_delta"]["large"]
    assert 0.0 <= c["curve"][0]["power"] <= 1.0
    assert c["baseline"]["mean_board_units"] > c["curve"][0]["mean_board_units"]

    assert report["slot_integrity"]["all_pass"] is True


# ---------------------------------------------------------------------------
# The full measurement — one shared run, asserted in slices.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def full_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Experiments A+B shared across the marked assertions.

    Experiment B carries the 60-trial null condition (the soundness bar), so
    this fixture is the marked suite's cost driver (~75s single-worker; the
    marked run is opt-in and excluded from the default suite)."""
    params = HarnessParams()
    return {
        "a": experiment_a(params, tmp_path_factory.mktemp("casc-a")),
        "b": experiment_b(params, tmp_path_factory.mktemp("casc-b")),
        "params": params,
    }


@pytest.mark.cascade_oc
def test_experiment_a_slice_floor_grows_as_slice_shrinks(full_report: dict) -> None:
    """§3.1/§4.2(1): the A/A floor is a function of the slice size, growing as
    the slice shrinks (roughly ∝ 1/sqrt(m)). A floor that did NOT grow on a
    smaller slice would mean the seeding stopped varying (the §4-floor bug)."""
    a = full_report["a"]
    floors = {int(m): v for m, v in a["slice_floors"].items()}
    print(f"\n[A slice floors] {floors}")
    # The full-board floor is a real, nonzero noise scale near the analytic
    # neighbourhood (~0.66 for this sigma).
    assert 0.4 <= a["full_board_floor"] <= 1.0
    # The smallest slice is materially noisier than the full board.
    assert floors[1] >= floors[5] * 1.3
    # Monotone-ish growth toward the small slices (the full board is the floor).
    assert floors[5] <= floors[4] <= floors[2]


@pytest.mark.cascade_oc
def test_experiment_a_rung_false_cut_vs_slice_floor(full_report: dict) -> None:
    """§3.1/§4.2(3,4): a rung's false-cut rate is governed by its OWN slice
    floor. An unmissable effect (δ ≳ 1× slice floor) is almost never cut; an
    effect below the slice floor is cut materially more often — the coarse-cut
    discipline (a stage may not resolve effects below its slice floor)."""
    fc = full_report["a"]["rung_false_cut"]

    def _rates(name: str) -> list[float]:
        return [s["false_cut_rate"] for s in fc[name]["by_slice"].values()]

    small, medium, large = _rates("small"), _rates("medium"), _rates("large")
    print(f"\n[A rung false-cut] small={small} medium={medium} large={large}")
    # A large effect (δ ~2-3× every slice floor) is essentially never cut.
    assert max(large) <= 0.15
    # A sub-slice-floor effect is cut materially more often than the large one.
    assert sum(small) / len(small) >= max(large) + 0.10
    # Power (1 − false-cut) is ordered by effect size on the noisiest slice.
    assert large[0] <= small[0] + 0.05  # large never worse than small


@pytest.mark.cascade_oc
def test_experiment_a_veto_stage_confirm_squaring(full_report: dict) -> None:
    """§4.2(4): the veto stage (screen) follows confirm-before-veto squaring —
    its false-veto rate is ≈ flip-rate² and far below the naive any-flip rule
    (the failing alternative), measured on the identical seeded draws."""
    v = full_report["a"]["veto_stage"]
    print(
        f"\n[A veto] confirmed={v['confirmed_false_veto_rate']:.3f} "
        f"naive={v['naive_any_flip_rate']:.3f} sigma^2={v['sigma_squared']:.3f}"
    )
    # Confirm-before-veto lands near sigma^2 (the squaring), not sigma.
    assert v["confirmed_false_veto_rate"] <= 3.0 * v["sigma_squared"]
    # The naive any-flip alternative runs materially hotter.
    assert v["naive_any_flip_rate"] >= 2.5 * v["confirmed_false_veto_rate"]


@pytest.mark.cascade_oc
def test_experiment_b_null_soundness_and_power_cost(full_report: dict) -> None:
    """§4.3: the hard soundness bar (cascade must NOT raise P(promote|null)
    above the single-stage contract's rate — here both are the evidence-gated
    zero, fact #4) AND the honest power accounting (the cascade never gains
    power over the single stage; the staging costs power at small effects).

    The null bar rests on ``null_trials`` (60) trials, not the effect
    conditions' 16 — a cheap null field bought a stiffer bound (95% Wilson
    upper ~0.06 rather than ~0.20)."""
    b = full_report["b"]["by_condition"]
    for cond, row in b.items():
        print(
            f"\n[B {cond}] n={row['n']} on={row['cascade_on']:.2f} off={row['single_stage']:.2f} "
            f"naive={row['naive_gate_at_every_rung']:.2f}"
        )

    # The null bar is measured on the larger trial count.
    assert b["null"]["n"] >= 60
    # The hard bar: no null promotion leaks through the cascade, and it is not
    # above the single-stage contract's (zero) rate.
    assert b["null"]["cascade_on"] == 0.0
    assert b["null"]["cascade_on"] <= b["null"]["single_stage"]

    # The cascade never manufactures power the single stage lacks.
    for cond in ("small", "medium", "large"):
        assert b[cond]["cascade_on"] <= b[cond]["single_stage"] + 0.05

    # The unmissable effect is preserved end-to-end.
    assert b["large"]["cascade_on"] >= 0.9
    # The staging demonstrably COSTS power at a small effect (the quantified
    # regression the harness exists to expose).
    assert b["small"]["cascade_on"] <= b["small"]["single_stage"] - 0.10

    # The failing alternative (gate at every rung) now runs on the IDENTICAL
    # seeded draws through the SAME evidence-gated terminal as the cascade
    # column (rule-vs-rule). Sharing that sound terminal, it too holds the null
    # at zero — the honest correction to the first run's 0.25 "leak", which was
    # an artifact of pairing the naive rung rule with a weaker margin terminal.
    # Soundness is the terminal's job, not the rung rule's.
    assert b["null"]["naive_gate_at_every_rung"] == 0.0


@pytest.mark.cascade_oc
def test_experiment_c_build_curve_and_do_not_build_regime(
    full_report: dict, tmp_path: Path
) -> None:
    """§4.4: the build-decision artifact. At a large (unmissable) effect the
    cascade reaches the reference power at MATERIALLY lower budget (a build
    signal); at a small effect the early rungs bleed power and NO config
    qualifies (the legitimate 'do not build' outcome). The verdict itself is
    the operator's — this only exhibits the curves."""
    params = full_report["params"]
    c = experiment_c(params, tmp_path, floor=full_report["a"]["full_board_floor"])
    for delta, blk in c["by_delta"].items():
        print(
            f"\n[C {delta}] baseline power={blk['baseline']['power']:.2f} "
            f"units={blk['baseline']['mean_board_units']:.0f}; "
            f"configs={[(r['label'], r['power'], r['mean_board_units']) for r in blk['curve']]}; "
            f"build={blk['build_candidate_configs']}"
        )

    large = c["by_delta"]["large"]
    # A build signal exists at the large effect: at least one config reaches the
    # reference power (within 5pp) at <= 75% of the reference budget.
    assert large["build_candidate_configs"], "no budget-saving config at the large effect"
    # The saving is material — the cheapest cascade config spends far less than
    # the full-board-on-every-candidate baseline.
    cheapest = min(r["mean_board_units"] for r in large["curve"])
    assert cheapest <= 0.5 * large["baseline"]["mean_board_units"]

    # The 'do not build' regime is representable: at the small effect the
    # cascade's cheap early rungs cost too much power for any config to qualify.
    small = c["by_delta"]["small"]
    assert small["build_candidate_configs"] == []


@pytest.mark.cascade_oc
def test_slot_integrity_cross_stage_independence(tmp_path: Path) -> None:
    """§4.5: every stage draws under its OWN reserved base and none clobbers the
    canonical r0 slots — the §3.2 cross-stage independence invariant made
    mechanical (the ``test_full_mode_evidence_loop_never_touches_canonical_slots``
    pattern lifted to the whole pipeline)."""
    checks = slot_integrity_proof(HarnessParams(), tmp_path)
    print(f"\n[§4.5 slot integrity] {checks}")
    assert checks["canonical_r0_unchanged"] is True
    assert checks["calibration_draws_present_base_1000"] is True
    assert checks["evidence_draws_present_base_4000_both_sides"] is True
    assert checks["reserved_bases_disjoint"] is True
    assert checks["all_pass"] is True
