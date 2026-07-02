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
        """Return this session's draft from the shared store."""
        return self.store.get(self.session_id, self.workspace_root)


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
    warns = ops.validate(draft)
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
) -> str:
    """Edit the train/holdout split (enable, hash-fraction, explicit tags).

    Any subset of ``enabled`` / ``fraction`` / ``tags`` may be supplied.
    Returns the patch + updated cost / warnings.
    """
    ctx = _active_context()
    patch = ops.set_holdout(ctx.draft(), enabled=enabled, fraction=fraction, tags=tags)
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
) -> str:
    """Set the promote gate: the margin floor + pass-rate monotonicity.

    ``monotonicity`` toggles the pass-rate guard on/off. When on,
    ``monotonicity_scope`` selects its granularity: ``"per_entry"``
    (default) rejects if any champion-passed entry flips to fail — right
    for invariant / regression-suite boards; ``"aggregate"`` rejects only
    if the overall pass-rate drops — right for sampled evaluation boards
    where a strictly-better challenger should not be vetoed by one entry
    flip.
    """
    ctx = _active_context()
    patch = ops.set_gate(
        ctx.draft(),
        promote_margin=promote_margin,
        monotonicity=monotonicity,
        monotonicity_scope=monotonicity_scope,
    )
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


# ---------------------------------------------------------------------------
# Read tools — cost + validate + dry-run preview.
# ---------------------------------------------------------------------------


def estimate_cost() -> str:
    """Return the current board-runs-per-round cost estimate for the draft."""
    ctx = _active_context()
    return _result_json({"cost": ops.estimate_cost(ctx.draft()).to_dict()})


def validate() -> str:
    """Return the current advisory validation warnings for the draft."""
    ctx = _active_context()
    return _result_json({"warnings": [w.to_dict() for w in ops.validate(ctx.draft())]})


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
    from goldfive import DriftSeverity  # noqa: PLC0415

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
    set_screening,
    edit_board_entry,
    add_judge,
    remove_judge,
    set_brief,
    estimate_cost,
    validate,
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
    "set_screening",
    "edit_board_entry",
    "add_judge",
    "remove_judge",
    "set_brief",
    "estimate_cost",
    "validate",
    "preview_apply",
]
