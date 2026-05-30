// test/v2_skeleton.test.mjs — the v2 foundation skeleton.
//
// Pins the v2 backbone (DASHBOARD-V2 §4.6 + §5 + §6):
//   * the v2 router — fragment parsing for every route + the v1/v2
//     prefix isolation + v2Href round-trips.
//   * stateBlock — the four honest states (not_yet / running / empty /
//     broken), running's {done,total} progress, broken's reason.
//   * trajectory — the scale-robust spine: a SINGLE node renders a
//     centered, LABELED fallback (NOT empty space with a stray glyph —
//     the v1 lineageRibbon bug), and N nodes plot the full curve.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

const {
  parseV2Hash, v2Href, V2_VIEWS, V2_MODE, V2_DEFAULT_VIEW,
} = await import('../js/v2/router.js');
const { stateBlock, normalizeKind } = await import('../js/v2/components/stateBlock.js');
const {
  trajectory, computeTrajectoryLayout,
} = await import('../js/v2/components/trajectory.js');

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
function allText(node) {
  return node && node.textContent ? node.textContent : '';
}

// ===========================================================================
// Router
// ===========================================================================

test('router: empty / non-v2 / unknown fragment resolves to the default view', () => {
  assertEqual(parseV2Hash('').view, V2_DEFAULT_VIEW);
  assertEqual(parseV2Hash('#/v2').view, V2_DEFAULT_VIEW);
  assertEqual(parseV2Hash('#/v2/nope').view, V2_DEFAULT_VIEW);
  // A bare v1-style fragment (no v2 prefix, unknown to v2) → default.
  assertEqual(parseV2Hash('#/overview').view, 'overview');
});

test('router: parses every v2 view + its params', () => {
  assertEqual(parseV2Hash('#/v2/overview').view, 'overview');
  assertEqual(parseV2Hash('#/v2/bench').view, 'bench');

  const ep = parseV2Hash('#/v2/epoch/2026-05-30_e0');
  assertEqual(ep.view, 'epoch');
  assertEqual(ep.params.epochId, '2026-05-30_e0');

  const exp = parseV2Hash('#/v2/experiment/v3');
  assertEqual(exp.view, 'experiment');
  assertEqual(exp.params.generationId, 'v3');

  const run1 = parseV2Hash('#/v2/run/waffles_single');
  assertEqual(run1.view, 'run');
  assertEqual(run1.params.entryId, 'waffles_single');
  const run2 = parseV2Hash('#/v2/run/waffles_single/v3');
  assertEqual(run2.params.entryId, 'waffles_single');
  assertEqual(run2.params.generationId, 'v3');

  const rep = parseV2Hash('#/v2/report/2026-05-30_e0');
  assertEqual(rep.view, 'report');
  assertEqual(rep.params.epochId, '2026-05-30_e0');
});

test('router: decodes encoded segments + tolerates a malformed one', () => {
  // A percent-encoded id (incl. an encoded slash) stays ONE segment and
  // decodes whole — the split happens on literal '/', not encoded ones.
  const r = parseV2Hash('#/v2/experiment/' + encodeURIComponent('v 3/x'));
  assertEqual(r.params.generationId, 'v 3/x', 'encoded segment decodes whole');
  // A raw percent that is not valid encoding must not throw.
  const bad = parseV2Hash('#/v2/epoch/%zz');
  assertEqual(bad.view, 'epoch');
  assertEqual(bad.params.epochId, '%zz');
});

test('router: v2Href round-trips through parseV2Hash', () => {
  for (const v of V2_VIEWS) {
    const href = v2Href(v);
    assert(href.startsWith('#/v2/'), `${v} href must carry the v2 prefix: ${href}`);
    assertEqual(parseV2Hash(href).view, v);
  }
  const h = v2Href('experiment', 'v3');
  assertEqual(parseV2Hash(h).params.generationId, 'v3');
  // An unknown view falls back to the default in the href.
  assert(v2Href('bogus').startsWith('#/v2/' + V2_DEFAULT_VIEW));
});

test('router: bench is the only live mode; everything else is notebook', () => {
  assertEqual(V2_MODE.bench, 'bench');
  for (const v of V2_VIEWS) {
    if (v === 'bench') continue;
    assertEqual(V2_MODE[v], 'notebook', `${v} must be a notebook view`);
  }
});

// ===========================================================================
// stateBlock — the four honest states
// ===========================================================================

test('stateBlock: not_yet renders the queued state with its glyph + label', () => {
  const n = stateBlock('not_yet');
  assertEqual(n.getAttribute('data-kind'), 'not_yet');
  assert(descendantsWithClass(n, 'v2-state-glyph').length === 1, 'has a glyph (redundant to color)');
  assert(allText(n).toLowerCase().includes('queued'), 'reads as queued');
});

test('stateBlock: running shows N/M progress + a fractional bar', () => {
  const n = stateBlock('running', { done: 7, total: 14 });
  assertEqual(n.getAttribute('data-kind'), 'running');
  assert(allText(n).includes('7/14'), 'shows done/total');
  const bars = descendantsWithClass(n, 'v2-state-progress');
  assertEqual(bars.length, 1, 'has a progress bar when total is known');
  const fill = descendantsWithClass(n, 'v2-state-progress-fill')[0];
  assert(fill.getAttribute('style').includes('50.0%'), `7/14 → 50% fill, got ${fill.getAttribute('style')}`);
  assertEqual(n.getAttribute('aria-busy'), 'true', 'running is aria-busy');
});

test('stateBlock: running without a total stays honest (no bar, no false count)', () => {
  const n = stateBlock('running');
  assertEqual(n.getAttribute('data-kind'), 'running');
  assertEqual(descendantsWithClass(n, 'v2-state-progress').length, 0, 'no bar without a total');
  assert(allText(n).toLowerCase().includes('running'));
});

test('stateBlock: empty is genuinely-nothing, distinct from not_yet', () => {
  const n = stateBlock('empty');
  assertEqual(n.getAttribute('data-kind'), 'empty');
  assert(n.getAttribute('data-kind') !== 'not_yet', 'empty is its own state');
});

test('stateBlock: broken surfaces the reason verbatim + is an alert', () => {
  const reason = 'HTTP 500: analyzer crashed on /api/epoch/e0/analysis';
  const n = stateBlock('broken', { reason });
  assertEqual(n.getAttribute('data-kind'), 'broken');
  assertEqual(n.getAttribute('role'), 'alert', 'broken is an alert for a11y');
  assert(allText(n).includes(reason), 'the reason is shown verbatim');
});

test('stateBlock: an unknown kind degrades to broken (never a silent blank)', () => {
  assertEqual(normalizeKind('wat'), 'broken');
  assertEqual(stateBlock('wat').getAttribute('data-kind'), 'broken');
});

// ===========================================================================
// trajectory — scale robustness (the v1 single-node bug)
// ===========================================================================

test('trajectory: zero nodes renders a LABELED empty state, never blank', () => {
  const n = trajectory({ nodes: [] });
  assertEqual(n.getAttribute('data-mode'), 'empty');
  assert(allText(n).trim().length > 0, 'empty state carries a message, not blank space');
});

test('trajectory: ONE node renders a centered, LABELED solo node (not a stray glyph)', () => {
  const n = trajectory({ nodes: [{ id: 'v0', scalar: 0.42, verdict: 'open', live: true }] });
  assertEqual(n.getAttribute('data-mode'), 'solo', 'single node uses the solo fallback');
  // The node itself is present and clickable.
  const nodes = descendantsWithClass(n, 'v2-traj-node');
  assertEqual(nodes.length, 1, 'exactly one drawn node');
  // It is LABELED — the id and the scalar are both shown (the v1 bug was
  // a lone unlabeled dot floating in empty space).
  const txt = allText(n);
  assert(txt.includes('v0'), 'solo node shows its id');
  assert(txt.includes('0.420'), 'solo node shows its scalar');
  // A hint frames it as the start of a trajectory — not empty space.
  assert(descendantsWithClass(n, 'v2-trajectory-solo-hint').length === 1, 'solo has a framing hint');
  // The solo layout is reported by the pure layout fn too.
  const layout = computeTrajectoryLayout([{ id: 'v0', scalar: 0.42 }]);
  assertEqual(layout.mode, 'solo');
  assertEqual(String(layout.node.id), 'v0');
});

test('trajectory: a single live mid-first-run node still draws + labels', () => {
  // The exact case v1 botched: one node, mid-first-run, live.
  const n = trajectory({ nodes: [{ id: 'v1', scalar: null, verdict: 'open', live: true }] });
  assertEqual(n.getAttribute('data-mode'), 'solo');
  const dots = descendantsWithClass(n, 'v2-traj-dot');
  assertEqual(dots.length, 1, 'the live node is drawn');
  assert(descendantsWithClass(n, 'v2-traj-node')[0].getAttribute('data-live') === 'true',
    'the node is marked live (pulse gated by reduced-motion in CSS)');
});

test('trajectory: degenerate scalar domain (all equal) falls back to solo, not a flat axis', () => {
  const layout = computeTrajectoryLayout([
    { id: 'a', scalar: 0.5, verdict: 'promoted' },
    { id: 'b', scalar: 0.5, verdict: 'promoted' },
  ]);
  assertEqual(layout.mode, 'solo', 'equal scalars → no meaningful curve → solo');
});

test('trajectory: N nodes plot the full curve with a spine path + branches', () => {
  const nodes = [
    { id: 'v0', parentId: null, scalar: 0.80, verdict: 'promoted' },
    { id: 'v1', parentId: 'v0', scalar: 0.60, verdict: 'promoted' },
    { id: 'v2', parentId: 'v1', scalar: 0.95, verdict: 'rejected' }, // challenger → branch
    { id: 'v3', parentId: 'v1', scalar: 0.45, verdict: 'promoted' },
  ];
  const layout = computeTrajectoryLayout(nodes);
  assertEqual(layout.mode, 'plot');
  assertEqual(layout.columns.length, 3, 'promoted lineage = 3 spine columns');
  assertEqual(layout.branches.length, 1, 'the rejected challenger branches off its parent');
  assertEqual(String(layout.branches[0].parent.node.id), 'v1', 'branch hangs off v1');
  assert(layout.spinePath.length > 0, 'a spine connector path is drawn');
  // Lower loss sits HIGHER on screen: v3 (0.45, best) above v0 (0.80, worst).
  const byId = {};
  for (const c of layout.columns) byId[c.node.id] = c;
  assert(byId.v3.y < byId.v0.y, 'lower scalar (v3) sits higher (smaller y) than v0');

  // The rendered node is clickable → onSelect(id).
  let picked = null;
  const dom = trajectory({ nodes, onSelect: (id) => { picked = id; } });
  assertEqual(dom.getAttribute('data-mode'), 'plot');
  const first = descendantsWithClass(dom, 'v2-traj-node')[0];
  first.dispatchEvent(makeEvent('click', { preventDefault() {} }));
  assert(picked != null, 'clicking a node fires onSelect with its id');
});

test('trajectory: the live node marks the spine for the pulse', () => {
  const nodes = [
    { id: 'v0', parentId: null, scalar: 0.8, verdict: 'promoted' },
    { id: 'v1', parentId: 'v0', scalar: 0.5, verdict: 'open', live: true },
  ];
  const dom = trajectory({ nodes });
  const live = descendantsWithClass(dom, 'v2-traj-node').filter(
    (x) => x.getAttribute('data-live') === 'true');
  assertEqual(live.length, 1, 'exactly one live node is marked');
});

await run();
