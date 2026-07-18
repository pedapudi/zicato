"""WS-MINE — the eval-synthesis episode extractor (EVAL-SYNTHESIS.md §2 / §9).

Per-source known-answer tests over REAL-shaped fixtures (the records the real
writers emit — ``ObservationRun`` off the corpus writer, ``JudgeAdjudication``
off ``write_adjudication``, ``HypothesisGrade`` off the ledger grader, real
experiment ``patches/*.json``), the deterministic total-order ranking (proved
order-independent), and the tolerant degrade paths (cold workspace, no
reflection, no index, malformed line).
"""

from __future__ import annotations

from pathlib import Path

from zicato.core import LossProfile
from zicato.core.workspace import reflection_adjudication_path
from zicato.query.paths import WorkspacePaths
from zicato.reflection import mining as m
from zicato.reflection.adjudicator import (
    VERDICT_AMBIGUOUS,
    VERDICT_FN,
    VERDICT_FP,
    VERDICT_TN,
    VERDICT_TP,
    JudgeAdjudication,
    write_adjudication,
)
from zicato.reflection.corpus import ObservationRun, ingest_lineage
from zicato.reflection.plan import new_plan, write_plan
from zicato.tournament.detail import HypothesisGrade, MovementGrade
from zicato.tournament.unit_cache import _unit_loss_path

EPOCH = "epoch-1"
CREATED_AT = "2026-07-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# real-shaped fixture builders
# ---------------------------------------------------------------------------


def _obs(**kw: object) -> ObservationRun:
    base: dict[str, object] = dict(
        reflection_id="mining",
        candidate_id="v3",
        entry_id="login",
        replicate=0,
        scalar=1.0,
        drift_loss=0.0,
        pass_fail=True,
        runtime_ms=10,
        aborted=False,
        abort_cause=None,
        fidelity="preview",
        has_result=False,
        has_judge_io=False,
        loss_ref=None,
        transcript_ref=None,
    )
    base.update(kw)
    return ObservationRun(**base)  # type: ignore[arg-type]


def _adj(
    judge: str, run_ref: str, verdict: str, *, fidelity: str = "verbatim"
) -> JudgeAdjudication:
    observed = "fired" if verdict in (VERDICT_TP, VERDICT_FP) else "silent"
    adjudicated = "should_fire" if verdict in (VERDICT_TP, VERDICT_FN) else "should_be_silent"
    return JudgeAdjudication(
        judge_name=judge,
        run_ref=run_ref,
        observed=observed,
        adjudicated=adjudicated,
        verdict=verdict,
        severity_match=None,
        evidence_span=f"span-{run_ref}",
        meta_judge_rationale="rationale",
        meta_judge_model="meta-model",
        adjudicator_self_agreement=None,
        operator_confirmed=None,
        fidelity=fidelity,
        prompt_version=2,
        k_adj=1,
    )


def _movement(metric: str, *, recorded: bool) -> MovementGrade:
    return MovementGrade(
        metric_name=metric,
        predicted_direction="increase",
        predicted_magnitude="medium",
        actual_from=0.1 if recorded else None,
        actual_to=0.4 if recorded else None,
        actual_magnitude="medium" if recorded else "",
        sign_match=recorded,
        magnitude_match=recorded,
        matched=recorded,
    )


# ---------------------------------------------------------------------------
# (a) FAILURE episodes
# ---------------------------------------------------------------------------


def test_failure_episodes_classify_predicate_abort_drift() -> None:
    obs = [
        _obs(candidate_id="v3", entry_id="login", pass_fail=False, loss_ref="/w/a"),
        _obs(
            candidate_id="v5",
            entry_id="login",
            aborted=True,
            abort_cause="endpoint_error",
            loss_ref="/w/b",
        ),
        _obs(
            candidate_id="v9",
            entry_id="pay",
            drift_events=(
                {"kind": "custom:cite", "severity": "critical", "judge_name": "cite", "count": 2},
            ),
            loss_ref="/w/c",
        ),
    ]
    eps = m.failure_episodes(obs)
    by_class = {(e.subject, e.evidence["failure_class"]): e for e in eps}
    assert set(by_class) == {
        ("login", "predicate_miss"),
        ("login", "abort"),
        ("pay", "drift_spike"),
    }
    assert by_class[("login", "abort")].severity_rank == m._SEV_ABORT
    assert by_class[("login", "abort")].evidence["infra"] is True
    assert by_class[("pay", "drift_spike")].evidence["drift_kinds"] == ["custom:cite"]
    assert all(e.suggestion_hint == m.HINT_REGRESSION_ENTRY for e in eps)


def test_failure_episodes_group_replicates_and_candidates() -> None:
    # Three failing runs of ONE entry the SAME way fold into ONE episode.
    obs = [
        _obs(candidate_id="v3", entry_id="login", replicate=0, pass_fail=False, loss_ref="/w/1"),
        _obs(candidate_id="v3", entry_id="login", replicate=1, pass_fail=False, loss_ref="/w/2"),
        _obs(candidate_id="v7", entry_id="login", replicate=0, pass_fail=False, loss_ref="/w/3"),
    ]
    eps = m.failure_episodes(obs)
    assert len(eps) == 1
    ep = eps[0]
    assert ep.source_lineage_ids == ("v3", "v7")  # deduped, order-stable
    assert ep.coverage_key == 2
    assert ep.recency_key == 7  # max generation ordinal
    assert set(ep.source_refs) == {"/w/1", "/w/2", "/w/3"}


def test_failure_episodes_clean_runs_yield_nothing() -> None:
    assert m.failure_episodes([_obs(pass_fail=True), _obs(pass_fail=None)]) == []
    # a non-critical drift is not a spike
    assert (
        m.failure_episodes([_obs(drift_events=({"kind": "x", "severity": "warning", "count": 1},))])
        == []
    )


def test_failure_episodes_best_fidelity_wins() -> None:
    obs = [
        _obs(
            candidate_id="v3", entry_id="login", pass_fail=False, fidelity="preview", loss_ref="a"
        ),
        _obs(
            candidate_id="v5", entry_id="login", pass_fail=False, fidelity="verbatim", loss_ref="b"
        ),
    ]
    (ep,) = m.failure_episodes(obs)
    assert ep.evidence["fidelity"] == "verbatim"


# ---------------------------------------------------------------------------
# (b) JUDGE_DISAGREEMENT episodes
# ---------------------------------------------------------------------------


def test_judge_disagreement_fn_and_fp_only() -> None:
    adjs = [
        _adj("cite", "v3:login", VERDICT_FN),
        _adj("cite", "v4:login", VERDICT_FN),
        _adj("tone", "v5:pay", VERDICT_FP),
        _adj("clean", "v6:pay", VERDICT_TP),  # agreements ignored
        _adj("clean", "v7:pay", VERDICT_TN),
        _adj("amb", "v8:pay", VERDICT_AMBIGUOUS),
    ]
    eps = m.judge_disagreement_episodes(adjs)
    by = {(e.subject, e.evidence["verdict"]): e for e in eps}
    assert set(by) == {("cite", VERDICT_FN), ("tone", VERDICT_FP)}
    fn = by[("cite", VERDICT_FN)]
    assert fn.suggestion_hint == m.HINT_JUDGE
    assert fn.severity_rank == m._SEV_MISSED_FIRE
    assert fn.evidence["count"] == 2
    assert set(fn.source_refs) == {"v3:login", "v4:login"}
    assert by[("tone", VERDICT_FP)].suggestion_hint == m.HINT_RUBRIC_REVISION


def test_judge_disagreement_empty() -> None:
    assert m.judge_disagreement_episodes([]) == []
    assert m.judge_disagreement_episodes([_adj("x", "r", VERDICT_TP)]) == []


# ---------------------------------------------------------------------------
# (c) COVERAGE_GAP episodes
# ---------------------------------------------------------------------------


def test_coverage_gap_fires_only_on_blind_board() -> None:
    churn = {"prompt_a": ["v1", "v2", "v3"], "prompt_b": ["v1"]}  # b below MIN_CHURN
    eps = m.coverage_gap_episodes(
        mutation_churn=churn, discriminating_entries=0, dead_entries=["login"]
    )
    assert [e.subject for e in eps] == ["prompt_a"]
    assert eps[0].coverage_key == 3
    assert eps[0].suggestion_hint == m.HINT_COVERAGE_ENTRY
    assert eps[0].evidence["dead_entries"] == ["login"]


def test_coverage_gap_silent_when_board_discriminates() -> None:
    churn = {"prompt_a": ["v1", "v2", "v3"]}
    assert (
        m.coverage_gap_episodes(mutation_churn=churn, discriminating_entries=2, dead_entries=[])
        == []
    )


# ---------------------------------------------------------------------------
# (d) UNRESOLVED_CLAIM episodes
# ---------------------------------------------------------------------------


def test_unresolved_claim_only_unrecorded_movements() -> None:
    g1 = HypothesisGrade(
        generation_id="v4",
        core_idea="idea",
        movements=(
            _movement("citations.count", recorded=False),
            _movement("pass.rate", recorded=True),  # recorded → ignored
            _movement("drift:looping", recorded=False),
        ),
        predictions=3,
        matches=1,
        accuracy=0.33,
    )
    g2 = HypothesisGrade(
        generation_id="v6",
        core_idea="idea2",
        movements=(_movement("citations.count", recorded=False),),
        predictions=1,
        matches=0,
        accuracy=0.0,
    )
    eps = m.unresolved_claim_episodes([g1, g2])
    by = {e.subject: e for e in eps}
    assert set(by) == {"citations.count", "drift:looping"}
    assert by["citations.count"].suggestion_hint == m.HINT_COVERAGE_ENTRY
    assert by["citations.count"].source_lineage_ids == ("v4", "v6")
    assert by["drift:looping"].suggestion_hint == m.HINT_JUDGE  # drift: → judge


def test_unresolved_claim_empty() -> None:
    assert m.unresolved_claim_episodes([]) == []


# ---------------------------------------------------------------------------
# (e) STALENESS episodes
# ---------------------------------------------------------------------------


class _Finding:
    def __init__(self, severity: str, summary: str, generation_id: str) -> None:
        self.severity = severity
        self.summary = summary
        self.detail = {"generation_id": generation_id}


def test_staleness_dead_and_gap() -> None:
    dead = [{"entry_id": "login", "discrimination_pairs": 4, "slice": "train"}]
    findings = [_Finding("critical", "gap widened to +0.4", "v9")]
    eps = m.staleness_episodes(dead_entries=dead, gap_findings=findings)
    dead_ep = next(e for e in eps if e.subject == "login")
    gap_ep = next(e for e in eps if e.subject == "__board__")
    assert dead_ep.suggestion_hint == m.HINT_HARDER_VARIANT
    assert dead_ep.evidence["reason"] == "dead_entry"
    assert gap_ep.severity_rank == m._SEV_GAP_CRIT
    assert gap_ep.source_lineage_ids == ("v9",)
    assert all(e.suggestion_hint == m.HINT_HARDER_VARIANT for e in eps)


def test_staleness_empty() -> None:
    assert m.staleness_episodes(dead_entries=[], gap_findings=[]) == []


# ---------------------------------------------------------------------------
# ranking — deterministic TOTAL order, order-independent
# ---------------------------------------------------------------------------


def _sample_episodes() -> list[m.MinedEpisode]:
    obs = [
        _obs(
            candidate_id="v5",
            entry_id="login",
            aborted=True,
            abort_cause="endpoint_error",
            loss_ref="a",
        ),
        _obs(candidate_id="v3", entry_id="pay", pass_fail=False, loss_ref="b"),
    ]
    adjs = [_adj("cite", "v3:login", VERDICT_FN), _adj("tone", "v5:pay", VERDICT_FP)]
    cov = m.coverage_gap_episodes(
        mutation_churn={"p": ["v1", "v2"]}, discriminating_entries=0, dead_entries=[]
    )
    return m.failure_episodes(obs) + m.judge_disagreement_episodes(adjs) + cov


def test_rank_is_total_and_order_independent() -> None:
    eps = _sample_episodes()
    ranked = m.rank_episodes(eps)
    reranked = m.rank_episodes(list(reversed(eps)))
    assert [e.episode_id for e in ranked] == [e.episode_id for e in reranked]
    # severity descending is the primary key
    sev = [e.severity_rank for e in ranked]
    assert sev == sorted(sev, reverse=True)


def test_rank_pins() -> None:
    # A fixed episode set pins the exact ranked order (severity, then recency).
    ranked = m.rank_episodes(_sample_episodes())
    kinds = [(e.episode_type, e.severity_rank, e.recency_key) for e in ranked]
    # abort(5,v5) and FN(5,recency0) tie on severity → recency breaks it; then
    # the sev-4 predicate(v3) / FP(3) / coverage(4) fall in severity order.
    assert kinds[0] == ("failure", 5, 5)  # the abort cascade, freshest
    assert kinds[1] == ("judge_disagreement", 5, 0)  # FN, no lineage recency
    assert kinds[-1] == ("judge_disagreement", 3, 0)  # the FP, softest


def test_episode_id_is_content_stable() -> None:
    (ep,) = m.failure_episodes([_obs(entry_id="login", pass_fail=False, loss_ref="/w/x")])
    (ep2,) = m.failure_episodes([_obs(entry_id="login", pass_fail=False, loss_ref="/w/x")])
    assert ep.episode_id == ep2.episode_id
    assert ep.episode_id.startswith("ep-")


# ---------------------------------------------------------------------------
# real-writer grounding — the corpus + adjudication + patch-churn readers
# ---------------------------------------------------------------------------


def _write_loss(workspace: Path, gen: str, entry: str, *, pass_fail: bool | None) -> Path:
    from zicato.telemetry import reducer

    loss = LossProfile(
        run_id=f"run-{gen}-{entry}",
        entry_id=entry,
        generation_id=gen,
        epoch_id=EPOCH,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=42,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=1.0,
        pass_fail=pass_fail,
    )
    path = _unit_loss_path(workspace, EPOCH, gen, entry, 0)
    reducer.write_loss_profile(loss, path)
    return path


def test_failure_episodes_over_real_ingest_lineage(tmp_path: Path) -> None:
    # Ground the corpus binding: write a real loss.json, ingest it with the REAL
    # corpus writer/reader, and feed the emitted ObservationRun to the extractor.
    from zicato.core import ScoringWeights

    _write_loss(tmp_path, "v1", "login", pass_fail=False)
    _write_loss(tmp_path, "v1", "pay", pass_fail=True)
    obs = ingest_lineage(
        workspace_root=tmp_path,
        epoch_id=EPOCH,
        reflection_id="mining",
        candidates=["v1"],
        entries=["login", "pay"],
        weights=ScoringWeights(),
    )
    eps = m.failure_episodes(obs)
    assert [e.subject for e in eps] == ["login"]
    assert eps[0].evidence["failure_class"] == "predicate_miss"


def test_load_latest_adjudications_over_real_writer(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path)
    rid = "refl-20260701000000-mine0001"
    # A discoverable reflection (plan.json) with a real persisted verdict.
    write_plan(
        tmp_path,
        new_plan(
            epoch_id=EPOCH,
            candidates=["v1"],
            entries=["login"],
            replicates=2,
            created_at=CREATED_AT,
            reflection_id=rid,
        ),
    )
    write_adjudication(
        reflection_adjudication_path(tmp_path, EPOCH, rid, "cite", "v1:login"),
        _adj("cite", "v1:login", VERDICT_FN),
    )
    adjs = m._load_latest_adjudications(paths, EPOCH)
    assert [a.verdict for a in adjs] == [VERDICT_FN]
    eps = m.judge_disagreement_episodes(adjs)
    assert eps and eps[0].subject == "cite"


def test_load_latest_adjudications_tolerates_malformed(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path)
    rid = "refl-20260701000000-mine0002"
    write_plan(
        tmp_path,
        new_plan(
            epoch_id=EPOCH,
            candidates=["v1"],
            entries=["login"],
            replicates=2,
            created_at=CREATED_AT,
            reflection_id=rid,
        ),
    )
    good = reflection_adjudication_path(tmp_path, EPOCH, rid, "cite", "v1:login")
    write_adjudication(good, _adj("cite", "v1:login", VERDICT_FP))
    # a torn sibling file — must be skipped, never crash
    (good.parent / "torn.json").write_text("{not json", encoding="utf-8")
    adjs = m._load_latest_adjudications(paths, EPOCH)
    assert [a.verdict for a in adjs] == [VERDICT_FP]


def test_mutation_churn_from_patch_history() -> None:
    experiments = [
        {"generation_id": "v1", "patches": {"prompt_a": {}, "prompt_b": {}}},
        {"generation_id": "v2", "patches": {"prompt_a": {}}},
        {"generation_id": "v3", "patches": {}},
        {"generation_id": "v4"},  # no patches key
    ]
    churn = m._mutation_churn(experiments)
    assert churn == {"prompt_a": ["v1", "v2"], "prompt_b": ["v1"]}


# ---------------------------------------------------------------------------
# degrade paths — the orchestrator never crashes
# ---------------------------------------------------------------------------


def test_mine_episodes_cold_workspace(tmp_path: Path) -> None:
    # No epochs, no index, no runs, no reflection, no calibration.
    assert m.mine_episodes(WorkspacePaths(tmp_path)) == []


def test_mine_episodes_unknown_epoch(tmp_path: Path) -> None:
    assert m.mine_episodes(WorkspacePaths(tmp_path), "../escape") == []
    assert m.mine_episodes(WorkspacePaths(tmp_path), "nope") == []


def test_orchestrator_readers_degrade_empty(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path)
    assert m._load_observations(paths, EPOCH) == []
    assert m._load_latest_adjudications(paths, EPOCH) == []
    assert m._load_experiments(paths, EPOCH) == []
    assert m._dead_entries(paths, EPOCH) == []
    assert m._load_ledger(paths, EPOCH) == []
    assert m._discriminating_entry_count(paths, EPOCH, []) == 0
