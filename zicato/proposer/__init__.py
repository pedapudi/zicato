"""zicato.proposer — structured patch proposer.

The proposer is the LLM-driven half of zicato's improvement loop. Given a
generation's loss patterns, a manifest of mutation points, and an
operator-edited proposer brief, it composes an
:class:`zicato.core.types.Experiment` — an explicit, schema-validated
:class:`HypothesisSpec` joined with a tuple of concrete :class:`Patch`
instances.

The public surface is intentionally narrow:

* :func:`propose_experiment` — orchestration entry point that prompts an
  auxiliary LLM, parses its response, and validates against the live
  mutation manifest. Retries on schema failure with the parse error fed
  back to the model.
* :class:`ProposerError` — raised when the proposer exhausts retries
  without producing a schema-valid response.
* :class:`ProposerBrief` / :func:`load_brief` / :func:`enforce_forbidden`
  — operator-editable proposer guidance, parsed from a ``brief.md`` that
  lives next to the epoch's board / scoring files.
* :data:`EXPERIMENT_JSON_SCHEMA` / :func:`parse_experiment_json` — the
  JSON contract the proposer's structured output MUST conform to, plus
  the parser that lifts a raw response string into a typed
  :class:`Experiment`.

Module layout follows the rest of zicato — small, focused modules under
a re-exporting package init. Downstream callers import from
``zicato.proposer``; the internal split between ``prompts.py``,
``structured.py``, ``brief.py``, and ``proposer.py`` may evolve.
"""

from __future__ import annotations

from zicato.proposer.brief import ProposerBrief, enforce_forbidden, load_brief
from zicato.proposer.prompts import (
    SYSTEM_PROMPT_TEMPLATE,
    USER_PROMPT_TEMPLATE,
    render_mutation_block,
    render_pattern_block,
    render_system_prompt,
    render_user_prompt,
)
from zicato.proposer.proposer import ProposerError, propose_experiment
from zicato.proposer.structured import (
    EXPERIMENT_JSON_SCHEMA,
    ExperimentParseError,
    parse_experiment_json,
)

__all__ = [
    "ProposerError",
    "propose_experiment",
    "SYSTEM_PROMPT_TEMPLATE",
    "USER_PROMPT_TEMPLATE",
    "render_mutation_block",
    "render_pattern_block",
    "render_system_prompt",
    "render_user_prompt",
    "ProposerBrief",
    "load_brief",
    "enforce_forbidden",
    "EXPERIMENT_JSON_SCHEMA",
    "ExperimentParseError",
    "parse_experiment_json",
]
