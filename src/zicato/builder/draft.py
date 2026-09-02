"""The mutable draft-contract state the builder edits.

A :class:`TournamentDraft` is the editable working copy of a whole
evaluation **contract** — the scoring weights (structure + params +
overfitting/holdout + gate + per-kind/per-judge weights), the board
(entries with their judges / predicates / rubrics and ``holdout`` tags),
the proposer brief text, and the proposer dir. Both the form (B2) and the
copilot (B1b) drive the *same* draft through the operations in
:mod:`zicato.builder.operations`; the draft is the single editable
surface, and nothing it does touches the live workspace until
:func:`zicato.builder.operations.apply` is called with ``confirm=True``.

Unlike the frozen contract dataclasses in :mod:`zicato.core.types`, a
:class:`TournamentDraft` is MUTABLE — operations mutate it in
place and return a structured patch describing what changed. A
:class:`DraftStore` keys independent drafts by ``session_id`` so two
concurrent builder sessions never tread on each other.

The draft can be initialised blank or, via
:meth:`TournamentDraft.from_workspace`, pre-filled from the CURRENT live
contract so the builder opens showing exactly what is running.
"""

from __future__ import annotations

import re
from collections import deque
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

    A component that differs will roll the epoch on
    :func:`zicato.builder.operations.apply`. The diff is what the UI
    renders to warn the operator before they confirm.

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
    builder edits it as a per-entry boolean. Idempotent — adding a tag an
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

    # -- construction -----------------------------------------------------

    @classmethod
    def from_workspace(cls, workspace_root: Path) -> TournamentDraft:
        """Initialise a draft from the CURRENT live contract.

        Reads the workspace's current epoch — its scoring weights, board
        (with judges / predicates / rubrics and any ``holdout`` tags), the
        proposer-brief text, and the configured proposer dir — so the
        builder opens pre-filled with what is running. A missing component
        degrades to its default (empty board, empty brief, built-in
        proposer) rather than raising, so a freshly-``init``-ed workspace
        with no epoch yet still yields an editable draft. A missing scoring
        contract degrades to the RECOMMENDED scaffold contract
        (:func:`zicato.core.scoring_config.recommended_scaffold_weights` —
        racing field 4, replicates 2, evidence gate on), the same full
        effective contract ``zicato init`` writes, so a blank draft opens on
        the noise-aware recommendation rather than the bare gauntlet.
        """
        from zicato.core.scoring_config import recommended_scaffold_weights  # noqa: PLC0415
        from zicato.workspace_loader import (  # noqa: PLC0415
            load_current_board_with_meta,
            load_current_brief,
            load_current_epoch_config,
            load_current_scoring,
        )

        try:
            scoring = load_current_scoring(workspace_root)
        except FileNotFoundError:
            scoring = recommended_scaffold_weights()

        # The WITH-META loader: the board-level ``board_meta`` header
        # (disable_drift / judge_only) is part of the contract, and
        # ``apply`` writes the board back — loading entries alone would
        # silently strip the header from the live contract on apply.
        try:
            loaded, disable_drift, judge_only = load_current_board_with_meta(workspace_root)
            entries = list(loaded)
        except FileNotFoundError:
            entries = []
            disable_drift = ()
            judge_only = False

        try:
            brief = load_current_brief(workspace_root).text
        except FileNotFoundError:
            brief = ""

        proposer_path: Path | None = None
        try:
            cfg = load_current_epoch_config(workspace_root)
            proposer_path = cfg.proposer_path
        except FileNotFoundError:
            proposer_path = None

        return cls(
            scoring=scoring,
            entries=entries,
            brief=brief,
            proposer_path=proposer_path,
            disable_drift=tuple(disable_drift),
            judge_only=judge_only,
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
        the epoch on :func:`zicato.builder.operations.apply`.
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
        "has_custom_agent": spec.agent_source_sha256 is not None,
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


#: Slot names are path/JSON-safe short slugs — same spirit as epoch ids.
_SLOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Bounded per-session undo depth. Deep enough for a whole authoring
#: session's worth of missteps, small enough that history can never grow
#: without bound in a long-lived dashboard process.
_HISTORY_LIMIT = 20


def _copy_draft(draft: TournamentDraft) -> TournamentDraft:
    """A safe working copy of ``draft`` for a named slot.

    ``scoring`` is a frozen dataclass and every operation REPLACES it (and
    replaces board entries wholesale — entries themselves are never
    mutated in place), so a shallow copy of the entries list is a real
    fork: edits to either copy can never leak into the other.
    ``disable_drift`` / ``judge_only`` are immutable values, carried
    verbatim — a fork that dropped them would strip the board_meta header
    from the variant.
    """
    return TournamentDraft(
        scoring=draft.scoring,
        entries=list(draft.entries),
        brief=draft.brief,
        proposer_path=draft.proposer_path,
        disable_drift=draft.disable_drift,
        judge_only=draft.judge_only,
    )


class DraftStore:
    """In-memory store of editable drafts keyed by session id — plus SLOTS.

    Concurrent builder sessions (multiple browser tabs, the form and the
    copilot side by side) each get an independent :class:`TournamentDraft`
    so their edits never collide. A session new to the store is lazily
    initialised from the CURRENT live contract via
    :meth:`TournamentDraft.from_workspace`, so the builder always opens
    pre-filled with what is running.

    NAMED SLOTS are the fork/compare lifecycle. :meth:`fork` snapshots a
    session's working draft into a named slot and binds the session TO
    that slot, so subsequent edits accumulate on it; :meth:`switch`
    rebinds the session to another slot with its state intact. Named
    drafts are how an operator iterates on contract variants WITHOUT
    rolling the epoch: the write path is untouched, and ``apply`` still
    writes whichever draft the session is on.

    Slots persist exactly the way session drafts do — in this
    process-local store (drafts have never outlived the dashboard
    process; slots inherit that contract rather than inventing a second
    persistence story). Everything lives until
    :func:`zicato.builder.operations.apply` writes one to the workspace,
    or the process exits.

    UNDO HISTORY: :meth:`remember` records a bounded (20) per-session
    deque of pre-op draft snapshots — the seam both front doors call
    before a write op — and :meth:`pop_undo` hands the ``undo`` op the
    newest snapshot that differs from the current state. History is
    process-local like everything else here.
    """

    def __init__(self) -> None:
        self._drafts: dict[str, TournamentDraft] = {}
        #: Named slots, store-global (shared across sessions — two tabs
        #: naming the same slot see the same draft, exactly like two tabs
        #: sharing a session id).
        self._slots: dict[str, TournamentDraft] = {}
        #: Per-session bounded undo history: value snapshots of the
        #: session's draft, recorded by :meth:`remember` at both front
        #: doors (the REST dispatch and the copilot's tool context)
        #: BEFORE a write op mutates the draft. Newest last.
        self._history: dict[str, deque[TournamentDraft]] = {}

    def get(self, session_id: str, workspace_root: Path) -> TournamentDraft:
        """Return the draft for ``session_id``, initialising it if new.

        A session not yet in the store is initialised from the live
        contract so it opens pre-filled. Subsequent calls return the same
        mutable instance, so operations accumulate across requests.
        """
        draft = self._drafts.get(session_id)
        if draft is None:
            draft = TournamentDraft.from_workspace(workspace_root)
            self._drafts[session_id] = draft
        return draft

    def reset(self, session_id: str, workspace_root: Path) -> TournamentDraft:
        """Discard ``session_id``'s draft and re-init it from live."""
        draft = TournamentDraft.from_workspace(workspace_root)
        self._drafts[session_id] = draft
        return draft

    def has(self, session_id: str) -> bool:
        """``True`` iff ``session_id`` already has a draft in the store."""
        return session_id in self._drafts

    # -- undo history (remember / pop_undo) ---------------------------------

    def remember(self, session_id: str) -> None:
        """Snapshot the session's CURRENT draft state onto its undo history.

        The recording seam for step-undo: both front doors call this with
        the PRE-op state — ``builder_op`` right before a write-op
        dispatch, and :meth:`BuilderToolContext.draft` on every copilot
        tool's draft fetch. Dedups against the newest snapshot by field
        equality, so a read tool (or a no-op edit) records nothing.
        Bounded to 20 snapshots per session — the oldest falls off. A
        session with no draft yet is a no-op (there is no state to
        remember).
        """
        draft = self._drafts.get(session_id)
        if draft is None:
            return
        history = self._history.setdefault(session_id, deque(maxlen=_HISTORY_LIMIT))
        if history and history[-1] == draft:
            return
        history.append(_copy_draft(draft))

    def pop_undo(self, session_id: str) -> TournamentDraft | None:
        """Pop the newest history snapshot that DIFFERS from the current draft.

        Snapshots equal (by field equality) to the session's current
        state are discarded on the way down — they would make undo a
        visible no-op. Returns ``None`` when the history is exhausted;
        the caller renders that as a "nothing to undo" patch note. The
        returned snapshot is a value copy — the caller restores it INTO
        the session's live draft object (in place) so slot bindings stay
        coherent.
        """
        history = self._history.get(session_id)
        current = self._drafts.get(session_id)
        while history:
            snapshot = history.pop()
            if current is None or snapshot != current:
                return snapshot
        return None

    # -- named slots (fork / list / switch) --------------------------------

    def fork(self, session_id: str, name: str, workspace_root: Path) -> TournamentDraft:
        """Snapshot the session's working draft into slot ``name`` and switch to it.

        The fork is a COPY of the current working draft — the state the
        operator has built up so far becomes the new slot's starting
        point — and the session is bound to the slot, so subsequent edits
        accumulate on it. Raises :class:`ValueError` on a malformed name
        or a name already taken (fork never silently overwrites a
        variant).
        """
        if not _SLOT_NAME_RE.match(name or ""):
            raise ValueError(
                f"invalid draft name {name!r}: use 1-64 chars of [A-Za-z0-9._-], "
                "starting alphanumeric"
            )
        if name in self._slots:
            raise ValueError(f"a draft named {name!r} already exists; switch to it instead")
        forked = _copy_draft(self.get(session_id, workspace_root))
        self._slots[name] = forked
        self._drafts[session_id] = forked
        return forked

    def list_drafts(self) -> list[str]:
        """The named slots, sorted."""
        return sorted(self._slots)

    def switch(self, session_id: str, name: str) -> TournamentDraft:
        """Bind ``session_id`` to slot ``name`` (its state intact).

        The session's previous working draft is left exactly where it was
        (if it was a slot, that slot keeps every edit; a never-forked
        working draft is simply left behind). Raises :class:`ValueError`
        on an unknown name.
        """
        slot = self._slots.get(name)
        if slot is None:
            known = ", ".join(sorted(self._slots)) or "none"
            raise ValueError(f"no draft named {name!r} (known: {known})")
        self._drafts[session_id] = slot
        return slot

    def slot(self, name: str) -> TournamentDraft | None:
        """The slot draft for ``name``, or ``None`` when absent."""
        return self._slots.get(name)


__all__ = [
    "ContractComponentDiff",
    "ContractDiff",
    "TournamentDraft",
    "DraftStore",
]
