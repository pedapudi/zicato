"""Tests for the cross-run pattern detectors.

The detectors operate on synthetic :class:`zicato.core.LossProfile`
windows plus optional goldfive events JSONLs. Tests construct losses
inline (no fixtures file) to keep each test self-contained; tests that
need raw events synthesise a minimal events JSONL and use
``pytest.importorskip("goldfive")`` so the suite still passes in an
environment without goldfive on ``sys.path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zicato.core import BoardEntry, DriftCount, LossProfile, Pattern
from zicato.patterns import (
    ALL_DETECTORS,
    DetectorInput,
    detect_drift_kind_frequency,
    detect_hot_agents,
    detect_hot_tasks,
    detect_multi_turn_context_loss,
    detect_multi_turn_memory_failure,
    detect_patterns,
    detect_plan_revision_instability,
    get_all_detectors,
    register_detector,
)
from zicato.patterns.detectors import _pattern_id


# ---------------------------------------------------------------------------
# Loss / entry builders
# ---------------------------------------------------------------------------


def _loss(
    *,
    run_id: str,
    entry_id: str = "e1",
    generation_id: str = "g0",
    epoch_id: str = "ep0",
    drift_counts: tuple[DriftCount, ...] = (),
    plan_revisions: int = 0,
    memory_failure_count: int | None = None,
    context_loss_count: int | None = None,
    turns_completed: int | None = None,
) -> LossProfile:
    """Construct a :class:`LossProfile` with sensible defaults.

    Tests only need to override the few fields they care about.
    """

    return LossProfile(
        run_id=run_id,
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id=epoch_id,
        drift_counts=drift_counts,
        plan_revisions=plan_revisions,
        task_failure_ratio=0.0,
        runtime_ms=1000,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=0.0,
        pass_fail=None,
        turns_completed=turns_completed,
        memory_failure_count=memory_failure_count,
        context_loss_count=context_loss_count,
    )


def _single_turn_entry(entry_id: str = "e1") -> BoardEntry:
    """Default single-turn entry for tests that don't care about kind."""

    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hi",
    )


# ---------------------------------------------------------------------------
# Empty / zero-data guards
# ---------------------------------------------------------------------------


def test_every_detector_returns_empty_on_empty_input() -> None:
    inp = DetectorInput(losses=[], entries={}, events_paths={})
    for detector in ALL_DETECTORS:
        assert detector(inp) == []


def test_detect_patterns_returns_empty_on_empty_input() -> None:
    inp = DetectorInput(losses=[], entries={}, events_paths={})
    assert detect_patterns(inp) == []


# ---------------------------------------------------------------------------
# drift_kind_frequency
# ---------------------------------------------------------------------------


def test_drift_kind_frequency_fires_at_20_percent_threshold() -> None:
    # 10 losses; 3 of them have OFF_TOPIC -> 30% >= 20%.
    losses: list[LossProfile] = []
    for i in range(10):
        if i < 3:
            dc = (DriftCount(kind="off_topic", severity="warning", count=2),)
        else:
            dc = ()
        losses.append(_loss(run_id=f"r{i}", entry_id="e1", drift_counts=dc))
    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})

    patterns = detect_drift_kind_frequency(inp)
    assert len(patterns) == 1
    pat = patterns[0]
    assert pat.kind == "drift_kind_frequency"
    assert pat.detail["drift_kind"] == "off_topic"
    assert pat.detail["hits"] == "3"
    assert pat.detail["run_count"] == "10"
    assert pat.detail["max_severity"] == "warning"
    assert pat.severity == "warning"


def test_drift_kind_frequency_skips_below_threshold() -> None:
    # 10 losses; 1 with OFF_TOPIC -> 10% < 20%.
    losses = [
        _loss(
            run_id=f"r{i}",
            drift_counts=(
                (DriftCount(kind="off_topic", severity="info", count=1),) if i == 0 else ()
            ),
        )
        for i in range(10)
    ]
    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})
    assert detect_drift_kind_frequency(inp) == []


def test_drift_kind_frequency_zero_count_does_not_fire() -> None:
    # A DriftCount with count=0 must NOT be treated as a hit.
    losses = [
        _loss(
            run_id=f"r{i}",
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
        )
        for i in range(10)
    ]
    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})
    assert detect_drift_kind_frequency(inp) == []


def test_drift_kind_frequency_takes_max_severity() -> None:
    # One info hit, one critical hit, one warning hit -> max severity critical.
    losses = [
        _loss(
            run_id="r0",
            drift_counts=(DriftCount(kind="tool_error", severity="info", count=1),),
        ),
        _loss(
            run_id="r1",
            drift_counts=(DriftCount(kind="tool_error", severity="warning", count=1),),
        ),
        _loss(
            run_id="r2",
            drift_counts=(DriftCount(kind="tool_error", severity="critical", count=1),),
        ),
        _loss(run_id="r3"),
        _loss(run_id="r4"),
    ]
    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})
    patterns = detect_drift_kind_frequency(inp)
    assert len(patterns) == 1
    assert patterns[0].detail["max_severity"] == "critical"
    assert patterns[0].severity == "critical"


# ---------------------------------------------------------------------------
# plan_revision_instability
# ---------------------------------------------------------------------------


def test_plan_revision_instability_detects_single_outlier() -> None:
    # 9 losses with plan_revisions=1, 1 with plan_revisions=5.
    # Mean = (9*1 + 5) / 10 = 1.4
    # Threshold = max(2 * 1.4, 1.4 + 2) = max(2.8, 3.4) = 3.4
    # The single 5 >= 3.4 fires.
    losses = [_loss(run_id=f"r{i}", plan_revisions=1) for i in range(9)]
    losses.append(_loss(run_id="r9", entry_id="e2", plan_revisions=5))
    inp = DetectorInput(
        losses=losses,
        entries={"e1": _single_turn_entry(), "e2": _single_turn_entry("e2")},
        events_paths={},
    )

    patterns = detect_plan_revision_instability(inp)
    assert len(patterns) == 1
    pat = patterns[0]
    assert pat.kind == "plan_revision_instability"
    assert pat.detail["outlier_run_count"] == "1"
    assert "r9" in pat.detail["outlier_run_ids"]
    assert pat.detail["max_revisions"] == "5"
    assert "e2" in pat.detail["affected_entry_ids"]


def test_plan_revision_instability_quiet_window_does_not_fire() -> None:
    losses = [_loss(run_id=f"r{i}", plan_revisions=1) for i in range(10)]
    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})
    assert detect_plan_revision_instability(inp) == []


# ---------------------------------------------------------------------------
# multi_turn_memory_failure / context_loss
# ---------------------------------------------------------------------------


def test_multi_turn_memory_failure_fires_at_40_percent() -> None:
    # 5 multi-turn losses; 2 have memory_failure_count=1 -> 40% > 30%.
    losses = [
        _loss(run_id="r0", entry_id="e1", memory_failure_count=1, turns_completed=3),
        _loss(run_id="r1", entry_id="e1", memory_failure_count=1, turns_completed=3),
        _loss(run_id="r2", entry_id="e1", memory_failure_count=0, turns_completed=3),
        _loss(run_id="r3", entry_id="e1", memory_failure_count=0, turns_completed=3),
        _loss(run_id="r4", entry_id="e1", memory_failure_count=0, turns_completed=3),
    ]
    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})

    patterns = detect_multi_turn_memory_failure(inp)
    assert len(patterns) == 1
    pat = patterns[0]
    assert pat.kind == "multi_turn_memory_failure"
    assert pat.detail["entry_id"] == "e1"
    assert pat.detail["positive_run_count"] == "2"
    assert pat.detail["run_count"] == "5"
    assert pat.detail["max_count"] == "1"


def test_multi_turn_memory_failure_ignores_single_turn_losses() -> None:
    # memory_failure_count=None on single-turn losses -> excluded.
    losses = [_loss(run_id=f"r{i}", memory_failure_count=None) for i in range(5)]
    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})
    assert detect_multi_turn_memory_failure(inp) == []


def test_multi_turn_memory_failure_below_30_percent_does_not_fire() -> None:
    # 5 multi-turn losses; 1 positive -> 20%.
    losses = [
        _loss(run_id="r0", memory_failure_count=1, turns_completed=3),
        _loss(run_id="r1", memory_failure_count=0, turns_completed=3),
        _loss(run_id="r2", memory_failure_count=0, turns_completed=3),
        _loss(run_id="r3", memory_failure_count=0, turns_completed=3),
        _loss(run_id="r4", memory_failure_count=0, turns_completed=3),
    ]
    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})
    assert detect_multi_turn_memory_failure(inp) == []


def test_multi_turn_context_loss_fires_with_same_shape() -> None:
    # Same threshold semantics as memory_failure.
    losses = [
        _loss(run_id="r0", context_loss_count=2, turns_completed=4),
        _loss(run_id="r1", context_loss_count=1, turns_completed=4),
        _loss(run_id="r2", context_loss_count=0, turns_completed=4),
        _loss(run_id="r3", context_loss_count=0, turns_completed=4),
        _loss(run_id="r4", context_loss_count=0, turns_completed=4),
    ]
    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})

    patterns = detect_multi_turn_context_loss(inp)
    assert len(patterns) == 1
    assert patterns[0].kind == "multi_turn_context_loss"
    assert patterns[0].detail["positive_run_count"] == "2"


# ---------------------------------------------------------------------------
# Goldfive-event-backed detectors (skip when goldfive isn't importable)
# ---------------------------------------------------------------------------


def _write_events_jsonl(path: Path, events: list[object]) -> None:
    """Serialise a list of goldfive proto Events to a JSONL file.

    Uses ``MessageToJson`` so the file round-trips through
    ``replay_from_jsonl``. Each event lands on its own line per
    goldfive's persistence sink contract.
    """

    from google.protobuf.json_format import MessageToJson  # type: ignore[import-not-found]

    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            # ``indent=None`` would default to two-space pretty-printing;
            # we want compact JSONL (one event per line).
            f.write(MessageToJson(ev, indent=0).replace("\n", "") + "\n")


def _new_event(pb, sequence: int, **payload_kwargs: object) -> object:
    """Build one goldfive Event with the requested payload populated.

    Exactly one ``payload_kwargs`` key is expected, e.g.
    ``task_started={"task_id": "t1"}``. The helper assigns the inner
    fields onto the matching oneof member.
    """

    ev = pb.Event()
    ev.run_id = "synthetic"
    ev.sequence = sequence
    [(case, fields)] = payload_kwargs.items()
    sub = getattr(ev, case)
    for k, v in fields.items():  # type: ignore[union-attr]
        setattr(sub, k, v)
    return ev


def test_detect_hot_tasks_flags_high_failure_rate(tmp_path: Path) -> None:
    pytest.importorskip("goldfive")
    from goldfive.sinks.persistence import _events_module  # type: ignore[import-not-found]

    pb = _events_module()
    # task t_hot: 5 starts, 5 failures (rate 1.0)
    # task t_warm: 5 starts, 1 failure (rate 0.2)
    # task t_cold: 5 starts, 0 failures (rate 0.0)
    # median rate = 0.2; threshold = max(0.4, 0.5) = 0.5 -> only t_hot fires.
    events: list[object] = []
    seq = 0
    for task_id, starts, fails in [("t_hot", 5, 5), ("t_warm", 5, 1), ("t_cold", 5, 0)]:
        for _ in range(starts):
            events.append(_new_event(pb, seq, task_started={"task_id": task_id}))
            seq += 1
        for _ in range(fails):
            events.append(
                _new_event(
                    pb,
                    seq,
                    task_failed={"task_id": task_id, "reason": "x", "recoverable": False},
                )
            )
            seq += 1

    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(events_path, events)

    losses = [_loss(run_id="r0", entry_id="e1")]
    inp = DetectorInput(
        losses=losses,
        entries={"e1": _single_turn_entry()},
        events_paths={"e1": events_path},
    )
    patterns = detect_hot_tasks(inp)
    assert len(patterns) == 1
    pat = patterns[0]
    assert pat.kind == "hot_task"
    assert pat.detail["task_id"] == "t_hot"
    assert pat.detail["entry_id"] == "e1"
    assert pat.detail["fail_or_block_rate"] == "1.000"


def test_detect_hot_tasks_counts_blocks_too(tmp_path: Path) -> None:
    pytest.importorskip("goldfive")
    from goldfive.sinks.persistence import _events_module  # type: ignore[import-not-found]

    pb = _events_module()
    events: list[object] = []
    seq = 0
    # Two tasks, both started 4 times. t_block has 4 blocks; t_clean has none.
    for _ in range(4):
        events.append(_new_event(pb, seq, task_started={"task_id": "t_block"}))
        seq += 1
        events.append(_new_event(pb, seq, task_started={"task_id": "t_clean"}))
        seq += 1
    for _ in range(4):
        events.append(
            _new_event(
                pb,
                seq,
                task_blocked={"task_id": "t_block", "blocker": "x", "needed": "y"},
            )
        )
        seq += 1

    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(events_path, events)

    inp = DetectorInput(
        losses=[_loss(run_id="r0")],
        entries={"e1": _single_turn_entry()},
        events_paths={"e1": events_path},
    )
    patterns = detect_hot_tasks(inp)
    assert len(patterns) == 1
    assert patterns[0].detail["task_id"] == "t_block"


def test_detect_hot_tasks_returns_empty_when_no_events_files() -> None:
    # No events_paths at all -> detector exits cleanly with no patterns.
    inp = DetectorInput(
        losses=[_loss(run_id="r0")],
        entries={"e1": _single_turn_entry()},
        events_paths={},
    )
    assert detect_hot_tasks(inp) == []


def test_detect_hot_tasks_skips_missing_files(tmp_path: Path) -> None:
    # Path that doesn't exist -> replay returns None -> detector skips it.
    inp = DetectorInput(
        losses=[_loss(run_id="r0")],
        entries={"e1": _single_turn_entry()},
        events_paths={"e1": tmp_path / "does_not_exist.jsonl"},
    )
    assert detect_hot_tasks(inp) == []


def test_detect_hot_agents_flags_disproportionate_drift(tmp_path: Path) -> None:
    pytest.importorskip("goldfive")
    from goldfive.sinks.persistence import _events_module  # type: ignore[import-not-found]

    pb = _events_module()
    # agent_a: 10 drifts (hot). agent_b: 1 drift. agent_c: 0 drifts.
    # mean = (10 + 1 + 0) / 3 = 3.67; threshold = max(7.33, 3) = 7.33
    # -> only agent_a (10) clears the bar.
    events: list[object] = []
    seq = 0
    for name in ["agent_a", "agent_b", "agent_c"]:
        ev = pb.Event()
        ev.run_id = "synthetic"
        ev.sequence = seq
        ev.agent_invocation_started.agent_name = name
        ev.agent_invocation_started.invocation_id = f"inv_{name}"
        events.append(ev)
        seq += 1
    for _ in range(10):
        ev = pb.Event()
        ev.run_id = "synthetic"
        ev.sequence = seq
        ev.drift_detected.current_agent_id = "agent_a"
        ev.drift_detected.detail = "x"
        events.append(ev)
        seq += 1
    ev = pb.Event()
    ev.run_id = "synthetic"
    ev.sequence = seq
    ev.drift_detected.current_agent_id = "agent_b"
    ev.drift_detected.detail = "x"
    events.append(ev)

    events_path = tmp_path / "events.jsonl"
    _write_events_jsonl(events_path, events)

    inp = DetectorInput(
        losses=[_loss(run_id="r0")],
        entries={"e1": _single_turn_entry()},
        events_paths={"e1": events_path},
    )
    patterns = detect_hot_agents(inp)
    assert len(patterns) == 1
    assert patterns[0].kind == "hot_agent"
    assert patterns[0].detail["agent_name"] == "agent_a"
    assert patterns[0].detail["drift_count"] == "10"


def test_detect_hot_agents_returns_empty_without_events() -> None:
    inp = DetectorInput(
        losses=[_loss(run_id="r0")],
        entries={"e1": _single_turn_entry()},
        events_paths={},
    )
    assert detect_hot_agents(inp) == []


# ---------------------------------------------------------------------------
# Pattern.id determinism
# ---------------------------------------------------------------------------


def test_pattern_ids_are_deterministic() -> None:
    losses = [
        _loss(
            run_id=f"r{i}",
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=1),),
        )
        for i in range(3)
    ] + [_loss(run_id=f"r{i}") for i in range(3, 10)]
    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})
    first = detect_drift_kind_frequency(inp)
    second = detect_drift_kind_frequency(inp)
    assert [p.id for p in first] == [p.id for p in second]


def test_pattern_id_changes_when_affected_ids_change() -> None:
    id_a = _pattern_id("hot_task", "summary", ("e1", "t1"))
    id_b = _pattern_id("hot_task", "summary", ("e2", "t1"))
    assert id_a != id_b


# ---------------------------------------------------------------------------
# detect_patterns aggregator
# ---------------------------------------------------------------------------


def test_detect_patterns_end_to_end_mixed_batch() -> None:
    # Mixed batch: drift-kind, plan-revision, memory-failure all fire.
    # NB. drift-kind frequency is over the whole window (24 losses below),
    # so we need enough OFF_TOPIC hits to clear 20% across that window —
    # not just within the 10-loss subset that carries the drift counts.
    losses: list[LossProfile] = []
    # Drift-kind: 6/10 OFF_TOPIC on e1 -> 6/24 = 25% across window.
    for i in range(10):
        if i < 6:
            dc = (DriftCount(kind="off_topic", severity="warning", count=1),)
        else:
            dc = ()
        losses.append(_loss(run_id=f"d{i}", entry_id="e1", drift_counts=dc))
    # Plan revisions: 9 with 1, 1 with 6.
    for i in range(9):
        losses.append(_loss(run_id=f"p{i}", entry_id="e2", plan_revisions=1))
    losses.append(_loss(run_id="p9", entry_id="e2", plan_revisions=6))
    # Multi-turn memory failure on e3: 3/5 positive.
    for i in range(5):
        losses.append(
            _loss(
                run_id=f"m{i}",
                entry_id="e3",
                memory_failure_count=1 if i < 3 else 0,
                turns_completed=3,
            )
        )

    inp = DetectorInput(
        losses=losses,
        entries={
            "e1": _single_turn_entry("e1"),
            "e2": _single_turn_entry("e2"),
            "e3": _single_turn_entry("e3"),
        },
        events_paths={},
    )

    patterns = detect_patterns(inp)
    kinds = {p.kind for p in patterns}
    assert "drift_kind_frequency" in kinds
    assert "plan_revision_instability" in kinds
    assert "multi_turn_memory_failure" in kinds
    # Ids are unique even after dedup.
    assert len({p.id for p in patterns}) == len(patterns)


def test_detect_patterns_dedupes_by_id() -> None:
    # Register a duplicate detector twice; detect_patterns should keep
    # only the first occurrence of each id.
    losses = [
        _loss(
            run_id=f"r{i}",
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=1),),
        )
        for i in range(3)
    ] + [_loss(run_id=f"r{i}") for i in range(3, 10)]
    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})

    detectors = (detect_drift_kind_frequency, detect_drift_kind_frequency)
    patterns = detect_patterns(inp, detectors=detectors)
    assert len(patterns) == 1
    assert isinstance(patterns[0], Pattern)


def test_detect_patterns_preserves_detector_order() -> None:
    # Two detectors, two distinct patterns -> output order matches
    # detector tuple order.
    losses = [
        _loss(
            run_id=f"r{i}",
            entry_id="e1",
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=1),),
            plan_revisions=1,
        )
        for i in range(9)
    ] + [_loss(run_id="r9", entry_id="e1", plan_revisions=6)]

    inp = DetectorInput(losses=losses, entries={"e1": _single_turn_entry()}, events_paths={})
    patterns = detect_patterns(
        inp,
        detectors=(detect_plan_revision_instability, detect_drift_kind_frequency),
    )
    assert [p.kind for p in patterns] == [
        "plan_revision_instability",
        "drift_kind_frequency",
    ]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_register_and_retrieve() -> None:
    def my_detector(inp: DetectorInput) -> list[Pattern]:
        return []

    before = get_all_detectors()
    register_detector(my_detector)
    after = get_all_detectors()
    assert my_detector in after
    # Idempotent re-registration should not duplicate.
    register_detector(my_detector)
    assert after.count(my_detector) == 1
    # Append-only: every previously-registered detector remains.
    for fn in before:
        assert fn in after


def test_registered_detector_runs_via_detect_patterns() -> None:
    sentinel_summary = "registered-detector-sentinel"

    def sentinel(inp: DetectorInput) -> list[Pattern]:
        if not inp.losses:
            return []
        return [
            Pattern(
                id="sentinel-id",
                kind="sentinel",
                summary=sentinel_summary,
                detail={},
            )
        ]

    register_detector(sentinel)
    inp = DetectorInput(
        losses=[_loss(run_id="r0")],
        entries={"e1": _single_turn_entry()},
        events_paths={},
    )
    detectors = ALL_DETECTORS + get_all_detectors()
    patterns = detect_patterns(inp, detectors=detectors)
    summaries = [p.summary for p in patterns]
    assert sentinel_summary in summaries
