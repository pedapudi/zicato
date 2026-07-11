"""``zicato reflect`` CLI — discovery, run / report / apply over a fixture.

Exercises the surfaces end to end with the R3 scripted-double adjudicators
(zero live endpoints, G3): the group appears in ``--help``; ``--pre-register``
stops before any run; active adjudication without a callable REFUSES with the
live-run-gate message; an active run with a scripted adjudicator persists the
corpus / scorecards / findings / summary and projects an index row; ``--passive``
spends zero LLM (a counting callable is never called); ``report`` renders; and
``apply`` forks a builder draft carrying the finding's op while leaving the
sealed contract files byte-unchanged.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner
from goldfive import DriftSeverity

from zicato.board.jsonl import save_board
from zicato.cli.discovery import build_cli_root
from zicato.core.types import BoardEntry, JudgeMode, JudgeSpec, ScoringWeights
from zicato.core.workspace import (
    reflection_findings_path,
    reflection_plan_path,
    reflection_scorecards_path,
)
from zicato.epoch.lifecycle import new_epoch, set_epoch_noise_floor
from zicato.judge_runtime.io_capture import JudgeIOFileSink, judge_io_path_for_loss
from zicato.testing.adjudicators import AlwaysConfirm
from zicato.tournament.unit_cache import _unit_loss_path

CANDIDATES = ["g0", "g1"]


class _Counter:
    """A call_llm double that counts invocations (zero-LLM assertion)."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.calls += 1
        return "{}"


@pytest.fixture
def doubles_module() -> str:
    """Register an in-process module of adjudicator doubles; return its name."""
    name = "_refl_cli_doubles"
    mod = types.ModuleType(name)
    mod.confirm = AlwaysConfirm()
    mod.counter = _Counter()
    sys.modules[name] = mod
    try:
        yield name
    finally:
        sys.modules.pop(name, None)


def _write_run(workspace: Path, epoch_id: str, gen: str, *, fired: bool) -> None:
    from zicato.core import DriftCount, JudgeLoss, LossProfile
    from zicato.telemetry import reducer

    drift = 1.0 if fired else 0.0
    loss = LossProfile(
        run_id=f"run-{gen}-entryA",
        entry_id="entryA",
        generation_id=gen,
        epoch_id=epoch_id,
        drift_counts=((DriftCount(kind="custom:j", severity="warning", count=1),) if fired else ()),
        plan_revisions=0,
        task_failure_ratio=0.0,
        runtime_ms=10,
        wall_clock_budget_exceeded=False,
        expectation_result=None,
        drift_loss=drift,
        pass_fail=not fired,
        per_judge_loss=(JudgeLoss("j", raw_loss=drift, weight=1.0, weighted_loss=drift),),
    )
    loss_path = _unit_loss_path(workspace, epoch_id, gen, "entryA", 0)
    reducer.write_loss_profile(loss, loss_path)
    sink = JudgeIOFileSink(judge_io_path_for_loss(loss_path))
    sink.record(
        "j",
        reasoning_text=f"transcript for {gen} — the assistant answered",
        transcript_window=(f"transcript for {gen}",),
        raw_response="{}",
        drift_emitted=fired,
        kind="custom:j",
        severity="warning" if fired else "info",
        detail="missing citation" if fired else "",
    )


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, str]:
    """A workspace with one epoch, one judged entry, two candidate runs."""
    ws = tmp_path / ".zicato"
    ws.mkdir(parents=True)
    (ws / "config.json").write_text(json.dumps({"runtime": {}, "adapter": {}}), encoding="utf-8")

    entry = BoardEntry(
        id="entryA",
        kind="single_turn",
        wall_clock_budget_seconds=30,
        input="hi",
        judges=(
            JudgeSpec(
                name="j",
                mode=JudgeMode.INLINE,
                body="checks that claims are cited",
                severity=DriftSeverity.WARNING,
            ),
        ),
    )
    board_file = tmp_path / "board.jsonl"
    save_board([entry], board_file)

    cfg = new_epoch(
        ws,
        "refltest",
        board_file,
        "propose concrete deltas",
        ScoringWeights(promote_margin=0.01),
    )
    epoch_id = cfg.id
    # A noise floor above the promote margin so a margin-below-floor finding
    # (which carries a set_gate proposed_op) is emitted deterministically.
    set_epoch_noise_floor(
        ws,
        epoch_id,
        {
            "generation_id": "g0",
            "epoch_id": epoch_id,
            "runs": 5,
            "scalars": [1.0, 1.0],
            "max_abs_delta": 0.5,
            "delta_std": 0.1,
            "measured_at": "2026-07-01",
        },
    )
    _write_run(ws, epoch_id, "g0", fired=True)
    _write_run(ws, epoch_id, "g1", fired=False)
    return ws, epoch_id


def _run(args: list[str]) -> object:
    return CliRunner().invoke(build_cli_root(), args)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_reflect_in_advanced_help() -> None:
    result = _run(["--help"])
    assert result.exit_code == 0
    assert "reflect" in result.output


# ---------------------------------------------------------------------------
# pre-register stops
# ---------------------------------------------------------------------------


def test_pre_register_writes_plan_and_stops(workspace: tuple[Path, str]) -> None:
    ws, epoch_id = workspace
    result = _run(
        [
            "reflect",
            "run",
            "--workspace",
            str(ws),
            "--epoch",
            epoch_id,
            "--candidate",
            "g0",
            "--candidate",
            "g1",
            "--pre-register",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "pre-registered plan" in result.output
    # A plan exists; NO scorecards / findings were produced (it stopped).
    refl_ids = [p.name for p in (ws / "epochs" / epoch_id / "reflections").iterdir()]
    assert len(refl_ids) == 1
    rid = refl_ids[0]
    assert reflection_plan_path(ws, epoch_id, rid).exists()
    assert not reflection_findings_path(ws, epoch_id, rid).exists()


# ---------------------------------------------------------------------------
# G3 — active adjudication without a callable REFUSES
# ---------------------------------------------------------------------------


def test_active_without_adjudicator_refuses(workspace: tuple[Path, str]) -> None:
    ws, epoch_id = workspace
    result = _run(
        [
            "reflect",
            "run",
            "--workspace",
            str(ws),
            "--epoch",
            epoch_id,
            "--candidate",
            "g0",
            "--candidate",
            "g1",
        ]
    )
    assert result.exit_code != 0
    assert "live-run gate" in result.output or "ACTIVE mode" in result.output
    # Nothing was persisted beyond the (up-front) plan — no findings spent.
    refl_root = ws / "epochs" / epoch_id / "reflections"
    if refl_root.exists():
        for rid in refl_root.iterdir():
            assert not reflection_findings_path(ws, epoch_id, rid.name).exists()


# ---------------------------------------------------------------------------
# active run with a scripted adjudicator
# ---------------------------------------------------------------------------


def test_active_run_with_scripted_adjudicator(
    workspace: tuple[Path, str], doubles_module: str
) -> None:
    ws, epoch_id = workspace
    result = _run(
        [
            "reflect",
            "run",
            "--workspace",
            str(ws),
            "--epoch",
            epoch_id,
            "--candidate",
            "g0",
            "--candidate",
            "g1",
            "--entries",
            "entryA",
            "--adjudicator-call-llm",
            f"{doubles_module}:confirm",
        ]
    )
    assert result.exit_code == 0, result.output
    refl_root = ws / "epochs" / epoch_id / "reflections"
    rid = next(iter(refl_root.iterdir())).name

    scorecards = json.loads(reflection_scorecards_path(ws, epoch_id, rid).read_text())
    assert any(c["judge_name"] == "j" for c in scorecards["scorecards"])
    findings = json.loads(reflection_findings_path(ws, epoch_id, rid).read_text())
    # The margin-below-floor finding carries a set_gate proposed_op. (Some
    # findings are recommendation-only with proposed_op=None — e.g. the blind
    # adjudicator's FN on g1's clean run — so tolerate a null op.)
    margin = [
        f for f in findings["findings"] if (f.get("proposed_op") or {}).get("op") == "set_gate"
    ]
    assert margin, findings
    # The reflection projected an index row.
    from zicato.index import query as iq

    row = iq.reflection_row(ws / "index.db", rid)
    assert row is not None
    assert row["n_judges"] >= 1


# ---------------------------------------------------------------------------
# passive run spends zero LLM
# ---------------------------------------------------------------------------


def test_passive_run_is_zero_llm(workspace: tuple[Path, str], doubles_module: str) -> None:
    ws, epoch_id = workspace
    counter = sys.modules[doubles_module].counter
    result = _run(
        [
            "reflect",
            "run",
            "--workspace",
            str(ws),
            "--epoch",
            epoch_id,
            "--candidate",
            "g0",
            "--candidate",
            "g1",
            "--passive",
            "--adjudicator-call-llm",
            f"{doubles_module}:counter",
        ]
    )
    assert result.exit_code == 0, result.output
    # --passive never adjudicates: the counting callable is never invoked.
    assert counter.calls == 0
    # A corpus + summary were still produced (the cheap tier ran).
    refl_root = ws / "epochs" / epoch_id / "reflections"
    rid = next(iter(refl_root.iterdir())).name
    assert (ws / "epochs" / epoch_id / "reflections" / rid / "corpus.jsonl").exists()
    assert (ws / "epochs" / epoch_id / "reflections" / rid / "summary.json").exists()


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_renders(workspace: tuple[Path, str], doubles_module: str) -> None:
    ws, epoch_id = workspace
    _run(
        [
            "reflect",
            "run",
            "--workspace",
            str(ws),
            "--epoch",
            epoch_id,
            "--candidate",
            "g0",
            "--candidate",
            "g1",
            "--adjudicator-call-llm",
            f"{doubles_module}:confirm",
        ]
    )
    rid = next(iter((ws / "epochs" / epoch_id / "reflections").iterdir())).name
    result = _run(["reflect", "report", rid, "--workspace", str(ws)])
    assert result.exit_code == 0, result.output
    assert "Reflection report" in result.output
    assert "Findings" in result.output

    js = _run(["reflect", "report", rid, "--workspace", str(ws), "--json"])
    assert js.exit_code == 0
    payload = json.loads(js.output)
    assert "summary" in payload and "findings" in payload


# ---------------------------------------------------------------------------
# apply — forks a draft carrying the op; the sealed contract is byte-unchanged
# ---------------------------------------------------------------------------


def _contract_bytes(ws: Path, epoch_id: str) -> dict[str, bytes]:
    edir = ws / "epochs" / epoch_id
    out: dict[str, bytes] = {}
    for name in ("board.jsonl", "scoring.json", "brief.md"):
        p = edir / name
        if p.exists():
            out[name] = p.read_bytes()
    return out


def test_apply_forks_draft_and_leaves_contract_unchanged(
    workspace: tuple[Path, str], doubles_module: str
) -> None:
    ws, epoch_id = workspace
    _run(
        [
            "reflect",
            "run",
            "--workspace",
            str(ws),
            "--epoch",
            epoch_id,
            "--candidate",
            "g0",
            "--candidate",
            "g1",
            "--adjudicator-call-llm",
            f"{doubles_module}:confirm",
        ]
    )
    rid = next(iter((ws / "epochs" / epoch_id / "reflections").iterdir())).name
    findings = json.loads(reflection_findings_path(ws, epoch_id, rid).read_text())["findings"]
    margin = next(f for f in findings if f.get("proposed_op", {}).get("op") == "set_gate")
    finding_id = margin["finding_id"]

    before = _contract_bytes(ws, epoch_id)
    result = _run(
        ["reflect", "apply", rid, finding_id, "--workspace", str(ws), "--epoch", epoch_id]
    )
    assert result.exit_code == 0, result.output
    assert "builder draft slot" in result.output
    assert "set_gate" in result.output
    # The sealed contract files are BYTE-unchanged (apply never writes them).
    assert _contract_bytes(ws, epoch_id) == before

    # And the forked draft actually carries the op: promote_margin moved to the
    # finding's recommended value.
    from zicato.reflection.apply import apply_finding_to_draft

    applied = apply_finding_to_draft(
        workspace_root=ws,
        epoch_id=epoch_id,
        reflection_id=rid,
        finding_id=finding_id,
    )
    assert applied.op == "set_gate"
    recommended = margin["proposed_op"]["args"]["promote_margin"]
    changed = applied.patch["changed"]
    assert "promote_margin" in changed
    assert changed["promote_margin"]["to"] == recommended
    # The sealed contract is STILL byte-unchanged after the direct apply too.
    assert _contract_bytes(ws, epoch_id) == before


def test_apply_non_actionable_finding_errors(workspace: tuple[Path, str]) -> None:
    ws, epoch_id = workspace
    # Hand-write a findings.json whose only finding has no proposed_op.
    rid = "refl-manual-0001"
    fp = reflection_findings_path(ws, epoch_id, rid)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(
        json.dumps(
            {
                "reflection_id": rid,
                "findings": [{"finding_id": "f-untested", "proposed_op": None}],
            }
        ),
        encoding="utf-8",
    )
    result = _run(
        ["reflect", "apply", rid, "f-untested", "--workspace", str(ws), "--epoch", epoch_id]
    )
    assert result.exit_code != 0
    assert "no proposed_op" in result.output or "recommendation only" in result.output


# ---------------------------------------------------------------------------
# practice review — persisted by run (free), plus the cheap `practices` tier
# ---------------------------------------------------------------------------


def test_passive_run_persists_practices(workspace: tuple[Path, str], doubles_module: str) -> None:
    ws, epoch_id = workspace
    result = _run(
        [
            "reflect",
            "run",
            "--workspace",
            str(ws),
            "--epoch",
            epoch_id,
            "--candidate",
            "g0",
            "--candidate",
            "g1",
            "--passive",
            "--adjudicator-call-llm",
            f"{doubles_module}:counter",
        ]
    )
    assert result.exit_code == 0, result.output
    refl_root = ws / "epochs" / epoch_id / "reflections"
    rid = next(iter(refl_root.iterdir())).name
    practices = json.loads((refl_root / rid / "practices.json").read_text())
    assert "checks" in practices and "verdict_counts" in practices
    assert len(practices["checks"]) == 11
    # The rendered report carries a Practice review section.
    assert "Practice review" in result.output


def test_reflect_practices_subcommand_no_corpus(workspace: tuple[Path, str]) -> None:
    ws, epoch_id = workspace
    result = _run(["reflect", "practices", "--workspace", str(ws), "--epoch", epoch_id, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["checks"]) == 11
    # No corpus / scorecards ⇒ the corpus-dependent checks report unmeasured honestly.
    by_id = {c["check_id"]: c for c in payload["checks"]}
    assert by_id["loss_monoculture"]["verdict"] == "unmeasured"
    assert by_id["weight_revisit"]["verdict"] == "unmeasured"
    # The measured noise floor (0.5 in the fixture) powers the statistical checks.
    assert by_id["statistical_power"]["verdict"] != "unmeasured"


def test_reflect_practices_in_group_help() -> None:
    result = _run(["reflect", "--help"])
    assert result.exit_code == 0
    assert "practices" in result.output
