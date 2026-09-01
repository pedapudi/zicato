"""Tests for the structured promote-gate breakdown endpoint.

``GET /api/round/{epoch_id}/{champion}/{challenger}/gate`` decomposes the
authoritative :func:`zicato.tournament.gate.evaluate_gate` verdict into its
ordered rules. These tests build a minimal ``.zicato/`` workspace with the
on-disk ``scoring.json`` + per-generation ``gen_score.json`` aggregates the
reader consumes, then assert the endpoint agrees with what the gate would
decide — a promotion, a scalar-margin rejection, and a pass-rate
monotonicity rejection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from zicato.dashboard.server import create_app
from zicato.query import WorkspacePaths, build_gate_breakdown

EPOCH_ID = "2026-05-28_e0"


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _gen_score(
    *,
    scalar: float,
    pass_rate: float,
    per_entry: dict[str, dict[str, object]],
    namespace_aggregates: dict[str, float] | None = None,
    scalar_components: dict[str, float] | None = None,
    scalar_provenance: object = "__unset__",
) -> dict[str, object]:
    score: dict[str, object] = {
        "scalar": scalar,
        "pass_rate": pass_rate,
        "per_entry": per_entry,
        "scalar_components": scalar_components or {"drift": scalar, "pass": 0.0},
    }
    if namespace_aggregates is not None:
        score["namespace_aggregates"] = namespace_aggregates
    # ``"__unset__"`` (the default) writes NO ``scalar_provenance`` key — the
    # pre-#19 / back-compat shape. ``None`` writes an explicit null; a string
    # writes the recorded Seam-2 token.
    if scalar_provenance != "__unset__":
        score["scalar_provenance"] = scalar_provenance
    return score


def _write_loss(
    ws: Path, generation_id: str, entry_id: str, *, scoring_provenance: object = "__unset__"
) -> None:
    """Write a minimal per-run ``loss.json`` carrying a Seam-1 token.

    Lets the decomposition tests seed the per-run drift provenance the gate
    breakdown reads off the generation's ``runs/{entry}/loss.json`` files.
    """
    loss: dict[str, object] = {"entry_id": entry_id, "drift_loss": 0.3, "pass_fail": True}
    if scoring_provenance != "__unset__":
        loss["scoring_provenance"] = scoring_provenance
    _write_json(
        ws / "epochs" / EPOCH_ID / "generations" / generation_id / "runs" / entry_id / "loss.json",
        loss,
    )


def _make_workspace(
    tmp_path: Path,
    *,
    champion: dict[str, object],
    challenger: dict[str, object],
    scoring: dict[str, object] | None = None,
) -> Path:
    ws = tmp_path / ".zicato"
    epoch_dir = ws / "epochs" / EPOCH_ID
    (ws).mkdir(parents=True, exist_ok=True)
    (ws / "current_epoch").write_text(EPOCH_ID, encoding="utf-8")
    _write_json(epoch_dir / "scoring.json", scoring or {})
    _write_json(epoch_dir / "generations" / "v0" / "gen_score.json", champion)
    _write_json(epoch_dir / "generations" / "v1" / "gen_score.json", challenger)
    return ws


def _client(ws: Path, static_dir: Path) -> TestClient:
    return TestClient(create_app(ws, static_dir, read_only=True))


# ---------------------------------------------------------------------------
# Promoted: every rule passes.
# ---------------------------------------------------------------------------


def test_gate_promoted_all_rules_pass(tmp_path: Path) -> None:
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.5, "pass_fail": True}},
        namespace_aggregates={"drift:": 0.5, "cost:": 0.2},
    )
    challenger = _gen_score(
        scalar=0.30,  # clear improvement, beats margin 0.01
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.3, "pass_fail": True}},
        namespace_aggregates={"drift:": 0.3, "cost:": 0.1},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01, "pass_rate_monotonicity": True},
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    assert result["decision"] == "promoted"
    assert result["reason"] == ""
    assert result["delta_scalar"] == pytest.approx(-0.20)
    rules = {r["id"]: r for r in result["rules"]}
    assert [r["id"] for r in result["rules"]] == [
        "regression_suite",
        "scalar_margin",
        "pass_rate_monotonicity",
        "namespace_monotonicity",
    ]
    # regression disabled by default -> skipped, not fired.
    assert rules["regression_suite"]["status"] == "skipped"
    assert rules["regression_suite"]["fired"] is False
    assert rules["scalar_margin"]["status"] == "pass"
    assert rules["scalar_margin"]["fired"] is False
    assert rules["pass_rate_monotonicity"]["status"] == "pass"
    # Default namespace_monotonicity has at least one tracked namespace; it
    # is satisfied here (challenger improved on every namespace).
    assert rules["namespace_monotonicity"]["status"] in {"pass", "disabled"}
    assert all(r["fired"] is False for r in result["rules"])
    # STRUCTURED decision surface: nothing fired, margin is echoed verbatim.
    assert result["deciding_rule"] is None
    assert result["margin"] == pytest.approx(0.01)
    assert result["regressed_predicate"] is None
    assert result["regressed_namespace"] is None


# ---------------------------------------------------------------------------
# Scalar-margin rejection: scalar fires, later rules not_reached.
# ---------------------------------------------------------------------------


def test_gate_scalar_margin_rejection(tmp_path: Path) -> None:
    champion = _gen_score(
        scalar=47.58,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 47.58, "pass_fail": True}},
        namespace_aggregates={"drift:": 47.58},
    )
    challenger = _gen_score(
        scalar=57.70,  # loss ROSE — regressed
        pass_rate=0.5,  # also a pass-rate regression, but scalar fires first
        per_entry={"e1": {"drift_loss": 57.70, "pass_fail": False}},
        namespace_aggregates={"drift:": 57.70},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01, "pass_rate_monotonicity": True},
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    assert result["decision"] == "rejected"
    assert "challenger regressed" in result["reason"]
    assert result["delta_scalar"] == pytest.approx(10.12)
    rules = {r["id"]: r for r in result["rules"]}
    assert rules["scalar_margin"]["status"] == "fail"
    assert rules["scalar_margin"]["fired"] is True
    assert "47.58 → 57.70" in rules["scalar_margin"]["detail"]
    assert "+10.12" in rules["scalar_margin"]["detail"]
    # Rules AFTER the fired one are not_reached, never fired.
    assert rules["pass_rate_monotonicity"]["status"] == "not_reached"
    assert rules["pass_rate_monotonicity"]["fired"] is False
    assert rules["namespace_monotonicity"]["status"] == "not_reached"
    # Only one rule fired.
    assert sum(1 for r in result["rules"] if r["fired"]) == 1
    # STRUCTURED decision surface: the server NAMES the fired rule + margin so
    # the frontend never re-infers it from the rule list / detail strings.
    assert result["deciding_rule"] == "scalar_margin"
    assert result["margin"] == pytest.approx(0.01)
    assert result["regressed_predicate"] is None
    assert result["regressed_namespace"] is None


# ---------------------------------------------------------------------------
# Pass-rate monotonicity rejection: scalar passes, monotonicity fires.
# ---------------------------------------------------------------------------


def test_gate_pass_rate_monotonicity_rejection(tmp_path: Path) -> None:
    # Challenger's scalar improves (so scalar_margin passes) but an entry
    # the champion passed now fails -> pass-rate monotonicity rejects.
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={
            "entry_x": {"drift_loss": 0.5, "pass_fail": True},
            "entry_y": {"drift_loss": 0.5, "pass_fail": True},
        },
        namespace_aggregates={"drift:": 0.5},
    )
    challenger = _gen_score(
        scalar=0.20,  # improved scalar
        pass_rate=0.5,
        per_entry={
            "entry_x": {"drift_loss": 0.2, "pass_fail": False},  # regressed
            "entry_y": {"drift_loss": 0.2, "pass_fail": True},
        },
        namespace_aggregates={"drift:": 0.2},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01, "pass_rate_monotonicity": True},
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    assert result["decision"] == "rejected"
    assert "pass-rate regression" in result["reason"]
    rules = {r["id"]: r for r in result["rules"]}
    # Scalar passed.
    assert rules["scalar_margin"]["status"] == "pass"
    assert rules["scalar_margin"]["fired"] is False
    # Pass-rate monotonicity fired.
    assert rules["pass_rate_monotonicity"]["status"] == "fail"
    assert rules["pass_rate_monotonicity"]["fired"] is True
    assert "entry_x" in rules["pass_rate_monotonicity"]["detail"]
    assert "entry_y" not in rules["pass_rate_monotonicity"]["detail"]
    # The later namespace rule is not reached.
    assert rules["namespace_monotonicity"]["status"] == "not_reached"
    # STRUCTURED decision surface: the fired monotonicity rule NAMES its
    # regressed predicate — the frontend never regex-scrapes the detail.
    assert result["deciding_rule"] == "pass_rate_monotonicity"
    assert result["regressed_predicate"] == "entry_x"
    assert result["regressed_namespace"] is None


def test_gate_aggregate_scope_promotes_and_renders_rate_delta(tmp_path: Path) -> None:
    """Under aggregate scope (issue #17) a reshuffled-but-net-neutral
    challenger promotes, and the pass-rate rule renders the overall
    pass-rate delta rather than a per-entry regressed list."""
    champion = _gen_score(
        scalar=0.50,
        pass_rate=0.5,
        per_entry={
            "entry_x": {"drift_loss": 0.5, "pass_fail": True},
            "entry_y": {"drift_loss": 0.5, "pass_fail": False},
        },
        namespace_aggregates={"drift:": 0.5},
    )
    challenger = _gen_score(
        scalar=0.20,  # improved scalar
        pass_rate=0.5,  # net pass-rate held: x flipped off, y flipped on
        per_entry={
            "entry_x": {"drift_loss": 0.2, "pass_fail": False},
            "entry_y": {"drift_loss": 0.2, "pass_fail": True},
        },
        namespace_aggregates={"drift:": 0.2},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={
            "promote_margin": 0.01,
            "pass_rate_monotonicity": True,
            "pass_rate_monotonicity_scope": "aggregate",
        },
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    assert result["decision"] == "promoted"
    rules = {r["id"]: r for r in result["rules"]}
    pass_rule = rules["pass_rate_monotonicity"]
    assert pass_rule["status"] == "pass"
    assert pass_rule["fired"] is False
    # Aggregate detail mentions the overall pass-rate, NOT a regressed id list.
    assert "overall" in pass_rule["detail"]
    assert "aggregate scope" in pass_rule["detail"]
    assert "entry_x" not in pass_rule["detail"]


def test_gate_aggregate_scope_fires_on_net_regression(tmp_path: Path) -> None:
    """A genuine overall pass-rate drop under aggregate scope fires the rule
    and renders the pass-rate delta detail."""
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={
            "entry_x": {"drift_loss": 0.5, "pass_fail": True},
            "entry_y": {"drift_loss": 0.5, "pass_fail": True},
        },
        namespace_aggregates={"drift:": 0.5},
    )
    challenger = _gen_score(
        scalar=0.20,  # improved scalar, but pass-rate fell 1.0 -> 0.5
        pass_rate=0.5,
        per_entry={
            "entry_x": {"drift_loss": 0.2, "pass_fail": False},
            "entry_y": {"drift_loss": 0.2, "pass_fail": True},
        },
        namespace_aggregates={"drift:": 0.2},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={
            "promote_margin": 0.01,
            "pass_rate_monotonicity": True,
            "pass_rate_monotonicity_scope": "aggregate",
        },
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    assert result["decision"] == "rejected"
    assert "overall pass-rate fell by" in result["reason"]
    rules = {r["id"]: r for r in result["rules"]}
    pass_rule = rules["pass_rate_monotonicity"]
    assert pass_rule["status"] == "fail"
    assert pass_rule["fired"] is True
    assert "overall" in pass_rule["detail"]
    # The later namespace rule is not reached.
    assert rules["namespace_monotonicity"]["status"] == "not_reached"


# ---------------------------------------------------------------------------
# Endpoint wiring + degradation.
# ---------------------------------------------------------------------------


def test_gate_endpoint_serves_breakdown(tmp_path: Path, static_dir: Path) -> None:
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.5, "pass_fail": True}},
        namespace_aggregates={"drift:": 0.5},
    )
    challenger = _gen_score(
        scalar=0.30,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.3, "pass_fail": True}},
        namespace_aggregates={"drift:": 0.3},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01},
    )
    with _client(ws, static_dir) as client:
        r = client.get(f"/api/round/{EPOCH_ID}/v0/v1/gate")
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "promoted"
        assert [rule["id"] for rule in body["rules"]] == [
            "regression_suite",
            "scalar_margin",
            "pass_rate_monotonicity",
            "namespace_monotonicity",
        ]
        assert "champion" in body["scalar_components"]
        assert "primary_driver" in body


def test_gate_endpoint_invalid_id_degrades(tmp_path: Path, static_dir: Path) -> None:
    ws = _make_workspace(
        tmp_path,
        champion=_gen_score(scalar=0.5, pass_rate=1.0, per_entry={}),
        challenger=_gen_score(scalar=0.3, pass_rate=1.0, per_entry={}),
    )
    with _client(ws, static_dir) as client:
        r = client.get(f"/api/round/{EPOCH_ID}/bad%20id/v1/gate")
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "deferred"
        assert body["rules"] == []


def test_gate_missing_aggregate_degrades_to_unknown(tmp_path: Path) -> None:
    # No gen_score.json for the challenger -> scalar unknown, no crash.
    ws = tmp_path / ".zicato"
    (ws / "epochs" / EPOCH_ID).mkdir(parents=True)
    (ws / "current_epoch").write_text(EPOCH_ID, encoding="utf-8")
    _write_json(ws / "epochs" / EPOCH_ID / "scoring.json", {})
    _write_json(
        ws / "epochs" / EPOCH_ID / "generations" / "v0" / "gen_score.json",
        _gen_score(scalar=0.5, pass_rate=1.0, per_entry={}),
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert result["decision"] == "deferred"
    rules = {r["id"]: r for r in result["rules"]}
    assert rules["scalar_margin"]["status"] == "unknown"
    assert all(r["fired"] is False for r in result["rules"])
    # The present side's absolute scalar still surfaces; the missing side is
    # None, and a settled round carries no live overlay.
    assert result["champion_scalar"] == pytest.approx(0.5)
    assert result["challenger_scalar"] is None
    assert result["live"] is None


# ---------------------------------------------------------------------------
# Absolute scalars + live projected-standing overlay (additive projection).
# ---------------------------------------------------------------------------


def test_gate_emits_absolute_scalars_settled_round(tmp_path: Path) -> None:
    """A settled round carries both absolute scalars and no live overlay."""
    champion = _gen_score(
        scalar=47.58,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 47.58, "pass_fail": True}},
        namespace_aggregates={"drift:": 47.58},
    )
    challenger = _gen_score(
        scalar=57.70,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 57.70, "pass_fail": True}},
        namespace_aggregates={"drift:": 57.70},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01},
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    assert result["champion_scalar"] == pytest.approx(47.58)
    assert result["challenger_scalar"] == pytest.approx(57.70)
    # No active tournament on disk -> no live overlay.
    assert result["live"] is None


def test_gate_overlays_live_projected_challenger(tmp_path: Path) -> None:
    """While THIS round is in flight, the gate block carries the challenger's
    live projected absolute + board progress from the active tournament."""
    from zicato.runtime.state import ActiveTournament, write_active_tournament

    champion = _gen_score(
        scalar=10.0,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 10.0, "pass_fail": True}},
    )
    challenger = _gen_score(
        scalar=12.0,  # settled-so-far value on disk
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 12.0, "pass_fail": True}},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01},
    )
    write_active_tournament(
        ws,
        ActiveTournament(
            tournament_id="t-live",
            parent_generation_id="v0",
            child_generation_id="v1",
            epoch_id=EPOCH_ID,
            started_at="2026-05-28T00:30:00Z",
            phase="running",
            projected={
                "v1": {
                    "scalar": 9.8,
                    "boards_done": 6,
                    "boards_total": 8,
                    "pass_rate": 1.0,
                },
            },
        ),
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    # The settled absolutes are still the on-disk aggregates…
    assert result["champion_scalar"] == pytest.approx(10.0)
    assert result["challenger_scalar"] == pytest.approx(12.0)
    # …and the live overlay carries the PROJECTED challenger absolute + boards.
    assert result["live"] == {
        "challenger_scalar": pytest.approx(9.8),
        "boards_done": 6,
        "boards_total": 8,
    }


def test_gate_live_overlay_ignores_unrelated_tournament(tmp_path: Path) -> None:
    """An active tournament for a DIFFERENT challenger never bleeds its
    projected standing into a settled historical round's breakdown."""
    from zicato.runtime.state import ActiveTournament, write_active_tournament

    champion = _gen_score(
        scalar=10.0,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 10.0, "pass_fail": True}},
    )
    challenger = _gen_score(
        scalar=12.0,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 12.0, "pass_fail": True}},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01},
    )
    # A live tournament, but for a different challenger (v2) — the historical
    # v0->v1 breakdown must not pick up v2's projection.
    write_active_tournament(
        ws,
        ActiveTournament(
            tournament_id="t-live",
            parent_generation_id="v1",
            child_generation_id="v2",
            epoch_id=EPOCH_ID,
            started_at="2026-05-28T00:30:00Z",
            phase="running",
            projected={"v2": {"scalar": 5.0, "boards_done": 3, "boards_total": 8}},
        ),
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert result["live"] is None


def test_gate_endpoint_carries_scalars_and_live_keys(tmp_path: Path, static_dir: Path) -> None:
    """The HTTP gate route always carries the additive scalar + live keys."""
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.5, "pass_fail": True}},
    )
    challenger = _gen_score(
        scalar=0.30,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.3, "pass_fail": True}},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01},
    )
    with _client(ws, static_dir) as client:
        r = client.get(f"/api/round/{EPOCH_ID}/v0/v1/gate")
        assert r.status_code == 200
        body = r.json()
        assert body["champion_scalar"] == pytest.approx(0.50)
        assert body["challenger_scalar"] == pytest.approx(0.30)
        assert body["live"] is None
        # The unsafe-id degrade path keeps the same shape.
        bad = client.get(f"/api/round/{EPOCH_ID}/bad%20id/v1/gate").json()
        assert bad["champion_scalar"] is None
        assert bad["challenger_scalar"] is None
        assert bad["live"] is None


# ---------------------------------------------------------------------------
# Scoring provenance decomposition (#19 phase 4).
# ---------------------------------------------------------------------------


def test_provenance_parser_token_shapes() -> None:
    """The parser decomposes every documented token shape (incl. fail-open)."""
    from zicato.query import _parse_scoring_provenance

    # None / builtin -> quiet built-in (None additionally marks present=False).
    none_view = _parse_scoring_provenance(None)
    assert none_view["kind"] == "builtin"
    assert none_view["present"] is False
    assert none_view["fail_open"] is False
    builtin_view = _parse_scoring_provenance("builtin")
    assert builtin_view["kind"] == "builtin"
    assert builtin_view["present"] is True

    # Seam-2 pass transform.
    pass_view = _parse_scoring_provenance("transform:pass=pow(2.0)")
    assert pass_view["kind"] == "transform"
    assert pass_view["source"] == "pow(2.0)"
    assert pass_view["fail_open"] is False

    # Seam-1 drift transform with two reshaped kinds.
    drift_view = _parse_scoring_provenance(
        "transform:drift{looping_reasoning=harmonic, off_topic=cap(5)}"
    )
    assert drift_view["kind"] == "transform"
    kinds = {t["kind"]: t["op"] for t in drift_view["transforms"]}
    assert kinds == {"looping_reasoning": "harmonic", "off_topic": "cap(5)"}

    # Dotted-spec plugins.
    plugin_view = _parse_scoring_provenance("plugin:scalar_fn=mypkg.contract.scoring:my_scalar")
    assert plugin_view["kind"] == "plugin"
    assert plugin_view["source"] == "mypkg.contract.scoring:my_scalar"
    assert plugin_view["seam"] == "scalar_fn"


def test_provenance_parser_flags_fail_open() -> None:
    """A ``(fallback: …)`` token is flagged fail-open with its reason, while
    the underlying pre-plugin token is still classified."""
    from zicato.query import _parse_scoring_provenance

    view = _parse_scoring_provenance("builtin (fallback: raised ValueError)")
    assert view["fail_open"] is True
    assert view["fallback_reason"] == "raised ValueError"
    # The pre-plugin value here was the built-in.
    assert view["kind"] == "builtin"

    # A transform that a plugin tried to wrap, then failed open over.
    view2 = _parse_scoring_provenance("transform:pass=pow(2.0) (fallback: non-finite return)")
    assert view2["fail_open"] is True
    assert view2["fallback_reason"] == "non-finite return"
    assert view2["kind"] == "transform"
    assert view2["source"] == "pow(2.0)"


def test_gate_breakdown_surfaces_scalar_decomposition(tmp_path: Path) -> None:
    """build_gate_breakdown carries a scalar_decomposition parsed from the
    recorded Seam-1 (loss.json) + Seam-2 (gen_score.json) provenance."""
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.5, "pass_fail": True}},
        scalar_provenance="builtin",
    )
    challenger = _gen_score(
        scalar=0.30,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.3, "pass_fail": True}},
        scalar_provenance="transform:pass=pow(2.0)",
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01},
    )
    # Challenger drift was reshaped by a harmonic transform on one kind.
    _write_loss(
        ws,
        "v1",
        "e1",
        scoring_provenance="transform:drift{looping_reasoning=harmonic}",
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    decomp = result["scalar_decomposition"]
    assert decomp["present"] is True
    assert decomp["fail_open"] is False
    # Challenger's pass term came from a pow transform; its drift from a
    # harmonic drift transform.
    chall = decomp["challenger"]
    assert chall["scalar"]["kind"] == "transform"
    assert chall["scalar"]["source"] == "pow(2.0)"
    assert chall["drift"]["kind"] == "transform"
    assert chall["drift"]["transforms"][0]["kind"] == "looping_reasoning"
    # Champion was plain built-in — present but quiet.
    assert decomp["champion"]["scalar"]["kind"] == "builtin"


def test_gate_breakdown_flags_fail_open_event(tmp_path: Path) -> None:
    """A fired plugin that failed open is flagged on the decomposition as a
    first-class caution signal."""
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.5, "pass_fail": True}},
        scalar_provenance="builtin",
    )
    challenger = _gen_score(
        scalar=0.30,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.3, "pass_fail": True}},
        # The Seam-2 scalar_fn plugin RAISED and fell back to the built-in.
        scalar_provenance="builtin (fallback: raised ValueError)",
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01},
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    decomp = result["scalar_decomposition"]
    assert decomp["fail_open"] is True
    chall_scalar = decomp["challenger"]["scalar"]
    assert chall_scalar["fail_open"] is True
    assert chall_scalar["fallback_reason"] == "raised ValueError"


def test_gate_breakdown_decomposition_backcompat_none(tmp_path: Path) -> None:
    """A pre-#19 run (no provenance recorded anywhere) yields a clean
    decomposition with present=False — the UI renders nothing new."""
    champion = _gen_score(
        scalar=0.50,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.5, "pass_fail": True}},
    )  # no scalar_provenance key
    challenger = _gen_score(
        scalar=0.30,
        pass_rate=1.0,
        per_entry={"e1": {"drift_loss": 0.3, "pass_fail": True}},
    )
    ws = _make_workspace(
        tmp_path,
        champion=champion,
        challenger=challenger,
        scoring={"promote_margin": 0.01},
    )
    # No loss.json files written either -> no Seam-1 token.
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")

    decomp = result["scalar_decomposition"]
    assert decomp["present"] is False
    assert decomp["fail_open"] is False
    # Both sides still classify as built-in (quiet), present=False per seam.
    assert decomp["champion"]["scalar"]["present"] is False
    assert decomp["champion"]["scalar"]["kind"] == "builtin"


# ---------------------------------------------------------------------------
# Operator override block (gate.override).
# ---------------------------------------------------------------------------


def _write_experiment_outcome(ws: Path, generation_id: str, outcome: dict[str, object]) -> None:
    """Write a challenger's experiment.json with an ``outcome`` block."""
    path = ws / "epochs" / EPOCH_ID / "generations" / generation_id / "experiment.json"
    _write_json(path, {"outcome": outcome})


def test_gate_override_block_absent_without_override(tmp_path: Path) -> None:
    """A gate-decided pair reports override.present=False (back-compat clean)."""
    champion = _gen_score(
        scalar=0.50, pass_rate=1.0, per_entry={"e1": {"drift_loss": 0.5, "pass_fail": True}}
    )
    challenger = _gen_score(
        scalar=0.30, pass_rate=1.0, per_entry={"e1": {"drift_loss": 0.3, "pass_fail": True}}
    )
    ws = _make_workspace(
        tmp_path, champion=champion, challenger=challenger, scoring={"promote_margin": 0.01}
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert result["override"] == {"present": False, "action": None, "reason": None}


def test_gate_override_block_promote(tmp_path: Path) -> None:
    """A force-promoted challenger surfaces override.present=True + action."""
    champion = _gen_score(
        scalar=0.50, pass_rate=1.0, per_entry={"e1": {"drift_loss": 0.5, "pass_fail": True}}
    )
    challenger = _gen_score(
        scalar=0.80, pass_rate=1.0, per_entry={"e1": {"drift_loss": 0.8, "pass_fail": True}}
    )
    ws = _make_workspace(
        tmp_path, champion=champion, challenger=challenger, scoring={"promote_margin": 0.01}
    )
    _write_experiment_outcome(
        ws,
        "v1",
        {
            "tournament_decision": "promoted",
            "operator_override": True,
            "operator_override_reason": "prefer the diverse idea",
        },
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert result["override"]["present"] is True
    assert result["override"]["action"] == "promote"
    assert result["override"]["reason"] == "prefer the diverse idea"


def test_gate_override_block_reject(tmp_path: Path) -> None:
    """A force-rejected challenger surfaces override.action == reject."""
    champion = _gen_score(
        scalar=0.50, pass_rate=1.0, per_entry={"e1": {"drift_loss": 0.5, "pass_fail": True}}
    )
    challenger = _gen_score(
        scalar=0.30, pass_rate=1.0, per_entry={"e1": {"drift_loss": 0.3, "pass_fail": True}}
    )
    ws = _make_workspace(
        tmp_path, champion=champion, challenger=challenger, scoring={"promote_margin": 0.01}
    )
    _write_experiment_outcome(
        ws,
        "v1",
        {
            "tournament_decision": "rejected",
            "operator_override": True,
            "operator_override_reason": "regression risk",
        },
    )
    result = build_gate_breakdown(WorkspacePaths(ws), EPOCH_ID, "v0", "v1")
    assert result["override"]["present"] is True
    assert result["override"]["action"] == "reject"
    assert result["override"]["reason"] == "regression risk"
