"""At-epoch-close analysis pass.

Generates ``analysis.md`` for an epoch by handing the auxiliary LLM:

* the running ``journal.md``,
* every ``experiment.json`` written under the epoch's ``generations/``,
* an optional patterns snapshot if one is available on disk.

The LLM is asked to produce a fixed-structure markdown document with the
sections enumerated in :data:`REQUIRED_SECTIONS`. We do NOT parse or
re-validate the result beyond writing it through; downstream tooling
that wants structure should read from ``experiment.json`` and
``journal.md`` directly.

The pass is **bounded**: we cap the journal slice and per-experiment
detail we inline into the prompt so the call is predictable. Operators
who need a fuller retrospective can re-run the pass with a larger budget
by setting environment knobs (left for a later patch — the function
takes a ``model`` arg today for forward compat).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from zicato.core.workspace import (
    analysis_path,
    epoch_dir,
    journal_path,
)

# A goldfive-compatible auxiliary call_llm.
_AuxCallLLM = Callable[[str, str, str], Awaitable[str]]

REQUIRED_SECTIONS: tuple[str, ...] = (
    "Headline movements",
    "Hypotheses that held",
    "Hypotheses that didn't",
    "Surface still open at epoch close",
    "Recommended focus for next epoch",
)


# Soft caps so the prompt size stays predictable. These are intentionally
# generous — operators will overflow them only on multi-week epochs.
_MAX_JOURNAL_CHARS = 60_000
_MAX_PATTERNS_CHARS = 20_000
_MAX_EXPERIMENT_CHARS = 4_000
_MAX_EXPERIMENTS_INLINE = 50


_SYSTEM_PROMPT = """\
You are an expert reviewer summarizing one epoch of an automated agent
optimization loop. You will receive:

  * the running narrative journal for the epoch,
  * a structured list of every experiment that ran (hypothesis + outcome),
  * optionally, a patterns snapshot summarising drift observations.

Your job is to write `analysis.md` — a retrospective the operator will
read between epochs. Be specific. Cite generation ids when relevant.
Prefer concrete observations over generalities.

You MUST produce exactly these sections, in this order, as markdown
level-2 headings:

  ## Headline movements
  ## Hypotheses that held
  ## Hypotheses that didn't
  ## Surface still open at epoch close
  ## Recommended focus for next epoch

Open with a single level-1 heading naming the epoch (e.g.
`# Epoch analysis: 2026-04-08_hardened_research`). Do not invent
sections that are not in the list above; do not omit any of them. Each
section may be a short paragraph or bullet list — whichever fits the
material.
"""


def _slice(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n... [truncated for analysis pass]"


def _collect_experiments(workspace_root: Path, epoch_id: str) -> list[dict]:
    """Read every ``experiment.json`` under the epoch's ``generations/``.

    Returns dicts in lineage order (sorted by generation id). Files that
    fail to parse are skipped silently — they predate the experiment
    schema we want to summarise.
    """
    gens_root = epoch_dir(workspace_root, epoch_id) / "generations"
    if not gens_root.exists():
        return []
    out: list[dict] = []
    for gen_dir in sorted(gens_root.iterdir()):
        if not gen_dir.is_dir():
            continue
        path = gen_dir / "experiment.json"
        if not path.exists():
            continue
        try:
            d = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            d.setdefault("generation_id", gen_dir.name)
            out.append(d)
    return out


def _collect_patterns_snapshot(workspace_root: Path, epoch_id: str) -> str:
    """Aggregate ``patterns/round_*.json`` files into a single text blob.

    Returns the empty string when there is no patterns directory; this
    is the common case in v0 (pattern detection lands in a later patch).
    """
    patterns_dir = epoch_dir(workspace_root, epoch_id) / "patterns"
    if not patterns_dir.exists():
        return ""
    parts: list[str] = []
    for path in sorted(patterns_dir.glob("*.json")):
        try:
            d = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        parts.append(f"### {path.stem}\n{json.dumps(d, indent=2, sort_keys=True)}")
    return "\n\n".join(parts)


def _format_experiment(d: dict) -> str:
    """Compact one experiment dict down to its journal-relevant fields."""
    keep: dict = {}
    for k in (
        "id",
        "generation_id",
        "parent_generation_id",
        "proposed_at",
        "hypothesis",
        "outcome",
    ):
        if k in d:
            keep[k] = d[k]
    return _slice(json.dumps(keep, indent=2, sort_keys=True), _MAX_EXPERIMENT_CHARS)


def _compose_user_prompt(
    epoch_id: str,
    journal_text: str,
    experiments: list[dict],
    patterns_text: str,
) -> str:
    """Assemble the prompt body. Order: journal, experiments, patterns."""
    chunks: list[str] = []
    chunks.append(f"# Epoch under review: {epoch_id}")
    chunks.append("")
    chunks.append("## Journal")
    chunks.append("")
    chunks.append("```")
    chunks.append(_slice(journal_text or "(no journal entries)", _MAX_JOURNAL_CHARS))
    chunks.append("```")
    chunks.append("")
    chunks.append("## Experiments")
    if not experiments:
        chunks.append("")
        chunks.append("(no experiments recorded for this epoch)")
    else:
        head = experiments[:_MAX_EXPERIMENTS_INLINE]
        for exp in head:
            chunks.append("")
            chunks.append("```json")
            chunks.append(_format_experiment(exp))
            chunks.append("```")
        if len(experiments) > len(head):
            chunks.append("")
            chunks.append(
                f"... [{len(experiments) - len(head)} additional experiments "
                "omitted from the prompt for size]"
            )
    if patterns_text:
        chunks.append("")
        chunks.append("## Patterns")
        chunks.append("")
        chunks.append(_slice(patterns_text, _MAX_PATTERNS_CHARS))
    chunks.append("")
    chunks.append(
        "Produce `analysis.md` now. Follow the section structure described "
        "in the system prompt exactly."
    )
    return "\n".join(chunks)


async def generate_analysis(
    workspace_root: Path,
    epoch_id: str,
    aux_call_llm: _AuxCallLLM,
    model: str = "",
) -> Path:
    """Run the analysis pass and write ``analysis.md``.

    Returns the path to the written file. The caller is responsible for
    arranging that ``aux_call_llm`` is the AUXILIARY callable (not the
    inner-harness one) — see :class:`RuntimeConfig` and
    :func:`assert_distinct_callables` for the collusion guard.

    The function is async because the LLM call is. Callers in synchronous
    contexts wrap with ``asyncio.run``; ``close_epoch`` does this for the
    common path.
    """
    journal_text = ""
    jpath = journal_path(workspace_root, epoch_id)
    if jpath.exists():
        journal_text = jpath.read_text()

    experiments = _collect_experiments(workspace_root, epoch_id)
    patterns_text = _collect_patterns_snapshot(workspace_root, epoch_id)

    user_prompt = _compose_user_prompt(
        epoch_id=epoch_id,
        journal_text=journal_text,
        experiments=experiments,
        patterns_text=patterns_text,
    )

    response = await aux_call_llm(_SYSTEM_PROMPT, user_prompt, model)
    out_path = analysis_path(workspace_root, epoch_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(response)
    return out_path


__all__ = [
    "REQUIRED_SECTIONS",
    "generate_analysis",
]
