"""End-to-end tests for the proposer orchestration loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import Experiment, MutationPoint, Pattern
from zicato.proposer.prompts import render_system_prompt, render_user_prompt
from zicato.proposer.proposer import ProposerError, propose_experiment
from zicato.proposer.structured import PostApplyValidationError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mp(mid: str, *, metadata: dict[str, str] | None = None) -> MutationPoint:
    return MutationPoint(
        id=mid,
        kind="span",
        file=Path(f"/src/{mid}.py"),
        source_root=Path("/src"),
        line_start=1,
        line_end=3,
        content="content",
        content_hash="abc",
        metadata=metadata or {},
    )


_MUTATIONS = [
    _mp("router__sp"),
    _mp("planner__sp"),
]


def _pattern() -> Pattern:
    return Pattern(
        id="pat1",
        kind="drift_kind_frequency",
        summary="off_topic dominates",
        detail={"top_kind": "off_topic", "count": "12"},
        affected_mutation_ids=("router__sp",),
        severity="warning",
    )


def _valid_response() -> str:
    return json.dumps(
        {
            "hypothesis": {
                "core_idea": "tighten router preamble",
                "modulating": ["router__sp"],
                "why": "off_topic dominates",
                "expected_drift_movements": [
                    {"kind": "off_topic", "direction": "decrease", "magnitude": "medium"}
                ],
                "expected_pass_rate_delta": "+0.05",
            },
            "patches": [
                {
                    "mutation_id": "router__sp",
                    "op": "replace",
                    "new_content": "new router prompt",
                    "rationale": "tighter wording",
                }
            ],
        }
    )


class _StubLLM:
    """Records calls and returns scripted responses in order."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, system: str, user: str, model: str) -> str:
        self.calls.append((system, user, model))
        if not self._responses:
            raise AssertionError("stub LLM ran out of responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_experiment_happy_path() -> None:
    stub = _StubLLM([_valid_response()])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[_pattern()],
        mutations=_MUTATIONS,
        brief_text="# Proposer brief\n- Be careful.\n",
        current_loss_summary="loss=2.3, pass_rate=0.6",
        aux_call_llm=stub,
        model="test-model",
        max_retries=2,
    )
    assert exp.id == "exp_e1_v1"
    assert exp.hypothesis.core_idea == "tighten router preamble"
    assert len(exp.patches) == 1
    assert exp.outcome is None
    assert len(stub.calls) == 1


@pytest.mark.asyncio
async def test_propose_experiment_passes_brief_to_system_prompt() -> None:
    stub = _StubLLM([_valid_response()])
    brief = "# Proposer brief\n- Operator hint: prefer router edits.\n"
    await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        brief_text=brief,
        current_loss_summary="",
        aux_call_llm=stub,
    )
    system, _, _ = stub.calls[0]
    assert "Operator hint" in system


@pytest.mark.asyncio
async def test_propose_experiment_passes_patterns_and_mutations() -> None:
    stub = _StubLLM([_valid_response()])
    await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[_pattern()],
        mutations=_MUTATIONS,
        brief_text="",
        current_loss_summary="loss summary text",
        aux_call_llm=stub,
    )
    _, user, _ = stub.calls[0]
    assert "loss summary text" in user
    assert "off_topic dominates" in user
    assert "router__sp" in user
    assert "planner__sp" in user


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_experiment_retries_on_invalid_json() -> None:
    stub = _StubLLM(["this is not json", _valid_response()])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        brief_text="",
        current_loss_summary="",
        aux_call_llm=stub,
        max_retries=2,
    )
    assert exp.hypothesis.core_idea == "tighten router preamble"
    assert len(stub.calls) == 2
    # The retry's user prompt should mention the previous failure.
    _, retry_user, _ = stub.calls[1]
    assert "Previous attempt was rejected" in retry_user


@pytest.mark.asyncio
async def test_propose_experiment_retries_on_schema_error() -> None:
    bad = json.dumps({"hypothesis": {}, "patches": []})
    stub = _StubLLM([bad, _valid_response()])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        brief_text="",
        current_loss_summary="",
        aux_call_llm=stub,
        max_retries=2,
    )
    assert exp.id == "exp_e1_v1"
    assert len(stub.calls) == 2


@pytest.mark.asyncio
async def test_propose_experiment_raises_after_exhausting_retries() -> None:
    stub = _StubLLM(["junk1", "junk2", "junk3"])
    with pytest.raises(ProposerError) as exc_info:
        await propose_experiment(
            epoch_id="e1",
            parent_generation_id="v0",
            new_generation_id="v1",
            patterns=[],
            mutations=_MUTATIONS,
            brief_text="",
            current_loss_summary="",
            aux_call_llm=stub,
            max_retries=2,
        )
    assert len(exc_info.value.attempts) == 3
    assert len(stub.calls) == 3


@pytest.mark.asyncio
async def test_propose_experiment_handles_llm_exception_as_retryable() -> None:
    stub = _StubLLM([RuntimeError("transient"), _valid_response()])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        brief_text="",
        current_loss_summary="",
        aux_call_llm=stub,
        max_retries=2,
    )
    assert exp.hypothesis.core_idea == "tighten router preamble"
    assert len(stub.calls) == 2


# ---------------------------------------------------------------------------
# Post-apply validation hook — destructive patches are a retryable class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_experiment_retries_on_post_apply_validation_failure() -> None:
    """A destructive patch (caught post-apply) triggers a bounded retry.

    The first proposer response parses cleanly but its patch set breaks
    the snapshot; the validation hook returns concrete error strings.
    The proposer must feed those back as feedback and re-propose — the
    second response is accepted.
    """
    seen_experiments: list[Experiment] = []

    async def validate(exp: Experiment) -> list[str]:
        seen_experiments.append(exp)
        # Reject the first applied patch set; accept any later one.
        if len(seen_experiments) == 1:
            return [
                "Post-apply file agent.py dropped top-level imports: import os",
                "mutation_id 'router__sp' no longer resolves in target_root",
            ]
        return []

    stub = _StubLLM([_valid_response(), _valid_response()])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        brief_text="",
        current_loss_summary="",
        aux_call_llm=stub,
        max_retries=2,
        validate_experiment=validate,
    )
    assert exp.hypothesis.core_idea == "tighten router preamble"
    # Two LLM calls: the destructive attempt and the accepted retry.
    assert len(stub.calls) == 2
    assert len(seen_experiments) == 2
    # The retry's user prompt carries the concrete validator findings.
    _, retry_user, _ = stub.calls[1]
    assert "Previous attempt was rejected" in retry_user
    assert "dropped top-level imports" in retry_user
    assert "no longer resolves" in retry_user


@pytest.mark.asyncio
async def test_propose_experiment_post_apply_failure_is_bounded() -> None:
    """A proposer that keeps emitting destructive patches gives up bounded.

    The validation hook rejects every attempt; the proposer must exhaust
    exactly ``max_retries + 1`` attempts and then raise — the retry
    budget is honoured, so the per-run wall-clock budget cannot blow.
    """
    call_count = {"n": 0}

    async def always_reject(exp: Experiment) -> list[str]:
        del exp
        call_count["n"] += 1
        return ["Post-apply syntax error in agent.py: unexpected EOF"]

    stub = _StubLLM([_valid_response(), _valid_response(), _valid_response()])
    with pytest.raises(ProposerError) as exc_info:
        await propose_experiment(
            epoch_id="e1",
            parent_generation_id="v0",
            new_generation_id="v1",
            patterns=[],
            mutations=_MUTATIONS,
            brief_text="",
            current_loss_summary="",
            aux_call_llm=stub,
            max_retries=2,
            validate_experiment=always_reject,
        )
    # Bounded: exactly max_retries + 1 attempts, no more.
    assert len(stub.calls) == 3
    assert call_count["n"] == 3
    assert len(exc_info.value.attempts) == 3
    assert all("post-apply" in a.lower() for a in exc_info.value.attempts)


@pytest.mark.asyncio
async def test_propose_experiment_post_apply_accepts_raised_error() -> None:
    """The hook may signal failure by raising PostApplyValidationError."""
    state = {"n": 0}

    async def validate(exp: Experiment) -> list[str]:
        del exp
        state["n"] += 1
        if state["n"] == 1:
            raise PostApplyValidationError(["mutation_id 'x' no longer resolves"])
        return []

    stub = _StubLLM([_valid_response(), _valid_response()])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        brief_text="",
        current_loss_summary="",
        aux_call_llm=stub,
        max_retries=2,
        validate_experiment=validate,
    )
    assert exp.id == "exp_e1_v1"
    assert len(stub.calls) == 2
    _, retry_user, _ = stub.calls[1]
    assert "no longer resolves" in retry_user


@pytest.mark.asyncio
async def test_propose_experiment_no_hook_skips_post_apply_validation() -> None:
    """Without a validation hook the proposer behaves exactly as before."""
    stub = _StubLLM([_valid_response()])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        brief_text="",
        current_loss_summary="",
        aux_call_llm=stub,
        max_retries=2,
    )
    assert exp.id == "exp_e1_v1"
    assert len(stub.calls) == 1


# ---------------------------------------------------------------------------
# Forbidden-id enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_experiment_rejects_forbidden_patch() -> None:
    stub = _StubLLM([_valid_response()])
    with pytest.raises(ProposerError) as exc_info:
        await propose_experiment(
            epoch_id="e1",
            parent_generation_id="v0",
            new_generation_id="v1",
            patterns=[],
            mutations=_MUTATIONS,
            brief_text="",
            current_loss_summary="",
            aux_call_llm=stub,
            max_retries=0,
            forbidden_ids=("router__sp",),
        )
    assert any("forbidden" in a.lower() for a in exc_info.value.attempts)


@pytest.mark.asyncio
async def test_propose_experiment_succeeds_when_retry_avoids_forbidden_id() -> None:
    # First response patches the forbidden id; retry patches a different one.
    alt_response = json.dumps(
        {
            "hypothesis": {
                "core_idea": "tighten planner",
                "modulating": ["planner__sp"],
                "why": "switching after forbidden hit",
                "expected_drift_movements": [
                    {"kind": "off_topic", "direction": "decrease", "magnitude": "small"}
                ],
                "expected_pass_rate_delta": "+0.02",
            },
            "patches": [
                {
                    "mutation_id": "planner__sp",
                    "op": "replace",
                    "new_content": "new planner prompt",
                    "rationale": "switched target",
                }
            ],
        }
    )
    stub = _StubLLM([_valid_response(), alt_response])
    exp = await propose_experiment(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        brief_text="",
        current_loss_summary="",
        aux_call_llm=stub,
        max_retries=2,
        forbidden_ids=("router__sp",),
    )
    assert exp.patches[0].mutation_id == "planner__sp"


# ---------------------------------------------------------------------------
# Prompt rendering sanity
# ---------------------------------------------------------------------------


def test_render_system_prompt_embeds_brief() -> None:
    rendered = render_system_prompt("# Rubric\nSome guidance.")
    assert "Some guidance." in rendered
    # The schema description should mention required field names.
    assert "core_idea" in rendered
    assert "expected_drift_movements" in rendered


def test_render_system_prompt_handles_empty_brief() -> None:
    rendered = render_system_prompt("")
    assert "(empty)" in rendered


def test_render_system_prompt_carries_style_section_for_new_content() -> None:
    """The Style section instructs the proposer to break long ``new_content``.

    The dashboard's mutation-diff view rendered emitted prompts as one
    unbroken line, making the diff column unreadable. The Style section
    is the content-formatting expectation that pushes the proposer to
    emit ``new_content`` with sensible line breaks (~80-100 chars per
    line) — the patch applier handles indentation re-anchoring already,
    so the proposer only needs to insert newlines.
    """
    rendered = render_system_prompt("")
    assert "Style" in rendered, "the Style section must be present"
    # The expectation names the field, the line-length target, and the
    # newline-escape encoding so the proposer cannot misread it.
    assert "new_content" in rendered
    # The line-length window is named explicitly so the model has a
    # concrete target (not a vague "keep lines short").
    assert (
        "80-100" in rendered or "80 to 100" in rendered.lower()
    ), "the Style section must name the 80-100 character per-line window"
    # The one-shot example reflects the style: its router prompt is now
    # multi-line via `\\n` inside the JSON string literal.
    assert "Do not include preambles" in rendered
    assert "\\n" in rendered, "the one-shot example must demonstrate the `\\n` line-break encoding"


def test_render_user_prompt_includes_feedback_when_present() -> None:
    rendered = render_user_prompt(
        current_loss_summary="loss=1.0",
        patterns=[],
        mutations=_MUTATIONS,
        feedback="bad json",
    )
    assert "Previous attempt was rejected" in rendered
    assert "bad json" in rendered


def test_render_user_prompt_renders_patterns_block_for_empty_list() -> None:
    rendered = render_user_prompt(
        current_loss_summary="",
        patterns=[],
        mutations=_MUTATIONS,
    )
    assert "no patterns" in rendered.lower()


def test_render_mutation_block_shows_full_content_for_replace_target() -> None:
    """A replace target must be shown in full — never truncated.

    A proposer asked to ``op: replace`` a span has to faithfully
    reproduce every part it is not changing (imports, markers,
    indentation). A truncated preview is exactly how those parts get
    dropped, so the mutation block renders the whole content verbatim.
    """
    from zicato.proposer.prompts import render_mutation_block

    # A long docstring-style span — well past any historical preview cap.
    long_body = (
        '    """Read the generated presentation files and return contents.\n\n'
        + "\n".join(f"    line {i}: detailed guidance about the slug format" for i in range(40))
        + '\n    """\n'
    )
    assert len(long_body) > 240  # would have been truncated under the old cap
    mp = _mp("read_files__doc")
    mp = MutationPoint(
        id=mp.id,
        kind="span",
        file=mp.file,
        source_root=mp.source_root,
        line_start=10,
        line_end=52,
        content=long_body,
        content_hash="abc",
        metadata={},
    )
    rendered = render_mutation_block([mp])
    # Every line of the span survives into the rendered block.
    assert "line 0: detailed guidance" in rendered
    assert "line 39: detailed guidance" in rendered
    # No truncation ellipsis was inserted.
    assert "…" not in rendered
    assert "truncated" not in rendered.lower()
    # The lead-in tells the proposer this is the full span.
    assert "full" in rendered.lower()


def test_render_mutation_block_caps_only_a_runaway_span() -> None:
    """A pathologically large span is trimmed, and the trim is annotated."""
    from zicato.proposer.prompts import _MUTATION_CONTENT_LIMIT_CHARS, render_mutation_block

    huge = "x" * (_MUTATION_CONTENT_LIMIT_CHARS + 5000)
    mp = MutationPoint(
        id="runaway",
        kind="span",
        file=Path("/src/runaway.py"),
        source_root=Path("/src"),
        line_start=1,
        line_end=999,
        content=huge,
        content_hash="abc",
        metadata={},
    )
    rendered = render_mutation_block([mp])
    assert "truncated" in rendered.lower()
    # The trim is far more generous than the old 240-char preview.
    assert _MUTATION_CONTENT_LIMIT_CHARS >= 4000


def test_system_prompt_warns_against_restating_surrounding_code() -> None:
    """The system prompt tells the proposer to emit ONLY the span text."""
    rendered = render_system_prompt("brief")
    lowered = rendered.lower()
    assert "zicato:mutable" in rendered
    assert "marker" in lowered
    assert "import" in lowered
    # It must explicitly say the replacement is only the inner text.
    assert "only the replacement text" in lowered or "only the inner text" in lowered


def test_render_user_prompt_mentions_mutation_metadata() -> None:
    mp = _mp("router__sp", metadata={"role": "system_prompt", "language": "text"})
    rendered = render_user_prompt(
        current_loss_summary="",
        patterns=[],
        mutations=[mp],
    )
    assert "role=system_prompt" in rendered
    assert "language=text" in rendered


def test_render_user_prompt_includes_insights_when_present() -> None:
    rendered = render_user_prompt(
        current_loss_summary="loss=1.0",
        patterns=[],
        mutations=_MUTATIONS,
        insights="## Headline observations\n- ladder escalations: observe->nudge x 5",
    )
    assert "Recent telemetry insights" in rendered
    assert "ladder escalations: observe->nudge x 5" in rendered


def test_render_user_prompt_omits_insights_section_when_empty() -> None:
    rendered = render_user_prompt(
        current_loss_summary="loss=1.0",
        patterns=[],
        mutations=_MUTATIONS,
        insights="",
    )
    assert "Recent telemetry insights" not in rendered


@pytest.mark.asyncio
async def test_propose_experiment_embeds_workspace_insights(tmp_path: Path) -> None:
    """When workspace_root is supplied and insights exist, the user prompt embeds them."""
    workspace = tmp_path / ".zicato"
    insights_dir = workspace / "epochs" / "ep1" / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    (insights_dir / "round_0001.md").write_text(
        "## Suggested next mutations\n- tighten router preamble\n",
        encoding="utf-8",
    )

    stub = _StubLLM([_valid_response()])
    await propose_experiment(
        epoch_id="ep1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        brief_text="brief",
        current_loss_summary="loss=1.0",
        aux_call_llm=stub,
        workspace_root=workspace,
    )
    _, user, _ = stub.calls[0]
    assert "Recent telemetry insights" in user
    assert "tighten router preamble" in user


@pytest.mark.asyncio
async def test_propose_experiment_unchanged_without_insights(tmp_path: Path) -> None:
    """No insights dir → user prompt has no insights section."""
    workspace = tmp_path / ".zicato"
    stub = _StubLLM([_valid_response()])
    await propose_experiment(
        epoch_id="ep_none",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=[],
        mutations=_MUTATIONS,
        brief_text="brief",
        current_loss_summary="loss=1.0",
        aux_call_llm=stub,
        workspace_root=workspace,
    )
    _, user, _ = stub.calls[0]
    assert "Recent telemetry insights" not in user
