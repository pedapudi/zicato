"""Triage pins for issue #125 — no post-promotion hook for out-of-tree side effects.

A target whose evolved state lives outside the mutable tree (a database, a
cache, a served artifact, a remote config) has nowhere to fold a promoted
generation into its long-lived store. Today the promotion tail is closed:

* gauntlet — ``zicato.evolve.persist._finalize_generation:95`` advances the
  champion marker under ``advance_current_generation``;
* multi-challenger — ``zicato.orchestrator:2626`` advances it inline, after the
  crowning invariant and before the journal loop.

Both are private, both are the whole extension surface, and the adapter (which
IS in scope at both sites — ``_evolve_multi_challenger(adapter=...)``) is never
consulted. ``zicato.adapters.base.HarnessAdapter`` declares ``mutable_subpaths``
/ ``load`` / ``mutation_points`` and nothing lifecycle-shaped.

This is FEATURE-shaped: the pins below encode the two properties the issue asks
for as CONTRACT, not the mechanism. Where the hook is invoked, and whether an
adapter method or a contract-declared command is the right carrier, was an open
adjudication when these were written; it resolved to an optional adapter
coroutine fired from both promote seams, with the round's loop-health report
carrying the observability the second pin asks for. See each test's docstring,
and ``tests/test_on_promote_hook.py`` for the behavioural coverage.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

from zicato.adapters.base import OPTIONAL_ADAPTER_MEMBERS, HarnessAdapter
from zicato.core.epoch import Generation


class _MinimalAdapter:
    """A structurally-conforming adapter that declares no lifecycle hook.

    Deliberately the pre-#125 shape: it must keep satisfying the Protocol
    after the hook lands, which is what makes the hook optional.
    """

    name = "minimal"
    run_output_names: tuple[str, ...] = ()

    def mutable_subpaths(self, generation_root: Path) -> list[Path]:
        return [generation_root]

    def load(self, generation_root: Path) -> object:
        raise NotImplementedError

    def mutation_points(self, source_roots: list[Path] | None = None) -> list[object]:
        return []


def test_an_adapter_without_the_hook_still_satisfies_the_protocol() -> None:
    """Backwards-compat guard: the hook must be OPTIONAL when it lands.

    Every shipped adapter and every operator-authored one predates #125. If
    ``on_promote`` becomes a required Protocol member, each of them stops
    type-checking and stops passing the runner's ``runtime_checkable`` gate.
    """
    assert isinstance(_MinimalAdapter(), HarnessAdapter)


def test_adapter_protocol_declares_a_post_promotion_hook() -> None:
    """A promoted generation must be able to reach state outside the tree.

    RESOLVED (adjudication: adapter method, not a contract-declared shell
    command — the latter would need its own security round to justify a
    contract file naming an executable). The carrier is the optional
    coroutine ``HarnessAdapter.on_promote``, fired from both promote seams
    by :func:`zicato.evolve.promote_hook.fire_on_promote`.

    Asserted off the class body rather than ``__protocol_attrs__``: that
    attribute is a 3.12 addition (3.11 computes the same set on the fly
    inside ``typing``), and this project supports 3.11. The class body is
    the declaration the contract is actually about, and it also pins the
    two properties ``__protocol_attrs__`` cannot express — that the hook
    is awaitable, and that it is optional.
    """
    hook = HarnessAdapter.__dict__.get("on_promote")
    assert hook is not None, "HarnessAdapter must declare an on_promote member"
    assert inspect.iscoroutinefunction(hook)
    assert "on_promote" in OPTIONAL_ADAPTER_MEMBERS


def test_promotion_side_effects_are_observable() -> None:
    """Whether a promotion was applied must be answerable from the run record.

    ADJUDICATED-DROP of this pin's original carrier. It was written to
    assert a tri-state ``Generation.committed`` field, on the reasoning
    that a promoted generation whose hook has not run is a distinguishable
    state from one whose hook succeeded — but the pin's *intent* is
    "promotion side effects are observable", and a lineage schema change
    is a heavier answer than that intent needs. A tri-state field would
    also be honest only while the loop is running: the hook fires once, at
    the transition, so a persisted ``committed=None`` could never be
    resolved after the fact by anything zicato owns.

    The observability the intent asks for is delivered instead by the
    round's loop-health report, which is where every other
    round-scoped-but-not-lineage-shaped fact already lands: a failed hook
    raises an ``on_promote_hook_failed`` WARNING naming the adapter, the
    generation, and the exception, alongside an ERROR log carrying the
    traceback. So this pin now asserts the finding exists rather than the
    field.
    """
    from zicato.health.diagnostics import detect_on_promote_hook_failed

    assert {f.name for f in dataclasses.fields(Generation)} >= {"id", "promoted"}
    (finding,) = detect_on_promote_hook_failed(("mystore", "v4", "ConnectionError"))
    assert finding.code == "on_promote_hook_failed"
    assert finding.severity == "warning"
    assert {"mystore", "v4", "ConnectionError"} <= set(finding.detail.values())
