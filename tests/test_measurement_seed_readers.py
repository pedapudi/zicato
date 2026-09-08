"""Seed-qualified measurements remain distinct through descriptive readers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests._workspace_support import write_tournament
from zicato.core import ScoringWeights
from zicato.core.measurement import (
    UNKNOWN_SEED,
    BaseSeed,
    MeasurementDraw,
    measurement_artifact_path,
)
from zicato.core.workspace import run_id_for_unit
from zicato.query.events_index import find_run_events_path, resolve_transcript_events
from zicato.query.execution_plan import _unit_nodes
from zicato.query.judge_view import resolve_run_id_for_entry
from zicato.query.paths import WorkspacePaths
from zicato.query.replicate_scores import cell_replicate_draws, cell_replicate_draws_indexed
from zicato.reflection.adjudicator import run_ref_for
from zicato.reflection.corpus import ingest_lineage, read_corpus, write_corpus
from zicato.telemetry.reducer import write_loss_profile
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament.scoring import write_gen_score
from zicato.workspace import WorkspaceLayout, read_loss


def _write(root: Path, seed: BaseSeed, index: int = 0) -> Path:
    run_dir = WorkspaceLayout.from_root(root).run_dir("e0", "v0", "entry")
    measurement = MeasurementDraw.from_index(index, base_seed=seed)
    path = measurement_artifact_path(run_dir, "loss", index, base_seed=seed)
    run_id = run_id_for_unit("v0", "entry", index, base_seed=seed)
    write_loss_profile(
        make_loss_profile(
            run_id=run_id,
            epoch_id="e0",
            generation_id="v0",
            entry_id="entry",
            measurement=measurement,
            execution_started=True,
        ),
        path,
    )
    events = measurement_artifact_path(run_dir, "events", index, base_seed=seed)
    events.write_text(json.dumps({"runId": run_id}) + "\n")
    return path


def test_seed_variants_have_distinct_query_nodes_and_reflection_decisions(tmp_path: Path) -> None:
    seeds = [UNKNOWN_SEED, None, 17, 23]
    for seed in seeds:
        _write(tmp_path, seed)
    paths = WorkspacePaths(tmp_path)
    draws = cell_replicate_draws_indexed(paths, "e0", "v0", "entry")
    assert {profile.measurement.base_seed for _, profile in draws} == set(seeds)
    nodes = _unit_nodes(paths, "e0", "v0", "entry", "sweep")
    assert len({node.id for node in nodes}) == 4
    corpus = ingest_lineage(
        workspace_root=tmp_path,
        epoch_id="e0",
        reflection_id="reflection",
        candidates=["v0"],
        entries=["entry"],
        weights=ScoringWeights(),
    )
    refs = {run_ref_for(obs) for obs in corpus}
    assert refs == {
        "v0:entry:r0",
        "seed-none:v0:entry:r0",
        "seed-17:v0:entry:r0",
        "seed-23:v0:entry:r0",
    }
    write_corpus(tmp_path, "e0", "reflection", corpus)
    assert {run_ref_for(obs) for obs in read_corpus(tmp_path, "e0", "reflection")} == refs


def test_exact_seed_resolution_survives_incomplete_alternate_publication(tmp_path: Path) -> None:
    historical = _write(tmp_path, UNKNOWN_SEED)
    selected = _write(tmp_path, 17)
    paths = WorkspacePaths(tmp_path)
    selected_id = run_id_for_unit("v0", "entry", base_seed=17)
    expected_events = selected.with_name("events.jsonl")
    assert (
        resolve_transcript_events(paths, "e0", "v0", "entry", run_id=selected_id) == expected_events
    )
    assert find_run_events_path(paths, selected_id) == expected_events
    assert resolve_run_id_for_entry(paths, "e0", "v0", "entry", run_id=selected_id) == selected_id
    incomplete = _write(tmp_path, 23)
    incomplete.unlink()
    missing_id = run_id_for_unit("v0", "entry", base_seed=None)
    assert resolve_transcript_events(paths, "e0", "v0", "entry", run_id=missing_id) is None
    draws = cell_replicate_draws_indexed(paths, "e0", "v0", "entry")
    assert {profile.run_id for _, profile in draws} == {selected_id, "v0--entry"}
    assert historical.exists()
    assert (
        read_loss(WorkspaceLayout.from_root(tmp_path), "e0", "v0", "entry", base_seed=None) is None
    )
    assert (
        read_loss(WorkspaceLayout.from_root(tmp_path), "e0", "v0", "entry", base_seed=17)["run_id"]
        == selected_id
    )


def test_index_audits_all_seeds_and_projects_only_selected_seed(tmp_path: Path) -> None:
    from zicato.index.ingest import ensure_index, ingest_run
    from zicato.index.query import loss_profiles_for_generation, runs_for_generation

    for seed in (UNKNOWN_SEED, None, 17, 23):
        _write(tmp_path, seed)
    layout = WorkspaceLayout.from_root(tmp_path)
    score_path = layout.gen_score("e0", "v0")
    write_gen_score(tmp_path, "e0", "v0", {"base_seed": 17, "generation_id": "v0", "scalar": 0.0})
    database = tmp_path / "index.db"
    ingest_run(tmp_path, database, "e0", "v0", "entry")
    assert len(runs_for_generation(database, "e0", "v0")) == 4
    selected_id = run_id_for_unit("v0", "entry", base_seed=17)
    assert [row["run_id"] for row in loss_profiles_for_generation(database, "e0", "v0")] == [
        selected_id
    ]
    paths = WorkspacePaths(tmp_path)
    assert [profile.run_id for profile in cell_replicate_draws(paths, "e0", "v0", "entry")] == [
        selected_id
    ]

    # The same draw count with a different selected seed replaces the aggregate row.
    write_gen_score(tmp_path, "e0", "v0", {"base_seed": None, "generation_id": "v0", "scalar": 0.0})
    ingest_run(tmp_path, database, "e0", "v0", "entry")
    assert [row["run_id"] for row in loss_profiles_for_generation(database, "e0", "v0")] == [
        run_id_for_unit("v0", "entry", base_seed=None)
    ]
    assert len(runs_for_generation(database, "e0", "v0")) == 4

    actions: list[str] = []
    ensure_index(tmp_path, database, action_out=actions)
    assert actions == ["present"]
    actions.clear()
    ensure_index(tmp_path, database, action_out=actions)
    assert actions == ["present"]

    for invalid in ({"base_seed": 17}, {"base_seed": 17, "generation_id": "v2"}):
        score_path.write_text(json.dumps({"format_version": 1, "scalar": 0.0, **invalid}))
        ingest_run(tmp_path, database, "e0", "v0", "entry")
        assert loss_profiles_for_generation(database, "e0", "v0") == []
        assert cell_replicate_draws(paths, "e0", "v0", "entry") == []
        assert len(runs_for_generation(database, "e0", "v0")) == 4


@pytest.mark.parametrize("conflict", ["base_seed", "run_id"])
def test_index_refuses_conflicting_seed_record_without_replacing_projection(
    tmp_path: Path, conflict: str
) -> None:
    from tests._runtime_builders import seed_promoted_lineage
    from zicato.index.ingest import rebuild_index

    path = _write(tmp_path, 17)
    seed_promoted_lineage(tmp_path, "e0")
    database = rebuild_index(tmp_path)
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT run_id FROM runs").fetchall()
    assert before == [(run_id_for_unit("v0", "entry", base_seed=17),)]
    raw = json.loads(path.read_text())
    if conflict == "base_seed":
        raw["measurement"]["base_seed"] = 23
    else:
        raw["run_id"] = run_id_for_unit("v0", "entry", base_seed=23)
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="conflicts"):
        rebuild_index(tmp_path)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT run_id FROM runs").fetchall() == before


def test_interrupted_replacement_keeps_archive_visible_without_an_extra_draw(
    tmp_path: Path,
) -> None:
    from zicato.tournament.artifacts import archive_unit_artifacts
    from zicato.workspace.reads import read_events_history

    _write(tmp_path, UNKNOWN_SEED)
    selected = _write(tmp_path, 17)
    archived = archive_unit_artifacts(selected)
    assert archived is not None and not selected.exists()
    pending = selected.parent / "attempts" / ".pending-copy"
    pending.mkdir()
    (pending / "loss.json").write_text("{")
    layout = WorkspaceLayout.from_root(tmp_path)
    history = read_events_history(layout, "e0", "v0", "entry", base_seed=17)
    assert history == [[{"runId": run_id_for_unit("v0", "entry", base_seed=17)}]]
    paths = WorkspacePaths(tmp_path)
    assert len(cell_replicate_draws_indexed(paths, "e0", "v0", "entry")) == 1

    _write(tmp_path, 17)
    nodes = _unit_nodes(paths, "e0", "v0", "entry", "sweep")
    assert len(nodes) == 2
    seeded = next(node for node in nodes if node.coordinates.get("base_seed") == 17)
    assert len(seeded.children) == 1
    assert seeded.children[0].kind == "board_entry_attempt"
    assert len(cell_replicate_draws_indexed(paths, "e0", "v0", "entry")) == 2


def test_duplicate_physical_spelling_never_adds_an_independent_draw(tmp_path: Path) -> None:
    from zicato.index.ingest import ingest_run

    path = _write(tmp_path, 17)
    path.with_name("loss.r0.json").write_bytes(path.read_bytes())
    paths = WorkspacePaths(tmp_path)
    assert len(cell_replicate_draws_indexed(paths, "e0", "v0", "entry")) == 1
    corpus = ingest_lineage(
        workspace_root=tmp_path,
        epoch_id="e0",
        reflection_id="reflection",
        candidates=["v0"],
        entries=["entry"],
        weights=ScoringWeights(),
    )
    assert len(corpus) == 1
    ingest_run(tmp_path, None, "e0", "v0", "entry")
    from zicato.query.replicate_scores import measurement_band_draws_indexed

    audit = measurement_band_draws_indexed(paths, "e0", "v0", "entry")
    assert len(audit) == 1 and audit[0][1].key == "ambiguous"
    assert path.with_name("loss.r0.json").read_bytes() == path.read_bytes()


def test_wrong_coordinates_remain_auditable_without_becoming_cell_evidence(tmp_path: Path) -> None:
    from zicato.query.replicate_scores import measurement_band_draws_indexed

    path = _write(tmp_path, 17)
    raw = json.loads(path.read_text())
    raw["entry_id"] = "different-entry"
    path.write_text(json.dumps(raw))
    paths = WorkspacePaths(tmp_path)
    assert not cell_replicate_draws_indexed(paths, "e0", "v0", "entry")
    audit = measurement_band_draws_indexed(paths, "e0", "v0", "entry")
    assert len(audit) == 1 and audit[0][1].key == "ambiguous"
    assert read_loss(WorkspaceLayout.from_root(tmp_path), "e0", "v0", "entry", base_seed=17) is None


@pytest.mark.parametrize("seed", [None, 17])
def test_selected_conversation_uses_its_seed(tmp_path: Path, seed: int | None) -> None:
    from zicato.query.conversations_view import build_matchup_conversations
    from zicato.query.events_index import find_generation_entry_events, find_generation_run

    _write(tmp_path, UNKNOWN_SEED)
    selected = _write(tmp_path, seed)
    _write(tmp_path, 29)
    write_gen_score(tmp_path, "e0", "v0", {"generation_id": "v0", "base_seed": seed, "scalar": 0.0})
    write_tournament(tmp_path, {"parent_generation_id": "v0", "entries": []})
    paths = WorkspacePaths(tmp_path)
    run_id = run_id_for_unit("v0", "entry", base_seed=seed)
    assert find_generation_run(paths, "v0", "entry") == (run_id, selected.with_name("events.jsonl"))
    assert find_generation_entry_events(paths, "v0", "entry") == selected.with_name("events.jsonl")
    assert build_matchup_conversations(paths, "entry")["champion"]["run_id"] == run_id
    from zicato.query.transcript_view import resolve_conversation

    assert resolve_transcript_events(paths, "e0", "v0", "entry") == selected.with_name(
        "events.jsonl"
    )
    assert resolve_conversation(
        paths, run_id, gen="v0", entry="entry", epoch="e0"
    ) == selected.with_name("events.jsonl")


def test_missing_exact_seed_has_no_outer_historical_fallback(tmp_path: Path) -> None:
    from zicato.query.transcript_view import resolve_conversation

    _write(tmp_path, UNKNOWN_SEED)
    _write(tmp_path, 17)
    missing = run_id_for_unit("v0", "entry", base_seed=29)
    assert (
        resolve_conversation(WorkspacePaths(tmp_path), missing, gen="v0", entry="entry", epoch="e0")
        is None
    )


@pytest.mark.parametrize(
    "selection",
    [{"base_seed": 29}, {"generation_id": "other", "base_seed": 17}, {"base_seed": True}],
)
def test_missing_or_invalid_selected_conversation_cannot_borrow_history(
    tmp_path: Path, selection: dict
) -> None:
    from zicato.query.events_index import find_generation_entry_events, find_generation_run

    _write(tmp_path, UNKNOWN_SEED)
    _write(tmp_path, 17)
    layout = WorkspaceLayout.from_root(tmp_path)
    layout.gen_score("e0", "v0").write_text(json.dumps({"generation_id": "v0", **selection}))
    paths = WorkspacePaths(tmp_path)
    assert find_generation_run(paths, "v0", "entry") is None
    assert find_generation_entry_events(paths, "v0", "entry") is None
    assert find_generation_run(paths, "v0", "absent") is None


def test_live_conversation_before_score_publication_requires_exact_identity(tmp_path: Path) -> None:
    from zicato.query.events_index import find_generation_run
    from zicato.query.transcript_view import resolve_conversation

    selected = _write(tmp_path, 17)
    selected.unlink()
    paths = WorkspacePaths(tmp_path)
    assert find_generation_run(paths, "v0", "entry") is None
    assert resolve_conversation(
        paths, run_id_for_unit("v0", "entry", base_seed=17), gen="v0", entry="entry", epoch="e0"
    ) == selected.with_name("events.jsonl")
