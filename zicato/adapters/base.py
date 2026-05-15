"""Protocol surface for zicato's inner-harness adapters.

Two :func:`~typing.runtime_checkable` Protocols make up the adapter
contract:

* :class:`HarnessAdapter` — knows how to *load* a generation snapshot
  (a frozen source tree produced by the patch applier) into a runnable
  instance, and how to *enumerate* the mutation points the proposer
  may target. Adapters are typically constructed once per
  :class:`~zicato.core.RuntimeConfig` and re-used across many
  generations; each ``load`` call builds a fresh
  :class:`RunnableHarness` instance from a generation root.
* :class:`RunnableHarness` — a loaded inner-harness instance bound to
  one generation. Stateless across runs: the runner constructs a new
  one per generation and discards it once the generation's board has
  been executed.

The two Protocols are deliberately small. The runner doesn't care
*how* the adapter wires its inner harness, only that it can drive
``run(entry, sinks, config)`` and recover a :class:`RunResult`. This
gives non-ADK frameworks a clean integration surface without forcing
them through goldfive.

Runtime checkability is on for both Protocols so the runner can
``isinstance(...)``-check operator-supplied callables at construction
time and fail fast on shape errors rather than only at the first
``run`` invocation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from zicato.core import BoardEntry, MutationPoint, RunResult, RuntimeConfig


@runtime_checkable
class RunnableHarness(Protocol):
    """A loaded inner-harness instance bound to one generation snapshot.

    Concrete adapters return instances of their own private classes
    from :meth:`HarnessAdapter.load`; the runner only consumes them
    through this Protocol.

    Implementations MUST be stateless across :meth:`run` calls — the
    runner expects to invoke ``run`` once per board entry under one
    generation and never to share state between entries. Adapters that
    need per-generation caches (e.g. a tokenizer warm-up) keep those
    on the runnable instance, not on the adapter itself.
    """

    async def run(
        self,
        entry: "BoardEntry",
        sinks: list[Any],
        config: "RuntimeConfig",
    ) -> "RunResult":
        """Execute ``entry`` under this generation and return a :class:`RunResult`.

        Parameters
        ----------
        entry:
            The board entry to execute. Kind-discriminated; the adapter
            dispatches on :attr:`BoardEntry.kind` to single-turn /
            scripted multi-turn / emulated multi-turn drivers.
        sinks:
            goldfive :class:`EventSink` list to forward to the inner
            harness. The runner constructs and owns these (typically a
            :class:`JSONLPersistenceSink` writing to the per-run events
            file plus any operator-attached extras); the adapter only
            wires them through.
        config:
            :class:`RuntimeConfig` carrying the harness LLM callable,
            the seed, and bookkeeping ids. Adapters MUST forward
            :attr:`RuntimeConfig.harness_call_llm` (and not the
            ``auxiliary_call_llm``) to the inner harness — the
            two-callable rule is a collusion guard.

        Returns
        -------
        RunResult
            The transcript-shape result of the run. On timeout (the
            entry's :attr:`wall_clock_budget_seconds`), implementations
            MUST return a :class:`RunResult` with ``aborted=True`` and
            ``abort_reason='wall_clock_budget'`` rather than propagating
            an exception.
        """
        ...


@runtime_checkable
class HarnessAdapter(Protocol):
    """An adapter that knows how to load and enumerate an inner harness.

    Attributes
    ----------
    name:
        Short symbolic identifier for the adapter shape (e.g.
        ``"adk"``, ``"callable"``, ``"langchain"``). Logged in run
        records so operators can tell at a glance which adapter
        executed a given generation. MUST be filesystem-safe; the
        runner uses it unmodified in journal entries.
    """

    name: str

    def load(self, generation_root: Path) -> RunnableHarness:
        """Load a :class:`RunnableHarness` rooted at ``generation_root``.

        ``generation_root`` is the path the patch applier emitted for
        this generation — a fully realized source-tree snapshot
        containing the inner-harness modules with patches applied.
        Adapters MUST resolve the inner harness's entry point against
        this root (e.g. by inserting it at the front of ``sys.path``
        and re-importing the entrypoint module).

        The returned :class:`RunnableHarness` is treated as opaque by
        the runner; callers do not introspect it.

        Implementations MUST raise (rather than return a degraded
        instance) when the entry point cannot be resolved — the runner
        fails the whole generation rather than producing misleading
        run records.
        """
        ...

    def mutation_points(
        self, source_roots: list[Path] | None = None
    ) -> list["MutationPoint"]:
        """Enumerate the inner harness's mutation points.

        Parameters
        ----------
        source_roots:
            Optional list of source-tree roots to enumerate over. When
            ``None``, adapters fall back to their construction-time
            ``mutable_trees`` default. The ``None`` path is the common
            case during proposer rounds; explicit ``source_roots`` is
            used by the patch applier to re-enumerate against a fresh
            generation snapshot before applying patches.

        Returns
        -------
        list[MutationPoint]
            Stable-id mutation points. Ids MUST be stable across
            generations — re-enumerating after a patch that did not
            structurally relocate a span MUST return the same id for
            that span, so the proposer can keep targeting it.
        """
        ...


__all__ = [
    "HarnessAdapter",
    "RunnableHarness",
]
