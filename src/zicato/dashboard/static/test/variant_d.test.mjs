// test/variant_d.test.mjs — Variant D ("Tufte data-viz") unit tests.
//
// Exercises the pure pieces of the variant against the dependency-free
// harness DOM: the SVG primitive library (svg.js), the non-collision
// guarantee that makes the slopegraph "done right", the hash router,
// the safe markdown renderer for the proposer brief, and the cached
// data layer's failure tolerance. The interactive views compose these,
// so pinning the primitives pins the variant.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const svg = await import('../js/variants/D/svg.js');
const router = await import('../js/variants/D/router.js');
const ui = await import('../js/variants/D/ui.js');

// ---- numeric helpers ------------------------------------------------

test('isNum rejects NaN / Infinity / non-numbers', () => {
  assert(svg.isNum(0.5));
  assert(!svg.isNum(NaN));
  assert(!svg.isNum(Infinity));
  assert(!svg.isNum('3'));
  assert(!svg.isNum(null));
});

test('extent ignores gaps and pads a flat series', () => {
  const [lo, hi] = svg.extent([1, null, 3, NaN, 2]);
  assertEqual(lo, 1); assertEqual(hi, 3);
  const [flo, fhi] = svg.extent([5, 5, 5]);
  assert(flo < 5 && fhi > 5, 'flat series must be padded so it is drawable');
});

test('fmtSigned signs and fixes; fmt sentinels NaN', () => {
  assertEqual(svg.fmtSigned(0.5), '+0.500');
  assertEqual(svg.fmtSigned(-0.25, 2), '-0.25');
  assertEqual(svg.fmt(NaN), '—');
});

// ---- decollide: the non-collision guarantee -------------------------

test('decollide keeps a minimum gap between every neighbour', () => {
  // Three labels whose natural y positions overlap badly.
  const y = (v) => v; // identity scale for the test
  const items = [{ v: 100 }, { v: 102 }, { v: 103 }];
  const out = svg.decollide(items, y, 13, 0, 400);
  // Sort the OUTPUT and assert every adjacent pair clears the min gap.
  const sorted = [...out].sort((a, b) => a - b);
  for (let i = 1; i < sorted.length; i++) {
    assert(sorted[i] - sorted[i - 1] >= 13 - 1e-6,
      `labels collide: ${sorted[i - 1]} → ${sorted[i]}`);
  }
});

test('decollide preserves input order in its return array', () => {
  const y = (v) => v;
  const items = [{ v: 300 }, { v: 100 }, { v: 305 }];
  const out = svg.decollide(items, y, 13, 0, 400);
  // index 1 (value 100) must be the smallest output position.
  assert(out[1] < out[0] && out[1] < out[2], 'order must be preserved by index');
});

test('decollide clamps inside [top, bottom]', () => {
  const y = (v) => v;
  const items = Array.from({ length: 8 }, () => ({ v: 50 }));
  const out = svg.decollide(items, y, 13, 10, 90);
  for (const p of out) assert(p >= 10 - 1e-6 && p <= 90 + 1e-6, 'must clamp into frame');
});

// ---- sparkline ------------------------------------------------------

test('sparkline draws an empty rule for a gappy/empty series', () => {
  const node = svg.sparkline({ values: [null, NaN] });
  assertEqual(node.localName, 'svg');
  const empty = node.querySelector('[class]');
  // The only child is the empty-rule line.
  assert(node.children.length === 1, 'empty series → single rule');
});

test('sparkline breaks the line at gaps (no interpolated lie)', () => {
  const node = svg.sparkline({ values: [1, null, 3] });
  const path = [...node.children].find((c) => c.localName === 'path');
  assert(path, 'a finite series must draw a path');
  const d = path.getAttribute('d');
  // Two M commands → the gap broke the pen, so it is not one line.
  const moves = (d.match(/M/g) || []).length;
  assert(moves >= 2, `gap must break the line, saw d="${d}"`);
});

test('sparkline end-dot colours by good direction (down = improved)', () => {
  const improving = svg.sparkline({ values: [3, 2, 1], goodDirection: 'down' });
  const dot = [...improving.children].find((c) => c.localName === 'circle');
  assert(dot.getAttribute('class').includes('d-good'), 'falling loss should read good');
  const regressing = svg.sparkline({ values: [1, 2, 3], goodDirection: 'down' });
  const dot2 = [...regressing.children].find((c) => c.localName === 'circle');
  assert(dot2.getAttribute('class').includes('d-bad'), 'rising loss should read bad');
});

// ---- dotPlot --------------------------------------------------------

test('dotPlot has a zero rule and one dot per finite item', () => {
  const node = svg.dotPlot({ items: [{ label: 'a', value: -0.2 }, { label: 'b', value: 0.3 }] });
  const zero = [...node.children].find((c) => c.getAttribute('class') === 'd-zero-rule');
  assert(zero, 'must draw a zero rule');
  const dots = node.querySelectorAll('[class]').filter((c) => c.localName === 'circle');
  assertEqual(dots.length, 2);
});

test('dotPlot fires onClick with the bound item', () => {
  let clicked = null;
  const node = svg.dotPlot({ items: [{ label: 'x', value: 1, id: 'x' }], onClick: (it) => { clicked = it; } });
  const row = [...node.children].find((c) => c.localName === 'g');
  row.dispatchEvent({ type: 'click' });
  assert(clicked && clicked.id === 'x', 'click must pass the item through');
});

// ---- slopegraph -----------------------------------------------------

test('slopegraph draws a line per series and an empty label when blank', () => {
  const blank = svg.slopegraph({ series: [] });
  assert(blank.querySelector('[class]'), 'blank slope still renders a node');
  const node = svg.slopegraph({
    series: [{ label: 'g1', a: 1, b: 0.5 }, { label: 'g2', a: 0.5, b: 0.7 }],
  });
  const lines = node.querySelectorAll('[class]').filter((c) => (c.getAttribute('class') || '').startsWith('d-slope-line'));
  assertEqual(lines.length, 2);
  // Direction colouring: improving (1→0.5) good, regressing (0.5→0.7) bad.
  assert(lines.some((l) => l.getAttribute('class').includes('d-good')));
  assert(lines.some((l) => l.getAttribute('class').includes('d-bad')));
});

// ---- bumps ----------------------------------------------------------

test('bumps separates promoted (spine) from rejected (challenger) lanes', () => {
  const node = svg.bumps({
    nodes: [
      { id: 'v0', x: 0, promoted: true },
      { id: 'v1', x: 1, promoted: false, parent: 'v0' },
      { id: 'v2', x: 2, promoted: true, parent: 'v0' },
    ],
  });
  const promoted = node.querySelectorAll('[class]').filter((c) => (c.getAttribute('class') || '').includes('d-promoted'));
  const rejected = node.querySelectorAll('[class]').filter((c) => (c.getAttribute('class') || '').includes('d-rejected'));
  assertEqual(promoted.length, 2);
  assertEqual(rejected.length, 1);
});

// ---- predictedActual ------------------------------------------------

test('predictedActual draws predicted target and actual dot', () => {
  const node = svg.predictedActual({ predicted: -0.1, actual: -0.3, label: 'scalar' });
  const pred = node.querySelectorAll('[class]').filter((c) => (c.getAttribute('class') || '') === 'd-pva-pred');
  const act = node.querySelectorAll('[class]').filter((c) => (c.getAttribute('class') || '').startsWith('d-pva-actual'));
  assertEqual(pred.length, 1);
  assertEqual(act.length, 1);
  assert(act[0].getAttribute('class').includes('d-good'), 'a negative actual is an improvement');
});

// ---- heatmap --------------------------------------------------------

test('heatmap renders rows × cols cells and tolerates nulls', () => {
  const node = svg.heatmap({
    rows: [{ id: 'e1', label: 'e1' }, { id: 'e2', label: 'e2' }],
    cols: [{ id: 'g0', label: 'g0' }, { id: 'g1', label: 'g1' }],
    value: (r, c) => (r === 'e1' && c === 'g0' ? 0.5 : null),
  });
  const cells = node.querySelectorAll('[class]').filter((c) => c.getAttribute('class') === 'd-hm-cell');
  assertEqual(cells.length, 4, '2×2 grid → 4 cells');
});

// ---- router ---------------------------------------------------------

test('parseRoute maps the D-prefixed hashes', () => {
  assertEqual(router.parseRoute('#/D/').view, 'environment');
  assertEqual(router.parseRoute('#/D/epoch').view, 'epoch');
  assertEqual(router.parseRoute('#/D/tournament').view, 'tournament');
  assertEqual(router.parseRoute('#/D/bench').view, 'bench');
  const ex = router.parseRoute('#/D/experiment/v3');
  assertEqual(ex.view, 'experiment');
  assertEqual(ex.params.gen, 'v3');
});

test('parseRoute defaults a foreign / empty hash to environment', () => {
  assertEqual(router.parseRoute('').view, 'environment');
  assertEqual(router.parseRoute('#/something-else').view, 'environment');
  assertEqual(router.parseRoute('#/D/bogus').view, 'environment');
});

test('href round-trips through parseRoute', () => {
  const h = router.href('experiment', { gen: 'v2' });
  assertEqual(router.parseRoute(h).params.gen, 'v2');
});

// ---- ui: verdict + markdown ----------------------------------------

test('normaliseDecision reads tournament_decision / decision', () => {
  assertEqual(ui.normaliseDecision({ tournament_decision: 'PROMOTED' }), 'promoted');
  assertEqual(ui.normaliseDecision({ decision: 'rejected' }), 'rejected');
  assertEqual(ui.normaliseDecision({}), null);
});

test('renderMarkdown renders headings, lists, code without innerHTML', () => {
  const md = '# Goal\nMake it better.\n\n- one\n- two\n\nUse `flag` carefully.';
  const node = ui.renderMarkdown(md);
  // No innerHTML write anywhere in the subtree — the safe-render rule.
  assertEqual(node.innerHTMLWriteCount(), 0, 'markdown must build nodes, never innerHTML');
  const tags = node.children.map((c) => c.localName);
  assert(tags.includes('h1'), 'a # heading becomes h1');
  assert(tags.includes('ul'), 'bullets become a ul');
  const codeText = node.textContent;
  assert(codeText.includes('flag'), 'inline code text survives');
});

test('renderMarkdown gives an honest empty state for a blank brief', () => {
  const node = ui.renderMarkdown('');
  assert(node.textContent.toLowerCase().includes('no brief'), 'blank brief → honest empty');
});

// ---- data layer cache ----------------------------------------------

test('cachedJson caches failures as null and retries after invalidate', async () => {
  // Stub fetch to fail once, then succeed.
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    if (calls === 1) throw new Error('boom');
    return { ok: true, json: async () => ({ ok: true }) };
  };
  const data = await import('../js/variants/D/data.js');
  const first = await data.cachedJson('/api/__test');
  assertEqual(first, null, 'a failure is cached as null (honest unavailable)');
  const cached = await data.cachedJson('/api/__test');
  assertEqual(cached, null, 'cached without a second fetch');
  assertEqual(calls, 1, 'no re-fetch while cached');
  data.invalidate('/api/__test');
  const retried = await data.cachedJson('/api/__test');
  assert(retried && retried.ok === true, 'invalidate allows a retry');
});

run();
