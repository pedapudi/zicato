"""Byte-identical-except-ordering harness for the dashboard readers.

The oracle for the ``zicato.workspace`` canonical-read-layer migration.
A deterministic multi-epoch fixture (one EMPTY epoch; one whose directory
name sorts BEFORE the others but whose ``created_at`` is LATER — the bug
mirror) drives every public ``build_*`` reader. The captured snapshot is
pinned against a committed golden:

* **non-epoch-list responses** must be BYTE-IDENTICAL to the golden — the
  migration moves these through the new layer without changing a byte.
* **epoch-list-bearing responses** must carry the same SET of epochs and
  identical per-epoch content as the golden, and present them in the
  canonical timestamp-first ``list_epoch_ids`` order — the intended fix.

Re-capture the golden with ``ZICATO_PARITY_UPDATE=1`` when an ordering fix
legitimately changes an epoch-list response; the byte-identity gate then
keeps every other response pinned.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from tests._reader_parity_harness import (
    CANONICAL_EPOCH_ORDER,
    EPOCH_LIST_LABELS,
    build_fixture_workspace,
    canonical_json,
    capture_snapshot,
    epoch_order_of,
)
from zicato.dashboard import state_reader as sr

_GOLDEN = Path(__file__).parent / "data" / "reader_parity_snapshot.json"

# The epoch-list-bearing labels whose epoch order the harness ENFORCES to be
# the canonical timestamp-first order. The other epoch-list labels are still
# pinned byte-identical to the golden (set + per-epoch content always); a
# label graduates into this set when its enumeration is migrated onto the
# single ordering authority and the golden is re-captured. ``meta_loop_ledger``
# (and therefore the ``ledger`` embedded in ``workspace_view``) joins once the
# events_index enumeration is migrated off name order.
ORDER_ENFORCED = frozenset(
    {
        "epochs_summary",
        "workspace_view",
        "lineage_view",
        # Migrated off name order onto the single ordering authority: the
        # events_index enumerations (build_meta_loop_ledger :705 +
        # build_contract_diff :579) now order by canonical timestamp-first.
        "meta_loop_ledger",
    }
)


def _load_golden() -> dict:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def _maybe_update_golden(snapshot: dict) -> None:
    if os.environ.get("ZICATO_PARITY_UPDATE") == "1":
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def test_canonical_epoch_order_is_timestamp_first(tmp_path: Path) -> None:
    """Sanity: the fixture's canonical order is timestamp-first, and the
    name/natural order is the WRONG order the bug produced (e0 first)."""
    ws = build_fixture_workspace(tmp_path)
    paths = sr.WorkspacePaths(ws)
    assert sr.list_epoch_ids(paths) == CANONICAL_EPOCH_ORDER
    # The bug mirror: e0's name sorts first but it is created last.
    assert CANONICAL_EPOCH_ORDER[0] == "e1"
    assert CANONICAL_EPOCH_ORDER[-1] == "e0"


def test_reader_parity_snapshot(tmp_path: Path) -> None:
    """Every reader response matches the golden (byte-identical for
    non-epoch-list responses; set + content for epoch-list responses)."""
    ws = build_fixture_workspace(tmp_path)
    snapshot = capture_snapshot(ws)
    _maybe_update_golden(snapshot)

    assert _GOLDEN.exists(), f"golden missing at {_GOLDEN}; capture with ZICATO_PARITY_UPDATE=1"
    golden = _load_golden()

    # Same set of labels — no reader silently appeared / vanished.
    assert set(snapshot) == set(golden), (
        f"label set drift: only-new={set(snapshot) - set(golden)}, "
        f"only-golden={set(golden) - set(snapshot)}"
    )

    for label, value in snapshot.items():
        if label in EPOCH_LIST_LABELS:
            # Set + per-epoch content equality (order-independent), plus the
            # canonical ordering invariant.
            _assert_epoch_list_parity(label, value, golden[label])
        else:
            assert canonical_json(value) == canonical_json(
                golden[label]
            ), f"non-epoch-list response '{label}' is not byte-identical to golden"


def _by_epoch(label: str, value: object) -> dict[str, object]:
    """Index an epoch-list response's per-epoch content by epoch id."""
    assert isinstance(value, dict | list)
    if label == "epochs_summary":
        assert isinstance(value, list)
        return {row["epoch_id"]: row for row in value}
    if label in ("workspace_view", "meta_loop_ledger"):
        assert isinstance(value, dict)
        rows = value["epochs"]
        return {row["epoch_id"]: row for row in rows}
    if label == "lineage_view":
        assert isinstance(value, dict)
        by: dict[str, object] = {}
        for node in value["generations"]:
            by.setdefault(node["epoch_id"], []).append(node)  # type: ignore[union-attr]
        return by
    raise AssertionError(f"unhandled epoch-list label {label}")


def _assert_epoch_list_parity(label: str, value: object, golden_value: object) -> None:
    # Same SET of epochs.
    cur_by = _by_epoch(label, value)
    gold_by = _by_epoch(label, golden_value)
    assert set(cur_by) == set(gold_by), f"{label}: epoch set drift"
    # Identical per-epoch content (order-independent).
    for eid in cur_by:
        assert canonical_json(cur_by[eid]) == canonical_json(
            gold_by[eid]
        ), f"{label}: per-epoch content drift for {eid}"
    # The fix: epoch order is the canonical timestamp-first order. Enforced
    # for the labels whose enumeration has been migrated onto the single
    # ordering authority (the rest are pinned byte-identical to the golden).
    if label in ORDER_ENFORCED:
        order = epoch_order_of(label, value)
        assert order is not None
        # Filter the canonical order to the epochs this response actually
        # carries (e.g. lineage_view omits the empty epoch, no generations).
        expected = [e for e in CANONICAL_EPOCH_ORDER if e in set(order)]
        assert order == expected, f"{label}: epoch order {order} != canonical {expected}"
    else:
        # Not yet migrated: pin the epoch order byte-identical to the golden
        # so a regression is still caught (the migration commit flips this to
        # the ORDER_ENFORCED branch and re-captures).
        assert epoch_order_of(label, value) == epoch_order_of(
            label, golden_value
        ), f"{label}: epoch order drifted from golden before migration"
