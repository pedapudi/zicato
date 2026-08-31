"""The single-challenger gauntlet under fast mode holds the holdout back.

Fast mode is a runtime evaluation knob: it resolves board units through the
unit cache instead of re-running them. It is not an evaluation rule, so a
fast round must select and promote under exactly the rule a full round uses
— internal selection scored on the TRAIN slice, and a train win confirmed on
the holdout before it can crown (OVERFITTING.md §3/§4).

The one path that did otherwise aggregated a fast-mode gauntlet duel over
the WHOLE board, holdout entries included, and skipped the crowning
confirmation outright (issue #319). Gains an operator can only see on
entries the proposer never reads could therefore carry a promotion, which
is the exact failure the split exists to catch.

The harness stub keys its canned loss on ``(generation, entry)``, so a
challenger can score differently on the holdout entry than on the train
entries — the divergence these tests are built out of.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests._orchestrator_harness import (
    _harness_call_llm,
    _install_stub_adapter_factory,
    _make_aux_responder,
)
from tests.test_orchestrator_multi_challenger_holdout import (
    _bootstrap,
    _crowned_outcome,
    _distinct_field_responses,
    _install_per_entry_telemetry_stubs,
)

#: The four train entries and the one explicitly tagged holdout entry the
#: shared bootstrap writes.
_TRAIN_ENTRIES = tuple(f"train_{i}" for i in range(4))
_HOLDOUT_ENTRY = "h0"


def _run_fast_round(workspace: Path, epoch_id: str) -> object:
    """Run one single-challenger round under fast mode."""
    from zicato.orchestrator import evolve_once

    return asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(_distinct_field_responses(1)),
            fast_mode=True,
        )
    )


def _losses(
    *, champion: float, challenger_train: float, challenger_holdout: float
) -> dict[tuple[str, str], float]:
    """Canned per-entry losses for the champion ``v0`` and challenger ``v1``."""
    loss: dict[tuple[str, str], float] = {}
    for entry in (*_TRAIN_ENTRIES, _HOLDOUT_ENTRY):
        loss[("v0", entry)] = champion
    for entry in _TRAIN_ENTRIES:
        loss[("v1", entry)] = challenger_train
    loss[("v1", _HOLDOUT_ENTRY)] = challenger_holdout
    return loss


def test_a_holdout_only_improvement_cannot_promote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A challenger that improves only on held-out entries is refused.

    The challenger matches the champion on every train entry and improves
    hugely on the one holdout entry. Aggregated over the whole board that is
    a mean of 1.6 against the champion's 2.0 — a 0.4 win, four times the
    promote margin. Scored on the train slice, which is what decides
    selection, the two are equal and nothing has been shown.
    """
    workspace, epoch_id = _bootstrap(tmp_path, structure="gauntlet", field_size=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=_losses(champion=2.0, challenger_train=2.0, challenger_holdout=0.0),
        pass_by_gen={"v0": True, "v1": True},
    )

    outcome = _run_fast_round(workspace, epoch_id)

    from zicato.evolve.generation_phase import current_generation

    assert outcome.tournament_decision != "promoted"
    assert current_generation(workspace, epoch_id) == "v0"
    record = _crowned_outcome(workspace, epoch_id, "v1")
    # Selection saw the train slice alone, so the challenger's recorded train
    # loss is the train value rather than the whole-board blend.
    assert record["train_loss"] == pytest.approx(2.0)


def test_a_train_win_that_holds_on_the_holdout_still_promotes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The mirror: a real improvement is crowned, and the confirmation ran.

    A populated ``holdout`` block is the evidence that fast mode now confirms
    a crowning on the held-out slice; the block was absent from every
    fast-mode gauntlet promotion before.
    """
    workspace, epoch_id = _bootstrap(tmp_path, structure="gauntlet", field_size=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=_losses(champion=2.0, challenger_train=0.5, challenger_holdout=0.5),
        pass_by_gen={"v0": True, "v1": True},
    )

    outcome = _run_fast_round(workspace, epoch_id)

    from zicato.evolve.generation_phase import current_generation

    assert outcome.tournament_decision == "promoted"
    assert current_generation(workspace, epoch_id) == "v1"
    record = _crowned_outcome(workspace, epoch_id, "v1")
    assert record["holdout"] is not None, "a fast-mode crowning must carry its confirmation"
    assert record["holdout"]["confirmed"] is True
    assert record["train_loss"] == pytest.approx(0.5)
    assert record["holdout_loss"] == pytest.approx(0.5)
    assert record["generalization_gap"] == pytest.approx(0.0)


def test_a_train_win_that_regresses_on_the_holdout_is_flipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A memorized win is caught in fast mode, as it already was in full mode.

    The challenger improves on every train entry and regresses hard on the
    holdout entry. The crowning confirmation flips the train promote and the
    champion stands.
    """
    workspace, epoch_id = _bootstrap(tmp_path, structure="gauntlet", field_size=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=_losses(champion=2.0, challenger_train=0.5, challenger_holdout=5.0),
        pass_by_gen={"v0": True, "v1": True},
    )

    outcome = _run_fast_round(workspace, epoch_id)

    from zicato.evolve.generation_phase import current_generation

    assert outcome.tournament_decision == "rejected"
    assert "holdout_not_confirmed" in outcome.rejection_reason
    assert current_generation(workspace, epoch_id) == "v0"
    record = _crowned_outcome(workspace, epoch_id, "v1")
    assert record["holdout"]["confirmed"] is False
    assert record["holdout_loss"] == pytest.approx(5.0)
    assert record["generalization_gap"] > 0.0


def test_the_same_round_in_full_mode_reaches_the_same_verdicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fast and full mode agree, which is the property the fix restores.

    Fast mode changes where a board unit's result comes from, never what the
    result means, so the holdout-only improvement is refused under both.
    """
    workspace, epoch_id = _bootstrap(tmp_path, structure="gauntlet", field_size=1)
    _install_stub_adapter_factory(monkeypatch)
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=_losses(champion=2.0, challenger_train=2.0, challenger_holdout=0.0),
        pass_by_gen={"v0": True, "v1": True},
    )

    from zicato.evolve.generation_phase import current_generation
    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(_distinct_field_responses(1)),
            fast_mode=False,
        )
    )

    assert outcome.tournament_decision != "promoted"
    assert current_generation(workspace, epoch_id) == "v0"


def test_an_empty_holdout_leaves_fast_mode_scoring_the_whole_board(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no holdout, the train slice IS the board and nothing is excluded.

    The degrade guard: a board too small to split (or a contract with the
    split off) must reach the same verdict it always did, since the train
    slice and the board are then the same set of entries.
    """
    workspace, epoch_id = _bootstrap(
        tmp_path, structure="gauntlet", field_size=1, with_holdout_tag=False
    )
    _install_stub_adapter_factory(monkeypatch)
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=_losses(champion=2.0, challenger_train=0.5, challenger_holdout=0.5),
        pass_by_gen={"v0": True, "v1": True},
    )

    outcome = _run_fast_round(workspace, epoch_id)

    from zicato.evolve.generation_phase import current_generation

    assert outcome.tournament_decision == "promoted"
    assert current_generation(workspace, epoch_id) == "v1"
    record = _crowned_outcome(workspace, epoch_id, "v1")
    assert record["holdout"] is None
    assert json.loads(json.dumps(record))["train_loss"] == pytest.approx(0.5)
