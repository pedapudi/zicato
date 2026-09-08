"""reflection_view — the Instrument-lens read surface over board reflection.

Index-first, file-fallback readers that project a completed reflection's
canonical artifacts (``plan.json`` / ``corpus.jsonl`` / ``adjudication/`` /
``scorecards.json`` / ``findings.json`` / the derived ``summary.json``) into
the JSON view shapes the console's Instrument lens and the dashboard
endpoints consume. Every reader is best-effort: a missing or truncated file,
a never-built index, or an unknown id degrades to a same-shape empty payload
rather than raising.

The index is a projection; **a reflection is readable with no index at all** —
each reader falls back to the canonical files when the index row is absent
(the filesystem is canonical and the index is derived; ``AGENTS.md``). This
module must stay **dashboard-free**
(the ``zicato.query`` import contract), and it keeps no engine behind a read:
it imports reflection record owners and pure analysis, without loading
adjudication execution or dashboard drivers. The transcript x-ray therefore
reconstructs from ``result.json`` (preferred) then the verbatim ``judge_io``
window; the events-preview tier belongs to the adjudicator and is never re-run
behind a read, so it is honestly reported as unavailable.

Readers
-------
* :func:`list_reflections` — every reflection under a workspace (or one epoch).
* :func:`build_reflection_summary` — the four-pillar bill of health.
* :func:`build_judge_scorecards` — the per-judge confusion-matrix cards.
* :func:`build_adjudication_xray` — the transcript + judge verdict + the
  meta-judge adjudication record for one decision — what an operator opens to
  see why one judgement went the way it did.
* :func:`entry_candidate_matrix` — the reflection-INDEPENDENT entry×candidate
  matrix straight off the index loss tables (the continuous passive tier).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zicato.epoch._storage import RecordError
from zicato.query.paths import WorkspacePaths, list_epoch_ids
from zicato.reflection.plan import read_plan

#: Fidelity tiers, strongest first, mirroring the capture ladder in
#: :mod:`zicato.reflection.corpus`. Kept local so this module needs no import
#: edge onto those constants for the reader-only degrade paths.
_FIDELITY_VERBATIM = "verbatim"
_FIDELITY_RESULT = "result"
_FIDELITY_UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# Canonical-file helpers (files are the source of truth; index is a projection)
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Any | None:
    """Read + parse one JSON file; ``None`` on any defect (best-effort)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _resolve_epoch(paths: WorkspacePaths, reflection_id: str) -> str | None:
    """Find which epoch owns ``reflection_id`` — index first, then the tree.

    Prefers the index ``reflections`` row (one lookup); falls back to walking
    each epoch's ``reflections/`` directory for a matching id so a reflection
    is resolvable with no index at all.
    """
    from zicato.index import query as iq  # noqa: PLC0415

    try:
        row = iq.reflection_row(paths.index_db, reflection_id)
    except Exception:  # noqa: BLE001 — best-effort
        row = None
    if row is not None:
        epoch = row["epoch_id"]
        if isinstance(epoch, str) and epoch:
            return epoch

    from zicato.core.workspace import reflection_dir  # noqa: PLC0415

    for epoch_id in list_epoch_ids(paths):
        if reflection_dir(paths.root, epoch_id, reflection_id).is_dir():
            return epoch_id
    return None


def _plan_dict(paths: WorkspacePaths, epoch_id: str, reflection_id: str) -> dict[str, Any] | None:
    plan = read_plan(paths.root, epoch_id, reflection_id)
    return plan.to_json() if plan is not None else None


def _summary_from_file(paths: WorkspacePaths, epoch_id: str, reflection_id: str) -> dict[str, Any]:
    from zicato.core.workspace import reflection_dir  # noqa: PLC0415

    raw = _load_json(reflection_dir(paths.root, epoch_id, reflection_id) / "summary.json")
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# list_reflections — every reflection under a workspace (or one epoch)
# ---------------------------------------------------------------------------


def _reflection_stub(plan: dict[str, Any], epoch_id: str, reflection_id: str) -> dict[str, Any]:
    """One list-item shape from a plan dict (the file-fallback item)."""
    return {
        "reflection_id": str(plan.get("reflection_id") or reflection_id),
        "epoch_id": str(plan.get("epoch_id") or epoch_id),
        "created_at": str(plan.get("created_at") or ""),
        "mode": str(plan.get("mode") or ""),
        "executed": bool(plan.get("executed", False)),
        "noise_floor_max_abs_delta": None,
        "decision_flip_p": None,
        "n_findings": None,
        "n_judges": None,
    }


def list_reflections(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """Every reflection under the workspace (or one epoch), newest first.

    Index-first: reads the ``reflections`` projection per epoch when the index
    is built. File-fallback: walks each epoch's ``reflections/`` directory for
    a ``plan.json`` when a row is missing (a reflection persisted but not yet
    indexed, or a never-indexed workspace). Returns ``{"reflections": [...]}``
    — an empty list on a workspace with none.
    """
    from zicato.index import query as iq  # noqa: PLC0415

    epoch_ids = [epoch_id] if epoch_id else list_epoch_ids(paths)
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    unreadable: dict[tuple[str, str], str] = {}

    for eid in epoch_ids:
        try:
            rows = iq.reflections_for_epoch(paths.index_db, eid)
        except Exception:  # noqa: BLE001 — best-effort
            rows = []
        for row in rows:
            rid = row["reflection_id"]
            if not isinstance(rid, str) or rid in by_id:
                continue
            try:
                plan = _plan_dict(paths, eid, rid)
            except RecordError as exc:
                unreadable[eid, rid] = str(exc)
                continue
            if plan is None:
                continue
            item = {
                "reflection_id": rid,
                "epoch_id": eid,
                "created_at": plan.get("created_at", ""),
                "mode": plan.get("mode", ""),
                "executed": plan.get("executed", False),
                "noise_floor_max_abs_delta": _opt_num(row["noise_floor_max_abs_delta"]),
                "decision_flip_p": _opt_num(row["decision_flip_p"]),
                "n_findings": _opt_int(row["n_findings"]),
                "n_judges": _opt_int(row["n_judges"]),
            }
            by_id[rid] = item
            order.append(rid)

    # File fallback: discover any reflection dir the index missed.
    from zicato.core.workspace import reflections_dir  # noqa: PLC0415

    for eid in epoch_ids:
        root = reflections_dir(paths.root, eid)
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name in by_id or (eid, child.name) in unreadable:
                continue
            try:
                plan = _plan_dict(paths, eid, child.name)
            except RecordError as exc:
                unreadable[eid, child.name] = str(exc)
                continue
            if plan is None:
                continue
            by_id[child.name] = _reflection_stub(plan, eid, child.name)
            order.append(child.name)

    items = [by_id[rid] for rid in order]
    items.sort(
        key=lambda d: (str(d.get("created_at") or ""), str(d["reflection_id"])),
        reverse=True,
    )
    payload: dict[str, Any] = {"reflections": items}
    if unreadable:
        payload["unreadable"] = [
            {"epoch_id": eid, "reflection_id": rid, "reason": reason}
            for (eid, rid), reason in sorted(unreadable.items())
        ]
    return payload


# ---------------------------------------------------------------------------
# build_reflection_summary — the four-pillar bill of health
# ---------------------------------------------------------------------------


def _empty_summary(reflection_id: str) -> dict[str, Any]:
    return {
        "reflection_id": reflection_id,
        "epoch_id": None,
        "created_at": "",
        "mode": "",
        "executed": False,
        "found": False,
        "pillars": {},
        "findings": [],
        "fidelity_tiers": [],
        "note": "no such reflection",
    }


def build_reflection_summary(paths: WorkspacePaths, reflection_id: str) -> dict[str, Any]:
    """The four-pillar bill of health for one reflection.

    Projects the reflection's canonical ``plan.json`` (identity), the derived
    ``summary.json`` (the four pillars the CLI computed — reliability /
    discrimination / validity / calibration), and ``findings.json`` (the ranked
    findings) into one payload. An unknown reflection degrades to a same-shape
    empty summary with ``found: False`` (never raises).
    """
    epoch_id = _resolve_epoch(paths, reflection_id)
    if epoch_id is None:
        return _empty_summary(reflection_id)
    try:
        plan = _plan_dict(paths, epoch_id, reflection_id)
    except RecordError as exc:
        return dict(
            _empty_summary(reflection_id), epoch_id=epoch_id, note=str(exc), unreadable=True
        )
    if plan is None:
        return _empty_summary(reflection_id)

    from zicato.reflection.findings import read_findings  # noqa: PLC0415

    try:
        collection = read_findings(paths.root, epoch_id, reflection_id)
    except RecordError as exc:
        return dict(
            _empty_summary(reflection_id), epoch_id=epoch_id, note=str(exc), unreadable=True
        )
    summary = _summary_from_file(paths, epoch_id, reflection_id)
    findings = [finding.to_json() for finding in collection.items] if collection is not None else []
    raw_pillars = summary.get("pillars")
    pillars = raw_pillars if isinstance(raw_pillars, dict) else {}
    raw_tiers = summary.get("fidelity_tiers")
    tiers = raw_tiers if isinstance(raw_tiers, list) else []

    return {
        "reflection_id": str(plan.get("reflection_id") or reflection_id),
        "epoch_id": str(plan.get("epoch_id") or epoch_id),
        "created_at": str(plan.get("created_at") or ""),
        "mode": str(plan.get("mode") or ""),
        "executed": bool(plan.get("executed", False)),
        "found": True,
        "noise_floor_max_abs_delta": _opt_num(summary.get("noise_floor_max_abs_delta")),
        "decision_flip_p": _opt_num(summary.get("decision_flip_p")),
        "pillars": pillars,
        "findings": findings,
        "fidelity_tiers": [str(t) for t in tiers],
    }


# ---------------------------------------------------------------------------
# build_judge_scorecards — the per-judge confusion-matrix cards
# ---------------------------------------------------------------------------


def build_judge_scorecards(paths: WorkspacePaths, reflection_id: str) -> dict[str, Any]:
    """Project accepted canonical cards; corruption cannot reuse indexed evidence."""
    from zicato.reflection.scorecards import read_scorecards  # noqa: PLC0415

    empty = {"reflection_id": reflection_id, "judges": []}
    epoch_id = _resolve_epoch(paths, reflection_id)
    if epoch_id is None:
        return empty
    try:
        record = read_scorecards(paths.root, epoch_id, reflection_id)
    except RecordError as exc:
        return {**empty, "unreadable": str(exc)}
    return {
        "reflection_id": reflection_id,
        "judges": [card.to_json() for card in record.cards] if record is not None else [],
    }


# ---------------------------------------------------------------------------
# build_practice_review — the narrative layer above the four pillars
# ---------------------------------------------------------------------------


def _empty_practice_review(reflection_id: str) -> dict[str, Any]:
    return {
        "reflection_id": reflection_id,
        "epoch_id": None,
        "found": False,
        "checks": [],
        "verdict_counts": {"sound": 0, "attend": 0, "unsound": 0, "unmeasured": 0},
        "note": "no such reflection / practice review",
    }


def build_practice_review(paths: WorkspacePaths, reflection_id: str) -> dict[str, Any]:
    """The practice review for one reflection — FILE-first, same-shape degrade.

    Projects the canonical ``practices.json`` (the ``PracticeReview.to_json``
    shape: ``{checks, verdict_counts}``) written by ``zicato inspect reflection run``. An
    unknown reflection, or one whose directory holds no ``practices.json``,
    degrades to a same-shape empty payload with
    ``found: False`` rather than raising — the file is canonical and this reader
    needs no index row.
    """
    epoch_id = _resolve_epoch(paths, reflection_id)
    if epoch_id is None:
        return _empty_practice_review(reflection_id)
    from zicato.reflection.practices import read_practice_review  # noqa: PLC0415

    try:
        review = read_practice_review(paths.root, epoch_id, reflection_id)
    except RecordError as exc:
        return dict(
            _empty_practice_review(reflection_id), epoch_id=epoch_id, note=str(exc), unreadable=True
        )
    if review is None:
        payload = _empty_practice_review(reflection_id)
        payload["epoch_id"] = epoch_id
        return payload
    return {
        "reflection_id": reflection_id,
        "epoch_id": epoch_id,
        "found": True,
        "checks": [check.to_json() for check in review.checks],
        "verdict_counts": review.verdict_counts(),
    }


# ---------------------------------------------------------------------------
# build_adjudication_xray — transcript + judge verdict + adjudication record
# ---------------------------------------------------------------------------


def _empty_xray(reflection_id: str, judge_name: str, run_ref: str) -> dict[str, Any]:
    return {
        "reflection_id": reflection_id,
        "judge_name": judge_name,
        "run_ref": run_ref,
        "found": False,
        "transcript": {"fidelity": _FIDELITY_UNAVAILABLE, "turns": []},
        "judge_verdict": None,
        "adjudication": None,
        "note": "no such reflection / decision",
    }


def _transcript_from_result(loss_ref: str | None) -> dict[str, Any] | None:
    """Reconstruct the transcript from ``result.json`` (the preferred source)."""
    if not loss_ref:
        return None
    from zicato.tournament.unit_cache import (  # noqa: PLC0415
        read_capture_loss,
        read_run_result,
        unit_result_path,
    )

    try:
        loss = read_capture_loss(Path(loss_ref))
    except ValueError:
        return None
    if loss is None:
        return None
    body = read_run_result(unit_result_path(Path(loss_ref)), expected=loss)
    if body is None:
        return None
    turns = list(body["transcript"])
    final = body["final_output"]
    if final:
        turns.append(final)
    if not turns:
        return None
    return {"fidelity": _FIDELITY_RESULT, "turns": turns}


def _transcript_from_judge_io(loss_ref: str | None, judge_name: str) -> dict[str, Any] | None:
    """Reconstruct the verbatim window from the ``judge_io`` sidecar."""
    if not loss_ref:
        return None
    from zicato.judge_runtime.io_capture import (  # noqa: PLC0415
        judge_io_path_for_loss,
        read_judge_io,
    )
    from zicato.tournament.unit_cache import read_capture_loss  # noqa: PLC0415

    try:
        loss = read_capture_loss(Path(loss_ref))
    except ValueError:
        return None
    if loss is None:
        return None
    for rec in read_judge_io(judge_io_path_for_loss(Path(loss_ref)), expected=loss):
        if str(rec.get("judge_name", "")) != judge_name:
            continue
        inp = rec["input"]
        window = list(inp["transcript_window"])
        reasoning = inp["reasoning_text"]
        if reasoning and (not window or window[-1] != reasoning):
            window.append(reasoning)
        if window:
            return {"fidelity": _FIDELITY_VERBATIM, "turns": window}
    return None


def build_adjudication_xray(
    paths: WorkspacePaths,
    reflection_id: str,
    judge_name: str,
    run_ref: str,
) -> dict[str, Any]:
    """The transcript x-ray for one adjudicated decision — the centrepiece.

    Assembles the conversation the judge graded (``result.json`` preferred, the
    verbatim ``judge_io`` window as the fallback — the events-preview tier is
    the adjudicator's own fallback, never re-run behind a read, and so is
    reported ``unavailable`` here), the judge's ORIGINAL verdict from the
    corpus, and the independent meta-judge's adjudication record. An unknown
    reflection / decision degrades to a same-shape empty payload.
    """
    epoch_id = _resolve_epoch(paths, reflection_id)
    if epoch_id is None:
        return _empty_xray(reflection_id, judge_name, run_ref)

    from zicato.reflection.adjudicator import run_ref_for  # noqa: PLC0415
    from zicato.reflection.corpus import read_corpus  # noqa: PLC0415

    corpus = read_corpus(paths.root, epoch_id, reflection_id)
    match = next(
        (o for o in corpus if run_ref_for(o) == run_ref),
        None,
    )
    if match is None:
        payload = _empty_xray(reflection_id, judge_name, run_ref)
        payload["epoch_id"] = epoch_id
        return payload

    judge_verdict = next(
        (dict(d) for d in match.judge_decisions if str(d.get("judge_name", "")) == judge_name),
        None,
    )

    try:
        transcript = (
            _transcript_from_result(match.loss_ref)
            or _transcript_from_judge_io(match.loss_ref, judge_name)
            or {"fidelity": _FIDELITY_UNAVAILABLE, "turns": []}
        )
    except RecordError as exc:
        return dict(
            _empty_xray(reflection_id, judge_name, run_ref),
            epoch_id=epoch_id,
            judge_verdict=judge_verdict,
            unreadable=True,
            note=str(exc),
        )

    from zicato.core.workspace import reflection_adjudication_path  # noqa: PLC0415
    from zicato.reflection.adjudication import read_adjudication  # noqa: PLC0415

    try:
        record = read_adjudication(
            reflection_adjudication_path(paths.root, epoch_id, reflection_id, judge_name, run_ref)
        )
    except RecordError as exc:
        return dict(
            _empty_xray(reflection_id, judge_name, run_ref),
            epoch_id=epoch_id,
            transcript=transcript,
            judge_verdict=judge_verdict,
            unreadable=True,
            note=str(exc),
        )

    return {
        "reflection_id": reflection_id,
        "epoch_id": epoch_id,
        "judge_name": judge_name,
        "run_ref": run_ref,
        "found": True,
        "transcript": transcript,
        "judge_verdict": judge_verdict,
        "adjudication": record.to_json() if record is not None else None,
    }


# ---------------------------------------------------------------------------
# entry_candidate_matrix — reflection-INDEPENDENT (the continuous passive tier)
# ---------------------------------------------------------------------------


def entry_candidate_matrix(paths: WorkspacePaths, epoch_id: str) -> dict[str, Any]:
    """The entry×candidate mean-drift matrix straight off the index loss tables.

    The reflection-INDEPENDENT discrimination feed (BOARD-REFLECTION.md verdict
    5): the continuous passive tier and the dashboard get the entry×candidate
    spread without running a reflection. For every generation under ``epoch_id``
    and every board-entry run, the cell is the mean ``drift_loss`` across that
    unit's persisted loss rows. Axes are sorted; a missing cell is ``None``. A
    never-indexed workspace yields empty axes and an empty matrix, on the same
    same-shape degrade.
    """
    from zicato.index import query as iq  # noqa: PLC0415

    try:
        gens = iq.generations_for_epoch(paths.index_db, epoch_id)
    except Exception:  # noqa: BLE001 — best-effort
        gens = []
    candidates = [g["generation_id"] for g in gens if isinstance(g["generation_id"], str)]

    # (entry_id, candidate) -> [drift_loss draws]
    cell: dict[tuple[str, str], list[float]] = {}
    entry_set: set[str] = set()
    for candidate in candidates:
        try:
            rows = iq.loss_profiles_for_generation(paths.index_db, epoch_id, candidate)
        except Exception:  # noqa: BLE001 — best-effort
            rows = []
        for r in rows:
            entry_id = r["entry_id"]
            drift = _opt_num(r["drift_loss"])
            if not isinstance(entry_id, str) or drift is None:
                continue
            entry_set.add(entry_id)
            cell.setdefault((entry_id, candidate), []).append(drift)

    entries = sorted(entry_set)
    matrix: list[list[float | None]] = []
    for entry in entries:
        row: list[float | None] = []
        for candidate in candidates:
            draws = cell.get((entry, candidate))
            row.append(sum(draws) / len(draws) if draws else None)
        matrix.append(row)

    return {
        "epoch_id": epoch_id,
        "entries": entries,
        "candidates": candidates,
        "matrix": matrix,
    }


# ---------------------------------------------------------------------------
# small coercions
# ---------------------------------------------------------------------------


def _opt_num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _opt_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


__all__ = [
    "build_adjudication_xray",
    "build_judge_scorecards",
    "build_practice_review",
    "build_reflection_summary",
    "entry_candidate_matrix",
    "list_reflections",
]
