"""zicato — a self-improving harness for multi-agent systems.

Library/driver contract
-----------------------

``zicato`` is a library first. Everything importable from this module is
the declared public surface: the evolve loop, the epoch lifecycle, the
board/scoring layer, the generation and record storage seams, the
harness-adapter contract, and the health diagnostics.

Three *drivers* sit on top of the library and consume THIS surface (plus
the documented subpackage modules, e.g. :mod:`zicato.query` for
dashboard-style reads):

* :mod:`zicato.cli` — the ``zicato`` command line.
* :mod:`zicato.dashboard` — the dashboard HTTP server.
* :mod:`zicato.builder` — the tournament-builder GUI backend.

Library packages never import the drivers; the allowed driver→driver
edges are declared in the import-linter contracts (``pyproject.toml``).

Every re-export below is lazy (module-level ``__getattr__``): importing
``zicato`` stays cheap so the CLI's fast ``--help`` path never pays for
the heavy modules.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.3.0"

#: Facade name -> (home module, attribute). ``None`` attribute means the
#: module object itself is the export.
_EXPORTS: dict[str, tuple[str, str | None]] = {
    # Evolve loop
    "evolve_once": ("zicato.orchestrator", "evolve_once"),
    "evolve_n_rounds": ("zicato.evolve.loop", "evolve_n_rounds"),
    "EvolveRoundOutcome": ("zicato.orchestrator", "EvolveRoundOutcome"),
    # Workspace
    "load_workspace_config": ("zicato.workspace_loader", "load_workspace_config"),
    "WorkspaceLayout": ("zicato.workspace", "WorkspaceLayout"),
    # Epoch lifecycle
    "new_epoch": ("zicato.epoch", "new_epoch"),
    "close_epoch": ("zicato.epoch", "close_epoch"),
    "close_epoch_async": ("zicato.epoch", "close_epoch_async"),
    "list_epochs": ("zicato.epoch", "list_epochs"),
    "switch_epoch": ("zicato.epoch", "switch_epoch"),
    "current_epoch_id": ("zicato.epoch", "current_epoch_id"),
    "load_epoch": ("zicato.epoch", "load_epoch"),
    "set_epoch_goal": ("zicato.epoch", "set_epoch_goal"),
    "set_epoch_noise_floor": ("zicato.epoch", "set_epoch_noise_floor"),
    "set_epoch_preflight": ("zicato.epoch", "set_epoch_preflight"),
    # Contract + preflight
    "default_contract_paths": ("zicato.epoch.contract", "default_contract_paths"),
    "run_contract_preflight": ("zicato.epoch.preflight", "run_contract_preflight"),
    "preflight_verdict": ("zicato.epoch.preflight", "preflight_verdict"),
    # Board + scoring
    "load_board": ("zicato.board", "load_board"),
    "ScoringWeights": ("zicato.core.scoring_config", "ScoringWeights"),
    "recommended_scaffold_weights": (
        "zicato.core.scoring_config",
        "recommended_scaffold_weights",
    ),
    # Generation storage (source trees)
    "GenerationStore": ("zicato.epoch.genstore", "GenerationStore"),
    "DirectoryGenerationStore": ("zicato.epoch.genstore", "DirectoryGenerationStore"),
    "GitGenerationStore": ("zicato.epoch.git_genstore", "GitGenerationStore"),
    "default_generation_store": ("zicato.epoch.genstore", "default_generation_store"),
    # Record storage
    "StorageBackend": ("zicato.storage", "StorageBackend"),
    "FileStorageBackend": ("zicato.storage", "FileStorageBackend"),
    "InMemoryStorageBackend": ("zicato.storage", "InMemoryStorageBackend"),
    # Harness adapter contract
    "CallLLM": ("zicato.core.runtime", "CallLLM"),
    "HarnessAdapter": ("zicato.adapters", "HarnessAdapter"),
    "RunnableHarness": ("zicato.adapters", "RunnableHarness"),
    # Health diagnostics
    "assess_loop_health": ("zicato.health", "assess_loop_health"),
    "measure_noise_floor": ("zicato.tournament.calibration", "measure_noise_floor"),
    # Round log
    "fold_round_record": ("zicato.epoch.round_log", "fold_round_record"),
    "round_log": ("zicato.epoch.round_log", None),
    # Config
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
    from zicato.epoch import close_epoch as close_epoch
    from zicato.epoch import close_epoch_async as close_epoch_async
    from zicato.epoch import current_epoch_id as current_epoch_id
    from zicato.epoch import list_epochs as list_epochs
    from zicato.epoch import load_epoch as load_epoch
    from zicato.epoch import new_epoch as new_epoch
    from zicato.epoch import round_log as round_log
    from zicato.epoch import set_epoch_goal as set_epoch_goal
    from zicato.epoch import set_epoch_noise_floor as set_epoch_noise_floor
    from zicato.epoch import set_epoch_preflight as set_epoch_preflight
    from zicato.epoch import switch_epoch as switch_epoch
    from zicato.epoch.contract import default_contract_paths as default_contract_paths
    from zicato.epoch.genstore import (
        DirectoryGenerationStore as DirectoryGenerationStore,
    )
    from zicato.epoch.genstore import GenerationStore as GenerationStore
    from zicato.epoch.genstore import (
        default_generation_store as default_generation_store,
    )
    from zicato.epoch.git_genstore import GitGenerationStore as GitGenerationStore
    from zicato.epoch.preflight import preflight_verdict as preflight_verdict
    from zicato.epoch.preflight import run_contract_preflight as run_contract_preflight
    from zicato.epoch.round_log import fold_round_record as fold_round_record
    from zicato.evolve.loop import evolve_n_rounds as evolve_n_rounds
    from zicato.health import assess_loop_health as assess_loop_health
    from zicato.orchestrator import EvolveRoundOutcome as EvolveRoundOutcome
    from zicato.orchestrator import evolve_once as evolve_once
    from zicato.storage import FileStorageBackend as FileStorageBackend
    from zicato.storage import InMemoryStorageBackend as InMemoryStorageBackend
    from zicato.storage import StorageBackend as StorageBackend
    from zicato.tournament.calibration import measure_noise_floor as measure_noise_floor
    from zicato.workspace import WorkspaceLayout as WorkspaceLayout
    from zicato.workspace_loader import load_workspace_config as load_workspace_config
