"""Parity MOCK-GOLDEN gates, driven through pytest for the monkeypatch fixture.

Run by ``tools/parity.sh``. Two modes, selected by an env var:

* ``ZICATO_PARITY_UPDATE=1`` — write/refresh the committed golden.
* (default) — assert the freshly captured artifacts are byte-identical to
  the committed golden.

One test, parametrized by capture lane
--------------------------------------
Each lane in :data:`~mock_evolve_capture.LANES` is a (tournament structure,
runtime mode) pair that executes production branches no other lane reaches
— the gauntlet's single-challenger selector and its holdout skip, and the
cache-first slot resolution of fast mode — and each has its own golden. The
parity script runs each lane as its own gate by selecting on the lane name
(``pytest -k <lane>``), so a failure names the configuration that moved.

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
from mock_evolve_capture import LANES, Lane, run_mock_evolve


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
