"""Cross-backend conformance suite for :class:`StorageBackend`.

Every backend in :mod:`zicato.storage` must round-trip the same operations
with the same observable semantics. This module is the canonical contract:
a backend that passes every test here is a drop-in for any zicato domain
routed through the storage seam.

Adding a third backend (the v0+1 git backend) is a one-line change —
append a :class:`BackendSpec` to ``BACKENDS`` describing how to build a
started backend for the test; the parametrised ``backend`` fixture does
the rest.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from zicato.storage import (
    FileStorageBackend,
    InMemoryStorageBackend,
    StorageBackend,
    make_storage_backend,
    workspace_backend,
)

# --- backend registry ------------------------------------------------------
#
# To add a backend, append one entry. ``build`` receives a pytest
# ``tmp_path`` so backends needing an on-disk location scope it to the
# test. The fixture calls ``start()`` / ``close()`` for you.


@dataclass
class BackendSpec:
    name: str
    build: Callable[[Path], StorageBackend]


BACKENDS: list[BackendSpec] = [
    BackendSpec(name="memory", build=lambda _tmp: make_storage_backend("memory")),
    BackendSpec(
        name="files",
        build=lambda tmp: make_storage_backend("files", root=tmp / "ws"),
    ),
]


@pytest.fixture(params=BACKENDS, ids=lambda b: b.name)
def backend(request, tmp_path: Path):
    spec: BackendSpec = request.param
    b = spec.build(tmp_path)
    b.start()
    try:
        yield b
    finally:
        b.close()


# === JSON records ==========================================================


def test_read_missing_json_returns_none(backend: StorageBackend):
    assert backend.read_json("runtime/ghost.json") is None


def test_write_then_read_json_round_trip(backend: StorageBackend):
    payload = {"pid": 7, "phase": "tournament", "entries": [1, 2, 3]}
    backend.write_json("runtime/heartbeat.json", payload)
    assert backend.read_json("runtime/heartbeat.json") == payload


def test_write_json_replaces_prior_value(backend: StorageBackend):
    backend.write_json("runtime/state.json", {"v": 1})
    backend.write_json("runtime/state.json", {"v": 2})
    assert backend.read_json("runtime/state.json") == {"v": 2}


def test_write_json_creates_intermediate_namespace(backend: StorageBackend):
    backend.write_json("runtime/active_runs/run_abc.json", {"run_id": "run_abc"})
    assert backend.read_json("runtime/active_runs/run_abc.json") == {"run_id": "run_abc"}


def test_written_json_is_decoupled_from_caller(backend: StorageBackend):
    payload = {"nested": {"k": "v"}}
    backend.write_json("k.json", payload)
    payload["nested"]["k"] = "mutated"
    assert backend.read_json("k.json") == {"nested": {"k": "v"}}


def test_read_json_is_decoupled_from_backend(backend: StorageBackend):
    backend.write_json("k.json", {"nested": {"k": "v"}})
    first = backend.read_json("k.json")
    first["nested"]["k"] = "mutated"
    assert backend.read_json("k.json") == {"nested": {"k": "v"}}


# === text records ==========================================================


def test_read_missing_text_returns_none(backend: StorageBackend):
    assert backend.read_text("runtime/control/pause_epoch") is None


def test_write_then_read_text_round_trip(backend: StorageBackend):
    backend.write_text("runtime/control/rubric.txt", "new rubric body\nline two")
    assert backend.read_text("runtime/control/rubric.txt") == "new rubric body\nline two"


def test_write_empty_text_is_a_valid_record(backend: StorageBackend):
    backend.write_text("runtime/control/pause_epoch", "")
    assert backend.read_text("runtime/control/pause_epoch") == ""
    assert backend.exists("runtime/control/pause_epoch") is True


# === existence / deletion ==================================================


def test_exists_false_for_missing_key(backend: StorageBackend):
    assert backend.exists("nope.json") is False


def test_exists_true_after_write(backend: StorageBackend):
    backend.write_json("present.json", {"ok": True})
    assert backend.exists("present.json") is True


def test_delete_returns_true_when_record_existed(backend: StorageBackend):
    backend.write_json("doomed.json", {"x": 1})
    assert backend.delete("doomed.json") is True
    assert backend.exists("doomed.json") is False
    assert backend.read_json("doomed.json") is None


def test_delete_missing_key_returns_false(backend: StorageBackend):
    assert backend.delete("never_existed.json") is False


def test_delete_is_idempotent(backend: StorageBackend):
    backend.write_json("once.json", {"x": 1})
    assert backend.delete("once.json") is True
    assert backend.delete("once.json") is False


# === listing ===============================================================


def test_list_keys_empty_prefix_returns_empty(backend: StorageBackend):
    assert backend.list_keys("runtime/active_runs") == []


def test_list_keys_returns_direct_children_sorted(backend: StorageBackend):
    backend.write_json("runtime/active_runs/run_c.json", {"id": "c"})
    backend.write_json("runtime/active_runs/run_a.json", {"id": "a"})
    backend.write_json("runtime/active_runs/run_b.json", {"id": "b"})
    keys = backend.list_keys("runtime/active_runs")
    assert keys == [
        "runtime/active_runs/run_a.json",
        "runtime/active_runs/run_b.json",
        "runtime/active_runs/run_c.json",
    ]


def test_list_keys_is_not_recursive(backend: StorageBackend):
    backend.write_json("data/top.json", {"x": 1})
    backend.write_json("data/nested/deep.json", {"x": 2})
    assert backend.list_keys("data") == ["data/top.json"]


def test_list_keys_excludes_sibling_prefixes(backend: StorageBackend):
    backend.write_json("runtime/active_runs/run_a.json", {"id": "a"})
    backend.write_json("runtime/heartbeat.json", {"pid": 1})
    assert backend.list_keys("runtime/active_runs") == ["runtime/active_runs/run_a.json"]


def test_list_keys_reflects_deletion(backend: StorageBackend):
    backend.write_json("d/a.json", {})
    backend.write_json("d/b.json", {})
    backend.delete("d/a.json")
    assert backend.list_keys("d") == ["d/b.json"]


def test_list_namespaces_empty_prefix_returns_empty(backend: StorageBackend):
    assert backend.list_namespaces("epochs/e1/generations") == []


def test_list_namespaces_returns_direct_children_sorted(backend: StorageBackend):
    for generation_id in ("v2", "v0", "v1"):
        backend.write_json(f"epochs/e1/generations/{generation_id}/experiment.json", {})
    assert backend.list_namespaces("epochs/e1/generations") == [
        "epochs/e1/generations/v0",
        "epochs/e1/generations/v1",
        "epochs/e1/generations/v2",
    ]


def test_list_namespaces_is_not_recursive(backend: StorageBackend):
    backend.write_json("epochs/e1/generations/v0/runs/t1/loss.json", {})
    assert backend.list_namespaces("epochs/e1") == ["epochs/e1/generations"]


def test_list_namespaces_excludes_plain_records(backend: StorageBackend):
    """A namespace holds records; a record beside it is not a namespace.

    The two listings partition a prefix's contents, so a caller enumerating
    generation records never picks up the ``config.json`` sitting beside the
    ``generations/`` subtree.
    """
    backend.write_json("epochs/e1/config.json", {"id": "e1"})
    backend.write_json("epochs/e1/generations/v0/experiment.json", {})
    assert backend.list_namespaces("epochs/e1") == ["epochs/e1/generations"]
    assert backend.list_keys("epochs/e1") == ["epochs/e1/config.json"]


# === JSONL streams =========================================================


def test_read_jsonl_missing_stream_yields_nothing(backend: StorageBackend):
    assert list(backend.read_jsonl("telemetry/events.jsonl")) == []


def test_append_jsonl_then_read_preserves_order(backend: StorageBackend):
    for i in range(4):
        backend.append_jsonl("telemetry/events.jsonl", {"seq": i, "kind": "e"})
    records = list(backend.read_jsonl("telemetry/events.jsonl"))
    assert [r["seq"] for r in records] == [0, 1, 2, 3]


def test_append_jsonl_does_not_overwrite(backend: StorageBackend):
    backend.append_jsonl("s.jsonl", {"a": 1})
    backend.append_jsonl("s.jsonl", {"b": 2})
    records = list(backend.read_jsonl("s.jsonl"))
    assert records == [{"a": 1}, {"b": 2}]


def test_appended_jsonl_record_is_decoupled_from_caller(backend: StorageBackend):
    rec = {"mutable": [1]}
    backend.append_jsonl("s.jsonl", rec)
    rec["mutable"].append(2)
    assert list(backend.read_jsonl("s.jsonl")) == [{"mutable": [1]}]


def test_jsonl_stream_is_deletable(backend: StorageBackend):
    backend.append_jsonl("s.jsonl", {"a": 1})
    assert backend.exists("s.jsonl") is True
    assert backend.delete("s.jsonl") is True
    assert list(backend.read_jsonl("s.jsonl")) == []


# === key validation ========================================================


@pytest.mark.parametrize("bad", ["", "/", "  ", "../escape.json", "a/../b.json"])
def test_invalid_keys_are_rejected(backend: StorageBackend, bad: str):
    with pytest.raises(ValueError):
        backend.write_json(bad, {"x": 1})


# === lifecycle =============================================================


def test_context_manager_starts_and_closes(tmp_path: Path):
    with FileStorageBackend(tmp_path / "ctx") as b:
        b.write_json("k.json", {"ok": True})
        assert b.read_json("k.json") == {"ok": True}


def test_start_is_idempotent(backend: StorageBackend):
    backend.start()
    backend.start()
    backend.write_json("k.json", {"ok": True})
    assert backend.read_json("k.json") == {"ok": True}


# === file backend specifics ================================================


def test_file_backend_layout_matches_plain_files(tmp_path: Path):
    """A record written through the seam lands at root/key on disk."""
    backend = FileStorageBackend(tmp_path)
    backend.start()
    backend.write_json("runtime/heartbeat.json", {"pid": 99})
    on_disk = tmp_path / "runtime" / "heartbeat.json"
    assert on_disk.exists()
    import json as _json

    assert _json.loads(on_disk.read_text()) == {"pid": 99}


def test_file_backend_list_keys_skips_tmp_artefacts(tmp_path: Path):
    backend = FileStorageBackend(tmp_path)
    backend.start()
    backend.write_json("d/real.json", {"x": 1})
    # Simulate a racing atomic write leaving a .tmp sibling.
    (tmp_path / "d" / "real.json.tmp").write_text("partial")
    assert backend.list_keys("d") == ["d/real.json"]


def test_memory_backend_is_isolated_per_instance():
    a = InMemoryStorageBackend()
    b = InMemoryStorageBackend()
    a.write_json("k.json", {"owner": "a"})
    assert b.read_json("k.json") is None


def test_make_storage_backend_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown storage backend"):
        make_storage_backend("redis")


def test_make_storage_backend_files_requires_root():
    with pytest.raises(ValueError, match="requires a root"):
        make_storage_backend("files")


# === the one workspace construction path ===================================
#
# Every domain gets its backend from workspace_backend, and the lifecycle
# it asks for decides whether construction touches the filesystem. The two
# tests below pin the side effect on each side of that choice, because the
# epoch/, runtime/ and workspace/ readers depend on the unstarted side
# leaving an absent workspace absent.


def test_workspace_backend_unstarted_creates_nothing(tmp_path: Path):
    root = tmp_path / "ws"
    backend = workspace_backend(root, start=False)
    assert isinstance(backend, FileStorageBackend)
    assert backend.root == root
    assert not root.exists()
    # A read of an absent workspace stays side-effect-free too.
    assert backend.read_json("runtime/heartbeat.json") is None
    assert not root.exists()


def test_workspace_backend_started_creates_the_root(tmp_path: Path):
    root = tmp_path / "ws"
    backend = workspace_backend(root, start=True)
    assert isinstance(backend, FileStorageBackend)
    assert backend.root == root
    assert root.is_dir()
