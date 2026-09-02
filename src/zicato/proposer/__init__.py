"""zicato.proposer — how a candidate is invented, and what it must look like.

The proposer is the generative half of zicato's improvement loop. Given a
generation's loss patterns, a manifest of mutation points and an
operator-edited proposer brief, it produces an
:class:`zicato.core.types.Experiment` — a schema-validated
:class:`HypothesisSpec` joined with concrete :class:`Patch` instances.

One runtime produces it. :class:`~zicato.proposer.foe_agent.FoeProposerAgent`
runs each candidate as one Foe episode: the episode edits a disposable
copy of the parent snapshot, checks its work, and returns the hypothesis
that explains the change, and zicato reads the copy back as the patch
set. ``docs/design/PROPOSER.md`` states the ownership boundary and the
trust boundary an operator's own class would run under.

The public surface is intentionally narrow:

* :class:`ProposerAgent` / :func:`build_proposer_agent` — the one-method
  seam the round drives, and the builder that resolves a workspace's
  declared proposer to the class implementing it.
* :class:`ProposerError` — raised when a proposal produced no experiment.
  :class:`ProposerBlocked` and :class:`ProposerExhausted` are the two
  endings that are refusals rather than failures.
* :class:`ProposerBrief` / :func:`load_brief` / :func:`enforce_forbidden`
  — operator-editable proposer guidance, parsed from a ``brief.md`` that
  lives next to the epoch's board / scoring files.
* :data:`EXPERIMENT_JSON_SCHEMA` / :func:`parse_experiment_json` — the
  JSON contract a proposal must conform to, plus the parser that lifts a
  raw response string into a typed :class:`Experiment`.

Module layout follows the rest of zicato — small, focused modules under
a re-exporting package init. Downstream callers import from
``zicato.proposer``; the internal split may evolve.
"""

from __future__ import annotations

from zicato.proposer.agent import (
    ProposerAgent,
    ProposerContext,
    build_proposer_agent,
)
from zicato.proposer.brief import ProposerBrief, enforce_forbidden, load_brief
from zicato.proposer.prompts import (
    render_mutation_block,
    render_pattern_block,
    render_skills_block,
)
from zicato.proposer.proposer import (
    ExperimentValidator,
    ProposerBlocked,
    ProposerError,
    ProposerExhausted,
)
from zicato.proposer.skills import resolve_proposer_spec
from zicato.proposer.structured import (
    EXPERIMENT_JSON_SCHEMA,
    ExperimentParseError,
    PostApplyValidationError,
    parse_experiment_json,
)

__all__ = [
    "EXPERIMENT_JSON_SCHEMA",
    "ExperimentParseError",
    "ExperimentValidator",
    "PostApplyValidationError",
    "ProposerAgent",
    "ProposerBlocked",
    "ProposerBrief",
    "ProposerContext",
    "ProposerError",
    "ProposerExhausted",
    "build_proposer_agent",
    "enforce_forbidden",
    "load_brief",
    "parse_experiment_json",
    "render_mutation_block",
    "render_pattern_block",
    "render_skills_block",
    "resolve_proposer_spec",
]
