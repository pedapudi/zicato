"""Invocation ownership fixes configuration and selected execution inputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.settings import InvocationOverlay
from zicato.evolve.invocation import validated_invocation
from zicato.runtime.lock import validate_workspace_lock


@pytest.mark.asyncio
async def test_recovery_precedes_authored_validation_and_overlay_capture(tmp_path, monkeypatch):
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    path = workspace / "config.json"
    path.write_text('{"runtime":{"seed":true}}')
    observed = []

    def recover_contract(root, *, writer):
        validate_workspace_lock(writer, root)
        observed.append("contract")
        path.write_text('{"runtime":{"seed":17,"parallelism":2}}')

    def recover_epoch(root, *, writer):
        validate_workspace_lock(writer, root)
        observed.append("epoch")

    def validate(*args, **kwargs):
        assert observed == ["contract", "epoch"]
        observed.append("validate")

    monkeypatch.setattr(
        "zicato.contract_draft.publication.recover_contract_publication", recover_contract
    )
    monkeypatch.setattr("zicato.epoch.lifecycle.recover_epoch_publication", recover_epoch)
    monkeypatch.setattr("zicato.check.require_workspace_valid", validate)
    overlay = InvocationOverlay.from_mapping({"runtime": {"seed": 29}})
    async with validated_invocation(workspace, None, "recovery", overlay=overlay) as invocation:
        assert invocation.configuration.values.runtime.seed == 29
        assert invocation.configuration.values.runtime.parallelism == 2
        assert invocation.configuration.sources["runtime.seed"] == "invocation"
        path.write_text('{"runtime":{"seed":41,"parallelism":7}}')
        assert invocation.workspace_config["runtime"]["seed"] == 17
        assert invocation.configuration.values.runtime.parallelism == 2


@pytest.mark.asyncio
async def test_public_wrappers_forward_independent_invocation_settings(tmp_path, monkeypatch):
    from zicato.evolve import loop, round_entry

    monkeypatch.setattr("zicato.check.require_workspace_valid", lambda *args, **kwargs: None)
    observed = []

    async def observe(*, invocation, **kwargs):
        observed.append(invocation.configuration)
        return []

    monkeypatch.setattr(loop, "_evolve_n_rounds", observe)
    monkeypatch.setattr(round_entry, "_evolve_once", observe)
    for index, run in enumerate((loop.evolve_n_rounds, round_entry.evolve_once)):
        workspace = tmp_path / str(index)
        workspace.mkdir()
        (workspace / "config.json").write_text(json.dumps({"runtime": {"seed": 17}}))
        kwargs = {"rounds": 1} if index == 0 else {}
        await run(
            workspace_root=workspace,
            invocation_overlay=InvocationOverlay.from_mapping({"runtime": {"seed": 29 + index}}),
            **kwargs,
        )
    assert [item.values.runtime.seed for item in observed] == [29, 30]
    assert all(item.sources["runtime.seed"] == "invocation" for item in observed)


def test_competing_cli_invocation_starts_no_service(tmp_path, monkeypatch):
    from importlib import import_module

    from click.testing import CliRunner

    from zicato.runtime.lock import acquire_workspace_lock

    command = import_module("zicato.cli.commands.evolve")
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text("{}")
    started = []

    async def spawn(*args, **kwargs):
        started.append(args)

    async def report(*args, **kwargs):
        pass

    monkeypatch.setattr(command, "_maybe_spawn_supervisor", spawn)
    monkeypatch.setattr(command, "_maybe_spawn_dashboard", spawn)
    monkeypatch.setattr(command, "_report_dashboard_url", report)
    with acquire_workspace_lock(workspace, "active"):
        result = CliRunner().invoke(command.evolve_cmd, ["--workspace", str(workspace)])
    assert result.exit_code != 0
    assert started == []


def test_score_selection_requires_revision_before_replacement(tmp_path, monkeypatch):
    from zicato.tournament.scoring import write_gen_score
    from zicato.workspace import WorkspaceLayout, projection

    write_gen_score(tmp_path, "e0", "v0", {"base_seed": 17, "scalar": 0.0})
    layout = WorkspaceLayout.from_root(tmp_path)
    selected = layout.gen_score("e0", "v0").read_bytes()
    history = layout.gen_score_history("e0", "v0").read_bytes()
    assert projection.epoch_revisions(tmp_path).keys() == {"e0"}

    def refuse_revision(root: Path, epoch_id: str) -> None:
        assert root == tmp_path and epoch_id == "e0"
        assert layout.gen_score("e0", "v0").read_bytes() == selected
        raise OSError("revision persistence failed")

    monkeypatch.setattr(projection, "mark_epoch_changed", refuse_revision)
    with pytest.raises(OSError, match="revision persistence failed"):
        write_gen_score(tmp_path, "e0", "v0", {"base_seed": 23, "scalar": 0.0})
    assert layout.gen_score("e0", "v0").read_bytes() == selected
    assert layout.gen_score_history("e0", "v0").read_bytes() == history


@pytest.mark.asyncio
async def test_pending_contract_is_read_only_in_preview_and_recovers_before_execution(
    tmp_path, monkeypatch
):
    from click.testing import CliRunner

    from tests.test_contract_draft_apply import workspace as workspace_fixture
    from zicato.cli.commands.evolve import evolve_cmd
    from zicato.contract_draft import operations, publication
    from zicato.contract_draft.draft import TournamentDraft
    from zicato.workspace.contract_publication import contract_publication_path

    workspace = workspace_fixture.__wrapped__(tmp_path)
    draft = TournamentDraft.from_workspace(workspace)
    operations.set_brief(draft, "Accepted proposal instructions.\n")
    operations.set_weights(draft, pass_weight=3)
    atomic_write = publication.atomic_write_text

    def interrupt(path, text, **kwargs):
        atomic_write(path, text, **kwargs)
        raise OSError("publication interrupted")

    monkeypatch.setattr(publication, "atomic_write_text", interrupt)
    with pytest.raises(OSError, match="publication interrupted"):
        operations.apply(draft, workspace, confirm=True)
    monkeypatch.setattr(publication, "atomic_write_text", atomic_write)
    marker = contract_publication_path(workspace)
    pending = json.loads(marker.read_text())
    paths = [marker, *(Path(write["path"]) for write in pending["writes"])]
    before = {path: path.read_bytes() for path in paths}
    result = CliRunner().invoke(evolve_cmd, ["--workspace", str(workspace), "--dry-run"])
    assert result.exit_code != 0
    assert {path: path.read_bytes() for path in paths} == before

    def validate(*args, **kwargs):
        assert json.loads(marker.read_text())["state"] == "complete"
        for write in pending["writes"]:
            assert Path(write["path"]).read_text() == write["text"]

    monkeypatch.setattr("zicato.check.require_workspace_valid", validate)
    async with validated_invocation(workspace, None, "recover-contract"):
        pass
