"""Accepted proposal edits agree with mutation policy and reconstructed source."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._foe_support import call_turn, return_turn
from tests.test_proposer_foe_agent import _HYPOTHESIS, Workspace, _edit
from zicato.core.types import Patch
from zicato.epoch.git_genstore import GitGenerationStore
from zicato.evolve.round import build_post_apply_validator
from zicato.mutation.applier import apply_patches
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.policy import MutationPolicy
from zicato.proposer.foe_scratch import (
    EditOutsideMutationPointError,
    project_working_copy,
    scratch_working_copy,
)
from zicato.proposer.proposer import ProposerError

_REGIONS = [
    ("literal.py", '# zicato:mutable id="unit"\nPROMPT = "original"\n', '"original"', '"edited"'),
    (
        "unicode.py",
        '# zicato:mutable id="unit"\nPRÖMPT = "original"; SUFFIX = 1\n',
        '"original"',
        '"edited"',
    ),
    (
        "code.py",
        '# zicato:mutable:code id="unit"\nVALUE = 1\n# zicato:mutable:end\nTAIL = 2\n',
        "VALUE = 1\n",
        "VALUE = 3\nADDED = 4\n",
    ),
    ("module.py", '# zicato:mutable:file id="unit"\nVALUE = 1\n', "VALUE = 1", "RENAMED = 2"),
    ("brief.md", '<!-- zicato:mutable:file id="unit" -->\noriginal\n', "original", "edited"),
    ("config.yaml", '# zicato:mutable:file id="unit"\nvalue: original\n', "original", "edited"),
    ("config.toml", '# zicato:mutable:file id="unit"\nvalue = "original"\n', "original", "edited"),
]


@pytest.mark.parametrize("filename,source,before,after", _REGIONS, ids=[r[0] for r in _REGIONS])
def test_accepted_units_reconstruct_exact_source_and_reject_extra_bytes(
    tmp_path: Path, filename: str, source: str, before: str, after: str
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / filename).write_text(source)
    (parent / "opaque.bin").write_bytes(b"\x00\xff")
    policy = MutationPolicy.capture(parent, enumerate_mutations([parent]))
    assert before in source
    accepted = source.replace(before, after)
    with scratch_working_copy(parent) as scratch:
        (scratch / filename).write_text(accepted)
        patches = project_working_copy(policy, scratch)
        assert len(patches) == 1
        child = tmp_path / "child"
        apply_patches(parent, patches, child)
        assert (child / filename).read_bytes() == (scratch / filename).read_bytes()
        assert (child / "opaque.bin").read_bytes() == b"\x00\xff"
        (scratch / "opaque.bin").write_bytes(b"\x00\xfe")
        with pytest.raises(EditOutsideMutationPointError, match="opaque.bin"):
            project_working_copy(policy, scratch)


@pytest.mark.parametrize("change", ["delete", "add", "executable", "symlink", "parent"])
def test_source_identity_changes_cannot_hide_in_a_valid_literal_edit(
    tmp_path: Path, change: str
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    source = '# zicato:mutable id="unit"\nPROMPT = "original"\n'
    (parent / "prompt.py").write_text(source)
    (parent / "support.bin").write_bytes(b"\x00\xff")
    policy = MutationPolicy.capture(parent, enumerate_mutations([parent]))
    with scratch_working_copy(parent) as scratch:
        (scratch / "prompt.py").write_text(source.replace("original", "edited"))
        match change:
            case "delete":
                (scratch / "support.bin").unlink()
            case "add":
                (scratch / "added").write_bytes(b"")
            case "executable":
                (scratch / "prompt.py").chmod(0o755)
            case "symlink":
                (scratch / "link").symlink_to("prompt.py")
            case "parent":
                (parent / "support.bin").write_bytes(b"changed")
        with pytest.raises(EditOutsideMutationPointError):
            project_working_copy(policy, scratch)


@pytest.mark.parametrize("before,after", [("original", "edited"), ("NUMBER = 1", "NUMBER = 2")])
def test_allowed_file_replacement_cannot_edit_a_forbidden_nested_operation(
    tmp_path: Path, before: str, after: str
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    source = (
        '# zicato:mutable:file id="module"\n'
        '# zicato:mutable id="protected"\nPROMPT = "original"\n'
        "NUMBER = 1\n"
    )
    (parent / "prompt.py").write_text(source)
    policy = MutationPolicy.capture(parent, enumerate_mutations([parent]), ("protected",))
    with scratch_working_copy(parent) as scratch:
        assert before in source
        (scratch / "prompt.py").write_text(source.replace(before, after))
        with pytest.raises(EditOutsideMutationPointError, match="forbidden.*protected"):
            project_working_copy(policy, scratch)


@pytest.mark.parametrize("forbidden", [False, True], ids=["unprotected", "forbidden"])
@pytest.mark.parametrize("change", ["remove", "kind"])
@pytest.mark.parametrize("narrow_selection", [False, True], ids=["whole-root", "selected-root"])
def test_whole_file_replacement_changes_only_unprotected_inner_declarations(
    tmp_path: Path, forbidden: bool, change: str, narrow_selection: bool
) -> None:
    parent = tmp_path / "parent"
    selected = parent / "selected"
    selected.mkdir(parents=True)
    marker = '# zicato:mutable:file id="module"\n'
    relative_file = "selected/prompt.py"
    (parent / relative_file).write_text(
        marker + '# zicato:mutable id="inner"\nPROMPT = "original"\n'
    )
    support = '# zicato:mutable id="inner"\nSUPPORT = "unchanged"\n'
    if narrow_selection:
        (parent / "support.py").write_text(support)
    roots = [selected] if narrow_selection else [parent]
    policy = MutationPolicy.capture(
        parent,
        enumerate_mutations(roots),
        ("inner",) if forbidden else (),
        enumeration_roots=roots,
    )
    edited = marker + (
        'PROMPT = "edited"\n'
        if change == "remove"
        else '# zicato:mutable:code id="inner"\nPROMPT = "original"\n# zicato:mutable:end\n'
    )
    whole_file_patch = Patch("patch", "module", "replace", edited, None, None, "Edit module.")
    child = tmp_path / "authoritative-child"
    apply_patches(parent, [whole_file_patch], child, enumeration_roots=roots)
    assert (child / relative_file).read_bytes() == edited.encode()
    assert policy.check_patches([whole_file_patch]) == []
    assert bool(policy.check_child(child)) is forbidden
    with scratch_working_copy(parent) as scratch:
        (scratch / relative_file).write_text(edited)
        if forbidden:
            with pytest.raises(EditOutsideMutationPointError, match="forbidden"):
                project_working_copy(policy, scratch)
        else:
            patches = project_working_copy(policy, scratch)
            assert [patch.mutation_id for patch in patches] == ["module"]
            reconstructed = tmp_path / "projected-child"
            apply_patches(parent, patches, reconstructed, enumeration_roots=roots)
            assert (reconstructed / relative_file).read_bytes() == edited.encode()
            if narrow_selection:
                assert (reconstructed / "support.py").read_text() == support


@pytest.mark.parametrize("narrow_selection", [False, True], ids=["whole-root", "selected-root"])
def test_whole_file_replacement_cannot_move_an_inner_point_into_unpatched_source(
    tmp_path: Path, narrow_selection: bool
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    marker = '# zicato:mutable:file id="module"\n'
    inner = '# zicato:mutable id="inner"\nPROMPT = "original"\n'
    (parent / "prompt.py").write_text(marker + inner)
    (parent / "support.py").write_text('SUPPORT = "unchanged"\n')
    roots = [parent / "prompt.py"] if narrow_selection else [parent]
    policy = MutationPolicy.capture(parent, enumerate_mutations(roots), enumeration_roots=roots)
    with scratch_working_copy(parent) as scratch:
        (scratch / "prompt.py").write_text(marker)
        (scratch / "support.py").write_text(inner)
        with pytest.raises(EditOutsideMutationPointError, match="support.py"):
            project_working_copy(policy, scratch)


@pytest.mark.parametrize("invalid", ["missing", "hash", "location", "metadata", "duplicate"])
def test_invalid_mutation_snapshots_never_authorize_projection(
    tmp_path: Path, invalid: str
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "prompt.py").write_text('# zicato:mutable id="unit"\nPROMPT = "original"\n')
    points = enumerate_mutations([parent])
    match invalid:
        case "missing":
            points = []
        case "hash":
            points = [replace(points[0], content_hash="0" * 64)]
        case "location":
            points = [replace(points[0], line_start=1)]
        case "metadata":
            points = [replace(points[0], metadata={"required_placeholders": "unknown"})]
        case "duplicate":
            points = points * 2
    with pytest.raises(ValueError, match="snapshot"):
        MutationPolicy.capture(parent, points)


def test_unreadable_source_is_a_finding_and_artifacts_are_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "prompt.py").write_text('# zicato:mutable id="unit"\nPROMPT = "original"\n')
    (parent / "output").mkdir()
    (parent / "output" / "ignored.bin").write_bytes(b"\xff")
    policy = MutationPolicy.capture(parent, enumerate_mutations([parent]))
    with scratch_working_copy(parent) as scratch:
        assert not (scratch / "output").exists()
        assert project_working_copy(policy, scratch) == []
        read_bytes = Path.read_bytes

        def denied(path: Path) -> bytes:
            if path == scratch / "prompt.py":
                raise PermissionError("prompt.py is unreadable")
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", denied)
        with pytest.raises(EditOutsideMutationPointError, match="unreadable"):
            project_working_copy(policy, scratch)


@pytest.mark.parametrize(
    "edited",
    [
        'RENAMED = "answer clearly"\n',
        'RENAMED = "answer concisely"\n',
        'PROMPT = "answer concisely"; ADJACENT = True\n',
    ],
    ids=["rename-only", "rename-and-literal", "adjacent-expression"],
)
def test_projection_rejects_source_the_patch_cannot_reconstruct(
    tmp_path: Path, edited: str
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    marker = '# zicato:mutable id="prompt"\n'
    (parent / "policy.py").write_text(marker + 'PROMPT = "answer clearly"\n')
    with scratch_working_copy(parent) as scratch:
        (scratch / "policy.py").write_text(marker + edited)
        with pytest.raises(EditOutsideMutationPointError):
            project_working_copy(
                MutationPolicy.capture(parent, enumerate_mutations([parent])), scratch
            )


def test_changed_binary_source_is_not_an_unchanged_copy(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "payload.bin").write_bytes(b"\xff\x00before")
    with scratch_working_copy(parent) as scratch:
        (scratch / "payload.bin").write_bytes(b"\xff\x00after")
        with pytest.raises(EditOutsideMutationPointError):
            project_working_copy(MutationPolicy.capture(parent, ()), scratch)


@pytest.mark.parametrize("single_file", [False, True], ids=["subdirectory", "single-file"])
def test_selected_roots_preserve_support_source_without_exposing_its_mutations(
    tmp_path: Path, single_file: bool
) -> None:
    parent = tmp_path / "parent"
    for name in ("editable", "support"):
        directory = parent / name
        directory.mkdir(parents=True)
        (directory / "prompt.py").write_text(f'# zicato:mutable id="{name}"\nPROMPT = "original"\n')
    selected = parent / "editable"
    if single_file:
        selected /= "prompt.py"
    policy = MutationPolicy.capture(
        parent, enumerate_mutations([selected]), enumeration_roots=[selected]
    )
    assert len(policy.parent_files) == 2
    with scratch_working_copy(parent) as scratch:
        edited = scratch / "editable" / "prompt.py"
        edited.write_text('# zicato:mutable id="editable"\nPROMPT = "edited"\n')
        assert [patch.mutation_id for patch in project_working_copy(policy, scratch)] == [
            "editable"
        ]
        (scratch / "support" / "prompt.py").write_text(
            '# zicato:mutable id="support"\nPROMPT = "edited"\n'
        )
        with pytest.raises(EditOutsideMutationPointError, match="support/prompt.py"):
            project_working_copy(policy, scratch)


@pytest.mark.parametrize("selection", ["empty", "outside", "symlink", "missing"])
def test_invalid_selected_roots_cannot_expand_mutation_permissions(
    tmp_path: Path, selection: str
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (parent / "link").symlink_to(outside, target_is_directory=True)
    roots = {
        "empty": [],
        "outside": [outside],
        "symlink": [parent / "link"],
        "missing": [parent / "absent"],
    }[selection]
    with pytest.raises(ValueError, match="roots"):
        MutationPolicy.capture(parent, (), enumeration_roots=roots)


def test_persistent_forbidden_episode_edit_cannot_return_an_experiment(tmp_path: Path) -> None:
    workspace = Workspace(
        tmp_path,
        [
            call_turn(_edit('\n# zicato:mutable id="instr"\nINSTR = "forbidden edit"\n')),
            return_turn(_HYPOTHESIS),
        ],
    )
    with pytest.raises(ProposerError):
        asyncio.run(
            workspace.agent().propose(
                workspace.context(
                    forbidden_ids=("instr",), brief_text="# Forbidden edits\n- `instr`\n"
                )
            )
        )


def test_forbidden_edit_is_repaired_before_the_accepted_source_is_committed(tmp_path: Path) -> None:
    source = (
        '# zicato:mutable id="instr"\nINSTR = "original"\n'
        '# zicato:mutable id="style"\nSTYLE = "terse"\n'
    )
    forbidden = source.replace("original", "forbidden")
    accepted = source.replace("terse", "concise")
    hypothesis = {**_HYPOTHESIS, "modulating": ["style"]}
    workspace = Workspace(
        tmp_path,
        [
            call_turn(_edit(forbidden)),
            return_turn(hypothesis),
            call_turn(_edit(accepted)),
            return_turn(hypothesis),
        ],
    )
    (workspace.tree / "prompts.py").write_text(source)
    store = GitGenerationStore(workspace.root)
    store.seed_generation("e1", "v0", [workspace.tree])
    parent = store.materialize_snapshot("e1", "v0")
    mutations = enumerate_mutations([parent])
    policy = MutationPolicy.capture(parent, mutations, ("instr",))
    mounted: dict[str, Path] = {}
    validate = build_post_apply_validator(
        genstore=store,
        epoch_id="e1",
        parent_id="v0",
        next_id="v1",
        mutations=mutations,
        beater=None,
        round_index=0,
        last_child_snapshot=mounted,
        mutation_policy=policy,
    )
    experiment = asyncio.run(
        workspace.agent().propose(
            workspace.context(
                generation_root=parent,
                mutations=tuple(mutations),
                forbidden_ids=("instr",),
                validate_experiment=validate,
            )
        )
    )
    assert [p.mutation_id for p in experiment.patches] == ["style"]
    assert store.read_file("e1", "v1", "agent/prompts.py") == accepted.encode()
    assert (mounted["path"] / "agent/prompts.py").read_bytes() == accepted.encode()
    assert store.read_file("e1", "v0", "agent/prompts.py") == source.encode()
    from tests._foe_support import request_texts

    assert "forbidden" in request_texts(workspace.episode_log())


@pytest.mark.parametrize("swap_after_validation", [False, True])
def test_custom_proposer_cannot_return_unchecked_forbidden_patches(
    tmp_path: Path, swap_after_validation: bool
) -> None:
    from tests.test_proposer_foe_agent import _EDITED_FILE
    from zicato.evolve.propose_apply import _propose_child

    workspace = Workspace(tmp_path, [call_turn(_edit(_EDITED_FILE)), return_turn(_HYPOTHESIS)])
    experiment = asyncio.run(workspace.agent().propose(workspace.context()))
    store = GitGenerationStore(workspace.root)
    store.seed_generation("e1", "v0", [workspace.tree])
    parent = store.materialize_snapshot("e1", "v0")
    mutations = enumerate_mutations([parent])
    policy = MutationPolicy.capture(parent, mutations, ("instr",))
    mounted: dict[str, Path] = {}
    validate = build_post_apply_validator(
        genstore=store,
        epoch_id="e1",
        parent_id="v0",
        next_id="v1",
        mutations=mutations,
        beater=None,
        round_index=0,
        last_child_snapshot=mounted,
        mutation_policy=policy,
    )

    class CustomProposer:
        async def propose(self, ctx):
            if swap_after_validation:
                assert not await ctx.validate_experiment(replace(experiment, patches=()))
            return experiment

    with pytest.raises(ProposerError, match="forbidden"):
        asyncio.run(
            _propose_child(
                proposer_agent=CustomProposer(),
                epoch_id="e1",
                parent_id="v0",
                next_id="v1",
                patterns=(),
                mutations=mutations,
                brief=SimpleNamespace(text="", forbidden_ids=("instr",)),
                loss_summary="",
                evaluation_call_llm=workspace.context().aux_call_llm,
                evaluation_model="",
                max_proposer_retries=1,
                workspace_root=workspace.root,
                generation_root=parent,
                validate_experiment=validate,
                meta_loop_emitter=None,
                custom_judge_names=frozenset(),
                prior_experiments=(),
                restrict_visibility=False,
                failure_profile="",
                round_index=0,
            )
        )
    assert "path" not in mounted
    if not swap_after_validation:
        assert not store.has_generation("e1", "v1")
