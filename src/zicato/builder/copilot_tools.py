"""Copilot tools — ADK FunctionTools wrapping the B1a builder ops.

The tournament-builder copilot (B1b) edits the SAME session draft the form
(B2) edits. It does that by calling the operations in
:mod:`zicato.builder.operations` — there is exactly one place each edit's
semantics live. This module exposes those operations as ADK tools so the
copilot's model can drive an edit while it reasons, and after every
mutation the tool returns a compact JSON summary (the
:class:`~zicato.builder.operations.DraftPatch` plus the current cost /
warnings) so the model sees the effect of its edit immediately.

Why a context var
-----------------
A tool function cannot carry the per-request ``(session_id, DraftStore,
workspace_root)`` as a bound argument: the copilot's ``LlmAgent`` is built
fresh per chat request, but the tools are plain module-level functions ADK
wraps once. So — mirroring :mod:`zicato.proposer.tools` — the tools read
their context from a module-level :class:`contextvars.ContextVar` that
:func:`zicato.builder.copilot.run_copilot` sets immediately around each
agent run via :func:`bind_builder_tool_context`. A ``ContextVar`` (not a
plain global) keeps two concurrent chat sessions from leaking each other's
draft.

apply is DRY-RUN ONLY here
--------------------------
:func:`preview_apply` calls :func:`zicato.builder.operations.apply` with
``confirm=False`` ALWAYS. The copilot may preview / propose applying, but
committing (which lets the auto-epoch machinery roll the epoch) stays a
deliberate UI action via ``POST /builder/apply {confirm:true}``. The
copilot must never roll the epoch itself, so no copilot tool ever calls
``apply`` with ``confirm=True``.

No ADK at import time
---------------------
This module imports no ``google.adk`` symbol. The tools are plain Python
callables; ADK wraps them as ``FunctionTool``s only when the copilot agent
is built (in :mod:`zicato.builder.copilot`). So importing
:mod:`zicato.builder` — and this module — never requires the optional
``google-adk`` extra.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.builder import operations as ops
from zicato.builder.draft import DraftStore, TournamentDraft
from zicato.core.types import JudgeMode, JudgeSpec, validate_board_entry


@dataclass(frozen=True)
class BuilderToolContext:
    """The per-request context a copilot tool reads.

    Set immediately around each agent run by
    :func:`bind_builder_tool_context` and read by every tool via the
    module-level context var. Carries the session id + the shared
    :class:`DraftStore` so the tool mutates the SAME draft the form's
    ``POST /builder/op`` mutates, and the workspace root so the cost /
    diff / dry-run preview resolve against the live contract.

    Fields
    ------
    session_id:
        The builder session whose draft the tools edit.
    store:
        The shared draft store — the tools fetch ``session_id``'s draft
        from it, so mutations persist for a subsequent ``GET
        /builder/draft``.
    workspace_root:
        The ``.zicato`` workspace root the cost / diff / dry-run preview
        resolve against.
    """

    session_id: str
    store: DraftStore
    workspace_root: Path

    def draft(self) -> TournamentDraft:
        """Return this session's draft from the shared store.

        Also records the current state onto the session's bounded undo
        history (:meth:`DraftStore.remember`) — this is the copilot
        front door's pre-op capture seam, mirroring ``builder_op``'s
        remember-before-dispatch, so a form edit and a chat edit share
        ONE undo history. ``remember`` dedups by field equality, so a
        read tool fetching the draft records nothing.
        """
        draft = self.store.get(self.session_id, self.workspace_root)
        self.store.remember(self.session_id)
        return draft


_TOOL_CONTEXT: contextvars.ContextVar[BuilderToolContext | None] = contextvars.ContextVar(
    "zicato_builder_tool_context",
    default=None,
)


@contextmanager
def bind_builder_tool_context(ctx: BuilderToolContext) -> Iterator[None]:
    """Bind ``ctx`` as the active builder-tool context for the block.

    Sets the module-level context var on entry and RESETS it to its prior
    value on exit (even on exception), so a concurrent chat session under
    its own ``bind_builder_tool_context`` never sees this block's context
    and nothing leaks past the block. Used by
    :func:`zicato.builder.copilot.run_copilot` to wrap each agent run.
    """
    token = _TOOL_CONTEXT.set(ctx)
    try:
        yield
    finally:
        _TOOL_CONTEXT.reset(token)


def _active_context() -> BuilderToolContext:
    """Return the bound context or raise a clear out-of-context error.

    A tool called outside a :func:`bind_builder_tool_context` block has no
    session / store to edit; raising here (rather than silently editing a
    phantom draft) makes the misuse obvious — the copilot tools may only be
    called from within a :func:`zicato.builder.copilot.run_copilot` run.
    """
    ctx = _TOOL_CONTEXT.get()
    if ctx is None:
        raise RuntimeError(
            "builder tool called with no bound BuilderToolContext; builder "
            "tools may only be called from within a copilot run (see "
            "bind_builder_tool_context)"
        )
    return ctx


def _summary(patch: ops.DraftPatch) -> dict[str, Any]:
    """Render the post-mutation result the model sees after an edit.

    Carries the :class:`DraftPatch` plus the current cost headline and the
    validation warnings, so the model can react to the consequence of its
    edit (a cost jump, a new warning) on the very next turn — and the SSE
    layer reuses the SAME shape to push a live ``patch`` frame to the form
    (so the two stay byte-compatible with the REST ``op`` response).
    """
    ctx = _active_context()
    draft = ctx.draft()
    cost = ops.estimate_cost(draft)
    warns = ops.validate(draft, ctx.workspace_root)
    diff = draft.diff_vs_live(ctx.workspace_root)
    return {
        "patch": patch.to_dict(),
        "cost": cost.to_dict(),
        "warnings": [w.to_dict() for w in warns],
        "diff": diff.to_dict(),
    }


def _result_json(payload: dict[str, Any]) -> str:
    """Serialize a tool result compactly for the model's context window."""
    return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Write tools — each mutates the shared session draft via a B1a op.
# ---------------------------------------------------------------------------


def set_structure(structure: str) -> str:
    """Set the tournament structure (e.g. ``gauntlet`` / ``swiss`` / ``racing``).

    Mutates the session draft and returns the patch + updated cost /
    warnings. Invalid structure tokens are reported as an ``error``.
    """
    ctx = _active_context()
    try:
        patch = ops.set_structure(ctx.draft(), structure)
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


def set_param(key: str, value: str | None = None) -> str:
    """Set one structure param (``field_size``, ``replicates``, ``rounds_n``, …).

    ``value`` is a JSON literal token: a number (``"4"``, ``"0.25"``), a
    boolean (``"true"``), a string, or ``null`` to REMOVE the key. The
    token is JSON-decoded so a numeric param lands as a number (the cost
    arithmetic reads it as an int / float); a token that is not valid JSON
    is stored verbatim as a string. Returns the patch + updated cost /
    warnings.
    """
    ctx = _active_context()
    patch = ops.set_param(ctx.draft(), key, _decode_param_value(value))
    return _result_json(_summary(patch))


def _decode_param_value(value: str | None) -> Any:
    """Decode a ``set_param`` value token into its JSON-native type.

    ``None`` ⇒ ``None`` (removes the key). Otherwise the token is parsed as
    a JSON literal so ``"4"`` becomes ``4`` and ``"true"`` becomes ``True``;
    a non-JSON token (a bare word) is kept as the literal string.
    """
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def set_holdout(
    enabled: bool | None = None,
    fraction: float | None = None,
    tags: list[str] | None = None,
    min_board_size_for_split: int | None = None,
    rotate_holdout: bool | None = None,
    restrict_proposer_visibility: bool | None = None,
    random_baseline_every_n: int | None = None,
    max_generations_per_contract: int | None = None,
    ladder: dict[str, Any] | None = None,
) -> str:
    """Edit the train/holdout split + the full anti-overfitting config.

    ``enabled`` / ``fraction`` tune the hash-derived split; ``tags`` sets
    the explicit per-entry holdout ids. The rest covers the whole
    overfitting contract: ``min_board_size_for_split`` (the split floor),
    ``rotate_holdout`` (a fresh ~fraction slice each epoch),
    ``restrict_proposer_visibility`` (band/aggregate what the proposer
    sees), ``random_baseline_every_n`` (the PLACEBO cadence — every Nth
    round fields one no-op challenger the gate MUST reject; a promoted
    placebo is the gate-discrimination alarm; 0 = off),
    ``max_generations_per_contract`` (board-refresh recommendation
    ceiling; 0 clears it), and ``ladder`` — a partial mapping over the
    Ladder/Thresholdout governor (``enabled`` / ``threshold`` /
    ``budget`` / ``noise_scale``; ``"threshold": null`` resets to
    auto-derive from promote_margin). Any subset may be supplied; every
    change rolls the epoch. Returns the patch + updated cost / warnings.
    """
    ctx = _active_context()
    try:
        patch = ops.set_holdout(
            ctx.draft(),
            enabled=enabled,
            fraction=fraction,
            tags=tags,
            min_board_size_for_split=min_board_size_for_split,
            rotate_holdout=rotate_holdout,
            restrict_proposer_visibility=restrict_proposer_visibility,
            random_baseline_every_n=random_baseline_every_n,
            max_generations_per_contract=max_generations_per_contract,
            ladder=ladder,
        )
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


def set_proposer(proposer_path: str | None = None) -> str:
    """Point the draft at a proposer dir, or ``null`` for the built-in default."""
    ctx = _active_context()
    patch = ops.set_proposer(ctx.draft(), proposer_path)
    return _result_json(_summary(patch))


def set_weights(
    drift_weight: float | None = None,
    pass_weight: float | None = None,
    per_kind_weights: dict[str, float] | None = None,
    per_judge_weights: dict[str, float] | None = None,
    default_judge_weight: float | None = None,
    plan_revision_weight: float | None = None,
    runtime_weight: float | None = None,
    severity_weights: dict[str, float] | None = None,
) -> str:
    """Set scoring weights (the loss-shaping knobs).

    Any subset of the supported weight fields may be supplied; mapping
    fields replace the whole mapping. Returns the patch + updated cost /
    warnings.
    """
    ctx = _active_context()
    patch = ops.set_weights(
        ctx.draft(),
        drift_weight=drift_weight,
        pass_weight=pass_weight,
        per_kind_weights=per_kind_weights,
        per_judge_weights=per_judge_weights,
        default_judge_weight=default_judge_weight,
        plan_revision_weight=plan_revision_weight,
        runtime_weight=runtime_weight,
        severity_weights=severity_weights,
    )
    return _result_json(_summary(patch))


def set_gate(
    promote_margin: float | None = None,
    monotonicity: bool | None = None,
    monotonicity_scope: str | None = None,
    namespace_monotonicity: dict[str, bool] | None = None,
    block_on_containment_violation: bool | None = None,
    block_on_gate_contradiction: bool | None = None,
    regression_gate_enabled: bool | None = None,
    regression_test_command: list[str] | None = None,
    regression_timeout_s: int | None = None,
) -> str:
    """Set the promote gate: margin, monotonicity, and the hard blocks.

    ``monotonicity`` toggles the pass-rate guard on/off. When on,
    ``monotonicity_scope`` selects its granularity: ``"per_entry"``
    (default) rejects if any champion-passed entry flips to fail — right
    for invariant / regression-suite boards; ``"aggregate"`` rejects only
    if the overall pass-rate drops — right for sampled evaluation boards
    where a strictly-better challenger should not be vetoed by one entry
    flip. ``namespace_monotonicity`` replaces the per-namespace
    strict-monotonicity flag mapping wholesale (e.g. ``{"rubric:": true}``).
    The two ``block_on_*`` booleans upgrade the integrity notary from
    alarm-only to BLOCKING (containment violations / gate
    contradictions). The ``regression_*`` trio runs the snapshot's own
    test suite as a hard pre-gate (argv list + timeout seconds >= 1).
    Returns the patch + updated cost / warnings, or an ``error`` for an
    invalid scope / command / timeout.
    """
    ctx = _active_context()
    try:
        patch = ops.set_gate(
            ctx.draft(),
            promote_margin=promote_margin,
            monotonicity=monotonicity,
            monotonicity_scope=monotonicity_scope,
            namespace_monotonicity=namespace_monotonicity,
            block_on_containment_violation=block_on_containment_violation,
            block_on_gate_contradiction=block_on_gate_contradiction,
            regression_gate_enabled=regression_gate_enabled,
            regression_test_command=regression_test_command,
            regression_timeout_s=regression_timeout_s,
        )
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


def set_namespace_weights(
    namespace_weights: dict[str, float] | None = None,
    diff_complexity_weight: float | None = None,
    diff_complexity_ceiling: float | None = None,
) -> str:
    """Set the multi-objective namespace coefficients + the parsimony term.

    ``namespace_weights`` replaces the whole per-namespace coefficient
    mapping (keys keep the trailing colon, e.g. ``"drift:"``; the sign
    encodes the "worse" direction — positive = higher is worse, negative
    = higher is better, zero = tracked but unscored).
    ``diff_complexity_weight`` is the opt-in MDL/parsimony coefficient
    (0 = exactly absent; must be >= 0 — it biases selection toward the
    smaller, more general edit). ``diff_complexity_ceiling`` is the paired
    opt-in parsimony CEILING (0 = OFF; must be >= 0 — a challenger whose
    diff complexity exceeds it is rejected outright by the gate). Changing
    any rolls the epoch.
    """
    ctx = _active_context()
    try:
        patch = ops.set_namespace_weights(
            ctx.draft(),
            namespace_weights=namespace_weights,
            diff_complexity_weight=diff_complexity_weight,
            diff_complexity_ceiling=diff_complexity_ceiling,
        )
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


def set_proposer_quality(
    best_of_n: int | None = None,
    critique_enabled: bool | None = None,
    process_exemplars: int | None = None,
    recombine: bool | None = None,
    genealogy: int | None = None,
    calibration_feedback: int | None = None,
    recombine_merge: str | None = None,
) -> str:
    """Set the proposer-quality levers: best-of-N slate + self-critique.

    ``best_of_n`` is how many candidate experiments each propose-step
    samples before selection (1 = the historical single sample, no
    critique; must be >= 1); ``critique_enabled`` toggles the auxiliary
    self-critique selection pass (inert at best_of_n 1);
    ``process_exemplars`` opts the proposer into up to that many REDACTED
    drift-anchored event windows per round (0 = off, the default; it
    touches the overfitting boundary — point the operator at
    docs/design/PROCESS-EXEMPLARS.md §5, the harm-detection runbook,
    before setting it; read-side only, no cost-meter impact).
    ``recombine`` opts in the mechanical recombination slot (WS-REC):
    when True the last best-of-N slot mints the patch union of two
    rejected complementary challengers instead of sampling the LLM —
    REQUIRES best_of_n > 1 to have effect, and is cost-neutral (the mint
    REPLACES that slot's auxiliary propose call, never adds one).
    Flipping it rolls the epoch. ``recombine_merge`` (``"mechanical"``
    default | ``"llm"``) chooses HOW the slot composes the union:
    ``"mechanical"`` mints the disjoint patch concatenation with no LLM
    call; ``"llm"`` issues one merge call (relaxing disjointness so an
    OVERLAPPING pair the mechanical mint cannot touch can be merged).
    Meaningful only with recombine on; ``"llm"`` rolls the epoch.
    ``genealogy`` opts in the genealogy
    channel (WS-GENE): up to that many candidate-lineage items — the
    champion's promoted patch history + diverse rejected reign candidates,
    each with a banded outcome — are spliced into the prompt so the
    proposer can evolve in context (0 = off, the default; read-side only,
    no cost-meter impact). ``calibration_feedback`` opts in the
    critic-calibration channel (WS-CAL): up to that many RECENT graded
    hypotheses — the proposer's own falsifiable predictions graded against
    realized outcomes (hit / miss / unresolved counts + the overall
    calibration fraction + banded per-claim outcomes) — are spliced into
    the prompt so the proposer sees its OWN miss pattern and predicts more
    honestly (0 = off, the default; read-side only, no cost-meter impact).
    The screen (tryout) knobs live on `set_screening` — the ops COMPOSE on
    the same proposer_quality contract block. Changing any rolls the epoch.
    Returns the patch + updated cost / warnings.
    """
    ctx = _active_context()
    try:
        patch = ops.set_proposer_quality(
            ctx.draft(),
            best_of_n=best_of_n,
            critique_enabled=critique_enabled,
            process_exemplars=process_exemplars,
            recombine=recombine,
            genealogy=genealogy,
            calibration_feedback=calibration_feedback,
            recombine_merge=recombine_merge,
        )
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


def set_experiment_memory(cross_epoch: bool | None = None) -> str:
    """Set the experiment-memory scoping (what settled history the proposer sees).

    ``cross_epoch=True`` opts settled experiments from PRIOR epochs that
    share the current contract hash into the proposer's digest (banded;
    same-epoch history keeps budget priority); ``False`` (default) is
    same-epoch-only. A contract change — it rolls the epoch.
    """
    ctx = _active_context()
    patch = ops.set_experiment_memory(ctx.draft(), cross_epoch=cross_epoch)
    return _result_json(_summary(patch))


def set_telemetry_dialect(dialect: str | None = None) -> str:
    """Set the telemetry dialect — the producer that reduces raw telemetry into the LossProfile.

    ``goldfive`` (default): the full drift-instrument stream — the most
    powerful dialect, the only one carrying in-process drift instruments,
    custom process-judge drift, and emulator introspection.
    ``adk_events``: a generic agent event-log JSONL (tool-call / tool-response
    / transfer / error / model-usage) — no in-process drift instruments and
    no custom process-judge drift, but it recovers the failure / cost / loop
    envelope.
    ``transcript``: the floor — no telemetry at all; the drift term is
    structurally zero and scoring degrades to predicates + optional in-run
    judges only. Changing the dialect selects champions under a different
    measurement rule and rolls the epoch. Returns an ``error`` for an unknown
    dialect name.
    """
    ctx = _active_context()
    try:
        patch = ops.set_telemetry_dialect(ctx.draft(), dialect=dialect)
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


def set_mutation_surface(mutation_surface: dict[str, Any] | None = None) -> str:
    """Declare which file types carry mutation sites.

    ``mutation_surface`` is the whole table keyed by file suffix —
    ``{".ts": {"leaders": ["//", "/*"], "trailers": ["*/"]}}`` — folded
    over the built-ins (``.md``, ``.markdown``, ``.txt``, ``.yaml``,
    ``.yml``, ``.toml``, and the reserved ``.py``). ``leaders`` are the
    comment lead-ins a marker may be written under and are required:
    zicato strips echoed marker lines out of a rewritten region under
    them, so a file type with no declared comment syntax is one whose
    regions it cannot keep contained. ``trailers`` are optional
    end-of-line block closers (``*/``, ``-->``).

    Pass ``{}`` to go back to the built-ins alone. Widening the surface
    changes what the proposer may rewrite, so it rolls the epoch. Returns
    an ``error`` for a malformed table or an entry for ``.py``.
    """
    ctx = _active_context()
    try:
        patch = ops.set_mutation_surface(ctx.draft(), mutation_surface=mutation_surface)
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


def set_screening(entries: int | None = None, veto_only: bool | None = None) -> str:
    """Set the pre-tournament candidate screen (tryouts).

    ``entries`` sizes the rotating train panel each best-of-N slate
    candidate runs BEFORE the selection pass (``0`` = screen off; the
    recommended scaffold uses ``2``) — veto-first semantics: a candidate
    with a confirmed catastrophic regression is disqualified, the critic
    chooses among the survivors. ``veto_only=True`` keeps the veto but
    withholds the screen's counts from the selection tiebreak. Changing
    either rolls the epoch (a contract change). Returns an ``error`` for
    a negative ``entries``.
    """
    ctx = _active_context()
    try:
        patch = ops.set_screening(ctx.draft(), entries=entries, veto_only=veto_only)
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


def edit_board_entry(entry: dict[str, Any]) -> str:
    """Add or replace a board entry (matched by id).

    ``entry`` is the JSON shape of a board entry (the same shape the REST
    ``op`` accepts); it is validated before it lands. Returns the patch +
    updated cost / warnings, or an ``error`` on a malformed entry.
    """
    ctx = _active_context()
    try:
        board_entry = validate_board_entry(entry)
        patch = ops.edit_board_entry(ctx.draft(), board_entry)
    except (ValueError, KeyError, TypeError) as exc:
        return _result_json({"error": f"invalid board entry: {exc}"})
    return _result_json(_summary(patch))


def add_board_entry(entry: dict[str, Any]) -> str:
    """Append a NEW board entry — the add beside edit_board_entry's add/replace.

    ``entry`` is the JSON shape of a board entry (the same shape the REST ``op``
    accepts); it is validated before it lands and a duplicate id is REFUSED (use
    edit_board_entry to replace). Returns the patch + updated cost / warnings, or
    an ``error`` on a malformed / colliding entry. A board change — it rolls the
    epoch.
    """
    ctx = _active_context()
    try:
        board_entry = validate_board_entry(entry)
        patch = ops.add_board_entry(ctx.draft(), board_entry)
    except (ValueError, KeyError, TypeError) as exc:
        return _result_json({"error": f"invalid board entry: {exc}"})
    return _result_json(_summary(patch))


def remove_board_entry(entry_id: str) -> str:
    """Remove a board entry by id — the delete beside edit_board_entry.

    Returns the patch + updated cost / warnings, or an ``error`` when no
    entry carries the id. A board change — it rolls the epoch.
    """
    ctx = _active_context()
    try:
        patch = ops.remove_board_entry(ctx.draft(), entry_id)
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


def revert_to_live() -> str:
    """Discard the session draft's edits: restore it from the LIVE contract.

    Restores IN PLACE from a fresh read of the workspace's running
    contract, so the session (and any slot it is bound to) lands exactly
    on live. The pre-revert state is remembered first — `undo` brings
    the discarded edits back. Writes nothing to the workspace.
    """
    ctx = _active_context()
    draft = ctx.draft()  # records the pre-revert state for undo
    patch = ops.restore_draft(draft, TournamentDraft.from_workspace(ctx.workspace_root))
    return _result_json(_summary(patch))


def undo() -> str:
    """Undo the most recent draft edit (bounded 20-step history).

    Both front doors — the form's op dispatch and every copilot write
    tool — record the pre-op state onto one shared per-session history,
    so this undoes the latest edit regardless of which door made it.
    Restores in place; returns the patch + updated cost / warnings, or a
    "nothing to undo" note when the history is exhausted. Writes nothing
    to the workspace.
    """
    ctx = _active_context()
    draft = ctx.store.get(ctx.session_id, ctx.workspace_root)
    snapshot = ctx.store.pop_undo(ctx.session_id)
    if snapshot is None:
        patch = ops.DraftPatch(op="undo", note="nothing to undo")
    else:
        patch = ops.restore_draft(draft, snapshot, op="undo")
    return _result_json(_summary(patch))


def add_judge(entry_id: str, judge: dict[str, Any]) -> str:
    """Add a process judge to a board entry.

    ``judge`` is the JSON shape ``{name, mode, body, severity}``. Returns
    the patch + updated cost / warnings, or an ``error`` when the entry is
    unknown or a judge of the same name already exists.
    """
    ctx = _active_context()
    try:
        judge_spec = _judge_from_dict(judge)
        patch = ops.add_judge(ctx.draft(), entry_id, judge_spec)
    except (ValueError, KeyError, TypeError) as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


def remove_judge(entry_id: str, name: str) -> str:
    """Remove the named process judge from a board entry.

    Returns the patch + updated cost / warnings, or an ``error`` when the
    entry is unknown (removing an absent judge is a no-op, reported in the
    patch note).
    """
    ctx = _active_context()
    try:
        patch = ops.remove_judge(ctx.draft(), entry_id, name)
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


def set_brief(text: str) -> str:
    """Replace the proposer-brief text. Returns the patch + cost / warnings."""
    ctx = _active_context()
    patch = ops.set_brief(ctx.draft(), text)
    return _result_json(_summary(patch))


def set_board_meta(
    disable_drift: list[str] | None = None,
    judge_only: bool | None = None,
) -> str:
    """Set the board-level ``board_meta`` header (drift suppression + judge-only).

    ``disable_drift`` replaces the whole board-level drift-suppression
    set wholesale (lowercase drift-kind tokens, e.g. ``["off_topic"]``;
    an empty list clears the set; ``null`` leaves it unchanged).
    ``judge_only`` toggles no-steering evaluation (goldfive judges
    observe without steering). A contract change — the header folds into
    the board's contract hash, so it rolls the epoch. Unknown drift-kind
    tokens are reported as an ``error``.
    """
    ctx = _active_context()
    try:
        patch = ops.set_board_meta(
            ctx.draft(),
            disable_drift=disable_drift,
            judge_only=judge_only,
        )
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json(_summary(patch))


# ---------------------------------------------------------------------------
# Read tools — cost + validate + dry-run preview.
# ---------------------------------------------------------------------------


def estimate_cost() -> str:
    """Return the current board-runs-per-round cost estimate for the draft."""
    ctx = _active_context()
    return _result_json({"cost": ops.estimate_cost(ctx.draft()).to_dict()})


def validate() -> str:
    """Return the current advisory validation warnings for the draft.

    Includes the statistical margin-vs-noise-floor rule when the current
    epoch's record carries a measured A/A floor (severity ``refuse`` —
    recommend-only, never blocking).
    """
    ctx = _active_context()
    return _result_json(
        {"warnings": [w.to_dict() for w in ops.validate(ctx.draft(), ctx.workspace_root)]}
    )


async def preflight(runs: int | None = None) -> str:
    """Measure the DRAFT contract's noise floor + degradation signal.

    The build-time statistical pre-flight (the same measurement `zicato
    board preflight` takes, run against the DRAFT's board + scoring):
    (a) K A/A draws of the workspace's champion measure the noise floor;
    (b) a deliberately-degraded ephemeral copy measures the achievable
    signal. Verdict `ok` / `warn` (saturated — the board cannot
    discriminate) / `refuse` (signal at or below the floor — duels would
    be decided by noise). RECOMMEND-ONLY, never a gate. Degrades honestly
    (`available: false` + a reason) when the workspace has no registered
    target / seeded baseline / runtime call_llm config. ``runs`` defaults
    to 5 A/A draws; re-running is cache-idempotent.
    """
    ctx = _active_context()
    try:
        result = await ops.preflight(ctx.draft(), ctx.workspace_root, runs=runs)
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    payload: dict[str, Any] = {"preflight": result.to_dict()}
    floor = (result.noise_floor or {}).get("max_abs_delta")
    payload["warnings"] = [
        w.to_dict()
        for w in ops.validate(
            ctx.draft(),
            ctx.workspace_root,
            noise_floor_max_abs_delta=floor if isinstance(floor, int | float) else None,
        )
    ]
    return _result_json(payload)


def fork(name: str) -> str:
    """Fork the working draft into a NAMED slot and switch to it.

    Snapshots the session's current draft as ``name`` (1-64 chars of
    [A-Za-z0-9._-]) — the fork/compare way to iterate on contract
    variants WITHOUT rolling the epoch. Subsequent edits accumulate on
    the named slot; `switch` moves between slots with their state intact;
    `compare` diffs any two. Never overwrites an existing name (returns
    an ``error`` instead). Applying still writes whichever draft the
    session is on.
    """
    ctx = _active_context()
    try:
        ctx.store.fork(ctx.session_id, name, ctx.workspace_root)
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    patch = ops.DraftPatch(op="fork", changed={"name": name})
    return _result_json({**_summary(patch), "drafts": ctx.store.list_drafts()})


def switch(name: str) -> str:
    """Switch the session to the named draft slot (its state intact).

    The previous slot keeps every edit. Returns the patch + the switched
    draft's cost / warnings / diff, or an ``error`` for an unknown name.
    """
    ctx = _active_context()
    try:
        ctx.store.switch(ctx.session_id, name)
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    patch = ops.DraftPatch(op="switch", changed={"name": name})
    return _result_json({**_summary(patch), "drafts": ctx.store.list_drafts()})


def list_drafts() -> str:
    """List the named draft slots (the fork/compare variants)."""
    ctx = _active_context()
    return _result_json({"drafts": ctx.store.list_drafts()})


def compare(name_a: str, name_b: str) -> str:
    """Keyed diff between two drafts — slots, ``"session"``, or ``"live"``.

    ``"session"`` is the current working draft; ``"live"`` is the
    workspace's running contract; anything else names a fork slot. The
    diff is keyed the way the epoch-roll rule sees it: differing
    contract-canonical scoring keys (with both values), board entry ids
    added/removed/changed, the brief, the proposer. Read-only.
    """
    ctx = _active_context()

    def _resolve(name: str) -> TournamentDraft:
        if name == "session":
            return ctx.draft()
        if name == "live":
            return TournamentDraft.from_workspace(ctx.workspace_root)
        slot = ctx.store.slot(name)
        if slot is None:
            known = ", ".join(["session", "live", *ctx.store.list_drafts()])
            raise ValueError(f"no draft named {name!r} (known: {known})")
        return slot

    try:
        diff = ops.compare_drafts(_resolve(name_a), _resolve(name_b))
    except ValueError as exc:
        return _result_json({"error": str(exc)})
    return _result_json({"compare": {"a": name_a, "b": name_b, **diff}})


def preview_apply() -> str:
    """Dry-run the apply: preview the diff / predicted hash / cost.

    ALWAYS runs with ``confirm=False`` — this tool writes NOTHING and never
    rolls the epoch. The copilot uses it to show the operator what applying
    WOULD do (which contract components differ, whether it would roll the
    epoch, the predicted contract hash); committing stays a deliberate UI
    action via ``POST /builder/apply {confirm:true}``.
    """
    ctx = _active_context()
    try:
        result = ops.apply(ctx.draft(), ctx.workspace_root, confirm=False)
    except FileNotFoundError as exc:
        return _result_json({"error": str(exc)})
    return _result_json({"preview": result.to_dict()})


def _judge_from_dict(raw: Any) -> JudgeSpec:
    """Reconstruct a :class:`JudgeSpec` from its JSON shape.

    Mirrors :func:`zicato.builder.api._judge_from_dict` so the copilot and
    the REST path coerce a judge identically (same enum handling, same
    clear failure on a bad ``mode`` / ``severity`` token).
    """
    from zicato.core.drift_kinds import DriftSeverity  # noqa: PLC0415

    if not isinstance(raw, dict):
        raise ValueError("judge must be a JSON object")
    return JudgeSpec(
        name=str(raw["name"]),
        mode=JudgeMode(str(raw.get("mode", "inline"))),
        body=str(raw["body"]),
        severity=DriftSeverity(str(raw.get("severity", "warning"))),
    )


#: The full builder tool set the copilot agent opts into. Covers the
#: mutate ops + ``estimate_cost`` + ``validate`` + the DRY-RUN-ONLY
#: ``preview_apply``. The copilot agent does ``tools=list(DEFAULT_BUILDER_TOOLS)``;
#: ADK wraps each as a ``FunctionTool`` automatically.
DEFAULT_BUILDER_TOOLS = (
    set_structure,
    set_param,
    set_holdout,
    set_proposer,
    set_weights,
    set_gate,
    set_namespace_weights,
    set_proposer_quality,
    set_experiment_memory,
    set_telemetry_dialect,
    set_mutation_surface,
    set_screening,
    edit_board_entry,
    add_board_entry,
    remove_board_entry,
    add_judge,
    remove_judge,
    set_brief,
    set_board_meta,
    estimate_cost,
    validate,
    preflight,
    fork,
    switch,
    list_drafts,
    compare,
    revert_to_live,
    undo,
    preview_apply,
)


__all__ = [
    "DEFAULT_BUILDER_TOOLS",
    "BuilderToolContext",
    "bind_builder_tool_context",
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
    "set_mutation_surface",
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
    "preflight",
    "fork",
    "switch",
    "list_drafts",
    "compare",
    "revert_to_live",
    "undo",
    "preview_apply",
]
