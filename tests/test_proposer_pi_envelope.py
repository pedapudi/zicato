"""The proposer's capability envelope (issue #147 phase 6).

The visibility envelope is enforced by what the proposer is *shown*. A
default pi session has ``bash``, ``read`` and ``grep`` pointed at the
working directory; a proposer with those can read the board and the
holdout slice, and nothing errors and nothing warns — you get a champion
that has been quietly overfitting while the tournament reports it winning.
So the envelope is asserted the way contract-hash stability is asserted,
at two depths:

* **hermetic** (always) — the command actually issued to launch a
  proposer, recorded by the stub peer: every negative flag present, the
  model threaded from the resolved config, our extension and no other, the
  agent dir isolated, the working directory outside every snapshot;
* **live** (whenever ``integrations/pi`` is installed; the opt-in ``pi``
  marker lane) — real pi, asked what tools it actually has. That is the
  only observation that cannot be fooled by a flag we spelled wrong.

Plus the drift guard on the tool contract itself: the terminating tool's
schema is the proposer's causal surface, and it lives in TypeScript while
its authority lives in Python.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_proposer_pi_transport import (  # noqa: PLC2701 - shared stub harness
    STUB,
    _context,
    _experiment_args,
    _launches,
    _script,
)
from zicato.core.types import ProposerSpec
from zicato.proposer.external import ExternalProposerConfig, resolve_external_spec
from zicato.proposer.pi_agent import (
    SANCTIONED_EXTENSIONS,
    SANCTIONED_FLAGS,
    SANCTIONED_TOOLS,
    PiProposerAgent,
    build_pi_argv,
    resolve_pi_bin,
    resolve_pi_version,
)
from zicato.proposer.structured import EXPERIMENT_JSON_SCHEMA

INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integrations" / "pi"

#: Flags that would widen the tool surface. None may appear in a launch.
FORBIDDEN_FLAGS = ("--tools", "-t", "--skill", "--approve", "-a")


@pytest.fixture
def launch(tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    """Propose once through the stub peer; return the recorded launch."""
    import asyncio

    workspace = tmp_path / "ws"
    workspace.mkdir()
    records = tmp_path / "records"
    monkeypatch.setenv("ZICATO_PI_STUB_RECORD", str(records))
    monkeypatch.setenv("ZICATO_PI_STUB_SCRIPT", str(tmp_path / "script.json"))
    _script(tmp_path, [{"emit": _experiment_args()}])

    agent = PiProposerAgent(
        spec=ProposerSpec.default(),
        config=ExternalProposerConfig(
            dotted_path="zicato.proposer.pi_agent:PiProposerAgent",
            workspace_root=workspace,
            options={"pi_bin": str(STUB)},
        ),
    )
    asyncio.run(agent.propose(_context(workspace)))
    return _launches(records)[0]


# -- the launch envelope -----------------------------------------------------


def test_every_sanctioned_flag_is_on_the_command_line(launch: dict[str, Any]) -> None:
    argv = launch["argv"]
    for flag in SANCTIONED_FLAGS:
        assert flag in argv, f"{flag} missing: the envelope is only as tight as its flags"


def test_nothing_widens_the_tool_surface(launch: dict[str, Any]) -> None:
    argv = launch["argv"]
    for flag in FORBIDDEN_FLAGS:
        assert flag not in argv
    # Exactly our extensions, loaded by explicit path. Discovery is off, so
    # this list IS the extension set.
    loaded = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--extension"]
    assert [Path(p).name for p in loaded] == list(SANCTIONED_EXTENSIONS)


def test_the_model_is_threaded_from_the_resolved_config(launch: dict[str, Any]) -> None:
    """The collusion guard: pi is told the model, it never picks one."""
    argv = launch["argv"]
    assert argv[argv.index("--model") + 1] == "stub/model-1"


def test_the_environment_isolates_pis_own_state(launch: dict[str, Any]) -> None:
    env = launch["env"]
    agent_dir = Path(env["PI_CODING_AGENT_DIR"])
    assert env["PI_OFFLINE"] == "1"
    # Sessions and packages resolve INSIDE the per-challenger dir, so no
    # installed package, memory extension or saved trust decision from the
    # operator's own agent dir is reachable.
    assert Path(env["PI_CODING_AGENT_SESSION_DIR"]).is_relative_to(agent_dir)
    assert Path(env["PI_PACKAGE_DIR"]).is_relative_to(agent_dir)


def test_the_working_directory_is_outside_every_snapshot(launch: dict[str, Any]) -> None:
    """cwd is where pi discovers ``AGENTS.md`` and project-local ``.pi/``.

    Pointing it at a generation snapshot would make the system under test
    an unhashed contract input AND an injection path into its own rewriter.
    """
    cwd = Path(launch["cwd"])
    assert cwd.is_relative_to(Path(launch["env"]["PI_CODING_AGENT_DIR"]))
    assert "epochs" not in cwd.parts
    # Recorded by the peer at startup: there was nothing there to discover.
    assert launch["cwd_entries"] == []


# -- the sanctioned set has one source ---------------------------------------


def test_the_spec_declares_the_sanctioned_tools() -> None:
    """What the launch offers, what the contract hashes, one list."""
    config = ExternalProposerConfig(dotted_path="zicato.proposer.pi_agent:PiProposerAgent")
    spec = resolve_external_spec(config)

    assert spec.agent_id == "external:pi"
    assert spec.tools == SANCTIONED_TOOLS
    assert PiProposerAgent.contract_identity(config)["tools"] == list(SANCTIONED_TOOLS)


def test_the_identity_pins_the_flag_set_and_the_extension_bytes() -> None:
    """Widening the envelope rolls the epoch; it cannot happen quietly."""
    config = ExternalProposerConfig(dotted_path="zicato.proposer.pi_agent:PiProposerAgent")
    identity = PiProposerAgent.contract_identity(config)

    assert identity["flags"] == list(SANCTIONED_FLAGS)
    extensions = identity["extensions"]
    assert set(extensions) == set(SANCTIONED_EXTENSIONS)
    assert all(len(digest) == 64 for digest in extensions.values())


def test_the_pinned_version_is_the_backstop() -> None:
    """Resolved from the install when present, from the pin otherwise."""
    config = ExternalProposerConfig(dotted_path="zicato.proposer.pi_agent:PiProposerAgent")
    version = resolve_pi_version(config)
    pinned = json.loads((INTEGRATION_DIR / "package.json").read_text(encoding="utf-8"))

    assert version == pinned["dependencies"]["@earendil-works/pi-coding-agent"]


# -- the tool-registration seam ----------------------------------------------


def test_a_tool_server_reaches_the_launch_without_editing_the_transport(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The seam issue #147 phases 3-5 plug into: flags plus per-LAUNCH env.

    The env half is the one that is easy to get wrong. Best-of-N runs N
    challengers concurrently in ONE process, so a tool server's round
    context cannot travel through the shared process environment — the
    last slot to write it would win for all of them. Here two concurrent
    proposals must reach their subprocesses with DIFFERENT context paths.
    """
    import asyncio

    class WithToolServer(PiProposerAgent):
        def tool_flags(self) -> tuple[str, ...]:
            return ("--extension", "/opt/mcp-adapter.ts")

        def tool_env(self, ctx: Any, agent_dir: Path) -> dict[str, str]:
            # A per-challenger context file, inside the dir that is already
            # unique per invocation and removed when the call ends.
            return {"ZICATO_PROPOSER_TOOL_CONTEXT": str(agent_dir / "round.json")}

    workspace = tmp_path / "ws"
    workspace.mkdir()
    records = tmp_path / "records"
    monkeypatch.setenv("ZICATO_PI_STUB_RECORD", str(records))
    monkeypatch.setenv("ZICATO_PI_STUB_SCRIPT", str(tmp_path / "script.json"))
    _script(tmp_path, [{"emit": _experiment_args()}] * 3)

    agent = WithToolServer(
        spec=ProposerSpec.default(),
        config=ExternalProposerConfig(
            dotted_path="zicato.proposer.pi_agent:PiProposerAgent",
            workspace_root=workspace,
            options={"pi_bin": str(STUB)},
        ),
    )

    async def _both() -> None:
        await asyncio.gather(agent.propose(_context(workspace)), agent.propose(_context(workspace)))

    asyncio.run(_both())

    launches = _launches(records)
    assert len(launches) == 2
    for launch in launches:
        assert launch["argv"][-2:] == ["--extension", "/opt/mcp-adapter.ts"]
        # ...and the envelope still wins over anything the server asked for.
        assert launch["env"]["PI_OFFLINE"] == "1"
    contexts = {launch["env"]["ZICATO_PROPOSER_TOOL_CONTEXT"] for launch in launches}
    assert len(contexts) == 2, "concurrent challengers shared one tool context"


def test_a_tool_server_cannot_relax_the_envelope(tmp_path: Path) -> None:
    """The pi-state variables are applied last, on purpose."""
    from zicato.proposer.pi_agent import build_pi_env

    env = build_pi_env(tmp_path, {"PI_OFFLINE": "0", "PI_CODING_AGENT_DIR": "/home/operator/.pi"})

    assert env["PI_OFFLINE"] == "1"
    assert env["PI_CODING_AGENT_DIR"] == str(tmp_path)


# -- the tool contract must not drift from its Python authority --------------


def test_the_typebox_schema_mirrors_structured_py() -> None:
    """``structured.py``'s cross-check is authoritative; the .ts must agree.

    A field added to the Python schema and forgotten in TypeScript is a
    field the model is never told about — invisible until a proposal is
    silently poorer.
    """
    source = (INTEGRATION_DIR / "propose-experiment.ts").read_text(encoding="utf-8")

    for name in _schema_property_names(EXPERIMENT_JSON_SCHEMA):
        assert name in source, f"{name!r} is in EXPERIMENT_JSON_SCHEMA but not in the tool schema"
    for member in ("decrease_or_neutral", "increase_or_neutral", "set_numeric", "set_enum"):
        assert member in source


def _schema_property_names(schema: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, sub in properties.items():
            names.add(name)
            if isinstance(sub, dict):
                names |= _schema_property_names(sub)
    items = schema.get("items")
    if isinstance(items, dict):
        names |= _schema_property_names(items)
    return names


# -- the live envelope -------------------------------------------------------


def _pi_binary() -> Path | None:
    """The installed pi, or ``None`` when this checkout has not run ``npm ci``.

    Also ``None`` when the binary cannot execute — pi needs Node >= 22, and
    a dev box on an older runtime should skip rather than fail with a
    stack trace from someone else's program.
    """
    binary = resolve_pi_bin(ExternalProposerConfig(dotted_path="x:y"))
    if not binary.exists() or shutil.which("node") is None:
        return None
    try:
        probe = subprocess.run(  # noqa: S603 - a pinned in-tree binary
            [str(binary), "--version"], capture_output=True, timeout=60, check=False
        )
    except OSError:
        return None
    return binary if probe.returncode == 0 else None


@pytest.mark.pi
@pytest.mark.integration
def test_real_pi_offers_exactly_the_sanctioned_tools(tmp_path: Path) -> None:
    """The observation that cannot be fooled: ask the running agent.

    ``envelope-probe.ts`` reports ``pi.getActiveTools()`` at session start
    and registers nothing itself, so what it writes is the set the model
    can call — no ``bash``/``read``/``grep`` builtins, no pi skills, no
    memory packages.
    """
    binary = _pi_binary()
    if binary is None:
        pytest.skip("pi is not installed; run `npm ci` in integrations/pi (needs Node >= 22)")

    agent_dir = tmp_path / "agent"
    cwd = tmp_path / "cwd"
    agent_dir.mkdir()
    cwd.mkdir()
    probe = tmp_path / "tools.json"

    argv = build_pi_argv(
        binary=binary,
        model="google/gemini-2.0-flash",
        system_prompt="You are zicato's proposer.",
        extensions=tuple(INTEGRATION_DIR / name for name in SANCTIONED_EXTENSIONS),
    )
    argv += ["--extension", str(INTEGRATION_DIR / "envelope-probe.ts")]

    env = dict(os.environ)
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(agent_dir),
            "PI_OFFLINE": "1",
            "ZICATO_PI_ENVELOPE_PROBE": str(probe),
        }
    )
    subprocess.run(  # noqa: S603 - a pinned in-tree binary
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
    )

    assert probe.is_file(), "pi never started a session; the launch flags are wrong"
    assert json.loads(probe.read_text(encoding="utf-8"))["tools"] == sorted(SANCTIONED_TOOLS)
