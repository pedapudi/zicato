"""Secret-safe model-engine settings endpoints."""

from __future__ import annotations

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
) -> dict[str, Callable[[Request], Awaitable[Response]]]:
    """Read configured engines without exposing credentials."""
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

    return {
        "settings_models_get": settings_models_get,
    }


def settings_routes(
    workspace_root: Path,
) -> list[Route]:
    """Return the ``/settings/models`` routes, ready to splice into the app."""
    handlers = make_settings_endpoints(workspace_root)
    return [
        Route("/settings/models", handlers["settings_models_get"], methods=["GET"]),
    ]


__all__ = ["make_settings_endpoints", "settings_routes"]
