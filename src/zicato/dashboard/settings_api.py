"""Secret-safe model-engine settings endpoints."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from zicato.models_config import PUBLIC_MODEL_ROLES, models_config_from_dict
from zicato.workspace.config_io import read_workspace_config


def make_settings_endpoints(
    workspace_root: Path,
    *,
    read_only: bool = False,
) -> dict[str, Callable[[Request], Awaitable[Response]]]:
    """Build the ``/settings/models`` GET/POST handlers bound to a workspace.

    ``read_only`` mirrors the dashboard's flag — POST returns ``403`` when
    set; the GET read stays available.
    """
    root = Path(workspace_root)

    def _load_models() -> Any:
        """Parse the ``models`` block out of ``config.json`` (defaults if absent)."""
        try:
            raw = read_workspace_config(root).raw
        except ValueError:
            raw = {}
        return models_config_from_dict(raw.get("models"))

    async def settings_models_get(_request: Request) -> JSONResponse:
        models = _load_models()
        return JSONResponse(
            {
                "models": models.to_public_dict(),
                "roles": list(PUBLIC_MODEL_ROLES),
                "rolls_epoch": False,
            }
        )

    async def settings_models_post(request: Request) -> JSONResponse:
        if read_only:
            return JSONResponse({"error": "settings are read-only"}, status_code=403)
        try:
            body = await request.body()
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError as exc:
            return JSONResponse({"error": f"invalid JSON body: {exc.msg}"}, status_code=400)
        if not isinstance(parsed, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        raw_models = parsed.get("models")
        if not isinstance(raw_models, dict):
            return JSONResponse(
                {"error": "missing 'models' object in request body"}, status_code=400
            )
        try:
            models = models_config_from_dict(raw_models)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        try:
            loaded = read_workspace_config(root)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not loaded.exists:
            return JSONResponse(
                {"error": f"workspace config not found at {loaded.path}"}, status_code=400
            )
        path = loaded.path
        current = dict(loaded.raw)

        serialised = models.to_dict()
        if serialised:
            current["models"] = serialised
        else:
            current.pop("models", None)
        path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")

        return JSONResponse(
            {
                "models": models.to_public_dict(),
                "roles": list(PUBLIC_MODEL_ROLES),
                "rolls_epoch": False,
            }
        )

    return {
        "settings_models_get": settings_models_get,
        "settings_models_post": settings_models_post,
    }


def settings_routes(
    workspace_root: Path,
    *,
    read_only: bool = False,
) -> list[Route]:
    """Return the ``/settings/models`` routes, ready to splice into the app."""
    handlers = make_settings_endpoints(workspace_root, read_only=read_only)
    return [
        Route("/settings/models", handlers["settings_models_get"], methods=["GET"]),
        Route("/settings/models", handlers["settings_models_post"], methods=["POST"]),
    ]


__all__ = ["make_settings_endpoints", "settings_routes"]
