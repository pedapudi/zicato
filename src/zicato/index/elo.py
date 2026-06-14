"""A read-only Elo analytics fold over the persisted match ledger.

This module answers the design question "can tournaments generate Elo?"
(see ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md`` §5) with a pure,
**read-only** rating layer derived at *index* time from the
already-persisted per-match results. It is for **visibility** — a
human-legible candidate-strength number across the lineage — and it
**never** touches the promote gate or the selection path. Nothing here
is consulted by the orchestrator while deciding a crowning; the gate and
the gauntlet stay byte-identical.

What an Elo "game" is
---------------------
Every settled duel is one Elo game: the winner scores ``1``, the loser
``0``. Games are processed in a stable order — by ``match_id`` /
``ran_at`` within a tournament, with tournaments taken in epoch order —
so the fold is deterministic and a full ``zicato reindex`` reproduces
the same ratings.

The games are sourced from the ``tournaments`` index rows
(:func:`zicato.index.ingest._upsert_tournament` /
``_upsert_field_tournament``), which already carry the complete pairwise
ledger:

* a **gauntlet** crowning row is one game (``parent`` vs ``child``,
  resolved from the row's ``decision`` / ``delta_scalar``);
* a **non-gauntlet** crowning row's ``rounds_json`` carries the
  challenger's per-match audit (one game per opponent it faced);
* a **field** row's ``rounds_json`` carries the full bracket — every
  settled two-competitor match — so challenger-vs-challenger duels that
  never surface as a crowning row are still counted.

Games are de-duplicated across these overlapping sources by a stable key
so the same physical match is folded exactly once.

Two refinements the data supports
---------------------------------
* **Margin-of-victory K-weighting** — the effective K is scaled by the
  normalised ``|delta_scalar|`` of the duel, so a blow-out moves the
  rating more than a photo finish (recovering the margin Copeland throws
  away).
* **Provisional-K decay** — a generation's first few games use a larger
  K so a brand-new candidate's rating converges quickly, then settles to
  the stable K.

Cross-epoch carry-forward
-------------------------
A new generation's rating is **seeded at its parent's current rating** —
a child starts as strong as the parent it was derived from — anchored on
the champion. Because a contract roll changes the rules, an epoch-roll
boundary is **flagged**: the parent's rating is carried across the roll
as a *prior*, never treated as a candidate jump (a duel measured under a
new contract is not commensurable with one measured under the old).

All public functions here are pure over their inputs (a list of games +
the lineage map) — :func:`compute_elo` takes plain data and returns plain
data, so it is trivially testable and never opens a database itself.
:func:`fold_elo_into_index` is the only function that reads/writes the
SQLite index, and it only writes the additive ``generations.elo`` /
``generations.elo_games`` columns.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Tunables (module-level so a test can reference the same constants)
# ---------------------------------------------------------------------------

#: The rating every un-seeded generation starts from (the conventional
#: Elo anchor). A genesis ``v0`` seed with no parent begins here.
DEFAULT_RATING: float = 1500.0

#: The stable per-game K-factor, used once a generation is past its
#: provisional window. Larger K = faster-moving (noisier) rating.
BASE_K: float = 24.0

#: The provisional K-factor for a generation's first few games. A new
#: candidate has a high-variance rating, so its early games move it more
#: so it converges to its true strength quickly.
PROVISIONAL_K: float = 48.0

#: How many of a generation's first games use the provisional K before it
#: decays to :data:`BASE_K`. The decay is linear across this window.
PROVISIONAL_GAMES: int = 5

#: The ``|delta_scalar|`` at which the margin multiplier saturates at its
#: maximum. A duel decided by this margin or more gets the full margin
#: weight; smaller margins scale down toward :data:`MARGIN_MIN_MULT`.
MARGIN_SATURATION: float = 1.0

#: The margin multiplier floor — a razor-thin (or unknown) margin still
#: contributes at least this fraction of K, so a near-tie is not a no-op.
MARGIN_MIN_MULT: float = 0.5

#: The margin multiplier ceiling — a blow-out contributes at most this
#: multiple of K, capping how far one lopsided duel can move a rating.
MARGIN_MAX_MULT: float = 1.5

#: The logistic scale of the Elo expected-score curve (the classic 400).
ELO_SCALE: float = 400.0


# ---------------------------------------------------------------------------
# Data shapes (pure inputs / outputs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EloGame:
    """One settled duel, normalised into an Elo game.

    Fields
    ------
    epoch_id:
        The epoch the duel ran under. Used to order games and to detect
        epoch-roll boundaries for the carry-forward prior.
    tournament_id:
        The tournament row the duel belongs to (its index key). Part of
        the stable processing order and the de-dup key.
    match_id:
        The match's stable id within the tournament (e.g. ``"rung0_m2"``,
        ``"WB-R0-0"``). Empty for a gauntlet crowning duel, which has no
        per-match audit row — the gauntlet game is keyed on its
        tournament id alone.
    winner, loser:
        The generation ids of the two sides; ``winner`` scored 1.
    margin:
        The non-negative ``|delta_scalar|`` of the duel — the absolute
        scalar gap between the sides. ``0.0`` when the source carried no
        usable delta (the margin multiplier then floors at
        :data:`MARGIN_MIN_MULT`).
    ran_at:
        Best-effort ISO-8601 timestamp the duel settled at; used only as
        a tiebreak in the stable ordering. Empty when unknown.
    """

    epoch_id: str
    tournament_id: str
    match_id: str
    winner: str
    loser: str
    margin: float = 0.0
    ran_at: str = ""

    def dedup_key(self) -> tuple[str, str, frozenset[str]]:
        """A stable identity for one physical match.

        The same duel can surface in two index rows (a challenger's
        crowning row AND the field row's full bracket). Keying on
        ``(tournament_id, match_id, {sides})`` folds it exactly once.
        The unordered ``frozenset`` of sides makes the key independent of
        which row recorded the duel from which perspective.
        """
        return (self.tournament_id, self.match_id, frozenset((self.winner, self.loser)))


@dataclass(frozen=True, slots=True)
class EloRating:
    """The folded rating for one generation.

    Fields
    ------
    generation_id, epoch_id:
        The generation this rating belongs to.
    rating:
        The current Elo after every game the generation played (and its
        carry-forward seed, if it played none).
    games:
        How many games the generation actually played. ``0`` for a
        generation that only inherited its parent's rating (e.g. a
        never-challenged seed), which still gets a ``rating`` via the
        carry-forward prior.
    """

    generation_id: str
    epoch_id: str
    rating: float
    games: int


@dataclass(frozen=True, slots=True)
class LineageNode:
    """The minimal lineage metadata the fold needs for one generation.

    Fields
    ------
    epoch_id, generation_id:
        Lineage coordinates.
    parent_generation_id:
        The generation this one was derived from (its rating seed), or
        ``None`` for a genesis seed.
    created_at:
        Best-effort creation timestamp; used to order generations within
        an epoch so a parent is always seeded before its children.
    """

    epoch_id: str
    generation_id: str
    parent_generation_id: str | None
    created_at: str = ""


@dataclass
class _Player:
    """Mutable per-generation rating state during the fold."""

    rating: float
    games: int = 0
    seeded: bool = False
    epoch_id: str = ""
    history: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The pure Elo update
# ---------------------------------------------------------------------------


def _expected_score(rating_a: float, rating_b: float) -> float:
    """The classic logistic expected score of A against B."""
    return 1.0 / (1.0 + math.pow(10.0, (rating_b - rating_a) / ELO_SCALE))


def _margin_multiplier(margin: float) -> float:
    """Scale K by the normalised duel margin.

    A larger ``|delta_scalar|`` (a more decisive win) moves the rating
    more. The raw margin is normalised against :data:`MARGIN_SATURATION`
    and clamped into ``[MARGIN_MIN_MULT, MARGIN_MAX_MULT]`` so one
    blow-out cannot run away with the rating and a near-tie still counts.
    A non-finite or negative margin degrades to the floor.
    """
    if not math.isfinite(margin) or margin <= 0.0 or MARGIN_SATURATION <= 0.0:
        return MARGIN_MIN_MULT
    frac = min(margin / MARGIN_SATURATION, 1.0)
    mult = MARGIN_MIN_MULT + frac * (MARGIN_MAX_MULT - MARGIN_MIN_MULT)
    return max(MARGIN_MIN_MULT, min(MARGIN_MAX_MULT, mult))


def _provisional_k(games_played: int) -> float:
    """The K-factor for a generation that has played ``games_played`` games.

    Decays linearly from :data:`PROVISIONAL_K` (the generation's first
    game) to :data:`BASE_K` once it is past :data:`PROVISIONAL_GAMES`.
    The argument is the count of games already played *before* this one,
    so the very first game (``games_played == 0``) gets the full
    provisional K.
    """
    if PROVISIONAL_GAMES <= 0 or games_played >= PROVISIONAL_GAMES:
        return BASE_K
    # Linear decay across the provisional window.
    frac = games_played / PROVISIONAL_GAMES
    return PROVISIONAL_K + frac * (BASE_K - PROVISIONAL_K)


def _k_for(player: _Player, margin: float) -> float:
    """The effective K for ``player`` in a duel of the given margin.

    Combines the provisional-decay K (by how many games the player has
    already played) with the margin multiplier.
    """
    return _provisional_k(player.games) * _margin_multiplier(margin)


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


def compute_elo(
    games: Sequence[EloGame],
    lineage: Mapping[str, LineageNode],
) -> dict[str, EloRating]:
    """Fold a ledger of games into a per-generation Elo rating.

    Pure: depends only on its two arguments and is deterministic for a
    fixed input. The processing order is by epoch (in lineage-creation
    order), then by the stable per-game key (``tournament_id`` /
    ``match_id`` / ``ran_at``), so a re-run — or a full
    ``zicato reindex`` — reproduces identical ratings.

    Parameters
    ----------
    games:
        Every settled duel, already normalised to :class:`EloGame`. The
        function de-duplicates overlapping sources internally
        (see :meth:`EloGame.dedup_key`), so a caller may pass the union of
        the crowning-row games and the field-row games without
        pre-filtering.
    lineage:
        Every generation keyed by id, carrying its ``parent_generation_id``
        (the rating seed) and ``created_at`` (the seeding order). A
        generation that plays a game but is absent from ``lineage`` is
        still rated — it simply has no carry-forward prior and starts at
        :data:`DEFAULT_RATING`.

    Returns
    -------
    dict
        ``{generation_id: EloRating}`` for every generation that either
        played a game or appears in ``lineage`` (so a never-challenged
        champion still surfaces with its carried-forward prior).

    Cross-epoch carry-forward + epoch-roll flagging
    -----------------------------------------------
    A generation's rating is seeded — once, lazily, the first time it is
    touched — at its parent's *current* rating, anchored on the champion
    lineage. This carries strength across an epoch roll as a **prior**:
    the seed is the parent's rating regardless of whether the roll
    happened, so a contract roll never manifests as a spurious rating
    jump. The seed is a prior only; the generation's own games then move
    it from there under the new contract's rules.
    """
    players: dict[str, _Player] = {}

    # Resolve a generation's epoch from lineage (best-effort) so a player
    # created lazily from a game still records its epoch.
    epoch_of: dict[str, str] = {gid: node.epoch_id for gid, node in lineage.items()}

    # Cycle guard for the recursive parent seed walk (a malformed lineage
    # must not infinite-loop). Defined before ``_seed`` so the closure
    # binds to the live set.
    _seeding_in_progress: set[str] = set()

    def _seed(gid: str) -> _Player:
        """Return ``gid``'s player, lazily seeding it from its parent.

        The seed walks up ``parent_generation_id`` to the nearest ancestor
        that already has a rating (recursively seeding ancestors first),
        so a child inherits the champion lineage's strength as a prior.
        A generation with no resolvable parent starts at
        :data:`DEFAULT_RATING`. Cycle-guarded against a malformed lineage.
        """
        existing = players.get(gid)
        if existing is not None and existing.seeded:
            return existing
        node = lineage.get(gid)
        seed_rating = DEFAULT_RATING
        if node is not None and node.parent_generation_id:
            parent = node.parent_generation_id
            if parent != gid and parent not in _seeding_in_progress:
                _seeding_in_progress.add(parent)
                try:
                    parent_player = _seed(parent)
                    seed_rating = parent_player.rating
                finally:
                    _seeding_in_progress.discard(parent)
        player = players.get(gid)
        if player is None:
            player = _Player(
                rating=seed_rating,
                epoch_id=epoch_of.get(gid, node.epoch_id if node else ""),
            )
            players[gid] = player
        else:
            # A player created by an earlier game touch but not yet seeded
            # from lineage: adopt the carry-forward prior now.
            player.rating = seed_rating
        player.seeded = True
        return player

    # Seed every lineage generation in creation order so a parent's prior
    # is available before its children (the carry-forward anchor). A child
    # whose game runs before this seed pass still works — _seed is lazy —
    # but pre-seeding keeps a never-played champion in the output.
    for gid in _lineage_order(lineage):
        _seed(gid)

    # De-duplicate, then order the games deterministically.
    deduped = _dedup_games(games)
    ordered = _order_games(deduped, lineage)

    for game in ordered:
        if not game.winner or not game.loser or game.winner == game.loser:
            continue
        w = _seed(game.winner)
        loser = _seed(game.loser)
        if w.epoch_id == "":
            w.epoch_id = game.epoch_id
        if loser.epoch_id == "":
            loser.epoch_id = game.epoch_id

        exp_w = _expected_score(w.rating, loser.rating)
        exp_l = 1.0 - exp_w
        k_w = _k_for(w, game.margin)
        k_l = _k_for(loser, game.margin)
        w.rating += k_w * (1.0 - exp_w)
        loser.rating += k_l * (0.0 - exp_l)
        w.games += 1
        loser.games += 1

    # Carry-forward for never-played generations. A generation that played
    # no game (a never-challenged champion, a brand-new leaf) inherits its
    # nearest ancestor's FINAL rating — its strength prior — rather than the
    # default anchor. The pre-seed pass above ran before any games folded,
    # so a zero-game player still holds its initial prior; refresh it now
    # from the parent's settled rating, in lineage order so an ancestor
    # finalises before its descendants (an epoch-roll parent finalises in
    # its own earlier epoch, so the prior carries across the roll without a
    # spurious jump). Players that actually played keep their game-derived
    # rating untouched.
    for gid in _lineage_order(lineage):
        player = players.get(gid)
        if player is None or player.games > 0:
            continue
        node = lineage.get(gid)
        if node is None or not node.parent_generation_id:
            continue
        ancestor: str | None = node.parent_generation_id
        guard: set[str] = set()
        while ancestor and ancestor not in guard:
            guard.add(ancestor)
            anc_player = players.get(ancestor)
            if anc_player is not None:
                player.rating = anc_player.rating
                break
            anc_node = lineage.get(ancestor)
            ancestor = anc_node.parent_generation_id if anc_node else None

    out: dict[str, EloRating] = {}
    for gid, player in players.items():
        out[gid] = EloRating(
            generation_id=gid,
            epoch_id=player.epoch_id or epoch_of.get(gid, ""),
            rating=player.rating,
            games=player.games,
        )
    return out


def _dedup_games(games: Sequence[EloGame]) -> list[EloGame]:
    """Drop duplicate physical matches, keeping the first occurrence.

    Two index rows can describe the same duel (a crowning row and the
    field bracket). The first seen — by the order the caller assembled
    the union — wins; the ordering pass re-sorts deterministically
    afterward, so which copy survives does not affect the fold.
    """
    seen: set[tuple[str, str, frozenset[str]]] = set()
    out: list[EloGame] = []
    for g in games:
        key = g.dedup_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(g)
    return out


def _lineage_order(lineage: Mapping[str, LineageNode]) -> list[str]:
    """Generation ids in a stable seeding order.

    Sorted by ``(epoch_id, created_at, generation_id)`` so a parent is
    seeded before its children within an epoch and epochs are taken in a
    stable order. A parent in an earlier epoch is always seeded first
    because epochs sort ahead.
    """
    return sorted(
        lineage,
        key=lambda gid: (
            lineage[gid].epoch_id,
            lineage[gid].created_at or "",
            gid,
        ),
    )


def _order_games(games: Sequence[EloGame], lineage: Mapping[str, LineageNode]) -> list[EloGame]:
    """Order games deterministically: epoch order, then match/ran-at order.

    Epochs are taken in the order their generations were created (so an
    earlier epoch's duels fold before a later epoch's), then within an
    epoch by ``(ran_at, tournament_id, match_id)`` — a stable,
    reproducible key that does not depend on dict iteration order.
    """
    # Rank each epoch by the earliest creation timestamp of any generation
    # in it, so epochs fold in lineage order. Epochs unknown to lineage
    # sort last, by id.
    epoch_rank: dict[str, tuple[int, str]] = {}
    for node in lineage.values():
        prev = epoch_rank.get(node.epoch_id)
        cand = (0, node.created_at or "~")
        if prev is None or cand < prev:
            epoch_rank[node.epoch_id] = cand

    def _epoch_key(epoch_id: str) -> tuple[int, str]:
        return epoch_rank.get(epoch_id, (1, epoch_id))

    return sorted(
        games,
        key=lambda g: (
            _epoch_key(g.epoch_id),
            g.epoch_id,
            g.ran_at or "",
            g.tournament_id,
            g.match_id,
        ),
    )


# ---------------------------------------------------------------------------
# Reading games out of the index ``tournaments`` rows
# ---------------------------------------------------------------------------


def _safe_json(raw: Any) -> Any:
    """Parse a JSON column tolerantly; ``None`` on any failure."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _games_from_crowning_row(row: Mapping[str, Any]) -> list[EloGame]:
    """Extract Elo games from one per-challenger crowning ``tournaments`` row.

    Two cases:

    * **Gauntlet** (empty / absent ``rounds_json``) — the single crowning
      duel is described by the top-level columns: the child wins iff the
      decision is ``"promoted"``; otherwise the parent (incumbent) holds
      and wins. The margin is ``|delta_scalar|``.
    * **Non-gauntlet** — each item in ``rounds_json``
      (``{match_id, opponent, won, delta_scalar}``) is the challenger's
      audit of one match it played; ``won`` says whether the child or the
      opponent won, and ``|delta_scalar|`` is the margin.

    A field-level row (empty ``parent_generation_id`` /
    ``child_generation_id``) is skipped here — it is handled by
    :func:`_games_from_field_row`.
    """
    epoch_id = str(row.get("epoch_id") or "")
    tournament_id = str(row.get("tournament_id") or "")
    parent = str(row.get("parent_generation_id") or "")
    child = str(row.get("child_generation_id") or "")
    ran_at = str(row.get("ran_at") or "")
    if not parent or not child:
        return []  # a field-level row — handled elsewhere

    rounds = _safe_json(row.get("rounds_json"))
    games: list[EloGame] = []
    if isinstance(rounds, list) and rounds:
        for item in rounds:
            if not isinstance(item, dict):
                continue
            opponent = str(item.get("opponent") or "")
            if not opponent:
                continue  # a bye / N-way rung — no two-sided game
            won = bool(item.get("won"))
            match_id = str(item.get("match_id") or "")
            margin = _abs_float(item.get("delta_scalar"))
            winner, loser = (child, opponent) if won else (opponent, child)
            games.append(
                EloGame(
                    epoch_id=epoch_id,
                    tournament_id=tournament_id,
                    match_id=match_id,
                    winner=winner,
                    loser=loser,
                    margin=margin,
                    ran_at=ran_at,
                )
            )
        return games

    # Gauntlet: one game from the top-level crowning columns.
    decision = str(row.get("decision") or "")
    delta = row.get("delta_scalar")
    margin = _abs_float(delta)
    if decision == "promoted":
        winner, loser = child, parent
    elif decision in ("rejected", "deferred"):
        winner, loser = parent, child
    else:
        # Unknown / missing decision: fall back to the delta sign. The
        # per-challenger ``delta_scalar`` is ``child - parent`` (negative =
        # child better). A zero / missing delta defaults to the incumbent
        # holding (parent wins), the historical no-improvement convention.
        if isinstance(delta, int | float) and float(delta) < 0.0:
            winner, loser = child, parent
        else:
            winner, loser = parent, child
    games.append(
        EloGame(
            epoch_id=epoch_id,
            tournament_id=tournament_id,
            match_id="",
            winner=winner,
            loser=loser,
            margin=margin,
            ran_at=ran_at,
        )
    )
    return games


def _games_from_field_row(row: Mapping[str, Any]) -> list[EloGame]:
    """Extract Elo games from one field-level ``tournaments`` row.

    A field row's ``rounds_json`` is the full settled bracket:
    ``[{stage_index, label, matches: [{match_id, competitors, winner,
    delta_scalar, bye, ...}]}]``. Each settled two-competitor match with a
    named ``winner`` is one game; a bye (one competitor, or ``bye=True``)
    and a still-``pending`` match contribute none. The loser is the other
    competitor; the margin is ``|delta_scalar|``.

    Only field rows are processed here (empty per-matchup
    ``parent_generation_id`` / ``child_generation_id``); a per-challenger
    crowning row is handled by :func:`_games_from_crowning_row`.
    """
    epoch_id = str(row.get("epoch_id") or "")
    tournament_id = str(row.get("tournament_id") or "")
    parent = str(row.get("parent_generation_id") or "")
    child = str(row.get("child_generation_id") or "")
    ran_at = str(row.get("ran_at") or "")
    # A field row leaves the per-matchup columns empty; if they are set this
    # is a crowning row and is handled elsewhere.
    if parent or child:
        return []

    rounds = _safe_json(row.get("rounds_json"))
    if not isinstance(rounds, list):
        return []
    games: list[EloGame] = []
    for rnd in rounds:
        if not isinstance(rnd, dict):
            continue
        for m in rnd.get("matches") or []:
            if not isinstance(m, dict):
                continue
            if m.get("bye"):
                continue
            if m.get("pending"):
                continue
            winner = str(m.get("winner") or "")
            if not winner:
                continue  # a cut/rung without a single crowned side
            competitors = [str(c) for c in (m.get("competitors") or []) if c]
            if len(competitors) != 2:
                continue  # only two-sided matches are Elo games
            loser = next((c for c in competitors if c != winner), "")
            if not loser or loser == winner:
                continue
            match_id = str(m.get("match_id") or "")
            margin = _abs_float(m.get("delta_scalar"))
            games.append(
                EloGame(
                    epoch_id=epoch_id,
                    tournament_id=tournament_id,
                    match_id=match_id,
                    winner=winner,
                    loser=loser,
                    margin=margin,
                    ran_at=ran_at,
                )
            )
    return games


def _abs_float(value: Any) -> float:
    """Coerce a possibly-missing numeric to a non-negative float magnitude."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float) and math.isfinite(float(value)):
        return abs(float(value))
    return 0.0


def games_from_tournament_rows(rows: Sequence[Mapping[str, Any]]) -> list[EloGame]:
    """Normalise every ``tournaments`` index row into Elo games.

    The union of the per-challenger crowning games and the field-bracket
    games across every row. De-duplication of the overlap is left to
    :func:`compute_elo` (via :meth:`EloGame.dedup_key`), so this function
    can emit both sources freely.
    """
    games: list[EloGame] = []
    for row in rows:
        games.extend(_games_from_crowning_row(row))
        games.extend(_games_from_field_row(row))
    return games


# ---------------------------------------------------------------------------
# The index-side fold (the only DB-touching function)
# ---------------------------------------------------------------------------

_TOURNAMENT_COLUMNS = (
    "tournament_id",
    "epoch_id",
    "parent_generation_id",
    "child_generation_id",
    "decision",
    "delta_scalar",
    "rounds_json",
    "ran_at",
)


def _read_tournament_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read the Elo-relevant columns off every ``tournaments`` row.

    Tolerant of a partially-built schema: a missing column reads as
    ``NULL`` so a legacy index still folds (it just yields fewer games).
    """
    present = {r[1] for r in conn.execute("PRAGMA table_info(tournaments)")}
    if not present:
        return []
    select_terms = [c if c in present else f"NULL AS {c}" for c in _TOURNAMENT_COLUMNS]
    sql = f"SELECT {', '.join(select_terms)} FROM tournaments"
    rows: list[dict[str, Any]] = []
    for r in conn.execute(sql):
        rows.append({col: r[i] for i, col in enumerate(_TOURNAMENT_COLUMNS)})
    return rows


def _read_lineage(conn: sqlite3.Connection) -> dict[str, LineageNode]:
    """Build the lineage map from the ``generations`` table."""
    present = {r[1] for r in conn.execute("PRAGMA table_info(generations)")}
    if not present:
        return {}
    out: dict[str, LineageNode] = {}
    for r in conn.execute(
        "SELECT epoch_id, generation_id, parent_generation_id, created_at FROM generations"
    ):
        epoch_id = str(r[0] or "")
        gid = str(r[1] or "")
        if not gid:
            continue
        parent = r[2]
        out[gid] = LineageNode(
            epoch_id=epoch_id,
            generation_id=gid,
            parent_generation_id=str(parent) if parent else None,
            created_at=str(r[3] or ""),
        )
    return out


def fold_elo_into_index(conn: sqlite3.Connection) -> dict[str, EloRating]:
    """Compute Elo over the index's match ledger and write it back.

    Reads every ``tournaments`` row + the ``generations`` lineage off the
    open connection, folds the ratings (:func:`compute_elo`), and writes
    each generation's ``elo`` / ``elo_games`` columns. **Read-only with
    respect to the ledger** — it only ever updates the two additive
    rating columns; it never touches a decision, a loss, or any other
    column.

    Idempotent: a re-run (or a full ``zicato reindex``, which calls this
    after the tournaments are ingested) recomputes the same ratings and
    rewrites the same cells. Returns the computed ratings for the caller
    to inspect / log.

    Does **not** commit — the caller owns the transaction (the rebuild
    path commits once at the end; ``reindex`` commits after the fold).
    """
    gen_cols = {r[1] for r in conn.execute("PRAGMA table_info(generations)")}
    if "elo" not in gen_cols or "elo_games" not in gen_cols:
        # The additive columns are missing (a pre-fold schema). Nothing to
        # write; an apply_schema / migration adds them. Return empty so a
        # caller on a stale schema degrades quietly.
        return {}

    rows = _read_tournament_rows(conn)
    lineage = _read_lineage(conn)
    games = games_from_tournament_rows(rows)
    ratings = compute_elo(games, lineage)

    for gid, rating in ratings.items():
        node = lineage.get(gid)
        epoch_id = node.epoch_id if node is not None else rating.epoch_id
        # Only write a row that exists; a game can reference a generation
        # with no ``generations`` row (rare — a deleted gen dir), which we
        # skip rather than insert a thin orphan.
        conn.execute(
            "UPDATE generations SET elo = ?, elo_games = ? "
            "WHERE generation_id = ? AND epoch_id = ?",
            (float(rating.rating), int(rating.games), gid, epoch_id),
        )
    return ratings


__all__ = [
    "DEFAULT_RATING",
    "BASE_K",
    "PROVISIONAL_K",
    "PROVISIONAL_GAMES",
    "EloGame",
    "EloRating",
    "LineageNode",
    "compute_elo",
    "games_from_tournament_rows",
    "fold_elo_into_index",
]
