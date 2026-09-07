"""Editable evaluation inputs with their source revision and comparison to live files."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.board.jsonl import board_meta_to_dict, entry_to_dict
from zicato.board.split import HOLDOUT_TAG, split_board
from zicato.core.types import (
    BoardEntry,
    ProposerSpec,
    ScoringWeights,
)

if TYPE_CHECKING:
    from zicato.contract_draft.publication import ContractSource
    from zicato.core.drift_kinds import DriftKind


@dataclass(frozen=True, slots=True)
class ContractComponentDiff:
    """Whether one contract component differs between draft and live.

    Fields
    ------
    component:
        One of ``"board"`` / ``"brief"`` / ``"scoring"`` / ``"proposer"``
        / ``"structure"`` / ``"overfitting"``. ``structure`` and
        ``overfitting`` are sub-views of scoring surfaced separately so
        the UI can show *which* part of the scoring contract moved.
    changed:
        ``True`` iff the draft's value for this component differs from the
        live workspace's value.
    """

    component: str
    changed: bool


@dataclass(frozen=True, slots=True)
class ContractDiff:
    """Which contract components differ between the draft and live.

    Applying a changed component updates live inputs; the next execution
    opens an epoch when the contract differs. The UI renders this diff before
    the operator confirms the edit.

    Fields
    ------
    components:
        Per-component diff flags (see :class:`ContractComponentDiff`).
    rolls_epoch:
        ``True`` iff any *contract* component differs — i.e. applying the
        draft would roll the epoch. ``structure`` and ``overfitting`` are
        sub-views of ``scoring`` and do not independently flip this beyond
        what ``scoring`` already does.
    """

    components: tuple[ContractComponentDiff, ...]
    rolls_epoch: bool

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable snapshot for the UI."""
        return {
            "components": [
                {"component": c.component, "changed": c.changed} for c in self.components
            ],
            "rolls_epoch": self.rolls_epoch,
            "changed_components": [c.component for c in self.components if c.changed],
        }


def _entry_with_holdout(entry: BoardEntry, *, holdout: bool) -> BoardEntry:
    """Return ``entry`` with the ``holdout`` tag added or removed.

    The ``holdout`` tag is how an operator declares an explicit
    train/holdout split by hand (see :mod:`zicato.board.split`); the
    edit supplies a boolean per entry. Adding a tag an
    entry already carries, or removing one it lacks, returns an
    equivalent entry.
    """
    import dataclasses

    has = HOLDOUT_TAG in entry.tags
    if holdout and not has:
        return dataclasses.replace(entry, tags=(*entry.tags, HOLDOUT_TAG))
    if not holdout and has:
        return dataclasses.replace(entry, tags=tuple(t for t in entry.tags if t != HOLDOUT_TAG))
    return entry


@dataclass(slots=True)
class TournamentDraft:
    """A mutable, in-memory editable copy of one evaluation contract.

    Fields
    ------
    scoring:
        The working :class:`ScoringWeights` — structure + params,
        overfitting/holdout config, the promote gate, and the per-kind /
        per-judge weights. Mutated wholesale by the operations (every
        ``set_*`` op replaces this with a new frozen instance).
    entries:
        The working board, in order. Mutable list; operations edit
        entries / judges in place. Each entry's ``holdout`` tag carries
        the explicit train/holdout split.
    brief:
        The proposer-brief text (markdown), verbatim.
    proposer_path:
        Location of the proposer dir, or ``None`` for the built-in
        default proposer.
    disable_drift:
        The board-level ``board_meta`` header's drift-suppression set
        (:class:`goldfive.DriftKind` members). Part of the contract:
        the header line is written back by ``apply`` and folds into the
        contract hash, so dropping it here would silently strip it from
        the live board.
    judge_only:
        The board-level ``board_meta`` header's judge-only flag
        (goldfive judges without steering). Same round-trip contract as
        :attr:`disable_drift`.
    """

    scoring: ScoringWeights = field(default_factory=ScoringWeights)
    entries: list[BoardEntry] = field(default_factory=list)
    brief: str = ""
    proposer_path: Path | None = None
    disable_drift: tuple[DriftKind, ...] = ()
    judge_only: bool = False
    source: ContractSource | None = field(default=None, repr=False, compare=False)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_workspace(cls, workspace_root: Path) -> TournamentDraft:
        """Load the editable contract and capture its bytes for conflict detection.

        Frozen epoch files describe past evaluations. Drafts use registered live
        paths, including files edited before the first epoch exists. Missing
        components use the shared scoring defaults.
        """
        import json

        from zicato.board.jsonl import parse_board_with_meta
        from zicato.contract_draft.publication import capture_contract_source
        from zicato.workspace_loader import scoring_weights_from_dict

        source = capture_contract_source(workspace_root)
        scoring_text = source.file("scoring").text
        scoring = (
            ScoringWeights()
            if scoring_text is None
            else scoring_weights_from_dict(json.loads(scoring_text))
        )
        board_text = source.file("board").text
        entries, disable_drift, judge_only = parse_board_with_meta(
            board_text or "", source=source.file("board").path
        )
        return cls(
            scoring=scoring,
            entries=entries,
            brief=source.file("brief").text or "",
            proposer_path=source.inputs.proposer_path,
            disable_drift=disable_drift,
            judge_only=judge_only,
            source=source,
        )

    # -- read-side --------------------------------------------------------

    def entry_by_id(self, entry_id: str) -> BoardEntry | None:
        """Return the entry with ``entry_id``, or ``None`` if absent."""
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    def resolved_proposer(self) -> ProposerSpec:
        """Resolve the draft's proposer dir into a :class:`ProposerSpec`."""
        from zicato.proposer.skills import resolve_proposer_spec  # noqa: PLC0415

        return resolve_proposer_spec(self.proposer_path)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable snapshot of the whole draft for the UI.

        The scoring block is rendered through the shared lifecycle
        serializer so it is byte-compatible with the on-disk
        ``scoring.json`` shape; the board through the shared JSONL
        per-entry serializer. ``holdout`` is surfaced as a derived
        train/holdout id partition so the UI can highlight the split
        without re-deriving it.
        """
        from zicato.epoch.lifecycle import scoring_to_dict  # noqa: PLC0415

        train_ids, holdout_ids = split_board(self.entries, self.scoring.overfitting)
        return {
            "scoring": scoring_to_dict(self.scoring),
            "board": [entry_to_dict(e) for e in self.entries],
            "board_meta": {
                "disable_drift": [_drift_token(k) for k in self.disable_drift],
                "judge_only": self.judge_only,
            },
            "brief": self.brief,
            "proposer_path": str(self.proposer_path) if self.proposer_path is not None else None,
            "proposer": _proposer_to_dict(self.resolved_proposer()),
            "holdout": {
                "train_ids": list(train_ids),
                "holdout_ids": list(holdout_ids),
            },
        }

    # -- diff vs live -----------------------------------------------------

    def diff_vs_live(self, workspace_root: Path) -> ContractDiff:
        """Return which contract components differ from the live workspace.

        Builds the live draft via :meth:`from_workspace` and compares each
        contract component to this draft's. A differing component will roll
        the epoch on :func:`zicato.contract_draft.operations.apply`.
        """
        live = TournamentDraft.from_workspace(workspace_root)

        board_changed = _board_canon(
            self.entries, self.disable_drift, self.judge_only
        ) != _board_canon(live.entries, live.disable_drift, live.judge_only)
        brief_changed = _brief_canon(self.brief) != _brief_canon(live.brief)
        scoring_changed = _scoring_canon(self.scoring) != _scoring_canon(live.scoring)
        proposer_changed = self.resolved_proposer() != live.resolved_proposer()
        structure_changed = self.scoring.tournament_structure != live.scoring.tournament_structure
        overfitting_changed = self.scoring.overfitting != live.scoring.overfitting

        components = (
            ContractComponentDiff("board", board_changed),
            ContractComponentDiff("brief", brief_changed),
            ContractComponentDiff("scoring", scoring_changed),
            ContractComponentDiff("proposer", proposer_changed),
            ContractComponentDiff("structure", structure_changed),
            ContractComponentDiff("overfitting", overfitting_changed),
        )
        rolls_epoch = board_changed or brief_changed or scoring_changed or proposer_changed
        return ContractDiff(components=components, rolls_epoch=rolls_epoch)

    def set_holdout_tags(self, holdout_ids: Sequence[str]) -> None:
        """Set the explicit ``holdout`` tag exactly on ``holdout_ids``.

        Every entry whose id is in ``holdout_ids`` gains the tag; every
        other entry loses it. Mutates :attr:`entries` in place.
        """
        wanted = set(holdout_ids)
        self.entries = [_entry_with_holdout(e, holdout=e.id in wanted) for e in self.entries]


def _proposer_to_dict(spec: ProposerSpec) -> dict[str, Any]:
    """JSON-serializable view of a resolved proposer for the UI."""
    return {
        "agent_id": spec.agent_id,
        "tools": list(spec.tools),
        "skills": [{"name": s.name, "description": s.description} for s in spec.skills],
    }


def _drift_token(kind: Any) -> str:
    """The lowercase wire token of a board-level drift kind."""
    return str(getattr(kind, "value", kind))


def _board_canon(
    entries: Sequence[BoardEntry],
    disable_drift: tuple[DriftKind, ...] = (),
    judge_only: bool = False,
) -> str:
    """Canonical, order-independent string form of a board for diffing.

    Reuses the contract canonicalizer's per-entry serialization so the
    diff agrees with the epoch-roll rule: reordering entries does not
    register a change; editing an entry's content does.

    The board-level ``board_meta`` header is prepended ONLY when it is
    non-default (``disable_drift`` non-empty or ``judge_only`` true),
    mirroring :func:`zicato.board.jsonl.save_board`'s
    emit-only-when-non-default rule — so this canon agrees with the
    on-disk bytes the contract hash sees, and a default-meta draft canons
    byte-identically to a board saved before the header existed.
    """
    import json

    lines: list[str] = []
    if disable_drift or judge_only:
        lines.append(
            json.dumps(
                board_meta_to_dict(disable_drift, judge_only),
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    lines.extend(
        json.dumps(entry_to_dict(e), sort_keys=True, ensure_ascii=False)
        for e in sorted(entries, key=lambda e: e.id)
    )
    return "\n".join(lines)


def _brief_canon(text: str) -> str:
    """Whitespace-normalized brief, matching the contract canonicalizer."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _scoring_canon(weights: ScoringWeights) -> str:
    """Fully defaulted, key-sorted scoring form matching the contract hash."""
    import json

    from zicato.epoch.contract import scoring_contract_to_canon  # noqa: PLC0415

    return json.dumps(scoring_contract_to_canon(weights), sort_keys=True)


__all__ = [
    "ContractComponentDiff",
    "ContractDiff",
    "TournamentDraft",
]
