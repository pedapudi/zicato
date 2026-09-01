"""Crash and concurrency tests for the durable Ladder query charge."""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

import zicato.tournament.governance as governance
from zicato.core import ScoringWeights
from zicato.core.types import LadderConfig, OverfittingConfig
from zicato.tournament.gate import GateOutcome
from zicato.tournament.governance import LadderStateError


def _weights(*, budget: int = 2) -> ScoringWeights:
    return ScoringWeights(
        promote_margin=0.1,
        overfitting=OverfittingConfig(ladder=LadderConfig(budget=budget)),
    )


def _agg(scalar: float) -> dict[str, object]:
    return {"scalar": scalar, "pass_rate": 1.0, "per_entry": {}}


def _reserve_once(workspace_root: str, epoch_id: str, budget: int) -> bool:
    """Process-pool entry point for the cross-process final-query race."""
    _state, reservation = governance._reserve_ladder_query(
        Path(workspace_root), epoch_id, LadderConfig(budget=budget)
    )
    return reservation is not None


def _settle(
    workspace_root: Path,
    epoch_id: str,
    weights: ScoringWeights,
    reservation: governance.LadderQueryReservation,
    *,
    holdout_scalar: float = 0.8,
) -> tuple[GateOutcome, dict[str, object] | None]:
    return governance._ladder_mediated_outcome(
        train_outcome=GateOutcome(
            decision="promoted", reason="", delta_scalar=-0.5, delta_pass_rate=0.0
        ),
        parent_agg=_agg(1.0),
        child_agg=_agg(0.5),
        holdout_parent_agg=_agg(1.0),
        holdout_child_agg=_agg(holdout_scalar),
        weights=weights,
        workspace_root=workspace_root,
        epoch_id=epoch_id,
        reservation=reservation,
    )


def test_established_state_cannot_disappear_and_restore_capacity(tmp_path: Path) -> None:
    weights = _weights()
    state, reservation = governance._reserve_ladder_query(
        tmp_path, "e0", weights.overfitting.ladder
    )
    assert reservation is not None and state.budget_remaining == 1

    from zicato.core.workspace import ladder_state_path

    ladder_state_path(tmp_path, "e0").unlink()
    with pytest.raises(LadderStateError, match="missing after the epoch budget was initialized"):
        governance._reserve_ladder_query(tmp_path, "e0", weights.overfitting.ladder)


def test_malformed_established_state_is_not_reinitialized(tmp_path: Path) -> None:
    weights = _weights()
    governance._load_ladder_state(tmp_path, "e0", weights.overfitting.ladder)

    from zicato.core.workspace import ladder_state_path

    ladder_state_path(tmp_path, "e0").write_text("{not-json", encoding="utf-8")
    with pytest.raises(LadderStateError, match="cannot read Ladder state"):
        governance._reserve_ladder_query(tmp_path, "e0", weights.overfitting.ladder)


def test_missing_cross_process_lock_support_fails_before_state_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unavailable() -> object:
        raise LadderStateError("durable Ladder reservations require cross-process file locking")

    monkeypatch.setattr(governance, "_cross_process_lock_module", unavailable)
    with pytest.raises(LadderStateError, match="require cross-process file locking"):
        governance._reserve_ladder_query(tmp_path, "e0", _weights().overfitting.ladder)

    from zicato.core.workspace import ladder_state_path

    assert not ladder_state_path(tmp_path, "e0").exists()


@pytest.mark.parametrize(
    ("best_holdout_scalar", "best_confirmed"),
    [(None, True), (0.4, None)],
)
def test_released_scalar_and_confirmation_must_be_present_together(
    tmp_path: Path,
    best_holdout_scalar: float | None,
    best_confirmed: bool | None,
) -> None:
    weights = _weights()
    governance._load_ladder_state(tmp_path, "e0", weights.overfitting.ladder)

    from zicato.core.workspace import ladder_state_path

    path = ladder_state_path(tmp_path, "e0")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["best_holdout_scalar"] = best_holdout_scalar
    raw["best_confirmed"] = best_confirmed
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(LadderStateError, match="invalid or mismatched values"):
        governance._load_ladder_state(tmp_path, "e0", weights.overfitting.ladder)


def test_interrupted_initialization_does_not_launch_or_restore_capacity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    weights = _weights()
    real_write = governance.atomic_write_json

    def fail_marker(path: Path, data: object) -> None:
        if path.name == "ladder_state.initialized.json":
            raise OSError("marker unavailable")
        real_write(path, data)

    monkeypatch.setattr(governance, "atomic_write_json", fail_marker)
    with pytest.raises(LadderStateError, match="cannot mark Ladder state initialized"):
        governance._reserve_ladder_query(tmp_path, "e0", weights.overfitting.ladder)

    from zicato.core.workspace import ladder_state_path

    raw = json.loads(ladder_state_path(tmp_path, "e0").read_text(encoding="utf-8"))
    assert raw["budget_remaining"] == 2

    monkeypatch.setattr(governance, "atomic_write_json", real_write)
    state, reservation = governance._reserve_ladder_query(
        tmp_path, "e0", weights.overfitting.ladder
    )
    assert reservation is not None
    assert state.budget_remaining == 1


def test_failed_reservation_write_preserves_the_previous_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    weights = _weights()
    governance._load_ladder_state(tmp_path, "e0", weights.overfitting.ladder)
    real_write = governance.atomic_write_json

    def fail_state(path: Path, data: object) -> None:
        if path.name == "ladder_state.json":
            raise OSError("state unavailable")
        real_write(path, data)

    monkeypatch.setattr(governance, "atomic_write_json", fail_state)
    with pytest.raises(LadderStateError, match="cannot persist Ladder state"):
        governance._reserve_ladder_query(tmp_path, "e0", weights.overfitting.ladder)

    monkeypatch.setattr(governance, "atomic_write_json", real_write)
    state = governance._load_ladder_state(tmp_path, "e0", weights.overfitting.ladder)
    assert state.budget_remaining == 2


def test_crash_after_reservation_conservatively_consumes_the_query(tmp_path: Path) -> None:
    weights = _weights(budget=1)
    _state, reservation = governance._reserve_ladder_query(
        tmp_path, "e0", weights.overfitting.ladder
    )
    assert reservation is not None

    from zicato.core.workspace import ladder_state_path

    raw = json.loads(ladder_state_path(tmp_path, "e0").read_text(encoding="utf-8"))
    assert raw["pending_reservations"] == [
        {
            "reservation_id": reservation.reservation_id,
            "budget_before_query": 1,
        }
    ]

    # Simulate a process ending here, before it launches or settles a matchup.
    recovered = governance._load_ladder_state(tmp_path, "e0", weights.overfitting.ladder)
    assert recovered.budget_remaining == 0
    exhausted, second = governance._reserve_ladder_query(tmp_path, "e0", weights.overfitting.ladder)
    assert second is None and exhausted.budget_remaining == 0


def test_failed_final_publication_cannot_restore_the_charged_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    weights = _weights(budget=1)
    _state, reservation = governance._reserve_ladder_query(
        tmp_path, "e0", weights.overfitting.ladder
    )
    assert reservation is not None
    real_write = governance.atomic_write_json

    def fail_state(path: Path, data: object) -> None:
        if path.name == "ladder_state.json":
            raise OSError("state unavailable")
        real_write(path, data)

    monkeypatch.setattr(governance, "atomic_write_json", fail_state)
    with pytest.raises(LadderStateError, match="cannot persist Ladder state"):
        governance._ladder_mediated_outcome(
            train_outcome=GateOutcome(
                decision="promoted", reason="", delta_scalar=-0.5, delta_pass_rate=0.0
            ),
            parent_agg=_agg(1.0),
            child_agg=_agg(0.5),
            holdout_parent_agg=_agg(1.0),
            holdout_child_agg=_agg(0.8),
            weights=weights,
            workspace_root=tmp_path,
            epoch_id="e0",
            reservation=reservation,
        )

    monkeypatch.setattr(governance, "atomic_write_json", real_write)
    recovered = governance._load_ladder_state(tmp_path, "e0", weights.overfitting.ladder)
    assert recovered.budget_remaining == 0
    assert recovered.best_confirmed is None


def test_settled_reservation_cannot_release_a_second_holdout_result(tmp_path: Path) -> None:
    weights = _weights(budget=1)
    _state, reservation = governance._reserve_ladder_query(
        tmp_path, "e0", weights.overfitting.ladder
    )
    assert reservation is not None

    first, _block = _settle(tmp_path, "e0", weights, reservation)
    assert first.decision == "promoted"

    with pytest.raises(LadderStateError, match="already settled or is unknown"):
        _settle(tmp_path, "e0", weights, reservation, holdout_scalar=5.0)

    state = governance._load_ladder_state(tmp_path, "e0", weights.overfitting.ladder)
    assert state.budget_remaining == 0
    assert state.best_confirmed is True


def test_reservation_from_another_workspace_is_rejected(tmp_path: Path) -> None:
    weights = _weights()
    source = tmp_path / "source"
    target = tmp_path / "target"
    _state, reservation = governance._reserve_ladder_query(source, "e0", weights.overfitting.ladder)
    assert reservation is not None

    with pytest.raises(LadderStateError, match="no longer matches"):
        _settle(target, "e0", weights, reservation)

    from zicato.core.workspace import ladder_state_path

    assert not ladder_state_path(target, "e0").exists()


def test_reservation_from_another_epoch_is_rejected_without_initializing_it(
    tmp_path: Path,
) -> None:
    weights = _weights()
    _state, reservation = governance._reserve_ladder_query(
        tmp_path, "e0", weights.overfitting.ladder
    )
    assert reservation is not None

    with pytest.raises(LadderStateError, match="no longer matches"):
        _settle(tmp_path, "e1", weights, reservation)

    from zicato.core.workspace import ladder_state_path

    assert not ladder_state_path(tmp_path, "e1").exists()


def test_concurrent_callers_cannot_reserve_the_same_final_query(tmp_path: Path) -> None:
    weights = _weights(budget=1)

    def reserve() -> bool:
        _state, reservation = governance._reserve_ladder_query(
            tmp_path, "e0", weights.overfitting.ladder
        )
        return reservation is not None

    with ThreadPoolExecutor(max_workers=8) as pool:
        reserved = list(pool.map(lambda _index: reserve(), range(8)))

    assert reserved.count(True) == 1
    state = governance._load_ladder_state(tmp_path, "e0", weights.overfitting.ladder)
    assert state.budget_remaining == 0


def test_concurrent_processes_cannot_reserve_the_same_final_query(tmp_path: Path) -> None:
    with ProcessPoolExecutor(max_workers=4) as pool:
        reserved = list(
            pool.map(
                _reserve_once,
                [str(tmp_path)] * 8,
                ["e0"] * 8,
                [1] * 8,
            )
        )

    assert reserved.count(True) == 1
    state = governance._load_ladder_state(tmp_path, "e0", LadderConfig(budget=1))
    assert state.budget_remaining == 0
