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

import pytest

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
    # No round has settled for this epoch ⇒ every measure says so honestly
    # (the TRUE cause — no settled round — never "instrumentation not yet
    # wired"), and nothing invents a number or a CRITICAL placebo callout.
    assert "No round has settled for this epoch yet" in md
    # The purged false premise must never resurface: round-log emission is
    # live on the evolve path, so no degrade line may claim otherwise.
    assert "not yet emitted" not in md
    assert "later phase" not in md
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
        "## Introduction\n\n_(prose section unavailable — the evaluation LLM "
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


# ---------------------------------------------------------------------------
# Prose splice — anchor-exact fences (no silent truncation on ---/heading)
# ---------------------------------------------------------------------------


def test_prose_fences_survive_structural_lines_through_two_refreshes(tmp_path: Path) -> None:
    """LLM prose carrying a ``---`` rule, an embedded ``## heading``, and a
    fence-shaped line round-trips BYTE-IDENTICAL through two consecutive
    deterministic refreshes — the old "stop at the first ``## ``/``---``"
    heuristic would have permanently truncated it."""
    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    epoch = "2026-07-12_pub"
    md_path = analysis_path(ws, epoch)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    tricky = (
        "The clause held across the lineage.\n\n"
        "---\n\n"
        "## Deep Dive\n\n"
        "An embedded heading the old heuristic would have severed here.\n\n"
        "A fence-shaped line that is not our sentinel: <!-- PROSE:NOTES -->\n"
        "and another block's close fence inline: <!-- /PROSE:CONCLUSION -->"
    )
    # A seed already in the fenced format, but with a STALE deterministic
    # body so the first refresh genuinely rewrites (and thus exercises the
    # parse→re-emit round-trip) rather than short-circuiting on the digest.
    seeded = (
        "<!-- EYEBROW -->\nZicato\n\n# Publication Fixture\n\n"
        "## Abstract\n\n<!-- PROSE:ABSTRACT -->\nAbstract prose.\n<!-- /PROSE:ABSTRACT -->\n\n"
        "## Introduction\n\n<!-- PROSE:INTRODUCTION -->\nIntro prose.\n"
        "<!-- /PROSE:INTRODUCTION -->\n\n"
        "## Methodology\n\nstale\n\n"
        "## Analysis — What Worked and What Didn't\n\n"
        "<!-- PROSE:ANALYSIS -->\n" + tricky + "\n<!-- /PROSE:ANALYSIS -->\n\n"
        "## Conclusion & Next Directions\n\n"
        "<!-- PROSE:CONCLUSION -->\nKeep the clause.\n<!-- /PROSE:CONCLUSION -->\n"
    )
    md_path.write_text(seeded, encoding="utf-8")

    assert regenerate_epoch_report_deterministic(ws, epoch) is True
    after_first = md_path.read_bytes()
    refreshed = md_path.read_text(encoding="utf-8")
    # Every structural line inside the prose survived verbatim — no truncation.
    assert "## Deep Dive" in refreshed
    assert "An embedded heading the old heuristic would have severed here." in refreshed
    assert "<!-- PROSE:NOTES -->" in refreshed
    assert "another block's close fence inline: <!-- /PROSE:CONCLUSION -->" in refreshed
    # The prose after the embedded rule/heading is intact (not severed).
    assert "Keep the clause." in refreshed

    # A second refresh with no data change is a byte-identical no-op — the
    # fenced round-trip is stable (the fence-shaped lines did not perturb it).
    assert regenerate_epoch_report_deterministic(ws, epoch) is False
    assert md_path.read_bytes() == after_first


def test_unfenced_legacy_report_upgrades_to_fences_without_prose_loss(tmp_path: Path) -> None:
    """A pre-fix ``analysis.md`` WITHOUT fences must not lose prose: the parse
    falls back to the old heuristic, splices what it captured verbatim, and
    the assembler re-emits WITH fences so the document self-heals on the first
    refresh (a fenced no-op thereafter)."""
    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    epoch = "2026-07-12_pub"
    md_path = analysis_path(ws, epoch)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    legacy = (
        "<!-- EYEBROW -->\nZicato\n\n# Publication Fixture\n\n"
        "## Abstract\n\nLegacy abstract prose.\n\n"
        "## Introduction\n\nLegacy intro prose.\n\n"
        "## Methodology\n\nstale\n\n"
        "## Analysis — What Worked and What Didn't\n\nLegacy analysis prose.\n\n"
        "## Conclusion & Next Directions\n\nLegacy conclusion prose.\n"
    )
    assert "<!-- PROSE:" not in legacy  # genuinely unfenced
    md_path.write_text(legacy, encoding="utf-8")

    assert regenerate_epoch_report_deterministic(ws, epoch) is True
    upgraded = md_path.read_text(encoding="utf-8")
    # Prose preserved through the heuristic fallback...
    assert "Legacy abstract prose." in upgraded
    assert "Legacy analysis prose." in upgraded
    assert "Legacy conclusion prose." in upgraded
    # ...and the document self-healed — it now carries the fences.
    assert "<!-- PROSE:ABSTRACT -->" in upgraded
    assert "<!-- /PROSE:CONCLUSION -->" in upgraded

    # Now fenced, a second refresh is a no-op.
    assert regenerate_epoch_report_deterministic(ws, epoch) is False


# ---------------------------------------------------------------------------
# LIVING DRAFT clears on explicit `zicato epoch close`
# ---------------------------------------------------------------------------


def test_epoch_close_clears_living_draft_and_preserves_prose(tmp_path: Path) -> None:
    """The no-LLM close seam (``zicato epoch close``) must clear the LIVING
    DRAFT stamp on an existing living-draft ``analysis.md`` while preserving
    the LLM prose verbatim."""
    from zicato.epoch import lifecycle

    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01}, closed=False)
    epoch = "2026-07-12_pub"
    edir = ws / "epochs" / epoch
    # The strict close-path loader (`load_epoch`) requires the frozen
    # board/brief paths the minimal fixture config omits; add them.
    (edir / "brief.md").write_text("## Goal\n\nHold the line.\n", encoding="utf-8")
    _write(
        edir / "config.json",
        {
            "id": epoch,
            "name": "Publication Fixture",
            "created_at": "2026-07-12T00:00:00Z",
            "board_path": "board.jsonl",
            "brief_path": "brief.md",
            "contract_hash": "feedfacecafebabe",
            "closed": False,
            "closed_at": "",
        },
    )
    md_path = analysis_path(ws, epoch)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        "<!-- EYEBROW -->\nZicato\n\n# Publication Fixture\n\n"
        "## Abstract\n\nDurable abstract prose.\n\n"
        "## Introduction\n\nDurable intro prose.\n\n"
        "## Analysis — What Worked and What Didn't\n\nDurable analysis prose.\n\n"
        "## Conclusion & Next Directions\n\nDurable conclusion prose.\n",
        encoding="utf-8",
    )
    # A mid-epoch refresh makes this a LIVING DRAFT (epoch still open).
    assert regenerate_epoch_report_deterministic(ws, epoch) is True
    draft = md_path.read_text(encoding="utf-8")
    assert "LIVING DRAFT" in draft
    assert "Durable abstract prose." in draft

    # Explicit close with NO evaluation LLM — the `zicato epoch close` path.
    lifecycle.close_epoch(ws, epoch_id=epoch, aux_call_llm=None)

    closed = md_path.read_text(encoding="utf-8")
    # The stamp is gone and the status reads closed...
    assert "LIVING DRAFT" not in closed
    assert "**Status**: closed" in closed
    # ...and the prose is preserved verbatim across the close re-stamp.
    assert "Durable abstract prose." in closed
    assert "Durable conclusion prose." in closed


# ---------------------------------------------------------------------------
# Proposer analytics — the LIT slate-mix path + best-effort freshness hook
# ---------------------------------------------------------------------------


def test_proposer_analytics_lights_up_slate_mix_with_round_log(tmp_path: Path) -> None:
    """The ``if sampled:`` branch of the slate-mix fold binds real
    round-record data — mirroring the statistical-integrity lightup."""
    from zicato.epoch.round_log import (
        CandidateSampled,
        DecisionRecorded,
        RoundClosed,
        RoundLog,
        RoundOpened,
    )

    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    epoch = "2026-07-12_pub"
    # One round that sampled three candidates, one of them minted by
    # mechanical recombination of rejected parents (WS-REC).
    log1 = RoundLog(ws, epoch, 1)
    log1.append(RoundOpened(contract_hash="feedfacecafebabe"))
    log1.append(CandidateSampled(i=0, n=3))
    log1.append(CandidateSampled(i=1, n=3))
    log1.append(CandidateSampled(i=2, n=3, recombined=True))
    log1.append(DecisionRecorded(decision="promoted", provenance={}))
    log1.append(RoundClosed())

    data = gather_epoch_report_data(ws, epoch)
    assert len(data.round_records) == 1
    md = render_proposer_analytics_section(data)
    # The LIT branch reports the real sampled/recombined counts, never the
    # degrade one-liner.
    assert "3 candidate" in md and "were sampled" in md
    assert "1 came from mechanical recombination" in md
    assert "No round has settled" not in md


async def test_round_report_regeneration_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedge inside report generation during the round epilogue is swallowed
    — the freshness hook must NEVER abort the round."""
    import zicato.analyzer as analyzer_pkg
    from zicato.evolve.round_reporting import _regenerate_epoch_report
    from zicato.util.best_effort import best_effort_failures, reset_best_effort_failures

    async def _unused_llm(system: str, user: str, model: str) -> str:  # pragma: no cover
        return ""

    def _boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("report generation wedged")

    monkeypatch.setattr(analyzer_pkg, "regenerate_epoch_report_deterministic", _boom)
    reset_best_effort_failures()

    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    # Does NOT raise, even though the underlying regeneration blew up.
    await _regenerate_epoch_report(ws, "2026-07-12_pub", _unused_llm, "")
    # The swallow is observable via the best-effort failure tally.
    assert best_effort_failures().get("epoch analysis report regeneration", 0) >= 1
