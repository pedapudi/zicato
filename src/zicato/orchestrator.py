"""Public dispatch surface for the evolve round pipeline."""

from zicato.evolve.epoching import ensure_epoch_for_contract
from zicato.evolve.gauntlet import evolve_once
from zicato.evolve.loop import evolve_n_rounds
from zicato.evolve.round_api import DEFERRED_INFRA_DECISION, EvolveRoundOutcome

__all__ = [
    "DEFERRED_INFRA_DECISION",
    "EvolveRoundOutcome",
    "ensure_epoch_for_contract",
    "evolve_n_rounds",
    "evolve_once",
]
