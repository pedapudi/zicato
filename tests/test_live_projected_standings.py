"""LIVE PROJECTED STANDINGS — the in-flight projected-standing feature.

An in-flight candidate (still being evaluated on boards) shows a PROJECTED
standing that climbs as board results stream in, visually distinct from a
settled one. These pin the backend half:

* ``ActiveTournament.projected`` round-trips through ``to_dict`` /
  ``from_dict`` and an OLD payload (no ``projected`` key) loads
  byte-identical (default-empty additive field);
* ``update_tournament_projected`` MERGES per-generation rows the instant a
  board settles, mirroring ``update_tournament_partial_aggregate``;
* the ``_IncrementalScorer`` writes a per-generation projected row
  (``scalar`` / ``boards_done`` / ``boards_total`` / ``pass_rate``) for
  every settled board unit;
* ``_overlay_projected_standings`` folds the projected map onto the
  standings rows for IN-FLIGHT competitors only and RE-SORTS per the
  per-structure rule — elim/racing re-rank on the projected scalar; SWISS
  does NOT project Copeland points (it only nudges the mean-scalar
  tiebreak); a settled row keeps its real scalar.

No live ``zicato evolve`` is run — the projected state is simulated.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from zicato.orchestrator import _overlay_projected_standings
from zicato.runtime.state import (
    ActiveTournament,
    read_active_tournament,
    update_tournament_partial_aggregate,
    update_tournament_projected,
    write_active_tournament,
)
from zicato.tournament.runner import _IncrementalScorer
from zicato.tournament.scoring import ScoringWeights

# ---------------------------------------------------------------------------
# (a) the projected field round-trips + old payloads load byte-identical
# ---------------------------------------------------------------------------


def test_projected_round_trips_through_to_from_dict() -> None:
    t = ActiveTournament(
        tournament_id="t",
        parent_generation_id="p",
        child_generation_id="c",
        epoch_id="e",
        started_at="2026-01-01",
        projected={
            "g1": {"scalar": 1.5, "boards_done": 2, "boards_total": 5, "pass_rate": 0.8},
        },
    )
    d = t.to_dict()
    assert d["projected"] == {
        "g1": {"scalar": 1.5, "boards_done": 2, "boards_total": 5, "pass_rate": 0.8}
    }
    back = ActiveTournament.from_dict(d)
    assert back.projected == t.projected


def test_old_payload_without_projected_loads_empty() -> None:
    # an active_tournament.json written before this feature has no `projected`
    # key — it must load with an EMPTY projected map (additive, default-empty).
    old = ActiveTournament.from_dict(
        {
            "tournament_id": "t",
            "parent_generation_id": "p",
            "child_generation_id": "c",
            "epoch_id": "e",
            "started_at": "x",
        }
    )
    assert old.projected == {}


# ---------------------------------------------------------------------------
# (b) the writer merges rows + preserves the rest of the envelope
# ---------------------------------------------------------------------------


def test_update_tournament_projected_merges_and_preserves(tmp_path: Path) -> None:
    write_active_tournament(
        tmp_path,
        ActiveTournament(
            tournament_id="t",
            parent_generation_id="",
            child_generation_id="",
            epoch_id="e",
            started_at="x",
            structure="single_elim",
            standings=[{"generation_id": "A", "rank": 1, "scalar": 0.0}],
        ),
    )
    # the champion side's board settles first, then the challenger's: both rows
    # must coexist (a board unit runs both sides concurrently).
    update_tournament_projected(
        tmp_path, {"A": {"scalar": 2.0, "boards_done": 1, "boards_total": 4, "pass_rate": 1.0}}
    )
    update_tournament_projected(
        tmp_path, {"B": {"scalar": 1.0, "boards_done": 1, "boards_total": 4, "pass_rate": 1.0}}
    )
    back = read_active_tournament(tmp_path)
    assert back is not None
    assert set(back.projected) == {"A", "B"}
    assert back.projected["A"]["scalar"] == 2.0
    assert back.projected["B"]["boards_total"] == 4
    # the rest of the envelope (the standings the orchestrator published) is
    # untouched by the projected writer.
    assert back.standings == [{"generation_id": "A", "rank": 1, "scalar": 0.0}]


def test_partial_aggregate_and_projected_compose(tmp_path: Path) -> None:
    # both runner writers (partial aggregate + projected) read-modify-write the
    # same file; neither clobbers the other.
    write_active_tournament(
        tmp_path,
        ActiveTournament(
            tournament_id="t",
            parent_generation_id="",
            child_generation_id="",
            epoch_id="e",
            started_at="x",
        ),
    )
    update_tournament_partial_aggregate(tmp_path, champion_agg={"scalar": 3.0})
    update_tournament_projected(
        tmp_path, {"A": {"scalar": 3.0, "boards_done": 1, "boards_total": 2}}
    )
    back = read_active_tournament(tmp_path)
    assert back is not None
    assert back.partial_champion_agg == {"scalar": 3.0}
    assert back.projected["A"]["scalar"] == 3.0


# ---------------------------------------------------------------------------
# (c) the incremental scorer writes a projected row per settled board
# ---------------------------------------------------------------------------


class _FakeLoss:
    """Minimal LossProfile stand-in for aggregate_generation_score.

    ``drift_loss`` is the per-run float (``per_run_drift_loss`` returns it
    verbatim); ``unified_metrics()`` yields no namespaced metrics; the
    ``pass_fail`` carries the predicate verdict.
    """

    def __init__(self, entry_id: str, drift: float, passed: bool | None = True) -> None:
        self.entry_id = entry_id
        self.drift_loss = drift
        self.pass_fail = passed
        self.adk_session_id = ""

    def unified_metrics(self) -> list:
        return []


def test_incremental_scorer_writes_projected_per_board(tmp_path: Path) -> None:
    write_active_tournament(
        tmp_path,
        ActiveTournament(
            tournament_id="t",
            parent_generation_id="",
            child_generation_id="",
            epoch_id="e",
            started_at="x",
        ),
    )
    weights = ScoringWeights()
    scorer = _IncrementalScorer(
        weights,
        tmp_path,
        champion_id="v0",
        challenger_id="v1",
        board_total=3,
    )

    async def _run() -> None:
        # one board unit settles (champion + challenger).
        await scorer.record(
            champion_loss=_FakeLoss("b0", 0.4), challenger_loss=_FakeLoss("b0", 0.2)
        )

    asyncio.run(_run())
    back = read_active_tournament(tmp_path)
    assert back is not None
    assert set(back.projected) == {"v0", "v1"}
    # each row carries the running aggregate scalar + the boards-so-far progress.
    assert back.projected["v0"]["boards_done"] == 1
    assert back.projected["v0"]["boards_total"] == 3
    assert back.projected["v1"]["boards_done"] == 1
    # the challenger (lower drift) projects a lower scalar than the champion.
    assert back.projected["v1"]["scalar"] < back.projected["v0"]["scalar"]


def test_incremental_scorer_no_ids_writes_no_projected(tmp_path: Path) -> None:
    # the gauntlet seed-scoring path threads no gen ids → no projected write
    # (byte-identical to before the feature).
    write_active_tournament(
        tmp_path,
        ActiveTournament(
            tournament_id="t",
            parent_generation_id="",
            child_generation_id="",
            epoch_id="e",
            started_at="x",
        ),
    )
    scorer = _IncrementalScorer(ScoringWeights(), tmp_path)

    async def _run() -> None:
        await scorer.record(challenger_loss=_FakeLoss("b0", 0.2))

    asyncio.run(_run())
    back = read_active_tournament(tmp_path)
    assert back is not None
    assert back.projected == {}


# ---------------------------------------------------------------------------
# (d) the orchestrator overlay folds + re-sorts per structure
# ---------------------------------------------------------------------------


def _two_competitor_state(tmp_path: Path, structure: str, standings: list[dict]) -> None:
    write_active_tournament(
        tmp_path,
        ActiveTournament(
            tournament_id="t",
            parent_generation_id="",
            child_generation_id="",
            epoch_id="e",
            started_at="x",
            structure=structure,
            standings=standings,
        ),
    )


_PENDING_AB = [
    {
        "round_index": 0,
        "matches": [{"match_id": "m1", "competitors": ["A", "B"], "pending": True, "winner": None}],
    }
]


def test_overlay_elim_reranks_on_projected_scalar(tmp_path: Path) -> None:
    standings = [
        {"generation_id": "A", "rank": 1, "scalar": 0.0, "wins": 0, "status": "alive"},
        {"generation_id": "B", "rank": 2, "scalar": 0.0, "wins": 0, "status": "alive"},
    ]
    _two_competitor_state(tmp_path, "single_elim", standings)
    # B projects the lower (better) scalar — it must bubble to rank 1.
    update_tournament_projected(
        tmp_path,
        {
            "A": {"scalar": 2.0, "boards_done": 3, "boards_total": 5},
            "B": {"scalar": 1.0, "boards_done": 3, "boards_total": 5},
        },
    )
    out = _overlay_projected_standings(
        [dict(s) for s in standings], _PENDING_AB, tmp_path, "single_elim"
    )
    assert out[0]["generation_id"] == "B"
    assert out[0]["rank"] == 1
    assert out[0]["in_flight"] is True
    assert out[0]["projected_scalar"] == 1.0
    assert out[0]["boards_done"] == 3 and out[0]["boards_total"] == 5


def test_overlay_swiss_does_not_project_copeland_points(tmp_path: Path) -> None:
    # A has 1 win (settled, no projection). B is in flight with a GREAT projected
    # scalar but 0 wins. SWISS must NOT promote B over A — a half-finished duel
    # has crowned no winner; points are authoritative.
    standings = [
        {"generation_id": "A", "rank": 1, "scalar": 3.0, "wins": 1, "status": "alive"},
        {"generation_id": "B", "rank": 2, "scalar": 0.0, "wins": 0, "status": "alive"},
    ]
    _two_competitor_state(tmp_path, "swiss", standings)
    rounds = [
        {
            "round_index": 1,
            "matches": [
                {"match_id": "m", "competitors": ["B", "C"], "pending": True, "winner": None}
            ],
        }
    ]
    update_tournament_projected(
        tmp_path, {"B": {"scalar": 0.01, "boards_done": 4, "boards_total": 5}}
    )
    out = _overlay_projected_standings([dict(s) for s in standings], rounds, tmp_path, "swiss")
    assert out[0]["generation_id"] == "A", "swiss never re-ranks on a projected scalar"
    # B is still MARKED in-flight (the visual treatment), just not re-ranked up.
    b_row = next(r for r in out if r["generation_id"] == "B")
    assert b_row.get("in_flight") is True
    assert b_row.get("projected_scalar") == 0.01


def test_overlay_swiss_tiebreaks_on_projected_scalar_within_equal_wins(tmp_path: Path) -> None:
    # equal wins → the projected mean-scalar breaks the tie (B's lower scalar wins).
    standings = [
        {"generation_id": "A", "rank": 1, "scalar": 0.0, "wins": 0, "status": "alive"},
        {"generation_id": "B", "rank": 2, "scalar": 0.0, "wins": 0, "status": "alive"},
    ]
    _two_competitor_state(tmp_path, "swiss", standings)
    update_tournament_projected(
        tmp_path,
        {
            "A": {"scalar": 2.0, "boards_done": 3, "boards_total": 5},
            "B": {"scalar": 1.0, "boards_done": 3, "boards_total": 5},
        },
    )
    out = _overlay_projected_standings([dict(s) for s in standings], _PENDING_AB, tmp_path, "swiss")
    assert out[0]["generation_id"] == "B"


def test_overlay_settled_row_keeps_its_real_scalar(tmp_path: Path) -> None:
    # a competitor NOT in a pending match (already settled) keeps its real scalar
    # and gets NO in_flight marker even if a stale projected row lingers.
    standings = [
        {"generation_id": "A", "rank": 1, "scalar": 1.5, "wins": 1, "status": "alive"},
        {"generation_id": "B", "rank": 2, "scalar": 0.0, "wins": 0, "status": "alive"},
    ]
    _two_competitor_state(tmp_path, "single_elim", standings)
    # A's projected row is stale (A's match already settled); only B is pending.
    update_tournament_projected(
        tmp_path,
        {
            "A": {"scalar": 9.9, "boards_done": 5, "boards_total": 5},
            "B": {"scalar": 0.5, "boards_done": 2, "boards_total": 5},
        },
    )
    rounds = [
        {
            "round_index": 1,
            "matches": [
                {"match_id": "m", "competitors": ["B", "C"], "pending": True, "winner": None}
            ],
        }
    ]
    out = _overlay_projected_standings(
        [dict(s) for s in standings], rounds, tmp_path, "single_elim"
    )
    a_row = next(r for r in out if r["generation_id"] == "A")
    assert "in_flight" not in a_row
    assert a_row["scalar"] == 1.5  # the settled scalar, NOT the stale 9.9 projection


def test_overlay_no_projected_returns_input_unchanged(tmp_path: Path) -> None:
    standings = [{"generation_id": "A", "rank": 1, "scalar": 1.0}]
    _two_competitor_state(tmp_path, "single_elim", standings)
    out = _overlay_projected_standings(
        [dict(s) for s in standings], _PENDING_AB, tmp_path, "single_elim"
    )
    assert out == standings
