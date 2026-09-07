"""Canonical reflection records with explicit fixture observations."""

from dataclasses import replace
from typing import Any

from zicato.reflection.adjudication import JudgeAdjudication
from zicato.reflection.findings import Finding
from zicato.reflection.scorecards import build_scorecard


def scorecard_body(fields: dict[str, Any]) -> dict[str, Any]:
    """Aggregate the declared verdict counts and retain explicitly supplied metrics."""
    adjudications = []
    for key, verdict in (
        ("tp", "TP"),
        ("fp", "FP"),
        ("fn", "FN"),
        ("tn", "TN"),
        ("ambiguous", "ambiguous"),
    ):
        for index in range(fields.get(key, 0)):
            adjudications.append(
                JudgeAdjudication(
                    judge_name=fields["judge_name"],
                    run_ref=f"{key}:{index}",
                    observed="fired" if verdict in ("TP", "FP") else "silent",
                    adjudicated=(
                        "ambiguous"
                        if verdict == "ambiguous"
                        else "should_fire"
                        if verdict in ("TP", "FN")
                        else "should_be_silent"
                    ),
                    verdict=verdict,
                    severity_match=None,
                    evidence_span="",
                    meta_judge_rationale="",
                    meta_judge_model="fixture",
                    adjudicator_self_agreement=None,
                    operator_confirmed=None,
                    fidelity="verbatim",
                    prompt_version=1,
                    k_adj=1,
                )
            )
    card = build_scorecard(
        judge_name=fields["judge_name"], adjudications=adjudications, corpus=[], vectors={}
    )
    values = {
        key: tuple(value)
        if key in {"redundant_with", "conflicts_with", "fidelity_tiers"}
        else value
        for key, value in fields.items()
    }
    return replace(card, **values).to_json()


def finding_body(fields: dict[str, Any]) -> dict[str, Any]:
    """Complete a fixture finding through the canonical producer type."""
    finding = Finding(
        finding_id=fields["finding_id"],
        pillar="validity",
        severity="info",
        title="Fixture finding",
        detail="",
        evidence=(),
        recommendation="",
        proposed_op=None,
    )
    values = {key: tuple(value) if key == "evidence" else value for key, value in fields.items()}
    return replace(finding, **values).to_json()
