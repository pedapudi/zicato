"""Real imports and worker processes obey declared driver and candidate locations."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from zicato.core.adapter_config import DriverImportContext
from zicato.core.loss import validate_loss_identity
from zicato.core.measurement import MeasurementDraw, measurement_artifact_path
from zicato.core.workspace import run_dir, run_id_for_unit
from zicato.driver_imports import driver_import_scope, imported_sources
from zicato.telemetry.reducer import read_loss_profile
from zicato.tournament.unit_cache import read_run_result


def test_overlapping_async_calls_keep_imports_until_both_exit(tmp_path: Path) -> None:
    _package(tmp_path, "candidate")
    context = DriverImportContext((tmp_path,), ("candidate_target",))
    original_path = sys.path[:]

    async def overlap() -> None:
        first_entered, second_entered, first_exited = (asyncio.Event() for _ in range(3))

        async def first() -> None:
            with driver_import_scope(context):
                first_entered.set()
                await second_entered.wait()
            first_exited.set()

        async def second() -> None:
            await first_entered.wait()
            with driver_import_scope(context):
                second_entered.set()
                await first_exited.wait()
                assert importlib.import_module("candidate_target").VALUE == "candidate"

        await asyncio.wait_for(asyncio.gather(first(), second()), timeout=2)

    asyncio.run(overlap())
    assert sys.path == original_path
    assert "candidate_target" not in sys.modules


def _package(root: Path, value: str) -> None:
    package = root / "candidate_target"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")


def _driver(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fixed_driver.py").write_text(
        """from candidate_target import VALUE
from zicato.core import RunResult

async def target(system, user, model):
    return "target"

async def evaluation(system, user, model):
    return "evaluation"

class Session:
    def tree_import_status(self):
        return {"candidate_target": "verified"}

    async def run(self, entry, sinks, config):
        context = config.run_context
        assert context.generation_id == entry.context["generation_id"]
        assert context.scratch_dir.is_dir()
        (context.scratch_dir / "context.txt").write_text(context.run_id)
        return RunResult(run_id=context.run_id, entry_id=entry.id,
                         final_output=VALUE, transcript=(VALUE,), runtime_ms=1)

class Adapter:
    name = "fixed-driver"
    def load(self, root):
        return Session()
    def mutable_subpaths(self, root):
        return [root / "candidate_target"]
    def mutation_points(self, root):
        return []
    def worker_spec(self):
        return {"kind": "import", "factory": "fixed_driver:make_adapter"}

def make_adapter(*, label="default"):
    return Adapter()
""",
        encoding="utf-8",
    )


def test_snapshot_replaces_preloaded_driver_and_target_then_restores_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, snapshot = tmp_path / "project", tmp_path / "snapshot"
    _package(project, "live decoy")
    _package(snapshot, "candidate")
    _driver(project)
    monkeypatch.syspath_prepend(str(project))
    parent_driver = importlib.import_module("fixed_driver")
    parent_target = importlib.import_module("candidate_target")
    path_before = sys.path[:]
    context = DriverImportContext((project,), ("candidate_target",))
    try:
        with pytest.raises(RuntimeError, match="exit scope"):
            with driver_import_scope(context, snapshot_root=snapshot):
                assert importlib.import_module("fixed_driver").VALUE == "candidate"
                assert imported_sources(context, snapshot) == {
                    "candidate_target": str(snapshot / "candidate_target" / "__init__.py"),
                }
                raise RuntimeError("exit scope")
        assert sys.path == path_before
        assert sys.modules["fixed_driver"] is parent_driver
        assert sys.modules["candidate_target"] is parent_target
        assert parent_driver.VALUE == "live decoy"
    finally:
        sys.modules.pop("fixed_driver", None)
        sys.modules.pop("candidate_target", None)


def test_missing_candidate_cannot_use_the_live_target(tmp_path: Path) -> None:
    project, snapshot = tmp_path / "project", tmp_path / "snapshot"
    _package(project, "live decoy")
    _driver(project)
    snapshot.mkdir()
    context = DriverImportContext((project,), ("candidate_target",))
    with driver_import_scope(context, snapshot_root=snapshot):
        importlib.import_module("fixed_driver")
        with pytest.raises(RuntimeError, match="candidate module .* expected source under"):
            imported_sources(context, snapshot)


@pytest.mark.parametrize("context_format", ["legacy", "record"])
def test_two_real_workers_import_their_own_snapshots_with_preloaded_decoys(
    tmp_path: Path, context_format: str
) -> None:
    project, workspace = tmp_path / "driver project with spaces", tmp_path / ".zicato"
    _package(project, "live decoy")
    _driver(project)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    for generation, value in (("v0", "parent candidate"), ("v1", "child candidate")):
        snapshot = tmp_path / generation
        _package(snapshot, value)
        measurement = MeasurementDraw.from_index(0, base_seed=None)
        run_id = run_id_for_unit(
            generation, "entry", measurement.replicate_index, base_seed=measurement.base_seed
        )
        unit = measurement_artifact_path(
            run_dir(workspace, "e0", generation, "entry"),
            "loss",
            measurement.replicate_index,
            base_seed=measurement.base_seed,
        ).parent
        unit.mkdir(parents=True)
        payload = {
            "workspace_root": str(workspace),
            "epoch_id": "e0",
            "generation_id": generation,
            "run_id": run_id,
            "snapshot_root": str(snapshot),
            "scratch_dir": str(unit / "scratch"),
            "driver_imports": DriverImportContext((project,), ("candidate_target",)).document(),
            "adapter": {"kind": "import", "factory": "fixed_driver:make_adapter"},
            "entry": {
                "id": "entry",
                "kind": "single_turn",
                "input": "example",
                "wall_clock_budget_seconds": 20,
                "context": {"generation_id": generation},
            },
            "target_role": {"dotted": "fixed_driver:target"},
            "evaluation_role": {"dotted": "fixed_driver:evaluation"},
            "sink_events_path": str(unit / "events.jsonl"),
            "loss_path": str(unit / "loss.json"),
            "measurement": measurement.to_json(),
            "result_path": str(unit / "worker.result.json"),
            "harmonograf_url": "",
        }
        if context_format == "record":
            from zicato.core.run_context import RunContext
            from zicato.core.runtime_context import WorkerRuntimeContext

            payload["runtime_context"] = WorkerRuntimeContext(
                run=RunContext(workspace, "e0", generation, run_id, snapshot, unit / "scratch")
            ).to_json()
        args = unit / "args.json"
        args.write_text(json.dumps(payload), encoding="utf-8")
        bootstrap = (
            "import sys; sys.path.insert(0, sys.argv.pop(1)); "
            "sys.path.insert(0, sys.argv.pop(1)); "
            "import fixed_driver, candidate_target; "
            "assert fixed_driver.VALUE == 'live decoy'; "
            "from zicato._tournament_worker import main; raise SystemExit(main())"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                bootstrap,
                str(Path(__file__).resolve().parents[1] / "src"),
                str(project),
                str(args),
            ],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        measured = json.loads((unit / "worker.result.json").read_text())
        profile = read_loss_profile(unit / "loss.json")
        validate_loss_identity(
            profile,
            epoch_id="e0",
            generation_id=generation,
            entry_id="entry",
            measurement=measurement,
        )
        assert profile.measurement == measurement
        capture = read_run_result(unit / "result.json", expected=profile)
        assert capture is not None
        assert capture["final_output"] == value
        assert measured["run_result"]["final_output"] == value
        provenance = json.loads(
            (
                workspace / "epochs" / "e0" / "generations" / generation / "harness_load.json"
            ).read_text()
        )["implementation"]
        assert provenance["factory_file"] == str(project / "fixed_driver.py")
        assert provenance["factory_source_sha256"]
        assert provenance["candidate_modules"] == {
            "candidate_target": "candidate_target/__init__.py"
        }


def test_scoped_import_reads_changed_source_with_preserved_size_and_timestamp(
    tmp_path: Path,
) -> None:
    _package(tmp_path, "parent")
    source = tmp_path / "candidate_target" / "__init__.py"
    source.write_text(source.read_text() + "def read():\n    return VALUE\n")
    from zicato.scoring.plugins import resolve_plugin_source

    context = DriverImportContext((tmp_path,), ("candidate_target",))
    with driver_import_scope(context):
        assert importlib.import_module("candidate_target").VALUE == "parent"
        before_hash = resolve_plugin_source("candidate_target:read")
    original = source.stat()
    content = source.read_text()
    assert "parent" in content
    source.write_text(content.replace("parent", "child!"))
    assert source.stat().st_size == original.st_size
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    with driver_import_scope(context):
        assert importlib.import_module("candidate_target").VALUE == "child!"
        assert resolve_plugin_source("candidate_target:read") != before_hash


def test_example_setup_in_fresh_process_without_pythonpath(tmp_path: Path) -> None:
    project = tmp_path / "project with spaces"
    project.mkdir()
    workspace = project / ".zicato"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    import sysconfig
    import venv

    # Child probes must import the same checkout as the command even when a
    # shared development interpreter has another checkout installed editable.
    environment = tmp_path / "interpreter"
    venv.EnvBuilder(with_pip=False).create(environment)
    packages = Path(sysconfig.get_path("purelib", vars={"base": str(environment)}))
    source = Path(__file__).resolve().parents[1] / "src"
    (packages / "checkout.pth").write_text(f"{source}\n{sysconfig.get_path('purelib')}\n")
    executable = environment / "bin" / "python"
    for arguments in (
        ["init", "--example", "--workspace", str(workspace)],
        ["inspect", "setup", "--workspace", str(workspace)],
        ["evolve", "--dry-run", "--workspace", str(workspace)],
    ):
        result = subprocess.run(
            [str(executable), "-c", "from zicato.cli import main; main()", *arguments],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    assert not list((workspace / "epochs").glob("*/config.json"))
