"""The armed judge roster for one epoch — built-ins after ``disable_drift``.

The board page's Judges panel answers "what actually grades a run on this
board". Half of that answer is authored and can simply be read: the custom
judges each entry declares ride the epoch payload as ``board_judges``. The
other half has to be DERIVED, and this module derives it — goldfive's
built-in judge set MINUS whatever the board's ``disable_drift`` header
suppressed, the per-judge weights the gate applies to both halves, and a
pointer to each judge's reflection scorecard where one exists.

The suppression derivation goes through :mod:`zicato.judge_runtime.disable`
— the one module that knows which built-in judge emits which drift kind —
rather than restating the mapping here. That matters because the mapping is
deliberately PARTIAL: a ``disable_drift`` kind no built-in judge emits
suppresses nothing at all, and a panel that assumed a named kind always
disarms something would draw a suppression that never happened. Those kinds
come back as ``unmapped_drift_kinds`` so the surface can say so out loud.

goldfive is an optional extra, and the built-in roster is goldfive's own
default set — so without it there is nothing to enumerate. The reader then
serves an empty roster plus a note naming the reason, never a guess.

Best-effort like every reader here: no input can make this raise.
"""

from __future__ import annotations

from typing import Any

from zicato.query.epoch_view import _parse_board_meta
from zicato.query.paths import (
    WorkspacePaths,
    _read_json_value,
    _resolve_epoch_id,
    coerce_float,
    coerce_numeric_dict,
    layout_of,
)
from zicato.query.reflection_view import build_judge_scorecards, list_reflections

#: goldfive absent — the roster is its default judge set, so there is nothing
#: to enumerate. Named separately from the generic failure so the surface
#: reports the ACTUAL reason rather than the likeliest one.
NO_GOLDFIVE_NOTE = "built-in roster unavailable (goldfive not installed)"

#: goldfive present but its default set would not enumerate. Distinct text so
#: an operator is not sent looking for a missing install that is right there.
NO_ROSTER_NOTE = "built-in roster unavailable (goldfive's default judge set could not be read)"


def _empty_judge_roster(epoch_id: str | None) -> dict[str, Any]:
    """The roster shape for an epoch that resolves to nothing.

    Single-sourced so the endpoint's malformed-id degrade and the reader's
    no-epoch degrade cannot drift apart (the ``_empty_dossier`` precedent).
    """
    return {
        "epoch_id": epoch_id,
        "builtins": [],
        "builtins_note": None,
        "disable_drift": [],
        "unmapped_drift_kinds": [],
        "per_judge_weights": {},
        "default_judge_weight": None,
        "scorecards": {},
    }


def _scorecard_reflections(paths: WorkspacePaths, epoch_id: str) -> dict[str, str]:
    """``{judge_name: reflection_id}`` — the newest reflection that scored each judge.

    :func:`~zicato.query.reflection_view.list_reflections` returns newest
    first, so the first reflection carrying a judge's scorecard wins and the
    panel links at the most recent adjudicated reading of that judge. A judge
    no reflection has scored is simply absent — the panel then renders its
    name without a link, which is the truth.
    """
    try:
        listing = list_reflections(paths, epoch_id)
    except Exception:  # noqa: BLE001 — best-effort; a cold index must not 500
        return {}
    by_judge: dict[str, str] = {}
    for item in listing.get("reflections", []):
        reflection_id = item.get("reflection_id") if isinstance(item, dict) else None
        if not isinstance(reflection_id, str) or not reflection_id:
            continue
        try:
            cards = build_judge_scorecards(paths, reflection_id).get("judges", [])
        except Exception:  # noqa: BLE001 — best-effort, per reflection
            continue
        for card in cards:
            name = card.get("judge_name") if isinstance(card, dict) else None
            if isinstance(name, str) and name and name not in by_judge:
                by_judge[name] = reflection_id
    return by_judge


def build_judge_roster(paths: WorkspacePaths, epoch_id: str | None = None) -> dict[str, Any]:
    """What is armed to judge a run on one epoch's board.

    Returns:

    * ``builtins`` — every judge in goldfive's default set, each with
      ``suppressed`` and the ``suppressed_by`` drift kinds that did it. The
      suppressed ones stay ON the list rather than being filtered out: the
      point of the panel is to show what the ``disable_drift`` header
      actually changed, and a silently shorter list shows nothing.
    * ``builtins_note`` — why the roster is empty, when it is (see
      :data:`NO_GOLDFIVE_NOTE` / :data:`NO_ROSTER_NOTE`); ``None`` otherwise.
    * ``disable_drift`` — the header's kinds in wire form, deduplicated.
    * ``unmapped_drift_kinds`` — the subset no built-in judge emits, and
      which therefore suppress NOTHING.
    * ``per_judge_weights`` / ``default_judge_weight`` — the frozen
      ``scoring.json`` weights, which key on judge name across BOTH halves
      (a built-in and a custom judge are weighted by the same lookup).
    * ``scorecards`` — ``{judge_name: reflection_id}`` for the judges a
      reflection has scored.

    The suppression half needs no goldfive (the mapping is a plain dict in
    :mod:`zicato.judge_runtime.disable`), so ``unmapped_drift_kinds`` stays
    correct even when the roster itself cannot be enumerated.
    """
    from zicato.judge_runtime.disable import (  # noqa: PLC0415 — goldfive-adjacent
        builtin_judge_names_to_suppress,
        default_judges_minus,
        kind_to_wire_string,
    )

    try:
        epoch_id = _resolve_epoch_id(paths, epoch_id)
    except ValueError:
        # An unknown or path-unsafe id resolves to no epoch. A reader never
        # raises (DQ3), and ``None`` is the honest epoch_id for a roster that
        # describes nothing — echoing the rejected string back would not be.
        epoch_id = None
    roster = _empty_judge_roster(epoch_id)
    if epoch_id is None:
        return roster
    layout = layout_of(paths)

    meta = _parse_board_meta(layout.board(epoch_id)) or {}
    kinds = sorted({kind_to_wire_string(k) for k in meta.get("disable_drift", ())})
    roster["disable_drift"] = kinds

    # ONE kind at a time through the PUBLIC mapping, so a kind that suppresses
    # nothing is visible as itself rather than lost in a union.
    suppressed_by_kind = {kind: builtin_judge_names_to_suppress((kind,)) for kind in kinds}
    roster["unmapped_drift_kinds"] = [k for k in kinds if not suppressed_by_kind[k]]

    # ``default_judges_minus(set())`` is the FULL default set — the same call
    # the adapter makes to arm a run, with nothing removed, so the roster
    # cannot drift from what actually runs.
    try:
        names = [str(getattr(judge, "name", "") or "") for judge in default_judges_minus(set())]
    except ImportError:
        roster["builtins_note"] = NO_GOLDFIVE_NOTE
        names = []
    except Exception:  # noqa: BLE001 — best-effort; an honest note, not a 500
        roster["builtins_note"] = NO_ROSTER_NOTE
        names = []
    builtins: list[dict[str, Any]] = []
    for name in names:
        if not name:
            continue
        by = [kind for kind in kinds if name in suppressed_by_kind[kind]]
        builtins.append({"name": name, "suppressed": bool(by), "suppressed_by": by})
    roster["builtins"] = builtins

    scoring = _read_json_value(layout.scoring(epoch_id))
    if isinstance(scoring, dict):
        roster["per_judge_weights"] = coerce_numeric_dict(scoring.get("per_judge_weights"))
        roster["default_judge_weight"] = coerce_float(scoring.get("default_judge_weight"))

    roster["scorecards"] = _scorecard_reflections(paths, epoch_id)
    return roster


__all__ = [
    "NO_GOLDFIVE_NOTE",
    "NO_ROSTER_NOTE",
    "_empty_judge_roster",
    "build_judge_roster",
]
