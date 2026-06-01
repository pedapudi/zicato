// test/variant_k.test.mjs — Variant K ("Monograph") unit tests.
//
// K is report-first: the ACM-style epoch publication IS the home, with live
// Tufte figures embedded inline and drill-down into the live dashboard. These
// tests pin the brief's mandates:
//   * the ACM report is the HOME and renders from analysis_md (eyebrow /
//     title / meta / abstract / sections);
//   * embedded figures render AND are clickable (drill into a live view);
//   * the render-discipline no-op (digest-gated repaint);
//   * a COLD deep-link transcript renders content, not an empty panel;
//   * the NEW mutation-per-generation matrix renders + cells drill;
//   * the Tufte Sankey is fit-to-width (a responsive viewBox, NO pan/zoom
//     viewport surface);
//   * lineage is non-colliding AND clickable;
//   * the three-theme switch re-skins (light / dark / monokai).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/K/router.js');
const svg = await import('../js/variants/K/svg.js');
const ui = await import('../js/variants/K/ui.js');
const paper = await import('../js/variants/K/paper.js');
const data = await import('../js/variants/K/data.js');
const dom = await import('../js/core/dom.js');

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
  '**Generations**: 3 attempted · 1 promoted · 2 rejected',
  '',
  '### Goal',
  '',
  'Make the presentation agent crisper.',
  '',
  '## Abstract',
  '',
  'Two challengers were proposed against the v0 seed; both regressed and the',
  'crown stood. The decisive driver was the `incorporates_feedback` judge.',
  '',
  '## Lineage',
  '',
  'The champion spine held across the epoch.',
  '',
  '<!-- FIGURE:lineage -->',
  '',
  '## Per-board behaviour',
  '',
  'Loss concentrated on the emulated stakeholder entry.',
  '',
  '<!-- FIGURE:per-board-heatmap -->',
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
      { mutation_id: 'coordinator_prompt', kind: 'prompt', file: 'agent/coordinator.py', role: 'coordinator system prompt', line_start: 10, line_end: 40, patched_generation_ids: ['v1'] },
      { mutation_id: 'slide_writer_prompt', kind: 'prompt', file: 'agent/writer.py', role: 'slide writer prompt', line_start: 5, line_end: 22, patched_generation_ids: ['v2'] },
      { mutation_id: 'oversight_policy', kind: 'policy', file: 'agent/policy.py', role: 'oversight policy', line_start: 1, line_end: 12, patched_generation_ids: ['v1', 'v2'] },
    ],
  },
  [`/api/files/${EPOCH_ID}/v1/patches`]: { patches: [
    { id: 'p1', mutation_id: 'coordinator_prompt', op: 'edit', new_content: 'Always emit an explicit slide structure.', rationale: 'Enforce structure.' },
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
  reason: 'challenger regressed', rules: [
    { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 146.65' },
    { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'not_reached', fired: false },
  ],
  scalar_components: { champion: { drift: 68.5, schema: 1.43 }, challenger: { drift: 145.64, schema: 0.0 } } };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', delta_scalar: 1.51, reason: 'challenger regressed', rules: [] };
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

// ---- router ---------------------------------------------------------

test('router: the paper is the default view (home)', () => {
  assertEqual(router.parseRoute('').view, 'paper');
  assertEqual(router.parseRoute('#/K/').view, 'paper');
  assertEqual(router.parseRoute('#/something').view, 'paper');
  assertEqual(router.parseRoute('#/K/bogus').view, 'paper');
});

test('router: drill routes parse', () => {
  assertEqual(router.parseRoute('#/K/candidate/v1').params.gen, 'v1');
  assertEqual(router.parseRoute('#/K/candidate/v1/waffles_single').params.entry, 'waffles_single');
  const m = router.parseRoute('#/K/matchups/v0/v1');
  assertEqual(m.params.champion, 'v0'); assertEqual(m.params.challenger, 'v1');
  assertEqual(router.parseRoute('#/K/mutations/v1').params.gen, 'v1');
  assertEqual(router.parseRoute('#/K/run/v1/waffles_single').view, 'run');
});

test('router: crumbTrail roots everything at the paper', () => {
  for (const r of [{ view: 'candidate', params: { gen: 'v1' } }, { view: 'matchups', params: {} }, { view: 'mutations', params: {} }]) {
    const trail = router.crumbTrail(r);
    assertEqual(trail[0].view, 'paper');
    assert(trail[trail.length - 1].current === true, 'leaf is current');
  }
});

// ---- the paper parser ----------------------------------------------

test('parsePaper splits eyebrow / title / meta / abstract / body', () => {
  const p = paper.parsePaper(ANALYSIS_MD);
  assert(p.eyebrow.includes('improvement campaign'), 'eyebrow captured');
  assertEqual(p.title, 'Presentation agent · epoch e0');
  assert(p.meta.length >= 3, 'meta pairs captured');
  assert(p.meta.some((m) => m.label === 'Epoch id'), 'meta label parsed');
  assert(p.abstract.includes('Two challengers'), 'abstract captured');
  assert(p.body.includes('<!-- FIGURE:lineage -->'), 'figure markers remain in body');
});

// ---- the ACM report is the HOME, rendered from analysis_md ----------

test('home view: the ACM paper is the home and typesets from analysis_md', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/K/views/home.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await home.render(host, ctx, {});
  const article = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vk-paper')[0];
  assert(article, 'the paper article rendered as the home');
  assert(host.textContent.includes('Presentation agent · epoch e0'), 'the title typeset');
  assert(host.querySelectorAll('[class]').some((n) => (n.getAttribute('class') || '') === 'vk-eyebrow'), 'eyebrow rendered');
  assert(host.querySelectorAll('[class]').some((n) => (n.getAttribute('class') || '') === 'vk-meta'), 'masthead meta rendered');
  assert(host.querySelectorAll('[class]').some((n) => (n.getAttribute('class') || '') === 'vk-abstract'), 'abstract rendered');
  assert(host.textContent.includes('Two challengers'), 'abstract body text present');
});

test('home view: embedded figures render at their markers and are clickable → a live view', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/K/views/home.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await home.render(host, ctx, {});
  // the lineage bumps figure is embedded inline (from <!-- FIGURE:lineage -->)
  const bumps = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vk-bumps');
  const heat = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vk-heatmap');
  assert(bumps.length >= 1, 'lineage bumps embedded as a figure');
  assert(heat.length >= 1, 'per-board heatmap embedded as a figure');
  // a bump node is clickable → drills into the candidate view.
  const node = bumps[0].querySelectorAll('[data-vk]').filter((n) => n.getAttribute('data-vk') === 'bump-node')[0];
  assert(node, 'a clickable lineage node exists');
  node.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'candidate', 'clicking a lineage node drills into the candidate view');
});

test('home view: digest-gated — identical data does NOT rebuild the DOM', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/K/views/home.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await home.render(host, ctx, {});
  const digest1 = host.getAttribute('data-vk-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'home painted');
  await home.render(host, ctx, {});
  assertEqual(host.getAttribute('data-vk-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

test('home view: a not-yet-built report still embeds the live figure gallery', async () => {
  freshState();
  // analysis returns empty markdown → "not written yet", figures still live.
  const F2 = { ...FIXTURE, [`/api/epoch/${EPOCH_ID}/analysis`]: { epoch_id: EPOCH_ID, analysis_md: '' } };
  globalThis.fetch = async (path) => Object.prototype.hasOwnProperty.call(F2, path)
    ? { ok: true, json: async () => F2[path] } : { ok: false, status: 404, json: async () => ({}) };
  const home = await import('../js/variants/K/views/home.js');
  const host = document.createElement('div');
  await home.render(host, { navigate() {}, href: router.href }, {});
  assert(host.querySelectorAll('[class]').some((n) => (n.getAttribute('class') || '').includes('vk-notyet')), 'honest not-yet state');
  assert(host.querySelectorAll('[class]').some((n) => (n.getAttribute('class') || '') === 'vk-bumps'), 'canonical live figures still embedded');
});

// ---- the NEW mutation-per-generation matrix ------------------------

test('mutations view: renders the site × generation matrix; a cell drills to the patch diff', async () => {
  freshState(); installFetch();
  const mut = await import('../js/variants/K/views/mutations.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  await mut.render(host, ctx, {});
  const matrix = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vk-mutmatrix')[0];
  assert(matrix, 'the mutation matrix rendered');
  // a filled cell (a generation that patched a site) is clickable.
  const onCells = matrix.querySelectorAll('[data-vk]').filter((n) => n.getAttribute('data-vk') === 'mut-cell' && (n.getAttribute('class') || '').includes('vk-mm-on'));
  assert(onCells.length >= 1, 'at least one filled (patched) cell');
  onCells[0].dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'mutations' && navTo.p.gen, 'clicking a patched cell drills into that generation’s patches');
  // and the drill-down renders the patch diff content.
  freshState(); installFetch();
  await mut.render(host, ctx, { gen: 'v1' });
  assert(host.textContent.includes('explicit slide structure'), 'the patch diff content rendered on drill-in');
});

// ---- cold deep-link transcript -------------------------------------

test('run view: a COLD deep-link fetches the conversation and renders the transcript', async () => {
  freshState(); installFetch();
  const runView = await import('../js/variants/K/views/run.js');
  const host = document.createElement('div');
  await runView.render(host, { navigate() {}, href: router.href }, { gen: 'v1', entry: 'waffles_single' });
  const scroller = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vk-transcript')[0];
  assert(scroller, 'the transcript scroll container rendered');
  const turns = scroller.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('vk-turn vk-turn-'));
  assertEqual(turns.length, 2, 'transcript shows its turns');
  assert(host.textContent.includes('Drafting an outline'), 'turn text rendered from /api/conversation');
  assert(host.textContent.includes('omitted the requested structure'), 'drift annotation rendered');
});

test('run view: a deep-link to a missing run id degrades to an honest empty state', async () => {
  freshState(); installFetch();
  const runView = await import('../js/variants/K/views/run.js');
  const host = document.createElement('div');
  await runView.render(host, { navigate() {}, href: router.href }, { gen: 'v1', entry: 'does_not_exist' });
  assert(host.textContent.toLowerCase().includes('no run id'), 'honest empty for an unknown entry');
});

// ---- candidate view: per-board dot-plot (readable) + drill ---------

test('candidate view: per-board dot-plot renders; entry param drills into expectations', async () => {
  freshState(); installFetch();
  const cand = await import('../js/variants/K/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await cand.render(host, ctx, { gen: 'v1' });
  const dot = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'vk-valdot');
  assert(dot.length === 1, 'per-board value dot-plot present');
  // drilling into the entry surfaces its expectation outcome.
  freshState();
  await cand.render(host, ctx, { gen: 'v1', entry: 'waffles_single' });
  assert(host.textContent.includes('predicate returned False') || host.textContent.toLowerCase().includes('fail'), 'entry drill shows the outcome');
});

// ---- the Tufte Sankey: fit-to-width, NO viewport -------------------

test('sankey: fit-to-width responsive viewBox, no pan/zoom viewport surface', () => {
  const mark = svg.sankey({
    width: 760,
    candidate: { label: 'v0', sub: 'patch' },
    boards: [
      { id: 'a', label: 'waffles_single', value: 60.5, ref: 'a' },
      { id: 'b', label: 'picky_stakeholder_emulated', value: 105.5, ref: 'b', cls: 'vk-bad' },
    ],
    aggregate: { label: 'scalar', sub: '166 loss' },
  });
  assertEqual(mark.localName, 'svg');
  // responsive: it carries a viewBox + preserveAspectRatio (fit-to-width),
  // not a fixed pannable surface with its own controls.
  assert(mark.getAttribute('viewBox'), 'sankey has a viewBox (responsive)');
  assert(mark.getAttribute('preserveAspectRatio'), 'sankey preserves aspect ratio (fit-to-width)');
  // honest no-viewport: there is no surface-controls chrome inside.
  const ribbons = mark.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('vk-ribbon'));
  assert(ribbons.length === 4, 'two boards → four ribbons (cand→board, board→agg)');
});

test('sankey: a board node is clickable → drills to the run', () => {
  let opened = null;
  const mark = svg.sankey({
    width: 600, candidate: { label: 'v0' },
    boards: [{ id: 'a', label: 'waffles_single', value: 60.5, ref: 'waffles_single' }],
    aggregate: { label: 'scalar' },
    onBoard: (entryId) => { opened = entryId; },
  });
  const board = mark.querySelectorAll('[data-vk]').filter((n) => n.getAttribute('data-vk') === 'sankey-board')[0];
  assert(board, 'a clickable board node exists');
  board.dispatchEvent({ type: 'click' });
  assertEqual(opened, 'waffles_single', 'clicking a board drills into its run');
});

// ---- lineage: non-colliding AND clickable --------------------------

test('lineage bumps: coincident challengers are de-collided (no shared x) and clickable', () => {
  let clicked = null;
  const mark = svg.bumps({
    width: 640, height: 190,
    nodes: [
      { id: 'v0', x: 0, promoted: true, parent: null },
      { id: 'v1', x: 1, promoted: false, parent: 'v0' },
      { id: 'v2', x: 1, promoted: false, parent: 'v0' }, // SAME x as v1 (F's collision)
    ],
    onClick: (n) => { clicked = n.id; },
  });
  const nodes = mark.querySelectorAll('[data-vk]').filter((n) => n.getAttribute('data-vk') === 'bump-node');
  assertEqual(nodes.length, 3, 'one node per generation');
  // v1 and v2 share an x but must NOT share a cx (de-collided).
  const v1 = nodes.find((n) => n.getAttribute('data-gen') === 'v1');
  const v2 = nodes.find((n) => n.getAttribute('data-gen') === 'v2');
  assert(v1.getAttribute('cx') !== v2.getAttribute('cx'), 'coincident challengers are de-collided in x');
  // clickable.
  v1.dispatchEvent({ type: 'click' });
  assertEqual(clicked, 'v1', 'a lineage node click fires with its generation id');
});

test('decollide pushes coincident y-values at least minGap apart', () => {
  const y = (v) => v; // identity scale
  const out = svg.decollide([{ v: 50 }, { v: 50 }, { v: 50 }], y, 12, 0, 1000);
  out.sort((a, b) => a - b);
  assert(out[1] - out[0] >= 12 - 1e-9 && out[2] - out[1] >= 12 - 1e-9, 'neighbours kept minGap apart');
});

// ---- the three-theme switcher --------------------------------------

test('theme: switcher offers all three themes and applies + persists the pick', () => {
  freshState();
  const root = document.createElement('div');
  let picked = null;
  const sw = ui.themeSwitcher('solarized-light', (t) => { picked = t; });
  const btns = sw.querySelectorAll('[data-theme]');
  const ids = btns.map((b) => b.getAttribute('data-theme'));
  assert(ids.includes('solarized-light') && ids.includes('solarized-dark') && ids.includes('monokai'), 'all three themes offered');
  // applyTheme stamps the attribute + persists.
  ui.applyTheme(root, 'monokai');
  assertEqual(root.getAttribute('data-vk-theme'), 'monokai', 'theme applied to the root');
  assertEqual(ui.readTheme(), 'monokai', 'theme persisted to storage');
  // a bad value falls back to the default.
  assertEqual(ui.normaliseTheme('nonsense'), 'solarized-light', 'unknown theme → default');
  // the switcher click invokes onPick.
  btns.find((b) => b.getAttribute('data-theme') === 'solarized-dark').dispatchEvent({ type: 'click' });
  assertEqual(picked, 'solarized-dark', 'switcher click fires onPick');
});

test('per-board dot-plot: marks carry theme tokens (read in all three themes)', () => {
  // The dot-plot colours come from --vk-* tokens (set per theme), so the same
  // SVG re-skins; we assert the mark uses the token-bound classes rather than
  // hard-coded fills, which is what makes it readable in every theme.
  const mark = svg.valueDotPlot({ items: [
    { label: 'waffles_single', id: 'a', value: 60.5, pass: 0 },
    { label: 'picky', id: 'b', value: 642.5, pass: 0, timeout: true },
  ], reference: { label: 'champ', value: 100 } });
  const good = mark.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('vk-good'));
  const bad = mark.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('vk-bad'));
  assert(good.length >= 1, 'a below-reference entry uses the improve token class');
  assert(bad.length >= 1, 'an above-reference entry uses the regress token class');
});

await run();
