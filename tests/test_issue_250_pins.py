"""Unit-level regression pins for replicate-keyed run artifacts (#250)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import zicato.query.events_index as events_index_module
from zicato.core import (
    BoardEntry,
    events_jsonl_path,
    run_id_for_unit,
)
from zicato.core.measurement import MeasurementDraw, MeasurementPurpose
from zicato.query.events_index import find_run_events_path, resolve_transcript_events
from zicato.query.paths import WorkspacePaths
from zicato.query.run_log import locate_events_file
from zicato.telemetry.sink import events_prev_path_for
from zicato.tournament.unit_cache import unit_events_path
from zicato.tournament.worker_transport import _run_id_for, _stamp_measurement
from zicato.workspace import WorkspaceLayout, events_measurement, is_events_file

EPOCH = "2026-08-18_alpha"
GEN = "v3"
ENTRY = "conv_summary"


def _entry(entry_id: str = ENTRY) -> BoardEntry:
    return BoardEntry(id=entry_id, kind="single_turn", wall_clock_budget_seconds=60, input="x")


def test_replicate_run_id_passes_the_shared_id_guard() -> None:
    """Every id the supervisor and dashboard police must survive their guard.

    ``_is_safe_id`` mirrors the Rust ``routes::is_safe_id``; the reserved
    ``r{n}.`` prefix must not push a realistic id past either.
    """
    from zicato.dashboard.endpoints import _is_safe_id

    for replicate in (0, 1, 9, 999):
        assert _is_safe_id(
            run_id_for_unit(
                GEN,
                ENTRY,
                MeasurementDraw(MeasurementPurpose.TOURNAMENT, replicate),
                epoch_id=EPOCH,
            )
        )
    long_entry = "transformers_progressive_scripted" * 5  # 165 chars
    assert _is_safe_id(
        run_id_for_unit(
            "v1234", long_entry, MeasurementDraw(MeasurementPurpose.TOURNAMENT, 999), epoch_id=EPOCH
        )
    )


def test_legacy_suffix_entry_remains_valid_and_cannot_collide() -> None:
    legacy = _entry(f"{ENTRY}--r1")
    legacy.validate()
    assert run_id_for_unit(GEN, legacy.id, epoch_id=EPOCH) != run_id_for_unit(
        GEN, ENTRY, MeasurementDraw(MeasurementPurpose.TOURNAMENT, 1), epoch_id=EPOCH
    )


def test_events_and_archives_are_replicate_keyed(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path)
    r0 = events_jsonl_path(tmp_path, EPOCH, GEN, ENTRY)
    r1 = events_jsonl_path(
        tmp_path, EPOCH, GEN, ENTRY, MeasurementDraw(MeasurementPurpose.TOURNAMENT, 1)
    )
    assert (r0.name, r1.name) == ("events.tournament.r0.jsonl", "events.tournament.r1.jsonl")
    assert events_prev_path_for(r0) == layout.events_prev(EPOCH, GEN, ENTRY)
    assert events_prev_path_for(r1) == layout.events_prev(
        EPOCH, GEN, ENTRY, MeasurementDraw(MeasurementPurpose.TOURNAMENT, 1)
    )
    assert unit_events_path(r1.with_name("loss.tournament.r1.json")) == r1
    assert (events_measurement(r0).draw, events_measurement(r1).draw) == (0, 1)
    assert is_events_file(r1) and not is_events_file(events_prev_path_for(r1))


def test_parent_run_id_producer_uses_the_canonical_helper() -> None:
    class _Gen:
        id = GEN
        epoch_id = EPOCH

    stamped = _stamp_measurement([_entry()], MeasurementDraw(MeasurementPurpose.TOURNAMENT, 2))[0]
    assert _run_id_for(_Gen(), stamped) == run_id_for_unit(
        GEN, ENTRY, MeasurementDraw(MeasurementPurpose.TOURNAMENT, 2), epoch_id=EPOCH
    )  # type: ignore[arg-type]


def test_replicate_events_resolve_real_event_and_runtime_identities(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path)
    run = WorkspaceLayout(tmp_path).events(EPOCH, GEN, ENTRY).parent
    run.mkdir(parents=True)
    r0, r1 = run / "events.tournament.r0.jsonl", run / "events.tournament.r1.jsonl"
    r0.write_text(json.dumps({"runId": "goldfive-r0"}) + "\n")
    r1.write_text(json.dumps({"runId": "goldfive-r1"}) + "\n")
    os.utime(r0, ns=(1_000_000_000, 1_000_000_000))
    os.utime(r1, ns=(2_000_000_000, 2_000_000_000))
    assert find_run_events_path(paths, "goldfive-r1") == r1
    assert (
        resolve_transcript_events(
            paths,
            EPOCH,
            GEN,
            ENTRY,
            run_id=run_id_for_unit(
                GEN, ENTRY, MeasurementDraw(MeasurementPurpose.TOURNAMENT, 1), epoch_id=EPOCH
            ),
        )
        == r1
    )
    assert resolve_transcript_events(paths, EPOCH, GEN, ENTRY, run_id="goldfive-r1") == r1
    assert locate_events_file(paths) == r1


def test_run_id_index_does_not_reparse_identified_files_on_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live appends retain per-file ids; only newly discovered files are read."""
    paths = WorkspacePaths(tmp_path)
    run = WorkspaceLayout(tmp_path).events(EPOCH, GEN, ENTRY).parent
    run.mkdir(parents=True)
    r0, r1 = run / "events.tournament.r0.jsonl", run / "events.tournament.r1.jsonl"
    r0.write_text(json.dumps({"runId": "goldfive-r0"}) + "\n")
    r1.write_text(json.dumps({"runId": "goldfive-r1"}) + "\n")

    original = events_index_module._run_id_of_events_file
    opened: list[Path] = []
    scans: list[Path] = []

    def counted(path: Path) -> str | None:
        opened.append(path)
        return original(path)

    original_scan = events_index_module._current_events_files

    def counted_scan(epochs: Path) -> list[Path]:
        scans.append(epochs)
        return original_scan(epochs)

    monkeypatch.setattr(events_index_module, "_run_id_of_events_file", counted)
    monkeypatch.setattr(events_index_module, "_current_events_files", counted_scan)
    assert find_run_events_path(paths, "goldfive-r0") == r0
    assert opened == [r0, r1]
    assert scans == [paths.epochs]

    opened.clear()
    scans.clear()
    with r0.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"runId": "goldfive-r0", "sequence": 1}) + "\n")
    assert find_run_events_path(paths, "goldfive-r0") == r0
    assert opened == []
    assert scans == []

    opened.clear()
    r2 = run / "events.tournament.r2.jsonl"
    r2.write_text(json.dumps({"runId": "goldfive-r2"}) + "\n")
    assert find_run_events_path(paths, "goldfive-r2") == r2
    assert opened == [r2]

    # Replacement must invalidate the retained id even when the path is the
    # same; production archives the old inode before opening a fresh sink.
    opened.clear()
    r1.replace(run / "events.r1.prev.jsonl")
    r1.write_text(json.dumps({"runId": "goldfive-r1-new"}) + "\n")
    assert find_run_events_path(paths, "goldfive-r1-new") == r1
    assert opened == [r1]

    # An empty live file is unresolved, so its first append is reparsed once.
    opened.clear()
    r3 = run / "events.tournament.r3.jsonl"
    r3.touch()
    assert find_run_events_path(paths, "not-yet-written") is None
    assert opened == [r3]
    opened.clear()
    r3.write_text(json.dumps({"runId": "goldfive-r3"}) + "\n")
    assert find_run_events_path(paths, "goldfive-r3") == r3
    assert opened == [r3]


def test_sse_reports_replicate_events_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zicato.dashboard.sse import ChangeBroker

    broker = ChangeBroker(WorkspacePaths(tmp_path))
    reported: list[Path] = []
    monkeypatch.setattr(broker, "_schedule_state_change", lambda _kind: None)
    monkeypatch.setattr(broker, "_report_events_growth", reported.append)
    path = tmp_path / "events.tournament.r3.jsonl"
    broker._on_path_changed(str(path))
    assert reported == [path]
