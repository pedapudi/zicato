"""Flag→config threading for the env-var-rationalization CLI flags.

Five operator knobs that used to be ``ZICATO_*`` environment variables
are now ``zicato evolve`` flags (``--parallelism``, ``--aux-call-timeout``,
``--supervisor-binary``, ``--harmonograf-url``) plus ``--static-dir`` on
``zicato dashboard`` / ``zicato dashboard --view builder`` (covered in their own test
files). The evolve flags land on the typed config tree via
:func:`zicato.config.pin_overrides`; these tests prove:

* each flag reaches the knob it shadows, through a bare deep-call-site
  ``load_config()`` — the exact form the consumers use;
* an unset flag pins nothing (the workspace ``config.json`` and the
  dataclass defaults stay in charge);
* the tournament runner threads the pins into the worker args file, and
  a real worker subprocess honours them (the cross-process leg of
  ``--aux-call-timeout``);
* the harmonograf split: the FLAG is the operator surface, while the
  ``ZICATO_HARMONOGRAF_URL`` env var survives strictly as the internal
  auto-launch handoff channel — read after the flag, before the
  workspace config.

The suite-wide autouse fixture clears pins between tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from tests._cli_support import install_evolve_capture
from tests._runtime_builders import make_generation
from zicato.config import get_pinned_overrides, load_config, pin_overrides
from zicato.core import BoardEntry, LossProfile, RuntimeConfig, ScoringWeights

# ---------------------------------------------------------------------------
# Stub LLMs for evolve invocations (importable module-level objects)
# ---------------------------------------------------------------------------


async def _target_call_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


async def _aux_call_llm(system: str, user: str, model: str) -> str:
    del system, user, model
    return ""


def _invoke_evolve(
    monkeypatch: pytest.MonkeyPatch,
    *flags: str,
) -> None:
    """Invoke ``zicato evolve`` with stubbed loop + extra ``flags``."""
    from zicato.cli.commands.evolve import evolve_cmd

    captured: dict[str, Any] = {}
    install_evolve_capture(monkeypatch, captured)
    runner = CliRunner()
    result = runner.invoke(
        evolve_cmd,
        [
            *flags,
        ],
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Per-flag threading — flag value visible to a bare load_config()
# ---------------------------------------------------------------------------


def test_parallelism_flag_pins_runtime_parallelism(
    monkeypatch: pytest.MonkeyPatch, mock_dashboard_spawn: list[Any]
) -> None:
    del mock_dashboard_spawn
    _invoke_evolve(monkeypatch, "--parallelism", "11")
    assert load_config().runtime.parallelism == 11


def test_aux_call_timeout_flag_reaches_deep_call_site(
    monkeypatch: pytest.MonkeyPatch, mock_dashboard_spawn: list[Any]
) -> None:
    del mock_dashboard_spawn
    from zicato.aux_timeout import aux_call_timeout_s

    _invoke_evolve(monkeypatch, "--aux-call-timeout", "7.25")
    # The bare call-site form every aux consumer uses.
    assert aux_call_timeout_s() == 7.25


def test_supervisor_binary_flag_pins_integration_knob(
    monkeypatch: pytest.MonkeyPatch, mock_dashboard_spawn: list[Any], tmp_path: Path
) -> None:
    del mock_dashboard_spawn
    sentinel = tmp_path / "sentinel-supervisor"
    _invoke_evolve(monkeypatch, "--supervisor-binary", str(sentinel))
    assert load_config().integration.supervisor_binary == str(sentinel)


def test_harmonograf_url_flag_reaches_the_resolver(
    monkeypatch: pytest.MonkeyPatch, mock_dashboard_spawn: list[Any]
) -> None:
    del mock_dashboard_spawn
    from zicato.telemetry.sink import resolve_harmonograf_url

    _invoke_evolve(monkeypatch, "--harmonograf-url", "http://shared.example:9000")
    assert resolve_harmonograf_url() == "http://shared.example:9000"


def test_no_flags_pin_nothing(
    monkeypatch: pytest.MonkeyPatch, mock_dashboard_spawn: list[Any]
) -> None:
    """A flagless evolve leaves the pin layer empty — defaults / config.json rule."""
    del mock_dashboard_spawn
    _invoke_evolve(monkeypatch)
    assert get_pinned_overrides() == {}


def test_aux_call_timeout_flag_rejects_non_positive(
    monkeypatch: pytest.MonkeyPatch, mock_dashboard_spawn: list[Any]
) -> None:
    """A zero budget would short-circuit every call; the flag rejects it."""
    del mock_dashboard_spawn
    from zicato.cli.commands.evolve import evolve_cmd

    captured: dict[str, Any] = {}
    install_evolve_capture(monkeypatch, captured)
    runner = CliRunner()
    result = runner.invoke(
        evolve_cmd,
        [
            "--aux-call-timeout",
            "0",
        ],
    )
    assert result.exit_code != 0
    assert "aux-call-timeout" in result.output


# ---------------------------------------------------------------------------
# The harmonograf operator/internal split
# ---------------------------------------------------------------------------


def test_harmonograf_internal_handoff_still_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env var survives as the INTERNAL auto-launch handoff channel.

    ``_resolve_or_launch_harmonograf`` writes the launched server's URL
    into ``ZICATO_HARMONOGRAF_URL`` so downstream re-resolvers (workers
    included) discover it; the resolver must still read it even though
    it is no longer a ``load_config`` binding.
    """
    from zicato.telemetry.sink import resolve_harmonograf_url

    monkeypatch.setenv("ZICATO_HARMONOGRAF_URL", "http://auto-launched.local:7999")
    assert resolve_harmonograf_url() == "http://auto-launched.local:7999"


def test_harmonograf_flag_beats_internal_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit operator URL (the flag's pin) outranks an inherited handoff."""
    from zicato.telemetry.sink import resolve_harmonograf_url

    monkeypatch.setenv("ZICATO_HARMONOGRAF_URL", "http://outer-invocation.local:7999")
    pin_overrides({"integration": {"harmonograf_url": "http://operator.example:9000"}})
    assert resolve_harmonograf_url() == "http://operator.example:9000"


def test_harmonograf_handoff_beats_workspace_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-flight handoff outranks the workspace config.json key.

    Mid-evolve, the auto-launched URL is THE console for this
    invocation; a stale config.json value must not shadow it.
    """
    from zicato.telemetry.sink import resolve_harmonograf_url

    monkeypatch.setenv("ZICATO_HARMONOGRAF_URL", "http://auto-launched.local:7999")
    resolved = resolve_harmonograf_url({"harmonograf_url": "http://stale.example:1"})
    assert resolved == "http://auto-launched.local:7999"


# ---------------------------------------------------------------------------
# Cross-process threading — orchestrator pins → worker args → worker config
# ---------------------------------------------------------------------------


def _entry(entry_id: str = "entry_a", budget_s: int = 60) -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=budget_s,
        input="hello",
    )


def _runtime_config(workspace: Path) -> RuntimeConfig:
    from tests._subprocess_worker_support import evaluation_call_llm, target_call_llm

    return RuntimeConfig(
        instance_id="test",
        workspace_root=workspace,
        target_call_llm=target_call_llm,
        evaluation_call_llm=evaluation_call_llm,
    )


@pytest.mark.integration
def test_runner_threads_pins_into_worker_args_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_run_single`` writes the current pins into the worker args file."""
    from tests._subprocess_worker_support import StubAdapter
    from zicato.tournament.runner import _run_single

    pin_overrides(
        {
            "aux": {"call_timeout_s": 7.5},
        }
    )

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry()

    captured_args: dict[str, Any] = {}
    real_create = asyncio.create_subprocess_exec

    async def _spy_create(*args: object, **kwargs: object) -> object:
        # argv: python -m zicato._tournament_worker <args_path>. Read the
        # args file NOW, before the worker consumes / the runner cleans it.
        args_path = Path(str(args[-1]))
        captured_args.update(json.loads(args_path.read_text(encoding="utf-8")))
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_create)

    loss = asyncio.run(
        _run_single(
            adapter=StubAdapter(),
            generation=generation,
            entry=entry,
            weights=ScoringWeights(),
            config=_runtime_config(workspace),
            workspace_root=workspace,
            epoch_id="e0",
            side="parent",
        )
    )

    assert isinstance(loss, LossProfile)
    assert captured_args["config_pins"] == {"aux": {"call_timeout_s": 7.5}}


@pytest.mark.integration
def test_worker_honours_config_pins_from_args_file(tmp_path: Path) -> None:
    """A real worker subprocess re-pins the args-file pins before running.

    The probe adapter records the WORKER-side ``load_config()`` view of
    the evaluation-call budget consumed inside the worker; it must reflect
    the orchestrator's flag pin, with no environment variable involved.
    """
    import os
    import subprocess
    import sys

    from zicato.core.workspace import events_jsonl_path, loss_profile_path, run_id_for_unit

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    generation = make_generation(workspace)
    entry = _entry()

    sink_path = events_jsonl_path(workspace, "e0", generation.id, entry.id)
    loss_path = loss_profile_path(workspace, "e0", generation.id, entry.id)
    result_path = tmp_path / "result.json"
    args_path = tmp_path / "args.json"
    args_path.write_text(
        json.dumps(
            {
                "workspace_root": str(workspace),
                "epoch_id": "e0",
                "generation_id": generation.id,
                "snapshot_root": str(generation.snapshot_root),
                "entry": {
                    "id": entry.id,
                    "kind": entry.kind,
                    "wall_clock_budget_seconds": entry.wall_clock_budget_seconds,
                    "input": entry.input,
                },
                "adapter": {
                    "kind": "import",
                    "factory": "tests._subprocess_worker_support:make_config_probe_adapter",
                },
                "target_role": {"dotted": "tests._subprocess_worker_support:target_call_llm"},
                "evaluation_role": {
                    "dotted": "tests._subprocess_worker_support:evaluation_call_llm"
                },
                "run_id": run_id_for_unit(generation.id, entry.id),
                "sink_events_path": str(sink_path),
                "loss_path": str(loss_path),
                "result_path": str(result_path),
                "instance_id": "test",
                "seed": None,
                "harmonograf_url": "",
                "weights": {},
                "config_pins": {
                    "aux": {"call_timeout_s": 7.5},
                },
            }
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root if not existing else f"{root}{os.pathsep}{existing}"
    proc = subprocess.run(
        [sys.executable, "-m", "zicato._tournament_worker", str(args_path)],
        capture_output=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()

    probe = json.loads((sink_path.parent / "config_probe.json").read_text(encoding="utf-8"))
    assert probe["aux_call_timeout_s"] == 7.5
