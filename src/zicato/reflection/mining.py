"""WS-MINE — the episode extractor for eval synthesis (generative reflection).

The instrument's second loop (EVAL-SYNTHESIS.md) opens here: mine bounded
**episodes** from the candidate loop's observed behaviour, each one a demand
signal for an instrument change. This module is the endpoint-free front of that
pipeline — it spends ZERO LLM budget and only READS already-persisted
artifacts. Downstream (WS-SYNTH / WS-ADMIT / WS-SURFACE) turns ranked episodes
into measured suggestions; nothing here authors a suggestion or touches a
contract.

Five episode kinds, each bound to a **tree-verified** real data source
(EVAL-SYNTHESIS.md §2 — the binding discipline is the eval-view lesson: bind to
the shapes the real writers produce, never a synthetic shape the pipeline can't
emit):

* **FAILURE** — from the dialect-agnostic ``LossProfile`` convergence point,
  read as :class:`zicato.reflection.corpus.ObservationRun` (predicate misses,
  abort cascades, critical drift spikes). One binding covers both the
  ``goldfive`` and ``adk_events`` dialects (TELEMETRY-DIALECTS.md §1).
* **JUDGE_DISAGREEMENT** — from reflection's adjudicated corpus
  (:class:`zicato.reflection.adjudicator.JudgeAdjudication`): an ``FP`` / ``FN``
  verdict is an in-run judge vs meta-judge flip.
* **COVERAGE_GAP** — churned mutation points (the applied-patch history) crossed
  with a board that discriminates nothing (the MATCHUP-RECORD discrimination
  binding, NOT ``loss_profiles`` pairs — EVAL-VIEW.md §2.3).
* **UNRESOLVED_CLAIM** — from the hypothesis calibration ledger
  (:func:`zicato.tournament.detail.hypothesis_ledger`): a predicted metric
  movement the outcome never recorded is a channel nothing measures.
* **STALENESS** — dead entries (the instrument panel) + generalization-gap
  detector firings → harder-variant demand.

Every extractor is a **pure function** over already-parsed inputs (mirroring
:mod:`zicato.reflection.analysis`); :func:`mine_episodes` is the one I/O
orchestrator that reads the artifacts, calls the extractors, and ranks the
result with a documented TOTAL order (§2). Every read is tolerant: a cold
workspace / absent reflection / absent index / malformed line degrades to fewer
episodes (malformed inputs counted, never a crash — the dialect discipline).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from zicato.core.loss import is_infra_abort_cause
from zicato.reflection.adjudicator import VERDICT_FN, VERDICT_FP, JudgeAdjudication
from zicato.reflection.corpus import ObservationRun

#: Stamped onto every episode's provenance so a suggestion traces back to the
#: miner that produced it (EVAL-SYNTHESIS.md §4).
MINER_VERSION: str = "eval-synth/1"

# --- episode kinds (EVAL-SYNTHESIS.md §2) ----------------------------------
EPISODE_FAILURE: str = "failure"
EPISODE_JUDGE_DISAGREEMENT: str = "judge_disagreement"
EPISODE_COVERAGE_GAP: str = "coverage_gap"
EPISODE_UNRESOLVED_CLAIM: str = "unresolved_claim"
EPISODE_STALENESS: str = "staleness"

# --- suggestion hints (which EVAL-SYNTHESIS.md §3 type an episode seeds) ----
HINT_REGRESSION_ENTRY: str = "regression_entry"
HINT_COVERAGE_ENTRY: str = "coverage_entry"
HINT_JUDGE: str = "judge_suggestion"
HINT_RUBRIC_REVISION: str = "rubric_revision"
HINT_HARDER_VARIANT: str = "harder_variant"
#: An infra flake seeds NOTHING — the synthesiser routes no suggestion for it
#: (EVAL-SYNTHESIS.md §2a: an infrastructure abort is not a candidate failure, so
#: it never becomes a regression entry). The episode still rides the mining
#: output for operator visibility.
HINT_INFRA_FLAKE: str = "infra_flake"

# --- severity ranks (higher = worse; the ranking is descending, §2) --------
#: A missed real failure (FN) and an infra/abort cascade are the worst demand
#: signals — a real failure the instrument does not catch. A false fire (FP) and
#: an unresolved claim are middling; a dead-channel staleness is the softest.
_SEV_ABORT: int = 5
_SEV_MISSED_FIRE: int = 5
_SEV_PREDICATE_MISS: int = 4
_SEV_COVERAGE_GAP: int = 4
_SEV_DRIFT_SPIKE: int = 4
_SEV_UNRESOLVED_CLAIM: int = 3
_SEV_FALSE_FIRE: int = 3
_SEV_GAP_CRIT: int = 3
_SEV_STALENESS: int = 2
_SEV_GAP_WARN: int = 2
#: An infra flake is the SOFTEST signal — it is not a candidate failure at all,
#: just a telemetry note that a run aborted on infrastructure, not on behaviour.
_SEV_INFRA_FLAKE: int = 1

#: A mutation point is "churned" once it has been rewritten in at least this
#: many generations — a single rewrite is not yet a track record.
MIN_CHURN: int = 2

#: The critical drift severity token a FAILURE drift-spike episode keys on.
_SEVERITY_CRITICAL: str = "critical"

_DIGITS_RE = re.compile(r"(\d+)")


def _generation_ordinal(generation_id: str) -> int:
    """The integer suffix of a generation id (``v10`` → ``10``); ``0`` if none.

    A total, deterministic recency key — a fresher generation ranks higher.
    Uses the LAST digit run so ``epoch2-v10`` reads as ``10``, mirroring the
    natural-key idiom the query layer already sorts generations by.
    """
    matches = _DIGITS_RE.findall(generation_id or "")
    return int(matches[-1]) if matches else 0


@dataclass(frozen=True, slots=True)
class MinedEpisode:
    """One mined demand signal (EVAL-SYNTHESIS.md §2 / §9).

    Fields
    ------
    episode_id:
        Content-stable ``ep-{8hex}`` — a sha256 over ``(episode_type, subject,
        sorted source_refs)``, INDEPENDENT of ranking order so a re-run
        resolves the same id.
    episode_type:
        One of the five ``EPISODE_*`` kinds.
    subject:
        What the episode concerns — an entry id, a judge name, a ``mutation_id``,
        a metric name, or the board sentinel ``"__board__"``.
    summary:
        One-line human-readable description (numbers inline).
    severity_rank, recency_key, coverage_key:
        The TOTAL-order ranking keys (§2). ``severity_rank`` per kind × intra
        severity; ``recency_key`` the max source-generation ordinal;
        ``coverage_key`` how many sources the episode folds. All descending.
    suggestion_hint:
        Which EVAL-SYNTHESIS.md §3 suggestion type this seeds (``HINT_*``).
    evidence:
        A source-specific bag (fidelity tier, drift kind, verdict, span, …).
    miner_version, source_episodes, source_refs, source_lineage_ids:
        The §4 provenance block. ``source_episodes`` is reserved for downstream
        composition (an episode folded from finer episodes); the miner emits it
        empty. ``source_refs`` are artifact refs (loss paths, adjudication
        paths, mutation ids); ``source_lineage_ids`` the generations that
        motivated it.
    """

    episode_id: str
    episode_type: str
    subject: str
    summary: str
    severity_rank: int
    recency_key: int
    coverage_key: int
    suggestion_hint: str
    evidence: dict[str, Any] = field(default_factory=dict)
    miner_version: str = MINER_VERSION
    source_episodes: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    source_lineage_ids: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "episode_type": self.episode_type,
            "subject": self.subject,
            "summary": self.summary,
            "severity_rank": self.severity_rank,
            "recency_key": self.recency_key,
            "coverage_key": self.coverage_key,
            "suggestion_hint": self.suggestion_hint,
            "evidence": dict(self.evidence),
            "provenance": {
                "miner_version": self.miner_version,
                "source_episodes": list(self.source_episodes),
                "source_refs": list(self.source_refs),
                "source_lineage_ids": list(self.source_lineage_ids),
            },
        }


def _episode_id(episode_type: str, subject: str, source_refs: tuple[str, ...]) -> str:
    """Content-stable id — a sha256 over the kind, subject, and sorted refs."""
    payload = "|".join([episode_type, subject, *sorted(source_refs)])
    return "ep-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _make_episode(
    *,
    episode_type: str,
    subject: str,
    summary: str,
    severity_rank: int,
    suggestion_hint: str,
    source_lineage_ids: tuple[str, ...] = (),
    source_refs: tuple[str, ...] = (),
    evidence: dict[str, Any] | None = None,
) -> MinedEpisode:
    """Assemble a :class:`MinedEpisode`, deriving the id + the ranking keys.

    ``recency_key`` = the max generation ordinal among the source lineage ids;
    ``coverage_key`` = how many source lineage ids (or refs) the episode folds.
    """
    lineage = tuple(source_lineage_ids)
    # Dedup refs (order-stable) BEFORE they seed the content-stable episode id, so
    # a repeated ref never inflates coverage or perturbs the id.
    refs = tuple(dict.fromkeys(source_refs))
    recency = max((_generation_ordinal(g) for g in lineage), default=0)
    coverage = len(lineage) or len(refs)
    return MinedEpisode(
        episode_id=_episode_id(episode_type, subject, refs),
        episode_type=episode_type,
        subject=subject,
        summary=summary,
        severity_rank=severity_rank,
        recency_key=recency,
        coverage_key=coverage,
        suggestion_hint=suggestion_hint,
        evidence=dict(evidence or {}),
        source_refs=refs,
        source_lineage_ids=lineage,
    )


# ---------------------------------------------------------------------------
# (a) FAILURE episodes — over the ObservationRun corpus (LossProfile-bound)
# ---------------------------------------------------------------------------

_FAIL_ABORT: str = "abort"
_FAIL_INFRA_ABORT: str = "infra_abort"
_FAIL_PREDICATE: str = "predicate_miss"
_FAIL_DRIFT: str = "drift_spike"


def _classify_failure(obs: ObservationRun) -> str | None:
    """The single most-severe failure class for one observed run, or ``None``.

    Priority — one run yields at most ONE failure class, never double-counted:
    an abort outranks a predicate miss outranks a critical drift spike. An
    **infra** abort (``is_infra_abort_cause`` — an endpoint 500, a transport
    error) is classed SEPARATELY (:data:`_FAIL_INFRA_ABORT`): it is a flake, NOT
    a candidate failure, so it never seeds a regression (SHOULD-FIX-C). A clean
    run (passed, no abort, no critical drift) yields ``None``.
    """
    if obs.aborted or obs.abort_cause:
        return _FAIL_INFRA_ABORT if is_infra_abort_cause(obs.abort_cause) else _FAIL_ABORT
    if obs.pass_fail is False:
        return _FAIL_PREDICATE
    for ev in obs.drift_events:
        if str(ev.get("severity", "")) == _SEVERITY_CRITICAL:
            return _FAIL_DRIFT
    return None


def failure_episodes(observations: list[ObservationRun]) -> list[MinedEpisode]:
    """FAILURE episodes from the observation corpus (EVAL-SYNTHESIS.md §2a).

    Groups failing runs by ``(entry_id, failure_class)`` so N replicates / N
    candidates failing one entry the same way fold into ONE episode (its source
    lineage = the failing candidates, its coverage = how many). An abort cascade
    and a predicate miss on the same entry stay DISTINCT episodes (different
    classes → different demand). All seed a regression entry that pins the
    failure.
    """
    grouped: dict[tuple[str, str], list[ObservationRun]] = {}
    for obs in observations:
        cls = _classify_failure(obs)
        if cls is None:
            continue
        grouped.setdefault((obs.entry_id, cls), []).append(obs)

    episodes: list[MinedEpisode] = []
    for (entry_id, cls), runs in grouped.items():
        lineage = tuple(dict.fromkeys(o.candidate_id for o in runs))  # dedup, order-stable
        refs = tuple(o.loss_ref for o in runs if o.loss_ref)
        best_fidelity = _best_fidelity(runs)
        hint = HINT_REGRESSION_ENTRY
        if cls == _FAIL_INFRA_ABORT:
            # An infra flake is NOT a candidate failure — it seeds no regression
            # (SHOULD-FIX-C). It rides mining output for operator visibility only.
            hint = HINT_INFRA_FLAKE
            severity = _SEV_INFRA_FLAKE
            cause = next((o.abort_cause for o in runs if o.abort_cause), "infra_abort")
            summary = (
                f"entry {entry_id!r} hit an infrastructure abort on {len(runs)} run(s) across "
                f"{len(lineage)} candidate(s) (cause {cause!r}) — an infra flake, not a candidate "
                f"failure; no regression is seeded"
            )
            evidence = {"failure_class": cls, "infra": True, "abort_cause": cause}
        elif cls == _FAIL_ABORT:
            severity = _SEV_ABORT
            cause = next((o.abort_cause for o in runs if o.abort_cause), "wall_clock_budget")
            summary = (
                f"entry {entry_id!r} aborted on {len(runs)} run(s) across "
                f"{len(lineage)} candidate(s) (cause {cause!r})"
            )
            evidence = {"failure_class": cls, "infra": False, "abort_cause": cause}
        elif cls == _FAIL_PREDICATE:
            severity = _SEV_PREDICATE_MISS
            summary = (
                f"entry {entry_id!r} failed its expectation on {len(runs)} run(s) "
                f"across {len(lineage)} candidate(s)"
            )
            evidence = {"failure_class": cls}
        else:  # drift spike
            severity = _SEV_DRIFT_SPIKE
            kinds = sorted(
                {
                    str(ev.get("kind", ""))
                    for o in runs
                    for ev in o.drift_events
                    if str(ev.get("severity", "")) == _SEVERITY_CRITICAL
                }
            )
            summary = (
                f"entry {entry_id!r} drew critical drift ({', '.join(kinds) or 'custom'}) "
                f"on {len(runs)} run(s) across {len(lineage)} candidate(s)"
            )
            evidence = {"failure_class": cls, "drift_kinds": kinds}
        evidence["fidelity"] = best_fidelity
        episodes.append(
            _make_episode(
                episode_type=EPISODE_FAILURE,
                subject=entry_id,
                summary=summary,
                severity_rank=severity,
                suggestion_hint=hint,
                source_lineage_ids=lineage,
                source_refs=refs,
                evidence=evidence,
            )
        )
    return episodes


def _best_fidelity(runs: list[ObservationRun]) -> str:
    """The strongest fidelity tier present across a group (verbatim > result > preview)."""
    from zicato.reflection.corpus import (  # noqa: PLC0415
        FIDELITY_PREVIEW,
        FIDELITY_RESULT,
        FIDELITY_VERBATIM,
    )

    order = {FIDELITY_VERBATIM: 3, FIDELITY_RESULT: 2, FIDELITY_PREVIEW: 1}
    return max((o.fidelity for o in runs), key=lambda f: order.get(f, 0), default=FIDELITY_PREVIEW)


# ---------------------------------------------------------------------------
# (b) JUDGE_DISAGREEMENT episodes — over the adjudicated corpus
# ---------------------------------------------------------------------------


def judge_disagreement_episodes(adjudications: list[JudgeAdjudication]) -> list[MinedEpisode]:
    """JUDGE_DISAGREEMENT episodes from FP/FN adjudications (EVAL-SYNTHESIS.md §2b).

    Groups the non-agreeing verdicts by ``(judge_name, verdict)``:

    * ``FN`` (judge silent, meta-judge found the failure exhibited) — the board
      has a real failure no judge catches; seeds a NEW judge / regression entry.
    * ``FP`` (judge fired, meta-judge found the transcript clean) — the
      criterion is too loose; seeds a rubric revision.

    Each episode carries the adjudicated spans + the meta-judge rationale as
    evidence; ``run_ref``s are the source refs.
    """
    grouped: dict[tuple[str, str], list[JudgeAdjudication]] = {}
    for a in adjudications:
        if a.verdict in (VERDICT_FP, VERDICT_FN):
            grouped.setdefault((a.judge_name, a.verdict), []).append(a)

    episodes: list[MinedEpisode] = []
    for (judge_name, verdict), items in grouped.items():
        refs = tuple(a.run_ref for a in items if a.run_ref)
        spans = [a.evidence_span for a in items if a.evidence_span]
        model = next((a.meta_judge_model for a in items if a.meta_judge_model), "")
        if verdict == VERDICT_FN:
            severity = _SEV_MISSED_FIRE
            hint = HINT_JUDGE
            summary = (
                f"judge {judge_name!r} missed {len(items)} real failure(s) the meta-judge "
                f"found exhibited — a blind spot the board does not catch"
            )
        else:  # FP
            severity = _SEV_FALSE_FIRE
            hint = HINT_RUBRIC_REVISION
            summary = (
                f"judge {judge_name!r} fired falsely on {len(items)} clean transcript(s) — "
                f"its criterion is too loose"
            )
        episodes.append(
            _make_episode(
                episode_type=EPISODE_JUDGE_DISAGREEMENT,
                subject=judge_name,
                summary=summary,
                severity_rank=severity,
                suggestion_hint=hint,
                source_refs=refs,
                evidence={
                    "verdict": verdict,
                    "count": len(items),
                    "spans": spans[:8],
                    "meta_judge_model": model,
                    "fidelity": _best_adj_fidelity(items),
                },
            )
        )
    return episodes


def _best_adj_fidelity(items: list[JudgeAdjudication]) -> str:
    from zicato.reflection.corpus import (  # noqa: PLC0415
        FIDELITY_PREVIEW,
        FIDELITY_RESULT,
        FIDELITY_VERBATIM,
    )

    order = {FIDELITY_VERBATIM: 3, FIDELITY_RESULT: 2, FIDELITY_PREVIEW: 1}
    return max((a.fidelity for a in items), key=lambda f: order.get(f, 0), default=FIDELITY_PREVIEW)


# ---------------------------------------------------------------------------
# (c) COVERAGE_GAP episodes — churned mutation points × a blind board
# ---------------------------------------------------------------------------


def coverage_gap_episodes(
    *,
    mutation_churn: dict[str, list[str]],
    discriminating_entries: int,
    dead_entries: list[str],
    min_churn: int = MIN_CHURN,
) -> list[MinedEpisode]:
    """COVERAGE_GAP episodes (EVAL-SYNTHESIS.md §2c).

    A mutation point the proposer keeps rewriting (``mutation_churn[id]`` = the
    generations that patched it, churn = its length) is a blind spot when the
    board discriminates NOTHING — ``discriminating_entries == 0`` over the
    reign's settled matchups (the MATCHUP-RECORD binding, not ``loss_profiles``
    pairs). The instrument cannot tell whether those rewrites help, so the
    churned point seeds a coverage entry that exercises its surface. When the
    board DOES discriminate somewhere, no gap is emitted (the instrument can
    measure). ``dead_entries`` ride the evidence as the proven-blind channels.
    """
    if discriminating_entries > 0:
        return []
    episodes: list[MinedEpisode] = []
    for mutation_id, gens in mutation_churn.items():
        churn = len(gens)
        if churn < min_churn:
            continue
        lineage = tuple(dict.fromkeys(gens))
        episodes.append(
            _make_episode(
                episode_type=EPISODE_COVERAGE_GAP,
                subject=mutation_id,
                summary=(
                    f"mutation point {mutation_id!r} churned in {churn} generation(s) but the "
                    f"board discriminates nothing — no entry measures whether the rewrites help"
                ),
                severity_rank=_SEV_COVERAGE_GAP,
                suggestion_hint=HINT_COVERAGE_ENTRY,
                source_lineage_ids=lineage,
                source_refs=(mutation_id,),
                evidence={"churn": churn, "dead_entries": sorted(dead_entries)[:16]},
            )
        )
    return episodes


# ---------------------------------------------------------------------------
# (d) UNRESOLVED_CLAIM episodes — the hypothesis calibration ledger
# ---------------------------------------------------------------------------


def unresolved_claim_episodes(ledger: list[Any]) -> list[MinedEpisode]:
    """UNRESOLVED_CLAIM episodes (EVAL-SYNTHESIS.md §2d).

    A :class:`~zicato.tournament.detail.HypothesisGrade` carries per-metric
    ``movements``. A movement the proposer predicted but the outcome NEVER
    recorded (``actual_from is None and actual_to is None`` — the
    ``_grade_movement`` "predicted a movement the outcome never recorded" case)
    names a channel the board has no entry/judge for. Grouped by
    ``metric_name`` → one episode per unmeasured metric; a ``drift:``-namespaced
    metric seeds a judge, everything else a coverage entry (a predicate).
    """
    by_metric: dict[str, list[str]] = {}
    for grade in ledger:
        gen = str(getattr(grade, "generation_id", "") or "")
        for mv in getattr(grade, "movements", ()) or ():
            if getattr(mv, "actual_from", None) is None and getattr(mv, "actual_to", None) is None:
                metric = str(getattr(mv, "metric_name", "") or "")
                if metric:
                    by_metric.setdefault(metric, []).append(gen)

    episodes: list[MinedEpisode] = []
    for metric, gens in by_metric.items():
        lineage = tuple(dict.fromkeys(g for g in gens if g))
        hint = HINT_JUDGE if metric.startswith("drift:") else HINT_COVERAGE_ENTRY
        episodes.append(
            _make_episode(
                episode_type=EPISODE_UNRESOLVED_CLAIM,
                subject=metric,
                summary=(
                    f"proposer predicted movement on {metric!r} across {len(gens)} "
                    f"hypothesis(es) but nothing on the board measures it"
                ),
                severity_rank=_SEV_UNRESOLVED_CLAIM,
                suggestion_hint=hint,
                source_lineage_ids=lineage,
                source_refs=(metric,),
                evidence={"metric": metric, "predictions": len(gens)},
            )
        )
    return episodes


# ---------------------------------------------------------------------------
# (e) STALENESS episodes — dead entries + generalization-gap firings
# ---------------------------------------------------------------------------


def staleness_episodes(
    *,
    dead_entries: list[dict[str, Any]],
    gap_findings: list[Any],
) -> list[MinedEpisode]:
    """STALENESS episodes (EVAL-SYNTHESIS.md §2e).

    Two instrument-panel signals seed harder-variant demand:

    * each DEAD entry (``build_eval_health``'s ``dead`` list — zero
      discrimination over the reign's settled matchups) is a saturated channel
      → a harder variant that restores discrimination;
    * each generalization-gap firing (``detect_generalization_gap``) is a
      board-wide memorization signal → harder-variant demand across the board
      (subject ``"__board__"``), carrying the gap detail + its remediation.
    """
    episodes: list[MinedEpisode] = []
    for item in dead_entries:
        eid = str(item.get("entry_id", "")) if isinstance(item, dict) else ""
        if not eid:
            continue
        pairs = item.get("discrimination_pairs") if isinstance(item, dict) else None
        episodes.append(
            _make_episode(
                episode_type=EPISODE_STALENESS,
                subject=eid,
                summary=(
                    f"entry {eid!r} is dead — it never separated any of "
                    f"{pairs if pairs is not None else '?'} settled matchup(s); harden it"
                ),
                severity_rank=_SEV_STALENESS,
                suggestion_hint=HINT_HARDER_VARIANT,
                source_refs=(eid,),
                evidence={"reason": "dead_entry", "discrimination_pairs": pairs},
            )
        )
    for finding in gap_findings:
        severity_tok = str(getattr(finding, "severity", "warning"))
        detail = getattr(finding, "detail", {}) or {}
        gen = str(detail.get("generation_id", "")) if isinstance(detail, dict) else ""
        episodes.append(
            _make_episode(
                episode_type=EPISODE_STALENESS,
                subject="__board__",
                summary=str(
                    getattr(finding, "summary", "")
                    or "generalization gap widened — harden the board"
                ),
                severity_rank=_SEV_GAP_CRIT
                if severity_tok == _SEVERITY_CRITICAL
                else _SEV_GAP_WARN,
                suggestion_hint=HINT_HARDER_VARIANT,
                source_lineage_ids=(gen,) if gen else (),
                source_refs=(f"generalization_gap:{gen or 'board'}",),
                evidence={
                    "reason": "generalization_gap",
                    "severity": severity_tok,
                    "detail": detail if isinstance(detail, dict) else {},
                },
            )
        )
    return episodes


# ---------------------------------------------------------------------------
# Ranking — the deterministic TOTAL order (EVAL-SYNTHESIS.md §2)
# ---------------------------------------------------------------------------


def rank_episodes(episodes: list[MinedEpisode]) -> list[MinedEpisode]:
    """Sort by ``(−severity, −recency, −coverage, episode_id)`` — a TOTAL order.

    The ``episode_id`` tiebreak (content-stable, ascending) makes the order
    total and INDEPENDENT of the input walk: the same episode set yields a
    byte-stable ranking in any order (the eval-view fixture discipline).
    """
    return sorted(
        episodes,
        key=lambda e: (-e.severity_rank, -e.recency_key, -e.coverage_key, e.episode_id),
    )


# ---------------------------------------------------------------------------
# The I/O orchestrator
# ---------------------------------------------------------------------------


def mine_episodes(paths: Any, epoch_id: str | None = None) -> list[MinedEpisode]:
    """Mine every episode kind for one epoch and return them ranked (§9).

    The one place that touches disk: resolve the epoch, build the observation
    corpus (:func:`~zicato.reflection.corpus.ingest_lineage`, zero LLM), read
    the latest reflection's adjudications, read the experiments (patch churn) +
    the instrument health (dead entries + discrimination), read the hypothesis
    ledger, run the generalization-gap detector, call each pure extractor, and
    rank. EVERY read is best-effort: a cold workspace, an unknown epoch, an
    absent reflection / index, or a malformed artifact degrades to fewer
    episodes — never an exception.
    """
    from zicato.query.paths import _resolve_epoch_id  # noqa: PLC0415

    try:
        resolved = _resolve_epoch_id(paths, epoch_id)
    except ValueError:
        return []
    if not resolved:
        return []

    observations = _load_observations(paths, resolved)
    adjudications = _load_latest_adjudications(paths, resolved)
    experiments = _load_experiments(paths, resolved)
    mutation_churn = _mutation_churn(experiments)
    dead_entries = _dead_entries(paths, resolved)
    discriminating = _discriminating_entry_count(paths, resolved, experiments)
    ledger = _load_ledger(paths, resolved)
    gap_findings = _gap_findings(experiments)

    episodes: list[MinedEpisode] = []
    episodes.extend(failure_episodes(observations))
    episodes.extend(judge_disagreement_episodes(adjudications))
    episodes.extend(
        coverage_gap_episodes(
            mutation_churn=mutation_churn,
            discriminating_entries=discriminating,
            dead_entries=[str(d.get("entry_id", "")) for d in dead_entries],
        )
    )
    episodes.extend(unresolved_claim_episodes(ledger))
    episodes.extend(staleness_episodes(dead_entries=dead_entries, gap_findings=gap_findings))
    return rank_episodes(episodes)


# --- orchestrator readers (each tolerant, degrade to empty) ----------------


def _load_observations(paths: Any, epoch_id: str) -> list[ObservationRun]:
    """The passive observation corpus over the epoch's lineage (zero LLM)."""
    from zicato.reflection.corpus import ingest_lineage  # noqa: PLC0415

    try:
        candidates = _candidate_ids(paths, epoch_id)
        entries = _entry_ids(paths, epoch_id)
        if not candidates or not entries:
            return []
        weights = _scoring_weights(paths, epoch_id)
        return ingest_lineage(
            workspace_root=paths.root,
            epoch_id=epoch_id,
            reflection_id="mining",
            candidates=candidates,
            entries=entries,
            weights=weights,
        )
    except Exception:  # noqa: BLE001 — best-effort; a defect degrades to no corpus
        return []


def _candidate_ids(paths: Any, epoch_id: str) -> list[str]:
    from zicato.query.eval_view import _candidate_axis  # noqa: PLC0415

    try:
        return [str(c["generation_id"]) for c in _candidate_axis(paths, epoch_id)]
    except Exception:  # noqa: BLE001
        return []


def _entry_ids(paths: Any, epoch_id: str) -> list[str]:
    from zicato.query.eval_view import _load_board_entries  # noqa: PLC0415

    try:
        return [str(e.id) for e in _load_board_entries(paths, epoch_id) if getattr(e, "id", "")]
    except Exception:  # noqa: BLE001
        return []


def _scoring_weights(paths: Any, epoch_id: str) -> Any:
    from zicato.core import ScoringWeights  # noqa: PLC0415

    try:
        from zicato.query.paths import _read_json_value, layout_of  # noqa: PLC0415
        from zicato.workspace_loader import scoring_weights_from_dict  # noqa: PLC0415

        raw = _read_json_value(layout_of(paths).epoch_dir(epoch_id) / "scoring.json")
        if isinstance(raw, dict):
            return scoring_weights_from_dict(raw)
    except Exception:  # noqa: BLE001 — a missing/bad scoring.json → defaults
        pass
    return ScoringWeights()


def _load_latest_adjudications(paths: Any, epoch_id: str) -> list[JudgeAdjudication]:
    """Every adjudication under the epoch's most recent reflection (or ``[]``).

    Walks ``adjudication/{judge_name}/*.json`` under the latest reflection dir
    with the tolerant :func:`~zicato.reflection.adjudicator.read_adjudication`
    reader; an unreadable verdict file is skipped, an epoch with no reflection
    yields ``[]`` (an honest zero, never a fabricated flip).
    """
    from zicato.reflection.adjudicator import read_adjudication  # noqa: PLC0415

    try:
        reflection_id = _latest_reflection_id(paths, epoch_id)
        if reflection_id is None:
            return []
        from zicato.core.workspace import reflection_adjudication_dir  # noqa: PLC0415

        adj_root = reflection_adjudication_dir(paths.root, epoch_id, reflection_id)
        if not adj_root.is_dir():
            return []
        out: list[JudgeAdjudication] = []
        for judge_dir in sorted(adj_root.iterdir()):
            if not judge_dir.is_dir():
                continue
            for verdict_file in sorted(judge_dir.iterdir()):
                if verdict_file.suffix != ".json":
                    continue
                adj = read_adjudication(verdict_file)
                if adj is not None:
                    out.append(adj)
        return out
    except Exception:  # noqa: BLE001 — best-effort
        return []


def _latest_reflection_id(paths: Any, epoch_id: str) -> str | None:
    from zicato.query.reflection_view import list_reflections  # noqa: PLC0415

    try:
        items = list_reflections(paths, epoch_id).get("reflections", [])
    except Exception:  # noqa: BLE001
        return None
    for item in items:  # already newest-first
        rid = item.get("reflection_id")
        if isinstance(rid, str) and rid:
            return rid
    return None


def _load_experiments(paths: Any, epoch_id: str) -> list[dict[str, Any]]:
    from zicato.query.epoch_view import _read_epoch_experiments  # noqa: PLC0415
    from zicato.query.paths import layout_of  # noqa: PLC0415

    try:
        return _read_epoch_experiments(layout_of(paths).epoch_dir(epoch_id))
    except Exception:  # noqa: BLE001
        return []


def _mutation_churn(experiments: list[dict[str, Any]]) -> dict[str, list[str]]:
    """``{mutation_id: [generation_ids that patched it]}`` from the patch history.

    Bound to ``_read_epoch_experiments``'s per-generation ``patches`` map
    (keyed by ``mutation_id``, ``epoch_view.py``). A mutation id rewritten in N
    generations has churn N.
    """
    churn: dict[str, list[str]] = {}
    for exp in experiments:
        if not isinstance(exp, dict):
            continue
        gen = str(exp.get("generation_id", "") or "")
        patches = exp.get("patches")
        if not isinstance(patches, dict):
            continue
        for mutation_id in patches:
            if isinstance(mutation_id, str) and mutation_id:
                churn.setdefault(mutation_id, []).append(gen)
    return churn


def _dead_entries(paths: Any, epoch_id: str) -> list[dict[str, Any]]:
    from zicato.query.eval_view import build_eval_health  # noqa: PLC0415

    try:
        health = build_eval_health(paths, epoch_id)
        dead = health.get("dead", [])
        return [d for d in dead if isinstance(d, dict)]
    except Exception:  # noqa: BLE001
        return []


def _discriminating_entry_count(
    paths: Any, epoch_id: str, experiments: list[dict[str, Any]]
) -> int:
    """How many entries discriminate — the MATCHUP-RECORD binding (EVAL-VIEW §2.3).

    Reuses ``_discrimination_by_entry`` (the reign's settled matchups via
    ``build_matchup_grid``) so the count agrees byte-for-byte with the
    instrument panel; an entry counts when its rate is positive over at least
    the minimum comparisons.
    """
    from zicato.query.eval_view import (  # noqa: PLC0415
        _MIN_DISCRIMINATION_COMPARISONS,
        _discrimination_by_entry,
    )

    try:
        dmap = _discrimination_by_entry(paths, epoch_id, experiments)
    except Exception:  # noqa: BLE001
        return 0
    return sum(
        1
        for rate, pairs in dmap.values()
        if rate is not None and rate > 0.0 and pairs >= _MIN_DISCRIMINATION_COMPARISONS
    )


def _load_ledger(paths: Any, epoch_id: str) -> list[Any]:
    from zicato.tournament.detail import hypothesis_ledger  # noqa: PLC0415

    try:
        return list(hypothesis_ledger(paths.index_db, epoch_id))
    except Exception:  # noqa: BLE001 — no index / cold db → no ledger
        return []


def _gap_findings(experiments: list[dict[str, Any]]) -> list[Any]:
    from zicato.health.diagnostics import detect_generalization_gap  # noqa: PLC0415

    try:
        return list(detect_generalization_gap(experiments))
    except Exception:  # noqa: BLE001
        return []


__all__ = [
    "EPISODE_COVERAGE_GAP",
    "EPISODE_FAILURE",
    "EPISODE_JUDGE_DISAGREEMENT",
    "EPISODE_STALENESS",
    "EPISODE_UNRESOLVED_CLAIM",
    "HINT_COVERAGE_ENTRY",
    "HINT_HARDER_VARIANT",
    "HINT_INFRA_FLAKE",
    "HINT_JUDGE",
    "HINT_REGRESSION_ENTRY",
    "HINT_RUBRIC_REVISION",
    "MINER_VERSION",
    "MIN_CHURN",
    "MinedEpisode",
    "coverage_gap_episodes",
    "failure_episodes",
    "judge_disagreement_episodes",
    "mine_episodes",
    "rank_episodes",
    "staleness_episodes",
    "unresolved_claim_episodes",
]
