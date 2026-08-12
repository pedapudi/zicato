"""Identity-leak probe for the proposer's redacted query surface.

The deliverable of ``zicato.proposer.redacted_query`` is the ENVELOPE, not
the aggregates: a view that leaks board identity into the proposer
silently destroys the overfitting guarantee the tournament exists to
provide — nothing errors, nothing warns, and the champion quietly
memorizes the board. So this file is the enforcement, not a smoke test.

The fixture is an *identity-leak probe* workspace: every board entry id,
every task prompt, every model output, and the holdout slice's data are
unmistakable ``ZZQQ`` sentinel strings, planted in the exact fields a
careless view would echo (``run_started.goal_summary``,
``run_completed.summary``, ``task_failed.reason``, a drift ``detail`` that
QUOTES the task prompt, and the plan's task titles). The probe then:

1. loops over ``REDACTED_QUERY_TOOLS`` — the module's own exported tuple,
   so a newly-added tool is covered automatically and cannot skip the
   probe — and asserts no sentinel appears in any output;
2. asserts a holdout-only sentinel stays absent even when the holdout is
   the only slice with data at all;
3. asserts the fail-closed degrade (no board / no scoring / no champion
   coordinates ⇒ the explicit "unavailable" status and NO data, never a
   crash and never the whole board);
4. asserts the SECOND gate independently, by feeding
   :func:`drop_out_of_slice` a result set containing an out-of-slice entry
   id.

A leak probe passes vacuously if the tools return nothing, so the
happy-path tests below pin the positive content too: the aggregates must
actually be there, banded, while the sentinels are not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from zicato.board.jsonl import save_board
from zicato.core.workspace import board_path, events_jsonl_path, scoring_path
from zicato.epoch.lifecycle import scoring_to_dict
from zicato.proposer.redacted_query import (
    REDACTED_QUERY_TOOLS,
    _EntryFacts,
    drop_out_of_slice,
    train_slice_agent_profile,
    train_slice_drift_profile,
    train_slice_process_profile,
)
from zicato.proposer.tool_context import ProposerToolContext, bind_proposer_tool_context
from zicato.testing.fixtures import make_board_entry, make_scoring_weights

# ---------------------------------------------------------------------------
# The identity-leak probe fixture
# ---------------------------------------------------------------------------

_EPOCH = "ep-probe"
_GEN = "v7"

#: Every planted sentinel shares this marker, so one assertion covers the
#: whole family (including any sentinel a future fixture adds).
_MARKER = "ZZQQ"

_TRAIN_IDS = ("ENTRYID-ZZQQ-0001", "ENTRYID-ZZQQ-0002")
_HOLDOUT_ID = "ENTRYID-ZZQQ-HOLD"
_TASK_TEXT = "TASKTEXT-ZZQQ-secret-question about the refund policy"
_MODEL_OUT = "MODELOUT-ZZQQ-secret-answer naming the refund policy verbatim"
_HOLDOUT_MARKER = "HOLDOUT-ZZQQ-marker"

#: The sentinels asserted individually, on top of the marker sweep.
_SENTINELS = (*_TRAIN_IDS, _HOLDOUT_ID, _TASK_TEXT, _MODEL_OUT, _HOLDOUT_MARKER)


def _events(entry_id: str, *, drift_kind: str, agent: str, holdout: bool = False) -> list[Any]:
    """One run's events, with identity planted in EVERY droppable field."""
    task_id = f"task-{entry_id}"
    task_text = _HOLDOUT_MARKER if holdout else _TASK_TEXT
    model_out = _HOLDOUT_MARKER if holdout else _MODEL_OUT
    return [
        {
            "eventId": f"evt-{entry_id}-0",
            "runId": f"run-{entry_id}",
            "sequence": 0,
            "runStarted": {"runId": f"run-{entry_id}", "goalSummary": task_text},
        },
        {
            "sequence": 1,
            "planSubmitted": {
                "plan": {
                    "id": f"plan-{entry_id}",
                    "summary": task_text,
                    "tasks": [{"id": task_id, "title": task_text, "description": model_out}],
                    "edges": [],
                }
            },
        },
        {
            "sequence": 2,
            "agentInvocationStarted": {
                "agentName": agent,
                "taskId": task_id,
                "invocationId": f"inv-{entry_id}",
            },
        },
        {
            "sequence": 3,
            "driftDetected": {
                "kind": drift_kind,
                "severity": "DRIFT_SEVERITY_WARNING",
                # Free text QUOTING the task prompt — this module drops the
                # field outright rather than truncating it.
                "detail": f"looped four times while handling {task_text}",
                "currentAgentId": agent,
                "currentTaskId": task_id,
                "triggerInput": task_text,
            },
        },
        {
            "sequence": 4,
            "steeringDecisionMade": {
                "detectorName": "loop_detector",
                "outcome": "STEERING_OUTCOME_INTERVENED",
                "chosenSeverity": "DRIFT_SEVERITY_WARNING",
                "chosenInterventionLevel": "INTERVENTION_LEVEL_NUDGE",
                "agentName": "coordinator" if not holdout else f"AGENT-{_HOLDOUT_MARKER}",
                "taskId": task_id,
                "reason": model_out,
            },
        },
        {
            "sequence": 5,
            "taskFailed": {"taskId": task_id, "recoverable": False, "reason": model_out},
        },
        {"sequence": 6, "taskProgress": {"taskId": task_id, "detail": model_out}},
        {
            "sequence": 7,
            "runCompleted": {"runId": f"run-{entry_id}", "summary": model_out},
        },
    ]


def _write_events(ws: Path, entry_id: str, events: list[Any]) -> None:
    path = events_jsonl_path(ws, _EPOCH, _GEN, entry_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


def _write_contract(ws: Path) -> None:
    """Write the epoch's frozen board + scoring — the slice's only inputs.

    The holdout is declared with the explicit ``holdout`` TAG (rule 1 of
    :func:`zicato.board.split.split_board`), so the partition is exact and
    independent of board size or the hash-fraction thresholds.
    """
    entries = [make_board_entry(id=eid, input=f"{_TASK_TEXT} [{eid}]") for eid in _TRAIN_IDS]
    entries.append(make_board_entry(id=_HOLDOUT_ID, input=_HOLDOUT_MARKER, tags=("holdout",)))
    bpath = board_path(ws, _EPOCH)
    bpath.parent.mkdir(parents=True, exist_ok=True)
    save_board(entries, bpath)
    scoring_path(ws, _EPOCH).write_text(
        json.dumps(scoring_to_dict(make_scoring_weights())), encoding="utf-8"
    )


def _probe_workspace(tmp_path: Path, *, train_events: bool = True) -> Path:
    """A workspace whose every identity-bearing field is a sentinel."""
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True, exist_ok=True)
    _write_contract(ws)
    if train_events:
        for eid in _TRAIN_IDS:
            _write_events(
                ws, eid, _events(eid, drift_kind="DRIFT_KIND_LOOPING_TOOL_CALL", agent="researcher")
            )
    # The holdout run carries a drift kind and an agent name that appear
    # NOWHERE in the train slice, so any holdout read is visible as content,
    # not just as a sentinel.
    _write_events(
        ws,
        _HOLDOUT_ID,
        _events(
            _HOLDOUT_ID,
            drift_kind="DRIFT_KIND_OFF_TOPIC",
            agent=f"AGENT-{_HOLDOUT_MARKER}",
            holdout=True,
        ),
    )
    return ws


def _ctx(ws: Path, *, epoch_id: str = _EPOCH, generation_id: str = _GEN) -> ProposerToolContext:
    return ProposerToolContext(
        workspace_root=ws,
        generation_root=ws / "generations" / generation_id,
        epoch_id=epoch_id,
        mutations=(),
        generation_id=generation_id,
    )


def _call_all(ctx: ProposerToolContext) -> dict[str, str]:
    """Call EVERY exported tool under ``ctx``; ``{tool name: output}``."""
    out: dict[str, str] = {}
    with bind_proposer_tool_context(ctx):
        for tool in REDACTED_QUERY_TOOLS:
            out[tool.__name__] = tool()
    return out


def _assert_no_sentinels(name: str, text: str) -> None:
    assert _MARKER not in text, f"{name} leaked a probe sentinel: {text}"
    for sentinel in _SENTINELS:
        assert sentinel not in text, f"{name} leaked {sentinel!r}: {text}"


# ---------------------------------------------------------------------------
# 1. No exported tool may emit any planted identity
# ---------------------------------------------------------------------------


def test_no_exported_tool_leaks_any_sentinel(tmp_path: Path) -> None:
    """The probe, looped over the module's OWN exported tuple.

    Written against ``REDACTED_QUERY_TOOLS`` rather than a hand-listed set
    so a tool added to that tuple is covered by this probe automatically —
    a future tool cannot ship without passing it.
    """
    ws = _probe_workspace(tmp_path)
    outputs = _call_all(_ctx(ws))
    assert set(outputs) == {tool.__name__ for tool in REDACTED_QUERY_TOOLS}
    for name, text in outputs.items():
        _assert_no_sentinels(name, text)


def test_no_exported_tool_leaks_the_holdout_slice(tmp_path: Path) -> None:
    """The holdout's drift kind and agent never surface, even as aggregates.

    Gate 1 in action: the holdout entry is the ONLY entry carrying
    ``off_topic`` drift and the only one whose steering names a holdout
    agent, so either value appearing anywhere means a holdout event file
    was opened.
    """
    ws = _probe_workspace(tmp_path)
    for name, text in _call_all(_ctx(ws)).items():
        assert "off_topic" not in text, f"{name} read the holdout slice: {text}"
        assert "AGENT-" not in text, f"{name} read the holdout slice: {text}"


def test_holdout_only_data_yields_no_data_at_all(tmp_path: Path) -> None:
    """A holdout-only sentinel stays absent when it is the ONLY data present."""
    ws = _probe_workspace(tmp_path, train_events=False)
    for name, text in _call_all(_ctx(ws)).items():
        _assert_no_sentinels(name, text)
        payload = json.loads(text)
        assert payload["status"] == "ok"
        assert payload["entries_with_events"] == 0
        assert "no train-slice event data" in payload["note"]


# ---------------------------------------------------------------------------
# 2. The aggregates are really there — the probe must not pass vacuously
# ---------------------------------------------------------------------------


def test_drift_profile_reports_banded_train_slice_aggregates(tmp_path: Path) -> None:
    ws = _probe_workspace(tmp_path)
    with bind_proposer_tool_context(_ctx(ws)):
        payload = json.loads(train_slice_drift_profile())
    assert payload["status"] == "ok"
    assert payload["train_slice_entries"] == len(_TRAIN_IDS)
    assert payload["entries_with_events"] == len(_TRAIN_IDS)
    kinds = {row["drift_kind"]: row for row in payload["drift_kinds"]}
    assert set(kinds) == {"looping_tool_call"}
    # Banded, never an exact count.
    assert kinds["looping_tool_call"]["entries_affected"] == "~all"
    severities = {row["severity"] for row in kinds["looping_tool_call"]["severity_mix"]}
    assert severities == {"warning"}


def test_agent_profile_reports_harness_side_roles_only(tmp_path: Path) -> None:
    ws = _probe_workspace(tmp_path)
    with bind_proposer_tool_context(_ctx(ws)):
        payload = json.loads(train_slice_agent_profile())
    agents = {row["agent"]: row for row in payload["agents"]}
    assert set(agents) == {"researcher", "coordinator"}
    assert agents["researcher"]["entries_invoked"] == "~all"
    assert agents["researcher"]["entries_with_attributed_drift"] == "~all"
    assert agents["coordinator"]["entries_with_steering_decision"] == "~all"


def test_process_profile_reports_closed_vocabulary_only(tmp_path: Path) -> None:
    ws = _probe_workspace(tmp_path)
    with bind_proposer_tool_context(_ctx(ws)):
        payload = json.loads(train_slice_process_profile())
    cases = {row["event"]: row["entries_affected"] for row in payload["process_failures"]}
    assert cases == {"task_failed": "~all"}
    assert payload["unrecoverable_task_failure"] == "~all"
    outcomes = {row["outcome"] for row in payload["steering_outcomes"]}
    assert outcomes == {"STEERING_OUTCOME_INTERVENED"}
    levels = {row["level"] for row in payload["intervention_levels"]}
    assert levels == {"INTERVENTION_LEVEL_NUDGE"}


def test_free_text_process_fields_are_dropped_not_truncated(tmp_path: Path) -> None:
    """No ``detail`` / ``reason`` / ``blocker`` free text reaches the output.

    The read allowlist admits no free-text field at all, so the elision
    marker the R3 truncator would leave behind must never appear either —
    if it did, some free text had survived far enough to be truncated.
    """
    ws = _probe_workspace(tmp_path)
    for name, text in _call_all(_ctx(ws)).items():
        assert "…" not in text, f"{name} truncated free text instead of dropping it: {text}"
        assert "looped four times" not in text
        assert "loop_detector" not in text


# ---------------------------------------------------------------------------
# 3. Fail closed — never a crash, never the whole board
# ---------------------------------------------------------------------------

#: The data-bearing keys a tool must NOT emit when it degrades.
_DATA_KEYS = (
    "drift_kinds",
    "agents",
    "process_failures",
    "steering_outcomes",
    "intervention_levels",
    "reasoning_judge_classifications",
    "unrecoverable_task_failure",
    "train_slice_entries",
    "entries_with_events",
)


def _assert_fail_closed(outputs: dict[str, str]) -> None:
    for name, text in outputs.items():
        payload = json.loads(text)
        assert payload["status"] == "train slice unavailable", f"{name}: {text}"
        assert payload["reason"]
        for key in _DATA_KEYS:
            assert key not in payload, f"{name} emitted {key} while degraded: {text}"
        _assert_no_sentinels(name, text)


def test_fail_closed_when_no_board(tmp_path: Path) -> None:
    """An empty workspace degrades to the explicit unavailable status."""
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    _assert_fail_closed(_call_all(_ctx(ws)))


def test_fail_closed_when_no_scoring_config(tmp_path: Path) -> None:
    """A board with no ``scoring.json`` must NOT fall back to the whole board."""
    ws = _probe_workspace(tmp_path)
    scoring_path(ws, _EPOCH).unlink()
    _assert_fail_closed(_call_all(_ctx(ws)))


def test_fail_closed_when_scoring_is_unparseable(tmp_path: Path) -> None:
    ws = _probe_workspace(tmp_path)
    scoring_path(ws, _EPOCH).write_text("{not json", encoding="utf-8")
    _assert_fail_closed(_call_all(_ctx(ws)))


def test_fail_closed_when_board_is_unparseable(tmp_path: Path) -> None:
    ws = _probe_workspace(tmp_path)
    board_path(ws, _EPOCH).write_text('{"id": "x", "kind": "nonsense"}\n', encoding="utf-8")
    _assert_fail_closed(_call_all(_ctx(ws)))


def test_fail_closed_when_no_epoch_id(tmp_path: Path) -> None:
    ws = _probe_workspace(tmp_path)
    _assert_fail_closed(_call_all(_ctx(ws, epoch_id="")))


def test_fail_closed_when_no_champion_generation(tmp_path: Path) -> None:
    """No champion coordinates ⇒ unavailable, not a scan of some other tree."""
    ws = _probe_workspace(tmp_path)
    _assert_fail_closed(_call_all(_ctx(ws, generation_id="")))


def test_tools_refuse_outside_a_bound_context(tmp_path: Path) -> None:
    """Called with no bound round context the tools raise, never guess."""
    for tool in REDACTED_QUERY_TOOLS:
        with pytest.raises(RuntimeError, match="no bound ProposerToolContext"):
            tool()


# ---------------------------------------------------------------------------
# 4. The second gate, asserted independently of the first
# ---------------------------------------------------------------------------


def test_second_gate_drops_out_of_slice_rows() -> None:
    """``drop_out_of_slice`` filters by entry id regardless of provenance.

    Gate 1 (opening only train-slice event files) and gate 2 (this filter)
    are independent by construction; this test exercises gate 2 on a result
    set gate 1 never produced, so a refactor that weakens either one is
    caught on its own.
    """
    rows = {
        _TRAIN_IDS[0]: _EntryFacts(agents_invoked=frozenset({"researcher"})),
        _HOLDOUT_ID: _EntryFacts(agents_invoked=frozenset({"holdout-agent"})),
        "some-entry-nobody-declared": _EntryFacts(),
    }
    kept = drop_out_of_slice(rows, frozenset({_TRAIN_IDS[0]}))
    assert set(kept) == {_TRAIN_IDS[0]}


def test_second_gate_on_an_empty_slice_keeps_nothing() -> None:
    """An empty derived slice keeps NOTHING — the fail-closed direction."""
    rows = {_TRAIN_IDS[0]: _EntryFacts(), _HOLDOUT_ID: _EntryFacts()}
    assert drop_out_of_slice(rows, frozenset()) == {}
