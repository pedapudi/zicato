"""The proposer prompt states what the contract prioritises, not membership.

The scoring weights already answer "what should this round work on"; these
tests hold the path that passes the answer along — ``build_metric_priorities``
resolving the frozen contract, ``render_metric_priorities_block`` banding it,
and ``_render_loss_summary`` reporting only terms the contract scores.

Two invariants recur and are the reason the split exists:

* a zero-weight target is ABSENT from the prompt (the scoring side's own
  omit-at-zero convention) but STILL ACCEPTED by the hypothesis validator, so
  dropping it costs no bounded retry;
* channels are peers. Drift kinds ride ``drift_loss_mean`` and judges ride
  their own channel, pass is its own bounded term, and each metric namespace
  is calibrated to its own units — so banding happens strictly within a
  channel and never across.
"""

from __future__ import annotations

from zicato.core import (
    BoardEntry,
    DriftCount,
    ExpectationResult,
    JudgeLoss,
    LossProfile,
    MetricCount,
    ScoringWeights,
)
from zicato.evolve.decision_support import _render_loss_summary, build_metric_priorities
from zicato.evolve.round_api import _declared_custom_judge_names
from zicato.proposer.prompts import (
    render_metric_priorities_block,
    render_metric_targets_block,
)
from zicato.proposer.structured import parse_experiment_json

_WEIGHTED = ScoringWeights(
    per_kind_weights={"off_topic": 3.0, "task_timeout": 0.5, "tool_error": 0.0},
    per_judge_weights={"critical_judge": 4.0, "ignored_judge": 0.0},
)


def _block(weights: ScoringWeights, losses: list[LossProfile] | None = None) -> str:
    return render_metric_priorities_block(build_metric_priorities([], weights, losses or []))


def _band_of(block: str, section: str, name: str) -> str:
    """The band label the named target is listed under, within one section."""
    for line in block.split(section)[1].splitlines():
        band, _, listed = line.partition(":")
        if name in [n.strip() for n in listed.split(",")]:
            return band.strip()
    raise AssertionError(f"{name!r} is not listed under {section!r}")


def test_a_heavier_target_is_banded_above_a_lighter_one() -> None:
    block = _block(_WEIGHTED)
    assert _band_of(block, "Built-in drift kinds", "off_topic") == "high"
    assert _band_of(block, "Built-in drift kinds", "goal_drift") == "medium"
    assert _band_of(block, "Built-in drift kinds", "task_timeout") == "low"


def test_zero_weight_targets_are_absent_rather_than_annotated() -> None:
    block = _block(_WEIGHTED)
    assert "tool_error" not in block
    assert "ignored_judge" not in block
    # …and the ones that DO score are still named.
    assert "critical_judge" in block
    assert "goal_drift" in block


def test_a_zeroed_judge_is_unadvertised_but_still_validator_accepted() -> None:
    """The prompt filter must not become a promote-path change.

    Filtering the validator's accept-list would turn a zeroed judge's movement
    from accepted into a hard parse rejection and a burned retry, so the two
    sets are deliberately separate.
    """
    assert "ignored_judge" not in _block(_WEIGHTED)
    assert "ignored_judge" in _declared_custom_judge_names([], _WEIGHTED)

    experiment = parse_experiment_json(
        """
        {"hypothesis": {"core_idea": "quieten the ignored judge",
          "modulating": ["m1"],
          "why": "the judge scores zero, but the movement must still parse",
          "expected_pass_rate_delta": "+0",
          "risks": "",
          "expected_metric_movements": [
            {"metric_name": "ignored_judge", "direction": "decrease",
             "magnitude": "medium"}]},
         "patches": [{"op": "replace", "mutation_id": "m1",
                      "new_content": "x", "rationale": "y"}]}
        """,
        epoch_id="e0",
        parent_gen="v0",
        new_gen="v1",
        mutations_by_id={"m1": _MutationStub()},
        custom_judge_names=_declared_custom_judge_names([], _WEIGHTED),
    )
    assert experiment.hypothesis.expected_metric_movements[0].metric_name == "ignored_judge"


def test_pass_rate_is_named_and_not_ranked_against_drift() -> None:
    block = _block(_WEIGHTED)
    assert "pass_rate" in block
    pass_section = block.split("Board outcome")[1]
    # Its own section, with no band label borrowed from the drift ranking.
    assert "high:" not in pass_section and "low:" not in pass_section
    # A pass-weight of zero removes it entirely, like any other target.
    assert "pass_rate" not in _block(ScoringWeights(pass_weight=0.0))


def test_the_default_contract_says_all_scored_equally() -> None:
    # per_kind_weights defaults to empty, so every kind sits at 1.0 — banding
    # them would mark all forty "high", which is noise dressed as signal.
    block = _block(ScoringWeights())
    assert "(all scored equally)" in block
    assert "high:" not in block
    # It still names every drift kind the membership rendering names today.
    membership = render_metric_targets_block(())
    for kind in ("off_topic", "tool_error", "goal_drift", "task_timeout"):
        assert kind in block and kind in membership


def test_namespaces_carry_direction_and_are_not_ranked_against_each_other() -> None:
    block = _block(_WEIGHTED)
    namespaces = block.split("Other metric namespaces")[1]
    # rubric: is scored NEGATIVE — higher is better — while cost: is positive.
    assert "rubric:" in namespaces and "higher is better" in namespaces
    assert "cost:" in namespaces and "lower is better" in namespaces
    # output: is weighted 0.0 by default: observability-only, so absent.
    assert "output:" not in namespaces


def test_namespace_metric_names_come_from_the_round_s_own_losses() -> None:
    loss = LossProfile(
        run_id="r",
        entry_id="e",
        generation_id="v0",
        epoch_id="e0",
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(kind="predicate", passed=True),
        drift_loss=0.0,
        pass_fail=True,
        metric_counts=(MetricCount(name="cost:tokens_spent", severity="", count=1200.0),),
    )
    # With losses the concrete metric is named; without them only the
    # namespace is, because nothing on the contract knows the metric names.
    assert "cost:tokens_spent" in _block(_WEIGHTED, [loss])
    assert "cost:tokens_spent" not in _block(_WEIGHTED)
    assert "cost:" in _block(_WEIGHTED)


def test_the_targets_block_falls_back_to_membership_without_priorities() -> None:
    # Every caller that holds no weights (the standalone propose, tests) keeps
    # today's rendering byte-for-byte.
    assert render_metric_targets_block(["a_judge"]) == render_metric_targets_block(["a_judge"], "")
    assert render_metric_targets_block(["a_judge"], "PRE-RENDERED") == "PRE-RENDERED"


# ---------------------------------------------------------------------------
# The loss summary reports the terms the contract scores
# ---------------------------------------------------------------------------


def _loss_with(judge: str, judge_loss: float, drift_loss: float) -> LossProfile:
    return LossProfile(
        run_id="r",
        entry_id="e",
        generation_id="v0",
        epoch_id="e0",
        drift_counts=(DriftCount(kind="off_topic", severity="warning", count=1),),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=1,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(kind="predicate", passed=True),
        drift_loss=drift_loss,
        pass_fail=True,
        per_judge_loss=(
            JudgeLoss(judge_name=judge, raw_loss=judge_loss, weight=1.0, weighted_loss=judge_loss),
        ),
    )


def test_loss_summary_omits_a_zero_weighted_term() -> None:
    losses = [_loss_with("critical_judge", 0.4, 0.89)]
    weights = ScoringWeights(
        namespace_weights={"drift:": 0.0, "judge:": 0.0, "failure:": 1.0},
        pass_weight=1.0,
    )
    summary = _render_loss_summary(losses, build_metric_priorities([], weights, losses))
    # Neither channel is scored, so neither is reported; the pass term the
    # contract does score is.
    assert "drift_loss_mean" not in summary
    assert "critical_judge" not in summary
    assert "pass_rate=1.00" in summary


def test_a_zeroed_drift_channel_leaves_the_judges_reported() -> None:
    """Judges are their own channel: turning drift off no longer silences them.

    Under the old composition judges reached the scalar through the drift
    term, so a drift-disabled contract dropped every judge from the prompt
    while they still had to be worked on — or, worse, while they genuinely
    scored nothing.
    """
    losses = [_loss_with("critical_judge", 0.4, 0.89)]
    weights = ScoringWeights(
        per_judge_weights={"critical_judge": 4.0},
        namespace_weights={"drift:": 0.0, "judge:": 1.0, "failure:": 1.0},
    )
    summary = _render_loss_summary(losses, build_metric_priorities([], weights, losses))
    assert "drift_loss_mean" not in summary
    assert "critical_judge=0.400" in summary


def test_loss_summary_names_a_heavily_weighted_judge() -> None:
    losses = [_loss_with("critical_judge", 0.4, 0.89)]
    summary = _render_loss_summary(losses, build_metric_priorities([], _WEIGHTED, losses))
    assert "drift_loss_mean=0.890" in summary
    assert "critical_judge=0.400" in summary


def test_loss_summary_sentinels_survive() -> None:
    priorities = build_metric_priorities([], _WEIGHTED, [])
    assert _render_loss_summary([], priorities) == "(no prior loss data; this is a baseline round)"
    # No priorities supplied ⇒ the unfiltered rendering, byte-for-byte.
    losses = [_loss_with("critical_judge", 0.4, 0.89)]
    assert (
        _render_loss_summary(losses)
        == "drift_loss_mean=0.890 over 1 runs, pass_rate=1.00 over 1 entries"
    )


def test_priorities_with_nothing_to_name_render_no_block() -> None:
    # An empty block would be worse than the flat list, so the renderer
    # returns the omit sentinel and the membership form stands. A contract
    # cannot reach this state through its weights any more — ``failure:`` is
    # required to be positive, so it is always a named target — but the
    # renderer's sentinel is still its contract.
    from zicato.proposer.prompts import MetricPriorities

    nothing = MetricPriorities()
    assert nothing.is_empty()
    assert render_metric_priorities_block(nothing) == ""
    assert render_metric_targets_block(("a_judge",), "") != ""


def test_board_declared_judges_resolve_at_the_default_judge_weight() -> None:
    board = [
        BoardEntry(
            id="e1",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="x",
            judges=(_JudgeSpecStub("board_judge"),),
        )
    ]
    priorities = build_metric_priorities(board, _WEIGHTED, [])
    names = [t.name for t in priorities.judges]
    # Declared on the board with no per_judge_weights entry ⇒ default weight,
    # so it is scored and ranked below the explicitly-raised judge.
    assert names == ["critical_judge", "board_judge"]


class _MutationStub:
    """Minimal stand-in for a manifest ``MutationPoint`` the validator resolves."""

    id = "m1"
    kind = "span"
    file = "agent/prompt.txt"
    current_content = "x"


class _JudgeSpecStub:
    """Minimal stand-in for a board ``JudgeSpec`` (only ``name`` is read)."""

    def __init__(self, name: str) -> None:
        self.name = name
