// test/variant_o.test.mjs — Variant O ("Compass") unit tests.
//
// Compass is a master-detail two-pane workspace: a persistent selector rail
// (epoch → generation → board entry) + a detail pane that follows the
// EXPLICIT, PERSISTENT selection in the URL. These tests pin:
//   * the router (typed selection; deep-link round-trip; mutations site slot);
//   * the two-pane layout (rail + detail) renders;
//   * selecting a board entry opens the per-board CROSS-CANDIDATE view —
//     keyed by ENTRY ID, never an arbitrary candidate;
//   * the side-by-side mutation diff renders REAL strings (NOT "[object
//     Object]"): baseline = /api/mutations/{e}/{mid}.baseline.content,
//     challenger = the matching patch's .new_content;
//   * the promote gate is laid out as clean stacked sections (rules ladder +
//     a SEPARATE scalar-components block) with no overlap;
//   * the sankey's per-board label and loss VALUE are distinct nodes (≠);
//   * the heatmap accepts a theme-derived ramp;
//   * the typeface + color pickers switch (and persist) the root attributes;
//   * a digest-gated repaint is a no-op (identical data → no rebuild);
//   * a COLD deep-link hydrates the selection (run transcript paints);
//   * the publication GFM table renders.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/O/router.js');
const ui = await import('../js/variants/O/ui.js');
const svg = await import('../js/variants/O/svg.js');
const data = await import('../js/variants/O/data.js');
const dom = await import('../js/core/dom.js');

const EPOCH_ID = '2026-05-30_e0';

const FIXTURE = {
  '/api/epoch': {
    epoch_id: EPOCH_ID, closed: false, goal: 'Improve the presentation agent.',
    board: [
      { id: 'waffles_single', kind: 'single_turn', input_preview: 'Make a presentation about waffles.', expectation_kind: 'predicate', budget_s: 180, weight: 1, tags: ['smoke'] },
      { id: 'picky_stakeholder_emulated', kind: 'multi_turn_emulated', input_preview: null, expectation_kind: null, budget_s: 360, weight: 1, tags: ['hard'] },
    ],
  },
  '/api/lineage': { generations: [
    { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v2', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
  ] },
  '/api/tournaments': { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71 },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51 },
  ] },
  // analysis with a GFM table + a figure marker.
  [`/api/epoch/${EPOCH_ID}/analysis`]: { analysis_md: [
    '<!-- EYEBROW -->', '', 'epoch report', '# Improving the presentation agent', '',
    '<!-- META -->', '**Epoch**: ' + EPOCH_ID + '  **Champion**: v0', '',
    '## Abstract', 'Two challengers were tried; both regressed.', '',
    '## Results', '', '<!-- FIGURE:combined-scores -->', '',
    '| generation | aggregate loss |', '| --- | --- |', '| v0 | 166.0 |', '| v1 | 703.0 |', '',
  ].join('\n') },
  // mutation surface.
  [`/api/mutations/${EPOCH_ID}`]: { generations: ['v0', 'v1', 'v2'], mutations: [
    { mutation_id: 'coordinator_prompt', kind: 'prompt', file: 'agent/coordinator.py', role: 'coordinator system prompt', line_start: 10, line_end: 40, patched_generation_ids: ['v1'] },
    { mutation_id: 'planner_prompt', kind: 'prompt', file: 'agent/planner.py', role: 'planner brief', line_start: 5, line_end: 12, patched_generation_ids: ['v2'] },
  ] },
  // the BASELINE for one site — a STRING at .baseline.content (NOT the object).
  [`/api/mutations/${EPOCH_ID}/coordinator_prompt`]: {
    mutation_id: 'coordinator_prompt',
    baseline: { content: 'You are the coordinator.\nKeep slides terse.\nAlways cite sources.' },
  },
  // what v1 changed → the challenger STRING is .new_content.
  [`/api/files/${EPOCH_ID}/v1/patches`]: { patches: [
    { id: 'p1', mutation_id: 'coordinator_prompt', op: 'edit', rationale: 'enforce explicit slide structure',
      new_content: 'You are the coordinator.\nKeep slides terse.\nEMIT an explicit slide outline first.\nAlways cite sources.' },
  ] },
  [`/api/files/${EPOCH_ID}/v0/patches`]: { patches: [] },
  [`/api/files/${EPOCH_ID}/v2/patches`]: { patches: [
    { id: 'p2', mutation_id: 'planner_prompt', op: 'edit', new_content: 'Plan tightly.' },
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
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v2_picky', drift_loss: 110.0, pass_fail: 0, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`] = {
  decision: 'rejected', reason: 'challenger regressed: loss rose by 75.71', delta_scalar: 75.71, delta_pass_rate: 0.0,
  rules: [
    { id: 'regression_suite', label: 'Regression suite', status: 'skipped', fired: false },
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', detail: '70.94 → 146.65 (+75.71; needs ≤ -0.01)', fired: true },
    { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'not_reached', fired: false },
  ],
  scalar_components: {
    champion: { cost: 0.009, drift: 68.5, pass: 1.0, schema: 1.43 },
    challenger: { cost: 0.009, drift: 145.64, pass: 1.0, schema: 0.0 },
  },
  primary_driver: { judge: 'incorporates_feedback', delta: 24.0 },
};
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', reason: '…', delta_scalar: 1.51, rules: [], scalar_components: null };
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v1`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v1', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 60.5, delta: 0, verdict: 'flat', won_by: null },
  { entry_id: 'picky_stakeholder_emulated', parent_drift_loss: 105.5, child_drift_loss: 642.5, delta: 537, verdict: 'regressed', won_by: 'v0' },
] };
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v2`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v2', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 61.0, delta: 0.5, verdict: 'regressed', won_by: 'v0' },
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
    if (Object.prototype.hasOwnProperty.call(FIXTURE, path)) return { ok: true, json: async () => FIXTURE[path] };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}
function freshState() {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
}

// ---- router ----------------------------------------------------------

test('router: parses typed selections under the #/O/ prefix', () => {
  assertEqual(router.parseRoute('#/O/').view, 'overview');
  const g = router.parseRoute('#/O/gen/v1/mutations');
  assertEqual(g.view, 'gen'); assertEqual(g.kind, 'gen'); assertEqual(g.gen, 'v1'); assertEqual(g.facet, 'mutations');
  const gs = router.parseRoute('#/O/gen/v1/mutations/coordinator_prompt');
  assertEqual(gs.facet, 'mutations'); assertEqual(gs.entry, 'coordinator_prompt');
  const b = router.parseRoute('#/O/board/waffles_single');
  assertEqual(b.view, 'board'); assertEqual(b.kind, 'board'); assertEqual(b.entry, 'waffles_single');
  const r = router.parseRoute('#/O/gen/v1/run/waffles_single');
  assertEqual(r.view, 'run'); assertEqual(r.gen, 'v1'); assertEqual(r.entry, 'waffles_single');
});

test('router: a foreign / empty hash defaults to overview', () => {
  assertEqual(router.parseRoute('').view, 'overview');
  assertEqual(router.parseRoute('#/something').view, 'overview');
});

test('router: href round-trips a board + a gen+facet selection', () => {
  assertEqual(router.parseRoute(router.href('board', { entry: 'q3' })).entry, 'q3');
  const back = router.parseRoute(router.href('gen', { gen: 'v2', facet: 'matchups' }));
  assertEqual(back.gen, 'v2'); assertEqual(back.facet, 'matchups');
  const site = router.parseRoute(router.href('gen', { gen: 'v1', facet: 'mutations', entry: 'planner_prompt' }));
  assertEqual(site.entry, 'planner_prompt');
});

test('router: selectionKey is stable per selection', () => {
  const a = router.selectionKey(router.parseRoute('#/O/board/waffles_single'));
  const b = router.selectionKey(router.parseRoute('#/O/board/waffles_single'));
  const c = router.selectionKey(router.parseRoute('#/O/gen/v1/lifecycle'));
  assertEqual(a, b); assert(a !== c, 'different selections → different keys');
});

// ---- gatedSwap: the no-flash guarantee -------------------------------

test('gatedSwap rebuilds on a changed digest and no-ops on an identical one', () => {
  const host = document.createElement('div');
  let builds = 0;
  ui.gatedSwap(host, 'A', () => { builds += 1; return [dom.el('p', { text: 'one' })]; });
  ui.gatedSwap(host, 'A', () => { builds += 1; return [dom.el('p', { text: 'one' })]; });
  assertEqual(builds, 1, 'identical digest is a no-op');
  ui.gatedSwap(host, 'B', () => { builds += 1; return [dom.el('p', { text: 'two' })]; });
  assertEqual(builds, 2, 'a changed digest rebuilds');
});

// ---- side-by-side diff: REAL strings, NOT [object Object] -------------

test('lineDiff marks added / removed / unchanged lines', () => {
  const ops = ui.lineDiff(['a', 'b', 'c'], ['a', 'x', 'b', 'c']);
  assert(ops.some((o) => o.kind === 'add' && o.right === 'x'), 'the inserted line is an add');
  assert(ops.filter((o) => o.kind === 'same').length === 3, 'a/b/c are unchanged');
});

test('sideBySideDiff renders two columns of real string content (no [object Object])', () => {
  const baseline = FIXTURE[`/api/mutations/${EPOCH_ID}/coordinator_prompt`].baseline.content;
  const challenger = FIXTURE[`/api/files/${EPOCH_ID}/v1/patches`].patches[0].new_content;
  assertEqual(typeof baseline, 'string');
  assertEqual(typeof challenger, 'string');
  const node = ui.sideBySideDiff(baseline, challenger);
  const heads = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('vo-sxs-col-head'));
  assert(heads.length === 2, 'two column heads (baseline | new)');
  const txt = node.textContent;
  assert(txt.includes('You are the coordinator.'), 'baseline string rendered');
  assert(txt.includes('EMIT an explicit slide outline first.'), 'challenger new_content string rendered');
  assert(!txt.includes('[object Object]'), 'never renders the baseline object');
});

// ---- gate: clean stacked sections, no overlap ------------------------

test('gatePanel stacks a rules ladder + a SEPARATE scalar-components block', () => {
  const gate = FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`];
  const node = ui.gatePanel(gate, 'v0', 'v1', { fmt: svg.fmt, fmtSigned: svg.fmtSigned });
  const rules = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('vo-rule vo-rule-'));
  assertEqual(rules.length, 3, 'one row per rule');
  // each rule row carries a label, a status, and a detail in its OWN cells.
  const first = rules.find((r) => (r.getAttribute('class') || '').includes('vo-rule-fail'));
  assert(first, 'the fired rule row exists');
  assert(first.textContent.includes('Scalar margin'), 'rule label present');
  assert(first.textContent.includes('fail'), 'rule status present');
  // the scalar-components live in their OWN table block (not overlaid on rules).
  const tables = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-sc-table');
  assertEqual(tables.length, 1, 'a separate champion-vs-challenger components table');
  assert(node.textContent.includes('drift'), 'a scalar component is named');
});

// ---- sankey: label ≠ value ------------------------------------------

test('sankey draws the board label and its loss value as DISTINCT nodes', () => {
  const node = svg.sankey({
    width: 760, candidate: { label: 'v1', sub: 'patch' },
    boards: [
      { id: 'picky_stakeholder_emulated', label: 'picky_stakeholder_emulated', value: 642.5 },
      { id: 'waffles_single', label: 'waffles_single', value: 60.5 },
    ],
    aggregate: { label: 'scalar', sub: '703.0 loss' },
  });
  const labels = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-sankey-label');
  const vals = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-sankey-val');
  assert(labels.length >= 2, 'a label per board node');
  assert(vals.length >= 2, 'a value per board node, separate from the label');
  // The label text is the (truncated) entry id; the value text is the number
  // — they are different text nodes, so the value never overprints the label.
  const labelTexts = labels.map((n) => n.textContent);
  const valTexts = vals.map((n) => n.textContent);
  assert(labelTexts.some((t) => t.startsWith('picky')), 'label carries the entry id');
  assert(valTexts.some((t) => t.includes('642')), 'value carries the loss, on its own node');
  assert(!labelTexts.some((t) => t.includes('642')), 'the label never contains the value');
});

// ---- heatmap: theme-aware ramp --------------------------------------

test('heatRamp resolves a [lo,hi] pair per theme; heatmap consumes it', () => {
  for (const t of ['solarized-dark', 'solarized-light', 'monokai']) {
    const ramp = ui.heatRamp(null, t);
    assert(Array.isArray(ramp) && ramp.length === 2, 'ramp is a pair');
    assert(/^#[0-9a-f]{6}$/i.test(ramp[0]) && /^#[0-9a-f]{6}$/i.test(ramp[1]), 'ramp endpoints are hex for ' + t);
    assert(ramp[0] !== ramp[1], 'ramp lo ≠ hi for ' + t);
  }
  const node = svg.heatmap({
    rows: [{ id: 'a', label: 'a' }], cols: [{ id: 'v0', label: 'v0' }], ramp: ui.heatRamp(null, 'monokai'),
    value: () => 5,
  });
  assert(node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-hm-cell').length === 1, 'cell drawn with the ramp');
});

// ---- typeface + color pickers switch & persist -----------------------

test('typeface + color pickers set (and persist) the root attributes', () => {
  freshState();
  const root = document.createElement('div');
  // typeface
  assertEqual(ui.applyTypeface(root, 'editorial'), 'editorial');
  assertEqual(root.getAttribute('data-vo-type'), 'editorial');
  assertEqual(ui.readTypeface(), 'editorial', 'persisted');
  // O defaults to "display".
  assertEqual(ui.normaliseTypeface('bogus'), 'display');
  // color
  assertEqual(ui.applyTheme(root, 'monokai'), 'monokai');
  assertEqual(root.getAttribute('data-vo-theme'), 'monokai');
  assertEqual(ui.readTheme(), 'monokai', 'persisted');
  // the switchers fire onPick with the chosen id.
  let pickedFace = null; let pickedTheme = null;
  const tf = ui.typefaceSwitcher('display', (f) => { pickedFace = f; });
  tf.querySelectorAll('[data-type]').filter((b) => b.getAttribute('data-type') === 'technical')[0].dispatchEvent({ type: 'click' });
  assertEqual(pickedFace, 'technical');
  const cs = ui.themeSwitcher('solarized-dark', (t) => { pickedTheme = t; });
  cs.querySelectorAll('[data-theme]').filter((b) => b.getAttribute('data-theme') === 'solarized-light')[0].dispatchEvent({ type: 'click' });
  assertEqual(pickedTheme, 'solarized-light');
});

// ---- the rail + a digest-gated no-op --------------------------------

test('rail renders epoch + generations + board entries; identical data is a no-op', async () => {
  freshState(); installFetch();
  const { loadRailModel } = await import('../js/variants/O/model.js');
  const rail = await import('../js/variants/O/rail.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  const m = await loadRailModel();
  rail.renderRail(host, ctx, { epochId: m.epochId, gens: m.gens, board: m.board, selection: router.parseRoute('#/O/') });
  const gens = host.querySelectorAll('[data-gen]').filter((n) => (n.getAttribute('class') || '').includes('vo-rail-gen'));
  const boards = host.querySelectorAll('[data-entry]');
  assertEqual(gens.length, 3, 'a rail row per generation');
  assertEqual(boards.length, 2, 'a rail row per board entry');
  const first = host.firstChild;
  rail.renderRail(host, ctx, { epochId: m.epochId, gens: m.gens, board: m.board, selection: router.parseRoute('#/O/') });
  assert(host.firstChild === first, 'identical data → no rebuild');
});

// ---- two-pane layout renders (rail + detail) -------------------------

test('overview detail pane renders the lineage + workspace glance', async () => {
  freshState(); installFetch();
  const overview = await import('../js/variants/O/views/overview.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await overview.render(host, ctx, router.parseRoute('#/O/'));
  assert(host.children.length > 0, 'overview painted');
  const bumps = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-bumps');
  assertEqual(bumps.length, 1, 'lineage bumps present');
  const first = host.firstChild;
  await overview.render(host, ctx, router.parseRoute('#/O/'));
  assert(host.firstChild === first, 'identical data → no rebuild');
});

// ---- selecting a board entry opens the per-board cross-candidate view --

test('board view opens BY ENTRY ID showing every candidate (not a candidate)', async () => {
  freshState(); installFetch();
  const boardView = await import('../js/variants/O/views/board.js');
  const host = document.createElement('div');
  let routedTo = null;
  const ctx = { navigate: (v, p) => { routedTo = { v, p }; }, href: router.href };
  await boardView.render(host, ctx, router.parseRoute('#/O/board/waffles_single'));
  assert(host.textContent.includes('waffles_single'), 'the board entry id is the subject');
  assert(host.textContent.includes('cross-candidate'), 'this is the cross-candidate view');
  // the comparative chart shows a bar per candidate that ran the entry.
  const bars = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-sortbars');
  assertEqual(bars.length, 1, 'a sorted comparative chart');
  const sortRows = host.querySelectorAll('[data-vo]').filter((n) => n.getAttribute('data-vo') === 'sortrow');
  assertEqual(sortRows.length, 3, 'one row per candidate (v0/v1/v2), keyed by candidate');
  // a drill-to-run list with a row per candidate.
  assert(host.textContent.includes('open run'), 'each candidate drills to its run for THIS board');
  // clicking a candidate row routes to that candidate's RUN for THIS board
  // entry — carrying the entry id, never an arbitrary candidate view.
  const list = host.querySelectorAll('[data-gen]').filter((n) => (n.getAttribute('class') || '').includes('vo-runlist-item'));
  assert(list.length >= 1, 'run-list rows present');
  list[0].dispatchEvent({ type: 'click' });
  assertEqual(routedTo.v, 'run');
  assertEqual(routedTo.p.entry, 'waffles_single', 'routes to a RUN keyed by the board entry id');
});

// ---- candidate mutations facet: combined matrix + side-by-side diff ---

test('candidate mutations facet combines the matrix with a real-string side-by-side diff', async () => {
  freshState(); installFetch();
  const cand = await import('../js/variants/O/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  // select v1's mutations facet with the coordinator site chosen.
  await cand.render(host, ctx, router.parseRoute('#/O/gen/v1/mutations/coordinator_prompt'));
  const matrix = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-mutmatrix');
  const diff = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-sxs');
  assertEqual(matrix.length, 1, 'the site × generation matrix');
  assertEqual(diff.length, 1, 'ONE side-by-side diff, in the same combined visual');
  assert(host.textContent.includes('EMIT an explicit slide outline first.'), 'challenger .new_content rendered');
  assert(host.textContent.includes('Always cite sources.'), 'baseline .content rendered');
  assert(!host.textContent.includes('[object Object]'), 'no [object Object] bug');
});

// ---- candidate lifecycle facet: dot-plot + sankey + gate -------------

test('candidate lifecycle facet renders per-board scoring, the sankey, and the gate', async () => {
  freshState(); installFetch();
  const cand = await import('../js/variants/O/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await cand.render(host, ctx, router.parseRoute('#/O/gen/v1/lifecycle'));
  const dot = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-valdot');
  const sankey = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-sankey');
  const gate = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-gate');
  assertEqual(dot.length, 1, 'per-board value dot-plot');
  assertEqual(sankey.length, 1, 'the Tufte sankey');
  assertEqual(gate.length, 1, 'the promote gate panel');
});

// ---- publication facet: GFM table renders ----------------------------

test('publication facet renders the ACM paper with a GFM table (not raw pipes)', async () => {
  freshState(); installFetch();
  const cand = await import('../js/variants/O/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await cand.render(host, ctx, router.parseRoute('#/O/gen/v0/publication'));
  const paper = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-paper');
  assertEqual(paper.length, 1, 'the K-grade paper renderer');
  const tables = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('vo-md-table'));
  assert(tables.length >= 1, 'the GFM table rendered as a real <table>');
  assert(host.textContent.includes('Improving the presentation agent'), 'the title rendered');
  assert(host.textContent.includes('aggregate loss'), 'the table header cells rendered');
  // the markdown table should NOT survive as raw pipe text.
  assert(!host.textContent.includes('| --- |'), 'no raw markdown table separator leaked');
});

// ---- COLD deep-link hydrates the selection (run transcript paints) ---

test('run view: a COLD deep-link hydrates the run_id and paints the transcript', async () => {
  freshState(); installFetch();
  const runView = await import('../js/variants/O/views/run.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await runView.render(host, ctx, router.parseRoute('#/O/gen/v1/run/waffles_single'));
  const scroller = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-transcript')[0];
  assert(scroller, 'the transcript scroll container rendered');
  const turns = scroller.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('vo-turn vo-turn-'));
  assertEqual(turns.length, 2, `transcript shows its turns (saw ${turns.length})`);
  assert(host.textContent.includes('Drafting an outline'), 'turn text rendered from /api/conversation');
  assert(host.textContent.includes('omitted the requested structure'), 'drift annotation rendered');
});

await run();
