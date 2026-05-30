// test/decision_components.test.mjs — unit tests for the decision-view
// components. Each factory is a pure function over inputs returning a
// detached, re-render-safe DOM node; the tests exercise structure,
// re-render safety, and the sign/color logic that makes a verdict legible.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { verdictGlyph } = await import('../js/components/verdict_glyph.js');
const { gateLadder } = await import('../js/components/gate_ladder.js');
const { divergingBar } = await import('../js/components/diverging_bar.js');
const { scalarWaterfall } = await import('../js/components/scalar_waterfall.js');
const { scalarBand } = await import('../js/components/scalar_band.js');

// Collect every node carrying a class, for class assertions.
function withClass(root, cls) {
  const out = [];
  const all = root.querySelectorAll('[class]');
  for (const n of all) {
    if ((n.className || '').split(/\s+/).includes(cls)) out.push(n);
  }
  return out;
}

// ---------------------------------------------------------------------
// verdict glyph
// ---------------------------------------------------------------------

test('verdictGlyph returns a fresh node each call (re-render safe)', () => {
  const a = verdictGlyph('promoted');
  const b = verdictGlyph('promoted');
  assert(a !== b, 'distinct nodes on repeat calls');
  assert(a.classList.contains('vglyph'), 'base class');
});

test('verdictGlyph maps each decision to glyph + color kind', () => {
  const cases = [
    ['promoted', '✓', 'vglyph-promoted'],
    ['rejected', '✗', 'vglyph-rejected'],
    ['deferred', '◦', 'vglyph-neutral'],
    ['open', '◦', 'vglyph-neutral'],
    ['pending', '·', 'vglyph-muted'],
  ];
  for (const [decision, glyph, cls] of cases) {
    const node = verdictGlyph(decision);
    assert(node.classList.contains(cls), `${decision} → ${cls}`);
    assert(node.textContent.includes(glyph), `${decision} → glyph ${glyph}`);
    assert(node.textContent.includes(decision), `${decision} label rendered`);
  }
});

test('verdictGlyph unknown decision falls back to pending dot', () => {
  const node = verdictGlyph('???');
  assert(node.classList.contains('vglyph-muted'), 'muted fallback');
  assert(node.textContent.includes('·'), 'pending dot');
});

test('verdictGlyph withLabel:false hides the word but keeps the mark', () => {
  const node = verdictGlyph('promoted', { withLabel: false });
  assert(node.textContent.includes('✓'), 'mark present');
  assertEqual(withClass(node, 'vglyph-label').length, 0, 'no label span');
  assertEqual(node.getAttribute('aria-label'), 'promoted', 'a11y label retained');
});

// ---------------------------------------------------------------------
// gate ladder
// ---------------------------------------------------------------------

test('gateLadder returns a fresh node and renders one row per rule', () => {
  const rules = [
    { id: 'r1', label: 'regression suite', status: 'pass' },
    { id: 'r2', label: 'scalar margin', status: 'pass' },
  ];
  const a = gateLadder({ rules });
  const b = gateLadder({ rules });
  assert(a !== b, 'distinct nodes');
  assert(a.classList.contains('gate-ladder'), 'base class');
  assertEqual(a.children.length, 2, 'two rows');
});

test('gateLadder emphasizes the fired rule and greys not_reached', () => {
  const node = gateLadder({
    rules: [
      { id: 'reg', label: 'regression suite', status: 'pass', detail: '12/12' },
      { id: 'margin', label: 'scalar margin', status: 'fail', detail: '+0.031', fired: true },
      { id: 'pass', label: 'pass-rate monotonicity', status: 'not_reached' },
      { id: 'ns', label: 'namespace monotonicity', status: 'not_reached' },
    ],
  });
  const fired = withClass(node, 'gate-fired');
  assertEqual(fired.length, 1, 'exactly one fired row');
  assert(fired[0].textContent.includes('scalar margin'), 'margin rule is the fired one');
  assertEqual(fired[0].getAttribute('aria-current'), 'true', 'aria-current set on fired row');
  // not_reached rows are greyed.
  const muted = withClass(node, 'gate-muted');
  assertEqual(muted.length, 2, 'two greyed not_reached rows');
});

test('gateLadder pass/fail rows get the correct status class + glyph', () => {
  const node = gateLadder({
    rules: [
      { id: 'a', label: 'A', status: 'pass' },
      { id: 'b', label: 'B', status: 'fail', fired: true },
      { id: 'c', label: 'C', status: 'skipped' },
    ],
  });
  assertEqual(withClass(node, 'gate-pass').length, 1, 'one pass row');
  assertEqual(withClass(node, 'gate-fail').length, 1, 'one fail row');
  assert(node.textContent.includes('✓'), 'pass glyph');
  assert(node.textContent.includes('✗'), 'fail glyph');
});

test('gateLadder empty rules renders a clean empty row', () => {
  const node = gateLadder({ rules: [] });
  assert(node.textContent.toLowerCase().includes('no gate rules'), 'empty message');
});

// ---------------------------------------------------------------------
// diverging bar
// ---------------------------------------------------------------------

test('divergingBar negative delta is an improvement (good/right)', () => {
  const node = divergingBar({ rows: [{ label: 'drift', delta: -0.2 }] });
  const good = withClass(node, 'dbar-good');
  assert(good.length >= 1, 'good class present for negative delta');
  // The fill sits in the right half.
  assertEqual(withClass(node, 'dbar-right').length, 1, 'improvement bar on the right');
  assertEqual(withClass(node, 'dbar-left').length, 0, 'no left bar');
});

test('divergingBar positive delta is a regression (bad/left)', () => {
  const node = divergingBar({ rows: [{ label: 'cost', delta: 0.4 }] });
  assert(withClass(node, 'dbar-bad').length >= 1, 'bad class for positive delta');
  assertEqual(withClass(node, 'dbar-left').length, 1, 'regression bar on the left');
  assertEqual(withClass(node, 'dbar-right').length, 0, 'no right bar');
});

test('divergingBar flips with goodWhenNegative:false', () => {
  const node = divergingBar({
    rows: [{ label: 'pass rate', delta: 0.1 }],
    goodWhenNegative: false,
  });
  assert(withClass(node, 'dbar-good').length >= 1, 'positive is good when flipped');
  assertEqual(withClass(node, 'dbar-right').length, 1, 'good bar on the right');
});

test('divergingBar sorts rows by |delta| descending', () => {
  const node = divergingBar({
    rows: [
      { label: 'small', delta: -0.01 },
      { label: 'big', delta: 0.5 },
      { label: 'mid', delta: -0.2 },
    ],
  });
  const labels = withClass(node, 'dbar-label').map((n) => n.textContent.trim());
  assertEqual(labels[0], 'big', 'largest |delta| first');
  assertEqual(labels[1], 'mid', 'mid second');
  assertEqual(labels[2], 'small', 'smallest last');
});

test('divergingBar renders an annotation glyph with its title', () => {
  const node = divergingBar({
    rows: [{ label: 'entry-7', delta: 0.3, annotation: { glyph: '⚠', title: 'pass→fail' } }],
  });
  const ann = withClass(node, 'dbar-annotation');
  assertEqual(ann.length, 1, 'annotation rendered');
  assert(ann[0].textContent.includes('⚠'), 'annotation glyph');
  assertEqual(ann[0].getAttribute('title'), 'pass→fail', 'annotation title');
});

test('divergingBar returns a fresh node and handles empty input', () => {
  const a = divergingBar({ rows: [{ label: 'x', delta: -0.1 }] });
  const b = divergingBar({ rows: [{ label: 'x', delta: -0.1 }] });
  assert(a !== b, 'distinct nodes');
  const empty = divergingBar({ rows: [] });
  assert(empty.textContent.toLowerCase().includes('no deltas'), 'empty message');
});

// ---------------------------------------------------------------------
// scalar waterfall
// ---------------------------------------------------------------------

test('scalarWaterfall colors components by sign and totals them', () => {
  const node = scalarWaterfall({
    components: [
      { name: 'drift', delta: -0.3 },
      { name: 'pass', delta: 0.1 },
      { name: 'ns:cost', delta: -0.05 },
    ],
    label: 'champion → challenger',
  });
  assert(node.classList.contains('swfall'), 'base class');
  assert(withClass(node, 'swfall-good').length >= 2, 'two improvements colored good');
  assert(withClass(node, 'swfall-bad').length >= 1, 'one regression colored bad');
  // total = -0.3 + 0.1 - 0.05 = -0.25 → improvement.
  const total = withClass(node, 'swfall-total');
  assertEqual(total.length, 1, 'one total row');
  assert(total[0].textContent.includes('-0.250'), 'total delta rendered');
});

test('scalarWaterfall returns a fresh node and handles empty input', () => {
  const a = scalarWaterfall({ components: [{ name: 'x', delta: -0.1 }] });
  const b = scalarWaterfall({ components: [{ name: 'x', delta: -0.1 }] });
  assert(a !== b, 'distinct nodes');
  const empty = scalarWaterfall({ components: [] });
  assert(empty.textContent.toLowerCase().includes('no scalar components'), 'empty message');
});

// ---------------------------------------------------------------------
// scalar band
// ---------------------------------------------------------------------

test('scalarBand marks a challenger below the threshold a win', () => {
  // threshold = 0.6 - 0.05 = 0.55; challenger 0.4 <= 0.55 → win.
  const node = scalarBand({ champion: 0.6, challenger: 0.4, margin: 0.05 });
  assert(node.classList.contains('sband'), 'base class');
  assert(withClass(node, 'sband-win').length >= 1, 'win class applied');
  assert(node.textContent.includes('win'), 'win verdict in legend');
});

test('scalarBand marks a near-miss (improved but not enough)', () => {
  // threshold = 0.55; challenger 0.57 is < champion but > threshold.
  const node = scalarBand({ champion: 0.6, challenger: 0.57, margin: 0.05 });
  assert(withClass(node, 'sband-near').length >= 1, 'near class applied');
  assert(node.textContent.includes('near-miss'), 'near-miss verdict');
});

test('scalarBand marks a regression (worse than champion)', () => {
  const node = scalarBand({ champion: 0.6, challenger: 0.7, margin: 0.05 });
  assert(withClass(node, 'sband-regress').length >= 1, 'regress class applied');
  assert(node.textContent.includes('regressed'), 'regressed verdict');
});

test('scalarBand draws an error bar only when an interval is given', () => {
  const plain = scalarBand({ champion: 0.6, challenger: 0.4, margin: 0.05 });
  assertEqual(withClass(plain, 'sband-interval').length, 0, 'no interval bar without CI');
  const withCI = scalarBand({
    champion: 0.6, challenger: 0.4, margin: 0.05,
    challengerInterval: { lo: 0.35, hi: 0.45 },
  });
  assert(withClass(withCI, 'sband-interval').length >= 1, 'interval bar drawn for CI');
  assert(withCI.textContent.includes('[0.350, 0.450]'), 'interval shown in legend');
});

test('scalarBand returns a fresh node and handles non-finite input', () => {
  const a = scalarBand({ champion: 0.5, challenger: 0.4, margin: 0.05 });
  const b = scalarBand({ champion: 0.5, challenger: 0.4, margin: 0.05 });
  assert(a !== b, 'distinct nodes');
  const bad = scalarBand({ champion: NaN, challenger: 0.4 });
  assert(bad.textContent.toLowerCase().includes('no scalar'), 'empty message on bad input');
});

await run();
