"""Tests for the decision-telemetry analyzer entry point.

The tests stand up a tiny workspace tree with synthetic ``events.jsonl``
files and exercise:

* ``analyze_epoch_telemetry`` happy path → markdown written.
* Empty epoch (no events) → fallback markdown written, no LLM call.
* ``load_latest_insights`` concatenation across multiple round files.
* Timeout enforcement when the auxiliary callable hangs.
* An aux callable that raises → fallback body cites the exception.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from zicato.analyzer import analyze_epoch_telemetry, load_latest_insights


def _envelope(seq: int, payload_key: str, payload: dict) -> dict:
    return {
        "event_id": f"evt_{seq}",
        "run_id": "run_test",
        "sequence": seq,
        "emitted_at": {"seconds": 1_700_000_000 + seq, "nanos": 0},
        "session_id": "sess_test",
        payload_key: payload,
    }


def _make_epoch_tree(workspace: Path, epoch_id: str) -> None:
    """Set up the directory layout the analyzer walks."""

    (workspace / "epochs" / epoch_id / "generations").mkdir(parents=True, exist_ok=True)


def _write_events(
    workspace: Path,
    epoch_id: str,
    generation: str,
    entry: str,
    events: list,
) -> Path:
    run_dir = workspace / "epochs" / epoch_id / "generations" / generation / "runs" / entry
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "events.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return path


def test_analyze_epoch_telemetry_writes_markdown(tmp_path: Path) -> None:
    """Canned aux callable → analyzer writes the LLM body to insights/round_X.md."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_test"
    _make_epoch_tree(workspace, epoch_id)
    _write_events(
        workspace,
        epoch_id,
        "v0",
        "e1",
        [
            _envelope(
                0,
                "ladder_transition_decided",
                {
                    "from_level": "observe",
                    "to_level": "nudge",
                    "reason": "first occurrence",
                    "drift_kind": "DRIFT_KIND_OFF_TOPIC",
                    "drift_id": "d1",
                    "severity": "DRIFT_SEVERITY_WARNING",
                },
            ),
        ],
    )

    captured: dict[str, str] = {}

    async def fake_aux(system: str, user: str, model: str) -> str:
        captured["system"] = system
        captured["user"] = user
        captured["model"] = model
        return "## Headline observations\n- saw 1 ladder transition\n"

    out = asyncio.run(
        analyze_epoch_telemetry(workspace, epoch_id, fake_aux, model="opaque-1", round_n=3)
    )

    assert out.exists()
    # round_n=3 → zero-padded to width 4 in the filename.
    assert out.name == "round_0003.md"
    body = out.read_text(encoding="utf-8")
    assert "Headline observations" in body
    assert "saw 1 ladder transition" in body
    # The LLM was given the system + user prompts.
    assert "decision telemetry" in captured["system"].lower()
    assert "observe->nudge" in captured["user"]
    assert captured["model"] == "opaque-1"


def test_analyze_epoch_telemetry_grounds_prompt_in_mutation_ids(tmp_path: Path) -> None:
    """A5: the enumerated mutation ids are rendered into the insight prompt.

    The insight prompt previously told the LLM to "reference the
    optimization manifest's mutation ids" without ever giving it the
    real ids, so the LLM hallucinated targets. The fix threads the
    agent's real enumerated mutation surface into the user prompt and
    the system prompt forbids inventing an id.
    """
    workspace = tmp_path / ".zicato"
    epoch_id = "ep_test"
    _make_epoch_tree(workspace, epoch_id)
    _write_events(
        workspace,
        epoch_id,
        "v0",
        "e1",
        [
            _envelope(
                0,
                "ladder_transition_decided",
                {
                    "from_level": "observe",
                    "to_level": "nudge",
                    "reason": "first occurrence",
                    "drift_kind": "DRIFT_KIND_OFF_TOPIC",
                    "drift_id": "d1",
                    "severity": "DRIFT_SEVERITY_WARNING",
                },
            ),
        ],
    )

    captured: dict[str, str] = {}

    async def fake_aux(system: str, user: str, model: str) -> str:
        captured["system"] = system
        captured["user"] = user
        return "## Headline observations\n- ok\n"

    mutation_ids = ["mut_prompt_a1b2", "mut_threshold_c3d4"]
    asyncio.run(
        analyze_epoch_telemetry(
            workspace,
            epoch_id,
            fake_aux,
            round_n=1,
            mutation_ids=mutation_ids,
        )
    )

    # The real ids appear VERBATIM in the user prompt.
    for mid in mutation_ids:
        assert mid in captured["user"]
    # The user prompt has the dedicated grounding section.
    assert "Available mutation targets" in captured["user"]
    # The system prompt forbids inventing ids.
    assert "verbatim" in captured["system"].lower()
    assert "do not invent" in captured["system"].lower()


def test_analyze_epoch_telemetry_marks_absent_mutation_surface(tmp_path: Path) -> None:
    """Without enumerated ids, the prompt says so rather than leaving a blank."""
    workspace = tmp_path / ".zicato"
    epoch_id = "ep_test"
    _make_epoch_tree(workspace, epoch_id)
    _write_events(
        workspace,
        epoch_id,
        "v0",
        "e1",
        [
            _envelope(
                0,
                "ladder_transition_decided",
                {
                    "from_level": "observe",
                    "to_level": "nudge",
                    "reason": "x",
                    "drift_kind": "DRIFT_KIND_OFF_TOPIC",
                    "drift_id": "d1",
                    "severity": "DRIFT_SEVERITY_WARNING",
                },
            ),
        ],
    )

    captured: dict[str, str] = {}

    async def fake_aux(system: str, user: str, model: str) -> str:
        captured["user"] = user
        return "## Headline observations\n- ok\n"

    asyncio.run(analyze_epoch_telemetry(workspace, epoch_id, fake_aux, round_n=1))
    assert "Available mutation targets" in captured["user"]
    assert "none observed" in captured["user"].lower()


def test_render_mutation_targets_dedupes_and_orders() -> None:
    """``render_insight_user_prompt`` de-dupes mutation ids, first-seen order."""
    from zicato.analyzer.aggregator import DecisionEventSummary
    from zicato.analyzer.prompts import render_insight_user_prompt

    summary = DecisionEventSummary(total_events_seen=0)
    prompt = render_insight_user_prompt(
        summary,
        "ep1",
        mutation_ids=["mut_b", "mut_a", "mut_b", "  ", "mut_c"],
    )
    # Each unique id appears exactly once.
    assert prompt.count("`mut_b`") == 1
    assert prompt.count("`mut_a`") == 1
    assert prompt.count("`mut_c`") == 1
    # First-seen order preserved.
    assert prompt.index("`mut_b`") < prompt.index("`mut_a`") < prompt.index("`mut_c`")


def test_analyze_epoch_telemetry_empty_epoch_short_circuits(tmp_path: Path) -> None:
    """Epoch with no events.jsonl → fallback body, aux callable NOT invoked."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_empty"
    _make_epoch_tree(workspace, epoch_id)

    invoked = False

    async def fake_aux(_system: str, _user: str, _model: str) -> str:
        nonlocal invoked
        invoked = True
        return "this should not appear"

    out = asyncio.run(analyze_epoch_telemetry(workspace, epoch_id, fake_aux, round_n=0))

    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert invoked is False
    assert "No decision-telemetry events" in body


def test_analyze_epoch_telemetry_latest_filename(tmp_path: Path) -> None:
    """``round_n=None`` writes to ``insights/latest.md``."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_latest"
    _make_epoch_tree(workspace, epoch_id)
    _write_events(
        workspace,
        epoch_id,
        "v0",
        "e1",
        [
            _envelope(
                0,
                "policy_applied",
                {
                    "policy_name": "observation_only_gate",
                    "outcome": "applied",
                    "reason": "observation_only=true",
                    "detail": "",
                },
            ),
        ],
    )

    async def fake_aux(_system: str, _user: str, _model: str) -> str:
        return "# insight\n"

    out = asyncio.run(analyze_epoch_telemetry(workspace, epoch_id, fake_aux, round_n=None))

    assert out.name == "latest.md"


def test_load_latest_insights_concatenates_round_files(tmp_path: Path) -> None:
    """Multiple round_*.md files concatenate in lexicographic order."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_load"
    insights_dir = workspace / "epochs" / epoch_id / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    (insights_dir / "round_0001.md").write_text("# round 1\n", encoding="utf-8")
    (insights_dir / "round_0002.md").write_text("# round 2\n", encoding="utf-8")
    (insights_dir / "round_0003.md").write_text("# round 3\n", encoding="utf-8")

    joined = load_latest_insights(workspace, epoch_id)

    assert "# round 1" in joined
    assert "# round 2" in joined
    assert "# round 3" in joined
    # Ordering: round_0001 before round_0002 before round_0003.
    assert joined.index("# round 1") < joined.index("# round 2")
    assert joined.index("# round 2") < joined.index("# round 3")


def test_load_latest_insights_empty_when_missing(tmp_path: Path) -> None:
    """No insights directory → empty string (the proposer's sentinel)."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_none"
    assert load_latest_insights(workspace, epoch_id) == ""


def test_load_latest_insights_empty_when_no_md(tmp_path: Path) -> None:
    """Insights dir exists but has no readable markdown → empty string."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_blank"
    (workspace / "epochs" / epoch_id / "insights").mkdir(parents=True, exist_ok=True)
    # Stray non-.md file should be ignored.
    (workspace / "epochs" / epoch_id / "insights" / "notes.txt").write_text("ignore me")
    assert load_latest_insights(workspace, epoch_id) == ""


def test_analyze_epoch_telemetry_timeout_bounded(tmp_path: Path) -> None:
    """A hung aux callable does not block past the configured budget."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_timeout"
    _make_epoch_tree(workspace, epoch_id)
    _write_events(
        workspace,
        epoch_id,
        "v0",
        "e1",
        [
            _envelope(
                0,
                "retry_budget_spent",
                {
                    "operation": "refine",
                    "attempt": 1,
                    "budget_remaining": 1,
                    "reason": "call_llm raised",
                },
            ),
        ],
    )

    # 0.1 second timeout so the test runs fast — pinned the way the
    # --aux-call-timeout flag pins it (the env binding is deleted).
    from zicato.config import pin_overrides

    pin_overrides({"aux": {"call_timeout_s": 0.1}})

    async def hung_aux(_system: str, _user: str, _model: str) -> str:
        await asyncio.sleep(5.0)
        return "never"

    out = asyncio.run(analyze_epoch_telemetry(workspace, epoch_id, hung_aux, round_n=1))

    body = out.read_text(encoding="utf-8")
    assert "timeout" in body.lower()


def test_analyze_epoch_telemetry_handles_aux_exception(tmp_path: Path) -> None:
    """An aux callable that raises → fallback body cites the exception."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_err"
    _make_epoch_tree(workspace, epoch_id)
    _write_events(
        workspace,
        epoch_id,
        "v0",
        "e1",
        [
            _envelope(
                0,
                "policy_applied",
                {
                    "policy_name": "same_turn_dedup",
                    "outcome": "skipped",
                    "reason": "",
                    "detail": "",
                },
            ),
        ],
    )

    async def broken_aux(_system: str, _user: str, _model: str) -> str:
        raise RuntimeError("simulated provider outage")

    out = asyncio.run(analyze_epoch_telemetry(workspace, epoch_id, broken_aux, round_n=0))

    body = out.read_text(encoding="utf-8")
    assert "simulated provider outage" in body
    assert "RuntimeError" in body
