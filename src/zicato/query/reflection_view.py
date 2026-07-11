"""reflection_view — the Instrument-lens read surface over board reflection.

Index-first, file-fallback readers that project a completed reflection's
canonical artifacts (``plan.json`` / ``corpus.jsonl`` / ``adjudication/`` /
``scorecards.json`` / ``findings.json`` / the derived ``summary.json``) into
the JSON view shapes the console's Instrument lens (R5) and the dashboard
endpoints consume. Every reader is best-effort: a missing / truncated file, a
never-built index, or an unknown id degrades to a same-shape empty payload
(the DQ3 degrade rule) rather than raising.

The index is a projection; **a reflection is readable with no index at all** —
each reader falls back to the canonical files when the index row is absent
(the ``AGENTS.md`` rule 4 invariant). This module must stay **dashboard-free**
(the ``zicato.query`` import contract) and so it imports only the
dashboard-free reflection submodules (:mod:`~zicato.reflection.plan`,
:mod:`~zicato.reflection.corpus`, :mod:`~zicato.reflection.analysis`) plus the
canonical file layout — never :mod:`zicato.reflection.adjudicator` /
``scorecards`` / ``findings`` (which reach the builder / the events preview
reconstructor), and never :mod:`zicato.dashboard`. The transcript x-ray
therefore reconstructs from ``result.json`` (preferred) then the verbatim
``judge_io`` window; the events-preview tier — the only source needing the
dashboard reconstructor — is honestly reported as unavailable here.

Readers
-------
* :func:`list_reflections` — every reflection under a workspace (or one epoch).
* :func:`build_reflection_summary` — the four-pillar bill of health.
* :func:`build_judge_scorecards` — the per-judge confusion-matrix cards.
* :func:`build_adjudication_xray` — the transcript + judge verdict + the
  meta-judge adjudication record for one decision (the emotional centrepiece).
* :func:`entry_candidate_matrix` — the reflection-INDEPENDENT entry×candidate
  matrix straight off the index loss tables (the continuous passive tier).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zicato.query.paths import WorkspacePaths, list_epoch_ids

#: Fidelity tiers, strongest first (mirrors the R1 capture ladder). Kept local
#: so this module needs no import edge onto :mod:`zicato.reflection.corpus`'s
#: constants for the reader-only degrade paths.
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
    from zicato.core.workspace import reflection_plan_path  # noqa: PLC0415

    raw = _load_json(reflection_plan_path(paths.root, epoch_id, reflection_id))
    return raw if isinstance(raw, dict) else None


def _scorecards_from_file(
    paths: WorkspacePaths, epoch_id: str, reflection_id: str
) -> list[dict[str, Any]]:
    from zicato.core.workspace import reflection_scorecards_path  # noqa: PLC0415

    raw = _load_json(reflection_scorecards_path(paths.root, epoch_id, reflection_id))
    if isinstance(raw, dict):
        raw = raw.get("scorecards")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict)]


def _findings_from_file(
    paths: WorkspacePaths, epoch_id: str, reflection_id: str
) -> list[dict[str, Any]]:
    from zicato.core.workspace import reflection_findings_path  # noqa: PLC0415

    raw = _load_json(reflection_findings_path(paths.root, epoch_id, reflection_id))
    if isinstance(raw, dict):
        raw = raw.get("findings")
    if not isinstance(raw, list):
        return []
    return [f for f in raw if isinstance(f, dict)]


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

    for eid in epoch_ids:
        try:
            rows = iq.reflections_for_epoch(paths.index_db, eid)
        except Exception:  # noqa: BLE001 — best-effort
            rows = []
        for row in rows:
            rid = row["reflection_id"]
            if not isinstance(rid, str) or rid in by_id:
                continue
            item = {
                "reflection_id": rid,
                "epoch_id": row["epoch_id"],
                "created_at": row["created_at"] or "",
                "mode": row["mode"] or "",
                "executed": bool(row["executed"]),
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
            if not child.is_dir() or child.name in by_id:
                continue
            plan = _plan_dict(paths, eid, child.name)
            if plan is None:
                continue
            by_id[child.name] = _reflection_stub(plan, eid, child.name)
            order.append(child.name)

    items = [by_id[rid] for rid in order]
    items.sort(
        key=lambda d: (str(d.get("created_at") or ""), str(d["reflection_id"])),
        reverse=True,
    )
    return {"reflections": items}


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
    plan = _plan_dict(paths, epoch_id, reflection_id)
    if plan is None:
        return _empty_summary(reflection_id)

    summary = _summary_from_file(paths, epoch_id, reflection_id)
    findings = _findings_from_file(paths, epoch_id, reflection_id)
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
    """The per-judge scorecards for one reflection — index-first, file fallback.

    Prefers the ``judge_scorecards`` projection; falls back to the canonical
    ``scorecards.json`` when the index row is absent (a reflection persisted but
    not yet indexed, or a never-indexed workspace). Returns ``{reflection_id,
    judges: [...]}`` — an empty list for an unknown reflection.
    """
    from zicato.index import query as iq  # noqa: PLC0415

    try:
        rows = iq.judge_scorecards_for_reflection(paths.index_db, reflection_id)
    except Exception:  # noqa: BLE001 — best-effort
        rows = []
    if rows:
        judges = [
            {
                "judge_name": r["judge_name"],
                "tp": _opt_int(r["tp"]),
                "fp": _opt_int(r["fp"]),
                "fn": _opt_int(r["fn"]),
                "tn": _opt_int(r["tn"]),
                "ambiguous": _opt_int(r["ambiguous"]),
                "precision": _opt_num(r["precision"]),
                "recall": _opt_num(r["recall"]),
                "f1": _opt_num(r["f1"]),
                "severity_accuracy": _opt_num(r["severity_accuracy"]),
                "disagreement_rate": _opt_num(r["disagreement_rate"]),
                "self_consistency_kappa": _opt_num(r["kappa"]),
                "exercised": bool(r["exercised"]),
                "redundant_with": _opt_json_list(r["redundant_with_json"]),
            }
            for r in rows
        ]
        return {"reflection_id": reflection_id, "judges": judges}

    # File fallback — read the canonical scorecards.json.
    epoch_id = _resolve_epoch(paths, reflection_id)
    if epoch_id is None:
        return {"reflection_id": reflection_id, "judges": []}
    return {
        "reflection_id": reflection_id,
        "judges": _scorecards_from_file(paths, epoch_id, reflection_id),
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
    from zicato.tournament.unit_cache import read_run_result, unit_result_path  # noqa: PLC0415

    body = read_run_result(unit_result_path(Path(loss_ref)))
    if not isinstance(body, dict):
        return None
    turns = [str(t) for t in (body.get("transcript") or [])]
    final = str(body.get("final_output") or "")
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

    for rec in read_judge_io(judge_io_path_for_loss(Path(loss_ref))):
        if str(rec.get("judge_name", "")) != judge_name:
            continue
        inp = rec.get("input", {}) if isinstance(rec, dict) else {}
        window = [str(t) for t in (inp.get("transcript_window") or [])]
        reasoning = str(inp.get("reasoning_text") or "")
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
    verbatim ``judge_io`` window as the fallback — the events-preview tier needs
    the dashboard reconstructor and so is reported ``unavailable`` here to keep
    the query layer dashboard-free), the judge's ORIGINAL verdict from the
    corpus, and the independent meta-judge's adjudication record. An unknown
    reflection / decision degrades to a same-shape empty payload.
    """
    epoch_id = _resolve_epoch(paths, reflection_id)
    if epoch_id is None:
        return _empty_xray(reflection_id, judge_name, run_ref)

    from zicato.reflection.corpus import read_corpus  # noqa: PLC0415

    corpus = read_corpus(paths.root, epoch_id, reflection_id)
    match = next(
        (o for o in corpus if f"{o.candidate_id}:{o.entry_id}:r{o.replicate}" == run_ref),
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

    transcript = (
        _transcript_from_result(match.loss_ref)
        or _transcript_from_judge_io(match.loss_ref, judge_name)
        or {"fidelity": _FIDELITY_UNAVAILABLE, "turns": []}
    )

    from zicato.core.workspace import reflection_adjudication_path  # noqa: PLC0415

    adjudication = _load_json(
        reflection_adjudication_path(paths.root, epoch_id, reflection_id, judge_name, run_ref)
    )
    adjudication = adjudication if isinstance(adjudication, dict) else None

    return {
        "reflection_id": reflection_id,
        "epoch_id": epoch_id,
        "judge_name": judge_name,
        "run_ref": run_ref,
        "found": True,
        "transcript": transcript,
        "judge_verdict": judge_verdict,
        "adjudication": adjudication,
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
    never-indexed workspace yields empty axes + an empty matrix (DQ3 same-shape
    degrade).
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


def _opt_json_list(value: Any) -> list[Any]:
    if not isinstance(value, str):
        return list(value) if isinstance(value, list) else []
    try:
        parsed = json.loads(value)
    except (ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


__all__ = [
    "build_adjudication_xray",
    "build_judge_scorecards",
    "build_reflection_summary",
    "entry_candidate_matrix",
    "list_reflections",
]
