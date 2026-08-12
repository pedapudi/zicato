"""Tests for the target 4 (coding-agent config package) example scaffolding.

Target 4's system under test is an external coding agent binary that CI does
not have and must never call. Everything here is exercised against the
example's own hermetic stand-in (``stub_agent.py``): a real subprocess
speaking the real rpc protocol, reading the real config package, editing a
real working tree — with no model anywhere.

The four properties pinned, mirroring target 0's suite shape:

1. the markdown mutation surface enumerates (floor + membership — the set
   is open, so nothing here asserts a closed set);
2. the board parses and validates, and every predicate it names resolves;
3. the driver honours the adapter contract — transcript-dialect sinks, the
   snapshot's config package mounted, an offline environment, and a
   wall-clock abort rather than an exception; and
4. a patch applies to the markdown surface and the point re-enumerates
   under the same id.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

import zicato_examples.target_4_agent_config as _t4_pkg
from zicato.adapter_factory import make_adapter_from_config
from zicato.adapters.base import HarnessAdapter
from zicato.core import BoardEntry, Patch, validate_board_entry
from zicato.mutation.applier import apply_patches
from zicato.mutation.enumerator import enumerate_mutations
from zicato_examples.target_4_agent_config import predicates
from zicato_examples.target_4_agent_config.driver import (
    AGENT_BIN_ENV,
    ENV_PASSTHROUGH_PREFIX,
    PATCH_SENTINEL,
    agent_environment,
    config_fingerprint,
    make_adapter,
    run_identifier,
)
from zicato_examples.target_4_agent_config.stub_agent import FINGERPRINT_PREFIX, STUB_PLAN_ENV

EXAMPLE_DIR = Path(_t4_pkg.__file__).resolve().parent
CONFIG_PACKAGE = EXAMPLE_DIR / "config_package"

#: The mutation ids the brief names as the preferred surface. Membership is
#: asserted, not equality: adding a fifth marked region is a legitimate
#: operator edit and must not break the suite.
EXPECTED_MUTATION_IDS = frozenset(
    {
        "agents_operating_rules",
        "agents_tool_policy",
        "skill_repo_navigation",
        "skill_patch_discipline_rules",
    }
)


class _RecordingSink:
    """Collects what the driver emits, in the shape the sinks receive it."""

    def __init__(self) -> None:
        self.lines: list[dict[str, str]] = []

    async def emit(self, event: dict[str, str]) -> None:
        self.lines.append(dict(event))


def _use_stub(monkeypatch: pytest.MonkeyPatch, plan: dict[str, object] | None = None) -> None:
    """Point the driver at the stub binary and hand it a plan.

    The plan travels through the driver's explicit passthrough prefix
    because the offline environment drops everything else — which is
    itself part of what this suite pins.
    """
    monkeypatch.setenv(
        AGENT_BIN_ENV,
        f"{sys.executable} -m zicato_examples.target_4_agent_config.stub_agent",
    )
    monkeypatch.setenv(
        f"{ENV_PASSTHROUGH_PREFIX}{STUB_PLAN_ENV}", json.dumps(plan or {}, sort_keys=True)
    )


def _entry(
    entry_id: str = "t4_fix_window",
    budget: int = 60,
    **context: str,
) -> BoardEntry:
    return BoardEntry(
        id=entry_id,
        kind="single_turn",
        wall_clock_budget_seconds=budget,
        context={"fixture": "toolbox", **context},
        input="fix the window slice",
    )


# ---------------------------------------------------------------------------
# 1. The mutation surface
# ---------------------------------------------------------------------------


def test_config_package_enumerates_the_briefed_mutation_points() -> None:
    """Every id the brief names enumerates from the markdown surface."""
    points = enumerate_mutations([CONFIG_PACKAGE])
    found = {p.id for p in points}
    assert EXPECTED_MUTATION_IDS <= found, f"missing: {sorted(EXPECTED_MUTATION_IDS - found)}"
    by_id = {p.id: p for p in points}
    assert by_id["skill_repo_navigation"].kind == "file"
    assert by_id["agents_operating_rules"].kind == "code"
    assert "vendor/" in by_id["skill_patch_discipline_rules"].content


def test_settings_json_hosts_no_mutation_point() -> None:
    """Strict JSON cannot carry a marker, so ``settings.json`` is immutable.

    Not an oversight to be fixed later by marking it: a JSON document with
    a comment in it is not JSON. The file is real and shipped; it is simply
    outside the surface the proposer may rewrite.
    """
    assert (CONFIG_PACKAGE / "settings.json").is_file()
    touched = {p.file.name for p in enumerate_mutations([CONFIG_PACKAGE])}
    assert "settings.json" not in touched


# ---------------------------------------------------------------------------
# 2. The board
# ---------------------------------------------------------------------------


def test_board_loads_validates_and_resolves_its_predicates() -> None:
    """Every entry validates, names a real fixture, and names a real predicate."""
    path = EXAMPLE_DIR / "board.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(rows) >= 3, "the smoke board is 3-5 entries"

    for row in rows:
        entry = validate_board_entry(row)
        assert entry.kind == "single_turn"
        fixture = entry.context.get("fixture", "")
        assert (EXAMPLE_DIR / "fixtures" / fixture).is_dir(), f"{entry.id}: no fixture {fixture!r}"
        assert entry.expectation is not None, f"{entry.id}: no expectation"
        module_path, _, func = entry.expectation.spec.partition(":")
        resolved = getattr(importlib.import_module(module_path), func)
        assert callable(resolved), f"{entry.id}: {entry.expectation.spec} is not callable"


def test_scoring_pins_the_transcript_dialect() -> None:
    """The contract is transcript-dialect; drift knobs stay at their defaults.

    A non-default drift knob under ``transcript`` is inert and earns a
    capability warning — the contract must not ask for measurement the
    dialect cannot produce.
    """
    scoring = json.loads((EXAMPLE_DIR / "scoring.json").read_text())
    assert scoring["telemetry_dialect"] == "transcript"
    assert scoring["drift_weight"] == 1.0
    assert scoring["plan_revision_weight"] == 0.5
    assert scoring.get("per_kind_weights", {}) == {}


# ---------------------------------------------------------------------------
# 3. The driver contract
# ---------------------------------------------------------------------------


def test_adapter_satisfies_the_harness_protocol_and_round_trips() -> None:
    """The adapter is a :class:`HarnessAdapter` the worker can rebuild."""
    adapter = make_adapter()
    assert isinstance(adapter, HarnessAdapter)
    rebuilt = make_adapter_from_config({"adapter": adapter.worker_spec()})
    assert type(rebuilt) is type(adapter)
    assert adapter.mutable_subpaths(EXAMPLE_DIR) == [CONFIG_PACKAGE]


def test_agent_environment_is_offline_and_carries_no_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A caller's credentials and proxy settings never reach the agent."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-travel")
    monkeypatch.setenv("HTTPS_PROXY", "http://should-not-travel:8080")
    env = agent_environment(tmp_path)
    assert "OPENAI_API_KEY" not in env
    assert "HTTPS_PROXY" not in env
    assert env["PI_OFFLINE"] == "1"
    assert env["PI_CODING_AGENT_DIR"] == str(tmp_path)


async def test_driver_drives_the_stub_and_reports_the_produced_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One full run: turns to the sinks, edits to the tree, diff to the output."""
    fixed = (
        (EXAMPLE_DIR / "fixtures/toolbox/ops.py")
        .read_text()
        .replace("values[: size - 1]", "values[:size]")
    )
    _use_stub(
        monkeypatch,
        {
            "turns": [{"role": "assistant", "content": "reading ops.py"}],
            "writes": {"ops.py": fixed},
            "final": "took the full window",
        },
    )
    sink = _RecordingSink()
    session = make_adapter().load(EXAMPLE_DIR)
    result = await session.run(_entry(), [sink], None)

    assert not result.aborted
    assert session.agent_version, "the binary's --version is probed and recorded"
    # Transcript dialect: the sinks see {"role", "content"} lines and nothing else.
    assert [line["role"] for line in sink.lines] == ["user", "assistant"]
    assert all(set(line) == {"role", "content"} for line in sink.lines)
    # The diff is appended after the sentinel, and the predicate grades it.
    assert PATCH_SENTINEL in result.final_output
    assert predicates.patched_paths(result) == {"ops.py"}
    assert predicates.fixes_window_off_by_one(result)
    # The fixture on disk is untouched — the run worked on a copy.
    assert "values[: size - 1]" in (EXAMPLE_DIR / "fixtures/toolbox/ops.py").read_text()


async def test_emitted_lines_round_trip_through_the_real_transcript_reducer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The driver's sink lines reduce under the dialect the contract pins.

    The claim this target rests on is that ``telemetry_dialect: transcript``
    needs no new plumbing — so this drives the REAL goldfive JSONL sink the
    worker attaches and the REAL producer, rather than asserting the shape
    of the dicts and hoping.

    It is also a guard against a silent regression. ``reduce_transcript``
    skips any line carrying a ``type`` it does not know, counting it as
    malformed rather than raising, so a future edit that wrapped these
    lines in the driver's own ``{"type": "turn", ...}`` protocol envelope
    would reduce every run to zero turns and fail nothing. Asserting
    ``malformed_line_count == 0`` is what catches that.
    """
    pytest.importorskip("goldfive")
    from goldfive.sinks.persistence import JSONLPersistenceSink

    from zicato.telemetry.dialects import reduce_transcript

    _use_stub(
        monkeypatch,
        {"turns": [{"role": "assistant", "content": "reading ops.py"}], "final": "done"},
    )
    events = tmp_path / "events.jsonl"
    entry = _entry()
    await (
        make_adapter()
        .load(EXAMPLE_DIR)
        .run(entry, [JSONLPersistenceSink(path=events, mode="write")], None)
    )

    signals = reduce_transcript(events, entry)
    assert signals.user_turns == ("fix the window slice",)
    assert signals.agent_turns == ("reading ops.py",)
    assert signals.malformed_line_count == 0
    assert signals.warnings == ()
    # The floor tier carries no drift, which is why the drift knobs in
    # scoring.json are left at their (inert) defaults.
    assert signals.drift_counts == ()


async def test_driver_mounts_the_snapshot_config_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The agent's config dir is the SNAPSHOT's package, not the checkout's.

    The stub digests whatever ``PI_CODING_AGENT_DIR`` points at and reports
    it, so an edited snapshot must produce a different fingerprint than the
    working tree. This is the whole mechanism of the target: the config
    directory is the agent's identity, so the generation under evaluation
    has to be the one that loads.
    """
    _use_stub(monkeypatch)
    snapshot = tmp_path / "gen"
    shutil.copytree(CONFIG_PACKAGE, snapshot / "config_package")
    agents_md = snapshot / "config_package" / "AGENTS.md"
    agents_md.write_text(agents_md.read_text() + "\n- An edit only this snapshot has.\n")

    result = await make_adapter().load(snapshot).run(_entry(), [], None)

    reported = [
        line.split(FINGERPRINT_PREFIX, 1)[1]
        for line in result.final_output.splitlines()
        if FINGERPRINT_PREFIX in line
    ]
    assert reported == [config_fingerprint(snapshot / "config_package")]
    assert reported[0] != config_fingerprint(CONFIG_PACKAGE)


def test_run_ids_separate_generations_and_replicates() -> None:
    """One entry run twice never reuses an id — or a scratch directory.

    ``runs`` rows in the analytical index are keyed on ``run_id``, so a
    reused id reads as the same run overwritten rather than two runs; and
    because the per-run scratch directory is named from the id, a
    collision would have two concurrent replicates deleting each other's
    working tree mid-run.
    """
    base = run_identifier(_entry())
    generation = run_identifier(_entry(generation_id="v3"))
    replicate = run_identifier(_entry(generation_id="v3", replicate_index="2"))
    assert len({base, generation, replicate}) == 3
    assert "v3" in generation
    assert replicate.endswith("-r2")
    # A malformed replicate index degrades to 0 rather than failing a run.
    assert run_identifier(_entry(generation_id="v3", replicate_index="?")) == generation


async def test_wall_clock_budget_aborts_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An over-budget run returns an aborted result, per the adapter contract."""
    _use_stub(monkeypatch, {"sleep": 30})
    result = await make_adapter().load(EXAMPLE_DIR).run(_entry(budget=1), [], None)
    assert result.aborted
    assert result.abort_reason == "wall_clock_budget"


# ---------------------------------------------------------------------------
# 4. Patching the markdown surface
# ---------------------------------------------------------------------------


def test_patch_applies_to_markdown_and_the_point_re_enumerates(tmp_path: Path) -> None:
    """A ``replace`` lands inside the marked region and keeps its id.

    The id has to survive the edit — a proposer targets the same logical
    region round after round — and the markers themselves have to survive,
    because they are the operator's boundary, not the proposer's content.
    """
    source = tmp_path / "before"
    shutil.copytree(CONFIG_PACKAGE, source)
    target = tmp_path / "after"

    new_body = "- Change one thing, and say which thing you changed.\n"
    point = {p.id: p for p in enumerate_mutations([source])}["agents_operating_rules"]
    apply_patches(
        source,
        [
            Patch(
                id="p1",
                mutation_id=point.id,
                op="replace",
                new_content=new_body,
                new_numeric=None,
                new_enum=None,
                rationale="tighten the operating rules",
            )
        ],
        target,
    )

    after = {p.id: p for p in enumerate_mutations([target])}
    assert EXPECTED_MUTATION_IDS <= set(after)
    assert after["agents_operating_rules"].content.strip() == new_body.strip()
    assert after["agents_operating_rules"].content_hash != point.content_hash
    # The untouched sibling region is byte-identical.
    before = {p.id: p for p in enumerate_mutations([source])}
    assert after["agents_tool_policy"].content == before["agents_tool_policy"].content
