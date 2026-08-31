"""Implementation backend for ``zicato init``.

This module lives *outside* :mod:`zicato.cli.commands` so it is **not**
picked up by the discovery scan. The click-decorated command in
``zicato.cli.commands.init`` delegates to :func:`initialize_workspace`
here; keeping the body separable lets other entry points (a future
Python API, tests, an in-process bootstrap) reuse the same logic
without going through click.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from zicato.workspace.config_io import (
    CONFIG_FILENAME,
    LINEAGE_FILENAME,
    workspace_is_initialized,
    write_workspace_config,
)


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")


#: The only pre-existing ``config.json`` key ``--force`` carries across.
#: Named in the refusal message so the loss is stated rather than implied.
_PRESERVED_ON_FORCE = "generation_source_backend"


def _recorded_epoch_count(workspace_root: Path) -> int:
    """Return how many epochs the workspace's lineage records.

    Tolerant on the file: an absent, unreadable, or malformed
    ``lineage.json`` records nothing that ``--force`` could destroy, so it
    reads as zero. Only a well-formed DAG with epochs in it blocks a force.
    """
    try:
        loaded = json.loads((workspace_root / LINEAGE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 0
    if not isinstance(loaded, dict):
        return 0
    epochs = loaded.get("epochs")
    return len(epochs) if isinstance(epochs, list) else 0


def initialize_workspace(
    workspace_root: Path,
    *,
    instance_id: str,
    force: bool = False,
    reset_lineage: bool = False,
) -> dict[str, Any]:
    """Create ``workspace_root`` with an empty lineage and a config file.

    Returns the config that was written. Raises :class:`FileExistsError`
    if the workspace already exists and ``force`` is False, and also when
    ``force`` would discard a lineage that records at least one epoch —
    that discard needs ``reset_lineage`` said out loud, because the epochs,
    generations, and promotion decisions in ``lineage.json`` are not
    reconstructible from the workspace's other files.

    Force REPLACES ``config.json``: only ``generation_source_backend``
    carries across, so a registration's ``contract`` / ``mutable_trees`` /
    ``source_roots`` / ``adk_entrypoint`` are dropped and must be written
    again. To change only the source backend on a workspace that already
    exists, use ``zicato repair generation-source-backend`` instead — it
    merges one key and leaves the rest of the config and the lineage alone.

    Layout produced:

    * ``{workspace_root}/`` (directory)
    * ``{workspace_root}/config.json`` — ``{instance_id, created_at,
      generation_source_backend}``
    * ``{workspace_root}/lineage.json`` — empty DAG: ``{"epochs": []}``
      (the shape :func:`zicato.epoch.lineage.load_lineage` reads; any other
      shape is rejected as malformed and silently replaced with the empty
      DAG on the first mutation)
    * ``{workspace_root_parent}/scoring.json`` — the FULL recommended
      effective contract (racing field 4, replicates 2, evidence gate on;
      see :func:`zicato.core.scoring_config.recommended_scaffold_weights`),
      written only when no ``scoring.json`` exists there yet (never
      clobbered rather than even with ``force`` — it is the operator's live
      contract source, resolved by ``resolve_contract_inputs``).
    """
    if workspace_root.exists():
        if not force:
            raise FileExistsError(
                f"workspace {workspace_root!s} already exists; pass --force to overwrite"
            )
        # `force` only clears the two files we own; we don't recursively
        # delete the directory because epoch artifacts may live alongside.
        # Those artifacts are exactly why a recorded lineage cannot be
        # discarded silently: the epochs stay on disk while the DAG naming
        # them, and every promotion decision in it, is replaced by an empty
        # list. Nothing else on disk can rebuild that.
        recorded_epochs = _recorded_epoch_count(workspace_root)
        if recorded_epochs and not reset_lineage:
            raise FileExistsError(
                f"workspace {workspace_root!s}: --force would reset a "
                f"{LINEAGE_FILENAME} recording {recorded_epochs} epoch(s) to an empty "
                f"DAG, and would drop every config.json key except "
                f"{_PRESERVED_ON_FORCE!r}. To set only the generation source backend, "
                f"run `zicato repair generation-source-backend`. To re-initialize "
                f"anyway and discard the lineage, pass --reset-lineage."
            )

    from zicato.epoch.genstore import (  # noqa: PLC0415
        DEFAULT_GENERATION_SOURCE_BACKEND,
        GENERATION_SOURCE_BACKEND_KEY,
        KNOWN_GENERATION_SOURCE_BACKENDS,
    )

    source_backend = DEFAULT_GENERATION_SOURCE_BACKEND
    existing_config_path = workspace_root / CONFIG_FILENAME
    if force and existing_config_path.is_file():
        try:
            existing_config = json.loads(existing_config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing_config = None
        if isinstance(existing_config, dict):
            configured_backend = existing_config.get(GENERATION_SOURCE_BACKEND_KEY)
            if isinstance(configured_backend, str):
                normalized_backend = configured_backend.strip().lower()
                if normalized_backend in KNOWN_GENERATION_SOURCE_BACKENDS:
                    source_backend = normalized_backend

    workspace_root.mkdir(parents=True, exist_ok=True)

    lineage_path = workspace_root / LINEAGE_FILENAME
    if force or not lineage_path.exists():
        lineage_path.write_text(json.dumps({"epochs": []}, indent=2, sort_keys=True) + "\n")

    # The generation-store backend is recorded rather than left to the default.
    # Which store a workspace's generations live in is a durable property
    # of the workspace: writing it here means a later change to
    # DEFAULT_GENERATION_SOURCE_BACKEND cannot re-interpret a workspace that already
    # exists.
    config: dict[str, Any] = {
        "instance_id": instance_id,
        "created_at": _utcnow_iso(),
        GENERATION_SOURCE_BACKEND_KEY: source_backend,
        "models": {
            "engines": {},
            "roles": {},
            "_guide": {
                "nouns": {
                    "engine": "reusable model plus transport and credential-variable name",
                    "target": "adapter-defined system under test; may use no LLM",
                    "target_llm": "optional model injected by the target role",
                    "evaluation": "default for internal model work",
                    "proposer": "creates candidate changes; may merit a stronger engine",
                    "user_emulator": "constrained text role playing the user",
                    "judge": "constrained text/structured role scoring behavior",
                    "adjudicator": "independent constrained role auditing judges",
                    "builder": "interactive configuration assistant",
                    "proposer_generate/review": "candidate generation / critique and revision",
                    "revision": (
                        "operator-declared logical deployment identity, unlike a transport URL"
                    ),
                },
                "override": "define named engines, then map roles; see docs/design/MODEL-CONFIG.md",
                "example": {
                    "engines": {"strong": {"model": "..."}, "small": {"model": "..."}},
                    "roles": {"proposer": "strong", "user_emulator": "small"},
                },
            },
        },
    }
    write_workspace_config(workspace_root, config)

    # Scaffold the operator's live scoring.json with the FULL effective
    # contract — every field spelled out (the field-enumerating serializer),
    # so the recommended noise-aware knobs are visible and editable rather
    # than implicit. Lives at the default contract-source location
    # (<workspace_root_parent>/scoring.json). Only written when absent: an
    # existing contract is the operator's, never overwritten.
    scoring_scaffold = workspace_root.resolve().parent / "scoring.json"
    if not scoring_scaffold.exists():
        from zicato.core.scoring_config import recommended_scaffold_weights  # noqa: PLC0415
        from zicato.epoch.lifecycle import scoring_to_dict  # noqa: PLC0415

        scoring_scaffold.write_text(
            json.dumps(scoring_to_dict(recommended_scaffold_weights()), indent=2) + "\n"
        )

    # Sanity check that the config file is actually on disk now.
    assert workspace_is_initialized(
        workspace_root
    ), f"post-condition failed: {CONFIG_FILENAME} not present after init"
    return config


__all__ = ["initialize_workspace"]
