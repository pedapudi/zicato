// test/variant_q.test.mjs — Variant Q ("Atlas IV") unit tests.
//
// Q is the roomy convergence-III dashboard (solarized-dark + Sans default) on a
// data-model TREE sidebar. These pin the brief's seven mandatory fixes plus the
// tree headline + pickers:
//   * the tree sidebar renders the Environment → Epoch → {Generations · Boards ·
//     Mutation surface · Publication} hierarchy and navigates multiple gens;
//   * the promote gate is on the CANDIDATE page (fix #1), stacked, no overlap;
//   * the candidate's PATCH node → per-candidate side-by-side diff with REAL
//     strings, not "[object Object]" (fix #2);
//   * ALL match-ups for a candidate (v0 → both v1 and v2) (fix #3);
//   * the Boards view is first-class from the tree (fix #4) with an INLINE
//     side-by-side transcript on entry select — no separate-page nav (fix #5);
//   * the trellis lives in the Boards view, NOT the epoch overview (fix #6);
//   * colour (sol-dark) + typeface (Sans) pickers switch + persist;
//   * digest-gated repaint is a true no-op.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/Q/router.js');
const svg = await import('../js/variants/Q/svg.js');
const ui = await import('../js/variants/Q/ui.js');
const shell = await import('../js/variants/Q/shell.js');
const data = await import('../js/variants/Q/data.js');
const tree = await import('../js/variants/Q/tree.js');

const EPOCH_ID = '2026-05-30_e0';

const ANALYSIS_MD = [
  '<!-- EYEBROW -->',
  'Zicato improvement campaign · epoch analysis report',
  '',
  '# Presentation agent · epoch e0',
  '',
  '<!-- META -->',
  '**Epoch id**: `2026-05-30_e0`  ',
  '**Status**: in progress  ',
  '',
  '## Abstract',
  '',
  'Two challengers were proposed; both regressed and the crown stood.',
  '',
  '## Aggregate generation scores',
  '',
  '| generation | scalar | outcome |',
  '| --- | --- | --- |',
  '| v0 | 70.94 | promoted |',
  '| v1 | 146.65 | rejected |',
  '| v2 | 72.45 | rejected |',
  '',
  '<!-- FIGURE:aggregate-scores -->',
].join('\n');

const FIXTURE = {
  '/api/epoch': {
    epoch_id: EPOCH_ID, closed: false, goal: 'Make the presentation agent crisper.',
    experiments: [
      { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected' } },
      { generation_id: 'v2', parent_generation_id: 'v0', outcome: { decision: 'rejected' } },
    ],
    board: [
      { id: 'waffles_single', kind: 'single_turn', input_preview: 'Make a presentation about waffles.', expectation_kind: 'predicate', budget_s: 180, weight: 1, tags: ['smoke'] },
      { id: 'picky_stakeholder_emulated', kind: 'multi_turn_emulated', input_preview: null, expectation_kind: null, budget_s: 360, weight: 1, tags: ['hard'] },
    ],
  },
  [`/api/epoch/${EPOCH_ID}/analysis`]: { epoch_id: EPOCH_ID, analysis_md: ANALYSIS_MD },
  '/api/lineage': { generations: [
    { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v2', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
  ] },
  '/api/tournaments': { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71 },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51 },
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
    baseline: { generation_id: 'v0', content: 'Loosely oversee the coordinator.', file: 'agent/policy.py', role: 'oversight policy', line_start: 1, line_end: 12 },
    versions: [],
  },
  [`/api/files/${EPOCH_ID}/v1/patches`]: { patches: [
    { id: 'p1', mutation_id: 'coordinator_prompt', op: 'edit', new_content: 'You are the coordinator.\nAlways emit an explicit slide structure.', rationale: 'Enforce structure.' },
    { id: 'p2', mutation_id: 'oversight_policy', op: 'edit', new_content: 'Tighten coordinator oversight.' },
  ] },
  [`/api/files/${EPOCH_ID}/v2/patches`]: { patches: [
    { id: 'p3', mutation_id: 'oversight_policy', op: 'edit', new_content: 'Loosen coordinator oversight.' },
  ] },
};
FIXTURE[`/api/generation/${EPOCH_ID}/v0/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v0', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v0_waffles', drift_loss: 60.5, pass_fail: 0, runtime_ms: 180000, wall_clock_budget_exceeded: false },
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v0_picky', drift_loss: 105.5, pass_fail: 0, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/generation/${EPOCH_ID}/v1/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v1', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v1_waffles', drift_loss: 60.5, pass_fail: 0, runtime_ms: 180000, wall_clock_budget_exceeded: true },
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v1_picky', drift_loss: 642.5, pass_fail: 0, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/generation/${EPOCH_ID}/v2/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v2', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v2_waffles', drift_loss: 61.0, pass_fail: 0, runtime_ms: 180000, wall_clock_budget_exceeded: false },
] };
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v1`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v1', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 60.5, delta: 0, verdict: 'flat', won_by: null },
  { entry_id: 'picky_stakeholder_emulated', parent_drift_loss: 105.5, child_drift_loss: 642.5, delta: 537, verdict: 'regressed', won_by: 'v0' },
] };
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v2`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v2', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 61.0, delta: 0.5, verdict: 'regressed', won_by: 'v0' },
] };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`] = { decision: 'rejected', delta_scalar: 75.71, delta_pass_rate: 0,
  reason: 'challenger regressed: loss rose by 75.71', rules: [
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 146.65 (+75.71; needs ≤ -0.01)' },
    { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'not_reached', fired: false },
    { id: 'namespace_monotonicity', label: 'Namespace monotonicity', status: 'not_reached', fired: false },
  ],
  scalar_components: { champion: { drift: 68.5, schema: 1.43 }, challenger: { drift: 145.64, schema: 0.0 } },
  primary_driver: { judge: 'incorporates_feedback', delta: 24.0 } };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', delta_scalar: 1.51, reason: 'regressed', rules: [
  { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 72.45' },
] };
FIXTURE['/api/conversation/run_v0_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Here is a structured outline.' },
  ],
  annotations: [],
};
FIXTURE['/api/conversation/run_v1_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Drafting an outline now.', tool_calls: [{ name: 'write_slide' }] },
  ],
  annotations: [{ anchor_seq: 1, kind: 'drift', summary: 'omitted the requested structure' }],
};
FIXTURE['/api/conversation/run_v2_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Loosened the oversight and drifted off-brief.' },
  ],
  annotations: [],
};

function installFetch() {
  globalThis.fetch = async (path) => {
    if (Object.prototype.hasOwnProperty.call(FIXTURE, path)) {
      return { ok: true, json: async () => FIXTURE[path] };
    }
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}
function freshState() {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
}
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

// ---- router ---------------------------------------------------------

test('router: environment is the default; epoch + gen carry the epoch id', () => {
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/Q/').view, 'home');
  assertEqual(router.parseRoute('#/bogus').view, 'home');
  assertEqual(router.parseRoute(`#/Q/epoch/${EPOCH_ID}`).params.epochId, EPOCH_ID);
  const g = router.parseRoute(`#/Q/gen/${EPOCH_ID}/v1/waffles_single`);
  assertEqual(g.view, 'gen'); assertEqual(g.params.gen, 'v1'); assertEqual(g.params.entry, 'waffles_single');
  assertEqual(router.parseRoute(`#/Q/board/${EPOCH_ID}/waffles_single`).params.entry, 'waffles_single');
  assertEqual(router.parseRoute(`#/Q/mutations/${EPOCH_ID}/v1/coordinator_prompt`).params.mutId, 'coordinator_prompt');
  assertEqual(router.parseRoute(`#/Q/matchups/${EPOCH_ID}/v0`).params.gen, 'v0');
  // href round-trips the epoch id (multi-epoch navigability).
  assert(router.href('gen', { epochId: EPOCH_ID, gen: 'v2' }).includes(EPOCH_ID), 'gen href carries the epoch id');
});

// ---- HEADLINE: the data-model tree sidebar ------------------------

test('tree: renders the Environment → Epoch → {Generations · Boards · Mutation surface · Publication} hierarchy', () => {
  freshState();
  const model = {
    epochs: [{ epoch_id: EPOCH_ID, closed: false }],
    lineageByEpoch: { [EPOCH_ID]: [
      { id: 'v0', parent: null, promoted: true }, { id: 'v1', parent: 'v0', promoted: false }, { id: 'v2', parent: 'v0', promoted: false },
    ] },
    boardByEpoch: { [EPOCH_ID]: [{ id: 'waffles_single' }, { id: 'picky_stakeholder_emulated' }] },
  };
  // expand the epoch + groups so the children render.
  window.localStorage.setItem('zicato.Q.tree.expanded',
    JSON.stringify(['epoch:' + EPOCH_ID, 'gens:' + EPOCH_ID, 'boards:' + EPOCH_ID]));
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, rerenderTree() {} };
  const nav = tree.buildTree(model, ctx, router.parseRoute(`#/Q/gen/${EPOCH_ID}/v1`));
  const txt = nav.textContent;
  assert(txt.includes('Environment'), 'Environment root');
  assert(txt.includes(EPOCH_ID), 'the epoch node');
  assert(txt.includes('Generations'), 'Generations group');
  assert(txt.includes('Boards'), 'Boards group');
  assert(txt.includes('Mutation surface'), 'Mutation surface leaf');
  assert(txt.includes('Publication'), 'Publication leaf');
  assert(txt.includes('v0') && txt.includes('v1') && txt.includes('v2'), 'all generations listed');
  assert(txt.includes('waffles_single'), 'a board entry listed');
});

test('tree: multi-generation navigation — selecting a gen node navigates to that candidate (any epoch+gen)', () => {
  freshState();
  const model = {
    epochs: [{ epoch_id: EPOCH_ID, closed: false }],
    lineageByEpoch: { [EPOCH_ID]: [{ id: 'v0', parent: null, promoted: true }, { id: 'v2', parent: 'v0', promoted: false }] },
    boardByEpoch: { [EPOCH_ID]: [] },
  };
  window.localStorage.setItem('zicato.Q.tree.expanded', JSON.stringify(['epoch:' + EPOCH_ID, 'gens:' + EPOCH_ID]));
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, rerenderTree() {} };
  const nav = tree.buildTree(model, ctx, router.parseRoute(`#/Q/epoch/${EPOCH_ID}`));
  // find the v2 generation row's label button and click it.
  const labels = nav.querySelectorAll('[class]').filter((n) => n.localName === 'button' && (n.getAttribute('class') || '').includes('dq-tree-label'));
  const v2 = labels.find((b) => b.textContent.includes('v2'));
  assert(v2, 'a v2 generation row exists');
  v2.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'gen' && navTo.p.gen === 'v2' && navTo.p.epochId === EPOCH_ID, 'clicking the gen node navigates to that candidate with its epoch');
});

// ---- FIX #1: promote gate on the CANDIDATE page -------------------

test('candidate view: the promote gate is ON the candidate page, stacked, rules each their own row', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/Q/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const gate = allByClass(host, 'dq-gate')[0];
  assert(gate, 'a promote-gate panel rendered on the candidate page');
  const rules = allByClass(host, 'dq-rule');
  assert(rules.length >= 3, 'each gate rule is its own row (3 short-circuiting rules)');
  assert(allByClass(host, 'dq-sc-table').length >= 1, 'a separate champion-vs-challenger scalar-components block');
  assert(host.textContent.includes('Scalar margin'), 'a rule label present');
});

// ---- FIX #2: patch node → per-candidate side-by-side diff ----------

test('candidate view: the PATCH node is clickable → this candidate’s mutation diff', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/Q/views/candidate.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  // the lifecycle DAG's PATCH node carries a click → mutations focused on v1.
  const patch = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('ezn-clickable'))[0];
  assert(patch, 'the PATCH node is clickable');
  patch.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'mutations' && navTo.p.gen === 'v1' && navTo.p.epochId === EPOCH_ID, 'PATCH click opens this candidate’s diff');
});

test('mutations view (focused on a candidate): side-by-side diff with REAL strings, not "[object Object]"', async () => {
  freshState(); installFetch();
  const mut = await import('../js/variants/Q/views/mutations.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  // focused on v1 — it auto-pins v1's first patched site and fills the diff.
  await mut.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' });
  const sxs = allByClass(host, 'dn-sxs')[0];
  assert(sxs, 'the side-by-side diff pane filled for the focused candidate');
  assert(host.textContent.includes('Always emit an explicit slide structure'), 'challenger new_content (from /patches) on the right');
  assert(host.textContent.includes('Draft an outline'), 'baseline.content (from /mutations/{id}) on the left');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');
  // two head columns, old | new.
  assertEqual(allByClass(host, 'dn-sxs-col-h').length, 2, 'two side-by-side columns');
});

// ---- FIX #3: ALL match-ups for a candidate ------------------------

test('candidate view (v0): shows ALL match-ups it appeared in — v0→v1 AND v0→v2', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/Q/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v0' });
  assert(host.textContent.includes('All match-ups for this candidate'), 'the all-matchups section rendered');
  assert(host.textContent.includes('v1') && host.textContent.includes('v2'), 'both challengers listed');
});

test('data.matchupsForGen: returns every round where the gen is champion OR challenger (v0 → 2)', () => {
  const bracket = FIXTURE['/api/tournaments'];
  assertEqual(data.matchupsForGen(bracket, 'v0').length, 2, 'v0 appears in both rounds (champion of each)');
  assertEqual(data.matchupsForGen(bracket, 'v1').length, 1, 'v1 appears in one round (challenger)');
});

// ---- FIX #4 + #5: boards view first-class + INLINE transcript -----

test('board view (no entry): the TRELLIS is here (first-class boards from the tree)', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/Q/views/board.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await board.render(host, ctx, { epochId: EPOCH_ID });
  assert(allByClass(host, 'dq-trellis')[0], 'the board trellis renders in the Boards view');
  const cards = allByClass(host, 'dq-trellis-cell');
  assert(cards.length >= 2, 'one trellis card per board entry');
});

test('board view (entry selected): cross-candidate detail + INLINE side-by-side transcript (no nav away)', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/Q/views/board.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await board.render(host, ctx, { epochId: EPOCH_ID, entry: 'waffles_single' });
  assert(host.textContent.includes('Board · waffles_single'), 'the per-board heading');
  assert(allByClass(host, 'dn-valdot').length === 1, 'a per-candidate comparative dot-plot');
  assert(host.textContent.includes('v0') && host.textContent.includes('v1') && host.textContent.includes('v2'), 'all candidates listed');
  // the INLINE transcript: TWO transcript columns, on this page (fix #5).
  const cols = allByClass(host, 'dq-xscript-col');
  assert(cols.length === 2, 'two transcript columns side by side, inline');
  // both transcripts rendered their turns WITHOUT navigating away.
  assert(host.textContent.includes('Here is a structured outline'), 'champion v0 transcript inline');
  assert(host.textContent.includes('Loosened the oversight and drifted off-brief'), 'challenger transcript inline (worst-scoring on this board)');
  assert(navTo === null, 'selecting the entry did NOT navigate to a separate run page');
});

// ---- FIX #6: trellis NOT on the epoch overview --------------------

test('epoch overview: HEATMAP is here, the TRELLIS is NOT (de-dup — trellis lives in Boards)', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/Q/views/epoch.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await epoch.render(host, ctx, { epochId: EPOCH_ID });
  assert(allByClass(host, 'dn-heatmap')[0], 'the heatmap is on the epoch overview');
  assertEqual(allByClass(host, 'dq-trellis').length, 0, 'the trellis is NOT on the epoch overview (de-dup)');
  // a heatmap cell routes to the board view by the row (entry) id.
  const cell = allByClass(host, 'dn-hm-cell').filter((n) => !(n.getAttribute('class') || '').includes('dn-hm-empty'))[0];
  assert(cell, 'a valued heatmap cell exists');
  cell.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'board' && navTo.p.entry, 'heatmap cell routes to the per-board view keyed by entry id');
});

// ---- pickers + pills ----------------------------------------------

test('pickers: typeface (Sans default) + colour (solarized-dark default) switch + persist', () => {
  freshState();
  const root = document.createElement('div');
  assertEqual(ui.DEFAULT_COLOR, 'solarized-dark', 'solarized-dark is the default colour theme');
  assertEqual(ui.DEFAULT_TYPE, 'sans', 'Sans is the default typeface');
  const typeIds = ui.TYPE_THEMES.map((t) => t[0]);
  assert(['sans', 'editorial', 'technical', 'display'].every((t) => typeIds.includes(t)), 'all four typefaces offered');
  const colorIds = ui.COLOR_THEMES.map((t) => t[0]);
  assert(['solarized-dark', 'solarized-light', 'monokai'].every((c) => colorIds.includes(c)), 'all three colour themes offered');
  shell.applyTheme('monokai', root);
  assertEqual(root.getAttribute('data-q-theme'), 'monokai', 'colour applied to root');
  assertEqual(ui.readColor(), 'monokai', 'colour persisted');
  shell.applyTypeface('editorial', root);
  assertEqual(root.getAttribute('data-q-type'), 'editorial', 'typeface applied to root');
  assertEqual(ui.readType(), 'editorial', 'typeface persisted');
  assertEqual(ui.normaliseColor('nonsense'), 'solarized-dark', 'unknown colour → sol-dark');
  assertEqual(ui.normaliseType('nonsense'), 'sans', 'unknown typeface → sans');
});

test('verdict pills: promoted / rejected / baseline render the right class', () => {
  assert((ui.verdictPill('promoted').getAttribute('class') || '').includes('dq-promoted'), 'promoted pill');
  assert((ui.verdictPill('rejected').getAttribute('class') || '').includes('dq-rejected'), 'rejected pill');
  assert(ui.verdictPill('baseline').textContent.includes('seed'), 'baseline pill reads seed');
});

// ---- digest-gated repaint (no-op) ---------------------------------

test('home view: digest-gated — identical data does NOT rebuild the DOM', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/Q/views/home.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await home.render(host, ctx, {});
  const digest1 = host.getAttribute('data-q-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'home painted');
  await home.render(host, ctx, {});
  assertEqual(host.getAttribute('data-q-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

await run();
