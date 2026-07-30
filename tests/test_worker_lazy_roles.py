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

import json
import os
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


async def test_inner_model_resolved_after_active_run_and_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The heavy inner-model build happens AFTER liveness registration.

    ``_resolve_inner_model_from_role`` forces the ``google.adk`` import for
    an endpoint-shaped harness role (~1 s / 80 MB — RUNTIME.md §5.5.8), and
    ``ADKHarnessAdapter.load`` pays that same cost unconditionally moments
    later regardless, so deferring the build past ``.load()`` saves nothing
    on TOTAL worker cost. What it DOES change, and what this test pins: the
    worker's ``active_runs`` state write and heartbeat-thread start — the
    two things the supervisor watchdog and staleness check key liveness on
    — must happen BEFORE that ~1 s import, not after, so a live worker
    never looks unregistered while it pays a tax the watchdog does not care
    it pays eagerly. Everything ADK/goldfive-shaped is mocked out; only the
    ORDER of the three calls is under test.
    """
    import zicato._tournament_worker as worker
    import zicato.runtime.heartbeat as heartbeat_mod
    import zicato.runtime.state as state_mod
    from zicato.core.workspace import events_jsonl_path, loss_profile_path

    order: list[str] = []

    def _fake_write_active_run(root: object, record: object) -> None:
        del root, record
        order.append("active_run_write")

    class _FakeHeartbeat:
        def __init__(self, *a: object, **kw: object) -> None:
            del a, kw

        def start(self) -> None:
            order.append("heartbeat_start")

        def stop(self) -> None:
            pass

    def _fake_resolve_inner_model(spec: object) -> None:
        del spec
        order.append("inner_model_resolve")
        return None

    class _FakeSession:
        async def run(self, entry: object, sink_path: Path) -> None:
            del entry
            sink_path.parent.mkdir(parents=True, exist_ok=True)
            sink_path.write_text("", encoding="utf-8")

    class _FakeAdapter:
        def load(self, generation_root: object) -> _FakeSession:
            del generation_root
            return _FakeSession()

    # ``_run`` does ``from zicato.runtime import state as state_mod`` and
    # ``from zicato.runtime.heartbeat import RunHeartbeatBeater`` INSIDE the
    # function body, freshly on every call — patching the defining modules
    # (not ``worker``'s own namespace, which never binds these names) is
    # what a fresh ``import`` picks up.
    monkeypatch.setattr(state_mod, "write_active_run", _fake_write_active_run)
    monkeypatch.setattr(heartbeat_mod, "RunHeartbeatBeater", _FakeHeartbeat)
    monkeypatch.setattr(worker, "_resolve_inner_model_from_role", _fake_resolve_inner_model)
    monkeypatch.setattr(worker, "_build_adapter", lambda spec: _FakeAdapter())

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "snap").mkdir()
    args = {
        "workspace_root": str(workspace),
        "epoch_id": "e0",
        "generation_id": "v0",
        "snapshot_root": str(workspace / "snap"),
        "entry": {
            "id": "entry_a",
            "kind": "single_turn",
            "wall_clock_budget_seconds": 60,
            "input": "hello",
        },
        "adapter": {"kind": "adk", "entrypoint": "unused:unused"},
        # Endpoint-shaped, so a real (unmocked) resolve would import ADK —
        # the fake above just records the call instead.
        "harness_role": {"models_role": dict(_MODEL_SPEC)},
        "auxiliary_role": {"dotted": "tests._subprocess_worker_support:auxiliary_call_llm"},
        "sink_events_path": str(events_jsonl_path(workspace, "e0", "v0", "entry_a")),
        "loss_path": str(loss_profile_path(workspace, "e0", "v0", "entry_a")),
        "result_path": str(tmp_path / "result.json"),
        "instance_id": "test",
        "seed": None,
        "harmonograf_url": "",
        "weights": {},
    }

    await worker._run(args)

    assert order == ["active_run_write", "heartbeat_start", "inner_model_resolve"], (
        "the active_run write and heartbeat start must precede the inner-model "
        f"resolve (the ADK-import trigger), got order={order}"
    )


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


def _write_inner_model_gate_args(
    args_path: Path, *, workspace: Path, generation_snapshot: Path, result_path: Path
) -> None:
    """A worker args file whose harness role is endpoint-shaped but whose
    adapter kind is ``"import"`` (a :class:`~tests._subprocess_worker_support.
    StubAdapter`, never an ADK adapter).

    Only ``ADKHarnessAdapter.run`` ever reads ``config.inner_model``
    (``adapters/adk.py``), so a non-``"adk"`` worker must never pay the
    ``google.adk`` import tax to build one — see the adapter-kind gate on
    ``_resolve_inner_model_from_role`` in ``_tournament_worker._run``.
    """
    from zicato.core.workspace import events_jsonl_path, loss_profile_path

    sink_path = events_jsonl_path(workspace, "e0", "v0", "entry_a")
    loss_path = loss_profile_path(workspace, "e0", "v0", "entry_a")
    payload = {
        "workspace_root": str(workspace),
        "epoch_id": "e0",
        "generation_id": "v0",
        "snapshot_root": str(generation_snapshot),
        "entry": {
            "id": "entry_a",
            "kind": "single_turn",
            "wall_clock_budget_seconds": 60,
            "input": "hello",
        },
        "adapter": {
            "kind": "import",
            "factory": "tests._subprocess_worker_support:make_stub_adapter",
        },
        # Endpoint-shaped: exactly the live-validation shape that forces
        # ``build_adk_model`` to import ``google.adk`` when it IS resolved.
        "harness_role": {
            "models_role": {
                "model": "openai/fake-model",
                "endpoint": "http://127.0.0.1:1/v1",
                "api_key_env": "ZICATO_TEST_UNSET_KEY_GATE",
            }
        },
        "auxiliary_role": {"dotted": "tests._subprocess_worker_support:auxiliary_call_llm"},
        "sink_events_path": str(sink_path),
        "loss_path": str(loss_path),
        "result_path": str(result_path),
        "instance_id": "test",
        "seed": None,
        "harmonograf_url": "",
        "weights": {},
    }
    args_path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.slow
@pytest.mark.integration
def test_non_adk_adapter_never_resolves_inner_model(tmp_path: Path) -> None:
    """A ``"import"``-kind worker skips ``google.adk`` even with an
    endpoint-shaped harness role.

    Runs a REAL end-to-end worker (stub adapter, no goldfive / real LLM)
    in a fresh interpreter and asserts both that it completes cleanly AND
    that ``google.adk`` never lands in ``sys.modules`` — the adapter-kind
    gate on ``_resolve_inner_model_from_role`` (only the ADK adapter ever
    reads ``config.inner_model``) is what makes that true; before the gate
    existed, this exact args shape imported the whole ADK graph for a value
    the stub adapter's session never even looks at.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation_snapshot = workspace / "snap" / "v0"
    generation_snapshot.mkdir(parents=True)
    args_path = tmp_path / "args.json"
    result_path = tmp_path / "result.json"
    _write_inner_model_gate_args(
        args_path,
        workspace=workspace,
        generation_snapshot=generation_snapshot,
        result_path=result_path,
    )

    root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root if not existing else f"{root}{os.pathsep}{existing}"

    source = (
        "import sys\n"
        "from zicato._tournament_worker import main\n"
        f"rc = main([{str(args_path)!r}])\n"
        "leaked = sorted(\n"
        "    name for name in sys.modules\n"
        "    if name == 'google.adk' or name.startswith('google.adk.')\n"
        ")\n"
        "print(rc)\n"
        "print(':'.join(leaked))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out_lines = proc.stdout.strip().splitlines()
    worker_rc, leaked_line = out_lines[0], (out_lines[1] if len(out_lines) > 1 else "")
    assert worker_rc == "0", f"worker itself should exit cleanly; stderr: {proc.stderr}"
    leaked = [name for name in leaked_line.split(":") if name]
    assert leaked == [], (
        "a non-adk adapter worker must never import google.adk, even with an "
        f"endpoint-shaped harness role: eagerly imported {leaked}"
    )
    assert json.loads(result_path.read_text(encoding="utf-8"))["aborted"] is False


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
