"""Explicit coordinates and writable scratch space for one measured run."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RunContext:
    """Available to harnesses through the runtime configuration passed to run."""

    workspace_root: Path
    epoch_id: str
    generation_id: str
    run_id: str
    snapshot_root: Path
    scratch_dir: Path | None
