"""Worker environment-scrub composition — the producer side of containment.

A scrubbed worker is spawned with a MINIMAL explicit environment instead of
inheriting the orchestrator's full env, so a mutated worker cannot read every
credential in the process env. These tests pin the composition of that env:
the essential floor, the configured roles' ``api_key_env`` names, and the
operator passthrough — and that an absent key is never invented.
"""

from __future__ import annotations

from zicato.models_config import ModelsConfig, RoleSpec
from zicato.tournament.worker_transport import (
    _WORKER_ESSENTIAL_ENV_KEYS,
    _api_key_env_names,
    scrubbed_worker_env,
)


def test_api_key_env_names_empty_for_unconfigured_models() -> None:
    """An all-default ModelsConfig contributes no api_key_env names."""
    assert _api_key_env_names(ModelsConfig()) == []


def test_api_key_env_names_collects_configured_roles() -> None:
    """Each model-spec role's api_key_env NAME is collected, de-duplicated."""
    models = ModelsConfig(
        harness=RoleSpec(model="m1", api_key_env="HARNESS_KEY"),
        auxiliary=RoleSpec(model="m2", api_key_env="AUX_KEY"),
        # A duplicate name across roles must appear once.
        judge=RoleSpec(model="m3", api_key_env="AUX_KEY"),
    )
    names = _api_key_env_names(models)
    assert names == ["HARNESS_KEY", "AUX_KEY"]


def test_api_key_env_names_ignores_dotted_path_roles() -> None:
    """A dotted-path (call_llm) role carries no api_key_env and is skipped."""
    models = ModelsConfig(
        harness=RoleSpec(call_llm="pkg.mod:fn"),
        auxiliary=RoleSpec(model="m", api_key_env="ONLY_KEY"),
    )
    assert _api_key_env_names(models) == ["ONLY_KEY"]


def test_scrubbed_env_keeps_present_essentials_omits_absent() -> None:
    """Essential keys are copied only when present in the source env."""
    base = {"PATH": "/usr/bin", "HOME": "/home/u", "RANDOM_SECRET": "shh"}
    env = scrubbed_worker_env(models=ModelsConfig(), base_env=base)
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/u"
    # An essential key absent from the source is not invented.
    assert "TMPDIR" not in env
    # A non-essential, non-allowlisted variable is dropped entirely — this is
    # the whole point of the scrub.
    assert "RANDOM_SECRET" not in env


def test_scrubbed_env_includes_configured_api_key_env() -> None:
    """The api_key_env a configured role needs survives the scrub."""
    base = {
        "PATH": "/usr/bin",
        "HARNESS_KEY": "sk-secret",
        "UNRELATED_KEY": "nope",
    }
    models = ModelsConfig(harness=RoleSpec(model="m", api_key_env="HARNESS_KEY"))
    env = scrubbed_worker_env(models=models, base_env=base)
    assert env["HARNESS_KEY"] == "sk-secret"
    # A credential NOT named by any role is excluded.
    assert "UNRELATED_KEY" not in env


def test_scrubbed_env_passthrough_keys() -> None:
    """Operator-named passthrough keys are forwarded when present."""
    base = {"PATH": "/usr/bin", "CUSTOM_TARGET_VAR": "v", "OTHER": "x"}
    env = scrubbed_worker_env(
        models=ModelsConfig(),
        extra_env_keys=("CUSTOM_TARGET_VAR",),
        base_env=base,
    )
    assert env["CUSTOM_TARGET_VAR"] == "v"
    assert "OTHER" not in env


def test_scrubbed_env_does_not_invent_missing_passthrough() -> None:
    """A passthrough key absent from the source is silently omitted."""
    env = scrubbed_worker_env(
        models=ModelsConfig(),
        extra_env_keys=("DOES_NOT_EXIST",),
        base_env={"PATH": "/usr/bin"},
    )
    assert "DOES_NOT_EXIST" not in env


def test_scrubbed_env_is_a_fresh_dict() -> None:
    """The returned env is a fresh mapping, not the source object."""
    base = {"PATH": "/usr/bin"}
    env = scrubbed_worker_env(models=ModelsConfig(), base_env=base)
    assert env is not base
    env["PATH"] = "/mutated"
    assert base["PATH"] == "/usr/bin"


def test_essential_keys_are_minimal_and_credential_free() -> None:
    """The essential floor names no provider credential variable.

    A regression guard: the essential list must stay a process floor (PATH,
    HOME, temp, locale, interpreter bootstrap) and never start smuggling a
    credential variable through, which would defeat the scrub.
    """
    joined = " ".join(_WORKER_ESSENTIAL_ENV_KEYS).upper()
    for forbidden in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"):
        assert forbidden not in joined
