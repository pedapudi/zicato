# ruff: noqa: PLC0415
# ADK / genai symbols are imported INSIDE functions on purpose — hoisting
# them to module scope would force the optional ``google-adk`` extra on
# every importer of ``zicato.builder`` (the form / REST path must stay
# dependency-light). See the module docstring.
"""The tournament-builder copilot (B1b) — agent build + streaming run.

This module builds the copilot ``LlmAgent`` and drives it on ADK's own
:class:`~google.adk.runners.InMemoryRunner`, translating the run's event
stream into the SSE event schema the builder frontend (B2) consumes. It
sits ON TOP of the B1a backend: the copilot's tools
(:data:`zicato.builder.copilot_tools.DEFAULT_BUILDER_TOOLS`) call the same
operations the form's REST ``op`` calls, mutating the SAME session draft —
one source of truth.

How the model is resolved (from ``builder.json``)
------------------------------------------------
:func:`build_copilot_agent` resolves the agent's model from
:class:`~zicato.builder.config.BuilderAgentConfig`:

* if ``agent.call_llm`` (a dotted path) is set, it is imported and used as
  the agent's ``model=`` directly — the escape hatch for a fully custom
  model object / factory; else
* the agent's ``model=`` is built from ``agent.model`` (the common case: a
  model string ADK understands) via the SINGLE ADK-model-from-spec builder
  :func:`zicato.models_config.build_adk_model`. When ``agent.endpoint`` /
  ``agent.api_key_env`` are also set, that builder routes through ADK's
  ``LiteLlm`` so a custom endpoint / API-key env var is honoured; with
  neither, the bare model string is handed to ``LlmAgent`` (ADK resolves
  it to its native provider). This module no longer constructs ``LiteLlm``
  itself — the one construction site is ``build_adk_model``.

The streaming SSE schema (B2 consumes this)
------------------------------------------
:func:`run_copilot` is an async generator of frame dicts:

* ``{"type": "token", "text": "..."}`` — an assistant text delta;
* ``{"type": "tool", "name": "...", "args": {...}}`` — a tool invocation;
* ``{"type": "patch", "patch": {...}, "cost": {...}, "warnings": [...],
  "diff": {...}}`` — after a draft-mutating tool, the SAME ``DraftPatch``
  shape the REST ``op`` returns, so the form updates live;
* ``{"type": "done"}`` — the run finished cleanly;
* ``{"type": "error", "message": "..."}`` — the run could not start / run
  (chat disabled, ADK missing, or the agent raised).

The REST layer (:func:`zicato.builder.api`) wraps these frames in
``text/event-stream`` and the mutations persist in the shared
:class:`~zicato.builder.draft.DraftStore`, so a subsequent ``GET
/builder/draft`` reflects them.

Lazy ADK imports
----------------
Every ``google.adk`` / ``google.genai`` import here is local to the
function that needs it, so importing :mod:`zicato.builder.copilot` (and,
transitively, :mod:`zicato.builder`) never requires the optional
``google-adk`` extra. Only :func:`build_copilot_agent` and the live-run
path of :func:`run_copilot` pull ADK in.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from zicato.builder.config import BuilderConfig
from zicato.builder.copilot_tools import (
    DEFAULT_BUILDER_TOOLS,
    BuilderToolContext,
    bind_builder_tool_context,
)
from zicato.builder.draft import DraftStore, TournamentDraft
from zicato.core.types import ProposerSkill
from zicato.import_path import import_dotted_path

#: The clear, single error frame returned when the copilot cannot run —
#: no model configured in ``builder.json`` or the optional ADK extra is not
#: installed. The form / REST path keeps working untouched in either case.
CHAT_DISABLED_MESSAGE = (
    "configure builder.json agent.model (and install the adk extra) to "
    "enable the build assistant"
)

#: Stable ADK app / user coordinates for a copilot run. The copilot is
#: single-session, single-user per chat request.
_APP_NAME = "zicato-builder-copilot"
_USER_ID = "zicato"

#: The copilot's static instruction. The per-request operator message is
#: the run INPUT; the skill bodies + current-draft summary are appended
#: here (see :func:`build_copilot_agent`).
_INSTRUCTION_HEAD = (
    "You are the tournament-builder copilot for zicato. You help an "
    "operator assemble a whole evaluation contract — the tournament "
    "structure and its params, the board and its train/holdout split, the "
    "scoring weights and promote gate, the proposer, and the proposer "
    "brief — by EDITING A DRAFT through your tools.\n\n"
    "Use your tools to make every change: each tool mutates the operator's "
    "draft and returns the resulting patch plus the updated cost (board-runs "
    "per round) and any validation warnings. Read that result and surface "
    "the consequence — a cost jump, a new warning — to the operator.\n\n"
    "You may PREVIEW applying with `preview_apply` (a dry run: it writes "
    "nothing and never rolls the epoch), but you must NEVER commit. "
    "Committing — which rolls the epoch — is the operator's deliberate "
    "action in the UI, not yours.\n"
)


def _builder_skills_roots(workspace_root: Path) -> list[Path]:
    """Candidate ``skills/`` roots a builder skill may live under, in order.

    The copilot's design/workflow skills (``zicato-build-tournament`` /
    ``zicato-build-board``) ship in the repo's top-level ``skills/`` tree;
    a workspace may also ship its own under ``<workspace>/skills/``. We
    probe the workspace first (so a workspace override wins) then the
    package's repo-root ``skills/`` (resolved from this module's location).
    """
    roots: list[Path] = []
    ws_skills = workspace_root / "skills"
    if ws_skills.is_dir():
        roots.append(ws_skills)
    # ``src/zicato/builder/copilot.py`` → repo root is three parents up
    # from the package dir (``src/zicato`` → ``src`` → repo root).
    repo_skills = Path(__file__).resolve().parents[3] / "skills"
    if repo_skills.is_dir():
        roots.append(repo_skills)
    return roots


def load_builder_skills(
    skill_names: tuple[str, ...],
    workspace_root: Path,
) -> tuple[ProposerSkill, ...]:
    """Load the named builder skills' ``SKILL.md`` bodies, in order.

    Each name resolves to ``<skills_root>/<name>/SKILL.md`` under the first
    candidate root (see :func:`_builder_skills_roots`) that has it. A name
    that resolves nowhere is skipped (the copilot still runs, just without
    that skill). Reuses the proposer's frontmatter parser so the loaded
    ``name`` / ``description`` / ``body`` match how proposer skills are
    parsed.
    """
    from zicato.proposer.skills import _parse_frontmatter

    roots = _builder_skills_roots(workspace_root)
    skills: list[ProposerSkill] = []
    for name in skill_names:
        for root in roots:
            md_path = root / name / "SKILL.md"
            if md_path.is_file():
                text = md_path.read_text(encoding="utf-8")
                parsed_name, description, body = _parse_frontmatter(text, stem=name)
                skills.append(ProposerSkill(name=parsed_name, description=description, body=body))
                break
    return tuple(skills)


def _render_skills_block(skills: tuple[ProposerSkill, ...]) -> str:
    """Render the loaded builder skills into an instruction section.

    Reuses the proposer's skill renderer so a builder skill reads in the
    instruction exactly as a proposer skill reads in the proposer system
    prompt. Empty skills ⇒ the empty string (omit the section).
    """
    from zicato.proposer.prompts import render_skills_block

    return render_skills_block(skills)


def _render_draft_summary(draft: TournamentDraft) -> str:
    """Render a compact current-draft snapshot for the instruction.

    Gives the model the starting state it is editing — the structure +
    params, the board size + holdout split, the proposer, and the cost /
    warnings — so it does not have to call a read tool just to orient. The
    operator's per-request message is the run input; this is the static
    backdrop.
    """
    from zicato.builder import operations as ops

    ts = draft.scoring.tournament_structure
    cost = ops.estimate_cost(draft)
    warns = ops.validate(draft)
    summary = {
        "structure": ts.structure,
        "params": dict(ts.params),
        "board_size": len(draft.entries),
        "holdout": draft.to_dict()["holdout"],
        "proposer_path": str(draft.proposer_path) if draft.proposer_path is not None else None,
        "promote_margin": draft.scoring.promote_margin,
        "board_runs_per_round": cost.board_runs_per_round,
        "warnings": [w.code for w in warns],
    }
    return json.dumps(summary, default=str, indent=2)


def _resolve_model(config: BuilderConfig, workspace_root: Path | None = None) -> Any:
    """Resolve the agent ``model=`` for the copilot.

    Resolution order:

    * the workspace ``models.builder`` role (when configured) WINS — a model
      change is runtime infra, not the contract, so it never rolls the epoch;
      resolved via :func:`zicato.models_config.resolve_builder_model`. Else
      fall back to ``builder.json`` ``agent`` (backward-compat):
    * ``agent.call_llm`` (dotted path) set ⇒ import it and use it directly
      (a custom model object, or a factory the caller pre-resolved).
    * else build from ``agent.model``: when ``endpoint`` / ``api_key_env``
      are set, route through ADK's ``LiteLlm`` (so a custom endpoint /
      API-key env var is honoured); otherwise hand the bare model string to
      ``LlmAgent`` (ADK resolves it to its native provider).

    Raises :class:`ValueError` when neither a ``call_llm`` path nor a model
    string is configured — but callers gate on
    :attr:`BuilderConfig.chat_enabled` first, so this is a guard, not the
    common path.
    """
    if workspace_root is not None:
        from zicato.models_config import load_models_config, resolve_builder_model
        from zicato.workspace_loader import load_workspace_config

        try:
            models = load_models_config(load_workspace_config(workspace_root))
        except (FileNotFoundError, ValueError):
            models = None
        if models is not None and not models.builder.is_empty:
            return resolve_builder_model(models.builder)

    agent_cfg = config.agent
    if agent_cfg.call_llm:
        return import_dotted_path(agent_cfg.call_llm, label="builder agent.call_llm")
    if not agent_cfg.model:
        raise ValueError("builder.json agent.model is empty; chat is disabled")
    # The model-spec branch shares THE single ADK-model-from-spec builder
    # (:func:`zicato.models_config.build_adk_model`) so ``builder.json``'s
    # ``agent`` and the unified ``models.*`` roles reach a provider through one
    # code path: ``endpoint`` / ``api_key_env`` set ⇒ a ``LiteLlm`` honouring a
    # custom base URL + API-key env var; neither set ⇒ the bare model string
    # handed back for ADK to resolve natively. Behaviour is identical to the
    # former inline construction; the duplication is gone.
    from zicato.models_config import RoleSpec, build_adk_model

    return build_adk_model(
        RoleSpec(
            model=agent_cfg.model,
            endpoint=agent_cfg.endpoint,
            api_key_env=agent_cfg.api_key_env,
        ),
        role="builder",
    )


def build_copilot_agent(
    config: BuilderConfig,
    draft: TournamentDraft,
    workspace_root: Path,
) -> Any:
    """Build the copilot ``LlmAgent`` for a chat request.

    The agent's instruction is the static copilot prompt
    (:data:`_INSTRUCTION_HEAD`) plus the injected builder-skill bodies
    (loaded by name from :attr:`BuilderConfig.skills`) plus a compact
    summary of the CURRENT draft. Its tools are
    :data:`~zicato.builder.copilot_tools.DEFAULT_BUILDER_TOOLS`, and its
    ``model=`` is resolved by :func:`_resolve_model`. ADK is imported
    lazily here so the module stays importable without the extra.
    """
    from google.adk.agents import LlmAgent

    skills = load_builder_skills(config.skills, workspace_root)
    instruction = _INSTRUCTION_HEAD
    skills_block = _render_skills_block(skills)
    if skills_block:
        instruction = f"{instruction}\n## Builder skills\n\n{skills_block}\n"
    instruction = f"{instruction}\n## Current draft\n\n{_render_draft_summary(draft)}\n"

    return LlmAgent(
        name="zicato_builder_copilot",
        model=_resolve_model(config, workspace_root),
        instruction=instruction,
        tools=list(DEFAULT_BUILDER_TOOLS),
    )


def _adk_available() -> bool:
    """Return ``True`` iff the optional ``google-adk`` extra is importable."""
    import importlib.util

    return importlib.util.find_spec("google.adk") is not None


def _models_builder_configured(workspace_root: Path) -> bool:
    """Return ``True`` iff the workspace ``models.builder`` role is configured.

    A configured ``models.builder`` enables the copilot even when
    ``builder.json`` carries no model (the unified config supersedes it), so
    the chat gate consults it alongside :attr:`BuilderConfig.chat_enabled`.
    """
    from zicato.models_config import load_models_config
    from zicato.workspace_loader import load_workspace_config

    try:
        models = load_models_config(load_workspace_config(workspace_root))
    except (FileNotFoundError, ValueError):
        return False
    return not models.builder.is_empty


async def run_copilot(
    config: BuilderConfig,
    *,
    session_id: str,
    message: str,
    store: DraftStore,
    workspace_root: Path,
    agent: Any | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run the copilot for one operator message, yielding SSE frame dicts.

    Graceful degrade: when chat is disabled (no model in ``builder.json``)
    OR the ADK extra is not importable AND no ``agent`` was injected, yields
    a single ``{"type": "error", "message": CHAT_DISABLED_MESSAGE}`` and
    stops — the form / REST path is untouched.

    Otherwise builds (or uses the injected) copilot agent, binds the
    builder tool context for ``session_id`` so every tool mutates the SAME
    draft in ``store``, runs the agent on ADK's ``InMemoryRunner``, and
    translates the event stream into the frame schema documented in the
    module docstring. Tool mutations persist in ``store`` for a subsequent
    ``GET /builder/draft``.

    ``agent`` is the test seam: a pre-built ``LlmAgent`` (wired to a
    :class:`~zicato.testing.adk_fake.FakeADKModel`) bypasses model
    resolution and disk loading entirely.
    """
    if agent is None and (
        not (config.chat_enabled or _models_builder_configured(workspace_root))
        or not _adk_available()
    ):
        yield {"type": "error", "message": CHAT_DISABLED_MESSAGE}
        return

    draft = store.get(session_id, workspace_root)
    try:
        if agent is not None:
            run_agent = agent
        else:
            run_agent = build_copilot_agent(config, draft, workspace_root)
    except Exception as exc:  # noqa: BLE001 — surface any build/resolve error cleanly
        yield {"type": "error", "message": f"could not build the build assistant: {exc}"}
        return

    tool_ctx = BuilderToolContext(
        session_id=session_id,
        store=store,
        workspace_root=workspace_root,
    )
    try:
        with bind_builder_tool_context(tool_ctx):
            async for frame in _drive_agent(run_agent, message):
                yield frame
    except Exception as exc:  # noqa: BLE001 — opaque agent/model errors are common
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
        return
    yield {"type": "done"}


async def _drive_agent(agent: Any, message: str) -> AsyncIterator[dict[str, Any]]:
    """Run the agent on ADK's ``Runner`` and yield translated SSE frames.

    Builds an :class:`~google.adk.runners.InMemoryRunner` over the agent,
    sends ``message`` as the user input, and walks the event stream:

    * a part with ``text`` ⇒ a ``token`` frame;
    * a part with a ``function_call`` ⇒ a ``tool`` frame;
    * a part with a ``function_response`` ⇒ a ``patch`` frame, parsed from
      the tool's JSON result (a result carrying ``error`` instead of a
      patch yields no patch frame — the model already saw the error text).

    The bound builder tool context is set by the caller, so each tool the
    runner dispatches mutates the right session draft.
    """
    import uuid

    from google.adk.runners import InMemoryRunner
    from google.genai import types as genai_types

    runner = InMemoryRunner(agent, app_name=_APP_NAME)
    session_id = uuid.uuid4().hex
    await runner.session_service.create_session(
        app_name=_APP_NAME,
        user_id=_USER_ID,
        session_id=session_id,
    )
    new_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=message)],
    )
    async for event in runner.run_async(
        user_id=_USER_ID,
        session_id=session_id,
        new_message=new_message,
    ):
        content = getattr(event, "content", None)
        if content is None:
            continue
        for part in getattr(content, "parts", None) or ():
            for frame in _frames_for_part(part):
                yield frame


def _frames_for_part(part: Any) -> list[dict[str, Any]]:
    """Translate one ADK content part into zero or more SSE frames.

    A text part ⇒ a ``token`` frame; a ``function_call`` part ⇒ a ``tool``
    frame; a ``function_response`` part whose JSON result carries a
    ``patch`` ⇒ a ``patch`` frame (mirroring the REST ``op`` response
    shape). Anything else yields nothing.
    """
    frames: list[dict[str, Any]] = []
    text = getattr(part, "text", None)
    if text:
        frames.append({"type": "token", "text": text})

    fn_call = getattr(part, "function_call", None)
    if fn_call is not None:
        frames.append(
            {
                "type": "tool",
                "name": getattr(fn_call, "name", "") or "",
                "args": dict(getattr(fn_call, "args", None) or {}),
            }
        )

    fn_response = getattr(part, "function_response", None)
    if fn_response is not None:
        patch_frame = _patch_frame_from_response(fn_response)
        if patch_frame is not None:
            frames.append(patch_frame)
    return frames


def _patch_frame_from_response(fn_response: Any) -> dict[str, Any] | None:
    """Build a ``patch`` frame from a tool's ``function_response``, or ``None``.

    The builder tools return a JSON string carrying ``{patch, cost,
    warnings, diff}`` on a mutation (and ``{error: ...}`` on a rejected
    edit, or a read-only shape with no ``patch``). ADK delivers that under
    the response's ``response`` mapping (commonly keyed ``result``). We
    parse it back out and emit a ``patch`` frame ONLY when a ``patch`` is
    present — so a read tool or a rejected edit produces no spurious form
    update.
    """
    raw = getattr(fn_response, "response", None)
    payload = _coerce_tool_result(raw)
    if not isinstance(payload, dict) or "patch" not in payload:
        return None
    return {
        "type": "patch",
        "patch": payload.get("patch"),
        "cost": payload.get("cost"),
        "warnings": payload.get("warnings", []),
        "diff": payload.get("diff"),
    }


def _coerce_tool_result(raw: Any) -> Any:
    """Coerce a function-response payload into the tool's JSON result dict.

    ADK wraps a tool's string return in a mapping (typically ``{"result":
    "<json string>"}``). We pull the inner value out and ``json.loads`` it
    when it is a JSON string, so the caller sees the tool's actual
    ``{patch, ...}`` dict regardless of the wrapping.
    """
    value = raw
    if isinstance(raw, dict):
        value = raw.get("result", raw.get("output", raw))
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


__all__ = [
    "CHAT_DISABLED_MESSAGE",
    "build_copilot_agent",
    "load_builder_skills",
    "run_copilot",
]
