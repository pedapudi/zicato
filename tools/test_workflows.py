"""The GitHub workflow files are structurally valid, checked locally.

A workflow file is a gate definition that nothing else validates before it
reaches GitHub, and GitHub's rejection is nearly silent: the run concludes
`failure` with ZERO jobs and the message "This run likely failed because of
a workflow file issue". Nothing runs, and a reader skimming the checks sees
a red mark rather than an explanation.

Parsing the file is not enough to catch this, which is why the assertions
below are about what GitHub requires rather than what YAML permits. Two
constructions a rebase or a bad paste produces are valid YAML, and
`yaml.safe_load` returns something that looks correct for both:

* a step mapping carrying two `run:` keys. A mapping may repeat a key and
  the last one wins, so the loaded document holds one plausible command
  and a job-name assertion passes over it.
* a `- name:` line on a list item of its own, leaving a step with a name
  and NO `run` or `uses`. That is a valid mapping; it is simply not a
  runnable step, and a duplicate-key check cannot see it because the two
  names sit in different mappings.

Hence three rules: no repeated key anywhere, a unique name per step within
a job, and one of `run` or `uses` on every step and no more.

Lives beside the tool tests because it needs no fixtures and runs in
milliseconds; CI collects it on the same explicit path argument.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


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


def test_there_are_workflows_to_check() -> None:
    """Guard the guard: an empty glob would make every test below vacuous."""
    assert workflow_paths(), f"no workflow files under {WORKFLOWS}"


@pytest.mark.parametrize("path", workflow_paths(), ids=lambda p: p.name)
def test_no_mapping_declares_a_key_twice(path: Path) -> None:
    """The defect PyYAML hides: two `run:` keys on one step, last one wins."""
    yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)


@pytest.mark.parametrize("path", workflow_paths(), ids=lambda p: p.name)
def test_every_step_has_exactly_one_of_run_or_uses(path: Path) -> None:
    """A step with neither is not runnable, and GitHub rejects the file.

    This is the shape a duplicated `- name:` line leaves behind, and it is
    invisible to both a parse and a duplicate-key check.
    """
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    for job_name, job in document["jobs"].items():
        for index, step in enumerate(job.get("steps", [])):
            label = step.get("name", f"step {index}")
            has = {key for key in ("run", "uses") if key in step}
            assert has, f"{path.name}: {job_name}: {label!r} has neither `run` nor `uses`"
            assert len(has) == 1, f"{path.name}: {job_name}: {label!r} has both `run` and `uses`"


@pytest.mark.parametrize("path", workflow_paths(), ids=lambda p: p.name)
def test_step_names_are_unique_within_a_job(path: Path) -> None:
    """Two steps with one name is the signature of a bad paste or rebase."""
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    for job_name, job in document["jobs"].items():
        names = [step["name"] for step in job.get("steps", []) if "name" in step]
        duplicated = sorted({name for name in names if names.count(name) > 1})
        assert not duplicated, f"{path.name}: {job_name}: repeated step name(s) {duplicated}"
