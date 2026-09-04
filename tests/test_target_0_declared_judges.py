"""The deterministic target_0 harness invokes board-declared process judges.

Regression coverage for issue #84's inline-judge invocation gap. The
deterministic policy harness historically hand-emitted only its own token
drifts and NEVER invoked ``entry.judges`` — so a board that declared a
process judge produced zero ``custom:<name>`` counts and loop-health flagged
the judge "never fired" even though the harness never gave it a chance to.

These tests drive the REAL harness session (no live LLM: the judge endpoint
is a scripted callable) for both a ``single_turn`` and a
``multi_turn_emulated`` entry, then reduce the emitted events through the
REAL reducer and assert:

1. a declared judge that finds a violation produces a ``custom:<name>``
   :class:`~zicato.core.types.DriftCount` on the run's loss — i.e. it was
   actually invoked (the fix);
2. a declared judge that finds nothing produces NO ``custom:<name>`` count —
   the harness distinguishes "ran and passed" from "never invoked"; and
3. a judge-free entry emits a byte-identical event stream (the convergence
   oracle is unaffected by the additive judge step).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("goldfive")

import goldfive  # noqa: E402

from zicato.board.judges import Judge  # noqa: E402
from zicato.core import BoardEntry, RuntimeConfig, ScoringWeights  # noqa: E402
from zicato.telemetry.reducer import reduce_loss, split_judge_attributed_kind  # noqa: E402
from zicato_examples.target_0_convergence.harness import (  # noqa: E402
    DeterministicPolicyAdapter,
)

# ---------------------------------------------------------------------------
# Scripted endpoints (module-level, distinct callables — no live LLM)
# ---------------------------------------------------------------------------


async def _target_llm(system: str, user: str, model: str) -> str:  # pragma: no cover - unused
    """The deterministic harness never calls an LLM; present for construction."""
    return ""


async def _aux_llm(system: str, user: str, model: str) -> str:  # pragma: no cover - unused
    return ""


async def _judge_says_violation(system: str, user: str, model: str) -> str:
    """A scripted judge endpoint that always reports a violation."""
    return "VIOLATION: the criterion was breached"


async def _judge_says_ok(system: str, user: str, model: str) -> str:
    """A scripted judge endpoint that always reports no violation."""
    return "OK"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_policy(root: Path, tokens: str = "") -> Path:
    """Write a minimal ``agent/policy.py`` under a fresh generation root."""
    gen_root = root / "snap"
    (gen_root / "agent").mkdir(parents=True, exist_ok=True)
    (gen_root / "agent" / "policy.py").write_text(f"STYLE_RULES = {tokens!r}\n", encoding="utf-8")
    return gen_root


def _config(workspace: Path, judge_llm) -> RuntimeConfig:
    return RuntimeConfig(
        instance_id="test",
        workspace_root=workspace,
        target_call_llm=_target_llm,
        evaluation_call_llm=_aux_llm,
        judge_call_llm=judge_llm,
    )


def _single_turn_entry(*, judges=()) -> BoardEntry:
    return BoardEntry(
        id="conv_judge",
        kind="single_turn",
        wall_clock_budget_seconds=60,
        input="summarise this",
        judges=judges,
    )


def _emulated_entry(*, judges=()) -> BoardEntry:
    return BoardEntry(
        id="conv_emulated",
        kind="multi_turn_emulated",
        wall_clock_budget_seconds=60,
        user_persona="a picky reviewer",
        max_turns=2,
        judges=judges,
    )


def _drive_and_reduce(entry: BoardEntry, config: RuntimeConfig, tmp_path: Path) -> object:
    """Run one entry through the real harness session; reduce the events."""
    import goldfive  # noqa: PLC0415

    gen_root = _seed_policy(tmp_path)
    events_path = tmp_path / "events.jsonl"
    adapter = DeterministicPolicyAdapter()
    session = adapter.load(gen_root)

    async def _run() -> object:
        sink = goldfive.JSONLPersistenceSink(events_path, mode="write")
        result = await session.run(entry, sinks=[sink], config=config)
        await sink.close()
        return result

    result = asyncio.run(_run())
    return reduce_loss(
        events_path,
        entry,
        "v1",
        "e0",
        None,
        int(getattr(result, "runtime_ms", 1)),
        False,
        weights=ScoringWeights(),
        final_output=str(getattr(result, "final_output", "") or ""),
    )


def _custom_judge_names(loss: object) -> set[str]:
    names: set[str] = set()
    for count in getattr(loss, "drift_counts", ()):  # type: ignore[attr-defined]
        is_custom, judge_name = split_judge_attributed_kind(count.kind)
        if is_custom and judge_name:
            names.add(judge_name)
    return names


# ---------------------------------------------------------------------------
# Tests — invocation proof
# ---------------------------------------------------------------------------


def test_single_turn_declared_judge_fires_and_emits_custom_count(tmp_path: Path) -> None:
    """A declared inline judge that violates yields a ``custom:<name>`` count."""
    judge = Judge.custom(
        "audience_appropriate", "keep it accessible", severity=goldfive.DriftSeverity.WARNING
    )
    entry = _single_turn_entry(judges=(judge,))
    loss = _drive_and_reduce(entry, _config(tmp_path, _judge_says_violation), tmp_path)

    assert "audience_appropriate" in _custom_judge_names(
        loss
    ), "the harness must invoke the declared judge and attribute its custom drift"


def test_multi_turn_emulated_declared_judge_fires_and_emits_custom_count(
    tmp_path: Path,
) -> None:
    """Invocation also fires for a ``multi_turn_emulated`` entry kind."""
    judge = Judge.custom(
        "no_fabricated_numbers", "never invent metrics", severity=goldfive.DriftSeverity.CRITICAL
    )
    entry = _emulated_entry(judges=(judge,))
    loss = _drive_and_reduce(entry, _config(tmp_path, _judge_says_violation), tmp_path)

    assert "no_fabricated_numbers" in _custom_judge_names(loss)


def test_declared_judge_that_passes_emits_no_custom_count(tmp_path: Path) -> None:
    """A judge that ran and found nothing must NOT produce a custom count.

    This is the "ran and passed" case the operator must be able to
    distinguish from "never invoked" — here the judge IS invoked (the
    scripted endpoint answers ``OK``) so no ``custom:<name>`` drift is minted.
    """
    judge = Judge.custom(
        "audience_appropriate", "keep it accessible", severity=goldfive.DriftSeverity.WARNING
    )
    entry = _single_turn_entry(judges=(judge,))
    loss = _drive_and_reduce(entry, _config(tmp_path, _judge_says_ok), tmp_path)

    assert _custom_judge_names(loss) == set()


def test_judge_free_entry_emits_no_custom_counts(tmp_path: Path) -> None:
    """A judge-free entry (the oracle's shape) mints no custom drift at all."""
    entry = _single_turn_entry(judges=())
    loss = _drive_and_reduce(entry, _config(tmp_path, _judge_says_violation), tmp_path)

    assert _custom_judge_names(loss) == set()
