"""Tests for :class:`zicato.adapters.adk.ADKHarnessAdapter`.

Requires both :mod:`goldfive` and :mod:`google.adk` — gated by
:func:`pytest.importorskip` so environments without the ADK extra
simply skip these tests.

The strategy across the file:

* For mutation-point enumeration we build a tiny module in ``tmp_path``
  with ``# zicato:mutable`` markers and verify that the adapter
  delegates correctly to :mod:`zicato.mutation.enumerator`. The
  enumerator is owned by a sibling module; when it is not yet
  installed we skip the relevant tests rather than fail.
* For :meth:`ADKHarnessAdapter.load` we build a tmp_path module
  containing an ``agent = LlmAgent(...)`` declaration and verify the
  resolver finds it.
* For :meth:`ADKRunnableHarness.run` we DO NOT exercise the full
  goldfive stack — we monkeypatch :func:`goldfive.run` to a stub and
  verify the wiring: that sinks pass through, that the wall-clock
  budget enforces via :func:`asyncio.wait_for`, and that the
  transcript shape comes out right.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

# Gate the whole module on both optional deps.
pytest.importorskip("goldfive")
pytest.importorskip("google.adk")

from google.adk.agents import LlmAgent  # noqa: E402

from zicato.adapters.adk import (  # noqa: E402
    ADKHarnessAdapter,
    ADKRunnableHarness,
    _goldfive_runtime,
    _outcome_transcript,
    _split_entrypoint,
    rebind_tree_models_to_call_llm,
)
from zicato.adapters.base import HarnessAdapter, RunnableHarness  # noqa: E402
from zicato.core import BoardEntry, RunResult, RuntimeConfig  # noqa: E402

# ---------------------------------------------------------------------------
# _split_entrypoint
# ---------------------------------------------------------------------------


def test_split_entrypoint_parses_well_formed_spec() -> None:
    assert _split_entrypoint("pkg.mod:agent") == ("pkg.mod", "agent")


def test_split_entrypoint_rejects_missing_colon() -> None:
    with pytest.raises(ValueError, match="module.path:agent_symbol"):
        _split_entrypoint("pkg.mod.agent")


def test_split_entrypoint_rejects_empty_halves() -> None:
    with pytest.raises(ValueError):
        _split_entrypoint(":agent")
    with pytest.raises(ValueError):
        _split_entrypoint("pkg.mod:")


def test_split_entrypoint_rejects_multiple_colons() -> None:
    with pytest.raises(ValueError, match="exactly one ':'"):
        _split_entrypoint("pkg:mod:agent")


# ---------------------------------------------------------------------------
# Tiny inner harness on disk
# ---------------------------------------------------------------------------


def _write_inner_harness(root: Path, module_name: str = "demo_inner") -> Path:
    """Write a minimal inner-harness module with mutable markers.

    The module declares a single :class:`LlmAgent` named ``agent`` whose
    instruction is preceded by a ``# zicato:mutable`` marker. The
    structure matches what the mutation enumerator is designed to walk.
    """
    pkg_dir = root / module_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "agent.py").write_text(
        textwrap.dedent(
            """\
            from google.adk.agents import LlmAgent

            # zicato:mutable id=greeter_instruction
            INSTRUCTION = "You greet the user and report success."

            agent = LlmAgent(
                name="greeter",
                instruction=INSTRUCTION,
                model="gemini-2.0-flash",
            )
            """
        )
    )
    return pkg_dir


@pytest.fixture
def inner_harness(tmp_path: Path) -> tuple[Path, str]:
    """Yield ``(generation_root, entrypoint)`` for a tmp_path inner harness."""
    _write_inner_harness(tmp_path)
    return tmp_path, "demo_inner.agent:agent"


@pytest.fixture(autouse=True)
def _cleanup_sys_modules() -> Any:
    """Drop any ``demo_inner*`` modules between tests so reloads are clean."""
    yield
    for name in list(sys.modules):
        if name.startswith("demo_inner"):
            sys.modules.pop(name, None)


# ---------------------------------------------------------------------------
# Construction + Protocol conformance
# ---------------------------------------------------------------------------


def test_adk_adapter_construction_with_explicit_mutable_trees(tmp_path: Path) -> None:
    adapter = ADKHarnessAdapter(
        entrypoint="some.module:agent",
        mutable_trees=[tmp_path],
    )
    assert adapter.name == "adk"
    assert adapter._module_path == "some.module"
    assert adapter._symbol == "agent"
    assert adapter.mutable_trees == [tmp_path.resolve()]


def test_adk_adapter_default_mutable_trees_when_module_missing() -> None:
    """An unresolvable entrypoint module yields empty mutable_trees, no raise."""
    adapter = ADKHarnessAdapter(entrypoint="does.not.exist:agent")
    assert adapter.mutable_trees == []


def test_adk_adapter_conforms_to_harness_adapter_protocol(tmp_path: Path) -> None:
    adapter = ADKHarnessAdapter(
        entrypoint="some.module:agent",
        mutable_trees=[tmp_path],
    )
    assert isinstance(adapter, HarnessAdapter)


# ---------------------------------------------------------------------------
# .load() resolves a module-level ADK agent
# ---------------------------------------------------------------------------


def test_load_resolves_agent_symbol_from_generation_root(
    inner_harness: tuple[Path, str],
) -> None:
    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    runnable = adapter.load(generation_root)
    assert isinstance(runnable, ADKRunnableHarness)
    assert isinstance(runnable, RunnableHarness)
    assert isinstance(runnable._agent, LlmAgent)
    assert runnable._agent.name == "greeter"


def test_load_raises_on_missing_symbol(tmp_path: Path) -> None:
    """When the module exists but the symbol does not, surface AttributeError."""
    _write_inner_harness(tmp_path, module_name="demo_inner_missing_symbol")
    # Tweak the module to remove ``agent``
    agent_py = tmp_path / "demo_inner_missing_symbol" / "agent.py"
    agent_py.write_text(agent_py.read_text().replace("agent = ", "_other = "))

    adapter = ADKHarnessAdapter(
        entrypoint="demo_inner_missing_symbol.agent:agent",
    )
    with pytest.raises(AttributeError, match="has no symbol"):
        adapter.load(tmp_path)


# ---------------------------------------------------------------------------
# .mutation_points() delegates to the enumerator (when available)
# ---------------------------------------------------------------------------


def test_mutation_points_returns_empty_when_no_trees() -> None:
    """No mutable trees and no source_roots override → empty list, no enumerator import."""
    adapter = ADKHarnessAdapter(entrypoint="does.not.exist:agent")
    assert adapter.mutable_trees == []
    assert adapter.mutation_points() == []


def test_mutation_points_delegates_to_enumerator_when_available(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the enumerator is present, ``mutation_points`` forwards to it.

    We don't depend on the enumerator existing — if it's not installed
    we install a stub at ``zicato.mutation.enumerator`` so the lazy
    import inside ``ADKHarnessAdapter.mutation_points`` resolves to
    our fake. This isolates the test from R2-B's module landing.
    """
    from zicato.core import MutationPoint

    generation_root, entrypoint = inner_harness
    fake_point = MutationPoint(
        id="greeter_instruction",
        kind="span",
        file=generation_root / "demo_inner" / "agent.py",
        source_root=generation_root / "demo_inner",
        line_start=4,
        line_end=4,
        content="You greet the user and report success.",
        content_hash="deadbeef",
    )

    called_with: dict[str, Any] = {}

    def fake_enumerate(roots: list[Path]) -> list[MutationPoint]:
        called_with["roots"] = list(roots)
        return [fake_point]

    fake_module = type(sys)("zicato.mutation.enumerator")
    fake_module.enumerate_mutations = fake_enumerate  # type: ignore[attr-defined]
    fake_pkg = type(sys)("zicato.mutation")
    fake_pkg.enumerator = fake_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zicato.mutation", fake_pkg)
    monkeypatch.setitem(sys.modules, "zicato.mutation.enumerator", fake_module)

    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    points = adapter.mutation_points()
    assert len(points) >= 1
    assert points[0].id == "greeter_instruction"
    assert called_with["roots"] == [generation_root / "demo_inner"]


def test_mutation_points_uses_explicit_source_roots_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``source_roots`` kwarg overrides the construction-time default."""
    from zicato.core import MutationPoint

    captured: dict[str, Any] = {}

    def fake_enumerate(roots: list[Path]) -> list[MutationPoint]:
        captured["roots"] = list(roots)
        return []

    fake_module = type(sys)("zicato.mutation.enumerator")
    fake_module.enumerate_mutations = fake_enumerate  # type: ignore[attr-defined]
    fake_pkg = type(sys)("zicato.mutation")
    fake_pkg.enumerator = fake_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zicato.mutation", fake_pkg)
    monkeypatch.setitem(sys.modules, "zicato.mutation.enumerator", fake_module)

    adapter = ADKHarnessAdapter(
        entrypoint="does.not.exist:agent",
        mutable_trees=[tmp_path / "default-root"],
    )
    override = [tmp_path / "explicit-root"]
    adapter.mutation_points(source_roots=override)
    assert captured["roots"] == override


def test_mutation_points_raises_clear_error_when_enumerator_missing(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the enumerator module is not importable, surface a clear ImportError."""
    generation_root, entrypoint = inner_harness

    # Force the import to fail by inserting a sentinel that raises on attr access.
    monkeypatch.delitem(sys.modules, "zicato.mutation", raising=False)
    monkeypatch.delitem(sys.modules, "zicato.mutation.enumerator", raising=False)

    real_import = importlib.import_module

    def blocking_import(name: str, package: Any = None) -> Any:
        if name == "zicato.mutation.enumerator":
            raise ImportError(f"blocked: {name}")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", blocking_import)

    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    with pytest.raises(ImportError, match="zicato.mutation.enumerator"):
        adapter.mutation_points()


# ---------------------------------------------------------------------------
# .run() wiring — mock goldfive.run and verify forwarding
# ---------------------------------------------------------------------------


def _runtime_config(tmp_path: Path) -> RuntimeConfig:
    """Build a throwaway :class:`RuntimeConfig` for adapter tests."""

    async def harness_llm(system: str, user: str, model: str) -> str:
        return "harness-reply"

    async def aux_llm(system: str, user: str, model: str) -> str:
        return "aux-reply"

    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        harness_call_llm=harness_llm,
        auxiliary_call_llm=aux_llm,
    )


class _FakeSession:
    def __init__(self, completed: dict[str, str]) -> None:
        self.completed_results = completed


class _FakeOutcome:
    def __init__(self, completed: dict[str, str]) -> None:
        self.success = True
        self.session = _FakeSession(completed)


def test_outcome_transcript_returns_completed_results_in_order() -> None:
    """Helper extracts the user-facing transcript from an outcome shape."""
    outcome = _FakeOutcome({"task_a": "first reply", "task_b": "second reply"})
    assert _outcome_transcript(outcome) == ("first reply", "second reply")


def test_outcome_transcript_handles_missing_session() -> None:
    class NoSession:
        pass

    assert _outcome_transcript(NoSession()) == ()


def test_outcome_transcript_handles_empty_completed_results() -> None:
    assert _outcome_transcript(_FakeOutcome({})) == ()


@pytest.mark.asyncio
async def test_run_single_turn_forwards_sinks_and_callable(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ADKRunnableHarness.run`` forwards sinks + harness_call_llm to goldfive.run."""
    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    runnable = adapter.load(generation_root)

    import goldfive

    seen: dict[str, Any] = {}

    async def fake_goldfive_run(agent: Any, user_input: str, **kwargs: Any) -> _FakeOutcome:
        seen["agent"] = agent
        seen["user_input"] = user_input
        seen["sinks"] = kwargs.get("sinks")
        seen["call_llm"] = kwargs.get("call_llm")
        return _FakeOutcome({"t1": "reply-from-agent"})

    monkeypatch.setattr(goldfive, "run", fake_goldfive_run)

    entry = BoardEntry(
        id="entry-1",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="hello",
    )
    entry.validate()
    config = _runtime_config(tmp_path)
    sentinel_sink = object()

    result = await runnable.run(entry, sinks=[sentinel_sink], config=config)

    assert isinstance(result, RunResult)
    assert result.entry_id == "entry-1"
    assert result.final_output == "reply-from-agent"
    assert result.transcript == ("reply-from-agent",)
    assert not result.aborted
    assert seen["user_input"] == "hello"
    # sinks passed through verbatim
    assert seen["sinks"] == [sentinel_sink]
    # the harness_call_llm — NOT the auxiliary — is forwarded.
    assert seen["call_llm"] is config.harness_call_llm
    assert seen["call_llm"] is not config.auxiliary_call_llm


# ---------------------------------------------------------------------------
# call_llm rebind — string-model agents must NOT keep a bare model string,
# or ADK resolves it and constructs an unused google.genai client whose
# teardown floods the log with AttributeError('_async_httpx_client').
# ---------------------------------------------------------------------------


def _write_multi_agent_harness(root: Path, module_name: str = "demo_tree") -> Path:
    """Write an inner harness with a root + two string-model sub_agents.

    Mirrors the real target's multi-agent shape (a coordinator with
    sub-agents) so the rebind walker is exercised over ``sub_agents`` edges,
    not just a single root. Every agent declares a BARE MODEL STRING — the
    exact pre-fix state that makes ADK build a genai client per turn.
    """
    pkg_dir = root / module_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "agent.py").write_text(
        textwrap.dedent(
            """\
            from google.adk.agents import LlmAgent

            leaf_a = LlmAgent(name="leaf_a", instruction="a", model="gemma-3-12b-it")
            leaf_b = LlmAgent(name="leaf_b", instruction="b", model="gemma-3-12b-it")
            agent = LlmAgent(
                name="coordinator",
                instruction="coordinate",
                model="gemma-3-12b-it",
                sub_agents=[leaf_a, leaf_b],
            )
            """
        )
    )
    return pkg_dir


@pytest.fixture(autouse=True)
def _cleanup_tree_modules() -> Any:
    """Drop any ``demo_tree*`` modules between tests so reloads are clean."""
    yield
    for name in list(sys.modules):
        if name.startswith("demo_tree"):
            sys.modules.pop(name, None)


def test_rebind_replaces_string_models_with_call_llm_backed_basellm() -> None:
    """Every string-model agent in the tree becomes a call_llm-backed BaseLlm.

    This is the core of the fix: ADK's ``canonical_model`` short-circuits on
    a ``BaseLlm`` model and therefore NEVER resolves a bare model string
    through ``LLMRegistry.new_llm`` (which builds the unused, flood-causing
    google.genai client). We assert the post-rebind invariant the production
    run depends on.
    """
    from google.adk.models import BaseLlm

    leaf_a = LlmAgent(name="leaf_a", instruction="a", model="gemma-3-12b-it")
    leaf_b = LlmAgent(name="leaf_b", instruction="b", model="gemma-3-12b-it")
    coordinator = LlmAgent(
        name="coordinator",
        instruction="c",
        model="gemma-3-12b-it",
        sub_agents=[leaf_a, leaf_b],
    )

    async def call_llm(system: str, user: str, model: str) -> str:
        return "ok"

    rebound = rebind_tree_models_to_call_llm(coordinator, call_llm)

    assert rebound == 3
    for agent in (coordinator, leaf_a, leaf_b):
        assert isinstance(agent.model, BaseLlm), agent.name
        # canonical_model returns the BaseLlm directly — no LLMRegistry /
        # genai client construction can happen for this agent.
        assert agent.canonical_model is agent.model
        # The original model string label is preserved for observability.
        assert agent.model.model == "gemma-3-12b-it"


def test_rebind_is_noop_without_call_llm() -> None:
    """No harness call_llm ⇒ the model-string live path is left unchanged."""
    agent = LlmAgent(name="solo", instruction="s", model="gemma-3-12b-it")
    assert rebind_tree_models_to_call_llm(agent, None) == 0
    assert agent.model == "gemma-3-12b-it"


def test_rebind_leaves_author_supplied_basellm_untouched() -> None:
    """An agent whose model is already a BaseLlm is not rebound."""
    from zicato.testing.adk_fake import TextTurn, make_fake_adk_model

    fake_model = make_fake_adk_model([TextTurn("hi")], model="author-model")
    agent = LlmAgent(name="solo", instruction="s", model=fake_model)

    async def call_llm(system: str, user: str, model: str) -> str:
        return "ok"

    assert rebind_tree_models_to_call_llm(agent, call_llm) == 0
    assert agent.model is fake_model


def test_rebind_to_adk_model_replaces_string_models() -> None:
    """The config-driven path injects the configured model into every
    string-model agent (the inner-model override) — keeping native tools."""
    from zicato.adapters.adk import rebind_tree_models_to_adk_model
    from zicato.testing.adk_fake import TextTurn, make_fake_adk_model

    configured = make_fake_adk_model([TextTurn("hi")], model="configured-endpoint")
    leaf = LlmAgent(name="leaf", instruction="l", model="openai/gpt-4o-mini")
    coordinator = LlmAgent(
        name="coordinator", instruction="c", model="gemma-3-12b-it", sub_agents=[leaf]
    )

    rebound = rebind_tree_models_to_adk_model(coordinator, configured)
    assert rebound == 2
    assert coordinator.model is configured
    assert leaf.model is configured


def test_rebind_to_adk_model_leaves_author_basellm_and_noops_on_none() -> None:
    """Author-supplied BaseLlm models are not overridden; a falsy model is a
    no-op so the caller can fall through to the shim path."""
    from zicato.adapters.adk import rebind_tree_models_to_adk_model
    from zicato.testing.adk_fake import TextTurn, make_fake_adk_model

    author = make_fake_adk_model([TextTurn("a")], model="author")
    configured = make_fake_adk_model([TextTurn("c")], model="configured")
    agent = LlmAgent(name="solo", instruction="s", model=author)

    assert rebind_tree_models_to_adk_model(agent, configured) == 0
    assert agent.model is author
    assert rebind_tree_models_to_adk_model(agent, None) == 0


def test_rebind_leaves_litellm_resolvable_string_untouched() -> None:
    """A provider-style ``openai/<model>`` string resolves to a real
    function-calling ``LiteLlm`` and must NOT be rebound to the text-only
    call_llm shim — doing so would strip native tool/function calling and
    reduce a tool-calling tree to a single text turn.

    Skipped when litellm is unavailable: without it the provider string is
    unresolvable, so the shim fallback (rebind) is correct and expected.
    """
    pytest.importorskip("litellm")

    agent = LlmAgent(name="solo", instruction="s", model="openai/gpt-4o-mini")

    async def call_llm(system: str, user: str, model: str) -> str:
        return "ok"

    assert rebind_tree_models_to_call_llm(agent, call_llm) == 0
    assert agent.model == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_run_rebinds_every_tree_model_to_basellm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After ``ADKRunnableHarness.run``, NO agent in the tree is a bare string.

    Drives the public ``run`` path (with ``goldfive.run`` stubbed so no real
    model turns fire) and asserts that every ``LlmAgent.model`` reachable
    from the root — root + ``sub_agents`` — is a call_llm-backed
    :class:`BaseLlm`. Pre-fix this assertion FAILS: the models stay bare
    ``"gemma-3-12b-it"`` strings, which is exactly what makes ADK construct
    the unused genai client that floods the log on teardown.
    """
    from google.adk.models import BaseLlm

    _write_multi_agent_harness(tmp_path)
    adapter = ADKHarnessAdapter(
        entrypoint="demo_tree.agent:agent",
        mutable_trees=[tmp_path / "demo_tree"],
    )
    runnable = adapter.load(tmp_path)

    # Sanity: the freshly loaded tree starts as bare model strings.
    root_agent = runnable._agent
    assert isinstance(root_agent.model, str)
    assert all(isinstance(sub.model, str) for sub in root_agent.sub_agents)

    import goldfive

    async def fake_goldfive_run(agent: Any, user_input: str, **kwargs: Any) -> _FakeOutcome:
        return _FakeOutcome({"t1": "reply"})

    monkeypatch.setattr(goldfive, "run", fake_goldfive_run)

    entry = BoardEntry(
        id="entry-tree",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="hello",
    )
    entry.validate()
    config = _runtime_config(tmp_path)

    await runnable.run(entry, sinks=[], config=config)

    # The fix: every agent's model is now a call_llm-backed BaseLlm, so ADK
    # never resolves a string to a genai client.
    assert isinstance(root_agent.model, BaseLlm)
    for sub in root_agent.sub_agents:
        assert isinstance(sub.model, BaseLlm), sub.name
        assert sub.canonical_model is sub.model


# ---------------------------------------------------------------------------
# Judge wiring — the adapter assembles judges and passes them to goldfive.run
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _JudgeSpecStub:
    """Structural stand-in for ``zicato.core.JudgeSpec`` (owned elsewhere).

    The adapter forwards ``BoardEntry.judges`` to
    :func:`zicato.judge_runtime.assemble_judges`, which consumes a
    JudgeSpec duck-typed. This stub matches the
    ``{name, mode, body, severity}`` contract.
    """

    name: str
    mode: str
    body: str
    severity: Any


@dataclasses.dataclass
class _EntryStub:
    """Mutable duck-typed stand-in for a :class:`BoardEntry`.

    The real :class:`BoardEntry` is frozen + slotted, so its
    judge-carrying field (``judges``, owned by ``zicato/core/types.py``)
    cannot be set dynamically in a test that predates the field landing.
    The adapter consumes the entry purely structurally — ``id`` /
    ``kind`` / ``wall_clock_budget_seconds`` / ``input`` / ``judges`` /
    ``context`` — so this stub drives the same ``run`` code path.
    """

    id: str
    kind: str
    wall_clock_budget_seconds: int
    input: str | None = None
    judges: tuple[Any, ...] = ()
    context: dict[str, str] = dataclasses.field(default_factory=dict)


@pytest.mark.asyncio
async def test_run_single_turn_passes_judges_into_goldfive_run(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The adapter forwards an explicit judge list to ``goldfive.run``.

    For a plain entry with no custom :class:`JudgeSpec` declared, the
    adapter still passes goldfive's default built-in judge set via
    ``judges=`` — the judge surface is always wired in, not left to
    goldfive's own default.
    """
    import goldfive

    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint, mutable_trees=[generation_root / "demo_inner"]
    )
    runnable = adapter.load(generation_root)

    seen: dict[str, Any] = {}

    async def fake_goldfive_run(agent: Any, user_input: str, **kwargs: Any) -> _FakeOutcome:
        seen["judges"] = kwargs.get("judges")
        seen["call_llm"] = kwargs.get("call_llm")
        return _FakeOutcome({"t1": "reply"})

    monkeypatch.setattr(goldfive, "run", fake_goldfive_run)

    entry = BoardEntry(
        id="entry-judges",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="hello",
    )
    entry.validate()
    config = _runtime_config(tmp_path)

    result = await runnable.run(entry, sinks=[], config=config)
    assert isinstance(result, RunResult)

    judges = seen["judges"]
    # goldfive.run was given an explicit judge list (not None / not absent).
    assert judges is not None
    names = [j.name for j in judges]
    # goldfive's built-in judges stay default-on.
    assert "reasoning_drift" in names
    assert "tool_error" in names
    assert names == [j.name for j in goldfive.builtin_judges.default_judges()]
    # the harness callable is still forwarded; judges did not displace it.
    assert seen["call_llm"] is config.harness_call_llm


@pytest.mark.asyncio
async def test_run_single_turn_includes_entry_custom_judges(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An entry's declared ``JudgeSpec`` judges land in the ``goldfive.run`` list."""
    import goldfive

    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint, mutable_trees=[generation_root / "demo_inner"]
    )
    runnable = adapter.load(generation_root)

    seen: dict[str, Any] = {}

    async def fake_goldfive_run(agent: Any, user_input: str, **kwargs: Any) -> _FakeOutcome:
        seen["judges"] = kwargs.get("judges")
        return _FakeOutcome({"t1": "reply"})

    monkeypatch.setattr(goldfive, "run", fake_goldfive_run)

    custom = _JudgeSpecStub(
        name="entry_custom_judge",
        mode="inline",
        body="the agent must stay on the user's task",
        severity=goldfive.DriftSeverity.WARNING,
    )
    # ``BoardEntry`` is frozen+slotted; use the duck-typed entry stub so
    # the test does not depend on the ``judges`` field's landing order.
    entry = _EntryStub(
        id="entry-custom",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="hello",
        judges=(custom,),
    )
    config = _runtime_config(tmp_path)

    await runnable.run(entry, sinks=[], config=config)  # type: ignore[arg-type]
    names = [j.name for j in seen["judges"]]
    # built-ins stay default-on AND the entry's custom judge is appended.
    assert "reasoning_drift" in names
    assert "entry_custom_judge" in names
    # the custom judge is appended after the built-ins.
    assert names.index("entry_custom_judge") > names.index("reasoning_drift")


@pytest.mark.asyncio
async def test_run_single_turn_disable_drift_suppresses_builtin(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A board's ``disable_drift`` drops the matching built-in from ``judges=``."""
    import goldfive

    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint, mutable_trees=[generation_root / "demo_inner"]
    )
    runnable = adapter.load(generation_root)

    seen: dict[str, Any] = {}

    async def fake_goldfive_run(agent: Any, user_input: str, **kwargs: Any) -> _FakeOutcome:
        seen["judges"] = kwargs.get("judges")
        return _FakeOutcome({"t1": "reply"})

    monkeypatch.setattr(goldfive, "run", fake_goldfive_run)

    entry = BoardEntry(
        id="entry-disable",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="hello",
        # ``disable_drift`` is a board-level setting; the adapter reads
        # it off the entry's opaque ``context`` channel (a comma /
        # whitespace separated list of drift-kind wire strings).
        context={"disable_drift": "tool_error"},
    )
    entry.validate()
    config = _runtime_config(tmp_path)

    await runnable.run(entry, sinks=[], config=config)
    names = [j.name for j in seen["judges"]]
    # the suppressed built-in is gone...
    assert "tool_error" not in names
    # ...the rest of the default built-ins remain default-on.
    assert "reasoning_drift" in names
    assert "refusal" in names


def test_entry_judge_specs_reads_judges_field() -> None:
    """``_entry_judge_specs`` returns ``entry.judges`` and tolerates its absence."""
    from zicato.adapters.adk import _entry_judge_specs

    spec = _JudgeSpecStub(name="j", mode="inline", body="...", severity="warning")
    with_judges = _EntryStub(
        id="e", kind="single_turn", wall_clock_budget_seconds=5, judges=(spec,)
    )
    assert _entry_judge_specs(with_judges) == (spec,)  # type: ignore[arg-type]

    # A real BoardEntry has no ``judges`` field yet -> empty tuple, no raise.
    plain = BoardEntry(id="e", kind="single_turn", wall_clock_budget_seconds=5, input="x")
    assert _entry_judge_specs(plain) == ()


def test_entry_disable_drift_reads_context() -> None:
    """``entry_disable_drift`` reads the board-level set off ``entry.context``.

    ``disable_drift`` is a board-LEVEL setting; the tournament runner
    stamps it onto each entry's ``context['disable_drift']`` (see
    ``zicato.tournament.runner._stamp_disable_drift``). The adapter reads
    that one channel — a comma / whitespace separated list of drift-kind
    wire strings.
    """
    from zicato.adapters.adk import entry_disable_drift

    # comma-separated wire strings.
    comma = _EntryStub(
        id="e",
        kind="single_turn",
        wall_clock_budget_seconds=5,
        context={"disable_drift": "tool_error, agent_refusal"},
    )
    assert entry_disable_drift(comma) == ("tool_error", "agent_refusal")

    # whitespace-separated wire strings (the form the runner stamps).
    spaced = _EntryStub(
        id="e",
        kind="single_turn",
        wall_clock_budget_seconds=5,
        context={"disable_drift": "tool_error agent_refusal"},
    )
    assert entry_disable_drift(spaced) == ("tool_error", "agent_refusal")

    # a plain BoardEntry with no disable_drift in context -> empty tuple.
    plain = BoardEntry(id="e", kind="single_turn", wall_clock_budget_seconds=5, input="x")
    assert entry_disable_drift(plain) == ()


def testentry_judge_only_reads_context() -> None:
    """``entry_judge_only`` reads the board-level flag off ``entry.context``.

    ``judge_only`` is a board-LEVEL setting; the tournament runner stamps
    it onto each entry's ``context['judge_only']`` as the wire string
    ``"true"`` (see ``zicato.tournament.runner._stamp_judge_only``). The
    adapter reads that one channel.
    """
    from zicato.adapters.adk import _JUDGE_ONLY_CONTEXT_KEY, entry_judge_only

    on = _EntryStub(
        id="e",
        kind="single_turn",
        wall_clock_budget_seconds=5,
        context={_JUDGE_ONLY_CONTEXT_KEY: "true"},
    )
    assert entry_judge_only(on) is True

    off = _EntryStub(
        id="e",
        kind="single_turn",
        wall_clock_budget_seconds=5,
        context={_JUDGE_ONLY_CONTEXT_KEY: "false"},
    )
    assert entry_judge_only(off) is False

    # a plain BoardEntry with no judge_only in context -> False.
    plain = BoardEntry(id="e", kind="single_turn", wall_clock_budget_seconds=5, input="x")
    assert entry_judge_only(plain) is False


@pytest.mark.asyncio
async def test_run_single_turn_judge_only_spreads_overrides_into_goldfive_run(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """judge_only ON spreads the no-steering overrides into ``goldfive.run``.

    The wiring contract (fast, deterministic, no real goldfive stack):

    * judge_only ON → the call carries ``planner`` + ``goal_deriver``
      overrides (the no-steering set built by ``_judge_only_overrides``),
      AND still carries ``judges=`` — judging stays armed.
    * judge_only OFF → the call carries NEITHER override key (byte-
      identical to the legacy steering path), and still carries
      ``judges=``.

    The empirical proof that this override set actually removes steering
    while the native tree still executes lives in
    ``test_run_single_turn_judge_only_no_steering_empirical`` below.
    """
    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    runnable = adapter.load(generation_root)

    import goldfive

    seen: dict[str, Any] = {}

    async def fake_goldfive_run(agent: Any, user_input: str, **kwargs: Any) -> _FakeOutcome:
        seen.clear()
        seen.update(kwargs)
        return _FakeOutcome({"t1": "reply"})

    monkeypatch.setattr(goldfive, "run", fake_goldfive_run)
    config = _runtime_config(tmp_path)

    # --- judge_only ON ---
    entry_on = BoardEntry(
        id="e-on",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="hello",
        context={"judge_only": "true"},
    )
    entry_on.validate()
    await runnable.run(entry_on, sinks=[object()], config=config)
    assert (
        "planner" in seen and "goal_deriver" in seen and "steerer" in seen
    ), "judge_only ON must spread the no-steering, no-refine overrides into goldfive.run"
    assert "judges" in seen, "judges must stay armed in judge_only mode"

    # --- judge_only OFF (default) ---
    entry_off = BoardEntry(
        id="e-off",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="hello",
    )
    entry_off.validate()
    await runnable.run(entry_off, sinks=[object()], config=config)
    assert (
        "planner" not in seen and "goal_deriver" not in seen and "steerer" not in seen
    ), "judge_only OFF must NOT pass any steering override — byte-identical to the legacy path"
    assert "judges" in seen, "judges must stay armed on the default path too"


def _goldfive_event_kinds(sink: Any) -> tuple[set[str], int]:
    """Return (payload-kind set, count of goldfive_llm_call_start events).

    goldfive's :class:`InMemorySink` collects events as protobuf messages
    OR plain dicts (the overlay path emits some inner-tree events as
    dicts). This normalises both: the discriminant of a goldfive ``Event``
    is the single *non-envelope* oneof field name (``goal_derived``,
    ``run_completed``, ``goldfive_llm_call_start``, ...) — or the ``kind``
    string on a dict-shaped overlay event. The second return value counts
    ``goldfive_llm_call_start`` events: every steering LLM call (goal
    derivation, planner refine) shows up there, so a count of ZERO is the
    empirical signature of "no steering LLM call fired".
    """
    from google.protobuf.json_format import MessageToDict  # noqa: PLC0415

    envelope = {"event_id", "run_id", "sequence", "emitted_at", "session_id"}
    kinds: set[str] = set()
    llm_call_starts = 0
    for ev in sink.events:
        if isinstance(ev, dict):
            d = ev
            if "kind" in d and "payload" in d:
                kinds.add(str(d["kind"]))
                if str(d["kind"]) == "goldfive_llm_call_start":
                    llm_call_starts += 1
                continue
        else:
            d = MessageToDict(ev, preserving_proto_field_name=True)
        for key in d:
            if key not in envelope:
                kinds.add(key)
        if d.get("goldfive_llm_call_start") is not None:
            llm_call_starts += 1
    return kinds, llm_call_starts


@pytest.mark.asyncio
async def test_run_single_turn_judge_only_no_steering_empirical(
    inner_harness: tuple[Path, str], tmp_path: Path
) -> None:
    """EMPIRICAL acceptance contract: judge_only judges WITHOUT steering.

    This drives a REAL ``goldfive.run`` (no monkeypatch) through the
    adapter with a minimal ``LlmAgent`` and a stub ``call_llm``, exactly
    as goldfive's own ``test_adk_wrap_passthrough`` does. Events are
    captured via goldfive's :class:`InMemorySink` (passed as the run's
    sink). The simplification vs a full multi-agent tree: a single-leaf
    agent is enough to prove the override set yields
    execution-without-steering — the discriminating signal is the LLM-call
    surface, not the agent's internal fan-out.

    Acceptance contract asserted:

    (a) judge_only ON →
        * the native agent tree EXECUTED (overlay events present:
          ``agent_invocation_started`` / ``task_started``) and the run
          ``run_completed`` (NOT ``run_aborted``);
        * ZERO ``goldfive_llm_call_start`` events — no steering LLM call
          (goal derivation, planner refine) fired at all.
    (b) judge_only OFF (the legacy steering path) →
        * a steering LLM call DID fire (``goldfive_llm_call_start`` >= 1,
          the default ``LLMGoalDeriver``'s ``goal_derive`` call), proving
          the two paths differ and that ON genuinely removed steering.

    The stub ``call_llm`` returns prose (not planner JSON) so the legacy
    LLMPlanner cannot parse it and the OFF run aborts after its first
    steering call — that is fine: the contract is about the PRESENCE of a
    steering LLM call on the OFF path and its ABSENCE on the ON path.
    """
    import goldfive
    from goldfive import InMemorySink

    from zicato.adapters.adk import (
        _goldfive_runtime,
        _judge_only_overrides,
        entry_judge_only,
    )

    agent = LlmAgent(name="greeter", instruction="Make a presentation.", model="fake-model")

    async def stub_call_llm(
        system: Any = None, user: Any = None, model: Any = None, **_: Any
    ) -> str:
        return "Slide 1: Waffles. Slide 2: Done."

    # --- judge_only ON: drive the real goldfive.run with the overrides. ---
    on_entry = _EntryStub(
        id="e-on",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="Make a presentation about waffles.",
        context={"judge_only": "true"},
    )
    assert entry_judge_only(on_entry) is True
    sink_on = InMemorySink()
    gf_runtime = _goldfive_runtime()
    await goldfive.run(
        agent,
        on_entry.input,
        sinks=[sink_on],
        call_llm=stub_call_llm,
        judges=[],  # judges stay armed in real usage; empty here keeps the stub deterministic
        runtime=gf_runtime,
        **_judge_only_overrides(agent, stub_call_llm, gf_runtime),
    )
    kinds_on, llm_calls_on = _goldfive_event_kinds(sink_on)

    assert llm_calls_on == 0, (
        f"judge_only ON must fire ZERO steering LLM calls; saw {llm_calls_on}. "
        f"kinds={sorted(kinds_on)}"
    )
    assert (
        "run_completed" in kinds_on
    ), f"judge_only ON: the native agent must execute to completion; kinds={sorted(kinds_on)}"
    assert "run_aborted" not in kinds_on
    assert kinds_on & {
        "agent_invocation_started",
        "task_started",
        "pin_resolved",
    }, f"judge_only ON: expected overlay execution events; kinds={sorted(kinds_on)}"

    # --- judge_only OFF: the legacy steering path fires a steering call. ---
    sink_off = InMemorySink()
    await goldfive.run(
        agent,
        "Make a presentation about waffles.",
        sinks=[sink_off],
        call_llm=stub_call_llm,
        judges=[],
    )
    _kinds_off, llm_calls_off = _goldfive_event_kinds(sink_off)
    assert llm_calls_off >= 1, (
        "judge_only OFF (steering default) must fire at least one steering LLM "
        f"call (goal derivation); saw {llm_calls_off}"
    )


@pytest.mark.asyncio
async def test_run_enforces_wall_clock_budget(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When goldfive.run hangs past the budget, return aborted=wall_clock_budget."""
    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    runnable = adapter.load(generation_root)

    import goldfive

    async def slow_goldfive_run(agent: Any, user_input: str, **kwargs: Any) -> Any:
        # Far longer than the entry's budget.
        await asyncio.sleep(5.0)
        return _FakeOutcome({"t1": "would-have-been-fine"})

    monkeypatch.setattr(goldfive, "run", slow_goldfive_run)

    entry = BoardEntry(
        id="entry-budget",
        kind="single_turn",
        wall_clock_budget_seconds=1,
        input="hello",
    )
    entry.validate()
    config = _runtime_config(tmp_path)

    # Override to a sub-second budget at the adapter level by mutating the
    # entry's budget via dataclasses.replace; BoardEntry is frozen.
    import dataclasses

    short_entry = dataclasses.replace(entry, wall_clock_budget_seconds=1)
    # Sub-second isn't expressible (BoardEntry budget is int seconds and
    # must be > 0). Patch ``asyncio.wait_for`` to a tighter timeout via
    # the entry's budget being treated as float seconds; here we instead
    # rely on the int-1-second budget against a 5-second sleep.
    result = await runnable.run(short_entry, sinks=[], config=config)

    assert result.aborted
    assert result.abort_reason == "wall_clock_budget"
    assert result.final_output == ""
    assert result.transcript == ()
    assert result.entry_id == "entry-budget"


@pytest.mark.asyncio
async def test_run_catches_harness_exception(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exceptions inside goldfive.run surface as aborted RunResults."""
    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    runnable = adapter.load(generation_root)

    import goldfive

    async def boom(agent: Any, user_input: str, **kwargs: Any) -> Any:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(goldfive, "run", boom)

    entry = BoardEntry(
        id="entry-boom",
        kind="single_turn",
        wall_clock_budget_seconds=5,
        input="hi",
    )
    entry.validate()
    config = _runtime_config(tmp_path)

    result = await runnable.run(entry, sinks=[], config=config)
    assert result.aborted
    assert result.abort_reason.startswith("harness_exception:")
    assert "RuntimeError" in result.abort_reason


@pytest.mark.asyncio
async def test_run_unsupported_kind_returns_aborted_result(
    inner_harness: tuple[Path, str], tmp_path: Path
) -> None:
    """Reserved forward-compat kinds return aborted=unsupported_kind."""
    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    runnable = adapter.load(generation_root)

    entry = BoardEntry(
        id="entry-synth",
        kind="synthetic_clean",
        wall_clock_budget_seconds=5,
        input="placeholder",
    )
    entry.validate()
    config = _runtime_config(tmp_path)

    result = await runnable.run(entry, sinks=[], config=config)
    assert result.aborted
    assert result.abort_reason == "unsupported_kind"


@pytest.mark.asyncio
async def test_run_multi_turn_scripted_graceful_when_driver_missing(
    inner_harness: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``zicato.board.scripted`` installed, return scripted_driver_unavailable."""
    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    runnable = adapter.load(generation_root)

    # Force the import to fail.
    monkeypatch.setitem(sys.modules, "zicato.board", None)

    from zicato.core import ScriptedTurn

    entry = BoardEntry(
        id="entry-script",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=5,
        turns=(ScriptedTurn(user="first"),),
        max_turns=4,
    )
    entry.validate()
    config = _runtime_config(tmp_path)

    result = await runnable.run(entry, sinks=[], config=config)
    assert result.aborted
    assert result.abort_reason == "scripted_driver_unavailable"


@pytest.mark.asyncio
async def test_run_multi_turn_emulated_graceful_when_emulator_missing(
    inner_harness: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``zicato.emulator`` installed, return emulator_unavailable."""
    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    runnable = adapter.load(generation_root)

    monkeypatch.setitem(sys.modules, "zicato.emulator", None)
    # If zicato.emulator was already imported before this test (e.g.
    # because pytest collected test_emulator_driver.py earlier and that
    # collection imported zicato.emulator), the 'from zicato import
    # emulator' in production code resolves 'emulator' from the zicato
    # package's __dict__, bypassing sys.modules entirely. Remove the
    # attribute so the import path goes through sys.modules and sees
    # the sentinel None, which raises ImportError as expected.
    import zicato as _zicato_pkg

    if hasattr(_zicato_pkg, "emulator"):
        monkeypatch.delattr(_zicato_pkg, "emulator")

    from zicato.core import UserPersona

    entry = BoardEntry(
        id="entry-emu",
        kind="multi_turn_emulated",
        wall_clock_budget_seconds=5,
        user_persona=UserPersona(
            goal="ask a thing",
            constraints="be terse",
            stop_when="answer received",
        ),
        max_turns=4,
    )
    entry.validate()
    config = _runtime_config(tmp_path)

    result = await runnable.run(entry, sinks=[], config=config)
    assert result.aborted
    assert result.abort_reason == "emulator_unavailable"


# ---------------------------------------------------------------------------
# Regression: scripted multi-turn must NOT pass the raw ADK agent to the
# scripted driver — the driver expects async run(user_message: str), not the
# ADK agent's own .run() signature.  Before the fix, _run_multi_turn_scripted
# called run_scripted(agent=self._agent, ...) and the scripted driver's
# _resolve_invoker found agent.run(), then called it as method(user_message)
# — which raises TypeError because the ADK agent's .run() requires ADK-
# specific positional arguments.  After the fix, a _PerTurnCaller wrapper is
# passed instead; it calls goldfive.run(agent, user_message, ...) per turn.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_multi_turn_scripted_calls_goldfive_run_per_turn(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Scripted multi-turn entries drive goldfive.run once per scripted turn.

    Regression guard for the TypeError that occurred when the raw ADK
    agent was handed directly to the scripted driver: the driver found
    ``agent.run`` and called it as ``agent.run(user_message)`` — wrong
    signature for an ADK agent, hence TypeError.

    The fix wraps the agent in a ``_PerTurnCaller`` that calls
    ``goldfive.run(agent, user_message, ...)`` per turn.  This test
    verifies:

    * No TypeError is raised.
    * ``goldfive.run`` is called once per scripted turn (not zero times,
      not once for the whole entry).
    * The final transcript contains one reply per turn, in order.
    * The harness_call_llm (not auxiliary_call_llm) is forwarded on each
      turn — the two-callable collusion guard holds for scripted entries.
    """
    import goldfive

    from zicato.core import ScriptedTurn

    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    runnable = adapter.load(generation_root)

    calls: list[dict[str, Any]] = []

    async def fake_goldfive_run(agent: Any, user_input: str, **kwargs: Any) -> _FakeOutcome:
        calls.append(
            {
                "user_input": user_input,
                "call_llm": kwargs.get("call_llm"),
                "sinks": kwargs.get("sinks"),
            }
        )
        return _FakeOutcome({"turn": f"reply:{user_input}"})

    monkeypatch.setattr(goldfive, "run", fake_goldfive_run)

    entry = BoardEntry(
        id="scripted-regression",
        kind="multi_turn_scripted",
        wall_clock_budget_seconds=30,
        turns=(
            ScriptedTurn(user="turn-one"),
            ScriptedTurn(user="turn-two"),
            ScriptedTurn(user="turn-three"),
        ),
        max_turns=5,
    )
    entry.validate()
    config = _runtime_config(tmp_path)
    sentinel_sink = object()

    result = await runnable.run(entry, sinks=[sentinel_sink], config=config)

    # No TypeError — result is a clean RunResult, not aborted.
    assert isinstance(result, RunResult)
    assert not result.aborted, f"unexpected abort: {result.abort_reason!r}"
    assert result.entry_id == "scripted-regression"

    # goldfive.run was called once per scripted turn, in order.
    assert len(calls) == 3
    assert [c["user_input"] for c in calls] == ["turn-one", "turn-two", "turn-three"]

    # The harness_call_llm (not auxiliary) is forwarded on every turn.
    for call in calls:
        assert call["call_llm"] is config.harness_call_llm
        assert call["call_llm"] is not config.auxiliary_call_llm

    # Sinks are forwarded on every turn.
    for call in calls:
        assert call["sinks"] == [sentinel_sink]

    # The transcript contains the final_output from each turn's outcome.
    assert result.transcript == ("reply:turn-one", "reply:turn-two", "reply:turn-three")
    assert result.final_output == "reply:turn-three"


# ---------------------------------------------------------------------------
# Regression: emulated multi-turn must NOT pass the raw ADK agent to the
# emulator driver.  #105 fixed this bug class for the scripted path and
# explicitly scoped out the emulated path; the emulator's run_emulated bridge
# calls agent.run(user_message) with a bare string, which raises TypeError on
# the raw ADK agent.  After the fix, a _PerTurnCaller wrapper is passed; it
# calls goldfive.run(agent, user_message, ...) per emulated turn.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_multi_turn_emulated_calls_goldfive_run_per_turn(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Emulated multi-turn entries drive goldfive.run once per emulated turn.

    Regression guard for the TypeError that occurred when the raw ADK
    agent was handed directly to the emulator driver: the driver's
    ``run_emulated`` bridge found ``agent.run`` and called it as
    ``agent.run(user_message)`` — wrong signature for an ADK agent,
    hence TypeError.  #105 fixed the analogous bug on the *scripted*
    path and explicitly scoped out the emulated path; this is the
    matching fix.

    The fix wraps the agent in a ``_PerTurnCaller`` that calls
    ``goldfive.run(agent, user_message, ...)`` per turn.  This test
    verifies:

    * No TypeError is raised.
    * ``goldfive.run`` is called once per harness (emulated) turn.
    * The harness_call_llm (not auxiliary_call_llm) is forwarded on each
      turn — the two-callable collusion guard holds for emulated entries.
    * The auxiliary_call_llm drives the emulator's user turns.
    """
    import goldfive

    from zicato.core import UserPersona
    from zicato.emulator.sealed import END_TOKEN

    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    runnable = adapter.load(generation_root)

    calls: list[dict[str, Any]] = []

    async def fake_goldfive_run(agent: Any, user_input: str, **kwargs: Any) -> _FakeOutcome:
        calls.append(
            {
                "user_input": user_input,
                "call_llm": kwargs.get("call_llm"),
                "sinks": kwargs.get("sinks"),
            }
        )
        return _FakeOutcome({"turn": f"reply:{len(calls)}"})

    monkeypatch.setattr(goldfive, "run", fake_goldfive_run)

    # The emulator emits one user turn, then <<END>> to terminate.
    aux_outputs = ["I need a laptop for travel.", END_TOKEN]
    aux_state = {"i": 0}

    async def aux_llm(system: str, user: str, model: str) -> str:
        out = aux_outputs[min(aux_state["i"], len(aux_outputs) - 1)]
        aux_state["i"] += 1
        return out

    async def harness_llm(system: str, user: str, model: str) -> str:
        return "harness-reply"

    config = RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        harness_call_llm=harness_llm,
        auxiliary_call_llm=aux_llm,
    )

    entry = BoardEntry(
        id="emulated-regression",
        kind="multi_turn_emulated",
        wall_clock_budget_seconds=30,
        user_persona=UserPersona(
            goal="Buy a laptop for travel.",
            constraints="Be vague about budget.",
            stop_when="A specific model has been recommended.",
        ),
        max_turns=5,
    )
    entry.validate()
    sentinel_sink = object()

    result = await runnable.run(entry, sinks=[sentinel_sink], config=config)

    # No TypeError — result is a clean RunResult, not aborted.
    assert isinstance(result, RunResult)
    assert not result.aborted, f"unexpected abort: {result.abort_reason!r}"
    assert result.entry_id == "emulated-regression"

    # goldfive.run was called once per emulated harness turn (the
    # emulator emitted exactly one user turn before <<END>>).
    assert len(calls) == 1
    assert calls[0]["user_input"] == "I need a laptop for travel."

    # The harness_call_llm (not auxiliary) is forwarded on every turn.
    for call in calls:
        assert call["call_llm"] is config.harness_call_llm
        assert call["call_llm"] is not config.auxiliary_call_llm
        assert call["sinks"] == [sentinel_sink]


# ---------------------------------------------------------------------------
# A2: per-call LLM timeout — the adapter raises goldfive's AgentConfig
# call_timeout_ms above its 120s default so a real reasoning model under
# concurrency does not get its healthy LLM calls aborted.
# ---------------------------------------------------------------------------


def test_goldfive_runtime_raises_call_timeout_above_goldfive_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_goldfive_runtime`` sets ``agent.call_timeout_ms`` from zicato config.

    goldfive's :class:`~goldfive.config.AgentConfig` defaults
    ``call_timeout_ms`` to 120 000 ms. zicato's
    :attr:`RuntimeTuningConfig.harness_call_timeout_ms` defaults higher
    (1 800 000 ms) so a real reasoning model's long LLM call is not
    aborted; ``_goldfive_runtime`` must thread that value onto the
    goldfive runtime config it builds.
    """
    monkeypatch.delenv("GOLDFIVE_AGENT_CALL_TIMEOUT_MS", raising=False)
    runtime = _goldfive_runtime()
    assert runtime.agent.call_timeout_ms == 1_800_000
    assert runtime.agent.call_timeout_ms > 120_000


def test_goldfive_runtime_honours_pinned_flag_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned ``--harness-call-timeout-ms`` value tunes the per-call budget.

    The value is pinned exactly as the evolve CLI pins it (and as the
    worker re-pins it from its args file); the deleted
    ``ZICATO_HARNESS_CALL_TIMEOUT_MS`` env var is set too, to prove it
    is ignored.
    """
    from zicato.config import pin_overrides

    monkeypatch.delenv("GOLDFIVE_AGENT_CALL_TIMEOUT_MS", raising=False)
    monkeypatch.setenv("ZICATO_HARNESS_CALL_TIMEOUT_MS", "123000")  # ignored
    pin_overrides({"runtime": {"harness_call_timeout_ms": 456000}})
    runtime = _goldfive_runtime()
    assert runtime.agent.call_timeout_ms == 456000


def test_goldfive_runtime_defers_to_explicit_goldfive_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``GOLDFIVE_AGENT_CALL_TIMEOUT_MS`` is not overridden."""
    from zicato.config import pin_overrides

    monkeypatch.setenv("GOLDFIVE_AGENT_CALL_TIMEOUT_MS", "999000")
    pin_overrides({"runtime": {"harness_call_timeout_ms": 111000}})
    runtime = _goldfive_runtime()
    # goldfive's own env value wins; zicato does not override it.
    assert runtime.agent.call_timeout_ms == 999000


@pytest.mark.asyncio
async def test_run_single_turn_forwards_runtime_to_goldfive_run(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``goldfive.run`` receives a ``runtime`` carrying the raised timeout."""
    monkeypatch.delenv("GOLDFIVE_AGENT_CALL_TIMEOUT_MS", raising=False)
    generation_root, entrypoint = inner_harness
    adapter = ADKHarnessAdapter(
        entrypoint=entrypoint,
        mutable_trees=[generation_root / "demo_inner"],
    )
    runnable = adapter.load(generation_root)

    import goldfive

    seen: dict[str, Any] = {}

    async def fake_goldfive_run(agent: Any, user_input: str, **kwargs: Any) -> _FakeOutcome:
        seen["runtime"] = kwargs.get("runtime")
        return _FakeOutcome({"t1": "reply"})

    monkeypatch.setattr(goldfive, "run", fake_goldfive_run)

    entry = BoardEntry(
        id="entry-rt",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="hello",
    )
    entry.validate()
    config = _runtime_config(tmp_path)

    await runnable.run(entry, sinks=[], config=config)

    runtime = seen["runtime"]
    assert runtime is not None
    assert runtime.agent.call_timeout_ms == 1_800_000


# ---------------------------------------------------------------------------
# Judge-only steerer: observe + judge, ZERO refine attempts
# ---------------------------------------------------------------------------


def _drift_detected_and_judgement_counts(sink: Any) -> tuple[int, int, int]:
    """Return (drift_detected, judgement_emitted, refine_attempted) counts.

    Normalises goldfive's :class:`InMemorySink` events (protobuf messages
    or plain dicts) the same way :func:`_goldfive_event_kinds` does, then
    counts the three event kinds the judge-only no-refine contract turns
    on: a custom-judge verdict must still emit ``drift_detected`` +
    ``judgement_emitted`` (the scalar signal zicato's reducer reads) while
    firing ZERO ``refine_attempted`` (the abort-spiral trigger).
    """
    from google.protobuf.json_format import MessageToDict  # noqa: PLC0415

    drift_detected = 0
    judgement_emitted = 0
    refine_attempted = 0
    for ev in sink.events:
        if isinstance(ev, dict):
            kind = str(ev.get("kind", ""))
            if kind == "drift_detected":
                drift_detected += 1
            elif kind == "judgement_emitted":
                judgement_emitted += 1
            elif kind == "refine_attempted":
                refine_attempted += 1
            continue
        d = MessageToDict(ev, preserving_proto_field_name=True)
        if d.get("drift_detected") is not None:
            drift_detected += 1
        if d.get("judgement_emitted") is not None:
            judgement_emitted += 1
        if d.get("refine_attempted") is not None:
            refine_attempted += 1
    return drift_detected, judgement_emitted, refine_attempted


class _RefineRecordingPlanner:
    """A planner whose ``refine`` records that it was invoked at all.

    The discriminating signal for the judge-only contract is whether
    ``refine`` is reached. A control :class:`DefaultSteerer` routes a
    CRITICAL custom drift into the refine ladder and calls this; the
    judge-only steerer must NOT.
    """

    def __init__(self) -> None:
        self.refine_calls = 0

    async def generate(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        return None

    async def refine(self, *args: Any, **kwargs: Any) -> Any:
        self.refine_calls += 1
        return None

    async def handle_turn(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        return None


class _CriticalFabricationJudge:
    """Stub of the board's CRITICAL ``no_fabricated_numbers`` judge.

    Emits a drift-flavoured CRITICAL verdict every time it is evaluated —
    exactly the shape that, on the live emulated path, was promoted to a
    ``custom`` drift, escalated through the refine ladder, and spun the
    run to the wall-clock budget.
    """

    name = "no_fabricated_numbers"

    async def evaluate(self, ctx: Any) -> Any:
        from goldfive.judges import JudgeVerdict  # noqa: PLC0415
        from goldfive.types import DriftKind, DriftSeverity  # noqa: PLC0415

        return JudgeVerdict(
            drift_emitted=True,
            drift_kind=DriftKind.CUSTOM,
            severity=DriftSeverity.CRITICAL,
            detail="The agent fabricated specific metric values it was not given.",
        )


def _judge_only_test_session() -> Any:
    """Build a minimal bound-ready :class:`Session` carrying a live task.

    A non-empty task is required so the CRITICAL drift has a concrete
    target for the control steerer's ladder to refine against.
    """
    from goldfive.types import Goal, Plan, Session, Task  # noqa: PLC0415

    task = Task(id="t1", title="Draft the Q3 metrics deck", description="draft")
    goal = Goal(id="g1", summary="produce a Q3 metrics deck")
    plan = Plan(
        id="p1",
        run_id="r1",
        goal_ids=("g1",),
        tasks=(task,),
        edges=(),
        summary="one-task plan",
    )
    session = Session(run_id="r1", goals=[goal], plan=plan, current_task_id="t1")
    return session


@pytest.mark.asyncio
async def test_judge_only_steerer_emits_drift_but_never_refines() -> None:
    """Judge-only: a CRITICAL custom-judge verdict scores WITHOUT refining.

    Drives ``evaluate_judges`` directly on the judge-only steerer with the
    stub CRITICAL ``no_fabricated_numbers`` judge. The contract:

    * ``drift_detected`` IS emitted — the ``custom``-kind drift zicato's
      reducer attributes to the judge by ``judge_name`` for the scalar;
    * ``judgement_emitted`` IS emitted — the paired judgement envelope;
    * ``refine_attempted`` is NEVER emitted AND the bound planner's
      ``refine`` is NEVER called — zero refine attempts, so no
      ``HUMAN_INTERVENTION_REQUIRED`` escalation and no wall-clock spin.

    The control half proves the assertion is load-bearing: a plain
    :class:`DefaultSteerer` with the SAME judge + planner DOES reach
    ``refine`` (the live behaviour this fix removes).
    """
    from goldfive import InMemorySink
    from goldfive.judges.base import JudgeContext
    from goldfive.steerer import DefaultSteerer

    from zicato.adapters.adk import _build_judge_only_steerer

    judge = _CriticalFabricationJudge()

    # --- judge-only steerer: observe + judge, NO refine ---
    jo_sink = InMemorySink()
    jo_steerer = _build_judge_only_steerer(call_llm=None, runtime=_goldfive_runtime())
    jo_planner = _RefineRecordingPlanner()
    jo_steerer.bind(sinks=[jo_sink], planner=jo_planner)
    jo_steerer.set_judges([judge])
    jo_session = _judge_only_test_session()
    jo_ctx = JudgeContext(
        plan=jo_session.plan,
        session_state=jo_session,
        current_task_id="t1",
    )
    await jo_steerer.evaluate_judges(jo_ctx, session=jo_session, run_id="r1")

    jo_drift, jo_judgement, jo_refine = _drift_detected_and_judgement_counts(jo_sink)
    assert jo_drift >= 1, "judge-only must still emit drift_detected (the scalar signal)"
    assert jo_judgement >= 1, "judge-only must still emit judgement_emitted"
    assert jo_refine == 0, f"judge-only must fire ZERO refine_attempted; saw {jo_refine}"
    assert (
        jo_planner.refine_calls == 0
    ), f"judge-only must NEVER call planner.refine; called {jo_planner.refine_calls}x"

    # --- control: a plain DefaultSteerer DOES reach refine on the same drift ---
    ctrl_sink = InMemorySink()
    ctrl_steerer = DefaultSteerer()
    ctrl_planner = _RefineRecordingPlanner()
    ctrl_steerer.bind(sinks=[ctrl_sink], planner=ctrl_planner)
    ctrl_steerer.set_judges([judge])
    ctrl_session = _judge_only_test_session()
    ctrl_ctx = JudgeContext(
        plan=ctrl_session.plan,
        session_state=ctrl_session,
        current_task_id="t1",
    )
    await ctrl_steerer.evaluate_judges(ctrl_ctx, session=ctrl_session, run_id="r1")

    assert ctrl_planner.refine_calls >= 1, (
        "control DefaultSteerer must reach planner.refine on a CRITICAL drift "
        "(proves the judge-only assertion bites)"
    )


# ---------------------------------------------------------------------------
# Inner-model resolution + the tool-calling regression
#
# The bug this section guards: an ADK function-calling target was
# unconditionally rebound to the TEXT-ONLY ``call_llm`` shim, which strips the
# tool ``function_declarations``, so a tool-driven tree degenerated to a single
# text turn and wrote no files. These tests pin the three properties that would
# have caught it, all DETERMINISTIC (no live LLM):
#
#   (a) a provider-string model resolves to a function-calling ``LiteLlm`` AND
#       the shim's guard leaves it alone;
#   (b) THE REGRESSION: under the config-driven inner-model rebind, an agent
#       carrying a ``FunctionTool`` keeps function-calling — ADK puts the tool's
#       ``function_declarations`` into the captured ``LlmRequest.config.tools``;
#       the text shim, by contrast, drops them (yields text, never a call);
#   (c) the ``adk`` extra actually supplies ``litellm`` (the native path's
#       precondition).
# ---------------------------------------------------------------------------


def test_litellm_is_provided_by_the_adk_extra() -> None:
    """(c) The ``adk`` extra supplies ``litellm`` — the native path precondition.

    The function-calling path (a configured ``LiteLlm`` inner model, and the
    shim guard that recognises ``openai/<model>`` strings) only exists when
    ADK's ``LiteLlm`` is importable, which the ``adk`` extra provides via
    ``google-adk[extensions]``. A failure here means the extra regressed and
    every native tool-calling target would silently fall back to the text shim.
    """
    from google.adk.models.lite_llm import LiteLlm

    assert LiteLlm.__name__ == "LiteLlm"


def test_provider_string_resolves_to_litellm_and_guard_leaves_it_alone() -> None:
    """(a) ``openai/<model>`` resolves to ``LiteLlm`` and the shim skips it.

    Two halves of the same guard:

    * :func:`_resolves_to_native_function_calling` classifies every
      registry-resolvable string as function-calling capable — the
      provider-style ``openai/<model>`` (``LiteLlm``) AND a native
      ``gemini-*`` / ``gemma-*`` id (``Gemini`` / ``Gemma``, issue #98). The
      classifier resolves the model *class* without instantiating it, so it
      never builds a genai client. Only a string ADK cannot resolve at all
      classifies ``False``.
    * :func:`rebind_tree_models_to_call_llm` therefore LEAVES the
      ``openai/<model>`` agent untouched — rebinding it to the text-only shim
      would strip its native tool/function calling. A genai-backed string is
      still shimmed for a TOOL-FREE agent (the client-flood guard, now a
      construction-path concern: :func:`_resolves_to_genai_client`).

    Skipped without ``litellm``: the provider string is then unresolvable, so
    the shim fallback (rebind) is the correct behaviour.
    """
    pytest.importorskip("litellm")

    from zicato.adapters.adk import (
        _resolves_to_genai_client,
        _resolves_to_native_function_calling,
    )

    # The capability classifier: registry-resolvable -> True; garbage -> False.
    assert _resolves_to_native_function_calling("openai/gpt-4o-mini") is True
    assert _resolves_to_native_function_calling("gemma-3-12b-it") is True
    assert _resolves_to_native_function_calling("gemini-2.0-flash") is True
    assert _resolves_to_native_function_calling("not-a-real-model-xyz") is False
    # The separate construction-path concern: which of them builds a genai
    # client (the GC-flood source the shim displaces for tool-free agents).
    assert _resolves_to_genai_client("openai/gpt-4o-mini") is False
    assert _resolves_to_genai_client("gemma-3-12b-it") is True
    assert _resolves_to_genai_client("gemini-2.0-flash") is True
    assert _resolves_to_genai_client("not-a-real-model-xyz") is False

    # The guard: the shim leaves the LiteLlm-resolvable agent's string in place.
    agent = LlmAgent(name="solo", instruction="s", model="openai/gpt-4o-mini")

    async def call_llm(system: str, user: str, model: str) -> str:
        return "ok"

    assert rebind_tree_models_to_call_llm(agent, call_llm) == 0
    assert agent.model == "openai/gpt-4o-mini"


def _weather_tool() -> Any:
    """Build a deterministic ADK ``FunctionTool`` for the regression test.

    A trivial typed function so ADK derives a ``function_declaration`` for it;
    the body never runs (the fake model answers with text before any tool
    dispatch). Returns the wrapped tool the agent declares under ``tools=``.
    """
    from google.adk.tools import FunctionTool

    def lookup_weather(city: str) -> dict[str, str]:
        """Return the weather for a city.

        Args:
            city: the city to look up.
        """
        return {"city": city, "weather": "sunny"}

    return FunctionTool(lookup_weather)


@pytest.mark.asyncio
async def test_inner_model_rebind_preserves_tool_function_declarations() -> None:
    """(b) REGRESSION: the config-driven inner-model rebind keeps tool-calling.

    Under :func:`rebind_tree_models_to_adk_model` (the path taken when a
    ``models.harness`` inner model is configured), an agent that declares a
    ``FunctionTool`` must still be driven WITH its tools: ADK assembles the
    ``LlmRequest`` with the tool's ``function_declarations`` on
    ``config.tools``. We capture that request via a fake ``BaseLlm`` (the
    configured inner model) and assert the declaration reaches it.

    This is exactly what the bug stripped: rebinding the same agent to the
    text-only ``call_llm`` shim drops the tools — proven by the contrast half,
    where the shim, handed the SAME tool-carrying request, yields a single text
    part and NEVER a ``function_call``.
    """
    from google.adk.models.llm_request import LlmRequest
    from google.adk.runners import InMemoryRunner
    from google.genai import types as genai_types

    from zicato.adapters.adk import (
        rebind_tree_models_to_adk_model,
        rebind_tree_models_to_call_llm,
    )
    from zicato.testing.adk_fake import TextTurn, make_fake_adk_model

    # --- config-driven inner model: tools must survive. ---
    tool = _weather_tool()
    agent = LlmAgent(
        name="weatherbot",
        instruction="help the user",
        model="gemma-3-12b-it",  # a bare string the bug would have shimmed
        tools=[tool],
    )
    # A FakeADKModel records every LlmRequest it is driven with; it stands in
    # for the configured LiteLlm inner model (a function-calling BaseLlm).
    configured = make_fake_adk_model([TextTurn("Paris is sunny.")], model="configured-endpoint")
    rebound = rebind_tree_models_to_adk_model(agent, configured)
    assert rebound == 1
    assert agent.model is configured  # the inner model overrode the bare string

    runner = InMemoryRunner(agent, app_name="zicato-inner-model-test")
    await runner.session_service.create_session(
        app_name="zicato-inner-model-test", user_id="u", session_id="s"
    )
    message = genai_types.Content(role="user", parts=[genai_types.Part(text="weather in Paris?")])
    async for _event in runner.run_async(user_id="u", session_id="s", new_message=message):
        pass

    # ADK drove the configured model at least once and assembled the request
    # WITH the tool's function declarations on config.tools.
    assert configured.invocations, "the configured inner model must be driven"
    captured: Any = configured.invocations[0]
    config = getattr(captured, "config", None)
    declared_names: list[str] = []
    for adk_tool in getattr(config, "tools", None) or ():
        for decl in getattr(adk_tool, "function_declarations", None) or ():
            declared_names.append(decl.name)
    assert "lookup_weather" in declared_names, (
        "the inner-model rebind must preserve native tool-calling — the tool's "
        f"function_declaration must reach the model; saw {declared_names!r}"
    )

    # --- contrast: the text-only shim DROPS the tools. ---
    # A TOOL-FREE agent is the only kind the shim may displace (issue #98: a
    # tool-declaring agent keeps its native model or raises — see
    # ``test_rebind_raises_rather_than_silently_stripping_tools``). The shim it
    # gets is the same object either way, so handing THAT shim a
    # tool-carrying request still proves the no-tools limitation.
    shim_agent = LlmAgent(
        name="weatherbot2",
        instruction="help the user",
        model="gemma-3-12b-it",
    )

    async def call_llm(system: str, user: str, model: str) -> str:
        return "I cannot call tools; here is plain text."

    assert rebind_tree_models_to_call_llm(shim_agent, call_llm) == 1
    shim = shim_agent.model

    # Hand the shim a request that DOES carry tools, exactly as ADK would, and
    # confirm it answers with text and never a function_call — tools dropped.
    tool_config = genai_types.GenerateContentConfig(
        system_instruction="help the user",
        tools=[
            genai_types.Tool(
                function_declarations=[
                    genai_types.FunctionDeclaration(name="lookup_weather", description="weather")
                ]
            )
        ],
    )
    shim_request = LlmRequest(
        contents=[genai_types.Content(role="user", parts=[genai_types.Part(text="weather?")])],
        config=tool_config,
    )
    responses = [resp async for resp in shim.generate_content_async(shim_request)]
    shim_parts = responses[0].content.parts
    assert not any(
        getattr(p, "function_call", None) is not None for p in shim_parts
    ), "the text-only shim must NOT emit a function_call (it drops the tools)"
    assert any(
        getattr(p, "text", None) for p in shim_parts
    ), "the text-only shim yields a single text part"
