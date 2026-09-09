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

Every read degrades rather than raising, so the report still generates
on a partially-populated workspace (the common case mid-epoch). Where a
record has an owning codec the strict decode is caught at this boundary
and the reason is carried into the view — a generation whose
``experiment.json`` does not parse lands in
:attr:`EpochReportData.unreadable_generations` rather than vanishing
from a report that then reads as complete. The one structural exception
is the path math, which is pure.

The :class:`EpochReportData` view is JSON-friendly throughout — it is
both rendered into the report's deterministic sections AND serialised
verbatim into the LLM prompt, so the model interprets exactly the same
numbers the operator reads.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from zicato.core.types import (
    ExpectedMetricMovement,
    Experiment,
    MetricMovementActual,
)
from zicato.core.workspace import _normalise_workspace_root
from zicato.epoch._storage import RecordError
from zicato.epoch.journal import read_epoch_experiments
from zicato.mutation.inventory import read_mutation_inventory
from zicato.proposer.brief import brief_goal, load_epoch_brief
from zicato.tournament.scoring import read_gen_score
from zicato.util.text import preview
from zicato.workspace import (
    ScalarStep,
    WorkspaceLayout,
    cumulative_scalars,
    per_judge_loss_totals,
    read_board_entries,
    read_epoch_config,
    read_round_records,
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
    expected_metric_movements: tuple[dict[str, str], ...]
    decision: str
    rejection_reason: str
    scalar_score_delta: float
    drift_loss_delta: float
    pass_rate_delta: float
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
    # One named reason per generation whose ``experiment.json`` is present
    # and does not parse. Such a generation is absent from
    # :attr:`generations`, and without this the report would present a
    # short epoch as a complete one; the renderer states the reason
    # instead. Empty on every intact epoch.
    unreadable_generations: tuple[str, ...] = ()

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
    return _report_text(text, limit)


def _report_text(text: str, limit: int) -> str:
    """Cap embedded guidance and journal text with an explicit truncation note."""
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

    Reads through the shared canonical board reader
    (:func:`zicato.workspace.read_board_entries`), which the query layer's
    file-reading board paths use as well, so one rule decides what an
    epoch's board holds. A missing board yields empty entries; a present
    unreadable board raises RecordError before a report can be published.
    """
    board = read_board_entries(layout, epoch_id)
    if board is None:
        return (), ()
    views: list[BoardEntryView] = []
    for e in board.entries:
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
    return tuple(views), board.disable_drift


def _load_scoring(layout: WorkspaceLayout, epoch_id: str) -> dict[str, Any]:
    """Read ``scoring.json`` into a plain dict (best-effort)."""
    raw = _read_json(layout.scoring(epoch_id))
    return raw if isinstance(raw, dict) else {}


def _str_movements(
    movements: Sequence[ExpectedMetricMovement],
) -> tuple[dict[str, str], ...]:
    """Project the predicted metric movements to string-valued dicts."""
    return tuple(
        {"metric_name": m.metric_name, "direction": str(m.direction), "magnitude": str(m.magnitude)}
        for m in movements
    )


def _load_one_generation(
    layout: WorkspaceLayout,
    epoch_id: str,
    gen_id_dir: str,
    experiment: Experiment,
) -> GenerationView:
    """Reduce one generation's experiment to a :class:`GenerationView`.

    ``experiment`` is the generation's decoded record; ``gen_id_dir`` is
    the generation directory name, which is where the per-generation
    cached aggregate and run-level loss totals are read from. A generation
    with an experiment but no outcome yields a view with
    ``decision="pending"``.
    """
    hypothesis = experiment.hypothesis
    outcome = experiment.outcome

    parent = experiment.parent_generation_id or ""
    gen_id = experiment.generation_id
    is_baseline = not parent or parent == gen_id

    if outcome is not None:
        decision = str(outcome.tournament_decision or "pending")
        rejection_reason = outcome.rejection_reason
        scalar_delta = outcome.scalar_score_delta
        drift_delta = outcome.drift_loss_delta
        pass_delta = outcome.pass_rate_delta
        metric_movements = _movement_dicts(outcome.metric_movements)
    else:
        decision = "baseline" if is_baseline else "pending"
        rejection_reason = ""
        scalar_delta = drift_delta = pass_delta = 0.0
        metric_movements = ()

    patches = _patch_views(experiment)
    gen_score = read_gen_score(layout, epoch_id, gen_id_dir)
    gen_score_body = gen_score.to_dict() if gen_score else {}
    per_judge_totals = _load_per_judge_totals(layout, epoch_id, gen_id_dir)

    return GenerationView(
        generation_id=gen_id,
        parent_generation_id=parent,
        is_baseline=is_baseline,
        proposed_at=experiment.proposed_at,
        core_idea=hypothesis.core_idea,
        why=hypothesis.why,
        risks=hypothesis.risks,
        modulating=tuple(str(m) for m in hypothesis.modulating),
        expected_pass_rate_delta=hypothesis.expected_pass_rate_delta,
        expected_metric_movements=_str_movements(hypothesis.expected_metric_movements),
        decision=decision,
        rejection_reason=rejection_reason,
        scalar_score_delta=scalar_delta,
        drift_loss_delta=drift_delta,
        pass_rate_delta=pass_delta,
        metric_movements=metric_movements,
        patches=patches,
        gen_score=gen_score_body,
        per_judge_loss_totals=per_judge_totals,
    )


def _load_per_judge_totals(
    layout: WorkspaceLayout, epoch_id: str, generation_id: str
) -> tuple[tuple[str, float], ...]:
    """One generation's per-judge weighted-loss totals, labelled for the table.

    The summation itself is the shared canonical one
    (:func:`zicato.workspace.per_judge_loss_totals`), which adds each run's
    recorded per-judge weighted loss across the generation's runs. This
    function adds only the report's presentation: the reducer's
    unattributed bucket, keyed on disk by the empty judge name, is promoted
    to a readable label, and the rows are re-sorted so the table's iteration
    order stays deterministic under that label.

    Returns the empty tuple when no judge fired (or no runs landed) —
    a no-custom-judge board produces no rows under this section.
    """
    return tuple(
        sorted(
            (name if name else "(unattributed)", total)
            for name, total in per_judge_loss_totals(layout, epoch_id, generation_id)
        )
    )


def _patch_views(experiment: Experiment) -> tuple[dict[str, str], ...]:
    """Reduce a generation's patches to the report-relevant string fields.

    The patches come off the decoded record, which resolved them from the
    sibling ``patches/{id}.json`` files, so the report never opens those
    files itself.
    """
    return tuple(
        {
            "mutation_id": patch.mutation_id,
            "op": str(patch.op),
            "rationale": patch.rationale,
        }
        for patch in experiment.patches
    )


def _movement_dicts(
    movements: Sequence[MetricMovementActual],
) -> tuple[dict[str, Any], ...]:
    """Project the realised movements to plain dicts."""
    return tuple(asdict(m) for m in movements)


def _cumulate_scalar(generations: list[GenerationView]) -> list[GenerationView]:
    """Return a copy of ``generations`` with ``cumulative_scalar`` filled.

    The cumulation is the shared canonical one
    (:func:`zicato.workspace.cumulative_scalars`): the baseline seeds at
    ``0.0``, every subsequent generation adds its ``scalar_score_delta`` to
    its parent's cumulative scalar, and a generation whose parent is unknown
    inherits ``0.0``. The result is deterministic and matches the trajectory
    the renderers plot.
    """
    from dataclasses import replace as _replace  # noqa: PLC0415

    scores = cumulative_scalars(
        ScalarStep(
            generation_id=g.generation_id,
            parent_generation_id=g.parent_generation_id,
            is_baseline=g.is_baseline,
            scalar_score_delta=g.scalar_score_delta,
        )
        for g in generations
    )
    return [
        _replace(g, cumulative_scalar=score)
        for g, (_id, score) in zip(generations, scores, strict=True)
    ]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def gather_epoch_report_data(
    workspace_root: Path,
    epoch_id: str,
    *,
    recorded_experiments: tuple[list[tuple[str, Experiment]], list[str]] | None = None,
) -> EpochReportData:
    """Walk one epoch's workspace tree into a frozen :class:`EpochReportData`.

    An epoch-close caller supplies the experiment records already read for its
    narrative, so HTML generation uses the same experiment observations.

    Missing artifacts retain their empty/default representation. A present
    corrupt board or unreadable brief raises RecordError before publication;
    the report cannot describe a partial evaluation contract as complete.
    Refused generation records remain listed in unreadable_generations.
    """
    # The analyzer accepts either the inner ``.zicato`` root or the outer
    # project dir (a historical caller passed the latter); normalise to
    # the inner form once, then route every read through the canonical
    # workspace layer so enumeration + ordering share one authority.
    layout = WorkspaceLayout.from_root(_normalise_workspace_root(Path(workspace_root)))

    cfg = _load_epoch_config(layout, epoch_id)
    board_entries, disable_drift = _load_board(layout, epoch_id)
    scoring = _load_scoring(layout, epoch_id)
    mutation_surface = tuple(read_mutation_inventory(layout.mutations(epoch_id)) or ())

    try:
        brief_text = _report_text(
            load_epoch_brief(layout.epoch_dir(epoch_id)).text, _MAX_BRIEF_CHARS
        )
    except FileNotFoundError:
        brief_text = ""
    journal_text = _read_text(layout.journal(epoch_id), _MAX_JOURNAL_CHARS)

    experiments, read_errors = (
        read_epoch_experiments(layout.root, epoch_id)
        if recorded_experiments is None
        else recorded_experiments
    )
    unreadable_generations = list(read_errors)
    raw_generations = []
    for gen_id_dir, experiment in experiments:
        try:
            raw_generations.append(_load_one_generation(layout, epoch_id, gen_id_dir, experiment))
        except RecordError as exc:
            unreadable_generations.append(f"{gen_id_dir}: {exc}")
    generations = _cumulate_scalar(raw_generations)

    timestamps = [g.proposed_at for g in generations if g.proposed_at]
    span_start = min(timestamps) if timestamps else ""
    span_end = max(timestamps) if timestamps else ""

    from zicato.core.configuration import dataclass_to_jsonable
    from zicato.core.scoring_config import scoring_weights_from_dict

    tournament_structure = dataclass_to_jsonable(
        scoring_weights_from_dict(scoring).tournament_structure
    )
    pq_raw = scoring.get("proposer_quality")
    proposer_quality = pq_raw if isinstance(pq_raw, dict) else {}
    round_records = read_round_records(layout, epoch_id)

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
        goal=str(cfg.get("goal", "") or "") or preview(brief_goal(brief_text) or ""),
        telemetry_dialect=str(scoring.get("telemetry_dialect", "") or ""),
        tournament_structure=tournament_structure,
        proposer_quality=proposer_quality,
        round_records=round_records,
        unreadable_generations=tuple(unreadable_generations),
    )


__all__ = [
    "BoardEntryView",
    "GenerationView",
    "EpochReportData",
    "gather_epoch_report_data",
]
