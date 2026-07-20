"""The COMPACT admission summary (TRAJECTORY-UI.md §2.2a / task 3).

``reflect report`` gains a compact, evidence-tier-led admission line per
suggestion, consistent with the Console cards (probed/planned tier + flip WITH
its n + sep/pairs). Honest throughout: an unmeasured probe reads ``unmeasured``,
never a fabricated ``0.0``.
"""

from __future__ import annotations

from zicato.reflection.suggestions import (
    Suggestion,
    format_admission_compact,
    render_suggestions_md,
)


def _sug(admission: dict | None) -> Suggestion:
    return Suggestion(
        suggestion_id="sug-x1",
        suggestion_type="regression_entry",
        artifact_kind="board_entry",
        subject="entryA",
        summary="pin the miss",
        rationale="why",
        target_slice="train",
        draft_artifact={"id": "entryA_reg"},
        proposed_op={"op": "add_board_entry", "args": {"entry": {"id": "entryA_reg"}}},
        provenance={"source_episodes": ["ep-1"]},
        admission=admission,
        severity_rank=4,
        recency_key=0,
        coverage_key=1,
    )


def test_compact_measured_leads_with_probed_tier_and_carries_n() -> None:
    adm = {
        "noise": {"flip_rate": 0.2, "runs": 5, "measured": True, "base": 6000},
        "discrimination": {"separated": 3, "pairs": 4, "measured": True},
    }
    out = format_admission_compact(adm)
    assert out.startswith("[probed]")
    assert "flip 0.2 (n=5)" in out
    assert "sep 3/4" in out
    assert "over the" not in out  # 0.2 is under the 0.25 ceiling


def test_compact_flags_over_ceiling_and_dead_channel_honestly() -> None:
    adm = {
        "noise": {"flip_rate": 0.4, "runs": 5, "measured": True},
        "discrimination": {"separated": 0, "pairs": 5, "measured": True},
    }
    out = format_admission_compact(adm)
    assert out.startswith("[probed]")
    assert "over the 0.25 ceiling" in out
    assert "sep 0/5" in out


def test_compact_unmeasured_is_planned_and_never_fabricates_zero() -> None:
    for adm in (None, {"noise": {"measured": False}, "discrimination": {"measured": False}}):
        out = format_admission_compact(adm)
        assert out.startswith("[planned]")
        assert "flip unmeasured" in out
        assert "sep unmeasured" in out
        assert "0.0" not in out and "0/0" not in out


def test_report_md_carries_the_compact_line_per_suggestion() -> None:
    adm = {
        "noise": {"flip_rate": 0.2, "runs": 5, "measured": True},
        "discrimination": {"separated": 3, "pairs": 4, "measured": True},
    }
    lines = render_suggestions_md([_sug(adm)])
    body = "\n".join(lines)
    assert "- admission (compact): [probed] flip 0.2 (n=5) · sep 3/4" in body
    # the compact line is consistent with the standalone formatter.
    assert format_admission_compact(adm) in body
