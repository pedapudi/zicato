"""Tests for the dashboard mutation-site browser — :mod:`zicato.dashboard.mutations`.

The mutation-site browser extends the Files view: it lists every
``# zicato:mutable`` annotated span in an epoch's ``v0`` baseline and,
per site, exposes the baseline content plus the patched content in any
generation whose patch touched that mutation id — the frontend diffs the
two. These tests exercise it end-to-end through the ASGI app on the
directory backend only, exactly like the file-tree browser it sits
beside: the endpoints read exclusively through the ``GenerationStore``
protocol, whose cross-backend behaviour is pinned by
``tests/test_genstore_conformance.py``, so a git axis here added spawn
cost without new coverage.
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.core.types import Experiment, HypothesisSpec, Patch
from zicato.dashboard.mutations import RECORDS_CAPTION, UNREACHABLE_CAPTION
from zicato.dashboard.server import create_app
from zicato.epoch.genstore import DirectoryGenerationStore
from zicato.epoch.journal import write_experiment
from zicato.mutation.enumerator import enumerate_mutations


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _mutable_tree(root: Path, *, instr: str = "original instruction") -> Path:
    """A source tree with two ``# zicato:mutable`` spans."""
    tree = root / "agent"
    _write(
        tree / "prompts.py",
        f'''
        # zicato:mutable id="researcher_instr" role="system_instruction"
        RESEARCHER = """{instr}"""

        # zicato:mutable id="reviewer_instr" role="system_instruction"
        REVIEWER = """review carefully"""
        ''',
    )
    return tree


def _patch(pid: str, mutation_id: str, new_content: str) -> Patch:
    return Patch(
        id=pid,
        mutation_id=mutation_id,
        op="replace",
        new_content=new_content,
        new_numeric=None,
        new_enum=None,
        rationale="dashboard mutation-browser test",
    )


def _experiment(patches: tuple[Patch, ...]) -> Experiment:
    return Experiment(
        id="exp_e1_v1",
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-05-18T00:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea="rewrite the researcher instruction",
            modulating=("researcher_instr",),
            why="test fixture",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.0",
        ),
        patches=patches,
        outcome=None,
    )


@pytest.fixture
def populated_workspace(tmp_path: Path) -> Path:
    """A ``.zicato/`` workspace with a seeded + patched generation.

    Directory backend only (backend-agnostic endpoints; see the module
    docstring). ``v1`` is derived from ``v0`` by a patch against
    ``researcher_instr``; ``reviewer_instr`` is left untouched so the
    "site with no patch" path is also covered. The ``experiment.json``
    record is written so :meth:`list_patches` — the seam the mutation
    browser reads patch sets through — resolves the patch set for ``v1``.
    The ``config.json`` pin is still required: the dashboard reads through
    ``default_generation_store``, which defaults to git, so the workspace
    must declare the backend it was seeded under.
    """
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "epochs" / "e1").mkdir(parents=True)

    (ws / "config.json").write_text('{"storage_backend": "directory"}', encoding="utf-8")
    store = DirectoryGenerationStore(ws)

    tree = _mutable_tree(tmp_path / "src", instr="original instruction")
    store.seed_generation("e1", "v0", [tree])
    patch = _patch("p1", "researcher_instr", '"""rewritten instruction"""')
    store.derive_generation("e1", "v0", "v1", [patch])
    write_experiment(ws, "e1", "v1", _experiment((patch,)))
    return ws


@pytest.fixture
def client(populated_workspace: Path, tmp_path: Path) -> TestClient:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    app = create_app(populated_workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# /api/mutations/{epoch} — the mutation-site index
# ---------------------------------------------------------------------------


def test_mutation_index_lists_sites(client: TestClient) -> None:
    resp = client.get("/api/mutations/e1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["epoch_id"] == "e1"
    ids = {m["mutation_id"] for m in body["mutations"]}
    assert ids == {"researcher_instr", "reviewer_instr"}


def test_mutation_index_carries_file_and_role(client: TestClient) -> None:
    body = client.get("/api/mutations/e1").json()
    sites = {m["mutation_id"]: m for m in body["mutations"]}
    researcher = sites["researcher_instr"]
    assert researcher["file"] == "agent/prompts.py"
    assert researcher["role"] == "system_instruction"
    assert researcher["line_start"] >= 1


def test_mutation_index_flags_patched_site(client: TestClient) -> None:
    """Only the patched site reports a patching generation."""
    body = client.get("/api/mutations/e1").json()
    sites = {m["mutation_id"]: m for m in body["mutations"]}
    assert sites["researcher_instr"]["patched_generation_ids"] == ["v1"]
    assert sites["reviewer_instr"]["patched_generation_ids"] == []


def test_mutation_index_missing_epoch_degrades(client: TestClient) -> None:
    resp = client.get("/api/mutations/no_such_epoch")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mutations"] == []
    assert "error" in body


def test_mutation_index_rejects_unsafe_id(client: TestClient) -> None:
    resp = client.get("/api/mutations/..%2f..%2fetc")
    assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# /api/mutations/{epoch}/{mutation_id} — one site's baseline + diffs
# ---------------------------------------------------------------------------


def test_mutation_detail_baseline_content(client: TestClient) -> None:
    body = client.get("/api/mutations/e1/researcher_instr").json()
    assert body["mutation_id"] == "researcher_instr"
    assert "original instruction" in body["baseline"]["content"]
    assert body["baseline"]["generation_id"] == "v0"


def test_mutation_detail_patched_version(client: TestClient) -> None:
    """The patched site exposes v1's rewritten content for the diff."""
    body = client.get("/api/mutations/e1/researcher_instr").json()
    versions = body["versions"]
    assert len(versions) == 1
    v1 = versions[0]
    assert v1["generation_id"] == "v1"
    assert "rewritten instruction" in v1["content"]
    assert "original instruction" not in v1["content"]
    assert v1["op"] == "replace"
    assert v1["rationale"]


def test_mutation_detail_unpatched_site_has_no_versions(client: TestClient) -> None:
    """A site no patch touched shows its baseline content, no versions."""
    body = client.get("/api/mutations/e1/reviewer_instr").json()
    assert body["versions"] == []
    assert "review carefully" in body["baseline"]["content"]


def test_mutation_detail_unknown_id_has_error(client: TestClient) -> None:
    resp = client.get("/api/mutations/e1/ghost_id")
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_mutation_detail_rejects_unsafe_id(client: TestClient) -> None:
    resp = client.get("/api/mutations/e1/..%2f..%2fetc")
    assert resp.status_code in (400, 404)


def test_mutation_detail_baseline_differs_from_patched(client: TestClient) -> None:
    """The endpoint exposes enough for a real diff: the two contents differ."""
    body = client.get("/api/mutations/e1/researcher_instr").json()
    baseline = body["baseline"]["content"]
    patched = body["versions"][0]["content"]
    assert baseline != patched


# ---------------------------------------------------------------------------
# Records-first: the surface outlives the tree (issue #194 §6)
# ---------------------------------------------------------------------------
#
# Snapshot GC prunes generation source trees and keeps every record. The
# repro these pin: prune the trees of a real, fully-recorded epoch and the
# whole mutation browser used to render EMPTY, while ``mutations.json`` and
# the patch records sat unread beside it.


def _record_surface(ws: Path, epoch_id: str, roots: list[Path]) -> None:
    """Write the epoch's ``mutations.json`` the way an evolve round does.

    Through the orchestrator's own writer, not a hand-rolled dict: the
    dashboard reads what that writer produces, and a fixture that spelled
    the record itself could keep passing while the two drifted apart.
    """
    from zicato.evolve.round_baseline import _dump_mutations_snapshot

    _dump_mutations_snapshot(ws, epoch_id, list(enumerate_mutations(roots)))


def _prune_trees(ws: Path, epoch_id: str, *generation_ids: str) -> None:
    """Remove generations' source trees, keeping every record — like GC."""
    for generation_id in generation_ids:
        shutil.rmtree(ws / "epochs" / epoch_id / "generations" / generation_id / "snapshot")


@pytest.fixture
def pruned_workspace(populated_workspace: Path) -> Path:
    """``populated_workspace`` after snapshot GC took every tree."""
    store = DirectoryGenerationStore(populated_workspace)
    _record_surface(populated_workspace, "e1", [Path(store.snapshot_root("e1", "v0"))])
    _prune_trees(populated_workspace, "e1", "v0", "v1")
    return populated_workspace


@pytest.fixture
def pruned_client(pruned_workspace: Path, tmp_path: Path) -> TestClient:
    static_dir = tmp_path / "static-pruned"
    static_dir.mkdir()
    app = create_app(pruned_workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        yield c


def test_mutation_index_survives_pruned_trees(pruned_client: TestClient) -> None:
    """The repro: every tree gone, the full site index still renders."""
    body = pruned_client.get("/api/mutations/e1").json()
    ids = {m["mutation_id"] for m in body["mutations"]}
    assert ids == {"researcher_instr", "reviewer_instr"}
    assert "error" not in body
    sites = {m["mutation_id"]: m for m in body["mutations"]}
    assert sites["researcher_instr"]["file"] == "agent/prompts.py"
    assert sites["researcher_instr"]["patched_generation_ids"] == ["v1"]


def test_mutation_index_flags_record_provenance(pruned_client: TestClient) -> None:
    """A reconstructed surface says so, in the words the view renders."""
    body = pruned_client.get("/api/mutations/e1").json()
    assert body["provenance"] == "records"
    assert body["provenance_note"] == RECORDS_CAPTION


def test_mutation_index_keeps_snapshot_provenance_when_tree_present(client: TestClient) -> None:
    """The tree is exactly faithful, so it stays the path — and says nothing."""
    body = client.get("/api/mutations/e1").json()
    assert body["provenance"] == "snapshot"
    assert body["provenance_note"] == ""


def test_mutation_index_columns_include_pruned_generations(pruned_client: TestClient) -> None:
    """A generation whose tree is gone keeps its column in the matrix."""
    body = pruned_client.get("/api/mutations/e1").json()
    assert body["generations"] == ["v0", "v1"]


def test_mutation_detail_reconstructs_patched_content(pruned_client: TestClient) -> None:
    """A pruned challenger's patched content comes back from its patch record."""
    body = pruned_client.get("/api/mutations/e1/researcher_instr").json()
    version = body["versions"][0]
    assert version["generation_id"] == "v1"
    assert version["provenance"] == "records"
    assert "rewritten instruction" in version["content"]
    assert body["provenance_note"] == RECORDS_CAPTION


def test_mutation_detail_declines_to_name_a_records_baseline(pruned_client: TestClient) -> None:
    """The frozen enumeration is the round's champion — not provably ``v0``."""
    body = pruned_client.get("/api/mutations/e1/researcher_instr").json()
    assert body["baseline"]["generation_id"] is None
    assert body["baseline"]["provenance"] == "records"
    assert "original instruction" in body["baseline"]["content"]


def test_mutation_detail_names_v0_when_the_tree_answered(client: TestClient) -> None:
    body = client.get("/api/mutations/e1/researcher_instr").json()
    assert body["baseline"]["generation_id"] == "v0"
    assert body["baseline"]["provenance"] == "snapshot"
    assert body["versions"][0]["provenance"] == "snapshot"
    assert body["provenance_note"] == ""


def test_mutation_index_names_an_unreachable_tree_as_such(
    populated_workspace: Path, tmp_path: Path
) -> None:
    """A tree the STORE cannot reach is not a pruned tree, and does not say so.

    The condition a real June-dead workspace arrives in: directory-shaped
    snapshots on disk, but the declared backend looks somewhere else. The
    records still answer; claiming GC pruned trees the operator can see
    would be a guess.
    """
    store = DirectoryGenerationStore(populated_workspace)
    _record_surface(populated_workspace, "e1", [Path(store.snapshot_root("e1", "v0"))])
    (populated_workspace / "config.json").write_text('{"storage_backend": "git"}', encoding="utf-8")
    static_dir = tmp_path / "static-unreachable"
    static_dir.mkdir()
    with TestClient(create_app(populated_workspace, static_dir, read_only=True)) as c:
        body = c.get("/api/mutations/e1").json()
    assert len(body["mutations"]) == 2
    assert body["provenance_note"] == UNREACHABLE_CAPTION


def test_mutation_index_with_neither_tree_nor_record_names_both(
    pruned_workspace: Path, tmp_path: Path
) -> None:
    (pruned_workspace / "epochs" / "e1" / "mutations.json").unlink()
    static_dir = tmp_path / "static-empty"
    static_dir.mkdir()
    with TestClient(create_app(pruned_workspace, static_dir, read_only=True)) as c:
        body = c.get("/api/mutations/e1").json()
    assert body["mutations"] == []
    assert "no v0 source tree" in body["error"]
    assert "no mutations.json record" in body["error"]


# ---------------------------------------------------------------------------
# set_numeric / set_enum reconstruction
# ---------------------------------------------------------------------------
#
# A ``replace`` record IS the new content. A ``set_numeric`` / ``set_enum``
# record carries a VALUE the applier wrote into a constant, so reconstructing
# means substituting it into the baseline span — and the test of "faithful"
# is the applier's own output, captured from the tree before it is pruned.


def _value_patch(pid: str, mutation_id: str, op: str, **payload: object) -> Patch:
    return Patch(
        id=pid,
        mutation_id=mutation_id,
        op=op,  # type: ignore[arg-type]
        new_content=payload.get("new_content"),  # type: ignore[arg-type]
        new_numeric=payload.get("new_numeric"),  # type: ignore[arg-type]
        new_enum=payload.get("new_enum"),  # type: ignore[arg-type]
        rationale="value-op reconstruction test",
    )


def _value_workspace(tmp_path: Path, source: str, patch: Patch) -> Path:
    """Seed ``v0`` from ``source``, derive ``v1`` with ``patch``, record both."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "epochs" / "e1").mkdir(parents=True)
    (ws / "config.json").write_text('{"storage_backend": "directory"}', encoding="utf-8")
    store = DirectoryGenerationStore(ws)
    tree = tmp_path / "src" / "agent"
    _write(tree / "knobs.py", source)
    store.seed_generation("e1", "v0", [tree])
    store.derive_generation("e1", "v0", "v1", [patch])
    write_experiment(ws, "e1", "v1", _experiment((patch,)))
    _record_surface(ws, "e1", [Path(store.snapshot_root("e1", "v0"))])
    return ws


def _detail(ws: Path, tmp_path: Path, mutation_id: str) -> dict:
    static_dir = tmp_path / f"static-{mutation_id}"
    static_dir.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(ws, static_dir, read_only=True)) as c:
        return c.get(f"/api/mutations/e1/{mutation_id}").json()


def test_set_enum_reconstruction_matches_the_applier(tmp_path: Path) -> None:
    ws = _value_workspace(
        tmp_path,
        """
        # zicato:mutable id="strategy"
        STRATEGY = "greedy"
        """,
        _value_patch("p1", "strategy", "set_enum", new_enum="balanced"),
    )
    from_tree = _detail(ws, tmp_path, "strategy")["versions"][0]
    assert from_tree["provenance"] == "snapshot"

    _prune_trees(ws, "e1", "v0", "v1")
    from_records = _detail(ws, tmp_path / "b", "strategy")["versions"][0]
    assert from_records["provenance"] == "records"
    assert from_records["content"] == from_tree["content"]
    assert "balanced" in from_records["content"]


def test_set_numeric_reconstruction_matches_the_applier(tmp_path: Path) -> None:
    """The constant sits INSIDE the enumerated span — reconstructable exactly."""
    ws = _value_workspace(
        tmp_path,
        """
        # zicato:mutable id="threshold"
        THRESHOLD = ("promote margin", 0.85)
        """,
        _value_patch("p1", "threshold", "set_numeric", new_numeric=0.42),
    )
    from_tree = _detail(ws, tmp_path, "threshold")["versions"][0]
    assert "0.42" in from_tree["content"]

    _prune_trees(ws, "e1", "v0", "v1")
    from_records = _detail(ws, tmp_path / "b", "threshold")["versions"][0]
    assert from_records["content"] == from_tree["content"]


def test_value_reconstruction_names_the_text_it_substituted_into(tmp_path: Path) -> None:
    """A value op's reconstruction carries v0's text, and must say so.

    ``set_numeric`` / ``set_enum`` record a VALUE, so reconstructing means
    writing it into the baseline span. What a generation BETWEEN v0 and
    this one wrote at the site is therefore absent, and the entry names
    the generation whose text is actually on screen. A ``replace`` record
    carries its own text and takes no such flag.
    """
    ws = _value_workspace(
        tmp_path,
        """
        # zicato:mutable id="strategy"
        STRATEGY = "greedy"
        """,
        _value_patch("p1", "strategy", "set_enum", new_enum="balanced"),
    )
    assert "reconstructed_against" not in _detail(ws, tmp_path, "strategy")["versions"][0]

    _prune_trees(ws, "e1", "v0", "v1")
    assert _detail(ws, tmp_path / "b", "strategy")["versions"][0]["reconstructed_against"] == "v0"


def test_replace_reconstruction_carries_its_own_text(tmp_path: Path) -> None:
    """A ``replace`` record IS the content, so nothing was substituted into."""
    ws = _value_workspace(
        tmp_path,
        """
        # zicato:mutable id="strategy"
        STRATEGY = "greedy"
        """,
        _value_patch("p1", "strategy", "replace", new_content='STRATEGY = "balanced"'),
    )
    _prune_trees(ws, "e1", "v0", "v1")
    version = _detail(ws, tmp_path, "strategy")["versions"][0]
    assert version["provenance"] == "records"
    assert "reconstructed_against" not in version


def test_set_numeric_outside_the_span_says_so(tmp_path: Path) -> None:
    """The applier's target can sit outside the span; then there is nothing to show."""
    ws = _value_workspace(
        tmp_path,
        """
        # zicato:mutable id="threshold"
        THRESHOLD_DOC = "the promote margin"
        THRESHOLD = 0.85
        """,
        _value_patch("p1", "threshold", "set_numeric", new_numeric=0.42),
    )
    _prune_trees(ws, "e1", "v0", "v1")
    version = _detail(ws, tmp_path, "threshold")["versions"][0]
    assert version["content"] is None
    assert "0.42" in version["note"]
    assert "outside the recorded span" in version["note"]


def test_records_path_reports_the_site_file_relative(pruned_workspace: Path) -> None:
    """The record stores an absolute path from a tree that no longer exists.

    Even after the workspace is MOVED, the rendered location stays the
    repo-relative path the file-tree browser shows — the recorded prefix is
    not resolved against anything that has to still be there.
    """
    moved = pruned_workspace.parent.parent / "moved" / ".zicato"
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(pruned_workspace, moved)
    recorded = json.loads((moved / "epochs" / "e1" / "mutations.json").read_text())
    assert str(recorded[0]["file"]).startswith(str(pruned_workspace))  # stale, by design

    from zicato.dashboard.mutations import build_mutation_index
    from zicato.query import WorkspacePaths

    body = build_mutation_index(WorkspacePaths(moved), "e1")
    assert {m["file"] for m in body["mutations"]} == {"agent/prompts.py"}
