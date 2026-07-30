"""Unit tests for the round-log emission seams (WS8).

The e2e transition-sequence oracle lives in
``tests/test_convergence_known_answer.py``; these tests pin the emission
plumbing itself:

* :class:`zicato.orchestrator._RoundLogEmitter` — the best-effort
  discipline (a failing append / an unknown token can never raise) and the
  typed-event resolution through the wire token;
* the best-of-N wrapper's ``candidate_sampled`` / ``critique_selected``
  emission through the :attr:`ProposerContext.round_event_emitter` seam,
  including the never-fail guard around a raising emitter.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from zicato.core.types import Experiment, HypothesisSpec, ProposerQualityConfig
from zicato.epoch.round_log import RoundLog, fold_round_record
from zicato.orchestrator import _RoundLogEmitter
from zicato.proposer.agent import ProposerContext
from zicato.proposer.best_of_n import BestOfNProposerAgent


def _experiment(gen_id: str, idea: str) -> Experiment:
    return Experiment(
        id=f"exp_e1_{gen_id}",
        epoch_id="e1",
        generation_id=gen_id,
        parent_generation_id="v0",
        proposed_at="2026-01-01T00:00:00+00:00",
        hypothesis=HypothesisSpec(
            core_idea=idea,
            modulating=("m1",),
            why="test",
            expected_drift_movements=(),
            expected_pass_rate_delta="0.0",
            risks="",
        ),
        patches=(),
        outcome=None,
    )


def _ctx(emitter: Any) -> ProposerContext:
    async def _aux(_s: str, _u: str, _m: str) -> str:
        return "0"

    return ProposerContext(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=(),
        mutations=(),
        brief_text="",
        current_loss_summary="",
        aux_call_llm=_aux,
        round_event_emitter=emitter,
    )


class TestRoundLogEmitter:
    def test_emits_typed_events_that_fold(self, tmp_path: Path) -> None:
        emitter = _RoundLogEmitter(tmp_path, "e1", 3)
        emitter.emit("round_opened", {"contract_hash": "abc"})
        emitter.emit("experiment_minted", {"experiment_id": "exp_1"})
        emitter.emit("round_closed")
        events = RoundLog(tmp_path, "e1", 3).read()
        assert [e.type for e in events] == ["round_opened", "experiment_minted", "round_closed"]
        record = fold_round_record(events)
        assert record.complete
        assert record.contract_hash == "abc"
        assert record.proposal.experiment_ids == ("exp_1",)

    def test_unknown_token_is_dropped_silently(self, tmp_path: Path) -> None:
        emitter = _RoundLogEmitter(tmp_path, "e1", 0)
        emitter.emit("not_a_real_event", {"x": 1})
        assert RoundLog(tmp_path, "e1", 0).read() == []

    def test_bad_fields_never_raise(self, tmp_path: Path) -> None:
        emitter = _RoundLogEmitter(tmp_path, "e1", 0)
        # An unexpected field is a constructor TypeError — swallowed.
        emitter.emit("round_opened", {"no_such_field": True})
        emitter.emit("round_opened", {"contract_hash": "ok"})
        events = RoundLog(tmp_path, "e1", 0).read()
        assert [e.type for e in events] == ["round_opened"]

    def test_unwritable_log_never_raises(self, tmp_path: Path) -> None:
        # Bind onto a path whose parent is a FILE, so every append fails —
        # the emitter must swallow it (emission never fails a round).
        blocker = tmp_path / "epochs"
        blocker.write_text("not a directory")
        emitter = _RoundLogEmitter(tmp_path, "e1", 0)
        emitter.emit("round_opened", {"contract_hash": "abc"})  # must not raise


class TestHarnessLoadedEmission:
    """The worker → orchestrator snapshot-origin seam (issue #110).

    The worker is the only process that imports the entrypoint, so it writes
    the resolved ``__file__`` to the generation's ``harness_load.json``; the
    orchestrator — the round log's single writer — folds it into one
    ``harness_loaded`` event per generation that ran.
    """

    @staticmethod
    def _duel(parent_id: str, child_id: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(parent_generation_id=parent_id, child_generation_id=child_id)

    def test_worker_record_is_emitted_once_per_generation(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from zicato._tournament_worker import _record_harness_load
        from zicato.orchestrator import _emit_harness_loaded

        (tmp_path / "epochs").mkdir()
        # The worker sees a per-run EPHEMERAL snapshot root; what it records is
        # the snapshot-RELATIVE path, so the durable record survives the
        # checkout being deleted and is comparable across generations.
        for gen_id in ("v0", "v1"):
            checkout = tmp_path / "ztw-snap" / gen_id
            _record_harness_load(
                tmp_path,
                epoch_id="e1",
                generation_id=gen_id,
                session=SimpleNamespace(entrypoint_file=str(checkout / "agent" / "agent.py")),
                snapshot_root=checkout,
            )

        emitter = _RoundLogEmitter(tmp_path, "e1", 4)
        _emit_harness_loaded(emitter, tmp_path, "e1", self._duel("v0", "v1"))

        record = fold_round_record(RoundLog(tmp_path, "e1", 4).read())
        assert record.harness_entrypoint_files == {
            "v0": "agent/agent.py",
            "v1": "agent/agent.py",
        }

    def test_absent_record_emits_nothing(self, tmp_path: Path) -> None:
        """No record (a non-ADK adapter, a cache-served side) ⇒ no event."""
        from types import SimpleNamespace

        from zicato._tournament_worker import _record_harness_load
        from zicato.orchestrator import _emit_harness_loaded

        (tmp_path / "epochs").mkdir()
        # An adapter exposing no entrypoint_file writes nothing at all.
        _record_harness_load(
            tmp_path,
            epoch_id="e1",
            generation_id="v0",
            session=SimpleNamespace(),
            snapshot_root=tmp_path / "ztw-snap" / "v0",
        )
        emitter = _RoundLogEmitter(tmp_path, "e1", 5)
        _emit_harness_loaded(emitter, tmp_path, "e1", self._duel("v0", "v1"))
        assert RoundLog(tmp_path, "e1", 5).read() == []


class TestBestOfNEmission:
    def test_candidate_and_critique_events_emitted(self) -> None:
        emitted: list[tuple[str, dict[str, Any]]] = []

        class _Inner:
            def __init__(self) -> None:
                self.calls = 0

            async def propose(self, ctx: ProposerContext) -> Experiment:
                self.calls += 1
                return _experiment(f"v{self.calls}", f"idea {self.calls}")

        agent = BestOfNProposerAgent(
            inner=_Inner(),
            config=ProposerQualityConfig(best_of_n=3, critique_enabled=False),
        )
        ctx = _ctx(lambda token, fields: emitted.append((token, fields)))
        asyncio.run(agent.propose(ctx))
        tokens = [t for t, _f in emitted]
        assert tokens == [
            "candidate_sampled",
            "candidate_sampled",
            "candidate_sampled",
            "critique_selected",
        ]
        assert [f["i"] for t, f in emitted if t == "candidate_sampled"] == [0, 1, 2]
        assert all(f["n"] == 3 for t, f in emitted if t == "candidate_sampled")
        critique = emitted[-1][1]
        assert critique["reason"] == "heuristic"
        assert isinstance(critique["index"], int)

    def test_raising_emitter_never_fails_the_propose(self) -> None:
        class _Inner:
            async def propose(self, ctx: ProposerContext) -> Experiment:
                return _experiment("v1", "idea")

        def _boom(_token: str, _fields: dict[str, Any]) -> None:
            raise RuntimeError("emitter down")

        agent = BestOfNProposerAgent(
            inner=_Inner(),
            config=ProposerQualityConfig(best_of_n=2, critique_enabled=False),
        )
        result = asyncio.run(agent.propose(_ctx(_boom)))
        assert result.generation_id == "v1"

    def test_single_sample_pin_emits_nothing(self) -> None:
        emitted: list[str] = []

        class _Inner:
            async def propose(self, ctx: ProposerContext) -> Experiment:
                return _experiment("v1", "idea")

        agent = BestOfNProposerAgent(inner=_Inner(), config=ProposerQualityConfig(best_of_n=1))
        asyncio.run(agent.propose(_ctx(lambda t, f: emitted.append(t))))
        assert emitted == []
