"""Tests for the control-protocol CONSUMER (RUNTIME-V2.md Phase 2).

The dashboard has long *produced* control commands under
``.zicato/runtime/control/`` while nothing consumed them. These tests cover
the now-wired consumer at every safe point:

* unit — :mod:`zicato.runtime.control_consumer`: a command present →
  claimed → acted on → archived in ``control_log/``;
* integration — the real ``evolve_once`` path: a ``promote``/``reject``
  override flips the gate verdict and is journaled as an EXPLICIT operator
  override (never silent); a ``skip_round`` aborts the round cleanly; an
  override aimed at a different generation does NOT fire;
* loop — ``evolve_n_rounds`` (mock ``evolve_once``): ``pause_epoch`` blocks
  scheduling until cleared, and ``rubric_replacement`` rolls the epoch.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import zicato.orchestrator as orch
from tests.test_orchestrator import (
    _bootstrap_workspace,
    _harness_call_llm,
    _install_stub_adapter_factory,
    _install_telemetry_stubs,
    _make_aux_responder,
    _valid_proposer_response,
)
from zicato.runtime.control import (
    CMD_PAUSE_EPOCH,
    CMD_PROMOTE_PREFIX,
    CMD_REJECT_PREFIX,
    CMD_RUBRIC_REPLACEMENT,
    CMD_SKIP_ROUND,
    ControlCommand,
    list_pending_commands,
    write_command,
)
from zicato.runtime.control_consumer import (
    CONSUMER_SOURCE,
    block_while_paused,
    claim_gate_override,
    claim_rubric_replacement,
    claim_skip_round,
    drain_stale_gate_overrides,
)
from zicato.runtime.paths import control_log_dir

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _archived(workspace_root: Path) -> list[dict[str, Any]]:
    """Return every control_log audit record (the claim-once archive)."""
    out: list[dict[str, Any]] = []
    log_dir = control_log_dir(workspace_root)
    if not log_dir.exists():
        return out
    for f in sorted(log_dir.iterdir()):
        if f.suffix == ".json":
            out.append(json.loads(f.read_text()))
    return out


def _dashboard_flag_payload(reason: str) -> str:
    """The JSON body the dashboard writes into a pause/skip flag file."""
    return json.dumps({"reason": reason, "ts": "2026-06-10T00:00:00Z"})


# ---------------------------------------------------------------------------
# Unit — skip_round
# ---------------------------------------------------------------------------


def test_claim_skip_round_absent_returns_none(tmp_path: Path) -> None:
    assert claim_skip_round(tmp_path) is None


def test_claim_skip_round_consumes_and_archives(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_SKIP_ROUND))
    reason = claim_skip_round(tmp_path)
    assert reason == ""  # a bare flag has no reason
    # The flag is consumed (no longer pending) and archived.
    assert list_pending_commands(tmp_path) == []
    archived = _archived(tmp_path)
    assert len(archived) == 1
    assert archived[0]["command"] == CMD_SKIP_ROUND
    assert archived[0]["source"] == CONSUMER_SOURCE


def test_claim_skip_round_reads_dashboard_reason(tmp_path: Path) -> None:
    """The dashboard writes a JSON flag body with a reason; the consumer reads it."""
    from zicato.runtime.control import control_dir
    from zicato.runtime.paths import ensure_runtime_dirs

    # Mirror the real producer: the dashboard writes the flag file with a
    # ``{"reason": ..., "ts": ...}`` JSON body (not the empty flag body
    # ``write_command`` writes for a hand-queued flag).
    ensure_runtime_dirs(tmp_path)
    (control_dir(tmp_path) / CMD_SKIP_ROUND).write_text(_dashboard_flag_payload("boring round"))

    reason = claim_skip_round(tmp_path)
    assert reason == "boring round"
    # And the operator's reason rode into the audit log.
    assert _archived(tmp_path)[0]["reason"] == "boring round"


# ---------------------------------------------------------------------------
# Unit — gate override (promote / reject)
# ---------------------------------------------------------------------------


def test_claim_gate_override_absent_returns_none(tmp_path: Path) -> None:
    assert claim_gate_override(tmp_path, "v1") is None


def test_claim_gate_override_promote(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_PROMOTE_PREFIX, arg="v1"))
    override = claim_gate_override(tmp_path, "v1")
    assert override is not None
    assert override.decision == "promoted"
    assert override.generation_id == "v1"
    # Consumed + archived.
    assert list_pending_commands(tmp_path) == []
    assert _archived(tmp_path)[0]["command"] == CMD_PROMOTE_PREFIX


def test_claim_gate_override_reject(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_REJECT_PREFIX, arg="v2"))
    override = claim_gate_override(tmp_path, "v2")
    assert override is not None
    assert override.decision == "rejected"


def test_claim_gate_override_ignores_other_generation(tmp_path: Path) -> None:
    """An override aimed at a DIFFERENT generation is left pending, not fired."""
    write_command(tmp_path, ControlCommand(name=CMD_PROMOTE_PREFIX, arg="v9"))
    assert claim_gate_override(tmp_path, "v1") is None
    # Still pending — it must not mis-fire on the wrong round.
    pending = list_pending_commands(tmp_path)
    assert [(c.name, c.arg) for c in pending] == [(CMD_PROMOTE_PREFIX, "v9")]


def test_claim_gate_override_promote_wins_and_drains_reject(tmp_path: Path) -> None:
    """Promote+reject for the same gen: promote wins, reject is drained."""
    write_command(tmp_path, ControlCommand(name=CMD_PROMOTE_PREFIX, arg="v1"))
    write_command(tmp_path, ControlCommand(name=CMD_REJECT_PREFIX, arg="v1"))
    override = claim_gate_override(tmp_path, "v1")
    assert override is not None
    assert override.decision == "promoted"
    # Both commands are gone — the reject cannot re-fire on a later round.
    assert list_pending_commands(tmp_path) == []
    archived = {r["command"] for r in _archived(tmp_path)}
    assert archived == {CMD_PROMOTE_PREFIX, CMD_REJECT_PREFIX}


# ---------------------------------------------------------------------------
# Unit — drain stale gate overrides on an epoch roll
# ---------------------------------------------------------------------------


def test_drain_stale_gate_overrides_archives_promote_and_reject(tmp_path: Path) -> None:
    write_command(tmp_path, ControlCommand(name=CMD_PROMOTE_PREFIX, arg="v1"))
    write_command(tmp_path, ControlCommand(name=CMD_REJECT_PREFIX, arg="v2"))
    drained = drain_stale_gate_overrides(tmp_path, reason="superseded by epoch roll e0 -> e1")
    assert sorted(drained) == ["v1", "v2"]
    # Both are gone from pending and archived (never silently deleted).
    assert list_pending_commands(tmp_path) == []
    archived = _archived(tmp_path)
    assert {r["command"] for r in archived} == {CMD_PROMOTE_PREFIX, CMD_REJECT_PREFIX}
    assert all(r["reason"] == "superseded by epoch roll e0 -> e1" for r in archived)
    assert all(r["source"] == CONSUMER_SOURCE for r in archived)


def test_drain_stale_gate_overrides_leaves_flags_and_rubric(tmp_path: Path) -> None:
    """Only promote/reject drain; a pause flag and a rubric payload survive a roll."""
    write_command(tmp_path, ControlCommand(name=CMD_PROMOTE_PREFIX, arg="v3"))
    write_command(tmp_path, ControlCommand(name=CMD_PAUSE_EPOCH))
    write_command(tmp_path, ControlCommand(name=CMD_RUBRIC_REPLACEMENT, payload="new brief"))
    drained = drain_stale_gate_overrides(tmp_path, reason="roll")
    assert drained == ["v3"]
    remaining = {(c.name, c.arg) for c in list_pending_commands(tmp_path)}
    assert remaining == {(CMD_PAUSE_EPOCH, ""), (CMD_RUBRIC_REPLACEMENT, "")}


def test_drained_override_cannot_misfire_on_a_reused_generation_id(tmp_path: Path) -> None:
    """F4 regression: an override surviving a roll must not fire on the new epoch's vN.

    A promote/v3 queued in the closed epoch is drained at the roll; the new
    epoch's own v3 then gates without inheriting the stale override.
    """
    write_command(tmp_path, ControlCommand(name=CMD_PROMOTE_PREFIX, arg="v3"))
    drain_stale_gate_overrides(tmp_path, reason="superseded by epoch roll e0 -> e1")
    # New epoch's v3 reaches its gate — no stale override is claimed.
    assert claim_gate_override(tmp_path, "v3") is None


# ---------------------------------------------------------------------------
# Unit — rubric replacement
# ---------------------------------------------------------------------------


def test_claim_rubric_replacement_absent_returns_none(tmp_path: Path) -> None:
    assert claim_rubric_replacement(tmp_path) is None


def test_claim_rubric_replacement_carries_payload_and_archives(tmp_path: Path) -> None:
    write_command(
        tmp_path,
        ControlCommand(name=CMD_RUBRIC_REPLACEMENT, payload="# New brief\n- focus\n"),
    )
    rubric = claim_rubric_replacement(tmp_path)
    assert rubric is not None
    assert rubric.payload == "# New brief\n- focus\n"
    assert list_pending_commands(tmp_path) == []
    archived = _archived(tmp_path)
    assert archived[0]["command"] == CMD_RUBRIC_REPLACEMENT
    # The audit log preserves the new brief text verbatim.
    assert archived[0]["payload"] == "# New brief\n- focus\n"


# ---------------------------------------------------------------------------
# Unit — pause (block while present, archive on release)
# ---------------------------------------------------------------------------


def test_block_while_paused_absent_is_zero_polls(tmp_path: Path) -> None:
    assert block_while_paused(tmp_path) == 0
    # Nothing archived — there was no pause to honour.
    assert _archived(tmp_path) == []


def test_block_while_paused_blocks_then_clears(tmp_path: Path) -> None:
    """Block while the flag is present; clear it via the injected sleep."""
    write_command(tmp_path, ControlCommand(name=CMD_PAUSE_EPOCH))
    from zicato.runtime.control import control_dir  # local import for the path

    flag = control_dir(tmp_path) / CMD_PAUSE_EPOCH

    cleared_after = 3
    state = {"polls": 0}

    def _fake_sleep(_seconds: float) -> None:
        state["polls"] += 1
        if state["polls"] >= cleared_after:
            # Operator resumes — remove the flag.
            flag.unlink()

    polls = block_while_paused(tmp_path, sleep=_fake_sleep, max_polls=10)
    assert polls == cleared_after
    # The pause episode is archived once on release.
    archived = _archived(tmp_path)
    assert len(archived) == 1
    assert archived[0]["command"] == CMD_PAUSE_EPOCH
    assert archived[0]["source"] == CONSUMER_SOURCE


def test_block_while_paused_respects_max_polls(tmp_path: Path) -> None:
    """A never-cleared pause stops at ``max_polls`` (the test safety cap)."""
    write_command(tmp_path, ControlCommand(name=CMD_PAUSE_EPOCH))
    polls = block_while_paused(tmp_path, sleep=lambda _s: None, max_polls=4)
    assert polls == 4


# ---------------------------------------------------------------------------
# Integration — gate override flips the real evolve_once decision
# ---------------------------------------------------------------------------


def test_reject_override_flips_a_would_promote_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child that would PROMOTE is force-REJECTED by an operator override.

    The override is recorded as an explicit operator override in the
    OutcomeRecord / journal — never silently.
    """
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    # Operator queues a reject for the generation this round will mint (v1).
    write_command(workspace, ControlCommand(name=CMD_REJECT_PREFIX, arg="v1"))

    outcome = asyncio.run(
        orch.evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )
    # The gate would have promoted (child scalar < parent), but the override
    # rejected it.
    assert outcome.tournament_decision == "rejected"
    # The current_generation marker was NOT bumped — v1 was overridden out.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert not marker.exists()

    # Journal/experiment record carries the explicit operator override.
    body = json.loads(
        (workspace / "epochs" / epoch_id / "generations" / "v1" / "experiment.json").read_text()
    )
    out = body["outcome"]
    assert out["tournament_decision"] == "rejected"
    assert out["operator_override"] is True
    assert out["operator_override_reason"]  # non-empty
    assert "operator override" in out["rejection_reason"]

    # The command was claimed + archived in control_log/.
    assert list_pending_commands(workspace) == []
    archived = _archived(workspace)
    assert any(r["command"] == CMD_REJECT_PREFIX and r["arg"] == "v1" for r in archived)


def test_promote_override_flips_a_would_reject_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child that would REJECT is force-PROMOTED by an operator override."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 0.0, "v1": 5.0},
        canned_pass_by_gen={"v0": True, "v1": False},
    )
    write_command(workspace, ControlCommand(name=CMD_PROMOTE_PREFIX, arg="v1"))

    outcome = asyncio.run(
        orch.evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )
    # The gate would have rejected (child regressed), but the override promoted.
    assert outcome.tournament_decision == "promoted"
    # The current_generation marker WAS bumped to v1.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.exists()
    assert marker.read_text().strip() == "v1"

    body = json.loads(
        (workspace / "epochs" / epoch_id / "generations" / "v1" / "experiment.json").read_text()
    )
    out = body["outcome"]
    assert out["tournament_decision"] == "promoted"
    assert out["operator_override"] is True
    # A forced promotion clears the rejection reason.
    assert out["rejection_reason"] == ""


def test_override_for_other_generation_does_not_fire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An override aimed at a different gen leaves the real gate untouched."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    # Override targets v7 — not the v1 this round mints.
    write_command(workspace, ControlCommand(name=CMD_REJECT_PREFIX, arg="v7"))

    outcome = asyncio.run(
        orch.evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([_valid_proposer_response()]),
        )
    )
    # The gate's own verdict (promote) stands.
    assert outcome.tournament_decision == "promoted"
    body = json.loads(
        (workspace / "epochs" / epoch_id / "generations" / "v1" / "experiment.json").read_text()
    )
    assert body["outcome"]["operator_override"] is False
    # The stale override is still pending — it never mis-fired on v1.
    pending = list_pending_commands(workspace)
    assert [(c.name, c.arg) for c in pending] == [(CMD_REJECT_PREFIX, "v7")]


# ---------------------------------------------------------------------------
# Integration — skip_round aborts the round cleanly
# ---------------------------------------------------------------------------


def test_skip_round_aborts_evolve_once_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A queued skip_round aborts the round before any propose/tournament work."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)
    _install_stub_adapter_factory(monkeypatch)
    _install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0},
        canned_pass_by_gen={"v0": True, "v1": True},
    )
    write_command(workspace, ControlCommand(name=CMD_SKIP_ROUND))

    # The proposer responder would raise on a SECOND call; a clean skip never
    # proposes, so it is never consulted — a strong signal nothing ran.
    outcome = asyncio.run(
        orch.evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder([]),
            round_index=2,
        )
    )
    assert outcome.tournament_decision == "rejected"
    assert outcome.rejection_reason.startswith("skip_round")
    assert outcome.proposed_generation_id == ""
    # No v1 generation was minted — the round was skipped before proposing.
    assert not (workspace / "epochs" / epoch_id / "generations" / "v1").exists()
    # The flag was consumed + archived.
    assert list_pending_commands(workspace) == []
    assert any(r["command"] == CMD_SKIP_ROUND for r in _archived(workspace))


# ---------------------------------------------------------------------------
# Loop — pause blocks scheduling; rubric rolls the epoch
# ---------------------------------------------------------------------------


async def _aux_call_llm(system: str, user: str, model: str) -> str:
    return "aux-output"


def _mock_outcome(round_idx: int) -> orch.EvolveRoundOutcome:
    return orch.EvolveRoundOutcome(
        parent_generation_id=f"v{round_idx}",
        proposed_generation_id=f"v{round_idx + 1}",
        tournament_decision="promoted",
        rejection_reason="",
        parent_scalar=1.0,
        child_scalar=0.5,
        delta_scalar=-0.5,
    )


def test_pause_blocks_then_resumes_between_rounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pause flag present at the top of the loop blocks until it clears."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)

    calls: list[int] = []

    async def _mock_evolve_once(*, round_index: int = 0, **_kwargs: Any) -> orch.EvolveRoundOutcome:
        calls.append(round_index)
        return _mock_outcome(round_index)

    monkeypatch.setattr(orch, "evolve_once", _mock_evolve_once)

    # Pause is active before the loop starts.
    write_command(workspace, ControlCommand(name=CMD_PAUSE_EPOCH))
    from zicato.runtime.control import control_dir

    flag = control_dir(workspace) / CMD_PAUSE_EPOCH

    # The injected sleep clears the pause after 2 polls — observed by the
    # real block_while_paused inside evolve_n_rounds.
    poll_state = {"n": 0}

    def _fake_sleep(_seconds: float) -> None:
        poll_state["n"] += 1
        if poll_state["n"] >= 2:
            if flag.exists():
                flag.unlink()

    import zicato.runtime.control_consumer as cc

    real_block = cc.block_while_paused

    def _patched_block(ws: Path, **_k: Any) -> int:
        return real_block(ws, sleep=_fake_sleep, max_polls=20)

    monkeypatch.setattr(orch, "block_while_paused", _patched_block)

    outcomes = asyncio.run(
        orch.evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_aux_call_llm,
        )
    )
    # The round ran only after the pause cleared.
    assert calls == [0]
    assert len(outcomes) == 1
    assert poll_state["n"] >= 2  # the loop actually blocked
    # The pause episode is archived.
    assert any(r["command"] == CMD_PAUSE_EPOCH for r in _archived(workspace))


def test_rubric_replacement_rolls_the_epoch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rubric_replacement between rounds writes the brief and rolls the epoch."""
    workspace, epoch_id = _bootstrap_workspace(tmp_path)

    seen_epochs: list[str | None] = []

    async def _mock_evolve_once(
        *, epoch_id: str | None = None, round_index: int = 0, **_kwargs: Any
    ) -> orch.EvolveRoundOutcome:
        seen_epochs.append(epoch_id)
        return _mock_outcome(round_index)

    monkeypatch.setattr(orch, "evolve_once", _mock_evolve_once)

    # Queue the rubric replacement BEFORE the loop starts so it fires at the
    # top of round 0's between-rounds safe point.
    write_command(
        workspace,
        ControlCommand(name=CMD_RUBRIC_REPLACEMENT, payload="# Rolled brief\n- new focus\n"),
    )

    # Pass epoch_id=None so the loop resolves (and re-resolves) the epoch via
    # the contract — the rubric edit drifts the hash and rolls a new epoch.
    asyncio.run(
        orch.evolve_n_rounds(
            rounds=1,
            workspace_root=workspace,
            epoch_id=None,
            auto_epoch=True,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_aux_call_llm,
        )
    )

    # The round ran against a DIFFERENT (rolled) epoch than the original.
    assert seen_epochs and seen_epochs[0] is not None
    assert seen_epochs[0] != epoch_id

    # The live brief now carries the operator's replacement text.
    from zicato.epoch.contract import resolve_contract_inputs

    brief_path = resolve_contract_inputs(workspace).brief_path
    assert brief_path.read_text() == "# Rolled brief\n- new focus\n"

    # The rubric command was consumed + archived.
    assert list_pending_commands(workspace) == []
    assert any(r["command"] == CMD_RUBRIC_REPLACEMENT for r in _archived(workspace))
