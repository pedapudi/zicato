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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from zicato.builder import operations as ops
from zicato.builder.config import load_builder_config
from zicato.builder.copilot import builder_chat_enabled
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
    from zicato.core.drift_kinds import DriftSeverity  # noqa: PLC0415

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
            enabled=_opt_bool(args, "enabled"),
            fraction=_opt_float(args, "fraction"),
            tags=args.get("tags"),
            min_board_size_for_split=_opt_int(args, "min_board_size_for_split"),
            rotate_holdout=_opt_bool(args, "rotate_holdout"),
            restrict_proposer_visibility=_opt_bool(args, "restrict_proposer_visibility"),
            random_baseline_every_n=_opt_int(args, "random_baseline_every_n"),
            max_generations_per_contract=_opt_int(args, "max_generations_per_contract"),
            ladder=args.get("ladder"),
        )
    if op == "set_proposer":
        return ops.set_proposer(draft, args.get("proposer_path"))
    if op == "set_weights":
        return ops.set_weights(
            draft,
            pass_weight=_opt_float(args, "pass_weight"),
            per_kind_weights=args.get("per_kind_weights"),
            per_judge_weights=args.get("per_judge_weights"),
            default_judge_weight=_opt_float(args, "default_judge_weight"),
            plan_revision_weight=_opt_float(args, "plan_revision_weight"),
            task_failure_weight=_opt_float(args, "task_failure_weight"),
            not_completed_weight=_opt_float(args, "not_completed_weight"),
            severity_weights=args.get("severity_weights"),
        )
    if op == "set_gate":
        return ops.set_gate(
            draft,
            promote_margin=_opt_float(args, "promote_margin"),
            holdout_margin=_opt_float(args, "holdout_margin"),
            holdout_entry_regression_budget=_opt_int(args, "holdout_entry_regression_budget"),
            monotonicity=_opt_bool(args, "monotonicity"),
            monotonicity_scope=args.get("monotonicity_scope"),
            namespace_monotonicity=args.get("namespace_monotonicity"),
            block_on_containment_violation=_opt_bool(args, "block_on_containment_violation"),
            block_on_gate_contradiction=_opt_bool(args, "block_on_gate_contradiction"),
            regression_gate_enabled=_opt_bool(args, "regression_gate_enabled"),
            regression_test_command=args.get("regression_test_command"),
            regression_timeout_s=_opt_int(args, "regression_timeout_s"),
        )
    if op == "set_namespace_weights":
        return ops.set_namespace_weights(
            draft,
            namespace_weights=args.get("namespace_weights"),
            diff_complexity_weight=_opt_float(args, "diff_complexity_weight"),
            diff_complexity_ceiling=_opt_float(args, "diff_complexity_ceiling"),
        )
    if op == "set_proposer_quality":
        return ops.set_proposer_quality(
            draft,
            best_of_n=_opt_int(args, "best_of_n"),
            critique_enabled=_opt_bool(args, "critique_enabled"),
            process_exemplars=_opt_int(args, "process_exemplars"),
            recombine=_opt_bool(args, "recombine"),
            genealogy=_opt_int(args, "genealogy"),
            calibration_feedback=_opt_int(args, "calibration_feedback"),
            recombine_merge=_opt_str(args, "recombine_merge"),
        )
    if op == "set_experiment_memory":
        return ops.set_experiment_memory(draft, cross_epoch=_opt_bool(args, "cross_epoch"))
    if op == "set_telemetry_dialect":
        return ops.set_telemetry_dialect(draft, dialect=_opt_str(args, "dialect"))
    if op == "set_mutation_surface":
        raw_surface = args.get("mutation_surface")
        return ops.set_mutation_surface(
            draft,
            mutation_surface=raw_surface if isinstance(raw_surface, Mapping) else None,
        )
    if op == "set_screening":
        raw_entries = args.get("entries")
        raw_veto_only = args.get("veto_only")
        return ops.set_screening(
            draft,
            entries=int(raw_entries) if raw_entries is not None else None,
            veto_only=bool(raw_veto_only) if raw_veto_only is not None else None,
        )
    if op == "edit_board_entry":
        entry = validate_board_entry(args["entry"])
        return ops.edit_board_entry(draft, entry)
    if op == "add_board_entry":
        entry = validate_board_entry(args["entry"])
        return ops.add_board_entry(draft, entry)
    if op == "remove_board_entry":
        return ops.remove_board_entry(draft, str(args["entry_id"]))
    if op == "add_judge":
        return ops.add_judge(draft, str(args["entry_id"]), _judge_from_dict(args["judge"]))
    if op == "remove_judge":
        return ops.remove_judge(draft, str(args["entry_id"]), str(args["name"]))
    if op == "set_brief":
        return ops.set_brief(draft, str(args["text"]))
    if op == "set_board_meta":
        raw_disable = args.get("disable_drift")
        if raw_disable is not None and not isinstance(raw_disable, list):
            raise ValueError("'disable_drift' must be a list of drift-kind tokens or null")
        return ops.set_board_meta(
            draft,
            disable_drift=[str(t) for t in raw_disable] if raw_disable is not None else None,
            judge_only=_opt_bool(args, "judge_only"),
        )
    raise ValueError(f"unknown builder op {op!r}")


def _opt_int(args: dict[str, Any], key: str) -> int | None:
    """Coerce an optional integer arg (absent / null ⇒ ``None``).

    A non-integer raises :class:`ValueError` so the handler returns a
    clear 400 instead of silently mis-typing a contract knob.
    """
    raw = args.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key!r} must be an integer, got {raw!r}") from exc


def _opt_float(args: dict[str, Any], key: str) -> float | None:
    """Coerce an optional float arg (absent / null ⇒ ``None``).

    The float twin of :func:`_opt_int`, closing the same hole on the other
    half of the knobs. Were a float arg to reach an op as the RAW JSON
    value, the outcome would split by which validator the field happens to
    have. A string ``"0.5"`` lands in the contract intact for
    ``promote_margin`` and the weight scalars, whose validators never compare
    them. The same string raises an uncaught ``TypeError`` — a 500 rather
    than a 400 — for ``holdout_fraction``, whose validator does compare.
    Both are the mis-typed contract knob this coercion exists to refuse.

    A bool is rejected outright: Python floats it to 0.0/1.0 happily, so
    ``true`` would otherwise read as a silent 1.0 weight.
    """
    raw = args.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{key!r} must be a number, got {raw!r}")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key!r} must be a number, got {raw!r}") from exc


def _opt_bool(args: dict[str, Any], key: str) -> bool | None:
    """Coerce an optional boolean arg (absent / null ⇒ ``None``)."""
    raw = args.get(key)
    if raw is None:
        return None
    return bool(raw)


def _opt_str(args: dict[str, Any], key: str) -> str | None:
    """Coerce an optional string arg (absent / null ⇒ ``None``).

    A non-string raises :class:`ValueError` so the handler returns a clear
    400 instead of silently mis-typing a contract knob.
    """
    raw = args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{key!r} must be a string, got {raw!r}")
    return raw


def _runs_of(args: dict[str, Any]) -> int | None:
    """Coerce the optional ``runs`` arg of the ``preflight`` op.

    ``None`` (absent) defers to the op's default; a non-integer raises
    :class:`ValueError` so the handler returns a clear 400.
    """
    raw = args.get("runs")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"preflight 'runs' must be an integer, got {raw!r}") from exc


def _builder_vocab() -> dict[str, list[str]]:
    """The board-authoring enum vocabulary, server-derived.

    Served on ``GET /builder/config`` so the GUI's forms (entry kinds,
    expectation kinds/reads, judge modes/severities, board-level drift
    kinds) render their choices from the SAME enums the validators
    enforce — the JS never hardcodes an enum, and a new member appears in
    the forms without a client change.
    """
    from typing import get_args  # noqa: PLC0415

    from zicato.core.drift_kinds import (  # noqa: PLC0415
        GOLDFIVE_DRIFT_KINDS,
        DriftSeverity,
    )
    from zicato.core.types import (  # noqa: PLC0415
        BoardEntryKind,
        ExpectationKind,
        JudgeMode,
        OutputScope,
    )

    return {
        "kinds": list(get_args(BoardEntryKind)),
        "expectation_kinds": [m.value for m in ExpectationKind],
        "reads": [m.value for m in OutputScope],
        "judge_modes": [m.value for m in JudgeMode],
        "severities": [m.value for m in DriftSeverity],
        "drift_kinds": sorted(GOLDFIVE_DRIFT_KINDS),
    }


def _proposer_dirs(workspace_root: Path) -> list[dict[str, str]]:
    """Discover candidate proposer dirs for the builder's proposer picker.

    Scans ``<workspace_parent>/proposers/*`` — the conventional location
    next to the ``.zicato/`` dir, like the contract source files — for
    subdirectories that look like proposers: an ``agent.py`` or a
    ``skills/`` dir (the two things
    :func:`zicato.proposer.skills.resolve_proposer_spec` reads). Pure
    read plumbing: degrades to ``[]`` on any absence or filesystem
    error, never raises.
    """
    base = Path(workspace_root).parent / "proposers"
    found: list[dict[str, str]] = []
    try:
        candidates = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return []
    for candidate in candidates:
        try:
            if (candidate / "agent.py").is_file() or (candidate / "skills").is_dir():
                found.append({"name": candidate.name, "path": str(candidate)})
        except OSError:
            continue
    return found


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
        warns = ops.validate(draft, root)
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
        return JSONResponse(
            {
                **cfg.to_public_dict(),
                "chat_enabled": builder_chat_enabled(root),
                "vocab": _builder_vocab(),
            }
        )

    async def builder_draft(request: Request) -> JSONResponse:
        session = request.query_params.get("session") or _DEFAULT_SESSION
        draft = draft_store.get(session, root)
        cost = ops.estimate_cost(draft)
        warns = ops.validate(draft, root)
        diff = draft.diff_vs_live(root)
        return JSONResponse(
            {
                "session": session,
                "draft": draft.to_dict(),
                "cost": cost.to_dict(),
                "warnings": [w.to_dict() for w in warns],
                "diff": diff.to_dict(),
                "drafts": draft_store.list_drafts(),
                "proposer_dirs": _proposer_dirs(root),
            }
        )

    def _resolve_compare_draft(name: str, session: str) -> TournamentDraft:
        """Resolve a compare operand: ``"session"`` (the working draft),
        ``"live"`` (the workspace's current contract), or a named slot."""
        if name == "session":
            return draft_store.get(session, root)
        if name == "live":
            return TournamentDraft.from_workspace(root)
        slot = draft_store.slot(name)
        if slot is None:
            known = ", ".join(["session", "live", *draft_store.list_drafts()])
            raise ValueError(f"no draft named {name!r} (known: {known})")
        return slot

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
        if op in ("fork", "switch", "list_drafts", "compare", "revert_to_live", "undo"):
            # The fork/compare/undo LIFECYCLE ops act on the store's named
            # slots / undo history rather than mutating draft fields via
            # _dispatch_op, so they dispatch here. Their responses carry the
            # normal envelope (the possibly-switched active draft) plus the
            # slot list — and `compare` its keyed diff.
            try:
                extra: dict[str, Any] = {}
                if op == "fork":
                    draft = draft_store.fork(session, str(args["name"]), root)
                    patch = ops.DraftPatch(op="fork", changed={"name": str(args["name"])})
                elif op == "switch":
                    draft = draft_store.switch(session, str(args["name"]))
                    patch = ops.DraftPatch(op="switch", changed={"name": str(args["name"])})
                elif op == "list_drafts":
                    patch = ops.DraftPatch(op="list_drafts")
                elif op == "revert_to_live":
                    # Remember the pre-revert state so `undo` can bring the
                    # discarded edits back; restore IN PLACE so slot bindings
                    # stay coherent.
                    draft_store.remember(session)
                    patch = ops.restore_draft(draft, TournamentDraft.from_workspace(root))
                elif op == "undo":
                    snapshot = draft_store.pop_undo(session)
                    if snapshot is None:
                        patch = ops.DraftPatch(op="undo", note="nothing to undo")
                    else:
                        patch = ops.restore_draft(draft, snapshot, op="undo")
                else:  # compare
                    name_a = str(args["name_a"])
                    name_b = str(args["name_b"])
                    extra["compare"] = {
                        "a": name_a,
                        "b": name_b,
                        **ops.compare_drafts(
                            _resolve_compare_draft(name_a, session),
                            _resolve_compare_draft(name_b, session),
                        ),
                    }
                    patch = ops.DraftPatch(op="compare", changed={"a": name_a, "b": name_b})
            except KeyError as exc:
                return JSONResponse(
                    {"error": f"missing arg {exc.args[0]!r} for op {op!r}"}, status_code=400
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return JSONResponse(
                {
                    "draft": draft.to_dict(),
                    "patch": patch.to_dict(),
                    "cost": ops.estimate_cost(draft).to_dict(),
                    "warnings": [w.to_dict() for w in ops.validate(draft, root)],
                    "diff": draft.diff_vs_live(root).to_dict(),
                    "drafts": draft_store.list_drafts(),
                    **extra,
                }
            )
        if op == "preflight":
            # The build-time statistical pre-flight — a READ op that spends
            # the small K-draw measurement budget, so it rides the same
            # read-only guard as the write ops. Its response carries the
            # normal envelope PLUS the `preflight` result, and the warnings
            # are recomputed against the JUST-MEASURED floor so the
            # margin-vs-noise rule fires immediately.
            try:
                result = await ops.preflight(draft, root, runs=_runs_of(args))
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            floor = None
            if result.noise_floor is not None:
                floor = result.noise_floor.get("max_abs_delta")
            warns = ops.validate(
                draft,
                root,
                noise_floor_max_abs_delta=floor if isinstance(floor, int | float) else None,
            )
            return JSONResponse(
                {
                    "draft": draft.to_dict(),
                    "preflight": result.to_dict(),
                    "cost": ops.estimate_cost(draft).to_dict(),
                    "warnings": [w.to_dict() for w in warns],
                    "diff": draft.diff_vs_live(root).to_dict(),
                }
            )
        try:
            # The pre-op undo seam: snapshot the session's current state
            # (deduped, bounded) before ANY write op mutates it, so `undo`
            # can restore it. The copilot front door mirrors this in
            # BuilderToolContext.draft().
            draft_store.remember(session)
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

    async def builder_suggestions(_request: Request) -> JSONResponse:
        """The eval-suggestions inbox feed (EVAL-SYNTHESIS.md §6).

        Reads the CURRENT epoch's latest reflection's persisted
        ``suggestions.json`` (the ``reflect suggest`` output) so the board
        editor can stage a suggestion onto the draft. Fully tolerant: no epoch,
        no reflection, or no suggestions all degrade to an HONEST empty list
        (never a fabricated suggestion) plus the ``epoch_id`` / ``reflection_id``
        the Instrument-lens link needs.
        """
        return JSONResponse(_read_suggestions_feed(root))

    return {
        "builder_config": builder_config,
        "builder_draft": builder_draft,
        "builder_op": builder_op,
        "builder_apply": builder_apply,
        "builder_chat": builder_chat,
        "builder_suggestions": builder_suggestions,
    }


def _read_suggestions_feed(root: Path) -> dict[str, Any]:
    """Latest reflection's suggestions for the current epoch (tolerant, operator-side)."""
    from zicato.reflection.suggestions import read_suggestions_json  # noqa: PLC0415

    empty: dict[str, Any] = {"epoch_id": None, "reflection_id": None, "suggestions": []}
    try:
        from zicato.epoch.lifecycle import current_epoch_id  # noqa: PLC0415

        epoch_id = current_epoch_id(root)
    except Exception:  # noqa: BLE001 — a cold workspace has no epoch
        return empty
    if not epoch_id:
        return empty
    reflection_id = _latest_reflection_id(root, epoch_id)
    if reflection_id is None:
        return {"epoch_id": epoch_id, "reflection_id": None, "suggestions": []}
    return {
        "epoch_id": epoch_id,
        "reflection_id": reflection_id,
        "suggestions": read_suggestions_json(root, epoch_id, reflection_id),
    }


def _latest_reflection_id(root: Path, epoch_id: str) -> str | None:
    """The newest reflection dir carrying a ``suggestions.json`` (mint-mode aware).

    Scans ``reflections/*/suggestions.json`` DIRECTLY rather than via
    ``list_reflections`` — a ``reflect suggest`` in mint mode writes a reflection
    dir with ONLY a ``suggestions.json`` (no ``plan.json``), and the
    plan.json-keyed reflection discovery skips exactly those dirs, so the inbox
    would never see a mint-mode suggestion. Newest-first by the suggestions
    file's mtime (tiebroken by dir name, descending) so the freshest
    ``reflect suggest`` output wins.
    """
    from zicato.core.workspace import reflection_suggestions_path, reflections_dir  # noqa: PLC0415

    root_dir = reflections_dir(root, epoch_id)
    if not root_dir.is_dir():
        return None
    candidates: list[tuple[float, str]] = []
    try:
        children = list(root_dir.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.is_dir():
            continue
        path = reflection_suggestions_path(root, epoch_id, child.name)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue  # no suggestions.json in this dir
        candidates.append((mtime, child.name))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return candidates[0][1]


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
        Route("/builder/suggestions", handlers["builder_suggestions"], methods=["GET"]),
    ]


__all__ = [
    "make_builder_endpoints",
    "builder_routes",
]
