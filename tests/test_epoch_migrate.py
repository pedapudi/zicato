"""Tests for :mod:`zicato.epoch.migrate`."""

from __future__ import annotations

import json
from pathlib import Path

from zicato.epoch.migrate import migrate_inline_to_perpatch


def _write_legacy_experiment(gen_dir: Path) -> dict[str, object]:
    """Drop a legacy-shape ``experiment.json`` into ``gen_dir``."""
    body: dict[str, object] = {
        "id": "exp_legacy",
        "epoch_id": "e0",
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
        "patches": [
            {
                "id": "p_a",
                "mutation_id": "x",
                "op": "replace",
                "new_content": "a",
                "new_numeric": None,
                "new_enum": None,
                "rationale": "first",
            },
            {
                "id": "p_b",
                "mutation_id": "x",
                "op": "set_numeric",
                "new_content": None,
                "new_numeric": 0.5,
                "new_enum": None,
                "rationale": "second",
            },
        ],
        "outcome": None,
    }
    gen_dir.mkdir(parents=True, exist_ok=True)
    (gen_dir / "experiment.json").write_text(json.dumps(body))
    return body


def test_migrate_writes_per_patch_files(tmp_path: Path) -> None:
    gdir = tmp_path / "v_legacy"
    _write_legacy_experiment(gdir)

    summary = migrate_inline_to_perpatch(gdir)

    assert summary.success is True
    assert summary.already_per_patch is False
    assert summary.migrated_patch_ids == ("p_a", "p_b")
    # Per-patch files exist.
    assert (gdir / "patches" / "p_a.json").exists()
    assert (gdir / "patches" / "p_b.json").exists()
    pa = json.loads((gdir / "patches" / "p_a.json").read_text())
    assert pa["new_content"] == "a"
    # experiment.json now uses patch_ids.
    body = json.loads((gdir / "experiment.json").read_text())
    assert body["patch_ids"] == ["p_a", "p_b"]
    assert "patches" not in body


def test_migrate_idempotent_on_new_shape(tmp_path: Path) -> None:
    gdir = tmp_path / "v_new"
    gdir.mkdir()
    body = {
        "id": "exp_new",
        "patch_ids": ["existing_id"],
        "hypothesis": {},
        "outcome": None,
    }
    (gdir / "experiment.json").write_text(json.dumps(body))

    summary = migrate_inline_to_perpatch(gdir)
    assert summary.success is True
    assert summary.already_per_patch is True
    assert summary.migrated_patch_ids == ()
    # File is unchanged (no patches/ created).
    assert not (gdir / "patches").exists()


def test_migrate_missing_experiment_json_reports_failure(tmp_path: Path) -> None:
    gdir = tmp_path / "v_empty"
    gdir.mkdir()
    summary = migrate_inline_to_perpatch(gdir)
    assert summary.success is False
    assert "experiment.json not found" in summary.error


def test_migrate_inline_patch_without_id_reports_failure(tmp_path: Path) -> None:
    gdir = tmp_path / "v_bad"
    gdir.mkdir()
    (gdir / "experiment.json").write_text(
        json.dumps(
            {
                "id": "exp",
                "patches": [{"mutation_id": "x", "op": "replace"}],
                "hypothesis": {},
                "outcome": None,
            }
        )
    )
    summary = migrate_inline_to_perpatch(gdir)
    assert summary.success is False
    assert "missing 'id'" in summary.error


def test_migrate_experiment_with_no_patches_normalises_to_empty_list(
    tmp_path: Path,
) -> None:
    gdir = tmp_path / "v_no_patches"
    gdir.mkdir()
    (gdir / "experiment.json").write_text(
        json.dumps(
            {
                "id": "exp_nop",
                "hypothesis": {},
                "outcome": None,
            }
        )
    )
    summary = migrate_inline_to_perpatch(gdir)
    assert summary.success is True
    assert summary.migrated_patch_ids == ()
    body = json.loads((gdir / "experiment.json").read_text())
    assert body["patch_ids"] == []
