"""Tests for the zicato adapter Protocol surface.

These tests intentionally avoid touching :mod:`goldfive` or
:mod:`google.adk` — the Protocol definitions live in
:mod:`zicato.adapters.base` and must be usable without either optional
dependency installed. A small stub class is enough to validate that
the Protocol shape matches what concrete adapters will conform to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zicato.adapters import HarnessAdapter, RunnableHarness
from zicato.core import BoardEntry, MutationPoint, RunResult, RuntimeConfig


class _StubRunnableHarness:
    """Minimal :class:`RunnableHarness` shape for Protocol-checking tests."""

    async def run(
        self,
        entry: BoardEntry,
        sinks: list[Any],
        config: RuntimeConfig,
    ) -> RunResult:
        return RunResult(
            run_id="stub-run",
            entry_id=entry.id,
            final_output="",
            transcript=(),
            runtime_ms=0,
        )


class _StubHarnessAdapter:
    """Minimal :class:`HarnessAdapter` shape for Protocol-checking tests."""

    name: str = "stub"

    def load(self, generation_root: Path) -> _StubRunnableHarness:
        return _StubRunnableHarness()

    def mutation_points(
        self, source_roots: list[Path] | None = None
    ) -> list[MutationPoint]:
        return []


def test_runnable_harness_protocol_accepts_stub_with_run_method() -> None:
    """A class implementing ``async def run(...)`` is a :class:`RunnableHarness`."""
    assert isinstance(_StubRunnableHarness(), RunnableHarness)


def test_runnable_harness_protocol_rejects_class_without_run() -> None:
    """A class without ``run`` is NOT a :class:`RunnableHarness`."""

    class NotARunnable:
        pass

    assert not isinstance(NotARunnable(), RunnableHarness)


def test_harness_adapter_protocol_accepts_stub_with_full_surface() -> None:
    """A class with ``name`` + ``load`` + ``mutation_points`` conforms."""
    assert isinstance(_StubHarnessAdapter(), HarnessAdapter)


def test_harness_adapter_protocol_rejects_class_without_load() -> None:
    """A class missing ``load`` fails the Protocol check."""

    class MissingLoad:
        name: str = "missing"

        def mutation_points(
            self, source_roots: list[Path] | None = None
        ) -> list[MutationPoint]:
            return []

    assert not isinstance(MissingLoad(), HarnessAdapter)


def test_harness_adapter_protocol_rejects_class_without_mutation_points() -> None:
    """A class missing ``mutation_points`` fails the Protocol check."""

    class MissingEnumerator:
        name: str = "missing"

        def load(self, generation_root: Path) -> _StubRunnableHarness:
            return _StubRunnableHarness()

    assert not isinstance(MissingEnumerator(), HarnessAdapter)


def test_stub_adapter_load_returns_runnable_harness() -> None:
    """End-to-end Protocol path: adapter.load returns a runnable harness."""
    adapter: HarnessAdapter = _StubHarnessAdapter()
    runnable = adapter.load(Path("/tmp/does-not-exist"))
    assert isinstance(runnable, RunnableHarness)


def test_stub_adapter_mutation_points_returns_empty_list_by_default() -> None:
    """The Protocol's ``mutation_points`` is callable with no arguments."""
    adapter: HarnessAdapter = _StubHarnessAdapter()
    assert adapter.mutation_points() == []
    assert adapter.mutation_points(source_roots=[Path("/tmp")]) == []


def test_stub_adapter_has_string_name() -> None:
    """Adapters carry a filesystem-safe symbolic name."""
    adapter = _StubHarnessAdapter()
    assert isinstance(adapter.name, str)
    assert adapter.name == "stub"
