"""Foundational dataclasses for zicato.

These types are the contract every other zicato module imports. They are
frozen dataclasses with explicit field types so that downstream code —
adapters, the runner, the proposer, the tournament, the persistence
layer, pattern detectors, the CLI — can rely on a stable surface while
their internals evolve independently.

Design rules encoded here:

* **Frozen** — every dataclass uses ``frozen=True, slots=True``. State
  transitions construct new instances via :func:`dataclasses.replace`;
  callers who captured a reference keep operating on their snapshot.
* **JSON-friendly enums** — discriminant fields are either
  :class:`typing.Literal` strings or string-valued :class:`enum.Enum`
  members. The enums in this module (:class:`OutputScope`,
  :class:`ExpectationKind`, :class:`JudgeMode`) subclass ``str``, so a
  member compares equal to its wire string and ``json.dumps`` emits the
  bare string with no converter. The board-authoring API
  (:mod:`zicato.board`) hands operators those enum members directly so
  there are no magic strings at any call site; the on-disk JSONL stays a
  plain string token. Goldfive's own :class:`~goldfive.DriftKind` /
  :class:`~goldfive.DriftSeverity` follow the same string-enum shape and
  are reused verbatim where a board entry needs a drift coordinate.
* **Discriminated unions for board entries** — :class:`BoardEntry` carries
  every kind's discriminant fields as optional attributes; the
  :meth:`BoardEntry.validate` method (and the free function
  :func:`validate_board_entry`) enforce that the right combination is
  present for the declared ``kind``.
* **Model-agnostic LLM surface** — the only callable shape this module
  references is ``Callable[[str, str, str], Awaitable[str]]``
  (``(system, user, model) -> response``). No vendor SDK is named.
* **Open-ended kind strings where forward-compat matters** —
  :class:`BoardEntry`'s ``kind`` field is a closed :class:`Literal` for
  the v0 surface, but :class:`Pattern.kind` and the drift-kind strings
  on :class:`DriftCount` / :class:`ExpectedDriftMovement` /
  :class:`DriftMovementActual` are bare ``str`` validated against a
  registered set (see :mod:`zicato.core.drift_kinds`). This is the
  forward-compatible posture required by the dogfood-target plan.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Literal

from goldfive import DriftSeverity

from zicato.core.drift_kinds import validate_drift_kind

# ---------------------------------------------------------------------------
# Mutation surface
# ---------------------------------------------------------------------------

#: Granularity of a mutable region in the inner-harness source tree.
#:
#: * ``"span"`` — a single annotated span (typically a string literal or
#:   string-valued statement) immediately preceded by a marker comment.
#:   Span granularity is the default; it gives the proposer typed targets
#:   without exposing surrounding control flow.
#: * ``"file"`` — a whole file declared mutable as one unit via a
#:   top-of-file marker. Intended for prompt modules whose strings are
#:   tightly coupled. Validator constraints (imports survive, syntax
#:   parses) bound what a file-level rewrite can do.
#: * ``"code"`` — a pointed code region delimited by a
#:   ``# zicato:mutable:code`` opening marker and a
#:   ``# zicato:mutable:end`` closing sentinel. The content is the
#:   verbatim source lines BETWEEN the two markers (control flow, not a
#:   string literal). Unlike ``"file"`` it exposes only the annotated
#:   block — the surface needed to rewrite a tool's slugify / path
#:   logic without handing the proposer the whole module. The applier
#:   replaces the region body verbatim; the validator's post-apply
#:   syntax + import checks bound what a code-region rewrite can do.
MutationKind = Literal["span", "file", "code"]


@dataclass(frozen=True, slots=True)
class MutationPoint:
    """An annotated mutable region in an inner-harness source tree.

    Mutation points are enumerated by a ``HarnessAdapter`` and addressed
    by stable :attr:`id` from :class:`Patch` instances. The id MUST be
    stable across generations so a proposer can re-target the same span
    after a previous generation rewrote its neighborhood; the contract
    is "same logical mutable region → same id".

    Fields
    ------
    id:
        Globally unique mutation-point identifier within a generation.
        Stable across generations — adapters compute ids from a hash of
        the marker's structural position, not from the line range, so
        unrelated edits to other parts of the file do not invalidate it.
    kind:
        Granularity of the region (see :data:`MutationKind`).
    file:
        Absolute path to the source file the region lives in.
    source_root:
        Absolute path to the source-root tree this point lives under.
        A single harness may expose mutable surface across multiple
        source roots (forward-compat for the goldfive-as-target dogfood
        plan); this field disambiguates which root the patch applier
        should resolve relative paths against.
    line_start, line_end:
        1-indexed inclusive line range of the region's CURRENT content.
        Line numbers will drift as patches land; callers MUST re-enumerate
        before applying patches if they cached an older snapshot.
    content:
        Current text of the mutable region — for ``"span"`` kind, the
        span body (without the marker comment); for ``"file"`` kind, the
        whole file contents.
    content_hash:
        Hex-encoded SHA-256 of :attr:`content`. The patch applier checks
        this before applying a patch so a stale proposer round cannot
        clobber an already-rewritten region.
    metadata:
        Adapter-specific structured metadata. Common keys include
        ``"required_placeholders"`` (comma-separated f-string-style
        placeholders the rewritten content must preserve), ``"language"``
        (e.g. ``"text"`` / ``"markdown"`` / ``"python"``), and
        ``"role"`` (e.g. ``"system_prompt"`` / ``"tool_description"``).
        All values are strings to keep the structure JSON-friendly without
        per-key converters.
    """

    id: str
    kind: MutationKind
    file: Path
    source_root: Path
    line_start: int
    line_end: int
    content: str
    content_hash: str
    metadata: Mapping[str, str] = field(default_factory=dict)


#: The operation a :class:`Patch` performs on its target mutation point.
#:
#: * ``"replace"`` — overwrite :attr:`MutationPoint.content` with
#:   :attr:`Patch.new_content`. The most general op; works on both span
#:   and file mutation kinds.
#: * ``"set_numeric"`` — replace the target with the decimal rendering
#:   of :attr:`Patch.new_numeric`. Used when an adapter has typed the
#:   mutation point as numeric (e.g. a threshold, a budget) so the
#:   proposer doesn't need to handle string formatting.
#: * ``"set_enum"`` — replace the target with :attr:`Patch.new_enum`, a
#:   string the adapter has declared belongs to a finite enum (e.g. a
#:   strategy name, a routing key).
PatchOpKind = Literal["replace", "set_numeric", "set_enum"]


@dataclass(frozen=True, slots=True)
class Patch:
    """A single proposed edit to one mutation point.

    Patches are produced by the proposer, bundled into an
    :class:`Experiment`, and consumed by the patch applier. They carry
    a per-patch :attr:`id` (uuid4 hex by convention) so the journal
    can refer to individual patches when an experiment's outcome is
    ambiguous across multiple patches.

    Exactly one of :attr:`new_content`, :attr:`new_numeric`,
    :attr:`new_enum` is populated; which one is implied by :attr:`op`.
    The dataclass does not enforce that invariant — the patch applier
    raises at apply time on a mismatch. This keeps the dataclass cheap
    to construct from JSON dicts in tests and fixtures.

    Fields
    ------
    id:
        Stable per-patch identifier (uuid4 hex by convention).
    mutation_id:
        The :attr:`MutationPoint.id` this patch targets.
    op:
        The kind of edit (see :data:`PatchOpKind`).
    new_content:
        New text for ``"replace"`` ops; ``None`` otherwise.
    new_numeric:
        New numeric value for ``"set_numeric"`` ops; ``None`` otherwise.
        Floats cover both int- and float-typed mutation points; the
        applier formats them according to adapter-supplied metadata.
    new_enum:
        New enum value for ``"set_enum"`` ops; ``None`` otherwise.
    rationale:
        One-sentence reason this specific patch is being applied. Joined
        with the broader :class:`HypothesisSpec` in the journal but stored
        per-patch so multi-patch experiments don't lose granularity.
    """

    id: str
    mutation_id: str
    op: PatchOpKind
    new_content: str | None
    new_numeric: float | None
    new_enum: str | None
    rationale: str


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------

#: The kind of board entry. Closed Literal for the v0 surface, but the
#: set is designed to extend without schema breakage as new harness
#: targets motivate new kinds (the forward-compat slots
#: ``"synthetic_adversarial"`` and ``"synthetic_clean"`` are reserved
#: for the goldfive-as-target dogfood plan).
BoardEntryKind = Literal[
    "single_turn",
    "multi_turn_scripted",
    "multi_turn_emulated",
    # Reserved forward-compat slots — present in the type today so adding
    # them to the runtime doesn't require a schema bump for existing
    # operators. See the dogfood-target plan for context.
    "synthetic_adversarial",
    "synthetic_clean",
]


class ExpectationKind(StrEnum):
    """The matcher behind a board entry's :class:`Expectation`.

    ``Predicate`` + ``Rubric`` are the two OUTCOME-check families — they
    grade a finished run post-hoc. The five members below are the
    matchers those families compile to:

    * :attr:`EXPECTED_TEXT` — exact substring compared to the run's final
      output (or the full transcript when :attr:`Expectation.reads` is
      :attr:`OutputScope.TRANSCRIPT`).
    * :attr:`REGEX` — pattern compiled with :func:`re.search`.
    * :attr:`JSON_SCHEMA` — JSON Schema document the final output (parsed
      as JSON) must validate against.
    * :attr:`PREDICATE` — dotted path to a Python callable
      ``(run_result) -> bool``.
    * :attr:`RUBRIC` — built-in LLM-as-judge rubric evaluator. ``spec`` is
      a JSON document of the form ``{"rubric": <text>, "threshold":
      <float|null>, "scale": [lo, hi]}``. No operator-supplied dotted
      path — the matcher is provided by :mod:`zicato.board.rubric`.

    The enum subclasses ``str``: a member equals its lowercase wire token
    and serialises through ``json.dumps`` with no converter. The board
    JSONL writer emits the bare token; the loader rejects any token not
    in this enum with a message listing the valid values.
    """

    EXPECTED_TEXT = "expected_text"
    REGEX = "regex"
    JSON_SCHEMA = "json_schema"
    PREDICATE = "predicate"
    RUBRIC = "rubric"


class OutputScope(StrEnum):
    """Which slice of a run an :class:`Expectation` reads.

    Replaces the older free ``fires_on`` string. A :class:`~enum.StrEnum`
    for the same JSON-friendliness as :class:`ExpectationKind`.

    * :attr:`FINAL` — the last assistant turn's user-facing output. The
      single-turn-friendly default.
    * :attr:`TRANSCRIPT` — the full conversation transcript. Used when the
      operator wants to assert on the whole conversation shape for a
      multi-turn entry.
    """

    FINAL = "final_output"
    TRANSCRIPT = "conversation_end"


#: Backwards-compatible alias. The board-authoring vocabulary renamed
#: ``fires_on`` to ``reads`` and the value type from a ``Literal`` to the
#: :class:`OutputScope` enum; this alias keeps the old name importable so
#: downstream modules that referenced :data:`ExpectationFiresOn` by name
#: continue to resolve while integration catches up. New code should use
#: :class:`OutputScope` directly.
ExpectationFiresOn = OutputScope


@dataclass(frozen=True, slots=True)
class Expectation:
    """A pass/fail OUTCOME assertion attached to a board entry.

    Expectations are optional — entries without one are scored on drift
    loss alone. When present, a passing/failing expectation contributes
    to the tournament's pass-rate dimension alongside drift loss (see
    :class:`ScoringWeights`). An :class:`Expectation` is the compiled
    form of a ``Predicate.*`` or ``Rubric.*`` authoring call; it is an
    OUTCOME check (graded post-hoc). PROCESS checks — assertions about
    *how* the run unfolded while it was still running — are carried
    separately by :class:`JudgeSpec`.

    Fields
    ------
    kind:
        The matcher kind (see :class:`ExpectationKind`).
    spec:
        Matcher-specific specifier. For :attr:`ExpectationKind.PREDICATE`
        the dotted import path of the callable. For
        :attr:`ExpectationKind.EXPECTED_TEXT`,
        :attr:`ExpectationKind.REGEX`, and
        :attr:`ExpectationKind.JSON_SCHEMA`, the inline value. For
        :attr:`ExpectationKind.RUBRIC`, the JSON rubric document. Kept as
        a single string field so the discriminated union round-trips
        through JSON without nested objects.
    reads:
        Which slice of the run the expectation is evaluated against (see
        :class:`OutputScope`). Renamed from the former ``fires_on``.
    """

    kind: ExpectationKind
    spec: str
    reads: OutputScope = OutputScope.FINAL


class JudgeMode(StrEnum):
    """How a :class:`JudgeSpec`'s :attr:`~JudgeSpec.body` is interpreted.

    * :attr:`INLINE` — :attr:`JudgeSpec.body` is a natural-language
      criterion the process judge is asked to evaluate the run against.
    * :attr:`PYTHON` — :attr:`JudgeSpec.body` is a dotted import path to
      a Python process-judge callable.

    A :class:`~enum.StrEnum` for the same JSON-friendliness as the other
    board-authoring enums.
    """

    INLINE = "inline"
    PYTHON = "python"


@dataclass(frozen=True, slots=True)
class JudgeSpec:
    """A PROCESS check evaluated *while a run is in flight*.

    Where an :class:`Expectation` is an OUTCOME check graded after the
    run finishes, a :class:`JudgeSpec` is a PROCESS check: it observes
    how the run unfolds and surfaces its verdict as a goldfive judge
    signal. The :attr:`name` is mandatory and becomes goldfive's
    ``judge_name``, so it must be a stable slug-like identifier.

    A board entry carries a tuple of :class:`JudgeSpec` (see
    :attr:`BoardEntry.judges`); :class:`ScoringWeights.per_judge_weights`
    is keyed on :attr:`name`.

    Fields
    ------
    name:
        Stable slug-like identifier for the judge. Becomes goldfive's
        ``judge_name``. Validated by the :class:`zicato.board.judges.Judge`
        authoring helpers — lowercase alphanumerics, underscores, and
        hyphens only.
    mode:
        How :attr:`body` is interpreted (see :class:`JudgeMode`).
    body:
        The natural-language criterion when :attr:`mode` is
        :attr:`JudgeMode.INLINE`; the dotted import path of a Python
        process-judge callable when :attr:`mode` is
        :attr:`JudgeMode.PYTHON`.
    severity:
        Goldfive drift severity the judge's adverse verdict is reported
        at. Reuses :class:`goldfive.DriftSeverity` verbatim.
    """

    name: str
    mode: JudgeMode
    body: str
    severity: DriftSeverity


@dataclass(frozen=True, slots=True)
class UserPersona:
    """Persona spec for a multi-turn emulated entry.

    The persona is the ONLY runtime input — alongside the user-facing
    transcript — that the emulator agent receives. Internal harness
    state, agent reasoning, tool calls, and the entry's expectation are
    all withheld by construction. This is the collusion guard.

    Fields
    ------
    goal:
        What the simulated user is trying to accomplish, phrased from the
        user's point of view.
    constraints:
        What the simulated user will and will not say or do — tone,
        format, willingness to provide details, etc.
    stop_when:
        Plain-language termination condition; the emulator checks each
        turn whether it has been met. Either this fires or
        :attr:`BoardEntry.max_turns` caps the conversation.
    """

    goal: str
    constraints: str
    stop_when: str


@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    """A single scripted user turn for a multi-turn scripted entry.

    Fields
    ------
    user:
        The exact user message to send on this turn. Sent regardless of
        what the agent said on the previous turn — scripted entries are
        deliberately rigid for cheap regression testing.
    """

    user: str


@dataclass(frozen=True, slots=True)
class BoardEntry:
    """One entry in an evaluation board.

    A board is a JSONL file of :class:`BoardEntry` rows; an entry is a
    single executable evaluation under some generation of the inner
    harness. Entries are kind-discriminated: the same dataclass carries
    every kind's discriminant fields as optional attributes, and
    :meth:`validate` (or the free function :func:`validate_board_entry`
    when parsing from JSON) enforces that the right combination is set
    for the declared :attr:`kind`.

    Fields
    ------
    id:
        Stable identifier for the entry. Used as a directory name under
        ``runs/`` in the workspace; MUST be filesystem-safe.
    kind:
        Discriminant (see :data:`BoardEntryKind`).
    wall_clock_budget_seconds:
        Hard ceiling on total run time for the entry. For multi-turn
        kinds the budget covers the whole conversation. Exceeding the
        budget aborts the run and scores it as worst-case.
    weight:
        Relative importance in scoring aggregation. Defaults to 1.0;
        operators can up-weight critical entries to dominate the
        aggregate score.
    tags:
        Operator-supplied labels. Pattern detectors slice by tag (e.g.
        ``"research"``, ``"router"``) to spot kind-specific regressions.
    context:
        Opaque adapter-specific metadata. String-valued for JSON
        cleanliness; adapters parse known keys (e.g. ``"attachments"``,
        ``"session_state"``) and ignore the rest.
    expectation:
        Optional pass/fail OUTCOME assertion. Absent → drift-loss-only
        scoring.
    judges:
        Tuple of PROCESS checks (see :class:`JudgeSpec`) evaluated while
        the run is in flight. Default empty. Independent of
        :attr:`expectation` — an entry may carry any combination of an
        outcome expectation and zero or more process judges.

    Discriminated-union fields (validate which are required by
    :meth:`validate`)
    -----------------------------------------------------------------
    input:
        Single-turn / synthetic-adversarial user message.
    turns:
        Scripted user turns (multi-turn-scripted).
    user_persona:
        Persona spec (multi-turn-emulated).
    max_turns:
        Conversation cap for multi-turn kinds.
    adversarial_agent_spec:
        Dotted path to a known-bad agent for synthetic-adversarial
        entries (target-2 dogfood plan).
    required_drift_kinds:
        Drift kinds that MUST be detected for a synthetic-adversarial
        entry to count as "passing" (the agent is known-bad; the
        steerer's job is to notice).
    """

    id: str
    kind: BoardEntryKind
    wall_clock_budget_seconds: int
    weight: float = 1.0
    tags: tuple[str, ...] = ()
    context: Mapping[str, str] = field(default_factory=dict)
    expectation: Expectation | None = None
    judges: tuple[JudgeSpec, ...] = ()
    # Discriminated-union fields. Exactly which subset is required is a
    # function of :attr:`kind` (see :meth:`validate`).
    input: str | None = None
    turns: tuple[ScriptedTurn, ...] | None = None
    user_persona: UserPersona | None = None
    max_turns: int | None = None
    adversarial_agent_spec: str | None = None
    required_drift_kinds: tuple[str, ...] | None = None

    def validate(self) -> None:
        """Raise :class:`ValueError` if the discriminant fields are wrong.

        Called by :func:`validate_board_entry` after JSON parsing, and
        intended to be called by ``zicato board add`` after operator
        edits. Cheaper than per-kind subclasses while keeping the
        discriminated-union semantics enforceable.
        """
        if self.wall_clock_budget_seconds <= 0:
            raise ValueError(f"BoardEntry {self.id!r}: wall_clock_budget_seconds must be > 0")
        if self.weight < 0:
            raise ValueError(f"BoardEntry {self.id!r}: weight must be >= 0")

        if self.kind == "single_turn":
            if self.input is None:
                raise ValueError(f"BoardEntry {self.id!r}: single_turn requires 'input'")
            if self.turns is not None:
                raise ValueError(f"BoardEntry {self.id!r}: single_turn must not set 'turns'")
            if self.user_persona is not None:
                raise ValueError(f"BoardEntry {self.id!r}: single_turn must not set 'user_persona'")
        elif self.kind == "multi_turn_scripted":
            if self.turns is None or len(self.turns) == 0:
                raise ValueError(
                    f"BoardEntry {self.id!r}: multi_turn_scripted requires non-empty 'turns'"
                )
            if self.max_turns is None or self.max_turns <= 0:
                raise ValueError(
                    f"BoardEntry {self.id!r}: multi_turn_scripted requires 'max_turns' > 0"
                )
            if self.input is not None:
                raise ValueError(
                    f"BoardEntry {self.id!r}: multi_turn_scripted must not set 'input'"
                )
            if self.user_persona is not None:
                raise ValueError(
                    f"BoardEntry {self.id!r}: multi_turn_scripted must not set 'user_persona'"
                )
        elif self.kind == "multi_turn_emulated":
            if self.user_persona is None:
                raise ValueError(
                    f"BoardEntry {self.id!r}: multi_turn_emulated requires 'user_persona'"
                )
            if self.max_turns is None or self.max_turns <= 0:
                raise ValueError(
                    f"BoardEntry {self.id!r}: multi_turn_emulated requires 'max_turns' > 0"
                )
            if self.input is not None:
                raise ValueError(
                    f"BoardEntry {self.id!r}: multi_turn_emulated must not set 'input'"
                )
            if self.turns is not None:
                raise ValueError(
                    f"BoardEntry {self.id!r}: multi_turn_emulated must not set 'turns'"
                )
        elif self.kind == "synthetic_adversarial":
            if self.input is None:
                raise ValueError(f"BoardEntry {self.id!r}: synthetic_adversarial requires 'input'")
            if self.adversarial_agent_spec is None or not self.adversarial_agent_spec:
                raise ValueError(
                    f"BoardEntry {self.id!r}: synthetic_adversarial requires "
                    "'adversarial_agent_spec'"
                )
            if self.required_drift_kinds is None or len(self.required_drift_kinds) == 0:
                raise ValueError(
                    f"BoardEntry {self.id!r}: synthetic_adversarial requires non-empty "
                    "'required_drift_kinds'"
                )
            for kind in self.required_drift_kinds:
                validate_drift_kind(kind)
        elif self.kind == "synthetic_clean":
            if self.input is None:
                raise ValueError(f"BoardEntry {self.id!r}: synthetic_clean requires 'input'")
        else:  # pragma: no cover — Literal-typed; this is belt-and-braces
            raise ValueError(f"BoardEntry {self.id!r}: unknown kind {self.kind!r}")

        if self.expectation is not None:
            # Cross-check the expectation's read scope against the kind.
            if self.kind == "single_turn" and self.expectation.reads == OutputScope.TRANSCRIPT:
                raise ValueError(
                    f"BoardEntry {self.id!r}: single_turn expectation cannot read the "
                    "full transcript (OutputScope.TRANSCRIPT)"
                )

        seen_judge_names: set[str] = set()
        for judge in self.judges:
            if judge.name in seen_judge_names:
                raise ValueError(f"BoardEntry {self.id!r}: duplicate judge name {judge.name!r}")
            seen_judge_names.add(judge.name)


def _coerce_enum(enum_cls: type[Enum], raw: Any, field_name: str) -> Any:
    """Coerce ``raw`` into a member of ``enum_cls`` or raise a clear error.

    Used by :func:`validate_board_entry` for every enum-valued field so a
    malformed board file fails with a message listing the valid tokens
    rather than silently constructing an out-of-domain value.
    """
    try:
        return enum_cls(raw)
    except ValueError:
        valid = ", ".join(repr(m.value) for m in enum_cls)
        raise ValueError(f"invalid {field_name} {raw!r}; valid values are: {valid}") from None


def validate_board_entry(d: Mapping[str, Any]) -> BoardEntry:
    """Parse a JSON-shaped dict into a validated :class:`BoardEntry`.

    Used by the board loader on JSONL rows and by ``zicato board add`` on
    operator-authored entries. Performs:

    1. Field extraction from the dict (with sensible coercions: lists to
       tuples for ``tags`` / ``turns`` / ``required_drift_kinds`` /
       ``judges``).
    2. Construction of nested :class:`Expectation`, :class:`JudgeSpec`,
       :class:`UserPersona`, :class:`ScriptedTurn` objects from sub-dicts.
    3. Coercion of every enum-valued token (``kind``, ``reads``, judge
       ``mode`` / ``severity``) into its enum member, rejecting unknown
       tokens with a message listing the valid values.
    4. Discriminant-validation via :meth:`BoardEntry.validate`.

    Raises :class:`ValueError` on any structural problem.
    """

    expectation_dict = d.get("expectation")
    expectation: Expectation | None
    if expectation_dict is None:
        expectation = None
    else:
        # ``reads`` is the current key; ``fires_on`` is the pre-rename
        # name. Accept both on input so a board mid-migration still
        # loads, preferring the new key when both are present.
        raw_reads = expectation_dict.get("reads", expectation_dict.get("fires_on", "final_output"))
        expectation = Expectation(
            kind=_coerce_enum(ExpectationKind, expectation_dict["kind"], "expectation kind"),
            spec=expectation_dict["spec"],
            reads=_coerce_enum(OutputScope, raw_reads, "expectation reads"),
        )

    raw_judges = d.get("judges")
    judges: tuple[JudgeSpec, ...]
    if raw_judges is None:
        judges = ()
    else:
        judges = tuple(
            JudgeSpec(
                name=j["name"],
                mode=_coerce_enum(JudgeMode, j["mode"], "judge mode"),
                body=j["body"],
                severity=_coerce_enum(DriftSeverity, j["severity"], "judge severity"),
            )
            for j in raw_judges
        )

    persona_dict = d.get("user_persona")
    user_persona: UserPersona | None
    if persona_dict is None:
        user_persona = None
    else:
        user_persona = UserPersona(
            goal=persona_dict["goal"],
            constraints=persona_dict["constraints"],
            stop_when=persona_dict["stop_when"],
        )

    raw_turns = d.get("turns")
    turns: tuple[ScriptedTurn, ...] | None
    if raw_turns is None:
        turns = None
    else:
        turns = tuple(ScriptedTurn(user=t["user"]) for t in raw_turns)

    raw_required_drift = d.get("required_drift_kinds")
    required_drift_kinds: tuple[str, ...] | None
    if raw_required_drift is None:
        required_drift_kinds = None
    else:
        required_drift_kinds = tuple(raw_required_drift)

    entry = BoardEntry(
        id=d["id"],
        kind=d["kind"],
        wall_clock_budget_seconds=int(d["wall_clock_budget_seconds"]),
        weight=float(d.get("weight", 1.0)),
        tags=tuple(d.get("tags", ())),
        context=dict(d.get("context", {})),
        expectation=expectation,
        judges=judges,
        input=d.get("input"),
        turns=turns,
        user_persona=user_persona,
        max_turns=d.get("max_turns"),
        adversarial_agent_spec=d.get("adversarial_agent_spec"),
        required_drift_kinds=required_drift_kinds,
    )
    entry.validate()
    return entry


# ---------------------------------------------------------------------------
# Telemetry / loss
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DriftCount:
    """A count of drift events of one (kind, severity) pair within one run.

    Produced by the post-run reducer from a run's events JSONL. Multiple
    :class:`DriftCount` entries with the same :attr:`kind` but different
    :attr:`severity` may appear in one :class:`LossProfile` — the
    INTENT_DIVERGENCE kind fires at variable severity by design and the
    reducer keeps the buckets separate so severity-weighted scoring can
    do the right thing.

    Back-compat note
    ----------------
    :class:`DriftCount` is the original (drift-only) measurement unit.
    The generalised successor is :class:`MetricCount`, which carries an
    arbitrary namespaced metric name (``"drift:off_topic"``,
    ``"cost:input_tokens"``, ``"rubric:slide_structure"``, ...) and a
    float count. DriftCount is preserved verbatim so existing tests and
    JSON shapes that mention ``drift_counts`` keep working; the
    :meth:`MetricCount.from_drift_count` helper round-trips one into the
    other.

    Fields
    ------
    kind:
        Lowercase wire-canonical drift-kind string (see
        :mod:`zicato.core.drift_kinds`).
    severity:
        Goldfive's three-level severity scale.
    count:
        Number of drift events in this (kind, severity) bucket.
    """

    kind: str
    severity: Literal["info", "warning", "critical"]
    count: int


#: Severity literal for :class:`MetricCount`. Adds the empty string as a
#: "no severity" value for namespaces (cost, latency, output, ...) where
#: the drift three-bucket scale is meaningless.
MetricSeverity = Literal["info", "warning", "critical", ""]


@dataclass(frozen=True, slots=True)
class MetricCount:
    """Generic per-run metric measurement.

    Generalises :class:`DriftCount` so the same per-run unit can carry
    any namespaced metric the reducer / detectors / scorer cares about:
    drift kinds, cost (token counts, dollars), latency (p95 turn time),
    rubric scores, schema-failure counts, output-length stats, and so
    on. The namespace lives inside the :attr:`name` string as a
    colon-prefix (``"drift:off_topic"``, ``"cost:input_tokens"``,
    ``"rubric:slide_structure"``, ``"output:chars"``,
    ``"latency:p95_turn_ms"``).

    Drift becomes one namespace among many; :class:`DriftCount` stays as
    the back-compat surface and :meth:`from_drift_count` is the canonical
    promotion helper. The reducer continues to emit
    :attr:`LossProfile.drift_counts` and additionally exposes the
    superset view as :attr:`LossProfile.metric_counts` (everything in
    drift_counts is also present in metric_counts under the ``"drift:"``
    namespace, plus any other namespaces the reducer derived).

    Fields
    ------
    name:
        Namespaced metric name. Convention: ``"<namespace>:<key>"`` with
        a lowercase namespace prefix. Unnamespaced names (no colon) are
        legal but discouraged.
    severity:
        Severity bucket, or the empty string when the namespace has no
        natural severity (e.g. cost / latency).
    count:
        The measured value. Float rather than int so the same dataclass
        can carry counts (whole integers), rates (``[0.0, 1.0]``), scores
        (``[0.0, 5.0]``), and durations (milliseconds) without
        per-namespace dataclasses.
    """

    name: str
    severity: MetricSeverity = ""
    count: float = 0.0

    @classmethod
    def from_drift_count(cls, dc: DriftCount) -> MetricCount:
        """Promote a :class:`DriftCount` into a :class:`MetricCount`.

        The drift kind is prefixed with ``"drift:"`` to form the metric
        name. Severity and count carry over verbatim; the count is
        widened from ``int`` to ``float``.
        """
        return cls(
            name=f"drift:{dc.kind}",
            severity=dc.severity,
            count=float(dc.count),
        )


@dataclass(frozen=True, slots=True)
class JudgeLoss:
    """Per-judge loss attribution for one run.

    A custom in-run process judge fires a :class:`DriftDetected` of kind
    ``custom`` for each adverse verdict, paired with a
    ``JudgementEmitted`` carrying the judge's stable ``judge_name``. The
    reducer attributes each such drift to its authoring judge via
    :func:`zicato.telemetry.reducer._judge_attributed_kind` (folded into
    ``DriftCount.kind`` as ``"custom:<judge_name>"``). The aggregate
    drift_loss term that lives on :class:`LossProfile.drift_loss` already
    includes the per-judge contributions, but it does NOT preserve the
    per-judge attribution — every judge's contribution is summed into one
    scalar. :class:`JudgeLoss` carries that attribution out of the reducer
    so downstream consumers (the analyzer's per-judge drift-attribution
    section, the analytical index's ``judge_losses`` table) can answer
    "which judges drove this run's loss" without re-walking ``events.jsonl``.

    Fields
    ------
    judge_name:
        Stable per-judge identity (the ``name`` attribute of a
        :class:`zicato.board.judges.Judge`). Mirrors the key under
        :attr:`ScoringWeights.per_judge_weights`. The bare ``""``
        (empty string) names the catch-all bucket for unattributed
        ``custom``-kind drifts the reducer could not pair with a
        ``JudgementEmitted``.
    raw_loss:
        The judge's unweighted drift contribution — the
        severity-weighted sum of the judge's ``custom`` drift counts:
        ``sum(severity_weights[c.severity] * c.count for c in
        judge_drifts)``. Comparable across judges within the same epoch.
    weight:
        The judge's multiplier (:attr:`ScoringWeights.per_judge_weights`
        value, falling back to :attr:`ScoringWeights.default_judge_weight`).
        Preserved on the profile so the ingest path does not have to
        re-read scoring.json to recover the multiplier.
    weighted_loss:
        ``raw_loss * weight`` — the per-judge contribution that the
        aggregate ``drift_loss`` already sums in. Stored explicitly so a
        round-trip through JSON does not lose precision.
    """

    judge_name: str
    raw_loss: float
    weight: float
    weighted_loss: float


@dataclass(frozen=True, slots=True)
class ExpectationResult:
    """The outcome of evaluating a :class:`BoardEntry`'s expectation.

    Fields
    ------
    kind:
        The matcher kind that produced this result (same value as the
        originating :class:`Expectation.kind`). Typed as the
        :class:`ExpectationKind` enum; because that enum subclasses
        ``str``, a producer may still pass the bare wire token and it
        compares equal to the matching member.
    passed:
        ``True`` iff the matcher accepted the run.
    detail:
        Optional human-readable explanation (e.g. regex match position,
        judge rationale). Empty string when the matcher had nothing
        useful to say. Stored to give the journal something concrete to
        render alongside a pass/fail bit.
    """

    kind: ExpectationKind
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class LossProfile:
    """The reducer's per-run output — the contract scoring reads from.

    A :class:`LossProfile` is produced by the post-run reducer after the
    goldfive JSONL has been written. Pattern detectors and tournament
    scoring consume :class:`LossProfile` instances; they never re-read
    the raw events. This decoupling lets us evolve event schemas
    upstream without touching scoring.

    The structure is flat by design — every field is a scalar, a tuple of
    scalars, or a tuple of small frozen dataclasses — so the reducer's
    output round-trips through JSON and can be diffed in the journal.

    Fields
    ------
    run_id:
        Unique id of the run this profile describes.
    entry_id:
        The :class:`BoardEntry.id` the run executed.
    generation_id, epoch_id:
        Lineage coordinates — which generation under which epoch produced
        this profile.
    drift_counts:
        Per (kind, severity) drift-event counts.
    plan_revisions:
        Number of plan-revision events observed. A high count generally
        indicates the steerer worked hard; whether that is "good" or
        "bad" depends on outcome and the operator's rubric.
    task_failure_ratio:
        Ratio of fatally-failed tasks to total tasks the run produced.
        Range ``[0.0, 1.0]``.
    runtime_ms:
        Total wall-clock duration in milliseconds.
    wall_clock_budget_exceeded:
        ``True`` iff the run hit :attr:`BoardEntry.wall_clock_budget_seconds`
        and was force-aborted. When true, scoring treats this run as
        worst-case for the entry.
    expectation_result:
        Result of evaluating the entry's expectation, or ``None`` when
        the entry had no expectation. Note: this is allowed to be ``None``
        even on entries that DID have an expectation but the run was
        aborted before the expectation could fire — the reducer records
        that distinction via :attr:`wall_clock_budget_exceeded`.
    drift_loss:
        Weighted scalar derived from :attr:`drift_counts` using the
        epoch's :class:`ScoringWeights`. Higher = worse.
    pass_fail:
        Derived from :attr:`expectation_result`; ``None`` when no
        expectation was attached. Allows pass-rate aggregation across
        the board to ignore entries without ground truth.

    Multi-turn extras (single-turn entries leave these as ``None``)
    ----------------------------------------------------------------
    turns_completed:
        Number of conversational turns the run executed before
        terminating (whether by ``stop_when``, ``max_turns``, or abort).
    memory_failure_count:
        Zicato-derived signal: number of times across the conversation
        the inner agent re-asked something the simulated user had
        already answered. Computed by the reducer, not by goldfive.
    context_loss_count:
        Zicato-derived signal: number of times the inner agent appeared
        to forget a fact established earlier in the conversation.
        Heuristic; same multi-turn-pattern detector as
        :attr:`memory_failure_count`.

    Generalised metric surface
    --------------------------
    metric_counts:
        Superset of :attr:`drift_counts` lifted into the namespaced
        :class:`MetricCount` shape. When the reducer populates it
        explicitly, ``metric_counts`` carries every namespace the
        reducer derived (drift kinds under ``"drift:"``, token counts
        under ``"cost:"``, output length under ``"output:"``, schema
        failures under ``"schema:"``, ...). When the field is left as
        the empty tuple — the back-compat default — :meth:`unified_metrics`
        synthesises it on the fly from :attr:`drift_counts` plus the
        first-class scalar fields.
    tokens_spent, output_chars, schema_failures:
        First-class scalar metrics promoted out of the
        ``metric_counts`` tuple because they show up so often in
        analysis. Single source of truth: the reducer ensures these
        scalars and their MetricCount mirror entries
        (``"cost:tokens_spent"``, ``"output:chars"``, ``"schema:failures"``)
        agree.
    adk_session_id:
        The ADK/goldfive session id for this run — the ``sessionId``
        envelope field present on every event in the run's
        ``events.jsonl``. goldfive keys its session views by this id;
        the harmonograf deep-link route is ``/#/session/<adk_session_id>``.
        Empty string when the events file is absent or carries no
        envelope ``sessionId``. Back-compat default: ``""`` so profiles
        written before this field was added load cleanly.
    match_id:
        The tournament matchup this run executed within — e.g.
        ``"rung0_m2"``, ``"rung1_m0"``, ``"racing-final"``. Stamped by
        the tournament runner once the run settles (the reducer/worker
        does not know it). Empty string for runs that ran outside a
        tagged matchup — a gauntlet duel (which goes through
        ``run_tournament``, not ``run_matchup``) or any ad-hoc run — and
        for profiles written before this field was added. The dashboard
        derives a ``rung`` label from it (see
        :func:`zicato.selection.strategy.rung_for_match_id`).
    """

    run_id: str
    entry_id: str
    generation_id: str
    epoch_id: str
    drift_counts: tuple[DriftCount, ...]
    plan_revisions: int
    task_failure_ratio: float
    runtime_ms: int
    wall_clock_budget_exceeded: bool
    expectation_result: ExpectationResult | None
    drift_loss: float
    pass_fail: bool | None
    # Multi-turn extras
    turns_completed: int | None = None
    memory_failure_count: int | None = None
    context_loss_count: int | None = None
    # Generalised metric surface (back-compat: default empty; consumers
    # that want the merged view should call :meth:`unified_metrics`).
    metric_counts: tuple[MetricCount, ...] = ()
    tokens_spent: int = 0
    output_chars: int = 0
    schema_failures: int = 0
    # ADK/goldfive session id — carried on every event envelope; the
    # harmonograf deep-link route is /#/session/<adk_session_id>.
    # Back-compat default: "" so old profiles load without change.
    adk_session_id: str = ""
    # The tournament matchup this run ran within (e.g. "rung0_m2",
    # "racing-final"). Stamped by the tournament runner after the run
    # settles; "" for gauntlet / ad-hoc runs and for profiles written
    # before this field was added.
    match_id: str = ""
    # Per-judge loss attribution — empty tuple when no custom judge fired
    # against this run. The reducer sums each judge's ``custom``-kind
    # drift contributions (already attributed via ``custom:<judge_name>``)
    # and multiplies by the judge's weight; the aggregate ``drift_loss``
    # field already includes these contributions so this tuple is purely
    # the per-judge breakdown for downstream attribution. Back-compat
    # default: ``()`` so profiles written before this field was added
    # load cleanly.
    per_judge_loss: tuple[JudgeLoss, ...] = ()
    # Carried-over (cached) provenance. ``cached`` is ``True`` when this
    # profile was NOT produced by a live run in its own epoch but
    # MATERIALISED from a prior evaluation — the champion carried forward
    # into a new epoch (baseline-seed reuse) or a fast-mode reuse. The
    # per-board scalar (``drift_loss`` / ``pass_fail``) is the carried
    # value; ``source_epoch`` / ``source_run`` name where it came from so
    # the champion is consistent with the challengers (both materialised
    # per board, distinguished only by this provenance) and the index
    # never double-counts a cached champion as a fresh evaluation.
    # Back-compat default: ``cached=False`` / empty sources for every
    # freshly-run profile, so existing loss.json files load unchanged.
    cached: bool = False
    source_epoch: str = ""
    source_run: str = ""

    def unified_metrics(self) -> tuple[MetricCount, ...]:
        """Return the merged metric view across drift_counts + metric_counts.

        Always returns at least every :attr:`drift_counts` entry lifted
        into a :class:`MetricCount` under the ``"drift:"`` namespace.
        When :attr:`metric_counts` is non-empty, its entries are
        appended after the drift-promoted ones; the caller can dedupe
        on ``(name, severity)`` if they care, but the reducer is
        responsible for not emitting the same drift entry in both
        tuples — :attr:`metric_counts` is a superset when populated.

        When :attr:`metric_counts` is empty, the helper also synthesises
        :class:`MetricCount` entries for the first-class scalar fields
        (``tokens_spent``, ``output_chars``, ``schema_failures``) so
        downstream consumers see a uniform view regardless of how the
        profile was constructed.
        """
        out: list[MetricCount] = [MetricCount.from_drift_count(dc) for dc in self.drift_counts]
        if self.metric_counts:
            # Caller (the reducer) has populated the superset view
            # explicitly; trust it but skip duplicates of the drift
            # entries we already emitted.
            seen = {(mc.name, mc.severity) for mc in out}
            for mc in self.metric_counts:
                if (mc.name, mc.severity) in seen:
                    continue
                out.append(mc)
                seen.add((mc.name, mc.severity))
        else:
            # Synthesise the first-class scalars only when the caller
            # hasn't given us a richer view. Avoids double-counting when
            # the reducer already wrote them into metric_counts.
            if self.tokens_spent:
                out.append(
                    MetricCount(
                        name="cost:tokens_spent", severity="", count=float(self.tokens_spent)
                    )
                )
            if self.output_chars:
                out.append(
                    MetricCount(name="output:chars", severity="", count=float(self.output_chars))
                )
            if self.schema_failures:
                out.append(
                    MetricCount(
                        name="schema:failures", severity="", count=float(self.schema_failures)
                    )
                )
        return tuple(out)


# ---------------------------------------------------------------------------
# Run record / lineage
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Persistence-side record of one run.

    Bridges a run's executed-state metadata (when it started, when it
    ended, where its artifacts landed) with the lineage view. Distinct
    from :class:`RunResult` (the transcript-shape result the harness
    handed back) and :class:`LossProfile` (the reducer's output).

    Fields
    ------
    run_id:
        Unique id of the run.
    entry_id:
        The :class:`BoardEntry.id` executed.
    generation_id, epoch_id:
        Lineage coordinates.
    started_at, ended_at:
        ISO-8601 UTC strings — wall-clock timestamps.
    events_jsonl_path:
        Absolute path to the goldfive event JSONL written by the
        persistence sink.
    loss_profile_path:
        Absolute path to the reducer's per-run ``loss.json``.
    aborted:
        ``True`` iff the run was force-terminated (budget exceeded,
        operator cancel, runner exception).
    abort_reason:
        Short symbolic reason when :attr:`aborted` is true. Empty string
        otherwise.
    """

    run_id: str
    entry_id: str
    generation_id: str
    epoch_id: str
    started_at: str
    ended_at: str
    events_jsonl_path: Path
    loss_profile_path: Path
    aborted: bool = False
    abort_reason: str = ""


@dataclass(frozen=True, slots=True)
class RunResult:
    """The transcript-shape result of executing one board entry under one generation.

    Returned by the runner to expectation evaluators and to the reducer's
    multi-turn-pattern detectors. Carries only the user-facing surface —
    internal agent reasoning, tool calls, and goldfive events are stored
    elsewhere (the events JSONL) and intentionally not exposed here so
    the emulator and the judge cannot trivially collude with the inner
    harness.

    Fields
    ------
    run_id:
        Unique id of the run.
    entry_id:
        The :class:`BoardEntry.id` executed.
    final_output:
        The last assistant turn's user-facing output as a string. For
        single-turn entries this is the only assistant output. For
        multi-turn entries this is the final assistant turn.
    transcript:
        All assistant user-facing turns in order. For single-turn entries
        this is a length-1 tuple matching :attr:`final_output`. For
        multi-turn entries this is the full conversation from the user's
        view. User turns are NOT included — the entry already carries
        them (scripted) or the emulator produced them (emulated) and the
        reducer fetches them from goldfive's transcript if needed.
    runtime_ms:
        Total wall-clock duration in milliseconds.
    aborted:
        ``True`` iff the runner force-terminated this run.
    abort_reason:
        Short symbolic reason when :attr:`aborted` is true.
    """

    run_id: str
    entry_id: str
    final_output: str
    transcript: tuple[str, ...]
    runtime_ms: int
    aborted: bool = False
    abort_reason: str = ""


# ---------------------------------------------------------------------------
# Hypothesis / experiment
# ---------------------------------------------------------------------------


#: Predicted direction of movement for a drift kind under a hypothesis.
#:
#: * ``"decrease"`` / ``"increase"`` — strict directional predictions.
#: * ``"neutral"`` — expected to stay roughly flat.
#: * ``"decrease_or_neutral"`` / ``"increase_or_neutral"`` — directional
#:   prediction with the neutral case acceptable (the proposer is
#:   confident about one side but agnostic about the magnitude).
DriftDirection = Literal[
    "decrease",
    "increase",
    "neutral",
    "decrease_or_neutral",
    "increase_or_neutral",
]

#: Predicted magnitude of movement. Coarse buckets keep proposer
#: schemas compact; the journal records the actual delta separately so
#: this is only a qualitative hint.
DriftMagnitude = Literal["small", "medium", "large"]


@dataclass(frozen=True, slots=True)
class ExpectedDriftMovement:
    """A proposer's prediction about how one drift kind will move.

    Fields
    ------
    kind:
        The drift-kind string the prediction is about.
    direction:
        Predicted direction (see :data:`DriftDirection`).
    magnitude:
        Predicted magnitude bucket (see :data:`DriftMagnitude`).
    """

    kind: str
    direction: DriftDirection
    magnitude: DriftMagnitude


@dataclass(frozen=True, slots=True)
class ExpectedMetricMovement:
    """A proposer's prediction about how one namespaced metric will move.

    Generalises :class:`ExpectedDriftMovement` to any namespace. The
    proposer can now make claims about non-drift objectives — cost,
    latency, rubric scores, schema failures — using the same shape.

    Fields
    ------
    metric_name:
        The :class:`MetricCount.name` the prediction is about. Carries
        the namespace prefix (``"drift:off_topic"``, ``"cost:tokens_spent"``,
        ``"rubric:slide_structure"``, ``"latency:p95_turn_ms"``, ...).
    direction:
        Predicted direction (see :data:`DriftDirection` — reused
        verbatim; the direction lattice is namespace-agnostic).
    magnitude:
        Predicted magnitude bucket (see :data:`DriftMagnitude`).
    """

    metric_name: str
    direction: DriftDirection
    magnitude: DriftMagnitude


@dataclass(frozen=True, slots=True)
class HypothesisSpec:
    """Structured hypothesis written by the proposer BEFORE the run.

    Hypotheses are mandatory and structured so the journal captures what
    the proposer was thinking and whether it was right, not just what
    changed. Schema-invalid proposer responses are rejected and the
    proposer is asked to fix.

    Fields
    ------
    core_idea:
        One sentence describing what is being modulated. Must be terse
        enough to render in a one-line journal entry.
    modulating:
        The :class:`MutationPoint.id` values this hypothesis is touching.
        The proposer's :class:`Patch` set MUST address only these ids; the
        applier verifies.
    why:
        Pattern-driven rationale — why the proposer believes this edit
        will move the loss in the expected direction. Free-form prose.
    expected_drift_movements:
        Per-drift-kind directional predictions (see
        :class:`ExpectedDriftMovement`). Only kinds the proposer is
        making claims about need appear — silence implies "no claim".
    expected_pass_rate_delta:
        Predicted change in board-wide pass rate as free-text
        (e.g. ``"+0.10 to +0.20"``). Free text rather than a typed range
        because the proposer expresses uncertainty differently per
        hypothesis and a typed range would force false precision.
    risks:
        Optional one-paragraph description of failure modes the proposer
        anticipates and any mitigations baked into the patches.
    """

    core_idea: str
    modulating: tuple[str, ...]
    why: str
    expected_drift_movements: tuple[ExpectedDriftMovement, ...]
    expected_pass_rate_delta: str
    risks: str = ""
    # Generalised: predictions over any namespaced metric. Back-compat
    # default: empty. The proposer prefers this field when emitting new
    # hypotheses; the orchestrator round-trips the older
    # `expected_drift_movements` shape transparently.
    expected_metric_movements: tuple[ExpectedMetricMovement, ...] = ()


@dataclass(frozen=True, slots=True)
class DriftMovementActual:
    """Realized movement of one drift kind from parent to child generation.

    Joined with :class:`ExpectedDriftMovement` at outcome-write time to
    decide whether the proposer's prediction was correct.

    Fields
    ------
    kind:
        The drift-kind string.
    from_rate:
        Per-run mean count of this kind in the parent generation.
    to_rate:
        Per-run mean count of this kind in the child generation.
    hypothesis_match:
        ``True`` iff the realized movement matches the proposer's
        directional prediction within the magnitude bucket. ``False`` if
        the proposer predicted a movement that did not occur or occurred
        in the wrong direction.
    note:
        Optional human-readable detail (e.g. "predicted decrease,
        observed flat — within neutral band").
    """

    kind: str
    from_rate: float
    to_rate: float
    hypothesis_match: bool
    note: str = ""


@dataclass(frozen=True, slots=True)
class MetricMovementActual:
    """Realised movement of one namespaced metric across two generations.

    Generalises :class:`DriftMovementActual` for the metric-namespace
    surface. Joined with :class:`ExpectedMetricMovement` at outcome-
    write time to decide whether the proposer's prediction was correct.

    Fields
    ------
    metric_name:
        The :class:`MetricCount.name` whose movement is recorded.
    from_value:
        Per-run mean (or aggregate) value of this metric in the parent
        generation.
    to_value:
        Per-run mean (or aggregate) value of this metric in the child
        generation.
    hypothesis_match:
        ``True`` iff the realised movement matches the proposer's
        directional prediction within the magnitude bucket.
    note:
        Optional human-readable detail.
    """

    metric_name: str
    from_value: float
    to_value: float
    hypothesis_match: bool
    note: str = ""


#: The tournament's decision about an experiment.
#:
#: * ``"promoted"`` — child wins; becomes the new lineage head.
#: * ``"rejected"`` — child loses or regresses a hard gate.
#: * ``"deferred"`` — neither wins decisively; lineage head unchanged but
#:   the experiment is kept for analysis.
TournamentDecision = Literal["promoted", "rejected", "deferred"]


#: The five v1 tournament structures. ``"gauntlet"`` is the default and
#: reproduces the historical king-of-the-hill behaviour byte-for-byte.
#: The other four are configurable per-epoch via the ``tournament`` block
#: of ``scoring.json`` (see :class:`TournamentStructure`). The string
#: tokens are the closed enum the loader validates against and the keys
#: the selection-strategy registry maps to concrete strategy classes.
VALID_TOURNAMENT_STRUCTURES: tuple[str, ...] = (
    "gauntlet",
    "single_elim",
    "double_elim",
    "swiss",
    "racing",
)


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    """One match a generation played inside its tournament.

    A small audit record carried on :class:`OutcomeRecord` so a
    non-gauntlet structure (bracket / Swiss / racing) can record, per
    generation, which opponents it faced and how each duel went. A
    gauntlet leaves :attr:`OutcomeRecord.match_record` empty — its single
    crowning duel is already described by the top-level outcome fields.

    Fields
    ------
    match_id:
        Stable id of the match within the tournament (e.g. ``"WB-R0-0"``,
        ``"r2_m1"``, ``"rung1"``).
    opponent:
        The generation id this generation was paired against. Empty for a
        bye or an N-way racing rung.
    won:
        ``True`` when this generation was the match's winner (the side the
        gate / rank preferred).
    delta_scalar:
        ``this.scalar - opponent.scalar`` for the match. Negative = this
        generation scored the lower (better) loss.
    """

    match_id: str
    opponent: str
    won: bool
    delta_scalar: float


@dataclass(frozen=True, slots=True)
class TournamentStructure:
    """The per-epoch tournament structure and its tuning params.

    Part of the frozen evaluation contract: it is modelled as a field of
    :class:`ScoringWeights` (and therefore folds into the contract hash
    automatically), so changing the structure — or any param — rolls the
    epoch, exactly as retuning ``promote_margin`` does. A gauntlet
    champion and a Swiss champion are selected under different rules and
    are not directly comparable, which is precisely the contract-roll
    rationale.

    Fields
    ------
    structure:
        One of :data:`VALID_TOURNAMENT_STRUCTURES`. Defaults to
        ``"gauntlet"`` — the shipped king-of-the-hill behaviour.
    params:
        A structure-specific JSON object, stored and round-tripped
        verbatim as an opaque ``Mapping[str, Any]`` (the same
        forward-compat posture :attr:`BoardEntry.context` takes). The
        data layer enforces only that this is a mapping; per-key
        semantics (``field_size``, ``replicates``, ``swiss.rounds_n``,
        ``racing.eta`` / ``board_fraction`` / ``rung0_board_size``, …)
        are owned by the selection strategy that reads them.

    The default factory :meth:`gauntlet` yields the fully-defaulted
    gauntlet spec an absent ``tournament`` block resolves to.
    """

    structure: str = "gauntlet"
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.structure not in VALID_TOURNAMENT_STRUCTURES:
            valid = ", ".join(repr(s) for s in VALID_TOURNAMENT_STRUCTURES)
            raise ValueError(
                f"invalid tournament structure {self.structure!r}; " f"valid values are: {valid}"
            )
        if not isinstance(self.params, Mapping):
            raise ValueError(
                f"tournament params must be a JSON object (mapping), got "
                f"{type(self.params).__name__}"
            )

    @classmethod
    def gauntlet(cls) -> TournamentStructure:
        """The fully-defaulted gauntlet spec (the back-compat default)."""
        return cls(structure="gauntlet", params={})


def _default_tournament_structure() -> TournamentStructure:
    """Default-factory for :attr:`ScoringWeights.tournament_structure`."""
    return TournamentStructure.gauntlet()


@dataclass(frozen=True, slots=True)
class LadderConfig:
    """The Ladder/Thresholdout governor over the holdout query (OVERFITTING.md §4, §12 #2).

    Phase A built the train/holdout split and a holdout-*confirmation* step
    (:class:`OverfittingConfig`). This sub-config governs *how* that holdout
    is queried across an epoch's rounds, after Blum & Hardt's Ladder: a
    reused holdout stays valid under an adaptively-querying proposer only if
    every interaction with it is mediated by a mechanism that limits the
    information leaked back. The two rules:

    * **Release rule.** A new holdout-based signal is released only when the
      *train-measured* improvement clears the threshold beyond the noise
      band. Within the band the previous best is re-reported, so the
      proposer cannot chase board fluctuations.
    * **Budget.** Each holdout query charges a finite per-epoch budget; once
      exhausted, no further holdout signals are released (the loop degrades
      to "champion stands" — no holdout-gated promotion).

    Folded into the contract hash through :class:`OverfittingConfig` →
    :class:`ScoringWeights` (the canonicalizer recurses into nested frozen
    dataclasses), so changing any knob — or the one-time default-on rollout —
    rolls the epoch, exactly as retuning ``promote_margin`` does.

    Default-on with a safe auto-degrade: an empty holdout (small board, split
    disabled) means there is nothing to govern, and the Ladder is a no-op —
    behaviour stays byte-identical to Phase A.

    Fields
    ------
    enabled:
        Master switch for the Ladder governor. ``True`` by default. When
        ``False`` the holdout confirmation runs in its raw Phase-A form
        (every holdout query counts, no budget, no release rule).
    threshold:
        The train-improvement bar the release rule applies. ``None``
        (default) derives it from :attr:`ScoringWeights.promote_margin` so
        the Ladder reuses the gate's existing noise threshold; a float pins
        it explicitly.
    budget:
        Per-epoch holdout-query budget. Each round that consults the holdout
        charges one. When the budget is exhausted the Ladder stops releasing
        holdout signals. Must be ``>= 0`` (``0`` releases nothing).
    noise_scale:
        Width of the noise band added to the threshold. ``0.0`` (default) is
        the parameter-free Ladder — no calibration needed. Reserved for
        DP-grade noise calibration later; must be ``>= 0``.
    """

    enabled: bool = True
    threshold: float | None = None
    budget: int = 16
    noise_scale: float = 0.0

    def __post_init__(self) -> None:
        if self.threshold is not None and self.threshold < 0.0:
            raise ValueError(f"ladder.threshold must be >= 0 or None, got {self.threshold!r}")
        if self.budget < 0:
            raise ValueError(f"ladder.budget must be >= 0, got {self.budget!r}")
        if self.noise_scale < 0.0:
            raise ValueError(f"ladder.noise_scale must be >= 0, got {self.noise_scale!r}")

    @classmethod
    def defaults(cls) -> LadderConfig:
        """The fully-defaulted (default-on) config an absent block resolves to."""
        return cls()


def _default_ladder_config() -> LadderConfig:
    """Default-factory for :attr:`OverfittingConfig.ladder`."""
    return LadderConfig.defaults()


@dataclass(frozen=True, slots=True)
class OverfittingConfig:
    """Anti-overfitting controls: the train/holdout board split + leakage gate.

    Part of the frozen evaluation contract: it is modelled as a field of
    :class:`ScoringWeights` (and therefore folds into the contract hash
    automatically through the existing scoring canonicalizer), so changing
    any knob — or the one-time default-on rollout — rolls the epoch,
    exactly as retuning ``promote_margin`` does. A run that splits a holdout
    out of the board, and confirms promotions against it, selects champions
    under a different rule than one that does not, which is precisely the
    contract-roll rationale.

    Every field is default-on with a safe auto-degrade: a board too small
    to split (fewer than :attr:`min_board_size_for_split` entries, and no
    explicit ``holdout`` tag) yields an *empty* holdout, and the whole
    machine collapses to the pre-split behaviour byte-for-byte.

    Fields
    ------
    enabled:
        Master switch for the train/holdout split. ``True`` by default.
        When ``False``, no holdout is ever derived (an explicit
        ``holdout`` tag still wins — see :func:`zicato.board.split.split_board`)
        and the loop behaves exactly as it did before this phase.
    holdout_fraction:
        Target fraction of the board to hold out when the split is derived
        by hash (no explicit ``holdout`` tag). A deterministic, id-stable
        threshold selects approximately this fraction. Range ``(0, 1)``.
    min_board_size_for_split:
        Smallest board size at which a hash-derived split is attempted.
        Below this the holdout is empty (degrade to today's behaviour) so a
        small board is never starved of train entries. An explicit
        ``holdout`` tag overrides this floor.
    restrict_proposer_visibility:
        When ``True`` (default), the proposer prompt is sanitised at the
        render boundary: per-entry identities in the detector patterns are
        aggregated to counts/rates, and experiment-memory ``Δscalar`` is
        coarsened to ``improved``/``flat``/``regressed`` buckets. Turning
        it off restores the verbatim rendering byte-for-byte.
    ladder:
        The Ladder/Thresholdout governor over the holdout query
        (:class:`LadderConfig`; OVERFITTING.md §4 / §12 #2). Default-on;
        a no-op when the holdout is empty.
    rotate_holdout:
        When ``True`` (default), the hash-derived holdout *rotates* across
        epochs (OVERFITTING.md §7 / §12 #6): the epoch id is folded into the
        id-hash at the split call sites so a different ~``holdout_fraction``
        slice is held out each epoch — no fixed slice is mined forever.
        Stable *within* an epoch (the seed is the epoch id). When ``False``
        the unseeded split is used (the same slice every epoch). The
        rotation is an epoch-local derivation: it does NOT change the
        contract hash for an unchanged board — only this flag itself
        participates in the hash. An explicit ``holdout`` tag is never
        rotated.
    max_generations_per_contract:
        Optional cadence ceiling (OVERFITTING.md §9 / §12 #6, cross-ref
        SELECTION-THEORY.md §5 optimal-stopping horizon). When set, the loop
        surfaces a board-refresh *recommendation* (a health finding / logged
        signal) once a contract has been mined for this many generations —
        a cue that the contract should be refreshed (the operator rolls).
        ``None`` (default) imposes no ceiling. This never forces a surprising
        auto epoch-roll; it only recommends. Must be ``>= 1`` when set.
    """

    enabled: bool = True
    holdout_fraction: float = 0.3
    min_board_size_for_split: int = 8
    restrict_proposer_visibility: bool = True
    ladder: LadderConfig = field(default_factory=_default_ladder_config)
    rotate_holdout: bool = True
    max_generations_per_contract: int | None = None

    def __post_init__(self) -> None:
        if not 0.0 < self.holdout_fraction < 1.0:
            raise ValueError(f"holdout_fraction must be in (0, 1), got {self.holdout_fraction!r}")
        if self.min_board_size_for_split < 0:
            raise ValueError(
                f"min_board_size_for_split must be >= 0, got " f"{self.min_board_size_for_split!r}"
            )
        if self.max_generations_per_contract is not None and self.max_generations_per_contract < 1:
            raise ValueError(
                f"max_generations_per_contract must be >= 1 or None, got "
                f"{self.max_generations_per_contract!r}"
            )

    @classmethod
    def defaults(cls) -> OverfittingConfig:
        """The fully-defaulted (default-on) config an absent block resolves to."""
        return cls()


def _default_overfitting_config() -> OverfittingConfig:
    """Default-factory for :attr:`ScoringWeights.overfitting`."""
    return OverfittingConfig.defaults()


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """The post-run record appended to an :class:`Experiment` after evaluation.

    Written atomically by the tournament runner once the decision is
    made. The pairing with :class:`HypothesisSpec` is the journal's
    core unit — what was predicted, what happened, what was decided.

    Fields
    ------
    ran_at:
        ISO-8601 UTC timestamp when the experiment finished evaluating.
    drift_movements:
        Per-kind realized movements, one entry per kind the hypothesis
        made a claim about plus any kind whose realized movement was
        large enough for the tournament to flag.
    pass_rate_delta:
        Change in board-wide pass rate from parent to child generation.
        Range ``[-1.0, 1.0]``.
    drift_loss_delta:
        Change in mean drift loss across the board. Negative = improvement.
    scalar_score_delta:
        Change in the combined tournament scalar (see
        :class:`ScoringWeights`). The sign of this field gates the
        :attr:`tournament_decision`.
    tournament_decision:
        The decision (see :data:`TournamentDecision`).
    rejection_reason:
        Symbolic reason when :attr:`tournament_decision` is
        ``"rejected"``. Empty string for the other two outcomes.
    """

    ran_at: str
    drift_movements: tuple[DriftMovementActual, ...]
    pass_rate_delta: float
    drift_loss_delta: float
    scalar_score_delta: float
    tournament_decision: TournamentDecision
    rejection_reason: str = ""
    # Generalised: realised movements over any namespaced metric. The
    # original `drift_movements` field is kept verbatim so existing
    # journal JSON keeps deserialising; `metric_movements` is the
    # superset surface for new namespaces.
    metric_movements: tuple[MetricMovementActual, ...] = ()
    # Generalised tournament-structure surface (additive; every field
    # defaults to the gauntlet reading so old journals deserialize and
    # score unchanged). ``structure`` mirrors the epoch's resolved
    # ``tournament.structure``; ``final_rank`` / ``eliminated_in_round``
    # / ``match_record`` describe this generation's path through a
    # non-gauntlet bracket. A gauntlet leaves them at the defaults below.
    structure: str = "gauntlet"
    final_rank: int | None = None
    eliminated_in_round: int | None = None
    match_record: tuple[MatchOutcome, ...] = ()
    # RUNTIME champion-eval provenance (NOT a contract input): how the
    # champion side was evaluated this round under the ``--mode`` knob.
    # ``"full"`` = the champion was run live; ``"fast"`` = its cached
    # per-board scalars were reused and the champion was NOT executed;
    # ``"fast-degraded"`` = fast was requested but no cache covered the
    # needed boards, so the champion ran once to seed it. Defaults to
    # ``"full"`` so older journals deserialize unchanged. Recorded purely
    # for provenance — flipping fast↔full does not roll the epoch.
    champion_eval_mode: str = "full"
    # Holdout + Ladder evidence for THIS round (OVERFITTING.md §4 / §12 #2).
    # ``None`` (the default) when there was no holdout to consult — a small
    # board, the split disabled, or no tagged entry — so older journals and
    # the byte-identical Phase-A degrade carry no block. When a holdout was
    # consulted this is a plain JSON-shaped dict with the stable shape the
    # dashboard reads (the keys are documented at
    # :func:`zicato.tournament.ladder.holdout_record`):
    # ``{"confirmed": bool|None, "train_scalar": float|None,
    #    "holdout_scalar": float|None, "ladder_released": bool,
    #    "ladder_budget_total": int, "ladder_budget_remaining": int,
    #    "threshold": float}``. RUNTIME evidence, not a contract input.
    holdout: dict[str, Any] | None = None
    # Per-generation train/holdout loss + the generalization gap
    # (OVERFITTING.md §6 / §12 #5). RUNTIME evidence, not a contract input.
    # ``train_loss`` is THIS generation's (the child's) TRAIN-slice scalar —
    # the score that gated it. ``holdout_loss`` is its HOLDOUT-slice scalar,
    # or ``None`` when there was no holdout (small board / split disabled /
    # older journals). ``generalization_gap`` is ``holdout_loss - train_loss``
    # (positive = the holdout is worse than train, the memorization signature),
    # or ``None`` when there is no holdout. A parallel dashboard agent reads
    # these three keys verbatim; the ``generalization_gap`` health detector
    # reads them off the champion lineage.
    train_loss: float | None = None
    holdout_loss: float | None = None
    generalization_gap: float | None = None


#: Hard cap on the number of settled prior experiments surfaced to the
#: proposer's experiment-memory section (the ``## What's already been
#: tried`` block). A long epoch can accumulate dozens of experiments; the
#: digest is curated and capped to this many so the prompt stays small
#: and the mutation manifest the proposer must read in full is not
#: crowded out. Wins are never dropped by the cap; the sharpest recent
#: rejections fill the remainder. See ``docs/design/EXPERIMENT-MEMORY.md``
#: §3.3.
EXPERIMENT_MEMORY_MAX_ENTRIES = 12


@dataclass(frozen=True, slots=True)
class PriorExperiment:
    """One prior experiment as surfaced to the proposer's memory section.

    A compact digest entry — what was tried, where, and how it fared —
    assembled by the orchestrator (the index reader for settled history,
    the field loop for in-flight siblings) and rendered into the
    ``## What's already been tried`` user-prompt section. The proposer
    reads it to avoid re-proposing known failures and to build on known
    wins. It is advisory context only — never part of the hard schema or
    the system prompt. See ``docs/design/EXPERIMENT-MEMORY.md`` §3.2.

    Fields
    ------
    generation_id, epoch_id:
        Lineage coordinates of the prior experiment's child generation.
    core_idea:
        One-sentence hypothesis core (the ``HypothesisSpec.core_idea`` the
        proposer wrote for that experiment).
    modulating:
        The targeted mutation-point ids — the experiment's *declared*
        ``HypothesisSpec.modulating`` set, lifted from the recorded
        hypothesis.
    decision:
        The verdict: ``"promoted"`` / ``"rejected"`` / ``"deferred"`` for
        a settled experiment, or ``"in_flight"`` for a sibling minted
        this round but not yet run.
    rejection_reason:
        The symbolic reason when ``decision == "rejected"``; ``""``
        otherwise.
    scalar_score_delta:
        The signed Δscalar (negative = the child scored the lower /
        better loss). ``None`` when the experiment is unsettled /
        in-flight or when the delta does not transfer (a cross-contract
        entry — see :attr:`same_contract`).
    same_contract:
        ``True`` for a same-epoch (same-contract) entry whose Δscalar is
        directly comparable; ``False`` for a cross-contract entry from a
        different epoch under the same ``contract_hash``, which renders
        without its Δscalar because the number does not transfer.
    """

    generation_id: str
    epoch_id: str
    core_idea: str
    modulating: tuple[str, ...]
    decision: str
    rejection_reason: str
    scalar_score_delta: float | None
    same_contract: bool = True


@dataclass(frozen=True, slots=True)
class Experiment:
    """One generation's proposer output joined with its tournament outcome.

    An :class:`Experiment` is the unit of journaling. It is constructed
    when the proposer emits a hypothesis+patches; the :attr:`outcome`
    starts as ``None`` and is filled in by the tournament runner once
    the run completes and the decision is made.

    Fields
    ------
    id:
        Experiment identifier (convention: ``"exp_{epoch}_{generation}"``).
    epoch_id, generation_id:
        The lineage coordinates of THIS experiment's child generation.
    parent_generation_id:
        The lineage head this experiment is challenging.
    proposed_at:
        ISO-8601 UTC timestamp when the proposer emitted the hypothesis.
    hypothesis:
        The proposer's structured ahead-of-time prediction.
    patches:
        The concrete edits the proposer wants applied to the parent
        snapshot to produce the child snapshot.
    outcome:
        The tournament's verdict, or ``None`` until the experiment runs.
    round_index:
        The 0-based EVOLVE round that minted this generation. Persisted into
        ``experiment.json`` so the dashboard can attribute each generation to
        its birth round (the round-timeline / champion-spine view reads it);
        the canonical value the orchestrator already threads as
        ``Generation.round_index``. Defaults to 0 for the seed and for
        pre-feature records that predate the stamp.
    """

    id: str
    epoch_id: str
    generation_id: str
    parent_generation_id: str
    proposed_at: str
    hypothesis: HypothesisSpec
    patches: tuple[Patch, ...]
    outcome: OutcomeRecord | None
    round_index: int = 0


# ---------------------------------------------------------------------------
# Epoch / generation
# ---------------------------------------------------------------------------


def _default_severity_weights() -> Mapping[str, float]:
    """Default severity multipliers for drift-loss scoring.

    INFO is the baseline (1.0), WARNING is materially worse (3.0), and
    CRITICAL is qualitatively different (10.0) — a single CRITICAL drift
    swamps a handful of INFOs. Operators tune these per epoch.
    """
    return {"info": 1.0, "warning": 3.0, "critical": 10.0}


def _default_namespace_weights() -> Mapping[str, float]:
    """Default per-namespace weights for the multi-objective scalar.

    The mapping keys are namespace prefixes (with the trailing colon
    preserved so callers never have to remember to add or strip it).
    Values are signed coefficients that turn a namespace's per-run mean
    metric value into a scalar-component contribution:

    * Positive weight → "higher value is worse". The component is added
      to the scalar as ``weight * mean``. Drift, cost, latency, and
      schema-failure namespaces have positive weights.
    * Negative weight → "higher value is better". Rubric scores grow with
      quality, so a negative weight turns the scalar into a loss.
    * Zero → namespace excluded from the scalar entirely. Useful for
      observability-only namespaces (``output:`` length stats) the
      operator wants to track but not optimise.

    Defaults intentionally span several orders of magnitude — cost is
    often counted in tokens (thousands) while drift loss is a small
    weighted sum, so the cost coefficient is small to keep both terms
    in a comparable scale.
    """
    return {
        "drift:": 1.0,
        "cost:": 0.001,
        "latency:": 0.0001,
        "rubric:": -1.0,
        "output:": 0.0,
        "schema:": 5.0,
    }


def _default_namespace_monotonicity() -> Mapping[str, bool]:
    """Default per-namespace monotonicity flags for the promote gate.

    When a namespace's flag is ``True``, the gate rejects any child
    whose per-namespace aggregate has regressed against the parent (in
    the namespace's own "worse" direction, as encoded by the sign of
    the corresponding :func:`_default_namespace_weights` entry).

    The defaults guard the namespaces whose regression is qualitatively
    bad even when the overall scalar improves: rubric (quality drop)
    and schema (introducing failures). Drift is left unguarded so
    proposers can trade some drift movement for gains elsewhere.
    """
    return {
        "drift:": False,
        "rubric:": True,
        "schema:": True,
    }


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """Tunable weights that turn a :class:`LossProfile` into a scalar.

    A single :class:`ScoringWeights` instance is frozen for the lifetime
    of an epoch. Changing weights starts a new epoch — generations across
    different epochs are not directly comparable.

    Fields
    ------
    drift_weight:
        Coefficient on the aggregated drift-loss term.
    pass_weight:
        Coefficient on the ``(1 - pass_rate)`` term.
    severity_weights:
        Per-severity multipliers applied inside the drift-loss
        aggregation. Keys are lowercase severity strings; missing keys
        default to ``0.0`` (the aggregator treats unknown severities as
        non-scoring rather than panicking).
    per_kind_weights:
        Optional per-drift-kind multipliers. Stacks multiplicatively
        with :attr:`severity_weights`. Empty mapping = uniform weighting
        across kinds.
    per_judge_weights:
        Optional per-custom-judge multipliers, keyed on the stable
        ``judge_name`` (the value a judge implementation sets on its
        ``name`` attribute). A custom judge emits drift under the
        single ``"custom"`` drift kind, so :attr:`per_kind_weights`
        cannot tell two custom judges apart — ``per_judge_weights``
        is the per-judge analogue. It stacks multiplicatively with
        :attr:`severity_weights` exactly the way :attr:`per_kind_weights`
        does for first-class kinds. A custom judge with no entry here
        scores at :attr:`default_judge_weight` rather than crashing —
        mirroring how an unknown kind falls back to ``1.0`` under
        :attr:`per_kind_weights`. Empty mapping = every custom judge
        scores at the default.
    default_judge_weight:
        Fallback multiplier for a custom judge whose ``judge_name`` is
        absent from :attr:`per_judge_weights`. Defaults to ``1.0`` so an
        unconfigured custom judge contributes on the same footing as a
        first-class drift kind with no ``per_kind_weights`` entry.
    plan_revision_weight:
        Coefficient on :attr:`LossProfile.plan_revisions`. Defaults to
        ``0.5`` — plan revisions are signal but less so than drift.
    runtime_weight:
        Coefficient on per-second runtime. Defaults to ``0.0`` — operators
        usually rely on the wall-clock budget as a hard ceiling rather
        than scoring runtime continuously, but the knob is here for
        cases where runtime matters intrinsically.
    promote_margin:
        Minimum scalar-score improvement the child generation must show
        over the parent to be promoted. Acts as a regression-noise
        threshold.
    pass_rate_monotonicity:
        When ``True`` (default), any regression in pass rate
        automatically rejects the child regardless of drift-side
        improvement. The stricter half of the tournament gate; operators
        can flip to ``False`` for experimental epochs where they expect
        non-monotone exploration.
    regression_gate_enabled:
        When ``True``, the tournament runner shells out to the
        snapshot's own test suite BEFORE evaluating the scoring gate.
        A non-passing suite hard-rejects the candidate regardless of
        drift_loss / pass_rate movement. Defaults to ``False`` for
        backwards compatibility with epochs whose snapshots do not
        ship a regression suite.
    regression_test_command:
        The argv used to invoke the regression suite. Defaults to a
        plain pytest invocation; operators with non-pytest suites can
        override (e.g. ``("python", "-m", "unittest", "discover")``).
    regression_timeout_s:
        Wall-clock seconds the regression subprocess is allowed before
        the runner kills it. A timeout counts as a regression failure.
    namespace_weights:
        Per-namespace coefficients used by the multi-objective scalar.
        Keys are namespace prefixes (with the trailing colon, e.g.
        ``"drift:"``). The sign of each coefficient codifies the
        namespace's "worse" direction:

        * Positive → higher value is worse (drift, cost, latency,
          schema). Added to the scalar as ``weight * mean``.
        * Negative → higher value is better (rubric). The negation
          flips the metric into a loss so the scalar stays
          lower-is-better.
        * Zero → namespace excluded from the scalar; tracked but not
          optimised (default for ``"output:"``).

        See :func:`_default_namespace_weights` for the shipped values.
    namespace_monotonicity:
        Per-namespace strict-monotonicity flags. When a namespace's
        flag is ``True``, the promote gate rejects any child whose
        per-namespace aggregate has moved in the namespace's "worse"
        direction (as defined by the sign in
        :attr:`namespace_weights`) by more than the namespace's
        tolerance — even when the combined scalar improves. Namespaces
        whose flag is missing or ``False`` are not gated this way.
    """

    drift_weight: float = 1.0
    pass_weight: float = 1.0
    severity_weights: Mapping[str, float] = field(default_factory=_default_severity_weights)
    per_kind_weights: Mapping[str, float] = field(default_factory=dict)
    per_judge_weights: Mapping[str, float] = field(default_factory=dict)
    default_judge_weight: float = 1.0
    plan_revision_weight: float = 0.5
    runtime_weight: float = 0.0
    promote_margin: float = 0.01
    pass_rate_monotonicity: bool = True
    regression_gate_enabled: bool = False
    regression_test_command: tuple[str, ...] = ("pytest", "tests/", "-q")
    regression_timeout_s: int = 600
    # Multi-objective surface — see the helpers above for the rationale
    # behind the default coefficient choices.
    namespace_weights: Mapping[str, float] = field(default_factory=_default_namespace_weights)
    namespace_monotonicity: Mapping[str, bool] = field(
        default_factory=_default_namespace_monotonicity
    )
    # Per-epoch tournament structure (gauntlet by default). Modelled here
    # so it factors into the contract hash through the existing scoring
    # canonicalizer with zero new plumbing: changing the structure or any
    # param rolls the epoch. See :class:`TournamentStructure`.
    tournament_structure: TournamentStructure = field(default_factory=_default_tournament_structure)
    # Anti-overfitting controls (train/holdout split + proposer leakage
    # restriction). Modelled here so it factors into the contract hash
    # through the existing scoring canonicalizer with zero new plumbing:
    # changing any knob — or the one-time default-on rollout — rolls the
    # epoch. Default-on with a safe auto-degrade on small boards. See
    # :class:`OverfittingConfig` and ``docs/design/OVERFITTING.md``.
    overfitting: OverfittingConfig = field(default_factory=_default_overfitting_config)


# ---------------------------------------------------------------------------
# Proposer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposerSkill:
    """One markdown skill module the proposer loads from a proposer dir.

    A skill is a single ``proposers/<name>/skills/*.md`` file: a small
    block of operator-authored guidance the proposer composes into its
    context. Skills carry SKILL.md-style frontmatter (a ``name`` and a
    ``description``) followed by a free-form markdown body.

    The skill is part of the *evaluation contract*: a semantic edit to a
    skill body — like an edit to the proposer brief — means generations on
    either side of the change are steered differently and are no longer
    directly comparable, so the epoch must roll. The contract hash folds
    the skill bodies in (see :func:`zicato.epoch.contract._canon_proposer`);
    cosmetic whitespace edits are normalized away so only semantic changes
    roll the epoch.

    Fields
    ------
    name:
        The skill's identifier — the ``name`` frontmatter value, falling
        back to the file's stem when no frontmatter is present.
    description:
        One-line summary from the ``description`` frontmatter value;
        the empty string when absent.
    body:
        The markdown body following the frontmatter, verbatim. Contract
        canonicalization normalizes its whitespace before hashing.
    """

    name: str
    description: str
    body: str


@dataclass(frozen=True, slots=True)
class ProposerSpec:
    """The resolved proposer for an epoch — its agent identity + skills.

    A proposer is either the built-in default agent (no skills, no custom
    agent module) or a ``proposers/<name>/`` directory carrying markdown
    skill modules and an optional custom ``agent.py``. :class:`ProposerSpec`
    is the resolved, hash-ready shape of that directory; it is produced by
    :func:`zicato.proposer.skills.resolve_proposer_spec` and folded into the
    contract hash so configuring a proposer dir — or editing one of its
    skills — rolls the epoch.

    Fields
    ------
    agent_id:
        ``"builtin:default"`` for the built-in agent, or ``"dir:<name>"``
        when a ``proposers/<name>/agent.py`` directory backs the proposer.
        The id distinguishes the builtin from any on-disk proposer even
        when the latter happens to carry no skills.
    tools:
        Names of the tools the proposer agent may call. Empty for the
        builtin; tool declaration is a later phase, so an on-disk proposer
        also resolves with empty tools for now.
    skills:
        The loaded :class:`ProposerSkill` modules, sorted by name.
    agent_source_sha256:
        Hex SHA-256 of the proposer dir's ``agent.py`` when present, else
        ``None``. Folded into the contract hash so editing the custom
        agent's source rolls the epoch.
    """

    agent_id: str
    tools: tuple[str, ...]
    skills: tuple[ProposerSkill, ...]
    agent_source_sha256: str | None

    @classmethod
    def default(cls) -> ProposerSpec:
        """Return the built-in default proposer — no skills, no tools.

        The default is the built-in agent that runs when no proposer dir
        is configured. It canonicalizes to a stable form so a workspace
        that never configures a proposer keeps a stable contract hash.
        """
        return cls(
            agent_id="builtin:default",
            tools=(),
            skills=(),
            agent_source_sha256=None,
        )


@dataclass(frozen=True, slots=True)
class EpochConfig:
    """The frozen evaluation contract for an epoch.

    Pinned for the lifetime of the epoch: board, proposer brief, scoring
    weights. Changing any of these starts a new epoch — see
    :doc:`project_zicato_journaling_and_epochs`.

    Fields
    ------
    id:
        Stable epoch identifier (operator-chosen; filesystem-safe).
    name:
        Human-readable name surfaced in CLI listings.
    created_at:
        ISO-8601 UTC timestamp of epoch creation.
    board_path:
        Absolute path to the frozen ``board.jsonl`` for this epoch.
    brief_path:
        Absolute path to the frozen ``brief.md`` proposer brief the
        proposer reads each round. (Earlier revisions named this field
        ``rubric_path`` and the file ``rubric.md``; both were renamed.)
    scoring:
        The frozen :class:`ScoringWeights` for this epoch.
    closed:
        ``True`` once the epoch has been closed by ``zicato epoch close``
        (or auto-closed by a subsequent ``zicato epoch new``). Closed
        epochs are read-only.
    closed_at:
        ISO-8601 UTC timestamp of closure, or empty string when still
        open.
    contract_hash:
        ``sha256`` hex digest of the canonicalized evaluation contract
        (board + rubric + scoring + the registered inner-harness
        identity) at the time this epoch was created. See
        :mod:`zicato.epoch.contract`. The orchestrator recomputes this
        on every ``evolve`` and auto-rolls the epoch when the live
        contract drifts from the stored value.

        The default is the empty string. An empty ``contract_hash``
        means "epoch created before contract-hash auto-epoching landed"
        — such legacy epochs are treated as *always matching* so the
        orchestrator never spuriously rolls a workspace that predates
        the feature.
    goal:
        Free-form operator-supplied statement of *why* this epoch
        exists — the intent the operator is testing (e.g. "shift the
        proposer brief toward concrete deltas" or "new scoring weights
        for cost drift"). Machine-readable companion to the narrative
        in ``journal.md``; surfaced in the analyzer report header so
        the reason for an epoch is visible without re-reading the
        journal. Defaults to the empty string (which renders as "no
        goal recorded" downstream) so epochs already on disk that
        predate this field load cleanly. May be multi-line.
    proposer_path:
        Filesystem location of the ``proposers/<name>/`` directory frozen
        for this epoch, or ``None`` for the built-in default proposer.
        Folds into the contract hash via :mod:`zicato.epoch.contract`, so
        configuring a proposer dir (or editing one of its skills) rolls
        the epoch. Defaults to ``None``; an epoch ``config.json`` written
        before this field landed loads as the built-in default.
    """

    id: str
    name: str
    created_at: str
    board_path: Path
    brief_path: Path
    scoring: ScoringWeights
    closed: bool = False
    closed_at: str = ""
    contract_hash: str = ""
    goal: str = ""
    # Location of the proposer dir frozen for this epoch, or ``None`` for
    # the built-in default proposer. Folded into the contract hash; missing
    # in an epoch ``config.json`` written before this field landed ⇒ ``None``.
    proposer_path: Path | None = None


@dataclass(frozen=True, slots=True)
class Generation:
    """One node in an epoch's generation lineage.

    Fields
    ------
    id:
        Stable generation identifier. Convention: ``"v0"``, ``"v1"``,
        ascending under one epoch. The ``"v"`` prefix is preserved in
        filesystem paths.
    epoch_id:
        The epoch this generation belongs to.
    parent_id:
        The generation this one was forked from, or ``None`` for the
        epoch's seed generation (``"v0"``).
    snapshot_root:
        Absolute path to the source-tree snapshot for this generation.
        The patch applier produced this by copying the parent's snapshot
        and applying the experiment's patches; the runner mounts it as
        the inner harness's source root for the duration of the run.
    created_at:
        ISO-8601 UTC creation timestamp.
    promoted:
        ``True`` iff this generation has been promoted to lineage head
        by a tournament. The epoch's current head is the most-recent
        promoted generation; ``promoted=False`` generations are dead
        branches kept for analysis.
    round_index:
        The evolve round that MINTED this generation — its birth round.
        Round indices are zero-based (the first evolve round is ``0``),
        and the epoch's genesis seed (``v0``) is round ``0``. A champion
        carried into later rounds keeps its birth round; it is NOT
        re-stamped each round it defends. Consumers group an epoch's
        generations as ``Epoch -> Round -> {challengers minted that
        round}``. Defaults to ``0`` so legacy callers (and the seed)
        need not specify it.
    """

    id: str
    epoch_id: str
    parent_id: str | None
    snapshot_root: Path
    created_at: str
    promoted: bool = False
    round_index: int = 0


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pattern:
    """A loss-pattern observation surfaced by a pattern detector.

    Patterns are the bridge from raw :class:`LossProfile` instances to
    proposer-actionable observations. Detectors produce a list of
    :class:`Pattern` objects each generation; the proposer reads the
    list and decides which to address. :attr:`kind` is open-ended (a
    bare string, not a Literal) so new detector kinds can be added
    without breaking the schema.

    Fields
    ------
    id:
        Stable pattern identifier within a generation.
    kind:
        Detector-defined kind string. Conventional values include
        ``"drift_kind_frequency"`` (one drift kind dominates),
        ``"hot_task"`` (one task id drifts disproportionately often),
        ``"hot_agent"`` (one agent id is overrepresented in drift
        sources). New detectors register new kinds without coordinating
        with the type module.
    summary:
        One-line human-readable description for the journal.
    detail:
        Kind-specific structured payload. String-valued for JSON
        cleanliness; consumers (the proposer) parse known fields per
        kind and ignore the rest.
    affected_mutation_ids:
        Suggested mutation points the proposer might target if it
        chooses to address this pattern. The proposer is not required
        to act on the suggestion — patterns are advisory, not
        prescriptive.
    severity:
        Detector-assigned severity. Same scale as drift severity for
        consistency in journal rendering.
    """

    id: str
    kind: str
    summary: str
    detail: Mapping[str, str]
    affected_mutation_ids: tuple[str, ...] = ()
    severity: Literal["info", "warning", "critical"] = "info"


# ---------------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------------


#: The model-agnostic LLM-call shape used everywhere in zicato.
#:
#: Mirrors goldfive's call_llm surface: ``(system, user, model) ->
#: response``. The ``model`` parameter is a free-form string the caller
#: passes through; concrete implementations interpret it (route to a
#: provider, look up credentials, etc.). Zicato never inspects or
#: switches on ``model``.
CallLLM = Callable[[str, str, str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """The runtime-side parameters that bind one zicato instance.

    Fields
    ------
    instance_id:
        Identifier for this zicato instance. Distinguishes nested
        instances when an outer zicato is optimizing an inner zicato
        (the target-3 dogfood plan). v0 single-instance runs pass a
        constant (e.g. ``"default"``); future nested runs key
        workspaces, event streams, and lineage by this id.
    workspace_root:
        Absolute path to the ``.zicato/`` directory this instance
        writes under.
    harness_call_llm:
        LLM callable used BY the inner harness during runs. Zicato
        never invokes this directly; it is forwarded to the harness
        adapter at construction.
    auxiliary_call_llm:
        LLM callable used by every zicato-internal LLM consumer — the
        emulator, the proposer, the judge, the analysis pass. MUST be
        a distinct callable from :attr:`harness_call_llm` (identity-
        unequal) so the emulator cannot trivially collude with the
        inner harness through shared state.
    judge_call_llm:
        Optional LLM callable used by the in-run process judges /
        rubric matchers. ``None`` (the default) ⇒ judges fall back to
        :attr:`auxiliary_call_llm` (today's behavior). When set (from
        the workspace ``models.judge`` block) it lets an operator point
        the judges at a separate endpoint/model from the rest of the
        auxiliary surface. Read via :meth:`effective_judge_call_llm`.
    seed:
        Optional integer seed for any zicato-internal random number
        generators. Adapters may or may not honor it for the inner
        harness.
    parallelism:
        Maximum number of **board units** the tournament runner keeps
        in flight at once — i.e. "how many boards run in parallel". The
        unit of scheduling is a board unit: one per board entry. In full
        mode a board unit runs its champion (parent) and challenger
        (child) runs CONCURRENTLY, so ``parallelism`` board units mean
        up to ``2 * parallelism`` run subprocesses alive at once; in
        fast mode a unit runs only the challenger, so up to
        ``parallelism`` subprocesses. ``1`` admits one board unit at a
        time (still two concurrent subprocesses per full-mode unit).
        Values above ``1`` let the runner play several "boards" of the
        tournament hall simultaneously, bounded by an
        :class:`asyncio.Semaphore`. The real-world ceiling is almost
        always the LLM endpoint's own concurrency limit, not this
        number — size it against ``2 * parallelism`` for full mode — so
        a modest default (``4``) is a safe starting point; operators
        raise it only when the endpoint can absorb more in-flight calls.
        Must be ``>= 1``.

    Construction-time validation
    ----------------------------
    The frozen dataclass does NOT validate the two-callable rule on
    construction (frozen dataclasses cannot run interesting
    ``__post_init__`` logic against the slotted fields without
    workarounds, and we keep this dataclass cheap to construct from
    JSON+factory paths in tests). Instead, call
    :func:`zicato.core.workspace.assert_distinct_callables` from the
    construction site before handing the :class:`RuntimeConfig` to the
    runner. The runner re-checks at startup as a defense in depth.

    The one check the dataclass DOES run in :meth:`__post_init__` is the
    cheap, scalar ``parallelism >= 1`` bound: an out-of-range value is a
    plain programming error (a sub-one semaphore is meaningless) caught
    far better at construction than deep inside the runner's gather. It
    reads no callable identity and mutates no field, so it does not
    reopen the deliberately-deferred two-callable validation above.
    """

    instance_id: str
    workspace_root: Path
    harness_call_llm: CallLLM
    auxiliary_call_llm: CallLLM
    seed: int | None = None
    parallelism: int = 4
    judge_call_llm: CallLLM | None = None

    def __post_init__(self) -> None:
        """Validate the cheap scalar invariants (currently ``parallelism``)."""
        if self.parallelism < 1:
            raise ValueError(
                f"RuntimeConfig.parallelism must be >= 1, got {self.parallelism!r}; "
                "use 1 for fully sequential board execution"
            )

    def effective_judge_call_llm(self) -> CallLLM:
        """The callable judges run on: :attr:`judge_call_llm` or the auxiliary.

        Judges historically run on :attr:`auxiliary_call_llm`; a workspace
        ``models.judge`` block may override them onto a separate endpoint via
        :attr:`judge_call_llm`. This single accessor centralises that
        fall-back so every judge call site reads the same rule.
        """
        return self.judge_call_llm if self.judge_call_llm is not None else self.auxiliary_call_llm


__all__ = [
    # Mutation surface
    "MutationKind",
    "MutationPoint",
    "PatchOpKind",
    "Patch",
    # Board
    "BoardEntryKind",
    "ExpectationKind",
    "OutputScope",
    "ExpectationFiresOn",
    "Expectation",
    "JudgeMode",
    "JudgeSpec",
    "UserPersona",
    "ScriptedTurn",
    "BoardEntry",
    "validate_board_entry",
    # Telemetry / loss
    "DriftCount",
    "MetricCount",
    "MetricSeverity",
    "JudgeLoss",
    "ExpectationResult",
    "LossProfile",
    # Run record / lineage
    "RunRecord",
    "RunResult",
    # Hypothesis / experiment
    "DriftDirection",
    "DriftMagnitude",
    "ExpectedDriftMovement",
    "ExpectedMetricMovement",
    "HypothesisSpec",
    "DriftMovementActual",
    "MetricMovementActual",
    "TournamentDecision",
    "VALID_TOURNAMENT_STRUCTURES",
    "MatchOutcome",
    "TournamentStructure",
    "OutcomeRecord",
    "Experiment",
    "PriorExperiment",
    "EXPERIMENT_MEMORY_MAX_ENTRIES",
    # Proposer
    "ProposerSkill",
    "ProposerSpec",
    # Epoch / generation
    "ScoringWeights",
    "OverfittingConfig",
    "LadderConfig",
    "EpochConfig",
    "Generation",
    # Patterns
    "Pattern",
    # Runtime config
    "CallLLM",
    "RuntimeConfig",
]
