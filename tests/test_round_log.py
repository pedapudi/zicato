"""Unit tests for the per-round durable event log (WS8-1 schema + fold).

The reference event sequence mirrors the Tier-1 convergence round shape
(``tests/test_convergence_known_answer.py`` round 1): open under a
contract hash → one clean proposer attempt → best-of-1 sampling →
experiment minted → patches applied into ``v1`` → the full-board duel's
ten units (5 entries x champion/challenger) → the gate promotes on the
scalar-margin rule → decision recorded with provenance → close.
"""

from __future__ import annotations

import json

import pytest

from zicato.epoch.round_log import (
    CandidateSampled,
    DecisionRecorded,
    EvidenceReplicated,
    ExperimentMinted,
    GateEvaluated,
    HoldoutReleased,
    PatchesApplied,
    ProposalAttempted,
    RoundClosed,
    RoundLog,
    RoundOpened,
    UnitCompleted,
    ValidationFailed,
    fold_round_record,
    round_log_path,
)

ENTRIES = ("conv_body", "conv_summary", "conv_citations", "conv_concise", "conv_no_fabrication")


def _convergence_round_events() -> list:
    """The Tier-1 convergence round-1 shape as a typed event sequence."""
    events: list = [
        RoundOpened(contract_hash="sha256:contract-t0"),
        ProposalAttempted(errors=()),
        CandidateSampled(i=1, n=1),
        ExperimentMinted(experiment_id="exp-v1"),
        PatchesApplied(generation_id="v1"),
    ]
    for entry_id in ENTRIES:
        events.append(UnitCompleted(entry_id=entry_id, replicate=0, side="parent"))
        events.append(UnitCompleted(entry_id=entry_id, replicate=0, side="child"))
    events.extend(
        [
            GateEvaluated(rule_fired="scalar_margin", decision="promoted"),
            DecisionRecorded(
                decision="promoted",
                provenance={"delta_scalar": -1.2, "crowning_matchup_id": "gauntlet"},
            ),
            RoundClosed(),
        ]
    )
    return events


def test_round_log_path_convention(tmp_path):
    """epochs/{epoch}/rounds/{round}/round_log.jsonl, plain decimal round."""
    path = round_log_path(tmp_path, "epoch-01", 3)
    assert path == tmp_path / "epochs" / "epoch-01" / "rounds" / "3" / "round_log.jsonl"


def test_round_trip_and_seq_monotonicity(tmp_path):
    """Appended events read back typed, in order, with a gap-free seq."""
    log = RoundLog(tmp_path, "epoch-01", 1)
    appended = [log.append(e) for e in _convergence_round_events()]
    assert [env.seq for env in appended] == list(range(1, len(appended) + 1))

    read_back = log.read()
    assert [env.seq for env in read_back] == list(range(1, len(appended) + 1))
    assert [env.type for env in read_back] == [env.type for env in appended]
    assert [env.event for env in read_back] == [env.event for env in appended]
    # One complete compact-JSON line per event, newline-terminated.
    text = log.path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert len(text.splitlines()) == len(appended)

    # seq continuity survives re-binding (a fresh writer over the same log
    # continues from the durable tail — the single-writer contract).
    resumed = RoundLog(tmp_path, "epoch-01", 1)
    extra = resumed.append(ProposalAttempted(errors=("late",)))
    assert extra.seq == len(appended) + 1


def test_fold_round_record_on_the_convergence_shape(tmp_path):
    """The fold reduces the reference sequence to the expected typed summary."""
    log = RoundLog(tmp_path, "epoch-01", 1)
    for event in _convergence_round_events():
        log.append(event)

    record = fold_round_record(log.read())
    assert record.opened and record.closed and record.complete
    assert record.contract_hash == "sha256:contract-t0"
    assert record.proposal.attempts == 1
    assert record.proposal.errors == ()
    assert record.proposal.candidates_sampled == 1
    assert record.proposal.experiment_ids == ("exp-v1",)
    assert record.generation_ids == ("v1",)
    assert record.validation_findings == ()
    assert len(record.units) == 2 * len(ENTRIES)
    assert {u.side for u in record.units} == {"parent", "child"}
    assert {u.entry_id for u in record.units} == set(ENTRIES)
    assert record.gates == (GateEvaluated(rule_fired="scalar_margin", decision="promoted"),)
    assert record.holdout is None
    assert record.decision == "promoted"
    assert record.decision_provenance["crowning_matchup_id"] == "gauntlet"
    assert record.last_seq == 2 * len(ENTRIES) + 8


def test_fold_partial_log_is_incomplete_but_usable(tmp_path):
    """A mid-round crash leaves a foldable partial record (closed=False)."""
    log = RoundLog(tmp_path, "epoch-01", 2)
    log.append(RoundOpened(contract_hash="h"))
    log.append(ProposalAttempted(errors=("schema: bad patch",)))
    log.append(ValidationFailed(findings=("markers: unknown mutation id",)))

    record = fold_round_record(log.read())
    assert record.opened and not record.closed and not record.complete
    assert record.proposal.attempts == 1
    assert record.proposal.errors == ("schema: bad patch",)
    assert record.validation_findings == ("markers: unknown mutation id",)
    assert record.decision == ""


def test_fold_evidence_and_holdout_trail(tmp_path):
    """Holdout release + evidence refits accumulate in order; last decision wins."""
    log = RoundLog(tmp_path, "epoch-01", 4)
    log.append(RoundOpened(contract_hash="h"))
    log.append(HoldoutReleased(confirmed=True))
    log.append(EvidenceReplicated(ci_state={"p_stronger": 0.62, "ci_overlap": True}))
    log.append(EvidenceReplicated(ci_state={"p_stronger": 0.91, "ci_overlap": False}))
    log.append(DecisionRecorded(decision="deferred", provenance={"step": 1}))
    log.append(DecisionRecorded(decision="promoted", provenance={"step": 2}))
    log.append(RoundClosed())

    record = fold_round_record(log.read())
    assert record.holdout == HoldoutReleased(confirmed=True)
    assert [s["p_stronger"] for s in record.evidence_trail] == [0.62, 0.91]
    assert record.decision == "promoted"
    assert record.decision_provenance == {"step": 2}


def test_torn_tail_is_skipped_and_repaired(tmp_path):
    """A crash mid-append leaves a torn tail: readers skip it, the next
    append drops it and continues the seq gap-free."""
    log = RoundLog(tmp_path, "epoch-01", 5)
    log.append(RoundOpened(contract_hash="h"))
    log.append(PatchesApplied(generation_id="v1"))

    # Simulate the crash: a partial, newline-less final line.
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq":3,"ts":"2026-')

    # The reader tolerates the torn tail (skips it) and the fold still works.
    events = log.read()
    assert [env.seq for env in events] == [1, 2]
    record = fold_round_record(events)
    assert record.generation_ids == ("v1",)

    # The next append repairs the tail: the dead bytes are dropped, the
    # new event does NOT concatenate with them, and the seq continues from
    # the last durably complete event.
    appended = log.append(RoundClosed())
    assert appended.seq == 3
    events = log.read()
    assert [env.seq for env in events] == [1, 2, 3]
    assert events[-1].event == RoundClosed()
    # Every line on disk is parseable again — the torn bytes are gone.
    for line in log.path.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_interior_corruption_raises(tmp_path):
    """A corrupt NON-tail line violates append-only and must surface loudly."""
    log = RoundLog(tmp_path, "epoch-01", 6)
    log.append(RoundOpened(contract_hash="h"))
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write("garbage-not-json\n")
    log_path = log.path
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 3, "ts": "t", "type": "round_closed", "payload": {}}) + "\n")
    with pytest.raises(ValueError, match="append-only invariant"):
        log.read()


def test_unknown_event_types_read_as_raw_envelopes(tmp_path):
    """A newer writer's unknown event type folds as a no-op, not a failure."""
    log = RoundLog(tmp_path, "epoch-01", 7)
    log.append(RoundOpened(contract_hash="h"))
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"seq": 2, "ts": "t", "type": "from_the_future", "payload": {"x": 1}},
                separators=(",", ":"),
            )
            + "\n"
        )
    log.append(RoundClosed())

    events = log.read()
    assert [env.seq for env in events] == [1, 2, 3]
    unknown = events[1]
    assert unknown.event is None and unknown.payload == {"x": 1}
    record = fold_round_record(events)
    assert record.complete
    assert record.last_seq == 3


def test_tuple_fields_round_trip_through_json(tmp_path):
    """Tuple-typed payload fields come back as tuples on the typed event."""
    log = RoundLog(tmp_path, "epoch-01", 8)
    log.append(ProposalAttempted(errors=("first", "second")))
    (env,) = log.read()
    assert env.event == ProposalAttempted(errors=("first", "second"))


def test_logs_are_isolated_per_round(tmp_path):
    """Rounds 1 and 2 keep independent logs with independent sequences."""
    r1 = RoundLog(tmp_path, "epoch-01", 1)
    r2 = RoundLog(tmp_path, "epoch-01", 2)
    r1.append(RoundOpened(contract_hash="a"))
    assert r2.read() == []
    assert r2.append(RoundOpened(contract_hash="b")).seq == 1
    assert fold_round_record(r1.read()).contract_hash == "a"
    assert fold_round_record(r2.read()).contract_hash == "b"


def test_round_dir_layout_matches_path_helper(tmp_path):
    """The writer creates the directory tree the path helper names."""
    log = RoundLog(tmp_path, "e9", 12)
    log.append(RoundOpened(contract_hash="h"))
    expected = tmp_path / "epochs" / "e9" / "rounds" / "12" / "round_log.jsonl"
    assert log.path == expected
    assert expected.exists()


def test_candidate_sampled_recombined_flag_round_trips_and_folds(tmp_path):
    """WS-REC: the additive ``recombined`` flag decodes + folds; absent
    payloads (pre-recombine logs) default to False and fold to zero."""
    log = RoundLog(tmp_path, "epoch-01", 7)
    log.append(RoundOpened(contract_hash="h"))
    log.append(CandidateSampled(i=0, n=3))
    log.append(CandidateSampled(i=1, n=3))
    log.append(CandidateSampled(i=2, n=3, recombined=True))
    log.append(RoundClosed())

    events = log.read()
    sampled = [e.event for e in events if isinstance(e.event, CandidateSampled)]
    assert [s.recombined for s in sampled] == [False, False, True]

    record = fold_round_record(events)
    assert record.proposal.candidates_sampled == 3
    assert record.proposal.recombined_sampled == 1


def test_pre_recombine_log_decodes_with_flag_defaulted(tmp_path):
    """A log written BEFORE the flag existed (no ``recombined`` key in the
    payload) decodes identically — the additive-default contract."""
    import json as _json

    path = round_log_path(tmp_path, "epoch-01", 8)
    path.parent.mkdir(parents=True)
    lines = [
        {"seq": 1, "ts": "t", "type": "round_opened", "payload": {"contract_hash": "h"}},
        {"seq": 2, "ts": "t", "type": "candidate_sampled", "payload": {"i": 0, "n": 3}},
    ]
    path.write_text("".join(_json.dumps(rec) + "\n" for rec in lines), encoding="utf-8")

    events = RoundLog(tmp_path, "epoch-01", 8).read()
    sampled = events[1].event
    assert isinstance(sampled, CandidateSampled)
    assert sampled.recombined is False
    record = fold_round_record(events)
    assert record.proposal.recombined_sampled == 0


def test_harness_loaded_round_trips_and_folds_per_generation(tmp_path):
    """Issue #110: the snapshot-origin provenance decodes + folds per generation.

    ``harness_loaded`` records WHICH file each side's entrypoint resolved to.
    The fold reduces the events to one entry per generation (last word wins),
    which is what an operator auditing "did the mutation actually run?" reads.
    """
    from zicato.epoch.round_log import HarnessLoaded

    log = RoundLog(tmp_path, "epoch-01", 9)
    log.append(RoundOpened(contract_hash="h"))
    log.append(PatchesApplied(generation_id="v1"))
    # Snapshot-RELATIVE paths, as the worker records them. Distinct here
    # because a proposer may move the entrypoint module between generations.
    log.append(HarnessLoaded(generation_id="v0", entrypoint_file="agent/agent.py"))
    log.append(HarnessLoaded(generation_id="v1", entrypoint_file="agent/root.py"))
    log.append(RoundClosed())

    events = log.read()
    loaded = [e.event for e in events if isinstance(e.event, HarnessLoaded)]
    assert [x.generation_id for x in loaded] == ["v0", "v1"]

    record = fold_round_record(events)
    assert record.harness_entrypoint_files == {
        "v0": "agent/agent.py",
        "v1": "agent/root.py",
    }
    assert record.harness_never_imported_trees == {}


def test_harness_loaded_folds_the_per_tree_verdicts(tmp_path):
    """The per-tree half folds too — a never-imported tree is the #110 alarm.

    ``trees_never_imported`` names the mutable trees NO unit of that generation
    ever imported, so its mutations cannot have been under test. Last word wins
    per generation, including a later event that clears the gap.
    """
    from zicato.epoch.round_log import HarnessLoaded

    log = RoundLog(tmp_path, "epoch-01", 11)
    log.append(RoundOpened(contract_hash="h"))
    log.append(
        HarnessLoaded(
            generation_id="v0",
            entrypoint_file="",
            trees_verified=("agent",),
            trees_never_imported=("otherpkg",),
        )
    )
    log.append(
        HarnessLoaded(
            generation_id="v1",
            entrypoint_file="agent/agent.py",
            trees_verified=("agent", "otherpkg"),
        )
    )
    log.append(RoundClosed())

    events = log.read()
    loaded = [e.event for e in events if isinstance(e.event, HarnessLoaded)]
    # JSON round-trips lists; the decode re-tuples them.
    assert loaded[0].trees_never_imported == ("otherpkg",)
    assert loaded[1].trees_verified == ("agent", "otherpkg")

    record = fold_round_record(events)
    assert record.harness_never_imported_trees == {"v0": ("otherpkg",)}
    assert record.harness_entrypoint_files == {"v0": "", "v1": "agent/agent.py"}


def test_pre_harness_loaded_log_folds_with_an_empty_map(tmp_path):
    """A log written BEFORE the event existed folds to an empty map.

    The additive-field discipline: readers tolerate absence, so every
    pre-existing round log decodes and folds unchanged.
    """
    path = round_log_path(tmp_path, "epoch-01", 10)
    path.parent.mkdir(parents=True)
    lines = [
        {"seq": 1, "ts": "t", "type": "round_opened", "payload": {"contract_hash": "h"}},
        {"seq": 2, "ts": "t", "type": "patches_applied", "payload": {"generation_id": "v1"}},
        {"seq": 3, "ts": "t", "type": "round_closed", "payload": {}},
        # A harness_loaded from before the per-tree fields existed.
        {
            "seq": 4,
            "ts": "t",
            "type": "harness_loaded",
            "payload": {"generation_id": "v1", "entrypoint_file": "agent/agent.py"},
        },
    ]
    path.write_text("".join(json.dumps(rec) + "\n" for rec in lines), encoding="utf-8")

    record = fold_round_record(RoundLog(tmp_path, "epoch-01", 10).read())
    assert record.harness_entrypoint_files == {"v1": "agent/agent.py"}
    assert record.harness_never_imported_trees == {}
    assert record.complete is True
