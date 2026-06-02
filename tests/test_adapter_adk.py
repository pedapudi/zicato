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
    """``_entry_disable_drift`` reads the board-level set off ``entry.context``.

    ``disable_drift`` is a board-LEVEL setting; the tournament runner
    stamps it onto each entry's ``context['disable_drift']`` (see
    ``zicato.tournament.runner._stamp_disable_drift``). The adapter reads
    that one channel — a comma / whitespace separated list of drift-kind
    wire strings.
    """
    from zicato.adapters.adk import _entry_disable_drift

    # comma-separated wire strings.
    comma = _EntryStub(
        id="e",
        kind="single_turn",
        wall_clock_budget_seconds=5,
        context={"disable_drift": "tool_error, agent_refusal"},
    )
    assert _entry_disable_drift(comma) == ("tool_error", "agent_refusal")

    # whitespace-separated wire strings (the form the runner stamps).
    spaced = _EntryStub(
        id="e",
        kind="single_turn",
        wall_clock_budget_seconds=5,
        context={"disable_drift": "tool_error agent_refusal"},
    )
    assert _entry_disable_drift(spaced) == ("tool_error", "agent_refusal")

    # a plain BoardEntry with no disable_drift in context -> empty tuple.
    plain = BoardEntry(id="e", kind="single_turn", wall_clock_budget_seconds=5, input="x")
    assert _entry_disable_drift(plain) == ()


def test_entry_judge_only_reads_context() -> None:
    """``_entry_judge_only`` reads the board-level flag off ``entry.context``.

    ``judge_only`` is a board-LEVEL setting; the tournament runner stamps
    it onto each entry's ``context['judge_only']`` as the wire string
    ``"true"`` (see ``zicato.tournament.runner._stamp_judge_only``). The
    adapter reads that one channel.
    """
    from zicato.adapters.adk import _JUDGE_ONLY_CONTEXT_KEY, _entry_judge_only

    on = _EntryStub(
        id="e",
        kind="single_turn",
        wall_clock_budget_seconds=5,
        context={_JUDGE_ONLY_CONTEXT_KEY: "true"},
    )
    assert _entry_judge_only(on) is True

    off = _EntryStub(
        id="e",
        kind="single_turn",
        wall_clock_budget_seconds=5,
        context={_JUDGE_ONLY_CONTEXT_KEY: "false"},
    )
    assert _entry_judge_only(off) is False

    # a plain BoardEntry with no judge_only in context -> False.
    plain = BoardEntry(id="e", kind="single_turn", wall_clock_budget_seconds=5, input="x")
    assert _entry_judge_only(plain) is False


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
        "planner" in seen and "goal_deriver" in seen
    ), "judge_only ON must spread the no-steering overrides into goldfive.run"
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
    assert "planner" not in seen and "goal_deriver" not in seen, (
        "judge_only OFF must NOT pass any steering override — byte-identical " "to the legacy path"
    )
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

    from zicato.adapters.adk import _entry_judge_only, _judge_only_overrides

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
    assert _entry_judge_only(on_entry) is True
    sink_on = InMemorySink()
    await goldfive.run(
        agent,
        on_entry.input,
        sinks=[sink_on],
        call_llm=stub_call_llm,
        judges=[],  # judges stay armed in real usage; empty here keeps the stub deterministic
        **_judge_only_overrides(agent),
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
    monkeypatch.delenv("ZICATO_HARNESS_CALL_TIMEOUT_MS", raising=False)
    runtime = _goldfive_runtime()
    assert runtime.agent.call_timeout_ms == 1_800_000
    assert runtime.agent.call_timeout_ms > 120_000


def test_goldfive_runtime_honours_zicato_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ZICATO_HARNESS_CALL_TIMEOUT_MS`` tunes the per-call budget."""
    monkeypatch.delenv("GOLDFIVE_AGENT_CALL_TIMEOUT_MS", raising=False)
    monkeypatch.setenv("ZICATO_HARNESS_CALL_TIMEOUT_MS", "456000")
    runtime = _goldfive_runtime()
    assert runtime.agent.call_timeout_ms == 456000


def test_goldfive_runtime_defers_to_explicit_goldfive_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``GOLDFIVE_AGENT_CALL_TIMEOUT_MS`` is not overridden."""
    monkeypatch.setenv("GOLDFIVE_AGENT_CALL_TIMEOUT_MS", "999000")
    monkeypatch.setenv("ZICATO_HARNESS_CALL_TIMEOUT_MS", "111000")
    runtime = _goldfive_runtime()
    # goldfive's own env value wins; zicato does not override it.
    assert runtime.agent.call_timeout_ms == 999000


@pytest.mark.asyncio
async def test_run_single_turn_forwards_runtime_to_goldfive_run(
    inner_harness: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``goldfive.run`` receives a ``runtime`` carrying the raised timeout."""
    monkeypatch.delenv("GOLDFIVE_AGENT_CALL_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("ZICATO_HARNESS_CALL_TIMEOUT_MS", raising=False)
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
