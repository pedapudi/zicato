"""Strict domain configuration, explicit defaults, and environment inventory."""

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
    describe_env_vars,
    health_config_from_workspace,
    load_config,
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


def test_health_block_rejects_invalid_values_with_their_field_path() -> None:
    """Invalid authored thresholds fail before a detector can use a default."""
    for name, value in {
        "scoring_window": 0,
        "stalled_rejects": -4,
        "scoring_epsilon": -1.0,
        "generalization_gap_warn": "not-a-number",
    }.items():
        with pytest.raises(ValueError, match=f"health.{name}"):
            health_config_from_workspace({"health": {name: value}})


def test_health_block_zero_is_valid_for_non_negative_float() -> None:
    """Zero is a legal value for a non-negative-float field (epsilon)."""
    cfg = health_config_from_workspace({"health": {"scoring_epsilon": 0}})
    assert cfg.scoring_epsilon == 0.0


def test_health_block_rejects_string_numbers() -> None:
    """An authored integer field requires an integer JSON value."""
    with pytest.raises(ValueError, match="health.scoring_window: expected an integer"):
        health_config_from_workspace({"health": {"scoring_window": "5"}})


def test_health_block_unknown_key_raises() -> None:
    """A typo'd key fails loudly, naming the valid fields."""
    with pytest.raises(ValueError, match="health.scoring_windw"):
        health_config_from_workspace({"health": {"scoring_windw": 5}})


def test_health_block_non_object_raises() -> None:
    """A ``health`` block that is not a JSON object fails loudly."""
    with pytest.raises(ValueError, match="health: expected an object"):
        health_config_from_workspace({"health": 5})


# ---------------------------------------------------------------------------
# Deleted bindings — env vars replaced by CLI flags are gone, not aliased
# ---------------------------------------------------------------------------

#: Every deleted env binding, with a plausible value. The redundant trio
#: was fully shadowed by pre-existing CLI flags; five more were converted
#: to flags (`zicato evolve --parallelism /
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
        assert name not in by_name


# ---------------------------------------------------------------------------
# The merited env-var set
# ---------------------------------------------------------------------------


def test_environment_report_describes_the_retained_boundary_values() -> None:
    from zicato.epoch.snapshot_scope import SCRATCH_DIR_ENV
    from zicato.runtime.context import RUNTIME_CONTEXT_ENV

    infos = describe_env_vars()
    assert all(info.role and info.description for info in infos)
    by_name = {info.name: info for info in infos}
    assert by_name[SCRATCH_DIR_ENV].role == "harness-contract"
    assert by_name[RUNTIME_CONTEXT_ENV].role == "internal-handoff"
    assert by_name["XDG_RUNTIME_DIR"].role == "operating-system"
    assert "ZICATO_HARMONOGRAF_URL" not in by_name
    assert "ZICATO_HARMONOGRAF_GRPC" not in by_name


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
    with pytest.raises(ValueError, match="config.nonsense: unknown field"):
        load_config(overrides={"nonsense": {"x": 1}})


def test_unknown_override_field_raises() -> None:
    """An unknown field name within a known section raises."""
    with pytest.raises(ValueError, match="unknown field"):
        load_config(overrides={"health": {"not_a_field": 1}})


def test_non_mapping_override_section_raises() -> None:
    """An override section whose value is not a mapping raises ``TypeError``."""
    with pytest.raises(ValueError, match="expected an object"):
        load_config(overrides={"health": 5})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# Deleted env vars are ignored through the REAL os.environ too
# ---------------------------------------------------------------------------


def test_deleted_env_vars_ignored_via_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted variable in the real process environment is a no-op."""
    monkeypatch.setenv("ZICATO_HEALTH_SCORING_WINDOW", "21")
    cfg = load_config()
    assert cfg.health.scoring_window == 3  # default — env var deleted


def test_every_sub_config_is_reachable_from_the_root() -> None:
    """Each domain sub-config is a field on :class:`ZicatoConfig`."""
    cfg = ZicatoConfig()
    assert isinstance(cfg.health, HealthConfig)
    assert isinstance(cfg.aux, AuxConfig)
    assert isinstance(cfg.integration, IntegrationConfig)
    assert isinstance(cfg.dashboard, DashboardConfig)
    assert isinstance(cfg.runtime, RuntimeTuningConfig)
