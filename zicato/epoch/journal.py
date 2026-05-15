"""Running narrative of every experiment within an epoch.

``journal.md`` lives under ``{workspace}/epochs/{id}/journal.md`` and is
appended on every experiment write — both before-the-run (the hypothesis
landed) and after-the-run (the tournament made a decision). The format
is plain markdown so operators read it directly in a terminal pager;
``zicato journal show`` is just ``cat`` with a friendly name.

This module is intentionally minimal: append one section per call,
render whatever outcome information has been populated. Schema-rich
artifacts live in ``experiment.json``; the journal is the prose surface.
"""

from __future__ import annotations

from pathlib import Path

from zicato.core.types import Experiment, OutcomeRecord
from zicato.core.workspace import journal_path


def _first_sentence(text: str) -> str:
    """Best-effort first-sentence extraction for the ``why`` field.

    We accept either a period-terminated sentence, a newline break, or
    the entire string if nothing else terminates. Whitespace is trimmed.
    """
    text = text.strip()
    if not text:
        return ""
    for sep in [". ", ".\n", "\n", "."]:
        idx = text.find(sep)
        if idx >= 0:
            return text[:idx].strip()
    return text


def _version_label(generation_id: str) -> str:
    """Render ``v3`` as ``v3``; otherwise prefix.

    The convention is that generation ids start with ``v`` (``v0``,
    ``v1``, ...), but ``Experiment.generation_id`` is a free string
    field — adapters that name generations differently still want a
    legible journal heading. If the id already begins with ``v`` we
    keep it; otherwise we wrap it in backticks to look stable.
    """
    if generation_id.startswith("v") and generation_id[1:].lstrip("0123456789") == "":
        return generation_id
    return f"`{generation_id}`"


def _format_outcome(outcome: OutcomeRecord) -> str:
    """Render the post-decision line of a journal entry."""
    pass_part = f"Δpass_rate={outcome.pass_rate_delta:+.3f}"
    scalar_part = f"Δscalar={outcome.scalar_score_delta:+.3f}"
    drift_part = f"Δdrift_loss={outcome.drift_loss_delta:+.3f}"
    return (
        f"**outcome**: {outcome.tournament_decision} "
        f"({scalar_part}, {drift_part}, {pass_part})"
    )


def _render_section(experiment: Experiment) -> str:
    """Render one journal section in canonical markdown form.

    Format:
        ## v{N} — {one-line core_idea}
        **proposed_at**: {ts}
        **modulating**: id1, id2, ...
        **why**: {first sentence of why}
        **outcome**: {decision} (Δscalar=..., Δdrift_loss=..., Δpass_rate=...)
        **rejection_reason**: ... (only when rejected)

    Missing-outcome experiments render just the proposed_at/modulating/why
    triple. The tournament runner re-renders the same section once
    outcome is populated; appending twice is fine — operators see the
    proposal then the verdict.
    """
    label = _version_label(experiment.generation_id)
    core = experiment.hypothesis.core_idea.strip().splitlines()[0]

    lines: list[str] = []
    lines.append(f"## {label} — {core}")
    lines.append("")
    lines.append(f"**proposed_at**: {experiment.proposed_at}")
    if experiment.hypothesis.modulating:
        lines.append(
            "**modulating**: " + ", ".join(experiment.hypothesis.modulating)
        )
    else:
        lines.append("**modulating**: (none)")
    why = _first_sentence(experiment.hypothesis.why)
    if why:
        lines.append(f"**why**: {why}")
    if experiment.outcome is not None:
        lines.append(_format_outcome(experiment.outcome))
        if (
            experiment.outcome.tournament_decision == "rejected"
            and experiment.outcome.rejection_reason
        ):
            lines.append(
                f"**rejection_reason**: {experiment.outcome.rejection_reason}"
            )
    lines.append("")
    return "\n".join(lines)


def append_journal_entry(
    workspace_root: Path, epoch_id: str, experiment: Experiment
) -> None:
    """Append a markdown section for ``experiment`` to the epoch's journal.

    Creates the file if it does not yet exist; otherwise appends with a
    leading newline so consecutive sections do not run together. The
    epoch directory MUST already exist — the caller is responsible for
    having created it via :func:`zicato.epoch.lifecycle.new_epoch`.
    """
    path = journal_path(workspace_root, epoch_id)
    if not path.parent.exists():
        raise FileNotFoundError(
            f"epoch directory {path.parent} does not exist; create it with new_epoch first"
        )
    section = _render_section(experiment)
    if path.exists() and path.stat().st_size > 0:
        existing = path.read_text()
        if not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + section)
    else:
        path.write_text(section)


def read_journal(workspace_root: Path, epoch_id: str) -> str:
    """Return the epoch's full journal text, or an empty string if missing."""
    path = journal_path(workspace_root, epoch_id)
    if not path.exists():
        return ""
    return path.read_text()


__all__ = [
    "append_journal_entry",
    "read_journal",
]
