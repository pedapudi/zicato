"""Regression pins for issue #124 — the rejection reason reaches lineage.json.

``lineage.json`` is the DAG-shaped record of what the loop did: one node per
generation (:func:`zicato.epoch.lineage.append_to_lineage`). It used to carry
``id`` / ``parent_id`` / ``promoted`` / ``created_at`` / ``round_index`` only —
recording THAT a generation was rejected and nothing about WHY, so anything
reading the DAG (the dashboard, a proposer tool, post-run analysis) had to join
against ``experiment.json`` per generation to recover the reason the gate had
already computed. The settle-time append now carries the reason and the duel's
two scalars; these pins hold that shape, and the tri-state invariant it must
not violate.

The reason was never far away: ``zicato.orchestrator`` computes
``rejection_reason`` per challenger in one loop and calls
:func:`append_to_lineage` for the same challengers in the next loop of the
SAME function, and used to discard the reason in between (``orchestrator.py``
~2521 and ~2624). The parent and child scalars are in that scope too.

The invariant these pins enforce: ``promoted`` is TRI-STATE. ``True`` is
promoted, ``False`` is a resolved dead branch, and ``None`` is an
APPLIED-BUT-UNRESOLVED in-flight challenger that has landed a snapshot but not
yet been crowned or cut. A reason field must be empty for BOTH ``True`` and
``None`` — a pending node that grew a reason would read as rejected, which is
the exact ambiguity ``pending`` was introduced to remove.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zicato.core.types import Generation, ScoringWeights
from zicato.epoch import append_to_lineage, load_lineage, new_epoch

#: The gate's own phrasing for the commonest rejection (Rule 1).
REASON = "insufficient improvement: 0.7328 vs 0.7188 (margin 0.0200)"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    return ws


@pytest.fixture()
def rubric_file(tmp_path: Path) -> Path:
    p = tmp_path / "rubric.md"
    p.write_text("# Rubric\n")
    return p


def _gen(
    workspace: Path, epoch_id: str, gid: str, parent: str | None, promoted: bool
) -> Generation:
    """A real :class:`Generation`, the shape the settle path hands lineage."""
    return Generation(
        id=gid,
        epoch_id=epoch_id,
        parent_id=parent,
        snapshot_root=workspace / f"snap_{gid}",
        created_at="2026-07-29T10:00:00+00:00",
        promoted=promoted,
        round_index=1,
    )


def _node(workspace: Path, epoch_id: str, gid: str) -> dict[str, Any]:
    """The persisted lineage node for ``gid`` (read back through the API)."""
    data = load_lineage(workspace)
    [entry] = [e for e in data["epochs"] if e["id"] == epoch_id]
    [node] = [g for g in entry["generations"] if g["id"] == gid]
    result: dict[str, Any] = node
    return result


# ---------------------------------------------------------------------------
# Pin 1 — the reason must reach the DAG
# ---------------------------------------------------------------------------


def test_lineage_records_the_rejection_reason(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    """A rejected generation must say WHY in the record tooling reads."""
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    append_to_lineage(workspace, cfg.id, _gen(workspace, cfg.id, "v0", None, True), None)
    append_to_lineage(
        workspace,
        cfg.id,
        _gen(workspace, cfg.id, "v1", "v0", False),
        "v0",
        rejection_reason=REASON,
    )

    node = _node(workspace, cfg.id, "v1")
    # The fields that hold today — this is a real rejected node, not a stub.
    assert node["promoted"] is False
    assert node["parent_id"] == "v0"
    # The pin.
    assert node["rejection_reason"] == REASON


# ---------------------------------------------------------------------------
# Pin 2 — the empty-reason-on-promote invariant
# ---------------------------------------------------------------------------


def test_lineage_promoted_generation_carries_an_empty_reason(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    """``reason == ""`` must keep meaning "promoted", as it does everywhere else.

    Five persisted surfaces already read an empty reason as the promote
    signal; a lineage field that could carry text on a promoted node would
    make the DAG disagree with all of them.
    """
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    append_to_lineage(
        workspace,
        cfg.id,
        _gen(workspace, cfg.id, "v1", "v0", True),
        "v0",
        rejection_reason=REASON,  # a caller passing one anyway must not win
    )

    node = _node(workspace, cfg.id, "v1")
    assert node["promoted"] is True
    assert node["rejection_reason"] == ""


# ---------------------------------------------------------------------------
# Pin 3 — the tri-state guard
# ---------------------------------------------------------------------------


def test_lineage_pending_generation_carries_an_empty_reason(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    """An in-flight challenger is neither promoted nor rejected — and says so.

    The pending write is followed by a settle-time upsert of the SAME node;
    the reason must appear only on the resolved write.
    """
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    gen = _gen(workspace, cfg.id, "v1", "v0", False)
    append_to_lineage(workspace, cfg.id, gen, "v0", pending=True)

    pending_node = _node(workspace, cfg.id, "v1")
    # Holds today: pending is persisted as null, not False.
    assert pending_node["promoted"] is None
    # The pin: a pending node must not carry a reason it cannot yet have.
    assert pending_node["rejection_reason"] == ""

    append_to_lineage(workspace, cfg.id, gen, "v0", rejection_reason=REASON)
    settled_node = _node(workspace, cfg.id, "v1")
    assert settled_node["promoted"] is False
    assert settled_node["rejection_reason"] == REASON


# ---------------------------------------------------------------------------
# Pin 4 — the duel's numbers, alongside the reason
# ---------------------------------------------------------------------------


def test_lineage_records_the_duel_scalars(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    """Reason and effect size belong together, on both decision paths.

    ``None`` — not ``0.0`` — is the absent value: a scalar of zero is a legal
    measurement, so a numeric default would make "this record predates the
    field" indistinguishable from "both sides scored zero" (the argument
    ``GateEvaluated`` already settled for the round log).
    """
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    append_to_lineage(
        workspace,
        cfg.id,
        _gen(workspace, cfg.id, "v1", "v0", False),
        "v0",
        rejection_reason=REASON,
        parent_scalar=0.7188,
        child_scalar=0.7328,
    )

    node = _node(workspace, cfg.id, "v1")
    assert node["parent_scalar"] == pytest.approx(0.7188)
    assert node["child_scalar"] == pytest.approx(0.7328)
    assert node["delta_scalar"] == pytest.approx(0.0140)

    # A generation recorded without them decodes as absent, never as zero.
    append_to_lineage(workspace, cfg.id, _gen(workspace, cfg.id, "v2", "v1", True), "v1")
    bare = _node(workspace, cfg.id, "v2")
    assert bare["parent_scalar"] is None
    assert bare["delta_scalar"] is None


# ---------------------------------------------------------------------------
# Pin 5 — the settle path actually threads it
# ---------------------------------------------------------------------------


def test_finalize_generation_threads_the_reason_onto_the_lineage_node(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    """The gate's reason must survive the ONE pipeline every round tail uses.

    Pins 1-4 hold the lineage writer's contract; this one holds the wiring,
    at the seam both settle paths share (``_finalize_generation`` — the
    gauntlet calls it with the lineage generation, the multi-challenger loop
    defers lineage but reads the same outcome back). Without it the fields
    could be correct and permanently empty.
    """
    from zicato.core.experiment import Experiment, HypothesisSpec, OutcomeRecord
    from zicato.epoch import write_experiment
    from zicato.evolve.persist import _finalize_generation

    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    write_experiment(
        workspace,
        cfg.id,
        "v1",
        Experiment(
            id="exp_v1",
            epoch_id=cfg.id,
            generation_id="v1",
            parent_generation_id="v0",
            proposed_at="2026-07-29T10:00:00+00:00",
            hypothesis=HypothesisSpec(
                core_idea="tighten the researcher instruction",
                modulating=(),
                why="fewer confabulations",
                expected_drift_movements=(),
                expected_pass_rate_delta="+0.05",
            ),
            patches=(),
            outcome=None,
        ),
    )

    _finalize_generation(
        workspace_root=workspace,
        epoch_id=cfg.id,
        generation_id="v1",
        outcome=OutcomeRecord(
            ran_at="2026-07-29T10:30:00+00:00",
            drift_movements=(),
            pass_rate_delta=0.0,
            drift_loss_delta=0.0,
            scalar_score_delta=0.014,
            tournament_decision="rejected",
            rejection_reason=REASON,
        ),
        lineage_generation=_gen(workspace, cfg.id, "v1", "v0", False),
        lineage_parent_id="v0",
        lineage_parent_scalar=0.7188,
        lineage_child_scalar=0.7328,
        journal=False,
    )

    node = _node(workspace, cfg.id, "v1")
    assert node["promoted"] is False
    assert node["rejection_reason"] == REASON
    assert node["parent_scalar"] == pytest.approx(0.7188)
    assert node["delta_scalar"] == pytest.approx(0.0140)


def test_lineage_settled_reason_survives_a_later_defence_upsert(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    """Once set, the verdict is not blanked by a re-record.

    A generation is re-appended whenever it is recorded again (a champion
    defending a later round comes back through the same writer with nothing
    to say about a rejection). ``round_index`` already has this discipline;
    the settle-time facts share it, or a later no-argument upsert would
    quietly erase why a dead branch died.
    """
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    gen = _gen(workspace, cfg.id, "v1", "v0", False)
    append_to_lineage(
        workspace,
        cfg.id,
        gen,
        "v0",
        rejection_reason=REASON,
        parent_scalar=0.7188,
        child_scalar=0.7328,
    )

    append_to_lineage(workspace, cfg.id, gen, "v0")

    node = _node(workspace, cfg.id, "v1")
    assert node["rejection_reason"] == REASON
    assert node["parent_scalar"] == pytest.approx(0.7188)
    assert node["child_scalar"] == pytest.approx(0.7328)
    assert node["delta_scalar"] == pytest.approx(0.0140)


# ---------------------------------------------------------------------------
# Pin 7 — the API surfaces what the DAG now records
# ---------------------------------------------------------------------------


def test_lineage_view_passes_the_settle_facts_through(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    """``/api/lineage`` carries the reason + scalars, present-only.

    Passthrough is the whole surface for this pass — no UI renders them
    yet — so the pin is that the fields reach the payload for a node that
    has them, and that a node without them keeps its prior payload shape
    (the key ABSENT, not null, matching how ``round_index`` degrades).
    """
    import json as _json  # noqa: PLC0415

    from zicato.query import WorkspacePaths, build_lineage_view  # noqa: PLC0415

    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    append_to_lineage(
        workspace,
        cfg.id,
        _gen(workspace, cfg.id, "v1", "v0", False),
        "v0",
        rejection_reason=REASON,
        parent_scalar=0.7188,
        child_scalar=0.7328,
    )
    append_to_lineage(workspace, cfg.id, _gen(workspace, cfg.id, "v2", "v1", True), "v1")
    for gid in ("v1", "v2"):
        gdir = workspace / "epochs" / cfg.id / "generations" / gid
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / "experiment.json").write_text(
            _json.dumps({"generation_id": gid, "proposed_at": "2026-07-29T10:00:00+00:00"})
        )

    view = build_lineage_view(WorkspacePaths(workspace), include_ratings=False)
    nodes = {n["generation_id"]: n for n in view["generations"]}

    assert nodes["v1"]["rejection_reason"] == REASON
    assert nodes["v1"]["parent_scalar"] == pytest.approx(0.7188)
    assert nodes["v1"]["delta_scalar"] == pytest.approx(0.0140)
    # A promoted node has no reason to surface and no duel numbers recorded.
    assert "rejection_reason" not in nodes["v2"]
    assert "parent_scalar" not in nodes["v2"]


# ---------------------------------------------------------------------------
# Pin 8 — the "reason ⇒ rejected" invariant belongs to the RECORD
# ---------------------------------------------------------------------------


def test_lineage_reason_does_not_outlive_the_rejection_that_set_it(
    workspace: Path, board_file: Path, rubric_file: Path
) -> None:
    """A reason must never survive its node ceasing to be a rejection.

    The writer refuses to SET a reason on anything but a settled rejection,
    but ``promoted`` is rewritten unconditionally by every upsert while the
    reason is once-set and sticky. A node that settles rejected and is later
    re-recorded promoted (or re-opened pending) would therefore carry both a
    ``promoted`` that says otherwise and a non-empty reason — and the five
    persisted surfaces that read a non-empty reason as "rejected" would
    render it rejected. That is the exact ambiguity this issue removes, so
    the invariant has to hold on the stored record, not just on the write
    that created it.
    """
    cfg = new_epoch(workspace, "alpha", board_file, rubric_file, ScoringWeights())
    rejected = _gen(workspace, cfg.id, "v1", "v0", False)
    append_to_lineage(workspace, cfg.id, rejected, "v0", rejection_reason=REASON)
    assert _node(workspace, cfg.id, "v1")["rejection_reason"] == REASON

    # Re-recorded as promoted — the reason no longer describes this node.
    append_to_lineage(workspace, cfg.id, _gen(workspace, cfg.id, "v1", "v0", True), "v0")
    assert _node(workspace, cfg.id, "v1")["rejection_reason"] == ""

    # And re-opened pending, from a settled rejection.
    append_to_lineage(workspace, cfg.id, rejected, "v0", rejection_reason=REASON)
    assert _node(workspace, cfg.id, "v1")["rejection_reason"] == REASON
    append_to_lineage(workspace, cfg.id, rejected, "v0", pending=True)
    node = _node(workspace, cfg.id, "v1")
    assert node["promoted"] is None
    assert node["rejection_reason"] == ""
