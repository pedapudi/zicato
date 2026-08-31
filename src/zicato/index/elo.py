"""A read-only Plackett--Luce rating fold over the persisted match ledger.

This module answers the design question "can tournaments generate a
candidate-strength number?" (see ``docs/design/FUNCTIONALITY-RECOMMENDATIONS.md``
§5 + ``docs/design/SELECTION-THEORY.md`` §7.1) with a pure, **read-only**
rating layer derived at *index* time from the already-persisted per-match
results. It is for **visibility** — a human-legible strength number (plus a
standard error) across the lineage — and it **never** touches the promote gate
or the selection path. Nothing here is consulted by the orchestrator while
deciding a crowning; the gate and the gauntlet stay byte-identical. The columns
keep their historical names (``elo`` / ``elo_games``, plus the additive
``elo_se``) and the ratings are still reported on the conventional Elo scale, so
downstream consumers do not have to learn a new unit.

The engine: batch maximum-likelihood Plackett--Luce
---------------------------------------------------
The rating is the maximum-likelihood Plackett--Luce fit
(:func:`zicato.selection.rating.fit_plackett_luce`) over the de-duplicated
**observations** in the ledger. Each contestant ``i`` has a latent strength
``theta_i``. Two observation shapes feed one likelihood:

* a settled **two-competitor game** (``i`` beat ``j``) — the probability ``i``
  wins is ``sigma(theta_i - theta_j)``, which is where Plackett--Luce reduces
  *exactly* to Bradley--Terry, so the pairwise ratings are unchanged from the
  earlier BT fold;
* a **racing rung group** — a survivor set ``S`` finished above a cut set ``C``,
  with the order within each block unobserved. This is the exact marginal
  Plackett--Luce likelihood over the within-``S`` orderings, so a rung with
  no single winner (its ``winner`` is ``None``; see below) still contributes
  a real ranked observation and its cut generations are rated.

The fitted strengths are mapped onto the Elo scale so a 400-point gap is the
classic 10:1 odds::

    elo    = 1500 + theta * (400 / ln 10)
    elo_se =         se    * (400 / ln 10)

Three properties this engine buys over the sequential margin-K Elo it replaced:

* **Order-independence.** A batch MLE depends only on the *tally* of wins per
  pairing, never on the sequence the games are folded in. A sequential Elo is
  path-dependent — each update is applied to the *current* rating, so the same
  multiset of games processed in a different order yields different final
  ratings. The BT fit gives one answer regardless of ordering, so a full
  ``zicato repair index`` — or any re-derivation — reproduces identical ratings with
  no reliance on a stable game-ordering pass.
* **Uncertainty.** The Fisher information yields a per-generation standard error
  (``elo_se``). The ridge prior keeps the information matrix positive-definite,
  so the SE is always finite — even for a contestant with a perfect or empty
  record, or in a disconnected component of the duel graph (two clusters that
  never played each other are each anchored to the field mean by the ridge
  prior rather than diverging).
* **Margins are ignored.** The fit uses win/loss and rank-group
  outcomes only. The margin of victory (``|delta_scalar|``) is still *extracted*
  from the ledger (:class:`EloGame` carries it) but is **not** an input to the
  rating: margins ride the *gate*, which is where the scalar magnitude belongs.
  A rating measures who beats whom; the gate measures by how much. Folding the
  margin into the rating would double-count the same evidence and let a single
  blow-out distort a visibility number.

What an "observation" is
------------------------
Two shapes, both sourced from the ``tournaments`` index rows
(:func:`zicato.index.ingest._upsert_tournament` / ``_upsert_field_tournament``):

* a settled **two-competitor game** — the winner, the loser. From:

  * a **gauntlet** crowning row (``parent`` vs ``child``, resolved from the
    row's ``decision`` / ``delta_scalar``);
  * a **non-gauntlet** crowning row's ``rounds_json`` per-match audit (one game
    per named opponent the challenger faced);
  * a **field** row's ``rounds_json`` full bracket — every settled
    two-competitor match — so challenger-vs-challenger duels that never surface
    as a crowning row are still counted.

* a **racing rung group** — a field row's ``rounds_json`` rung match persists a
  ``survivors`` set ranked above a ``cut`` set (its ``winner`` is ``None`` —
  a rung cuts, it does not crown). Each such rung is one grouped Plackett--Luce
  observation: the survivors finished above the cut arms, order within each
  block unobserved.

Games are de-duplicated by ``(tournament_id, match_id, frozenset(sides))`` and
rung groups by ``(tournament_id, rung_id)``, so the same physical observation —
which can surface in two overlapping rows — is folded exactly once.

Zero-observation generations
----------------------------
A generation that appeared in **no** observation gets **no** rating — its
``elo`` / ``elo_se`` stay NULL and it is simply absent from the fit output.
This is honest: a batch MLE has nothing to estimate a strength from with zero
observations, so there is no rating to show rather than a fabricated carry-
forward number. (The former sequential fold seeded a never-played child at its
parent's rating; the batch fit does not — a strength you never measured is not
a strength you can report.)

``elo_games`` counts the **observations a generation appeared in** — a
two-competitor game counts for its two sides, and a rung group counts once per
participant (every survivor and every cut arm). It is the evidence count behind
the rating rather than a literal duel tally. A racing rung's cut generation, which the
earlier BT fold left NULL (a set cut is not a pairwise winner), is now rated
from the rung group it appeared in.

Slice-size weighting is **unweighted** in v1: a rung run on a small
board slice is noisier evidence than one on the full board, but every
observation enters the likelihood with equal weight. The rating is
visibility-only (it never gates), so under-counting a small-slice rung's noise
costs nothing operational; a variance-aware weighting is future work (see
SELECTION-THEORY.md §7.1).

A rung whose survivor set exceeds :data:`~zicato.selection.rating.PL_MAX_SURVIVORS`
is skipped (with a debug log) rather than approximated — the exact marginal is
factorial in the survivor count and racing fields are single-digit, so the cap
is comfortable headroom.

All public functions here are pure over their inputs (a list of games + the
lineage map) — :func:`compute_elo` takes plain data and returns plain data, so
it is trivially testable and never opens a database itself.
:func:`fold_elo_into_index` is the only function that reads/writes the SQLite
index, and it only writes the additive ``generations.elo`` /
``generations.elo_se`` / ``generations.elo_games`` columns.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from zicato.selection.rating import PL_MAX_SURVIVORS, fit_plackett_luce

# ---------------------------------------------------------------------------
# Tunables (module-level so a test can reference the same constants)
# ---------------------------------------------------------------------------

#: The rating an even (zero-strength) contestant reports — the conventional Elo
#: anchor. The zero-sum-gauged BT fit centres ``theta`` at zero, so the field
#: mean sits at this anchor and a stronger-than-average generation reads above
#: it, a weaker one below.
DEFAULT_RATING: float = 1500.0

#: The logistic scale of the Elo expected-score curve (the classic 400): a
#: 400-point rating gap is 10:1 odds.
ELO_SCALE: float = 400.0

#: The theta-to-Elo conversion factor. A BT strength difference of ``ln 10``
#: (10:1 odds) maps to a 400-point Elo gap, so one strength unit is
#: ``400 / ln 10 ~= 173.7`` Elo points. Both the rating and its standard error
#: are scaled by this factor (the SE is a linear function of theta, so it scales
#: with the same constant).
ELO_PER_THETA: float = ELO_SCALE / math.log(10.0)


# ---------------------------------------------------------------------------
# Data shapes (pure inputs / outputs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EloGame:
    """One settled duel, normalised into a Bradley--Terry game.

    Fields
    ------
    epoch_id:
        The epoch the duel ran under. Retained for epoch tagging of the
        resulting rating (a generation belongs to exactly one epoch).
    tournament_id:
        The tournament row the duel belongs to (its index key). Part of the
        de-dup key.
    match_id:
        The match's stable id within the tournament (e.g. ``"rung0_m2"``,
        ``"WB-R0-0"``). Empty for a gauntlet crowning duel, which has no
        per-match audit row — the gauntlet game is keyed on its tournament id
        alone.
    winner, loser:
        The generation ids of the two sides; ``winner`` won.
    margin:
        The non-negative ``|delta_scalar|`` of the duel. It is **recorded but
        not used by the rating** — Bradley--Terry is fit on win/loss outcomes
        only, and the margin rides the gate rather than the rating (see the
        module docstring). Retained on the game so the extraction stays
        loss-lossless and a future margin-aware analytic can read it. ``0.0``
        when the source carried no usable delta.
    ran_at:
        Best-effort ISO-8601 timestamp the duel settled at. Retained for
        provenance; the batch fit is order-independent so it is not used to
        sequence the games.
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

        The same duel can surface in two index rows (a challenger's crowning
        row AND the field row's full bracket). Keying on
        ``(tournament_id, match_id, {sides})`` folds it exactly once. The
        unordered ``frozenset`` of sides makes the key independent of which row
        recorded the duel from which perspective.
        """
        return (self.tournament_id, self.match_id, frozenset((self.winner, self.loser)))


@dataclass(frozen=True, slots=True)
class RungEvent:
    """One racing-rung group observation: a survivor set ranked above a cut set.

    A racing intermediate rung persists a ``survivors`` set that carried and a
    ``cut`` set that was eliminated, with no single named ``winner`` — the
    order within each block is unobserved. It is one grouped Plackett--Luce
    observation (the survivors all finished above the cut arms). It carries no
    margin (a rung ranks by scalar; the ordering rather than any single delta is the
    evidence).

    Fields
    ------
    epoch_id:
        The epoch the rung ran under (used to tag the resulting rating).
    tournament_id:
        The field-tournament row the rung belongs to. Part of the de-dup key.
    rung_id:
        The rung's stable match id within the tournament (e.g. ``"rung0"``).
        Part of the de-dup key.
    survivors, cut:
        The generation ids that carried / were eliminated. Every survivor is
        ranked above every cut arm.
    ran_at:
        Best-effort ISO-8601 timestamp; retained for provenance (the batch fit
        is order-independent so it is not used to sequence observations).
    """

    epoch_id: str
    tournament_id: str
    rung_id: str
    survivors: tuple[str, ...]
    cut: tuple[str, ...]
    ran_at: str = ""

    def dedup_key(self) -> tuple[str, str]:
        """A stable identity for one physical rung.

        A rung's survivor/cut set can surface in more than one overlapping row
        (the assembled field record and, in principle, a re-ingest). Keying on
        ``(tournament_id, rung_id)`` folds it exactly once.
        """
        return (self.tournament_id, self.rung_id)


@dataclass(frozen=True, slots=True)
class EloRating:
    """The folded rating for one generation.

    Fields
    ------
    generation_id, epoch_id:
        The generation this rating belongs to.
    rating:
        The Plackett--Luce strength mapped onto the Elo scale
        (``1500 + theta * 400 / ln 10``).
    se:
        The standard error of the rating on the Elo scale (``se_theta *
        400 / ln 10``), from the inverse observed information of the fit. Always
        finite (the ridge prior guarantees it); larger = less certain.
    games:
        How many observations the generation appeared in — a two-competitor
        game counts for its two sides, a racing rung group counts once per
        participant. Always ``>= 1`` for a rated generation; a generation in no
        observation gets no rating at all (it is absent from the fit output).
    """

    generation_id: str
    epoch_id: str
    rating: float
    se: float
    games: int


@dataclass(frozen=True, slots=True)
class LineageNode:
    """The minimal lineage metadata the fold needs for one generation.

    Fields
    ------
    epoch_id, generation_id:
        Lineage coordinates. ``epoch_id`` is the authoritative epoch a rating is
        written under (the fold keys its UPDATE on ``(generation_id,
        epoch_id)``).
    parent_generation_id:
        The generation this one was derived from, or ``None`` for a genesis
        seed. Retained for lineage provenance; the batch fit does not seed a
        child from its parent (a strength is measured rather than inherited).
    created_at:
        Best-effort creation timestamp. Retained for provenance.
    """

    epoch_id: str
    generation_id: str
    parent_generation_id: str | None
    created_at: str = ""


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


def compute_elo(
    games: Sequence[EloGame],
    lineage: Mapping[str, LineageNode],
    rungs: Sequence[RungEvent] = (),
) -> dict[str, EloRating]:
    """Fold a ledger of games + rung groups into a per-generation rating.

    Pure and **order-independent**: the result depends only on the *set* of
    de-duplicated observations (the win/loss tally per pairing plus the ranked
    survivor/cut sets), never on the order they are passed. A re-run — or a full
    ``zicato repair index`` — reproduces identical ratings with no reliance on an
    observation-ordering pass. This is the property the batch MLE buys over the
    sequential margin-K Elo it replaced (that fold's rating was path-dependent:
    each update landed on the running rating, so a different fold order gave a
    different answer).

    Parameters
    ----------
    games:
        Every settled two-competitor duel, already normalised to
        :class:`EloGame`. The function de-duplicates overlapping sources
        internally (see :meth:`EloGame.dedup_key`), so a caller may pass the
        union of the crowning-row games and the field-row games without
        pre-filtering. Self-matches / empty-sided games are ignored.
    lineage:
        Every generation keyed by id, carrying its ``epoch_id`` (used to tag the
        resulting rating). A generation that appears in an observation but is
        absent from ``lineage`` is still rated — its epoch is taken from the
        observation.
    rungs:
        Every racing-rung group observation, normalised to :class:`RungEvent`
        (survivor set ranked above cut set). De-duplicated internally by
        :meth:`RungEvent.dedup_key`. A rung whose survivor set exceeds
        :data:`~zicato.selection.rating.PL_MAX_SURVIVORS` is skipped (with a
        debug log inside the fit) rather than approximated.

    Returns
    -------
    dict
        ``{generation_id: EloRating}`` for **every generation that appeared in
        at least one observation** (a two-competitor game OR a rung group). A
        generation that appeared in none is absent (it has no measured strength;
        the fold writes NULL for it). The strengths are zero-sum-gauged by the
        fit, so the field mean sits at :data:`DEFAULT_RATING`. ``games`` on each
        rating is the count of observations that generation appeared in — a
        game counts for its two sides, a rung group counts once per participant.
    """
    epoch_of: dict[str, str] = {gid: node.epoch_id for gid, node in lineage.items()}

    deduped = _dedup_games(games)
    deduped_rungs = _dedup_rungs(rungs)

    observations: list[tuple[Sequence[str], Sequence[str]]] = []
    game_count: dict[str, int] = {}
    for game in deduped:
        if not game.winner or not game.loser or game.winner == game.loser:
            continue
        observations.append(((game.winner,), (game.loser,)))
        game_count[game.winner] = game_count.get(game.winner, 0) + 1
        game_count[game.loser] = game_count.get(game.loser, 0) + 1
        # Best-effort epoch tag for a generation absent from lineage: the first
        # observation it appears in wins.
        epoch_of.setdefault(game.winner, game.epoch_id)
        epoch_of.setdefault(game.loser, game.epoch_id)

    for rung in deduped_rungs:
        survivors = tuple(s for s in rung.survivors if s)
        cut = tuple(c for c in rung.cut if c)
        if not survivors or not cut or set(survivors) & set(cut):
            continue  # no comparative information / contradictory
        observations.append((survivors, cut))
        # ``elo_games`` counts observations a generation APPEARED in; a rung
        # group counts once per participant. An over-cap rung is skipped by the
        # fit, so only count participants of rungs the fit will keep.
        if len(survivors) <= PL_MAX_SURVIVORS:
            for pid in (*survivors, *cut):
                game_count[pid] = game_count.get(pid, 0) + 1
                epoch_of.setdefault(pid, rung.epoch_id)

    if not observations:
        return {}

    # The batch MLE. The ridge prior (default 1.0) keeps the fit identifiable
    # and finite even for a perfect/empty record or a disconnected graph, so
    # every observed generation gets a finite (theta, se). Plackett--Luce
    # reduces exactly to Bradley--Terry on the pairwise (singleton-group)
    # observations, so pairwise-only ledgers are byte-unchanged from the BT fold.
    fitted = fit_plackett_luce(observations)

    out: dict[str, EloRating] = {}
    for gid, (theta, se) in fitted.items():
        out[gid] = EloRating(
            generation_id=gid,
            epoch_id=epoch_of.get(gid, ""),
            rating=DEFAULT_RATING + theta * ELO_PER_THETA,
            se=se * ELO_PER_THETA,
            games=game_count.get(gid, 0),
        )
    return out


def _dedup_games(games: Sequence[EloGame]) -> list[EloGame]:
    """Drop duplicate physical matches, keeping the first occurrence.

    Two index rows can describe the same duel (a crowning row and the field
    bracket). The first seen — by the order the caller assembled the union —
    wins; the batch fit is order-independent, so which copy survives does not
    affect the result.
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


def _dedup_rungs(rungs: Sequence[RungEvent]) -> list[RungEvent]:
    """Drop duplicate rung observations, keeping the first occurrence.

    A rung's survivor/cut set can surface in more than one overlapping row
    (or a re-ingest of the same round). Keying on ``(tournament_id, rung_id)``
    folds it exactly once; the batch fit is order-independent, so which copy
    survives does not affect the result.
    """
    seen: set[tuple[str, str]] = set()
    out: list[RungEvent] = []
    for r in rungs:
        key = r.dedup_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


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

    A racing intermediate rung that persists a survivor/cut *set* with no single
    named ``winner`` yields no pairwise game HERE — it is a grouped ranking, not
    a two-competitor duel, so it is extracted separately as a
    :class:`RungEvent` by :func:`_rungs_from_field_row` and folded through the
    Plackett--Luce likelihood.

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


def _rungs_from_row(row: Mapping[str, Any]) -> list[RungEvent]:
    """Extract racing-rung group observations from one ``tournaments`` row.

    A field-level row's ``rounds_json`` bracket persists each racing rung as a
    match carrying a ``survivors`` set ranked above a ``cut`` set (its
    ``winner`` is ``None`` — a rung cuts rather than crowns). Every such match
    is one :class:`RungEvent` (a survivor-over-cut partial order). A match with
    no ``survivors`` / ``cut`` field, or with only one side populated, carries
    no comparative information and yields nothing — as does a per-challenger
    crowning row, whose audit ``rounds_json`` has no bracket ``matches``.

    De-duplication of a rung that surfaces in more than one row is left to
    :func:`compute_elo` (via :meth:`RungEvent.dedup_key`).
    """
    epoch_id = str(row.get("epoch_id") or "")
    tournament_id = str(row.get("tournament_id") or "")
    ran_at = str(row.get("ran_at") or "")
    rounds = _safe_json(row.get("rounds_json"))
    if not isinstance(rounds, list):
        return []
    rungs: list[RungEvent] = []
    for rnd in rounds:
        if not isinstance(rnd, dict):
            continue
        for m in rnd.get("matches") or []:
            if not isinstance(m, dict):
                continue
            surv_raw = m.get("survivors")
            cut_raw = m.get("cut")
            if not isinstance(surv_raw, list) or not isinstance(cut_raw, list):
                continue  # not a rung group (a two-competitor match / gate)
            # Dedup within each side so a corrupt row with a repeated entry
            # is capped and counted on the same effective set the fit keeps
            # (a generation must never end up rated with zero games).
            survivors = tuple(dict.fromkeys(str(s) for s in surv_raw if s))
            cut = tuple(dict.fromkeys(str(c) for c in cut_raw if c))
            if not survivors or not cut:
                continue  # everyone carried / everyone cut — no ranking signal
            rungs.append(
                RungEvent(
                    epoch_id=epoch_id,
                    tournament_id=tournament_id,
                    # Positional fallback: two malformed rungs both missing
                    # match_id must not collide into one dedup key.
                    rung_id=str(m.get("match_id") or f"rung@{len(rungs)}"),
                    survivors=survivors,
                    cut=cut,
                    ran_at=ran_at,
                )
            )
    return rungs


def games_from_tournament_rows(rows: Sequence[Mapping[str, Any]]) -> list[EloGame]:
    """Normalise every ``tournaments`` index row into Elo games.

    The union of the per-challenger crowning games and the field-bracket
    games across every row. De-duplication of the overlap is left to
    :func:`compute_elo` (via :meth:`EloGame.dedup_key`), so this function
    can emit both sources freely. Racing rung groups are NOT games; extract
    them with :func:`rungs_from_tournament_rows`.
    """
    games: list[EloGame] = []
    for row in rows:
        games.extend(_games_from_crowning_row(row))
        games.extend(_games_from_field_row(row))
    return games


def rungs_from_tournament_rows(rows: Sequence[Mapping[str, Any]]) -> list[RungEvent]:
    """Normalise every ``tournaments`` index row into racing-rung group events.

    The union of the rung groups across every row (only field-bracket rows
    carry them; a crowning row's audit yields none). De-duplication of the
    overlap is left to :func:`compute_elo` (via :meth:`RungEvent.dedup_key`).
    """
    rungs: list[RungEvent] = []
    for row in rows:
        rungs.extend(_rungs_from_row(row))
    return rungs


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
    ``NULL`` so an index at an earlier schema version still folds, yielding
    fewer games.
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
    """Compute the BT rating over the index's match ledger and write it back.

    Reads every ``tournaments`` row + the ``generations`` lineage off the
    open connection, folds the ratings (:func:`compute_elo`), and writes
    each rated generation's ``elo`` / ``elo_se`` / ``elo_games`` columns.
    **Read-only with respect to the ledger** — it only ever updates the three
    additive rating columns; it never touches a decision, a loss, or any other
    column, and nothing gate-side ever reads them back (Elo is for visibility,
    never the gate).

    Idempotent + order-independent: a re-run (or a full ``zicato repair index``,
    which calls this after the tournaments are ingested) recomputes the same
    ratings and rewrites the same cells. Returns the computed ratings for the
    caller to inspect / log.

    Schema tolerance: the ``elo`` / ``elo_games`` columns land in schema v10 and
    ``elo_se`` in v12. On a stale schema missing ``elo`` / ``elo_games`` the
    fold writes nothing (returns empty); on a v10/v11 index that has ``elo`` but
    not yet ``elo_se`` it writes the two older columns and skips the SE (the
    same additive guard the reader honours). A generation that played no game is
    left NULL (it is absent from the fit output).

    Does **not** commit — the caller owns the transaction (the rebuild
    path commits once at the end; ``reindex`` commits after the fold).
    """
    gen_cols = {r[1] for r in conn.execute("PRAGMA table_info(generations)")}
    if "elo" not in gen_cols or "elo_games" not in gen_cols:
        # The additive columns are missing (a pre-fold schema). Nothing to
        # write; an apply_schema / migration adds them. Return empty so a
        # caller on a stale schema degrades quietly.
        return {}
    has_se = "elo_se" in gen_cols

    rows = _read_tournament_rows(conn)
    lineage = _read_lineage(conn)
    games = games_from_tournament_rows(rows)
    rungs = rungs_from_tournament_rows(rows)
    ratings = compute_elo(games, lineage, rungs)

    for gid, rating in ratings.items():
        node = lineage.get(gid)
        epoch_id = node.epoch_id if node is not None else rating.epoch_id
        # Only write a row that exists; a game can reference a generation
        # with no ``generations`` row (rare — a deleted gen dir), which we
        # skip rather than insert a thin orphan.
        if has_se:
            conn.execute(
                "UPDATE generations SET elo = ?, elo_se = ?, elo_games = ? "
                "WHERE generation_id = ? AND epoch_id = ?",
                (float(rating.rating), float(rating.se), int(rating.games), gid, epoch_id),
            )
        else:
            # A v10/v11 index that has ``elo`` but not yet the v12 ``elo_se``
            # column: write the two older columns and skip the SE.
            conn.execute(
                "UPDATE generations SET elo = ?, elo_games = ? "
                "WHERE generation_id = ? AND epoch_id = ?",
                (float(rating.rating), int(rating.games), gid, epoch_id),
            )
    return ratings


__all__ = [
    "DEFAULT_RATING",
    "ELO_SCALE",
    "ELO_PER_THETA",
    "EloGame",
    "EloRating",
    "LineageNode",
    "RungEvent",
    "compute_elo",
    "games_from_tournament_rows",
    "rungs_from_tournament_rows",
    "fold_elo_into_index",
]
