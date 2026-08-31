"""Pre-flight, second review round — severity must obey the gate mode.

Three findings on the #106/#112 branch, all of them one level above the
measurement again:

* **The severity bug (the real one).** ``preflight_signal_below_floor`` was
  emitted ``critical`` unconditionally. The detector re-reads the PERSISTED
  record, so a refuse verdict re-fires identically every round — an unbroken
  critical streak — and two of those trip
  ``_DEGENERATE_HEALTH_STOP_THRESHOLD``. A contract that is genuinely
  noise-limited therefore hard-stopped the loop after two rounds *under the
  DEFAULT* ``preflight_gate="warn"``, making the ``"warn"`` setting
  indistinguishable from ``"refuse"`` except for wasting two rounds first.
  Severity is now gate-aware: ``critical`` only under ``"refuse"`` (where the
  run already stopped at the pre-flight, so the breaker is moot), ``warning``
  otherwise — loud everywhere an operator looks, structurally unable to stop
  a run they asked to let run.
* **``inert`` was credited with a protection it does not provide.** The
  verdict needs champion spread ``> 0`` AND a degraded scalar EXACTLY at the
  champion mean, which is jointly reachable only on a quantized scoring
  scale. The tests below pin what actually happens to issue #106's canonical
  case on the two realistic harnesses, so the docstrings cannot drift back to
  the old story.
* **A config typo silently disabled a refuse-mode gate.** The pre-flight runs
  under ``best_effort``, so an unknown pinned mutation id became a
  ``log.warning`` and the run proceeded UNGATED even under
  ``preflight_gate="refuse"``. An outage never disqualifies a contract; a
  typo is not an outage.

Plus the free-validation ordering: probe selection now runs before
``measure_noise_floor`` spends K champion draws.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests._orchestrator_harness import (
    _bootstrap_workspace,
    _harness_call_llm,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
    _make_aux_responder,
    _valid_proposer_response,
)
from tests.test_evolve_preflight_gate import _prepare, _set_preflight_gate
from zicato.epoch.preflight import (
    VERDICT_INERT,
    VERDICT_REFUSE,
    VERDICT_WARN,
    PreflightConfigError,
    PreflightRefusedError,
    preflight_verdict,
)
from zicato.health.diagnostics import detect_preflight_verdict

_REFUSE_RECORD: dict[str, Any] = {
    "verdict": "refuse",
    "signal": 0.02,
    "noise_floor_max_abs_delta": 0.08,
    "promote_margin": 0.05,
    "window_verdict": "warn",
    "window_failure": "empty_window",
}


# ---------------------------------------------------------------------------
# A. Severity respects the operator's gate mode
# ---------------------------------------------------------------------------


def test_refusal_is_a_warning_under_the_default_gate_and_critical_only_under_refuse() -> None:
    """The severity contract, stated directly.

    Both gradings report the SAME fact with the same numbers and the same
    recommendation; only the severity — and therefore only reachability of the
    degenerate-health breaker — differs.
    """
    (warned,) = detect_preflight_verdict(_REFUSE_RECORD)
    assert warned.code == "preflight_signal_below_floor"
    assert warned.severity == "warning", "the DEFAULT gate must not be able to stop the loop"
    assert warned.detail["preflight_gate"] == "warn"

    (also_warned,) = detect_preflight_verdict(_REFUSE_RECORD, "warn")
    assert also_warned.severity == "warning"
    # "off" is weaker than "warn" — it does not even measure — so it certainly
    # must not grade harder.
    (off,) = detect_preflight_verdict(_REFUSE_RECORD, "off")
    assert off.severity == "warning"

    (hard,) = detect_preflight_verdict(_REFUSE_RECORD, "refuse")
    assert hard.severity == "critical"
    assert hard.detail["preflight_gate"] == "refuse"
    # Same diagnosis either way — the severity is about the operator's choice,
    # not about a different measurement.
    assert hard.summary == warned.summary
    assert hard.detail["recommendation"] == warned.detail["recommendation"]


def test_the_finding_is_still_loud_enough_to_make_the_loop_unhealthy() -> None:
    """Warning, not silence: ``zicato health`` must still say UNHEALTHY.

    Downgrading the severity would be a regression if it hid the finding. It
    does not: ``LoopHealth.healthy`` is false on any warning, so every operator
    surface still shows the refusal — only the circuit breaker stops seeing it.
    """
    from zicato.health.diagnostics import assess_loop_health

    report = assess_loop_health({}, [], [], "e1", preflight=_REFUSE_RECORD)
    assert not report.healthy
    (finding,) = (f for f in report.findings if f.code == "preflight_signal_below_floor")
    assert finding.severity == "warning"

    hard = assess_loop_health({}, [], [], "e1", preflight=_REFUSE_RECORD, preflight_gate="refuse")
    assert [f.severity for f in hard.findings if f.code == "preflight_signal_below_floor"] == [
        "critical"
    ]


def test_the_breaker_observes_criticals_only() -> None:
    """The structural half of the fix, pinned where it lives.

    ``_assess_and_persist_loop_health`` returns ``has_critical``; the loop feeds
    exactly that to :class:`DegenerateHealthPolicy`. So a WARNING cannot trip
    the breaker no matter how many rounds re-emit it — which is what makes
    warn-mode severity a real protection rather than a cosmetic one.
    """
    from zicato.evolve.loop import _DEGENERATE_HEALTH_STOP_THRESHOLD, DegenerateHealthPolicy

    policy = DegenerateHealthPolicy(enabled=True, threshold=_DEGENERATE_HEALTH_STOP_THRESHOLD)
    for _ in range(10):
        assert policy.observe(health_critical=False) is False
    assert policy.streak == 0
    # And it does fire on criticals, so the test above is not vacuous.
    assert [policy.observe(health_critical=True) for _ in range(2)][-1] is True


def _canned_losses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telemetry stubs that let four consecutive rounds all promote."""
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 4.0, "v1": 3.0, "v2": 2.0, "v3": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )


def test_warn_mode_survives_an_all_refuse_preflight_past_the_breaker_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The end-to-end regression: three rounds under warn mode, no hard stop.

    A noise-limited contract under the DEFAULT gate is the exact shape that
    used to die: round 0 persists ``refuse``, every round re-emits the finding
    from that record, and two criticals stopped the loop — overriding the
    operator's explicit ``"warn"``. Now it runs to completion with a loud
    warning, which is what "warn" means.
    """
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _set_preflight_gate(workspace, "warn")
    _install_stub_adapter_factory(monkeypatch)
    _canned_losses(monkeypatch)

    from tests.test_evolve_preflight_gate import _install_canned_preflight

    calls: list[Any] = []
    _install_canned_preflight(
        monkeypatch, verdict="refuse", signal=0.02, floor_max=0.08, calls=calls
    )

    from zicato.evolve.loop import _DEGENERATE_HEALTH_STOP_THRESHOLD, evolve_n_rounds

    rounds = _DEGENERATE_HEALTH_STOP_THRESHOLD + 1
    stop_reason: list[str] = []
    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=rounds,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()] * rounds),
            stop_reason_out=stop_reason,
        )
    )

    assert len(calls) == 1, "the pre-flight is measured once per epoch, then re-read"
    assert stop_reason != ["degenerate_health"], (
        "a refuse verdict under the DEFAULT warn gate tripped the degenerate-health "
        "breaker — the breaker overrode the operator's explicit gate choice"
    )
    assert len(outcomes) == rounds, outcomes

    # The finding IS in every round's persisted report — visible, just not fatal.
    health_dir = workspace / "epochs" / epoch_id / "health"
    reports = sorted(health_dir.glob("round_*.json"))
    assert len(reports) >= _DEGENERATE_HEALTH_STOP_THRESHOLD
    for path in reports:
        findings = json.loads(path.read_text())["findings"]
        refusals = [f for f in findings if f["code"] == "preflight_signal_below_floor"]
        assert refusals, f"{path.name} lost the refusal finding entirely"
        assert refusals[0]["severity"] == "warning", path.name


def test_refuse_mode_still_refuses_at_the_pre_flight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The companion: the protection the severity fix must not weaken.

    An operator who asked for the hard gate still gets it, and gets it at
    round 0 — which is also why the critical severity is harmless there: the
    breaker never sees a round.
    """
    workspace, epoch_id, _ = _prepare(
        monkeypatch, tmp_path, gate="refuse", verdict="refuse", signal=0.02, floor_max=0.08
    )
    from zicato.evolve.loop import evolve_n_rounds

    stop_reason: list[str] = []
    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=3,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()] * 3),
            stop_reason_out=stop_reason,
        )
    )
    assert outcomes == []
    assert stop_reason == ["preflight_refused"]


# ---------------------------------------------------------------------------
# B. What `inert` actually covers (and what covers issue #106)
# ---------------------------------------------------------------------------


def test_inert_needs_a_quantized_scale_and_does_not_catch_issue_106() -> None:
    """The honest reach of ``inert``, pinned so the docstring cannot drift back.

    #106's canonical case is a live mutation point the deliverable happens to
    route around (a tool description bypassed by a structured-output schema).
    Follow it through both realistic harnesses:

    * NOISY harness — the degraded draw lands near, but not exactly on, the
      mean of the varying champion draws. Signal is small and positive, so the
      verdict is ``refuse``, NOT ``inert``. (Continuous scale ⇒ landing exactly
      on the mean is measure-zero.)
    * DETERMINISTIC harness — the champion draws do not vary at all, so the
      spread is zero and SATURATION claims the case first.

    So ``inert`` fires only where a quantized scale makes the champion mean an
    attainable score. It is correct when it fires and costs nothing to keep,
    but it is not what keeps #106's healthy board off a false refusal.
    """
    # Noisy harness, champion draws 0.50/0.60, degraded 0.549 — one LSB off the
    # mean, which is all a continuous scale ever gives you.
    verdict, signal = preflight_verdict((0.50, 0.60), 0.549, 0.10)
    assert 0.0 < signal <= 0.10
    assert verdict == VERDICT_REFUSE, (
        "the routed-around point of #106 presents as a weak probe, not as an "
        "inert one — so `inert` is not the guard against its false refusal"
    )

    # Deterministic harness: no champion spread, so saturation precedes inert.
    verdict, signal = preflight_verdict((0.55, 0.55), 0.55, 0.0)
    assert (verdict, signal) == (VERDICT_WARN, 0.0)

    # Quantized scale where the mean IS attainable — the one case that reaches
    # `inert`, and it is a real (if narrow) diagnosis.
    verdict, signal = preflight_verdict((0.4, 0.6), 0.5, 0.1)
    assert (verdict, signal) == (VERDICT_INERT, 0.0)


def test_what_does_guard_issue_106_is_the_sample_plus_the_warn_severity() -> None:
    """Name the two real protections, so a future edit cannot re-credit ``inert``.

    (1) the role-diverse multi-point sample out-measures a routed-around point;
    (2) under the default gate the resulting finding is a warning, so even a
    board the pre-flight calls unmeasurable keeps running while the operator
    fixes the sample.
    """
    from tests.test_preflight_probe_and_margin_window import _point
    from zicato.epoch.preflight import select_probe_points

    points = [
        _point("routed_around", role="tool_description"),
        _point("coordinator", role="coordinator_routing"),
    ]
    sample, _ = select_probe_points(points, limit=2)
    assert [p.id for p in sample] == ["routed_around", "coordinator"], (
        "the sample must reach a second role, so one routed-around point cannot "
        "decide the verdict alone"
    )
    (finding,) = detect_preflight_verdict(_REFUSE_RECORD)
    assert finding.severity == "warning"


# ---------------------------------------------------------------------------
# C. A config typo must not silently disable a refuse-mode gate
# ---------------------------------------------------------------------------


def _pin_probe_ids(workspace: Path, ids: list[str]) -> None:
    cfg_path = workspace / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg.setdefault("runtime", {})["preflight_probe_mutation_ids"] = ids
    cfg_path.write_text(json.dumps(cfg))


def _run_once(workspace: Path, epoch_id: str) -> Any:
    from zicato.orchestrator import evolve_once

    return asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )


def test_an_unknown_pinned_probe_id_refuses_the_run_under_the_hard_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A typo in ``runtime.preflight_probe_mutation_ids`` must not proceed ungated.

    The pre-flight runs under ``best_effort`` because an endpoint outage must
    never disqualify a contract. A misspelled mutation id is the opposite kind
    of failure: deterministic, identical next round, and entirely the
    operator's. Swallowing it left a ``preflight_gate="refuse"`` run with NO
    gate at all — the one outcome that operator ruled out.

    Note this drives the REAL pre-flight (no canned verdict): the refusal has
    to survive the whole config → selection → hook chain.
    """
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _set_preflight_gate(workspace, "refuse")
    _pin_probe_ids(workspace, ["no_such_mutation_point"])
    _install_stub_adapter_factory(monkeypatch)
    _canned_losses(monkeypatch)

    with pytest.raises(PreflightRefusedError, match="CONFIG ERROR"):
        _run_once(workspace, epoch_id)


def test_the_same_typo_only_warns_under_the_default_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Warn mode keeps the recommend-only behaviour it always had."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _set_preflight_gate(workspace, "warn")
    _pin_probe_ids(workspace, ["no_such_mutation_point"])
    _install_stub_adapter_factory(monkeypatch)
    _canned_losses(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="zicato.orchestrator"):
        outcome = _run_once(workspace, epoch_id)

    assert outcome is not None
    msgs = [r.getMessage() for r in caplog.records if r.name == "zicato.orchestrator"]
    assert any("contract pre-flight skipped" in m and "do not enumerate" in m for m in msgs), msgs


def test_a_config_error_is_a_value_error_so_existing_handlers_still_catch_it() -> None:
    """``PreflightConfigError`` narrows ``ValueError``; it does not replace it."""
    assert issubclass(PreflightConfigError, ValueError)


# ---------------------------------------------------------------------------
# D. Validate the knobs before spending draws
# ---------------------------------------------------------------------------


def test_probe_selection_is_validated_before_the_floor_spends_a_draw(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mistyped knob must cost ZERO champion evaluations.

    ``select_probe_points``'s checks are pure and free, but they used to run
    after ``measure_noise_floor`` had already drawn the champion K times — so
    an operator paid K real evaluations to be told they had a typo. The
    monkeypatched floor asserts the ordering directly: it is never reached.
    """
    from tests.test_contract_preflight import _bootstrap, _run_preflight

    workspace, epoch_id = _bootstrap(tmp_path)

    measured: list[Any] = []

    async def _never_measure(**kwargs: Any) -> Any:
        measured.append(kwargs)
        raise AssertionError("the noise floor was measured before the knobs were validated")

    monkeypatch.setattr("zicato.epoch.preflight.measure_noise_floor", _never_measure)

    with pytest.raises(PreflightConfigError, match="do not enumerate"):
        _run_preflight(workspace, epoch_id, degrade_mutation_id="no_such_point")
    assert measured == []

    with pytest.raises(PreflightConfigError, match="must be >= 1"):
        _run_preflight(workspace, epoch_id, probe_points=0)
    assert measured == []


def test_the_probe_ceiling_is_validated_at_construction() -> None:
    """The knob cannot be set wider than the reserved replicate block.

    ``preflight_probe_points`` indexes replicate slots at
    ``PREFLIGHT_REPLICATE_BASE + j``; past the block width the pre-flight would
    draw into the candidate screen's range and make ITS cache idempotence a
    lie. The pre-flight already refused such a sample, but only after
    enumerating a snapshot — validating the field fails at the config that set
    it instead.
    """
    from zicato.core.runtime import PREFLIGHT_PROBE_POINTS_MAX
    from zicato.epoch.preflight import PREFLIGHT_REPLICATE_SPAN
    from zicato.testing.fixtures import make_runtime_config

    assert PREFLIGHT_PROBE_POINTS_MAX == PREFLIGHT_REPLICATE_SPAN, (
        "the mirrored ceiling drifted from the replicate block it mirrors — "
        "zicato.core cannot import zicato.epoch, so this equality is the seam"
    )

    ok = make_runtime_config(preflight_probe_points=PREFLIGHT_PROBE_POINTS_MAX)
    assert ok.preflight_probe_points == PREFLIGHT_PROBE_POINTS_MAX
    with pytest.raises(ValueError, match="must be <="):
        make_runtime_config(preflight_probe_points=PREFLIGHT_PROBE_POINTS_MAX + 1)
