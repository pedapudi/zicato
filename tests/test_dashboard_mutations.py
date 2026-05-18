"""Tests for the dashboard mutation-site browser — :mod:`zicato.dashboard.mutations`.

The mutation-site browser extends the Files view: it lists every
``# zicato:mutable`` annotated span in an epoch's ``v0`` baseline and,
per site, exposes the baseline content plus the patched content in any
generation whose patch touched that mutation id — the frontend diffs the
two. These tests exercise it end-to-end through the ASGI app for **both**
storage backends, so the view is proven backend-neutral, exactly like
the file-tree browser it sits beside.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.core.types import Experiment, HypothesisSpec, Patch
from zicato.dashboard.server import create_app
from zicato.epoch.genstore import DirectoryGenerationStore
from zicato.epoch.git_genstore import GitGenerationStore
from zicato.epoch.journal import write_experiment


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


@pytest.fixture(params=["directory", "git"])
def populated_workspace(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A ``.zicato/`` workspace with a seeded + patched generation.

    Parametrised over both storage backends. ``v1`` is derived from
    ``v0`` by a patch against ``researcher_instr``; ``reviewer_instr`` is
    left untouched so the "site with no patch" path is also covered. The
    ``experiment.json`` record is written so :meth:`list_patches` — the
    seam the mutation browser reads patch sets through — resolves the
    patch set for ``v1``.
    """
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "epochs" / "e1").mkdir(parents=True)

    if request.param == "git":
        (ws / "config.json").write_text('{"storage_backend": "git"}', encoding="utf-8")
        store: DirectoryGenerationStore | GitGenerationStore = GitGenerationStore(ws)
    else:
        (ws / "config.json").write_text("{}", encoding="utf-8")
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
