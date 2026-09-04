"""Protocol surface for zicato's system-under-test adapters.

Two :func:`~typing.runtime_checkable` Protocols make up the adapter
contract:

* :class:`HarnessAdapter` — knows how to *load* a generation snapshot
  (a frozen source tree produced by the patch applier) into a runnable
  instance, and how to *enumerate* the mutation points the proposer
  may target. Adapters are typically constructed once per
  :class:`~zicato.core.RuntimeConfig` and re-used across many
  generations; each ``load`` call builds a fresh
  :class:`RunnableHarness` instance from a generation root.
* :class:`RunnableHarness` — a loaded system-under-test instance bound to
  one generation. Stateless across runs: the runner constructs a new
  one per generation and discards it once the generation's board has
  been executed.

The two Protocols are small. The runner doesn't care
*how* the adapter wires its system under test, only that it can drive
``run(entry, sinks, config)`` and recover a :class:`RunResult`. This
gives non-ADK frameworks a clean integration surface without forcing
them through goldfive.

Runtime checkability is on for both Protocols so the runner can
``isinstance(...)``-check operator-supplied callables at construction
time and fail fast on shape errors rather than only at the first
``run`` invocation.

:class:`HarnessAdapter` also carries one OPTIONAL member,
:meth:`HarnessAdapter.on_promote` (issue #125) — see
:data:`OPTIONAL_ADAPTER_MEMBERS` for how "optional" is enforced at
``isinstance`` time.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from zicato.core import BoardEntry, MutationPoint, RunResult, RuntimeConfig

#: Members of :class:`HarnessAdapter` an adapter MAY omit and still be
#: an adapter. Every shipped and operator-authored adapter predates the
#: post-promotion hook, so making :meth:`HarnessAdapter.on_promote`
#: required would retroactively un-adapt all of them.
OPTIONAL_ADAPTER_MEMBERS = frozenset({"on_promote"})

#: The members :class:`HarnessAdapter`'s ``isinstance`` gate actually
#: enforces: the three behavioural methods, which Python guarantees are
#: class-level and therefore visible to ``__subclasshook__``. The data
#: attributes (``name``, ``run_output_names``) are NOT in
#: this set — an adapter may legitimately assign them in ``__init__``,
#: where a class-level hook cannot see them.
REQUIRED_ADAPTER_METHODS = ("mutable_subpaths", "load", "mutation_points")


@runtime_checkable
class RunnableHarness(Protocol):
    """A loaded system-under-test instance bound to one generation snapshot.

    Concrete adapters return instances of their own private classes
    from :meth:`HarnessAdapter.load`; the runner only consumes them
    through this Protocol.

    Implementations MUST be stateless across :meth:`run` calls — the
    runner expects to invoke ``run`` once per board entry under one
    generation and never to share state between entries. Adapters that
    need per-generation caches (e.g. a tokenizer warm-up) keep those
    on the runnable instance rather than on the adapter itself.
    """

    async def run(
        self,
        entry: BoardEntry,
        sinks: list[Any],
        config: RuntimeConfig,
    ) -> RunResult:
        """Execute ``entry`` under this generation and return a :class:`RunResult`.

        Parameters
        ----------
        entry:
            The board entry to execute. Kind-discriminated; the adapter
            dispatches on :attr:`BoardEntry.kind` to single-turn /
            scripted multi-turn / emulated multi-turn drivers.
        sinks:
            goldfive :class:`EventSink` list to forward to the system
            under test. The runner constructs and owns these (typically a
            :class:`JSONLPersistenceSink` writing to the per-run events
            file plus any operator-attached extras); the adapter only
            wires them through.
        config:
            :class:`RuntimeConfig` carrying the target LLM callable,
            the seed, and bookkeeping ids. Adapters MUST forward
            :attr:`RuntimeConfig.target_call_llm` (and not the
            ``evaluation_call_llm``) to the system under test — the
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
    """An adapter that knows how to load and enumerate a system under test.

    Attributes
    ----------
    name:
        Short symbolic identifier for the adapter shape (e.g.
        ``"adk"``, ``"callable"``, ``"langchain"``). Logged in run
        records so operators can tell at a glance which adapter
        executed a given generation. MUST be filesystem-safe; the
        runner uses it unmodified in journal entries.
    run_output_names:
        Optional set of directory / file *names* the system under test
        writes run output under, relative to anywhere in its source
        tree. The generation store excludes these from every
        generation copy (alongside the standing artifact set in
        :mod:`zicato.epoch.snapshot_scope`) so run output never
        compounds across a lineage. An adapter that routes all its
        output through the per-run scratch directory (see
        :meth:`mutable_subpaths` and the ``run_scratch_dir`` contract
        below) can leave this empty. Default: an empty tuple.
    """

    name: str
    run_output_names: tuple[str, ...]

    def mutable_subpaths(self, generation_root: Path) -> list[Path]:
        """Return the system under test's mutable sub-trees under ``generation_root``.

        The **mutable surface** is the set of paths the proposer may
        rewrite — the source files carrying ``# zicato:mutable``
        markers. It is *narrower* than the whole generation
        snapshot: a snapshot also contains support code the worker needs
        to execute the harness but that the proposer never edits.

        Mutation enumeration walks only the returned sub-paths. The
        generation prepare phase consults this instead
        of defaulting to ``[generation_root]``.

        Each returned path MUST be inside ``generation_root`` (the
        method resolves the adapter's construction-time mutable-tree
        declaration against this concrete snapshot root). An adapter
        with no narrower declaration MAY return ``[generation_root]`` —
        the whole tree — but that is the fallback rather than the contract.

        Note this concerns *which source the proposer edits*; it is
        unrelated to *where the harness writes run output*. Run output
        goes to the per-run scratch directory the runner supplies via
        the :data:`zicato.epoch.snapshot_scope.SCRATCH_DIR_ENV`
        environment variable — never into a mutable sub-path and never
        into the snapshot.
        """
        ...

    def load(self, generation_root: Path) -> RunnableHarness:
        """Load a :class:`RunnableHarness` rooted at ``generation_root``.

        ``generation_root`` is the path the patch applier emitted for
        this generation — a fully realized source-tree snapshot
        containing the system-under-test modules with patches applied.
        Adapters MUST resolve the system under test's entry point against
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

    def mutation_points(self, source_roots: list[Path] | None = None) -> list[MutationPoint]:
        """Enumerate the system under test's mutation points.

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

    async def on_promote(
        self,
        *,
        epoch_id: str,
        generation_id: str,
        parent_generation_id: str | None,
        snapshot_root: Path,
        workspace_root: Path,
    ) -> None:
        """OPTIONAL: fold a just-promoted generation into out-of-tree state.

        Called at most once per settled promotion, after canonical settlement
        advances the champion marker to ``generation_id``. An adapter whose evolved state lives
        only in the mutable tree needs nothing here; this exists for a
        target whose real state lives somewhere the tree cannot reach (a
        database row, a served artifact, a remote config) and which
        therefore has to be told when a generation became the champion.

        Parameters
        ----------
        epoch_id:
            The epoch the promotion settled under.
        generation_id:
            The generation just crowned champion. Under a
            multi-challenger structure with an operator multi-promote,
            this is the PRIMARY head — the one ``current_generation``
            advanced to — not every generation marked ``promoted`` in
            lineage.
        parent_generation_id:
            The champion this generation displaced, or ``None`` when the
            round had no recorded parent.
        snapshot_root:
            The promoted generation's realized source tree. Read-only by
            contract: the snapshot is the run record for that generation
            and mutating it invalidates the unit cache keyed on it.
        workspace_root:
            The zicato workspace the run is writing under.

        Failure semantics
        -----------------
        BEST-EFFORT, by contract. A hook that raises — or that exceeds
        :data:`~zicato.evolve.promote_hook.ON_PROMOTE_TIMEOUT_SECONDS`
        — never un-promotes the generation and never fails the round:
        the champion marker has already advanced and the outcome is
        already durable. The field-settlement receipt records
        ``delivery_unknown`` before the call and resolves it to
        ``succeeded`` or ``failed`` afterward. Recovery never retries an
        unknown delivery. A failure is logged at ``ERROR`` and raised as an
        ``on_promote_hook_failed`` WARNING in the round's loop-health report;
        reconciling the external side effect is then the operator's job.
        Implementations that need a promotion to be all-or-nothing must make
        their own side effect idempotent and reconcile from the receipt and
        ``lineage.json``.

        Optionality
        -----------
        This member is in :data:`OPTIONAL_ADAPTER_MEMBERS`: an adapter
        that does not define it is still a :class:`HarnessAdapter` at
        ``isinstance`` time and is simply never called. The default
        implementation here is a no-op, so an adapter that *inherits*
        this Protocol explicitly also gets hookless behaviour for free.
        """
        return None

    @classmethod
    def __subclasshook__(cls, other: type) -> Any:
        """Make :data:`OPTIONAL_ADAPTER_MEMBERS` genuinely optional.

        The stock ``runtime_checkable`` instance check requires *every*
        member in ``__protocol_attrs__``, which would make the optional
        :meth:`on_promote` mandatory the moment it is declared — the
        exact back-compat break issue #125 must not cause. ABCs consult
        ``__subclasshook__`` first, so this short-circuits to ``True``
        for any class carrying the three :data:`REQUIRED_ADAPTER_METHODS`.

        Returning :data:`NotImplemented` (rather than ``False``) for
        anything else hands the decision back to the stock protocol
        check, so a malformed adapter is still rejected — and rejected
        with the stock check's instance-level view, which sees
        attributes assigned in ``__init__``.

        The one shape that falls between the two: an adapter that binds
        the three required methods as INSTANCE attributes in ``__init__``
        rather than declaring them on the class. This hook cannot see
        those, so such an adapter takes the stock path — which also demands
        the optional ``on_promote``, and rejects it. No adapter in this
        repository, and no ``isinstance`` gate outside the adapter tests, has
        that shape; an adapter meant to pass must declare its methods on the
        class (issue #125).
        """
        if cls is not HarnessAdapter:
            return NotImplemented
        for method in REQUIRED_ADAPTER_METHODS:
            if not any(method in base.__dict__ for base in other.__mro__):
                return NotImplemented
        return True


__all__ = [
    "OPTIONAL_ADAPTER_MEMBERS",
    "REQUIRED_ADAPTER_METHODS",
    "HarnessAdapter",
    "RunnableHarness",
]
