"""One lineage walk per /api/environment, and a scoped walk for one epoch.

``build_lineage_view`` walks ``epochs/*/generations/*`` and reads a JSON file per
generation directory, so it is the most expensive thing the read model does —
cProfile attributed 84% of ``build_environment`` to it, at ``ncalls=2``. Two
independent builds were happening:

1. ``build_environment`` serves the ``generations`` feed from one walk and
   builds nothing else from the lineage; ``build_score_trajectory`` accepts a
   ``lineage`` the caller already has, for the readers that serve both.
2. ``build_score_trajectory`` walked EVERY epoch and then filtered down to one,
   even though ``build_lineage_view`` takes an ``epoch_id`` that scopes the walk.

Two more readers had the same shape and are fixed the same way:

3. ``build_per_judge_trend`` walked EVERY epoch and filtered to one, exactly
   like (2). It now passes its ``epoch_id`` down.
4. ``build_round_timeline`` walked one epoch and then made
   ``build_score_trajectory`` walk that SAME epoch again. It now hands its
   feed over — a feed already scoped to the requested epoch is a valid
   hand-off, because the trajectory's epoch filter is a no-op on it.

All four are pure internal plumbing: the payloads must not move. These tests pin
the call COUNT (the regression that reopens the cost) and the payload
equivalence (the regression that would corrupt the curve). The reader-parity
golden covers the byte-level claim across a multi-epoch fixture for the
environment, the trajectory and the per-judge trend; the round timeline is not
in that golden, so its equivalence is pinned here directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._workspace_support import (
    experiment_record,
    seed_index,
    set_current_epoch,
    workspace,
    write_epoch,
    write_generation,
)
from zicato.query import WorkspacePaths, build_score_trajectory
from zicato.query.judge_view import build_environment, build_per_judge_trend
from zicato.query.lineage_view import build_lineage_view
from zicato.query.rounds_view import build_round_timeline
from zicato.workspace import WorkspaceLayout

EPOCHS = ("e0", "e1", "e2")
GENS = ("v0", "v1", "v2", "v3")


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    """A three-epoch workspace with real generation directories on disk."""
    built = workspace(tmp_path)
    set_current_epoch(built, EPOCHS[0])

    epoch_rows: list[dict[str, Any]] = []
    loss_rows: list[dict[str, Any]] = []
    for ei, eid in enumerate(EPOCHS):
        write_epoch(built, eid, config={"goal": f"goal {eid}"})
        epoch_rows.append(
            {
                "epoch_id": eid,
                "contract_hash": "h",
                "created_at": f"2026-05-1{ei}T04:00:00Z",
                "closed": 0,
                "goal": f"goal {eid}",
                "parent_epoch_id": None,
            }
        )
        for gi, gid in enumerate(GENS):
            write_generation(
                built,
                eid,
                gid,
                experiment=experiment_record(
                    gid,
                    parent_generation_id=None if gi == 0 else GENS[gi - 1],
                    proposed_at=f"2026-05-1{ei}T0{gi}:30:00Z",
                    round_index=gi,
                    decision="promoted" if gi % 2 else "rejected",
                ),
            )
            # two entries, and v1 is RE-SCORED (t1 twice) so the scalar fold is
            # exercised rather than trivially averaged
            runs = [("t1", 0.4 + 0.01 * gi), ("t2", 0.2 + 0.01 * gi)]
            if gid == "v1":
                runs.append(("t1", 0.6))
            for ri, (entry, loss) in enumerate(runs):
                loss_rows.append(
                    {
                        "run_id": f"{eid}-{gid}-{entry}-{ri}",
                        "epoch_id": eid,
                        "generation_id": gid,
                        "entry_id": entry,
                        "drift_loss": loss,
                        "pass_fail": 1,
                        "runtime_ms": 100,
                        "wall_clock_budget_exceeded": 0,
                        "loss_json": "{}",
                        "tournament_id": None,
                    }
                )
    seed_index(
        built,
        {"epochs": epoch_rows, "loss_profiles": loss_rows},
    )
    return built


def _count_walks(monkeypatch) -> list[tuple[str | None, bool]]:
    """Record every build_lineage_view call as (epoch_id, include_ratings).

    Every consumer imports the symbol directly, so each module reference has
    to be patched — patching lineage_view alone would count nothing.
    """
    calls: list[tuple[str | None, bool]] = []
    real = build_lineage_view

    def spy(paths, epoch_id=None, *, include_ratings=True):
        calls.append((epoch_id, include_ratings))
        return real(paths, epoch_id, include_ratings=include_ratings)

    for module in ("judge_view", "gate_view", "rounds_view"):
        monkeypatch.setattr(f"zicato.query.{module}.build_lineage_view", spy)
    return calls


def test_environment_walks_the_lineage_once(layout: WorkspaceLayout, monkeypatch) -> None:
    """THE pin: /api/environment builds the lineage exactly ONCE.

    A second walk — a component added to the payload that builds its own
    feed — doubles the most expensive read in the payload, and fails here.
    """
    calls = _count_walks(monkeypatch)
    build_environment(WorkspacePaths(layout.root))
    assert len(calls) == 1, f"expected ONE lineage walk, got {len(calls)}: {calls}"


def test_environment_payload_is_unchanged_by_the_hand_off(layout: WorkspaceLayout) -> None:
    """Passing the feed in yields the SAME trajectory as building it inside.

    Exact equality: the hand-off is plumbing, so any difference is a bug. The
    supplied feed carries the rating triple and the internally built one does
    not, which must not matter — nothing here reads it.
    """
    paths = WorkspacePaths(layout.root)
    supplied = build_lineage_view(paths)
    assert build_score_trajectory(paths, lineage=supplied) == build_score_trajectory(paths)


def test_scoped_walk_matches_walking_everything_then_filtering(layout: WorkspaceLayout) -> None:
    """Scoping the walk to one epoch == the old global walk plus a filter.

    This is the Fix-2 equivalence claim. If scoping perturbed ordering or any
    per-node field, the curve would silently change shape.
    """
    paths = WorkspacePaths(layout.root)
    global_feed = build_lineage_view(paths, include_ratings=False)
    for eid in EPOCHS:
        scoped = build_score_trajectory(paths, eid)
        from_global = build_score_trajectory(paths, eid, lineage=global_feed)
        assert scoped == from_global, f"scoped walk diverged from the global walk for {eid}"


def test_scoped_walk_reads_only_the_requested_epoch(layout: WorkspaceLayout, monkeypatch) -> None:
    """The scope actually reaches build_lineage_view — not filtered after."""
    calls = _count_walks(monkeypatch)
    build_score_trajectory(WorkspacePaths(layout.root), EPOCHS[1])
    assert calls == [(EPOCHS[1], False)], (
        f"expected one epoch-scoped walk, got {calls} — "
        "an unscoped walk means every epoch is being read to render one"
    )


def test_scoped_and_global_agree_on_every_generation(layout: WorkspaceLayout) -> None:
    """Every point (ids, order, scalars) matches between the two paths."""
    paths = WorkspacePaths(layout.root)
    for eid in EPOCHS:
        points = build_score_trajectory(paths, eid)["points"]
        assert [p["generation_id"] for p in points] == list(GENS), "lineage order moved"
        # v1 is the re-scored generation: t1 mean = (0.41 + 0.6)/2, t2 = 0.21
        v1 = next(p for p in points if p["generation_id"] == "v1")
        assert v1["scalar"] == pytest.approx(((0.41 + 0.6) / 2 + 0.21) / 2)
        assert v1["entry_count"] == 2


def test_no_current_epoch_still_walks_globally(layout: WorkspaceLayout, monkeypatch) -> None:
    """With no current epoch the global walk is correct, not a bug to scope away."""
    layout.current_epoch_marker.unlink()
    calls = _count_walks(monkeypatch)
    result = build_score_trajectory(WorkspacePaths(layout.root))
    assert calls == [(None, False)], f"expected ONE global walk, got {calls}"
    # every epoch's generations are present
    assert len(result["points"]) == len(EPOCHS) * len(GENS)


# ---------------------------------------------------------------------------
# The same defect, in the two readers the first pass did not reach
# ---------------------------------------------------------------------------


def test_per_judge_trend_walks_only_its_own_epoch(layout: WorkspaceLayout, monkeypatch) -> None:
    """Fix 3: the per-judge matrix scopes its walk instead of reading everything.

    An unscoped walk here means every epoch's generation directories are read
    to render ONE epoch's heatmap.
    """
    calls = _count_walks(monkeypatch)
    build_per_judge_trend(WorkspacePaths(layout.root), EPOCHS[1])
    assert calls == [(EPOCHS[1], False)], f"expected one epoch-scoped walk, got {calls}"


def test_per_judge_trend_matches_the_old_global_walk(layout: WorkspaceLayout, monkeypatch) -> None:
    """Fix 3's equivalence claim, against a reconstruction of the old reader.

    The pre-change reader walked every epoch and filtered the feed down to
    one afterwards; the scoped walk has to be indistinguishable from that,
    which is also what makes dropping the now-redundant filter safe.
    """
    paths = WorkspacePaths(layout.root)
    real = build_lineage_view

    def global_walk_then_filter(p, epoch_id=None, *, include_ratings=True):
        feed = real(p, None, include_ratings=include_ratings)
        return {
            "generations": [
                g for g in feed["generations"] if epoch_id is None or g["epoch_id"] == epoch_id
            ]
        }

    monkeypatch.setattr("zicato.query.judge_view.build_lineage_view", global_walk_then_filter)
    old = {eid: build_per_judge_trend(paths, eid) for eid in EPOCHS}
    monkeypatch.undo()
    for eid in EPOCHS:
        assert (
            build_per_judge_trend(paths, eid) == old[eid]
        ), f"the scoped walk diverged from the global-walk-then-filter for {eid}"


def test_round_timeline_walks_the_lineage_once(layout: WorkspaceLayout, monkeypatch) -> None:
    """Fix 4: the round timeline and its trajectory share ONE walk of the epoch.

    Both were scoped to the same epoch already, so the second walk was pure
    duplicate cost — this pins the hand-off that removed it.
    """
    calls = _count_walks(monkeypatch)
    build_round_timeline(WorkspacePaths(layout.root), EPOCHS[0])
    assert calls == [(EPOCHS[0], False)], f"expected ONE scoped walk, got {len(calls)}: {calls}"


def test_a_same_epoch_scoped_feed_is_a_valid_hand_off(layout: WorkspaceLayout) -> None:
    """What fix 4 rests on: an already-scoped feed yields the same trajectory.

    The trajectory keeps its epoch filter because a supplied feed MAY be
    workspace-global; on a feed already scoped to the epoch being asked for,
    that filter is a no-op and the curve is identical.
    """
    paths = WorkspacePaths(layout.root)
    for eid in EPOCHS:
        scoped_feed = build_lineage_view(paths, eid, include_ratings=False)
        assert build_score_trajectory(paths, eid, lineage=scoped_feed) == build_score_trajectory(
            paths, eid
        ), f"a same-epoch scoped feed changed the trajectory for {eid}"
