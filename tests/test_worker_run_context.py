"""Run coordinates are accepted before worker imports and filesystem writes."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from zicato.core.run_context import RunContext
from zicato.core.runtime_context import WorkerRuntimeContext


@pytest.mark.parametrize("changed", ["epoch_id", "snapshot_root", "scratch_dir", "absent"])
def test_conflicting_run_record_fails_before_driver_import_or_run_writes(
    tmp_path: Path, changed: str
) -> None:
    workspace, snapshot, scratch = (
        tmp_path / name for name in ("workspace", "snapshot", "scratch")
    )
    driver = tmp_path / "driver.py"
    marker = tmp_path / "imported"
    driver.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    context = WorkerRuntimeContext(
        run=RunContext(workspace, "e0", "v0", "run", snapshot, scratch)
    ).to_json()
    if changed == "absent":
        context["run"] = None
    else:
        context["run"][changed] = "e1" if changed == "epoch_id" else str(tmp_path / "other")
    payload = {
        "workspace_root": str(workspace),
        "epoch_id": "e0",
        "generation_id": "v0",
        "run_id": "run",
        "snapshot_root": str(snapshot),
        "scratch_dir": str(scratch),
        "runtime_context": context,
        "driver_imports": {"roots": [str(tmp_path)], "mutable_packages": []},
        "adapter": {"kind": "import", "factory": "driver:make"},
    }
    script = (
        "import sys, json, asyncio; sys.path.insert(0, sys.argv[1]); "
        "from zicato._tournament_worker import _run; "
        "asyncio.run(_run(json.loads(sys.argv[2])))"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(Path(__file__).resolve().parents[1] / "src"),
            json.dumps(payload),
        ],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    expected = (
        "must include its run coordinates"
        if changed == "absent"
        else "disagrees with worker coordinates"
    )
    assert expected in result.stderr
    assert not marker.exists()
    assert not workspace.exists()
    assert not scratch.exists()
