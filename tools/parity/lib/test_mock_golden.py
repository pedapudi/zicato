"""Parity MOCK-GOLDEN gate, driven through pytest for the monkeypatch fixture.

Run by ``tools/parity.sh``. Two modes, selected by an env var:

* ``ZICATO_PARITY_UPDATE=1`` — write/refresh the committed golden.
* (default) — assert the freshly captured artifacts are byte-identical to
  the committed golden.

This is intentionally a single test so the parity script can invoke it in
isolation: ``uv run pytest -n0 tools/parity/lib/test_mock_golden.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from goldendiff import golden_mismatch_message
from mock_evolve_capture import GOLDEN_PATH, run_mock_evolve


def _canonical(doc: dict[str, object]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_mock_evolve_golden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = run_mock_evolve(monkeypatch, tmp_path)
    text = _canonical(captured)

    if os.environ.get("ZICATO_PARITY_UPDATE") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(text, encoding="utf-8")
        return

    assert GOLDEN_PATH.exists(), f"golden missing at {GOLDEN_PATH}; run with ZICATO_PARITY_UPDATE=1"
    expected = GOLDEN_PATH.read_text(encoding="utf-8")
    assert text == expected, golden_mismatch_message(
        "MOCK-GOLDEN drift: the deterministic racing mock evolve produced "
        "different serialized artifacts than the committed golden. A "
        "behavior-preserving refactor must not move these bytes.",
        expected,
        text,
        golden_path=str(GOLDEN_PATH),
    )
