"""Train/holdout split + Ladder-mediated champion-gate confirmation for the
NON-GAUNTLET tournament structures (swiss / single_elim / double_elim / racing).

The non-gauntlet structures resolve a leader/survivor through their
bracket/swiss/racing logic — all scored on the TRAIN slice — and then run ONE
final champion-gate duel of that survivor vs the reigning champion. These
tests prove the faithful extension of the gauntlet's invariant to every
structure (OVERFITTING.md §3/§4):

* internal selection scores on the TRAIN slice (the holdout entry never picks
  the leader);
* the final champion-gate Ladder-confirms a true general win (promoted, with
  ``record.holdout`` + the per-generation train/holdout/gap fields persisted);
* a holdout regression flips a bracket-leader's train win to ``rejected``
  (``holdout_not_confirmed``) — the champion stands;
* the per-epoch ladder budget is SHARED + decremented across the structure's
  confirmation;
* an EMPTY holdout (small board / split disabled) is byte-identical to today's
  whole-board non-gauntlet behaviour (a regression guard).

The harness mock keys the canned per-board loss on ``(generation, entry)`` so
the train and holdout slices can diverge for one challenger — the only thing
the default ``canned_loss_by_gen`` stub (keyed on generation alone) cannot
express.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from tests.test_orchestrator import (
    _harness_call_llm,
    _install_stub_adapter_factory,
    _make_aux_responder,
    _valid_proposer_response,
)
from zicato.core import BoardEntry, DriftCount, ExpectationResult, LossProfile
from zicato.core.types import OverfittingConfig, ScoringWeights, TournamentStructure
from zicato.epoch.lifecycle import new_epoch

# Structures under test + their minimal params (small fields keep the bracket
# shallow so the synthetic field resolves in one pass).
_STRUCTURES = ["swiss", "single_elim", "double_elim", "racing"]


def _struct_params(structure: str, field_size: int) -> dict[str, object]:
    params: dict[str, object] = {"field_size": field_size, "replicates": 1}
    if structure == "swiss":
        params["rounds_n"] = 1
    if structure == "racing":
        # Keep every rung on the whole (train) board so the synthetic losses
        # decide the cut deterministically.
        params["board_fraction"] = 1.0
    return params


def _install_per_entry_telemetry_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    loss_by_gen_entry: dict[tuple[str, str], float],
    pass_by_gen: dict[str, bool],
    default_loss: float = 0.0,
) -> None:
    """Telemetry stub whose per-board loss is keyed on ``(generation, entry)``.

    Mirrors ``test_orchestrator._install_telemetry_stubs`` but lets a single
    challenger score differently on the holdout entry than on the train
    entries — the divergence the holdout-confirmation step exists to catch.
    """
    sink_mod = types.ModuleType("zicato.telemetry.sink")

    def make_run_sink_path(
        *, workspace_root: Path, epoch_id: str, generation_id: str, entry_id: str
    ) -> Path:
        del epoch_id, generation_id, entry_id
        return workspace_root / "events.jsonl"

    sink_mod.make_run_sink_path = make_run_sink_path  # type: ignore[attr-defined]

    reducer_mod = types.ModuleType("zicato.telemetry.reducer")

    def read_loss_profile(path: Path) -> LossProfile:
        del path
        raise FileNotFoundError

    reducer_mod.read_loss_profile = read_loss_profile  # type: ignore[attr-defined]

    supervisor_mod = types.ModuleType("zicato.telemetry.harmonograf_supervisor")

    class _StubHandle:
        url: str = ""

        def shutdown(self) -> None:
            return None

    def _stub_start(*_a: object, **_k: object) -> _StubHandle:
        return _StubHandle()

    supervisor_mod.start_harmonograf = _stub_start  # type: ignore[attr-defined]
    supervisor_mod.HarmonografHandle = _StubHandle  # type: ignore[attr-defined]

    telemetry_pkg = types.ModuleType("zicato.telemetry")
    telemetry_pkg.sink = sink_mod  # type: ignore[attr-defined]
    telemetry_pkg.reducer = reducer_mod  # type: ignore[attr-defined]
    telemetry_pkg.harmonograf_supervisor = supervisor_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "zicato.telemetry", telemetry_pkg)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.sink", sink_mod)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.reducer", reducer_mod)
    monkeypatch.setitem(sys.modules, "zicato.telemetry.harmonograf_supervisor", supervisor_mod)

    import zicato.tournament.runner as _runner_mod

    async def _fake_run_single(
        *,
        adapter: object,
        generation: object,
        entry: BoardEntry,
        weights: object,
        config: object,
        workspace_root: Path,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, side, match_id
        gid = generation.id  # type: ignore[attr-defined]
        expectation_result = (
            ExpectationResult(kind="predicate", passed=True)
            if entry.expectation is not None
            else None
        )
        _runner_mod._ingest_run_into_index(workspace_root, epoch_id, gid, entry.id)
        return LossProfile(
            run_id=f"r-{gid}-{entry.id}",
            entry_id=entry.id,
            generation_id=gid,
            epoch_id=epoch_id,
            drift_counts=(DriftCount(kind="off_topic", severity="info", count=0),),
            plan_revisions=0,
            task_failure_ratio=0.0,
            runtime_ms=100,
            wall_clock_budget_exceeded=False,
            expectation_result=expectation_result,
            drift_loss=loss_by_gen_entry.get((gid, entry.id), default_loss),
            pass_fail=pass_by_gen.get(gid),
        )

    monkeypatch.setattr(_runner_mod, "_run_single", _fake_run_single)


def _bootstrap(
    tmp_path: Path,
    *,
    structure: str,
    field_size: int,
    overfitting: OverfittingConfig | None = None,
    with_holdout_tag: bool = True,
) -> tuple[Path, str]:
    """Workspace + non-gauntlet epoch + a v0 baseline over a multi-entry board.

    The board is four train entries + one entry that is the holdout (an
    explicit ``holdout`` tag makes the split deterministic). ``with_holdout_tag
    =False`` drops the tag so the small board degrades to an empty holdout.
    """
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "test",
                "created_at": "2026-06-04T00:00:00Z",
                # Hand-built directory-backend snapshot layout below; pin the
                # directory backend so the git default does not look for git
                # tags this fixture never writes.
                "storage_backend": "directory",
                "adapter": {"kind": "stub"},
            }
        )
    )

    board_src = tmp_path / "board.jsonl"
    lines: list[str] = []
    for i in range(4):
        lines.append(
            json.dumps(
                {
                    "id": f"train_{i}",
                    "kind": "single_turn",
                    "wall_clock_budget_seconds": 60,
                    "input": "hello",
                }
            )
        )
    holdout_entry: dict[str, object] = {
        "id": "h0",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 60,
        "input": "hello",
    }
    if with_holdout_tag:
        holdout_entry["tags"] = ["holdout"]
    lines.append(json.dumps(holdout_entry))
    board_src.write_text("\n".join(lines) + "\n")

    brief_src = tmp_path / "brief.md"
    brief_src.write_text("# Proposer brief\n- Be careful.\n")

    cfg = new_epoch(
        workspace,
        name=f"{structure}-epoch",
        board_source=board_src,
        brief_source=brief_src,
        weights=ScoringWeights(
            promote_margin=0.1,
            tournament_structure=TournamentStructure(
                structure=structure, params=_struct_params(structure, field_size)
            ),
            **({"overfitting": overfitting} if overfitting is not None else {}),
        ),
        auto_close_previous=False,
    )

    v0_dir = workspace / "epochs" / cfg.id / "generations" / "v0"
    snap = v0_dir / "snapshot"
    snap.mkdir(parents=True)
    (snap / "agent.py").write_text(
        '"""Stub harness source for tests."""\n\n'
        '# zicato:mutable id="greeting"\nGREETING = "hello"\n'
    )
    (workspace / "epochs" / cfg.id / "current_generation").write_text("v0\n")
    return workspace, cfg.id


def _crowned_outcome(workspace: Path, epoch_id: str, gid: str) -> dict[str, object]:
    gens = workspace / "epochs" / epoch_id / "generations"
    return json.loads((gens / gid / "experiment.json").read_text())["outcome"]


def _field_bracket(workspace: Path, epoch_id: str, first_challenger_id: str) -> dict[str, object]:
    """Read the durable per-round FIELD tournament snapshot."""
    from zicato.core.workspace import field_tournament_path

    path = field_tournament_path(workspace, epoch_id, first_challenger_id)
    return json.loads(path.read_text())


def _lineage_promoted(workspace: Path, epoch_id: str, gid: str) -> bool | None:
    """Return the ``promoted`` flag the lineage records for ``gid``."""
    from zicato.epoch.lineage import load_lineage

    lineage = load_lineage(workspace)
    for entry in lineage.get("epochs", []):
        if entry.get("id") != epoch_id:
            continue
        for g in entry.get("generations", []):
            if g.get("id") == gid:
                return g.get("promoted")
    return None


@pytest.mark.parametrize("structure", _STRUCTURES)
def test_holdout_confirms_a_true_win_and_persists_records(
    structure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A challenger that wins on BOTH train and holdout is crowned, and the
    crowning challenger's OutcomeRecord carries ``holdout`` + the per-generation
    train/holdout/gap fields (so #5's detector + the board-status surface work
    for these structures too)."""
    workspace, epoch_id = _bootstrap(tmp_path, structure=structure, field_size=2)
    _install_stub_adapter_factory(monkeypatch)
    # v1 improves both train (2.0 -> 0.5) and holdout (2.0 -> 0.5): a general
    # win. v2 is a weaker challenger; v0 is the champion.
    loss: dict[tuple[str, str], float] = {}
    for e in (*(f"train_{i}" for i in range(4)), "h0"):
        loss[("v0", e)] = 2.0
        loss[("v1", e)] = 0.5
        loss[("v2", e)] = 1.5
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=loss,
        pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(
                [_valid_proposer_response(), _valid_proposer_response()]
            ),
        )
    )

    assert outcome.tournament_decision == "promoted", structure
    crowned = outcome.proposed_generation_id
    rec = _crowned_outcome(workspace, epoch_id, crowned)
    assert rec["tournament_decision"] == "promoted"
    # The crowning challenger carries the Ladder/holdout evidence block.
    assert rec["holdout"] is not None, "crowning challenger must persist record.holdout"
    assert rec["holdout"]["confirmed"] is True
    # Per-generation train/holdout/gap fields. Selection steers on the TRAIN
    # scalar (the crowned challenger's, 0.5 or 1.5 depending on the bracket /
    # swiss survivor) — never a blend with the holdout. The challenger holds on
    # the holdout slice, so the gap is ~0.
    assert rec["train_loss"] in (pytest.approx(0.5), pytest.approx(1.5))
    assert rec["holdout_loss"] == pytest.approx(rec["train_loss"])
    assert rec["generalization_gap"] == pytest.approx(0.0)
    # current_generation advanced to the crowned challenger.
    marker = workspace / "epochs" / epoch_id / "current_generation"
    assert marker.read_text().strip() == crowned


@pytest.mark.parametrize("structure", _STRUCTURES)
def test_holdout_regression_flips_a_bracket_leaders_win_to_reject(
    structure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The strongest challenger wins the bracket on the TRAIN slice but
    REGRESSES on the holdout: a memorized win. The final champion-gate
    Ladder-confirmation flips the promote to ``rejected`` (``holdout_not_
    confirmed``) and the champion stands."""
    workspace, epoch_id = _bootstrap(tmp_path, structure=structure, field_size=2)
    _install_stub_adapter_factory(monkeypatch)
    # BOTH challengers win the TRAIN slice (2.0 -> 0.5 / 1.5) but BOTH regress
    # hard on the HOLDOUT entry (2.0 -> 5.0): whichever survivor reaches the
    # champion gate, the holdout confirmation must flip its train win. (Using
    # both removes any dependence on which challenger a structure's bracket /
    # swiss bye happens to advance to the gate.)
    loss: dict[tuple[str, str], float] = {}
    for e in (f"train_{i}" for i in range(4)):
        loss[("v0", e)] = 2.0
        loss[("v1", e)] = 0.5
        loss[("v2", e)] = 1.5
    loss[("v0", "h0")] = 2.0
    loss[("v1", "h0")] = 5.0  # holdout regression
    loss[("v2", "h0")] = 5.0  # holdout regression
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=loss,
        pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    from zicato.orchestrator import _resolve_current_generation, evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(
                [_valid_proposer_response(), _valid_proposer_response()]
            ),
        )
    )

    assert outcome.tournament_decision == "rejected", structure
    assert "holdout_not_confirmed" in outcome.rejection_reason
    # Champion stands — the promoted head is still v0.
    assert _resolve_current_generation(workspace, epoch_id) == "v0"
    # The bracket leader (the survivor that reached the gate) records the
    # holdout cause + the gap that exposed it.
    leader = outcome.proposed_generation_id
    rec = _crowned_outcome(workspace, epoch_id, leader)
    assert rec["tournament_decision"] == "rejected"
    assert rec["holdout"] is not None
    assert rec["holdout"]["confirmed"] is False
    # The leader's holdout-slice scalar exposes the regression (gap > 0). The
    # train scalar is whichever survivor reached the gate (0.5 or 1.5).
    assert rec["train_loss"] in (pytest.approx(0.5), pytest.approx(1.5))
    assert rec["holdout_loss"] == pytest.approx(5.0)
    assert rec["generalization_gap"] > 0.0


@pytest.mark.parametrize("structure", _STRUCTURES)
def test_per_epoch_ladder_budget_is_shared_and_decremented(
    structure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The crowning confirmation loads/saves the SAME per-epoch
    ``ladder_state.json`` the gauntlet uses, so the budget is shared across the
    structure's confirmation and decrements when the holdout is queried."""
    workspace, epoch_id = _bootstrap(tmp_path, structure=structure, field_size=2)
    _install_stub_adapter_factory(monkeypatch)
    loss: dict[tuple[str, str], float] = {}
    for e in (*(f"train_{i}" for i in range(4)), "h0"):
        loss[("v0", e)] = 2.0
        loss[("v1", e)] = 0.5
        loss[("v2", e)] = 1.5
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=loss,
        pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    from zicato.core.workspace import ladder_state_path
    from zicato.orchestrator import evolve_once

    asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(
                [_valid_proposer_response(), _valid_proposer_response()]
            ),
        )
    )

    state_path = ladder_state_path(workspace, epoch_id)
    assert state_path.exists(), "the crowning confirmation must persist the shared ladder state"
    state = json.loads(state_path.read_text())
    # The single crowning confirmation charged exactly one query against the
    # shared per-epoch budget.
    assert state["budget_remaining"] == state["budget_total"] - 1


@pytest.mark.parametrize("structure", _STRUCTURES)
def test_empty_holdout_degrades_to_whole_board(
    structure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the split disabled (empty holdout) the non-gauntlet path is
    byte-identical to today: the holdout step is skipped, no ladder state is
    written, and no holdout fields are persisted — even when one entry would
    have regressed on a (now non-existent) holdout slice."""
    workspace, epoch_id = _bootstrap(
        tmp_path,
        structure=structure,
        field_size=2,
        # No holdout tag + split disabled ⇒ the whole board is train.
        overfitting=OverfittingConfig(enabled=False),
        with_holdout_tag=False,
    )
    _install_stub_adapter_factory(monkeypatch)
    # h0 would regress, but with no holdout slice it is just another train
    # entry — v1 still wins the whole-board average and promotes.
    loss: dict[tuple[str, str], float] = {}
    for e in (f"train_{i}" for i in range(4)):
        loss[("v0", e)] = 2.0
        loss[("v1", e)] = 0.5
        loss[("v2", e)] = 1.5
    loss[("v0", "h0")] = 2.0
    loss[("v1", "h0")] = 0.5
    loss[("v2", "h0")] = 1.5
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=loss,
        pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    from zicato.core.workspace import ladder_state_path
    from zicato.orchestrator import evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(
                [_valid_proposer_response(), _valid_proposer_response()]
            ),
        )
    )

    assert outcome.tournament_decision == "promoted", structure
    crowned = outcome.proposed_generation_id
    rec = _crowned_outcome(workspace, epoch_id, crowned)
    # No holdout was consulted: no evidence block, no holdout/gap fields, and
    # no ladder state file was written (no query charged).
    assert rec["holdout"] is None
    assert rec["holdout_loss"] is None
    assert rec["generalization_gap"] is None
    assert not ladder_state_path(workspace, epoch_id).exists()


# --- issue #20: a settled bracket may never assert a promotion the workspace
# contradicts. These tests pin the durable FIELD bracket against the champion
# pointer + lineage for BOTH a true win and a holdout-flipped win, and prove
# the crowning invariant fails loudly when they cannot be made to agree.


@pytest.mark.parametrize("structure", _STRUCTURES)
def test_settled_promotion_agrees_with_champion_and_lineage(
    structure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A round that settles ``promoted`` advances ``current_generation`` AND
    lineage to the promoted generation, AND the durable FIELD bracket records
    the SAME promotion — no store disagrees (issue #20, acceptance #1)."""
    workspace, epoch_id = _bootstrap(tmp_path, structure=structure, field_size=2)
    _install_stub_adapter_factory(monkeypatch)
    loss: dict[tuple[str, str], float] = {}
    for e in (*(f"train_{i}" for i in range(4)), "h0"):
        loss[("v0", e)] = 2.0
        loss[("v1", e)] = 0.5
        loss[("v2", e)] = 1.5
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=loss,
        pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    from zicato.orchestrator import _resolve_current_generation, evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(
                [_valid_proposer_response(), _valid_proposer_response()]
            ),
        )
    )

    assert outcome.tournament_decision == "promoted", structure
    crowned = outcome.proposed_generation_id
    # The champion pointer advanced AND lineage marks the crowned gen promoted.
    assert _resolve_current_generation(workspace, epoch_id) == crowned
    assert _lineage_promoted(workspace, epoch_id, crowned) is True
    # The durable FIELD bracket records the SAME promotion — no contradiction.
    bracket = _field_bracket(workspace, epoch_id, "v1")
    assert bracket["decision"] == "promoted", structure
    assert bracket["promoted_generation_id"] == crowned
    assert bracket["state"] == "settled"


@pytest.mark.parametrize("structure", _STRUCTURES)
def test_holdout_flip_persists_a_rejected_bracket_not_a_phantom_promotion(
    structure: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The regression in issue #20: the durable bracket used to be persisted
    from the TRAIN decision BEFORE the holdout confirmation could flip it, so a
    holdout-demoted round left a settled bracket asserting ``promoted`` while
    the champion pointer + lineage stayed at v0. The bracket must now record
    the HOLDOUT-RESOLVED ``rejected`` verdict — agreeing with the champion that
    stood."""
    workspace, epoch_id = _bootstrap(tmp_path, structure=structure, field_size=2)
    _install_stub_adapter_factory(monkeypatch)
    loss: dict[tuple[str, str], float] = {}
    for e in (f"train_{i}" for i in range(4)):
        loss[("v0", e)] = 2.0
        loss[("v1", e)] = 0.5
        loss[("v2", e)] = 1.5
    loss[("v0", "h0")] = 2.0
    loss[("v1", "h0")] = 5.0  # holdout regression
    loss[("v2", "h0")] = 5.0  # holdout regression
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=loss,
        pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    from zicato.orchestrator import _resolve_current_generation, evolve_once

    outcome = asyncio.run(
        evolve_once(
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=_harness_call_llm,
            auxiliary_call_llm=_make_aux_responder(
                [_valid_proposer_response(), _valid_proposer_response()]
            ),
        )
    )

    assert outcome.tournament_decision == "rejected", structure
    # Champion stands.
    assert _resolve_current_generation(workspace, epoch_id) == "v0"
    leader = outcome.proposed_generation_id
    assert _lineage_promoted(workspace, epoch_id, leader) is False
    # The durable bracket reflects the holdout flip — NOT a phantom promotion.
    bracket = _field_bracket(workspace, epoch_id, "v1")
    assert bracket["decision"] == "rejected", structure
    assert not bracket["promoted_generation_id"]
    assert "holdout_not_confirmed" in str(bracket["reason"])


def test_crowning_invariant_raises_when_champion_pointer_cannot_advance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If crowning settles ``promoted`` but the champion pointer cannot be
    updated to the promoted generation, settlement RAISES rather than persist a
    contradictory bracket (issue #20, acceptance #3). We force the divergence
    by stubbing the marker writer to a no-op so the re-read after the crowning
    write still names the old champion."""
    workspace, epoch_id = _bootstrap(tmp_path, structure="single_elim", field_size=2)
    _install_stub_adapter_factory(monkeypatch)
    loss: dict[tuple[str, str], float] = {}
    for e in (*(f"train_{i}" for i in range(4)), "h0"):
        loss[("v0", e)] = 2.0
        loss[("v1", e)] = 0.5
        loss[("v2", e)] = 1.5
    _install_per_entry_telemetry_stubs(
        monkeypatch,
        loss_by_gen_entry=loss,
        pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    import zicato.orchestrator as _orch

    # The crowning write becomes a no-op, so current_generation stays v0 even
    # though the bracket settled a promotion — exactly the silent divergence
    # the fail-loud guard must catch.
    monkeypatch.setattr(_orch, "_set_current_generation", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="crowning invariant violated"):
        asyncio.run(
            _orch.evolve_once(
                workspace_root=workspace,
                epoch_id=epoch_id,
                harness_call_llm=_harness_call_llm,
                auxiliary_call_llm=_make_aux_responder(
                    [_valid_proposer_response(), _valid_proposer_response()]
                ),
            )
        )
