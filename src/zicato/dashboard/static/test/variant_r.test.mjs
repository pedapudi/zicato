// test/variant_r.test.mjs — Variant R ("Strata") unit tests.
//
// R is the round-5 convergence-III dashboard navigated as MILLER COLUMNS
// (solarized-dark + Display default). These pin the column cascade + the
// seven mandatory fixes:
//   * miller columns render (environment → epoch sections → items → detail) and
//     a column selection drives the next;
//   * navigating to a SECOND generation works (N's gap);
//   * the promote gate is on the candidate detail (stacked, no overlap);
//   * the patch node → per-candidate side-by-side diff (REAL strings);
//   * ALL match-ups for a candidate (v0 ≥ 2);
//   * the board column → per-board detail + INLINE side-by-side transcript;
//   * the trellis lives in the board detail, NOT the epoch overview;
//   * the colour + typeface pickers switch + persist;
//   * digest-gated repaint is a true no-op.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/R/router.js');
const svg = await import('../js/variants/R/svg.js');
const ui = await import('../js/variants/R/ui.js');
const shell = await import('../js/variants/R/shell.js');
const data = await import('../js/variants/R/data.js');
const columns = await import('../js/variants/R/columns.js');

const EPOCH_ID = '2026-05-30_e0';

const ANALYSIS_MD = [
  '<!-- EYEBROW -->',
  'Zicato improvement campaign · epoch analysis report',
  '',
  '# Presentation agent · epoch e0',
  '',
  '<!-- META -->',
  '**Epoch id**: `2026-05-30_e0`  ',
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
  '',
  '<!-- FIGURE:aggregate-scores -->',
].join('\n');

const FIXTURE = {
  '/api/epoch': {
    epoch_id: EPOCH_ID, closed: false, goal: 'Make the presentation agent crisper.', brief: 'Tighten structure.',
    experiments: [
      { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected' } },
      { generation_id: 'v2', parent_generation_id: 'v0', outcome: { decision: 'rejected' } },
    ],
    board: [
      { id: 'waffles_single', kind: 'single_turn', input_preview: 'Make a presentation about waffles.', budget_s: 180, weight: 1 },
      { id: 'picky_stakeholder_emulated', kind: 'multi_turn_emulated', budget_s: 360, weight: 1 },
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
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 60.5, delta: 0, verdict: 'flat' },
  { entry_id: 'picky_stakeholder_emulated', parent_drift_loss: 105.5, child_drift_loss: 642.5, delta: 537, verdict: 'regressed' },
] };
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v2`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v2', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 61.0, delta: 0.5, verdict: 'regressed' },
] };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`] = { decision: 'rejected', delta_scalar: 75.71, delta_pass_rate: 0,
  reason: 'challenger regressed: loss rose by 75.71', rules: [
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 146.65 (+75.71)' },
    { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'not_reached', fired: false },
    { id: 'namespace_monotonicity', label: 'Namespace monotonicity', status: 'not_reached', fired: false },
  ],
  scalar_components: { champion: { drift: 68.5, schema: 1.43 }, challenger: { drift: 145.64, schema: 0.0 } },
  primary_driver: { judge: 'incorporates_feedback', delta: 24.0 } };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', delta_scalar: 1.51, reason: 'regressed', rules: [
  { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 72.45' },
] };
FIXTURE['/api/conversation/run_v0_waffles'] = {
  turns: [{ seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' }, { seq: 1, role: 'agent', agent: 'coordinator', text: 'Champion outline.' }],
  annotations: [],
};
FIXTURE['/api/conversation/run_v1_waffles'] = {
  turns: [{ seq: 0, role: 'user', agent: 'operator', text: 'Make a presentation about waffles.' }, { seq: 1, role: 'agent', agent: 'coordinator', text: 'Challenger outline now.', tool_calls: [{ name: 'write_slide' }] }],
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
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
async function loadModel(path) {
  installFetch();
  const [ws, ep, lin] = await Promise.all([data.workspace(), data.epoch(), data.lineage()]);
  return columns.deriveModel(path, ep, lin, ws);
}
const ctxFor = (sink) => ({ navigate: (p) => { sink.nav = p; }, href: router.href });

// ---- router: the Miller column path round-trips --------------------

test('router: the column path parses + re-encodes (cold deep-link reconstructs)', () => {
  assertEqual(router.parsePath('').epoch, undefined);
  assertEqual(router.parsePath('#/R/').epoch, undefined);
  const p1 = router.parsePath('#/R/2026-05-30_e0/generations/v1/patch/coordinator_prompt');
  assertEqual(p1.epoch, '2026-05-30_e0');
  assertEqual(p1.section, 'generations');
  assertEqual(p1.gen, 'v1');
  assertEqual(p1.mutationId, 'coordinator_prompt');
  assertEqual(router.href(p1), '#/R/2026-05-30_e0/generations/v1/patch/coordinator_prompt');
  const p2 = router.parsePath('#/R/2026-05-30_e0/boards/waffles_single/run/v1');
  assertEqual(p2.section, 'boards');
  assertEqual(p2.entry, 'waffles_single');
  assertEqual(p2.runGen, 'v1');
  assertEqual(router.detailKind(p2), 'board');
  assertEqual(router.detailKind(router.parsePath('#/R/2026-05-30_e0/mutations')), 'mutations');
  assertEqual(router.detailKind(router.parsePath('#/R/2026-05-30_e0/publication')), 'publication');
});

// ---- the Miller columns cascade ------------------------------------

test('columns: environment → epoch sections → items render, and a selection drives the next', async () => {
  freshState();
  // col1: environment lists the epoch.
  const c1 = document.createElement('div'); const sink1 = {};
  let model = await loadModel(router.parsePath('#/R/'));
  columns.renderEpochColumn(c1, ctxFor(sink1), model);
  assert(allByClass(c1, 'dr-col-row').length >= 1, 'col1 lists at least one epoch');
  assert(c1.textContent.includes(EPOCH_ID), 'the epoch id appears in col1');
  // select the epoch → col2 shows the four sections.
  allByClass(c1, 'dr-col-row')[0].dispatchEvent({ type: 'click' });
  assert(sink1.nav && sink1.nav.epoch === EPOCH_ID, 'clicking the epoch row navigates with that epoch');

  const c2 = document.createElement('div'); const sink2 = {};
  model = await loadModel(router.parsePath('#/R/' + EPOCH_ID));
  columns.renderSectionColumn(c2, ctxFor(sink2), model);
  const secRows = allByClass(c2, 'dr-col-row');
  assertEqual(secRows.length, 4, 'col2 has the four sections');
  assert(c2.textContent.includes('Generations') && c2.textContent.includes('Boards') && c2.textContent.includes('Mutation surface') && c2.textContent.includes('Publication'), 'all four sections named');
  // selecting Generations drives col3.
  secRows[0].dispatchEvent({ type: 'click' });
  assert(sink2.nav && sink2.nav.section === 'generations', 'section click drives the next column');

  const c3 = document.createElement('div'); const sink3 = {};
  model = await loadModel(router.parsePath('#/R/' + EPOCH_ID + '/generations'));
  columns.renderItemColumn(c3, ctxFor(sink3), model);
  const genRows = allByClass(c3, 'dr-col-row');
  assertEqual(genRows.length, 3, 'col3 lists the three generations');
  genRows[1].dispatchEvent({ type: 'click' });
  assert(sink3.nav && sink3.nav.gen, 'a generation row click drives the detail pane');
});

test('columns: the Boards section drives a board items column', async () => {
  freshState();
  const c3 = document.createElement('div'); const sink = {};
  const model = await loadModel(router.parsePath('#/R/' + EPOCH_ID + '/boards'));
  columns.renderItemColumn(c3, ctxFor(sink), model);
  const rows = allByClass(c3, 'dr-col-row');
  assertEqual(rows.length, 2, 'col3 lists the two board entries');
  assert(c3.textContent.includes('waffles_single'), 'a board entry appears');
  rows[0].dispatchEvent({ type: 'click' });
  assert(sink.nav && sink.nav.section === 'boards' && sink.nav.entry, 'a board row drives the per-board detail');
});

// ---- navigating to a SECOND generation (N's gap) -------------------

test('candidate detail: navigating to a SECOND generation works', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/R/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epoch: EPOCH_ID, section: 'generations', gen: 'v1' });
  assert(host.textContent.includes('Candidate v1'), 'v1 candidate detail rendered');
  // now switch to v2 — the same host repaints to the OTHER candidate.
  freshState(); installFetch();
  await candidate.render(host, ctx, { epoch: EPOCH_ID, section: 'generations', gen: 'v2' });
  assert(host.textContent.includes('Candidate v2'), 'v2 candidate detail rendered after switching');
  assert(!host.textContent.includes('Candidate v1'), 'the v1 content was replaced');
});

// ---- FIX #1: promote gate on the candidate detail ------------------

test('candidate detail: the promote gate is on the candidate, stacked, no overlap', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/R/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epoch: EPOCH_ID, section: 'generations', gen: 'v1' });
  const gate = allByClass(host, 'dr-gate')[0];
  assert(gate, 'a promote-gate panel rendered on the candidate detail');
  const rules = allByClass(host, 'dr-rule');
  assert(rules.length >= 3, 'each gate rule is its own row (3 short-circuiting rules)');
  assert(allByClass(host, 'dr-sc-table').length >= 1, 'a separate champion-vs-challenger scalar-components block');
  assert(host.textContent.includes('Scalar margin'), 'a rule label present');
});

// ---- FIX #2: patch node → per-candidate side-by-side diff ----------

test('candidate detail: the patch site drills into a per-candidate side-by-side diff (REAL strings)', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/R/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epoch: EPOCH_ID, section: 'generations', gen: 'v1', mutationId: 'coordinator_prompt' });
  const sxs = allByClass(host, 'dr-sxs')[0];
  assert(sxs, 'the per-candidate side-by-side diff filled');
  assert(host.textContent.includes('Always emit an explicit slide structure'), 'challenger new_content (from /patches) on the right');
  assert(host.textContent.includes('Draft an outline'), 'baseline.content (from /mutations/{id}) on the left');
  assert(!host.textContent.includes('[object Object]'), 'never the baseline OBJECT');
  // the patch rail offers this candidate's sites.
  assert(allByClass(host, 'dr-patch-chip').length >= 1, 'a patch-sites rail lists the candidate sites');
});

test('sideBySideDiff: two columns of real strings (NOT "[object Object]")', () => {
  const mark = svg.sideBySideDiff({ baseline: 'a\nb', challenger: 'a\nc', leftLabel: 'L', rightLabel: 'R' });
  assert(mark.textContent.includes('c'), 'new string rendered');
  assert(!mark.textContent.includes('[object Object]'), 'no object stringification');
  assertEqual(allByClass(mark, 'dr-sxs-col-h').length, 2, 'two side-by-side columns');
});

// ---- FIX #3: ALL match-ups for a candidate -------------------------

test('candidate detail: ALL match-ups for v0 (champion||challenger) — v0-vs-v1 AND v0-vs-v2', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/R/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epoch: EPOCH_ID, section: 'generations', gen: 'v0' });
  const cells = allByClass(host, 'dr-pslope-cell');
  assert(cells.length >= 2, 'v0 shows BOTH rounds it was in (≥2), not one');
  assert(host.textContent.includes('v0 → v1') && host.textContent.includes('v0 → v2'), 'both v0-vs-v1 and v0-vs-v2 present');
});

// ---- FIX #4 + #5: board detail + INLINE side-by-side transcript ----

test('board detail: per-board cross-candidate; selecting a run renders the INLINE side-by-side transcript', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/R/views/board.js');
  const host = document.createElement('div');
  // without a run selected — the per-board view, no transcript yet.
  await board.render(host, { navigate() {}, href: router.href }, { epoch: EPOCH_ID, section: 'boards', entry: 'waffles_single' });
  assert(host.textContent.includes('Board · waffles_single'), 'the per-board heading');
  assert(allByClass(host, 'dr-valdot').length === 1, 'a per-candidate comparative dot-plot');
  assert(host.textContent.includes('v0') && host.textContent.includes('v1') && host.textContent.includes('v2'), 'all candidates listed');
  assertEqual(allByClass(host, 'dr-xscript-cols').length, 0, 'no transcript until a run is selected');
  // select v1's run on this board → INLINE two-column transcript, no navigation.
  freshState(); installFetch();
  await board.render(host, { navigate() {}, href: router.href }, { epoch: EPOCH_ID, section: 'boards', entry: 'waffles_single', runGen: 'v1' });
  const cols = allByClass(host, 'dr-xscript-cols')[0];
  assert(cols, 'the inline side-by-side transcript rendered IN this pane');
  const transcripts = allByClass(host, 'dr-transcript');
  assert(transcripts.length === 2, 'TWO transcripts side by side (champion + selected candidate)');
  assert(host.textContent.includes('Challenger outline now'), 'the selected candidate transcript rendered');
  assert(host.textContent.includes('Champion outline'), 'the champion transcript rendered alongside');
});

// ---- FIX #6: trellis in the board detail, NOT the epoch overview ---

test('de-dup: the heatmap is at the epoch overview; the trellis is in the board detail (never both on one page)', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/R/views/epoch.js');
  const board = await import('../js/variants/R/views/board.js');
  // epoch overview: heatmap present, trellis ABSENT.
  const ehost = document.createElement('div');
  await epoch.render(ehost, { navigate() {}, href: router.href }, { epoch: EPOCH_ID });
  assert(allByClass(ehost, 'dr-heatmap').length >= 1, 'the epoch overview carries the heatmap');
  assertEqual(allByClass(ehost, 'dr-trellis-cell').length, 0, 'the epoch overview does NOT carry the trellis');
  // board detail: trellis present, heatmap ABSENT.
  const bhost = document.createElement('div');
  await board.render(bhost, { navigate() {}, href: router.href }, { epoch: EPOCH_ID, section: 'boards', entry: 'waffles_single' });
  assert(allByClass(bhost, 'dr-trellis-cell').length >= 1, 'the board detail carries the trellis');
  assertEqual(allByClass(bhost, 'dr-heatmap').length, 0, 'the board detail does NOT carry the heatmap');
});

test('epoch overview heatmap cell routes to the per-board view (by entry id)', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/R/views/epoch.js');
  const host = document.createElement('div');
  let nav = null;
  await epoch.render(host, { navigate: (p) => { nav = p; }, href: router.href }, { epoch: EPOCH_ID });
  const heat = allByClass(host, 'dr-heatmap')[0];
  const cell = heat.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dr-hm-cell') && !(n.getAttribute('class') || '').includes('dr-hm-empty'))[0];
  assert(cell, 'a valued heatmap cell exists');
  cell.dispatchEvent({ type: 'click' });
  assert(nav && nav.section === 'boards' && nav.entry, 'heatmap cell routes to the per-board view keyed by entry id');
});

// ---- mutations + publication detail views --------------------------

test('mutations detail: the matrix + side-by-side diff fills on cell-select', async () => {
  freshState(); installFetch();
  const mutations = await import('../js/variants/R/views/mutations.js');
  const host = document.createElement('div');
  await mutations.render(host, { navigate() {}, href: router.href }, { epoch: EPOCH_ID, section: 'mutations' });
  assert(allByClass(host, 'dr-mtxsvg')[0], 'the mutation matrix rendered');
  // deep-link to a site → the diff fills (gen defaulted to the first patcher).
  freshState(); installFetch();
  await mutations.render(host, { navigate() {}, href: router.href }, { epoch: EPOCH_ID, section: 'mutations', mutationId: 'coordinator_prompt' });
  assert(allByClass(host, 'dr-sxs')[0], 'the side-by-side diff filled on select');
  assert(host.textContent.includes('Always emit an explicit slide structure'), 'challenger string on the right');
});

test('publication detail: renders from analysis_md and a GFM table renders', async () => {
  freshState(); installFetch();
  const pub = await import('../js/variants/R/views/publication.js');
  const host = document.createElement('div');
  await pub.render(host, { navigate() {}, href: router.href }, { epoch: EPOCH_ID, section: 'publication' });
  assert(allByClass(host, 'dr-paper')[0], 'the publication article rendered');
  assert(host.textContent.includes('Presentation agent · epoch e0'), 'the title typeset');
  const realTables = host.querySelectorAll('[class]').filter((n) => n.localName === 'table');
  assert(realTables.length >= 1, 'at least one real <table> (GFM table rendered)');
  assert(!host.textContent.includes('| generation | scalar |'), 'the table row is NOT left as raw markdown pipes');
});

// ---- sankey label ≠ value (Tufte discipline) -----------------------

test('sankey: the per-board loss VALUE is a distinct mark from the label', () => {
  const mark = svg.sankey({
    width: 760, colHeight: 220, nodeW: 150,
    patch: [{ id: 'patch', label: 'patch v0→v1', value: 703 }],
    drift: [{ id: 'd_picky', label: 'picky_stakeholder_emulated', value: 642.5, cls: 'dr-bad' }, { id: 'd_waffles', label: 'waffles_single', value: 60.5 }],
    gate: [{ id: 'gate', label: '✕ rejected', value: 703, cls: 'dr-bad' }], links: [],
  });
  assert(mark.getAttribute('viewBox'), 'fit-to-width responsive viewBox');
  const values = allByClass(mark, 'dr-sankey-value');
  assert(values.length >= 2, 'each drift node carries its loss value as its OWN element');
  const labels = allByClass(mark, 'dr-sankey-label');
  assert(labels.some((l) => l.textContent.includes('picky')), 'the label keeps the (truncated) board id');
  assert(values.some((v) => v.textContent.replace(/\D/g, '') === '643' || v.textContent.replace(/\D/g, '') === '642'), 'the value is the numeric loss, separate from the label');
});

// ---- heatmap is theme-token-driven ---------------------------------

test('heatmap: cells are theme-token ink at value-driven opacity (no fixed hex ramp)', () => {
  const mark = svg.heatmap({ rows: [{ id: 'a', label: 'x' }, { id: 'b', label: 'y' }], cols: [{ id: 'v0', label: 'v0' }, { id: 'v1', label: 'v1' }], value: (r, c) => (r === 'b' && c === 'v1' ? 642.5 : 60.5) });
  const cells = allByClass(mark, 'dr-hm-cell').filter((n) => !(n.getAttribute('class') || '').includes('dr-hm-empty'));
  assert(cells.length >= 1, 'valued cells exist');
  for (const c of cells) {
    const fill = c.getAttribute('fill');
    assert(fill == null || !/^#|rgb\(/.test(String(fill)), 'no fixed hex/rgb fill — token-driven');
    assert(c.getAttribute('fill-opacity') != null, 'value-driven opacity present');
  }
});

// ---- pickers + pills -----------------------------------------------

test('pickers: typeface (Display default) + colour (solarized-dark default) switch + persist', () => {
  freshState();
  const root = document.createElement('div');
  assertEqual(ui.DEFAULT_COLOR, 'solarized-dark', 'solarized-dark is the default colour theme');
  assertEqual(ui.DEFAULT_TYPE, 'display', 'Display is the default typeface');
  const typeIds = ui.TYPE_THEMES.map((t) => t[0]);
  assert(['sans', 'editorial', 'technical', 'display'].every((t) => typeIds.includes(t)), 'all four typefaces offered');
  shell.applyTheme('monokai', root);
  assertEqual(root.getAttribute('data-r-theme'), 'monokai', 'colour applied to root');
  assertEqual(ui.readColor(), 'monokai', 'colour persisted');
  shell.applyTypeface('editorial', root);
  assertEqual(root.getAttribute('data-r-type'), 'editorial', 'typeface applied to root');
  assertEqual(ui.readType(), 'editorial', 'typeface persisted');
  assertEqual(ui.normaliseColor('nonsense'), 'solarized-dark', 'unknown colour → solarized-dark');
  assertEqual(ui.normaliseType('nonsense'), 'display', 'unknown typeface → display');
  // verdict pills.
  const pill = ui.verdictPill('promoted');
  assert((pill.getAttribute('class') || '').includes('dr-promoted'), 'a promoted verdict pill');
});

// ---- digest-gated repaint (no-op) ----------------------------------

test('column digest: identical data does NOT rebuild the column DOM', async () => {
  freshState();
  const host = document.createElement('div');
  const model = await loadModel(router.parsePath('#/R/' + EPOCH_ID + '/generations'));
  columns.renderItemColumn(host, ctxFor({}), model);
  const digest1 = host.getAttribute('data-r-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'the column painted');
  columns.renderItemColumn(host, ctxFor({}), model);
  assertEqual(host.getAttribute('data-r-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

test('detail digest: an identical candidate detail repaint is a true no-op', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/variants/R/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epoch: EPOCH_ID, section: 'generations', gen: 'v1' });
  const digest1 = host.getAttribute('data-r-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  await candidate.render(host, ctx, { epoch: EPOCH_ID, section: 'generations', gen: 'v1' });
  assertEqual(host.getAttribute('data-r-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === first, 'no rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

await run();
