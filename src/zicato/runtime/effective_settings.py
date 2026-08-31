"""The settings a run is operating under, each with the tier that set it.

A run's behaviour is decided by knobs that arrive from four places: the
dataclass field defaults, the workspace ``config.json``, the CLI flags a
command pins for the process, and the host itself — the worker ceiling is
derived from the usable CPU count when nothing pins it. Reading an effective
value back off a running system meant re-deriving that composition by hand,
and a ceiling nobody wrote down is indistinguishable, from the outside, from
one an operator chose.

:func:`effective_settings` composes the whole map once — ``{name: {"value":
..., "source": ...}}``, keyed by the dotted name the knob carries in
configuration — and the evolve loop stamps it onto the run's heartbeat
record. The field is additive: a reader that does not know it ignores it.

Two knobs are resolved rather than read, and both keep their rule in
:mod:`zicato.runtime_factory` where the configuration is built:
``runtime.parallelism`` is the one knob all three configurable tiers can
set, and ``runtime.host_worker_permits`` reports the count actually in
force, so an AUTO ceiling is checkable against the machine that ran it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

from zicato.core.types import RuntimeConfig

#: The knob is at the default its dataclass field declares.
SOURCE_DEFAULT = "default"
#: The workspace ``config.json`` sets the knob.
SOURCE_WORKSPACE = "workspace config.json"
#: A CLI flag pinned the knob for this process (``zicato.config.pin_overrides``).
SOURCE_PINNED_FLAG = "pinned CLI flag"
#: The value was derived from the host's usable CPU count.
SOURCE_HOST_CPU_COUNT = "host CPU count"

#: Every tier a recorded setting can name.
SOURCE_TIERS: tuple[str, ...] = (
    SOURCE_DEFAULT,
    SOURCE_WORKSPACE,
    SOURCE_PINNED_FLAG,
    SOURCE_HOST_CPU_COUNT,
)

#: The :class:`~zicato.core.types.RuntimeConfig` fields this record reports.
#: Each is a knob the workspace ``config.json`` ``runtime`` block can set
#: under its own name, so presence of the key in that block is what separates
#: a configured value from a defaulted one.
RECORDED_RUNTIME_KNOBS: tuple[str, ...] = (
    "instance_id",
    "seed",
    "parallelism",
    "propose_parallelism",
    "host_worker_permits",
    "scrub_worker_env",
    "worker_env_passthrough",
    "diversity_tolerance",
    "infra_abort_round_threshold",
    "infra_backoff_base_s",
    "infra_backoff_cap_s",
    "max_tokens_per_round",
    "persist_run_results",
    "persist_judge_io",
    "preflight_gate",
    "preflight_probe_points",
    "preflight_probe_mutation_ids",
)

#: The :class:`~zicato.core.types.RuntimeConfig` fields the record leaves out,
#: each with the reason it is not a recordable setting. A guard test requires
#: every field to be either recorded or named here, so a new knob cannot join
#: the config silently.
UNRECORDED_RUNTIME_FIELDS: Mapping[str, str] = {
    "workspace_root": "the path the run was invoked against, not a tuned knob",
    "harness_call_llm": "a resolved callable; the dotted path is a models-block setting",
    "auxiliary_call_llm": "a resolved callable; the dotted path is a models-block setting",
    "judge_call_llm": "a resolved callable, set by the models block",
    "adjudicator_call_llm": "a resolved callable, set by the models block",
    "user_emulator_call_llm": "a resolved callable, set by the models block",
    "proposer_call_llm": "a resolved callable, set by the models block",
    "proposer_breadth_call_llm": "a resolved callable, set by the models block",
    "proposer_depth_call_llm": "a resolved callable, set by the models block",
    "proposer_breadth_model": "a model name, set by the models block",
    "proposer_depth_model": "a model name, set by the models block",
    "proposer_model": "a model name, set by the models block",
    "inner_model": "a live model object built from the models block",
    "token_ledger": "a per-round tally minted at run time, not configuration",
    "judge_io_sink": "a live sink object the worker binds, not configuration",
    "supervisor_kill_wait_s": "no tier sets it; the factory never reads it from a file",
}


def _jsonable(value: Any) -> Any:
    """Render one setting value in the JSON types the record can hold."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value


def _entry(value: Any, source: str) -> dict[str, Any]:
    """One recorded setting: what it is, and which tier decided it."""
    return {"value": _jsonable(value), "source": source}


def effective_settings(
    config: RuntimeConfig, runtime_block: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Return every effective setting of a run, paired with its source tier.

    ``config`` is the :class:`~zicato.core.types.RuntimeConfig` the run was
    built with, so the recorded values are the ones in force rather than a
    second reading of the same files. ``runtime_block`` is the workspace
    ``config.json`` ``runtime`` object that built it, which is what tells a
    configured value from a defaulted one.

    The typed configuration tree (:class:`zicato.config.ZicatoConfig`) is
    read here rather than passed in: it is process state, composed from the
    dataclass defaults and whatever the command pinned at startup. An
    embedding application that passes ``overrides`` to a single
    :func:`zicato.config.load_config` call is not represented, because that
    override lives only in that call and no runtime path uses one.
    """
    from zicato.config import (  # noqa: PLC0415 — the driver layer loads late
        ZicatoConfig,
        get_pinned_overrides,
        load_config,
    )
    from zicato.runtime.spawn_permit import effective_permit_count  # noqa: PLC0415
    from zicato.runtime_factory import (  # noqa: PLC0415 — avoid an import cycle
        resolve_host_worker_permits,
        resolve_parallelism,
    )

    pinned = get_pinned_overrides()
    typed = load_config()
    settings: dict[str, dict[str, Any]] = {}

    for section in fields(ZicatoConfig):
        block = getattr(typed, section.name)
        pinned_here = pinned.get(section.name, {})
        for knob in fields(block):
            source = SOURCE_PINNED_FLAG if knob.name in pinned_here else SOURCE_DEFAULT
            settings[f"{section.name}.{knob.name}"] = _entry(getattr(block, knob.name), source)

    for name in RECORDED_RUNTIME_KNOBS:
        source = SOURCE_WORKSPACE if name in runtime_block else SOURCE_DEFAULT
        settings[f"runtime.{name}"] = _entry(getattr(config, name), source)

    value, source = resolve_parallelism(runtime_block)
    settings["runtime.parallelism"] = _entry(value, source)

    limit, permits_source = resolve_host_worker_permits(runtime_block)
    settings["runtime.host_worker_permits"] = _entry(effective_permit_count(limit), permits_source)

    return settings
