"""Tests for :mod:`zicato.epoch.gc` — snapshot GC / retention.

Every behavioural test is parametrised over BOTH generation-store
backends: pruning must honour each backend's own notion of "remove the
source tree" (directory: drop ``snapshot/``; git: drop the tag + the
materialised worktree) while NEVER touching records — ``lineage.json``,
the journal's ``experiment.json`` files, ``gen_score.json``. The
dashboard-degradation tests then pin that the Files/diff/patches views
keep answering (never a raised exception) for a pruned generation.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.core.epoch import Generation
from zicato.core.experiment import Experiment, HypothesisSpec
from zicato.core.types import Patch
from zicato.epoch.gc import (
    STORAGE_GC_KEY,
    maybe_prune_on_epoch_close,
    prune_generations,
)
from zicato.epoch.genstore import GenerationStore, default_generation_store
from zicato.epoch.journal import read_experiment, write_experiment, write_seed_experiment
from zicato.epoch.lineage import append_to_lineage, load_lineage

EPOCH = "e1"

# ---------------------------------------------------------------------------
# Fixtures — a mini-workspace with a decided lineage on either backend
# ---------------------------------------------------------------------------


@pytest.fixture(params=["directory", "git"])
def workspace(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A workspace whose ``config.json`` selects the parametrised backend."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "config.json").write_text(
        json.dumps({"storage_backend": request.param}), encoding="utf-8"
    )
    return ws


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _patch(pid: str, content: str) -> Patch:
    return Patch(
        id=pid,
        mutation_id="instr",
        op="replace",
        new_content=content,
        new_numeric=None,
        new_enum=None,
        rationale="gc-test",
    )


def _record_lineage(ws: Path, gen_id: str, parent: str | None, promoted: bool | None) -> None:
    generation = Generation(
        id=gen_id,
        epoch_id=EPOCH,
        parent_id=parent,
        snapshot_root=ws / "unused",
        created_at="2026-07-01T00:00:00+00:00",
        promoted=bool(promoted),
    )
    append_to_lineage(ws, EPOCH, generation, parent, pending=promoted is None)


def _record_experiment(ws: Path, gen_id: str, parent: str, patch: Patch) -> None:
    write_experiment(
        ws,
        EPOCH,
        gen_id,
        Experiment(
            id=f"exp_{EPOCH}_{gen_id}",
            epoch_id=EPOCH,
            generation_id=gen_id,
            parent_generation_id=parent,
            proposed_at="2026-07-01T00:00:00+00:00",
            hypothesis=HypothesisSpec(
                core_idea="gc test",
                modulating=("instr",),
                why="testing",
                expected_drift_movements=(),
                expected_pass_rate_delta="0",
            ),
            patches=(patch,),
            outcome=None,
        ),
    )


def _seed_lineage(ws: Path) -> GenerationStore:
    """v0 promoted → v1 rejected → v2 rejected → v3 promoted → v4 pending.

    Every derived generation is a child of the CURRENT promoted head,
    mirroring the real loop (rejected branches fork off the champion).
    Journal experiment records are written for every derived generation
    so the record-survival assertions have something to check.
    """
    store = default_generation_store(ws)
    tree = ws.parent / "registered" / "agent"
    _write(
        tree / "prompts.py",
        '''
        # zicato:mutable id="instr"
        INSTR = """seed"""
        ''',
    )
    store.seed_generation(EPOCH, "v0", [tree])
    write_seed_experiment(ws, EPOCH, "v0")
    _record_lineage(ws, "v0", None, promoted=True)
    plan: list[tuple[str, str, bool | None]] = [
        ("v1", "v0", False),
        ("v2", "v0", False),
        ("v3", "v0", True),
        ("v4", "v3", None),
    ]
    for gen_id, parent, promoted in plan:
        patch = _patch(f"p_{gen_id}", f'"""{gen_id}"""')
        store.derive_generation(EPOCH, parent, gen_id, [patch])
        _record_experiment(ws, gen_id, parent, patch)
        _record_lineage(ws, gen_id, parent, promoted)
    return store


# ---------------------------------------------------------------------------
# Policy validation
# ---------------------------------------------------------------------------


def test_prune_requires_exactly_one_policy(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    with pytest.raises(ValueError, match="one of keep_last_n / keep_promoted_only"):
        prune_generations(ws, EPOCH)
    with pytest.raises(ValueError, match="not both"):
        prune_generations(ws, EPOCH, keep_last_n=2, keep_promoted_only=True)
    with pytest.raises(ValueError, match=">= 1"):
        prune_generations(ws, EPOCH, keep_last_n=0)


# ---------------------------------------------------------------------------
# prune_generations — behaviour on both backends
# ---------------------------------------------------------------------------


def test_dry_run_plans_but_removes_nothing(workspace: Path) -> None:
    store = _seed_lineage(workspace)
    report = prune_generations(workspace, EPOCH, keep_promoted_only=True, dry_run=True)
    assert report.dry_run is True
    assert report.pruned == ("v1", "v2")
    assert set(report.kept) == {"v0", "v3", "v4"}
    # Nothing was touched: every generation still has its source tree.
    for gen_id in ("v0", "v1", "v2", "v3", "v4"):
        assert store.has_generation(EPOCH, gen_id), gen_id


def test_keep_promoted_only_prunes_settled_rejected(workspace: Path) -> None:
    store = _seed_lineage(workspace)
    lineage_before = json.dumps(load_lineage(workspace), sort_keys=True)

    report = prune_generations(workspace, EPOCH, keep_promoted_only=True, dry_run=False)
    assert report.dry_run is False
    assert report.pruned == ("v1", "v2")

    # Pruned: source trees gone.
    assert not store.has_generation(EPOCH, "v1")
    assert not store.has_generation(EPOCH, "v2")
    # Kept: promoted chain, pending challenger, and the seed all intact.
    for gen_id in ("v0", "v3", "v4"):
        assert store.has_generation(EPOCH, gen_id), gen_id
        assert store.read_file(EPOCH, gen_id, "agent/prompts.py")

    # Records are NEVER touched: lineage byte-identical, experiment
    # records for the pruned generations still readable.
    assert json.dumps(load_lineage(workspace), sort_keys=True) == lineage_before
    for gen_id in ("v1", "v2"):
        experiment = read_experiment(workspace, EPOCH, gen_id)
        assert experiment.patches, gen_id


def test_keep_last_n_retains_recent_rejected(workspace: Path) -> None:
    _seed_lineage(workspace)
    # keep_last_n=3 keeps v2/v3/v4 by recency (plus promoted v0/v3 and
    # pending v4 via the floor); only v1 is old enough to prune.
    report = prune_generations(workspace, EPOCH, keep_last_n=3, dry_run=False)
    assert report.pruned == ("v1",)
    assert set(report.kept) == {"v0", "v2", "v3", "v4"}


def test_pending_and_unrecorded_generations_are_never_pruned(workspace: Path) -> None:
    store = _seed_lineage(workspace)
    # v5 exists in the store but has NO lineage record at all (a crash
    # window between apply and lineage append): conservative keep.
    store.derive_generation(EPOCH, "v3", "v5", [_patch("p_v5", '"""v5"""')])
    report = prune_generations(workspace, EPOCH, keep_promoted_only=True, dry_run=False)
    assert "v5" in report.kept
    assert "v4" in report.kept  # pending (promoted null)
    assert store.has_generation(EPOCH, "v5")
    assert store.has_generation(EPOCH, "v4")


def test_prune_is_idempotent_and_store_stays_healthy(workspace: Path) -> None:
    store = _seed_lineage(workspace)
    prune_generations(workspace, EPOCH, keep_promoted_only=True, dry_run=False)
    # A second sweep finds nothing new and does not raise.
    report = prune_generations(workspace, EPOCH, keep_promoted_only=True, dry_run=False)
    assert report.pruned == ()
    # The store still derives + checks out new generations afterwards.
    root = store.derive_generation(EPOCH, "v3", "v6", [_patch("p_v6", '"""v6"""')])
    assert "v6" in (root / "agent" / "prompts.py").read_text(encoding="utf-8")
    checkout = store.checkout_ephemeral(EPOCH, "v6", "v6--entry_a")
    try:
        assert (checkout.working_dir / "agent" / "prompts.py").is_file()
    finally:
        checkout.cleanup()


def test_directory_backend_keeps_sibling_records_on_disk(tmp_path: Path) -> None:
    """Directory backend: ONLY ``snapshot/`` is removed, siblings survive."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "config.json").write_text(json.dumps({"storage_backend": "directory"}), encoding="utf-8")
    _seed_lineage(ws)
    gen_dir = ws / "epochs" / EPOCH / "generations" / "v1"
    assert (gen_dir / "snapshot").is_dir()
    assert (gen_dir / "experiment.json").is_file()

    prune_generations(ws, EPOCH, keep_promoted_only=True, dry_run=False)
    assert not (gen_dir / "snapshot").exists()
    assert (gen_dir / "experiment.json").is_file()
    assert (gen_dir / "patches").is_dir()


def test_git_backend_removes_tag_and_worktree(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "config.json").write_text(json.dumps({"storage_backend": "git"}), encoding="utf-8")
    store = _seed_lineage(ws)
    # Materialise v1's worktree (as a tournament round would have).
    v1_worktree = store.snapshot_root(EPOCH, "v1")
    assert v1_worktree.is_dir()

    report = prune_generations(ws, EPOCH, keep_promoted_only=True, dry_run=False)
    assert "v1" in report.pruned
    assert not v1_worktree.exists()
    assert not store.has_generation(EPOCH, "v1")
    assert "v1" not in store.list_generations(EPOCH)
    # The kept promoted head still reads back from its commit.
    assert b"v3" in store.read_file(EPOCH, "v3", "agent/prompts.py")


# ---------------------------------------------------------------------------
# Dashboard degradation — pruned generations must degrade, never raise
# ---------------------------------------------------------------------------


def test_dashboard_views_degrade_gracefully_for_pruned_generation(workspace: Path) -> None:
    from zicato.dashboard.filetree import (
        build_file_index,
        build_generation_diff,
        build_generation_patches,
        build_generation_tree,
        read_generation_file,
    )
    from zicato.query import WorkspacePaths

    _seed_lineage(workspace)
    prune_generations(workspace, EPOCH, keep_promoted_only=True, dry_run=False)
    paths = WorkspacePaths(workspace)

    # The index never raises; kept generations still enumerate.
    index = build_file_index(paths)
    listed = {
        g["generation_id"]
        for e in index["epochs"]
        if e["epoch_id"] == EPOCH
        for g in e["generations"]
    }
    assert {"v0", "v3", "v4"} <= listed

    # Tree view: explicit error + empty entries, not a raise.
    tree = build_generation_tree(paths, EPOCH, "v1")
    assert tree["entries"] == []
    assert "error" in tree

    # File content: explicit error, not a raise.
    content = read_generation_file(paths, EPOCH, "v1", "agent/prompts.py")
    assert "error" in content

    # Patch metadata SURVIVES pruning — it lives in the journal record
    # (directory backend reads it directly; the git backend falls back
    # to the same record once the tag is gone).
    patches = build_generation_patches(paths, EPOCH, "v1")
    assert patches.get("error") is None
    assert [p["id"] for p in patches["patches"]] == ["p_v1"]

    # Diff view on the pruned generation: explicit error, not a raise.
    diff = build_generation_diff(paths, EPOCH, "v1")
    assert diff["files"] == []
    assert "error" in diff

    # Diff view on a KEPT generation still works after the sweep.
    kept_diff = build_generation_diff(paths, EPOCH, "v3")
    assert "error" not in kept_diff


def test_dashboard_mutation_index_survives_pruned_generations(workspace: Path) -> None:
    """The mutation-site browser still attributes a PRUNED generation's patch.

    ``build_mutation_index`` enumerates the baseline surface (``v0`` is
    never pruned) and, per site, the generations whose patch set touched
    it — read through ``list_patches``, whose record survives pruning on
    both backends (per-patch JSON files / journal fallback).
    """
    from zicato.dashboard.mutations import build_mutation_index
    from zicato.query import WorkspacePaths

    _seed_lineage(workspace)
    prune_generations(workspace, EPOCH, keep_promoted_only=True, dry_run=False)

    payload = build_mutation_index(WorkspacePaths(workspace), EPOCH)
    assert payload.get("error") is None
    sites = {m["mutation_id"]: m for m in payload["mutations"]}
    assert "instr" in sites
    patched_by = {g["generation_id"] for g in sites["instr"].get("patched_by", [])}
    # The KEPT generations always attribute their patches to the site.
    assert "v4" in patched_by
    backend = json.loads((workspace / "config.json").read_text(encoding="utf-8"))["storage_backend"]
    if backend == "directory":
        # Directory backend: pruned generations still ENUMERATE (their
        # record directories survive) and their per-patch JSON records
        # keep the attribution alive.
        assert {"v1", "v2"} <= patched_by
    else:
        # Git backend: a pruned generation's tag is gone, so it drops
        # out of the store's enumeration — the view degrades to
        # omitting it (its history stays in the journal/lineage views,
        # which never read the genstore).
        assert {"v1", "v2"}.isdisjoint(patched_by)


# ---------------------------------------------------------------------------
# CLI — zicato epoch gc
# ---------------------------------------------------------------------------


def test_cli_gc_dry_run_default_and_apply(workspace: Path) -> None:
    from zicato.cli.commands.epoch import epoch_grp

    store = _seed_lineage(workspace)
    runner = CliRunner()

    result = runner.invoke(
        epoch_grp,
        ["gc", EPOCH, "--workspace", str(workspace), "--keep-promoted-only"],
    )
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "would prune" in result.output
    assert store.has_generation(EPOCH, "v1")

    result = runner.invoke(
        epoch_grp,
        ["gc", EPOCH, "--workspace", str(workspace), "--keep-promoted-only", "--apply"],
    )
    assert result.exit_code == 0, result.output
    assert "DRY RUN" not in result.output
    assert not store.has_generation(EPOCH, "v1")


def test_cli_gc_requires_exactly_one_policy(workspace: Path) -> None:
    from zicato.cli.commands.epoch import epoch_grp

    _seed_lineage(workspace)
    runner = CliRunner()
    for args in (
        ["gc", EPOCH, "--workspace", str(workspace)],
        ["gc", EPOCH, "--workspace", str(workspace), "--keep-last", "2", "--keep-promoted-only"],
    ):
        result = runner.invoke(epoch_grp, args)
        assert result.exit_code != 0
        assert "exactly one" in result.output


# ---------------------------------------------------------------------------
# Epoch-close hook — additive config knob, default OFF
# ---------------------------------------------------------------------------


def test_close_hook_off_by_default(workspace: Path) -> None:
    store = _seed_lineage(workspace)
    assert maybe_prune_on_epoch_close(workspace, EPOCH) is None
    assert store.has_generation(EPOCH, "v1")


def test_close_hook_prunes_when_opted_in(workspace: Path) -> None:
    store = _seed_lineage(workspace)
    config = json.loads((workspace / "config.json").read_text(encoding="utf-8"))
    config[STORAGE_GC_KEY] = {"on_epoch_close": True, "keep_promoted_only": True}
    (workspace / "config.json").write_text(json.dumps(config), encoding="utf-8")

    report = maybe_prune_on_epoch_close(workspace, EPOCH)
    assert report is not None
    assert report.pruned == ("v1", "v2")
    assert not store.has_generation(EPOCH, "v1")


def test_close_hook_ignores_malformed_config(workspace: Path) -> None:
    store = _seed_lineage(workspace)
    config = json.loads((workspace / "config.json").read_text(encoding="utf-8"))
    config[STORAGE_GC_KEY] = {"on_epoch_close": True, "keep_last_n": "three"}
    (workspace / "config.json").write_text(json.dumps(config), encoding="utf-8")
    assert maybe_prune_on_epoch_close(workspace, EPOCH) is None
    assert store.has_generation(EPOCH, "v1")


def test_close_epoch_runs_the_hook(tmp_path: Path) -> None:
    """The full ``close_epoch`` path drives the opt-in prune end-to-end."""
    from zicato.epoch import lifecycle

    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "config.json").write_text("{}", encoding="utf-8")
    board = tmp_path / "board.jsonl"
    board.write_text(
        '{"id": "e1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "hi"}\n',
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# brief\n", encoding="utf-8")

    from zicato.core.types import ScoringWeights

    cfg = lifecycle.new_epoch(ws, "gc-hook", board, brief, ScoringWeights())

    # Build a decided lineage under the REAL epoch id, git backend
    # (the workspace default), with the knob enabled.
    store = default_generation_store(ws)
    tree = tmp_path / "registered" / "agent"
    _write(
        tree / "prompts.py",
        '''
        # zicato:mutable id="instr"
        INSTR = """seed"""
        ''',
    )
    store.seed_generation(cfg.id, "v0", [tree])
    gen0 = Generation(
        id="v0",
        epoch_id=cfg.id,
        parent_id=None,
        snapshot_root=ws / "unused",
        created_at="2026-07-01T00:00:00+00:00",
        promoted=True,
    )
    append_to_lineage(ws, cfg.id, gen0, None)
    store.derive_generation(cfg.id, "v0", "v1", [_patch("p_v1", '"""v1"""')])
    gen1 = Generation(
        id="v1",
        epoch_id=cfg.id,
        parent_id="v0",
        snapshot_root=ws / "unused",
        created_at="2026-07-01T00:00:00+00:00",
        promoted=False,
    )
    append_to_lineage(ws, cfg.id, gen1, "v0")

    config = json.loads((ws / "config.json").read_text(encoding="utf-8"))
    config[STORAGE_GC_KEY] = {"on_epoch_close": True, "keep_promoted_only": True}
    (ws / "config.json").write_text(json.dumps(config), encoding="utf-8")

    lifecycle.close_epoch(ws, epoch_id=cfg.id)
    assert not store.has_generation(cfg.id, "v1")
    assert store.has_generation(cfg.id, "v0")
