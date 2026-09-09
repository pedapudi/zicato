"""A real target worker executes a tool through its captured local transport."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import get_args

from zicato.config import resolve_configuration
from zicato.core.adapter_config import DriverImportContext
from zicato.core.drift_kinds import DriftKind
from zicato.core.measurement import MeasurementDraw, MeasurementPurpose, measurement_artifact_path
from zicato.core.run_context import RunContext
from zicato.core.runtime_context import WorkerRuntimeContext
from zicato.core.workspace import run_dir, run_id_for_unit
from zicato.models_config import capture_execution_roles
from zicato.telemetry.reducer import read_loss_profile
from zicato.tournament.unit_cache import read_run_result


def test_root_capture_selects_final_visible_root_text():
    from google.adk.events import Event
    from google.genai.types import Content, FunctionCall, Part

    from zicato.adapters.adk import _root_output_capture

    capture = _root_output_capture("root")
    events = [
        Event(author="child", content=Content(role="model", parts=[Part(text="child output")])),
        Event(
            author="root", content=Content(role="model", parts=[Part(text="thought", thought=True)])
        ),
        Event(
            author="root", content=Content(role="model", parts=[Part(text="partial")]), partial=True
        ),
        Event(
            author="root",
            content=Content(
                role="model",
                parts=[
                    Part(text="tool preamble"),
                    Part(function_call=FunctionCall(name="tool", args={})),
                ],
            ),
        ),
        Event(author="root", content=Content(role="model", parts=[Part(text="first final")])),
        Event(author="root", content=Content(role="model", parts=[Part(text="last final")])),
        Event(author="root", content=Content(role="model", parts=[Part(text="")])),
    ]

    async def observe():
        for event in events:
            assert await capture.on_event_callback(invocation_context=None, event=event) is None

    asyncio.run(observe())
    assert capture.outputs == ["first final", "last final", ""]


def test_actual_completed_output_takes_precedence_over_task_summary():
    from types import SimpleNamespace

    from zicato.adapters.adk import _outcome_transcript

    outcome = SimpleNamespace(
        session=SimpleNamespace(
            completed_results={"task": "status summary", "historical": "legacy output"},
            completed_outputs={"task": "actual output"},
        )
    )
    assert _outcome_transcript(outcome) == ("actual output", "legacy output")


def test_default_credentials_remain_available_without_ambient_key_override(tmp_path, monkeypatch):
    from google.adk.models.google_llm import Gemini
    from google.auth.credentials import AnonymousCredentials

    from zicato.models_config import (
        RoleSpec,
        _build_captured_native_model,
        execution_roles_from_json,
    )

    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "captured-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "captured-region")
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "google.auth._cloud_sdk.get_application_default_credentials_path",
        lambda: str(tmp_path / "absent.json"),
    )
    credentials = AnonymousCredentials()
    resolved = []

    def default_credentials(**kwargs):
        resolved.append(kwargs)
        return credentials, "different-project"

    monkeypatch.setattr("google.auth.default", default_credentials)
    spec = RoleSpec(model=Gemini.model_fields["model"].default)
    roles = capture_execution_roles({"models": {"engines": {"target": spec.to_dict()}}})
    transport = execution_roles_from_json(roles)["target"]["transport"]
    assert transport["api_key_env"] is None and transport["credential_file"] is None
    monkeypatch.setenv("GOOGLE_API_KEY", "unrelated-ambient-key")
    model = _build_captured_native_model(spec, transport)
    client = model.api_client
    try:
        assert resolved
        assert client._api_client._credentials is credentials
        assert client._api_client.api_key is None
        assert client._api_client.project == "captured-project"
        assert client._api_client.location == "captured-region"
    finally:
        client.close()


def test_captured_native_transport_refuses_ambiguous_credential_references():
    import pytest

    from zicato.models_config import execution_roles_from_json

    transport = {
        "model_factory": "package:Model",
        "client_factory": "package:Client",
        "backend": True,
        "project": "project",
        "location": "region",
        "base_url": "http://127.0.0.1:1",
        "api_version": "v1",
        "api_key_env": None,
        "credential_file": None,
    }
    for references in (
        {"api_key_env": ""},
        {"credential_file": ""},
        {"api_key_env": "CREDENTIAL", "credential_file": "/configured/credential.json"},
    ):
        document = {
            "target": {"models_role": {"model": "local"}, "transport": {**transport, **references}}
        }
        with pytest.raises(ValueError, match="invalid captured native transport"):
            execution_roles_from_json(json.dumps(document).encode())


def test_real_worker_preserves_captured_native_transport_and_tool_protocol(tmp_path, monkeypatch):
    from google.adk.models.google_llm import Gemini

    from zicato.adapters.adk import ADKHarnessAdapter
    from zicato.tournament.worker_transport import adapter_worker_spec

    requests = []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            parts = (
                [{"functionCall": {"name": "write_report", "args": {"text": "tool completed"}}}]
                if len(requests) == 1
                else [{"text": "task completed"}]
            )
            response = json.dumps(
                {
                    "candidates": [
                        {"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 4,
                        "candidatesTokenCount": 3,
                        "totalTokenCount": 7,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("GOOGLE_API_KEY", "local-test-credential")
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
        monkeypatch.delenv("GOOGLE_GENAI_USE_ENTERPRISE", raising=False)
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", f"http://127.0.0.1:{endpoint.server_port}")
        declared = {
            "models": {
                "engines": {
                    "target": {"model": Gemini.model_fields["model"].default},
                    "evaluation": {
                        "call_llm": "tests._subprocess_worker_support:evaluation_call_llm"
                    },
                }
            }
        }
        roles = json.loads(capture_execution_roles(declared))
        assert "local-test-credential" not in json.dumps(roles)
        assert roles["target"]["transport"]["backend"] is False

        workspace, snapshot = tmp_path / ".zicato", tmp_path / "snapshot"
        package = snapshot / "local_target"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            "from pathlib import Path\n"
            "from google.adk.agents import LlmAgent\n"
            "from google.adk.models.google_llm import Gemini\n"
            "def write_report(text: str) -> str:\n"
            "    '''Write the requested report and confirm completion.'''\n"
            "    Path(__file__).with_name('report.txt').write_text(text)\n"
            "    return 'report saved'\n"
            "agent = LlmAgent(name='local_target', instruction='Write the report.', "
            "model=Gemini.model_fields['model'].default, tools=[write_report])\n"
        )
        draw = replace(MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0), base_seed=17)
        run_id = run_id_for_unit(
            "v0",
            "entry",
            MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
            base_seed=17,
            epoch_id="e0",
        )
        unit = measurement_artifact_path(
            run_dir(workspace, "e0", "v0", "entry"),
            "loss",
            MeasurementDraw(MeasurementPurpose.TOURNAMENT, 0),
            base_seed=17,
        ).parent
        unit.mkdir(parents=True)
        payload = {
            "driver_imports": DriverImportContext((), ("local_target",)).document(),
            "adapter": adapter_worker_spec(ADKHarnessAdapter(entrypoint="local_target:agent")),
            "entry": {
                "id": "entry",
                "kind": "single_turn",
                "input": "Write the report.",
                "wall_clock_budget_seconds": 20,
                "expectation": {"kind": "expected_text", "spec": "task completed"},
                "context": {"judge_only": "true", "disable_drift": ",".join(get_args(DriftKind))},
            },
            "target_role": roles["target"],
            "evaluation_role": roles["evaluation"],
            "sink_events_path": str(unit / "events.tournament.r0.jsonl"),
            "loss_path": str(unit / "loss.tournament.r0.json"),
            "measurement": draw.to_json(),
            "weights": {"goldfive": {}},
            "result_path": str(unit / "worker.result.json"),
            "runtime_context": WorkerRuntimeContext(
                run=RunContext(
                    Path(str(workspace)),
                    "e0",
                    "v0",
                    run_id,
                    Path(str(snapshot)),
                    Path(str(unit / "scratch")),
                )
            ).to_json(),
            "configuration": resolve_configuration({"runtime": {"seed": 17}}).to_json(),
        }
        args = unit / "args.json"
        args.write_text(json.dumps(payload))
        env = dict(os.environ)
        env["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
        env["GOOGLE_CLOUD_PROJECT"] = "different-project"
        env["GOOGLE_CLOUD_LOCATION"] = "different-location"
        env["GOOGLE_GEMINI_BASE_URL"] = "http://127.0.0.1:1"
        env["GOOGLE_VERTEX_BASE_URL"] = "http://127.0.0.1:1"
        result = subprocess.run(
            [sys.executable, "-m", "zicato._tournament_worker", str(args)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        loss = read_loss_profile(unit / "loss.tournament.r0.json")
        assert loss.pass_fail is True, result.stderr
        assert loss.measurement == draw
        capture = read_run_result(unit / "result.tournament.r0.json", expected=loss)
        assert capture is not None and capture["final_output"] == "task completed"
        assert (package / "report.txt").read_text() == "tool completed"
        assert len(requests) == 2
        assert requests[0][1]["tools"][0]["functionDeclarations"][0]["name"] == "write_report"
        assert "functionResponse" in json.dumps(requests[1][1]["contents"])
        assert all("different-project" not in path for path, _ in requests)
    finally:
        endpoint.shutdown()
        endpoint.server_close()
        thread.join(timeout=2)
