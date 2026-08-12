"""The board page's Judges panel payloads (#194 §5).

Two readers, deliberately kept apart:

* ``_parse_board_judges`` — the AUTHORED half. A sibling of ``_parse_board``
  (never a widening of it) projecting each entry's declared process judges
  onto the epoch view as ``board_judges``. Omitted when the board declares
  none, so a judge-free epoch's payload stays byte-identical.
* ``build_judge_roster`` — the DERIVED half. goldfive's built-in judge set
  marked up with what the board's ``disable_drift`` header actually
  suppressed, the frozen per-judge weights, and each judge's reflection
  scorecard.

The sharpest case here is the ``disable_drift`` kind that no built-in judge
emits: it suppresses NOTHING, and the roster must report it as such rather
than let the panel imply a disarm that never happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.test_no_goldfive_import import _ok, _run_without_goldfive
from zicato.dashboard.server import create_app
from zicato.query import WorkspacePaths, build_epoch_view, build_judge_roster
from zicato.query.epoch_view import _parse_board_judges
from zicato.query.judge_roster import NO_GOLDFIVE_NOTE

EPOCH = "2026-06-07_e4"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _board(*rows: dict) -> str:
    return "\n".join(json.dumps(r) for r in rows) + "\n"


_ENTRY_WITH_BOTH_MODES = {
    "id": "transformers_lay_audience",
    "kind": "single_turn",
    "input": "Build slides explaining transformers.",
    "expectation": {"kind": "predicate", "spec": "pkg.preds:mentions"},
    "judges": [
        {
            "name": "audience_appropriate",
            "mode": "inline",
            "body": "The agent keeps the explanation accessible to a non-ML audience.",
            "severity": "warning",
        },
        {
            "name": "file_findability",
            "mode": "python",
            "body": "pkg.judges:FileFindabilityJudge",
            "severity": "critical",
        },
    ],
}

_ENTRY_WITHOUT_JUDGES = {
    "id": "waffles_single",
    "kind": "single_turn",
    "input": "Make a presentation about waffles.",
    "expectation": {"kind": "predicate", "spec": "pkg.preds:waffles"},
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A one-epoch workspace mirroring the shape the June target_1 run wrote."""
    ws = tmp_path / ".zicato"
    _write(ws / "current_epoch", EPOCH + "\n")
    _write(ws / "config.json", json.dumps({"adapter": {"entrypoint": "pkg.mod:agent"}}))
    edir = ws / "epochs" / EPOCH
    _write(edir / "config.json", json.dumps({"id": EPOCH, "created_at": "2026-06-07T00:00:00Z"}))
    _write(
        edir / "board.jsonl",
        _board(
            {"board_meta": True, "disable_drift": ["tool_error", "user_steer"], "judge_only": True},
            _ENTRY_WITH_BOTH_MODES,
            _ENTRY_WITHOUT_JUDGES,
        ),
    )
    _write(
        edir / "scoring.json",
        json.dumps({"default_judge_weight": 1.0, "per_judge_weights": {"file_findability": 2.0}}),
    )
    return ws


# ---------------------------------------------------------------------------
# _parse_board_judges — the authored half
# ---------------------------------------------------------------------------


def test_projects_names_and_metadata_only(workspace: Path) -> None:
    """Both modes project name / mode / severity; only python carries its path.

    An inline judge's ``body`` is the criterion PROMPT — it must never reach
    the wire. A python judge's is a dotted import path, which is the only
    thing telling two python judges apart on screen.
    """
    judges = _parse_board_judges(workspace / "epochs" / EPOCH / "board.jsonl")
    assert judges is not None
    assert list(judges) == ["transformers_lay_audience"]  # the judge-free entry is absent
    inline, python = judges["transformers_lay_audience"]

    assert inline == {"name": "audience_appropriate", "mode": "inline", "severity": "warning"}
    assert python == {
        "name": "file_findability",
        "mode": "python",
        "severity": "critical",
        "path": "pkg.judges:FileFindabilityJudge",
    }

    # The prompt text appears nowhere in the payload, at any depth.
    assert "accessible to a non-ML audience" not in json.dumps(judges)


def test_empty_when_no_entry_declares_a_judge(tmp_path: Path) -> None:
    """A judge-free board yields ``None`` so the epoch key is omitted."""
    board = tmp_path / "board.jsonl"
    _write(board, _board({"board_meta": True, "judge_only": True}, _ENTRY_WITHOUT_JUDGES))
    assert _parse_board_judges(board) is None


def test_degrades_per_row_not_per_file(tmp_path: Path) -> None:
    """A torn line, a non-object judge, and an unnamed judge drop; siblings survive."""
    board = tmp_path / "board.jsonl"
    _write(
        board,
        json.dumps({"id": "a", "judges": [{"name": "keeps", "mode": "inline", "severity": "info"}]})
        + "\n{not json at all\n"
        + json.dumps({"id": "b", "judges": ["a bare string", {"mode": "inline"}]})
        + "\n"
        + json.dumps({"id": "c", "judges": "not a list"})
        + "\n"
        + json.dumps({"judges": [{"name": "no_entry_id"}]})
        + "\n",
    )
    judges = _parse_board_judges(board)
    # ``b`` declared only unusable judges, so it contributes no row at all —
    # an entry key mapping to [] would read as "judges configured, none shown".
    assert judges == {"a": [{"name": "keeps", "mode": "inline", "severity": "info"}]}


def test_missing_board_is_not_an_error(tmp_path: Path) -> None:
    assert _parse_board_judges(tmp_path / "nope.jsonl") is None


def test_epoch_view_carries_board_judges_and_omits_them_when_absent(workspace: Path) -> None:
    """The key rides the epoch view, and vanishes for a judge-free board."""
    view = build_epoch_view(WorkspacePaths(workspace), EPOCH)
    assert set(view["board_judges"]) == {"transformers_lay_audience"}
    # ``board`` itself is untouched — the projection is a SIBLING, so no board
    # row gained a key and every _parse_board consumer reads what it always did.
    assert all("judges" not in row for row in view["board"])

    _write(
        workspace / "epochs" / EPOCH / "board.jsonl",
        _board({"board_meta": True, "disable_drift": ["tool_error"]}, _ENTRY_WITHOUT_JUDGES),
    )
    assert "board_judges" not in build_epoch_view(WorkspacePaths(workspace), EPOCH)


# ---------------------------------------------------------------------------
# build_judge_roster — the derived half
# ---------------------------------------------------------------------------


def _named(roster: dict, name: str) -> dict:
    return next(b for b in roster["builtins"] if b["name"] == name)


def test_suppression_is_marked_not_filtered(workspace: Path) -> None:
    """A suppressed built-in stays on the roster, flagged with the kind that did it.

    Filtering it out would show the header's effect by omission — i.e. show
    nothing at all, which is the surface this panel replaces.
    """
    roster = build_judge_roster(WorkspacePaths(workspace), EPOCH)
    assert roster["builtins_note"] is None
    assert _named(roster, "tool_error") == {
        "name": "tool_error",
        "suppressed": True,
        "suppressed_by": ["tool_error"],
    }
    assert _named(roster, "refusal")["suppressed"] is False
    # Every default judge is present, suppressed or not.
    assert len(roster["builtins"]) >= 5


def test_a_kind_no_builtin_emits_suppresses_nothing(workspace: Path) -> None:
    """``user_steer`` is a real drift kind that no BUILT-IN judge emits.

    The mapping in ``judge_runtime.disable`` is deliberately partial, so such
    a kind is a documented no-op. The roster names it rather than leaving the
    panel to imply a disarm that never happened.
    """
    roster = build_judge_roster(WorkspacePaths(workspace), EPOCH)
    assert roster["disable_drift"] == ["tool_error", "user_steer"]
    assert roster["unmapped_drift_kinds"] == ["user_steer"]
    assert not any("user_steer" in b["suppressed_by"] for b in roster["builtins"])


def test_no_header_suppresses_nothing(workspace: Path) -> None:
    _write(
        workspace / "epochs" / EPOCH / "board.jsonl",
        _board(_ENTRY_WITH_BOTH_MODES),
    )
    roster = build_judge_roster(WorkspacePaths(workspace), EPOCH)
    assert roster["disable_drift"] == []
    assert roster["unmapped_drift_kinds"] == []
    assert all(b["suppressed"] is False for b in roster["builtins"])


def test_weights_come_from_the_frozen_scoring_block(workspace: Path) -> None:
    """``per_judge_weights`` keys on judge NAME, across both halves alike."""
    roster = build_judge_roster(WorkspacePaths(workspace), EPOCH)
    assert roster["per_judge_weights"] == {"file_findability": 2.0}
    assert roster["default_judge_weight"] == 1.0


def test_malformed_weights_drop_rather_than_raise(workspace: Path) -> None:
    _write(
        workspace / "epochs" / EPOCH / "scoring.json",
        json.dumps(
            {"per_judge_weights": {"good": 0.5, "bad": "heavy"}, "default_judge_weight": []}
        ),
    )
    roster = build_judge_roster(WorkspacePaths(workspace), EPOCH)
    assert roster["per_judge_weights"] == {"good": 0.5}
    assert roster["default_judge_weight"] is None


@pytest.mark.parametrize("bad", ["no_such_epoch", "../secrets"])
def test_an_unresolvable_epoch_yields_the_empty_shape(workspace: Path, bad: str) -> None:
    """A reader never raises — an unknown or unsafe id describes no epoch."""
    assert build_judge_roster(WorkspacePaths(workspace), bad) == {
        "epoch_id": None,
        "builtins": [],
        "builtins_note": None,
        "disable_drift": [],
        "unmapped_drift_kinds": [],
        "per_judge_weights": {},
        "default_judge_weight": None,
        "scorecards": {},
    }


def test_scorecards_link_the_newest_reflection_that_scored_each_judge(workspace: Path) -> None:
    """Two reflections score the same judge; the newer one wins the link."""
    refls = workspace / "epochs" / EPOCH / "reflections"
    older, newer = "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z"
    for rid, created in (("refl_old", older), ("refl_new", newer)):
        _write(
            refls / rid / "plan.json",
            json.dumps({"reflection_id": rid, "epoch_id": EPOCH, "created_at": created}),
        )
        _write(
            refls / rid / "scorecards.json",
            json.dumps({"scorecards": [{"judge_name": "file_findability", "precision": 0.9}]}),
        )
    _write(
        refls / "refl_new" / "scorecards.json",
        json.dumps(
            {
                "scorecards": [
                    {"judge_name": "file_findability", "precision": 0.9},
                    {"judge_name": "audience_appropriate", "precision": 0.4},
                ]
            }
        ),
    )

    roster = build_judge_roster(WorkspacePaths(workspace), EPOCH)
    assert roster["scorecards"] == {
        "file_findability": "refl_new",
        "audience_appropriate": "refl_new",
    }


def test_no_reflection_means_no_link_not_a_broken_one(workspace: Path) -> None:
    assert build_judge_roster(WorkspacePaths(workspace), EPOCH)["scorecards"] == {}


def test_a_corrupt_index_does_not_break_the_roster(workspace: Path) -> None:
    """The reflection lookup is best-effort — a poisoned index degrades to no links."""
    _write(workspace / "index.db", "this is not a sqlite database")
    roster = build_judge_roster(WorkspacePaths(workspace), EPOCH)
    assert roster["scorecards"] == {}
    assert _named(roster, "tool_error")["suppressed"] is True


# ---------------------------------------------------------------------------
# goldfive is an optional extra
# ---------------------------------------------------------------------------


def test_roster_degrades_honestly_without_goldfive(workspace: Path) -> None:
    """No goldfive ⇒ no built-in roster to enumerate, and the reader says why.

    The suppression MAPPING is a plain dict and needs no goldfive, so the
    unmapped-kind analysis must survive intact even when the roster cannot be
    listed — the panel then still reports what the header did and did not do.
    """
    stdout = _ok(
        _run_without_goldfive(
            f"""
            import json
            from zicato.query import WorkspacePaths, build_judge_roster

            roster = build_judge_roster(WorkspacePaths({str(workspace)!r}), {EPOCH!r})
            assert roster["builtins"] == [], roster["builtins"]
            assert roster["disable_drift"] == ["tool_error", "user_steer"], roster
            assert roster["unmapped_drift_kinds"] == ["user_steer"], roster
            assert roster["per_judge_weights"] == {{"file_findability": 2.0}}, roster
            print(roster["builtins_note"])
            """
        )
    )
    assert stdout.strip() == NO_GOLDFIVE_NOTE


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(workspace: Path, tmp_path: Path) -> TestClient:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>zicato</title>", encoding="utf-8")
    (static / "app_T.js").write_text("// app", encoding="utf-8")
    with TestClient(create_app(workspace, static, read_only=True)) as c:
        yield c


def test_endpoint_serves_the_roster(client: TestClient) -> None:
    r = client.get(f"/api/epoch/{EPOCH}/judge-roster")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch_id"] == EPOCH
    assert body["unmapped_drift_kinds"] == ["user_steer"]


def test_endpoint_degrades_on_a_malformed_id(client: TestClient) -> None:
    """An id the validator rejects degrades to the empty shape, never a 500."""
    r = client.get("/api/epoch/not a safe id/judge-roster")
    assert r.status_code == 200
    assert r.json()["builtins"] == []


def test_endpoint_degrades_on_an_unknown_epoch(client: TestClient) -> None:
    """A well-formed id for an epoch that does not exist reads as no epoch."""
    r = client.get("/api/epoch/2099-01-01_e9/judge-roster")
    assert r.status_code == 200
    assert r.json() == {
        "epoch_id": None,
        "builtins": [],
        "builtins_note": None,
        "disable_drift": [],
        "unmapped_drift_kinds": [],
        "per_judge_weights": {},
        "default_judge_weight": None,
        "scorecards": {},
    }
