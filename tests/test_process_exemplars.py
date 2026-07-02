"""Redaction + extraction tests for the process-exemplar channel.

Every normative rule of ``docs/design/PROCESS-EXEMPLARS.md`` §2–§3 maps to
a test here — the doc is the contract, this file is its enforcement:

* R1 field allowlist (default-deny; unlisted cases render as bare markers);
* R2 identity anonymization (entry ids never emitted, task ids become
  window-local tokens, offsets are relative);
* R3 free-text truncation (head/tail elision at the fixed cap);
* R4 identity-corpus scrub — exercised ADVERSARIALLY: an identity string is
  planted in EVERY event field (kept, truncated, and dropped alike) and
  must be absent from the rendered output;
* §2 cap + refresh/determinism semantics;
* the contract knob (validation, omit-at-default hash stability, and the
  epoch roll on opt-in).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from zicato.analyzer.process_exemplars import (
    _FREE_TEXT_LIMIT_CHARS,
    ExemplarEvent,
    ProcessExemplar,
    _truncate_free_text,
    extract_process_exemplars,
)
from zicato.core import Pattern
from zicato.core.types import ProposerQualityConfig
from zicato.core.workspace import events_jsonl_path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_EPOCH = "ep1"
_GEN = "v3"

#: The planted identity strings the redactor must never let through. Long
#: enough (>= 12 chars) to be corpus-scrubbed wherever they are quoted.
_ENTRY_ID = "entry-billing-refund-flow"
_TASK_PROMPT = "Refund the customer order #99871 and email a confirmation to alice@example.com"
_MODEL_OUTPUT = "I have refunded order #99871 and emailed alice@example.com the confirmation."
_TASK_ID = "task-refund-order-99871"


def _write_events(ws: Path, entry_id: str, events: list[dict[str, Any]]) -> None:
    path = events_jsonl_path(ws, _EPOCH, _GEN, entry_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _adversarial_events() -> list[dict[str, Any]]:
    """A run whose EVERY field carries identity — the §3 adversarial fixture.

    The task prompt / model output / entry id / task id are planted in
    kept-class fields (drift detail quotes the prompt), truncated-class
    fields, dropped-class fields, and the envelope — the redactor must
    strip them all while keeping the process signal.
    """
    return [
        {
            "eventId": f"evt-{_ENTRY_ID}-0",
            "runId": f"run-{_ENTRY_ID}",
            "sequence": 0,
            "runStarted": {"runId": f"run-{_ENTRY_ID}", "goalSummary": _TASK_PROMPT},
        },
        {
            "sequence": 1,
            "planSubmitted": {
                "plan": {
                    "id": f"plan-{_ENTRY_ID}",
                    "summary": _TASK_PROMPT,
                    "tasks": [
                        {"id": _TASK_ID, "title": _TASK_PROMPT, "description": _TASK_PROMPT},
                        {"id": "task-verify", "title": "verify"},
                    ],
                    "edges": [{"fromTaskId": _TASK_ID, "toTaskId": "task-verify"}],
                }
            },
        },
        {
            "sequence": 2,
            "taskStarted": {"taskId": _TASK_ID, "detail": _TASK_PROMPT},
        },
        {
            "sequence": 3,
            "agentInvocationStarted": {
                "agentName": "researcher",
                "taskId": _TASK_ID,
                "invocationId": f"inv-{_ENTRY_ID}",
            },
        },
        {
            "sequence": 4,
            "driftDetected": {
                "kind": "DRIFT_KIND_LOOPING_TOOL_CALL",
                "severity": "DRIFT_SEVERITY_WARNING",
                # Kept-class free text QUOTING the task prompt — the exact
                # leak R4 exists for.
                "detail": f"tool called 4 times with identical args while handling {_TASK_PROMPT}",
                "currentAgentId": "researcher",
                "currentTaskId": _TASK_ID,
                "triggerInput": _TASK_PROMPT,
            },
        },
        {
            # An UNLISTED payload case inside the window, carrying model
            # output — must render as a bare case marker (R1 default-deny).
            "sequence": 5,
            "taskProgress": {"taskId": _TASK_ID, "fraction": 0.5, "detail": _MODEL_OUTPUT},
        },
        {
            "sequence": 6,
            "steeringDecisionMade": {
                "detectorName": "looping",
                "outcome": "intervene",
                "chosenSeverity": "warning",
                "agentName": "researcher",
                "taskId": _TASK_ID,
                "reason": f"loop confirmed on {_TASK_ID} for goal {_TASK_PROMPT}",
            },
        },
        {
            "sequence": 7,
            "taskFailed": {
                "taskId": _TASK_ID,
                "reason": f"budget exhausted; last output was {_MODEL_OUTPUT}",
                "recoverable": True,
            },
        },
        {
            "sequence": 8,
            "agentInvocationCompleted": {
                "agentName": "researcher",
                "taskId": _TASK_ID,
                "summary": _MODEL_OUTPUT,
            },
        },
        {
            "sequence": 9,
            "runCompleted": {"outcomeSummary": _MODEL_OUTPUT},
        },
    ]


def _looping_pattern(entry_ids: tuple[str, ...] = (_ENTRY_ID,)) -> Pattern:
    return Pattern(
        id="p" * 16,
        kind="drift_kind_frequency",
        summary="drift kind 'looping_tool_call' fires in 100.0% of runs across 1 entries",
        detail={
            "drift_kind": "looping_tool_call",
            "metric_name": "drift:looping_tool_call",
            "affected_entry_ids": ",".join(entry_ids),
            "frequency": "1.000",
        },
    )


def _rendered_text(exemplars: tuple[ProcessExemplar, ...]) -> str:
    """Flatten every rendered field of every exemplar into one haystack."""
    parts: list[str] = []
    for ex in exemplars:
        parts.append(ex.anchor_label)
        parts.append(ex.pattern_kind)
        for ev in ex.events:
            parts.append(ev.case)
            for name, value in ev.fields:
                parts.append(name)
                parts.append(value)
    return "\n".join(parts)


def _extract(ws: Path, patterns: list[Pattern], cap: int = 2) -> tuple[ProcessExemplar, ...]:
    return extract_process_exemplars(
        ws,
        _EPOCH,
        patterns,
        cap,
        parent_generation_id=_GEN,
        train_entry_ids=[_ENTRY_ID],
    )


# ---------------------------------------------------------------------------
# R1 — field allowlist, default-deny
# ---------------------------------------------------------------------------


def test_window_anchors_on_the_drift_and_keeps_allowlisted_fields(tmp_path: Path) -> None:
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    exemplars = _extract(tmp_path, [_looping_pattern()])
    assert len(exemplars) == 1
    ex = exemplars[0]
    assert ex.pattern_kind == "drift_kind_frequency"
    assert ex.anchor_label == "drift kind 'looping_tool_call'"
    anchor = next(ev for ev in ex.events if ev.offset == 0)
    assert anchor.case == "drift_detected"
    fields = dict(anchor.fields)
    # Closed-vocabulary fields kept verbatim, kind/severity normalized.
    assert fields["kind"] == "looping_tool_call"
    assert fields["severity"] == "warning"
    # Harness-side identity (the agent under evolution) is kept.
    assert fields["current_agent_id"] == "researcher"
    # The raw-input field is DROPPED, not merely truncated.
    assert "trigger_input" not in fields


def test_unlisted_cases_render_as_bare_markers(tmp_path: Path) -> None:
    """``task_progress`` is not in the field policy and carries model
    output in its ``detail`` — R1's default-deny renders it as a case name
    with NO fields at all."""
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    exemplars = _extract(tmp_path, [_looping_pattern()])
    (ex,) = exemplars
    cases = {ev.case: ev for ev in ex.events}
    assert cases["task_progress"].fields == ()
    # A listed case in the same window keeps ONLY its admitted fields —
    # the invocation id is dropped, the task id anonymized.
    assert cases["agent_invocation_started"].fields == (
        ("agent_name", "researcher"),
        ("task_id", "task-1"),
    )


def test_plan_renders_as_structure_only(tmp_path: Path) -> None:
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    (ex,) = _extract(tmp_path, [_looping_pattern()])
    plan_ev = next(ev for ev in ex.events if ev.case == "plan_submitted")
    assert plan_ev.fields == (("plan", "2 tasks, 1 edges"),)


def test_offsets_are_relative_never_absolute_sequence(tmp_path: Path) -> None:
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    (ex,) = _extract(tmp_path, [_looping_pattern()])
    offsets = [ev.offset for ev in ex.events]
    # Anchor at file index 4 with radius 3 → offsets -3..+3, anchor == 0.
    assert offsets == [-3, -2, -1, 0, 1, 2, 3]


# ---------------------------------------------------------------------------
# R2 — identity anonymization
# ---------------------------------------------------------------------------


def test_entry_id_is_stripped_from_every_rendered_field(tmp_path: Path) -> None:
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    exemplars = _extract(tmp_path, [_looping_pattern()])
    assert _ENTRY_ID not in _rendered_text(exemplars)


def test_task_ids_become_window_local_tokens(tmp_path: Path) -> None:
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    (ex,) = _extract(tmp_path, [_looping_pattern()])
    haystack = _rendered_text((ex,))
    assert _TASK_ID not in haystack
    # The SAME raw task id maps to the SAME token across the window, so
    # "this one task keeps appearing" survives anonymization.
    anchor = dict(next(ev for ev in ex.events if ev.offset == 0).fields)
    failed = dict(next(ev for ev in ex.events if ev.case == "task_failed").fields)
    assert anchor["current_task_id"] == failed["task_id"] == "task-1"


# ---------------------------------------------------------------------------
# R3 — free-text truncation
# ---------------------------------------------------------------------------


def test_truncate_free_text_head_tail_elision() -> None:
    text = "H" * 200 + "MIDDLE" + "T" * 200
    out = _truncate_free_text(text)
    assert len(out) <= _FREE_TEXT_LIMIT_CHARS + len(" … ")
    assert out.startswith("H")
    assert out.endswith("T")
    assert " … " in out
    assert "MIDDLE" not in out


def test_truncate_free_text_short_text_unchanged() -> None:
    assert _truncate_free_text("short process note") == "short process note"


def test_kept_free_text_is_capped_in_rendered_output(tmp_path: Path) -> None:
    events = _adversarial_events()
    # A long process narration (no identity content) in a truncated field.
    events[7]["taskFailed"]["reason"] = "retry storm: " + "step; " * 100
    _write_events(tmp_path, _ENTRY_ID, events)
    (ex,) = _extract(tmp_path, [_looping_pattern()])
    failed = dict(next(ev for ev in ex.events if ev.case == "task_failed").fields)
    assert len(failed["reason"]) <= _FREE_TEXT_LIMIT_CHARS + len(" … ")
    assert failed["reason"].startswith("retry storm:")


# ---------------------------------------------------------------------------
# R4 — the identity-corpus scrub (adversarial)
# ---------------------------------------------------------------------------


def test_task_prompt_text_is_absent_from_output_even_when_quoted(tmp_path: Path) -> None:
    """The drift detail QUOTES the task prompt; the steering reason quotes
    it too. Both are kept-class fields — the corpus scrub must remove the
    quote while preserving the surrounding process narration."""
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    exemplars = _extract(tmp_path, [_looping_pattern()])
    haystack = _rendered_text(exemplars)
    assert _TASK_PROMPT not in haystack
    assert "order #99871" not in haystack
    assert "alice@example.com" not in haystack
    # The process signal itself survives.
    anchor = dict(next(ev for ex in exemplars for ev in ex.events if ev.offset == 0).fields)
    assert anchor["detail"].startswith("tool called 4 times")
    assert "[withheld]" in anchor["detail"]


def test_model_output_is_absent_from_output_even_when_quoted(tmp_path: Path) -> None:
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    haystack = _rendered_text(_extract(tmp_path, [_looping_pattern()]))
    assert _MODEL_OUTPUT not in haystack


def test_identity_planted_in_every_field_never_renders(tmp_path: Path) -> None:
    """The blanket adversarial assertion: NONE of the planted identity
    strings — entry id, task prompt, model output, raw task id, run id —
    survives into any rendered field of any exemplar."""
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    haystack = _rendered_text(_extract(tmp_path, [_looping_pattern()]))
    for planted in (_ENTRY_ID, _TASK_PROMPT, _MODEL_OUTPUT, _TASK_ID, f"run-{_ENTRY_ID}"):
        assert planted not in haystack, planted


# ---------------------------------------------------------------------------
# §2 — cap, refresh/determinism, anchors per pattern kind
# ---------------------------------------------------------------------------


def _second_entry_events(entry_id: str) -> list[dict[str, Any]]:
    return [
        {"sequence": 0, "planSubmitted": {"plan": {"tasks": [{"id": "a"}], "edges": []}}},
        {
            "sequence": 1,
            "driftDetected": {
                "kind": "DRIFT_KIND_OFF_TOPIC",
                "severity": "DRIFT_SEVERITY_INFO",
                "detail": "went off topic",
                "currentAgentId": "writer",
            },
        },
        {
            "sequence": 2,
            "planRevised": {
                "driftKind": "DRIFT_KIND_PLAN_DIVERGENCE",
                "severity": "DRIFT_SEVERITY_WARNING",
                "reason": "collapse repeated step",
                "revisionIndex": 2,
                "plan": {"tasks": [{"id": "a"}, {"id": "b"}], "edges": []},
            },
        },
    ]


def _off_topic_pattern(entry_id: str) -> Pattern:
    return Pattern(
        id="q" * 16,
        kind="drift_kind_frequency",
        summary="drift kind 'off_topic' fires",
        detail={"drift_kind": "off_topic", "affected_entry_ids": entry_id},
    )


def _plan_instability_pattern(entry_id: str) -> Pattern:
    return Pattern(
        id="r" * 16,
        kind="plan_revision_instability",
        summary="plan-revision instability",
        detail={"affected_entry_ids": entry_id},
    )


def test_cap_limits_total_exemplars(tmp_path: Path) -> None:
    other = "entry-two"
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    _write_events(tmp_path, other, _second_entry_events(other))
    patterns = [
        _looping_pattern(),
        _off_topic_pattern(other),
        _plan_instability_pattern(other),
    ]
    capped = extract_process_exemplars(
        tmp_path,
        _EPOCH,
        patterns,
        2,
        parent_generation_id=_GEN,
        train_entry_ids=[_ENTRY_ID, other],
    )
    assert len(capped) == 2
    assert [ex.pattern_kind for ex in capped] == [
        "drift_kind_frequency",
        "drift_kind_frequency",
    ]
    uncapped = extract_process_exemplars(
        tmp_path,
        _EPOCH,
        patterns,
        5,
        parent_generation_id=_GEN,
        train_entry_ids=[_ENTRY_ID, other],
    )
    assert len(uncapped) == 3
    assert uncapped[2].pattern_kind == "plan_revision_instability"
    assert uncapped[2].events[0].case in ("plan_submitted", "drift_detected", "plan_revised")


def test_cap_zero_extracts_nothing(tmp_path: Path) -> None:
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    assert _extract(tmp_path, [_looping_pattern()], cap=0) == ()


def test_at_most_one_exemplar_per_pattern(tmp_path: Path) -> None:
    # The run carries the anchor drift twice; only one window is minted.
    events = _adversarial_events()
    events.append(
        {
            "sequence": 9,
            "driftDetected": {
                "kind": "DRIFT_KIND_LOOPING_TOOL_CALL",
                "severity": "DRIFT_SEVERITY_WARNING",
                "detail": "second loop",
                "currentAgentId": "researcher",
            },
        }
    )
    _write_events(tmp_path, _ENTRY_ID, events)
    assert len(_extract(tmp_path, [_looping_pattern()], cap=5)) == 1


def test_extraction_is_deterministic_and_stable_across_calls(tmp_path: Path) -> None:
    """The §2 refresh semantics: same (pattern set, champion events) ⇒
    byte-identical exemplars — re-presenting the block leaks nothing new."""
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    patterns = [_looping_pattern()]
    first = _extract(tmp_path, patterns)
    second = _extract(tmp_path, patterns)
    assert first == second


def test_changing_the_pattern_set_refreshes_the_selection(tmp_path: Path) -> None:
    other = "entry-two"
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    _write_events(tmp_path, other, _second_entry_events(other))
    before = extract_process_exemplars(
        tmp_path,
        _EPOCH,
        [_looping_pattern()],
        2,
        parent_generation_id=_GEN,
        train_entry_ids=[_ENTRY_ID, other],
    )
    after = extract_process_exemplars(
        tmp_path,
        _EPOCH,
        [_off_topic_pattern(other)],
        2,
        parent_generation_id=_GEN,
        train_entry_ids=[_ENTRY_ID, other],
    )
    assert [ex.anchor_label for ex in before] == ["drift kind 'looping_tool_call'"]
    assert [ex.anchor_label for ex in after] == ["drift kind 'off_topic'"]


def test_train_slice_is_never_widened(tmp_path: Path) -> None:
    """A pattern naming a HOLDOUT entry (outside train_entry_ids) yields no
    exemplar — the extractor narrows to the intersection and never reads a
    file it was not given."""
    holdout = "entry-holdout"
    _write_events(tmp_path, holdout, _second_entry_events(holdout))
    exemplars = extract_process_exemplars(
        tmp_path,
        _EPOCH,
        [_off_topic_pattern(holdout)],
        2,
        parent_generation_id=_GEN,
        train_entry_ids=[_ENTRY_ID],  # holdout NOT in the slice
    )
    assert exemplars == ()


def test_missing_events_files_and_empty_patterns_are_tolerated(tmp_path: Path) -> None:
    assert _extract(tmp_path, [_looping_pattern()]) == ()
    assert _extract(tmp_path, []) == ()


def test_patterns_without_event_footprint_are_skipped(tmp_path: Path) -> None:
    _write_events(tmp_path, _ENTRY_ID, _adversarial_events())
    cost = Pattern(
        id="c" * 16,
        kind="cost_metric_frequency",
        summary="cost metric fires",
        detail={"metric_name": "cost:tokens_spent"},
    )
    assert _extract(tmp_path, [cost]) == ()


# ---------------------------------------------------------------------------
# The contract knob
# ---------------------------------------------------------------------------


def test_process_exemplars_knob_validates_non_negative() -> None:
    assert ProposerQualityConfig().process_exemplars == 0
    assert ProposerQualityConfig(process_exemplars=2).process_exemplars == 2
    with pytest.raises(ValueError, match="process_exemplars"):
        ProposerQualityConfig(process_exemplars=-1)


def test_contract_hash_stable_at_default_and_rolls_on_opt_in() -> None:
    """Omit-at-default: a contract that never mentions the knob hashes
    byte-identically to one pinning its 0 default; opting in rolls."""
    from zicato.core.types import ScoringWeights
    from zicato.epoch.contract import scoring_to_canon

    base = scoring_to_canon(ScoringWeights())
    pinned = scoring_to_canon(
        ScoringWeights(proposer_quality=ProposerQualityConfig(process_exemplars=0))
    )
    assert base == pinned
    assert "process_exemplars" not in json.dumps(base)

    opted = scoring_to_canon(
        ScoringWeights(proposer_quality=ProposerQualityConfig(process_exemplars=2))
    )
    assert opted != base
    assert json.loads(json.dumps(opted))["proposer_quality"]["process_exemplars"] == 2


def test_scaffold_does_not_enable_process_exemplars() -> None:
    """The deliberate asymmetry with screening (PROCESS-EXEMPLARS.md §4):
    the scaffold turns the screen ON but leaves this knob OFF."""
    from zicato.core.scoring_config import recommended_scaffold_weights

    weights = recommended_scaffold_weights()
    assert weights.proposer_quality.screen_entries > 0
    assert weights.proposer_quality.process_exemplars == 0


def test_exemplar_types_are_frozen_and_hashable() -> None:
    ev = ExemplarEvent(offset=0, case="drift_detected", fields=(("kind", "off_topic"),))
    ex = ProcessExemplar(
        pattern_id="p", pattern_kind="drift_kind_frequency", anchor_label="x", events=(ev,)
    )
    assert hash(ex) == hash(ex)
    with pytest.raises(AttributeError):
        ev.case = "other"  # type: ignore[misc]
