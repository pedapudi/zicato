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
adapter method or a contract-declared command is the right carrier, is an open
adjudication (see the triage report) — deliberately not pinned here.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from zicato.adapters.base import HarnessAdapter
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


@pytest.mark.xfail(
    strict=True,
    reason="issue #125: no post-promotion extension point exists on the "
    "adapter Protocol (ADJUDICATION: adapter method vs contract-declared command)",
)
def test_adapter_protocol_declares_a_post_promotion_hook() -> None:
    """A promoted generation must be able to reach state outside the tree.

    The issue's proposed carrier is an optional adapter coroutine:

        async def on_promote(self, *, generation_id: str,
                             snapshot_path: Path, epoch_id: str) -> None: ...
    """
    assert "on_promote" in HarnessAdapter.__protocol_attrs__


@pytest.mark.xfail(
    strict=True,
    reason="issue #125: the lineage record has no field answering "
    "'did the promotion actually get applied?'",
)
def test_lineage_records_whether_the_promotion_was_committed() -> None:
    """Whether a promotion was applied must be answerable from the run record.

    Without it, the answer lives only in the target's own bookkeeping — which
    is exactly the accounting the issue's polling workaround puts outside the
    loop. A promoted generation whose hook has not (yet) run is a
    distinguishable state from one whose hook succeeded, so the field must be
    tri-state-capable rather than a bare ``bool`` defaulting to ``False``
    (compare ``append_to_lineage(pending=...)``, which persists ``promoted``
    as ``null`` for the same reason).
    """
    fields = {f.name for f in dataclasses.fields(Generation)}
    assert "committed" in fields
