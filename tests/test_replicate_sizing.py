"""The replicate count sized from the measured noise floor.

The two-sample minimum detectable effect and its inverse share one module, so
the instrument-health ladder and the count an epoch runs cannot drift; the
resolver attributes the count to the contract, the floor, or the structure
default; the loop threads it to the strategy and states it once in the log.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from zicato.core.types import TournamentStructure
from zicato.runtime.effective_settings import SOURCE_TIERS
from zicato.selection.registry import default_replicates_for, make_strategy
from zicato.selection.replicates import (
    REPLICATE_SOURCE_TIERS,
    SOURCE_CONTRACT,
    SOURCE_NOISE_FLOOR,
    SOURCE_STRUCTURE_DEFAULT,
    resolve_replicates,
)
from zicato.tournament.detectable_effect import (
    MDE_ALPHA_RELAXED,
    REPLICATE_SIZING_CAP,
    minimum_detectable_effect,
    replicates_for_margin,
    students_t_upper_quantile,
)

FLOOR = {"generation_id": "v0", "runs": 5, "max_abs_delta": 0.05, "delta_std": 0.02}


def _gauntlet(**params: object) -> TournamentStructure:
    return TournamentStructure(structure="gauntlet", params=dict(params))


# ---------------------------------------------------------------------------
# The formula
# ---------------------------------------------------------------------------


def test_the_t_quantile_matches_the_tables() -> None:
    assert abs(students_t_upper_quantile(0.025, 10) - 2.2281) < 1e-3
    assert abs(students_t_upper_quantile(0.20, 10) - 0.8791) < 1e-3


def test_the_effect_at_six_replicates_is_the_reference_multiple() -> None:
    # CAMPAIGN.md §3: 1.79 × sd at α 0.05 and 1.55 × sd at α 0.10 (n = 6, df = 10).
    assert abs(minimum_detectable_effect(1.0, 6) - 1.7939) < 1e-3  # type: ignore[operator]
    relaxed = minimum_detectable_effect(1.0, 6, alpha=MDE_ALPHA_RELAXED)
    assert abs(relaxed - 1.5539) < 1e-3  # type: ignore[operator]
    assert minimum_detectable_effect(1.0, 1) is None
    assert minimum_detectable_effect(0.0, 4) == 0.0


def test_the_inverse_recovers_every_count_on_the_ladder() -> None:
    sd = 0.037
    for n in range(2, REPLICATE_SIZING_CAP + 1):
        effect = minimum_detectable_effect(sd, n)
        assert effect is not None
        assert replicates_for_margin(sd, effect) == n


def test_the_inverse_is_bounded_by_the_cap_and_refuses_an_empty_margin() -> None:
    sd = 1.0
    at_cap = minimum_detectable_effect(sd, REPLICATE_SIZING_CAP)
    assert at_cap is not None
    assert replicates_for_margin(sd, at_cap) == REPLICATE_SIZING_CAP
    assert replicates_for_margin(sd, at_cap * 0.999) is None
    assert replicates_for_margin(sd, 0.0) is None
    # A zero floor (a deterministic system under test) resolves any margin at two.
    assert replicates_for_margin(0.0, 1e-9) == 2
    # The recommended margin of 2.5 × delta_std needs four replicates.
    assert replicates_for_margin(sd, 2.5) == 4


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def test_a_pinned_count_wins_and_reports_the_effect_it_resolves() -> None:
    setting = resolve_replicates(_gauntlet(replicates=3), floor=FLOOR, promote_margin=0.01)
    assert setting.replicates == 3
    assert setting.source == SOURCE_CONTRACT
    assert setting.delta_std == 0.02
    assert setting.detectable_effect == minimum_detectable_effect(0.02, 3)
    assert setting.under_powered  # 0.02 × 2.6 > 0.01


def test_an_unpinned_count_is_sized_from_the_floor() -> None:
    setting = resolve_replicates(_gauntlet(), floor=FLOOR, promote_margin=0.05)
    assert setting.source == SOURCE_NOISE_FLOOR
    assert setting.replicates == replicates_for_margin(0.02, 0.05) == 4
    assert not setting.under_powered
    assert setting.note is None


def test_the_structure_default_is_the_floor_of_a_derived_count() -> None:
    # A wide margin resolves at two; racing's default of one is lifted to it,
    # and a structure whose default is already two keeps the default tier.
    racing = TournamentStructure(structure="racing", params={"field_size": 4})
    sized = resolve_replicates(racing, floor=FLOOR, promote_margin=1.0)
    assert sized.replicates == 2 and sized.source == SOURCE_NOISE_FLOOR
    gauntlet = resolve_replicates(_gauntlet(), floor=FLOOR, promote_margin=1.0)
    assert gauntlet.replicates == default_replicates_for("gauntlet") == 2
    assert gauntlet.source == SOURCE_NOISE_FLOOR


def test_a_margin_beyond_the_cap_keeps_the_default_and_says_why() -> None:
    setting = resolve_replicates(_gauntlet(), floor=FLOOR, promote_margin=0.001)
    assert setting.replicates == 2
    assert setting.source == SOURCE_STRUCTURE_DEFAULT
    assert setting.note is not None and "32" in setting.note and "raise the margin" in setting.note


def test_without_a_usable_floor_the_default_applies_silently() -> None:
    for floor in (None, {"max_abs_delta": 0.0, "delta_std": 0.0}, {"delta_std": "x"}, {}):
        setting = resolve_replicates(_gauntlet(), floor=floor, promote_margin=0.05)
        assert setting.replicates == 2
        assert setting.source == SOURCE_STRUCTURE_DEFAULT
        assert setting.delta_std is None and setting.detectable_effect is None
        assert setting.note is None


def test_every_replicate_tier_is_a_recorded_settings_tier() -> None:
    assert set(REPLICATE_SOURCE_TIERS) <= set(SOURCE_TIERS)


# ---------------------------------------------------------------------------
# The registry threads the count without overriding a pinned one
# ---------------------------------------------------------------------------


def test_make_strategy_injects_the_count_only_when_the_contract_pins_none() -> None:
    sized = make_strategy(_gauntlet(), replicates=5, noise_floor_delta_std=0.02)
    assert sized.replicates() == 5
    assert sized.params["noise_floor_delta_std"] == 0.02
    pinned = make_strategy(_gauntlet(replicates=1), replicates=5)
    assert pinned.replicates() == 1
    untouched = make_strategy(_gauntlet())
    assert untouched.replicates() == 2
    assert "noise_floor_delta_std" not in untouched.params


# ---------------------------------------------------------------------------
# The loop states the count once
# ---------------------------------------------------------------------------


def _pin_inputs(
    monkeypatch: pytest.MonkeyPatch, floor: object, margin: float, gate_on: bool
) -> None:
    from zicato.health import inputs as health_inputs

    monkeypatch.setattr(
        health_inputs,
        "epoch_noise_floor_inputs",
        lambda workspace_root, epoch_id: (floor, margin, gate_on),
    )


def test_a_derived_count_is_logged_with_its_effect(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from zicato.evolve.round_prepare import _resolve_replicates_in_effect

    _pin_inputs(monkeypatch, FLOOR, 0.05, False)
    with caplog.at_level(logging.INFO, logger="zicato.orchestrator"):
        setting = _resolve_replicates_in_effect(
            Path("/nonexistent"), "epoch-0", _gauntlet(), log_once=True
        )
    assert setting.replicates == 4 and setting.source == SOURCE_NOISE_FLOOR
    (line,) = (m for m in caplog.messages if "replicates in effect" in m)
    assert "4" in line and "derived from the measured noise floor" in line
    assert f"{setting.detectable_effect:.6g}" in line


def test_a_pinned_under_powered_count_warns_once_with_both_numbers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from zicato.evolve.round_prepare import _resolve_replicates_in_effect

    _pin_inputs(monkeypatch, FLOOR, 0.01, False)
    spec = _gauntlet(replicates=2)
    with caplog.at_level(logging.INFO, logger="zicato.orchestrator"):
        first = _resolve_replicates_in_effect(Path("/nonexistent"), "epoch-0", spec, log_once=True)
        _resolve_replicates_in_effect(Path("/nonexistent"), "epoch-0", spec, log_once=False)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    line = warnings[0].getMessage()
    assert "replicates=2" in line and "0.01" in line
    assert f"{first.detectable_effect:.6g}" in line
    assert first.replicates == 2 and first.source == SOURCE_CONTRACT


def test_a_pinned_under_powered_count_is_informational_under_the_evidence_gate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from zicato.evolve.round_prepare import _resolve_replicates_in_effect

    _pin_inputs(monkeypatch, FLOOR, 0.01, True)
    with caplog.at_level(logging.INFO, logger="zicato.orchestrator"):
        _resolve_replicates_in_effect(
            Path("/nonexistent"), "epoch-0", _gauntlet(replicates=2), log_once=True
        )
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    (line,) = (m for m in caplog.messages if "replicates=2" in m)
    assert "evidence gate is on" in line


def test_a_margin_beyond_the_cap_warns_with_the_default_that_stands(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from zicato.evolve.round_prepare import _resolve_replicates_in_effect

    _pin_inputs(monkeypatch, FLOOR, 0.001, False)
    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        setting = _resolve_replicates_in_effect(
            Path("/nonexistent"), "epoch-0", _gauntlet(), log_once=True
        )
    assert setting.source == SOURCE_STRUCTURE_DEFAULT
    (line,) = (m for m in caplog.messages if "replicates in effect" in m)
    assert "structure default" in line and "raise the margin" in line
