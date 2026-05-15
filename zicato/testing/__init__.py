"""zicato.testing — utilities for testing zicato modules.

Three submodules:

* :mod:`zicato.testing.mock_llm` — deterministic ``CallLLM`` doubles
  (canned, recording, scripted).
* :mod:`zicato.testing.replay` — thin import-guarded wrapper over
  goldfive's events-JSONL replay helper plus a dict projection for
  assertion-friendly comparisons.
* :mod:`zicato.testing.fixtures` — factory functions that build valid
  instances of every :mod:`zicato.core.types` dataclass with sensible
  defaults, plus a synthetic goldfive events JSONL writer.

All names are re-exported from the package root so tests can write
``from zicato.testing import CannedCallLLM, make_board_entry`` without
caring about the submodule layout.
"""

from __future__ import annotations

from zicato.testing.fixtures import (
    make_board_entry,
    make_drift_count,
    make_drift_movement_actual,
    make_epoch_config,
    make_expectation,
    make_experiment,
    make_generation,
    make_hypothesis_spec,
    make_loss_profile,
    make_mutation_point,
    make_outcome_record,
    make_patch,
    make_pattern,
    make_run_result,
    make_runtime_config,
    make_scoring_weights,
    make_scripted_turn,
    make_synthetic_events_jsonl,
    make_user_persona,
)
from zicato.testing.mock_llm import (
    CannedCallLLM,
    RecordingCallLLM,
    ScriptedCallLLM,
)
from zicato.testing.replay import events_to_dicts, replay_events

__all__ = [
    # mock_llm
    "CannedCallLLM",
    "RecordingCallLLM",
    "ScriptedCallLLM",
    # replay
    "replay_events",
    "events_to_dicts",
    # fixtures
    "make_board_entry",
    "make_drift_count",
    "make_drift_movement_actual",
    "make_epoch_config",
    "make_expectation",
    "make_experiment",
    "make_generation",
    "make_hypothesis_spec",
    "make_loss_profile",
    "make_mutation_point",
    "make_outcome_record",
    "make_patch",
    "make_pattern",
    "make_run_result",
    "make_runtime_config",
    "make_scoring_weights",
    "make_scripted_turn",
    "make_synthetic_events_jsonl",
    "make_user_persona",
]
