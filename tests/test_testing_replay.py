"""Tests for :mod:`zicato.testing.replay`."""

from __future__ import annotations

from pathlib import Path

import pytest

from zicato.testing.fixtures import make_synthetic_events_jsonl
from zicato.testing.replay import events_to_dicts, replay_events


def test_replay_events_round_trip(tmp_path: Path) -> None:
    """Round-trip a synthetic events JSONL through goldfive's replay helper.

    Skipped when goldfive is not installed.
    """
    pytest.importorskip("goldfive")

    jsonl_path = tmp_path / "events.jsonl"
    make_synthetic_events_jsonl(
        jsonl_path,
        drift_events=[("off_topic", "warning"), ("looping_reasoning", "info")],
        plan_revisions=1,
        task_failures=1,
        task_starts=2,
        conversation_turns=1,
    )

    events = replay_events(jsonl_path)
    # RunStarted + 2*conversation + 2*task_starts + 1*task_failed +
    # 2*drift + 1*plan_revised + 1*RunCompleted = 1 + 1 + 2 + 1 + 2 + 1 + 1 + 1 = 10
    assert len(events) == 10

    # First event is RunStarted.
    payload = events[0].WhichOneof("payload")
    assert payload == "run_started"

    # Last event is RunCompleted (the terminal marker we appended).
    assert events[-1].WhichOneof("payload") == "run_completed"

    # At least one event carries a drift payload with off_topic kind.
    drift_payloads = [
        evt for evt in events if evt.WhichOneof("payload") == "drift_detected"
    ]
    assert len(drift_payloads) == 2

    # The two drift events preserve the (kind, severity) we asked for.
    kinds_severities = sorted(
        (int(evt.drift_detected.kind), int(evt.drift_detected.severity))
        for evt in drift_payloads
    )
    # Just assert both are non-zero (real proto enum values, not the
    # unspecified default).
    for k, s in kinds_severities:
        assert k != 0, "drift kind enum must not be DRIFT_KIND_UNSPECIFIED"
        assert s != 0, "drift severity enum must not be DRIFT_SEVERITY_UNSPECIFIED"


def test_replay_events_empty_file(tmp_path: Path) -> None:
    pytest.importorskip("goldfive")
    jsonl_path = tmp_path / "empty.jsonl"
    jsonl_path.write_text("", encoding="utf-8")
    assert replay_events(jsonl_path) == []


def test_replay_events_skips_blank_lines(tmp_path: Path) -> None:
    pytest.importorskip("goldfive")
    jsonl_path = tmp_path / "events.jsonl"
    make_synthetic_events_jsonl(
        jsonl_path,
        task_starts=1,
        conversation_turns=0,
    )

    # Inject blank lines and reread.
    content = jsonl_path.read_text(encoding="utf-8")
    jsonl_path.write_text("\n" + content + "\n\n", encoding="utf-8")
    events = replay_events(jsonl_path)
    # RunStarted + TaskStarted + RunCompleted = 3
    assert len(events) == 3


def test_events_to_dicts_proto_path(tmp_path: Path) -> None:
    """``events_to_dicts`` on real proto events uses MessageToDict."""
    pytest.importorskip("goldfive")
    jsonl_path = tmp_path / "events.jsonl"
    make_synthetic_events_jsonl(
        jsonl_path,
        drift_events=[("off_topic", "warning")],
        task_starts=1,
        conversation_turns=0,
    )

    events = replay_events(jsonl_path)
    dicts = events_to_dicts(events)

    assert len(dicts) == len(events)
    # Every dict has the envelope keys (snake_case preserved).
    for d in dicts:
        assert "event_id" in d
        assert "run_id" in d
        # exactly one payload key, identified via WhichOneof on the proto

    # The drift event's dict contains a "drift_detected" key (snake_case).
    drift_dicts = [d for d in dicts if "drift_detected" in d]
    assert len(drift_dicts) == 1
    drift = drift_dicts[0]["drift_detected"]
    # MessageToDict surfaces enums as their string names; off_topic
    # should appear in the kind field rendering.
    assert "kind" in drift


def test_events_to_dicts_dict_input() -> None:
    """``events_to_dicts`` is a no-op-ish projection for dict inputs."""
    evts = [
        {"event_id": "e1", "run_started": {"run_id": "r"}},
        {"event_id": "e2", "drift_detected": {"kind": "off_topic"}},
    ]
    out = events_to_dicts(evts)  # type: ignore[arg-type]
    assert out == evts
    # Independent copy so caller mutation doesn't bleed back.
    out[0]["mutated"] = "yes"
    assert "mutated" not in evts[0]


def test_events_to_dicts_dataclass_input() -> None:
    """``events_to_dicts`` handles dataclass-shaped events."""
    from dataclasses import dataclass

    @dataclass
    class FakeEvent:
        event_id: str
        run_id: str

    evts = [FakeEvent(event_id="e1", run_id="r1"), FakeEvent(event_id="e2", run_id="r2")]
    out = events_to_dicts(evts)  # type: ignore[arg-type]
    assert out == [
        {"event_id": "e1", "run_id": "r1"},
        {"event_id": "e2", "run_id": "r2"},
    ]


def test_events_to_dicts_to_dict_protocol() -> None:
    """``events_to_dicts`` picks up an event's ``to_dict`` method when present."""

    class FakeEvent:
        def __init__(self, payload: dict[str, str]) -> None:
            self._payload = payload

        def to_dict(self) -> dict[str, str]:
            return dict(self._payload)

    out = events_to_dicts([FakeEvent({"k": "v"})])
    assert out == [{"k": "v"}]
