"""The small, lazy public API for running and configuring zicato.

Advanced APIs live in their owning subpackages. The package root intentionally
exports only the evolve loop, harness protocols, board/config loaders, and
scoring types.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.3.0"

#: Facade name -> (home module, attribute). ``None`` attribute means the
#: module object itself is the export.
_EXPORTS: dict[str, tuple[str, str | None]] = {
    "evolve_once": ("zicato.orchestrator", "evolve_once"),
    "evolve_n_rounds": ("zicato.evolve.loop", "evolve_n_rounds"),
    "EvolveRoundOutcome": ("zicato.orchestrator", "EvolveRoundOutcome"),
    "load_workspace_config": ("zicato.workspace_loader", "load_workspace_config"),
    "load_board": ("zicato.board", "load_board"),
    "ScoringWeights": ("zicato.core.scoring_config", "ScoringWeights"),
    "recommended_scaffold_weights": (
        "zicato.core.scoring_config",
        "recommended_scaffold_weights",
    ),
    "CallLLM": ("zicato.core.runtime", "CallLLM"),
    "HarnessAdapter": ("zicato.adapters", "HarnessAdapter"),
    "RunnableHarness": ("zicato.adapters", "RunnableHarness"),
    "ZicatoConfig": ("zicato.config", "ZicatoConfig"),
    "load_config": ("zicato.config", "load_config"),
}

__all__ = ["__version__", *sorted(_EXPORTS)]


def __getattr__(name: str) -> Any:
    """Resolve a facade name lazily from its home module."""
    try:
        module_name, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    module = importlib.import_module(module_name)
    value: Any = module if attr is None else getattr(module, attr)
    globals()[name] = value  # cache: subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


if TYPE_CHECKING:  # static-analysis view of the lazy surface
    from zicato.adapters import HarnessAdapter as HarnessAdapter
    from zicato.adapters import RunnableHarness as RunnableHarness
    from zicato.board import load_board as load_board
    from zicato.config import ZicatoConfig as ZicatoConfig
    from zicato.config import load_config as load_config
    from zicato.core.runtime import CallLLM as CallLLM
    from zicato.core.scoring_config import ScoringWeights as ScoringWeights
    from zicato.core.scoring_config import (
        recommended_scaffold_weights as recommended_scaffold_weights,
    )
    from zicato.evolve.loop import evolve_n_rounds as evolve_n_rounds
    from zicato.orchestrator import EvolveRoundOutcome as EvolveRoundOutcome
    from zicato.orchestrator import evolve_once as evolve_once
    from zicato.workspace_loader import load_workspace_config as load_workspace_config
