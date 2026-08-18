"""Tests for the pre-spend wiring gate ``evolve`` runs before round 0.

Every finding is provable from the workspace alone and every one of
them stops the loop; there is no advisory tier.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from zicato.check import CheckContext, WorkspaceCheckError, build_report
from zicato.cli.commands.evolve import evolve_cmd
from zicato.core.types import ScoringWeights
from zicato.mutation.validator import duplicate_mutation_ids

_EPOCH = "ep_test"
_DEFAULTS = ScoringWeights()
_VALID_ADK_ENTRYPOINT = "tests.test_check_gate:_FAKE_AGENT"
_FAKE_AGENT = object()


class _TestAdapter:
    name = "test"
    run_output_names: tuple[str, ...] = ()

    def __init__(self, mode: str = "normal") -> None:
        self.mode = mode

    def mutable_subpaths(self, generation_root: Path) -> list[Path]:
        if self.mode == "narrow":
            return [generation_root / "bundle" / "editable"]
        return [generation_root]

    def load(self, generation_root: Path) -> object:
        if self.mode == "broken_load":
            raise RuntimeError(f"refusing snapshot {generation_root}")
        return object()

    def mutation_points(self, source_roots: list[Path] | None = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        if self.mode == "bad_spec":
            return {"kind": "not-a-worker-adapter"}
        return {
            "kind": "import",
            "factory": "tests.test_check_gate:_make_test_adapter",
            "args": [self.mode],
        }


def _make_test_adapter(mode: str = "normal") -> _TestAdapter:
    return _TestAdapter(mode)


async def _harness_call_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


async def _auxiliary_call_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


_MUTABLE = """\
# zicato:mutable:file id="{point_id}"
PROMPT = "hello"
"""


def _workspace(
    root: Path,
    *,
    config: dict | None = None,
    scoring: dict | None = None,
    board: list[dict] | None = None,
    trees: dict[str, str] | None = None,
) -> Path:
    """Write a workspace: config.json, one epoch, and any mutable trees."""
    root.mkdir(parents=True, exist_ok=True)
    for name, body in (trees or {}).items():
        tree = root.parent / name
        tree.mkdir(parents=True, exist_ok=True)
        (tree / "harness.py").write_text(body, encoding="utf-8")
    config = dict(config or {})
    if trees:
        registered = [str(root.parent / name) for name in trees]
        config["mutable_trees"] = registered
        if isinstance(adapter := config.get("adapter"), dict):
            config["adapter"] = {**adapter, "mutable_trees": registered}
    elif isinstance(adapter := config.get("adapter"), dict) and adapter.get("mutable_trees"):
        config["mutable_trees"] = list(adapter["mutable_trees"])
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if scoring is not None or board is not None:
        epoch = root / "epochs" / _EPOCH
        epoch.mkdir(parents=True, exist_ok=True)
        (epoch / "scoring.json").write_text(json.dumps(scoring or {}), encoding="utf-8")
        (root.parent / "scoring.json").write_text(json.dumps(scoring or {}), encoding="utf-8")
        if board is not None:
            rendered_board = "\n".join(json.dumps(row) for row in board)
            (epoch / "board.jsonl").write_text(rendered_board, encoding="utf-8")
            (root.parent / "board.jsonl").write_text(rendered_board, encoding="utf-8")
        (root / "current_epoch").write_text(_EPOCH, encoding="utf-8")
    return root


def _models(engines: dict | None = None, **roles: str) -> dict:
    return {
        "engines": engines
        or {"target": {"model": "target-model"}, "evaluation": {"model": "evaluation-model"}},
        "roles": dict(roles),
    }


def _codes(root: Path, **kwargs: object) -> set[str]:
    with CheckContext(root, **kwargs) as ctx:  # type: ignore[arg-type]
        return {f.code for f in build_report(ctx).findings}


def _entry(entry_id: str, **extra: object) -> dict:
    return {
        "id": entry_id,
        "kind": "single_turn",
        "input": "hi",
        "wall_clock_budget_seconds": 30,
        **extra,
    }


# --- hard stop: duplicate mutation ids --------------------------------------


def test_the_shared_helper_reports_only_ids_seen_more_than_once(tmp_path: Path) -> None:
    tree = tmp_path / "harness"
    tree.mkdir()
    (tree / "a.py").write_text(_MUTABLE.format(point_id="shared"), encoding="utf-8")
    (tree / "b.py").write_text(_MUTABLE.format(point_id="shared"), encoding="utf-8")
    (tree / "c.py").write_text(_MUTABLE.format(point_id="unique"), encoding="utf-8")

    from zicato.mutation.enumerator import enumerate_mutations

    collisions = duplicate_mutation_ids(enumerate_mutations([tree]))
    assert set(collisions) == {"shared"}
    assert len(collisions["shared"]) == 2


def test_a_duplicated_id_anywhere_on_the_surface_is_a_hard_stop(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            }
        },
        trees={"harness": _MUTABLE.format(point_id="dup")},
    )
    (tmp_path / "harness" / "second.py").write_text(
        _MUTABLE.format(point_id="dup"), encoding="utf-8"
    )
    assert "duplicate_mutation_id" in _codes(root)


# --- hard stop: a surface the proposer cannot edit --------------------------


def test_no_declared_trees_is_a_hard_stop(tmp_path: Path) -> None:
    root = _workspace(tmp_path / ".zicato", config={"models": _models()})
    assert "no_mutable_trees" in _codes(root)


def test_a_tree_that_enumerates_to_nothing_is_a_hard_stop(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            }
        },
        trees={"harness": "PROMPT = 'no markers here'\n"},
    )
    assert "empty_mutation_surface" in _codes(root)


def test_a_missing_tree_is_named(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": ["/nonexistent/gone"],
            }
        },
    )
    assert "missing_mutable_tree" in _codes(root)


# --- hard stop: the adapter must rebuild in a worker ------------------------


def test_an_adapter_that_cannot_be_rebuilt_in_a_subprocess_is_a_hard_stop(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={"adapter": {"kind": "import", "factory": "no_such_module:make"}},
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "adapter_import_failed" in _codes(root)


def test_no_adapter_at_all_is_a_hard_stop(tmp_path: Path) -> None:
    root = _workspace(tmp_path / ".zicato", config={"models": _models()})
    assert "no_adapter" in _codes(root)


def test_an_adk_entrypoint_that_cannot_load_is_a_hard_stop(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={"adapter": {"kind": "adk", "entrypoint": "no_such_module:agent"}},
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "adapter_import_failed" in _codes(root)


def test_fresh_workspace_probe_calls_adapter_load_on_ephemeral_v0(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
                "args": ["broken_load"],
            }
        },
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "adapter_import_failed" in _codes(root)


def test_probe_uses_the_adapters_canonical_worker_spec(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
                "args": ["bad_spec"],
            }
        },
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "adapter_import_failed" in _codes(root)


def test_mutation_checks_use_the_reigning_generation_snapshot(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            }
        },
        board=[_entry("e1", expectation={"kind": "expected_text", "spec": "hi"})],
        scoring={},
        trees={"harness": _MUTABLE.format(point_id="source_only")},
    )
    from zicato.evolve.generation_phase import set_current_generation, snapshot_root

    snapshot = snapshot_root(root, _EPOCH, "v0")
    snap_tree = snapshot / "harness"
    snap_tree.mkdir(parents=True)
    (snap_tree / "a.py").write_text(_MUTABLE.format(point_id="dup"), encoding="utf-8")
    (snap_tree / "b.py").write_text(_MUTABLE.format(point_id="dup"), encoding="utf-8")
    set_current_generation(root, _EPOCH, "v0")

    ctx = CheckContext(root)
    assert ctx.generation_snapshot == snapshot
    assert all(point.file.is_relative_to(snapshot) for point in ctx.surface)
    assert "duplicate_mutation_id" in _codes(root)


def test_declared_text_syntax_is_used_for_surface_enumeration(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={"adapter": {"kind": "adk", "entrypoint": _VALID_ADK_ENTRYPOINT}},
        scoring={"mutation_surface": {".ts": {"leaders": ["//"]}}},
        board=[_entry("drift-only")],
        trees={"harness": "const prompt = 'hello';\n"},
    )
    (tmp_path / "harness" / "prompt.ts").write_text(
        '// zicato:mutable:file id="typescript_prompt"\nconst prompt = "hello";\n',
        encoding="utf-8",
    )

    with CheckContext(root) as ctx:
        assert [point.id for point in ctx.surface] == ["typescript_prompt"]
    assert "empty_mutation_surface" not in _codes(root)


def test_adapter_scoping_excludes_markers_outside_runtime_surface(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
                "args": ["narrow"],
            }
        },
        trees={"bundle": _MUTABLE.format(point_id="support_only")},
    )
    editable = tmp_path / "bundle" / "editable"
    editable.mkdir()
    (editable / "plain.py").write_text("PROMPT = 'unmarked'\n", encoding="utf-8")

    assert "empty_mutation_surface" in _codes(root)


# --- hard stop: the board and the scoring must agree ------------------------


def test_a_drift_only_board_is_a_valid_evaluation_contract(tmp_path: Path) -> None:
    root = _workspace(tmp_path / ".zicato", board=[_entry("solo")], scoring={})
    assert "no_expectations" not in _codes(root)


def test_a_weight_for_a_judge_no_entry_declares_is_a_hard_stop(tmp_path: Path) -> None:
    judged = _entry(
        "judged",
        judges=[
            {"name": "real_judge", "mode": "inline", "body": "is it good", "severity": "warning"}
        ],
    )
    root = _workspace(
        tmp_path / ".zicato", board=[judged], scoring={"per_judge_weights": {"ghost": 2.0}}
    )
    codes = _codes(root)
    assert "weight_for_absent_judge" in codes
    assert "no_expectations" not in codes


def test_an_unreadable_board_is_reported_once(tmp_path: Path) -> None:
    root = _workspace(tmp_path / ".zicato", board=[], scoring={})
    (root / "epochs" / _EPOCH / "board.jsonl").write_text("{not json", encoding="utf-8")
    codes = _codes(root)
    assert "board_unreadable" in codes
    assert "empty_board" not in codes


@pytest.mark.parametrize(
    "contents",
    ["{not json", "[]", json.dumps({"pass_exponent": 2})],
)
def test_malformed_scoring_is_a_hard_stop(tmp_path: Path, contents: str) -> None:
    root = _workspace(tmp_path / ".zicato", board=[_entry("drift-only")], scoring={})
    (root / "epochs" / _EPOCH / "scoring.json").write_text(contents, encoding="utf-8")
    assert "scoring_unreadable" in _codes(root)


def test_a_span_marker_that_binds_to_nothing_is_a_hard_stop(tmp_path: Path) -> None:
    """The enumerator already detects this — into a log nobody reads first.

    A bare span marker binds to a Python string literal, so one in a
    text file contributes no point. The file still looks marked up.
    """
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            }
        },
        trees={"harness": _MUTABLE.format(point_id="real")},
    )
    notes = tmp_path / "harness" / "notes.md"
    notes.write_text('<!-- zicato:mutable id="orphan" -->\nsome text\n', encoding="utf-8")
    assert "unbound_span_marker" in _codes(root)


def test_a_tree_contributing_no_point_is_named_individually(tmp_path: Path) -> None:
    """One dead tree among live ones is a stale path, not an empty surface."""
    root = tmp_path / ".zicato"
    live = tmp_path / "live"
    dead = tmp_path / "dead"
    live.mkdir()
    dead.mkdir()
    (live / "a.py").write_text(_MUTABLE.format(point_id="p"), encoding="utf-8")
    (dead / "b.py").write_text("PROMPT = 'unmarked'\n", encoding="utf-8")
    _workspace(
        root,
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [str(live), str(dead)],
            }
        },
    )
    codes = _codes(root)
    assert "tree_enumerates_to_nothing" in codes
    assert "empty_mutation_surface" not in codes


def test_a_predicate_that_does_not_import_is_a_hard_stop(tmp_path: Path) -> None:
    entry = _entry("e1", expectation={"kind": "predicate", "spec": "no_such_mod:check"})
    root = _workspace(tmp_path / ".zicato", board=[entry], scoring={})
    assert "predicate_unresolvable" in _codes(root)


def test_a_python_judge_that_does_not_import_is_a_hard_stop(tmp_path: Path) -> None:
    judge = {
        "name": "shape",
        "mode": "python",
        "body": "no_such_mod:judge",
        "severity": "warning",
    }
    entry = _entry("e1", judges=[judge])
    root = _workspace(tmp_path / ".zicato", board=[entry], scoring={})
    assert "judge_unresolvable" in _codes(root)


def test_every_defect_is_reported_not_just_the_first(tmp_path: Path) -> None:
    """First-fail would make an operator rediscover the next one each round."""
    root = _workspace(tmp_path / ".zicato", config={}, board=[_entry("e1")], scoring={})
    codes = _codes(root)
    assert {"no_mutable_trees", "no_adapter"} <= codes


# --- the gate on evolve ----------------------------------------------------


def _evolve(root: Path, *args: str):
    return CliRunner().invoke(
        evolve_cmd,
        [
            "--workspace",
            str(root),
            "--harness-call-llm",
            "tests.test_check_gate:_harness_call_llm",
            "--auxiliary-call-llm",
            "tests.test_check_gate:_auxiliary_call_llm",
            "--no-dashboard",
            *args,
        ],
    )


def test_evolve_refuses_to_spend_a_round_on_a_broken_workspace(tmp_path: Path) -> None:
    """The gate runs with no flag: a defect stops the loop before round 0."""
    root = _workspace(tmp_path / ".zicato", config={"models": _models()})
    result = _evolve(root)
    assert result.exit_code != 0
    assert "[ERROR]" in result.output
    assert "would make this round unmeasurable" in result.output


def test_public_evolve_loop_runs_gate_before_any_model_or_auto_epoch(tmp_path: Path) -> None:
    from zicato.evolve.loop import evolve_n_rounds

    root = _workspace(tmp_path / ".zicato", config={"models": _models()})
    calls: list[str] = []

    async def call_llm(system: str, user: str, model: str) -> str:
        del system, user, model
        calls.append("model")
        return ""

    with pytest.raises(WorkspaceCheckError):
        asyncio.run(
            evolve_n_rounds(
                rounds=1,
                workspace_root=root,
                harness_call_llm=call_llm,
                auxiliary_call_llm=call_llm,
            )
        )
    assert calls == []


def test_a_clean_workspace_dry_runs_to_zero_without_spending(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            }
        },
        board=[_entry("e1", expectation={"kind": "expected_text", "spec": "hi"})],
        scoring={},
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    result = _evolve(root, "--dry-run")
    assert result.exit_code == 0
    assert "Nothing was spent." in result.output
    assert "1 board entry" in result.output
    assert "1 mutation point" in result.output


def test_implicit_evolve_accepts_a_live_drift_only_contract(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            }
        },
        board=[_entry("frozen", expectation={"kind": "expected_text", "spec": "hi"})],
        scoring={},
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    (root.parent / "board.jsonl").write_text(json.dumps(_entry("live-ungraded")), encoding="utf-8")

    result = _evolve(root, "--dry-run")
    assert result.exit_code == 0
    assert "1 board entry" in result.output


def test_dry_run_rejects_an_unresolvable_llm_callable(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            }
        },
        board=[_entry("e1", expectation={"kind": "expected_text", "spec": "hi"})],
        scoring={},
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    result = CliRunner().invoke(
        evolve_cmd,
        [
            "--workspace",
            str(root),
            "--harness-call-llm",
            "no_such_module:harness",
            "--auxiliary-call-llm",
            "tests.test_check_gate:_auxiliary_call_llm",
            "--dry-run",
        ],
    )
    assert result.exit_code != 0
    assert "no_such_module" in result.output
