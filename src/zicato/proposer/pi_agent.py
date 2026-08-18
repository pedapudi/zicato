"""Process-backed proposer with a bounded RPC and tool envelope.

Retries and native best-of-N turns share one process. The design, visibility
constraints, and extension contract live in ``docs/design/PROPOSER.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.types import Experiment, ProposerQualityConfig, ProposerSpec
from zicato.proposer.agent import ProposerContext, propose_via_engine
from zicato.proposer.best_of_n import BestOfNProposerAgent
from zicato.proposer.external import ExternalProposerConfig
from zicato.proposer.prompts import render_system_prompt
from zicato.proposer.proposer import ProposerError

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from types import TracebackType

log = logging.getLogger("zicato.proposer.pi")

#: Contract-hashed, envelope-tested structured output tools.
SANCTIONED_TOOLS: tuple[str, ...] = ("propose_experiment", "select_candidate")

#: Extension files we author, loaded explicitly by path. Hashed by BYTES
#: into the contract identity: they are edited in place, so they have no
#: version to record.
SANCTIONED_EXTENSIONS: tuple[str, ...] = ("propose-experiment.ts",)

#: No ambient tools, state, extensions, skills, context files, or startup IO.
SANCTIONED_FLAGS: tuple[str, ...] = (
    "--mode",
    "rpc",
    "--no-session",
    "--no-builtin-tools",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-context-files",
    "--no-approve",
    "--offline",
)

#: Where the pinned runtime and our extensions live, relative to this
#: checkout. ``runtime.pi_integration_dir`` overrides it.
_INTEGRATION_DIR = Path(__file__).resolve().parents[3] / "integrations" / "pi"

#: How much stderr to keep for diagnostics when a launch or a turn fails.
_STDERR_TAIL_LINES = 40

#: Node's default stream limit (64 KiB) is far below a single ``agent_end``
#: event, which carries every message of the run.
_STDOUT_LIMIT_BYTES = 16 * 1024 * 1024

#: How long a terminated subprocess gets to exit before it is killed.
_TERMINATE_GRACE_S = 5.0


class PiTransportError(RuntimeError):
    """The subprocess failed to produce a usable turn."""


def resolve_pi_bin(config: ExternalProposerConfig) -> Path:
    """Resolve the explicit executable or pinned install."""
    override = config.options.get("pi_bin")
    if override:
        return Path(override).expanduser()
    return _integration_dir(config) / "node_modules" / ".bin" / "pi"


def _integration_dir(config: ExternalProposerConfig) -> Path:
    override = config.options.get("pi_integration_dir")
    return Path(override).expanduser() if override else _INTEGRATION_DIR


def resolve_pi_version(config: ExternalProposerConfig) -> str:
    """Resolve the installed version, or the offline dependency pin."""
    binary = resolve_pi_bin(config)
    if binary.exists():
        # node_modules/.bin/pi is a symlink into the package's dist/.
        package_json = binary.resolve().parent.parent / "package.json"
        version = _read_json_key(package_json, "version")
        if version:
            return version
    pin = _integration_dir(config) / "package.json"
    try:
        deps = json.loads(pin.read_text(encoding="utf-8")).get("dependencies") or {}
    except (OSError, ValueError):
        return ""
    return str(deps.get("@earendil-works/pi-coding-agent", ""))


def _read_json_key(path: Path, key: str) -> str:
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get(key, ""))
    except (OSError, ValueError):
        return ""


def build_pi_argv(
    *,
    binary: Path,
    model: str,
    system_prompt: str,
    extensions: tuple[Path, ...],
    extra_tool_flags: tuple[str, ...] = (),
) -> list[str]:
    """Build the envelope-tested command line."""
    argv = [str(binary), *SANCTIONED_FLAGS, "--model", model, "--system-prompt", system_prompt]
    for extension in extensions:
        argv += ["--extension", str(extension)]
    argv += list(extra_tool_flags)
    return argv


def build_pi_env(agent_dir: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Inherit credentials but isolate all process-owned state.

    The agent dir is that isolation: themes, model list, sessions and trust
    decisions all hang off it. Pi's shipped assets are a separate root it
    resolves itself, and pointing that root at per-run state leaves it with
    no assets at all, so ``PI_PACKAGE_DIR`` is the operator's to set.
    """
    env = dict(os.environ)
    if extra:
        env.update(extra)
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    env["PI_CODING_AGENT_SESSION_DIR"] = str(agent_dir / "sessions")
    env["PI_OFFLINE"] = "1"
    return env


def prepare_agent_dir(workspace_root: Path) -> Path:
    """Create an isolated agent directory containing only credentials."""
    base = workspace_root / ".pi-proposer"
    base.mkdir(parents=True, exist_ok=True)
    agent_dir = Path(tempfile.mkdtemp(prefix="challenger-", dir=base))
    (agent_dir / "cwd").mkdir()
    source = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")
    auth = source / "auth.json"
    if auth.is_file():
        shutil.copy2(auth, agent_dir / "auth.json")
    return agent_dir


class PiRpcSession:
    """One live RPC conversation exposed as a text callable."""

    def __init__(self, proc: asyncio.subprocess.Process, system_prompt: str) -> None:
        self._proc = proc
        self._system_prompt = system_prompt
        self._next_id = 0
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr_task: asyncio.Task[None] | None = None
        self.selection_rationale = ""

    @classmethod
    async def launch(
        cls,
        *,
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        system_prompt: str,
    ) -> PiRpcSession:
        """Spawn the subprocess and start draining stderr."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=dict(env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STDOUT_LIMIT_BYTES,
            )
        except OSError as exc:
            raise PiTransportError(f"could not launch {argv[0]!r}: {exc}") from exc

        session = cls(proc, system_prompt)
        if proc.stderr is not None:
            session._stderr_task = asyncio.ensure_future(session._drain_stderr(proc.stderr))
        return session

    async def resolved_model(self) -> str:
        """Return the model id reported by the process."""
        response = await self._command({"type": "get_state"})
        model = (response.get("data") or {}).get("model") or {}
        return str(model.get("id", ""))

    async def call(self, system: str, user: str, model: str) -> str:
        """Send one prompt and return structured experiment JSON."""
        del model  # threaded at launch; see the module docstring
        if system != self._system_prompt:
            raise PiTransportError(
                "system prompt drifted from the one the pi process was "
                "launched with; the session cannot be reused"
            )
        response = await self._command({"type": "prompt", "message": user})
        if not response.get("success", False):
            raise PiTransportError(f"pi rejected the prompt: {response.get('error', '(no error)')}")
        try:
            return await self._await_experiment()
        except asyncio.CancelledError:
            # The engine's per-attempt timeout fired. Leave the session
            # usable for the next attempt rather than burning the budget.
            await self._abort()
            raise

    async def select(self, system: str, user: str, count: int) -> int | None:
        """Choose one candidate with the session's structured review tool."""
        message = (
            f"{system}\n\n{user}\n\n"
            "Review only. Call select_candidate with the strongest listed candidate."
        )
        response = await self._command({"type": "prompt", "message": message})
        if not response.get("success", False):
            raise PiTransportError(f"pi rejected the review: {response.get('error', '(no error)')}")
        selection = await self._await_tool("select_candidate")
        try:
            index = int(selection["index"])
            self.selection_rationale = str(selection["rationale"])
        except (KeyError, TypeError, ValueError):
            return None
        return index if 0 <= index < count else None

    async def aclose(self) -> None:
        """Terminate the subprocess and stop draining its stderr."""
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        if self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._proc.wait(), timeout=_TERMINATE_GRACE_S)
        if self._proc.returncode is None:  # pragma: no cover - stubborn child
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()

    async def __aenter__(self) -> PiRpcSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- protocol plumbing ------------------------------------------------

    async def _command(self, command: dict[str, Any]) -> dict[str, Any]:
        """Send one command and read back its correlated response."""
        self._next_id += 1
        request_id = f"zicato-{self._next_id}"
        await self._send({**command, "id": request_id})
        while True:
            event = await self._read()
            if event.get("type") == "response" and event.get("id") == request_id:
                return event

    async def _await_experiment(self) -> str:
        """Consume events until the turn settles; return the tool arguments."""
        return json.dumps(await self._await_tool("propose_experiment"))

    async def _await_tool(self, name: str) -> dict[str, Any]:
        emitted: dict[str, Any] = {}
        while True:
            event = await self._read()
            kind = event.get("type")
            if kind == "tool_execution_start" and event.get("toolName") == name:
                emitted = event.get("args") or {}
            elif kind == "agent_settled":
                return emitted

    async def _abort(self) -> None:
        """Best-effort cancellation without awaiting a second response."""
        with contextlib.suppress(Exception):
            await self._send({"type": "abort"})

    async def _send(self, command: dict[str, Any]) -> None:
        if self._proc.stdin is None:  # pragma: no cover - PIPE always set
            raise PiTransportError("pi subprocess has no stdin")
        self._proc.stdin.write((json.dumps(command) + "\n").encode("utf-8"))
        try:
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise PiTransportError(
                f"pi subprocess closed its input: {self._diagnostics()}"
            ) from exc

    async def _read(self) -> dict[str, Any]:
        """Read one JSONL record. Strict LF framing, per pi's rpc.md."""
        if self._proc.stdout is None:  # pragma: no cover - PIPE always set
            raise PiTransportError("pi subprocess has no stdout")
        while True:
            try:
                raw = await self._proc.stdout.readline()
            except ValueError as exc:  # line longer than the stream limit
                raise PiTransportError(f"oversized rpc record: {exc}") from exc
            if not raw:
                raise PiTransportError(f"pi subprocess exited: {self._diagnostics()}")
            line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                log.debug("pi: skipping non-JSON stdout line: %s", line[:200])
                continue
            if isinstance(event, dict):
                return event

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            raw = await stream.readline()
            if not raw:
                return
            self._stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())

    def _diagnostics(self) -> str:
        code = self._proc.returncode
        tail = " | ".join(self._stderr_tail) or "(no stderr)"
        return f"exit={code} stderr={tail}"


@dataclass(frozen=True)
class PiProposerAgent:
    """The external proposer backed by a pi coding-agent subprocess.

    Resolved by ``runtime.proposer_agent =
    "zicato.proposer.pi_agent:PiProposerAgent"``. Constructed by
    :func:`~zicato.proposer.agent.build_proposer_agent` with the spec that
    was hashed and the config it was hashed from.
    """

    spec: ProposerSpec
    config: ExternalProposerConfig

    #: Spells the ``external:pi`` agent id in the contract canon.
    external_id = "pi"

    @classmethod
    def contract_identity(cls, config: ExternalProposerConfig) -> Mapping[str, Any]:
        """The causal surface: what decides how this proposer reasons.

        The resolved pi version (coarse backstop), the sanctioned tool
        set, the launch envelope, and the sha256 of every extension file
        we author — those are edited in place, so their bytes are their
        version. Deliberately absent: the model, which is runtime infra by
        the same standing rule that keeps every ``models.*`` role out of
        the contract hash, and is guarded at launch instead.
        """
        integration = _integration_dir(config)
        return {
            "kind": "pi",
            "pi_version": resolve_pi_version(config),
            "tools": list(SANCTIONED_TOOLS),
            "flags": list(SANCTIONED_FLAGS),
            "extensions": {
                name: _sha256_file(integration / name) for name in SANCTIONED_EXTENSIONS
            },
        }

    def tool_flags(self) -> tuple[str, ...]:
        """Launch flags that register tools beyond the terminating one.

        The seam for the MCP tool server (issue #147 phases 3-5). pi has
        no native MCP flag: an adapter is loaded like any other extension,
        so this returns argv fragments — ``("--extension", "<adapter>.ts")``
        and whatever the adapter needs to find the server. Add the tool
        names it exposes to :data:`SANCTIONED_TOOLS` in the same change:
        that constant is read by the launch envelope, by the envelope
        assertion, and by :meth:`contract_identity`, so a widened tool
        surface rolls the epoch and is asserted in one place rather than
        three. Nothing else in this module needs to change.

        Empty today, so the launched surface is exactly the terminating
        tool.
        """
        return ()

    def tool_env(self, ctx: ProposerContext, agent_dir: Path) -> Mapping[str, str]:
        """Per-LAUNCH environment for whatever :meth:`tool_flags` registered.

        The other half of the seam, and the half that is easy to get
        wrong: a tool server needs this round's context, and best-of-N
        runs N challengers concurrently in one process, so the context
        cannot travel through the shared process environment — the last
        slot to write it would win for all of them. It travels here
        instead, one mapping per launch.

        ``agent_dir`` is the per-challenger isolated tree. Write anything
        the server must read (a serialized round context, a socket path)
        inside it: it is already unique per invocation and it is removed
        when the call ends, so nothing outlives the challenger that
        created it.

        Whatever this returns cannot loosen the envelope — the pi-state
        variables are applied after it (:func:`build_pi_env`).
        """
        del ctx, agent_dir
        return {}

    async def propose(self, ctx: ProposerContext) -> Experiment:
        """Drive one challenger's proposal through a live pi session."""
        return await self._run_session(ctx, lambda session: self._propose_one(session, ctx))

    async def propose_slate(
        self, ctx: ProposerContext, config: ProposerQualityConfig
    ) -> Experiment:
        """Generate and review a slate in one process with one conversation."""

        async def run(session: PiRpcSession) -> Experiment:
            inner = _SessionProposer(self.spec, session)
            slate = _PiBestOfNProposerAgent(
                inner=inner, config=config, session=session, candidates=inner.candidates
            )
            return await slate.propose(ctx)

        return await self._run_session(ctx, run)

    async def _propose_one(self, session: PiRpcSession, ctx: ProposerContext) -> Experiment:
        return await propose_via_engine(spec=self.spec, ctx=ctx, aux_call_llm=session.call)

    async def _run_session(
        self,
        ctx: ProposerContext,
        run: Callable[[PiRpcSession], Awaitable[Experiment]],
    ) -> Experiment:
        """Launch once, run either proposal capability, then scrub credentials."""
        if not ctx.model:
            raise ProposerError(
                [
                    "pi proposer: no model on the ProposerContext. Refusing to "
                    "launch: pi would fall back to its own configured default, "
                    "and the contract would record a model the run did not use."
                ]
            )
        workspace_root = self.config.workspace_root or ctx.workspace_root
        if workspace_root is None:
            raise ProposerError(
                ["pi proposer: no workspace root to place the isolated agent dir under"]
            )

        integration = _integration_dir(self.config)
        system_prompt = render_system_prompt(ctx.brief_text, self.spec.skills)
        agent_dir = prepare_agent_dir(workspace_root)
        try:
            # A launch failure is not a failed attempt — the engine's retry
            # budget never opened. Report it as the ProposerAgent protocol
            # requires so the orchestrator's existing handling applies.
            try:
                session = await PiRpcSession.launch(
                    argv=build_pi_argv(
                        binary=resolve_pi_bin(self.config),
                        model=ctx.model,
                        system_prompt=system_prompt,
                        extensions=tuple(integration / name for name in SANCTIONED_EXTENSIONS),
                        extra_tool_flags=self.tool_flags(),
                    ),
                    cwd=agent_dir / "cwd",
                    env=build_pi_env(agent_dir, self.tool_env(ctx, agent_dir)),
                    system_prompt=system_prompt,
                )
            except PiTransportError as exc:
                raise ProposerError([str(exc)]) from exc
            async with session:
                # The collusion guard's live half: --model went in, and this
                # is the id the process says it resolved. pi accepts a
                # `provider/id` pattern, so the resolved id is a substring of
                # what we passed rather than equal to it.
                resolved = await session.resolved_model()
                if resolved and resolved not in ctx.model:
                    log.warning(
                        "pi proposer: launched with --model %r but the process "
                        "resolved %r; the contract records the configured model",
                        ctx.model,
                        resolved,
                    )
                return await run(session)
        finally:
            # The dir holds a copy of the operator's credentials.
            shutil.rmtree(agent_dir, ignore_errors=True)


@dataclass
class _SessionProposer:
    spec: ProposerSpec
    session: PiRpcSession
    candidates: list[Experiment] = field(default_factory=list)

    async def propose(self, ctx: ProposerContext) -> Experiment:
        candidate = await propose_via_engine(
            spec=self.spec, ctx=ctx, aux_call_llm=self.session.call
        )
        self.candidates.append(candidate)
        return candidate


@dataclass
class _PiBestOfNProposerAgent(BestOfNProposerAgent):
    session: PiRpcSession | None = None
    candidates: list[Experiment] = field(default_factory=list)

    async def propose(self, ctx: ProposerContext) -> Experiment:
        emitter = ctx.round_event_emitter
        if emitter is None:
            return await super().propose(ctx)

        def emit(kind: str, fields: dict[str, Any]) -> None:
            if kind == "critique_selected":
                fields = {
                    **fields,
                    "slate": [
                        {
                            "index": i,
                            "core_idea": item.hypothesis.core_idea,
                            "mutation_ids": list(item.hypothesis.modulating),
                        }
                        for i, item in enumerate(self.candidates)
                    ],
                    "rationale": self.session.selection_rationale if self.session else "",
                }
            emitter(kind, fields)

        return await super().propose(replace(ctx, round_event_emitter=emit))

    async def _mint_recombined(self, ctx: ProposerContext) -> Experiment | None:
        candidate = await super()._mint_recombined(ctx)
        if candidate is not None:
            self.candidates.append(candidate)
        return candidate

    async def _merge_recombined(
        self, ctx: ProposerContext
    ) -> tuple[Experiment | None, tuple[str, ...]]:
        candidate, errors = await super()._merge_recombined(ctx)
        if candidate is not None:
            self.candidates.append(candidate)
        return candidate, errors

    async def _critique(
        self,
        aux_call_llm: Callable[[str, str, str], Awaitable[str]],
        candidates: list[Experiment],
        ctx: ProposerContext,
        screen_note: str = "",
    ) -> int | None:
        assert self.session is not None
        session = self.session

        async def native_review(system: str, user: str, model: str) -> str:
            del model
            choice = await session.select(system, user, len(candidates))
            return "" if choice is None else str(choice)

        return await super()._critique(native_review, candidates, ctx, screen_note)


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


__all__ = [
    "SANCTIONED_EXTENSIONS",
    "SANCTIONED_FLAGS",
    "SANCTIONED_TOOLS",
    "PiProposerAgent",
    "PiRpcSession",
    "PiTransportError",
    "build_pi_argv",
    "build_pi_env",
    "prepare_agent_dir",
    "resolve_pi_bin",
    "resolve_pi_version",
]
