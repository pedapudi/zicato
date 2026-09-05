"""Real detector findings with distinctive private identities."""

from pathlib import Path

from goldfive.sinks.persistence import JSONLPersistenceSink, _events_module

from zicato.core import DriftCount, LossProfile, MetricCount, Pattern
from zicato.patterns import ALL_DETECTORS, DetectorInput


async def private_detector_patterns(
    tmp_path: Path, *, identity: str = "private"
) -> tuple[Pattern, ...]:
    """Serialize events and produce one finding from each built-in detector."""
    pb = _events_module()
    events = []
    for index in range(3):
        task = f"{identity}-task-{index}"
        agent = f"{identity}-agent-{index}"
        events.append(pb.Event(task_started=pb.TaskStarted(task_id=task)))
        events.append(
            pb.Event(agent_invocation_started=pb.AgentInvocationStarted(agent_name=agent))
        )
        if index == 0:
            events.append(pb.Event(task_failed=pb.TaskFailed(task_id=task)))
            events.extend(
                pb.Event(drift_detected=pb.DriftDetected(current_agent_id=agent)) for _ in range(6)
            )
    for sequence, event in enumerate(events):
        event.sequence = sequence
        event.run_id = f"{identity}-event-run"
    events_path = tmp_path / f"{identity}-events.jsonl"
    sink = JSONLPersistenceSink(events_path, mode="write")
    try:
        for event in events:
            await sink.emit(event)
    finally:
        await sink.close()
    entry_id = f"{identity}-entry"
    losses = [
        LossProfile(
            run_id=f"{identity}-loss-run-{index}",
            entry_id=entry_id,
            generation_id="v0",
            epoch_id="epoch",
            drift_counts=(DriftCount(kind="off_topic", severity="warning", count=1),),
            metric_counts=(
                MetricCount(name="cost:tokens_spent", count=100),
                MetricCount(name=f"rubric:{identity}-dimension", count=1),
            ),
            plan_revisions=9 if index == 0 else 0,
            task_failure_ratio=0,
            runtime_ms=10,
            wall_clock_budget_exceeded=False,
            expectation_result=None,
            drift_loss=0,
            pass_fail=None,
            memory_failure_count=1,
            context_loss_count=2,
        )
        for index in range(3)
    ]
    inp = DetectorInput(losses=losses, entries={}, events_paths={entry_id: events_path})
    patterns = []
    for detector in ALL_DETECTORS:
        findings = detector(inp)
        assert len(findings) == 1, detector.__name__
        patterns.extend(findings)
    return tuple(patterns)
