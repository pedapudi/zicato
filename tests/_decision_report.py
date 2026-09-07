"""Immutable observations and complete seeded decision reports for assertions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DecisionObservation:
    workspace_seed: int
    generation_id: str
    entry_id: str
    replicate_index: int
    drift_loss: float
    passed: bool | None


@dataclass(frozen=True, slots=True)
class DecisionTrial:
    seed: int
    observations: tuple[DecisionObservation, ...]
    decision: str
    reason: str
    audit_json: str
    rating_eligibility: tuple[bool, ...] | None
    evidence_json: str | None = None

    @property
    def comparisons_spent(self) -> int:
        """Each comparison in the seeded harness advances the workspace seed."""
        return len({observation.workspace_seed for observation in self.observations})


@dataclass(frozen=True, slots=True)
class DecisionReport:
    inputs_json: str
    implementation_digest: str
    seeds: tuple[int, ...]
    trials: tuple[DecisionTrial, ...]

    def __post_init__(self) -> None:
        if not self.seeds or len(set(self.seeds)) != len(self.seeds):
            raise ValueError("a report requires a nonempty set of distinct seeds")
        if tuple(trial.seed for trial in self.trials) != self.seeds:
            raise ValueError("report trials must contain every requested seed once, in order")

    @property
    def promotion_rate(self) -> float:
        return sum(trial.decision == "promoted" for trial in self.trials) / len(self.seeds)


def implementation_digest() -> str:
    """Identify the production code, noise model and report producer being exercised."""
    root = Path(__file__).resolve().parents[1]
    paths = [
        *sorted((root / "src/zicato").rglob("*.py")),
        *sorted((root / "examples/zicato_examples/target_0_convergence").rglob("*.py")),
        root / "tests/test_decision_procedure_power.py",
        Path(__file__),
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()
