"""A harness adapter that runs anywhere, including a fresh interpreter.

Test workspaces need an adapter that returns a canned run result without
importing a model SDK. Most suites get that from a monkeypatched factory,
which is enough because they only ever construct the adapter in the
pytest process.

That is not enough for a fixture whose workspace has to survive the
pre-spend gate (:mod:`zicato.check`), because the gate rebuilds the
adapter in a subprocess exactly as a tournament worker does, and a
monkeypatch does not cross a process boundary. So this adapter lives in
an importable module and declares a ``worker_spec`` naming
:func:`make_stub_adapter` — the same reconstruction contract a real
custom adapter has to satisfy.

Reaching it from a subprocess needs the repository root on the import
path; :func:`stub_adapter_pythonpath` composes the value to export.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from zicato.core.types import RunResult

#: The dotted path a worker spec names to rebuild this adapter.
STUB_ADAPTER_FACTORY = "tests._stub_adapter:make_stub_adapter"

_REPO_ROOT = Path(__file__).resolve().parent.parent


class StubSession:
    """A loaded harness that answers every board entry identically."""

    async def run(self, entry: Any, sinks: list[Any], config: Any) -> RunResult:
        del sinks, config
        return RunResult(
            run_id=f"r-{entry.id}",
            entry_id=entry.id,
            final_output="hello world",
            transcript=("hello world",),
            runtime_ms=100,
        )


class StubAdapter:
    """The adapter itself: loads anywhere, contributes no points of its own."""

    name = "stub"

    def load(self, snapshot_root: Path) -> StubSession:
        del snapshot_root
        return StubSession()

    def mutation_points(self, source_roots: list[Path] | None = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        """How a worker (or the pre-spend gate's probe) rebuilds this."""
        return {"kind": "import", "factory": STUB_ADAPTER_FACTORY}


def make_stub_adapter(*_args: Any) -> StubAdapter:
    """Factory named by :data:`STUB_ADAPTER_FACTORY`."""
    return StubAdapter()


def stub_adapter_pythonpath() -> str:
    """``PYTHONPATH`` a subprocess needs to import this module.

    The repository root prepended to whatever is already exported, so a
    probe or worker started from any working directory can resolve
    ``tests._stub_adapter``.
    """
    existing = os.environ.get("PYTHONPATH", "")
    root = str(_REPO_ROOT)
    if not existing:
        return root
    if root in existing.split(os.pathsep):
        return existing
    return os.pathsep.join((root, existing))


__all__ = [
    "STUB_ADAPTER_FACTORY",
    "StubAdapter",
    "StubSession",
    "make_stub_adapter",
    "stub_adapter_pythonpath",
]
