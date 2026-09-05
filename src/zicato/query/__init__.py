"""The workspace query layer: read-only ``.zicato/`` state assembly.

Library code: these readers turn the on-disk workspace
(runtime state files, the SQLite analytical index, epoch records) into
the JSON view shapes any consumer can render. The dashboard server is
the primary consumer today, but the layer has no dashboard dependency —
:mod:`zicato.query` must never import :mod:`zicato.dashboard` (enforced
by the import-linter contracts).

The readers live in one submodule per view. This package re-exports the
readers production code calls — the dashboard endpoint table, the SSE
snapshot builder, and the ``logs`` CLI command — and nothing else.
``__all__`` is therefore the supported surface: a name absent from it is
reached through the submodule that defines it, such as
``from zicato.query.runtime_view import derive_liveness``.

Every function here is best-effort: a missing or transiently-truncated
file degrades to an empty / ``None`` value rather than raising, so no
endpoint built on top of this ever returns a 500.
"""

from __future__ import annotations

from zicato.query.candidate_view import build_candidate_dossier
from zicato.query.conversations_view import build_matchup_conversations
from zicato.query.epoch_view import (
    build_epoch_analysis,
    build_epoch_view,
    read_epoch_analysis_html,
)
from zicato.query.eval_view import (
    build_eval_dossier,
    build_eval_health,
    build_eval_matrix,
)
from zicato.query.events_index import (
    build_contract_diff,
    build_workspace_view,
)
from zicato.query.execution_plan import build_execution_plan
from zicato.query.file_view import (
    build_file_index,
    build_generation_diff,
    build_generation_patches,
    build_generation_tree,
    read_generation_file,
)
from zicato.query.gate_view import (
    build_drift_movements,
    build_gate_breakdown,
    build_health_report,
    build_score_trajectory,
)
from zicato.query.hypothesis_view import (
    build_calibration_trend,
    build_hypothesis_accuracy,
)
from zicato.query.journal_view import (
    read_epoch_journal,
    read_epoch_journal_md,
)
from zicato.query.judge_roster import build_judge_roster
from zicato.query.judge_view import (
    build_environment,
    build_expectation_outcomes_for_run,
    build_per_entry_for_generation,
    build_per_judge_comparison,
    build_per_judge_for_entry,
    build_per_judge_for_generation,
    build_per_judge_for_run,
    build_per_judge_trend,
    build_run_header,
    build_search_results,
)
from zicato.query.ledger_view import build_experiments_ledger
from zicato.query.lineage_view import build_lineage_view
from zicato.query.live_execution_plan import (
    build_live_execution_plan,
    build_live_pipeline,
)
from zicato.query.log_stream import (
    build_log_view,
    clamp_log_limit,
)
from zicato.query.loop_view import (
    build_optimization_trajectory,
    build_tournament_cost,
)
from zicato.query.mutation_view import build_mutation_detail, build_mutation_index
from zicato.query.paths import (
    WorkspacePaths,
    read_current_epoch,
)
from zicato.query.proposer_view import (
    build_proposer_recommendations,
    build_proposer_scorecard,
)
from zicato.query.racing_view import build_racing_field
from zicato.query.reflection_view import (
    build_adjudication_xray,
    build_judge_scorecards,
    build_practice_review,
    build_reflection_summary,
    list_reflections,
)
from zicato.query.rounds_view import build_round_timeline
from zicato.query.run_log import (
    build_run_log,
    clamp_run_log_limit,
)
from zicato.query.runtime_view import (
    build_snapshot,
    read_active_runs_view,
    read_active_tournament_dict,
    read_effective_settings,
    read_heartbeat_dict,
)
from zicato.query.tournament_view import (
    build_bracket,
    build_matchup_detail,
    build_matchup_grid,
    build_tournament_structure,
)
from zicato.query.trace_view import (
    build_suggestion_provenance,
    build_trace_detail,
    build_trace_list,
)
from zicato.query.transcript_view import (
    build_proposal_episode_export,
    build_run_transcript,
    build_run_transcript_delta,
    empty_run_transcript,
    empty_run_transcript_delta,
    read_proposal_episode_export,
    resolve_conversation,
)

__all__ = [
    "WorkspacePaths",
    "build_adjudication_xray",
    "build_bracket",
    "build_calibration_trend",
    "build_candidate_dossier",
    "build_contract_diff",
    "build_drift_movements",
    "build_environment",
    "build_epoch_analysis",
    "build_epoch_view",
    "build_eval_dossier",
    "build_eval_health",
    "build_eval_matrix",
    "build_execution_plan",
    "build_expectation_outcomes_for_run",
    "build_experiments_ledger",
    "build_file_index",
    "build_gate_breakdown",
    "build_generation_diff",
    "build_generation_patches",
    "build_generation_tree",
    "build_health_report",
    "build_hypothesis_accuracy",
    "build_judge_roster",
    "build_judge_scorecards",
    "build_lineage_view",
    "build_live_execution_plan",
    "build_live_pipeline",
    "build_log_view",
    "build_matchup_conversations",
    "build_matchup_detail",
    "build_matchup_grid",
    "build_mutation_detail",
    "build_mutation_index",
    "build_optimization_trajectory",
    "build_per_entry_for_generation",
    "build_per_judge_comparison",
    "build_per_judge_for_entry",
    "build_per_judge_for_generation",
    "build_per_judge_for_run",
    "build_per_judge_trend",
    "build_practice_review",
    "build_proposal_episode_export",
    "build_proposer_recommendations",
    "build_proposer_scorecard",
    "build_racing_field",
    "build_reflection_summary",
    "build_round_timeline",
    "build_run_header",
    "build_run_log",
    "build_run_transcript",
    "build_run_transcript_delta",
    "build_score_trajectory",
    "build_search_results",
    "build_snapshot",
    "build_suggestion_provenance",
    "build_tournament_cost",
    "build_tournament_structure",
    "build_trace_detail",
    "build_trace_list",
    "build_workspace_view",
    "clamp_log_limit",
    "clamp_run_log_limit",
    "empty_run_transcript",
    "empty_run_transcript_delta",
    "list_reflections",
    "read_active_runs_view",
    "read_active_tournament_dict",
    "read_current_epoch",
    "read_effective_settings",
    "read_epoch_analysis_html",
    "read_epoch_journal",
    "read_epoch_journal_md",
    "read_generation_file",
    "read_heartbeat_dict",
    "read_proposal_episode_export",
    "resolve_conversation",
]
