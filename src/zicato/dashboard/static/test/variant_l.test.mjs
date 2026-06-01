// test/variant_l.test.mjs — Variant L ("Atlas III") unit tests.
//
// L is the main-line convergence-II dashboard. These tests pin the brief's
// Round-4 mandates:
//   * the mutation view renders ONE cohesive visual — a site×generation
//     matrix + a SIDE-BY-SIDE diff with REAL string baseline + new content
//     (never "[object Object]");
//   * the NEW per-board cross-candidate view renders, and trellis cards +
//     heatmap cells route to it by ENTRY ID (not an arbitrary candidate);
//   * the promote gate is laid out as STACKED, non-overlapping sections;
//   * the Tufte sankey's per-board label and value are SEPARATE (no overlap);
//   * the heatmap uses ramp colours derived from the active theme's tokens;
//   * the typeface picker AND the colour picker switch + persist;
//   * the digest gate is a no-op on a heartbeat repaint;
//   * a COLD deep-link transcript renders;
//   * the publication reuses K's renderer and GFM tables render.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/L/router.js');
const svg = await import('../js/variants/L/svg.js');
const ui = await import('../js/variants/L/ui.js');
const paper = await import('../js/variants/L/paper.js');
const data = await import('../js/variants/L/data.js');

const EPOCH_ID = '2026-05-30_e0';

const ANALYSIS_MD = [
  '<!-- EYEBROW -->',
  'Zicato improvement campaign · epoch analysis report',
  '',
  '# Presentation agent · epoch e0',
  '',
  '<!-- META -->',
  '**Epoch id**: `2026-05-30_e0`  ',
  '**Status**: in progress',
  '',
  '## Abstract',
  '',
  'Two challengers were proposed against the v0 seed; both regressed.',
  '',
  '## Aggregate generation scores',
  '',
  '| generation | scalar |',
  '| --- | --- |',
  '| v0 | 70.9 |',
  '| v1 | 146.7 |',
  '',
  '<!-- FIGURE:aggregate-generation-scores -->',
  '',
  '## Methods — the mutation surface',
  '',
  '<!-- FIGURE:mutation-surface -->',
].join('\n');

const FIXTURE = {
  '/api/epoch': {
    epoch_id: EPOCH_ID, closed: false, goal: 'Make the presentation agent crisper.',
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
  [`/api/mutations/${EPOCH_ID}`]: {
    generations: ['v0', 'v1', 'v2'],
    mutations: [
      { mutation_id: 'coordinator_prompt', kind: 'prompt', file: 'agent/coordinator.py', role: 'coordinator system prompt', line_start: 10, line_end: 40, patched_generation_ids: ['v1'],
        baseline: { content: 'You are the coordinator.\nKeep it brief.' } },
      { mutation_id: 'oversight_policy', kind: 'policy', file: 'agent/policy.py', role: 'oversight policy', line_start: 1, line_end: 12, patched_generation_ids: ['v1', 'v2'],
        baseline: { content: 'Allow one revision.' } },
    ],
  },
  // per-site detail — the baseline content STRING lives at .baseline.content.
  [`/api/mutations/${EPOCH_ID}/coordinator_prompt`]: {
    mutation_id: 'coordinator_prompt',
    baseline: { content: 'You are the coordinator.\nKeep it brief.' },
  },
  [`/api/files/${EPOCH_ID}/v1/patches`]: { patches: [
    { id: 'p1', mutation_id: 'coordinator_prompt', op: 'edit', new_content: 'You are the coordinator.\nAlways emit an explicit slide structure.', rationale: 'Enforce structure.' },
    { id: 'p2', mutation_id: 'oversight_policy', op: 'edit', new_content: 'Tighten coordinator oversight.' },
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
  { entry_id: 'waffles_single', run_id: 'run_v2_waffles', drift_loss: 61.0, pass_fail: 1, runtime_ms: 180000, wall_clock_budget_exceeded: false },
] };
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v1`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v1', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 60.5, delta: 0, verdict: 'flat', won_by: null },
  { entry_id: 'picky_stakeholder_emulated', parent_drift_loss: 105.5, child_drift_loss: 642.5, delta: 537, verdict: 'regressed', won_by: 'v0' },
] };
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v2`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v2', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 61.0, delta: 0.5, verdict: 'regressed', won_by: 'v0' },
] };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`] = {
  decision: 'rejected', delta_scalar: 75.71, delta_pass_rate: 0, reason: 'challenger regressed: loss rose by 75.71',
  rules: [
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 146.65 (+75.71; needs ≤ -0.01)' },
    { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'not_reached', fired: false },
    { id: 'namespace_monotonicity', label: 'Namespace monotonicity', status: 'not_reached', fired: false },
  ],
  scalar_components: { champion: { drift: 68.5, schema: 1.43, pass: 1.0 }, challenger: { drift: 145.64, schema: 0.0, pass: 1.0 } },
  primary_driver: { judge: 'incorporates_feedback', delta: 24.0 },
};
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', delta_scalar: 1.51, delta_pass_rate: 0, reason: 'challenger regressed', rules: [], scalar_components: null };
FIXTURE[`/api/run/${EPOCH_ID}/v1/waffles_single/expectations`] = { outcomes: [{ kind: 'predicate', passed: false, detail: 'predicate returned False', judge_name: null, score: null }] };
FIXTURE[`/api/run/${EPOCH_ID}/v1/waffles_single/per-judge`] = { judges: [{ judge_name: 'incorporates_feedback', weighted_loss: 27.0, raw_loss: 27.0, run_count: 1, weight: 1 }] };
FIXTURE['/api/conversation/run_v1_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Drafting an outline now.', tool_calls: [{ name: 'write_slide' }] },
  ],
  annotations: [{ anchor_seq: 1, kind: 'drift', summary: 'omitted the requested structure' }],
};

function installFetch() {
  globalThis.fetch = async (path) => Object.prototype.hasOwnProperty.call(FIXTURE, path)
    ? { ok: true, json: async () => FIXTURE[path] }
    : { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
}
function freshState() {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
}
function setRoot(theme, type) {
  const root = document.createElement('div');
  root.setAttribute('id', 'variant-root');
  root.setAttribute('data-variant', 'L');
  if (theme) root.setAttribute('data-vl-theme', theme);
  if (type) root.setAttribute('data-vl-type', type);
  document.body.appendChild(root);
  return root;
}
function classNodes(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

// ---- router ---------------------------------------------------------

test('router: dashboard-first — Environment is the default view', () => {
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/L/').view, 'home');
  assertEqual(router.parseRoute('#/bogus').view, 'home');
});

test('router: the convergence-II routes parse (board + mutations site + publication)', () => {
  assertEqual(router.parseRoute('#/L/board/waffles_single').params.entry, 'waffles_single');
  const m = router.parseRoute('#/L/mutations/v1/coordinator_prompt');
  assertEqual(m.params.gen, 'v1'); assertEqual(m.params.mutationId, 'coordinator_prompt');
  assertEqual(router.parseRoute('#/L/publication').view, 'publication');
  assertEqual(router.parseRoute('#/L/candidate/v1/waffles_single').params.entry, 'waffles_single');
  assertEqual(router.parseRoute('#/L/run/v1/waffles_single').view, 'run');
});

// ---- the pickers (colour + typeface) -------------------------------

test('colour picker: offers all three themes; applies + persists; L defaults to dark', () => {
  freshState();
  assertEqual(ui.DEFAULT_COLOR, 'solarized-dark');
  const root = document.createElement('div');
  let picked = null;
  const sw = ui.colorSwitcher('solarized-dark', (t) => { picked = t; });
  const ids = sw.querySelectorAll('[data-theme]').map((b) => b.getAttribute('data-theme'));
  assert(ids.includes('solarized-light') && ids.includes('solarized-dark') && ids.includes('monokai'), 'all three colour themes offered');
  ui.applyColor(root, 'monokai');
  assertEqual(root.getAttribute('data-vl-theme'), 'monokai', 'colour applied to root');
  assertEqual(ui.readColor(), 'monokai', 'colour persisted');
  assertEqual(ui.normaliseColor('nonsense'), 'solarized-dark', 'unknown colour → default');
  sw.querySelectorAll('[data-theme]').find((b) => b.getAttribute('data-theme') === 'solarized-light').dispatchEvent({ type: 'click' });
  assertEqual(picked, 'solarized-light', 'colour switcher click fires onPick');
});

test('typeface picker: offers the four Open-Sans pairings; applies + persists; L defaults to Sans', () => {
  freshState();
  assertEqual(ui.DEFAULT_TYPE, 'sans');
  const root = document.createElement('div');
  let picked = null;
  const sw = ui.typeSwitcher('sans', (t) => { picked = t; });
  const ids = sw.querySelectorAll('[data-type]').map((b) => b.getAttribute('data-type'));
  for (const want of ['sans', 'editorial', 'technical', 'display']) assert(ids.includes(want), `typeface ${want} offered`);
  ui.applyType(root, 'editorial');
  assertEqual(root.getAttribute('data-vl-type'), 'editorial', 'typeface applied to root');
  assertEqual(ui.readType(), 'editorial', 'typeface persisted');
  assertEqual(ui.normaliseType('nonsense'), 'sans', 'unknown typeface → default');
  sw.querySelectorAll('[data-type]').find((b) => b.getAttribute('data-type') === 'technical').dispatchEvent({ type: 'click' });
  assertEqual(picked, 'technical', 'typeface switcher click fires onPick');
});

// ---- heatmap: theme-aware ramp -------------------------------------

test('heatmap: ramp derives from the ACTIVE colour theme tokens (not a fixed ramp)', () => {
  // No getComputedStyle in the harness → themeRamp falls back to the
  // data-vl-theme-keyed lookup, which is still theme-AWARE (differs per theme).
  const dark = setRoot('solarized-dark');
  const rampDark = ui.themeRamp(dark);
  const mono = setRoot('monokai');
  const rampMono = ui.themeRamp(mono);
  assert(Array.isArray(rampDark) && rampDark.length === 2, 'ramp is a [lo, hi] pair');
  assert(rampDark[0] !== rampMono[0] || rampDark[1] !== rampMono[1], 'the ramp changes with the active theme');
  // and the heatmap colours its cells from the supplied ramp (no hard-coded fill).
  const mark = svg.heatmap({
    rows: [{ id: 'a', label: 'waffles_single' }], cols: [{ id: 'v0', label: 'v0' }, { id: 'v1', label: 'v1' }],
    ramp: rampMono, value: (r, c) => (c === 'v0' ? 60.5 : 642.5),
  });
  const cells = mark.querySelectorAll('[data-vl]').filter((n) => n.getAttribute('data-vl') === 'hm-cell');
  assertEqual(cells.length, 2, 'one cell per (row × col)');
  // the high-loss cell is filled (a colour from the ramp), not the empty token.
  const filled = cells.map((c) => c.getAttribute('fill'));
  assert(filled.every((f) => f && f !== 'var(--l-cell-empty)'), 'scored cells take a ramp colour');
  assert(filled[0] !== filled[1], 'distinct losses → distinct ramp colours');
});

// ---- sankey: label / value never overlap ---------------------------

test('sankey: per-board label and loss value are SEPARATE nodes, disjoint x-ranges', () => {
  const mark = svg.sankey({
    width: 760, candidate: { label: 'v0', sub: 'patch' },
    boards: [
      { id: 'a', label: 'waffles_single', value: 60.5, ref: 'a' },
      { id: 'b', label: 'picky_stakeholder_emulated', value: 642.5, ref: 'b', cls: 'vl-bad' },
    ],
    aggregate: { label: 'scalar', sub: '703 loss' },
  });
  assert(mark.getAttribute('viewBox'), 'sankey is fit-to-width (viewBox)');
  assert(mark.getAttribute('preserveAspectRatio'), 'sankey preserves aspect ratio');
  const labels = classNodes(mark, 'vl-sankey-label');
  const values = classNodes(mark, 'vl-sankey-value');
  assert(labels.length >= 2 && values.length >= 2, 'each board node carries a separate label and value text node');
  // the label is left-anchored, the value right-anchored — disjoint x ranges
  // → they cannot render on top of each other (the overlap bug).
  const boardLabel = labels.find((n) => (n.textContent || '').includes('picky'));
  const boardValue = values.find((n) => (n.textContent || '').includes('642'));
  assert(boardLabel && boardValue, 'the long-id board has both a label and a value');
  assertEqual(boardLabel.getAttribute('text-anchor'), 'start', 'label is left-anchored');
  assertEqual(boardValue.getAttribute('text-anchor'), 'end', 'value is right-anchored');
  const lx = parseFloat(boardLabel.getAttribute('x'));
  const vx = parseFloat(boardValue.getAttribute('x'));
  assert(vx > lx, 'the value sits to the right of the label start, on its own baseline');
});

// ---- mutation view: ONE cohesive visual (matrix + side-by-side diff) -

test('mutation view: matrix renders; a cell selects a site; the diff is SIDE-BY-SIDE with REAL strings', async () => {
  freshState(); installFetch(); setRoot('solarized-dark');
  const mut = await import('../js/variants/L/views/mutations.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await mut.render(host, ctx, {});
  const matrix = classNodes(host, 'vl-mutmatrix')[0];
  assert(matrix, 'the mutation matrix rendered (the surface)');
  const onCells = matrix.querySelectorAll('[data-vl]').filter((n) => n.getAttribute('data-vl') === 'mut-cell' && (n.getAttribute('class') || '').includes('vl-mm-on'));
  assert(onCells.length >= 1, 'at least one filled (patched) cell');
  onCells[0].dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'mutations' && navTo.p.gen && navTo.p.mutationId, 'a cell click selects BOTH a generation and a site');

  // now render the focused state → the SIDE-BY-SIDE diff fills the detail pane.
  freshState(); installFetch();
  await mut.render(host, ctx, { gen: 'v1', mutationId: 'coordinator_prompt' });
  const sbs = classNodes(host, 'vl-sbs')[0];
  assert(sbs, 'the side-by-side diff rendered');
  const cols = classNodes(sbs, 'vl-diff-col');
  assertEqual(cols.length, 2, 'two columns: champion baseline | challenger new');
  // REAL string content on BOTH sides — never "[object Object]".
  assert(host.textContent.includes('Keep it brief.'), 'baseline string content rendered (from .baseline.content)');
  assert(host.textContent.includes('Always emit an explicit slide structure'), 'challenger new_content string rendered');
  assert(!host.textContent.includes('[object Object]'), 'NO "[object Object]" — the baseline OBJECT is never rendered');
});

test('lineDiff: aligns two strings into added / deleted / same rows', () => {
  const { left, right } = svg.lineDiff('alpha\nbeta', 'alpha\ngamma');
  // the shared "alpha" line is "same" on both sides.
  assert(left.some((r) => r.text === 'alpha' && r.cls === 'same'), 'shared line marked same');
  assert(left.some((r) => r.text === 'beta' && r.cls === 'del'), 'removed line marked del');
  assert(right.some((r) => r.text === 'gamma' && r.cls === 'add'), 'added line marked add');
  assertEqual(left.length, right.length, 'columns are row-aligned');
});

// ---- per-board cross-candidate view + routing ----------------------

test('board view: renders ONE entry across EVERY candidate with a comparative chart + drill', async () => {
  freshState(); installFetch(); setRoot('solarized-dark');
  const board = await import('../js/variants/L/views/board.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await board.render(host, ctx, { entry: 'waffles_single' });
  assert(host.textContent.includes('Board · waffles_single'), 'the board entry is the page subject');
  const chart = classNodes(host, 'vl-cmpbars')[0];
  assert(chart, 'the comparative bar chart rendered');
  const rows = chart.querySelectorAll('[data-vl]').filter((n) => n.getAttribute('data-vl') === 'cmp-row');
  assertEqual(rows.length, 3, 'one bar per candidate (v0, v1, v2 all ran waffles_single)');
  // every candidate's loss + a per-candidate detail table is present.
  assert(classNodes(host, 'vl-board-table').length === 1, 'per-candidate detail table present');
  // clicking a candidate bar drills into ITS run for THIS entry.
  rows[0].dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'run' && navTo.p.entry === 'waffles_single', 'a bar click drills into that candidate’s run for this entry');
});

test('routing: epoch trellis cards AND heatmap cells route to the BOARD view by entry id', async () => {
  freshState(); installFetch(); setRoot('solarized-dark');
  const epoch = await import('../js/variants/L/views/epoch.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await epoch.render(host, ctx, {});
  // a trellis card click → board view, keyed by entry id.
  const cell = host.querySelectorAll('[data-vl]').filter((n) => n.getAttribute('data-vl') === 'trellis-cell')[0];
  assert(cell, 'a board trellis cell rendered');
  cell.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'board' && navTo.p.entry, 'a trellis card routes to the per-board view by entry id');
  assert(navTo.p.entry !== 'v0' && navTo.p.entry !== 'v1', 'routes by ENTRY id, not a candidate id');
  // a heatmap cell click → board view, keyed by entry id.
  navTo = null;
  const hmCell = host.querySelectorAll('[data-vl]').filter((n) => n.getAttribute('data-vl') === 'hm-cell')[0];
  assert(hmCell, 'a heatmap cell rendered');
  hmCell.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'board' && navTo.p.entry, 'a heatmap cell routes to the per-board view by entry id');
});

// ---- promote gate: clean STACKED sections, no overlap --------------

test('gate: laid out as STACKED sections (decision+deltas / rules ladder / scalar-components)', async () => {
  freshState(); installFetch(); setRoot('solarized-dark');
  const cand = await import('../js/variants/L/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await cand.render(host, ctx, { gen: 'v1' });
  const gate = classNodes(host, 'vl-gate')[0];
  assert(gate, 'the promote-gate panel rendered for a challenger');
  // (a) decision + deltas
  assert(classNodes(gate, 'vl-gate-head').length === 1, 'decision + deltas head block present');
  assert(classNodes(gate, 'vl-gate-delta').length >= 2, 'Δscalar and Δpass-rate tiles present');
  // (b) the rules ladder — each rule its OWN row, three named columns.
  const rules = classNodes(gate, 'vl-gate-rule');
  assertEqual(rules.length, 3, 'three rule rows, one each (scalar_margin / pass_rate / namespace)');
  for (const r of rules) {
    assert(classNodes(r, 'vl-gate-rule-label').length === 1, 'rule has its own label cell');
    assert(classNodes(r, 'vl-gate-rule-status').length === 1, 'rule has its own status cell');
    assert(classNodes(r, 'vl-gate-rule-detail').length === 1, 'rule has its own detail cell — never overlapping the dot-plot');
  }
  // (c) a SEPARATE champion-vs-challenger scalar-components block (a table,
  //     NOT overlaid on the rules).
  const comp = classNodes(host, 'vl-comp-table');
  assert(comp.length === 1, 'a separate scalar-components comparison table');
  assert(host.textContent.includes('145.64') && host.textContent.includes('68.5'), 'both sides’ drift components shown');
  // the three blocks are distinct stacked sub-blocks within the gate panel.
  assert(classNodes(gate, 'vl-gate-block').length >= 2, 'rules + scalar-components are separate stacked blocks');
});

// ---- digest gate: no-op on a heartbeat repaint ---------------------

test('digest gate: an identical repaint does NOT rebuild the DOM (no flash)', async () => {
  freshState(); installFetch(); setRoot('solarized-dark');
  const home = await import('../js/variants/L/views/home.js');
  // home reads /api/workspace which is not in the fixture → honest fallback,
  // but the digest gate still applies. Use the epoch view (data-rich) instead.
  const epoch = await import('../js/variants/L/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epoch.render(host, ctx, {});
  const digest1 = host.getAttribute('data-vl-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'epoch painted');
  await epoch.render(host, ctx, {});
  assertEqual(host.getAttribute('data-vl-digest'), digest1, 'digest unchanged on the heartbeat repaint');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// ---- cold deep-link transcript -------------------------------------

test('run view: a COLD deep-link fetches the conversation and renders the transcript', async () => {
  freshState(); installFetch(); setRoot('solarized-dark');
  const runView = await import('../js/variants/L/views/run.js');
  const host = document.createElement('div');
  await runView.render(host, { navigate() {}, href: router.href }, { gen: 'v1', entry: 'waffles_single' });
  const scroller = classNodes(host, 'vl-transcript')[0];
  assert(scroller, 'the transcript scroll container rendered');
  const turns = scroller.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('vl-turn vl-turn-'));
  assertEqual(turns.length, 2, 'transcript shows its turns');
  assert(host.textContent.includes('Drafting an outline'), 'turn text rendered from /api/conversation');
  assert(host.textContent.includes('omitted the requested structure'), 'drift annotation rendered');
});

test('run view: a deep-link to a missing run id degrades to an honest empty state', async () => {
  freshState(); installFetch(); setRoot('solarized-dark');
  const runView = await import('../js/variants/L/views/run.js');
  const host = document.createElement('div');
  await runView.render(host, { navigate() {}, href: router.href }, { gen: 'v1', entry: 'does_not_exist' });
  assert(host.textContent.toLowerCase().includes('no run id'), 'honest empty for an unknown entry');
});

// ---- publication: K's renderer reused; GFM tables render -----------

test('publication: reuses K-style renderer; GFM table renders (not raw | … |)', async () => {
  freshState(); installFetch(); setRoot('solarized-dark');
  const pub = await import('../js/variants/L/views/publication.js');
  const host = document.createElement('div');
  await pub.render(host, { navigate() {}, href: router.href }, {});
  const article = classNodes(host, 'vl-paper')[0];
  assert(article, 'the publication article rendered');
  assert(host.textContent.includes('Presentation agent · epoch e0'), 'the title typeset');
  assert(classNodes(host, 'vl-eyebrow').length >= 1, 'eyebrow rendered');
  assert(classNodes(host, 'vl-abstract').length >= 1, 'abstract rendered');
  // the GFM table from analysis_md renders as a real <table>, NOT raw pipes.
  const tables = host.querySelectorAll('[class]').filter((n) => (n.localName === 'table'));
  assert(tables.length >= 1, 'a GFM table rendered as <table>');
  assert(host.querySelectorAll('[class]').some((n) => n.localName === 'th'), 'the table has header cells');
});

test('parsePaper splits eyebrow / title / meta / abstract / body', () => {
  const p = paper.parsePaper(ANALYSIS_MD);
  assert(p.eyebrow.includes('improvement campaign'), 'eyebrow captured');
  assertEqual(p.title, 'Presentation agent · epoch e0');
  assert(p.meta.some((m) => m.label === 'Epoch id'), 'meta label parsed');
  assert(p.abstract.includes('Two challengers'), 'abstract captured');
  assert(p.body.includes('<!-- FIGURE:mutation-surface -->'), 'figure markers remain in body');
});

await run();
