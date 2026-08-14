"""Pre-tournament candidate screening (tryouts) — panel, vetoes, hygiene.

Covers :mod:`zicato.epoch.screen`:

* the ROTATING train panel (:func:`select_screen_entries`): lexicographic
  ring, per-round rotation, coverage over rounds, short-panel fill from
  non-passing entries, cold start (no parent losses), determinism;
* veto classification: a clear candidate is never vetoed; a pass-flip on a
  champion-passing entry vetoes only after the confirm re-run flips TWICE;
  an infra abort is NO-SIGNAL (never a veto); a budget abort vetoes
  immediately with no confirm run spent;
* cache + lineage hygiene: every screen run lands on the RESERVED
  replicate 3000 (confirm at 3001), no ``*-screen-*`` phantom directory
  survives the screen, the entry-time sweep self-heals a crashed prior
  run, and the canonical cache slot of a REAL generation still MISSES
  afterwards (a screen can never pre-seed a tournament);
* the counts-only contract: no result string ever carries an entry id.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import zicato.tournament.runner as runner_mod
import zicato.tournament.scheduling as scheduling_mod
from zicato.core import BoardEntry, Generation, LossProfile, RuntimeConfig, ScoringWeights
from zicato.core.loss import BUDGET_ABORT_CAUSE
from zicato.core.types import Experiment, HypothesisSpec
from zicato.core.workspace import generations_dir
from zicato.epoch.screen import (
    SCREEN_REPLICATE_BASE,
    ScreenPanel,
    run_candidate_screen,
    select_screen_entries,
    sweep_stale_screen_dirs,
)
from zicato.testing.fixtures import make_loss_profile
from zicato.tournament.unit_cache import _resolve_cached_unit

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _entry(entry_id: str) -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )


def _parent_loss(entry_id: str, *, pass_fail: bool | None = True) -> LossProfile:
    return make_loss_profile(
        run_id=f"run-v0-{entry_id}",
        entry_id=entry_id,
        generation_id="v0",
        epoch_id="e1",
        drift_loss=1.0,
        pass_fail=pass_fail,
    )


def _experiment(exp_id: str) -> Experiment:
    """A patch-free experiment: apply_patches degrades to a pure tree copy."""
    return Experiment(
        id=exp_id,
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-06-09T00:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea=f"idea {exp_id}",
            modulating=(),
            why="because",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.05",
        ),
        patches=(),
        outcome=None,
    )


def _workspace(tmp_path: Path) -> tuple[Path, Generation]:
    """A minimal workspace + parent generation with an on-disk snapshot."""
    workspace_root = tmp_path / "ws"
    generations_dir(workspace_root, "e1").mkdir(parents=True, exist_ok=True)
    snapshot = tmp_path / "parent_snapshot"
    snapshot.mkdir(exist_ok=True)
    (snapshot / "policy.txt").write_text("champion policy\n")
    parent = Generation(
        id="v0",
        epoch_id="e1",
        parent_id=None,
        snapshot_root=snapshot,
        created_at="2026-06-09T00:00:00Z",
        promoted=True,
    )
    return workspace_root, parent


def _config(tmp_path: Path) -> RuntimeConfig:
    async def _llm(system: str, user: str, model: str) -> str:
        return ""

    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        harness_call_llm=_llm,
        auxiliary_call_llm=_llm,
    )


class _ScreenWorld:
    """Scriptable stand-in for the board-unit worker.

    ``verdicts`` maps ``(entry_id, replicate_index)`` to either a
    ``bool | None`` pass verdict or an ``abort_cause`` string; entries
    not scripted pass cleanly. Every call is recorded as
    ``(generation_id, entry_id, replicate_index, match_id)``.
    """

    def __init__(self, verdicts: dict[tuple[str, int], Any] | None = None) -> None:
        self.verdicts = dict(verdicts or {})
        self.calls: list[tuple[str, str, int, str]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner_mod, "_run_single", self._fake_run_single)
        monkeypatch.setattr(scheduling_mod, "_runtime_state", lambda: None)

    async def _fake_run_single(
        self,
        *,
        adapter: Any,
        generation: Generation,
        entry: BoardEntry,
        weights: Any,
        config: Any,
        workspace_root: Any,
        epoch_id: str,
        side: str,
        match_id: str = "",
    ) -> LossProfile:
        del adapter, weights, config, workspace_root, side
        replicate = int(dict(entry.context).get("replicate_index", "0") or 0)
        self.calls.append((generation.id, entry.id, replicate, match_id))
        verdict = self.verdicts.get((entry.id, replicate), True)
        abort_cause: str | None = None
        pass_fail: bool | None
        budget_exceeded = False
        if isinstance(verdict, str):
            abort_cause = verdict
            pass_fail = None
            budget_exceeded = True
        else:
            pass_fail = verdict
        return make_loss_profile(
            run_id=f"run-{generation.id}-{entry.id}-r{replicate}",
            entry_id=entry.id,
            generation_id=generation.id,
            epoch_id=epoch_id,
            drift_loss=1.0 if pass_fail else 2.0,
            pass_fail=pass_fail,
            wall_clock_budget_exceeded=budget_exceeded,
            abort_cause=abort_cause,
        )


async def _screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    world: _ScreenWorld,
    *,
    panel: ScreenPanel,
    candidates: list[Experiment] | None = None,
    round_index: int = 0,
) -> list[Any]:
    workspace_root, parent = _workspace(tmp_path)
    world.install(monkeypatch)
    return await run_candidate_screen(
        candidates=candidates if candidates is not None else [_experiment("c0")],
        adapter=object(),
        parent_gen=parent,
        panel=panel,
        weights=ScoringWeights(),
        config=_config(tmp_path),
        workspace_root=workspace_root,
        epoch_id="e1",
        round_index=round_index,
    )


# ---------------------------------------------------------------------------
# Panel selection — rotation / ring / coverage / cold start
# ---------------------------------------------------------------------------


def test_panel_is_lexicographic_ring_over_champion_passing_entries() -> None:
    board = [_entry(e) for e in ("e_c", "e_a", "e_b", "e_d")]
    losses = [_parent_loss(e.id) for e in board]
    panel = select_screen_entries(board, losses, 2, round_index=0)
    assert [e.id for e in panel.entries] == ["e_a", "e_b"]
    assert panel.baseline_pass_ids == {"e_a", "e_b"}


def test_panel_rotates_across_rounds_and_covers_the_ring() -> None:
    board = [_entry(f"e{i}") for i in range(5)]
    losses = [_parent_loss(e.id) for e in board]
    seen: set[str] = set()
    panels = []
    for r in range(5):
        panel = select_screen_entries(board, losses, 2, round_index=r)
        assert len(panel.entries) == 2
        panels.append(tuple(e.id for e in panel.entries))
        seen.update(e.id for e in panel.entries)
    # start = (r*k) % 5 walks the whole ring: full coverage by round 5.
    assert seen == {f"e{i}" for i in range(5)}
    # Rotation: consecutive rounds start k entries later, wrapping.
    assert panels[0] == ("e0", "e1")
    assert panels[1] == ("e2", "e3")
    assert panels[2] == ("e4", "e0")


def test_panel_is_deterministic_no_wall_clock() -> None:
    board = [_entry(f"e{i}") for i in range(7)]
    losses = [_parent_loss(e.id) for e in board]
    first = select_screen_entries(board, losses, 3, round_index=4)
    second = select_screen_entries(board, losses, 3, round_index=4)
    assert first == second


def test_short_panel_fills_from_non_passing_entries_crash_only() -> None:
    board = [_entry(e) for e in ("e_a", "e_b", "e_c", "e_d")]
    # Only e_c passes at baseline; e_b failed, others carry no expectation.
    losses = [
        _parent_loss("e_c", pass_fail=True),
        _parent_loss("e_b", pass_fail=False),
        _parent_loss("e_a", pass_fail=None),
    ]
    panel = select_screen_entries(board, losses, 3, round_index=0)
    ids = [e.id for e in panel.entries]
    assert ids[0] == "e_c"  # the one flip-eligible entry leads
    assert len(ids) == 3
    # Fill entries carry no passing baseline — crash detection only.
    assert panel.baseline_pass_ids == {"e_c"}


def test_cold_start_no_parent_losses_is_crash_only() -> None:
    board = [_entry(f"e{i}") for i in range(4)]
    panel = select_screen_entries(board, [], 2, round_index=1)
    assert len(panel.entries) == 2
    assert panel.baseline_pass_ids == frozenset()


def test_panel_empty_when_disabled_or_no_board() -> None:
    board = [_entry("e0")]
    assert select_screen_entries(board, [], 0, round_index=0).entries == ()
    assert select_screen_entries([], [], 3, round_index=0).entries == ()


def test_panel_never_exceeds_the_board() -> None:
    board = [_entry(f"e{i}") for i in range(2)]
    losses = [_parent_loss(e.id) for e in board]
    panel = select_screen_entries(board, losses, 10, round_index=0)
    assert sorted(e.id for e in panel.entries) == ["e0", "e1"]


# ---------------------------------------------------------------------------
# Veto classification
# ---------------------------------------------------------------------------


def _panel(*entry_ids: str, passing: tuple[str, ...] | None = None) -> ScreenPanel:
    ids = tuple(entry_ids)
    return ScreenPanel(
        entries=tuple(_entry(e) for e in ids),
        baseline_pass_ids=frozenset(ids if passing is None else passing),
    )


def test_clear_candidate_is_not_vetoed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _ScreenWorld()
    results = asyncio.run(_screen(tmp_path, monkeypatch, world, panel=_panel("e_a", "e_b")))
    (res,) = results
    assert res.vetoed is False
    assert res.confirmed is False
    assert res.scalar is not None
    assert res.entries_screened == 2
    assert res.baseline_passes == 2
    assert res.candidate_passes == 2
    assert res.reason.startswith("clear")


def test_pass_flip_vetoes_only_after_confirm_re_run_flips_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _ScreenWorld(
        {
            ("e_a", SCREEN_REPLICATE_BASE): False,
            ("e_a", SCREEN_REPLICATE_BASE + 1): False,  # flips twice ⇒ veto
        }
    )
    results = asyncio.run(_screen(tmp_path, monkeypatch, world, panel=_panel("e_a", "e_b")))
    (res,) = results
    assert res.vetoed is True
    assert res.confirmed is True
    # ONE confirming re-run of the flipped entry at the reserved slot 3001.
    confirms = [c for c in world.calls if c[2] == SCREEN_REPLICATE_BASE + 1]
    assert [(c[1]) for c in confirms] == ["e_a"]
    assert confirms[0][3] == "candidate-screen-confirm:r0:c0"


def test_unconfirmed_flip_does_not_veto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _ScreenWorld(
        {
            ("e_a", SCREEN_REPLICATE_BASE): False,
            ("e_a", SCREEN_REPLICATE_BASE + 1): True,  # confirm run passes ⇒ noise
        }
    )
    results = asyncio.run(_screen(tmp_path, monkeypatch, world, panel=_panel("e_a", "e_b")))
    (res,) = results
    assert res.vetoed is False
    assert res.confirmed is False


def test_infra_abort_is_no_signal_never_a_veto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _ScreenWorld(
        {
            ("e_a", SCREEN_REPLICATE_BASE): "parent_kill",
            ("e_b", SCREEN_REPLICATE_BASE): "gone_no_result",
        }
    )
    results = asyncio.run(_screen(tmp_path, monkeypatch, world, panel=_panel("e_a", "e_b")))
    (res,) = results
    assert res.vetoed is False
    # Every panel unit infra-aborted — no usable signal, no scalar.
    assert res.scalar is None
    # No confirm run was spent on a no-signal panel.
    assert all(c[2] == SCREEN_REPLICATE_BASE for c in world.calls)


def test_budget_abort_vetoes_immediately_without_confirm_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _ScreenWorld({("e_a", SCREEN_REPLICATE_BASE): BUDGET_ABORT_CAUSE})
    results = asyncio.run(_screen(tmp_path, monkeypatch, world, panel=_panel("e_a", "e_b")))
    (res,) = results
    assert res.vetoed is True
    assert res.confirmed is False  # immediate veto, not a confirmed flip
    assert all(c[2] == SCREEN_REPLICATE_BASE for c in world.calls)


def test_non_passing_baseline_entries_cannot_pass_flip_veto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cold-start shape: no entry carries a passing baseline, and the
    # candidate fails both — crash-only semantics keep it un-vetoed.
    world = _ScreenWorld(
        {
            ("e_a", SCREEN_REPLICATE_BASE): False,
            ("e_b", SCREEN_REPLICATE_BASE): False,
        }
    )
    results = asyncio.run(
        _screen(tmp_path, monkeypatch, world, panel=_panel("e_a", "e_b", passing=()))
    )
    (res,) = results
    assert res.vetoed is False
    assert res.baseline_passes == 0
    # ...but a budget abort still vetoes on a crash-only panel.
    world2 = _ScreenWorld({("e_a", SCREEN_REPLICATE_BASE): BUDGET_ABORT_CAUSE})
    results2 = asyncio.run(
        _screen(tmp_path, monkeypatch, world2, panel=_panel("e_a", "e_b", passing=()))
    )
    assert results2[0].vetoed is True


def test_reason_strings_carry_counts_only_never_entry_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _ScreenWorld(
        {
            ("entry_alpha", SCREEN_REPLICATE_BASE): False,
            ("entry_alpha", SCREEN_REPLICATE_BASE + 1): False,
            ("entry_beta", SCREEN_REPLICATE_BASE): BUDGET_ABORT_CAUSE,
        }
    )
    results = asyncio.run(
        _screen(
            tmp_path,
            monkeypatch,
            world,
            panel=_panel("entry_alpha", "entry_beta", "entry_gamma"),
        )
    )
    for res in results:
        for token in ("entry_alpha", "entry_beta", "entry_gamma"):
            assert token not in res.reason


def test_screen_error_yields_no_signal_not_a_veto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A candidate whose patches cannot re-apply (bogus mutation id) is a
    # per-candidate engine failure: no signal, never a veto — screening
    # must never fail (or narrow) a propose step on its own error.
    from dataclasses import replace

    from zicato.core.types import Patch

    bad = replace(
        _experiment("broken"),
        patches=(
            Patch(
                id="p1",
                mutation_id="missing__mutation",
                op="replace",
                new_content="x",
                new_numeric=None,
                new_enum=None,
                rationale="r",
            ),
        ),
    )
    world = _ScreenWorld()
    results = asyncio.run(
        _screen(
            tmp_path,
            monkeypatch,
            world,
            panel=_panel("e_a"),
            candidates=[bad, _experiment("fine")],
        )
    )
    assert results[0].vetoed is False
    assert results[0].scalar is None
    # The healthy sibling still screened normally.
    assert results[1].scalar is not None


# ---------------------------------------------------------------------------
# Cache isolation + phantom-dir hygiene
# ---------------------------------------------------------------------------


def test_cache_isolation_reserved_replicate_and_no_phantom_dir_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root, parent = _workspace(tmp_path)
    world = _ScreenWorld()
    world.install(monkeypatch)
    panel = _panel("e_a", "e_b")
    results = asyncio.run(
        run_candidate_screen(
            candidates=[_experiment("c0"), _experiment("c1")],
            adapter=object(),
            parent_gen=parent,
            panel=panel,
            weights=ScoringWeights(),
            config=_config(tmp_path),
            workspace_root=workspace_root,
            epoch_id="e1",
            round_index=3,
        )
    )
    assert len(results) == 2
    # (a) The stub saw every unit on the RESERVED replicate 3000, under the
    # ephemeral screen ids, with the screen match_id.
    assert world.calls, "screen ran no units"
    for gen_id, _entry_id, replicate, match_id in world.calls:
        assert replicate == SCREEN_REPLICATE_BASE
        assert gen_id in ("v0-screen-r3c0", "v0-screen-r3c1")
        assert match_id.startswith("candidate-screen:r3:c")
    # (b) No *-screen-* phantom generation dir survives the screen.
    survivors = [d.name for d in generations_dir(workspace_root, "e1").iterdir()]
    assert not [name for name in survivors if "-screen-" in name]
    # (c) The canonical cache slot of the REAL child generation still
    # MISSES — a screen run can never pre-seed a tournament unit.
    for gen_id in ("v1", "v0"):
        for entry_id in ("e_a", "e_b"):
            assert (
                _resolve_cached_unit(
                    workspace_root=workspace_root,
                    epoch_id="e1",
                    generation_id=gen_id,
                    entry_id=entry_id,
                    replicate_index=0,
                )
                is None
            )


def test_entry_sweep_self_heals_stale_screen_dirs(tmp_path: Path) -> None:
    workspace_root, _parent = _workspace(tmp_path)
    root = generations_dir(workspace_root, "e1")
    (root / "v0-screen-r0c1").mkdir()
    (root / "v0-screen-r2c0" / "runs").mkdir(parents=True)
    (root / "v3").mkdir()  # a REAL generation dir must never be swept
    removed = sweep_stale_screen_dirs(workspace_root, "e1")
    assert removed == 2
    assert sorted(d.name for d in root.iterdir()) == ["v3"]


def test_empty_panel_screens_nothing_and_reports_no_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _ScreenWorld()
    results = asyncio.run(
        _screen(
            tmp_path,
            monkeypatch,
            world,
            panel=ScreenPanel(entries=(), baseline_pass_ids=frozenset()),
        )
    )
    (res,) = results
    assert res.vetoed is False
    assert res.scalar is None
    assert world.calls == []


# ---------------------------------------------------------------------------
# Round-log integration — the candidate_screened event + fold tally
# ---------------------------------------------------------------------------


def test_round_log_folds_candidate_screened_tally(tmp_path: Path) -> None:
    from zicato.epoch.round_log import (
        CandidateSampled,
        CandidateScreened,
        RoundLog,
        RoundOpened,
        fold_round_record,
    )

    log = RoundLog(tmp_path, "e1", 0)
    log.append(RoundOpened(contract_hash="h"))
    log.append(CandidateSampled(i=0, n=2))
    log.append(CandidateSampled(i=1, n=2))
    log.append(
        CandidateScreened(index=0, vetoed=False, confirmed=False, screen_summary={"panel": 2})
    )
    log.append(CandidateScreened(index=1, vetoed=True, confirmed=True, screen_summary={"panel": 2}))
    record = fold_round_record(log.read())
    assert record.proposal.candidates_sampled == 2
    assert record.proposal.candidates_screened == 2
    assert record.proposal.screen_vetoes == 1
    # The event decodes through the wire vocabulary (round-trip).
    decoded = [e.event for e in log.read() if e.type == "candidate_screened"]
    assert [getattr(e, "vetoed", None) for e in decoded] == [False, True]


# ---------------------------------------------------------------------------
# Orchestrator wiring — the per-round closure builder
# ---------------------------------------------------------------------------


def test_screen_runner_not_built_unless_opted_in(tmp_path: Path) -> None:
    from zicato.evolve.round_context import _build_candidate_screen_runner

    workspace_root, parent = _workspace(tmp_path)
    board = [_entry("e_a"), _entry("e_b")]

    def _build(weights: ScoringWeights) -> Any:
        return _build_candidate_screen_runner(
            weights=weights,
            adapter=object(),
            parent_gen=parent,
            train_board=board,
            parent_losses=[_parent_loss(e.id) for e in board],
            config=_config(tmp_path),
            workspace_root=workspace_root,
            epoch_id="e1",
            round_index=0,
            disable_drift=(),
            judge_only=False,
            beater=None,
        )

    from zicato.core.types import ProposerQualityConfig

    # The code default (screen_entries=0): no closure is even constructed.
    assert _build(ScoringWeights()) is None
    # Inert unless best_of_n > 1 — a single sample has no slate to screen.
    assert (
        _build(
            ScoringWeights(proposer_quality=ProposerQualityConfig(best_of_n=1, screen_entries=2))
        )
        is None
    )
    # Opted in: a per-round closure exists.
    runner = _build(ScoringWeights(proposer_quality=ProposerQualityConfig(screen_entries=2)))
    assert runner is not None


def test_screen_runner_closure_drives_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zicato.core.types import ProposerQualityConfig
    from zicato.evolve.round_context import _build_candidate_screen_runner

    workspace_root, parent = _workspace(tmp_path)
    board = [_entry("e_a"), _entry("e_b"), _entry("e_c")]
    world = _ScreenWorld({("e_a", SCREEN_REPLICATE_BASE): BUDGET_ABORT_CAUSE})
    world.install(monkeypatch)
    runner = _build_candidate_screen_runner(
        weights=ScoringWeights(proposer_quality=ProposerQualityConfig(screen_entries=2)),
        adapter=object(),
        parent_gen=parent,
        train_board=board,
        parent_losses=[_parent_loss(e.id) for e in board],
        config=_config(tmp_path),
        workspace_root=workspace_root,
        epoch_id="e1",
        round_index=0,
        disable_drift=(),
        judge_only=False,
        beater=None,
    )
    assert runner is not None
    results = asyncio.run(runner([_experiment("c0")]))
    (res,) = list(results)
    # Round 0's ring panel over the passing baseline is (e_a, e_b); the
    # budget abort on e_a vetoes immediately.
    assert res.vetoed is True
    assert {c[1] for c in world.calls} == {"e_a", "e_b"}
    assert all(c[2] == SCREEN_REPLICATE_BASE for c in world.calls)
