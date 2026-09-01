"""The `slow` tier is a declared set, so it cannot drift silently.

The default `pytest` run excludes `slow` (see `[tool.pytest.ini_options]`
in `pyproject.toml`), so every mark added here removes a test from the run
a contributor gets by default. That is a deliberate scheduling decision
per test, and this module makes it reviewable: the marked set must equal
the set declared below, node id for node id. Adding a mark without adding
its row reds this test, and so does removing one.

The membership rule is a MEASUREMENT, and the measurement is SERIAL:

    pytest -n0 --durations=0 <the test>

A test belongs here when that reports 15 s or more. `-n0` is the whole
point. Under `-n auto` twelve workers contend for twelve cores and each
scripted test spawns worker processes of its own, so the same test reads
two to three times longer on a busy box than on an idle one — and a tier
whose membership depends on what else was running is not reproducible.
Six tests were tiered on `-n auto` numbers of 15 to 17 s and measured 5.5
to 7.4 s alone; they belong in the default tier and are in it.

The serial numbers leave a wide gap: the slowest test outside this tier
measures 7.4 s, the fastest inside it 29.2 s. Nothing sits near the line.

The seconds in each row are that measurement, recorded so a later reader
can tell a test that has always been heavy from one that grew into the
tier. They are documentation rather than an assertion — nothing here
re-times anything, because a timing assertion is a flake generator.

To change the tier: re-measure with `pytest -n0 --durations=0`, move the
mark, and move the row in the same commit.

The `slow` marker is about RUNTIME only. What a test TOUCHES is the
`integration` marker's subject, and the two are independent — most
`integration` tests are fast and run in the default tier.
"""

from __future__ import annotations

import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: File -> test name -> the seconds it measured ALONE. Ordered by cost.
SLOW_TIER: dict[str, dict[str, float]] = {
    "tests/test_decision_procedure_power.py": {
        "test_power_at_planted_deltas": 139.8,
        "test_naive_default_misses_small_effects_the_evidence_gate_catches": 44.9,
        "test_margin_below_noise_floor_without_evidence_gate_is_unsound": 36.7,
        "test_aa_effective_contract_false_promotion_rate_is_zero": 32.6,
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


@cache
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


# ---------------------------------------------------------------------------
# The bare default is the only invocation that drops the tier.
# ---------------------------------------------------------------------------


@cache
def _collected(*args: str) -> int:
    """How many tests a child pytest session collects for these arguments.

    Cached on the arguments: a child collection IMPORTS every test module,
    which costs seconds, and four of the assertions below ask for the same
    three argument sets. Nothing here changes the tree between calls, so
    one collection per set is all the information there is.
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
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert (
        completed.returncode == 0
    ), f"collecting {args} failed:\n{completed.stdout}\n{completed.stderr}"
    return len(
        [
            line
            for line in completed.stdout.splitlines()
            if line.startswith("tests/") and "::" in line
        ]
    )


#: A file holding one `slow` test and one that is not, so a count of two
#: distinguishes "ran what I named" from "silently dropped the slow half".
A_MIXED_SLOW_FILE = "tests/test_convergence_known_answer.py"


@pytest.mark.integration
def test_naming_a_file_runs_its_slow_tests_too() -> None:
    """Selecting something explicitly lifts the tier exclusion.

    Before the exclusion moved out of `addopts` and into a collection hook,
    this file collected one of its two tests and the run reported green.
    Every recipe in the guide that names an oracle file — including the
    two-oracles rule — was quietly covering less than it claimed.
    """
    assert _collected(A_MIXED_SLOW_FILE) == 2


@pytest.mark.integration
def test_naming_a_slow_test_by_node_id_runs_it() -> None:
    """The worst case of the same bug: naming a slow test ran NOTHING, and exited 0."""
    node_id = f"{A_MIXED_SLOW_FILE}::test_racing_field_best_arm_survives_to_floor"
    assert _collected(node_id) == 1


@pytest.mark.integration
def test_selecting_a_slow_test_by_keyword_runs_it() -> None:
    """`-k` is a selection by name, so it lifts the tier like a path does.

    Someone who types `pytest -k convergence` has said which tests they
    want; silently dropping the slow half of the match would be the same
    failure as dropping it when the file is named.
    """
    assert _collected("-k", "racing_field_best_arm") == 1


@pytest.mark.integration
def test_an_explicit_marker_expression_is_honoured_as_written() -> None:
    """`-m` replaces the configured expression, so the tier must not be re-applied."""
    assert _collected("-m", "slow and not node and not cascade_oc") == len(declared_node_ids())


@pytest.mark.integration
def test_the_bare_default_drops_the_tier_and_nothing_else() -> None:
    """The one invocation that skips the tier, and the reason the tier exists."""
    full = _collected("-m", "not node and not cascade_oc")
    default = _collected()
    assert full - default == len(declared_node_ids()), (
        "the bare default and the full suite differ by exactly the declared tier; "
        f"full={full} default={default} tier={len(declared_node_ids())}"
    )
