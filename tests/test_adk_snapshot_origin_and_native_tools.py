"""Triage pins for the two ADK-adapter silent-no-op defects.

* **Issue #110** — :meth:`zicato.adapters.adk.ADKHarnessAdapter.load` inserts
  the generation snapshot at the front of ``sys.path`` and re-imports the
  entrypoint, but never verifies the module actually came from the snapshot.
  When the entrypoint's TOP-LEVEL package is already importable from
  elsewhere, the insert cannot shadow it: the installed copy wins, the
  snapshot is ignored, and every mutation the loop applies is a no-op that
  still scores, gates, promotes and reports.
* **Issue #98** — :func:`zicato.adapters.adk._resolves_to_native_function_calling`
  classifies function-calling capability as ``issubclass(cls, LiteLlm)``, so a
  bare native ``gemini-*`` / ``gemma-*`` model is judged incapable and gets
  rebound to the TEXT-ONLY ``call_llm`` shim, which strips every tool.

Both defects are FIXED; the pins below are live regression tests (the
``xfail(strict=True)`` markers the triage landed them under were removed as
each fix landed). ``load`` now asserts the resolved ``module.__file__`` lies
under the generation snapshot and refuses otherwise, ``register`` refuses a
registration that could never load from a snapshot, and the capability
classifier answers on registry-resolvability so a native Gemini/Gemma tool
agent keeps its model.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("google.adk")
pytest.importorskip("goldfive")


# ---------------------------------------------------------------------------
# Issue #110 — the snapshot-origin assertion
# ---------------------------------------------------------------------------


def test_load_refuses_an_entrypoint_resolved_outside_the_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load that did not come from the snapshot must fail LOUD.

    Models the real wiring: the entrypoint is ``ztriage_pkg.agent.agent``,
    ``ztriage_pkg`` is already importable from an "installed" root, and the
    generation snapshot contains only the mutable tree's own top-level
    directory (``agent/``) — so it cannot shadow ``ztriage_pkg`` and the
    installed copy wins.
    """
    from zicato.adapters.adk import ADKHarnessAdapter

    installed = tmp_path / "site"
    pkg = installed / "ztriage_pkg"
    (pkg / "agent").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "agent" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "agent" / "agent.py").write_text(
        textwrap.dedent(
            '''
            # zicato:mutable id="root_instruction"
            INSTRUCTION = """installed copy"""
            root_agent = {"instruction": INSTRUCTION}
            '''
        ),
        encoding="utf-8",
    )

    # The generation snapshot: only the mutable tree's own top-level dir.
    snapshot = tmp_path / "snapshot"
    (snapshot / "agent").mkdir(parents=True)
    (snapshot / "agent" / "__init__.py").write_text("", encoding="utf-8")
    (snapshot / "agent" / "agent.py").write_text(
        textwrap.dedent(
            '''
            # zicato:mutable id="root_instruction"
            INSTRUCTION = """snapshot copy"""
            root_agent = {"instruction": INSTRUCTION}
            '''
        ),
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(installed))
    for name in [m for m in sys.modules if m.startswith("ztriage_pkg")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    adapter = ADKHarnessAdapter(
        "ztriage_pkg.agent.agent:root_agent",
        mutable_trees=[snapshot / "agent"],
    )
    with pytest.raises(RuntimeError, match="snapshot"):
        adapter.load(snapshot)


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


def test_register_refuses_an_entrypoint_no_snapshot_could_supply(tmp_path: Path) -> None:
    """``zicato register`` refuses the mis-wiring BEFORE a single round runs.

    The static, import-free half of the #110 fix. The snapshot copies each
    mutable tree under its BASENAME, so an entrypoint whose top-level module
    is not one of those basenames can only ever import the installed copy.
    ``register`` says so up front rather than letting the loop score no-ops
    for an epoch.
    """
    from click.testing import CliRunner

    from zicato.cli.commands.init import init_cmd
    from zicato.cli.commands.register import register_cmd

    workspace = tmp_path / ".zicato"
    tree = tmp_path / "agent"
    tree.mkdir()
    runner = CliRunner()
    assert runner.invoke(init_cmd, ["--workspace", str(workspace)]).exit_code == 0

    refused = runner.invoke(
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
    assert refused.exit_code != 0, refused.output
    assert "snapshot" in refused.output
    assert "installed_pkg" in refused.output

    # The tree-relative form is accepted (the guard does not over-reach).
    accepted = runner.invoke(
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
    assert accepted.exit_code == 0, accepted.output


def test_register_time_rule_matches_the_snapshot_basename_layout(tmp_path: Path) -> None:
    """The static rule is the adapter's own ``mutable_subpaths`` rule.

    Pins the two halves to the SAME layout fact: ``mutable_subpaths``
    re-bases each registered tree onto ``snapshot/<basename>``, and the
    refusal accepts exactly the entrypoints whose top-level module is one of
    those basenames.
    """
    from zicato.adapters.adk import ADKHarnessAdapter, entrypoint_snapshot_origin_error

    tree = tmp_path / "src" / "my_agent"
    tree.mkdir(parents=True)
    snapshot = tmp_path / "snapshot"
    (snapshot / "my_agent").mkdir(parents=True)

    adapter = ADKHarnessAdapter("my_agent.agent:root_agent", mutable_trees=[tree])
    assert adapter.mutable_subpaths(snapshot) == [snapshot / "my_agent"]
    assert entrypoint_snapshot_origin_error("my_agent.agent:root_agent", [tree]) is None
    assert entrypoint_snapshot_origin_error("src.my_agent.agent:root_agent", [tree]) is not None
    # No registered trees ⇒ the rule does not apply (the seed step owns that).
    assert entrypoint_snapshot_origin_error("anything.at:all", []) is None


# ---------------------------------------------------------------------------
# Issue #98 — native Gemini/Gemma IS function-calling capable
# ---------------------------------------------------------------------------


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
