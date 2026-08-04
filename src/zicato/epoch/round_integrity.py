"""ROUND-GRANULAR completeness for a measured epoch — liveness is not integrity.

A pure reader over the durable per-round event logs
(:mod:`zicato.epoch.round_log`) that answers one question a scheduler
cannot: *did every round of this epoch actually produce the measurement
it claims to have produced, or did some of them settle without one?*

Why this exists
---------------
During a long parallel measurement sweep an endpoint's credentials
lapsed mid-run. The cell (one workspace / one epoch) that straddled the
outage did not fail cleanly. It kept the results of the rounds that had
already run, satisfied a "did we reach the model at all?" liveness
probe, was marked complete by the scheduler, and then silently
contributed a mean built from fewer duels than its peers. Cell-level
accounting reported every cell healthy while a sixth of the individual
rounds had never emitted a ``gate_evaluated`` event — and because the
loss was correlated with the arm being measured, it biased the very
baseline the rest of the comparison was anchored to.

The lesson is the design rule of this module: **a completeness check
must run at the granularity the endpoint is consumed at (rounds), not
the granularity the scheduler tracks (cells).** A cell that reports
"done" tells you the loop exited; only the round logs tell you how many
duels are actually behind its number.

Why it lives BESIDE ``round_log.py`` rather than inside it
----------------------------------------------------------
:mod:`zicato.epoch.round_log`'s stated scope is *schema + fold*: what a
round-lifecycle event is on the wire, and how a stream of them reduces
to a :class:`~zicato.epoch.round_log.RoundRecord`. This module adds a
*judgement* on top of that record — an infra-error vocabulary and an
acceptance rule — and a judgement is exactly the kind of thing a log
schema must not depend on. Keeping them apart means the acceptance rule
can be tightened, widened, or replaced without touching the durable
format, and a reader of the format never has to reason about which
errors this month's protocol considers disqualifying.

Nothing here writes, mutates, or reaches the network. It reads
``epochs/{epoch}/rounds/*/round_log.jsonl`` and returns dataclasses.

The three statuses
------------------
``complete``
    The round settled (opened AND closed) and evaluated at least one
    gate. This is a round the endpoint consumed: a duel happened and a
    decision came out of it.
``settled_degraded``
    The round settled with no gate, but the proposer *was* reached and
    genuinely produced an invalid patch. A real measurement whose
    outcome was "this arm cannot produce a valid child" — see
    :func:`classify_round`.
``void``
    Anything else: a torn or partial log, a round that closed carrying a
    hard infra error and no gate, or a round that closed with neither a
    measurement nor an explanation. A void round's epoch must not
    contribute a mean.

The cell-acceptance rule
------------------------
A cell (= one workspace / one epoch) is ACCEPTED iff it contains **zero
VOID rounds**. ``complete`` rounds are the ones the endpoint consumed;
``settled_degraded`` rounds are real measurements that produced no duel,
so they neither contribute to the mean nor disqualify the cell. See
:attr:`EpochRoundIntegrity.accepted`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from zicato.epoch.round_log import (
    RoundLog,
    RoundRecord,
    fold_round_record,
    round_log_path,
    rounds_dir,
)


class RoundStatus(StrEnum):
    """The three integrity verdicts a single round can carry.

    A :class:`enum.StrEnum`, so a member equals its lowercase wire token
    (``RoundStatus.VOID == "void"``) and serialises to that token
    through :func:`json.dumps` without a custom encoder — the same
    idiom as :class:`zicato.core.tournament.TournamentDecision`.
    """

    COMPLETE = "complete"
    SETTLED_DEGRADED = "settled_degraded"
    VOID = "void"


#: Lowercase substrings that mark a HARD infrastructure failure in a
#: proposer attempt's free-text error. Scanned over ``proposal.errors``
#: ONLY — see the note in :func:`classify_round`.
#:
#: Three classes, and each is here for the same reason: an error of this
#: shape means the request was refused or never arrived, so the round
#: behind it cannot have measured anything, however cleanly it closed.
#:
#: (a) *Credential / authorization lapse* — the exact outage this module
#:     was written for. A key expires mid-sweep and every subsequent
#:     round fails before the model is ever consulted.
#: (b) *Hard transport failure* — the endpoint was not reached at all
#:     (DNS, refused/reset connection).
#: (c) *Exhausted capacity* — the request was refused outright rather
#:     than served, which for this purpose is the same outage as (a).
#:
#: **The set is biased HARD against false positives, and the asymmetry is
#: deliberate.** A false positive here voids a REAL measurement and sends
#: that arm back around the retry loop to exhaustion — rule 3 outranks
#: rule 4, so a stray match defeats the narrow acceptance the module
#: exists to protect. A false negative merely falls through to rule 5,
#: which still lands VOID for a genuine credential lapse: when the
#: endpoint refused the request the proposer was never reached, so
#: ``proposer_reached`` is False and rule 4 cannot fire either way. In
#: other words this vocabulary mostly buys a BETTER REASON, not extra
#: coverage — which is exactly why it should never reach for a token it
#: is not sure about.
#:
#: Deliberately EXCLUDED for that reason:
#:
#: * bare ``"timeout"`` / ``"timed out"`` — one attempt timing out and a
#:   later attempt returning a real (if invalid) proposal is a real
#:   measurement.
#: * bare ``"forbidden"`` — zicato's OWN proposer emits forbidden-id
#:   rejections as free-text proposal errors (``patch ... targets
#:   forbidden mutation id ...`` in
#:   :func:`zicato.proposer.brief.check_forbidden_ids`, and ``... is in
#:   the forbidden set and may not be patched`` in
#:   :mod:`zicato.mutation.validator`). Those are the CANONICAL
#:   proposer-was-reached-and-produced-an-invalid-patch case, i.e.
#:   precisely ``settled_degraded``. Only the unambiguous HTTP reason
#:   phrase ``"403 forbidden"`` is matched.
#: * bare numeric status codes (``401``, ``403``, ``429``, ``503``) —
#:   three-digit runs occur inside ids, byte offsets, and line numbers in
#:   proposal-error prose. The reason phrases are matched instead; SDK
#:   errors render the phrase alongside the code, and the textual tokens
#:   below (``authentication``, ``unauthorized``, ``api key``, ``rate
#:   limit exceeded``, ``connection refused``, …) carry the real
#:   detection load regardless.
#:
#: **False-negative caveat.** ``ProposalAttempted.errors`` is free text
#: off the proposer's exception, so this match is a heuristic, not a
#: structural fact. An endpoint whose error prose uses none of these
#: tokens reads back as a plain proposer failure — which, absent
#: evidence the proposer was reached, still lands as VOID by rule 5, but
#: with the weaker reason. This vocabulary is a FLOOR on detection, not
#: a proof of its absence; widen it per-caller via the ``infra_markers``
#: parameter rather than by editing this set in place.
HARD_INFRA_MARKERS: frozenset[str] = frozenset(
    {
        # (a) credential / authorization lapse
        "credential",
        "unauthorized",
        "authentication",
        "authentication_error",
        "api key",
        "api_key",
        "invalid_api_key",
        "permission denied",
        "permission_denied",
        "access denied",
        "401 unauthorized",
        "403 forbidden",
        # (b) hard transport failure
        "connection error",
        "connection refused",
        "connection reset",
        "connection aborted",
        "failed to establish a new connection",
        "endpoint unreachable",
        "name or service not known",
        "temporary failure in name resolution",
        # (c) exhausted capacity
        "rate limit exceeded",
        "rate_limit",
        "resource_exhausted",
        "insufficient_quota",
        "quota exceeded",
        "429 too many requests",
        "service unavailable",
        "503 service unavailable",
    }
)


@dataclass(frozen=True, slots=True)
class RoundIntegrity:
    """One round's integrity verdict plus the evidence behind it.

    ``infra_markers`` carries the MATCHED ERROR STRINGS verbatim, not a
    boolean: the operator reading a void verdict needs the endpoint's
    own words to tell a credential lapse from a quota wall, and a
    boolean throws exactly that away. ``evidence`` is the short
    human-readable trail the CLI renders, so a reader can see WHY a
    round was called void without re-running anything.
    """

    round_index: int
    status: str
    opened: bool
    closed: bool
    gate_count: int
    proposer_reached: bool
    invalid_patch: bool
    infra_markers: tuple[str, ...]
    evidence: tuple[str, ...]
    log_path: str

    @property
    def settled(self) -> bool:
        """True when BOTH lifecycle markers are present in the log."""
        return self.opened and self.closed


@dataclass(frozen=True, slots=True)
class EpochRoundIntegrity:
    """Every round of one epoch, classified, plus the cell verdict.

    ``rounds`` is ordered by NUMERIC round index (round ``10`` sorts
    after round ``2``, not between ``1`` and ``2``).
    """

    epoch_id: str
    rounds: tuple[RoundIntegrity, ...]

    @property
    def complete_count(self) -> int:
        """Rounds that settled with at least one gate evaluation."""
        return sum(1 for r in self.rounds if r.status == RoundStatus.COMPLETE)

    @property
    def settled_degraded_count(self) -> int:
        """Rounds that measured a genuinely-degraded arm (no duel)."""
        return sum(1 for r in self.rounds if r.status == RoundStatus.SETTLED_DEGRADED)

    @property
    def void_count(self) -> int:
        """Rounds that produced no trustworthy measurement."""
        return sum(1 for r in self.rounds if r.status == RoundStatus.VOID)

    @property
    def round_count(self) -> int:
        """How many round directories were classified."""
        return len(self.rounds)

    @property
    def no_rounds(self) -> bool:
        """True when the epoch has NO round logs at all.

        Kept separate from :attr:`accepted` on purpose. An epoch with
        zero rounds is vacuously free of void rounds, and letting that
        render as plain health is how an empty cell sneaks through a
        sweep. Callers must surface this loudly; the CLI prints the
        round count on every run for the same reason.
        """
        return not self.rounds

    @property
    def accepted(self) -> bool:
        """The CELL-ACCEPTANCE RULE: no VOID round anywhere in the epoch.

        ``complete`` rounds are the ones the endpoint consumed;
        ``settled_degraded`` rounds are real measurements that produced
        no duel, so they neither contribute to the mean nor disqualify
        the cell. A single void round is enough to reject: a mean built
        from a truncated set of duels is not a smaller measurement, it
        is a different one.
        """
        return self.void_count == 0

    @property
    def counts(self) -> dict[str, int]:
        """Per-status tally keyed by wire token."""
        return {
            RoundStatus.COMPLETE.value: self.complete_count,
            RoundStatus.SETTLED_DEGRADED.value: self.settled_degraded_count,
            RoundStatus.VOID.value: self.void_count,
        }

    def as_dict(self) -> dict[str, Any]:
        """A JSON-ready mapping: the dataclass plus the derived verdict.

        :func:`dataclasses.asdict` sees fields only, and every part of
        the verdict a consuming protocol needs (the counts, the
        acceptance bit, the empty-cell flag) is a property. Serialising
        the fields alone would hand a campaign harness the raw rounds
        and make it re-derive the acceptance rule — which is precisely
        the duplication that lets two callers disagree about what
        "healthy" means. So the derived block is injected here.
        """
        payload: dict[str, Any] = asdict(self)
        payload["counts"] = self.counts
        payload["round_count"] = self.round_count
        payload["no_rounds"] = self.no_rounds
        payload["accepted"] = self.accepted
        return payload


def _matched_markers(
    texts: tuple[str, ...],
    infra_markers: frozenset[str],
) -> tuple[str, ...]:
    """Return, verbatim and de-duplicated, the ``texts`` naming hard infra.

    Matching is case-insensitive substring containment; the ORIGINAL
    string is what comes back, because the operator needs the
    endpoint's own words, not the token that happened to match.
    """
    out: list[str] = []
    for text in texts:
        lowered = text.lower()
        if any(marker in lowered for marker in infra_markers) and text not in out:
            out.append(text)
    return tuple(out)


def classify_round(
    record: RoundRecord,
    *,
    round_index: int,
    log_path: str,
    infra_markers: frozenset[str] = HARD_INFRA_MARKERS,
) -> RoundIntegrity:
    """Classify ONE folded round record. Pure — no I/O.

    The rules, applied in this order:

    1. **Not settled** (missing ``round_opened`` or ``round_closed``) →
       ``void``. A torn or partial log — including an absent one —
       cannot be shown to have finished, so it must not contribute a
       truncated mean. This fires even when the log carries a gate: a
       round that never closed may have had more duels coming.
    2. **Settled with at least one gate** → ``complete``.
    3. **Settled, no gate, a hard infra marker present** → ``void``,
       naming the matched marker. A round that BOTH lacks a gate AND
       carries a credential/transport/quota failure is the outage this
       module exists to catch.
    4. **Settled, no gate, the proposer was reached AND the patch was
       invalid** → ``settled_degraded``. This is the deliberately
       NARROW acceptance and it is load-bearing: a round where the
       proposer really was reached and really did produce an invalid
       patch is a REAL MEASUREMENT of a degraded arm, and voiding it
       would send a legitimately-degraded arm around the retry loop to
       exhaustion, burning the sweep's budget re-measuring a result it
       already has. Accepting it is the point.
    5. **Settled, no gate, anything else** → ``void``: no measurement
       and no explanation for its absence.

    ``proposer_reached`` is asserted from tokens that can only exist
    *after* the proposer model returned something — candidates sampled,
    an experiment minted, patches applied. ``invalid_patch`` is
    asserted from snapshot-validation findings or from a proposal
    attempt the loop rejected on content/schema grounds
    (``RoundRecord.proposal.errors`` accumulates every attempt's error
    strings). ``infra_markers`` is matched against ``proposal.errors``
    ALONE — a validation finding proves a patch existed, so it can only
    ever generate a false void.
    """
    settled = record.opened and record.closed
    gate_count = len(record.gates)
    proposer_reached = bool(
        record.proposal.candidates_sampled > 0
        or record.proposal.experiment_ids
        or record.generation_ids
    )
    invalid_patch = bool(record.validation_findings) or any(record.proposal.errors)
    # Markers are scanned over the PROPOSAL ERRORS ONLY, never over
    # ``validation_findings``. A validation finding is by construction a
    # CONTENT rejection: for it to exist a patch had to exist, so the
    # proposer was reached and the round is a real measurement. Scanning
    # findings for infra tokens could therefore only ever produce a false
    # VOID, never a true one. (The findings still appear in ``evidence``,
    # where they belong — they explain the degradation.)
    markers = _matched_markers(tuple(record.proposal.errors), infra_markers)

    evidence: list[str] = []

    def _finish(status: RoundStatus, reason: str) -> RoundIntegrity:
        evidence.insert(0, reason)
        return RoundIntegrity(
            round_index=round_index,
            status=status.value,
            opened=record.opened,
            closed=record.closed,
            gate_count=gate_count,
            proposer_reached=proposer_reached,
            invalid_patch=invalid_patch,
            infra_markers=markers,
            evidence=tuple(evidence),
            log_path=log_path,
        )

    if proposer_reached:
        evidence.append(
            "proposer reached: "
            f"{record.proposal.candidates_sampled} candidate(s) sampled, "
            f"{len(record.proposal.experiment_ids)} experiment(s) minted, "
            f"{len(record.generation_ids)} generation(s) applied"
        )
    for marker in markers:
        evidence.append(f"infra marker: {marker}")
    for finding in record.validation_findings:
        evidence.append(f"validation finding: {finding}")
    for error in record.proposal.errors:
        if error not in markers:
            evidence.append(f"proposal error: {error}")

    # Rule 1 — no completion marker.
    if not settled:
        missing = "never opened" if not record.opened else "opened but never closed"
        return _finish(RoundStatus.VOID, f"no completion marker ({missing})")

    # Rule 2 — a gate fired: the endpoint consumed this round.
    if gate_count >= 1:
        return _finish(
            RoundStatus.COMPLETE,
            f"settled with {gate_count} gate evaluation(s)",
        )

    # Rule 3 — closed with no gate AND a hard infra failure.
    if markers:
        return _finish(
            RoundStatus.VOID,
            f"closed without a gate, carrying a hard infra error ({markers[0]})",
        )

    # Rule 4 — the narrow acceptance: a real measurement of a degraded arm.
    if proposer_reached and invalid_patch:
        return _finish(
            RoundStatus.SETTLED_DEGRADED,
            "closed without a gate, but the proposer was reached and "
            "returned an invalid patch (a real measurement)",
        )

    # Rule 5 — no measurement, no explanation.
    return _finish(
        RoundStatus.VOID,
        "closed without a gate and without evidence the proposer was reached",
    )


def _relative_log_path(workspace_root: Path, epoch_id: str, round_index: int) -> str:
    """Workspace-relative POSIX path of one round's log, for reporting."""
    path = round_log_path(workspace_root, epoch_id, round_index)
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:  # pragma: no cover - defensive; the path is derived
        return path.as_posix()


def round_integrity(
    workspace_root: Path,
    epoch_id: str,
    round_index: int,
    *,
    infra_markers: frozenset[str] = HARD_INFRA_MARKERS,
) -> RoundIntegrity:
    """Read and classify ONE round's durable log.

    An absent log reads as an empty event stream, which folds to a
    record that neither opened nor closed — void by rule 1, which is the
    honest verdict for a round directory with nothing in it.

    INTERIOR corruption (:meth:`RoundLog.read` raises ``ValueError``
    when a non-tail line is unparseable) is caught and classified void
    with the exception text as evidence. A corrupt log is precisely the
    case this check exists for; it must never crash a sweep-wide
    verification and leave the remaining cells unexamined.
    """
    log_path = _relative_log_path(workspace_root, epoch_id, round_index)
    log = RoundLog(workspace_root, epoch_id, round_index)
    try:
        events = log.read()
    except ValueError as exc:
        return RoundIntegrity(
            round_index=round_index,
            status=RoundStatus.VOID.value,
            opened=False,
            closed=False,
            gate_count=0,
            proposer_reached=False,
            invalid_patch=False,
            infra_markers=(),
            evidence=("log is unreadable — the round cannot be verified", str(exc)),
            log_path=log_path,
        )
    return classify_round(
        fold_round_record(events),
        round_index=round_index,
        log_path=log_path,
        infra_markers=infra_markers,
    )


def epoch_round_integrity(
    workspace_root: Path,
    epoch_id: str,
    *,
    infra_markers: frozenset[str] = HARD_INFRA_MARKERS,
) -> EpochRoundIntegrity:
    """Classify every round of one epoch, in numeric round order.

    Walks ``epochs/{epoch}/rounds/`` for subdirectories whose name is an
    integer and classifies each. A directory whose name is not an
    integer is skipped (it is not a round). A round directory with no
    ``round_log.jsonl`` is void by rule 1 — an empty directory is not
    evidence of a measurement.

    A missing ``rounds/`` directory yields an EMPTY result rather than
    an error. That is vacuously "accepted", which is why
    :attr:`EpochRoundIntegrity.no_rounds` exists and why every caller
    must render the round count: an epoch nothing ever ran is not a
    healthy epoch, it is an unmeasured one.
    """
    base = rounds_dir(workspace_root, epoch_id)
    indices: list[int] = []
    if base.is_dir():
        for child in base.iterdir():
            if not child.is_dir():
                continue
            try:
                indices.append(int(child.name))
            except ValueError:
                continue  # not a round directory
    return EpochRoundIntegrity(
        epoch_id=epoch_id,
        rounds=tuple(
            round_integrity(
                workspace_root,
                epoch_id,
                index,
                infra_markers=infra_markers,
            )
            for index in sorted(indices)
        ),
    )


def render_round_integrity(report: EpochRoundIntegrity) -> str:
    """Render the report as the operator-facing text block.

    Renders the EVIDENCE, not a boolean: every round gets its status,
    its gate count, and the lines explaining the call — matched infra
    markers verbatim — so the verdict can be audited from this output
    alone.
    """
    # ``Path(".")`` as the workspace root turns the path helper into the
    # workspace-RELATIVE convention (``epochs/{id}/rounds``) without
    # restating it here — one source of truth for where round logs live.
    tree = rounds_dir(Path("."), report.epoch_id).as_posix()
    lines: list[str] = [
        f"Epoch {report.epoch_id} — round integrity "
        f"({report.round_count} round(s) under {tree}/)"
    ]
    if report.no_rounds:
        lines.append("  NO ROUNDS — this epoch has no round logs; nothing was measured.")
    for entry in report.rounds:
        lines.append(
            f"  round {entry.round_index:>4}  {entry.status:<16}  gates={entry.gate_count}"
        )
        for line in entry.evidence:
            lines.append(f"      {line}")
    counts = report.counts
    lines.append(
        "  counts: "
        + "  ".join(f"{token}={counts[token]}" for token in sorted(counts))
        + f"  (total {report.round_count})"
    )
    if report.accepted:
        lines.append("  VERDICT: ACCEPTED — no void round; every round is accountable.")
    else:
        lines.append(
            f"  VERDICT: NOT ACCEPTED — {report.void_count} void round(s); "
            "this cell's mean is built from fewer duels than it claims."
        )
    return "\n".join(lines)


__all__ = [
    "HARD_INFRA_MARKERS",
    "RoundStatus",
    "RoundIntegrity",
    "EpochRoundIntegrity",
    "classify_round",
    "round_integrity",
    "epoch_round_integrity",
    "render_round_integrity",
]
