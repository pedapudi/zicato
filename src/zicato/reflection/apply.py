"""``reflect apply`` — carry a finding's proposed edit to a builder draft.

The finding→builder seam. A reflection finding carries an executable
``proposed_op`` (a real builder op validated against its signature at emit
time; BOARD-REFLECTION.md verdict 6). :func:`apply_finding_to_draft` loads that
finding, **forks a builder draft** from the LIVE contract, and applies the op to
the draft — it NEVER writes the sealed contract. The operator reviews the
resulting draft diff and seals it through the builder, which is the gated step
that rolls the epoch. The recommend-only invariant holds end to end: reflection
diagnoses and stages, the operator (through the builder) decides.

This module lives in :mod:`zicato.reflection` — NOT the CLI — precisely because
the CLI must not import the builder directly (the import-linter ``cli -> dashboard
-> builder`` declared-edge contract), whereas ``zicato.reflection`` is a library
already permitted the builder edge (R3's ``findings`` signature validation).
``zicato reflect apply`` calls in here; the builder dependency stays on the
reflection side of the boundary.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The forked draft slot name derives from the reflection id, clamped to the
#: DraftStore's slot-name grammar (1-64 chars of ``[A-Za-z0-9._-]`` starting
#: alphanumeric). ``reflect-{reflection_id[:12]}`` always starts with ``r``.
_SLOT_STEM = "reflect-"
_UNSAFE_SLOT_CHAR = re.compile(r"[^A-Za-z0-9._-]")


class FindingNotFoundError(LookupError):
    """No finding with the requested id in the reflection's ``findings.json``."""


class FindingNotActionableError(ValueError):
    """The finding exists but carries no ``proposed_op`` — nothing to apply.

    A recommendation-only finding (a missed-fire pile, an untested judge, an
    ambiguous pile) names the evidence but leaves the fix an authoring
    decision; there is no mechanical op to stage.
    """


class SuggestionNotFoundError(LookupError):
    """No suggestion with the requested id in the reflection's ``suggestions.json``."""


@dataclass(frozen=True, slots=True)
class AppliedFinding:
    """The result of staging one finding's op onto a forked draft."""

    reflection_id: str
    finding_id: str
    slot_name: str
    op: str
    args: dict[str, Any]
    #: The builder ``DraftPatch.to_dict()`` describing what the op changed.
    patch: dict[str, Any]
    #: The forked draft's diff vs the live contract (``ContractDiff.to_dict()``).
    diff: dict[str, Any]


def _slot_name(reflection_id: str) -> str:
    """Derive a DraftStore-valid slot name from a reflection id."""
    stem = _UNSAFE_SLOT_CHAR.sub("-", reflection_id)[:12]
    return f"{_SLOT_STEM}{stem}"


def _read_findings(workspace_root: Path, epoch_id: str, reflection_id: str) -> list[dict[str, Any]]:
    """Read the reflection's ``findings.json`` into a list of finding dicts."""
    from zicato.core.workspace import reflection_findings_path  # noqa: PLC0415

    path = reflection_findings_path(workspace_root, epoch_id, reflection_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("findings")
    if not isinstance(raw, list):
        return []
    return [f for f in raw if isinstance(f, dict)]


def find_finding(
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
    finding_id: str,
) -> dict[str, Any]:
    """Return one finding dict by id, or raise :class:`FindingNotFoundError`."""
    for finding in _read_findings(workspace_root, epoch_id, reflection_id):
        if str(finding.get("finding_id", "")) == finding_id:
            return finding
    raise FindingNotFoundError(
        f"no finding {finding_id!r} in reflection {reflection_id!r} (epoch {epoch_id!r})"
    )


def apply_finding_to_draft(
    *,
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
    finding_id: str,
) -> AppliedFinding:
    """Fork a builder draft from the live contract and stage a finding's op.

    Loads the finding, requires a ``proposed_op`` (else
    :class:`FindingNotActionableError`), forks a NEW ``DraftStore`` slot named
    ``reflect-{reflection_id[:12]}`` off the live contract, and applies the op
    to that draft via the builder op layer. Returns the :class:`AppliedFinding`
    (slot name + the applied patch + the draft-vs-live diff). The sealed
    contract is NEVER written here — the operator seals through the builder.

    The op is dispatched by name against :mod:`zicato.builder.operations` with a
    keyword splat; because every finding's ``proposed_op`` was validated against
    the op's real signature at emit time (``findings.validate_proposed_op``),
    the splat cannot pass an argument the op would reject.
    """
    from zicato.builder import operations as ops  # noqa: PLC0415
    from zicato.builder.draft import DraftStore  # noqa: PLC0415

    finding = find_finding(workspace_root, epoch_id, reflection_id, finding_id)
    proposed = finding.get("proposed_op")
    if not isinstance(proposed, dict) or not proposed.get("op"):
        raise FindingNotActionableError(
            f"finding {finding_id!r} carries no proposed_op — it is a "
            "recommendation only (the fix is an authoring decision); "
            "there is nothing to stage on a draft"
        )
    op_name = str(proposed["op"])
    raw_args = proposed.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}

    fn = getattr(ops, op_name, None)
    if not callable(fn):
        raise FindingNotActionableError(
            f"finding {finding_id!r} names an unknown builder op {op_name!r}"
        )

    slot_name = _slot_name(reflection_id)
    store = DraftStore()
    # A single ephemeral session forks the slot off the LIVE contract
    # (DraftStore.fork copies the session's working draft, which is lazily
    # initialised from ``TournamentDraft.from_workspace``).
    draft = store.fork(session_id="reflect-apply", name=slot_name, workspace_root=workspace_root)
    # A builder op raises ValueError for a rejected edit (a duplicate entry id, an
    # unknown target). Surface it as a not-actionable finding so the CLI renders
    # the op's message cleanly instead of a raw traceback.
    try:
        patch = fn(draft, **args)
    except ValueError as exc:
        raise FindingNotActionableError(
            f"finding {finding_id!r} op {op_name!r} could not be staged: {exc}"
        ) from exc
    diff = draft.diff_vs_live(workspace_root)

    return AppliedFinding(
        reflection_id=reflection_id,
        finding_id=finding_id,
        slot_name=slot_name,
        op=op_name,
        args=dict(args),
        patch=patch.to_dict(),
        diff=diff.to_dict(),
    )


@dataclass(frozen=True, slots=True)
class AppliedSuggestion:
    """The result of staging one eval suggestion's op onto a forked draft."""

    reflection_id: str
    suggestion_id: str
    suggestion_type: str
    slot_name: str
    op: str
    args: dict[str, Any]
    #: The builder ``DraftPatch.to_dict()`` describing what the op changed.
    patch: dict[str, Any]
    #: The forked draft's diff vs the live contract (``ContractDiff.to_dict()``).
    diff: dict[str, Any]


def _reconstruct_op_args(op_name: str, args: dict[str, Any]) -> tuple[Any, ...]:
    """Reconstruct the TYPED positional args a board op needs from JSON args.

    The suggestion apply seam carries the same ``{op, args}`` JSON the copilot /
    REST dispatch does, but the board ops take typed objects (``BoardEntry`` /
    ``JudgeSpec``) rather than dicts. This mirrors the builder REST dispatch's
    reconstruction so an entry / judge suggestion lands byte-identically to a
    hand-authored board edit. Unknown ops raise (recorded as not-actionable).
    """
    from zicato.core.types import JudgeMode, JudgeSpec, validate_board_entry  # noqa: PLC0415

    if op_name == "add_board_entry":
        return (validate_board_entry(args["entry"]),)
    if op_name == "add_judge":
        from goldfive import DriftSeverity  # noqa: PLC0415

        raw = args["judge"]
        if not isinstance(raw, dict):
            raise ValueError("judge must be a JSON object")
        judge = JudgeSpec(
            name=str(raw["name"]),
            mode=JudgeMode(str(raw.get("mode", "inline"))),
            body=str(raw["body"]),
            severity=DriftSeverity(str(raw.get("severity", "warning"))),
        )
        return (str(args["entry_id"]), judge)
    raise FindingNotActionableError(
        f"suggestion op {op_name!r} has no typed apply seam yet — "
        "record the gap (rubric revision has no builder judge-edit op)"
    )


def find_suggestion(
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
    suggestion_id: str,
) -> dict[str, Any]:
    """Return one suggestion dict by id, or raise :class:`SuggestionNotFoundError`."""
    from zicato.reflection.suggestions import read_suggestions_json  # noqa: PLC0415

    for s in read_suggestions_json(workspace_root, epoch_id, reflection_id):
        if str(s.get("suggestion_id", "")) == suggestion_id:
            return s
    raise SuggestionNotFoundError(
        f"no suggestion {suggestion_id!r} in reflection {reflection_id!r} (epoch {epoch_id!r})"
    )


def apply_suggestion_to_draft(
    *,
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
    suggestion_id: str,
) -> AppliedSuggestion:
    """Fork a builder draft from the live contract and stage a suggestion's op.

    The suggestion analogue of :func:`apply_finding_to_draft`: an entry
    suggestion (regression / coverage / harder variant) stages through the new
    ``add_board_entry`` op; a judge suggestion through the existing
    ``add_judge`` op. Both reconstruct the TYPED artifact from the persisted
    JSON before dispatch, so the drafted entry / judge lands exactly as a
    hand-authored board edit would. A rubric revision carries no ``proposed_op``
    (no builder judge-edit op exists — the recorded gap) and raises
    :class:`FindingNotActionableError`. NEVER writes the sealed contract.
    """
    from zicato.builder import operations as ops  # noqa: PLC0415
    from zicato.builder.draft import DraftStore  # noqa: PLC0415

    suggestion = find_suggestion(workspace_root, epoch_id, reflection_id, suggestion_id)
    proposed = suggestion.get("proposed_op")
    if not isinstance(proposed, dict) or not proposed.get("op"):
        raise FindingNotActionableError(
            f"suggestion {suggestion_id!r} carries no proposed_op — it is a "
            "recommendation only (a rubric revision has no builder judge-edit op "
            "yet; the fix is an authoring decision). Nothing to stage."
        )
    op_name = str(proposed["op"])
    raw_args = proposed.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}

    fn = getattr(ops, op_name, None)
    if not callable(fn):
        raise FindingNotActionableError(
            f"suggestion {suggestion_id!r} names an unknown builder op {op_name!r}"
        )
    try:
        positional = _reconstruct_op_args(op_name, args)
    except (KeyError, ValueError) as exc:
        raise FindingNotActionableError(
            f"suggestion {suggestion_id!r} op {op_name!r} has a malformed artifact: {exc}"
        ) from exc

    slot_name = _slot_name(reflection_id)
    store = DraftStore()
    draft = store.fork(session_id="reflect-apply", name=slot_name, workspace_root=workspace_root)
    # The board op raises ValueError for a rejected edit (most commonly a
    # duplicate entry id when the same suggestion is staged twice). Surface it as
    # not-actionable so the CLI renders the message cleanly, never a traceback.
    try:
        patch = fn(draft, *positional)
    except ValueError as exc:
        raise FindingNotActionableError(
            f"suggestion {suggestion_id!r} op {op_name!r} could not be staged: {exc}"
        ) from exc
    diff = draft.diff_vs_live(workspace_root)

    return AppliedSuggestion(
        reflection_id=reflection_id,
        suggestion_id=suggestion_id,
        suggestion_type=str(suggestion.get("suggestion_type", "")),
        slot_name=slot_name,
        op=op_name,
        args=dict(args),
        patch=patch.to_dict(),
        diff=diff.to_dict(),
    )


__all__ = [
    "AppliedFinding",
    "AppliedSuggestion",
    "FindingNotActionableError",
    "FindingNotFoundError",
    "SuggestionNotFoundError",
    "apply_finding_to_draft",
    "apply_suggestion_to_draft",
    "find_finding",
    "find_suggestion",
]
