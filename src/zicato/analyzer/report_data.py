"""Deterministic data-gathering for the epoch analysis report.

The comprehensive epoch analysis report (see
:mod:`zicato.analyzer.report`) is a *hybrid* artifact: every
data-bearing fact in it is templated directly from the structured
workspace data, and only the interpretive prose is written by an LLM.
This module owns the first half — it walks one epoch's workspace tree
and reduces every structured artifact into a single frozen
:class:`EpochReportData` view.

The artifacts read here, all under ``epochs/{epoch_id}/``:

* ``config.json`` — the epoch's :class:`~zicato.core.types.EpochConfig`
  (id, name, contract hash, open/closed state).
* ``board.jsonl`` — the evaluation board (entries, expectation kinds,
  judges, weights).
* ``scoring.json`` — the frozen scoring model.
* ``brief.md`` — the operator's proposer brief (the epoch's goal).
* ``mutations.json`` — the most-recent enumerated mutation surface.
* ``generations/{gen}/experiment.json`` — every experiment.
* ``generations/{gen}/gen_score.json`` — the cached per-generation
  tournament aggregate (scalar, drift_loss_mean, pass_rate, ...).
* ``generations/{gen}/runs/{entry}/loss.json`` — the reducer's per-run
  loss profile.
* ``journal.md`` — the running narrative.

Every read is best-effort: a missing or malformed file degrades to an
empty / default value rather than raising, so the report still
generates on a partially-populated workspace (the common case
mid-epoch). The one structural exception is the path math, which is
pure.

The :class:`EpochReportData` view is JSON-friendly throughout — it is
both rendered into the report's deterministic sections AND serialised
verbatim into the LLM prompt, so the model interprets exactly the same
numbers the operator reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.core.workspace import _normalise_workspace_root
from zicato.workspace import (
    WorkspaceLayout,
    read_epoch_config,
    read_experiments,
    read_gen_score,
    read_loss,
    round_indices,
    run_entry_ids,
)

# Soft caps so the data view (and therefore the prompt) stays bounded.
_MAX_JOURNAL_CHARS = 40_000
_MAX_BRIEF_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class BoardEntryView:
    """One board entry reduced to the fields the report cares about."""

    id: str
    kind: str
    weight: float
    tags: tuple[str, ...]
    expectation_kind: str
    expectation_spec: str
    judges: tuple[str, ...]
    wall_clock_budget_seconds: int


@dataclass(frozen=True, slots=True)
class GenerationView:
    """One generation's proposer hypothesis joined with its outcome.

    Every numeric field is sourced verbatim from ``experiment.json`` /
    ``gen_score.json``; the renderers and the LLM prompt both consume
    this view so a number is never restated by hand.
    """

    generation_id: str
    parent_generation_id: str
    is_baseline: bool
    proposed_at: str
    core_idea: str
    why: str
    risks: str
    modulating: tuple[str, ...]
    expected_pass_rate_delta: str
    expected_drift_movements: tuple[dict[str, str], ...]
    decision: str
    rejection_reason: str
    scalar_score_delta: float
    drift_loss_delta: float
    pass_rate_delta: float
    drift_movements: tuple[dict[str, Any], ...]
    metric_movements: tuple[dict[str, Any], ...]
    patches: tuple[dict[str, str], ...]
    # Cached absolute tournament aggregate for this generation, if the
    # orchestrator wrote a ``gen_score.json``. Empty dict when absent.
    gen_score: dict[str, Any] = field(default_factory=dict)
    # The cumulated scalar score (baseline seeded at 0.0, deltas summed
    # along the promoted lineage). Deterministic — see
    # :func:`_cumulate_scalar`.
    cumulative_scalar: float = 0.0
    # Per-judge weighted-loss totals across every run under this
    # generation, sorted by judge_name. The reducer's
    # :attr:`LossProfile.per_judge_loss` is summed across each board
    # entry's run — this gives the analyzer the "which judges drove
    # this round's loss" view without reopening the index DB. Empty
    # tuple when no custom judge fired (or no loss profiles were
    # readable).
    per_judge_loss_totals: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class EpochReportData:
    """The complete deterministic view of one epoch for the report.

    Frozen and JSON-friendly: rendered into the report's deterministic
    sections and serialised verbatim into the LLM prompt so the model
    never has to invent a number.
    """

    epoch_id: str
    epoch_name: str
    contract_hash: str
    created_at: str
    closed: bool
    closed_at: str
    brief_text: str
    journal_text: str
    board_entries: tuple[BoardEntryView, ...]
    disable_drift: tuple[str, ...]
    scoring: dict[str, Any]
    mutation_surface: tuple[dict[str, Any], ...]
    generations: tuple[GenerationView, ...]
    span_start: str
    span_end: str
    # The free-form operator-supplied goal for the epoch. Empty when
    # no goal was recorded; the analyzer renders that case as "no goal
    # recorded" in the header so the report shape stays uniform.
    goal: str = ""
    # The frozen telemetry dialect (which reducer vocabulary drift is read
    # through). Sourced from ``scoring.json``; empty when the epoch
    # predates the field, which the renderer degrades to "default".
    telemetry_dialect: str = ""
    # The per-epoch tournament structure, ``{"structure": name, "params":
    # {...}}`` as serialised into ``scoring.json``. Empty dict ⇒ the
    # gauntlet default (renderer says so rather than fabricating params).
    tournament_structure: dict[str, Any] = field(default_factory=dict)
    # The nested proposer-quality config (best_of_n / critique / screen /
    # exemplars / genealogy / recombine ...), as serialised into
    # ``scoring.json``. Empty dict ⇒ the built-in defaults.
    proposer_quality: dict[str, Any] = field(default_factory=dict)
    # The durable per-round event records, folded from
    # ``epochs/{id}/rounds/{n}/round_log.jsonl`` (best-effort; empty tuple
    # when no round has settled yet — the evolve path emits the log per
    # round, so the validity / proposer-analytics sections light up as
    # rounds accrue and degrade honestly before the first one settles).
    round_records: tuple[Any, ...] = ()

    @property
    def attempted(self) -> int:
        """Count of non-baseline generations (experiments) in the epoch."""
        return sum(1 for g in self.generations if not g.is_baseline)

    @property
    def promoted(self) -> int:
        return sum(1 for g in self.generations if g.decision == "promoted")

    @property
    def rejected(self) -> int:
        return sum(1 for g in self.generations if g.decision == "rejected")

    @property
    def deferred(self) -> int:
        return sum(1 for g in self.generations if g.decision == "deferred")

    @property
    def final_scalar(self) -> float:
        """Cumulative scalar of the generation currently IN FORCE.

        CHAMPION-ANCHORED: the last promoted generation, or the baseline
        (``0.0``) when nothing has promoted. This is the number the
        harness actually stands behind.

        It is not ``generations[-1]``. ``_cumulate_scalar``
        fills a cumulative for every generation regardless of decision,
        so the newest row is a *rejected* challenger whenever the last
        round did not promote — and its cumulative is a counterfactual,
        the score the lineage would have carried had the challenger been
        accepted. Publishing that as the lineage's score credits the
        promoted lineage with work that was measured and then discarded,
        which in a zero-promotion epoch headlines a stalled campaign as
        an improving one. See :attr:`latest_rejected_scalar` for the
        counterfactual under its own name.
        """
        champion = 0.0
        for g in self.generations:
            if g.is_baseline:
                champion = g.cumulative_scalar
            elif g.decision == "promoted":
                champion = g.cumulative_scalar
        return champion

    @property
    def latest_rejected_scalar(self) -> float | None:
        """The most recent REJECTED challenger's counterfactual cumulative.

        The score that challenger would have taken the lineage to had it
        cleared the gate — a path not taken, never the lineage's own
        score. ``None`` when nothing has been rejected.
        """
        for g in reversed(self.generations):
            if g.decision == "rejected" and not g.is_baseline:
                return g.cumulative_scalar
        return None

    @property
    def last_round(self) -> int:
        """The settled-round count the document is current through.

        Used by the masthead's ``LIVING DRAFT — through round N`` stamp.
        Derived from the number of challenger generations settled so far
        (each round settles exactly one), so it is deterministic and needs
        no extra artifact read.
        """
        return self.attempted


# ---------------------------------------------------------------------------
# Best-effort JSON readers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    """Read+parse a JSON file, returning ``None`` on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_text(path: Path, limit: int) -> str:
    """Read a text file (size-capped), returning ``""`` on any failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) > limit:
        return text[:limit] + "\n\n... [truncated for the analysis report]"
    return text


# ---------------------------------------------------------------------------
# Per-artifact reducers
# ---------------------------------------------------------------------------


def _load_epoch_config(layout: WorkspaceLayout, epoch_id: str) -> dict[str, Any]:
    """Read ``config.json`` for the epoch into a plain dict (best-effort)."""
    cfg = read_epoch_config(layout, epoch_id)
    return cfg if isinstance(cfg, dict) else {}


def _load_board(
    layout: WorkspaceLayout, epoch_id: str
) -> tuple[tuple[BoardEntryView, ...], tuple[str, ...]]:
    """Reduce ``board.jsonl`` to entry views + the disable_drift list.

    Uses the strict board loader when it parses cleanly; on any failure
    (a board predating the current schema, a malformed line) it falls
    back to a tolerant JSONL re-read so the report still surfaces the
    entry ids and kinds. Either path yields the same view shape.
    """
    bpath = layout.board(epoch_id)
    if not bpath.exists():
        return (), ()
    try:
        from zicato.board.jsonl import load_board_with_meta  # noqa: PLC0415

        entries, disable_drift, _judge_only = load_board_with_meta(bpath)
        views: list[BoardEntryView] = []
        for e in entries:
            exp_kind = e.expectation.kind if e.expectation is not None else ""
            exp_spec = e.expectation.spec if e.expectation is not None else ""
            views.append(
                BoardEntryView(
                    id=e.id,
                    kind=str(e.kind),
                    weight=float(e.weight),
                    tags=tuple(e.tags),
                    expectation_kind=str(exp_kind),
                    expectation_spec=str(exp_spec),
                    judges=tuple(j.name for j in e.judges),
                    wall_clock_budget_seconds=int(e.wall_clock_budget_seconds),
                )
            )
        return tuple(views), tuple(str(d) for d in disable_drift)
    except Exception:  # noqa: BLE001 — fall back to a tolerant re-read
        return _load_board_tolerant(bpath), ()


def _load_board_tolerant(bpath: Path) -> tuple[BoardEntryView, ...]:
    """Tolerant JSONL re-read of a board file the strict loader rejected."""
    views: list[BoardEntryView] = []
    try:
        lines = bpath.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("board_meta") is True:
            continue
        exp = obj.get("expectation")
        exp_kind = str(exp.get("kind", "")) if isinstance(exp, dict) else ""
        exp_spec = str(exp.get("spec", "")) if isinstance(exp, dict) else ""
        judges_raw = obj.get("judges", []) or []
        judge_names = tuple(str(j.get("name", "")) for j in judges_raw if isinstance(j, dict))
        budget = obj.get("wall_clock_budget_seconds", obj.get("budget_s", 0))
        try:
            budget_i = int(budget) if budget is not None else 0
        except (TypeError, ValueError):
            budget_i = 0
        views.append(
            BoardEntryView(
                id=str(obj.get("id", "")),
                kind=str(obj.get("kind", "")),
                weight=float(obj.get("weight", 1.0)),
                tags=tuple(str(t) for t in obj.get("tags", []) or []),
                expectation_kind=exp_kind,
                expectation_spec=exp_spec,
                judges=judge_names,
                wall_clock_budget_seconds=budget_i,
            )
        )
    return tuple(views)


def _load_scoring(layout: WorkspaceLayout, epoch_id: str) -> dict[str, Any]:
    """Read ``scoring.json`` into a plain dict (best-effort)."""
    raw = _read_json(layout.scoring(epoch_id))
    return raw if isinstance(raw, dict) else {}


def load_mutation_surface(layout: WorkspaceLayout, epoch_id: str) -> tuple[dict[str, Any], ...]:
    """Read ``mutations.json`` — the most-recent enumerated surface.

    Public because it is the ONE reader of the epoch's frozen mutation
    enumeration that keeps every recorded field (the dashboard's
    ``_parse_mutations`` keeps only a preview). The dashboard's
    mutation-site browser reads it too, as the record that outlives a
    pruned snapshot tree — see :mod:`zicato.dashboard.mutations`.
    """
    raw = _read_json(layout.mutations(epoch_id))
    if not isinstance(raw, list):
        return ()
    out: list[dict[str, Any]] = []
    for m in raw:
        if isinstance(m, dict):
            out.append(m)
    return tuple(out)


def _str_movements(raw: Any) -> tuple[dict[str, str], ...]:
    """Coerce a list of expected-movement dicts to string-valued dicts."""
    out: list[dict[str, str]] = []
    for m in raw or ():
        if isinstance(m, dict):
            out.append({str(k): str(v) for k, v in m.items()})
    return tuple(out)


def _load_one_generation(
    layout: WorkspaceLayout,
    epoch_id: str,
    gen_id_dir: str,
    exp_raw: dict[str, Any],
) -> GenerationView | None:
    """Reduce one generation's experiment dict to a :class:`GenerationView`.

    ``exp_raw`` is the generation's already-read ``experiment.json`` (the
    canonical enumeration only yields generations whose experiment parsed
    cleanly). ``gen_id_dir`` is the generation directory name — the
    fallback id when ``experiment.json`` carries none. The per-generation
    cached aggregate, patches, and run-level loss totals are read off the
    layout. Always returns a view (never ``None`` for a present
    experiment); a generation with an experiment but no outcome yields a
    view with ``decision="pending"``.
    """
    hyp = exp_raw.get("hypothesis")
    hyp = hyp if isinstance(hyp, dict) else {}
    outcome = exp_raw.get("outcome")
    outcome = outcome if isinstance(outcome, dict) else None

    parent = str(exp_raw.get("parent_generation_id", "") or "")
    gen_id = str(exp_raw.get("generation_id", gen_id_dir))
    is_baseline = not parent or parent == gen_id

    if outcome is not None:
        decision = str(outcome.get("tournament_decision", "pending") or "pending")
        rejection_reason = str(outcome.get("rejection_reason", "") or "")
        scalar_delta = _as_float(outcome.get("scalar_score_delta"))
        drift_delta = _as_float(outcome.get("drift_loss_delta"))
        pass_delta = _as_float(outcome.get("pass_rate_delta"))
        drift_movements = _movement_dicts(outcome.get("drift_movements"))
        metric_movements = _movement_dicts(outcome.get("metric_movements"))
    else:
        decision = "baseline" if is_baseline else "pending"
        rejection_reason = ""
        scalar_delta = drift_delta = pass_delta = 0.0
        drift_movements = ()
        metric_movements = ()

    patches = _load_patches(layout, epoch_id, gen_id_dir, exp_raw)
    gen_score = read_gen_score(layout, epoch_id, gen_id_dir)
    per_judge_totals = _load_per_judge_totals(layout, epoch_id, gen_id_dir)

    return GenerationView(
        generation_id=gen_id,
        parent_generation_id=parent,
        is_baseline=is_baseline,
        proposed_at=str(exp_raw.get("proposed_at", "") or ""),
        core_idea=str(hyp.get("core_idea", "") or ""),
        why=str(hyp.get("why", "") or ""),
        risks=str(hyp.get("risks", "") or ""),
        modulating=tuple(str(m) for m in hyp.get("modulating", []) or ()),
        expected_pass_rate_delta=str(hyp.get("expected_pass_rate_delta", "") or ""),
        expected_drift_movements=_str_movements(hyp.get("expected_drift_movements")),
        decision=decision,
        rejection_reason=rejection_reason,
        scalar_score_delta=scalar_delta,
        drift_loss_delta=drift_delta,
        pass_rate_delta=pass_delta,
        drift_movements=drift_movements,
        metric_movements=metric_movements,
        patches=patches,
        gen_score=gen_score,
        per_judge_loss_totals=per_judge_totals,
    )


def _load_per_judge_totals(
    layout: WorkspaceLayout, epoch_id: str, generation_id: str
) -> tuple[tuple[str, float], ...]:
    """Sum ``per_judge_loss`` across every ``loss.json`` under one generation.

    Walks ``runs/*/loss.json`` directly and pulls the ``per_judge_loss``
    array off each profile (the reducer's per-judge weighted-loss
    attribution). Tolerant of missing / unparseable files — a run with
    no loss.json contributes nothing. Returns the totals sorted by
    judge_name so the analyzer's table iteration is deterministic.

    Returns the empty tuple when no judge fired (or no runs landed) —
    a no-custom-judge board produces no rows under this section.
    """
    totals: dict[str, float] = {}
    for entry_id in run_entry_ids(layout, epoch_id, generation_id):
        raw = read_loss(layout, epoch_id, generation_id, entry_id)
        if raw is None:
            continue
        per_judge = raw.get("per_judge_loss")
        if not isinstance(per_judge, list):
            continue
        for j in per_judge:
            if not isinstance(j, dict):
                continue
            name = str(j.get("judge_name", "") or "")
            # Promote the unattributed bucket to a stable display label
            # so the table's row order stays deterministic and the cell
            # is recognisable.
            display = name if name else "(unattributed)"
            weighted = _as_float(j.get("weighted_loss", 0.0))
            totals[display] = totals.get(display, 0.0) + weighted
    return tuple(sorted(totals.items()))


def _load_patches(
    layout: WorkspaceLayout,
    epoch_id: str,
    generation_id: str,
    exp_raw: dict[str, Any],
) -> tuple[dict[str, str], ...]:
    """Resolve a generation's patches from inline or per-patch storage.

    The journal writer persists patches one-file-each under
    ``patches/{id}.json`` and lists their ids in ``experiment.json``;
    workspaces predating that refactor inline a ``patches`` list. Both
    shapes resolve here to the same reduced view.
    """
    out: list[dict[str, str]] = []
    inline = exp_raw.get("patches")
    if isinstance(inline, list):
        for p in inline:
            if isinstance(p, dict):
                out.append(_patch_view(p))
        return tuple(out)
    patch_ids = exp_raw.get("patch_ids")
    if isinstance(patch_ids, list):
        patches_dir = layout.patches_dir(epoch_id, generation_id)
        for pid in patch_ids:
            p = _read_json(patches_dir / f"{pid}.json")
            if isinstance(p, dict):
                out.append(_patch_view(p))
    return tuple(out)


def _patch_view(p: dict[str, Any]) -> dict[str, str]:
    """Reduce one patch dict to the report-relevant string fields."""
    return {
        "mutation_id": str(p.get("mutation_id", "")),
        "op": str(p.get("op", "")),
        "rationale": str(p.get("rationale", "")),
    }


def _movement_dicts(raw: Any) -> tuple[dict[str, Any], ...]:
    """Coerce a list of realised-movement dicts to plain dicts."""
    out: list[dict[str, Any]] = []
    for m in raw or ():
        if isinstance(m, dict):
            out.append(dict(m))
    return tuple(out)


def _as_float(value: Any) -> float:
    """Coerce ``value`` to ``float``, defaulting to ``0.0`` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _cumulate_scalar(generations: list[GenerationView]) -> list[GenerationView]:
    """Return a copy of ``generations`` with ``cumulative_scalar`` filled.

    The baseline seeds at ``0.0``; every subsequent generation adds its
    ``scalar_score_delta`` to its parent's cumulative scalar. A
    generation whose parent is unknown inherits ``0.0``. The result is
    deterministic and matches the trajectory the renderers plot.
    """
    from dataclasses import replace as _replace  # noqa: PLC0415

    by_id: dict[str, float] = {}
    out: list[GenerationView] = []
    for g in generations:
        if g.is_baseline:
            score = 0.0
        else:
            parent_score = by_id.get(g.parent_generation_id, 0.0)
            score = parent_score + g.scalar_score_delta
        by_id[g.generation_id] = score
        out.append(_replace(g, cumulative_scalar=score))
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _load_round_records(workspace_root: Path, epoch_id: str) -> tuple[Any, ...]:
    """Fold every ``rounds/{n}/round_log.jsonl`` into typed round records.

    Best-effort: the orchestrator emits the durable per-round event log as
    each round settles, so a freshly-opened epoch with no settled round yet
    has no ``rounds/`` tree and this returns ``()`` — the validity and
    proposer-analytics sections then degrade to their honest one-liners.
    As rounds accrue the same reader lights those sections up with no
    report change. A malformed / interior-corrupt log for one round is
    skipped rather than failing the whole gather.
    """
    from zicato.epoch.round_log import RoundLog, fold_round_record  # noqa: PLC0415

    records: list[Any] = []
    for idx in round_indices(WorkspaceLayout.from_root(workspace_root), epoch_id):
        try:
            events = RoundLog(workspace_root, epoch_id, idx).read()
            records.append(fold_round_record(events))
        except Exception:  # noqa: BLE001 — one bad round never sinks the gather
            continue
    return tuple(records)


def _distill_brief_goal(brief: str) -> str:
    """The brief's ``## Goal`` paragraph, or ``""`` when there is none.

    The operator's goal lives in the proposer brief's ``## Goal`` section,
    not in ``config.json`` (whose ``goal`` field is usually empty). The
    masthead must name the same goal the rest of the UI shows, so this
    DELEGATES to the dashboard's distillation rather than restating it.

    A second copy of that logic here would drift from the dashboard's: when
    only one of the two learns to reassemble a hard-wrapped goal paragraph,
    the other still returns the first PHYSICAL line and a masthead renders
    the goal truncated mid-word at a dangling hyphen (issue #107). One
    distiller, one behaviour — do not re-inline it.
    """
    from zicato.query.epoch_view import _distill_brief_goal as _distill  # noqa: PLC0415

    return _distill(brief) or ""


def gather_epoch_report_data(workspace_root: Path, epoch_id: str) -> EpochReportData:
    """Walk one epoch's workspace tree into a frozen :class:`EpochReportData`.

    Every artifact is read best-effort: a missing or malformed file
    degrades to an empty / default value. The function therefore always
    returns a populated view (possibly with zero generations) and never
    raises on a partially-written workspace — the report generator is a
    best-effort, regenerated-each-round caller.
    """
    # The analyzer accepts either the inner ``.zicato`` root or the outer
    # project dir (a historical caller passed the latter); normalise to
    # the inner form once, then route every read through the canonical
    # workspace layer so enumeration + ordering share one authority.
    layout = WorkspaceLayout.from_root(_normalise_workspace_root(Path(workspace_root)))

    cfg = _load_epoch_config(layout, epoch_id)
    board_entries, disable_drift = _load_board(layout, epoch_id)
    scoring = _load_scoring(layout, epoch_id)
    mutation_surface = load_mutation_surface(layout, epoch_id)

    brief_text = _read_text(layout.brief(epoch_id), _MAX_BRIEF_CHARS)
    journal_text = _read_text(layout.journal(epoch_id), _MAX_JOURNAL_CHARS)

    raw_generations: list[GenerationView] = []
    for gen_id_dir, exp_raw in read_experiments(layout, epoch_id):
        view = _load_one_generation(layout, epoch_id, gen_id_dir, exp_raw)
        if view is not None:
            raw_generations.append(view)
    generations = _cumulate_scalar(raw_generations)

    timestamps = [g.proposed_at for g in generations if g.proposed_at]
    span_start = min(timestamps) if timestamps else ""
    span_end = max(timestamps) if timestamps else ""

    ts_raw = scoring.get("tournament_structure")
    tournament_structure = ts_raw if isinstance(ts_raw, dict) else {}
    pq_raw = scoring.get("proposer_quality")
    proposer_quality = pq_raw if isinstance(pq_raw, dict) else {}
    round_records = _load_round_records(layout.root, epoch_id)

    return EpochReportData(
        epoch_id=epoch_id,
        epoch_name=str(cfg.get("name", "") or epoch_id),
        contract_hash=str(cfg.get("contract_hash", "") or ""),
        created_at=str(cfg.get("created_at", "") or ""),
        closed=bool(cfg.get("closed", False)),
        closed_at=str(cfg.get("closed_at", "") or ""),
        brief_text=brief_text,
        journal_text=journal_text,
        board_entries=board_entries,
        disable_drift=disable_drift,
        scoring=scoring,
        mutation_surface=mutation_surface,
        generations=tuple(generations),
        span_start=span_start,
        span_end=span_end,
        goal=str(cfg.get("goal", "") or "") or _distill_brief_goal(brief_text),
        telemetry_dialect=str(scoring.get("telemetry_dialect", "") or ""),
        tournament_structure=tournament_structure,
        proposer_quality=proposer_quality,
        round_records=round_records,
    )


__all__ = [
    "BoardEntryView",
    "GenerationView",
    "EpochReportData",
    "gather_epoch_report_data",
]
