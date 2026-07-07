"""Process-exemplar channel e2e on the target_0 convergence contract.

The known-answer harness (``zicato_examples.target_0_convergence``) runs
real subprocess workers whose deterministic policy adapter emits REAL
goldfive lifecycle frames (``run_started``, one ``drift_detected`` per
remaining planted defect token, ``run_completed``) through the worker's
JSONL sink — so a contract that opts into
``proposer_quality.process_exemplars`` must, from round 2 onward (once
the champion has telemetry), splice a redacted ``## Process exemplars``
section into the proposer's user prompt, anchored on the planted
``unexpected_output`` drift.

Asserted here, per ``docs/design/PROCESS-EXEMPLARS.md``:

* round 1 (no parent telemetry) renders NO exemplar section — the
  best-effort extraction degrades to the empty-string sentinel;
* round 2 renders the section, positioned after the failure-mode
  profile, with the anchor drift's closed-vocabulary fields visible;
* REDACTION holds on real worker-written events: no board entry id, no
  board input text (the task prompt rides ``run_started.goal_summary``,
  an unlisted case), and no run id reaches the prompt.

The prompt is captured by wrapping the single-shot engine's
``render_user_prompt`` seam (the target_0 proposer dir is skill-composed,
so it drives :func:`zicato.proposer.proposer.propose_experiment`
in-process); the subprocess workers are untouched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import zicato_examples.target_0_convergence as _t0_pkg
from zicato.epoch.lifecycle import _scoring_from_dict, new_epoch
from zicato_examples.target_0_convergence import mocks as t0_mocks

EXAMPLE_DIR = Path(_t0_pkg.__file__).resolve().parent
AGENT_DIR = EXAMPLE_DIR / "agent"
BOARD_PATH = EXAMPLE_DIR / "board.jsonl"
SCORING_PATH = EXAMPLE_DIR / "scoring.json"

ADAPTER_BLOCK = {
    "kind": "import",
    "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
}

#: The five board-entry ids — none may appear in any proposer prompt section
#: the exemplar channel added.
_BOARD_ENTRY_IDS = (
    "conv_body",
    "conv_summary",
    "conv_citations",
    "conv_concise",
    "conv_no_fabrication",
)


def _board_input_texts() -> list[str]:
    """Every board entry's task-prompt text — must never reach the prompt
    through the exemplar channel (it rides ``run_started.goal_summary``)."""
    texts = []
    for line in BOARD_PATH.read_text().splitlines():
        if line.strip():
            texts.append(json.loads(line)["input"])
    return texts


def _bootstrap(tmp_path: Path) -> tuple[Path, str]:
    """A target_0 workspace whose contract opts into process exemplars."""
    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    (workspace / "config.json").write_text(
        json.dumps(
            {
                "instance_id": "default",
                "created_at": "2026-07-01T00:00:00Z",
                "adapter": ADAPTER_BLOCK,
                "mutable_trees": [str(AGENT_DIR)],
            }
        )
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Exemplar e2e brief\n- Remove defect tokens, one per round.\n")

    scoring = json.loads(SCORING_PATH.read_text())
    # The shipped deterministic pins (single-sample proposer, single-run
    # duel) plus the ONE knob under test: the opt-in exemplar channel.
    scoring["proposer_quality"] = {"best_of_n": 1, "process_exemplars": 2}
    weights = _scoring_from_dict(scoring)
    cfg = new_epoch(
        workspace,
        name="t0-process-exemplars",
        board_source=BOARD_PATH,
        brief_source=brief,
        weights=weights,
        auto_close_previous=False,
        proposer_path=EXAMPLE_DIR / "proposer",
    )
    return workspace, cfg.id


def test_exemplar_block_renders_redacted_from_round_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zicato.proposer.proposer as proposer_mod
    from zicato.evolve.loop import evolve_n_rounds
    from zicato.proposer.prompts import render_user_prompt as real_render

    captured: list[str] = []

    def _recording_render(**kwargs: object) -> str:
        rendered = real_render(**kwargs)  # type: ignore[arg-type]
        captured.append(rendered)
        return rendered

    # The single-shot engine (the target_0 proposer dir is skill-composed)
    # renders its user prompt through this module-level seam, in-process.
    monkeypatch.setattr(proposer_mod, "render_user_prompt", _recording_render)

    workspace, epoch_id = _bootstrap(tmp_path)
    t0_mocks.reset()
    outcomes = asyncio.run(
        evolve_n_rounds(
            rounds=2,
            workspace_root=workspace,
            epoch_id=epoch_id,
            harness_call_llm=t0_mocks.harness_llm,
            auxiliary_call_llm=t0_mocks.aux_llm,
            auto_epoch=False,
            fast_mode=True,
        )
    )
    assert len(outcomes) == 2
    # The known-answer script: round 1 promotes, round 2 is the negative
    # control (rejected) — the exemplar channel must not disturb either.
    assert outcomes[0].tournament_decision == "promoted"
    assert outcomes[1].tournament_decision == "rejected"

    # One captured prompt per round (single-sample proposer, no retries).
    assert len(captured) == 2
    round_1, round_2 = captured

    # Round 1: the champion (v0) has no telemetry yet — no patterns, no
    # exemplars; the best-effort extraction leaves the prompt untouched.
    assert "## Process exemplars" not in round_1

    # Round 2: the champion (v1, promoted in round 1) carries real
    # worker-written events; the planted `unexpected_output` drift fires
    # the frequency pattern and the exemplar window renders.
    section_at = round_2.find("## Process exemplars (train slice — redacted event windows)")
    assert section_at != -1, "round 2 rendered no exemplar section"
    assert "Redaction contract (PROCESS-EXEMPLARS.md)" in round_2
    # Positioned directly after the failure-mode profile block.
    profile_at = round_2.find("## Failure-mode profile")
    assert profile_at != -1 and profile_at < section_at

    # The anchor drift is visible with its closed-vocabulary fields, and
    # the deterministic detail (a harness-side token, not board identity)
    # survives redaction.
    assert "drift_detected kind=unexpected_output severity=info" in round_2
    assert "planted defect token" in round_2

    # REDACTION on real events: no entry id, no board input text, and no
    # run id reaches the exemplar section (or anywhere the channel added).
    section = round_2[section_at:]
    section_end = section.find("\n## ")
    exemplar_block = section[:section_end] if section_end != -1 else section
    for entry_id in _BOARD_ENTRY_IDS:
        assert entry_id not in exemplar_block, f"entry id {entry_id!r} leaked"
    for input_text in _board_input_texts():
        assert input_text not in round_2, f"board input {input_text!r} leaked"
    assert "conv-" not in exemplar_block, "a run id leaked into the exemplar block"

    # Determinism (the §2 refresh semantics): the same champion + pattern
    # set re-extracts byte-identically — verified against a direct
    # re-extraction of the round-2 inputs.
    from zicato.analyzer.process_exemplars import extract_process_exemplars
    from zicato.patterns.detectors import ALL_DETECTORS, DetectorInput, detect_patterns
    from zicato.proposer.prompts import render_process_exemplars
    from zicato.telemetry.reducer import read_loss_profile

    runs_dir = workspace / "epochs" / epoch_id / "generations" / "v1" / "runs"
    losses = [read_loss_profile(runs_dir / e / "loss.json") for e in _BOARD_ENTRY_IDS]
    from zicato.board.jsonl import load_board

    board = load_board(workspace / "epochs" / epoch_id / "board.jsonl")
    patterns = detect_patterns(
        DetectorInput(
            losses=losses,
            entries={e.id: e for e in board},
            events_paths={e: runs_dir / e / "events.jsonl" for e in _BOARD_ENTRY_IDS},
        ),
        detectors=ALL_DETECTORS,
    )
    exemplars = extract_process_exemplars(
        workspace,
        epoch_id,
        patterns,
        2,
        parent_generation_id="v1",
        train_entry_ids=list(_BOARD_ENTRY_IDS),
    )
    assert exemplars, "direct re-extraction found no exemplars"
    assert render_process_exemplars(exemplars) in round_2
