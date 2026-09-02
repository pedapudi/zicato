"""Correspondence guards for storage keys and the layout declarations they name.

A storage key is a persisted address: :class:`~zicato.storage.FileStorageBackend`
resolves ``key`` as ``root / key``, so the key for a record and the
:class:`~zicato.workspace.layout.WorkspaceLayout` path for the same record are
two readings of one location. Only one of them is a declaration: the layout
member names the directories and the filename, and the ``*_key`` helper reads
that member.

The registry below names each key helper beside the layout member that owns its
location, so nothing can move one reading without the other. Four guards read
it:

* :func:`test_every_key_equals_its_layout_declaration` — string correspondence:
  the helper returns the layout's path for that record, stated relative to the
  workspace root.
* :func:`test_backend_resolves_every_key_to_its_layout_path` — the same claim on
  a real filesystem: a write through the backend at the key produces a file at
  the absolute path the layout resolves for the same record.
* :func:`test_key_spellings_are_unchanged` — the frozen spellings. A key names
  data already written into operators' workspaces, so changing one orphans that
  data; this pins every key against the literal it has always had.
* :func:`test_every_key_helper_is_registered` — a new key helper must be entered
  here, which is what keeps the first three guards exhaustive.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from zicato.epoch import _storage as epoch_keys
from zicato.evolve.settlement_recovery import field_settlement_intent_key
from zicato.runtime import _storage as runtime_keys
from zicato.storage import workspace_backend
from zicato.workspace.layout import WORKSPACE_RELATIVE_LAYOUT, WorkspaceLayout, storage_key

# Sample coordinates. The ids are shaped like the real ones (a timestamped
# epoch id, a ``v{n}`` generation id, a ``{generation}--{entry}`` run id) so a
# row exercises the same joins a live workspace does.
EPOCH = "2026-01-02-fixture"
GENERATION = "v3"
ENTRY = "t1"
PATCH = "p1"
RUN = f"{GENERATION}--{ENTRY}"
ROUND = 7


@dataclass(frozen=True)
class KeyDeclaration:
    """One storage key beside the layout declaration that owns its location."""

    #: Name of the ``*_key`` / ``*_prefix`` helper this row covers.
    helper: str
    #: The key that helper returns for the sample coordinates above.
    key: str
    #: The same record's path, resolved off any layout.
    path: Callable[[WorkspaceLayout], Path]
    #: The frozen spelling: what the key has always been on disk.
    spelling: str
    #: Whether the key names a directory of records rather than one record.
    directory: bool = False


_STORAGE_KEYS: tuple[KeyDeclaration, ...] = (
    KeyDeclaration(
        "epochs_prefix",
        epoch_keys.epochs_prefix(),
        lambda layout: layout.epochs_dir,
        "epochs",
        directory=True,
    ),
    KeyDeclaration(
        "epoch_prefix",
        epoch_keys.epoch_prefix(EPOCH),
        lambda layout: layout.epoch_dir(EPOCH),
        f"epochs/{EPOCH}",
        directory=True,
    ),
    KeyDeclaration(
        "epoch_config_key",
        epoch_keys.epoch_config_key(EPOCH),
        lambda layout: layout.epoch_config(EPOCH),
        f"epochs/{EPOCH}/config.json",
    ),
    KeyDeclaration(
        "scoring_key",
        epoch_keys.scoring_key(EPOCH),
        lambda layout: layout.scoring(EPOCH),
        f"epochs/{EPOCH}/scoring.json",
    ),
    KeyDeclaration(
        "journal_key",
        epoch_keys.journal_key(EPOCH),
        lambda layout: layout.journal(EPOCH),
        f"epochs/{EPOCH}/journal.md",
    ),
    KeyDeclaration(
        "lineage_key",
        epoch_keys.lineage_key(),
        lambda layout: layout.lineage_path,
        "lineage.json",
    ),
    KeyDeclaration(
        "current_epoch_key",
        epoch_keys.current_epoch_key(),
        lambda layout: layout.current_epoch_marker,
        "current_epoch",
    ),
    KeyDeclaration(
        "rounds_prefix",
        epoch_keys.rounds_prefix(EPOCH),
        lambda layout: layout.rounds_dir(EPOCH),
        f"epochs/{EPOCH}/rounds",
        directory=True,
    ),
    KeyDeclaration(
        "round_prefix",
        epoch_keys.round_prefix(EPOCH, ROUND),
        lambda layout: layout.round_dir(EPOCH, ROUND),
        f"epochs/{EPOCH}/rounds/{ROUND}",
        directory=True,
    ),
    KeyDeclaration(
        "experiment_key",
        epoch_keys.experiment_key(EPOCH, GENERATION),
        lambda layout: layout.experiment(EPOCH, GENERATION),
        f"epochs/{EPOCH}/generations/{GENERATION}/experiment.json",
    ),
    KeyDeclaration(
        "patches_prefix",
        epoch_keys.patches_prefix(EPOCH, GENERATION),
        lambda layout: layout.patches_dir(EPOCH, GENERATION),
        f"epochs/{EPOCH}/generations/{GENERATION}/patches",
        directory=True,
    ),
    KeyDeclaration(
        "patch_key",
        epoch_keys.patch_key(EPOCH, GENERATION, PATCH),
        lambda layout: layout.patch_json(EPOCH, GENERATION, PATCH),
        f"epochs/{EPOCH}/generations/{GENERATION}/patches/{PATCH}.json",
    ),
    KeyDeclaration(
        "field_settlement_intent_key",
        field_settlement_intent_key(EPOCH, ROUND),
        lambda layout: layout.round_dir(EPOCH, ROUND) / "field_settlement.json",
        f"epochs/{EPOCH}/rounds/{ROUND}/field_settlement.json",
    ),
    KeyDeclaration(
        "heartbeat_key",
        runtime_keys.heartbeat_key(),
        lambda layout: layout.heartbeat,
        "runtime/heartbeat.json",
    ),
    KeyDeclaration(
        "lock_key",
        runtime_keys.lock_key(),
        lambda layout: layout.lock,
        "runtime/lock.json",
    ),
    KeyDeclaration(
        "active_tournament_key",
        runtime_keys.active_tournament_key(),
        lambda layout: layout.active_tournament,
        "runtime/active_tournament.json",
    ),
    KeyDeclaration(
        "active_tournament_log_key",
        runtime_keys.active_tournament_log_key(),
        lambda layout: layout.active_tournament_log,
        "runtime/active_tournament.events.jsonl",
    ),
    KeyDeclaration(
        "progress_log_key",
        runtime_keys.progress_log_key(),
        lambda layout: layout.progress_log,
        "runtime/progress.events.jsonl",
    ),
    KeyDeclaration(
        "active_runs_prefix",
        runtime_keys.active_runs_prefix(),
        lambda layout: layout.active_runs_dir,
        "runtime/active_runs",
        directory=True,
    ),
    KeyDeclaration(
        "active_run_key",
        runtime_keys.active_run_key(RUN),
        lambda layout: layout.active_run(RUN),
        f"runtime/active_runs/{RUN}.json",
    ),
    KeyDeclaration(
        "control_prefix",
        runtime_keys.control_prefix(),
        lambda layout: layout.control_dir,
        "runtime/control",
        directory=True,
    ),
    KeyDeclaration(
        "control_command_key",
        runtime_keys.control_command_key(f"kill_runs/{RUN}"),
        lambda layout: layout.control_command(f"kill_runs/{RUN}"),
        f"runtime/control/kill_runs/{RUN}",
    ),
    KeyDeclaration(
        "control_log_prefix",
        runtime_keys.control_log_prefix(),
        lambda layout: layout.control_log_dir,
        "runtime/control_log",
        directory=True,
    ),
    KeyDeclaration(
        "kill_request_key",
        runtime_keys.kill_request_key(RUN),
        lambda layout: layout.kill_request(RUN),
        f"runtime/control/kill_requests/{RUN}",
    ),
)


def test_every_key_equals_its_layout_declaration() -> None:
    """Each key is the layout's path for that record, relative to the root."""
    for row in _STORAGE_KEYS:
        declared = storage_key(row.path(WORKSPACE_RELATIVE_LAYOUT))
        assert row.key == declared, (
            f"{row.helper}() returns {row.key!r} but the layout declares "
            f"{declared!r} for the same record"
        )


def test_backend_resolves_every_key_to_its_layout_path(tmp_path: Path) -> None:
    """A write at the key lands where the layout resolves the same record.

    String correspondence alone would still pass if the backend prepended
    layout of its own. Writing through the backend and looking for the file at
    the layout's absolute path proves the two agree on a real filesystem.
    """
    root = tmp_path / ".zicato"
    backend = workspace_backend(root, start=True)
    layout = WorkspaceLayout.from_root(root)
    for row in _STORAGE_KEYS:
        key = f"{row.key}/probe.json" if row.directory else row.key
        expected = row.path(layout) / "probe.json" if row.directory else row.path(layout)
        backend.write_json(key, {"helper": row.helper})
        assert json.loads(expected.read_text(encoding="utf-8")) == {"helper": row.helper}, (
            f"{row.helper}() resolved to a file the layout does not name; " f"expected {expected}"
        )


def test_key_spellings_are_unchanged() -> None:
    """Every key still spells the address its data was written under.

    A key names records already on disk in operators' workspaces. Changing one
    does not migrate that data, it orphans it, so a spelling change has to be a
    deliberate edit here rather than a side effect of moving a declaration.
    """
    for row in _STORAGE_KEYS:
        assert row.key == row.spelling, (
            f"{row.helper}() now returns {row.key!r}; records already written "
            f"under {row.spelling!r} would no longer be found"
        )


def test_every_key_helper_is_registered() -> None:
    """No key helper exists that the guards above do not cover."""
    exported = {
        name
        for module in (epoch_keys, runtime_keys)
        for name in module.__all__
        if name.endswith(("_key", "_prefix"))
    }
    exported.add("field_settlement_intent_key")
    registered = {row.helper for row in _STORAGE_KEYS}
    assert exported == registered, (
        "the storage-key registry has drifted from the key helpers: "
        f"unregistered {sorted(exported - registered)}, "
        f"stale {sorted(registered - exported)}"
    )
