"""Reflection run plan — round-trip + pre-registration stop/resume semantics.

The plan is a pre-registration contract (BOARD-REFLECTION.md §"the protocol"):
it round-trips through ``plan.json`` losslessly, its ``reflection_id`` is
deterministic under an injected timestamp (no wall-clock nondeterminism), and
the monotone ``executed`` flag is the stop/resume seam — a ``--pre-register``
plan is written un-executed and STOPS; a later run loads it, executes, and
re-writes it executed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zicato.epoch._storage import RecordError
from zicato.reflection.plan import (
    DEFAULT_CHECKS,
    MODE_ACTIVE,
    PLAN_FORMAT_VERSION,
    ReflectionPlan,
    make_reflection_id,
    new_plan,
    read_plan,
    write_plan,
)

CREATED_AT = "2026-07-01T00:00:00+00:00"


def _plan(**overrides: object) -> ReflectionPlan:
    kwargs: dict[str, object] = {
        "epoch_id": "epoch-1",
        "candidates": ["v0", "v1"],
        "entries": ["entryA", "entryB"],
        "replicates": 3,
        "created_at": CREATED_AT,
        "token": "seed",
    }
    kwargs.update(overrides)
    return new_plan(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# reflection_id determinism
# ---------------------------------------------------------------------------


def test_reflection_id_is_deterministic_under_injected_timestamp() -> None:
    a = make_reflection_id(CREATED_AT, token="seed")
    b = make_reflection_id(CREATED_AT, token="seed")
    assert a == b
    assert a.startswith("refl-20260701000000-")
    assert len(a.rsplit("-", 1)[1]) == 8  # 8 hex suffix


def test_reflection_id_random_suffix_when_no_token() -> None:
    a = make_reflection_id(CREATED_AT)
    b = make_reflection_id(CREATED_AT)
    assert a != b  # fresh uuid suffix each time
    assert a.startswith("refl-20260701000000-")


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_plan_json_round_trip() -> None:
    plan = _plan()
    restored = ReflectionPlan.from_json(plan.to_json())
    assert restored == plan
    assert restored.format_version == PLAN_FORMAT_VERSION
    assert restored.checks == DEFAULT_CHECKS
    assert restored.mode == MODE_ACTIVE
    assert restored.executed is False


def test_from_json_rejects_unknown_format_version() -> None:
    payload = _plan().to_json()
    payload["format_version"] = 99
    with pytest.raises(RecordError, match="format_version"):
        ReflectionPlan.from_json(payload)


def test_adjudicator_model_none_round_trips() -> None:
    plan = _plan(adjudicator_model=None)
    assert plan.adjudicator_model is None
    assert ReflectionPlan.from_json(plan.to_json()).adjudicator_model is None
    plan2 = _plan(adjudicator_model="meta-judge-x")
    assert ReflectionPlan.from_json(plan2.to_json()).adjudicator_model == "meta-judge-x"


# ---------------------------------------------------------------------------
# Pre-registration stop/resume (the executed flag)
# ---------------------------------------------------------------------------


def test_mark_executed_is_monotone_and_frozen() -> None:
    plan = _plan(pre_registered=True)
    assert plan.executed is False
    executed = plan.mark_executed()
    assert executed.executed is True
    # The original is frozen — untouched.
    assert plan.executed is False
    # Every other field is preserved.
    assert executed.reflection_id == plan.reflection_id
    assert executed.candidates == plan.candidates


def test_write_then_read_plan_round_trips_on_disk(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    plan = _plan(pre_registered=True)
    path = write_plan(workspace, plan)
    assert path.exists()
    assert path.name == "plan.json"
    # Path is under the reserved reflections subtree.
    assert "reflections" in path.parts
    assert plan.reflection_id in path.parts

    loaded = read_plan(workspace, plan.epoch_id, plan.reflection_id)
    assert loaded == plan
    assert loaded is not None
    assert loaded.pre_registered is True
    assert loaded.executed is False  # pre-registered: STOP here


def test_resume_overwrites_pre_registered_plan_as_executed(tmp_path: Path) -> None:
    workspace = tmp_path / ".zicato"
    plan = _plan(pre_registered=True)
    write_plan(workspace, plan)

    # Resume: load, execute, re-write executed over the same path.
    loaded = read_plan(workspace, plan.epoch_id, plan.reflection_id)
    assert loaded is not None
    write_plan(workspace, loaded.mark_executed())

    reloaded = read_plan(workspace, plan.epoch_id, plan.reflection_id)
    assert reloaded is not None
    assert reloaded.executed is True
    assert reloaded.pre_registered is True  # still a pre-registration, now run


def test_read_plan_absent_returns_none(tmp_path: Path) -> None:
    assert read_plan(tmp_path / ".zicato", "epoch-1", "refl-nope") is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", True),
        ("executed", "false"),
        ("pre_registered", 1),
        ("replicates", True),
        ("replicates", "3"),
        ("candidates", "v0"),
        ("entries", [1]),
        ("mode", "unknown"),
    ],
)
def test_plan_refuses_coercion(field: str, value: object) -> None:
    body = dict(_plan().to_json(), **{field: value})
    with pytest.raises(RecordError, match=field):
        ReflectionPlan.from_json(body)


def test_plan_preserves_historical_omissions_and_extension_numbers(tmp_path: Path) -> None:
    body = {
        "format_version": 1,
        "reflection_id": "refl-example",
        "epoch_id": "epoch-1",
        "replicates": 1,
        "extension": {"count": 1, "weight": 1.0},
    }
    plan = ReflectionPlan.from_json(body)
    path = write_plan(tmp_path, plan)
    assert path.read_bytes() == json.dumps(body, indent=2, sort_keys=True).encode()
    assert plan.to_json() == body
    assert plan.mark_executed().to_json() == dict(body, executed=True)
    assert plan.to_json() == body


def test_plan_refuses_present_corruption_and_location_mismatch(tmp_path: Path) -> None:
    plan = _plan()
    path = write_plan(tmp_path, plan)
    original = path.read_bytes()
    path.write_text("null", encoding="utf-8")
    with pytest.raises(RecordError, match="JSON object"):
        read_plan(tmp_path, plan.epoch_id, plan.reflection_id)
    with pytest.raises(RecordError):
        write_plan(tmp_path, plan)
    assert path.read_text() == "null"
    path.write_bytes(original)
    body = dict(plan.to_json(), epoch_id="epoch-other")
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(RecordError, match="location"):
        read_plan(tmp_path, plan.epoch_id, plan.reflection_id)


def test_plan_revision_failure_prevents_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zicato.reflection import plan as plan_module

    plan = _plan()
    path = write_plan(tmp_path, plan)
    original = path.read_bytes()

    def fail_mark(*args: object) -> None:
        raise OSError("revision unavailable")

    monkeypatch.setattr(plan_module, "mark_epoch_changed", fail_mark)
    with pytest.raises(OSError, match="revision unavailable"):
        write_plan(tmp_path, plan.mark_executed())
    assert path.read_bytes() == original
