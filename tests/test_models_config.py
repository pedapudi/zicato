"""Tests for :mod:`zicato.models_config` — the unified per-role models config.

Covers: parse/serialize both role forms (dotted-path + model-spec), the
absent-block default, the secret-safe public view (api_key_env NAME + a
set/unset flag, never the value), and the resolver (dotted-path import +
model-spec → text call_llm with the ADK/goldfive layer mocked). The
secret-safety property is asserted EXPLICITLY: the api_key_env value is read
from os.environ only at resolve time and never appears in any serialized /
returned dict.
"""

from __future__ import annotations

import sys
import types

import pytest

from zicato.models_config import (
    MODEL_ROLES,
    ModelsConfig,
    RoleSpec,
    models_config_from_dict,
    resolve_text_call_llm,
    role_spec_from_dict,
)

_SECRET = "sk-super-secret-value-do-not-leak"
_ENV_NAME = "ZICATO_TEST_API_KEY"


# ---------------------------------------------------------------------------
# Parse / serialize
# ---------------------------------------------------------------------------


def test_absent_models_block_is_all_default() -> None:
    """A ``None`` / non-mapping ``models`` block ⇒ every role empty."""
    cfg = models_config_from_dict(None)
    assert isinstance(cfg, ModelsConfig)
    for role in MODEL_ROLES:
        assert cfg.role(role).is_empty
    # And it serializes to an empty block (reads back as all-default).
    assert cfg.to_dict() == {}


def test_call_llm_form_roundtrips() -> None:
    spec = role_spec_from_dict({"call_llm": "pkg.mod:fn"})
    assert spec.uses_call_llm
    assert spec.call_llm == "pkg.mod:fn"
    # Only the call_llm key is emitted (no stray model-spec keys).
    assert spec.to_dict() == {"call_llm": "pkg.mod:fn"}


def test_model_spec_form_roundtrips() -> None:
    spec = role_spec_from_dict(
        {"model": "house-x", "endpoint": "https://e.example", "api_key_env": _ENV_NAME}
    )
    assert not spec.uses_call_llm
    assert spec.model == "house-x"
    assert spec.endpoint == "https://e.example"
    assert spec.api_key_env == _ENV_NAME
    assert spec.to_dict() == {
        "model": "house-x",
        "endpoint": "https://e.example",
        "api_key_env": _ENV_NAME,
    }


def test_call_llm_wins_when_both_keys_present() -> None:
    """A ``call_llm`` key takes precedence over a model-spec on the same role."""
    spec = role_spec_from_dict({"call_llm": "pkg:fn", "model": "house-x"})
    assert spec.uses_call_llm
    assert spec.model is None


def test_models_config_roundtrips_all_roles() -> None:
    raw = {
        "harness": {"call_llm": "pkg:harness"},
        "auxiliary": {"model": "aux-x", "endpoint": None, "api_key_env": None},
        "builder": {"model": "build-x"},
        "judge": {"call_llm": "pkg:judge"},
    }
    cfg = models_config_from_dict(raw)
    out = cfg.to_dict()
    assert out["harness"] == {"call_llm": "pkg:harness"}
    assert out["auxiliary"] == {"model": "aux-x", "endpoint": None, "api_key_env": None}
    assert out["builder"] == {"model": "build-x", "endpoint": None, "api_key_env": None}
    assert out["judge"] == {"call_llm": "pkg:judge"}
    # Re-parsing the serialized form is a fixpoint.
    assert models_config_from_dict(out).to_dict() == out


def test_unknown_role_lookup_raises() -> None:
    with pytest.raises(ValueError, match="unknown model role"):
        ModelsConfig().role("nonsense")


# ---------------------------------------------------------------------------
# Secret safety
# ---------------------------------------------------------------------------


def test_public_dict_never_contains_the_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public view carries the env-var NAME + a set flag, NEVER the value."""
    monkeypatch.setenv(_ENV_NAME, _SECRET)
    spec = RoleSpec(model="house-x", api_key_env=_ENV_NAME)
    pub = spec.to_public_dict()
    assert pub["api_key_env"] == _ENV_NAME
    assert pub["api_key_env_set"] is True
    # The secret value appears NOWHERE in the serialized public view.
    assert _SECRET not in repr(pub)
    assert "api_key" not in pub  # only api_key_env (the NAME) is ever present


def test_public_dict_set_flag_false_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_ENV_NAME, raising=False)
    spec = RoleSpec(model="house-x", api_key_env=_ENV_NAME)
    pub = spec.to_public_dict()
    assert pub["api_key_env_set"] is False


def test_to_dict_never_reads_or_emits_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The on-disk serialization carries only the NAME, never the value."""
    monkeypatch.setenv(_ENV_NAME, _SECRET)
    spec = RoleSpec(model="house-x", api_key_env=_ENV_NAME)
    d = spec.to_dict()
    assert d["api_key_env"] == _ENV_NAME
    assert _SECRET not in repr(d)


def test_models_public_dict_emits_every_role() -> None:
    """The public view always emits all four roles (even unconfigured ones)."""
    pub = ModelsConfig().to_public_dict()
    assert set(pub.keys()) == set(MODEL_ROLES)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


async def _a_call_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return "ok"


def test_resolve_dotted_path_imports_the_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = types.ModuleType("fake_mc_mod")
    mod.fn = _a_call_llm  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_mc_mod", mod)
    resolved = resolve_text_call_llm(RoleSpec(call_llm="fake_mc_mod:fn"), role="harness")
    assert resolved is _a_call_llm


def test_resolve_dotted_path_rejects_non_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = types.ModuleType("fake_mc_noncallable")
    mod.value = 7  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_mc_noncallable", mod)
    with pytest.raises(ValueError, match="expected a callable"):
        resolve_text_call_llm(RoleSpec(call_llm="fake_mc_noncallable:value"), role="harness")


def test_resolve_empty_spec_raises() -> None:
    with pytest.raises(ValueError, match="neither a call_llm"):
        resolve_text_call_llm(RoleSpec(), role="judge")


def test_resolve_model_spec_builds_text_call_llm_and_reads_secret_at_resolve_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model-spec resolves through the ADK/goldfive layer (mocked).

    Asserts the secret is read from os.environ ONLY at resolve time and is
    threaded into the ADK model builder — never stored on the spec or
    surfaced in any returned/serialized dict.
    """
    monkeypatch.setenv(_ENV_NAME, _SECRET)

    seen: dict[str, object] = {}

    # Mock the LiteLlm constructor (the ADK layer) so the test needs no extra.
    lite_mod = types.ModuleType("google.adk.models.lite_llm")

    class _FakeLiteLlm:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    lite_mod.LiteLlm = _FakeLiteLlm  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.adk.models.lite_llm", lite_mod)

    # Mock goldfive's ADK detector so model → text call_llm needs no real ADK.
    detect_mod = types.ModuleType("goldfive._llm_detect")
    built: dict[str, object] = {}

    def _make_default_adk_call_llm(model: object):  # type: ignore[no-untyped-def]
        built["model"] = model
        return _a_call_llm

    detect_mod.make_default_adk_call_llm = _make_default_adk_call_llm  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "goldfive._llm_detect", detect_mod)

    spec = RoleSpec(model="house-x", endpoint="https://e.example", api_key_env=_ENV_NAME)
    resolved = resolve_text_call_llm(spec, role="auxiliary")
    assert resolved is _a_call_llm

    # The secret was read from os.environ at resolve time and handed to LiteLlm
    # (api_key) — but it lives only inside the model object, never on the spec.
    assert seen["api_key"] == _SECRET
    assert seen["model"] == "house-x"
    assert seen["api_base"] == "https://e.example"
    assert built["model"] is not None
    # Secret-safety: the spec and its serializations still carry only the NAME.
    assert _SECRET not in repr(spec)
    assert _SECRET not in repr(spec.to_dict())
    assert _SECRET not in repr(spec.to_public_dict())


def test_resolve_model_spec_without_endpoint_or_key_returns_bare_model() -> None:
    """A bare model string (no endpoint / key) is returned as-is for goldfive."""
    captured: dict[str, object] = {}
    detect_mod = types.ModuleType("goldfive._llm_detect")

    def _make(model: object):  # type: ignore[no-untyped-def]
        captured["model"] = model
        return _a_call_llm

    detect_mod.make_default_adk_call_llm = _make  # type: ignore[attr-defined]
    sys.modules["goldfive._llm_detect"] = detect_mod
    try:
        resolved = resolve_text_call_llm(RoleSpec(model="bare-x"), role="judge")
        assert resolved is _a_call_llm
        # No LiteLlm path ⇒ the bare model string was handed straight through.
        assert captured["model"] == "bare-x"
    finally:
        del sys.modules["goldfive._llm_detect"]
