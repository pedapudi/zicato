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

Both tests are ``xfail(strict=True)``; they must XPASS once fixed, at which
point the marker is removed.
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #110: load() never checks module.__file__ is under the "
        "generation snapshot, so an entrypoint whose top-level package is "
        "importable elsewhere silently runs the INSTALLED copy and every "
        "mutation becomes a no-op"
    ),
)
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


# ---------------------------------------------------------------------------
# Issue #98 — native Gemini/Gemma IS function-calling capable
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #98: the classifier is LiteLlm-only, so a native Gemini model "
        "is judged tool-incapable and rebound to the text-only shim"
    ),
)
@pytest.mark.parametrize("model_str", ["gemini-2.0-flash", "gemini-1.5-pro"])
def test_native_gemini_counts_as_function_calling(model_str: str) -> None:
    from zicato.adapters.adk import _resolves_to_native_function_calling

    assert _resolves_to_native_function_calling(model_str) is True


def test_litellm_and_unresolvable_classification_is_unchanged() -> None:
    """The two ends the #98 fix must NOT move: LiteLlm yes, garbage no."""
    from zicato.adapters.adk import _resolves_to_native_function_calling

    assert _resolves_to_native_function_calling("openai/gpt-4o-mini") is True
    assert _resolves_to_native_function_calling("not-a-model-at-all") is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #98: rebind_tree_models_to_call_llm strips the tools off an "
        "agent running on a native Gemini model"
    ),
)
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
