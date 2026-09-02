"""WorkspaceLayout — the single source of ``.zicato/`` path math.

A small, pure (no-I/O) value object that resolves every path the dashboard
reads off a workspace root. It exists so leaf filename joins
(``epochs/<id>/generations/<gen>/runs/<entry>/loss.json`` and friends) live
in ONE place instead of being re-spelled at dozens of dashboard call sites.

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

    def rounds_dir(self, epoch_id: str) -> Path:
        """One epoch's ``rounds/`` directory (one sub-dir per evolve round).

        :func:`zicato.epoch.round_log.rounds_dir` resolves through here, so
        the round subtree has one path definition like every other.
        """
        return self.epoch_dir(epoch_id) / "rounds"

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
