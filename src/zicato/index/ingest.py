"""Build and incrementally update the zicato analytical index.

Public entry points:

* :func:`rebuild_index` — the canonical "rebuild from files" path.
  Re-derives every row by walking every epoch / generation / run under
  ``.zicato/``, into a scratch file that is renamed over ``index.db``
  on success. Backs ``zicato repair index``.
* :func:`ensure_index` — builds the index when it is absent, older than
  :data:`~zicato.index.schema.SCHEMA_VERSION`, or unreadable, and does
  nothing otherwise. Runs at ``evolve`` start and at dashboard start.
* :func:`validate_index` / :func:`heal_index` — compare each epoch's
  persisted cursor against cheap workspace signals and re-project only
  the epochs that diverged. Runs at ``evolve`` start. Together with
  ``ensure_index`` these are why routine ``zicato repair index`` is not a
  thing an operator should ever have to do
  (``docs/design/ANALYTICAL-INDEX.md`` §5).
* :func:`ingest_run` — incrementally upserts one run's ``runs`` /
  ``loss_profiles`` / ``metric_counts`` rows. The orchestrator calls
  this for live dual-write the moment a run's ``loss.json`` lands
  (R9-4).
* :func:`ingest_experiment` — incrementally upserts one experiment, its
  patches, and (when the experiment has resolved) its tournament row.

Source-of-truth rule
--------------------
Everything ingested is *derived*. The files under ``.zicato/`` are
canonical; the index holds nothing that is not already on disk. That is
why :func:`rebuild_index` can drop and recreate the database with no
loss — it is purely a re-projection of the files.

Idempotency
-----------
Every write is an ``INSERT ... ON CONFLICT DO UPDATE`` upsert keyed on
the natural primary key (``run_id``, ``(epoch_id, generation_id)``,
``patch_id``, ``tournament_id``). Running :func:`ingest_run` or
:func:`ingest_experiment` twice produces the same rows; running
:func:`rebuild_index` repeatedly is a no-op beyond the file drop.

Reading source files
--------------------
The index reuses zicato's own readers wherever they exist
(:func:`zicato.epoch.lineage.load_lineage`,
:func:`zicato.epoch.journal.read_experiment`,
:func:`zicato.telemetry.reducer.read_loss_profile`), and routes epoch
enumeration / ordering through the canonical workspace-read layer
(:func:`zicato.workspace.iter_epochs`), so the index never re-derives a
parse — or an epoch ordering — that a canonical module already owns. The
index is a pure projection of the canonical files: ``metric_counts`` is
derived solely from each run's ``loss.json`` (via
:meth:`zicato.core.loss.LossProfile.unified_metrics`); it never
independently re-tallies a run's events JSONL.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import os
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from zicato.core.types import Experiment, LossProfile
from zicato.core.workspace import loss_profile_path
from zicato.index.schema import (
    SCHEMA_VERSION,
    apply_schema,
    raise_if_newer,
    read_schema_version,
)

log = logging.getLogger("zicato.index")


def _now_iso() -> str:
    """UTC now as ``YYYY-MM-DDTHH:MM:SSZ`` — the workspace timestamp shape."""
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_db_path(workspace_root: Path) -> Path:
    """The canonical index location: ``{workspace_root}/index.db``."""
    return workspace_root / "index.db"


# ---------------------------------------------------------------------------
# Cross-epoch + cross-generation lineage resolvers
# ---------------------------------------------------------------------------


def _parent_epoch_id_from_lineage_entry(entry: dict[str, Any] | None) -> str | None:
    """Extract the parent epoch id from a single ``lineage.json`` entry.

    The ``v0_parent`` field is the canonical pointer at the prior
    epoch's promoted leaf. In current writers it carries the bare
    parent epoch id (the design comment in :mod:`zicato.epoch.lineage`
    flags a planned ``{epoch}:{gen}`` form). We accept either:

    * a bare ``"epoch_id"`` string — used verbatim,
    * a ``"epoch_id:generation_id"`` string — the ``epoch_id`` half
      is the answer,
    * ``None`` — the workspace's first epoch has no parent.

    A non-string value collapses to ``None``.
    """
    if entry is None:
        return None
    raw = entry.get("v0_parent")
    if not isinstance(raw, str) or not raw:
        return None
    # Tolerate the planned "{epoch}:{gen}" form by stripping the
    # generation suffix when present.
    if ":" in raw:
        return raw.split(":", 1)[0]
    return raw


def _tournament_id_for_run(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
) -> str | None:
    """Resolve the tournament a run belongs to from its generation's experiment.

    A "tournament round" between a parent and a child generation is
    keyed as ``"{epoch_id}:{parent_gen}->{child_gen}"`` — the same id
    the ``tournaments`` table already uses. The child generation's
    ``experiment.json`` carries the ``parent_generation_id`` field,
    which is exactly the parent half of the key.

    The runs that belong to a tournament are the runs under the
    *child* generation: a tournament round runs the challenger across
    every board entry, and the parent's scores are read from the
    parent generation's cached ``gen_score.json`` rather than re-run.
    So the helper looks up the child's experiment, not the parent's.

    Returns ``None`` when the generation has no ``experiment.json``
    (e.g. a ``v0`` seed) — those runs are champion-only fast-cache
    runs with no tournament round attached.
    """
    from zicato.epoch.journal import read_experiment  # noqa: PLC0415

    try:
        experiment = read_experiment(workspace_root, epoch_id, generation_id)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None
    parent = experiment.parent_generation_id
    if not parent:
        return None
    return f"{epoch_id}:{parent}->{generation_id}"


def _namespace_of(metric_name: str) -> str:
    """Return the namespace prefix of a metric name (``""`` if unnamespaced).

    The :class:`zicato.core.types.MetricCount` convention is
    ``"<namespace>:<key>"``. We keep the namespace WITHOUT the trailing
    colon in the ``metric_counts.namespace`` column so a query can
    ``WHERE namespace = 'drift'`` without remembering the punctuation.
    """
    idx = metric_name.find(":")
    return metric_name[:idx] if idx > 0 else ""


# ---------------------------------------------------------------------------
# Row writers (upserts)
# ---------------------------------------------------------------------------


def _upsert_epoch(
    conn: sqlite3.Connection,
    epoch_id: str,
    contract_hash: str | None,
    created_at: str,
    closed: bool,
    goal: str = "",
    parent_epoch_id: str | None = None,
) -> None:
    """Upsert one ``epochs`` row.

    ``contract_hash`` is the descriptive hash from the epoch's
    ``config.json``; ``None`` (a pre-hash / legacy epoch) projects to the
    column's empty-string form so the index wire shape is unchanged. The
    index column is purely derived — it does not feed the canonicalizer.

    ``goal`` is the operator's free-form statement of why this epoch
    exists, written to the per-epoch ``config.json``. Empty string for
    epochs created before the field existed.

    ``parent_epoch_id`` is the id of the epoch this one was forked off
    of (the prior epoch in the workspace's lineage). The schema
    permits ``NULL`` for the workspace's first epoch.

    The upsert never clobbers an existing ``parent_epoch_id`` with
    ``NULL`` — incremental writers that don't know the parent (e.g.
    ``_upsert_owning_epoch_generation`` opened off a partially-rebuilt
    lineage) pass ``None`` and the existing value is preserved via
    ``COALESCE``. The canonical rebuild path always knows the parent
    from ``lineage.json`` and writes it explicitly.
    """
    conn.execute(
        "INSERT INTO epochs(epoch_id, contract_hash, created_at, closed, goal, parent_epoch_id) "
        "VALUES(?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(epoch_id) DO UPDATE SET "
        "contract_hash = excluded.contract_hash, "
        "created_at = excluded.created_at, "
        "closed = excluded.closed, "
        "goal = excluded.goal, "
        "parent_epoch_id = COALESCE(excluded.parent_epoch_id, epochs.parent_epoch_id)",
        (epoch_id, contract_hash or "", created_at, 1 if closed else 0, goal, parent_epoch_id),
    )


def _round_index_from_lineage_gen(gen: dict[str, Any]) -> int | None:
    """Extract a generation's birth ``round_index`` from its lineage dict.

    Legacy lineage rows predate the field, so an absent or non-integer
    value reads as ``None`` (birth round unknown) — the index column is
    nullable and consumers degrade on a null.
    """
    raw = gen.get("round_index")
    if isinstance(raw, bool):
        # ``bool`` is an ``int`` subclass; a stray boolean is not a round.
        return None
    if isinstance(raw, int):
        return raw
    return None


def _upsert_generation(
    conn: sqlite3.Connection,
    epoch_id: str,
    generation_id: str,
    parent_generation_id: str | None,
    promoted: bool,
    created_at: str,
    round_index: int | None = None,
) -> None:
    # ``round_index`` is the birth round of the generation. It is
    # written via COALESCE so a partial upsert (e.g. the
    # experiment-derived path, which does not know the round) never
    # clobbers a value a lineage-derived pass already set. A legacy
    # generation whose lineage row predates the field leaves it NULL —
    # birth round unknown — which consumers read as absent.
    conn.execute(
        "INSERT INTO generations("
        "epoch_id, generation_id, parent_generation_id, promoted, created_at, round_index) "
        "VALUES(?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(epoch_id, generation_id) DO UPDATE SET "
        "parent_generation_id = excluded.parent_generation_id, "
        "promoted = excluded.promoted, "
        "created_at = excluded.created_at, "
        "round_index = COALESCE(excluded.round_index, generations.round_index)",
        (
            epoch_id,
            generation_id,
            parent_generation_id,
            1 if promoted else 0,
            created_at,
            round_index,
        ),
    )


def _upsert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
    started_at: str,
    ended_at: str,
    aborted: bool,
    runtime_ms: int,
    tournament_id: str | None = None,
    match_id: str | None = None,
) -> None:
    """Upsert one ``runs`` row.

    ``tournament_id`` is the FK link back to a ``tournaments`` row.
    NULL is permitted for old rows (pre-v2 schema) and for runs that
    have no tournament round (champion-only fast-cache runs under a
    ``v0`` seed). The upsert preserves an existing ``tournament_id``
    via ``COALESCE`` so a re-ingest that cannot resolve the round
    (e.g. the child's ``experiment.json`` was deleted) does not clear
    the column.

    ``match_id`` (schema v4) is the per-board-run tournament-provenance
    tag — the matchup id this run executed within (e.g. ``"rung0_m2"``,
    ``"racing-final"``). NULL for legacy runs persisted before the tag
    existed and for runs that ran outside a tagged matchup (a gauntlet
    duel, which never carries a ``match_id``). Same ``COALESCE`` story
    as ``tournament_id``: a re-ingest that cannot recover the tag leaves
    an existing value intact.
    """
    conn.execute(
        "INSERT INTO runs("
        "run_id, epoch_id, generation_id, entry_id, started_at, ended_at, "
        "aborted, runtime_ms, tournament_id, match_id) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET "
        "epoch_id = excluded.epoch_id, "
        "generation_id = excluded.generation_id, "
        "entry_id = excluded.entry_id, "
        "started_at = excluded.started_at, "
        "ended_at = excluded.ended_at, "
        "aborted = excluded.aborted, "
        "runtime_ms = excluded.runtime_ms, "
        "tournament_id = COALESCE(excluded.tournament_id, runs.tournament_id), "
        "match_id = COALESCE(excluded.match_id, runs.match_id)",
        (
            run_id,
            epoch_id,
            generation_id,
            entry_id,
            started_at,
            ended_at,
            1 if aborted else 0,
            int(runtime_ms),
            tournament_id,
            match_id,
        ),
    )


def _upsert_loss_profile(
    conn: sqlite3.Connection,
    profile: LossProfile,
    tournament_id: str | None = None,
) -> None:
    """Upsert one ``loss_profiles`` row.

    ``tournament_id`` matches the FK on the parallel ``runs`` row
    (same nullability story). Preserves any pre-existing value via
    ``COALESCE`` so a re-ingest that can't resolve the round leaves
    the column intact.

    ``match_id`` (schema v4) is read straight off the profile — the
    runner stamps it onto ``LossProfile.match_id`` (and into the run's
    ``loss.json``) for runs that executed within a tagged matchup. An
    empty string on the profile means "untagged" (a gauntlet / ad-hoc /
    legacy run) and is stored as NULL so the column reads consistently
    with the ``runs`` table.
    """
    match_id = getattr(profile, "match_id", "") or None
    # Carried-over (cached) champion provenance (schema v6). ``cached``
    # distinguishes a materialised carry-forward row from a fresh live
    # evaluation so a reader never double-counts the champion; the
    # ``source_*`` columns name where the live evaluation happened.
    cached = 1 if getattr(profile, "cached", False) else 0
    source_epoch = getattr(profile, "source_epoch", "") or None
    source_run = getattr(profile, "source_run", "") or None
    # Abort-cause provenance (schema v9). Read straight off the profile: the
    # runner/worker stamp it onto ``LossProfile.abort_cause`` for synthesised
    # aborted profiles (``budget_exhausted`` vs the infra causes). An empty /
    # absent value (a cleanly-reduced run, or a legacy profile) is stored as
    # NULL so a reader can ``WHERE abort_cause = 'parent_kill'`` to spot an
    # over-firing watchdog without re-parsing the ``loss_json`` blob.
    abort_cause = getattr(profile, "abort_cause", None) or None
    conn.execute(
        "INSERT INTO loss_profiles("
        "run_id, epoch_id, generation_id, entry_id, drift_loss, pass_fail, "
        "runtime_ms, wall_clock_budget_exceeded, loss_json, tournament_id, match_id, "
        "cached, source_epoch, source_run, abort_cause) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET "
        "epoch_id = excluded.epoch_id, "
        "generation_id = excluded.generation_id, "
        "entry_id = excluded.entry_id, "
        "drift_loss = excluded.drift_loss, "
        "pass_fail = excluded.pass_fail, "
        "runtime_ms = excluded.runtime_ms, "
        "wall_clock_budget_exceeded = excluded.wall_clock_budget_exceeded, "
        "loss_json = excluded.loss_json, "
        "tournament_id = COALESCE(excluded.tournament_id, loss_profiles.tournament_id), "
        "match_id = COALESCE(excluded.match_id, loss_profiles.match_id), "
        "cached = excluded.cached, "
        "source_epoch = COALESCE(excluded.source_epoch, loss_profiles.source_epoch), "
        "source_run = COALESCE(excluded.source_run, loss_profiles.source_run), "
        "abort_cause = excluded.abort_cause",
        (
            profile.run_id,
            profile.epoch_id,
            profile.generation_id,
            profile.entry_id,
            float(profile.drift_loss),
            _bool_to_int_or_none(profile.pass_fail),
            int(profile.runtime_ms),
            1 if profile.wall_clock_budget_exceeded else 0,
            json.dumps(asdict(profile), sort_keys=True),
            tournament_id,
            match_id,
            cached,
            source_epoch,
            source_run,
            abort_cause,
        ),
    )


def _bool_to_int_or_none(value: bool | None) -> int | None:
    """Map an optional bool to SQLite's ``0`` / ``1`` / ``NULL``.

    ``pass_fail`` is genuinely tri-state — ``None`` means "no
    expectation was attached to the entry" — so we preserve ``NULL``
    rather than collapsing it to ``0``.
    """
    if value is None:
        return None
    return 1 if value else 0


def _replace_judge_losses(
    conn: sqlite3.Connection,
    run_id: str,
    profile: LossProfile,
) -> None:
    """Rewrite the ``judge_losses`` rows for one run.

    ``judge_losses`` is keyed on ``(run_id, judge_name)``; the natural
    primary key would let us upsert in-place, but the canonical rebuild
    path is a delete-then-insert per run so re-ingest after a reducer
    change cannot leave a stale ``(run_id, judge_name)`` row behind when
    a judge is removed from a board. The rows are sourced directly from
    :attr:`LossProfile.per_judge_loss` — the reducer's already-
    attributed per-judge view — so the index never re-derives the
    attribution itself.
    """
    conn.execute("DELETE FROM judge_losses WHERE run_id = ?", (run_id,))
    rows: list[tuple[str, str, float, float, float]] = []
    for jl in profile.per_judge_loss:
        rows.append(
            (
                run_id,
                jl.judge_name,
                float(jl.weighted_loss),
                float(jl.raw_loss),
                float(jl.weight),
            )
        )
    if rows:
        conn.executemany(
            "INSERT INTO judge_losses(run_id, judge_name, weighted_loss, raw_loss, weight) "
            "VALUES(?, ?, ?, ?, ?)",
            rows,
        )


def _replace_metric_counts(
    conn: sqlite3.Connection,
    run_id: str,
    profile: LossProfile,
) -> None:
    """Rewrite the ``metric_counts`` rows for one run.

    ``metric_counts`` has no natural primary key (a run produces many
    rows), so an idempotent upsert is a delete-then-insert keyed on
    ``run_id``. The rows come from
    :meth:`LossProfile.unified_metrics`, which yields every drift entry
    under the ``"drift:"`` namespace plus any non-drift namespaces the
    reducer derived (cost / output / schema). That covers the contract
    requirement that metric_counts is populated from BOTH the drift
    events and the LossProfile's own metric surface.
    """
    conn.execute("DELETE FROM metric_counts WHERE run_id = ?", (run_id,))
    rows: list[tuple[str, str, str, str, float]] = []
    for mc in profile.unified_metrics():
        rows.append(
            (
                run_id,
                _namespace_of(mc.name),
                mc.name,
                mc.severity,
                float(mc.count),
            )
        )
    if rows:
        conn.executemany(
            "INSERT INTO metric_counts(run_id, namespace, name, severity, count) "
            "VALUES(?, ?, ?, ?, ?)",
            rows,
        )


def _upsert_experiment(conn: sqlite3.Connection, experiment: Experiment) -> None:
    """Write the ``experiments`` row + every ``patches`` row for one experiment."""
    hyp = experiment.hypothesis
    outcome = experiment.outcome
    if outcome is not None:
        decision: str | None = outcome.tournament_decision
        rejection_reason: str | None = outcome.rejection_reason
        scalar_delta: float | None = outcome.scalar_score_delta
        drift_delta: float | None = outcome.drift_loss_delta
        pass_delta: float | None = outcome.pass_rate_delta
        outcome_json: str | None = json.dumps(asdict(outcome), sort_keys=True)
    else:
        decision = None
        rejection_reason = None
        scalar_delta = None
        drift_delta = None
        pass_delta = None
        outcome_json = None

    conn.execute(
        "INSERT INTO experiments("
        "epoch_id, generation_id, hypothesis_core_idea, hypothesis_why, "
        "hypothesis_json, tournament_decision, rejection_reason, "
        "scalar_score_delta, drift_loss_delta, pass_rate_delta, outcome_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(epoch_id, generation_id) DO UPDATE SET "
        "hypothesis_core_idea = excluded.hypothesis_core_idea, "
        "hypothesis_why = excluded.hypothesis_why, "
        "hypothesis_json = excluded.hypothesis_json, "
        "tournament_decision = excluded.tournament_decision, "
        "rejection_reason = excluded.rejection_reason, "
        "scalar_score_delta = excluded.scalar_score_delta, "
        "drift_loss_delta = excluded.drift_loss_delta, "
        "pass_rate_delta = excluded.pass_rate_delta, "
        "outcome_json = excluded.outcome_json",
        (
            experiment.epoch_id,
            experiment.generation_id,
            hyp.core_idea,
            hyp.why,
            json.dumps(asdict(hyp), sort_keys=True),
            decision,
            rejection_reason,
            scalar_delta,
            drift_delta,
            pass_delta,
            outcome_json,
        ),
    )

    for patch in experiment.patches:
        conn.execute(
            "INSERT INTO patches("
            "patch_id, epoch_id, generation_id, mutation_id, op, rationale) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(patch_id) DO UPDATE SET "
            "epoch_id = excluded.epoch_id, "
            "generation_id = excluded.generation_id, "
            "mutation_id = excluded.mutation_id, "
            "op = excluded.op, "
            "rationale = excluded.rationale",
            (
                patch.id,
                experiment.epoch_id,
                experiment.generation_id,
                patch.mutation_id,
                patch.op,
                patch.rationale,
            ),
        )


def _upsert_tournament(conn: sqlite3.Connection, experiment: Experiment) -> None:
    """Write a ``tournaments`` row for a resolved experiment.

    A no-op when the experiment has no outcome yet — an unresolved
    experiment has no tournament. The tournament id is derived as
    ``"{epoch_id}:{parent}->{child}"`` so it is stable across rebuilds
    and unique per parent/child pairing.

    The ``parent_scalar`` / ``child_scalar`` columns are not carried
    by :class:`zicato.core.types.OutcomeRecord` directly — the outcome
    records only the *delta*. We store the delta in ``delta_scalar``
    and leave the absolute scalars ``NULL``; a consumer that needs the
    absolutes joins against the generations' cached ``gen_score.json``.
    """
    outcome = experiment.outcome
    if outcome is None:
        return
    tournament_id = (
        f"{experiment.epoch_id}:{experiment.parent_generation_id}->{experiment.generation_id}"
    )
    # v3 structure-aware columns. The existing per-matchup columns keep
    # describing the CROWNING match (the match that decided who becomes
    # champion) for every structure, so a gauntlet-only reader still gets
    # a coherent champion-vs-challenger answer. ``structure`` defaults to
    # ``"gauntlet"`` for runs that predate the feature (the OutcomeRecord
    # field defaults there too). ``competitors_json`` is the candidate
    # field this generation faced, derived from its per-match opponents;
    # for a gauntlet that is just parent + child.
    structure = outcome.structure or "gauntlet"
    competitors = [experiment.parent_generation_id, experiment.generation_id]
    for m in outcome.match_record:
        if m.opponent and m.opponent not in competitors:
            competitors.append(m.opponent)
    rounds = [
        {
            "match_id": m.match_id,
            "opponent": m.opponent,
            "won": m.won,
            "delta_scalar": m.delta_scalar,
        }
        for m in outcome.match_record
    ]
    # v8 per-round champion-eval provenance. ``champion_eval_mode`` is the
    # crowning matchup's OutcomeRecord field — how the champion (parent /
    # left) side was evaluated this round. It defaults to ``"full"`` on the
    # dataclass for journals that predate the field, so reading it here is
    # always one of ``"full"`` / ``"fast"`` / ``"fast-degraded"``.
    champion_eval_mode = outcome.champion_eval_mode or "full"
    # ``champion_run_ref`` is a best-effort pointer at the champion's
    # per-round run/output. The cleanest already-available reference is the
    # champion (parent) generation's workspace-relative directory, where its
    # per-board runs / loss files live — derived purely from ids we already
    # hold, no filesystem walk. When there is no parent generation (a v0
    # seed has no crowning matchup, so this branch is not normally reached)
    # we write NULL rather than inventing a reference.
    champion_run_ref: str | None = (
        f"epochs/{experiment.epoch_id}/generations/{experiment.parent_generation_id}"
        if experiment.parent_generation_id
        else None
    )
    conn.execute(
        "INSERT INTO tournaments("
        "tournament_id, epoch_id, parent_generation_id, child_generation_id, "
        "decision, parent_scalar, child_scalar, delta_scalar, rejection_reason, "
        "ran_at, structure, structure_params_json, competitors_json, rounds_json, "
        "standings_json, field_status_json, champion_eval_mode, champion_run_ref) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tournament_id) DO UPDATE SET "
        "epoch_id = excluded.epoch_id, "
        "parent_generation_id = excluded.parent_generation_id, "
        "child_generation_id = excluded.child_generation_id, "
        "decision = excluded.decision, "
        "parent_scalar = excluded.parent_scalar, "
        "child_scalar = excluded.child_scalar, "
        "delta_scalar = excluded.delta_scalar, "
        "rejection_reason = excluded.rejection_reason, "
        "ran_at = excluded.ran_at, "
        "structure = excluded.structure, "
        "structure_params_json = excluded.structure_params_json, "
        "competitors_json = excluded.competitors_json, "
        "rounds_json = excluded.rounds_json, "
        "standings_json = excluded.standings_json, "
        # Keep any field-status the settle path may have written: the
        # per-experiment crowning record does not carry the proposing
        # outcomes, so COALESCE preserves an existing value rather than
        # clobbering it with this row's empty list.
        "field_status_json = COALESCE(tournaments.field_status_json, excluded.field_status_json), "
        "champion_eval_mode = excluded.champion_eval_mode, "
        # Preserve an existing run-ref rather than clobbering it with NULL
        # when a re-ingest cannot resolve a parent (defensive — the crowning
        # row always carries one).
        "champion_run_ref = COALESCE(excluded.champion_run_ref, tournaments.champion_run_ref)",
        (
            tournament_id,
            experiment.epoch_id,
            experiment.parent_generation_id,
            experiment.generation_id,
            outcome.tournament_decision,
            None,
            None,
            float(outcome.scalar_score_delta),
            outcome.rejection_reason,
            outcome.ran_at,
            structure,
            json.dumps({}),
            json.dumps(competitors),
            json.dumps(rounds),
            json.dumps([]),
            json.dumps([]),
            champion_eval_mode,
            champion_run_ref,
        ),
    )


def _upsert_reflection(
    conn: sqlite3.Connection,
    *,
    reflection_id: str,
    epoch_id: str,
    created_at: str,
    mode: str,
    executed: bool,
    noise_floor_max_abs_delta: float | None,
    decision_flip_p: float | None,
    n_findings: int,
    n_judges: int,
    verdict_counts: dict[str, int],
) -> None:
    """Upsert one ``reflections`` row (the board-reflection projection, v11).

    The four-pillar bill-of-health summary of one reflection run, keyed on
    ``reflection_id``. ``noise_floor_max_abs_delta`` / ``decision_flip_p`` are
    the headline reliability numbers (``None`` when the reflection ran no
    reliability pillar — a passive/ingest-only pass over a workspace with no
    persisted floor). ``verdict_counts`` is the corpus-wide TP/FP/FN/TN/ambiguous
    tally, stored as JSON. Every write is a keyed upsert so a re-ingest (or a
    ``zicato repair index`` after the file was rewritten) is idempotent.
    """
    conn.execute(
        "INSERT INTO reflections("
        "reflection_id, epoch_id, created_at, mode, executed, "
        "noise_floor_max_abs_delta, decision_flip_p, n_findings, n_judges, "
        "verdict_counts_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(reflection_id) DO UPDATE SET "
        "epoch_id = excluded.epoch_id, "
        "created_at = excluded.created_at, "
        "mode = excluded.mode, "
        "executed = excluded.executed, "
        "noise_floor_max_abs_delta = excluded.noise_floor_max_abs_delta, "
        "decision_flip_p = excluded.decision_flip_p, "
        "n_findings = excluded.n_findings, "
        "n_judges = excluded.n_judges, "
        "verdict_counts_json = excluded.verdict_counts_json",
        (
            reflection_id,
            epoch_id,
            created_at,
            mode,
            1 if executed else 0,
            noise_floor_max_abs_delta,
            decision_flip_p,
            int(n_findings),
            int(n_judges),
            json.dumps(verdict_counts, sort_keys=True),
        ),
    )


def _upsert_judge_scorecards(
    conn: sqlite3.Connection,
    reflection_id: str,
    scorecards: list[dict[str, Any]],
) -> None:
    """Rewrite the ``judge_scorecards`` rows for one reflection.

    Delete-then-insert keyed on ``reflection_id`` (the same idempotent shape
    as :func:`_replace_judge_losses`) so a re-ingest after a scorecard set
    shrinks can never leave a stale ``(reflection_id, judge_name)`` row behind.
    Each ``scorecards`` dict is one :meth:`JudgeScorecard.to_json` — the
    ``self_consistency_kappa`` field projects to the ``kappa`` column and
    ``redundant_with`` to ``redundant_with_json``.
    """
    conn.execute("DELETE FROM judge_scorecards WHERE reflection_id = ?", (reflection_id,))
    rows: list[tuple[Any, ...]] = []
    for card in scorecards:
        rows.append(
            (
                reflection_id,
                str(card.get("judge_name", "")),
                _opt_int_field(card.get("tp")),
                _opt_int_field(card.get("fp")),
                _opt_int_field(card.get("fn")),
                _opt_int_field(card.get("tn")),
                _opt_int_field(card.get("ambiguous")),
                _opt_float_field(card.get("precision")),
                _opt_float_field(card.get("recall")),
                _opt_float_field(card.get("f1")),
                _opt_float_field(card.get("severity_accuracy")),
                _opt_float_field(card.get("disagreement_rate")),
                _opt_float_field(card.get("self_consistency_kappa")),
                1 if card.get("exercised") else 0,
                json.dumps(card.get("redundant_with") or [], sort_keys=True),
            )
        )
    if rows:
        conn.executemany(
            "INSERT INTO judge_scorecards("
            "reflection_id, judge_name, tp, fp, fn, tn, ambiguous, "
            "precision, recall, f1, severity_accuracy, disagreement_rate, kappa, "
            "exercised, redundant_with_json) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _opt_int_field(value: Any) -> int | None:
    """Coerce an optional integer scorecard field (``None`` / non-int ⇒ ``None``)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _opt_float_field(value: Any) -> float | None:
    """Coerce an optional float scorecard field (``None`` / non-number ⇒ ``None``)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _upsert_field_tournament(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    """Write the FIELD-level ``tournaments`` row for a settled tournament.

    Unlike :func:`_upsert_tournament` (one row PER CHALLENGER, describing
    that challenger's crowning duel), this writes ONE row for the whole
    round's tournament: the settled round-by-round pairings
    (``rounds_json``), the Copeland standings (``standings_json``), the
    full competitor field (``competitors_json``), and the proposing
    field-status (``field_status_json``) — the same shape the runtime
    ``active_tournament`` envelope carries, so the dashboard's structure
    renderers (which already consume that shape live) render the swiss /
    elim ladder post-run unchanged.

    The ``tournament_id`` is the field-level id
    ``"{epoch_id}:field:{first_challenger}"`` — stable per round and
    idempotent across rebuilds. The legacy per-matchup
    ``parent_generation_id`` / ``child_generation_id`` columns are left
    EMPTY: a field row is not a champion-vs-challenger duel, and a
    populated ``child_generation_id`` would collide with the per-challenger
    crowning row for the same generation (both keyed on the promoted id)
    and pollute the per-matchup ladder. The crowning verdict survives in
    ``decision`` and in the standings' ``champion`` status row. The
    per-challenger rows remain the gauntlet view's source and keep their
    own crowning columns untouched.

    A no-op for a degenerate two-competitor (gauntlet) field — the
    per-challenger row already covers it.
    """
    competitors = record.get("competitors") or []
    if len(competitors) < 3:
        # A pure gauntlet (champion + one challenger) needs no field row;
        # the per-challenger crowning row is the whole picture.
        return
    tournament_id = str(record.get("tournament_id") or "")
    epoch_id = str(record.get("epoch_id") or "")
    if not tournament_id or not epoch_id:
        return
    conn.execute(
        "INSERT INTO tournaments("
        "tournament_id, epoch_id, parent_generation_id, child_generation_id, "
        "decision, parent_scalar, child_scalar, delta_scalar, rejection_reason, "
        "ran_at, structure, structure_params_json, competitors_json, rounds_json, "
        "standings_json, field_status_json) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(tournament_id) DO UPDATE SET "
        "epoch_id = excluded.epoch_id, "
        "parent_generation_id = excluded.parent_generation_id, "
        "child_generation_id = excluded.child_generation_id, "
        "decision = excluded.decision, "
        "delta_scalar = excluded.delta_scalar, "
        "rejection_reason = excluded.rejection_reason, "
        "ran_at = excluded.ran_at, "
        "structure = excluded.structure, "
        "structure_params_json = excluded.structure_params_json, "
        "competitors_json = excluded.competitors_json, "
        "rounds_json = excluded.rounds_json, "
        "standings_json = excluded.standings_json, "
        "field_status_json = excluded.field_status_json",
        (
            tournament_id,
            epoch_id,
            "",
            "",
            str(record.get("decision") or ""),
            None,
            None,
            record.get("delta_scalar"),
            str(record.get("reason") or ""),
            str(record.get("ran_at") or ""),
            str(record.get("structure") or "gauntlet"),
            json.dumps(record.get("structure_params") or {}),
            json.dumps(list(competitors)),
            json.dumps(record.get("rounds") or []),
            json.dumps(record.get("standings") or []),
            json.dumps(record.get("field_status") or []),
        ),
    )


# ---------------------------------------------------------------------------
# Source-file readers
# ---------------------------------------------------------------------------


def _load_loss_profile(path: Path) -> LossProfile | None:
    """Read one ``loss.json`` via the reducer's reader; ``None`` on failure.

    The reducer owns the canonical ``LossProfile`` (de)serialisation,
    including the back-compat handling for profiles written before the
    generalised metric surface — so the index reuses it rather than
    re-deriving the parse.
    """
    from zicato.telemetry.reducer import read_loss_profile  # noqa: PLC0415

    try:
        return read_loss_profile(path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _iter_generation_dirs(workspace_root: Path, epoch_id: str) -> Iterable[str]:
    """Yield generation ids that have a directory on disk under ``epoch_id``.

    The lineage DAG is the authoritative generation list, but a run can
    land before lineage is updated; walking the directory tree as well
    means :func:`rebuild_index` never misses a generation that has
    telemetry on disk.
    """
    gens_root = workspace_root / "epochs" / epoch_id / "generations"
    if not gens_root.exists():
        return []
    out: list[str] = []
    for child in sorted(gens_root.iterdir()):
        if child.is_dir():
            out.append(child.name)
    return out


def _iter_run_entry_ids(workspace_root: Path, epoch_id: str, generation_id: str) -> list[str]:
    """Yield board-entry ids that have a ``runs/`` subdirectory on disk."""
    runs_root = workspace_root / "epochs" / epoch_id / "generations" / generation_id / "runs"
    if not runs_root.exists():
        return []
    return sorted(child.name for child in runs_root.iterdir() if child.is_dir())


def _load_field_tournaments(workspace_root: Path, epoch_id: str) -> list[dict[str, Any]]:
    """Read an epoch's durable field-tournament snapshots from disk.

    One ``field-*.json`` per round under the epoch's ``tournaments/``
    directory (written by the orchestrator at settle time). Each holds the
    settled field structure — round pairings, Copeland standings,
    competitors, proposing field-status — for one non-gauntlet round.
    Returns an empty list when the directory is absent (a pure-gauntlet
    epoch) or unreadable; an individual unparseable file is skipped.
    """
    from zicato.core.workspace import field_tournaments_dir  # noqa: PLC0415

    root = field_tournaments_dir(workspace_root, epoch_id)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_file() or child.suffix != ".json":
            continue
        try:
            record = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


# ---------------------------------------------------------------------------
# Per-run / per-experiment ingest (the building blocks)
# ---------------------------------------------------------------------------


def _ingest_run_into(
    conn: sqlite3.Connection,
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> bool:
    """Ingest one run's rows into an open connection.

    Returns ``True`` when a ``loss.json`` was found and ingested,
    ``False`` when the run directory has no loss profile yet (a run
    that started but whose reducer has not run).

    ``metric_counts`` is a pure projection of the run's ``loss.json``
    (via :meth:`LossProfile.unified_metrics`): the reducer owns the
    canonical metric surface, so the index never independently re-tallies
    the run's events JSONL. A loss profile written by an older reducer
    with an empty metric surface simply yields no metric_counts rows —
    correct-by-construction rather than reconstructed from a second
    source that could disagree with the file.
    """
    lpath = loss_profile_path(workspace_root, epoch_id, generation_id, entry_id)
    profile = _load_loss_profile(lpath)
    if profile is None:
        return False

    # Resolve the tournament round this run belongs to from the child
    # generation's experiment.json. Returns ``None`` for v0 seed runs
    # (no experiment) or runs whose experiment cannot be read; the
    # upsert tolerates either case (NULL column, idempotent re-ingest
    # via COALESCE).
    tournament_id = _tournament_id_for_run(workspace_root, epoch_id, generation_id)

    _upsert_run(
        conn,
        run_id=profile.run_id,
        epoch_id=epoch_id,
        generation_id=generation_id,
        entry_id=entry_id,
        # The run's wall-clock span, as the worker stamped it (issue #242).
        # Empty for a profile written before those fields existed and for a
        # synthesised worst-case that never measured one; runtime_ms remains
        # the authoritative DURATION either way.
        started_at=(profile.started_at or ""),
        ended_at=(profile.ended_at or ""),
        aborted=profile.wall_clock_budget_exceeded,
        runtime_ms=profile.runtime_ms,
        tournament_id=tournament_id,
        # Per-board-run tournament provenance (schema v4). The runner
        # stamped the matchup id onto the profile + loss.json; "" means
        # untagged (gauntlet / ad-hoc / legacy run) -> stored NULL.
        match_id=(profile.match_id or None),
    )
    _upsert_loss_profile(conn, profile, tournament_id=tournament_id)
    _replace_metric_counts(conn, profile.run_id, profile)
    _replace_judge_losses(conn, profile.run_id, profile)
    return True


def _ingest_experiment_into(
    conn: sqlite3.Connection,
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
) -> bool:
    """Ingest one experiment (+ patches + tournament + generation row).

    Returns ``True`` when an ``experiment.json`` was found and
    ingested, ``False`` when the generation has no experiment yet
    (``v0`` seed generations have no proposer experiment).

    The generation row's ``parent_generation_id`` and ``promoted`` flag
    are re-derived from the experiment itself: ``experiment.json`` is
    the canonical journal entry for a generation and carries both the
    parent it challenged (``parent_generation_id``) and the tournament
    verdict (``outcome.tournament_decision``). This is the
    source-of-truth the dashboard lineage walker uses (see
    ``zicato.query._champion_lineage``), so writing it from here keeps
    the index aligned with disk even when the live dual-write fires
    BEFORE ``lineage.json`` is updated (the orchestrator writes
    experiment.json first, then appends to lineage — so a
    lineage-only read at dual-write time is stale).
    """
    from zicato.epoch.journal import read_experiment  # noqa: PLC0415

    try:
        experiment = read_experiment(workspace_root, epoch_id, generation_id)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return False
    _upsert_experiment(conn, experiment)
    _upsert_tournament(conn, experiment)
    _upsert_generation_from_experiment(conn, experiment)
    return True


def _load_json_file(path: Path) -> Any | None:
    """Read and parse one JSON file; ``None`` on any defect (best-effort)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _scorecard_list(raw: Any) -> list[dict[str, Any]]:
    """Normalise a persisted scorecards artifact to a list of card dicts.

    Tolerates either the wrapped ``{"scorecards": [...]}`` form the CLI
    writes or a bare list, and skips any non-dict element.
    """
    if isinstance(raw, dict):
        raw = raw.get("scorecards")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict)]


def _finding_list(raw: Any) -> list[dict[str, Any]]:
    """Normalise a persisted findings artifact to a list of finding dicts."""
    if isinstance(raw, dict):
        raw = raw.get("findings")
    if not isinstance(raw, list):
        return []
    return [f for f in raw if isinstance(f, dict)]


def _ingest_reflection_into(
    conn: sqlite3.Connection,
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
) -> bool:
    """Ingest one reflection's projection rows into an open connection.

    Returns ``True`` when a ``plan.json`` was found and the ``reflections``
    (+ ``judge_scorecards``) rows were upserted, ``False`` when the reflection
    directory has no readable plan yet (nothing to project). The canonical
    files are the source of truth; the index rows are a pure projection of
    ``plan.json`` (identity + mode + executed), ``scorecards.json`` (per-judge
    cards + the corpus verdict tally), ``findings.json`` (finding count), and
    the derived ``summary.json`` (the consumed noise floor + decision-flip
    headline). Every artifact is read best-effort — a reflection with a plan
    but no scorecards (a ``--passive`` / ``--no-llm-adjudication`` run that
    produced no adjudications) still projects a row with an empty judge set.
    """
    from zicato.core.workspace import (  # noqa: PLC0415
        reflection_dir,
        reflection_findings_path,
        reflection_plan_path,
        reflection_scorecards_path,
    )

    plan = _load_json_file(reflection_plan_path(workspace_root, epoch_id, reflection_id))
    if not isinstance(plan, dict):
        return False

    scorecards = _scorecard_list(
        _load_json_file(reflection_scorecards_path(workspace_root, epoch_id, reflection_id))
    )
    findings = _finding_list(
        _load_json_file(reflection_findings_path(workspace_root, epoch_id, reflection_id))
    )
    summary = _load_json_file(
        reflection_dir(workspace_root, epoch_id, reflection_id) / "summary.json"
    )
    summary = summary if isinstance(summary, dict) else {}

    verdict_counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "ambiguous": 0}
    for card in scorecards:
        for key in verdict_counts:
            val = card.get(key)
            if isinstance(val, int) and not isinstance(val, bool):
                verdict_counts[key] += val

    _upsert_reflection(
        conn,
        reflection_id=str(plan.get("reflection_id") or reflection_id),
        epoch_id=str(plan.get("epoch_id") or epoch_id),
        created_at=str(plan.get("created_at") or ""),
        mode=str(plan.get("mode") or ""),
        executed=bool(plan.get("executed", False)),
        noise_floor_max_abs_delta=_opt_float_field(summary.get("noise_floor_max_abs_delta")),
        decision_flip_p=_opt_float_field(summary.get("decision_flip_p")),
        n_findings=len(findings),
        n_judges=len(scorecards),
        verdict_counts=verdict_counts,
    )
    _upsert_judge_scorecards(conn, str(plan.get("reflection_id") or reflection_id), scorecards)
    return True


def _ingest_pareto_frontier_into(
    conn: sqlite3.Connection,
    workspace_root: Path,
    epoch_id: str,
) -> bool:
    """Project one epoch's Pareto frontier record into ``pareto_frontier`` (v13).

    Returns ``True`` when a record existed and its rows were written. The
    canonical file is ``epochs/{epoch}/pareto_frontier.json``; this table is a
    pure projection of it, so an epoch with no record (every epoch that never
    admitted a candidate, and every workspace older than the feature) simply
    contributes no rows and is not an error.

    Delete-then-insert keyed on ``epoch_id`` rather than a per-row upsert: a
    generation can MOVE from ``member`` to ``retired``, and an upsert alone
    would leave a stale member row behind if the record ever shrank. The
    frontier is small (a handful of rows per epoch), so the rewrite is cheap
    and exactly reproduces the file.

    An UNREADABLE record — truncated JSON, a hand-edit, a ``format_version``
    from a newer build — skips the projection and leaves the epoch's existing
    rows alone. It must not raise: this function is called from BOTH the
    guarded incremental dual-write and the UNGUARDED full rebuild, and
    ``rebuild_index`` unlinks the database before repopulating it, so a raise
    here would leave a schema-only file with every other table empty. That
    turns one corrupt record into total index loss, repeatably, along the very
    path an operator runs to recover a bad index. Reading before the DELETE
    below is what makes "leave the existing rows alone" true.
    """
    from zicato.epoch._storage import RecordFormatError  # noqa: PLC0415
    from zicato.epoch.pareto import frontier_path, load_frontier  # noqa: PLC0415

    try:
        frontier = load_frontier(workspace_root, epoch_id)
    except (OSError, ValueError, json.JSONDecodeError, RecordFormatError) as exc:
        # Warned, not silent: unlike the derived rows around it, the file this
        # projects IS canonical, so a defect in it is the operator's to fix.
        log.warning(
            "index: pareto frontier projection skipped for epoch %s (%s): %s",
            epoch_id,
            frontier_path(workspace_root, epoch_id),
            exc,
        )
        return False
    if not frontier.members and not frontier.retired:
        conn.execute("DELETE FROM pareto_frontier WHERE epoch_id = ?", (epoch_id,))
        return False

    conn.execute("DELETE FROM pareto_frontier WHERE epoch_id = ?", (epoch_id,))
    rows: list[tuple[Any, ...]] = []
    for member in frontier.members:
        rows.append(
            (
                epoch_id,
                member.generation_id,
                "member",
                int(member.round_admitted),
                None,
                None,
                member.champion_generation_id,
                member.scalar,
                json.dumps(dict(member.axis_values), sort_keys=True),
                json.dumps(list(member.beats_champion_on)),
            )
        )
    for entry in frontier.retired:
        member = entry.member
        rows.append(
            (
                epoch_id,
                member.generation_id,
                "retired",
                int(member.round_admitted),
                int(entry.round_retired),
                entry.reason,
                member.champion_generation_id,
                member.scalar,
                json.dumps(dict(member.axis_values), sort_keys=True),
                json.dumps(list(member.beats_champion_on)),
            )
        )
    conn.executemany(
        "INSERT OR REPLACE INTO pareto_frontier("
        "epoch_id, generation_id, status, round_admitted, round_retired, "
        "retired_reason, champion_generation_id, scalar, axis_values_json, "
        "beats_champion_on_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return True


def _iter_reflection_dirs(workspace_root: Path, epoch_id: str) -> list[str]:
    """Yield reflection ids that have a directory on disk under ``epoch_id``."""
    from zicato.core.workspace import reflections_dir  # noqa: PLC0415

    root = reflections_dir(workspace_root, epoch_id)
    if not root.exists():
        return []
    return sorted(child.name for child in root.iterdir() if child.is_dir())


def _upsert_generation_from_experiment(
    conn: sqlite3.Connection,
    experiment: Experiment,
) -> None:
    """Refresh ``parent_generation_id`` + ``promoted`` from the experiment.

    The experiment's ``parent_generation_id`` is authoritative — the
    proposer attached it at hypothesis-emission time, and it survives
    the tournament unchanged. The ``promoted`` flag is true exactly
    when the resolved outcome's ``tournament_decision`` is
    ``"promoted"``; an unresolved experiment (outcome ``None``) is left
    as ``promoted=False`` since the verdict isn't in yet.

    Only the two columns the experiment owns authoritatively are
    written here — ``created_at`` is left to the lineage-driven
    :func:`_upsert_owning_epoch_generation`, which has the real
    creation timestamp. A targeted ``UPDATE`` (rather than the full
    upsert) ensures idempotency: re-running against the same
    experiment after lineage has caught up does not clobber the
    timestamp.

    Inserts a thin row when none exists yet (the orchestrator can call
    this before ``_upsert_owning_epoch_generation`` lands a row);
    falls back to the experiment's ``proposed_at`` for ``created_at``
    in that edge case.
    """
    promoted = (
        experiment.outcome is not None and experiment.outcome.tournament_decision == "promoted"
    )
    parent = experiment.parent_generation_id or None
    # First try a targeted UPDATE so we never touch created_at on an
    # existing row. SQLite's ``execute`` returns a cursor whose
    # ``rowcount`` we can inspect to see whether the row existed.
    cur = conn.execute(
        "UPDATE generations SET parent_generation_id = ?, promoted = ? "
        "WHERE epoch_id = ? AND generation_id = ?",
        (
            parent,
            1 if promoted else 0,
            experiment.epoch_id,
            experiment.generation_id,
        ),
    )
    if cur.rowcount > 0:
        return
    # No row yet — fall back to the full upsert so the row exists. The
    # lineage-driven upsert will overwrite the (fallback) created_at
    # on the next pass with the lineage value.
    _upsert_generation(
        conn,
        epoch_id=experiment.epoch_id,
        generation_id=experiment.generation_id,
        parent_generation_id=parent,
        promoted=promoted,
        created_at=experiment.proposed_at,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rebuild_index(workspace_root: Path, db_path: Path | None = None) -> Path:
    """Drop and rebuild the index database from the workspace files.

    Walks every epoch (from ``lineage.json`` + each epoch's
    ``config.json``), every generation under each epoch, and every run
    under each generation, re-deriving the full set of rows. This is
    the canonical "rebuild from files" path and backs ``zicato
    reindex``.

    The whole database is derived into a scratch file beside the target
    and renamed into place on success (:func:`_build_index_atomically`),
    so the result is a from-scratch rebuild — the index carries no state
    that is not in the files, so dropping the old one loses nothing — but
    a FAILED rebuild leaves the existing index byte-untouched instead of
    destroying it.

    Idempotent: running it twice produces an identical database.

    Parameters
    ----------
    workspace_root:
        The ``.zicato/`` directory to index.
    db_path:
        Where to write the database. Defaults to
        ``{workspace_root}/index.db`` — the location every consumer
        expects.

    Returns
    -------
    Path
        The path the index was written to.
    """
    target = db_path if db_path is not None else _default_db_path(workspace_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    _build_index_atomically(workspace_root, target)
    return target


def _fold_elo(conn: sqlite3.Connection) -> None:
    """Run the read-only Elo fold over the ingested match ledger.

    Best-effort: an unexpected failure in the analytics fold must not
    abort a rebuild whose canonical rows are already written. The fold is
    purely derived (the ``elo`` columns re-derive on the next reindex), so
    swallowing an error here loses nothing the files cannot reconstruct.
    """
    try:
        from zicato.index.elo import fold_elo_into_index  # noqa: PLC0415

        fold_elo_into_index(conn)
    except Exception:  # noqa: BLE001 — analytics fold is best-effort
        return


@dataclass(frozen=True, slots=True)
class _EpochWalkItem:
    """One epoch as the canonical rebuild sees it.

    ``config`` is the typed :class:`zicato.core.types.EpochConfig` when the
    epoch has a readable ``config.json``, and ``None`` for a thin epoch known
    only to ``lineage.json``. ``lineage_entry`` is that epoch's raw lineage
    dict, or ``None`` when the epoch has a directory but no lineage row yet.
    """

    epoch_id: str
    lineage_entry: dict[str, Any] | None
    config: Any | None


def _lineage_by_epoch(workspace_root: Path) -> dict[str, dict[str, Any]]:
    """Index ``lineage.json``'s epoch entries by id; ``{}`` when unreadable."""
    from zicato.epoch.lineage import load_lineage  # noqa: PLC0415

    try:
        lineage = load_lineage(workspace_root)
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in lineage.get("epochs", []):
        eid = entry.get("id")
        if isinstance(eid, str):
            out[eid] = entry
    return out


def _walk_epochs(workspace_root: Path) -> list[_EpochWalkItem]:
    """Enumerate every epoch, in the canonical rebuild's order.

    Epoch enumeration + ordering come from the canonical workspace-read
    layer (:func:`zicato.workspace.iter_epochs`), whose timestamp-first /
    numeric-aware authority is the single definition of epoch order — the
    same one the dashboard uses — so the index never disagrees with the
    rest of the system about which epoch precedes which. Each epoch's
    typed ``config.json`` is then read via
    :func:`zicato.epoch.lifecycle.load_epoch`; a directory whose config is
    missing / unreadable falls through to the lineage-only pass (it is a
    thin auto-created entry), preserving the exact set of epochs the prior
    ``list_epochs``-driven walk indexed.

    Shared by the full rebuild, :func:`validate_index`, and
    :func:`heal_index` so all three agree on what "the epochs" are — a heal
    that walked a different set than the rebuild could not converge with it.
    """
    from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415
    from zicato.workspace import WorkspaceLayout, iter_epochs  # noqa: PLC0415

    lineage_by_epoch = _lineage_by_epoch(workspace_root)
    layout = WorkspaceLayout.from_root(workspace_root)
    items: list[_EpochWalkItem] = []
    seen_epochs: set[str] = set()
    for epoch in iter_epochs(layout):
        try:
            cfg = load_epoch(workspace_root, epoch.id)
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
        seen_epochs.add(epoch.id)
        items.append(_EpochWalkItem(cfg.id, lineage_by_epoch.get(cfg.id), cfg))
    for eid, entry in lineage_by_epoch.items():
        if eid not in seen_epochs:
            items.append(_EpochWalkItem(eid, entry, None))
    return items


def _upsert_epoch_from_walk(conn: sqlite3.Connection, item: _EpochWalkItem) -> None:
    """Write the ``epochs`` row for one walked epoch.

    A config-bearing epoch takes every column from its typed
    ``config.json``. A thin epoch known only to ``lineage.json`` takes what
    the lineage entry carries and leaves ``goal`` empty — there is no
    ``config.json`` to read it from.
    """
    cfg = item.config
    if cfg is not None:
        _upsert_epoch(
            conn,
            epoch_id=cfg.id,
            contract_hash=cfg.contract_hash,
            created_at=cfg.created_at,
            closed=cfg.closed,
            goal=cfg.goal,
            parent_epoch_id=_parent_epoch_id_from_lineage_entry(item.lineage_entry),
        )
        return
    entry = item.lineage_entry or {}
    _upsert_epoch(
        conn,
        epoch_id=item.epoch_id,
        contract_hash="",
        created_at=str(entry.get("started_at", "")),
        closed=bool(entry.get("closed_at")),
        goal="",
        parent_epoch_id=_parent_epoch_id_from_lineage_entry(item.lineage_entry),
    )


def _rebuild_all(conn: sqlite3.Connection, workspace_root: Path) -> None:
    """Walk the whole workspace, populating every table.

    Generation lineage comes from :func:`zicato.epoch.lineage.load_lineage`
    (via :func:`_walk_epochs`). Generation directories and run directories
    are additionally walked so a generation / run whose telemetry landed
    before lineage was updated is still indexed.
    """
    for item in _walk_epochs(workspace_root):
        _upsert_epoch_from_walk(conn, item)
        _rebuild_epoch(conn, workspace_root, item.epoch_id, item.lineage_entry)


def _rebuild_epoch(
    conn: sqlite3.Connection,
    workspace_root: Path,
    epoch_id: str,
    lineage_entry: dict[str, Any] | None,
) -> None:
    """Populate generations / experiments / runs for one epoch."""
    # Generation metadata (parent + promoted) from lineage.json.
    gen_meta: dict[str, dict[str, Any]] = {}
    if lineage_entry is not None:
        for g in lineage_entry.get("generations", []):
            gid = g.get("id")
            if isinstance(gid, str):
                gen_meta[gid] = g

    # The set of generations to index is the union of those in lineage
    # and those with a directory on disk.
    generation_ids = set(gen_meta) | set(_iter_generation_dirs(workspace_root, epoch_id))
    for generation_id in sorted(generation_ids):
        meta = gen_meta.get(generation_id, {})
        _upsert_generation(
            conn,
            epoch_id=epoch_id,
            generation_id=generation_id,
            parent_generation_id=meta.get("parent_id"),
            promoted=bool(meta.get("promoted", False)),
            created_at=str(meta.get("created_at", "")),
            round_index=_round_index_from_lineage_gen(meta),
        )
        _ingest_experiment_into(conn, workspace_root, epoch_id, generation_id)
        for entry_id in _iter_run_entry_ids(workspace_root, epoch_id, generation_id):
            _ingest_run_into(conn, workspace_root, epoch_id, generation_id, entry_id)

    # Field-level tournament rows: re-derive each non-gauntlet round's
    # settled structure from its durable snapshot so the swiss / elim
    # ladder survives a full rebuild (the per-challenger experiment audit
    # cannot reconstruct the round pairings + standings on its own).
    for record in _load_field_tournaments(workspace_root, epoch_id):
        _upsert_field_tournament(conn, record)

    # Board-reflection projection (schema v11): re-derive each reflection's
    # bill-of-health + judge-scorecard rows from its canonical
    # ``plan.json`` / ``scorecards.json`` / ``findings.json`` / ``summary.json``
    # so the Instrument lens survives a full rebuild. Files are canonical; the
    # index is a projection, so a reflection is readable with no index at all.
    for reflection_id in _iter_reflection_dirs(workspace_root, epoch_id):
        _ingest_reflection_into(conn, workspace_root, epoch_id, reflection_id)

    # Pareto-frontier projection (schema v13): re-derive the epoch's frontier
    # members + retirements from its canonical ``pareto_frontier.json``. Files
    # are canonical; the table is a projection, so the record is fully readable
    # with no index at all (docs/design/PARETO-FRONTIER.md §7).
    _ingest_pareto_frontier_into(conn, workspace_root, epoch_id)

    # Ingest cursor (schema v14): record what the WORKSPACE looked like at the
    # moment this projection was taken, so a later ``validate_index`` can spot
    # divergence from four directory counts rather than re-deriving every row.
    # Written here — the one place both the full rebuild and the incremental
    # heal converge — so the two paths can never disagree about the cursor.
    _write_cursor(conn, workspace_root, epoch_id, lineage_entry)


# ---------------------------------------------------------------------------
# Ingest cursors — the per-epoch staleness signal (schema v14)
# ---------------------------------------------------------------------------

#: The five cheap signals a cursor records, in column order:
#: ``(experiments, runs, round_dirs, reflections, lineage_generations)``.
_CursorSignals = tuple[int, int, int, int, int]


def _count_dirs(root: Path) -> int:
    """Count child DIRECTORIES of ``root``; ``0`` when it is absent/unreadable."""
    try:
        return sum(1 for child in root.iterdir() if child.is_dir())
    except OSError:
        return 0


def _epoch_signals(
    workspace_root: Path,
    epoch_id: str,
    lineage_entry: dict[str, Any] | None,
) -> _CursorSignals:
    """Compute one epoch's cheap staleness signals from the workspace.

    Directory-entry counts and stats only — never a file parse. Validation
    runs at every ``evolve`` start on a workspace that may hold hundreds of
    generations, so it has to be affordable enough that nobody is tempted to
    turn it off; re-deriving row content to compare it would not be.

    ``runs_count`` counts ``loss.json`` files under
    ``generations/*/runs/*/``, and it is the signal that makes a CRASHED
    DUAL-WRITE visible. Everything else an epoch accumulates is bracketed by
    an experiment: if a round's runs reduced but the process died before
    :func:`ingest_run` projected them, no other count here moves, and until
    this signal existed such an epoch validated clean forever while the index
    silently held no rows for those runs. It is one ``iterdir`` per generation
    plus one ``is_file`` per run — the same order of cost as the experiment
    count directly above it, and no file is parsed.

    ``round_dirs_count`` is a signal the index has no table for — nothing
    projects ``epochs/{e}/rounds/``. It is carried anyway because it is the
    cheapest proxy for "this epoch advanced": a round directory appears at
    round start, before the experiment that will eventually land. Healing on
    it is idempotent, so an eager heal costs a walk and nothing else.
    """
    from zicato.core.workspace import (  # noqa: PLC0415
        experiment_json_path,
        generations_dir,
        loss_profile_path,
        reflections_dir,
    )
    from zicato.epoch.round_log import rounds_dir  # noqa: PLC0415

    experiments = 0
    runs = 0
    gens_root = generations_dir(workspace_root, epoch_id)
    try:
        gen_children = sorted(gens_root.iterdir())
    except OSError:
        gen_children = []
    for child in gen_children:
        if not child.is_dir():
            continue
        if experiment_json_path(workspace_root, epoch_id, child.name).is_file():
            experiments += 1
        for entry_id in _iter_run_entry_ids(workspace_root, epoch_id, child.name):
            if loss_profile_path(workspace_root, epoch_id, child.name, entry_id).is_file():
                runs += 1

    lineage_generations = 0
    for gen in (lineage_entry or {}).get("generations", []):
        if isinstance(gen, dict) and isinstance(gen.get("id"), str):
            lineage_generations += 1

    return (
        experiments,
        runs,
        _count_dirs(rounds_dir(workspace_root, epoch_id)),
        _count_dirs(reflections_dir(workspace_root, epoch_id)),
        lineage_generations,
    )


def _index_side_counts(conn: sqlite3.Connection, epoch_id: str) -> tuple[int, int]:
    """``(experiments, runs)`` as they exist IN THE INDEX for one epoch.

    Both are 1:1 with the workspace files the matching signal counts, which
    is what lets :func:`_diverged_epochs` compare them directly against
    :func:`_epoch_signals`.

    Runs are counted by DISTINCT ``(generation_id, entry_id)`` rather than by
    row, because that pair — not ``run_id`` — is what the workspace signal
    counts: a ``loss.json`` lives at exactly one
    ``generations/{gen}/runs/{entry}/`` path. ``runs`` is keyed by ``run_id``,
    so two profiles carrying the same id collapse to one row; counting rows
    would then under-report against a file count that cannot collide, and
    make the epoch diverge on a difference that is about the KEY rather than
    about the projection being incomplete.
    """
    experiments = conn.execute(
        "SELECT COUNT(*) FROM experiments WHERE epoch_id = ?", (epoch_id,)
    ).fetchone()[0]
    runs = conn.execute(
        "SELECT COUNT(DISTINCT generation_id || '/' || entry_id) FROM runs WHERE epoch_id = ?",
        (epoch_id,),
    ).fetchone()[0]
    return int(experiments or 0), int(runs or 0)


def _write_cursor(
    conn: sqlite3.Connection,
    workspace_root: Path,
    epoch_id: str,
    lineage_entry: dict[str, Any] | None,
) -> None:
    """Upsert one epoch's ``ingest_cursors`` row.

    **The two column families, and why they are not interchangeable.** A
    cursor exists to be compared against the workspace, so a column is only
    useful if it says something the workspace does not already say.

    * ``experiments_count`` / ``runs_count`` are stamped from the INDEX
      (:func:`_index_side_counts`) — *what this index actually holds*.
    * ``round_dirs_count`` / ``reflections_count`` /
      ``lineage_generations_count`` are stamped from the WORKSPACE, because
      the index has no 1:1 counterpart to count: nothing projects
      ``rounds/`` at all, a reflection DIRECTORY need not yield a row, and
      the ``generations`` table is the union of lineage ids and on-disk
      directories rather than the lineage list this signal counts. For these
      three the cursor means "what the workspace looked like when this epoch
      was last projected", and a change since then is the divergence.

    Stamping the first two from the workspace is what made a crashed
    dual-write invisible. :func:`_refresh_cursor` runs after every
    incremental ``ingest_*``, so a workspace-stamped ``runs_count`` recorded
    the loss profiles that were ON DISK — including any the crashed write
    never projected — and the epoch then validated clean forever against an
    index that did not hold them. A self-consistent lie, which is the one
    failure mode a staleness signal must not have. Index-stamped, the same
    epoch reports fewer rows than the workspace has files, every time it is
    asked, until a heal actually projects them.

    The honest cost of that choice: a canonical file the projection cannot
    read (:func:`_load_loss_profile` returns ``None`` on a malformed
    ``loss.json``) is counted by the workspace signal and yields no row, so
    the epoch stays divergent and is re-projected at every ``evolve`` start.
    Bounded, once-per-run, and correct in the sense that matters — the index
    genuinely cannot represent that file, and saying so repeatedly is better
    than recording that it can.
    """
    _experiments, _runs, round_dirs, reflections, lineage_generations = _epoch_signals(
        workspace_root, epoch_id, lineage_entry
    )
    indexed_experiments, indexed_runs = _index_side_counts(conn, epoch_id)
    conn.execute(
        "INSERT INTO ingest_cursors("
        "epoch_id, experiments_count, runs_count, round_dirs_count, reflections_count, "
        "lineage_generations_count, last_ingested_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(epoch_id) DO UPDATE SET "
        "experiments_count = excluded.experiments_count, "
        "runs_count = excluded.runs_count, "
        "round_dirs_count = excluded.round_dirs_count, "
        "reflections_count = excluded.reflections_count, "
        "lineage_generations_count = excluded.lineage_generations_count, "
        "last_ingested_at = excluded.last_ingested_at",
        (
            epoch_id,
            indexed_experiments,
            indexed_runs,
            round_dirs,
            reflections,
            lineage_generations,
            _now_iso(),
        ),
    )


def _refresh_cursor(conn: sqlite3.Connection, workspace_root: Path, epoch_id: str) -> None:
    """Dual-write companion: bring one epoch's cursor up to date.

    Called from every incremental ``ingest_*`` entry point so a live evolve's
    cursors track its writes. Without this the cursor would only ever be
    written by a rebuild or a heal, and every dual-written round would read as
    divergence at the next validation — turning the cheap incremental heal
    into a full re-projection of the active epoch on every ``evolve`` start.

    It records what the index NOW HOLDS, including the row the caller just
    wrote — never a fresh reading of the workspace for the index-backed
    columns. See :func:`_write_cursor` for why that distinction is the whole
    point of this function rather than a detail of it.
    """
    _write_cursor(conn, workspace_root, epoch_id, _lineage_by_epoch(workspace_root).get(epoch_id))


def _read_cursors(conn: sqlite3.Connection) -> dict[str, _CursorSignals]:
    """Read every persisted cursor; ``{}`` when the table is absent.

    An absent (or empty) table reads as "every epoch diverged", which is the
    correct conservative answer for a database written before v14: the first
    heal re-projects each epoch once and writes its cursor, and every heal
    after that is a no-op.
    """
    try:
        rows = conn.execute(
            "SELECT epoch_id, experiments_count, runs_count, round_dirs_count, "
            "reflections_count, lineage_generations_count FROM ingest_cursors"
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, _CursorSignals] = {}
    for row in rows:
        if isinstance(row[0], str):
            out[row[0]] = (
                int(row[1] or 0),
                int(row[2] or 0),
                int(row[3] or 0),
                int(row[4] or 0),
                int(row[5] or 0),
            )
    return out


def _indexed_epoch_ids(conn: sqlite3.Connection) -> set[str]:
    """Every epoch id the index holds a row for, cursor or not."""
    try:
        rows = conn.execute("SELECT DISTINCT epoch_id FROM epochs").fetchall()
    except sqlite3.Error:
        return set()
    return {row[0] for row in rows if isinstance(row[0], str)}


def _diverged_epochs(
    workspace_root: Path,
    cursors: dict[str, _CursorSignals],
    walk: list[_EpochWalkItem],
    indexed: set[str],
) -> tuple[str, ...]:
    """Return the sorted ids of epochs whose index rows no longer match disk.

    Three things count as divergence: an epoch on disk with no cursor row, an
    epoch whose cursor disagrees with any signal, and an epoch the INDEX still
    holds rows for that is GONE from the workspace.

    That last set is the union of the cursor table and ``indexed`` (the epoch
    ids actually present in ``epochs``) — not the cursor table alone. A
    cursor-driven test can only ever find epochs some cursor-writing path
    already visited, so rows written before v14 existed are invisible to it:
    a v13 database migrated IN PLACE by the incremental writers arrives with
    populated tables and ZERO cursors, and any of its epochs since deleted
    from the workspace would be orphaned in the index permanently, with
    ``heal_index`` reporting nothing to do. Reading the epoch ids straight
    off the index closes that, and costs one ``SELECT DISTINCT``.
    """
    stale: set[str] = set()
    on_disk: set[str] = set()
    for item in walk:
        on_disk.add(item.epoch_id)
        recorded = cursors.get(item.epoch_id)
        if recorded != _epoch_signals(workspace_root, item.epoch_id, item.lineage_entry):
            stale.add(item.epoch_id)
    stale.update((set(cursors) | set(indexed)) - on_disk)
    return tuple(sorted(stale))


#: Every epoch-scoped delete, in dependency order. The three tables that carry
#: no ``epoch_id`` of their own are reached through their lookup table, and
#: therefore must be deleted BEFORE that table's own rows are stripped — a
#: ``metric_counts`` delete that runs after the ``runs`` delete matches nothing
#: and silently orphans every metric row of the epoch.
_EPOCH_DELETE_STATEMENTS: tuple[str, ...] = (
    "DELETE FROM metric_counts WHERE run_id IN (SELECT run_id FROM runs WHERE epoch_id = ?)",
    "DELETE FROM judge_losses WHERE run_id IN (SELECT run_id FROM runs WHERE epoch_id = ?)",
    "DELETE FROM judge_scorecards WHERE reflection_id IN "
    "(SELECT reflection_id FROM reflections WHERE epoch_id = ?)",
    "DELETE FROM runs WHERE epoch_id = ?",
    "DELETE FROM loss_profiles WHERE epoch_id = ?",
    "DELETE FROM reflections WHERE epoch_id = ?",
    "DELETE FROM patches WHERE epoch_id = ?",
    "DELETE FROM experiments WHERE epoch_id = ?",
    "DELETE FROM tournaments WHERE epoch_id = ?",
    "DELETE FROM pareto_frontier WHERE epoch_id = ?",
    "DELETE FROM generations WHERE epoch_id = ?",
    "DELETE FROM epochs WHERE epoch_id = ?",
    "DELETE FROM ingest_cursors WHERE epoch_id = ?",
)


def _delete_epoch_rows(conn: sqlite3.Connection, epoch_id: str) -> None:
    """Remove every index row belonging to one epoch, across every table.

    The heal's unit of work. Deleting the ``epochs`` row too (rather than
    letting the re-projection's upsert overwrite it) is what makes heal and
    rebuild converge: ``_upsert_epoch`` preserves ``parent_epoch_id`` through
    ``COALESCE``, so an in-place upsert could keep a parent the workspace no
    longer claims while a from-scratch rebuild wrote NULL.
    """
    for statement in _EPOCH_DELETE_STATEMENTS:
        conn.execute(statement, (epoch_id,))


# ---------------------------------------------------------------------------
# Self-healing (docs/design/ANALYTICAL-INDEX.md §5)
# ---------------------------------------------------------------------------


def _unlink_db(path: Path) -> None:
    """Remove a SQLite file and its WAL / SHM sidecars, if present."""
    for suffix in ("", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _build_tmp_path(target: Path) -> Path:
    """A scratch path for ONE build, unique to this process and call.

    Unique rather than a fixed ``{target}.tmp`` because builders are NOT
    serialised against each other. Evolve's build runs under the workspace
    lock, but the dashboard's (``dashboard/server.py::_ensure_index_at_startup``)
    only SKIPS when it observes a held lock — two dashboards racing, or one
    whose lock read lost the window to a starting evolve, both build.

    A shared scratch path makes that race destructive rather than merely
    wasteful, because a build is not a moment: the second builder's
    :func:`_unlink_db` removes the inode the first is still writing into,
    the first's :func:`os.replace` then renames whatever now sits at the
    path — the second's HALF-BUILT database — onto the live index, and the
    sidecar unlink that follows deletes the WAL holding the rest of it.
    The observed result is a valid, EMPTY ``index.db`` (``user_version=0``,
    zero tables) installed by the self-healing path itself, with no
    exception raised anywhere. A partial build lands the worse shape: a
    correct ``user_version`` over missing rows, which :func:`_rebuild_reason`
    has no reason to rebuild.

    With a unique path the race costs duplicated work and nothing else:
    each builder derives a COMPLETE database into its own file and
    ``os.replace`` publishes one of them whole. Last writer wins, and every
    possible winner is valid.
    """
    return target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _sweep_stale_build_tmps(target: Path) -> None:
    """Remove build scratch abandoned by builders that are no longer alive.

    Unique scratch names (:func:`_build_tmp_path`) give up the one virtue of
    a fixed name: a build killed outright — SIGKILL, an OOM, a pulled plug —
    used to have its leftovers unlinked by the next build reusing the path.
    Now nothing reclaims them, and the leftovers are database-sized.

    Liveness is decided by the PID stamped into the name, the same
    stale-owner test the workspace lock uses (:func:`is_pid_alive`), so a
    CONCURRENT builder's scratch is never touched — the whole point of the
    unique name would be lost if the sweep could delete a live build. A name
    this cannot parse is left alone: unlinking unrecognised files next to the
    index is not this function's business.

    Best-effort throughout. Reclaiming disk must never be why a build fails.
    """
    from zicato.runtime.lock import is_pid_alive  # noqa: PLC0415

    # The pre-unique fixed name, from an index built before this seam
    # existed. Nothing writes there any more, so it is pure leftover.
    with contextlib.suppress(OSError):
        _unlink_db(target.with_name(target.name + ".tmp"))
    try:
        candidates = list(target.parent.glob(f"{target.name}.*.tmp"))
    except OSError:
        return
    for entry in candidates:
        stem = entry.name[len(target.name) + 1 : -len(".tmp")]
        pid_text = stem.split(".", 1)[0]
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if is_pid_alive(pid):
            continue
        with contextlib.suppress(OSError):
            _unlink_db(entry)


def _build_index_atomically(workspace_root: Path, target: Path) -> None:
    """Build the full index beside ``target``, then rename it into place.

    The single build path — :func:`rebuild_index` and :func:`ensure_index`
    both route through it. The whole database is derived into a private
    scratch file (:func:`_build_tmp_path`) and only then
    :func:`os.replace`\\ d onto ``target``, so a failure at any point during
    the build leaves the existing index byte-untouched.

    That ordering retires a defect class rather than a defect. The previous
    shape unlinked ``index.db`` FIRST and built in place, so any failure
    mid-build — an unreadable canonical record, a full disk, a Ctrl-C — left
    a schema-only file with every table empty, along the very path an
    operator runs to RECOVER a bad index. Here the worst case is that the
    old index survives unchanged and the caller sees the exception.

    The scratch path is per-build because concurrent builders are possible
    and are not serialised — see :func:`_build_tmp_path` for what a shared
    one does to them.

    The outgoing file's WAL / SHM sidecars are removed **before** the
    rename, and the order is the whole point. They describe the inode that
    is about to be swapped out, and a WAL is not merely "confusing" beside a
    different database — SQLite REPLAYS it. Its frames are validated by an
    internal checksum chain seeded from the WAL header's own salts, with no
    tie to the main file, so a complete foreign WAL is accepted and its
    pages — page 1 included, carrying ``user_version`` and the whole schema
    — are recovered over the file that was just published. Removing the
    sidecars afterwards leaves a window in which exactly that is on disk: a
    crash inside it (or a reader opening the pair, which may then CHECKPOINT
    the foreign frames into the new file and make it permanent) resurrects
    the OLD index in place of the new one, ``PRAGMA integrity_check`` says
    ``ok``, and nothing anywhere raises. Clearing them first means the new
    inode never coexists with a sidecar that is not its own.

    (The SCRATCH file needs no such care — closing the last connection
    checkpoints its WAL back into the main file and removes the sidecars,
    which is what makes the renamed file whole.)
    """
    _sweep_stale_build_tmps(target)
    tmp = _build_tmp_path(target)
    _unlink_db(tmp)
    try:
        conn = sqlite3.connect(str(tmp))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Same short lock wait every other index connection sets: a build
            # racing a live reader (dashboard / supervisor) should queue
            # behind the lock briefly rather than fail immediately with
            # "database is locked".
            conn.execute("PRAGMA busy_timeout=5000")
            apply_schema(conn)
            _rebuild_all(conn, workspace_root)
            # The read-only Elo analytics fold runs AFTER every tournament has
            # been ingested (it reads the full match ledger off the
            # ``tournaments`` rows). It only ever writes the additive
            # ``generations.elo`` / ``generations.elo_games`` columns — Elo is
            # for visibility, never for the gate — and never touches a
            # decision/loss. A best-effort guard keeps a fold failure from
            # aborting an otherwise-complete build.
            _fold_elo(conn)
            conn.commit()
        finally:
            conn.close()
    except BaseException:
        # Leave no half-built scratch file behind for the next build to
        # inherit. The real index is untouched either way.
        _unlink_db(tmp)
        raise
    # The clean close above checkpointed the scratch WAL back into the scratch
    # file and removed its sidecars; that is precisely what makes the renamed
    # file whole. If they are somehow still here, the rename would publish a
    # database whose tail is in a file we are about to orphan — refuse, and
    # leave the existing index alone, which is this function's entire promise.
    orphans = [
        sidecar
        for sidecar in (tmp.with_name(tmp.name + suffix) for suffix in ("-wal", "-shm"))
        if sidecar.exists()
    ]
    if orphans:
        _unlink_db(tmp)
        raise RuntimeError(
            "refusing to publish an index whose scratch file still has sidecars "
            f"({', '.join(p.name for p in orphans)}); its contents are not all in the "
            "file being renamed"
        )
    # BEFORE the rename — see the docstring. A foreign WAL left beside the
    # published file is replayed over it, not ignored.
    for suffix in ("-wal", "-shm"):
        target.with_name(target.name + suffix).unlink(missing_ok=True)
    os.replace(tmp, target)


def _rebuild_reason(target: Path) -> str | None:
    """Why ``target`` needs a full build, or ``None`` when it does not.

    One of ``"absent"``, ``"stale-schema"``, ``"unreadable"``. An
    EQUAL-version database is never a reason: detecting that its *contents*
    drifted from the workspace is :func:`heal_index`'s job, not this one's.

    Raises :class:`~zicato.index.schema.IndexSchemaNewerError` for a database
    written by a NEWER build. Auto-deleting it is forbidden — its columns and
    semantics are unknown here, so the recovery belongs to the operator.
    """
    if not target.exists():
        return "absent"
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    except sqlite3.Error:
        return "unreadable"
    try:
        version = read_schema_version(conn)
    except sqlite3.DatabaseError:
        # Not a SQLite database at all (truncated, or some other file that
        # ended up at this path). A rebuild is the recovery.
        return "unreadable"
    finally:
        conn.close()
    raise_if_newer(version)
    return "stale-schema" if version < SCHEMA_VERSION else None


def _record_action(action_out: list[str] | None, action: str) -> None:
    if action_out is not None:
        action_out.append(action)


def ensure_index(
    workspace_root: Path,
    db_path: Path | None = None,
    *,
    action_out: list[str] | None = None,
) -> Path:
    """Guarantee an index of the CURRENT schema exists, building it if not.

    The structural half of the self-healing index (M1;
    ``docs/design/ANALYTICAL-INDEX.md`` §5.1). On return ``index.db`` exists
    and carries :data:`~zicato.index.schema.SCHEMA_VERSION`. It builds when —
    and only when — the file is absent, stamped with an OLDER version, or not
    a readable SQLite database. An equal-version database is left alone:
    content drift is :func:`heal_index`'s business.

    An older-version file is REBUILT rather than migrated in place because
    whole-table additions need a backfill that ``ALTER TABLE`` cannot
    provide. The v11 reflection tables and the v13 ``pareto_frontier`` table
    both land empty on an in-place open and stay empty until something walks
    the files again; a rebuild is the only shape that fills them.

    Parameters
    ----------
    workspace_root:
        The ``.zicato/`` directory to index.
    db_path:
        Where the index lives. Defaults to ``{workspace_root}/index.db``.
    action_out:
        Optional caller-supplied list this appends exactly one symbolic
        action to, so a caller can report what happened without re-deriving
        it: ``"present"``, ``"built:absent"``, ``"built:stale-schema"``, or
        ``"built:unreadable"``.

    Returns
    -------
    Path
        The path the index lives at.

    Raises
    ------
    zicato.index.schema.IndexSchemaNewerError
        When the existing database was written by a newer zicato.
    """
    target = db_path if db_path is not None else _default_db_path(workspace_root)
    reason = _rebuild_reason(target)
    if reason is None:
        _record_action(action_out, "present")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    _build_index_atomically(workspace_root, target)
    _record_action(action_out, f"built:{reason}")
    return target


def validate_index(workspace_root: Path, db_path: Path | None = None) -> tuple[str, ...]:
    """Return the sorted ids of epochs whose index rows diverged from disk.

    Read-only, and cheap by construction: every epoch's persisted cursor
    (schema v14) is compared against four directory-entry counts, never
    against re-derived row content. An empty result means the index agrees
    with the workspace at cursor granularity.

    A missing database yields ``()`` — there is nothing to validate, and
    building one is :func:`ensure_index`'s job. A database predating v14 has
    no cursors, so every epoch reads as diverged; the first
    :func:`heal_index` re-projects each once and every pass after is a no-op.
    """
    target = db_path if db_path is not None else _default_db_path(workspace_root)
    if not target.exists():
        return ()
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    except sqlite3.Error:
        return ()
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        cursors = _read_cursors(conn)
        indexed = _indexed_epoch_ids(conn)
    except sqlite3.DatabaseError:
        return ()
    finally:
        conn.close()
    return _diverged_epochs(workspace_root, cursors, _walk_epochs(workspace_root), indexed)


def heal_index(workspace_root: Path, db_path: Path | None = None) -> tuple[str, ...]:
    """Re-ingest ONLY the epochs whose index rows diverged from the workspace.

    The incremental half of the self-healing index (M2;
    ``docs/design/ANALYTICAL-INDEX.md`` §5.2). Each diverged epoch has every
    one of its rows deleted and is then re-projected through the same
    :func:`_rebuild_epoch` machinery the full rebuild uses. An epoch that is
    in the index but gone from the workspace is deleted and not re-projected.

    The Elo fold is re-run over the WHOLE database afterwards: the
    ``generations.elo*`` columns are a cross-epoch analytics fold, so
    deleting and re-inserting one epoch's generations nulls them, and only a
    whole-ledger re-fold restores what a from-scratch rebuild would have
    produced. Skipping it is how a heal would quietly fail to converge.

    Returns the ids it healed (``()`` when nothing diverged). Idempotent: a
    second call immediately after the first finds nothing to do.
    """
    conn, _ = _open_for_write(workspace_root, db_path)
    try:
        walk = _walk_epochs(workspace_root)
        stale = _diverged_epochs(
            workspace_root, _read_cursors(conn), walk, _indexed_epoch_ids(conn)
        )
        if not stale:
            return ()
        by_id = {item.epoch_id: item for item in walk}
        for epoch_id in stale:
            _delete_epoch_rows(conn, epoch_id)
            item = by_id.get(epoch_id)
            if item is None:
                # Gone from the workspace: the rows are removed, and there is
                # nothing left on disk to re-project them from.
                continue
            _upsert_epoch_from_walk(conn, item)
            _rebuild_epoch(conn, workspace_root, epoch_id, item.lineage_entry)
        _fold_elo(conn)
        conn.commit()
    finally:
        conn.close()
    return stale


def _open_for_write(workspace_root: Path, db_path: Path | None) -> tuple[sqlite3.Connection, Path]:
    """Open (creating + schema-applying if needed) the index for a write.

    Used by the incremental ingest paths. When the database does not
    exist yet it is created and the schema applied — so the first
    live dual-write from the orchestrator does not require a prior
    ``rebuild_index``. The connection is in WAL mode for concurrent
    reads.
    """
    target = db_path if db_path is not None else _default_db_path(workspace_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    fresh = not target.exists()
    conn = sqlite3.connect(str(target))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if fresh:
        apply_schema(conn)
    else:
        # Cheap idempotent re-apply guards against an index file that
        # exists but predates a table (e.g. a partially-built database).
        apply_schema(conn)
    return conn, target


def ingest_run(
    workspace_root: Path,
    db_path: Path | None,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> None:
    """Incrementally upsert one run's index rows.

    Reads the run's ``loss.json`` and upserts the ``runs``,
    ``loss_profiles``, and ``metric_counts`` rows for it. ``metric_counts``
    is a pure projection of the loss profile's metric surface — the index
    never independently re-tallies the run's events JSONL.

    This is the live-dual-write entry point: the orchestrator calls it
    the moment a run's loss profile lands (R9-4), so the index tracks
    in-progress epochs without waiting for a full ``zicato repair index``.

    Idempotent — calling it twice for the same run produces the same
    rows (every write is a keyed upsert; ``metric_counts`` is
    delete-then-insert keyed on ``run_id``).

    The owning epoch / generation rows are also upserted (best-effort,
    from ``config.json`` / ``lineage.json``) so a freshly-indexed run
    is never an orphan. When the database does not exist yet it is
    created with the schema applied.

    A run whose ``loss.json`` has not been written yet is silently
    skipped — the orchestrator may call this slightly early.
    """
    conn, _ = _open_for_write(workspace_root, db_path)
    try:
        _upsert_owning_epoch_generation(conn, workspace_root, epoch_id, generation_id)
        _ingest_run_into(conn, workspace_root, epoch_id, generation_id, entry_id)
        _refresh_cursor(conn, workspace_root, epoch_id)
        conn.commit()
    finally:
        conn.close()


def ingest_experiment(
    workspace_root: Path,
    db_path: Path | None,
    epoch_id: str,
    generation_id: str,
) -> None:
    """Incrementally upsert one experiment, its patches, and its tournament.

    Reads the generation's ``experiment.json`` (+ per-patch files) via
    :func:`zicato.epoch.journal.read_experiment` and upserts the
    ``experiments`` and ``patches`` rows. When the experiment has a
    resolved :class:`zicato.core.types.OutcomeRecord` a ``tournaments``
    row is upserted too; an unresolved experiment writes no tournament
    row (it gets one on the next ingest after the tournament runs).

    Idempotent — every write is a keyed upsert.

    The owning epoch / generation rows are upserted first so the
    experiment is never an orphan. When the database does not exist
    yet it is created with the schema applied. A generation with no
    ``experiment.json`` (a ``v0`` seed) is silently skipped.
    """
    conn, _ = _open_for_write(workspace_root, db_path)
    try:
        _upsert_owning_epoch_generation(conn, workspace_root, epoch_id, generation_id)
        _ingest_experiment_into(conn, workspace_root, epoch_id, generation_id)
        _refresh_cursor(conn, workspace_root, epoch_id)
        conn.commit()
    finally:
        conn.close()


def ingest_field_tournament(
    workspace_root: Path,
    db_path: Path | None,
    record: dict[str, Any],
) -> None:
    """Incrementally upsert one round's FIELD-level ``tournaments`` row.

    ``record`` is the settled field structure the orchestrator wrote to
    the round's durable ``field-*.json`` snapshot — the same shape the
    runtime ``active_tournament`` envelope carries (``tournament_id`` /
    ``epoch_id`` / ``structure`` / ``competitors`` / ``rounds`` /
    ``standings`` / ``field_status`` / the crowning verdict). This is the
    live dual-write companion to :func:`ingest_experiment`: the
    orchestrator calls it at settle time so the swiss / elim ladder is in
    the index immediately, without waiting for a full ``zicato repair index``.

    A no-op for a degenerate two-competitor (gauntlet) field. Idempotent —
    the write is a keyed upsert on the field-level ``tournament_id``. When
    the database does not exist yet it is created with the schema applied.
    """
    conn, _ = _open_for_write(workspace_root, db_path)
    try:
        _upsert_field_tournament(conn, record)
        epoch_id = record.get("epoch_id")
        if isinstance(epoch_id, str) and epoch_id:
            _refresh_cursor(conn, workspace_root, epoch_id)
        conn.commit()
    finally:
        conn.close()


def ingest_reflection(
    workspace_root: Path,
    db_path: Path | None,
    epoch_id: str,
    reflection_id: str,
) -> None:
    """Incrementally upsert one reflection's ``reflections`` + scorecard rows.

    The live dual-write companion to :func:`ingest_experiment` for the
    board-reflection projection: ``zicato inspect reflection run`` calls it at finalize
    (the moment ``findings.json`` lands) so the Instrument lens sees the new
    reflection immediately, without a full ``zicato repair index``. Reads the
    reflection's canonical files (``plan.json`` / ``scorecards.json`` /
    ``findings.json`` / ``summary.json``) and projects them; a reflection with
    no readable ``plan.json`` is silently skipped. Idempotent — every write is
    a keyed upsert (scorecards are delete-then-insert keyed on
    ``reflection_id``). When the database does not exist yet it is created with
    the schema applied.
    """
    conn, _ = _open_for_write(workspace_root, db_path)
    try:
        _ingest_reflection_into(conn, workspace_root, epoch_id, reflection_id)
        _refresh_cursor(conn, workspace_root, epoch_id)
        conn.commit()
    finally:
        conn.close()


def ingest_pareto_frontier(
    workspace_root: Path,
    db_path: Path | None,
    epoch_id: str,
) -> None:
    """Incrementally re-project one epoch's Pareto frontier record.

    The live dual-write companion for the frontier: the evolve loop calls it
    at settle, the moment ``pareto_frontier.json`` changes, so a live index
    does not go stale between rebuilds. Idempotent — the epoch's rows are
    rewritten wholesale from the file. When the database does not exist yet it
    is created with the schema applied.
    """
    conn, _ = _open_for_write(workspace_root, db_path)
    try:
        _ingest_pareto_frontier_into(conn, workspace_root, epoch_id)
        _refresh_cursor(conn, workspace_root, epoch_id)
        conn.commit()
    finally:
        conn.close()


def _upsert_owning_epoch_generation(
    conn: sqlite3.Connection,
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
) -> None:
    """Best-effort upsert of the epoch + generation a run/experiment belongs to.

    Keeps an incrementally-ingested run or experiment from being an
    orphan row. Reads ``config.json`` for the epoch and ``lineage.json``
    for the generation's parent / promoted flags; tolerates either
    being absent (a thin row with empty metadata is still written so a
    later ``rebuild_index`` or ``ingest_*`` call fills it in).
    """
    contract_hash: str | None = None
    created_at = ""
    closed = False
    goal = ""
    try:
        from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415

        cfg = load_epoch(workspace_root, epoch_id)
        contract_hash = cfg.contract_hash
        created_at = cfg.created_at
        closed = cfg.closed
        goal = cfg.goal
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    # Resolve the parent epoch id from lineage.json's v0_parent field
    # for the matching entry. Tolerates a missing / unreadable lineage
    # — the upsert preserves any existing parent_epoch_id via COALESCE.
    parent_epoch_id: str | None = None
    parent_id: str | None = None
    promoted = False
    gen_created_at = ""
    round_index: int | None = None
    try:
        from zicato.epoch.lineage import load_lineage  # noqa: PLC0415

        lineage = load_lineage(workspace_root)
        for entry in lineage.get("epochs", []):
            if entry.get("id") != epoch_id:
                continue
            parent_epoch_id = _parent_epoch_id_from_lineage_entry(entry)
            for g in entry.get("generations", []):
                if g.get("id") == generation_id:
                    parent_id = g.get("parent_id")
                    promoted = bool(g.get("promoted", False))
                    gen_created_at = str(g.get("created_at", ""))
                    round_index = _round_index_from_lineage_gen(g)
            break
    except (OSError, json.JSONDecodeError):
        pass
    _upsert_epoch(
        conn,
        epoch_id,
        contract_hash,
        created_at,
        closed,
        goal=goal,
        parent_epoch_id=parent_epoch_id,
    )
    _upsert_generation(
        conn,
        epoch_id=epoch_id,
        generation_id=generation_id,
        parent_generation_id=parent_id,
        promoted=promoted,
        created_at=gen_created_at,
        round_index=round_index,
    )


# ---------------------------------------------------------------------------
# Backfill helper
# ---------------------------------------------------------------------------


def backfill_generations(
    workspace_root: Path,
    db_path: Path | None = None,
) -> dict[str, int]:
    """Reconcile the ``generations`` table against the on-disk source-of-truth.

    Targeted repair for workspaces whose ``generations`` rows were
    written by a buggy dual-write path (parent NULL, promoted clamped
    to 0). Walks every epoch in ``lineage.json`` and every per-
    generation ``experiment.json`` and rewrites each row's
    ``parent_generation_id`` and ``promoted`` flag from those canonical
    sources — same precedence the dashboard's lineage walker uses
    (experiment.json wins where it disagrees with lineage.json).

    Read-only against the disk files; the database is the only thing
    mutated. Idempotent: running it twice produces the same rows.

    The fix at the writer (``_ingest_experiment_into`` now refreshes
    the generation row whenever an experiment is ingested) keeps new
    workspaces from needing this. The backfill exists to repair the
    historical workspaces written before the fix.

    Parameters
    ----------
    workspace_root:
        The ``.zicato/`` directory to reconcile.
    db_path:
        Where the index lives. Defaults to ``{workspace_root}/index.db``.

    Returns
    -------
    dict
        ``{"updated": N, "scanned": M}`` — how many rows were rewritten
        and how many generations were scanned. ``M - N`` is the number
        already correct (or seed rows whose lineage row already
        matches disk).
    """
    target = db_path if db_path is not None else _default_db_path(workspace_root)
    if not target.exists():
        return {"updated": 0, "scanned": 0}

    from zicato.epoch.journal import read_experiment  # noqa: PLC0415
    from zicato.epoch.lineage import load_lineage  # noqa: PLC0415

    scanned = 0
    updated = 0
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        lineage = load_lineage(workspace_root)
        for entry in lineage.get("epochs", []):
            epoch_id = entry.get("id")
            if not isinstance(epoch_id, str):
                continue
            for g in entry.get("generations", []):
                gid = g.get("id")
                if not isinstance(gid, str):
                    continue
                scanned += 1
                # Prefer experiment.json (authoritative for non-seed
                # generations); fall back to lineage's own fields for
                # the seed (which has no experiment).
                parent: str | None
                promoted: bool
                try:
                    exp = read_experiment(workspace_root, epoch_id, gid)
                    parent = exp.parent_generation_id or None
                    promoted = (
                        exp.outcome is not None and exp.outcome.tournament_decision == "promoted"
                    )
                except (FileNotFoundError, json.JSONDecodeError, KeyError):
                    raw_parent = g.get("parent_id")
                    parent = raw_parent if isinstance(raw_parent, str) else None
                    promoted = bool(g.get("promoted", False))

                # ``round_index`` is owned by lineage.json (the birth
                # round); reconcile it too so a legacy row gains it once
                # lineage carries it. An absent value reads as None.
                round_index = _round_index_from_lineage_gen(g)

                # Read what the DB currently has so we only count a real
                # rewrite, not a no-op upsert.
                cur = conn.execute(
                    "SELECT parent_generation_id, promoted, created_at, round_index "
                    "FROM generations WHERE epoch_id = ? AND generation_id = ?",
                    (epoch_id, gid),
                )
                row = cur.fetchone()
                created_at = ""
                cur_parent: str | None = None
                cur_promoted = 0
                cur_round_index: int | None = None
                if row is not None:
                    cur_parent = row[0] if row[0] is not None else None
                    cur_promoted = int(row[1] or 0)
                    created_at = row[2] if row[2] else ""
                    cur_round_index = row[3] if row[3] is not None else None
                # Prefer the existing created_at; fall back to lineage's,
                # then to the empty string (matches the live writer's
                # behaviour when timestamps are unavailable).
                if not created_at:
                    created_at = str(g.get("created_at", ""))

                # The upsert writes round_index via COALESCE, so it never
                # nulls an existing value; a backfill is needed only when
                # lineage supplies a value the DB row is missing.
                round_index_needs_write = round_index is not None and cur_round_index is None
                if (
                    row is not None
                    and cur_parent == parent
                    and cur_promoted == (1 if promoted else 0)
                    and not round_index_needs_write
                ):
                    continue
                _upsert_generation(
                    conn,
                    epoch_id=epoch_id,
                    generation_id=gid,
                    parent_generation_id=parent,
                    promoted=promoted,
                    created_at=created_at,
                    round_index=round_index,
                )
                updated += 1
        conn.commit()
    finally:
        conn.close()
    return {"updated": updated, "scanned": scanned}


def repair_epoch_goals(
    workspace_root: Path,
    db_path: Path | None = None,
) -> dict[str, int]:
    """Walk every epoch on disk and reconcile the ``goal`` field.

    Targeted repair for workspaces whose per-epoch ``config.json``
    predates the ``goal`` field (or whose row in the ``epochs`` index
    table was written before the column existed). For every epoch we
    can read off disk, this:

    1. Ensures the ``config.json`` carries a ``goal`` key (added with
       an empty string if missing); the rewrite goes through the
       canonical :func:`zicato.epoch.lifecycle._write_config` so all
       other keys are preserved.
    2. Re-upserts the ``epochs`` row so the index column matches the
       on-disk value.

    Idempotent — running it twice writes the same bytes. Read-only on
    epoch ids: epochs that exist only in ``lineage.json`` (no
    ``config.json`` on disk) are left untouched.

    Parameters
    ----------
    workspace_root:
        The ``.zicato/`` directory to repair.
    db_path:
        Where the index lives. Defaults to ``{workspace_root}/index.db``.

    Returns
    -------
    dict
        ``{"scanned": M, "config_patched": A, "index_updated": B}`` —
        ``M`` epochs walked, ``A`` config files that needed the goal
        key added, ``B`` index rows refreshed. The two counters are
        independent; an epoch can need a config patch but not an
        index refresh (and vice versa).
    """
    from zicato.epoch._storage import (  # noqa: PLC0415
        backend_for,
        epoch_config_key,
    )
    from zicato.epoch.lifecycle import (  # noqa: PLC0415
        _write_config,
        list_epochs,
    )

    scanned = 0
    config_patched = 0
    index_updated = 0
    backend = backend_for(workspace_root)

    # 1. Walk every epoch on disk and patch its config.json.
    for cfg in list_epochs(workspace_root):
        scanned += 1
        # Read the raw JSON so we can tell whether the goal key was
        # actually present (vs. defaulted-to-empty by the loader).
        raw = backend.read_json(epoch_config_key(cfg.id))
        if not isinstance(raw, dict):
            continue
        if "goal" not in raw:
            # The loader already defaulted cfg.goal to "" — round-trip
            # back through _write_config to land the key on disk.
            _write_config(workspace_root, cfg)
            config_patched += 1

    # 2. Refresh the index epochs.goal column.
    target = db_path if db_path is not None else _default_db_path(workspace_root)
    if target.exists():
        conn = sqlite3.connect(str(target))
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            apply_schema(conn)
            for cfg in list_epochs(workspace_root):
                cur = conn.execute(
                    "SELECT goal FROM epochs WHERE epoch_id = ?",
                    (cfg.id,),
                )
                row = cur.fetchone()
                existing_goal: str | None = row[0] if row is not None else None
                if existing_goal == cfg.goal:
                    continue
                _upsert_epoch(
                    conn,
                    epoch_id=cfg.id,
                    contract_hash=cfg.contract_hash,
                    created_at=cfg.created_at,
                    closed=cfg.closed,
                    goal=cfg.goal,
                )
                index_updated += 1
            conn.commit()
        finally:
            conn.close()

    return {
        "scanned": scanned,
        "config_patched": config_patched,
        "index_updated": index_updated,
    }


def backfill_tournament_fk(
    workspace_root: Path,
    db_path: Path | None = None,
) -> dict[str, int]:
    """Backfill ``tournament_id`` on existing ``runs`` and ``loss_profiles``.

    Walks every generation in ``lineage.json`` whose ``experiment.json``
    is on disk, computes the canonical
    ``"{epoch_id}:{parent_gen}->{child_gen}"`` tournament id, and
    rewrites the ``runs.tournament_id`` and ``loss_profiles.tournament_id``
    columns for every row under that generation that does not already
    have one.

    Also backfills ``epochs.parent_epoch_id`` from each lineage entry's
    ``v0_parent`` — folded into the same command so an operator with an
    older index gets both v2 columns repaired in one pass.

    Idempotent: only rewrites cells that are currently ``NULL`` or
    disagree with the disk-derived value. A fresh index built by the
    v2 ingest path is already populated correctly, so this command is
    a no-op there.

    Returns
    -------
    dict
        ``{"runs_updated": A, "loss_updated": B, "epochs_updated": C,
        "scanned": M}`` — A, B, C count cells actually rewritten, M is
        the number of generation rows the walk visited.
    """
    target = db_path if db_path is not None else _default_db_path(workspace_root)
    if not target.exists():
        return {
            "runs_updated": 0,
            "loss_updated": 0,
            "epochs_updated": 0,
            "scanned": 0,
        }

    from zicato.epoch.journal import read_experiment  # noqa: PLC0415
    from zicato.epoch.lineage import load_lineage  # noqa: PLC0415

    runs_updated = 0
    loss_updated = 0
    epochs_updated = 0
    scanned = 0
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        # Make sure the v2 columns exist before we try to write them —
        # an older index file opened here might still be v1. apply_schema
        # is idempotent + carries the v1 -> v2 migration.
        apply_schema(conn)

        lineage = load_lineage(workspace_root)
        for entry in lineage.get("epochs", []):
            epoch_id = entry.get("id")
            if not isinstance(epoch_id, str):
                continue

            # epochs.parent_epoch_id from this lineage entry's v0_parent.
            parent_epoch_id = _parent_epoch_id_from_lineage_entry(entry)
            cur = conn.execute(
                "SELECT parent_epoch_id FROM epochs WHERE epoch_id = ?",
                (epoch_id,),
            )
            row = cur.fetchone()
            if row is not None and row[0] != parent_epoch_id:
                conn.execute(
                    "UPDATE epochs SET parent_epoch_id = ? WHERE epoch_id = ?",
                    (parent_epoch_id, epoch_id),
                )
                epochs_updated += 1

            for g in entry.get("generations", []):
                gid = g.get("id")
                if not isinstance(gid, str):
                    continue
                scanned += 1
                try:
                    experiment = read_experiment(workspace_root, epoch_id, gid)
                except (FileNotFoundError, json.JSONDecodeError, KeyError):
                    continue
                parent = experiment.parent_generation_id
                if not parent:
                    continue
                tournament_id = f"{epoch_id}:{parent}->{gid}"

                # runs.tournament_id — only rewrite cells where the
                # current value disagrees (NULL or a stale id).
                cur = conn.execute(
                    "UPDATE runs SET tournament_id = ? "
                    "WHERE epoch_id = ? AND generation_id = ? "
                    "AND (tournament_id IS NULL OR tournament_id != ?)",
                    (tournament_id, epoch_id, gid, tournament_id),
                )
                runs_updated += cur.rowcount if cur.rowcount > 0 else 0

                cur = conn.execute(
                    "UPDATE loss_profiles SET tournament_id = ? "
                    "WHERE epoch_id = ? AND generation_id = ? "
                    "AND (tournament_id IS NULL OR tournament_id != ?)",
                    (tournament_id, epoch_id, gid, tournament_id),
                )
                loss_updated += cur.rowcount if cur.rowcount > 0 else 0
        conn.commit()
    finally:
        conn.close()
    return {
        "runs_updated": runs_updated,
        "loss_updated": loss_updated,
        "epochs_updated": epochs_updated,
        "scanned": scanned,
    }


__all__ = [
    "rebuild_index",
    "ensure_index",
    "validate_index",
    "heal_index",
    "ingest_run",
    "ingest_experiment",
    "ingest_field_tournament",
    "ingest_reflection",
    "ingest_pareto_frontier",
    "backfill_generations",
    "repair_epoch_goals",
    "backfill_tournament_fk",
]
