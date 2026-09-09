"""Physical slots and recorded coordinates agree across measurement consumers."""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from zicato.core import ScoringWeights
from zicato.core.measurement import TOURNAMENT_DRAW, MeasurementDraw, MeasurementPurpose
from zicato.core.workspace import run_id_for_unit
from zicato.index.ingest import _ingest_run_into
from zicato.index.schema import apply_schema
from zicato.query.paths import WorkspacePaths
from zicato.query.replicate_scores import cell_replicate_draws, measurement_band_draws_indexed
from zicato.telemetry.reducer import write_loss_profile
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament.scoring import aggregate_generation_score, write_gen_score
from zicato.tournament.unit_cache import _resolve_cached_unit, own_code_board_draws
from zicato.workspace import WorkspaceLayout, read_loss


def _profile(*, seed=17, index=TOURNAMENT_DRAW):
    return make_loss_profile(
        epoch_id="e0",
        generation_id="v0",
        entry_id="entry",
        run_id=run_id_for_unit("v0", "entry", index, base_seed=seed, epoch_id="e0"),
        measurement=replace(index, base_seed=seed),
        execution_started=True,
        score=0.8,
    )


def _cached(root, *, seed=17, index=TOURNAMENT_DRAW):
    return _resolve_cached_unit(
        workspace_root=root,
        epoch_id="e0",
        generation_id="v0",
        entry_id="entry",
        measurement=index,
        base_seed=seed,
    )


@pytest.mark.parametrize(
    "index,alias",
    [
        (MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0), "loss.tournament.r00.json"),
        (MeasurementDraw(MeasurementPurpose.TOURNAMENT, 1), "loss.tournament.r01.json"),
    ],
)
def test_filename_alias_cannot_replace_the_canonical_draw(tmp_path: Path, index, alias) -> None:
    layout = WorkspaceLayout.from_root(tmp_path)
    profile = _profile(index=index)
    canonical = layout.loss("e0", "v0", "entry", index, base_seed=17)
    write_loss_profile(profile, canonical)
    alias_path = canonical.with_name(alias)
    write_loss_profile(replace(profile, score=0.2), alias_path)
    raw_alias = alias_path.read_bytes()
    write_gen_score(tmp_path, "e0", "v0", aggregate_generation_score([profile], ScoringWeights()))

    cached = _cached(tmp_path, index=index)
    assert cached is not None and cached.score == 0.8
    draws = cell_replicate_draws(WorkspacePaths(tmp_path), "e0", "v0", "entry")
    assert [draw.score for draw in draws] == [0.8]
    audit = measurement_band_draws_indexed(WorkspacePaths(tmp_path), "e0", "v0", "entry")
    assert audit == []
    assert own_code_board_draws(layout.run_dir("e0", "v0", "entry")) == [(index, canonical)]
    with sqlite3.connect(":memory:") as connection:
        apply_schema(connection)
        assert _ingest_run_into(connection, tmp_path, "e0", "v0", "entry")
        assert connection.execute("SELECT run_id FROM runs").fetchall() == [(profile.run_id,)]
    assert alias_path.read_bytes() == raw_alias


@pytest.mark.parametrize("seed", [None, 17])
def test_known_seed_requires_the_same_runtime_identity_everywhere(tmp_path: Path, seed) -> None:
    layout = WorkspaceLayout.from_root(tmp_path)
    path = layout.loss("e0", "v0", "entry", base_seed=seed)
    write_loss_profile(replace(_profile(seed=seed), run_id="unrelated-run"), path)
    raw = path.read_bytes()
    assert _cached(tmp_path, seed=seed) is None
    assert not own_code_board_draws(layout.run_dir("e0", "v0", "entry"))
    assert not cell_replicate_draws(WorkspacePaths(tmp_path), "e0", "v0", "entry")
    assert read_loss(layout, "e0", "v0", "entry", base_seed=seed) is None
    with sqlite3.connect(":memory:") as connection:
        apply_schema(connection)
        with pytest.raises(ValueError, match="identity|coordinates"):
            _ingest_run_into(connection, tmp_path, "e0", "v0", "entry")
        assert connection.execute("SELECT count(*) FROM runs").fetchone() == (0,)
    assert path.read_bytes() == raw
