"""Validate edits to evaluation inputs and publish accepted files together."""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.board.budgets import BUDGET_OUTLIER_FACTOR, assess_budget_outliers
from zicato.board.split import HOLDOUT_TAG, split_board
from zicato.contract_draft.admission import authored_edit
from zicato.contract_draft.draft import TournamentDraft
from zicato.core.configuration import authored_dataclass_from_json
from zicato.core.constraints import require_knob
from zicato.core.types import (
    VALID_TOURNAMENT_STRUCTURES,
    BoardEntry,
    JudgeSpec,
    ProposerQualityConfig,
    ScoringWeights,
    TournamentStructure,
)
from zicato.selection.registry import default_replicates_for
from zicato.selection.strategies.racing import SLICE_SCHEDULES
from zicato.selection.strategy import _param_float, _param_int

if TYPE_CHECKING:
    from zicato.runtime.lock import WorkspaceLock

# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DraftPatch:
    """What one operation changed, for the UI / chat to render.

    Fields
    ------
    op:
        The operation name (e.g. ``"set_structure"``).
    changed:
        Human-facing summary of the fields the op touched, as
        ``field -> {"from": old, "to": new}`` JSON-friendly entries.
    note:
        Optional one-line note (e.g. a no-op explanation).
    """

    op: str
    changed: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable snapshot for the UI."""
        return {"op": self.op, "changed": self.changed, "note": self.note}


@dataclass(frozen=True, slots=True)
class CostLine:
    """One line of the cost-meter breakdown.

    Fields
    ------
    label:
        Human-readable term name (e.g. ``"per-duel runs"``).
    runs:
        Runs this term contributes. Board-runs for every term except the
        clearly-labelled evaluation lines (``best-of-N propose calls``),
        which count LLM calls and are excluded from the board-runs
        headline — the label + detail say so.
    detail:
        Short arithmetic explanation (e.g. ``"field_size 2 × replicates 1"``).
    """

    label: str
    runs: int
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "runs": self.runs, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Board-runs-per-round estimate the cost-meter renders.

    Fields
    ------
    structure:
        The structure the estimate is for.
    board_size:
        Number of entries on the (train) board the estimate assumes.
    holdout_size:
        Number of held-out entries (the confirm-runs term scales with
        this).
    board_runs_per_round:
        Total board-runs per evolve round — the headline number.
    breakdown:
        Per-term contributions (see :class:`CostLine`).
    """

    structure: str
    board_size: int
    holdout_size: int
    board_runs_per_round: int
    breakdown: tuple[CostLine, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "structure": self.structure,
            "board_size": self.board_size,
            "holdout_size": self.holdout_size,
            "board_runs_per_round": self.board_runs_per_round,
            "breakdown": [line.to_dict() for line in self.breakdown],
        }


@dataclass(frozen=True, slots=True)
class Warning:
    """One validation warning surfaced to the operator.

    Fields
    ------
    code:
        Stable symbolic code (e.g. ``"field_size_degrades_to_gauntlet"``)
        so the UI can key on it.
    message:
        Human-readable explanation.
    severity:
        ``"info"`` (advisory) / ``"warning"`` (likely a mistake) /
        ``"refuse"`` (statistically unsound). Warnings inform the operator;
        they do not block publication.
    """

    code: str
    message: str
    severity: str = "warning"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """The outcome of :func:`apply`.

    Fields
    ------
    confirmed:
        ``True`` when the draft was written (``confirm=True``); ``False``
        for a dry-run preview.
    rolled:
        ``True`` iff applying changed the contract — i.e. the next resolve
        will roll the epoch. Always ``False`` for a dry run.
    components_changed:
        Which contract components differ from live (the same set
        :class:`~zicato.contract_draft.draft.ContractDiff` reports).
    new_contract_hash:
        The contract hash the draft resolves to. For a dry run this is the
        *predicted* hash (computed over a temp materialization); for a
        confirmed apply it is the hash the written contract produces.
    cost:
        The cost estimate for the applied / previewed draft.
    diff:
        The contract diff vs. live.
    warnings:
        Validation warnings for the draft.
    """

    confirmed: bool
    rolled: bool
    components_changed: tuple[str, ...]
    new_contract_hash: str
    cost: CostEstimate
    diff: dict[str, Any]
    warnings: tuple[Warning, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "rolled": self.rolled,
            "components_changed": list(self.components_changed),
            "new_contract_hash": self.new_contract_hash,
            "cost": self.cost.to_dict(),
            "diff": self.diff,
            "warnings": [w.to_dict() for w in self.warnings],
        }


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


def _replace_scoring(draft: TournamentDraft, **changes: Any) -> ScoringWeights:
    """Return a new :class:`ScoringWeights` with ``changes`` applied."""
    return dataclasses.replace(draft.scoring, **changes)


@authored_edit
def set_structure(draft: TournamentDraft, structure: str) -> DraftPatch:
    """Set the tournament structure, preserving the existing params.

    Raises :class:`ValueError` on an invalid structure token (the
    :class:`TournamentStructure` constructor validates and lists the
    valid tokens) and on an experimental token while the draft's
    ``experimental.tournament_structures`` flag is off (the
    :class:`ScoringWeights` constructor refuses it, naming the flag).
    """
    old = draft.scoring.tournament_structure
    new_ts = TournamentStructure(structure=structure, params=dict(old.params))
    draft.scoring = _replace_scoring(draft, tournament_structure=new_ts)
    return DraftPatch(
        op="set_structure",
        changed={"structure": {"from": old.structure, "to": structure}},
    )


#: Structure params whose value is a closed vocabulary rather than a number.
#: The params object is otherwise opaque to the data layer, but an unchecked
#: typo here would sail through the draft, roll the epoch on save, and only
#: surface as a ``ValueError`` from ``make_strategy`` at round start — costing
#: a round to learn about a misspelling. Validating at edit time makes it a
#: field-precise 400 instead (the same reasoning as ``_LADDER_TYPES`` below).
_PARAM_CHOICES: dict[str, tuple[str, ...]] = {"slice_schedule": SLICE_SCHEDULES}


@authored_edit
def set_param(draft: TournamentDraft, key: str, value: Any) -> DraftPatch:
    """Set one structure param (``field_size``, ``replicates``, …).

    The params object is opaque to the data layer (per-key semantics are
    the selection strategy's), so the value is stored verbatim — except for
    the closed-vocabulary keys in :data:`_PARAM_CHOICES`, which are checked
    against the strategy's accepted values. Setting a value of ``None``
    removes the key.
    """
    if value is not None and key in _PARAM_CHOICES:
        allowed = _PARAM_CHOICES[key]
        if value not in allowed:
            choices = ", ".join(repr(c) for c in allowed)
            raise ValueError(f"{key} must be one of {choices}, got {value!r}")
    old = draft.scoring.tournament_structure
    params = dict(old.params)
    prev = params.get(key)
    if value is None:
        params.pop(key, None)
    else:
        params[key] = value
    new_ts = TournamentStructure(structure=old.structure, params=params)
    draft.scoring = _replace_scoring(draft, tournament_structure=new_ts)
    return DraftPatch(
        op="set_param",
        changed={key: {"from": prev, "to": value}},
    )


@authored_edit
def set_holdout(
    draft: TournamentDraft,
    *,
    enabled: bool | None = None,
    fraction: float | None = None,
    tags: list[str] | None = None,
    min_board_size_for_split: int | None = None,
    rotate_holdout: bool | None = None,
    restrict_proposer_visibility: bool | None = None,
    ladder: dict[str, Any] | None = None,
) -> DraftPatch:
    """Edit the train/holdout split + the full anti-overfitting config.

    ``enabled`` / ``fraction`` tune the hash-derived split on
    :class:`OverfittingConfig`; ``tags`` sets the explicit per-entry
    ``holdout`` tag exactly on the supplied ids (every other entry loses
    the tag). The remaining keywords cover the rest of the overfitting
    contract: the split floor (``min_board_size_for_split``), per-epoch
    holdout rotation, and the proposer-visibility restriction.

    ``ladder`` is a PARTIAL mapping over the
    :class:`~zicato.core.scoring_config.LadderConfig` knobs (``enabled``
    / ``threshold`` / ``budget``) merged onto the
    current ladder — an explicit ``"threshold": null`` IN the mapping
    resets the release threshold to auto (derive from
    ``promote_margin``). Unknown ladder keys raise. Any subset of the
    keywords may be supplied; each change rolls the epoch like every
    contract edit (the dataclass validators re-check on replace).
    """
    changed: dict[str, Any] = {}
    of = draft.scoring.overfitting
    of_changes: dict[str, Any] = {}
    for name, value in (
        ("enabled", enabled),
        ("holdout_fraction", fraction),
        ("min_board_size_for_split", min_board_size_for_split),
        ("rotate_holdout", rotate_holdout),
        ("restrict_proposer_visibility", restrict_proposer_visibility),
    ):
        if value is not None and value != getattr(of, name):
            of_changes[name] = value
            changed[name] = {"from": getattr(of, name), "to": value}
    if ladder is not None:
        candidate = authored_dataclass_from_json(
            type(of.ladder), {**dataclasses.asdict(of.ladder), **ladder}, path="set_holdout.ladder"
        )
        for key in ladder:
            before, after = getattr(of.ladder, key), getattr(candidate, key)
            if before != after:
                changed[f"ladder.{key}"] = {"from": before, "to": after}
        if candidate != of.ladder:
            of_changes["ladder"] = candidate
    if of_changes:
        draft.scoring = _replace_scoring(draft, overfitting=dataclasses.replace(of, **of_changes))
    if tags is not None:
        before = sorted(e.id for e in draft.entries if HOLDOUT_TAG in e.tags)
        draft.set_holdout_tags(tags)
        after = sorted(e.id for e in draft.entries if HOLDOUT_TAG in e.tags)
        if before != after:
            changed["holdout_tags"] = {"from": before, "to": after}
    return DraftPatch(op="set_holdout", changed=changed)


def set_proposer(draft: TournamentDraft, proposer_path: str | Path | None) -> DraftPatch:
    """Point the draft at a proposer dir, or ``None`` for the builtin."""
    old = draft.proposer_path
    new = Path(proposer_path) if proposer_path is not None else None
    draft.proposer_path = new
    return DraftPatch(
        op="set_proposer",
        changed={
            "proposer_path": {
                "from": str(old) if old is not None else None,
                "to": str(new) if new is not None else None,
            }
        },
    )


@authored_edit
def set_weights(
    draft: TournamentDraft,
    *,
    pass_weight: float | None = None,
    per_kind_weights: dict[str, float] | None = None,
    per_judge_weights: dict[str, float] | None = None,
    default_judge_weight: float | None = None,
    plan_revision_weight: float | None = None,
    task_failure_weight: float | None = None,
    not_completed_weight: float | None = None,
    severity_weights: dict[str, float] | None = None,
) -> DraftPatch:
    """Set scoring weights (the loss-shaping knobs).

    Any subset of the supported weight fields may be supplied. Mapping
    fields replace the whole mapping.
    The per-CHANNEL coefficients — including ``drift:``, ``judge:``,
    ``failure:`` and ``runtime:`` — are :func:`set_namespace_weights`; the
    fields here shape a channel from within it.
    """
    changed: dict[str, Any] = {}
    scoring_changes: dict[str, Any] = {}
    for name, value in (
        ("pass_weight", pass_weight),
        ("default_judge_weight", default_judge_weight),
        ("plan_revision_weight", plan_revision_weight),
        ("task_failure_weight", task_failure_weight),
        ("not_completed_weight", not_completed_weight),
    ):
        if value is not None and value != getattr(draft.scoring, name):
            scoring_changes[name] = value
            changed[name] = {"from": getattr(draft.scoring, name), "to": value}
    for name, mapping in (
        ("per_kind_weights", per_kind_weights),
        ("per_judge_weights", per_judge_weights),
        ("severity_weights", severity_weights),
    ):
        if mapping is not None:
            normalized = {str(k): float(v) for k, v in mapping.items()}
            if normalized != dict(getattr(draft.scoring, name)):
                scoring_changes[name] = normalized
                changed[name] = {
                    "from": dict(getattr(draft.scoring, name)),
                    "to": normalized,
                }
    if scoring_changes:
        draft.scoring = _replace_scoring(draft, **scoring_changes)
    return DraftPatch(op="set_weights", changed=changed)


@authored_edit
def set_gate(
    draft: TournamentDraft,
    *,
    promote_margin: float | None = None,
    holdout_margin: float | None = None,
    holdout_entry_regression_budget: int | None = None,
    monotonicity: bool | None = None,
    monotonicity_scope: str | None = None,
    namespace_monotonicity: dict[str, bool] | None = None,
    block_on_containment_violation: bool | None = None,
    block_on_gate_contradiction: bool | None = None,
    regression_gate_enabled: bool | None = None,
    regression_test_command: list[str] | None = None,
    regression_timeout_s: int | None = None,
) -> DraftPatch:
    """Set the promote gate: margin, monotonicity, and the hard blocks.

    ``promote_margin`` is the improvement a challenger must show before the
    gate promotes it; it may not be negative, which would turn the rule into
    one that promotes a regression.

    ``monotonicity`` is the on/off switch; ``monotonicity_scope`` selects
    the granularity when it is on (``"per_entry"`` — default, every
    champion-passed entry must hold — or ``"aggregate"`` — only the overall
    pass-rate may not regress; see SCORING.md §5). An invalid scope token
    raises rather than silently coercing.

    ``holdout_margin`` and ``holdout_entry_regression_budget`` are the
    holdout CONFIRMATION's own bounds (issue #118), separate from the
    train-side ``promote_margin`` because the holdout is the coarser slice
    by construction. ASYMMETRY, like ``max_generations_per_contract`` on
    :func:`set_holdout`: ``None`` here means "leave unchanged", so a
    NEGATIVE ``holdout_margin`` is the token that RESETS the field to auto
    (``None`` — reuse ``promote_margin``); a non-negative value pins it.

    The remaining keywords cover the rest of the gate contract:
    ``namespace_monotonicity`` replaces the per-namespace strict-
    monotonicity flag mapping, like :func:`set_weights`; the two ``block_on_*`` booleans
    opt into the integrity BLOCKING modes (containment / gate-
    contradiction — both alarm-only by default); the ``regression_*``
    trio configures the snapshot's own test suite as a hard pre-gate
    (``regression_test_command`` is the argv list; ``regression_timeout_s``
    must be >= 1).
    """
    changed: dict[str, Any] = {}
    scoring_changes: dict[str, Any] = {}
    if promote_margin is not None:
        require_knob(ScoringWeights, "promote_margin", promote_margin)
        if promote_margin != draft.scoring.promote_margin:
            scoring_changes["promote_margin"] = promote_margin
            changed["promote_margin"] = {
                "from": draft.scoring.promote_margin,
                "to": promote_margin,
            }
    if holdout_margin is not None:
        # Negative CLEARS to auto (the field's meaningful "off" is None,
        # which this op reserves for "leave unchanged"); the dataclass
        # rejects a negative outright, so no valid pin is shadowed.
        effective = None if holdout_margin < 0 else holdout_margin
        if effective != draft.scoring.holdout_margin:
            scoring_changes["holdout_margin"] = effective
            changed["holdout_margin"] = {
                "from": draft.scoring.holdout_margin,
                "to": effective,
            }
    if holdout_entry_regression_budget is not None:
        require_knob(
            ScoringWeights, "holdout_entry_regression_budget", holdout_entry_regression_budget
        )
        if holdout_entry_regression_budget != draft.scoring.holdout_entry_regression_budget:
            scoring_changes["holdout_entry_regression_budget"] = holdout_entry_regression_budget
            changed["holdout_entry_regression_budget"] = {
                "from": draft.scoring.holdout_entry_regression_budget,
                "to": holdout_entry_regression_budget,
            }
    if monotonicity is not None and monotonicity != draft.scoring.pass_rate_monotonicity:
        scoring_changes["pass_rate_monotonicity"] = monotonicity
        changed["pass_rate_monotonicity"] = {
            "from": draft.scoring.pass_rate_monotonicity,
            "to": monotonicity,
        }
    if monotonicity_scope is not None:
        require_knob(ScoringWeights, "pass_rate_monotonicity_scope", monotonicity_scope)
        if monotonicity_scope != draft.scoring.pass_rate_monotonicity_scope:
            scoring_changes["pass_rate_monotonicity_scope"] = monotonicity_scope
            changed["pass_rate_monotonicity_scope"] = {
                "from": draft.scoring.pass_rate_monotonicity_scope,
                "to": monotonicity_scope,
            }
    for name, value in (
        ("block_on_containment_violation", block_on_containment_violation),
        ("block_on_gate_contradiction", block_on_gate_contradiction),
        ("regression_gate_enabled", regression_gate_enabled),
    ):
        if value is not None and value != getattr(draft.scoring, name):
            scoring_changes[name] = value
            changed[name] = {"from": getattr(draft.scoring, name), "to": value}
    if namespace_monotonicity is not None:
        normalized_ns = {str(k): bool(v) for k, v in namespace_monotonicity.items()}
        if normalized_ns != dict(draft.scoring.namespace_monotonicity):
            scoring_changes["namespace_monotonicity"] = normalized_ns
            changed["namespace_monotonicity"] = {
                "from": dict(draft.scoring.namespace_monotonicity),
                "to": normalized_ns,
            }
    if regression_test_command is not None:
        command = tuple(str(part) for part in regression_test_command)
        if not command:
            raise ValueError("regression_test_command must be a non-empty argv list")
        if command != draft.scoring.regression_test_command:
            scoring_changes["regression_test_command"] = command
            changed["regression_test_command"] = {
                "from": list(draft.scoring.regression_test_command),
                "to": list(command),
            }
    if regression_timeout_s is not None:
        require_knob(ScoringWeights, "regression_timeout_s", regression_timeout_s)
        if regression_timeout_s != draft.scoring.regression_timeout_s:
            scoring_changes["regression_timeout_s"] = regression_timeout_s
            changed["regression_timeout_s"] = {
                "from": draft.scoring.regression_timeout_s,
                "to": regression_timeout_s,
            }
    if scoring_changes:
        draft.scoring = _replace_scoring(draft, **scoring_changes)
    return DraftPatch(op="set_gate", changed=changed)


@authored_edit
def set_namespace_weights(
    draft: TournamentDraft,
    *,
    namespace_weights: dict[str, float] | None = None,
) -> DraftPatch:
    """Replace namespace coefficients; positive values make higher loss worse."""
    changed: dict[str, Any] = {}
    if namespace_weights is not None:
        normalized = {str(key): float(value) for key, value in namespace_weights.items()}
        if normalized != dict(draft.scoring.namespace_weights):
            changed["namespace_weights"] = {
                "from": dict(draft.scoring.namespace_weights),
                "to": normalized,
            }
            draft.scoring = _replace_scoring(draft, namespace_weights=normalized)
    return DraftPatch(op="set_namespace_weights", changed=changed)


@authored_edit
def set_proposer_quality(
    draft: TournamentDraft,
    *,
    best_of_n: int | None = None,
    critique_enabled: bool | None = None,
) -> DraftPatch:
    """Set candidate count and critique; screening has its own operation.

    A single candidate bypasses critique. Candidate count must be positive.
    """
    quality = draft.scoring.proposer_quality
    changes: dict[str, Any] = {}
    if best_of_n is not None:
        require_knob(ProposerQualityConfig, "best_of_n", best_of_n)
        if best_of_n != quality.best_of_n:
            changes["best_of_n"] = best_of_n
    if critique_enabled is not None and critique_enabled != quality.critique_enabled:
        changes["critique_enabled"] = critique_enabled
    if changes:
        draft.scoring = _replace_scoring(
            draft, proposer_quality=dataclasses.replace(quality, **changes)
        )
    return DraftPatch(
        op="set_proposer_quality",
        changed={
            name: {"from": getattr(quality, name), "to": value} for name, value in changes.items()
        },
    )


@authored_edit
def set_experimental(
    draft: TournamentDraft,
    *,
    tournament_structures: bool | None = None,
    process_exemplars: int | None = None,
    recombine: bool | None = None,
    recombine_merge: str | None = None,
    genealogy: int | None = None,
    calibration_feedback: int | None = None,
    random_baseline_every_n: int | None = None,
    max_generations_per_contract: int | None = None,
    diff_complexity_weight: float | None = None,
    diff_complexity_ceiling: float | None = None,
    cross_epoch_memory: bool | None = None,
    standing_rating: str | None = None,
    resolver: str | None = None,
) -> DraftPatch:
    """Edit features whose improvement evidence has not met graduation criteria.

    Omitted values leave settings unchanged. A generation ceiling of zero
    clears it; the rating and resolver value ``"none"`` disables that feature.
    Disabling tournament structures requires an ordinary selected structure.
    All supplied values are validated before replacing the draft's scoring.
    """
    supplied = locals()
    experimental = draft.scoring.experimental
    changes = {
        item.name: supplied[item.name]
        for item in dataclasses.fields(experimental)
        if supplied[item.name] is not None
    }
    if changes.get("max_generations_per_contract") == 0:
        changes["max_generations_per_contract"] = None
    changes = {
        name: value for name, value in changes.items() if value != getattr(experimental, name)
    }
    if changes:
        draft.scoring = _replace_scoring(
            draft, experimental=dataclasses.replace(experimental, **changes)
        )
    return DraftPatch(
        op="set_experimental",
        changed={
            name: {"from": getattr(experimental, name), "to": value}
            for name, value in changes.items()
        },
    )


@authored_edit
def set_goldfive(
    draft: TournamentDraft,
    *,
    config: Mapping[str, Any] | None,
) -> DraftPatch:
    """Activate, partially update, or remove the optional Goldfive block."""
    current = draft.scoring.goldfive
    previous = draft.scoring.to_json().get("goldfive")
    if config is None:
        updated = None
    else:
        from zicato.integrations.goldfive import normalize_config  # noqa: PLC0415

        def merge(base: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
            result = dict(base)
            for key, value in changes.items():
                result[key] = (
                    merge(result[key], value)
                    if isinstance(value, Mapping) and isinstance(result.get(key), Mapping)
                    else value
                )
            return result

        try:
            updated = normalize_config(merge(current or {}, config))
        except ImportError:
            raise ValueError(
                "Goldfive configuration requires the optional zicato[goldfive] installation"
            ) from None
    changed: dict[str, Any] = {}
    if updated != previous:
        draft.scoring = _replace_scoring(draft, goldfive=updated)
        changed["goldfive"] = {
            "from": previous,
            "to": updated,
        }
    return DraftPatch(op="set_goldfive", changed=changed)


@authored_edit
def set_telemetry_dialect(
    draft: TournamentDraft,
    *,
    dialect: str | None = None,
) -> DraftPatch:
    """Set the telemetry dialect — the PRODUCER that reduces a run's raw
    telemetry into the ``LossProfile`` inputs (TELEMETRY-DIALECTS.md).

    ``"goldfive"`` (the default, most powerful) consumes the full
    drift-instrument stream; ``"adk_events"`` reduces a generic agent
    event-log JSONL (no in-process drift instruments, no custom
    process-judge drift); ``"transcript"`` is the predicate/judge-only floor
    with a structurally zero drift term. The dialect is part of the
    evaluation contract — changing it selects champions under a different
    measurement rule, so it rolls the epoch like any scoring change. It is
    omitted from the contract canonical form at its ``"goldfive"`` default,
    so setting a non-default dialect rolls and reverting to ``"goldfive"``
    rolls back to the original hash. ``None`` leaves it unchanged; an unknown
    name raises :class:`ValueError` (the closed dialect set validated the
    same way the ``ScoringWeights`` contract-load check validates it — never
    a second hardcoded list).
    """
    changed: dict[str, Any] = {}
    if dialect is not None:
        require_knob(ScoringWeights, "telemetry_dialect", dialect)
        current = draft.scoring.telemetry_dialect
        if dialect != current:
            draft.scoring = _replace_scoring(draft, telemetry_dialect=dialect)
            changed["telemetry_dialect"] = {"from": current, "to": dialect}
    return DraftPatch(op="set_telemetry_dialect", changed=changed)


@authored_edit
def set_mutation_surface(
    draft: TournamentDraft,
    *,
    mutation_surface: Mapping[str, Any] | None = None,
) -> DraftPatch:
    """Declare which file types carry mutation sites (MUTATION-SURFACE.md §2.5).

    ``mutation_surface`` is the whole table, keyed by suffix:
    ``{".ts": {"leaders": ["//", "/*"], "trailers": ["*/"]}}``. It folds
    over the built-in syntaxes (markdown / YAML / TOML / text, and the
    reserved ``.py``), so an empty table is "the built-ins alone". The
    leaders are what lets the applier strip an echoed marker line out of a
    region body, which is why declaring one is required and why ``.py`` —
    whose grammar the built-in Python pass depends on — cannot be
    redeclared.

    The table decides what the proposer may rewrite, so it is contract:
    declaring a file type rolls the epoch (and clearing it back to empty
    rolls back to the original hash). ``None`` leaves it unchanged; a
    malformed table raises :class:`ValueError` from the same validator the
    run path installs through — never a second copy of the rules.
    """
    from zicato.mutation.markers import syntax_table_from_config  # noqa: PLC0415

    changed: dict[str, Any] = {}
    if mutation_surface is not None:
        table = {
            str(k): dict(v) if isinstance(v, Mapping) else v for k, v in mutation_surface.items()
        }
        syntax_table_from_config(table)
        current = dict(draft.scoring.mutation_surface)
        if table != current:
            draft.scoring = _replace_scoring(draft, mutation_surface=table)
            changed["mutation_surface"] = {"from": current, "to": table}
    return DraftPatch(op="set_mutation_surface", changed=changed)


@authored_edit
def set_screening(
    draft: TournamentDraft,
    *,
    entries: int | None = None,
    veto_only: bool | None = None,
) -> DraftPatch:
    """Set the pre-tournament candidate screen (tryouts).

    ``entries`` is the rotating train-panel size each best-of-N slate
    candidate runs before selection (``0`` turns the screen OFF — the
    code default; the scaffold enables ``2``); ``veto_only`` restricts
    the screen's measurements to the veto (no selection tiebreak feeds).
    Both live on the nested ``proposer_quality`` contract block, so a
    change rolls the epoch like any other weight. A negative ``entries``
    is refused by the bound the ``screen_entries`` field declares, with the
    message the contract loader would give.
    """
    changed: dict[str, Any] = {}
    quality = draft.scoring.proposer_quality
    quality_changes: dict[str, Any] = {}
    if entries is not None:
        require_knob(ProposerQualityConfig, "screen_entries", entries)
        if entries != quality.screen_entries:
            quality_changes["screen_entries"] = entries
            changed["screen_entries"] = {"from": quality.screen_entries, "to": entries}
    if veto_only is not None and veto_only != quality.screen_veto_only:
        quality_changes["screen_veto_only"] = veto_only
        changed["screen_veto_only"] = {"from": quality.screen_veto_only, "to": veto_only}
    if quality_changes:
        draft.scoring = _replace_scoring(
            draft, proposer_quality=dataclasses.replace(quality, **quality_changes)
        )
    return DraftPatch(op="set_screening", changed=changed)


def edit_board_entry(draft: TournamentDraft, entry: BoardEntry) -> DraftPatch:
    """Add or replace a board entry (matched by id).

    The entry is validated before it lands (so a malformed edit raises
    :class:`ValueError` rather than corrupting the draft). An id already
    on the board is replaced in place; a new id is appended.
    """
    entry.validate()
    existing_index = next((i for i, e in enumerate(draft.entries) if e.id == entry.id), None)
    if existing_index is None:
        draft.entries.append(entry)
        action = "added"
    else:
        draft.entries[existing_index] = entry
        action = "replaced"
    return DraftPatch(
        op="edit_board_entry",
        changed={"entry_id": entry.id, "action": action},
    )


def add_board_entry(draft: TournamentDraft, entry: BoardEntry) -> DraftPatch:
    """Append a NEW board entry — the add beside :func:`add_judge`.

    Where :func:`edit_board_entry` is add-OR-replace (id-matched), this is a
    strict ADD: it mirrors :func:`add_judge`'s validate-then-append shape and
    REFUSES a duplicate id (a silent replace would hide a suggestion colliding
    with a live entry). The entry is validated before it lands (a malformed
    draft raises :class:`ValueError` rather than corrupting the board). Any
    provenance the author stamped onto ``entry.context`` (EVAL-SYNTHESIS.md §4)
    rides along untouched — the op neither injects nor strips it.

    Publishing the board change causes the next evaluation to use a fresh epoch.
    """
    entry.validate()
    if any(e.id == entry.id for e in draft.entries):
        raise ValueError(
            f"board entry {entry.id!r} already exists — use edit_board_entry to replace it"
        )
    draft.entries.append(entry)
    return DraftPatch(
        op="add_board_entry",
        changed={"entry_id": entry.id, "action": "added"},
    )


def remove_board_entry(draft: TournamentDraft, entry_id: str) -> DraftPatch:
    """Remove the board entry with ``entry_id`` — the delete beside
    :func:`edit_board_entry`'s add/replace.

    Raises :class:`ValueError` on an unknown id (a delete that silently
    no-ops would hide a typo). Mutates the entries list in place; a board
    change, so it rolls the epoch like any board edit.
    """
    index = next((i for i, e in enumerate(draft.entries) if e.id == entry_id), None)
    if index is None:
        raise ValueError(f"no board entry with id {entry_id!r}")
    del draft.entries[index]
    return DraftPatch(
        op="remove_board_entry",
        changed={"entry_id": entry_id, "action": "removed"},
    )


def add_judge(draft: TournamentDraft, entry_id: str, judge: JudgeSpec) -> DraftPatch:
    """Add a process judge to a board entry.

    Raises :class:`ValueError` when ``entry_id`` is unknown or a judge of
    the same name already exists on the entry (the entry's own
    ``validate`` enforces unique judge names).
    """
    entry = draft.entry_by_id(entry_id)
    if entry is None:
        raise ValueError(f"no board entry with id {entry_id!r}")
    if any(j.name == judge.name for j in entry.judges):
        raise ValueError(f"board entry {entry_id!r} already has a judge named {judge.name!r}")
    updated = dataclasses.replace(entry, judges=(*entry.judges, judge))
    updated.validate()
    _replace_entry(draft, updated)
    return DraftPatch(
        op="add_judge",
        changed={"entry_id": entry_id, "judge": judge.name},
    )


def remove_judge(draft: TournamentDraft, entry_id: str, name: str) -> DraftPatch:
    """Remove the named process judge from a board entry.

    Raises :class:`ValueError` when ``entry_id`` is unknown. Removing a
    judge name the entry does not carry is a no-op (reported in the patch
    note).
    """
    entry = draft.entry_by_id(entry_id)
    if entry is None:
        raise ValueError(f"no board entry with id {entry_id!r}")
    kept = tuple(j for j in entry.judges if j.name != name)
    if len(kept) == len(entry.judges):
        return DraftPatch(
            op="remove_judge",
            changed={"entry_id": entry_id, "judge": name},
            note=f"entry {entry_id!r} had no judge named {name!r}",
        )
    updated = dataclasses.replace(entry, judges=kept)
    _replace_entry(draft, updated)
    return DraftPatch(
        op="remove_judge",
        changed={"entry_id": entry_id, "judge": name},
    )


def set_brief(draft: TournamentDraft, text: str) -> DraftPatch:
    """Replace the proposer-brief text."""
    old_len = len(draft.brief)
    draft.brief = text
    return DraftPatch(
        op="set_brief",
        changed={"brief_chars": {"from": old_len, "to": len(text)}},
    )


def set_board_meta(
    draft: TournamentDraft,
    *,
    disable_drift: list[str] | None = None,
    judge_only: bool | None = None,
) -> DraftPatch:
    """Set the board-level ``board_meta`` header (drift suppression + judge-only).

    ``disable_drift`` replaces the whole suppression set wholesale (the
    caller supplies the complete set, like :func:`set_weights`);
    each token is validated against the registered drift-kind set
    (:func:`zicato.core.drift_kinds.validate_drift_kind`) and an unknown
    token raises :class:`ValueError` listing the offender. ``judge_only``
    toggles the board-level no-steering evaluation flag. ``None`` for
    either means "leave unchanged"; an empty list is a REAL value (clear
    the suppression set).

    The header folds into the board's contract-hash canon, so a change
    here rolls the epoch like any board edit. The header is written back
    by ``apply`` only when non-default, byte-compatible with
    :func:`zicato.board.jsonl.save_board`.

    """
    from zicato.core.drift_kinds import DriftKind, validate_drift_kind  # noqa: PLC0415

    changed: dict[str, Any] = {}
    if disable_drift is not None:
        kinds: list[DriftKind] = []
        for token in disable_drift:
            text = str(token)
            validate_drift_kind(text)
            kinds.append(DriftKind(text))
        new_set = tuple(kinds)
        if new_set != tuple(draft.disable_drift):
            changed["disable_drift"] = {
                "from": [str(getattr(k, "value", k)) for k in draft.disable_drift],
                "to": [k.value for k in new_set],
            }
            draft.disable_drift = new_set
    if judge_only is not None and judge_only != draft.judge_only:
        changed["judge_only"] = {"from": draft.judge_only, "to": judge_only}
        draft.judge_only = judge_only
    return DraftPatch(op="set_board_meta", changed=changed)


def _replace_entry(draft: TournamentDraft, updated: BoardEntry) -> None:
    """Replace the entry with ``updated.id`` in place."""
    for i, e in enumerate(draft.entries):
        if e.id == updated.id:
            draft.entries[i] = updated
            return
    draft.entries.append(updated)


# ---------------------------------------------------------------------------
# Read-side: cost + validate
# ---------------------------------------------------------------------------


def estimate_cost(draft: TournamentDraft) -> CostEstimate:
    """Estimate board-runs-per-round for the draft's structure + params.

    The estimate accounts for the scheduled evaluations:

    * ``gauntlet`` — ``field_size × replicates`` duel runs (one duel per
      challenger), counted across the train board.
    * ``single_elim`` / ``double_elim`` — a bracket of ``field_size``
      challengers: ``(field_size − 1)`` win-bracket matches for single
      elim, roughly ``2 × (field_size − 1)`` for double, each ``×
      replicates``, across the train board.
    * ``swiss`` — ``rounds_n × pairings × replicates``, where ``pairings``
      is ``field_size // 2`` per round.
    * ``racing`` — the sum of each rung's surviving-field size (successive
      halving by ``eta`` over a board slice that grows by ``eta`` each
      rung), each ``× replicates``, plus the final full-board duel.

    Every structure adds a ``holdout_confirm`` term: the winning
    challenger is re-scored on the ``holdout`` slice.

    HONEST-METER terms beyond the base schedule (each only when the
    contract opts in):

    * ``candidate-screen runs`` — the pre-tournament tryout panel
      (``proposes × best_of_n × panel``).
    * ``best-of-N propose calls`` — ``proposes × best_of_n`` EVALUATION
      LLM calls per round (the slate SAMPLING, which runs on the
      ensemble proposer's breadth role; the critique / revise DEPTH calls
      run on the depth role and are not separately metered). These are
      evaluation calls rather than board runs, so the line is labelled
      evaluation and EXCLUDED from the board-runs headline. It is still
      real money and belongs on the meter.
    * ``crowning-confirm runs`` — the evidence gate's defer→replicate
      budget: each replicate is a FRESH board sweep for BOTH crowning
      contestants, so ``budget × 2 × board``. Spent per CONFIRMED
      crowning (an upper bound per round); with the scaffold's
      32-replicate budget this is typically the LARGEST term.
    * ``placebo-baseline runs`` — the ``random_baseline_every_n``
      control arm: one extra no-op challenger every N rounds, amortized
      to ``ceil(replicates × board / N)`` per round.

    The estimate is a coarse upper-ish bound for the
    cost-meter — the exact schedule is the selection strategy's; this
    surfaces the order of magnitude before the operator commits.
    """
    ts = draft.scoring.tournament_structure
    params = ts.params
    structure = ts.structure
    train_ids, holdout_ids = split_board(draft.entries, draft.scoring.overfitting)
    board_size = len(train_ids)
    holdout_size = len(holdout_ids)
    # ``replicates`` defaults to the STRUCTURE's own default (swiss / elim
    # default to 2 — replication rather than bracket shape is their noise lever),
    # NOT a flat 1. The default is read from the selection layer's
    # single source of truth (each strategy's ``_default_replicates``), so the
    # meter cannot under-report the schedule a structure actually runs. An
    # EXPLICIT ``replicates`` in params is honored verbatim.
    replicates = max(1, _param_int(params, "replicates", default_replicates_for(structure)))
    field_size = max(1, _param_int(params, "field_size", 2))

    lines: list[CostLine] = []

    if structure == "gauntlet" or field_size <= 1:
        duels = field_size
        per_round = duels * replicates * board_size
        lines.append(
            CostLine(
                "duel runs",
                per_round,
                f"field_size {field_size} × replicates {replicates} × board {board_size}",
            )
        )
    elif structure in ("single_elim", "double_elim"):
        matches = max(0, field_size - 1)
        if structure == "double_elim":
            matches = max(0, 2 * (field_size - 1))
        per_round = matches * replicates * board_size
        lines.append(
            CostLine(
                "bracket-match runs",
                per_round,
                f"{matches} matches × replicates {replicates} × board {board_size}",
            )
        )
    elif structure == "swiss":
        rounds_n = max(1, _param_int(params, "rounds_n", 4))
        pairings = max(1, field_size // 2)
        per_round = rounds_n * pairings * replicates * board_size
        lines.append(
            CostLine(
                "swiss-pairing runs",
                per_round,
                f"rounds_n {rounds_n} × pairings {pairings} × replicates {replicates} "
                f"× board {board_size}",
            )
        )
    elif structure == "racing":
        per_round, racing_lines = _racing_cost(
            params,
            field_size=field_size,
            replicates=replicates,
            board_size=board_size,
        )
        lines.extend(racing_lines)
    else:  # pragma: no cover — structure validated upstream
        per_round = field_size * replicates * board_size
        lines.append(CostLine("duel runs", per_round, "fallback"))

    holdout_confirm = holdout_size * replicates
    if holdout_confirm:
        lines.append(
            CostLine(
                "holdout-confirm runs",
                holdout_confirm,
                f"holdout {holdout_size} × replicates {replicates}",
            )
        )
        per_round += holdout_confirm

    # Pre-tournament candidate screening (tryouts): when the contract opts
    # in (screen_entries > 0 with a best-of-N slate), each propose-step's
    # candidates run a small train panel before selection — the gauntlet
    # proposes once per round, a wider structure proposes field_size
    # challengers. The panel can never exceed the train board.
    quality = draft.scoring.proposer_quality
    proposes = 1 if (structure == "gauntlet" or field_size <= 1) else field_size
    if quality.screen_entries > 0 and quality.best_of_n > 1:
        panel = min(quality.screen_entries, board_size)
        screen_runs = proposes * quality.best_of_n * panel
        if screen_runs:
            lines.append(
                CostLine(
                    "candidate-screen runs",
                    screen_runs,
                    f"proposes {proposes} × best_of_n {quality.best_of_n} × panel {panel}",
                )
            )
            per_round += screen_runs

    # Best-of-N propose multiplier: each propose-step samples best_of_n
    # candidate experiments — evaluation LLM CALLS rather than board runs, so the
    # line is labelled and EXCLUDED from the board-runs headline. Real
    # spend the operator should still see priced. An UPPER BOUND under the
    # recombination slot (experimental.recombine): a round that mints
    # a recombination pair REPLACES its last slot's propose call with the
    # free mechanical mint, spending best_of_n − 1 calls that round.
    if quality.best_of_n > 1:
        propose_calls = proposes * quality.best_of_n
        lines.append(
            CostLine(
                "best-of-N propose calls",
                propose_calls,
                f"proposes {proposes} × best_of_n {quality.best_of_n} — evaluation "
                "LLM calls on the proposer-breadth role (sampling); critique / "
                "revise run on proposer-depth. Not board runs (excluded from the "
                "headline)",
            )
        )

    # The evidence gate's crowning-confirm budget: when the contract sets
    # promote_confidence_threshold, the defer→replicate loop may spend up
    # to `promote_confidence_replicates` FRESH board sweeps for BOTH
    # crowning contestants chasing CI separation — budget × 2 × board.
    # Spent per CONFIRMED crowning (so per-round it is an upper bound);
    # with the recommended scaffold's 32-replicate budget this is
    # typically the LARGEST term on the meter.
    from zicato.selection.evidence_gate import (  # noqa: PLC0415
        read_promote_confidence_threshold,
        read_replicate_budget,
    )

    if read_promote_confidence_threshold(params) is not None:
        budget = read_replicate_budget(params)
        confirm_runs = budget * 2 * board_size
        if confirm_runs:
            lines.append(
                CostLine(
                    "crowning-confirm runs (evidence gate)",
                    confirm_runs,
                    f"budget {budget} × 2 contestants × board {board_size} — per "
                    "confirmed crowning (upper bound)",
                )
            )
            per_round += confirm_runs

    # The placebo control arm: one extra no-op challenger every N rounds
    # (a full duel across the train board), amortized to per-round runs.
    baseline_n = draft.scoring.experimental.random_baseline_every_n
    if baseline_n > 0:
        placebo_runs = math.ceil(replicates * board_size / baseline_n)
        if placebo_runs:
            lines.append(
                CostLine(
                    "placebo-baseline runs (amortized)",
                    placebo_runs,
                    f"1 no-op challenger every {baseline_n} rounds × replicates "
                    f"{replicates} × board {board_size}",
                )
            )
            per_round += placebo_runs

    return CostEstimate(
        structure=structure,
        board_size=board_size,
        holdout_size=holdout_size,
        board_runs_per_round=per_round,
        breakdown=tuple(lines),
    )


def _racing_cost(
    params: Any,
    *,
    field_size: int,
    replicates: int,
    board_size: int,
) -> tuple[int, list[CostLine]]:
    """Successive-halving rung sum + the final full-board champion duel.

    Mirrors :class:`zicato.selection.strategies.racing.RacingStrategy`'s
    board-slice growth: rung ``r`` scores the surviving field on a slice
    of size ``ceil(board_fraction · |board|) · eta**r`` (capped at the
    full board), and the field is halved by ``eta`` each rung. A final
    full-board duel confirms the survivor against the champion.
    """
    eta = max(2, _param_int(params, "eta", 2))
    board_fraction = _param_float(params, "board_fraction", 0.25)
    rung0 = _param_int(params, "rung0_board_size", 0)
    base_slice = rung0 if rung0 > 0 else max(1, math.ceil(board_size * board_fraction))

    lines: list[CostLine] = []
    alive = max(1, field_size)
    rung = 0
    total = 0
    # Guard against a pathological field that never shrinks.
    while alive > 1 and rung < 32:
        slice_size = min(board_size, base_slice * (eta**rung))
        rung_runs = alive * replicates * slice_size
        total += rung_runs
        lines.append(
            CostLine(
                f"rung {rung} runs",
                rung_runs,
                f"alive {alive} × replicates {replicates} × slice {slice_size}",
            )
        )
        if slice_size >= board_size:
            break
        alive = max(1, alive // eta)
        rung += 1
    final_runs = replicates * board_size
    total += final_runs
    lines.append(
        CostLine(
            "racing-final runs",
            final_runs,
            f"full board {board_size} × replicates {replicates}",
        )
    )
    return total, lines


def validate(
    draft: TournamentDraft,
    workspace_root: Path | None = None,
    *,
    noise_floor_max_abs_delta: float | None = None,
) -> list[Warning]:
    """Return advisory warnings about the draft (never blocking).

    Checks include:

    * ``field_size == 1`` degrades a field structure to a gauntlet.
    * a board smaller than ``min_board_size_for_split`` (with no explicit
      ``holdout`` tag) disables the hash-derived holdout.
    * for ``racing``, the rung-0 slice size = ``ceil(board_fraction ·
      |board|)`` — surfaced so the operator sees how thin the first rung
      is.
    * ``replicates < 2`` is risky for a bracket structure (a single noisy
      run can flip a match verdict).
    * an explicit ``holdout`` tag referencing no entry, or every entry.
    * BOARD-AUTHORING checks (all recommend-only):
      ``duplicate_entry_id`` (refuse — ``apply`` would fail:
      :func:`~zicato.board.jsonl.save_board` rejects duplicate ids);
      ``entry_id_unsafe`` (ids become run directory names);
      ``dotted_path_malformed`` (predicate specs + python judge bodies);
      ``rubric_spec_invalid`` / ``json_schema_spec_invalid`` (the two
      JSON-document expectation specs); ``entry_budget_outlier`` (info —
      a wall-clock budget more than 10× the board median);
      ``judge_only_board`` (info — the board_meta judge-only flag).
    * STATISTICAL: when a measured A/A noise floor is known — passed in
      explicitly (``noise_floor_max_abs_delta``) or read off the current
      epoch's record under ``workspace_root`` — a ``promote_margin`` at
      or below that floor WITH the evidence gate off
      (``promote_confidence_threshold`` unset) is flagged at ``refuse``
      severity: every duel decided by the margin alone would be decided
      by noise. Recommend-only, like every warning here — apply is never
      hard-blocked.

    Dotted-path checks validate syntax without importing modules: resolving
    a module executes parent-package code. ``zicato board audit`` exercises
    the paths in the workspace's runtime context.
    """
    warnings: list[Warning] = []
    ts = draft.scoring.tournament_structure
    structure = ts.structure
    params = ts.params
    field_size = max(1, _param_int(params, "field_size", 2))
    replicates = max(1, _param_int(params, "replicates", 1))

    if structure != "gauntlet" and field_size == 1:
        warnings.append(
            Warning(
                "field_size_degrades_to_gauntlet",
                f"structure {structure!r} with field_size=1 degrades to a single "
                "champion-vs-challenger duel (a gauntlet).",
            )
        )

    of = draft.scoring.overfitting
    tagged = [e for e in draft.entries if HOLDOUT_TAG in e.tags]
    if (
        draft.entries
        and not tagged
        and of.enabled
        and len(draft.entries) < of.min_board_size_for_split
    ):
        warnings.append(
            Warning(
                "holdout_disabled_small_board",
                f"board has {len(draft.entries)} entries, below "
                f"min_board_size_for_split={of.min_board_size_for_split}; the "
                "hash-derived holdout is disabled (no entry held out).",
                severity="info",
            )
        )

    if structure == "racing" and draft.entries:
        board_fraction = _param_float(params, "board_fraction", 0.25)
        rung0 = _param_int(params, "rung0_board_size", 0)
        train_ids, _ = split_board(draft.entries, of)
        slice_size = rung0 if rung0 > 0 else max(1, math.ceil(len(train_ids) * board_fraction))
        warnings.append(
            Warning(
                "racing_rung0_slice",
                f"racing rung-0 slice = {slice_size} entries "
                f"(ceil(board_fraction {board_fraction} × board {len(train_ids)})).",
                severity="info",
            )
        )

    if structure in ("single_elim", "double_elim", "swiss") and replicates < 2:
        warnings.append(
            Warning(
                "replicates_recommended_for_brackets",
                f"structure {structure!r} with replicates={replicates}: a single "
                "noisy run can flip a match verdict; replicates>=2 is recommended.",
            )
        )

    if draft.entries and len(tagged) == len(draft.entries):
        warnings.append(
            Warning(
                "holdout_tags_cover_whole_board",
                "every board entry is tagged 'holdout' — no train entries remain; "
                "the split degrades to an empty holdout.",
            )
        )

    warnings.extend(_board_authoring_warnings(draft))

    floor = noise_floor_max_abs_delta
    if floor is None and workspace_root is not None:
        floor = _measured_noise_floor(workspace_root)
    if floor is not None:
        from zicato.selection.evidence_gate import (  # noqa: PLC0415
            read_promote_confidence_threshold,
        )

        gate_on = read_promote_confidence_threshold(params) is not None
        margin = draft.scoring.promote_margin
        if not gate_on and margin <= floor:
            warnings.append(
                Warning(
                    "margin_below_noise_floor",
                    f"promote_margin {margin:.6g} does not clear the measured A/A "
                    f"noise floor {floor:.6g} and the evidence gate "
                    "(promote_confidence_threshold) is off: a duel decided by the "
                    "margin alone cannot distinguish a real improvement from a "
                    "re-roll of the same tree. Raise promote_margin above the "
                    "floor or enable the evidence gate. Recommend-only — apply "
                    "is not blocked.",
                    severity="refuse",
                )
            )

    return warnings


#: Filesystem-safe entry ids — an id becomes a run directory name under
#: ``runs/`` in the workspace.
_SAFE_ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: The SHAPE of a dotted import path (``pkg.module.attr`` or
#: ``pkg.module:attr`` — the two forms :func:`zicato.import_path.
#: import_dotted_path` accepts). Shape only: validate never resolves it.
_DOTTED_PATH_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"
    r"|^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$"
)

#: The message tail every dotted-path shape warning carries — the checks
#: are shape-only by design (see the security posture in ``validate``'s
#: docstring); runtime exercise belongs to ``zicato board audit``.
_AUDIT_HINT = (
    "Import-path syntax is checked without loading the module; run "
    "`zicato board audit` to exercise it."
)


def _token(value: Any) -> str:
    """The lowercase wire token of a StrEnum-ish field (or the raw str)."""
    return str(getattr(value, "value", value))


def _board_authoring_warnings(draft: TournamentDraft) -> list[Warning]:
    """The board-entry authoring checks of :func:`validate`.

    All recommend-only; the dotted-path checks are SHAPE-ONLY (no
    server-side import — see the security posture in ``validate``'s
    docstring).
    """
    warnings: list[Warning] = []

    counts: dict[str, int] = {}
    for entry in draft.entries:
        counts[entry.id] = counts.get(entry.id, 0) + 1
    duplicates = sorted(eid for eid, n in counts.items() if n > 1)
    if duplicates:
        warnings.append(
            Warning(
                "duplicate_entry_id",
                f"duplicate board entry id(s): {', '.join(repr(d) for d in duplicates)} "
                "— two entries share an id, so apply cannot save the board "
                "(save_board rejects duplicate ids) and run artifacts would "
                "collide. Recommend-only — apply is not blocked here, but it "
                "will fail.",
                severity="refuse",
            )
        )

    for entry in draft.entries:
        if not _SAFE_ENTRY_ID_RE.match(entry.id or ""):
            warnings.append(
                Warning(
                    "entry_id_unsafe",
                    f"entry id {entry.id!r} is not filesystem-safe (expected "
                    "an alphanumeric start then [A-Za-z0-9._-]); ids become "
                    "run directory names under runs/.",
                )
            )

        expectation = entry.expectation
        if expectation is not None:
            exp_kind = _token(expectation.kind)
            if exp_kind == "predicate" and not _DOTTED_PATH_RE.match(expectation.spec or ""):
                warnings.append(
                    Warning(
                        "dotted_path_malformed",
                        f"entry {entry.id!r}: predicate spec {expectation.spec!r} "
                        "does not look like a dotted path ('pkg.module.attr' or "
                        f"'pkg.module:attr'). {_AUDIT_HINT}",
                    )
                )
            elif exp_kind == "rubric":
                problem = _rubric_spec_problem(expectation.spec)
                if problem:
                    warnings.append(
                        Warning(
                            "rubric_spec_invalid",
                            f"entry {entry.id!r}: rubric spec {problem} — expected a "
                            'JSON object like {"rubric": <text>, "threshold": '
                            '<number|null>, "scale": [lo, hi]}.',
                        )
                    )
            elif exp_kind == "json_schema":
                problem = _json_schema_spec_problem(expectation.spec)
                if problem:
                    warnings.append(
                        Warning(
                            "json_schema_spec_invalid",
                            f"entry {entry.id!r}: json_schema spec {problem} — expected "
                            "a JSON Schema document (a JSON object, or a bare "
                            "true/false).",
                        )
                    )

        for judge in entry.judges:
            if _token(judge.mode) == "python" and not _DOTTED_PATH_RE.match(judge.body or ""):
                warnings.append(
                    Warning(
                        "dotted_path_malformed",
                        f"entry {entry.id!r}: python judge {judge.name!r} body "
                        f"{judge.body!r} does not look like a dotted path "
                        f"('pkg.module.attr' or 'pkg.module:attr'). {_AUDIT_HINT}",
                    )
                )

    budget_assessment = assess_budget_outliers(draft.entries)
    for entry in budget_assessment.outliers:
        warnings.append(
            Warning(
                "entry_budget_outlier",
                f"entry {entry.id!r} has a wall-clock budget of "
                f"{entry.wall_clock_budget_seconds}s, more than {BUDGET_OUTLIER_FACTOR:g}× the "
                f"board median ({budget_assessment.median_seconds:g}s) — it will dominate the "
                "round's wall-clock time.",
                severity="info",
            )
        )

    if draft.judge_only:
        warnings.append(
            Warning(
                "judge_only_board",
                "board_meta sets judge_only: every run is evaluated by goldfive "
                "judges WITHOUT steering — drift is observed, never corrected. "
                "Intentional for judge-calibration boards; surfaced so a "
                "left-over flag is noticed.",
                severity="info",
            )
        )

    return warnings


def _rubric_spec_problem(spec: str) -> str:
    """Why a rubric expectation spec is malformed, or ``""`` when it is fine.

    Mirrors the runtime parse in :func:`zicato.board.rubric.
    evaluate_rubric_judge` — JSON object with a string ``rubric``, an
    optional numeric ``threshold`` and an optional 2-element numeric
    ``scale`` — WITHOUT evaluating anything, so the authoring-time
    warning agrees with the run-time failure mode.
    """
    import json  # noqa: PLC0415

    try:
        parsed = json.loads(spec)
    except (json.JSONDecodeError, TypeError) as exc:
        return f"is not valid JSON ({exc})"
    if not isinstance(parsed, dict):
        return f"must be a JSON object, got {type(parsed).__name__}"
    rubric = parsed.get("rubric")
    if not isinstance(rubric, str) or not rubric.strip():
        return "is missing a non-empty string 'rubric'"
    threshold = parsed.get("threshold")
    if threshold is not None and (
        isinstance(threshold, bool) or not isinstance(threshold, int | float)
    ):
        return f"'threshold' must be a number or null, got {threshold!r}"
    scale = parsed.get("scale")
    if scale is not None:
        if not isinstance(scale, list) or len(scale) != 2:
            return f"'scale' must be a 2-element [lo, hi] list, got {scale!r}"
        if any(isinstance(v, bool) or not isinstance(v, int | float) for v in scale):
            return f"'scale' entries must be numbers, got {scale!r}"
    return ""


def _json_schema_spec_problem(spec: str) -> str:
    """Why a json_schema expectation spec is malformed, or ``""`` if fine.

    A JSON Schema document is a JSON object (or, per the spec, a bare
    boolean). Parse-and-shape only — no schema compilation here.
    """
    import json  # noqa: PLC0415

    try:
        parsed = json.loads(spec)
    except (json.JSONDecodeError, TypeError) as exc:
        return f"is not valid JSON ({exc})"
    if not isinstance(parsed, dict | bool):
        return f"must be a JSON object or boolean, got {type(parsed).__name__}"
    return ""


def _measured_noise_floor(workspace_root: Path) -> float | None:
    """The current epoch's measured A/A floor (``max_abs_delta``), if any.

    Reads the additive ``noise_floor`` field off the CURRENT epoch's
    record (the :func:`zicato.epoch.lifecycle.set_epoch_noise_floor`
    shape — written by ``zicato board audit`` / ``board preflight`` / the
    epoch-open calibration hook). ``None`` on any absence — no epoch, no
    record, no measurement, malformed value — so the statistical validate
    rule degrades silently on an uncalibrated workspace instead of
    guessing a floor.
    """
    from zicato.epoch.lifecycle import current_epoch_id, load_epoch  # noqa: PLC0415

    try:
        epoch_id = current_epoch_id(workspace_root)
        if not epoch_id:
            return None
        record = load_epoch(workspace_root, epoch_id)
    except (OSError, ValueError):
        return None
    raw = record.noise_floor
    if not isinstance(raw, dict):
        return None
    raw_value = raw.get("max_abs_delta")
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        return None
    value = float(raw_value)
    if not math.isfinite(value) or value < 0.0:
        return None
    return value


# ---------------------------------------------------------------------------
# Read-side: draft-vs-draft compare (the fork/compare lifecycle)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Read-side: the build-time contract pre-flight
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _components_changed(diff: dict[str, Any]) -> tuple[str, ...]:
    """Pull the changed-component names out of a ContractDiff dict."""
    return tuple(diff.get("changed_components", ()))


def _predicted_contract_hash(draft: TournamentDraft, workspace_root: Path) -> str:
    """Compute the contract hash the draft WOULD produce, writing nothing.

    Materializes the draft's board / brief / scoring into a throwaway temp
    directory and runs the real :func:`compute_contract_hash` over them
    while retaining every non-file component from the live contract. Used by
    the dry-run preview so the operator sees the exact hash an apply would
    land — without touching the workspace.
    """
    import tempfile

    from zicato.board.jsonl import save_board
    from zicato.epoch.contract import (
        ContractInputs,
        compute_contract_hash,
        resolve_contract_inputs,
    )
    from zicato.epoch.lifecycle import scoring_to_dict

    try:
        live_inputs = resolve_contract_inputs(workspace_root)
    except FileNotFoundError:
        live_inputs = None

    with tempfile.TemporaryDirectory(prefix="zicato-contract-") as tmp:
        tmp_dir = Path(tmp)
        board_file = tmp_dir / "board.jsonl"
        brief_file = tmp_dir / "brief.md"
        scoring_file = tmp_dir / "scoring.json"
        # Preview and publication include the same board metadata so their
        # contract hashes agree.
        save_board(
            list(draft.entries),
            board_file,
            disable_drift=tuple(draft.disable_drift),
            judge_only=draft.judge_only,
        )
        brief_file.write_text(draft.brief, encoding="utf-8")
        import json as _json

        scoring_file.write_text(_json.dumps(scoring_to_dict(draft.scoring)), encoding="utf-8")
        if live_inputs is None:
            predicted_inputs = ContractInputs(
                board_path=board_file,
                brief_path=brief_file,
                scoring_path=scoring_file,
                entrypoint="",
                mutable_trees=(),
                proposer_path=draft.proposer_path,
            )
        else:
            predicted_inputs = dataclasses.replace(
                live_inputs,
                board_path=board_file,
                brief_path=brief_file,
                scoring_path=scoring_file,
                proposer_path=draft.proposer_path,
            )
        return compute_contract_hash(predicted_inputs)


def candidate_scoring(draft: TournamentDraft) -> dict[str, Any]:
    """Validate edited scoring while preserving unrelated authored omissions."""
    import json

    from zicato.epoch.lifecycle import scoring_to_dict
    from zicato.workspace_loader import scoring_weights_from_dict

    after = scoring_to_dict(draft.scoring)
    if draft.source is None or draft.source.file("scoring").text is None:
        scoring_weights_from_dict(after)
        return after
    original = json.loads(draft.source.file("scoring").text or "{}")
    before = scoring_to_dict(scoring_weights_from_dict(original))

    def merge(
        raw: dict[str, Any], previous: dict[str, Any], accepted: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(raw)
        for key in previous.keys() | accepted.keys():
            if key not in accepted:
                result.pop(key, None)
            elif key not in previous or previous[key] != accepted[key]:
                if isinstance(previous.get(key), dict) and isinstance(accepted[key], dict):
                    result[key] = merge(dict(raw.get(key) or {}), previous[key], accepted[key])
                else:
                    result[key] = accepted[key]
        return result

    candidate = merge(original, before, after)
    scoring_weights_from_dict(candidate)
    return candidate


def _accepted_contract(draft: TournamentDraft) -> dict[str, str]:
    """Serialize and validate every accepted file before publication starts."""
    import json

    from zicato.board.jsonl import board_to_jsonl, parse_board_with_meta

    source = draft.source
    if source is None:
        raise ValueError("load a draft from the workspace before applying it")
    board = board_to_jsonl(
        list(draft.entries), disable_drift=draft.disable_drift, judge_only=draft.judge_only
    )
    parse_board_with_meta(board)
    scoring = json.dumps(candidate_scoring(draft), indent=2) + "\n"
    config = dict(source.config.raw)
    contract = dict(source.config.contract)
    contract.update(
        board_path=str(source.file("board").path),
        rubric_path=str(source.file("brief").path),
        scoring_path=str(source.file("scoring").path),
    )
    if "brief_path" in contract:
        contract["brief_path"] = str(source.file("brief").path)
    if draft.proposer_path is None:
        contract.pop("proposer_path", None)
    else:
        contract["proposer_path"] = str(draft.proposer_path.resolve())
    config["contract"] = contract
    accepted = {
        "board": board,
        "brief": draft.brief,
        "scoring": scoring,
        "config": json.dumps(config, indent=2, sort_keys=True) + "\n",
    }
    # Preserve formatting when an unrelated edit leaves the decoded component
    # unchanged. Its original bytes still determine the source revision.
    for component in ("scoring", "config"):
        original = source.file(component).text
        if original is not None and json.loads(original) == json.loads(accepted[component]):
            accepted[component] = original
    original_board = source.file("board").text
    if original_board is not None and parse_board_with_meta(
        original_board
    ) == parse_board_with_meta(board):
        accepted["board"] = original_board
    return accepted


def apply(
    draft: TournamentDraft,
    workspace_root: Path,
    confirm: bool,
    *,
    writer: WorkspaceLock | None = None,
) -> ApplyResult:
    """Apply the draft, or preview it.

    When ``confirm`` is ``True`` the draft is written to the workspace's
    live contract source paths (board.jsonl, brief.md, scoring.json incl.
    tournament + overfitting + gate + weights, and the proposer dir) via
    the contract publication owner, and the existing auto-epoch machinery rolls
    the epoch on the next resolve. When ``confirm`` is ``False`` nothing
    is written — the result is a dry-run preview carrying the diff, the
    predicted contract hash, and the cost.

    This function NEVER starts a live ``zicato evolve``.
    """
    from zicato.contract_draft.publication import recover_contract_publication  # noqa: PLC0415
    from zicato.runtime.lock import acquire_workspace_lock, validate_workspace_lock  # noqa: PLC0415

    if confirm and writer is None:
        with acquire_workspace_lock(workspace_root, "contract-edit") as owned_writer:
            return apply(draft, workspace_root, confirm, writer=owned_writer)
    if writer is not None:
        validate_workspace_lock(writer, workspace_root)
        if confirm:
            recover_contract_publication(workspace_root, writer=writer)
    if draft.source is not None:
        draft.source.require_unchanged(workspace_root)
    candidate_scoring(draft)
    diff = draft.diff_vs_live(workspace_root)
    diff_dict = diff.to_dict()
    cost = estimate_cost(draft)
    warns = tuple(validate(draft, workspace_root))
    components_changed = _components_changed(diff_dict)

    if not confirm:
        predicted = _predicted_contract_hash(draft, workspace_root)
        return ApplyResult(
            confirmed=False,
            rolled=False,
            components_changed=components_changed,
            new_contract_hash=predicted,
            cost=cost,
            diff=diff_dict,
            warnings=warns,
        )

    from zicato.contract_draft.publication import (  # noqa: PLC0415
        capture_contract_source,
        publish_contract,
    )

    accepted = _accepted_contract(draft)
    assert draft.source is not None and writer is not None
    publish_contract(draft.source, accepted, writer=writer)
    draft.source = capture_contract_source(workspace_root)
    # Recompute the hash from the now-written live contract so the result
    # reflects exactly what the next resolve will see.
    from zicato.epoch.contract import (  # noqa: PLC0415
        compute_contract_hash,
        resolve_contract_inputs,
    )

    new_hash = compute_contract_hash(resolve_contract_inputs(workspace_root))
    return ApplyResult(
        confirmed=True,
        rolled=diff.rolls_epoch,
        components_changed=components_changed,
        new_contract_hash=new_hash,
        cost=cost,
        diff=diff_dict,
        warnings=warns,
    )


__all__ = [
    "DraftPatch",
    "CostLine",
    "CostEstimate",
    "Warning",
    "ApplyResult",
    "set_structure",
    "set_param",
    "set_holdout",
    "set_proposer",
    "set_weights",
    "set_gate",
    "set_namespace_weights",
    "set_proposer_quality",
    "set_experimental",
    "set_goldfive",
    "set_mutation_surface",
    "set_telemetry_dialect",
    "set_screening",
    "edit_board_entry",
    "add_board_entry",
    "remove_board_entry",
    "add_judge",
    "remove_judge",
    "set_brief",
    "set_board_meta",
    "estimate_cost",
    "validate",
    "apply",
    "VALID_TOURNAMENT_STRUCTURES",
]
