"""The run's recorded settings, and the tier each one is attributed to.

A knob reaches a run from one of four places — the dataclass default, the
workspace ``config.json``, a CLI flag pinned for the process, or the host's
CPU count — and the record has to name the right one. Attribution is the
whole point of the map: an operator reading a ceiling back off a run needs
to know whether it is a number they chose.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from zicato.config import pin_overrides
from zicato.core.types import RuntimeConfig
from zicato.runtime.effective_settings import (
    RECORDED_RUNTIME_KNOBS,
    SOURCE_DEFAULT,
    SOURCE_HOST_CPU_COUNT,
    SOURCE_PINNED_FLAG,
    SOURCE_TIERS,
    SOURCE_WORKSPACE,
    UNRECORDED_RUNTIME_FIELDS,
    effective_settings,
)
from zicato.runtime.heartbeat import HeartbeatBeater
from zicato.runtime.spawn_permit import default_host_worker_permits
from zicato.runtime.state import Heartbeat, read_heartbeat
from zicato.runtime_factory import make_runtime_config


def _target_call_llm(*_args: object, **_kwargs: object) -> str:
    return ""


def _evaluation_call_llm(*_args: object, **_kwargs: object) -> str:
    return ""


def _config(tmp_path: Path, runtime_block: dict[str, object]) -> RuntimeConfig:
    """Build the RuntimeConfig a run with this ``runtime`` block would use.

    The two callables are distinct because the factory refuses a shared one
    (a harness that is also its own evaluation can collude on an emulated
    entry).
    """
    return make_runtime_config(
        {"runtime": runtime_block},
        workspace_root=tmp_path,
        target_call_llm=_target_call_llm,
        evaluation_call_llm=_evaluation_call_llm,
    )


# ---------------------------------------------------------------------------
# The four tiers
# ---------------------------------------------------------------------------


def test_a_knob_nobody_configured_is_attributed_to_the_default(tmp_path: Path) -> None:
    settings = effective_settings(_config(tmp_path, {}), {})

    assert settings["runtime.propose_parallelism"] == {"value": 4, "source": SOURCE_DEFAULT}
    assert settings["health.scoring_window"] == {"value": 3, "source": SOURCE_DEFAULT}


def test_a_knob_the_workspace_sets_is_attributed_to_config_json(tmp_path: Path) -> None:
    block: dict[str, object] = {"propose_parallelism": 8, "max_tokens_per_round": 50_000}
    settings = effective_settings(_config(tmp_path, block), block)

    assert settings["runtime.propose_parallelism"] == {"value": 8, "source": SOURCE_WORKSPACE}
    assert settings["runtime.max_tokens_per_round"] == {"value": 50_000, "source": SOURCE_WORKSPACE}


def test_a_pinned_flag_outranks_the_workspace_and_is_named(tmp_path: Path) -> None:
    """``--parallelism`` beats the same knob in the file, and the map says so."""
    block: dict[str, object] = {"parallelism": 12}
    pin_overrides({"runtime": {"parallelism": 3}, "aux": {"call_timeout_s": 30.0}})

    settings = effective_settings(_config(tmp_path, block), block)

    assert settings["runtime.parallelism"] == {"value": 3, "source": SOURCE_PINNED_FLAG}
    assert settings["aux.call_timeout_s"] == {"value": 30.0, "source": SOURCE_PINNED_FLAG}


def test_the_worker_ceiling_reports_the_count_the_host_resolved(tmp_path: Path) -> None:
    """An unset ceiling records the number in force, not the word AUTO.

    The knob's own value is ``None`` when nothing sets it, which says
    nothing about the machine the run happened on. Recording the resolved
    count is what makes the ceiling checkable after the fact.
    """
    settings = effective_settings(_config(tmp_path, {}), {})

    assert settings["runtime.host_worker_permits"] == {
        "value": default_host_worker_permits(),
        "source": SOURCE_HOST_CPU_COUNT,
    }


@pytest.mark.parametrize(
    ("written", "expected_value", "expected_source"),
    [
        (9, 9, SOURCE_WORKSPACE),
        (0, 0, SOURCE_WORKSPACE),
        # ``true`` reads as AUTO rather than ``int()``-ing to a one-worker
        # host, so the host is what decided the number.
        (True, None, SOURCE_HOST_CPU_COUNT),
        (False, 0, SOURCE_WORKSPACE),
    ],
)
def test_an_explicit_worker_ceiling_is_recorded_as_written(
    tmp_path: Path, written: object, expected_value: int | None, expected_source: str
) -> None:
    block: dict[str, object] = {"host_worker_permits": written}
    entry = effective_settings(_config(tmp_path, block), block)["runtime.host_worker_permits"]

    assert entry["source"] == expected_source
    assert entry["value"] == (
        default_host_worker_permits() if expected_value is None else expected_value
    )


# ---------------------------------------------------------------------------
# The record cannot rot
# ---------------------------------------------------------------------------


def test_every_runtime_field_is_recorded_or_carries_a_stated_reason() -> None:
    """A knob added to the runtime config cannot skip the record silently."""
    declared = {f.name for f in fields(RuntimeConfig)}
    recorded = set(RECORDED_RUNTIME_KNOBS)
    excused = set(UNRECORDED_RUNTIME_FIELDS)

    unaccounted = sorted(declared - recorded - excused)
    assert not unaccounted, (
        f"RuntimeConfig field(s) {unaccounted} are neither recorded nor excused — add "
        "them to RECORDED_RUNTIME_KNOBS, or to UNRECORDED_RUNTIME_FIELDS with the "
        "reason they are not a setting."
    )
    stale = sorted((recorded | excused) - declared)
    assert not stale, f"the record names {stale}, which RuntimeConfig no longer declares."
    overlap = sorted(recorded & excused)
    assert not overlap, f"{overlap} are both recorded and excused."


def test_every_entry_names_a_known_tier_and_a_json_value(tmp_path: Path) -> None:
    block: dict[str, object] = {"worker_env_passthrough": ["HTTPS_PROXY"], "seed": 7}
    settings = effective_settings(_config(tmp_path, block), block)

    for name, entry in settings.items():
        assert set(entry) == {"value", "source"}, name
        assert entry["source"] in SOURCE_TIERS, name
        assert isinstance(entry["value"], bool | int | float | str | list | type(None)), name
    # A tuple field reaches the record as a JSON list.
    assert settings["runtime.worker_env_passthrough"]["value"] == ["HTTPS_PROXY"]


# ---------------------------------------------------------------------------
# Carrying the map on the run record
# ---------------------------------------------------------------------------


def test_the_heartbeat_round_trips_the_map() -> None:
    recorded = {"runtime.parallelism": {"value": 4, "source": SOURCE_DEFAULT}}
    beat = Heartbeat(
        pid=1, instance_id="default", started_at="t", last_heartbeat="t", settings=recorded
    )

    assert Heartbeat.from_dict(beat.to_dict()) == beat


def test_a_heartbeat_without_the_field_reads_back_empty() -> None:
    """The field is additive: a record written before it existed still loads."""
    older = {"pid": 1, "instance_id": "default", "started_at": "t", "last_heartbeat": "t"}

    assert Heartbeat.from_dict(older).settings == {}


@pytest.mark.asyncio
async def test_the_beater_carries_the_map_onto_disk(tmp_path: Path) -> None:
    """Stamped once, the map survives every later periodic bump."""
    beater = HeartbeatBeater(tmp_path, "default", interval_s=10.0)
    beater.update(settings={"runtime.parallelism": {"value": 3, "source": SOURCE_PINNED_FLAG}})
    beater.bump_now()
    beater.update(phase="proposer")
    beater.bump_now()

    written = read_heartbeat(tmp_path)
    assert written is not None
    assert written.phase == "proposer"
    assert written.settings == {"runtime.parallelism": {"value": 3, "source": SOURCE_PINNED_FLAG}}
