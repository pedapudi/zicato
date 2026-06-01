// test/variant_i.test.mjs — Variant I ("Ledger") unit tests.
//
// Ledger is the editorial, light-first convergence skin built on Variant
// E's flow. These tests pin the round-3 guarantees the brief calls out:
//   (1) digest-gated repaint — identical data / heartbeat = no rebuild;
//   (2) a COLD deep-link run/transcript render hydrates + paints content;
//   (3) the NEW mutation-site × generation matrix renders with patched cells;
//   (4) the NEW ACM-style publication renders the analysis_md (masthead +
//       numbered sections + live embedded figures);
//   (5) the Tufte Sankey is FIT-TO-WIDTH (viewBox + width:100%, no pan/zoom
//       viewport surface);
//   (6) lineage bumps are NON-COLLIDING + CLICKABLE;
//   (7) the three-theme switch flips the variant root's data-i-theme.
// Plus the router (E-style IA extended with the two new views) and the
// gatedSwap no-flash helper.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/I/router.js');
const ui = await import('../js/variants/I/ui.js');
const svg = await import('../js/variants/I/svg.js');
const sankey = await import('../js/variants/I/diagram/sankey.js');
const report = await import('../js/variants/I/report.js');
const theme = await import('../js/variants/I/theme.js');
const data = await import('../js/variants/I/data.js');
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
  // mutation surface (round-3 NEW)
  [`/api/mutations/${EPOCH_ID}`]: {
    epoch_id: EPOCH_ID, generations: ['v0', 'v1', 'v2'],
    mutations: [
      { mutation_id: 'm1', kind: 'block', file: 'agent/coordinator.py', role: 'system_prompt', line_start: 12, line_end: 20, patched_generation_ids: ['v1', 'v2'] },
      { mutation_id: 'm2', kind: 'block', file: 'agent/writer.py', role: 'instructions', line_start: 30, line_end: 36, patched_generation_ids: ['v1'] },
    ],
  },
  [`/api/mutations/${EPOCH_ID}/m1`]: {
    epoch_id: EPOCH_ID, mutation_id: 'm1', baseline_content: 'be terse',
    generations: [{ generation_id: 'v1', content: 'be terse AND enforce slide structure' }, { generation_id: 'v2', content: 'be terse; oversee writer' }],
  },
  // analysis publication (round-3 NEW)
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
      'Caption: Lineage of the epoch.',
      '<!-- FIGURE: lineage -->',
      '',
      'The challengers lost on the per-board duels.',
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
  { entry_id: 'waffles_single', run_id: 'run_v2_waffles', drift_loss: 61.0, pass_fail: 0, runtime_ms: 180000, wall_clock_budget_exceeded: false },
  { entry_id: 'picky_stakeholder_emulated', run_id: 'run_v2_picky', drift_loss: 110.0, pass_fail: 0, runtime_ms: 360000, wall_clock_budget_exceeded: true },
] };
FIXTURE[`/api/files/${EPOCH_ID}/v0/patches`] = { epoch_id: EPOCH_ID, generation_id: 'v0', patches: [] };
FIXTURE[`/api/files/${EPOCH_ID}/v1/patches`] = { epoch_id: EPOCH_ID, generation_id: 'v1', patches: [
  { id: 'p1', mutation_id: 'm1', op: 'replace', new_content: 'be terse AND enforce slide structure' },
  { id: 'p2', mutation_id: 'm2', op: 'replace', new_content: 'write tighter' },
] };
FIXTURE[`/api/files/${EPOCH_ID}/v2/patches`] = { epoch_id: EPOCH_ID, generation_id: 'v2', patches: [
  { id: 'p3', mutation_id: 'm1', op: 'replace', new_content: 'be terse; oversee writer' },
] };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`] = { decision: 'rejected', delta_scalar: 75.71, reason: 'challenger regressed: loss rose by 75.71' };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', delta_scalar: 1.51, reason: 'challenger regressed: loss rose by 1.51' };
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

// ---- router (E-style IA + the two new views) ------------------------

test('router: parses the Ledger-prefixed hashes incl. mutations + paper', () => {
  assertEqual(router.parseRoute('#/I/').view, 'home');
  assertEqual(router.parseRoute('#/I/epoch').view, 'epoch');
  assertEqual(router.parseRoute('#/I/matchups').view, 'matchups');
  assertEqual(router.parseRoute('#/I/mutations').view, 'mutations');
  assertEqual(router.parseRoute('#/I/mutations/m1').params.mutationId, 'm1');
  assertEqual(router.parseRoute('#/I/paper').view, 'paper');
  const c = router.parseRoute('#/I/candidate/v1/waffles_single');
  assertEqual(c.view, 'candidate');
  assertEqual(c.params.gen, 'v1');
  assertEqual(c.params.entry, 'waffles_single');
});

test('router: a foreign / empty hash defaults to home', () => {
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/E/epoch').view, 'home');
  assertEqual(router.parseRoute('#/I/bogus').view, 'home');
});

test('router: href round-trips through parseRoute', () => {
  const h = router.href('mutations', { mutationId: 'm2' });
  const back = router.parseRoute(h);
  assertEqual(back.view, 'mutations');
  assertEqual(back.params.mutationId, 'm2');
});

// ---- gatedSwap: the no-flash guarantee ------------------------------

test('gatedSwap rebuilds on a changed digest and no-ops on an identical one', () => {
  const host = document.createElement('div');
  let builds = 0;
  ui.gatedSwap(host, 'A', () => { builds += 1; return [dom.el('p', { text: 'one' })]; });
  assertEqual(builds, 1, 'first paint builds');
  ui.gatedSwap(host, 'A', () => { builds += 1; return [dom.el('p', { text: 'one' })]; });
  assertEqual(builds, 1, 'identical digest is a no-op (no rebuild)');
  ui.gatedSwap(host, 'B', () => { builds += 1; return [dom.el('p', { text: 'two' })]; });
  assertEqual(builds, 2, 'a changed digest rebuilds');
});

// ---- (1) digest-gated repaint: identical data / heartbeat = no rebuild ----

test('home view: a re-render with identical data does NOT rebuild the DOM', async () => {
  freshState(); installFetch();
  const home = await import('../js/variants/I/views/home.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await home.render(host, ctx, {});
  const digest1 = host.getAttribute('data-i-digest');
  const writes1 = host.innerHTMLWriteCount();
  const firstChild = host.firstChild;
  assert(host.children.length > 0, 'home painted content');
  await home.render(host, ctx, {});
  assertEqual(host.getAttribute('data-i-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === firstChild, 'the content host was not rebuilt (same node identity)');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// ---- (6) lineage bumps: NON-COLLIDING + CLICKABLE -------------------

test('lineage bumps: nodes are clickable and v1/v2 do not collide', () => {
  let clicked = null;
  const nodes = [
    { id: 'v0', x: 0, promoted: true, scalar: 70.94, parent: null },
    { id: 'v1', x: 1, promoted: false, scalar: 146.65, parent: 'v0' },
    { id: 'v2', x: 2, promoted: false, scalar: 72.45, parent: 'v0' },
  ];
  const node = svg.bumps({ width: 640, height: 180, nodes, onClick: (n) => { clicked = n.id; } });
  assertEqual(node.localName, 'svg');
  const circles = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('d-bump-node'));
  assertEqual(circles.length, 3, 'one node circle per generation');
  // distinct cy per node (no collision): promoted spine vs challenger lane.
  const cys = circles.map((c) => Number(c.getAttribute('cy')));
  const uniq = new Set(cys.map((y) => Math.round(y)));
  assert(uniq.size >= 2, 'champion and challengers occupy distinct lanes (non-colliding)');
  // clickable
  circles[1].dispatchEvent({ type: 'click' });
  assert(clicked === 'v1' || clicked === 'v0' || clicked === 'v2', 'a node click fires onClick with a generation id');
});

test('decollide pushes coincident labels apart by at least minGap', () => {
  // three identical values would collide at the same y; decollide spreads them.
  const y = (v) => v; // identity scale for the test
  const out = svg.decollide([{ v: 100 }, { v: 100 }, { v: 100 }], y, 13, 0, 500);
  out.sort((a, b) => a - b);
  assert(out[1] - out[0] >= 12.9, 'adjacent labels keep the minimum gap');
  assert(out[2] - out[1] >= 12.9, 'adjacent labels keep the minimum gap');
});

// ---- (5) Tufte Sankey: FIT-TO-WIDTH, NO viewport --------------------

test('Tufte Sankey: fit-to-width viewBox + width:100%, no pan/zoom surface', () => {
  const node = sankey.buildTufteSankey({
    genId: 'v1',
    rows: [
      { entryId: 'waffles_single', driftLoss: 60.5, passFail: 0, budgetExceeded: true, runId: 'r1' },
      { entryId: 'picky_stakeholder_emulated', driftLoss: 642.5, passFail: 0, budgetExceeded: true, runId: 'r2' },
    ],
    onBoard() {},
  });
  assertEqual(node.localName, 'svg');
  assert(node.getAttribute('class') === 'i-sankey', 'is the Tufte sankey root');
  assert(node.getAttribute('viewBox'), 'carries a viewBox (responsive scaling)');
  // fit-to-width: width:100% style, NOT a fixed pixel width that needs a viewport.
  assertEqual(node.style._props.width, '100%', 'svg scales to the container width');
  // NO pan/zoom surface: the C surface emits a `.cz-surface` / `.cz-viewport`;
  // the Tufte sankey must NOT.
  const surfaces = node.querySelectorAll('[class]').filter((n) => {
    const c = n.getAttribute('class') || '';
    return c.includes('cz-surface') || c.includes('cz-viewport');
  });
  assertEqual(surfaces.length, 0, 'no pan/zoom viewport surface');
  // board nodes are clickable (drill-down).
  const boardNodes = node.querySelectorAll('[data-cz]').filter((n) => n.getAttribute('data-cz') === 'sankey-board-node');
  assertEqual(boardNodes.length, 2, 'one clickable board node per entry');
});

test('Tufte Sankey board node click drills into the entry', () => {
  let drilled = null;
  const node = sankey.buildTufteSankey({
    genId: 'v1',
    rows: [{ entryId: 'waffles_single', driftLoss: 60.5, passFail: 0, budgetExceeded: false, runId: 'r1' }],
    onBoard: (r) => { drilled = r.entryId; },
  });
  const bn = node.querySelectorAll('[data-cz]').filter((n) => n.getAttribute('data-cz') === 'sankey-board-node')[0];
  bn.dispatchEvent({ type: 'click' });
  assertEqual(drilled, 'waffles_single');
});

// ---- candidate view: Tufte Sankey + dot-plot + drill-down -----------

test('candidate view: renders the Tufte Sankey + per-board dot-plot, drills on entry', async () => {
  freshState(); installFetch();
  const cand = await import('../js/variants/I/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await cand.render(host, ctx, { gen: 'v1' });
  const sankeySvg = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'i-sankey');
  const dotplot = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-valdot');
  assert(sankeySvg.length === 1, 'fit-to-width Tufte Sankey present');
  assert(dotplot.length === 1, 'per-board value dot-plot present');
  // the hypothesis bet is rendered as a pull-quote (editorial voice).
  assert(host.textContent.includes('Enforce explicit slide-structure output'), 'hypothesis pull-quote rendered');
  // the rejection reason is rendered as a pull-quote.
  assert(host.textContent.includes('challenger regressed'), 'gate rejection reason pull-quote rendered');
  // drill into an entry
  freshState();
  await cand.render(host, ctx, { gen: 'v1', entry: 'waffles_single' });
  assert(host.textContent.includes('predicate returned False'), 'entry drill-down shows the expectation detail');
});

// ---- (2) cold deep-link run/transcript renders content --------------

test('run view: a COLD deep-link fetches the conversation and renders the transcript', async () => {
  freshState(); installFetch();
  const runView = await import('../js/variants/I/views/run.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await runView.render(host, ctx, { gen: 'v1', entry: 'waffles_single' });
  const scroller = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'e-transcript')[0];
  assert(scroller, 'the transcript scroll container rendered');
  const turns = scroller.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('d-turn d-turn-'));
  assert(turns.length === 2, `transcript shows its turns (saw ${turns.length})`);
  assert(host.textContent.includes('Drafting an outline'), 'turn text rendered from /api/conversation');
  assert(host.textContent.includes('omitted the requested structure'), 'drift annotation rendered');
});

test('run view: a deep-link to a missing run id degrades to an honest empty state', async () => {
  freshState(); installFetch();
  const runView = await import('../js/variants/I/views/run.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await runView.render(host, ctx, { gen: 'v1', entry: 'does_not_exist' });
  assert(host.textContent.toLowerCase().includes('no run id'), 'honest empty for an unknown entry');
});

// ---- (3) NEW mutation-site × generation matrix ----------------------

test('mutations view: renders the site × generation matrix with patched cells + drill', async () => {
  freshState(); installFetch();
  const mut = await import('../js/variants/I/views/mutations.js');
  const host = document.createElement('div');
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = [v, p]; }, href: router.href };
  await mut.render(host, ctx, {});
  const matrix = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'i-mut-matrix');
  assert(matrix.length === 1, 'the mutation matrix table rendered');
  // m1 was patched by v1 AND v2; m2 only by v1 → patched cells present.
  const patched = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('i-patched'));
  assert(patched.length >= 3, `patched cells present (saw ${patched.length})`);
  assert(host.textContent.includes('coordinator.py'), 'a mutation site file is shown');
  // a patched cell click drills to the site.
  patched[0].dispatchEvent({ type: 'click' });
  assert(navTo && navTo[0] === 'mutations' && navTo[1] && navTo[1].mutationId, 'patched cell click drills to a mutation site');
});

test('mutations view: drilling a site shows its baseline + per-generation patch diff', async () => {
  freshState(); installFetch();
  const mut = await import('../js/variants/I/views/mutations.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await mut.render(host, ctx, { mutationId: 'm1' });
  assert(host.textContent.includes('be terse'), 'baseline content shown');
  assert(host.textContent.includes('enforce slide structure'), 'a generation patch content shown');
});

// ---- (4) NEW ACM-style publication renders --------------------------

test('report parser: extracts eyebrow, title, meta, sections, and figure slots', () => {
  const md = FIXTURE[`/api/epoch/${EPOCH_ID}/analysis`].analysis_md;
  const doc = report.parseAnalysis(md);
  assert(doc.eyebrow && doc.eyebrow.includes('improvement campaign'), 'eyebrow captured');
  assert(doc.title && doc.title.includes(EPOCH_ID), 'title captured');
  assert(doc.meta.length >= 3, 'metadata pairs captured');
  assert(doc.meta.some((m) => m.label === 'Status'), 'a labelled meta cell parsed');
  assert(doc.blocks.some((b) => b.kind === 'heading' && b.text === 'Abstract'), 'Abstract section parsed');
  assert(doc.blocks.some((b) => b.kind === 'figure'), 'a figure slot parsed from the FIGURE marker');
});

test('paper view: renders the publication masthead, numbered sections, and a LIVE embedded figure', async () => {
  freshState(); installFetch();
  const paper = await import('../js/variants/I/views/paper.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await paper.render(host, ctx, { epochId: EPOCH_ID });
  // masthead + body
  const masthead = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'i-paper-masthead');
  assert(masthead.length === 1, 'publication masthead rendered');
  assert(host.textContent.includes('Abstract'), 'the Abstract section rendered');
  assert(host.textContent.includes('1. Abstract') || host.textContent.includes('1.Abstract'), 'sections are auto-numbered');
  // the FIGURE marker became a LIVE embedded Tufte figure (a bumps svg).
  const liveFig = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-bumps');
  assert(liveFig.length >= 1, 'a live Tufte figure (lineage bumps) is embedded in the paper');
  // figure carries a numbered caption.
  assert(host.textContent.includes('Figure 1.'), 'the embedded figure has a numbered caption');
});

test('paper view: a missing analysis degrades to an honest not-built state with live preview', async () => {
  freshState();
  // a fetch where the analysis endpoint returns empty md.
  const EMPTY = { ...FIXTURE };
  EMPTY[`/api/epoch/${EPOCH_ID}/analysis`] = { epoch_id: EPOCH_ID, analysis_md: '', analysis_html_available: false };
  globalThis.fetch = async (path) => {
    if (Object.prototype.hasOwnProperty.call(EMPTY, path)) return { ok: true, json: async () => EMPTY[path] };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };
  const paper = await import('../js/variants/I/views/paper.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await paper.render(host, ctx, { epochId: EPOCH_ID });
  assert(host.textContent.toLowerCase().includes('not built yet'), 'honest not-built note');
  const liveFig = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-bumps');
  assert(liveFig.length >= 1, 'live figures still preview even without a built paper');
});

// ---- (7) three-theme switch ----------------------------------------

test('theme: switcher defaults to solarized-light and flips the root attribute', () => {
  const root = document.createElement('div');
  root.setAttribute('data-variant', 'I');
  theme.initTheme(root);
  assertEqual(root.getAttribute('data-i-theme'), 'solarized-light', 'light is the default');
  const sw = theme.themeSwitcher(dom.el);
  const btns = sw.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('i-theme-btn'));
  assertEqual(btns.length, 3, 'three theme segments (light / dark / monokai)');
  // flip to monokai
  const monokai = btns.filter((b) => b.getAttribute('data-theme') === 'monokai')[0];
  monokai.dispatchEvent({ type: 'click', preventDefault() {} });
  assertEqual(root.getAttribute('data-i-theme'), 'monokai', 'switch flips the root theme attribute');
  assertEqual(theme.currentTheme(), 'monokai', 'current theme tracks the switch');
  // and back to dark
  const dark = btns.filter((b) => b.getAttribute('data-theme') === 'solarized-dark')[0];
  dark.dispatchEvent({ type: 'click', preventDefault() {} });
  assertEqual(root.getAttribute('data-i-theme'), 'solarized-dark', 'switch flips to solarized-dark');
  // restore default for any later test
  theme.applyTheme('solarized-light');
});

// ---- epoch view: lineage + heatmap + trellis, digest-gated ----------

test('epoch view renders lineage + heatmap + trellis and is digest-gated', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/variants/I/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epoch.render(host, ctx, {});
  assert(host.children.length > 0, 'epoch painted content');
  const bumps = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-bumps');
  const heat = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-heatmap');
  const trellis = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-trellis');
  assert(bumps.length === 1, 'lineage bumps present');
  assert(heat.length === 1, 'entries × generation heatmap present');
  assert(trellis.length === 1, 'board trellis present');
  const first = host.firstChild;
  await epoch.render(host, ctx, {});
  assert(host.firstChild === first, 'identical data → no rebuild');
});

await run();
