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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.board.split import HOLDOUT_TAG, split_board
from zicato.builder.draft import TournamentDraft
from zicato.core.types import (
    VALID_TOURNAMENT_STRUCTURES,
    BoardEntry,
    JudgeSpec,
    ScoringWeights,
    TournamentStructure,
)

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
        Board-runs this term contributes.
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
        ``"info"`` (advisory) / ``"warning"`` (likely a mistake). The
        builder never blocks on these — they inform the operator's choice.
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
) -> DraftPatch:
    """Edit the train/holdout split.

    ``enabled`` / ``fraction`` tune the hash-derived split on
    :class:`OverfittingConfig`; ``tags`` sets the explicit per-entry
    ``holdout`` tag exactly on the supplied ids (every other entry loses
    the tag). Any subset of the three may be supplied.
    """
    changed: dict[str, Any] = {}
    of = draft.scoring.overfitting
    of_changes: dict[str, Any] = {}
    if enabled is not None and enabled != of.enabled:
        of_changes["enabled"] = enabled
        changed["enabled"] = {"from": of.enabled, "to": enabled}
    if fraction is not None and fraction != of.holdout_fraction:
        of_changes["holdout_fraction"] = fraction
        changed["holdout_fraction"] = {"from": of.holdout_fraction, "to": fraction}
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
) -> DraftPatch:
    """Set the promote gate: the margin floor + pass-rate monotonicity.

    ``monotonicity`` is the on/off switch; ``monotonicity_scope`` selects
    the granularity when it is on (``"per_entry"`` — default, every
    champion-passed entry must hold — or ``"aggregate"`` — only the overall
    pass-rate may not regress; see SCORING.md §5). An invalid scope token
    raises rather than silently coercing.
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
    if scoring_changes:
        draft.scoring = _replace_scoring(draft, **scoring_changes)
    return DraftPatch(op="set_gate", changed=changed)


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

    The estimate is deliberately a coarse upper-ish bound for the
    cost-meter — the exact schedule is the selection strategy's; this
    surfaces the order of magnitude before the operator commits.
    """
    ts = draft.scoring.tournament_structure
    params = ts.params
    train_ids, holdout_ids = split_board(draft.entries, draft.scoring.overfitting)
    board_size = len(train_ids)
    holdout_size = len(holdout_ids)
    replicates = max(1, _param_int(params, "replicates", 1))
    field_size = max(1, _param_int(params, "field_size", 2))

    lines: list[CostLine] = []
    structure = ts.structure

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


def validate(draft: TournamentDraft) -> list[Warning]:
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

    return warnings


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
    from zicato.epoch.lifecycle import _scoring_to_dict

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
        save_board(list(draft.entries), board_file)
        brief_file.write_text(draft.brief, encoding="utf-8")
        import json as _json

        scoring_file.write_text(_json.dumps(_scoring_to_dict(draft.scoring)), encoding="utf-8")
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
    from zicato.cli.common import read_workspace_config, write_workspace_config
    from zicato.epoch.contract import default_contract_paths
    from zicato.epoch.lifecycle import _scoring_to_dict

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

    save_board(list(draft.entries), board_target)
    brief_target.write_text(draft.brief, encoding="utf-8")
    scoring_target.write_text(
        _json.dumps(_scoring_to_dict(draft.scoring), indent=2) + "\n", encoding="utf-8"
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
    warns = tuple(validate(draft))
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
    "ApplyResult",
    "set_structure",
    "set_param",
    "set_holdout",
    "set_proposer",
    "set_weights",
    "set_gate",
    "edit_board_entry",
    "add_judge",
    "remove_judge",
    "set_brief",
    "estimate_cost",
    "validate",
    "apply",
    "VALID_TOURNAMENT_STRUCTURES",
]
