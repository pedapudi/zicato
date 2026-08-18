"""Path math and construction-time invariants for the ``.zicato/`` workspace.

Pure path-resolution helpers — no I/O, no directory creation, no file
reads. Callers compose these helpers and then perform whatever I/O they
need; that separation keeps the path layout testable without a tmpdir
and lets the CLI introspect paths (e.g. ``zicato paths``) without
touching the filesystem.

The canonical layout these helpers produce::

    {workspace_root}/
      epochs/{epoch_id}/
        board.jsonl
        brief.md
        scoring.json
        generations/{generation_id}/
          experiment.json
          runs/{entry_id}/
            events.jsonl
            loss.json
        journal.md
        analysis.md
      lineage.json

The two-callable invariant from :class:`zicato.core.types.RuntimeConfig`
is enforced here by :func:`assert_distinct_callables`; the dataclass
itself stays purely declarative.

Convention drift — outer vs inner workspace root
-------------------------------------------------

``workspace_root`` is, by convention, the inner ``.zicato`` directory
itself: ``epochs/``, ``runtime/``, etc. hang directly off it. Some
callers historically pass the *outer* project directory (the parent that
holds ``.zicato/``) — when that happens the helpers below
transparently descend into ``.zicato/`` when the inner layout exists.
This is the single I/O exception in this module: a best-effort
``Path.is_dir()`` probe that lets the report regenerator + dashboard
read the right tree even when the caller hands us the outer dir.

The descent only fires when the outer form does NOT carry an
``epochs/`` directory but the inner ``.zicato/`` does; legacy callers
that already pass the inner dir, and tests that build a synthetic
``{ws}/epochs/`` tree, are untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from zicato.workspace.layout import WorkspaceLayout


def _normalise_workspace_root(workspace_root: Path) -> Path:
    """Resolve ``workspace_root`` to the inner ``.zicato/`` dir when needed.

    The path helpers treat ``workspace_root`` as the inner ``.zicato``
    directory; epoch artifacts live at
    ``workspace_root / "epochs" / {epoch_id}``. Some callers — historic
    or convenience wrappers — pass the *outer* project dir instead. To
    keep those callers working without surprise, this normaliser
    descends into ``workspace_root / ".zicato"`` when:

    * the outer form does NOT carry an ``epochs/`` directory, AND
    * the inner form DOES carry one.

    Otherwise the path is returned unchanged. The behaviour is
    deliberately conservative: legacy layouts where ``{ws}/epochs/``
    already exists are never overridden, and tests that build a
    synthetic ``{tmp}/epochs/`` tree don't accidentally redirect.
    """
    root = Path(workspace_root)
    if (root / "epochs").is_dir():
        return root
    inner = root / ".zicato"
    if (inner / "epochs").is_dir():
        return inner
    return root


def _layout(workspace_root: Path) -> WorkspaceLayout:
    """Build a :class:`WorkspaceLayout` over the descent-normalised root.

    The outer→inner descent (:func:`_normalise_workspace_root`) is the one
    I/O step this module owns; every *leaf* path join is then delegated to
    the shared :class:`WorkspaceLayout` so there is a single definition of
    the ``.zicato/`` filename layout (read AND write). The descent must run
    first because the layout itself does no probing.
    """
    return WorkspaceLayout.from_root(_normalise_workspace_root(workspace_root))


def epoch_dir(workspace_root: Path, epoch_id: str) -> Path:
    """Return the directory holding one epoch's artifacts."""
    return _layout(workspace_root).epoch_dir(epoch_id)


def generations_dir(workspace_root: Path, epoch_id: str) -> Path:
    """Return the ``generations/`` directory under one epoch.

    The parent of every per-generation directory. Callers that enumerate
    an epoch's generations (the health CLI, the analysis pass, the
    repair tools) resolve this single join rather than re-spelling
    ``epoch_dir(...) / "generations"``.
    """
    return _layout(workspace_root).generations_dir(epoch_id)


def generation_dir(workspace_root: Path, epoch_id: str, generation_id: str) -> Path:
    """Return the directory holding one generation's artifacts."""
    return _layout(workspace_root).generation_dir(epoch_id, generation_id)


def reflections_dir(workspace_root: Path, epoch_id: str) -> Path:
    """Return one epoch's board-reflection subtree (``reflections/``).

    The parent of every per-reflection directory
    (:func:`reflection_dir`). Beside the calibration / preflight helpers,
    this is the storage seam board reflection's corpus + analysis persist
    under (BOARD-REFLECTION.md's data model). Created lazily; readers
    tolerate its absence.
    """
    return _layout(workspace_root).reflections_dir(epoch_id)


def reflection_dir(workspace_root: Path, epoch_id: str, reflection_id: str) -> Path:
    """Return the directory holding ONE reflection run's artifacts."""
    return _layout(workspace_root).reflection_dir(epoch_id, reflection_id)


def reflection_plan_path(workspace_root: Path, epoch_id: str, reflection_id: str) -> Path:
    """Path to one reflection's pre-registered run plan (``plan.json``)."""
    return _layout(workspace_root).reflection_plan(epoch_id, reflection_id)


def reflection_corpus_path(workspace_root: Path, epoch_id: str, reflection_id: str) -> Path:
    """Path to one reflection's observation corpus (``corpus.jsonl``).

    One :class:`~zicato.reflection.corpus.ObservationRun` per line — each
    REFERENCES the run artifacts under ``generations/`` (never copies).
    """
    return _layout(workspace_root).reflection_corpus(epoch_id, reflection_id)


def reflection_adjudication_dir(workspace_root: Path, epoch_id: str, reflection_id: str) -> Path:
    """Path to one reflection's meta-judge adjudication subtree (``adjudication/``)."""
    return _layout(workspace_root).reflection_adjudication_dir(epoch_id, reflection_id)


def reflection_adjudication_path(
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
    judge_name: str,
    run_ref: str,
) -> Path:
    """Path to one adjudicated decision's verdict file.

    ``adjudication/{judge_name}/{run_ref}.json`` — file-exists is a cache
    HIT (the corpus is frozen per ``reflection_id``).
    """
    return _layout(workspace_root).reflection_adjudication(
        epoch_id, reflection_id, judge_name, run_ref
    )


def reflection_scorecards_path(workspace_root: Path, epoch_id: str, reflection_id: str) -> Path:
    """Path to one reflection's aggregated ``scorecards.json``."""
    return _layout(workspace_root).reflection_scorecards(epoch_id, reflection_id)


def reflection_findings_path(workspace_root: Path, epoch_id: str, reflection_id: str) -> Path:
    """Path to one reflection's ranked ``findings.json``."""
    return _layout(workspace_root).reflection_findings(epoch_id, reflection_id)


def reflection_practices_path(workspace_root: Path, epoch_id: str, reflection_id: str) -> Path:
    """Path to one reflection's practice review (``practices.json``)."""
    return _layout(workspace_root).reflection_practices(epoch_id, reflection_id)


def reflection_suggestions_path(workspace_root: Path, epoch_id: str, reflection_id: str) -> Path:
    """Path to one reflection's synthesised eval suggestions (``suggestions.json``)."""
    return _layout(workspace_root).reflection_suggestions(epoch_id, reflection_id)


def proposer_reflections_dir(workspace_root: Path, epoch_id: str) -> Path:
    """Return one epoch's proposer-reflection subtree (``proposer_reflections/``)."""
    return _layout(workspace_root).proposer_reflections_dir(epoch_id)


def proposer_reflection_dir(workspace_root: Path, epoch_id: str, reflection_id: str) -> Path:
    """Return the directory holding ONE proposer-reflection pass's artifacts."""
    return _layout(workspace_root).proposer_reflection_dir(epoch_id, reflection_id)


def proposer_reflection_findings_path(
    workspace_root: Path, epoch_id: str, reflection_id: str
) -> Path:
    """Path to one proposer-reflection pass's ``findings.json``."""
    return _layout(workspace_root).proposer_reflection_findings(epoch_id, reflection_id)


def proposer_staged_recommendations_path(workspace_root: Path) -> Path:
    """Path to the workspace's staged-recommendation queue (``proposer_staged.json``)."""
    return _layout(workspace_root).proposer_staged_recommendations()


def run_dir(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> Path:
    """Return the directory holding one run's artifacts.

    A run is one ``(epoch, generation, board_entry)`` triple; its
    directory holds the events JSONL and the reducer's loss profile.
    """
    return _layout(workspace_root).run_dir(epoch_id, generation_id, entry_id)


def events_jsonl_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> Path:
    """Path to the goldfive event JSONL for one run."""
    return _layout(workspace_root).events(epoch_id, generation_id, entry_id)


def loss_profile_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> Path:
    """Path to the reducer's ``loss.json`` output for one run."""
    return _layout(workspace_root).loss(epoch_id, generation_id, entry_id)


def run_result_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    entry_id: str,
) -> Path:
    """Path to one run's persisted ``result.json`` (the RunResult capture).

    The read twin of the worker's post-run write: the user-facing
    transcript + final output the run produced, persisted beside
    ``loss.json`` when :attr:`RuntimeConfig.persist_run_results` is on
    (the default). The canonical replicate-0 slot; replicate ``r>0``
    lives at the sibling ``result.r{n}.json``
    (:func:`zicato.tournament.unit_cache.unit_result_path`). Readers
    must tolerate absence — legacy runs, an opted-out runtime, or a
    best-effort write that failed all leave no file
    (:func:`zicato.tournament.unit_cache.read_run_result` returns
    ``None`` in every such case).
    """
    return _layout(workspace_root).result(epoch_id, generation_id, entry_id)


def experiment_json_path(workspace_root: Path, epoch_id: str, generation_id: str) -> Path:
    """Path to a generation's ``experiment.json`` (hypothesis + outcome)."""
    return _layout(workspace_root).experiment(epoch_id, generation_id)


def harness_load_path(workspace_root: Path, epoch_id: str, generation_id: str) -> Path:
    """Path to a generation's ``harness_load.json`` snapshot-origin record.

    The worker writes it after a successful harness load (it is the only
    process that sees the resolved entrypoint ``__file__``); the
    orchestrator reads it to emit the round log's ``harness_loaded`` event
    (issue #110). Readers MUST tolerate absence — see
    :meth:`zicato.workspace.layout.WorkspaceLayout.harness_load`.
    """
    return _layout(workspace_root).harness_load(epoch_id, generation_id)


def patches_dir(workspace_root: Path, epoch_id: str, generation_id: str) -> Path:
    """Path to the per-patch JSON directory under a generation.

    See :doc:`project_zicato_storage_design` for the per-patch file
    layout. The directory is created lazily by writers; readers tolerate
    its absence (an experiment with zero patches has no directory).
    """
    return _layout(workspace_root).patches_dir(epoch_id, generation_id)


def patch_json_path(
    workspace_root: Path,
    epoch_id: str,
    generation_id: str,
    patch_id: str,
) -> Path:
    """Path to one patch JSON file inside a generation's patches directory."""
    return _layout(workspace_root).patch_json(epoch_id, generation_id, patch_id)


def mutations_json_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's per-round mutation-points snapshot.

    Written by the orchestrator after each round's mutation enumeration:
    a JSON array of the :class:`zicato.core.types.MutationPoint` records
    the proposer was offered. The dashboard reads this to render the
    mutable surface for an in-progress epoch without re-walking the
    snapshot tree. The file lives under the epoch directory (not a
    generation directory) and is overwritten every round — it always
    reflects the most recent enumeration.
    """
    return _layout(workspace_root).mutations(epoch_id)


def proposer_inputs_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's captured proposer inputs (``proposer_inputs.jsonl``).

    Append-only, one line per proposer LLM call, holding the rendered
    system + user text verbatim plus the lineage coordinates that identify
    the call (:mod:`zicato.proposer.input_capture`). It lives under the
    epoch directory rather than ``runtime/`` because it is durable history
    that must survive a resume, not live process state.
    """
    return _layout(workspace_root).proposer_inputs(epoch_id)


def ladder_state_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's persisted Ladder governor state (``ladder_state.json``).

    The Ladder (OVERFITTING.md §4 / §12 #2) tracks per-epoch state across
    rounds — the best holdout score released so far and the remaining
    holdout-query budget. The runner reads it before mediating a round's
    holdout query and writes the updated state back after. Lives under the
    epoch directory (not a generation directory) because the budget is a
    per-epoch resource, exactly like ``mutations.json``.
    """
    return _layout(workspace_root).ladder_state(epoch_id)


def journal_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's running narrative journal."""
    return _layout(workspace_root).journal(epoch_id)


def analysis_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's at-close analysis writeup."""
    return _layout(workspace_root).analysis_md(epoch_id)


def lineage_path(workspace_root: Path) -> Path:
    """Path to the workspace-level cross-cutting lineage DAG."""
    return _layout(workspace_root).lineage_path


def brief_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to the operator-edited proposer brief for one epoch (``brief.md``)."""
    return _layout(workspace_root).brief(epoch_id)


def rubric_path(workspace_root: Path, epoch_id: str) -> Path:
    """Deprecated alias of :func:`brief_path`.

    The per-epoch proposer brief was once ``rubric.md``; it is now
    ``brief.md``. This alias resolves to the current path so older
    callers/imports keep working — new code should use ``brief_path``.
    """
    return brief_path(workspace_root, epoch_id)


def board_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to one epoch's frozen board JSONL."""
    return _layout(workspace_root).board(epoch_id)


def field_tournaments_dir(workspace_root: Path, epoch_id: str) -> Path:
    """Path to an epoch's durable field-tournament snapshot directory.

    A non-gauntlet structure (swiss / single-elim / double-elim / racing)
    settles ONE field record per round's tournament — the round-by-round
    pairings + the Copeland standings + the proposing field-status — which
    the per-challenger ``experiment.json`` audit cannot reconstruct on its
    own. The orchestrator writes that settled field structure here so the
    analytical index can re-derive it on ``zicato repair index`` (the
    files-are-canonical rule) and the dashboard renders the ladder /
    bracket post-run rather than blank.

    One file per field tournament: ``field-{first_challenger}.json``,
    keyed on the round's first applied challenger so a multi-round epoch
    keeps a snapshot per round without clobbering earlier ones. The
    directory is created lazily by the writer; readers tolerate its
    absence (a pure-gauntlet epoch never writes one).
    """
    return _layout(workspace_root).field_tournaments_dir(epoch_id)


def field_tournament_path(workspace_root: Path, epoch_id: str, first_challenger_id: str) -> Path:
    """Path to one round's durable field-tournament snapshot JSON.

    See :func:`field_tournaments_dir`. ``first_challenger_id`` is the
    round's first applied challenger — the same stable key the runtime
    ``active_tournament`` tournament id is minted from — so the snapshot
    is idempotent across rebuilds and unique per round.
    """
    return _layout(workspace_root).field_tournament(epoch_id, first_challenger_id)


def scoring_path(workspace_root: Path, epoch_id: str) -> Path:
    """Path to one epoch's frozen scoring-weights JSON."""
    return _layout(workspace_root).scoring(epoch_id)


def assert_distinct_callables(
    harness_call_llm: Callable[..., Any],
    auxiliary_call_llm: Callable[..., Any],
) -> None:
    """Enforce that the two LLM callables on :class:`RuntimeConfig` differ.

    The emulator, judge, and analysis pass run on the ``auxiliary_call_llm``
    side; the inner harness runs on the ``harness_call_llm`` side. If the two
    sides share a callable, the emulator and the inner harness execute through
    the same process state and risk colluding (the inner harness can perceive
    the emulator's prompts, the emulator can leak the expected output through
    shared state, etc.). The collusion risk is high enough that we refuse to
    start the run when the two callables are identity-equal.

    The WS-ENS proposer ROLE callables (breadth / depth) are guard-exempt:
    they are proposer-side, one trust domain, and may freely be the same
    callable as each other or as the auxiliary. The emulator↔harness collusion
    risk this guard defends rides the still-guarded auxiliary surface, not the
    proposer roles.

    Raises
    ------
    RuntimeError
        If ``harness_call_llm is auxiliary_call_llm``.

    Notes
    -----
    Identity comparison (``is``) is intentional. Two distinct callables
    that happen to wrap the same underlying client / endpoint pass this
    check; that is the operator's responsibility. The point here is to
    catch the trivial mistake of passing the same callable twice.
    """
    if harness_call_llm is auxiliary_call_llm:
        raise RuntimeError(
            "harness_call_llm and auxiliary_call_llm must be distinct callables; "
            "shared callables risk collusion in multi-turn emulated entries"
        )


__all__ = [
    "epoch_dir",
    "generations_dir",
    "generation_dir",
    "reflections_dir",
    "reflection_dir",
    "reflection_plan_path",
    "reflection_corpus_path",
    "reflection_adjudication_dir",
    "reflection_adjudication_path",
    "reflection_scorecards_path",
    "reflection_findings_path",
    "reflection_practices_path",
    # Defined since the eval-synthesis surface landed but never exported; the
    # CLI reaches it through the module, so the omission was invisible.
    "reflection_suggestions_path",
    "proposer_reflections_dir",
    "proposer_reflection_dir",
    "proposer_reflection_findings_path",
    "proposer_staged_recommendations_path",
    "run_dir",
    "events_jsonl_path",
    "loss_profile_path",
    "run_result_path",
    "experiment_json_path",
    "harness_load_path",
    "patches_dir",
    "patch_json_path",
    "mutations_json_path",
    "proposer_inputs_path",
    "ladder_state_path",
    "journal_path",
    "analysis_path",
    "lineage_path",
    "brief_path",
    "rubric_path",
    "board_path",
    "scoring_path",
    "field_tournaments_dir",
    "field_tournament_path",
    "assert_distinct_callables",
]
