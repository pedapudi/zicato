"""Browser addresses resolved into Home, Standings and Instrument review views."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote, unquote

LENSES: tuple[str, ...] = ("home", "standings", "instrument")

BROWSER_ONLY: dict[str, str] = {
    "builder": "home",
    "settings": "home",
    "publication": "home",
    "paper": "home",
    "traces": "instrument",
    "diff": "standings",
    "mutations": "standings",
}


def segment(value: str) -> str:
    """Encode one coordinate without changing its identity."""
    return quote(value, safe="")


@dataclass(frozen=True)
class Route:
    """A navigation address and the evidence selected within its rail view."""

    lens: str = "home"
    params: dict[str, str] = field(default_factory=dict)
    unsupported: str | None = None

    def to_path(self) -> str:
        epoch = self.params.get("epoch")
        base = f"/e/{segment(epoch)}" if epoch else ""
        detail = self.params.get("detail")
        entry = self.params.get("entry")
        gen = self.params.get("gen")
        if detail in {"health", "logs"}:
            return f"{base}/{detail}"
        if detail in {"board", "boards", "evals"}:
            if entry:
                path = f"{base}/board/{segment(entry)}"
                return path + (f"/{segment(gen)}" if gen else "")
            return f"{base}/{detail}"
        if self.lens == "home":
            return base or "/"
        if self.lens == "standings":
            if gen:
                path = f"{base}/gen/{segment(gen)}"
                return path + (f"/{segment(entry)}" if entry else "")
            return f"{base}/gens"
        path = f"{base}/instrument"
        for key in ("reflection", "judge", "run_ref"):
            if self.params.get(key):
                path += f"/{segment(self.params[key])}"
        return path


def parse_route(path: str | None) -> Route:
    """Resolve a browser hash, full path or shorthand to a review view."""
    parts = [
        unquote(p)
        for p in (path or "").strip().lstrip("#").split("~", 1)[0].strip("/").split("/")
        if p
    ]
    if not parts:
        return Route()
    params = {}
    if parts[0] == "e":
        if len(parts) < 2:
            return Route()
        params["epoch"] = parts[1]
        parts = parts[2:]
    if not parts:
        return Route(params=params)
    group, *rest = parts
    if group in {"candidate", "gen", "standings", "gens"}:
        params = _with(params, "gen", rest, 0)
        if len(rest) > 1 and rest[1] == "diff":
            return Route("standings", params, "diff")
        return Route("standings", _with(params, "entry", rest, 1))
    if group in {"board", "boards", "boardstatus", "evals", "evals_health"}:
        params["detail"] = (
            "evals" if group == "evals_health" else "boards" if group == "boardstatus" else group
        )
        params = _with(params, "entry", rest, 0)
        return Route("instrument", _with(params, "gen", rest, 1))
    if group in {"health", "logs"}:
        return Route("home", {**params, "detail": group})
    if group == "instrument":
        for i, key in enumerate(("reflection", "judge", "run_ref")):
            params = _with(params, key, rest, i)
        return Route("instrument", params)
    if group in BROWSER_ONLY:
        return Route(BROWSER_ONLY[group], params, group)
    return Route("home", params)


def _with(params: dict[str, str], key: str, rest: list[str], index: int) -> dict[str, str]:
    return {**params, key: rest[index]} if len(rest) > index and rest[index] else params


__all__ = ["BROWSER_ONLY", "LENSES", "Route", "parse_route", "segment"]
