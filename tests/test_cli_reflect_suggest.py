"""``zicato reflect suggest`` CLI — the WS-SURFACE round-trip (EVAL-SYNTHESIS.md §6).

Exercises the surface end to end with MOCKED synth/admit seams (the three
workstreams build in parallel against the doc shapes, so the CLI is driven by
fakes here, zero live spend):

* ``suggest`` (default, no probe) mines + synthesises + persists, and SPENDS
  NOTHING — the admission seam is never consulted (plan mode);
* ``report`` renders the persisted suggestions honestly;
* ``apply`` forks a builder draft carrying the entry suggestion (through the new
  ``add_board_entry`` op) and the judge suggestion (through ``add_judge``), each
  with provenance, while the sealed contract stays byte-unchanged;
* ``--probe`` consults the admission seam (the endpoint-gated spend).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.board.jsonl import save_board
from zicato.cli.discovery import build_cli_root
from zicato.core.types import BoardEntry, ScoringWeights
from zicato.core.workspace import board_path, reflection_suggestions_path
from zicato.epoch.lifecycle import new_epoch
from zicato.reflection import suggestions as sug_mod
from zicato.reflection.suggestions import Suggestion

_REFLECTION_ID = "refl-suggest-test"


def _entry_artifact() -> dict[str, object]:
    return {
        "id": "entryA_regression",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 30,
        "input": "the failing prompt, pinned",
        "context": {"provenance": json.dumps({"miner_version": "eval-synth/1"})},
    }


def _fake_suggestions() -> list[Suggestion]:
    prov = {
        "miner_version": "eval-synth/1",
        "source_episodes": ["ep-aaaa1111"],
        "source_refs": ["loss/g0"],
        "source_lineage_ids": ["g0"],
        "suggestion_type": "regression_entry",
        "target_slice": "train",
    }
    entry_sug = Suggestion(
        suggestion_id="sug-entry01",
        suggestion_type="regression_entry",
        artifact_kind="board_entry",
        subject="entryA",
        summary="pin the g0 predicate miss as a regression",
        rationale="entryA failed its expectation on g0",
        target_slice="train",
        draft_artifact=_entry_artifact(),
        proposed_op={"op": "add_board_entry", "args": {"entry": _entry_artifact()}},
        provenance=prov,
        admission=None,
        severity_rank=4,
        recency_key=0,
        coverage_key=1,
    )
    judge_sug = Suggestion(
        suggestion_id="sug-judge01",
        suggestion_type="judge_suggestion",
        artifact_kind="judge",
        subject="citations",
        summary="draft a citations judge from the missed FN span",
        rationale="the meta-judge found a failure no judge catches",
        target_slice="incoming_rotation",
        draft_artifact={
            "name": "citations",
            "mode": "inline",
            "body": "flags uncited claims",
            "severity": "warning",
        },
        proposed_op={
            "op": "add_judge",
            "args": {
                "entry_id": "entryA",
                "judge": {
                    "name": "citations",
                    "mode": "inline",
                    "body": "flags uncited claims",
                    "severity": "warning",
                },
            },
        },
        provenance={
            "miner_version": "eval-synth/1",
            "source_episodes": ["ep-bbbb2222"],
            "source_lineage_ids": ["g0"],
            "suggestion_type": "judge_suggestion",
            "target_slice": "incoming_rotation",
        },
        admission=None,
        severity_rank=5,
        recency_key=0,
        coverage_key=1,
    )
    return [entry_sug, judge_sug]


class _AdmitSpy:
    """A fake admission seam that records whether it was consulted (the spend)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self,
        suggestions: object,
        *,
        probe: bool = False,
        workspace_root: Path | None = None,
        epoch_id: str | None = None,
    ) -> list[Suggestion]:
        self.calls += 1
        out: list[Suggestion] = []
        for s in list(suggestions):  # type: ignore[arg-type]
            stamped = {
                "execution": {"ran": True, "aborted": False},
                "noise": {"flip_rate": 0.10, "runs": 5, "measured": True, "base": 6000},
                "discrimination": {"separated": 3, "pairs": 5, "measured": True},
                "leakage": {"target_slice_ok": True, "self_preference_flag": False},
            }
            out.append(
                Suggestion.from_json({**s.to_json(), "admission": stamped})  # type: ignore[attr-defined]
            )
        return out


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, str]:
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    (ws / "config.json").write_text(json.dumps({"runtime": {}, "adapter": {}}), encoding="utf-8")
    entry = BoardEntry(id="entryA", kind="single_turn", wall_clock_budget_seconds=30, input="hi")
    board_file = tmp_path / "board.jsonl"
    save_board([entry], board_file)
    cfg = new_epoch(ws, "sugtest", board_file, "steer", ScoringWeights())
    return ws, cfg.id


def _run(args: list[str]) -> object:
    return CliRunner(mix_stderr=False).invoke(build_cli_root(), args)


def _seed_synth(monkeypatch: pytest.MonkeyPatch) -> None:
    def _synth(episodes: object, *, allow_llm: bool = False) -> list[Suggestion]:
        return _fake_suggestions()

    monkeypatch.setattr(sug_mod, "resolve_synthesize", lambda: _synth)


def test_suggest_default_spends_nothing_and_persists(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, epoch_id = workspace
    _seed_synth(monkeypatch)
    spy = _AdmitSpy()
    monkeypatch.setattr(sug_mod, "resolve_admit", lambda: spy)

    result = _run(["reflect", "suggest", "--workspace", str(ws), "--reflection", _REFLECTION_ID])
    assert result.exit_code == 0, result.output
    # Plan mode: the admission seam (the only live-spend surface) is NEVER consulted.
    assert spy.calls == 0
    assert "plan mode" in result.stderr
    # Persisted beside findings.json, admission unmeasured (honest).
    persisted = json.loads(
        reflection_suggestions_path(ws, epoch_id, _REFLECTION_ID).read_text(encoding="utf-8")
    )
    ids = {s["suggestion_id"] for s in persisted["suggestions"]}
    assert ids == {"sug-entry01", "sug-judge01"}
    assert all(s["admission"] is None for s in persisted["suggestions"])


def test_suggest_probe_consults_admission_seam(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, epoch_id = workspace
    _seed_synth(monkeypatch)
    spy = _AdmitSpy()
    monkeypatch.setattr(sug_mod, "resolve_admit", lambda: spy)

    result = _run(
        ["reflect", "suggest", "--workspace", str(ws), "--reflection", _REFLECTION_ID, "--probe"]
    )
    assert result.exit_code == 0, result.output
    assert spy.calls == 1  # the probe tier consults admission (the spend)
    persisted = json.loads(
        reflection_suggestions_path(ws, epoch_id, _REFLECTION_ID).read_text(encoding="utf-8")
    )
    noise = {s["suggestion_id"]: s["admission"]["noise"] for s in persisted["suggestions"]}
    assert noise["sug-entry01"]["measured"] is True
    assert noise["sug-entry01"]["base"] == 6000


def test_report_renders_persisted_suggestions(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, epoch_id = workspace
    _seed_synth(monkeypatch)
    monkeypatch.setattr(sug_mod, "resolve_admit", lambda: None)
    _run(["reflect", "suggest", "--workspace", str(ws), "--reflection", _REFLECTION_ID])

    result = _run(["reflect", "report", _REFLECTION_ID, "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "Eval suggestions" in result.output
    assert "regression_entry" in result.output
    assert "unmeasured" in result.output  # honest admission render


def test_apply_entry_suggestion_carries_the_entry_with_provenance(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, epoch_id = workspace
    _seed_synth(monkeypatch)
    monkeypatch.setattr(sug_mod, "resolve_admit", lambda: None)
    _run(["reflect", "suggest", "--workspace", str(ws), "--reflection", _REFLECTION_ID])

    before = board_path(ws, epoch_id).read_bytes()
    result = _run(["reflect", "apply", _REFLECTION_ID, "sug-entry01", "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "add_board_entry" in result.output
    assert "staged suggestion sug-entry01" in result.output
    # The sealed contract is byte-unchanged — apply only stages a DRAFT.
    assert board_path(ws, epoch_id).read_bytes() == before

    # The forked draft carries the new entry with its provenance context.
    from zicato.builder.draft import DraftStore

    store = DraftStore()
    draft = store.fork(session_id="verify", name="reflect-verify", workspace_root=ws)
    from zicato.reflection.apply import apply_suggestion_to_draft

    applied = apply_suggestion_to_draft(
        workspace_root=ws,
        epoch_id=epoch_id,
        reflection_id=_REFLECTION_ID,
        suggestion_id="sug-entry01",
    )
    assert applied.op == "add_board_entry"
    assert applied.patch["changed"]["entry_id"] == "entryA_regression"
    assert "board" in applied.diff["changed_components"]
    _ = draft


def test_apply_judge_suggestion_carries_the_judge(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, epoch_id = workspace
    _seed_synth(monkeypatch)
    monkeypatch.setattr(sug_mod, "resolve_admit", lambda: None)
    _run(["reflect", "suggest", "--workspace", str(ws), "--reflection", _REFLECTION_ID])

    from zicato.reflection.apply import apply_suggestion_to_draft

    applied = apply_suggestion_to_draft(
        workspace_root=ws,
        epoch_id=epoch_id,
        reflection_id=_REFLECTION_ID,
        suggestion_id="sug-judge01",
    )
    assert applied.op == "add_judge"
    assert applied.patch["changed"] == {"entry_id": "entryA", "judge": "citations"}
