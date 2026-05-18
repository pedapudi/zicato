"""Run the dashboard's JavaScript test harness from pytest.

The dashboard frontend is a modular ES-module app: a thin ``app.js``
entry point plus the core spine, the shared component library and the
render layer under ``static/js/``. JavaScript behaviour — the
incremental render spine, the append-only log tail, matchup-click
survival across a state delta — is not reachable from the pure-parsing
structural tests in ``test_dashboard_ui.py``, so it has its own
dependency-free DOM + assertion harness under ``static/test/``.

This module shells out to ``node`` to run that harness as part of the
ordinary ``pytest`` run. When ``node`` is unavailable the test is
skipped rather than failed — the harness is a developer-facing check
and CI may not provision a JS runtime.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import zicato.dashboard as _dashboard_pkg

STATIC_DIR = Path(_dashboard_pkg.__file__).resolve().parent / "static"
TEST_DIR = STATIC_DIR / "test"
RUN_ALL = TEST_DIR / "run-all.mjs"

_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node runtime not available")
def test_dashboard_js_harness_passes() -> None:
    """The dashboard JS test harness runs green.

    ``test/run-all.mjs`` imports every ``*.test.mjs`` file, each of
    which installs its own minimal DOM and asserts a slice of frontend
    behaviour. A non-zero exit means a JS behaviour regression.
    """
    assert RUN_ALL.is_file(), f"missing JS test driver: {RUN_ALL}"
    proc = subprocess.run(  # noqa: S603 — _NODE is resolved via shutil.which
        [_NODE, str(RUN_ALL)],
        cwd=str(STATIC_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = proc.stdout + "\n" + proc.stderr
    assert proc.returncode == 0, f"dashboard JS harness failed:\n{output}"
    # A green run prints a per-file summary; assert the harness actually
    # executed tests rather than silently importing nothing.
    assert "passed" in output, f"JS harness produced no test output:\n{output}"
    assert "0 failed" in output, f"JS harness reported failures:\n{output}"
