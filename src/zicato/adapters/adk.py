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
``sys.modules`` is NOT restored after the load: the tournament runner
gives each generation a fresh process, so a single-process pass-through
here is enough. A caller that runs several generations in one process can
wrap the calls itself if it needs stricter isolation.

The mutated-tree invariant (fail CLOSED)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The invariant a scored round depends on is **"the MUTATED TREE is what
runs"** — not the narrower "the entrypoint came from the snapshot". The
two coincide only when the entrypoint lives INSIDE a mutable tree; for
the equally legitimate *dependency* shape (mutate a package the
entrypoint merely imports — target 2 mutates goldfive and drives it from
a harness module outside every tree) the entrypoint's own origin says
nothing about whether the mutations were under test, and a registration
with two trees can satisfy the entrypoint rule while every mutation to
the second tree is a silent scored no-op.

Putting the snapshot on ``sys.path`` is NOT sufficient to guarantee the
snapshot's code is what runs. ``sys.path`` governs only TOP-LEVEL name
resolution, and being FIRST on it shadows nothing it does not itself
contain: a top-level name absent from the snapshot falls straight
through to the next entry and loads the INSTALLED copy (a distribution,
an editable checkout). Every mutation the loop applies is then a no-op
that still scores, gates, promotes and reports a plausible null result
(issue #110).

Two secondary effects can defeat the insert even when the snapshot DOES
contain the name, both consequences of something already being imported:
a ``sys.modules`` hit short-circuits the path search entirely, and
:func:`importlib.reload` of a DOTTED module re-runs the finder against
only its parent package's ``__path__`` — still pointing wherever that
parent was first found. Neither arises in the worker (fresh process per
generation, nothing pre-imported), which is why the invariant is
asserted rather than repaired.

Three layers close the hole, each verifying where its truth exists:

* **Register time** (static, lexical, import-free) —
  :func:`entrypoint_snapshot_origin_error` refuses only what it can
  PROVE wrong without importing: a registered tree whose basename could
  never be a top-level importable name (empty after resolve, or not an
  identifier), because a snapshot copies each tree under its basename
  (:meth:`ADKHarnessAdapter.mutable_subpaths`) and that basename is the
  only handle ``sys.path`` gives the tree. An entrypoint OUTSIDE every
  tree is accepted — that is the dependency shape — and
  :func:`entrypoint_outside_trees_notice` states what then carries the
  verification (per run, at load and post-run).
* **Load time** — :meth:`ADKHarnessAdapter.load` asserts, for EVERY
  registered tree, that the tree's top-level name resolves inside
  ``generation_root``: an already-imported module must have its file
  under the root (a pre-imported installed copy is the shadowing case),
  and an unimported one must ``find_spec`` to a location under the root
  (pure resolution — no target code executes). When the entrypoint's own
  top level is one of those tree names, its imported ``__file__`` is
  asserted under the root too.
* **Post-run** (the truth layer) — :func:`tree_import_status` inspects
  ``sys.modules`` once per tree after a unit ran: imported from under the
  root is VERIFIED, imported from outside is a loud failure (defence in
  depth — load time should already have caught it), and NEVER IMPORTED is
  recorded as ``tree_never_imported`` in ``harness_load.json``. The last
  is not a run failure (a board entry may legitimately not touch a tree)
  but it is the ONLY detector of a fully shadowed snapshot — an installed
  entrypoint under a different top-level name that never imports the
  tree at all — so a generation whose units never imported a tree raises
  a WARNING loop-health finding.

The resolved entrypoint file is surfaced on
:attr:`ADKRunnableHarness.entrypoint_file` so the caller (the subprocess
worker) can record which file the generation actually ran; the per-tree
status is surfaced on :meth:`ADKRunnableHarness.tree_import_status`.

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

Judge-only mode
---------------

A board may opt into *judge-only* evaluation via its board-level
``judge_only`` flag (``Board.judge_only`` → ``board_meta`` header →
stamped onto each entry's ``context`` by the tournament runner →
:func:`entry_judge_only`). In judge-only mode goldfive still JUDGES the
wrapped agent — the drift / process judges stay armed exactly as above —
but does ZERO steering: no goal-derivation LLM call, no planner
replanning, no drift-triggered refine. This is implemented by spreading
:func:`_judge_only_overrides` (a one-task ``StaticPlanner`` so the native
agent tree still runs on goldfive's overlay path and produces a
transcript, a ``LiteralGoalDeriver`` so the entry input becomes the goal
verbatim, AND a :class:`_JudgeOnlySteerer` that suppresses the refine
loop) into every ``goldfive.run`` call for the entry. The default
(``judge_only`` False) leaves the steering path untouched and
byte-identical. The flag folds into the epoch contract hash, so flipping
it opens a new epoch.

Why the steerer override is required
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``StaticPlanner`` alone does NOT yield zero refine attempts. goldfive
dispatches EVERY non-INFO drift through ``DriftObserver.handle_drift`` →
the cancel + refine ladder, regardless of which planner is installed. A
``StaticPlanner`` makes ``planner.refine`` return ``None``, but goldfive
treats a ``None`` refine as *handler exhaustion* and escalates to
``DRIFT_KIND_HUMAN_INTERVENTION_REQUIRED`` — emitting ``refine_attempted``
/ ``refine_failed`` and (under the ``multi_turn_emulated`` persona, where
a CRITICAL ``no_fabricated_numbers`` judge keeps re-firing) spinning to
the wall-clock budget and aborting the run. ``SteeringConfig.observation_only``
does not help either: it gates only the three steer *injection* points
while ``planner.refine`` still runs.

The custom-judge SCORING signal that zicato's reducer reads is the
``custom``-kind ``DriftDetected`` event (attributed to its ``judge_name``
via the paired ``JudgementEmitted``) — and ``handle_drift`` emits that
``DriftDetected`` *before* it enters the ladder. So judge-only suppresses
the refine loop WITHOUT losing the scalar by overriding ``handle_drift``
to emit the ``DriftDetected`` and return, skipping the cancel / promote /
refine machinery entirely. ``JudgementEmitted`` is published upstream in
``DefaultSteerer.evaluate_judges`` and is unaffected. Net effect under
judge-only on EVERY board-run path — gauntlet, multi-challenger, and
``multi_turn_emulated`` — is pure observe + judge: ZERO ``steering``
decisions and ZERO ``refine_attempted`` events.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import keyword
import logging
import sys
import time
import uuid
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zicato.core import BoardEntry, MutationPoint, RunResult, RuntimeConfig
from zicato.import_path import explain_attribute_error

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


def _tree_basename(tree: str | Path) -> str:
    """The top-level name a snapshot will expose ``tree`` under.

    ``seed_generation`` copies each registered tree to
    ``generation_root / Path(raw).resolve().name``, so the FILESYSTEM-RESOLVED
    basename — not the lexical one — is the name ``sys.path`` can supply.
    Without the resolve a relative registration whose last component is ``.``
    or ``..`` (``--mutable-tree .`` from inside the target) yields an EMPTY
    basename and every rule built on it misfires. This resolve feeds
    operator-facing checks only; it is NOT the canonical form of
    anything hashed (folding the checkout path into a contract hash is its own
    bug — see ``_canon_mutable_trees``).
    """
    return Path(tree).resolve().name


def _is_importable_top_level(name: str) -> bool:
    """True when ``name`` could be a top-level module/package name.

    Lexical only: a non-empty Python identifier that is not a keyword. A
    directory named ``goldfive-zicato-optimization-surface`` or ``my-project``
    fails, which is exactly the registration
    :func:`entrypoint_snapshot_origin_error` refuses — a snapshot exposes a
    tree under its basename, and a basename Python cannot name as a module can
    never be verified to have loaded from the snapshot.
    """
    return bool(name) and name.isidentifier() and not keyword.iskeyword(name)


def entrypoint_snapshot_origin_error(
    entrypoint: str,
    mutable_trees: Iterable[str | Path],
) -> str | None:
    """Refusal message when a registration can never put its trees under test.

    The static, IMPORT-FREE, LEXICAL layer of the mutated-tree invariant (see
    the module docstring). A generation snapshot copies each registered mutable
    tree under its own BASENAME — ``generation_root / Path(tree).name`` (the
    rule :meth:`ADKHarnessAdapter.mutable_subpaths` re-bases on, and the one
    ``seed_generation`` materialises) — and the loader only prepends
    ``generation_root`` to ``sys.path``, which resolves TOP-LEVEL names. That
    basename is therefore the ONLY handle the snapshot gives the tree: a tree
    whose basename is not a possible top-level module name can never be shown
    to have run from the snapshot, so it is refused here, before an epoch of
    rounds scores no-ops (issue #110).

    What this does NOT refuse: an entrypoint whose top-level component matches
    no registered tree. That is the DEPENDENCY shape — the mutable tree is a
    dependency the entrypoint imports, which is target 2's declared form
    (mutate goldfive, drive it from a harness module outside every tree) — and
    it is verified per run instead, at load time and post-run, for every tree.
    :func:`entrypoint_outside_trees_notice` returns the operator notice for it.

    Returns ``None`` when nothing is lexically provable: a well-shaped
    registration, a malformed entrypoint (:func:`_split_entrypoint` / the
    CLI's own syntactic check owns that), or no registered mutable trees at all
    (the snapshot surface is then the whole root and the seed step raises its
    own error). Otherwise returns a single operator-actionable line ready to be
    wrapped in the caller's error type.

    Import-free by construction: pure path math, no ``importlib``, so
    ``zicato epoch register`` still works in an environment where the target's own
    runtime deps are not installed.
    """
    trees = list(mutable_trees)
    if not trees:
        return None
    unimportable = [
        (str(tree), _tree_basename(tree))
        for tree in trees
        if not _is_importable_top_level(_tree_basename(tree))
    ]
    if not unimportable:
        return None
    raw, basename = unimportable[0]
    shown = basename or "<empty>"
    return (
        f"mutable tree {raw!r} can never be verified to have run from a "
        f"generation snapshot: a snapshot copies each tree under its basename "
        f"(snapshot/<basename>/...) and the loader only prepends the snapshot "
        f"root to sys.path — which resolves TOP-LEVEL module names only — but "
        f"{shown!r} is not a possible module name. Every mutation to this tree "
        f"would be a scored no-op. Register the IMPORTABLE package directory "
        f"instead (e.g. --mutable-tree {raw.rstrip('/')}/<package_name>), whose "
        f"basename is the package the target imports."
    )


def entrypoint_outside_trees_notice(
    entrypoint: str,
    mutable_trees: Iterable[str | Path],
) -> str | None:
    """Operator notice when ``entrypoint`` lives outside every mutable tree.

    The accepted DEPENDENCY shape: the entrypoint is a harness module that
    imports the mutable tree rather than living inside it (target 2 — mutate
    goldfive, drive it from ``zicato_examples...``). Nothing lexical can
    verify it, because whether the mutated tree runs depends on what the
    harness imports at RUN time — so the registration is accepted and the
    verification moves to where that truth exists: the per-tree resolution
    assert in :meth:`ADKHarnessAdapter.load` and the post-run
    :func:`tree_import_status` record in ``harness_load.json``.

    Returns ``None`` for a registration where the entrypoint's top-level
    component IS a registered tree basename (the in-tree shape, verified
    lexically), for a malformed entrypoint, and when no trees are registered.
    """
    trees = list(mutable_trees)
    if not trees or ":" not in entrypoint:
        return None
    module_path = entrypoint.partition(":")[0].strip()
    if not module_path:
        return None
    top_level = module_path.split(".")[0]
    basenames = [_tree_basename(tree) for tree in trees]
    if top_level in basenames:
        return None
    return (
        f"entrypoint {entrypoint!r} is outside every mutable tree "
        f"{basenames!r}: the trees must be imported by the harness at run "
        f"time for the mutations to be under test — verified per run, see "
        f"harness_load.json."
    )


# ---------------------------------------------------------------------------
# Per-tree verification (load time + post-run)
# ---------------------------------------------------------------------------

#: One tree's post-run verdict: its top-level module was imported from under
#: the generation snapshot. The mutations to it were under test.
TREE_IMPORT_VERIFIED = "verified"

#: One tree's post-run verdict: its top-level module was imported from OUTSIDE
#: the snapshot. The load-time assert should have caught this; reaching it
#: post-run is a defence-in-depth failure and fails the run.
TREE_IMPORT_OUTSIDE_ROOT = "outside_root"

#: One tree's post-run verdict: its top-level module was never imported at
#: all. Not a run failure (a board entry may legitimately not touch a tree),
#: but a generation whose every unit reports it means the tree's mutations
#: cannot have been under test: the snapshot was shadowed (issue #110).
TREE_IMPORT_NEVER_IMPORTED = "never_imported"


def _module_locations(module: Any) -> list[Path]:
    """Every filesystem location an already-imported ``module`` serves from.

    ``__file__`` for a module or a regular package's ``__init__``, plus every
    ``__path__`` entry (a namespace package has no ``__file__`` at all and can
    span several portions — each one is a location its submodules could come
    from). Returns an empty list for a module with neither, which callers treat
    as unverifiable and therefore a failure.
    """
    locations: list[Path] = []
    raw_file = getattr(module, "__file__", None)
    if raw_file:
        locations.append(Path(str(raw_file)).resolve())
    for entry in list(getattr(module, "__path__", None) or []):
        locations.append(Path(str(entry)).resolve())
    return locations


def _spec_locations(spec: Any) -> list[Path]:
    """Every filesystem location a resolved (not yet executed) ``spec`` names.

    The :func:`importlib.util.find_spec` counterpart of
    :func:`_module_locations`: ``origin`` plus every
    ``submodule_search_locations`` portion. A non-path origin (``"built-in"``,
    ``"frozen"``) is kept verbatim so the caller's under-the-root test fails it
    — a tree basename that resolves to a builtin is not the tree.
    """
    if spec is None:
        return []
    locations: list[Path] = []
    origin = getattr(spec, "origin", None)
    if origin:
        locations.append(Path(str(origin)).resolve())
    for entry in list(getattr(spec, "submodule_search_locations", None) or []):
        locations.append(Path(str(entry)).resolve())
    return locations


def _all_under(locations: list[Path], root: Path) -> bool:
    """True when ``locations`` is non-empty and every entry is under ``root``.

    Fail-closed by shape: an empty location list (a namespace/builtin module
    with nothing to point at) is NOT under the root, and one outside portion
    condemns the whole resolution — a namespace package with a portion in
    site-packages can serve unmutated submodules from there.
    """
    return bool(locations) and all(loc.is_relative_to(root) for loc in locations)


def tree_import_status(
    tree_basenames: Iterable[str],
    generation_root: Path,
) -> dict[str, str]:
    """Classify, per tree, where its top-level module was imported from.

    The POST-RUN truth layer of the mutated-tree invariant (module docstring):
    called after a unit's run has finished, when ``sys.modules`` records what
    the run actually imported. Returns ``{tree_basename: verdict}`` over
    :data:`TREE_IMPORT_VERIFIED` / :data:`TREE_IMPORT_OUTSIDE_ROOT` /
    :data:`TREE_IMPORT_NEVER_IMPORTED`, deduplicated and in first-seen order.

    Pure observation — imports nothing, executes nothing, mutates no state — so
    it is safe to call on the way out of any run, including an aborted one (a
    run that timed out simply reports whatever it had imported by then).
    """
    root = Path(generation_root).resolve()
    status: dict[str, str] = {}
    for name in dict.fromkeys(tree_basenames):
        if not name:
            continue
        module = sys.modules.get(name)
        if module is None:
            status[name] = TREE_IMPORT_NEVER_IMPORTED
            continue
        status[name] = (
            TREE_IMPORT_VERIFIED
            if _all_under(_module_locations(module), root)
            else TREE_IMPORT_OUTSIDE_ROOT
        )
    return status


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
# goldfive RuntimeConfig — per-call LLM timeout
# ---------------------------------------------------------------------------


def _goldfive_runtime() -> Any:
    """Build the goldfive ``RuntimeConfig`` for a ``goldfive.run`` call.

    goldfive's :class:`~goldfive.config.AgentConfig.call_timeout_ms`
    defaults to 120 000 ms. A real reasoning model under concurrency
    legitimately exceeds 120 s on a single long-prompt LLM call, so the
    raw default aborts healthy calls and fires a spurious
    ``LLM_CALL_TIMEOUT`` drift. zicato raises the per-call budget to
    :attr:`zicato.config.RuntimeTuningConfig.harness_call_timeout_ms`
    (operator-tunable via ``zicato evolve --harness-call-timeout-ms``,
    whose pinned value reaches this worker-side call site through the
    worker args file — see ``zicato.config.pin_overrides``).

    We start from :meth:`goldfive.config.RuntimeConfig.from_env` so
    every other goldfive subsystem (embedding, judge endpoint,
    drift thresholds, ...) keeps its env-driven configuration, and only
    replace the ``agent`` sub-config's ``call_timeout_ms``. An explicit
    ``GOLDFIVE_AGENT_CALL_TIMEOUT_MS`` env var still wins — when set, we
    leave goldfive's env-resolved value untouched so an operator who
    tunes goldfive directly is not overridden.
    """
    import dataclasses  # noqa: PLC0415
    import os  # noqa: PLC0415

    from goldfive.config import RuntimeConfig as GoldfiveRuntimeConfig  # noqa: PLC0415

    from zicato.config import load_config  # noqa: PLC0415

    runtime = GoldfiveRuntimeConfig.from_env()
    if os.environ.get("GOLDFIVE_AGENT_CALL_TIMEOUT_MS"):
        # Operator tuned goldfive directly — defer to their value.
        return runtime
    timeout_ms = load_config().runtime.harness_call_timeout_ms
    agent = dataclasses.replace(runtime.agent, call_timeout_ms=timeout_ms)
    return dataclasses.replace(runtime, agent=agent)


# ---------------------------------------------------------------------------
# call_llm-backed ADK model — the LAST-RESORT text-only shim.
#
# This whole section builds the TEXT-ONLY fallback for the inner ADK agents'
# own task turns. It is the LAST of three parallel ADK-model paths and the
# wrong one for any function-calling target:
#
#   1. ``models_config.build_adk_model``      — the canonical builder: a real
#      (typically function-calling ``LiteLlm``) ``BaseLlm`` from a configured
#      ``{model, endpoint, api_key_env}`` spec. PREFERRED. Injected by
#      :func:`rebind_tree_models_to_adk_model` when ``models.harness`` is set.
#   2. ADK's own ``LLMRegistry`` native resolution — ANY registry-resolvable
#      string an author hardcoded (``"openai/<model>"`` → ``LiteLlm``, a native
#      ``"gemini-*"`` / ``"gemma-*"`` id → ``Gemini`` / ``Gemma``) keeps native
#      tool/function calling; :func:`_resolves_to_native_function_calling`
#      detects it and the shim LEAVES IT ALONE.
#   3. THIS shim (:func:`rebind_tree_models_to_call_llm`) — fires ONLY on a
#      TOOL-FREE agent that owns a model string of its own (an EMPTY model
#      inherits the root's binding and is left alone) which resolves to a
#      ``google.genai``-backed client (``gemini-*`` / ``gemma-*``) or is wholly
#      unresolvable. It exists
#      to stop the genai-client GC flood (below) for an UNCONFIGURED /
#      misconfigured target — and it is TEXT-ONLY: it carries NO
#      ``function_declarations``, so any agent rebound to it loses native
#      tool/function calling and a tool-driven tree degenerates to a single
#      text turn. Hence last-resort, and hence never for an agent that declares
#      tools: that case keeps its native model, or raises (issue #98).
# ---------------------------------------------------------------------------


def _build_call_llm_adk_model_class() -> type:
    """Build the LAST-RESORT, TEXT-ONLY ``BaseLlm`` shim driven by a call_llm.

    LIMITATION (load-bearing): this shim is **text-only**. Its
    :meth:`generate_content_async` flattens the request to a ``(system, user)``
    text pair and never reads ``llm_request.config.tools`` — so the model it
    yields carries NO ``function_declarations`` and CANNOT make tool/function
    calls. Rebinding a function-calling agent to it silently strips its tools
    and reduces a tool-driven tree to a single text turn. It is therefore the
    LAST resort, used only when neither a configured inner model (path 1) nor a
    native ``LiteLlm``-resolvable string (path 2) is available; see the section
    banner above and :func:`rebind_tree_models_to_call_llm`.

    The harness threads its ``(system, user, model) -> str`` callable into
    every ``goldfive.run`` invocation, and goldfive's *steering* (goal
    derive / planner refine / judges) runs on it. But the wrapped ADK
    ``LlmAgent``s' OWN task turns still run on each agent's declared
    ``model`` field — a bare model string in live runs. ADK resolves that
    string through ``LLMRegistry.new_llm`` and constructs a
    :class:`google.genai.Client`'s ``BaseApiClient`` for it. When no Google
    API key is present (the vLLM / call_llm path never needs one), that
    constructor raises ``ValueError('No API key')`` *before* it sets
    ``_async_httpx_client`` — leaving a partial client whose ``__del__``
    schedules ``aclose()``, which then raises
    ``AttributeError('_async_httpx_client')`` on GC. With one such client
    per turn this floods the log with hundreds of "Task exception was never
    retrieved" tracebacks. The client is never used for real generation.

    The fix is to give each ``LlmAgent`` a real ``BaseLlm`` backed by the
    harness ``call_llm`` so ADK's ``canonical_model`` returns it directly
    (it short-circuits on ``isinstance(self.model, BaseLlm)``), never
    touching ``LLMRegistry.new_llm`` / the genai client. This is the inverse
    of goldfive's :func:`goldfive._llm_detect.make_default_adk_call_llm`
    (which wraps a ``BaseLlm`` *into* a call_llm); here we wrap a call_llm
    *into* a ``BaseLlm``.

    Defined as a factory so the ADK / genai symbols are imported only when a
    rebind actually happens — keeping this module importable without the
    optional ``google-adk`` extra.
    """
    from google.adk.models import BaseLlm  # noqa: PLC0415
    from google.adk.models.llm_request import LlmRequest  # noqa: PLC0415
    from google.adk.models.llm_response import LlmResponse  # noqa: PLC0415
    from google.genai import types as genai_types  # noqa: PLC0415

    class _CallLlmADKModel(BaseLlm):
        """A :class:`BaseLlm` whose generation routes through a call_llm.

        Implements the single abstract method
        :meth:`generate_content_async` by flattening the ADK
        :class:`LlmRequest` into the ``(system, user)`` text pair the
        harness ``call_llm`` expects, awaiting it, and yielding a single
        text :class:`LlmResponse`. ``model`` preserves the original
        model-string label purely for observability; the actual generation
        is the call_llm's, which is already model-bound.

        The system instruction is read from
        ``llm_request.config.system_instruction`` and the user text is the
        concatenation of every text part across the request's ``contents``
        (mirroring how ADK assembles a turn). This is the symmetric reverse
        of goldfive's forward adapter, which puts ``system`` on
        ``config.system_instruction`` and ``user`` on a single user
        ``Content`` — so a round-trip through both is lossless for the
        text-only turns the harness drives.
        """

        # ``call_llm`` is the harness callable; declared as a pydantic
        # field (BaseLlm is a pydantic BaseModel) so assignment validates.
        call_llm: Any = None

        async def generate_content_async(
            self,
            llm_request: LlmRequest,
            stream: bool = False,
        ) -> AsyncGenerator[Any, None]:
            del stream  # the harness call_llm is non-streaming text-in/out
            # TEXT-ONLY by construction: we read only the system + user TEXT and
            # ignore ``llm_request.config.tools`` (the agent's
            # ``function_declarations``). The reply is a single text part with no
            # ``function_call`` — so an agent on this shim CANNOT call its tools.
            # That is the no-tools limitation that makes this a last resort; the
            # configured-inner-model path keeps tools (see the section banner).
            config = getattr(llm_request, "config", None)
            system = ""
            if config is not None:
                raw_system = getattr(config, "system_instruction", None)
                if isinstance(raw_system, str):
                    system = raw_system
                elif raw_system is not None:
                    # Some ADK shapes carry a Content/list here; flatten its
                    # text parts defensively rather than str()-ing the object.
                    system = _flatten_request_text([raw_system])
            user = _flatten_request_text(getattr(llm_request, "contents", None) or ())
            reply = await self.call_llm(system, user, self.model)
            yield LlmResponse(
                content=genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(text=str(reply))],
                )
            )

    return _CallLlmADKModel


def _flatten_request_text(contents: Iterable[Any]) -> str:
    """Concatenate every text part across an ADK request's ``contents``.

    Walks each ``Content``-shaped item's ``parts`` and joins the non-empty
    ``part.text`` values with newlines, in order. Tolerant of bare strings
    and shapes missing ``parts`` so a malformed request degrades to an empty
    (or best-effort) user string rather than raising inside the model.
    """
    chunks: list[str] = []
    for content in contents:
        if isinstance(content, str):
            if content:
                chunks.append(content)
            continue
        parts = getattr(content, "parts", None) or ()
        for part in parts:
            text = getattr(part, "text", None)
            if text:
                chunks.append(str(text))
    return "\n".join(chunks)


def _iter_agent_tree(root: Any) -> Iterable[Any]:
    """Yield ``root`` and every agent reachable through the ADK tree edges.

    Follows the same edges goldfive's own ADK adapter walks —
    ``sub_agents`` (the native agent tree), ``inner_agent`` (overlay
    wrappers), and ``AgentTool.agent`` (tool-wrapped agents reachable via an
    agent's ``tools``) — so every ``LlmAgent`` that ADK could drive a turn
    on is visited exactly once. Cycle-safe via an id-based visited set.
    """
    seen: set[int] = set()
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        for sub in getattr(node, "sub_agents", None) or ():
            stack.append(sub)
        inner = getattr(node, "inner_agent", None)
        if inner is not None:
            stack.append(inner)
        for tool in getattr(node, "tools", None) or ():
            tool_agent = getattr(tool, "agent", None)
            if tool_agent is not None:
                stack.append(tool_agent)


def _resolves_to_native_function_calling(model_str: str) -> bool:
    """True if ``model_str`` resolves to a real function-calling ``BaseLlm``.

    Answers exactly the question its caller asks — *can a model built from this
    string make tool/function calls?* — and nothing else. ADK's
    :class:`LLMRegistry` is the authority: every class it resolves is a real
    ``BaseLlm`` implementation that carries ``function_declarations`` through to
    its provider (``LiteLlm`` against an OpenAI-compatible endpoint,
    ``Gemini`` / ``Gemma`` against ``google.genai``). So registry-resolvable
    ⇒ ``True``; only a string ADK cannot resolve at all ⇒ ``False``.

    Uses :meth:`LLMRegistry.resolve`, which returns the model *class* without
    instantiating it — so this classifier never constructs (and therefore never
    floods on the garbage-collection of) a ``google.genai`` client.

    This predicate must NOT be written as ``issubclass(cls, LiteLlm)``, which
    conflates "is function-calling capable" with
    "is not a ``google.genai``-backed class" (issue #98). A native ``gemini-*`` / ``gemma-*``
    id resolves to :class:`Gemini` / :class:`Gemma`, NOT a ``LiteLlm``
    subclass — so every native Gemini/Gemma target was judged tool-INCAPABLE
    and its tool agents were rebound to the text-only shim, silently stripping
    every tool. The genai-client-flood concern that tempts one to write it
    that way is a property of CONSTRUCTING such a model rather than of its
    capability, and it is handled on the construction path
    (:func:`_resolves_to_genai_client` in
    :func:`rebind_tree_models_to_call_llm`).

    Returns ``False`` if ADK is unavailable (no native path exists at all).
    """
    try:
        from google.adk.models.registry import LLMRegistry  # noqa: PLC0415
    except ImportError:
        return False
    try:
        LLMRegistry.resolve(model_str)
    except Exception:  # noqa: BLE001 — unresolvable string → shim fallback
        return False
    return True


def _resolves_to_genai_client(model_str: str) -> bool:
    """True if ``model_str`` resolves to a ``google.genai``-backed ADK class.

    The CONSTRUCTION-path concern, kept separate from the capability question
    :func:`_resolves_to_native_function_calling` answers. A bare ``gemini-*`` /
    ``gemma-*`` id resolves to :class:`Gemini` / :class:`Gemma`, whose
    construction builds a :class:`google.genai.Client`. With no Google API key
    present (the vLLM / call_llm path never needs one) that constructor raises
    before it sets ``_async_httpx_client``, and the partial client's
    ``__del__`` floods the log with ``AttributeError`` tracebacks on GC — one
    per turn (see :func:`_build_call_llm_adk_model_class`). Avoiding that flood
    is the ONLY reason the text-only shim ever displaces a resolvable model,
    and it applies only to a TOOL-FREE agent: for a tool-declaring agent the
    shim would be a silent no-op, which is strictly worse than a loud
    credential error.

    Identifies the genai-backed classes POSITIVELY — ``issubclass(cls,
    Gemini)``, which covers :class:`Gemma` (it subclasses :class:`Gemini`) and
    nothing else in the registry. The tempting shorthand ``not
    issubclass(cls, LiteLlm)`` is wrong in two directions: it is unanswerable
    when ``litellm`` is not importable (``google-adk``'s ``extensions`` extra
    owns it, so ADK can resolve ``gemini-*`` in an install that cannot import
    :class:`LiteLlm`), where returning ``False`` would fail OPEN and let the
    flood back in; and it misreads every OTHER native provider class the
    registry grows as genai-backed, displacing a real function-calling model
    for a flood it could never cause.

    ``False`` for a :class:`LiteLlm`-resolvable string (its construction builds
    no genai client), for any other non-genai provider class, for an
    unresolvable string, and when ADK is unavailable.
    """
    try:
        from google.adk.models.google_llm import Gemini  # noqa: PLC0415
        from google.adk.models.registry import LLMRegistry  # noqa: PLC0415
    except ImportError:
        return False
    try:
        cls = LLMRegistry.resolve(model_str)
    except Exception:  # noqa: BLE001 — unresolvable string: not a genai client
        return False
    return issubclass(cls, Gemini)


def _agent_declares_tools(agent: Any) -> bool:
    """True if ``agent`` declares any ``tools=`` entry.

    The hardening backstop's predicate (issue #98): an agent with tools is a
    FUNCTION-CALLING target, and the text-only ``call_llm`` shim can never
    serve it — it sends no ``function_declarations`` and can return no
    ``function_call``, so the tree would degenerate to one text turn while
    still scoring. Read defensively: a plain ``BaseAgent`` (or an overlay
    wrapper) carries no ``tools`` field at all.
    """
    return bool(getattr(agent, "tools", None))


def _inherits_model_from_ancestor(agent: Any) -> bool:
    """True if ``agent`` has a model-carrying ancestor to inherit from.

    An EMPTY ``model`` is NOT "no model" — it is ADK's default, and it means
    "use the nearest ``LlmAgent`` ancestor's model". ``canonical_model`` walks
    ``parent_agent`` for exactly that, so an idiomatic tree names a model on
    the ROOT only and every sub-agent inherits it. This mirrors that walk
    (duck-typed on the ``model`` field, the same test
    :func:`_iter_agent_tree`'s callers use to recognise an ``LlmAgent``-shaped
    node) so :func:`rebind_tree_models_to_call_llm` can leave those agents
    alone: rebinding one would OVERRIDE a binding the root already owns, and
    treating it as modelless would refuse a perfectly well-formed tree.

    ``False`` for a tree root, and for an agent reached through an
    ``AgentTool`` edge (ADK sets no ``parent_agent`` across it, and its
    ``canonical_model`` falls through to ADK's default the same way) — in both
    cases an empty ``model`` genuinely has nothing to inherit.
    """
    parent = getattr(agent, "parent_agent", None)
    while parent is not None:
        if hasattr(parent, "model"):
            return True
        parent = getattr(parent, "parent_agent", None)
    return False


def rebind_tree_models_to_call_llm(root: Any, call_llm: Any) -> int:
    """LAST-RESORT: rebind only TOOL-FREE non-endpoint models to the text shim.

    The third and last ADK-model path (see the section banner): used only when
    no configured inner model (:func:`rebind_tree_models_to_adk_model`) is
    available. Walks the agent tree (root + ``sub_agents`` / ``inner_agent`` /
    ``AgentTool.agent`` edges) and replaces a qualifying agent's bare string
    ``model`` with the TEXT-ONLY :class:`BaseLlm` shim backed by ``call_llm``,
    so ADK's ``canonical_model`` returns it directly and never resolves the
    string through ``LLMRegistry.new_llm`` (which would build the unused,
    flood-causing google.genai client). Because the shim carries no tools,
    every rebound agent loses native tool/function calling — which is why it
    only fires where nothing is lost.

    An agent qualifies for the shim only when it declares NO ``tools=``, owns a
    ``model`` of its own, and that model either resolves to a
    ``google.genai``-backed class (the flood source — see
    :func:`_resolves_to_genai_client`) or does not resolve at all. Four kinds of
    agent are LEFT UNTOUCHED:

    * an agent whose ``model`` is already a :class:`BaseLlm` — an author who
      wired a real model object owns it;
    * an agent whose ``model`` string resolves to a function-calling
      :class:`LiteLlm` (e.g. ``"openai/<model>"`` against a local endpoint) —
      rebinding it to the text-only shim would strip native tool/function
      calling and reduce a tool-calling tree to a single text turn;
    * an agent that DECLARES TOOLS on a registry-resolvable model (including a
      native ``gemini-*`` / ``gemma-*`` id — issue #98). Displacing a real
      function-calling model to dodge the genai-client flood would turn a
      tool-using target into a silent text-only no-op; a loud credential error
      from the real client is strictly better. A ``warning`` names them; and
    * an agent with an EMPTY ``model`` under a model-carrying ancestor, which
      INHERITS that ancestor's binding (see
      :func:`_inherits_model_from_ancestor`). ADK's default ``model`` IS the
      empty string, so the idiomatic multi-agent tree names a model on the root
      only — rebinding a sub-agent would override a binding the root owns, and
      reading its empty model as "no model" would refuse a well-formed tree.

    The hardening backstop: an agent that declares tools and has NO
    function-calling model left — its OWN model string is unresolvable, or it
    has no model and nothing to inherit one from — raises :class:`RuntimeError`
    rather than being quietly rebound. A tool-using target must never silently
    become a text-only no-op that still scores, gates and promotes.

    Returns the number of agents rebound. A no-op (returns 0) when ``call_llm``
    is falsy. Idempotent — a second pass finds every model already a
    ``BaseLlm`` (or left untouched by the rules above) and rebinds none.
    """
    if not call_llm:
        return 0
    from google.adk.models import BaseLlm  # noqa: PLC0415

    model_cls = _build_call_llm_adk_model_class()
    rebound: list[tuple[str, str]] = []  # (agent name, why the shim was needed)
    kept_native: list[str] = []  # tool agents kept on a genai-backed model
    for agent in _iter_agent_tree(root):
        # Only ``LlmAgent``-shaped nodes carry a ``model``; a plain
        # ``BaseAgent`` (or an overlay wrapper) simply has no such field.
        if not hasattr(agent, "model"):
            continue
        current = agent.model
        if isinstance(current, BaseLlm):
            continue  # author-supplied model object — leave it.
        name = str(getattr(agent, "name", "?"))
        model_str = current if isinstance(current, str) and current else ""
        if not model_str and _inherits_model_from_ancestor(agent):
            # An empty ``model`` under a model-carrying ancestor INHERITS that
            # ancestor's binding (ADK's ``canonical_model`` walk). Whatever the
            # ancestor ends up on — its own native model, or the shim if it
            # qualified — governs here too, so this node is not ours to touch.
            continue
        declares_tools = _agent_declares_tools(agent)
        if model_str and _resolves_to_native_function_calling(model_str):
            if not _resolves_to_genai_client(model_str):
                continue  # real LiteLlm endpoint model — keep native tool-calling.
            if declares_tools:
                # A native Gemini/Gemma tool agent keeps its model
                # (issue #98). The
                # shim would strip its tools silently; the genai client's own
                # "No API key" failure is loud and diagnosable.
                kept_native.append(name)
                continue
            reason = "a google.genai-backed model with no configured endpoint"
        else:
            reason = f"an unresolvable model string {model_str!r}" if model_str else "no model"
        if declares_tools:
            raise RuntimeError(
                f"ADK model binding: agent {name!r} declares tools but has "
                f"{reason} — the only remaining path is the TEXT-ONLY harness "
                f"call_llm shim, which sends no function_declarations and can "
                f"return no function_call. Rebinding it would silently reduce "
                f"this tool-using tree to a single text turn that still scores, "
                f"gates and promotes. Configure a function-calling model for the "
                f"inner harness (models.harness — e.g. 'openai/<model>' against "
                f"your endpoint with the 'adk' extra, or a native 'gemini-*' id "
                f"with Google credentials) instead."
            )
        agent.model = model_cls(model=model_str or "call-llm", call_llm=call_llm)
        rebound.append((name, reason))
    if rebound:
        log.warning(
            "ADK model binding: %d tool-free agent(s) %s were routed through "
            "the harness call_llm TEXT-ONLY shim (each had %s). Those agents "
            "can no longer make tool/function calls — the shim carries no "
            "function_declarations — so any tool or sub-agent transfer they "
            "would have driven is INERT; they answer in one text turn. "
            "Configure an inner-harness model (models.harness — e.g. "
            "'openai/<model>' against your endpoint with the 'adk' extra) to "
            "run them on a real function-calling model.",
            len(rebound),
            [name for name, _ in rebound],
            "; ".join(sorted({reason for _, reason in rebound})),
        )
    if kept_native:
        log.warning(
            "ADK model binding: %d tool-declaring agent(s) %s were LEFT on "
            "their native google.genai model string rather than routed through "
            "the text-only harness call_llm shim, which would have stripped "
            "their tools. Their turns need Google credentials (or a configured "
            "models.harness endpoint model); without them ADK's genai client "
            "fails loudly per turn.",
            len(kept_native),
            kept_native,
        )
    return len(rebound)


def rebind_tree_models_to_adk_model(root: Any, model: Any) -> int:
    """Rebind every string-model ``LlmAgent`` in ``root``'s tree to ``model``.

    The config-driven counterpart to :func:`rebind_tree_models_to_call_llm`:
    when the workspace configures an inner agent model (a ``models.harness``
    model spec built into a :class:`BaseLlm` — typically a function-calling
    :class:`LiteLlm` pointed at the live endpoint), the adapter injects that
    object into every string-model agent. The configured model is the source
    of truth for the inner harness, so it overrides whatever bare model string
    the target hardcoded (e.g. the example target's ``"openai/gpt-4o-mini"``
    default) — unlike the shim path, this keeps native tool/function calling.

    Agents whose ``model`` is already a :class:`BaseLlm` are left untouched —
    an author who wired a real model object owns it. The same ``model``
    instance is shared across the tree (a ``BaseLlm`` is stateless per call,
    exactly as a normal multi-agent ADK app shares one model). Returns the
    number of agents rebound; a no-op (returns 0) when ``model`` is falsy.
    """
    if model is None:
        return 0
    from google.adk.models import BaseLlm  # noqa: PLC0415

    rebound = 0
    for agent in _iter_agent_tree(root):
        if not hasattr(agent, "model"):
            continue
        if isinstance(agent.model, BaseLlm):
            continue  # author-supplied model object — leave it.
        agent.model = model
        rebound += 1
    return rebound


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


def entry_disable_drift(entry: BoardEntry) -> tuple[Any, ...]:
    """Return the drift kinds the board wants suppressed for ``entry``.

    ``disable_drift`` is a board-LEVEL setting (``Board.disable_drift``),
    but the :class:`~zicato.adapters.base.RunnableHarness` Protocol hands
    the adapter a :class:`BoardEntry` rather than the owning ``Board``. The
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


#: ``BoardEntry.context`` key the tournament runner stamps the
#: board-level ``judge_only`` flag under. Kept in sync with
#: ``zicato.tournament.runner._JUDGE_ONLY_CONTEXT_KEY`` — the two ends
#: meet on this single string.
_JUDGE_ONLY_CONTEXT_KEY = "judge_only"


def entry_judge_only(entry: BoardEntry) -> bool:
    """Return whether ``entry`` should run in judge-only (no-steering) mode.

    ``judge_only`` is a board-LEVEL setting (``Board.judge_only``), but
    the :class:`~zicato.adapters.base.RunnableHarness` Protocol hands the
    adapter a :class:`BoardEntry` rather than the owning ``Board``. The
    tournament runner therefore stamps the flag onto every entry's
    :attr:`~zicato.core.BoardEntry.context` mapping under
    :data:`_JUDGE_ONLY_CONTEXT_KEY` (see
    ``zicato.tournament.runner._stamp_judge_only``) — ``context`` is the
    one per-entry channel that survives the runner -> subprocess worker
    -> :func:`zicato.core.validate_board_entry` round-trip.

    The value is the lowercase wire string ``"true"`` / ``"false"``.
    Returns ``False`` when the entry carries no such key (the default,
    steering-on path).
    """
    raw = (getattr(entry, "context", {}) or {}).get(_JUDGE_ONLY_CONTEXT_KEY)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() == "true"


def _build_judge_only_steerer(call_llm: Any, runtime: Any) -> Any:
    """Build the :class:`DefaultSteerer` subclass that suppresses refine.

    Under judge-only the goal is ZERO steering AND ZERO refine attempts
    while the scoring signal is preserved. The ``StaticPlanner`` alone
    does not achieve that: goldfive dispatches every non-INFO drift
    through ``DriftObserver.handle_drift`` → the cancel + refine ladder,
    and a ``StaticPlanner.refine`` returning ``None`` is treated as
    handler exhaustion → ``HUMAN_INTERVENTION_REQUIRED`` escalation (which
    emits ``refine_attempted`` / ``refine_failed`` and, on the persona
    path, spins to the wall-clock budget).

    The fix is a steerer whose drift observer's ``handle_drift`` emits the
    ``DriftDetected`` event — the SAME signal zicato's reducer reads to
    attribute custom-judge loss by ``judge_name`` — and then returns,
    skipping the entire cancel / promote / refine machinery. This mirrors
    goldfive's own "emit for observability, skip dispatch" early-return
    pattern (used on its redundant-verdict and concurrent-refine guards).
    ``JudgementEmitted`` is published earlier in
    ``DefaultSteerer.evaluate_judges`` and is therefore unaffected, so the
    paired judgement + custom drift the reducer needs both still land.

    Detection stays fully wired. ``goldfive.wrap`` builds its default
    steerer with the judge call_llm + the runtime's drift configs (see
    its ``resolved_steerer = DefaultSteerer(...)`` branch); passing an
    explicit ``steerer=`` SKIPS that construction, so this factory must
    reproduce the same wiring or the built-in goal-/reasoning-drift
    detectors silently go inert and the scalar changes. We therefore
    thread the adapter's harness ``call_llm`` (which is exactly what
    ``goldfive.wrap`` resolves the judge callable to when the caller
    passes ``call_llm=`` explicitly, as the adapter does) and the
    resolved ``runtime`` into the same constructor kwargs. The ONLY
    behavioural delta from goldfive's default steerer is the neutered
    ``handle_drift``: detectors fire and emit ``DriftDetected`` as they do
    under goldfive's steerer, and only the refine ladder is skipped.

    Imported lazily so the optional goldfive dependency stays out of this
    module's import time. The subclass is defined inside the factory so
    the ``DefaultSteerer`` base symbol is resolved at call time.
    """
    from goldfive.steerer import DefaultSteerer  # noqa: PLC0415

    class _JudgeOnlySteerer(DefaultSteerer):
        """``DefaultSteerer`` that observes + judges but never refines.

        Rebinds the bound :class:`~goldfive.drift_observer.DriftObserver`'s
        ``handle_drift`` to a variant that emits ``DriftDetected`` (so the
        scalar reducer still sees the custom-judge-attributed drift) and
        returns before the refine ladder. Everything else — judge
        invocation, ``JudgementEmitted`` emission, sink fan-out, the
        built-in detector wiring — is inherited unchanged.
        """

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            observer = self.drift

            # ``DriftObserver.handle_drift`` is the single chokepoint every
            # drift (built-in detector OR custom-judge verdict) flows
            # through before the cancel + refine ladder. Replace it on the
            # instance with an observe-only variant. ``_emit_drift_detected``
            # is the canonical ``DriftDetected`` emit the reducer keys on.
            async def _observe_only_handle_drift(drift: Any, session: Any) -> None:
                await observer._emit_drift_detected(session, drift)

            observer.handle_drift = _observe_only_handle_drift  # type: ignore[method-assign]

    # Mirror ``goldfive.wrap``'s default-steerer construction so the
    # built-in goal-/reasoning-drift detectors stay wired identically.
    # Each config field is read defensively: a goldfive RuntimeConfig
    # revision that renames a sub-config must not crash judge-only — a
    # missing field falls back to the steerer's own default.
    kwargs: dict[str, Any] = {}
    if call_llm is not None:
        kwargs["goal_drift_call_llm"] = call_llm
        kwargs["reasoning_drift_call_llm"] = call_llm
    goal_drift = getattr(runtime, "goal_drift", None)
    if goal_drift is not None:
        kwargs["goal_drift_config"] = goal_drift
    tool_loops = getattr(runtime, "tool_loops", None)
    if tool_loops is not None:
        kwargs["tool_loop_config"] = tool_loops
    reasoning_drift = getattr(runtime, "reasoning_drift", None)
    if reasoning_drift is not None:
        kwargs["reasoning_drift_config"] = reasoning_drift
        mode = getattr(reasoning_drift, "mode", None)
        if mode is not None:
            kwargs["reasoning_drift_mode"] = mode
    steering = getattr(runtime, "steering", None)
    if steering is not None:
        kwargs["steering_config"] = steering
    return _JudgeOnlySteerer(**kwargs)


def _judge_only_overrides(agent: Any, call_llm: Any, runtime: Any) -> dict[str, Any]:
    """Build the ``goldfive.run`` kwargs that turn STEERING off, JUDGING on.

    Judge-only evaluation keeps goldfive's drift / process judges armed
    (``reasoning_drift_mode="judge"`` is goldfive's default and is left
    untouched) while removing every steering LLM call AND every refine
    attempt:

    * ``goal_deriver=LiteralGoalDeriver()`` — the entry input becomes the
      goal verbatim, so no ``goal_derive`` LLM call fires (goldfive's
      default :class:`LLMGoalDeriver` would call the LLM here).
    * ``planner=StaticPlanner(<one task>)`` — installs a single static
      task assigned to the root agent so the native agent tree runs on
      goldfive's overlay path (``invoke_passthrough``) and produces a
      transcript to judge. :class:`StaticPlanner.generate` returns that
      fixed plan; its ``refine`` / ``handle_turn`` return ``None``, so no
      replanning LLM call ever fires. (Goldfive's default
      :class:`LLMPlanner` would replan/refine via the LLM.)
      ``PassthroughPlanner`` is NOT used: its ``generate``
      returns ``None`` and aborts the run with an empty transcript,
      leaving nothing to judge.
    * ``steerer=_build_judge_only_steerer()`` — a :class:`DefaultSteerer`
      subclass whose ``handle_drift`` emits ``DriftDetected`` (preserving
      the custom-judge scoring signal) but skips the cancel + refine
      ladder. WITHOUT this, the ``StaticPlanner``'s ``None`` refine is
      mis-read by goldfive as handler exhaustion → ``refine_attempted`` /
      ``refine_failed`` → ``HUMAN_INTERVENTION_REQUIRED`` escalation,
      which is exactly the ``multi_turn_emulated`` abort spiral this mode
      must avoid. See :func:`_build_judge_only_steerer`.

    Symbols are imported lazily so the optional goldfive dependency stays
    out of this module's import time (matching the lazy-import discipline
    used at the call sites).

    Empirically (goldfive installed in zicato's ``.venv``): with this set
    the only ``goldfive_llm_call_start`` events carry judge names
    (e.g. ``judge_goal_drift``); the steering ``goal_derive`` /
    ``refine`` / ``refine_steer`` call names never appear, and NO
    ``refine_attempted`` event is emitted on any path. The
    ``goal_derived`` / ``plan_revised`` *bookkeeping* events still fire
    once (literal goal recorded; the static plan's "initial plan install"
    revision) but neither is LLM-driven nor a refine.
    """
    from goldfive import StaticPlanner  # noqa: PLC0415
    from goldfive.goal_deriver import LiteralGoalDeriver  # noqa: PLC0415
    from goldfive.types import Plan, Task  # noqa: PLC0415

    one_task_plan = Plan(
        id="zicato-judge-only",
        run_id="",
        goal_ids=("g1",),
        tasks=(
            Task(
                id="t1",
                title="accomplish the user's request",
                description="accomplish the user's request",
                # The overlay's passthrough dispatch routes to the agent
                # whose name matches the task assignee — the root agent.
                assignee_agent_id=agent.name,
            ),
        ),
        edges=(),
        summary="judge-only: run the native agent tree once, no steering",
    )
    return {
        "planner": StaticPlanner(one_task_plan),
        "goal_deriver": LiteralGoalDeriver(),
        "steerer": _build_judge_only_steerer(call_llm, runtime),
    }


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

    __slots__ = ("_agent", "_entrypoint_file", "_generation_root", "_mutable_trees")

    def __init__(
        self,
        agent: Any,
        mutable_trees: list[Path],
        entrypoint_file: str = "",
        generation_root: Path | None = None,
    ) -> None:
        """Bind a loaded ADK ``agent`` and remember the mutable-tree set.

        ``mutable_trees`` is kept on the runnable for diagnostics and for
        the post-run :meth:`tree_import_status` check; the runner does not
        consult it on the runnable, only on the adapter that produced this
        instance.

        ``entrypoint_file`` is the ``__file__`` the entrypoint module
        actually resolved to under the generation snapshot — the value
        :meth:`ADKHarnessAdapter.load` asserted is under the snapshot. The
        subprocess worker records it per generation so an operator can
        audit WHICH file a generation ran (issue #110). Empty for the
        dependency shape (the entrypoint legitimately lives outside every
        tree, so there is no snapshot-relative file to name) and for a
        directly-constructed runnable.

        ``generation_root`` is the snapshot this agent was loaded from,
        remembered so :meth:`tree_import_status` can answer the post-run
        question without the caller re-deriving it. ``None`` for a
        directly-constructed runnable, which then reports no tree status.
        """
        self._agent = agent
        self._mutable_trees = list(mutable_trees)
        self._entrypoint_file = str(entrypoint_file or "")
        self._generation_root = Path(generation_root) if generation_root is not None else None

    @property
    def entrypoint_file(self) -> str:
        """The ``__file__`` the entrypoint resolved to under the snapshot.

        Empty when the runnable was constructed without one, and for the
        dependency shape (entrypoint outside every mutable tree). Read
        best-effort by the worker for the round log's ``harness_loaded``
        provenance; nothing in the run path depends on it.
        """
        return self._entrypoint_file

    def tree_import_status(self) -> dict[str, str]:
        """Per-tree post-run verdict: ``{tree_basename: verdict}``.

        The runnable's view of :func:`tree_import_status` — the POST-RUN truth
        layer of the mutated-tree invariant. Called by the subprocess worker
        once a unit's run has finished, when ``sys.modules`` records what the
        run actually imported: a tree imported from under this generation's
        snapshot was genuinely under test, one imported from outside was not
        (and fails the run), and one never imported means the mutations to it
        could not have been exercised by that unit.

        Empty for a runnable with no remembered snapshot or no declared trees
        — there is then nothing to attribute, and the entrypoint assert in
        :meth:`ADKHarnessAdapter.load` is the whole verification.
        """
        if self._generation_root is None:
            return {}
        return tree_import_status(
            (Path(tree).name for tree in self._mutable_trees),
            self._generation_root,
        )

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

        ONE deliberate exception to that invariant: the model-binding step
        below runs BEFORE the guarded block and RAISES when a tool-declaring
        agent has no function-calling model left (issue #98). That is a
        target misconfiguration rather than a run outcome — a tool-using tree
        driven by the text-only shim would produce a plausible one-turn
        transcript and SCORE it. Failing the run (the worker surfaces it as
        an infra abort) is the fail-closed answer.
        """
        run_id = uuid.uuid4().hex
        budget_s = float(entry.wall_clock_budget_seconds)
        started_at = time.monotonic()

        # Bind the loaded tree's string-model agents to a working model BEFORE
        # any goldfive.run dispatch. Two paths, config-driven first:
        #
        # 1. A configured inner model (config.inner_model — a BaseLlm/LiteLlm
        #    built from a models.harness spec) is injected into every
        #    string-model agent. This is the preferred, idiomatic path: the
        #    agents reach the live endpoint with native tool/function calling
        #    intact, overriding the target's hardcoded default model string.
        # 2. Otherwise, fall back to the guarded call_llm shim rebind: only a
        #    TOOL-FREE agent whose bare string is unresolvable, or would build
        #    an unused google.genai client (a "gemma-*"/"gemini-*" id that
        #    floods the log with AttributeError('_async_httpx_client')
        #    tracebacks on GC), is routed through the harness call_llm. Any
        #    registry-resolvable model is left for ADK so native tool-calling
        #    survives, and a TOOL-DECLARING agent is never shimmed — routing it
        #    through the text-only shim would reduce a tool-calling tree to a
        #    single text turn (the presentation target writes no files then);
        #    with no function-calling model left it raises instead
        #    (issue #98).
        #
        # See rebind_tree_models_to_adk_model / rebind_tree_models_to_call_llm /
        # _resolves_to_native_function_calling. Both are idempotent.
        if getattr(config, "inner_model", None) is not None:
            rebind_tree_models_to_adk_model(self._agent, config.inner_model)
        else:
            rebind_tree_models_to_call_llm(self._agent, config.harness_call_llm)

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
            disable_drift=entry_disable_drift(entry),
            aux_call_llm=config.effective_judge_call_llm(),
            # Board reflection's verbatim-capture seam: the worker binds a
            # per-run judge-I/O sink onto the config when persist_judge_io
            # is on; None (the default) captures nothing (byte-identical).
            io_sink=getattr(config, "judge_io_sink", None),
        )
        # Judge-only mode: spread in the no-steering overrides
        # (StaticPlanner + LiteralGoalDeriver) so goldfive judges without
        # deriving goals, replanning, or refining. Judges stay armed in
        # both paths. When off (the default), the call takes goldfive's
        # ordinary steering path unchanged.
        gf_runtime = _goldfive_runtime()
        overrides = (
            _judge_only_overrides(self._agent, config.harness_call_llm, gf_runtime)
            if entry_judge_only(entry)
            else {}
        )
        outcome = await goldfive.run(
            self._agent,
            entry.input,
            sinks=sinks,
            call_llm=config.harness_call_llm,
            judges=judges,
            runtime=gf_runtime,
            **overrides,
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
            disable_drift=entry_disable_drift(entry),
            aux_call_llm=config.effective_judge_call_llm(),
            # Board reflection's verbatim-capture seam: the worker binds a
            # per-run judge-I/O sink onto the config when persist_judge_io
            # is on; None (the default) captures nothing (byte-identical).
            io_sink=getattr(config, "judge_io_sink", None),
        )
        agent = self._agent
        gf_runtime = _goldfive_runtime()
        # Judge-only overrides (no-steering, no-refine) applied to every
        # per-turn goldfive.run call; empty dict on the default steering
        # path. Built ONCE (the judge-only steerer is reused across turns,
        # which goldfive explicitly supports — it unwires per-run state at
        # each run boundary).
        overrides = (
            _judge_only_overrides(agent, config.harness_call_llm, gf_runtime)
            if entry_judge_only(entry)
            else {}
        )

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
                    runtime=gf_runtime,
                    **overrides,
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

        The emulator driver (:func:`zicato.emulator.run_emulated`)
        invokes its ``agent`` argument once per emulated turn as
        ``agent.run(user_message)`` with a bare string. Passing the raw
        ADK agent would raise :class:`TypeError` because the ADK
        agent's ``.run()`` does not accept a bare string positional —
        it expects ADK-specific invocation arguments. The scripted path
        wraps the agent for the same reason (issue #105). So the emulated
        path wraps it too: a thin
        per-turn caller that calls :func:`goldfive.run` with the
        correct signature on each emulated turn — mirroring
        :class:`_PerTurnCaller` in :meth:`_run_multi_turn_scripted`.
        """
        import goldfive  # lazy: keep the optional dep out of import time

        from zicato.judge_runtime import assemble_judges

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

        judges = assemble_judges(
            entry_judges=_entry_judge_specs(entry),
            disable_drift=entry_disable_drift(entry),
            aux_call_llm=config.effective_judge_call_llm(),
            # Board reflection's verbatim-capture seam: the worker binds a
            # per-run judge-I/O sink onto the config when persist_judge_io
            # is on; None (the default) captures nothing (byte-identical).
            io_sink=getattr(config, "judge_io_sink", None),
        )
        agent = self._agent
        gf_runtime = _goldfive_runtime()
        # Judge-only overrides (no-steering, no-refine) applied to every
        # per-turn goldfive.run call; empty dict on the default steering
        # path. This is the path the picky-stakeholder persona runs on —
        # without the refine-suppressing steerer in these overrides the
        # CRITICAL no_fabricated_numbers judge's verdict was promoted to a
        # drift, the StaticPlanner's None-refine was mis-read as handler
        # exhaustion, and the run escalated to HUMAN_INTERVENTION_REQUIRED
        # and spun to the 900s wall-clock. Built ONCE; the judge-only
        # steerer is reused across turns (goldfive supports a shared
        # steerer — it unwires per-run state at each run boundary).
        overrides = (
            _judge_only_overrides(agent, config.harness_call_llm, gf_runtime)
            if entry_judge_only(entry)
            else {}
        )

        class _PerTurnCaller:
            """Thin wrapper that calls ``goldfive.run`` per emulated turn.

            The emulator driver calls ``agent.run(user_message)``; this
            wrapper satisfies that interface and dispatches each call to
            ``goldfive.run(agent, user_message, ...)`` with the correct
            ADK-level arguments. The return value is the last
            user-facing assistant reply extracted from the outcome's
            ``completed_results``, matching the extraction path used by
            :meth:`_run_single_turn`. Identical in shape to the
            scripted path's wrapper — the emulator driver and the
            scripted driver both expect an ``async run(str) -> str``.
            """

            async def run(self, user_message: str) -> str:
                outcome = await goldfive.run(
                    agent,
                    user_message,
                    sinks=sinks,
                    call_llm=config.harness_call_llm,
                    judges=judges,
                    runtime=gf_runtime,
                    **overrides,
                )
                transcript = _outcome_transcript(outcome)
                return transcript[-1] if transcript else ""

        return await emulator.run_emulated(
            agent=_PerTurnCaller(),
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

    The ``mutable_trees`` paths are *absolute* (resolved at construction
    against the registered source). The mutable surface inside a given
    generation snapshot is obtained by :meth:`mutable_subpaths`, which
    re-bases each ``mutable_trees`` entry onto the snapshot root.

    Conforms to :class:`~zicato.adapters.base.HarnessAdapter`
    structurally; the Protocol's ``runtime_checkable`` check passes
    without inheritance.
    """

    name: str = "adk"

    #: Names the inner harness writes run output under, excluded from
    #: every generation copy by :mod:`zicato.epoch.snapshot_scope`.
    #: ``"output"`` is already in the standing artifact set; declaring it
    #: here keeps the adapter contract explicit and lets the directory /
    #: git stores honour an adapter-specific name without a code change.
    run_output_names: tuple[str, ...] = ("output",)

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

    def mutable_subpaths(self, generation_root: Path) -> list[Path]:
        """Re-base the adapter's mutable trees onto a concrete snapshot root.

        Each construction-time ``mutable_trees`` entry is an absolute
        path under the *registered* source. A generation snapshot copies
        every registered tree under its basename, so the mutable surface
        inside ``generation_root`` is ``generation_root / <basename>``
        for each registered tree.

        Returns only the sub-paths that exist under ``generation_root``.
        Falls back to ``[generation_root]`` when the adapter has no
        ``mutable_trees`` declaration at all — the whole snapshot is then
        the surface, matching the pre-narrowing default behaviour.
        """
        root = Path(generation_root).resolve()
        if not self.mutable_trees:
            return [root]
        subpaths: list[Path] = []
        for tree in self.mutable_trees:
            candidate = root / Path(tree).name
            if candidate.exists():
                subpaths.append(candidate)
        # Every registered tree missing from the snapshot is a degenerate
        # case (e.g. an empty snapshot); fall back to the whole root so
        # enumeration still has something to walk.
        return subpaths or [root]

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

        Fails CLOSED on a tree that cannot be running from the snapshot.
        Prepending the snapshot to ``sys.path`` shadows only the TOP-LEVEL
        names the snapshot itself contains — a name absent from it falls
        through to its installed location and runs the UNMUTATED copy,
        making every round a no-op that still scores, gates, promotes and
        reports (issue #110). Two asserts, both :class:`RuntimeError`:

        * EVERY registered mutable tree's top-level name must resolve
          under ``generation_root`` — already-imported (a pre-imported
          installed copy is the shadowing case) or resolvable by
          :func:`importlib.util.find_spec`, which finds without executing
          the target's code. This is the assert that covers a tree the
          entrypoint merely depends on, and every tree of a multi-tree
          registration rather than only the entrypoint's own.
        * When the entrypoint's top-level component IS one of those tree
          names, the imported module's ``__file__`` must be under
          ``generation_root`` too. For the dependency shape (entrypoint
          outside every tree — target 2) that assert does not apply: the
          harness module legitimately lives elsewhere, and the per-tree
          assert above plus the post-run :func:`tree_import_status` record
          are what verify the mutations were under test.

        DETECT rather than repair: a second ``load`` in the SAME process against
        a different ``generation_root`` still resolves a dotted entrypoint
        to the first generation's files (``reload`` re-runs the finder
        against the parent package's unchanged ``__path__``) and therefore
        raises. The fresh-process-per-generation contract above is what
        makes that unreachable in the worker; nothing here relies on
        reload picking up a new root.

        We do not restore ``sys.path`` or ``sys.modules`` — see this
        module's docstring on the fresh-process-per-generation
        contract.
        """
        # Lazy import: keep these optional at zicato.adapters import time.
        import goldfive  # noqa: F401 — surface the dep here so missing extra fails clean
        import google.adk  # noqa: F401 — same; google-adk is the ADK extra

        root = Path(generation_root).resolve()
        root_str = str(root)
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

        entrypoint_file = ""
        if self._entrypoint_is_in_a_tree():
            entrypoint_file = self._assert_loaded_from_snapshot(module, root)
        self._assert_trees_resolve_in_snapshot(root)

        try:
            agent = getattr(module, self._symbol)
        except AttributeError as exc:
            # Still an AttributeError: the subprocess worker catches this
            # type by construction. Only the message improves.
            detail = explain_attribute_error(module, self._symbol, exc)
            if detail is not None:
                raise AttributeError(
                    f"ADKHarnessAdapter: entrypoint module {self._module_path!r}: "
                    f"{detail} (loaded from "
                    f"{getattr(module, '__file__', '<unknown>')!r})"
                ) from exc
            raise AttributeError(
                f"ADKHarnessAdapter: entrypoint module {self._module_path!r} "
                f"has no symbol {self._symbol!r} (loaded from "
                f"{getattr(module, '__file__', '<unknown>')!r})"
            ) from exc

        return ADKRunnableHarness(
            agent=agent,
            mutable_trees=list(self.mutable_trees),
            entrypoint_file=entrypoint_file,
            generation_root=root,
        )

    def tree_basenames(self) -> list[str]:
        """The top-level names the snapshot exposes this adapter's trees under.

        One per registered mutable tree, deduplicated and in registration
        order. ``mutable_trees`` is already filesystem-resolved at
        construction, so a plain ``Path.name`` here is the same value
        :func:`_tree_basename` computes at register time and
        ``seed_generation`` materialises.
        """
        return list(dict.fromkeys(Path(tree).name for tree in self.mutable_trees))

    def _entrypoint_is_in_a_tree(self) -> bool:
        """True when the entrypoint's top-level component names a mutable tree.

        The in-tree shape, where "the entrypoint came from the snapshot" and
        "the mutated tree is what runs" coincide, so
        :meth:`_assert_loaded_from_snapshot` applies. ``False`` is the
        dependency shape (see :func:`entrypoint_outside_trees_notice`), where
        the entrypoint's origin carries no information about the mutations and
        the per-tree asserts are the whole verification.

        An adapter with NO declared trees counts as in-tree: the mutable
        surface is then the entire snapshot (:meth:`mutable_subpaths` falls
        back to ``[generation_root]``), so the entrypoint's own origin is the
        only signal there is and it must be under the root.
        """
        basenames = self.tree_basenames()
        if not basenames:
            return True
        return self._module_path.split(".")[0] in basenames

    def _assert_trees_resolve_in_snapshot(self, generation_root: Path) -> None:
        """Assert EVERY mutable tree's top-level name resolves in the snapshot.

        The load-time half of the mutated-tree invariant, and the layer that
        closes the multi-tree hole the entrypoint assert leaves: with trees
        ``[agent, otherpkg]`` and entrypoint ``agent.agent:root_agent``, the
        entrypoint check passes while every mutation to ``otherpkg`` is a
        silent scored no-op if ``otherpkg`` resolves to an installed copy.

        Per tree, fail closed:

        * ALREADY IMPORTED (``sys.modules``) — its locations must be under
          ``generation_root``. A pre-imported installed copy short-circuits the
          path search entirely, which is exactly the shadowing case; the
          message names the shadowing file.
        * NOT YET IMPORTED — :func:`importlib.util.find_spec` must resolve it
          under ``generation_root``. Pure RESOLUTION: the finder runs, the
          target's module body does not, so this cannot perturb what the run
          then imports. An unresolvable or elsewhere-resolving name raises.

        Raises :class:`RuntimeError` naming the tree, what it resolved to, and
        the registration that fixes it.
        """
        root = Path(generation_root).resolve()
        for basename in self.tree_basenames():
            module = sys.modules.get(basename)
            if module is not None:
                locations = _module_locations(module)
                if _all_under(locations, root):
                    continue
                shadow = str(locations[0]) if locations else "<no __file__>"
                raise RuntimeError(
                    f"ADKHarnessAdapter: mutable tree {basename!r} was ALREADY "
                    f"IMPORTED from {shadow!r}, which is NOT under the generation "
                    f"snapshot {str(root)!r} — a module already in sys.modules "
                    f"short-circuits the path search, so the snapshot's mutated "
                    f"copy can never load and every mutation to this tree is a "
                    f"silent scored no-op (issue #110). The tree must not be "
                    f"pre-imported before the adapter loads (the tournament "
                    f"worker's contract is a fresh process per generation)."
                )
            try:
                spec = importlib.util.find_spec(basename)
            except (ImportError, ValueError) as exc:
                raise RuntimeError(
                    f"ADKHarnessAdapter: mutable tree {basename!r} does not "
                    f"resolve as a top-level module ({exc}) — the generation "
                    f"snapshot {str(root)!r} is first on sys.path, so a tree it "
                    f"exposes must resolve there; every mutation to this tree "
                    f"would be a scored no-op. Register the tree's IMPORTABLE "
                    f"package directory (--mutable-tree <...>/{basename})."
                ) from exc
            locations = _spec_locations(spec)
            if _all_under(locations, root):
                continue
            resolved = str(locations[0]) if locations else "<unresolvable>"
            raise RuntimeError(
                f"ADKHarnessAdapter: mutable tree {basename!r} resolves to "
                f"{resolved!r}, which is NOT under the generation snapshot "
                f"{str(root)!r} — the mutated copy of this tree is not what "
                f"would run, so every mutation to it is a silent scored no-op "
                f"(issue #110). sys.path resolves TOP-LEVEL names only and the "
                f"snapshot exposes each tree under its basename, so "
                f"{basename!r} must be the importable package name "
                f"(--mutable-tree <...>/{basename}) and must not be shadowed by "
                f"an earlier sys.path entry."
            )

    def _assert_loaded_from_snapshot(self, module: Any, generation_root: Path) -> str:
        """Return ``module.__file__``, asserting it lies under the snapshot.

        The load-time half of the snapshot-origin invariant (issue #110).
        Raises :class:`RuntimeError` when the resolved module did not come
        from ``generation_root`` — including the namespace-package /
        builtin case where there is no ``__file__`` to check at all. The
        message names the file that WAS resolved, the root it had to be
        under, and the register-time rule that fixes the registration, so
        the operator can act without reading this source.
        """
        raw_file = getattr(module, "__file__", None)
        resolved = Path(raw_file).resolve() if raw_file else None
        if resolved is not None and resolved.is_relative_to(generation_root):
            return str(resolved)
        top_level = self._module_path.split(".")[0]
        expected = [Path(tree).name for tree in self.mutable_trees]
        raise RuntimeError(
            f"ADKHarnessAdapter: entrypoint {self._entrypoint!r} resolved to "
            f"{str(resolved) if resolved is not None else '<no __file__>'!r}, "
            f"which is NOT under the generation snapshot {str(generation_root)!r} "
            f"— the mutated code under test was never loaded, so this run would "
            f"score an unmutated copy (every round a silent no-op). Prepending "
            f"the snapshot to sys.path shadows only the TOP-LEVEL names the "
            f"snapshot itself contains, so top-level module {top_level!r} fell "
            f"through to its installed location. Register the entrypoint "
            f"relative to a mutable tree: a "
            f"snapshot copies each tree under its basename, so the entrypoint's "
            f"top-level module must be one of {expected!r} "
            f"(`zicato epoch register --adk <tree_basename>.<module>:<symbol> "
            f"--mutable-tree <tree>`)."
        )

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
        #
        # Use importlib.import_module explicitly (rather than a
        # from-import) so test-side monkeypatches of
        # importlib.import_module can intercept this call to simulate
        # the module being absent.
        try:
            enumerator_mod = importlib.import_module("zicato.mutation.enumerator")
            enumerate_mutations = enumerator_mod.enumerate_mutations
        except ImportError as exc:
            raise ImportError(
                "ADKHarnessAdapter.mutation_points requires "
                "zicato.mutation.enumerator.enumerate_mutations; the "
                "mutation enumerator module is not yet available."
            ) from exc

        return _coerce_to_list(enumerate_mutations(roots))

    async def on_promote(
        self,
        *,
        epoch_id: str,
        generation_id: str,
        parent_generation_id: str | None,
        snapshot_root: Path,
        workspace_root: Path,
    ) -> None:
        """No-op: an ADK tree's evolved state IS the snapshot (issue #125).

        The post-promotion hook exists for targets whose real state
        lives somewhere the mutable tree cannot reach. An ADK tree has
        no such state — the promoted snapshot is the whole artifact, and
        the champion marker already names it — so there is nothing to
        commit. Declared explicitly rather than omitted so this adapter
        keeps satisfying the full :class:`HarnessAdapter` surface for the
        type checker, which (unlike the runtime ``isinstance`` gate) has
        no notion of :data:`~zicato.adapters.base.OPTIONAL_ADAPTER_MEMBERS`.
        """
        return None


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
    "TREE_IMPORT_NEVER_IMPORTED",
    "TREE_IMPORT_OUTSIDE_ROOT",
    "TREE_IMPORT_VERIFIED",
    "entry_disable_drift",
    "entry_judge_only",
    "entrypoint_outside_trees_notice",
    "entrypoint_snapshot_origin_error",
    "tree_import_status",
    "ADKRunnableHarness",
    "rebind_tree_models_to_adk_model",
    "rebind_tree_models_to_call_llm",
]
