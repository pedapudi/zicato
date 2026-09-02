"""WorkspaceLayout — the one declaration of where each ``.zicato/`` artifact lives.

A small, pure (no-I/O) value object that resolves every path a reader or a
writer needs off a workspace root. Each leaf filename join
(``epochs/<id>/generations/<gen>/runs/<entry>/loss.json`` and its siblings)
is declared here once, so a location cannot be spelled two ways.

Every other path surface in the tree resolves through this class rather
than re-joining the names: :mod:`zicato.core.workspace` (which adds the
outer→inner descent below), :mod:`zicato.runtime.paths`,
:class:`zicato.query.paths.WorkspacePaths`, and the record-key helpers in
:mod:`zicato.epoch._storage` and :mod:`zicato.runtime._storage`.

The key helpers reach a location through :data:`WORKSPACE_RELATIVE_LAYOUT`
and :func:`storage_key` at the bottom of this module: a storage key is a
layout path stated relative to the workspace root, so a record's location
has one declaration whether a caller wants it as a path or as a key.

``root`` is the inner ``.zicato`` directory itself — the same convention as
:class:`zicato.query.paths.WorkspacePaths` (``runtime/`` and
``epochs/`` hang directly off it). Unlike
:mod:`zicato.core.workspace`, this layout does **no** outer→inner descent:
the dashboard always passes the inner root, so the prior path math never
probed the filesystem, and neither does this — keeping the resolved paths
byte-identical to the inline joins it replaces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_REPLICATE_EVENTS_RE = re.compile(r"^events\.r([1-9]\d*)\.jsonl$")


def events_replicate_index(path: Path | str) -> int | None:
    """Return a current events file's replicate index; archives are ``None``."""
    name = Path(path).name
    if name == "events.jsonl":
        return 0
    match = _REPLICATE_EVENTS_RE.match(name)
    return int(match.group(1)) if match else None


def is_events_file(path: Path | str) -> bool:
    """Whether ``path`` is ``events.jsonl`` or a current ``events.rN.jsonl``."""
    return events_replicate_index(path) is not None


@dataclass(frozen=True)
class WorkspaceLayout:
    """Pure path resolution over a ``.zicato`` workspace root.

    Construct one with :meth:`from_root` (accepts any path-like) and ask it
    for paths; it never touches the filesystem.
    """

    root: Path

    @classmethod
    def from_root(cls, root: Path | str) -> WorkspaceLayout:
        """Build a layout from a workspace root (the inner ``.zicato`` dir)."""
        return cls(Path(root))

    # -- top-level -----------------------------------------------------------

    @property
    def epochs_dir(self) -> Path:
        """The ``epochs/`` directory holding every epoch's subtree."""
        return self.root / "epochs"

    @property
    def lineage_path(self) -> Path:
        """The workspace-level cross-cutting ``lineage.json``."""
        return self.root / "lineage.json"

    @property
    def index_db_path(self) -> Path:
        """The SQLite analytical index (``index.db``)."""
        return self.root / "index.db"

    @property
    def current_epoch_marker(self) -> Path:
        """The ``current_epoch`` marker file at the workspace root."""
        return self.root / "current_epoch"

    @property
    def logs_dir(self) -> Path:
        """The structured operator-log directory (``logs/``).

        One ``<utc-stamp>-<pid>.jsonl`` stream per ``evolve`` / ``reflect``
        invocation, as LOGGING.md describes.
        """
        return self.root / "logs"

    # -- runtime state -------------------------------------------------------
    #
    # The ``runtime/`` subtree is the read/write surface the orchestrator and
    # the external supervisor binary share. It holds live process state, so
    # every reader tolerates absence: a workspace with no evolve loop running
    # has never written most of it. Directory creation is a domain decision
    # and stays in :func:`zicato.runtime.paths.ensure_runtime_dirs`.

    @property
    def runtime_dir(self) -> Path:
        """The ``runtime/`` directory holding live process state."""
        return self.root / "runtime"

    @property
    def lock(self) -> Path:
        """The exclusive workspace lock record (``runtime/lock.json``)."""
        return self.runtime_dir / "lock.json"

    @property
    def heartbeat(self) -> Path:
        """The orchestrator's liveness beat (``runtime/heartbeat.json``)."""
        return self.runtime_dir / "heartbeat.json"

    @property
    def dashboard_endpoint(self) -> Path:
        """The dashboard's actually-bound host and port (``runtime/dashboard.json``).

        The standalone dashboard service walks ``+1`` from its preferred port
        when that port is taken, so the port it ends up serving on is not
        knowable up front. The service writes what it bound to once the
        listener is up, and ``zicato evolve`` reads it back to report the real
        URL instead of guessing.
        """
        return self.runtime_dir / "dashboard.json"

    @property
    def active_runs_dir(self) -> Path:
        """The directory holding per-run live state (``runtime/active_runs/``)."""
        return self.runtime_dir / "active_runs"

    def active_run(self, run_id: str) -> Path:
        """One in-flight run's live-state record."""
        return self.active_runs_dir / f"{run_id}.json"

    @property
    def active_tournament(self) -> Path:
        """The active tournament's SNAPSHOT record (``runtime/active_tournament.json``).

        Read only as a fallback, by the compatibility reader and by resume
        cleanup: a snapshot with no event log beside it is still folded into a
        live view. Nothing writes it — the live producer appends to
        :meth:`active_tournament_log` instead.
        """
        return self.runtime_dir / "active_tournament.json"

    @property
    def active_tournament_log(self) -> Path:
        """The active tournament's EVENT LOG (``runtime/active_tournament.events.jsonl``).

        The single-writer, append-only JSONL carrying the in-progress
        tournament's live state. The runner appends one typed event per state
        transition and a reader folds the log into the live view, which removes
        the snapshot's read-modify-write race.
        """
        return self.runtime_dir / "active_tournament.events.jsonl"

    @property
    def progress_log(self) -> Path:
        """The orchestrator's progress EVENT LOG (``runtime/progress.events.jsonl``).

        A single-writer, append-only JSONL the evolve loop appends one typed
        event to on each genuine transition (round start, propose, apply,
        tournament start and settle, gate, promote or reject). Its monotonic
        ``seq`` is the true liveness signal: it advances only on real progress,
        never on a timer, so a wedged loop whose heartbeat thread keeps
        stamping the clock does not read as alive. The tail ``seq`` is stamped
        into :meth:`heartbeat` and the dashboard's server-sent frames.
        """
        return self.runtime_dir / "progress.events.jsonl"

    @property
    def inconclusive_dir(self) -> Path:
        """The dead-letter directory for inconclusive crowning duels.

        The opt-in Bradley-Terry promotion pre-gate records here any crowning
        duel whose rating confidence intervals never separated before its
        replicate budget was spent. One file per generation
        (:meth:`inconclusive_record`) captures the unresolved duel and its
        final intervals, so nothing is dropped silently. An absent directory
        means no such duel was ever recorded, which is the default for every
        run that did not opt into the pre-gate.
        """
        return self.runtime_dir / "inconclusive"

    def inconclusive_record(self, generation_id: str) -> Path:
        """One inconclusive challenger generation's dead-letter record."""
        return self.inconclusive_dir / f"{generation_id}.json"

    @property
    def control_dir(self) -> Path:
        """The directory operator commands are dropped into (``runtime/control/``)."""
        return self.runtime_dir / "control"

    def control_command(self, command: str) -> Path:
        """One control command's file, under :meth:`control_dir`.

        ``command`` is taken verbatim as a relative path, and may include a
        subdirectory component: the kill, promote, and reject commands keep one
        file per target under a per-command-kind subdirectory (for example
        ``kill_runs/run_abc``).
        """
        return self.control_dir / command

    @property
    def control_log_dir(self) -> Path:
        """The directory consumed commands are archived in (``runtime/control_log/``)."""
        return self.runtime_dir / "control_log"

    @property
    def kill_requests_dir(self) -> Path:
        """The directory holding parent-to-supervisor kill escalations.

        Distinct from the operator's ``control/kill_runs/`` channel, which the
        orchestrator consumes. A marker here is written by the Python parent
        when a worker overran its budget, asking the Rust supervisor to run the
        single SIGTERM-grace-SIGKILL escalator on that worker's pid.
        Consolidating escalation in the supervisor removes the race the parent
        and the supervisor would otherwise have over one worker pid.
        """
        return self.control_dir / "kill_requests"

    def kill_request(self, run_id: str) -> Path:
        """One run's parent-to-supervisor kill-request marker.

        No ``.json`` suffix: the supervisor matches on the bare run id.
        """
        return self.kill_requests_dir / run_id

    # -- per-epoch -----------------------------------------------------------

    def epoch_dir(self, epoch_id: str) -> Path:
        """The directory holding one epoch's artifacts."""
        return self.epochs_dir / epoch_id

    def epoch_config(self, epoch_id: str) -> Path:
        """One epoch's ``config.json`` (contract hash, created_at, closed)."""
        return self.epoch_dir(epoch_id) / "config.json"

    def board(self, epoch_id: str) -> Path:
        """One epoch's frozen board JSONL (``board.jsonl``)."""
        return self.epoch_dir(epoch_id) / "board.jsonl"

    def scoring(self, epoch_id: str) -> Path:
        """One epoch's frozen scoring-weights JSON (``scoring.json``)."""
        return self.epoch_dir(epoch_id) / "scoring.json"

    def brief(self, epoch_id: str) -> Path:
        """One epoch's operator proposer brief (``brief.md``)."""
        return self.epoch_dir(epoch_id) / "brief.md"

    def legacy_rubric(self, epoch_id: str) -> Path:
        """The fallback proposer-brief filename, ``rubric.md``."""
        return self.epoch_dir(epoch_id) / "rubric.md"

    def journal(self, epoch_id: str) -> Path:
        """One epoch's running narrative journal (``journal.md``)."""
        return self.epoch_dir(epoch_id) / "journal.md"

    def analysis_md(self, epoch_id: str) -> Path:
        """One epoch's at-close analysis markdown (``analysis.md``)."""
        return self.epoch_dir(epoch_id) / "analysis.md"

    def analysis_html(self, epoch_id: str) -> Path:
        """One epoch's rendered analysis page (``analysis.html``)."""
        return self.epoch_dir(epoch_id) / "analysis.html"

    def mutations(self, epoch_id: str) -> Path:
        """One epoch's per-round mutation-points snapshot (``mutations.json``)."""
        return self.epoch_dir(epoch_id) / "mutations.json"

    def proposer_inputs(self, epoch_id: str) -> Path:
        """One epoch's captured proposer inputs (``proposer_inputs.jsonl``)."""
        return self.epoch_dir(epoch_id) / "proposer_inputs.jsonl"

    def contract_components(self, epoch_id: str) -> Path:
        """One epoch's per-component contract sub-hashes (``contract_components.json``)."""
        return self.epoch_dir(epoch_id) / "contract_components.json"

    def ladder_state(self, epoch_id: str) -> Path:
        """One epoch's persisted Ladder governor state (``ladder_state.json``)."""
        return self.epoch_dir(epoch_id) / "ladder_state.json"

    def current_generation_marker(self, epoch_id: str) -> Path:
        """One epoch's promoted-lineage-head marker (``current_generation``)."""
        return self.epoch_dir(epoch_id) / "current_generation"

    def roll_seed_marker(self, epoch_id: str) -> Path:
        """One epoch's cross-epoch v0-seed marker (``v0_seed_from``).

        Written when an epoch is opened by a contract-roll: it records the
        absolute path to the predecessor epoch's promoted-head snapshot, so
        the new epoch's ``v0`` is seeded from there rather than the
        registered source. Absent for a fresh (non-rolled) epoch.
        """
        return self.epoch_dir(epoch_id) / "v0_seed_from"

    def field_tournament(self, epoch_id: str, first_challenger_id: str) -> Path:
        """One round's durable field-tournament snapshot JSON.

        See :meth:`field_tournaments_dir`. ``first_challenger_id`` keys the
        snapshot on the round's first applied challenger so a multi-round
        epoch keeps one file per round (``field-{first_challenger}.json``).
        """
        return self.field_tournaments_dir(epoch_id) / f"field-{first_challenger_id}.json"

    def health_dir(self, epoch_id: str) -> Path:
        """One epoch's loop-health snapshot directory (``health/``)."""
        return self.epoch_dir(epoch_id) / "health"

    def field_tournaments_dir(self, epoch_id: str) -> Path:
        """One epoch's durable field-tournament snapshot directory (``tournaments/``)."""
        return self.epoch_dir(epoch_id) / "tournaments"

    def generations_dir(self, epoch_id: str) -> Path:
        """One epoch's ``generations/`` directory."""
        return self.epoch_dir(epoch_id) / "generations"

    def episodes_dir(self, epoch_id: str) -> Path:
        """One epoch's proposal-episode subtree (``episodes/``).

        One self-contained Foe episode directory per proposal, holding the
        ``episode.jsonl`` that is the whole record of what the proposer did.
        """
        return self.epoch_dir(epoch_id) / "episodes"

    def proposal_episode_dir(
        self, epoch_id: str, generation_id: str, slot_index: int | None = None
    ) -> Path:
        """Where one candidate's proposal episode writes its log.

        A round proposing one candidate per generation names the directory
        after the generation it is proposing. A best-of-N slate runs several
        episodes toward the same generation id, so each carries the slate slot
        it belongs to.
        """
        slot = "" if slot_index is None else f"-{slot_index}"
        return self.episodes_dir(epoch_id) / f"{generation_id}{slot}"

    def proposal_episode_export(
        self, epoch_id: str, generation_id: str, slot_index: int | None = None
    ) -> Path:
        """Foe's static page for one proposal episode, beside that episode's log.

        Written when the episode settles, by
        :func:`zicato.proposer.episode_export.write_episode_export`. Absent
        whenever that render did not happen, which every reader tolerates.
        """
        from zicato.proposer.episode_export import (  # noqa: PLC0415 - avoids an import cycle
            EXPORT_FILENAME,
        )

        return self.proposal_episode_dir(epoch_id, generation_id, slot_index) / EXPORT_FILENAME

    def rounds_dir(self, epoch_id: str) -> Path:
        """One epoch's ``rounds/`` directory (one sub-dir per evolve round).

        :func:`zicato.epoch.round_log.rounds_dir` resolves through here, so
        the round subtree has one path definition like every other.
        """
        return self.epoch_dir(epoch_id) / "rounds"

    def round_dir(self, epoch_id: str, round_index: int) -> Path:
        """One evolve round's directory under :meth:`rounds_dir`.

        ``round_index`` is the epoch-cumulative round number — the same axis
        :attr:`zicato.core.types.Generation.round_index` and the health
        reports' ``round_{n}.json`` use — rendered as its plain decimal
        string.
        """
        return self.rounds_dir(epoch_id) / str(int(round_index))

    # -- board reflection ----------------------------------------------------

    def reflections_dir(self, epoch_id: str) -> Path:
        """One epoch's board-reflection subtree (``reflections/``).

        Holds one sub-directory per reflection run, keyed by its
        ``reflection_id`` — the frozen observation corpus + analysis a
        board-reflection produces for the sealed contract (see
        BOARD-REFLECTION.md's data model). Created lazily by the reflection
        engine; readers tolerate its absence (an epoch that was never
        reflected has no directory).
        """
        return self.epoch_dir(epoch_id) / "reflections"

    def reflection_dir(self, epoch_id: str, reflection_id: str) -> Path:
        """The directory holding ONE reflection run's artifacts."""
        return self.reflections_dir(epoch_id) / reflection_id

    def reflection_plan(self, epoch_id: str, reflection_id: str) -> Path:
        """One reflection's pre-registered run plan (``plan.json``)."""
        return self.reflection_dir(epoch_id, reflection_id) / "plan.json"

    def reflection_corpus(self, epoch_id: str, reflection_id: str) -> Path:
        """One reflection's observation corpus (``corpus.jsonl``).

        One :class:`~zicato.reflection.corpus.ObservationRun` per line —
        each REFERENCING the run artifacts under ``generations/`` rather
        than copying them.
        """
        return self.reflection_dir(epoch_id, reflection_id) / "corpus.jsonl"

    def reflection_adjudication_dir(self, epoch_id: str, reflection_id: str) -> Path:
        """One reflection's meta-judge adjudication subtree (``adjudication/``).

        Holds one sub-directory per judge name, each with one JSON file per
        adjudicated decision keyed by ``run_ref`` — the per-decision
        meta-judge verdict (BOARD-REFLECTION.md's data model). The corpus is
        frozen per ``reflection_id``, so a present file is a cache HIT: a
        re-run of the same reflection re-reads it and spends no adjudicator
        budget. Created lazily; readers tolerate its absence.
        """
        return self.reflection_dir(epoch_id, reflection_id) / "adjudication"

    def reflection_adjudication(
        self, epoch_id: str, reflection_id: str, judge_name: str, run_ref: str
    ) -> Path:
        """One adjudicated decision's verdict file.

        ``adjudication/{judge_name}/{run_ref}.json`` where ``run_ref`` is
        ``{candidate}:{entry}:r{replicate}`` — the stable key of the judge
        decision under review.
        """
        return (
            self.reflection_adjudication_dir(epoch_id, reflection_id)
            / judge_name
            / (f"{run_ref}.json")
        )

    def reflection_scorecards(self, epoch_id: str, reflection_id: str) -> Path:
        """One reflection's aggregated per-judge scorecards (``scorecards.json``)."""
        return self.reflection_dir(epoch_id, reflection_id) / "scorecards.json"

    def reflection_findings(self, epoch_id: str, reflection_id: str) -> Path:
        """One reflection's ranked findings + proposed edits (``findings.json``)."""
        return self.reflection_dir(epoch_id, reflection_id) / "findings.json"

    def reflection_practices(self, epoch_id: str, reflection_id: str) -> Path:
        """One reflection's practice review (``practices.json``).

        The narrative layer above the four pillars — the ``PracticeReview``
        (:mod:`zicato.reflection.practices`) persisted beside ``findings.json``.
        File-canonical; readers degrade on its absence.
        """
        return self.reflection_dir(epoch_id, reflection_id) / "practices.json"

    def reflection_suggestions(self, epoch_id: str, reflection_id: str) -> Path:
        """One reflection's synthesised eval suggestions (``suggestions.json``).

        The eval-synthesis output (EVAL-SYNTHESIS.md §6) persisted BESIDE
        ``findings.json`` — the same additive shape: a canonical file the
        ``reflect suggest`` mode writes and a tolerant reader degrades on
        absence (a reflection that never ran synthesis has no file).
        """
        return self.reflection_dir(epoch_id, reflection_id) / "suggestions.json"

    # -- proposer reflection -------------------------------------------------

    def proposer_reflections_dir(self, epoch_id: str) -> Path:
        """One epoch's PROPOSER-reflection subtree (``proposer_reflections/``).

        The sibling of :meth:`reflections_dir`, kept separate because the two
        audit different instruments: board reflection audits the evaluation
        contract, proposer reflection audits the thing that writes proposals
        against it. Same shape — one sub-directory per pass, keyed by its id,
        created lazily, absent for an epoch never reflected on.
        """
        return self.epoch_dir(epoch_id) / "proposer_reflections"

    def proposer_reflection_dir(self, epoch_id: str, reflection_id: str) -> Path:
        """The directory holding ONE proposer-reflection pass's artifacts."""
        return self.proposer_reflections_dir(epoch_id) / reflection_id

    def proposer_reflection_findings(self, epoch_id: str, reflection_id: str) -> Path:
        """One proposer-reflection pass's ``findings.json``.

        The persisted recommendations — each carrying the five-slot evidence
        block and, as its remedy slot, a ready-to-apply edit to the proposer
        dir. Written by ``zicato proposer reflect``; never by anything else.
        """
        return self.proposer_reflection_dir(epoch_id, reflection_id) / "findings.json"

    def proposer_staged_recommendations(self) -> Path:
        """The workspace's staged-recommendation queue (``proposer_staged.json``).

        ``zicato proposer apply-recommendation`` writes the applied id here
        after editing the proposer dir; the NEXT epoch to open drains the queue
        into its own record, so an epoch's lineage says which recommendation
        changed the proposer that produced it. Absent until something is
        applied.
        """
        return self.root / "proposer_staged.json"

    # -- per-generation ------------------------------------------------------

    def generation_dir(self, epoch_id: str, generation_id: str) -> Path:
        """The directory holding one generation's artifacts."""
        return self.generations_dir(epoch_id) / generation_id

    def experiment(self, epoch_id: str, generation_id: str) -> Path:
        """One generation's ``experiment.json`` (hypothesis + outcome)."""
        return self.generation_dir(epoch_id, generation_id) / "experiment.json"

    def gen_score(self, epoch_id: str, generation_id: str) -> Path:
        """One generation's cached ``gen_score.json`` aggregate."""
        return self.generation_dir(epoch_id, generation_id) / "gen_score.json"

    def gen_score_history(self, epoch_id: str, generation_id: str) -> Path:
        """One generation's append-only ``gen_score.history.jsonl`` archive.

        Every aggregate ever written for the generation, one JSON line
        per write, oldest first — the record that survives a champion's
        re-measurement overwriting the flat :meth:`gen_score` (issue
        #122). Absent until a generation has been scored at least once.
        """
        return self.generation_dir(epoch_id, generation_id) / "gen_score.history.jsonl"

    def harness_load(self, epoch_id: str, generation_id: str) -> Path:
        """One generation's ``harness_load.json`` snapshot-origin provenance.

        Written best-effort by the subprocess worker once it has loaded the
        harness (the only process that knows the resolved ``__file__``), read
        best-effort by the orchestrator to emit the round log's
        ``harness_loaded`` event. Every reader tolerates absence: an adapter
        kind that reports no entrypoint file, a fully cache-served
        generation, or a failed write all leave no file.
        """
        return self.generation_dir(epoch_id, generation_id) / "harness_load.json"

    def patches_dir(self, epoch_id: str, generation_id: str) -> Path:
        """One generation's per-patch JSON directory (``patches/``)."""
        return self.generation_dir(epoch_id, generation_id) / "patches"

    def patch_json(self, epoch_id: str, generation_id: str, patch_id: str) -> Path:
        """One patch's JSON file inside a generation's ``patches/`` directory."""
        return self.patches_dir(epoch_id, generation_id) / f"{patch_id}.json"

    def runs_dir(self, epoch_id: str, generation_id: str) -> Path:
        """One generation's ``runs/`` directory (one sub-dir per board entry)."""
        return self.generation_dir(epoch_id, generation_id) / "runs"

    # -- per-run -------------------------------------------------------------

    def run_dir(self, epoch_id: str, generation_id: str, entry_id: str) -> Path:
        """The directory holding one run's artifacts (one board entry)."""
        return self.runs_dir(epoch_id, generation_id) / entry_id

    def loss(self, epoch_id: str, generation_id: str, entry_id: str) -> Path:
        """One run's reducer ``loss.json`` output."""
        return self.run_dir(epoch_id, generation_id, entry_id) / "loss.json"

    def result(self, epoch_id: str, generation_id: str, entry_id: str) -> Path:
        """One run's persisted ``result.json`` (the RunResult capture).

        The canonical (replicate 0) slot; replicate ``r>0`` maps to the
        sibling ``result.r{n}.json`` via
        :func:`zicato.tournament.unit_cache.unit_result_path`, exactly
        mirroring how :meth:`loss` relates to ``loss.r{n}.json``.
        """
        return self.run_dir(epoch_id, generation_id, entry_id) / "result.json"

    def events(
        self, epoch_id: str, generation_id: str, entry_id: str, replicate_index: int = 0
    ) -> Path:
        """One replicate's events JSONL; replicate 0 is canonical."""
        run = self.run_dir(epoch_id, generation_id, entry_id)
        if replicate_index <= 0:
            return run / "events.jsonl"
        return run / f"events.r{replicate_index}.jsonl"

    def events_prev(
        self, epoch_id: str, generation_id: str, entry_id: str, replicate_index: int = 0
    ) -> Path:
        """The retained predecessor of one replicate's events JSONL."""
        run = self.run_dir(epoch_id, generation_id, entry_id)
        if replicate_index <= 0:
            return run / "events.prev.jsonl"
        return run / f"events.r{replicate_index}.prev.jsonl"

    def loss_archive(self, epoch_id: str, generation_id: str, entry_id: str) -> Path:
        """One run's displaced-loss archive (``loss.archive.jsonl``).

        The per-entry profiles a re-measurement overwrote, one JSON line
        each — see :func:`zicato.tournament.unit_cache.read_unit_loss_history`,
        which joins them with the canonical slot.
        """
        return self.run_dir(epoch_id, generation_id, entry_id) / "loss.archive.jsonl"


#: The layout resolved against an empty root, so every path it yields is
#: already stated relative to the workspace root. Pair it with
#: :func:`storage_key` to name a record's location as a backend key: the
#: ``*_key`` helpers in :mod:`zicato.epoch._storage` and
#: :mod:`zicato.runtime._storage` read their locations off this layout
#: instead of re-spelling the joins, which is what keeps each location to
#: one declaration.
WORKSPACE_RELATIVE_LAYOUT = WorkspaceLayout.from_root(Path())


def storage_key(relative_path: Path) -> str:
    """The backend key naming a path from :data:`WORKSPACE_RELATIVE_LAYOUT`.

    A storage key is a ``/``-separated path relative to the workspace root:
    :class:`zicato.storage.FileStorageBackend` resolves ``key`` as
    ``root / key``. So the key for a record is the layout's path for it read
    off the relative-rooted layout, spelled with forward slashes whatever
    separator the running platform uses.
    """
    return relative_path.as_posix()
