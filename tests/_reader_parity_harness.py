"""Shared scaffolding for the dashboard-reader byte-identical-except-ordering
harness.

Builds a deterministic MULTI-EPOCH fixture workspace that mirrors the real
epoch-ordering bug (an epoch whose directory name sorts BEFORE the others but
whose recorded ``created_at`` is LATER, plus an EMPTY epoch with no
generations), then captures every public ``build_*`` reader response into a
single canonical-JSON snapshot.

The harness is the oracle for the workspace-layer migration: capture the
snapshot BEFORE migrating the readers, capture it again AFTER, and assert
that every NON-epoch-list response is byte-identical and every
epoch-list-bearing response carries the same SET of epochs + identical
per-epoch content, with the order now equal to the canonical
timestamp-first ``list_epoch_ids`` order (the intended fix).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zicato import query as sr

# ---------------------------------------------------------------------------
# The multi-epoch fixture
# ---------------------------------------------------------------------------
#
# Chronological (created_at) order — the canonical/correct order:
#     e1 (Jan)  ->  e2 (Feb, EMPTY)  ->  e0 (Mar)
#
# Numeric/name order — the WRONG order the buggy sites produce:
#     e0  ->  e1  ->  e2
#
# ``e0`` is the bug mirror: its name sorts FIRST but it was created LAST.
# ``e2`` is the empty epoch (no generations).

_EPOCHS: list[dict[str, Any]] = [
    {
        "id": "e1",
        "created_at": "2026-01-01T00:00:00Z",
        "closed": True,
        "goal": "First epoch chronologically.",
        "gens": ["v0", "v1"],
    },
    {
        "id": "e2",
        "created_at": "2026-02-01T00:00:00Z",
        "closed": True,
        "goal": "Empty epoch — no generations.",
        "gens": [],  # the EMPTY epoch
    },
    {
        "id": "e0",
        "created_at": "2026-03-01T00:00:00Z",  # LATEST despite the smallest id
        "closed": False,
        "goal": "Name sorts first, created last — the bug mirror.",
        "gens": ["v0"],
    },
]

# The canonical timestamp-first order the fix must produce everywhere.
CANONICAL_EPOCH_ORDER = ["e1", "e2", "e0"]


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_fixture_workspace(tmp_path: Path) -> Path:
    """Materialize the multi-epoch fixture under ``tmp_path/.zicato``.

    Returns the workspace root (the inner ``.zicato`` directory). The set
    of files is the canonical-read surface the dashboard reads: per-epoch
    ``config.json`` / ``board.jsonl`` / ``scoring.json`` / ``brief.md`` /
    ``contract_components.json`` and per-generation ``experiment.json`` /
    ``gen_score.json`` / ``runs/<entry>/loss.json``.
    """
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True, exist_ok=True)

    # Workspace-level harness config (read by ``_read_harness``).
    _write_json(
        ws / "config.json",
        {
            "adapter": {
                "entrypoint": "pkg.module:agent",
                "mutable_trees": ["src/pkg"],
            }
        },
    )

    # The current epoch is the chronologically-latest one (e0).
    _write_text(ws / "current_epoch", "e0\n")

    for order, spec in enumerate(_EPOCHS):
        eid = spec["id"]
        edir = ws / "epochs" / eid
        _write_json(
            edir / "config.json",
            {
                "id": eid,
                "created_at": spec["created_at"],
                "closed": spec["closed"],
                "goal": spec["goal"],
                "contract_hash": f"hash-{eid}",
            },
        )
        _write_text(edir / "brief.md", f"# Brief {eid}\n\n## Goal\n\n{spec['goal']}\n")
        # A frozen scoring block carrying a (deterministic) tournament
        # structure so the meta-loop ledger surfaces a structure token.
        _write_json(
            edir / "scoring.json",
            {
                "drift_weight": 1.0,
                "pass_weight": 1.0,
                "tournament": {"structure": "gauntlet", "params": {}},
            },
        )
        # Per-component contract sub-hashes; vary one component per epoch so
        # the contract-diff / ledger change-maps are non-trivial.
        _write_json(
            edir / "contract_components.json",
            {
                "board": f"board-{order}",
                "brief": f"brief-{eid}",
                "scoring": "scoring-const",
                "entrypoint": "entry-const",
                "mutable_trees": "trees-const",
                "proposer": "proposer-const",
            },
        )
        # Board: a meta header + two entries.
        _write_text(
            edir / "board.jsonl",
            "\n".join(
                [
                    json.dumps({"board_meta": True, "disable_drift": False}),
                    json.dumps(
                        {
                            "id": "t1",
                            "kind": "single_turn",
                            "input": "Say hello.",
                            "expectation": {"kind": "rubric"},
                            "weight": 1.0,
                            "tags": ["smoke"],
                        }
                    ),
                    json.dumps(
                        {
                            "id": "t2",
                            "kind": "single_turn",
                            "input": "Say goodbye.",
                            "expectation": {"kind": "predicate"},
                            "weight": 2.0,
                            "tags": [],
                        }
                    ),
                ]
            )
            + "\n",
        )

        for gi, gid in enumerate(spec["gens"]):
            gdir = edir / "generations" / gid
            promoted = gi == 0
            _write_json(
                gdir / "experiment.json",
                {
                    "generation_id": gid,
                    "parent_generation_id": None if gid == "v0" else "v0",
                    "proposed_at": spec["created_at"],
                    "hypothesis": {"summary": f"hyp {eid} {gid}"},
                    "outcome": {
                        "decision": "promoted" if promoted else "rejected",
                        "scalar_score_delta": -0.05 if promoted else 0.02,
                    },
                },
            )
            _write_json(
                gdir / "gen_score.json",
                {"scalar": 0.5 - 0.01 * gi, "pass_rate": 1.0},
            )
            for entry in ("t1", "t2"):
                _write_json(
                    gdir / "runs" / entry / "loss.json",
                    {
                        "entry_id": entry,
                        "run_id": f"{eid}-{gid}-{entry}",
                        "drift_loss": 0.3 + 0.01 * gi,
                        "pass_fail": True,
                        "score": 0.9,
                    },
                )

    _write_json(
        ws / "lineage.json",
        {
            "epochs": [
                {
                    "id": spec["id"],
                    "generations": [
                        {
                            "id": gid,
                            "parent_id": None if gid == "v0" else "v0",
                            "promoted": gi == 0,
                            "created_at": spec["created_at"],
                        }
                        for gi, gid in enumerate(spec["gens"])
                    ],
                }
                for spec in _EPOCHS
            ]
        },
    )

    return ws


# ---------------------------------------------------------------------------
# Capture every public build_* response
# ---------------------------------------------------------------------------
#
# Each entry: name -> a thunk producing the response from a paths handle.
# ``epoch_list`` flags the responses whose epoch ordering the fix corrects.


def capture_snapshot(ws: Path) -> dict[str, Any]:
    """Capture every public reader response for the fixture workspace.

    Returns a JSON-serializable dict mapping a response label to its value.
    Per-epoch / per-generation scoped responses are captured for EACH epoch
    (and its generations) so the snapshot exercises the leaf path-readers as
    well as the workspace-wide enumerations.
    """
    paths = sr.WorkspacePaths(ws)
    snap: dict[str, Any] = {}

    # --- workspace-wide (epoch-list-bearing where noted) -------------------
    snap["workspace_view"] = sr.build_workspace_view(paths)
    snap["epochs_summary"] = sr.build_epochs_summary(paths)
    snap["lineage_view"] = sr.build_lineage_view(paths)
    snap["meta_loop_ledger"] = sr.build_meta_loop_ledger(paths)
    snap["health_report"] = sr.build_health_report(paths)
    snap["snapshot"] = sr.build_snapshot(paths)
    snap["run_log"] = sr.build_run_log(paths, limit=50)
    snap["environment"] = sr.build_environment(paths)
    snap["search_empty"] = sr.build_search_results(paths, "")
    snap["search_hello"] = sr.build_search_results(paths, "hello")

    # --- per-epoch scoped ---------------------------------------------------
    for eid in CANONICAL_EPOCH_ORDER:
        snap[f"epoch_view::{eid}"] = sr.build_epoch_view(paths, eid)
        snap[f"bracket::{eid}"] = sr.build_bracket(paths, eid)
        snap[f"contract_diff::{eid}"] = sr.build_contract_diff(paths, eid)
        snap[f"tournament_structure::{eid}"] = sr.build_tournament_structure(paths, eid, "v1")
        snap[f"score_trajectory::{eid}"] = sr.build_score_trajectory(paths, eid)
        snap[f"calibration_trend::{eid}"] = sr.build_calibration_trend(paths, eid)
        snap[f"per_judge_trend::{eid}"] = sr.build_per_judge_trend(paths, eid)
        # The execution plan is served by the Python service alone; the
        # Rust supervisor answers its standard empty shape until it grows
        # the route (DQ8). Pinning the plan here — including for the EMPTY
        # fixture epoch, which is the shape a client paints in that case —
        # is what keeps the two servers' answers from skewing silently.
        snap[f"execution_plan::{eid}"] = sr.build_execution_plan(paths, eid)

    # --- per-generation scoped ---------------------------------------------
    # (champion v0, challenger v1) is the matchup the e1 epoch carries; the
    # leaf readers exercise the per-gen path math for both.
    for eid, gid in [("e1", "v0"), ("e1", "v1"), ("e0", "v0")]:
        snap[f"hypothesis_accuracy::{eid}::{gid}"] = sr.build_hypothesis_accuracy(paths, eid, gid)
        snap[f"drift_movements::{eid}::{gid}"] = sr.build_drift_movements(paths, gid)
        snap[f"matchup_detail::{eid}::{gid}"] = sr.build_matchup_detail(paths, gid)
        snap[f"per_judge_for_generation::{eid}::{gid}"] = sr.build_per_judge_for_generation(
            paths, eid, gid
        )
        snap[f"per_entry_for_generation::{eid}::{gid}"] = sr.build_per_entry_for_generation(
            paths, eid, gid
        )
    # Matchup grid + gate breakdown take (epoch, champion, challenger).
    snap["matchup_grid::e1::v0::v1"] = sr.build_matchup_grid(paths, "e1", "v0", "v1")
    snap["gate_breakdown::e1::v0::v1"] = sr.build_gate_breakdown(paths, "e1", "v0", "v1")

    # Mask wall-clock noise + the per-run tmp workspace path so the snapshot
    # is reproducible across the capture/compare boundary (the golden is
    # stored masked). The workspace root is an absolute tmp path that varies
    # every run; normalize it to a sentinel so the path-bearing responses
    # (``environment.workspace``) compare stably without weakening any check.
    root_str = str(ws)
    return {label: mask_volatile(_normalize_root(value, root_str)) for label, value in snap.items()}


def _normalize_root(value: Any, root_str: str) -> Any:
    """Replace the per-run absolute workspace root with a stable sentinel."""
    if isinstance(value, str):
        return value.replace(root_str, "<ws>")
    if isinstance(value, dict):
        return {k: _normalize_root(v, root_str) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_root(v, root_str) for v in value]
    return value


# The labels whose epoch ordering the fix corrects. For these the harness
# asserts SET + per-epoch-content equality (order-independent) and that the
# epoch order now equals the canonical ``list_epoch_ids`` order; for every
# other label it asserts byte-identity against the golden.
EPOCH_LIST_LABELS = frozenset(
    {
        "workspace_view",
        "epochs_summary",
        "lineage_view",
        "meta_loop_ledger",
    }
)


def epoch_order_of(label: str, value: Any) -> list[str] | None:
    """Extract the epoch-id order a labeled response presents, or ``None``.

    Used to assert each epoch-list-bearing response orders its epochs
    identically to the canonical ``list_epoch_ids`` order.
    """
    if label == "workspace_view":
        return [row["epoch_id"] for row in value["epochs"]]
    if label == "epochs_summary":
        return [row["epoch_id"] for row in value]
    if label == "meta_loop_ledger":
        return [row["epoch_id"] for row in value["epochs"]]
    if label == "lineage_view":
        # First-appearance order of distinct epochs in the generation list.
        seen: list[str] = []
        for node in value["generations"]:
            eid = node["epoch_id"]
            if eid not in seen:
                seen.append(eid)
        return seen
    return None


# Keys whose value is wall-clock noise (captured at read time, not derived
# from on-disk data). Masked to a constant before comparison so the snapshot
# is reproducible across capture / re-run — the same masking the MOCK-GOLDEN
# parity gate applies. NOTE: this masks the response-stamp ``generated_at``
# only; on-disk-derived timestamps (``created_at``/``proposed_at`` read from
# config/experiment files) are deterministic in the fixture and NOT masked.
_VOLATILE_KEYS = frozenset({"generated_at"})
_MASK = "<masked>"


def mask_volatile(value: Any) -> Any:
    """Recursively replace wall-clock noise keys with a constant.

    Keeps the snapshot reproducible across the capture/compare boundary
    without weakening any structural / ordering check.
    """
    if isinstance(value, dict):
        return {k: (_MASK if k in _VOLATILE_KEYS else mask_volatile(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_volatile(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """Stable JSON encoding for byte-comparison (sorted keys, volatile-masked)."""
    return json.dumps(mask_volatile(value), sort_keys=True, ensure_ascii=False)
