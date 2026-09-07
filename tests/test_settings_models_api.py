"""The dashboard shows configured models without exposing secrets or accepting edits."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.models_config import PUBLIC_MODEL_ROLES
from zicato.workspace.config_io import write_workspace_config

_SECRET = "sk-leak-canary-value"
_ENV_NAME = "ZICATO_SETTINGS_TEST_KEY"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    write_workspace_config(
        ws,
        {
            "instance_id": "default",
            "models": {
                "engines": {
                    "target": {"call_llm": "pkg.harness:fn"},
                    "evaluation": {
                        "model": "house-x",
                        "endpoint": None,
                        "api_key_env": _ENV_NAME,
                    },
                },
                "roles": {},
            },
        },
    )
    return ws


@pytest.fixture()
def client(workspace: Path, tmp_path: Path) -> TestClient:
    static = tmp_path / "static"
    static.mkdir()
    app = create_app(workspace, static, read_only=False)
    return TestClient(app)


def test_get_returns_secret_safe_view_with_set_flag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV_NAME, _SECRET)
    resp = client.get("/settings/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rolls_epoch"] is False
    models = body["models"]
    # EVERY role is present (even the unconfigured ones) — including the two
    # WS-ENS proposer-ensemble roles.
    assert set(body["roles"]) == set(PUBLIC_MODEL_ROLES)
    assert models["engines"]["target"]["call_llm"] == "pkg.harness:fn"
    # The model-spec role carries the NAME + a set flag, NEVER the value.
    assert models["engines"]["evaluation"]["api_key_env"] == _ENV_NAME
    assert models["engines"]["evaluation"]["api_key_env_set"] is True
    assert _SECRET not in resp.text


def test_get_set_flag_false_when_env_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ENV_NAME, raising=False)
    body = client.get("/settings/models").json()
    assert body["models"]["engines"]["evaluation"]["api_key_env_set"] is False


@pytest.mark.parametrize(
    "path", ["/settings/models", "/builder/op", "/builder/apply", "/builder/chat"]
)
def test_dashboard_configuration_cannot_be_written(client, workspace, path):
    config = workspace / "config.json"
    before = config.read_bytes()
    response = client.post(path, json={"models": {"engines": {}, "roles": {}}})
    assert response.status_code in {404, 405}
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "path", ["/builder/config", "/builder/draft", "/api/proposer/recommendations"]
)
def test_dashboard_has_no_editor_or_proposer_recommendation_endpoint(client, path):
    assert client.get(path).status_code == 404
