"""Tests for the dashboard Files view — :mod:`zicato.dashboard.filetree`.

The Files view browses every generation's source tree and applied
patches through the :class:`~zicato.epoch.genstore.GenerationStore`
seam. These tests exercise it end-to-end through the ASGI app for
**both** storage backends, so the view is proven backend-neutral.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.core.types import Patch
from zicato.dashboard.server import create_app
from zicato.epoch.genstore import DirectoryGenerationStore
from zicato.epoch.git_genstore import GitGenerationStore


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


@pytest.fixture(params=["directory", "git"])
def populated_workspace(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A ``.zicato/`` workspace with a seeded + derived generation.

    Parametrised over both storage backends. For the git backend the
    workspace ``config.json`` carries ``storage_backend: "git"`` so the
    dashboard's :func:`default_generation_store` selects it.
    """
    ws = tmp_path / ".zicato"
    ws.mkdir()
    # An epoch directory must exist for the filetree index to list it.
    (ws / "epochs" / "e1").mkdir(parents=True)

    if request.param == "git":
        (ws / "config.json").write_text('{"storage_backend": "git"}', encoding="utf-8")
        store = GitGenerationStore(ws)
    else:
        (ws / "config.json").write_text("{}", encoding="utf-8")
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
