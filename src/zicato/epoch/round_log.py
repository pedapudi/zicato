"""The per-round durable EVENT LOG — one evolve round's store-of-record trace.

Every decision an evolve round makes today is scattered across the
journal, ``experiment.json``, the runtime tournament log (ephemeral), and
free-text reasons. This module defines the ONE durable, replayable record
of a round: a typed, sequenced JSONL event log at

``epochs/{epoch}/rounds/{round}/round_log.jsonl``. Each wire record has a
type-specific ``payload`` plus an extensible ``scope`` envelope for plan
coordinates; adding a coordinate does not require changing every event's
payload schema.

plus the fold that reduces it to a typed :class:`RoundRecord` summary.
The orchestrator's ``_RoundLogEmitter`` wires this on the evolve path:
every settled round writes its ``round_log.jsonl`` as the round runs.

Load-bearing invariants
-----------------------
* **Append-only, single-writer.** Exactly one process (the orchestrator
  driving the round) appends to a given round's log; each append is one
  complete ``\\n``-terminated compact-JSON line, so a concurrent reader
  observes a prefix of events, never a partial record. The monotonic
  ``seq`` (first event ``1``, every append exactly ``+1``) is derived
  from the current tail under that single-writer contract — the same
  discipline as :class:`zicato.runtime.channel.EventLog` and the runtime
  tournament log.
* **Torn-tail tolerance.** A crash mid-append can leave one torn final
  line. The reader SKIPS an unparseable last line (the reducer's
  discipline for a run's ``events.jsonl``); every earlier line is
  covered by the append-only invariant, so an unparseable INTERIOR line
  means something bypassed the writer and raises rather than silently
  dropping history. The writer repairs a torn tail before appending
  (terminates the partial line) so the dead bytes can never concatenate
  with the next event.
* **Durable, not runtime.** The log lives under ``epochs/`` — the
  store-of-record tree — not ``runtime/``: it survives the run, is
  keyed by the round it describes, and is never cleared by resume/crash
  cleanup.

Event vocabulary
----------------
One frozen dataclass per event type (see :data:`EVENT_TYPES`), covering
the round's full arc: open (contract hash) → proposal session (attempts,
sampled candidates, critique selection, minted experiment) → apply/
validate → harness load provenance → tournament units → gate/holdout/
evidence → frontier record → recorded decision → close. Unknown event types read back as
raw envelopes (typed payload ``None``) so a newer writer's log still
folds on an older reader.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, ClassVar

from zicato.util.iso_time import now_iso as _now_iso

# ---------------------------------------------------------------------------
# Path convention — epochs/{epoch}/rounds/{round}/round_log.jsonl
# ---------------------------------------------------------------------------

#: Basename of the per-round event log file.
ROUND_LOG_FILENAME = "round_log.jsonl"


def rounds_dir(workspace_root: Path, epoch_id: str) -> Path:
    """Return ``epochs/{epoch}/rounds/`` for a workspace (pure path math)."""
    return workspace_root / "epochs" / epoch_id / "rounds"


def round_dir(workspace_root: Path, epoch_id: str, round_index: int) -> Path:
    """Return one round's directory, ``epochs/{epoch}/rounds/{round}/``.

    ``round_index`` is the epoch-cumulative evolve round number — the
    same axis ``Generation.round_index`` / the health reports'
    ``round_{n}.json`` use — rendered as its plain decimal string.
    """
    return rounds_dir(workspace_root, epoch_id) / str(int(round_index))


def round_log_path(workspace_root: Path, epoch_id: str, round_index: int) -> Path:
    """Return the path to one round's ``round_log.jsonl``."""
    return round_dir(workspace_root, epoch_id, round_index) / ROUND_LOG_FILENAME


# ---------------------------------------------------------------------------
# Event types — one frozen dataclass per round-lifecycle transition.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoundOpened:
    """The round started under a frozen evaluation contract."""

    TYPE: ClassVar[str] = "round_opened"
    contract_hash: str = ""


@dataclass(frozen=True, slots=True)
class ProposalAttempted:
    """One proposer attempt settled; ``errors`` is empty on success.

    ``slot_index`` names the best-of-N SLATE SLOT the attempt belongs to,
    and exists because a slate slot that fails while a sibling survives is
    an attempt the round log never used to record at all (issue #141): the
    wrapper narrowed the slate and dropped the error, so a round could lose
    most of its slate to a credential lapse and leave ``proposal.errors``
    empty. ``None`` is the NON-SLATE attempt — the single-propose retry
    trail :mod:`zicato.evolve.propose_apply` emits, which has no slot to
    name. Additive with a default so every pre-slot log decodes identically
    (the ``CandidateSampled.revise`` precedent).
    """

    TYPE: ClassVar[str] = "proposal_attempted"
    errors: tuple[str, ...] = ()
    slot_index: int | None = None


@dataclass(frozen=True, slots=True)
class CandidateSampled:
    """Best-of-N sampling drew candidate ``i`` of ``n``.

    ``revise`` marks the ONE bounded screen-informed revise re-sample an
    all-vetoed slate may take (``i`` is then the replacement's slate
    position, one past the sampled slots). Additive with a default so
    every pre-revise log decodes identically.

    ``recombined`` marks the slot that MECHANICALLY MINTED the union of two
    rejected parents' patch sets (WS-REC) instead of sampling the LLM — the
    last slot when the round carries a recombination pair. Additive with a
    default so every pre-recombine log decodes identically.

    Its challenger and other plan coordinates live in the enclosing
    :class:`RoundEventScope` rather than in this event-specific payload.
    """

    TYPE: ClassVar[str] = "candidate_sampled"
    i: int = 0
    n: int = 1
    revise: bool = False
    recombined: bool = False


@dataclass(frozen=True, slots=True)
class CandidateScreened:
    """The pre-tournament screen settled slate candidate ``index``.

    Emitted once per candidate after the ``candidate_sampled`` events and
    before ``critique_selected`` (the screen runs between the slate
    settling and the selection). ``screen_summary`` carries the
    counts-only measurement block (panel size, baseline passes, candidate
    passes, the counts-only reason string) — NEVER an entry id.
    ``confirmed`` is true only for a veto that survived the
    confirm-before-veto re-run (a twice-flipped entry); an immediate
    budget-abort veto carries ``False``. ``revise`` marks the screen of
    the ONE bounded revise replacement an all-vetoed slate may sample
    (``index`` is then one past the original slate) — additive with a
    default so every pre-revise log decodes identically.

    Its challenger and other plan coordinates live in the enclosing
    :class:`RoundEventScope` rather than in this event-specific payload.
    """

    TYPE: ClassVar[str] = "candidate_screened"
    index: int = 0
    vetoed: bool = False
    confirmed: bool = False
    screen_summary: dict[str, Any] = field(default_factory=dict)
    revise: bool = False


@dataclass(frozen=True, slots=True)
class CritiqueSelected:
    """The self-critique pass picked candidate ``index`` for ``reason``.

    Its challenger and other plan coordinates live in the enclosing
    :class:`RoundEventScope` rather than in this event-specific payload.
    """

    TYPE: ClassVar[str] = "critique_selected"
    index: int = 0
    reason: str = ""
    slate: tuple[dict[str, Any], ...] = ()
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class ExperimentMinted:
    """A schema-valid experiment was minted for this round."""

    TYPE: ClassVar[str] = "experiment_minted"
    experiment_id: str = ""


@dataclass(frozen=True, slots=True)
class PatchesApplied:
    """The experiment's patches were applied into a fresh generation."""

    TYPE: ClassVar[str] = "patches_applied"
    generation_id: str = ""


@dataclass(frozen=True, slots=True)
class HarnessLoaded:
    """WHAT a generation's harness actually loaded from its snapshot.

    The mutated-tree provenance (issue #110). ``entrypoint_file`` is the
    SNAPSHOT-RELATIVE path (``agent/agent.py``) of the ``module.__file__`` the
    adapter imported for ``generation_id``, after asserting it lies under that
    generation's snapshot. Relative, not absolute, because the snapshot a
    worker loads is a per-run ephemeral checkout that is deleted when the run
    ends — the durable, comparable fact is WHICH module inside the snapshot
    ran, not where the throwaway copy of it lived. Empty for the dependency
    shape, where the entrypoint legitimately lives outside every mutable tree
    (target 2 mutates goldfive and drives it from a harness module elsewhere).

    ``trees_verified`` / ``trees_never_imported`` carry the per-tree half — the
    one that answers "were the MUTATIONS under test?" rather than "where did
    the entrypoint come from?". A verified tree was imported from under the
    generation's snapshot by at least one of its units; a never-imported tree
    was touched by none of them, which means its mutations cannot have been
    exercised (a tree imported from OUTSIDE the snapshot never reaches this
    event — it fails its unit).

    Emitted at most once per generation per round (the champion and the
    challenger each get one), and only for an adapter that reports something —
    an adapter kind that does not, or a generation whose units all came from
    the unit cache, simply contributes no event. Purely additive provenance:
    readers MUST tolerate its absence, unknown tokens are ignored by the fold,
    and every field defaults, so every pre-existing log decodes unchanged.
    """

    TYPE: ClassVar[str] = "harness_loaded"
    generation_id: str = ""
    entrypoint_file: str = ""
    trees_verified: tuple[str, ...] = ()
    trees_never_imported: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationFailed:
    """Snapshot validation rejected the applied patches."""

    TYPE: ClassVar[str] = "validation_failed"
    findings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UnitCompleted:
    """One board unit ``(entry, replicate, side)`` settled."""

    TYPE: ClassVar[str] = "unit_completed"
    entry_id: str = ""
    replicate: int = 0
    side: str = ""


@dataclass(frozen=True, slots=True)
class GateEvaluated:
    """The promote gate fired ``rule_fired`` and returned ``decision``.

    ``champion_scalar`` / ``challenger_scalar`` / ``margin_required`` are the
    inputs to the gate's CONTINUOUS decision axis — Rule 1 is literally
    ``challenger_scalar > champion_scalar - margin_required`` ⇒ reject — recorded
    on BOTH decisions so the duel's effect size is reconstructable from the log
    alone.

    Before they existed the compared scalars survived only inside the
    human-readable REJECT text (``rule_fired``, which is empty on a clean
    promote — unchanged by this addition). A promoted duel therefore recorded no
    numbers at all, and that gap is not merely missing data: it is CORRELATED
    with the quantity being measured. A sample recovered from the log is missing
    exactly its promotions, which are by definition the largest improvements, so
    comparing configurations biases the ranking toward whichever one promotes
    least — close to the opposite of what the analysis is looking for. Nothing in
    the output signals it; the per-arm sample sizes still look plausible.

    Division of labour: these three fields are the CONTRACT for anything a
    consumer computes on; ``rule_fired`` names which rule actually decided and is
    PRESENTATION. Its phrasing varies by rule (``insufficient improvement: ...``,
    ``challenger regressed: ...``, ``pass-rate regression on entries: ...``,
    ``diff_complexity_ceiling: ...``), so nothing should be regexed out of it —
    and it is empty whenever the gate promotes, which is why the numbers had to
    move somewhere structural rather than into the prose.

    Rules 2 and 3 (pass-rate and per-namespace monotonicity) decide on per-entry
    and per-namespace maps that would not fit an event payload; ``rule_fired``
    names them when they fire, and the aggregates themselves live in the
    generations' ``gen_score.json``.

    Additive with ``None`` — NOT ``0.0`` — defaults: a scalar of ``0.0`` is a
    legal measurement, so a numeric default would make "this log predates the
    fields" indistinguishable from "both sides scored zero", reintroducing the
    same ambiguity one layer down. Every pre-existing log decodes with all three
    ``None`` (:func:`_decode_event` defaults absent keys), following the
    ``revise: bool = False`` precedent above.

    ``attributable_regressions`` names the entries that regressed on their own
    per-entry evidence, on BOTH decisions — the observation the gate makes but
    never acts on (see
    :func:`zicato.tournament.gate.attributable_entry_regressions`). A PROMOTED
    duel carrying entries here is the case worth reading: the loss is now in the
    lineage and ``rule_fired`` is, correctly, empty. Emitted only when non-empty,
    so an ordinary duel's payload is unchanged; ``()`` here is "none reported",
    which for this field is the same statement as "not recorded" — it changes no
    analysis, so the empty tuple default is safe where a ``0.0`` scalar was not.
    """

    TYPE: ClassVar[str] = "gate_evaluated"
    rule_fired: str = ""
    decision: str = ""
    champion_scalar: float | None = None
    challenger_scalar: float | None = None
    margin_required: float | None = None
    attributable_regressions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HoldoutReleased:
    """The Ladder released the holdout-confirmation bit for this round."""

    TYPE: ClassVar[str] = "holdout_released"
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class EvidenceReplicated:
    """One evidence-gate refit after a replicate duel; ``ci_state`` is the
    ``{p_stronger, ci_overlap, replicates_spent}`` trace row."""

    TYPE: ClassVar[str] = "evidence_replicated"
    ci_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DecisionRecorded:
    """The round's terminal decision plus its provenance block."""

    TYPE: ClassVar[str] = "decision_recorded"
    decision: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FrontierUpdated:
    """The epoch's Pareto frontier record changed membership this round.

    Emitted ONLY when something moved — a round that admits and retires
    nothing is silent, so the presence of this event means the record has
    news. ``size`` is the member count AFTER the update. Purely a record of
    an observation: the frontier never touches promotion, selection, or the
    proposer (``docs/design/PARETO-FRONTIER.md``).
    """

    TYPE: ClassVar[str] = "frontier_updated"
    admitted: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()
    size: int = 0


@dataclass(frozen=True, slots=True)
class RoundClosed:
    """The round closed; the log is complete."""

    TYPE: ClassVar[str] = "round_closed"


#: The closed event vocabulary, keyed by wire type token. The writer and
#: the fold agree on these; an unknown token in a log reads back as a raw
#: envelope (typed ``event`` ``None``) rather than failing the fold.
EVENT_TYPES: dict[str, type] = {
    cls.TYPE: cls
    for cls in (
        RoundOpened,
        ProposalAttempted,
        CandidateSampled,
        CandidateScreened,
        CritiqueSelected,
        ExperimentMinted,
        PatchesApplied,
        HarnessLoaded,
        ValidationFailed,
        UnitCompleted,
        GateEvaluated,
        HoldoutReleased,
        EvidenceReplicated,
        DecisionRecorded,
        FrontierUpdated,
        RoundClosed,
    )
}

#: Union alias for a typed round-log event (any of :data:`EVENT_TYPES`).
RoundEvent = (
    RoundOpened
    | ProposalAttempted
    | CandidateSampled
    | CandidateScreened
    | CritiqueSelected
    | ExperimentMinted
    | PatchesApplied
    | HarnessLoaded
    | ValidationFailed
    | UnitCompleted
    | GateEvaluated
    | HoldoutReleased
    | EvidenceReplicated
    | DecisionRecorded
    | FrontierUpdated
    | RoundClosed
)


@dataclass(frozen=True, slots=True)
class RoundEventScope:
    """Stable plan coordinates that enclose, rather than alter, an event.

    A round log is a shared event stream: field rounds can produce several
    challengers, and a single challenger can produce several board units. The
    event ``type`` describes *what happened* and its payload records the
    type-specific fact; this object describes *where that fact belongs* in the
    round plan. Keeping those concerns separate lets a reader group events
    without guessing from their position in the JSONL file.

    The scope names the challenger on EVERY event a single challenger owns,
    including the events whose payload already states it (``patches_applied``,
    ``harness_loaded``). The repetition is deliberate. A reader that groups by
    challenger must be able to read one place on every record; if the scope
    were left empty wherever the payload happened to carry the id, every such
    reader would need a per-event-type table saying where to look — the
    guessing this envelope exists to remove. The two values cannot disagree:
    each call site writes both from one variable.

    An event no single challenger owns leaves the coordinate empty rather than
    inventing one: the round's own boundaries, its terminal decision, and its
    frontier record belong to the round.

    Two coordinates are named fields, because two have writers today:

    * ``generation_id`` — the challenger the event belongs to.
    * ``step`` — the execution plan's lifecycle step: propose, apply, run,
      gate or decide. The round's own boundaries are not steps within it, so
      ``round_opened`` and ``round_closed`` carry none.

    A coordinate gets a named field when it gets a WRITER, and not before. A
    named field nobody fills is not a neutral placeholder: it is a claim that
    the coordinate's meaning is settled, and the shape of the plan's
    coordinates is what is still open across the remaining node kinds
    (board sweeps, measurement bands, screen units). Naming a field early
    locks that shape the same way a slate ordinal would, so anything an
    interim writer needs rides ``attributes`` — where the duel's
    ``matchup_id`` and ``opponent_generation_id`` already travel — until the
    coordinate is settled across every node kind that must carry it.

    A slate ordinal is the standing example of why. ``candidate_sampled``'s
    ``i`` numbers slate SLOTS while ``candidate_screened`` /
    ``critique_selected`` number the SURVIVORS that reached the screen, so
    those numbers diverge as soon as one slot fails and no single coordinate
    can carry both; settling one is a payload decision rather than an
    envelope one.
    The round is not a coordinate either: a record's round is the
    ``rounds/{round}/round_log.jsonl`` path it was read from, and a copy the
    writer restated could only disagree with it.

    The ``attributes`` map is an explicit extension point for a newer
    emitter's additional coordinates. It is separate from the named fields
    so an extension cannot silently redefine ``generation_id`` or
    another shared coordinate. A reader that predates an extension preserves
    those values in ``attributes`` instead of dropping them, and promotes one
    into its named field once a later reader knows that name. It holds
    COORDINATES ONLY, never content: a scope is subject to the same redaction
    denylist as every other durable record
    (:func:`zicato.proposer.reflection.assert_redacted`), so board text,
    prompts and transcripts must never travel here.

    All fields default to the empty scope. Logs written before scopes existed
    therefore remain readable, but their events cannot be attributed more
    precisely than the legacy record permits.
    """

    generation_id: str = ""
    step: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    #: DELIBERATELY unhashable. ``frozen=True`` would otherwise generate a
    #: ``__hash__`` that raises on the ``attributes`` mapping — clean to a
    #: type checker, a crash at runtime, and no hint of the supported route.
    #: ``None`` states the decision: the type reports as unhashable, and a
    #: reader who needs a set member or a dict key uses :meth:`grouping_key`.
    __hash__ = None  # type: ignore[assignment]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> RoundEventScope:
        """Decode a wire scope, retaining unknown coordinates as attributes."""
        if not isinstance(payload, Mapping):
            return cls()
        known = {f.name for f in fields(cls)}
        attributes = payload.get("attributes", {})
        extras = dict(attributes) if isinstance(attributes, Mapping) else {}
        extras.update({key: value for key, value in payload.items() if key not in known})

        def coordinate(name: str) -> Any:
            """Read one named coordinate, PROMOTING an attributes copy of it.

            A writer that predates a named coordinate puts it in
            ``attributes``, where the extension point sends an unrecognised
            name. Reading the named field alone would leave
            that value unreachable through the field that now names it, so it
            is promoted out — and out of ``extras`` either way, so the decoded
            scope never carries the same coordinate twice.
            """
            demoted = extras.pop(name, None)
            value = payload.get(name)
            return demoted if value is None else value

        return cls(
            # ``or ""`` and not ``str(...)``: a null wire coordinate is an
            # ABSENT coordinate. ``str(None)`` would persist the literal
            # "None" as a generation id, which ``to_payload`` then keeps
            # because it is neither "" nor None.
            generation_id=str(coordinate("generation_id") or ""),
            step=str(coordinate("step") or ""),
            attributes=extras,
        )

    def grouping_key(self) -> str:
        """Return this scope's canonical, hashable grouping key.

        ``attributes`` is open JSON data and can contain nested
        lists or maps, so a scope itself must not become a hash key: its
        mutable mapping would make a set or dict corrupt after mutation. A
        plan reader that needs grouping uses this snapshot instead; canonical
        JSON gives the same key to equivalent wire coordinates regardless of
        mapping insertion order.
        """
        return json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"))

    def to_payload(self) -> dict[str, Any]:
        """Return the compact, forward-compatible JSON shape for this scope."""
        payload: dict[str, Any] = {}
        for name in ("generation_id", "step"):
            value = getattr(self, name)
            if value not in ("", None):
                payload[name] = value
        if self.attributes:
            # A COPY: the scope is frozen, so handing out its live mapping
            # would let a caller mutate one through the payload it was
            # rendered into.
            payload["attributes"] = dict(self.attributes)
        return payload


@dataclass(frozen=True, slots=True)
class RoundLogEnvelope:
    """One decoded log line: the sequenced wire record plus its typed event.

    ``seq`` is the machine ordering key (strictly increasing, gap-free
    under the single-writer contract); ``ts`` is for humans. ``event`` is
    the decoded dataclass for a known ``type``; ``None`` for a token this
    reader does not know (forward compatibility — the raw ``payload`` is
    still carried verbatim). ``scope`` is the stable, type-independent plan
    coordinate envelope. It is empty for legacy records which predate scope.
    """

    seq: int
    ts: str
    type: str
    payload: dict[str, Any]
    event: RoundEvent | None = None
    scope: RoundEventScope = field(default_factory=RoundEventScope)


def _decode_event(type_token: str, payload: dict[str, Any]) -> RoundEvent | None:
    """Decode a payload dict to its typed event, or ``None`` when unknown.

    Extra payload keys are dropped (a newer writer may carry more fields);
    missing keys take the dataclass defaults. JSON round-trips lists as
    lists, so tuple-typed fields are re-tupled here.
    """
    cls = EVENT_TYPES.get(type_token)
    if cls is None:
        return None
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in payload:
            continue
        value = payload[f.name]
        if isinstance(value, list):
            value = tuple(value)
        # Re-tupling is the ONLY coercion, which holds because every field
        # on every event is a str / int / bool / float / tuple[str, ...] /
        # dict, or an OPTIONAL one of those (JSON ``null`` round-trips to
        # ``None``, which is the declared type — no coercion to do). A field
        # declared as a StrEnum or Path would land here as
        # its raw JSON value and read back a different runtime type than
        # the writer emitted (issue #132); coerce it here if one is added.
        kwargs[f.name] = value
    event: RoundEvent = cls(**kwargs)
    return event


# ---------------------------------------------------------------------------
# The log — single-writer append + torn-tail-tolerant read.
# ---------------------------------------------------------------------------


class RoundLog:
    """One round's append-only event log at its canonical path.

    Binding is pure path math — no I/O until :meth:`append` / :meth:`read`.
    The append discipline mirrors the runtime tournament log's
    :class:`~zicato.runtime.channel.EventLog` (one complete compact-JSON
    line per event, ``seq`` derived from the tail under the single-writer
    contract) with one durability addition this store-of-record needs:
    the reader tolerates a torn tail, and the writer terminates one
    before appending so the dead bytes never merge into a new event.
    """

    def __init__(self, workspace_root: Path, epoch_id: str, round_index: int) -> None:
        self._path = round_log_path(workspace_root, epoch_id, round_index)

    @property
    def path(self) -> Path:
        """The log's on-disk JSONL path."""
        return self._path

    def append(
        self, event: RoundEvent, *, scope: RoundEventScope | Mapping[str, Any] | None = None
    ) -> RoundLogEnvelope:
        """Append one typed event; return it with its assigned ``seq`` + ``ts``.

        ``seq`` is the last PARSEABLE event's ``seq`` plus one (``1`` for
        an empty/absent log) — a torn tail contributes nothing, so a
        writer resuming after a crash continues the monotonic sequence.
        Before appending, a file that does not end in a newline (the torn
        tail a crash mid-append leaves) is TRUNCATED back to its last
        complete line: the partial record was never a complete event (its
        append never finished), so dropping it is the honest repair — and
        it can never concatenate with this append or read back later as
        interior corruption.
        """
        # Repair BEFORE deriving the seq, so a torn final line — even one
        # whose partial bytes happen to parse — is dropped first and the
        # sequence continues gap-free from the last durably complete event.
        if self._path.exists() and not self._ends_with_newline():
            self._truncate_torn_tail()
        tail = self.tail()
        seq = 1 if tail is None else tail.seq + 1
        ts = _now_iso()
        payload = asdict(event)
        event_scope = (
            scope if isinstance(scope, RoundEventScope) else RoundEventScope.from_payload(scope)
        )
        record = {
            "seq": seq,
            "ts": ts,
            "type": type(event).TYPE,
            "scope": event_scope.to_payload(),
            "payload": payload,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":"))
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return RoundLogEnvelope(
            seq=seq,
            ts=ts,
            type=type(event).TYPE,
            payload=payload,
            scope=event_scope,
            event=event,
        )

    def read(self) -> list[RoundLogEnvelope]:
        """Return every decoded event in append order, tolerating a torn tail.

        An unparseable LAST line is skipped (a crash mid-append); an
        unparseable INTERIOR line raises :class:`ValueError` — under the
        append-only single-writer invariant only the tail can be torn, so
        interior corruption means something bypassed the writer and must
        surface rather than silently dropping history. An absent file is
        an empty log.
        """
        if not self._path.exists():
            return []
        raw_lines = self._path.read_text(encoding="utf-8").splitlines()
        lines = [(i, line.strip()) for i, line in enumerate(raw_lines) if line.strip()]
        out: list[RoundLogEnvelope] = []
        for pos, (line_no, line) in enumerate(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if pos == len(lines) - 1:
                    continue  # torn tail — skip, like the telemetry reducer
                raise ValueError(
                    f"round log {self._path} line {line_no + 1} is corrupt "
                    "(not the tail — the append-only invariant was violated)"
                ) from None
            payload = record.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            scope = RoundEventScope.from_payload(record.get("scope"))
            type_token = str(record.get("type", ""))
            out.append(
                RoundLogEnvelope(
                    seq=int(record.get("seq", 0)),
                    ts=str(record.get("ts", "")),
                    type=type_token,
                    payload=payload,
                    scope=scope,
                    event=_decode_event(type_token, payload),
                )
            )
        return out

    def tail(self) -> RoundLogEnvelope | None:
        """The last parseable event, or ``None`` for an empty/absent log."""
        events = self.read()
        return events[-1] if events else None

    def _ends_with_newline(self) -> bool:
        """True when the existing log's final byte is ``\\n`` (or it is empty)."""
        try:
            with self._path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                if size == 0:
                    return True
                fh.seek(size - 1)
                return fh.read(1) == b"\n"
        except OSError:
            return True

    def _truncate_torn_tail(self) -> None:
        """Drop the incomplete final line a crash mid-append left behind.

        Truncates the file back to just past its last ``\\n`` (to empty
        when no complete line exists). Only ever called by :meth:`append`
        under the single-writer contract, so no reader can observe a
        mid-truncate state that a subsequent append does not immediately
        repair.
        """
        data = self._path.read_bytes()
        cut = data.rfind(b"\n") + 1
        with self._path.open("rb+") as fh:
            fh.truncate(cut)


# ---------------------------------------------------------------------------
# Fold — reduce the event stream to a typed round summary.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposalSession:
    """The round's proposal-session summary, folded from the log.

    ``candidates_screened`` / ``screen_vetoes`` tally the pre-tournament
    candidate screen (one ``candidate_screened`` event per slate
    candidate); both stay ``0`` for a round whose contract does not opt
    into screening.
    """

    attempts: int = 0
    errors: tuple[str, ...] = ()
    candidates_sampled: int = 0
    candidates_screened: int = 0
    screen_vetoes: int = 0
    recombined_sampled: int = 0
    critique_index: int | None = None
    critique_reason: str = ""
    experiment_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """The typed summary of one round's log — the fold's output.

    A skeleton by design (WS8-1 is schema + fold; emission wiring and any
    richer per-matchup reconstruction come later): it reduces the event
    stream to the round's arc — proposal session, applied generations,
    validation findings, the matchup units that ran, the gate/holdout/
    evidence trail, and the recorded decision with its provenance.
    ``complete`` is true only for a log that both opened and closed.
    """

    opened: bool = False
    closed: bool = False
    contract_hash: str = ""
    proposal: ProposalSession = field(default_factory=ProposalSession)
    generation_ids: tuple[str, ...] = ()
    #: Per-generation snapshot-origin provenance folded from the
    #: ``harness_loaded`` events: ``{generation_id: snapshot-relative
    #: entrypoint path}``.
    #: Additive — empty for every log written before the event existed, for
    #: a non-reporting adapter kind, and for a fully cache-served round.
    harness_entrypoint_files: dict[str, str] = field(default_factory=dict)
    #: Per-generation mutable trees NO unit of that generation ever imported,
    #: folded from the same events: ``{generation_id: (tree_basename, ...)}``.
    #: A non-empty entry means that generation's mutations to those trees
    #: cannot have been under test (issue #110's original shape) — the
    #: loop-health check turns it into a WARNING finding. Additive and
    #: normally empty.
    harness_never_imported_trees: dict[str, tuple[str, ...]] = field(default_factory=dict)
    validation_findings: tuple[str, ...] = ()
    units: tuple[UnitCompleted, ...] = ()
    gates: tuple[GateEvaluated, ...] = ()
    holdout: HoldoutReleased | None = None
    evidence_trail: tuple[dict[str, Any], ...] = ()
    #: The rounds' Pareto-frontier movements, in emission order. Empty for
    #: every round that moved nothing and for every log written before the
    #: event existed — the record is an observation, never a decision, so a
    #: reader that ignores this field reads the round exactly as before.
    frontier_updates: tuple[FrontierUpdated, ...] = ()
    decision: str = ""
    decision_provenance: dict[str, Any] = field(default_factory=dict)
    last_seq: int = 0

    @property
    def complete(self) -> bool:
        """True when the round both opened and closed in this log."""
        return self.opened and self.closed


def fold_round_record(events: list[RoundLogEnvelope]) -> RoundRecord:
    """Fold a round's decoded event stream into a :class:`RoundRecord`.

    Pure and total over any prefix of a valid log: a mid-round crash
    leaves a foldable partial record (``closed`` false), and unknown
    event types (``event is None``) are ignored rather than failing the
    fold. Later events win where a field is single-valued (e.g. a second
    ``decision_recorded`` overwrites the first — the last word is the
    record), while trail-shaped fields accumulate in order.
    """
    opened = False
    closed = False
    contract_hash = ""
    attempts = 0
    errors: list[str] = []
    candidates = 0
    screened = 0
    screen_vetoes = 0
    recombined_sampled = 0
    critique_index: int | None = None
    critique_reason = ""
    experiment_ids: list[str] = []
    generation_ids: list[str] = []
    entrypoint_files: dict[str, str] = {}
    never_imported_trees: dict[str, tuple[str, ...]] = {}
    findings: list[str] = []
    units: list[UnitCompleted] = []
    gates: list[GateEvaluated] = []
    holdout: HoldoutReleased | None = None
    evidence_trail: list[dict[str, Any]] = []
    frontier_updates: list[FrontierUpdated] = []
    decision = ""
    provenance: dict[str, Any] = {}
    last_seq = 0

    for envelope in events:
        last_seq = max(last_seq, envelope.seq)
        event = envelope.event
        if event is None:
            continue
        if isinstance(event, RoundOpened):
            opened = True
            contract_hash = event.contract_hash
        elif isinstance(event, ProposalAttempted):
            attempts += 1
            errors.extend(event.errors)
        elif isinstance(event, CandidateSampled):
            candidates += 1
            if event.recombined:
                recombined_sampled += 1
        elif isinstance(event, CandidateScreened):
            screened += 1
            if event.vetoed:
                screen_vetoes += 1
        elif isinstance(event, CritiqueSelected):
            critique_index = event.index
            critique_reason = event.reason
        elif isinstance(event, ExperimentMinted):
            experiment_ids.append(event.experiment_id)
        elif isinstance(event, PatchesApplied):
            generation_ids.append(event.generation_id)
        elif isinstance(event, HarnessLoaded):
            # Last word wins per generation: a re-load inside the same round
            # (a replicate duel) reports the same file and the same accumulated
            # tree verdicts, so both maps are stable.
            entrypoint_files[event.generation_id] = event.entrypoint_file
            if event.trees_never_imported:
                never_imported_trees[event.generation_id] = tuple(event.trees_never_imported)
            else:
                never_imported_trees.pop(event.generation_id, None)
        elif isinstance(event, ValidationFailed):
            findings.extend(event.findings)
        elif isinstance(event, UnitCompleted):
            units.append(event)
        elif isinstance(event, GateEvaluated):
            gates.append(event)
        elif isinstance(event, HoldoutReleased):
            holdout = event
        elif isinstance(event, EvidenceReplicated):
            evidence_trail.append(dict(event.ci_state))
        elif isinstance(event, FrontierUpdated):
            frontier_updates.append(event)
        elif isinstance(event, DecisionRecorded):
            decision = event.decision
            provenance = dict(event.provenance)
        elif isinstance(event, RoundClosed):
            closed = True

    return RoundRecord(
        opened=opened,
        closed=closed,
        contract_hash=contract_hash,
        proposal=ProposalSession(
            attempts=attempts,
            errors=tuple(errors),
            candidates_sampled=candidates,
            candidates_screened=screened,
            screen_vetoes=screen_vetoes,
            recombined_sampled=recombined_sampled,
            critique_index=critique_index,
            critique_reason=critique_reason,
            experiment_ids=tuple(experiment_ids),
        ),
        generation_ids=tuple(generation_ids),
        harness_entrypoint_files=dict(entrypoint_files),
        harness_never_imported_trees=dict(never_imported_trees),
        validation_findings=tuple(findings),
        units=tuple(units),
        gates=tuple(gates),
        holdout=holdout,
        evidence_trail=tuple(evidence_trail),
        frontier_updates=tuple(frontier_updates),
        decision=decision,
        decision_provenance=provenance,
        last_seq=last_seq,
    )


__all__ = [
    "ROUND_LOG_FILENAME",
    "rounds_dir",
    "round_dir",
    "round_log_path",
    "RoundOpened",
    "ProposalAttempted",
    "CandidateSampled",
    "CandidateScreened",
    "CritiqueSelected",
    "ExperimentMinted",
    "PatchesApplied",
    "HarnessLoaded",
    "ValidationFailed",
    "UnitCompleted",
    "GateEvaluated",
    "HoldoutReleased",
    "EvidenceReplicated",
    "FrontierUpdated",
    "DecisionRecorded",
    "RoundClosed",
    "EVENT_TYPES",
    "RoundEvent",
    "RoundEventScope",
    "RoundLogEnvelope",
    "RoundLog",
    "ProposalSession",
    "RoundRecord",
    "fold_round_record",
]
