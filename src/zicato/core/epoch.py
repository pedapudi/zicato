"""Epoch / generation types: the frozen contract and a lineage node.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zicato.core.scoring_config import ScoringWeights

# ---------------------------------------------------------------------------
# Epoch / generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpochConfig:
    """The frozen evaluation contract for an epoch.

    Pinned for the lifetime of the epoch: board, proposer brief, scoring
    weights. Changing any of these starts a new epoch — see
    :doc:`project_zicato_journaling_and_epochs`.

    Fields
    ------
    id:
        Stable epoch identifier (operator-chosen; filesystem-safe).
    name:
        Human-readable name surfaced in CLI listings.
    created_at:
        ISO-8601 UTC timestamp of epoch creation.
    board_path:
        Absolute path to the frozen ``board.jsonl`` for this epoch.
    brief_path:
        Absolute path to the frozen ``brief.md`` proposer brief the
        proposer reads each round. (Earlier revisions named this field
        ``rubric_path`` and the file ``rubric.md``; both were renamed.)
    scoring:
        The frozen :class:`ScoringWeights` for this epoch.
    closed:
        ``True`` once the epoch has been closed by ``zicato epoch close``
        (or auto-closed by a subsequent ``zicato epoch new``). Closed
        epochs are read-only.
    closed_at:
        ISO-8601 UTC timestamp of closure, or empty string when still
        open.
    contract_hash:
        ``sha256`` hex digest of the canonicalized evaluation contract
        (board + rubric + scoring + the registered inner-harness
        identity) at the time this epoch was created. See
        :mod:`zicato.epoch.contract`. The orchestrator recomputes this
        on every ``evolve`` and auto-rolls the epoch when the live
        contract drifts from the stored value.
        The default is the empty string. An empty ``contract_hash``
        means "epoch created before contract-hash auto-epoching landed"
        — such legacy epochs are treated as *always matching* so the
        orchestrator never spuriously rolls a workspace that predates
        the feature.
    goal:
        Free-form operator-supplied statement of *why* this epoch
        exists — the intent the operator is testing (e.g. "shift the
        proposer brief toward concrete deltas" or "new scoring weights
        for cost drift"). Machine-readable companion to the narrative
        in ``journal.md``; surfaced in the analyzer report header so
        the reason for an epoch is visible without re-reading the
        journal. Defaults to the empty string (which renders as "no
        goal recorded" downstream) so epochs already on disk that
        predate this field load cleanly. May be multi-line.
    proposer_path:
        Filesystem location of the ``proposers/<name>/`` directory frozen
        for this epoch, or ``None`` for the built-in default proposer.
        Folds into the contract hash via :mod:`zicato.epoch.contract`, so
        configuring a proposer dir (or editing one of its skills) rolls
        the epoch. Defaults to ``None``; an epoch ``config.json`` written
        before this field landed loads as the built-in default.
    """

    id: str
    name: str
    created_at: str
    board_path: Path
    brief_path: Path
    scoring: ScoringWeights
    closed: bool = False
    closed_at: str = ""
    contract_hash: str = ""
    goal: str = ""
    # Location of the proposer dir frozen for this epoch, or ``None`` for
    # the built-in default proposer. Folded into the contract hash; missing
    # in an epoch ``config.json`` written before this field landed ⇒ ``None``.
    proposer_path: Path | None = None


@dataclass(frozen=True, slots=True)
class Generation:
    """One node in an epoch's generation lineage.

    Fields
    ------
    id:
        Stable generation identifier. Convention: ``"v0"``, ``"v1"``,
        ascending under one epoch. The ``"v"`` prefix is preserved in
        filesystem paths.
    epoch_id:
        The epoch this generation belongs to.
    parent_id:
        The generation this one was forked from, or ``None`` for the
        epoch's seed generation (``"v0"``).
    snapshot_root:
        Absolute path to the source-tree snapshot for this generation.
        The patch applier produced this by copying the parent's snapshot
        and applying the experiment's patches; the runner mounts it as
        the inner harness's source root for the duration of the run.
    created_at:
        ISO-8601 UTC creation timestamp.
    promoted:
        ``True`` iff this generation has been promoted to lineage head
        by a tournament. The epoch's current head is the most-recent
        promoted generation; ``promoted=False`` generations are dead
        branches kept for analysis.
    round_index:
        The evolve round that MINTED this generation — its birth round.
        Round indices are zero-based (the first evolve round is ``0``),
        and the epoch's genesis seed (``v0``) is round ``0``. A champion
        carried into later rounds keeps its birth round; it is NOT
        re-stamped each round it defends. Consumers group an epoch's
        generations as ``Epoch -> Round -> {challengers minted that
        round}``. Defaults to ``0`` so legacy callers (and the seed)
        need not specify it.
    """

    id: str
    epoch_id: str
    parent_id: str | None
    snapshot_root: Path
    created_at: str
    promoted: bool = False
    round_index: int = 0
