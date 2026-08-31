"""The browser shows exactly the cost and lint findings Python computed.

The tournament builder's cost estimate and its recommend-only lint findings
have one owner: :func:`zicato.builder.operations.estimate_cost` and
:func:`zicato.builder.operations.validate`. Both endpoints the console calls
(``POST /builder/op`` after every edit, ``GET /builder/draft`` for the
read-only contract summary in Settings) carry their results in the response
envelope, and the browser renders that envelope without recomputing any of it.

Two implementations of one piece of arithmetic drift without anything noticing,
because each side's tests pass on its own numbers. This module tests the join
instead: for a fixed set of drafts it computes the envelope in Python, renders
it through the console's own preview module under node, and requires the
numbers and texts read back off the rendered nodes to equal the ones Python
produced. A field renamed on either side breaks the readback.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import zicato.dashboard as _dashboard_pkg
from zicato.board.split import split_board
from zicato.builder import operations as ops
from zicato.builder.draft import TournamentDraft
from zicato.core.types import BoardEntry

STATIC_DIR = Path(_dashboard_pkg.__file__).resolve().parent / "static"
READBACK = STATIC_DIR / "test" / "cost_envelope_readback.mjs"

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node runtime not available")


def _board(n: int) -> list[BoardEntry]:
    return [
        BoardEntry(id=f"e{i}", kind="single_turn", wall_clock_budget_seconds=60, input="hello")
        for i in range(n)
    ]


def _draft(structure: str, entries: int, **params: Any) -> TournamentDraft:
    draft = TournamentDraft()
    draft.entries = _board(entries)
    ops.set_structure(draft, structure)
    for key, value in params.items():
        ops.set_param(draft, key, value)
    return draft


def _gauntlet_with_evidence_gate() -> TournamentDraft:
    """Every term a bare draft leaves out: the crowning-confirm budget, the
    auxiliary best-of-N propose calls, the screen panel, the placebo arm."""
    draft = _draft(
        "gauntlet",
        10,
        field_size=1,
        replicates=2,
        promote_confidence_threshold=0.8,
        promote_confidence_replicates=8,
    )
    ops.set_screening(draft, entries=3)
    ops.set_proposer_quality(draft, best_of_n=3)
    ops.set_holdout(draft, random_baseline_every_n=4)
    return draft


#: The drafts the correspondence runs over, named for what each one exercises.
#: Swiss leaves ``replicates`` unset, so the estimate must resolve the
#: structure's own default of 2 rather than a flat 1; the degenerate field sits
#: below the split floor, so no entry is held out.
FIXTURES: dict[str, Any] = {
    "gauntlet-evidence-gate": _gauntlet_with_evidence_gate,
    "swiss-default-replicates": lambda: _draft("swiss", 9, field_size=4),
    "racing-rung0-override": lambda: _draft("racing", 12, field_size=4, eta=2, rung0_board_size=3),
    "bracket-one-replicate": lambda: _draft("single_elim", 9, field_size=4, replicates=1),
    "degenerate-field-small-board": lambda: _draft("double_elim", 5, field_size=1, replicates=2),
}


def _envelope(name: str, draft: TournamentDraft) -> dict[str, Any]:
    """One fixture in the shape the endpoints serve it and the browser reads it.

    The A/A noise floor is passed explicitly rather than read off a workspace,
    so the findings depend only on the draft.
    """
    cost = ops.estimate_cost(draft)
    warnings = ops.validate(draft, None, noise_floor_max_abs_delta=0.05)
    train_ids, holdout_ids = split_board(draft.entries, draft.scoring.overfitting)
    return {
        "name": name,
        "structure": draft.scoring.tournament_structure.structure,
        "params": dict(draft.scoring.tournament_structure.params),
        "cost": cost.to_dict(),
        "warnings": [w.to_dict() for w in warnings],
        "board_count": len(draft.entries),
        "train_count": len(train_ids),
        "holdout_count": len(holdout_ids),
    }


@pytest.fixture(scope="module")
def envelopes() -> list[dict[str, Any]]:
    return [_envelope(name, build()) for name, build in FIXTURES.items()]


@pytest.fixture(scope="module")
def rendered(envelopes: list[dict[str, Any]], tmp_path_factory: Any) -> list[dict[str, Any]]:
    """The envelopes as the console's own preview module renders them."""
    fixture_file = tmp_path_factory.mktemp("envelopes") / "cost_envelopes.json"
    fixture_file.write_text(json.dumps(envelopes), encoding="utf-8")
    assert READBACK.is_file(), f"missing readback driver: {READBACK}"
    proc = subprocess.run(  # noqa: S603 — _NODE is resolved via shutil.which
        [str(_NODE), str(READBACK), str(fixture_file)],
        cwd=str(STATIC_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"readback failed:\n{proc.stdout}\n{proc.stderr}"
    return list(json.loads(proc.stdout))


def test_rendered_preview_shows_the_python_cost_and_findings(
    envelopes: list[dict[str, Any]], rendered: list[dict[str, Any]]
) -> None:
    """Failure means the page and the estimator disagree about the envelope:
    either a term Python emits never reaches the page, or the page reads a
    field Python stopped emitting."""
    assert rendered == [
        {
            "name": e["name"],
            "board_runs_per_round": e["cost"]["board_runs_per_round"],
            "breakdown": e["cost"]["breakdown"],
            "warnings": [
                {"severity": w["severity"], "message": w["message"]} for w in e["warnings"]
            ],
        }
        for e in envelopes
    ]


def test_the_fixtures_cover_every_cost_term_and_every_entry_free_finding(
    envelopes: list[dict[str, Any]], rendered: list[dict[str, Any]]
) -> None:
    """A term or a rule no fixture reaches could drift unnoticed, so pin that
    the fixtures collectively render every cost line the estimator produces and
    every finding a draft alone, with a known noise floor, can raise."""
    labels = {line["label"] for r in rendered for line in r["breakdown"]}
    # Racing prices one line per rung, so fold the rung index out of the label.
    terms = {re.sub(r"^rung \d+ runs$", "rung runs", label) for label in labels}
    assert terms == {
        "duel runs",
        "bracket-match runs",
        "swiss-pairing runs",
        "rung runs",
        "racing-final runs",
        "holdout-confirm runs",
        "candidate-screen runs",
        "best-of-N propose calls",
        "crowning-confirm runs (evidence gate)",
        "placebo-baseline runs (amortized)",
    }, sorted(terms)

    shown = {w["message"] for r in rendered for w in r["warnings"]}
    codes = {w["code"] for e in envelopes for w in e["warnings"] if w["message"] in shown}
    assert codes == {
        "field_size_degrades_to_gauntlet",
        "holdout_disabled_small_board",
        "racing_rung0_slice",
        "replicates_recommended_for_brackets",
        "margin_below_noise_floor",
    }, sorted(codes)


def test_no_javascript_module_spells_a_cost_line_or_a_finding(
    envelopes: list[dict[str, Any]],
) -> None:
    """A second implementation would have to emit the labels and codes it
    produces as string literals, so a quoted occurrence under ``static/js/`` is
    the signature of a copy growing back. Prose about a finding is not a copy of
    it, so an unquoted mention in a comment passes."""
    owned = {line["label"] for e in envelopes for line in e["cost"]["breakdown"]}
    owned |= {w["code"] for e in envelopes for w in e["warnings"]}
    quoted = [re.compile(rf"[\"'`]{re.escape(literal)}[\"'`]") for literal in sorted(owned)]

    offenders = {
        f"{path.relative_to(STATIC_DIR)}: {pattern.pattern}"
        for path in (STATIC_DIR / "js").rglob("*.js")
        for pattern in quoted
        if pattern.search(path.read_text(encoding="utf-8"))
    }
    assert not offenders, sorted(offenders)
