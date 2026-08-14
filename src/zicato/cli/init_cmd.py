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


def initialize_workspace(
    workspace_root: Path,
    *,
    instance_id: str,
    force: bool = False,
) -> dict[str, Any]:
    """Create ``workspace_root`` with an empty lineage and a config file.

    Returns the config that was written. Raises :class:`FileExistsError`
    if the workspace already exists and ``force`` is False.

    Layout produced:

    * ``{workspace_root}/`` (directory)
    * ``{workspace_root}/config.json`` — ``{instance_id, created_at,
      storage_backend}``
    * ``{workspace_root}/lineage.json`` — empty DAG: ``{"epochs": []}``
      (the shape :func:`zicato.epoch.lineage.load_lineage` reads; the
      seed used to be ``{"nodes": [], "edges": []}``, which the loader
      rejected as malformed and silently replaced with the empty DAG on
      the first mutation)
    * ``{workspace_root_parent}/scoring.json`` — the FULL recommended
      effective contract (racing field 4, replicates 2, evidence gate on;
      see :func:`zicato.core.scoring_config.recommended_scaffold_weights`),
      written only when no ``scoring.json`` exists there yet (never
      clobbered, not even with ``force`` — it is the operator's live
      contract source, resolved by ``resolve_contract_inputs``).
    """
    if workspace_root.exists():
        if not force:
            raise FileExistsError(
                f"workspace {workspace_root!s} already exists; pass --force to overwrite"
            )
        # `force` only clears the two files we own; we don't recursively
        # delete the directory because epoch artifacts may live alongside.

    workspace_root.mkdir(parents=True, exist_ok=True)

    lineage_path = workspace_root / LINEAGE_FILENAME
    if force or not lineage_path.exists():
        lineage_path.write_text(json.dumps({"epochs": []}, indent=2, sort_keys=True) + "\n")

    # The generation-store backend is recorded, not left to the default.
    # Which store a workspace's generations live in is a durable property
    # of the workspace: writing it here means a later change to
    # DEFAULT_STORAGE_BACKEND cannot re-interpret a workspace that already
    # exists.
    from zicato.epoch.genstore import (  # noqa: PLC0415
        DEFAULT_STORAGE_BACKEND,
        STORAGE_BACKEND_KEY,
    )

    config: dict[str, Any] = {
        "instance_id": instance_id,
        "created_at": _utcnow_iso(),
        STORAGE_BACKEND_KEY: DEFAULT_STORAGE_BACKEND,
        "models": {
            "engines": {},
            "roles": {},
            "_guide": {
                "nouns": {
                    "engine": "reusable model plus transport and credential-variable name",
                    "target": "system under test",
                    "evaluation": "default for internal model work",
                    "proposer": "creates candidate changes; may merit a stronger engine",
                    "user_emulator": "plays the user; may use a smaller engine",
                    "judge": "scores run behavior",
                    "adjudicator": "independently audits judges",
                    "builder": "interactive configuration assistant",
                    "breadth/depth": "candidate sampling / critique and revision",
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
