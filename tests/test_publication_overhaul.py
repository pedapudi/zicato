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

import hashlib
import json
from pathlib import Path

import pytest

from tests._workspace_support import experiment_record, write_epoch
from zicato.analyzer.report import (
    regenerate_epoch_report_deterministic,
)
from zicato.analyzer.report_data import gather_epoch_report_data
from zicato.analyzer.report_sections import (
    render_methodology_section,
    render_proposer_analytics_section,
    render_statistical_integrity_section,
    render_title_block,
)
from zicato.core.mutation import MutationPoint
from zicato.core.workspace import analysis_path
from zicato.mutation.inventory import write_mutation_inventory
from zicato.workspace.layout import WorkspaceLayout


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _base_epoch(tmp_path: Path, *, scoring: dict[str, object], closed: bool = False) -> Path:
    """A minimal epoch workspace with one baseline + one promoted challenger."""
    ws = tmp_path / ".zicato"
    epoch = "2026-07-12_pub"
    edir = ws / "epochs" / epoch
    edir.mkdir(parents=True)
    write_epoch(
        WorkspaceLayout.from_root(ws),
        epoch,
        scoring=scoring,
        config={
            "id": epoch,
            "name": "Publication Fixture",
            "created_at": "2026-07-12T00:00:00Z",
            "contract_hash": "feedfacecafebabe" * 4,
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
    write_mutation_inventory(
        edir / "mutations.json",
        [
            MutationPoint(
                id="m",
                kind="span",
                file=Path("p.txt"),
                source_root=Path("."),
                line_start=1,
                line_end=1,
                content="",
                content_hash=hashlib.sha256(b"").hexdigest(),
            )
        ],
    )
    _write(
        edir / "generations" / "v0" / "experiment.json",
        experiment_record(
            **{
                "epoch_id": epoch,
                "round_index": 0,
                **{
                    "generation_id": "v0",
                    "parent_generation_id": None,
                    "proposed_at": "2026-07-12T01:00:00Z",
                    "hypothesis": {"core_idea": "baseline"},
                },
            }
        ),
    )
    _write(
        edir / "generations" / "v1" / "experiment.json",
        experiment_record(
            **{
                "epoch_id": epoch,
                "round_index": 1,
                **{
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
            }
        ),
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
            "tournament": {"structure": "racing", "params": {"rungs": 3}},
            "proposer_quality": {
                "best_of_n": 4,
                "critique_enabled": True,
                "screen_entries": 2,
            },
            "experimental": {
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


def test_method_reports_effective_defaults(tmp_path: Path) -> None:
    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    data = gather_epoch_report_data(ws, "2026-07-12_pub")
    md = render_methodology_section(data)
    assert "Tournament structure: **racing**" in md
    assert "| best_of_n | 3 (slate) |" in md
    assert "| self-critique | on |" in md
    assert "| pre-tournament screen | 2 entries" in md


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
    prose = {
        "ABSTRACT": "This campaign reduced drift.",
        "ANALYSIS": "The prompt clause held.",
        "CONCLUSION": "Keep the clause; widen the board.",
    }
    assert regenerate_epoch_report_deterministic(ws, epoch, authored=prose)
    md_path = analysis_path(ws, epoch)
    assert "## Statistical Integrity" in md_path.read_text()
    before = md_path.read_bytes()
    assert not regenerate_epoch_report_deterministic(ws, epoch)
    assert md_path.read_bytes() == before
    assert all(text in before.decode() for text in prose.values())


# ---------------------------------------------------------------------------
# Prose splice — anchor-exact fences (no silent truncation on ---/heading)
# ---------------------------------------------------------------------------


def test_authored_prose_survives_refresh_without_parsing_markdown(tmp_path: Path) -> None:
    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    epoch = "2026-07-12_pub"
    prose = "The clause held.\n\n---\n\n## Detailed results\n\nA further observation."
    regenerate_epoch_report_deterministic(ws, epoch, authored={"ANALYSIS": prose})
    path = analysis_path(ws, epoch)
    before = path.read_bytes()
    # Markdown is derived: a partial output cannot erase the recorded narrative.
    path.write_text("interrupted output")
    assert regenerate_epoch_report_deterministic(ws, epoch)
    assert path.read_bytes() == before
    assert prose in path.read_text()
    assert not regenerate_epoch_report_deterministic(ws, epoch)


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
    # Exercise closure with explicitly recorded relative board and brief paths.
    (edir / "brief.md").write_text("## Goal\n\nHold the line.\n", encoding="utf-8")
    write_epoch(
        WorkspaceLayout.from_root(ws),
        epoch,
        scoring={"promote_margin": 0.01},
        config={
            "id": epoch,
            "name": "Publication Fixture",
            "created_at": "2026-07-12T00:00:00Z",
            "board_path": "board.jsonl",
            "brief_path": "brief.md",
            "contract_hash": "feedfacecafebabe" * 4,
            "closed": False,
            "closed_at": "",
        },
    )
    md_path = analysis_path(ws, epoch)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    regenerate_epoch_report_deterministic(
        ws,
        epoch,
        authored={
            "ABSTRACT": "Durable abstract prose.",
            "INTRODUCTION": "Durable intro prose.",
            "ANALYSIS": "Durable analysis prose.",
            "CONCLUSION": "Durable conclusion prose.",
        },
    )
    # An unchanged open epoch retains the same draft.
    assert regenerate_epoch_report_deterministic(ws, epoch) is False
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
    from zicato.health.inputs import epoch_optional_failures
    from zicato.logging_stream import install_log_stream, round_log_context

    async def _unused_llm(system: str, user: str, model: str) -> str:  # pragma: no cover
        return ""

    def _boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("report generation wedged")

    monkeypatch.setattr(analyzer_pkg, "regenerate_epoch_report_deterministic", _boom)

    ws = _base_epoch(tmp_path, scoring={"promote_margin": 0.01})
    # Does NOT raise, even though the underlying regeneration blew up.
    handle = install_log_stream(ws)
    try:
        with round_log_context("2026-07-12_pub", 4):
            await _regenerate_epoch_report(ws, "2026-07-12_pub", _unused_llm, "")
    finally:
        handle.close()
    records = epoch_optional_failures(ws, "2026-07-12_pub")
    assert len(records) == 1
    assert records[0]["fields"] == {
        "operation": "epoch analysis report regeneration",
        "exception_type": "RuntimeError",
        "round_index": 4,
    }
