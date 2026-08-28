"""WS-CONC — best-of-N slate concurrency proofs.

Three independent guarantees the gather rests on:

* **Determinism under out-of-order completion.** A scripted inner agent whose
  slots finish in the REVERSE of slot order must produce a slate, event
  sequence, and chosen candidate byte-identical to the serial
  (``propose_parallelism=1``) reference — the deterministic post-gather pass
  restores slot order. The reversal is forced by an ``asyncio.Event`` chain
  (:class:`_CompletionGate`), not by per-slot sleeps: sleeps made the
  assertion timing-dependent and it flaked under saturated xdist (issue #103).
* **The overlap is real.** All N slate slots are genuinely in flight AT
  ONCE, proven structurally by a rendezvous every slot must reach together
  (:class:`_RendezvousAgent`) rather than by a wall-clock margin — a slate
  that silently serialised deadlocks the rendezvous instead of just running
  slower, so this cannot flake under a saturated CI runner the way a timing
  ratio can (issue #103's discipline applied to the overlap proof too).
* **Real per-slot scratch derives are disjoint + intact.** N concurrent
  ``GenerationStore.derive_scratch`` calls into disjoint scratch roots each
  materialise their OWN candidate's tree with no cross-contamination and no
  torn trees — and none leaks into the generation namespace.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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


#: Ceiling on a gated slate propose. The event chain below completes in
#: microseconds when the gather is genuinely concurrent, so anything near
#: this bound means the slots serialised and the chain deadlocked.
_GATED_SLATE_TIMEOUT_S = 20.0


class _CompletionGate:
    """Event chain that pins slate-slot COMPLETION ORDER without any sleeps.

    Forcing out-of-order completion by giving slot ``s`` a shorter
    ``asyncio.sleep`` than slot ``s-1`` is timing-dependent, and the margin
    was lost under a saturated ``pytest -n`` (issue #103): ``_run_one_slot``
    runs a SYNCHRONOUS prelude (``ctx.scratch_validator_factory()`` →
    ``tempfile.mkdtemp``) inside each slot coroutine BEFORE the inner
    ``propose`` awaits, so slot ``s``'s clock starts after every earlier
    slot's prelude. Prelude jitter above the inter-slot delta inverts the
    order and the assertion flakes.

    This gate removes the clock entirely. With ``reverse=True`` slot ``s``
    waits for slot ``s+1``'s event before recording, so the highest slot
    always completes FIRST and the order is exactly ``[n-1, …, 1, 0]`` no
    matter how the preludes interleave. It is also a STRICTLY STRONGER
    probe: if the slate ever silently serialises in slot order, slot 0
    waits forever on a gate no one will set, so the chain DEADLOCKS instead
    of quietly passing — which is why every gated propose is wrapped in
    :data:`_GATED_SLATE_TIMEOUT_S` to fail fast with a clear message.

    Requires the slate's ``propose_parallelism`` to be ``>= n`` (all slots
    in flight at once). ``reverse=False`` records the natural arrival order
    and never waits — the serial (``propose_parallelism=1``) reference.
    """

    def __init__(self, n: int, *, reverse: bool) -> None:
        self._gates = [asyncio.Event() for _ in range(n)]
        self._reverse = reverse
        self.completion_order: list[int] = []

    async def record(self, slot: int) -> None:
        if self._reverse:
            nxt = slot + 1
            if nxt < len(self._gates):
                await self._gates[nxt].wait()
        self.completion_order.append(slot)
        self._gates[slot].set()


async def _gated_propose(agent: BestOfNProposerAgent, ctx: ProposerContext) -> Experiment:
    """Run an event-gated slate propose under a hard ceiling.

    A timeout here is a diagnosis, not a slow machine: the gates resolve in
    microseconds once the gather genuinely overlaps, so exhausting
    :data:`_GATED_SLATE_TIMEOUT_S` means the event chain deadlocked — the
    slots ran serially instead of concurrently (or one died before signalling
    its gate) and at least one slot is waiting on an event nobody will ever
    set. Shared by every event-gated proof in this module — the reverse-order
    completion gate (:class:`_CompletionGate`) and the rendezvous overlap
    proof (:class:`_RendezvousAgent`) alike.
    """
    try:
        async with asyncio.timeout(_GATED_SLATE_TIMEOUT_S):
            return await agent.propose(ctx)
    except TimeoutError:
        pytest.fail(
            f"gated slate propose did not finish within {_GATED_SLATE_TIMEOUT_S}s: "
            "the event chain deadlocked, so the slate slots did NOT run "
            "concurrently (serialisation regression)"
        )


class _HintKeyedDelayedAgent:
    """Inner agent whose per-slot output is keyed by the slot hint.

    Returns ``_experiment(slot, contents[slot])`` after recording through a
    :class:`_CompletionGate`, so the completion ORDER is chosen by the test
    independently of slot order — and independently of wall-clock timing.
    ``reverse=True`` forces the exact reverse of slot order.
    """

    def __init__(self, contents: list[str], *, reverse: bool) -> None:
        self._contents = contents
        self._gate = _CompletionGate(len(contents), reverse=reverse)

    @property
    def completion_order(self) -> list[int]:
        return self._gate.completion_order

    async def propose(self, ctx: ProposerContext) -> Experiment:
        slot = _slot_of_hint(ctx.sample_hint)
        await self._gate.record(slot)
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
    serial_inner = _HintKeyedDelayedAgent(contents, reverse=False)
    serial_agent = BestOfNProposerAgent(
        inner=serial_inner,
        config=ProposerQualityConfig(best_of_n=3),
        propose_parallelism=1,
    )
    serial_out = await serial_agent.propose(_ctx(_FixedCritic("0"), serial_events))

    # Concurrent gather, event-gated so slot 2 finishes FIRST and slot 0 LAST —
    # the exact reverse of slot order, with no timing dependence at all. A
    # slate that serialised would deadlock here, not pass: hence the timeout.
    conc_events: list[tuple[str, dict]] = []
    conc_inner = _HintKeyedDelayedAgent(contents, reverse=True)
    conc_agent = BestOfNProposerAgent(
        inner=conc_inner,
        config=ProposerQualityConfig(best_of_n=3),
        propose_parallelism=4,
    )
    conc_out = await _gated_propose(conc_agent, _ctx(_FixedCritic("0"), conc_events))

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
# Proof 2 — the gathered execution genuinely overlaps
# --------------------------------------------------------------------------


class _RendezvousAgent:
    """Inner agent proving genuine N-way overlap structurally — no sleeps.

    Each slot records its arrival (bumping the shared in-flight counter and
    the observed peak), then waits for EVERY slot to have arrived before
    returning. All N slots can only complete if all N were in flight AT
    ONCE: a slate that silently serialised would deadlock here — slot 0
    can never observe slot 1's arrival, because slot 1 cannot even START
    until slot 0 already returned — rather than merely finishing slower.
    That is the same "wrong behaviour deadlocks, not just runs oddly"
    discipline :class:`_CompletionGate` uses above (issue #103: a
    wall-clock margin can flake under a saturated ``pytest -n`` with no
    real regression; a deadlock cannot).
    """

    def __init__(self, n: int) -> None:
        self._events = [asyncio.Event() for _ in range(n)]
        self.peak_in_flight = 0
        self._in_flight = 0

    async def propose(self, ctx: ProposerContext) -> Experiment:
        slot = _slot_of_hint(ctx.sample_hint)
        self._in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
        self._events[slot].set()
        for event in self._events:
            await event.wait()
        self._in_flight -= 1
        return _experiment(slot, f"content-{slot}")


@pytest.mark.asyncio
async def test_gathered_slate_overlaps_the_per_slot_wait() -> None:
    """The gathered slate is genuinely concurrent: all N slots are in flight
    AT ONCE, proven structurally (a rendezvous every slot must reach
    together) instead of by a wall-clock ratio — the flake class issue
    #103 eliminated elsewhere in this file (``gathered < 1.8 * per_slot``
    could flake under a saturated CI runner with no real serialisation
    regression). A slate that silently serialised would DEADLOCK the
    rendezvous rather than just finish slowly — a strictly stronger probe
    than the margin it replaces — so the gated propose is wrapped in the
    same hard ceiling as the reverse-order proof above, to fail fast with
    a clear diagnosis instead of hanging.
    """
    n = 3
    tracker = _RendezvousAgent(n)
    agent = BestOfNProposerAgent(
        inner=tracker,
        config=ProposerQualityConfig(best_of_n=n, critique_enabled=False),
        propose_parallelism=n,
    )
    events: list[tuple[str, dict]] = []
    await _gated_propose(agent, _ctx(_FixedCritic("0"), events))
    assert (
        tracker.peak_in_flight == n
    ), f"gather did not overlap: peak concurrency was {tracker.peak_in_flight}, expected {n}"


# --------------------------------------------------------------------------
# Proof 2b — no scratch lease is still open when the slate raises
# --------------------------------------------------------------------------


#: Event-loop turns to pump before concluding the slate is genuinely parked
#: on its sibling. Turns, not seconds — a saturated runner adds wall-clock
#: time but never extra turns, so this cannot flake the way a sleep would.
#: The pre-fix gather surfaces its exception on turn 2.
_SETTLE_TURNS = 50


class _LeaseLedger:
    """A ``scratch_validator_factory`` that counts OPEN per-slot leases.

    Stands in for the real per-slot scratch tree: ``open_count`` is how many
    slots currently hold a scratch parent on disk, i.e. exactly the residue
    the WS-CONC leak was about.
    """

    def __init__(self) -> None:
        self.open_count = 0
        self.leased = 0

    def __call__(self) -> tuple[object, object]:
        self.open_count += 1
        self.leased += 1

        def _cleanup() -> None:
            self.open_count -= 1

        async def _validate(_experiment: Experiment) -> list[str]:
            return []

        return _validate, _cleanup


class _BoomAndHoldAgent:
    """Slot 0 raises an UNEXPECTED exception; slot 1 keeps its lease open.

    ``_sample_slot`` folds a :class:`ProposerError` into a normal
    ``_SlotOutcome``, so the only thing that reaches the gather is an
    exception the slate never anticipated — a bug in the inner proposer, a
    cleanup failure, an ``asyncio`` error. That is the path this rig drives.
    Slot 1 parks on ``release`` (held by the test) so it is guaranteed to
    still be mid-flight, holding its scratch lease, at the instant slot 0
    blows up.
    """

    def __init__(self) -> None:
        self.boom_raised = asyncio.Event()
        self.slot1_parked = asyncio.Event()
        self.release = asyncio.Event()

    async def propose(self, ctx: ProposerContext) -> Experiment:
        slot = _slot_of_hint(ctx.sample_hint)
        if slot == 0:
            await self.slot1_parked.wait()
            self.boom_raised.set()
            raise RuntimeError("slot 0 exploded")
        self.slot1_parked.set()
        await self.release.wait()
        return _experiment(slot, f"content-{slot}")


@pytest.mark.asyncio
async def test_slate_raise_waits_for_every_sibling_lease_to_be_released() -> None:
    """The slate's exception must not outrun its siblings' scratch cleanup.

    This is the WS-CONC leak itself, and nothing else in this file covers
    it. Plain ``asyncio.gather()`` completes its outer future the instant
    ONE child raises, leaving the others running as orphans — so the caller
    regained control while a sibling's scratch parent was still on disk, and
    the round's residue sweep could see a lease that was about to be
    released by a task nobody was awaiting.

    The proof is structural, not timed. Slot 1 parks on an event only the
    TEST can set, so once slot 0 has raised the propose task can never
    complete while the fix holds — no number of event-loop turns will let
    it — whereas the pre-fix gather completes within a couple of turns,
    exception in hand and slot 1's lease still open. Pumping the loop a
    bounded number of turns and asserting the task never finished therefore
    separates the two behaviours without depending on any wall clock
    (measured: pre-fix finishes on turn 2).
    """
    inner = _BoomAndHoldAgent()
    ledger = _LeaseLedger()
    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=2, critique_enabled=False),
        propose_parallelism=2,
    )
    events: list[tuple[str, dict]] = []
    ctx = replace(
        _ctx(_FixedCritic("0"), events),
        scratch_validator_factory=ledger,  # type: ignore[arg-type]
    )

    task = asyncio.create_task(agent.propose(ctx))
    async with asyncio.timeout(_GATED_SLATE_TIMEOUT_S):
        await inner.boom_raised.wait()
    # Slot 0 has raised and slot 1 is parked holding its lease. Pump the loop:
    # while the fix holds the slate simply cannot finish here, so any turn on
    # which it does is the orphaned-sibling bug.
    for turn in range(_SETTLE_TURNS):
        await asyncio.sleep(0)
        assert not task.done(), (
            f"on event-loop turn {turn} the slate surfaced slot 0's failure while "
            f"slot 1 still held its scratch lease ({ledger.open_count} open) — the "
            "caller regained control with an orphaned slot writing to a live "
            "scratch tree"
        )

    inner.release.set()
    with pytest.raises(RuntimeError, match="slot 0 exploded"):
        async with asyncio.timeout(_GATED_SLATE_TIMEOUT_S):
            await task
    assert ledger.leased == 2, "both slots should have leased a scratch tree"
    assert ledger.open_count == 0, (
        f"{ledger.open_count} scratch lease(s) still open after the slate raised — "
        "the caller can observe a scratch parent that is still on disk"
    )


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
    store.materialize_snapshot(epoch_id, "v0")  # type: ignore[attr-defined]

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
    parent_line = _policy_style_line(store.materialize_snapshot(epoch_id, "v0"))  # type: ignore[attr-defined]
    for content in contents:
        assert content not in parent_line


# --------------------------------------------------------------------------
# Proof 4 — the COMPOSED end-to-end: real git derive + genuinely-yielding
# concurrency + the final mount, through the whole wrapper at p=4
# --------------------------------------------------------------------------


class _GitDeriveDelayedInner:
    """Inner proposer keyed by slot hint: completes inversely to slot order.

    Gated on a :class:`_CompletionGate` chain (``reverse=True``), so LOW slots
    finish LAST and the gather genuinely completes out of slot order — unlike
    the scripted doubles the tree-integrity goldens use, which never await and
    degrade to serial. Each slot returns a ``style_rules`` candidate carrying
    its OWN token, so the real per-slot ``derive_scratch`` produces a
    distinguishable tree.

    The chain replaced the previous ``asyncio.sleep(base * (n - slot))``
    ladder, whose 20 ms inter-slot margin was lost to ``_run_one_slot``'s
    synchronous ``mkdtemp`` prelude under a saturated ``pytest -n`` — the
    xdist flake in issue #103. See :class:`_CompletionGate`.
    """

    def __init__(self, contents: list[str]) -> None:
        self._contents = contents
        self._gate = _CompletionGate(len(contents), reverse=True)

    @property
    def completion_order(self) -> list[int]:
        return self._gate.completion_order

    async def propose(self, ctx: ProposerContext) -> Experiment:
        slot = _slot_of_hint(ctx.sample_hint)
        await self._gate.record(slot)
        return _experiment(slot, self._contents[slot])


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.asyncio
async def test_composed_real_git_slate_out_of_order_mounts_the_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole slate path, end-to-end, on the REAL git genstore at p=4.

    Threads the real ``build_scratch_validator_factory`` (per-slot scratch
    derive) AND ``build_post_apply_validator`` (the shared ``next_id`` mount)
    onto a ``BestOfNProposerAgent(propose_parallelism=4)``, with an inner that
    GENUINELY yields out of slot order. Asserts, together: the slots finish out
    of order, the ``candidate_sampled`` events are SLOT-ordered, the critic's
    chosen candidate is returned, the mounted ``next_id`` tree byte-matches the
    chosen candidate, and NO ``ztw-slate-*`` scratch residue is left behind.
    """
    import tempfile

    # Route the in-process slate scratch (``ztw-slate-*``) into a test-local
    # temp dir — the ``test_best_of_n_tree_integrity.py`` precedent — so the
    # residue check below cannot pick up an unrelated ``ztw-slate-*`` a
    # sibling xdist worker (or a concurrent CI job on a shared runner)
    # happens to still have open in the REAL system temp dir at the instant
    # this test snapshots it. ``tempfile.mkdtemp`` with no ``dir=`` resolves
    # through ``tempfile.gettempdir()``, which honours this module attribute
    # first, so every ``ztw-slate-*`` this test creates lands here instead.
    scratch_tmp = tmp_path / "tmp"
    scratch_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_tmp))

    from zicato.epoch.git_genstore import GitGenerationStore
    from zicato.evolve.round import (
        SLATE_SCRATCH_PREFIX,
        build_post_apply_validator,
        build_scratch_validator_factory,
    )
    from zicato.mutation.enumerator import enumerate_mutations

    workspace = tmp_path / ".zicato"
    workspace.mkdir()
    store = GitGenerationStore(workspace)
    store.seed_generation("e1", "v0", [AGENT_DIR])
    mutations = list(enumerate_mutations([store.materialize_snapshot("e1", "v0")]))

    n = 3
    contents = [f"verbose-prose; slot-{i}-token" for i in range(n)]
    inner = _GitDeriveDelayedInner(contents)

    last_child_snapshot: dict[str, Path] = {}
    validate = build_post_apply_validator(
        genstore=store,
        epoch_id="e1",
        parent_id="v0",
        next_id="v1",
        mutations=mutations,
        beater=None,
        round_index=0,
        last_child_snapshot=last_child_snapshot,
    )
    temp_root = Path(tempfile.gettempdir())
    slate_before = set(temp_root.glob(f"{SLATE_SCRATCH_PREFIX}*"))
    factory = build_scratch_validator_factory(
        genstore=store,
        epoch_id="e1",
        parent_id="v0",
        next_id="v1",
        mutations=mutations,
        beater=None,
        round_index=0,
    )

    events: list[tuple[str, dict]] = []
    base = _ctx(_FixedCritic("1"), events)
    ctx = replace(base, validate_experiment=validate, scratch_validator_factory=factory)

    agent = BestOfNProposerAgent(
        inner=inner,
        config=ProposerQualityConfig(best_of_n=n),
        propose_parallelism=4,
    )
    chosen = await _gated_propose(agent, ctx)

    # The slots really did finish out of slot order (low slots last)...
    assert inner.completion_order == [2, 1, 0]
    # ...yet ``candidate_sampled`` is emitted in SLOT order by the post-gather pass.
    assert [f["i"] for t, f in events if t == "candidate_sampled"] == [0, 1, 2]
    # The critic (index 1) picked slot 1 — that candidate is returned.
    assert chosen.patches[0].new_content == contents[1]

    # The mounted ``next_id`` (v1) tree byte-matches the chosen candidate: it is
    # a real generation and its scratch snapshot carries ONLY slot 1's token.
    assert store.has_generation("e1", "v1")
    mounted_line = _policy_style_line(Path(last_child_snapshot["path"]))
    assert contents[1] in mounted_line
    for j, other in enumerate(contents):
        if j != 1:
            assert other not in mounted_line, f"mounted tree contaminated by slot {j}"

    # Zero ``ztw-slate-*`` residue: every per-slot scratch parent was cleaned up
    # in its slot ``finally`` — no new one survives the propose.
    slate_after = set(temp_root.glob(f"{SLATE_SCRATCH_PREFIX}*"))
    assert slate_after - slate_before == set(), "slate scratch dirs leaked"


def test_slate_scratch_sweep_removes_only_stale_crash_leaks(tmp_path: Path) -> None:
    """The startup sweep reaps OLD ``ztw-slate-*`` leaks but spares fresh ones.

    A SIGKILL leak is an old ``ztw-slate-*`` dir; a live sibling orchestrator's
    slot is a FRESH one. The age gate must remove the former and never the
    latter (nor anything without the prefix). Runs against an injected temp
    root so it cannot touch the real temp dir.
    """
    import os
    import time
    from unittest import mock

    from zicato.evolve import round as round_mod
    from zicato.evolve.round import (
        _SLATE_SCRATCH_STALE_SECONDS,
        _sweep_stale_slate_scratch,
    )

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()

    stale = temp_root / "ztw-slate-crash-leak"
    stale.mkdir()
    (stale / "child").mkdir()
    old = time.time() - _SLATE_SCRATCH_STALE_SECONDS - 60
    os.utime(stale, (old, old))

    fresh = temp_root / "ztw-slate-live-sibling"  # a live slot's parent
    fresh.mkdir()
    unrelated = temp_root / "ztw-snap-some-run"  # different prefix, must survive
    unrelated.mkdir()

    with mock.patch.object(round_mod.tempfile, "gettempdir", return_value=str(temp_root)):
        _sweep_stale_slate_scratch()

    assert not stale.exists(), "the stale crash leak must be reaped"
    assert fresh.exists(), "a fresh (live) slate dir must be spared"
    assert unrelated.exists(), "a non-slate prefix must never be touched"
