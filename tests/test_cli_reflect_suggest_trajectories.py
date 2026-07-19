"""``zicato reflect suggest --from-trajectories`` — WS-WIRE (TRAJECTORY-BOOTSTRAP.md §6).

The trajectory-bootstrap surface on ``reflect suggest``: a directory of foreign
agent trace files is imported (format-sniffed + reduced through the existing
dialect reducers), persisted under the minted reflection dir, and mined
ALONGSIDE the workspace episodes. These tests drive the CLI flag matrix with a
MOCKED synth seam AT §7's extended signature (``imported_traces=``) — the
un-mocked whole-chain proof lives in ``test_trajectory_bootstrap_composition``
and activates at the integration merge. Covered here:

* the flag matrix — a missing dir, an empty dir, a mixed-format dir;
* both-sources mining folds imported + workspace episodes into one ranked list;
* the imported records persist under ``imported/`` with the sniffed dialects;
* plan mode still SPENDS NOTHING with the new flag;
* the foreign-source provenance block renders in ``reflect report``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from zicato.board.jsonl import save_board
from zicato.cli.discovery import build_cli_root
from zicato.core.types import BoardEntry, ScoringWeights
from zicato.core.workspace import reflection_suggestions_path
from zicato.epoch.lifecycle import new_epoch
from zicato.reflection import suggestions as sug_mod
from zicato.reflection.suggestions import Suggestion

_FIXTURES = Path(__file__).parent / "fixtures"
_TRAJ_DIR = _FIXTURES / "trajectories"
_TRAJ_GF_FREE = _FIXTURES / "trajectories_goldfive_free"
_TRAJ_EMPTY = _FIXTURES / "trajectories_empty"
_TRAJ_INVALID_BYTES = _FIXTURES / "trajectories_invalid_bytes"
_REFLECTION_ID = "refl-traj-test"


def _foreign_suggestion() -> Suggestion:
    """A bootstrap-shaped surface suggestion (foreign_source provenance)."""
    entry = {
        "id": "bootstrap_error_cascade",
        "kind": "single_turn",
        "wall_clock_budget_seconds": 30,
        "input": "the recorded opening turn",
    }
    return Suggestion(
        suggestion_id="sug-boot001",
        suggestion_type="regression_entry",
        artifact_kind="board_entry",
        subject="trace-a0be332d",
        summary="pin the error cascade observed in prod-run-01",
        rationale="a foreign trace (adk_run.jsonl) showed a tool-error cascade",
        target_slice="train",
        draft_artifact=entry,
        proposed_op={"op": "add_board_entry", "args": {"entry": entry}},
        provenance={
            "miner_version": "eval-synth/1",
            "source_episodes": ["ep-cccc3333"],
            "source_refs": ["adk_run.jsonl", "error_cascade"],
            "source_lineage_ids": [],
            "suggestion_type": "regression_entry",
            "target_slice": "train",
            "foreign_source": {
                "kind": "trajectory_bootstrap",
                "dialect": "adk_events",
                "trace_id": "trace-a0be332d",
                "source_file": "adk_run.jsonl",
            },
        },
        admission=None,
        severity_rank=5,
        recency_key=0,
        coverage_key=1,
    )


class _SynthSpy:
    """A fake synth seam at §7's extended signature; records the episodes it saw."""

    def __init__(self, out: list[Suggestion]) -> None:
        self.out = out
        self.episodes: list[object] = []
        self.imported_traces: object = None

    def __call__(
        self,
        episodes: object,
        *,
        allow_llm: bool = False,
        workspace_root: Path | None = None,
        epoch_id: str | None = None,
        imported_traces: object = (),
    ) -> list[Suggestion]:
        self.episodes = list(episodes)  # type: ignore[arg-type]
        self.imported_traces = imported_traces
        return self.out


class _AdmitSpy:
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
        return list(suggestions)  # type: ignore[arg-type]


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, str]:
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    (ws / "config.json").write_text(json.dumps({"runtime": {}, "adapter": {}}), encoding="utf-8")
    entry = BoardEntry(id="entryA", kind="single_turn", wall_clock_budget_seconds=30, input="hi")
    board_file = tmp_path / "board.jsonl"
    save_board([entry], board_file)
    cfg = new_epoch(ws, "trajtest", board_file, "steer", ScoringWeights())
    return ws, cfg.id


def _run(args: list[str]) -> object:
    return CliRunner(mix_stderr=False).invoke(build_cli_root(), args)


def test_missing_trajectory_dir_is_an_honest_exit0(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, epoch_id = workspace
    spy = _SynthSpy([_foreign_suggestion()])
    monkeypatch.setattr(sug_mod, "resolve_synthesize", lambda: spy)
    monkeypatch.setattr(sug_mod, "resolve_admit", lambda: None)

    result = _run(
        [
            "reflect",
            "suggest",
            "--workspace",
            str(ws),
            "--reflection",
            _REFLECTION_ID,
            "--from-trajectories",
            str(ws / "does_not_exist"),
        ]
    )
    assert result.exit_code == 0, result.output
    assert "no importable" in result.output
    # Nothing was synthesised or persisted — the honest empty degrade.
    assert spy.episodes == []
    assert not reflection_suggestions_path(ws, epoch_id, _REFLECTION_ID).exists()


def test_empty_trajectory_dir_is_an_honest_exit0(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, epoch_id = workspace
    spy = _SynthSpy([_foreign_suggestion()])
    monkeypatch.setattr(sug_mod, "resolve_synthesize", lambda: spy)
    monkeypatch.setattr(sug_mod, "resolve_admit", lambda: None)

    result = _run(
        [
            "reflect",
            "suggest",
            "--workspace",
            str(ws),
            "--reflection",
            _REFLECTION_ID,
            "--from-trajectories",
            str(_TRAJ_EMPTY),
        ]
    )
    assert result.exit_code == 0, result.output
    assert "no importable" in result.output
    assert not reflection_suggestions_path(ws, epoch_id, _REFLECTION_ID).exists()


def test_invalid_bytes_dir_survives_exit0(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # BLOCKER regression: a trace directory whose member carries invalid UTF-8
    # bytes must NOT crash the CLI (a propagated UnicodeDecodeError). The import
    # tolerates it (malformed line counted, valid lines kept) and the run exits 0.
    ws, _epoch_id = workspace
    spy = _SynthSpy([_foreign_suggestion()])
    monkeypatch.setattr(sug_mod, "resolve_synthesize", lambda: spy)
    monkeypatch.setattr(sug_mod, "resolve_admit", lambda: None)

    result = _run(
        [
            "reflect",
            "suggest",
            "--workspace",
            str(ws),
            "--reflection",
            _REFLECTION_ID,
            "--from-trajectories",
            str(_TRAJ_INVALID_BYTES),
        ]
    )
    assert result.exit_code == 0, result.output
    # The invalid-byte trace was imported, not skipped or crashed.
    assert "imported" in result.stderr


def test_mixed_format_dir_imports_persists_and_mines(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, epoch_id = workspace
    spy = _SynthSpy([_foreign_suggestion()])
    monkeypatch.setattr(sug_mod, "resolve_synthesize", lambda: spy)
    monkeypatch.setattr(sug_mod, "resolve_admit", lambda: None)

    result = _run(
        [
            "reflect",
            "suggest",
            "--workspace",
            str(ws),
            "--reflection",
            _REFLECTION_ID,
            "--from-trajectories",
            str(_TRAJ_DIR),
        ]
    )
    assert result.exit_code == 0, result.output
    assert "imported" in result.stderr

    # The imported records persisted under imported/{trace_id}.json with the
    # sniffed dialects (the mixed-format dir sniffs goldfive/adk_events/transcript).
    from zicato.reflection.trace_import import read_imported_traces

    persisted_traces = read_imported_traces(ws, epoch_id, _REFLECTION_ID)
    dialects = {t.dialect for t in persisted_traces}
    assert {"goldfive", "adk_events", "transcript"} <= dialects

    # The imported episodes reached the synth seam (mining folded them).
    imported_types = {getattr(e, "episode_type", "") for e in spy.episodes}
    assert "imported_signal" in imported_types

    # The suggestions persisted; the foreign-source block survived the round-trip.
    persisted = json.loads(
        reflection_suggestions_path(ws, epoch_id, _REFLECTION_ID).read_text(encoding="utf-8")
    )
    foreign = persisted["suggestions"][0]["provenance"]["foreign_source"]
    assert foreign["dialect"] == "adk_events"
    assert foreign["source_file"] == "adk_run.jsonl"


def test_both_sources_mining_folds_imported_and_workspace_episodes(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # mine_episodes folds imported episodes (mined first, unconditionally) with
    # the workspace episodes when an epoch resolves, then ranks the union.
    ws, epoch_id = workspace
    from zicato.query.paths import WorkspacePaths
    from zicato.reflection import mining
    from zicato.reflection.mining import MinedEpisode
    from zicato.reflection.trace_import import import_trajectories

    sentinel = MinedEpisode(
        episode_id="ep-workspace1",
        episode_type="failure_regression",
        subject="entryA",
        summary="a workspace failure episode",
        severity_rank=1,
        recency_key=0,
        coverage_key=1,
        suggestion_hint="regression_entry",
    )
    monkeypatch.setattr(mining, "failure_episodes", lambda observations: [sentinel])

    traces = import_trajectories(_TRAJ_DIR)
    assert traces  # the fixtures import
    paths = WorkspacePaths(ws)
    episodes = mining.mine_episodes(paths, epoch_id, imported_traces=traces)

    ids = {e.episode_id for e in episodes}
    types = {e.episode_type for e in episodes}
    # BOTH sources present in the ranked union.
    assert "ep-workspace1" in ids  # the workspace episode
    assert "imported_signal" in types  # the foreign episodes
    # The high-severity imported episodes (sev 5) outrank the low workspace one.
    assert episodes[0].severity_rank >= sentinel.severity_rank


def test_plan_mode_spends_nothing_with_the_new_flag(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, _epoch_id = workspace
    spy = _SynthSpy([_foreign_suggestion()])
    admit = _AdmitSpy()
    monkeypatch.setattr(sug_mod, "resolve_synthesize", lambda: spy)
    monkeypatch.setattr(sug_mod, "resolve_admit", lambda: admit)

    result = _run(
        [
            "reflect",
            "suggest",
            "--workspace",
            str(ws),
            "--reflection",
            _REFLECTION_ID,
            "--from-trajectories",
            str(_TRAJ_GF_FREE),
        ]
    )
    assert result.exit_code == 0, result.output
    # Default (no --probe): the admission seam — the only live-spend surface — is
    # NEVER consulted, even on the bootstrap path.
    assert admit.calls == 0
    assert "plan mode" in result.stderr


def test_report_renders_foreign_source_provenance(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, _epoch_id = workspace
    spy = _SynthSpy([_foreign_suggestion()])
    monkeypatch.setattr(sug_mod, "resolve_synthesize", lambda: spy)
    monkeypatch.setattr(sug_mod, "resolve_admit", lambda: None)
    _run(
        [
            "reflect",
            "suggest",
            "--workspace",
            str(ws),
            "--reflection",
            _REFLECTION_ID,
            "--from-trajectories",
            str(_TRAJ_DIR),
        ]
    )

    result = _run(["reflect", "report", _REFLECTION_ID, "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "foreign source: adk_run.jsonl (adk_events)" in result.output
