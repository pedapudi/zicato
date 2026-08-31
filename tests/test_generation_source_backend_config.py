"""The generation-source backend as workspace configuration.

Three properties, all about a workspace whose ``config.json`` does not
agree with the source trees on disk:

* **Construction refuses a contradiction.** A knob naming a backend whose
  source data is absent while the other backend's is present raises, and
  the error names the backend the data actually matches. Without that, the
  store does not fail — it reads an empty workspace, and every reader
  downstream reports "this generation has no source tree".
* **The dashboard degrades, never 500s.** A workspace with no resolvable
  backend at all — the shape every workspace created before the key
  existed has — still answers all four store-backed endpoints, in their
  normal shapes, with the reason in the payload. And it never captions the
  condition as snapshot GC, which would be an invented retention fact.
* **There is a safe way to fix it.** ``zicato repair
  generation-source-backend`` merges the one key; ``zicato init --force``
  refuses to reach the same end by resetting a recorded lineage.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from zicato.cli.commands.init import init_cmd
from zicato.cli.commands.repair_generation_source_backend import (
    repair_generation_source_backend_cmd,
)
from zicato.core.types import Experiment, HypothesisSpec, Patch
from zicato.dashboard.mutations import SPANS_UNREACHABLE_CAPTION
from zicato.dashboard.server import create_app
from zicato.epoch.genstore import (
    GENERATION_SOURCE_BACKEND_KEY,
    DirectoryGenerationStore,
    default_generation_store,
    generation_source_evidence,
)
from zicato.epoch.journal import write_experiment

_REPAIR_COMMAND = "zicato repair generation-source-backend"


def _patch(pid: str, new_content: str) -> Patch:
    return Patch(
        id=pid,
        mutation_id="instr",
        op="replace",
        new_content=new_content,
        new_numeric=None,
        new_enum=None,
        rationale="backend config test",
    )


def _mutable_tree(root: Path) -> Path:
    tree = root / "agent"
    tree.mkdir(parents=True)
    (tree / "prompts.py").write_text(
        textwrap.dedent(
            '''
            # zicato:mutable id="instr"
            INSTR = """original"""
            '''
        ),
        encoding="utf-8",
    )
    return tree


def _directory_workspace(tmp_path: Path, backend: str | None) -> Path:
    """A directory-snapshot workspace declaring ``backend`` (or nothing).

    The trees are seeded through :class:`DirectoryGenerationStore` directly,
    so the source data on disk is the directory backend's regardless of what
    the config then claims — which is exactly the disagreement under test.
    """
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "epochs" / "e1").mkdir(parents=True)
    config: dict[str, str] = {"instance_id": "test"}
    if backend is not None:
        config[GENERATION_SOURCE_BACKEND_KEY] = backend
    (ws / "config.json").write_text(json.dumps(config), encoding="utf-8")

    store = DirectoryGenerationStore(ws)
    store.seed_generation("e1", "v0", [_mutable_tree(tmp_path / "src")])
    patch = _patch("p1", '"""rewritten"""')
    store.derive_generation("e1", "v0", "v1", [patch])
    # The store materialises the tree; the journal records the patch set the
    # records-only views answer from. A real round writes both.
    write_experiment(
        ws,
        "e1",
        "v1",
        Experiment(
            id="exp_e1_v1",
            epoch_id="e1",
            generation_id="v1",
            parent_generation_id="v0",
            proposed_at="2026-05-18T00:00:00Z",
            hypothesis=HypothesisSpec(
                core_idea="rewrite the instruction",
                modulating=("instr",),
                why="backend config test",
                expected_drift_movements=(),
                expected_pass_rate_delta="+0.0",
            ),
            patches=(patch,),
            outcome=None,
        ),
    )
    return ws


def _git_workspace(tmp_path: Path, backend: str) -> Path:
    """A workspace holding the git backend's repository and no snapshots."""
    ws = tmp_path / ".zicato"
    (ws / "repo" / ".git").mkdir(parents=True)
    (ws / "config.json").write_text(
        json.dumps({"instance_id": "test", GENERATION_SOURCE_BACKEND_KEY: backend}),
        encoding="utf-8",
    )
    return ws


# ---------------------------------------------------------------------------
# Construction refuses a knob the disk contradicts
# ---------------------------------------------------------------------------


def test_git_knob_over_directory_snapshots_raises_and_names_directory(tmp_path: Path) -> None:
    ws = _directory_workspace(tmp_path, "git")

    assert generation_source_evidence(ws) == "directory"
    with pytest.raises(ValueError) as excinfo:
        default_generation_store(ws)

    message = str(excinfo.value)
    assert "'directory'" in message, message
    assert "snapshot" in message, message
    assert _REPAIR_COMMAND in message, message


def test_directory_knob_over_a_git_repository_raises_and_names_git(tmp_path: Path) -> None:
    ws = _git_workspace(tmp_path, "directory")

    assert generation_source_evidence(ws) == "git"
    with pytest.raises(ValueError) as excinfo:
        default_generation_store(ws)

    message = str(excinfo.value)
    assert "'git'" in message, message
    assert _REPAIR_COMMAND in message, message


def test_a_matching_knob_constructs_a_store(tmp_path: Path) -> None:
    """The guard fires on a contradiction, never on agreement."""
    ws = _directory_workspace(tmp_path, "directory")

    store = default_generation_store(ws)

    assert store.backend_name == "directory"
    assert sorted(store.list_generations("e1")) == ["v0", "v1"]


def test_silent_evidence_lets_either_backend_stand(tmp_path: Path) -> None:
    """A workspace with no materialised source declares whatever it likes."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "config.json").write_text(
        json.dumps({GENERATION_SOURCE_BACKEND_KEY: "git"}), encoding="utf-8"
    )

    assert generation_source_evidence(ws) is None
    assert default_generation_store(ws).backend_name == "git"


def test_a_missing_key_names_the_command_that_sets_it(tmp_path: Path) -> None:
    ws = _directory_workspace(tmp_path, None)

    with pytest.raises(ValueError) as excinfo:
        default_generation_store(ws)

    message = str(excinfo.value)
    assert _REPAIR_COMMAND in message, message
    # …and it recommends the value the workspace's own data matches.
    assert "--backend directory" in message, message


# ---------------------------------------------------------------------------
# The dashboard degrades same-shaped rather than 500ing
# ---------------------------------------------------------------------------


@pytest.fixture
def keyless_client(tmp_path: Path) -> TestClient:
    """A dashboard over a workspace whose config predates the backend key."""
    ws = _directory_workspace(tmp_path, None)
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    with TestClient(create_app(ws, static_dir, read_only=True)) as client:
        yield client


def test_file_index_lists_recorded_generations_and_names_the_reason(
    keyless_client: TestClient,
) -> None:
    response = keyless_client.get("/api/files")

    assert response.status_code == 200
    body = response.json()
    generations = body["epochs"][0]["generations"]
    assert [g["generation_id"] for g in generations] == ["v0", "v1"]
    assert all(g["has_tree"] is False for g in generations)
    assert GENERATION_SOURCE_BACKEND_KEY in body["error"]


def test_generation_tree_degrades_without_blaming_snapshot_gc(
    keyless_client: TestClient,
) -> None:
    response = keyless_client.get("/api/files/e1/v1/tree")

    assert response.status_code == 200
    body = response.json()
    assert body["entries"] == []
    assert "pruned by snapshot GC" not in body["error"], body["error"]
    assert GENERATION_SOURCE_BACKEND_KEY in body["error"]


def test_generation_diff_degrades_to_reconstructed_spans(keyless_client: TestClient) -> None:
    response = keyless_client.get("/api/files/e1/v1/diff")

    assert response.status_code == 200
    body = response.json()
    assert body["provenance"] == "records"
    # The patch record still names the span the generation rewrote, so the
    # diff answers from records rather than reporting nothing.
    assert [f["span"]["mutation_id"] for f in body["files"]] == ["instr"]
    # …and the caption says the tree is unreachable, not that GC took it.
    assert body["provenance_note"] == SPANS_UNREACHABLE_CAPTION


def test_mutation_index_degrades_and_reports_the_reason(keyless_client: TestClient) -> None:
    response = keyless_client.get("/api/mutations/e1")

    assert response.status_code == 200
    body = response.json()
    assert body["provenance"] == "records"
    assert "pruned" not in body["provenance_note"], body["provenance_note"]
    assert GENERATION_SOURCE_BACKEND_KEY in body["error"]


# ---------------------------------------------------------------------------
# The safe remedy
# ---------------------------------------------------------------------------


def test_repair_sets_the_key_and_leaves_every_other_key_alone(tmp_path: Path) -> None:
    ws = _directory_workspace(tmp_path, None)
    config = json.loads((ws / "config.json").read_text())
    config["contract"] = {"frozen": True}
    config["mutable_trees"] = ["agent"]
    (ws / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (ws / "lineage.json").write_text(json.dumps({"epochs": [{"id": "e1"}]}), encoding="utf-8")

    result = CliRunner().invoke(
        repair_generation_source_backend_cmd,
        ["--workspace", str(ws), "--backend", "directory"],
    )

    assert result.exit_code == 0, result.output
    written = json.loads((ws / "config.json").read_text())
    assert written[GENERATION_SOURCE_BACKEND_KEY] == "directory"
    assert written["contract"] == {"frozen": True}
    assert written["mutable_trees"] == ["agent"]
    assert json.loads((ws / "lineage.json").read_text())["epochs"] == [{"id": "e1"}]
    # …and the workspace now opens.
    assert default_generation_store(ws).backend_name == "directory"


def test_repair_refuses_a_value_the_source_data_contradicts(tmp_path: Path) -> None:
    ws = _directory_workspace(tmp_path, None)

    result = CliRunner().invoke(
        repair_generation_source_backend_cmd,
        ["--workspace", str(ws), "--backend", "git"],
    )

    assert result.exit_code != 0
    assert "--backend directory" in result.output, result.output
    assert GENERATION_SOURCE_BACKEND_KEY not in json.loads((ws / "config.json").read_text())


def test_repair_writes_a_contradicting_value_under_force(tmp_path: Path) -> None:
    ws = _directory_workspace(tmp_path, None)

    result = CliRunner().invoke(
        repair_generation_source_backend_cmd,
        ["--workspace", str(ws), "--backend", "git", "--force"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads((ws / "config.json").read_text())[GENERATION_SOURCE_BACKEND_KEY] == "git"


def test_repair_requires_an_initialized_workspace(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        repair_generation_source_backend_cmd,
        ["--workspace", str(tmp_path / "nope"), "--backend", "git"],
    )

    assert result.exit_code != 0
    assert "not initialized" in result.output


def test_init_force_refuses_to_discard_a_recorded_lineage(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "config.json").write_text(json.dumps({"instance_id": "first"}), encoding="utf-8")
    lineage = json.dumps({"epochs": [{"id": "e1", "generations": ["v0", "v1"]}]})
    (ws / "lineage.json").write_text(lineage, encoding="utf-8")

    result = CliRunner().invoke(
        init_cmd, ["--workspace", str(ws), "--instance-id", "second", "--force"]
    )

    assert result.exit_code != 0
    assert "--reset-lineage" in result.output, result.output
    assert _REPAIR_COMMAND in result.output, result.output
    # Nothing was touched.
    assert (ws / "lineage.json").read_text() == lineage
    assert json.loads((ws / "config.json").read_text())["instance_id"] == "first"


def test_init_force_discards_a_recorded_lineage_when_asked_explicitly(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "config.json").write_text(json.dumps({"instance_id": "first"}), encoding="utf-8")
    (ws / "lineage.json").write_text(json.dumps({"epochs": [{"id": "e1"}]}), encoding="utf-8")

    result = CliRunner().invoke(
        init_cmd,
        ["--workspace", str(ws), "--instance-id", "second", "--force", "--reset-lineage"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads((ws / "lineage.json").read_text()) == {"epochs": []}


def test_init_force_still_re_initializes_an_empty_workspace(tmp_path: Path) -> None:
    """The refusal is about recorded epochs, not about ``--force`` itself."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    (ws / "config.json").write_text(json.dumps({"instance_id": "first"}), encoding="utf-8")

    result = CliRunner().invoke(
        init_cmd, ["--workspace", str(ws), "--instance-id", "second", "--force"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads((ws / "config.json").read_text())["instance_id"] == "second"
