"""Factory helpers that build valid instances of every :mod:`zicato.core.types` dataclass.

Each ``make_*`` function returns an instance of the named dataclass
with sensible defaults that pass type-level validation (and where
relevant, dataclass-level ``validate()`` methods). All kwargs map
directly onto the underlying dataclass field names, so a test
``make_foo(field=override)`` just overrides one field — no factory
plumbing on top of the dataclass field set.

Defaults are minimal and JSON-safe (strings, paths to
``/tmp``, small tuples). Tests that need realistic data should pass
their own overrides; the factories' job is to keep the construction
boilerplate one line.

A bonus :func:`make_synthetic_events_jsonl` writes a goldfive events
JSONL file with operator-specified drift / plan-revision / task
counts. When the goldfive proto stubs are importable the file is
written in proto-canonical JSON (``MessageToJson`` with sorted keys),
so a downstream ``replay_from_jsonl`` round-trips it. When goldfive is
not importable the function falls back to a permissive dict-shaped
JSONL — sufficient for tests that ``importorskip('goldfive')`` and
still want a structurally similar artifact to assert against.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from zicato.core.types import (
    BoardEntry,
    DriftCount,
    DriftMovementActual,
    EpochConfig,
    Expectation,
    ExpectedDriftMovement,
    Experiment,
    Generation,
    HypothesisSpec,
    LossProfile,
    MutationPoint,
    OutcomeRecord,
    Patch,
    Pattern,
    RunResult,
    RuntimeConfig,
    ScoringWeights,
    ScriptedTurn,
    UserPersona,
)
from zicato.testing.mock_llm import CannedCallLLM

# ---------------------------------------------------------------------------
# Board / expectation / persona
# ---------------------------------------------------------------------------


def make_expectation(
    kind: str = "expected_text",
    spec: str = "ok",
    **overrides: Any,
) -> Expectation:
    """Build a valid :class:`Expectation`.

    Defaults to ``expected_text`` matching the string ``"ok"`` and
    firing on the final output (the single-turn-friendly default).
    """
    kwargs: dict[str, Any] = {"kind": kind, "spec": spec}
    kwargs.update(overrides)
    return Expectation(**kwargs)


def make_user_persona(**overrides: Any) -> UserPersona:
    """Build a valid :class:`UserPersona` with neutral defaults."""
    kwargs: dict[str, Any] = {
        "goal": "Get a concise answer.",
        "constraints": "Be terse. Do not volunteer extra detail.",
        "stop_when": "An answer has been given.",
    }
    kwargs.update(overrides)
    return UserPersona(**kwargs)


def make_scripted_turn(text: str = "hi") -> ScriptedTurn:
    """Build a single :class:`ScriptedTurn` with the given user text."""
    return ScriptedTurn(user=text)


def make_board_entry(kind: str = "single_turn", **overrides: Any) -> BoardEntry:
    """Build a valid :class:`BoardEntry` of the given kind.

    The factory selects discriminant-field defaults appropriate to the
    requested ``kind`` so the returned entry passes
    :meth:`BoardEntry.validate`. Operator-supplied ``overrides`` win
    on a field-by-field basis; the factory does NOT re-validate after
    overrides land — tests crafting invalid entries can
    do so without the factory fighting them.
    """
    kwargs: dict[str, Any] = {
        "id": "e_default",
        "kind": kind,
        "wall_clock_budget_seconds": 60,
    }
    if kind == "single_turn":
        kwargs["input"] = "hello"
    elif kind == "multi_turn_scripted":
        kwargs["turns"] = (ScriptedTurn(user="hi"),)
        kwargs["max_turns"] = 2
    elif kind == "multi_turn_emulated":
        kwargs["user_persona"] = make_user_persona()
        kwargs["max_turns"] = 4
    elif kind == "synthetic_adversarial":
        kwargs["input"] = "trigger"
        kwargs["adversarial_agent_spec"] = "tests.fakes.bad_agent"
        kwargs["required_drift_kinds"] = ("off_topic",)
    elif kind == "synthetic_clean":
        kwargs["input"] = "hello"
    # Unknown kinds fall through with no defaults — BoardEntry.validate
    # will reject them at the test's discretion.
    kwargs.update(overrides)
    return BoardEntry(**kwargs)


# ---------------------------------------------------------------------------
# Loss / telemetry
# ---------------------------------------------------------------------------


def make_drift_count(
    kind: str = "off_topic",
    severity: str = "warning",
    count: int = 1,
) -> DriftCount:
    """Build a :class:`DriftCount` with the named drift kind and severity."""
    return DriftCount(kind=kind, severity=severity, count=count)  # type: ignore[arg-type]


def make_loss_profile(**overrides: Any) -> LossProfile:
    """Build a :class:`LossProfile` with single-run defaults.

    Defaults describe a no-drift, no-revision, ran-in-1s run with no
    expectation result and a 0.0 drift loss — i.e. the trivial passing
    profile. Tests asserting on aggregation should override the
    relevant fields.
    """
    kwargs: dict[str, Any] = {
        "run_id": "run_default",
        "entry_id": "e_default",
        "generation_id": "v0",
        "epoch_id": "epoch_default",
        "drift_counts": (),
        "plan_revisions": 0,
        "task_failure_ratio": 0.0,
        "runtime_ms": 1000,
        "wall_clock_budget_exceeded": False,
        "expectation_result": None,
        "drift_loss": 0.0,
        "pass_fail": None,
    }
    kwargs.update(overrides)
    return LossProfile(**kwargs)


# ---------------------------------------------------------------------------
# Run record / lineage
# ---------------------------------------------------------------------------


def make_run_result(**overrides: Any) -> RunResult:
    """Build a :class:`RunResult` describing a one-turn assistant reply."""
    kwargs: dict[str, Any] = {
        "run_id": "run_default",
        "entry_id": "e_default",
        "final_output": "ok",
        "transcript": ("ok",),
        "runtime_ms": 1000,
    }
    kwargs.update(overrides)
    return RunResult(**kwargs)


# ---------------------------------------------------------------------------
# Hypothesis / experiment
# ---------------------------------------------------------------------------


def make_hypothesis_spec(**overrides: Any) -> HypothesisSpec:
    """Build a :class:`HypothesisSpec` with placeholder rationale and no movements."""
    kwargs: dict[str, Any] = {
        "core_idea": "Tighten the system prompt to suppress off-topic drift.",
        "modulating": ("mut_default",),
        "why": "Pattern detector flagged off_topic concentration on the prompt span.",
        "expected_drift_movements": (
            ExpectedDriftMovement(
                kind="off_topic",
                direction="decrease",
                magnitude="small",
            ),
        ),
        "expected_pass_rate_delta": "+0.00 to +0.05",
    }
    kwargs.update(overrides)
    return HypothesisSpec(**kwargs)


def make_experiment(**overrides: Any) -> Experiment:
    """Build an :class:`Experiment` with one patch and no outcome yet."""
    patch = make_patch()
    kwargs: dict[str, Any] = {
        "id": "exp_default",
        "epoch_id": "epoch_default",
        "generation_id": "v1",
        "parent_generation_id": "v0",
        "proposed_at": "2026-01-01T00:00:00Z",
        "hypothesis": make_hypothesis_spec(modulating=(patch.mutation_id,)),
        "patches": (patch,),
        "outcome": None,
    }
    kwargs.update(overrides)
    return Experiment(**kwargs)


# ---------------------------------------------------------------------------
# Mutation surface / patches / patterns
# ---------------------------------------------------------------------------


def make_pattern(**overrides: Any) -> Pattern:
    """Build a :class:`Pattern` flagging one drift-kind hotspot."""
    kwargs: dict[str, Any] = {
        "id": "pat_default",
        "kind": "drift_kind_frequency",
        "summary": "off_topic dominates loss across the board",
        "detail": {"drift_kind": "off_topic", "share": "0.62"},
        "affected_mutation_ids": ("mut_default",),
        "severity": "warning",
    }
    kwargs.update(overrides)
    return Pattern(**kwargs)


def make_mutation_point(**overrides: Any) -> MutationPoint:
    """Build a :class:`MutationPoint` describing a span on a fake source tree."""
    kwargs: dict[str, Any] = {
        "id": "mut_default",
        "kind": "span",
        "file": Path("/tmp/zicato/test/harness/prompts.py"),
        "source_root": Path("/tmp/zicato/test/harness"),
        "line_start": 10,
        "line_end": 14,
        "content": "You are a helpful assistant.",
        "content_hash": "0" * 64,
        "metadata": {"language": "text", "role": "system_prompt"},
    }
    kwargs.update(overrides)
    return MutationPoint(**kwargs)


def make_patch(**overrides: Any) -> Patch:
    """Build a :class:`Patch` that rewrites a span on the default mutation point."""
    kwargs: dict[str, Any] = {
        "id": "patch_default",
        "mutation_id": "mut_default",
        "op": "replace",
        "new_content": "You are a terse, on-topic assistant.",
        "new_numeric": None,
        "new_enum": None,
        "rationale": "Address off_topic concentration on the system prompt.",
    }
    kwargs.update(overrides)
    return Patch(**kwargs)


# ---------------------------------------------------------------------------
# Epoch / generation / scoring
# ---------------------------------------------------------------------------


def make_scoring_weights(**overrides: Any) -> ScoringWeights:
    """Build :class:`ScoringWeights` with the dataclass-defined defaults.

    Operators rarely need to override these for type-level tests; the
    factory is here so the surface is uniform with every other type.
    """
    return ScoringWeights(**overrides)


def make_epoch_config(**overrides: Any) -> EpochConfig:
    """Build an :class:`EpochConfig` with placeholder paths and default scoring."""
    kwargs: dict[str, Any] = {
        "id": "epoch_default",
        "name": "default",
        "created_at": "2026-01-01T00:00:00Z",
        "board_path": Path("/tmp/zicato/test/epoch/board.jsonl"),
        "brief_path": Path("/tmp/zicato/test/epoch/brief.md"),
        "scoring": make_scoring_weights(),
    }
    kwargs.update(overrides)
    return EpochConfig(**kwargs)


def make_generation(**overrides: Any) -> Generation:
    """Build a :class:`Generation` describing the seed (``v0``) of an epoch."""
    kwargs: dict[str, Any] = {
        "id": "v0",
        "epoch_id": "epoch_default",
        "parent_id": None,
        "snapshot_root": Path("/tmp/zicato/test/snapshot/v0"),
        "created_at": "2026-01-01T00:00:00Z",
    }
    kwargs.update(overrides)
    return Generation(**kwargs)


# ---------------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------------


def make_runtime_config(
    harness_call_llm: Callable[[str, str, str], Awaitable[str]] | None = None,
    auxiliary_call_llm: Callable[[str, str, str], Awaitable[str]] | None = None,
    **overrides: Any,
) -> RuntimeConfig:
    """Build a :class:`RuntimeConfig` with two distinct mock callables by default.

    The distinct-callable invariant in
    :func:`zicato.core.workspace.assert_distinct_callables` is satisfied
    out of the box by constructing two SEPARATE :class:`CannedCallLLM`
    instances. Callers can pass their own callables to either slot; the
    factory does not enforce the invariant itself — that is the
    workspace helper's job — but the defaults are wired to pass it.
    """
    if harness_call_llm is None:
        harness_call_llm = CannedCallLLM(["harness-response"], model="mock-harness")
    if auxiliary_call_llm is None:
        auxiliary_call_llm = CannedCallLLM(["aux-response"], model="mock-aux")

    kwargs: dict[str, Any] = {
        "instance_id": "default",
        "workspace_root": Path("/tmp/zicato/test/workspace"),
        "harness_call_llm": harness_call_llm,
        "auxiliary_call_llm": auxiliary_call_llm,
    }
    kwargs.update(overrides)
    return RuntimeConfig(**kwargs)


# ---------------------------------------------------------------------------
# Synthetic goldfive events JSONL
# ---------------------------------------------------------------------------


# Mapping from the lowercase wire-canonical drift-kind strings (the
# values used everywhere in zicato.core.types) to the goldfive proto
# enum member names. Only the kinds we hand out via the public surface
# need to appear; unknown kinds fall back to the generic CUSTOM enum.
_DRIFT_KIND_TO_PROTO = {
    "tool_error": "DRIFT_KIND_TOOL_ERROR",
    "agent_refusal": "DRIFT_KIND_AGENT_REFUSAL",
    "plan_divergence": "DRIFT_KIND_PLAN_DIVERGENCE",
    "off_topic": "DRIFT_KIND_OFF_TOPIC",
    "looping_reasoning": "DRIFT_KIND_LOOPING_REASONING",
    "looping_tool_call": "DRIFT_KIND_LOOPING_TOOL_CALL",
    "goal_drift": "DRIFT_KIND_GOAL_DRIFT",
    "task_failed_recoverable": "DRIFT_KIND_TASK_FAILED_RECOVERABLE",
    "task_failed_fatal": "DRIFT_KIND_TASK_FAILED_FATAL",
    "hallucination_suspected": "DRIFT_KIND_HALLUCINATION_SUSPECTED",
    "safety_concern": "DRIFT_KIND_SAFETY_CONCERN",
    "intent_divergence": "DRIFT_KIND_INTENT_DIVERGENCE",
    "blocked": "DRIFT_KIND_BLOCKED",
}

_SEVERITY_TO_PROTO = {
    "info": "DRIFT_SEVERITY_INFO",
    "warning": "DRIFT_SEVERITY_WARNING",
    "critical": "DRIFT_SEVERITY_CRITICAL",
}


def _try_import_goldfive_proto() -> Any | None:
    """Best-effort import of goldfive's events_pb2 module.

    Returns the module on success, ``None`` if any import step fails.
    Used by :func:`make_synthetic_events_jsonl` to decide between the
    proto-canonical JSONL writer and the permissive dict-JSONL fallback.
    """
    try:
        from goldfive.pb.goldfive.v1 import events_pb2
    except ImportError:
        return None
    return events_pb2


def make_synthetic_events_jsonl(
    path: Path,
    *,
    drift_events: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
    plan_revisions: int = 0,
    task_failures: int = 0,
    task_starts: int = 1,
    conversation_turns: int = 1,
) -> None:
    """Write a synthetic goldfive events JSONL at ``path``.

    The file shape depends on whether goldfive's proto stubs are
    importable in the current environment:

    * If importable, every event is a real ``events_pb2.Event`` proto
      serialised by ``MessageToJson(sort_keys=True, indent=None)``. This
      shape round-trips through :func:`goldfive.sinks.persistence.replay_from_jsonl`.
    * If not importable, the function writes a permissive dict-shaped
      JSONL — one ``{"event_id": ..., "sequence": ..., <payload-kind>: {...}}``
      object per line — that downstream tests can ``json.loads`` and
      assert against without booting the proto runtime.

    Parameters
    ----------
    path:
        Output JSONL file. Parent directories are created if absent.
    drift_events:
        ``(drift_kind, severity)`` pairs. Each pair produces one
        ``DriftDetected`` event with the named kind / severity.
    plan_revisions:
        Number of ``PlanRevised`` events to emit (kind / severity hardcoded
        to ``PLAN_DIVERGENCE`` / ``WARNING``; revision_index starts at 1).
    task_failures:
        Number of ``TaskFailed`` events. Each gets a fresh ``task_id`` of
        the form ``"t_fail_{i}"``.
    task_starts:
        Number of ``TaskStarted`` events. Each gets a fresh ``task_id`` of
        the form ``"t_start_{i}"``. Defaults to 1 so the file always has
        some task activity to anchor reducer tests.
    conversation_turns:
        Number of ``ConversationStarted`` + ``ConversationEnded`` event
        pairs. Defaults to 1; pass 0 for files describing a non-
        conversational run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    events_pb2 = _try_import_goldfive_proto()

    run_id = "run_synth"
    session_id = "sess_synth"
    sequence_counter = 0

    if events_pb2 is not None:
        # Proto path — write goldfive-canonical JSON lines.
        from google.protobuf import timestamp_pb2
        from google.protobuf.json_format import MessageToJson

        def _ts(seconds: int) -> Any:
            t = timestamp_pb2.Timestamp()
            t.seconds = seconds
            return t

        def _envelope() -> Any:
            nonlocal sequence_counter
            evt = events_pb2.Event()
            evt.event_id = f"evt_{sequence_counter}"
            evt.run_id = run_id
            evt.sequence = sequence_counter
            evt.emitted_at.CopyFrom(_ts(1_700_000_000 + sequence_counter))
            evt.session_id = session_id
            sequence_counter += 1
            return evt

        lines: list[str] = []

        # RunStarted
        evt = _envelope()
        evt.run_started.run_id = run_id
        evt.run_started.goal_summary = "synthetic run"
        evt.run_started.started_at.CopyFrom(_ts(1_700_000_000))
        lines.append(MessageToJson(evt, sort_keys=True, indent=None))

        for _ in range(conversation_turns):
            evt = _envelope()
            evt.conversation_started.SetInParent()
            lines.append(MessageToJson(evt, sort_keys=True, indent=None))

        for i in range(task_starts):
            evt = _envelope()
            evt.task_started.task_id = f"t_start_{i}"
            evt.task_started.detail = "synthetic"
            lines.append(MessageToJson(evt, sort_keys=True, indent=None))

        for i in range(task_failures):
            evt = _envelope()
            evt.task_failed.task_id = f"t_fail_{i}"
            evt.task_failed.reason = "synthetic failure"
            evt.task_failed.recoverable = False
            lines.append(MessageToJson(evt, sort_keys=True, indent=None))

        for drift_kind, severity in drift_events:
            evt = _envelope()
            proto_kind_name = _DRIFT_KIND_TO_PROTO.get(drift_kind, "DRIFT_KIND_CUSTOM")
            proto_severity_name = _SEVERITY_TO_PROTO.get(severity, "DRIFT_SEVERITY_UNSPECIFIED")
            kind_enum = getattr(events_pb2, proto_kind_name, None)
            if kind_enum is None:
                # Fallback: look it up via the generated DriftKind descriptor.
                from goldfive.pb.goldfive.v1 import types_pb2

                kind_enum = getattr(types_pb2, proto_kind_name)
            sev_enum = getattr(events_pb2, proto_severity_name, None)
            if sev_enum is None:
                from goldfive.pb.goldfive.v1 import types_pb2

                sev_enum = getattr(types_pb2, proto_severity_name)
            evt.drift_detected.kind = kind_enum
            evt.drift_detected.severity = sev_enum
            evt.drift_detected.detail = f"synthetic {drift_kind}"
            evt.drift_detected.authored_by = "goldfive"
            lines.append(MessageToJson(evt, sort_keys=True, indent=None))

        for i in range(plan_revisions):
            evt = _envelope()
            # PlanRevised carries a nested Plan message; we set the
            # revision_index and drift metadata to give the event some
            # content. The plan submessage is left at its default (an
            # empty proto), which is sufficient for tests that count
            # plan-revision events.
            evt.plan_revised.revision_index = i + 1
            evt.plan_revised.reason = "synthetic revision"
            proto_kind_name = "DRIFT_KIND_PLAN_DIVERGENCE"
            proto_severity_name = "DRIFT_SEVERITY_WARNING"
            kind_enum = getattr(events_pb2, proto_kind_name, None)
            sev_enum = getattr(events_pb2, proto_severity_name, None)
            if kind_enum is None or sev_enum is None:
                from goldfive.pb.goldfive.v1 import types_pb2

                kind_enum = getattr(types_pb2, proto_kind_name)
                sev_enum = getattr(types_pb2, proto_severity_name)
            evt.plan_revised.drift_kind = kind_enum
            evt.plan_revised.severity = sev_enum
            lines.append(MessageToJson(evt, sort_keys=True, indent=None))

        for _ in range(conversation_turns):
            evt = _envelope()
            evt.conversation_ended.SetInParent()
            lines.append(MessageToJson(evt, sort_keys=True, indent=None))

        # RunCompleted as a terminal marker.
        evt = _envelope()
        evt.run_completed.SetInParent()
        lines.append(MessageToJson(evt, sort_keys=True, indent=None))

        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return

    # Fallback: dict-JSONL. Tests that don't need proto-round-trip can
    # still inspect the file shape (event_id / sequence / payload kind).
    def _dict_envelope(payload_key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal sequence_counter
        out = {
            "event_id": f"evt_{sequence_counter}",
            "run_id": run_id,
            "sequence": sequence_counter,
            "emitted_at": int(time.time()) + sequence_counter,
            "session_id": session_id,
            payload_key: dict(payload),
        }
        sequence_counter += 1
        return out

    rows: list[dict[str, Any]] = [
        _dict_envelope(
            "run_started",
            {"run_id": run_id, "goal_summary": "synthetic run"},
        )
    ]
    for _ in range(conversation_turns):
        rows.append(_dict_envelope("conversation_started", {}))
    for i in range(task_starts):
        rows.append(
            _dict_envelope(
                "task_started",
                {"task_id": f"t_start_{i}", "detail": "synthetic"},
            )
        )
    for i in range(task_failures):
        rows.append(
            _dict_envelope(
                "task_failed",
                {
                    "task_id": f"t_fail_{i}",
                    "reason": "synthetic failure",
                    "recoverable": False,
                },
            )
        )
    for drift_kind, severity in drift_events:
        rows.append(
            _dict_envelope(
                "drift_detected",
                {
                    "kind": drift_kind,
                    "severity": severity,
                    "detail": f"synthetic {drift_kind}",
                    "authored_by": "goldfive",
                },
            )
        )
    for i in range(plan_revisions):
        rows.append(
            _dict_envelope(
                "plan_revised",
                {
                    "revision_index": i + 1,
                    "reason": "synthetic revision",
                    "drift_kind": "plan_divergence",
                    "severity": "warning",
                },
            )
        )
    for _ in range(conversation_turns):
        rows.append(_dict_envelope("conversation_ended", {}))
    rows.append(_dict_envelope("run_completed", {}))

    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Outcome / drift-movement helpers (re-exported for completeness)
# ---------------------------------------------------------------------------


def make_drift_movement_actual(**overrides: Any) -> DriftMovementActual:
    """Build a :class:`DriftMovementActual` describing a small improvement.

    Convenience helper for tests that assemble an :class:`OutcomeRecord`
    by hand; not in the spec's required list but cheap to provide and
    symmetric with :func:`make_outcome_record`.
    """
    kwargs: dict[str, Any] = {
        "kind": "off_topic",
        "from_rate": 1.0,
        "to_rate": 0.5,
        "hypothesis_match": True,
    }
    kwargs.update(overrides)
    return DriftMovementActual(**kwargs)


def make_outcome_record(**overrides: Any) -> OutcomeRecord:
    """Build an :class:`OutcomeRecord` for a small-promotion test outcome."""
    kwargs: dict[str, Any] = {
        "ran_at": "2026-01-01T00:01:00Z",
        "drift_movements": (make_drift_movement_actual(),),
        "pass_rate_delta": 0.05,
        "drift_loss_delta": -0.10,
        "scalar_score_delta": 0.15,
        "tournament_decision": "promoted",
    }
    kwargs.update(overrides)
    return OutcomeRecord(**kwargs)


__all__ = [
    # Board / expectation / persona
    "make_expectation",
    "make_user_persona",
    "make_scripted_turn",
    "make_board_entry",
    # Loss / telemetry
    "make_drift_count",
    "make_loss_profile",
    # Run record / lineage
    "make_run_result",
    # Hypothesis / experiment
    "make_hypothesis_spec",
    "make_experiment",
    "make_drift_movement_actual",
    "make_outcome_record",
    # Mutation surface / patches / patterns
    "make_pattern",
    "make_mutation_point",
    "make_patch",
    # Epoch / generation / scoring
    "make_scoring_weights",
    "make_epoch_config",
    "make_generation",
    # Runtime config
    "make_runtime_config",
    # Synthetic goldfive events JSONL
    "make_synthetic_events_jsonl",
]
