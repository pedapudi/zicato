#!/usr/bin/env python
"""Capture the trajectory-bootstrap UI reader fixtures (TRAJECTORY-UI.md §4.1).

Seeds a temp workspace, runs the REAL trajectory-bootstrap pipeline over the
real foreign-trace fixture dir, and writes the three reader payloads
(``list.json`` / ``detail.json`` / ``provenance.json``) so the sibling
view-agents' node render tests load payloads produced by the REAL readers —
never a hand-authored mock shape (the composition-check, rule 2).

Usage::

    uv run python tools/gen_trace_view_fixtures.py            # (re)write the fixtures
    uv run python tools/gen_trace_view_fixtures.py --check    # fail if they drifted

The readers are deterministic and the capture harness pins the (date-derived)
epoch id, so ``--check`` is a byte-stability gate the CI can assert.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._trace_view_harness import build_and_capture, canonical_json  # noqa: E402

#: Where the node render tests load the captured payloads from.
FIXTURE_DIR = (
    _REPO_ROOT / "src" / "zicato" / "dashboard" / "static" / "test" / "fixtures" / "trace_view"
)
_NAMES = ("list", "detail", "provenance")


def _capture() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        payloads = build_and_capture(Path(tmp))
    return {name: canonical_json(payloads[name]) for name in _NAMES}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the fixtures drifted")
    args = parser.parse_args()

    captured = _capture()
    if args.check:
        drifted: list[str] = []
        for name, text in captured.items():
            path = FIXTURE_DIR / f"{name}.json"
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                drifted.append(name)
        if drifted:
            print(
                "trace-view fixtures drifted: "
                + ", ".join(sorted(drifted))
                + " — re-run tools/gen_trace_view_fixtures.py",
                file=sys.stderr,
            )
            return 1
        print("trace-view fixtures are current.")
        return 0

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in captured.items():
        (FIXTURE_DIR / f"{name}.json").write_text(text, encoding="utf-8")
    print(f"wrote {len(captured)} trace-view fixtures to {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
