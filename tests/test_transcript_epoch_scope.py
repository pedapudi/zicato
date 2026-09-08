"""Transcript coordinates confine reads even when generations repeat across epochs."""

import json
from pathlib import Path

import pytest

from zicato.core.workspace import run_id_for_unit
from zicato.query.events_index import find_run_events_path, resolve_transcript_events
from zicato.query.paths import WorkspacePaths
from zicato.query.transcript_view import (
    build_run_transcript,
    build_run_transcript_delta,
    resolve_conversation,
)
from zicato.workspace import WorkspaceLayout


def _events(root: Path, epoch: str, identity: str) -> tuple[str, Path]:
    layout = WorkspaceLayout.from_root(root)
    if identity == "seeded":
        run_id = run_id_for_unit("v0", "entry", base_seed=17)
        path = layout.events(epoch, "v0", "entry", base_seed=17)
    else:
        run_id = "recorded-run"
        path = layout.events(epoch, "v0", "entry")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runId": run_id}) + "\n")
    return run_id, path


@pytest.mark.parametrize("identity", ["seeded", "opaque"])
@pytest.mark.parametrize("entry_directory_exists", [False, True])
def test_requested_epoch_cannot_borrow_another_epochs_transcript(
    tmp_path: Path, identity: str, entry_directory_exists: bool
) -> None:
    run_id, _ = _events(tmp_path, "other", identity)
    layout = WorkspaceLayout.from_root(tmp_path)
    layout.epoch_dir("requested").mkdir()
    if entry_directory_exists:
        layout.run_dir("requested", "v0", "entry").mkdir(parents=True)
    paths = WorkspacePaths(tmp_path)

    assert resolve_transcript_events(paths, "requested", "v0", "entry", run_id=run_id) is None
    assert resolve_conversation(paths, run_id, epoch="requested", gen="v0", entry="entry") is None
    assert build_run_transcript(paths, "requested", "v0", "entry", run_id=run_id)["turns"] == []
    assert (
        build_run_transcript_delta(paths, "requested", "v0", "entry", run_id=run_id)["turns"] == []
    )


@pytest.mark.parametrize("identity", ["seeded", "opaque"])
def test_requested_epoch_selects_its_own_repeated_generation(tmp_path: Path, identity: str) -> None:
    run_id, other = _events(tmp_path, "earlier", identity)
    _, expected = _events(tmp_path, "requested", identity)
    assert other != expected
    paths = WorkspacePaths(tmp_path)

    assert resolve_transcript_events(paths, "requested", "v0", "entry", run_id=run_id) == expected
    assert (
        resolve_conversation(paths, run_id, epoch="requested", gen="v0", entry="entry") == expected
    )


@pytest.mark.parametrize("identity", ["seeded", "opaque"])
def test_omitted_epoch_retains_transcript_discovery(tmp_path: Path, identity: str) -> None:
    run_id, expected = _events(tmp_path, "available", identity)
    paths = WorkspacePaths(tmp_path)

    assert resolve_transcript_events(paths, "", "v0", "entry", run_id=run_id) == expected
    assert resolve_conversation(paths, run_id, gen="v0", entry="entry") == expected
    assert resolve_conversation(paths, run_id) == expected


def test_match_disambiguator_cannot_change_requested_epoch(tmp_path: Path) -> None:
    run = WorkspaceLayout.from_root(tmp_path).run_dir("available", "v0", "entry")
    events = run / "seed-none" / "events.r1.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text('{"runId":"rung-run"}\n')
    events.with_name("loss.r1.json").write_text(json.dumps({"match_id": "rung0"}))
    paths = WorkspacePaths(tmp_path)

    assert resolve_transcript_events(paths, "missing", "v0", "entry", match_id="rung0") is None
    assert resolve_transcript_events(paths, "available", "v0", "entry", match_id="rung0") == events


@pytest.mark.parametrize("identity", ["seeded", "opaque"])
def test_epoch_only_lookup_separates_identical_run_ids(tmp_path: Path, identity: str) -> None:
    run_id, earlier = _events(tmp_path, "earlier", identity)
    _, requested = _events(tmp_path, "requested", identity)
    paths = WorkspacePaths(tmp_path)

    assert resolve_conversation(paths, run_id) == earlier
    assert resolve_conversation(paths, run_id, epoch="requested") == requested
    assert resolve_conversation(paths, run_id, epoch="missing") is None
    assert resolve_conversation(paths, run_id, epoch="earlier") == earlier
    assert find_run_events_path(paths, run_id) == earlier


@pytest.mark.parametrize("source", ["active", "events"])
def test_epoch_filter_applies_to_run_id_lookup(tmp_path: Path, source: str) -> None:
    run_id = "recorded-run"
    layout = WorkspaceLayout.from_root(tmp_path)
    events = layout.events("available", "v0", "entry")
    events.parent.mkdir(parents=True)
    events.write_text(json.dumps({"runId": run_id}) + "\n")
    if source == "active":
        layout.active_runs_dir.mkdir(parents=True)
        (layout.active_runs_dir / f"{run_id}.json").write_text(
            json.dumps({"events_jsonl_path": str(events)})
        )
    paths = WorkspacePaths(tmp_path)

    assert resolve_conversation(paths, run_id) == events
    assert resolve_conversation(paths, run_id, epoch="missing") is None
    assert resolve_conversation(paths, run_id, epoch="available") == events
