"""Response-level snapshot harness for the table-driven dashboard read routes.

Every dashboard route that answers with a single query-library call is
declared once in :data:`zicato.dashboard.endpoints.READ_ENDPOINTS`. This
harness pins what those routes put on the wire: it serves the deterministic
multi-epoch fixture workspace (:func:`tests._reader_parity_harness.build_fixture_workspace`)
through the real ASGI application and records each probe's status code and
response body, with the key ORDER of the body preserved.

Two probes are declared per route carrying path coordinates: one with a
coordinate the fixture holds, and one with a coordinate the route's guard
rejects, which is what exercises the canned
degrade shape. Routes taking an optional ``?epoch=`` scope get a third probe
with a malformed scope, so the degrade the scope rejection serves is pinned
too, and the one route taking a required query parameter gets a third probe
without it.

The golden is captured from the code that predates the table and compared
after it, so a route whose status, body, or key order moved shows up as a
diff. Re-capture it only when a route's response is meant to change:

    ZICATO_ENDPOINT_SNAPSHOT_UPDATE=1 uv run pytest -q \\
        tests/test_dashboard_endpoint_table.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from tests._reader_parity_harness import build_fixture_workspace
from zicato.dashboard.server import create_app

#: A coordinate value every guard rejects. ``~`` is outside all three safe-id
#: alphabets, and it is an unreserved URL character, so it reaches the handler
#: unchanged rather than re-encoded or path-normalised on the way in, which
#: is what lets the probe reach the degrade it pins.
REJECTED = "bad~id"

#: One probe per row: ``(label, url)``. The label names the route and which
#: coordinate it was asked for, so a diff reads without decoding the URL.
ROUTE_PROBES: tuple[tuple[str, str], ...] = (
    # -- workspace-wide reads, no coordinate ---------------------------
    ("state", "/api/state"),
    ("workspace", "/api/workspace"),
    ("health_report", "/api/health-report"),
    ("active_runs", "/api/active-runs"),
    ("active_tournament", "/api/active-tournament"),
    ("heartbeat", "/api/heartbeat"),
    ("config", "/api/config"),
    ("live_pipeline", "/api/live/pipeline"),
    ("live_execution_plan", "/api/live/execution-plan"),
    ("proposer_recommendations", "/api/proposer/recommendations"),
    # -- reads taking the optional ?epoch= scope -----------------------
    ("epoch/current", "/api/epoch"),
    ("epoch/scoped", "/api/epoch?epoch=e1"),
    ("epoch/rejected-scope", f"/api/epoch?epoch={REJECTED}"),
    ("lineage/current", "/api/lineage"),
    ("lineage/scoped", "/api/lineage?epoch=e1"),
    ("lineage/rejected-scope", f"/api/lineage?epoch={REJECTED}"),
    ("score_trajectory/current", "/api/score-trajectory"),
    ("score_trajectory/scoped", "/api/score-trajectory?epoch=e1"),
    ("score_trajectory/rejected-scope", f"/api/score-trajectory?epoch={REJECTED}"),
    ("calibration_trend/current", "/api/calibration-trend"),
    ("calibration_trend/scoped", "/api/calibration-trend?epoch=e1"),
    ("calibration_trend/rejected-scope", f"/api/calibration-trend?epoch={REJECTED}"),
    ("tournaments/current", "/api/tournaments"),
    ("tournaments/scoped", "/api/tournaments?epoch=e1"),
    ("tournaments/rejected-scope", f"/api/tournaments?epoch={REJECTED}"),
    ("reflections/all", "/api/reflections"),
    ("reflections/scoped", "/api/reflections?epoch=e1"),
    ("reflections/rejected-scope", f"/api/reflections?epoch={REJECTED}"),
    ("proposer_scorecard/all", "/api/proposer/scorecard"),
    ("proposer_scorecard/scoped", "/api/proposer/scorecard?epoch=e1"),
    ("proposer_scorecard/rejected-scope", f"/api/proposer/scorecard?epoch={REJECTED}"),
    # -- epoch-coordinate reads ----------------------------------------
    ("per_judge_trend", "/api/epoch/e1/per-judge-trend"),
    ("per_judge_trend/rejected", f"/api/epoch/{REJECTED}/per-judge-trend"),
    ("epoch_trajectory", "/api/epoch/e1/trajectory"),
    ("epoch_trajectory/rejected", f"/api/epoch/{REJECTED}/trajectory"),
    ("epoch_cost", "/api/epoch/e1/cost"),
    ("epoch_cost/rejected", f"/api/epoch/{REJECTED}/cost"),
    ("racing_field", "/api/epoch/e1/racing-field"),
    ("racing_field/rejected", f"/api/epoch/{REJECTED}/racing-field"),
    ("round_timeline", "/api/epoch/e1/round-timeline"),
    ("round_timeline/rejected", f"/api/epoch/{REJECTED}/round-timeline"),
    ("execution_plan", "/api/epoch/e1/execution-plan"),
    ("execution_plan/rejected", f"/api/epoch/{REJECTED}/execution-plan"),
    ("experiments_ledger", "/api/epoch/e1/experiments-ledger"),
    ("experiments_ledger/rejected", f"/api/epoch/{REJECTED}/experiments-ledger"),
    ("contract_diff", "/api/contract-diff/e1"),
    ("contract_diff/rejected", f"/api/contract-diff/{REJECTED}"),
    ("epoch_journal", "/api/epoch/e1/journal"),
    ("epoch_journal/rejected", f"/api/epoch/{REJECTED}/journal"),
    ("epoch_analysis", "/api/epoch/e1/analysis"),
    ("epoch_analysis/rejected", f"/api/epoch/{REJECTED}/analysis"),
    ("eval_matrix", "/api/epoch/e1/evals"),
    ("eval_matrix/rejected", f"/api/epoch/{REJECTED}/evals"),
    ("eval_dossier", "/api/epoch/e1/eval/t1"),
    ("eval_dossier/rejected-epoch", f"/api/epoch/{REJECTED}/eval/t1"),
    ("eval_dossier/rejected-entry", f"/api/epoch/e1/eval/{REJECTED}"),
    ("eval_health", "/api/epoch/e1/eval-health"),
    ("eval_health/rejected", f"/api/epoch/{REJECTED}/eval-health"),
    ("judge_roster", "/api/epoch/e1/judge-roster"),
    ("judge_roster/rejected", f"/api/epoch/{REJECTED}/judge-roster"),
    # -- generation- and run-coordinate reads --------------------------
    ("per_judge_for_generation", "/api/generation/e1/v0/per-judge"),
    ("per_judge_for_generation/rejected", f"/api/generation/e1/{REJECTED}/per-judge"),
    ("per_entry_for_generation", "/api/generation/e1/v0/per-entry"),
    ("per_entry_for_generation/rejected", f"/api/generation/e1/{REJECTED}/per-entry"),
    ("per_judge_comparison", "/api/round/e1/v0/v1/per-judge-comparison"),
    ("per_judge_comparison/rejected", f"/api/round/e1/v0/{REJECTED}/per-judge-comparison"),
    ("per_judge_for_run", "/api/run/e1-v0-t1/per-judge"),
    ("per_judge_for_run/rejected", f"/api/run/{REJECTED}/per-judge"),
    ("per_judge_for_entry", "/api/run/e1/v0/t1/per-judge"),
    ("per_judge_for_entry/rejected", f"/api/run/e1/v0/{REJECTED}/per-judge"),
    ("run_expectations", "/api/run/e1/v0/t1/expectations"),
    ("run_expectations/rejected", f"/api/run/e1/v0/{REJECTED}/expectations"),
    ("run_header", "/api/run/e1/v0/t1/header"),
    ("run_header/rejected", f"/api/run/e1/v0/{REJECTED}/header"),
    ("hypothesis_accuracy", "/api/hypothesis-accuracy/e1/v0"),
    ("hypothesis_accuracy/rejected", f"/api/hypothesis-accuracy/e1/{REJECTED}"),
    # -- tournament reads ----------------------------------------------
    ("tournament_structure", "/api/tournament-structure/e1/v1"),
    ("tournament_structure/rejected", f"/api/tournament-structure/e1/{REJECTED}"),
    ("matchup_detail", "/api/tournaments/v0"),
    ("matchup_detail/rejected", f"/api/tournaments/{REJECTED}"),
    ("matchup_grid", "/api/matchup-grid/e1/v0/v1"),
    ("matchup_grid/rejected", f"/api/matchup-grid/e1/v0/{REJECTED}"),
    ("gate", "/api/round/e1/v0/v1/gate"),
    ("gate/rejected", f"/api/round/e1/v0/{REJECTED}/gate"),
    ("drift_movements", "/api/drift-movements/v0"),
    ("drift_movements/rejected", f"/api/drift-movements/{REJECTED}"),
    # -- reflection reads ------------------------------------------------
    ("reflection_summary", "/api/reflection/r1/summary"),
    ("reflection_summary/rejected", f"/api/reflection/{REJECTED}/summary"),
    ("reflection_scorecards", "/api/reflection/r1/scorecards"),
    ("reflection_scorecards/rejected", f"/api/reflection/{REJECTED}/scorecards"),
    ("reflection_practices", "/api/reflection/r1/practices"),
    ("reflection_practices/rejected", f"/api/reflection/{REJECTED}/practices"),
    ("reflection_xray", "/api/reflection/r1/xray/j1/v0:t1:r0"),
    ("reflection_xray/rejected-run-ref", f"/api/reflection/r1/xray/j1/{REJECTED}"),
    ("reflection_traces", "/api/reflection/r1/traces"),
    ("reflection_traces/rejected", f"/api/reflection/{REJECTED}/traces"),
    ("reflection_trace", "/api/reflection/r1/trace/tr1"),
    ("reflection_trace/rejected", f"/api/reflection/r1/trace/{REJECTED}"),
    ("suggestion_provenance", "/api/reflection/r1/suggestion/s1/provenance"),
    ("suggestion_provenance/rejected", f"/api/reflection/r1/suggestion/{REJECTED}/provenance"),
    # -- generation files and the mutation surface ---------------------
    ("files_index", "/api/files"),
    ("files_tree", "/api/files/e1/v0/tree"),
    ("files_tree/rejected", f"/api/files/e1/{REJECTED}/tree"),
    ("files_content", "/api/files/e1/v0/content?path=agent.py"),
    ("files_content/rejected", f"/api/files/e1/{REJECTED}/content?path=agent.py"),
    ("files_content/no-path", "/api/files/e1/v0/content"),
    ("files_patches", "/api/files/e1/v1/patches"),
    ("files_patches/rejected", f"/api/files/e1/{REJECTED}/patches"),
    ("files_diff", "/api/files/e1/v1/diff"),
    ("files_diff/rejected", f"/api/files/e1/{REJECTED}/diff"),
    ("mutation_index", "/api/mutations/e1"),
    ("mutation_index/rejected", f"/api/mutations/{REJECTED}"),
    ("mutation_detail", "/api/mutations/e1/instr"),
    ("mutation_detail/rejected", f"/api/mutations/e1/{REJECTED}"),
)

#: Keys whose value is read-time wall-clock noise rather than fixture data.
#: Replaced by a constant before comparison, the same masking the reader
#: parity harness applies.
_VOLATILE_KEYS = frozenset({"generated_at"})
_MASK = "<masked>"


def _mask(value: Any, root: str) -> Any:
    """Blank wall-clock noise and the per-run workspace path, in key order."""
    if isinstance(value, dict):
        return {
            key: (_MASK if key in _VOLATILE_KEYS else _mask(item, root))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask(item, root) for item in value]
    if isinstance(value, str):
        return value.replace(root, "<ws>")
    return value


def capture_route_snapshot(tmp_path: Path, static_dir: Path) -> dict[str, Any]:
    """Serve every probe against a fresh fixture workspace and record it.

    Each entry is ``{"status": <code>, "body": <masked JSON value>}``. The
    body keeps the order the handler emitted its keys in, so a reordering
    is a diff rather than a silent pass.
    """
    ws = build_fixture_workspace(tmp_path)
    root = str(ws)
    out: dict[str, Any] = {}
    with TestClient(create_app(ws, static_dir, read_only=True)) as client:
        for label, url in ROUTE_PROBES:
            response = client.get(url)
            try:
                body = json.loads(response.text)
            except json.JSONDecodeError:
                body = response.text
            out[label] = {"status": response.status_code, "body": _mask(body, root)}
    return out


def snapshot_text(snapshot: dict[str, Any]) -> str:
    """Encode a snapshot for the golden file, preserving body key order."""
    return json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
