"""Tests for :mod:`zicato.config` — the typed configuration tree.

:func:`zicato.config.load_config` is the *single* place zicato reads the
environment. These tests cover the three things that matters: the
dataclass defaults, the env-var parsing (including invalid-value
clamping and type coercion), and the precedence layering
(defaults < env < explicit overrides).

Every test passes an explicit ``env`` dict so the suite is fully
isolated from the real process environment — no ``monkeypatch`` of
``os.environ`` required.
"""

from __future__ import annotations

import dataclasses

import pytest
from zicato.config import (
    AuxConfig,
    BudgetConfig,
    DashboardConfig,
    HealthConfig,
    IntegrationConfig,
    RuntimeTuningConfig,
    WorkspaceConfig,
    ZicatoConfig,
    describe_env_vars,
    load_config,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_empty_env_yields_all_defaults() -> None:
    """An empty environment with no overrides yields the dataclass defaults."""
    cfg = load_config(env={})
    assert cfg == ZicatoConfig()
    assert cfg.health.scoring_window == 3
    assert cfg.health.scoring_epsilon == 1e-6
    assert cfg.health.no_expectations_fraction == 0.5
    assert cfg.health.stalled_rejects == 3
    assert cfg.aux.call_timeout_s == 120.0
    assert cfg.budget.max_wall_clock_seconds is None
    assert cfg.integration.harmonograf_url == ""
    assert cfg.integration.supervisor_binary == ""
    assert cfg.dashboard.static_dir == ""
    assert cfg.workspace.root == ".zicato"
    assert cfg.workspace.instance_id == "default"
    assert cfg.runtime.parallelism == 4


def test_config_tree_is_frozen() -> None:
    """Every config dataclass is frozen — values cannot be mutated in place."""
    cfg = load_config(env={})
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
# Env-var parsing and type coercion
# ---------------------------------------------------------------------------


def test_env_sets_every_section() -> None:
    """Each ``ZICATO_*`` variable lands on the right sub-config field."""
    cfg = load_config(
        env={
            "ZICATO_HEALTH_SCORING_WINDOW": "8",
            "ZICATO_HEALTH_SCORING_EPSILON": "0.25",
            "ZICATO_HEALTH_NO_EXPECTATIONS_FRACTION": "0.75",
            "ZICATO_HEALTH_STALLED_REJECTS": "6",
            "ZICATO_AUX_CALL_TIMEOUT": "45.5",
            "ZICATO_MAX_WALL_CLOCK_SECONDS": "900",
            "ZICATO_HARMONOGRAF_URL": "http://localhost:9000",
            "ZICATO_SUPERVISOR_BINARY": "/opt/zicato-supervisor",
            "ZICATO_DASHBOARD_STATIC_DIR": "/srv/static",
            "ZICATO_WORKSPACE": "/work/.zicato",
            "ZICATO_INSTANCE_ID": "instance-7",
            "ZICATO_PARALLELISM": "16",
            "ZICATO_HARNESS_CALL_TIMEOUT_MS": "600000",
        }
    )
    assert cfg.health.scoring_window == 8
    assert cfg.health.scoring_epsilon == 0.25
    assert cfg.health.no_expectations_fraction == 0.75
    assert cfg.health.stalled_rejects == 6
    assert cfg.aux.call_timeout_s == 45.5
    assert cfg.budget.max_wall_clock_seconds == 900
    assert cfg.integration.harmonograf_url == "http://localhost:9000"
    assert cfg.integration.supervisor_binary == "/opt/zicato-supervisor"
    assert cfg.dashboard.static_dir == "/srv/static"
    assert cfg.workspace.root == "/work/.zicato"
    assert cfg.workspace.instance_id == "instance-7"
    assert cfg.runtime.parallelism == 16
    assert cfg.runtime.harness_call_timeout_ms == 600000


def test_env_int_coercion_produces_real_ints() -> None:
    """An int-typed env var is coerced from its string form to an ``int``."""
    cfg = load_config(env={"ZICATO_HEALTH_SCORING_WINDOW": "5"})
    assert cfg.health.scoring_window == 5
    assert isinstance(cfg.health.scoring_window, int)


def test_env_float_coercion_produces_real_floats() -> None:
    """A float-typed env var is coerced from its string form to a ``float``."""
    cfg = load_config(env={"ZICATO_AUX_CALL_TIMEOUT": "12"})
    assert cfg.aux.call_timeout_s == 12.0
    assert isinstance(cfg.aux.call_timeout_s, float)


def test_optional_int_budget_accepts_a_value() -> None:
    """The wall-clock budget — ``int | None`` — coerces a concrete value."""
    cfg = load_config(env={"ZICATO_MAX_WALL_CLOCK_SECONDS": "600"})
    assert cfg.budget.max_wall_clock_seconds == 600


# ---------------------------------------------------------------------------
# Invalid-value clamping
# ---------------------------------------------------------------------------


def test_unparseable_int_falls_back_to_default() -> None:
    """A non-numeric int env value is clamped back to the default."""
    cfg = load_config(env={"ZICATO_HEALTH_SCORING_WINDOW": "not-a-number"})
    assert cfg.health.scoring_window == 3


def test_non_positive_int_falls_back_to_default() -> None:
    """A zero or negative positive-int env value is clamped to the default."""
    assert load_config(env={"ZICATO_HEALTH_SCORING_WINDOW": "0"}).health.scoring_window == 3
    assert load_config(env={"ZICATO_HEALTH_STALLED_REJECTS": "-4"}).health.stalled_rejects == 3
    assert load_config(env={"ZICATO_PARALLELISM": "0"}).runtime.parallelism == 4
    assert (
        load_config(env={"ZICATO_HARNESS_CALL_TIMEOUT_MS": "0"}).runtime.harness_call_timeout_ms
        == 1_800_000
    )


def test_negative_float_falls_back_to_default() -> None:
    """A negative value for a non-negative-float field is clamped."""
    cfg = load_config(env={"ZICATO_HEALTH_SCORING_EPSILON": "-1.0"})
    assert cfg.health.scoring_epsilon == 1e-6


def test_zero_is_valid_for_non_negative_float() -> None:
    """Zero is a legal value for a non-negative-float field (epsilon)."""
    cfg = load_config(env={"ZICATO_HEALTH_SCORING_EPSILON": "0"})
    assert cfg.health.scoring_epsilon == 0.0


def test_non_positive_aux_timeout_falls_back_to_default() -> None:
    """A zero or negative aux timeout is clamped — a 0s budget is invalid."""
    assert load_config(env={"ZICATO_AUX_CALL_TIMEOUT": "0"}).aux.call_timeout_s == 120.0
    assert load_config(env={"ZICATO_AUX_CALL_TIMEOUT": "-3"}).aux.call_timeout_s == 120.0


def test_unparseable_aux_timeout_falls_back_to_default() -> None:
    """A non-numeric aux timeout env value is clamped to the default."""
    cfg = load_config(env={"ZICATO_AUX_CALL_TIMEOUT": "soon"})
    assert cfg.aux.call_timeout_s == 120.0


def test_invalid_budget_keeps_none_default() -> None:
    """An unparseable wall-clock budget keeps the ``None`` (unbounded) default."""
    unparseable = load_config(env={"ZICATO_MAX_WALL_CLOCK_SECONDS": "lots"})
    assert unparseable.budget.max_wall_clock_seconds is None
    non_positive = load_config(env={"ZICATO_MAX_WALL_CLOCK_SECONDS": "0"})
    assert non_positive.budget.max_wall_clock_seconds is None


# ---------------------------------------------------------------------------
# Precedence / override layering
# ---------------------------------------------------------------------------


def test_overrides_beat_the_environment() -> None:
    """An explicit override wins over an env-var value for the same field."""
    cfg = load_config(
        env={"ZICATO_HEALTH_SCORING_WINDOW": "7"},
        overrides={"health": {"scoring_window": 99}},
    )
    assert cfg.health.scoring_window == 99


def test_overrides_beat_the_defaults() -> None:
    """An explicit override wins over the dataclass default."""
    cfg = load_config(env={}, overrides={"aux": {"call_timeout_s": 5.0}})
    assert cfg.aux.call_timeout_s == 5.0


def test_env_beats_the_defaults() -> None:
    """An env var wins over the dataclass default when no override is given."""
    cfg = load_config(env={"ZICATO_HEALTH_STALLED_REJECTS": "9"})
    assert cfg.health.stalled_rejects == 9


def test_override_and_env_on_different_fields_compose() -> None:
    """Env and override target different fields — both land, no interference."""
    cfg = load_config(
        env={"ZICATO_HEALTH_SCORING_WINDOW": "7"},
        overrides={"aux": {"call_timeout_s": 30.0}},
    )
    assert cfg.health.scoring_window == 7  # from env
    assert cfg.aux.call_timeout_s == 30.0  # from override


def test_override_leaves_other_fields_of_a_section_intact() -> None:
    """Overriding one field of a section preserves that section's other fields."""
    cfg = load_config(
        env={"ZICATO_HEALTH_SCORING_EPSILON": "0.1"},
        overrides={"health": {"scoring_window": 42}},
    )
    assert cfg.health.scoring_window == 42  # from override
    assert cfg.health.scoring_epsilon == 0.1  # from env, untouched by override
    assert cfg.health.stalled_rejects == 3  # default, untouched


# ---------------------------------------------------------------------------
# Override validation
# ---------------------------------------------------------------------------


def test_unknown_override_section_raises() -> None:
    """An unknown section name in ``overrides`` raises rather than silently no-ops."""
    with pytest.raises(KeyError, match="unknown config section"):
        load_config(env={}, overrides={"nonsense": {"x": 1}})


def test_unknown_override_field_raises() -> None:
    """An unknown field name within a known section raises."""
    with pytest.raises(KeyError, match="unknown field"):
        load_config(env={}, overrides={"health": {"not_a_field": 1}})


def test_non_mapping_override_section_raises() -> None:
    """An override section whose value is not a mapping raises ``TypeError``."""
    with pytest.raises(TypeError, match="must be a mapping"):
        load_config(env={}, overrides={"health": 5})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# Isolation: the default env is os.environ but a passed env is honoured
# ---------------------------------------------------------------------------


def test_passed_env_fully_replaces_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ``env`` is read instead of ``os.environ`` — no leakage."""
    # A real env var that would otherwise be picked up.
    monkeypatch.setenv("ZICATO_HEALTH_SCORING_WINDOW", "999")
    # An explicit env that does NOT carry it.
    cfg = load_config(env={})
    assert cfg.health.scoring_window == 3  # default, not 999


def test_default_env_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no ``env`` argument, ``load_config`` reads the real ``os.environ``."""
    monkeypatch.setenv("ZICATO_HEALTH_SCORING_WINDOW", "21")
    cfg = load_config()
    assert cfg.health.scoring_window == 21


# ---------------------------------------------------------------------------
# describe_env_vars introspection
# ---------------------------------------------------------------------------


def test_describe_env_vars_enumerates_every_binding() -> None:
    """``describe_env_vars`` lists every honoured variable mapped to its field."""
    described = describe_env_vars()
    assert described["ZICATO_HEALTH_SCORING_WINDOW"] == "health.scoring_window"
    assert described["ZICATO_AUX_CALL_TIMEOUT"] == "aux.call_timeout_s"
    assert described["ZICATO_MAX_WALL_CLOCK_SECONDS"] == "budget.max_wall_clock_seconds"
    assert described["ZICATO_PARALLELISM"] == "runtime.parallelism"
    # Every described "section.field" pair resolves to a real dataclass field.
    blank = ZicatoConfig()
    for target in described.values():
        section, field = target.split(".")
        assert hasattr(getattr(blank, section), field)


def test_every_sub_config_is_reachable_from_the_root() -> None:
    """Each domain sub-config is a field on :class:`ZicatoConfig`."""
    cfg = ZicatoConfig()
    assert isinstance(cfg.health, HealthConfig)
    assert isinstance(cfg.aux, AuxConfig)
    assert isinstance(cfg.budget, BudgetConfig)
    assert isinstance(cfg.integration, IntegrationConfig)
    assert isinstance(cfg.dashboard, DashboardConfig)
    assert isinstance(cfg.workspace, WorkspaceConfig)
    assert isinstance(cfg.runtime, RuntimeTuningConfig)
