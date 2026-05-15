"""Tests for adversarial + clean entry runners and the dotted-path resolver."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from zicato.core.types import BoardEntry, RuntimeConfig
from zicato.synthetic.adversarial import (
    AdversarialResolutionError,
    resolve_adversarial_agent,
    run_adversarial_entry,
)
from zicato.synthetic.clean import run_clean_entry

# ---------------------------------------------------------------------------
# Stub agents — used in dotted-path resolution tests
# ---------------------------------------------------------------------------


class _StubLoopingAgent:
    """Marker class — used to verify ``resolve_adversarial_agent`` returns identity."""

    last_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        type(self).last_kwargs = kwargs


class _StubCleanAgent:
    def __init__(self) -> None:
        self.invoked = False


# ---------------------------------------------------------------------------
# Test config
# ---------------------------------------------------------------------------


async def _harness_call_llm(system: str, user: str, model: str) -> str:
    return "harness response"


async def _auxiliary_call_llm(system: str, user: str, model: str) -> str:
    return "auxiliary response"


def _make_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        harness_call_llm=_harness_call_llm,
        auxiliary_call_llm=_auxiliary_call_llm,
    )


# ---------------------------------------------------------------------------
# resolve_adversarial_agent
# ---------------------------------------------------------------------------


def test_resolve_agent_colon_form_returns_class() -> None:
    cls = resolve_adversarial_agent(
        f"{__name__}:_StubLoopingAgent"
    )
    assert cls is _StubLoopingAgent


def test_resolve_agent_dotted_form_returns_class() -> None:
    cls = resolve_adversarial_agent(
        f"{__name__}._StubLoopingAgent"
    )
    assert cls is _StubLoopingAgent


def test_resolve_agent_empty_spec_raises() -> None:
    with pytest.raises(AdversarialResolutionError, match="non-empty"):
        resolve_adversarial_agent("")


def test_resolve_agent_whitespace_only_spec_raises() -> None:
    with pytest.raises(AdversarialResolutionError, match="non-empty"):
        resolve_adversarial_agent("   ")


def test_resolve_agent_non_string_spec_raises() -> None:
    with pytest.raises(AdversarialResolutionError, match="non-empty"):
        resolve_adversarial_agent(None)  # type: ignore[arg-type]


def test_resolve_agent_bare_name_raises() -> None:
    # No dot, no colon — there is no way to split this into module + attr.
    with pytest.raises(AdversarialResolutionError, match="must be either"):
        resolve_adversarial_agent("LoopingAgent")


def test_resolve_agent_colon_form_with_empty_module_raises() -> None:
    with pytest.raises(AdversarialResolutionError, match="empty"):
        resolve_adversarial_agent(":LoopingAgent")


def test_resolve_agent_colon_form_with_empty_attr_raises() -> None:
    with pytest.raises(AdversarialResolutionError, match="empty"):
        resolve_adversarial_agent("some.module:")


def test_resolve_agent_unknown_module_raises_with_clear_message() -> None:
    with pytest.raises(AdversarialResolutionError) as excinfo:
        resolve_adversarial_agent("definitely.not.a.real.module:Foo")
    assert "could not import module" in str(excinfo.value)
    assert "definitely.not.a.real.module" in str(excinfo.value)
    # Original cause chained for the operator's traceback.
    assert isinstance(excinfo.value.__cause__, ImportError)


def test_resolve_agent_missing_attribute_raises_with_clear_message() -> None:
    with pytest.raises(AdversarialResolutionError) as excinfo:
        resolve_adversarial_agent(f"{__name__}:DefinitelyNotAClass")
    assert "no attribute" in str(excinfo.value)
    assert "DefinitelyNotAClass" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, AttributeError)


# ---------------------------------------------------------------------------
# run_adversarial_entry — uses a fake goldfive module so we do not depend
# on the upstream testkit being merged.
# ---------------------------------------------------------------------------


class _FakeOutcome:
    """Duck-typed substitute for goldfive's ``ExecutionOutcome``."""

    def __init__(self, final_output: str, transcript: tuple[str, ...]) -> None:
        self.final_output = final_output
        self.transcript = transcript


class _FakeRunner:
    """Test-side stand-in for the runner ``goldfive.wrap`` returns."""

    def __init__(self, outcome: _FakeOutcome, *, delay: float = 0.0) -> None:
        self._outcome = outcome
        self._delay = delay
        self.last_user_input: str | None = None

    async def run(self, user_input: str) -> _FakeOutcome:
        self.last_user_input = user_input
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._outcome


@pytest.fixture
def fake_goldfive(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install a fake ``goldfive`` module exposing a stub ``wrap()``.

    The fake records the agent / sinks / call_llm it was handed and
    returns a configurable :class:`_FakeRunner`. Removed automatically
    by ``monkeypatch`` teardown.
    """
    state: dict[str, Any] = {
        "wrap_calls": [],
        "outcome": _FakeOutcome("final answer", ("final answer",)),
        "delay": 0.0,
    }

    def _wrap(agent: Any, *, sinks: list, call_llm: Any, **_kwargs: Any) -> _FakeRunner:
        state["wrap_calls"].append(
            {"agent": agent, "sinks": list(sinks), "call_llm": call_llm}
        )
        return _FakeRunner(state["outcome"], delay=state["delay"])

    fake_module = types.ModuleType("goldfive")
    fake_module.wrap = _wrap  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "goldfive", fake_module)
    return state


# ---------------------------------------------------------------------------
# Stub agents and helpers used by the runner tests
# ---------------------------------------------------------------------------


class _LocalLoopingAgent:
    """Local stub of a known-bad agent — the spec under test points here."""

    def __init__(self, severity: str = "warning") -> None:
        self.severity = severity


class _NoArgAgent:
    def __init__(self) -> None:
        self.constructed = True


class _PickyAgent:
    """Constructor requires a specific kwarg — used to test args parsing."""

    def __init__(self, *, theme: str) -> None:
        self.theme = theme


def _make_adversarial_entry(
    spec: str,
    *,
    input_msg: str = "produce drift please",
    required: tuple[str, ...] = ("looping_reasoning",),
    context: dict[str, str] | None = None,
    budget: int = 30,
) -> BoardEntry:
    return BoardEntry(
        id="syn_adv_1",
        kind="synthetic_adversarial",
        wall_clock_budget_seconds=budget,
        input=input_msg,
        adversarial_agent_spec=spec,
        required_drift_kinds=required,
        context=context or {},
    )


async def test_run_adversarial_entry_drives_agent_through_wrap(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    pytest.importorskip("zicato.synthetic.adversarial")  # belt-and-braces
    entry = _make_adversarial_entry(f"{__name__}:_LocalLoopingAgent")
    sinks: list = ["sink_a", "sink_b"]
    config = _make_config(tmp_path)

    result = await run_adversarial_entry(entry, sinks, config)

    assert len(fake_goldfive["wrap_calls"]) == 1
    call = fake_goldfive["wrap_calls"][0]
    assert isinstance(call["agent"], _LocalLoopingAgent)
    assert call["sinks"] == ["sink_a", "sink_b"]
    assert call["call_llm"] is _harness_call_llm
    assert result.entry_id == "syn_adv_1"
    assert result.final_output == "final answer"
    assert result.transcript == ("final answer",)
    assert result.aborted is False
    assert result.abort_reason == ""


async def test_run_adversarial_entry_parses_kwargs_from_context(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    entry = _make_adversarial_entry(
        f"{__name__}:_PickyAgent",
        context={"args": '{"theme": "loops"}'},
    )
    result = await run_adversarial_entry(entry, [], _make_config(tmp_path))
    assert result.aborted is False
    call = fake_goldfive["wrap_calls"][0]
    assert isinstance(call["agent"], _PickyAgent)
    assert call["agent"].theme == "loops"


async def test_run_adversarial_entry_parses_positional_args_list(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    # JSON list form -> positional args.
    entry = _make_adversarial_entry(
        f"{__name__}:_LocalLoopingAgent",
        context={"args": '["critical"]'},
    )
    result = await run_adversarial_entry(entry, [], _make_config(tmp_path))
    assert result.aborted is False
    call = fake_goldfive["wrap_calls"][0]
    assert call["agent"].severity == "critical"


async def test_run_adversarial_entry_no_args_constructs_with_no_arguments(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    entry = _make_adversarial_entry(f"{__name__}:_NoArgAgent")
    result = await run_adversarial_entry(entry, [], _make_config(tmp_path))
    assert result.aborted is False
    call = fake_goldfive["wrap_calls"][0]
    assert isinstance(call["agent"], _NoArgAgent)
    assert call["agent"].constructed is True


async def test_run_adversarial_entry_rejects_wrong_kind(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    entry = BoardEntry(
        id="single",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="hello",
    )
    with pytest.raises(ValueError, match="synthetic_adversarial"):
        await run_adversarial_entry(entry, [], _make_config(tmp_path))


async def test_run_adversarial_entry_surfaces_resolution_error(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    entry = _make_adversarial_entry("definitely.not.a.module:Foo")
    with pytest.raises(AdversarialResolutionError):
        await run_adversarial_entry(entry, [], _make_config(tmp_path))


async def test_run_adversarial_entry_aborts_on_budget(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    # Force the fake runner to outlast the wall-clock budget.
    fake_goldfive["delay"] = 0.5
    entry = _make_adversarial_entry(
        f"{__name__}:_LocalLoopingAgent",
        budget=1,  # budgets are integer seconds
    )
    # The fake runner sleeps 0.5s which is under the 1s budget — verify
    # we DO get a normal completion in that case.
    result = await run_adversarial_entry(entry, [], _make_config(tmp_path))
    assert result.aborted is False

    # Now make the runner outlast a tiny budget. We monkey the asyncio
    # timeout path by setting delay > budget; budget must be > 0 per
    # BoardEntry.validate so we use the smallest legal value (1) with a
    # delay that exceeds it.
    fake_goldfive["delay"] = 1.2
    entry_short = _make_adversarial_entry(
        f"{__name__}:_LocalLoopingAgent",
        budget=1,
    )
    result_short = await run_adversarial_entry(
        entry_short, [], _make_config(tmp_path)
    )
    assert result_short.aborted is True
    assert result_short.abort_reason == "wall_clock_budget_exceeded"


async def test_run_adversarial_entry_aborts_on_runner_exception(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    # Install a wrap() that returns a runner whose run() raises.
    class _ExplodingRunner:
        async def run(self, _user_input: str) -> Any:
            raise RuntimeError("boom")

    def _wrap(agent: Any, **_kwargs: Any) -> _ExplodingRunner:
        return _ExplodingRunner()

    fake_module = sys.modules["goldfive"]
    fake_module.wrap = _wrap  # type: ignore[attr-defined]

    entry = _make_adversarial_entry(f"{__name__}:_LocalLoopingAgent")
    result = await run_adversarial_entry(entry, [], _make_config(tmp_path))
    assert result.aborted is True
    assert result.abort_reason.startswith("runner_exception:")
    assert "RuntimeError" in result.abort_reason


async def test_run_adversarial_entry_constructor_typeerror_raises(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    # _PickyAgent requires theme=; passing no args should fail at
    # instantiation, surfaced as AdversarialResolutionError.
    entry = _make_adversarial_entry(f"{__name__}:_PickyAgent")
    with pytest.raises(AdversarialResolutionError, match="could not be instantiated"):
        await run_adversarial_entry(entry, [], _make_config(tmp_path))


# ---------------------------------------------------------------------------
# run_clean_entry — exercises the fallback path AND an explicit override
# ---------------------------------------------------------------------------


def _make_clean_entry(
    *,
    context: dict[str, str] | None = None,
    budget: int = 30,
) -> BoardEntry:
    return BoardEntry(
        id="syn_clean_1",
        kind="synthetic_clean",
        wall_clock_budget_seconds=budget,
        input="hello",
        context=context or {},
    )


async def test_run_clean_entry_uses_explicit_override(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    entry = _make_clean_entry(
        context={"clean_agent_spec": f"{__name__}:_StubCleanAgent"}
    )
    result = await run_clean_entry(entry, [], _make_config(tmp_path))
    assert result.aborted is False
    call = fake_goldfive["wrap_calls"][0]
    assert isinstance(call["agent"], _StubCleanAgent)


async def test_run_clean_entry_falls_back_when_testkit_missing(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    # The default spec points at goldfive.testkit.adversarial:CleanAgent
    # which is NOT importable in this test (we only installed a fake
    # ``goldfive`` module with a ``wrap`` attribute). The clean runner
    # must fall back to its inline _FallbackCleanAgent rather than
    # raising.
    entry = _make_clean_entry()
    result = await run_clean_entry(entry, [], _make_config(tmp_path))
    assert result.aborted is False
    call = fake_goldfive["wrap_calls"][0]
    # The fallback agent class name encodes the contract.
    assert type(call["agent"]).__name__ == "_FallbackCleanAgent"


async def test_run_clean_entry_explicit_override_surfaces_errors(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    # Explicit overrides MUST be strict — the fallback is only for the
    # default-spec path. If the operator types a spec, they get an
    # actionable error rather than silent fallback.
    entry = _make_clean_entry(
        context={"clean_agent_spec": "definitely.not.a.module:Foo"}
    )
    with pytest.raises(AdversarialResolutionError):
        await run_clean_entry(entry, [], _make_config(tmp_path))


async def test_run_clean_entry_rejects_wrong_kind(
    fake_goldfive: dict[str, Any], tmp_path: Path
) -> None:
    entry = BoardEntry(
        id="single",
        kind="single_turn",
        wall_clock_budget_seconds=10,
        input="hello",
    )
    with pytest.raises(ValueError, match="synthetic_clean"):
        await run_clean_entry(entry, [], _make_config(tmp_path))


# ---------------------------------------------------------------------------
# A defensive test that goldfive.wrap is invoked through the live import
# path — only runs when goldfive's testkit is actually available.
# ---------------------------------------------------------------------------


def _testkit_available() -> bool:
    try:
        import importlib

        importlib.import_module("goldfive.testkit.adversarial")
        return True
    except ImportError:
        return False


@pytest.mark.skipif(
    not _testkit_available(),
    reason="goldfive.testkit.adversarial not available; testkit is being implemented in parallel",
)
async def test_run_adversarial_entry_with_real_testkit(tmp_path: Path) -> None:
    pytest.importorskip("goldfive")
    pytest.importorskip("goldfive.testkit.adversarial")

    entry = _make_adversarial_entry(
        "goldfive.testkit.adversarial:LoopingAgent",
        budget=5,
    )
    # We do not assert on outcome shape — only that the runner does not
    # raise unexpectedly. The expectation matchers are the real check.
    result = await run_adversarial_entry(entry, [], _make_config(tmp_path))
    assert result.entry_id == "syn_adv_1"
