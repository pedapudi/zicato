// test/variant_j.test.mjs — Variant J ("Console") unit tests.
//
// Console is the round-3 convergence dashboard: dense, data-ink-maximal,
// monokai-default, built on Variant E's flow with two NEW views. These tests
// pin the convergence-brief guarantees:
//   (a) digest-gated repaint — identical data is a DOM no-op;
//   (b) a COLD deep-link run/transcript render paints content, not empty;
//   (c) the NEW mutation-site × generation matrix renders + drills to a patch;
//   (d) the NEW ACM-style epoch publication renders from analysis_md, with a
//       live inline figure;
//   (e) the Tufte Sankey fits to width (NO viewport — width=100%, no pan/zoom);
//   (f) the lineage bumps are non-colliding AND clickable;
//   (g) the three-theme switcher (incl. monokai default) restyles the root.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/J/router.js');
const dag = await import('../js/variants/J/dag.js');
const ui = await import('../js/variants/J/ui.js');
const svg = await import('../js/variants/J/svg.js');
const data = await import('../js/variants/J/data.js');
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
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected', scalar_score: 146.65 } },
      { generation_id: 'v2', parent_generation_id: 'v0', outcome: { decision: 'rejected', scalar_score: 72.45 } },
    ],
  },
  '/api/tournaments': { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71 },
    { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51 },
  ] },
  // the NEW mutation surface: two sites, v1 patched both, v2 patched one.
  [`/api/mutations/${EPOCH_ID}`]: {
    generations: ['v0', 'v1', 'v2'],
    mutations: [
      { mutation_id: 'coordinator.system', kind: 'instruction', file: 'agent/coordinator.py', role: 'system_prompt', line_start: 12, line_end: 40, patched_by: [], patched_generation_ids: ['v1', 'v2'] },
      { mutation_id: 'writer.tooldesc', kind: 'tool', file: 'agent/writer.py', role: 'tool_description', line_start: 88, line_end: 90, patched_by: [], patched_generation_ids: ['v1'] },
    ],
  },
  [`/api/files/${EPOCH_ID}/v0/patches`]: { patches: [] },
  [`/api/files/${EPOCH_ID}/v1/patches`]: { patches: [
    { id: 'p1', mutation_id: 'coordinator.system', op: 'replace', new_content: 'Always enforce explicit slide-structure output.\nNumber every slide.', rationale: 'Enforce slide structure.' },
    { id: 'p2', mutation_id: 'writer.tooldesc', op: 'set_numeric', new_numeric: 5 },
  ] },
  [`/api/files/${EPOCH_ID}/v2/patches`]: { patches: [
    { id: 'p3', mutation_id: 'coordinator.system', op: 'replace', new_content: 'Tighten the coordinator oversight loop.', rationale: 'Tighten oversight.' },
  ] },
  // the NEW ACM analysis_md with markers + a live figure.
  [`/api/epoch/${EPOCH_ID}/analysis`]: { epoch_id: EPOCH_ID, analysis_md:
    '<!-- EYEBROW -->\nZicato epoch report\n\n# Both challengers fell to the seed\n\n<!-- META -->\nEpoch 2026-05-30_e0 · 3 generations\n\n## Abstract\nThe seed v0 held the crown; v1 and v2 both regressed.\n\n## Lineage\nThe family tree.\n\n<!-- FIGURE:LINEAGE -->\n\n## Findings\n- v1 regressed sharply on the emulated stakeholder.\n\n<!-- CALLOUT:Takeaway -->\nThe gate correctly rejected noise.' },
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
FIXTURE[`/api/round/${EPOCH_ID}/v0/v1/gate`] = { decision: 'rejected', delta_scalar: 75.71, reason: 'challenger regressed' };
FIXTURE[`/api/round/${EPOCH_ID}/v0/v2/gate`] = { decision: 'rejected', delta_scalar: 1.51, reason: 'challenger regressed' };
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

// ---- router (E-style IA + breadcrumb, + the two new views) ----------

test('router: parses the Console-prefixed hashes, incl. the new views', () => {
  assertEqual(router.parseRoute('#/J/').view, 'home');
  assertEqual(router.parseRoute('#/J/epoch').view, 'epoch');
  assertEqual(router.parseRoute('#/J/matchups').view, 'matchups');
  assertEqual(router.parseRoute('#/J/mutations').view, 'mutations');
  assertEqual(router.parseRoute('#/J/mutations/coordinator.system').params.mutId, 'coordinator.system');
  assertEqual(router.parseRoute('#/J/report').view, 'report');
  assertEqual(router.parseRoute('#/J/report/2026-05-30_e0').params.epochId, '2026-05-30_e0');
  const r = router.parseRoute('#/J/run/v1/waffles_single');
  assertEqual(r.view, 'run');
  assertEqual(r.params.gen, 'v1');
  assertEqual(r.params.entry, 'waffles_single');
});

test('router: a foreign / empty hash defaults to home', () => {
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/E/epoch').view, 'home'); // E prefix is foreign to J
  assertEqual(router.parseRoute('#/J/bogus').view, 'home');
});

// ---- gatedSwap: the no-flash guarantee ------------------------------

test('gatedSwap rebuilds on a changed digest and no-ops on an identical one', () => {
  const host = document.createElement('div');
  let builds = 0;
  ui.gatedSwap(host, 'A', () => { builds += 1; return [dom.el('p', { text: 'one' })]; });
  assertEqual(builds, 1, 'first paint builds');
  ui.gatedSwap(host, 'A', () => { builds += 1; return [dom.el('p', { text: 'one' })]; });
  assertEqual(builds, 1, 'identical digest is a no-op');
  ui.gatedSwap(host, 'B', () => { builds += 1; return [dom.el('p', { text: 'two' })]; });
  assertEqual(builds, 2, 'a changed digest rebuilds');
});

// ---- (f) lineage bumps: non-colliding + clickable -------------------

test('bumps: coincident challengers are de-collided AND each node is clickable', () => {
  let clicked = null;
  // v1 + v2 both branch off v0 at neighbouring x — without de-collision their
  // screen-x would coincide. The view passes onClick → candidate.
  const node = svg.bumps({
    width: 400, height: 160,
    nodes: [
      { id: 'v0', x: 0, promoted: true, parent: null },
      { id: 'v1', x: 1, promoted: false, parent: 'v0' },
      { id: 'v2', x: 1, promoted: false, parent: 'v0' },
    ],
    onClick: (n) => { clicked = n.id; },
  });
  assertEqual(node.localName, 'svg');
  const circles = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('dj-bump-node'));
  assertEqual(circles.length, 3, 'one node per generation');
  // the two challengers share x=1; their cx must differ after de-collision.
  const challengers = circles.filter((c) => (c.getAttribute('class') || '').includes('dj-rejected'));
  assertEqual(challengers.length, 2, 'two challenger nodes');
  const cx0 = Number(challengers[0].getAttribute('cx'));
  const cx1 = Number(challengers[1].getAttribute('cx'));
  assert(Math.abs(cx0 - cx1) >= 1, `challenger nodes do not collide (cx ${cx0} vs ${cx1})`);
  // clickable
  challengers[0].dispatchEvent({ type: 'click' });
  assert(clicked === 'v1' || clicked === 'v2', 'a node click fires onClick → candidate');
});

test('decollide pushes coincident y-values at least minGap apart', () => {
  const y = (v) => v; // identity scale for the test
  const out = svg.decollide([{ v: 50 }, { v: 50 }, { v: 50 }], y, 13, 0, 1000);
  out.sort((a, b) => a - b);
  assert(out[1] - out[0] >= 13 - 1e-9, 'gap 1');
  assert(out[2] - out[1] >= 13 - 1e-9, 'gap 2');
});

// ---- (e) the Tufte Sankey: fit-to-width, NO viewport ----------------

test('sankey: fits to width (width=100%, no pan/zoom viewport) and draws flow', () => {
  const node = svg.sankey({
    width: 720, colHeight: 200,
    patch: [{ id: 'patch', label: 'patch v0→v1', value: 100 }],
    drift: [{ id: 'd_a', label: 'a', value: 60, cls: 'dj-bad', ref: 'a' }, { id: 'd_b', label: 'b', value: 40, cls: 'dj-good', ref: 'b' }],
    gate: [{ id: 'gate', label: 'rejected', value: 100, cls: 'dj-bad' }],
    links: [
      { source: 'patch', target: 'd_a', value: 60, cls: 'dj-bad' },
      { source: 'patch', target: 'd_b', value: 40, cls: 'dj-good' },
      { source: 'd_a', target: 'gate', value: 60, cls: 'dj-bad' },
      { source: 'd_b', target: 'gate', value: 40, cls: 'dj-good' },
    ],
  });
  assertEqual(node.localName, 'svg');
  // FIT TO WIDTH: the svg width attribute is responsive (100%), and there is
  // a viewBox (intrinsic coords) — i.e. no fixed pixel viewport to pan around.
  assertEqual(node.getAttribute('width'), '100%', 'svg is responsive width (fit-to-container)');
  assert(node.getAttribute('viewBox'), 'has a viewBox (scales, never pans)');
  const ribbons = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('dj-sankey-ribbon'));
  assert(ribbons.length === 4, `four flow ribbons (saw ${ribbons.length})`);
});

test('layoutSankey: ribbon widths are proportional and fit the column box', () => {
  const { box, links } = svg.layoutSankey({
    width: 600, patch: [{ id: 'p', value: 100 }], drift: [{ id: 'a', value: 60 }, { id: 'b', value: 40 }], gate: [{ id: 'g', value: 100 }],
    links: [{ source: 'p', target: 'a', value: 60 }, { source: 'p', target: 'b', value: 40 }, { source: 'a', target: 'g', value: 60 }, { source: 'b', target: 'g', value: 40 }],
  });
  assertEqual(box.w, 600, 'box width equals the requested fit-to-container width');
  const pa = links.find((l) => l.source === 'p' && l.target === 'a');
  const pb = links.find((l) => l.source === 'p' && l.target === 'b');
  assert(pa.hwS > pb.hwS, 'the heavier (60) flow is wider than the lighter (40)');
});

// ---- the compact lifecycle DAG --------------------------------------

test('lifecycleDag draws a board node per entry, coloured by outcome', () => {
  const node = dag.lifecycleDag({
    genId: 'v1', parentId: 'v0', decision: 'rejected', deltaScalar: 75.71, patchPoints: 2,
    entries: [
      { entry_id: 'a', drift_loss: 60.5, pass_fail: 0, wall_clock_budget_exceeded: false },
      { entry_id: 'b', drift_loss: 642.5, pass_fail: 0, wall_clock_budget_exceeded: true },
    ],
  });
  assertEqual(node.localName, 'svg');
  const boardNodes = node.querySelectorAll('[data-cz]').filter((n) => n.getAttribute('data-cz') === 'lc-board-node');
  assertEqual(boardNodes.length, 2, 'one board node per entry');
  assert(boardNodes.some((n) => (n.getAttribute('class') || '').includes('ezj-deferred')), 'timeout → deferred');
  assert(boardNodes.some((n) => (n.getAttribute('class') || '').includes('ezj-rejected')), 'fail → rejected');
});

// ---- (a) digest-gated repaint: identical data = no rebuild ----------

test('home view: a re-render with identical data does NOT rebuild the DOM', async () => {
  freshState();
  installFetch();
  const home = await import('../js/variants/J/views/home.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await home.render(host, ctx, {});
  const digest1 = host.getAttribute('data-j-digest');
  const writes1 = host.innerHTMLWriteCount();
  const firstChild = host.firstChild;
  assert(host.children.length > 0, 'home painted content');
  await home.render(host, ctx, {});
  assertEqual(host.getAttribute('data-j-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === firstChild, 'the content host was not rebuilt (same node identity)');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

test('epoch view renders lineage + heatmap + trellis, and is digest-gated', async () => {
  freshState();
  installFetch();
  const epoch = await import('../js/variants/J/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epoch.render(host, ctx, {});
  assert(host.children.length > 0, 'epoch painted content');
  const bumps = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'dj-bumps');
  const heat = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'dj-heatmap');
  const trellis = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'dj-trellis');
  assert(bumps.length === 1, 'lineage bumps present');
  assert(heat.length === 1, 'entries × generation heatmap present');
  assert(trellis.length === 1, 'board trellis present');
  const first = host.firstChild;
  await epoch.render(host, ctx, {});
  assert(host.firstChild === first, 'identical data → no rebuild');
});

// ---- per-board scoring (theme-readable dot-plot) --------------------

test('candidate view: per-board dot-plot + lifecycle DAG; entry param drills in', async () => {
  freshState();
  installFetch();
  const cand = await import('../js/variants/J/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await cand.render(host, ctx, { gen: 'v1' });
  const dotplot = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'dj-valdot');
  const dagSvg = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'ezj-dag');
  assert(dotplot.length === 1, 'per-board value dot-plot present');
  assert(dagSvg.length === 1, 'compact lifecycle DAG present');
  // the dot-plot dots carry semantic classes that the CSS themes per theme.
  const dots = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('dj-dot ') || (n.getAttribute('class') || '') === 'dj-dot');
  assert(dots.length >= 1, 'dot-plot draws value dots (themed in all 3 themes)');
  freshState();
  await cand.render(host, ctx, { gen: 'v1', entry: 'waffles_single' });
  assert(host.textContent.includes('predicate returned False'), 'entry drill-down shows the expectation detail');
});

// ---- (b) cold deep-link run/transcript renders content, not empty ---

test('run view: a COLD deep-link fetches the conversation and renders the transcript', async () => {
  freshState();
  installFetch();
  const runView = await import('../js/variants/J/views/run.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await runView.render(host, ctx, { gen: 'v1', entry: 'waffles_single' });
  const scroller = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'dj-transcript')[0];
  assert(scroller, 'the transcript scroll container rendered');
  const turns = scroller.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('dj-turn dj-turn-'));
  assert(turns.length === 2, `transcript shows its turns (saw ${turns.length})`);
  assert(host.textContent.includes('Drafting an outline'), 'turn text rendered from /api/conversation');
  assert(host.textContent.includes('omitted the requested structure'), 'drift annotation rendered');
});

test('run view: a deep-link to a missing run id degrades to an honest empty state', async () => {
  freshState();
  installFetch();
  const runView = await import('../js/variants/J/views/run.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await runView.render(host, ctx, { gen: 'v1', entry: 'does_not_exist' });
  assert(host.textContent.toLowerCase().includes('no run id'), 'honest empty for an unknown entry');
});

// ---- (c) NEW: mutation-site × generation matrix + drill -------------

test('mutations view: renders the site × generation matrix and drills to a patch diff', async () => {
  freshState();
  installFetch();
  const mut = await import('../js/variants/J/views/mutations.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await mut.render(host, ctx, {});
  const tables = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'dj-mtx');
  assert(tables.length === 1, 'the mutation matrix table rendered');
  // two sites → two body rows; v1 patched both, v2 patched one → 3 "on" cells.
  const onCells = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dj-mtx-on'));
  assertEqual(onCells.length, 3, 'three filled (patched) cells across the matrix');
  assert(host.textContent.includes('coordinator.py'), 'a site label shows the file:line');

  // drill into one pinned site → its patch diff renders the new content.
  freshState();
  const host2 = document.createElement('div');
  await mut.render(host2, ctx, { mutId: 'coordinator.system' });
  assert(host2.textContent.includes('enforce explicit slide-structure'), 'v1 patch new_content rendered in the diff');
  assert(host2.textContent.includes('Tighten the coordinator oversight'), 'v2 patch new_content rendered in the diff');
  const diffBodies = host2.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'dj-diff-body');
  assert(diffBodies.length >= 1, 'the patch drill-down renders a themed line diff');
});

// ---- (d) NEW: ACM-style epoch publication from analysis_md ----------

test('report view: renders the ACM publication from analysis_md with a live figure', async () => {
  freshState();
  installFetch();
  const report = await import('../js/variants/J/views/report.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await report.render(host, ctx, { epochId: EPOCH_ID });
  const paper = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'dj-paper')[0];
  assert(paper, 'the typeset paper rendered');
  // markers parsed: eyebrow, title, meta, abstract, callout.
  assert(host.querySelectorAll('[class]').some((n) => (n.getAttribute('class') || '') === 'dj-paper-eyebrow'), 'eyebrow marker parsed');
  assert(host.querySelectorAll('[class]').some((n) => (n.getAttribute('class') || '') === 'dj-paper-title'), 'title rendered');
  assert(host.querySelectorAll('[class]').some((n) => (n.getAttribute('class') || '') === 'dj-paper-meta'), 'meta marker parsed');
  assert(host.querySelectorAll('[class]').some((n) => (n.getAttribute('class') || '').includes('dj-paper-abstract')), 'abstract section set apart');
  assert(host.querySelectorAll('[class]').some((n) => (n.getAttribute('class') || '') === 'dj-paper-callout'), 'callout marker parsed');
  // the FIGURE:LINEAGE marker embeds a live bumps chart inline.
  const figs = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'dj-paper-fig');
  assert(figs.length === 1, 'one inline figure embedded');
  const figBumps = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'dj-bumps');
  assert(figBumps.length === 1, 'the lineage figure is a LIVE Tufte bumps chart');
  assert(host.textContent.includes('Both challengers fell to the seed'), 'the title text from analysis_md');
});

test('report view: a missing analysis degrades to an honest "not built yet" state', async () => {
  freshState();
  installFetch();
  const report = await import('../js/variants/J/views/report.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  // an epoch id with no analysis fixture → 404 → empty md → honest state.
  await report.render(host, ctx, { epochId: 'no_such_epoch' });
  assert(host.textContent.toLowerCase().includes('no analysis report built yet'), 'honest not-built state');
});

// ---- (g) the three-theme switcher (incl. monokai default) -----------

test('shell: monokai is the default theme and the switcher restyles the root', async () => {
  freshState();
  const shell = await import('../js/variants/J/shell.js');
  // All three themes ship; monokai is first / J's default.
  assertEqual(shell.THEMES[0], 'monokai', 'monokai is first / default');
  assert(shell.THEMES.includes('solarized-dark') && shell.THEMES.includes('solarized-light'), 'all three themes present');
  // The switcher swaps [data-j-theme] on the root — the single attribute the
  // whole console.css keys on, so one swap restyles every mark. We target a
  // root directly (applyTheme's rootEl arg) to avoid booting the SSE loop.
  const root = document.createElement('div');
  shell.applyTheme('monokai', root);
  assertEqual(root.getAttribute('data-j-theme'), 'monokai', 'monokai applies');
  shell.applyTheme('solarized-light', root);
  assertEqual(root.getAttribute('data-j-theme'), 'solarized-light', 'switcher swaps to solarized-light');
  shell.applyTheme('solarized-dark', root);
  assertEqual(root.getAttribute('data-j-theme'), 'solarized-dark', 'switcher swaps to solarized-dark');
  // an unknown theme falls back to monokai (never an undefined attribute).
  shell.applyTheme('bogus', root);
  assertEqual(root.getAttribute('data-j-theme'), 'monokai', 'unknown theme falls back to monokai default');
});

await run();
