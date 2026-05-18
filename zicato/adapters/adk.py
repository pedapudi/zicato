"""ADK :class:`~zicato.adapters.base.HarnessAdapter` for goldfive-wrapped trees.

Drives Google ADK agent trees (``LlmAgent`` and ``BaseAgent`` subclasses)
through :mod:`goldfive` so the inner harness gets the same goal / plan
/ drift overlay every other zicato target uses. The shape mirrors
goldfive's own one-line ``goldfive.run`` surface: the adapter takes
care of *which* agent symbol to import from *which* generation
snapshot, then hands the live agent to ``goldfive.run`` for the
actual turn.

Lazy imports
------------

This module imports :mod:`goldfive` and :mod:`google.adk` only inside
:meth:`ADKHarnessAdapter.load` (and the runnable's :meth:`run`), so
``from zicato.adapters import HarnessAdapter`` does not force the
ADK extra on consumers who only need the Protocol surface.

:mod:`zicato.mutation.enumerator` is owned by a sibling module and is
imported lazily inside :meth:`ADKHarnessAdapter.mutation_points` for
the same reason — the adapter does not transitively pull in the
enumerator's parser machinery at import time.

Generation-snapshot loading
---------------------------

:meth:`ADKHarnessAdapter.load` puts ``generation_root`` at the front
of ``sys.path`` and re-imports the entrypoint module from that root.
We deliberately do NOT restore ``sys.modules`` after the load — the
tournament-runner contract in v0+1 is "fresh process per generation",
so a single-process pass-through here is enough. Multi-generation
processes can wrap calls themselves if they need stricter isolation.

Transcript extraction
---------------------

goldfive's :class:`~goldfive.results.ExecutionOutcome` carries the
session as ``outcome.session``; the user-facing assistant outputs
land on ``session.completed_results`` keyed by task id. We treat the
ordered values of that dict as the run's transcript and the last
entry as :attr:`RunResult.final_output`. For trees that produce no
``completed_results`` (e.g. when the planner short-circuited
PassthroughPlanner with no LLM available), the transcript is empty
and :attr:`final_output` is ``""``.

Judges
------

goldfive#437 lets a caller pass a custom :class:`~goldfive.judges.Judge`
list into ``goldfive.run`` / ``goldfive.wrap`` via ``judges=[...]``. The
adapter assembles that list per board entry through
:func:`zicato.judge_runtime.assemble_judges`: goldfive's default
built-in judges (minus any the board's ``disable_drift`` suppressed)
plus the entry's declared :class:`~zicato.core.JudgeSpec` judges, each
turned into a live goldfive ``Judge``. Inline judges run on zicato's
*auxiliary* callable (the two-callable rule); python judges bring their
own dependencies. When the entry declares no custom judges and the
board suppresses nothing, the assembled list equals goldfive's default
set, so behaviour is byte-identical to a plain ``goldfive.run`` call.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core import BoardEntry, MutationPoint, RunResult, RuntimeConfig

if TYPE_CHECKING:
    from zicato.adapters.base import RunnableHarness

log = logging.getLogger("zicato.adapters.adk")


# ---------------------------------------------------------------------------
# Entrypoint resolution
# ---------------------------------------------------------------------------


def _split_entrypoint(entrypoint: str) -> tuple[str, str]:
    """Split a ``"module.path:agent_symbol"`` spec into its two halves.

    Raises :class:`ValueError` with an actionable message on a malformed
    spec — empty, missing the colon, multiple colons, empty module, or
    empty symbol. The colon convention mirrors Python's ``entry_points``
    syntax so operators authoring zicato configs feel at home.
    """
    if not entrypoint or ":" not in entrypoint:
        raise ValueError(
            f"ADKHarnessAdapter: entrypoint must be 'module.path:agent_symbol', got {entrypoint!r}"
        )
    parts = entrypoint.split(":")
    if len(parts) != 2:
        raise ValueError(
            f"ADKHarnessAdapter: entrypoint must contain exactly one ':' "
            f"separator, got {entrypoint!r}"
        )
    module_path, symbol = parts
    module_path = module_path.strip()
    symbol = symbol.strip()
    if not module_path or not symbol:
        raise ValueError(
            f"ADKHarnessAdapter: entrypoint module and symbol must be non-empty, got {entrypoint!r}"
        )
    return module_path, symbol


def _default_mutable_trees(module_path: str) -> list[Path]:
    """Best-effort default for :attr:`ADKHarnessAdapter.mutable_trees`.

    Resolves the entrypoint module via :func:`importlib.util.find_spec`
    and returns the directory containing the module file as the single
    mutable tree. Returns an empty list when the module cannot be
    resolved at construction time — the adapter does not fail
    construction on a missing module because tests construct adapters
    against modules that will only exist after a later patch applier
    pass.
    """
    try:
        spec = importlib.util.find_spec(module_path)
    except (ImportError, ValueError):
        return []
    if spec is None or spec.origin is None:
        return []
    origin = Path(spec.origin)
    if not origin.is_file():
        return []
    return [origin.parent.resolve()]


# ---------------------------------------------------------------------------
# Transcript extraction from a goldfive ExecutionOutcome
# ---------------------------------------------------------------------------


def _outcome_transcript(outcome: Any) -> tuple[str, ...]:
    """Return the ordered user-facing assistant outputs from ``outcome``.

    goldfive 0.x represents per-task assistant text on
    ``outcome.session.completed_results`` — a ``dict[str, str]`` keyed
    by task id and ordered by completion. We treat those values, in
    insertion order, as the run's transcript. For runs that produced
    no ``completed_results`` (PassthroughPlanner with no LLM, or a
    failed run that aborted before any task completed), the transcript
    is empty.
    """
    session = getattr(outcome, "session", None)
    if session is None:
        return ()
    completed = getattr(session, "completed_results", None)
    if not completed:
        return ()
    return tuple(str(v) for v in completed.values())


# ---------------------------------------------------------------------------
# Judge assembly inputs from a board entry
# ---------------------------------------------------------------------------


def _entry_judge_specs(entry: BoardEntry) -> tuple[Any, ...]:
    """Return the entry's declared :class:`~zicato.core.JudgeSpec` tuple.

    Reads ``BoardEntry.judges`` defensively via :func:`getattr` so the
    adapter keeps working against a :class:`BoardEntry` revision that
    predates the ``judges`` field (the field is owned by
    ``zicato/core/types.py``; this adapter must not assume a particular
    landing order). An absent / ``None`` field yields an empty tuple —
    the entry simply contributes no custom judges.
    """
    judges = getattr(entry, "judges", None)
    if not judges:
        return ()
    return tuple(judges)


#: ``BoardEntry.context`` key the tournament runner stamps the
#: board-level ``disable_drift`` suppression set under. Kept in sync with
#: ``zicato.tournament.runner._DISABLE_DRIFT_CONTEXT_KEY`` — the two ends
#: meet on this single string.
_DISABLE_DRIFT_CONTEXT_KEY = "disable_drift"


def _entry_disable_drift(entry: BoardEntry) -> tuple[Any, ...]:
    """Return the drift kinds the board wants suppressed for ``entry``.

    ``disable_drift`` is a board-LEVEL setting (``Board.disable_drift``),
    but the :class:`~zicato.adapters.base.RunnableHarness` Protocol hands
    the adapter a :class:`BoardEntry`, not the owning ``Board``. The
    tournament runner therefore stamps the board-level suppression set
    onto every entry's :attr:`~zicato.core.BoardEntry.context` mapping
    under :data:`_DISABLE_DRIFT_CONTEXT_KEY` (see
    ``zicato.tournament.runner._stamp_disable_drift``) — ``context`` is
    the one per-entry channel that survives the runner -> subprocess
    worker -> :func:`zicato.core.validate_board_entry` round-trip.

    The value is a comma / whitespace separated list of
    :class:`goldfive.DriftKind` wire strings. Returns an empty tuple when
    the entry carries no such key, in which case goldfive's built-in
    judges all stay default-on.
    """
    raw = (getattr(entry, "context", {}) or {}).get(_DISABLE_DRIFT_CONTEXT_KEY)
    if not raw:
        return ()
    # ``context`` is a string-valued mapping; split on commas /
    # whitespace into individual drift-kind wire strings.
    return tuple(token for token in raw.replace(",", " ").split() if token)


# ---------------------------------------------------------------------------
# Concrete RunnableHarness
# ---------------------------------------------------------------------------


class ADKRunnableHarness:
    """An ADK agent loaded under one generation snapshot.

    Constructed by :meth:`ADKHarnessAdapter.load`; not intended for
    direct instantiation. Stateless across :meth:`run` calls — the
    runner constructs a new instance per generation and discards it
    when the generation's board has been executed.

    Conforms to the :class:`~zicato.adapters.base.RunnableHarness`
    Protocol structurally (no inheritance), so the Protocol's
    ``runtime_checkable`` check passes.
    """

    __slots__ = ("_agent", "_mutable_trees")

    def __init__(self, agent: Any, mutable_trees: list[Path]) -> None:
        """Bind a loaded ADK ``agent`` and remember the mutable-tree set.

        ``mutable_trees`` is kept on the runnable purely for diagnostics;
        the runner does not consult it on the runnable, only on the
        adapter that produced this instance.
        """
        self._agent = agent
        self._mutable_trees = list(mutable_trees)

    async def run(
        self,
        entry: BoardEntry,
        sinks: list[Any],
        config: RuntimeConfig,
    ) -> RunResult:
        """Execute ``entry`` against the loaded ADK agent via :mod:`goldfive`.

        Dispatches on ``entry.kind``:

        * ``single_turn`` → :func:`goldfive.run` with ``entry.input`` as
          the user message.
        * ``multi_turn_scripted`` → lazy import :mod:`zicato.board.scripted`
          and delegate (sibling module owned by R2-E).
        * ``multi_turn_emulated`` → lazy import :mod:`zicato.emulator`
          and delegate (sibling module owned by R2-I).

        For ``synthetic_*`` kinds (forward-compat reserved slots) we
        return an aborted :class:`RunResult` with ``abort_reason=
        'unsupported_kind'`` rather than raising — they're not yet
        wired and the runner should report a clean failure for them.

        The entry's :attr:`wall_clock_budget_seconds` is enforced via
        :func:`asyncio.wait_for`; on timeout we return ``RunResult(
        aborted=True, abort_reason='wall_clock_budget')`` rather than
        propagating :class:`asyncio.TimeoutError`.

        Any other exception is caught and surfaced as ``RunResult(
        aborted=True, abort_reason='harness_exception')`` with the
        exception message on :attr:`RunResult.abort_reason` after a
        colon. The runner's outer scope still sees no exception — the
        invariant the runner relies on is "one RunResult per entry,
        always".
        """
        run_id = uuid.uuid4().hex
        budget_s = float(entry.wall_clock_budget_seconds)
        started_at = time.monotonic()

        async def _drive() -> RunResult:
            if entry.kind == "single_turn":
                return await self._run_single_turn(run_id, entry, sinks, config)
            if entry.kind == "multi_turn_scripted":
                return await self._run_multi_turn_scripted(run_id, entry, sinks, config)
            if entry.kind == "multi_turn_emulated":
                return await self._run_multi_turn_emulated(run_id, entry, sinks, config)
            # Reserved forward-compat slots — not wired in v0.
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            return RunResult(
                run_id=run_id,
                entry_id=entry.id,
                final_output="",
                transcript=(),
                runtime_ms=elapsed_ms,
                aborted=True,
                abort_reason="unsupported_kind",
            )

        try:
            return await asyncio.wait_for(_drive(), timeout=budget_s)
        except TimeoutError:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            return RunResult(
                run_id=run_id,
                entry_id=entry.id,
                final_output="",
                transcript=(),
                runtime_ms=elapsed_ms,
                aborted=True,
                abort_reason="wall_clock_budget",
            )
        except Exception as exc:  # noqa: BLE001 — see RunResult invariant in docstring
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            log.warning(
                "ADKRunnableHarness.run: harness raised %s on entry %r",
                type(exc).__name__,
                entry.id,
            )
            return RunResult(
                run_id=run_id,
                entry_id=entry.id,
                final_output="",
                transcript=(),
                runtime_ms=elapsed_ms,
                aborted=True,
                abort_reason=f"harness_exception:{type(exc).__name__}",
            )

    # ------------------------------------------------------------------
    # Per-kind drivers
    # ------------------------------------------------------------------

    async def _run_single_turn(
        self,
        run_id: str,
        entry: BoardEntry,
        sinks: list[Any],
        config: RuntimeConfig,
    ) -> RunResult:
        """Drive a single ``goldfive.run`` invocation against the agent.

        Forwards :attr:`RuntimeConfig.harness_call_llm` (not the
        auxiliary callable — see the two-callable rule on
        :class:`RuntimeConfig`) and the entry's input. Returns a
        :class:`RunResult` constructed from the outcome's session's
        ``completed_results`` values.

        Judges (goldfive#437) are assembled per entry and passed into
        ``goldfive.run`` via its ``judges=`` parameter: goldfive's
        default built-ins minus any the board's ``disable_drift``
        suppressed, plus the entry's declared
        :class:`~zicato.core.JudgeSpec` judges. Inline judges run on the
        *auxiliary* callable — distinct from the harness callable the
        agent runs on — so a judge cannot trivially collude with the
        tree it grades.
        """
        import goldfive  # lazy: keep the optional dep out of import time

        from zicato.judge_runtime import assemble_judges

        assert entry.input is not None, "single_turn entry must have 'input' (validated upstream)"
        started_at = time.monotonic()
        judges = assemble_judges(
            entry_judges=_entry_judge_specs(entry),
            disable_drift=_entry_disable_drift(entry),
            aux_call_llm=config.auxiliary_call_llm,
        )
        outcome = await goldfive.run(
            self._agent,
            entry.input,
            sinks=sinks,
            call_llm=config.harness_call_llm,
            judges=judges,
        )
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        transcript = _outcome_transcript(outcome)
        final_output = transcript[-1] if transcript else ""
        return RunResult(
            run_id=run_id,
            entry_id=entry.id,
            final_output=final_output,
            transcript=transcript,
            runtime_ms=elapsed_ms,
        )

    async def _run_multi_turn_scripted(
        self,
        run_id: str,
        entry: BoardEntry,
        sinks: list[Any],
        config: RuntimeConfig,
    ) -> RunResult:
        """Delegate to :mod:`zicato.board.scripted` (lazy import).

        The scripted driver is owned by R2-E and may not exist at the
        time this adapter is built. We import it lazily and surface a
        clean abort if it is missing so the adapter degrades gracefully
        when other modules land out of order.

        The scripted driver expects a ``harness`` object with an
        ``async run(user_message: str)`` interface (see
        :func:`zicato.board.scripted._resolve_invoker`). Passing the
        raw ADK agent would cause :class:`TypeError` because the ADK
        agent's ``.run()`` method does not accept a bare string
        positional argument — it expects ADK-specific invocation
        arguments. We therefore wrap the agent in a thin per-turn
        caller that calls :func:`goldfive.run` with the correct
        signature on each scripted turn.
        """
        import goldfive  # lazy: keep the optional dep out of import time

        from zicato.judge_runtime import assemble_judges

        try:
            from zicato.board import scripted as scripted_driver
        except ImportError:
            return RunResult(
                run_id=run_id,
                entry_id=entry.id,
                final_output="",
                transcript=(),
                runtime_ms=0,
                aborted=True,
                abort_reason="scripted_driver_unavailable",
            )

        judges = assemble_judges(
            entry_judges=_entry_judge_specs(entry),
            disable_drift=_entry_disable_drift(entry),
            aux_call_llm=config.auxiliary_call_llm,
        )
        agent = self._agent

        class _PerTurnCaller:
            """Thin wrapper that calls ``goldfive.run`` per scripted turn.

            The scripted driver calls ``harness.run(user_message)``; this
            wrapper satisfies that interface and dispatches each call to
            ``goldfive.run(agent, user_message, ...)`` with the correct
            ADK-level arguments. The return value is the last user-facing
            assistant reply extracted from the outcome's
            ``completed_results``, matching the same extraction path used
            by :meth:`_run_single_turn`. Returning a plain string lets the
            scripted driver's :func:`~zicato.board.scripted._coerce_reply`
            pass it through without any further unwrapping.
            """

            async def run(self, user_message: str) -> str:
                outcome = await goldfive.run(
                    agent,
                    user_message,
                    sinks=sinks,
                    call_llm=config.harness_call_llm,
                    judges=judges,
                )
                transcript = _outcome_transcript(outcome)
                return transcript[-1] if transcript else ""

        return await scripted_driver.run_scripted(
            agent=_PerTurnCaller(),
            entry=entry,
            sinks=sinks,
            config=config,
            run_id=run_id,
        )

    async def _run_multi_turn_emulated(
        self,
        run_id: str,
        entry: BoardEntry,
        sinks: list[Any],
        config: RuntimeConfig,
    ) -> RunResult:
        """Delegate to :mod:`zicato.emulator` (lazy import).

        The emulator is owned by R2-I and may not exist at the time
        this adapter is built; same degradation contract as the
        scripted driver above.
        """
        try:
            from zicato import emulator
        except ImportError:
            return RunResult(
                run_id=run_id,
                entry_id=entry.id,
                final_output="",
                transcript=(),
                runtime_ms=0,
                aborted=True,
                abort_reason="emulator_unavailable",
            )
        return await emulator.run_emulated(
            agent=self._agent,
            entry=entry,
            sinks=sinks,
            config=config,
            run_id=run_id,
        )


# ---------------------------------------------------------------------------
# Concrete HarnessAdapter
# ---------------------------------------------------------------------------


class ADKHarnessAdapter:
    """A :class:`HarnessAdapter` for Google ADK trees driven through goldfive.

    Constructed once per zicato instance and re-used across all
    generations of one epoch. The expensive work (importing
    :mod:`goldfive`, :mod:`google.adk`, the entrypoint module) is
    deferred to :meth:`load`.

    Parameters
    ----------
    entrypoint:
        ``"module.path:agent_symbol"`` string identifying a
        module-level ADK agent. The module is re-imported under each
        generation root; the symbol is fetched via :func:`getattr` and
        passed to :func:`goldfive.run`.
    mutable_trees:
        Optional list of source-tree roots the mutation enumerator
        should walk. When ``None``, defaults to ``[Path(<directory
        containing the entrypoint module>)]`` — the natural single-tree
        case. The default is resolved at construction time on a
        best-effort basis (we tolerate a missing module so adapters
        can be built against modules that only exist post-patch);
        callers wanting a strict construction-time check should pass
        ``mutable_trees`` explicitly.

    Conforms to :class:`~zicato.adapters.base.HarnessAdapter`
    structurally; the Protocol's ``runtime_checkable`` check passes
    without inheritance.
    """

    name: str = "adk"

    def __init__(
        self,
        entrypoint: str,
        mutable_trees: list[Path] | None = None,
    ) -> None:
        module_path, symbol = _split_entrypoint(entrypoint)
        self._entrypoint = entrypoint
        self._module_path = module_path
        self._symbol = symbol
        if mutable_trees is None:
            self.mutable_trees = _default_mutable_trees(module_path)
        else:
            self.mutable_trees = [Path(p).resolve() for p in mutable_trees]

    # ------------------------------------------------------------------
    # HarnessAdapter surface
    # ------------------------------------------------------------------

    def load(self, generation_root: Path) -> RunnableHarness:
        """Load the entrypoint agent from ``generation_root``.

        Puts ``generation_root`` at the front of :data:`sys.path`,
        re-imports the entrypoint module (so a cached parent-generation
        version is replaced with the snapshot version), fetches the
        named symbol, and returns an :class:`ADKRunnableHarness`
        wrapping it.

        We do not restore ``sys.path`` or ``sys.modules`` — see this
        module's docstring on the fresh-process-per-generation
        contract.
        """
        # Lazy import: keep these optional at zicato.adapters import time.
        import goldfive  # noqa: F401 — surface the dep here so missing extra fails clean
        import google.adk  # noqa: F401 — same; google-adk is the ADK extra

        root_str = str(Path(generation_root).resolve())
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        # Reload semantics: if the module was previously imported from a
        # different root, force a fresh import so the snapshot's version
        # wins. ``importlib.reload`` requires an existing module object;
        # for the first-import case we fall back to ``import_module``.
        if self._module_path in sys.modules:
            module = importlib.reload(sys.modules[self._module_path])
        else:
            module = importlib.import_module(self._module_path)

        try:
            agent = getattr(module, self._symbol)
        except AttributeError as exc:
            raise AttributeError(
                f"ADKHarnessAdapter: entrypoint module {self._module_path!r} "
                f"has no symbol {self._symbol!r} (loaded from "
                f"{getattr(module, '__file__', '<unknown>')!r})"
            ) from exc

        return ADKRunnableHarness(agent=agent, mutable_trees=list(self.mutable_trees))

    def mutation_points(self, source_roots: list[Path] | None = None) -> list[MutationPoint]:
        """Enumerate mutation points across ``source_roots``.

        When ``source_roots`` is ``None``, falls back to
        :attr:`mutable_trees`. Delegates to
        :func:`zicato.mutation.enumerator.enumerate_mutations` — owned
        by a sibling module and imported lazily so this adapter does
        not pull the enumerator's parser at import time.
        """
        roots = source_roots if source_roots is not None else self.mutable_trees
        if not roots:
            return []

        # Lazy import — the enumerator module is owned elsewhere and may
        # not exist yet at adapter import time. Surface a clean error
        # instead of an opaque ImportError so operators know which
        # module is missing.
        try:
            from zicato.mutation.enumerator import enumerate_mutations
        except ImportError as exc:
            raise ImportError(
                "ADKHarnessAdapter.mutation_points requires "
                "zicato.mutation.enumerator.enumerate_mutations; the "
                "mutation enumerator module is not yet available."
            ) from exc

        return _coerce_to_list(enumerate_mutations(roots))


def _coerce_to_list(points: Iterable[MutationPoint]) -> list[MutationPoint]:
    """Normalize the enumerator's return shape to a concrete ``list``.

    The enumerator's signature is "returns an iterable of
    :class:`MutationPoint`"; we materialize once so callers downstream
    can safely iterate twice (e.g. one pass to count, one pass to
    render an audit table).
    """
    if isinstance(points, list):
        return points
    return list(points)


__all__ = [
    "ADKHarnessAdapter",
    "ADKRunnableHarness",
]
