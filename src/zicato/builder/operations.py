"""Builder operations — the single source of truth for form + copilot.

Every editable change to a :class:`~zicato.builder.draft.TournamentDraft`
flows through one of the operations here. Both the form's direct edits
(B2) and the copilot's tool calls (B1b) call the *same* functions, so
there is exactly one place each mutation's semantics live.

The write ops (``set_structure`` … ``set_brief``) mutate the draft in
place and return a structured :class:`DraftPatch` describing what changed
— the UI / chat renders that to confirm the edit. The read ops
(:func:`estimate_cost`, :func:`validate`) never mutate. :func:`apply`
either writes the draft to the workspace (``confirm=True``) reusing the
existing epoch / register write paths and lets the auto-epoch machinery
roll the epoch on the next resolve, or returns a dry-run preview
(``confirm=False``) that writes nothing.

These functions never start a live ``zicato evolve``.
"""

from __future__ import annotations

import dataclasses
import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.board.split import HOLDOUT_TAG, split_board
from zicato.builder.draft import TournamentDraft
from zicato.core.types import (
    KNOWN_TELEMETRY_DIALECTS,
    VALID_TOURNAMENT_STRUCTURES,
    BoardEntry,
    JudgeSpec,
    ScoringWeights,
    TournamentStructure,
)
from zicato.selection.registry import default_replicates_for

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
        clearly-labelled auxiliary lines (``best-of-N propose calls``),
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
        ``"refuse"`` (statistically unsound — the same recommend-only
        REFUSE posture the contract pre-flight verdict carries). The
        builder never blocks on any of these — they inform the operator's
        choice; even a ``refuse`` never hard-blocks apply.
    """

    code: str
    message: str
    severity: str = "warning"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """The outcome of the builder's :func:`preflight` read-op.

    Either a real measurement (``available=True`` with the
    :class:`~zicato.epoch.preflight.PreflightReport` JSON + the measured
    A/A noise floor) or an HONEST degrade (``available=False`` with a
    clear ``reason`` naming what the measurement needs — a registered
    target, a seeded baseline, runtime ``call_llm`` config). Never an
    exception for a workspace that simply is not ready; recommend-only
    either way.

    Fields
    ------
    available:
        ``True`` iff the measurement ran.
    verdict:
        The pre-flight verdict (``"ok"`` / ``"warn"`` / ``"refuse"``)
        when available, else ``None``. Recommend-only, never a gate.
    reason:
        The honest degrade explanation when ``available`` is ``False``.
    report:
        :meth:`PreflightReport.to_json` dict when available.
    noise_floor:
        The measured A/A floor's :meth:`NoiseFloor.to_json` dict when
        available.
    """

    available: bool
    verdict: str | None = None
    reason: str = ""
    report: dict[str, Any] | None = None
    noise_floor: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "verdict": self.verdict,
            "reason": self.reason,
            "report": self.report,
            "noise_floor": self.noise_floor,
        }


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
        :class:`~zicato.builder.draft.ContractDiff` reports).
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


def set_structure(draft: TournamentDraft, structure: str) -> DraftPatch:
    """Set the tournament structure, preserving the existing params.

    Raises :class:`ValueError` on an invalid structure token (the
    :class:`TournamentStructure` constructor validates and lists the
    valid tokens).
    """
    old = draft.scoring.tournament_structure
    new_ts = TournamentStructure(structure=structure, params=dict(old.params))
    draft.scoring = _replace_scoring(draft, tournament_structure=new_ts)
    return DraftPatch(
        op="set_structure",
        changed={"structure": {"from": old.structure, "to": structure}},
    )


def set_param(draft: TournamentDraft, key: str, value: Any) -> DraftPatch:
    """Set one structure param (``field_size``, ``replicates``, …).

    The params object is opaque to the data layer (per-key semantics are
    the selection strategy's), so the value is stored verbatim. Setting a
    value of ``None`` removes the key.
    """
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


def set_holdout(
    draft: TournamentDraft,
    *,
    enabled: bool | None = None,
    fraction: float | None = None,
    tags: list[str] | None = None,
    min_board_size_for_split: int | None = None,
    rotate_holdout: bool | None = None,
    restrict_proposer_visibility: bool | None = None,
    random_baseline_every_n: int | None = None,
    max_generations_per_contract: int | None = None,
    ladder: dict[str, Any] | None = None,
) -> DraftPatch:
    """Edit the train/holdout split + the full anti-overfitting config.

    ``enabled`` / ``fraction`` tune the hash-derived split on
    :class:`OverfittingConfig`; ``tags`` sets the explicit per-entry
    ``holdout`` tag exactly on the supplied ids (every other entry loses
    the tag). The remaining keywords cover the rest of the overfitting
    contract: the split floor (``min_board_size_for_split``), per-epoch
    holdout rotation, the proposer-visibility restriction, the placebo
    cadence (``random_baseline_every_n``; the gate-discrimination
    control — ``0`` fields no baseline), and the board-refresh ceiling
    (``max_generations_per_contract``; ``0`` CLEARS the ceiling, since
    ``None`` here means "leave unchanged").

    ``ladder`` is a PARTIAL mapping over the
    :class:`~zicato.core.scoring_config.LadderConfig` knobs (``enabled``
    / ``threshold`` / ``budget`` / ``noise_scale``) merged onto the
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
        ("random_baseline_every_n", random_baseline_every_n),
    ):
        if value is not None and value != getattr(of, name):
            of_changes[name] = value
            changed[name] = {"from": getattr(of, name), "to": value}
    if max_generations_per_contract is not None:
        # ``0`` clears the ceiling (the field's meaningful "off" is None,
        # which this op reserves for "leave unchanged").
        ceiling = None if max_generations_per_contract == 0 else max_generations_per_contract
        if ceiling != of.max_generations_per_contract:
            of_changes["max_generations_per_contract"] = ceiling
            changed["max_generations_per_contract"] = {
                "from": of.max_generations_per_contract,
                "to": ceiling,
            }
    if ladder is not None:
        allowed = {"enabled", "threshold", "budget", "noise_scale"}
        unknown = set(ladder) - allowed
        if unknown:
            raise ValueError(
                f"unknown ladder key(s) {sorted(unknown)!r}; expected a subset of "
                f"{sorted(allowed)!r}"
            )
        ladder_changes: dict[str, Any] = {}
        for key, value in ladder.items():
            # ``threshold: None`` is a REAL value (auto-derive); every other
            # key treats None as absent-from-the-mapping only.
            if value != getattr(of.ladder, key):
                ladder_changes[key] = value
                changed[f"ladder.{key}"] = {"from": getattr(of.ladder, key), "to": value}
        if ladder_changes:
            of_changes["ladder"] = dataclasses.replace(of.ladder, **ladder_changes)
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


def set_weights(
    draft: TournamentDraft,
    *,
    drift_weight: float | None = None,
    pass_weight: float | None = None,
    per_kind_weights: dict[str, float] | None = None,
    per_judge_weights: dict[str, float] | None = None,
    default_judge_weight: float | None = None,
    plan_revision_weight: float | None = None,
    runtime_weight: float | None = None,
    severity_weights: dict[str, float] | None = None,
) -> DraftPatch:
    """Set scoring weights (the loss-shaping knobs).

    Any subset of the supported weight fields may be supplied. Mapping
    fields replace the whole mapping (the builder edits them wholesale).
    """
    changed: dict[str, Any] = {}
    scoring_changes: dict[str, Any] = {}
    for name, value in (
        ("drift_weight", drift_weight),
        ("pass_weight", pass_weight),
        ("default_judge_weight", default_judge_weight),
        ("plan_revision_weight", plan_revision_weight),
        ("runtime_weight", runtime_weight),
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


def set_gate(
    draft: TournamentDraft,
    *,
    promote_margin: float | None = None,
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

    ``monotonicity`` is the on/off switch; ``monotonicity_scope`` selects
    the granularity when it is on (``"per_entry"`` — default, every
    champion-passed entry must hold — or ``"aggregate"`` — only the overall
    pass-rate may not regress; see SCORING.md §5). An invalid scope token
    raises rather than silently coercing.

    The remaining keywords cover the rest of the gate contract:
    ``namespace_monotonicity`` replaces the per-namespace strict-
    monotonicity flag mapping wholesale (the builder edits mappings
    wholesale, like :func:`set_weights`); the two ``block_on_*`` booleans
    opt into the integrity BLOCKING modes (containment / gate-
    contradiction — both alarm-only by default); the ``regression_*``
    trio configures the snapshot's own test suite as a hard pre-gate
    (``regression_test_command`` is the argv list; ``regression_timeout_s``
    must be >= 1).
    """
    changed: dict[str, Any] = {}
    scoring_changes: dict[str, Any] = {}
    if promote_margin is not None and promote_margin != draft.scoring.promote_margin:
        scoring_changes["promote_margin"] = promote_margin
        changed["promote_margin"] = {
            "from": draft.scoring.promote_margin,
            "to": promote_margin,
        }
    if monotonicity is not None and monotonicity != draft.scoring.pass_rate_monotonicity:
        scoring_changes["pass_rate_monotonicity"] = monotonicity
        changed["pass_rate_monotonicity"] = {
            "from": draft.scoring.pass_rate_monotonicity,
            "to": monotonicity,
        }
    if monotonicity_scope is not None:
        if monotonicity_scope not in ("per_entry", "aggregate"):
            raise ValueError(
                f"monotonicity_scope must be 'per_entry' or 'aggregate', got "
                f"{monotonicity_scope!r}"
            )
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
        if regression_timeout_s < 1:
            raise ValueError(f"regression_timeout_s must be >= 1, got {regression_timeout_s!r}")
        if regression_timeout_s != draft.scoring.regression_timeout_s:
            scoring_changes["regression_timeout_s"] = regression_timeout_s
            changed["regression_timeout_s"] = {
                "from": draft.scoring.regression_timeout_s,
                "to": regression_timeout_s,
            }
    if scoring_changes:
        draft.scoring = _replace_scoring(draft, **scoring_changes)
    return DraftPatch(op="set_gate", changed=changed)


def set_namespace_weights(
    draft: TournamentDraft,
    *,
    namespace_weights: dict[str, float] | None = None,
    diff_complexity_weight: float | None = None,
    diff_complexity_ceiling: float | None = None,
) -> DraftPatch:
    """Set the multi-objective namespace coefficients + the parsimony term.

    ``namespace_weights`` replaces the whole per-namespace coefficient
    mapping (keys keep their trailing colon, e.g. ``"drift:"``; the SIGN
    encodes the namespace's "worse" direction — positive = higher is
    worse, negative = higher is better, zero = tracked but unscored).
    ``diff_complexity_weight`` is the opt-in MDL/parsimony coefficient
    (``0`` = the term is exactly absent; must be >= 0).
    ``diff_complexity_ceiling`` is the paired opt-in parsimony CEILING
    (``0`` = OFF; must be >= 0): a challenger whose diff complexity exceeds
    it is rejected outright by the gate. All are contract fields — changing
    any rolls the epoch.
    """
    changed: dict[str, Any] = {}
    scoring_changes: dict[str, Any] = {}
    if namespace_weights is not None:
        normalized = {str(k): float(v) for k, v in namespace_weights.items()}
        if normalized != dict(draft.scoring.namespace_weights):
            scoring_changes["namespace_weights"] = normalized
            changed["namespace_weights"] = {
                "from": dict(draft.scoring.namespace_weights),
                "to": normalized,
            }
    if diff_complexity_weight is not None:
        if diff_complexity_weight < 0:
            raise ValueError(f"diff_complexity_weight must be >= 0, got {diff_complexity_weight!r}")
        if diff_complexity_weight != draft.scoring.diff_complexity_weight:
            scoring_changes["diff_complexity_weight"] = diff_complexity_weight
            changed["diff_complexity_weight"] = {
                "from": draft.scoring.diff_complexity_weight,
                "to": diff_complexity_weight,
            }
    if diff_complexity_ceiling is not None:
        if diff_complexity_ceiling < 0:
            raise ValueError(
                f"diff_complexity_ceiling must be >= 0, got {diff_complexity_ceiling!r}"
            )
        if diff_complexity_ceiling != draft.scoring.diff_complexity_ceiling:
            scoring_changes["diff_complexity_ceiling"] = diff_complexity_ceiling
            changed["diff_complexity_ceiling"] = {
                "from": draft.scoring.diff_complexity_ceiling,
                "to": diff_complexity_ceiling,
            }
    if scoring_changes:
        draft.scoring = _replace_scoring(draft, **scoring_changes)
    return DraftPatch(op="set_namespace_weights", changed=changed)


def set_proposer_quality(
    draft: TournamentDraft,
    *,
    best_of_n: int | None = None,
    critique_enabled: bool | None = None,
    process_exemplars: int | None = None,
    recombine: bool | None = None,
    genealogy: int | None = None,
    calibration_feedback: int | None = None,
    recombine_merge: str | None = None,
) -> DraftPatch:
    """Set the proposer-quality levers: best-of-N slate + self-critique.

    ``best_of_n`` is how many candidate experiments each propose-step
    samples before selection (``1`` = the historical single sample, no
    critique; must be >= 1); ``critique_enabled`` toggles the auxiliary
    self-critique selection pass (inert at ``best_of_n == 1``);
    ``process_exemplars`` opts the proposer into up to that many REDACTED
    drift-anchored event windows per round (``0`` = off, the default —
    see ``docs/design/PROCESS-EXEMPLARS.md`` incl. its §5 harm-detection
    runbook before opting in; must be >= 0; read-side only, so the cost
    meter is untouched). ``recombine`` opts in the mechanical
    recombination slot (WS-REC): when ``True`` the last best-of-N slot
    mints the patch union of two rejected complementary challengers
    instead of sampling the LLM — REQUIRES ``best_of_n > 1`` to have any
    effect (a single-sample proposer has no slate slot to mint into) and
    is cost-neutral (the mint REPLACES the slot's auxiliary propose call,
    never adds one — see :mod:`zicato.epoch.recombine`). Flipping it
    rolls the epoch. ``recombine_merge`` (``"mechanical"`` default |
    ``"llm"``) chooses HOW the slot composes the union: ``"mechanical"``
    mints the disjoint patch concatenation with no LLM call; ``"llm"``
    issues one merge call whose response flows through the normal parse
    path and RELAXES disjointness so an OVERLAPPING pair the mechanical
    mint cannot touch can be merged (PROPOSER.md §2.6.1). Meaningful only
    with ``recombine`` on; ``"llm"`` rolls the epoch. ``genealogy`` opts in
    the genealogy channel
    (WS-GENE): up to that many candidate-LINEAGE items — the champion's
    promoted patch history + diverse rejected reign candidates, each with
    a banded outcome — are spliced into the prompt so the proposer can
    evolve in context (``0`` = off, the default; must be >= 0; read-side
    only, so the cost meter is untouched — see
    :mod:`zicato.proposer.genealogy`). ``calibration_feedback`` opts in the
    critic-calibration channel (WS-CAL): up to that many RECENT graded
    hypotheses — the proposer's own falsifiable predictions graded against
    realized outcomes (hit / miss / unresolved counts + the overall
    calibration fraction + banded per-claim outcomes) — are spliced into
    the prompt so the proposer sees its OWN miss pattern and predicts more
    honestly (``0`` = off, the default; must be >= 0; read-side only, so the
    cost meter is untouched — see :mod:`zicato.proposer.calibration`).
    COMPOSES with :func:`set_screening` — both edit the same nested
    ``proposer_quality`` block; the screen knobs stay that op's. Changing
    any rolls the epoch.
    """
    changed: dict[str, Any] = {}
    quality = draft.scoring.proposer_quality
    quality_changes: dict[str, Any] = {}
    if best_of_n is not None:
        if best_of_n < 1:
            raise ValueError(f"best_of_n must be >= 1, got {best_of_n!r}")
        if best_of_n != quality.best_of_n:
            quality_changes["best_of_n"] = best_of_n
            changed["best_of_n"] = {"from": quality.best_of_n, "to": best_of_n}
    if critique_enabled is not None and critique_enabled != quality.critique_enabled:
        quality_changes["critique_enabled"] = critique_enabled
        changed["critique_enabled"] = {"from": quality.critique_enabled, "to": critique_enabled}
    if process_exemplars is not None:
        if process_exemplars < 0:
            raise ValueError(f"process_exemplars must be >= 0, got {process_exemplars!r}")
        if process_exemplars != quality.process_exemplars:
            quality_changes["process_exemplars"] = process_exemplars
            changed["process_exemplars"] = {
                "from": quality.process_exemplars,
                "to": process_exemplars,
            }
    if recombine is not None and recombine != quality.recombine:
        quality_changes["recombine"] = recombine
        changed["recombine"] = {"from": quality.recombine, "to": recombine}
    if recombine_merge is not None:
        if recombine_merge not in ("mechanical", "llm"):
            raise ValueError(
                f"recombine_merge must be 'mechanical' or 'llm', got {recombine_merge!r}"
            )
        if recombine_merge != quality.recombine_merge:
            quality_changes["recombine_merge"] = recombine_merge
            changed["recombine_merge"] = {
                "from": quality.recombine_merge,
                "to": recombine_merge,
            }
    if genealogy is not None:
        if genealogy < 0:
            raise ValueError(f"genealogy must be >= 0, got {genealogy!r}")
        if genealogy != quality.genealogy:
            quality_changes["genealogy"] = genealogy
            changed["genealogy"] = {"from": quality.genealogy, "to": genealogy}
    if calibration_feedback is not None:
        if calibration_feedback < 0:
            raise ValueError(f"calibration_feedback must be >= 0, got {calibration_feedback!r}")
        if calibration_feedback != quality.calibration_feedback:
            quality_changes["calibration_feedback"] = calibration_feedback
            changed["calibration_feedback"] = {
                "from": quality.calibration_feedback,
                "to": calibration_feedback,
            }
    if quality_changes:
        draft.scoring = _replace_scoring(
            draft, proposer_quality=dataclasses.replace(quality, **quality_changes)
        )
    return DraftPatch(op="set_proposer_quality", changed=changed)


def set_experiment_memory(
    draft: TournamentDraft,
    *,
    cross_epoch: bool | None = None,
) -> DraftPatch:
    """Set the experiment-memory scoping (what settled history the proposer sees).

    ``cross_epoch=True`` opts settled experiments from PRIOR epochs that
    share the current contract hash into the proposer's digest (banded,
    same-epoch entries keep budget priority); ``False`` (the default) is
    same-epoch-only. A contract field — changing it rolls the epoch.
    """
    changed: dict[str, Any] = {}
    memory = draft.scoring.experiment_memory
    if cross_epoch is not None and cross_epoch != memory.cross_epoch:
        draft.scoring = _replace_scoring(
            draft, experiment_memory=dataclasses.replace(memory, cross_epoch=cross_epoch)
        )
        changed["cross_epoch"] = {"from": memory.cross_epoch, "to": cross_epoch}
    return DraftPatch(op="set_experiment_memory", changed=changed)


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
        if dialect not in KNOWN_TELEMETRY_DIALECTS:
            known = ", ".join(sorted(KNOWN_TELEMETRY_DIALECTS))
            raise ValueError(f"telemetry_dialect must be one of {{{known}}}, got {dialect!r}")
        current = draft.scoring.telemetry_dialect
        if dialect != current:
            draft.scoring = _replace_scoring(draft, telemetry_dialect=dialect)
            changed["telemetry_dialect"] = {"from": current, "to": dialect}
    return DraftPatch(op="set_telemetry_dialect", changed=changed)


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
    raises (the dataclass validator re-checks on replace).
    """
    changed: dict[str, Any] = {}
    quality = draft.scoring.proposer_quality
    quality_changes: dict[str, Any] = {}
    if entries is not None:
        if entries < 0:
            raise ValueError(f"screen entries must be >= 0, got {entries!r}")
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

    A board change, so it rolls the epoch like any board edit. This is the op a
    regression / coverage / harder-variant suggestion applies through (the
    ``reflect apply`` suggestion seam).
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


def restore_draft(
    draft: TournamentDraft,
    source: TournamentDraft,
    *,
    op: str = "revert_to_live",
) -> DraftPatch:
    """Restore ``draft``'s contract fields IN PLACE from ``source``.

    The shared implementation behind the ``revert_to_live`` lifecycle op
    (``source`` = a fresh :meth:`TournamentDraft.from_workspace`) and the
    step-``undo`` op (``source`` = a :class:`DraftStore` history
    snapshot). IN PLACE — never a rebind — so every live binding to the
    draft object (the store's session entry, a named slot the session is
    on, the copilot's bound context) sees the restored state; rebinding
    would silently detach the session from its slot.

    The patch's ``changed`` map reports the restored components through
    the same canonicalizers the epoch-roll rule uses
    (:func:`compare_drafts`), so a restore that only reorders entries —
    canonically identical — honestly reports no change.
    """
    diff = compare_drafts(draft, source)
    changed: dict[str, Any] = {}
    for component in diff["changed_components"]:
        if component == "scoring":
            changed["scoring"] = diff["scoring"]
        elif component == "board":
            changed["board"] = diff["board"]
        elif component == "board_meta":
            changed["board_meta"] = {
                "from": diff["board_meta"]["a"],
                "to": diff["board_meta"]["b"],
            }
        elif component == "brief":
            changed["brief_chars"] = {
                "from": diff["brief"]["a_chars"],
                "to": diff["brief"]["b_chars"],
            }
        elif component == "proposer":
            changed["proposer_path"] = {
                "from": diff["proposer"]["a"],
                "to": diff["proposer"]["b"],
            }
    draft.scoring = source.scoring
    draft.entries = list(source.entries)
    draft.brief = source.brief
    draft.proposer_path = source.proposer_path
    draft.disable_drift = tuple(source.disable_drift)
    draft.judge_only = source.judge_only
    note = "" if changed else "draft already matches the restore source"
    return DraftPatch(op=op, changed=changed, note=note)


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
    builder edits mappings/sets wholesale, like :func:`set_weights`);
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

    GUI note (invariant L2's documented exception): the board_meta form
    control lands with the board-editor phase; until then this op is
    reachable from the copilot and the REST dispatch only.
    """
    from goldfive import DriftKind  # noqa: PLC0415

    from zicato.core.drift_kinds import validate_drift_kind  # noqa: PLC0415

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


def _param_int(params: Any, key: str, default: int) -> int:
    """Read an int param, falling back to ``default`` on absence / bad type."""
    try:
        return int(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _param_float(params: Any, key: str, default: float) -> float:
    """Read a float param, falling back to ``default`` on absence / bad type."""
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        return default


def estimate_cost(draft: TournamentDraft) -> CostEstimate:
    """Estimate board-runs-per-round for the draft's structure + params.

    The model follows the builder skill's schedule arithmetic:

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
    * ``best-of-N propose calls`` — ``proposes × best_of_n`` AUXILIARY
      LLM calls per round (the slate SAMPLING — the WS-ENS
      proposer-breadth role; the critique / revise DEPTH calls run on
      proposer-depth and are not separately metered). NOT board runs, so
      the line is labelled auxiliary and EXCLUDED from the board-runs
      headline — but it is real money and belongs on the meter.
    * ``crowning-confirm runs`` — the evidence gate's defer→replicate
      budget: each replicate is a FRESH board sweep for BOTH crowning
      contestants, so ``budget × 2 × board``. Spent per CONFIRMED
      crowning (an upper bound per round); with the scaffold's
      32-replicate budget this is typically the LARGEST term.
    * ``placebo-baseline runs`` — the ``random_baseline_every_n``
      control arm: one extra no-op challenger every N rounds, amortized
      to ``ceil(replicates × board / N)`` per round.

    The estimate is deliberately a coarse upper-ish bound for the
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
    # default to 2 — replication, not bracket shape, is their noise lever),
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
    # candidate experiments — auxiliary LLM CALLS, not board runs, so the
    # line is labelled and EXCLUDED from the board-runs headline. Real
    # spend the operator should still see priced. An UPPER BOUND under the
    # recombination slot (proposer_quality.recombine): a round that mints
    # a recombination pair REPLACES its last slot's propose call with the
    # free mechanical mint, spending best_of_n − 1 calls that round.
    if quality.best_of_n > 1:
        propose_calls = proposes * quality.best_of_n
        lines.append(
            CostLine(
                "best-of-N propose calls",
                propose_calls,
                f"proposes {proposes} × best_of_n {quality.best_of_n} — auxiliary "
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
    baseline_n = draft.scoring.overfitting.random_baseline_every_n
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
      explicitly (``noise_floor_max_abs_delta``, e.g. the floor a
      just-run :func:`preflight` measured) or read off the current
      epoch's record under ``workspace_root`` — a ``promote_margin`` at
      or below that floor WITH the evidence gate off
      (``promote_confidence_threshold`` unset) is flagged at ``refuse``
      severity: every duel decided by the margin alone would be decided
      by noise. Recommend-only, like every warning here — apply is never
      hard-blocked.

    SECURITY POSTURE — the dotted-path checks are SHAPE-ONLY. ``validate``
    NEVER imports (or ``find_spec``s) an operator- or copilot-supplied
    dotted path server-side: resolving a module executes parent-package
    code, and a draft may be copilot-authored. The messages point the
    operator at ``zicato board audit``, which exercises the paths in the
    workspace's own runtime context. Keep any future path check on this
    side of the line.
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

#: How far above the board's median wall-clock budget an entry must sit
#: before the ``entry_budget_outlier`` info fires.
_BUDGET_OUTLIER_FACTOR = 10.0

#: The message tail every dotted-path shape warning carries — the checks
#: are shape-only by design (see the security posture in ``validate``'s
#: docstring); runtime exercise belongs to ``zicato board audit``.
_AUDIT_HINT = (
    "Shape-check only — the builder never imports the path; run "
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

    budgets = [e.wall_clock_budget_seconds for e in draft.entries]
    if len(budgets) >= 2:
        median = statistics.median(budgets)
        if median > 0:
            for entry in draft.entries:
                if entry.wall_clock_budget_seconds > _BUDGET_OUTLIER_FACTOR * median:
                    warnings.append(
                        Warning(
                            "entry_budget_outlier",
                            f"entry {entry.id!r} has a wall-clock budget of "
                            f"{entry.wall_clock_budget_seconds}s, more than 10× the "
                            f"board median ({median:g}s) — it will dominate the "
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


def compare_drafts(a: TournamentDraft, b: TournamentDraft) -> dict[str, Any]:
    """A keyed diff between two drafts — the fork/compare read-op.

    The :meth:`TournamentDraft.diff_vs_live` precedent generalized to any
    draft pair, over the SAME canonicalizers the epoch-roll rule uses
    (:func:`~zicato.builder.draft._scoring_canon` /
    :func:`~zicato.builder.draft._board_canon` /
    :func:`~zicato.builder.draft._brief_canon`), so "differs" here agrees
    with "would roll the epoch". Purely read-side; mutates nothing.

    Shape::

        {
          "changed_components": ["scoring", "board", ...],
          "scoring": {key: {"a": ..., "b": ...}, ...},   # differing top-level keys
          "board": {"added": [ids], "removed": [ids], "changed": [ids]},
          "board_meta": {"changed": bool, "a": {...}, "b": {...}},
          "brief": {"changed": bool, "a_chars": int, "b_chars": int},
          "proposer": {"changed": bool, "a": str|None, "b": str|None},
        }

    ``scoring`` keys come from the contract-canonical scoring form (float-
    rounded, omitted-at-default fields absent), so the diff never reports
    a phantom change the contract hash would not see. ``board`` is keyed
    by entry id: ``added`` = in ``b`` only, ``removed`` = in ``a`` only,
    ``changed`` = present in both with differing canonical content.
    ``board_meta`` is the board-level header (disable_drift / judge_only)
    — not per-entry, so it gets its own detail key and its own
    ``changed_components`` entry when it differs.
    """
    import json as _json

    from zicato.builder.draft import _board_canon, _brief_canon, _scoring_canon

    changed_components: list[str] = []

    scoring_a = _json.loads(_scoring_canon(a.scoring))
    scoring_b = _json.loads(_scoring_canon(b.scoring))
    scoring_diff: dict[str, Any] = {}
    for key in sorted(set(scoring_a) | set(scoring_b)):
        va, vb = scoring_a.get(key), scoring_b.get(key)
        if va != vb:
            scoring_diff[key] = {"a": va, "b": vb}
    if scoring_diff:
        changed_components.append("scoring")

    canon_a = {e.id: _board_canon([e]) for e in a.entries}
    canon_b = {e.id: _board_canon([e]) for e in b.entries}
    added = sorted(set(canon_b) - set(canon_a))
    removed = sorted(set(canon_a) - set(canon_b))
    entry_changed = sorted(
        eid for eid in set(canon_a) & set(canon_b) if canon_a[eid] != canon_b[eid]
    )
    if added or removed or entry_changed:
        changed_components.append("board")

    meta_a = {
        "disable_drift": [str(getattr(k, "value", k)) for k in a.disable_drift],
        "judge_only": a.judge_only,
    }
    meta_b = {
        "disable_drift": [str(getattr(k, "value", k)) for k in b.disable_drift],
        "judge_only": b.judge_only,
    }
    meta_changed = meta_a != meta_b
    if meta_changed:
        changed_components.append("board_meta")

    brief_changed = _brief_canon(a.brief) != _brief_canon(b.brief)
    if brief_changed:
        changed_components.append("brief")

    proposer_a = str(a.proposer_path) if a.proposer_path is not None else None
    proposer_b = str(b.proposer_path) if b.proposer_path is not None else None
    proposer_changed = proposer_a != proposer_b
    if proposer_changed:
        changed_components.append("proposer")

    return {
        "changed_components": changed_components,
        "scoring": scoring_diff,
        "board": {"added": added, "removed": removed, "changed": entry_changed},
        "board_meta": {"changed": meta_changed, "a": meta_a, "b": meta_b},
        "brief": {
            "changed": brief_changed,
            "a_chars": len(a.brief),
            "b_chars": len(b.brief),
        },
        "proposer": {"changed": proposer_changed, "a": proposer_a, "b": proposer_b},
    }


# ---------------------------------------------------------------------------
# Read-side: the build-time contract pre-flight
# ---------------------------------------------------------------------------


async def preflight(
    draft: TournamentDraft,
    workspace_root: Path,
    *,
    runs: int | None = None,
) -> PreflightResult:
    """Measure the DRAFT contract's noise floor + achievable signal.

    Runs :func:`zicato.epoch.preflight.run_contract_preflight` — the SAME
    measurement ``zicato board preflight`` takes — but against the
    DRAFT's board and scoring weights (the two contract components the
    builder edits; ``run_contract_preflight`` consumes them directly, so
    the draft needs no on-disk materialization). The champion tree, the
    adapter, and the runtime ``call_llm`` config are the workspace's own:
    a pre-flight needs a real registered target to probe.

    HONEST DEGRADE, never a crash: each missing prerequisite returns
    ``available=False`` with a ``reason`` naming exactly what is missing
    (no current epoch / no seeded baseline generation / no adapter block /
    no ``runtime.harness_call_llm`` dotted callables / an empty draft
    board / no mutation points). The result is RECOMMEND-ONLY and is NOT
    persisted onto the epoch record — the draft is not the live contract,
    so its measurement must never masquerade as the live epoch's.

    This op never starts a live ``zicato evolve``; it spends only the
    small K-draw measurement budget (cache-idempotent with ``zicato board
    audit`` — re-running is a cache hit).
    """
    from zicato import adapter_factory, runtime_factory, workspace_loader  # noqa: PLC0415
    from zicato.core.types import Generation  # noqa: PLC0415
    from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415
    from zicato.epoch.preflight import run_contract_preflight  # noqa: PLC0415
    from zicato.tournament.calibration import DEFAULT_CALIBRATION_RUNS  # noqa: PLC0415

    resolved_runs = DEFAULT_CALIBRATION_RUNS if runs is None else int(runs)
    if resolved_runs < 2:
        raise ValueError(f"preflight needs at least 2 A/A draws, got {resolved_runs!r}")

    if not draft.entries:
        return PreflightResult(
            available=False,
            reason="preflight requires a non-empty draft board — there is nothing to measure",
        )

    epoch_id = current_epoch_id(workspace_root)
    if not epoch_id:
        return PreflightResult(
            available=False,
            reason=(
                "preflight requires a registered target: no current epoch under "
                "this workspace (run `zicato register` / `zicato epoch new` first)"
            ),
        )

    try:
        workspace_config = workspace_loader.load_workspace_config(workspace_root)
    except (FileNotFoundError, ValueError) as exc:
        return PreflightResult(
            available=False,
            reason=f"preflight requires a registered target: {exc}",
        )

    from zicato.orchestrator import (  # noqa: PLC0415
        _resolve_current_generation,
        _snapshot_root,
    )

    try:
        champion_id = _resolve_current_generation(workspace_root, epoch_id)
    except FileNotFoundError:
        return PreflightResult(
            available=False,
            reason=(
                "preflight requires a registered target with a seeded baseline "
                "generation — run one `zicato evolve` round (or seed v0) first"
            ),
        )

    try:
        adapter = adapter_factory.make_adapter_from_config(workspace_config)
    except (KeyError, ValueError, ImportError) as exc:
        return PreflightResult(
            available=False,
            reason=f"preflight requires a configured adapter: {exc}",
        )

    try:
        config = runtime_factory.make_runtime_config(
            workspace_config, workspace_root=workspace_root
        )
    except (ValueError, ImportError) as exc:
        return PreflightResult(
            available=False,
            reason=(
                "preflight requires the runtime call_llm config "
                f"(config.json `runtime.harness_call_llm` / `runtime.auxiliary_call_llm`): {exc}"
            ),
        )

    champion = Generation(
        id=champion_id,
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=_snapshot_root(workspace_root, epoch_id, champion_id),
        created_at="",
        promoted=True,
    )

    try:
        report, floor = await run_contract_preflight(
            adapter=adapter,
            generation=champion,
            board=list(draft.entries),
            weights=draft.scoring,
            config=config,
            workspace_root=workspace_root,
            epoch_id=epoch_id,
            runs=resolved_runs,
        )
    except ValueError as exc:
        # No mutation points under the champion snapshot — nothing to
        # degrade (and nothing an evolve loop could optimize either).
        return PreflightResult(available=False, reason=str(exc))

    return PreflightResult(
        available=True,
        verdict=report.verdict,
        report=report.to_json(),
        noise_floor=floor.to_json(),
    )


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
    (plus the workspace's live entrypoint / mutable-trees and the draft's
    proposer). Used by the dry-run preview so the operator sees the exact
    hash an apply would land — without touching the workspace.
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
        entrypoint = live_inputs.entrypoint
        mutable_trees = live_inputs.mutable_trees
    except FileNotFoundError:
        entrypoint = ""
        mutable_trees = ()

    with tempfile.TemporaryDirectory(prefix="zicato-builder-") as tmp:
        tmp_dir = Path(tmp)
        board_file = tmp_dir / "board.jsonl"
        brief_file = tmp_dir / "brief.md"
        scoring_file = tmp_dir / "scoring.json"
        # Thread the board_meta header exactly as _write_contract will, so
        # the dry-run's predicted hash equals the confirmed apply's hash.
        save_board(
            list(draft.entries),
            board_file,
            disable_drift=tuple(draft.disable_drift),
            judge_only=draft.judge_only,
        )
        brief_file.write_text(draft.brief, encoding="utf-8")
        import json as _json

        scoring_file.write_text(_json.dumps(scoring_to_dict(draft.scoring)), encoding="utf-8")
        return compute_contract_hash(
            ContractInputs(
                board_path=board_file,
                brief_path=brief_file,
                scoring_path=scoring_file,
                entrypoint=entrypoint,
                mutable_trees=mutable_trees,
                proposer_path=draft.proposer_path,
            )
        )


def _write_contract(draft: TournamentDraft, workspace_root: Path) -> None:
    """Write the draft to the workspace's LIVE contract source paths.

    Reuses the same canonical contract source locations ``zicato
    register`` / ``zicato epoch new`` publish (recorded under the
    ``contract`` key of ``config.json``, defaulting to the conventional
    location next to ``.zicato/``). Writing the board / brief / scoring
    there — and recording the resolved paths and proposer back into
    ``config.json`` — is exactly what the auto-epoch machinery reads on
    the next ``evolve`` / resolve, so the epoch rolls on its own. This
    function never rolls the epoch itself and never starts a run.
    """
    import json as _json

    from zicato.board.jsonl import save_board
    from zicato.epoch.contract import default_contract_paths
    from zicato.epoch.lifecycle import scoring_to_dict
    from zicato.workspace.config_io import read_workspace_config, write_workspace_config

    config = read_workspace_config(workspace_root)
    defaults = default_contract_paths(workspace_root)
    contract = dict(config.get("contract") or {})

    default_board = defaults["board_path"]
    default_brief = defaults["brief_path"]
    default_scoring = defaults["scoring_path"]
    assert default_board is not None and default_brief is not None
    assert default_scoring is not None

    board_target = Path(contract.get("board_path") or default_board)
    brief_target = Path(contract.get("brief_path") or contract.get("rubric_path") or default_brief)
    scoring_target = Path(contract.get("scoring_path") or default_scoring)

    board_target.parent.mkdir(parents=True, exist_ok=True)
    brief_target.parent.mkdir(parents=True, exist_ok=True)
    scoring_target.parent.mkdir(parents=True, exist_ok=True)

    # The board_meta header (disable_drift / judge_only) round-trips: the
    # draft carries it from load_current_board_with_meta and it is written
    # back here — a builder apply on a meta-carrying workspace must never
    # strip the header from the live contract.
    save_board(
        list(draft.entries),
        board_target,
        disable_drift=tuple(draft.disable_drift),
        judge_only=draft.judge_only,
    )
    brief_target.write_text(draft.brief, encoding="utf-8")
    scoring_target.write_text(
        _json.dumps(scoring_to_dict(draft.scoring), indent=2) + "\n", encoding="utf-8"
    )

    contract["board_path"] = str(board_target.resolve())
    contract["rubric_path"] = str(brief_target.resolve())
    contract["scoring_path"] = str(scoring_target.resolve())
    if draft.proposer_path is not None:
        contract["proposer_path"] = str(Path(draft.proposer_path).resolve())
    else:
        contract.pop("proposer_path", None)
    config["contract"] = contract
    write_workspace_config(workspace_root, config)


def apply(draft: TournamentDraft, workspace_root: Path, confirm: bool) -> ApplyResult:
    """Apply the draft, or preview it.

    When ``confirm`` is ``True`` the draft is written to the workspace's
    live contract source paths (board.jsonl, brief.md, scoring.json incl.
    tournament + overfitting + gate + weights, and the proposer dir) via
    :func:`_write_contract`, and the existing auto-epoch machinery rolls
    the epoch on the next resolve. When ``confirm`` is ``False`` nothing
    is written — the result is a dry-run preview carrying the diff, the
    predicted contract hash, and the cost.

    This function NEVER starts a live ``zicato evolve``.
    """
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

    _write_contract(draft, workspace_root)
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
    "PreflightResult",
    "ApplyResult",
    "preflight",
    "set_structure",
    "set_param",
    "set_holdout",
    "set_proposer",
    "set_weights",
    "set_gate",
    "set_namespace_weights",
    "set_proposer_quality",
    "set_experiment_memory",
    "set_telemetry_dialect",
    "set_screening",
    "edit_board_entry",
    "add_board_entry",
    "remove_board_entry",
    "add_judge",
    "remove_judge",
    "set_brief",
    "set_board_meta",
    "restore_draft",
    "estimate_cost",
    "validate",
    "compare_drafts",
    "apply",
    "VALID_TOURNAMENT_STRUCTURES",
]
