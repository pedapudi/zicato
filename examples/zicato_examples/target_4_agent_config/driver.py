"""The target-4 driver: run a coding agent whose CONFIG PACKAGE is the target.

The mutable tree for this target is a directory of markdown — ``AGENTS.md``
plus ``skills/*.md`` — that the agent binary loads at startup. There is no
build step and no Python inside the tree: a generation snapshot of that
directory literally *is* an agent identity, so promoting a generation
promotes a configuration.

This module is the ENTRYPOINT, and it deliberately lives OUTSIDE the
mutable tree (``adapter.kind = "import"``, factory :func:`make_adapter`).
That is the dependency shape zicato already supports: the harness code is
installed, the tree is snapshotted, and each run is verified to have
mounted the snapshot's copy rather than a working-tree one.

What one run does
-----------------

1. Resolve the snapshot's config package (``<generation_root>/config_package``).
2. Copy it to a FRESH per-run agent directory under the run scratch dir, so
   the agent may write state without ever mutating the read-only snapshot,
   and so two concurrent units cannot see each other's state.
3. Copy the entry's fixture repository to a fresh per-run working tree.
4. Spawn the agent binary in rpc mode with the agent-config directory
   pointed at the copy, cwd at the working tree, and a minimal OFFLINE
   environment (no inherited credentials, no proxy variables).
5. Speak :ref:`the rpc protocol <rpc-protocol>` over stdin/stdout, forwarding
   each turn to the run's sinks as a ``transcript``-dialect line.
6. Diff the working tree against the fixture and append the diff to
   ``final_output`` under :data:`PATCH_SENTINEL`, so a board predicate can
   assert on the PRODUCED PATCH and not only on what the agent said.

.. _rpc-protocol:

The rpc protocol
----------------

Newline-delimited JSON, one request in, a stream of events out. The
driver writes exactly one line::

    {"op": "run", "input": "<the board entry's input>", "cwd": "<abs path>"}

and then reads lines until a terminal event or EOF:

* ``{"type": "turn", "role": "assistant"|"user", "content": "..."}`` — one
  user-facing turn. Forwarded verbatim to the sinks.
* ``{"type": "result", "final_output": "..."}`` — terminal. Ends the run.

Unparseable lines and unknown ``type`` values are ignored, so a binary that
interleaves its own diagnostics on stdout still drives cleanly. This is the
zicato-side shape; a binary that speaks a different wire needs a shim here,
not a change anywhere else in zicato.

Hygiene
-------

Offline environment, a fresh agent directory per run, the wall-clock budget
enforced as a hard kill, and the binary's ``--version`` recorded beside the
run. A version bump changes the system under test without changing the
tree, so by convention it is an epoch boundary — rebase the baseline rather
than comparing across it.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import logging
import os
import shlex
import shutil
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from zicato.core import RunResult
from zicato.epoch.snapshot_scope import SCRATCH_DIR_ENV

_log = logging.getLogger(__name__)

#: This example's directory. The driver is installed (never snapshotted),
#: so ``__file__`` resolves to a stable location and the fixture repos
#: below it are the same bytes for every generation — they are the
#: EVALUATION's property, not the target's.
EXAMPLE_DIR = Path(__file__).resolve().parent

#: Where the fixture repositories live, addressed by board entries through
#: :data:`FIXTURE_CONTEXT_KEY`.
FIXTURES_DIR = EXAMPLE_DIR / "fixtures"

#: The mutable tree's basename. A snapshot copies each registered tree
#: under its basename, so the package lands here inside every generation
#: root. A generation root that IS the package (an ad-hoc drive against
#: the example directly) is accepted as the fallback.
CONFIG_PACKAGE_DIRNAME = "config_package"

#: The environment variable the agent binary reads to locate its config
#: directory. Pointing it at a snapshot copy is the whole mechanism: the
#: config dir is the agent's identity.
AGENT_CONFIG_DIR_ENV = "PI_CODING_AGENT_DIR"

#: Operator knob for the agent binary, following target 1's
#: ``ZICATO_TARGET_1_MODEL`` precedent (a target-local variable read at the
#: point of use). The value is a COMMAND LINE, split with :mod:`shlex`, so
#: it can name a bare binary (``pi``), an absolute path, or an interpreter
#: plus module. ``--mode rpc --no-session`` is appended by the driver.
#:
#: A ``runtime.pi_bin`` knob now exists (#173) but belongs to a DIFFERENT
#: surface: :func:`zicato.proposer.pi_agent.resolve_pi_bin` reads it off
#: :class:`~zicato.proposer.external.ExternalProposerConfig`, which
#: configures the PROPOSER. This adapter configures the TARGET, and #170
#: keeps those two roles apart on purpose — the same binary in two roles
#: is the safety argument, so one knob naming both would erase the
#: distinction it rests on. The env var stays until a target-side knob
#: exists. See README.md for the pinned-install question that raises.
AGENT_BIN_ENV = "ZICATO_TARGET_4_AGENT_BIN"

#: Default when :data:`AGENT_BIN_ENV` is unset.
DEFAULT_AGENT_BIN = "pi"

#: ``BoardEntry.context`` key naming the fixture repository under
#: :data:`FIXTURES_DIR` that the entry's task is posed against.
FIXTURE_CONTEXT_KEY = "fixture"

#: Separates the agent's own final output from the unified diff of the
#: working tree the driver appends. Predicates split on it; see
#: :func:`zicato_examples.target_4_agent_config.predicates.produced_patch`.
PATCH_SENTINEL = "\n===== zicato:target_4:patch =====\n"

#: Environment variables passed through to the agent process. Everything
#: else — API keys, proxy settings, cloud credentials — is dropped, so a
#: run cannot reach the network by inheriting a caller's configuration.
#: ``PYTHONPATH`` is here because the CI stub is an interpreter invocation.
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "PYTHONPATH",
)

#: How an operator adds ONE variable to the agent's environment without
#: widening :data:`ENV_ALLOWLIST`: export ``ZICATO_TARGET_4_AGENT_ENV_FOO=1``
#: and the child sees ``FOO=1``. A closed allowlist that cannot be extended
#: is unusable for a binary with its own settings; naming each addition
#: explicitly keeps the extension auditable, which a blanket passthrough
#: would not.
ENV_PASSTHROUGH_PREFIX = "ZICATO_TARGET_4_AGENT_ENV_"

#: Set in the agent's environment to state the run has no network. The
#: allowlist above is what ENFORCES offline-ness for credential-shaped
#: access; this flag is how a cooperating binary is TOLD.
OFFLINE_ENV: Mapping[str, str] = {"PI_OFFLINE": "1", "NO_NETWORK": "1"}

#: Seconds allowed for the one-shot ``--version`` probe at load time.
VERSION_PROBE_TIMEOUT_SECONDS = 10.0

#: Filename the probed version is recorded under, beside the run.
VERSION_RECORD_NAME = "agent_binary_version.txt"

#: Files never copied into a per-run working tree or agent directory.
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")

#: ``BoardEntry.context`` keys carrying run provenance to the session. Kept
#: in sync with the tournament runner's
#: ``zicato.tournament.worker_transport`` constants — the two ends meet on
#: these strings. The runner stamps the generation id onto every worker
#: entry and the replication loop stamps the replicate index, which is the
#: only way a session can tell two runs of the same entry apart.
GENERATION_ID_CONTEXT_KEY = "generation_id"
REPLICATE_INDEX_CONTEXT_KEY = "replicate_index"


def run_identifier(entry: Any) -> str:
    """One run's id, unique per ``(generation, entry, replicate)``.

    A bare ``t4-<entry>`` would be REUSED across generations and
    replicates, which the analytical index (``runs`` rows keyed on
    ``run_id``) reads as the same run overwritten — the exact defect
    target 0 documents. It would also collide the per-run scratch
    directory, so two concurrent replicates of one entry would delete each
    other's working tree mid-run. An ad-hoc drive outside the worker (no
    generation in context) keeps the short form.
    """
    context = dict(getattr(entry, "context", {}) or {})
    try:
        replicate = int(context.get(REPLICATE_INDEX_CONTEXT_KEY, "0") or 0)
    except (TypeError, ValueError):
        replicate = 0
    parts = ["t4"]
    generation = str(context.get(GENERATION_ID_CONTEXT_KEY, "") or "")
    if generation:
        parts.append(generation)
    parts.append(str(getattr(entry, "id", "") or "entry"))
    if replicate:
        parts.append(f"r{replicate}")
    return "-".join(parts)


def agent_command() -> list[str]:
    """Return the full argv for one agent invocation.

    Reads :data:`AGENT_BIN_ENV` at call time (not import time) so a test or
    an operator can point the driver at a different binary without
    reloading the module.
    """
    raw = os.environ.get(AGENT_BIN_ENV, "") or DEFAULT_AGENT_BIN
    return [*shlex.split(raw), "--mode", "rpc", "--no-session"]


def agent_environment(agent_config_dir: Path) -> dict[str, str]:
    """Build the minimal offline environment for one agent process."""
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    for name, value in os.environ.items():
        if name.startswith(ENV_PASSTHROUGH_PREFIX) and len(name) > len(ENV_PASSTHROUGH_PREFIX):
            env[name[len(ENV_PASSTHROUGH_PREFIX) :]] = value
    env.update(OFFLINE_ENV)
    env[AGENT_CONFIG_DIR_ENV] = str(agent_config_dir)
    return env


def config_package_root(generation_root: Path) -> Path:
    """Resolve the config package inside ``generation_root``.

    The snapshot exposes the registered tree under its basename; an ad-hoc
    drive that hands the example's own ``config_package/`` directly is
    accepted as-is.
    """
    nested = Path(generation_root) / CONFIG_PACKAGE_DIRNAME
    return nested if nested.is_dir() else Path(generation_root)


def fixture_root(entry: Any) -> Path:
    """Resolve the fixture repository an entry poses its task against."""
    context = dict(getattr(entry, "context", {}) or {})
    name = str(context.get(FIXTURE_CONTEXT_KEY, "") or "")
    if not name:
        raise ValueError(
            f"board entry {getattr(entry, 'id', '?')!r} has no "
            f"context[{FIXTURE_CONTEXT_KEY!r}]; target 4 entries name their fixture repo"
        )
    root = FIXTURES_DIR / name
    if not root.is_dir():
        raise ValueError(f"fixture repository {name!r} not found under {FIXTURES_DIR}")
    return root


def _text_files(root: Path) -> dict[str, str]:
    """Map every readable text file under ``root`` to its content.

    Undecodable files are skipped — the diff is a review artifact for the
    predicates, and a binary blob has nothing to contribute to it.
    """
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return files


def tree_diff(before: Path, after: Path) -> str:
    """Return a stable unified diff of two directory trees.

    Paths are relative and sorted, so the diff is a pure function of the
    two trees — the same edit produces the same bytes on every machine.
    Added and removed files appear as diffs against an empty side.
    """
    old = _text_files(before)
    new = _text_files(after)
    chunks: list[str] = []
    for name in sorted(set(old) | set(new)):
        old_lines = old.get(name, "").splitlines(keepends=True)
        new_lines = new.get(name, "").splitlines(keepends=True)
        if old_lines == new_lines:
            continue
        chunks.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
                n=3,
            )
        )
        if chunks and not chunks[-1].endswith("\n"):
            chunks.append("\n")
    return "".join(chunks)


def _run_scratch_dir(run_id: str) -> Path:
    """Per-run scratch directory, honouring the runner's scratch contract.

    The runner exports :data:`~zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV`
    so run output lands OUTSIDE the generation snapshot. An ad-hoc drive
    with no scratch dir exported gets a temp directory instead, which keeps
    the snapshot clean either way.
    """
    base = os.environ.get(SCRATCH_DIR_ENV, "")
    root = Path(base) if base else Path(tempfile.mkdtemp(prefix="zicato-t4-"))
    scratch = root / run_id
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


async def _forward_turn(sinks: Sequence[Any], role: str, content: str) -> None:
    """Emit one transcript-dialect line to every sink.

    ``{"role": ..., "content": ...}`` is exactly what
    :func:`zicato.telemetry.dialects.reduce_transcript` reads, and the
    goldfive JSONL sink serialises a plain dict unchanged — so a
    ``telemetry_dialect: transcript`` contract needs no new plumbing.
    """
    for sink in sinks:
        try:
            await sink.emit({"role": role, "content": content})
        except Exception as exc:  # noqa: BLE001 — telemetry must never break a run
            _log.debug("target_4: sink %r rejected a transcript line: %s", sink, exc)


class _AgentConfigSession:
    """One loaded generation: run a board entry through the agent binary."""

    def __init__(self, generation_root: Path, agent_version: str = "") -> None:
        self.generation_root = Path(generation_root)
        self.config_package = config_package_root(generation_root)
        self.agent_version = agent_version

    async def run(self, entry: Any, sinks: Any, config: Any) -> RunResult:
        """Drive one board entry and return its :class:`RunResult`."""
        del config  # target 4 calls no model through zicato; the binary is the harness.
        started = time.monotonic()
        run_id = run_identifier(entry)
        sink_list = list(sinks or [])
        budget = float(getattr(entry, "wall_clock_budget_seconds", 0) or 0) or 600.0

        scratch = _run_scratch_dir(run_id)
        agent_dir = scratch / "agent-config"
        work = scratch / "work"
        fixture = fixture_root(entry)
        shutil.copytree(self.config_package, agent_dir, ignore=_COPY_IGNORE)
        shutil.copytree(fixture, work, ignore=_COPY_IGNORE)
        if self.agent_version:
            (scratch / VERSION_RECORD_NAME).write_text(self.agent_version + "\n", encoding="utf-8")

        prompt = str(getattr(entry, "input", "") or "")
        await _forward_turn(sink_list, "user", prompt)

        try:
            said = await asyncio.wait_for(
                self._drive(prompt, work, agent_dir, sink_list),
                timeout=budget,
            )
        except TimeoutError:
            runtime_ms = max(1, int((time.monotonic() - started) * 1000))
            return RunResult(
                run_id=run_id,
                entry_id=str(entry.id),
                final_output="",
                transcript=(),
                runtime_ms=runtime_ms,
                aborted=True,
                abort_reason="wall_clock_budget",
            )

        diff = tree_diff(fixture, work)
        final_output = said + PATCH_SENTINEL + diff
        runtime_ms = max(1, int((time.monotonic() - started) * 1000))
        return RunResult(
            run_id=run_id,
            entry_id=str(entry.id),
            final_output=final_output,
            transcript=(final_output,),
            runtime_ms=runtime_ms,
        )

    async def _drive(
        self,
        prompt: str,
        work: Path,
        agent_dir: Path,
        sinks: Sequence[Any],
    ) -> str:
        """Spawn the binary, speak the protocol, return its final output.

        Cancellation-safe: the ``finally`` kills the process group's leader
        and awaits it, so a wall-clock abort never leaves an orphan holding
        the working tree open.
        """
        argv = agent_command()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(work),
            env=agent_environment(agent_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert proc.stdin is not None and proc.stdout is not None
            request = json.dumps({"op": "run", "input": prompt, "cwd": str(work)})
            proc.stdin.write((request + "\n").encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            return await self._read_events(proc.stdout, sinks)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

    async def _read_events(self, stdout: Any, sinks: Sequence[Any]) -> str:
        """Consume protocol events until the terminal ``result`` or EOF."""
        final = ""
        async for raw in stdout:
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # a diagnostic line on stdout is not a protocol error
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "turn":
                role = "user" if str(event.get("role")) == "user" else "assistant"
                await _forward_turn(sinks, role, str(event.get("content", "")))
            elif kind == "result":
                final = str(event.get("final_output", ""))
                break
        return final


def probe_agent_version() -> str:
    """Return the agent binary's ``--version`` output, or ``""``.

    Best-effort and never fatal: a missing binary is a run-time failure with
    a far better error message than a load-time one, and an ad-hoc
    enumeration of the mutation surface must not need the binary at all.
    """
    import subprocess  # noqa: PLC0415 — only the version probe shells out

    command = shlex.split(os.environ.get(AGENT_BIN_ENV, "") or DEFAULT_AGENT_BIN)
    try:
        completed = subprocess.run(  # noqa: S603 — operator-configured command
            [*command, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _log.debug("target_4: agent version probe failed: %s", exc)
        return ""
    return completed.stdout.strip() or completed.stderr.strip()


def config_fingerprint(config_package: Path) -> str:
    """Digest of a config package's marker-bearing surface.

    Sorted-relative-path plus content, so two packages fingerprint equal
    iff their markdown is byte-identical. The stub agent reports this, which
    is how a test proves the run mounted the SNAPSHOT's package rather than
    the working tree's.
    """
    digest = hashlib.sha256()
    for name, content in sorted(_text_files(Path(config_package)).items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:12]


class AgentConfigAdapter:
    """Adapter whose mutable surface is a coding agent's config package.

    ``load`` captures the generation root the runner mounted and hands it to
    the session, so every run reads the configuration of exactly the
    generation under evaluation. ``mutation_points()`` returns the empty
    list — the orchestrator enumerates the markers from the snapshot itself
    (the same shape target 0 uses).
    """

    name = "agent_config"
    run_output_names: tuple[str, ...] = ()

    def __init__(self, probe_version: bool = True) -> None:
        self.probe_version = bool(probe_version)

    def mutable_subpaths(self, generation_root: Path) -> list[Path]:
        return [config_package_root(generation_root)]

    def load(self, generation_root: Path) -> _AgentConfigSession:
        version = probe_agent_version() if self.probe_version else ""
        if version:
            _log.info("target_4: agent binary version %s", version)
        return _AgentConfigSession(generation_root, agent_version=version)

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        """The subprocess-worker reconstruction spec (kind='import')."""
        return {
            "kind": "import",
            "factory": "zicato_examples.target_4_agent_config.driver:make_adapter",
            "args": [{"probe_version": self.probe_version}],
        }


def make_adapter(options: dict[str, Any] | None = None) -> AgentConfigAdapter:
    """Module-level factory for the ``import`` adapter spec.

    ``options`` is the single positional ``args`` element of the
    ``{"kind": "import", ...}`` block: ``{"probe_version": <bool>}``. An
    absent/empty dict keeps the version probe on.
    """
    return AgentConfigAdapter(probe_version=bool((options or {}).get("probe_version", True)))


__all__ = [
    "AGENT_BIN_ENV",
    "AGENT_CONFIG_DIR_ENV",
    "CONFIG_PACKAGE_DIRNAME",
    "DEFAULT_AGENT_BIN",
    "ENV_ALLOWLIST",
    "ENV_PASSTHROUGH_PREFIX",
    "EXAMPLE_DIR",
    "FIXTURES_DIR",
    "FIXTURE_CONTEXT_KEY",
    "GENERATION_ID_CONTEXT_KEY",
    "OFFLINE_ENV",
    "REPLICATE_INDEX_CONTEXT_KEY",
    "PATCH_SENTINEL",
    "VERSION_RECORD_NAME",
    "AgentConfigAdapter",
    "agent_command",
    "agent_environment",
    "config_fingerprint",
    "config_package_root",
    "fixture_root",
    "make_adapter",
    "probe_agent_version",
    "run_identifier",
    "tree_diff",
]
