"""Triage pins for the stale-anchor crash (issue #83).

The generation-level transaction boundary in :mod:`zicato.evolve.round`
catches ``ValueError`` only. Every "bad patch set" condition inside
:mod:`zicato.mutation.applier` must therefore signal ``ValueError`` so a
hallucinated / stale anchor rejects ONE candidate instead of aborting the
whole evolve run.

Adjudication note (triage, 2026-07-29): the SIMPLE hallucinated-id case
reported in #83 IS fixed on current main — :func:`apply_patches` runs
:func:`~zicato.mutation.validator.validate_patches` up front and raises
``ValueError`` (``test_apply_unresolved_id_raises`` pins that). What
remains live is #83's SECOND scenario: a batch where an earlier patch
removes the anchor a later patch resolves against. The pre-check passes
(both ids resolve in the freshly-copied tree), and the sequential apply's
re-enumeration then drops the later id — reaching the ``KeyError`` sites in
``_apply_patches_into_tree`` that the transaction boundary does not catch.

Both tests below are ``xfail(strict=True)``: they fail on current main and
must XPASS once the fix lands, at which point the marker is removed.
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #83: _apply_patches_into_tree raises KeyError for an anchor an "
        "earlier patch in the same batch erased; the evolve transaction "
        "boundary only catches ValueError, so the run aborts"
    ),
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "issue #83: build_post_apply_validator guards only ValueError, so a "
        "KeyError out of derive_generation escapes the retryable-finding path"
    ),
)
@pytest.mark.asyncio
async def test_transaction_boundary_surfaces_key_error_as_a_finding(tmp_path: Path) -> None:
    """The transaction boundary degrades ANY bad-patch-set signal to a finding.

    Defence in depth for the applier fix above: even if some apply site keeps
    raising ``KeyError``, the round-level boundary must turn it into a
    retryable rejected-patch finding rather than let it abort the round.
    """
    from zicato.core.experiment import Experiment
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
        generation_id="v1",
        epoch_id="e1",
        hypothesis="triage probe",
        patches=(),
    )
    findings = await validate(candidate)
    assert findings, "a bad patch set must surface as a finding, not propagate"
    assert any("patch set" in f for f in findings)
