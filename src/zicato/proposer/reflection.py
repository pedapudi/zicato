"""Proposer self-reflection — recommend-only, gated at the epoch boundary.

Board reflection audits the evaluation contract and never edits it. This is the
same instrument aimed one level up: it audits the PROPOSER against its own
scorecard, and produces findings whose remedy slot is a **ready-to-apply edit
to the proposer dir** — a new ``skills/*.md``, or a replacement for an existing
one. It never applies one. Applying is an operator command
(``zicato proposer apply-recommendation``), it happens at an epoch boundary,
and it rolls the contract hash because the proposer dir is a hashed contract
input (PROPOSER.md §4).

The four invariants (issue #169), and where each one lives
----------------------------------------------------------
* **Never mid-epoch** — this module writes only findings; the only writer that
  touches the proposer dir is :mod:`zicato.proposer.apply_recommendation`, and
  the edit it makes is detected as contract drift on the next ``evolve``, which
  opens a fresh epoch before proposing anything.
* **Never self-applied** — there is no call path from :func:`reflect` to the
  apply module. Nothing here imports it; the test suite pins that.
* **Redacted evidence only** — :func:`assert_redacted` runs over every record
  before it is written and RAISES on an identity-bearing key or a board-content
  key at any depth. It is an active guard, not a convention: a future emitter
  that reaches for an entry id fails at persist time rather than leaking.
* **Every accepted edit is hashed** — the remedy carries the SHA-256 of the
  exact bytes it would write, and the apply command records that digest, so an
  applied recommendation is verifiable after the fact.

The investigation seam
----------------------
What the pass may LOOK AT is a pluggable substrate
(:class:`InvestigationSource`) that returns an :class:`Investigation`. v1 ships
:class:`ScorecardInvestigation` — the epoch's scorecard plus a BANDED history
of the epochs before it. A richer substrate (a redacted query facility over the
workspace, issue #147 phase 5) implements the same protocol and returns the
same :class:`Investigation`, so it drops in without reshaping a single
persisted record. The emitters read the ``Investigation``, never the workspace.

Why the history is banded
-------------------------
The comparison slot is the one number a drafting model reads round over round.
Handing it the exact per-epoch rate would hand it a response surface to climb —
the memorization risk PROPOSER.md §2.5 and OVERFITTING.md §11.4 close for the
proposer's failure-mode channel. So historical rates go through
:func:`zicato.proposer.prompts.band_rate` and reach the record as ``~30%``. The
CURRENT epoch's card keeps its exact numbers: it is what the operator is being
asked to act on, and it is not a gradient over anything.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from zicato.core.runtime import CallLLM
from zicato.core.workspace import (
    proposer_reflection_findings_path,
    proposer_reflections_dir,
)
from zicato.proposer.prompts import band_rate
from zicato.proposer.scorecard import (
    MIN_SAMPLE_N,
    ProposerScorecard,
    Rate,
    read_epoch_scorecard,
    read_scorecard_trend,
)

# Severity vocabulary — the board-reflection set, so the two findings surfaces
# rank and colour identically.
SEVERITY_CRITICAL: str = "critical"
SEVERITY_WARNING: str = "warning"
SEVERITY_INFO: str = "info"
_SEVERITY_RANK = {SEVERITY_CRITICAL: 3, SEVERITY_WARNING: 2, SEVERITY_INFO: 1}

#: A rate must reach this before an emitter will draft an edit against it. Set
#: at a quarter of proposals: below that the mechanism is a nuisance, not a
#: weakness worth changing the proposer's contract over.
FIRE_THRESHOLD: float = 0.25

#: A rate at or above this is CRITICAL rather than a warning — the mechanism is
#: failing more often than it works.
CRITICAL_THRESHOLD: float = 0.5


# ---------------------------------------------------------------------------
# Redaction — the active guard, not a convention
# ---------------------------------------------------------------------------

#: Keys that must never appear in a persisted proposer-reflection record, at
#: any depth. Two families: identity keys that name a specific board entry, and
#: content keys that would carry task/answer/transcript text. The proposer may
#: learn an aggregate property of its own behaviour; it may never learn what the
#: board asks.
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "entry_id",
        "entry_ids",
        "entries",
        "task",
        "task_text",
        "question",
        "prompt",
        "expected",
        "expected_output",
        "answer",
        "output",
        "transcript",
        "turns",
        "holdout",
        "holdout_entries",
        "attributable_regressions",
        "run_ref",
        "span",
        "evidence_span",
    }
)


class RedactionError(RuntimeError):
    """A record reached the persist boundary carrying board content."""


def assert_redacted(payload: Any, *, where: str = "record") -> None:
    """Raise :class:`RedactionError` if ``payload`` carries a forbidden key.

    Walks the whole structure — dicts, lists, tuples — because a leak one level
    down is still a leak. Deliberately a KEY check rather than a value scan: a
    value scan needs to know the board to know what to look for (and so would
    have to read it), while the key check is total and needs nothing. Every
    channel that could carry entry text into a record does so under one of
    these names, and an emitter that invents a new one is adding a field that
    must be reviewed anyway.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise RedactionError(
                    f"{where} carries forbidden key {key!r} — proposer-reflection records "
                    "hold aggregate mechanism evidence only, never board content "
                    "(issue #169, 'redacted evidence only')"
                )
            assert_redacted(value, where=f"{where}.{key}")
        return
    if isinstance(payload, list | tuple):
        for i, item in enumerate(payload):
            assert_redacted(item, where=f"{where}[{i}]")


# ---------------------------------------------------------------------------
# The investigation seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Investigation:
    """The redacted substrate one reflection pass reasoned over.

    ``card`` is the epoch under review at full resolution; ``history`` is the
    BANDED read of the epochs before it (one row per epoch, rates as ``~30%``
    labels). ``source`` names the substrate that produced this, so a record
    says what the pass could see when it drew its conclusions.
    """

    epoch_id: str
    card: ProposerScorecard
    history: tuple[dict[str, Any], ...] = ()
    source: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "epoch_id": self.epoch_id,
            "source": self.source,
            "card": self.card.to_json(),
            "history": [dict(row) for row in self.history],
        }


@runtime_checkable
class InvestigationSource(Protocol):
    """What a reflection pass is allowed to look at.

    v1's implementation reads the scorecard; a later one reads a redacted query
    facility. Both return :class:`Investigation`, so the findings records —
    and every surface over them — are unchanged by the swap.

    One method, deliberately: a substrate NAMES itself through the
    ``source`` it stamps on the :class:`Investigation` it returns, which is the
    same string the record persists. Requiring a separate ``name`` attribute
    would let the two disagree, and the record would then claim a provenance
    the pass did not have.
    """

    def investigate(self, workspace_root: Path, epoch_id: str) -> Investigation:
        """Gather the substrate for ``epoch_id``. Must not read board content."""
        ...


def _banded_row(card: ProposerScorecard) -> dict[str, Any]:
    """One banded history row — coarse labels, sample counts kept.

    The sample count survives banding on purpose: it is not a response surface
    (it is how much evidence there was) and dropping it would re-introduce the
    "is that 40% over five proposals or five hundred" ambiguity the scorecard
    exists to close.
    """

    def band(rate: Rate) -> str | None:
        value = rate.value
        return None if value is None else band_rate(value)

    return {
        "epoch_id": card.epoch_id,
        "proposer_agent_id": card.proposer_agent_id,
        "rounds": card.rounds,
        "proposals": card.proposals,
        "promote_rate": band(card.promote_rate),
        "validation_failure_rate": band(card.validation_failure_rate),
        "validator_failure_rates": {
            code: band(rate) for code, rate in sorted(card.validator_failure_rates.items())
        },
        "screen_veto_rate": band(card.screen_veto_rate),
    }


@dataclass(frozen=True, slots=True)
class ScorecardInvestigation:
    """v1's substrate — the epoch's scorecard plus banded prior-epoch history."""

    name: str = "scorecard"
    history_limit: int = 8

    def investigate(self, workspace_root: Path, epoch_id: str) -> Investigation:
        card = read_epoch_scorecard(workspace_root, epoch_id)
        trend = read_scorecard_trend(workspace_root, limit=self.history_limit)
        history = tuple(_banded_row(c) for c in trend if c.epoch_id != epoch_id)
        return Investigation(epoch_id=epoch_id, card=card, history=history, source=self.name)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposerRemedy:
    """A ready-to-apply edit to the proposer dir — the finding's remedy slot.

    ``relative_path`` is resolved against the proposer dir; ``new_text`` is the
    exact bytes the apply command writes; ``sha256`` digests them so an applied
    recommendation is verifiable afterwards. ``diff`` is the unified diff
    against what is on disk today, for the operator to read before deciding.
    """

    kind: str
    relative_path: str
    new_text: str
    sha256: str
    diff: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "relative_path": self.relative_path,
            "new_text": self.new_text,
            "sha256": self.sha256,
            "diff": self.diff,
        }


@dataclass(frozen=True, slots=True)
class ProposerFinding:
    """One recommendation, carrying the five-slot evidence convention.

    The slots are the same five board reflection's findings carry, read for a
    proposer: ``population`` (which proposals, which epochs), ``measured`` (the
    scorecard numbers that fired it), ``compared_against`` (the banded prior
    epochs or the base rate), ``remedy`` (the drafted diff — a real payload, not
    a prose suggestion), and ``remedy_safety`` (what the edit cannot affect).
    """

    finding_id: str
    severity: str
    title: str
    detail: str
    population: str
    measured: tuple[dict[str, Any], ...]
    compared_against: str
    remedy: ProposerRemedy | None
    remedy_safety: str

    def to_json(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "population": self.population,
            "measured": [dict(m) for m in self.measured],
            "compared_against": self.compared_against,
            "remedy": self.remedy.to_json() if self.remedy is not None else None,
            "remedy_safety": self.remedy_safety,
        }


#: What the edit an accepted recommendation makes CANNOT affect. Stated once
#: because it is the same statement for every finding this module emits: a
#: proposer skill is markdown the proposer reads, so the blast radius is the
#: proposer's own context — not the board, not the gate, not the scoring.
REMEDY_SAFETY: str = (
    "The edit writes markdown under the proposer dir's skills/. It cannot change the "
    "board, the scoring weights, the promote gate, or any generation already scored — "
    "a skill is context the proposer reads, not a contract term the tournament applies. "
    "It IS a hashed contract input, so applying it rolls the epoch and the proposals "
    "before and after it are not directly comparable."
)


def _finding_id(subject: str, kind: str) -> str:
    """Content-stable finding id, independent of emission order.

    Deterministic so ``apply-recommendation <id>`` resolves the same finding
    across two re-derivations of the same epoch — the same discipline
    :func:`zicato.reflection.findings._finding_id` keeps.
    """
    digest = hashlib.sha256(f"proposer|{subject}|{kind}".encode()).hexdigest()[:8]
    return f"prec-{digest}"


def _severity_for(rate: float) -> str:
    return SEVERITY_CRITICAL if rate >= CRITICAL_THRESHOLD else SEVERITY_WARNING


def _fires(rate: Rate) -> float | None:
    """The rate's value when it is BOTH high enough and measured enough.

    A provisional rate never drafts an edit to the contract. The scorecard
    reports it (suppressing information is its own dishonesty), but changing
    what the proposer is on the strength of four observations is exactly the
    move the sample-count discipline exists to prevent.
    """
    value = rate.value
    if value is None or rate.n < MIN_SAMPLE_N or value < FIRE_THRESHOLD:
        return None
    return value


# The per-check remedy table. Each row is one post-apply check code, the skill
# file an accepted recommendation would write, and the guidance body. The
# bodies are deliberately MECHANISM-level ("preserve the import block"), never
# board-level — that is what makes them safe to draft from aggregate evidence.
_CHECK_REMEDIES: dict[str, dict[str, str]] = {
    "A1": {
        "slug": "emit-parseable-python",
        "title": "produce syntactically valid Python",
        "guidance": (
            "Every patch that rewrites a `.py` span must leave the file parseable. "
            "Before emitting a `replace`, re-read the span you are replacing and check "
            "that your replacement closes every bracket, quote, and block it opens, and "
            "that its indentation matches the surrounding scope — the span is spliced "
            "back verbatim, so the indentation of your first line is the indentation it "
            "lands at. Prefer a smaller, self-contained rewrite over a large one that "
            "restructures control flow you cannot see the end of."
        ),
    },
    "A2": {
        "slug": "keep-mutation-markers",
        "title": "keep every mutation marker intact",
        "guidance": (
            "A mutation id must still resolve after your patch applies — the next round "
            "has to find the same site. The marker comments that delimit a span are NOT "
            "part of the content you are replacing: never emit them inside "
            "`new_content`, and never delete or renumber a marker. If a change seems to "
            "require removing a site, it is out of scope for a patch — the mutation "
            "surface is operator-owned."
        ),
    },
    "A3": {
        "slug": "keep-required-placeholders",
        "title": "keep required placeholders",
        "guidance": (
            "Some spans declare `required_placeholders` in their marker metadata. Those "
            "exact substrings — braces included, e.g. `{user_message}` — are injected by "
            "the surrounding code and MUST survive verbatim in your replacement. Read the "
            "site's metadata before rewriting it, and when you rephrase prose around a "
            "placeholder, copy the placeholder token rather than retyping it."
        ),
    },
    "A4": {
        "slug": "preserve-imports",
        "title": "preserve top-level imports",
        "guidance": (
            "When a patch rewrites a whole `.py` file, the post-apply import set must be "
            "a SUPERSET of the pre-apply set: you may add imports, never silently drop "
            "one. Before emitting a file-level `replace`, list the top-level "
            "`import` / `from ... import` statements in the original and confirm each "
            "one is still present in your replacement — including imports the code you "
            "rewrote no longer uses, since another part of the file may."
        ),
    },
}


def _skill_text(*, name: str, description: str, guidance: str, evidence: str) -> str:
    """Render a drafted skill as a SKILL.md-style markdown module.

    The frontmatter is the shape :func:`zicato.proposer.skills.parse_frontmatter`
    reads, so a drafted skill loads the moment it is applied. The evidence line
    is included in the body ON PURPOSE: a skill that says why it exists survives
    the next operator's review of the proposer dir, and it is aggregate
    mechanism evidence, which is exactly what the proposer is allowed to read.
    """
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {description[:1].upper()}{description[1:]}\n\n"
        f"{guidance}\n\n"
        f"_Drafted by `zicato proposer reflect` from {evidence}._\n"
    )


def _diff_against(proposer_path: Path | None, relative_path: str, new_text: str) -> tuple[str, str]:
    """Return ``(kind, unified diff)`` for writing ``new_text`` at ``relative_path``.

    ``kind`` is ``skill_add`` when nothing is there today and ``skill_replace``
    when there is — the operator reading the queue can tell at a glance whether
    a recommendation grows the proposer or rewrites part of it. With no
    proposer dir configured (the built-in default) every remedy is an add, and
    the diff is against an empty file.
    """
    current = ""
    if proposer_path is not None:
        target = proposer_path / relative_path
        if target.is_file():
            current = target.read_text(encoding="utf-8")
    kind = "skill_replace" if current else "skill_add"
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{relative_path}" if current else "/dev/null",
            tofile=f"b/{relative_path}",
        )
    )
    return kind, diff


def _remedy(
    proposer_path: Path | None,
    *,
    slug: str,
    description: str,
    guidance: str,
    evidence: str,
) -> ProposerRemedy:
    relative_path = f"skills/{slug}.md"
    text = _skill_text(name=slug, description=description, guidance=guidance, evidence=evidence)
    kind, diff = _diff_against(proposer_path, relative_path, text)
    return ProposerRemedy(
        kind=kind,
        relative_path=relative_path,
        new_text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        diff=diff,
    )


def _history_comparison(investigation: Investigation, key: str) -> str:
    """A one-line banded comparison against the prior epochs, or the honest absence."""
    rows = [
        f"{row['epoch_id']}={row.get(key)}"
        for row in investigation.history
        if row.get(key) is not None
    ]
    if not rows:
        return (
            "No prior epoch carries this rate — this is the first measurement, "
            "so there is no trend to compare against."
        )
    return "Banded prior epochs: " + ", ".join(rows)


def _check_history(investigation: Investigation, code: str) -> str:
    rows = [
        f"{row['epoch_id']}={(row.get('validator_failure_rates') or {}).get(code)}"
        for row in investigation.history
        if (row.get("validator_failure_rates") or {}).get(code) is not None
    ]
    if not rows:
        return (
            f"No prior epoch carries a {code} rate — this is the first measurement, "
            "so there is no trend to compare against."
        )
    return f"Banded prior epochs ({code}): " + ", ".join(rows)


def derive_findings(
    investigation: Investigation,
    *,
    proposer_path: Path | None = None,
) -> list[ProposerFinding]:
    """Fold an :class:`Investigation` into ranked, remedy-carrying findings.

    Deterministic and free — no model is called. An LLM may LATER redraft a
    remedy's prose (:func:`draft_remedy`), but the diagnosis, the thresholds,
    and the decision to emit at all are mechanical, so the operator's queue is
    reproducible from the same round logs.

    Returns findings sorted by descending severity; ties keep emission order.
    """
    card = investigation.card
    population = (
        f"{card.proposals} proposal attempt(s) across {card.rounds} round(s) of epoch "
        f"{card.epoch_id}, proposed by {card.proposer_agent_id or 'an unresolved proposer'}."
    )
    findings: list[ProposerFinding] = []

    # --- per-check validator weaknesses ------------------------------------
    for code, spec in _CHECK_REMEDIES.items():
        rate = card.validator_failure_rates.get(code)
        if rate is None:
            continue
        value = _fires(rate)
        if value is None:
            continue
        description = spec["title"]
        evidence = f"a {value:.0%} {code} failure rate over {rate.n} proposal attempts"
        findings.append(
            ProposerFinding(
                finding_id=_finding_id(code, "validator_failure"),
                severity=_severity_for(value),
                title=f"Post-apply check {code} fails on {value:.0%} of proposals",
                detail=(
                    f"{rate.k} of {rate.n} proposal attempts in this epoch failed post-apply "
                    f"check {code}. That is a MECHANISM gap, not a taste gap: the patch never "
                    "reached a tournament, so the round spent a proposer call and produced no "
                    "evidence. The drafted skill states the constraint the proposer keeps "
                    "violating, in the proposer's own context, where it is read before the "
                    "patch is written rather than after it is rejected."
                ),
                population=population,
                measured=({"metric": f"validator_failure_rate.{code}", **rate.to_json()},),
                compared_against=_check_history(investigation, code),
                remedy=_remedy(
                    proposer_path,
                    slug=spec["slug"],
                    description=description,
                    guidance=spec["guidance"],
                    evidence=evidence,
                ),
                remedy_safety=REMEDY_SAFETY,
            )
        )

    # --- the screen keeps vetoing what the proposer keeps sampling ---------
    veto_value = _fires(card.screen_veto_rate)
    if veto_value is not None:
        rate = card.screen_veto_rate
        findings.append(
            ProposerFinding(
                finding_id=_finding_id("screen", "veto_heavy"),
                severity=_severity_for(veto_value),
                title=f"The pre-tournament screen vetoes {veto_value:.0%} of candidates",
                detail=(
                    f"{rate.k} of {rate.n} screened candidates were vetoed before the "
                    "tournament ran. The screen is cheap and the tournament is not, so a high "
                    "veto rate is the proposer paying full sampling cost for candidates that "
                    "were never going to be measured. The drafted skill asks for the check the "
                    "screen performs to be performed BEFORE the patch is emitted."
                ),
                population=population,
                measured=({"metric": "screen_veto_rate", **rate.to_json()},),
                compared_against=_history_comparison(investigation, "screen_veto_rate"),
                remedy=_remedy(
                    proposer_path,
                    slug="screen-before-you-propose",
                    description="check a candidate against the screen's own question first",
                    guidance=(
                        "The pre-tournament screen asks one question: does this candidate "
                        "still pass what the parent already passed? Ask it of your own patch "
                        "before you emit it. A change that rewrites shared behaviour to fix "
                        "one narrow case will regress the cases that depended on the old "
                        "behaviour, and the screen will catch it — at the cost of the whole "
                        "sampling round. Prefer additive, narrowly-scoped changes; when a "
                        "change must alter shared behaviour, say so in the hypothesis so the "
                        "trade is visible rather than accidental."
                    ),
                    evidence=(
                        f"a {veto_value:.0%} screen-veto rate over " f"{rate.n} screened candidates"
                    ),
                ),
                remedy_safety=REMEDY_SAFETY,
            )
        )

    # --- the revise re-sample is not recovering the slate ------------------
    revise = card.revision_success_rate
    if revise.n >= MIN_SAMPLE_N and revise.value is not None and revise.value < FIRE_THRESHOLD:
        findings.append(
            ProposerFinding(
                finding_id=_finding_id("revise", "recovery_low"),
                severity=SEVERITY_WARNING,
                title=f"Screen-informed revision recovers only {revise.value:.0%} of slates",
                detail=(
                    f"{revise.k} of {revise.n} revise re-samples survived the screen. The "
                    "revise slot exists to turn an all-vetoed slate into a measurable "
                    "candidate using the screen's own counts-only feedback; at this rate it "
                    "is mostly re-sampling the same failure. The drafted skill tells the "
                    "proposer what the revise feedback actually contains and what to do "
                    "with it."
                ),
                population=population,
                measured=({"metric": "revision_success_rate", **revise.to_json()},),
                compared_against=(
                    "The revise slot is a recovery mechanism; a rate near zero means it is "
                    "spending a sample without changing the outcome."
                ),
                remedy=_remedy(
                    proposer_path,
                    slug="use-the-screen-feedback",
                    description="treat a screen veto as evidence, not as a retry signal",
                    guidance=(
                        "When your slate is vetoed you get counts, not identities: how many "
                        "checks the parent passed and how many your candidate passed. That "
                        "difference is the whole signal, and it says your change broke "
                        "behaviour that already worked. Re-sampling the same idea will be "
                        "vetoed again. Instead, make the replacement candidate SMALLER than "
                        "the vetoed one — target a single site, leave shared behaviour "
                        "alone — or target a different site entirely."
                    ),
                    evidence=(
                        f"a {revise.value:.0%} revise-recovery rate over {revise.n} re-samples"
                    ),
                ),
                remedy_safety=REMEDY_SAFETY,
            )
        )

    # --- a mutation site the proposer keeps failing on ---------------------
    for site in card.mutation_sites:
        if site.proposed < MIN_SAMPLE_N or site.promoted > 0:
            continue
        findings.append(
            ProposerFinding(
                finding_id=_finding_id(site.mutation_id, "site_never_promotes"),
                severity=SEVERITY_INFO,
                title=f"Mutation site {site.mutation_id} has never promoted",
                detail=(
                    f"The proposer patched {site.mutation_id} in {site.proposed} rounds and "
                    "none of them promoted. That is a claim about this proposer at this "
                    "site, not about the site itself — another proposer may do better, and "
                    "the site may simply not be where the loss is. It is surfaced as INFO "
                    "for exactly that reason: the honest response is usually an operator "
                    "decision about the mutation surface, not an edit to the proposer."
                ),
                population=population,
                measured=(
                    {
                        "metric": "mutation_site.promote_rate",
                        "mutation_id": site.mutation_id,
                        **site.promote_rate.to_json(),
                    },
                ),
                compared_against=(
                    "Compared against the epoch's own promote rate: "
                    f"{card.promote_rate.k}/{card.promote_rate.n} rounds promoted."
                ),
                remedy=None,
                remedy_safety=(
                    "No edit is drafted. Retargeting the proposer away from a site is an "
                    "operator decision about the mutation surface (the brief's Forbidden "
                    "list), not a skill the proposer can be given."
                ),
            )
        )

    findings.sort(key=lambda f: _SEVERITY_RANK.get(f.severity, 0), reverse=True)
    return findings


# ---------------------------------------------------------------------------
# The optional drafting call
# ---------------------------------------------------------------------------

_DRAFT_SYSTEM = (
    "You are refining one guidance module for an automated code-proposing agent. "
    "You are given a mechanism-level weakness measured in aggregate and a draft of the "
    "guidance that addresses it. Rewrite the guidance to be more concrete and more "
    "actionable for the agent that will read it. Constraints: output ONLY the replacement "
    "guidance prose, no frontmatter and no headings; stay under 200 words; describe the "
    "mechanism and what to do about it, never a specific task, dataset, or expected "
    "answer; invent no numbers beyond those you are given."
)


async def draft_remedy(
    finding: ProposerFinding,
    *,
    call_llm: CallLLM,
    model: str,
    proposer_path: Path | None = None,
) -> ProposerFinding:
    """Return ``finding`` with its remedy prose redrafted by the auxiliary model.

    OPTIONAL and off by default: :func:`derive_findings` already produces a
    complete, appliable remedy, so the model is a polish pass over guidance the
    operator will read — never the thing that decides a finding fires. A finding
    with no remedy is returned unchanged; so is one whose call returns nothing
    usable, because a degraded draft must not replace a working one.
    """
    if finding.remedy is None:
        return finding
    user = json.dumps(
        {
            "title": finding.title,
            "detail": finding.detail,
            "measured": [dict(m) for m in finding.measured],
            "draft_guidance": finding.remedy.new_text,
        },
        indent=2,
        sort_keys=True,
    )
    try:
        raw = await call_llm(_DRAFT_SYSTEM, user, model)
    except Exception:  # noqa: BLE001 - a failed polish pass must not fail the finding
        return finding
    guidance = (raw or "").strip()
    if not guidance:
        return finding
    slug = Path(finding.remedy.relative_path).stem
    description = finding.title
    text = _skill_text(
        name=slug,
        description=description,
        guidance=guidance,
        evidence="`zicato proposer reflect`, refined by the auxiliary model",
    )
    kind, diff = _diff_against(proposer_path, finding.remedy.relative_path, text)
    remedy = ProposerRemedy(
        kind=kind,
        relative_path=finding.remedy.relative_path,
        new_text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        diff=diff,
    )
    return ProposerFinding(
        finding_id=finding.finding_id,
        severity=finding.severity,
        title=finding.title,
        detail=finding.detail,
        population=finding.population,
        measured=finding.measured,
        compared_against=finding.compared_against,
        remedy=remedy,
        remedy_safety=finding.remedy_safety,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def mint_reflection_id(*, now: _dt.datetime | None = None) -> str:
    """A sortable, filesystem-safe id for one proposer-reflection pass."""
    stamp = (now or _dt.datetime.now(_dt.UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"prefl-{stamp}"


def _write_json(path: Path, payload: Any) -> None:
    """Atomically write ``payload`` as pretty JSON (tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True, slots=True)
class ProposerReflection:
    """One persisted recommend-only pass."""

    reflection_id: str
    epoch_id: str
    created_at: str
    investigation_source: str
    findings: tuple[ProposerFinding, ...] = ()
    investigation: Investigation | None = field(default=None)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reflection_id": self.reflection_id,
            "epoch_id": self.epoch_id,
            "created_at": self.created_at,
            "investigation_source": self.investigation_source,
            "findings": [f.to_json() for f in self.findings],
        }
        if self.investigation is not None:
            payload["investigation"] = self.investigation.to_json()
        return payload


def write_reflection(workspace_root: Path, reflection: ProposerReflection) -> Path:
    """Persist one pass's ``findings.json``; return the path written.

    Runs :func:`assert_redacted` over the FULL payload first. A record that
    would leak never reaches the disk, and the failure is loud — an emitter bug
    is a bug, not a degrade.
    """
    payload = reflection.to_json()
    assert_redacted(payload, where=f"proposer reflection {reflection.reflection_id}")
    path = proposer_reflection_findings_path(
        workspace_root, reflection.epoch_id, reflection.reflection_id
    )
    _write_json(path, payload)
    return path


def reflect(
    workspace_root: Path,
    epoch_id: str,
    *,
    source: InvestigationSource | None = None,
    proposer_path: Path | None = None,
    now: _dt.datetime | None = None,
) -> ProposerReflection:
    """Run one recommend-only pass over ``epoch_id`` and return it UNWRITTEN.

    Deriving and persisting are separate so a caller can inspect (or redraft)
    the findings before anything lands on disk, and so a dry run costs nothing.
    Nothing in this function can apply a remedy — that is the "never
    self-applied" invariant, and it holds because the apply module is not
    reachable from here.
    """
    substrate: InvestigationSource = ScorecardInvestigation() if source is None else source
    investigation = substrate.investigate(workspace_root, epoch_id)
    findings = derive_findings(investigation, proposer_path=proposer_path)
    created = (now or _dt.datetime.now(_dt.UTC)).isoformat()
    return ProposerReflection(
        reflection_id=mint_reflection_id(now=now),
        epoch_id=epoch_id,
        created_at=created,
        investigation_source=investigation.source,
        findings=tuple(findings),
        investigation=investigation,
    )


# ---------------------------------------------------------------------------
# Reading back — the pending queue
# ---------------------------------------------------------------------------


def _epoch_ids_newest_first(workspace_root: Path) -> list[str]:
    """Epoch ids in reverse canonical order, enumerated from the DIRECTORY.

    Deliberately the directory enumeration rather than
    :func:`zicato.epoch.lifecycle.list_epochs`: a recommendation lives under
    ``epochs/<id>/proposer_reflections/`` and is perfectly readable whether or
    not that epoch's ``config.json`` parses. Requiring the config would make an
    unreadable contract silently swallow the operator's pending queue.
    """
    from zicato.workspace import WorkspaceLayout, list_epoch_ids  # noqa: PLC0415

    try:
        return list(reversed(list_epoch_ids(WorkspaceLayout.from_root(workspace_root))))
    except OSError:
        return []


def list_reflections(workspace_root: Path, epoch_id: str) -> list[dict[str, Any]]:
    """Every persisted pass for ``epoch_id``, newest id first; ``[]`` when none."""
    base = proposer_reflections_dir(workspace_root, epoch_id)
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(base.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        path = proposer_reflection_findings_path(workspace_root, epoch_id, child.name)
        payload = _read_json(path)
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_finding(
    workspace_root: Path,
    finding_id: str,
    *,
    epoch_id: str | None = None,
) -> tuple[str, str, dict[str, Any]] | None:
    """Find one recommendation by id; return ``(epoch_id, reflection_id, finding)``.

    Searches ``epoch_id`` when given, else every epoch newest-first. Ids are
    content-stable, so the same recommendation re-derived in a later pass
    resolves to the newest copy — which is the one whose diff is against the
    proposer dir as it stands now.
    """
    epoch_ids = [epoch_id] if epoch_id is not None else _epoch_ids_newest_first(workspace_root)
    for eid in epoch_ids:
        for payload in list_reflections(workspace_root, eid):
            for finding in payload.get("findings", []):
                if isinstance(finding, dict) and finding.get("finding_id") == finding_id:
                    return eid, str(payload.get("reflection_id", "")), finding
    return None


def pending_recommendations(
    workspace_root: Path,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every drafted recommendation that carries a remedy and has not been applied.

    "Applied" is read from the epoch records (each epoch stamps the ids applied
    into the proposer that produced it) plus the staged queue an apply writes
    before the next epoch drains it. A recommendation re-derived after being
    applied stays out of the queue, because the id is content-stable: same
    weakness, same id, already answered.
    """
    applied = applied_recommendation_ids(workspace_root)
    epochs = _epoch_ids_newest_first(workspace_root)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for eid in epochs:
        for payload in list_reflections(workspace_root, eid):
            for finding in payload.get("findings", []):
                if not isinstance(finding, dict) or finding.get("remedy") is None:
                    continue
                fid = str(finding.get("finding_id", ""))
                if not fid or fid in seen or fid in applied:
                    continue
                seen.add(fid)
                out.append(
                    {
                        **finding,
                        "epoch_id": eid,
                        "reflection_id": payload.get("reflection_id", ""),
                    }
                )
                if limit is not None and len(out) >= limit:
                    return out
    return out


def render_recommendation_lines(pending: list[dict[str, Any]]) -> list[str]:
    """The pending queue as an epoch boundary prints it; ``[]`` when empty.

    Lives here rather than in the CLI because THREE surfaces print it — the
    ``recommendations`` command, ``zicato epoch new``, and evolve's auto-roll —
    and the operator should meet the same wording at each. Empty means print
    nothing at all: a boundary with no pending recommendation must stay as
    quiet as it was before this feature existed.
    """
    if not pending:
        return []
    lines = [
        f"Pending proposer recommendations ({len(pending)}) — "
        "applying one is free at this boundary:",
    ]
    for item in pending:
        remedy = item.get("remedy") or {}
        lines.append(
            f"  [{item.get('severity', '?')}] {item.get('finding_id', '?')}  "
            f"{item.get('title', '')}"
        )
        lines.append(
            f"      recommendation: {remedy.get('kind', 'edit')} "
            f"{remedy.get('relative_path', '?')}  (from epoch {item.get('epoch_id', '?')})"
        )
    lines.append("  Apply one with: zicato proposer apply-recommendation <id>")
    return lines


def echo_pending_recommendations(workspace_root: Path) -> None:
    """Print the pending queue at an epoch boundary; silent when there is none.

    Best-effort by construction: a workspace with no reflections, an unreadable
    record, or a missing epoch list yields an empty queue and prints nothing.
    Surfacing a recommendation must never be able to fail an epoch roll.
    """
    try:
        pending = pending_recommendations(workspace_root)
    except Exception:  # noqa: BLE001 - a report must never break the boundary
        return
    for line in render_recommendation_lines(pending):
        print(line)


def applied_recommendation_ids(workspace_root: Path) -> set[str]:
    """Every recommendation id already applied, from the epoch records + the queue."""
    from zicato.core.workspace import (  # noqa: PLC0415
        proposer_staged_recommendations_path,
    )
    from zicato.epoch.lifecycle import list_epochs  # noqa: PLC0415

    applied: set[str] = set()
    try:
        for cfg in list_epochs(workspace_root):
            applied.update(cfg.applied_proposer_recommendations)
    except (OSError, ValueError, FileNotFoundError):
        pass
    staged = _read_json(proposer_staged_recommendations_path(workspace_root))
    if isinstance(staged, dict):
        raw = staged.get("recommendation_ids")
        if isinstance(raw, list):
            applied.update(str(x) for x in raw)
    return applied


__all__ = [
    "CRITICAL_THRESHOLD",
    "FIRE_THRESHOLD",
    "FORBIDDEN_KEYS",
    "REMEDY_SAFETY",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "Investigation",
    "InvestigationSource",
    "ProposerFinding",
    "ProposerReflection",
    "ProposerRemedy",
    "RedactionError",
    "ScorecardInvestigation",
    "applied_recommendation_ids",
    "assert_redacted",
    "derive_findings",
    "draft_remedy",
    "list_reflections",
    "mint_reflection_id",
    "pending_recommendations",
    "echo_pending_recommendations",
    "read_finding",
    "reflect",
    "render_recommendation_lines",
    "write_reflection",
]
