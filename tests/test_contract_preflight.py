"""Contract pre-flight — unit, integration, CLI, and epoch-open hook tests.

The pure verdict math and the synthetic-degradation rules are exercised
with synthetic values; the full measurement runs against target_0's
planted-defect adapters:

* the DETERMINISTIC adapter measures a floor of exactly ``0.0`` and a
  strictly positive achievable signal → verdict ``ok``;
* a no-expectation variant of the board saturates (the degraded tree
  emits the same number of drift frames as the champion, and no
  predicate can tell them apart) → the ``warn`` verdict — the historical
  ``1.000000`` null-run signature;
* the seeded-noise adapter at high sigma swamps the perturbation signal
  under the A/A floor → the ``refuse`` verdict.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace as _replace
from pathlib import Path

import pytest

import zicato_examples.target_0_convergence as _t0_pkg
from zicato.epoch.lifecycle import _scoring_from_dict, load_epoch, new_epoch
from zicato.epoch.preflight import (
    PREFLIGHT_REPLICATE_BASE,
    VERDICT_OK,
    VERDICT_REFUSE,
    VERDICT_WARN,
    degraded_content_for,
    effective_gate_verdict,
    preflight_verdict,
    run_contract_preflight,
)
from zicato.health.diagnostics import detect_preflight_verdict
from zicato.tournament.calibration import CALIBRATION_REPLICATE_BASE
from zicato_examples.target_0_convergence import mocks as t0_mocks

# Every unit here is target_0, whose adapter reads a generation as TEXT,
# and none of these tests is about the process boundary — so they run
# through the worker entry in-process (tests/conftest.py).
pytestmark = pytest.mark.usefixtures("inline_worker")

EXAMPLE_DIR = Path(_t0_pkg.__file__).resolve().parent
AGENT_DIR = EXAMPLE_DIR / "agent"
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"
SCORING_PATH = EXAMPLE_DIR / "scoring.json"

DETERMINISTIC_ADAPTER = {
    "kind": "import",
    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
}


def _noisy_adapter(sigma: float) -> dict:
    return {
        "kind": "import",
        "factory": "zicato_examples.target_0_convergence.harness:make_noisy_adapter",
        "args": [{"noise_sigma": sigma}],
    }


# ---------------------------------------------------------------------------
# Pure verdict math + degradation rules
# ---------------------------------------------------------------------------


def test_preflight_replicate_base_clears_calibration_slots() -> None:
    # The degraded draw must never collide with duels (0..) or A/A draws.
    assert PREFLIGHT_REPLICATE_BASE > CALIBRATION_REPLICATE_BASE


def test_verdict_ok_when_signal_clears_floor() -> None:
    import pytest

    verdict, signal = preflight_verdict([3.6, 3.6, 3.6], 3.0, 0.0)
    assert verdict == VERDICT_OK
    assert signal == pytest.approx(0.6)


def test_verdict_refuse_when_signal_at_or_below_floor() -> None:
    # Floor 1.0 (noisy draws), degraded moved only 0.5 from the mean.
    verdict, signal = preflight_verdict([3.0, 4.0], 4.0, 1.0)
    assert verdict == VERDICT_REFUSE
    assert signal == 0.5
    # Exactly-at-floor also refuses (signal <= floor).
    verdict, _ = preflight_verdict([3.0, 5.0], 5.0, 1.0)
    assert verdict == VERDICT_REFUSE


def test_verdict_warn_on_exact_saturation_beats_refuse() -> None:
    # Every probe identical — the 1.000000 signature. Even though
    # signal (0) <= floor (0) also holds, the saturation diagnosis wins.
    verdict, signal = preflight_verdict([1.0, 1.0, 1.0], 1.0, 0.0)
    assert verdict == VERDICT_WARN
    assert signal == 0.0


def test_degraded_content_rules() -> None:
    from zicato.core.mutation import MutationPoint

    def _point(kind: str, content: str, suffix: str = ".py") -> MutationPoint:
        return MutationPoint(
            id="p",
            kind=kind,  # type: ignore[arg-type]
            file=Path(f"/tmp/x{suffix}"),
            source_root=Path("/tmp"),
            line_start=1,
            line_end=1,
            content=content,
            content_hash="",
        )

    # Span: deterministic scramble (reversal) — a pure function of content.
    assert degraded_content_for(_point("span", "abc")) == "cba"
    # Empty span: a fixed garbage token, never a no-op probe.
    assert degraded_content_for(_point("span", "  ")) == "zicato-preflight-degraded"
    # Code region: blanked control flow, always-valid Python.
    assert degraded_content_for(_point("code", "if x:\n    y()\n")) == "pass\n"
    # Whole .py file: a comment-only module (parses, exports nothing).
    assert degraded_content_for(_point("file", "X = 1\n")).startswith("#")
    # Whole non-.py file: reversed content.
    assert degraded_content_for(_point("file", "abc", suffix=".md")) == "cba"


# ---------------------------------------------------------------------------
# Health detector
# ---------------------------------------------------------------------------


def test_detector_silent_without_record_or_on_ok() -> None:
    assert detect_preflight_verdict(None) == []
    assert detect_preflight_verdict({"verdict": "ok", "signal": 1.0}) == []
    assert detect_preflight_verdict({"verdict": "junk"}) == []
    assert detect_preflight_verdict({}) == []


def test_detector_fires_on_refuse_and_warning_on_saturation() -> None:
    """The refusal finding's severity follows the operator's gate mode.

    ``critical`` under the opt-in hard gate; ``warning`` under the default
    ``preflight_gate="warn"``, where a critical would re-fire from the
    persisted record every round and trip the degenerate-health breaker —
    turning the mode the operator chose into ``"refuse"``. See
    ``tests/test_preflight_severity_and_config_gate.py``.
    """
    record = {"verdict": "refuse", "signal": 0.1, "noise_floor_max_abs_delta": 0.5}
    (refuse,) = detect_preflight_verdict(record, "refuse")
    assert refuse.code == "preflight_signal_below_floor"
    assert refuse.severity == "critical"
    assert refuse.detail["signal"] == 0.1
    assert refuse.detail["noise_floor_max_abs_delta"] == 0.5

    (warned,) = detect_preflight_verdict(record)
    assert warned.code == "preflight_signal_below_floor"
    assert warned.severity == "warning"

    (warn,) = detect_preflight_verdict({"verdict": "warn", "signal": 0.0})
    assert warn.code == "preflight_saturated_contract"
    assert warn.severity == "warning"


def test_detector_separates_an_inert_probe_from_a_noise_limited_contract() -> None:
    """Issue #106: two zero-signal causes must not read as the same finding.

    An inert probe is a ``warning`` (the measurement is missing) rather than a
    ``critical`` (the contract is broken), and its recommendation points at
    probe selection instead of at the board.
    """
    (inert,) = detect_preflight_verdict(
        {
            "verdict": "inert",
            "signal": 0.0,
            "noise_floor_max_abs_delta": 0.08,
            "probed_points": [{"mutation_id": "docs_tone", "signal": 0.0}],
        }
    )
    assert inert.code == "preflight_inert_probe"
    assert inert.severity == "warning"
    assert "UNMEASURED" in inert.summary
    assert "preflight_probe_mutation_ids" in inert.detail["recommendation"]


def test_detector_fires_on_a_margin_window_failure_alone() -> None:
    """Issue #112: a contract can clear its noise floor and still be null.

    The window is a separate question, so the finding must fire even when the
    signal verdict is ``ok``. It is a WARNING and not a critical for two
    independent reasons: what the probe measures is DEGRADATION headroom, which
    does not bound a challenger's improvement at all (issue #119), and it
    degrades ONE point per draw, so it under-reports even that. Critical would
    trip the loop's degenerate-health circuit breaker and kill a legitimate
    recombination run whose margin sits above single-point reach by design.

    The record here carries the pre-#119 ``window_verdict: "refuse"`` on
    purpose — the DETECTOR still surfaces it; what changed is that the GATE no
    longer escalates it (see :func:`effective_gate_verdict`).
    """
    (finding,) = detect_preflight_verdict(
        {
            "verdict": "ok",
            "signal": 0.041,
            "noise_floor_max_abs_delta": 0.02,
            "promote_margin": 0.10,
            "window_verdict": "refuse",
            "window_failure": "margin_above_achievable",
        }
    )
    assert finding.code == "preflight_margin_above_achievable"
    assert finding.severity == "warning"
    assert "degradation signal" in finding.summary
    assert "UNMEASURED" in finding.summary, "the summary says improvement headroom is not known"
    assert finding.detail["promote_margin"] == 0.10

    (below,) = detect_preflight_verdict(
        {
            "verdict": "ok",
            "signal": 0.50,
            "noise_floor_max_abs_delta": 0.20,
            "promote_margin": 0.10,
            "window_verdict": "warn",
            "window_failure": "margin_below_floor",
        }
    )
    assert below.code == "preflight_margin_below_floor"
    assert below.severity == "warning"


def test_margin_window_finding_never_trips_the_degenerate_health_breaker() -> None:
    """A recombination contract must survive its own deliberately-high margin.

    ``recombine`` exists to union two individually sub-margin fixes, so such a
    contract legitimately runs with ``promote_margin`` above what any SINGLE
    mutation point can move — and the pre-flight's achievable signal is exactly
    a single-point measurement. Were the window finding a ``critical``,
    ``evolve_n_rounds``'s degenerate-health circuit breaker would stop the run
    after two rounds and the known-answer recombination tests could never
    promote. This pins the severity contract that keeps them running.
    """
    from zicato.evolve.loop import _DEGENERATE_HEALTH_STOP_THRESHOLD

    assert _DEGENERATE_HEALTH_STOP_THRESHOLD >= 1  # the breaker exists
    findings = detect_preflight_verdict(
        {
            "verdict": "ok",
            "signal": 0.2,
            "noise_floor_max_abs_delta": 0.0,
            "promote_margin": 1.5,
            "window_verdict": "refuse",
            "window_failure": "margin_above_achievable",
        }
    )
    assert findings, "the window failure must still be reported"
    assert not any(f.severity == "critical" for f in findings), (
        "a single-point achievable-signal bound is evidence, not proof — a "
        "critical here trips the degenerate-health breaker and kills legitimate "
        "compound-patch (recombination) runs"
    )


def test_detector_does_not_double_report_an_empty_window() -> None:
    """``empty_window`` IS the refusal, so it rewrites the recommendation.

    Emitting a second critical for the same fact would be noise; issue #112's
    ask is that the operator be told not to tune the margin at all.
    """
    (finding,) = detect_preflight_verdict(
        {
            "verdict": "refuse",
            "signal": 0.041,
            "noise_floor_max_abs_delta": 0.10,
            "promote_margin": 0.10,
            "window_verdict": "warn",
            "window_failure": "empty_window",
        }
    )
    assert finding.code == "preflight_signal_below_floor"
    assert "no promote_margin is defensible" in finding.detail["recommendation"]


# ---------------------------------------------------------------------------
# Integration — target_0 through the real board-unit workers
# ---------------------------------------------------------------------------


def _bootstrap(
    tmp_path: Path,
    *,
    adapter_block: dict | None = None,
    board_source: Path | None = None,
    extra_config: dict | None = None,
    agent_dir: Path | None = None,
    weights: object | None = None,
) -> tuple[Path, str]:
    workspace = tmp_path / ".zicato"
    workspace.mkdir(parents=True)
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "default",
                "created_at": "2026-07-01T00:00:00Z",
                "generation_source_backend": "git",
                "adapter": adapter_block or DETERMINISTIC_ADAPTER,
                "mutable_trees": [str(agent_dir or AGENT_DIR)],
                **(extra_config or {}),
            }
        )
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Pre-flight brief\n- Remove defect tokens.\n")
    resolved_weights = (
        weights if weights is not None else _scoring_from_dict(json.loads(SCORING_PATH.read_text()))
    )
    cfg = new_epoch(
        workspace,
        name="t0-preflight",
        board_source=board_source or BOARD_PATH,
        brief_source=brief,
        weights=resolved_weights,  # type: ignore[arg-type]
        auto_close_previous=False,
        proposer_path=EXAMPLE_DIR / "proposer",
    )
    return workspace, cfg.id


def _seed_baseline(workspace: Path, epoch_id: str) -> object:
    """Materialise v0 (no rounds, no proposer) and return the champion."""
    from zicato import workspace_loader
    from zicato.core.types import Generation
    from zicato.evolve.generation_phase import current_generation, snapshot_root
    from zicato.evolve.round_baseline import _ensure_baseline_snapshot

    workspace_config = workspace_loader.load_workspace_config(workspace)
    _ensure_baseline_snapshot(workspace, epoch_id, workspace_config)
    champion_id = current_generation(workspace, epoch_id)
    return Generation(
        id=champion_id,
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=snapshot_root(workspace, epoch_id, champion_id),
        created_at="",
        promoted=True,
    )


def _run_preflight(workspace: Path, epoch_id: str, runs: int = 3, **kwargs: object) -> tuple:
    from zicato import adapter_factory, runtime_factory, workspace_loader

    champion = _seed_baseline(workspace, epoch_id)
    workspace_config = workspace_loader.load_workspace_config(workspace)
    adapter = adapter_factory.make_adapter_from_config(workspace_config)
    config = runtime_factory.make_runtime_config(
        workspace_config,
        workspace_root=workspace,
        harness_call_llm=t0_mocks.harness_llm,
        auxiliary_call_llm=t0_mocks.aux_llm,
    )
    epoch_cfg = load_epoch(workspace, epoch_id)
    return asyncio.run(
        run_contract_preflight(
            adapter=adapter,
            generation=champion,  # type: ignore[arg-type]
            board=workspace_loader.load_current_board(workspace),
            weights=epoch_cfg.scoring,
            config=config,
            workspace_root=workspace,
            epoch_id=epoch_id,
            runs=runs,
            **kwargs,  # type: ignore[arg-type]
        )
    )


def test_deterministic_adapter_ok_verdict(tmp_path: Path) -> None:
    """Floor exactly 0.0, perturbation signal > 0 ⇒ OK, and the degraded
    tree was ephemeral — the lineage carries only v0."""
    workspace, epoch_id = _bootstrap(tmp_path)
    report, floor = _run_preflight(workspace, epoch_id)

    assert floor.max_abs_delta == 0.0
    assert report.verdict == VERDICT_OK
    assert report.signal > 0.0
    assert report.noise_floor_max_abs_delta == 0.0
    # target_0 enumerates exactly one mutation point, so the sample IS that
    # point and it settles the verdict on the first probe.
    assert report.degraded_mutation_id == "style_rules"
    assert report.degraded_mutation_kind == "span"
    assert [(p.mutation_id, p.skipped) for p in report.probed_points] == [("style_rules", "")]
    # A healthy contract still costs exactly ONE degraded draw (issue #106's
    # multi-point sample is a ceiling, not a spend).
    assert report.probed_points[0].signal == report.signal
    # The degraded tree never entered the lineage: only v0 exists.
    from zicato.epoch.genstore import default_generation_store

    assert default_generation_store(workspace).list_generations(epoch_id) == ["v0"]
    # The real champion snapshot is untouched (still carries its defects).
    policy = (Path(report.degraded_file).name, "policy.py")
    assert policy[0] == policy[1]
    snapshot_policy = (
        default_generation_store(workspace).materialize_snapshot(epoch_id, "v0")
        / "agent"
        / "policy.py"
    ).read_text()
    assert "verbose-prose; omit-summary; skip-citations" in snapshot_policy

    # Persist + round-trip through the epoch record (additive field).
    from zicato.epoch.lifecycle import set_epoch_preflight

    before = load_epoch(workspace, epoch_id)
    set_epoch_preflight(workspace, epoch_id, report.to_json())
    reloaded = load_epoch(workspace, epoch_id)
    assert reloaded.preflight is not None
    assert reloaded.preflight["verdict"] == "ok"
    assert reloaded.preflight["generation_id"] == "v0"
    # A runtime measurement, never a contract input: the hash is untouched.
    assert reloaded.contract_hash == before.contract_hash


def _no_expectation_board(tmp_path: Path) -> Path:
    """target_0's board with every ``expectation`` stripped — a board that
    cannot discriminate the deterministic harness's defect tokens (each
    token still emits exactly one drift frame, degraded or not)."""
    out = tmp_path / "board_saturating.jsonl"
    lines = []
    for line in BOARD_PATH.read_text().splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        payload.pop("expectation", None)
        lines.append(json.dumps(payload))
    out.write_text("\n".join(lines) + "\n")
    return out


def test_saturating_board_warns(tmp_path: Path) -> None:
    """A no-expectation board scores champion and degraded identically —
    the 1.000000 saturation signature ⇒ WARN (not REFUSE)."""
    board = _no_expectation_board(tmp_path)
    workspace, epoch_id = _bootstrap(tmp_path, board_source=board)
    report, floor = _run_preflight(workspace, epoch_id)

    assert floor.max_abs_delta == 0.0
    assert report.degraded_scalar == report.champion_scalars[0]
    assert report.signal == 0.0
    assert report.verdict == VERDICT_WARN
    (finding,) = detect_preflight_verdict(report.to_json())
    assert finding.code == "preflight_saturated_contract"
    assert finding.severity == "warning"


def _single_unknown_token_agent(tmp_path: Path) -> Path:
    """A policy whose ONE token is unknown to the harness.

    Unknown tokens emit one drift frame each but suppress no feature, and
    the pre-flight's scramble (a reversal) turns one unknown token into
    another — so the degraded tree's TRUE quality equals the champion's.
    Any measured difference is pure observation noise: exactly the
    "noise swamps the achievable signal" pathology REFUSE exists for.
    """
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "__init__.py").write_text("")
    (agent / "policy.py").write_text(
        '"""Single-unknown-token policy for the pre-flight REFUSE case."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        '# zicato:mutable id="style_rules" role="writing_policy"\n'
        'STYLE_RULES = "quirky-tone"\n'
        "\n"
        '__all__ = ["STYLE_RULES"]\n'
    )
    return agent


def test_noisy_adapter_refuses_when_signal_below_floor(tmp_path: Path) -> None:
    """High-sigma seeded noise + a perturbation that cannot move true
    quality ⇒ the A/A floor swamps the measured signal — the
    signal<=floor REFUSE path — and the detector fires."""
    workspace, epoch_id = _bootstrap(
        tmp_path,
        adapter_block=_noisy_adapter(0.45),
        agent_dir=_single_unknown_token_agent(tmp_path),
    )
    report, floor = _run_preflight(workspace, epoch_id, runs=5)

    assert floor.max_abs_delta > 0.0
    assert report.signal <= floor.max_abs_delta
    assert report.verdict == VERDICT_REFUSE
    (finding,) = detect_preflight_verdict(report.to_json())
    assert finding.code == "preflight_signal_below_floor"
    # Live-measured records grade exactly like hand-written ones: the severity
    # is the operator's gate mode, not a property of the measurement.
    assert finding.severity == "warning"
    (hard,) = detect_preflight_verdict(report.to_json(), "refuse")
    assert hard.severity == "critical"


# ---------------------------------------------------------------------------
# Issue #106 — an inert FIRST point must not decide the verdict
# ---------------------------------------------------------------------------


def _inert_first_point_agent(tmp_path: Path) -> Path:
    """An agent whose FIRST enumerated point is inert under the contract.

    The real shape #106 filed: the harness synthesises its deliverable from
    ``agent/policy.py``'s ``STYLE_RULES`` and reads nothing else, so
    ``docs_policy.py``'s span — annotated, enumerable, a perfectly legitimate
    mutation point — cannot influence any measurement. Enumeration sorts by
    ``(source_root, file, line_start, id)``, so ``docs_policy.py`` lands
    BEFORE ``policy.py``: pre-#106 this dead point was the only point ever
    probed, and a healthy board measured signal 0.
    """
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "__init__.py").write_text("")
    (agent / "docs_policy.py").write_text(
        '"""Documentation tone — never read by the deliverable harness."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        '# zicato:mutable id="docs_tone" role="doc_tone"\n'
        'DOCS_TONE = "friendly; second-person; short-sentences"\n'
        "\n"
        '__all__ = ["DOCS_TONE"]\n'
    )
    (agent / "policy.py").write_text(
        (AGENT_DIR / "policy.py").read_text(),
    )
    return agent


def test_the_inert_fixture_really_enumerates_first(tmp_path: Path) -> None:
    """Guard the guard: the fixture must reproduce the pre-fix SELECTION.

    If ``docs_tone`` ever stopped sorting ahead of ``style_rules`` the
    regression test below would pass for the wrong reason — it would no longer
    be exercising an inert FIRST point at all.
    """
    from zicato.mutation.enumerator import enumerate_mutations

    points = enumerate_mutations([_inert_first_point_agent(tmp_path)])
    assert [p.id for p in points] == ["docs_tone", "style_rules"]


def test_inert_first_point_no_longer_decides_the_verdict(tmp_path: Path) -> None:
    """THE issue-#106 regression: a healthy board is no longer condemned.

    Pre-fix, ``points[0]`` was the only probe, so ``docs_tone`` — inert under
    this contract — produced signal 0 on a board that discriminates perfectly,
    and the pre-flight reported the unmeasurable-contract verdict every round
    (deterministically, never flakily). The sample now reaches ``style_rules``
    and the max over probes clears the floor.
    """
    workspace, epoch_id = _bootstrap(tmp_path, agent_dir=_inert_first_point_agent(tmp_path))
    report, floor = _run_preflight(workspace, epoch_id)

    assert floor.max_abs_delta == 0.0
    assert report.verdict == VERDICT_OK
    assert report.signal > 0.0
    # Both points are on the record, with the diagnosis #106 asked for: the
    # operator can see that one was inert and the other was not, rather than
    # only the winner.
    probes = {p.mutation_id: p for p in report.probed_points}
    assert set(probes) == {"docs_tone", "style_rules"}
    assert probes["docs_tone"].signal == 0.0, "the inert point moved nothing"
    assert probes["style_rules"].signal == report.signal


def test_single_point_probe_reproduces_the_pre_fix_measurement(tmp_path: Path) -> None:
    """``probe_points=1`` is the pre-#106 behaviour, and it is now opt-in.

    Confining the sample to the first point measures signal 0 on the same
    healthy board — which is the whole reason the ceiling defaults above 1.
    """
    workspace, epoch_id = _bootstrap(tmp_path, agent_dir=_inert_first_point_agent(tmp_path))
    report, _floor = _run_preflight(workspace, epoch_id, probe_points=1)

    assert [p.mutation_id for p in report.probed_points] == ["docs_tone"]
    assert report.signal == 0.0
    assert report.verdict != VERDICT_OK


def test_explicit_pin_probes_exactly_the_named_point(tmp_path: Path) -> None:
    """``--degrade-mutation-id`` answers the selection question by hand."""
    workspace, epoch_id = _bootstrap(tmp_path, agent_dir=_inert_first_point_agent(tmp_path))
    report, _floor = _run_preflight(workspace, epoch_id, degrade_mutation_id="style_rules")

    assert [p.mutation_id for p in report.probed_points] == ["style_rules"]
    assert report.verdict == VERDICT_OK
    assert report.signal > 0.0

    # An id that does not enumerate fails the measurement loudly rather than
    # quietly measuring something the operator did not choose.
    import pytest

    with pytest.raises(ValueError, match="do not enumerate"):
        _run_preflight(workspace, epoch_id, degrade_mutation_id="not_a_point")


def test_probe_draws_use_distinct_reserved_cache_slots(tmp_path: Path) -> None:
    """Probe ``j`` draws at ``PREFLIGHT_REPLICATE_BASE + j``.

    One slot per probe: sharing a slot would make probe 2 a cache HIT on probe
    1's degraded tree and silently report the first probe's number twice. The
    slots stay inside the pre-flight's reserved range (below reflection's
    5000), so no tournament or audit evidence is touched.
    """
    from zicato import workspace_loader
    from zicato.epoch.preflight import PREFLIGHT_REPLICATE_SPAN
    from zicato.epoch.screen import SCREEN_REPLICATE_BASE
    from zicato.tournament.unit_cache import _unit_loss_path

    workspace, epoch_id = _bootstrap(tmp_path, agent_dir=_inert_first_point_agent(tmp_path))
    report, _floor = _run_preflight(workspace, epoch_id)
    n_probes = len(report.probed_points)
    assert n_probes == 2
    # The block the pre-flight owns must stop short of the screen's — squatting
    # a neighbour's range makes their idempotence a lie (dev-guide ch.04 §8.2).
    assert PREFLIGHT_REPLICATE_BASE + PREFLIGHT_REPLICATE_SPAN <= SCREEN_REPLICATE_BASE
    assert n_probes <= PREFLIGHT_REPLICATE_SPAN

    entry_id = workspace_loader.load_current_board(workspace)[0].id
    for ordinal in range(n_probes):
        # Cached under the CHAMPION's id — the degraded trees are ephemeral.
        assert _unit_loss_path(
            workspace,
            epoch_id,
            "v0",
            entry_id,
            PREFLIGHT_REPLICATE_BASE + ordinal,
        ).exists(), f"probe {ordinal} did not draw its own reserved cache slot"


# ---------------------------------------------------------------------------
# Issue #112 — the promote_margin window
# ---------------------------------------------------------------------------


def test_margin_above_the_degradation_signal_warns_but_never_refuses(
    tmp_path: Path,
) -> None:
    """A margin larger than the measured movement is worth saying — not enforcing.

    The contract out-signals its noise (verdict OK), and the margin sits above
    the only movement the probe demonstrated. That is a real thing to tell an
    operator, and it used to be a refusal. It no longer is: what the probe
    measured is DEGRADATION headroom — how far the scalar fell when a mutation
    point was destroyed — which does not bound how far a challenger can improve
    (issue #119). The finding stays; the enforcement goes.
    """
    weights = _scoring_from_dict(json.loads(SCORING_PATH.read_text()))
    # Above any scalar movement target_0's single mutation point can produce.
    weights = _replace(weights, promote_margin=99.0)
    workspace, epoch_id = _bootstrap(tmp_path, weights=weights)
    report, _floor = _run_preflight(workspace, epoch_id)

    assert report.verdict == VERDICT_OK, "the signal DOES clear the noise floor"
    assert report.signal > 0.0
    assert report.promote_margin == 99.0
    assert report.window_failure == "margin_above_achievable"
    assert report.window_verdict == VERDICT_WARN
    # And so the hard gate has nothing to act on: the signal verdict is OK.
    assert effective_gate_verdict(report.to_json()) == VERDICT_OK

    (finding,) = detect_preflight_verdict(report.to_json())
    assert finding.code == "preflight_margin_above_achievable"
    assert finding.severity == "warning"
    assert "degradation" in finding.summary, "the finding names what was measured"


def test_healthy_contract_reports_an_intact_window(tmp_path: Path) -> None:
    """target_0's shipped margin sits strictly inside the window."""
    workspace, epoch_id = _bootstrap(tmp_path)
    report, _floor = _run_preflight(workspace, epoch_id)

    assert report.window_verdict == VERDICT_OK
    assert report.window_failure is None
    assert report.noise_floor_max_abs_delta < report.promote_margin < report.signal
    assert detect_preflight_verdict(report.to_json()) == []


def test_saturated_board_reports_an_empty_window_not_a_mis_set_margin(
    tmp_path: Path,
) -> None:
    """``achievable <= noise`` means NO margin is defensible — say that.

    Issue #112 item 3: an operator told "your margin is mis-set" spends a
    cycle tuning a number that has no valid value on this board.
    """
    board = _no_expectation_board(tmp_path)
    workspace, epoch_id = _bootstrap(tmp_path, board_source=board)
    report, _floor = _run_preflight(workspace, epoch_id)

    assert report.signal == 0.0
    assert report.window_failure == "empty_window"
    # An empty window is NOT re-gated: the signal verdict already carries that
    # fact (here as saturation), so there is exactly one finding, not two.
    assert report.window_verdict != VERDICT_REFUSE
    (finding,) = detect_preflight_verdict(report.to_json())
    assert finding.code == "preflight_saturated_contract"


def test_recommended_margin_rides_along_on_the_record(tmp_path: Path) -> None:
    """The record carries a margin recommendation from the STABLE statistic.

    A deterministic harness measures no noise, so it recommends nothing rather
    than a meaningless 0.0; a noisy one recommends 2.5 sigma of ``delta_std``.
    """
    from zicato.tournament.calibration import MARGIN_NOISE_MULTIPLE

    workspace, epoch_id = _bootstrap(tmp_path)
    report, _floor = _run_preflight(workspace, epoch_id)
    assert report.recommended_margin is None, "no measured noise ⇒ nothing to recommend"

    noisy_workspace, noisy_epoch = _bootstrap(
        tmp_path / "noisy", adapter_block=_noisy_adapter(0.45)
    )
    noisy_report, noisy_floor = _run_preflight(noisy_workspace, noisy_epoch, runs=5)
    assert noisy_floor.delta_std > 0.0
    assert noisy_report.recommended_margin == MARGIN_NOISE_MULTIPLE * noisy_floor.delta_std


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_board_preflight_cli_measures_and_persists(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from zicato.cli.discovery import build_cli_root

    workspace, epoch_id = _bootstrap(tmp_path)
    _seed_baseline(workspace, epoch_id)

    runner = CliRunner()
    result = runner.invoke(
        build_cli_root(),
        [
            "board",
            "preflight",
            "--workspace",
            str(workspace),
            "--runs",
            "3",
            "--harness-call-llm",
            "zicato_examples.target_0_convergence.mocks:harness_llm",
            "--auxiliary-call-llm",
            "zicato_examples.target_0_convergence.mocks:aux_llm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Contract pre-flight" in result.output
    assert "degradation signal" in result.output
    assert "verdict:           OK" in result.output

    cfg = load_epoch(workspace, epoch_id)
    assert cfg.preflight is not None
    assert cfg.preflight["verdict"] == "ok"
    # The pre-flight's A/A draws double as the noise-floor measurement.
    assert cfg.noise_floor is not None
    assert cfg.noise_floor["max_abs_delta"] == 0.0


# ---------------------------------------------------------------------------
# Epoch-open hook (opt-in workspace knob, mirroring calibrate_noise_floor)
# ---------------------------------------------------------------------------


def test_epoch_open_hook_persists_verdict(tmp_path: Path) -> None:
    from zicato.evolve.loop import evolve_n_rounds

    workspace, epoch_id = _bootstrap(tmp_path, extra_config={"contract_preflight": 3})
    t0_mocks.reset()
    asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=t0_mocks.harness_llm,
            auxiliary_call_llm=t0_mocks.aux_llm,
            auto_epoch=False,
            fast_mode=True,
        )
    )

    cfg = load_epoch(workspace, epoch_id)
    assert cfg.preflight is not None
    assert cfg.preflight["verdict"] == "ok"
    assert cfg.preflight["noise_floor_runs"] == 3
    # Measured on the epoch's seed champion at epoch open.
    assert cfg.preflight["generation_id"] == "v0"
    # The shared A/A draws also persisted the noise floor.
    assert cfg.noise_floor is not None
    assert cfg.noise_floor["max_abs_delta"] == 0.0


def test_preflight_voids_on_infra_abort_instead_of_persisting_a_poisoned_floor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An endpoint outage during the epoch's first round must VOID the
    pre-flight (best-effort skip), not fold the aborted draws into a persisted
    noise floor / verdict.

    Regression for the issue-#84 review (Finding 1): the default-on pre-flight
    is a new consumer of champion A/A draws; without the ``is_infra_abort_cause``
    guard a transient outage poisons the epoch's floor and — under the hard
    gate — would falsely disqualify a contract that an outage merely made
    un-measurable. With the guard, ``run_contract_preflight`` raises
    :class:`NoiseFloorInconclusive` (the caller's ``best_effort`` turns it into
    a skip + re-measure next round); the default ``measure_noise_floor`` path
    (``raise_on_infra_abort=False`` — the ``board audit`` surface) is unchanged.
    """
    import pytest

    import zicato.tournament.runner as _runner_mod
    from zicato.core.types import DriftCount, LossProfile
    from zicato.tournament.calibration import NoiseFloorInconclusive, measure_noise_floor

    async def _infra_abort_run_single(
        *,
        adapter: object,
        generation: object,
        entry: object,
        weights: object,
        config: object,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, workspace_root, side, match_id
        return LossProfile(
            run_id=f"r-{generation.id}-{entry.id}",  # type: ignore[attr-defined]
            entry_id=entry.id,  # type: ignore[attr-defined]
            generation_id=generation.id,  # type: ignore[attr-defined]
            epoch_id=epoch_id,
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=1.0,
            runtime_ms=100,
            wall_clock_budget_exceeded=False,
            expectation_result=None,
            drift_loss=10.0,
            pass_fail=None,
            abort_cause="nonzero_exit:1",  # an is_infra_abort_cause class
        )

    monkeypatch.setattr(_runner_mod, "_run_single", _infra_abort_run_single)

    # The strict pre-flight consumer VOIDS the measurement rather than persist
    # an outage-derived floor.
    workspace, epoch_id = _bootstrap(tmp_path)
    with pytest.raises(NoiseFloorInconclusive):
        _run_preflight(workspace, epoch_id)

    # Backward-compat: the default calibration surface (board audit) still
    # tolerates aborts and returns a floor — the guard is opt-in.
    from zicato import adapter_factory, runtime_factory, workspace_loader

    champion = _seed_baseline(workspace, epoch_id)
    wc = workspace_loader.load_workspace_config(workspace)
    adapter = adapter_factory.make_adapter_from_config(wc)
    config = runtime_factory.make_runtime_config(
        wc,
        workspace_root=workspace,
        harness_call_llm=t0_mocks.harness_llm,
        auxiliary_call_llm=t0_mocks.aux_llm,
    )
    epoch_cfg = load_epoch(workspace, epoch_id)
    floor = asyncio.run(
        measure_noise_floor(
            adapter=adapter,
            generation=champion,  # type: ignore[arg-type]
            board=workspace_loader.load_current_board(workspace),
            weights=epoch_cfg.scoring,
            config=config,
            workspace_root=workspace,
            epoch_id=epoch_id,
            runs=3,
        )
    )
    assert floor.runs == 3  # tolerated (raise_on_infra_abort defaults False)


# ---------------------------------------------------------------------------
# Legibility — what the loop REPORTS while the measurement runs (issue #276)
# ---------------------------------------------------------------------------


class _RecordingBeater:
    """A :class:`HeartbeatBeater` stand-in recording the phases stamped on it."""

    def __init__(self) -> None:
        self.phases: list[str] = []

    def update(self, **fields: object) -> None:
        if fields.get("phase") is not None:
            self.phases.append(str(fields["phase"]))

    def bump_now(self) -> None:
        pass


class _HookConfig:
    """The RuntimeConfig surface the epoch-open hook reads."""

    def __init__(self, *, gate: str = "warn", probe_points: int = 2) -> None:
        self.preflight_gate = gate
        self.preflight_probe_points = probe_points
        self.preflight_probe_mutation_ids: tuple[str, ...] = ()


def _report(*, runs: int = 3, verdict: str = VERDICT_OK) -> object:
    from zicato.epoch.preflight import PreflightReport

    return PreflightReport(
        epoch_id="e0",
        generation_id="v0",
        verdict=verdict,
        noise_floor_max_abs_delta=0.0,
        noise_floor_runs=runs,
        champion_scalars=(1.0,) * runs,
        degraded_scalar=2.0,
        signal=1.0,
        degraded_mutation_id="style_rules",
        degraded_mutation_kind="span",
        degraded_file="agent/policy.py",
        measured_at="2026-08-17T00:00:00Z",
    )


def _preflight_hook(
    monkeypatch,
    *,
    workspace_config: dict,
    measured: object,
    persisted: object = None,
    config: object = None,
    board_size: int = 2,
    round_index: int = 4,
    beater: _RecordingBeater | None = None,
) -> _RecordingBeater:
    """Run the epoch-open pre-flight step against a stubbed measurement.

    ``measured`` is either the ``(report, floor)`` pair the fake measurement
    returns — reporting progress over its own units the way the real one does
    — or an exception it raises.
    """
    import zicato.epoch.lifecycle as lifecycle
    import zicato.epoch.preflight as preflight_mod
    from zicato.evolve.round_prepare import _maybe_contract_preflight
    from zicato.tournament.calibration import NoiseFloor

    floor = NoiseFloor(
        generation_id="v0",
        epoch_id="e0",
        runs=3,
        scalars=(1.0, 1.0, 1.0),
        max_abs_delta=0.0,
        delta_std=0.0,
        measured_at="2026-08-17T00:00:00Z",
    )

    async def _fake_preflight(*, runs: int, on_probe=None, **_kw: object) -> object:
        total = runs + 1
        if on_probe is not None:
            on_probe(0, total)
            for unit in range(1, total + 1):
                on_probe(unit, total)
        if isinstance(measured, Exception):
            raise measured
        return measured, floor

    monkeypatch.setattr(preflight_mod, "run_contract_preflight", _fake_preflight)
    monkeypatch.setattr(lifecycle, "set_epoch_preflight", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "set_epoch_noise_floor", lambda *a, **k: None)
    monkeypatch.setattr(
        lifecycle, "load_epoch", lambda *a, **k: type("_Cfg", (), {"noise_floor": None})()
    )

    beater = beater if beater is not None else _RecordingBeater()
    asyncio.run(
        _maybe_contract_preflight(
            workspace_root=Path("."),
            epoch_id="e0",
            epoch_cfg=type("_Cfg", (), {"preflight": persisted})(),
            workspace_config=workspace_config,
            adapter=None,
            parent_gen=None,  # type: ignore[arg-type]
            board=[None] * board_size,
            weights=None,
            config=config if config is not None else _HookConfig(),
            disable_drift=(),
            judge_only=False,
            beater=beater,  # type: ignore[arg-type]
            round_index=round_index,
        )
    )
    return beater


def test_preflight_owns_the_phase_and_counts_its_units(monkeypatch) -> None:
    """The measurement stamps its OWN phase, counting every A/A draw and every
    degraded probe, then hands the round back its phase — a working pre-flight
    used to be indistinguishable from a round that had hung."""
    beater = _preflight_hook(
        monkeypatch,
        workspace_config={"contract_preflight": 3},
        measured=_report(),
    )
    assert beater.phases == [
        "evolve_once:contract_preflight",
        "evolve_once:contract_preflight:0/4",
        "evolve_once:contract_preflight:1/4",
        "evolve_once:contract_preflight:2/4",
        "evolve_once:contract_preflight:3/4",
        "evolve_once:contract_preflight:4/4",
        "evolve_once:round_4",
    ]


def test_a_preflighting_phase_reads_as_active_work() -> None:
    """No segment of the pre-flight phase is an at-rest token, so a workspace
    mid-measurement reads ACTIVE rather than settled."""
    from zicato.epoch.preflight import PREFLIGHT_PHASE
    from zicato.query.runtime_view import is_active_phase

    assert is_active_phase(PREFLIGHT_PHASE)
    assert is_active_phase(f"{PREFLIGHT_PHASE}:4/6")


def test_a_failed_preflight_still_hands_the_round_its_phase_back(monkeypatch) -> None:
    """The measurement is best-effort: a failure must not leave the heartbeat
    parked on a measurement that is no longer running."""
    beater = _preflight_hook(
        monkeypatch,
        workspace_config={"contract_preflight": 2},
        measured=RuntimeError("endpoint outage"),
    )
    assert beater.phases[0] == "evolve_once:contract_preflight"
    assert beater.phases[-1] == "evolve_once:round_4"


def test_a_refused_run_still_hands_the_round_its_phase_back(monkeypatch) -> None:
    """The one failure that ESCAPES the best-effort contract — a probe-config
    error under the hard gate — leaves through the same ``finally``."""
    import pytest

    from zicato.epoch.preflight import PreflightConfigError, PreflightRefusedError

    beater = _RecordingBeater()
    with pytest.raises(PreflightRefusedError):
        _preflight_hook(
            monkeypatch,
            workspace_config={"contract_preflight": 2},
            measured=PreflightConfigError("no such mutation point"),
            config=_HookConfig(gate="refuse"),
            beater=beater,
        )
    assert beater.phases[0] == "evolve_once:contract_preflight"
    assert beater.phases[-1] == "evolve_once:round_4"


def test_a_skipped_preflight_touches_no_phase(monkeypatch) -> None:
    """Opted out, misconfigured, or already measured: the round's own phase
    stands untouched — byte-identical to the behaviour before the step
    reported itself."""
    for workspace_config, persisted, config in (
        ({}, None, _HookConfig(gate="off")),
        ({"contract_preflight": "three"}, None, None),
        ({"contract_preflight": 1}, None, None),
        ({"contract_preflight": 3}, {"verdict": "ok"}, None),
    ):
        beater = _preflight_hook(
            monkeypatch,
            workspace_config=workspace_config,
            measured=_report(),
            persisted=persisted,
            config=config,
        )
        assert beater.phases == [], (workspace_config, persisted)


def test_the_preflight_cost_is_named_before_the_first_draw(monkeypatch, caplog) -> None:
    """K A/A draws + up to P probes x N board entries is knowable up front; the
    operator should not have to infer the shape from probe draws on disk."""
    import logging

    with caplog.at_level(logging.INFO, logger="zicato.orchestrator"):
        _preflight_hook(
            monkeypatch,
            workspace_config={"contract_preflight": 3},
            measured=_report(),
            config=_HookConfig(probe_points=2),
            board_size=6,
        )
    cost = next(m for m in caplog.messages if "board-entry runs" in m)
    assert "3 A/A draw(s) + up to 2 degraded probe(s) x 6 board entries" in cost
    assert "up to 30 board-entry runs" in cost
    assert "serially" in cost


def test_progress_counts_every_a_a_draw_and_every_probe(tmp_path: Path) -> None:
    """Against the real probe loop: the count covers BOTH measurement stages —
    K A/A draws then the degraded probes — because each is one pass over the
    board, and the total is fixed before the first draw is spent."""
    workspace, epoch_id = _bootstrap(tmp_path)
    progress: list[tuple[int, int]] = []
    report, _floor = _run_preflight(
        workspace,
        epoch_id,
        runs=3,
        on_probe=lambda done, total: progress.append((done, total)),
    )
    # target_0 enumerates exactly one mutation point, so the pre-flight's units
    # are 3 A/A draws + 1 probe, each reported once as it settles.
    assert report.drawn_probe_count() == 1
    assert progress == [(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]
