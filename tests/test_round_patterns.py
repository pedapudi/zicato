"""The per-round pattern record: its codec and the round that writes it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._orchestrator_harness import (
    bootstrap_workspace,
    evaluation_call_llm,
    install_stub_adapter_factory,
    install_telemetry_stubs,
    run_evolve_once,
)
from zicato.core.patterns import Pattern
from zicato.epoch._storage import RecordError
from zicato.epoch.round_patterns import (
    RoundPatterns,
    pattern_from_dict,
    read_round_patterns,
    write_round_patterns,
)
from zicato.workspace import WorkspaceLayout

_PATTERN = Pattern(
    id="hot_task:e1:t1",
    kind="hot_task",
    summary="task t1 fails often",
    detail={"entry_id": "e1", "fail_or_block_rate": "0.750"},
    affected_mutation_ids=("instr",),
    severity="critical",
)


def test_record_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "rounds" / "3" / "patterns.json"
    write_round_patterns(path, parent_generation_id="v2", patterns=[_PATTERN])
    assert read_round_patterns(path) == RoundPatterns("v2", (_PATTERN,))


def test_absent_record_reads_as_none(tmp_path: Path) -> None:
    assert read_round_patterns(tmp_path / "patterns.json") is None


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        "not json",
        json.dumps({"format_version": 2, "parent_generation_id": "v0", "patterns": []}),
        json.dumps({"format_version": 1, "parent_generation_id": "", "patterns": []}),
        json.dumps({"format_version": 1, "parent_generation_id": "v0", "patterns": [{}]}),
    ],
)
def test_malformed_record_is_refused(tmp_path: Path, body: str) -> None:
    path = tmp_path / "patterns.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(RecordError):
        read_round_patterns(path)


def test_writer_refuses_a_pattern_the_reader_would_refuse(tmp_path: Path) -> None:
    path = tmp_path / "patterns.json"
    numeric_detail = Pattern(id="p", kind="k", summary="", detail={"count": 3})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="detail must map strings to strings"):
        write_round_patterns(path, parent_generation_id="v0", patterns=[numeric_detail])
    assert not path.exists()


def test_decoder_applies_pattern_defaults() -> None:
    assert pattern_from_dict({"id": "p", "kind": "k"}) == Pattern(
        id="p", kind="k", summary="", detail={}
    )


def test_round_records_the_patterns_it_passed_to_the_proposer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One evolve round stores its detector output under its own round directory."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    detected: list[Any] = []

    def fake_detect_patterns(inp: Any, detectors: Any = ()) -> list[Pattern]:
        detected.append(inp)
        return [_PATTERN]

    monkeypatch.setattr("zicato.patterns.detectors.detect_patterns", fake_detect_patterns)

    run_evolve_once(workspace, epoch_id, evaluation_call_llm)

    assert len(detected) == 1
    record = read_round_patterns(WorkspaceLayout.from_root(workspace).round_patterns(epoch_id, 0))
    assert record == RoundPatterns("v0", (_PATTERN,))
