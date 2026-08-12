"""Proposer types: the resolved proposer identity + its markdown skills.

Split out of :mod:`zicato.core.types`; re-exported from there and from
:mod:`zicato.core` so existing import paths keep working.
"""

from __future__ import annotations

from dataclasses import dataclass

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
