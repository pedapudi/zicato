"""Triage pins for the two ADK-adapter silent-no-op defects.

* **Issue #110** — :meth:`zicato.adapters.adk.ADKHarnessAdapter.load` inserts
  the generation snapshot at the front of ``sys.path`` and re-imports the
  entrypoint, but never verifies that the MUTATED TREE is what runs. When a
  tree's TOP-LEVEL name is already importable from elsewhere, the insert
  cannot shadow it: the installed copy wins, the snapshot is ignored, and
  every mutation the loop applies is a no-op that still scores, gates,
  promotes and reports.
* **Issue #98** — :func:`zicato.adapters.adk._resolves_to_native_function_calling`
  classifies function-calling capability as ``issubclass(cls, LiteLlm)``, so a
  bare native ``gemini-*`` / ``gemma-*`` model is judged incapable and gets
  rebound to the TEXT-ONLY ``call_llm`` shim, which strips every tool.

Both defects are FIXED; the pins below are live regression tests (the
``xfail(strict=True)`` markers the triage landed them under were removed as
each fix landed). The #110 half is verified in three layers — a lexical
register-time refusal, a per-tree resolution assert at load, and a post-run
``sys.modules`` inspection — because the invariant is "the mutated tree is what
runs", which the entrypoint's own origin only answers when the entrypoint lives
inside a tree. The #98 half classifies capability on registry-resolvability so a
native Gemini/Gemma tool agent keeps its model.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("google.adk")
pytest.importorskip("goldfive")


# ---------------------------------------------------------------------------
# Issue #110 — the mutated-tree invariant
# ---------------------------------------------------------------------------


def _write_pkg(root: Path, name: str, body: str) -> Path:
    """Write ``root/name/{__init__,agent}.py`` and return the package dir."""
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "agent.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return pkg


def _forget(monkeypatch: pytest.MonkeyPatch, *prefixes: str) -> None:
    """Drop every ``sys.modules`` entry under ``prefixes`` for this test."""
    for name in [m for m in sys.modules if m.split(".")[0] in prefixes]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def test_load_refuses_an_in_tree_entrypoint_resolved_outside_the_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An IN-TREE entrypoint that did not come from the snapshot fails LOUD.

    The entrypoint's top-level module IS the registered tree's basename, so
    "the entrypoint came from the snapshot" and "the mutated tree is what runs"
    are the same question — and the snapshot here does not contain the tree at
    all, so the installed copy wins and every mutation is a scored no-op.
    """
    from zicato.adapters.adk import ADKHarnessAdapter

    installed = tmp_path / "site"
    _write_pkg(
        installed,
        "ztriage_pkg",
        '''
        # zicato:mutable id="root_instruction"
        INSTRUCTION = """installed copy"""
        root_agent = {"instruction": INSTRUCTION}
        ''',
    )
    # The generation snapshot does NOT contain ztriage_pkg (a registration
    # whose tree the seed step could not materialise under that basename).
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    monkeypatch.syspath_prepend(str(installed))
    _forget(monkeypatch, "ztriage_pkg")

    adapter = ADKHarnessAdapter(
        "ztriage_pkg.agent:root_agent",
        mutable_trees=[snapshot / "ztriage_pkg"],
    )
    with pytest.raises(RuntimeError, match="snapshot"):
        adapter.load(snapshot)


def test_load_fails_closed_when_a_SECOND_tree_resolves_to_an_installed_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every tree is verified, not just the entrypoint's (the two-tree hole).

    With trees ``[agent, ztriage_other]`` and entrypoint ``agent.agent:...``,
    the entrypoint check passes — it resolved from the snapshot — while
    ``ztriage_other`` resolves to an INSTALLED copy the snapshot never shadowed,
    so every mutation to that tree is a silent scored no-op. FAILS against the
    entrypoint-only check: nothing there ever looks at a second tree.
    """
    from zicato.adapters.adk import ADKHarnessAdapter

    installed = tmp_path / "site"
    _write_pkg(installed, "ztriage_other", 'VALUE = "installed copy"\n')

    snapshot = tmp_path / "snapshot"
    _write_pkg(snapshot, "agent", 'root_agent = {"instruction": "snapshot copy"}\n')
    # ztriage_other is registered as mutable but ABSENT from the snapshot.

    monkeypatch.syspath_prepend(str(installed))
    _forget(monkeypatch, "agent", "ztriage_other")

    adapter = ADKHarnessAdapter(
        "agent.agent:root_agent",
        mutable_trees=[snapshot / "agent", snapshot / "ztriage_other"],
    )
    with pytest.raises(RuntimeError, match="ztriage_other"):
        adapter.load(snapshot)


def test_the_dependency_shape_registers_loads_and_verifies_per_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target 2's shape: entrypoint OUTSIDE every tree, tree imported by it.

    The mutable tree is a DEPENDENCY of the entrypoint (mutate goldfive, drive
    it from a harness module that lives elsewhere). Register accepts it with a
    notice, load passes — the tree resolves inside the snapshot — and the
    post-run inspection reports the tree VERIFIED, because the harness imported
    it from the snapshot. FAILS against the entrypoint-only rule, which refused
    this registration outright.
    """
    from zicato.adapters.adk import (
        TREE_IMPORT_VERIFIED,
        ADKHarnessAdapter,
        entrypoint_outside_trees_notice,
        entrypoint_snapshot_origin_error,
    )

    snapshot = tmp_path / "snapshot"
    _write_pkg(snapshot, "ztriage_dep", 'SETTING = "snapshot copy"\n')

    # The harness lives outside every tree and IMPORTS the mutable tree.
    harness = tmp_path / "site"
    _write_pkg(
        harness,
        "ztriage_harness",
        """
        from ztriage_dep.agent import SETTING

        root_agent = {"instruction": SETTING}
        """,
    )

    trees = [snapshot / "ztriage_dep"]
    entrypoint = "ztriage_harness.agent:root_agent"
    assert entrypoint_snapshot_origin_error(entrypoint, trees) is None
    notice = entrypoint_outside_trees_notice(entrypoint, trees)
    assert notice is not None
    assert "harness_load.json" in notice

    monkeypatch.syspath_prepend(str(harness))
    _forget(monkeypatch, "ztriage_dep", "ztriage_harness")

    adapter = ADKHarnessAdapter(entrypoint, mutable_trees=trees)
    runnable = adapter.load(snapshot)
    assert runnable._agent == {"instruction": "snapshot copy"}
    # No snapshot-relative entrypoint file exists for this shape; the per-tree
    # verdict is what carries the provenance.
    assert runnable.entrypoint_file == ""
    assert runnable.tree_import_status() == {"ztriage_dep": TREE_IMPORT_VERIFIED}


def test_the_original_110_shape_is_caught_post_run_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape #110 was reported as: the tree is never imported at all.

    The entrypoint is an INSTALLED module under a different top level that does
    not import the mutable tree. Nothing lexical or resolution-based can see
    this — the tree does resolve inside the snapshot, it is simply never used —
    so the run succeeds and the POST-RUN inspection is the only detector: the
    tree reports ``never_imported``, which the loop-health check turns into a
    warning naming the generation.
    """
    from zicato.adapters.adk import (
        TREE_IMPORT_NEVER_IMPORTED,
        ADKHarnessAdapter,
    )
    from zicato.health.diagnostics import detect_tree_never_imported

    snapshot = tmp_path / "snapshot"
    _write_pkg(snapshot, "ztriage_unused", 'SETTING = "snapshot copy"\n')

    installed = tmp_path / "site"
    _write_pkg(installed, "ztriage_installed", 'root_agent = {"instruction": "installed"}\n')

    monkeypatch.syspath_prepend(str(installed))
    _forget(monkeypatch, "ztriage_unused", "ztriage_installed")

    adapter = ADKHarnessAdapter(
        "ztriage_installed.agent:root_agent",
        mutable_trees=[snapshot / "ztriage_unused"],
    )
    runnable = adapter.load(snapshot)
    assert runnable._agent == {"instruction": "installed"}
    assert runnable.tree_import_status() == {"ztriage_unused": TREE_IMPORT_NEVER_IMPORTED}

    findings = detect_tree_never_imported({"v3": ("ztriage_unused",)})
    assert [f.severity for f in findings] == ["warning"]
    assert [f.code for f in findings] == ["tree_never_imported"]
    assert (
        "mutations to tree ztriage_unused cannot have been under test in generation v3"
        in findings[0].summary
    )


def test_layer_two_resolves_an_unimported_tree_without_executing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tree not yet imported is verified by RESOLUTION, both ways.

    ``find_spec`` runs the finder without executing the target's module body:
    a tree present in the snapshot passes, and the same tree present only
    OUTSIDE the snapshot raises naming it. The module bodies write a marker
    file, so "no execution" is asserted, not assumed.
    """
    from zicato.adapters.adk import ADKHarnessAdapter

    marker = tmp_path / "executed.txt"
    body = f"""
        from pathlib import Path

        Path({str(marker)!r}).write_text("executed", encoding="utf-8")
        SETTING = "tree"
        """

    snapshot = tmp_path / "snapshot"
    _write_pkg(snapshot, "ztriage_entry", 'root_agent = {"instruction": "snapshot"}\n')
    _write_pkg(snapshot, "ztriage_resolved", body)
    outside = tmp_path / "site"
    _write_pkg(outside, "ztriage_elsewhere", body)

    monkeypatch.syspath_prepend(str(outside))
    _forget(monkeypatch, "ztriage_entry", "ztriage_resolved", "ztriage_elsewhere")

    in_snapshot = ADKHarnessAdapter(
        "ztriage_entry.agent:root_agent",
        mutable_trees=[snapshot / "ztriage_entry", snapshot / "ztriage_resolved"],
    )
    in_snapshot.load(snapshot)
    assert not marker.exists(), "resolving a tree must not execute the target's code"

    # The same tree name registered as mutable but resolvable only OUTSIDE the
    # snapshot: the mutated copy is not what would run.
    elsewhere = ADKHarnessAdapter(
        "ztriage_entry.agent:root_agent",
        mutable_trees=[snapshot / "ztriage_entry", snapshot / "ztriage_elsewhere"],
    )
    with pytest.raises(RuntimeError, match="ztriage_elsewhere"):
        elsewhere.load(snapshot)
    assert not marker.exists()


def test_register_refuses_a_tree_whose_basename_is_not_importable(tmp_path: Path) -> None:
    """Target 2's declared registration is the refusable shape, for the real reason.

    ``--mutable-tree <repo>`` where the repo directory is
    ``goldfive-zicato-optimization-surface`` can never be verified: a snapshot
    exposes each tree under its basename and that basename is not a possible
    module name. The fix is the PACKAGE directory inside it, with the
    entrypoint left outside every tree.
    """
    from zicato.adapters.adk import entrypoint_snapshot_origin_error

    entrypoint = "zicato_examples.target_2.agent_under_test:agent"
    repo = tmp_path / "goldfive-zicato-optimization-surface"
    (repo / "goldfive").mkdir(parents=True)

    refusal = entrypoint_snapshot_origin_error(entrypoint, [repo])
    assert refusal is not None
    assert "goldfive-zicato-optimization-surface" in refusal
    assert "--mutable-tree" in refusal
    # The corrected registration — the importable package dir — is accepted
    # even though the entrypoint is outside it (the dependency shape).
    assert entrypoint_snapshot_origin_error(entrypoint, [repo / "goldfive"]) is None
    # A tree that resolves to the filesystem root has no basename at all.
    assert entrypoint_snapshot_origin_error(entrypoint, ["/"]) is not None


def test_load_from_a_snapshot_relative_entrypoint_still_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The correct wiring must keep working — the guard is not a regression.

    Passes today; pinned so the #110 fix cannot over-reach and reject a
    legitimately snapshot-relative entrypoint.
    """
    from zicato.adapters.adk import ADKHarnessAdapter

    snapshot = tmp_path / "snapshot"
    (snapshot / "ztriage_ok").mkdir(parents=True)
    (snapshot / "ztriage_ok" / "__init__.py").write_text("", encoding="utf-8")
    (snapshot / "ztriage_ok" / "agent.py").write_text(
        'root_agent = {"instruction": "snapshot copy"}\n', encoding="utf-8"
    )
    for name in [m for m in sys.modules if m.startswith("ztriage_ok")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    adapter = ADKHarnessAdapter(
        "ztriage_ok.agent:root_agent",
        mutable_trees=[snapshot / "ztriage_ok"],
    )
    harness = adapter.load(snapshot)
    assert harness._agent == {"instruction": "snapshot copy"}
    # The resolved file is surfaced so the worker can record WHICH file ran.
    assert harness.entrypoint_file == str((snapshot / "ztriage_ok" / "agent.py").resolve())


def test_register_accepts_an_out_of_tree_entrypoint_with_a_notice(tmp_path: Path) -> None:
    """``zicato epoch register`` ACCEPTS the dependency shape, and says what verifies it.

    An entrypoint outside every mutable tree is the legitimate "mutate a
    dependency" registration, so refusing it (as the entrypoint-only rule did)
    is a false refusal of target 2's declared shape. It is accepted with a
    NOTICE pointing at the per-run verification; a tree whose basename could
    never be imported is still refused.
    """
    from click.testing import CliRunner

    from zicato.cli.commands.init import init_cmd
    from zicato.cli.commands.register import register_cmd

    workspace = tmp_path / ".zicato"
    tree = tmp_path / "agent"
    tree.mkdir()
    runner = CliRunner()
    assert runner.invoke(init_cmd, ["--workspace", str(workspace)]).exit_code == 0

    accepted = runner.invoke(
        register_cmd,
        [
            "--workspace",
            str(workspace),
            "--adk",
            "installed_pkg.agent.agent:root_agent",
            "--mutable-tree",
            str(tree),
        ],
    )
    assert accepted.exit_code == 0, accepted.output
    assert "NOTICE" in accepted.output
    assert "harness_load.json" in accepted.output

    # The in-tree form is accepted silently — nothing to warn about.
    in_tree = runner.invoke(
        register_cmd,
        [
            "--workspace",
            str(workspace),
            "--adk",
            "agent.agent:root_agent",
            "--mutable-tree",
            str(tree),
        ],
    )
    assert in_tree.exit_code == 0, in_tree.output
    assert "NOTICE" not in in_tree.output

    # A tree Python could never name as a module IS refused.
    hyphenated = tmp_path / "not-a-module"
    hyphenated.mkdir()
    refused = runner.invoke(
        register_cmd,
        [
            "--workspace",
            str(workspace),
            "--adk",
            "agent.agent:root_agent",
            "--mutable-tree",
            str(hyphenated),
        ],
    )
    assert refused.exit_code != 0, refused.output
    assert "not-a-module" in refused.output


def test_register_time_rule_matches_the_snapshot_basename_layout(tmp_path: Path) -> None:
    """The static rule is the adapter's own ``mutable_subpaths`` rule.

    Pins both to the SAME layout fact: ``mutable_subpaths`` re-bases each
    registered tree onto ``snapshot/<basename>``, so that basename is the only
    handle the snapshot gives the tree — and the refusal fires exactly when
    that basename could not be a module name.
    """
    from zicato.adapters.adk import ADKHarnessAdapter, entrypoint_snapshot_origin_error

    tree = tmp_path / "src" / "my_agent"
    tree.mkdir(parents=True)
    snapshot = tmp_path / "snapshot"
    (snapshot / "my_agent").mkdir(parents=True)

    adapter = ADKHarnessAdapter("my_agent.agent:root_agent", mutable_trees=[tree])
    assert adapter.mutable_subpaths(snapshot) == [snapshot / "my_agent"]
    assert adapter.tree_basenames() == ["my_agent"]
    assert entrypoint_snapshot_origin_error("my_agent.agent:root_agent", [tree]) is None
    # Outside every tree is the dependency shape, verified per run, not refused.
    assert entrypoint_snapshot_origin_error("src.my_agent.agent:root_agent", [tree]) is None
    # No registered trees ⇒ the rule does not apply (the seed step owns that).
    assert entrypoint_snapshot_origin_error("anything.at:all", []) is None


def test_register_time_rule_takes_the_basename_the_seed_step_will(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dot-suffixed relative tree must not be a FALSE refusal.

    ``seed_generation`` resolves each source before taking its basename
    (``Path(raw).resolve().name``), so ``--mutable-tree .`` from inside the
    target materialises ``snapshot/<target-dir-name>/`` and the entrypoint
    ``<target-dir-name>.mod:sym`` loads fine. A purely lexical
    ``Path(".").name`` is the EMPTY string, which read as an unimportable tree
    and refused that registration outright — while suggesting the nonsense
    ``--adk .<module>:<symbol>``. Every rule must speak the same layout the
    seed step materialises.
    """
    from zicato.adapters.adk import (
        entrypoint_outside_trees_notice,
        entrypoint_snapshot_origin_error,
    )

    target = tmp_path / "mytarget"
    (target / "mytarget").mkdir(parents=True)
    monkeypatch.chdir(target)

    for tree in (".", "./", "mytarget/.."):
        assert entrypoint_snapshot_origin_error("mytarget.mod:root_agent", [tree]) is None
        # The resolved basename is what the notice compares against, so the
        # in-tree registration draws no notice through any of the three forms.
        assert entrypoint_outside_trees_notice("mytarget.mod:root_agent", [tree]) is None
    # An entrypoint outside the tree is the dependency shape: accepted, noticed.
    assert entrypoint_snapshot_origin_error("installed_pkg.mod:root_agent", ["."]) is None
    assert entrypoint_outside_trees_notice("installed_pkg.mod:root_agent", ["."]) is not None


def test_second_in_process_load_of_a_dotted_entrypoint_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two generations in ONE process: the loader DETECTS, it does not repair.

    ``importlib.reload`` of a dotted module re-runs the finder against its
    parent package's ``__path__``, which still points at the FIRST
    generation's snapshot — so generation 2 gets generation 1's bytes. Before
    #110 that scored silently; now it raises. Pinned because the fresh-
    process-per-generation contract is the only thing keeping it unreachable:
    if anything ever drives two generations in one process, this is a loud
    failure and not a silent no-op.
    """
    from zicato.adapters.adk import ADKHarnessAdapter

    roots = {}
    for gen in ("g1", "g2"):
        pkg = tmp_path / gen / "ztriage_multi"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "agent.py").write_text(f'root_agent = {{"gen": "{gen}"}}\n', encoding="utf-8")
        roots[gen] = tmp_path / gen
    for name in [m for m in sys.modules if m.startswith("ztriage_multi")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))

    adapter = ADKHarnessAdapter(
        "ztriage_multi.agent:root_agent",
        mutable_trees=[roots["g1"] / "ztriage_multi"],
    )
    assert adapter.load(roots["g1"])._agent == {"gen": "g1"}
    with pytest.raises(RuntimeError, match="NOT under the generation snapshot"):
        adapter.load(roots["g2"])


# ---------------------------------------------------------------------------
# Issue #98 — native Gemini/Gemma IS function-calling capable
# ---------------------------------------------------------------------------


def test_genai_client_classification_survives_an_unimportable_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flood guard must not fail OPEN where ``LiteLlm`` cannot be imported.

    ``litellm`` arrives via ``google-adk``'s ``extensions`` extra, so ADK can
    resolve a native ``gemini-*`` id in an install where
    ``google.adk.models.lite_llm`` raises on import. Answering
    "is this genai-backed?" as ``not issubclass(cls, LiteLlm)`` is
    unanswerable there and returned ``False`` — leaving a tool-free agent on
    its native model string, rebuilding the unused genai client, and bringing
    back the per-turn ``AttributeError`` GC flood the shim exists to stop. The
    classification is positive (``issubclass(cls, Gemini)``) and needs no
    ``litellm`` at all.
    """
    import builtins

    from zicato.adapters.adk import _resolves_to_genai_client

    real_import = builtins.__import__

    def _no_litellm(name: str, *args: object, **kwargs: object) -> object:
        if name in {"litellm", "google.adk.models.lite_llm"}:
            raise ImportError("litellm is not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _no_litellm)
    monkeypatch.delitem(sys.modules, "google.adk.models.lite_llm", raising=False)

    assert _resolves_to_genai_client("gemini-2.0-flash") is True
    assert _resolves_to_genai_client("gemma-3-12b-it") is True
    assert _resolves_to_genai_client("not-a-real-model-xyz") is False


def test_a_tool_subagent_inheriting_the_root_model_is_left_alone() -> None:
    """An empty ``model`` INHERITS; it must not read as "no model".

    ADK's ``LlmAgent.model`` defaults to ``""`` and ``canonical_model`` then
    walks ``parent_agent`` — so the idiomatic multi-agent tree names a model on
    the ROOT only. The #98 hardening backstop must not fire on such a
    sub-agent: it has a real function-calling model (the root's), and raising
    would abort every unit of every round for a perfectly well-formed target.
    Nor may the shim displace it, which would override the root's binding and
    strip the sub-agent's tools.
    """
    from google.adk.agents import LlmAgent

    from zicato.adapters.adk import rebind_tree_models_to_call_llm

    def _a_tool(x: str) -> str:
        """A tool.

        Args:
            x: input.
        """
        return x

    child = LlmAgent(name="worker", instruction="work", tools=[_a_tool])
    root = LlmAgent(
        name="root",
        instruction="route",
        model="openai/gpt-4o-mini",
        sub_agents=[child],
    )
    # ADK itself resolves the child to the root's function-calling model.
    assert child.model == ""
    assert child.canonical_model.model == "openai/gpt-4o-mini"
    assert rebind_tree_models_to_call_llm(root, lambda s, u, m: "text") == 0
    assert root.model == "openai/gpt-4o-mini"
    assert child.model == ""

    # The genuinely modelless ROOT of a tool-declaring tree still raises: it
    # has nothing to inherit from, so no function-calling model exists.
    orphan = LlmAgent(name="orphan", instruction="work", tools=[_a_tool])
    with pytest.raises(RuntimeError, match="declares tools"):
        rebind_tree_models_to_call_llm(orphan, lambda s, u, m: "text")

    # A tool-FREE sub-agent inherits too — the root's binding governs, so the
    # shim must not displace it either.
    plain_child = LlmAgent(name="plain", instruction="chat")
    plain_root = LlmAgent(
        name="proot",
        instruction="route",
        model="openai/gpt-4o-mini",
        sub_agents=[plain_child],
    )
    assert rebind_tree_models_to_call_llm(plain_root, lambda s, u, m: "text") == 0


@pytest.mark.parametrize("model_str", ["gemini-2.0-flash", "gemini-1.5-pro"])
def test_native_gemini_counts_as_function_calling(model_str: str) -> None:
    from zicato.adapters.adk import _resolves_to_native_function_calling

    assert _resolves_to_native_function_calling(model_str) is True


def test_litellm_and_unresolvable_classification_is_unchanged() -> None:
    """The two ends the #98 fix must NOT move: LiteLlm yes, garbage no."""
    from zicato.adapters.adk import _resolves_to_native_function_calling

    assert _resolves_to_native_function_calling("openai/gpt-4o-mini") is True
    assert _resolves_to_native_function_calling("not-a-model-at-all") is False


def test_rebind_leaves_a_native_gemini_tool_agent_alone() -> None:
    """A tool-declaring agent on a native Gemini model must keep its model.

    This is the defect's actual blast radius: the rebind replaces the agent's
    model with the text-only shim, which never reads
    ``llm_request.config.tools``, so the tree degenerates to one text turn.
    """
    from google.adk.agents import LlmAgent

    from zicato.adapters.adk import rebind_tree_models_to_call_llm

    def _a_tool(x: str) -> str:
        """A tool."""
        return x

    agent = LlmAgent(name="root", model="gemini-2.0-flash", tools=[_a_tool])
    rebound = rebind_tree_models_to_call_llm(agent, lambda s, u, m: "text")
    assert rebound == 0, "a native Gemini tool agent must not be rebound"
    assert agent.model == "gemini-2.0-flash"


def test_rebind_raises_rather_than_silently_stripping_tools() -> None:
    """The hardening backstop: a tool agent never becomes a text-only no-op.

    When NO function-calling model is left for a tool-declaring agent (an
    unresolvable model string), the shim would strip its tools and the tree
    would score one text turn. That must be a loud refusal, not a silent
    degradation.
    """
    from google.adk.agents import LlmAgent

    from zicato.adapters.adk import rebind_tree_models_to_call_llm

    def _a_tool(x: str) -> str:
        """A tool."""
        return x

    agent = LlmAgent(name="root", model="not-a-model-at-all", tools=[_a_tool])
    with pytest.raises(RuntimeError, match="declares tools"):
        rebind_tree_models_to_call_llm(agent, lambda s, u, m: "text")
    # A tool-FREE agent on the same unresolvable string still takes the shim.
    tool_free = LlmAgent(name="plain", model="not-a-model-at-all")
    assert rebind_tree_models_to_call_llm(tool_free, lambda s, u, m: "text") == 1
