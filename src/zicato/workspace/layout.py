"""WorkspaceLayout — the single source of ``.zicato/`` path math.

A small, pure (no-I/O) value object that resolves every path the dashboard
reads off a workspace root. It exists so leaf filename joins
(``epochs/<id>/generations/<gen>/runs/<entry>/loss.json`` and friends) live
in ONE place instead of being re-spelled at dozens of dashboard call sites.

``root`` is the inner ``.zicato`` directory itself — the same convention as
:class:`zicato.dashboard.readers.paths.WorkspacePaths` (``runtime/`` and
``epochs/`` hang directly off it). Unlike
:mod:`zicato.core.workspace`, this layout does **no** outer→inner descent:
the dashboard always passes the inner root, so the prior path math never
probed the filesystem, and neither does this — keeping the resolved paths
byte-identical to the inline joins it replaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
        """The legacy proposer-brief filename (``rubric.md``), read as fallback."""
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

    def contract_components(self, epoch_id: str) -> Path:
        """One epoch's per-component contract sub-hashes (``contract_components.json``)."""
        return self.epoch_dir(epoch_id) / "contract_components.json"

    def health_dir(self, epoch_id: str) -> Path:
        """One epoch's loop-health snapshot directory (``health/``)."""
        return self.epoch_dir(epoch_id) / "health"

    def field_tournaments_dir(self, epoch_id: str) -> Path:
        """One epoch's durable field-tournament snapshot directory (``tournaments/``)."""
        return self.epoch_dir(epoch_id) / "tournaments"

    def generations_dir(self, epoch_id: str) -> Path:
        """One epoch's ``generations/`` directory."""
        return self.epoch_dir(epoch_id) / "generations"

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

    def patches_dir(self, epoch_id: str, generation_id: str) -> Path:
        """One generation's per-patch JSON directory (``patches/``)."""
        return self.generation_dir(epoch_id, generation_id) / "patches"

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

    def events(self, epoch_id: str, generation_id: str, entry_id: str) -> Path:
        """One run's goldfive event JSONL (``events.jsonl``)."""
        return self.run_dir(epoch_id, generation_id, entry_id) / "events.jsonl"
