"""The pi coding-agent proposer: one RPC subprocess per challenger.

The first implementation of the external-proposer seam
(:mod:`zicato.proposer.external`). pi is a Node program; this module is
the Python side of the wire, and ``integrations/pi/`` is the pinned
runtime plus the one extension we author.

**The transport is the whole trick.** ``pi --mode rpc`` speaks strict-LF
JSONL over stdin/stdout, so the session outlives a single message — and
that is exactly the shape
:func:`~zicato.proposer.proposer.propose_experiment` already wants. Its
bounded-retry loop calls ``aux_call_llm(system, user, model)`` once per
attempt, feeding each failure back as the next attempt's prompt.
:meth:`PiRpcSession.call` satisfies that signature by sending the user
prompt into the *live* session and returning the arguments of the
terminating ``propose_experiment`` tool call as JSON text. So a retry is a
follow-up message on a warm conversation rather than a cold restart that
re-sends the whole manifest — and every downstream behaviour (the
forbidden-id enforcement, the post-apply validation hook, the meta-loop
bookends, ``revise_feedback``, the repair-turn prompt) is the engine's,
unchanged and unduplicated. This tier differs from the text shim in its
transport, not its semantics, which is what makes it an honest A/B
baseline.

**The envelope is the other half.** A default pi session has ``bash``,
``read`` and ``grep`` pointed at the working directory; a proposer with
those can read the board and the holdout slice, and nothing would warn.
So the launch is negative-flag-first (:data:`SANCTIONED_FLAGS`): built-in
tools off, extension/skill/prompt-template discovery off, context files
off, project-local files untrusted, offline. The agent dir is a fresh
isolated tree under the workspace with credentials copied into it
deliberately, never the operator's own — which also means no packages and
no cross-round memory. cwd is an empty directory outside every snapshot,
because the snapshot is the system under test and reading it ambiently
would be both an unhashed contract input and an injection path from the
thing being rewritten into the thing rewriting it. The single sanctioned
tool is :data:`SANCTIONED_TOOLS`, and ``tests/test_proposer_pi_envelope.py``
asserts that set the way contract-hash stability is asserted.

**The model is threaded, never inferred.** ``--model`` carries
:attr:`~zicato.proposer.agent.ProposerContext.model` — the resolved
auxiliary or ensemble-role model the rest of the proposer stack uses. An
empty one is a hard failure rather than a fallback, because pi's own
configured default deciding would mean the run is not the run the
contract describes.

Registering a tool server (the MCP surface of issue #147 phases 3-5) does
not touch the transport: override :meth:`PiProposerAgent.tool_flags` to
return the server's launch flags, and declare the tool names it exposes in
:data:`SANCTIONED_TOOLS`. Both are read by the launch AND by the contract
identity, so a widened tool surface rolls the epoch by construction.
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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core.types import Experiment, ProposerSpec
from zicato.proposer.agent import ProposerContext, propose_via_engine
from zicato.proposer.external import ExternalProposerConfig
from zicato.proposer.prompts import render_system_prompt
from zicato.proposer.proposer import ProposerError

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from types import TracebackType

log = logging.getLogger("zicato.proposer.pi")

#: The tools the proposer may call. Exactly one: the terminating
#: structured-output tool that ends its turn with the experiment. Read by
#: the launch, by the contract identity, and by the envelope assertion.
SANCTIONED_TOOLS: tuple[str, ...] = ("propose_experiment",)

#: Extension files we author, loaded explicitly by path. Hashed by BYTES
#: into the contract identity: they are edited in place, so they have no
#: version to record.
SANCTIONED_EXTENSIONS: tuple[str, ...] = ("propose-experiment.ts",)

#: The launch envelope. Every flag is load-bearing:
#:
#: * ``--mode rpc`` — the live session the retry loop rides on;
#: * ``--no-session`` — no session file. Cross-round persistence would be
#:   an unhashed side channel around the overfitting envelope, carrying
#:   raw observations across rounds and epoch boundaries with none of the
#:   banding the governed channels apply;
#: * ``--no-builtin-tools`` — no ``bash`` / ``read`` / ``grep`` / ``edit``.
#:   The visibility envelope is enforced by what the proposer is shown;
#: * ``--no-extensions`` — no ambient extension discovery (explicit ``-e``
#:   paths still load, which is how ours does);
#: * ``--no-skills`` / ``--no-prompt-templates`` — pi has its own skills
#:   mechanism. ``proposers/<name>/skills/*.md`` is the hashed one; a
#:   second, unhashed one is how generations stop being comparable with no
#:   signal that anything changed;
#: * ``--no-context-files`` / ``--no-approve`` — no ``AGENTS.md``, no
#:   project-local ``.pi/`` from the working directory;
#: * ``--offline`` — no startup network operations.
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
    """The pi subprocess failed to produce a usable turn.

    Raised by :meth:`PiRpcSession.call`, where
    :func:`~zicato.proposer.proposer.propose_experiment` treats it like any
    other failed attempt: the message becomes the next attempt's feedback.
    A malformed *experiment* is not this error — that is the engine's
    :class:`~zicato.proposer.structured.ExperimentParseError` path.
    """


def resolve_pi_bin(config: ExternalProposerConfig) -> Path:
    """Resolve the pi executable: ``runtime.pi_bin``, else the pinned install.

    The default is ``integrations/pi/node_modules/.bin/pi`` — the binary
    ``npm install`` materializes from the exact version pinned there. The
    knob exists for dev clones and for operators who install pi elsewhere.
    """
    override = config.options.get("pi_bin")
    if override:
        return Path(override).expanduser()
    return _integration_dir(config) / "node_modules" / ".bin" / "pi"


def _integration_dir(config: ExternalProposerConfig) -> Path:
    override = config.options.get("pi_integration_dir")
    return Path(override).expanduser() if override else _INTEGRATION_DIR


def resolve_pi_version(config: ExternalProposerConfig) -> str:
    """The version of the pi install that will actually be launched.

    Read from the ``package.json`` beside the resolved binary — the
    RESOLVED version, not the one a config file asked for — falling back
    to the pin in ``integrations/pi/package.json`` when the binary is not
    materialized (contract hashing happens on machines that never run
    ``npm install``). Both are deterministic and offline; neither launches
    a process, because this runs on the contract-hash path.

    A pi upgrade therefore rolls the epoch, which is the coarse backstop
    behind the finer signals in :meth:`PiProposerAgent.contract_identity`.
    The standing rule stays: do not upgrade pi mid-tournament.
    """
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
    """The exact command line. Pure, so the envelope test can assert it.

    ``extra_tool_flags`` is the tool-registration seam: an MCP server is
    appended here, never by editing the flag set above.
    """
    argv = [str(binary), *SANCTIONED_FLAGS, "--model", model, "--system-prompt", system_prompt]
    for extension in extensions:
        argv += ["--extension", str(extension)]
    argv += list(extra_tool_flags)
    return argv


def build_pi_env(agent_dir: Path) -> dict[str, str]:
    """The child environment: the operator's, with pi's own vars overridden.

    Inheriting the parent environment is deliberate — provider credentials
    configured as environment variables are how most operators authenticate,
    and cutting them would just move the secret into a file. What is NOT
    inherited is pi's own state: the agent dir, the session dir and the
    package dir all point inside a fresh per-challenger tree, so the
    process cannot reach the operator's installed packages, memory
    extensions or saved trust decisions.
    """
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    env["PI_CODING_AGENT_SESSION_DIR"] = str(agent_dir / "sessions")
    env["PI_PACKAGE_DIR"] = str(agent_dir / "packages")
    env["PI_OFFLINE"] = "1"
    return env


def prepare_agent_dir(workspace_root: Path) -> Path:
    """Mint a fresh, isolated pi agent dir and provision credentials into it.

    Fresh per invocation (best-of-N runs N slots concurrently under one
    generation id, so the generation is not a unique key), under
    ``<workspace>/.pi-proposer/``. ``auth.json`` — and only ``auth.json`` —
    is copied from the operator's real agent dir: the process is cut off
    from that dir precisely so that everything it gets from there is
    something we chose to give it.
    """
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
    """One live ``pi --mode rpc`` conversation, driven as an LLM callable.

    :meth:`call` has the ``aux_call_llm`` signature, so the engine's
    bounded-retry loop drives this session without knowing it is one.
    """

    def __init__(self, proc: asyncio.subprocess.Process, system_prompt: str) -> None:
        self._proc = proc
        self._system_prompt = system_prompt
        self._next_id = 0
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr_task: asyncio.Task[None] | None = None

    @classmethod
    async def launch(
        cls,
        *,
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str],
        system_prompt: str,
    ) -> PiRpcSession:
        """Spawn the subprocess and start draining its stderr.

        Returns as soon as the process exists; whether it came up usable is
        established by the first command sent to it, which reports the exit
        status and the captured stderr when it did not.
        """
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
        """The model id the running process actually resolved.

        The collusion guard's live half: we pass ``--model``, and this is
        what the process says it heard. A mismatch is worth knowing about
        because a contract that records one model while the run uses
        another is a silently invalid comparison.
        """
        response = await self._command({"type": "get_state"})
        model = (response.get("data") or {}).get("model") or {}
        return str(model.get("id", ""))

    async def call(self, system: str, user: str, model: str) -> str:
        """Send one user prompt; return the emitted experiment as JSON text.

        Matches the ``aux_call_llm`` signature the engine calls. ``system``
        and ``model`` were fixed at launch (they are process-level flags),
        so they are checked rather than used — a divergence means the
        caller composed a different prompt than the one the process is
        running under, which would make every attempt after the first a
        different proposer.

        Returns the empty string when the turn settled without calling
        ``propose_experiment``. That is not an error here: it is exactly
        the empty-response case the engine's repair turn already targets.
        """
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
        emitted = ""
        while True:
            event = await self._read()
            kind = event.get("type")
            if kind == "tool_execution_start" and event.get("toolName") in SANCTIONED_TOOLS:
                emitted = json.dumps(event.get("args") or {})
            elif kind == "agent_settled":
                return emitted

    async def _abort(self) -> None:
        """Best-effort: tell the running turn to stop.

        Fire-and-forget by design. This runs while the calling task is
        being cancelled, so awaiting a correlated response would be a
        second suspension point inside a cancellation — and it is not
        needed: the next command reads until it sees its OWN request id,
        so the abort's response and any trailing events are skipped.
        """
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

    async def propose(self, ctx: ProposerContext) -> Experiment:
        """Drive one challenger's proposal through a live pi session."""
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
                    env=build_pi_env(agent_dir),
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
                return await propose_via_engine(spec=self.spec, ctx=ctx, aux_call_llm=session.call)
        finally:
            # The dir holds a copy of the operator's credentials.
            shutil.rmtree(agent_dir, ignore_errors=True)


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
