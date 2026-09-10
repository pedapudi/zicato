"""Canonical record errors and storage keys for epoch data.

WorkspaceLayout owns record paths. These helpers convert its relative paths
into storage keys and validate record versions and finite JSON values."""

from __future__ import annotations

import json
from typing import Any

from zicato.workspace.layout import WORKSPACE_RELATIVE_LAYOUT as _LAYOUT
from zicato.workspace.layout import storage_key


def epochs_prefix() -> str:
    """Storage-key prefix every epoch's records sit under."""
    return storage_key(_LAYOUT.epochs_dir)


def epoch_prefix(epoch_id: str) -> str:
    """Storage-key prefix for one epoch's records."""
    return storage_key(_LAYOUT.epoch_dir(epoch_id))


def epoch_config_key(epoch_id: str) -> str:
    """Storage key for one epoch's ``config.json``."""
    return storage_key(_LAYOUT.epoch_config(epoch_id))


def scoring_key(epoch_id: str) -> str:
    """Storage key for one epoch's frozen ``scoring.json``."""
    return storage_key(_LAYOUT.scoring(epoch_id))


#: Supported stamp for canonical epoch, experiment, lineage, and score records.
#: Owners with independently versioned formats pass their expected version.
RECORD_FORMAT_VERSION = 1


class RecordError(RuntimeError):
    """A canonical JSON record is present and cannot be understood.

    The base every record decoder's refusal shares, so a view degrading at
    its own boundary catches one type and renders ``str(exc)`` as the
    reason. Absence is NOT this: a record that was never written raises
    :class:`FileNotFoundError` or is reported as ``None`` by a reader whose
    return type says so.
    """


class RecordFormatError(RecordError):
    """A canonical JSON record's ``format_version`` is not readable here."""


def copy_json_object(raw: Any, name: str) -> dict[str, Any]:
    """Validate finite JSON values and detach a record from the caller's mutable data."""
    if not isinstance(raw, dict):
        raise RecordError(f"{name}: expected a JSON object")
    try:
        result: dict[str, Any] = json.loads(json.dumps(raw, allow_nan=False))
        return result
    except (TypeError, ValueError) as exc:
        raise RecordError(f"{name}: invalid JSON value: {exc}") from exc


def check_record_format(
    body: dict[str, object],
    record_name: str,
    *,
    expected_version: int = RECORD_FORMAT_VERSION,
) -> None:
    """Require the owner's supported integer format stamp on a present record."""
    raw = body.get("format_version")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw == expected_version:
        return
    raise RecordFormatError(
        f"{record_name}: unsupported format_version {raw!r}; "
        f"expected integer {expected_version}"
    )


def lineage_key() -> str:
    """Storage key for the workspace-level ``lineage.json``."""
    return storage_key(_LAYOUT.lineage_path)


def current_epoch_key() -> str:
    """Storage key for the workspace ``current_epoch`` marker file."""
    return storage_key(_LAYOUT.current_epoch_marker)


def rounds_prefix(epoch_id: str) -> str:
    """Storage-key prefix one epoch's per-round records sit under."""
    return storage_key(_LAYOUT.rounds_dir(epoch_id))


def round_prefix(epoch_id: str, round_index: int) -> str:
    """Storage-key prefix for one evolve round's records."""
    return storage_key(_LAYOUT.round_dir(epoch_id, round_index))


def experiment_key(epoch_id: str, generation_id: str) -> str:
    """Storage key for a generation's ``experiment.json``."""
    return storage_key(_LAYOUT.experiment(epoch_id, generation_id))


def patches_prefix(epoch_id: str, generation_id: str) -> str:
    """Storage-key prefix the per-patch JSON records sit under."""
    return storage_key(_LAYOUT.patches_dir(epoch_id, generation_id))


def patch_key(epoch_id: str, generation_id: str, patch_id: str) -> str:
    """Storage key for one patch's JSON record."""
    return storage_key(_LAYOUT.patch_json(epoch_id, generation_id, patch_id))


__all__ = [
    "RECORD_FORMAT_VERSION",
    "RecordFormatError",
    "check_record_format",
    "epochs_prefix",
    "epoch_prefix",
    "epoch_config_key",
    "scoring_key",
    "lineage_key",
    "current_epoch_key",
    "rounds_prefix",
    "round_prefix",
    "experiment_key",
    "patches_prefix",
    "patch_key",
]
