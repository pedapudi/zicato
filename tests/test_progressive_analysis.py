"""Tests for ``regenerate_in_progress_html``.

The progressive path is deterministic (no LLM) and writes a valid HTML
file directly from on-disk experiment JSON. The renderer's edge cases
are exercised by ``test_epoch_html_report.py`` — these tests focus on
the orchestrator-facing contract: empty epoch → no file, one experiment
→ file written, mtime advances on rewrite.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.core.workspace import analysis_path
from zicato.epoch.analysis import regenerate_in_progress_html


def _write_experiment(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    parent_generation_id: str,
    decision: str | None,
) -> None:
    """Drop a minimal experiment.json under ``generations/{generation_id}/``."""
    gen_dir = workspace_root / "epochs" / epoch_id / "generations" / generation_id
    gen_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "id": f"exp-{generation_id}",
        "epoch_id": epoch_id,
        "generation_id": generation_id,
        "parent_generation_id": parent_generation_id,
        "proposed_at": "2026-05-14T00:00:00Z",
        "hypothesis": {
            "core_idea": "test idea",
            "modulating": [],
            "why": "test",
            "expected_pass_rate_delta": "+0.0",
            "risks": "",
        },
    }
    if decision is not None:
        payload["outcome"] = {
            "ran_at": "2026-05-14T00:00:01Z",
            "drift_movements": [],
            "pass_rate_delta": 0.0,
            "drift_loss_delta": -0.1,
            "scalar_score_delta": -0.1,
            "tournament_decision": decision,
            "rejection_reason": "",
        }
    (gen_dir / "experiment.json").write_text(json.dumps(payload))


def test_regenerate_returns_none_when_no_experiments(tmp_path: Path) -> None:
    """An epoch with no generations yields ``None`` and no file."""
    out = regenerate_in_progress_html(tmp_path, "epoch_a")
    assert out is None
    assert not analysis_path(tmp_path, "epoch_a").with_suffix(".html").exists()


def test_regenerate_writes_html_after_one_round(tmp_path: Path) -> None:
    """One promoted experiment → valid analysis.html with the lineage."""
    _write_experiment(tmp_path, "epoch_a", "v1", "v0", "promoted")

    out = regenerate_in_progress_html(tmp_path, "epoch_a")
    assert out is not None
    assert out.exists()
    text = out.read_text()
    # Sanity checks: it's an HTML document and references our gen ids.
    assert text.startswith("<!DOCTYPE html>") or text.lstrip().startswith("<!DOCTYPE")
    assert "epoch_a" in text
    assert "v1" in text


def test_regenerate_rewrites_on_subsequent_round(tmp_path: Path) -> None:
    """A second call after a new experiment lands updates the file."""
    _write_experiment(tmp_path, "epoch_a", "v1", "v0", "promoted")
    first = regenerate_in_progress_html(tmp_path, "epoch_a")
    assert first is not None
    first_text = first.read_text()
    # The first render only knows about v1, so it cannot mention v2.
    assert "v2" not in first_text

    _write_experiment(tmp_path, "epoch_a", "v2", "v1", "rejected")
    second = regenerate_in_progress_html(tmp_path, "epoch_a")
    assert second == first
    second_text = second.read_text()
    assert "v2" in second_text
    # The rewrite landed: the new experiment's generation id is present and
    # the content differs from the first render. (Asserting the content
    # change directly avoids the old second-granularity mtime sleep — the
    # added v2 lineage guarantees the text differs.)
    assert second_text != first_text
