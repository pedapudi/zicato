"""Constructors for the runtime objects scripted tests build by hand.

A test that drives the tournament runner, a worker subprocess or a CLI
command needs a :class:`~zicato.core.RuntimeConfig` and, often, a
:class:`~zicato.core.Generation` and a seeded lineage — none of which is
the subject of the test. Before this module each such test carried its
own copy of the same constructor, and the copies drifted only in the
name they were given, never in what they built.

The builders here are deliberately minimal: they construct the smallest
object the runtime accepts, with values that carry no meaning beyond
being distinct. A test whose subject IS one of these values passes it
explicitly or builds the object itself.
"""

from __future__ import annotations

from pathlib import Path

from zicato.core import Generation, RuntimeConfig
from zicato.epoch.lineage import append_to_lineage


def runtime_config(tmp_path: Path) -> RuntimeConfig:
    """A RuntimeConfig whose two LLM callables return the empty string.

    The harness and evaluation callables are separate function objects, not
    one function bound twice: the runner re-checks that the two callables
    are identity-unequal as defense in depth, so a config that reused a
    single callable would fail that check for a reason unrelated to the
    test's subject.
    """

    async def harness_call(system: str, user: str, model: str) -> str:
        return ""

    async def aux_call(system: str, user: str, model: str) -> str:
        return ""

    return RuntimeConfig(
        instance_id="test",
        workspace_root=tmp_path,
        target_call_llm=harness_call,
        evaluation_call_llm=aux_call,
    )


def make_generation(workspace: Path, gen_id: str = "v0") -> Generation:
    """A parentless generation whose snapshot directory exists on disk.

    The snapshot root is created eagerly because the callers hand the
    generation to code that reads the directory.
    """
    snap = workspace / "snap" / gen_id
    snap.mkdir(parents=True, exist_ok=True)
    return Generation(
        id=gen_id,
        epoch_id="e0",
        parent_id=None,
        snapshot_root=snap,
        created_at="2026-05-15T00:00:00Z",
    )


def seed_promoted_lineage(ws: Path, epoch_id: str) -> None:
    """Register a promoted seed ``v0`` and a promoted child ``v1`` in lineage.

    The snapshot roots are paths that are never read — the callers exercise
    lineage and index bookkeeping, not snapshot contents.
    """
    g0 = Generation(
        id="v0",
        epoch_id=epoch_id,
        parent_id=None,
        snapshot_root=Path("/tmp/snap/v0"),
        created_at="2026-01-01T00:00:00Z",
        promoted=True,
    )
    g1 = Generation(
        id="v1",
        epoch_id=epoch_id,
        parent_id="v0",
        snapshot_root=Path("/tmp/snap/v1"),
        created_at="2026-01-02T00:00:00Z",
        promoted=True,
    )
    append_to_lineage(ws, epoch_id, g0, None)
    append_to_lineage(ws, epoch_id, g1, "v0")
