"""Triage pins for the stale-anchor crash (issue #83).

Every "bad patch set" condition on the CHECKED
:func:`zicato.mutation.applier.apply_patches` surface must signal
``ValueError`` — one logical condition, one exception type — so a
hallucinated / stale anchor rejects ONE candidate instead of aborting the
whole evolve run. The generation-level transaction boundary in
:mod:`zicato.evolve.round` then degrades it to a retryable finding.

Adjudication note (triage, 2026-07-29): the SIMPLE hallucinated-id case
reported in #83 IS fixed on current main — :func:`apply_patches` runs
:func:`~zicato.mutation.validator.validate_patches` up front and raises
``ValueError`` (``test_apply_unresolved_id_raises`` pins that). What
remains live is #83's SECOND scenario: a batch where an earlier patch
removes the anchor a later patch resolves against. The pre-check passes
(both ids resolve in the freshly-copied tree), and the sequential apply's
re-enumeration then drops the later id — reaching the ``KeyError`` sites in
``_apply_patches_into_tree`` that the transaction boundary does not catch.

Both tests below pinned that gap as ``xfail(strict=True)`` during triage;
the fix landed (the checked ``apply_patches`` surface converts every
missing-anchor site to ``ValueError``, and the round-level boundary catches
``ValueError`` and ``KeyError`` alike), so they are now plain pins.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from zicato.core.types import Patch
from zicato.mutation.applier import apply_patches


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _patch(*, pid: str, mutation_id: str, new_content: str) -> Patch:
    return Patch(
        id=pid,
        mutation_id=mutation_id,
        op="replace",
        new_content=new_content,
        new_numeric=None,
        new_enum=None,
        rationale="triage probe",
    )


def test_batch_erased_anchor_raises_value_error(tmp_path: Path) -> None:
    """A patch whose anchor an EARLIER patch erased is a bad patch set.

    The file-kind point ``whole`` and the span point ``instr`` both resolve
    in the freshly-copied tree, so ``validate_patches`` passes the batch.
    Applying ``p1`` rewrites the whole file without the ``instr`` marker, so
    the re-enumeration between patches drops ``instr`` and ``p2``'s lookup
    misses. That is a rejectable patch set, not a crash — it must raise
    ``ValueError`` like every other apply-time rejection.
    """
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    _write(
        src / "prompts.py",
        '''
        # zicato:mutable:file id="whole"
        # zicato:mutable id="instr"
        INSTR = """original"""
        ''',
    )
    patches = [
        _patch(pid="p1", mutation_id="whole", new_content='X = "no markers here"\n'),
        _patch(pid="p2", mutation_id="instr", new_content='"""rewritten"""'),
    ]
    with pytest.raises(ValueError):
        apply_patches(src, patches, tgt)


@pytest.mark.asyncio
async def test_transaction_boundary_surfaces_key_error_as_a_finding(tmp_path: Path) -> None:
    """The transaction boundary degrades ANY bad-patch-set signal to a finding.

    Defence in depth for the applier fix above: even if some apply site keeps
    raising ``KeyError``, the round-level boundary must turn it into a
    retryable rejected-patch finding rather than let it abort the round.
    """
    from zicato.core.types import Experiment, HypothesisSpec
    from zicato.evolve.round import build_post_apply_validator

    class _KeyErrorGenstore:
        def derive_generation(self, **_kwargs: Any) -> Path:
            raise KeyError("Patch 'p2': mutation_id 'ghost' not found in target_root")

    validate = build_post_apply_validator(
        genstore=_KeyErrorGenstore(),
        epoch_id="e1",
        parent_id="v0",
        next_id="v1",
        mutations=[],
        beater=None,
        round_index=0,
        last_child_snapshot={},
    )
    candidate = Experiment(
        id="exp_e1_v1",
        epoch_id="e1",
        generation_id="v1",
        parent_generation_id="v0",
        proposed_at="2026-07-29T00:00:00Z",
        hypothesis=HypothesisSpec(
            core_idea="triage probe",
            modulating=("instr",),
            why="because",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.00",
        ),
        patches=(),
        outcome=None,
    )
    findings = await validate(candidate)
    assert findings, "a bad patch set must surface as a finding, not propagate"
    assert any("patch set" in f for f in findings)
