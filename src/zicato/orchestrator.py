"""Typed dispatch surface for the evolve round pipeline."""

# Transitional imports remain module attributes only until callers move to the
# owning phase modules; they are data aliases, not forwarding functions.
# ruff: noqa: F401,I001

from __future__ import annotations

import time

from zicato.core.types import OutcomeRecord
from zicato.evolve.dashboard_projection import (
    _clear_active_tournament,
    _field_entries,
    _mark_run_terminal,
    _overlay_projected_live_progress,
    _overlay_projected_standings,
    _persist_field_tournament,
    _publish_active_tournament,
    _serialise_rounds,
    _serialise_standings,
    _settle_active_tournament,
)
from zicato.evolve.decision_support import (
    _build_events_paths,
    _count_infra_aborted_runs,
    _defer_round_infra_outage,
    _field_failure_summary,
    _generalization_fields,
    _generalization_fields_from_scalars,
    _load_parent_losses,
    _render_failure_profile,
    _render_loss_summary,
    _render_process_exemplars_block,
    _token_clip_state,
)
from zicato.evolve.epoching import (
    _component_diff_label,
    _create_epoch_from_contract,
    _promoted_head_snapshot,
    _stored_component_hashes,
    _write_component_hashes,
    ensure_epoch_for_contract,
)
from zicato.evolve.gate import (
    _apply_field_overrides,
    _confirm_crowning_on_holdout,
    _confirm_gauntlet_promotion,
    _CrowningHoldout,
    _gauntlet_decision_from_result,
    _integrity_block_reason,
    _resolve_round_champion_mode,
)
from zicato.evolve.gauntlet import evolve_once
from zicato.evolve.ingest import _index_db_path, _load_prior_experiments, index_preflight
from zicato.evolve.lifecycle_services import (
    _build_meta_loop_emitter_safe,
    _EnvVarRestorer,
    _LaunchedHandle,
    _NoopShutdownHandle,
    _now_iso,
    _resolve_harmonograf_url,
    _resolve_or_launch_harmonograf,
)
from zicato.evolve.propose_apply import (
    _diversity_signature,
    _max_overlap_with_accepted,
    _mint_challenger_field,
    _propose_and_apply_challenger,
)
from zicato.evolve.round_api import DEFERRED_INFRA_DECISION, EvolveRoundOutcome
from zicato.evolve.round_context import (
    _build_candidate_screen_runner,
    _recombine_pair_for_slot,
)
from zicato.evolve.round_baseline import (
    _atomic_write_text,
    _dump_mutations_snapshot,
    _ensure_baseline_snapshot,
    _load_historical_aggregate,
    _materialize_carried_champion,
    _source_epoch_generation,
)
from zicato.evolve.round_prepare import (
    _assess_and_persist_loop_health,
    _maybe_calibrate_noise_floor,
    _maybe_contract_preflight,
    _preflight_diagnosis,
    _summarise_loop_health,
    _warn_erroring_judges,
    _warn_loop_no_signal,
    _warn_margin_below_noise_floor,
)
from zicato.evolve.round_reporting import (
    _collect_epoch_health_inputs,
    _emit_gate_evaluated,
    _emit_harness_loaded,
    _promoted_entry_regressions,
    _regenerate_epoch_report,
    _RoundLogEmitter,
)
from zicato.runtime.control_consumer import (
    block_while_paused,
    claim_rubric_replacement,
    claim_skip_round,
)

# Imported after every loop collaborator above so its runtime module lookup sees
# a complete dispatch surface.
from zicato.evolve.loop import evolve_n_rounds

__all__ = [
    "DEFERRED_INFRA_DECISION",
    "EvolveRoundOutcome",
    "ensure_epoch_for_contract",
    "evolve_n_rounds",
    "evolve_once",
]
