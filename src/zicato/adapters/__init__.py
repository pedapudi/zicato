"""zicato.adapters — pluggable system-under-test adapters.

A :class:`HarnessAdapter` abstracts "how do we run one generation of the
system under test against one :class:`~zicato.core.BoardEntry`?" so non-ADK
runtimes (langchain, plain callables, future frameworks) can plug into
the same runner / tournament / journal infrastructure without
touching shared modules.

v0 ships one concrete adapter: :class:`ADKHarnessAdapter` for Google
ADK trees driven through :mod:`goldfive`. The Protocol surface
(:class:`HarnessAdapter`, :class:`RunnableHarness`) is intentionally
minimal — ``load(generation_root) -> RunnableHarness`` and
``mutation_points(source_roots) -> list[MutationPoint]`` on the
adapter, ``run(entry, sinks, config) -> RunResult`` on the loaded
runnable. The pair is the only contract the runner depends on.

The :class:`ADKHarnessAdapter` import is lazy in two ways:

* :mod:`goldfive` and :mod:`google.adk` are only required at
  :meth:`ADKHarnessAdapter.load` time rather than at ``import zicato.adapters``
  time. Pure-Protocol consumers (e.g. tests that stub their own
  :class:`HarnessAdapter`) pay nothing for the optional extras.
* :mod:`zicato.mutation.enumerator` (owned by another module) is only
  resolved when :meth:`ADKHarnessAdapter.mutation_points` is invoked —
  the adapter module itself does not transitively import it.
"""

from __future__ import annotations

from zicato.adapters.adk import (
    entry_disable_drift,
    entry_judge_only,
    rebind_tree_models_to_adk_model,
)
from zicato.adapters.base import HarnessAdapter, RunnableHarness

__all__ = [
    "HarnessAdapter",
    "RunnableHarness",
    "entry_disable_drift",
    "entry_judge_only",
    "rebind_tree_models_to_adk_model",
]
