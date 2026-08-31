"""One addressing scheme for both surfaces.

``zicato tui --view <path>`` takes the SAME path the browser's hash router
takes, so a link pasted out of a browser address bar opens the matching
terminal lens and vice versa. ``/e/<epoch>/gen/17`` is the candidate dossier in
both places; there is no second vocabulary to learn.

Shorthands exist for the common cases (``candidate/17``, ``instrument``), and
they resolve to the same :class:`Route`. The terminal ships fewer views than
the browser: the candidate, board and health lenses are not built here, and
the builder, settings, publication and traces surfaces are browser-side
only. An address naming one of those resolves to the NEAREST lens this
build does have, and records what was asked for in
:attr:`Route.unsupported`, so the status band can say "candidate is not in
this build" rather than landing somewhere unrelated without comment.

That is the rule this module enforces: an address the operator can type
always resolves, and always admits when it could not give them what they
asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import unquote

#: The lenses v1 ships, in rail order. ``1``-``3`` jump to these.
LENSES: tuple[str, ...] = ("home", "standings", "instrument")

#: Lenses DESIGNED in docs/design/TUI.md and deferred out of v1, each mapped to
#: the shipped lens that carries the nearest evidence. Distinct from
#: :data:`BROWSER_ONLY`: these are coming, and the address already works.
DEFERRED: dict[str, str] = {
    # the dossier's nearest neighbour is the row it was opened from
    "candidate": "standings",
    "diff": "standings",
    "mutations": "standings",
    # board status + evaluation health are one deferred lens between them
    "board": "home",
    "boardstatus": "home",
    "evals": "home",
    "evals_health": "home",
    # health findings + the log tail
    "health": "home",
    "logs": "home",
}

#: Browser views that stay in the browser BY DESIGN (v1 non-goals: authoring,
#: deep trace visualisation, formatted reports), and the lens each lands on.
BROWSER_ONLY: dict[str, str] = {
    "builder": "home",
    "settings": "home",
    "publication": "home",
    "traces": "instrument",
}

#: Everything an address may resolve to that this build cannot render.
UNSHIPPED: dict[str, str] = {**DEFERRED, **BROWSER_ONLY}


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
            gen = self.params.get("gen")
            if gen:
                return f"{base}/gen/{gen}"
            return f"{base}/gens" if base else "/gens"
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
    if head in UNSHIPPED:
        # An unshipped shorthand still carries its argument to the lens that
        # lands it: `candidate/v4` puts the cursor near v4 in the standings
        # rather than dropping the operator at the top of an unrelated table.
        landed = _shorthand(UNSHIPPED[head], parts[1:])
        return Route(lens=landed.lens, params=landed.params, unsupported=head)
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
        # The candidate dossier is deferred; the address still resolves, to the
        # standings row the operator would have opened it from, and says so.
        return Route(
            lens="standings",
            params=_with(params, "gen", rest, 0),
            unsupported="diff" if len(rest) > 1 and rest[1] == "diff" else "candidate",
        )
    if group in ("board", "boards", "evals"):
        return Route(
            lens=DEFERRED["board"],
            params=_with(params, "entry", rest, 0),
            unsupported=group,
        )
    if group == "instrument":
        resolved = _with(params, "reflection", rest, 0)
        resolved = _with(resolved, "judge", rest, 1)
        resolved = _with(resolved, "run_ref", rest, 2)
        return Route(lens="instrument", params=resolved)
    if group in UNSHIPPED:
        return Route(lens=UNSHIPPED[group], params=params, unsupported=group)
    return Route(lens="home", params=params)


def _shorthand(lens: str, rest: list[str]) -> Route:
    keys = {"standings": "gen", "instrument": "reflection"}
    key = keys.get(lens)
    if key and rest:
        return Route(lens=lens, params={key: rest[0]})
    return Route(lens=lens)


def _with(params: dict[str, str], key: str, rest: list[str], index: int) -> dict[str, str]:
    if len(rest) > index and rest[index]:
        return {**params, key: rest[index]}
    return params


__all__ = ["BROWSER_ONLY", "DEFERRED", "LENSES", "UNSHIPPED", "Route", "parse_route"]
