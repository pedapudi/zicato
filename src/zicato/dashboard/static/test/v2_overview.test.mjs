// test/v2_overview.test.mjs — the Overview view (DASHBOARD-V2 §4.2, §8).
//
// The Overview answers "is the loop progressing & healthy?". This pins:
//   * the loss TRAJECTORY as the hero — nodes built from /api/workspace
//     (best scalar per epoch), drillable to the epoch page, the current
//     epoch live when a run is in flight, net-movement badge.
//   * the HEALTH strip — the loop-health report mapped onto the three
//     signal colors (green/amber/red) with the top finding.
//   * compact IDENTITY/context — root, current epoch, counts (terse).
//   * the LIVE affordance — a "● LIVE → go to the Bench" link only when a
//     tournament is running; absent otherwise.
//   * HONEST states — every async section degrades through stateBlock
//     (loading / empty / broken), never a bare "No data".
//
// The three reads are mocked through globalThis.fetch; we drain the
// microtask + macrotask queues so each section's fetch settles before
// asserting.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const overview = await import('../js/v2/views/overview.js');
const { overviewNodes, trajectoryDelta, healthSignal, renderOverview } = overview;
const { state } = await import('../js/core/state.js');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function descendantsWithClass(node, cls) {
  const out = [];
  const walk = (n) => {
    if (!n || n.nodeType !== 1) return;
    if (n.classList && n.classList.contains(cls)) out.push(n);
    for (const c of n.children) walk(c);
  };
  walk(node);
  return out;
}
function firstWithClass(node, cls) { return descendantsWithClass(node, cls)[0] || null; }
function allText(node) { return node && node.textContent ? node.textContent : ''; }

// Route a mocked fetch by path → payload. A path mapped to the sentinel
// REJECT rejects (an HTTP error); an absent mapping rejects too (so a
// test only wires the endpoints it cares about).
const REJECT = Symbol('reject');
function installFetch(routes) {
  globalThis.fetch = (path) => {
    const p = String(path);
    for (const [key, value] of Object.entries(routes)) {
      if (p === key || p.startsWith(key)) {
        if (value === REJECT) return Promise.reject(new Error(`${p} -> HTTP 500`));
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(value) });
      }
    }
    return Promise.reject(new Error(`${p} -> HTTP 404`));
  };
}

// Let every queued fetch .then() chain settle (microtasks) plus any
// macrotask hop.
async function settle() {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
  await new Promise((r) => setTimeout(r, 0));
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

// A fresh host attached to document.body so the view's getElementById
// lookups (`$('v2-ov-*')`) resolve. Detach any prior host first so the
// view's element ids stay unique in the tree.
function freshHost(opts) {
  const o = opts || {};
  const body = globalThis.document.body;
  if (!o.keepPrior) {
    for (const child of [...body.children]) body.removeChild(child);
  }
  const host = globalThis.document.createElement('div');
  host.setAttribute('id', 'v2-view');
  body.appendChild(host);
  return host;
}

// A representative two-epoch workspace ribbon (the /api/workspace shape).
function workspaceFixture() {
  return {
    current_epoch_id: '2026-05-30_e1',
    epochs: [
      {
        epoch_id: '2026-05-30_e0', goal: 'baseline', best_scalar: 0.80,
        best_generation_id: 'v0', generation_count: 2, promoted_count: 1,
        closed: true, parent_epoch_id: null,
      },
      {
        epoch_id: '2026-05-30_e1', goal: 'tighten structure', best_scalar: 0.55,
        best_generation_id: 'v3', generation_count: 3, promoted_count: 1,
        closed: false, parent_epoch_id: '2026-05-30_e0',
      },
    ],
    sparkline: [
      { epoch_id: '2026-05-30_e0', scalar: 0.80 },
      { epoch_id: '2026-05-30_e1', scalar: 0.55 },
    ],
  };
}

// ===========================================================================
// Pure helpers
// ===========================================================================

test('overviewNodes: one node per epoch, scalar from best_scalar, drill id = epoch id', () => {
  const nodes = overviewNodes(workspaceFixture(), null);
  assertEqual(nodes.length, 2);
  assertEqual(nodes[0].id, '2026-05-30_e0');
  assertEqual(nodes[0].scalar, 0.80);
  assertEqual(nodes[1].parentId, '2026-05-30_e0', 'lineage edge carried from parent_epoch_id');
  // A promoted epoch is on the spine; verdict drives the trajectory glyph.
  assertEqual(nodes[0].verdict, 'promoted');
  // No live run → no live node.
  assert(!nodes.some((n) => n.live), 'no live node without a running tournament');
});

test('overviewNodes: the current epoch pulses live when a tournament is running', () => {
  const t = { phase: 'running', epoch_id: '2026-05-30_e1' };
  const nodes = overviewNodes(workspaceFixture(), t);
  const live = nodes.filter((n) => n.live);
  assertEqual(live.length, 1, 'exactly one live node');
  assertEqual(live[0].id, '2026-05-30_e1', 'the running epoch is the live node');
});

test('overviewNodes: a completed/absent tournament marks nothing live', () => {
  assert(!overviewNodes(workspaceFixture(), { phase: 'completed', epoch_id: '2026-05-30_e1' }).some((n) => n.live));
  assert(!overviewNodes(workspaceFixture(), null).some((n) => n.live));
});

test('overviewNodes: an unscored epoch contributes a null-scalar node (never a fabricated number)', () => {
  const ws = { epochs: [{ epoch_id: 'e0', best_scalar: null, generation_count: 1, promoted_count: 0 }] };
  const nodes = overviewNodes(ws, null);
  assertEqual(nodes.length, 1);
  assertEqual(nodes[0].scalar, null);
  assertEqual(nodes[0].verdict, 'open', 'no promotion yet → open');
});

test('trajectoryDelta: net first→last movement; lower-is-better so a descent is negative', () => {
  const d = trajectoryDelta(workspaceFixture());
  assert(d != null && d < 0, `descent should be negative, got ${d}`);
  // Fewer than two finite points → null (no movement to report).
  assertEqual(trajectoryDelta({ sparkline: [{ epoch_id: 'e0', scalar: 0.5 }] }), null);
  assertEqual(trajectoryDelta({ sparkline: [] }), null);
});

test('healthSignal: severity → signal mapping (critical=red, warning=amber, clean=green)', () => {
  assertEqual(healthSignal({ healthy: true, findings: [] }).signal, 'improve');
  assertEqual(healthSignal({
    healthy: false,
    findings: [{ code: 'flat_drift_signal', severity: 'warning', summary: 'drift is flat' }],
  }).signal, 'caution');
  assertEqual(healthSignal({
    healthy: false,
    findings: [{ code: 'degenerate_scoring', severity: 'critical', summary: 'scoring degenerate' }],
  }).signal, 'regress');
});

test('healthSignal: surfaces the MOST SEVERE finding as the top finding', () => {
  const h = healthSignal({
    healthy: false,
    findings: [
      { code: 'a', severity: 'info', summary: 'fyi' },
      { code: 'b', severity: 'critical', summary: 'the bad one' },
      { code: 'c', severity: 'warning', summary: 'meh' },
    ],
  });
  assertEqual(h.signal, 'regress');
  assertEqual(h.finding, 'the bad one', 'top finding is the critical summary');
  assertEqual(h.count, 3);
});

// ===========================================================================
// Render — the four sections, honest states, drillability
// ===========================================================================

test('render: the hero plots the trajectory; nodes drill to the epoch page', async () => {
  state.activeTournament = null;
  state.workspace = '/home/sunil/lab/.zicato';
  installFetch({
    '/api/workspace': workspaceFixture(),
    '/api/health-report': { epoch_id: '2026-05-30_e1', healthy: true, findings: [] },
    '/api/active-tournament': REJECT, // no live run (absent file)
  });
  const host = freshHost();
  renderOverview(host, { view: 'overview', params: {} });
  await settle();

  // The hero rendered a trajectory (not a stateBlock).
  const traj = firstWithClass(host, 'v2-trajectory');
  assert(traj != null, 'the hero plots the trajectory primitive');
  // Two epochs + a non-degenerate scalar domain → the full plot.
  assertEqual(traj.getAttribute('data-mode'), 'plot');

  // Clicking a node drills to that epoch via the router (sets the hash).
  const node = firstWithClass(host, 'v2-traj-node');
  assert(node != null, 'a clickable trajectory node exists');
  globalThis.window.location.hash = '';
  node.dispatchEvent(makeEvent('click', { preventDefault() {} }));
  assert(globalThis.window.location.hash.startsWith('#/v2/epoch/'),
    `node click drills to an epoch route, got ${globalThis.window.location.hash}`);

  // Net-movement badge reads as a descent (green/improve).
  const delta = firstWithClass(host, 'v2-ov-delta');
  assertEqual(delta.getAttribute('data-signal'), 'improve', 'net descent reads improve');
});

test('render: the health strip carries the signal color + the finding', async () => {
  state.activeTournament = null;
  installFetch({
    '/api/workspace': workspaceFixture(),
    '/api/health-report': {
      epoch_id: '2026-05-30_e1', healthy: false,
      findings: [{ code: 'flat_drift_signal', severity: 'warning', summary: 'drift signal is flat across entries' }],
    },
    '/api/active-tournament': REJECT,
  });
  const host = freshHost();
  renderOverview(host, { view: 'overview', params: {} });
  await settle();

  const strip = firstWithClass(host, 'v2-ov-health');
  assert(strip != null, 'a health strip rendered');
  assertEqual(strip.getAttribute('data-signal'), 'caution', 'a warning maps to amber/caution');
  assert(allText(strip).includes('drift signal is flat'), 'the finding summary is shown');
  // The finding is a door to its epoch.
  const link = firstWithClass(host, 'v2-ov-health-link');
  assert(link != null && link.getAttribute('href').startsWith('#/v2/epoch/'), 'finding drills to its epoch');
});

test('render: a healthy report reads green with a plain no-issues line', async () => {
  state.activeTournament = null;
  installFetch({
    '/api/workspace': workspaceFixture(),
    '/api/health-report': { epoch_id: '2026-05-30_e1', healthy: true, findings: [] },
    '/api/active-tournament': REJECT,
  });
  const host = freshHost();
  renderOverview(host, { view: 'overview', params: {} });
  await settle();
  const strip = firstWithClass(host, 'v2-ov-health');
  assertEqual(strip.getAttribute('data-signal'), 'improve');
  assert(allText(strip).toLowerCase().includes('no issues'), 'clean health reads plainly');
});

test('render: compact context shows root, current epoch + counts', async () => {
  state.activeTournament = null;
  state.workspace = '/home/sunil/lab/.zicato';
  installFetch({
    '/api/workspace': workspaceFixture(),
    '/api/health-report': { epoch_id: '2026-05-30_e1', healthy: true, findings: [] },
    '/api/active-tournament': REJECT,
  });
  const host = freshHost();
  renderOverview(host, { view: 'overview', params: {} });
  await settle();

  const facts = firstWithClass(host, 'v2-ov-facts');
  assert(facts != null, 'the context fact list rendered');
  const txt = allText(facts);
  assert(txt.includes('/home/sunil/lab/.zicato'), 'shows the workspace root');
  assert(txt.includes('2026-05-30_e1'), 'shows the current epoch');
  // The values are read off the labeled <dd> cells so a coincidental
  // substring match elsewhere (e.g. inside an epoch id) cannot pass it.
  const vals = descendantsWithClass(facts, 'v2-ov-fact-val').map((d) => allText(d).trim());
  assert(vals.includes('2'), `2 epochs / 2 promoted is a fact value, got ${JSON.stringify(vals)}`);
  assert(vals.includes('5'), `2+3 = 5 generations is a fact value, got ${JSON.stringify(vals)}`);
});

test('render: a LIVE run shows the prominent go-to-the-Bench affordance', async () => {
  state.activeTournament = null;
  installFetch({
    '/api/workspace': workspaceFixture(),
    '/api/health-report': { epoch_id: '2026-05-30_e1', healthy: true, findings: [] },
    '/api/active-tournament': {
      phase: 'running', epoch_id: '2026-05-30_e1',
      parent_generation_id: 'v0', child_generation_id: 'v3',
      round_index: 0, total_rounds: 1, entries: [],
    },
  });
  const host = freshHost();
  renderOverview(host, { view: 'overview', params: {} });
  await settle();

  const cta = firstWithClass(host, 'v2-ov-live-cta');
  assert(cta != null, 'the live CTA is present during a running tournament');
  assertEqual(cta.getAttribute('href'), '#/v2/bench', 'the CTA points at the Bench (does not duplicate it)');
  assert(allText(cta).includes('LIVE'), 'reads LIVE');
  assert(allText(cta).includes('v0 → v3'), 'shows the champion → challenger matchup');
  assert(allText(cta).toLowerCase().includes('round 1/1'), 'shows the round context');
});

test('render: NO live affordance when nothing is in flight', async () => {
  state.activeTournament = null;
  installFetch({
    '/api/workspace': workspaceFixture(),
    '/api/health-report': { epoch_id: '2026-05-30_e1', healthy: true, findings: [] },
    '/api/active-tournament': REJECT,
  });
  const host = freshHost();
  renderOverview(host, { view: 'overview', params: {} });
  await settle();
  assertEqual(descendantsWithClass(host, 'v2-ov-live-cta').length, 0, 'no CTA when idle');
});

test('render: an empty workspace is honest (not_yet), never a bare "No data"', async () => {
  state.activeTournament = null;
  installFetch({
    '/api/workspace': { current_epoch_id: null, epochs: [], sparkline: [] },
    '/api/health-report': { epoch_id: null, healthy: true, findings: [] },
    '/api/active-tournament': REJECT,
  });
  const host = freshHost();
  renderOverview(host, { view: 'overview', params: {} });
  await settle();

  // The hero falls back to a not_yet stateBlock; the context to empty.
  const heroBody = host.querySelector ? null : null; // (no :scope query needed)
  const states = descendantsWithClass(host, 'v2-state');
  assert(states.length >= 1, 'honest stateBlock(s) rendered for the empty workspace');
  assert(!allText(host).toLowerCase().includes('no data'), 'never a bare "No data"');
  // The hero specifically says not-yet (queued), not "broken".
  const notYet = states.filter((s) => s.getAttribute('data-kind') === 'not_yet');
  assert(notYet.length >= 1, 'the empty hero reads not-yet');
});

test('render: a broken workspace read surfaces a broken state with the reason', async () => {
  state.activeTournament = null;
  installFetch({
    '/api/workspace': REJECT,
    '/api/health-report': { epoch_id: 'e0', healthy: true, findings: [] },
    '/api/active-tournament': REJECT,
  });
  const host = freshHost();
  renderOverview(host, { view: 'overview', params: {} });
  await settle();

  const broken = descendantsWithClass(host, 'v2-state').filter((s) => s.getAttribute('data-kind') === 'broken');
  assert(broken.length >= 1, 'a failed workspace read shows a broken state');
  assert(allText(host).includes('HTTP 500'), 'the failure reason is surfaced verbatim');
});

test('render: a stale fetch landing after a re-render does not write the old host', async () => {
  // First render against host A with a SLOW workspace read; before it
  // settles, re-render against host B. A must stay empty of a trajectory.
  state.activeTournament = null;
  let resolveA;
  globalThis.fetch = (path) => {
    const p = String(path);
    if (p.startsWith('/api/workspace')) {
      return new Promise((res) => { resolveA = () => res({ ok: true, status: 200, json: () => Promise.resolve(workspaceFixture()) }); });
    }
    if (p.startsWith('/api/health-report')) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ healthy: true, findings: [] }) });
    return Promise.reject(new Error(`${p} -> HTTP 404`));
  };
  const hostA = freshHost();
  renderOverview(hostA, { view: 'overview', params: {} });
  // Re-render (new generation) before A's workspace resolves.
  const hostB = freshHost();
  renderOverview(hostB, { view: 'overview', params: {} });
  // Now let A's workspace land late.
  if (resolveA) resolveA();
  await settle();
  // A's hero body must NOT have been overwritten with the late trajectory.
  assertEqual(descendantsWithClass(hostA, 'v2-trajectory').length, 0,
    'a stale fetch does not paint the swapped-away host');
});

await run();
