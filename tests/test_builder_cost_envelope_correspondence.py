"""The browser shows exactly the cost and lint findings Python computed.

The tournament builder's cost estimate and its recommend-only lint findings
have one owner: :func:`zicato.builder.operations.estimate_cost` and
:func:`zicato.builder.operations.validate`. Both endpoints the console calls
(``POST /builder/op`` after every edit, ``GET /builder/draft`` for the
read-only contract summary in Settings) carry their results in the response
envelope, and the browser renders that envelope without recomputing any of it.

Two independent implementations of the same arithmetic would drift; nothing
would notice, because each side's tests would pass on its own numbers. This
module removes the possibility of a silent drift by checking the join instead:
for a fixed set of drafts it computes the envelope in Python, hands it to the
production renderer (``static/js/builder/preview.js``) running under node, and
requires the numbers and texts read back off the rendered nodes to equal the
ones Python produced. A key renamed on either side breaks the readback.
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
        BoardEntry(
            id=f"e{i}",
            kind="single_turn",
            wall_clock_budget_seconds=60,
            input="hello",
        )
        for i in range(n)
    ]


def _gauntlet_with_evidence_gate() -> TournamentDraft:
    """A gauntlet whose evidence gate, screening and placebo arm are all on.

    Exercises the terms that are absent from a bare draft: the crowning-confirm
    budget, the auxiliary best-of-N propose calls, the candidate-screen panel
    and the amortized placebo baseline.
    """
    draft = TournamentDraft()
    draft.entries = _board(10)
    ops.set_structure(draft, "gauntlet")
    ops.set_param(draft, "field_size", 1)
    ops.set_param(draft, "replicates", 2)
    ops.set_param(draft, "promote_confidence_threshold", 0.8)
    ops.set_param(draft, "promote_confidence_replicates", 8)
    ops.set_screening(draft, entries=3)
    ops.set_proposer_quality(draft, best_of_n=3)
    ops.set_holdout(draft, random_baseline_every_n=4)
    return draft


def _swiss_without_replicates() -> TournamentDraft:
    """Swiss with ``replicates`` unset, which is where under-reporting hid.

    The estimate must resolve the structure's own default (2 for swiss) rather
    than a flat 1, so this draft pins that the number the browser shows is the
    schedule the run would actually spend.
    """
    draft = TournamentDraft()
    draft.entries = _board(9)
    ops.set_structure(draft, "swiss")
    ops.set_param(draft, "field_size", 4)
    return draft


def _racing_with_rung0_override() -> TournamentDraft:
    """Racing with an explicit rung-0 slice, so the rung ladder is priced."""
    draft = TournamentDraft()
    draft.entries = _board(12)
    ops.set_structure(draft, "racing")
    ops.set_param(draft, "field_size", 4)
    ops.set_param(draft, "eta", 2)
    ops.set_param(draft, "rung0_board_size", 3)
    return draft


def _bracket_on_one_replicate() -> TournamentDraft:
    """A knockout bracket whose single replicate lets noise decide a match."""
    draft = TournamentDraft()
    draft.entries = _board(9)
    ops.set_structure(draft, "single_elim")
    ops.set_param(draft, "field_size", 4)
    ops.set_param(draft, "replicates", 1)
    return draft


def _degenerate_field_on_a_small_board() -> TournamentDraft:
    """A bracket with one challenger on a board too small to hold anything out.

    ``field_size=1`` degrades the bracket to a single champion-versus-challenger
    duel, and six entries sit below the split floor, so no entry is held out.
    """
    draft = TournamentDraft()
    draft.entries = _board(5)
    ops.set_structure(draft, "double_elim")
    ops.set_param(draft, "field_size", 1)
    ops.set_param(draft, "replicates", 2)
    return draft


#: The drafts the correspondence runs over, named for what each one exercises.
FIXTURES: dict[str, Any] = {
    "gauntlet-evidence-gate": _gauntlet_with_evidence_gate,
    "swiss-default-replicates": _swiss_without_replicates,
    "racing-rung0-override": _racing_with_rung0_override,
    "bracket-one-replicate": _bracket_on_one_replicate,
    "degenerate-field-small-board": _degenerate_field_on_a_small_board,
}


def _envelope(name: str, draft: TournamentDraft) -> dict[str, Any]:
    """One fixture in the shape the endpoints serve it and the browser reads it.

    ``noise_floor_max_abs_delta`` is passed explicitly rather than read off a
    workspace, so the lint findings depend only on the draft.
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


def _expected(envelope: dict[str, Any]) -> dict[str, Any]:
    """What the rendered preview must show for one envelope."""
    return {
        "name": envelope["name"],
        "board_runs_per_round": envelope["cost"]["board_runs_per_round"],
        "breakdown": envelope["cost"]["breakdown"],
        "warnings": [
            {"severity": w["severity"], "message": w["message"]} for w in envelope["warnings"]
        ],
    }


def _render(envelopes: list[dict[str, Any]], tmp_path: Path) -> list[dict[str, Any]]:
    """Render the envelopes through the console's own preview module."""
    fixture_file = tmp_path / "cost_envelopes.json"
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


def test_rendered_preview_shows_the_python_cost_and_findings(tmp_path: Path) -> None:
    """Every fixture renders the estimate and the findings Python produced.

    Failure here means the browser and the estimator disagree about the
    envelope: either a term Python emits never reaches the page, or the page
    reads a field Python stopped emitting.
    """
    envelopes = [_envelope(name, build()) for name, build in FIXTURES.items()]
    rendered = _render(envelopes, tmp_path)
    assert rendered == [_expected(e) for e in envelopes]


def test_the_fixtures_cover_every_cost_term_and_every_entry_free_finding(
    tmp_path: Path,
) -> None:
    """The correspondence is only worth as much as the terms it reaches.

    A term or a lint rule no fixture triggers could drift unnoticed, so pin
    that the fixture set collectively renders every cost line the estimator can
    produce and every finding a draft alone (with a known noise floor) can
    raise. Adding a term or a rule without a fixture that reaches it fails
    here rather than passing silently.
    """
    envelopes = [_envelope(name, build()) for name, build in FIXTURES.items()]
    rendered = _render(envelopes, tmp_path)

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
    codes = {
        w["code"] for envelope in envelopes for w in envelope["warnings"] if w["message"] in shown
    }
    assert codes == {
        "field_size_degrades_to_gauntlet",
        "holdout_disabled_small_board",
        "racing_rung0_slice",
        "replicates_recommended_for_brackets",
        "margin_below_noise_floor",
    }, sorted(codes)


def test_no_javascript_module_spells_a_cost_line_or_a_finding(tmp_path: Path) -> None:
    """No frontend module re-derives what the estimator and the validator own.

    A second implementation would have to emit the terms and the codes it
    produces as string literals, so a quoted occurrence anywhere in the
    console's JavaScript is the signature of a copy growing back. The renderer
    reads labels and messages out of the envelope and never spells one itself.
    Prose about a finding is not a copy of it, so an unquoted mention in a
    comment passes.
    """
    envelopes = [_envelope(name, build()) for name, build in FIXTURES.items()]
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
