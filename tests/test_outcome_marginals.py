"""Tests for Capability 2 of issue #18 — the outcome-marginal,
board-anonymized failure-signal channel to the proposer.

The design invariant under test (the whole point): feed the MARGINAL, never
the JOINT. The proposer may learn aggregate PROPERTIES of the agent's
behaviour ("over-retrieves ~40% of runs") but must NEVER be able to
reconstruct any board entry, question, or specific output. These tests pin:

* the marginals the core analyzer computes from a train slice;
* every rendered number is BUCKETED/BANDED (no exact per-run value leaks);
* the prompt is BYTE-IDENTICAL when there is no profile (back-compat);
* the recall/precision decomposition line appears when Cap-1 metrics are
  present;
* zicato ENFORCES bucketing + anonymity on the operator summarizer's
  structured output (a hook that tries to return an entry id / free string
  is sanitized away);
* the ADVERSARIAL holdout-leak test — the rendered profile leaks NO holdout
  id / question / output token, is computed from the TRAIN slice only, and
  is fully banded.
"""

from __future__ import annotations

import re

from tests._proposal_evidence import render_proposal_evidence
from zicato.analyzer.outcome_marginals import (
    OutcomeMarginalSummary,
    aggregate_outcome_marginals,
    run_operator_summarizer,
    sanitize_operator_marginals,
)
from zicato.core.types import DriftCount, ExpectationResult, LossProfile
from zicato.proposer.prompts import (
    render_failure_mode_profile,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _loss(
    entry_id: str,
    *,
    output_chars: int = 500,
    pass_fail: bool | None = None,
    score: float | None = None,
    metrics: dict[str, float] | None = None,
    looping: bool = False,
    question: str = "",
    final_output: str = "",
) -> LossProfile:
    """Build a LossProfile-shaped per-entry result for the aggregator.

    ``question`` / ``final_output`` are NOT real LossProfile fields — they
    are attached as extra attributes so the adversarial leak test can verify
    that even when a distinctive question / output token rides ALONGSIDE the
    profile, none of it reaches the rendered marginal (the aggregator reads
    only scalar / count fields).
    """
    drift_counts: tuple[DriftCount, ...] = ()
    if looping:
        drift_counts = (DriftCount(kind="looping_reasoning", severity="warning", count=3),)
    exp = (
        ExpectationResult(kind="predicate", passed=bool(pass_fail), score=score, metrics=metrics)
        if pass_fail is not None or score is not None
        else None
    )
    lp = LossProfile(
        run_id=f"run:{entry_id}",
        entry_id=entry_id,
        generation_id="v1",
        epoch_id="epoch_test",
        drift_counts=drift_counts,
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=exp,
        drift_loss=0.0,
        pass_fail=pass_fail,
        output_chars=output_chars,
        score=score,
        metrics=metrics,
    )
    # Attach non-field carriers for the adversarial test. ``object.__setattr__``
    # because LossProfile is a frozen, slotted dataclass — but it has no
    # __slots__ entry for these, so we wrap them on a thin subclass-free
    # SimpleNamespace-style holder instead.
    return _WithExtras(lp, question=question, final_output=final_output)


class _WithExtras:
    """Transparent proxy that adds question / final_output to a LossProfile.

    The aggregator reads attributes via ``getattr``; this proxy forwards every
    LossProfile attribute and adds the two distinctive carriers the
    adversarial test plants. If the aggregator ever reached for the question
    or the output, the leak test below would catch the token in the render.
    """

    def __init__(self, inner: LossProfile, *, question: str, final_output: str) -> None:
        self._inner = inner
        self.question = question
        self.final_output = final_output

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


# --------------------------------------------------------------------------
# Marginal aggregation
# --------------------------------------------------------------------------


def test_empty_slice_summary_is_empty() -> None:
    summary = aggregate_outcome_marginals([])
    assert summary.is_empty()
    assert summary.n_runs == 0


def test_generic_marginals_are_rates() -> None:
    losses = [
        _loss("a", output_chars=0),  # empty
        _loss("b", output_chars=10),  # terse
        _loss("c", output_chars=500, looping=True),  # looping
        _loss("d", output_chars=500),
        _loss("e", output_chars=500),
    ]
    s = aggregate_outcome_marginals(losses)
    assert s.n_runs == 5
    assert s.empty_rate == 1 / 5
    assert s.terse_rate == 1 / 5
    assert s.looping_rate == 1 / 5


def test_pass_rate_and_score_means() -> None:
    losses = [
        _loss("a", pass_fail=True, score=0.8),
        _loss("b", pass_fail=False, score=0.2),
        _loss("c", pass_fail=True, score=0.6),
    ]
    s = aggregate_outcome_marginals(losses)
    assert s.pass_rate == 2 / 3
    assert abs((s.mean_score or 0.0) - (0.8 + 0.2 + 0.6) / 3) < 1e-9


def test_recall_precision_decomposition_marginals() -> None:
    # Three runs: two with precision below 0.5 (over-retrieval).
    losses = [
        _loss("a", score=0.5, metrics={"precision": 0.3, "recall": 0.7}),
        _loss("b", score=0.5, metrics={"precision": 0.4, "recall": 0.6}),
        _loss("c", score=0.7, metrics={"precision": 0.8, "recall": 0.5}),
    ]
    s = aggregate_outcome_marginals(losses)
    assert abs((s.precision_mean or 0.0) - (0.3 + 0.4 + 0.8) / 3) < 1e-9
    assert abs((s.recall_mean or 0.0) - (0.7 + 0.6 + 0.5) / 3) < 1e-9
    assert s.over_retrieval_rate == 2 / 3


# --------------------------------------------------------------------------
# Rendering — banding + back-compat
# --------------------------------------------------------------------------


def test_render_empty_summary_is_empty_string() -> None:
    assert render_failure_mode_profile(OutcomeMarginalSummary()) == ""


def _render_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "current_loss_summary": "drift_loss_mean=0.1 over 5 runs",
        "patterns": (),
        "mutations": (),
    }
    base.update(overrides)
    return base


def test_prompt_byte_identical_when_no_profile() -> None:
    # No failure_profile (default) and an explicit empty string must both
    # reproduce the pre-cap-2 prompt exactly — no section, no extra newline.
    without = render_proposal_evidence(**_render_kwargs())  # type: ignore[arg-type]
    empty = render_proposal_evidence(**_render_kwargs(failure_profile=""))  # type: ignore[arg-type]
    assert without == empty
    assert "Failure-mode profile" not in without


def test_prompt_byte_identical_when_summary_empty() -> None:
    # An empty SUMMARY renders the empty string, which the prompt omits —
    # so the whole pipeline (aggregate -> render -> prompt) is byte-identical
    # to today when there is no outcome data.
    profile = render_failure_mode_profile(aggregate_outcome_marginals([]))
    with_empty = render_proposal_evidence(**_render_kwargs(failure_profile=profile))  # type: ignore[arg-type]
    without = render_proposal_evidence(**_render_kwargs())  # type: ignore[arg-type]
    assert with_empty == without


def test_decomposition_line_appears_with_metrics() -> None:
    losses = [
        _loss("a", score=0.5, metrics={"precision": 0.3, "recall": 0.6}),
        _loss("b", score=0.5, metrics={"precision": 0.3, "recall": 0.6}),
    ]
    rendered = render_failure_mode_profile(aggregate_outcome_marginals(losses))
    assert "recall:" in rendered
    assert "precision:" in rendered
    # recall (~0.6, medium) materially above precision (~0.3, low) ⇒ the
    # directional read flags over-retrieval.
    assert "over-retrieves" in rendered
    assert "over-retrieval (precision<0.5)" in rendered


def test_decomposition_line_absent_without_metrics() -> None:
    losses = [_loss("a", pass_fail=True), _loss("b", pass_fail=False)]
    rendered = render_failure_mode_profile(aggregate_outcome_marginals(losses))
    assert "recall:" not in rendered
    assert "precision" not in rendered
    # The pass-rate band still renders (the binary back-compat signal).
    assert "pass-rate" in rendered


def test_every_rendered_number_is_banded() -> None:
    # A summary with deliberately precise rates / means; the render must show
    # ONLY banded labels — no exact fraction, no raw mean.
    losses = [
        _loss(
            "a",
            output_chars=0,
            pass_fail=False,
            score=0.137,
            metrics={"precision": 0.137, "recall": 0.612},
        ),
        _loss(
            "b",
            output_chars=3,
            pass_fail=False,
            score=0.241,
            metrics={"precision": 0.241, "recall": 0.588},
        ),
        _loss(
            "c",
            output_chars=999,
            pass_fail=True,
            score=0.913,
            metrics={"precision": 0.913, "recall": 0.444},
        ),
    ]
    rendered = render_failure_mode_profile(aggregate_outcome_marginals(losses))
    # The exact per-run / mean values must not leak verbatim.
    for leak in ("0.137", "0.241", "0.612", "0.588", "0.913", "0.444", "0.666", "0.333"):
        assert leak not in rendered, f"exact value {leak} leaked into the profile"
    # Only the coarse band tokens for quality appear.
    assert re.search(r"\(~0\.[369]\)", rendered)
    # Rate bands are approximate percentages or none/~all only.
    rate_tokens = re.findall(r"~\d+%|none|~all", rendered)
    assert rate_tokens, "expected at least one banded rate token"


# --------------------------------------------------------------------------
# Operator summarizer hook — sanitization / anonymity enforcement
# --------------------------------------------------------------------------


def test_sanitize_keeps_numeric_marginals() -> None:
    out = sanitize_operator_marginals({"over_retrieval": 0.4, "misses": 0.1})
    assert out == {"over_retrieval": 0.4, "misses": 0.1}


def test_sanitize_strips_freeform_string_value() -> None:
    # A summarizer that returns a free-text note as a value is an
    # un-auditable leak vector — the value is dropped.
    out = sanitize_operator_marginals(
        {"note": "on entry q3_metrics you returned table T_42", "over_retrieval": 0.4}
    )
    assert out == {"over_retrieval": 0.4}


def test_sanitize_strips_identity_bearing_keys() -> None:
    # A question fragment / entry id as a KEY — anything with spaces, caps,
    # punctuation, or over-length — is rejected so identity cannot ride in
    # through the name. Only short, lowercase, identifier-shaped labels pass.
    out = sanitize_operator_marginals(
        {
            "What is Q3 revenue?": 0.9,  # spaces + '?' + caps
            "T_42_TABLE": 0.3,  # uppercase
            "on entry q3 you returned table t42 with these rows": 0.2,  # prose, over-length
            "over_retrieval": 0.4,  # valid: short lowercase label
        }
    )
    assert "over_retrieval" in out
    assert "What is Q3 revenue?" not in out
    assert "T_42_TABLE" not in out
    assert "on entry q3 you returned table t42 with these rows" not in out


def test_sanitize_drops_nonmapping_and_nonfinite() -> None:
    assert sanitize_operator_marginals("a free-form prose summary") == {}
    assert sanitize_operator_marginals(["a", "b"]) == {}
    assert sanitize_operator_marginals(None) == {}
    assert sanitize_operator_marginals({"x": float("nan"), "y": float("inf"), "z": 0.5}) == {
        "z": 0.5
    }
    # bool is rejected (meaningless as a rate).
    assert sanitize_operator_marginals({"flag": True}) == {}


def test_run_operator_summarizer_resolves_and_sanitizes() -> None:
    spec = "zicato_examples.target_1_presentation.predicates:search_outcome_summary"
    losses = [
        _loss("a", score=0.5, metrics={"precision": 0.3, "recall": 0.7}),
        _loss("b", score=0.5, metrics={"precision": 0.4, "recall": 0.6}),
        _loss("c", score=0.7, metrics={"precision": 0.8, "recall": 0.5}),
    ]
    out = run_operator_summarizer(spec, losses)
    # The GT-aware summarizer contributes over-retrieval / misses / means.
    assert out["over_retrieval"] == 2 / 3
    assert "mean_precision" in out and "mean_recall" in out
    # Every value is a finite float — sanitized.
    assert all(isinstance(v, float) for v in out.values())


def test_run_operator_summarizer_bad_spec_is_empty() -> None:
    assert run_operator_summarizer("nonexistent.module:fn", []) == {}
    assert run_operator_summarizer("", []) == {}


def test_run_operator_summarizer_sanitizes_a_leaky_hook() -> None:
    # A deliberately leaky summarizer that tries to return an entry id and a
    # free string is stripped down to nothing by zicato's enforcement.
    spec = "tests.test_outcome_marginals:_leaky_summarizer"
    out = run_operator_summarizer(spec, [_loss("a")])
    assert out == {}


def _leaky_summarizer(losses: list[object]) -> dict[str, object]:
    """A hostile operator hook used by the sanitization test.

    It tries to smuggle an entry id (as a key) and a free-text note (as a
    value) past the hook. zicato's :func:`sanitize_operator_marginals` must
    strip BOTH, leaving an empty mapping.
    """
    return {
        "entry q3_metrics returned T_42": 1.0,  # identity-bearing key
        "summary": "the agent over-retrieved on the revenue question",  # prose value
    }


# --------------------------------------------------------------------------
# Adversarial holdout-leak test (mandatory)
# --------------------------------------------------------------------------


def test_adversarial_holdout_and_identity_leak() -> None:
    """The whole point: the rendered profile leaks no entry/question/output.

    Build a slice where TRAIN entries and HOLDOUT entries carry distinctive
    ids / questions / output tokens. Assert that:

    1. the rendered profile contains NONE of them (no entry id, question
       text, or output token) — train OR holdout;
    2. it is computed from the TRAIN slice ONLY — a holdout entry whose
       behaviour is wildly different (always-looping, always over-retrieving)
       never moves the marginal;
    3. every number is banded (no exact per-run value leaks).
    """
    # Distinctive tokens an adversary would want reconstructed.
    train_tokens = ["TRAIN_ENTRY_001", "what is the q3 waffle revenue", "TABLE_T7_REVENUE"]
    holdout_tokens = [
        "HOLDOUT_ENTRY_999",
        "secret holdout question about raccoons",
        "TABLE_H9_SECRET",
    ]

    # Train values chosen so they band to medium/high but the EXACT numbers
    # (0.617, 0.642, ...) are not band midpoints — so a verbatim leak of any
    # exact value would be unmistakable against the coarse band labels.
    train = [
        _loss(
            "TRAIN_ENTRY_001",
            output_chars=500,
            score=0.617,
            metrics={"precision": 0.642, "recall": 0.617},
            question="what is the q3 waffle revenue",
            final_output="TABLE_T7_REVENUE rows ...",
        ),
        _loss(
            "TRAIN_ENTRY_002",
            output_chars=500,
            score=0.617,
            metrics={"precision": 0.642, "recall": 0.617},
        ),
    ]
    # Holdout entries: pathological behaviour the marginal must NOT reflect.
    holdout = [
        _loss(
            "HOLDOUT_ENTRY_999",
            output_chars=0,  # empty
            score=0.0,
            metrics={"precision": 0.01, "recall": 0.01},  # extreme over-retrieval + misses
            looping=True,
            question="secret holdout question about raccoons",
            final_output="TABLE_H9_SECRET ...",
        ),
    ]

    # The orchestrator passes ONLY the train slice. We mirror that here: the
    # summary is built over `train`, never `train + holdout`.
    summary = aggregate_outcome_marginals(train)
    rendered = render_failure_mode_profile(summary)

    # (1) No identity token — train OR holdout — appears in the render.
    for token in train_tokens + holdout_tokens:
        assert token not in rendered, f"identity token {token!r} leaked into the profile"
        assert (
            token.lower() not in rendered.lower()
        ), f"identity token {token!r} leaked (case-insensitive)"

    # (2) Train slice only: the pathological holdout entry never moves the
    # marginal. Train precision (0.642) is above 0.5 ⇒ zero over-retrieval;
    # had the looping/empty/over-retrieving holdout entry contributed, these
    # would be non-zero.
    assert summary.over_retrieval_rate == 0.0
    assert summary.looping_rate == 0.0
    assert summary.empty_rate == 0.0
    # And rendering the train+holdout slice WOULD differ — proving the holdout
    # carries signal we are deliberately withholding.
    leaked_summary = aggregate_outcome_marginals(train + holdout)
    assert leaked_summary.over_retrieval_rate != summary.over_retrieval_rate
    assert leaked_summary.looping_rate != summary.looping_rate

    # (3) Every number is banded — the EXACT per-run / mean values (none of
    # which are band midpoints) never appear; only the coarse band labels
    # (~0.3 / ~0.6 / ~0.9 quality midpoints, ~N% / none / ~all rates) do.
    for leak in ("0.617", "0.642", "0.01", "0.333", "0.666"):
        assert leak not in rendered, f"exact value {leak} leaked into the profile"
    # The render carries ONLY band tokens for every number it shows.
    numeric_tokens = re.findall(r"\d+(?:\.\d+)?", rendered)
    # "0.5" is the STATIC precision<0.5 threshold label, not a per-run value;
    # "0.3"/"0.6"/"0.9" are the quality band midpoints.
    allowed = {"0", "5", "0.3", "0.5", "0.6", "0.9"}
    for tok in numeric_tokens:
        # Rate bands render as "~N%" — strip a bare percent integer too.
        assert tok in allowed or re.fullmatch(
            r"\d{1,3}", tok
        ), f"unbanded number {tok!r} in profile"
