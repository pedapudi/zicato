"""The optional boundary between Zicato contracts and Goldfive runtime config."""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
import os
import tomllib
from pathlib import Path

import pytest

from zicato.core import ScoringWeights
from zicato.epoch.contract import scoring_contract_to_canon, scoring_to_canon
from zicato.integrations.goldfive import (
    GOLDFIVE_IMPLEMENTATION_VERSION,
    GOLDFIVE_REVISION,
    build_runtime_config,
    installed_goldfive_implementation_version,
    normalize_config,
    secret_env_names,
)
from zicato.models_config import ModelsConfig
from zicato.tournament.worker_transport import scrubbed_worker_env


def _canonical_json(config: dict[str, object]) -> str:
    return json.dumps(
        scoring_contract_to_canon(ScoringWeights(goldfive=config)),
        sort_keys=True,
    )


def test_generic_scoring_omits_goldfive_configuration() -> None:
    """A generic adapter carries no irrelevant Goldfive contract block."""
    weights = ScoringWeights(telemetry_dialect="transcript")
    assert weights.goldfive is None
    assert "goldfive" not in weights.to_json()
    assert "goldfive" not in scoring_to_canon(weights)
    assert ScoringWeights.from_json(weights.to_json()) == weights


def test_active_goldfive_document_is_canonical_and_contract_bound() -> None:
    weights = ScoringWeights(goldfive={"steering": {"threshold": "critical"}})

    assert weights.to_json()["goldfive"] == {"steering": {"threshold": "critical"}}
    assert ScoringWeights.from_json(weights.to_json()) == weights
    assert scoring_contract_to_canon(weights)["goldfive"] == {
        **normalize_config({"steering": {"threshold": "critical"}}),
        "implementation_identity": {
            "goldfive_version": GOLDFIVE_IMPLEMENTATION_VERSION,
            "zicato_integration_revision": 1,
        },
    }


def test_goldfive_implementation_identity_is_conditional_system_metadata() -> None:
    generic = scoring_contract_to_canon(ScoringWeights())
    configured = scoring_contract_to_canon(ScoringWeights(goldfive={}))

    assert "goldfive" not in generic
    assert (
        configured["goldfive"]["implementation_identity"]["goldfive_version"]
        == GOLDFIVE_IMPLEMENTATION_VERSION
    )
    assert "implementation_identity" not in ScoringWeights(goldfive={}).to_json()["goldfive"]


def test_goldfive_document_is_frozen_after_contract_load() -> None:
    source = {"steering": {"threshold": "warning"}}
    weights = ScoringWeights(goldfive=source)
    before = scoring_contract_to_canon(weights)

    source["steering"]["threshold"] = "critical"  # type: ignore[index]
    with pytest.raises(TypeError):
        weights.goldfive["steering"]["threshold"] = "critical"  # type: ignore[index,union-attr]
    assert scoring_contract_to_canon(weights) == before


def test_frozen_list_values_cross_the_goldfive_document_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGE_CREDENTIAL", "judge-secret")
    weights = ScoringWeights(
        goldfive={
            "steering": {"context_editor_rules": ["prune_stale_steer"]},
            "judge": {
                "base_url": "http://judge.example",
                "api_key_env": "JUDGE_CREDENTIAL",
            },
        }
    )
    assert weights.goldfive is not None
    assert weights.goldfive["steering"]["context_editor_rules"] == ("prune_stale_steer",)

    normalized = normalize_config(weights.goldfive)
    assert normalized["steering"]["context_editor_rules"] == ["prune_stale_steer"]
    assert secret_env_names(weights.goldfive) == ("JUDGE_CREDENTIAL",)
    runtime = build_runtime_config(weights.goldfive)
    assert runtime.steering.context_editor_rules == ["prune_stale_steer"]
    assert runtime.judge.api_key == "judge-secret"


@pytest.mark.parametrize("config", [{1: "value"}, {"value": object()}])
def test_goldfive_contract_accepts_only_json_shaped_values(config: object) -> None:
    with pytest.raises(ValueError, match="goldfive"):
        ScoringWeights(goldfive=config)  # type: ignore[arg-type]


def test_goldfive_validation_is_owned_by_the_upstream_document_api() -> None:
    with pytest.raises(ValueError, match="unknown"):
        normalize_config({"tool_loops": {"windwo": 12}})


def test_runtime_uses_zicato_timeout_overlay_and_operator_override() -> None:
    defaults = build_runtime_config({})
    configured = build_runtime_config({"agent": {"call_timeout_ms": 456_000}})

    assert defaults.agent.call_timeout_ms == 1_800_000
    assert configured.agent.call_timeout_ms == 456_000


def test_ambient_goldfive_environment_cannot_change_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean = dataclasses.asdict(build_runtime_config({}))
    monkeypatch.setenv("GOLDFIVE_AGENT_CALL_TIMEOUT_MS", "1")
    monkeypatch.setenv("GOLDFIVE_EMBEDDING_BASE_URL", "http://ambient.invalid")
    monkeypatch.setenv("GOLDFIVE_JUDGE_MODEL", "ambient-judge")

    assert dataclasses.asdict(build_runtime_config({})) == clean


def test_goldfive_runtime_requires_contract_bound_settings() -> None:
    with pytest.raises(ValueError, match=r'add "goldfive": \{\} to scoring.json'):
        build_runtime_config(None)


def test_goldfive_runtime_verifies_the_installed_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zicato.integrations.goldfive as integration

    monkeypatch.setattr(
        integration,
        "installed_goldfive_implementation_version",
        lambda: "git:" + "0" * 40,
    )
    with pytest.raises(ValueError, match="install the pinned VCS dependency"):
        build_runtime_config({})


def test_worker_exposes_goldfive_only_to_a_capability_declaring_adapter() -> None:
    from zicato._tournament_worker import _goldfive_config_for_adapter

    weights = ScoringWeights(goldfive={"fail_fast_on_revision_rejection": True})
    assert _goldfive_config_for_adapter(weights, {"kind": "import"}) is None
    assert _goldfive_config_for_adapter(
        weights,
        {"kind": "import", "integrations": ["goldfive"]},
    ) == {"fail_fast_on_revision_rejection": True}


def test_secret_values_resolve_only_into_the_worker_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_CREDENTIAL", "embedding-secret-value")
    monkeypatch.setenv("JUDGE_CREDENTIAL", "judge-secret-value")
    config: dict[str, object] = {
        "embedding": {
            "base_url": "http://embedding.example",
            "api_key_env": "EMBEDDING_CREDENTIAL",
        },
        "judge": {
            "base_url": "http://judge.example",
            "api_key_env": "JUDGE_CREDENTIAL",
        },
    }

    serialized = json.dumps(ScoringWeights(goldfive=config).to_json(), sort_keys=True)
    canonical = _canonical_json(config)
    assert secret_env_names(config) == ("EMBEDDING_CREDENTIAL", "JUDGE_CREDENTIAL")
    assert "EMBEDDING_CREDENTIAL" in serialized
    assert "JUDGE_CREDENTIAL" in canonical
    assert "embedding-secret-value" not in serialized + canonical
    assert "judge-secret-value" not in serialized + canonical

    runtime = build_runtime_config(config)
    assert runtime.embedding.api_key == "embedding-secret-value"
    assert runtime.judge.api_key == "judge-secret-value"


def test_secret_values_do_not_change_contract_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config: dict[str, object] = {
        "judge": {
            "base_url": "http://judge.example",
            "api_key_env": "JUDGE_CREDENTIAL",
        }
    }
    monkeypatch.setenv("JUDGE_CREDENTIAL", "first-secret")
    first = _canonical_json(config)
    monkeypatch.setenv("JUDGE_CREDENTIAL", "second-secret")
    assert _canonical_json(config) == first


def test_scrubbed_worker_retains_only_declared_goldfive_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config: dict[str, object] = {
        "judge": {
            "base_url": "http://judge.example",
            "api_key_env": "JUDGE_CREDENTIAL",
        }
    }
    full_env = {
        "PATH": os.environ.get("PATH", ""),
        "JUDGE_CREDENTIAL": "judge-secret",
        "UNRELATED_CREDENTIAL": "must-be-removed",
        "GOLDFIVE_JUDGE_MODEL": "ambient-model",
    }
    scrubbed = scrubbed_worker_env(
        models=ModelsConfig(),
        secret_env_keys=secret_env_names(config),
        base_env=full_env,
    )

    assert scrubbed["JUDGE_CREDENTIAL"] == "judge-secret"
    assert "UNRELATED_CREDENTIAL" not in scrubbed
    assert "GOLDFIVE_JUDGE_MODEL" not in scrubbed
    with monkeypatch.context() as scrubbed_context:
        scrubbed_context.setattr(os, "environ", scrubbed)
        assert build_runtime_config(config).judge.api_key == "judge-secret"


def test_missing_configured_secret_names_reference_without_exposing_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_JUDGE_CREDENTIAL", raising=False)
    config: dict[str, object] = {
        "judge": {
            "base_url": "http://judge.example",
            "api_key_env": "MISSING_JUDGE_CREDENTIAL",
        }
    }

    with pytest.raises(ValueError, match="MISSING_JUDGE_CREDENTIAL"):
        build_runtime_config(config)


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://example.test",
        "https://user:secret@example.test",
        "https://example.test?token=secret",
        "https://example.test#secret",
    ],
)
def test_endpoint_urls_cannot_embed_credentials_or_unscoped_metadata(
    base_url: str,
) -> None:
    with pytest.raises(ValueError) as raised:
        normalize_config({"judge": {"base_url": base_url}})
    assert "secret" not in str(raised.value)


def test_goldfive_implementation_matches_every_dependency_pin() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    suffix = f"git+https://github.com/pedapudi/goldfive.git@{GOLDFIVE_REVISION}"
    for extra in ("goldfive", "goldfive-remote", "goldfive-local-embedding", "adk", "all"):
        requirements = [
            requirement
            for requirement in pyproject["project"]["optional-dependencies"][extra]
            if requirement.startswith("goldfive")
        ]
        assert len(requirements) == 1, extra
        assert requirements[0].endswith(suffix), extra
    assert pyproject["tool"]["uv"]["override-dependencies"] == [f"goldfive @ {suffix}"]
    assert installed_goldfive_implementation_version() == GOLDFIVE_IMPLEMENTATION_VERSION


def test_goldfive_distribution_without_vcs_identity_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package version cannot stand in for the commit frozen by Zicato."""
    import zicato.integrations.goldfive as integration

    class RegistryDistribution:
        version = "0.1.0"

        @staticmethod
        def read_text(filename: str) -> str | None:
            assert filename == "direct_url.json"
            return None

    monkeypatch.setattr(importlib.metadata, "distribution", lambda _name: RegistryDistribution())
    assert integration.installed_goldfive_implementation_version() is None
    with pytest.raises(ValueError, match="pinned VCS dependency"):
        integration.build_runtime_config({})
