"""Record selected operational settings and runtime-derived limits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

from zicato.core.settings import RuntimeSettings
from zicato.core.types import RuntimeConfig
from zicato.selection.replicates import REPLICATE_SOURCE_TIERS

#: The knob is at the default its dataclass field declares.
SOURCE_DEFAULT = "default"
#: The workspace ``config.json`` sets the knob.
SOURCE_WORKSPACE = "workspace"
#: The explicit invocation overlay sets the knob.
SOURCE_INVOCATION = "invocation"
#: The value was derived from the host's usable CPU count.
SOURCE_HOST_CPU_COUNT = "host CPU count"

#: Every tier a recorded setting can name. The last three belong to the
#: tournament's replicate count (:mod:`zicato.selection.replicates`): the
#: frozen contract, the measured noise floor, or the structure's default.
SOURCE_TIERS: tuple[str, ...] = (
    SOURCE_DEFAULT,
    SOURCE_WORKSPACE,
    SOURCE_INVOCATION,
    SOURCE_HOST_CPU_COUNT,
    *REPLICATE_SOURCE_TIERS,
)

#: The key under which the replicate count in effect is recorded. The loop
#: adds it once the epoch's floor is known, after the runtime knobs.
TOURNAMENT_REPLICATES_KEY = "tournament.replicates"

#: The :class:`~zicato.core.types.RuntimeConfig` fields this record reports.
#: Each is a knob the workspace ``config.json`` ``runtime`` block can set
#: under its own name, so presence of the key in that block is what separates
#: a configured value from a defaulted one.
RECORDED_RUNTIME_KNOBS: tuple[str, ...] = tuple(item.name for item in fields(RuntimeSettings))

#: The :class:`~zicato.core.types.RuntimeConfig` fields the record leaves out,
#: each with the reason it is not a recordable setting. A guard test requires
#: every field to be either recorded or named here, so a new knob cannot join
#: the config silently.
UNRECORDED_RUNTIME_FIELDS: Mapping[str, str] = {
    "workspace_root": "the path the run was invoked against, not a tuned knob",
    "target_call_llm": "a resolved callable; the dotted path is a models-block setting",
    "evaluation_call_llm": "a resolved callable; the dotted path is a models-block setting",
    "judge_call_llm": "a resolved callable, set by the models block",
    "adjudicator_call_llm": "a resolved callable, set by the models block",
    "user_emulator_call_llm": "a resolved callable, set by the models block",
    "proposer_call_llm": "a resolved callable, set by the models block",
    "proposer_breadth_call_llm": "a resolved callable, set by the models block",
    "proposer_depth_call_llm": "a resolved callable, set by the models block",
    "proposer_breadth_model": "a model name, set by the models block",
    "proposer_depth_model": "a model name, set by the models block",
    "proposer_model": "a model name, set by the models block",
    "target_model": "a live model object built from the models block",
    "token_ledger": "a per-round tally minted at run time, not configuration",
    "judge_io_sink": "a live sink object the worker binds, not configuration",
    "goldfive": "contract settings recorded in the epoch's scoring.json",
    "configuration": "the selected values and their sources, reported field by field",
    "telemetry": "invocation service addresses, excluded from evaluation identity",
    "driver_imports": "resolved driver roots and mutable-package scope from adapter registration",
    "run_context": "per-unit workspace, epoch, generation, run, snapshot, and scratch identities",
}


def _jsonable(value: Any) -> Any:
    """Render one setting value in the JSON types the record can hold."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value


def recorded_setting(value: Any, source: str) -> dict[str, Any]:
    """One recorded setting: what it is, and which tier decided it."""
    return {"value": _jsonable(value), "source": source}


def effective_settings(
    config: RuntimeConfig, runtime_block: Mapping[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Report the values the runtime carries, with their selected sources."""
    from zicato.runtime.spawn_permit import effective_permit_count  # noqa: PLC0415

    resolved = config.operational_configuration()
    settings = resolved.effective_settings()
    limit = config.host_worker_permits
    source = (
        SOURCE_HOST_CPU_COUNT if limit is None else resolved.sources["runtime.host_worker_permits"]
    )
    settings["runtime.host_worker_permits"] = recorded_setting(
        effective_permit_count(limit), source
    )
    return settings
