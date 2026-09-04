BEGIN TRANSACTION;
CREATE TABLE epochs (
      epoch_id TEXT PRIMARY KEY,
      contract_hash TEXT,
      created_at TEXT,
      closed INTEGER,
      goal TEXT,
      parent_epoch_id TEXT
    );
INSERT INTO "epochs" VALUES('<DATE>_t1_racing','1fd112fae39a2f7acf59654a0a19990a6b450b97b0734dc407542a00c2336f4d','<TS>',0,'',NULL);
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
INSERT INTO "experiments" VALUES('<DATE>_t1_racing','v1','Tag the topic_slugify_logic literal for candidate v1.','A mechanical stand-in proposes a minimal, well-formed change so the loop under test has a real candidate to run.','{"core_idea": "Tag the topic_slugify_logic literal for candidate v1.", "expected_drift_movements": [], "expected_metric_movements": [{"direction": "neutral", "magnitude": "small", "metric_name": "drift:off_topic"}], "expected_pass_rate_delta": "+0.0 to +0.1", "modulating": ["topic_slugify_logic"], "risks": "The change is cosmetic; candidate v1 is not expected to move the board.", "why": "A mechanical stand-in proposes a minimal, well-formed change so the loop under test has a real candidate to run."}','promoted','',-1.6,-1.6,0.0,'{"champion_eval_mode": "full", "drift_loss_delta": -1.6, "drift_movements": [], "eliminated_in_round": null, "evidence": null, "final_rank": 2, "generalization_gap": 0.0, "holdout": {"confirmed": true, "holdout_consulted": true, "holdout_scalar": 0.4, "ladder_budget_before_query": 16, "ladder_budget_remaining": 15, "ladder_budget_total": 16, "ladder_query_reserved": true, "ladder_released": true, "threshold": 0.01, "train_scalar": 0.4}, "holdout_loss": 0.4, "match_record": [{"delta_scalar": -1.6, "match_id": "rung0_m0", "opponent": "v0", "won": true}, {"delta_scalar": -1.6, "match_id": "rung1_m0", "opponent": "v0", "won": true}, {"delta_scalar": -1.6, "match_id": "racing-final", "opponent": "v0", "won": true}], "metric_movements": [], "operator_override": false, "operator_override_reason": "", "pass_rate_delta": 0.0, "ran_at": "<TS>", "rejection_reason": "", "scalar_score_delta": -1.6, "structure": "racing", "tournament_decision": "promoted", "train_loss": 0.4}');
INSERT INTO "experiments" VALUES('<DATE>_t1_racing','v2','Tag the topic_slugify_logic literal for candidate v2.','A mechanical stand-in proposes a minimal, well-formed change so the loop under test has a real candidate to run.','{"core_idea": "Tag the topic_slugify_logic literal for candidate v2.", "expected_drift_movements": [], "expected_metric_movements": [{"direction": "neutral", "magnitude": "small", "metric_name": "drift:off_topic"}], "expected_pass_rate_delta": "+0.0 to +0.1", "modulating": ["topic_slugify_logic"], "risks": "The change is cosmetic; candidate v2 is not expected to move the board.", "why": "A mechanical stand-in proposes a minimal, well-formed change so the loop under test has a real candidate to run."}','rejected','',-1.2,0.0,0.0,'{"champion_eval_mode": "full", "drift_loss_delta": 0.0, "drift_movements": [], "eliminated_in_round": null, "evidence": null, "final_rank": 3, "generalization_gap": null, "holdout": null, "holdout_loss": null, "match_record": [{"delta_scalar": -1.2, "match_id": "rung0_m1", "opponent": "v0", "won": true}, {"delta_scalar": -1.2, "match_id": "rung1_m1", "opponent": "v0", "won": true}], "metric_movements": [], "operator_override": false, "operator_override_reason": "", "pass_rate_delta": 0.0, "ran_at": "<TS>", "rejection_reason": "", "scalar_score_delta": -1.2, "structure": "racing", "tournament_decision": "rejected", "train_loss": 0.8}');
INSERT INTO "experiments" VALUES('<DATE>_t1_racing','v3','Tag the topic_slugify_logic literal for candidate v3.','A mechanical stand-in proposes a minimal, well-formed change so the loop under test has a real candidate to run.','{"core_idea": "Tag the topic_slugify_logic literal for candidate v3.", "expected_drift_movements": [], "expected_metric_movements": [{"direction": "neutral", "magnitude": "small", "metric_name": "drift:off_topic"}], "expected_pass_rate_delta": "+0.0 to +0.1", "modulating": ["topic_slugify_logic"], "risks": "The change is cosmetic; candidate v3 is not expected to move the board.", "why": "A mechanical stand-in proposes a minimal, well-formed change so the loop under test has a real candidate to run."}','rejected','',-0.8,0.0,0.0,'{"champion_eval_mode": "full", "drift_loss_delta": 0.0, "drift_movements": [], "eliminated_in_round": null, "evidence": null, "final_rank": 4, "generalization_gap": null, "holdout": null, "holdout_loss": null, "match_record": [{"delta_scalar": -0.8, "match_id": "rung0_m2", "opponent": "v0", "won": true}], "metric_movements": [], "operator_override": false, "operator_override_reason": "", "pass_rate_delta": 0.0, "ran_at": "<TS>", "rejection_reason": "", "scalar_score_delta": -0.8, "structure": "racing", "tournament_decision": "rejected", "train_loss": 1.2}');
INSERT INTO "experiments" VALUES('<DATE>_t1_racing','v4','Tag the topic_slugify_logic literal for candidate v4.','A mechanical stand-in proposes a minimal, well-formed change so the loop under test has a real candidate to run.','{"core_idea": "Tag the topic_slugify_logic literal for candidate v4.", "expected_drift_movements": [], "expected_metric_movements": [{"direction": "neutral", "magnitude": "small", "metric_name": "drift:off_topic"}], "expected_pass_rate_delta": "+0.0 to +0.1", "modulating": ["topic_slugify_logic"], "risks": "The change is cosmetic; candidate v4 is not expected to move the board.", "why": "A mechanical stand-in proposes a minimal, well-formed change so the loop under test has a real candidate to run."}','rejected','',-0.3999999999999999,0.0,0.0,'{"champion_eval_mode": "full", "drift_loss_delta": 0.0, "drift_movements": [], "eliminated_in_round": null, "evidence": null, "final_rank": 5, "generalization_gap": null, "holdout": null, "holdout_loss": null, "match_record": [{"delta_scalar": -0.3999999999999999, "match_id": "rung0_m3", "opponent": "v0", "won": true}], "metric_movements": [], "operator_override": false, "operator_override_reason": "", "pass_rate_delta": 0.0, "ran_at": "<TS>", "rejection_reason": "", "scalar_score_delta": -0.3999999999999999, "structure": "racing", "tournament_decision": "rejected", "train_loss": 1.6}');
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
INSERT INTO "generations" VALUES('<DATE>_t1_racing','v0',NULL,NULL,'',NULL,1289.4320581849881,129.81263942945287,8);
INSERT INTO "generations" VALUES('<DATE>_t1_racing','v1','v0',1,'<TS>',0,1678.3808441189965,134.28228693525546,6);
INSERT INTO "generations" VALUES('<DATE>_t1_racing','v2','v0',0,'<TS>',0,1576.322983622206,135.52771007127808,4);
INSERT INTO "generations" VALUES('<DATE>_t1_racing','v3','v0',0,'<TS>',0,1477.9320570369046,145.08449230482952,2);
INSERT INTO "generations" VALUES('<DATE>_t1_racing','v4','v0',0,'<TS>',0,1477.9320570369046,145.08449230482952,2);
CREATE TABLE ingest_cursors (
      epoch_id TEXT PRIMARY KEY,
      experiments_count INTEGER,
      runs_count INTEGER,
      round_dirs_count INTEGER,
      reflections_count INTEGER,
      lineage_generations_count INTEGER,
      last_ingested_at TEXT
    );
INSERT INTO "ingest_cursors" VALUES('<DATE>_t1_racing',4,0,1,0,4,'<TS>');
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
CREATE TABLE pareto_frontier (
      epoch_id TEXT,
      generation_id TEXT,
      status TEXT,
      round_admitted INTEGER,
      round_retired INTEGER,
      retired_reason TEXT,
      champion_generation_id TEXT,
      scalar REAL,
      axis_values_json TEXT,
      beats_champion_on_json TEXT,
      PRIMARY KEY (epoch_id, generation_id, status, round_retired)
    );
CREATE TABLE patches (
      patch_id TEXT PRIMARY KEY,
      epoch_id TEXT,
      generation_id TEXT,
      mutation_id TEXT,
      op TEXT,
      rationale TEXT
    );
INSERT INTO "patches" VALUES('<HEX32>','<DATE>_t1_racing','v1','topic_slugify_logic','replace','Read back from the proposer''s working copy.');
INSERT INTO "patches" VALUES('<HEX32>','<DATE>_t1_racing','v2','topic_slugify_logic','replace','Read back from the proposer''s working copy.');
INSERT INTO "patches" VALUES('<HEX32>','<DATE>_t1_racing','v3','topic_slugify_logic','replace','Read back from the proposer''s working copy.');
INSERT INTO "patches" VALUES('<HEX32>','<DATE>_t1_racing','v4','topic_slugify_logic','replace','Read back from the proposer''s working copy.');
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
INSERT INTO "schema_meta" VALUES('schema_version','14');
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
INSERT INTO "tournaments" VALUES('<DATE>_t1_racing:v0->v4','<DATE>_t1_racing','v0','v4','rejected',NULL,NULL,-0.3999999999999999,'','<TS>','racing','{}','["v0", "v4"]','[{"match_id": "rung0_m3", "opponent": "v0", "won": true, "delta_scalar": -0.3999999999999999}]','[]','[]','full','epochs/<DATE>_t1_racing/generations/v0');
INSERT INTO "tournaments" VALUES('<DATE>_t1_racing:field:v1','<DATE>_t1_racing','','','promoted',NULL,NULL,-1.6,'','<TS>','racing','{"board_fraction": 0.4, "eta": 2, "field_size": 4, "final_rung_budget_seconds": 1800, "matchup_budget_seconds": null, "promote_confidence_threshold": null, "replicates": 2, "rung0_board_size": 0}','[{"generation_id": "v0", "role": "champion", "seed": 1}, {"generation_id": "v1", "role": "challenger", "seed": 2}, {"generation_id": "v2", "role": "challenger", "seed": 3}, {"generation_id": "v3", "role": "challenger", "seed": 4}, {"generation_id": "v4", "role": "challenger", "seed": 5}]','[{"label": "Rung 0", "matches": [{"board_fraction": 0.4, "bracket_slot": "", "bye": false, "competitors": ["v1", "v2", "v3", "v4"], "cut": ["v3", "v4"], "decision": "", "delta_scalar": null, "live_progress": {}, "match_id": "rung0", "pending": false, "survivors": ["v1", "v2"], "winner": null}], "stage_index": 0}, {"label": "Rung 1", "matches": [{"board_fraction": 0.8, "bracket_slot": "", "bye": false, "competitors": ["v1", "v2"], "cut": ["v2"], "decision": "", "delta_scalar": null, "live_progress": {}, "match_id": "rung1", "pending": false, "survivors": ["v1"], "winner": null}], "stage_index": 1}, {"label": "Champion gate", "matches": [{"board_fraction": 1.0, "bracket_slot": "", "bye": false, "competitors": ["v0", "v1"], "cut": [], "decision": "promoted", "delta_scalar": -1.6, "live_progress": {}, "match_id": "racing-final", "pending": false, "survivors": [], "winner": "v1"}], "stage_index": 3}]','[{"generation_id": "v0", "losses": 0, "rank": 1, "role": "champion", "scalar": 0.0, "status": "alive", "wins": 0}, {"generation_id": "v1", "losses": 0, "rank": 2, "role": "challenger", "scalar": 0.4, "status": "champion", "wins": 0}, {"generation_id": "v2", "losses": 0, "rank": 3, "role": "challenger", "scalar": 0.8, "status": "eliminated", "wins": 0}, {"generation_id": "v3", "losses": 0, "rank": 4, "role": "challenger", "scalar": 1.2, "status": "eliminated", "wins": 0}, {"generation_id": "v4", "losses": 0, "rank": 5, "role": "challenger", "scalar": 1.6, "status": "eliminated", "wins": 0}]','[{"attempt_reasons": [], "attempts": 1, "generation_id": "v1", "hypothesis": "Tag the topic_slugify_logic literal for candidate v1.", "reason": "", "seed": 2, "status": "applied"}, {"attempt_reasons": [], "attempts": 1, "generation_id": "v2", "hypothesis": "Tag the topic_slugify_logic literal for candidate v2.", "reason": "", "seed": 3, "status": "applied"}, {"attempt_reasons": [], "attempts": 1, "generation_id": "v3", "hypothesis": "Tag the topic_slugify_logic literal for candidate v3.", "reason": "", "seed": 4, "status": "applied"}, {"attempt_reasons": [], "attempts": 1, "generation_id": "v4", "hypothesis": "Tag the topic_slugify_logic literal for candidate v4.", "reason": "", "seed": 5, "status": "applied"}]',NULL,NULL);
CREATE INDEX idx_runs_gen ON runs(epoch_id, generation_id);
CREATE INDEX idx_loss_gen ON loss_profiles(epoch_id, generation_id);
CREATE INDEX idx_metric_run ON metric_counts(run_id);
CREATE INDEX idx_judge_losses_run ON judge_losses(run_id);
CREATE INDEX idx_runs_tournament ON runs(tournament_id);
CREATE INDEX idx_loss_tournament ON loss_profiles(tournament_id);
CREATE INDEX idx_epochs_parent ON epochs(parent_epoch_id);
CREATE INDEX idx_reflections_epoch ON reflections(epoch_id);
CREATE INDEX idx_judge_scorecards_refl ON judge_scorecards(reflection_id);
CREATE INDEX idx_pareto_frontier_epoch ON pareto_frontier(epoch_id);
COMMIT;
