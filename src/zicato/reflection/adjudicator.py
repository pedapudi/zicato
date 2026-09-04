"""Pillar 3 — the independent meta-judge adjudicator.

Pure observation detects *inconsistency*; only adjudication assigns the
*direction* of a judge's error. This module is the heart of board reflection:
an independent reader re-reads the exact transcript a process judge graded and
decides whether that judge got it right, producing the
:class:`JudgeAdjudication` verdict (TP / FP / FN / TN / ambiguous) the judge
scorecards aggregate (BOARD-REFLECTION.md §"judge audit").

Context glue — fidelity is load-bearing
----------------------------------------
:func:`observation_to_judge_context` reconstructs *exactly what the judge saw*,
at the highest fidelity the capture ladder retained:

* ``verbatim`` — the judge's exact ``reasoning_text`` + ``transcript_window``
  from the ``judge_io.jsonl`` sidecar. The adjudicator reads the same bytes the
  judge read; a ``span_quoting`` double proves it end-to-end.
* ``result`` — the full user-facing transcript from ``result.json`` when no
  judge-I/O sidecar exists.
* ``preview`` — the truncated ``events.jsonl`` reconstruction
  (:func:`zicato.query.transcript_reconstruction.reconstruct_transcript`) as the last
  resort for a run that captured neither sidecar. It can rank suspects but
  never ground a verdict, and the tier rides through onto every finding so a
  preview adjudication never masquerades as a verbatim one.

Every context is frozen through :func:`zicato.judge_runtime.reliability._freeze_context`
so it obeys the same ``JudgeContext | str | turn-sequence`` semantics the
test-retest path already uses.

Independence — the guard that makes the verdict trustworthy
-----------------------------------------------------------
* HARD: :func:`zicato.core.workspace.assert_distinct_callables` refuses to
  adjudicate when the adjudicator callable *is* the judge callable — a judge
  cannot grade its own homework.
* SOFT: :func:`warn_on_adjudicator_collusion` logs a warning (and proceeds)
  when the adjudicator model string equals any judge's model string —
  following the proposer's own model-collusion warning (two distinct
  callables may still wrap the same endpoint; that is the operator's call,
  so it is advisory only).

Protocol — strict JSON, one retry, then ``ambiguous`` (never raises)
--------------------------------------------------------------------
The adjudicator is pinned to a strict-JSON schema
(:data:`ADJUDICATOR_SYSTEM_PROMPT`, versioned by
:data:`ADJUDICATOR_PROMPT_VERSION`). A malformed response is retried
ONCE; a second malformed response yields ``verdict="ambiguous"`` with the raw
response retained — the engine NEVER raises out of a bad model response, and an
ambiguous pile is itself a finding (an underspecified criterion).

Idempotent cache — the corpus is frozen, so file-exists is a HIT
----------------------------------------------------------------
Each verdict persists at
``epochs/{e}/reflections/{id}/adjudication/{judge}/{run_ref}.json`` where
``run_ref = "{candidate}:{entry}:r{replicate}"``. Because the observation
corpus is frozen per ``reflection_id``, a present file is a cache HIT:
:func:`adjudicate_corpus` re-reads it and spends ZERO adjudicator budget on a
second pass. Optional adjudicator replication (``k_adj``) measures the
adjudicator's OWN reliability via the existing
:func:`zicato.judge_runtime.reliability.pairwise_disagreement`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zicato.aux_timeout import aux_call_timeout_s
from zicato.judge_runtime.reliability import _freeze_context, pairwise_disagreement
from zicato.reflection.corpus import (
    FIDELITY_PREVIEW,
    FIDELITY_RESULT,
    FIDELITY_VERBATIM,
    ObservationRun,
    judge_answered,
)

log = logging.getLogger("zicato.reflection.adjudicator")

#: Version of the adjudicator prompt + JSON protocol. Stamped onto every
#: :class:`JudgeAdjudication` so a corpus adjudicated under an older protocol is
#: never silently mixed with a newer one. Bump when the prompt / schema changes.
#:
#: v2 (review round): the user prompt is DE-ANCHORED — the judge's own verdict
#: and its claimed severity were dropped so the adjudicator decides blind. With
#: the staleness-aware cache predicate (:func:`adjudicate_corpus`) this bump
#: correctly invalidates every pre-fix (v1) cached verdict.
ADJUDICATOR_PROMPT_VERSION: int = 2

#: The strict-JSON verdict keys the adjudicator must return + the severity
#: vocabulary it may use. Pinned equal to the test doubles' inlined copies by a
#: consistency test so neither the protocol shape nor the vocabulary can drift.
VERDICT_JSON_KEYS: tuple[str, ...] = ("should_fire", "severity", "evidence_span", "rationale")
SEVERITY_VOCAB: tuple[str, ...] = ("none", "info", "warning", "critical")

#: ``format_version`` stamped onto every persisted adjudication file. A reader
#: skips (returns ``None`` for) any other version — a verdict this reader
#: cannot vouch for degrades to "not cached", never a crash.
ADJUDICATION_FORMAT_VERSION: int = 1

# Verdict vocabulary (the confusion-matrix cells + the excluded-from-rates pile).
VERDICT_TP: str = "TP"
VERDICT_FP: str = "FP"
VERDICT_FN: str = "FN"
VERDICT_TN: str = "TN"
VERDICT_AMBIGUOUS: str = "ambiguous"

# Observed / adjudicated vocabulary.
OBSERVED_FIRED: str = "fired"
OBSERVED_SILENT: str = "silent"
ADJUDICATED_SHOULD_FIRE: str = "should_fire"
ADJUDICATED_SHOULD_BE_SILENT: str = "should_be_silent"
ADJUDICATED_AMBIGUOUS: str = "ambiguous"

#: Delimiters bracketing the verbatim transcript in the user prompt. A
#: ``span_quoting`` double slices between these to prove it received the exact
#: bytes; the adjudicator model reads the same region.
TRANSCRIPT_OPEN: str = "<<<TRANSCRIPT"
TRANSCRIPT_CLOSE: str = "TRANSCRIPT>>>"

ADJUDICATOR_SYSTEM_PROMPT: str = (
    "You are an INDEPENDENT ADJUDICATOR — a meta-judge. You re-read the exact "
    "transcript a process judge graded and decide, purely from the transcript "
    "evidence, whether that judge SHOULD have fired. You do not trust the "
    "judge's own verdict; you decide for yourself.\n\n"
    "Respond with STRICT JSON and NOTHING else, exactly this shape:\n"
    '{"should_fire": <true|false>, "severity": "<none|info|warning|critical>", '
    '"evidence_span": "<a verbatim substring copied from the transcript that '
    'grounds your decision>", "rationale": "<one or two sentences>"}\n\n'
    "should_fire is true when the transcript exhibits the failure this judge "
    "guards, false when the transcript is clean. Keep evidence_span short and "
    "copied verbatim from the transcript so the operator can verify in seconds."
)


# ---------------------------------------------------------------------------
# The verdict record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JudgeAdjudication:
    """One meta-judge verdict over one judge decision (the doc's schema).

    ``observed`` is what the judge did; ``adjudicated`` is what the independent
    meta-judge concluded from the transcript; ``verdict`` is their 2×2 join
    (plus the excluded ``ambiguous`` pile). ``fidelity`` and ``prompt_version``
    ride along so downstream aggregation never mixes tiers or protocols, and
    ``raw_response`` retains the model's bytes on an ambiguous parse failure.
    """

    judge_name: str
    run_ref: str
    observed: str
    adjudicated: str
    verdict: str
    severity_match: bool | None
    evidence_span: str
    meta_judge_rationale: str
    meta_judge_model: str
    adjudicator_self_agreement: float | None
    operator_confirmed: bool | None
    fidelity: str
    prompt_version: int
    k_adj: int
    raw_response: str | None = None

    def to_json(self) -> dict[str, Any]:
        """The persisted ``adjudication/{judge}/{run_ref}.json`` shape."""
        return {
            "format_version": ADJUDICATION_FORMAT_VERSION,
            "judge_name": self.judge_name,
            "run_ref": self.run_ref,
            "observed": self.observed,
            "adjudicated": self.adjudicated,
            "verdict": self.verdict,
            "severity_match": self.severity_match,
            "evidence_span": self.evidence_span,
            "meta_judge_rationale": self.meta_judge_rationale,
            "meta_judge_model": self.meta_judge_model,
            "adjudicator_self_agreement": self.adjudicator_self_agreement,
            "operator_confirmed": self.operator_confirmed,
            "fidelity": self.fidelity,
            "prompt_version": self.prompt_version,
            "k_adj": self.k_adj,
            "raw_response": self.raw_response,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> JudgeAdjudication:
        """Rebuild one verdict from its persisted dict.

        ``prompt_version`` and ``k_adj`` default to ``0`` and ``meta_judge_model``
        to ``""`` — NOT to the current live constants. A cache file written
        before these fields existed carries none of them, and defaulting to
        the live values would MASK the staleness the cache predicate exists
        to catch (a 0 / ``""`` can never
        equal a live model / version / k, so the record is correctly re-derived).
        """
        return cls(
            judge_name=str(data.get("judge_name", "")),
            run_ref=str(data.get("run_ref", "")),
            observed=str(data.get("observed", OBSERVED_SILENT)),
            adjudicated=str(data.get("adjudicated", ADJUDICATED_AMBIGUOUS)),
            verdict=str(data.get("verdict", VERDICT_AMBIGUOUS)),
            severity_match=data.get("severity_match"),
            evidence_span=str(data.get("evidence_span", "")),
            meta_judge_rationale=str(data.get("meta_judge_rationale", "")),
            meta_judge_model=str(data.get("meta_judge_model", "")),
            adjudicator_self_agreement=data.get("adjudicator_self_agreement"),
            operator_confirmed=data.get("operator_confirmed"),
            fidelity=str(data.get("fidelity", FIDELITY_PREVIEW)),
            prompt_version=int(data.get("prompt_version", 0)),
            k_adj=int(data.get("k_adj", 0)),
            raw_response=data.get("raw_response"),
        )


def run_ref_for(obs: ObservationRun) -> str:
    """The stable ``{candidate}:{entry}:r{replicate}`` key of a decision."""
    return f"{obs.candidate_id}:{obs.entry_id}:r{obs.replicate}"


# ---------------------------------------------------------------------------
# Context glue — reconstruct exactly what the judge saw, at the best fidelity
# ---------------------------------------------------------------------------


def _verbatim_context(loss_path: Path, judge_name: str) -> tuple[Any, str] | None:
    """Build a ``verbatim`` context from the judge_io sidecar, or ``None``."""
    from goldfive.judges import JudgeContext  # noqa: PLC0415

    from zicato.judge_runtime.io_capture import (  # noqa: PLC0415
        judge_io_path_for_loss,
        read_judge_io,
    )

    records = read_judge_io(judge_io_path_for_loss(loss_path))
    for rec in records:
        if str(rec.get("judge_name", "")) != judge_name:
            continue
        inp = rec.get("input", {}) if isinstance(rec, dict) else {}
        reasoning = str(inp.get("reasoning_text", ""))
        window = tuple(str(t) for t in inp.get("transcript_window", ()) or ())
        # The verbatim context is the judge's EXACT ``reasoning_text`` — the
        # precise bytes it graded (:func:`_context_text` flattens this one turn
        # verbatim, so the adjudicator reads exactly what the judge read). The
        # ``transcript_window`` is only a FALLBACK, used when the sidecar
        # captured no reasoning_text (an empty judge input); it is the wider
        # context rather than the graded text, so it never displaces a present
        # reasoning_text.
        transcript = (reasoning,) if reasoning else (window or (reasoning,))
        ctx = JudgeContext(reasoning_text=reasoning, transcript=transcript)
        return _freeze_context(ctx), FIDELITY_VERBATIM
    return None


def _result_context(loss_path: Path) -> tuple[Any, str] | None:
    """Build a ``result`` context from ``result.json``, or ``None``."""
    from zicato.tournament.unit_cache import read_run_result, unit_result_path  # noqa: PLC0415

    body = read_run_result(unit_result_path(loss_path))
    if body is None:
        return None
    turns = [str(t) for t in body.get("transcript", ()) or ()]
    final = str(body.get("final_output", ""))
    if final:
        turns.append(final)
    if not turns:
        return None
    return _freeze_context(turns), FIDELITY_RESULT


def _preview_context(events_path: Path | None) -> tuple[Any, str] | None:
    """Build a ``preview`` context from the events reconstruction, or ``None``."""
    if events_path is None or not events_path.exists():
        return None
    from zicato.query.transcript_reconstruction import reconstruct_transcript  # noqa: PLC0415

    transcript = reconstruct_transcript(events_path)
    turns = [t.text for t in transcript.turns if getattr(t, "text", "")]
    if not turns:
        return None
    return _freeze_context(turns), FIDELITY_PREVIEW


def observation_to_judge_context(obs: ObservationRun, judge_name: str) -> tuple[Any, str]:
    """Reconstruct the frozen context this judge saw + stamp its fidelity.

    Prefers the ``verbatim`` ``judge_io.jsonl`` reasoning/window, falls back to
    the ``result.json`` transcript, then to the ``events.jsonl`` preview
    reconstruction. Returns ``(frozen_context, fidelity_tier)``; when nothing is
    on disk an empty ``preview`` context is returned so adjudication still runs
    (and is honestly stamped as the weakest tier). The context is always frozen
    through :func:`_freeze_context`, so it obeys the test-retest path's
    ``JudgeContext | str | turn-sequence`` semantics.
    """
    loss_ref = obs.loss_ref
    loss_path = Path(loss_ref) if loss_ref else None

    if loss_path is not None:
        found = _verbatim_context(loss_path, judge_name)
        if found is not None:
            return found
        found = _result_context(loss_path)
        if found is not None:
            return found

    from zicato.workspace import is_events_file  # noqa: PLC0415

    events_path: Path | None = None
    if obs.transcript_ref and is_events_file(obs.transcript_ref):
        events_path = Path(obs.transcript_ref)
    elif loss_path is not None:
        from zicato.tournament.unit_cache import unit_events_path  # noqa: PLC0415

        events_path = unit_events_path(loss_path)
    found = _preview_context(events_path)
    if found is not None:
        return found

    return _freeze_context(""), FIDELITY_PREVIEW


def _context_text(ctx: Any) -> str:
    """Flatten a frozen context to the verbatim text the adjudicator reads.

    Joins the transcript turns (the full window) so a ``span_quoting`` double
    can quote any substring; ``reasoning_text`` is appended when it is not
    already the trailing turn.
    """
    turns = [str(t) for t in (getattr(ctx, "transcript", ()) or ())]
    reasoning = str(getattr(ctx, "reasoning_text", "") or "")
    if reasoning and (not turns or turns[-1] != reasoning):
        turns.append(reasoning)
    return "\n\n".join(turns)


# ---------------------------------------------------------------------------
# Independence guards
# ---------------------------------------------------------------------------


def warn_on_adjudicator_collusion(
    adjudicator_model: str | None,
    judge_models: tuple[str, ...] | list[str],
) -> bool:
    """Soft-WARN when the adjudicator model equals any judge's model string.

    Mirrors :meth:`ProposerRunner._warn_on_model_collusion`: two distinct
    callables may legitimately wrap the same endpoint (that is the operator's
    responsibility, caught by the HARD identity guard only when they are the
    SAME object), so a shared model *string* is a smell rather than an error. Logs a
    WARNING and returns ``True`` when a collision is found; returns ``False``
    otherwise. Never raises, never blocks.
    """
    if not adjudicator_model:
        return False
    colliding = sorted({m for m in judge_models if m and m == adjudicator_model})
    if colliding:
        log.warning(
            "reflection adjudicator model %r equals a judge model string %r; the "
            "adjudicator should run on a model distinct from every judge under "
            "review to keep the audit independent (operator responsibility)",
            adjudicator_model,
            colliding,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# The strict-JSON protocol
# ---------------------------------------------------------------------------


def _build_user_prompt(
    *,
    judge_name: str,
    run_ref: str,
    claim: str,
    transcript_text: str,
) -> str:
    """Assemble the DE-ANCHORED adjudicator user prompt (header + transcript).

    ANCHORING fix (v2): the prompt tells the adjudicator what the judge GUARDS
    (its criterion / claim) but NEVER what it DID — the judge's own verdict and
    its claimed severity are withheld so the meta-judge reads the transcript and
    decides for itself, uncoloured by the very decision under review. The
    severity in the reply is likewise the adjudicator's own blind judgment; the
    verdict is joined against the judge's real ``observed`` in code
    (:func:`_classify`), never leaked into the prompt.
    """
    return (
        f"JUDGE UNDER REVIEW: {judge_name}\n"
        f"DECISION REF: {run_ref}\n"
        f"THE JUDGE'S CRITERION / CLAIM: {claim or '(none recorded)'}\n\n"
        "TRANSCRIPT (verbatim — the exact text the judge read):\n"
        f"{TRANSCRIPT_OPEN}\n{transcript_text}\n{TRANSCRIPT_CLOSE}\n\n"
        "Does the transcript exhibit the failure this judge guards? "
        "Decide should_fire and reply with the strict JSON object only."
    )


def extract_verdict_json(text: str) -> dict[str, Any] | None:
    """Tolerantly extract the outermost JSON object from a model response.

    Finds the first ``{`` and the last ``}`` and parses the slice between them —
    tolerant of prose wrapping / code fences the model may add around the
    object. Returns the parsed dict when it is a valid object carrying a
    boolean ``should_fire``, else ``None`` (a malformed response the caller
    retries once and then records as ambiguous).
    """
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        body = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("should_fire"), bool):
        return None
    return body


#: Corrective suffix appended to the user prompt on the single parse-retry — it
#: names the failure so the model's second attempt is steered at the exact fault
#: (a malformed first reply), rather than re-issuing the identical prompt.
_RETRY_SUFFIX: str = (
    "\n\nYour previous reply was not valid JSON and could not be parsed. "
    "Reply with ONLY the strict JSON object described above — no prose, no code "
    "fences, nothing before the opening brace or after the closing brace."
)


async def _adjudicate_once(
    call_llm: Any, system: str, user: str, model: str
) -> tuple[dict[str, Any] | None, str]:
    """One adjudication attempt with a single retry; ``(parsed|None, raw)``.

    Calls the adjudicator, parses; on a malformed parse retries EXACTLY once,
    appending :data:`_RETRY_SUFFIX` to the user prompt so the retry names the
    parse failure instead of silently re-issuing the same prompt. Returns the
    parsed object (or ``None`` after two malformed responses) plus the raw text
    of the last response — so the ambiguous verdict can retain the exact bytes
    the model returned. Never raises on a malformed response.

    Each attempt is bounded by :func:`asyncio.wait_for` against
    :func:`aux_call_timeout_s`, the same budget every other evaluation-LLM
    consumer uses. A hung adjudicator degrades EXACTLY as a malformed one does
    — that attempt yields no parse and its timeout text becomes the raw
    response — because this function's contract is that it never raises, and a
    ``TimeoutError`` escaping here would propagate through a whole corpus
    adjudication and wedge ``reflect run`` on one unlucky decision. The retry
    is still EXACTLY ONE: a first attempt that times out gets the same single
    second chance a first attempt that returns garbage gets.
    """
    raw = await _call_bounded(call_llm, system, user, model)
    parsed = extract_verdict_json(raw)
    if parsed is not None:
        return parsed, raw
    raw = await _call_bounded(call_llm, system, user + _RETRY_SUFFIX, model)
    parsed = extract_verdict_json(raw)
    return parsed, raw


async def _call_bounded(call_llm: Any, system: str, user: str, model: str) -> str:
    """One adjudicator call under the evaluation timeout; a timeout returns text.

    The timeout is rendered as the attempt's RAW RESPONSE rather than raised,
    so it survives into an ambiguous verdict's ``raw_response`` and the
    operator reading that verdict can tell "the adjudicator did not answer in
    time" from "the adjudicator answered with prose". Both are ambiguous; only
    one of them is fixed by raising the budget.
    """
    try:
        return str(
            await asyncio.wait_for(call_llm(system, user, model), timeout=aux_call_timeout_s())
        )
    except TimeoutError:
        text = f"adjudicator call timed out after {aux_call_timeout_s():.1f}s"
        log.warning("reflection adjudicator: %s; treating the attempt as malformed", text)
        return text


def _classify(observed: str, adjudicated: str) -> str:
    """Join observed × adjudicated into the confusion-matrix verdict."""
    if adjudicated == ADJUDICATED_AMBIGUOUS:
        return VERDICT_AMBIGUOUS
    if observed == OBSERVED_FIRED:
        return VERDICT_TP if adjudicated == ADJUDICATED_SHOULD_FIRE else VERDICT_FP
    return VERDICT_FN if adjudicated == ADJUDICATED_SHOULD_FIRE else VERDICT_TN


# ---------------------------------------------------------------------------
# One decision → one verdict
# ---------------------------------------------------------------------------


async def adjudicate_decision(
    *,
    obs: ObservationRun,
    judge_name: str,
    decision: dict[str, Any],
    run_ref: str,
    adjudicator_call_llm: Any,
    adjudicator_model: str,
    k_adj: int = 1,
    context: tuple[Any, str] | None = None,
) -> JudgeAdjudication:
    """Adjudicate ONE judge decision → a :class:`JudgeAdjudication`.

    Reconstructs the judge's context (stamping fidelity) — or reuses a
    ``context`` the caller already built (``(frozen_ctx, fidelity_tier)``, the
    seam :func:`adjudicate_corpus` uses so the cache check and the miss path
    share one reconstruction) — builds the DE-ANCHORED strict-JSON prompt, and
    calls the adjudicator ``k_adj`` times (each with its own single retry). The
    reported verdict follows the majority ``should_fire``; when every replicate
    is malformed the verdict is ``ambiguous`` with the raw response retained.
    ``adjudicator_self_agreement`` (``1 − pairwise disagreement`` over the
    replicates' ``should_fire`` outcomes) is reported only when at least two
    replicates parsed; a single-shot adjudication reports ``None``. The
    requested ``k_adj`` is stamped on the verdict so the cache can detect a
    replication-count change.
    """
    if context is None:
        context = observation_to_judge_context(obs, judge_name)
    ctx, fidelity = context
    transcript_text = _context_text(ctx)
    observed = OBSERVED_FIRED if decision.get("fired") else OBSERVED_SILENT
    claimed_severity = decision.get("severity")
    claim = str(decision.get("claim") or "")
    stamped_k_adj = int(k_adj)
    user = _build_user_prompt(
        judge_name=judge_name,
        run_ref=run_ref,
        claim=claim,
        transcript_text=transcript_text,
    )

    parsed_list: list[dict[str, Any]] = []
    last_raw = ""
    for _ in range(max(1, int(k_adj))):
        parsed, last_raw = await _adjudicate_once(
            adjudicator_call_llm, ADJUDICATOR_SYSTEM_PROMPT, user, adjudicator_model
        )
        if parsed is not None:
            parsed_list.append(parsed)

    if not parsed_list:
        # Every replicate was malformed after its retry — ambiguous, never raise.
        return JudgeAdjudication(
            judge_name=judge_name,
            run_ref=run_ref,
            observed=observed,
            adjudicated=ADJUDICATED_AMBIGUOUS,
            verdict=VERDICT_AMBIGUOUS,
            severity_match=None,
            evidence_span="",
            meta_judge_rationale="adjudicator response could not be parsed as strict JSON",
            meta_judge_model=adjudicator_model,
            adjudicator_self_agreement=None,
            operator_confirmed=None,
            fidelity=fidelity,
            prompt_version=ADJUDICATOR_PROMPT_VERSION,
            k_adj=stamped_k_adj,
            raw_response=last_raw,
        )

    should_fires = [bool(p.get("should_fire")) for p in parsed_list]
    fired_count = sum(should_fires)
    majority = fired_count * 2 >= len(should_fires)  # ties → should_fire
    representative = next(
        (p for p in parsed_list if bool(p.get("should_fire")) == majority), parsed_list[0]
    )
    adjudicated = ADJUDICATED_SHOULD_FIRE if majority else ADJUDICATED_SHOULD_BE_SILENT
    verdict = _classify(observed, adjudicated)

    severity_match: bool | None = None
    if verdict == VERDICT_TP:
        adj_sev = str(representative.get("severity", "")).strip().lower()
        claim_sev = str(claimed_severity or "").strip().lower()
        if adj_sev and claim_sev:
            severity_match = adj_sev == claim_sev

    self_agreement: float | None = None
    if len(should_fires) >= 2:
        self_agreement = 1.0 - pairwise_disagreement(fired_count, len(should_fires))

    return JudgeAdjudication(
        judge_name=judge_name,
        run_ref=run_ref,
        observed=observed,
        adjudicated=adjudicated,
        verdict=verdict,
        severity_match=severity_match,
        evidence_span=str(representative.get("evidence_span", "")),
        meta_judge_rationale=str(representative.get("rationale", "")),
        meta_judge_model=adjudicator_model,
        adjudicator_self_agreement=self_agreement,
        operator_confirmed=None,
        fidelity=fidelity,
        prompt_version=ADJUDICATOR_PROMPT_VERSION,
        k_adj=stamped_k_adj,
        raw_response=None,
    )


# ---------------------------------------------------------------------------
# Cache persistence
# ---------------------------------------------------------------------------


def write_adjudication(path: Path, adjudication: JudgeAdjudication) -> Path:
    """Persist one verdict via the fsync'd atomic JSON writer; return the path.

    Routes through :func:`zicato.storage.atomic_write_json` — the SAME durable
    ``.tmp`` + ``fsync`` (file AND parent dir) + rename discipline the
    ``result.json`` capture writer uses — so a cached verdict, like a captured run
    result, survives power loss rather than resting on a bare rename.
    """
    from zicato.storage import atomic_write_json  # noqa: PLC0415

    atomic_write_json(path, adjudication.to_json())
    return path


def read_adjudication(path: Path) -> JudgeAdjudication | None:
    """Read one persisted verdict; ``None`` on ANY defect (a re-run re-adjudicates).

    A missing / unreadable file, non-JSON / non-object content, or a
    ``format_version`` other than :data:`ADJUDICATION_FORMAT_VERSION` all
    return ``None`` — a cache MISS the caller re-adjudicates, never a crash.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        body = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict) or body.get("format_version") != ADJUDICATION_FORMAT_VERSION:
        return None
    return JudgeAdjudication.from_json(body)


# ---------------------------------------------------------------------------
# Corpus orchestration — idempotent, independence-guarded
# ---------------------------------------------------------------------------


async def adjudicate_corpus(
    *,
    corpus: list[ObservationRun],
    config: Any,
    epoch_id: str,
    reflection_id: str,
    adjudicator_model: str,
    workspace_root: Path,
    judge_models: tuple[str, ...] | list[str] = (),
    k_adj: int = 1,
    persist: bool = True,
) -> list[JudgeAdjudication]:
    """Adjudicate every judge decision in the corpus, idempotently.

    Enforces independence FIRST — HARD
    :func:`assert_distinct_callables` on the adjudicator vs judge callables
    (re-raised with an adjudication-specific actionable message), then the SOFT
    model-string warning — before any adjudication runs.

    Then, for every ``(judge_name, run_ref)`` decision the judge context (and
    its fidelity tier) is reconstructed ONCE, before the cache check, and reused
    on the miss path. A cached verdict is a HIT only when it is STILL VALID for
    the current request — same adjudicator model, same
    :data:`ADJUDICATOR_PROMPT_VERSION`, same requested ``k_adj``, AND the same
    fidelity tier currently available on disk. A model swap, a prompt-version
    bump, a ``k_adj`` change, or a fidelity upgrade (a ``preview`` verdict now
    that the ``verbatim`` sidecar exists) all MISS and re-adjudicate, overwriting
    the stale file. Because the corpus is frozen per ``reflection_id``, a second
    pass with the SAME parameters makes NO adjudicator calls at all (context
    reconstruction is disk-only, never an LLM call).
    """
    from zicato.core.workspace import (  # noqa: PLC0415
        assert_distinct_callables,
        reflection_adjudication_path,
    )

    adjudicator_call = config.effective_adjudicator_call_llm()
    judge_call = config.effective_judge_call_llm()
    try:
        assert_distinct_callables(adjudicator_call, judge_call)  # HARD — refuses on identity
    except RuntimeError as exc:
        raise RuntimeError(
            "the adjudicator and judge callables are identical — configure a distinct "
            "adjudicator (a models block, or --adjudicator-call-llm on the CLI); an "
            "adjudicator must not share the judges' endpoint, or the audit is not "
            "independent (a judge cannot grade its own homework)"
        ) from exc
    warn_on_adjudicator_collusion(adjudicator_model, judge_models)  # SOFT — warns, proceeds

    results: list[JudgeAdjudication] = []
    for obs in corpus:
        run_ref = run_ref_for(obs)
        for decision in obs.judge_decisions:
            judge_name = str(decision.get("judge_name", ""))
            if not judge_name:
                continue
            # A judge whose call RAISED made no decision, so there is nothing
            # to adjudicate (issue #121). Sending it anyway asks the
            # meta-judge "should this judge have fired?" about a judge that
            # never saw the transcript: it answers yes, and the FALSE
            # NEGATIVE that lands on the scorecard blames the criterion for a
            # broken endpoint. Skipping also spends no adjudicator budget on
            # a verdict that does not exist.
            if not judge_answered(decision):
                continue
            # Reconstruct the context (and current fidelity tier) ONCE — the
            # cache-validity check needs the tier, and the miss path reuses it.
            ctx_tier = observation_to_judge_context(obs, judge_name)
            tier = ctx_tier[1]
            path = reflection_adjudication_path(
                workspace_root, epoch_id, reflection_id, judge_name, run_ref
            )
            cached = read_adjudication(path) if path.exists() else None
            if (
                cached is not None
                and cached.meta_judge_model == adjudicator_model
                and cached.prompt_version == ADJUDICATOR_PROMPT_VERSION
                and cached.k_adj == k_adj
                and cached.fidelity == tier
            ):
                results.append(cached)
                continue
            adjudication = await adjudicate_decision(
                obs=obs,
                judge_name=judge_name,
                decision=dict(decision),
                run_ref=run_ref,
                adjudicator_call_llm=adjudicator_call,
                adjudicator_model=adjudicator_model,
                k_adj=k_adj,
                context=ctx_tier,
            )
            if persist:
                write_adjudication(path, adjudication)
            results.append(adjudication)
    return results


__all__ = [
    "ADJUDICATED_AMBIGUOUS",
    "ADJUDICATED_SHOULD_BE_SILENT",
    "ADJUDICATED_SHOULD_FIRE",
    "ADJUDICATION_FORMAT_VERSION",
    "ADJUDICATOR_PROMPT_VERSION",
    "ADJUDICATOR_SYSTEM_PROMPT",
    "OBSERVED_FIRED",
    "OBSERVED_SILENT",
    "SEVERITY_VOCAB",
    "TRANSCRIPT_CLOSE",
    "TRANSCRIPT_OPEN",
    "VERDICT_JSON_KEYS",
    "VERDICT_AMBIGUOUS",
    "VERDICT_FN",
    "VERDICT_FP",
    "VERDICT_TN",
    "VERDICT_TP",
    "JudgeAdjudication",
    "adjudicate_corpus",
    "adjudicate_decision",
    "extract_verdict_json",
    "observation_to_judge_context",
    "read_adjudication",
    "run_ref_for",
    "warn_on_adjudicator_collusion",
    "write_adjudication",
]
