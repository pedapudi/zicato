"""Tests for :mod:`zicato.config` — the typed configuration tree.

:func:`zicato.config.load_config` is the *single* place zicato reads the
environment. These tests cover the things that matter: the dataclass
defaults, the env-var parsing (including invalid-value clamping and
type coercion), the precedence layering
(defaults < env < pinned CLI flags < explicit overrides), and the
loud-ignore of every deleted env binding.

Every test passes an explicit ``env`` dict so the suite is fully
isolated from the real process environment — no ``monkeypatch`` of
``os.environ`` required.
"""

from __future__ import annotations

import dataclasses

import pytest

from zicato.config import (
    AuxConfig,
    DashboardConfig,
    HealthConfig,
    IntegrationConfig,
    RuntimeTuningConfig,
    ZicatoConfig,
    clear_pinned_overrides,
    describe_env_vars,
    get_pinned_overrides,
    health_config_from_workspace,
    load_config,
    pin_overrides,
    pinned_override,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_empty_env_yields_all_defaults() -> None:
    """An empty environment with no overrides yields the dataclass defaults."""
    cfg = load_config()
    assert cfg == ZicatoConfig()
    assert cfg.health.scoring_window == 3
    assert cfg.health.scoring_epsilon == 1e-6
    assert cfg.health.no_expectations_fraction == 0.5
    assert cfg.health.stalled_rejects == 3
    assert cfg.aux.call_timeout_s == 120.0
    assert cfg.integration.harmonograf_url == ""
    assert cfg.integration.supervisor_binary == ""
    assert cfg.dashboard.static_dir == ""
    assert cfg.runtime.parallelism == 4


def test_config_tree_is_frozen() -> None:
    """Every config dataclass is frozen — values cannot be mutated in place."""
    cfg = load_config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.health.scoring_window = 9  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.health = HealthConfig()  # type: ignore[misc]


def test_programmatic_construction_works() -> None:
    """``ZicatoConfig(health=HealthConfig(...))`` is constructible directly."""
    cfg = ZicatoConfig(health=HealthConfig(scoring_window=11))
    assert cfg.health.scoring_window == 11
    # Untouched sub-configs keep their defaults.
    assert cfg.aux.call_timeout_s == 120.0
    assert cfg.runtime.parallelism == 4


# ---------------------------------------------------------------------------
# The workspace config.json 'health' block -> HealthConfig
# ---------------------------------------------------------------------------


def test_health_block_round_trips_every_field() -> None:
    """Every ``health`` key lands on the matching :class:`HealthConfig` field."""
    cfg = health_config_from_workspace(
        {
            "health": {
                "scoring_window": 8,
                "scoring_epsilon": 0.25,
                "no_expectations_fraction": 0.75,
                "stalled_rejects": 6,
                "generalization_gap_warn": 0.02,
                "generalization_gap_crit": 0.4,
            }
        }
    )
    assert cfg == HealthConfig(
        scoring_window=8,
        scoring_epsilon=0.25,
        no_expectations_fraction=0.75,
        stalled_rejects=6,
        generalization_gap_warn=0.02,
        generalization_gap_crit=0.4,
    )


def test_health_block_absent_yields_defaults() -> None:
    """No config, or a config without a ``health`` block, means defaults."""
    assert health_config_from_workspace(None) == HealthConfig()
    assert health_config_from_workspace({}) == HealthConfig()
    assert health_config_from_workspace({"runtime": {}}) == HealthConfig()


def test_health_block_partial_keeps_other_defaults() -> None:
    """A partial block only overrides the named fields."""
    cfg = health_config_from_workspace({"health": {"scoring_window": 9}})
    assert cfg.scoring_window == 9
    assert cfg.stalled_rejects == 3
    assert cfg.scoring_epsilon == 1e-6


def test_health_block_clamps_invalid_values_to_defaults() -> None:
    """Out-of-range / unparseable values degrade to the field default.

    The same clamp the env bindings applied: a zero window or a negative
    epsilon would silently disable a detector, so it falls back instead.
    """
    cfg = health_config_from_workspace(
        {
            "health": {
                "scoring_window": 0,
                "stalled_rejects": -4,
                "scoring_epsilon": -1.0,
                "generalization_gap_warn": "not-a-number",
            }
        }
    )
    assert cfg.scoring_window == 3
    assert cfg.stalled_rejects == 3
    assert cfg.scoring_epsilon == 1e-6
    assert cfg.generalization_gap_warn == 0.05


def test_health_block_zero_is_valid_for_non_negative_float() -> None:
    """Zero is a legal value for a non-negative-float field (epsilon)."""
    cfg = health_config_from_workspace({"health": {"scoring_epsilon": 0}})
    assert cfg.scoring_epsilon == 0.0


def test_health_block_string_numbers_coerce() -> None:
    """A JSON-string number still coerces (same coercers as the env layer)."""
    cfg = health_config_from_workspace({"health": {"scoring_window": "5"}})
    assert cfg.scoring_window == 5
    assert isinstance(cfg.scoring_window, int)


def test_health_block_unknown_key_raises() -> None:
    """A typo'd key fails loudly, naming the valid fields."""
    with pytest.raises(KeyError, match="scoring_windw"):
        health_config_from_workspace({"health": {"scoring_windw": 5}})


def test_health_block_non_object_raises() -> None:
    """A ``health`` block that is not a JSON object fails loudly."""
    with pytest.raises(ValueError, match="'health' block"):
        health_config_from_workspace({"health": 5})


# ---------------------------------------------------------------------------
# Deleted bindings — env vars replaced by CLI flags are gone, not aliased
# ---------------------------------------------------------------------------

#: Every deleted env binding, with a plausible value. The redundant trio
#: was fully shadowed by pre-existing CLI flags; six more were converted
#: to flags (`zicato evolve --parallelism / --harness-call-timeout-ms /
#: --aux-call-timeout / --supervisor-binary / --harmonograf-url`,
#: `zicato dashboard|builder --static-dir`); the six ZICATO_HEALTH_*
#: thresholds moved to the workspace config.json 'health' block.
_DELETED_ENV_VARS: dict[str, str] = {
    "ZICATO_MAX_WALL_CLOCK_SECONDS": "900",
    "ZICATO_WORKSPACE": "/work/.zicato",
    "ZICATO_INSTANCE_ID": "instance-7",
    "ZICATO_AUX_CALL_TIMEOUT": "45.5",
    "ZICATO_PARALLELISM": "16",
    "ZICATO_HARNESS_CALL_TIMEOUT_MS": "600000",
    "ZICATO_SUPERVISOR_BINARY": "/opt/zicato-supervisor",
    "ZICATO_DASHBOARD_STATIC_DIR": "/srv/static",
    "ZICATO_HARMONOGRAF_URL": "http://localhost:9000",
    "ZICATO_HEALTH_SCORING_WINDOW": "8",
    "ZICATO_HEALTH_SCORING_EPSILON": "0.25",
    "ZICATO_HEALTH_NO_EXPECTATIONS_FRACTION": "0.75",
    "ZICATO_HEALTH_STALLED_REJECTS": "6",
    "ZICATO_HEALTH_GENERALIZATION_GAP_WARN": "0.02",
    "ZICATO_HEALTH_GENERALIZATION_GAP_CRIT": "0.4",
}


def test_deleted_env_vars_are_ignored_by_load_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every deleted binding is ignored entirely — no hidden alias survives.

    Setting all of them at once must leave the config tree at its
    defaults, and the tree no longer even carries the former ``budget``
    / ``workspace`` sections. (``ZICATO_HARMONOGRAF_URL`` survives only
    as the internal auto-launch handoff read by
    ``zicato.telemetry.sink`` — never as a ``load_config`` input.)
    """
    for name, value in _DELETED_ENV_VARS.items():
        monkeypatch.setenv(name, value)
    cfg = load_config()
    assert cfg == ZicatoConfig()
    assert not hasattr(cfg, "budget")
    assert not hasattr(cfg, "workspace")


def test_deleted_env_vars_absent_from_describe() -> None:
    """``describe_env_vars`` lists no deleted OPERATOR binding.

    The one deliberate exception: ``ZICATO_HARMONOGRAF_URL`` appears —
    but only in its surviving INTERNAL role (the auto-launch handoff),
    explicitly labelled as such, never as an operator knob.
    """
    by_name = {info.name: info for info in describe_env_vars()}
    for name in _DELETED_ENV_VARS:
        if name == "ZICATO_HARMONOGRAF_URL":
            assert by_name[name].role == "internal-handoff"
            continue
        assert name not in by_name


# ---------------------------------------------------------------------------
# The merited env-var set
# ---------------------------------------------------------------------------

_VALID_ROLES = {
    "harness-contract",
    "internal-handoff",
    "secrets-boundary",
    "external-integration",
    "test-toggle",
}


def test_describe_env_vars_is_the_labelled_merited_set() -> None:
    """Every kept variable carries a role label and a description."""
    infos = describe_env_vars()
    assert infos, "the merited set must not be empty"
    for info in infos:
        assert info.role in _VALID_ROLES, info
        assert info.description, info
    names = {info.name for info in infos}
    # The harness contract + the internal handoff pair + the goldfive
    # deferral + the CI/test toggles are all present.
    assert "ZICATO_RUN_SCRATCH_DIR" in names
    assert "ZICATO_HARMONOGRAF_URL" in names
    assert "ZICATO_HARMONOGRAF_GRPC" in names
    assert "GOLDFIVE_AGENT_CALL_TIMEOUT_MS" in names
    assert "ZICATO_SKIP_HOOK_CHECK" in names
    assert "ZICATO_PARITY_UPDATE" in names


def test_merited_set_names_match_the_code_constants() -> None:
    """The described names cannot drift from the constants the code uses."""
    from zicato.epoch.snapshot_scope import SCRATCH_DIR_ENV
    from zicato.telemetry.sink import HARMONOGRAF_GRPC_ENV, HARMONOGRAF_URL_ENV

    names = {info.name for info in describe_env_vars()}
    assert SCRATCH_DIR_ENV in names
    assert HARMONOGRAF_URL_ENV in names
    assert HARMONOGRAF_GRPC_ENV in names


def test_merited_set_harmonograf_is_internal_handoff() -> None:
    """The handoff pair is labelled internal — not an operator surface."""
    by_name = {info.name: info for info in describe_env_vars()}
    assert by_name["ZICATO_HARMONOGRAF_URL"].role == "internal-handoff"
    assert by_name["ZICATO_HARMONOGRAF_GRPC"].role == "internal-handoff"


# ---------------------------------------------------------------------------
# Precedence / override layering
# ---------------------------------------------------------------------------


def test_overrides_beat_the_defaults() -> None:
    """An explicit override wins over the dataclass default."""
    cfg = load_config(overrides={"aux": {"call_timeout_s": 5.0}})
    assert cfg.aux.call_timeout_s == 5.0


def test_override_leaves_other_fields_of_a_section_intact() -> None:
    """Overriding one field of a section preserves that section's other fields."""
    cfg = load_config(overrides={"health": {"scoring_window": 42}})
    assert cfg.health.scoring_window == 42  # from override
    assert cfg.health.scoring_epsilon == 1e-6  # default, untouched
    assert cfg.health.stalled_rejects == 3  # default, untouched


# ---------------------------------------------------------------------------
# Override validation
# ---------------------------------------------------------------------------


def test_unknown_override_section_raises() -> None:
    """An unknown section name in ``overrides`` raises rather than silently no-ops."""
    with pytest.raises(KeyError, match="unknown config section"):
        load_config(overrides={"nonsense": {"x": 1}})


def test_unknown_override_field_raises() -> None:
    """An unknown field name within a known section raises."""
    with pytest.raises(KeyError, match="unknown field"):
        load_config(overrides={"health": {"not_a_field": 1}})


def test_non_mapping_override_section_raises() -> None:
    """An override section whose value is not a mapping raises ``TypeError``."""
    with pytest.raises(TypeError, match="must be a mapping"):
        load_config(overrides={"health": 5})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# Deleted env vars are ignored through the REAL os.environ too
# ---------------------------------------------------------------------------


def test_deleted_env_vars_ignored_via_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted variable in the real process environment is a no-op."""
    monkeypatch.setenv("ZICATO_HEALTH_SCORING_WINDOW", "21")
    cfg = load_config()
    assert cfg.health.scoring_window == 3  # default — env var deleted


# ---------------------------------------------------------------------------
# Process-pinned overrides — the CLI-flag layer
# ---------------------------------------------------------------------------
#
# Pins are process-global; the suite-wide autouse fixture in conftest.py
# clears them around every test, so these tests only pin, never clean up.


def test_pinned_overrides_beat_the_defaults() -> None:
    """A pinned value beats the dataclass default."""
    pin_overrides({"health": {"scoring_window": 12}})
    cfg = load_config()
    assert cfg.health.scoring_window == 12


def test_pinned_overrides_reach_a_flagless_load_config() -> None:
    """The whole point: a deep call site's bare ``load_config()`` sees pins."""
    pin_overrides({"aux": {"call_timeout_s": 3.5}, "runtime": {"parallelism": 9}})
    cfg = load_config()
    assert cfg.aux.call_timeout_s == 3.5
    assert cfg.runtime.parallelism == 9


def test_explicit_overrides_beat_pinned_overrides() -> None:
    """An explicit ``overrides=`` mapping wins over the pinned layer."""
    pin_overrides({"aux": {"call_timeout_s": 3.5}})
    cfg = load_config(overrides={"aux": {"call_timeout_s": 99.0}})
    assert cfg.aux.call_timeout_s == 99.0


def test_pin_overrides_merges_field_by_field() -> None:
    """Repeated pins merge; the latest pin of a field wins."""
    pin_overrides({"health": {"scoring_window": 5}})
    pin_overrides({"health": {"stalled_rejects": 8}})
    pin_overrides({"health": {"scoring_window": 6}})
    cfg = load_config()
    assert cfg.health.scoring_window == 6
    assert cfg.health.stalled_rejects == 8


def test_pin_overrides_validates_eagerly() -> None:
    """An unknown section/field raises at the pin site and pins nothing."""
    with pytest.raises(KeyError, match="unknown config section"):
        pin_overrides({"nonsense": {"x": 1}})
    with pytest.raises(KeyError, match="unknown field"):
        pin_overrides({"health": {"not_a_field": 1}})
    assert get_pinned_overrides() == {}
    assert load_config() == ZicatoConfig()


def test_get_pinned_overrides_returns_a_detached_copy() -> None:
    """Mutating the returned mapping does not touch the live pins."""
    pin_overrides({"runtime": {"parallelism": 2}})
    snapshot = get_pinned_overrides()
    snapshot["runtime"]["parallelism"] = 999
    assert load_config().runtime.parallelism == 2


def test_pinned_override_reports_only_explicit_pins() -> None:
    """``pinned_override`` distinguishes an explicit pin from the default."""
    assert pinned_override("runtime", "parallelism") is None
    pin_overrides({"runtime": {"parallelism": 7}})
    assert pinned_override("runtime", "parallelism") == 7
    assert pinned_override("runtime", "harness_call_timeout_ms") is None


def test_clear_pinned_overrides_restores_defaults() -> None:
    """``clear_pinned_overrides`` drops every pin."""
    pin_overrides({"aux": {"call_timeout_s": 1.0}})
    clear_pinned_overrides()
    assert get_pinned_overrides() == {}
    assert load_config() == ZicatoConfig()


def test_every_sub_config_is_reachable_from_the_root() -> None:
    """Each domain sub-config is a field on :class:`ZicatoConfig`."""
    cfg = ZicatoConfig()
    assert isinstance(cfg.health, HealthConfig)
    assert isinstance(cfg.aux, AuxConfig)
    assert isinstance(cfg.integration, IntegrationConfig)
    assert isinstance(cfg.dashboard, DashboardConfig)
    assert isinstance(cfg.runtime, RuntimeTuningConfig)
