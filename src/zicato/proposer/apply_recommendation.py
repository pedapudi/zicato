"""Applying one drafted recommendation — the operator's gate, and the only writer.

This is the ONE code path in the repository that writes into the proposer dir on
a recommendation's behalf. It is reachable only from
``zicato proposer apply-recommendation``: :mod:`zicato.proposer.reflection` does
not import it, and the evolve loop does not import it, so there is no sequence
of automated steps that ends with the proposer having rewritten itself. That is
the "never self-applied" invariant, and it is a structural property rather than
a policy — the test suite pins the absent edge.

What applying does, in order:

1. Resolve the recommendation by id and refuse anything without a remedy.
2. Verify the remedy's bytes against the digest recorded when it was drafted,
   so an edited or truncated record cannot be applied under its original id.
3. Write the skill file into the LIVE proposer dir — the operator's editable
   copy rather than a frozen per-epoch one.
4. Stage the id for the next epoch to claim
   (:mod:`zicato.proposer.staging`).

Step 3 is what rolls the epoch. The proposer dir folds into the contract hash
(``_canon_proposer``, PROPOSER.md §4), so the next ``evolve`` sees drift on the
``proposer`` component and opens a fresh epoch before it proposes anything. That
is the whole design: a proposer change is structurally an epoch-boundary event,
because generations proposed by different proposers are not comparable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.epoch._storage import RecordError
from zicato.proposer.reflection_records import read_finding
from zicato.proposer.staging import stage_recommendation


class ApplyError(RuntimeError):
    """A recommendation could not be applied; nothing was written."""


@dataclass(frozen=True, slots=True)
class Applied:
    """The record of one applied recommendation."""

    recommendation_id: str
    epoch_id: str
    reflection_id: str
    path: Path
    sha256: str
    kind: str
    staged: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "epoch_id": self.epoch_id,
            "reflection_id": self.reflection_id,
            "path": str(self.path),
            "sha256": self.sha256,
            "kind": self.kind,
            "staged": list(self.staged),
        }


def apply_recommendation(
    workspace_root: Path,
    recommendation_id: str,
    *,
    proposer_path: Path,
    epoch_id: str | None = None,
) -> Applied:
    """Write one recommendation's remedy into ``proposer_path``; stage its id.

    Raises :class:`ApplyError` — having written nothing — when the id does not
    resolve, when the finding carries no remedy (an INFO finding whose honest
    answer is an operator decision rather than a skill), or when the remedy's bytes no
    longer match the digest drafted with them.

    Applying is idempotent in effect: re-applying the same recommendation
    rewrites identical bytes and does not double-stage the id.
    """
    try:
        located = read_finding(workspace_root, recommendation_id, epoch_id=epoch_id)
    except RecordError as exc:
        raise ApplyError(str(exc)) from exc
    if located is None:
        raise ApplyError(
            f"no proposer recommendation {recommendation_id!r} found under {workspace_root}; "
            "run `zicato proposer recommendations` for the pending queue"
        )
    found_epoch, reflection_id, finding = located
    remedy = finding.remedy
    if remedy is None:
        raise ApplyError(
            f"recommendation {recommendation_id!r} carries no remedy — it is a finding to "
            "read, not an edit to apply (mutation-surface findings are operator decisions)"
        )

    target = proposer_path / remedy.relative_path
    if not target.resolve().is_relative_to(proposer_path.resolve()):
        raise ApplyError("remedy path escapes the proposer dir through a symbolic link")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(remedy.new_text, encoding="utf-8")

    return Applied(
        recommendation_id=recommendation_id,
        epoch_id=found_epoch,
        reflection_id=reflection_id,
        path=target,
        sha256=remedy.sha256,
        kind=remedy.kind,
        staged=stage_recommendation(workspace_root, recommendation_id),
    )


__all__ = ["Applied", "ApplyError", "apply_recommendation"]
