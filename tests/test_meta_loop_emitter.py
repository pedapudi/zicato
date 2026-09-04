"""Tests for the meta-loop goldfive emitter (task #204 Part A).

Every test pins one observable behaviour from the architectural target:
the emitter brackets in-process LLM calls (proposer + analyzer) with
paired goldfive envelopes, attaches the canonical JSONL sink and the
optional harmonograf sink, isolates sink failures, and stays
bookkeeping-correct even when no sinks are attached.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from zicato.telemetry.harmonograf_supervisor import meta_loop_session_id
from zicato.telemetry.meta_loop import (
    MetaLoopEmitter,
    build_meta_loop_emitter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CapturingSink:
    """Minimal goldfive ``EventSink``-shaped capture sink for tests."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.closed = False

    async def emit(self, event: Any) -> None:
        self.events.append(event)

    async def close(self) -> None:
        self.closed = True


class _ExplodingSink:
    """Sink whose ``emit`` always raises; for failure-isolation tests."""

    def __init__(self) -> None:
        self.calls = 0

    async def emit(self, event: Any) -> None:
        self.calls += 1
        raise RuntimeError("boom")

    async def close(self) -> None:
        return None


def _event_kind(event: Any) -> str:
    """Project a goldfive event (proto or dict) to a string discriminator.

    For the dict fallback, ``kind`` is a top-level field. For the proto
    envelope, the discriminator was stamped into the nested oneof's
    ``task_id`` field (on ``agent_invocation_started`` /
    ``agent_invocation_completed`` payloads).
    """
    if isinstance(event, dict):
        return str(event.get("kind", ""))
    for nested in ("agent_invocation_started", "agent_invocation_completed"):
        nested_msg = getattr(event, nested, None)
        if nested_msg is None:
            continue
        task_id = getattr(nested_msg, "task_id", "")
        if task_id:
            return str(task_id)
    return ""


# ---------------------------------------------------------------------------
# Session id stability — required Part A coverage #1.
# ---------------------------------------------------------------------------


def test_meta_loop_session_id_stable_across_one_evolve() -> None:
    """One evolve invocation: one session id from one start ISO."""
    iso = "2026-05-28T04:05:06+00:00"
    sid_a = meta_loop_session_id(iso)
    sid_b = meta_loop_session_id(iso)
    assert sid_a == sid_b
    # Different start time -> different session id.
    sid_other = meta_loop_session_id("2026-05-28T04:05:07+00:00")
    assert sid_other != sid_a


# ---------------------------------------------------------------------------
# Agent-name sanitization — regression for the broken meta-loop link.
#
# harmonograf validates the client/agent name against
# ``[a-zA-Z0-9_-]{1,128}``. An ISO start time carries ':' (time + offset)
# and '+' (UTC offset sign), both out-of-class — so an unsanitized id
# made the sink construction raise and the live link point at nothing.
# ---------------------------------------------------------------------------

_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


@pytest.mark.parametrize(
    "iso",
    [
        "2026-05-30T17:30:26+00:00",  # the id from the observed failure
        "2026-05-28T04:05:06+00:00",
        "2026-12-31T23:59:59+05:30",  # non-UTC offset
        "2026-05-28T04:05:06.123456+00:00",  # fractional seconds ('.')
        "2026-05-28 04:05:06+00:00",  # space-separated variant
        "2026-05-28T04:05:06Z",  # 'Z' suffix
    ],
)
def test_meta_loop_session_id_matches_harmonograf_regex(iso: str) -> None:
    """Every representative start ISO yields a regex-valid session id."""
    sid = meta_loop_session_id(iso)
    assert _AGENT_NAME_RE.match(sid), f"invalid agent name: {sid!r}"
    # No out-of-class characters survive.
    assert ":" not in sid
    assert "+" not in sid
    assert "." not in sid
    assert " " not in sid


def test_meta_loop_session_id_for_observed_failure_iso() -> None:
    """Pin the exact before/after for the timestamp from the bug report."""
    sid = meta_loop_session_id("2026-05-30T17:30:26+00:00")
    assert sid == "zicato-meta-loop-2026-05-30T17-30-26-00-00"


def test_sanitized_client_name_is_regex_valid() -> None:
    """The composed harmonograf ``Client`` name is also regex-valid.

    The client name prefixes the session id with ``zicato-meta:`` — the
    ':' is itself out-of-class, so the composed name must be sanitized
    too (otherwise the sink construction raises despite a valid session
    id).
    """
    from zicato.telemetry.harmonograf_supervisor import _sanitize_agent_name

    sid = meta_loop_session_id("2026-05-30T17:30:26+00:00")
    client_name = _sanitize_agent_name(f"zicato-meta:{sid}")
    assert _AGENT_NAME_RE.match(client_name), client_name
    assert ":" not in client_name


def test_sanitize_agent_name_truncates_and_never_empty() -> None:
    """The helper enforces the 1..128 length bound on its output."""
    from zicato.telemetry.harmonograf_supervisor import _sanitize_agent_name

    assert _sanitize_agent_name("") == "-"
    assert _sanitize_agent_name(":::") == "---"
    long = _sanitize_agent_name("x" * 500)
    assert _AGENT_NAME_RE.match(long)
    assert len(long) == 128


# ---------------------------------------------------------------------------
# Sink composition — Part A required #2 and #3.
# ---------------------------------------------------------------------------


def test_build_meta_loop_emitter_jsonl_sink_attached_when_harmonograf_url_empty(
    tmp_path: Path,
) -> None:
    """Empty harmonograf URL still attaches the JSONL sink.

    Meta-loop telemetry is not load-bearing on harmonograf — disk
    telemetry must exist on every degraded install so the dashboard's
    static fallback has something to render.
    """
    emitter = build_meta_loop_emitter(
        tmp_path,
        harmonograf_url="",
        evolve_started_at_iso="2026-05-28T05:00:00+00:00",
    )
    # At least the JSONL sink (goldfive is installed in dev). No
    # harmonograf sink — URL was empty.
    assert len(emitter.sinks) >= 1
    sink_names = [type(s).__name__ for s in emitter.sinks]
    assert any("JSONL" in name for name in sink_names), sink_names


def test_build_meta_loop_emitter_with_harmonograf_url_attaches_extra_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-empty harmonograf URL adds a second sink beyond the JSONL one.

    We stub :func:`build_meta_loop_sink` so the test does not need a
    live harmonograf server — the contract under test is "URL non-empty
    AND helper returns a sink -> sink is appended".
    """
    from zicato.telemetry import meta_loop as ml

    stub_sink = _CapturingSink()

    def _stub_build(url: str, sid: str) -> Any:
        return stub_sink

    # Patch the in-module lookup the factory uses.
    monkeypatch.setattr(
        "zicato.telemetry.harmonograf_supervisor.build_meta_loop_sink",
        _stub_build,
    )

    emitter = ml.build_meta_loop_emitter(
        tmp_path,
        harmonograf_url="http://127.0.0.1:9999",
        evolve_started_at_iso="2026-05-28T05:01:00+00:00",
    )
    assert stub_sink in emitter.sinks


# ---------------------------------------------------------------------------
# Proposer call envelopes — Part A required #4.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proposer_started_completed_emits_paired_envelopes() -> None:
    """A proposer call lands a started + completed pair on every sink."""
    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="run-x", session_id="sess-x", sinks=[sink])

    invocation_id = await emitter.proposer_started(
        model="aux-model",
        epoch_id="ep-1",
        parent_generation_id="v0",
        new_generation_id="v1",
    )
    await emitter.proposer_completed(
        invocation_id=invocation_id,
        latency_s=0.5,
        response_chars=1234,
        outcome="completed",
    )
    assert invocation_id  # non-empty
    assert len(sink.events) == 2
    started, completed = sink.events
    # The paired envelopes carry the same invocation_id discriminator.
    # On the proto path the invocation_id rides on the message; on the
    # dict fallback it's a top-level field. Cover both.
    started_inv = (
        started["invocation_id"]
        if isinstance(started, dict)
        else started.agent_invocation_started.invocation_id
    )
    completed_inv = (
        completed["invocation_id"]
        if isinstance(completed, dict)
        else completed.agent_invocation_completed.invocation_id
    )
    assert started_inv == invocation_id
    assert completed_inv == invocation_id


# ---------------------------------------------------------------------------
# Judge invocation envelopes — Part A required #5.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_invoked_judgment_emitted_paired_envelopes() -> None:
    """A judge call lands a started + verdict pair on every sink."""
    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="run-y", session_id="sess-y", sinks=[sink])

    invocation_id = await emitter.judge_invoked(
        judge_name="decision_telemetry_analyzer",
        kind="process",
    )
    await emitter.judgment_emitted(
        invocation_id=invocation_id,
        judge_name="decision_telemetry_analyzer",
        verdict_kind="rubric",
        score=None,
        detail="insight written",
        latency_s=0.42,
    )
    assert len(sink.events) == 2
    # Both events use the same run_id (one evolve == one run).
    run_id_started = (
        sink.events[0]["run_id"]
        if isinstance(sink.events[0], dict)
        else getattr(sink.events[0], "run_id", "")
    )
    run_id_completed = (
        sink.events[1]["run_id"]
        if isinstance(sink.events[1], dict)
        else getattr(sink.events[1], "run_id", "")
    )
    assert run_id_started == "run-y" == run_id_completed


# ---------------------------------------------------------------------------
# Failure isolation — Part A required #6.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exploding_sink_does_not_crash_proposer_emit() -> None:
    """A raising sink is absorbed; the proposer's call site sees no exception.

    Mirrors the orchestrator's contract: meta-loop telemetry is additive,
    never load-bearing. A broken harmonograf sink must not regress the
    proposer.
    """
    good = _CapturingSink()
    bad = _ExplodingSink()
    emitter = MetaLoopEmitter(run_id="run-z", session_id="sess-z", sinks=[bad, good])

    # No exception escapes — both halves of the pair succeed for ``good``.
    invocation_id = await emitter.proposer_started(
        model="m",
        epoch_id="e",
        parent_generation_id="p",
        new_generation_id="c",
    )
    await emitter.proposer_completed(
        invocation_id=invocation_id,
        latency_s=0.01,
        response_chars=0,
        outcome="completed",
    )
    # ``good`` received both envelopes despite ``bad`` raising on each.
    assert len(good.events) == 2
    # ``bad`` was offered both events (the emitter doesn't disable it
    # after one failure — additive telemetry, never load-bearing).
    assert bad.calls == 2


# ---------------------------------------------------------------------------
# Empty-sink no-op — bookkeeping still works.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_sink_list_emitter_is_a_no_op() -> None:
    """An emitter with no sinks bookkeeping is consistent.

    Used when a degraded install can't attach goldfive — call sites
    still call the emitter methods, but no events go anywhere.
    """
    emitter = MetaLoopEmitter(run_id="r", session_id="s", sinks=[])
    invocation_id = await emitter.proposer_started(
        model="m", epoch_id="e", parent_generation_id="p", new_generation_id="c"
    )
    assert invocation_id  # still minted
    await emitter.proposer_completed(
        invocation_id=invocation_id, latency_s=0.0, outcome="completed"
    )
    # close() on an empty sink list does not raise.
    await emitter.close()


# ---------------------------------------------------------------------------
# JSONL persistence — verify writes reach the file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_loop_jsonl_sink_persists_events(tmp_path: Path) -> None:
    """The emitter's JSONL sink writes a line per envelope."""
    emitter = build_meta_loop_emitter(
        tmp_path,
        harmonograf_url="",
        evolve_started_at_iso="2026-05-28T05:02:00+00:00",
    )
    # Attach our own capturing sink alongside so we can compare.
    cap = _CapturingSink()
    # Direct internal append is fine for this test — keep the JSONL
    # sink, add a side observer.
    emitter._sinks.append(cap)  # type: ignore[attr-defined]  # test-only

    invocation_id = await emitter.proposer_started(
        model="m", epoch_id="e", parent_generation_id="p", new_generation_id="c"
    )
    await emitter.proposer_completed(
        invocation_id=invocation_id, latency_s=0.0, outcome="completed"
    )
    await emitter.close()

    # JSONL file exists and carries two lines (one per envelope).
    jsonl = tmp_path / "runtime" / "meta_loop_events.jsonl"
    # Tolerate either the ``.zicato`` subdir or the workspace root.
    if not jsonl.exists():
        jsonl = tmp_path / ".zicato" / "runtime" / "meta_loop_events.jsonl"
    assert jsonl.exists(), f"missing JSONL at {jsonl}"
    lines = jsonl.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    # The capturing sink observed the same two envelopes.
    assert len(cap.events) == 2


# ---------------------------------------------------------------------------
# Proposer integration — the wired call site emits an envelope.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_proposal_episode_emits_proposer_envelopes(tmp_path: Path) -> None:
    """End-to-end: an episode given an emitter records the pair.

    One episode is one call from this side — the turns inside it are the
    runtime's own transcript — so the pair brackets the episode rather
    than a model request, and the completed envelope carries how the
    episode ended.
    """
    from tests._foe_support import stand_in_proposer_block
    from tests._source_tree_builders import mutable_tree
    from zicato.core.types import ProposerSpec
    from zicato.mutation.enumerator import enumerate_mutations
    from zicato.proposer.agent import ProposerContext
    from zicato.proposer.external import external_proposer_config
    from zicato.proposer.foe_agent import FoeProposerAgent

    sink = _CapturingSink()
    emitter = MetaLoopEmitter(run_id="run-int", session_id="sess-int", sinks=[sink])

    snapshot = tmp_path / "snapshot"
    mutable_tree(snapshot, instr="Route the message.")
    binding = external_proposer_config(
        {"proposer": stand_in_proposer_block(tmp_path / "foe")}, tmp_path
    )
    assert binding is not None
    agent = FoeProposerAgent(
        spec=ProposerSpec(agent_id="external:foe", tools=(), skills=()), config=binding
    )

    async def _unused(system: str, user: str, model: str) -> str:  # pragma: no cover
        raise AssertionError("an episode never calls the evaluation text shim")

    await agent.propose(
        ProposerContext(
            epoch_id="ep",
            parent_generation_id="v0",
            new_generation_id="v1",
            patterns=(),
            mutations=tuple(enumerate_mutations([snapshot])),
            brief_text="",
            current_loss_summary="",
            aux_call_llm=_unused,
            model="aux-model",
            workspace_root=tmp_path,
            generation_root=snapshot,
            meta_loop_emitter=emitter,
        )
    )

    assert len(sink.events) == 2
    kinds = [_event_kind(e) for e in sink.events]
    assert "proposer_call_started" in kinds
    assert "proposer_call_completed" in kinds


# ---------------------------------------------------------------------------
# Part B coverage: lazy / safe build behaviour.
#
# Even with the "keep as is" decision (option i), the meta-loop emitter
# factory should remain robust to a degraded install — that contract is
# what underwrites the option choice, so we pin it.
# ---------------------------------------------------------------------------


def test_build_meta_loop_emitter_tolerates_missing_workspace(
    tmp_path: Path,
) -> None:
    """A nonexistent workspace path doesn't crash the factory.

    The JSONL sink creates the parent directory on emit; the factory
    only resolves the path. A nonexistent path still produces an
    emitter (possibly with the JSONL sink attached) because mkdir is
    deferred to the sink itself or done at construction time on a path
    we can create.
    """
    nowhere = tmp_path / "does-not-exist-yet"
    # No mkdir — let the factory cope.
    emitter = build_meta_loop_emitter(
        nowhere,
        harmonograf_url="",
        evolve_started_at_iso="2026-05-28T05:03:00+00:00",
    )
    # Always returns an emitter, even if the sink list is empty.
    assert emitter is not None
