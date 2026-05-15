"""Running narrative + per-experiment persistence within an epoch.

Two concerns share this module because they sit on the same on-disk
seam (one generation directory):

* **``journal.md``** — appended one section per experiment, both
  before-the-run (the hypothesis landed) and after-the-run (the
  tournament made a decision). Plain markdown so operators read it
  directly in a terminal pager; ``zicato journal show`` is just
  ``cat`` with a friendly name.
* **``experiment.json`` + ``patches/{id}.json``** — the typed
  :class:`Experiment` for one generation. The body of
  ``experiment.json`` carries ``patch_ids: [...]``; each patch is
  serialised to its own file. Write order is patches-first so a
  partial write leaves orphan patch files (harmless) rather than a
  dangling ``patch_ids`` reference.

The split-file form is the v0 storage shape pinned in the design
memo; readers transparently accept the older inline ``patches: [...]``
form so workspaces created before the refactor keep working.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from zicato.core.types import (
    DriftMovementActual,
    ExpectedDriftMovement,
    Experiment,
    HypothesisSpec,
    OutcomeRecord,
    Patch,
)
from zicato.core.workspace import (
    experiment_json_path,
    journal_path,
    patch_json_path,
    patches_dir,
)


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


def _coerce_paths(obj: Any) -> Any:
    """Recursively stringify :class:`Path` values so :func:`json.dumps` accepts them.

    ``asdict()`` leaves :class:`Path` objects intact; :func:`json.dumps`
    cannot serialise them directly. Centralised here so writers don't
    each re-derive a custom converter.
    """
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _coerce_paths(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_coerce_paths(v) for v in obj]
    return obj


def _patch_to_dict(patch: Patch) -> dict[str, Any]:
    coerced: dict[str, Any] = _coerce_paths(asdict(patch))
    return coerced


def _patch_from_dict(d: dict[str, Any]) -> Patch:
    return Patch(
        id=str(d["id"]),
        mutation_id=str(d["mutation_id"]),
        op=d["op"],
        new_content=d.get("new_content"),
        new_numeric=(
            float(d["new_numeric"]) if d.get("new_numeric") is not None else None
        ),
        new_enum=d.get("new_enum"),
        rationale=str(d.get("rationale", "")),
    )


def _hypothesis_from_dict(d: dict[str, Any]) -> HypothesisSpec:
    movements = tuple(
        ExpectedDriftMovement(
            kind=str(m["kind"]),
            direction=m["direction"],
            magnitude=m["magnitude"],
        )
        for m in d.get("expected_drift_movements", [])
    )
    return HypothesisSpec(
        core_idea=str(d.get("core_idea", "")),
        modulating=tuple(d.get("modulating", ())),
        why=str(d.get("why", "")),
        expected_drift_movements=movements,
        expected_pass_rate_delta=str(d.get("expected_pass_rate_delta", "")),
        risks=str(d.get("risks", "")),
    )


def _outcome_from_dict(d: dict[str, Any] | None) -> OutcomeRecord | None:
    if d is None:
        return None
    movements = tuple(
        DriftMovementActual(
            kind=str(m["kind"]),
            from_rate=float(m["from_rate"]),
            to_rate=float(m["to_rate"]),
            hypothesis_match=bool(m["hypothesis_match"]),
            note=str(m.get("note", "")),
        )
        for m in d.get("drift_movements", [])
    )
    return OutcomeRecord(
        ran_at=str(d.get("ran_at", "")),
        drift_movements=movements,
        pass_rate_delta=float(d.get("pass_rate_delta", 0.0)),
        drift_loss_delta=float(d.get("drift_loss_delta", 0.0)),
        scalar_score_delta=float(d.get("scalar_score_delta", 0.0)),
        tournament_decision=d.get("tournament_decision", "rejected"),
        rejection_reason=str(d.get("rejection_reason", "")),
    )


def write_experiment(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    experiment: Experiment,
) -> None:
    """Persist an :class:`Experiment` using the per-patch storage layout.

    Layout (see :doc:`project_zicato_storage_design`)::

        generations/{generation_id}/
          patches/{patch_id}.json     # one per patch
          experiment.json             # body carries patch_ids: [...]

    Write order is patches FIRST, then ``experiment.json`` LAST. A
    crash between the two phases leaves orphan patch files (harmless;
    no reader picks them up because the ``patch_ids`` list in
    ``experiment.json`` is the authoritative source) but never a
    dangling reference to a missing patch file.

    The in-memory :class:`Experiment.patches` tuple is preserved by
    construction — only the on-disk shape is split. Round-tripping
    through :func:`read_experiment` reconstitutes the same tuple.
    """
    gen_dir = experiment_json_path(
        workspace_root, epoch_id, generation_id
    ).parent
    gen_dir.mkdir(parents=True, exist_ok=True)

    patch_ids: list[str] = []
    if experiment.patches:
        pdir = patches_dir(workspace_root, epoch_id, generation_id)
        pdir.mkdir(parents=True, exist_ok=True)
        for patch in experiment.patches:
            patch_ids.append(patch.id)
            ppath = patch_json_path(
                workspace_root, epoch_id, generation_id, patch.id
            )
            ppath.write_text(
                json.dumps(_patch_to_dict(patch), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    body: dict[str, Any] = {
        "id": experiment.id,
        "epoch_id": experiment.epoch_id,
        "generation_id": experiment.generation_id,
        "parent_generation_id": experiment.parent_generation_id,
        "proposed_at": experiment.proposed_at,
        "hypothesis": _coerce_paths(asdict(experiment.hypothesis)),
        "patch_ids": patch_ids,
        "outcome": (
            _coerce_paths(asdict(experiment.outcome))
            if experiment.outcome is not None
            else None
        ),
    }
    target = experiment_json_path(workspace_root, epoch_id, generation_id)
    target.write_text(
        json.dumps(body, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def update_experiment_outcome(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    outcome: OutcomeRecord,
) -> Experiment:
    """Re-read the experiment, replace its outcome, and write back.

    The tournament runner / orchestrator use this after a tournament
    has produced a decision: the proposer-side experiment already
    landed on disk (with ``outcome=None``); this helper preserves the
    hypothesis, patches, and timestamps while atomically updating the
    outcome field. The per-patch files are NOT rewritten — only
    ``experiment.json`` is touched.

    Returns the updated :class:`Experiment` so callers can journal it
    in one swoop.
    """
    existing = read_experiment(workspace_root, epoch_id, generation_id)
    from dataclasses import replace as _replace  # noqa: PLC0415

    updated = _replace(existing, outcome=outcome)
    # We intentionally do NOT re-write the patches/*.json files;
    # write_experiment will rewrite experiment.json with the same
    # patch_ids list it had before.
    write_experiment(workspace_root, epoch_id, generation_id, updated)
    return updated


def read_experiment(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
) -> Experiment:
    """Reconstitute an :class:`Experiment` from the per-generation directory.

    Accepts BOTH on-disk shapes for backward compatibility:

    * **New (per-patch files)** — ``experiment.json`` carries
      ``patch_ids: [...]`` and each id resolves to
      ``patches/{id}.json``.
    * **Legacy (inline patches)** — ``experiment.json`` carries
      ``patches: [{...}, ...]`` directly. We pull the patches from the
      inline list without touching the patches directory.

    The returned :class:`Experiment` is identical in shape regardless
    of which on-disk form produced it. New writes always use the
    per-patch layout.
    """
    target = experiment_json_path(workspace_root, epoch_id, generation_id)
    if not target.exists():
        raise FileNotFoundError(
            f"experiment.json not found at {target}"
        )
    body = json.loads(target.read_text(encoding="utf-8"))

    raw_inline = body.get("patches")
    patch_ids = body.get("patch_ids")

    patches: list[Patch] = []
    if isinstance(raw_inline, list):
        # Legacy inline form — used by workspaces created before the
        # per-patch refactor landed. We tolerate the old shape so
        # existing on-disk data does not break.
        for d in raw_inline:
            patches.append(_patch_from_dict(d))
    elif isinstance(patch_ids, list):
        for pid in patch_ids:
            ppath = patch_json_path(
                workspace_root, epoch_id, generation_id, str(pid)
            )
            if not ppath.exists():
                raise FileNotFoundError(
                    f"patch file {ppath} referenced by experiment.json is missing"
                )
            patches.append(
                _patch_from_dict(json.loads(ppath.read_text(encoding="utf-8")))
            )

    hypothesis = _hypothesis_from_dict(body.get("hypothesis") or {})
    outcome = _outcome_from_dict(body.get("outcome"))

    return Experiment(
        id=str(body.get("id", "")),
        epoch_id=str(body.get("epoch_id", epoch_id)),
        generation_id=str(body.get("generation_id", generation_id)),
        parent_generation_id=str(body.get("parent_generation_id", "")),
        proposed_at=str(body.get("proposed_at", "")),
        hypothesis=hypothesis,
        patches=tuple(patches),
        outcome=outcome,
    )


__all__ = [
    "append_journal_entry",
    "read_journal",
    "write_experiment",
    "read_experiment",
    "update_experiment_outcome",
]
