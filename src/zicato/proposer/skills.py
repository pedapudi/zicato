"""Proposer skills + spec resolution.

A *proposer* is, on disk, a directory ``proposers/<name>/`` carrying:

* ``skills/*.md`` — markdown skill modules. Each is SKILL.md-style: an
  optional YAML-ish frontmatter block (``name`` + ``description``) fenced
  by ``---`` lines, followed by a free-form markdown body. Zero or more.
* an optional ``agent.py`` — a custom proposer agent. Its *presence and
  contents* are part of the evaluation contract; this module only hashes
  it (the loading of the agent itself is a later phase).

When no proposer dir is configured the proposer is the built-in default
agent — no skills, no tools, no custom agent module. A workspace may
instead name an *external* agent through ``runtime.proposer_agent``, in
which case :mod:`zicato.proposer.external` supplies the identity and any
proposer dir contributes only its skills.

This module turns a proposer dir (or ``None``) into a hash-ready
:class:`~zicato.core.types.ProposerSpec`. The contract layer
(:func:`zicato.epoch.contract._canon_proposer`) folds that spec into the
contract hash so configuring a proposer dir — or editing one of its
skills — rolls the epoch. Skill bodies are normalized exactly like the
proposer brief (see :func:`zicato.epoch.contract._canon_brief`) so
cosmetic whitespace edits do not roll the epoch.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from zicato.core.types import ProposerSkill, ProposerSpec

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from zicato.proposer.external import ExternalProposerConfig


def normalize_skill_body(body: str) -> str:
    """Normalize a skill body the way the proposer brief is normalized.

    Mirrors :func:`zicato.epoch.contract._canon_brief`: line endings are
    folded to ``\\n``, trailing whitespace is stripped per line, and
    leading / trailing blank lines are dropped. This is what makes a
    cosmetic skill edit (re-indenting, CRLF churn, a trailing newline)
    leave the contract hash unchanged while a semantic edit moves it.
    """
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def parse_frontmatter(text: str, *, stem: str) -> tuple[str, str, str]:
    """Split ``*.md`` text into ``(name, description, body)``.

    Recognizes a leading ``---``-fenced frontmatter block and reads the
    ``name`` / ``description`` keys out of it (simple ``key: value`` lines
    — no nested YAML). Missing frontmatter is tolerated: ``name`` falls
    back to the file stem and ``description`` to the empty string, with the
    whole text treated as the body.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    name = stem
    description = ""

    if normalized.startswith("---\n"):
        rest = normalized[len("---\n") :]
        end = rest.find("\n---")
        if end != -1:
            front = rest[:end]
            # Body starts after the closing fence line.
            after = rest[end + len("\n---") :]
            body = after[1:] if after.startswith("\n") else after
            for raw_line in front.split("\n"):
                line = raw_line.strip()
                if not line or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip()
                if key == "name" and value:
                    name = value
                elif key == "description":
                    description = value
            return name, description, body

    return name, description, normalized


def load_proposer_skills(skills_dir: Path) -> tuple[ProposerSkill, ...]:
    """Load every ``*.md`` skill under ``skills_dir``, sorted by filename.

    The discovery order is the sorted filename order so the result is
    independent of filesystem enumeration / mtime. A missing or non-
    directory ``skills_dir`` yields an empty tuple. Each file is parsed for
    SKILL.md-style frontmatter (``name`` / ``description``); missing
    frontmatter falls back to ``(stem, "")``. The body is stored verbatim —
    contract canonicalization normalizes its whitespace at hash time.
    """
    if not skills_dir.is_dir():
        return ()
    skills: list[ProposerSkill] = []
    for md_path in sorted(skills_dir.glob("*.md"), key=lambda p: p.name):
        if not md_path.is_file():
            continue
        text = md_path.read_text(encoding="utf-8")
        name, description, body = parse_frontmatter(text, stem=md_path.stem)
        skills.append(ProposerSkill(name=name, description=description, body=body))
    return tuple(skills)


def resolve_proposer_spec(
    proposer_path: Path | None,
    external: ExternalProposerConfig | None = None,
) -> ProposerSpec:
    """Resolve a proposer dir (or ``None``) into a :class:`ProposerSpec`.

    ``None`` ⇒ the built-in default proposer (:meth:`ProposerSpec.default`).
    Otherwise the spec is loaded from ``<proposer_path>/``:

    * skills come from ``<proposer_path>/skills/*.md``;
    * ``agent_id`` is ``"dir:<proposer_path.name>"``;
    * ``agent_source_sha256`` is the SHA-256 of ``<proposer_path>/agent.py``
      when that file exists, else ``None``;
    * ``tools`` is empty — tool declaration is a later phase.

    ``external`` (a resolved ``runtime.proposer_agent``, absent for every
    workspace that configures none) takes precedence over both: the spec
    becomes the ``external:<label>`` identity from
    :func:`zicato.proposer.external.resolve_external_spec`, still carrying
    the proposer dir's skills when one is also configured — the external
    agent is steered by the *hashed* skills, never by its own runtime's
    parallel skill system.
    """
    skills = load_proposer_skills(proposer_path / "skills") if proposer_path is not None else ()

    if external is not None:
        from zicato.proposer.external import resolve_external_spec  # noqa: PLC0415

        return resolve_external_spec(external, skills=skills)

    if proposer_path is None:
        return ProposerSpec.default()

    agent_py = proposer_path / "agent.py"
    agent_source_sha256: str | None
    if agent_py.is_file():
        agent_source_sha256 = hashlib.sha256(agent_py.read_bytes()).hexdigest()
    else:
        agent_source_sha256 = None

    return ProposerSpec(
        agent_id=f"dir:{proposer_path.name}",
        tools=(),
        skills=skills,
        agent_source_sha256=agent_source_sha256,
    )


__all__ = [
    "normalize_skill_body",
    "parse_frontmatter",
    "load_proposer_skills",
    "resolve_proposer_spec",
]
