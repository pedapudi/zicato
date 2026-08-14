"""Unit tests for the round-log emission seams (WS8).

The e2e transition-sequence oracle lives in
``tests/test_convergence_known_answer.py``; these tests pin the emission
plumbing itself:

* :class:`zicato.evolve.round_reporting._RoundLogEmitter` — the best-effort
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

import pytest

from zicato.core.types import Experiment, HypothesisSpec, ProposerQualityConfig
from zicato.epoch.round_log import RoundLog, fold_round_record
from zicato.evolve.round_reporting import _RoundLogEmitter
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
        from zicato.evolve.round_reporting import _emit_harness_loaded

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
        from zicato.evolve.round_reporting import _emit_harness_loaded

        (tmp_path / "epochs").mkdir()
        # An adapter exposing neither an entrypoint_file nor a tree status
        # writes nothing at all.
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


class TestTreeImportStatusRecord:
    """The per-tree half of the record: accumulate, emit, and stay compatible."""

    @staticmethod
    def _duel(parent_id: str, child_id: str) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(parent_generation_id=parent_id, child_generation_id=child_id)

    @staticmethod
    def _session(status: dict[str, str], entrypoint_file: str = "") -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(
            entrypoint_file=entrypoint_file,
            tree_import_status=lambda: dict(status),
        )

    def test_verified_by_any_unit_wins_over_never_imported(self, tmp_path: Path) -> None:
        """A tree ANY unit imported is verified for the whole generation.

        The record accumulates across the generation's units: one board entry
        that never touches a tree must not mark it never-imported when another
        entry did import it from the snapshot.
        """
        from zicato._tournament_worker import _record_harness_load, _verify_trees_after_run
        from zicato.core.workspace import harness_load_path
        from zicato.storage import read_json

        (tmp_path / "epochs").mkdir()
        checkout = tmp_path / "ztw-snap" / "v1"
        # Unit 1: imported neither tree.
        _verify_trees_after_run(
            tmp_path,
            epoch_id="e1",
            generation_id="v1",
            session=self._session({"agent": "never_imported", "otherpkg": "never_imported"}),
            snapshot_root=checkout,
        )
        # Unit 2: imported `agent` from the snapshot.
        _verify_trees_after_run(
            tmp_path,
            epoch_id="e1",
            generation_id="v1",
            session=self._session({"agent": "verified", "otherpkg": "never_imported"}),
            snapshot_root=checkout,
        )
        record = read_json(harness_load_path(tmp_path, "e1", "v1"))
        assert record["trees_verified"] == ["agent"]
        assert record["trees_never_imported"] == ["otherpkg"]

        # The pre-run entrypoint record and the post-run tree record share the
        # file without clobbering each other.
        _record_harness_load(
            tmp_path,
            epoch_id="e1",
            generation_id="v1",
            session=self._session({}, entrypoint_file=str(checkout / "agent" / "agent.py")),
            snapshot_root=checkout,
        )
        record = read_json(harness_load_path(tmp_path, "e1", "v1"))
        assert record["entrypoint_file"] == "agent/agent.py"
        assert record["trees_never_imported"] == ["otherpkg"]

    def test_a_tree_imported_from_outside_the_snapshot_fails_the_unit(self, tmp_path: Path) -> None:
        """Defence in depth: post-run, an outside-the-snapshot tree raises.

        Load time should already have refused it. Reaching here means the unit
        ran unmutated code, so it must fail rather than score — with the
        evidence recorded first.
        """
        from zicato._tournament_worker import _verify_trees_after_run
        from zicato.core.workspace import harness_load_path
        from zicato.storage import read_json

        (tmp_path / "epochs").mkdir()
        with pytest.raises(RuntimeError, match="otherpkg"):
            _verify_trees_after_run(
                tmp_path,
                epoch_id="e1",
                generation_id="v2",
                session=self._session({"agent": "verified", "otherpkg": "outside_root"}),
                snapshot_root=tmp_path / "ztw-snap" / "v2",
            )
        record = read_json(harness_load_path(tmp_path, "e1", "v2"))
        assert record["trees_verified"] == ["agent"]

    def test_never_imported_reaches_the_round_log_and_the_health_finding(
        self, tmp_path: Path
    ) -> None:
        """The worker's record folds through the round log AND loop health."""
        from zicato._tournament_worker import _verify_trees_after_run
        from zicato.evolve.round_reporting import _emit_harness_loaded
        from zicato.health.diagnostics import detect_tree_never_imported
        from zicato.health.inputs import epoch_tree_import_gaps

        (tmp_path / "epochs" / "e1" / "generations" / "v1").mkdir(parents=True)
        _verify_trees_after_run(
            tmp_path,
            epoch_id="e1",
            generation_id="v1",
            session=self._session({"agent": "verified", "otherpkg": "never_imported"}),
            snapshot_root=tmp_path / "ztw-snap" / "v1",
        )

        emitter = _RoundLogEmitter(tmp_path, "e1", 6)
        _emit_harness_loaded(emitter, tmp_path, "e1", self._duel("v0", "v1"))
        record = fold_round_record(RoundLog(tmp_path, "e1", 6).read())
        assert record.harness_never_imported_trees == {"v1": ("otherpkg",)}

        gaps = epoch_tree_import_gaps(tmp_path, "e1")
        assert gaps == {"v1": ("otherpkg",)}
        findings = detect_tree_never_imported(gaps)
        assert [(f.code, f.severity) for f in findings] == [("tree_never_imported", "warning")]
        assert "generation v1" in findings[0].summary

    def test_a_pre_existing_record_without_the_new_keys_still_reads(self, tmp_path: Path) -> None:
        """A ``harness_load.json`` written before the per-tree keys existed.

        Every reader must tolerate the old two-key shape: the emission carries
        the entrypoint alone, the gap collector sees no gap, and a later
        post-run record adds the tree keys without losing the entrypoint.
        """
        from zicato._tournament_worker import _verify_trees_after_run
        from zicato.core.workspace import harness_load_path
        from zicato.evolve.round_reporting import _emit_harness_loaded
        from zicato.health.inputs import epoch_tree_import_gaps
        from zicato.storage import atomic_write_json, read_json

        (tmp_path / "epochs" / "e1" / "generations" / "v1").mkdir(parents=True)
        atomic_write_json(
            harness_load_path(tmp_path, "e1", "v1"),
            {
                "schema": "zicato.harness_load/1",
                "generation_id": "v1",
                "entrypoint_file": "agent/agent.py",
            },
        )
        assert epoch_tree_import_gaps(tmp_path, "e1") == {}

        emitter = _RoundLogEmitter(tmp_path, "e1", 7)
        _emit_harness_loaded(emitter, tmp_path, "e1", self._duel("v0", "v1"))
        record = fold_round_record(RoundLog(tmp_path, "e1", 7).read())
        assert record.harness_entrypoint_files == {"v1": "agent/agent.py"}
        assert record.harness_never_imported_trees == {}

        _verify_trees_after_run(
            tmp_path,
            epoch_id="e1",
            generation_id="v1",
            session=self._session({"agent": "verified"}),
            snapshot_root=tmp_path / "ztw-snap" / "v1",
        )
        merged = read_json(harness_load_path(tmp_path, "e1", "v1"))
        assert merged["entrypoint_file"] == "agent/agent.py"
        assert merged["trees_verified"] == ["agent"]


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


class TestSlateEvidenceReachesTheReader:
    """The COMPOSED #141 seam: real wrapper → real log → real classifier.

    Both halves of issue #141 are pinned in isolation elsewhere — the
    wrapper's emission in ``tests/test_proposer_best_of_n.py``, the reader's
    classification in ``tests/test_epoch_round_integrity.py`` — and each
    half hand-builds the other's side. That leaves the one thing neither can
    check: whether the strings the wrapper actually writes are the strings
    the reader actually matches, through a real JSONL round-trip. A writer
    that re-wrapped its errors, or a reader whose anchor drifted off the
    emitted template, would pass both halves and fail here.
    """

    #: The REAL shape ``proposer/proposer.py`` raises a transport failure in.
    CREDENTIAL_ERROR = (
        "auxiliary LLM call raised AuthenticationError: "
        "401 Unauthorized — API key expired or revoked"
    )

    @staticmethod
    def _run_slate(tmp_path: Path, *, survivors: int, n: int) -> None:
        """Drive a real slate through a real emitter into a real round log."""
        from zicato.proposer.proposer import ProposerError

        emitter = _RoundLogEmitter(tmp_path, "e1", 1)
        emitter.emit("round_opened", {"contract_hash": "sha256:c"})

        class _MostlyDead:
            def __init__(self) -> None:
                self.calls = 0

            async def propose(self, ctx: ProposerContext) -> Experiment:
                self.calls += 1
                if self.calls > n - survivors:
                    return _experiment(f"v{self.calls}", f"idea {self.calls}")
                raise ProposerError([TestSlateEvidenceReachesTheReader.CREDENTIAL_ERROR])

        agent = BestOfNProposerAgent(
            inner=_MostlyDead(),
            config=ProposerQualityConfig(best_of_n=n, critique_enabled=False),
        )
        try:
            asyncio.run(agent.propose(_ctx(emitter.emit)))
        except ProposerError as exc:
            # VERBATIM the all-failed emission in
            # ``evolve/propose_apply.py::_propose_child`` — one event per
            # attempt of the escaping error, which is what puts the
            # ``slot N: `` tag in front of a call-boundary template.
            for attempt_error in exc.attempts:
                emitter.emit("proposal_attempted", {"errors": (str(attempt_error),)})
        emitter.emit("round_closed")

    def test_a_surviving_slate_still_voids_on_its_dead_siblings(self, tmp_path: Path) -> None:
        """Two slots die on a 401, one survives, the round never gates.

        The founding failure: before #141 this log read
        ``candidates_sampled=1, errors=()`` — a round that looks clean while
        its arm was sampled once instead of three times, mid-outage.
        """
        from zicato.epoch.round_integrity import RoundStatus, round_integrity

        self._run_slate(tmp_path, survivors=1, n=3)
        verdict = round_integrity(tmp_path, "e1", 1)

        assert verdict.status == RoundStatus.VOID
        assert verdict.proposer_reached  # the survivor's own candidate_sampled
        # Both dead slots raised the SAME string, and the reader de-duplicates
        # verbatim matches — one outage, reported once.
        assert verdict.infra_markers == (self.CREDENTIAL_ERROR,)
        assert any("hard infra error" in line for line in verdict.evidence)

    def test_an_all_dead_slate_voids_through_the_slot_tag(self, tmp_path: Path) -> None:
        """Every slot dies: the errors arrive slot-TAGGED, and still match.

        This is the round the tag exists for and the round it could most
        easily have blinded — the reader strips ``slot N: `` before testing
        the call-boundary anchor, so an all-slate credential lapse is
        matched rather than silently dropped to an unexplained void.
        """
        from zicato.epoch.round_integrity import RoundStatus, round_integrity

        self._run_slate(tmp_path, survivors=0, n=3)
        verdict = round_integrity(tmp_path, "e1", 1)

        assert verdict.status == RoundStatus.VOID
        assert not verdict.proposer_reached
        # One matched marker per slot, each carrying its slot tag VERBATIM so
        # the operator can tell three slots failing once from one failing
        # three times — the whole point of aggregating rather than re-raising.
        assert verdict.infra_markers == tuple(
            f"slot {i}: {self.CREDENTIAL_ERROR}" for i in range(3)
        )
        # ... and no slot-tagged transport error was mistaken for a patch.
        assert not verdict.invalid_patch
