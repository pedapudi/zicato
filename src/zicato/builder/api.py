"""REST surface for the tournament builder, wired into the dashboard.

Four thin handlers over the operations in :mod:`zicato.builder.operations`
and the draft state in :mod:`zicato.builder.draft`:

* ``GET  /builder/config`` — the public builder config + ``chat_enabled``.
* ``GET  /builder/draft?session=ID`` — the draft snapshot (init from the
  live contract when the session is new).
* ``POST /builder/op`` ``{session, op, args}`` — run one operation and
  return ``{draft, patch, cost, warnings, diff}``. This is the SAME path
  the form's direct edits and the copilot's tools both call — one source
  of truth.
* ``POST /builder/apply`` ``{session, confirm}`` — write the draft (or
  preview it) and return the :class:`~zicato.builder.operations.ApplyResult`.

The handlers are dispatched by op name to the operation functions, with
typed args (``BoardEntry`` / ``JudgeSpec``) reconstructed from their JSON
shapes via the existing validators. They never start a live run.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from zicato.builder import operations as ops
from zicato.builder.config import load_builder_config
from zicato.builder.draft import DraftStore, TournamentDraft
from zicato.core.types import (
    JudgeMode,
    JudgeSpec,
    validate_board_entry,
)

#: Default session id used when a request omits one. A single shared
#: session is enough for the common single-operator case; concurrent
#: sessions pass their own id and get an independent draft.
_DEFAULT_SESSION = "default"


def _judge_from_dict(raw: Any) -> JudgeSpec:
    """Reconstruct a :class:`JudgeSpec` from its JSON shape.

    Reuses the same enum coercion the board loader applies so an invalid
    ``mode`` / ``severity`` token fails with a clear message.
    """
    from goldfive import DriftSeverity  # noqa: PLC0415

    if not isinstance(raw, dict):
        raise ValueError("judge must be a JSON object")
    return JudgeSpec(
        name=str(raw["name"]),
        mode=JudgeMode(str(raw.get("mode", "inline"))),
        body=str(raw["body"]),
        severity=DriftSeverity(str(raw.get("severity", "warning"))),
    )


def _dispatch_op(draft: TournamentDraft, op: str, args: dict[str, Any]) -> ops.DraftPatch:
    """Run one builder operation by name, reconstructing typed args.

    Raises :class:`ValueError` on an unknown op or a malformed arg, which
    the handler turns into a 400.
    """
    if op == "set_structure":
        return ops.set_structure(draft, str(args["structure"]))
    if op == "set_param":
        return ops.set_param(draft, str(args["key"]), args.get("value"))
    if op == "set_holdout":
        return ops.set_holdout(
            draft,
            enabled=args.get("enabled"),
            fraction=args.get("fraction"),
            tags=args.get("tags"),
        )
    if op == "set_proposer":
        return ops.set_proposer(draft, args.get("proposer_path"))
    if op == "set_weights":
        return ops.set_weights(
            draft,
            drift_weight=args.get("drift_weight"),
            pass_weight=args.get("pass_weight"),
            per_kind_weights=args.get("per_kind_weights"),
            per_judge_weights=args.get("per_judge_weights"),
            default_judge_weight=args.get("default_judge_weight"),
            plan_revision_weight=args.get("plan_revision_weight"),
            runtime_weight=args.get("runtime_weight"),
            severity_weights=args.get("severity_weights"),
        )
    if op == "set_gate":
        return ops.set_gate(
            draft,
            promote_margin=args.get("promote_margin"),
            monotonicity=args.get("monotonicity"),
            monotonicity_scope=args.get("monotonicity_scope"),
        )
    if op == "edit_board_entry":
        entry = validate_board_entry(args["entry"])
        return ops.edit_board_entry(draft, entry)
    if op == "add_judge":
        return ops.add_judge(draft, str(args["entry_id"]), _judge_from_dict(args["judge"]))
    if op == "remove_judge":
        return ops.remove_judge(draft, str(args["entry_id"]), str(args["name"]))
    if op == "set_brief":
        return ops.set_brief(draft, str(args["text"]))
    raise ValueError(f"unknown builder op {op!r}")


def _format_sse(frame: dict[str, Any]) -> str:
    """Encode one copilot frame as an SSE ``data:`` event.

    Reuses the dashboard's one-frame-per-``data:``-line convention (see
    :func:`zicato.dashboard.sse._format_sse`); each frame's ``type`` is its
    semantic event, carried inside the JSON so the frontend reads a single
    uniform schema off ``JSON.parse(e.data).type``.
    """
    return f"data: {json.dumps(frame, default=str)}\n\n"


async def _read_json_body(request: Request) -> dict[str, Any]:
    """Parse a JSON request body into a dict (empty on absence / error)."""
    try:
        body = await request.body()
    except Exception:  # noqa: BLE001 — defensive against a broken stream
        return {}
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("request body must be a JSON object")
    return parsed


def make_builder_endpoints(
    workspace_root: Path,
    *,
    read_only: bool = False,
    store: DraftStore | None = None,
) -> dict[str, Callable[[Request], Awaitable[Response]]]:
    """Build the builder REST handlers bound to a workspace + draft store.

    The handlers are thin over :mod:`zicato.builder.operations`. ``store``
    lets a caller (or a test) inject a pre-seeded :class:`DraftStore`;
    omitted, a fresh process-local store is created so each app gets its
    own. ``read_only`` mirrors the dashboard's flag — the POST ops and
    ``apply`` return ``403`` when set (the GET reads stay available).
    """
    root = Path(workspace_root)
    draft_store = store if store is not None else DraftStore()

    def _forbidden() -> JSONResponse | None:
        if read_only:
            return JSONResponse({"error": "builder is read-only"}, status_code=403)
        return None

    def _session_of(args: dict[str, Any], request: Request) -> str:
        return str(args.get("session") or request.query_params.get("session") or _DEFAULT_SESSION)

    def _op_response(draft: TournamentDraft, patch: ops.DraftPatch) -> JSONResponse:
        cost = ops.estimate_cost(draft)
        warns = ops.validate(draft)
        diff = draft.diff_vs_live(root)
        return JSONResponse(
            {
                "draft": draft.to_dict(),
                "patch": patch.to_dict(),
                "cost": cost.to_dict(),
                "warnings": [w.to_dict() for w in warns],
                "diff": diff.to_dict(),
            }
        )

    async def builder_config(_request: Request) -> JSONResponse:
        cfg = load_builder_config(root)
        return JSONResponse(cfg.to_public_dict())

    async def builder_draft(request: Request) -> JSONResponse:
        session = request.query_params.get("session") or _DEFAULT_SESSION
        draft = draft_store.get(session, root)
        cost = ops.estimate_cost(draft)
        warns = ops.validate(draft)
        diff = draft.diff_vs_live(root)
        return JSONResponse(
            {
                "session": session,
                "draft": draft.to_dict(),
                "cost": cost.to_dict(),
                "warnings": [w.to_dict() for w in warns],
                "diff": diff.to_dict(),
            }
        )

    async def builder_op(request: Request) -> JSONResponse:
        forbidden = _forbidden()
        if forbidden is not None:
            return forbidden
        try:
            body = await _read_json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        op = body.get("op")
        if not isinstance(op, str) or not op:
            return JSONResponse({"error": "missing 'op'"}, status_code=400)
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return JSONResponse({"error": "'args' must be a JSON object"}, status_code=400)
        session = _session_of(body, request)
        draft = draft_store.get(session, root)
        try:
            patch = _dispatch_op(draft, op, args)
        except KeyError as exc:
            return JSONResponse(
                {"error": f"missing arg {exc.args[0]!r} for op {op!r}"}, status_code=400
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return _op_response(draft, patch)

    async def builder_apply(request: Request) -> JSONResponse:
        forbidden = _forbidden()
        if forbidden is not None:
            return forbidden
        try:
            body = await _read_json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        session = _session_of(body, request)
        confirm = bool(body.get("confirm", False))
        draft = draft_store.get(session, root)
        try:
            result = ops.apply(draft, root, confirm)
        except FileNotFoundError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(result.to_dict())

    async def builder_chat(request: Request) -> Response:
        """Stream the copilot's reply + draft patches as SSE.

        Reads ``{session, message}``, then streams the copilot run's frames
        (``token`` / ``tool`` / ``patch`` / ``done`` / ``error``) as
        ``text/event-stream``. The copilot's tools mutate the SAME session
        draft this handler's siblings edit (the shared ``draft_store``), so
        the FORM updates live and a subsequent ``GET /builder/draft``
        reflects the change. Graceful degrade — a disabled / ADK-less chat
        yields a single clear ``error`` frame (the form path is untouched).
        The copilot never rolls the epoch: its ``apply`` is dry-run only.
        """
        forbidden = _forbidden()
        if forbidden is not None:
            return forbidden
        try:
            body = await _read_json_body(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        message = body.get("message")
        if not isinstance(message, str) or not message:
            return JSONResponse({"error": "missing 'message'"}, status_code=400)
        session = _session_of(body, request)
        config = load_builder_config(root)

        async def _stream() -> AsyncIterator[str]:
            from zicato.builder.copilot import run_copilot  # noqa: PLC0415

            async for frame in run_copilot(
                config,
                session_id=session,
                message=message,
                store=draft_store,
                workspace_root=root,
            ):
                yield _format_sse(frame)

        return StreamingResponse(_stream(), media_type="text/event-stream")

    return {
        "builder_config": builder_config,
        "builder_draft": builder_draft,
        "builder_op": builder_op,
        "builder_apply": builder_apply,
        "builder_chat": builder_chat,
    }


def builder_routes(
    workspace_root: Path,
    *,
    read_only: bool = False,
    store: DraftStore | None = None,
) -> list[Route]:
    """Return the builder routes, ready to splice into the dashboard app.

    Wired by :func:`zicato.dashboard.server.create_app`.
    """
    handlers = make_builder_endpoints(workspace_root, read_only=read_only, store=store)
    return [
        Route("/builder/config", handlers["builder_config"], methods=["GET"]),
        Route("/builder/draft", handlers["builder_draft"], methods=["GET"]),
        Route("/builder/op", handlers["builder_op"], methods=["POST"]),
        Route("/builder/apply", handlers["builder_apply"], methods=["POST"]),
        Route("/builder/chat", handlers["builder_chat"], methods=["POST"]),
    ]


__all__ = [
    "make_builder_endpoints",
    "builder_routes",
]
