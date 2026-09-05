"""Recorded workspaces behind the console's browser-test fixtures.

The browser suite under ``src/zicato/dashboard/static/test`` stubs ``fetch``
with fixture maps keyed by route. A view that renders a server-side join (the
round timeline, the racing field, the candidate dossier) must be fed the
payload the Python reader serves, and the suite holds no Python. Each builder
here writes one canonical ``.zicato/`` tree carrying the ids, scalars and
decisions a browser fixture map describes, and derives the analytical index
from those files with the real indexer. The endpoint snapshot harness
(:mod:`tests._endpoint_snapshot_harness`) serves every tree through the
dashboard application and records what each probed route answers, and the
browser suite reads those recordings by route.

Every builder returns the workspace root (the ``.zicato`` directory). Files are
written with two-space indentation because the tree's exact bytes are part of
what the recorded responses are captured against.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests._workspace_support import (
    experiment_record,
    set_current_epoch,
    workspace,
    write_epoch,
    write_generation,
    write_json,
    write_lineage,
    write_text,
    write_workspace_config,
)
from zicato.core.experiment import (
    DriftMovementActual,
    ExpectedDriftMovement,
    Experiment,
    HypothesisSpec,
    MatchOutcome,
    OutcomeRecord,
    Patch,
)
from zicato.core.types import JudgeLoss, LossProfile
from zicato.epoch.journal import write_experiment
from zicato.index.ingest import rebuild_index
from zicato.telemetry.reducer import write_loss_profile
from zicato.workspace import WorkspaceLayout

#: The epoch id the shared browser fixture map (``fixtures.mjs``) names.
CONSOLE_EPOCH = "2026-05-30_e0"

#: The epoch a fast-mode champion's per-board results were reused from.
CACHED_SOURCE_EPOCH = "2026-05-29_e0"

#: The two board entries of the shared browser fixture: one single-turn task
#: with a predicate expectation and one emulated multi-turn task.
CONSOLE_BOARD: tuple[dict[str, Any], ...] = (
    {"board_meta": True, "disable_drift": False},
    {
        "id": "waffles_single",
        "kind": "single_turn",
        "input": "Make a presentation about waffles.",
        "expectation": {"kind": "predicate", "spec": "deck_has_title"},
        "wall_clock_budget_seconds": 180,
        "weight": 1.0,
        "tags": ["smoke"],
    },
    {
        "id": "picky_stakeholder_emulated",
        "kind": "multi_turn_emulated",
        "persona": {
            "name": "picky stakeholder",
            "goals": ["a crisp deck"],
            "style": "terse",
            "max_turns": 4,
        },
        "wall_clock_budget_seconds": 360,
        "weight": 1.0,
        "tags": ["hard"],
    },
)


def _workspace_config() -> dict[str, Any]:
    return {"adapter": {"entrypoint": "agent.coordinator:app", "mutable_trees": ["agent"]}}


def _judge(name: str, raw: float, weight: float = 1.0) -> JudgeLoss:
    return JudgeLoss(judge_name=name, raw_loss=raw, weight=weight, weighted_loss=raw * weight)


def _run(
    layout: WorkspaceLayout,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    *,
    run_id: str,
    drift_loss: float,
    pass_fail: bool | None,
    runtime_ms: int,
    wall_clock_budget_exceeded: bool = False,
    judges: Sequence[JudgeLoss] = (),
    match_id: str = "",
    adk_session_id: str = "",
    score: float | None = None,
    cached_from: tuple[str, str] | None = None,
) -> None:
    """Write one board run's ``loss.json`` through the reducer's writer.

    ``cached_from`` is ``(source_epoch, source_run)`` for a result a fast-mode
    round reused rather than re-ran.
    """
    profile = LossProfile(
        run_id=run_id,
        entry_id=entry_id,
        generation_id=generation_id,
        epoch_id=epoch_id,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=runtime_ms,
        wall_clock_budget_exceeded=wall_clock_budget_exceeded,
        expectation_result=None,
        drift_loss=drift_loss,
        pass_fail=pass_fail,
        match_id=match_id,
        per_judge_loss=tuple(judges),
        adk_session_id=adk_session_id,
        score=score,
        cached=cached_from is not None,
        source_epoch=cached_from[0] if cached_from else "",
        source_run=cached_from[1] if cached_from else "",
    )
    write_loss_profile(profile, layout.loss(epoch_id, generation_id, entry_id))


def _gen_score(
    layout: WorkspaceLayout,
    epoch_id: str,
    generation_id: str,
    *,
    scalar: float,
    pass_rate: float,
    components: Mapping[str, float],
    per_entry: Mapping[str, Mapping[str, Any]],
    mean_score: float | None = None,
) -> None:
    """Write the cached per-generation aggregate the gate and the stats read."""
    write_json(
        layout.gen_score(epoch_id, generation_id),
        {
            "drift_loss_mean": components.get("drift"),
            "pass_rate": pass_rate,
            # The aggregator reports the pass rate here for a board whose
            # entries carry no continuous score.
            "mean_score": pass_rate if mean_score is None else mean_score,
            "entry_count": len(per_entry),
            "scalar": scalar,
            "per_entry": {k: dict(v) for k, v in per_entry.items()},
            "scalar_components": dict(components),
        },
        indent=2,
    )


def _challenger(
    layout: WorkspaceLayout,
    epoch_id: str,
    generation_id: str,
    *,
    parent: str,
    core_idea: str,
    why: str,
    patches: Sequence[Patch],
    decision: str,
    scalar_delta: float,
    proposed_at: str,
    ran_at: str,
    round_index: int,
    structure: str = "gauntlet",
    match_record: Sequence[MatchOutcome] = (),
    movements: Sequence[DriftMovementActual] = (),
    expected: Sequence[ExpectedDriftMovement] = (),
) -> None:
    """Write one challenger's experiment record through the journal writer."""
    write_experiment(
        layout.root,
        epoch_id,
        generation_id,
        Experiment(
            id=f"exp_{epoch_id}_{generation_id}",
            epoch_id=epoch_id,
            generation_id=generation_id,
            parent_generation_id=parent,
            proposed_at=proposed_at,
            hypothesis=HypothesisSpec(
                core_idea=core_idea,
                modulating=tuple(p.mutation_id for p in patches),
                why=why,
                expected_drift_movements=tuple(expected),
                expected_pass_rate_delta="+0.00 to +0.10",
            ),
            patches=tuple(patches),
            outcome=OutcomeRecord(
                ran_at=ran_at,
                drift_movements=tuple(movements),
                pass_rate_delta=0.0,
                drift_loss_delta=scalar_delta,
                scalar_score_delta=scalar_delta,
                tournament_decision=decision,  # type: ignore[arg-type]
                structure=structure,
                match_record=tuple(match_record),
            ),
            round_index=round_index,
        ),
    )


def _seed(layout: WorkspaceLayout, epoch_id: str, proposed_at: str) -> None:
    """The parentless seed generation: a record with no hypothesis or outcome."""
    write_generation(
        layout,
        epoch_id,
        "v0",
        experiment=experiment_record("v0", parent_generation_id=None, proposed_at=proposed_at),
        indent=2,
    )


def _lineage_epoch(
    epoch_id: str, generations: Sequence[tuple[str, str | None, bool]], created_at: str
) -> dict[str, Any]:
    return {
        "id": epoch_id,
        "generations": [
            {"id": gid, "parent_id": parent, "promoted": promoted, "created_at": created_at}
            for gid, parent, promoted in generations
        ],
    }


# ---------------------------------------------------------------------------
# The shared console fixture: one gauntlet epoch, a seed and two rejected
# challengers
# ---------------------------------------------------------------------------


def build_console_workspace(
    tmp_path: Path, *, cached_champion: bool = False, episodes: bool = False
) -> Path:
    """The gauntlet workspace the shared browser fixture map describes.

    Epoch ``2026-05-30_e0`` holds the seed ``v0`` and two rejected
    challengers: ``v1`` (scalar 146.65 against the champion's 70.94, a
    +75.71 regression that the scalar-margin rule refuses) and ``v2``
    (72.45, +1.51). ``v0`` and ``v1`` ran both board entries; ``v2`` ran
    the single-turn entry only. Judge losses on the single-turn entry
    make ``incorporates_feedback`` the judge that decided ``v1``'s round.

    ``cached_champion`` marks the champion's two runs as reused from an
    earlier epoch, the way a fast-mode round records them. ``episodes`` adds
    the proposal episodes: ``v1`` has its log and the page rendered from it,
    ``v2`` has its log alone.
    """
    layout = workspace(tmp_path)
    write_workspace_config(layout, _workspace_config(), indent=2)
    write_epoch(
        layout,
        CONSOLE_EPOCH,
        config={
            "id": CONSOLE_EPOCH,
            "created_at": "2026-05-30T00:00:00Z",
            "closed": False,
            "goal": "Make the presentation agent crisper.",
            "contract_hash": "hash-console",
        },
        brief="# Brief\n\n## Goal\n\nMake the presentation agent crisper.\n",
        scoring={"promote_margin": 0.01, "tournament": {"structure": "gauntlet", "params": {}}},
        board=list(CONSOLE_BOARD),
        indent=2,
    )
    set_current_epoch(layout, CONSOLE_EPOCH, newline=True)

    _seed(layout, CONSOLE_EPOCH, "2026-05-30T00:00:00Z")
    _challenger(
        layout,
        CONSOLE_EPOCH,
        "v1",
        parent="v0",
        core_idea="Enforce explicit slide-structure output.",
        why="The coordinator's outline omits the slide structure the brief asks for.",
        patches=(
            Patch(
                id="p1",
                mutation_id="coordinator_prompt",
                op="replace",
                new_content="You are the coordinator.\nAlways emit an explicit slide structure.",
                new_numeric=None,
                new_enum=None,
                rationale="Enforce structure.",
            ),
            Patch(
                id="p2",
                mutation_id="oversight_policy",
                op="replace",
                new_content="Tighten coordinator oversight.",
                new_numeric=None,
                new_enum=None,
                rationale="Tighten oversight.",
            ),
        ),
        decision="rejected",
        scalar_delta=75.71,
        proposed_at="2026-05-30T00:30:00Z",
        ran_at="2026-05-30T01:00:00Z",
        round_index=0,
        expected=(ExpectedDriftMovement(kind="omission", direction="decrease", magnitude="small"),),
        movements=(
            DriftMovementActual(
                kind="omission", from_rate=0.5, to_rate=0.75, hypothesis_match=False
            ),
        ),
    )
    _challenger(
        layout,
        CONSOLE_EPOCH,
        "v2",
        parent="v0",
        core_idea="Tighten coordinator oversight.",
        why="The oversight policy lets a drifting draft through.",
        patches=(
            Patch(
                id="p3",
                mutation_id="oversight_policy",
                op="replace",
                new_content="Loosen coordinator oversight.",
                new_numeric=None,
                new_enum=None,
                rationale="Loosen oversight.",
            ),
        ),
        decision="rejected",
        scalar_delta=1.51,
        proposed_at="2026-05-30T01:30:00Z",
        ran_at="2026-05-30T02:00:00Z",
        round_index=1,
        expected=(ExpectedDriftMovement(kind="omission", direction="decrease", magnitude="small"),),
        movements=(
            DriftMovementActual(kind="omission", from_rate=0.5, to_rate=0.4, hypothesis_match=True),
        ),
    )

    def reused(gen: str, entry: str) -> tuple[str, str] | None:
        if cached_champion and gen == "v0":
            return (CACHED_SOURCE_EPOCH, f"run_prior_{entry}")
        return None

    def waffles(gen: str, drift: float, *, exceeded: bool, feedback: float) -> None:
        _run(
            layout,
            CONSOLE_EPOCH,
            gen,
            "waffles_single",
            run_id=f"run_{gen}_waffles",
            drift_loss=drift,
            pass_fail=False,
            runtime_ms=180000,
            wall_clock_budget_exceeded=exceeded,
            judges=(_judge("incorporates_feedback", feedback), _judge("omission", 4.0)),
            adk_session_id=f"sess-{gen}-waffles",
            cached_from=reused(gen, "waffles"),
        )

    def picky(gen: str, drift: float) -> None:
        _run(
            layout,
            CONSOLE_EPOCH,
            gen,
            "picky_stakeholder_emulated",
            run_id=f"run_{gen}_picky",
            drift_loss=drift,
            pass_fail=False,
            runtime_ms=360000,
            wall_clock_budget_exceeded=True,
            judges=(_judge("omission", 6.0),),
            cached_from=reused(gen, "picky"),
        )

    waffles("v0", 60.5, exceeded=False, feedback=10.0)
    picky("v0", 105.5)
    waffles("v1", 60.5, exceeded=True, feedback=34.0)
    picky("v1", 642.5)
    waffles("v2", 61.0, exceeded=False, feedback=12.0)

    def entry(drift: float, passed: bool) -> dict[str, Any]:
        return {"drift_loss": drift, "failure": 0.0, "pass_fail": passed, "score": None}

    _gen_score(
        layout,
        CONSOLE_EPOCH,
        "v0",
        scalar=70.94,
        pass_rate=0.0,
        components={"drift": 68.5, "schema": 1.43},
        per_entry={
            "waffles_single": entry(60.5, False),
            "picky_stakeholder_emulated": entry(105.5, False),
        },
    )
    _gen_score(
        layout,
        CONSOLE_EPOCH,
        "v1",
        scalar=146.65,
        pass_rate=0.0,
        components={"drift": 145.64, "schema": 0.0},
        per_entry={
            "waffles_single": entry(60.5, False),
            "picky_stakeholder_emulated": entry(642.5, False),
        },
    )
    _gen_score(
        layout,
        CONSOLE_EPOCH,
        "v2",
        scalar=72.45,
        pass_rate=0.0,
        components={"drift": 71.0, "schema": 1.45},
        per_entry={"waffles_single": entry(61.0, False)},
    )

    write_lineage(
        layout,
        {
            "epochs": [
                _lineage_epoch(
                    CONSOLE_EPOCH,
                    [("v0", None, True), ("v1", "v0", False), ("v2", "v0", False)],
                    "2026-05-30T00:00:00Z",
                )
            ]
        },
        indent=2,
    )
    if episodes:
        for gen, with_page in (("v1", True), ("v2", False)):
            directory = layout.proposal_episode_dir(CONSOLE_EPOCH, gen)
            directory.mkdir(parents=True, exist_ok=True)
            write_text(
                directory / "episode.jsonl",
                '{"seq": 0, "type": "episode/start", "time": 0, "data": {"model": "m"}}\n',
            )
            if with_page:
                write_text(
                    layout.proposal_episode_export(CONSOLE_EPOCH, gen),
                    "<!doctype html><title>episode</title>\n",
                )
    rebuild_index(layout.root)
    return layout.root


def build_cached_champion_workspace(tmp_path: Path) -> Path:
    """The console workspace with the champion's results reused from an earlier epoch."""
    return build_console_workspace(tmp_path, cached_champion=True)


def build_episodes_workspace(tmp_path: Path) -> Path:
    """The console workspace with the proposal episodes of v1 (with a page) and v2."""
    return build_console_workspace(tmp_path, episodes=True)


# ---------------------------------------------------------------------------
# A compact description of one epoch, for the scenario workspaces below
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gen:
    """One generation of a scenario epoch.

    ``decision`` is the tournament verdict the record carries: ``promoted``,
    ``rejected``, or ``None`` for a generation whose round has not settled
    (its experiment record then holds no outcome). ``lineage_promoted`` is the
    flag ``lineage.json`` records; ``None`` there is a generation still being
    scored. ``entries`` are ``(entry_id, drift_loss, pass_fail)`` runs, and
    ``scalar`` writes the cached aggregate the gate compares. ``round_index``
    of ``None`` writes a record with no round stamp, which is what a journal
    written before the stamp existed holds.
    """

    id: str
    parent: str | None = None
    decision: str | None = None
    lineage_promoted: bool | None = False
    scalar: float | None = None
    entries: tuple[tuple[str, float, bool | None], ...] = ()
    round_index: int | None = 0
    matches: tuple[MatchOutcome, ...] = ()
    core_idea: str = ""
    structure: str = "gauntlet"
    champion_eval_mode: str = "full"
    scalar_delta: float = 0.0


@dataclass(frozen=True)
class FieldRecord:
    """One settled field-tournament snapshot, as the runner writes it."""

    first_challenger: str
    structure: str
    competitors: tuple[dict[str, Any], ...]
    rounds: tuple[dict[str, Any], ...] = ()
    standings: tuple[dict[str, Any], ...] = ()
    structure_params: Mapping[str, Any] | None = None
    promoted: str = ""
    champion: str = ""
    decision: str = ""
    state: str = "settled"
    ran_at: str = "2026-05-30T03:00:00Z"


@dataclass(frozen=True)
class EpochSpec:
    """One epoch of a scenario workspace."""

    id: str
    gens: tuple[Gen, ...]
    created_at: str
    structure: str = "gauntlet"
    params: Mapping[str, Any] | None = None
    fields: tuple[FieldRecord, ...] = ()
    closed: bool = False
    goal: str = "g"
    board: Sequence[Any] = ({"id": "b1", "kind": "single_turn", "input": "Draft.", "weight": 1.0},)


def _stamp(created_at: str, minutes: int) -> str:
    """``created_at`` plus ``minutes``, on the minute, for ordered records."""
    hours, mins = int(created_at[11:13]), int(created_at[14:16]) + minutes
    hours, mins = hours + mins // 60, mins % 60
    return f"{created_at[:11]}{hours:02d}:{mins:02d}:00Z"


def _write_epoch_spec(layout: WorkspaceLayout, spec: EpochSpec) -> dict[str, Any]:
    """Write one epoch's tree and return its ``lineage.json`` block."""
    write_epoch(
        layout,
        spec.id,
        config={
            "id": spec.id,
            "created_at": spec.created_at,
            "closed": spec.closed,
            "goal": spec.goal,
            "contract_hash": f"hash-{spec.id}",
        },
        brief=f"# Brief {spec.id}\n\n## Goal\n\n{spec.goal}\n",
        scoring={
            "promote_margin": 0.01,
            "tournament": {"structure": spec.structure, "params": dict(spec.params or {})},
        },
        board=list(spec.board),
        indent=2,
    )
    for i, gen in enumerate(spec.gens):
        proposed_at = _stamp(spec.created_at, 10 * i)
        if gen.parent is None:
            write_generation(
                layout,
                spec.id,
                gen.id,
                experiment=experiment_record(
                    gen.id, parent_generation_id=None, proposed_at=proposed_at
                ),
                indent=2,
            )
        elif gen.round_index is None:
            outcome = (
                {
                    "tournament_decision": gen.decision,
                    "scalar_score_delta": gen.scalar_delta,
                    "ran_at": _stamp(spec.created_at, 10 * i + 5),
                    "structure": gen.structure,
                }
                if gen.decision is not None
                else None
            )
            write_generation(
                layout,
                spec.id,
                gen.id,
                experiment=experiment_record(
                    gen.id,
                    parent_generation_id=gen.parent,
                    proposed_at=proposed_at,
                    outcome=outcome,
                    hypothesis={"core_idea": gen.core_idea or f"Idea {gen.id}."},
                ),
                indent=2,
            )
        else:
            outcome = (
                OutcomeRecord(
                    ran_at=_stamp(spec.created_at, 10 * i + 5),
                    drift_movements=(),
                    pass_rate_delta=0.0,
                    drift_loss_delta=gen.scalar_delta,
                    scalar_score_delta=gen.scalar_delta,
                    tournament_decision=gen.decision,  # type: ignore[arg-type]
                    structure=gen.structure,
                    match_record=tuple(gen.matches),
                    champion_eval_mode=gen.champion_eval_mode,
                )
                if gen.decision is not None
                else None
            )
            write_experiment(
                layout.root,
                spec.id,
                gen.id,
                Experiment(
                    id=f"exp_{spec.id}_{gen.id}",
                    epoch_id=spec.id,
                    generation_id=gen.id,
                    parent_generation_id=gen.parent,
                    proposed_at=proposed_at,
                    hypothesis=HypothesisSpec(
                        core_idea=gen.core_idea or f"Idea {gen.id}.",
                        modulating=("site",),
                        why="The prior round's losses point at this site.",
                        expected_drift_movements=(),
                        expected_pass_rate_delta="+0.00 to +0.10",
                    ),
                    patches=(
                        Patch(
                            id=f"patch_{gen.id}",
                            mutation_id="site",
                            op="replace",
                            new_content=f"Edit {gen.id}.",
                            new_numeric=None,
                            new_enum=None,
                            rationale="Apply the idea.",
                        ),
                    ),
                    outcome=outcome,
                    round_index=gen.round_index,
                ),
            )
        for entry_id, drift, passed in gen.entries:
            _run(
                layout,
                spec.id,
                gen.id,
                entry_id,
                run_id=f"run_{spec.id}_{gen.id}_{entry_id}",
                drift_loss=drift,
                pass_fail=passed,
                runtime_ms=1000,
                match_id=gen.matches[-1].match_id if gen.matches else "",
            )
        if gen.scalar is not None:
            _gen_score(
                layout,
                spec.id,
                gen.id,
                scalar=gen.scalar,
                pass_rate=1.0 if all(p for _e, _d, p in gen.entries) else 0.0,
                components={"drift": gen.scalar},
                per_entry={
                    entry_id: {
                        "drift_loss": drift,
                        "failure": 0.0,
                        "pass_fail": passed,
                        "score": None,
                    }
                    for entry_id, drift, passed in gen.entries
                },
            )
    for field in spec.fields:
        write_json(
            layout.field_tournament(spec.id, field.first_challenger),
            {
                "tournament_id": f"{spec.id}:field:{field.first_challenger}",
                "epoch_id": spec.id,
                "structure": field.structure,
                "structure_params": dict(field.structure_params or spec.params or {}),
                "competitors": [dict(c) for c in field.competitors],
                "rounds": [dict(r) for r in field.rounds],
                "standings": [dict(r) for r in field.standings],
                "field_status": [],
                "promoted_generation_id": field.promoted,
                "champion_generation_id": field.champion,
                "decision": field.decision,
                "reason": "",
                "delta_scalar": None,
                "state": field.state,
                "ran_at": field.ran_at,
            },
            indent=2,
        )
    return _lineage_epoch(
        spec.id,
        [(g.id, g.parent, g.lineage_promoted) for g in spec.gens],  # type: ignore[misc]
        spec.created_at,
    )


def build_scenario(tmp_path: Path, *epochs: EpochSpec, current: str | None = None) -> Path:
    """Write a workspace holding ``epochs`` and derive its index.

    ``current`` names the epoch the workspace marker points at; the last
    epoch given when omitted.
    """
    layout = workspace(tmp_path)
    write_workspace_config(layout, _workspace_config(), indent=2)
    blocks = [_write_epoch_spec(layout, spec) for spec in epochs]
    set_current_epoch(layout, current or epochs[-1].id, newline=True)
    write_lineage(layout, {"epochs": blocks}, indent=2)
    rebuild_index(layout.root)
    return layout.root


def _competitors(champion: str, challengers: Sequence[str]) -> tuple[dict[str, Any], ...]:
    """The role-tagged competitor list a field record carries, champion first."""
    return (
        {"generation_id": champion, "seed": 1, "role": "champion"},
        *(
            {"generation_id": c, "seed": i + 2, "role": "challenger"}
            for i, c in enumerate(challengers)
        ),
    )


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------

#: The racing epoch four browser suites share (``RACING_TOURNAMENTS``): v1 and
#: v2 are cut at rung 0, v3 and v4 reach rung 1, v3 wins the champion gate.
RACING_EPOCH = "2026-06-01_e0"

#: The epoch of the many-round timeline fixture: one rejected challenger per
#: round for eleven rounds.
MANY_ROUNDS_EPOCH = "2026-05-31_many"

#: The two epochs of the cross-epoch fixture: a closed racing epoch whose
#: field reached v4, and a newer one with three generations and no records.
OLDER_EPOCH = "2026-06-01_e0"
NEWER_EPOCH = "2026-06-02_e1"

#: The epoch of the field-count fixture: a swiss epoch with an unscored orphan.
FIELD_COUNT_EPOCH = "2026-06-02_fn"

#: The epoch id the round-model fixtures of the figures suite name.
MODEL_EPOCH = "e-model"

#: The epoch of the candidate-identity fixture: one challenger whose scalar
#: clears the margin but which fails a board entry the champion passed.
IDENTITY_EPOCH = "2026-08-01_identity"


def _racing_match(match_id: str, won: bool, delta: float) -> MatchOutcome:
    return MatchOutcome(match_id=match_id, opponent="v0", won=won, delta_scalar=delta)


def _racing_gens() -> tuple[Gen, ...]:
    return (
        Gen("v0", lineage_promoted=True),
        Gen(
            "v1",
            "v0",
            "rejected",
            matches=(_racing_match("rung0_m0", False, 25.0),),
            structure="racing",
        ),
        Gen(
            "v2",
            "v0",
            "rejected",
            matches=(_racing_match("rung0_m1", False, 3.3),),
            structure="racing",
        ),
        Gen(
            "v3",
            "v0",
            "promoted",
            lineage_promoted=True,
            structure="racing",
            matches=(
                _racing_match("rung0_m2", True, -0.16),
                _racing_match("rung1_m0", False, 1.0),
                _racing_match("racing-final", True, -32.19),
            ),
            entries=(("q3_metrics_outline", 63.5, False),),
        ),
        Gen(
            "v4",
            "v0",
            "rejected",
            structure="racing",
            matches=(
                _racing_match("rung0_m3", False, 0.002),
                _racing_match("rung1_m1", False, 1.25),
            ),
        ),
    )


def _rung(
    index: int,
    competitors: Sequence[str],
    survivors: Sequence[str],
    cut: Sequence[str],
    fraction: float,
    deltas: Mapping[str, float],
) -> dict[str, Any]:
    return {
        "stage_index": index,
        "label": f"Rung {index}",
        "matches": [
            {
                "match_id": f"rung{index}",
                "competitors": list(competitors),
                "survivors": list(survivors),
                "cut": list(cut),
                "board_fraction": fraction,
                "deltas": dict(deltas),
            }
        ],
    }


def _champion_gate(
    index: int, champion: str, survivor: str, *, promoted: bool, delta: float
) -> dict[str, Any]:
    return {
        "stage_index": index,
        "label": "Champion gate",
        "matches": [
            {
                "match_id": "racing-final",
                "competitors": [champion, survivor],
                "winner": survivor if promoted else champion,
                "decision": "promoted" if promoted else "rejected",
                "delta_scalar": delta,
                "board_fraction": 1.0,
            }
        ],
    }


#: The settled field record of the shared racing epoch: v1 and v2 cut at rung
#: 0, v4 cut at rung 1, v3 promoted at the champion gate.
RACING_LADDER_ROUNDS: tuple[dict[str, Any], ...] = (
    _rung(
        0,
        ["v1", "v2", "v3", "v4"],
        ["v3", "v4"],
        ["v1", "v2"],
        0.25,
        {"v1": 25.0, "v2": 3.3, "v3": -0.16, "v4": 0.002},
    ),
    _rung(1, ["v3", "v4"], ["v3"], ["v4"], 0.5, {"v3": 1.0, "v4": 1.25}),
    _champion_gate(2, "v0", "v3", promoted=True, delta=-32.19),
)


def build_racing_ladder_workspace(tmp_path: Path) -> Path:
    """The closed racing epoch whose per-challenger records the suites join."""
    return build_scenario(
        tmp_path,
        EpochSpec(
            RACING_EPOCH,
            _racing_gens(),
            "2026-06-01T00:00:00Z",
            structure="racing",
            params={"eta": 2, "board_fraction": 0.25},
            closed=True,
            fields=(
                FieldRecord(
                    "v1",
                    "racing",
                    _competitors("v0", ["v1", "v2", "v3", "v4"]),
                    rounds=RACING_LADDER_ROUNDS,
                    promoted="v3",
                    champion="v0",
                    decision="promoted",
                    ran_at="2026-06-01T01:00:00Z",
                ),
            ),
        ),
    )


def build_racing_no_records_workspace(tmp_path: Path) -> Path:
    """A racing epoch whose one challenger was rejected before any rung ran."""
    return build_scenario(
        tmp_path,
        EpochSpec(
            RACING_EPOCH,
            (Gen("v0", lineage_promoted=True), Gen("v1", "v0", "rejected", structure="racing")),
            "2026-06-01T00:00:00Z",
            structure="racing",
            params={"rungs": [1, 2]},
        ),
    )


def build_many_rounds_workspace(tmp_path: Path) -> Path:
    """Eleven rounds, each minting one rejected challenger against the seed."""
    gens = [Gen("v0", lineage_promoted=True, entries=(("b1", 100.0, True),))]
    gens += [
        Gen(
            f"v{i}",
            "v0",
            "rejected",
            round_index=i - 1,
            entries=(("b1", 100.0 + i, True),),
            core_idea=f"Idea {i}.",
            scalar_delta=1.5 * i,
        )
        for i in range(1, 12)
    ]
    return build_scenario(
        tmp_path,
        EpochSpec(MANY_ROUNDS_EPOCH, tuple(gens), "2026-05-31T00:00:00Z", goal="Many rounds."),
    )


def build_two_epochs_workspace(tmp_path: Path) -> Path:
    """A closed racing epoch beside a newer epoch that shares its generation ids."""
    older = EpochSpec(
        OLDER_EPOCH,
        (
            Gen("v0", lineage_promoted=True, entries=(("waffles_single", 50.0, False),)),
            Gen(
                "v1",
                "v0",
                "rejected",
                structure="racing",
                entries=(("waffles_single", 50.0, False),),
                matches=(_racing_match("rung0_m0", False, 3.0),),
            ),
            Gen(
                "v2",
                "v0",
                "rejected",
                structure="racing",
                entries=(("waffles_single", 50.0, False),),
            ),
            Gen(
                "v3",
                "v0",
                "rejected",
                structure="racing",
                entries=(("waffles_single", 50.0, False),),
            ),
            Gen(
                "v4",
                "v0",
                "promoted",
                lineage_promoted=True,
                structure="racing",
                entries=(("waffles_single", 50.0, False),),
                matches=(
                    _racing_match("rung0_m3", True, -1.0),
                    _racing_match("racing-final", True, -5.0),
                ),
            ),
        ),
        "2026-06-01T00:00:00Z",
        structure="racing",
        params={"eta": 2, "board_fraction": 0.25},
        closed=True,
        fields=(
            FieldRecord(
                "v1",
                "racing",
                _competitors("v0", ["v1", "v2", "v3", "v4"]),
                rounds=(
                    _rung(
                        0,
                        ["v1", "v2", "v3", "v4"],
                        ["v4"],
                        ["v1", "v2", "v3"],
                        0.25,
                        {"v1": 3.0, "v2": 4.0, "v3": 5.0, "v4": -1.0},
                    ),
                    _champion_gate(1, "v0", "v4", promoted=True, delta=-5.0),
                ),
                promoted="v4",
                champion="v0",
                decision="promoted",
                ran_at="2026-06-01T01:00:00Z",
            ),
        ),
    )
    newer = EpochSpec(
        NEWER_EPOCH,
        (
            Gen("v0", lineage_promoted=True, entries=(("waffles_single", 50.0, False),)),
            Gen(
                "v1", "v0", None, lineage_promoted=None, entries=(("waffles_single", 50.0, False),)
            ),
            Gen(
                "v2", "v0", None, lineage_promoted=None, entries=(("waffles_single", 50.0, False),)
            ),
        ),
        "2026-06-02T00:00:00Z",
        structure="racing",
        params={"eta": 2, "board_fraction": 0.25},
    )
    return build_scenario(tmp_path, older, newer, current=NEWER_EPOCH)


#: The three structure records the ``structFixture`` browser helper serves,
#: each over the shared console epoch: a settled single-elimination bracket
#: (v1 beats v2 then v0), a two-round swiss ladder, and a two-rung racing
#: field, all with the round-by-round matches the figures draw.
SINGLE_ELIM_ROUNDS: tuple[dict[str, Any], ...] = (
    {
        "round_index": 0,
        "label": "Semifinal",
        "matches": [
            {
                "match_id": "WB-R0-0",
                "competitors": ["v0", "v3"],
                "winner": "v0",
                "decision": "rejected",
                "delta_scalar": 0.05,
                "bracket_slot": "WB-R0-0",
                "bye": False,
            },
            {
                "match_id": "WB-R0-1",
                "competitors": ["v1", "v2"],
                "winner": "v1",
                "decision": "promoted",
                "delta_scalar": -0.12,
                "bracket_slot": "WB-R0-1",
                "bye": False,
            },
        ],
    },
    {
        "round_index": 1,
        "label": "Final",
        "matches": [
            {
                "match_id": "WB-R1-0",
                "competitors": ["v0", "v1"],
                "winner": "v1",
                "decision": "promoted",
                "delta_scalar": -0.08,
                "bracket_slot": "WB-R1-0",
                "bye": False,
            },
        ],
    },
)
SWISS_ROUNDS: tuple[dict[str, Any], ...] = (
    {
        "round_index": 0,
        "label": "Round 1",
        "matches": [
            {"match_id": "r0m0", "competitors": ["v0", "v1"], "winner": "v1", "delta_scalar": -0.1}
        ],
    },
    {
        "round_index": 1,
        "label": "Round 2",
        "matches": [
            {"match_id": "r1m0", "competitors": ["v1", "v2"], "winner": "v1", "delta_scalar": -0.03}
        ],
    },
)
RACING_ROUNDS: tuple[dict[str, Any], ...] = (
    {
        "round_index": 0,
        "label": "Rung 1",
        "matches": [
            {
                "match_id": "rung1",
                "competitors": ["v0", "v1", "v2", "v3"],
                "survivors": ["v0", "v1"],
                "cut": ["v2", "v3"],
                "board_fraction": 0.5,
            }
        ],
    },
    {
        "round_index": 1,
        "label": "Rung 2",
        "matches": [
            {
                "match_id": "rung2",
                "competitors": ["v0", "v1"],
                "survivors": ["v1"],
                "cut": ["v0"],
                "board_fraction": 1.0,
            }
        ],
    },
)


def _structure_gens(structure: str, count: int, *, winner: str = "v1") -> tuple[Gen, ...]:
    """A seed and ``count - 1`` challengers, one of them the field's winner."""
    gens = [Gen("v0", lineage_promoted=True, scalar=70.0, entries=(("b1", 70.0, True),))]
    for i in range(1, count):
        gid = f"v{i}"
        gens.append(
            Gen(
                gid,
                "v0",
                "promoted" if gid == winner else "rejected",
                lineage_promoted=gid == winner,
                structure=structure,
                scalar=70.0 + i,
                entries=(("b1", 70.0 + i, True),),
                scalar_delta=-0.12 if gid == winner else 0.05,
            )
        )
    return tuple(gens)


def _structure_workspace(
    tmp_path: Path,
    structure: str,
    *,
    params: Mapping[str, Any],
    count: int,
    rounds: tuple[dict[str, Any], ...],
    standings: tuple[dict[str, Any], ...],
    winner: str = "v1",
) -> Path:
    return build_scenario(
        tmp_path,
        EpochSpec(
            CONSOLE_EPOCH,
            _structure_gens(structure, count, winner=winner),
            "2026-05-30T00:00:00Z",
            structure=structure,
            params=params,
            fields=(
                FieldRecord(
                    "v1",
                    structure,
                    _competitors("v0", [f"v{i}" for i in range(1, count)]),
                    rounds=rounds,
                    standings=standings,
                    promoted=winner,
                    champion="v0",
                    decision="promoted",
                ),
            ),
        ),
    )


def build_single_elim_workspace(tmp_path: Path) -> Path:
    """A four-competitor single-elimination bracket that crowned v1."""
    return _structure_workspace(
        tmp_path,
        "single_elim",
        params={"seed_order": "scalar"},
        count=4,
        rounds=SINGLE_ELIM_ROUNDS,
        standings=(
            {
                "generation_id": "v1",
                "rank": 1,
                "scalar": 0.41,
                "wins": 2,
                "losses": 0,
                "status": "champion",
                "role": "challenger",
            },
            {
                "generation_id": "v0",
                "rank": 2,
                "scalar": 0.49,
                "wins": 1,
                "losses": 1,
                "status": "eliminated",
                "role": "champion",
            },
        ),
    )


def build_swiss_workspace(tmp_path: Path) -> Path:
    """A two-round swiss ladder over the champion and two challengers, won by v1."""
    return _structure_workspace(
        tmp_path,
        "swiss",
        params={"rounds": 2},
        count=3,
        rounds=SWISS_ROUNDS,
        standings=(
            {
                "generation_id": "v1",
                "rank": 1,
                "scalar": 0.4,
                "wins": 2,
                "losses": 0,
                "status": "champion",
            },
            {
                "generation_id": "v0",
                "rank": 2,
                "scalar": 0.5,
                "wins": 0,
                "losses": 1,
                "status": "alive",
            },
        ),
    )


def build_racing_field_workspace(tmp_path: Path) -> Path:
    """A two-rung racing field over four competitors whose survivor v1 was crowned."""
    return _structure_workspace(
        tmp_path,
        "racing",
        params={"rungs": [{"fraction": 0.5, "keep": 0.5}, {"fraction": 1.0, "keep": 0.5}]},
        count=4,
        rounds=RACING_ROUNDS,
        standings=({"generation_id": "v1", "rank": 1, "scalar": 0.39, "status": "champion"},),
    )


#: The double-elimination bracket the browser suite derives from the
#: single-elimination one: the same semifinals and final, plus one losers'
#: round in which the champion beats the semifinal loser.
DOUBLE_ELIM_ROUNDS: tuple[dict[str, Any], ...] = (
    *SINGLE_ELIM_ROUNDS,
    {
        "round_index": 2,
        "label": "LB Round 1",
        "matches": [
            {
                "match_id": "LB-R0-0",
                "competitors": ["v0", "v2"],
                "winner": "v0",
                "decision": "rejected",
                "delta_scalar": 0.02,
                "bracket_slot": "LB-R0-0",
                "bye": False,
            },
        ],
    },
)


def build_double_elim_workspace(tmp_path: Path) -> Path:
    """A four-competitor double-elimination field with one losers' round."""
    return _structure_workspace(
        tmp_path,
        "double_elim",
        params={"grand_final_reset": True},
        count=4,
        rounds=DOUBLE_ELIM_ROUNDS,
        standings=(
            {
                "generation_id": "v1",
                "rank": 1,
                "scalar": 0.41,
                "wins": 2,
                "losses": 0,
                "status": "champion",
                "role": "challenger",
            },
            {
                "generation_id": "v0",
                "rank": 2,
                "scalar": 0.49,
                "wins": 2,
                "losses": 1,
                "status": "eliminated",
                "role": "champion",
            },
        ),
    )


def _swiss_with_field_status(tmp_path: Path, field_status: Sequence[dict[str, Any]]) -> Path:
    """The swiss ladder whose field record also carries the proposing status."""
    challengers = [row["generation_id"] for row in field_status]
    applied = [
        gid
        for gid, row in zip(challengers, field_status, strict=True)
        if row["status"] == "applied"
    ]
    gens = [Gen("v0", lineage_promoted=True, scalar=70.0, entries=(("b1", 70.0, True),))]
    for i, gid in enumerate(challengers):
        if gid in applied:
            gens.append(
                Gen(
                    gid,
                    "v0",
                    "promoted" if gid == "v1" else "rejected",
                    lineage_promoted=gid == "v1",
                    structure="swiss",
                    scalar=71.0 + i,
                    entries=(("b1", 71.0 + i, True),),
                )
            )
        else:
            gens.append(Gen(gid, "v0", None, lineage_promoted=None, round_index=None))
    record = {
        "tournament_id": f"{CONSOLE_EPOCH}:field:v1",
        "epoch_id": CONSOLE_EPOCH,
        "structure": "swiss",
        "structure_params": {"rounds": 2},
        "competitors": [dict(c) for c in _competitors("v0", applied)],
        "rounds": [dict(r) for r in SWISS_ROUNDS] if applied else [],
        "standings": [
            {
                "generation_id": "v1",
                "rank": 1,
                "scalar": 0.4,
                "wins": 2,
                "losses": 0,
                "status": "champion",
            },
            {
                "generation_id": "v0",
                "rank": 2,
                "scalar": 0.5,
                "wins": 0,
                "losses": 1,
                "status": "alive",
            },
        ]
        if applied
        else [],
        "field_status": [dict(row) for row in field_status],
        "promoted_generation_id": "v1" if "v1" in applied else "",
        "champion_generation_id": "v0",
        "decision": "promoted" if "v1" in applied else "rejected",
        "reason": "",
        "delta_scalar": None,
        "state": "settled",
        "ran_at": "2026-05-30T03:00:00Z",
    }
    root = build_scenario(
        tmp_path,
        EpochSpec(
            CONSOLE_EPOCH,
            tuple(gens),
            "2026-05-30T00:00:00Z",
            structure="swiss",
            params={"rounds": 2},
        ),
    )
    layout = WorkspaceLayout.from_root(root)
    write_json(layout.field_tournament(CONSOLE_EPOCH, "v1"), record, indent=2)
    rebuild_index(root)
    return root


def build_swiss_proposing_workspace(tmp_path: Path) -> Path:
    """A swiss field of three proposals whose second was rejected before it raced."""
    return _swiss_with_field_status(
        tmp_path,
        (
            {"generation_id": "v1", "status": "applied", "reason": "", "seed": 2},
            {
                "generation_id": "v2",
                "status": "rejected",
                "reason": "proposer returned invalid JSON",
                "seed": 3,
            },
            {"generation_id": "v3", "status": "applied", "reason": "", "seed": 4},
        ),
    )


def build_swiss_all_rejected_workspace(tmp_path: Path) -> Path:
    """A swiss round in which every one of four proposals was rejected.

    A field with fewer than three competitors is never written to the index,
    so the only record of such a round is the runtime envelope the runner
    publishes while the round is open. The workspace carries that envelope
    and no settled field record.
    """
    field_status = (
        {"generation_id": "v1", "status": "rejected", "reason": "empty response", "seed": 2},
        {
            "generation_id": "v2",
            "status": "rejected",
            "reason": "post-apply validation failed",
            "seed": 3,
        },
        {
            "generation_id": "v3",
            "status": "rejected",
            "reason": "mutation_id no longer resolves",
            "seed": 4,
        },
        {"generation_id": "v4", "status": "rejected", "reason": "empty response", "seed": 5},
    )
    gens = [Gen("v0", lineage_promoted=True, scalar=70.0, entries=(("b1", 70.0, True),))]
    gens += [
        Gen(row["generation_id"], "v0", None, lineage_promoted=None, round_index=None)
        for row in field_status
    ]
    root = build_scenario(
        tmp_path,
        EpochSpec(
            CONSOLE_EPOCH,
            tuple(gens),
            "2026-05-30T00:00:00Z",
            structure="swiss",
            params={"rounds": 2},
        ),
    )
    layout = WorkspaceLayout.from_root(root)
    write_json(
        layout.active_tournament,
        {
            "tournament_id": f"{CONSOLE_EPOCH}:field:v1",
            "epoch_id": CONSOLE_EPOCH,
            "structure": "swiss",
            "phase": "proposing",
            "structure_params": {"rounds": 2},
            "competitors": [dict(c) for c in _competitors("v0", [])],
            "rounds": [],
            "standings": [],
            "field_status": [dict(row) for row in field_status],
            "state": "in_progress",
        },
        indent=2,
    )
    return root


def build_swiss_all_applied_workspace(tmp_path: Path) -> Path:
    """A settled swiss round whose two proposals were both applied."""
    return _swiss_with_field_status(
        tmp_path,
        (
            {"generation_id": "v1", "status": "applied", "reason": "", "seed": 2},
            {"generation_id": "v2", "status": "applied", "reason": "", "seed": 3},
        ),
    )


def build_swiss_rated_workspace(tmp_path: Path) -> Path:
    """A swiss ladder over three competitors whose standings carry a rating.

    v1 played five rounds and won them all, so its rating clears the
    five-game floor the console renders as settled; v2 played once, so its
    rating is thin; v5 sits in the standings without a game and carries the
    null triple.
    """
    opponents = ["v0", "v2", "v3", "v4", "v0"]
    rounds = tuple(
        {
            "round_index": i,
            "label": f"Round {i + 1}",
            "matches": [
                {
                    "match_id": f"r{i}m0",
                    "competitors": ["v1", opponent],
                    "winner": "v1",
                    "delta_scalar": -0.1,
                }
            ],
        }
        for i, opponent in enumerate(opponents)
    )
    gens = (*_structure_gens("swiss", 5), Gen("v5", "v0", None, lineage_promoted=None))
    return build_scenario(
        tmp_path,
        EpochSpec(
            CONSOLE_EPOCH,
            gens,
            "2026-05-30T00:00:00Z",
            structure="swiss",
            params={"rounds": 5},
            fields=(
                FieldRecord(
                    "v1",
                    "swiss",
                    _competitors("v0", ["v1", "v2", "v3", "v4", "v5"]),
                    rounds=rounds,
                    promoted="v1",
                    champion="v0",
                    decision="promoted",
                    standings=(
                        {
                            "generation_id": "v1",
                            "rank": 1,
                            "scalar": 0.4,
                            "wins": 5,
                            "losses": 0,
                            "status": "champion",
                        },
                        {
                            "generation_id": "v0",
                            "rank": 2,
                            "scalar": 0.5,
                            "wins": 0,
                            "losses": 2,
                            "status": "alive",
                        },
                        {
                            "generation_id": "v2",
                            "rank": 3,
                            "scalar": 0.6,
                            "wins": 0,
                            "losses": 1,
                            "status": "eliminated",
                        },
                        {
                            "generation_id": "v5",
                            "rank": 4,
                            "scalar": 0.7,
                            "wins": 0,
                            "losses": 0,
                            "status": "eliminated",
                        },
                    ),
                ),
            ),
        ),
    )


#: The rungs the racing round scenarios publish: four competitors, v3 cut at
#: rung 0, v2 cut at rung 1, v1 the survivor.
RACING_ROUND_RUNGS: tuple[dict[str, Any], ...] = (
    {
        "stage_index": 0,
        "label": "Rung 0",
        "matches": [
            {
                "match_id": "rung0",
                "competitors": ["v0", "v1", "v2", "v3"],
                "survivors": ["v1", "v2"],
                "cut": ["v3"],
                "board_fraction": 0.5,
            }
        ],
    },
    {
        "stage_index": 1,
        "label": "Rung 1",
        "matches": [
            {
                "match_id": "rung1",
                "competitors": ["v1", "v2"],
                "survivors": ["v1"],
                "cut": ["v2"],
                "board_fraction": 1.0,
            }
        ],
    },
)
RACING_ROUND_STANDINGS: tuple[dict[str, Any], ...] = (
    {"generation_id": "v1", "rank": 1, "scalar": 40.0, "status": "alive"},
    {"generation_id": "v0", "rank": 2, "scalar": 54.0, "status": "alive", "role": "champion"},
)
_RACING_ROUND_PARAMS: dict[str, Any] = {"board_fraction": 0.5, "eta": 2, "field_size": 4}


def _racing_round_workspace(tmp_path: Path, *, live: bool) -> Path:
    """One racing round over four competitors, settled or still in flight.

    Settled, the field record carries the rungs and the standings. In flight,
    the field record is the empty one the runner opens the round with, and
    the rungs are published on the runtime envelope alone.
    """
    gens = (
        Gen("v0", lineage_promoted=True, scalar=54.0, entries=(("b0", 54.0, True),)),
        Gen(
            "v1",
            "v0",
            None if live else "promoted",
            lineage_promoted=None if live else True,
            structure="racing",
            scalar=None if live else 40.0,
            entries=() if live else (("b0", 40.0, True),),
        ),
        Gen(
            "v2",
            "v0",
            None if live else "rejected",
            lineage_promoted=None if live else False,
            structure="racing",
        ),
        Gen(
            "v3",
            "v0",
            None if live else "rejected",
            lineage_promoted=None if live else False,
            structure="racing",
        ),
    )
    root = build_scenario(
        tmp_path,
        EpochSpec(
            CONSOLE_EPOCH,
            gens,
            "2026-05-30T00:00:00Z",
            structure="racing",
            params=_RACING_ROUND_PARAMS,
            closed=not live,
            fields=(
                FieldRecord(
                    "v1",
                    "racing",
                    _competitors("v0", ["v1", "v2", "v3"]),
                    rounds=() if live else RACING_ROUND_RUNGS,
                    standings=() if live else RACING_ROUND_STANDINGS,
                    promoted="" if live else "v1",
                    champion="v0",
                    decision="" if live else "promoted",
                    state="in_progress" if live else "settled",
                ),
            ),
        ),
    )
    if live:
        write_json(
            WorkspaceLayout.from_root(root).active_tournament,
            {
                "tournament_id": f"{CONSOLE_EPOCH}:field:v1",
                "epoch_id": CONSOLE_EPOCH,
                "structure": "racing",
                "phase": "tournament:round_1:running",
                "structure_params": dict(_RACING_ROUND_PARAMS),
                "competitors": [dict(c) for c in _competitors("v0", ["v1", "v2", "v3"])],
                # rung 0 has settled; rung 1 is still racing, so it names no
                # survivor and no cut yet.
                "rounds": [
                    dict(RACING_ROUND_RUNGS[0]),
                    {
                        **RACING_ROUND_RUNGS[1],
                        "matches": [
                            {**RACING_ROUND_RUNGS[1]["matches"][0], "survivors": [], "cut": []}
                        ],
                    },
                ],
                "standings": [dict(r) for r in RACING_ROUND_STANDINGS],
                "field_status": [],
                "state": "in_progress",
            },
            indent=2,
        )
    return root


def build_racing_round_settled_workspace(tmp_path: Path) -> Path:
    """A settled racing round whose field record carries the rungs."""
    return _racing_round_workspace(tmp_path, live=False)


def build_racing_round_live_workspace(tmp_path: Path) -> Path:
    """A racing round in flight: an empty field record and a live envelope."""
    return _racing_round_workspace(tmp_path, live=True)


def build_gauntlet_one_round_workspace(tmp_path: Path) -> Path:
    """The shared epoch with one rejected challenger and no round stamps."""
    return build_scenario(
        tmp_path,
        EpochSpec(
            CONSOLE_EPOCH,
            (
                Gen(
                    "v0",
                    lineage_promoted=True,
                    entries=(("waffles_single", 60.5, False),),
                    round_index=None,
                ),
                Gen(
                    "v1",
                    "v0",
                    "rejected",
                    entries=(("waffles_single", 60.5, False),),
                    round_index=None,
                    scalar_delta=75.71,
                ),
            ),
            "2026-05-30T00:00:00Z",
            goal="Make the presentation agent crisper.",
        ),
    )


def build_identity_workspace(tmp_path: Path) -> Path:
    """The candidate-identity fixture: a scalar improvement refused for a regressed entry.

    ``v2`` proposes to name the audience and demand an outline, and improves
    the scalar from 0.72 to 0.69 (clearing the 0.02 margin), but fails
    ``q3_metrics_outline``, which the champion passed, so the pass-rate
    monotonicity rule rejects it.
    """
    layout = workspace(tmp_path)
    write_workspace_config(layout, _workspace_config(), indent=2)
    entries = ("b1", "b2", "b3", "q3_metrics_outline")
    write_epoch(
        layout,
        IDENTITY_EPOCH,
        config={
            "id": IDENTITY_EPOCH,
            "created_at": "2026-08-01T00:00:00Z",
            "closed": True,
            "goal": "Identity.",
            "contract_hash": "hash-identity",
        },
        brief="# Brief\n\n## Goal\n\nIdentity.\n",
        scoring={
            "promote_margin": 0.02,
            "pass_rate_monotonicity": True,
            "tournament": {"structure": "gauntlet", "params": {}},
        },
        board=[
            {"board_meta": True, "disable_drift": False},
            *(
                {
                    "id": e,
                    "kind": "single_turn",
                    "input": f"Task {e}.",
                    "expectation": {"kind": "predicate", "spec": "ok"},
                    "weight": 1.0,
                }
                for e in entries
            ),
        ],
        indent=2,
    )
    set_current_epoch(layout, IDENTITY_EPOCH, newline=True)
    _seed(layout, IDENTITY_EPOCH, "2026-08-01T00:00:00Z")
    write_experiment(
        layout.root,
        IDENTITY_EPOCH,
        "v2",
        Experiment(
            id=f"exp_{IDENTITY_EPOCH}_v2",
            epoch_id=IDENTITY_EPOCH,
            generation_id="v2",
            parent_generation_id="v0",
            proposed_at="2026-08-01T00:30:00Z",
            hypothesis=HypothesisSpec(
                core_idea="Name the audience up front and demand a slide outline before prose",
                modulating=("prompt.system", "agent.temperature"),
                why=(
                    "The judge flags narrative drift whenever the agent starts "
                    "writing paragraphs first."
                ),
                expected_drift_movements=(
                    ExpectedDriftMovement(
                        kind="off_topic", direction="decrease", magnitude="medium"
                    ),
                ),
                expected_pass_rate_delta="+0.10 to +0.20",
                risks="A longer prompt may crowd out the task description.",
            ),
            patches=(
                Patch(
                    id="p1",
                    mutation_id="prompt.system",
                    op="replace",
                    new_content="a\nb\nc",
                    new_numeric=None,
                    new_enum=None,
                    rationale="Lead with the audience.",
                ),
                Patch(
                    id="p2",
                    mutation_id="agent.temperature",
                    op="set_numeric",
                    new_content=None,
                    new_numeric=0.3,
                    new_enum=None,
                    rationale="Steadier prose.",
                ),
            ),
            outcome=OutcomeRecord(
                ran_at="2026-08-01T01:00:00Z",
                drift_movements=(
                    DriftMovementActual(
                        kind="off_topic", from_rate=0.5, to_rate=0.3, hypothesis_match=True
                    ),
                ),
                pass_rate_delta=-0.25,
                drift_loss_delta=-0.03,
                scalar_score_delta=-0.03,
                tournament_decision="rejected",
                rejection_reason="pass_rate_regressed",
            ),
            round_index=0,
        ),
    )
    for gen, failing in (("v0", ()), ("v2", ("q3_metrics_outline",))):
        for i, entry in enumerate(entries):
            _run(
                layout,
                IDENTITY_EPOCH,
                gen,
                entry,
                run_id=f"run_{gen}_{entry}",
                drift_loss=0.5 + 0.1 * i,
                pass_fail=entry not in failing,
                runtime_ms=1000,
            )
        _gen_score(
            layout,
            IDENTITY_EPOCH,
            gen,
            scalar=0.72 if gen == "v0" else 0.69,
            pass_rate=1.0 if gen == "v0" else 0.75,
            components={"drift": 0.7 if gen == "v0" else 0.67, "schema": 0.02},
            per_entry={
                entry: {
                    "drift_loss": 0.5 + 0.1 * i,
                    "failure": 0.0,
                    "pass_fail": entry not in failing,
                    "score": None,
                }
                for i, entry in enumerate(entries)
            },
        )
    write_lineage(
        layout,
        {
            "epochs": [
                _lineage_epoch(
                    IDENTITY_EPOCH,
                    [("v0", None, True), ("v2", "v0", False)],
                    "2026-08-01T00:00:00Z",
                )
            ]
        },
        indent=2,
    )
    rebuild_index(layout.root)
    return layout.root


def build_field_count_workspace(tmp_path: Path) -> Path:
    """A swiss epoch with two scored challengers and one unscored orphan, v9.

    The records carry no round stamp, so the timeline falls back to the
    tournament records, which the orphan has none of.
    """
    return build_scenario(
        tmp_path,
        EpochSpec(
            FIELD_COUNT_EPOCH,
            (
                Gen("v0", lineage_promoted=True, entries=(("b1", 50.0, True),), round_index=None),
                Gen(
                    "v1",
                    "v0",
                    "rejected",
                    structure="swiss",
                    entries=(("b1", 60.0, True),),
                    round_index=None,
                ),
                Gen(
                    "v2",
                    "v0",
                    "rejected",
                    structure="swiss",
                    entries=(("b1", 55.0, True),),
                    round_index=None,
                ),
                Gen("v9", "v0", None, lineage_promoted=None, round_index=None),
            ),
            "2026-06-02T00:00:00Z",
            structure="swiss",
            params={"rounds": 3},
        ),
    )


def build_swiss_empty_workspace(tmp_path: Path) -> Path:
    """A swiss epoch with no generations yet."""
    return build_scenario(
        tmp_path,
        EpochSpec(
            CONSOLE_EPOCH, (), "2026-05-30T00:00:00Z", structure="swiss", params={"rounds": 3}
        ),
    )


def _model(
    gens: tuple[Gen, ...], *, structure: str = "gauntlet", fields: tuple[FieldRecord, ...] = ()
) -> EpochSpec:
    return EpochSpec(MODEL_EPOCH, gens, "2026-06-03T00:00:00Z", structure=structure, fields=fields)


def _scored(
    gid: str,
    parent: str | None,
    decision: str | None,
    scalar: float,
    *,
    promoted: bool | None = False,
    round_index: int | None = 0,
) -> Gen:
    return Gen(
        gid,
        parent,
        decision,
        lineage_promoted=promoted,
        entries=(("b1", scalar, True),),
        round_index=round_index,
    )


def build_model_round_stamps_workspace(tmp_path: Path) -> Path:
    """Two rounds by round stamp: v2 promoted in round 0, v3 and v4 minted in round 1."""
    return build_scenario(
        tmp_path,
        _model(
            (
                _scored("v0", None, None, 100.0, promoted=True),
                _scored("v1", "v0", "rejected", 110.0),
                _scored("v2", "v0", "promoted", 80.0, promoted=True),
                _scored("v3", "v2", "rejected", 85.0, round_index=1),
                _scored("v4", "v2", "rejected", 90.0, round_index=1),
            )
        ),
    )


def build_model_field_records_workspace(tmp_path: Path) -> Path:
    """Two swiss field records and no round stamps: v2 promoted, then v3 and v4 minted.

    A field record is indexed only with three or more competitors, so each
    round fields two challengers against its champion.
    """
    gens = (
        _scored("v0", None, None, 100.0, promoted=True, round_index=None),
        Gen(
            "v1",
            "v0",
            "rejected",
            entries=(("b1", 110.0, True),),
            round_index=None,
            structure="swiss",
        ),
        Gen(
            "v2",
            "v0",
            "promoted",
            lineage_promoted=True,
            entries=(("b1", 80.0, True),),
            round_index=None,
            structure="swiss",
        ),
        Gen(
            "v3",
            "v2",
            "rejected",
            entries=(("b1", 90.0, True),),
            round_index=None,
            structure="swiss",
        ),
        Gen(
            "v4",
            "v2",
            "rejected",
            entries=(("b1", 95.0, True),),
            round_index=None,
            structure="swiss",
        ),
    )
    fields = (
        FieldRecord(
            "v1",
            "swiss",
            _competitors("v0", ["v1", "v2"]),
            promoted="v2",
            champion="v0",
            decision="promoted",
            ran_at="2026-06-03T01:00:00Z",
        ),
        FieldRecord(
            "v3",
            "swiss",
            _competitors("v2", ["v3", "v4"]),
            champion="v2",
            decision="rejected",
            ran_at="2026-06-03T02:00:00Z",
        ),
    )
    return build_scenario(tmp_path, _model(gens, structure="swiss", fields=fields))


def build_model_matchups_workspace(tmp_path: Path) -> Path:
    """Two rejected gauntlet duels with no round stamps."""
    return build_scenario(
        tmp_path,
        _model(
            (
                _scored("v0", None, None, 70.0, promoted=True, round_index=None),
                _scored("v1", "v0", "rejected", 146.0, round_index=None),
                _scored("v2", "v0", "rejected", 72.0, round_index=None),
            )
        ),
    )


def build_model_settled_round_workspace(tmp_path: Path) -> Path:
    """Round 0 settled with v2 promoted; a later round proposes live in the browser."""
    return build_scenario(
        tmp_path,
        _model(
            (
                _scored("v0", None, None, 100.0, promoted=True),
                _scored("v1", "v0", "rejected", 110.0),
                _scored("v2", "v0", "promoted", 80.0, promoted=True),
            )
        ),
    )


def build_model_promoted_pair_workspace(tmp_path: Path) -> Path:
    """The seed and one promoted challenger, both in round 0."""
    return build_scenario(
        tmp_path,
        _model(
            (
                _scored("v0", None, None, 100.0, promoted=True),
                _scored("v1", "v0", "promoted", 80.0, promoted=True),
            )
        ),
    )


def build_model_recorded_round_workspace(tmp_path: Path) -> Path:
    """Round 0 promoted v1; round 1 already records v5 against it."""
    return build_scenario(
        tmp_path,
        _model(
            (
                _scored("v0", None, None, 100.0, promoted=True),
                _scored("v1", "v0", "promoted", 80.0, promoted=True),
                _scored("v5", "v1", "rejected", 85.0, round_index=1),
            )
        ),
    )


def build_model_seed_only_workspace(tmp_path: Path) -> Path:
    """Only the seed exists; round 0 is still proposing its first field."""
    return build_scenario(
        tmp_path, _model((_scored("v0", None, None, 100.0, promoted=False, round_index=None),))
    )


def build_model_champion_modes_workspace(tmp_path: Path) -> Path:
    """Two rounds whose records ran the champion full, then reused it cached."""
    return build_scenario(
        tmp_path,
        _model(
            (
                Gen("v0", lineage_promoted=False, entries=(("b1", 5.0, True),)),
                Gen(
                    "v1",
                    "v0",
                    "rejected",
                    structure="swiss",
                    entries=(("b1", 6.0, True),),
                    champion_eval_mode="full",
                ),
                Gen(
                    "v2",
                    "v0",
                    "rejected",
                    structure="swiss",
                    entries=(("b1", 7.0, True),),
                    round_index=1,
                    champion_eval_mode="fast",
                ),
            ),
            structure="swiss",
        ),
    )


def build_model_waterfall_workspace(tmp_path: Path) -> Path:
    """A promotion that drops the loss floor from 20 to 14, then a held round."""
    return build_scenario(
        tmp_path,
        _model(
            (
                _scored("v0", None, None, 20.0, promoted=True, round_index=None),
                _scored("v1", "v0", "promoted", 14.0, promoted=True),
                _scored("v2", "v1", "rejected", 16.0, round_index=1),
            )
        ),
    )


__all__ = [
    "CONSOLE_BOARD",
    "CONSOLE_EPOCH",
    "FIELD_COUNT_EPOCH",
    "IDENTITY_EPOCH",
    "MANY_ROUNDS_EPOCH",
    "MODEL_EPOCH",
    "NEWER_EPOCH",
    "OLDER_EPOCH",
    "RACING_EPOCH",
    "build_cached_champion_workspace",
    "build_console_workspace",
    "build_double_elim_workspace",
    "build_episodes_workspace",
    "build_field_count_workspace",
    "build_gauntlet_one_round_workspace",
    "build_identity_workspace",
    "build_many_rounds_workspace",
    "build_model_champion_modes_workspace",
    "build_model_field_records_workspace",
    "build_model_matchups_workspace",
    "build_model_promoted_pair_workspace",
    "build_model_recorded_round_workspace",
    "build_model_round_stamps_workspace",
    "build_model_seed_only_workspace",
    "build_model_settled_round_workspace",
    "build_model_waterfall_workspace",
    "build_racing_field_workspace",
    "build_racing_ladder_workspace",
    "build_racing_no_records_workspace",
    "build_racing_round_live_workspace",
    "build_racing_round_settled_workspace",
    "build_single_elim_workspace",
    "build_swiss_all_applied_workspace",
    "build_swiss_all_rejected_workspace",
    "build_swiss_empty_workspace",
    "build_swiss_proposing_workspace",
    "build_swiss_rated_workspace",
    "build_swiss_workspace",
    "build_two_epochs_workspace",
]
