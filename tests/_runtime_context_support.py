"""Write the typed context inherited by a nested-process fixture."""

import json
from pathlib import Path

from zicato.core.run_context import RunContext
from zicato.core.runtime_context import TelemetryEndpoints, WorkerRuntimeContext
from zicato.runtime.context import RUNTIME_CONTEXT_ENV


def install_runtime_context(monkeypatch, tmp_path: Path, *, web_url=None, grpc_target=None):
    path = tmp_path / "worker-context.json"
    existing = WorkerRuntimeContext(RunContext(tmp_path, "e0", "v0", "run", tmp_path, None))
    if path.exists():
        existing = WorkerRuntimeContext.from_json(json.loads(path.read_text())["runtime_context"])
    endpoints = TelemetryEndpoints(
        existing.telemetry.web_url if web_url is None else web_url,
        existing.telemetry.grpc_target if grpc_target is None else grpc_target,
    )
    path.write_text(
        json.dumps({"runtime_context": WorkerRuntimeContext(existing.run, endpoints).to_json()})
    )
    monkeypatch.setenv(RUNTIME_CONTEXT_ENV, str(path))
    return path
