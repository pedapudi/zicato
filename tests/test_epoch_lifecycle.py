"""Tests for :mod:`zicato.epoch.lifecycle`."""

from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path

import pytest

from zicato.board.builder import Board, Entry
from zicato.core.types import ScoringWeights
from zicato.epoch import (
    close_epoch,
    current_epoch_id,
    list_epochs,
    load_epoch,
    new_epoch,
    switch_epoch,
)
from zicato.proposer.brief import ProposerBrief

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / ".zicato"
    ws.mkdir()
    return ws


@pytest.fixture()
def brief_file(tmp_path: Path) -> Path:
    path = tmp_path / "brief.md"
    path.write_text("# Proposer brief for tests\n\n## Forbidden\n\n(none)\n")
    return path


# ---------------------------------------------------------------------------
# new_epoch
# ---------------------------------------------------------------------------


def test_new_epoch_creates_expected_layout(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    weights = ScoringWeights()
    cfg = new_epoch(
        workspace_root=workspace,
        name="my first epoch",
        board_source=board_file,
        brief_source=brief_file,
        weights=weights,
    )

    # Id is date-prefixed with a filesystem-safe slug.
    today = _dt.datetime.now(_dt.UTC).date().isoformat()
    assert cfg.id == f"{today}_my_first_epoch"

    # Directory layout exists.
    edir = workspace / "epochs" / cfg.id
    assert edir.is_dir()
    assert (edir / "board.jsonl").exists()
    assert (edir / "brief.md").exists()
    assert (edir / "scoring.json").exists()
    assert (edir / "config.json").exists()
    # The per-epoch proposer brief is the renamed file; no legacy
    # ``rubric.md`` is written.
    assert not (edir / "rubric.md").exists()

    # Board and proposer brief are copies, not the originals.
    assert (edir / "board.jsonl").read_text() == board_file.read_text()
    assert (edir / "brief.md").read_text() == brief_file.read_text()
    # EpochConfig.brief_path carries the path to the frozen brief.
    assert cfg.brief_path == edir / "brief.md"

    # scoring.json is parseable and round-trips key fields.
    scoring = json.loads((edir / "scoring.json").read_text())
    assert scoring["pass_weight"] == weights.pass_weight
    assert scoring["pass_weight"] == weights.pass_weight
    assert scoring["severity_weights"]["critical"] == 10.0

    # current_epoch marker points at the new epoch.
    assert current_epoch_id(workspace) == cfg.id


def test_new_epoch_with_duplicate_name_gets_numeric_suffix(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    weights = ScoringWeights()
    a = new_epoch(workspace, "experiment", board_file, brief_file, weights)
    b = new_epoch(workspace, "experiment", board_file, brief_file, weights)
    assert a.id != b.id
    assert b.id.endswith("_2")


def test_new_epoch_rejects_empty_slug(workspace: Path, board_file: Path, brief_file: Path) -> None:
    with pytest.raises(ValueError, match="empty slug"):
        new_epoch(workspace, "!!!", board_file, brief_file, ScoringWeights())


def test_new_epoch_auto_closes_previous_open_epoch(
    workspace: Path,
    board_file: Path,
    brief_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    weights = ScoringWeights()
    first = new_epoch(workspace, "alpha", board_file, brief_file, weights)
    assert not load_epoch(workspace, first.id).closed

    # A small time gap so the suffix logic doesn't collide.
    time.sleep(0.01)
    second = new_epoch(workspace, "beta", board_file, brief_file, weights)

    # First epoch is now closed.
    refreshed = load_epoch(workspace, first.id)
    assert refreshed.closed
    assert refreshed.closed_at
    # Auto-close emitted a warning on stderr.
    err = capsys.readouterr().err
    assert "auto-closing" in err.lower() or "not closed manually" in err.lower()
    # Second is current and open.
    assert current_epoch_id(workspace) == second.id
    assert not load_epoch(workspace, second.id).closed


def test_new_epoch_can_skip_auto_close(workspace: Path, board_file: Path, brief_file: Path) -> None:
    weights = ScoringWeights()
    first = new_epoch(workspace, "alpha", board_file, brief_file, weights)
    new_epoch(
        workspace,
        "beta",
        board_file,
        brief_file,
        weights,
        auto_close_previous=False,
    )
    # First epoch remains open.
    assert not load_epoch(workspace, first.id).closed


def test_new_epoch_accepts_in_memory_objects_without_prior_save(
    workspace: Path,
) -> None:
    """new_epoch can be handed live objects — no caller-side ``.save()``.

    The board is an in-memory :class:`Board`, the proposer brief a
    :class:`ProposerBrief`, the weights an in-memory
    :class:`ScoringWeights`. None of them is persisted by the caller;
    ``new_epoch`` owns canonicalization + persistence. The frozen files
    must appear on disk and the contract hash must be set.
    """
    board = (
        Board()
        .add(Entry(id="e1", input="hi", budget_s=60))
        .add(Entry(id="e2", input="bye", budget_s=60))
    )
    brief = ProposerBrief(
        text="# Proposer brief\n\n# Forbidden edits\n- Avoid `router__sp`.\n",
        forbidden_ids=("router__sp",),
        preferred_ids=(),
    )
    weights = ScoringWeights(pass_weight=3.0)

    cfg = new_epoch(
        workspace_root=workspace,
        name="in memory",
        board_source=board,
        brief_source=brief,
        weights=weights,
    )

    edir = workspace / "epochs" / cfg.id
    # The frozen contracts were written by new_epoch itself.
    assert (edir / "board.jsonl").exists()
    assert (edir / "brief.md").exists()
    assert (edir / "scoring.json").exists()
    # Board round-trips: both in-memory entries were persisted.
    from zicato.board.jsonl import load_board

    persisted = load_board(edir / "board.jsonl")
    assert {e.id for e in persisted} == {"e1", "e2"}
    # The brief's source text was written verbatim.
    assert (edir / "brief.md").read_text() == brief.text
    # Scoring weights were serialized from the in-memory object.
    assert json.loads((edir / "scoring.json").read_text())["pass_weight"] == 3.0
    # The contract hash is populated (64-hex sha256).
    assert len(cfg.contract_hash) == 64
    assert load_epoch(workspace, cfg.id).contract_hash == cfg.contract_hash


def test_new_epoch_accepts_brief_as_plain_text(workspace: Path, board_file: Path) -> None:
    """A plain ``str`` brief_source is treated as proposer-brief text."""
    cfg = new_epoch(
        workspace_root=workspace,
        name="text brief",
        board_source=board_file,
        brief_source="# Proposer brief\n\nFree-form guidance.\n",
        weights=ScoringWeights(),
    )
    written = (workspace / "epochs" / cfg.id / "brief.md").read_text()
    assert written == "# Proposer brief\n\nFree-form guidance.\n"


# ---------------------------------------------------------------------------
# close_epoch
# ---------------------------------------------------------------------------


def test_close_epoch_marks_closed_and_writes_analysis(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    cfg = new_epoch(workspace, "alpha", board_file, brief_file, ScoringWeights())

    async def stub_call(system: str, user: str, model: str) -> str:
        # Echo a fixed structured response so we can assert on it.
        return (
            f"# Epoch analysis: {cfg.id}\n\n"
            "## Headline movements\n- A\n\n"
            "## Hypotheses that held\n- B\n\n"
            "## Hypotheses that didn't\n- C\n\n"
            "## Surface still open at epoch close\n- D\n\n"
            "## Recommended focus for next epoch\n- E\n"
        )

    out = close_epoch(workspace, cfg.id, aux_call_llm=stub_call)
    assert out.exists()
    text = out.read_text()
    assert f"# Epoch analysis: {cfg.id}" in text
    assert "## Headline movements" in text
    assert "## Recommended focus for next epoch" in text

    # Persistent state updated.
    refreshed = load_epoch(workspace, cfg.id)
    assert refreshed.closed
    assert refreshed.closed_at


def test_close_epoch_without_aux_writes_stub(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    cfg = new_epoch(workspace, "alpha", board_file, brief_file, ScoringWeights())
    out = close_epoch(workspace, cfg.id, aux_call_llm=None)
    text = out.read_text()
    assert f"# Epoch analysis: {cfg.id}" in text
    assert "stub" in text.lower()
    assert load_epoch(workspace, cfg.id).closed


def test_close_epoch_uses_current_when_id_omitted(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    cfg = new_epoch(workspace, "alpha", board_file, brief_file, ScoringWeights())
    close_epoch(workspace, None, aux_call_llm=None)
    assert load_epoch(workspace, cfg.id).closed


def test_close_epoch_with_no_current_raises(workspace: Path) -> None:
    with pytest.raises(RuntimeError, match="no current_epoch marker"):
        close_epoch(workspace, None, aux_call_llm=None)


# ---------------------------------------------------------------------------
# list / switch / current
# ---------------------------------------------------------------------------


def test_list_epochs_returns_creation_order(
    workspace: Path,
    board_file: Path,
    brief_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    weights = ScoringWeights()
    # ``created_at`` has second precision and ``list_epochs`` sorts by it,
    # so the three epochs must carry strictly-increasing timestamps for the
    # creation-order assertion to be meaningful. Rather than burn ~2s
    # sleeping across the second boundary, drive the timestamp source
    # (``lifecycle._now_iso``) through an increasing sequence of distinct
    # second-precision stamps — the ordering the sort keys on is exactly
    # what we control here.
    from zicato.epoch import lifecycle as _lifecycle

    # Every ``_now_iso`` call (created_at, and any auto-close closed_at)
    # advances one second, so each successive new_epoch's created_at is
    # strictly later than the prior epoch's — without depending on how many
    # times the lifecycle internally stamps per call.
    base = _dt.datetime(2026, 5, 15, 0, 0, 0, tzinfo=_dt.UTC)
    ticks = iter(range(1, 1000))

    def _fake_now_iso() -> str:
        return (base + _dt.timedelta(seconds=next(ticks))).replace(microsecond=0).isoformat()

    monkeypatch.setattr(_lifecycle, "_now_iso", _fake_now_iso)

    a = new_epoch(workspace, "alpha", board_file, brief_file, weights)
    b = new_epoch(workspace, "beta", board_file, brief_file, weights)
    c = new_epoch(workspace, "gamma", board_file, brief_file, weights)
    epochs = list_epochs(workspace)
    ids = [e.id for e in epochs]
    assert ids == [a.id, b.id, c.id]
    # The ordering is carried by the (increasing) created_at stamps, not by
    # epoch-id lexical order — assert the stamps actually differ.
    created = [e.created_at for e in epochs]
    assert created == sorted(created)
    assert len(set(created)) == 3


def test_list_epochs_empty_when_no_workspace(tmp_path: Path) -> None:
    assert list_epochs(tmp_path / ".zicato") == []


def test_list_epochs_skips_directories_without_config(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    new_epoch(workspace, "alpha", board_file, brief_file, ScoringWeights())
    # Drop a stub directory with no config.json.
    (workspace / "epochs" / "junk").mkdir()
    epochs = list_epochs(workspace)
    assert len(epochs) == 1


def _write_epoch_config(workspace: Path, epoch_id: str, created_at: str) -> None:
    """Materialize a minimal ``epochs/<id>/config.json`` directly on disk.

    Bypasses ``new_epoch`` so the test can pin both the id and the
    ``created_at`` stamp — the two inputs to the canonical sort key.
    """
    edir = workspace / "epochs" / epoch_id
    edir.mkdir(parents=True)
    (edir / "config.json").write_text(
        json.dumps(
            {
                "id": epoch_id,
                "name": epoch_id,
                "created_at": created_at,
                "board_path": "board.jsonl",
                "brief_path": "brief.md",
                "contract_hash": "deadbeef",
                "closed": False,
            }
        ),
        encoding="utf-8",
    )


def test_list_epochs_uses_canonical_numeric_tiebreaker(workspace: Path) -> None:
    """``list_epochs`` orders by the canonical timestamp-first key, breaking
    ties by the *numeric-aware* id key — not the old lexical ``c.id`` sort.

    With a shared ``created_at`` the tiebreaker decides the order. Lexical
    order would place ``e10`` before ``e2`` (string ``"1" < "2"``); the
    canonical ``natural_key`` places ``e2`` before ``e10``. This is the
    ordering bug the workspace enumeration layer was introduced to fix, and
    the parity REINDEX-DUMP fixture is single-epoch so it cannot catch a
    multi-epoch tiebreak regression on its own.
    """
    from zicato.workspace import WorkspaceLayout, list_epoch_ids

    shared = "2026-05-15T00:00:00+00:00"
    # Insert in an order that is neither the lexical nor the natural order,
    # so a passing assertion can't be an accident of disk-walk order.
    for eid in ("e10", "e2", "e1"):
        _write_epoch_config(workspace, eid, shared)

    ids = [c.id for c in list_epochs(workspace)]
    assert ids == ["e1", "e2", "e10"]  # numeric-aware, NOT lexical e1/e10/e2

    # Byte-identical to the single enumeration authority — that exact
    # agreement is the entire point of routing through it.
    layout = WorkspaceLayout.from_root(workspace)
    assert ids == list_epoch_ids(layout)


def test_list_epochs_orders_by_timestamp_over_id(workspace: Path) -> None:
    """The canonical key is timestamp-FIRST: a later-stamped epoch sorts
    after an earlier-stamped one regardless of how their ids compare.
    """
    _write_epoch_config(workspace, "zzz", "2026-05-15T00:00:00+00:00")
    _write_epoch_config(workspace, "aaa", "2026-05-16T00:00:00+00:00")
    ids = [c.id for c in list_epochs(workspace)]
    assert ids == ["zzz", "aaa"]


def test_switch_epoch_updates_marker(workspace: Path, board_file: Path, brief_file: Path) -> None:
    weights = ScoringWeights()
    a = new_epoch(workspace, "alpha", board_file, brief_file, weights)
    b = new_epoch(workspace, "beta", board_file, brief_file, weights)
    assert current_epoch_id(workspace) == b.id
    switch_epoch(workspace, a.id)
    assert current_epoch_id(workspace) == a.id


def test_switch_epoch_rejects_unknown_id(workspace: Path) -> None:
    with pytest.raises(FileNotFoundError):
        switch_epoch(workspace, "definitely_not_a_real_epoch")


def test_current_epoch_id_returns_none_when_marker_missing(workspace: Path) -> None:
    assert current_epoch_id(workspace) is None


def test_load_epoch_round_trips(workspace: Path, board_file: Path, brief_file: Path) -> None:
    weights = ScoringWeights(pass_weight=3.0, promote_margin=0.05)
    cfg = new_epoch(workspace, "alpha", board_file, brief_file, weights)
    loaded = load_epoch(workspace, cfg.id)
    assert loaded.id == cfg.id
    assert loaded.name == "alpha"
    assert loaded.scoring.pass_weight == 3.0
    assert loaded.scoring.pass_weight == 3.0
    assert loaded.scoring.promote_margin == 0.05
    assert loaded.closed is False


def test_load_epoch_missing_raises(workspace: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_epoch(workspace, "nothing")


# ---------------------------------------------------------------------------
# Per-judge weight preservation through the frozen scoring.json
#
# Twin of the workspace_loader fix (#179): ``_scoring_from_dict`` (read)
# and ``scoring_to_dict`` (write) on the epoch-creation path must
# preserve ``per_judge_weights`` and ``default_judge_weight``. Without
# the fix, the frozen ``<epoch>/scoring.json`` silently drops the
# operator's per-judge weighting, so re-reading the frozen contract
# (e.g. for re-analysis or replay) reverts to dataclass defaults even
# though the live workspace-level ``scoring.json`` had them.
# ---------------------------------------------------------------------------


def test_scoring_from_dict_preserves_per_judge_weights() -> None:
    """``_scoring_from_dict`` round-trips ``per_judge_weights`` +
    ``default_judge_weight``.

    Mirrors :func:`zicato.workspace_loader.scoring_weights_from_dict`
    after #179. The helper is private but exercised here directly so the
    fix-shape is locked in at the unit level.
    """
    from zicato.epoch.lifecycle import _scoring_from_dict

    payload = {
        "pass_weight": 1.0,
        "severity_weights": {"info": 1.0, "warning": 3.0, "critical": 10.0},
        "per_kind_weights": {},
        "per_judge_weights": {"quality": 4.0, "no_pii": 7.0},
        "default_judge_weight": 2.5,
        "plan_revision_weight": 0.5,
        "promote_margin": 0.01,
        "pass_rate_monotonicity": True,
    }
    weights = _scoring_from_dict(payload)
    assert dict(weights.per_judge_weights) == {"quality": 4.0, "no_pii": 7.0}
    assert weights.default_judge_weight == 2.5


def test_scoring_from_dict_defaults_when_per_judge_fields_absent() -> None:
    """Legacy ``scoring.json`` (no per_judge_weights / default_judge_weight
    keys) loads at the dataclass defaults — empty mapping + ``1.0`` —
    so an epoch frozen before #179 still loads cleanly."""
    from zicato.epoch.lifecycle import _scoring_from_dict

    legacy_payload = {
        "pass_weight": 1.0,
        "severity_weights": {"info": 1.0, "warning": 3.0, "critical": 10.0},
        "per_kind_weights": {},
        "plan_revision_weight": 0.5,
        "promote_margin": 0.01,
        "pass_rate_monotonicity": True,
    }
    weights = _scoring_from_dict(legacy_payload)
    assert dict(weights.per_judge_weights) == {}
    assert weights.default_judge_weight == 1.0


def test_new_epoch_freezes_per_judge_weights_into_scoring_json(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    """End-to-end: hand ``new_epoch`` a ScoringWeights with non-default
    ``per_judge_weights`` + ``default_judge_weight`` and confirm both
    fields survive the freeze (raw JSON on disk) and the round-trip
    through :func:`load_epoch` (parsed ScoringWeights)."""
    weights = ScoringWeights(
        per_judge_weights={"quality": 4.0, "no_pii": 7.0},
        default_judge_weight=2.5,
    )
    cfg = new_epoch(workspace, "per-judge", board_file, brief_file, weights)

    # The raw frozen file carries the fields verbatim (write side).
    frozen = json.loads(
        (workspace / "epochs" / cfg.id / "scoring.json").read_text(encoding="utf-8")
    )
    assert frozen["per_judge_weights"] == {"quality": 4.0, "no_pii": 7.0}
    assert frozen["default_judge_weight"] == 2.5

    # The parsed ScoringWeights from load_epoch carries them too (read side).
    loaded = load_epoch(workspace, cfg.id)
    assert dict(loaded.scoring.per_judge_weights) == {"quality": 4.0, "no_pii": 7.0}
    assert loaded.scoring.default_judge_weight == 2.5


def test_new_epoch_with_default_weights_freezes_empty_per_judge(
    workspace: Path, board_file: Path, brief_file: Path
) -> None:
    """Default ScoringWeights (no per-judge override) still write the
    keys — as an empty mapping + 1.0 — so the frozen schema is
    stable across epochs regardless of whether the operator opted into
    per-judge weighting."""
    cfg = new_epoch(workspace, "defaults", board_file, brief_file, ScoringWeights())
    frozen = json.loads(
        (workspace / "epochs" / cfg.id / "scoring.json").read_text(encoding="utf-8")
    )
    assert frozen["per_judge_weights"] == {}
    assert frozen["default_judge_weight"] == 1.0
    loaded = load_epoch(workspace, cfg.id)
    assert dict(loaded.scoring.per_judge_weights) == {}
    assert loaded.scoring.default_judge_weight == 1.0
