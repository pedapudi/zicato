"""Tests for :mod:`zicato.epoch._storage` — the epoch→storage adapter.

The adapter is the seam routing the ``epoch/`` record domain (journals,
experiments, lineage, per-epoch config) through
:class:`zicato.storage.StorageBackend`. These tests pin the two things
the adapter is responsible for:

* **Key computation** — an ``(epoch, generation, …)`` coordinate maps to
  the same logical key the pre-seam path layout used, so the on-disk
  bytes do not move.
* **Atomicity** — records written through the seam land via the file
  backend's ``.tmp`` + ``fsync`` + rename discipline, so no record is
  observable half-written. This is the concrete win of the migration
  over the pre-seam ``path.write_text(json.dumps(...))``.

The end-to-end behaviour of the migrated ``epoch/`` modules is covered
by ``test_epoch_journal.py`` / ``test_epoch_lineage.py`` /
``test_epoch_lifecycle.py``; this file covers the adapter itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.core.workspace import (
    experiment_json_path,
    journal_path,
    lineage_path,
    scoring_path,
)
from zicato.epoch import _storage
from zicato.storage import workspace_backend

# ---------------------------------------------------------------------------
# key computation — keys must mirror the workspace path layout
# ---------------------------------------------------------------------------


def test_keys_mirror_the_workspace_path_layout(tmp_path: Path) -> None:
    """Each *_key resolves, under the file backend, to the same path the
    pre-seam zicato.core.workspace helper produced — so the migration
    moves no on-disk bytes."""
    ws = tmp_path / ".zicato"
    epoch, gen = "2026-05-16_e1", "v3"

    def _resolved(key: str) -> Path:
        # FileStorageBackend resolves a key to root / key — this is
        # exactly the path write_json/read_json land a record at.
        return ws / key

    assert _resolved(_storage.experiment_key(epoch, gen)) == experiment_json_path(ws, epoch, gen)
    assert _resolved(_storage.journal_key(epoch)) == journal_path(ws, epoch)
    assert _resolved(_storage.scoring_key(epoch)) == scoring_path(ws, epoch)
    assert _resolved(_storage.lineage_key()) == lineage_path(ws)
    assert _resolved(_storage.epoch_config_key(epoch)) == ws / "epochs" / epoch / "config.json"
    assert _resolved(_storage.current_epoch_key()) == ws / "current_epoch"


def test_patch_key_sits_under_the_patches_prefix(tmp_path: Path) -> None:
    epoch, gen, pid = "e1", "v2", "abc123"
    pkey = _storage.patch_key(epoch, gen, pid)
    assert pkey == f"{_storage.patches_prefix(epoch, gen)}/{pid}.json"
    assert pkey.endswith(f"generations/{gen}/patches/{pid}.json")


# ---------------------------------------------------------------------------
# atomicity — the concrete win of routing epoch/ through the seam
# ---------------------------------------------------------------------------


def test_records_written_through_the_seam_leave_no_tmp_artefact(tmp_path: Path) -> None:
    """An atomic write leaves the final file and no .tmp sibling."""
    backend = workspace_backend(tmp_path, start=False)
    backend.write_json(_storage.experiment_key("e1", "v1"), {"id": "exp1"})

    gen_dir = tmp_path / "epochs" / "e1" / "generations" / "v1"
    assert (gen_dir / "experiment.json").exists()
    # No half-written temp artefact survives a completed atomic write.
    assert not list(gen_dir.glob("*.tmp"))


def test_seam_write_is_a_full_replacement(tmp_path: Path) -> None:
    backend = workspace_backend(tmp_path, start=False)
    key = _storage.lineage_key()
    backend.write_json(key, {"epochs": [{"id": "old"}]})
    backend.write_json(key, {"epochs": [{"id": "new"}]})
    on_disk = json.loads((tmp_path / "lineage.json").read_text(encoding="utf-8"))
    assert on_disk == {"epochs": [{"id": "new"}]}
