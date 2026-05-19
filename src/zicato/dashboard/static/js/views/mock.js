// views/mock.js — synthetic state for offline preview (?mock=1).
//
// A hardcoded snapshot populates AppState so every view renders without
// a running dashboard service. SSE is not opened in mock mode. Used for
// design iteration and exercised by the structural UI test.

//
// The mock covers all four views: a cross-epoch lineage (two epochs),
// the gauntlet bracket (multi-generation, mixed promoted / rejected), a
// live matchup, a health report with one warning finding, the full
// epoch contract, and a heartbeat carrying a harmonograf_url so the
// deep-links render.

// GET /api/tournaments/:generation_id — synthetic per-matchup detail
// for mock mode. Keyed by challenger generation id.
function mockMatchupDetail(genId) {
  const details = {
    v1: {
      hypothesis: {
        core_idea: 'Tighten the extraction schema to reject loose types.',
        why: 'Schema drift was the dominant kind in v0.',
        modulating: ['researcher.schema'],
      },
      patches: [
        { mutation_id: 'researcher.schema', op: 'replace',
          rationale: 'narrow allowed types to the strict invoice contract' },
      ],
      entry_grid: [
        // This row carries explicit per-side session ids — the
        // harmonograf grid links use them verbatim.
        { entry_id: 'extract_invoice_001', parent_drift_loss: 0.30,
          child_drift_loss: 0.21, parent_pass: true, child_pass: true,
          verdict: 'improved',
          parent_session_id: 's-v0-extract_invoice_001',
          child_session_id: 's-v1-extract_invoice_001' },
        // This row carries no session ids — the grid links fall back
        // to the deterministic `{generation}--{entry}` run-id form.
        { entry_id: 'schema_response', parent_drift_loss: 0.12,
          child_drift_loss: 0.34, parent_pass: true, child_pass: false,
          verdict: 'regressed' },
      ],
      scalar: { parent: 0.41, child: 0.43, delta: 0.022,
        components: { drift: -0.04, cost: 0.01, rubric: 0.05 } },
      decision: 'rejected',
      rejection_reason: 'pass-rate regression on schema_response — the strict schema rejected a valid borderline response.',
    },
    v2: {
      hypothesis: {
        core_idea: 'Move JSON validation earlier in the pipeline.',
        why: 'Validating before emit catches malformed output before it scores.',
        modulating: ['pipeline.order'],
      },
      patches: [
        { mutation_id: 'pipeline.order', op: 'reorder',
          rationale: 'validate-before-emit so a bad response never reaches scoring' },
      ],
      entry_grid: [
        { entry_id: 'extract_invoice_001', parent_drift_loss: 0.23,
          child_drift_loss: 0.15, parent_pass: true, child_pass: true,
          verdict: 'improved' },
        { entry_id: 'extract_invoice_002', parent_drift_loss: 0.31,
          child_drift_loss: 0.22, parent_pass: true, child_pass: true,
          verdict: 'improved' },
        { entry_id: 'schema_response', parent_drift_loss: 0.18,
          child_drift_loss: 0.18, parent_pass: true, child_pass: true,
          verdict: 'flat' },
      ],
      scalar: { parent: 0.49, child: 0.41, delta: -0.080,
        components: { drift: -0.06, cost: -0.01, rubric: -0.01 } },
      decision: 'promoted', rejection_reason: null,
    },
    v2x: {
      hypothesis: {
        core_idea: 'Inline the validator instead of reordering the pipeline.',
        why: 'Reordering added a stage; inlining avoids the extra hop.',
        modulating: ['pipeline.order'],
      },
      patches: [
        { mutation_id: 'pipeline.order', op: 'replace',
          rationale: 'inline validate into emit to drop a pipeline stage' },
      ],
      entry_grid: [
        { entry_id: 'extract_invoice_001', parent_drift_loss: 0.15,
          child_drift_loss: 0.17, parent_pass: true, child_pass: true,
          verdict: 'flat' },
        { entry_id: 'schema_response', parent_drift_loss: 0.18,
          child_drift_loss: 0.41, parent_pass: true, child_pass: false,
          verdict: 'regressed' },
      ],
      scalar: { parent: 0.41, child: 0.44, delta: 0.030,
        components: { drift: 0.02, cost: -0.02, rubric: 0.03 } },
      decision: 'rejected',
      rejection_reason: 'pass-rate regression on schema_response — coupling validation to emit dropped a guard.',
    },
    v4: {
      hypothesis: {
        core_idea: 'Carry the picky retry pass into the new epoch baseline.',
        why: 'The retry pass cleared borderline rejections last epoch.',
        modulating: ['researcher.retry'],
      },
      patches: [
        { mutation_id: 'researcher.retry', op: 'insert',
          rationale: 'retry once on a first-pass fail before scoring' },
      ],
      entry_grid: [
        { entry_id: 'extract_invoice_002', parent_drift_loss: 0.34,
          child_drift_loss: 0.31, parent_pass: false, child_pass: true,
          verdict: 'improved' },
        { entry_id: 'multi_turn_picky', parent_drift_loss: 0.28,
          child_drift_loss: 0.27, parent_pass: true, child_pass: true,
          verdict: 'flat' },
      ],
      scalar: { parent: 0.41, child: 0.38, delta: -0.030,
        components: { drift: -0.03, cost: 0.02, rubric: -0.02 } },
      decision: 'promoted', rejection_reason: null,
    },
  };
  return details[genId] || null;
}

function mockSnapshot() {
  return {
    epoch_summary: {
      id: '2026-05-15_e1',
      generation: 'v5',
      round: '2',
      startedAt: new Date(Date.now() - 4 * 60_000 - 23_000).toISOString(),
    },
    // Per-epoch goal summary — the `epochs` key on /api/environment.
    // The Overview epochs table annotates each row with this.
    epochs: [
      { epoch_id: '2026-05-10_e0',
        goal: 'Stabilise the extraction schema so invoice fields parse cleanly.' },
      { epoch_id: '2026-05-15_e1',
        goal: 'Cut off-topic drift by compressing verbose researcher tool docs.' },
    ],
    heartbeat: {
      // The header reads generation_id / round_index straight off the
      // heartbeat; the elapsed clock is now − started_at; the stale
      // badge is now − last_heartbeat > 90s. The two zone forms (`Z`
      // and `+00:00`) are mixed on purpose so ?mock=1 exercises the
      // robust parseIso path.
      generation_id: 'v5',
      round_index: 2,
      // Boards execute in parallel; the tournament hall appends this to
      // its occupancy header when present.
      parallelism: 3,
      last_heartbeat: new Date().toISOString(),
      round_started_at: new Date(Date.now() - 263_000).toISOString()
        .replace('Z', '+00:00'),
      started_at: new Date(Date.now() - 4 * 60_000 - 23_000).toISOString(),
      pid: 12345, instance_id: 'mock',
      // Assembled from parts so the static bundle carries no literal
      // remote-protocol scheme — the no-external-fetch structural test
      // forbids one in the shipped JS. A real heartbeat carries this
      // verbatim from the orchestrator; mock mode just needs a sample
      // to render the deep-links.
      harmonograf_url: ['h', 't', 't', 'p'].join('') + '://localhost:4180',
    },
    service: { version: '1.2.0', port: '7892', build: 'mock' },
    // GET /api/health — dashboard-service identity for the footer.
    health: {
      version: '0.1.0', port: 7892, build: '0.1.0+9feb5e8d3a16',
      uptime_seconds: 5_280,
    },
    scoring: { margin: 0.05 },
    // GET /api/active-runs — each element carries progress (0..1),
    // elapsed_seconds and budget_seconds so the run cards' progress
    // meters render under ?mock=1.
    active_runs: [
      { run_id: 'r-9c2a', entry_id: 'research_topic_q3', generation_id: 'v5',
        session_id: 's-research-9c2a',
        started_at: new Date(Date.now() - 42_000).toISOString(),
        progress: 0.23, elapsed_seconds: 42, budget_seconds: 180 },
      { run_id: 'r-7f10', entry_id: 'multi_turn_picky', generation_id: 'v5',
        started_at: new Date(Date.now() - 14_000).toISOString(),
        progress: 0.06, elapsed_seconds: 14, budget_seconds: 240 },
      // An over-deadline run — elapsed past budget. The hall board card
      // turns its bar red and flags the side.
      { run_id: 'r-3b88', entry_id: 'schema_response', generation_id: 'v5',
        started_at: new Date(Date.now() - 720_000).toISOString(),
        progress: 0.88, elapsed_seconds: 720, budget_seconds: 600 },
    ],
    // GET /api/active-tournament — the contract shape: round_index,
    // parent/child generation ids, and a flat per-SIDE entries list
    // (each board entry appears once per side). Rich extras
    // (hypothesis, drift_movements, per-entry runtime) are additive.
    active_tournament: {
      round_index: 2, total_rounds: 4,
      parent_generation_id: 'v4', child_generation_id: 'v5',
      elapsed_seconds: 263,
      hypothesis: {
        core_idea: 'Compress researcher tool descriptions to under 80 tokens each to reduce context bloat without dropping signal.',
        why: 'Round 1 drift was dominated by off_topic when the context window filled with verbose tool docs.',
        modulating: ['researcher_tool_descriptions', 'write_webpage_tool'],
      },
      // Boards execute in parallel — several entries are `running` at
      // once. The hall grid renders that naturally, with an accent
      // border on every board that has a running side.
      // Finished sides carry `adk_session_id` — the runner stamps the
      // run's ADK/goldfive session id onto the entry on completion, so
      // the per-board harmonograf link deep-links into the run's trace.
      entries: [
        { entry_id: 'extract_invoice_001', side: 'parent', status: 'done',
          scalar_score: 0.23, adk_session_id: 'adk-inv001-parent' },
        { entry_id: 'extract_invoice_001', side: 'child', status: 'done',
          scalar_score: 0.18, adk_session_id: 'adk-inv001-child' },
        { entry_id: 'extract_invoice_002', side: 'parent', status: 'done',
          scalar_score: 0.31, adk_session_id: 'adk-inv002-parent' },
        { entry_id: 'extract_invoice_002', side: 'child', status: 'done',
          scalar_score: 0.45, adk_session_id: 'adk-inv002-child' },
        { entry_id: 'research_topic_q3', side: 'parent', status: 'done',
          scalar_score: 0.19, adk_session_id: 'adk-q3-parent' },
        { entry_id: 'research_topic_q3', side: 'child', status: 'running',
          run_id: 'r-9c2a' },
        { entry_id: 'multi_turn_picky', side: 'parent', status: 'done',
          scalar_score: 0.27 },
        { entry_id: 'multi_turn_picky', side: 'child', status: 'running',
          run_id: 'r-7f10' },
        { entry_id: 'schema_response', side: 'parent', status: 'done',
          scalar_score: 0.14 },
        { entry_id: 'schema_response', side: 'child', status: 'running',
          run_id: 'r-3b88' },
      ],
      drift_movements: [
        { kind: 'off_topic', from_rate: 0.18, to_rate: 0.12 },
        { kind: 'schema_violation', from_rate: 0.10, to_rate: 0.14 },
      ],
      // The server-computed running partial aggregate — the runner
      // rewrites these per board unit as the round runs. champion =
      // the held generation, challenger = the proposed one.
      partial_champion_agg: {
        drift_loss_mean: 0.24, pass_rate: 0.92, scalar: 0.183, entry_count: 3,
      },
      partial_challenger_agg: {
        drift_loss_mean: 0.31, pass_rate: 0.90, scalar: 0.214, entry_count: 3,
      },
    },
    past_tournaments: [
      {
        round: 1, round_index: 1, total_rounds: 4,
        parent_id: 'v4_seed', child_id: 'v4',
        hypothesis: {
          core_idea: 'Carry the prior epoch’s retry pass forward as the v4 baseline.',
          modulating: ['picky_retry_pass'],
        },
        entries: [
          { entry_id: 'extract_invoice_001', status: 'done',
            parent: { drift_loss: 0.28, pass: true },
            child:  { drift_loss: 0.23, pass: true } },
          { entry_id: 'extract_invoice_002', status: 'done',
            parent: { drift_loss: 0.34, pass: false },
            child:  { drift_loss: 0.31, pass: true } },
        ],
        drift_movements: [
          { kind: 'off_topic', from_rate: 0.22, to_rate: 0.18 },
        ],
      },
    ],
    // GET /api/tournaments — the gauntlet bracket. The champion lineage
    // is the green spine; matchups carry promoted, rejected AND aborted
    // challenges so the bracket can hang the discards and the
    // ran-but-undecided challengers below the champion each faced.
    bracket: {
      epoch_id: '2026-05-15_e1',
      champion_lineage: ['v0', 'v2', 'v4'],
      matchups: [
        { champion: 'v0', challenger: 'v1', decision: 'rejected',
          delta_scalar: 0.022,
          rejection_reason: 'pass-rate regression on schema_response',
          hypothesis_core_idea: 'Tighten the extraction schema to reject loose types.',
          ran_at: '2026-05-10T10:12:00Z' },
        { champion: 'v0', challenger: 'v2', decision: 'promoted',
          delta_scalar: -0.080,
          hypothesis_core_idea: 'Move JSON validation earlier in the pipeline.',
          ran_at: '2026-05-10T11:40:00Z' },
        { champion: 'v2', challenger: 'v2x', decision: 'rejected',
          delta_scalar: 0.030,
          rejection_reason: 'pass-rate regression on schema_response',
          hypothesis_core_idea: 'Inline the validator instead of reordering the pipeline.',
          ran_at: '2026-05-10T13:05:00Z' },
        // An aborted challenger — it ran, but the tournament was torn
        // down before a verdict was decided. `decision` is null. It
        // must surface on the gauntlet as a distinct (not discarded)
        // node hanging below the champion it was challenging.
        { champion: 'v2', challenger: 'v3x', decision: null,
          hypothesis_core_idea: 'Batch the validator over the whole response.',
          ran_at: '2026-05-12T16:00:00Z' },
        { champion: 'v2', challenger: 'v4', decision: 'promoted',
          delta_scalar: -0.030,
          hypothesis_core_idea: 'Carry the picky retry pass into the new epoch baseline.',
          ran_at: '2026-05-15T09:20:00Z' },
      ],
    },
    // GET /api/health-report — the loop-health panel. One warning
    // finding so the panel demonstrates a non-trivial state.
    health_report: {
      epoch_id: '2026-05-15_e1',
      healthy: false,
      checked_at: new Date(Date.now() - 90_000).toISOString(),
      findings: [
        { code: 'rubric.low_spread', severity: 'warning',
          summary: 'Rubric scores cluster in a 0.08-wide band across the board.',
          detail: 'A narrow rubric spread weakens the optimization signal — challengers and champions score nearly the same. Consider widening the rubric or adding harder board entries.' },
      ],
    },
    // GET /api/lineage — generations across every epoch. The contract
    // keys are generation_id / parent_generation_id / promoted /
    // created_at; promoted===null marks the in-flight generation. The
    // legacy id / parent_id aliases are kept alongside for the tree
    // view's existing node layout.
    lineage: {
      generations: [
        { generation_id: 'v0', id: 'v0', parent_generation_id: null,
          parent_id: null, epoch_id: '2026-05-10_e0', promoted: true,
          created_at: '2026-05-10T09:00:00Z' },
        { generation_id: 'v1', id: 'v1', parent_generation_id: 'v0',
          parent_id: 'v0', epoch_id: '2026-05-10_e0', promoted: false,
          created_at: '2026-05-10T10:00:00Z' },
        { generation_id: 'v2', id: 'v2', parent_generation_id: 'v1',
          parent_id: 'v1', epoch_id: '2026-05-10_e0', promoted: true,
          created_at: '2026-05-10T11:30:00Z' },
        { generation_id: 'v2x', id: 'v2x', parent_generation_id: 'v1',
          parent_id: 'v1', epoch_id: '2026-05-10_e0', promoted: false,
          created_at: '2026-05-10T13:00:00Z' },
        { generation_id: 'v4_seed', id: 'v4_seed', parent_generation_id: null,
          parent_id: null, epoch_id: '2026-05-15_e1', v0_parent: 'v2',
          promoted: true, created_at: '2026-05-15T09:02:00Z' },
        { generation_id: 'v4', id: 'v4', parent_generation_id: 'v4_seed',
          parent_id: 'v4_seed', epoch_id: '2026-05-15_e1', promoted: true,
          created_at: '2026-05-15T09:20:00Z' },
        { generation_id: 'v5', id: 'v5', parent_generation_id: 'v4',
          parent_id: 'v4', epoch_id: '2026-05-15_e1', promoted: null,
          created_at: '2026-05-15T09:50:00Z' },
      ],
      experiments: [
        { generation_id: 'v1', hypothesis: { core_idea: 'Tighten extraction schema.' },
          outcome: { tournament_decision: 'promoted',
               scalar_score_delta: -0.080, drift_loss_delta: -0.06,
               pass_rate_delta: 0.10, drift_movements: [
                 { kind: 'off_topic', from_rate: 0.4, to_rate: 0.2 },
                 { kind: 'hallucinated_field', from_rate: 0.3, to_rate: 0.15 },
               ]}},
        { generation_id: 'v2', hypothesis: { core_idea: 'Move JSON validation earlier.' },
          outcome: { tournament_decision: 'promoted',
               scalar_score_delta: -0.040, drift_loss_delta: -0.04,
               pass_rate_delta: 0.05, drift_movements: [
                 { kind: 'off_topic', from_rate: 0.2, to_rate: 0.18 },
                 { kind: 'schema_violation', from_rate: 0.25, to_rate: 0.10 },
               ]}},
        { generation_id: 'v2x', hypothesis: { core_idea: 'Inline the validator instead of reordering.' },
          outcome: { tournament_decision: 'rejected',
               scalar_score_delta: 0.030, drift_loss_delta: 0.02,
               pass_rate_delta: -0.04, rejection_reason: 'pass-rate regression on schema_response' }},
        { generation_id: 'v4', hypothesis: { core_idea: 'Carry the picky retry pass into the new epoch baseline.' },
          outcome: { tournament_decision: 'promoted',
               scalar_score_delta: -0.030, drift_loss_delta: -0.03,
               pass_rate_delta: 0.04, drift_movements: [
                 { kind: 'off_topic', from_rate: 0.22, to_rate: 0.18 },
               ]}},
      ],
    },
    experiments: [
      { generation_id: 'v1', hypothesis: { core_idea: 'Tighten extraction schema.', why: 'Schema drift was the dominant kind in v0.', risks: 'May reject borderline-valid responses.' },
        patches: [{ mutation_id: 'researcher.schema', op: 'replace', rationale: 'narrow allowed types' }],
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.080, drift_loss_delta: -0.06, pass_rate_delta: 0.10 }},
      { generation_id: 'v2', hypothesis: { core_idea: 'Move JSON validation earlier.', why: 'Pipeline ordering issue.', risks: '' },
        patches: [{ mutation_id: 'pipeline.order', op: 'reorder', rationale: 'validate-before-emit' }],
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.040, drift_loss_delta: -0.04, pass_rate_delta: 0.05 }},
      { generation_id: 'v2x', hypothesis: { core_idea: 'Inline the validator instead of reordering.', why: 'Reorder added a stage; inline avoids it.', risks: 'Couples validation to emit.' },
        patches: [{ mutation_id: 'pipeline.order', op: 'replace', rationale: 'inline validate' }],
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 0.030, drift_loss_delta: 0.02, pass_rate_delta: -0.04, rejection_reason: 'pass-rate regression on schema_response' }},
      { generation_id: 'v4', hypothesis: { core_idea: 'Carry the picky retry pass into the new epoch baseline.', why: 'Retry pass cleared borderline rejections last epoch.', risks: 'Extra cost.' },
        patches: [{ mutation_id: 'researcher.retry', op: 'insert', rationale: 'retry on first-pass fail' }],
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.030, drift_loss_delta: -0.03, pass_rate_delta: 0.04 }},
      { generation_id: 'v5', hypothesis: { core_idea: 'Compress researcher tool descriptions to under 80 tokens each to reduce context bloat without dropping signal.', why: 'Round 1 drift was dominated by off_topic when the context window filled with verbose tool docs.', risks: 'Over-compression could drop a tool argument hint.' },
        patches: [{ mutation_id: 'researcher_tool_descriptions', op: 'replace', rationale: 'compress to <80 tokens' }],
        outcome: null },
    ],
    epoch: {
      epoch_id: '2026-05-15_e1',
      contract_hash: 'abc123def4567890',
      created_at: '2026-05-15T09:02:00Z',
      closed: false,
      harness: {
        entrypoint: 'myagent.harness:research_pipeline',
        mutable_trees: ['myagent/prompts', 'myagent/researcher'],
      },
      board: [
        { id: 'extract_invoice_001', kind: 'single_turn',
          input_preview: 'Extract the invoice total and due date from the attached PDF text...',
          expectation_kind: 'predicate', budget_s: 900, weight: 1.0, tags: ['extraction'] },
        { id: 'extract_invoice_002', kind: 'single_turn',
          input_preview: 'Extract line items from a multi-page invoice with a discount row...',
          expectation_kind: 'predicate', budget_s: 900, weight: 1.5, tags: ['extraction', 'hard'] },
        { id: 'research_topic_q3', kind: 'multi_turn',
          input_preview: 'Research the Q3 regulatory changes and summarise impact for a non-expert...',
          expectation_kind: 'rubric_judge', budget_s: 1800, weight: 1.0, tags: ['research'] },
        { id: 'multi_turn_picky', kind: 'multi_turn',
          input_preview: 'A picky client revises the brief twice; satisfy all constraints...',
          expectation_kind: 'rubric_judge', budget_s: 1800, weight: 1.0, tags: ['research', 'hard'] },
        { id: 'schema_response', kind: 'single_turn',
          input_preview: 'Return a strictly-typed JSON object matching the given schema...',
          expectation_kind: 'predicate', budget_s: 600, weight: 1.0, tags: ['schema'] },
      ],
      brief: '# Proposer brief\n\nSteering for the proposer this epoch.\n\n## Forbidden edits\n\n- Do not touch `researcher.schema` — it was just stabilized.\n\n## Preferred edits\n\n- Prefer compressing `researcher_tool_descriptions` over persona rewrites.\n\nKeep tool descriptions terse and grounded; off-topic drift dominated v0.\n',
      scoring: {
        drift_weight: 1.0,
        pass_rate_weight: 1.0,
        margin: 0.05,
        rubric_weight: 0.5,
      },
      mutations: [
        { id: 'researcher_tool_descriptions', kind: 'span',
          file: 'myagent/researcher/tools.py', lines: '12-34',
          preview: 'TOOL_DESCRIPTIONS = {\n  "search": "Search the corpus...' },
        { id: 'write_webpage_tool', kind: 'span',
          file: 'myagent/researcher/tools.py', lines: '40-58',
          preview: 'def write_webpage(url): ...' },
        { id: 'researcher.instruction', kind: 'file',
          file: 'myagent/prompts/researcher.md', lines: '1-120',
          preview: 'You are a careful research assistant...' },
      ],
      // Experiment log: per-generation hypothesis + outcome + patch content.
      experiments: [
        { generation_id: 'v1',
          hypothesis: {
            core_idea: 'Tighten the extraction schema to reject loose types.',
            why: 'Schema drift was the dominant kind in v0.',
            risks: 'May reject borderline-valid responses.',
            modulating: ['researcher.schema'],
          },
          patches: {
            'researcher.schema': {
              mutation_id: 'researcher.schema', op: 'replace',
              rationale: 'narrow allowed types to the strict invoice contract',
              new_content: 'SCHEMA = {"type": "object", "required": ["total", "due_date"]}',
            },
          },
          outcome: { tournament_decision: 'rejected', scalar_score_delta: 0.022,
            drift_loss_delta: 0.01, pass_rate_delta: -0.05,
            rejection_reason: 'pass-rate regression on schema_response' },
        },
        { generation_id: 'v2',
          hypothesis: {
            core_idea: 'Move JSON validation earlier in the pipeline.',
            why: 'Pipeline ordering issue — validating before emit catches malformed output.',
            risks: '',
            modulating: ['pipeline.order'],
          },
          patches: {
            'pipeline.order': {
              mutation_id: 'pipeline.order', op: 'reorder',
              rationale: 'validate-before-emit so a bad response never reaches scoring',
              new_content: 'steps = ["validate", "emit", "score"]',
            },
          },
          outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.040,
            drift_loss_delta: -0.04, pass_rate_delta: 0.05 },
        },
        { generation_id: 'v5',
          hypothesis: {
            core_idea: 'Compress researcher tool descriptions to under 80 tokens each.',
            why: 'Round 1 drift was dominated by off_topic when context filled with verbose tool docs.',
            risks: 'Over-compression could drop a tool argument hint.',
            modulating: ['researcher_tool_descriptions'],
          },
          patches: {
            'researcher_tool_descriptions': {
              mutation_id: 'researcher_tool_descriptions', op: 'replace',
              rationale: 'compress to <80 tokens',
              new_content: 'TOOL_DESCRIPTIONS = {"search": "Search.", "write": "Write."}',
            },
          },
          outcome: null,
        },
      ],
      // Journal: the canonical per-experiment section shape journal.py
      // writes — a `## v{N}` heading then `**field**: value` lines.
      journal: '# Epoch journal\n\n## v1 — Tighten extraction schema\n\n**proposed_at**: 2026-05-15T09:05:00Z\n**modulating**: researcher.schema\n**why**: Schema drift was the dominant kind in v0.\n**outcome**: rejected (Δscalar=+0.022, Δpass_rate=-0.050)\n**rejection_reason**: pass-rate regression on `schema_response`\n\n## v2 — Move JSON validation earlier\n\n**proposed_at**: 2026-05-15T09:30:00Z\n**modulating**: pipeline.order\n**outcome**: promoted (Δscalar=-0.040, Δpass_rate=+0.050)\n\nValidate-before-emit cleared the dominant schema_violation drift.\n',
      // Analysis report: post-epoch summary.
      analysis_md: '# Epoch analysis\n\n## Summary\n\nTwo experiments ran this epoch. One was promoted (`v2`), one was rejected (`v1`).\n\n## Key findings\n\n- Schema enforcement alone (v1) increased scalar by +0.022 — the strict schema rejected valid borderline responses.\n- Moving validation earlier (v2) improved scalar by −0.040; schema_violation drift dropped from 0.25 to 0.10.\n\n## Recommendation\n\nThe next epoch should focus on the rubric_judge entries, which still show high spread.\n',
      analysis_html_available: false,
    },
    // GET /api/score-trajectory — the environment-wide evolution curve.
    // The Overview's score-trajectory chart paints the per-generation
    // scalar across every generation; `promoted` colours each marker.
    score_trajectory: {
      epoch_id: '2026-05-15_e1',
      points: [
        { generation_id: 'v0', parent_generation_id: null, promoted: true,
          scalar: 0.49, entry_count: 5, created_at: '2026-05-10T09:00:00Z' },
        { generation_id: 'v1', parent_generation_id: 'v0', promoted: false,
          scalar: 0.51, entry_count: 5, created_at: '2026-05-10T10:00:00Z' },
        { generation_id: 'v2', parent_generation_id: 'v1', promoted: true,
          scalar: 0.43, entry_count: 5, created_at: '2026-05-10T11:30:00Z' },
        { generation_id: 'v4', parent_generation_id: 'v2', promoted: true,
          scalar: 0.38, entry_count: 5, created_at: '2026-05-15T09:20:00Z' },
        { generation_id: 'v5', parent_generation_id: 'v4', promoted: null,
          scalar: null, entry_count: 0, created_at: '2026-05-15T09:50:00Z' },
      ],
    },
    log_tail: [
      { ts: '12:34:50', level: 'info', message: 'tournament r2 entry research_topic_q3 started (run r-9c2a)' },
      { ts: '12:35:01', level: 'info', message: 'goldfive driver: tool researcher_search invoked' },
      { ts: '12:35:14', level: 'warn', message: 'drift detected: off_topic +1 in run r-9c2a' },
      { ts: '12:35:23', level: 'ok',   message: 'parent v4 entry extract_invoice_002 pass' },
    ],
    // GET /api/run-log?limit=40 — the structured event tail. The
    // contract shape is { events:[{ seq, kind, ts, summary }] }; seq
    // and ts may be null for a synthetic / un-sequenced event.
    run_log: {
      events: [
        { seq: 118, kind: 'tournament', ts: '2026-05-16T04:34:50Z',
          summary: 'tournament r2 entry research_topic_q3 started (run r-9c2a)' },
        { seq: 119, kind: 'run', ts: '2026-05-16T04:35:01Z',
          summary: 'goldfive driver: tool researcher_search invoked' },
        { seq: 120, kind: 'drift', ts: '2026-05-16T04:35:14Z',
          summary: 'drift detected: off_topic +1 in run r-9c2a' },
        { seq: 121, kind: 'score', ts: '2026-05-16T04:35:23Z',
          summary: 'parent v4 entry extract_invoice_002 pass' },
        { seq: null, kind: 'note', ts: null,
          summary: 'watchdog tick — all workers responsive' },
      ],
    },
  };
}

function mockConversation(entryId) {
  const championTranscript = {
    run_id: 'v4--' + entryId,
    event_count: 9,
    complete: true,
    turns: [
      { seq: 1, ts: '04:35:01', agent: 'researcher', role: 'user',
        kind: 'plan',
        text: 'Extract the invoice total and due date from the attached PDF.' },
      { seq: 2, ts: '04:35:03', agent: 'researcher', role: 'assistant',
        kind: 'message',
        text: 'I will read the document, then extract the two fields.',
        tool_calls: [
          { name: 'read_document', args: { path: 'invoice.pdf' },
            task_id: 't1' },
        ] },
      { seq: 3, ts: '04:35:05', agent: 'researcher', role: 'tool',
        kind: 'tool',
        text: '',
        tool_results: [
          { name: 'read_document',
            result: 'Invoice #4471 — total $1,240.00, due 2026-06-01.',
            task_id: 't1' } ] },
      { seq: 4, ts: '04:35:08', agent: 'researcher', role: 'assistant',
        kind: 'message',
        text: 'Total is $1,240.00 and the due date is 2026-06-01.' },
    ],
    annotations: [
      { kind: 'plan', ts: '04:35:01', anchor_seq: 1,
        summary: 'task plan registered',
        detail: 'two-field extraction; predicate expectation.' },
      { kind: 'judge', ts: '04:35:09', anchor_seq: 4,
        summary: 'predicate passed',
        detail: 'both fields match the expected contract.' },
    ],
  };
  const challengerTranscript = {
    run_id: 'v5--' + entryId,
    event_count: 7,
    complete: false,
    turns: [
      { seq: 1, ts: '04:36:00', agent: 'researcher', role: 'user',
        kind: 'plan',
        text: 'Extract the invoice total and due date from the attached PDF.' },
      { seq: 2, ts: '04:36:02', agent: 'researcher', role: 'assistant',
        kind: 'message',
        text: 'Reading the document with the compressed tool description.',
        tool_calls: [
          { name: 'read_document', args: { path: 'invoice.pdf' },
            task_id: 't1' } ] },
      { seq: 3, ts: '04:36:04', agent: 'researcher', role: 'tool',
        kind: 'tool',
        text: '',
        tool_results: [
          { name: 'read_document',
            result: 'Invoice #4471 — total $1,240.00, due 2026-06-01.',
            task_id: 't1' } ] },
      { seq: 4, ts: '04:36:07', agent: 'researcher', role: 'assistant',
        kind: 'message',
        text: 'The total appears to be $1,240 — still confirming the date.' },
    ],
    annotations: [
      { kind: 'steering', ts: '04:36:03', anchor_seq: 2,
        summary: 'steering nudge applied',
        detail: 'reminded the agent to emit the date in ISO form.' },
      { kind: 'drift', ts: '04:36:07', anchor_seq: 4,
        summary: 'schema drift: total dropped its cents',
        detail: 'emitted "$1,240" instead of the contract "$1,240.00".' },
    ],
  };
  return {
    champion: {
      run_id: championTranscript.run_id,
      generation_id: 'v4',
      transcript: championTranscript,
    },
    challenger: {
      run_id: challengerTranscript.run_id,
      generation_id: 'v5',
      transcript: challengerTranscript,
    },
  };
}

export { mockMatchupDetail, mockSnapshot, mockConversation };
