"""The adapter post-promotion hook (issue #125) — ``on_promote``.

A target whose evolved state lives outside the mutable tree needs to be
told when a generation became champion. These tests cover the four
properties that makes it a contract rather than a callback:

* **every tournament structure uses the shared promotion seam**;
* **it fires at most once per settled promotion**, and never for a
  rejected round; canonical settlement replay does not repeat the side effect;
* **it is best-effort**: a hook that raises or hangs leaves the
  promotion standing and the round intact;
* **a failure is observable** — an ERROR log plus an
  ``on_promote_hook_failed`` WARNING in the round's loop-health report.

The end-to-end rounds reuse the hermetic harness from
``tests/test_orchestrator.py`` (stub adapter, stub telemetry, scripted
proposer), swapping in an adapter that carries a recording hook.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tests._orchestrator_harness import (
    bootstrap_workspace,
    harness_call_llm,
    install_telemetry_stubs,
    make_aux_responder,
    run_evolve_once,
)
from tests.test_orchestrator_multi_challenger import (
    _bootstrap_swiss_workspace,
)
from zicato.evolve.promote_hook import ON_PROMOTE_TIMEOUT_SECONDS, fire_on_promote
from zicato.evolve.settlement_recovery import field_settlement_intent_path
from zicato.health.diagnostics import assess_loop_health, detect_on_promote_hook_failed

# ---------------------------------------------------------------------------
# Harness: a stub adapter that carries an on_promote hook
# ---------------------------------------------------------------------------


def _install_hooked_adapter_factory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_promote: Any = None,
) -> list[dict[str, Any]]:
    """Install the stub adapter factory, with an ``on_promote`` hook.

    Mirrors ``tests.test_orchestrator._install_stub_adapter_factory`` — the
    same do-nothing session and mutation surface — but the adapter declares
    the optional hook. Every call is appended to the returned list before
    ``on_promote`` (when given) is awaited, so a raising hook is still
    recorded as a call.
    """
    calls: list[dict[str, Any]] = []

    class _StubSession:
        async def run(self, entry: Any, sinks: list[Any], config: Any) -> Any:
            del sinks, config
            from zicato.core import RunResult

            return RunResult(
                run_id=f"r-{entry.id}",
                entry_id=entry.id,
                final_output="hello world",
                transcript=("hello world",),
                runtime_ms=100,
            )

    class _HookedAdapter:
        name = "hooked-stub"

        def load(self, snapshot_root: Path) -> _StubSession:
            del snapshot_root
            return _StubSession()

        def mutation_points(self, source_roots: list[Path] | None = None) -> list[Any]:
            del source_roots
            return []

        async def on_promote(self, **context: Any) -> None:
            calls.append(context)
            if on_promote is not None:
                await on_promote(**context)

    fake_factory = types.ModuleType("zicato.adapter_factory")

    def make_adapter_from_config(workspace_config: dict[str, Any]) -> Any:
        del workspace_config
        return _HookedAdapter()

    fake_factory.make_adapter_from_config = make_adapter_from_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zicato.adapter_factory", fake_factory)
    import zicato
    import zicato.check

    monkeypatch.setattr(zicato, "adapter_factory", fake_factory, raising=False)
    monkeypatch.setattr(zicato.check, "require_workspace_valid", lambda *a, **k: None)
    return calls


def _health_report(workspace: Path, epoch_id: str, round_n: int) -> dict[str, Any]:
    path = workspace / "epochs" / epoch_id / "health" / f"round_{round_n}.json"
    assert path.is_file(), f"no health report at {path}"
    body: dict[str, Any] = json.loads(path.read_text())
    return body


def _findings(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [f for f in report.get("findings", []) if f.get("code") == code]


def _settlement_receipt(workspace: Path, epoch_id: str) -> dict[str, Any]:
    """Read the first round's retained field-settlement receipt."""
    return json.loads(
        field_settlement_intent_path(workspace, epoch_id, 0).read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# fire_on_promote — the unit contract
# ---------------------------------------------------------------------------


async def test_hookless_adapter_is_a_no_op(tmp_path: Path) -> None:
    """An adapter predating #125 declares no hook and is simply skipped."""

    class _Hookless:
        name = "hookless"

    failure = await fire_on_promote(
        _Hookless(),
        workspace_root=tmp_path,
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        snapshot_root=tmp_path / "snap",
    )
    assert failure is None


async def test_hook_receives_the_full_promotion_context(tmp_path: Path) -> None:
    """The hook is called with the keyword-only promotion context."""
    seen: list[dict[str, Any]] = []

    class _Hooked:
        name = "hooked"

        async def on_promote(self, **context: Any) -> None:
            seen.append(context)

    failure = await fire_on_promote(
        _Hooked(),
        workspace_root=tmp_path,
        epoch_id="e1",
        generation_id="v3",
        parent_generation_id="v2",
        snapshot_root=tmp_path / "snap",
    )

    assert failure is None
    assert seen == [
        {
            "epoch_id": "e1",
            "generation_id": "v3",
            "parent_generation_id": "v2",
            "snapshot_root": tmp_path / "snap",
            "workspace_root": tmp_path,
        }
    ]


async def test_a_raising_hook_is_swallowed_and_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A hook that raises never propagates; it yields the failure payload."""

    class _Exploding:
        name = "exploding"

        async def on_promote(self, **context: Any) -> None:
            del context
            raise RuntimeError("the external store rejected the write")

    with caplog.at_level(logging.ERROR, logger="zicato.orchestrator"):
        failure = await fire_on_promote(
            _Exploding(),
            workspace_root=tmp_path,
            epoch_id="e1",
            generation_id="v1",
            parent_generation_id="v0",
            snapshot_root=tmp_path / "snap",
        )

    assert failure == ("exploding", "v1", "RuntimeError")
    # The operator sees the exception itself, not just the finding.
    assert any(
        record.levelno == logging.ERROR and record.exc_info is not None for record in caplog.records
    )


async def test_a_hanging_hook_times_out_rather_than_stalling_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook that never returns is cancelled at the timeout and counts as failed."""

    class _Hanging:
        name = "hanging"

        async def on_promote(self, **context: Any) -> None:
            del context
            await asyncio.sleep(3600)

    monkeypatch.setattr("zicato.evolve.promote_hook.ON_PROMOTE_TIMEOUT_SECONDS", 0.05)

    failure = await fire_on_promote(
        _Hanging(),
        workspace_root=tmp_path,
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        snapshot_root=tmp_path / "snap",
    )

    assert failure == ("hanging", "v1", "TimeoutError")


def test_the_hook_timeout_is_generous_but_finite() -> None:
    """The ceiling exists so a hung hook cannot stall the evolve loop."""
    assert 0 < ON_PROMOTE_TIMEOUT_SECONDS <= 600


async def test_operator_cancellation_is_not_swallowed(tmp_path: Path) -> None:
    """A CancelledError from the run's own shutdown keeps propagating.

    ``fire_on_promote`` swallows ``Exception``, deliberately not
    ``BaseException``: an operator stopping the run must not be recorded
    as a hook failure and must not be absorbed here.
    """

    class _Cancelled:
        name = "cancelled"

        async def on_promote(self, **context: Any) -> None:
            del context
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await fire_on_promote(
            _Cancelled(),
            workspace_root=tmp_path,
            epoch_id="e1",
            generation_id="v1",
            parent_generation_id="v0",
            snapshot_root=tmp_path / "snap",
        )


# ---------------------------------------------------------------------------
# The health finding
# ---------------------------------------------------------------------------


def test_no_failure_raises_no_finding() -> None:
    """Every round with no hook, and every successful hook, is silent."""
    assert detect_on_promote_hook_failed(None) == []


def test_the_finding_names_the_adapter_generation_and_exception() -> None:
    """The WARNING carries enough to act on without reading the log."""
    (finding,) = detect_on_promote_hook_failed(("mystore", "v7", "ConnectionError"))
    assert finding.code == "on_promote_hook_failed"
    assert finding.severity == "warning"
    assert "mystore" in finding.summary
    assert "v7" in finding.summary
    assert "ConnectionError" in finding.summary
    assert finding.detail["adapter"] == "mystore"
    assert finding.detail["generation_id"] == "v7"
    assert finding.detail["exception_type"] == "ConnectionError"
    assert finding.detail["timed_out"] is False
    # The promotion stands; reconciliation is the operator's job.
    assert "manual" in finding.detail["recommendation"].lower()


def test_a_timed_out_hook_is_flagged_as_such() -> None:
    """The timeout case is distinguishable from an ordinary raise."""
    (finding,) = detect_on_promote_hook_failed(("mystore", "v7", "TimeoutError"))
    assert finding.detail["timed_out"] is True


def test_the_finding_makes_the_loop_unhealthy() -> None:
    """A WARNING flips ``healthy`` — the loop report must not read clean."""
    health = assess_loop_health(
        {},
        [],
        [],
        "epoch-1",
        on_promote_failure=("mystore", "v7", "RuntimeError"),
    )
    assert not health.healthy
    assert [f.code for f in health.findings] == ["on_promote_hook_failed"]


# ---------------------------------------------------------------------------
# End-to-end: each tournament shape uses the shared promotion seam
# ---------------------------------------------------------------------------


def test_gauntlet_promotion_fires_the_hook_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gauntlet's champion-marker advance notifies the adapter."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    calls = _install_hooked_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    assert outcome.tournament_decision == "promoted"
    assert len(calls) == 1, calls
    (context,) = calls
    assert context["epoch_id"] == epoch_id
    assert context["generation_id"] == "v1"
    assert context["parent_generation_id"] == "v0"
    assert context["workspace_root"] == workspace
    # The snapshot handed over is the promoted generation's own tree, and
    # it is realized on disk by the time the hook can read it.
    assert context["snapshot_root"].is_dir()
    assert (context["snapshot_root"] / "agent.py").is_file()
    # It fires only after the promotion is durable: the marker already
    # names the generation the hook was told about.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v1"


def test_a_rejected_round_never_fires_the_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No promotion, no notification — the champion did not move."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    calls = _install_hooked_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 0.0, "v1": 5.0},
        canned_pass_by_gen={"v0": True, "v1": False},
    )

    outcome = run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    assert outcome.tournament_decision == "rejected"
    assert calls == []
    assert _settlement_receipt(workspace, epoch_id)["promotion_hook"]["state"] == "not_applicable"


def test_multi_challenger_crowning_fires_the_hook_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A multi-challenger settlement notifies the adapter too.

    A settled field crowns one primary head. That head is the only generation
    passed to the hook, even if the round applied several challengers.
    """
    workspace, epoch_id = _bootstrap_swiss_workspace(tmp_path, field_size=2, rounds_n=1)
    calls = _install_hooked_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 0.5, "v2": 1.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    outcome = run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    assert outcome.tournament_decision == "promoted"
    assert len(calls) == 1, calls
    (context,) = calls
    crowned = outcome.proposed_generation_id
    assert context["generation_id"] == crowned
    assert context["parent_generation_id"] == "v0"
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == crowned
    hook_delivery = _settlement_receipt(workspace, epoch_id)["promotion_hook"]
    assert hook_delivery == {
        "state": "succeeded",
        "adapter_name": "hooked-stub",
        "failure_type": "",
    }


# ---------------------------------------------------------------------------
# End-to-end: best-effort failure semantics
# ---------------------------------------------------------------------------


def test_a_failing_hook_leaves_the_promotion_standing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A raising hook never un-promotes the generation or fails the round.

    The champion marker has already advanced and the outcome is already
    durable when the hook runs, so the only honest thing a failure can do
    is be reported — which it is, as an ``on_promote_hook_failed`` WARNING
    in the round's loop-health report.
    """
    workspace, epoch_id = bootstrap_workspace(tmp_path)

    async def _explode(**context: Any) -> None:
        del context
        raise ConnectionError("the external store is unreachable")

    calls = _install_hooked_adapter_factory(monkeypatch, on_promote=_explode)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    outcome = run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    # The round settled normally and the promotion is intact on every store.
    assert len(calls) == 1
    assert outcome.tournament_decision == "promoted"
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == "v1"
    body = json.loads(
        (workspace / "epochs" / epoch_id / "generations" / "v1" / "experiment.json").read_text()
    )
    assert body["outcome"]["tournament_decision"] == "promoted"

    # ...and the un-committed side effect is visible in the health report.
    report = _health_report(workspace, epoch_id, 1)
    (finding,) = _findings(report, "on_promote_hook_failed")
    assert finding["severity"] == "warning"
    assert finding["detail"]["adapter"] == "hooked-stub"
    assert finding["detail"]["generation_id"] == "v1"
    assert finding["detail"]["exception_type"] == "ConnectionError"
    assert report["healthy"] is False
    hook_delivery = _settlement_receipt(workspace, epoch_id)["promotion_hook"]
    assert hook_delivery == {
        "state": "failed",
        "adapter_name": "hooked-stub",
        "failure_type": "ConnectionError",
    }


def test_a_successful_hook_raises_no_finding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The finding is a failure signal, not a promotion log line."""
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    _install_hooked_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )

    run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    assert _findings(_health_report(workspace, epoch_id, 1), "on_promote_hook_failed") == []
    assert _settlement_receipt(workspace, epoch_id)["promotion_hook"]["state"] == "succeeded"


# ---------------------------------------------------------------------------
# At-most-once across a crash-resume
# ---------------------------------------------------------------------------


def test_resume_never_re_fires_a_settled_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A promoted generation is not re-notified when the loop restarts.

    Settlement recovery does not replay adapter side effects. A restart can
    therefore complete canonical records without repeating a hook that already
    ran, and the next round moves past v1.
    """
    workspace, epoch_id = bootstrap_workspace(tmp_path)
    calls = _install_hooked_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        # Round 1 promotes v1; round 2's v2 is worse than the new champion
        # and is rejected, so the ONLY promotion in the run is v1's.
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0, "v2": 9.0},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": False},
    )

    first = run_evolve_once(workspace, epoch_id, make_aux_responder([]))
    assert first.tournament_decision == "promoted"
    assert [c["generation_id"] for c in calls] == ["v1"]

    # The settled promotion is nothing to resume: the reconciliation the
    # restarted loop runs classifies the workspace clean and re-enters no
    # generation, so nothing can replay v1's promote tail.
    from zicato.runtime.resume import prepare_resume

    plan = prepare_resume(workspace, epoch_id)
    assert plan.classification == "clean"
    assert plan.resume_generation_id is None
    assert plan.discarded_generation_id is None

    # Drive the restart for real: `evolve_n_rounds` runs `prepare_resume`
    # before its round loop.
    from zicato.orchestrator import evolve_n_rounds

    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=harness_call_llm,
            auxiliary_call_llm=make_aux_responder([]),
        )
    )

    assert [o.proposed_generation_id for o in outcomes] == ["v2"]
    assert outcomes[0].tournament_decision == "rejected"
    # v1 was notified exactly once, in round 1 — the restart neither
    # repeated it nor fired for the rejected v2.
    assert [c["generation_id"] for c in calls] == ["v1"]
