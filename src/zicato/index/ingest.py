"""Build and incrementally update the zicato analytical index.

Three public entry points:

* :func:`rebuild_index` — the canonical "rebuild from files" path.
  Drops ``index.db`` and walks every epoch / generation / run under
  ``.zicato/``, re-deriving every row from the workspace files. Backs
  ``zicato reindex``.
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

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zicato.core.types import Experiment, LossProfile
from zicato.core.workspace import loss_profile_path
from zicato.index.schema import apply_schema


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
        # The workspace does not persist a free-standing per-run record
        # with wall-clock timestamps — loss.json carries only the
        # duration. started_at / ended_at are left empty; runtime_ms is
        # the authoritative timing field.
        started_at="",
        ended_at="",
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
    ``state_reader._champion_lineage``), so writing it from here keeps
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

    The database file (and its WAL / SHM sidecars) is deleted first so
    the rebuild starts from an empty schema — the index carries no
    state that is not in the files, so dropping it loses nothing.

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

    # Drop the database + WAL/SHM sidecars so the rebuild is from scratch.
    for suffix in ("", "-wal", "-shm"):
        sidecar = target.with_name(target.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    conn = sqlite3.connect(str(target))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        apply_schema(conn)
        _rebuild_all(conn, workspace_root)
        # The read-only Elo analytics fold runs AFTER every tournament has
        # been ingested (it reads the full match ledger off the
        # ``tournaments`` rows). It only ever writes the additive
        # ``generations.elo`` / ``generations.elo_games`` columns — Elo is
        # for visibility, never for the gate — and never touches a
        # decision/loss. A best-effort guard keeps a fold failure from
        # aborting an otherwise-complete rebuild.
        _fold_elo(conn)
        conn.commit()
    finally:
        conn.close()
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


def _rebuild_all(conn: sqlite3.Connection, workspace_root: Path) -> None:
    """Walk the whole workspace, populating every table.

    Epoch enumeration + ordering come from the canonical workspace-read
    layer (:func:`zicato.workspace.iter_epochs`), whose timestamp-first /
    numeric-aware authority is the single definition of epoch order — the
    same one the dashboard uses — so the index never disagrees with the
    rest of the system about which epoch precedes which. Each epoch's
    typed ``config.json`` is then read via
    :func:`zicato.epoch.lifecycle.load_epoch`. Generation lineage comes
    from :func:`zicato.epoch.lineage.load_lineage`. Generation directories
    and run directories are additionally walked so a generation / run
    whose telemetry landed before lineage was updated is still indexed.
    """
    from zicato.epoch.lifecycle import load_epoch  # noqa: PLC0415
    from zicato.epoch.lineage import load_lineage  # noqa: PLC0415
    from zicato.workspace import WorkspaceLayout, iter_epochs  # noqa: PLC0415

    lineage = load_lineage(workspace_root)
    lineage_by_epoch: dict[str, dict[str, Any]] = {}
    for entry in lineage.get("epochs", []):
        eid = entry.get("id")
        if isinstance(eid, str):
            lineage_by_epoch[eid] = entry

    # Enumerate the ``epochs/`` directory through the canonical layer so the
    # walk order is the workspace's one timestamp-first authority. Each
    # directory-bearing epoch with a readable ``config.json`` is indexed
    # here; a directory whose config is missing / unreadable is left to the
    # lineage-only pass below (it is a thin auto-created entry), preserving
    # the exact set of epochs the prior ``list_epochs``-driven walk indexed.
    layout = WorkspaceLayout.from_root(workspace_root)
    seen_epochs: set[str] = set()
    for epoch in iter_epochs(layout):
        try:
            cfg = load_epoch(workspace_root, epoch.id)
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            # No readable config.json — defer to the lineage-only pass so a
            # thin epoch still gets a row (matching the prior behaviour, where
            # ``list_epochs`` skipped unreadable configs the same way).
            continue
        seen_epochs.add(epoch.id)
        _upsert_epoch(
            conn,
            epoch_id=cfg.id,
            contract_hash=cfg.contract_hash,
            created_at=cfg.created_at,
            closed=cfg.closed,
            goal=cfg.goal,
            parent_epoch_id=_parent_epoch_id_from_lineage_entry(lineage_by_epoch.get(cfg.id)),
        )
        _rebuild_epoch(conn, workspace_root, cfg.id, lineage_by_epoch.get(cfg.id))

    for eid, entry in lineage_by_epoch.items():
        if eid in seen_epochs:
            continue
        # Epoch known only to lineage — no config.json. Index a thin
        # epoch row and still walk its generations / runs. ``goal`` is
        # left empty because there is no config.json to read it from.
        _upsert_epoch(
            conn,
            epoch_id=eid,
            contract_hash="",
            created_at=str(entry.get("started_at", "")),
            closed=bool(entry.get("closed_at")),
            goal="",
            parent_epoch_id=_parent_epoch_id_from_lineage_entry(entry),
        )
        _rebuild_epoch(conn, workspace_root, eid, entry)


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
    in-progress epochs without waiting for a full ``zicato reindex``.

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
    the index immediately, without waiting for a full ``zicato reindex``.

    A no-op for a degenerate two-competitor (gauntlet) field. Idempotent —
    the write is a keyed upsert on the field-level ``tournament_id``. When
    the database does not exist yet it is created with the schema applied.
    """
    conn, _ = _open_for_write(workspace_root, db_path)
    try:
        _upsert_field_tournament(conn, record)
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
    "ingest_run",
    "ingest_experiment",
    "ingest_field_tournament",
    "backfill_generations",
    "repair_epoch_goals",
    "backfill_tournament_fk",
]
