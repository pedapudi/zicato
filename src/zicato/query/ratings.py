"""ratings — the best-effort read of the visibility rating triple.

One shared helper for every reader that decorates a payload with the
index-derived Bradley--Terry rating (``generations.elo`` / ``elo_se`` /
``elo_games``; schema v10 + v12): the lineage/gens feed and the tournament
standings. The rating is **visibility-only**: it never gates promotion.
This read is **best-effort by contract**: an absent index, a cold or stale
schema with missing columns, or any SQLite error yields an empty map. Every
consumer then attaches the null triple, so no payload fails because
analytics have not been derived yet.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from zicato.query._sqlite import _IndexAbsent, open_index_ro
from zicato.query.paths import WorkspacePaths

#: The three wire fields, in payload order, each in one snake_case spelling.
RATING_FIELDS: tuple[str, str, str] = ("elo", "elo_se", "elo_games")


def rating_by_generation(
    paths: WorkspacePaths, epoch_id: str | None = None
) -> dict[tuple[str, str], dict[str, Any]]:
    """The rating triple per ``(epoch_id, generation_id)``, best-effort.

    Returns ``{(epoch_id, generation_id): {"elo", "elo_se", "elo_games"}}``
    for every indexed generation (scoped to ``epoch_id`` when given). Values
    are ``None`` where the index has not derived them (a pre-v12 file's
    ``elo_se``, or a generation that never played a settled duel). An
    absent / unreadable index — or a pre-v10 schema with no rating columns
    at all — returns ``{}`` so the caller attaches the null triple to every
    row. The read degrades and never raises.
    """
    try:
        with open_index_ro(paths.index_db) as conn:
            try:
                present = {r[1] for r in conn.execute("PRAGMA table_info(generations)")}
            except sqlite3.Error:
                return {}
            if "elo" not in present:
                return {}  # pre-v10 index: no rating columns at all
            terms = ["epoch_id", "generation_id"] + [
                col if col in present else f"NULL AS {col}" for col in RATING_FIELDS
            ]
            sql = f"SELECT {', '.join(terms)} FROM generations"
            params: tuple[Any, ...] = ()
            if epoch_id is not None:
                sql += " WHERE epoch_id = ?"
                params = (epoch_id,)
            out: dict[tuple[str, str], dict[str, Any]] = {}
            for row in conn.execute(sql, params):
                eid = str(row["epoch_id"] or "")
                gid = str(row["generation_id"] or "")
                if not gid:
                    continue
                out[(eid, gid)] = {col: row[col] for col in RATING_FIELDS}
            return out
    except (_IndexAbsent, sqlite3.Error):
        return {}


def null_rating() -> dict[str, Any]:
    """The null triple a consumer attaches when no rating resolved."""
    return dict.fromkeys(RATING_FIELDS)


__all__ = ["RATING_FIELDS", "null_rating", "rating_by_generation"]
