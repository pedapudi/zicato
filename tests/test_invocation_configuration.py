"""Operational choices remain attached to their invocation across worker transport."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from zicato.config import InvocationOverlay, ResolvedConfiguration, resolve_configuration
from zicato.core.configuration import ConfigurationError
from zicato.runtime_factory import make_runtime_config
from zicato.tournament.worker_transport import _configuration_spec


async def _target(system: str, user: str, model: str) -> str:
    return "answer"


async def _evaluation(system: str, user: str, model: str) -> str:
    return '{"score": 10}'


def test_overlay_detaches_inputs_and_preserves_each_selected_source():
    raw = {"runtime": {"parallelism": 3, "worker_env_passthrough": ["WORKER_TOKEN"]}}
    overlay = InvocationOverlay.from_mapping(raw)
    raw["runtime"]["parallelism"] = 9
    raw["runtime"]["worker_env_passthrough"].append("OTHER_TOKEN")
    resolved = resolve_configuration({"runtime": {"seed": 71, "parallelism": 8}}, overlay=overlay)
    assert resolved.values.runtime.parallelism == 3
    assert resolved.values.runtime.worker_env_passthrough == ("WORKER_TOKEN",)
    assert resolved.sources["runtime.parallelism"] == "invocation"
    assert resolved.sources["runtime.seed"] == "workspace"
    assert resolved.sources["runtime.propose_parallelism"] == "default"
    with pytest.raises(TypeError):
        overlay.overrides["runtime"]["parallelism"] = 12
    with pytest.raises(FrozenInstanceError):
        resolved.values.runtime.parallelism = 12


def test_cross_field_constraints_apply_after_overlay_composition():
    resolved = resolve_configuration(
        {"health": {"generalization_gap_crit": 0.7}},
        overlay=InvocationOverlay.from_mapping({"health": {"generalization_gap_warn": 0.5}}),
    )
    assert resolved.values.health.generalization_gap_warn == 0.5
    with pytest.raises(ConfigurationError, match="health.generalization_gap_crit"):
        resolve_configuration(
            {}, overlay=InvocationOverlay.from_mapping({"health": {"generalization_gap_warn": 0.5}})
        )


@pytest.mark.parametrize(
    "raw",
    [
        {"runtime": {"paralellism": 3}},
        {"runtime": {"parallelism": 2.8}},
        {"runtime": {"scrub_worker_env": "false"}},
        {"runtime": {"host_worker_permits": "true"}},
        {"health": {"scoring_window": 0}},
    ],
)
def test_operational_input_rejects_lossy_values(raw):
    with pytest.raises(ConfigurationError):
        resolve_configuration(raw)


def test_interleaved_factories_and_worker_payloads_keep_invocation_choices():
    async def construct(parallelism, timeout):
        selected = resolve_configuration(
            {"runtime": {"seed": 7}},
            overlay=InvocationOverlay.from_mapping(
                {
                    "runtime": {"parallelism": parallelism},
                    "aux": {"call_timeout_s": timeout},
                }
            ),
        )
        await asyncio.sleep(0)
        runtime = make_runtime_config(
            {},
            workspace_root=Path("."),
            target_call_llm=_target,
            evaluation_call_llm=_evaluation,
            configuration=selected,
        )
        await asyncio.sleep(0)
        received = ResolvedConfiguration.from_json(
            json.loads(json.dumps(_configuration_spec(runtime)))
        )
        return runtime.parallelism, received.values.aux.call_timeout_s, received.sources

    async def interleave():
        return await asyncio.gather(construct(2, 0.25), construct(7, 420.0))

    first, second = asyncio.run(interleave())
    assert first[:2] == (2, 0.25)
    assert second[:2] == (7, 420.0)
    assert first[2]["aux.call_timeout_s"] == second[2]["aux.call_timeout_s"] == "invocation"
    assert resolve_configuration({}).values.runtime.parallelism == 4


def test_worker_transport_refuses_missing_or_invalid_sources():
    payload = resolve_configuration({}).to_json()
    payload["sources"].pop("runtime.parallelism")
    with pytest.raises(ConfigurationError, match="sources must cover"):
        ResolvedConfiguration.from_json(payload)

    payload = resolve_configuration({}).to_json()
    payload["sources"]["runtime.parallelism"] = "ambient"
    with pytest.raises(ConfigurationError, match="unknown configuration source"):
        ResolvedConfiguration.from_json(payload)


def test_invocation_overlay_cannot_change_execution_identity_or_run_paths():
    for name in ("target_call_llm", "evaluation_call_llm", "proposer_agent", "workspace_root"):
        with pytest.raises(ConfigurationError, match=f"config.runtime.{name}: unknown field"):
            InvocationOverlay.from_mapping({"runtime": {name: "override"}})
