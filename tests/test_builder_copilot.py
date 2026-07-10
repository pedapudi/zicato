"""Tests for the tournament-builder copilot (B1b).

Drives :func:`zicato.builder.copilot.run_copilot` with a copilot
``LlmAgent`` wired to a :class:`zicato.testing.adk_fake.FakeADKModel`, on
ADK's own ``Runner``. The fake model scripts two builder tool rounds
(``set_structure("swiss")`` then ``set_holdout(fraction=0.3)``) followed by
a final summary turn, and the tests assert:

* the SSE stream emits ``tool`` + ``patch`` + ``done`` frames carrying the
  right :class:`~zicato.builder.operations.DraftPatch`s;
* the SAME session draft in the shared :class:`DraftStore` was mutated, and
  the form's ``GET /builder/draft`` reflects the change (one source of
  truth);
* the copilot's ``apply`` tool only ever DRY-RUNS — it never writes the
  contract or rolls the epoch;
* the builder skills are injected into the agent instruction.

The live-run tests are gated on ``google.adk``. The graceful-degrade test
runs WITHOUT the import skip — a disabled chat must yield a clean error
frame with no ADK import required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from zicato.builder.config import BuilderAgentConfig, BuilderConfig
from zicato.builder.copilot import CHAT_DISABLED_MESSAGE, run_copilot
from zicato.builder.draft import DraftStore
from zicato.core.types import ScoringWeights
from zicato.epoch.lifecycle import new_epoch
from zicato.workspace.config_io import write_workspace_config


def _seed_workspace(tmp_path: Path) -> Path:
    """Create a workspace with a 4-entry live board the draft inits from."""
    ws = tmp_path / ".zicato"
    ws.mkdir()
    board = tmp_path / "board.jsonl"
    board.write_text(
        "".join(
            f'{{"id": "e{i}", "kind": "single_turn", "budget_s": 60, "input": "hi"}}\n'
            for i in range(1, 5)
        ),
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n\nsteer\n", encoding="utf-8")
    scoring = tmp_path / "scoring.json"
    scoring.write_text(json.dumps({"drift_weight": 1.0}), encoding="utf-8")
    write_workspace_config(
        ws,
        {
            "instance_id": "default",
            "adk_entrypoint": "pkg.mod:agent",
            "mutable_trees": [],
            "source_roots": [],
            "contract": {
                "board_path": str(board.resolve()),
                "rubric_path": str(brief.resolve()),
                "scoring_path": str(scoring.resolve()),
            },
        },
    )
    new_epoch(
        workspace_root=ws,
        name="alpha",
        board_source=board,
        brief_source=brief,
        weights=ScoringWeights(),
        entrypoint="pkg.mod:agent",
    )
    return ws


async def _collect(gen: Any) -> list[dict[str, Any]]:
    """Drain an async frame generator into a list."""
    frames: list[dict[str, Any]] = []
    async for frame in gen:
        frames.append(frame)
    return frames


# ---------------------------------------------------------------------------
# Graceful degrade — runs WITHOUT google.adk.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graceful_degrade_no_model_yields_single_error(tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    config = BuilderConfig()  # empty model ⇒ chat disabled
    assert config.chat_enabled is False
    store = DraftStore()

    frames = await _collect(
        run_copilot(
            config,
            session_id="s1",
            message="set the structure to swiss",
            store=store,
            workspace_root=ws,
        )
    )

    assert frames == [{"type": "error", "message": CHAT_DISABLED_MESSAGE}]


@pytest.mark.asyncio
async def test_graceful_degrade_leaves_draft_untouched(tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    store = DraftStore()

    await _collect(
        run_copilot(
            BuilderConfig(),
            session_id="s1",
            message="set the structure to swiss",
            store=store,
            workspace_root=ws,
        )
    )

    # The form path is untouched: the draft still reflects the live default.
    draft = store.get("s1", ws)
    assert draft.scoring.tournament_structure.structure == "gauntlet"


def test_chat_disabled_message_names_no_vendor() -> None:
    # Durable repo rule: nothing references a model vendor.
    lowered = CHAT_DISABLED_MESSAGE.lower()
    assert "claude" not in lowered
    assert "anthropic" not in lowered


# ---------------------------------------------------------------------------
# Live-run tests — gated on google.adk.
# ---------------------------------------------------------------------------

pytest.importorskip("google.adk")

from zicato.builder.copilot import build_copilot_agent  # noqa: E402
from zicato.builder.copilot_tools import DEFAULT_BUILDER_TOOLS  # noqa: E402
from zicato.testing.adk_fake import (  # noqa: E402
    FunctionCallTurn,
    TextTurn,
    make_fake_adk_model,
)


def _copilot_agent_with_script(script: list[Any]) -> Any:
    """Build a copilot LlmAgent wired to a fake model replaying ``script``."""
    from google.adk.agents import LlmAgent

    model = make_fake_adk_model(script, model="builder-copilot-model")
    return LlmAgent(
        name="zicato_builder_copilot",
        model=model,
        instruction="Edit the draft via your tools, then summarise.",
        tools=list(DEFAULT_BUILDER_TOOLS),
    )


@pytest.mark.asyncio
async def test_two_tool_rounds_mutate_shared_draft_and_stream_patches(tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    store = DraftStore()
    config = BuilderConfig(agent=BuilderAgentConfig(model="builder-copilot-model"))

    agent = _copilot_agent_with_script(
        [
            FunctionCallTurn(name="set_structure", args={"structure": "swiss"}),
            FunctionCallTurn(name="set_holdout", args={"fraction": 0.3}),
            TextTurn(text="Done — swiss with a 0.3 holdout."),
        ]
    )

    frames = await _collect(
        run_copilot(
            config,
            session_id="sess",
            message="make it swiss with a 30% holdout",
            store=store,
            workspace_root=ws,
            agent=agent,
        )
    )

    types = [f["type"] for f in frames]
    # tool + patch per round, then the final text token(s) and a done.
    assert types.count("tool") == 2
    assert types.count("patch") == 2
    assert types[-1] == "done"

    tool_frames = [f for f in frames if f["type"] == "tool"]
    assert tool_frames[0]["name"] == "set_structure"
    assert tool_frames[0]["args"] == {"structure": "swiss"}
    assert tool_frames[1]["name"] == "set_holdout"

    patch_frames = [f for f in frames if f["type"] == "patch"]
    assert patch_frames[0]["patch"]["op"] == "set_structure"
    assert patch_frames[0]["patch"]["changed"]["structure"]["to"] == "swiss"
    assert patch_frames[1]["patch"]["op"] == "set_holdout"
    # Each patch frame carries the same envelope the REST op returns.
    assert "cost" in patch_frames[0] and "warnings" in patch_frames[0]
    assert "diff" in patch_frames[0]

    # The SAME session draft in the shared store was mutated.
    draft = store.get("sess", ws)
    assert draft.scoring.tournament_structure.structure == "swiss"
    assert draft.scoring.overfitting.holdout_fraction == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_form_get_draft_reflects_copilot_edit(tmp_path: Path) -> None:
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from zicato.builder.api import builder_routes

    ws = _seed_workspace(tmp_path)
    store = DraftStore()
    config = BuilderConfig(agent=BuilderAgentConfig(model="builder-copilot-model"))

    agent = _copilot_agent_with_script(
        [
            FunctionCallTurn(name="set_structure", args={"structure": "swiss"}),
            TextTurn(text="Switched to swiss."),
        ]
    )

    await _collect(
        run_copilot(
            config,
            session_id="shared",
            message="swiss please",
            store=store,
            workspace_root=ws,
            agent=agent,
        )
    )

    # The form's GET /builder/draft, bound to the SAME store, reflects it.
    app = Starlette(routes=builder_routes(ws, store=store))
    client = TestClient(app)
    resp = client.get("/builder/draft?session=shared")
    assert resp.status_code == 200
    body = resp.json()
    assert body["draft"]["scoring"]["tournament"]["structure"] == "swiss"


@pytest.mark.asyncio
async def test_copilot_apply_tool_only_dry_runs(tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    store = DraftStore()
    config = BuilderConfig(agent=BuilderAgentConfig(model="builder-copilot-model"))

    # Read the live contract config + board BEFORE the run to compare after.
    from zicato.workspace.config_io import read_workspace_config

    before_config = read_workspace_config(ws)
    board_path = Path(before_config["contract"]["board_path"])
    before_board = board_path.read_text(encoding="utf-8")

    agent = _copilot_agent_with_script(
        [
            FunctionCallTurn(name="set_structure", args={"structure": "swiss"}),
            FunctionCallTurn(name="preview_apply", args={}),
            TextTurn(text="Here is what applying would do."),
        ]
    )

    frames = await _collect(
        run_copilot(
            config,
            session_id="dry",
            message="preview applying swiss",
            store=store,
            workspace_root=ws,
            agent=agent,
        )
    )

    # The preview ran (a tool frame) but wrote nothing to the live contract.
    tool_names = [f["name"] for f in frames if f["type"] == "tool"]
    assert "preview_apply" in tool_names
    # Live board on disk is unchanged — apply was dry-run only, no roll.
    assert board_path.read_text(encoding="utf-8") == before_board
    after_config = read_workspace_config(ws)
    assert after_config["contract"] == before_config["contract"]


def test_skills_injected_into_instruction(tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    store = DraftStore()
    config = BuilderConfig(agent=BuilderAgentConfig(model="builder-copilot-model"))
    draft = store.get("s", ws)

    agent = build_copilot_agent(config, draft, ws)

    instruction = agent.instruction
    # The default builder skills' bodies are spliced into the instruction.
    assert "Building a zicato tournament (copilot guide)" in instruction
    assert "Builder skills" in instruction
    # And the current-draft summary backdrop is present.
    assert "Current draft" in instruction


def test_model_resolves_from_builder_config_model_string(tmp_path: Path) -> None:
    ws = _seed_workspace(tmp_path)
    store = DraftStore()
    config = BuilderConfig(agent=BuilderAgentConfig(model="some-model-string"))
    draft = store.get("s", ws)

    agent = build_copilot_agent(config, draft, ws)

    # A bare model string is passed straight through to the LlmAgent.
    model = agent.model
    model_str = model if isinstance(model, str) else getattr(model, "model", None)
    assert model_str == "some-model-string"


def test_default_builder_tools_registry_covers_every_op() -> None:
    """ANTI-DRIFT PIN: the copilot's tool registry carries every builder op —
    the write ops (incl. the knob-coverage ops), the read ops, and the
    build-time statistical preflight. A new op added to operations.py /
    the API dispatch without a copilot tool fails here."""
    names = {t.__name__ for t in DEFAULT_BUILDER_TOOLS}
    expected = {
        # write ops
        "set_structure",
        "set_param",
        "set_holdout",
        "set_proposer",
        "set_weights",
        "set_gate",
        "set_namespace_weights",
        "set_proposer_quality",
        "set_experiment_memory",
        "set_screening",
        "edit_board_entry",
        "add_judge",
        "remove_judge",
        "set_brief",
        "set_board_meta",
        # read ops
        "estimate_cost",
        "validate",
        "preflight",
        "preview_apply",
        # the fork/compare lifecycle
        "fork",
        "switch",
        "list_drafts",
        "compare",
    }
    assert expected <= names, f"missing tools: {sorted(expected - names)}"


@pytest.mark.asyncio
async def test_preflight_tool_degrades_honestly_via_bound_context(tmp_path: Path) -> None:
    """The async preflight tool runs inside a bound BuilderToolContext and
    returns the honest degrade (the seeded workspace has no baseline
    generation) alongside the recomputed warnings."""
    import json as _json

    from zicato.builder import copilot_tools
    from zicato.builder.copilot_tools import BuilderToolContext, bind_builder_tool_context

    ws = _seed_workspace(tmp_path)
    store = DraftStore()
    ctx = BuilderToolContext(session_id="s", store=store, workspace_root=ws)
    with bind_builder_tool_context(ctx):
        raw = await copilot_tools.preflight()
    payload = _json.loads(raw)
    assert payload["preflight"]["available"] is False
    assert payload["preflight"]["reason"]
    assert "warnings" in payload


def test_new_knob_tools_edit_the_shared_draft(tmp_path: Path) -> None:
    """The knob-coverage tools mutate the SAME session draft the form edits."""
    import json as _json

    from zicato.builder import copilot_tools
    from zicato.builder.copilot_tools import BuilderToolContext, bind_builder_tool_context

    ws = _seed_workspace(tmp_path)
    store = DraftStore()
    ctx = BuilderToolContext(session_id="s", store=store, workspace_root=ws)
    with bind_builder_tool_context(ctx):
        r1 = _json.loads(copilot_tools.set_proposer_quality(best_of_n=4))
        r2 = _json.loads(copilot_tools.set_experiment_memory(cross_epoch=True))
        r3 = _json.loads(copilot_tools.set_namespace_weights(diff_complexity_weight=0.01))
        r4 = _json.loads(copilot_tools.set_holdout(ladder={"budget": 8}))
        r5 = _json.loads(copilot_tools.set_gate(regression_timeout_s=0))
    assert r1["patch"]["changed"]["best_of_n"]["to"] == 4
    assert r2["patch"]["changed"]["cross_epoch"]["to"] is True
    assert r3["patch"]["changed"]["diff_complexity_weight"]["to"] == 0.01
    assert r4["patch"]["changed"]["ladder.budget"]["to"] == 8
    # invalid values come back as an error the model can read, never a crash.
    assert "error" in r5
    draft = store.get("s", ws)
    assert draft.scoring.proposer_quality.best_of_n == 4
    assert draft.scoring.experiment_memory.cross_epoch is True
    assert draft.scoring.overfitting.ladder.budget == 8


def test_lifecycle_tools_fork_switch_compare(tmp_path: Path) -> None:
    """The fork/switch/list/compare tools drive the SAME store the REST
    endpoints use, through the bound context."""
    import json as _json

    from zicato.builder import copilot_tools
    from zicato.builder.copilot_tools import BuilderToolContext, bind_builder_tool_context

    ws = _seed_workspace(tmp_path)
    store = DraftStore()
    ctx = BuilderToolContext(session_id="s", store=store, workspace_root=ws)
    with bind_builder_tool_context(ctx):
        r1 = _json.loads(copilot_tools.fork("variant-a"))
        assert r1["drafts"] == ["variant-a"]
        _json.loads(copilot_tools.set_gate(promote_margin=0.07))
        r2 = _json.loads(copilot_tools.fork("variant-b"))
        assert r2["drafts"] == ["variant-a", "variant-b"]
        r3 = _json.loads(copilot_tools.compare("variant-a", "variant-b"))
        # forked before/after the margin edit: A carries it too? No — A was
        # forked FIRST, then edited (the session was bound to A), then B
        # forked from A. So A == B here; compare says identical.
        assert r3["compare"]["changed_components"] == []
        _json.loads(copilot_tools.set_structure("swiss"))
        r4 = _json.loads(copilot_tools.compare("variant-a", "variant-b"))
        assert "scoring" in r4["compare"]["changed_components"]
        r5 = _json.loads(copilot_tools.switch("variant-a"))
        assert r5["patch"]["op"] == "switch"
        r6 = _json.loads(copilot_tools.list_drafts())
        assert r6["drafts"] == ["variant-a", "variant-b"]
        # errors surface as readable tool results, never exceptions.
        assert "error" in _json.loads(copilot_tools.fork("variant-a"))
        assert "error" in _json.loads(copilot_tools.switch("ghost"))
        assert "error" in _json.loads(copilot_tools.compare("session", "ghost"))
