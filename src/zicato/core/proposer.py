"""Proposer types: its identity, its skills, and how an episode can end.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# How a proposal episode ends
# ---------------------------------------------------------------------------

#: The four ways one proposal episode can end, which is Foe's own outcome
#: vocabulary (``foe/docs/design.md``, "The episode") read in zicato's
#: terms. A **completed** episode produced an :class:`Experiment`. A
#: **blocked** one recognized that it cannot proceed and says why with a
#: code from :data:`PROPOSER_BLOCKED_CODES`. An **exhausted** one ran out
#: of budget with work still in progress, and names the dimension that
#: ran out. A **failed** one crashed or broke the protocol.
#:
#: Blocked and exhausted are neither each other nor failures, and the
#: distinction is what makes the remedy addressable: a block is a fact
#: about the mutation surface or the brief, exhaustion is a fact about the
#: budget, and a failure is a defect.
ProposerOutcomeKind = Literal["completed", "blocked", "exhausted", "failed"]

#: Why a proposal episode reported that it cannot proceed. A closed set:
#: a supervising round routes on it, and the proposer scorecard counts it,
#: so a code that means something new is added here rather than spelled
#: freely at a call site.
#:
#: Four are zicato's own conditions. ``no-groundable-mutation-point``: no
#: declared mutation point matches the brief's failure mode.
#: ``verification-unsatisfiable``: the patch verifier's retries were spent
#: with findings still present. ``edit-outside-mutation-point``: the
#: proposer edited its scratch copy outside every declared point.
#: ``ambiguous-brief``: the brief admits incompatible readings. The rest
#: are conditions the runtime detects and reports, and each is spelled as
#: Foe spells it so a log read against either vocabulary reads the same.
ProposerBlockedCode = Literal[
    "no-groundable-mutation-point",
    "verification-unsatisfiable",
    "edit-outside-mutation-point",
    "ambiguous-brief",
    "missing-capability",
    "looping-tool-call",
    "looping-reasoning",
    "child-blocked",
    "recovery-exhausted",
    "recovery-failed",
]

#: :data:`ProposerBlockedCode` as a runtime-checkable set.
PROPOSER_BLOCKED_CODES: frozenset[str] = frozenset(
    {
        "no-groundable-mutation-point",
        "verification-unsatisfiable",
        "edit-outside-mutation-point",
        "ambiguous-brief",
        "missing-capability",
        "looping-tool-call",
        "looping-reasoning",
        "child-blocked",
        "recovery-exhausted",
        "recovery-failed",
    }
)

#: Every code Foe's closed vocabulary admits
#: (``foe/docs/log-format.md``, "Blocked codes"), mapped onto zicato's.
#: Complete by construction: a Foe release that adds a code fails the
#: coverage test rather than reaching a round as an unrouted string.
#:
#: Two mappings are readings rather than renamings. Foe's
#: ``goal-unreachable`` means the model reported the task cannot be
#: completed as stated, and the task here is to ground a change in a
#: declared mutation point, so the zicato reading is
#: ``no-groundable-mutation-point``. Foe's ``ambiguous-task`` is
#: ``ambiguous-brief``, because the brief is the task's text.
FOE_BLOCKED_CODES: Mapping[str, ProposerBlockedCode] = {
    "looping-tool-call": "looping-tool-call",
    "looping-reasoning": "looping-reasoning",
    "goal-unreachable": "no-groundable-mutation-point",
    "ambiguous-task": "ambiguous-brief",
    "missing-capability": "missing-capability",
    "verification-unsatisfiable": "verification-unsatisfiable",
    "child-blocked": "child-blocked",
    "recovery-exhausted": "recovery-exhausted",
    "recovery-failed": "recovery-failed",
}

#: The budget dimensions an exhausted episode can name, which are Foe's
#: (``foe/docs/log-format.md``, "Exhausted limits"). Recorded verbatim so
#: the scorecard can say which allowance to raise.
PROPOSER_BUDGET_DIMENSIONS: tuple[str, ...] = (
    "model_calls",
    "input_tokens",
    "output_tokens",
    "context_window",
    "seconds",
    "depth",
    "episodes",
    "concurrency",
)


@dataclass(frozen=True, slots=True)
class ProposerEpisodeOutcome:
    """How one proposal episode ended, in a shape a round can record.

    Fields
    ------
    kind:
        One of :data:`ProposerOutcomeKind`.
    code:
        For a blocked episode, a :data:`ProposerBlockedCode`. For an
        exhausted one, the budget dimension that ran out. Empty for a
        completed or failed episode.
    message:
        What the episode said about the ending, redacted the way every
        proposer-facing string is: no board-entry id and no entry text.
        Empty when the ending carried no message.
    """

    kind: ProposerOutcomeKind
    code: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# Proposer identity
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
    either side of the change are steered differently and are not directly
    comparable, so the epoch must roll. The contract hash folds
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
        ``"builtin:default"`` for the built-in agent, ``"dir:<name>"``
        when a ``proposers/<name>/agent.py`` directory backs the proposer,
        or ``"external:<label>"`` when ``runtime.proposer_agent`` names a
        non-ADK agent (:mod:`zicato.proposer.external`). The id
        distinguishes the builtin from any on-disk proposer even when the
        latter happens to carry no skills.
    tools:
        Names of the tools the proposer agent may call. Empty for the
        builtin and for an on-disk proposer; an external agent declares
        its sanctioned set, read off the same identity mapping that is
        hashed.
    skills:
        The loaded :class:`ProposerSkill` modules, sorted by name.
    agent_source_sha256:
        Hex SHA-256 of the proposer dir's ``agent.py`` when present, else
        ``None``. Folded into the contract hash so editing the custom
        agent's source rolls the epoch.
    external_path:
        The ``runtime.proposer_agent`` dotted path when an external agent
        backs the proposer, else ``None``. This is the field
        :func:`~zicato.proposer.agent.build_proposer_agent` resolves on
        first, ahead of both ADK tiers.
    external_identity_sha256:
        Hex SHA-256 of that agent's canonicalized causal surface — its
        runtime version, the bytes of the files we author for it, its
        tool set, its launch envelope (see
        :func:`zicato.proposer.external.identity_sha256`). Folded into the
        contract hash so upgrading the external runtime, or editing what
        we hand it, rolls the epoch. ``None`` for every non-external
        proposer, which is what keeps their canonical form unchanged.
    """

    agent_id: str
    tools: tuple[str, ...]
    skills: tuple[ProposerSkill, ...]
    agent_source_sha256: str | None
    external_path: str | None = None
    external_identity_sha256: str | None = None

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
