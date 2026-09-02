"""Orchestrator-level wiring of experiment memory in the field path.

Proves the multi-challenger field accumulates in-flight siblings: with a
capturing aux LLM that records each proposer call's user prompt and
returns a challenger-specific hypothesis, challenger k's prompt carries
the in-flight core-ideas of challengers 0..k-1 minted earlier this round,
and a challenger whose proposer failed contributes no sibling line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._orchestrator_harness import (
    install_stub_adapter_factory,
    install_telemetry_stubs,
    make_aux_responder,
    run_evolve_once,
)
from tests.test_orchestrator_multi_challenger import _bootstrap_swiss_workspace


def _episode_tasks(workspace: Path, epoch_id: str) -> list[str]:
    """The task each proposal episode was given, in the order they ran.

    Read off the workspace's own durable capture rather than a patched
    renderer, so what is asserted is what the model saw.
    """
    from zicato.proposer.input_capture import ROLE_PROPOSAL, read_proposer_inputs

    return [
        record["user"]
        for record in read_proposer_inputs(workspace, epoch_id)
        if record["role"] == ROLE_PROPOSAL
    ]


def test_field_accumulates_in_flight_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Challenger k's task carries the in-flight core ideas of the
    siblings minted before it this round."""
    ideas = {
        "v1": {"idea": "challenger-A tighten coordinator routing"},
        "v2": {"idea": "challenger-B require source citations"},
        "v3": {"idea": "challenger-C terser specialist descriptions"},
    }
    idea_a, idea_b, idea_c = (ideas[k]["idea"] for k in ("v1", "v2", "v3"))
    workspace, epoch_id = _bootstrap_swiss_workspace(
        tmp_path, field_size=3, rounds_n=1, hypotheses=ideas
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.5, "v2": 1.0, "v3": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )

    run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    tasks = _episode_tasks(workspace, epoch_id)
    assert len(tasks) == 3
    p0, p1, p2 = tasks

    # The first challenger has no prior settled history and no siblings yet,
    # so its task carries none of the round's core ideas.
    assert idea_a not in p0
    assert "What's already been tried" not in p0

    # The second challenger sees challenger-A as an in-flight sibling.
    assert "What's already been tried" in p1
    assert idea_a in p1
    assert idea_b not in p1

    # The third challenger sees both earlier siblings, not yet itself.
    assert idea_a in p2
    assert idea_b in p2
    assert idea_c not in p2


def test_failed_challenger_contributes_no_sibling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A challenger whose episode produced nothing has no hypothesis to
    share, so it contributes no in-flight sibling line to the next
    challenger's task."""
    # The first challenger's episode predicts an undeclared judge, so its
    # hypothesis is refused and no experiment is minted; the second
    # proposes normally.
    workspace, epoch_id = _bootstrap_swiss_workspace(
        tmp_path,
        field_size=2,
        rounds_n=1,
        hypotheses={"v1": {"predict": "drift:not_a_declared_judge"}},
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0, "v2": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    tasks = _episode_tasks(workspace, epoch_id)
    assert len(tasks) == 2
    # The second challenger sees no in-flight sibling — the first produced
    # no hypothesis — so the section is omitted entirely.
    assert "What's already been tried" not in tasks[1]


def test_duplicate_sibling_is_soft_rejected_for_field_diversity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A challenger that duplicates an in-flight sibling (same modulating
    id-set + core idea) is SOFT-REJECTED so it cannot collapse the field
    (FUNCTIONALITY-RECOMMENDATIONS.md §4.3 / EXPERIMENT-MEMORY.md §2.2).

    Two of three challengers are byte-identical proposals; the duplicate is
    dropped from the run slate and recorded with a ``field_diversity_duplicate``
    reason, leaving exactly two DISTINCT challengers to run."""
    idea_a = "challenger-A tighten the greeting"
    idea_b = "challenger-B require a citation"
    # Challengers v1 and v2 state the SAME core idea over the same
    # mutation point; v3 is distinct. The duplicate (v2) must be rejected.
    workspace, epoch_id = _bootstrap_swiss_workspace(
        tmp_path,
        field_size=3,
        rounds_n=1,
        hypotheses={"v1": {"idea": idea_a}, "v2": {"idea": idea_a}, "v3": {"idea": idea_b}},
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.5, "v2": 1.0, "v3": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )

    from zicato.runtime.state import read_active_tournament

    run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    active = read_active_tournament(workspace)
    assert active is not None
    by_gen = {f["generation_id"]: f["status"] for f in active.field_status}
    reasons = {f["generation_id"]: f.get("reason", "") for f in active.field_status}

    # Three slots were proposed; the middle duplicate is soft-rejected.
    assert by_gen.get("v1") == "applied"
    assert by_gen.get("v2") == "rejected"
    assert reasons.get("v2") == "field_diversity_duplicate"
    assert by_gen.get("v3") == "applied"

    # The duplicate did NOT become an in-flight sibling — the third
    # (distinct) challenger's task carries challenger-A's idea once as a
    # sibling line, beside the copy it states as its own hypothesis.
    tasks = _episode_tasks(workspace, epoch_id)
    assert len(tasks) == 3
    assert tasks[2].count(idea_a) == 1  # the single surviving sibling, not two


def test_same_ids_different_idea_is_not_a_duplicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two challengers targeting the SAME mutation id with genuinely DIFFERENT
    ideas are distinct experiments — neither is soft-rejected (the constraint
    dedups by id-set AND core idea, not by id alone)."""
    # Same mutation point, genuinely different ideas.
    workspace, epoch_id = _bootstrap_swiss_workspace(
        tmp_path,
        field_size=2,
        rounds_n=1,
        hypotheses={
            "v1": {"idea": "tighten the greeting tone"},
            "v2": {"idea": "shorten the greeting length"},
        },
    )
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.0, "v2": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True},
    )

    from zicato.runtime.state import read_active_tournament

    run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    active = read_active_tournament(workspace)
    assert active is not None
    statuses = {f["generation_id"]: f["status"] for f in active.field_status}
    # Both applied — distinct ideas are kept even on the same target id.
    assert statuses.get("v1") == "applied"
    assert statuses.get("v2") == "applied"


# ---------------------------------------------------------------------------
# Opt-in field-diversity OVERLAP enforcement (diversity_tolerance)
# ---------------------------------------------------------------------------


def _set_runtime_diversity_tolerance(workspace: Path, tolerance: float) -> None:
    """Stamp ``runtime.diversity_tolerance`` onto the workspace config.json.

    The bootstrap writes a flat config.json with no ``runtime`` block, so the
    factory reads ``diversity_tolerance`` as absent (enforcement off). This
    injects an opt-in tolerance the way an operator would in their workspace
    config, so ``evolve_once`` constructs a :class:`RuntimeConfig` with the
    knob set.
    """
    cfg_path = workspace / "config.json"
    cfg = json.loads(cfg_path.read_text())
    runtime = dict(cfg.get("runtime", {}))
    runtime["diversity_tolerance"] = tolerance
    cfg["runtime"] = runtime
    cfg_path.write_text(json.dumps(cfg))


_VARIANT_IDEAS = {
    "v1": {"idea": "variant-A shorten the greeting"},
    "v2": {"idea": "variant-B warm up the greeting"},
    "v3": {"idea": "variant-C formalize the greeting"},
}


def test_overlap_soft_reject_fires_under_diversity_tolerance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With ``diversity_tolerance`` set, a challenger whose mutation-id set
    overlaps an ACCEPTED sibling beyond the ceiling is soft-rejected — even
    when its core idea is DISTINCT (so the exact-duplicate guard does not
    fire). This is the overlap enforcement firing on id-overlap alone.

    Every challenger targets the same single marker (``greeting``), so each
    pair's Jaccard overlap is ``1.0`` — well above a ``0.5`` tolerance. With
    distinct core ideas only the FIRST is kept; the rest are overlap soft-
    rejected, leaving a one-challenger field rather than three near-identical
    experiments."""
    workspace, epoch_id = _bootstrap_swiss_workspace(
        tmp_path, field_size=3, rounds_n=1, hypotheses=_VARIANT_IDEAS
    )
    _set_runtime_diversity_tolerance(workspace, 0.5)
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.5, "v2": 1.0, "v3": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )

    # The three ideas are DISTINCT — the exact-duplicate guard never
    # fires; only the id-overlap guard can reject here.
    from zicato.runtime.state import read_active_tournament

    run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    active = read_active_tournament(workspace)
    assert active is not None
    by_gen = {f["generation_id"]: f for f in active.field_status}

    # The first challenger is kept and stamped applied; the next two overlap
    # it (Jaccard 1.0 > 0.5) and are overlap soft-rejected.
    assert by_gen["v1"]["status"] == "applied"
    assert by_gen["v1"]["diversity_status"] == "applied"
    for gid in ("v2", "v3"):
        assert by_gen[gid]["status"] == "rejected"
        assert by_gen[gid]["reason"] == "field_diversity_overlap"
        assert by_gen[gid]["diversity_status"] == "soft_rejected"
        assert by_gen[gid]["overlap"] == 1.0
        assert by_gen[gid]["overlap_peer"] == "v1"

    # Only the single distinct challenger ran — the field did not collapse
    # into three near-identical experiments.
    statuses = [f["status"] for f in active.field_status]
    assert statuses.count("applied") == 1
    assert statuses.count("rejected") == 2


def test_overlap_enforcement_absent_is_byte_compatible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With NO ``diversity_tolerance`` configured, three same-id distinct-idea
    challengers ALL apply (no overlap guard) and no ``diversity_status`` key is
    written onto any field-status record — the default-off path is byte-
    compatible with today."""
    workspace, epoch_id = _bootstrap_swiss_workspace(
        tmp_path, field_size=3, rounds_n=1, hypotheses=_VARIANT_IDEAS
    )
    # Deliberately do NOT set diversity_tolerance.
    install_stub_adapter_factory(monkeypatch)
    install_telemetry_stubs(
        monkeypatch,
        canned_loss_by_gen={"v0": 2.0, "v1": 1.5, "v2": 1.0, "v3": 0.5},
        canned_pass_by_gen={"v0": True, "v1": True, "v2": True, "v3": True},
    )

    from zicato.runtime.state import read_active_tournament

    run_evolve_once(workspace, epoch_id, make_aux_responder([]))

    active = read_active_tournament(workspace)
    assert active is not None
    statuses = {f["generation_id"]: f["status"] for f in active.field_status}
    # All three distinct same-id challengers apply — no overlap guard runs.
    assert statuses.get("v1") == "applied"
    assert statuses.get("v2") == "applied"
    assert statuses.get("v3") == "applied"
    # No per-slot diversity status is stamped when enforcement is off.
    for f in active.field_status:
        assert "diversity_status" not in f
        assert "diversity_tolerance" not in f


# ---------------------------------------------------------------------------
# Pure helpers: Jaccard overlap + the field-diversity summary block
# ---------------------------------------------------------------------------


def test_jaccard_overlap_math() -> None:
    from zicato.evolve.propose_apply import _max_overlap_with_accepted
    from zicato.selection.diversity import jaccard

    assert jaccard(frozenset(), frozenset()) == 0.0
    assert jaccard(frozenset({"a"}), frozenset({"b"})) == 0.0
    assert jaccard(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0
    # |{a}| / |{a,b,c}| = 1/3
    assert jaccard(frozenset({"a", "b"}), frozenset({"a", "c"})) == pytest.approx(1 / 3)

    # Max overlap picks the most-overlapping accepted sibling and its index.
    accepted = [frozenset({"a"}), frozenset({"a", "b", "c"})]
    overlap, idx = _max_overlap_with_accepted(frozenset({"a", "b"}), accepted)
    assert idx == 1  # {a,b} ∩ {a,b,c} = 2 / 3 > {a,b} ∩ {a} = 1/2
    assert overlap == pytest.approx(2 / 3)
    # No accepted siblings ⇒ zero overlap, sentinel index.
    assert _max_overlap_with_accepted(frozenset({"a"}), []) == (0.0, -1)


def test_compute_field_diversity_summary() -> None:
    from zicato.selection.diversity import compute_field_diversity

    sets = [
        ("v1", frozenset({"a", "b"})),
        ("v2", frozenset({"a", "b"})),  # identical to v1
        ("v3", frozenset({"c"})),  # disjoint
    ]
    block = compute_field_diversity(sets, tolerance=0.5, soft_rejected_count=1)
    assert block["field_size"] == 3
    assert block["distinct_ideas"] == 2  # {a,b} and {c}
    assert block["max_overlap"] == 1.0
    assert block["max_overlap_pair"] == ["v1", "v2"]
    assert block["tolerance"] == 0.5
    assert block["soft_rejected_count"] == 1
    # mean over the three pairs: (1.0 + 0.0 + 0.0) / 3
    assert block["mean_overlap"] == pytest.approx(1 / 3)

    # A single-challenger field has no pairs: zero overlap, one idea.
    solo = compute_field_diversity([("v1", frozenset({"a"}))])
    assert solo["field_size"] == 1
    assert solo["distinct_ideas"] == 1
    assert solo["mean_overlap"] == 0.0
    assert solo["max_overlap"] == 0.0
    assert solo["max_overlap_pair"] is None
    assert solo["tolerance"] is None
    assert solo["soft_rejected_count"] == 0
