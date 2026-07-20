"""Shared capture harness for the trajectory-bootstrap UI readers (TRAJECTORY-UI.md §4.1).

The ONE place that seeds a workspace, runs the **REAL** trajectory-bootstrap
pipeline (import → persist → mine → synthesise, mechanical tiers, zero LLM),
and captures the three reader payloads. Both the pytest determinism/known-answer
test (``tests/test_trace_view.py``) and the fixture generator
(``tools/gen_trace_view_fixtures.py``) import this so the node-consumable
fixtures are produced by the SAME real path the tests assert on — never a
hand-authored mock shape (the composition-check rule 2).

The epoch id is ``{date}_{slug}`` (date-derived), so the harness PINS
``_today`` / ``_now`` to a fixed instant — the durable capture-harness lesson —
making every captured payload byte-stable across days.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

# The real foreign-trace fixture dir (all three dialects + ambiguous + malformed).
FIXTURES = Path(__file__).parent / "fixtures"
TRAJ_DIR = FIXTURES / "trajectories"

_PINNED_DATE = "2020-01-01"
_PINNED_NOW = "2020-01-01T00:00:00+00:00"
REFLECTION_ID = "refl-traceviz"


@contextmanager
def _pinned_clock() -> Iterator[None]:
    with (
        mock.patch("zicato.epoch.lifecycle._today", return_value=_PINNED_DATE),
        mock.patch("zicato.epoch.lifecycle._now_iso", return_value=_PINNED_NOW),
    ):
        yield


def build_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Seed a workspace with one epoch; return ``(workspace_root, epoch_id)``."""
    from zicato.board.jsonl import save_board
    from zicato.core.types import BoardEntry, ScoringWeights
    from zicato.epoch.lifecycle import new_epoch

    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    (ws / "config.json").write_text(json.dumps({"runtime": {}, "adapter": {}}), encoding="utf-8")
    entry = BoardEntry(id="entryA", kind="single_turn", wall_clock_budget_seconds=30, input="hi")
    board_file = tmp_path / "board.jsonl"
    save_board([entry], board_file)
    with _pinned_clock():
        cfg = new_epoch(ws, "boot", board_file, "steer", ScoringWeights())
    return ws, cfg.id


def run_pipeline(ws: Path, epoch_id: str, *, reflection_id: str = REFLECTION_ID) -> None:
    """Run the REAL import → persist → mine → synthesise → persist chain.

    Drives the library seams directly (exactly what ``reflect suggest
    --from-trajectories`` composes, TRAJECTORY-BOOTSTRAP.md §6) — zero LLM
    (``allow_llm=False``), the mechanical bootstrap tier only. ``admit`` stamps
    the plan-mode (unmeasured) admission record so the provenance reader's
    ``admission_viz`` renders honestly (no probe spent).
    """
    from zicato.query.paths import WorkspacePaths
    from zicato.reflection.admission import admit
    from zicato.reflection.mining import mine_episodes
    from zicato.reflection.suggestions import write_suggestions
    from zicato.reflection.synthesis import synthesize
    from zicato.reflection.trace_import import import_trajectories, write_imported_traces

    paths = WorkspacePaths(ws)
    traces = import_trajectories(TRAJ_DIR)
    write_imported_traces(ws, epoch_id, reflection_id, traces)
    episodes = mine_episodes(paths, epoch_id, imported_traces=traces)
    suggestions = synthesize(
        episodes,
        allow_llm=False,
        workspace_root=ws,
        epoch_id=epoch_id,
        imported_traces=traces,
    )
    # Plan-mode admission (no probe): stamps the unmeasured record honestly.
    suggestions = admit(suggestions, probe=False, workspace_root=ws, epoch_id=epoch_id)
    write_suggestions(ws, epoch_id, reflection_id, suggestions)


def _pick_suggestion_id(paths: Any, epoch_id: str, reflection_id: str) -> str | None:
    """A deterministic bootstrap suggestion id whose provenance chain is non-empty."""
    from zicato.reflection.suggestions import read_suggestions

    for sug in read_suggestions(paths.root, epoch_id, reflection_id):
        foreign = sug.provenance.get("foreign_source")
        episodes = sug.provenance.get("source_episodes") or []
        if isinstance(foreign, dict) and episodes:
            return sug.suggestion_id
    return None


def capture_payloads(
    ws: Path, epoch_id: str, *, reflection_id: str = REFLECTION_ID
) -> dict[str, Any]:
    """Call the three REAL readers; return ``{list, detail, provenance}`` payloads.

    The trace id is the richest trace (``build_trace_list`` orders richest-first,
    so ``traces[0]``); the suggestion id is a bootstrap suggestion with a
    non-empty provenance chain.
    """
    from zicato.query import (
        build_suggestion_provenance,
        build_trace_detail,
        build_trace_list,
    )
    from zicato.query.paths import WorkspacePaths

    paths = WorkspacePaths(ws)
    trace_list = build_trace_list(paths, reflection_id)
    trace_id = trace_list["traces"][0]["trace_id"] if trace_list["traces"] else "trace-missing"
    detail = build_trace_detail(paths, reflection_id, trace_id)
    suggestion_id = _pick_suggestion_id(paths, epoch_id, reflection_id) or "sug-missing"
    provenance = build_suggestion_provenance(paths, reflection_id, suggestion_id)
    return {"list": trace_list, "detail": detail, "provenance": provenance}


def build_and_capture(tmp_path: Path) -> dict[str, Any]:
    """Seed → run pipeline → capture, with a normalized (date-pinned) epoch id."""
    ws, epoch_id = build_workspace(tmp_path)
    run_pipeline(ws, epoch_id)
    return capture_payloads(ws, epoch_id)


def canonical_json(payload: Any) -> str:
    """The committed-fixture serialization (sorted keys, UTF-8, trailing newline)."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
