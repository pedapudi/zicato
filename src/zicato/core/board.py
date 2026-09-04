"""Board-entry types: kinds, expectations, judges, personas, validation.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any, Literal

from zicato.core.drift_kinds import DriftSeverity, validate_drift_kind

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

    .. warning::

       Adding a SIXTH member is a cache-invalidating change for OLDER
       readers, and it is not symmetric with adding one to
       :data:`BoardEntryKind` (which reserves forward-compat slots above
       for exactly this reason; this enum has none). ``loss.json`` stores
       the bare token, and
       :func:`~zicato.telemetry.reducer.loss_profile_from_dict` coerces it
       back to a member — so a build that does not know the new token reads
       every affected unit as a cache MISS, and on a budget-capped round
       ``scheduling._skip_unit_side`` then OVERWRITES the real measurement
       with a synthetic skip. A new member therefore needs either a
       reserved slot landed one release ahead, or an explicit decision that
       rolling back past it discards those units' evidence.
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


#: Compatibility alias. The expectation field is spelled ``reads`` and its
#: value type is the :class:`OutputScope` enum; this alias keeps
#: :data:`ExpectationFiresOn` importable for a downstream module that
#: refers to it by that name. Write :class:`OutputScope` in new code.
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
        Drift severity the judge's adverse verdict is reported at. Typed
        as :class:`zicato.core.DriftSeverity`, the string mirror of
        ``goldfive.DriftSeverity``; members of either enum compare equal
        and serialise to the same token.
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
        rigid for cheap regression testing.
    """

    user: str


@dataclass(frozen=True, slots=True)
class BoardEntry:
    """One entry in an evaluation board.

    A board is a JSONL file of :class:`BoardEntry` rows; an entry is a
    single executable evaluation under some generation of the system
    under test. Entries are kind-discriminated: the same dataclass carries
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
