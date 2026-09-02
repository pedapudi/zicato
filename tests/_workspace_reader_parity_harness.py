"""Snapshot oracle for every workspace reader OUTSIDE the query layer.

:mod:`tests._reader_parity_harness` pins the dashboard query layer's
``build_*`` responses against a small multi-epoch fixture. This module is its
sibling for the readers that walk the same ``.zicato/`` tree from
:mod:`zicato.analyzer`, :mod:`zicato.reflection`, :mod:`zicato.health`,
:mod:`zicato.index`, :mod:`zicato.workspace` and the ``zicato health``
command's own loaders in :mod:`zicato.cli`. Together the two snapshots cover
the whole read surface, so a change to any reader's output — its values or
the ORDER of its rows — shows up as a diff against a committed golden rather
than passing unobserved.

The fixture here is richer than the query harness's, because these readers
order things the query layer does not:

* **Eleven generations, ``v0`` through ``v10``.** Numeric-aware ordering puts
  ``v2`` before ``v10``; plain lexical ordering inverts them. Readers disagree
  today — :func:`zicato.workspace.reads.read_experiments` and the ``zicato
  health`` command sort numerically, while the index's generation walk sorts
  lexically — and the snapshot records which reader does which, so a
  consolidation onto one reader layer has to state, for each order, whether
  it preserves it or changes it.
* **Board entry ids ``t1``, ``t2``, ``t10`` and reflection ids ``r-2``,
  ``r-10``**, which put the same numeric-versus-lexical question to the
  per-entry and per-reflection walks.
* **Two epochs, ``e2`` (January) and ``e10`` (February)**, whose directory
  names sort in the opposite order to their recorded creation times under a
  lexical sort — the epoch-ordering axis the query harness exercises, carried
  here so the cross-epoch readers (the index, the lineage walk) are pinned on
  it too.
* **Per-run replicates**, including one slot in the pre-flight band the
  observation corpus must REFUSE, so the corpus reader's band filter is part
  of the pinned output.
* A per-round event log, a durable field-tournament snapshot, two board
  reflections, persisted loop-health reports, and per-generation
  mutated-tree-import provenance, so every reader below has real material
  rather than an empty directory.

Construction is fully deterministic: every timestamp, score and identifier is
a literal or derived from the generation index, and nothing reads the clock
or an RNG. The only wall-clock value any reader stamps onto its output is the
health report's ``checked_at``, which
:func:`tests._reader_parity_harness.mask_volatile` masks along with the
per-run absolute workspace root.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tests._reader_parity_harness import _MASK, _normalize_root, mask_volatile

# ---------------------------------------------------------------------------
# Fixture shape
# ---------------------------------------------------------------------------

#: Board entry ids. ``t2`` before ``t10`` numerically, the reverse lexically.
ENTRY_IDS: tuple[str, ...] = ("t1", "t2", "t10")

#: The rich epoch's generation ids, oldest first. ``v10`` is the id whose
#: lexical position (right after ``v1``) disagrees with its lineage position.
RICH_GENERATION_IDS: tuple[str, ...] = tuple(f"v{i}" for i in range(11))

#: The second epoch's generation ids.
SMALL_GENERATION_IDS: tuple[str, ...] = ("v0", "v1")

#: The rich epoch — eleven generations, replicates, reflections, rounds.
RICH_EPOCH_ID = "e2"

#: The second epoch — open, two generations, the workspace's current epoch.
SMALL_EPOCH_ID = "e10"

#: Both epochs in recorded-creation order. ``e2`` was created first even
#: though its directory name sorts after ``e10`` lexically.
EPOCH_IDS: tuple[str, ...] = (RICH_EPOCH_ID, SMALL_EPOCH_ID)

#: The reflection ids under the rich epoch, oldest first.
REFLECTION_IDS: tuple[str, ...] = ("r-2", "r-10")

#: The settled round indices under the rich epoch.
ROUND_INDICES: tuple[int, ...] = (1, 2, 10)

#: The candidates the fixture's observation corpus covers. Kept to the
#: generations that carry replicates, which is what gives the reliability
#: analyses more than one draw per unit to work with.
CORPUS_CANDIDATES: tuple[str, ...] = ("v0", "v1", "v2")

_EPOCH_CREATED_AT = {RICH_EPOCH_ID: "2026-01-01T00:00:00Z", SMALL_EPOCH_ID: "2026-02-01T00:00:00Z"}
_EPOCH_CLOSED_AT = {RICH_EPOCH_ID: "2026-01-31T00:00:00Z", SMALL_EPOCH_ID: ""}

#: A fixed timestamp for every record whose writer would otherwise stamp the
#: clock. Reading it back is what keeps the snapshot reproducible.
FIXED_TS = "2026-01-15T12:00:00+00:00"

#: The judges the board watches. ``tone_guard`` fires in the fixture's
#: captured judge I/O; ``fact_guard`` never fires, which is what makes the
#: reflection coverage reader report an untested judge.
JUDGE_NAMES: tuple[str, ...] = ("tone_guard", "fact_guard")

#: The mutable tree no unit of ``v3`` ever imported — the recorded gap that
#: makes the loop-health assessment emit a real finding.
NEVER_IMPORTED_TREE = "src/pkg/unused"


# ---------------------------------------------------------------------------
# Small write helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )


def _generation_index(generation_id: str) -> int:
    """The lineage position encoded in a ``vN`` id."""
    return int(generation_id[1:])


def _decision_for(generation_id: str) -> str:
    """The recorded tournament decision for one generation.

    ``v0`` is the seed and carries no outcome. Of the challengers, every
    third promotes, one defers, and the rest are rejected — enough of each
    for the outcome-counting readers to distinguish them.
    """
    index = _generation_index(generation_id)
    if index == 0:
        return ""
    if index == 6:
        return "deferred"
    return "promoted" if index % 3 == 1 else "rejected"


def _epoch_generation_ids(epoch_id: str) -> tuple[str, ...]:
    return RICH_GENERATION_IDS if epoch_id == RICH_EPOCH_ID else SMALL_GENERATION_IDS


# ---------------------------------------------------------------------------
# The contract: workspace config, epoch config, board, scoring
# ---------------------------------------------------------------------------


def _write_workspace_config(ws: Path) -> None:
    """The workspace-level ``config.json``.

    Carries the adapter block every workspace has, the ``runtime`` block the
    pre-flight-gate reader resolves its mode from, and a ``health`` block with
    non-default thresholds so the health config loader has something to read
    other than the built-in defaults.
    """
    _write_json(
        ws / "config.json",
        {
            "adapter": {
                "entrypoint": "pkg.module:agent",
                "mutable_trees": ["src/pkg", NEVER_IMPORTED_TREE],
            },
            "runtime": {"preflight_gate": "warn"},
            "health": {"stall_window": 4, "flat_scalar_epsilon": 0.001},
        },
    )


def _epoch_config(ws: Path, epoch_id: str) -> dict[str, Any]:
    """One epoch's ``config.json`` payload.

    The scoring block is produced by the epoch serializer rather than written
    by hand, so it stays a valid frozen contract as the scoring dataclass
    gains fields. The measured noise floor and the persisted pre-flight
    verdict are present on the rich epoch only: they are what the
    margin-versus-noise and pre-flight loop-health detectors read, and the
    second epoch's absence of them pins the silent path.
    """
    from zicato.core import ScoringWeights
    from zicato.epoch.lifecycle import scoring_to_dict

    rich = epoch_id == RICH_EPOCH_ID
    weights = ScoringWeights(promote_margin=0.01 if rich else 0.05)
    return {
        "format_version": 1,
        "id": epoch_id,
        "name": f"epoch {epoch_id}",
        "created_at": _EPOCH_CREATED_AT[epoch_id],
        "board_path": str(ws / "epochs" / epoch_id / "board.jsonl"),
        "brief_path": str(ws / "epochs" / epoch_id / "brief.md"),
        "scoring": scoring_to_dict(weights),
        "closed": rich,
        "closed_at": _EPOCH_CLOSED_AT[epoch_id],
        "contract_hash": f"contract-{epoch_id}",
        "goal": f"Reduce drift under the {epoch_id} contract.",
        "proposer_path": None,
        "noise_floor": ({"max_abs_delta": 0.04, "delta_std": 0.012, "runs": 8} if rich else None),
        "preflight": (
            {
                "verdict": "proceed",
                "checked_at": FIXED_TS,
                "saturated_entry_ids": ["t10"],
            }
            if rich
            else None
        ),
        "applied_proposer_recommendations": [],
    }


def _board_lines() -> list[dict[str, Any]]:
    """The frozen board: a meta header plus one entry per board-entry id.

    ``t1`` carries a rubric expectation and both judges, ``t2`` a predicate
    and one judge, ``t10`` a regex expectation and no judge — so the board
    readers see all three expectation kinds and a judge roster that is not
    uniform across entries.
    """
    return [
        {"board_meta": True, "disable_drift": ["looping_tool_call"]},
        {
            "id": "t1",
            "kind": "single_turn",
            "input": "Summarise the release notes.",
            "expectation": {"kind": "rubric", "spec": "Mentions every shipped change."},
            "weight": 1.0,
            "tags": ["smoke", "summary"],
            "wall_clock_budget_seconds": 60,
            "judges": [
                {
                    "name": "tone_guard",
                    "mode": "inline",
                    "body": "Flag a dismissive tone.",
                    "severity": "warning",
                },
                {
                    "name": "fact_guard",
                    "mode": "inline",
                    "body": "Flag an unsupported claim.",
                    "severity": "critical",
                },
            ],
        },
        {
            "id": "t2",
            "kind": "single_turn",
            "input": "List the open defects.",
            "expectation": {"kind": "predicate", "spec": "pkg.checks:lists_defects"},
            "weight": 2.0,
            "tags": ["defects"],
            "wall_clock_budget_seconds": 90,
            "judges": [
                {
                    "name": "tone_guard",
                    "mode": "inline",
                    "body": "Flag a dismissive tone.",
                    "severity": "warning",
                }
            ],
        },
        {
            "id": "t10",
            "kind": "single_turn",
            "input": "Name the release date.",
            "expectation": {"kind": "regex", "spec": "\\d{4}-\\d{2}-\\d{2}"},
            "weight": 0.5,
            "tags": [],
            "wall_clock_budget_seconds": 30,
        },
    ]


# ---------------------------------------------------------------------------
# Per-run artifacts
# ---------------------------------------------------------------------------


def _loss_profile(epoch_id: str, generation_id: str, entry_id: str, replicate: int) -> Any:
    """One run's reducer output, built through the canonical dataclasses.

    Every number is a function of the lineage position, the entry and the
    replicate index, so the same fixture always produces the same profile
    and the per-generation trends the readers compute are non-flat.
    """
    from zicato.core.loss import DriftCount, ExpectationResult, JudgeLoss, LossProfile

    index = _generation_index(generation_id)
    entry_rank = ENTRY_IDS.index(entry_id)
    base = 0.40 - 0.02 * index + 0.05 * entry_rank + 0.01 * replicate
    passed = (index + entry_rank) % 4 != 3
    judges = (
        (JudgeLoss(judge_name="tone_guard", raw_loss=1.0, weight=3.0, weighted_loss=3.0),)
        if entry_id in ("t1", "t2")
        else ()
    )
    return LossProfile(
        run_id=f"{epoch_id}-{generation_id}-{entry_id}-r{replicate}",
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id=epoch_id,
        drift_counts=(
            DriftCount(kind="plan_thrash", severity="warning", count=index % 3),
            DriftCount(kind="custom:tone_guard", severity="warning", count=1 if judges else 0),
        ),
        plan_revisions=index % 4,
        task_failure_ratio=0.0,
        runtime_ms=1000 + 10 * index + entry_rank,
        wall_clock_budget_exceeded=False,
        expectation_result=ExpectationResult(
            kind="rubric" if entry_id == "t1" else "predicate",
            passed=passed,
            detail="" if passed else "expectation not met",
        ),
        drift_loss=round(base, 6),
        pass_fail=passed,
        per_judge_loss=judges,
        score=round(1.0 - base, 6),
        tokens_spent=500 + 25 * index,
        output_chars=1200 + 40 * index,
    )


def _events_lines(generation_id: str, entry_id: str) -> list[dict[str, Any]]:
    """One run's goldfive-shaped telemetry.

    Carries one event of each decision-telemetry payload the analyzer's
    aggregator counts, plus the ``plan_revised`` and ``drift_detected``
    events the process-exemplar extractor anchors its windows on.
    """
    index = _generation_index(generation_id)
    run_id = f"{generation_id}-{entry_id}"
    return [
        {
            "eventId": f"{run_id}-1",
            "runId": run_id,
            "sequence": 1,
            "ladderTransitionDecided": {
                "fromLevel": "" if index == 0 else "light",
                "toLevel": "deep",
                "reason": "drift observed",
            },
        },
        {
            "eventId": f"{run_id}-2",
            "runId": run_id,
            "sequence": 2,
            "detectorDispatchOrdered": {"dispatchOrder": ["tone_guard", "fact_guard"]},
        },
        {
            "eventId": f"{run_id}-3",
            "runId": run_id,
            "sequence": 3,
            "policyApplied": {"policyName": "retry_once", "outcome": "applied"},
        },
        {
            "eventId": f"{run_id}-4",
            "runId": run_id,
            "sequence": 4,
            "retryBudgetSpent": {"operation": "tool_call", "attempt": 1 + index % 2},
        },
        {
            "eventId": f"{run_id}-5",
            "runId": run_id,
            "sequence": 5,
            "steeringDecisionMade": {"detectorName": "plan_thrash", "outcome": "steered"},
        },
        {
            "eventId": f"{run_id}-6",
            "runId": run_id,
            "sequence": 6,
            "planRevised": {
                "reason": "the first plan missed a requirement",
                "revisionIndex": 1,
            },
        },
        {
            "eventId": f"{run_id}-7",
            "runId": run_id,
            "sequence": 7,
            "driftDetected": {
                "kind": "plan_thrash",
                "severity": "warning",
                "currentAgentId": "planner",
            },
        },
    ]


def _write_run(ws: Path, epoch_id: str, generation_id: str, entry_id: str) -> None:
    """Write one ``(generation, entry)`` run directory.

    The canonical replicate-0 slot always lands. The rich epoch's first two
    generations additionally carry replicate 1 (a legitimate further draw of
    the same duel) and replicate 2000 — a contract pre-flight probe, which the
    observation corpus must refuse because it describes deliberately degraded
    code cached under the champion's own id.
    """
    from zicato.judge_runtime.io_capture import build_judge_io_record
    from zicato.telemetry.reducer import write_loss_profile
    from zicato.tournament.unit_cache import RUN_RESULT_FORMAT_VERSION

    run_dir = ws / "epochs" / epoch_id / "generations" / generation_id / "runs" / entry_id
    run_dir.mkdir(parents=True, exist_ok=True)

    write_loss_profile(_loss_profile(epoch_id, generation_id, entry_id, 0), run_dir / "loss.json")
    _write_jsonl(run_dir / "events.jsonl", _events_lines(generation_id, entry_id))

    index = _generation_index(generation_id)
    replicated = epoch_id == RICH_EPOCH_ID and index <= 1
    if replicated:
        write_loss_profile(
            _loss_profile(epoch_id, generation_id, entry_id, 1), run_dir / "loss.r1.json"
        )
        _write_jsonl(run_dir / "events.r1.jsonl", _events_lines(generation_id, entry_id))
        # A pre-flight probe cached beside the real draws. Every reader that
        # asks what this generation DID must leave it out.
        write_loss_profile(
            _loss_profile(epoch_id, generation_id, entry_id, 2000), run_dir / "loss.r2000.json"
        )

    # The capture sidecars decide the observation corpus' fidelity tier:
    # a judge-I/O sidecar is verbatim, a result capture alone is one tier
    # lower, neither is a preview. The fixture carries one of each.
    if entry_id == "t1":
        _write_json(
            run_dir / "result.json",
            {
                "format_version": RUN_RESULT_FORMAT_VERSION,
                "final_output": f"summary from {generation_id}",
                "transcript": ["user: summarise", f"agent: summary from {generation_id}"],
                "clipped": False,
            },
        )
        _write_jsonl(
            run_dir / "judge_io.jsonl",
            [
                build_judge_io_record(
                    judge_name="tone_guard",
                    call_index=0,
                    reasoning_text=f"tone review for {generation_id}",
                    transcript_window=("agent: summary",),
                    raw_response="FIRE warning dismissive phrasing",
                    drift_emitted=True,
                    kind="custom:tone_guard",
                    severity="warning",
                    detail="dismissive phrasing in the closing line",
                    ts=FIXED_TS,
                )
            ],
        )
    elif entry_id == "t2":
        _write_json(
            run_dir / "result.json",
            {
                "format_version": RUN_RESULT_FORMAT_VERSION,
                "final_output": f"defect list from {generation_id}",
                "transcript": [],
                "clipped": False,
            },
        )

    # One run keeps an archived predecessor, so the retained-measurement
    # reader has two measurements to return rather than one.
    if epoch_id == RICH_EPOCH_ID and generation_id == "v0" and entry_id == "t1":
        _write_jsonl(run_dir / "events.prev.jsonl", _events_lines(generation_id, entry_id)[:3])


# ---------------------------------------------------------------------------
# Per-generation artifacts
# ---------------------------------------------------------------------------


def _experiment(epoch_id: str, generation_id: str) -> dict[str, Any]:
    """One generation's ``experiment.json``.

    ``v0`` is the seed: it records a hypothesis-free baseline with no
    outcome, which is the shape the readers must render as "baseline"
    rather than "pending".
    """
    index = _generation_index(generation_id)
    record: dict[str, Any] = {
        "generation_id": generation_id,
        "epoch_id": epoch_id,
        "parent_generation_id": None if index == 0 else f"v{index - 1}",
        "proposed_at": f"2026-01-{index + 1:02d}T00:00:00Z",
        "patch_ids": [] if index == 0 else [f"p{index}"],
    }
    if index == 0:
        return record
    record["hypothesis"] = {
        "summary": f"Attempt {index}: tighten the planning prompt.",
        "core_idea": f"Rewrite the planner instruction block for round {index}.",
        "why": "Plan thrash dominates the drift profile.",
        "risks": "A shorter instruction may drop the citation requirement.",
        "modulating": ["prompt.planner"],
        "expected_pass_rate_delta": "+0.05",
        "expected_drift_movements": [
            {"kind": "plan_thrash", "direction": "down", "magnitude": "moderate"}
        ],
        "expected_metric_movements": [
            {"metric_name": "cost:tokens_spent", "direction": "down", "magnitude": "small"}
        ],
    }
    decision = _decision_for(generation_id)
    record["round_index"] = index
    record["id"] = f"exp-{epoch_id}-{generation_id}"
    record["outcome"] = {
        "ran_at": f"2026-01-{index + 1:02d}T06:00:00Z",
        "tournament_decision": decision,
        "structure": "gauntlet",
        "rejection_reason": "" if decision == "promoted" else "below promote margin",
        "scalar_score_delta": round(-0.02 if decision == "promoted" else 0.005, 6),
        "drift_loss_delta": round(-0.02 * index, 6),
        "pass_rate_delta": round(0.01 * index, 6),
        "drift_movements": [
            {
                "kind": "plan_thrash",
                "from_rate": 3.0,
                "to_rate": 2.0,
                "hypothesis_match": True,
                "note": "",
            }
        ],
        "metric_movements": [
            {
                "metric_name": "cost:tokens_spent",
                "from_value": 500,
                "to_value": 480,
                "hypothesis_match": True,
                "note": "",
            }
        ],
        "generalization_gap": {"train_delta": -0.02, "holdout_delta": 0.01},
    }
    return record


def _write_generation(ws: Path, epoch_id: str, generation_id: str) -> None:
    gen_dir = ws / "epochs" / epoch_id / "generations" / generation_id
    index = _generation_index(generation_id)

    _write_json(gen_dir / "experiment.json", _experiment(epoch_id, generation_id))

    if index > 0:
        _write_json(
            gen_dir / "patches" / f"p{index}.json",
            {
                "id": f"p{index}",
                "mutation_id": "prompt.planner" if index % 2 else "prompt.system",
                "op": "replace",
                "new_content": f"short instruction {index}",
                "new_numeric": None,
                "new_enum": None,
                "rationale": f"Round {index}: shorten the instruction block.",
            },
        )

    scalar = round(0.50 - 0.01 * index, 6)
    aggregate = {
        "generation_id": generation_id,
        "scalar": scalar,
        "pass_rate": round(0.60 + 0.02 * index, 6),
        "mean_drift_loss": round(0.40 - 0.02 * index, 6),
        "per_entry": [
            {"entry_id": entry_id, "drift_loss": round(0.40 - 0.02 * index, 6)}
            for entry_id in ENTRY_IDS
        ],
    }
    _write_json(gen_dir / "gen_score.json", aggregate)
    # The append-only archive keeps the measurements a re-scoring overwrote.
    # ``v0`` is measured again each time it defends, so it carries two.
    history = [dict(aggregate, seq=1, round_index=max(index, 1))]
    if index == 0:
        history.append(dict(aggregate, seq=2, round_index=2, scalar=round(scalar + 0.004, 6)))
    _write_jsonl(gen_dir / "gen_score.history.jsonl", history)

    # Snapshot-origin provenance. One generation records a mutable tree no
    # unit ever imported, which is the recorded gap the loop-health
    # assessment turns into a finding.
    _write_json(
        gen_dir / "harness_load.json",
        {
            "generation_id": generation_id,
            "entrypoint_file": "src/pkg/agent.py",
            "trees_never_imported": (
                [NEVER_IMPORTED_TREE] if epoch_id == RICH_EPOCH_ID and index == 3 else []
            ),
        },
    )

    for entry_id in ENTRY_IDS:
        _write_run(ws, epoch_id, generation_id, entry_id)


# ---------------------------------------------------------------------------
# Per-epoch artifacts beyond the contract
# ---------------------------------------------------------------------------


def _write_round_logs(ws: Path, epoch_id: str) -> None:
    """Write one durable event log per settled round of the rich epoch.

    Written through :class:`zicato.epoch.round_log.RoundLog`, the log's own
    writer, so the wire records stay valid as the event vocabulary grows. The
    writer stamps each envelope with the clock, but the fold that every reader
    consumes discards the stamp, so the snapshot stays reproducible.
    """
    from zicato.epoch.round_log import (
        DecisionRecorded,
        ExperimentMinted,
        GateEvaluated,
        HarnessLoaded,
        PatchesApplied,
        ProposalAttempted,
        RoundClosed,
        RoundLog,
        RoundOpened,
        UnitCompleted,
    )

    for round_index in ROUND_INDICES:
        generation_id = f"v{round_index}"
        log = RoundLog(ws, epoch_id, round_index)
        log.append(RoundOpened(contract_hash=f"contract-{epoch_id}"))
        log.append(ProposalAttempted(errors=(), slot_index=0))
        log.append(ExperimentMinted(experiment_id=f"exp-{round_index}"))
        log.append(PatchesApplied(generation_id=generation_id))
        log.append(
            HarnessLoaded(
                generation_id=generation_id,
                entrypoint_file="src/pkg/agent.py",
                trees_never_imported=(),
            )
        )
        decision = _decision_for(generation_id)
        for entry_id in ENTRY_IDS:
            for side in ("champion", "challenger"):
                log.append(UnitCompleted(entry_id=entry_id, replicate=0, side=side))
        log.append(
            GateEvaluated(
                rule_fired="" if decision == "promoted" else "insufficient improvement",
                decision=decision,
                champion_scalar=0.50,
                challenger_scalar=round(0.50 - 0.01 * round_index, 6),
                margin_required=0.01,
            )
        )
        log.append(
            DecisionRecorded(
                decision=decision,
                provenance={"decided_by": "gate", "round_index": round_index},
            )
        )
        log.append(RoundClosed())


def _write_field_tournament(ws: Path, epoch_id: str) -> None:
    """Write one round's durable field-tournament snapshot.

    Three competitors, so the index projects a field-level row rather than
    treating the round as a two-way gauntlet.
    """
    _write_json(
        ws / "epochs" / epoch_id / "tournaments" / "field-v4.json",
        {
            "tournament_id": f"{epoch_id}:field:v4",
            "epoch_id": epoch_id,
            "first_challenger_id": "v4",
            "decision": "promoted",
            "reason": "",
            "delta_scalar": -0.02,
            "ran_at": "2026-01-05T00:00:00Z",
            "structure": "swiss",
            "structure_params": {"rounds": 2},
            "competitors": ["v3", "v4", "v5"],
            "rounds": [
                {"round": 1, "pairings": [{"a": "v3", "b": "v4", "winner": "v4"}]},
                {"round": 2, "pairings": [{"a": "v4", "b": "v5", "winner": "v4"}]},
            ],
            "standings": [
                {"generation_id": "v4", "wins": 2, "losses": 0, "status": "champion"},
                {"generation_id": "v3", "wins": 0, "losses": 1, "status": "eliminated"},
                {"generation_id": "v5", "wins": 0, "losses": 1, "status": "eliminated"},
            ],
            "field_status": [
                {"generation_id": "v3", "status": "eliminated"},
                {"generation_id": "v4", "status": "champion"},
                {"generation_id": "v5", "status": "eliminated"},
            ],
        },
    )


def _write_reflections(ws: Path, epoch_id: str) -> None:
    """Write two board-reflection records under the rich epoch.

    The ids ``r-2`` and ``r-10`` put the numeric-versus-lexical question to
    the reflection walk. Both carry a pre-registered plan; only the later one
    carries the scorecards, findings and summary a completed run produces, so
    the readers' partial-record path is pinned alongside the complete one.
    """
    from zicato.reflection.corpus import write_corpus

    for reflection_id in REFLECTION_IDS:
        complete = reflection_id == "r-10"
        base = ws / "epochs" / epoch_id / "reflections" / reflection_id
        _write_json(
            base / "plan.json",
            {
                "reflection_id": reflection_id,
                "epoch_id": epoch_id,
                "created_at": f"2026-01-{2 if reflection_id == 'r-2' else 10:02d}T00:00:00Z",
                "mode": "passive" if not complete else "active",
                "executed": complete,
                "candidates": ["v0", "v1"],
                "entries": list(ENTRY_IDS),
                "replicates": 2,
                "adjudicator": "meta_judge",
                "checks": ["reliability", "discrimination"],
            },
        )
        if not complete:
            continue
        _write_json(
            base / "scorecards.json",
            [
                {
                    "judge_name": "tone_guard",
                    "tp": 4,
                    "fp": 1,
                    "fn": 2,
                    "tn": 9,
                    "ambiguous": 1,
                    "precision": 0.8,
                    "recall": 0.667,
                },
                {
                    "judge_name": "fact_guard",
                    "tp": 0,
                    "fp": 0,
                    "fn": 0,
                    "tn": 0,
                    "ambiguous": 0,
                    "precision": None,
                    "recall": None,
                },
            ],
        )
        _write_json(
            base / "findings.json",
            [
                {
                    "id": "finding-1",
                    "severity": "warning",
                    "summary": "tone_guard misses dismissive closings.",
                    "evidence": {"observed": 2, "expected": 6},
                },
                {
                    "id": "finding-2",
                    "severity": "info",
                    "summary": "t10 never differentiates the candidates.",
                    "evidence": {"spread": 0.0},
                },
            ],
        )
        _write_json(
            base / "summary.json",
            {"noise_floor_max_abs_delta": 0.04, "decision_flip_p": 0.18},
        )
        # The frozen observation corpus, built by the ingest that produces it
        # in a real run, so the persisted records are the ones the reader
        # returns.
        write_corpus(ws, epoch_id, reflection_id, _fixture_corpus(ws, epoch_id, reflection_id))
        _write_json(
            base / "adjudication" / "tone_guard" / "v0:t1:r0.json",
            {
                "judge_name": "tone_guard",
                "run_ref": "v0:t1:r0",
                "verdict": "tp",
                "rationale": "The closing line is dismissive.",
                "adjudicated_at": FIXED_TS,
            },
        )


def _fixture_corpus(ws: Path, epoch_id: str, reflection_id: str) -> list[Any]:
    """The passive observation corpus over the epoch's first three candidates.

    A pure read of the already-persisted run artifacts: the ingest copies
    nothing and spends no model budget, so calling it from the fixture builder
    and from the capture yields the same records.
    """
    from zicato.core import ScoringWeights
    from zicato.reflection.corpus import ingest_lineage

    return ingest_lineage(
        workspace_root=ws,
        epoch_id=epoch_id,
        reflection_id=reflection_id,
        candidates=CORPUS_CANDIDATES,
        entries=ENTRY_IDS,
        weights=ScoringWeights(),
    )


def _write_health_reports(ws: Path, epoch_id: str) -> None:
    """Write the persisted per-round loop-health reports.

    Rounds 1 and 10, so the "latest report" resolution has to compare the
    round numbers rather than the filenames.
    """
    for round_index in (1, 10):
        _write_json(
            ws / "epochs" / epoch_id / "health" / f"round_{round_index}.json",
            {
                "epoch_id": epoch_id,
                "checked_at": f"2026-01-{round_index:02d}T00:00:00Z",
                "healthy": round_index == 1,
                "findings": (
                    []
                    if round_index == 1
                    else [
                        {
                            "code": "tree_never_imported",
                            "severity": "warning",
                            "summary": f"{NEVER_IMPORTED_TREE} was never imported.",
                            "detail": {"generation_id": "v3", "tree": NEVER_IMPORTED_TREE},
                        }
                    ]
                ),
            },
        )


def _write_epoch(ws: Path, epoch_id: str) -> None:
    edir = ws / "epochs" / epoch_id
    _write_json(edir / "config.json", _epoch_config(ws, epoch_id))
    _write_jsonl(edir / "board.jsonl", _board_lines())
    _write_json(edir / "scoring.json", _epoch_config(ws, epoch_id)["scoring"])
    _write_text(
        edir / "brief.md",
        f"# Brief {epoch_id}\n\n## Goal\n\nReduce plan thrash without losing citations.\n",
    )
    _write_text(
        edir / "journal.md",
        f"# Journal {epoch_id}\n\nRound 1 promoted v1. Round 2 rejected v2.\n",
    )
    _write_json(
        edir / "contract_components.json",
        {
            "board": f"board-{epoch_id}",
            "brief": f"brief-{epoch_id}",
            "scoring": "scoring-const",
            "entrypoint": "entry-const",
            "mutable_trees": "trees-const",
            "proposer": "proposer-const",
        },
    )
    _write_json(
        edir / "mutations.json",
        [
            {
                "id": "prompt.system",
                "kind": "text_block",
                "file": "src/pkg/agent.py",
                "line": 12,
                "preview": "You are a release-notes assistant.",
            },
            {
                "id": "prompt.planner",
                "kind": "text_block",
                "file": "src/pkg/planner.py",
                "line": 40,
                "preview": "Plan before answering.",
            },
        ],
    )

    for round_index in ROUND_INDICES if epoch_id == RICH_EPOCH_ID else (1,):
        _write_text(
            edir / "insights" / f"round_{round_index:04d}.md",
            f"# Round {round_index} insights\n\nThe ladder escalated on plan thrash.\n",
        )

    for generation_id in _epoch_generation_ids(epoch_id):
        _write_generation(ws, epoch_id, generation_id)

    if epoch_id == RICH_EPOCH_ID:
        _write_round_logs(ws, epoch_id)
        _write_field_tournament(ws, epoch_id)
        _write_reflections(ws, epoch_id)
        _write_health_reports(ws, epoch_id)
        _write_text(edir / "current_generation", "v10\n")


def _write_lineage(ws: Path) -> None:
    """The workspace-level lineage record.

    Generations are listed oldest-first within each epoch. The second epoch
    records the first as its parent, so the index's ancestry selector has a
    chain to walk.
    """
    _write_json(
        ws / "lineage.json",
        {
            "epochs": [
                {
                    "id": epoch_id,
                    "parent_epoch_id": None if epoch_id == RICH_EPOCH_ID else RICH_EPOCH_ID,
                    "created_at": _EPOCH_CREATED_AT[epoch_id],
                    "generations": [
                        {
                            "id": generation_id,
                            "parent_id": (
                                None
                                if _generation_index(generation_id) == 0
                                else f"v{_generation_index(generation_id) - 1}"
                            ),
                            "promoted": _decision_for(generation_id) == "promoted",
                            "round_index": _generation_index(generation_id),
                            "created_at": f"2026-01-{_generation_index(generation_id) + 1:02d}"
                            "T00:00:00Z",
                        }
                        for generation_id in _epoch_generation_ids(epoch_id)
                    ],
                }
                for epoch_id in EPOCH_IDS
            ]
        },
    )


def build_reader_fixture_workspace(tmp_path: Path) -> Path:
    """Materialize the fixture workspace and return its inner ``.zicato`` root.

    See the module docstring for what the fixture contains and why each part
    is there. Construction is deterministic and touches neither the clock nor
    an RNG, so two calls with different ``tmp_path`` values differ only in the
    absolute paths the readers echo back.
    """
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True, exist_ok=True)
    _write_workspace_config(ws)
    _write_text(ws / "current_epoch", f"{SMALL_EPOCH_ID}\n")
    for epoch_id in EPOCH_IDS:
        _write_epoch(ws, epoch_id)
    _write_lineage(ws)
    # The derived SQLite index is part of a materialized workspace: the
    # reflection mining reader consults it for the candidate axis, so a
    # fixture without one would pin that reader's degraded path instead of
    # its real one.
    from zicato.index.ingest import rebuild_index

    rebuild_index(ws)
    return ws


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """Reduce any reader's return value to plain JSON types.

    The readers return dataclasses, tuples, ``sqlite3.Row`` objects and
    ``Path`` objects as well as plain dicts. Rendering them all into the same
    JSON shape is what lets one golden pin readers from six packages, and
    keeps a change of container type (a tuple becoming a list) from reading as
    a behavior change when the contents are identical.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, sqlite3.Row):
        return {key: to_jsonable(value[key]) for key in value.keys()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, str | bytes):
        return value.decode("utf-8") if isinstance(value, bytes) else value
    if isinstance(value, set | frozenset):
        return sorted(to_jsonable(v) for v in value)
    if isinstance(value, Sequence):
        return [to_jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _capture_workspace_reads(ws: Path, snap: dict[str, Any]) -> None:
    """The typed canonical-read layer: enumeration, ordering and leaf reads."""
    from zicato import workspace as wsp

    layout = wsp.WorkspaceLayout.from_root(ws)
    snap["zicato.workspace.list_epoch_ids"] = wsp.list_epoch_ids(layout)
    snap["zicato.workspace.iter_epochs"] = wsp.iter_epochs(layout)
    for epoch_id in EPOCH_IDS:
        snap[f"zicato.workspace.read_epoch_config::{epoch_id}"] = wsp.read_epoch_config(
            layout, epoch_id
        )
        snap[f"zicato.workspace.read_board::{epoch_id}"] = wsp.read_board(layout, epoch_id)
        snap[f"zicato.workspace.read_experiments::{epoch_id}"] = wsp.read_experiments(
            layout, epoch_id
        )
        # The three record enumerations every other reader in the tree now
        # calls. Pinning them here means one golden fixes the order the
        # whole read surface presents generations, board-entry runs and
        # rounds in.
        snap[f"zicato.workspace.generation_ids::{epoch_id}"] = wsp.generation_ids(layout, epoch_id)
        snap[f"zicato.workspace.round_indices::{epoch_id}"] = wsp.round_indices(layout, epoch_id)
        for generation_id in _epoch_generation_ids(epoch_id):
            snap[f"zicato.workspace.run_entry_ids::{epoch_id}::{generation_id}"] = (
                wsp.run_entry_ids(layout, epoch_id, generation_id)
            )
    epoch_id = RICH_EPOCH_ID
    for generation_id in ("v0", "v2", "v10"):
        snap[f"zicato.workspace.read_experiment::{generation_id}"] = wsp.read_experiment(
            layout, epoch_id, generation_id
        )
        snap[f"zicato.workspace.read_gen_score::{generation_id}"] = wsp.read_gen_score(
            layout, epoch_id, generation_id
        )
        snap[f"zicato.workspace.read_gen_score_history::{generation_id}"] = (
            wsp.read_gen_score_history(layout, epoch_id, generation_id)
        )
        snap[f"zicato.workspace.read_loss::{generation_id}"] = wsp.read_loss(
            layout, epoch_id, generation_id, "t1"
        )
    snap["zicato.workspace.read_events_history::v0::t1"] = wsp.read_events_history(
        layout, epoch_id, "v0", "t1"
    )


def _capture_analyzer(ws: Path, snap: dict[str, Any]) -> None:
    """The analyzer's report gathering, telemetry aggregation and insights."""
    from zicato.analyzer import aggregate_decision_events, gather_epoch_report_data
    from zicato.analyzer.insights import _collect_events_jsonl_paths, load_latest_insights
    from zicato.analyzer.process_exemplars import extract_process_exemplars
    from zicato.analyzer.report_data import load_mutation_surface
    from zicato.core.patterns import Pattern
    from zicato.workspace import WorkspaceLayout

    layout = WorkspaceLayout.from_root(ws)
    for epoch_id in EPOCH_IDS:
        snap[f"zicato.analyzer.gather_epoch_report_data::{epoch_id}"] = gather_epoch_report_data(
            ws, epoch_id
        )
        snap[f"zicato.analyzer.load_mutation_surface::{epoch_id}"] = load_mutation_surface(
            layout, epoch_id
        )
        snap[f"zicato.analyzer.load_latest_insights::{epoch_id}"] = load_latest_insights(
            ws, epoch_id
        )
        # The walk that finds the epoch's telemetry files has no public entry
        # point of its own, and its ORDER is what the aggregator accumulates
        # in, so it is captured under its own label: a reordering of the walk
        # would otherwise be invisible.
        paths = _collect_events_jsonl_paths(ws, epoch_id)
        snap[f"zicato.analyzer.collect_events_jsonl_paths::{epoch_id}"] = paths
        snap[f"zicato.analyzer.aggregate_decision_events::{epoch_id}"] = aggregate_decision_events(
            paths
        )

    # One pattern per anchor kind the extractor knows how to localize, so the
    # exemplar windows and their entry-scan order are both pinned.
    patterns = [
        Pattern(
            id="pat-plan",
            kind="plan_revision_instability",
            summary="Plans are revised repeatedly.",
            detail={"affected_entry_ids": "t1,t2,t10"},
            severity="warning",
        ),
        Pattern(
            id="pat-drift",
            kind="drift_kind_frequency",
            summary="plan_thrash dominates the drift profile.",
            detail={"drift_kind": "plan_thrash", "affected_entry_ids": "t2,t10"},
            severity="warning",
        ),
    ]
    snap["zicato.analyzer.extract_process_exemplars"] = extract_process_exemplars(
        ws,
        RICH_EPOCH_ID,
        patterns,
        parent_generation_id="v0",
        train_entry_ids=ENTRY_IDS,
    )


def _capture_reflection(ws: Path, snap: dict[str, Any]) -> None:
    """The board-reflection corpus, episode mining and the pure analyses.

    The LLM-calling surfaces (adjudication, synthesis, the practice review)
    are absent by design: they are not workspace readers, and pinning them
    would require a model.
    """
    from zicato.query import WorkspacePaths
    from zicato.reflection import analysis
    from zicato.reflection.corpus import read_corpus
    from zicato.reflection.mining import mine_episodes

    epoch_id = RICH_EPOCH_ID
    corpus = _fixture_corpus(ws, epoch_id, "r-10")
    snap["zicato.reflection.ingest_lineage"] = corpus
    snap["zicato.reflection.read_corpus"] = read_corpus(ws, epoch_id, "r-10")

    snap["zicato.reflection.noise_floor_summary"] = analysis.noise_floor_summary(
        corpus=corpus,
        epoch_noise_floor={"max_abs_delta": 0.04, "delta_std": 0.012, "runs": 8},
        epoch_preflight={"verdict": "proceed"},
    )
    snap["zicato.reflection.entry_differentiation"] = analysis.entry_differentiation(corpus=corpus)
    snap["zicato.reflection.entry_candidate_matrix"] = analysis.entry_candidate_matrix(
        corpus=corpus
    )
    snap["zicato.reflection.judge_self_consistency"] = analysis.judge_self_consistency(
        corpus=corpus
    )
    snap["zicato.reflection.coverage"] = analysis.coverage(
        corpus=corpus,
        board_kinds=["plan_thrash", "looping_tool_call"],
        board_judges=JUDGE_NAMES,
    )

    paths = WorkspacePaths(ws)
    snap["zicato.reflection.mine_episodes"] = _stabilize_episode_ids(
        [episode.to_json() for episode in mine_episodes(paths, epoch_id)]
    )


def _stabilize_episode_ids(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rename each mined episode's content hash to a first-appearance label.

    A mined episode's id is a sha256 over its kind, its subject and the
    absolute paths of the artifacts it references. The paths change with the
    temporary directory the fixture is built in, so the digest does too — the
    same per-run variation the workspace-root normalization handles for every
    other path-bearing value, except that this reader hashes the paths before
    the normalization can reach them. Renaming the distinct ids to
    ``episode-1``, ``episode-2`` … in the order they appear preserves
    everything the golden can still check: how many distinct episodes there
    are, which rows share an id, and the rank order. Only the opaque digest
    is dropped.
    """
    labels: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for episode in episodes:
        raw = str(episode.get("episode_id", ""))
        if raw not in labels:
            labels[raw] = f"episode-{len(labels) + 1}"
        out.append(dict(episode, episode_id=labels[raw]))
    return out


def _capture_health(ws: Path, snap: dict[str, Any]) -> None:
    """The loop-health inputs, the CLI's own loaders, and the findings builder."""
    from zicato.cli.commands.health import (
        _generation_ids,
        _load_board,
        _load_experiments,
        _load_losses_by_generation,
        _max_generations_per_contract,
    )
    from zicato.health.diagnostics import assess_loop_health
    from zicato.health.inputs import (
        epoch_noise_floor_inputs,
        epoch_preflight_record,
        epoch_tree_import_gaps,
        workspace_preflight_gate,
    )

    snap["zicato.health.workspace_preflight_gate"] = workspace_preflight_gate(ws)
    for epoch_id in EPOCH_IDS:
        snap[f"zicato.health.epoch_tree_import_gaps::{epoch_id}"] = epoch_tree_import_gaps(
            ws, epoch_id
        )
        snap[f"zicato.health.epoch_noise_floor_inputs::{epoch_id}"] = epoch_noise_floor_inputs(
            ws, epoch_id
        )
        snap[f"zicato.health.epoch_preflight_record::{epoch_id}"] = epoch_preflight_record(
            ws, epoch_id
        )

        # The ``zicato health`` command's own workspace walks. Its generation
        # enumeration sorts numerically, which is what puts ``v2`` before
        # ``v10`` in every window-based detector's view of the lineage.
        generation_ids = _generation_ids(ws, epoch_id)
        snap[f"zicato.cli.health.generation_ids::{epoch_id}"] = generation_ids
        board = _load_board(ws, epoch_id)
        snap[f"zicato.cli.health.load_board::{epoch_id}"] = board
        losses = _load_losses_by_generation(ws, epoch_id, generation_ids, board)
        snap[f"zicato.cli.health.load_losses_by_generation::{epoch_id}"] = losses
        experiments = _load_experiments(ws, epoch_id, generation_ids)
        snap[f"zicato.cli.health.load_experiments::{epoch_id}"] = experiments

        noise_floor, promote_margin, evidence_gate_on = epoch_noise_floor_inputs(ws, epoch_id)
        report = assess_loop_health(
            losses_by_generation=losses,
            experiments=experiments,
            board_entries=board,
            epoch_id=epoch_id,
            max_generations_per_contract=_max_generations_per_contract(ws, epoch_id),
            noise_floor=noise_floor,
            promote_margin=promote_margin,
            evidence_gate_on=evidence_gate_on,
            preflight=epoch_preflight_record(ws, epoch_id),
            preflight_gate=workspace_preflight_gate(ws),
            tree_import_gaps=epoch_tree_import_gaps(ws, epoch_id) or None,
        )
        # The report stamps the moment it was taken. That is the only
        # wall-clock value any pinned reader derives rather than reads, so it
        # is masked HERE — on this one field of this one report — rather than
        # by key name everywhere: the fixture's persisted pre-flight verdict
        # and per-round health reports carry a ``checked_at`` of their own that
        # is read off disk and must stay pinned.
        snap[f"zicato.health.assess_loop_health::{epoch_id}"] = dict(
            to_jsonable(report), checked_at=_MASK
        )


def _capture_index(ws: Path, snap: dict[str, Any]) -> None:
    """A fresh SQLite index built from the fixture, then every read selector.

    The whole-index dump already has a parity gate of its own over the
    mock-evolve workspace; what this adds is a per-selector label over a
    workspace rich enough for the selectors' ORDER BY clauses to matter.
    """
    from zicato.index import query as iq
    from zicato.index.ingest import rebuild_index, validate_index

    db_path = rebuild_index(ws)
    snap["zicato.index.index_counts"] = iq.index_counts(db_path)
    snap["zicato.index.validate_index"] = validate_index(ws)
    snap["zicato.index.all_epochs"] = iq.all_epochs(db_path)

    for epoch_id in EPOCH_IDS:
        snap[f"zicato.index.generations_for_epoch::{epoch_id}"] = iq.generations_for_epoch(
            db_path, epoch_id
        )
        snap[f"zicato.index.experiments_for_epoch::{epoch_id}"] = iq.experiments_for_epoch(
            db_path, epoch_id
        )
        snap[f"zicato.index.tournaments_for_epoch::{epoch_id}"] = iq.tournaments_for_epoch(
            db_path, epoch_id
        )
        snap[f"zicato.index.elo_for_epoch::{epoch_id}"] = iq.elo_for_epoch(db_path, epoch_id)
        snap[f"zicato.index.epoch_ancestry::{epoch_id}"] = iq.epoch_ancestry(db_path, epoch_id)
        snap[f"zicato.index.reflections_for_epoch::{epoch_id}"] = iq.reflections_for_epoch(
            db_path, epoch_id
        )
        snap[f"zicato.index.prior_experiments_for_epoch::{epoch_id}"] = (
            iq.prior_experiments_for_epoch(db_path, epoch_id)
        )

    epoch_id = RICH_EPOCH_ID
    for generation_id in ("v0", "v2", "v10"):
        snap[f"zicato.index.runs_for_generation::{generation_id}"] = iq.runs_for_generation(
            db_path, epoch_id, generation_id
        )
        snap[f"zicato.index.loss_profiles_for_generation::{generation_id}"] = (
            iq.loss_profiles_for_generation(db_path, epoch_id, generation_id)
        )
        snap[f"zicato.index.judge_losses_for_generation::{generation_id}"] = (
            iq.judge_losses_for_generation(db_path, epoch_id, generation_id)
        )
    snap["zicato.index.judge_loss_trend::tone_guard"] = iq.judge_loss_trend(
        db_path, epoch_id, "tone_guard"
    )
    snap["zicato.index.runs_for_tournament::field-v4"] = iq.runs_for_tournament(
        db_path, f"{epoch_id}:field:v4"
    )
    snap["zicato.index.judge_scorecards_for_reflection::r-10"] = iq.judge_scorecards_for_reflection(
        db_path, "r-10"
    )
    snap["zicato.index.reflection_row::r-10"] = iq.reflection_row(db_path, "r-10")
    snap["zicato.index.mutation_point_track_record"] = iq.mutation_point_track_record(
        db_path, epoch_id
    )


def capture_reader_snapshot(ws: Path) -> dict[str, Any]:
    """Capture every pinned reader's output for one fixture workspace.

    Returns a label-to-value mapping in which every value is plain JSON.
    Labels are ``<package>.<function>``, with the coordinate the reader was
    called on appended after ``::`` where one reader is captured several
    times. The per-run absolute workspace root is replaced by ``<ws>`` and
    the wall-clock stamps are masked, as the query-layer harness does.
    """
    snap: dict[str, Any] = {}
    _capture_workspace_reads(ws, snap)
    _capture_analyzer(ws, snap)
    _capture_reflection(ws, snap)
    _capture_health(ws, snap)
    _capture_index(ws, snap)
    root_str = str(ws)
    return {
        label: mask_volatile(_normalize_root(to_jsonable(value), root_str))
        for label, value in snap.items()
    }


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

#: Labels whose row ORDER is part of the contract, mapped to what identifies
#: one row: a JSON key, a tuple position, or ``None`` when the rows are bare
#: strings.
#:
#: Byte-identity against the golden already pins order for every label. These
#: get an additional, earlier assertion that compares only the identifier
#: sequence, so a reordering fails with a message naming the label and showing
#: the two orders rather than as an opaque diff — which is what a refactor
#: consolidating these walks needs in order to tell "the rows moved" from
#: "the values changed".
ORDER_ENFORCED: dict[str, str | int | None] = {
    # Numeric-aware generation order: v2 before v10.
    "zicato.workspace.read_experiments::e2": 0,
    "zicato.workspace.generation_ids::e2": None,
    "zicato.cli.health.generation_ids::e2": None,
    # Numeric-aware board-entry order: t2 before t10.
    "zicato.workspace.run_entry_ids::e2::v0": None,
    # Ascending round index: 2 before 10.
    "zicato.workspace.round_indices::e2": None,
    # The index's generation rows order by recorded creation time, which the
    # fixture's lineage makes agree with the numeric order above.
    "zicato.index.generations_for_epoch::e2": "generation_id",
    # Lexical generation order: ``v10`` lands between ``v1`` and ``v2``. The
    # experiment selector orders by the id column, so it disagrees with every
    # other generation enumeration in this snapshot.
    "zicato.index.experiments_for_epoch::e2": "generation_id",
    # Canonical timestamp-first epoch order: e2 before e10.
    "zicato.workspace.list_epoch_ids": None,
    "zicato.workspace.iter_epochs": "id",
    "zicato.index.all_epochs": "epoch_id",
    # A filesystem walk whose order the decision-event aggregate accumulates
    # in, so a reordering is observable only here.
    "zicato.analyzer.collect_events_jsonl_paths::e2": None,
    # Ranked output: the episode ranking is the reader's whole point.
    "zicato.reflection.mine_episodes": "summary",
    # Per-run enumeration order within a generation.
    "zicato.reflection.ingest_lineage": "loss_ref",
    "zicato.index.runs_for_generation::v0": "run_id",
    "zicato.index.loss_profiles_for_generation::v0": "run_id",
}


def order_of(label: str, value: Any) -> list[Any]:
    """The identifier sequence an order-enforced label's rows present."""
    key = ORDER_ENFORCED[label]
    if key is None:
        return list(value)
    return [row[key] for row in value]
