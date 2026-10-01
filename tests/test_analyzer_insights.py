"""Tests for the decision-telemetry analyzer entry point.

The tests stand up a tiny workspace tree with synthetic ``events.jsonl``
files and exercise:

* ``analyze_epoch_telemetry`` happy path → markdown written.
* Empty epoch (no events) → fallback markdown written, no LLM call.
* ``load_latest_insight`` returns only the highest-numbered round file,
  only when it opens with the training-slice provenance line, and bounded
  in length.
* ``training_entry_ids`` narrows the analysis to the named board entries'
  runs, ``restricted_identities`` restricts the summary, and
  ``proposer_slice`` names the epoch's training slice and visibility
  posture.
* Timeout enforcement when the evaluation callable hangs.
* An aux callable that raises → fallback body cites the exception.
"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest

from zicato.analyzer import analyze_epoch_telemetry, load_latest_insight
from zicato.analyzer.aggregator import DecisionEventSummary, restrict_summary
from zicato.analyzer.insights import ProposerSlice, proposer_slice

# The provenance line the analyzer writes on a training-slice analysis
# (``TRAINING_SLICE_ANALYSIS_MARKER``), spelled out so the format is pinned.
TRAINING_SLICE_ANALYSIS_MARKER = (
    "<!-- zicato: decision-telemetry analysis of the training slice -->"
)

_MARK = TRAINING_SLICE_ANALYSIS_MARKER + "\n"

# The training slice of the one-entry epochs these tests build.
_SLICE = ("e1",)


def test_report_replacement_preserves_an_open_reader(tmp_path: Path) -> None:
    async def unused_aux(_system: str, _user: str, _model: str) -> str:
        raise AssertionError("an empty epoch requires no evaluation call")

    out = asyncio.run(
        analyze_epoch_telemetry(
            tmp_path, "epoch", unused_aux, training_entry_ids=_SLICE, restricted_identities=None
        )
    )
    previous = "Previous complete report.\n" * 20
    out.write_text(previous, encoding="utf-8")
    out.chmod(0o600)
    with out.open("rb", buffering=0) as reader:
        prefix = reader.read(10)
        asyncio.run(
            analyze_epoch_telemetry(
                tmp_path, "epoch", unused_aux, training_entry_ids=_SLICE, restricted_identities=None
            )
        )
        observed = prefix + reader.read()

    assert observed == previous.encode("utf-8")
    assert out.read_text(encoding="utf-8") != previous
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


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
    run_dir = (
        workspace / "epochs" / epoch_id / "generations" / generation / "runs" / entry / "seed-none"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    from zicato.core.measurement import TOURNAMENT_DRAW
    from zicato.telemetry.reducer import write_loss_profile
    from zicato.testing.fixtures import make_loss_profile

    write_loss_profile(
        make_loss_profile(
            epoch_id=epoch_id, generation_id=generation, entry_id=entry, measurement=TOURNAMENT_DRAW
        ),
        run_dir / "loss.tournament.r0.json",
    )
    path = run_dir / "events.tournament.r0.jsonl"
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
        analyze_epoch_telemetry(
            workspace,
            epoch_id,
            fake_aux,
            model="opaque-1",
            round_n=3,
            training_entry_ids=_SLICE,
            restricted_identities=None,
        )
    )

    assert out.exists()
    # round_n=3 → zero-padded to width 4 in the filename.
    assert out.name == "round_0003.md"
    body = out.read_text(encoding="utf-8")
    assert body == _MARK + "## Headline observations\n- saw 1 ladder transition\n"
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
            training_entry_ids=_SLICE,
            restricted_identities=None,
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

    asyncio.run(
        analyze_epoch_telemetry(
            workspace,
            epoch_id,
            fake_aux,
            round_n=1,
            training_entry_ids=_SLICE,
            restricted_identities=None,
        )
    )
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

    out = asyncio.run(
        analyze_epoch_telemetry(
            workspace,
            epoch_id,
            fake_aux,
            round_n=0,
            training_entry_ids=_SLICE,
            restricted_identities=None,
        )
    )

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

    out = asyncio.run(
        analyze_epoch_telemetry(
            workspace,
            epoch_id,
            fake_aux,
            round_n=None,
            training_entry_ids=_SLICE,
            restricted_identities=None,
        )
    )

    assert out.name == "latest.md"


def test_load_latest_insight_reads_only_the_highest_numbered_round(tmp_path: Path) -> None:
    """Only the most recent round's file is returned; earlier rounds and the
    operator's ``latest.md`` are not."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_load"
    insights_dir = workspace / "epochs" / epoch_id / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    (insights_dir / "round_0002.md").write_text(_MARK + "# round 2\n", encoding="utf-8")
    (insights_dir / "round_0010.md").write_text(_MARK + "# round 10\n", encoding="utf-8")
    (insights_dir / "round_0003.md").write_text(_MARK + "# round 3\n", encoding="utf-8")
    (insights_dir / "latest.md").write_text(_MARK + "# operator run\n", encoding="utf-8")

    assert load_latest_insight(workspace, epoch_id) == "# round 10\n"


def test_load_latest_insight_withholds_a_placeholder(tmp_path: Path) -> None:
    """A placeholder in the latest round yields nothing, not an older analysis."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_placeholder"

    async def unused_aux(_system: str, _user: str, _model: str) -> str:
        raise AssertionError("an epoch with no telemetry requires no evaluation call")

    insights_dir = workspace / "epochs" / epoch_id / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    (insights_dir / "round_0001.md").write_text(_MARK + "# real analysis\n", encoding="utf-8")
    # No telemetry at all: the analyzer writes the empty-epoch placeholder.
    out = asyncio.run(
        analyze_epoch_telemetry(
            workspace,
            epoch_id,
            unused_aux,
            round_n=2,
            training_entry_ids=_SLICE,
            restricted_identities=None,
        )
    )

    assert not out.read_text(encoding="utf-8").startswith(TRAINING_SLICE_ANALYSIS_MARKER)
    assert load_latest_insight(workspace, epoch_id) == ""


def _latest_after_writing(tmp_path: Path, body: str) -> str:
    workspace = tmp_path / ".zicato"
    insights_dir = workspace / "epochs" / "ep_provenance" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    (insights_dir / "round_0004.md").write_text(body, encoding="utf-8")
    return load_latest_insight(workspace, "ep_provenance")


def test_load_latest_insight_withholds_an_unmarked_analysis(tmp_path: Path) -> None:
    """A file without the provenance line is withheld: an analysis written
    before the analyzer was limited to the training slice carries none, and
    may summarize holdout runs."""

    assert _latest_after_writing(tmp_path, "## Headline observations\n- nudge x 3\n") == ""


def test_load_latest_insight_withholds_an_old_format_placeholder(tmp_path: Path) -> None:
    old_placeholder = (
        "# Decision telemetry insights — epoch ep\n\n"
        "_(evaluation LLM call failed: TimeoutError: ; no insights generated for "
        "this round)_\n"
    )
    assert _latest_after_writing(tmp_path, old_placeholder) == ""


def test_load_latest_insight_withholds_a_marker_that_is_not_the_first_line(
    tmp_path: Path,
) -> None:
    assert _latest_after_writing(tmp_path, "# notes\n" + _MARK + "- nudge x 3\n") == ""


def test_load_latest_insight_cuts_a_long_analysis_with_a_visible_note(tmp_path: Path) -> None:
    """The delivered text is bounded at 8000 characters, the bound the
    mutation manifest applies to a span, and says that it was cut."""

    delivered = _latest_after_writing(tmp_path, _MARK + "x" * 9000 + "\nTAIL\n")

    assert delivered.startswith("x" * 8000 + "\n")
    assert "x" * 8001 not in delivered
    assert "TAIL" not in delivered
    assert delivered.endswith("[... truncated: the insight exceeds 8000 chars ...]\n")


def test_load_latest_insight_delivers_an_analysis_at_the_bound_unchanged(tmp_path: Path) -> None:
    assert _latest_after_writing(tmp_path, _MARK + "y" * 8000 + "\n") == "y" * 8000 + "\n"


def test_load_latest_insight_delivers_a_marked_analysis_without_its_marker(
    tmp_path: Path,
) -> None:
    body = _MARK + "## Headline observations\n- nudge x 3\n"
    assert _latest_after_writing(tmp_path, body) == "## Headline observations\n- nudge x 3\n"


def test_load_latest_insight_empty_when_missing(tmp_path: Path) -> None:
    """No insights directory → empty string (the proposer's sentinel)."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_none"
    assert load_latest_insight(workspace, epoch_id) == ""


def test_load_latest_insight_empty_when_no_round_file(tmp_path: Path) -> None:
    """Insights dir exists but has no round file → empty string."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_blank"
    (workspace / "epochs" / epoch_id / "insights").mkdir(parents=True, exist_ok=True)
    (workspace / "epochs" / epoch_id / "insights" / "notes.txt").write_text("ignore me")
    (workspace / "epochs" / epoch_id / "insights" / "latest.md").write_text("# operator run\n")
    assert load_latest_insight(workspace, epoch_id) == ""


def test_the_training_slice_is_a_required_argument(tmp_path: Path) -> None:
    """No default analyzes every run: a caller must name the slice."""

    async def unused_aux(_system: str, _user: str, _model: str) -> str:
        raise AssertionError("unreachable")

    with pytest.raises(TypeError, match="training_entry_ids"):
        analyze_epoch_telemetry(tmp_path, "epoch", unused_aux, round_n=1)  # type: ignore[call-arg]


def test_entry_ids_narrow_the_analysis_to_the_named_entries(tmp_path: Path) -> None:
    """Runs of entries outside ``entry_ids`` contribute nothing to the prompt."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_slice"
    _make_epoch_tree(workspace, epoch_id)
    for entry, policy in (("train_a", "policy_on_train"), ("held_b", "policy_on_holdout")):
        _write_events(
            workspace,
            epoch_id,
            "v1",
            entry,
            [
                _envelope(
                    0,
                    "policy_applied",
                    {"policy_name": policy, "outcome": "applied", "reason": "", "detail": ""},
                )
            ],
        )
    prompts: list[str] = []

    async def recording_aux(_system: str, user: str, _model: str) -> str:
        prompts.append(user)
        return "# insight\n"

    asyncio.run(
        analyze_epoch_telemetry(
            workspace,
            epoch_id,
            recording_aux,
            round_n=1,
            training_entry_ids=("train_a",),
            restricted_identities=None,
        )
    )

    assert len(prompts) == 1
    assert "policy_on_train" in prompts[0]
    assert "policy_on_holdout" not in prompts[0]


def test_proposer_slice_is_the_training_slice_and_visibility_posture(tmp_path: Path) -> None:
    """The epoch's frozen board minus its holdout-tagged entries, in board order."""

    workspace = tmp_path / ".zicato"
    epoch_id = "ep_split"
    epoch = workspace / "epochs" / epoch_id
    epoch.mkdir(parents=True)
    rows = [
        {"id": "t1", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "a"},
        {
            "id": "h1",
            "kind": "single_turn",
            "wall_clock_budget_seconds": 60,
            "input": "b",
            "tags": ["holdout"],
        },
        {"id": "t2", "kind": "single_turn", "wall_clock_budget_seconds": 60, "input": "c"},
    ]
    (epoch / "board.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    # Restricted visibility is the scoring default; an absent scoring file
    # resolves to the defaults.
    assert proposer_slice(workspace, epoch_id) == ProposerSlice(
        training_entry_ids=("t1", "t2"), restricted_identities=("t1", "h1", "t2")
    )
    (epoch / "scoring.json").write_text(
        json.dumps({"overfitting": {"restrict_proposer_visibility": False}})
    )
    assert proposer_slice(workspace, epoch_id) == ProposerSlice(
        training_entry_ids=("t1", "t2"), restricted_identities=None
    )


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

    from zicato.config import AuxConfig

    async def hung_aux(_system: str, _user: str, _model: str) -> str:
        await asyncio.sleep(5.0)
        return "never"

    out = asyncio.run(
        analyze_epoch_telemetry(
            workspace,
            epoch_id,
            hung_aux,
            round_n=1,
            aux_config=AuxConfig(call_timeout_s=0.1),
            training_entry_ids=_SLICE,
            restricted_identities=None,
        )
    )

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

    out = asyncio.run(
        analyze_epoch_telemetry(
            workspace,
            epoch_id,
            broken_aux,
            round_n=0,
            training_entry_ids=_SLICE,
            restricted_identities=None,
        )
    )

    body = out.read_text(encoding="utf-8")
    assert "simulated provider outage" in body
    assert "RuntimeError" in body


def test_restricted_summary_keeps_counts_and_drops_emitter_text() -> None:
    """Free-text reasons are dropped; a name that is long, not an identifier,
    or contains a board entry id is withheld, and its counts are summed."""

    summary = DecisionEventSummary(
        ladder_transitions={"(none)->nudge": 2, "(none)->Train_7 escalation": 1},
        ladder_reasons={"repeat (count=2) on the capital-of-France task": 2},
        dispatch_orders=[("goal_drift", "detector_about_train_7")],
        policy_outcomes={
            "same_turn_dedup": {"applied": 3, "skipped because the user asked X": 1},
            "p" * 49: {"applied": 1},
            "policy for train_7": {"applied": 2},
        },
        retry_attempts={"refine": [1, 2], "refine:train_7": [1]},
        steering_decisions={"goal_drift": {"drift": 4}},
        total_events_seen=9,
    )

    restricted = restrict_summary(summary, ("train_7", "h1"))

    assert restricted.ladder_transitions == {"(none)->nudge": 2, "(none)->(withheld)": 1}
    assert restricted.ladder_reasons == {}
    assert restricted.dispatch_orders == [("goal_drift", "(withheld)")]
    assert restricted.policy_outcomes == {
        "same_turn_dedup": {"applied": 3, "(withheld)": 1},
        "(withheld)": {"applied": 3},
    }
    assert restricted.retry_attempts == {"refine": [1, 2], "(withheld)": [1]}
    assert restricted.steering_decisions == {"goal_drift": {"drift": 4}}
    assert restricted.total_events_seen == 9


def test_restricted_analysis_prompt_omits_reasons_and_entry_ids(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    epoch_id = "ep_restricted"
    _make_epoch_tree(workspace, epoch_id)
    _write_events(
        workspace,
        epoch_id,
        "v1",
        "e1",
        [
            _envelope(
                0,
                "ladder_transition_decided",
                {"to_level": "nudge", "reason": "repeat on the e1 sorting task"},
            ),
            _envelope(
                1,
                "policy_applied",
                {"policy_name": "gate_for_e1", "outcome": "applied", "reason": "", "detail": ""},
            ),
        ],
    )
    prompts: list[str] = []

    async def recording_aux(_system: str, user: str, _model: str) -> str:
        prompts.append(user)
        return "# insight\n"

    for restricted in (None, ("e1",)):
        asyncio.run(
            analyze_epoch_telemetry(
                workspace,
                epoch_id,
                recording_aux,
                round_n=1,
                training_entry_ids=_SLICE,
                restricted_identities=restricted,
            )
        )

    unrestricted, restricted_prompt = prompts
    assert "repeat on the e1 sorting task" in unrestricted
    assert "gate_for_e1" in unrestricted
    assert "sorting task" not in restricted_prompt
    assert "gate_for_e1" not in restricted_prompt
    assert "(withheld)" in restricted_prompt
