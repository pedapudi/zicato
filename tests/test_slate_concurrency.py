"""WS-CONC — best-of-N slate concurrency proofs.

Three independent guarantees the gather rests on:

* **Determinism under out-of-order completion.** A scripted inner agent whose
  slots finish in the REVERSE of slot order (varying async delays) must
  produce a slate, event sequence, and chosen candidate byte-identical to the
  serial (``propose_parallelism=1``) reference — the deterministic post-gather
  pass restores slot order.
* **The wall-clock overlap is real.** ~100 ms-per-slot scripted slots take
  ≈ ``n × 100 ms`` serially and ≈ ``100 ms`` gathered.
* **Real per-slot scratch derives are disjoint + intact.** N concurrent
  ``GenerationStore.derive_scratch`` calls into disjoint scratch roots each
  materialise their OWN candidate's tree with no cross-contamination and no
  torn trees — and none leaks into the generation namespace.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import zicato_examples.target_0_convergence as _t0_pkg
from zicato.core.types import Experiment, HypothesisSpec, Patch, ProposerQualityConfig
from zicato.proposer.agent import ProposerContext
from zicato.proposer.best_of_n import BestOfNProposerAgent
from zicato.proposer.hints import EDIT_CLASS_HINTS

EXAMPLE_DIR = Path(_t0_pkg.__file__).resolve().parent
AGENT_DIR = EXAMPLE_DIR / "agent"


# --------------------------------------------------------------------------
# Wrapper-level scripted doubles (no genstore — the gather ordering + timing
# are wrapper concerns, orthogonal to the real derive).
# --------------------------------------------------------------------------


def _experiment(slot: int, content: str) -> Experiment:
    return Experiment(
        id=f"exp_slot_{slot}",
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-07-12T00:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea=f"slot {slot}",
            modulating=("style_rules",),
            why="because",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.05",
        ),
        patches=(
            Patch(
                id=f"p{slot}",
                mutation_id="style_rules",
                op="replace",
                new_content=content,
                new_numeric=None,
                new_enum=None,
                rationale="r",
            ),
        ),
        outcome=None,
    )


def _slot_of_hint(sample_hint: str) -> int:
    """Recover a slot index from the per-slot hint the wrapper stamps.

    With an empty failure profile ``hint_for_slot`` is the pure
    ``EDIT_CLASS_HINTS`` rotation, so each of the three slots carries a
    distinct hint prefix — the scripted agent keys off it so its output is a
    function of the SLOT, never of call order.
    """
    for i, hint in enumerate(EDIT_CLASS_HINTS):
        if sample_hint.startswith(hint):
            return i
    raise AssertionError(f"unrecognised sample hint: {sample_hint!r}")


class _HintKeyedDelayedAgent:
    """Inner agent whose per-slot output + delay are keyed by the slot hint.

    ``delays[slot]`` seconds of ``asyncio.sleep`` before returning
    ``_experiment(slot, contents[slot])``, so the completion ORDER is chosen
    by the test independently of slot order. Records the completion order so a
    test can assert the slots really did finish out of order.
    """

    def __init__(self, contents: list[str], delays: list[float]) -> None:
        self._contents = contents
        self._delays = delays
        self.completion_order: list[int] = []

    async def propose(self, ctx: ProposerContext) -> Experiment:
        slot = _slot_of_hint(ctx.sample_hint)
        await asyncio.sleep(self._delays[slot])
        self.completion_order.append(slot)
        return _experiment(slot, self._contents[slot])


class _FixedCritic:
    """Critic double returning a fixed index; records nothing else."""

    def __init__(self, choice: str) -> None:
        self._choice = choice

    async def __call__(self, system: str, user: str, model: str) -> str:
        return self._choice


def _ctx(aux: object, events: list[tuple[str, dict]]) -> ProposerContext:
    return ProposerContext(
        epoch_id="e1",
        parent_generation_id="v0",
        new_generation_id="v1",
        patterns=(),
        mutations=(),
        brief_text="# brief\n",
        current_loss_summary="loss=2.3",
        aux_call_llm=aux,  # type: ignore[arg-type]
        model="test-model",
        # Empty profile ⇒ the distinct EDIT_CLASS_HINTS rotation per slot.
        failure_profile="",
        round_event_emitter=lambda t, f: events.append((t, dict(f))),
        # No scratch factory + no validate hook: the gather-ordering surface
        # is independent of the real derive, which the stress test covers.
        validate_experiment=None,
        scratch_validator_factory=None,
    )


# --------------------------------------------------------------------------
# Proof 1 — determinism under out-of-order completion
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_order_slate_is_byte_identical_to_serial() -> None:
    contents = ["verbose-prose", "verbose-prose; skip-citations", "decoy; fabricate-metrics"]

    # Serial reference (propose_parallelism=1): slots complete in slot order.
    serial_events: list[tuple[str, dict]] = []
    serial_inner = _HintKeyedDelayedAgent(contents, [0.0, 0.0, 0.0])
    serial_agent = BestOfNProposerAgent(
        inner=serial_inner,
        config=ProposerQualityConfig(best_of_n=3),
        propose_parallelism=1,
    )
    serial_out = await serial_agent.propose(_ctx(_FixedCritic("0"), serial_events))

    # Concurrent gather with delays chosen so slot 2 finishes FIRST and slot 0
    # LAST — the reverse of slot order.
    conc_events: list[tuple[str, dict]] = []
    conc_inner = _HintKeyedDelayedAgent(contents, [0.05, 0.03, 0.01])
    conc_agent = BestOfNProposerAgent(
        inner=conc_inner,
        config=ProposerQualityConfig(best_of_n=3),
        propose_parallelism=4,
    )
    conc_out = await conc_agent.propose(_ctx(_FixedCritic("0"), conc_events))

    # The slots really did finish out of order under the gather...
    assert conc_inner.completion_order == [2, 1, 0]
    assert serial_inner.completion_order == [0, 1, 2]
    # ...yet the emitted event sequence is byte-identical (slot order restored
    # by the deterministic post-gather pass)...
    assert conc_events == serial_events
    assert [t for t, _ in conc_events] == [
        "candidate_sampled",
        "candidate_sampled",
        "candidate_sampled",
        "critique_selected",
    ]
    assert [f["i"] for t, f in conc_events if t == "candidate_sampled"] == [0, 1, 2]
    # ...and the chosen candidate is the same (slot 0, the critic's pick).
    assert conc_out == serial_out
    assert conc_out.patches[0].new_content == "verbose-prose"


# --------------------------------------------------------------------------
# Proof 2 — the wall-clock overlap is real
# --------------------------------------------------------------------------


class _SleepAgent:
    """Inner agent that sleeps a fixed time per slot then returns a candidate."""

    def __init__(self, per_slot_s: float) -> None:
        self._per_slot_s = per_slot_s

    async def propose(self, ctx: ProposerContext) -> Experiment:
        slot = _slot_of_hint(ctx.sample_hint)
        await asyncio.sleep(self._per_slot_s)
        return _experiment(slot, f"content-{slot}")


async def _time_slate(parallelism: int, per_slot_s: float) -> float:
    inner = _SleepAgent(per_slot_s)
    # critique_enabled=False ⇒ deterministic heuristic selection, NO aux call,
    # so only the sampling time is measured.
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=3, critique_enabled=False),
        propose_parallelism=parallelism,
    )
    events: list[tuple[str, dict]] = []
    t0 = time.monotonic()
    await agent.propose(_ctx(_FixedCritic("0"), events))
    return time.monotonic() - t0


@pytest.mark.asyncio
async def test_gathered_slate_overlaps_the_per_slot_wait() -> None:
    per_slot = 0.1
    n = 3
    serial = await _time_slate(1, per_slot)
    gathered = await _time_slate(n, per_slot)
    # Serial pays the full n × 100 ms; the gather overlaps to ≈ one slot.
    assert serial >= (n - 0.5) * per_slot, f"serial too fast: {serial:.3f}s"
    assert gathered < 1.8 * per_slot, f"gather did not overlap: {gathered:.3f}s"
    assert gathered < serial / 2, f"gather ({gathered:.3f}s) not faster than serial ({serial:.3f}s)"


# --------------------------------------------------------------------------
# Proof 3 — real concurrent derive_scratch is disjoint + intact
# --------------------------------------------------------------------------


def _style_patch(content: str) -> Patch:
    return Patch(
        id="p",
        mutation_id="style_rules",
        op="replace",
        new_content=content,
        new_numeric=None,
        new_enum=None,
        rationale="r",
    )


def _policy_style_line(tree_root: Path) -> str:
    policy = (tree_root / "agent" / "policy.py").read_text()
    lines = [ln for ln in policy.splitlines() if ln.startswith("STYLE_RULES")]
    assert len(lines) == 1, f"expected one STYLE_RULES line, got {lines!r}"
    return lines[0]


@pytest.mark.parametrize("backend", ["git", "directory"])
def test_concurrent_derive_scratch_is_disjoint_and_intact(tmp_path: Path, backend: str) -> None:
    """N threads deriving distinct patches into disjoint scratch roots each
    get their OWN complete tree — no torn trees, no cross-contamination, and
    nothing enters the generation namespace."""
    from zicato.epoch.genstore import DirectoryGenerationStore
    from zicato.epoch.git_genstore import GitGenerationStore

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    store: object
    if backend == "git":
        store = GitGenerationStore(workspace)
    else:
        store = DirectoryGenerationStore(workspace)

    epoch_id = "e1"
    store.seed_generation(epoch_id, "v0", [AGENT_DIR])  # type: ignore[attr-defined]
    # Pre-warm the parent tree once (git materialises the parent worktree),
    # exactly as the round's scratch-validator factory does before the gather.
    store.snapshot_root(epoch_id, "v0")  # type: ignore[attr-defined]

    contents = [f"verbose-prose; token-{i}" for i in range(8)]
    scratch_roots = [tmp_path / f"scratch-{i}" / "child" for i in range(len(contents))]

    def _derive(i: int) -> Path:
        return store.derive_scratch(  # type: ignore[attr-defined]
            epoch_id=epoch_id,
            parent_generation_id="v0",
            patches=[_style_patch(contents[i])],
            scratch_root=scratch_roots[i],
        )

    with ThreadPoolExecutor(max_workers=len(contents)) as pool:
        results = list(pool.map(_derive, range(len(contents))))

    # Every scratch root is distinct.
    assert len({str(r.resolve()) for r in results}) == len(contents)
    # Each scratch tree carries EXACTLY its own content — no torn / merged tree.
    for i, root in enumerate(results):
        line = _policy_style_line(root)
        assert contents[i] in line, f"scratch {i} missing its own content"
        for j, other in enumerate(contents):
            if j != i:
                assert other not in line, f"scratch {i} contaminated by content {j}"

    # No scratch tree entered the generation namespace — only v0 exists.
    assert store.list_generations(epoch_id) == ["v0"]  # type: ignore[attr-defined]
    # The parent tree is untouched (still the seed, no defect tokens dropped).
    parent_line = _policy_style_line(store.snapshot_root(epoch_id, "v0"))  # type: ignore[attr-defined]
    for content in contents:
        assert content not in parent_line
