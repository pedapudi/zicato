"""Tests for the mutation-point fertility map (track record) surfaces.

Three layers, matching how the feature ships:

* the index query (:func:`zicato.index.query.mutation_point_track_record`)
  against a hand-seeded SQLite index — per-point touch/promotion counts,
  Δscalar distribution summary, the documented last-K recency window, the
  settled-only rule, and the missing-index tolerance;
* the prompt-side annotation
  (:func:`zicato.proposer.prompts.render_mutation_track_annotation` +
  ``render_mutation_block(track_records=...)``) — banding (bucketed deltas,
  no raw floats), the honest "experiments touching this point / credit
  confounded" attribution labels, and byte-identity when no records exist;
* the read-only proposer tool (``mutation_track_record``) — registration in
  ``DEFAULT_PROPOSER_TOOLS``, output shape, manifest-scoped id validation,
  and the zeroed no-history answer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests._proposal_evidence import render_proposal_evidence
from zicato.index.query import MutationTrackRecord, mutation_point_track_record
from zicato.index.schema import apply_schema
from zicato.proposer.prompts import (
    render_mutation_block,
    render_mutation_track_annotation,
)
from zicato.proposer.tools import (
    DEFAULT_PROPOSER_TOOLS,
    ProposerToolContext,
    bind_proposer_tool_context,
    mutation_track_record,
)
from zicato.testing import make_mutation_point

_EPOCH = "e1"


def _seed_index(db_path: Path) -> None:
    """Seed a minimal index: two points, four experiments (one unsettled).

    Timeline (generation ``created_at`` ascending — the recency order):

    * ``v1`` — PROMOTED, Δ=-0.5, touches ``router__sp`` only;
    * ``v2`` — REJECTED, Δ=+0.3, touches ``router__sp`` AND ``planner__sp``
      (the multi-point / confounded-credit experiment);
    * ``v3`` — PROMOTED, Δ=-0.1, touches ``router__sp`` only;
    * ``v4`` — UNSETTLED (no decision yet), touches ``router__sp`` — must
      never be counted.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        apply_schema(conn)
        conn.execute(
            "INSERT INTO epochs(epoch_id, contract_hash, created_at, closed) "
            "VALUES(?, 'hash', '2026-01-01T00:00:00', 0)",
            (_EPOCH,),
        )
        gens = [("v1", 1), ("v2", 2), ("v3", 3), ("v4", 4)]
        for gid, hour in gens:
            conn.execute(
                "INSERT INTO generations(epoch_id, generation_id, parent_generation_id, "
                "promoted, created_at) VALUES(?, ?, 'v0', 0, ?)",
                (_EPOCH, gid, f"2026-01-01T{hour:02d}:00:00"),
            )
        experiments = [
            ("v1", "promoted", -0.5),
            ("v2", "rejected", 0.3),
            ("v3", "promoted", -0.1),
            ("v4", None, None),
        ]
        for gid, decision, delta in experiments:
            conn.execute(
                "INSERT INTO experiments(epoch_id, generation_id, hypothesis_core_idea, "
                "tournament_decision, scalar_score_delta) VALUES(?, ?, 'idea', ?, ?)",
                (_EPOCH, gid, decision, delta),
            )
        patches = [
            ("p1", "v1", "router__sp"),
            ("p2", "v2", "router__sp"),
            ("p3", "v2", "planner__sp"),
            ("p4", "v3", "router__sp"),
            # A second patch on the SAME point within one experiment must
            # count that experiment once, not twice.
            ("p5", "v3", "router__sp"),
            ("p6", "v4", "router__sp"),
        ]
        for pid, gid, mid in patches:
            conn.execute(
                "INSERT INTO patches(patch_id, epoch_id, generation_id, mutation_id, op) "
                "VALUES(?, ?, ?, ?, 'replace')",
                (pid, _EPOCH, gid, mid),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "index.db"
    _seed_index(path)
    return path


# ---------------------------------------------------------------------------
# The index query
# ---------------------------------------------------------------------------


def test_track_record_stats_settled_only(db_path: Path) -> None:
    records = mutation_point_track_record(db_path, _EPOCH)
    assert set(records) == {"router__sp", "planner__sp"}

    router = records["router__sp"]
    # v4 (unsettled) is excluded; v3's duplicate patch counts once.
    assert router.experiments_touching == 3
    assert router.promoted == 2
    assert router.confounded_experiments == 1  # v2 also touched planner__sp
    assert router.delta_min == -0.5
    assert router.delta_median == -0.1
    assert router.delta_max == 0.3

    planner = records["planner__sp"]
    assert planner.experiments_touching == 1
    assert planner.promoted == 0
    assert planner.confounded_experiments == 1
    assert planner.delta_min == planner.delta_median == planner.delta_max == 0.3


def test_track_record_recency_is_a_last_k_window(db_path: Path) -> None:
    """The documented last-K window: with K=2, only the two most recent
    SETTLED experiments (v2, v3) count as recent — v1's promotion ages out
    and the unsettled v4 never enters the ranking."""
    records = mutation_point_track_record(db_path, _EPOCH, recent_window=2)
    router = records["router__sp"]
    assert router.recent_touching == 2  # v2 + v3
    assert router.recent_promoted == 1  # v3 only — v1 is outside the window
    planner = records["planner__sp"]
    assert planner.recent_touching == 1  # v2

    # A window at least as large as the settled history marks everything
    # recent (the default window covers this 3-experiment epoch).
    wide = mutation_point_track_record(db_path, _EPOCH)
    assert wide["router__sp"].recent_touching == 3
    assert wide["router__sp"].recent_promoted == 2


def test_track_record_mutation_id_filter_and_missing_index(db_path: Path, tmp_path: Path) -> None:
    only = mutation_point_track_record(db_path, _EPOCH, "planner__sp")
    assert set(only) == {"planner__sp"}
    assert only["planner__sp"].experiments_touching == 1

    assert mutation_point_track_record(db_path, _EPOCH, "unknown__id") == {}
    assert mutation_point_track_record(tmp_path / "never-built.db", _EPOCH) == {}


# ---------------------------------------------------------------------------
# The banded prompt annotation
# ---------------------------------------------------------------------------


def _record(**overrides: object) -> MutationTrackRecord:
    base: dict[str, object] = {
        "mutation_id": "router__sp",
        "experiments_touching": 3,
        "confounded_experiments": 1,
        "promoted": 2,
        "delta_min": -0.5,
        "delta_median": -0.1,
        "delta_max": 0.3,
        "recent_touching": 2,
        "recent_promoted": 1,
    }
    base.update(overrides)
    return MutationTrackRecord(**base)  # type: ignore[arg-type]


def test_annotation_bands_deltas_and_labels_touching_not_causal() -> None:
    line = render_mutation_track_annotation(_record())
    assert "touched:3" in line
    assert "promoted:2/3" in line
    # Δscalar is BANDED — improved/flat/regressed buckets, never the raw
    # experiment-level float (the restricted-visibility envelope).
    assert "Δscalar[best:improved median:improved worst:regressed]" in line
    assert "0.5" not in line
    assert "0.3" not in line
    assert "recent" in line
    # Honesty labels: experiment-level attribution, confounding named,
    # never causal.
    assert "experiments touching this point" in line
    assert "1/3 also touched other points — credit confounded" in line
    assert "not causal" in line


def test_annotation_single_patch_attribution_label() -> None:
    """All-sole-point experiments get the clean-attribution label — still
    experiment-level, still not causal."""
    line = render_mutation_track_annotation(_record(confounded_experiments=0))
    assert "each touched only this point" in line
    assert "confounded" not in line
    assert "not causal" in line


def test_annotation_stale_and_no_delta_cases() -> None:
    line = render_mutation_track_annotation(
        _record(recent_touching=0, delta_min=None, delta_median=None, delta_max=None)
    )
    assert "stale" in line
    assert "Δscalar" not in line


def test_mutation_block_annotates_only_recorded_points_and_is_otherwise_byte_identical() -> None:
    mutations = (make_mutation_point(id="router__sp"), make_mutation_point(id="planner__sp"))
    plain = render_mutation_block(mutations)
    assert render_mutation_block(mutations, track_records={}) == plain
    assert render_mutation_block(mutations, track_records=None) == plain

    annotated = render_mutation_block(mutations, track_records={"router__sp": _record()})
    assert "track record: touched:3" in annotated
    # Only the recorded point gains a line; the un-recorded one is untouched.
    assert annotated.count("track record:") == 1
    assert plain != annotated


def test_user_prompt_threads_track_records_and_stays_byte_identical_without() -> None:
    kwargs: dict = {
        "current_loss_summary": "loss=1.0",
        "patterns": (),
        "mutations": (make_mutation_point(id="router__sp"),),
    }
    plain = render_proposal_evidence(**kwargs)
    assert render_proposal_evidence(**kwargs, mutation_track_records=None) == plain
    assert render_proposal_evidence(**kwargs, mutation_track_records={}) == plain
    annotated = render_proposal_evidence(**kwargs, mutation_track_records={"router__sp": _record()})
    assert "track record: touched:3" in annotated


# ---------------------------------------------------------------------------
# The read-only proposer tool
# ---------------------------------------------------------------------------


def _tool_ctx(tmp_path: Path) -> ProposerToolContext:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    _seed_index(workspace / "index.db")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir(exist_ok=True)
    return ProposerToolContext(
        workspace_root=workspace,
        generation_root=snapshot,
        epoch_id=_EPOCH,
        mutations=(
            make_mutation_point(id="router__sp"),
            make_mutation_point(id="style__untouched"),
        ),
    )


def test_tool_is_registered() -> None:
    assert mutation_track_record in DEFAULT_PROPOSER_TOOLS


def test_tool_output_shape_is_banded_aggregates(tmp_path: Path) -> None:
    with bind_proposer_tool_context(_tool_ctx(tmp_path)):
        payload = json.loads(mutation_track_record("router__sp"))
    assert payload["mutation_id"] == "router__sp"
    assert payload["experiments_touching"] == 3
    assert payload["promoted"] == 2
    assert payload["confounded_experiments"] == 1
    assert payload["recent"] is True
    assert "experiments touching this point" in payload["basis"]
    assert "not causal" in payload["basis"]
    # The summary is the SAME banded annotation the manifest carries — no
    # raw experiment-level delta leaks through the tool either.
    assert "Δscalar[best:improved median:improved worst:regressed]" in payload["summary"]
    assert "-0.5" not in payload["summary"]


def test_tool_zeroed_record_for_untouched_point(tmp_path: Path) -> None:
    with bind_proposer_tool_context(_tool_ctx(tmp_path)):
        payload = json.loads(mutation_track_record("style__untouched"))
    assert payload["experiments_touching"] == 0
    assert payload["promoted"] == 0
    assert payload["recent"] is False
    assert "no settled experiment" in payload["summary"]


def test_tool_rejects_ids_outside_the_manifest(tmp_path: Path) -> None:
    with bind_proposer_tool_context(_tool_ctx(tmp_path)):
        with pytest.raises(ValueError, match="unknown mutation id"):
            mutation_track_record("not_in_manifest")


def test_tool_requires_bound_context() -> None:
    with pytest.raises(RuntimeError, match="no bound ProposerToolContext"):
        mutation_track_record("router__sp")
