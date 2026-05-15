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
    _outcome_transcript,
    _split_entrypoint,
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

    async def fake_goldfive_run(
        agent: Any, user_input: str, **kwargs: Any
    ) -> _FakeOutcome:
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
