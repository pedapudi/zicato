// test/variant_m.test.mjs — Variant M ("Ledger II") unit tests.
//
// Ledger II is the round-4 convergence-II editorial skin built on Variant E's
// flow. These tests pin the seven mandatory convergence-II fixes + the new
// typeface picker, plus the carried-forward render discipline:
//   (1) the promote gate is laid out as clean STACKED sections (no overlap);
//   (2) the mutation view is ONE cohesive visual: matrix + SIDE-BY-SIDE diff
//       with REAL strings (champion baseline .baseline.content vs challenger
//       .new_content) — NEVER "[object Object]";
//   (3) the publication renders GFM tables (the "Aggregate generation scores"
//       table) via the reused K renderer;
//   (4) the heatmap ramp derives from active-theme tokens (no fixed ramp);
//   (5) the Tufte sankey's label and loss VALUE do not overlap (separate
//       right-anchored value text);
//   (6) proportional figure sizing (a shared figure envelope);
//   (7) the NEW per-board view + trellis/heatmap routing to it (by entry id);
//   + the typeface picker (and colour picker) switch + persist;
//   + digest no-op; cold deep-link transcript.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();
// jsdom-free harness has no getComputedStyle; svg.js falls back to baked-in
// ramp endpoints. We expose a stub so the heatmap-token test can assert the
// ramp READS the live token (and returns a colour derived from it).
let _themeTokens = {};
globalThis.getComputedStyle = () => ({
  getPropertyValue: (name) => (_themeTokens[name] != null ? _themeTokens[name] : ''),
});

const router = await import('../js/variants/M/router.js');
const ui = await import('../js/variants/M/ui.js');
const svg = await import('../js/variants/M/svg.js');
const sankey = await import('../js/variants/M/diagram/sankey.js');
const theme = await import('../js/variants/M/theme.js');
const typeface = await import('../js/variants/M/typeface.js');
const data = await import('../js/variants/M/data.js');
const dom = await import('../js/core/dom.js');

const EPOCH_ID = '2026-05-30_e0';
const FIXTURE = {
  '/api/workspace': {
    current_epoch_id: EPOCH_ID,
    epochs: [{ epoch_id: EPOCH_ID, goal: 'Improve the presentation agent.', best_scalar: 70.94, generation_count: 3, promoted_count: 1, closed: false }],
    sparkline: [{ scalar: 88.1 }, { scalar: 75.0 }, { scalar: 70.94 }],
  },
  '/api/health-report': { epoch_id: EPOCH_ID, healthy: true, findings: [] },
  '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 70.94 }, { generation_id: 'v1', scalar: 146.65 }, { generation_id: 'v2', scalar: 72.45 }] },
  '/api/lineage': { generations: [
    { generation_id: 'v0', epoch_id: EPOCH_ID, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
    { generation_id: 'v2', epoch_id: EPOCH_ID, parent_generation_id: 'v0', promoted: false },
  ] },
  '/api/epoch': {
    epoch_id: EPOCH_ID, closed: false, goal: 'Improve the presentation agent.', brief: '# Goal\nMake it crisper.\n\n- one\n- two',
    board: [
      { id: 'waffles_single', kind: 'single_turn', input_preview: 'Make a presentation about waffles.', expectation_kind: 'predicate', budget_s: 180, weight: 1, tags: ['smoke'] },
      { id: 'picky_stakeholder_emulated', kind: 'multi_turn_emulated', input_preview: null, expectation_kind: null, budget_s: 360, weight: 1, tags: ['hard'] },
    ],
    experiments: [
      { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'promoted', scalar_score: 70.94 } },
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected', scalar_score: 146.65 }, hypothesis: { core_idea: 'Enforce explicit slide-structure output.', mutation_points: ['m1', 'm2'] } },
      { generation_id: 'v2', parent_generation_id: 'v0', outcome: { decision: 'rejected', scalar_score: 72.45 }, hypothesis: { core_idea: 'Tighten the coordinator oversight.', mutation_points: ['m1'] } },
    ],
  },
  '/api/tournaments': { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71 },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51 },
  ] },
  [`/api/mutations/${EPOCH_ID}`]: {
    epoch_id: EPOCH_ID, generations: ['v0', 'v1', 'v2'],
    mutations: [
      { mutation_id: 'm1', kind: 'block', file: 'agent/coordinator.py', role: 'system_prompt', line_start: 12, line_end: 20, patched_generation_ids: ['v1', 'v2'] },
      { mutation_id: 'm2', kind: 'block', file: 'agent/writer.py', role: 'instructions', line_start: 30, line_end: 36, patched_generation_ids: ['v1'] },
    ],
  },
  // The convergence-II contract: baseline is an OBJECT whose `.content` is the
  // STRING. Rendering the object itself was the "[object Object]" bug.
  [`/api/mutations/${EPOCH_ID}/m1`]: {
    epoch_id: EPOCH_ID, mutation_id: 'm1',
    baseline: { content: 'be terse\nkeep it short', role: 'system_prompt' },
  },
  [`/api/epoch/${EPOCH_ID}/analysis`]: {
    epoch_id: EPOCH_ID,
    analysis_md: [
      '<!-- EYEBROW -->',
      'Zicato improvement campaign · epoch analysis report',
      '',
      '# Epoch ' + EPOCH_ID,
      '',
      '<!-- META -->',
      '**Epoch id**: `' + EPOCH_ID + '`  ',
      '**Status**: in progress  ',
      '**Generations**: 3 attempted · 1 promoted · 2 rejected',
      '',
      '## Abstract',
      '',
      'This epoch tested two challengers against the seed champion; both regressed.',
      '',
      '## Results',
      '',
      '### Aggregate generation scores',
      '',
      '| generation | scalar | role |',
      '| --- | --- | --- |',
      '| v0 | 70.94 | champion |',
      '| v1 | 146.65 | challenger |',
      '| v2 | 72.45 | challenger |',
      '',
      'Caption: Lineage of the epoch.',
      '<!-- FIGURE: lineage -->',
      '',
      '## Conclusion',
      '',
      'The champion stands.',
    ].join('\n'),
    analysis_html_inline: '', analysis_html_available: false,
  },
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
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v2_picky', drift_loss: 110.0, pass_fail: 0, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/files/${EPOCH_ID}/v0/patches`] = { epoch_id: EPOCH_ID, generation_id: 'v0', patches: [] };
FIXTURE[`/api/files/${EPOCH_ID}/v1/patches`] = { epoch_id: EPOCH_ID, generation_id: 'v1', patches: [
  { id: 'p1', mutation_id: 'm1', op: 'replace', new_content: 'be terse\nAND enforce slide structure', rationale: 'force a structured outline' },
  { id: 'p2', mutation_id: 'm2', op: 'replace', new_content: 'write tighter' },
] };
FIXTURE[`/api/files/${EPOCH_ID}/v2/patches`] = { epoch_id: EPOCH_ID, generation_id: 'v2', patches: [
  { id: 'p3', mutation_id: 'm1', op: 'replace', new_content: 'be terse\noversee the writer' },
] };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`] = {
  decision: 'rejected', reason: 'challenger regressed: loss rose by 75.71', delta_scalar: 75.71, delta_pass_rate: 0.0,
  rules: [
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', detail: '70.94 → 146.65 (+75.71; needs ≤ -0.01)', fired: true },
    { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'not_reached', fired: false },
    { id: 'namespace_monotonicity', label: 'Namespace monotonicity', status: 'not_reached', fired: false },
  ],
  scalar_components: {
    champion: { cost: 0.009, drift: 68.5, latency: 0.0, output: 0.0, pass: 1.0, rubric: -0.0, schema: 1.43 },
    challenger: { cost: 0.009, drift: 145.64, latency: 0.0, output: 0.0, pass: 1.0, rubric: -0.0, schema: 0.0 },
  },
  primary_driver: { judge: 'incorporates_feedback', delta: 24.0 },
};
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', delta_scalar: 1.51, delta_pass_rate: 0.0, reason: 'challenger regressed: loss rose by 1.51', rules: [], scalar_components: {} };
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v1`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v1', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 60.5, delta: 0, verdict: 'flat', won_by: null },
  { entry_id: 'picky_stakeholder_emulated', parent_drift_loss: 105.5, child_drift_loss: 642.5, delta: 537, verdict: 'regressed', won_by: 'v0' },
] };
FIXTURE[`/api/matchup-grid/${EPOCH_ID}/v0/v2`] = { epoch_id: EPOCH_ID, champion: 'v0', challenger: 'v2', entry_grid: [
  { entry_id: 'waffles_single', parent_drift_loss: 60.5, child_drift_loss: 61.0, delta: 0.5, verdict: 'regressed', won_by: 'v0' },
] };
FIXTURE[`/api/run/${EPOCH_ID}/v1/waffles_single/expectations`] = { outcomes: [{ kind: 'predicate', passed: false, detail: 'predicate returned False', judge_name: null, score: null }] };
FIXTURE[`/api/run/${EPOCH_ID}/v1/waffles_single/per-judge`] = { judges: [{ judge_name: 'incorporates_feedback', weighted_loss: 27.0, raw_loss: 27.0, run_count: 1, weight: 1.0 }] };
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
function allText(host) { return host.textContent; }

// ---- router (adds the per-board view) -------------------------------

test('router: parses Ledger-II hashes incl. mutations(+gen), board, paper', () => {
  assertEqual(router.parseRoute('#/M/').view, 'home');
  assertEqual(router.parseRoute('#/M/epoch').view, 'epoch');
  assertEqual(router.parseRoute('#/M/matchups').view, 'matchups');
  assertEqual(router.parseRoute('#/M/mutations').view, 'mutations');
  const md = router.parseRoute('#/M/mutations/m1/v1');
  assertEqual(md.params.mutationId, 'm1');
  assertEqual(md.params.gen, 'v1');
  const b = router.parseRoute('#/M/board/waffles_single');
  assertEqual(b.view, 'board');
  assertEqual(b.params.entry, 'waffles_single');
  assertEqual(router.parseRoute('#/M/paper').view, 'paper');
  assertEqual(router.parseRoute('#/E/epoch').view, 'home', 'foreign hash → home');
});

test('router: board + mutation hrefs round-trip', () => {
  const h = router.href('board', { entry: 'picky_stakeholder_emulated' });
  assertEqual(router.parseRoute(h).params.entry, 'picky_stakeholder_emulated');
  const m = router.href('mutations', { mutationId: 'm1', gen: 'v2' });
  const back = router.parseRoute(m);
  assertEqual(back.params.mutationId, 'm1');
  assertEqual(back.params.gen, 'v2');
});

// ---- digest no-op ---------------------------------------------------

test('gatedSwap rebuilds on a changed digest and no-ops on an identical one', () => {
  const host = document.createElement('div');
  let builds = 0;
  ui.gatedSwap(host, 'A', () => { builds += 1; return [dom.el('p', { text: 'one' })]; });
  ui.gatedSwap(host, 'A', () => { builds += 1; return [dom.el('p', { text: 'one' })]; });
  assertEqual(builds, 1, 'identical digest is a no-op');
  ui.gatedSwap(host, 'B', () => { builds += 1; return [dom.el('p', { text: 'two' })]; });
  assertEqual(builds, 2, 'a changed digest rebuilds');
});

test('home view: a re-render with identical data does NOT rebuild the DOM', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/M/views/home.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await home.render(host, ctx, {});
  const digest1 = host.getAttribute('data-m-digest');
  const writes1 = host.innerHTMLWriteCount();
  const firstChild = host.firstChild;
  assert(host.children.length > 0, 'home painted content');
  await home.render(host, ctx, {});
  assertEqual(host.getAttribute('data-m-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === firstChild, 'host not rebuilt (same node identity)');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// ---- (5) Tufte sankey: label ≠ value, fit-to-width ------------------

test('(5) sankey: the entry label and its loss value are separate, non-overlapping marks', () => {
  const node = sankey.buildTufteSankey({
    genId: 'v1',
    rows: [
      { entryId: 'picky_stakeholder_emulated', driftLoss: 642.5, passFail: 0, budgetExceeded: true, runId: 'r2' },
      { entryId: 'waffles_single', driftLoss: 60.5, passFail: 0, budgetExceeded: true, runId: 'r1' },
    ],
    onBoard() {},
  });
  assertEqual(node.getAttribute('class'), 'i-sankey');
  assert(node.getAttribute('viewBox'), 'responsive viewBox');
  assertEqual(node.style._props.width, '100%', 'fit-to-width');
  const labels = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'i-sankey-label');
  const values = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'i-sankey-value');
  assert(labels.length >= 2, 'each node carries a label mark');
  assert(values.length >= 2, 'each node carries a SEPARATE value mark');
  // label is left-anchored, value is right-anchored → they cannot overlap.
  const lbl = labels.find((n) => (n.childNodes[0] && n.childNodes[0].textContent || '').includes('picky'));
  const val = values.find((n) => (n.childNodes[0] && n.childNodes[0].textContent || '') === '642.5');
  assert(lbl && lbl.getAttribute('text-anchor') === 'start', 'label is left-anchored');
  assert(val && val.getAttribute('text-anchor') === 'end', 'value is right-anchored (no overlap)');
  // the label text does NOT carry the numeric loss glued onto it.
  assert(!String(lbl.childNodes[0].textContent).match(/\d{3}/), 'label text is not "…emu643…" — value is split out');
  // no pan/zoom viewport surface.
  const surfaces = node.querySelectorAll('[class]').filter((n) => /cz-surface|cz-viewport/.test(n.getAttribute('class') || ''));
  assertEqual(surfaces.length, 0, 'no pan/zoom viewport');
});

// ---- (4) heatmap theme-aware ramp -----------------------------------

test('(4) heatmap ramp derives from active-theme tokens (no fixed orange/brown)', () => {
  // Set distinct, recognizable tokens for the active theme and assert the
  // ramp colour is interpolated FROM them.
  _themeTokens = { '--m-ramp-lo': '#000000', '--m-ramp-hi': '#ffffff' };
  const hi = svg.rampColor(1, false);
  const lo = svg.rampColor(0, false);
  assertEqual(lo, 'rgb(0,0,0)', 'ramp low end reads --m-ramp-lo');
  assertEqual(hi, 'rgb(255,255,255)', 'ramp high end reads --m-ramp-hi');
  // a different theme's tokens produce a different ramp (theme-aware).
  _themeTokens = { '--m-ramp-lo': '#102030', '--m-ramp-hi': '#a0b0c0' };
  const mid = svg.rampColor(0.5, false);
  assert(mid === 'rgb(88,104,120)', `mid derives from the new tokens (got ${mid})`);
  _themeTokens = {};
});

// ---- (1) promote gate: stacked, no overlap --------------------------

test('(1) candidate view: the promote gate renders clean STACKED sections', async () => {
  freshState(); installFetch();
  const cand = await import('../js/variants/M/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await cand.render(host, ctx, { gen: 'v1' });
  const gate = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('m-gate'))[0];
  assert(gate, 'the stacked gate panel rendered');
  // (a) decision header with Δ scalar.
  assert(allText(host).includes('+75.71'), 'Δ scalar shown in the decision header');
  // (b) each rule is its OWN row, label · status · detail.
  const rules = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('m-gate-rule ') || (n.getAttribute('class') || '') === 'm-gate-rule m-gate-rule-fail m-gate-fired');
  const ruleRows = host.querySelectorAll('[class]').filter((n) => /(^| )m-gate-rule( |$)/.test(n.getAttribute('class') || ''));
  assert(ruleRows.length >= 3, `each rule its own row (saw ${ruleRows.length})`);
  assert(allText(host).includes('Scalar margin'), 'rule label rendered');
  assert(allText(host).includes('needs ≤ -0.01'), 'rule detail rendered');
  // (c) the SEPARATE scalar-components comparison block, both sides.
  const comp = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'm-gate-comp')[0];
  assert(comp, 'a separate scalar-components comparison block is present');
  assert(allText(host).includes('drift'), 'a scalar component (drift) is listed');
});

// ---- (2) mutation view: matrix + side-by-side REAL strings ----------

test('(2) mutation view: matrix + SIDE-BY-SIDE diff with REAL strings (not "[object Object]")', async () => {
  freshState(); installFetch();
  const mut = await import('../js/variants/M/views/mutations.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = [v, p]; }, href: router.href };
  // ONE cohesive visual: the matrix renders.
  await mut.render(host, ctx, {});
  const matrix = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-mutmatrix');
  assert(matrix.length === 1, 'the mutation-site × generation matrix rendered');
  const cells = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('d-mm-on'));
  assert(cells.length >= 3, `patched cells present (saw ${cells.length})`);
  // a cell click routes to the (site, gen) diff.
  cells[0].dispatchEvent({ type: 'click' });
  assert(navTo && navTo[0] === 'mutations' && navTo[1].mutationId && navTo[1].gen, 'a cell click selects (site, gen)');

  // drill into m1 vs v1 → the SIDE-BY-SIDE diff with the real strings.
  freshState();
  await mut.render(host, ctx, { mutationId: 'm1', gen: 'v1' });
  const sbs = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'm-sbs');
  assert(sbs.length === 1, 'the side-by-side diff rendered');
  const text = allText(host);
  assert(!text.includes('[object Object]'), 'NEVER renders "[object Object]"');
  assert(text.includes('be terse'), 'baseline STRING (.baseline.content) shown');
  assert(text.includes('keep it short'), 'full baseline string shown');
  assert(text.includes('enforce slide structure'), 'challenger new_content STRING shown');
  // two columns, with at least one removed + one added line.
  const base = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('m-sbs-base') && (n.getAttribute('class') || '').includes('m-sbs-cell'));
  const added = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('m-sbs-added'));
  assert(base.length >= 1, 'baseline column rows present');
  assert(added.length >= 1, 'an added (challenger) line is marked');
});

test('(2b) data.baselineContent reads .baseline.content (string), not the object', () => {
  assertEqual(data.baselineContent({ baseline: { content: 'hi there' } }), 'hi there');
  assertEqual(data.baselineContent({ baseline: { role: 'x' } }), null, 'no content string → null, never the object');
  assertEqual(data.baselineContent({ baseline_content: 'legacy' }), 'legacy', 'legacy top-level string supported');
});

// ---- (7) per-board view + trellis/heatmap route to it ---------------

test('(7) board view: per-board cross-candidate detail pivots every candidate', async () => {
  freshState(); installFetch();
  const board = await import('../js/variants/M/views/board.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await board.render(host, ctx, { entry: 'picky_stakeholder_emulated' });
  const text = allText(host);
  assert(text.includes('picky_stakeholder_emulated'), 'the entry id is the page subject');
  // every candidate appears with its loss on THIS entry.
  assert(text.includes('v0') && text.includes('v1') && text.includes('v2'), 'all three candidates listed');
  assert(text.includes('642.5'), 'v1 loss on this entry shown');
  assert(text.includes('105.5'), 'v0 (champion) loss on this entry shown');
  const dot = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-valdot');
  assert(dot.length === 1, 'a comparative dot-plot across candidates');
  const tbl = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'm-cand-table');
  assert(tbl.length === 1, 'a per-candidate run table with drill links');
});

test('(7b) epoch view: trellis cards AND heatmap cells route to the per-board view by entry id', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/M/views/epoch.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = [v, p]; }, href: router.href };
  await epoch.render(host, ctx, {});
  // a trellis card click → board, keyed by entry id (NOT candidate v2).
  const trellis = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-trellis')[0];
  const card = trellis.children[0];
  card.dispatchEvent({ type: 'click' });
  assert(navTo && navTo[0] === 'board' && navTo[1] && navTo[1].entry, `trellis routes to board by entry (got ${JSON.stringify(navTo)})`);
  // a heatmap cell click → board, keyed by entry id.
  navTo = null;
  const heat = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-heatmap')[0];
  const hmCell = heat.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-hm-cell')[0];
  hmCell.dispatchEvent({ type: 'click' });
  assert(navTo && navTo[0] === 'board' && navTo[1] && navTo[1].entry, `heatmap cell routes to board by entry (got ${JSON.stringify(navTo)})`);
});

// ---- (3) publication: GFM tables render via reused K renderer -------

test('(3) publication: the "Aggregate generation scores" GFM table renders (not raw "| … |")', async () => {
  freshState(); installFetch();
  const paper = await import('../js/variants/M/views/paper.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await paper.render(host, ctx, { epochId: EPOCH_ID });
  // the masthead + body rendered (K's renderer, re-skinned as .m-paper).
  const sheet = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('m-paper'))[0];
  assert(sheet, 'the publication sheet rendered');
  assert(allText(host).includes('Abstract'), 'the Abstract section rendered');
  // the GFM table rendered as a real <table>, not raw pipes.
  const tables = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vk-md-table');
  assert(tables.length >= 1, 'the markdown table rendered as a <table>');
  const ths = tables[0].querySelectorAll('[class]').filter(() => true);
  assert(allText(tables[0]).includes('generation') && allText(tables[0]).includes('scalar'), 'table header cells rendered');
  assert(allText(tables[0]).includes('146.65'), 'table data rendered');
  // body must NOT contain the raw pipe table syntax.
  assert(!allText(host).includes('| --- | --- | --- |'), 'no raw "| --- |" table syntax leaked through');
  // a LIVE figure (lineage bumps) embedded at the FIGURE marker.
  const liveFig = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-bumps');
  assert(liveFig.length >= 1, 'a live Tufte figure embedded in the paper');
});

test('(3b) publication: a COMBINED aggregate-generation-scores figure (bar chart + table) is available', async () => {
  freshState(); installFetch();
  const paper = await import('../js/variants/M/views/paper.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  // the analysis has only one FIGURE marker (lineage), so the canonical plate
  // set is NOT spliced; assert the combined visual builder works directly via
  // an analysis with the aggregate-scores marker.
  const EXTRA = { ...FIXTURE };
  EXTRA[`/api/epoch/${EPOCH_ID}/analysis`] = { epoch_id: EPOCH_ID, analysis_md: [
    '# Epoch ' + EPOCH_ID, '', '## Results', '', 'See the scores.', '<!-- FIGURE: aggregate-generation-scores -->', '',
  ].join('\n') };
  globalThis.fetch = async (path) => {
    if (Object.prototype.hasOwnProperty.call(EXTRA, path)) return { ok: true, json: async () => EXTRA[path] };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };
  await paper.render(host, ctx, { epochId: EPOCH_ID });
  const combined = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'm-aggscore');
  assert(combined.length === 1, 'the combined aggregate-scores visual rendered once');
  // it pairs a bar chart (d-vbars) WITH a table off the same numbers.
  const bars = combined[0].querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-vbars');
  const tbl = combined[0].querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'm-aggscore-table');
  assert(bars.length === 1, 'the bar chart is part of the combined visual');
  assert(tbl.length === 1, 'the table is part of the SAME combined visual');
  assert(allText(combined[0]).includes('70.94'), 'the figure reads off the live scalars');
});

test('(3c) publication: per-matchup detail (paired slopegraph) is bound from /api/matchup-grid', async () => {
  freshState(); installFetch();
  const paper = await import('../js/variants/M/views/paper.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  const EXTRA = { ...FIXTURE };
  EXTRA[`/api/epoch/${EPOCH_ID}/analysis`] = { epoch_id: EPOCH_ID, analysis_md: [
    '# Epoch ' + EPOCH_ID, '', '## Duel', '', 'The decisive round.', '<!-- FIGURE: matchup-detail -->', '',
  ].join('\n') };
  globalThis.fetch = async (path) => {
    if (Object.prototype.hasOwnProperty.call(EXTRA, path)) return { ok: true, json: async () => EXTRA[path] };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };
  await paper.render(host, ctx, { epochId: EPOCH_ID });
  const slope = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-pslope');
  assert(slope.length >= 1, 'a per-matchup paired slopegraph embedded in the paper');
  assert(allText(host).includes('Per-matchup detail'), 'captioned as per-matchup detail');
});

// ---- (6) proportional figure sizing ---------------------------------

test('(6) epoch view: figures share a proportional envelope (m-fig-md)', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/M/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epoch.render(host, ctx, {});
  const envelopes = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('m-fig-md'));
  // the bumps figure AND the heatmap figure both sit in the shared envelope.
  assert(envelopes.length >= 2, `multiple figures share the proportional envelope (saw ${envelopes.length})`);
  const heat = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-heatmap');
  const bumps = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-bumps');
  assert(heat.length === 1 && bumps.length === 1, 'both heatmap and bumps present (sized via the same envelope, not oversized)');
});

// ---- typeface + colour pickers switch + persist ---------------------

test('typeface picker: defaults to Editorial, switches all four, and persists', () => {
  const root = document.createElement('div');
  root.setAttribute('data-variant', 'M');
  typeface.initFace(root);
  assertEqual(root.getAttribute('data-m-face'), 'editorial', 'Editorial is M’s default');
  const sw = typeface.faceSwitcher(dom.el);
  const btns = sw.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('m-seg-btn'));
  assertEqual(btns.length, 4, 'four typeface options (Sans / Editorial / Technical / Display)');
  btns.find((b) => b.getAttribute('data-face') === 'technical').dispatchEvent({ type: 'click', preventDefault() {} });
  assertEqual(root.getAttribute('data-m-face'), 'technical', 'switch flips the root face attribute');
  assertEqual(typeface.currentFace(), 'technical', 'current face tracks the switch');
  assertEqual(window.localStorage.getItem('zicato.ui.M.face'), 'technical', 'the choice persists');
  btns.find((b) => b.getAttribute('data-face') === 'display').dispatchEvent({ type: 'click', preventDefault() {} });
  assertEqual(root.getAttribute('data-m-face'), 'display', 'switch flips to Display');
  typeface.applyFace('editorial');
});

test('colour picker: defaults to solarized-light and flips the root theme attribute', () => {
  const root = document.createElement('div');
  root.setAttribute('data-variant', 'M');
  theme.initTheme(root);
  assertEqual(root.getAttribute('data-m-theme'), 'solarized-light', 'light is the default');
  const sw = theme.themeSwitcher(dom.el);
  const btns = sw.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('m-seg-btn'));
  assertEqual(btns.length, 3, 'three colour themes');
  btns.find((b) => b.getAttribute('data-theme') === 'monokai').dispatchEvent({ type: 'click', preventDefault() {} });
  assertEqual(root.getAttribute('data-m-theme'), 'monokai', 'switch flips the root theme');
  assertEqual(window.localStorage.getItem('zicato.ui.M.theme'), 'monokai', 'the choice persists');
  theme.applyTheme('solarized-light');
});

// ---- cold deep-link transcript --------------------------------------

test('run view: a COLD deep-link fetches the conversation and renders the transcript', async () => {
  freshState(); installFetch();
  const runView = await import('../js/variants/M/views/run.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await runView.render(host, ctx, { gen: 'v1', entry: 'waffles_single' });
  const scroller = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'e-transcript')[0];
  assert(scroller, 'the transcript scroll container rendered');
  const turns = scroller.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('d-turn d-turn-'));
  assert(turns.length === 2, `transcript shows its turns (saw ${turns.length})`);
  assert(allText(host).includes('Drafting an outline'), 'turn text rendered from /api/conversation');
  assert(allText(host).includes('omitted the requested structure'), 'drift annotation rendered');
});

test('run view: a deep-link to a missing run id degrades to an honest empty state', async () => {
  freshState(); installFetch();
  const runView = await import('../js/variants/M/views/run.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await runView.render(host, ctx, { gen: 'v1', entry: 'does_not_exist' });
  assert(allText(host).toLowerCase().includes('no run id'), 'honest empty for an unknown entry');
});

// ---- lineage bumps: non-colliding + clickable -----------------------

test('lineage bumps: nodes are clickable and v1/v2 do not collide', () => {
  let clicked = null;
  const nodes = [
    { id: 'v0', x: 0, promoted: true, scalar: 70.94, parent: null },
    { id: 'v1', x: 1, promoted: false, scalar: 146.65, parent: 'v0' },
    { id: 'v2', x: 1, promoted: false, scalar: 72.45, parent: 'v0' },
  ];
  const node = svg.bumps({ width: 640, height: 180, nodes, onClick: (n) => { clicked = n.id; } });
  const circles = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('d-bump-node'));
  assertEqual(circles.length, 3, 'one node circle per generation');
  // v1 and v2 share x=1 but are de-collided in x (distinct cx).
  const v1v2 = circles.slice(1).map((c) => Number(c.getAttribute('cx')));
  assert(Math.abs(v1v2[0] - v1v2[1]) > 1, 'v1 and v2 do not collide (distinct cx)');
  circles[1].dispatchEvent({ type: 'click' });
  assert(clicked != null, 'a node click fires onClick');
});

await run();
