// test/variant_e.test.mjs — Variant E ("Atlas") unit tests.
//
// Atlas is a synthesis (A nav + D data-viz + C diagrams + B/D theming).
// These tests pin the pieces that carry the synthesis AND the two render
// guarantees the brief calls out explicitly:
//   (a) digest-gated repaint — a second render with identical data (and a
//       heartbeat-only state change) does NOT rebuild the DOM;
//   (b) a COLD deep-link run/transcript render — opening #/E/run/<gen>/<entry>
//       directly fetches the run_id then the conversation and paints the
//       transcript content (never an empty panel).
// It also covers the router (A-style IA + breadcrumb), the compact lifecycle
// DAG (C-style), and the gatedSwap no-flash helper.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/variants/E/router.js');
const dag = await import('../js/variants/E/dag.js');
const ui = await import('../js/variants/E/ui.js');
const svg = await import('../js/variants/E/svg.js');
const data = await import('../js/variants/E/data.js');
const dom = await import('../js/core/dom.js');

// ---- the live fixture (the exact shape the brief pins: one epoch, v0
//      crowned, v1/v2 rejected, all board entries fail) -----------------
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
};
// per-entry per generation
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
  // Reset the data-layer cache so each view test reads the fixture fresh.
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
}

// ---- router (A-style IA + breadcrumb) -------------------------------

test('router: parses the Atlas-prefixed hashes', () => {
  assertEqual(router.parseRoute('#/E/').view, 'home');
  assertEqual(router.parseRoute('#/E/epoch').view, 'epoch');
  assertEqual(router.parseRoute('#/E/matchups').view, 'matchups');
  const c = router.parseRoute('#/E/candidate/v1/waffles_single');
  assertEqual(c.view, 'candidate');
  assertEqual(c.params.gen, 'v1');
  assertEqual(c.params.entry, 'waffles_single');
  const r = router.parseRoute('#/E/run/v1/waffles_single');
  assertEqual(r.view, 'run');
  assertEqual(r.params.gen, 'v1');
  assertEqual(r.params.entry, 'waffles_single');
});

test('router: a foreign / empty hash defaults to home', () => {
  assertEqual(router.parseRoute('').view, 'home');
  assertEqual(router.parseRoute('#/something').view, 'home');
  assertEqual(router.parseRoute('#/E/bogus').view, 'home');
});

test('router: href round-trips through parseRoute', () => {
  const h = router.href('run', { gen: 'v2', entry: 'q3' });
  const back = router.parseRoute(h);
  assertEqual(back.view, 'run');
  assertEqual(back.params.gen, 'v2');
  assertEqual(back.params.entry, 'q3');
});

test('router: crumbTrail builds an ancestor chain with one current leaf', () => {
  const trail = router.crumbTrail({ view: 'candidate', params: { gen: 'v1', entry: 'waffles_single' } });
  // last crumb is the current leaf; every earlier crumb is a link.
  assert(trail[trail.length - 1].current === true, 'leaf is current');
  assert(trail.slice(0, -1).every((c) => c.view), 'ancestors are links');
  assertEqual(trail[trail.length - 1].label, 'waffles_single');
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

// ---- the compact lifecycle DAG (C-style) ----------------------------

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
  // a timed-out entry reads deferred (caution), a plain fail reads rejected.
  assert(boardNodes.some((n) => (n.getAttribute('class') || '').includes('ez-deferred')), 'timeout → deferred');
  assert(boardNodes.some((n) => (n.getAttribute('class') || '').includes('ez-rejected')), 'fail → rejected');
});

test('lifecycleDag onEntry fires with the clicked board entry id', () => {
  let clicked = null;
  const node = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', decision: 'rejected',
    entries: [{ entry_id: 'waffles', drift_loss: 60.5, pass_fail: 0 }], onEntry: (id) => { clicked = id; } });
  const bn = node.querySelectorAll('[data-cz]').filter((n) => n.getAttribute('data-cz') === 'lc-board-node')[0];
  bn.dispatchEvent({ type: 'click' });
  assertEqual(clicked, 'waffles');
});

// ---- (a) digest-gated repaint: identical data / heartbeat = no rebuild ----

test('home view: a re-render with identical data does NOT rebuild the DOM', async () => {
  freshState();
  installFetch();
  const home = await import('../js/variants/E/views/home.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await home.render(host, ctx, {});
  const digest1 = host.getAttribute('data-e-digest');
  const writes1 = host.innerHTMLWriteCount();
  const firstChild = host.firstChild;
  assert(host.children.length > 0, 'home painted content');
  // A second render with identical fixture data — simulates a heartbeat
  // re-dispatch. The digest must be unchanged AND the host's first child
  // must be the SAME node (no clear-and-rebuild).
  await home.render(host, ctx, {});
  assertEqual(host.getAttribute('data-e-digest'), digest1, 'digest unchanged');
  assert(host.firstChild === firstChild, 'the content host was not rebuilt (same node identity)');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

test('epoch view renders lineage + heatmap + trellis, and is digest-gated', async () => {
  freshState();
  installFetch();
  const epoch = await import('../js/variants/E/views/epoch.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epoch.render(host, ctx, {});
  assert(host.children.length > 0, 'epoch painted content');
  // a bumps chart, a heatmap, and trellis cells are all present.
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

// ---- (b) cold deep-link run/transcript renders content, not empty ----

test('run view: a COLD deep-link fetches the conversation and renders the transcript', async () => {
  freshState();
  installFetch();
  const runView = await import('../js/variants/E/views/run.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  // Cold deep-link: only the URL params (gen + entry), no prior navigation.
  await runView.render(host, ctx, { gen: 'v1', entry: 'waffles_single' });
  // The transcript scroll container exists AND carries turn content.
  const scroller = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'e-transcript')[0];
  assert(scroller, 'the transcript scroll container rendered');
  const turns = scroller.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').startsWith('d-turn d-turn-'));
  assert(turns.length === 2, `transcript shows its turns (saw ${turns.length})`);
  // The actual conversation text is present — not an empty panel.
  assert(host.textContent.includes('Drafting an outline'), 'turn text rendered from /api/conversation');
  assert(host.textContent.includes('omitted the requested structure'), 'drift annotation rendered');
});

test('run view: a deep-link to a missing run id degrades to an honest empty state', async () => {
  freshState();
  installFetch();
  const runView = await import('../js/variants/E/views/run.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await runView.render(host, ctx, { gen: 'v1', entry: 'does_not_exist' });
  // No run id → honest empty, never a blank panel.
  assert(host.textContent.toLowerCase().includes('no run id'), 'honest empty for an unknown entry');
});

// ---- candidate view: dot-plot + DAG + URL-driven drill-down ----------

test('candidate view: per-board dot-plot + lifecycle DAG; entry param drills in', async () => {
  freshState();
  installFetch();
  const cand = await import('../js/variants/E/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await cand.render(host, ctx, { gen: 'v1' });
  const dotplot = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'd-valdot');
  const dagSvg = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '') === 'ez-dag');
  assert(dotplot.length === 1, 'per-board value dot-plot present');
  assert(dagSvg.length === 1, 'compact lifecycle DAG present');
  // Drilling into an entry adds the expectation outcomes.
  freshState();
  await cand.render(host, ctx, { gen: 'v1', entry: 'waffles_single' });
  assert(host.textContent.includes('predicate returned False'), 'entry drill-down shows the expectation detail');
});

await run();
