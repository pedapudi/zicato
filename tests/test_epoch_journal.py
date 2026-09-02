"""Tests for :mod:`zicato.epoch.journal`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.core.types import (
    DriftMovementActual,
    ExpectedDriftMovement,
    Experiment,
    HypothesisSpec,
    OutcomeRecord,
    Patch,
)
from zicato.core.workspace import (
    experiment_json_path,
    generation_dir,
    journal_path,
    patch_json_path,
    patches_dir,
)
from zicato.epoch import (
    append_journal_entry,
    read_experiment,
    read_journal,
    update_experiment_outcome,
    write_experiment,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _experiment(
    *,
    generation_id: str = "v1",
    core_idea: str = "Tighten researcher's prompt to require citations.",
    modulating: tuple[str, ...] = ("researcher.instruction", "researcher.description"),
    why: str = (
        "Pattern observed across rounds 3-5: confabulation fires on 70% of "
        "research-tagged entries. The current instruction does not require "
        "source citations."
    ),
    outcome: OutcomeRecord | None = None,
) -> Experiment:
    hypothesis = HypothesisSpec(
        core_idea=core_idea,
        modulating=modulating,
        why=why,
        expected_drift_movements=(
            ExpectedDriftMovement(
                kind="off_topic",
                direction="decrease",
                magnitude="medium",
            ),
        ),
        expected_pass_rate_delta="+0.0 to +0.15",
        risks="May over-cite when sources are unavailable.",
    )
    patches = (
        Patch(
            id="p1",
            mutation_id="researcher.instruction",
            op="replace",
            new_content="new instruction",
            new_numeric=None,
            new_enum=None,
            rationale="cite sources",
        ),
    )
    return Experiment(
        id=f"exp_test_{generation_id}",
        epoch_id="2026-04-08_test",
        generation_id=generation_id,
        parent_generation_id="v0",
        proposed_at="2026-04-08T12:00:00+00:00",
        hypothesis=hypothesis,
        patches=patches,
        outcome=outcome,
    )


def _outcome(
    decision: str = "promoted",
    rejection_reason: str = "",
) -> OutcomeRecord:
    return OutcomeRecord(
        ran_at="2026-04-08T12:30:00+00:00",
        drift_movements=(
            DriftMovementActual(
                kind="off_topic",
                from_rate=0.7,
                to_rate=0.2,
                hypothesis_match=True,
            ),
        ),
        pass_rate_delta=0.05,
        drift_loss_delta=-0.18,
        scalar_score_delta=-0.20,
        tournament_decision=decision,  # type: ignore[arg-type]
        rejection_reason=rejection_reason,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_append_journal_entry_creates_file(epoch_root: tuple[Path, str]) -> None:
    ws, eid = epoch_root
    exp = _experiment(outcome=_outcome())
    append_journal_entry(ws, eid, exp)
    path = journal_path(ws, eid)
    assert path.exists()
    text = path.read_text()
    assert "## v1 — Tighten researcher's prompt to require citations." in text
    assert "**proposed_at**: 2026-04-08T12:00:00+00:00" in text
    assert "**modulating**: researcher.instruction, researcher.description" in text
    assert "**why**: Pattern observed across rounds 3-5" in text
    assert "**outcome**: promoted" in text
    assert "Δscalar=-0.200" in text
    assert "Δpass_rate=+0.050" in text


def test_append_journal_entry_without_outcome(epoch_root: tuple[Path, str]) -> None:
    ws, eid = epoch_root
    exp = _experiment(outcome=None)
    append_journal_entry(ws, eid, exp)
    text = read_journal(ws, eid)
    assert "## v1 —" in text
    assert "**modulating**:" in text
    assert "**outcome**:" not in text
    assert "**rejection_reason**:" not in text


def test_append_journal_entry_rejected_includes_reason(
    epoch_root: tuple[Path, str],
) -> None:
    ws, eid = epoch_root
    exp = _experiment(
        outcome=_outcome(
            decision="rejected",
            rejection_reason="pass_rate_regression_on_summarise_short",
        )
    )
    append_journal_entry(ws, eid, exp)
    text = read_journal(ws, eid)
    assert "**outcome**: rejected" in text
    assert "**rejection_reason**: pass_rate_regression_on_summarise_short" in text


def test_append_journal_entry_appends_multiple_sections(
    epoch_root: tuple[Path, str],
) -> None:
    ws, eid = epoch_root
    a = _experiment(generation_id="v1", outcome=_outcome())
    b = _experiment(
        generation_id="v2",
        core_idea="Reduce coordinator re-routing.",
        outcome=_outcome(decision="rejected", rejection_reason="regression"),
    )
    append_journal_entry(ws, eid, a)
    append_journal_entry(ws, eid, b)
    text = read_journal(ws, eid)
    assert text.count("## v1 —") == 1
    assert text.count("## v2 —") == 1
    # v1 appears before v2.
    assert text.index("## v1 —") < text.index("## v2 —")


def test_append_journal_entry_keeps_the_whole_why(
    epoch_root: tuple[Path, str],
) -> None:
    """DELIBERATELY INVERTED (issue #123).

    This used to assert ``"Second sentence" not in text`` — it pinned the
    write-time truncation as intended behaviour. It is not: ``journal.md``
    is append-only and is the durable narrative of what each round tried,
    so a sentence dropped here is lost permanently. The budget
    now lives on the readers (the two analysis paths), where it is
    recoverable.
    """
    ws, eid = epoch_root
    exp = _experiment(
        why="First sentence. Second sentence with more detail.",
        outcome=None,
    )
    append_journal_entry(ws, eid, exp)
    text = read_journal(ws, eid)
    assert "**why**: First sentence. Second sentence with more detail." in text


def test_append_journal_entry_missing_epoch_dir(tmp_path: Path) -> None:
    ws = tmp_path / ".zicato"
    with pytest.raises(FileNotFoundError):
        append_journal_entry(ws, "missing", _experiment())


def test_read_journal_returns_empty_when_missing(tmp_path: Path) -> None:
    assert read_journal(tmp_path / ".zicato", "missing") == ""


def test_append_journal_handles_empty_modulating(
    epoch_root: tuple[Path, str],
) -> None:
    ws, eid = epoch_root
    exp = _experiment(modulating=(), outcome=None)
    append_journal_entry(ws, eid, exp)
    text = read_journal(ws, eid)
    assert "**modulating**: (none)" in text


# ---------------------------------------------------------------------------
# Per-patch storage: write_experiment / read_experiment / update_experiment_outcome
# ---------------------------------------------------------------------------


def test_write_experiment_uses_per_patch_layout(
    epoch_root: tuple[Path, str],
) -> None:
    ws, eid = epoch_root
    exp = _experiment(outcome=None)
    write_experiment(ws, eid, "v1", exp)

    # experiment.json carries patch_ids, NOT inline patches.
    body = json.loads(experiment_json_path(ws, eid, "v1").read_text())
    assert body["patch_ids"] == ["p1"]
    assert "patches" not in body
    # The per-patch file exists with the right payload.
    ppath = patch_json_path(ws, eid, "v1", "p1")
    assert ppath.exists()
    pbody = json.loads(ppath.read_text())
    assert pbody["mutation_id"] == "researcher.instruction"
    assert pbody["new_content"] == "new instruction"


def test_read_experiment_round_trips_new_layout(
    epoch_root: tuple[Path, str],
) -> None:
    ws, eid = epoch_root
    exp = _experiment(outcome=_outcome())
    write_experiment(ws, eid, "v1", exp)

    loaded = read_experiment(ws, eid, "v1")
    assert loaded.id == exp.id
    assert loaded.hypothesis.core_idea == exp.hypothesis.core_idea
    assert len(loaded.patches) == 1
    assert loaded.patches[0].id == "p1"
    assert loaded.patches[0].mutation_id == "researcher.instruction"
    assert loaded.outcome is not None
    assert loaded.outcome.tournament_decision == "promoted"


def test_outcome_holdout_block_round_trips(epoch_root: tuple[Path, str]) -> None:
    # The Ladder/holdout evidence block (OVERFITTING.md §12 #2) survives a
    # write/read with its exact shape so the dashboard reads it back.
    from dataclasses import replace

    ws, eid = epoch_root
    block = {
        "confirmed": True,
        "train_scalar": 0.5,
        "holdout_scalar": 0.6,
        "ladder_released": True,
        "ladder_budget_total": 16,
        "ladder_budget_remaining": 15,
        "threshold": 0.01,
    }
    exp = _experiment(outcome=replace(_outcome(), holdout=block))
    write_experiment(ws, eid, "v1", exp)

    loaded = read_experiment(ws, eid, "v1")
    assert loaded.outcome is not None
    assert loaded.outcome.holdout == block


def test_outcome_holdout_block_absent_reads_as_none(epoch_root: tuple[Path, str]) -> None:
    # An outcome with no holdout block (the byte-identical Phase-A degrade, or
    # an older journal) reads back with holdout=None — no crash.
    ws, eid = epoch_root
    write_experiment(ws, eid, "v1", _experiment(outcome=_outcome()))
    loaded = read_experiment(ws, eid, "v1")
    assert loaded.outcome is not None
    assert loaded.outcome.holdout is None


def test_outcome_generalization_fields_round_trip(epoch_root: tuple[Path, str]) -> None:
    # The per-generation train/holdout loss + gap (OVERFITTING.md §12 #5)
    # survive a write/read with exact values so the dashboard + gap detector
    # read them back.
    from dataclasses import replace

    ws, eid = epoch_root
    exp = _experiment(
        outcome=replace(
            _outcome(),
            train_loss=0.42,
            holdout_loss=0.55,
            generalization_gap=0.13,
        )
    )
    write_experiment(ws, eid, "v1", exp)

    loaded = read_experiment(ws, eid, "v1")
    assert loaded.outcome is not None
    assert loaded.outcome.train_loss == 0.42
    assert loaded.outcome.holdout_loss == 0.55
    assert loaded.outcome.generalization_gap == 0.13


def test_outcome_generalization_fields_absent_read_as_none(
    epoch_root: tuple[Path, str],
) -> None:
    # No holdout (the byte-identical degrade / an older journal) reads back as
    # None on all three fields — no crash.
    ws, eid = epoch_root
    write_experiment(ws, eid, "v1", _experiment(outcome=_outcome()))
    loaded = read_experiment(ws, eid, "v1")
    assert loaded.outcome is not None
    assert loaded.outcome.train_loss is None
    assert loaded.outcome.holdout_loss is None
    assert loaded.outcome.generalization_gap is None


def test_write_experiment_persists_round_index(
    epoch_root: tuple[Path, str],
) -> None:
    """round_index is written into experiment.json and round-trips back, so the
    dashboard can attribute the generation to its birth round (issue #9)."""
    from dataclasses import replace

    ws, eid = epoch_root
    exp = replace(_experiment(outcome=_outcome()), round_index=2)
    write_experiment(ws, eid, "v1", exp)

    # it is in the on-disk body where the dashboard reader looks for it.
    body = json.loads((experiment_json_path(ws, eid, "v1")).read_text())
    assert body["round_index"] == 2
    # and it round-trips through the typed reader.
    assert read_experiment(ws, eid, "v1").round_index == 2


def test_read_experiment_defaults_round_index_when_absent(
    epoch_root: tuple[Path, str],
) -> None:
    """A pre-feature experiment.json with no round_index reads as 0 (no crash)."""
    ws, eid = epoch_root
    write_experiment(ws, eid, "v1", _experiment(outcome=_outcome()))
    path = experiment_json_path(ws, eid, "v1")
    body = json.loads(path.read_text())
    body.pop("round_index", None)
    path.write_text(json.dumps(body))
    assert read_experiment(ws, eid, "v1").round_index == 0


def test_read_experiment_accepts_legacy_inline_form(
    epoch_root: tuple[Path, str],
) -> None:
    """Workspaces created before the refactor have inline ``patches: [...]``."""
    ws, eid = epoch_root
    gdir = generation_dir(ws, eid, "v_legacy")
    gdir.mkdir(parents=True)

    legacy_body = {
        "id": "exp_legacy",
        "epoch_id": eid,
        "generation_id": "v_legacy",
        "parent_generation_id": "v0",
        "proposed_at": "2026-04-08T12:00:00+00:00",
        "hypothesis": {
            "core_idea": "legacy",
            "modulating": ["x"],
            "why": "history",
            "expected_drift_movements": [],
            "expected_pass_rate_delta": "+0.0",
            "risks": "",
        },
        # Inline patches — the OLD on-disk shape.
        "patches": [
            {
                "id": "p_legacy",
                "mutation_id": "x",
                "op": "replace",
                "new_content": "hello",
                "new_numeric": None,
                "new_enum": None,
                "rationale": "legacy",
            }
        ],
        "outcome": None,
    }
    (gdir / "experiment.json").write_text(json.dumps(legacy_body))

    loaded = read_experiment(ws, eid, "v_legacy")
    assert len(loaded.patches) == 1
    assert loaded.patches[0].id == "p_legacy"
    assert loaded.patches[0].new_content == "hello"


def test_update_experiment_outcome_preserves_patches(
    epoch_root: tuple[Path, str],
) -> None:
    ws, eid = epoch_root
    exp = _experiment(outcome=None)
    write_experiment(ws, eid, "v1", exp)

    new_outcome = _outcome(decision="rejected", rejection_reason="regression")
    updated = update_experiment_outcome(ws, eid, "v1", new_outcome)
    assert updated.outcome is not None
    assert updated.outcome.tournament_decision == "rejected"

    # Patches survived the rewrite.
    body = json.loads(experiment_json_path(ws, eid, "v1").read_text())
    assert body["patch_ids"] == ["p1"]
    assert patch_json_path(ws, eid, "v1", "p1").exists()

    # And round-trip reads see the new outcome.
    re_read = read_experiment(ws, eid, "v1")
    assert re_read.outcome is not None
    assert re_read.outcome.rejection_reason == "regression"


def test_write_experiment_with_zero_patches_omits_patches_dir(
    epoch_root: tuple[Path, str],
) -> None:
    ws, eid = epoch_root
    exp = Experiment(
        id="exp_zero",
        epoch_id=eid,
        generation_id="v_zero",
        parent_generation_id="v0",
        proposed_at="2026-04-08T12:00:00+00:00",
        hypothesis=HypothesisSpec(
            core_idea="no-op",
            modulating=(),
            why="placeholder",
            expected_drift_movements=(),
            expected_pass_rate_delta="+0.0",
        ),
        patches=(),
        outcome=None,
    )
    write_experiment(ws, eid, "v_zero", exp)

    body = json.loads(experiment_json_path(ws, eid, "v_zero").read_text())
    assert body["patch_ids"] == []
    # patches dir is allowed to be absent for a zero-patch experiment.
    pdir = patches_dir(ws, eid, "v_zero")
    assert not pdir.exists()


# ---------------------------------------------------------------------------
# Hypothesis metric-movements round-trip (the WS-REC phase-1 journal bug fix)
# ---------------------------------------------------------------------------


def test_hypothesis_metric_movements_round_trip(epoch_root: tuple[Path, str]) -> None:
    """``expected_metric_movements`` must survive a write/read round-trip.

    The writer always serialised them (``asdict`` over the whole
    hypothesis), but ``_hypothesis_from_dict`` silently dropped the key on
    read — every namespaced metric prediction was lost the moment a record
    was re-read (the outcome updater re-reads, so even one tournament round
    erased them). This test FAILS with the read-side fix stashed.
    """
    from zicato.core.types import ExpectedMetricMovement

    ws, eid = epoch_root
    movements = (
        ExpectedMetricMovement(
            metric_name="cost:tokens_spent", direction="decrease", magnitude="small"
        ),
        ExpectedMetricMovement(
            metric_name="rubric:slide_structure", direction="increase", magnitude="medium"
        ),
    )
    exp = _experiment()
    exp = Experiment(
        id=exp.id,
        epoch_id=exp.epoch_id,
        generation_id=exp.generation_id,
        parent_generation_id=exp.parent_generation_id,
        proposed_at=exp.proposed_at,
        hypothesis=HypothesisSpec(
            core_idea=exp.hypothesis.core_idea,
            modulating=exp.hypothesis.modulating,
            why=exp.hypothesis.why,
            expected_drift_movements=exp.hypothesis.expected_drift_movements,
            expected_pass_rate_delta=exp.hypothesis.expected_pass_rate_delta,
            risks=exp.hypothesis.risks,
            expected_metric_movements=movements,
        ),
        patches=exp.patches,
        outcome=None,
    )
    write_experiment(ws, eid, "v1", exp)

    # The writer really did persist them (this half held before the fix).
    body = json.loads(experiment_json_path(ws, eid, "v1").read_text())
    assert len(body["hypothesis"]["expected_metric_movements"]) == 2

    # The reader must reconstitute them — the bug dropped them here.
    re_read = read_experiment(ws, eid, "v1")
    assert re_read.hypothesis.expected_metric_movements == movements
    # And the round-trip is stable through the outcome updater's re-read.
    updated = update_experiment_outcome(ws, eid, "v1", _outcome())
    assert updated.hypothesis.expected_metric_movements == movements


def test_hypothesis_metric_movements_absent_reads_empty(
    epoch_root: tuple[Path, str],
) -> None:
    """A record written before the field (no key) reads as ``()``."""
    ws, eid = epoch_root
    write_experiment(ws, eid, "v1", _experiment())
    body = json.loads(experiment_json_path(ws, eid, "v1").read_text())
    del body["hypothesis"]["expected_metric_movements"]
    experiment_json_path(ws, eid, "v1").write_text(json.dumps(body, indent=2, sort_keys=True))
    re_read = read_experiment(ws, eid, "v1")
    assert re_read.hypothesis.expected_metric_movements == ()


# ---------------------------------------------------------------------------
# Recombination provenance (WS-REC): the CONDITIONAL recombined_from key
# ---------------------------------------------------------------------------


def test_recombined_from_round_trips(epoch_root: tuple[Path, str]) -> None:
    ws, eid = epoch_root
    exp = _experiment()
    exp = Experiment(
        id=exp.id,
        epoch_id=exp.epoch_id,
        generation_id=exp.generation_id,
        parent_generation_id=exp.parent_generation_id,
        proposed_at=exp.proposed_at,
        hypothesis=exp.hypothesis,
        patches=exp.patches,
        outcome=None,
        recombined_from=("v1", "v2"),
    )
    write_experiment(ws, eid, "v3", exp)
    body = json.loads(experiment_json_path(ws, eid, "v3").read_text())
    assert body["recombined_from"] == ["v1", "v2"]
    assert read_experiment(ws, eid, "v3").recombined_from == ("v1", "v2")
    # Survives the outcome updater's re-read + rewrite.
    updated = update_experiment_outcome(ws, eid, "v3", _outcome("rejected", "margin"))
    assert updated.recombined_from == ("v1", "v2")
    assert read_experiment(ws, eid, "v3").recombined_from == ("v1", "v2")


def test_recombined_from_key_omitted_at_default(epoch_root: tuple[Path, str]) -> None:
    """The DEFAULT-OFF byte-identity proof at the record level: an ordinary
    (non-recombined) experiment.json carries NO ``recombined_from`` key, so
    its serialised form is byte-identical to one written before the field
    existed."""
    ws, eid = epoch_root
    write_experiment(ws, eid, "v1", _experiment())
    body = json.loads(experiment_json_path(ws, eid, "v1").read_text())
    assert "recombined_from" not in body
    # The exact key set of the pre-field record shape — a new unconditional
    # key here would break byte-identity for every existing workspace.
    assert set(body) == {
        "format_version",
        "id",
        "epoch_id",
        "generation_id",
        "parent_generation_id",
        "proposed_at",
        "round_index",
        "hypothesis",
        "patch_ids",
        "outcome",
    }
    assert read_experiment(ws, eid, "v1").recombined_from == ()


def test_recombined_from_absent_on_legacy_record_reads_empty(
    epoch_root: tuple[Path, str],
) -> None:
    """An old-shape record (legacy inline patches, no key) reads as ()."""
    ws, eid = epoch_root
    gen_dir = generation_dir(ws, eid, "v9")
    gen_dir.mkdir(parents=True)
    (gen_dir / "experiment.json").write_text(
        json.dumps(
            {
                "id": "exp_legacy",
                "epoch_id": eid,
                "generation_id": "v9",
                "parent_generation_id": "v0",
                "proposed_at": "2026-01-01T00:00:00+00:00",
                "hypothesis": {
                    "core_idea": "legacy",
                    "modulating": [],
                    "why": "",
                    "expected_drift_movements": [],
                    "expected_pass_rate_delta": "",
                },
                "patches": [],
                "outcome": None,
            }
        )
    )
    assert read_experiment(ws, eid, "v9").recombined_from == ()
