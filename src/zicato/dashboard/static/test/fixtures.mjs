// test/fixtures.mjs — shared fixtures and helpers for the variant_t_*.test.mjs
// suite: the module handles, the
// FIXTURE map + fetch installers, and every helper/fixture that more than
// one split file references. Each test file calls installDom() BEFORE
// dynamically importing this module, so the js/ module imports below only
// ever evaluate against an installed DOM (same order as the monolith).

import { roundTimelineFromFixtures, racingFieldFromFixtures, racingFieldFromBracket, attachElimStates } from './mock_server.mjs';
export { roundTimelineFromFixtures, racingFieldFromFixtures, racingFieldFromBracket };

export const router = await import('../js/router.js');
export const svg = await import('../js/svg.js');
export const ui = await import('../js/ui.js');
export const shell = await import('../js/shell.js');
export const data = await import('../js/data.js');
export const tree = await import('../js/tree.js');
export const compare = await import('../js/compare.js');
export const livestatus = await import('../js/livestatus.js');
export const coreState = await import('../js/core/state.js');
export const { bus } = await import('../js/core/bus.js');
export const { svgEl } = await import('../js/core/dom.js');
export const rounds = await import('../js/rounds.js');
export const dag = await import('../js/dag.js');
export const hovercard = await import('../js/hovercard.js');
export const live = await import('../js/live.js');
export const STRUCT = await import('../js/views/structure.js');

export const EPOCH_ID = '2026-05-30_e0';

// Stamp a FRESH `ts` (the server's ONE typed ms-epoch liveness timestamp) on
// a heartbeat fixture so the live-status staleness gate (deriveLiveStatus)
// reads it as a live orchestrator pulse. A real heartbeat payload always
// carries the stamp; these UI fixtures elide it, and a heartbeat with no
// ageable timestamp reads STALE (not live). Respects an explicit `ts`
// already on the object (a fixture that is stale on purpose, for instance).
export function freshHb(hb) {
  if (hb && hb.ts == null) {
    return { ...hb, ts: Date.now() };
  }
  return hb;
}


export const FIXTURE = {
  '/api/epoch': {
    epoch_id: EPOCH_ID, closed: false, goal: 'Make the presentation agent crisper.',
    // the REIGNING champion — the server-stamped pointer the views read
    // (never re-scanned from the generation list client-side).
    current_champion: 'v0',
    experiments: [
      // each record carries the CANONICAL server-stamped decision surface
      // (`decision` + tri-state `promoted`) beside the raw outcome.
      { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' }, decision: 'baseline', promoted: false },
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected' }, decision: 'rejected', promoted: false },
      { generation_id: 'v2', parent_generation_id: 'v0', outcome: { decision: 'rejected' }, decision: 'rejected', promoted: false },
    ],
    board: [
      { entry_id: 'waffles_single', kind: 'single_turn', input_preview: 'Make a presentation about waffles.', expectation_kind: 'predicate', budget_s: 180, weight: 1, tags: ['smoke'] },
      { entry_id: 'picky_stakeholder_emulated', kind: 'multi_turn_emulated', input_preview: null, expectation_kind: null, budget_s: 360, weight: 1, tags: ['hard'] },
    ],
  },
  '/api/lineage': { generations: [
    { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v2', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
  ] },
  '/api/tournaments': { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71, hypothesis_core_idea: 'Enforce explicit slide-structure output.' },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51, hypothesis_core_idea: 'Tighten coordinator oversight.' },
  ] },
  '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 70.94 }, { generation_id: 'v1', scalar: 146.65 }, { generation_id: 'v2', scalar: 72.45 }] },
  '/api/workspace': { current_epoch_id: EPOCH_ID, epochs: [{ epoch_id: EPOCH_ID, generation_count: 3, promoted_count: 1, best_scalar: 70.94, closed: false, goal: 'crisper' }], sparkline: [] },
  '/api/health-report': { epoch_id: EPOCH_ID, healthy: true, findings: [] },
  [`/api/mutations/${EPOCH_ID}`]: {
    generations: ['v0', 'v1', 'v2'],
    mutations: [
      { mutation_id: 'coordinator_prompt', kind: 'prompt', file: 'agent/coordinator.py', role: 'coordinator system prompt', line_start: 10, line_end: 40, patched_generation_ids: ['v1'] },
      { mutation_id: 'oversight_policy', kind: 'policy', file: 'agent/policy.py', role: 'oversight policy', line_start: 1, line_end: 12, patched_generation_ids: ['v1', 'v2'] },
    ],
  },
  [`/api/mutations/${EPOCH_ID}/coordinator_prompt`]: {
    mutation_id: 'coordinator_prompt', epoch_id: EPOCH_ID,
    baseline: { generation_id: 'v0', content: 'You are the coordinator.\nDraft an outline.', file: 'agent/coordinator.py', role: 'coordinator system prompt', line_start: 10, line_end: 40 },
    versions: [{ generation_id: 'v1', op: 'edit', rationale: 'Enforce structure.', content: 'You are the coordinator.\nAlways emit an explicit slide structure.' }],
  },
  [`/api/mutations/${EPOCH_ID}/oversight_policy`]: {
    mutation_id: 'oversight_policy', epoch_id: EPOCH_ID,
    baseline: { generation_id: 'v0', content: 'Default oversight.', file: 'agent/policy.py', role: 'oversight policy', line_start: 1, line_end: 12 },
    versions: [],
  },
  [`/api/files/${EPOCH_ID}/v1/patches`]: { patches: [
    { id: 'p1', mutation_id: 'coordinator_prompt', op: 'edit', new_content: 'You are the coordinator.\nAlways emit an explicit slide structure.', rationale: 'Enforce structure.' },
    { id: 'p2', mutation_id: 'oversight_policy', op: 'edit', new_content: 'Tighten coordinator oversight.' },
  ] },
  [`/api/files/${EPOCH_ID}/v2/patches`]: { patches: [
    { id: 'p3', mutation_id: 'oversight_policy', op: 'edit', new_content: 'Loosen coordinator oversight.' },
  ] },
  [`/api/files/${EPOCH_ID}/v0/patches`]: { patches: [] },
};
FIXTURE[`/api/generation/${EPOCH_ID}/v0/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v0', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v0_waffles', drift_loss: 60.5, pass_fail: false, runtime_ms: 180000, wall_clock_budget_exceeded: false },
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v0_picky', drift_loss: 105.5, pass_fail: false, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/generation/${EPOCH_ID}/v1/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v1', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v1_waffles', drift_loss: 60.5, pass_fail: false, runtime_ms: 180000, wall_clock_budget_exceeded: true },
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v1_picky', drift_loss: 642.5, pass_fail: false, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/generation/${EPOCH_ID}/v2/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v2', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v2_waffles', drift_loss: 61.0, pass_fail: false, runtime_ms: 180000, wall_clock_budget_exceeded: false },
] };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`] = { decision: 'rejected', delta_scalar: 75.71, delta_pass_rate: 0,
  deciding_rule: 'scalar_margin', margin: 0.01, regressed_predicate: null, regressed_namespace: null,
  reason: 'challenger regressed: loss rose by 75.71', rules: [
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 146.65 (+75.71; needs ≤ -0.01)' },
    { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'not_reached', fired: false },
    { id: 'namespace_monotonicity', label: 'Namespace monotonicity', status: 'not_reached', fired: false },
  ],
  scalar_components: { champion: { drift: 68.5, schema: 1.43 }, challenger: { drift: 145.64, schema: 0.0 } },
  // scalar-provenance decomposition: the challenger's pass term was
  // reshaped by a pow transform and its drift by a harmonic drift transform;
  // the champion was plain built-in. No fail-open here.
  scalar_decomposition: { present: true, fail_open: false,
    champion: { scalar: { present: true, kind: 'builtin', source: 'built-in formula', transforms: [], fail_open: false, fallback_reason: null },
                drift: { present: true, kind: 'builtin', source: 'built-in formula', transforms: [], fail_open: false, fallback_reason: null } },
    challenger: { scalar: { present: true, kind: 'transform', source: 'pow(2.0)', transforms: [{ kind: 'pass', op: 'pow(2.0)' }], fail_open: false, fallback_reason: null },
                  drift: { present: true, kind: 'transform', source: 'drift transform', transforms: [{ kind: 'looping_reasoning', op: 'harmonic' }], fail_open: false, fallback_reason: null } } },
  primary_driver: { judge: 'incorporates_feedback', delta: 24.0 } };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', delta_scalar: 1.51, reason: 'regressed',
  deciding_rule: 'scalar_margin', margin: 0.01, regressed_predicate: null, regressed_namespace: null, rules: [
  { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 72.45' },
] };
FIXTURE['/api/conversation/run_v1_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Drafting an outline now.', tool_calls: [{ name: 'write_slide' }] },
  ],
  annotations: [{ anchor_seq: 1, kind: 'drift', summary: 'omitted the requested structure' }],
};
FIXTURE['/api/conversation/run_v0_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Here is a structured outline.', tool_calls: [] },
  ],
  annotations: [],
};

// ---- Instrument-lens (board reflection · R5) fixtures ----------------
//
// Shapes copied EXACTLY from src/zicato/query/reflection_view.py's reader
// outputs so the view renders against the real payload contract:
//   * list_reflections        → {reflections:[{reflection_id, epoch_id,
//       created_at, mode, executed, noise_floor_max_abs_delta,
//       decision_flip_p, n_findings, n_judges}]}         (reflection_view.py:174-184)
//   * build_reflection_summary → {reflection_id, epoch_id, created_at, mode,
//       executed, found, noise_floor_max_abs_delta, decision_flip_p,
//       pillars:{reliability, discrimination, validity, calibration},
//       findings:[…], fidelity_tiers:[…]}                (reflection_view.py:255-267)
//     pillars sub-shapes from cli/commands/reflect.py:_build_bill_of_health
//     (:180-231) over reflection/analysis.py: reliability = noise_floor_summary
//     (:154-164) + decision_flip (decision_flip_probability :299-308/:251-261),
//     discrimination = {entry_differentiation(:456), redundancy(:534),
//     coverage(:650)}, validity = {n_judges, aggregate_f1, untested_judges}
//     (:205-209), calibration = {promote_margin, noise_floor_max_abs_delta,
//     margin_clears_floor} (:212-218). findings from reflection/findings.py
//     Finding.to_json (:74-84); each evidence chip carries its adjudicated
//     `verdict` (FP/FN) from findings.py _evidence.
//   * build_judge_scorecards   → {reflection_id, judges:[…]} — the FILE-first
//       CANONICAL shape (scorecards.py JudgeScorecard.to_json), which build_
//       judge_scorecards now prefers over the lossy index projection: judge_name,
//       n_decisions, tp, fp, fn, tn, ambiguous, precision, recall, f1, fpr,
//       severity_accuracy, disagreement_rate, self_consistency_kappa,
//       redundant_with, conflicts_with, exercised, ambiguous_pile. (The index
//       projection is the fallback and DROPS fpr + conflicts_with.)
//   * build_adjudication_xray  → {reflection_id, epoch_id, judge_name, run_ref,
//       found, transcript:{fidelity, turns:[str]}, judge_verdict, adjudication}
//       (reflection_view.py:428-437); judge_verdict = one corpus.py
//       _judge_decisions dict (:264-272); adjudication = adjudicator.py
//       JudgeAdjudication.to_json (:170-187).
export const REFLECTION_ID = 'refl-2026-05-30';
export const REFL_JUDGE = 'format.json';
export const REFL_RUN_REF = 'gen-0042:task.itinerary:r2';
// the encoded x-ray path data.js builds (enc()'d judge + run_ref).
export const REFL_XRAY_PATH =
  `/api/reflection/${encodeURIComponent(REFLECTION_ID)}/xray/${encodeURIComponent(REFL_JUDGE)}/${encodeURIComponent(REFL_RUN_REF)}`;

export const REFLECTION_LIST = { reflections: [
  { reflection_id: REFLECTION_ID, epoch_id: EPOCH_ID, created_at: '2026-05-30T12:00:00Z',
    mode: 'reliability+validity', executed: true, noise_floor_max_abs_delta: 0.018,
    decision_flip_p: 0.31, n_findings: 3, n_judges: 5 },
  { reflection_id: 'refl-2026-05-29', epoch_id: EPOCH_ID, created_at: '2026-05-29T09:00:00Z',
    mode: 'reliability', executed: true, noise_floor_max_abs_delta: 0.02,
    decision_flip_p: null, n_findings: 0, n_judges: 5 },
] };

// ---- proposer panel (query/proposer_view.py reader shapes) ----------
//
// Two epochs so the trend has something to trend, and one of them carries the
// two honesty cases the panel must render distinctly: a NULL rate (nothing
// observed) and a PROVISIONAL one (measured, but too thin to act on).
export const PROPOSER_SCORECARD = {
  found: true, epoch_id: EPOCH_ID, min_sample_n: 5, card: null,
  epochs: [
    { epoch_id: 'e0', proposer_agent_id: 'builtin:default', rounds: 8, proposals: 8,
      promote_rate: { k: 2, n: 8, value: 0.25, provisional: false },
      validation_failure_rate: { k: 3, n: 8, value: 0.375, provisional: false },
      screen_veto_rate: { k: 0, n: 0, value: null, provisional: false },
      margins: { n: 6, unmeasured: 0, achieved_median: 0.021, provisional: false } },
    { epoch_id: EPOCH_ID, proposer_agent_id: 'dir:fancy', rounds: 3, proposals: 3,
      promote_rate: { k: 1, n: 3, value: 0.3333, provisional: true },
      validation_failure_rate: { k: 2, n: 3, value: 0.6667, provisional: true },
      screen_veto_rate: { k: 1, n: 3, value: 0.3333, provisional: true },
      margins: { n: 2, unmeasured: 1, achieved_median: null, provisional: true } },
  ],
};

export const PROPOSER_RECOMMENDATIONS = { found: true, count: 1, pending: [
  { finding_id: 'prec-9f3a12bc', epoch_id: 'e0', reflection_id: 'prefl-20260601T090000Z',
    severity: 'critical', title: 'Post-apply check A4 fails on 38% of proposals',
    detail: 'Three of eight proposal attempts dropped a top-level import.',
    population: '8 proposal attempts across 8 rounds of epoch e0.',
    measured: [{ metric: 'validator_failure_rate.A4', k: 3, n: 8, value: 0.375, provisional: false }],
    compared_against: 'Banded prior epochs (A4): none',
    remedy_safety: 'The edit writes markdown under the proposer dir’s skills/.',
    remedy_kind: 'skill_add', remedy_path: 'skills/preserve-imports.md',
    remedy_sha256: 'a1b2c3d4e5f6' },
] };

export const REFLECTION_SUMMARY = {
  reflection_id: REFLECTION_ID, epoch_id: EPOCH_ID, created_at: '2026-05-30T12:00:00Z',
  mode: 'reliability+validity', executed: true, found: true,
  noise_floor_max_abs_delta: 0.018, decision_flip_p: 0.31,
  pillars: {
    reliability: {
      consumed: true, fresh: false, noise_floor_max_abs_delta: 0.018, noise_floor_runs: 24,
      preflight_verdict: 'ok', per_candidate_scalar_sd: {}, fidelity_tiers: ['verbatim'],
      decision_flip: { p_flip: 0.31, reason: null, base_decision: 'reject', b: 1000,
        parent_id: 'v0', child_id: 'gen-0042', promote_margin: 0.01, fidelity_tiers: ['verbatim'] },
    },
    discrimination: {
      entry_differentiation: { entries: [
        { entry_id: 'task.itinerary', differentiates: true, spread: 0.12, n_candidates: 3 },
        { entry_id: 'task.summary', differentiates: true, spread: 0.08, n_candidates: 3 },
        { entry_id: 'task.flat', differentiates: false, spread: 0.0, n_candidates: 3 },
        { entry_id: 'task.singleton', differentiates: null, spread: 0.0, n_candidates: 1 },
      ], fidelity_tiers: ['verbatim'] },
      redundancy: { clusters: [['task.itinerary'], ['task.summary', 'task.flat']],
        redundant_clusters: [['task.summary', 'task.flat']], threshold: 0.95, fidelity_tiers: ['verbatim'] },
      coverage: { exercised_kinds: ['omission'], watched_kinds: ['omission', 'over_promise'],
        uncovered_kinds: ['over_promise'],
        judges: [{ judge_name: 'safety.scope', exercised: false, untested: true }],
        untested_judges: ['safety.scope'], fidelity_tiers: ['verbatim'] },
    },
    validity: { n_judges: 5, aggregate_f1: 0.81, untested_judges: ['safety.scope'] },
    // `noise_floor_delta_std` is the draw-count-stable A/A dispersion the
    // recommendation actually scales (calibration.py); max_abs_delta is the
    // K-inflated RANGE beside it. Both are written by the summary builder
    // (cli/commands/reflect.py's calibration block), so both are on the wire.
    calibration: { promote_margin: 0.01, noise_floor_max_abs_delta: 0.018,
      noise_floor_delta_std: 0.0062, margin_clears_floor: false },
  },
  findings: [
    { finding_id: 'find-0a1b2c3d', pillar: 'calibration', severity: 'critical',
      title: 'Promote margin is below the noise floor',
      detail: 'promote_margin=0.01 is below the measured noise floor max_abs_delta=0.018 — the gate is promoting on measurement noise. Recommend lifting it to 0.045 (2.5× the floor).',
      evidence: [], recommendation: 'raise promote_margin to 0.045',
      proposed_op: { op: 'set_gate', args: { promote_margin: 0.045 } } },
    { finding_id: 'find-4e5f6a7b', pillar: 'validity', severity: 'critical',
      title: "Judge 'safety.scope' misses real failures",
      detail: "Judge 'safety.scope' stayed silent on 1 transcript(s) the adjudicator found exhibited its failure (recall 0.67).",
      evidence: [{ run_ref: REFL_RUN_REF, judge_name: 'safety.scope', verdict: 'FN',
        span: 'guarantee the weather will be sunny', adjudication_path: 'adjudication/safety.scope/gen-0042:task.itinerary:r2.json' }],
      recommendation: "broaden 'safety.scope' to catch the named missed-fire spans", proposed_op: null },
    { finding_id: 'find-8c9d0e1f', pillar: 'validity', severity: 'warning',
      title: "Judge 'format.json' fires falsely",
      detail: "Judge 'format.json' has precision 0.40 over 3 false fires — it penalizes clean transcripts.",
      evidence: [{ run_ref: REFL_RUN_REF, judge_name: REFL_JUDGE, verdict: 'FP',
        span: 'response is not valid JSON', adjudication_path: 'adjudication/format.json/gen-0042:task.itinerary:r2.json' }],
      recommendation: "down-weight 'format.json' toward 0.5 and tighten it",
      proposed_op: { op: 'set_weights', args: { per_judge_weights: { 'format.json': 0.5 } } } },
  ],
  fidelity_tiers: ['verbatim'],
};

export const REFLECTION_SCORECARDS = { reflection_id: REFLECTION_ID, judges: [
  { judge_name: 'format.json', n_decisions: 41, tp: 14, fp: 3, fn: 6, tn: 15, ambiguous: 3,
    precision: 0.824, recall: 0.7, f1: 0.757, fpr: 0.167, severity_accuracy: 0.79,
    disagreement_rate: 0.06, self_consistency_kappa: 0.84,
    redundant_with: [{ judge: 'tool.args', corr: 0.96 }], conflicts_with: [{ judge: 'safety.scope', corr: -0.71 }],
    exercised: true, ambiguous_pile: false,
    recommendation: "tighten 'format.json' — it fires on 3 well-formed payloads" },
  { judge_name: 'safety.scope', n_decisions: 23, tp: 2, fp: 0, fn: 1, tn: 20, ambiguous: 0,
    precision: 1.0, recall: 0.667, f1: 0.8, fpr: 0.0, severity_accuracy: 1.0,
    disagreement_rate: 0.0, self_consistency_kappa: 0.91,
    redundant_with: [], conflicts_with: [], exercised: true, ambiguous_pile: false },
  { judge_name: 'recall.multi', n_decisions: 0, tp: 0, fp: 0, fn: 0, tn: 0, ambiguous: 0,
    precision: null, recall: null, f1: null, fpr: null, severity_accuracy: null,
    disagreement_rate: 0.0, self_consistency_kappa: null,
    redundant_with: [], conflicts_with: [], exercised: false, ambiguous_pile: false },
] };

// The practice-review narrative layer. Shape copied EXACTLY from
// build_practice_review (reflection_view.py:351-381): {reflection_id, epoch_id,
// found, checks:[…], verdict_counts:{…}}, each check the PracticeCheck.to_json
// shape (practices.py:145-154): {check_id, verdict, headline, evidence:{…},
// rationale, proposed_op:{op,args}|null, unmeasured_reason:str|null}. A mix of
// all four verdicts — a sound affirmation, an unsound with a proposed_op, an
// attend, and an unmeasured naming its missing input.
export const REFLECTION_PRACTICES = {
  reflection_id: REFLECTION_ID, epoch_id: EPOCH_ID, found: true,
  checks: [
    { check_id: 'statistical_power', verdict: 'unsound',
      headline: 'The min detectable Δ (0.052) exceeds the promote margin (0.010) — the loop is theater at this power.',
      evidence: { sigma: 0.018, replicates: 3, board_size: 4, promote_margin: 0.01, min_detectable_delta: 0.052 },
      rationale: 'when the min detectable Δ exceeds the margin the loop is theater at this power (ch.04 §3, §13).',
      proposed_op: { op: 'set_param', args: { replicates: 6 } }, unmeasured_reason: null },
    { check_id: 'promotion_hygiene', verdict: 'sound',
      headline: 'The margin clears the measured noise floor 3.1× with an evidence gate on — promotions are earned.',
      evidence: { promote_margin: 0.045, noise_floor: 0.0145, ratio: 3.1, evidence_gate: true },
      rationale: 'a promotion on a sub-floor margin with no evidence gate promotes noise (ch.04 §3, §6).',
      proposed_op: null, unmeasured_reason: null },
    { check_id: 'oracle_mix', verdict: 'attend',
      headline: '3 of 4 board entries declare only substring/regex oracles — they saturate (the issue-#84 class).',
      evidence: { n_entries: 4, n_weak_oracle_entries: 3 },
      rationale: 'all-expected_text/regex oracles saturate — the issue-#84 weak-oracle class (ch.04 §3).',
      proposed_op: null, unmeasured_reason: null },
    { check_id: 'loss_monoculture', verdict: 'unmeasured',
      headline: 'Loss term-contribution balance could not be assessed.',
      evidence: {},
      rationale: 'a monoculture loss optimizes one blind spot (ch.04 §1.5).',
      proposed_op: null,
      unmeasured_reason: 'no corpus term-contributions (run `zicato reflect run` for the loss decomposition)' },
  ],
  verdict_counts: { sound: 1, attend: 1, unsound: 1, unmeasured: 1 },
};

export const REFLECTION_XRAY = {
  reflection_id: REFLECTION_ID, epoch_id: EPOCH_ID, judge_name: REFL_JUDGE, run_ref: REFL_RUN_REF,
  found: true,
  transcript: { fidelity: 'verbatim', turns: [
    'Plan me a 2-day trip to a coastal town. Keep it cheap.',
    '{"itinerary": [{"day": 1, "items": ["harbour walk"]}]} — response is not valid JSON per the strict checker.',
  ] },
  judge_verdict: { judge_name: REFL_JUDGE, fired: true, severity: 'warning',
    claim: 'assistant response is not valid JSON.', transcript_span: 'sha256:abcd' },
  adjudication: {
    format_version: 1, judge_name: REFL_JUDGE, run_ref: REFL_RUN_REF,
    observed: 'fired', adjudicated: 'should_be_silent', verdict: 'FP', severity_match: null,
    evidence_span: 'response is not valid JSON', meta_judge_rationale: 'Denied. The payload IS valid, parseable JSON. The judge fired on a clean span — a FALSE FIRE.',
    meta_judge_model: 'independent-adjudicator', adjudicator_self_agreement: 0.91,
    operator_confirmed: null, fidelity: 'verbatim', prompt_version: 2, k_adj: 1, raw_response: null,
  },
};

// An x-ray whose capture was NOT retained — the honest "transcript unavailable"
// degrade (reflection_view.py:418 / _empty_xray :326-336 shape).
export const REFLECTION_XRAY_UNAVAILABLE = {
  reflection_id: REFLECTION_ID, epoch_id: EPOCH_ID, judge_name: REFL_JUDGE, run_ref: REFL_RUN_REF,
  found: true, transcript: { fidelity: 'unavailable', turns: [] },
  judge_verdict: null, adjudication: null,
};

// A fixture map carrying the base epoch + the reflection endpoints, so the tree
// grows its Instrument node and every Instrument route resolves. `opts.flip`
// picks the null-flip summary variant; `opts.xray` overrides the x-ray payload.
export function reflectionFixtureMap(opts) {
  const o = opts || {};
  const summary = o.summary || REFLECTION_SUMMARY;
  const F = { ...FIXTURE,
    '/api/reflections': REFLECTION_LIST,
    [`/api/reflection/${REFLECTION_ID}/summary`]: summary,
    [`/api/reflection/${REFLECTION_ID}/scorecards`]: REFLECTION_SCORECARDS,
    [`/api/reflection/${REFLECTION_ID}/practices`]: o.practices || REFLECTION_PRACTICES,
    [REFL_XRAY_PATH]: o.xray || REFLECTION_XRAY,
    // The proposer panel shares the Instrument landing, so its two reads ride
    // the same fixture map.
    '/api/proposer/scorecard': o.proposerScorecard || PROPOSER_SCORECARD,
    '/api/proposer/recommendations': o.proposerRecommendations || PROPOSER_RECOMMENDATIONS,
  };
  return F;
}

// Resolve a fetch path against a fixture map: try the EXACT path first (so an
// explicit `?epoch=<id>` fixture wins — the genuine multi-epoch scoping case),
// then fall back to the query-LESS base path. The Tier-1 views now request
// `/api/epoch?epoch=<id>` etc.; for a single-epoch fixture (every existing
// test) `<id>` is the current epoch, so the scoped read is byte-identical to
// the base — the fallback serves it from the base fixture, unchanged.
// The per-entry A/B grid the dashboard service derives from the two sides' loss
// files (query/tournament_view.build_matchup_grid). Derived here from the two
// per-entry fixtures by the same rules, so a fixture map does not have to
// hand-write a grid that is already implied by the rows it declares:
//
//   * one row per entry either side ran, sorted by entry id;
//   * `verdict` / `won_by` / `decided_by` resolve on the first channel that
//     SEPARATES the two sides — score (higher better), then the pass predicate,
//     then drift (lower better); a tie falls through to the next channel;
//   * `drift_present` is true when either side recorded a non-zero drift loss.
//
// A per-entry fixture row may declare `score_replicates` / `score_se` to pin the
// replicate spread; absent, the grid reports the single draw the row itself is.
function matchupGridFromFixtures(F, epochId, championId, challengerId) {
  const sideOf = (gen) => {
    const pe = lookupFixture(F, `/api/generation/${epochId}/${gen}/per-entry`);
    const out = new Map();
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) out.set(r.entry_id, r);
    return out;
  };
  const parent = sideOf(championId);
  const child = sideOf(challengerId);
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const ids = [...new Set([...parent.keys(), ...child.keys()])].sort();
  const entry_grid = ids.map((id) => {
    const p = parent.get(id) || null;
    const c = child.get(id) || null;
    const pd = p ? num(p.drift_loss) : null;
    const cd = c ? num(c.drift_loss) : null;
    const ps = p ? num(p.score) : null;
    const cs = c ? num(c.score) : null;
    const pb = p && typeof p.pass_fail === 'boolean' ? p.pass_fail : null;
    const cb = c && typeof c.pass_fail === 'boolean' ? c.pass_fail : null;
    const channels = [
      ['score', ps, cs],
      ['pass', pb == null ? null : (pb ? 1 : 0), cb == null ? null : (cb ? 1 : 0)],
      ['drift', pd == null ? null : -pd, cd == null ? null : -cd],
    ];
    let verdict = 'flat', won_by = null, decided_by = null;
    for (const [name, pr, cr] of channels) {
      if (pr == null || cr == null) continue;
      if (decided_by == null) decided_by = name;   // the channel it was READ on
      if (cr === pr) continue;                     // a tie separates nothing
      decided_by = name;
      if (cr > pr) { verdict = 'improved'; won_by = challengerId; }
      else { verdict = 'regressed'; won_by = championId; }
      break;
    }
    return {
      entry_id: id,
      parent_drift_loss: pd, child_drift_loss: cd,
      parent_pass: pb, child_pass: cb,
      parent_score: ps, child_score: cs,
      parent_metrics: (p && p.metrics) || null, child_metrics: (c && c.metrics) || null,
      delta: pd != null && cd != null ? cd - pd : null,
      delta_score: ps != null && cs != null ? cs - ps : null,
      score_replicates: c && typeof c.score_replicates === 'number' ? c.score_replicates : (cs != null ? 1 : 0),
      score_se: c ? num(c.score_se) : null,
      verdict, won_by, decided_by,
    };
  });
  return {
    epoch_id: epochId, champion: championId, challenger: challengerId,
    entry_grid, scalar: null, source: 'loss_files',
    drift_present: entry_grid.some((r) => (r.parent_drift_loss != null && r.parent_drift_loss !== 0)
      || (r.child_drift_loss != null && r.child_drift_loss !== 0)),
  };
}

export function lookupFixture(F, path) {
  if (Object.prototype.hasOwnProperty.call(F, path)) return F[path];
  // The two SERVED joins (round timeline + racing field) are derived from the
  // granular fixtures by the MOCK SERVER (mirroring the Python readers), so a
  // fixture map does not have to hand-write them — exactly like the real
  // dashboard service derives them from the same underlying records. An
  // EXPLICIT fixture (above) always wins.
  let m = /^\/api\/epoch\/([^/?]+)\/round-timeline$/.exec(path);
  if (m) return roundTimelineFromFixtures(F, decodeURIComponent(m[1]));
  m = /^\/api\/epoch\/([^/?]+)\/racing-field$/.exec(path);
  if (m) return racingFieldFromFixtures(F, decodeURIComponent(m[1]));
  m = /^\/api\/matchup-grid\/([^/?]+)\/([^/?]+)\/([^/?]+)$/.exec(path);
  if (m) {
    return matchupGridFromFixtures(F, decodeURIComponent(m[1]), decodeURIComponent(m[2]), decodeURIComponent(m[3]));
  }
  const q = path.indexOf('?');
  if (q >= 0) {
    const base = path.slice(0, q);
    if (Object.prototype.hasOwnProperty.call(F, base)) return F[base];
  }
  return undefined;
}
export function installFetch() {
  globalThis.fetch = async (path) => {
    const v = lookupFixture(FIXTURE, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}
export function freshState() {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
}
export function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
// read the scoped stylesheet text (for CSS-contract assertions).
export async function readCssAsync() {
  const fs = await import('node:fs');
  return fs.readFileSync(new URL('../css/console.css', import.meta.url), 'utf8');
}
export const _cssCache = await readCssAsync();
export function readCss() { return _cssCache; }

// Drive the hovercard like a browser would: fire `mouseenter` on a wired node,
// read the live card text, then fire `mouseleave` to hide it. Returns the text
// the styled card surfaced (so a test can assert the SAME explanation the old
// native <title> carried lives in the hovercard rather than in a <title>).
export function hovercardTextOf(node) {
  hovercard.hide();
  node.dispatchEvent({ type: 'mouseenter', target: node });
  const text = hovercard.cardText();
  node.dispatchEvent({ type: 'mouseleave', target: node });
  return text;
}

// assert a node carries NO native <title> child (the off-brand tooltip is gone).
export function hasNativeTitle(node) {
  return node.childNodes.filter((n) => n.localName === 'title').length > 0;
}

// helpers to read the painted SVG of a view -------------------------
export function svgsByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) =>
    n.localName === 'svg' && (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

// does any ancestor (within host) carry an inline horizontal-scroll style?
export function hasScrollWrapperAncestor(node, host) {
  let n = node && node.parentNode;
  while (n && n !== host) {
    const style = (n.getAttribute && n.getAttribute('style')) || '';
    const cls = (n.getAttribute && n.getAttribute('class')) || '';
    // a contained table-scroll wrapper is allowed; a panel/figure scroll is not.
    if (/overflow-x\s*:\s*auto|overflow-x\s*:\s*scroll/.test(style) && !cls.includes('dn-table-scroll')) return true;
    n = n.parentNode;
  }
  return false;
}

// shared helper: mount the real shell against a live `location` (the same
// harness plumbing the back-button test uses), returning the root.
export function mountLiveShell(initialHash) {
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.window = globalThis.window || {};
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  const loc = { _hash: '' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
    configurable: true,
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => { for (const fn of (listeners.hashchange || [])) fn(); };

  const root = document.createElement('div');
  document.body.appendChild(root);
  loc._hash = initialHash || '#/';
  shell.mountShell(root);
  return root;
}

// mock single-elim structure payload (§3.2 shape)
export const SE_STRUCT = {
  epoch_id: EPOCH_ID, tournament_id: 'tourn_e0_se', structure: 'single_elim',
  structure_params: { seed_order: 'scalar' },
  competitors: [
    { generation_id: 'v0', seed: 1, role: 'champion' },
    { generation_id: 'v1', seed: 2, role: 'challenger' },
    { generation_id: 'v2', seed: 3, role: 'challenger' },
    { generation_id: 'v3', seed: 4, role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Semifinal', matches: [
      { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], winner: 'v0', decision: 'rejected', delta_scalar: 0.05, bracket_slot: 'WB-R0-0', bye: false },
      { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], winner: 'v1', decision: 'promoted', delta_scalar: -0.12, bracket_slot: 'WB-R0-1', bye: false },
    ] },
    { round_index: 1, label: 'Final', matches: [
      { match_id: 'WB-R1-0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted', delta_scalar: -0.08, bracket_slot: 'WB-R1-0', bye: false },
    ] },
  ],
  standings: [
    { generation_id: 'v1', rank: 1, scalar: 0.41, wins: 2, losses: 0, status: 'champion', role: 'challenger' },
    { generation_id: 'v0', rank: 2, scalar: 0.49, wins: 1, losses: 1, status: 'eliminated', role: 'champion' },
  ],
  source: 'index',
};

export const SWISS_STRUCT = {
  epoch_id: EPOCH_ID, tournament_id: 'tourn_e0_sw', structure: 'swiss',
  structure_params: { rounds: 2 },
  competitors: [{ generation_id: 'v0', seed: 1, role: 'champion' }, { generation_id: 'v1', seed: 2, role: 'challenger' }],
  rounds: [
    { round_index: 0, label: 'Round 1', matches: [{ match_id: 'r0m0', competitors: ['v0', 'v1'], winner: 'v1', delta_scalar: -0.1 }] },
    { round_index: 1, label: 'Round 2', matches: [{ match_id: 'r1m0', competitors: ['v1', 'v2'], winner: 'v1', delta_scalar: -0.03 }] },
  ],
  standings: [
    { generation_id: 'v1', rank: 1, scalar: 0.4, wins: 2, losses: 0, status: 'champion' },
    { generation_id: 'v0', rank: 2, scalar: 0.5, wins: 0, losses: 1, status: 'alive' },
  ],
  source: 'index',
};

export const RACING_STRUCT = {
  epoch_id: EPOCH_ID, tournament_id: 'tourn_e0_rc', structure: 'racing',
  structure_params: { rungs: [{ fraction: 0.5, keep: 0.5 }, { fraction: 1.0, keep: 0.5 }] },
  competitors: [
    { generation_id: 'v0', seed: 1, role: 'champion' }, { generation_id: 'v1', seed: 2, role: 'challenger' },
    { generation_id: 'v2', seed: 3, role: 'challenger' }, { generation_id: 'v3', seed: 4, role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v0', 'v1'], cut: ['v2', 'v3'], board_fraction: 0.5 }] },
    { round_index: 1, label: 'Rung 2', matches: [{ match_id: 'rung2', competitors: ['v0', 'v1'], survivors: ['v1'], cut: ['v0'], board_fraction: 1.0 }] },
  ],
  standings: [{ generation_id: 'v1', rank: 1, scalar: 0.39, status: 'champion' }],
  source: 'index',
};

export function structFixture(structure, payload, tournamentId) {
  // PLAY THE SERVER: the real /api/tournaments + /api/tournament-structure
  // payloads carry the served ELIM MODEL (attach_elim_states — sorted rounds
  // + bracket_side/loser + gen_states); the fixture attaches it through the
  // mock mirror so the views render exactly what the server serves.
  const served = attachElimStates({ ...payload, structure });
  const gens = payload.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: EPOCH_ID, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: c.role === 'champion' }));
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g', current_champion: 'v0', tournament: { structure, params: payload.structure_params },
      experiments: gens.map((g) => ({ generation_id: g.generation_id, parent_generation_id: g.parent_generation_id, outcome: { decision: g.promoted ? 'baseline' : 'rejected' }, decision: g.promoted ? 'baseline' : 'rejected', promoted: false })), board: [] },
    '/api/lineage': { generations: gens },
    '/api/score-trajectory': { points: gens.map((g, i) => ({ generation_id: g.generation_id, scalar: 70 + i })) },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure, structure_params: payload.structure_params, champion_lineage: ['v0'],
      matchups: [{ champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 1 }],
      tournaments: [{ tournament_id: tournamentId, structure, structure_params: payload.structure_params, competitors: payload.competitors, rounds: served.rounds, ...(served.gen_states ? { gen_states: served.gen_states } : {}), standings: payload.standings }] },
    [`/api/tournament-structure/${EPOCH_ID}/${tournamentId}`]: served,
  };
  return F;
}

export function installFixtureMap(F) {
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };
}

// a live racing /api/active-tournament: first rung decided, second rung still
// racing (no cut/survivors recorded yet — the run is in flight). The eventual
// winner (v1) is NOT yet committed, so it must NOT show as rejected/eliminated.
export const LIVE_RACING = {
  structure: 'racing', phase: 'running',
  structure_params: { rungs: [{ fraction: 0.5 }, { fraction: 1.0 }] },
  competitors: [
    { generation_id: 'v0', seed: 1, role: 'champion' }, { generation_id: 'v1', seed: 2, role: 'challenger' },
    { generation_id: 'v2', seed: 3, role: 'challenger' }, { generation_id: 'v3', seed: 4, role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v0', 'v1'], cut: ['v2', 'v3'], board_fraction: 0.5 }] },
    { round_index: 1, label: 'Rung 2', matches: [{ match_id: 'rung2', competitors: ['v0', 'v1'], survivors: [], cut: [], board_fraction: 1.0 }] },
  ],
  // mid-run the completed-record view would crown v1 already; the LIVE record
  // leaves everyone racing.
  standings: [
    { generation_id: 'v1', rank: 1, scalar: 0.39, status: 'champion' },
    { generation_id: 'v0', rank: 2, scalar: 0.44, status: 'eliminated' },
  ],
};

// the live racing field shape per the NEW contract: v0 champion + v5..v8
// challengers, the active rung-0 PUBLISHED (its field pending), partial
// aggregates landing.
export function liveRacingField(extra) {
  return Object.assign({
    structure: 'racing', phase: 'running',
    structure_params: { field_size: 4, eta: 2, board_fraction: 0.25, board_size: 8 },
    round_index: 0, total_rounds: 2,
    competitors: [
      { generation_id: 'v0', seed: 1, role: 'champion' },
      { generation_id: 'v5', seed: 2, role: 'challenger' },
      { generation_id: 'v6', seed: 3, role: 'challenger' },
      { generation_id: 'v7', seed: 4, role: 'challenger' },
      { generation_id: 'v8', seed: 5, role: 'challenger' },
    ],
    // the backend publishes the active rung + the (pending) champion gate live.
    rounds: [
      { round_index: 0, label: 'Rung 0', matches: [{ match_id: 'rung0', competitors: ['v5', 'v6', 'v7', 'v8'], survivors: [], cut: [], board_fraction: 0.25, pending: true }] },
      { round_index: 1, label: 'Champion gate', matches: [{ match_id: 'racing-final', competitors: ['v0'], board_fraction: 1.0, winner: null, pending: true }] },
    ],
    standings: [],
    champion_lineage: ['v0'],
  }, extra || {});
}

export function liveElimField(extra) {
  return Object.assign({
    structure: 'single_elim', phase: 'running', epoch_id: HERO_EPOCH,
    structure_params: { board_size: 4 },
    round_index: 0,
    competitors: [
      { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
      { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
    ],
    rounds: [
      { round_index: 0, label: 'Semifinal', matches: [
        { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], bracket_slot: 'WB-R0-0' },
        { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], bracket_slot: 'WB-R0-1' },
      ] },
    ],
    standings: [],
    champion_lineage: ['v0'],
  }, extra || {});
}

export const RC_EPOCH = '2026-06-01_e0';

// the four per-challenger racing records, verbatim from the brief's live epoch.
export const RACING_PER_CHALLENGER = [
  { tournament_id: `${RC_EPOCH}:v0->v1`, structure: 'racing', competitors: ['v0', 'v1'], standings: [],
    rounds: [{ match_id: 'rung0_m0', opponent: 'v0', won: false, delta_scalar: 25.0 }] },
  { tournament_id: `${RC_EPOCH}:v0->v2`, structure: 'racing', competitors: ['v0', 'v2'], standings: [],
    rounds: [{ match_id: 'rung0_m1', opponent: 'v0', won: false, delta_scalar: 3.3 }] },
  { tournament_id: `${RC_EPOCH}:v0->v3`, structure: 'racing', competitors: ['v0', 'v3'], standings: [],
    rounds: [
      { match_id: 'rung0_m2', opponent: 'v0', won: true, delta_scalar: -0.16 },
      { match_id: 'rung1_m0', opponent: 'v0', won: false, delta_scalar: 1.0 },
      { match_id: 'racing-final', opponent: 'v0', won: true, delta_scalar: -32.19 },
    ] },
  { tournament_id: `${RC_EPOCH}:v0->v4`, structure: 'racing', competitors: ['v0', 'v4'], standings: [],
    rounds: [
      { match_id: 'rung0_m3', opponent: 'v0', won: false, delta_scalar: 0.002 },
      { match_id: 'rung1_m1', opponent: 'v0', won: false, delta_scalar: 1.25 },
    ] },
];

export const RACING_TOURNAMENTS = {
  epoch_id: RC_EPOCH, structure: 'racing',
  structure_params: { eta: 2, board_fraction: 0.25 },
  champion_lineage: ['v0', 'v3'],
  matchups: RACING_PER_CHALLENGER.map((t) => ({ champion: 'v0', challenger: t.tournament_id.split('->')[1], decision: t.tournament_id.endsWith('v3') ? 'promoted' : 'rejected', delta_scalar: 1 })),
  tournaments: RACING_PER_CHALLENGER,
};

export const HERO_EPOCH = '2026-06-02_e3';

// two epochs on /api/lineage: a COMPLETED e0 (v0..v4) and a NEW e1 (v0..v2).
export const TWO_EP_OLD = '2026-06-01_e0';

export const TWO_EP_NEW = '2026-06-02_e1';

export function twoEpochFixture(viewedEpoch, opts) {
  const o = opts || {};
  // the WHOLE-workspace lineage — both epochs, with COLLIDING ids (both have v0..).
  const lineage = [
    { generation_id: 'v0', epoch_id: TWO_EP_OLD, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: TWO_EP_OLD, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v2', epoch_id: TWO_EP_OLD, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v3', epoch_id: TWO_EP_OLD, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v4', epoch_id: TWO_EP_OLD, parent_generation_id: 'v0', promoted: true },
    { generation_id: 'v0', epoch_id: TWO_EP_NEW, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: TWO_EP_NEW, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v2', epoch_id: TWO_EP_NEW, parent_generation_id: 'v0', promoted: false },
  ];
  // per-entry profiles for BOTH epochs (so a leak would also leak loss columns).
  const perEntry = {};
  for (const g of lineage) {
    perEntry[`/api/generation/${g.epoch_id}/${g.generation_id}/per-entry`] = {
      epoch_id: g.epoch_id, generation_id: g.generation_id,
      entries: [{ entry_id: 'waffles_single', run_id: `r_${g.epoch_id}_${g.generation_id}`, drift_loss: 50, pass_fail: false }],
    };
  }
  const F = {
    '/api/epoch': {
      epoch_id: viewedEpoch, closed: viewedEpoch === TWO_EP_OLD, goal: 'g',
      tournament: { structure: 'racing', params: { eta: 2, board_fraction: 0.25 } },
      // ep.experiments is also epoch-scoped to the VIEWED epoch (the API returns
      // the contract for the current epoch) — used as the fallback path.
      experiments: lineage.filter((g) => g.epoch_id === viewedEpoch).map((g) => ({
        generation_id: g.generation_id, parent_generation_id: g.parent_generation_id,
        outcome: { decision: g.promoted ? 'baseline' : 'rejected' },
      })),
      board: [{ entry_id: 'waffles_single', kind: 'single_turn', budget_s: 180, weight: 1 }],
    },
    '/api/lineage': { generations: lineage },
    '/api/score-trajectory': { points: lineage.filter((g) => g.epoch_id === viewedEpoch).map((g, i) => ({ generation_id: g.generation_id, scalar: 50 + i })) },
    // the COMPLETED tournaments record carries ONLY e0's racing ladder (per the
    // per-challenger shape) — e1 has none yet.
    '/api/tournaments': {
      epoch_id: TWO_EP_OLD, structure: 'racing', structure_params: { eta: 2, board_fraction: 0.25 },
      champion_lineage: ['v0', 'v4'],
      matchups: [],
      tournaments: [
        { tournament_id: `${TWO_EP_OLD}:v0->v1`, structure: 'racing', competitors: ['v0', 'v1'], standings: [], rounds: [{ match_id: 'rung0_m0', opponent: 'v0', won: false, delta_scalar: 3 }] },
        { tournament_id: `${TWO_EP_OLD}:v0->v4`, structure: 'racing', competitors: ['v0', 'v4'], standings: [], rounds: [
          { match_id: 'rung0_m3', opponent: 'v0', won: true, delta_scalar: -1 },
          { match_id: 'racing-final', opponent: 'v0', won: true, delta_scalar: -5 },
        ] },
      ],
    },
  };
  if (o.activeTournament) F['/api/active-tournament'] = o.activeTournament;
  Object.assign(F, perEntry);
  return F;
}
