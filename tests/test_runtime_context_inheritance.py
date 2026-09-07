"""Distinct worker invocations retain their own context through a nested process."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from tests._runtime_builders import make_generation
from tests._runtime_context_support import install_runtime_context
from zicato.config import resolve_configuration
from zicato.core.configuration import ConfigurationError
from zicato.core.run_context import RunContext
from zicato.core.runtime_context import TelemetryEndpoints, WorkerRuntimeContext
from zicato.core.workspace import events_jsonl_path, loss_profile_path
from zicato.evolve.lifecycle_services import _resolve_or_launch_harmonograf
from zicato.runtime.context import RUNTIME_CONTEXT_ENV, inherited_runtime_context


def test_absent_context_uses_no_inherited_runtime(monkeypatch):
    monkeypatch.delenv(RUNTIME_CONTEXT_ENV, raising=False)
    assert inherited_runtime_context() is None


@pytest.mark.parametrize(
    "contents", [None, "not json", "{}", '{"runtime_context":{"telemetry":{"grpc_target":7}}}']
)
def test_broken_configured_context_fails_before_endpoint_resolution(
    tmp_path, monkeypatch, contents
):
    pointer = tmp_path / "missing-or-invalid.json"
    if contents is not None:
        pointer.write_text(contents)
    monkeypatch.setenv(RUNTIME_CONTEXT_ENV, str(pointer))
    with pytest.raises(ConfigurationError):
        inherited_runtime_context()


def test_inherited_native_target_is_scoped_to_its_browser_url(tmp_path, monkeypatch):
    from zicato.telemetry.sink import resolve_harmonograf_grpc_target

    install_runtime_context(
        monkeypatch, tmp_path, web_url="http://parent:8010", grpc_target="parent:8011"
    )
    assert resolve_harmonograf_grpc_target("http://parent:8010") == "parent:8011"
    assert resolve_harmonograf_grpc_target("http://another:8020") == "another:8020"


def test_explicit_endpoint_does_not_consult_an_unusable_inherited_pointer(tmp_path, monkeypatch):
    from zicato.core.settings import IntegrationConfig

    monkeypatch.setenv(RUNTIME_CONTEXT_ENV, str(tmp_path / "missing.json"))
    url, handle = _resolve_or_launch_harmonograf(
        tmp_path, IntegrationConfig(harmonograf_url="http://selected:8020")
    )
    assert url == "http://selected:8020"
    assert handle.grpc_target == "selected:8020"


def test_standalone_discovery_creates_no_service_files(tmp_path):
    from zicato.telemetry.harmonograf_supervisor import find_workspace_harmonograf

    assert find_workspace_harmonograf(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_interleaved_workers_and_real_nested_children_keep_distinct_contexts(tmp_path, monkeypatch):
    monkeypatch.delenv(RUNTIME_CONTEXT_ENV, raising=False)
    script = (
        "import sys; sys.modules['harmonograf_client'] = None; "
        "from zicato._tournament_worker import main; raise SystemExit(main())"
    )

    async def run(index):
        workspace = tmp_path / str(index) / ".zicato"
        workspace.mkdir(parents=True)
        generation = make_generation(workspace)
        run_id = f"context-{index}"
        endpoints = TelemetryEndpoints(f"http://parent-{index}:8010", f"parent-{index}:8011")
        context = WorkerRuntimeContext(
            endpoints,
            RunContext(workspace, "e0", generation.id, run_id, generation.snapshot_root, None),
        )
        result_path = workspace / "worker-result.json"
        args_path = workspace / "worker-args.json"
        args_path.write_text(
            json.dumps(
                {
                    "workspace_root": str(workspace),
                    "epoch_id": "e0",
                    "generation_id": generation.id,
                    "snapshot_root": str(generation.snapshot_root),
                    "run_id": run_id,
                    "entry": {
                        "id": "probe",
                        "kind": "single_turn",
                        "input": "context probe",
                        "wall_clock_budget_seconds": 10,
                    },
                    "adapter": {
                        "kind": "import",
                        "factory": (
                            "tests._subprocess_worker_support:make_nested_context_probe_adapter"
                        ),
                    },
                    "target_role": {"dotted": "tests._subprocess_worker_support:target_call_llm"},
                    "evaluation_role": {
                        "dotted": "tests._subprocess_worker_support:evaluation_call_llm"
                    },
                    "sink_events_path": str(
                        events_jsonl_path(workspace, "e0", generation.id, "probe")
                    ),
                    "loss_path": str(loss_profile_path(workspace, "e0", generation.id, "probe")),
                    "result_path": str(result_path),
                    "weights": {},
                    "configuration": resolve_configuration({}).to_json(),
                    "runtime_context": context.to_json(),
                }
            )
        )
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(args_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(worker.communicate(), timeout=30)
        assert worker.returncode == 0, (stdout.decode(), stderr.decode())
        observed = json.loads(json.loads(result_path.read_text())["run_result"]["final_output"])
        assert observed["worker_url"] == endpoints.web_url
        assert observed["worker_grpc"] == endpoints.grpc_target
        assert observed["nested_context"] == context.to_json()
        return observed["nested_context"]

    async def interleave():
        return await asyncio.gather(run(1), run(2))

    first, second = asyncio.run(interleave())
    assert first != second
    assert RUNTIME_CONTEXT_ENV not in os.environ
