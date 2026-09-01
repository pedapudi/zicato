"""The in-process worker seam: what it preserves, and what it refuses to fake.

`zicato.tournament.worker_transport.use_worker_launcher` lets a test harness
run a board unit through the worker's own module entry without paying for an
interpreter per unit. The seam is only safe while two things hold, and both
are pinned here.

The first is EQUIVALENCE: a unit executed inline must produce the loss
profile a subprocess would have produced, because the whole point is that it
is the same code reading the same args file and writing the same result file.

The second is HONESTY about the boundary the seam does not reproduce. A
thread has no pid and cannot be signalled, so the inline worker raises where
a real child would act. Faking either would let a test about worker
isolation pass without a worker.

The production default is asserted here too. Nothing in the runtime installs
a launcher, so the default is the subprocess spawn, and a test that leaves
one installed would be visible as a failure in this module rather than as a
mystery elsewhere.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter, deque
from dataclasses import replace
from pathlib import Path

import pytest

import zicato.tournament.worker_transport as transport
from tests._runtime_builders import make_generation
from tests._subprocess_worker_support import (
    CompletingAdapter,
    auxiliary_call_llm,
    harness_call_llm,
)
from zicato.config import pin_overrides
from zicato.core import BoardEntry, LossProfile, RuntimeConfig, ScoringWeights
from zicato.judge_runtime.error_register import (
    judge_error_snapshot,
    record_judge_error,
    record_judge_invocation,
)
from zicato.testing.worker_inline import InlineWorker, inline_workers
from zicato.tournament.runner import _run_single


def _entry() -> BoardEntry:
    return BoardEntry(
        id="entry_a",
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="hello",
    )


def _config(workspace: Path) -> RuntimeConfig:
    return RuntimeConfig(
        instance_id="test",
        workspace_root=workspace,
        harness_call_llm=harness_call_llm,
        auxiliary_call_llm=auxiliary_call_llm,
    )


def _drive_one_unit(workspace: Path) -> LossProfile:
    """One board unit through `_run_single`, whichever launcher is in force."""
    workspace.mkdir(parents=True, exist_ok=True)
    generation = make_generation(workspace)
    return asyncio.run(
        _run_single(
            adapter=CompletingAdapter(),
            generation=generation,
            entry=_entry(),
            weights=ScoringWeights(),
            config=_config(workspace),
            workspace_root=workspace,
            epoch_id="e0",
            side="parent",
        )
    )


# ---------------------------------------------------------------------------
# The production default.
# ---------------------------------------------------------------------------


def test_the_default_launcher_spawns_a_subprocess() -> None:
    """Production runs every unit in its own process; nothing changes that."""
    assert transport._launcher is transport.spawn_worker_subprocess


def test_the_seam_restores_the_previous_launcher_even_on_an_exception() -> None:
    """A failing test cannot leave the boundary swapped for the next one."""
    before = transport._launcher
    with pytest.raises(RuntimeError, match="deliberate"):
        with inline_workers():
            assert transport._launcher is not before
            raise RuntimeError("deliberate")
    assert transport._launcher is before


# ---------------------------------------------------------------------------
# Equivalence: the same unit, both ways, the same answer.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_an_inline_unit_scores_exactly_as_a_subprocess_unit_does(tmp_path: Path) -> None:
    """The seam preserves the loss profile, field for field.

    Runs the SAME adapter, generation, entry and weights twice — once
    through the real `python -m zicato._tournament_worker` subprocess and
    once through the same entry in this interpreter — and compares the two
    profiles.

    Three fields are masked, and only three: `runtime_ms`, `started_at` and
    `ended_at` are readings of the clock, and the two runs are sequential,
    so they differ whenever the pair straddles a second boundary. Every
    field that carries a SCORE or an outcome is compared. Masking wider
    than this would be how the test stops noticing a real divergence.
    """
    subprocess_profile = _drive_one_unit(tmp_path / "viasubprocess" / ".zicato")

    with inline_workers():
        inline_profile = _drive_one_unit(tmp_path / "viainline" / ".zicato")

    def comparable(profile: LossProfile) -> LossProfile:
        # run_id carries the workspace root, which differs by fixture path.
        return replace(profile, run_id="", runtime_ms=0, started_at="", ended_at="")

    comparable_inline = comparable(inline_profile)
    comparable_subprocess = comparable(subprocess_profile)
    assert comparable_inline == comparable_subprocess, (
        "the seam moved a unit's score:\n"
        f"  inline:     {comparable_inline}\n"
        f"  subprocess: {comparable_subprocess}"
    )
    assert subprocess_profile.expectation_result == inline_profile.expectation_result
    assert not inline_profile.wall_clock_budget_exceeded


@pytest.mark.integration
def test_an_inline_unit_writes_the_worker_s_own_result_file(tmp_path: Path) -> None:
    """It is the worker that runs, so the worker's artifacts are on disk.

    A seam that computed the answer some other way would leave this tree
    empty; the loss profile alone cannot tell the two apart.
    """
    workspace = tmp_path / ".zicato"
    with inline_workers():
        _drive_one_unit(workspace)
    losses = sorted(workspace.rglob("loss.json"))
    assert losses, "the inline worker wrote no loss.json, so it did not run the worker"


# ---------------------------------------------------------------------------
# What the seam refuses to fake.
# ---------------------------------------------------------------------------


async def test_an_inline_worker_has_no_pid_and_says_so() -> None:
    """A test asserting on worker pids must not silently get the parent's."""
    worker = InlineWorker(asyncio.get_running_loop().create_future())
    with pytest.raises(RuntimeError, match="no pid"):
        _ = worker.pid


@pytest.mark.parametrize("method", ["terminate", "kill"])
async def test_an_inline_worker_refuses_to_pretend_it_was_signalled(method: str) -> None:
    """A silent no-op would let a kill-escalation test pass without a kill."""
    worker = InlineWorker(asyncio.get_running_loop().create_future())
    with pytest.raises(RuntimeError, match="cannot be terminated"):
        getattr(worker, method)()


async def test_an_inline_worker_reports_no_exit_code_until_it_finishes() -> None:
    """`returncode` is the parent's completion signal and must not lie early."""
    pending: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    worker = InlineWorker(pending)
    assert worker.returncode is None
    pending.set_result(0)
    assert worker.returncode == 0


@pytest.mark.integration
def test_an_inline_unit_reports_no_judge_it_never_called(tmp_path: Path) -> None:
    """A unit's judge-error counts are its own, not the process's.

    The register is process-wide, and its own documentation rests on a
    worker process evaluating exactly one board unit. Sharing an
    interpreter breaks that assumption: before the seam cleared this
    register, a unit inherited whatever a test earlier in the same xdist
    worker had recorded and reported eight invocations and six errors for
    a judge named `j` that it had never called. The failure was
    intermittent, because it depended on which test landed first.

    The fixture below records exactly that, so this test fails against a
    seam that isolates configuration pins and role failures but not this.
    """
    record_judge_invocation("a_judge_from_an_earlier_test")
    record_judge_error("a_judge_from_an_earlier_test", RuntimeError("earlier"))

    with inline_workers():
        profile = _drive_one_unit(tmp_path / ".zicato")

    assert profile.judge_errors == (), (
        "the unit inherited judge counts from the calling process: " f"{profile.judge_errors}"
    )
    # And the caller's own register survives the unit, rather than being
    # cleared out from under it.
    assert any(
        error.judge_name == "a_judge_from_an_earlier_test" for error in judge_error_snapshot()
    )


# ---------------------------------------------------------------------------
# The leak surface, enumerated rather than remembered.
# ---------------------------------------------------------------------------


def _module_state_fingerprint() -> dict[str, str]:
    """A repr per mutable module-level value across every loaded `zicato.*` module.

    Walks the import table rather than a list somebody maintained. Only
    containers are read — a function, a class or a module is not state a
    unit mutates, and reading their attributes would recurse into the whole
    interpreter. The value is a `repr`, which is enough to detect a change
    and readable when one is reported.
    """
    fingerprint: dict[str, str] = {}
    for module_name, module in sorted(sys.modules.items()):
        if not module_name.startswith("zicato") or module is None:
            continue
        for attribute, value in sorted(vars(module).items()):
            if not isinstance(value, dict | list | set | frozenset | tuple | Counter | deque):
                continue
            try:
                fingerprint[f"{module_name}.{attribute}"] = repr(value)
            except Exception:  # noqa: BLE001 — a repr that raises is not state we can read
                fingerprint[f"{module_name}.{attribute}"] = "<unreadable>"
    return fingerprint


def _changed_names(before: dict[str, str], after: dict[str, str]) -> set[str]:
    """Module-level names whose value differs, appeared, or disappeared."""
    return {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}


#: The module-level names a unit is ALLOWED to leave changed, because the
#: seam restores them itself. Anything else appearing in the diff is a leak
#: the seam does not know about.
RESTORED_BY_THE_SEAM = {
    "zicato.config._PINNED_OVERRIDES",
    "zicato.judge_runtime.error_register._JUDGE_CALLS",
    "zicato.models_config._DEFERRED_ROLE_FAILURES",
    "zicato.util.best_effort._FAILURES",
}


def _plant_pins() -> None:
    pin_overrides({"aux": {"call_timeout_s": 11.5}})


def _plant_judge_calls() -> None:
    record_judge_invocation("a_judge_from_an_earlier_test")
    record_judge_error("a_judge_from_an_earlier_test", RuntimeError("earlier"))


def _plant_role_failures() -> None:
    from zicato.models_config import _DEFERRED_ROLE_FAILURES

    _DEFERRED_ROLE_FAILURES["judge"] = "an earlier resolution failure"


def _plant_best_effort() -> None:
    from zicato.util.best_effort import _FAILURES

    _FAILURES["an_earlier_degraded_write"] += 1


#: One planter per declared register, so each can be checked on its own.
#: A unit only WRITES these when it has something to record — a minimal
#: unit records nothing — so what the seam has to get right is putting a
#: CALLER's state back untouched, and that is what these plant.
_PLANTS = {
    "zicato.config._PINNED_OVERRIDES": _plant_pins,
    "zicato.judge_runtime.error_register._JUDGE_CALLS": _plant_judge_calls,
    "zicato.models_config._DEFERRED_ROLE_FAILURES": _plant_role_failures,
    "zicato.util.best_effort._FAILURES": _plant_best_effort,
}


@pytest.mark.integration
def test_an_inline_unit_leaves_no_module_state_behind(tmp_path: Path) -> None:
    """The seam's leak surface is enumerated and closed.

    Snapshots every mutable module-level value in every loaded `zicato.*`
    module, runs one unit through the seam, and snapshots again. After the
    seam's restore the two must be IDENTICAL: a unit that mutates a
    module-level name the seam does not restore fails here BY NAME, which is
    how the judge-error register should have been found rather than by an
    intermittent failure somewhere else.
    """
    # Import the whole surface a unit touches first, so the comparison is
    # not confused by a module appearing between the two snapshots.
    _drive_one_unit(tmp_path / "warm" / ".zicato")
    # Plant every declared register, so an empty diff means the seam put
    # four things back rather than that nothing ever moved.
    for plant in _PLANTS.values():
        plant()

    before = _module_state_fingerprint()
    with inline_workers():
        _drive_one_unit(tmp_path / "measured" / ".zicato")
    after = _module_state_fingerprint()

    leaked = _changed_names(before, after)
    assert not leaked, (
        "a unit left module-level state behind that the seam does not restore. "
        "Add each name to _isolated_worker_globals (snapshot, clear, restore) "
        f"and to RESTORED_BY_THE_SEAM: {sorted(leaked)}"
    )


@pytest.mark.integration
def test_the_declared_restore_set_covers_every_register_a_unit_can_touch(
    tmp_path: Path,
) -> None:
    """Each declared register really is restored, one at a time.

    The test above plants all four together and asks for an empty diff.
    This one plants each on its own, so a restore that covers three of the
    four fails naming the one it missed rather than passing because a
    sibling happened to be empty.
    """
    _drive_one_unit(tmp_path / "warm" / ".zicato")

    for name, plant in _PLANTS.items():
        plant()
        before = _module_state_fingerprint()
        with inline_workers():
            _drive_one_unit(tmp_path / name.rsplit(".", 1)[-1] / ".zicato")
        leaked = _changed_names(before, _module_state_fingerprint())
        assert not leaked, f"planting {name} alone leaked: {sorted(leaked)}"
