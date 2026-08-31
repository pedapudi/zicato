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
from zicato.epoch.round_log import RoundEventScope, RoundLog, fold_round_record
from zicato.evolve.round_reporting import _RoundLogEmitter
from zicato.proposer.agent import ProposerContext
from zicato.proposer.best_of_n import BestOfNProposerAgent
from zicato.proposer.proposer import ProposerError


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
        # The emitter derives exactly one coordinate: the lifecycle step of
        # the wire token. The round is the log's own path, not a field.
        assert [event.scope for event in events] == [
            RoundEventScope(step="open"),
            RoundEventScope(step="propose"),
            RoundEventScope(step="close"),
        ]
        record = fold_round_record(events)
        assert record.complete
        assert record.contract_hash == "abc"
        assert record.proposal.experiment_ids == ("exp_1",)

    def test_unknown_token_is_dropped_silently(self, tmp_path: Path) -> None:
        emitter = _RoundLogEmitter(tmp_path, "e1", 0)
        emitter.emit("not_a_real_event", {"x": 1})
        assert RoundLog(tmp_path, "e1", 0).read() == []

    def test_a_payload_field_no_event_declares_raises(self, tmp_path: Path) -> None:
        """A schema mistake is a BUG, and must not be swallowed as a mishap.

        ``seq`` is derived from the file's tail, so a dropped event leaves a
        gap-free log: the round reads back as one that never emitted the
        event at all, and nothing distinguishes the two afterwards. Only a
        STORAGE failure is best-effort (``test_unwritable_log_never_raises``).
        """
        emitter = _RoundLogEmitter(tmp_path, "e1", 0)
        with pytest.raises(TypeError):
            emitter.emit("round_opened", {"no_such_field": True})
        emitter.emit("round_opened", {"contract_hash": "ok"})
        events = RoundLog(tmp_path, "e1", 0).read()
        assert [e.type for e in events] == ["round_opened"]

    def test_scope_travels_beside_the_payload_and_duplicates_none_of_it(
        self, tmp_path: Path
    ) -> None:
        """Scope is a separate argument, and carries only what the payload lacks."""
        emitter = _RoundLogEmitter(tmp_path, "e1", 3)
        emitter.emit(
            "unit_completed",
            {"entry_id": "entry-1", "replicate": 2, "side": "child"},
            {"generation_id": "gen-7"},
        )
        event = RoundLog(tmp_path, "e1", 3).read()[0]
        # The entry, the side and the replicate stay where the payload
        # already states them — a second copy could only drift from the
        # first. The scope adds the challenger the payload cannot name.
        assert event.scope == RoundEventScope(generation_id="gen-7", step="run")
        assert event.payload == {"entry_id": "entry-1", "replicate": 2, "side": "child"}

    def test_a_null_coordinate_reads_back_as_absent(self, tmp_path: Path) -> None:
        emitter = _RoundLogEmitter(tmp_path, "e1", 1)
        emitter.emit("round_opened", {"contract_hash": "h"}, {"generation_id": None})
        event = RoundLog(tmp_path, "e1", 1).read()[0]
        # Never the literal string "None" — a null coordinate is an absent one.
        assert event.scope.generation_id == ""

    def test_proposal_lifecycle_events_share_the_next_generation_scope(
        self, tmp_path: Path
    ) -> None:
        """The outer proposer events join the slate, not merely the round."""
        from types import SimpleNamespace

        from zicato.evolve.propose_apply import _propose_child

        class _Proposer:
            async def propose(self, _ctx: ProposerContext) -> Experiment:
                return _experiment("v1", "idea")

        async def _aux(_system: str, _user: str, _model: str) -> str:
            return ""

        asyncio.run(
            _propose_child(
                proposer_agent=_Proposer(),
                epoch_id="e1",
                parent_id="v0",
                next_id="v1",
                patterns=(),
                mutations=(),
                brief=SimpleNamespace(text="", forbidden_ids=frozenset()),
                loss_summary="",
                auxiliary_call_llm=_aux,
                auxiliary_model="test",
                max_proposer_retries=1,
                workspace_root=tmp_path,
                generation_root=tmp_path,
                validate_experiment=None,
                meta_loop_emitter=None,
                custom_judge_names=frozenset(),
                prior_experiments=(),
                restrict_visibility=True,
                failure_profile="",
                round_index=3,
                round_emitter=_RoundLogEmitter(tmp_path, "e1", 3),
            )
        )
        events = RoundLog(tmp_path, "e1", 3).read()
        assert [event.type for event in events] == [
            "proposal_attempted",
            "experiment_minted",
            "patches_applied",
        ]
        # The two events whose payload cannot name a generation take it from
        # the scope; ``patches_applied`` already states it in its payload, so
        # the scope leaves the coordinate empty rather than restating it.
        assert [event.scope.generation_id for event in events] == ["v1", "v1", ""]
        assert events[-1].payload["generation_id"] == "v1"

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
        ctx = _ctx(lambda token, fields, scope: emitted.append((token, fields, scope)))
        asyncio.run(agent.propose(ctx))
        tokens = [t for t, _f, _s in emitted]
        assert tokens == [
            "candidate_sampled",
            "candidate_sampled",
            "candidate_sampled",
            "critique_selected",
        ]
        assert [f["i"] for t, f, _s in emitted if t == "candidate_sampled"] == [0, 1, 2]
        assert all(f["n"] == 3 for t, f, _s in emitted if t == "candidate_sampled")
        # Every event of a slate names the challenger it is building: a field
        # round drives several through this one seam while their candidate
        # indexes all restart at zero.
        assert all(s == {"generation_id": "v1"} for _t, _f, s in emitted)
        critique = emitted[-1][1]
        assert critique["reason"] == "heuristic"
        assert isinstance(critique["index"], int)

    def test_a_collapsed_slate_still_records_its_selection(self) -> None:
        """One surviving candidate is still a recorded selection (issue #292).

        The early return that mounts a sole survivor used to skip the emit
        entirely, so a round whose other slots all failed minted a generation
        with no record of what it was chosen from — the same blind spot one
        layer down from the missing slate summary. The event is the shared
        shape, and its ``reason`` names the degenerate basis: nothing chose.
        """
        emitted: list[tuple[str, dict[str, Any]]] = []

        class _Inner:
            def __init__(self) -> None:
                self.calls = 0

            async def propose(self, ctx: ProposerContext) -> Experiment:
                self.calls += 1
                if self.calls > 1:
                    raise ProposerError(["slot down"])
                return _experiment("v1", "the only idea")

        agent = BestOfNProposerAgent(
            inner=_Inner(),
            config=ProposerQualityConfig(best_of_n=3, critique_enabled=False),
        )
        result = asyncio.run(agent.propose(_ctx(lambda t, f, s: emitted.append((t, f)))))
        assert result.generation_id == "v1"
        assert [t for t, _f in emitted] == [
            "candidate_sampled",
            "proposal_attempted",
            "proposal_attempted",
            "critique_selected",
        ]
        assert emitted[-1][1] == {
            "index": 0,
            "reason": "sole_candidate",
            "slate": ({"index": 0, "core_idea": "the only idea", "mutation_ids": ["m1"]},),
            "rationale": "",
        }

    def test_a_full_slate_selection_is_unchanged(self) -> None:
        """The degenerate emit did not disturb a slate that really chose."""
        emitted: list[tuple[str, dict[str, Any]]] = []

        class _Inner:
            def __init__(self) -> None:
                self.calls = 0

            async def propose(self, ctx: ProposerContext) -> Experiment:
                self.calls += 1
                return _experiment(f"v{self.calls}", f"idea {self.calls}")

        agent = BestOfNProposerAgent(
            inner=_Inner(),
            config=ProposerQualityConfig(best_of_n=2, critique_enabled=False),
        )
        asyncio.run(agent.propose(_ctx(lambda t, f, s: emitted.append((t, f)))))
        assert [t for t, _f in emitted] == [
            "candidate_sampled",
            "candidate_sampled",
            "critique_selected",
        ]
        assert emitted[-1][1]["reason"] == "heuristic"
        assert [row["core_idea"] for row in emitted[-1][1]["slate"]] == ["idea 1", "idea 2"]

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
        slate_events = [
            event
            for event in RoundLog(tmp_path, "e1", 1).read()
            if event.type in {"candidate_sampled", "critique_selected"}
        ]
        assert slate_events
        assert {event.scope.generation_id for event in slate_events} == {"v1"}
        assert {event.scope.step for event in slate_events} == {"propose"}
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


class TestDuelScopeWiring:
    """What the unit and gate emitters actually WRITE as plan coordinates.

    These records are write-once: a dropped keyword or a reversed side map
    corrupts the durable log for every round emitted before anyone notices,
    and no re-run repairs it. So the assertions are on the scope that
    reaches the log, not on the arguments the call site passes.
    """

    class _Result:
        per_entry_losses = {"entry-b": 0.2, "entry-a": 0.1}
        parent_agg = {"scalar": 0.6}
        child_agg = {"scalar": 0.9}
        outcome = type("_O", (), {"reason": "", "decision": "promote"})()

    def _events(self, tmp_path: Path, type_token: str) -> list[Any]:
        return [e for e in RoundLog(tmp_path, "e1", 4).read() if e.type == type_token]

    def _scopes(self, tmp_path: Path, type_token: str) -> list[RoundEventScope]:
        return [e.scope for e in self._events(tmp_path, type_token)]

    def test_units_name_the_generation_on_each_side(self, tmp_path: Path) -> None:
        from zicato.evolve.round_reporting import _emit_tournament_units

        _emit_tournament_units(
            _RoundLogEmitter(tmp_path, "e1", 4),
            self._Result(),
            parent_generation_id="v-champ",
            child_generation_id="v-chal",
            matchup_id="m-7",
        )

        events = self._events(tmp_path, "unit_completed")
        scopes = [e.scope for e in events]
        # One per (entry, side), entries in sorted order. The entry and the
        # side are the payload's own; the scope adds the generation each ran.
        assert [
            (e.payload["entry_id"], e.payload["side"], e.scope.generation_id) for e in events
        ] == [
            ("entry-a", "parent", "v-champ"),
            ("entry-a", "child", "v-chal"),
            ("entry-b", "parent", "v-champ"),
            ("entry-b", "child", "v-chal"),
        ]
        # The opponent is the OTHER side, never the same generation twice.
        assert [s.attributes["opponent_generation_id"] for s in scopes] == [
            "v-chal",
            "v-champ",
            "v-chal",
            "v-champ",
        ]
        # The aggregate placeholder reaches NO coordinate, named or open:
        # its absence is the honest statement that this event names no draw.
        assert all("replicate" not in s.attributes for s in scopes)
        assert {s.attributes["matchup_id"] for s in scopes} == {"m-7"}
        assert {s.step for s in scopes} == {"run"}

    def test_the_gate_names_the_challenger_and_its_opponent(self, tmp_path: Path) -> None:
        from zicato.evolve.round_reporting import _emit_gate_evaluated

        _emit_gate_evaluated(
            _RoundLogEmitter(tmp_path, "e1", 4),
            self._Result.outcome,
            parent_agg=self._Result.parent_agg,
            child_agg=self._Result.child_agg,
            generation_id="v-chal",
            opponent_generation_id="v-champ",
            matchup_id="m-7",
        )

        (scope,) = self._scopes(tmp_path, "gate_evaluated")
        assert (scope.generation_id, scope.step) == ("v-chal", "gate")
        assert scope.attributes == {"opponent_generation_id": "v-champ", "matchup_id": "m-7"}

    def test_an_unnamed_duel_still_emits_an_unscoped_record(self, tmp_path: Path) -> None:
        """No ids to give (the default call): coordinates are absent, not faked."""
        from zicato.evolve.round_reporting import _emit_gate_evaluated, _emit_tournament_units

        emitter = _RoundLogEmitter(tmp_path, "e1", 4)
        _emit_tournament_units(emitter, self._Result())
        _emit_gate_evaluated(emitter, self._Result.outcome)

        units = self._scopes(tmp_path, "unit_completed")
        (gate,) = self._scopes(tmp_path, "gate_evaluated")
        assert {s.generation_id for s in units} == {""}
        assert [s.attributes for s in units] == [{}] * len(units)
        assert (gate.generation_id, gate.attributes) == ("", {})


def test_the_duel_call_site_names_the_generations_it_gates() -> None:
    """The one duel driver PASSES the ids, not merely accepts them.

    Every round now settles its matchups through ``field.py``'s
    ``_run_matchup`` — the gauntlet reaches it as a one-challenger field — so
    there is exactly ONE place that names the two sides of a duel, and it
    must keep naming them.

    The emitters default their scope arguments to ``""`` so an unscoped
    caller still emits a well-formed record. That default is what makes a
    dropped keyword silent: the round runs, the log is written, and only the
    attribution is gone — permanently, since these records are write-once.
    Driving a whole field round here would cost minutes, so the call site is
    pinned structurally instead, the way ``test_generation_phase`` already
    pins this package's shape.
    """
    import ast

    required = {
        "_emit_tournament_units": {"parent_generation_id", "child_generation_id"},
        "_emit_gate_evaluated": {"generation_id", "opponent_generation_id"},
    }
    module = Path(__file__).resolve().parents[1] / "src" / "zicato" / "evolve" / "field.py"
    seen: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(module.read_text())):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id in required:
            # One call site each; a second would silently overwrite here, so
            # the count is asserted rather than assumed.
            assert node.func.id not in seen, f"{node.func.id} now has more than one call site"
            seen[node.func.id] = {kw.arg or "" for kw in node.keywords}

    assert set(seen) == set(required)
    for name, keywords in sorted(seen.items()):
        missing = required[name] - keywords
        assert not missing, f"field.py: {name} no longer names {sorted(missing)}"
