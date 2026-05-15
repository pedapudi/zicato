"""Migration helpers for the per-patch storage layout.

A workspace created before the v0 storage refactor carries
``experiment.json`` files with patches serialised inline as a
``patches: [...]`` array. The new on-disk shape uses
``patch_ids: [...]`` on ``experiment.json`` and one
``patches/{id}.json`` file per patch — see
:doc:`project_zicato_storage_design`.

The reader path in :func:`zicato.epoch.journal.read_experiment`
transparently accepts both shapes, so migration is opportunistic —
operators are NOT forced to migrate old data to keep working. The
helpers here exist for the case where the operator wants to bring an
old generation in line with the new layout, for example before
archiving an epoch.

Usage::

    from zicato.epoch.migrate import migrate_inline_to_perpatch
    summary = migrate_inline_to_perpatch(generation_dir)
    print(summary.migrated_patch_ids)

The function is idempotent: running it twice against the same
generation directory is a no-op once the first run completes. A
generation directory whose ``experiment.json`` already uses the new
shape returns a summary with ``already_per_patch=True`` and no
filesystem changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class MigrationSummary:
    """Outcome of one :func:`migrate_inline_to_perpatch` call.

    Fields
    ------
    success:
        ``True`` iff the generation directory ends up in the new
        per-patch shape (regardless of whether this call did the work
        or a previous one did).
    already_per_patch:
        ``True`` iff the directory was already in the new shape when
        we looked at it. No files were touched in that case.
    migrated_patch_ids:
        Ids of the patches written out as per-patch files during this
        call. Empty when ``already_per_patch`` is true or when the
        experiment had zero patches inline.
    error:
        Short symbolic error string when ``success`` is ``False``;
        empty otherwise.
    """

    success: bool
    already_per_patch: bool
    migrated_patch_ids: tuple[str, ...]
    error: str = ""


def migrate_inline_to_perpatch(generation_dir: Path) -> MigrationSummary:
    """Rewrite one ``generation_dir`` from inline-patches to per-patch files.

    Steps:

    1. Read ``experiment.json``. If it already has ``patch_ids`` and
       NO ``patches`` field, return an ``already_per_patch=True``
       summary without touching anything.
    2. Otherwise read the inline ``patches`` list, write one
       ``patches/{id}.json`` per patch.
    3. Rewrite ``experiment.json`` with ``patch_ids: [...]`` in place
       of ``patches: [...]``. All other fields (hypothesis, outcome,
       etc.) are preserved verbatim.

    The write order matches :func:`zicato.epoch.journal.write_experiment`:
    patches first, then ``experiment.json`` last. Migration aborts
    early on missing ``id`` field on any patch (we refuse to silently
    drop a patch and we don't try to synthesise an id — the inline
    file already had one).

    Returns
    -------
    MigrationSummary
        Always returns; never raises for migration-time problems.
        Filesystem errors propagate (the caller is expected to handle
        permission / I/O issues).
    """
    exp_path = generation_dir / "experiment.json"
    if not exp_path.exists():
        return MigrationSummary(
            success=False,
            already_per_patch=False,
            migrated_patch_ids=(),
            error=f"experiment.json not found at {exp_path}",
        )
    try:
        body: dict[str, Any] = json.loads(exp_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return MigrationSummary(
            success=False,
            already_per_patch=False,
            migrated_patch_ids=(),
            error=f"could not parse {exp_path}: {exc.msg}",
        )

    raw_inline = body.get("patches")
    has_ids = "patch_ids" in body

    if has_ids and not isinstance(raw_inline, list):
        # Already in new shape — no work.
        return MigrationSummary(
            success=True,
            already_per_patch=True,
            migrated_patch_ids=(),
        )

    if not isinstance(raw_inline, list):
        # Neither shape — this experiment has no patches recorded.
        # Still convert to the new schema by setting an empty patch_ids
        # list so downstream readers do not see a missing field.
        new_body = dict(body)
        new_body.pop("patches", None)
        new_body["patch_ids"] = []
        exp_path.write_text(
            json.dumps(new_body, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return MigrationSummary(
            success=True,
            already_per_patch=False,
            migrated_patch_ids=(),
        )

    patches_dir = generation_dir / "patches"
    patches_dir.mkdir(parents=True, exist_ok=True)
    written_ids: list[str] = []
    for patch in raw_inline:
        if not isinstance(patch, dict) or "id" not in patch:
            return MigrationSummary(
                success=False,
                already_per_patch=False,
                migrated_patch_ids=tuple(written_ids),
                error="inline patch missing 'id' field; refusing to synthesise",
            )
        pid = str(patch["id"])
        out_path = patches_dir / f"{pid}.json"
        out_path.write_text(
            json.dumps(patch, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written_ids.append(pid)

    new_body = dict(body)
    new_body.pop("patches", None)
    new_body["patch_ids"] = written_ids
    exp_path.write_text(
        json.dumps(new_body, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return MigrationSummary(
        success=True,
        already_per_patch=False,
        migrated_patch_ids=tuple(written_ids),
    )


__all__ = ["MigrationSummary", "migrate_inline_to_perpatch"]
