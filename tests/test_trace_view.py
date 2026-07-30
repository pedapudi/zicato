"""WS-DATA tests — the trajectory-bootstrap UI readers (TRAJECTORY-UI.md §3–§4).

Known-answers over a SEEDED workspace via the REAL engine pipeline (import →
mine → synthesise → persist), the cold/unknown degrades, the pure strip-model
helpers, and the byte-stability of the captured node fixtures (the
composition-check, rule 2). No mocks of the readers — the payloads under test
are exactly what the shipped readers emit.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from tests._trace_view_harness import (
    REFLECTION_ID,
    build_and_capture,
    build_workspace,
    canonical_json,
    capture_payloads,
    run_pipeline,
)
from zicato.query import (
    build_suggestion_provenance,
    build_trace_detail,
    build_trace_list,
)
from zicato.query.paths import WorkspacePaths
from zicato.query.trace_view import (
    LANE_EXTENT_CAP,
    budget_fill,
    lane_marks,
    signal_ticks,
)

_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "zicato"
    / "dashboard"
    / "static"
    / "test"
    / "fixtures"
    / "trace_view"
)


# --- pure strip-model helpers (no I/O, known answers) ----------------------


def test_lane_marks_size_by_text_length() -> None:
    marks = lane_marks(["aaaa"], ["bbbbbbbbbbbb"])  # 4 + 12 chars
    assert [m["role"] for m in marks] == ["user", "agent"]
    # the compressive extent scale: weights sqrt(5) / sqrt(13), the widest mark
    # pinned to the per-mark cap, the RATIO preserved exactly.
    assert marks[0]["x0"] == 0.0
    assert marks[1]["x1"] - marks[1]["x0"] == pytest.approx(LANE_EXTENT_CAP)
    ratio = (marks[1]["x1"] - marks[1]["x0"]) / (marks[0]["x1"] - marks[0]["x0"])
    assert ratio == pytest.approx(math.sqrt(13) / math.sqrt(5), rel=1e-3)
    # height stays the RAW char share (the figure bounds it to ≤40 % of the lane)
    assert marks[0]["size"] == 0.3333  # 4 / 12 (tallest = 1.0)
    assert marks[1]["size"] == 1.0


def test_lane_marks_cap_the_widest_extent_and_never_wall_the_lane() -> None:
    """A 2-turn trace must NOT tile the lane (the black-blob regression)."""
    marks = lane_marks(["hi"], ["x" * 4000])
    assert len(marks) == 2
    assert all(m["x1"] - m["x0"] <= LANE_EXTENT_CAP + 1e-9 for m in marks)
    assert marks[-1]["x1"] < 1.0, "a 2-turn lane stays UNDER-filled, never edge to edge"
    # sqrt compression: a 4000-char answer is ~44× the 2-char prompt's weight,
    # not 2000× — but it is still capped, so the terse turn stays visible.
    assert marks[0]["x1"] - marks[0]["x0"] > 0.004


def test_lane_marks_saturate_and_tile_exactly_when_the_lane_fills() -> None:
    marks = lane_marks(["u" * 20] * 30, ["a" * 40] * 30)  # 60 turns
    assert marks[0]["x0"] == 0.0
    assert marks[-1]["x1"] == 1.0  # pinned to the right edge on saturation
    assert all(m["x1"] - m["x0"] <= LANE_EXTENT_CAP + 1e-9 for m in marks)
    # laid end-to-end, monotone, non-overlapping
    for prev, nxt in zip(marks, marks[1:], strict=False):
        assert nxt["x0"] == prev["x1"]


def test_lane_marks_alternate_and_append_trailing() -> None:
    marks = lane_marks(["u0", "u1"], ["a0"])  # trailing user turn
    assert [m["role"] for m in marks] == ["user", "agent", "user"]


def test_lane_marks_empty() -> None:
    assert lane_marks([], []) == []


def test_lane_marks_zero_char_even_spacing() -> None:
    marks = lane_marks(["", ""], ["", ""])  # 4 empty turns → even quarters
    assert [m["x0"] for m in marks] == [0.0, 0.25, 0.5, 0.75]
    assert marks[-1]["x1"] == 1.0
    assert all(m["size"] == 1.0 for m in marks)  # no length ⇒ no height claim


def test_signal_ticks_evenly_distributed_and_unpositioned() -> None:
    ticks = signal_ticks([("error_cascade", 3, "3 err"), ("retry_loop", 1, "1 loop")])
    assert [t["x"] for t in ticks] == [0.3333, 0.6667]
    assert ticks[0]["tone"] == "bad" and ticks[0]["glyph"] == "✕"
    assert ticks[1]["tone"] == "caution" and ticks[1]["glyph"] == "↻"
    assert all(t["positioned"] is False for t in ticks)


def test_admission_viz_probed_and_leakage_branches() -> None:
    from zicato.query.trace_view import _admission_viz

    # Unmeasured (None) — the plan-mode default: no fabricated numbers.
    none = _admission_viz(None)
    assert none["measured"] is False and none["evidence_tier"] == "planned"
    assert none["flip"]["rate"] is None and none["leakage_ok"] is None

    # A probed record with a noisy flip over the ceiling + a leakage flag.
    probed = _admission_viz(
        {
            "spent": True,
            "noise": {"measured": True, "flip_rate": 0.4, "runs": 5},
            "discrimination": {"measured": True, "separated": 0, "pairs": 3},
            "leakage_checked": True,
            "leakage": {"checked": True, "target_slice_ok": False},
        }
    )
    assert probed["measured"] is True and probed["evidence_tier"] == "probed"
    assert probed["flip"]["rate"] == 0.4 and probed["flip"]["over_ceiling"] is True
    assert probed["discrimination"]["separated"] == 0 and probed["discrimination"]["pairs"] == 3
    assert probed["leakage_ok"] is False  # target_slice_ok False → leak


def test_budget_fill_fraction_and_over() -> None:
    b = budget_fill(50_000, 25, max_tokens=100_000, max_calls=50)
    assert b["fill"] == 0.5 and b["over"] is False and b["shaded"] is True
    over = budget_fill(200_000, 10, max_tokens=100_000, max_calls=50)
    assert over["fill"] == 1.0 and over["over"] is True
    zero = budget_fill(0, 0, max_tokens=100_000, max_calls=50)
    assert zero["fill"] == 0.0 and zero["shaded"] is False


# --- known answers over the REAL pipeline ----------------------------------


def test_trace_list_known_answers(tmp_path: Path) -> None:
    ws, epoch_id = build_workspace(tmp_path)
    run_pipeline(ws, epoch_id)
    view = build_trace_list(WorkspacePaths(ws), REFLECTION_ID)

    assert view["found"] is True
    assert view["epoch_id"] == epoch_id
    assert view["trace_count"] == view["trace_count"] and view["trace_count"] >= 2
    # Richest traces lead (episode count desc): the adk_events run (many signals)
    # outranks the bare transcript (one behavioral episode).
    dialects = [t["dialect"] for t in view["traces"]]
    assert "adk_events" in dialects and "transcript" in dialects
    counts = [t["episode_count"] for t in view["traces"]]
    assert counts == sorted(counts, reverse=True)  # richest-first ordering
    # Every row carries a pre-computed strip-model the JS draws straight from.
    for t in view["traces"]:
        strip = t["strip_model"]
        assert strip["trace_id"] == t["trace_id"]
        assert "lane" in strip and "signals" in strip and "budget" in strip
        assert all(0.0 <= m["x0"] <= m["x1"] <= 1.0 for m in strip["lane"]["marks"])
        assert all(s["positioned"] is False for s in strip["signals"])


def test_trace_detail_reconstructs_conversation(tmp_path: Path) -> None:
    ws, epoch_id = build_workspace(tmp_path)
    run_pipeline(ws, epoch_id)
    paths = WorkspacePaths(ws)
    trace_id = build_trace_list(paths, REFLECTION_ID)["traces"][0]["trace_id"]
    detail = build_trace_detail(paths, REFLECTION_ID, trace_id)

    assert detail["found"] is True and detail["trace_id"] == trace_id
    # The reconstructed conversation uses the transcript turn vocabulary.
    assert detail["turns"], "no reconstructed turns"
    assert all(set(t) >= {"index", "role", "text", "chars", "truncated"} for t in detail["turns"])
    assert all(t["role"] in {"user", "agent"} for t in detail["turns"])
    assert detail["reconstruction_note"]
    # Episode spans + anchors link into the strip; a signal episode is anchored.
    for ep in detail["episodes"]:
        assert ep["span"]["anchor"] in {"signal", "lane"}
        assert 0.0 <= ep["span"]["x0"] <= ep["span"]["x1"] <= 1.0


def test_suggestion_provenance_chain(tmp_path: Path) -> None:
    ws, epoch_id = build_workspace(tmp_path)
    run_pipeline(ws, epoch_id)
    paths = WorkspacePaths(ws)
    # Find a bootstrap suggestion whose provenance chain is non-empty.
    from zicato.reflection.suggestions import read_suggestions

    sug = next(
        s
        for s in read_suggestions(ws, epoch_id, REFLECTION_ID)
        if isinstance(s.provenance.get("foreign_source"), dict)
        and s.provenance.get("source_episodes")
    )
    prov = build_suggestion_provenance(paths, REFLECTION_ID, sug.suggestion_id)

    assert prov["found"] is True
    assert prov["foreign_source"]["kind"] == "trajectory_bootstrap"
    assert prov["episodes"], "empty provenance chain"
    # Each chain link carries its trace-segment strip-model, focused on the episode.
    for link in prov["episodes"]:
        seg = link["segment_strip_model"]
        assert seg["focus_episode_id"] == link["episode_id"]
        assert seg["trace_id"] == link["trace_id"]
    # Admission is honest: plan-mode (no probe) is unmeasured, never a fabricated 0.
    viz = prov["admission_viz"]
    assert viz["evidence_tier"] == "planned"
    assert viz["measured"] is False
    assert viz["flip"]["rate"] is None  # unmeasured, not 0.0


# --- degrades (DQ3 same-shape, no raise, no fabricated numbers) -------------


def test_cold_reflection_degrades(tmp_path: Path) -> None:
    ws, _epoch_id = build_workspace(tmp_path)  # no pipeline run → no traces
    paths = WorkspacePaths(ws)
    lst = build_trace_list(paths, "refl-nope")
    assert lst["found"] is False and lst["traces"] == [] and lst["trace_count"] == 0
    det = build_trace_detail(paths, "refl-nope", "trace-nope")
    assert det["found"] is False and det["turns"] == [] and det["strip_model"] == {}
    prov = build_suggestion_provenance(paths, "refl-nope", "sug-nope")
    assert prov["found"] is False and prov["episodes"] == []
    assert prov["admission_viz"]["flip"]["rate"] is None


def test_unknown_trace_and_suggestion_degrade(tmp_path: Path) -> None:
    ws, epoch_id = build_workspace(tmp_path)
    run_pipeline(ws, epoch_id)
    paths = WorkspacePaths(ws)
    det = build_trace_detail(paths, REFLECTION_ID, "trace-doesnotexist")
    assert det["found"] is False and det["epoch_id"] == epoch_id
    prov = build_suggestion_provenance(paths, REFLECTION_ID, "sug-doesnotexist")
    assert prov["found"] is False and prov["epoch_id"] == epoch_id


# --- determinism + byte-stability of the captured fixtures ------------------


def test_capture_is_deterministic(tmp_path: Path) -> None:
    first = build_and_capture(tmp_path / "a")
    second = build_and_capture(tmp_path / "b")
    assert canonical_json(first) == canonical_json(second)


def test_re_import_yields_identical_payloads(tmp_path: Path) -> None:
    """A second pipeline run over the same traces resolves identical ids/records."""
    ws, epoch_id = build_workspace(tmp_path)
    run_pipeline(ws, epoch_id)
    once = capture_payloads(ws, epoch_id)
    run_pipeline(ws, epoch_id)  # idempotent re-import (content-hash ids)
    twice = capture_payloads(ws, epoch_id)
    assert canonical_json(once) == canonical_json(twice)


# --- endpoint smoke (the HTTP layer drives the readers end-to-end) ----------


def test_endpoints_serve_the_readers(tmp_path: Path) -> None:
    from starlette.testclient import TestClient

    from zicato.dashboard.server import create_app

    ws, epoch_id = build_workspace(tmp_path)
    run_pipeline(ws, epoch_id)
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>z</title>", encoding="utf-8")
    app = create_app(ws, static_dir, read_only=True)

    with TestClient(app) as c:
        traces = c.get(f"/api/reflection/{REFLECTION_ID}/traces").json()
        assert traces["found"] is True and traces["trace_count"] >= 2
        trace_id = traces["traces"][0]["trace_id"]

        detail = c.get(f"/api/reflection/{REFLECTION_ID}/trace/{trace_id}").json()
        assert detail["found"] is True and detail["trace_id"] == trace_id and detail["turns"]

        from zicato.reflection.suggestions import read_suggestions

        sug = next(
            s
            for s in read_suggestions(ws, epoch_id, REFLECTION_ID)
            if isinstance(s.provenance.get("foreign_source"), dict)
        )
        prov = c.get(
            f"/api/reflection/{REFLECTION_ID}/suggestion/{sug.suggestion_id}/provenance"
        ).json()
        assert prov["found"] is True and prov["episodes"]

        # Malformed ids degrade to a same-shape 200 (never a 500).
        bad = c.get("/api/reflection/bad id/traces")
        assert bad.status_code == 200 and bad.json()["found"] is False


def test_committed_node_fixtures_are_current(tmp_path: Path) -> None:
    """The committed node fixtures match a fresh capture (composition-check, §4.1).

    If this fails, re-run ``tools/gen_trace_view_fixtures.py`` — the readers
    changed shape and the sibling view-agents' node tests must re-capture.
    """
    captured = build_and_capture(tmp_path)
    for name in ("list", "detail", "provenance"):
        committed = (_FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
        assert (
            canonical_json(captured[name]) == committed
        ), f"trace-view fixture {name}.json drifted — run tools/gen_trace_view_fixtures.py"
