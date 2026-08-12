"""One addressing scheme for both surfaces.

``zicato tui --view <path>`` takes the SAME path the browser's hash router
takes, so a link pasted out of a browser address bar opens the matching
terminal lens and vice versa. ``/e/<epoch>/gen/17`` is the candidate dossier in
both places; there is no second vocabulary to learn.

Shorthands exist for the common cases (``candidate/17``, ``instrument``), and
they resolve to the same :class:`Route`. Where the browser has views the TUI
does not ship (the builder, settings, publication, traces, mutations), the
route resolves to the nearest lens and records what was asked for in
:attr:`Route.unsupported`, so the status band can say "the builder stays in the
browser" instead of silently landing somewhere unrelated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import unquote

#: The lenses, in rail order. ``1``-``6`` jump to these.
LENSES: tuple[str, ...] = ("home", "standings", "candidate", "board", "instrument", "health")

#: Browser views the TUI does not render, and the lens each one lands on.
#: Named here rather than dropped: the render-conformance rule says an
#: unrendered surface is DECLARED, never silently absent.
BROWSER_ONLY: dict[str, str] = {
    "builder": "home",
    "settings": "home",
    "publication": "home",
    "traces": "instrument",
    "mutations": "candidate",
    "diff": "candidate",
}


@dataclass(frozen=True)
class Route:
    """A resolved address: which lens, and the parameters it needs."""

    lens: str = "home"
    params: dict[str, str] = field(default_factory=dict)
    unsupported: str | None = None

    def to_path(self) -> str:
        """Render the route back to its browser-hash path form."""
        epoch = self.params.get("epoch")
        base = f"/e/{epoch}" if epoch else ""
        if self.lens == "home":
            return base or "/"
        if self.lens == "standings":
            return f"{base}/gens" if base else "/gens"
        if self.lens == "candidate":
            gen = self.params.get("gen")
            return f"{base}/gen/{gen}" if gen else f"{base}/gens"
        if self.lens == "board":
            entry = self.params.get("entry")
            return f"{base}/board/{entry}" if entry else f"{base}/boards"
        if self.lens == "instrument":
            reflection = self.params.get("reflection")
            return f"{base}/instrument/{reflection}" if reflection else f"{base}/instrument"
        return "/logs"


def parse_route(path: str | None) -> Route:
    """Resolve a ``--view`` argument (or a browser hash path) to a lens.

    Accepts, interchangeably:

    * the browser hash path, with or without the leading ``#``:
      ``#/e/2026-07-01_e3/gen/17``, ``/logs``, ``/``
    * a bare lens name: ``standings``, ``instrument``
    * a lens shorthand with its parameter: ``candidate/17``, ``board/entry-4``,
      ``instrument/refl-2``
    """
    raw = (path or "").strip()
    if not raw:
        return Route()
    raw = raw.lstrip("#")
    # `~k=v` suffix params address the browser's compare target; the TUI has no
    # compare lens, so the structural path is all that is read.
    raw = raw.split("~", 1)[0]
    parts = [unquote(p) for p in raw.strip("/").split("/") if p]
    if not parts:
        return Route()

    head = parts[0]
    if head in LENSES:
        return _shorthand(head, parts[1:])
    if head in BROWSER_ONLY:
        return Route(lens=BROWSER_ONLY[head], unsupported=head)
    if head == "logs":
        return Route(lens="health")
    if head != "e":
        return Route()

    epoch = parts[1] if len(parts) > 1 else None
    if not epoch:
        return Route()
    params = {"epoch": epoch}
    group = parts[2] if len(parts) > 2 else None
    if group is None:
        return Route(lens="home", params=params)
    rest = parts[3:]
    if group == "gens":
        return Route(lens="standings", params=params)
    if group == "gen":
        if len(rest) > 1 and rest[1] == "diff":
            return Route(
                lens="candidate",
                params={**params, "gen": rest[0]},
                unsupported="diff",
            )
        return Route(lens="candidate", params=_with(params, "gen", rest, 0))
    if group in ("board", "boards", "evals"):
        return Route(lens="board", params=_with(params, "entry", rest, 0))
    if group == "instrument":
        resolved = _with(params, "reflection", rest, 0)
        resolved = _with(resolved, "judge", rest, 1)
        resolved = _with(resolved, "run_ref", rest, 2)
        return Route(lens="instrument", params=resolved)
    if group in BROWSER_ONLY:
        return Route(lens=BROWSER_ONLY[group], params=params, unsupported=group)
    return Route(lens="home", params=params)


def _shorthand(lens: str, rest: list[str]) -> Route:
    keys = {"candidate": "gen", "board": "entry", "instrument": "reflection"}
    key = keys.get(lens)
    if key and rest:
        return Route(lens=lens, params={key: rest[0]})
    return Route(lens=lens)


def _with(params: dict[str, str], key: str, rest: list[str], index: int) -> dict[str, str]:
    if len(rest) > index and rest[index]:
        return {**params, key: rest[index]}
    return params


__all__ = ["BROWSER_ONLY", "LENSES", "Route", "parse_route"]
