"""Pre-flight cluster (#106, #112) — the fixes, pinned.

Both defects lived one level above the measurement: pre-flight measured the
right thing but asked too narrow a question of it.

* **#106** — ``run_contract_preflight`` degraded exactly ``points[0]``. When
  that point is inert under the current contract the measured signal is ~0
  and a healthy board got a deterministic (never flaky) ``REFUSE``. There
  was no way to choose the point, no fallback, and no way to tell "the probe
  was inert" from "the contract is noise-limited". Fixed by probing a
  deterministic role-diverse SAMPLE and taking the max, by the distinct
  ``inert`` verdict, and by explicit selection.
* **#112** — pre-flight answered "can the contract out-signal its own noise?"
  but never "is ``promote_margin`` reachable?". The window the loop needs is
  ``noise < margin < achievable``; only the lower bound was ever checked, so
  a guaranteed-null run passed. Fixed by
  :func:`~zicato.epoch.preflight.preflight_window_verdict` plus a margin
  recommendation that scales a draw-count-stable statistic.

These began as ``xfail(strict=True)`` triage pins; the markers came off with
the fix. Tests below the two triage sections pin the fixed behaviour itself.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from zicato.core.mutation import MutationPoint
from zicato.epoch.preflight import (
    VERDICT_INERT,
    VERDICT_OK,
    VERDICT_REFUSE,
    VERDICT_WARN,
    WINDOW_EMPTY,
    WINDOW_MARGIN_ABOVE_ACHIEVABLE,
    WINDOW_MARGIN_BELOW_FLOOR,
    effective_gate_verdict,
    is_no_op_degradation,
    preflight_verdict,
    preflight_window_verdict,
    select_probe_points,
)

# ---------------------------------------------------------------------------
# Issue #106 — one inert probe point must not veto a contract
# ---------------------------------------------------------------------------


def test_preflight_accepts_a_chosen_mutation_point() -> None:
    """The probed point must be selectable (``--degrade-mutation-id``)."""
    from zicato.epoch.preflight import run_contract_preflight

    params = inspect.signature(run_contract_preflight).parameters
    assert "degrade_mutation_id" in params


def test_preflight_report_records_every_probe() -> None:
    """The report must carry the per-point signals it took the max over.

    Probing several points and reporting only the winner would hide the
    diagnosis #106 asks for; the operator needs to see that point A was
    inert and point B was not.
    """
    from zicato.epoch.preflight import PreflightReport

    fields = set(PreflightReport.__dataclass_fields__)
    assert "probed_points" in fields


def test_inert_probe_is_diagnosed_distinctly_from_noise_limited() -> None:
    """Two zero-signal causes, two different operator fixes.

    Champion draws that genuinely differ (a real, non-zero noise floor) plus
    a degraded scalar identical to their mean means the PROBE moved nothing —
    which is not the same as "your board is noise-limited", and sends an
    operator to a different fix.
    """
    verdict, signal = preflight_verdict(
        champion_scalars=(0.50, 0.60),
        degraded_scalar=0.55,  # exactly the champion mean ⇒ the probe was inert
        floor_max_abs_delta=0.10,
    )
    assert signal == pytest.approx(0.0)
    assert verdict != VERDICT_REFUSE, "an inert probe is not a noise-limited contract"


def test_genuinely_noise_limited_contract_still_refuses() -> None:
    """The protection the #106 fix must not weaken: real signal under the floor."""
    verdict, signal = preflight_verdict(
        champion_scalars=(0.50, 0.60),
        degraded_scalar=0.58,
        floor_max_abs_delta=0.10,
    )
    assert signal == pytest.approx(0.03)
    assert verdict == VERDICT_REFUSE


def test_clear_signal_still_passes() -> None:
    """And the happy path stays OK."""
    verdict, signal = preflight_verdict(
        champion_scalars=(0.50, 0.52),
        degraded_scalar=0.95,
        floor_max_abs_delta=0.05,
    )
    assert signal > 0.05
    assert verdict == VERDICT_OK


# ---------------------------------------------------------------------------
# Issue #112 — assert the whole window, not just its lower bound
# ---------------------------------------------------------------------------


def test_margin_above_achievable_signal_is_flagged() -> None:
    """``noise < margin < achievable`` — the UPPER bound must be checked too.

    The campaign numbers from #112: floor 0.080-0.106, best achievable
    +0.041, configured margin 0.10 ⇒ 71 of 72 duels rejected by construction.
    """
    verdict, which_side = preflight_window_verdict(
        noise_floor=0.08,
        promote_margin=0.10,
        achievable_signal=0.30,
    )
    assert verdict == VERDICT_OK
    assert which_side is None

    verdict, which_side = preflight_window_verdict(
        noise_floor=0.08,
        promote_margin=0.30,
        achievable_signal=0.20,
    )
    assert verdict != VERDICT_OK
    assert which_side == "margin_above_achievable"


def test_empty_window_is_flagged_distinctly() -> None:
    """When ``achievable <= noise`` NO margin is defensible — say exactly that."""
    _verdict, which_side = preflight_window_verdict(
        noise_floor=0.10,
        promote_margin=0.10,
        achievable_signal=0.041,
    )
    assert which_side == "empty_window"


def test_margin_recommendation_uses_a_draw_count_stable_statistic() -> None:
    """The recommendation must not degrade as calibration improves.

    ``max |delta|`` grows with the number of draws on an unchanged board;
    ``delta_std`` (already computed and reported alongside) does not. Basing
    the recommendation on the range statistic is the trap #112 describes.
    """
    from zicato.tournament.calibration import recommended_promote_margin

    two_draws = recommended_promote_margin(scalars=(0.50, 0.60))
    ten_draws = recommended_promote_margin(
        scalars=(0.50, 0.60, 0.51, 0.59, 0.52, 0.58, 0.49, 0.61, 0.53, 0.57)
    )
    assert (
        ten_draws <= two_draws * 1.5
    ), "the recommendation must be stable as draws accumulate, not drift upward"


# ---------------------------------------------------------------------------
# The fix, pinned — probe selection
# ---------------------------------------------------------------------------


def _point(
    mid: str,
    *,
    role: str = "",
    kind: str = "span",
    content: str = "live content",
    file: str = "agent.py",
) -> MutationPoint:
    """A mutation point in the shape ``enumerate_mutations`` returns."""
    return MutationPoint(
        id=mid,
        kind=kind,  # type: ignore[arg-type]
        file=Path("/w/agent") / file,
        source_root=Path("/w/agent"),
        line_start=1,
        line_end=1,
        content=content,
        content_hash="",
        metadata={"role": role} if role else {},
    )


def test_sample_spans_roles_before_taking_two_from_one() -> None:
    """The sample must cross the mutable surface, not walk one corner of it.

    This IS #106's mechanism: ``enumerate_mutations`` orders by
    ``(source_root, file, line_start, id)``, which says nothing about which
    points matter, so a prefix of it can be three tool descriptions in a row
    while the coordinator instruction every run exercises goes unprobed.
    """
    points = [
        _point("logic_a", role="path_logic", kind="code", content="if x:\n    y()\n"),
        _point("logic_b", role="path_logic", kind="code", content="if z:\n    w()\n"),
        _point("tool_a", role="tool_description"),
        _point("tool_b", role="tool_description"),
        _point("instr_a", role="system_instruction"),
        _point("coord", role="coordinator_routing"),
    ]
    sample, skipped = select_probe_points(points, limit=4)
    assert skipped == []
    # One per role, in first-appearance order — the second path_logic point
    # waits until every OTHER role has been sampled once.
    assert [p.id for p in sample] == ["logic_a", "tool_a", "instr_a", "coord"]

    # Widening the sample only then takes seconds from a role.
    wider, _ = select_probe_points(points, limit=6)
    assert [p.id for p in wider] == [
        "logic_a",
        "tool_a",
        "instr_a",
        "coord",
        "logic_b",
        "tool_b",
    ]


def test_sample_is_deterministic_and_defaults_to_kind_without_roles() -> None:
    """Same input, same sample — and an unannotated harness still gets spread."""
    points = [
        _point("s1", content="alpha"),
        _point("s2", content="beta"),
        _point("c1", kind="code", content="if q:\n    r()\n"),
    ]
    first, _ = select_probe_points(points, limit=2)
    second, _ = select_probe_points(points, limit=2)
    assert [p.id for p in first] == [p.id for p in second]
    # No roles ⇒ group by kind, so the code region is reached before the
    # second span rather than after it.
    assert [p.id for p in first] == ["s1", "c1"]


def test_no_op_degradations_are_skipped_without_spending_a_draw() -> None:
    """A palindromic span reverses to itself: provably inert, so probe nothing.

    Issue #106 item 3. Detectable purely from the point, so it costs zero board
    evaluations to establish — and it must not consume a slot in the sample
    either, or the ceiling would be spent learning ``signal == 0``.
    """
    palindrome = _point("pal", content="racecar")
    live = _point("live", content="asymmetric")
    assert is_no_op_degradation(palindrome) is True
    assert is_no_op_degradation(live) is False

    sample, skipped = select_probe_points([palindrome, live], limit=2)
    assert [p.id for p in sample] == ["live"]
    assert [(s.mutation_id, s.skipped) for s in skipped] == [("pal", "no_op_patch")]


def test_explicit_pin_wins_and_an_unknown_id_fails_loudly() -> None:
    """A named point is measured verbatim; a typo never silently falls back.

    Silently sampling automatically after a bad pin would report a verdict
    measured on points the operator did not choose — worse than no answer.
    """
    points = [_point("a"), _point("b"), _point("c")]
    sample, skipped = select_probe_points(points, limit=1, mutation_ids=("c", "a"))
    assert [p.id for p in sample] == ["c", "a"], "pinned order is honored, limit ignored"
    assert skipped == []

    with pytest.raises(ValueError, match="do not enumerate"):
        select_probe_points(points, limit=1, mutation_ids=("nope",))

    # A pinned no-op IS probed: the pin means "measure exactly this".
    palindrome = _point("pal", content="racecar")
    pinned, _ = select_probe_points([palindrome], limit=1, mutation_ids=("pal",))
    assert [p.id for p in pinned] == ["pal"]


def test_probe_limit_must_be_at_least_one() -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        select_probe_points([_point("a")], limit=0)


# ---------------------------------------------------------------------------
# The fix, pinned — verdict semantics
# ---------------------------------------------------------------------------


def test_inert_is_its_own_verdict_and_saturation_still_wins_over_it() -> None:
    """``inert`` needs a varying champion; with no variance it is saturation.

    The ordering matters: a saturated contract also has ``signal == 0``, but
    "the board cannot discriminate anything" is a statement about the board
    while "the probe moved nothing" is a statement about the probe. Only the
    A/A spread separates them.
    """
    inert, signal = preflight_verdict((0.50, 0.60), 0.55, 0.10)
    assert (inert, signal) == (VERDICT_INERT, 0.0)

    saturated, signal = preflight_verdict((0.55, 0.55), 0.55, 0.0)
    assert (saturated, signal) == (VERDICT_WARN, 0.0)


def test_window_lower_bound_is_reported_and_bounds_are_inclusive() -> None:
    """A margin AT either bound is named, and every window verdict is a warning.

    Bounds are inclusive on the failing side. The upper one is a WARNING rather
    than a refusal (issue #119): it compares the margin against DEGRADATION
    headroom, which does not bound how far a challenger can improve, so naming
    it is useful and enforcing it was not honest.
    """
    assert preflight_window_verdict(0.10, 0.05, 0.50) == (
        VERDICT_WARN,
        WINDOW_MARGIN_BELOW_FLOOR,
    )
    assert preflight_window_verdict(0.10, 0.10, 0.50) == (
        VERDICT_WARN,
        WINDOW_MARGIN_BELOW_FLOOR,
    ), "a margin exactly at the floor is indistinguishable from noise"
    assert preflight_window_verdict(0.10, 0.50, 0.50) == (
        VERDICT_WARN,
        WINDOW_MARGIN_ABOVE_ACHIEVABLE,
    ), "a margin exactly at the measured signal exceeds everything the probe saw"


def test_empty_window_warns_rather_than_double_gating_the_same_fact() -> None:
    """``achievable <= noise`` is already the signal verdict's refusal.

    Refusing it a second time through the window would gate one fact twice;
    what the window branch adds is the MARGIN sentence ("no value is
    defensible"), not a second veto.
    """
    verdict, which = preflight_window_verdict(0.10, 0.10, 0.041)
    assert (verdict, which) == (VERDICT_WARN, WINDOW_EMPTY)


def test_gate_verdict_refuses_only_on_the_honestly_measured_failure() -> None:
    """The hard gate stops a noise-limited contract and nothing else.

    Two cases must NOT hard-stop: the false REFUSE of #106 (an inert probe),
    and — since #119 — a margin above the measured DEGRADATION signal, which is
    not evidence a challenger cannot clear it. The latter includes records
    PERSISTED before the demotion, which still carry ``window_verdict:
    "refuse"``; honouring those would keep refusing every round on the finding
    the fix retracted.
    """
    assert effective_gate_verdict(None) is None
    assert effective_gate_verdict({}) is None
    assert effective_gate_verdict({"verdict": "ok"}) == VERDICT_OK
    assert effective_gate_verdict({"verdict": "refuse"}) == VERDICT_REFUSE
    # A legacy window-only refusal no longer escalates.
    assert (
        effective_gate_verdict(
            {
                "verdict": "ok",
                "window_verdict": "refuse",
                "window_failure": "margin_above_achievable",
            }
        )
        == VERDICT_OK
    )
    # The collapse itself is intact for any OTHER window refusal.
    assert (
        effective_gate_verdict(
            {"verdict": "ok", "window_verdict": "refuse", "window_failure": "some_future_bound"}
        )
        == VERDICT_REFUSE
    )
    # Inert stays inert — refusing here was the bug.
    assert effective_gate_verdict({"verdict": "inert"}) == VERDICT_INERT
    assert effective_gate_verdict({"verdict": "inert", "window_verdict": "warn"}) == VERDICT_INERT
    # A pre-#112 record carries no window key and behaves exactly as it did.
    assert effective_gate_verdict({"verdict": "warn"}) == VERDICT_WARN


# ---------------------------------------------------------------------------
# The fix, pinned — the margin recommendation statistic
# ---------------------------------------------------------------------------


def test_recommendation_is_scale_stable_where_the_range_is_not() -> None:
    """The property #112 actually needs, stated directly.

    Draws from a fixed distribution: as K grows the RANGE grows (it is a
    max-minus-min), while the recommendation tracks ``delta_std``, which
    converges. Pinned as a comparison against the old range-based formula so a
    regression to ``2.5 * max_abs_delta`` reds here.
    """
    from zicato.tournament.calibration import (
        MARGIN_NOISE_MULTIPLE,
        delta_spread,
        recommended_promote_margin,
    )

    # A symmetric two-point distribution sampled 2 vs 12 times: the population
    # std is IDENTICAL, but the observed range widens with the tails.
    few = (0.50, 0.60)
    many = (0.50, 0.60, 0.55, 0.55, 0.45, 0.65, 0.55, 0.55, 0.55, 0.55, 0.55, 0.55)
    range_few, std_few = delta_spread(few)
    range_many, std_many = delta_spread(many)
    assert range_many > range_few, "the range statistic drifts upward — the trap"
    assert std_many < std_few, "the std does not"

    assert recommended_promote_margin(scalars=many) < recommended_promote_margin(scalars=few)
    assert recommended_promote_margin(scalars=few) == pytest.approx(MARGIN_NOISE_MULTIPLE * std_few)


def test_recommendation_prefers_delta_std_and_falls_back_to_the_range() -> None:
    """Persisted floors always carry ``delta_std``; hand-written ones may not."""
    from zicato.tournament.calibration import (
        MARGIN_NOISE_MULTIPLE,
        recommended_promote_margin,
        recommended_promote_margin_from_floor,
    )

    assert recommended_promote_margin(delta_std=0.08) == pytest.approx(MARGIN_NOISE_MULTIPLE * 0.08)
    # No usable std ⇒ the degraded range fallback rather than silence.
    assert recommended_promote_margin(delta_std=0.0, max_abs_delta=0.12) == pytest.approx(
        MARGIN_NOISE_MULTIPLE * 0.12
    )
    # Nothing measured at all recommends nothing.
    assert recommended_promote_margin() == 0.0
    assert recommended_promote_margin(scalars=(0.5, 0.5)) == 0.0

    # The floor-dict entry point, tolerant like margin_below_floor.
    assert recommended_promote_margin_from_floor(None) is None
    assert recommended_promote_margin_from_floor({"delta_std": "junk"}) is None
    assert recommended_promote_margin_from_floor({"max_abs_delta": 0.0}) is None
    assert recommended_promote_margin_from_floor(
        {"delta_std": 0.08, "max_abs_delta": 0.30}
    ) == pytest.approx(MARGIN_NOISE_MULTIPLE * 0.08), "the std wins over the range"


# ---------------------------------------------------------------------------
# The fix, pinned — operator prose counts the probes that COST something
# ---------------------------------------------------------------------------


def test_only_probes_that_spent_a_draw_are_counted_as_evidence() -> None:
    """``probed_points`` carries the free skips too; the count must not.

    The field exists so an operator can judge whether the sample was
    representative. Reporting "best of 5 probed points" when three were dropped
    for free (a palindromic span, or a point never reached because an earlier
    probe settled the verdict) claims broader evidence than was measured — the
    exact misreading #106 is about.
    """
    from zicato.epoch.preflight import PreflightReport, ProbedPoint

    report = PreflightReport(
        epoch_id="e",
        generation_id="v0",
        verdict=VERDICT_OK,
        noise_floor_max_abs_delta=0.02,
        noise_floor_runs=5,
        champion_scalars=(0.5, 0.52),
        degraded_scalar=0.9,
        signal=0.39,
        degraded_mutation_id="live_b",
        degraded_mutation_kind="span",
        degraded_file="agent.py",
        measured_at="2026-07-01T00:00:00+00:00",
        probed_points=(
            ProbedPoint(mutation_id="pal", kind="span", file="a.py", skipped="no_op_patch"),
            ProbedPoint(mutation_id="live_a", kind="span", file="a.py", signal=0.0),
            ProbedPoint(mutation_id="live_b", kind="span", file="a.py", signal=0.39),
            ProbedPoint(mutation_id="rest", kind="span", file="a.py", skipped="verdict_settled"),
        ),
    )
    assert len(report.probed_points) == 4
    assert report.drawn_probe_count() == 2


def test_the_inert_health_finding_counts_only_drawn_probes() -> None:
    """The same rule on the persisted-record side of the same sentence."""
    from zicato.health.diagnostics import detect_preflight_verdict

    (finding,) = detect_preflight_verdict(
        {
            "verdict": VERDICT_INERT,
            "signal": 0.0,
            "noise_floor_max_abs_delta": 0.08,
            "probed_points": [
                {"mutation_id": "pal", "skipped": "no_op_patch"},
                {"mutation_id": "docs_tone", "signal": 0.0, "skipped": ""},
            ],
        }
    )
    assert finding.code == "preflight_inert_probe"
    assert "(1)" in finding.summary, "the free no-op skip is not evidence of an inert probe"


# ---------------------------------------------------------------------------
# The recommendation rule holds on the APPLIABLE recommender too
# ---------------------------------------------------------------------------


def test_the_reflection_set_gate_op_scales_delta_std_not_the_range() -> None:
    """dev-guide ch.04 §9.4 is a repo rule, and this op is machine-appliable.

    ``check_promotion_hygiene`` proposes a ``set_gate`` op an operator (or the
    applier) can land directly, so a recommendation scaled off the RANGE puts
    #112's upward drift into the contract itself: raising the calibration draw
    count on an unchanged board would raise the proposed margin toward the
    achievable signal. The range stays the COMPARISON statistic.
    """
    from zicato.core import ScoringWeights
    from zicato.reflection import practices as P
    from zicato.tournament.calibration import MARGIN_NOISE_MULTIPLE

    floor = {
        "generation_id": "g0",
        "epoch_id": "e",
        "runs": 12,
        "scalars": [1.0, 1.5],
        # A range far wider than the dispersion — the K-inflated shape.
        "max_abs_delta": 0.5,
        "delta_std": 0.04,
        "measured_at": "2026-07-01",
    }
    check = P.check_promotion_hygiene(
        weights=ScoringWeights(promote_margin=0.01),
        experiments=[{"generation_id": "g1", "outcome": {"tournament_decision": "promoted"}}],
        board_entries=[],
        noise_floor=floor,
    )
    assert check.proposed_op is not None
    proposed = check.proposed_op["args"]["promote_margin"]
    assert proposed == pytest.approx(MARGIN_NOISE_MULTIPLE * 0.04)
    assert proposed < MARGIN_NOISE_MULTIPLE * 0.5, "the range must not set the proposed margin"


# ---------------------------------------------------------------------------
# The knobs are RUNTIME-only: tuning the probe must not roll the epoch
# ---------------------------------------------------------------------------


def test_probe_knobs_are_read_from_the_runtime_block() -> None:
    """Both knobs come off ``runtime.*`` with the documented defaults."""
    import tempfile

    from zicato.core.runtime import PREFLIGHT_PROBE_POINTS_DEFAULT
    from zicato.runtime_factory import make_runtime_config

    async def _llm(system: str, user: str, model: str) -> str:
        del system, user, model
        return ""

    async def _aux(system: str, user: str, model: str) -> str:
        del system, user, model
        return ""

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        default = make_runtime_config(
            {"runtime": {}},
            workspace_root=root,
            target_call_llm=_llm,
            evaluation_call_llm=_aux,
        )
        assert default.preflight_probe_points == PREFLIGHT_PROBE_POINTS_DEFAULT
        assert default.preflight_probe_mutation_ids == ()

        tuned = make_runtime_config(
            {
                "runtime": {
                    "preflight_probe_points": 2,
                    "preflight_probe_mutation_ids": ["coordinator_instruction", "reviewer"],
                }
            },
            workspace_root=root,
            target_call_llm=_llm,
            evaluation_call_llm=_aux,
        )
        assert tuned.preflight_probe_points == 2
        assert tuned.preflight_probe_mutation_ids == ("coordinator_instruction", "reviewer")

        # A bare string would be read character-by-character as ids — the kind
        # of config typo that silently probes nothing real.
        with pytest.raises(ValueError, match="must be a LIST"):
            make_runtime_config(
                {"runtime": {"preflight_probe_mutation_ids": "coordinator_instruction"}},
                workspace_root=root,
                target_call_llm=_llm,
                evaluation_call_llm=_aux,
            )

        with pytest.raises(ValueError, match="must be >= 1"):
            make_runtime_config(
                {"runtime": {"preflight_probe_points": 0}},
                workspace_root=root,
                target_call_llm=_llm,
                evaluation_call_llm=_aux,
            )


def test_probe_knobs_do_not_move_the_contract_hash(tmp_path: Path) -> None:
    """Pre-flight tuning is a RUNTIME concern and must not roll the epoch.

    The knobs deliberately live on ``RuntimeConfig`` (``runtime.*`` in the
    workspace config) rather than on ``ScoringWeights``: which points a
    diagnostic probe degrades is not part of the frozen evaluation contract, so
    changing it must not invalidate every existing epoch's comparability. This
    asserts the property directly — two epochs whose ONLY difference is the
    probe knobs hash identically — rather than trusting that the canonicalizer
    happens not to read the runtime block.
    """
    import json

    import zicato_examples.target_0_convergence as _t0_pkg
    from zicato.epoch.lifecycle import _scoring_from_dict, load_epoch, new_epoch

    example = Path(_t0_pkg.__file__).resolve().parent
    weights = _scoring_from_dict(json.loads((example / "scoring.json").read_text()))
    brief = tmp_path / "brief.md"
    brief.write_text("# brief\n")

    def _epoch(name: str, runtime: dict[str, object]) -> str:
        workspace = tmp_path / name
        workspace.mkdir(parents=True)
        (workspace / "config.json").write_text(
            json.dumps(
                {
                    "instance_id": "default",
                    "created_at": "2026-07-01T00:00:00Z",
                    "mutable_trees": [str(example / "agent")],
                    "runtime": runtime,
                }
            )
        )
        cfg = new_epoch(
            workspace,
            name="hash-parity",
            board_source=example / "board.jsonl",
            brief_source=brief,
            weights=weights,
            auto_close_previous=False,
            proposer_path=example / "proposer",
        )
        loaded = load_epoch(workspace, cfg.id)
        assert loaded.contract_hash
        return str(loaded.contract_hash)

    baseline = _epoch("default_knobs", {})
    tuned = _epoch(
        "tuned_knobs",
        {
            "preflight_probe_points": 3,
            "preflight_probe_mutation_ids": ["style_rules"],
            "preflight_gate": "refuse",
        },
    )
    assert tuned == baseline, (
        "the pre-flight probe knobs moved the contract hash — they must stay "
        "runtime-only (the propose_parallelism precedent), or every existing "
        "epoch rolls the moment an operator tunes a diagnostic probe"
    )
