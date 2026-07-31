"""Strict-xfail pins for issue #124 — lineage.json omits the rejection reason.

``lineage.json`` is the DAG-shaped record of what the loop did: one node per
generation carrying ``id`` / ``parent_id`` / ``promoted`` / ``created_at`` /
``round_index`` (:func:`zicato.epoch.lineage.append_to_lineage`). It records
THAT a generation was rejected and nothing about WHY, so anything reading the
DAG — the dashboard, a proposer tool, post-run analysis — has to join against
``experiment.json`` per generation to recover the reason the gate already
computed.

The reason is not far away: ``zicato.orchestrator`` computes
``rejection_reason`` per challenger in one loop and calls
:func:`append_to_lineage` for the same challengers in the next loop of the
SAME function, discarding the reason in between (``orchestrator.py`` ~2521
and ~2624). The parent and child scalars are in that scope too.

The invariant these pins must respect: ``promoted`` is TRI-STATE. ``True`` is
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
def board_file(tmp_path: Path) -> Path:
    p = tmp_path / "board.jsonl"
    p.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n'
    )
    return p


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


@pytest.mark.xfail(
    strict=True,
    reason="issue #124: append_to_lineage persists only id/parent_id/promoted/"
    "created_at/round_index — the gate's reason is dropped on the floor",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="issue #124: no rejection_reason field exists on a lineage node",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="issue #124: no rejection_reason field exists on a lineage node",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="issue #124: the lineage node carries no scalars; the duel's "
    "numbers survive only in the round log and experiment.json",
)
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
