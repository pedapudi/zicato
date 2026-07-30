"""Tests for the worker's lazy model-spec role resolution (RUNTIME.md §5.5.8).

Every board unit is a fresh interpreter, and resolving a *model spec* role
calls ``build_adk_model``, which pulls the whole ``google.adk`` import graph
— measured at 0.73 s / 88 MB / ~1330 modules per worker. The worker used to
resolve every configured role eagerly at startup, so a unit whose entry has
no LLM judge (or which never reaches the auxiliary side) paid for ADK
anyway.

Two things are pinned here:

1. **The deferral is real** — a model-spec role does not build its ADK model
   until first call, and builds it exactly once.
2. **The posture does not regress** — importing
   :mod:`zicato._tournament_worker` must not pull ``google.adk`` or
   ``litellm`` into ``sys.modules``. This is the durable half of the change:
   a future eager import at module scope fails here instead of quietly
   taxing every board unit.

On the ``litellm`` half specifically: there is nothing to make lazy, because
ADK already defers it (``google.adk.models.lite_llm`` imports ``litellm``
from inside ``LiteLLMClient.acompletion``, i.e. at the first LLM call).
:func:`test_no_module_imports_litellm_at_module_scope` pins that fact rather
than a change.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from zicato._tournament_worker import _resolve_role_call_llm
from zicato.models_config import lazy_text_call_llm, role_spec_from_dict

_MODEL_SPEC = {
    "model": "provider/some-model",
    "endpoint": "http://127.0.0.1:1/v1",
    "api_key_env": "NOT_SET_IN_THIS_TEST",
}


# ---------------------------------------------------------------------------
# 1. The deferral
# ---------------------------------------------------------------------------


async def test_model_spec_role_defers_the_adk_build_to_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is built at resolve time; the first call builds it once."""
    import zicato.models_config as models_config

    builds: list[str] = []

    def _fake_resolve(spec: object, *, role: str) -> object:
        builds.append(role)

        async def _call(system: str, user: str, model: str) -> str:
            del system, user, model
            return "ok"

        return _call

    monkeypatch.setattr(models_config, "_resolve_model_spec_call_llm", _fake_resolve)

    call_llm = lazy_text_call_llm(role_spec_from_dict(_MODEL_SPEC), role="judge")
    assert builds == [], "resolving a model-spec role must not build the ADK model"

    assert await call_llm("sys", "usr", "m") == "ok"
    assert builds == ["judge"], "the first call builds it"

    assert await call_llm("sys", "usr", "m") == "ok"
    assert builds == ["judge"], "the build is cached, not repeated per call"


def test_dotted_role_is_still_resolved_eagerly_and_unwrapped() -> None:
    """A ``call_llm`` dotted path never touched ADK, so it is not wrapped."""
    from tests._subprocess_worker_support import harness_call_llm

    spec = role_spec_from_dict({"call_llm": "tests._subprocess_worker_support:harness_call_llm"})
    assert lazy_text_call_llm(spec, role="harness") is harness_call_llm


async def test_deferred_resolution_failure_is_registered_and_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shape-valid, unresolvable spec must not become a silent "no signal".

    The judge path swallows every exception by hard contract (zicato's
    ``_InlineCriterionJudge`` and goldfive's ``DefaultSteerer`` both catch and
    log, because a misbehaving judge must not crash a run). Deferring
    resolution therefore moves a CONFIG fault — the ``adk`` extra missing, a
    model id ADK cannot resolve — from "worker exits non-zero at startup" to
    "judge reports no drift on every observation point, and the unit banks a
    scalar better than the truth". The register is what keeps that impossible.
    """
    import zicato.models_config as models_config

    def _boom(spec: object, *, role: str) -> object:
        raise ValueError(f"models.{role}: could not build a call_llm")

    monkeypatch.setattr(models_config, "_resolve_model_spec_call_llm", _boom)
    models_config.clear_deferred_role_failures()
    try:
        call_llm = lazy_text_call_llm(role_spec_from_dict(_MODEL_SPEC), role="judge")
        assert models_config.deferred_role_failures() == {}, "nothing has been called yet"

        with pytest.raises(models_config.RoleResolutionError):
            await call_llm("s", "u", "m")
        assert "judge" in models_config.deferred_role_failures()
    finally:
        models_config.clear_deferred_role_failures()


def test_worker_main_exits_nonzero_when_a_deferred_role_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A registered failure fails the UNIT, exactly as the eager path did.

    A non-zero worker exit is what the runner turns into an infra abort (which
    is never cached, so a later round re-attempts it). Anything less would let
    a misconfigured judge bank a score.
    """
    import zicato._tournament_worker as worker
    import zicato.models_config as models_config

    args_path = tmp_path / "args.json"
    args_path.write_text("{}", encoding="utf-8")

    async def _noop_run(args: dict[str, object]) -> None:
        del args

    monkeypatch.setattr(worker, "_load_args", lambda path: {"instance_id": "test"})
    monkeypatch.setattr(worker, "_run", _noop_run)
    monkeypatch.setattr(worker, "_install_worker_log_stream_from_args", lambda args: None)

    models_config.clear_deferred_role_failures()
    try:
        assert worker.main([str(args_path)]) == 0, "a clean run exits 0"

        # Now with a registered failure, the same clean run must fail the unit.
        models_config._DEFERRED_ROLE_FAILURES["judge"] = "could not build a call_llm"
        assert worker.main([str(args_path)]) == 1
    finally:
        models_config.clear_deferred_role_failures()


def test_malformed_spec_still_fails_eagerly() -> None:
    """A spec naming neither form fails at resolve time, not mid-run.

    Deferring the ADK build must not defer *config validation* — a broken
    ``models`` block has to surface at worker startup, where it is
    debuggable.
    """
    with pytest.raises(ValueError, match="neither a call_llm dotted path nor a model string"):
        lazy_text_call_llm(role_spec_from_dict({}), role="auxiliary")


def test_each_resolution_returns_a_distinct_callable() -> None:
    """The harness/auxiliary collusion guard compares identity.

    Two roles resolved from the same spec must not collapse into one
    object, or ``assert_distinct_callables`` would reject a legitimate
    configuration.
    """
    from zicato.core.workspace import assert_distinct_callables

    spec = role_spec_from_dict(_MODEL_SPEC)
    first = lazy_text_call_llm(spec, role="harness")
    second = lazy_text_call_llm(spec, role="auxiliary")
    assert first is not second
    assert_distinct_callables(first, second)


async def test_worker_role_resolution_uses_the_lazy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_role_call_llm`` routes a ``models_role`` spec through it."""
    import zicato.models_config as models_config

    builds: list[str] = []

    def _fake_resolve(spec: object, *, role: str) -> object:
        builds.append(role)

        async def _call(system: str, user: str, model: str) -> str:
            del system, user, model
            return "ok"

        return _call

    monkeypatch.setattr(models_config, "_resolve_model_spec_call_llm", _fake_resolve)

    call_llm = _resolve_role_call_llm({"models_role": _MODEL_SPEC}, role="auxiliary")
    assert builds == [], "the worker must not build ADK models at startup"
    assert await call_llm("s", "u", "m") == "ok"
    assert builds == ["auxiliary"]


# ---------------------------------------------------------------------------
# 2. The import posture (the durable guard)
# ---------------------------------------------------------------------------

_POSTURE_SOURCE = """
import sys
import zicato._tournament_worker  # noqa: F401
import zicato.adapters.adk  # noqa: F401
import zicato.models_config  # noqa: F401
import zicato.runtime_factory  # noqa: F401
leaked = sorted(
    name
    for name in sys.modules
    if name == "litellm" or name == "google.adk" or name.startswith(("litellm.", "google.adk."))
)
print(":".join(leaked))
"""


@pytest.mark.slow
@pytest.mark.integration
def test_worker_import_does_not_pull_adk_or_litellm() -> None:
    """The worker's import graph stays dependency-light.

    Runs in a FRESH interpreter because the pytest process has almost
    certainly imported ADK already via some other test. A regression here
    means every board unit on every board pays 0.73 s / 88 MB (ADK) or
    1.06 s / 126 MB (litellm) it does not need — see RUNTIME.md §5.5.1.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _POSTURE_SOURCE],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    leaked = [name for name in proc.stdout.strip().split(":") if name]
    assert leaked == [], (
        "importing the worker must not pull the heavy optional graph; "
        f"eagerly imported: {leaked}"
    )


@pytest.mark.slow
@pytest.mark.integration
def test_no_module_imports_litellm_at_module_scope() -> None:
    """``litellm`` is already lazy — ADK imports it at the first LLM call.

    Pins the fact the design note records (RUNTIME.md §5.5.8): merely
    importing ``google.adk.models.lite_llm`` and CONSTRUCTING a ``LiteLlm``
    does not pull ``litellm`` in. So there is no eager ``litellm`` import to
    remove anywhere, and the +1.06 s / +126 MB is paid inside the unit's
    wall-clock budget at the first call instead. Skipped when the ``adk``
    extra is absent.
    """
    pytest.importorskip("google.adk")
    source = (
        "import sys\n"
        "from google.adk.models.lite_llm import LiteLlm\n"
        "LiteLlm(model='provider/m', api_base='http://127.0.0.1:1/v1', api_key='k')\n"
        "print('litellm' in sys.modules)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", (
        "ADK now imports litellm eagerly — RUNTIME.md §5.5.8's adjudication "
        "needs revisiting, and a real lazy-import win may now exist"
    )
