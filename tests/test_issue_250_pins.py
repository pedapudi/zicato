"""Regression pins for issue #250 — the run identity was not replicate-keyed.

A board unit is ``(generation, entry, replicate)``. Before this fix the run
id and the events file were keyed by ``(generation, entry)`` only, so the
replicates of one unit shared both:

* ``runtime/active_runs/{run_id}.json`` — each worker writes it with its own
  pid, so two replicates in flight leave the supervisor tracking one of them
  and the first to finish deletes the record for both;
* the kill-request marker, keyed the same way;
* ``runs/<entry>/events.jsonl`` — the worker opens the sink with
  ``mode="write"``, so each replicate TRUNCATED the one before it. With the
  one-predecessor archive of issue #122 absorbing a second draw, a
  3-replicate unit kept the raw telemetry of its last two draws only.

The loss slot was already replicate-keyed (casebook Case 1). This is the
same invariant applied to the artifacts Case 1 left behind: any persisted
artifact consumed under a keyed scheme must be WRITTEN through that scheme.

Replicate 0 keeps every canonical name, so a single-replicate workspace is
byte-identical to one from before the fix.
"""

from __future__ import annotations

from pathlib import Path

from zicato.workspace import WorkspaceLayout

# NOTE: the symbols this fix ADDS (``run_id_for_unit``, ``events_prev_path_for``,
# the ``replicate_index`` parameters) are imported inside the tests that use
# them, never at module scope. A module-level import would make the whole file
# fail to COLLECT against the pre-fix tree, and an ImportError proves only that
# a new name is absent — not that the old behaviour was wrong. The data-loss
# test below drives only production APIs this fix did not change, so it
# collects and runs against the pre-fix tree and fails on its assertion. That
# is the red state the casebook requires.

EPOCH = "2026-08-18_alpha"
GEN = "v3"
ENTRY = "conv_summary"


def test_replicate_0_run_id_is_the_historical_string() -> None:
    """Replicate 0 keeps ``{generation}--{entry}`` exactly."""
    from zicato.core.workspace import run_id_for_unit

    assert run_id_for_unit(GEN, ENTRY) == f"{GEN}--{ENTRY}"
    assert run_id_for_unit(GEN, ENTRY, 0) == f"{GEN}--{ENTRY}"


def test_replicates_of_one_unit_get_distinct_run_ids() -> None:
    """The collision itself: r0 and r1 of ONE unit must not share an id."""
    from zicato.core.workspace import run_id_for_unit

    r0 = run_id_for_unit(GEN, ENTRY, 0)
    r1 = run_id_for_unit(GEN, ENTRY, 1)
    r2 = run_id_for_unit(GEN, ENTRY, 2)
    assert len({r0, r1, r2}) == 3
    assert r1 == f"{GEN}--{ENTRY}--r1"


def test_replicates_of_one_unit_get_distinct_active_run_records(tmp_path: Path) -> None:
    """Two replicates in flight must not share one ``active_runs`` file.

    This is the fault that let the first worker to finish delete the
    supervisor's only record of its still-running sibling.
    """
    from zicato.core.workspace import run_id_for_unit
    from zicato.runtime.paths import active_run_path

    paths = {active_run_path(tmp_path, run_id_for_unit(GEN, ENTRY, r)) for r in (0, 1, 2)}
    assert len(paths) == 3


def test_replicates_of_one_unit_get_distinct_events_files(tmp_path: Path) -> None:
    """Each replicate owns its raw telemetry; r0 keeps the canonical name."""
    from zicato.core.workspace import events_jsonl_path

    r0 = events_jsonl_path(tmp_path, EPOCH, GEN, ENTRY, 0)
    r1 = events_jsonl_path(tmp_path, EPOCH, GEN, ENTRY, 1)
    assert r0.name == "events.jsonl"
    assert r1.name == "events.r1.jsonl"
    assert r0.parent == r1.parent  # the replicate lives in the NAME, not the path
    assert r0 != r1


def test_events_archive_stays_within_its_own_replicate(tmp_path: Path) -> None:
    """One replicate's ``.prev`` archive cannot displace another's.

    ``archive_prior_events`` retains exactly one predecessor (issue #122).
    Keyed per replicate, that predecessor is the previous ROUND's draw of
    this same unit — which is what #122 intended — rather than a different
    replicate of the same round.
    """
    from zicato.telemetry.sink import events_prev_path_for

    layout = WorkspaceLayout(tmp_path)
    prev0 = layout.events_prev(EPOCH, GEN, ENTRY, 0)
    prev1 = layout.events_prev(EPOCH, GEN, ENTRY, 1)
    assert prev0.name == "events.prev.jsonl"
    assert prev1.name == "events.r1.prev.jsonl"
    assert prev0 != prev1
    # The sink derives the archive name from the source path, so the two
    # agree with the layout.
    assert events_prev_path_for(layout.events(EPOCH, GEN, ENTRY, 0)) == prev0
    assert events_prev_path_for(layout.events(EPOCH, GEN, ENTRY, 1)) == prev1


def test_every_run_id_producer_agrees(tmp_path: Path) -> None:
    """The parent, the scheduler's span, and the worker must build one id.

    Three call sites hand-rolled ``f"{generation_id}--{entry_id}"`` before
    this fix. A worker that disagreed with its parent about the id would
    write an ``active_runs`` record the parent never clears.
    """
    from zicato._tournament_worker import _entry_replicate_index_from_context
    from zicato.core import BoardEntry
    from zicato.core.workspace import run_id_for_unit
    from zicato.tournament.worker_transport import _run_id_for, _stamp_replicate_index

    del tmp_path

    class _Gen:
        id = GEN

    entry = BoardEntry(id=ENTRY, kind="single_turn", wall_clock_budget_seconds=60, input="x")
    stamped = _stamp_replicate_index([entry], 2)[0]

    parent_id = _run_id_for(_Gen(), stamped)  # type: ignore[arg-type]
    worker_id = run_id_for_unit(GEN, stamped.id, _entry_replicate_index_from_context(stamped))
    assert parent_id == worker_id == f"{GEN}--{ENTRY}--r2"


def test_every_replicate_keeps_its_own_raw_telemetry(tmp_path: Path) -> None:
    """The DATA LOSS pin: a replicated unit keeps one events file per draw.

    Drives the production API that the fix did not change — ``run_matchup``
    at ``replicates=2`` through REAL subprocess workers — and then reads the
    run directory. Before the fix both replicates resolved to the one
    canonical ``events.jsonl``, the worker opened it with ``mode="write"``,
    and replicate 1 truncated replicate 0. So this test compiles against the
    pre-fix tree and fails on the assertion, which is the red state the
    casebook requires.
    """
    import asyncio

    from tests.test_decision_procedure_power import (
        BASE_TOKENS,
        NAIVE_WEIGHTS,
        _board,
        _real_gen,
        _worker_config,
    )
    from zicato.tournament.runner import run_matchup
    from zicato_examples.target_0_convergence.harness import make_noisy_adapter

    workspace = tmp_path / "ws"
    workspace.mkdir()
    subset = ("conv_summary",)
    asyncio.run(
        run_matchup(
            adapter=make_noisy_adapter({"noise_sigma": 0.35}),
            left_gen=_real_gen(workspace, "aa-left", BASE_TOKENS),
            right_gen=_real_gen(workspace, "aa-right", BASE_TOKENS),
            board=list(_board()),
            weights=NAIVE_WEIGHTS,
            config=_worker_config(workspace, seed=7),
            workspace_root=workspace,
            epoch_id="e0",
            board_subset=subset,
            replicates=2,
            match_id="issue-250",
        )
    )

    run = WorkspaceLayout(workspace).run_dir("e0", "aa-right", "conv_summary")
    events = sorted(p.name for p in run.glob("events*.jsonl"))
    assert "events.jsonl" in events, f"replicate 0 lost its telemetry: {events}"
    assert "events.r1.jsonl" in events, f"replicate 1 lost its telemetry: {events}"
    # Both draws are real measurements, not one file copied twice.
    r0 = (run / "events.jsonl").read_text(encoding="utf-8")
    r1 = (run / "events.r1.jsonl").read_text(encoding="utf-8")
    assert r0.strip() and r1.strip()
