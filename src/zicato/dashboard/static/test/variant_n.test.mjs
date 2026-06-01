// test/variant_n.test.mjs — Variant N ("Console II") unit tests.
//
// N is the dense convergence-II observatory (monokai + JetBrains-Mono default).
// These pin the brief's seven mandatory fixes + the typeface picker:
//   * side-by-side mutation diff renders REAL strings (not "[object Object]");
//   * the per-board view + trellis/heatmap route to it (by entry id);
//   * the promote gate is stacked, rules each on their own row, no overlap;
//   * the sankey per-board node's loss VALUE is a distinct mark from its label;
//   * the heatmap is theme-token-driven (legible in monokai);
//   * the typeface picker + colour picker both switch + persist;
//   * digest-gated repaint is a true no-op;
//   * a COLD deep-link transcript renders content;
//   * the publication renders a GFM table.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/N/router.js');
const svg = await import('../js/variants/N/svg.js');
const ui = await import('../js/variants/N/ui.js');
const shell = await import('../js/variants/N/shell.js');
const data = await import('../js/variants/N/data.js');

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
  '',
  '## Lineage',
  '',
  '<!-- FIGURE:lineage -->',
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
  // baseline DETAIL for one site — .baseline.content is the STRING (not object).
  [`/api/mutations/${EPOCH_ID}/coordinator_prompt`]: {
    mutation_id: 'coordinator_prompt', epoch_id: EPOCH_ID,
    baseline: { generation_id: 'v0', content: 'You are the coordinator.\nDraft an outline.', file: 'agent/coordinator.py', role: 'coordinator system prompt', line_start: 10, line_end: 40 },
    versions: [{ generation_id: 'v1', op: 'edit', rationale: 'Enforce structure.', content: 'You are the coordinator.\nAlways emit an explicit slide structure.' }],
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
FIXTURE['/api/conversation/run_v1_waffles'] = {
  turns: [
    { seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' },
    { seq: 1, role: 'agent', agent: 'coordinator', text: 'Drafting an outline now.', tool_calls: [{ name: 'write_slide' }] },
  ],
  annotations: [{ anchor_seq: 1, kind: 'drift', summary: 'omitted the requested structure' }],
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

test('router: environment is the default; the new views parse', () => {
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/N/').view, 'home');
  assertEqual(router.parseRoute('#/bogus').view, 'home');
  assertEqual(router.parseRoute('#/N/board/waffles_single').params.entry, 'waffles_single');
  assertEqual(router.parseRoute('#/N/mutations/coordinator_prompt').params.mutId, 'coordinator_prompt');
  assertEqual(router.parseRoute('#/N/publication').view, 'publication');
  assertEqual(router.parseRoute('#/N/run/v1/waffles_single').view, 'run');
});

test('router: board + publication crumbs are rooted at environment', () => {
  for (const r of [{ view: 'board', params: { entry: 'x' } }, { view: 'publication', params: {} }, { view: 'mutations', params: {} }]) {
    const trail = router.crumbTrail(r);
    assertEqual(trail[0].view, 'home');
    assert(trail[trail.length - 1].current === true, 'leaf is current');
  }
});

// ---- FIX #2: side-by-side mutation diff with REAL strings ----------

test('sideBySideDiff: renders two columns of real strings (NOT "[object Object]")', () => {
  const mark = svg.sideBySideDiff({
    baseline: 'You are the coordinator.\nDraft an outline.',
    challenger: 'You are the coordinator.\nAlways emit an explicit slide structure.',
    leftLabel: 'champion baseline', rightLabel: 'challenger new',
  });
  assert(mark.textContent.includes('Always emit an explicit slide structure'), 'the new content string rendered');
  assert(mark.textContent.includes('Draft an outline'), 'the baseline string rendered');
  assert(!mark.textContent.includes('[object Object]'), 'no "[object Object]" — strings, not objects');
  // two head columns, old | new.
  assertEqual(allByClass(mark, 'dn-sxs-col-h').length, 2, 'two side-by-side columns');
});

test('mutations view: matrix + side-by-side diff, baseline.content drives the LEFT column', async () => {
  freshState(); installFetch();
  const mut = await import('../js/variants/N/views/mutations.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await mut.render(host, ctx, {});
  const matrix = allByClass(host, 'dn-mtx')[0];
  assert(matrix, 'the mutation matrix rendered');
  const onCells = matrix.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-mtx-on'));
  assert(onCells.length >= 1, 'at least one filled (patched) cell');
  // drill into the pinned site → the side-by-side diff fills, with real strings.
  freshState(); installFetch();
  await mut.render(host, ctx, { mutId: 'coordinator_prompt' });
  const sxs = allByClass(host, 'dn-sxs')[0];
  assert(sxs, 'the side-by-side diff pane filled on select');
  assert(host.textContent.includes('Always emit an explicit slide structure'), 'challenger new_content (from /patches) on the right');
  assert(host.textContent.includes('Draft an outline'), 'baseline.content (from /mutations/{id}) on the left');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');
});

// ---- FIX #7: the per-board view + trellis/heatmap route to it ------

test('board view: pivots one entry across every candidate; rows drill to the run', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/N/views/board.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await board.render(host, ctx, { entry: 'waffles_single' });
  assert(host.textContent.includes('Board · waffles_single'), 'the per-board heading');
  const dot = allByClass(host, 'dn-valdot');
  assert(dot.length === 1, 'a per-candidate comparative dot-plot');
  // every candidate that ran this entry appears.
  assert(host.textContent.includes('v0') && host.textContent.includes('v1') && host.textContent.includes('v2'), 'all candidates listed');
  // a run link drills with BOTH the gen and THIS entry id.
  const runLink = allByClass(host, 'dn-board-run')[0];
  assert(runLink && (runLink.getAttribute('href') || '').includes('waffles_single'), 'drill link keeps the entry id');
});

test('epoch view: trellis cards AND heatmap cells route to the per-board view (by entry id)', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/N/views/epoch.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await epoch.render(host, ctx, {});
  // a heatmap cell click → board, by the ROW (entry) id.
  const heat = allByClass(host, 'dn-heatmap')[0];
  assert(heat, 'heatmap present');
  const cell = heat.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-hm-cell') && !(n.getAttribute('class') || '').includes('dn-hm-empty'))[0];
  assert(cell, 'a valued heatmap cell exists');
  cell.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'board' && navTo.p.entry, 'heatmap cell routes to the per-board view keyed by entry id');
  // a trellis card click → board too.
  navTo = null;
  const trellisCard = allByClass(host, 'dn-trellis-cell')[0];
  assert(trellisCard, 'a trellis card exists');
  trellisCard.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'board' && navTo.p.entry, 'trellis card routes to the per-board view keyed by entry id');
});

// ---- FIX #1: the promote gate is stacked, no overlap --------------

test('matchups view: the promote gate is stacked — rules each on their own row', async () => {
  freshState(); installFetch();
  const matchups = await import('../js/variants/N/views/matchups.js');
  const host = document.createElement('div');
  await matchups.render(host, { navigate() {}, href: router.href }, {});
  const gate = allByClass(host, 'dn-gate')[0];
  assert(gate, 'a promote-gate panel rendered');
  const rules = allByClass(host, 'dn-rule');
  assert(rules.length >= 3, 'each gate rule is its own row (3 short-circuiting rules)');
  // the scalar-components comparison is a SEPARATE block below the rules.
  assert(allByClass(host, 'dn-sc-table').length >= 1, 'a separate champion-vs-challenger scalar-components block');
  assert(host.textContent.includes('Scalar margin'), 'a rule label present');
});

// ---- FIX #5: sankey label ≠ value ---------------------------------

test('sankey: the per-board loss VALUE is a distinct mark from the label (no overlap)', () => {
  const mark = svg.sankey({
    width: 760, colHeight: 220, nodeW: 150,
    patch: [{ id: 'patch', label: 'patch v0→v1', value: 703, cls: 'dn-sankey-patch' }],
    drift: [
      { id: 'd_picky_stakeholder_emulated', label: 'picky_stakeholder_emulated', value: 642.5, cls: 'dn-bad' },
      { id: 'd_waffles_single', label: 'waffles_single', value: 60.5, cls: 'dn-flat' },
    ],
    gate: [{ id: 'gate', label: '✕ rejected', value: 703, cls: 'dn-bad' }],
    links: [],
  });
  assert(mark.getAttribute('viewBox'), 'fit-to-width responsive viewBox');
  const values = allByClass(mark, 'dn-sankey-value');
  assert(values.length >= 2, 'each drift node carries its loss value as its OWN element');
  // the value text and the label text are different nodes (not concatenated).
  const labels = allByClass(mark, 'dn-sankey-label');
  assert(labels.some((l) => l.textContent.includes('picky')), 'the label keeps the (truncated) board id');
  assert(values.some((v) => v.textContent.replace(/\D/g, '') === '643' || v.textContent.replace(/\D/g, '') === '642'), 'the value is the numeric loss, separate from the label');
});

// ---- FIX #4: heatmap is theme-token-driven (monokai-legible) ------

test('heatmap: cells are theme-token ink at value-driven opacity (no fixed hex ramp)', () => {
  const mark = svg.heatmap({
    rows: [{ id: 'a', label: 'waffles_single' }, { id: 'b', label: 'picky' }],
    cols: [{ id: 'v0', label: 'v0' }, { id: 'v1', label: 'v1' }],
    value: (r, c) => (r === 'b' && c === 'v1' ? 642.5 : 60.5),
  });
  const cells = allByClass(mark, 'dn-hm-cell').filter((n) => !(n.getAttribute('class') || '').includes('dn-hm-empty'));
  assert(cells.length >= 1, 'valued cells exist');
  // no cell carries a hard-coded hex fill — the colour comes from the theme
  // token (CSS) at a value-driven fill-opacity.
  for (const c of cells) {
    const fill = c.getAttribute('fill');
    assert(fill == null || !/^#|rgb\(/.test(String(fill)), 'no fixed hex/rgb fill — token-driven');
    assert(c.getAttribute('fill-opacity') != null, 'value-driven opacity present');
  }
});

// ---- typeface + colour pickers switch + persist -------------------

test('pickers: typeface (Technical default) + colour (monokai default) switch + persist', () => {
  freshState();
  const root = document.createElement('div');
  // defaults
  assertEqual(ui.DEFAULT_COLOR, 'monokai', 'monokai is the default colour theme');
  assertEqual(ui.DEFAULT_TYPE, 'technical', 'Technical is the default typeface');
  // all four typefaces offered.
  const typeIds = ui.TYPE_THEMES.map((t) => t[0]);
  assert(['sans', 'editorial', 'technical', 'display'].every((t) => typeIds.includes(t)), 'all four typefaces offered');
  // applyTheme + applyTypeface stamp the root attributes + persist.
  shell.applyTheme('solarized-dark', root);
  assertEqual(root.getAttribute('data-n-theme'), 'solarized-dark', 'colour applied to root');
  assertEqual(ui.readColor(), 'solarized-dark', 'colour persisted');
  shell.applyTypeface('editorial', root);
  assertEqual(root.getAttribute('data-n-type'), 'editorial', 'typeface applied to root');
  assertEqual(ui.readType(), 'editorial', 'typeface persisted');
  // bad values fall back to the defaults.
  assertEqual(ui.normaliseColor('nonsense'), 'monokai', 'unknown colour → monokai');
  assertEqual(ui.normaliseType('nonsense'), 'technical', 'unknown typeface → technical');
});

// ---- digest-gated repaint (no-op) ---------------------------------

test('home view: digest-gated — identical data does NOT rebuild the DOM', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/N/views/home.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await home.render(host, ctx, {});
  const digest1 = host.getAttribute('data-n-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'home painted');
  await home.render(host, ctx, {});
  assertEqual(host.getAttribute('data-n-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// ---- cold deep-link transcript ------------------------------------

test('run view: a COLD deep-link fetches the conversation and renders the transcript', async () => {
  freshState(); installFetch();
  const runView = await import('../js/variants/N/views/run.js');
  const host = document.createElement('div');
  await runView.render(host, { navigate() {}, href: router.href }, { gen: 'v1', entry: 'waffles_single' });
  const scroller = allByClass(host, 'dn-transcript')[0];
  assert(scroller, 'the transcript scroll container rendered');
  assert(host.textContent.includes('Drafting an outline'), 'turn text rendered from /api/conversation');
  assert(host.textContent.includes('omitted the requested structure'), 'drift annotation rendered');
});

// ---- FIX #3: the publication renders a GFM table ------------------

test('publication view: renders from analysis_md AND a GFM table renders (not raw "| … |")', async () => {
  freshState(); installFetch();
  const pub = await import('../js/variants/N/views/publication.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await pub.render(host, ctx, { epochId: EPOCH_ID });
  assert(allByClass(host, 'dn-paper')[0], 'the publication article rendered');
  assert(host.textContent.includes('Presentation agent · epoch e0'), 'the title typeset');
  // the GFM table from analysis_md renders as a real <table>, not raw pipes.
  const tables = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-md-table') || n.localName === 'table');
  const realTables = host.querySelectorAll('[class]').filter((n) => n.localName === 'table');
  assert(realTables.length >= 1, 'at least one real <table> element (GFM table rendered)');
  assert(!host.textContent.includes('| generation | scalar |'), 'the table row is NOT left as raw markdown pipes');
  // the combined aggregate-scores figure (chart + table) is one cohesive visual.
  assert(allByClass(host, 'dn-scores-fig').length >= 1, 'the combined aggregate-scores figure rendered');
  // per-matchup detail is appended.
  assert(allByClass(host, 'dn-paper-matchups').length >= 1, 'per-matchup detail appended to the paper');
});

test('publication GFM table renderer: a standalone table parses to thead + rows', () => {
  const md = ['| a | b |', '| --- | --- |', '| 1 | 2 |', '| 3 | 4 |'].join('\n');
  const node = ui.renderMarkdown(md);
  const table = node.querySelectorAll('[class]').filter((n) => n.localName === 'table')[0];
  assert(table, 'a <table> element produced');
  assert(node.textContent.includes('1') && node.textContent.includes('4'), 'body cells rendered');
});

await run();
