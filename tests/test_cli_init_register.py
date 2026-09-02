"""Tests for the ``init`` and ``register`` subcommands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.cli.commands.init import init_cmd
from zicato.cli.commands.register import register_cmd
from zicato.epoch import load_lineage
from zicato.workspace.config_io import CONFIG_FILENAME, LINEAGE_FILENAME

# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    runner = CliRunner()
    result = runner.invoke(
        init_cmd,
        ["--workspace", str(workspace), "--instance-id", "alpha"],
    )
    assert result.exit_code == 0, result.output
    assert workspace.is_dir()

    config_path = workspace / CONFIG_FILENAME
    assert config_path.exists()
    config = json.loads(config_path.read_text())
    assert config["instance_id"] == "alpha"
    assert "created_at" in config

    lineage_path = workspace / LINEAGE_FILENAME
    assert lineage_path.exists()
    lineage = json.loads(lineage_path.read_text())
    # The shape the lineage loader reads — a ``nodes``/``edges`` document
    # is rejected as malformed by ``load_lineage`` (issue #124 triage).
    assert lineage == {"epochs": []}
    assert load_lineage(workspace) == {"epochs": []}


def test_init_scaffolds_the_proposal_runtime_unfilled(tmp_path: Path) -> None:
    """A new workspace carries the block to fill in, and nothing runnable.

    Every field is spelled out with its documented default so an operator
    edits rather than researches, and the one field only they can supply —
    the absolute path of the Foe binary — is left as a placeholder. Foe
    searches no path for a binary, so there is nothing sensible to guess.
    """
    from zicato.proposer.external import UNSET_BINARY, external_proposer_config
    from zicato.proposer.foe_config import VIEWER_POLICIES

    workspace = tmp_path / ".zicato"
    result = CliRunner().invoke(init_cmd, ["--workspace", str(workspace)])
    assert result.exit_code == 0, result.output

    config = json.loads((workspace / CONFIG_FILENAME).read_text())
    block = config["proposer"]
    assert block["binary"] == UNSET_BINARY
    assert block["budget"]["model_calls"] >= 1
    assert block["viewer"] in VIEWER_POLICIES
    assert set(block["model"]) == {"provider", "model", "options"}

    # Until the binary is named, the workspace has not said how it
    # proposes: the contract still hashes, with no binary present, and a
    # round refuses to open.
    assert external_proposer_config(config, workspace) is None


def test_init_records_the_generation_source_backend(tmp_path: Path) -> None:
    """A new workspace records which generation store it is built on.

    The backend a workspace uses is a durable property of its contents,
    so it is written at creation rather than inferred later from a
    default that can change under an existing workspace (issue #204).
    """
    from zicato.epoch.genstore import (
        DEFAULT_GENERATION_SOURCE_BACKEND,
        GENERATION_SOURCE_BACKEND_KEY,
        resolve_generation_store_backend,
    )

    workspace = tmp_path / ".zicato"
    runner = CliRunner()
    assert runner.invoke(init_cmd, ["--workspace", str(workspace)]).exit_code == 0
    config = json.loads((workspace / CONFIG_FILENAME).read_text())
    assert config[GENERATION_SOURCE_BACKEND_KEY] == DEFAULT_GENERATION_SOURCE_BACKEND
    assert resolve_generation_store_backend(workspace) == DEFAULT_GENERATION_SOURCE_BACKEND


def test_init_default_instance_id(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    runner = CliRunner()
    result = runner.invoke(init_cmd, ["--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    config = json.loads((workspace / CONFIG_FILENAME).read_text())
    assert config["instance_id"] == "default"


def test_init_refuses_to_overwrite(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    runner = CliRunner()
    first = runner.invoke(init_cmd, ["--workspace", str(workspace)])
    assert first.exit_code == 0

    second = runner.invoke(init_cmd, ["--workspace", str(workspace)])
    assert second.exit_code != 0
    # The error message should mention --force as the escape hatch.
    assert "--force" in second.output


def test_init_force_overwrites(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    runner = CliRunner()
    first = runner.invoke(init_cmd, ["--workspace", str(workspace), "--instance-id", "first"])
    assert first.exit_code == 0
    assert json.loads((workspace / CONFIG_FILENAME).read_text())["instance_id"] == "first"

    second = runner.invoke(
        init_cmd,
        ["--workspace", str(workspace), "--instance-id", "second", "--force"],
    )
    assert second.exit_code == 0, second.output
    assert json.loads((workspace / CONFIG_FILENAME).read_text())["instance_id"] == "second"


def test_init_force_preserves_existing_generation_source_backend(tmp_path: Path) -> None:
    """Force must not orphan a supported directory-backed source history."""
    from zicato.epoch.genstore import GENERATION_SOURCE_BACKEND_KEY

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / CONFIG_FILENAME).write_text(
        json.dumps(
            {
                "instance_id": "first",
                GENERATION_SOURCE_BACKEND_KEY: " Directory ",
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        init_cmd,
        ["--workspace", str(workspace), "--instance-id", "second", "--force"],
    )

    assert result.exit_code == 0, result.output
    config = json.loads((workspace / CONFIG_FILENAME).read_text())
    assert config[GENERATION_SOURCE_BACKEND_KEY] == "directory"


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def _init_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / ".zicato"
    runner = CliRunner()
    result = runner.invoke(init_cmd, ["--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    return workspace


def test_register_writes_entrypoint_and_trees(tmp_path: Path) -> None:
    """Both keys land. The entrypoint is tree-relative: its top-level module
    is a registered tree's basename, the only form a generation snapshot can
    supply (issue #110)."""
    workspace = _init_workspace(tmp_path)
    src_a = tmp_path / "my_pkg"
    src_a.mkdir()
    src_b = tmp_path / "extra_pkg"
    src_b.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        register_cmd,
        [
            "--workspace",
            str(workspace),
            "--adk",
            "my_pkg.agent:root_agent",
            "--mutable-tree",
            str(src_a),
            "--mutable-tree",
            str(src_b),
        ],
    )
    assert result.exit_code == 0, result.output

    config = json.loads((workspace / CONFIG_FILENAME).read_text())
    # init keys preserved:
    assert config["instance_id"] == "default"
    assert "created_at" in config
    # register keys written:
    assert config["adk_entrypoint"] == "my_pkg.agent:root_agent"
    assert config["mutable_trees"] == [str(src_a), str(src_b)]
    scoring = json.loads((tmp_path / "scoring.json").read_text())
    assert scoring["goldfive"] == {}


def test_register_preserves_explicit_goldfive_settings(tmp_path: Path) -> None:
    workspace = _init_workspace(tmp_path)
    scoring_file = tmp_path / "scoring.json"
    scoring = json.loads(scoring_file.read_text())
    scoring["goldfive"] = {"steering": {"threshold": "critical"}}
    scoring_file.write_text(json.dumps(scoring))

    result = CliRunner().invoke(
        register_cmd,
        ["--workspace", str(workspace), "--adk", "pkg.module:agent"],
    )

    assert result.exit_code == 0, result.output
    persisted = json.loads(scoring_file.read_text())
    assert persisted["goldfive"] == {"steering": {"threshold": "critical"}}


def test_register_with_no_mutable_trees(tmp_path: Path) -> None:
    workspace = _init_workspace(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        register_cmd,
        [
            "--workspace",
            str(workspace),
            "--adk",
            "pkg.module:agent",
        ],
    )
    assert result.exit_code == 0, result.output
    config = json.loads((workspace / CONFIG_FILENAME).read_text())
    assert config["mutable_trees"] == []


def test_register_writes_proposer_path(tmp_path: Path) -> None:
    """`--proposer-path` lands `contract.proposer_path` (absolutised)."""
    workspace = _init_workspace(tmp_path)
    proposer_dir = tmp_path / "proposers" / "fancy"
    (proposer_dir / "skills").mkdir(parents=True)

    runner = CliRunner()
    result = runner.invoke(
        register_cmd,
        [
            "--workspace",
            str(workspace),
            "--adk",
            "pkg.module:agent",
            "--proposer-path",
            str(proposer_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    config = json.loads((workspace / CONFIG_FILENAME).read_text())
    assert config["contract"]["proposer_path"] == str(proposer_dir.resolve())


def test_register_omits_proposer_path_by_default(tmp_path: Path) -> None:
    """Without the flag, `contract.proposer_path` is left unset (builtin)."""
    workspace = _init_workspace(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        register_cmd,
        ["--workspace", str(workspace), "--adk", "pkg.module:agent"],
    )
    assert result.exit_code == 0, result.output
    config = json.loads((workspace / CONFIG_FILENAME).read_text())
    assert "proposer_path" not in config["contract"]


def test_register_proposer_path_resolves_into_contract_inputs(tmp_path: Path) -> None:
    """A registered proposer dir is picked up by `resolve_contract_inputs`.

    Mirrors how `evolve` reads the contract back: the flag must land in
    `config.json` such that a subsequent contract resolve sees the
    proposer dir, while an unregistered workspace resolves to the builtin
    default (`None`).
    """
    from zicato.epoch.contract import resolve_contract_inputs

    workspace = _init_workspace(tmp_path)
    proposer_dir = tmp_path / "proposers" / "fancy"
    (proposer_dir / "skills").mkdir(parents=True)

    runner = CliRunner()
    # First register WITHOUT the flag — builtin default proposer.
    result = runner.invoke(
        register_cmd,
        ["--workspace", str(workspace), "--adk", "pkg.module:agent"],
    )
    assert result.exit_code == 0, result.output
    assert resolve_contract_inputs(workspace).proposer_path is None

    # Re-register WITH the flag — the proposer dir is now resolved.
    result = runner.invoke(
        register_cmd,
        [
            "--workspace",
            str(workspace),
            "--adk",
            "pkg.module:agent",
            "--proposer-path",
            str(proposer_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    resolved = resolve_contract_inputs(workspace).proposer_path
    assert resolved == proposer_dir.resolve()


def test_register_requires_initialized_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / ".not-yet-initialized"
    runner = CliRunner()
    result = runner.invoke(
        register_cmd,
        [
            "--workspace",
            str(workspace),
            "--adk",
            "pkg.module:agent",
        ],
    )
    assert result.exit_code != 0
    assert "init" in result.output.lower()


def test_register_rejects_malformed_entrypoint(tmp_path: Path) -> None:
    workspace = _init_workspace(tmp_path)
    runner = CliRunner()

    bad_inputs = ["no_colon_here", ":missing_module", "missing_symbol:"]
    for bad in bad_inputs:
        result = runner.invoke(
            register_cmd,
            ["--workspace", str(workspace), "--adk", bad],
        )
        assert result.exit_code != 0, f"expected failure for {bad!r}: {result.output}"


def test_register_requires_adk_flag(tmp_path: Path) -> None:
    workspace = _init_workspace(tmp_path)
    runner = CliRunner()
    result = runner.invoke(register_cmd, ["--workspace", str(workspace)])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Deleted env vars are ignored — flags are the only source
# ---------------------------------------------------------------------------


def test_init_ignores_deleted_instance_id_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ZICATO_INSTANCE_ID`` is deleted; only ``--instance-id`` counts.

    The env var was fully shadowed by the ``--instance-id`` flag and was
    removed. Setting it must change nothing: with no flag the default
    lands, and an explicit flag is the only way to pick another id.
    """
    monkeypatch.setenv("ZICATO_INSTANCE_ID", "from-env")
    runner = CliRunner()

    # No flag: the default wins, the env var is invisible.
    defaulted = tmp_path / "defaulted" / ".zicato"
    result = runner.invoke(init_cmd, ["--workspace", str(defaulted)])
    assert result.exit_code == 0
    config = json.loads((defaulted / CONFIG_FILENAME).read_text())
    assert config["instance_id"] == "default"

    # Explicit flag: the flag value lands.
    explicit = tmp_path / "explicit" / ".zicato"
    result = runner.invoke(
        init_cmd,
        ["--workspace", str(explicit), "--instance-id", "explicit-wins"],
    )
    assert result.exit_code == 0
    config = json.loads((explicit / CONFIG_FILENAME).read_text())
    assert config["instance_id"] == "explicit-wins"
