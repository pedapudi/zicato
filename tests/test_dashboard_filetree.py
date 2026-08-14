"""Tests for the dashboard Files view — :mod:`zicato.dashboard.filetree`.

The Files view browses every generation's source tree and applied
patches through the :class:`~zicato.epoch.genstore.GenerationStore`
seam. These tests exercise it end-to-end through the ASGI app on the
directory backend only: the endpoints read exclusively through the
``GenerationStore`` protocol, whose cross-backend behaviour is pinned by
``tests/test_genstore_conformance.py`` (and the git-specific mapping by
``tests/test_git_genstore.py``), so re-running every endpoint-shape test
against the git backend added spawn cost without new coverage. The
config-driven backend *selection* the endpoints rely on is covered in
``tests/test_git_genstore.py``'s config-knob tests.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.core.types import Experiment, HypothesisSpec, Patch
from zicato.dashboard.mutations import SPANS_CAPTION
from zicato.dashboard.server import create_app
from zicato.epoch.genstore import DirectoryGenerationStore


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _mutable_tree(root: Path, *, instr: str = "original") -> Path:
    tree = root / "agent"
    _write(
        tree / "prompts.py",
        f'''
        # zicato:mutable id="instr"
        INSTR = """{instr}"""
        ''',
    )
    _write(tree / "lib" / "util.py", "X = 1\n")
    return tree


def _patch(pid: str, new_content: str) -> Patch:
    return Patch(
        id=pid,
        mutation_id="instr",
        op="replace",
        new_content=new_content,
        new_numeric=None,
        new_enum=None,
        rationale="dashboard test",
    )


@pytest.fixture
def populated_workspace(tmp_path: Path) -> Path:
    """A ``.zicato/`` workspace with a seeded + derived generation.

    Directory backend only — the endpoints under test are backend-agnostic
    (they read through the ``GenerationStore`` protocol, held to the same
    contract for both backends by the conformance suite), so the git axis
    here bought no coverage. The ``config.json`` pin is still required: the
    dashboard reads through ``default_generation_store``, which defaults to
    git, so the workspace must declare the backend it was seeded under.
    """
    ws = tmp_path / ".zicato"
    ws.mkdir()
    # An epoch directory must exist for the filetree index to list it.
    (ws / "epochs" / "e1").mkdir(parents=True)

    (ws / "config.json").write_text('{"storage_backend": "directory"}', encoding="utf-8")
    store = DirectoryGenerationStore(ws)

    tree = _mutable_tree(tmp_path / "src", instr="original")
    store.seed_generation("e1", "v0", [tree])
    store.derive_generation("e1", "v0", "v1", [_patch("p1", '"""rewritten"""')])
    return ws


@pytest.fixture
def client(populated_workspace: Path, tmp_path: Path) -> TestClient:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    app = create_app(populated_workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# /api/files — the epoch/generation index
# ---------------------------------------------------------------------------


def test_files_index_lists_generations(client: TestClient) -> None:
    resp = client.get("/api/files")
    assert resp.status_code == 200
    body = resp.json()
    epochs = {e["epoch_id"]: e for e in body["epochs"]}
    assert "e1" in epochs
    gens = {g["generation_id"]: g for g in epochs["e1"]["generations"]}
    assert set(gens) == {"v0", "v1"}
    # Each generation reports a non-zero file count.
    assert gens["v0"]["file_count"] >= 2
    assert gens["v1"]["file_count"] >= 2


# ---------------------------------------------------------------------------
# /api/files/{epoch}/{gen}/tree — the source tree
# ---------------------------------------------------------------------------


def test_files_tree_lists_source(client: TestClient) -> None:
    resp = client.get("/api/files/e1/v0/tree")
    assert resp.status_code == 200
    body = resp.json()
    paths = {e["path"] for e in body["entries"]}
    assert "agent/prompts.py" in paths
    assert "agent/lib/util.py" in paths


def test_files_tree_missing_generation_degrades(client: TestClient) -> None:
    resp = client.get("/api/files/e1/v99/tree")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert "error" in body


def test_files_tree_rejects_unsafe_id(client: TestClient) -> None:
    resp = client.get("/api/files/e1/..%2f..%2fetc/tree")
    # The path-param router or the id check rejects it.
    assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# /api/files/{epoch}/{gen}/content — file content
# ---------------------------------------------------------------------------


def test_files_content_returns_file(client: TestClient) -> None:
    resp = client.get("/api/files/e1/v0/content", params={"path": "agent/prompts.py"})
    assert resp.status_code == 200
    body = resp.json()
    assert "original" in body["content"]
    assert body["binary"] is False
    assert body["truncated"] is False


def test_files_content_reflects_patch(client: TestClient) -> None:
    """v1's prompts.py shows the rewritten content, v0's the original."""
    v1 = client.get("/api/files/e1/v1/content", params={"path": "agent/prompts.py"}).json()
    assert "rewritten" in v1["content"]
    v0 = client.get("/api/files/e1/v0/content", params={"path": "agent/prompts.py"}).json()
    assert "original" in v0["content"]


def test_files_content_missing_path_is_400(client: TestClient) -> None:
    resp = client.get("/api/files/e1/v0/content")
    assert resp.status_code == 400


def test_files_content_traversal_is_rejected(client: TestClient) -> None:
    resp = client.get("/api/files/e1/v0/content", params={"path": "../../../etc/passwd"})
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_files_content_missing_file_has_error(client: TestClient) -> None:
    resp = client.get("/api/files/e1/v0/content", params={"path": "agent/ghost.py"})
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# /api/files/{epoch}/{gen}/patches — the applied patch set
# ---------------------------------------------------------------------------


def test_files_patches_seed_is_empty(client: TestClient) -> None:
    resp = client.get("/api/files/e1/v0/patches")
    assert resp.status_code == 200
    assert resp.json()["patches"] == []


# ---------------------------------------------------------------------------
# /api/files/{epoch}/{gen}/diff — the per-generation changed-files diff
# ---------------------------------------------------------------------------


def test_files_diff_reports_modified_file(client: TestClient) -> None:
    """v1's diff vs its v0 parent lists only the file the patch changed."""
    resp = client.get("/api/files/e1/v1/diff")
    assert resp.status_code == 200
    body = resp.json()
    assert body["generation_id"] == "v1"
    # The parent is resolved — either from experiment.json or the v(N-1)
    # / v0 fallback. For this fixture it is the v0 seed.
    assert body["parent_generation_id"] == "v0"
    changed = {f["path"]: f for f in body["files"]}
    # Only prompts.py was rewritten; the untouched util.py is NOT listed.
    assert set(changed) == {"agent/prompts.py"}
    diff = changed["agent/prompts.py"]
    assert diff["status"] == "modified"
    assert "original" in diff["old_content"]
    assert "rewritten" in diff["new_content"]
    assert diff["old_binary"] is False
    assert diff["new_binary"] is False


def test_files_diff_seed_lists_every_file_as_added(client: TestClient) -> None:
    """The v0 seed has no parent — every file reads as added."""
    resp = client.get("/api/files/e1/v0/diff")
    assert resp.status_code == 200
    body = resp.json()
    assert body["parent_generation_id"] is None
    statuses = {f["path"]: f["status"] for f in body["files"]}
    assert statuses == {"agent/prompts.py": "added", "agent/lib/util.py": "added"}
    # An added file has empty old content and non-empty new content.
    for f in body["files"]:
        assert f["old_content"] == ""
        assert f["new_content"] != ""


def test_files_diff_missing_generation_degrades(client: TestClient) -> None:
    resp = client.get("/api/files/e1/v99/diff")
    assert resp.status_code == 200
    body = resp.json()
    assert body["files"] == []
    assert "error" in body


def test_files_diff_rejects_unsafe_id(client: TestClient) -> None:
    resp = client.get("/api/files/e1/..%2f..%2fetc/diff")
    assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# When the tree is gone (issue #194 §6)
# ---------------------------------------------------------------------------
#
# Snapshot GC prunes generation source trees and keeps every record. Whole-tree
# browsing cannot come back; the spans the generation's patches touched can,
# and each response says which of the two it is handing over.


def _prune_tree(ws: Path, epoch_id: str, generation_id: str) -> None:
    shutil.rmtree(ws / "epochs" / epoch_id / "generations" / generation_id / "snapshot")


def _record_experiment(ws: Path, generation_id: str, parent_id: str, patch: Patch) -> None:
    """Write the generation's ``experiment.json`` — the record GC never touches.

    ``derive_generation`` materialises the tree; the journal records the
    lineage edge and the patch set. The base fixture only needs the tree, so
    the record is written by the tests that ask what survives without one.
    """
    from zicato.epoch.journal import write_experiment

    write_experiment(
        ws,
        "e1",
        generation_id,
        Experiment(
            id=f"exp_e1_{generation_id}",
            epoch_id="e1",
            generation_id=generation_id,
            parent_generation_id=parent_id,
            proposed_at="2026-05-18T00:00:00Z",
            hypothesis=HypothesisSpec(
                core_idea="rewrite the instruction",
                modulating=("instr",),
                why="test fixture",
                expected_drift_movements=(),
                expected_pass_rate_delta="+0.0",
            ),
            patches=(patch,),
            outcome=None,
        ),
    )


def test_files_index_flags_a_pruned_tree(populated_workspace: Path, tmp_path: Path) -> None:
    """``file_count: 0`` alone cannot tell "pruned" from "empty"; ``has_tree`` can."""
    _record_experiment(populated_workspace, "v1", "v0", _patch("p1", '"""rewritten"""'))
    _prune_tree(populated_workspace, "e1", "v1")
    static_dir = tmp_path / "static-pruned"
    static_dir.mkdir()
    with TestClient(create_app(populated_workspace, static_dir, read_only=True)) as c:
        body = c.get("/api/files").json()
    gens = {g["generation_id"]: g for g in body["epochs"][0]["generations"]}
    assert gens["v0"]["has_tree"] is True
    assert gens["v1"]["has_tree"] is False
    assert gens["v1"]["file_count"] == 0
    # The patch record is untouched by GC — that is the whole point of it.
    assert gens["v1"]["patch_count"] == 1


def test_files_tree_names_gc_and_points_at_the_records(
    populated_workspace: Path, tmp_path: Path
) -> None:
    """A collected tree is named as one, with the surviving records listed."""
    _record_experiment(populated_workspace, "v1", "v0", _patch("p1", '"""rewritten"""'))
    _prune_tree(populated_workspace, "e1", "v1")
    static_dir = tmp_path / "static-tree"
    static_dir.mkdir()
    with TestClient(create_app(populated_workspace, static_dir, read_only=True)) as c:
        body = c.get("/api/files/e1/v1/tree").json()
    assert body["entries"] == []
    assert "pruned by snapshot GC" in body["error"]
    assert "/api/mutations/e1" in body["error"]


def test_files_tree_unknown_generation_is_not_called_pruned(client: TestClient) -> None:
    """A coordinate that names nothing must not be blamed on GC."""
    body = client.get("/api/files/e1/v99/tree").json()
    assert body["entries"] == []
    assert body["error"] == "no generation e1/v99 in this workspace"


def test_files_diff_reconstructs_spans_for_a_pruned_generation(
    populated_workspace: Path, tmp_path: Path
) -> None:
    """The diff's real subject — what the patches changed — survives the prune."""
    from zicato.evolve.round_baseline import _dump_mutations_snapshot
    from zicato.mutation.enumerator import enumerate_mutations

    store = DirectoryGenerationStore(populated_workspace)
    _dump_mutations_snapshot(
        populated_workspace,
        "e1",
        list(enumerate_mutations([Path(store.snapshot_root("e1", "v0"))])),
    )
    _record_experiment(populated_workspace, "v1", "v0", _patch("p1", '"""rewritten"""'))
    _prune_tree(populated_workspace, "e1", "v1")

    static_dir = tmp_path / "static-spans"
    static_dir.mkdir()
    with TestClient(create_app(populated_workspace, static_dir, read_only=True)) as c:
        body = c.get("/api/files/e1/v1/diff").json()

    assert body["provenance"] == "records"
    assert body["provenance_note"] == SPANS_CAPTION
    assert "error" not in body
    entry = body["files"][0]
    assert entry["reconstructed"] is True
    assert entry["path"] == "agent/prompts.py"
    assert entry["span"]["mutation_id"] == "instr"
    assert "original" in entry["old_content"]
    assert "rewritten" in entry["new_content"]


def test_files_diff_keeps_snapshot_provenance_when_trees_are_there(client: TestClient) -> None:
    body = client.get("/api/files/e1/v1/diff").json()
    assert body["provenance"] == "snapshot"
    assert body["provenance_note"] == ""
    assert all("reconstructed" not in f for f in body["files"])


def test_files_diff_refuses_to_silently_diff_against_a_different_parent(
    populated_workspace: Path, tmp_path: Path
) -> None:
    """A pruned RECORDED parent must not be swapped for whichever tree survives.

    ``v2`` was derived from ``v1``. With ``v1``'s tree gone, falling back to
    ``v0`` would answer "what changed since the seed" under the heading
    "what this candidate changed" — a different question, silently.
    """
    store = DirectoryGenerationStore(populated_workspace)
    patch = _patch("p2", '"""second rewrite"""')
    store.derive_generation("e1", "v1", "v2", [patch])
    _record_experiment(populated_workspace, "v2", "v1", patch)
    _prune_tree(populated_workspace, "e1", "v1")

    static_dir = tmp_path / "static-parent"
    static_dir.mkdir()
    with TestClient(create_app(populated_workspace, static_dir, read_only=True)) as c:
        body = c.get("/api/files/e1/v2/diff").json()

    assert body["parent_generation_id"] == "v1"
    assert body["provenance"] == "records"
    assert body["provenance_note"] == SPANS_CAPTION
