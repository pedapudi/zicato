"""Byte-range evidence agrees with the authoritative applier and stored source."""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests._containment_corpus import CORPUS, build_cases, materialize_case
from zicato.core.experiment import Experiment, HypothesisSpec
from zicato.core.mutation import Patch
from zicato.epoch.containment import (
    attest_generation,
    write_containment_manifest,
    write_mutation_policy,
)
from zicato.epoch.genstore import DirectoryGenerationStore
from zicato.epoch.git_genstore import GitGenerationStore
from zicato.epoch.journal import write_experiment
from zicato.mutation.applier import apply_patches
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.policy import MutationPolicy, source_files
from zicato.proposer.foe_scratch import (
    EditOutsideMutationPointError,
    project_working_copy,
    scratch_working_copy,
)
from zicato.workspace.layout import WorkspaceLayout

_CASES = json.loads(CORPUS.read_text())["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_shared_corpus(case: dict, tmp_path: Path) -> None:
    materialize_case(case, tmp_path)
    result = attest_generation(
        tmp_path,
        epoch_id="epoch",
        parent_generation_id="v0",
        generation_id="v1",
        parent_root=tmp_path / "parent",
        child_root=tmp_path / "child",
    )
    assert result.status == case["expected_status"], result


def test_shared_corpus_uses_actual_mutation_and_applier_semantics() -> None:
    assert json.loads(CORPUS.read_text()) == build_cases()


def test_archived_generation_without_captured_policy_remains_unverified(tmp_path: Path) -> None:
    case = copy.deepcopy(_CASES[0])
    case["manifest"].pop("policy_sha256")
    case["policy"] = None
    materialize_case(case, tmp_path)
    result = attest_generation(
        tmp_path,
        epoch_id="epoch",
        parent_generation_id="v0",
        generation_id="v1",
        parent_root=tmp_path / "parent",
        child_root=tmp_path / "child",
    )
    assert result.status == "unverified"
    assert not (tmp_path / "epochs/epoch/generations/v0/mutation-policies").exists()


@pytest.mark.parametrize("record", ["policy", "brief", "scoring"])
def test_policy_binding_refuses_redirected_canonical_inputs(tmp_path: Path, record: str) -> None:
    case = copy.deepcopy(_CASES[0])
    materialize_case(case, tmp_path)
    layout = WorkspaceLayout.from_root(tmp_path)
    target = {
        "policy": layout.mutation_policy("epoch", "v0", case["manifest"]["policy_sha256"]),
        "brief": layout.brief("epoch"),
        "scoring": layout.scoring("epoch"),
    }[record]
    copy_path = tmp_path / "redirected-input"
    copy_path.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(copy_path)
    result = attest_generation(
        tmp_path,
        epoch_id="epoch",
        parent_generation_id="v0",
        generation_id="v1",
        parent_root=tmp_path / "parent",
        child_root=tmp_path / "child",
    )
    assert result.status == "unverified"
    assert any(finding.code == "policy_binding" for finding in result.findings)


def test_literal_replacement_preserves_line_endings_outside_its_byte_span(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    source = b'# zicato:mutable id="unit"\r\nPROMPT = "original"\r\nTAIL = 1\r\n'
    (parent / "prompt.py").write_bytes(source)
    patch = Patch("patch", "unit", "replace", '"edited"', None, None, "Edit the instruction.")
    child = tmp_path / "child"
    apply_patches(parent, [patch], child)
    assert (child / "prompt.py").read_bytes() == source.replace(b'"original"', b'"edited"')


@pytest.mark.parametrize(
    "store_type,stored_change",
    [
        pytest.param(DirectoryGenerationStore, None, id="directory"),
        pytest.param(GitGenerationStore, None, id="git"),
        pytest.param(GitGenerationStore, ("v0", "agent/prompt.py", "100755"), id="parent-mode"),
        pytest.param(GitGenerationStore, ("v1", "agent/prompt.py", "100755"), id="child-mode"),
        pytest.param(GitGenerationStore, ("v0", ".gitignore", "100755"), id="parent-ignore-mode"),
        pytest.param(GitGenerationStore, ("v1", ".gitignore", "100755"), id="child-ignore-mode"),
        pytest.param(GitGenerationStore, ("v1", "agent/prompt.py", "120000"), id="child-type"),
        pytest.param(GitGenerationStore, ("v1", ".gitignore", "120000"), id="child-ignore-type"),
    ],
)
def test_publication_binds_reconstructed_selected_and_committed_source(
    tmp_path: Path, store_type, stored_change: tuple[str, str, str] | None
) -> None:
    workspace = tmp_path / ".zicato"
    target = tmp_path / "agent"
    target.mkdir()
    (target / "prompt.py").write_text('# zicato:mutable id="unit"\nPROMPT = "original"\n')
    (target / ".gitignore").write_text("temporary.bin\n")
    store = store_type(workspace)
    store.seed_generation("epoch", "v0", [target])
    parent = store.materialize_snapshot("epoch", "v0")
    policy = MutationPolicy.capture(parent, enumerate_mutations([parent]))
    layout = WorkspaceLayout.from_root(workspace)
    layout.epoch_dir("epoch").mkdir(parents=True, exist_ok=True)
    layout.brief("epoch").write_text("# Improve clarity\n")
    layout.scoring("epoch").write_text("{}\n")
    policy_sha256 = write_mutation_policy(
        workspace, epoch_id="epoch", parent_generation_id="v0", policy=policy
    )
    policy_path = layout.mutation_policy("epoch", "v0", policy_sha256)
    retained_policy = policy_path.read_bytes()
    retained_stat = policy_path.stat()
    assert (
        write_mutation_policy(workspace, epoch_id="epoch", parent_generation_id="v0", policy=policy)
        == policy_sha256
    )
    assert policy_path.stat().st_mtime_ns == retained_stat.st_mtime_ns
    assert policy_path.stat().st_ino == retained_stat.st_ino
    patch = Patch("patch", "unit", "replace", '"edited"', None, None, "Edit the instruction.")
    experiment = Experiment(
        "experiment",
        "epoch",
        "v1",
        "v0",
        "2026-01-01T00:00:00Z",
        HypothesisSpec("Clarify the instruction.", ("unit",), "Reduce ambiguity.", "0"),
        (patch,),
        None,
    )
    child = store.derive_generation("epoch", "v0", "v1", [patch])
    write_experiment(workspace, "epoch", "v1", experiment)
    result = write_containment_manifest(
        workspace,
        epoch_id="epoch",
        parent_generation_id="v0",
        generation_id="v1",
        policy=policy,
        policy_sha256=policy_sha256,
        experiment=experiment,
        genstore=store,
    )
    assert result.status == "contained", result
    layout = WorkspaceLayout.from_root(workspace)
    record = json.loads(layout.containment_manifest("epoch", "v1").read_bytes())
    assert {entry["path"] for entry in record["files"]} == {
        entry.path for entry in source_files(child)
    }
    assert store.read_file("epoch", "v1", "agent/.gitignore") == b"temporary.bin\n"
    assert any(entry["path"] == "agent/.gitignore" for entry in record["files"])
    if store_type is GitGenerationStore:
        assert any(entry["path"] == ".gitignore" for entry in record["files"])
        assert store.read_file("epoch", "v1", ".gitignore") == (child / ".gitignore").read_bytes()
    accepted_record = layout.containment_manifest("epoch", "v1").read_bytes()

    if stored_change is not None:
        assert isinstance(store, GitGenerationStore)
        generation_id, path, mode = stored_change
        selected = store.snapshot_path("epoch", generation_id)
        selected_inventory = source_files(selected)
        tag = store._generation_tag("epoch", generation_id)
        blob = store._git("rev-parse", f"{tag}:{path}").strip()
        store._git("read-tree", tag)
        store._git("update-index", "--cacheinfo", f"{mode},{blob},{path}")
        store._commit("Change stored source metadata.")
        store._tag_generation("epoch", generation_id)
        assert store._git("ls-tree", tag, "--", path).startswith(mode)
        assert store.read_file("epoch", generation_id, path) == (selected / path).read_bytes()
        assert source_files(selected) == selected_inventory
        with pytest.raises(ValueError, match="committed source"):
            write_containment_manifest(
                workspace,
                epoch_id="epoch",
                parent_generation_id="v0",
                generation_id="v1",
                policy=policy,
                policy_sha256=policy_sha256,
                experiment=experiment,
                genstore=store,
            )
        assert layout.containment_manifest("epoch", "v1").read_bytes() == accepted_record
        return

    # A valid patch for the same unit cannot stand in for the accepted patch.
    changed = replace(experiment, patches=(replace(patch, new_content='"different"'),))
    write_experiment(workspace, "epoch", "v1", changed)
    with pytest.raises(ValueError, match="recorded patches differ"):
        write_containment_manifest(
            workspace,
            epoch_id="epoch",
            parent_generation_id="v0",
            generation_id="v1",
            policy=policy,
            policy_sha256=policy_sha256,
            experiment=experiment,
            genstore=store,
        )
    with pytest.raises(ValueError, match="authoritative patch reconstruction"):
        write_containment_manifest(
            workspace,
            epoch_id="epoch",
            parent_generation_id="v0",
            generation_id="v1",
            policy=policy,
            policy_sha256=policy_sha256,
            experiment=changed,
            genstore=store,
        )
    assert layout.containment_manifest("epoch", "v1").read_bytes() == accepted_record

    policy_path.write_bytes(retained_policy + b" ")
    with pytest.raises(ValueError, match="immutable mutation policy"):
        write_mutation_policy(workspace, epoch_id="epoch", parent_generation_id="v0", policy=policy)
    assert policy_path.read_bytes() == retained_policy + b" "

    # Target-authored ignore rules affect which files a future snapshot can contain.
    with scratch_working_copy(parent) as scratch:
        (scratch / "agent/.gitignore").write_text("prompt.py\n")
        with pytest.raises(EditOutsideMutationPointError, match=".gitignore"):
            project_working_copy(policy, scratch)
