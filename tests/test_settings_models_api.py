"""Tests for the ``/settings/models`` REST surface (the unified models config).

Covers the secret-safe GET (api_key_env NAME + a set/unset flag, never the
value), the POST round-trip (persisted into ``config.json`` ``models`` block,
NAMES only), and the read-only 403. The api_key_env value is asserted to never
appear in any response body.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.models_config import MODEL_ROLES
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
                "harness": {"call_llm": "pkg.harness:fn"},
                "auxiliary": {
                    "model": "house-x",
                    "endpoint": None,
                    "api_key_env": _ENV_NAME,
                },
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
    assert set(models.keys()) == set(MODEL_ROLES)
    assert {"proposer_breadth", "proposer_depth"} <= set(models.keys())
    assert models["harness"]["call_llm"] == "pkg.harness:fn"
    # The model-spec role carries the NAME + a set flag, NEVER the value.
    assert models["auxiliary"]["api_key_env"] == _ENV_NAME
    assert models["auxiliary"]["api_key_env_set"] is True
    assert _SECRET not in resp.text


def test_get_set_flag_false_when_env_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ENV_NAME, raising=False)
    body = client.get("/settings/models").json()
    assert body["models"]["auxiliary"]["api_key_env_set"] is False


def test_post_persists_models_block_names_only(client: TestClient, workspace: Path) -> None:
    payload = {
        "models": {
            "harness": {"call_llm": "pkg.harness:fn"},
            "judge": {"model": "judge-x", "endpoint": None, "api_key_env": _ENV_NAME},
        }
    }
    resp = client.post("/settings/models", json=payload)
    assert resp.status_code == 200
    # The on-disk config.json now carries the models block — NAMES only.
    on_disk = json.loads((workspace / "config.json").read_text(encoding="utf-8"))
    assert on_disk["models"]["judge"]["model"] == "judge-x"
    assert on_disk["models"]["judge"]["api_key_env"] == _ENV_NAME
    assert _SECRET not in json.dumps(on_disk)
    # The echoed view is secret-safe + flags the epoch is NOT rolled.
    body = resp.json()
    assert body["rolls_epoch"] is False
    assert "api_key_env_set" in body["models"]["judge"]


def test_post_round_trips_proposer_ensemble_roles(client: TestClient, workspace: Path) -> None:
    """The WS-ENS proposer_breadth / proposer_depth roles persist + echo like
    any other role (a call_llm path and a model spec, NAMES only)."""
    payload = {
        "models": {
            "proposer_breadth": {"call_llm": "pkg.breadth:fn"},
            "proposer_depth": {"model": "depth-x", "endpoint": None, "api_key_env": _ENV_NAME},
        }
    }
    resp = client.post("/settings/models", json=payload)
    assert resp.status_code == 200
    on_disk = json.loads((workspace / "config.json").read_text(encoding="utf-8"))
    assert on_disk["models"]["proposer_breadth"]["call_llm"] == "pkg.breadth:fn"
    assert on_disk["models"]["proposer_depth"]["model"] == "depth-x"
    assert on_disk["models"]["proposer_depth"]["api_key_env"] == _ENV_NAME
    assert _SECRET not in json.dumps(on_disk)
    # The echoed secret-safe view carries both roles.
    echoed = resp.json()["models"]
    assert echoed["proposer_breadth"]["call_llm"] == "pkg.breadth:fn"
    assert "api_key_env_set" in echoed["proposer_depth"]


def test_post_preserves_other_config_keys(client: TestClient, workspace: Path) -> None:
    """Writing the models block leaves every other config.json key intact."""
    client.post("/settings/models", json={"models": {"harness": {"call_llm": "pkg:fn"}}})
    on_disk = json.loads((workspace / "config.json").read_text(encoding="utf-8"))
    assert on_disk["instance_id"] == "default"


def test_post_empty_models_drops_the_block(client: TestClient, workspace: Path) -> None:
    """All-unconfigured roles ⇒ the models block is removed (reads back default)."""
    resp = client.post("/settings/models", json={"models": {}})
    assert resp.status_code == 200
    on_disk = json.loads((workspace / "config.json").read_text(encoding="utf-8"))
    assert "models" not in on_disk


def test_post_missing_models_object_is_400(client: TestClient) -> None:
    resp = client.post("/settings/models", json={"nope": 1})
    assert resp.status_code == 400


def test_post_is_403_in_read_only(workspace: Path, tmp_path: Path) -> None:
    static = tmp_path / "static-ro"
    static.mkdir()
    app = create_app(workspace, static, read_only=True)
    ro = TestClient(app)
    resp = ro.post("/settings/models", json={"models": {}})
    assert resp.status_code == 403
