"""Tests for the sidebar-search endpoint and reader.

The sidebar exposes an always-visible search bar that filters across
four categories sourced from the live workspace:

  * entries   — id substring against ``board.jsonl`` for the current epoch
  * judges    — name substring across in-board judges + index judge_losses
  * patches   — mutation_id + rationale substring against the index
  * mutations — mutation_id substring against the index

These tests pin the result shape, the per-category cap, the empty-query
short-circuit, and the exact-vs-substring sort order against a populated
fixture workspace.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.query import WorkspacePaths, build_search_results
from zicato.query.judge_view import SEARCH_LIMIT_PER_CATEGORY


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_search_index(db_path: Path, epoch_id: str, patches: list[tuple]) -> None:
    """Write the minimal SQLite tables the search reader queries."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE patches (
            patch_id TEXT PRIMARY KEY,
            epoch_id TEXT,
            generation_id TEXT,
            mutation_id TEXT,
            op TEXT,
            rationale TEXT
        );
        CREATE TABLE judge_losses (
            run_id TEXT,
            judge_name TEXT,
            weighted_loss REAL,
            raw_loss REAL,
            weight REAL,
            PRIMARY KEY (run_id, judge_name)
        );
        """
    )
    conn.executemany(
        "INSERT INTO patches VALUES (?, ?, ?, ?, ?, ?)",
        patches,
    )
    conn.executemany(
        "INSERT INTO judge_losses VALUES (?, ?, ?, ?, ?)",
        [
            ("r1", "no_fabricated_numbers", 0.1, 0.1, 1.0),
            ("r2", "incorporates_feedback", 0.2, 0.2, 1.0),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def search_workspace(tmp_path: Path) -> Path:
    """A workspace populated with board entries, judges, and patches."""
    ws = tmp_path / ".zicato"
    epoch_id = "2026-05-20_presn"
    epoch_dir = ws / "epochs" / epoch_id
    (ws / "runtime" / "active_runs").mkdir(parents=True)
    (ws / "runtime" / "control").mkdir(parents=True)

    _write(ws / "current_epoch", epoch_id)

    # Three entries — one matches "q3", one matches "waffles", one
    # matches "metrics" (substring inside id).
    board_lines = [
        {"board_meta": True, "disable_drift": []},
        {
            "id": "waffles_single",
            "kind": "single_turn",
            "input": "Make a deck about waffles.",
            "expectation": {"kind": "predicate"},
            "judges": [{"name": "audience_appropriate", "mode": "inline", "body": "..."}],
        },
        {
            "id": "q3_metrics_outline",
            "kind": "single_turn",
            "input": "Outline a deck on quarterly metrics for Q3.",
            "expectation": {"kind": "predicate"},
        },
        {
            "id": "picky_stakeholder_emulated",
            "kind": "multi_turn_emulated",
            "input": "Picky stakeholder dialogue.",
            "expectation": {"kind": "predicate"},
            "judges": [
                {"name": "incorporates_feedback", "mode": "inline", "body": "..."},
                {"name": "no_fabricated_numbers", "mode": "inline", "body": "..."},
            ],
        },
    ]
    _write(
        epoch_dir / "board.jsonl",
        "\n".join(json.dumps(line) for line in board_lines) + "\n",
    )

    # Patches in the index: one with mutation_id "researcher_instruction"
    # (substring match for "researcher"), one with rationale that
    # mentions "topicality" (rationale-only match), and a bunch of dummy
    # patches whose mutation_id starts with "noise_" so the per-category
    # limit test has data to work with.
    patches = [
        (
            "p1",
            epoch_id,
            "v2",
            "researcher_instruction",
            "replace",
            "Adding explicit constraints against tangential content.",
        ),
        (
            "p2",
            epoch_id,
            "v3",
            "coordinator_instruction",
            "replace",
            "Improve topicality enforcement in the coordinator.",
        ),
    ]
    # 15 noise patches whose mutation_id all begin with "noise_match_" so
    # a search for "noise_match" hits more than SEARCH_LIMIT_PER_CATEGORY.
    for i in range(15):
        patches.append(
            (
                f"noise_p{i}",
                epoch_id,
                f"v{10 + i}",
                f"noise_match_{i:02d}",
                "replace",
                f"Noise patch {i}",
            )
        )
    _build_search_index(ws / "index.db", epoch_id, patches)

    return ws


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>zicato</title>", encoding="utf-8")
    return d


@pytest.fixture
def client(search_workspace: Path, static_dir: Path) -> TestClient:
    app = create_app(search_workspace, static_dir, read_only=True)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Reader-level tests — exercise build_search_results directly.
# ---------------------------------------------------------------------------


def test_build_search_results_returns_expected_categories(
    search_workspace: Path,
) -> None:
    """A populated workspace yields hits in every category for a real query."""
    paths = WorkspacePaths(search_workspace)
    results = build_search_results(paths, "q3")
    assert isinstance(results, dict)
    for key in ("entries", "judges", "patches", "mutations"):
        assert key in results
    # The entry id contains "q3" exactly once (case-insensitive).
    entry_ids = {e["id"] for e in results["entries"]}
    assert "q3_metrics_outline" in entry_ids
    # And the entry's match_kind is annotated.
    for e in results["entries"]:
        assert e.get("match_kind") in ("exact", "substring")


def test_build_search_results_judge_hit_from_board(
    search_workspace: Path,
) -> None:
    """A judge name lifted from board.jsonl surfaces in the judges bucket."""
    paths = WorkspacePaths(search_workspace)
    results = build_search_results(paths, "no_fabricated")
    judge_names = {j["name"] for j in results["judges"]}
    assert "no_fabricated_numbers" in judge_names
    # Empty hits in unrelated categories — no entry id contains
    # "no_fabricated" — confirm the category isolation.
    assert results["entries"] == []
    assert results["patches"] == []
    assert results["mutations"] == []


def test_build_search_results_judge_scan_degrades_on_a_torn_board(
    search_workspace: Path,
) -> None:
    """The board judge scan degrades per ROW, and never raises (DQ3).

    The scan runs on the raw JSONL rather than the validating loader, so a
    board that ``load_board`` would reject still yields the judge names it
    can read: a malformed line and a non-object line drop out while their
    siblings survive.
    """
    paths = WorkspacePaths(search_workspace)
    board = search_workspace / "epochs" / "2026-05-20_presn" / "board.jsonl"
    _write(
        board,
        "\n".join(
            [
                json.dumps({"board_meta": True, "disable_drift": []}),
                "{ truncated mid-write",
                "[1, 2, 3]",
                json.dumps(
                    {
                        "id": "picky_stakeholder_emulated",
                        "kind": "multi_turn_emulated",
                        "judges": [{"name": "no_fabricated_numbers"}],
                    }
                ),
            ]
        )
        + "\n",
    )

    results = build_search_results(paths, "no_fabricated")
    assert {j["name"] for j in results["judges"]} == {"no_fabricated_numbers"}


def test_judge_board_scan_survives_a_non_utf8_board(
    search_workspace: Path,
) -> None:
    """A board that is not UTF-8 yields no judge names, never an exception.

    ``read_text(encoding="utf-8")`` raises ``UnicodeDecodeError`` — a
    ``ValueError``, NOT an ``OSError`` — so a reader that guards only on
    ``OSError`` lets it escape and turns a best-effort reader into a 500.

    Pinned against the judge scan directly rather than through
    :func:`build_search_results`, because that endpoint ALSO reads the board
    via ``epoch_view._parse_board`` for its entries category, and that reader
    still carries the ``OSError``-only guard this test would otherwise trip
    over. Move this up to the endpoint once ``_parse_board`` degrades too.
    """
    from zicato.query.judge_view import _collect_judge_names_from_board_file

    board = search_workspace / "epochs" / "2026-05-20_presn" / "board.jsonl"
    board.write_bytes(b"\xff\xfe not utf-8 at all\n")

    assert _collect_judge_names_from_board_file(board) == set()


def test_build_search_results_patches_rationale_substring(
    search_workspace: Path,
) -> None:
    """A rationale-only substring still surfaces as a patch hit."""
    paths = WorkspacePaths(search_workspace)
    results = build_search_results(paths, "topicality")
    # The patch whose rationale mentions "topicality" is found.
    patch_mutations = {p["mutation_id"] for p in results["patches"]}
    assert "coordinator_instruction" in patch_mutations
    # And the matching patch carries a rationale_snippet for the panel.
    coordinator_patch = next(
        p for p in results["patches"] if p["mutation_id"] == "coordinator_instruction"
    )
    assert "topicality" in coordinator_patch["rationale_snippet"].lower()
    # But the mutations bucket is empty — the query did NOT match a
    # mutation_id, only a rationale.
    assert results["mutations"] == []


def test_build_search_results_mutation_id_substring(
    search_workspace: Path,
) -> None:
    """A mutation_id substring lights up both patches AND mutations."""
    paths = WorkspacePaths(search_workspace)
    results = build_search_results(paths, "researcher")
    mutation_ids = {m["mutation_id"] for m in results["mutations"]}
    patch_mutations = {p["mutation_id"] for p in results["patches"]}
    assert "researcher_instruction" in mutation_ids
    assert "researcher_instruction" in patch_mutations


def test_build_search_results_empty_query_is_empty(
    search_workspace: Path,
) -> None:
    """A blank query yields empty result sets — never a wide scan."""
    paths = WorkspacePaths(search_workspace)
    assert build_search_results(paths, "") == {
        "entries": [],
        "judges": [],
        "patches": [],
        "mutations": [],
    }
    assert build_search_results(paths, "   ") == {
        "entries": [],
        "judges": [],
        "patches": [],
        "mutations": [],
    }


def test_build_search_results_respects_per_category_limit(
    search_workspace: Path,
) -> None:
    """No category returns more than SEARCH_LIMIT_PER_CATEGORY records."""
    paths = WorkspacePaths(search_workspace)
    results = build_search_results(paths, "noise_match")
    # 15 patches match — the cap MUST kick in.
    assert len(results["patches"]) == SEARCH_LIMIT_PER_CATEGORY
    assert len(results["mutations"]) == SEARCH_LIMIT_PER_CATEGORY


def test_build_search_results_exact_match_sorts_first(
    search_workspace: Path,
) -> None:
    """An exact (case-insensitive) match is ranked above substring matches."""
    paths = WorkspacePaths(search_workspace)
    # ``researcher_instruction`` is the only patch mutation_id that
    # equals the query verbatim, and it must lead the patches list.
    results = build_search_results(paths, "researcher_instruction")
    assert results["patches"], "expected at least one patch hit"
    assert results["patches"][0]["mutation_id"] == "researcher_instruction"
    assert results["patches"][0]["match_kind"] == "exact"


# ---------------------------------------------------------------------------
# Endpoint-level tests — exercise /api/search through the ASGI app.
# ---------------------------------------------------------------------------


def test_api_search_responds_with_results(client: TestClient) -> None:
    """``GET /api/search?q=q3`` returns the expected entry hit."""
    r = client.get("/api/search", params={"q": "q3"})
    assert r.status_code == 200
    body = r.json()
    for key in ("entries", "judges", "patches", "mutations"):
        assert key in body
    entry_ids = {e["id"] for e in body["entries"]}
    assert "q3_metrics_outline" in entry_ids


def test_api_search_empty_query_returns_empty(client: TestClient) -> None:
    """``GET /api/search?q=`` (or absent ``q``) returns empty result sets."""
    r = client.get("/api/search")
    assert r.status_code == 200
    body = r.json()
    assert body == {"entries": [], "judges": [], "patches": [], "mutations": []}


def test_api_search_judges_endpoint(client: TestClient) -> None:
    """The endpoint surfaces judges from the board + index."""
    r = client.get("/api/search", params={"q": "incorporates"})
    assert r.status_code == 200
    body = r.json()
    judge_names = {j["name"] for j in body["judges"]}
    assert "incorporates_feedback" in judge_names
