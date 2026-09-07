"""Terminal detail views preserve served evidence and browser coordinates."""

from __future__ import annotations

from copy import deepcopy

import pytest

from tests.tui_fixture import CHALLENGER, CHAMPION, EPOCH, PAYLOADS
from zicato.tui.client import SnapshotClient
from zicato.tui.console import Console
from zicato.tui.routes import parse_route
from zicato.tui.view import render_text


def review_payloads():
    payloads = deepcopy(PAYLOADS)
    record = next(
        g for g in payloads["/api/lineage"]["generations"] if g["generation_id"] == CHALLENGER
    )
    record.update(decision="promoted", decision_label="promoted")
    experiment = payloads[f"/api/epoch?epoch={EPOCH}"]["experiments"][0]
    payloads[f"/api/epoch/{EPOCH}/candidate/{CHALLENGER}"] = {
        "found": True,
        "epoch_id": EPOCH,
        "generation_id": CHALLENGER,
        "generation": record,
        "experiment": experiment,
        "relatives": [],
        "parent": CHAMPION,
        "champion": CHALLENGER,
        "structure": "gauntlet",
        "per_entry": payloads[f"/api/generation/{EPOCH}/{CHALLENGER}/per-entry"],
        "per_judge": payloads[f"/api/generation/{EPOCH}/{CHALLENGER}/per-judge"],
        "gates": [
            {
                "champion": CHAMPION,
                "challenger": CHALLENGER,
                "role": "challenger",
                "gate": payloads[f"/api/round/{EPOCH}/{CHAMPION}/{CHALLENGER}/gate"],
            }
        ],
    }
    payloads["/api/health-report"]["findings"][0]["detail"] = {
        "recommendation": "Add an entry that separates the candidates",
        "evidence": {"comparisons": 12, "discrimination": 0.0},
    }
    return payloads


@pytest.mark.parametrize(
    ("path", "lens", "evidence"),
    [
        (f"/e/{EPOCH}/gen/{CHALLENGER}", "standings", "scalar beat the promote margin"),
        (f"/e/{EPOCH}/evals", "instrument", "rotate the holdout"),
        ("/logs", "home", "round 3 promoted v4 over v3"),
    ],
)
def test_detail_route_reaches_evidence_without_another_rail_item(path, lens, evidence):
    route = parse_route(path)
    assert route.lens == lens
    assert route.unsupported is None
    console = Console(SnapshotClient(review_payloads()), route=route)
    console.refresh()
    assert evidence in render_text(console.view, width=120)


def test_candidate_reads_one_scoped_dossier_and_keeps_historical_gate():
    payloads = review_payloads()
    path = f"/api/epoch/{EPOCH}/candidate/{CHALLENGER}"
    gate = payloads[path]["gates"][0]["gate"]
    gate.clear()
    gate.update(decision="rejected", reason="Recorded under a previous gate contract")
    payloads[path]["generation"]["decision_label"] = "rejected"
    payloads["/api/lineage"] = {"generations": [{"generation_id": CHALLENGER, "elo": 9999}]}
    client = SnapshotClient(payloads)
    console = Console(client, route=parse_route(f"/e/{EPOCH}/gen/{CHALLENGER}"))
    console.refresh()
    assert client.requested == [path]
    text = render_text(console.view, width=120)
    assert "rejected" in text
    assert "previous gate contract" in text
    assert "No rule breakdown was recorded" in text
    assert "9999" not in text


def test_health_detail_and_log_rewrite_repaint_without_count_or_cursor_change():
    payloads = review_payloads()
    console = Console(SnapshotClient(payloads), route=parse_route("/logs"))
    console.refresh()
    first = console.view.digest
    assert console.refresh() is False
    payloads["/api/health-report"]["findings"][0]["detail"]["recommendation"] = (
        "Revise the predicate"
    )
    console.client = SnapshotClient(payloads)
    assert console.refresh() is True
    assert "Revise the predicate" in render_text(console.view)
    assert console.view.digest != first
    before = console.view.digest
    payloads["/api/logs?limit=200"]["records"][0]["message"] = "Repaired the recorded decision"
    console.client = SnapshotClient(payloads)
    assert console.refresh() is True
    assert "Repaired the recorded decision" in render_text(console.view)
    assert console.view.digest != before


def test_foreign_epoch_health_and_missing_matrix_remain_unavailable():
    payloads = review_payloads()
    console = Console(SnapshotClient(payloads), route=parse_route("/e/historical/health"))
    console.refresh()
    text = render_text(console.view)
    assert "no report is available for this selection" in text
    assert "reported health         unavailable" in text
    assert "Add an entry that separates" not in text
    assert "round 3 promoted" not in text
    console = Console(SnapshotClient({}), route=parse_route("/e/historical/evals"))
    console.refresh()
    assert "Evaluation health is unavailable" in render_text(console.view)
    assert "failed to render" not in (console.view.degraded or "")


def test_candidate_entry_and_external_parent_drills_preserve_coordinates():
    payloads = review_payloads()
    path = f"/api/epoch/{EPOCH}/candidate/{CHALLENGER}"
    payloads[path]["relatives"] = [
        {"relationship": "parent", "epoch_id": "source", "generation_id": "v4"}
    ]
    console = Console(SnapshotClient(payloads), route=parse_route(f"/e/{EPOCH}/gen/{CHALLENGER}"))
    console.refresh()
    target = next(r for r in console.rows if r.action == "/e/source/gen/v4")
    console.cursor = console.rows.index(target)
    console.drill()
    assert console.route.params == {"epoch": "source", "gen": "v4"}
    assert console.back() is True
    console.refresh()
    target = next(r for r in console.rows if r.key == "plan-long-1")
    console.cursor = console.rows.index(target)
    console.drill()
    assert console.route.params == {"epoch": EPOCH, "gen": CHALLENGER, "entry": "plan-long-1"}
    assert parse_route(console.route.to_path()) == console.route


@pytest.mark.parametrize("path", [f"/e/{EPOCH}/gen/{CHALLENGER}", f"/e/{EPOCH}/evals", "/logs"])
def test_detail_views_retain_evidence_in_ascii_and_narrow_rendering(path):
    console = Console(SnapshotClient(review_payloads()), route=parse_route(path), ascii_only=True)
    console.refresh()
    text = render_text(console.view, width=60, ascii_only=True)
    assert text.isascii()
    assert all(len(line) <= 60 for line in text.splitlines())
    keys = [key for key, _ in console.view.lines()]
    assert len(keys) == len(set(keys))
    assert console.view.selectable_rows()


def test_dossier_owns_epoch_scoped_decision_rating_and_external_parent(tmp_path):
    from tests._workspace_support import (
        experiment_record,
        seed_index,
        workspace,
        write_epoch,
        write_generation,
        write_lineage,
    )
    from zicato.query.candidate_view import build_candidate_dossier
    from zicato.query.paths import WorkspacePaths

    layout = workspace(tmp_path)
    epochs = []
    ratings = []
    for eid, rating, promoted in (("selected", 1800.0, True), ("active", 1200.0, False)):
        write_epoch(layout, eid, current=eid == "active")
        write_generation(
            layout, eid, "v1", experiment=experiment_record("v1", parent_generation_id="source:v8")
        )
        epochs.append(
            {
                "id": eid,
                "generations": [
                    {"id": "v1", "parent_id": "source:v8", "promoted": promoted},
                ],
            }
        )
        ratings.append(
            {"epoch_id": eid, "generation_id": "v1", "elo": rating, "elo_se": 20, "elo_games": 8}
        )
    write_lineage(layout, {"epochs": epochs})
    seed_index(layout, {"generations": ratings})
    dossier = build_candidate_dossier(WorkspacePaths(layout.root), "selected", "v1")
    assert dossier["generation"]["elo"] == 1800.0
    assert dossier["generation"]["decision_label"] == "promoted"
    assert dossier["experiment"]["decision_label"] == "promoted"
    assert dossier["relatives"] == [
        {"relationship": "parent", "epoch_id": "source", "generation_id": "v8"}
    ]
    client = SnapshotClient({"/api/epoch/selected/candidate/v1": dossier})
    console = Console(client, route=parse_route("/e/selected/gen/v1"))
    console.refresh()
    assert "1800" in render_text(console.view)
    assert "1200" not in render_text(console.view)


def test_pending_remedy_reaches_instrument_from_canonical_record_without_reflection(tmp_path):
    from hashlib import sha256

    from zicato.proposer.reflection import (
        ProposerFinding,
        ProposerReflection,
        ProposerRemedy,
        write_reflection,
    )
    from zicato.query.paths import WorkspacePaths
    from zicato.query.proposer_view import build_proposer_recommendations

    text = "Check the parent before proposing an edit.\n"
    remedy = ProposerRemedy(
        "skill_replace",
        "skills/review.md",
        text,
        sha256(text.encode()).hexdigest(),
        "-Guess the parent\n+Check the parent",
    )
    finding = ProposerFinding(
        "review-parent",
        "warning",
        "Read the parent",
        "Edits lack context",
        "proposals",
        ({"count": 4},),
        "recorded base rate",
        remedy,
        "Changes proposer instructions only",
    )
    write_reflection(
        tmp_path,
        ProposerReflection("review", EPOCH, "2026-07-04T00:00:00Z", "scorecard", (finding,)),
    )
    queue = build_proposer_recommendations(WorkspacePaths(tmp_path))
    assert queue["pending"][0]["remedy"] == remedy.to_json()
    payloads = {"/api/proposer/recommendations": queue}
    console = Console(SnapshotClient(payloads), route=parse_route(f"/e/{EPOCH}/instrument"))
    console.refresh()
    rendered = render_text(console.view, width=120)
    assert "+Check the parent" in rendered
    assert f"zicato proposer apply-recommendation review-parent --epoch {EPOCH}" in rendered
    assert "Board status and outcome distribution" in rendered
    assert all(not (r.action or "").startswith("zicato ") for r in console.rows)
    assert console.refresh() is False
    queue["pending"][0]["remedy"]["diff"] += "\n+Use the recorded ancestry"
    console.client = SnapshotClient(payloads)
    assert console.refresh() is True
    assert "Use the recorded ancestry" in render_text(console.view, width=120)


async def test_keyboard_reaches_long_health_detail_and_keeps_rail_selection():
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    from zicato.tui.app import ZicatoTui

    payloads = review_payloads()
    payloads["/api/health-report"]["findings"][0]["detail"] = {
        **{f"measurement_{i}": i for i in range(30)},
        "recommendation": "Review the final measured discrepancy",
    }
    app = ZicatoTui(
        client=SnapshotClient(payloads),
        route=parse_route("/logs"),
        workspace="/tmp/workspace",
        ascii_only=True,
        poll_seconds=3600,
    )
    async with app.run_test(size=(60, 18)) as pilot:
        await pilot.pause()
        target = next(
            i for i, r in enumerate(app.console_model.rows) if r.key.endswith(":recommendation")
        )
        app.action_move(target)
        await pilot.pause()
        assert "final measured discrepancy" in app.console_model.selected.text
        assert app.query_one("#content", VerticalScroll).scroll_y > 0
        assert app.query_one("#rail-home", Static).has_class("selected")
        assert not app.query_one("#rail-home", Static).has_class("cursor")


def test_parent_contradiction_is_rendered_without_client_reconciliation():
    payloads = review_payloads()
    path = f"/api/epoch/{EPOCH}/candidate/{CHALLENGER}"
    payloads[path].update(
        parent_inconsistency="Experiment parent contradicts recorded lineage", gates=[], parent=None
    )
    console = Console(SnapshotClient(payloads), route=parse_route(f"/e/{EPOCH}/gen/{CHALLENGER}"))
    console.refresh()
    assert "Experiment parent contradicts recorded lineage" in render_text(console.view)
    assert console.client.requested == [path]


async def test_log_event_refreshes_the_tail_after_progress_has_stopped():
    from zicato.tui.app import ZicatoTui
    from zicato.tui.client import Event

    payloads = review_payloads()
    app = ZicatoTui(
        client=SnapshotClient(payloads),
        route=parse_route("/logs"),
        workspace="/tmp/workspace",
        ascii_only=False,
        poll_seconds=3600,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        payloads["/api/logs?limit=200"]["records"][0]["message"] = "Decision record repaired"
        app.console_model.client = SnapshotClient(payloads)
        app.on_sse(Event("run_log", {"cursor": 1}))
        await pilot.pause()
        assert "Decision record repaired" in render_text(app.console_model.view)
