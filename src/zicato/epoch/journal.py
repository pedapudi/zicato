"""Canonical experiment and patch records, plus the human-readable journal.

The experiment body references its patches by identifier; each patch occupies
its own file. Writes publish patches before the experiment, using the storage
backend's atomic writes. An absent outcome means execution has not recorded one.

All readers use this owner to accept the writer's complete record format.
Missing records are distinct from malformed present records. Typed readers
resolve patch references; body readers preserve the accepted JSON for views.
The markdown journal records the full hypothesis and outcome, without trimming
reasoning at publication time.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, TypeVar

from zicato.core.configuration import authored_dataclass_from_json
from zicato.core.types import (
    ExpectedMetricMovement,
    Experiment,
    HypothesisSpec,
    MatchOutcome,
    MetricMovementActual,
    OutcomeRecord,
    Patch,
)
from zicato.core.workspace import epoch_dir
from zicato.epoch._storage import (
    RECORD_FORMAT_VERSION,
    RecordError,
    check_record_format,
    experiment_key,
    journal_key,
    patch_key,
)
from zicato.storage import StorageBackend, workspace_backend
from zicato.workspace import WorkspaceLayout, generation_ids, generation_round_number

#: What one epoch-wide walk of the generation records yields per generation:
#: the typed record, or the stored body the views serve.
_RecordT = TypeVar("_RecordT")


class ExperimentRecordError(RecordError):
    """A present ``experiment.json`` (or a patch it references) does not parse.

    Distinct from :class:`FileNotFoundError`, which the readers here keep
    for the one legal absence: a generation whose record has not been
    written yet. Every message names the record and its storage key, so a
    view catching :class:`~zicato.epoch._storage.RecordError` at its own
    boundary can render the reason rather than a blank.
    """


@dataclass(frozen=True, slots=True)
class PatchRecord:
    """The patches recorded for one generation, in application order."""

    generation_id: str
    patches: tuple[Patch, ...]


@dataclass(frozen=True, slots=True)
class ExperimentContents:
    """An accepted experiment body and the patches that body references."""

    body: dict[str, Any]
    patches: tuple[Patch, ...]


def _field(name: str, text: str) -> str:
    """Render one ``**name**: value`` field, preserving ``text`` in full.

    Single-line values stay inline, so an ordinary entry renders as one line
    per field. A value carrying newlines is fenced by a blank line on BOTH
    sides and becomes its own paragraph, so the markdown still renders and the
    bytes survive verbatim. The trailing blank line is load-bearing: without it
    the FOLLOWING field (``**why**``, ``**outcome**``) is only a line break
    away from the body and markdown folds it into the same paragraph, so a
    multi-line ``core_idea`` would visually swallow the field after it.

    Nothing is dropped here. ``journal.md`` is append-only and is the one
    durable surface a round's reasoning is read back from, so a truncation
    at write time is permanent; budget-limited consumers cap on READ
    instead (the analysis and report readers).
    """
    text = text.strip()
    if "\n" not in text:
        return f"**{name}**: {text}"
    return f"**{name}**:\n\n{text}\n"


def _version_label(generation_id: str) -> str:
    """Render a journal heading for one generation id.

    The convention is that generation ids are ``v`` followed by the round
    number (``v0``, ``v1``, ...), but ``Experiment.generation_id`` is a free
    string field — an adapter that names generations differently still wants a
    legible heading. An id following the convention is rendered as it stands;
    any other is wrapped in backticks to look stable.
    """
    if generation_round_number(generation_id) is not None:
        return generation_id
    return f"`{generation_id}`"


def _format_outcome(outcome: OutcomeRecord) -> str:
    """Render the post-decision line of a journal entry."""
    pass_part = f"Δpass_rate={outcome.pass_rate_delta:+.3f}"
    scalar_part = f"Δscalar={outcome.scalar_score_delta:+.3f}"
    drift_part = f"Δdrift_loss={outcome.drift_loss_delta:+.3f}"
    return (
        f"**outcome**: {outcome.tournament_decision} " f"({scalar_part}, {drift_part}, {pass_part})"
    )


def _render_section(experiment: Experiment) -> str:
    """Render one journal section in canonical markdown form.

    Format:
        ## v{N} — {first line of core_idea}
        **proposed_at**: {ts}
        **modulating**: id1, id2, ...
        **core_idea**: {full core_idea — only when it spans >1 line}
        **why**: {full why}
        **outcome**: {decision} (Δscalar=..., Δdrift_loss=..., Δpass_rate=...)
        **rejection_reason**: ... (only when rejected)

    The heading stays one line so it remains a legible markdown heading;
    a ``core_idea`` with more lines than that repeats in full as its own
    field rather than losing everything past line one (issue #123).

    Missing-outcome experiments render just the proposed_at/modulating/why
    triple. The tournament runner re-renders the same section once
    outcome is populated; appending twice is fine — operators see the
    proposal then the verdict.
    """
    label = _version_label(experiment.generation_id)
    core_idea = experiment.hypothesis.core_idea.strip()
    core_lines = core_idea.splitlines()
    heading = core_lines[0] if core_lines else ""

    lines: list[str] = []
    lines.append(f"## {label} — {heading}")
    lines.append("")
    lines.append(f"**proposed_at**: {experiment.proposed_at}")
    if experiment.hypothesis.modulating:
        lines.append("**modulating**: " + ", ".join(experiment.hypothesis.modulating))
    else:
        lines.append("**modulating**: (none)")
    if len(core_lines) > 1:
        lines.append(_field("core_idea", core_idea))
    why = experiment.hypothesis.why.strip()
    if why:
        lines.append(_field("why", why))
    if experiment.outcome is not None:
        lines.append(_format_outcome(experiment.outcome))
        if (
            experiment.outcome.tournament_decision == "rejected"
            and experiment.outcome.rejection_reason
        ):
            lines.append(f"**rejection_reason**: {experiment.outcome.rejection_reason}")
    lines.append("")
    return "\n".join(lines)


def append_journal_entry(workspace_root: Path, epoch_id: str, experiment: Experiment) -> None:
    """Append a markdown section for ``experiment`` to the epoch's journal.

    Creates the file if it does not yet exist; otherwise appends with a
    leading newline so consecutive sections do not run together. The
    epoch directory MUST already exist — the caller is responsible for
    having created it via :func:`zicato.epoch.lifecycle.new_epoch`.

    The journal is plain markdown, not JSONL, so the append is a
    read-modify-write of the whole text through the storage backend's
    atomic :meth:`~zicato.storage.StorageBackend.write_text`. A crash
    mid-write leaves the prior journal intact rather than a truncated
    file.
    """
    edir = epoch_dir(workspace_root, epoch_id)
    if not edir.exists():
        raise FileNotFoundError(
            f"epoch directory {edir} does not exist; create it with new_epoch first"
        )
    backend = workspace_backend(workspace_root, start=False)
    key = journal_key(epoch_id)
    section = _render_section(experiment)
    existing = backend.read_text(key)
    if existing:
        if not existing.endswith("\n"):
            existing += "\n"
        backend.write_text(key, existing + section)
    else:
        backend.write_text(key, section)


def append_journal_entry_once(
    workspace_root: Path,
    epoch_id: str,
    experiment: Experiment,
    *,
    settlement_identity: str,
) -> None:
    """Append a settled experiment once under a stable settlement identity.

    Field settlement can replay after any interrupted write. The journal
    section therefore carries a machine-readable HTML comment whose identity
    comes from the durable settlement intent and candidate generation. The
    marker and section land in one atomic journal replacement, so a replay
    either finds the complete entry or appends it once.
    """
    if not settlement_identity or any(c in settlement_identity for c in ('"', "\n", "\r")):
        raise ValueError("settlement_identity must be non-empty and contain no quotes or newlines")
    edir = epoch_dir(workspace_root, epoch_id)
    if not edir.exists():
        raise FileNotFoundError(
            f"epoch directory {edir} does not exist; create it with new_epoch first"
        )
    backend = workspace_backend(workspace_root, start=False)
    key = journal_key(epoch_id)
    marker = f'<!-- zicato:field-settlement identity="{settlement_identity}" -->'
    existing = backend.read_text(key) or ""
    if marker in existing:
        return
    section = marker + "\n" + _render_section(experiment)
    if existing:
        if not existing.endswith("\n"):
            existing += "\n"
        section = existing + section
    backend.write_text(key, section)


def read_journal(workspace_root: Path, epoch_id: str) -> str:
    """Return the epoch's full journal text, or an empty string if missing."""
    return workspace_backend(workspace_root, start=False).read_text(journal_key(epoch_id)) or ""


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
    if isinstance(obj, list | tuple):
        return [_coerce_paths(v) for v in obj]
    return obj


def patch_body(patch: Patch) -> dict[str, Any]:
    """Encode a :class:`Patch` as the body of its ``patches/{id}.json``.

    The single encoder for the patch record. A projection that serves a
    patch on the wire builds it from the typed value through this, so the
    served shape and the persisted shape cannot drift apart.
    """
    coerced: dict[str, Any] = _coerce_paths(asdict(patch))
    return coerced


def _complete_fields(d: dict[str, Any], record_type: type[Any]) -> None:
    """Require the fields emitted by the journal's dataclass encoder."""
    expected = {field.name for field in fields(record_type)}
    if not isinstance(d, dict) or set(d) != expected:
        raise ValueError(f"{record_type.__name__} must carry exactly {sorted(expected)}")


def _patch_from_dict(d: dict[str, Any]) -> Patch:
    _complete_fields(d, Patch)
    patch = authored_dataclass_from_json(Patch, d, path="patch")
    selected = {"replace": "new_content", "set_numeric": "new_numeric", "set_enum": "new_enum"}[
        d["op"]
    ]
    for key in ("new_content", "new_numeric", "new_enum"):
        if key != selected and d[key] is not None:
            raise ValueError(f"patch {d['op']} cannot supply {key}")
    if d[selected] is None:
        raise ValueError(f"patch {d['op']} requires {selected}")
    return patch


def _hypothesis_from_dict(d: dict[str, Any]) -> HypothesisSpec:
    _complete_fields(d, HypothesisSpec)
    hypothesis = authored_dataclass_from_json(HypothesisSpec, d, path="hypothesis")
    for m in d["expected_metric_movements"]:
        _complete_fields(m, ExpectedMetricMovement)
        if not m["metric_name"]:
            raise ValueError("prediction must name a metric")
    return hypothesis


def _outcome_from_dict(d: dict[str, Any] | None) -> OutcomeRecord | None:
    """Decode a completed outcome; null means execution has no outcome yet."""
    if d is None:
        return None
    _complete_fields(d, OutcomeRecord)
    outcome = authored_dataclass_from_json(OutcomeRecord, d, path="outcome")
    for m in d["metric_movements"]:
        _complete_fields(m, MetricMovementActual)
        if not m["metric_name"]:
            raise ValueError("movement must name a metric")
    for m in d["match_record"]:
        _complete_fields(m, MatchOutcome)
    return outcome


def outcome_from_dict(d: dict[str, Any]) -> OutcomeRecord:
    """Hydrate the complete persisted outcome payload used by recovery."""
    outcome = _outcome_from_dict(d)
    if outcome is None:  # pragma: no cover - the public contract excludes None
        raise ValueError("outcome payload must be a JSON object")
    return outcome


def write_seed_experiment(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str = "v0",
    *,
    proposed_at: str = "",
) -> bool:
    """Idempotently write a synthetic ``experiment.json`` for the seed generation.

    The v0 seed is not a proposer experiment — it is the initial workspace
    snapshot the epoch starts from. Downstream consumers (the analyzer
    report data loader, the index dual-write, the dashboard lineage
    walker) all expect every generation directory to carry an
    ``experiment.json``. A seed-shaped marker keeps the on-disk shape
    uniform without inventing tournament numbers.

    Marker shape:
      * ``id``: ``"exp_{epoch}_{generation}"`` — same convention as a
        proposer experiment.
      * ``parent_generation_id``: ``None`` — the seed has no parent within
        the epoch (cross-epoch lineage lives in ``lineage.json``). Written
        as JSON ``null``; an on-disk ``""`` reads back as ``None``.
      * ``hypothesis.core_idea``: ``"baseline seed"`` — terse and stable.
      * ``outcome``: ``None`` — the seed never ran a tournament round, so
        no realised deltas exist. Loaders detect this and render the
        baseline row with empty deltas + decision ``"baseline"``.

    The write is idempotent: if ``experiment.json`` already exists at the
    target path, the helper returns ``False`` without rewriting. Returns
    ``True`` when a new marker is written.

    ``proposed_at`` defaults to empty; callers that have a meaningful
    timestamp (e.g. the workspace ``create_at``, or the epoch's
    ``created_at``) pass it through verbatim.
    """
    backend = workspace_backend(workspace_root, start=False)
    key = experiment_key(epoch_id, generation_id)
    if backend.read_text(key) is not None:
        return False

    seed = Experiment(
        id=f"exp_{epoch_id}_{generation_id}",
        epoch_id=epoch_id,
        generation_id=generation_id,
        parent_generation_id=None,
        proposed_at=proposed_at,
        hypothesis=HypothesisSpec(
            core_idea="baseline seed",
            modulating=(),
            why="",
            expected_pass_rate_delta="",
            risks="",
        ),
        patches=(),
        outcome=None,
    )
    write_experiment(workspace_root, epoch_id, generation_id, seed)
    return True


def experiment_body(experiment: Experiment) -> dict[str, Any]:
    """Encode an :class:`Experiment` as the body of its ``experiment.json``.

    The single encoder for the record, and the inverse of
    :func:`read_experiment_from_backend`: for any record this module
    wrote, ``experiment_body(read_experiment(...))`` reproduces the file's
    parsed body key for key. The patches themselves are NOT in the body —
    it carries their ids, and each patch is its own record — so an encode
    of a decode is faithful only alongside the sibling patch files the
    decode resolved.

    A projection that starts from a typed value in hand builds its wire
    shape through this rather than re-opening the file. A projection of a
    record ON DISK takes :func:`read_experiment_body` instead: a body this
    module did not write can carry fields the type does not model and omit
    ones it defaults, and an encode of its decode would quietly answer for
    both.
    """
    body: dict[str, Any] = {
        # Readers require this explicit format version before decoding the record.
        "format_version": RECORD_FORMAT_VERSION,
        "id": experiment.id,
        "epoch_id": experiment.epoch_id,
        "generation_id": experiment.generation_id,
        "parent_generation_id": experiment.parent_generation_id,
        "proposed_at": experiment.proposed_at,
        "round_index": experiment.round_index,
        "hypothesis": _coerce_paths(asdict(experiment.hypothesis)),
        "patch_ids": [patch.id for patch in experiment.patches],
        "outcome": (
            _coerce_paths(asdict(experiment.outcome)) if experiment.outcome is not None else None
        ),
    }
    # Recombination provenance — CONDITIONAL key: emitted only when
    # non-empty, so every ordinary (non-recombined) experiment.json is
    # byte-identical to one written before the field existed (the default-off
    # byte-identity proof). A recombined mint carries the ascending-gid tuple
    # of its two rejected parents; the reader defaults absent → ().
    if experiment.recombined_from:
        body["recombined_from"] = list(experiment.recombined_from)
    return body


def write_experiment(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    experiment: Experiment,
) -> None:
    """Persist an :class:`Experiment` using the per-patch storage layout.

    Layout (see ``docs/design/STORAGE.md`` §5.1)::

        generations/{generation_id}/
          patches/{patch_id}.json     # one per patch
          experiment.json             # body carries patch_ids: [...]

    Write order is patches FIRST, then ``experiment.json`` LAST. A
    crash between the two phases leaves orphan patch files (harmless;
    no reader picks them up because the ``patch_ids`` list in
    ``experiment.json`` is the authoritative source) but never a
    dangling reference to a missing patch file. Each individual write
    is itself atomic — routed through the storage backend's ``.tmp`` +
    ``fsync`` + rename discipline — so no single file is ever observed
    half-written either.

    The in-memory :class:`Experiment.patches` tuple is preserved by
    construction — only the on-disk shape is split. Round-tripping
    through :func:`read_experiment` reconstitutes the same tuple.
    """
    from zicato.workspace.projection import mark_epoch_changed  # noqa: PLC0415

    mark_epoch_changed(workspace_root, epoch_id)
    backend = workspace_backend(workspace_root, start=False)

    for patch in experiment.patches:
        # Each patch file is written as text (not write_json) so the
        # trailing newline of the pre-seam on-disk form is preserved
        # byte-for-byte. The encoding (indent=2, sort_keys=True) is
        # identical to write_json's.
        backend.write_text(
            patch_key(epoch_id, generation_id, patch.id),
            json.dumps(patch_body(patch), indent=2, sort_keys=True) + "\n",
        )

    body = experiment_body(experiment)
    backend.write_text(
        experiment_key(epoch_id, generation_id),
        json.dumps(body, indent=2, sort_keys=True) + "\n",
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

    ``experiment.json`` carries ``patch_ids: [...]`` and each id resolves
    to ``patches/{id}.json``; the record parses exactly this one way.

    Raises :class:`FileNotFoundError` when the generation has no
    ``experiment.json`` — an interrupted round, which
    :func:`read_experiment_if_present` reports as an absence instead — and
    :class:`ExperimentRecordError` when the file is there and does not
    parse.
    """
    backend = workspace_backend(workspace_root, start=False)
    return read_experiment_from_backend(backend, epoch_id, generation_id)


def read_experiment_if_present(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
) -> Experiment | None:
    """The generation's :class:`Experiment`, or ``None`` if it has none yet.

    Absence is typed here because it is legal: a generation directory is
    created when the round starts and its ``experiment.json`` lands when
    the proposer's mint completes, so a reader enumerating a running epoch
    legitimately observes a generation mid-population. Malformation is not
    absence and still raises :class:`ExperimentRecordError`, which is what
    separates "nothing written yet" from "something wrote nonsense".
    """
    try:
        return read_experiment(workspace_root, epoch_id, generation_id)
    except FileNotFoundError:
        return None


def _accepted_body(
    backend: StorageBackend,
    epoch_id: str,
    generation_id: str,
) -> dict[str, Any] | None:
    """The stored body of one generation record, or ``None`` if it has none.

    The record's one acceptance test, shared by the typed read and by the
    body the views serve. It settles three questions and nothing else: is
    the record there at all, is what is there a JSON object this format
    version can be read as, and is it the one shape the record has. Whether
    the fields inside it build an :class:`Experiment` is settled by
    :func:`read_experiment_from_backend` on top of this.
    """
    exp_key = experiment_key(epoch_id, generation_id)
    where = f"{epoch_id}/{generation_id}"
    try:
        body = backend.read_json(exp_key)
    except json.JSONDecodeError as exc:
        raise ExperimentRecordError(
            f"experiment.json for {where} (storage key {exp_key!r}) is not " f"valid JSON: {exc}"
        ) from exc
    if body is None:
        return None
    if not isinstance(body, dict):
        raise ExperimentRecordError(
            f"experiment.json for {where} (storage key {exp_key!r}) is a "
            f"{type(body).__name__}, not a JSON object"
        )
    # Missing or unsupported format stamps refuse before field validation.
    check_record_format(body, f"experiment.json ({where})")

    if "patches" in body:
        # The record once inlined its patches here instead of referencing
        # sibling files. That shape is gone, and reading such a body under
        # the current rules would silently yield an experiment with no
        # patches at all, so it refuses by name instead.
        raise ExperimentRecordError(
            f"experiment.json for {where} (storage key {exp_key!r}) carries an "
            f"inline 'patches' array; the record references its patches by id "
            f"through 'patch_ids' and sibling patches/{{id}}.json files"
        )
    try:
        for key in ("id", "epoch_id", "generation_id", "proposed_at"):
            if not isinstance(body[key], str):
                raise ValueError(f"{key} must be a string")
        if body["epoch_id"] != epoch_id or body["generation_id"] != generation_id:
            raise ValueError("experiment identity disagrees with its location")
        if body["parent_generation_id"] is not None and not isinstance(
            body["parent_generation_id"], str
        ):
            raise ValueError("parent_generation_id must be a string or null")
        if type(body["round_index"]) is not int:
            raise ValueError("round_index must be an integer")
        if not isinstance(body["patch_ids"], list) or any(
            not isinstance(x, str) for x in body["patch_ids"]
        ):
            raise ValueError("patch_ids must be a list of strings")
        if "recombined_from" in body and (
            not isinstance(body["recombined_from"], list)
            or any(not isinstance(x, str) for x in body["recombined_from"])
        ):
            raise ValueError("recombined_from must be a list of generation identifiers")
        _hypothesis_from_dict(body["hypothesis"])
        _outcome_from_dict(body["outcome"])
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ExperimentRecordError(f"experiment.json for {where} does not parse: {exc}") from exc
    return body


def read_experiment_from_backend(
    backend: StorageBackend,
    epoch_id: str,
    generation_id: str,
) -> Experiment:
    """Read an experiment through the caller's generic record backend.

    This is the backend-neutral record query used by source-tree-agnostic
    readers.  It keeps experiment and patch lookup on the record seam even
    when generation source trees live in git.
    """
    exp_key = experiment_key(epoch_id, generation_id)
    where = f"{epoch_id}/{generation_id}"
    body = _accepted_body(backend, epoch_id, generation_id)
    if body is None:
        raise FileNotFoundError(f"experiment.json not found for {where} (storage key {exp_key!r})")
    return _experiment_from_body(backend, epoch_id, generation_id, body)


def _experiment_from_body(
    backend: StorageBackend, epoch_id: str, generation_id: str, body: dict[str, Any]
) -> Experiment:
    """Resolve one accepted body's declared patches and typed fields."""
    where = f"{epoch_id}/{generation_id}"

    patches: list[Patch] = []
    for pid in body["patch_ids"]:
        pkey = patch_key(epoch_id, generation_id, pid)
        try:
            patch_body = backend.read_json(pkey)
        except json.JSONDecodeError as exc:
            raise ExperimentRecordError(
                f"patch record for {where} (storage key {pkey!r}) is not " f"valid JSON: {exc}"
            ) from exc
        if patch_body is None:
            raise ExperimentRecordError(
                f"patch record referenced by experiment.json for {where} is "
                f"missing (storage key {pkey!r})"
            )
        try:
            patches.append(_patch_from_dict(patch_body))
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ExperimentRecordError(
                f"patch record for {where} (storage key {pkey!r}) does not " f"parse: {exc}"
            ) from exc

    return Experiment(
        id=body["id"],
        epoch_id=body["epoch_id"],
        generation_id=body["generation_id"],
        parent_generation_id=body["parent_generation_id"],
        proposed_at=body["proposed_at"],
        hypothesis=_hypothesis_from_dict(body["hypothesis"]),
        patches=tuple(patches),
        outcome=_outcome_from_dict(body["outcome"]),
        round_index=body["round_index"],
        recombined_from=tuple(body.get("recombined_from", ())),
    )


def read_experiment_body(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
) -> dict[str, Any] | None:
    """The generation record's stored body, or ``None`` if it has none yet.

    JSON views receive the stored fields after the same validation used by
    typed reads. An experiment awaiting execution has an explicit null
    outcome; a recorded outcome may have an explicit null decision.

    Use :func:`read_experiment` or :func:`read_experiment_if_present` for
    typed fields and resolved patches.

    Absence and malformation split the same way as
    :func:`read_experiment_if_present`.
    """
    backend = workspace_backend(workspace_root, start=False)
    return _accepted_body(backend, epoch_id, generation_id)


def read_experiment_contents(
    workspace_root: Path, epoch_id: str, generation_id: str
) -> ExperimentContents | None:
    """Read a body's stored fields and resolve its patches from that same body.

    Absence returns ``None``. A malformed body or declared patch raises
    ``RecordError`` through the same acceptance and decoding as the typed read.
    """
    backend = workspace_backend(workspace_root, start=False)
    body = _accepted_body(backend, epoch_id, generation_id)
    if body is None:
        return None
    record = _experiment_from_body(backend, epoch_id, generation_id, body)
    return ExperimentContents(body=body, patches=record.patches)


def read_epoch_experiments(
    workspace_root: Path,
    epoch_id: str,
) -> tuple[list[tuple[str, Experiment]], list[str]]:
    """Every generation record one epoch holds, plus the ones that would not parse.

    The one enumerate-and-decode of the record, shared by the views that
    walk a whole epoch. Each record is paired with the id of the record
    DIRECTORY it was read from, which is the coordinate its sibling
    records (the cached score, the run directories) are keyed by; a body
    naming a different ``generation_id`` does not move them. Generations
    come back in :func:`~zicato.workspace.reads.generation_ids` order,
    which is numeric-aware, so ``v2`` precedes ``v10``.

    Two dispositions, and they are not the same thing. A generation with no
    ``experiment.json`` is an interrupted round and is simply absent from
    both lists. A generation whose record IS there and does not parse
    contributes one named reason to the second list, so a view can say
    which record it dropped and why instead of presenting a short epoch as
    a complete one. A view that would rather refuse outright calls
    :func:`read_experiment` per generation and lets the error propagate.
    """
    return _walk_epoch_records(workspace_root, epoch_id, read_experiment_if_present)


def read_epoch_experiment_bodies(
    workspace_root: Path,
    epoch_id: str,
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """:func:`read_epoch_experiments` for the views that serve the record itself.

    Same enumeration, same two dispositions, and the same acceptance test;
    the pairs carry each record's stored body instead of the typed value
    built from it. See :func:`read_experiment_body` for which of the two a
    caller wants.
    """
    return _walk_epoch_records(workspace_root, epoch_id, read_experiment_body)


def _walk_epoch_records(
    workspace_root: Path,
    epoch_id: str,
    read_one: Callable[[Path, str, str], _RecordT | None],
) -> tuple[list[tuple[str, _RecordT]], list[str]]:
    """The enumerate-and-read shared by the two epoch-wide walks above."""
    layout = WorkspaceLayout.from_root(workspace_root)
    records: list[tuple[str, _RecordT]] = []
    unreadable: list[str] = []
    for generation_id in generation_ids(layout, epoch_id):
        try:
            record = read_one(workspace_root, epoch_id, generation_id)
        except RecordError as exc:
            unreadable.append(str(exc))
            continue
        if record is not None:
            records.append((generation_id, record))
    return records, unreadable


def read_generation_patches(
    backend: StorageBackend,
    epoch_id: str,
    generation_id: str,
) -> PatchRecord:
    """Return one generation's patch record through ``StorageBackend``.

    A seed or otherwise unrecorded generation has no applied patch set and
    returns an empty record.  Source-tree availability is irrelevant: snapshot
    pruning never removes the experiment or its per-patch records.
    """
    try:
        experiment = read_experiment_from_backend(backend, epoch_id, generation_id)
    except FileNotFoundError:
        return PatchRecord(generation_id=generation_id, patches=())
    return PatchRecord(generation_id=generation_id, patches=tuple(experiment.patches))


__all__ = [
    "append_journal_entry",
    "append_journal_entry_once",
    "experiment_body",
    "patch_body",
    "outcome_from_dict",
    "read_journal",
    "write_experiment",
    "write_seed_experiment",
    "read_experiment",
    "read_experiment_from_backend",
    "read_experiment_if_present",
    "read_epoch_experiments",
    "read_epoch_experiment_bodies",
    "read_experiment_body",
    "read_experiment_contents",
    "read_generation_patches",
    "ExperimentRecordError",
    "PatchRecord",
    "ExperimentContents",
    "update_experiment_outcome",
]
