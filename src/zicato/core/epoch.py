"""Epoch / generation types: the frozen contract and a lineage node.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zicato.core.scoring_config import ScoringWeights

# ---------------------------------------------------------------------------
# Epoch / generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EpochConfig:
    """The saved evaluation settings and runtime observations for an epoch.

    The contract hash identifies the board, brief, effective scoring settings,
    evaluator implementation, adapter, mutable source paths, and proposer.
    It is required; changed live inputs create a different epoch.

    Board and brief paths name the frozen files. Optional proposer_path selects
    a saved proposer directory; None selects the built-in proposer. The goal
    describes the operator's objective. closed and closed_at record completion.

    noise_floor and preflight are optional measurements made after creation.
    They do not contribute to contract identity. implementation_identity records
    the evaluator revisions that governed execution.
    """

    id: str
    name: str
    created_at: str
    board_path: Path
    brief_path: Path
    scoring: ScoringWeights
    contract_hash: str
    closed: bool = False
    closed_at: str = ""
    implementation_identity: dict[str, str | int] = field(default_factory=dict)
    goal: str = ""
    proposer_path: Path | None = None
    noise_floor: dict[str, Any] | None = None
    preflight: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.contract_hash, str)
            or len(self.contract_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.contract_hash)
        ):
            raise ValueError("contract_hash must be a SHA-256 hexadecimal digest")


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
        the system under test's source root for the duration of the run.
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
        round}``. Defaults to ``0``, so a caller with no round to report —
        the seed among them — need not specify it.
    """

    id: str
    epoch_id: str
    parent_id: str | None
    snapshot_root: Path
    created_at: str
    promoted: bool = False
    round_index: int = 0
