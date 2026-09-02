"""Tests for the pre-spend wiring gate ``evolve`` runs before round 0.

Every finding is provable from the workspace alone. A finding that
proves the round cannot be measured stops the loop; a finding that
proves only that something declared contributes nothing is advisory.
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
from zicato.evolve.loop import evolve_n_rounds
from zicato.mutation.validator import duplicate_mutation_ids
from zicato.tournament.worker_transport import _WORKER_ESSENTIAL_ENV_KEYS

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
        if self.mode == "broken_subpaths":
            raise RuntimeError("no roots for you")
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
        if self.mode == "bad_integrations":
            return {"kind": "import", "integrations": "goldfive"}
        spec = {
            "kind": "import",
            "factory": "tests.test_check_gate:_make_test_adapter",
            "args": [self.mode],
        }
        if self.mode == "goldfive":
            spec["integrations"] = ["goldfive"]
        return spec


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
    include_required_goldfive: bool = True,
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
        adapter = config.get("adapter")
        if isinstance(adapter, dict):
            config["adapter"] = {**adapter, "mutable_trees": registered}
        else:
            # No adapter block at all: the pre-factory shape, where the
            # trees live at the config top level.
            config["mutable_trees"] = registered
    config = {"generation_source_backend": "directory", **config}
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    if scoring is not None or board is not None:
        scoring = dict(scoring or {})
        adapter = config.get("adapter")
        uses_builtin_goldfive_adapter = (
            isinstance(adapter, dict) and adapter.get("kind") == "adk"
        ) or bool(config.get("adk_entrypoint"))
        if uses_builtin_goldfive_adapter and include_required_goldfive:
            scoring.setdefault("goldfive", {})
        epoch = root / "epochs" / _EPOCH
        epoch.mkdir(parents=True, exist_ok=True)
        (epoch / "scoring.json").write_text(json.dumps(scoring), encoding="utf-8")
        (root.parent / "scoring.json").write_text(json.dumps(scoring), encoding="utf-8")
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


def _codes(root: Path, *, live_contract: bool = True, **kwargs: object) -> set[str]:
    """Codes from one gate run, defaulting to the live-contract path.

    ``live_contract=True`` is what ``evolve_n_rounds`` passes whenever no
    explicit ``--epoch`` was given, which is the common path; the frozen
    variant is asked for by name.
    """
    with CheckContext(root, live_contract=live_contract, **kwargs) as ctx:  # type: ignore[arg-type]
        return {f.code for f in build_report(ctx).findings}


def _findings(root: Path, *, live_contract: bool = True, **kwargs: object) -> dict[str, bool]:
    """Map each reported code to whether it stops the run."""
    with CheckContext(root, live_contract=live_contract, **kwargs) as ctx:  # type: ignore[arg-type]
        return {f.code: f.blocking for f in build_report(ctx).findings}


def _entry(entry_id: str, **extra: object) -> dict:
    return {
        "id": entry_id,
        "kind": "single_turn",
        "input": "hi",
        "wall_clock_budget_seconds": 30,
        **extra,
    }


def _write_epoch_implementation_identity(root: Path, identity: object) -> None:
    epoch_config = root / "epochs" / _EPOCH / "config.json"
    epoch_config.write_text(
        json.dumps({"implementation_identity": identity}),
        encoding="utf-8",
    )


def test_frozen_epoch_requires_readable_implementation_identity(tmp_path: Path) -> None:
    root = _workspace(tmp_path / ".zicato", scoring={}, board=[_entry("a")])
    assert "epoch_implementation_identity_unreadable" in _codes(root, live_contract=False)


def test_frozen_epoch_refuses_a_different_zicato_evaluator(tmp_path: Path) -> None:
    root = _workspace(tmp_path / ".zicato", scoring={}, board=[_entry("a")])
    _write_epoch_implementation_identity(root, {"zicato_evaluator_revision": 0})
    assert "epoch_implementation_identity_mismatch" in _codes(root, live_contract=False)


def test_frozen_goldfive_epoch_refuses_a_different_goldfive_build(tmp_path: Path) -> None:
    from zicato.integrations.goldfive import ZICATO_GOLDFIVE_INTEGRATION_REVISION

    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
                "args": ["goldfive"],
            }
        },
        scoring={"goldfive": {}},
        board=[_entry("a")],
    )
    _write_epoch_implementation_identity(
        root,
        {
            "zicato_evaluator_revision": 1,
            "goldfive_version": "git:different-build",
            "zicato_goldfive_integration_revision": ZICATO_GOLDFIVE_INTEGRATION_REVISION,
        },
    )
    assert "epoch_implementation_identity_mismatch" in _codes(root, live_contract=False)


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


def test_malformed_adapter_integration_capabilities_are_a_hard_stop(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
                "args": ["bad_integrations"],
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
    from zicato.epoch.genstore import default_generation_store
    from zicato.evolve.generation_phase import set_current_generation

    snapshot = default_generation_store(root).snapshot_path(_EPOCH, "v0")
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


def test_a_weight_naming_no_board_judge_is_not_a_defect(tmp_path: Path) -> None:
    """``per_judge_weights`` is not scoped to board judges.

    ``scoring.builtins`` resolves telemetry ``custom:<name>`` kinds through
    the same mapping, so a key legitimately names an in-harness process
    judge that no board entry declares; the empty-string key weights the
    bare ``custom`` kind; and ``reflection.findings`` recommends a
    ``{name: 0.0}`` entry as the reversible way to retire a judge. Every
    one of those workspaces runs correctly, so none may be reported.
    """
    judged = _entry(
        "judged",
        judges=[
            {"name": "real_judge", "mode": "inline", "body": "is it good", "severity": "warning"}
        ],
    )
    root = _workspace(
        tmp_path / ".zicato",
        board=[judged],
        scoring={"per_judge_weights": {"in_harness_judge": 2.0, "": 1.0, "retired": 0.0}},
    )
    codes = _codes(root)
    assert "weight_for_absent_judge" not in codes
    assert "no_expectations" not in codes


def test_an_unreadable_board_is_reported_once(tmp_path: Path) -> None:
    """The parse failure, not the empty board it leaves behind."""
    root = _workspace(tmp_path / ".zicato", board=[], scoring={})
    (root.parent / "board.jsonl").write_text("{not json", encoding="utf-8")
    codes = _codes(root)
    assert "board_unreadable" in codes
    assert "empty_board" not in codes


@pytest.mark.parametrize(
    "contents",
    ["{not json", "[]", json.dumps({"pass_exponent": 2})],
)
def test_malformed_scoring_is_a_hard_stop(tmp_path: Path, contents: str) -> None:
    """Both contract paths: the live file evolve auto-epochs, and the frozen one."""
    root = _workspace(tmp_path / ".zicato", board=[_entry("drift-only")], scoring={})
    (root.parent / "scoring.json").write_text(contents, encoding="utf-8")
    assert "scoring_unreadable" in _codes(root)

    (root / "epochs" / _EPOCH / "scoring.json").write_text(contents, encoding="utf-8")
    assert "scoring_unreadable" in _codes(root, live_contract=False)


# --- advisory: a declared thing that contributes nothing --------------------


@pytest.mark.parametrize(
    ("filename", "body", "reason"),
    [
        # A bare span marker in a file with no AST to bind to.
        ("notes.md", '<!-- zicato:mutable id="orphan" -->\nsome text\n', "not Python"),
        # A span marker in a Python file with no literal beneath it.
        ("tail.py", '# zicato:mutable id="orphan"\nCOUNT = 3\n', "no string literal"),
    ],
)
def test_a_span_marker_that_binds_to_nothing_is_advisory(
    tmp_path: Path, filename: str, body: str, reason: str
) -> None:
    """Both ways a span marker fails to bind, and neither stops the run.

    The file still looks marked up while contributing no point, which is
    worth saying. It is not worth refusing a workspace over: the run is
    unaffected beyond having one fewer mutation point than the markup
    suggests, and these workspaces run today.
    """
    del reason
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
    (tmp_path / "harness" / filename).write_text(body, encoding="utf-8")
    assert _findings(root)["unbound_span_marker"] is False


def test_an_unbound_marker_is_reported_from_enumerator_facts(tmp_path: Path) -> None:
    """The id, the location, and the reason — not a scrape of a log line."""
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
    (tmp_path / "harness" / "notes.md").write_text(
        '<!-- zicato:mutable id="orphan" -->\nsome text\n', encoding="utf-8"
    )
    with CheckContext(root, live_contract=True) as ctx:
        finding = next(f for f in build_report(ctx).findings if f.code == "unbound_span_marker")
    assert finding.detail["mutation_id"] == "orphan"
    assert finding.detail["location"].endswith("notes.md:1")
    assert "not Python" in finding.detail["reason"]


def test_a_tree_contributing_no_point_is_advisory(tmp_path: Path) -> None:
    """One dead tree among live ones is a stale path, not an empty surface.

    The proposer still has surface to edit, so the loop still learns and
    the run must not be refused.
    """
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
    findings = _findings(root)
    assert findings["tree_enumerates_to_nothing"] is False
    assert "empty_mutation_surface" not in findings


def test_a_missing_declared_tree_is_advisory(tmp_path: Path) -> None:
    """A stale path alongside a live one does not stop a run that works."""
    root = tmp_path / ".zicato"
    live = tmp_path / "live"
    live.mkdir()
    (live / "a.py").write_text(_MUTABLE.format(point_id="p"), encoding="utf-8")
    _workspace(
        root,
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [str(live), str(tmp_path / "gone")],
            }
        },
    )
    assert _findings(root)["missing_mutable_tree"] is False


def test_advisories_alone_do_not_raise(tmp_path: Path) -> None:
    """The whole point of the tier: these workspaces still start."""
    from zicato.check import require_workspace_valid

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
    (tmp_path / "harness" / "notes.md").write_text(
        '<!-- zicato:mutable id="orphan" -->\nsome text\n', encoding="utf-8"
    )
    report = require_workspace_valid(root, live_contract=True)
    assert [f.code for f in report.advisories] == ["unbound_span_marker"]
    assert report.blocking == ()


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


# --- the probe runs the way a worker would ---------------------------------


def test_the_probe_runs_under_the_scrubbed_worker_env(tmp_path: Path) -> None:
    """Worker-equivalence, in the direction that matters.

    A workspace that opts into ``runtime.scrub_worker_env`` gives its
    workers a minimal explicit env. An adapter needing a variable the
    scrub drops must fail here, not in every worker mid-round — so the
    probe is handed the same composed env rather than inheriting the
    orchestrator's.
    """
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
            },
            "runtime": {"scrub_worker_env": True, "worker_env_passthrough": ["EXTRA_TARGET_VAR"]},
        },
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    with CheckContext(root, live_contract=True) as ctx:
        env = ctx.worker_env
    assert env is not None
    assert "EXTRA_TARGET_VAR" not in env  # named but unset: never invented
    assert set(env) <= {*_WORKER_ESSENTIAL_ENV_KEYS, "EXTRA_TARGET_VAR"}


def test_an_unscrubbed_workspace_inherits_the_environment(tmp_path: Path) -> None:
    """The default is full inheritance, and the probe must match it."""
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
            }
        },
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    with CheckContext(root, live_contract=True) as ctx:
        assert ctx.worker_env is None


def test_a_probe_that_never_returns_is_bounded(tmp_path: Path, monkeypatch) -> None:
    """The bound holds even when a grandchild inherits the output pipes.

    ``subprocess.run(timeout=...)`` keeps waiting on those pipes after the
    timeout fires, so the gate would hang for as long as the grandchild
    lives. The probe gets its own process group and the group is killed.
    """
    import time

    from zicato.check import validators

    monkeypatch.setattr(validators, "_IMPORT_TIMEOUT_S", 1)
    monkeypatch.setattr(
        validators,
        "_IMPORT_PROBE",
        # Leave a grandchild holding stdout/stderr, then hang.
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n",
    )
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
            }
        },
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    started = time.monotonic()
    assert "adapter_import_timeout" in _codes(root)
    assert time.monotonic() - started < 20


# --- configured model roles must resolve in a worker -----------------------


def test_a_role_whose_credential_is_unset_is_a_hard_stop(tmp_path: Path, monkeypatch) -> None:
    """A configured role wins over the CLI callable, and fails in the worker.

    ``build_adk_model`` raises when the named variable is unset, and a
    scrubbed worker env can only forward a variable the orchestrator's
    own environment already holds — so absence here is absence there.
    """
    monkeypatch.delenv("ZICATO_TEST_MISSING_KEY", raising=False)
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            },
            "models": _models(
                engines={
                    "target": {
                        "model": "some-model",
                        "endpoint": "https://example.invalid",
                        "api_key_env": "ZICATO_TEST_MISSING_KEY",
                    }
                },
                target="target",
            ),
        },
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    findings = _findings(root)
    assert findings["model_role_credential_unset"] is True


def test_a_role_whose_credential_is_set_is_clean(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZICATO_TEST_PRESENT_KEY", "not-a-real-secret")
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            },
            "models": _models(
                engines={
                    "target": {
                        "model": "some-model",
                        "endpoint": "https://example.invalid",
                        "api_key_env": "ZICATO_TEST_PRESENT_KEY",
                    }
                },
                target="target",
            ),
        },
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "model_role_credential_unset" not in _codes(root)


def test_a_goldfive_endpoint_credential_must_exist_before_workers_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ZICATO_TEST_MISSING_GOLDFIVE_KEY", raising=False)
    root = _workspace(
        tmp_path / ".zicato",
        config={"adapter": {"kind": "adk", "entrypoint": _VALID_ADK_ENTRYPOINT}},
        scoring={
            "goldfive": {
                "judge": {
                    "base_url": "http://judge.example",
                    "api_key_env": "ZICATO_TEST_MISSING_GOLDFIVE_KEY",
                }
            }
        },
        board=[_entry("e0")],
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "goldfive_credential_unset" in _codes(root)


def test_builtin_adk_declares_goldfive_and_requires_the_contract_block(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            }
        },
        scoring={},
        board=[_entry("e0")],
        trees={"harness": _MUTABLE.format(point_id="p")},
        include_required_goldfive=False,
    )
    assert "goldfive_config_missing" in _codes(root)


def test_a_generic_contract_does_not_require_goldfive(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
                "args": [],
            }
        },
        scoring={},
        board=[_entry("e0")],
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "goldfive_config_missing" not in _codes(root)


def test_an_import_adapter_can_declare_goldfive_without_a_kind_check(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
                "args": ["goldfive"],
            }
        },
        scoring={},
        board=[_entry("e0")],
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "goldfive_config_missing" in _codes(root)


def test_a_goldfive_declaring_adapter_requires_the_optional_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zicato.check.validators as validators

    monkeypatch.setattr(validators, "_module_importable", lambda _name: False)
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
                "args": ["goldfive"],
            }
        },
        scoring={"goldfive": {}},
        board=[_entry("e0")],
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "goldfive_runtime_unavailable" in _codes(root)


def test_invalid_goldfive_config_is_reported_when_worker_environment_is_scrubbed(
    tmp_path: Path,
) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {"kind": "adk", "entrypoint": _VALID_ADK_ENTRYPOINT},
            "runtime": {"scrub_worker_env": True},
        },
        scoring={"goldfive": {"unknown_setting": True}},
        board=[_entry("e0")],
        trees={"harness": _MUTABLE.format(point_id="p")},
    )

    assert "goldfive_config_invalid" in _codes(root)


def test_goldfive_local_embedding_requires_its_named_install_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zicato.integrations.goldfive as integration

    monkeypatch.setattr(
        integration,
        "missing_runtime_capabilities",
        lambda _config: ("local_embedding",),
    )
    root = _workspace(
        tmp_path / ".zicato",
        config={"adapter": {"kind": "adk", "entrypoint": _VALID_ADK_ENTRYPOINT}},
        scoring={"goldfive": {"reasoning_drift": {"mode": "embedding"}}},
        board=[_entry("e0")],
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "goldfive_runtime_capability_missing" in _codes(root)


def test_goldfive_judge_endpoint_requires_its_http_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zicato.integrations.goldfive as integration

    monkeypatch.setattr(
        integration,
        "missing_runtime_capabilities",
        lambda _config: ("remote_judge",),
    )
    root = _workspace(
        tmp_path / ".zicato",
        config={"adapter": {"kind": "adk", "entrypoint": _VALID_ADK_ENTRYPOINT}},
        scoring={"goldfive": {"judge": {"base_url": "http://judge.example"}}},
        board=[_entry("e0")],
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "goldfive_runtime_capability_missing" in _codes(root)


def test_goldfive_implementation_must_match_the_executed_evaluator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zicato.integrations.goldfive as integration

    monkeypatch.setattr(
        integration,
        "installed_goldfive_implementation_version",
        lambda: "git:" + "0" * 40,
    )
    root = _workspace(
        tmp_path / ".zicato",
        config={"adapter": {"kind": "adk", "entrypoint": _VALID_ADK_ENTRYPOINT}},
        scoring={"goldfive": {}},
        board=[_entry("e0")],
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "goldfive_implementation_mismatch" in _codes(root)


def test_a_present_goldfive_endpoint_credential_passes_the_static_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZICATO_TEST_GOLDFIVE_KEY", "not-a-real-secret")
    root = _workspace(
        tmp_path / ".zicato",
        config={"adapter": {"kind": "adk", "entrypoint": _VALID_ADK_ENTRYPOINT}},
        scoring={
            "goldfive": {
                "embedding": {
                    "base_url": "http://embedding.example",
                    "api_key_env": "ZICATO_TEST_GOLDFIVE_KEY",
                }
            }
        },
        board=[_entry("e0")],
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "goldfive_credential_unset" not in _codes(root)


def test_a_generic_adapter_rejects_an_unused_goldfive_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GENERIC_UNUSED_GOLDFIVE_KEY", "must-not-reach-worker")
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
                "args": [],
            },
            "runtime": {"scrub_worker_env": True},
        },
        scoring={
            "goldfive": {
                "judge": {
                    "base_url": "http://judge.example",
                    "api_key_env": "GENERIC_UNUSED_GOLDFIVE_KEY",
                }
            }
        },
        board=[_entry("e0")],
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "goldfive_config_unused" in _codes(root)
    with CheckContext(root, live_contract=True) as ctx:
        assert ctx.worker_env is not None
        assert "GENERIC_UNUSED_GOLDFIVE_KEY" not in ctx.worker_env


def test_a_role_call_llm_that_does_not_import_is_a_hard_stop(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            },
            "models": _models(
                engines={"target": {"call_llm": "no_such_module:call_llm"}},
                target="target",
            ),
        },
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    assert "model_role_unresolvable" in _codes(root)


def test_an_unconfigured_role_is_not_reported(tmp_path: Path) -> None:
    """It falls back to the callable the caller passed, which the CLI resolves."""
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "adk",
                "entrypoint": _VALID_ADK_ENTRYPOINT,
                "mutable_trees": [],
            }
        },
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    codes = _codes(root)
    assert not any(code.startswith("model_role") for code in codes)


# --- an adapter that cannot resolve its roots names the cause ---------------


def test_an_adapter_that_raises_resolving_roots_names_the_cause(tmp_path: Path) -> None:
    """Reporting the empty surface alone would name the symptom, not the fix."""
    root = _workspace(
        tmp_path / ".zicato",
        config={
            "adapter": {
                "kind": "import",
                "factory": "tests.test_check_gate:_make_test_adapter",
                "args": ["broken_subpaths"],
            }
        },
        trees={"harness": _MUTABLE.format(point_id="p")},
    )
    with CheckContext(root, live_contract=True) as ctx:
        finding = next(
            f for f in build_report(ctx).findings if f.code == "mutable_trees_unresolvable"
        )
    assert "no roots for you" in finding.detail["error"]


# --- the exported single-round entry point is a spend boundary too ---------


def test_evolve_once_gates_its_own_workspace(tmp_path: Path) -> None:
    """``zicato.orchestrator.evolve_once`` spends a full round on its own.

    Gating only ``evolve_n_rounds`` would leave the exported single-round
    call as a way past the gate for a library caller — and it is the call
    the mock-evolve parity capture drives.
    """
    from zicato.orchestrator import evolve_once

    root = _workspace(tmp_path / ".zicato", config={"models": _models()})
    with pytest.raises(WorkspaceCheckError):
        asyncio.run(
            evolve_once(
                workspace_root=root,
                harness_call_llm=_harness_call_llm,
                auxiliary_call_llm=_auxiliary_call_llm,
            )
        )


def test_a_multi_round_invocation_pays_for_the_gate_once(tmp_path: Path, monkeypatch) -> None:
    """The loop gates up front and tells each round it already did."""
    import zicato.check

    calls: list[object] = []
    real = zicato.check.require_workspace_valid
    monkeypatch.setattr(
        zicato.check,
        "require_workspace_valid",
        lambda *a, **k: (calls.append(a), real(*a, **k))[1],
    )
    root = _workspace(tmp_path / ".zicato", config={"models": _models()})
    with pytest.raises(WorkspaceCheckError):
        asyncio.run(
            evolve_n_rounds(
                rounds=3,
                workspace_root=root,
                harness_call_llm=_harness_call_llm,
                auxiliary_call_llm=_auxiliary_call_llm,
            )
        )
    assert len(calls) == 1
