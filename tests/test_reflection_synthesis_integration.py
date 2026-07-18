"""The un-mocked eval-synthesis round-trip (EVAL-SYNTHESIS.md §3/§5/§6).

The seam adapters (blockers 1-5) can never regress silently again: these tests
drive ``reflect suggest`` → ``reflect apply`` over a seeded, real-shaped
workspace with **NO monkeypatching of the resolvers** — real mining
(``mine_episodes``) → real mechanical synthesis (``synthesize``) → persisted
``suggestions.json`` with a non-empty ``draft_artifact`` + ``proposed_op`` →
real ``apply`` carrying the drafted entry into a builder draft. The only mock is
at the runner layer for the ``--probe`` spend (the admission tests' precedent),
so every admission statistic is a known answer with zero live budget.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from click.testing import CliRunner

import zicato.tournament.runner as runner_mod
from zicato.board.jsonl import save_board
from zicato.cli.discovery import build_cli_root
from zicato.core import BoardEntry, Generation, LossProfile, ScoringWeights
from zicato.core.board import Expectation, ExpectationKind
from zicato.core.workspace import board_path, generation_dir, reflection_suggestions_path
from zicato.epoch.lifecycle import new_epoch
from zicato.index.schema import apply_schema
from zicato.tournament.unit_cache import _unit_loss_path

_REFLECTION_ID = "refl-integration"


def _write_loss(ws: Path, epoch: str, gen: str, entry: str, *, passes: bool | None) -> None:
    from zicato.telemetry import reducer  # noqa: PLC0415

    loss = LossProfile(
        run_id=f"{gen}:{entry}",
        entry_id=entry,
        generation_id=gen,
        epoch_id=epoch,
        drift_counts=(),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=0.0 if passes else 0.5,
        pass_fail=passes,
    )
    reducer.write_loss_profile(loss, _unit_loss_path(ws, epoch, gen, entry, 0))


def _seed_index(ws: Path, epoch: str) -> None:
    conn = sqlite3.connect(str(ws / "index.db"))
    try:
        apply_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO epochs(epoch_id, contract_hash, created_at, closed) "
            "VALUES(?,?,?,?)",
            (epoch, "h", "2026-07-01", 0),
        )
        conn.execute(
            "INSERT OR REPLACE INTO generations(epoch_id, generation_id, parent_generation_id, "
            "promoted, created_at, round_index, elo, elo_se, elo_games) VALUES(?,?,?,?,?,?,?,?,?)",
            (epoch, "g0", None, 1, "2026-07-01", 0, 1500.0, 40.0, 4),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_workspace(tmp_path: Path) -> tuple[Path, str]:
    """A real-shaped workspace whose lineage recorded a predicate miss on ``login``."""
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    (ws / "config.json").write_text(json.dumps({"runtime": {}, "adapter": {}}), encoding="utf-8")
    login = BoardEntry(
        id="login",
        kind="single_turn",
        wall_clock_budget_seconds=30,
        input="log the user in",
        expectation=Expectation(kind=ExpectationKind.EXPECTED_TEXT, spec="welcome"),
    )
    pay = BoardEntry(
        id="pay",
        kind="single_turn",
        wall_clock_budget_seconds=30,
        input="take a payment",
        expectation=Expectation(kind=ExpectationKind.EXPECTED_TEXT, spec="ok"),
    )
    board_file = tmp_path / "board.jsonl"
    save_board([login, pay], board_file)
    cfg = new_epoch(ws, "integ", board_file, "steer", ScoringWeights())
    epoch = cfg.id

    # A generation g0 that FAILED login (predicate miss) and passed pay.
    snap = generation_dir(ws, epoch, "g0") / "snapshot"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "entrypoint.py").write_text("# stub\n", encoding="utf-8")
    _write_loss(ws, epoch, "g0", "login", passes=False)
    _write_loss(ws, epoch, "g0", "pay", passes=True)
    _seed_index(ws, epoch)
    return ws, epoch


def _run(args: list[str]) -> object:
    return CliRunner(mix_stderr=False).invoke(build_cli_root(), args)


def test_unmocked_round_trip_suggest_persists_and_applies(tmp_path: Path) -> None:
    ws, epoch = _seed_workspace(tmp_path)

    # NO monkeypatch of resolve_synthesize / resolve_admit — the real seams run.
    result = _run(["reflect", "suggest", "--workspace", str(ws), "--reflection", _REFLECTION_ID])
    assert result.exit_code == 0, result.output

    persisted = json.loads(
        reflection_suggestions_path(ws, epoch, _REFLECTION_ID).read_text(encoding="utf-8")
    )
    sugs = persisted["suggestions"]
    # Real mining → real mechanical synthesis produced a regression entry pinning
    # the login predicate miss, with a non-empty draft_artifact + proposed_op.
    regressions = [s for s in sugs if s["suggestion_type"] == "regression_entry"]
    assert (
        regressions
    ), f"expected a regression suggestion, got {[s['suggestion_type'] for s in sugs]}"
    reg = regressions[0]
    assert reg["subject"] == "login"
    assert reg["draft_artifact"]  # non-empty
    assert reg["draft_artifact"]["wall_clock_budget_seconds"] == 30  # canonical key, op-ready
    assert reg["proposed_op"]["op"] == "add_board_entry"
    assert reg["proposed_op"]["args"]["entry"]["id"].startswith("login__regression")
    assert reg["admission"] is None  # plan mode spent nothing
    # Provenance carried the synthesiser tier + the motivating episode's keys.
    assert reg["provenance"]["synthesizer"] == "mechanical"
    assert reg["severity_rank"] > 0

    # Real apply carries the drafted entry into a builder draft — the sealed
    # contract stays byte-unchanged.
    before = board_path(ws, epoch).read_bytes()
    applied = _run(
        ["reflect", "apply", _REFLECTION_ID, reg["suggestion_id"], "--workspace", str(ws)]
    )
    assert applied.exit_code == 0, applied.output
    assert "add_board_entry" in applied.output
    assert board_path(ws, epoch).read_bytes() == before

    from zicato.reflection.apply import apply_suggestion_to_draft  # noqa: PLC0415

    result_apply = apply_suggestion_to_draft(
        workspace_root=ws,
        epoch_id=epoch,
        reflection_id=_REFLECTION_ID,
        suggestion_id=reg["suggestion_id"],
    )
    assert result_apply.op == "add_board_entry"
    assert result_apply.patch["changed"]["entry_id"].startswith("login__regression")
    assert "board" in result_apply.diff["changed_components"]


def test_unmocked_probe_measures_against_the_fixture_runner(tmp_path: Path, monkeypatch) -> None:
    ws, epoch = _seed_workspace(tmp_path)
    # An import-kind adapter so make_adapter_from_config resolves; the runner is
    # mocked, so the adapter object is never dereferenced (the admission-test tier).
    (ws / "config.json").write_text(
        json.dumps({"runtime": {}, "adapter": {"kind": "import", "factory": "builtins:object"}}),
        encoding="utf-8",
    )

    class _Runner:
        def __init__(self) -> None:
            self.slots: list[int] = []

        async def __call__(
            self,
            *,
            adapter: object,
            generation: Generation,
            entry: BoardEntry,
            weights: object,
            config: object,
            workspace_root: Path,
            epoch_id: str,
            side: str,
            match_id: str = "",
        ) -> LossProfile:
            ri = int(entry.context.get("replicate_index", "0"))
            self.slots.append(ri)
            return LossProfile(
                run_id=f"{generation.id}:{entry.id}:r{ri}",
                entry_id=entry.id,
                generation_id=generation.id,
                epoch_id=epoch_id,
                drift_counts=(),
                plan_revisions=0,
                task_failure_ratio=0.0,
                runtime_ms=1,
                wall_clock_budget_exceeded=False,
                expectation_result=None,
                drift_loss=0.0,
                pass_fail=True,
            )

    stub = _Runner()
    monkeypatch.setattr(runner_mod, "_run_single", stub)

    result = _run(
        ["reflect", "suggest", "--workspace", str(ws), "--reflection", _REFLECTION_ID, "--probe"]
    )
    assert result.exit_code == 0, result.output

    persisted = json.loads(
        reflection_suggestions_path(ws, epoch, _REFLECTION_ID).read_text(encoding="utf-8")
    )
    regressions = [
        s for s in persisted["suggestions"] if s["suggestion_type"] == "regression_entry"
    ]
    assert regressions
    admission = regressions[0]["admission"]
    assert admission is not None  # the probe stamped a record
    assert admission["executed"] is True
    assert admission["noise"]["measured"] is True
    assert admission["noise"]["base"] == 6000  # measured at the reserved base
    # Every synthesis draw landed at/above the reserved base — r0 untouched.
    assert stub.slots and all(ri >= 6000 for ri in stub.slots)


def test_admit_seam_plan_mode_runs_nothing(tmp_path: Path, monkeypatch) -> None:
    # The admit() seam in plan mode (probe=False) must consult the runner ZERO
    # times — plan-mode estimates only, no live champion spend.
    from zicato.reflection.admission import admit
    from zicato.reflection.suggestions import Suggestion

    ws, epoch = _seed_workspace(tmp_path)

    calls = {"n": 0}

    async def _forbidden(**_kw: object) -> LossProfile:  # pragma: no cover — must not run
        calls["n"] += 1
        raise AssertionError("plan mode must not spend a board run")

    monkeypatch.setattr(runner_mod, "_run_single", _forbidden)

    sug = Suggestion(
        suggestion_id="sug-plan01",
        suggestion_type="regression_entry",
        artifact_kind="board_entry",
        subject="login",
        summary="pin",
        rationale="login failed",
        target_slice="train",
        draft_artifact={
            "id": "login_reg",
            "kind": "single_turn",
            "wall_clock_budget_seconds": 30,
            "input": "x",
        },
        proposed_op={"op": "add_board_entry", "args": {"entry": {}}},
        provenance={"source_episodes": []},
    )
    out = admit([sug], probe=False, workspace_root=ws, epoch_id=epoch)
    assert calls["n"] == 0  # nothing ran
    assert len(out) == 1
    adm = out[0].admission
    assert adm is not None
    assert adm["executed"] is False  # unmeasured — no probe spent
    assert adm["noise"]["measured"] is False
