"""Declare slow tests using measurements from isolated serial runs.

A bare pytest invocation deselects these tests. Selecting a file, node,
keyword or marker retains the requested tests. One repository-wide
collection verifies membership; command forms use a four-test repository
that imports the actual selection hook.

SLOW_TIER records each approved test and its measured seconds. To change
membership, measure with ``pytest -n0 --durations=0``, then update the marker
and its row together. The threshold is 15 seconds. Recorded times explain
membership; the test does not assert machine-dependent runtime.

The ``slow`` marker schedules tests by runtime. The independent
``integration`` marker identifies tests that cross a real process or
network boundary.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: File -> test name -> the seconds it measured ALONE. Ordered by cost.
SLOW_TIER: dict[str, dict[str, float]] = {
    "tests/test_recommended_complete_loop.py": {
        "test_interrupted_recommended_field_recovers": 42.83,
        "test_recommended_complete_round": 15.96,
        "test_partial_application_preserves_confirmation_requirements[measured]": 15.72,
    },
    "tests/test_gauntlet_evidence_gate_e2e.py": {
        "test_gauntlet_promote_confirmed_by_evidence_gate": 32.6,
    },
    "tests/test_convergence_known_answer.py": {
        "test_racing_field_best_arm_survives_to_floor": 30.7,
    },
    "tests/test_cascade_oc_harness.py": {
        "test_cascade_oc_smoke_end_to_end": 29.2,
    },
}


def declared_node_ids() -> dict[str, float]:
    """SLOW_TIER flattened to ``node id -> measured seconds``."""
    return {
        f"{filename}::{test}": secs
        for filename, tests in SLOW_TIER.items()
        for test, secs in tests.items()
    }


#: The measurement that decides membership.
THRESHOLD_SECONDS = 15.0


def _collect_marked(marker: str) -> set[str]:
    """Node ids carrying ``marker``, collected in a child pytest session.

    ``--collect-only`` with an explicit ``-m`` and ``-p no:cacheprovider``
    imports the test modules but runs nothing. The explicit ``-m`` replaces
    the ``addopts`` selector rather than intersecting with it, so the
    ``slow`` tier is reachable from inside a default (slow-excluding) run.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n0",
            "-q",
            "--collect-only",
            "-p",
            "no:cacheprovider",
            "-m",
            marker,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, (
        f"collecting -m {marker!r} failed ({completed.returncode}):\n"
        f"{completed.stdout}\n{completed.stderr}"
    )
    return {
        line.strip()
        for line in completed.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    }


@pytest.mark.integration
def test_the_slow_tier_is_exactly_the_declared_set() -> None:
    """Marked set == declared set. A drift in either direction is a failure."""
    marked = _collect_marked("slow and not node and not cascade_oc")
    declared = set(declared_node_ids())

    unlisted = sorted(marked - declared)
    assert not unlisted, (
        "these tests are marked `slow` but are not declared in SLOW_TIER. "
        "Measure each with `pytest --durations=0` and add its row, or drop "
        f"the mark: {unlisted}"
    )
    stale = sorted(declared - marked)
    assert not stale, (
        "these tests are declared in SLOW_TIER but carry no `slow` mark. "
        "Remove the row, or restore the mark if the test is still heavy: "
        f"{stale}"
    )


def test_every_declared_row_records_a_measurement_over_the_threshold() -> None:
    """The recorded seconds justify membership; a row under the bar is a typo."""
    under = {node: secs for node, secs in declared_node_ids().items() if secs < THRESHOLD_SECONDS}
    assert not under, (
        f"every SLOW_TIER row records at least {THRESHOLD_SECONDS} s, the "
        f"measurement that puts a test in the tier; these do not: {under}"
    )


@pytest.fixture(scope="module")
def selection_repository(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Exercise the repository hook against four inert tests."""
    root = tmp_path_factory.mktemp("marker-selection")
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\naddopts = -m 'not node and not cascade_oc'\n"
        "markers =\n    slow: measured runtime\n    node: browser suite\n"
        "    cascade_oc: optional measurement\n"
    )
    (root / "conftest.py").write_text("from tests.conftest import pytest_collection_modifyitems\n")
    (root / "tests/test_mixed.py").write_text(
        "import pytest\n"
        "def test_fast(): pass\n"
        "@pytest.mark.slow\ndef test_slow(): pass\n"
        "@pytest.mark.node\ndef test_browser(): pass\n"
        "@pytest.mark.cascade_oc\ndef test_measurement(): pass\n"
    )
    return root


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], {"fast"}),
        (["tests/test_mixed.py"], {"fast", "slow"}),
        (["tests/test_mixed.py::test_slow"], {"slow"}),
        (["-k", "slow"], {"slow"}),
        (["-kslow"], {"slow"}),
        (["-m", "slow and not node and not cascade_oc"], {"slow"}),
        (["-mslow"], {"slow"}),
        (["-m", "not node and not cascade_oc"], {"fast", "slow"}),
        (["-m", "node or cascade_oc"], {"browser", "measurement"}),
        (["tests/missing.py"], None),
        (["tests/test_mixed.py::test_missing"], None),
    ],
)
def test_command_selection_preserves_the_requested_tests(
    selection_repository: Path, args: list[str], expected: set[str] | None
) -> None:
    import os

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=selection_repository,
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_ADDOPTS": "",
            "PYTHONPATH": str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if expected is None:
        assert completed.returncode == 4, completed.stdout + completed.stderr
        return
    assert completed.returncode == 0, completed.stdout + completed.stderr
    collected = {
        line.removeprefix("tests/test_mixed.py::test_")
        for line in completed.stdout.splitlines()
        if line.startswith("tests/test_mixed.py::test_")
    }
    assert collected == expected
