# ruff: noqa: PLC0415
# MCP symbols are imported INSIDE the functions that need them on purpose
# — see the module docstring. Hoisting them to module scope would force
# the optional ``mcp`` extra on every importer of this module (and, via
# any package-level re-export, of ``zicato.proposer``), which no other
# proposer path requires.
"""Serve the proposer tool registry over the Model Context Protocol.

:mod:`zicato.proposer.tools` ships the tool set a proposer may call while
it reasons about the next experiment. The ADK path
(:class:`~zicato.proposer.adk_agent.ADKProposerAgent`) hands those
functions to an in-process ``LlmAgent``. A proposer that is NOT an
in-process ADK agent — an external agent process, an operator's own
client — cannot reach them that way. This module is the second door: a
thin stdio MCP server that exposes the SAME functions, unchanged, as MCP
tools.

Thin by contract
----------------
Nothing here reimplements a tool. Every MCP tool call lands on the exact
function in :data:`~zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS`, run
inside :func:`~zicato.proposer.tools.bind_proposer_tool_context` exactly
as the ADK path runs it — so the mutable-root resolution, the escape
guard, the output caps, each tool's own write posture and the redaction
its answers carry are the tools' own, with no second implementation to
drift. The tool LIST is likewise derived by reflecting over the registry
rather than transcribed: a tool added to ``DEFAULT_PROPOSER_TOOLS`` is
served here with no edit to this module.

The wrapper neither narrows nor widens the registry: whatever
``DEFAULT_PROPOSER_TOOLS`` contains is exactly what it serves. What
belongs on the sanctioned proposer surface — which tools may write (and
into what), which may query the training slice and under what redaction
— is therefore decided in :mod:`zicato.proposer.tools` rather than here. Do not
read a tool's presence on this transport as a second sanction; there is
only the one, upstream.

One server per challenger process
---------------------------------
A server built by :func:`build_mcp_server` is bound to ONE
:class:`~zicato.proposer.tools.ProposerToolContext` — one round, one
challenger, one parent snapshot. Do not share a server across concurrent
challengers. The tools read their context from a process-wide
:class:`contextvars.ContextVar`; a shared server would bind whichever
round's context its handler happened to be constructed with (or, worse,
re-bind mid-flight), so a challenger could read another challenger's
snapshot and manifest. Isolation here is the process boundary the
context var already assumes: launch one server per challenger process,
each pointed at its own context file.

Two entry points
----------------
* :func:`build_mcp_server` — the in-process embedding seam: hand it a
  context, get a configured low-level ``Server`` back.
* ``python -m zicato.proposer.mcp_server`` — the stdio launch. The
  per-round context is not expressible on a command line (it carries the
  whole mutation manifest), so it travels as a JSON file whose path the
  launcher puts in :data:`CONTEXT_ENV_VAR`;
  :func:`write_context_file` / :func:`load_context_file` are the matched
  pair that writes and rebuilds it. A missing or malformed file is fatal:
  serving with an empty context would answer every tool call with a
  plausible-looking lie about an empty snapshot.

Lazy MCP imports
----------------
Every ``mcp`` import in this module is local to the function that needs
it, so ``import zicato.proposer.mcp_server`` works without the optional
``mcp`` extra installed — the same discipline
:mod:`zicato.proposer.adk_agent` applies to ``google.adk``. Only the
server-building and serving paths pull MCP in.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import fields
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_origin, get_type_hints

from zicato.core.types import MutationPoint
from zicato.proposer.tools import (
    DEFAULT_PROPOSER_TOOLS,
    ProposerToolContext,
    bind_proposer_tool_context,
)
from zicato.storage import atomic_write_json, read_json

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from mcp import types
    from mcp.server import Server

#: Environment variable naming the per-round context file the stdio entry
#: point reads. Set by whatever launches the server process (one per
#: challenger — see the module docstring).
CONTEXT_ENV_VAR = "ZICATO_PROPOSER_TOOL_CONTEXT"

#: The MCP server name clients see. Stable — clients key tool
#: permissions on it.
SERVER_NAME = "zicato-proposer-tools"

#: Wire-format version of the context file. Bumped only on a
#: backward-incompatible change to the payload shape; the loader refuses
#: a version it does not know rather than guessing at the fields.
CONTEXT_FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# Tool definitions, reflected from the registry
# ---------------------------------------------------------------------------


def _tool_registry() -> dict[str, Callable[..., str]]:
    """Map tool name -> the registry function it dispatches to."""
    return {fn.__name__: fn for fn in DEFAULT_PROPOSER_TOOLS}


def _input_schema(fn: Callable[..., str]) -> dict[str, Any]:
    """Derive a tool's JSON-Schema input from its Python signature.

    Every tool in the registry takes zero or one ``str`` parameter and
    returns ``str``; the schema is built from those annotations rather
    than transcribed, so a newly-registered tool is described correctly
    without touching this module. An annotation this builder cannot
    express raises :class:`TypeError` — emitting a schema that silently
    disagrees with the function would hand the client a contract the call
    then rejects.
    """
    import inspect

    signature = inspect.signature(fn, eval_str=True)
    if signature.return_annotation is not str:
        raise TypeError(
            f"proposer tool {fn.__name__!r} returns "
            f"{signature.return_annotation!r}; the MCP wrapper serves text "
            "tools only (annotate the return as str)"
        )
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in signature.parameters.items():
        if param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            raise TypeError(
                f"proposer tool {fn.__name__!r} parameter {name!r} is "
                f"{param.kind.description}; MCP tool arguments must be "
                "nameable (positional-or-keyword or keyword-only)"
            )
        if param.annotation is not str:
            raise TypeError(
                f"proposer tool {fn.__name__!r} parameter {name!r} is "
                f"annotated {param.annotation!r}; the MCP wrapper supports "
                "str parameters only"
            )
        properties[name] = {"type": "string"}
        if param.default is param.empty:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def tool_definitions() -> list[types.Tool]:
    """Return the MCP tool list, reflected from the proposer registry.

    Name, description and input schema come from each registry
    function's ``__name__``, docstring and signature — the tools' own
    documented contract is what the client sees. A registry function
    with no docstring raises: an MCP client chooses tools by description,
    so an undescribed tool is an unusable one.
    """
    import inspect

    from mcp import types

    definitions: list[types.Tool] = []
    for fn in DEFAULT_PROPOSER_TOOLS:
        description = inspect.getdoc(fn)
        if not description:
            raise ValueError(
                f"proposer tool {fn.__name__!r} has no docstring; an MCP "
                "tool needs a description for the client to choose it"
            )
        definitions.append(
            types.Tool(
                name=fn.__name__,
                description=description,
                inputSchema=_input_schema(fn),
            )
        )
    return definitions


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


def build_mcp_server(ctx: ProposerToolContext) -> Server:
    """Build an MCP server serving the proposer tools bound to ``ctx``.

    The returned low-level ``Server`` is the in-process embedding seam:
    connect it to any MCP transport (stdio via :func:`serve_stdio`, or an
    in-memory client session in tests). Its ``call_tool`` handler runs the
    registry function inside
    :func:`~zicato.proposer.tools.bind_proposer_tool_context`, so the
    context var is set and reset per call exactly as the ADK path does —
    ``ctx`` is never installed process-wide.

    Errors are RESULTS rather than transport faults: a tool raising
    :class:`ValueError` — the tools' actionable-retry signal, e.g. an
    escape-guard rejection or an unknown mutation id — comes back as an
    error-flagged tool result carrying the message, so the client can
    correct itself and call again on the same session. A
    :class:`RuntimeError` (an unbound context) is a programming error in
    the embedder and is left to propagate.

    One server per challenger process — see the module docstring.
    """
    from mcp import types
    from mcp.server import Server

    registry = _tool_registry()
    server: Server = Server(SERVER_NAME, version=_zicato_version())

    # MCP's low-level decorators are themselves unannotated, so a strict
    # typecheck sees the handlers below as untyped once decorated. The
    # ignores are narrow and local; the handler bodies stay fully typed.
    @server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
    async def _list_tools() -> list[types.Tool]:
        return tool_definitions()

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        # The tools are synchronous and bounded (every one carries its own
        # output cap), and running them inline keeps the contextvar binding
        # exactly the block the call happens in — the same shape the ADK
        # path uses.
        try:
            fn = registry.get(name)
            if fn is None:
                raise ValueError(
                    f"unknown proposer tool {name!r}; available tools: "
                    f"{', '.join(sorted(registry))}"
                )
            with bind_proposer_tool_context(ctx):
                text = fn(**arguments)
        except ValueError as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))],
                isError=True,
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    return server


def _zicato_version() -> str:
    """Return the zicato version string reported to MCP clients."""
    from zicato import __version__

    return __version__


async def serve_stdio(server: Server) -> None:
    """Serve ``server`` over stdio until the client disconnects."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# ---------------------------------------------------------------------------
# The context file
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _mutation_field_kinds() -> tuple[tuple[str, str], ...]:
    """Return ``(field_name, wire_kind)`` for every :class:`MutationPoint` field.

    The serializer walks this rather than a transcribed field list, so a
    field added to :class:`MutationPoint` is carried across the wire
    instead of being silently dropped on one side of the round trip. A
    field whose type this encoder cannot represent raises :class:`TypeError`
    at the first serialize — loud, at the change that caused it.
    """
    hints = get_type_hints(MutationPoint)
    kinds: list[tuple[str, str]] = []
    for field in fields(MutationPoint):
        annotation = hints[field.name]
        if annotation is Path:
            kind = "path"
        elif get_origin(annotation) in (Mapping, dict):
            kind = "mapping"
        elif annotation in (str, int) or get_origin(annotation) is Literal:
            kind = "scalar"
        else:
            raise TypeError(
                f"MutationPoint.{field.name} is annotated {annotation!r}, which "
                "the proposer MCP context file cannot serialize; teach "
                "_mutation_field_kinds the new shape"
            )
        kinds.append((field.name, kind))
    return tuple(kinds)


def _mutation_to_dict(point: MutationPoint) -> dict[str, Any]:
    """Serialize one mutation point field-for-field to JSON-ready values."""
    payload: dict[str, Any] = {}
    for name, kind in _mutation_field_kinds():
        value = getattr(point, name)
        if kind == "path":
            payload[name] = str(value)
        elif kind == "mapping":
            payload[name] = dict(value)
        else:
            payload[name] = value
    return payload


def _mutation_from_dict(payload: Mapping[str, Any], *, source: Path) -> MutationPoint:
    """Rebuild one mutation point, raising on any missing field."""
    kwargs: dict[str, Any] = {}
    for name, kind in _mutation_field_kinds():
        if name not in payload:
            raise ValueError(
                f"proposer tool-context file {source}: mutation entry is "
                f"missing the {name!r} field"
            )
        value = payload[name]
        if kind == "path":
            kwargs[name] = Path(value)
        elif kind == "mapping":
            kwargs[name] = dict(value)
        else:
            kwargs[name] = value
    return MutationPoint(**kwargs)


def write_context_file(ctx: ProposerToolContext, path: Path) -> None:
    """Write ``ctx`` to ``path`` for a server process to load.

    The launcher side of the stdio entry point: it writes this file, puts
    its path in :data:`CONTEXT_ENV_VAR`, and spawns
    ``python -m zicato.proposer.mcp_server``. Written atomically (the
    workspace-wide discipline) so a server that starts while the launcher
    is still writing never reads a partial manifest.
    """
    payload = {
        "version": CONTEXT_FORMAT_VERSION,
        "workspace_root": str(ctx.workspace_root),
        "generation_root": str(ctx.generation_root),
        "epoch_id": ctx.epoch_id,
        "generation_id": ctx.generation_id,
        "mutations": [_mutation_to_dict(mp) for mp in ctx.mutations],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def load_context_file(path: Path) -> ProposerToolContext:
    """Rebuild the :class:`ProposerToolContext` ``write_context_file`` wrote.

    Fails LOUDLY on anything it cannot reconstruct — a missing file, a
    malformed one, an unknown format version, a missing field. There is
    no degraded mode: a server that came up on an empty context would
    answer every tool call with a confident description of an empty
    snapshot, which reads to the proposer as "this generation has no
    mutable surface" rather than as the misconfiguration it is.
    """
    try:
        raw = read_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"proposer tool-context file {path} is not valid JSON ({exc}); "
            f"it is written by write_context_file and named by {CONTEXT_ENV_VAR}"
        ) from exc
    if raw is None:
        raise FileNotFoundError(
            f"proposer tool-context file {path} does not exist; the launcher "
            f"must call write_context_file and point {CONTEXT_ENV_VAR} at it "
            "before starting the server"
        )
    if not isinstance(raw, dict):
        raise ValueError(
            f"proposer tool-context file {path} must contain a JSON object, "
            f"got {type(raw).__name__}"
        )
    version = raw.get("version")
    if version != CONTEXT_FORMAT_VERSION:
        raise ValueError(
            f"proposer tool-context file {path} declares format version "
            f"{version!r}; this server reads version {CONTEXT_FORMAT_VERSION}"
        )
    for key in ("workspace_root", "generation_root", "epoch_id", "mutations"):
        if key not in raw:
            raise ValueError(f"proposer tool-context file {path} is missing the {key!r} field")
    mutations = raw["mutations"]
    if not isinstance(mutations, list):
        raise ValueError(
            f"proposer tool-context file {path}: 'mutations' must be a list, "
            f"got {type(mutations).__name__}"
        )
    return ProposerToolContext(
        workspace_root=Path(raw["workspace_root"]),
        generation_root=Path(raw["generation_root"]),
        epoch_id=raw["epoch_id"],
        mutations=tuple(_mutation_from_dict(entry, source=path) for entry in mutations),
        generation_id=raw.get("generation_id", ""),
    )


def main() -> None:
    """Serve the proposer tools over stdio for one challenger process.

    Reads the per-round context from the file named by
    :data:`CONTEXT_ENV_VAR` and serves it. Every failure path here is
    fatal by design — see :func:`load_context_file`.
    """
    import asyncio

    raw_path = os.environ.get(CONTEXT_ENV_VAR, "").strip()
    if not raw_path:
        raise SystemExit(
            f"{CONTEXT_ENV_VAR} is unset; it must name the JSON tool-context "
            "file written by zicato.proposer.mcp_server.write_context_file"
        )
    ctx = load_context_file(Path(raw_path))
    asyncio.run(serve_stdio(build_mcp_server(ctx)))


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()


__all__ = [
    "CONTEXT_ENV_VAR",
    "CONTEXT_FORMAT_VERSION",
    "SERVER_NAME",
    "build_mcp_server",
    "load_context_file",
    "main",
    "serve_stdio",
    "tool_definitions",
    "write_context_file",
]
