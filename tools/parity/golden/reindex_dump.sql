BEGIN TRANSACTION;
CREATE TABLE epochs (
      epoch_id TEXT PRIMARY KEY,
      contract_hash TEXT,
      created_at TEXT,
      closed INTEGER,
      goal TEXT,
      parent_epoch_id TEXT
    );
INSERT INTO "epochs" VALUES('<DATE>_t1_racing','5095f02ed5e321ef6c5e99480b1686b199e319cb8fca992a1c4d76e8e9845646','<TS>',0,'',NULL);
CREATE TABLE experiments (
      epoch_id TEXT,
      generation_id TEXT,
      hypothesis_core_idea TEXT,
      hypothesis_why TEXT,
      hypothesis_json TEXT,
      tournament_decision TEXT,
      rejection_reason TEXT,
      scalar_score_delta REAL,
      drift_loss_delta REAL,
      pass_rate_delta REAL,
      outcome_json TEXT,
      PRIMARY KEY (epoch_id, generation_id)
    );
INSERT INTO "experiments" VALUES('<DATE>_t1_racing','v1','Tighten the researcher''s instruction so it produces a compact bullet-point synthesis instead of long prose.','The current researcher prompt encourages a verbose, step-by-step synthesis; compact bullets give the writer a cleaner input and should reduce off-topic drift.','{"core_idea": "Tighten the researcher''s instruction so it produces a compact bullet-point synthesis instead of long prose.", "expected_drift_movements": [{"direction": "decrease", "kind": "context_pressure", "magnitude": "medium"}, {"direction": "neutral", "kind": "stopped_early", "magnitude": "small"}], "expected_metric_movements": [], "expected_pass_rate_delta": "+0.05 to +0.10", "modulating": ["researcher_instruction"], "risks": "Compact bullets may drop nuance the writer relied on; the writer''s slide quality may regress if so.", "why": "The current researcher prompt encourages a verbose, step-by-step synthesis; compact bullets give the writer a cleaner input and should reduce off-topic drift."}','promoted','',-1.6,0.0,0.0,'{"champion_eval_mode": "full", "drift_loss_delta": 0.0, "drift_movements": [], "eliminated_in_round": null, "evidence": null, "final_rank": 2, "generalization_gap": 0.0, "holdout": {"confirmed": true, "holdout_scalar": 0.4, "ladder_budget_remaining": 15, "ladder_budget_total": 16, "ladder_released": true, "threshold": 0.01, "train_scalar": 0.4}, "holdout_loss": 0.4, "match_record": [{"delta_scalar": -1.6, "match_id": "rung0_m0", "opponent": "v0", "won": true}, {"delta_scalar": -1.6, "match_id": "rung1_m0", "opponent": "v0", "won": true}, {"delta_scalar": -1.6, "match_id": "racing-final", "opponent": "v0", "won": true}], "metric_movements": [], "operator_override": false, "operator_override_reason": "", "pass_rate_delta": 0.0, "ran_at": "<TS>", "rejection_reason": "", "scalar_score_delta": -1.6, "structure": "racing", "tournament_decision": "promoted", "train_loss": 0.4}');
INSERT INTO "experiments" VALUES('<DATE>_t1_racing','v2','Sharpen the coordinator''s routing instruction so it stops re-dispatching the reviewer in a loop on files_not_found cases.','The current coordinator prompt is long and conflates two failure modes; a sharper routing flow reduces agent_transfer churn on the picky-stakeholder entry.','{"core_idea": "Sharpen the coordinator''s routing instruction so it stops re-dispatching the reviewer in a loop on files_not_found cases.", "expected_drift_movements": [{"direction": "decrease", "kind": "agent_transfer", "magnitude": "medium"}, {"direction": "decrease_or_neutral", "kind": "looping_reasoning", "magnitude": "small"}], "expected_metric_movements": [], "expected_pass_rate_delta": "+0.02 to +0.08", "modulating": ["coordinator_instruction"], "risks": "An overly terse routing flow may skip the debugger when it was actually needed; watch the multi-turn entries.", "why": "The current coordinator prompt is long and conflates two failure modes; a sharper routing flow reduces agent_transfer churn on the picky-stakeholder entry."}','rejected','',-1.2,0.0,0.0,'{"champion_eval_mode": "full", "drift_loss_delta": 0.0, "drift_movements": [], "eliminated_in_round": null, "evidence": null, "final_rank": 3, "generalization_gap": null, "holdout": null, "holdout_loss": null, "match_record": [{"delta_scalar": -1.2, "match_id": "rung0_m1", "opponent": "v0", "won": true}, {"delta_scalar": -1.2, "match_id": "rung1_m1", "opponent": "v0", "won": true}], "metric_movements": [], "operator_override": false, "operator_override_reason": "", "pass_rate_delta": 0.0, "ran_at": "<TS>", "rejection_reason": "", "scalar_score_delta": -1.2, "structure": "racing", "tournament_decision": "rejected", "train_loss": 0.8}');
INSERT INTO "experiments" VALUES('<DATE>_t1_racing','v3','Require the researcher to attach a source citation to every claim so the writer stops inventing unsupported metrics.','Uncited claims drive the writer to fabricate numbers; demanding a citation per bullet tightens factual grounding.','{"core_idea": "Require the researcher to attach a source citation to every claim so the writer stops inventing unsupported metrics.", "expected_drift_movements": [{"direction": "decrease_or_neutral", "kind": "context_pressure", "magnitude": "small"}], "expected_metric_movements": [], "expected_pass_rate_delta": "+0.02 to +0.06", "modulating": ["researcher_instruction"], "risks": "Strict citation demands may slow the researcher down.", "why": "Uncited claims drive the writer to fabricate numbers; demanding a citation per bullet tightens factual grounding."}','rejected','',-0.8,0.0,0.0,'{"champion_eval_mode": "full", "drift_loss_delta": 0.0, "drift_movements": [], "eliminated_in_round": null, "evidence": null, "final_rank": 4, "generalization_gap": null, "holdout": null, "holdout_loss": null, "match_record": [{"delta_scalar": -0.8, "match_id": "rung0_m2", "opponent": "v0", "won": true}], "metric_movements": [], "operator_override": false, "operator_override_reason": "", "pass_rate_delta": 0.0, "ran_at": "<TS>", "rejection_reason": "", "scalar_score_delta": -0.8, "structure": "racing", "tournament_decision": "rejected", "train_loss": 1.2}');
INSERT INTO "experiments" VALUES('<DATE>_t1_racing','v4','Give the coordinator an explicit turn budget so it stops re-routing on revision turns once the budget is spent.','Without a budget the coordinator re-dispatches the reviewer indefinitely; a hard turn cap halts the loop.','{"core_idea": "Give the coordinator an explicit turn budget so it stops re-routing on revision turns once the budget is spent.", "expected_drift_movements": [{"direction": "decrease", "kind": "looping_reasoning", "magnitude": "medium"}], "expected_metric_movements": [], "expected_pass_rate_delta": "+0.01 to +0.05", "modulating": ["coordinator_instruction"], "risks": "Too tight a budget may cut a revision the user wanted.", "why": "Without a budget the coordinator re-dispatches the reviewer indefinitely; a hard turn cap halts the loop."}','rejected','',-3.999999999999999111e-01,0.0,0.0,'{"champion_eval_mode": "full", "drift_loss_delta": 0.0, "drift_movements": [], "eliminated_in_round": null, "evidence": null, "final_rank": 5, "generalization_gap": null, "holdout": null, "holdout_loss": null, "match_record": [{"delta_scalar": -0.3999999999999999, "match_id": "rung0_m3", "opponent": "v0", "won": true}], "metric_movements": [], "operator_override": false, "operator_override_reason": "", "pass_rate_delta": 0.0, "ran_at": "<TS>", "rejection_reason": "", "scalar_score_delta": -0.3999999999999999, "structure": "racing", "tournament_decision": "rejected", "train_loss": 1.6}');
CREATE TABLE generations (
      epoch_id TEXT,
      generation_id TEXT,
      parent_generation_id TEXT,
      promoted INTEGER,
      created_at TEXT,
      round_index INTEGER,
      elo REAL,
      elo_se REAL,
      elo_games INTEGER,
      PRIMARY KEY (epoch_id, generation_id)
    );
INSERT INTO "generations" VALUES('<DATE>_t1_racing','v0',NULL,0,'',NULL,1.279644338898006936e+03,1.292158308397073938e+02,8);
INSERT INTO "generations" VALUES('<DATE>_t1_racing','v1','v0',1,'<TS>',0,1.596539376198991931e+03,1.488617971188911894e+02,4);
INSERT INTO "generations" VALUES('<DATE>_t1_racing','v2','v0',0,'<TS>',0,1.558194730182773582e+03,1.561762412273665178e+02,2);
INSERT INTO "generations" VALUES('<DATE>_t1_racing','v3','v0',0,'<TS>',0,1.532810777360113889e+03,1.626759673974695203e+02,1);
INSERT INTO "generations" VALUES('<DATE>_t1_racing','v4','v0',0,'<TS>',0,1.532810777360113889e+03,1.626759673974695203e+02,1);
CREATE TABLE judge_losses (
      run_id TEXT,
      judge_name TEXT,
      weighted_loss REAL,
      raw_loss REAL,
      weight REAL,
      PRIMARY KEY (run_id, judge_name)
    );
CREATE TABLE judge_scorecards (
      reflection_id TEXT,
      judge_name TEXT,
      tp INTEGER,
      fp INTEGER,
      fn INTEGER,
      tn INTEGER,
      ambiguous INTEGER,
      precision REAL,
      recall REAL,
      f1 REAL,
      severity_accuracy REAL,
      disagreement_rate REAL,
      kappa REAL,
      exercised INTEGER,
      redundant_with_json TEXT,
      PRIMARY KEY (reflection_id, judge_name)
    );
CREATE TABLE loss_profiles (
      run_id TEXT PRIMARY KEY,
      epoch_id TEXT,
      generation_id TEXT,
      entry_id TEXT,
      drift_loss REAL,
      pass_fail INTEGER,
      runtime_ms INTEGER,
      wall_clock_budget_exceeded INTEGER,
      loss_json TEXT,
      tournament_id TEXT,
      match_id TEXT,
      cached INTEGER,
      source_epoch TEXT,
      source_run TEXT,
      abort_cause TEXT
    );
CREATE TABLE metric_counts (
      run_id TEXT,
      namespace TEXT,
      name TEXT,
      severity TEXT,
      count REAL
    );
CREATE TABLE patches (
      patch_id TEXT PRIMARY KEY,
      epoch_id TEXT,
      generation_id TEXT,
      mutation_id TEXT,
      op TEXT,
      rationale TEXT
    );
INSERT INTO "patches" VALUES('<HEX32>','<DATE>_t1_racing','v1','researcher_instruction','replace','Compact bullets reduce context pressure on the writer and tighten the topical signal.');
INSERT INTO "patches" VALUES('<HEX32>','<DATE>_t1_racing','v2','coordinator_instruction','replace','Tightening the routing flow reduces redundant agent_transfer events and breaks reviewer loops.');
INSERT INTO "patches" VALUES('<HEX32>','<DATE>_t1_racing','v3','researcher_instruction','replace','Per-bullet citations ground the writer''s numbers and cut fabricated metrics.');
INSERT INTO "patches" VALUES('<HEX32>','<DATE>_t1_racing','v4','coordinator_instruction','replace','A hard turn budget halts the reviewer re-dispatch loop deterministically.');
CREATE TABLE reflections (
      reflection_id TEXT PRIMARY KEY,
      epoch_id TEXT,
      created_at TEXT,
      mode TEXT,
      executed INTEGER,
      noise_floor_max_abs_delta REAL,
      decision_flip_p REAL,
      n_findings INTEGER,
      n_judges INTEGER,
      verdict_counts_json TEXT
    );
CREATE TABLE runs (
      run_id TEXT PRIMARY KEY,
      epoch_id TEXT,
      generation_id TEXT,
      entry_id TEXT,
      started_at TEXT,
      ended_at TEXT,
      aborted INTEGER,
      runtime_ms INTEGER,
      tournament_id TEXT,
      match_id TEXT
    );
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
INSERT INTO "schema_meta" VALUES('schema_version','12');
INSERT INTO "schema_meta" VALUES('description','zicato analytical index — derived, rebuildable from .zicato/ files');
CREATE TABLE tournaments (
      tournament_id TEXT PRIMARY KEY,
      epoch_id TEXT,
      parent_generation_id TEXT,
      child_generation_id TEXT,
      decision TEXT,
      parent_scalar REAL,
      child_scalar REAL,
      delta_scalar REAL,
      rejection_reason TEXT,
      ran_at TEXT,
      structure TEXT,
      structure_params_json TEXT,
      competitors_json TEXT,
      rounds_json TEXT,
      standings_json TEXT,
      field_status_json TEXT,
      champion_eval_mode TEXT,
      champion_run_ref TEXT
    );
INSERT INTO "tournaments" VALUES('<DATE>_t1_racing:v0->v1','<DATE>_t1_racing','v0','v1','promoted',NULL,NULL,-1.6,'','<TS>','racing','{}','["v0", "v1"]','[{"match_id": "rung0_m0", "opponent": "v0", "won": true, "delta_scalar": -1.6}, {"match_id": "rung1_m0", "opponent": "v0", "won": true, "delta_scalar": -1.6}, {"match_id": "racing-final", "opponent": "v0", "won": true, "delta_scalar": -1.6}]','[]','[]','full','epochs/<DATE>_t1_racing/generations/v0');
INSERT INTO "tournaments" VALUES('<DATE>_t1_racing:v0->v2','<DATE>_t1_racing','v0','v2','rejected',NULL,NULL,-1.2,'','<TS>','racing','{}','["v0", "v2"]','[{"match_id": "rung0_m1", "opponent": "v0", "won": true, "delta_scalar": -1.2}, {"match_id": "rung1_m1", "opponent": "v0", "won": true, "delta_scalar": -1.2}]','[]','[]','full','epochs/<DATE>_t1_racing/generations/v0');
INSERT INTO "tournaments" VALUES('<DATE>_t1_racing:v0->v3','<DATE>_t1_racing','v0','v3','rejected',NULL,NULL,-0.8,'','<TS>','racing','{}','["v0", "v3"]','[{"match_id": "rung0_m2", "opponent": "v0", "won": true, "delta_scalar": -0.8}]','[]','[]','full','epochs/<DATE>_t1_racing/generations/v0');
INSERT INTO "tournaments" VALUES('<DATE>_t1_racing:v0->v4','<DATE>_t1_racing','v0','v4','rejected',NULL,NULL,-3.999999999999999111e-01,'','<TS>','racing','{}','["v0", "v4"]','[{"match_id": "rung0_m3", "opponent": "v0", "won": true, "delta_scalar": -0.3999999999999999}]','[]','[]','full','epochs/<DATE>_t1_racing/generations/v0');
INSERT INTO "tournaments" VALUES('<DATE>_t1_racing:field:v1','<DATE>_t1_racing','','','promoted',NULL,NULL,-1.6,'','<TS>','racing','{"board_fraction": 0.4, "eta": 2, "field_size": 4, "final_rung_budget_seconds": 1800, "matchup_budget_seconds": null, "promote_confidence_threshold": null, "replicates": 2, "rung0_board_size": 0}','[{"generation_id": "v0", "seed": 1, "role": "champion"}, {"generation_id": "v1", "seed": 2, "role": "challenger"}, {"generation_id": "v2", "seed": 3, "role": "challenger"}, {"generation_id": "v3", "seed": 4, "role": "challenger"}, {"generation_id": "v4", "seed": 5, "role": "challenger"}]','[{"stage_index": 0, "label": "Rung 0", "matches": [{"match_id": "rung0", "competitors": ["v1", "v2", "v3", "v4"], "winner": null, "decision": "", "delta_scalar": null, "bracket_slot": "", "bye": false, "survivors": ["v1", "v2"], "cut": ["v3", "v4"], "board_fraction": 0.42857142857142855, "pending": false, "live_progress": {}}]}, {"stage_index": 1, "label": "Rung 1", "matches": [{"match_id": "rung1", "competitors": ["v1", "v2"], "winner": null, "decision": "", "delta_scalar": null, "bracket_slot": "", "bye": false, "survivors": ["v1"], "cut": ["v2"], "board_fraction": 0.8571428571428571, "pending": false, "live_progress": {}}]}, {"stage_index": 3, "label": "Champion gate", "matches": [{"match_id": "racing-final", "competitors": ["v0", "v1"], "winner": "v1", "decision": "promoted", "delta_scalar": -1.6, "bracket_slot": "", "bye": false, "survivors": [], "cut": [], "board_fraction": 1.0, "pending": false, "live_progress": {}}]}]','[{"generation_id": "v0", "rank": 1, "scalar": 0.0, "wins": 0, "losses": 0, "status": "alive", "role": "champion"}, {"generation_id": "v1", "rank": 2, "scalar": 0.4, "wins": 0, "losses": 0, "status": "champion", "role": "challenger"}, {"generation_id": "v2", "rank": 3, "scalar": 0.8, "wins": 0, "losses": 0, "status": "eliminated", "role": "challenger"}, {"generation_id": "v3", "rank": 4, "scalar": 1.2, "wins": 0, "losses": 0, "status": "eliminated", "role": "challenger"}, {"generation_id": "v4", "rank": 5, "scalar": 1.6, "wins": 0, "losses": 0, "status": "eliminated", "role": "challenger"}]','[{"generation_id": "v1", "status": "applied", "reason": "", "attempts": 1, "attempt_reasons": [], "hypothesis": "Tighten the researcher''s instruction so it produces a compact bullet-point synthesis instead of long prose.", "seed": 2}, {"generation_id": "v2", "status": "applied", "reason": "", "attempts": 1, "attempt_reasons": [], "hypothesis": "Sharpen the coordinator''s routing instruction so it stops re-dispatching the reviewer in a loop on files_not_found cases.", "seed": 3}, {"generation_id": "v3", "status": "applied", "reason": "", "attempts": 1, "attempt_reasons": [], "hypothesis": "Require the researcher to attach a source citation to every claim so the writer stops inventing unsupported metrics.", "seed": 4}, {"generation_id": "v4", "status": "applied", "reason": "", "attempts": 1, "attempt_reasons": [], "hypothesis": "Give the coordinator an explicit turn budget so it stops re-routing on revision turns once the budget is spent.", "seed": 5}]',NULL,NULL);
CREATE INDEX idx_runs_gen ON runs(epoch_id, generation_id);
CREATE INDEX idx_loss_gen ON loss_profiles(epoch_id, generation_id);
CREATE INDEX idx_metric_run ON metric_counts(run_id);
CREATE INDEX idx_judge_losses_run ON judge_losses(run_id);
CREATE INDEX idx_runs_tournament ON runs(tournament_id);
CREATE INDEX idx_loss_tournament ON loss_profiles(tournament_id);
CREATE INDEX idx_epochs_parent ON epochs(parent_epoch_id);
CREATE INDEX idx_reflections_epoch ON reflections(epoch_id);
CREATE INDEX idx_judge_scorecards_refl ON judge_scorecards(reflection_id);
COMMIT;
