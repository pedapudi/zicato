"""Parity MOCK-GOLDEN gates, driven through pytest for the monkeypatch fixture.

Run by ``tools/parity.sh``. Two modes, selected by an env var:

* ``ZICATO_PARITY_UPDATE=1`` — write/refresh the committed golden.
* (default) — assert the freshly captured artifacts are byte-identical to
  the committed golden.

Two tests, each parametrized by capture lane
-------------------------------------------
Each lane in :data:`~mock_evolve_capture.LANES` is a (tournament structure,
runtime mode, round count) triple that executes production branches no other
lane reaches — the gauntlet's single-challenger selector, the cache-first
slot resolution of fast mode, the bracket and Swiss schedules — and each has
its own golden. The parity script runs each lane as its own gate by selecting
on the lane name (``pytest -k <lane>``), so a failure names the configuration
that moved, and both tests below run under that one selector.

:func:`test_mock_evolve_golden` is the byte-comparison. :func:`test_lane_board_
splits_to_a_non_empty_holdout` is the guard on what the goldens are able to
witness: a lane whose board holds nothing back exercises no holdout rule at
all, so it silently stops covering the train-slice selection and the crowning
confirmation while still passing.

Lane names are chosen so none is a substring of another: ``-k`` matches by
substring, and a selector that caught two lanes would report one gate's
result under another gate's name.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from goldendiff import golden_mismatch_message
from mock_evolve_capture import (
    LANES,
    Lane,
    lane_board_split,
    lane_epoch_id,
    run_mock_evolve,
)


def _canonical(doc: dict[str, object]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("lane", LANES.values(), ids=list(LANES))
def test_mock_evolve_golden(lane: Lane, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = run_mock_evolve(monkeypatch, tmp_path, lane)
    text = _canonical(captured)
    golden_path: Path = lane.golden_path

    if os.environ.get("ZICATO_PARITY_UPDATE") == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(text, encoding="utf-8")
        return

    assert golden_path.exists(), f"golden missing at {golden_path}; run with ZICATO_PARITY_UPDATE=1"
    expected = golden_path.read_text(encoding="utf-8")
    assert text == expected, golden_mismatch_message(
        f"MOCK-GOLDEN drift ({lane.name}): the deterministic mock evolve produced "
        "different serialized artifacts than the committed golden. A "
        "behavior-preserving refactor must not move these bytes.",
        expected,
        text,
        golden_path=str(golden_path),
    )


@pytest.mark.parametrize("lane", LANES.values(), ids=list(LANES))
def test_lane_board_splits_to_a_non_empty_holdout(lane: Lane) -> None:
    """Every lane must hold some board entry back, or it witnesses nothing.

    The holdout is hash-derived and salted by the epoch id, which is per-lane
    (``PINNED_CAPTURE_DATE`` plus the lane's ``epoch_name``). Nothing forces a
    given salt to place any entry in the holdout, and when none lands there
    the split degrades to the whole board. A lane in that state still captures
    and still passes its byte comparison, while covering neither train-slice
    selection nor the crowning holdout confirmation — which is how the
    fast-mode gauntlet lane came to pin a holdout rule it never ran (issue
    #319). Renaming a lane, retuning ``holdout_fraction``, or editing the
    example board can all put a lane back in that state, so the property is
    asserted rather than assumed.

    The fix is to choose a different ``epoch_name`` for the lane: the name is
    the salt, and a descriptive alternative that splits non-empty is usually
    one or two tries away. Recapture that lane afterwards.
    """
    train_ids, holdout_ids = lane_board_split(lane)

    assert holdout_ids, (
        f"lane {lane.name!r} (epoch id {lane_epoch_id(lane)!r}) splits its "
        f"{len(train_ids)}-entry board to an EMPTY holdout, so it exercises no "
        "holdout rule and its golden cannot witness one. Choose a different "
        "epoch_name for the lane and recapture it."
    )
    assert train_ids, (
        f"lane {lane.name!r} (epoch id {lane_epoch_id(lane)!r}) holds out its "
        "whole board, leaving nothing to select on."
    )
