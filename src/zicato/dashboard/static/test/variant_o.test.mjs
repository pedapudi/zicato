// test/variant_o.test.mjs — Variant O ("Compass") unit tests.
//
// Compass is a master-detail two-pane workspace, scoped by LEVEL:
//   WORKSPACE (all epochs) → EPOCH → GENERATION → BOARD ENTRY.
// These tests pin the corrected IA:
//   * the router (typed selection incl. the new epoch level; deep-link
//     round-trip; the epoch mutations site/gen slots);
//   * default #/O/ renders an ALL-EPOCHS workspace overview (not a single
//     epoch); the rail lists epoch(s) at the top;
//   * selecting an EPOCH shows the publication AND the mutation surface at
//     EPOCH scope;
//   * a GENERATION's facet list does NOT include publication
//     (lifecycle/matchups/run only); the candidate-centric match-ups +
//     lifecycle still render;
//   * a board entry still routes to the per-board cross-candidate view by
//     entry id (never an arbitrary candidate);
//   * the side-by-side mutation diff renders REAL strings (NOT "[object
//     Object]"): baseline = /api/mutations/{e}/{mid}.baseline.content,
//     challenger = the matching patch's .new_content;
//   * the promote gate stacks (rules ladder + separate scalar-components);
//   * the sankey's per-board label and loss VALUE are distinct nodes;
//   * the heatmap accepts a theme-derived ramp;
//   * the typeface picker UPDATES the active pill on click (aria-pressed +
//     vo-active move to the clicked button) AND applies the font; the color
//     picker does the same;
//   * a digest-gated repaint is a no-op (a heartbeat-only change rebuilds 0);
//   * a COLD deep-link hydrates the selection (run transcript paints).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/O/router.js');
const ui = await import('../js/variants/O/ui.js');
const svg = await import('../js/variants/O/svg.js');
const data = await import('../js/variants/O/data.js');
const dom = await import('../js/core/dom.js');

const EPOCH_ID = '2026-05-30_e0';

const FIXTURE = {
  '/api/environment': { epoch: { epoch_id: EPOCH_ID } },
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
  '/api/score-trajectory': { points: [] },
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

test('router: default #/O/ is the WORKSPACE (all-epochs), not a single epoch', () => {
  const w = router.parseRoute('#/O/');
  assertEqual(w.view, 'workspace');
  assertEqual(w.kind, 'workspace');
});

test('router: parses the EPOCH level + its facets', () => {
  const e = router.parseRoute(`#/O/epoch/${EPOCH_ID}`);
  assertEqual(e.view, 'epoch'); assertEqual(e.kind, 'epoch'); assertEqual(e.epoch, EPOCH_ID); assertEqual(e.facet, 'overview');
  const pub = router.parseRoute(`#/O/epoch/${EPOCH_ID}/publication`);
  assertEqual(pub.facet, 'publication');
  const mut = router.parseRoute(`#/O/epoch/${EPOCH_ID}/mutations/coordinator_prompt/v1`);
  assertEqual(mut.facet, 'mutations'); assertEqual(mut.entry, 'coordinator_prompt'); assertEqual(mut.gen, 'v1');
});

test('router: a GENERATION has only candidate-centric facets (NO publication, NO mutations)', () => {
  assert(!router.FACETS.includes('publication'), 'publication is NOT a generation facet');
  assert(!router.FACETS.includes('mutations'), 'the full mutation surface is NOT a generation facet');
  // an unknown facet (e.g. the removed publication) degrades to lifecycle.
  const g = router.parseRoute('#/O/gen/v1/publication');
  assertEqual(g.view, 'gen'); assertEqual(g.facet, 'lifecycle');
  const lc = router.parseRoute('#/O/gen/v1/lifecycle');
  assertEqual(lc.facet, 'lifecycle');
  const mu = router.parseRoute('#/O/gen/v1/matchups');
  assertEqual(mu.facet, 'matchups');
});

test('router: parses typed board + run selections', () => {
  const b = router.parseRoute('#/O/board/waffles_single');
  assertEqual(b.view, 'board'); assertEqual(b.kind, 'board'); assertEqual(b.entry, 'waffles_single');
  const r = router.parseRoute('#/O/gen/v1/run/waffles_single');
  assertEqual(r.view, 'run'); assertEqual(r.gen, 'v1'); assertEqual(r.entry, 'waffles_single');
});

test('router: a foreign / empty hash defaults to the workspace', () => {
  assertEqual(router.parseRoute('').view, 'workspace');
  assertEqual(router.parseRoute('#/something').view, 'workspace');
});

test('router: href round-trips epoch + gen + board selections', () => {
  assertEqual(router.parseRoute(router.href('board', { entry: 'q3' })).entry, 'q3');
  const back = router.parseRoute(router.href('gen', { gen: 'v2', facet: 'matchups' }));
  assertEqual(back.gen, 'v2'); assertEqual(back.facet, 'matchups');
  const ep = router.parseRoute(router.href('epoch', { epoch: EPOCH_ID, facet: 'publication' }));
  assertEqual(ep.epoch, EPOCH_ID); assertEqual(ep.facet, 'publication');
  const site = router.parseRoute(router.href('epoch', { epoch: EPOCH_ID, facet: 'mutations', entry: 'planner_prompt', gen: 'v2' }));
  assertEqual(site.entry, 'planner_prompt'); assertEqual(site.gen, 'v2');
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
  const first = rules.find((r) => (r.getAttribute('class') || '').includes('vo-rule-fail'));
  assert(first, 'the fired rule row exists');
  assert(first.textContent.includes('Scalar margin'), 'rule label present');
  assert(first.textContent.includes('fail'), 'rule status present');
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

// ---- typeface picker: clicking UPDATES the active pill ---------------

test('typeface picker: clicking an option moves the active pill (aria-pressed + class) AND applies the font', () => {
  freshState();
  const root = document.createElement('div');
  let pickedFace = null;
  // start active on "display" (O's default).
  const tf = ui.typefaceSwitcher('display', (f) => { pickedFace = f; ui.applyTypeface(root, f); });
  const btn = (id) => tf.querySelectorAll('[data-type]').filter((b) => b.getAttribute('data-type') === id)[0];
  // before: display is active, technical is not.
  assert(btn('display').getAttribute('aria-pressed') === 'true', 'display starts pressed');
  assert(!(btn('technical').getAttribute('class') || '').includes('vo-active'), 'technical starts inactive');
  // click technical.
  btn('technical').dispatchEvent({ type: 'click' });
  assertEqual(pickedFace, 'technical', 'onPick fired with the chosen id');
  assertEqual(root.getAttribute('data-vo-type'), 'technical', 'the font was applied');
  // AFTER the click the active pill MOVED to technical (the bug: it stayed).
  assert((btn('technical').getAttribute('class') || '').includes('vo-active'), 'technical is now active');
  assertEqual(btn('technical').getAttribute('aria-pressed'), 'true', 'technical aria-pressed=true');
  assert(!(btn('display').getAttribute('class') || '').includes('vo-active'), 'display is no longer active');
  assertEqual(btn('display').getAttribute('aria-pressed'), 'false', 'display aria-pressed=false');
});

test('color picker: clicking an option moves the active pill (mirrors the typeface picker)', () => {
  freshState();
  let pickedTheme = null;
  const cs = ui.themeSwitcher('solarized-dark', (t) => { pickedTheme = t; });
  const btn = (id) => cs.querySelectorAll('[data-theme]').filter((b) => b.getAttribute('data-theme') === id)[0];
  btn('solarized-light').dispatchEvent({ type: 'click' });
  assertEqual(pickedTheme, 'solarized-light');
  assert((btn('solarized-light').getAttribute('class') || '').includes('vo-active'), 'light is now active');
  assertEqual(btn('solarized-light').getAttribute('aria-pressed'), 'true');
  assert(!(btn('solarized-dark').getAttribute('class') || '').includes('vo-active'), 'dark is no longer active');
});

test('pickers persist the chosen value on the root', () => {
  freshState();
  const root = document.createElement('div');
  assertEqual(ui.applyTypeface(root, 'editorial'), 'editorial');
  assertEqual(root.getAttribute('data-vo-type'), 'editorial');
  assertEqual(ui.readTypeface(), 'editorial', 'persisted');
  assertEqual(ui.normaliseTypeface('bogus'), 'display', 'O defaults to display');
  assertEqual(ui.applyTheme(root, 'monokai'), 'monokai');
  assertEqual(root.getAttribute('data-vo-theme'), 'monokai');
  assertEqual(ui.readTheme(), 'monokai', 'persisted');
});

// ---- the rail: ALL-EPOCHS at the top + a digest-gated no-op ----------

test('rail lists EPOCHS at the top; expands the selected epoch to gens + board; identical data is a no-op', async () => {
  freshState(); installFetch();
  const { loadRailModel, loadWorkspaceModel } = await import('../js/variants/O/model.js');
  const rail = await import('../js/variants/O/rail.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  const ws = await loadWorkspaceModel();
  const re = await loadRailModel(EPOCH_ID);
  const model = { epochs: ws.epochs, selectedEpochId: EPOCH_ID, gens: re.gens, board: re.board, selection: router.parseRoute(`#/O/epoch/${EPOCH_ID}`) };
  rail.renderRail(host, ctx, model);
  // the WORKSPACE root + an epoch row.
  const wsRows = host.querySelectorAll('[class]').filter((n) => {
    const c = n.getAttribute('class') || '';
    return c.split(/\s+/).includes('vo-rail-workspace');
  });
  assertEqual(wsRows.length, 1, 'a workspace root row');
  const epochs = host.querySelectorAll('[data-epoch]');
  assert(epochs.length >= 1, 'a rail row per epoch');
  // under the selected epoch, generations + board expand.
  const gens = host.querySelectorAll('[data-gen]').filter((n) => (n.getAttribute('class') || '').includes('vo-rail-gen'));
  const boards = host.querySelectorAll('[data-entry]');
  assertEqual(gens.length, 3, 'a rail row per generation under the expanded epoch');
  assertEqual(boards.length, 2, 'a rail row per board entry under the expanded epoch');
  const first = host.firstChild;
  rail.renderRail(host, ctx, model);
  assert(host.firstChild === first, 'identical data → no rebuild');
});

// ---- WORKSPACE detail: all-epochs overview ---------------------------

test('default #/O/ renders an ALL-EPOCHS workspace overview (not a single-epoch view)', async () => {
  freshState(); installFetch();
  const workspace = await import('../js/variants/O/views/workspace.js');
  const host = document.createElement('div');
  let routedTo = null;
  const ctx = { navigate: (v, p) => { routedTo = { v, p }; }, href: router.href };
  await workspace.render(host, ctx, router.parseRoute('#/O/'));
  assert(host.textContent.includes('Workspace'), 'the workspace heading');
  assert(host.textContent.toLowerCase().includes('every epoch'), 'the all-epochs lede');
  // one epoch card (live data has a single epoch — degrades gracefully).
  const cards = host.querySelectorAll('[data-epoch]');
  assertEqual(cards.length, 1, 'an epoch card per epoch (one in the live data)');
  // the lineage bumps render inside the card.
  const bumps = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-bumps');
  assert(bumps.length >= 1, 'lineage bumps present');
  // clicking the card body opens the EPOCH.
  cards[0].dispatchEvent({ type: 'click', target: cards[0] });
  assertEqual(routedTo.v, 'epoch');
  assertEqual(routedTo.p.epoch, EPOCH_ID, 'the card selects that epoch');
  const firstChild = host.firstChild;
  await workspace.render(host, ctx, router.parseRoute('#/O/'));
  assert(host.firstChild === firstChild, 'identical data → no rebuild (heartbeat no-op)');
});

// ---- EPOCH detail: publication + mutation surface at EPOCH scope -----

test('selecting an EPOCH shows the publication at EPOCH scope (GFM table renders)', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/O/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epoch.render(host, ctx, router.parseRoute(`#/O/epoch/${EPOCH_ID}/publication`));
  const paper = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-paper');
  assertEqual(paper.length, 1, 'the K-grade paper renderer, at epoch scope');
  const tables = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('vo-md-table'));
  assert(tables.length >= 1, 'the GFM table rendered as a real <table>');
  assert(host.textContent.includes('Improving the presentation agent'), 'the title rendered');
  assert(host.textContent.includes('aggregate loss'), 'the table header cells rendered');
  assert(!host.textContent.includes('| --- |'), 'no raw markdown table separator leaked');
});

test('selecting an EPOCH shows the mutation surface (matrix + real-string side-by-side diff) at EPOCH scope', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/O/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  // the coordinator site, patched by v1, selected via the route slots.
  await epoch.render(host, ctx, router.parseRoute(`#/O/epoch/${EPOCH_ID}/mutations/coordinator_prompt/v1`));
  const matrix = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-mutmatrix');
  const diff = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-sxs');
  assertEqual(matrix.length, 1, 'the epoch-wide site × generation matrix');
  assertEqual(diff.length, 1, 'ONE side-by-side diff, in the same combined visual');
  assert(host.textContent.includes('EMIT an explicit slide outline first.'), 'challenger .new_content rendered');
  assert(host.textContent.includes('Always cite sources.'), 'baseline .content rendered');
  assert(!host.textContent.includes('[object Object]'), 'no [object Object] bug');
});

test('the epoch overview renders lineage + a per-board heatmap + the match-up summary', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/O/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epoch.render(host, ctx, router.parseRoute(`#/O/epoch/${EPOCH_ID}`));
  const bumps = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-bumps');
  const heat = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-heatmap');
  assert(bumps.length >= 1, 'lineage bumps in the epoch overview');
  assert(heat.length >= 1, 'a per-board drift heatmap');
  assert(host.textContent.includes('v0 → v1') || host.textContent.includes('v0 → v2'), 'the match-up summary lists rounds');
});

// ---- GENERATION detail: candidate-centric, NO publication facet ------

test('a GENERATION detail is candidate-centric: lifecycle/matchups tabs only (NO publication tab)', async () => {
  freshState(); installFetch();
  const cand = await import('../js/variants/O/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await cand.render(host, ctx, router.parseRoute('#/O/gen/v1/lifecycle'));
  const tabFacets = host.querySelectorAll('[data-facet]').map((b) => b.getAttribute('data-facet'));
  assert(tabFacets.includes('lifecycle'), 'lifecycle tab present');
  assert(tabFacets.includes('matchups'), 'matchups tab present');
  assert(!tabFacets.includes('publication'), 'NO publication tab on a generation');
  assert(!tabFacets.includes('mutations'), 'NO full mutation-surface tab on a generation');
});

test('generation lifecycle renders per-board scoring + sankey + gate, and links its OWN patch sites to the epoch mutation surface', async () => {
  freshState(); installFetch();
  const cand = await import('../js/variants/O/views/candidate.js');
  const host = document.createElement('div');
  let routedTo = null;
  const ctx = { navigate: (v, p) => { routedTo = { v, p }; }, href: router.href };
  await cand.render(host, ctx, router.parseRoute('#/O/gen/v1/lifecycle'));
  const dot = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-valdot');
  const sankey = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-sankey');
  const gate = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-gate');
  assertEqual(dot.length, 1, 'per-board value dot-plot');
  assertEqual(sankey.length, 1, 'the Tufte sankey');
  assertEqual(gate.length, 1, 'the promote gate panel');
  // the candidate's own patch site links into the EPOCH mutation surface.
  assert(host.textContent.includes('coordinator_prompt'), 'this generation’s patched site is surfaced');
  const siteRow = host.querySelectorAll('[data-site]').filter((n) => n.getAttribute('data-site') === 'coordinator_prompt')[0];
  assert(siteRow, 'the patch-site row exists');
  siteRow.dispatchEvent({ type: 'click' });
  assertEqual(routedTo.v, 'epoch', 'a patch site routes to the EPOCH-scoped surface');
  assertEqual(routedTo.p.facet, 'mutations');
  assertEqual(routedTo.p.entry, 'coordinator_prompt');
  assertEqual(routedTo.p.gen, 'v1', 'focused on THIS generation’s patch');
});

test('generation match-ups render the gauntlet ladder + the paired duel + the round gate', async () => {
  freshState(); installFetch();
  const cand = await import('../js/variants/O/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await cand.render(host, ctx, router.parseRoute('#/O/gen/v1/matchups'));
  const bumps = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-bumps');
  const slope = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-pslope');
  const gate = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-gate');
  assert(bumps.length >= 1, 'the gauntlet ladder bumps');
  assert(slope.length >= 1, 'the paired per-board slopegraph duel');
  assert(gate.length >= 1, 'the round gate');
});

// ---- BOARD entry: per-board cross-candidate view (UNCHANGED) ---------

test('a board entry routes to the per-board cross-candidate view BY ENTRY ID (not a candidate)', async () => {
  freshState(); installFetch();
  const boardView = await import('../js/variants/O/views/board.js');
  const host = document.createElement('div');
  let routedTo = null;
  const ctx = { navigate: (v, p) => { routedTo = { v, p }; }, href: router.href };
  await boardView.render(host, ctx, router.parseRoute('#/O/board/waffles_single'));
  assert(host.textContent.includes('waffles_single'), 'the board entry id is the subject');
  assert(host.textContent.includes('cross-candidate'), 'this is the cross-candidate view');
  const bars = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vo-sortbars');
  assertEqual(bars.length, 1, 'a sorted comparative chart');
  const sortRows = host.querySelectorAll('[data-vo]').filter((n) => n.getAttribute('data-vo') === 'sortrow');
  assertEqual(sortRows.length, 3, 'one row per candidate (v0/v1/v2), keyed by candidate');
  assert(host.textContent.includes('open run'), 'each candidate drills to its run for THIS board');
  const list = host.querySelectorAll('[data-gen]').filter((n) => (n.getAttribute('class') || '').includes('vo-runlist-item'));
  assert(list.length >= 1, 'run-list rows present');
  list[0].dispatchEvent({ type: 'click' });
  assertEqual(routedTo.v, 'run');
  assertEqual(routedTo.p.entry, 'waffles_single', 'routes to a RUN keyed by the board entry id');
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
