"""WS-SYNTH — turn mined episodes into drafted eval-synthesis suggestions.

The second loop's authoring step (EVAL-SYNTHESIS.md §3). :mod:`~zicato.reflection.mining`
extracts ranked :class:`~zicato.reflection.mining.MinedEpisode` demand signals;
this module turns each one into a **suggestion** — a drafted BOARD-FORMAT entry
or :class:`~zicato.core.JudgeSpec`, a §4 provenance block (source episodes,
lineage ids, miner/synth versions, the per-type target-set rule), a
deterministic ``suggestion_id``, and a one-paragraph rationale. Nothing here
edits a sealed contract: every path terminates at a draft the operator carries
into the builder.

Two tiers, cleanly partitioned by the miner's ``suggestion_hint`` (EVAL-SYNTHESIS.md §7):

* **Mechanical (LLM-free, pure)** — regression entries pinned from failure
  episodes, harder variants of dead entries (a small typed perturbation
  vocabulary, no RNG, keyed off the episode id), and rubric revisions of
  false-firing judges (a structured tightening derived from the FP evidence).
  Zero LLM budget, the always-on passive tier.
* **LLM-drafted (aux seam, endpoint-gated)** — coverage entries exercising a
  blind mutation point / unmeasured metric, and new process judges drafted from
  disagreement / failure clusters. The call goes through the auxiliary callable
  (:data:`~zicato.core.runtime.CallLLM`), NEVER the harness callable; the
  response is tolerant-parsed (the proposer's
  :func:`~zicato.proposer.structured.extract_json_object` idiom) and validated
  against BOARD-FORMAT before it becomes a suggestion. A
  parse / validation failure drops the suggestion with a logged reason — never
  a crash, never a live call in tests (scripted callables only).

Every drafted ENTRY round-trips through the REAL board loader
(:func:`~zicato.board.jsonl.load_board` on a temp file) and every drafted JUDGE
through the real judge loader before it is ever surfaced (EVAL-SYNTHESIS.md §3
draft-artifact validity); a draft the loader would reject never ships. Output
is deterministic and order-independent for a fixed episode set + scripted
callable (the eval-view fixture discipline).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from goldfive import DriftSeverity

from zicato.core.board import BoardEntry, JudgeMode, JudgeSpec, validate_board_entry
from zicato.core.runtime import CallLLM
from zicato.reflection.mining import (
    HINT_COVERAGE_ENTRY,
    HINT_HARDER_VARIANT,
    HINT_JUDGE,
    HINT_REGRESSION_ENTRY,
    HINT_RUBRIC_REVISION,
    MINER_VERSION,
    MinedEpisode,
)

_LOG = logging.getLogger(__name__)

#: Stamped alongside the miner version into every suggestion's provenance so a
#: drafted artifact traces back to the synthesiser that produced it (§4).
SYNTH_VERSION: str = "eval-synth/1"

# --- suggestion types (EVAL-SYNTHESIS.md §3) -------------------------------
SUGGESTION_REGRESSION_ENTRY: str = "regression_entry"
SUGGESTION_COVERAGE_ENTRY: str = "coverage_entry"
SUGGESTION_JUDGE: str = "judge_suggestion"
SUGGESTION_RUBRIC_REVISION: str = "rubric_revision"
SUGGESTION_HARDER_VARIANT: str = "harder_variant"

# --- synthesiser tier ------------------------------------------------------
SYNTH_MECHANICAL: str = "mechanical"
SYNTH_LLM: str = "llm"

# --- target slices (EVAL-SYNTHESIS.md §4 rotation rule) --------------------
#: Default — the slice the motivating proposer has NOT seen (the collusion
#: guard). Coverage / judge / harder-variant suggestions all default here.
TARGET_INCOMING_ROTATION: str = "incoming_rotation"
#: Regression entries MAY target train — a proposer optimising to keep passing a
#: pinned past failure is doing exactly what the operator wants (§4 exception).
TARGET_TRAIN: str = "train"
#: A rubric revision edits an existing judge in place — no new board slice. Each
#: synthesizer stamps its own slice per this rule (§3 table): regression → train,
#: coverage / judge / harder variant → incoming rotation, rubric → existing judge.
TARGET_EXISTING_JUDGE: str = "existing_judge"

# --- the perturbation vocabulary (harder variants, §3 / task 1b) -----------
#: A dead entry is hardened by ONE deterministically-chosen perturbation from
#: this typed vocabulary — no RNG, the choice keyed off the episode id so a
#: re-run picks the same one. Ordered: the choice indexes into the applicable,
#: effective subset for the entry's kind.
PERTURB_TIGHTEN_BUDGET: str = "tighten_budget"
PERTURB_SUBSTITUTE_NUMERAL: str = "substitute_numeral"
PERTURB_APPEND_EDGE: str = "append_edge_constraint"
_PERTURBATION_ORDER: tuple[str, ...] = (
    PERTURB_SUBSTITUTE_NUMERAL,
    PERTURB_APPEND_EDGE,
    PERTURB_TIGHTEN_BUDGET,
)

#: The fixed adversarial edge-case clause appended by ``append_edge_constraint``.
_EDGE_CLAUSE: str = (
    " Additionally, handle the degenerate edge case (empty, malformed, or "
    "boundary input) correctly and without abandoning the stated goal."
)

#: Default wall-clock budget for a mechanically-scaffolded coverage entry.
_DEFAULT_BUDGET_SECONDS: int = 120

#: The board sentinel a board-wide staleness episode uses as its subject — it is
#: demand for rotation, not a single entry to perturb, so it seeds no artifact.
_BOARD_SENTINEL: str = "__board__"

_UNSAFE_ID_RE = re.compile(r"[^a-z0-9_-]+")
_NUMERAL_RE = re.compile(r"\d+")
#: The provenance key stamped into a drafted entry's opaque ``context`` so a
#: surfaced entry carries its own lineage through the board loader.
_PROVENANCE_CONTEXT_KEY: str = "synthesis_provenance"


# ---------------------------------------------------------------------------
# The suggestion artifact (EVAL-SYNTHESIS.md §3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One drafted, provenance-stamped eval-synthesis suggestion (§3).

    Exactly one draft artifact is set: ``entry`` for the entry-shaped types
    (regression / coverage / harder variant), ``judge`` for the judge-shaped
    types (judge suggestion / rubric revision). ``target_entry_id`` names the
    board entry a judge artifact attaches to (or whose judge a rubric revision
    edits) so the apply seam can build the ``add_judge`` op; it is ``None`` when
    the motivating episode did not resolve one.
    """

    suggestion_id: str
    suggestion_type: str
    synthesizer: str
    subject: str
    target_slice: str
    rationale: str
    provenance: dict[str, Any]
    entry: BoardEntry | None = None
    judge: JudgeSpec | None = None
    target_entry_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        from zicato.board.jsonl import entry_to_dict  # noqa: PLC0415

        return {
            "suggestion_id": self.suggestion_id,
            "suggestion_type": self.suggestion_type,
            "synthesizer": self.synthesizer,
            "subject": self.subject,
            "target_slice": self.target_slice,
            "rationale": self.rationale,
            "provenance": dict(self.provenance),
            "entry": entry_to_dict(self.entry) if self.entry is not None else None,
            "judge": _judge_to_json(self.judge) if self.judge is not None else None,
            "target_entry_id": self.target_entry_id,
            "evidence": dict(self.evidence),
        }


def _judge_to_json(judge: JudgeSpec) -> dict[str, Any]:
    return {
        "name": judge.name,
        "mode": judge.mode.value if hasattr(judge.mode, "value") else judge.mode,
        "body": judge.body,
        "severity": judge.severity.value if hasattr(judge.severity, "value") else judge.severity,
    }


# ---------------------------------------------------------------------------
# provenance + ids (EVAL-SYNTHESIS.md §4)
# ---------------------------------------------------------------------------


def _provenance(
    *, suggestion_type: str, target_slice: str, episodes: Sequence[MinedEpisode]
) -> dict[str, Any]:
    """The §4 provenance block folded from an episode's source refs / lineage."""
    source_episodes = sorted({e.episode_id for e in episodes})
    refs: list[str] = []
    for e in episodes:
        for r in e.source_refs:
            if r not in refs:
                refs.append(r)
    lineage: list[str] = []
    for e in episodes:
        for g in e.source_lineage_ids:
            if g and g not in lineage:
                lineage.append(g)
    miner_version = episodes[0].miner_version if episodes else MINER_VERSION
    return {
        "miner_version": miner_version,
        "synth_version": SYNTH_VERSION,
        "source_episodes": source_episodes,
        "source_refs": refs,
        "source_lineage_ids": lineage,
        "suggestion_type": suggestion_type,
        "target_slice": target_slice,
    }


def _suggestion_id(
    suggestion_type: str, synthesizer: str, subject: str, provenance: dict[str, Any]
) -> str:
    """Content-stable ``sug-{8hex}`` — independent of output order (§3 determinism).

    Keyed on the source episodes (not the drafted bytes) so the same episode set
    resolves the same id across re-derivations, and the synthesiser tier is in
    the key so a mechanical and an LLM draft for one episode never collide.
    """
    payload = "|".join(
        [suggestion_type, synthesizer, subject, *provenance.get("source_episodes", [])]
    )
    return "sug-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _slug(text: str) -> str:
    """A filesystem-safe entry-id fragment (BoardEntry.id is a runs/ dir name)."""
    slug = _UNSAFE_ID_RE.sub("_", (text or "").lower()).strip("_")
    return slug or "x"


# ---------------------------------------------------------------------------
# draft-artifact validity — the real loaders (EVAL-SYNTHESIS.md §3)
# ---------------------------------------------------------------------------


def _entry_reject_reason(entry: BoardEntry) -> str | None:
    """``None`` if ``entry`` round-trips through the REAL board loader, else why.

    Validates the in-memory entry, then serialises + reloads it via
    :func:`~zicato.board.jsonl.save_board` / :func:`~zicato.board.jsonl.load_board`
    on a temp file — a suggestion the loader would reject never surfaces.
    """
    from zicato.board.jsonl import load_board, save_board  # noqa: PLC0415

    try:
        entry.validate()
    except ValueError as exc:
        return f"entry failed validate(): {exc}"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "board.jsonl"
            save_board([entry], path)
            loaded = load_board(path)
    except (ValueError, OSError) as exc:
        return f"entry did not round-trip through load_board: {exc}"
    if len(loaded) != 1 or loaded[0].id != entry.id:
        return "entry round-trip changed the row set"
    return None


def _judge_reject_reason(judge: JudgeSpec) -> str | None:
    """``None`` if ``judge`` passes the real judge loader (embedded in an entry).

    Reconstructs the spec through the :class:`~zicato.board.judges.Judge`
    authoring validator (name slug / non-empty body / typed severity) and
    round-trips a synthetic host entry carrying it through ``load_board`` — the
    same path :func:`~zicato.core.validate_board_entry` parses a judge on.
    """
    from zicato.board.judges import Judge  # noqa: PLC0415

    try:
        if judge.mode == JudgeMode.INLINE:
            Judge.custom(judge.name, judge.body, severity=judge.severity)
        else:
            Judge.python(judge.name, judge.body, severity=judge.severity)
    except (ValueError, TypeError) as exc:
        return f"judge failed authoring validation: {exc}"
    host = BoardEntry(
        id="__judge_probe__",
        kind="single_turn",
        wall_clock_budget_seconds=_DEFAULT_BUDGET_SECONDS,
        input="probe",
        judges=(judge,),
    )
    reason = _entry_reject_reason(host)
    return None if reason is None else f"judge did not round-trip: {reason}"


def _stamp_provenance(entry: BoardEntry, provenance: dict[str, Any]) -> BoardEntry:
    """Return ``entry`` with the provenance block stamped into opaque context.

    Context is ``Mapping[str, str]``, so the block rides as a compact JSON
    string (sorted keys → deterministic bytes) that round-trips cleanly.
    """
    context = dict(entry.context)
    context[_PROVENANCE_CONTEXT_KEY] = json.dumps(provenance, sort_keys=True)
    return dataclasses.replace(entry, context=context)


# ---------------------------------------------------------------------------
# (a) regression entries — mechanical, from failure episodes (§3, task 1a)
# ---------------------------------------------------------------------------


def _regression_suggestion(
    episode: MinedEpisode, board_by_id: dict[str, BoardEntry]
) -> Suggestion | None:
    """Pin a failure episode as a regression entry (§3 regression entry).

    The entry that failed already encodes the CORRECT behaviour — its input and
    its expectation ARE the pin — so the regression reproduces that recorded
    scenario verbatim under a new id, not an invented one. Targets train (a
    regression working as intended when the proposer sees it, §4).
    """
    orig = board_by_id.get(episode.subject)
    if orig is None:
        _LOG.debug(
            "regression: no board entry %r to pin (episode %s)", episode.subject, episode.episode_id
        )
        return None

    provenance = _provenance(
        suggestion_type=SUGGESTION_REGRESSION_ENTRY,
        target_slice=TARGET_TRAIN,
        episodes=[episode],
    )
    # The failure class is in the id so an entry that failed two ways (a predicate
    # miss AND an abort) yields two distinctly-named regressions, never a pair
    # that collides on one board id.
    failure_class = str(episode.evidence.get("failure_class", "failure"))
    new_id = _unique_id(f"{orig.id}__regression_{_slug(failure_class)}", board_by_id)
    draft = dataclasses.replace(
        orig,
        id=new_id,
        tags=_dedup_tags(orig.tags, ("regression", "synthesized")),
    )
    draft = _stamp_provenance(draft, provenance)
    reason = _entry_reject_reason(draft)
    if reason is not None:
        _LOG.info("regression: dropped %r — %s", new_id, reason)
        return None

    rationale = (
        f"Entry {orig.id!r} recorded a {failure_class.replace('_', ' ')} across "
        f"{episode.coverage_key} candidate(s). This suggestion pins that exact scenario as a "
        f"regression entry so the failure cannot quietly return; its expectation is the "
        f"original entry's, unchanged, so a passing candidate is one that has genuinely fixed "
        f"the behaviour. It targets the train slice, where a pinned past failure is a "
        f"regression working as intended."
    )
    return Suggestion(
        suggestion_id=_suggestion_id(
            SUGGESTION_REGRESSION_ENTRY, SYNTH_MECHANICAL, new_id, provenance
        ),
        suggestion_type=SUGGESTION_REGRESSION_ENTRY,
        synthesizer=SYNTH_MECHANICAL,
        subject=orig.id,
        target_slice=TARGET_TRAIN,
        rationale=rationale,
        provenance=provenance,
        entry=draft,
        evidence={"failure_class": failure_class, "source_summary": episode.summary},
    )


# ---------------------------------------------------------------------------
# (b) harder variants — mechanical perturbation of a dead entry (§3, task 1b)
# ---------------------------------------------------------------------------


def _harder_variant_suggestion(
    episode: MinedEpisode, board_by_id: dict[str, BoardEntry]
) -> Suggestion | None:
    """Harden a dead (saturated) entry by one deterministic perturbation (§3).

    A board-wide gap episode (subject ``__board__``) is rotation demand, not a
    single entry to perturb, so it seeds no artifact here. A dead-entry episode
    picks ONE perturbation from the typed vocabulary — no RNG, the choice keyed
    off the episode id — and lands the variant in the incoming rotation set (§4).
    """
    if episode.subject == _BOARD_SENTINEL:
        return None
    orig = board_by_id.get(episode.subject)
    if orig is None:
        _LOG.debug("harder-variant: no board entry %r to perturb", episode.subject)
        return None

    chosen = _choose_perturbation(orig, episode.episode_id)
    if chosen is None:
        _LOG.info("harder-variant: no effective perturbation for %r (kind %s)", orig.id, orig.kind)
        return None
    perturbed = _apply_perturbation(orig, chosen)
    if perturbed is None:
        _LOG.info("harder-variant: perturbation %s produced no change for %r", chosen, orig.id)
        return None

    provenance = _provenance(
        suggestion_type=SUGGESTION_HARDER_VARIANT,
        target_slice=TARGET_INCOMING_ROTATION,
        episodes=[episode],
    )
    new_id = _unique_id(f"{orig.id}__hv_{chosen}", board_by_id)
    draft = dataclasses.replace(
        perturbed, id=new_id, tags=_dedup_tags(orig.tags, ("harder_variant", "synthesized"))
    )
    draft = _stamp_provenance(draft, provenance)
    reason = _entry_reject_reason(draft)
    if reason is not None:
        _LOG.info("harder-variant: dropped %r — %s", new_id, reason)
        return None

    rationale = (
        f"Entry {orig.id!r} has gone dead — it no longer separates any recent candidate pair, so "
        f"the channel is saturated. This variant applies a {chosen.replace('_', ' ')} perturbation "
        f"to restore discrimination without inventing a new scenario, and enters the incoming "
        f"rotation set so the proposer that saturated the original meets it blind."
    )
    return Suggestion(
        suggestion_id=_suggestion_id(
            SUGGESTION_HARDER_VARIANT, SYNTH_MECHANICAL, new_id, provenance
        ),
        suggestion_type=SUGGESTION_HARDER_VARIANT,
        synthesizer=SYNTH_MECHANICAL,
        subject=orig.id,
        target_slice=TARGET_INCOMING_ROTATION,
        rationale=rationale,
        provenance=provenance,
        entry=draft,
        evidence={"perturbation": chosen, "source_summary": episode.summary},
    )


def _entry_text_fields(entry: BoardEntry) -> str:
    """The perturbable free text of an entry (input, or joined scripted turns)."""
    if entry.input:
        return entry.input
    if entry.turns:
        return " ".join(t.user for t in entry.turns)
    if entry.user_persona:
        return entry.user_persona.constraints
    return ""


def _perturbation_effective(entry: BoardEntry, kind: str) -> bool:
    """Whether ``kind`` would actually change ``entry`` (no cosmetic variants)."""
    if kind == PERTURB_TIGHTEN_BUDGET:
        return entry.wall_clock_budget_seconds > 1
    if kind == PERTURB_SUBSTITUTE_NUMERAL:
        return bool(_NUMERAL_RE.search(_entry_text_fields(entry)))
    if kind == PERTURB_APPEND_EDGE:
        # Appending a clause is only meaningful where there is a text surface.
        return bool(entry.input) or bool(entry.turns) or bool(entry.user_persona)
    return False


def _choose_perturbation(entry: BoardEntry, episode_id: str) -> str | None:
    """Deterministically pick one effective perturbation, keyed off the episode id."""
    applicable = [k for k in _PERTURBATION_ORDER if _perturbation_effective(entry, k)]
    if not applicable:
        return None
    index = int(hashlib.sha256(episode_id.encode("utf-8")).hexdigest(), 16) % len(applicable)
    return applicable[index]


def _substitute_numerals(text: str) -> str:
    """Deterministic parameter substitution: ``n`` → ``2n + 1`` (a harder value)."""
    return _NUMERAL_RE.sub(lambda m: str(int(m.group(0)) * 2 + 1), text)


def _apply_perturbation(entry: BoardEntry, kind: str) -> BoardEntry | None:
    """Apply one typed perturbation, returning the hardened entry (or ``None``)."""
    if kind == PERTURB_TIGHTEN_BUDGET:
        tightened = max(1, entry.wall_clock_budget_seconds // 2)
        if tightened == entry.wall_clock_budget_seconds:
            return None
        return dataclasses.replace(entry, wall_clock_budget_seconds=tightened)

    if kind == PERTURB_SUBSTITUTE_NUMERAL:
        if entry.input:
            new_input = _substitute_numerals(entry.input)
            return None if new_input == entry.input else dataclasses.replace(entry, input=new_input)
        if entry.turns:
            new_turns = tuple(
                dataclasses.replace(t, user=_substitute_numerals(t.user)) for t in entry.turns
            )
            if all(n.user == o.user for n, o in zip(new_turns, entry.turns, strict=True)):
                return None
            return dataclasses.replace(entry, turns=new_turns)
        return None

    if kind == PERTURB_APPEND_EDGE:
        if entry.input is not None:
            return dataclasses.replace(entry, input=entry.input + _EDGE_CLAUSE)
        if entry.turns:
            last = entry.turns[-1]
            new_turns = entry.turns[:-1] + (
                dataclasses.replace(last, user=last.user + _EDGE_CLAUSE),
            )
            return dataclasses.replace(entry, turns=new_turns)
        if entry.user_persona:
            persona = dataclasses.replace(
                entry.user_persona, constraints=entry.user_persona.constraints + _EDGE_CLAUSE
            )
            return dataclasses.replace(entry, user_persona=persona)
        return None

    return None


# ---------------------------------------------------------------------------
# (c) rubric revisions — mechanical structured diff of a judge (§3, task 1c)
# ---------------------------------------------------------------------------


def _rubric_revision_suggestion(
    episode: MinedEpisode, judge_index: dict[str, list[tuple[str, JudgeSpec]]]
) -> Suggestion | None:
    """Revise a false-firing judge's criterion (§3 rubric revision).

    A structured tightening derived mechanically from the FP evidence: the
    original inline body plus an auto-derived clause instructing the judge NOT
    to fire on the transcripts the meta-judge confirmed clean. Only inline
    judges are revisable — a python-mode body is a dotted path, not a criterion.
    """
    hosts = judge_index.get(episode.subject)
    if not hosts:
        _LOG.debug("rubric-revision: judge %r not on the board", episode.subject)
        return None
    entry_id, judge = sorted(hosts, key=lambda pair: pair[0])[0]
    if judge.mode != JudgeMode.INLINE:
        _LOG.info("rubric-revision: judge %r is python-mode; not a revisable criterion", judge.name)
        return None

    count = int(episode.evidence.get("count", 0) or 0)
    spans = [str(s) for s in episode.evidence.get("spans", []) if s][:6]
    clause_lines = "\n".join(f"- {s}" for s in spans)
    tightening = (
        f"\n\nRevision (auto-derived from {count} adjudicated false fire(s)): do NOT fire when "
        f"the transcript matches the pattern of these confirmed-clean excerpts — the criterion "
        f"above fired on them in error:\n{clause_lines}"
        if spans
        else (
            f"\n\nRevision (auto-derived from {count} adjudicated false fire(s)): tighten the "
            f"criterion above so it does not fire on transcripts an independent adjudicator "
            f"reads as clean."
        )
    )
    revised = dataclasses.replace(judge, body=judge.body + tightening)
    reason = _judge_reject_reason(revised)
    if reason is not None:
        _LOG.info("rubric-revision: dropped judge %r — %s", judge.name, reason)
        return None

    provenance = _provenance(
        suggestion_type=SUGGESTION_RUBRIC_REVISION,
        target_slice=TARGET_EXISTING_JUDGE,
        episodes=[episode],
    )
    rationale = (
        f"Judge {judge.name!r} fired on {count} transcript(s) an independent meta-judge read as "
        f"clean — its criterion is too loose. This revision keeps the original body and appends a "
        f"tightening clause derived from the confirmed false fires, narrowing the judge without "
        f"rewriting its intent. It edits the existing judge in place; no new board slice is added."
    )
    return Suggestion(
        suggestion_id=_suggestion_id(
            SUGGESTION_RUBRIC_REVISION, SYNTH_MECHANICAL, judge.name, provenance
        ),
        suggestion_type=SUGGESTION_RUBRIC_REVISION,
        synthesizer=SYNTH_MECHANICAL,
        subject=judge.name,
        target_slice=TARGET_EXISTING_JUDGE,
        rationale=rationale,
        provenance=provenance,
        judge=revised,
        target_entry_id=entry_id,
        evidence={
            "false_fires": count,
            "body_before": judge.body,
            "body_after": revised.body,
            "spans": spans,
        },
    )


# ---------------------------------------------------------------------------
# LLM-drafted synthesizers — the aux seam (§3 / §7, task 2)
# ---------------------------------------------------------------------------

_COVERAGE_SYSTEM_PROMPT: str = (
    "You are drafting one evaluation-board entry for a coverage gap in an agent "
    "test suite. Respond with a SINGLE JSON object and nothing else. Schema: "
    '{"id": <slug>, "kind": "single_turn", "wall_clock_budget_seconds": <int>, '
    '"input": <user message that exercises the named surface>, '
    '"expectation": {"kind": "regex"|"expected_text", "spec": <string>}, '
    '"tags": [<string>, ...]}. The expectation must pin an observable, '
    "unambiguous property of a correct response. Do not include commentary."
)

_JUDGE_SYSTEM_PROMPT: str = (
    "You are drafting one process judge for an agent test suite — a natural-"
    "language criterion evaluated while a run is in flight. Respond with a "
    'SINGLE JSON object and nothing else. Schema: {"name": <lowercase slug: '
    'letters, digits, underscores, hyphens>, "body": <the criterion prose>, '
    '"severity": "info"|"warning"|"critical"}. The criterion must be a single, '
    "checkable property. Do not include commentary."
)


async def _coverage_entry_suggestion(
    episode: MinedEpisode, aux_call_llm: CallLLM
) -> Suggestion | None:
    """Draft a coverage entry via the aux callable (§3 coverage entry, LLM tier).

    Prompts the auxiliary callable (never the harness callable) for a
    schema-shaped board entry exercising the blind mutation point / unmeasured
    metric, tolerant-parses the response, and validates it against BOARD-FORMAT.
    Any parse / validation failure drops the suggestion with a logged reason.
    """
    subject = episode.subject
    user = (
        f"The surface {subject!r} is a coverage gap: {episode.summary}. Draft one board entry "
        f"that exercises it so the instrument can measure whether changes to it help or hurt."
    )
    raw = await _aux_text(aux_call_llm, _COVERAGE_SYSTEM_PROMPT, user, "coverage", subject)
    if raw is None:
        return None
    parsed = _parse_json_object(raw, "coverage", subject)
    if parsed is None:
        return None

    provenance = _provenance(
        suggestion_type=SUGGESTION_COVERAGE_ENTRY,
        target_slice=TARGET_INCOMING_ROTATION,
        episodes=[episode],
    )
    # Force a deterministic, collision-free id + coverage tags regardless of what
    # the model proposed, then validate the whole entry through the real loader.
    parsed["id"] = f"coverage__{_slug(subject)}"
    parsed.setdefault("kind", "single_turn")
    parsed.setdefault("wall_clock_budget_seconds", _DEFAULT_BUDGET_SECONDS)
    parsed["tags"] = _list_dedup([*_as_str_list(parsed.get("tags")), "coverage", "synthesized"])
    try:
        draft = validate_board_entry(parsed)
    except (KeyError, ValueError, TypeError) as exc:
        _LOG.info("coverage: dropped draft for %r — did not validate: %s", subject, exc)
        return None
    draft = _stamp_provenance(draft, provenance)
    reason = _entry_reject_reason(draft)
    if reason is not None:
        _LOG.info("coverage: dropped draft for %r — %s", subject, reason)
        return None

    rationale = (
        f"The proposer keeps rewriting {subject!r} but the board discriminates nothing there, so "
        f"the instrument cannot tell whether those rewrites help. This drafted entry exercises "
        f"that surface and enters the incoming rotation set, giving the loop a measured channel "
        f"the motivating proposer meets blind."
    )
    return Suggestion(
        suggestion_id=_suggestion_id(SUGGESTION_COVERAGE_ENTRY, SYNTH_LLM, draft.id, provenance),
        suggestion_type=SUGGESTION_COVERAGE_ENTRY,
        synthesizer=SYNTH_LLM,
        subject=subject,
        target_slice=TARGET_INCOMING_ROTATION,
        rationale=rationale,
        provenance=provenance,
        entry=draft,
        evidence={"source_summary": episode.summary},
    )


async def _judge_suggestion(episode: MinedEpisode, aux_call_llm: CallLLM) -> Suggestion | None:
    """Draft a new process judge via the aux callable (§3 judge suggestion, LLM tier).

    Prompts for a ``{name, body, severity}`` criterion drafted from the
    disagreement / failure cluster, validates it against the judge loader, and
    resolves the target entry from the episode's run refs when it can. A
    malformed / invalid response drops the suggestion with a logged reason.
    """
    subject = episode.subject
    user = (
        f"A blind spot was observed on {subject!r}: {episode.summary}. Draft one process judge "
        f"whose criterion would catch this class of failure while a run is in flight."
    )
    raw = await _aux_text(aux_call_llm, _JUDGE_SYSTEM_PROMPT, user, "judge", subject)
    if raw is None:
        return None
    parsed = _parse_json_object(raw, "judge", subject)
    if parsed is None:
        return None

    judge = _build_judge(parsed, subject)
    if judge is None:
        return None
    reason = _judge_reject_reason(judge)
    if reason is not None:
        _LOG.info("judge: dropped draft for %r — %s", subject, reason)
        return None

    provenance = _provenance(
        suggestion_type=SUGGESTION_JUDGE,
        target_slice=TARGET_INCOMING_ROTATION,
        episodes=[episode],
    )
    target_entry_id = _entry_from_refs(episode.source_refs)
    rationale = (
        f"Observed behaviour on {subject!r} names a failure no judge currently catches. This "
        f"drafted process judge pins a single checkable criterion for that failure so the board "
        f"can measure it going forward. It enters the incoming rotation so the proposer meets the "
        f"new channel blind, and admission must adjudicate it with an independent model."
    )
    return Suggestion(
        suggestion_id=_suggestion_id(SUGGESTION_JUDGE, SYNTH_LLM, judge.name, provenance),
        suggestion_type=SUGGESTION_JUDGE,
        synthesizer=SYNTH_LLM,
        subject=subject,
        target_slice=TARGET_INCOMING_ROTATION,
        rationale=rationale,
        provenance=provenance,
        judge=judge,
        target_entry_id=target_entry_id,
        evidence={"source_summary": episode.summary},
    )


def _build_judge(parsed: dict[str, Any], subject: str) -> JudgeSpec | None:
    """Build a validated inline :class:`JudgeSpec` from a parsed aux response."""
    from zicato.board.judges import Judge  # noqa: PLC0415

    name = parsed.get("name")
    body = parsed.get("body")
    severity_token = parsed.get("severity", "warning")
    if not isinstance(name, str) or not isinstance(body, str):
        _LOG.info("judge: dropped draft for %r — response missing name/body", subject)
        return None
    try:
        severity = DriftSeverity(severity_token)
    except ValueError:
        _LOG.info("judge: dropped draft for %r — unknown severity %r", subject, severity_token)
        return None
    try:
        return Judge.custom(name, body, severity=severity)
    except (ValueError, TypeError) as exc:
        _LOG.info("judge: dropped draft for %r — %s", subject, exc)
        return None


async def _aux_text(
    aux_call_llm: CallLLM, system: str, user: str, tier: str, subject: str
) -> str | None:
    """Invoke the aux callable defensively; a failure logs and drops (no crash)."""
    try:
        return await aux_call_llm(system, user, "")
    except Exception as exc:  # noqa: BLE001 — the aux endpoint is untrusted; degrade
        _LOG.info("%s: aux call failed for %r — %s", tier, subject, exc)
        return None


def _parse_json_object(raw: str, tier: str, subject: str) -> dict[str, Any] | None:
    """Tolerant-parse an aux response to a JSON object (the proposer idiom)."""
    from zicato.proposer.structured import extract_json_object  # noqa: PLC0415

    salvaged = extract_json_object(raw)
    if salvaged is None:
        _LOG.info("%s: dropped draft for %r — no JSON object in response", tier, subject)
        return None
    try:
        obj = json.loads(salvaged)
    except json.JSONDecodeError as exc:
        _LOG.info("%s: dropped draft for %r — JSON decode failed: %s", tier, subject, exc)
        return None
    if not isinstance(obj, dict):
        _LOG.info("%s: dropped draft for %r — response was not a JSON object", tier, subject)
        return None
    return obj


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def synthesize_mechanical(
    episodes: Sequence[MinedEpisode], *, board_entries: Sequence[BoardEntry]
) -> list[Suggestion]:
    """The always-on, LLM-free tier: regression + harder variant + rubric revision.

    Pure over already-mined episodes and the current board. Deterministic and
    order-independent: the result is sorted by ``suggestion_id`` so a re-run
    over the same episode set is byte-stable.
    """
    board_by_id = {e.id: e for e in board_entries}
    judge_index = _judge_index(board_entries)
    out: list[Suggestion] = []
    for episode in episodes:
        suggestion = _route_mechanical(episode, board_by_id, judge_index)
        if suggestion is not None:
            out.append(suggestion)
    return _finalize(out)


async def synthesize_suggestions(
    episodes: Sequence[MinedEpisode],
    *,
    board_entries: Sequence[BoardEntry],
    aux_call_llm: CallLLM | None = None,
) -> list[Suggestion]:
    """Mine → suggestions, both tiers (§3). The one combined entry point.

    Always runs the mechanical tier. When ``aux_call_llm`` is supplied (the
    endpoint-gated / scripted-callable path, §7) it also drafts the LLM tier —
    coverage entries and new judges. Every drafted artifact is loader-validated;
    a parse / validation failure drops that one suggestion, never the batch. The
    result is deterministic and order-independent for a fixed episode set +
    scripted callable.
    """
    board_by_id = {e.id: e for e in board_entries}
    judge_index = _judge_index(board_entries)
    out: list[Suggestion] = []
    for episode in episodes:
        mechanical = _route_mechanical(episode, board_by_id, judge_index)
        if mechanical is not None:
            out.append(mechanical)
            continue
        if aux_call_llm is None:
            continue
        drafted = await _route_llm(episode, aux_call_llm)
        if drafted is not None:
            out.append(drafted)
    return _finalize(out)


# ---------------------------------------------------------------------------
# The surface bridge — the sync seam WS-SURFACE / the CLI call into (§6)
# ---------------------------------------------------------------------------


def synthesize(
    episodes: Sequence[MinedEpisode],
    *,
    allow_llm: bool = False,
    workspace_root: Path | None = None,
    epoch_id: str | None = None,
) -> list[Any]:
    """The sync WS-SURFACE seam: ranked episodes → persisted-shape suggestions (§6).

    This is the callable :func:`zicato.reflection.suggestions.resolve_synthesize`
    late-binds and the CLI drives. It bridges the two suggestion shapes: the
    internal :class:`Suggestion` this module authors (a typed ``entry`` / ``judge``
    draft) and the surface :class:`zicato.reflection.suggestions.Suggestion` the
    persistence / apply / inbox contract reads (a ``draft_artifact`` + a
    ``proposed_op``). The surface shape WINS — every path here terminates in it.

    Steps:

    1. Load the epoch board (the ``board_entries`` the async
       :func:`synthesize_suggestions` needs to pin regressions / perturb dead
       entries / host judges). A missing workspace / board degrades to an empty
       board — the mechanical tier that needs a board entry then simply finds
       none to draft, never a crash.
    2. Resolve the auxiliary callable ONLY when ``allow_llm`` (the LLM tier), the
       SAME way reflection's own aux resolution works (``models.auxiliary`` first,
       then the legacy ``runtime.auxiliary_call_llm`` dotted path). When no aux is
       configured the LLM tier is SKIPPED with a logged reason and the mechanical
       tier still runs — the ``--allow-llm`` help says the LLM tier needs the
       configured aux endpoint.
    3. Run the async :func:`synthesize_suggestions` via :func:`asyncio.run` and
       TRANSLATE each internal suggestion into the surface shape (§3): the typed
       draft's canonical JSON becomes ``draft_artifact``; the ``proposed_op`` is
       ``add_board_entry`` for entry drafts / ``add_judge`` for judge drafts /
       ``None`` (a recorded gap) for a rubric revision; the ranking keys ride from
       the motivating episode; the ``suggestion_id`` is kept verbatim.
    """
    from zicato.reflection import suggestions as surface  # noqa: PLC0415

    board_entries = _load_epoch_board(workspace_root, epoch_id)
    aux: CallLLM | None = None
    if allow_llm:
        aux = _resolve_aux_call_llm(workspace_root)
        if aux is None:
            _LOG.info(
                "synthesize: --allow-llm requested but no auxiliary model/callable is "
                "configured (models.auxiliary / runtime.auxiliary_call_llm); the LLM tier "
                "is skipped (mechanical tier only)."
            )

    import asyncio  # noqa: PLC0415

    internal = asyncio.run(
        synthesize_suggestions(episodes, board_entries=board_entries, aux_call_llm=aux)
    )
    episode_by_id = {e.episode_id: e for e in episodes}
    return [_to_surface_suggestion(s, episode_by_id, surface) for s in internal]


def _to_surface_suggestion(
    s: Suggestion, episode_by_id: dict[str, MinedEpisode], surface: Any
) -> Any:
    """Translate one internal :class:`Suggestion` into the surface shape (§3)."""
    proposed_op_reason: str | None = None
    if s.entry is not None:
        artifact_kind = surface.ARTIFACT_BOARD_ENTRY
        draft_artifact = _entry_op_dict(s.entry)
        proposed_op: dict[str, Any] | None = {
            "op": "add_board_entry",
            "args": {"entry": draft_artifact},
        }
    elif s.judge is not None and s.suggestion_type == SUGGESTION_RUBRIC_REVISION:
        artifact_kind = surface.ARTIFACT_RUBRIC_REVISION
        draft_artifact = _judge_to_json(s.judge)
        proposed_op = None
        proposed_op_reason = (
            "rubric revision edits an existing judge — no builder judge-edit op yet"
        )
    elif s.judge is not None:
        artifact_kind = surface.ARTIFACT_JUDGE
        draft_artifact = _judge_to_json(s.judge)
        if s.target_entry_id:
            proposed_op = {
                "op": "add_judge",
                "args": {"entry_id": s.target_entry_id, "judge": draft_artifact},
            }
        else:
            proposed_op = None
            proposed_op_reason = (
                "judge suggestion did not resolve a target board entry to attach to"
            )
    else:  # pragma: no cover — a suggestion always carries exactly one draft
        artifact_kind = ""
        draft_artifact = {}
        proposed_op = None

    episode = _motivating_episode(s.provenance, episode_by_id)
    provenance = dict(s.provenance)
    provenance.setdefault("synthesizer", s.synthesizer)
    if proposed_op_reason is not None:
        provenance["proposed_op_reason"] = proposed_op_reason

    return surface.Suggestion(
        suggestion_id=s.suggestion_id,
        suggestion_type=s.suggestion_type,
        artifact_kind=artifact_kind,
        subject=s.subject,
        summary=_first_sentence(s.rationale),
        rationale=s.rationale,
        target_slice=s.target_slice,
        draft_artifact=draft_artifact,
        proposed_op=proposed_op,
        provenance=provenance,
        admission=None,
        severity_rank=episode.severity_rank if episode else 0,
        recency_key=episode.recency_key if episode else 0,
        coverage_key=episode.coverage_key if episode else 0,
    )


def _entry_op_dict(entry: BoardEntry) -> dict[str, Any]:
    """The canonical entry JSON the ``add_board_entry`` op reconstructs.

    :func:`~zicato.board.jsonl.entry_to_dict` emits the short ``budget_s`` key,
    but the apply seam validates the op's entry with
    :func:`~zicato.core.board.validate_board_entry`, which reads the canonical
    ``wall_clock_budget_seconds``. Normalise the one key so the drafted entry
    round-trips through the op exactly like a hand-authored board edit.
    """
    from zicato.board.jsonl import entry_to_dict  # noqa: PLC0415

    d = entry_to_dict(entry)
    if "budget_s" in d and "wall_clock_budget_seconds" not in d:
        d["wall_clock_budget_seconds"] = d.pop("budget_s")
    return d


def _motivating_episode(
    provenance: dict[str, Any], episode_by_id: dict[str, MinedEpisode]
) -> MinedEpisode | None:
    """The first source episode present in the pool — the ranking-key source (§2)."""
    for eid in provenance.get("source_episodes", []):
        episode = episode_by_id.get(str(eid))
        if episode is not None:
            return episode
    return None


def _first_sentence(text: str) -> str:
    """A one-line summary: the rationale's first sentence (or the whole line)."""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    best = len(stripped)
    for sep in (". ", "! ", "? "):
        idx = stripped.find(sep)
        if idx != -1:
            best = min(best, idx + 1)
    return stripped[:best].strip()


def _load_epoch_board(workspace_root: Path | None, epoch_id: str | None) -> list[BoardEntry]:
    """Load the epoch's board entries (tolerant: absent workspace/board ⇒ ``[]``)."""
    if workspace_root is None or not epoch_id:
        return []
    from zicato.board.jsonl import load_board_with_meta  # noqa: PLC0415
    from zicato.core.workspace import board_path  # noqa: PLC0415

    try:
        board, _disable_drift, _judge_only = load_board_with_meta(
            board_path(workspace_root, epoch_id)
        )
    except (OSError, ValueError, KeyError):
        return []
    return list(board)


def _resolve_aux_call_llm(workspace_root: Path | None) -> CallLLM | None:
    """Resolve the auxiliary callable the LLM tier drafts through (or ``None``).

    Mirrors reflection's own aux resolution: the unified ``models.auxiliary``
    role first, then the legacy ``runtime.auxiliary_call_llm`` dotted path. Any
    resolution failure (no config, an unimportable path) degrades to ``None`` so
    the LLM tier is skipped with a logged reason — never a crash, never a live
    call the operator did not ask for.
    """
    if workspace_root is None:
        return None
    try:
        from zicato import workspace_loader  # noqa: PLC0415

        cfg = workspace_loader.load_workspace_config(workspace_root)
    except (OSError, ValueError, FileNotFoundError):
        return None

    try:
        from zicato.models_config import load_models_config, resolve_text_call_llm  # noqa: PLC0415

        models = load_models_config(cfg)
        if not models.auxiliary.is_empty:
            return resolve_text_call_llm(models.auxiliary, role="auxiliary")
    except Exception as exc:  # noqa: BLE001 — an unresolvable aux spec degrades, never crashes
        _LOG.info("synthesize: models.auxiliary did not resolve (%s); trying the legacy path", exc)

    runtime = cfg.get("runtime", {}) if isinstance(cfg, dict) else {}
    dotted = runtime.get("auxiliary_call_llm") if isinstance(runtime, dict) else None
    if dotted:
        try:
            from zicato.import_path import import_dotted_path  # noqa: PLC0415

            fn = import_dotted_path(str(dotted), label="runtime.auxiliary_call_llm")
        except Exception as exc:  # noqa: BLE001
            _LOG.info("synthesize: runtime.auxiliary_call_llm did not import (%s)", exc)
            return None
        if callable(fn):
            return fn  # type: ignore[no-any-return]
    return None


def _route_mechanical(
    episode: MinedEpisode,
    board_by_id: dict[str, BoardEntry],
    judge_index: dict[str, list[tuple[str, JudgeSpec]]],
) -> Suggestion | None:
    hint = episode.suggestion_hint
    if hint == HINT_REGRESSION_ENTRY:
        return _regression_suggestion(episode, board_by_id)
    if hint == HINT_HARDER_VARIANT:
        return _harder_variant_suggestion(episode, board_by_id)
    if hint == HINT_RUBRIC_REVISION:
        return _rubric_revision_suggestion(episode, judge_index)
    return None


async def _route_llm(episode: MinedEpisode, aux_call_llm: CallLLM) -> Suggestion | None:
    hint = episode.suggestion_hint
    if hint == HINT_COVERAGE_ENTRY:
        return await _coverage_entry_suggestion(episode, aux_call_llm)
    if hint == HINT_JUDGE:
        return await _judge_suggestion(episode, aux_call_llm)
    return None


def _finalize(suggestions: list[Suggestion]) -> list[Suggestion]:
    """Dedup by id + sort by id — a deterministic, order-independent total order."""
    by_id: dict[str, Suggestion] = {}
    for s in suggestions:
        by_id.setdefault(s.suggestion_id, s)
    return sorted(by_id.values(), key=lambda s: s.suggestion_id)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _judge_index(
    board_entries: Sequence[BoardEntry],
) -> dict[str, list[tuple[str, JudgeSpec]]]:
    """``{judge_name: [(entry_id, spec), ...]}`` — every host of a judge name."""
    index: dict[str, list[tuple[str, JudgeSpec]]] = {}
    for entry in board_entries:
        for judge in entry.judges:
            index.setdefault(judge.name, []).append((entry.id, judge))
    return index


def _unique_id(candidate: str, board_by_id: dict[str, BoardEntry]) -> str:
    """A board-unique id — append a short deterministic suffix on collision."""
    if candidate not in board_by_id:
        return candidate
    suffix = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:4]
    return f"{candidate}_{suffix}"


def _dedup_tags(existing: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_list_dedup([*existing, *extra]))


def _list_dedup(items: Sequence[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _as_str_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw if isinstance(x, str)]
    return []


def _entry_from_refs(refs: tuple[str, ...]) -> str | None:
    """Resolve an entry id from an adjudication run_ref (``"gen:entry"``)."""
    for ref in refs:
        if ":" in ref:
            entry = ref.split(":", 1)[1].strip()
            if entry:
                return entry
    return None


__all__ = [
    "PERTURB_APPEND_EDGE",
    "PERTURB_SUBSTITUTE_NUMERAL",
    "PERTURB_TIGHTEN_BUDGET",
    "SUGGESTION_COVERAGE_ENTRY",
    "SUGGESTION_HARDER_VARIANT",
    "SUGGESTION_JUDGE",
    "SUGGESTION_REGRESSION_ENTRY",
    "SUGGESTION_RUBRIC_REVISION",
    "SYNTH_LLM",
    "SYNTH_MECHANICAL",
    "SYNTH_VERSION",
    "TARGET_EXISTING_JUDGE",
    "TARGET_INCOMING_ROTATION",
    "TARGET_TRAIN",
    "Suggestion",
    "synthesize",
    "synthesize_mechanical",
    "synthesize_suggestions",
]
