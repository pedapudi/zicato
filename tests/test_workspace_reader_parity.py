"""Byte-identical snapshot gate for every workspace reader outside the query layer.

The oracle for consolidating :mod:`zicato.analyzer`, :mod:`zicato.reflection`,
:mod:`zicato.health`, :mod:`zicato.index`, :mod:`zicato.workspace` and the
``zicato health`` command's independent walks of the ``.zicato/`` tree onto one
reader layer. Each of those readers is called against the deterministic fixture
in :mod:`tests._workspace_reader_parity_harness` and its output is pinned,
label by label, against a committed golden. A consolidation that changes any
reader's values or the ORDER of its rows fails here and has to say which change
was intended.

The gate has two levels:

* **Row order** — for the labels in ``ORDER_ENFORCED``, the identifier
  sequence is compared first, so a reordering fails with a message naming the
  label and showing both orders.
* **Byte identity** — every label's canonical JSON must equal the golden's.
  This subsumes the order check; the order check exists to make the common
  failure legible.

:mod:`tests.test_dashboard_reader_parity` is the same gate for the dashboard
query layer, against its own smaller fixture and its own golden. The two
goldens are independent: a change to one reader family never re-records the
other.

Re-record with ``ZICATO_PARITY_UPDATE=1``, and only once you can say, for
every label that moved, what changed and why.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from tests._reader_parity_harness import canonical_json
from tests._workspace_reader_parity_harness import (
    ENTRY_IDS,
    EPOCH_IDS,
    ORDER_ENFORCED,
    RICH_GENERATION_IDS,
    build_reader_fixture_workspace,
    capture_reader_snapshot,
    order_of,
)

_GOLDEN = Path(__file__).parent / "data" / "workspace_reader_parity_snapshot.json"


def _load_golden() -> dict[str, Any]:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def _maybe_update_golden(snapshot: dict[str, Any]) -> None:
    if os.environ.get("ZICATO_PARITY_UPDATE") == "1":
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def test_fixture_exposes_the_ordering_axes(tmp_path: Path) -> None:
    """The fixture really does make numeric and lexical order disagree.

    Without this the snapshot could pass while pinning nothing: a fixture whose
    ids happen to sort the same way under both rules cannot tell a numeric
    reader from a lexical one.
    """
    build_reader_fixture_workspace(tmp_path)

    assert len(RICH_GENERATION_IDS) == 11
    assert "v10" in RICH_GENERATION_IDS
    assert sorted(RICH_GENERATION_IDS) != list(RICH_GENERATION_IDS)
    assert sorted(ENTRY_IDS) != list(ENTRY_IDS)
    assert sorted(EPOCH_IDS) != list(EPOCH_IDS)


def test_fixture_is_reproducible(tmp_path: Path) -> None:
    """Two builds of the fixture produce the same snapshot.

    The snapshot is only an oracle if it depends on nothing but the fixture.
    Building it twice under different temporary roots and comparing the
    canonical JSON is what proves the masking covers every per-run value.
    """
    first = capture_reader_snapshot(build_reader_fixture_workspace(tmp_path / "a"))
    second = capture_reader_snapshot(build_reader_fixture_workspace(tmp_path / "b"))
    assert set(first) == set(second)
    for label in first:
        assert canonical_json(first[label]) == canonical_json(
            second[label]
        ), f"reader '{label}' is not reproducible across two builds of the same fixture"


def test_workspace_reader_parity_snapshot(tmp_path: Path) -> None:
    """Every pinned reader's output matches the golden, order included."""
    ws = build_reader_fixture_workspace(tmp_path)
    snapshot = capture_reader_snapshot(ws)
    _maybe_update_golden(snapshot)

    assert _GOLDEN.exists(), f"golden missing at {_GOLDEN}; record with ZICATO_PARITY_UPDATE=1"
    golden = _load_golden()

    # Same label set — no reader silently appeared or vanished.
    assert set(snapshot) == set(golden), (
        f"label set drift: only-new={sorted(set(snapshot) - set(golden))}, "
        f"only-golden={sorted(set(golden) - set(snapshot))}"
    )

    # Row order first, so the common failure names its label.
    for label in ORDER_ENFORCED:
        assert label in snapshot, f"ORDER_ENFORCED names '{label}', which no reader captures"
        current = order_of(label, snapshot[label])
        expected = order_of(label, golden[label])
        assert (
            current == expected
        ), f"reader '{label}' changed its row order:\n  {current}\n  {expected}"

    for label, value in snapshot.items():
        assert canonical_json(value) == canonical_json(
            golden[label]
        ), f"reader '{label}' no longer matches the golden"
