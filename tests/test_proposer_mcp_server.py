"""Tests for the proposer MCP server (``zicato.proposer.mcp_server``).

The server is a thin wrapper: these tests hold it to that. They assert
the served tool list is REFLECTED from
:data:`~zicato.proposer.tools.DEFAULT_PROPOSER_TOOLS` (so a tool added to
the registry is served with no edit to the wrapper), that a real
client<->server round trip over an in-memory transport returns the
tools' own content, that the mutable-tree escape guard still bites
THROUGH the MCP layer, and that the context file round-trips and fails
loudly when absent or malformed.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp.shared.memory import (  # noqa: E402 - after the importorskip guard
    create_connected_server_and_client_session,
)
from mcp.types import TextContent  # noqa: E402 - after the importorskip guard

from zicato.proposer import mcp_server  # noqa: E402 - after the importorskip guard
from zicato.proposer.mcp_server import (  # noqa: E402 - after the importorskip guard
    CONTEXT_ENV_VAR,
    build_mcp_server,
    load_context_file,
    tool_definitions,
    write_context_file,
)
from zicato.proposer.tools import (  # noqa: E402 - after the importorskip guard
    DEFAULT_PROPOSER_TOOLS,
    ProposerToolContext,
)
from zicato.testing import make_mutation_point  # noqa: E402 - after the importorskip guard


def _make_ctx(
    tmp_path: Path, *, mutation_id: str = "harness__system_prompt"
) -> ProposerToolContext:
    """Build a snapshot + manifest context, mirroring test_proposer_tools.

    Layout::

        {tmp}/snapshot/harness/prompts.py

    The manifest's ``source_root`` basename is ``harness``, so
    ``ProposerToolContext.mutable_roots`` resolves ``{snapshot}/harness``
    alongside the snapshot root.
    """
    snapshot = tmp_path / "snapshot"
    harness = snapshot / "harness"
    harness.mkdir(parents=True, exist_ok=True)
    (harness / "prompts.py").write_text(
        "SYSTEM_PROMPT = 'You are a helpful assistant.'\n", encoding="utf-8"
    )
    mp = make_mutation_point(
        id=mutation_id,
        file=harness / "prompts.py",
        source_root=Path("/orig/harness"),
        content="You are a helpful assistant.",
    )
    return ProposerToolContext(
        workspace_root=tmp_path / "ws",
        generation_root=snapshot,
        epoch_id="ep-001",
        mutations=(mp,),
        generation_id="v1",
    )


def _text(result: object) -> str:
    """Concatenate the text blocks of a ``CallToolResult``."""
    content = getattr(result, "content", [])
    return "".join(block.text for block in content if isinstance(block, TextContent))


# ---------------------------------------------------------------------------
# The tool list is reflected, not transcribed
# ---------------------------------------------------------------------------


def test_tool_definitions_cover_the_whole_registry() -> None:
    """Every registry function is served — including one added later.

    The equality (not a superset check) is the point: a tool added to
    ``DEFAULT_PROPOSER_TOOLS`` must appear here with no edit to the
    wrapper, and a tool served under a name no registry function owns
    would be a hand-maintained list drifting from the registry.
    """
    served = {tool.name for tool in tool_definitions()}
    assert served == {fn.__name__ for fn in DEFAULT_PROPOSER_TOOLS}


def test_tool_definitions_describe_each_function_from_its_signature() -> None:
    registry = {fn.__name__: fn for fn in DEFAULT_PROPOSER_TOOLS}
    for tool in tool_definitions():
        fn = registry[tool.name]
        assert tool.description, f"{tool.name} must carry a description"
        assert inspect.getdoc(fn) == tool.description
        schema = tool.inputSchema
        assert schema["type"] == "object"
        params = list(inspect.signature(fn).parameters)
        assert schema["required"] == params
        assert set(schema["properties"]) == set(params)
        assert all(prop == {"type": "string"} for prop in schema["properties"].values())


async def test_a_tool_added_to_the_registry_is_served_with_no_wrapper_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate the next tool landing in the registry.

    The registry is read at build time, so extending it is the whole
    integration: the new tool is listed, described from its own docstring,
    and callable — no name list in the wrapper to update.
    """

    def probe_new_tool(subject: str) -> str:
        """Echo the subject back, standing in for a future proposer tool."""
        return f"probed {subject}"

    monkeypatch.setattr(
        mcp_server, "DEFAULT_PROPOSER_TOOLS", (*DEFAULT_PROPOSER_TOOLS, probe_new_tool)
    )
    server = build_mcp_server(_make_ctx(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        listed = await session.list_tools()
        called = await session.call_tool("probe_new_tool", {"subject": "the manifest"})

    tool = next(t for t in listed.tools if t.name == "probe_new_tool")
    assert tool.description == inspect.getdoc(probe_new_tool)
    assert tool.inputSchema["required"] == ["subject"]
    assert called.isError is False
    assert _text(called) == "probed the manifest"


async def test_list_tools_over_the_wire_matches_the_registry(tmp_path: Path) -> None:
    server = build_mcp_server(_make_ctx(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.list_tools()
    assert {tool.name for tool in result.tools} == {fn.__name__ for fn in DEFAULT_PROPOSER_TOOLS}


# ---------------------------------------------------------------------------
# End-to-end: the wrapped tools return the tools' own content
# ---------------------------------------------------------------------------


async def test_call_tool_round_trip_returns_real_content(tmp_path: Path) -> None:
    server = build_mcp_server(_make_ctx(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        manifest = await session.call_tool("list_mutation_points", {})
        source = await session.call_tool("read_mutable_file", {"relative_path": "prompts.py"})

    assert manifest.isError is False
    entries = json.loads(_text(manifest))["mutation_points"]
    assert [entry["id"] for entry in entries] == ["harness__system_prompt"]
    # Rendered relative to the snapshot root, exactly as the tool does it.
    assert entries[0]["file"] == "harness/prompts.py"

    assert source.isError is False
    assert "You are a helpful assistant." in _text(source)


async def test_escape_guard_bites_through_the_mcp_layer(tmp_path: Path) -> None:
    """A traversal attempt returns an error RESULT, never file content.

    Load-bearing: it proves the wrapper dispatches into the real tool
    (whose ``_resolve_under_mutable_roots`` guard rejects the path) rather
    than reimplementing the read, and that the tools' ``ValueError``
    retry signal survives as an error-flagged result instead of killing
    the session.
    """
    server = build_mcp_server(_make_ctx(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("read_mutable_file", {"relative_path": "../../etc/passwd"})
        # The session is still usable after the rejection.
        followup = await session.call_tool("read_mutable_file", {"relative_path": "prompts.py"})

    assert result.isError is True
    assert "root:" not in _text(result)
    assert "does not resolve to a file" in _text(result)
    assert followup.isError is False


async def test_unknown_mutation_id_is_an_error_result(tmp_path: Path) -> None:
    server = build_mcp_server(_make_ctx(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("mutation_usage", {"mutation_id": "nope"})
    assert result.isError is True
    assert "unknown mutation id" in _text(result)


async def test_unknown_tool_name_is_an_error_result(tmp_path: Path) -> None:
    server = build_mcp_server(_make_ctx(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("definitely_not_a_tool", {})
    assert result.isError is True


async def test_two_servers_keep_their_own_contexts(tmp_path: Path) -> None:
    """Per-server context isolation — the reason for one server per challenger."""
    first = build_mcp_server(_make_ctx(tmp_path / "a", mutation_id="harness__alpha"))
    second = build_mcp_server(_make_ctx(tmp_path / "b", mutation_id="harness__beta"))

    async with create_connected_server_and_client_session(first) as session_a:
        manifest_a = await session_a.call_tool("list_mutation_points", {})
    async with create_connected_server_and_client_session(second) as session_b:
        manifest_b = await session_b.call_tool("list_mutation_points", {})

    ids_a = [e["id"] for e in json.loads(_text(manifest_a))["mutation_points"]]
    ids_b = [e["id"] for e in json.loads(_text(manifest_b))["mutation_points"]]
    assert ids_a == ["harness__alpha"]
    assert ids_b == ["harness__beta"]


# ---------------------------------------------------------------------------
# The context file
# ---------------------------------------------------------------------------


def test_context_file_round_trips(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    path = tmp_path / "run" / "tool-context.json"
    write_context_file(ctx, path)
    assert load_context_file(path) == ctx


def test_context_file_round_trips_every_mutation_field(tmp_path: Path) -> None:
    """The manifest survives field-for-field, Paths included."""
    ctx = _make_ctx(tmp_path)
    path = tmp_path / "tool-context.json"
    write_context_file(ctx, path)
    restored = load_context_file(path).mutations[0]
    original = ctx.mutations[0]
    assert restored == original
    assert restored.file == original.file
    assert restored.source_root == original.source_root
    assert restored.metadata == original.metadata


async def test_a_loaded_context_serves_the_same_manifest(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    path = tmp_path / "tool-context.json"
    write_context_file(ctx, path)
    server = build_mcp_server(load_context_file(path))
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("read_mutable_file", {"relative_path": "prompts.py"})
    assert "You are a helpful assistant." in _text(result)


def test_missing_context_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as excinfo:
        load_context_file(tmp_path / "absent.json")
    message = str(excinfo.value)
    assert "write_context_file" in message
    assert CONTEXT_ENV_VAR in message


def test_malformed_context_file_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "tool-context.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_context_file(path)


def test_context_file_with_a_missing_field_fails_loudly(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    path = tmp_path / "tool-context.json"
    write_context_file(ctx, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["mutations"][0]["content_hash"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="content_hash"):
        load_context_file(path)


def test_context_file_with_an_unknown_version_fails_loudly(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    path = tmp_path / "tool-context.json"
    write_context_file(ctx, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="format version"):
        load_context_file(path)
