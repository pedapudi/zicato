"""Project recorded tournament decisions and preserve undecided states."""

from __future__ import annotations

from typing import Any

from zicato.core.tournament import TournamentDecision, recorded_decision_token


def experiment_decision(exp: dict[str, Any]) -> str | None:
    """Read the recorded outcome decision, or None before an outcome exists."""
    return recorded_decision_token(exp.get("outcome"))


def canonical_decision(raw: str | None) -> str | None:
    """Validate a recorded decision against the tournament's supported tokens."""
    return TournamentDecision(raw).value if raw is not None else None


def promoted_tristate(raw: str | None) -> bool | None:
    """Preserve absent decisions and identify a recorded promotion."""
    decision = canonical_decision(raw)
    return decision == TournamentDecision.PROMOTED if decision is not None else None


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
