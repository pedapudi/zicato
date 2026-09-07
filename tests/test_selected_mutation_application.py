"""Selected mutation roots remain authoritative through source publication."""

import ast
import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from zicato.core.experiment import Experiment, HypothesisSpec
from zicato.core.mutation import Patch
from zicato.core.types import ExperimentalConfig, Generation, ScoringWeights, TournamentDecision
from zicato.epoch.containment import write_containment_manifest, write_mutation_policy
from zicato.epoch.genstore import DirectoryGenerationStore
from zicato.epoch.git_genstore import GitGenerationStore
from zicato.epoch.journal import write_experiment
from zicato.evolve.field_candidates import _append_placebo_arm
from zicato.evolve.propose_apply import _maybe_run_placebo_arm_gauntlet
from zicato.evolve.round import build_post_apply_validator, build_scratch_validator_factory
from zicato.mutation.applier import apply_patches, apply_patches_unchecked
from zicato.mutation.enumerator import enumerate_mutations
from zicato.mutation.policy import MutationPolicy, source_files
from zicato.proposer.foe_scratch import project_working_copy, scratch_working_copy
from zicato.proposer.tool_context import ProposerToolContext, bind_proposer_tool_context
from zicato.proposer.validate import validate_patches
from zicato.workspace.layout import WorkspaceLayout


@pytest.mark.parametrize("store_type", [DirectoryGenerationStore, GitGenerationStore])
@pytest.mark.parametrize(
    "boundary",
    [
        "projection",
        "generation",
        "scratch",
        "publication",
        "tool",
        "placebo-field",
        "placebo-gauntlet",
    ],
)
@pytest.mark.parametrize("selection", ["selected", "selected/prompt.py"])
def test_duplicate_outside_selected_root_cannot_redirect_accepted_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_type, boundary: str, selection: str
) -> None:
    workspace = tmp_path / ".zicato"
    source = tmp_path / "agent"
    (source / "selected").mkdir(parents=True)
    before = (
        '# zicato:mutable id="prompt" required_placeholders="{topic}"\n'
        'PROMPT = "original {topic}"\n'
    )
    support = '# zicato:mutable id="prompt"\nPROMPT = "support"\n'
    (source / "selected/prompt.py").write_text(before)
    (source / "support.py").write_text(support)
    (source / "opaque.bin").write_bytes(b"\x00\xff")
    store = store_type(workspace)
    parent = store.seed_generation("epoch", "v0", [source])
    selected = parent / "agent" / selection
    points = enumerate_mutations([selected])
    policy = MutationPolicy.capture(parent, points, enumeration_roots=[selected])
    patch = Patch(
        "patch", "prompt", "replace", '"edited {topic}"', None, None, "Clarify instruction."
    )
    experiment = Experiment(
        "experiment",
        "epoch",
        "v1",
        "v0",
        "2026-01-01T00:00:00Z",
        HypothesisSpec("Clarify instruction.", ("prompt",), "Reduce ambiguity.", (), "0"),
        (patch,),
        None,
    )
    layout = WorkspaceLayout.from_root(workspace)
    layout.epoch_dir("epoch").mkdir(parents=True, exist_ok=True)
    layout.brief("epoch").write_text("# Improve clarity\n")
    layout.scoring("epoch").write_text("{}\n")
    policy_sha256 = write_mutation_policy(
        workspace, epoch_id="epoch", parent_generation_id="v0", policy=policy
    )
    parent_files = source_files(parent)
    assert '"original {topic}"' in before
    expected = before.replace('"original {topic}"', '"edited {topic}"')
    common = dict(
        genstore=store,
        epoch_id="epoch",
        parent_id="v0",
        next_id="v1",
        mutations=points,
        beater=None,
        round_index=1,
        mutation_policy=policy,
    )
    if boundary.startswith("placebo-"):
        (workspace / "config.json").write_text(
            json.dumps(
                {
                    "generation_source_backend": "git"
                    if store_type is GitGenerationStore
                    else "directory"
                }
            )
        )
        parent_gen = Generation("v0", "epoch", None, parent, "2026-01-01T00:00:00Z")
        adapter = SimpleNamespace(mutable_subpaths=lambda root: [root / "agent" / selection])
        weights = ScoringWeights(experimental=ExperimentalConfig(random_baseline_every_n=1))
        if boundary == "placebo-field":
            field = SimpleNamespace(
                workspace_root=workspace,
                epoch_id="epoch",
                parent_id="v0",
                round_index=1,
                adapter=adapter,
                prepared=SimpleNamespace(parent_generation=parent_gen),
                weights=weights,
                field_size=2,
                mutations=points,
            )
            monkeypatch.setattr(
                "zicato.evolve.field_candidates._publish_proposing_slot", lambda *args: None
            )
            applied = []
            _append_placebo_arm(field, None, applied=applied, field_status=[], base_n=1)
            assert len(applied) == 1
            child_id = applied[0].generation_id
        else:

            async def matchup(**kwargs):
                assert kwargs["right_gen"].snapshot_root.exists()
                return SimpleNamespace(
                    outcome=SimpleNamespace(
                        decision=TournamentDecision.REJECTED,
                        delta_pass_rate=0.0,
                        delta_scalar=0.0,
                        reason="Equal loss.",
                    )
                )

            monkeypatch.setattr("zicato.tournament.runner.run_matchup", matchup)
            asyncio.run(
                _maybe_run_placebo_arm_gauntlet(
                    workspace_root=workspace,
                    epoch_id="epoch",
                    parent_id="v0",
                    parent_gen=parent_gen,
                    adapter=adapter,
                    weights=weights,
                    round_id="v1",
                    mutations=points,
                    board=[],
                    config=None,
                    disable_drift=(),
                    judge_only=False,
                    fast_mode=False,
                    round_index=1,
                    total_rounds=1,
                )
            )
            child_id = "v1-placebo"
        child = store.materialize_snapshot("epoch", child_id)
        assert (child / "agent/support.py").read_text() == support
        assert ast.dump(ast.parse((child / "agent/selected/prompt.py").read_text())) == ast.dump(
            ast.parse(before)
        )
        assert layout.experiment("epoch", child_id).is_file()
    elif boundary == "projection":
        with scratch_working_copy(parent) as scratch:
            (scratch / "agent/selected/prompt.py").write_text(expected)
            patches = project_working_copy(policy, scratch)
            assert [(p.mutation_id, p.new_content) for p in patches] == [
                ("prompt", '"edited {topic}"')
            ]
            assert (scratch / "agent/support.py").read_text() == support
    elif boundary == "generation":
        retained = {}
        validate = build_post_apply_validator(**common, last_child_snapshot=retained)
        assert asyncio.run(validate(experiment)) == []
        assert (retained["path"] / "agent/selected/prompt.py").read_text() == expected
        assert store.read_file("epoch", "v1", "agent/support.py") == support.encode()
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
    elif boundary == "scratch":
        validate, cleanup = build_scratch_validator_factory(**common)()
        try:
            assert asyncio.run(validate(experiment)) == []
            assert set(store.list_generations("epoch")) == {"v0"}
        finally:
            cleanup()
    elif boundary == "tool":
        context = ProposerToolContext(
            workspace, parent, "epoch", tuple(points), "v0", mutation_policy=policy
        )
        with bind_proposer_tool_context(context):
            report = json.loads(
                validate_patches(
                    json.dumps(
                        [
                            {
                                "id": patch.id,
                                "mutation_id": patch.mutation_id,
                                "op": patch.op,
                                "new_content": patch.new_content,
                                "rationale": patch.rationale,
                            }
                        ]
                    )
                )
            )
        assert report["ok"], report
    else:
        edited_source = tmp_path / "edited/agent"
        shutil.copytree(source, edited_source)
        (edited_source / "selected/prompt.py").write_text(expected)
        child = store.seed_generation("epoch", "v1", [edited_source])
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
        assert (child / "agent/support.py").read_text() == support
        assert (child / "agent/opaque.bin").read_bytes() == b"\x00\xff"
    assert source_files(parent) == parent_files


@pytest.mark.parametrize("apply", [apply_patches, apply_patches_unchecked])
def test_selected_roots_survive_each_operation_reenumeration(tmp_path: Path, apply) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    source = (
        '# zicato:mutable id="prompt"\nPROMPT = "original"\n'
        '# zicato:mutable id="count"\nCOUNT = 1\nLABEL = "count"\n'
        '# zicato:mutable id="choice"\nCHOICE = "original"\n'
    )
    (parent / "selected.py").write_text(source)
    (parent / "support.py").write_text(source)
    patches = [
        Patch("prompt", "prompt", "replace", '"edited"', None, None, "Clarify instruction."),
        Patch("count", "count", "set_numeric", None, 2, None, "Increase count."),
        Patch("choice", "choice", "set_enum", None, None, "edited", "Choose option."),
        Patch("repeat", "prompt", "replace", '"final"', None, None, "Clarify instruction."),
    ]
    child = tmp_path / "child"
    apply(parent, patches, child, enumeration_roots=[parent / "selected.py"])
    assert (child / "support.py").read_text() == source
    assert (child / "selected.py").read_text() == (
        '# zicato:mutable id="prompt"\nPROMPT = "final"\n'
        '# zicato:mutable id="count"\nCOUNT = 2\nLABEL = "count"\n'
        "# zicato:mutable id=\"choice\"\nCHOICE = 'edited'\n"
    )


@pytest.mark.parametrize("apply", [apply_patches, apply_patches_unchecked])
@pytest.mark.parametrize("selection", ["empty", "outside", "missing", "link"])
def test_invalid_selected_roots_fail_before_source_copy(
    tmp_path: Path, apply, selection: str
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (parent / "link").symlink_to(outside, target_is_directory=True)
    selected = {
        "empty": [],
        "outside": [outside],
        "missing": [parent / "missing"],
        "link": [parent / "link"],
    }[selection]
    child = tmp_path / "child"
    with pytest.raises(ValueError, match="inside the parent snapshot"):
        apply(parent, [], child, enumeration_roots=selected)
    assert not child.exists()
