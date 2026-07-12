"""Tests for the epoch-publication overhaul.

Covers the content the publication now emits (contract-derived method
extras: telemetry dialect, tournament structure, proposer configuration;
the statistical-integrity and proposer-analytics sections), the
honest-degrade discipline (a feature that was OFF renders a one-line
notice, never a fabricated number), the LIVING DRAFT stamp lifecycle, and
the event-driven deterministic refresh (prose-preserving + digest no-op).

The freshness contract is spelled out in ``docs/design/PUBLICATION.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

from zicato.analyzer.report import (
    parse_prose_from_markdown,
    regenerate_epoch_report_deterministic,
)
from zicato.analyzer.report_data import gather_epoch_report_data
from zicato.analyzer.report_sections import (
    render_methodology_section,
    render_proposer_analytics_section,
    render_statistical_integrity_section,
    render_title_block,
)
from zicato.core.workspace import analysis_path


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _base_epoch(tmp_path: Path, *, scoring: dict[str, object], closed: bool = False) -> Path:
    """A minimal epoch workspace with one baseline + one promoted challenger."""
    ws = tmp_path / ".zicato"
    epoch = "2026-07-12_pub"
    edir = ws / "epochs" / epoch
    edir.mkdir(parents=True)
    _write(
        edir / "config.json",
        {
            "id": epoch,
            "name": "Publication Fixture",
            "created_at": "2026-07-12T00:00:00Z",
            "contract_hash": "feedfacecafebabe",
            "closed": closed,
            "closed_at": "2026-07-12T09:00:00Z" if closed else "",
        },
    )
    (edir / "board.jsonl").write_text(
        '{"id": "a", "kind": "single_turn", "wall_clock_budget_seconds": 30, '
        '"input": "x", "weight": 1.0, '
        '"expectation": {"kind": "predicate", "spec": "ok"}}\n',
        encoding="utf-8",
    )
    _write(edir / "scoring.json", scoring)
    _write(edir / "mutations.json", [{"id": "m", "kind": "prompt_text", "file": "p.txt"}])
    _write(
        edir / "generations" / "v0" / "experiment.json",
        {
            "generation_id": "v0",
            "parent_generation_id": "",
            "proposed_at": "2026-07-12T01:00:00Z",
            "hypothesis": {"core_idea": "baseline"},
        },
    )
    _write(
        edir / "generations" / "v1" / "experiment.json",
        {
            "generation_id": "v1",
            "parent_generation_id": "v0",
            "proposed_at": "2026-07-12T02:00:00Z",
            "hypothesis": {
                "core_idea": "tighten prompt",
                "expected_pass_rate_delta": "+0.05 to +0.15",
            },
            "outcome": {
                "pass_rate_delta": 0.10,
                "drift_loss_delta": -0.20,
                "scalar_score_delta": -0.20,
                "tournament_decision": "promoted",
            },
        },
    )
    return ws


# ---------------------------------------------------------------------------
# Content — contract-derived method extras
# ---------------------------------------------------------------------------


def test_method_renders_configured_structure_dialect_and_proposer(tmp_path: Path) -> None:
    ws = _base_epoch(
        tmp_path,
        scoring={
            "promote_margin": 0.02,
            "telemetry_dialect": "goldfive",
            "tournament_structure": {"structure": "racing", "params": {"rungs": 3}},
            "proposer_quality": {
                "best_of_n": 4,
                "critique_enabled": True,
                "screen_entries": 2,
                "genealogy": 3,
                "recombine": True,
                "recombine_merge": "llm",
            },
        },
    )
    data = gather_epoch_report_data(ws, "2026-07-12_pub")
    md = render_methodology_section(data)
    # Every configured lever surfaces its real value.
    assert "telemetry_dialect" in md
    assert "racing" in md and "rungs" in md
    assert "best_of_n" in md and "| 4" in md
    assert "genealogy channel | 3" in md
    assert "recombination | on" in md and "llm" in md
    assert "pre-tournament screen | 2" in md


def test_method_honest_degrade_on_defaults(tmp_path: Path) -> None:
    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    data = gather_epoch_report_data(ws, "2026-07-12_pub")
    md = render_methodology_section(data)
    # No structure / proposer_quality configured ⇒ honest default notices,
    # never a fabricated param table.
    assert "default **gauntlet** structure" in md
    assert "built-in defaults" in md


# ---------------------------------------------------------------------------
# Statistical integrity + proposer analytics
# ---------------------------------------------------------------------------


def test_statistical_integrity_degrades_without_round_log(tmp_path: Path) -> None:
    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    data = gather_epoch_report_data(ws, "2026-07-12_pub")
    md = render_statistical_integrity_section(data)
    assert md.startswith("## Statistical Integrity")
    # No round log emitted for this epoch ⇒ every measure says so, and
    # nothing invents a number or a CRITICAL placebo callout.
    assert "not yet emitted" in md
    assert "CRITICAL" not in md
    assert "PLACEBO" not in md


def test_statistical_integrity_lights_up_with_round_log(tmp_path: Path) -> None:
    from zicato.epoch.round_log import (
        CandidateScreened,
        DecisionRecorded,
        EvidenceReplicated,
        HoldoutReleased,
        RoundClosed,
        RoundLog,
        RoundOpened,
    )

    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    epoch = "2026-07-12_pub"
    # Round 1: a real challenger — one screen veto, one evidence refit, a
    # confirmed holdout.
    log1 = RoundLog(ws, epoch, 1)
    log1.append(RoundOpened(contract_hash="feedfacecafebabe"))
    log1.append(CandidateScreened(index=0, vetoed=True, confirmed=True))
    log1.append(CandidateScreened(index=1, vetoed=False))
    log1.append(EvidenceReplicated(ci_state={"replicates_spent": 2}))
    log1.append(HoldoutReleased(confirmed=True))
    log1.append(DecisionRecorded(decision="promoted", provenance={}))
    log1.append(RoundClosed())
    # Round 2: a PROMOTED PLACEBO — the gate crowned a no-op change.
    log2 = RoundLog(ws, epoch, 2)
    log2.append(RoundOpened(contract_hash="feedfacecafebabe"))
    log2.append(DecisionRecorded(decision="promoted", provenance={"placebo": True}))
    log2.append(RoundClosed())

    data = gather_epoch_report_data(ws, epoch)
    assert len(data.round_records) == 2
    md = render_statistical_integrity_section(data)
    # The promoted placebo is a CRITICAL callout.
    assert "<!-- CALLOUT:CRITICAL -->" in md
    assert "PLACEBO arm was PROMOTED" in md
    # Real screen / evidence / holdout counts surface.
    assert "2 slate candidates were screened; 1 vetoed" in md
    assert "1 replicate refit" in md
    assert "1 confirmed the crowning" in md


def test_proposer_analytics_scores_hypothesis_calibration(tmp_path: Path) -> None:
    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    data = gather_epoch_report_data(ws, "2026-07-12_pub")
    md = render_proposer_analytics_section(data)
    # v1 predicted "+0.05 to +0.15" and realised +0.10 — a directional HIT.
    assert "1 of 1 completed challengers (100%)" in md


# ---------------------------------------------------------------------------
# LIVING DRAFT stamp lifecycle
# ---------------------------------------------------------------------------


def test_living_draft_stamp_present_while_open(tmp_path: Path) -> None:
    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01}, closed=False)
    data = gather_epoch_report_data(ws, "2026-07-12_pub")
    title = render_title_block(data)
    assert "LIVING DRAFT — through round 1" in title
    assert "in progress" in title


def test_living_draft_stamp_removed_on_close(tmp_path: Path) -> None:
    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01}, closed=True)
    data = gather_epoch_report_data(ws, "2026-07-12_pub")
    title = render_title_block(data)
    assert "LIVING DRAFT" not in title
    assert "closed" in title


# ---------------------------------------------------------------------------
# Event-driven deterministic refresh — prose-preserving + digest no-op
# ---------------------------------------------------------------------------


def test_deterministic_refresh_preserves_prose_and_is_digest_noop(tmp_path: Path) -> None:
    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    epoch = "2026-07-12_pub"
    # Seed a persisted report carrying real LLM prose.
    md_path = analysis_path(ws, epoch)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    seeded = (
        "<!-- EYEBROW -->\nZicato\n\n# Publication Fixture\n\n"
        "## Abstract\n\nThis campaign cut off-topic drift by a fifth.\n\n"
        "## Introduction\n\nThe target agent drifts off topic.\n\n"
        "## Methodology\n\nstale\n\n"
        "## Analysis — What Worked and What Didn't\n\nThe prompt clause held.\n\n"
        "## Conclusion & Next Directions\n\nKeep the clause; widen the board.\n"
    )
    md_path.write_text(seeded, encoding="utf-8")

    changed = regenerate_epoch_report_deterministic(ws, epoch)
    assert changed is True
    refreshed = md_path.read_text(encoding="utf-8")
    # Prose is preserved verbatim; deterministic sections are re-templated.
    assert "This campaign cut off-topic drift by a fifth." in refreshed
    assert "The prompt clause held." in refreshed
    assert "Keep the clause; widen the board." in refreshed
    assert "## Statistical Integrity" in refreshed  # a new deterministic section
    assert "LIVING DRAFT — through round 1" in refreshed

    # A second refresh with no data change is a byte-identical no-op.
    before = md_path.read_bytes()
    changed_again = regenerate_epoch_report_deterministic(ws, epoch)
    assert changed_again is False
    assert md_path.read_bytes() == before


def test_parse_prose_from_markdown_skips_placeholders(tmp_path: Path) -> None:
    md = (
        "# T\n\n## Abstract\n\nreal abstract\n\n"
        "## Introduction\n\n_(prose section unavailable — the auxiliary LLM "
        "did not return it this round.)_\n\n"
        "## Methodology\n\nnot prose\n\n"
        "## Conclusion & Next Directions\n\nreal conclusion\n\n---\n\nfooter\n"
    )
    prose = parse_prose_from_markdown(md)
    assert prose["ABSTRACT"] == "real abstract"
    assert prose["CONCLUSION"] == "real conclusion"
    # A placeholder body is NOT resurrected as prose.
    assert "INTRODUCTION" not in prose
    # A non-prose h2 (Methodology) is never captured as prose.
    assert "METHODOLOGY" not in prose
