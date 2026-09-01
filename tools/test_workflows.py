"""Validate the GitHub workflow definitions before GitHub reads them.

PyYAML accepts repeated mapping keys and steps without a command, although
GitHub rejects those forms without running any jobs. These tests reject those
forms and pin workflow triggers that provide before-merge results.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PULL_REQUEST_CI = WORKFLOWS / "ci.yml"
STATISTICAL_ORACLES = WORKFLOWS / "slow-tier.yml"


class DuplicateKeyError(AssertionError):
    """A mapping repeated a key, which YAML allows and GitHub does not."""


class _StrictLoader(yaml.SafeLoader):
    """A SafeLoader that refuses a repeated key instead of keeping the last."""


def _no_duplicate_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[str, Any]:
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise DuplicateKeyError(
                f"{key!r} is declared twice in one mapping at line "
                f"{key_node.start_mark.line + 1}"
            )
        seen.add(key)
    return loader.construct_mapping(node)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_mapping)


def workflow_paths() -> list[Path]:
    """Every workflow file, so a new one is covered without an edit here."""
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def load_workflow(path: Path) -> dict[Any, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)


def assert_unfiltered_pull_request(document: dict[Any, Any]) -> dict[Any, Any]:
    """Return workflow events after proving every pull request triggers them."""
    events = document[True]  # PyYAML 1.1 parses GitHub's `on` key as true.
    assert "pull_request" in events
    assert events["pull_request"] in (None, {}), (
        "the policy check must run on every pull request; branch or path filters "
        "can leave a required result unreported"
    )
    return events


def test_there_are_workflows_to_check() -> None:
    """Guard the guard: an empty glob would make every test below vacuous."""
    assert workflow_paths(), f"no workflow files under {WORKFLOWS}"


@pytest.mark.parametrize("path", workflow_paths(), ids=lambda p: p.name)
def test_no_mapping_declares_a_key_twice(path: Path) -> None:
    """The defect PyYAML hides: two `run:` keys on one step, last one wins."""
    load_workflow(path)


@pytest.mark.parametrize("path", workflow_paths(), ids=lambda p: p.name)
def test_every_step_has_exactly_one_of_run_or_uses(path: Path) -> None:
    """Reject steps with neither command form or with both forms."""
    document = load_workflow(path)
    for job_name, job in document["jobs"].items():
        for index, step in enumerate(job.get("steps", [])):
            label = step.get("name", f"step {index}")
            has = {key for key in ("run", "uses") if key in step}
            assert has, f"{path.name}: {job_name}: {label!r} has neither `run` nor `uses`"
            assert len(has) == 1, f"{path.name}: {job_name}: {label!r} has both `run` and `uses`"


@pytest.mark.parametrize("path", workflow_paths(), ids=lambda p: p.name)
def test_step_names_are_unique_within_a_job(path: Path) -> None:
    """Two steps with one name is the signature of a bad paste or rebase."""
    document = load_workflow(path)
    for job_name, job in document["jobs"].items():
        names = [step["name"] for step in job.get("steps", []) if "name" in step]
        duplicated = sorted({name for name in names if names.count(name) > 1})
        assert not duplicated, f"{path.name}: {job_name}: repeated step name(s) {duplicated}"


def test_statistical_oracles_are_a_visible_pull_request_lane() -> None:
    """Keep the policy triggers, an unfiltered PR lane, and stable status names."""
    document = load_workflow(STATISTICAL_ORACLES)
    events = assert_unfiltered_pull_request(document)
    required_events = {"pull_request", "schedule", "workflow_dispatch"}
    assert required_events <= set(events)

    job = document["jobs"]["slow-tier"]
    assert job["name"] == (
        "statistical and end-to-end oracles (Python ${{ matrix.python-version }})"
    )
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]


def test_dashboard_javascript_is_a_visible_pull_request_lane() -> None:
    """Pin the dashboard check's status name, runtime, and canonical command."""
    document = load_workflow(PULL_REQUEST_CI)
    events = assert_unfiltered_pull_request(document)
    assert events["push"]["branches"] == ["main"]
    job = document["jobs"]["dashboard-javascript"]
    assert job["name"] == "dashboard JavaScript behaviour (Node 22)"

    steps = job["steps"]
    setup = next(step for step in steps if step.get("name") == "Set up Node 22")
    assert setup["uses"] == "actions/setup-node@v4"
    assert setup["with"]["node-version"] == "22"

    test_step = next(step for step in steps if step.get("name") == "Dashboard JavaScript behaviour")
    assert test_step["run"] == "make node-test"


def test_statistical_oracle_lane_selects_the_declared_slow_tier() -> None:
    """Run the declared tier without the opt-in Node and cascade suites."""
    document = load_workflow(STATISTICAL_ORACLES)
    steps = document["jobs"]["slow-tier"]["steps"]
    pytest_step = next(step for step in steps if step.get("name") == "Pytest (slow tier)")
    assert pytest_step["run"] == ('uv run pytest tests/ -m "slow and not node and not cascade_oc"')
