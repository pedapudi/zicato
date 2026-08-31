"""decisions — THE one experiment-decision classifier the dashboard serves.

Every payload that names a tournament decision funnels through here so the
server ships ONE canonical vocabulary and the frontend never re-classifies:

* :data:`PROMOTED_DECISIONS` / :func:`experiment_decision` — the token set
  and the reader that the lineage view classifies promotions with.
* :func:`canonical_decision` — maps any recorded token onto the canonical
  ``promoted`` / ``rejected`` / ``deferred`` wire vocabulary
  (:class:`zicato.core.tournament.TournamentDecision`); an unrecognised
  token passes through lowercased rather than being guessed.
* :func:`promoted_tristate` — the tri-state ``promoted`` stamp: ``None``
  while no decision is recorded (in-flight / never raced), else the same
  boolean the lineage view derives.
"""

from __future__ import annotations

from typing import Any

#: Recorded tokens that count as a promotion. The canonical wire token is
#: ``"promoted"`` (``TournamentDecision.PROMOTED``); the rest are spellings
#: that older workspaces recorded and that still appear on disk.
PROMOTED_DECISIONS = frozenset({"promoted", "promote", "accepted", "accept", "win", "won"})

#: Recorded tokens that count as a rejection.
REJECTED_DECISIONS = frozenset({"rejected", "reject", "lose", "lost"})

#: Recorded tokens that count as a deferral: kept for analysis, never crowned.
DEFERRED_DECISIONS = frozenset({"deferred", "defer"})


def experiment_decision(exp: dict[str, Any]) -> str | None:
    """The raw decision token recorded on one experiment, or ``None``.

    Reads the ``outcome`` field: a bare string outcome IS the decision; a
    dict outcome carries it under ``decision`` / ``tournament_decision`` /
    ``verdict``. ``None`` when no decision was recorded (in-flight).
    """
    outcome = exp.get("outcome")
    if outcome is None:
        return None
    if isinstance(outcome, str):
        return outcome
    if isinstance(outcome, dict):
        for key in ("decision", "tournament_decision", "verdict"):
            val = outcome.get(key)
            if isinstance(val, str):
                return val
    return None


def canonical_decision(raw: str | None) -> str | None:
    """Map a recorded decision token onto the canonical wire vocabulary.

    ``promoted`` / ``rejected`` / ``deferred`` for every known spelling;
    an unknown token passes through lowercased (never guessed into a
    verdict); ``None`` / empty stays ``None`` (no decision recorded).
    """
    if raw is None:
        return None
    tok = raw.strip().lower()
    if not tok:
        return None
    if tok in PROMOTED_DECISIONS:
        return "promoted"
    if tok in REJECTED_DECISIONS:
        return "rejected"
    if tok in DEFERRED_DECISIONS:
        return "deferred"
    return tok


def promoted_tristate(raw: str | None) -> bool | None:
    """The tri-state ``promoted`` stamp for a recorded decision token.

    ``None`` when no decision is recorded (in-flight / never raced), and never
    a default ``False``, which would report an undecided promotion as a
    rejection. Otherwise the same boolean the lineage view derives
    (``token in PROMOTED_DECISIONS``).
    """
    if raw is None:
        return None
    tok = raw.strip().lower()
    if not tok:
        return None
    return tok in PROMOTED_DECISIONS


def decision_surface(parent: Any, promoted: Any) -> tuple[str, str]:
    """Canonical decision token and renderer label for one lineage node."""
    decision = (
        "baseline"
        if parent is None
        else "promoted"
        if promoted is True
        else "rejected"
        if promoted is False
        else "pending"
    )
    return decision, {"baseline": "seed (v0)", "pending": "undecided"}.get(decision, decision)


def stamp_experiment_decision(record: dict[str, Any]) -> None:
    """Stamp ``decision`` (canonical token) + ``promoted`` (tri-state) in place."""
    raw = experiment_decision(record)
    record["decision"] = canonical_decision(raw)
    record["promoted"] = promoted_tristate(raw)
