"""Pinned deterministic contract knobs for scripted orchestrator tests.

The shipped defaults are noise-aware: best-of-3 proposer sampling and two
averaged replicates per gauntlet duel (the Bradley--Terry evidence gate
stays opt-in — the scaffolded contracts enable it explicitly). Most
orchestrator/e2e tests, however, drive SCRIPTED single-shot proposers and
stub reducers whose call sequences assume exactly one propose per round
and exactly one paired run per duel — so their contracts pin the
historical deterministic knobs explicitly, exactly the way a
deterministic-harness operator would
(``examples/zicato_examples/target_0_convergence/scoring.json`` is the
canonical pinned contract). The ``promote_confidence_threshold: null``
pin is belt-and-braces documentation of the deterministic posture.

Pinning is by-design, not a workaround: the knobs are contract inputs,
and a test whose subject is the SCRIPT (not the sampling/replication
machinery) should pin them. Tests whose subject IS a new default assert
the new value instead.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from zicato.core.types import (
    ProposerQualityConfig,
    ScoringWeights,
    TournamentStructure,
)
from zicato.epoch.contract import ContractInputs, resolve_contract_inputs

#: The param pins that restore the historical single-run, gate-off duel.
DETERMINISTIC_PARAM_PINS: dict[str, Any] = {
    "replicates": 1,
    "promote_confidence_threshold": None,
}


def pin_deterministic(weights: ScoringWeights) -> ScoringWeights:
    """Return ``weights`` with the deterministic scripted-test pins applied.

    * ``tournament.params`` gains ``replicates: 1`` and
      ``promote_confidence_threshold: null`` for any key the caller did not
      already pin (an explicit caller value always wins).
    * ``proposer_quality`` is pinned to the single-sample, no-critique
      proposer (``best_of_n=1``) unless the caller set a non-default config.
    """
    params = dict(weights.tournament_structure.params)
    for key, value in DETERMINISTIC_PARAM_PINS.items():
        params.setdefault(key, value)
    structure = TournamentStructure(
        structure=weights.tournament_structure.structure,
        params=params,
    )
    proposer_quality = weights.proposer_quality
    if proposer_quality == ProposerQualityConfig():
        proposer_quality = ProposerQualityConfig(best_of_n=1)
    return replace(
        weights,
        tournament_structure=structure,
        proposer_quality=proposer_quality,
    )


def deterministic_weights(**kwargs: Any) -> ScoringWeights:
    """A ``ScoringWeights(**kwargs)`` with the deterministic pins applied."""
    return pin_deterministic(ScoringWeights(**kwargs))


def resolved_contract_with_proposer(workspace_root: Path, proposer_path: Path) -> ContractInputs:
    """Return the full workspace contract with a local proposer selected.

    Known-answer tests create epochs through the library API. Carrying the
    resolved contract keeps the adapter identity and every other non-file
    component aligned with the workspace that the evolve loop validates.
    """
    return replace(resolve_contract_inputs(workspace_root), proposer_path=proposer_path)


__all__ = [
    "DETERMINISTIC_PARAM_PINS",
    "deterministic_weights",
    "pin_deterministic",
    "resolved_contract_with_proposer",
]
