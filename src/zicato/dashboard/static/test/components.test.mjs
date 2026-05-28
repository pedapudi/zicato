// test/components.test.mjs — design-system component unit tests.
//
// Each component module exports a small render function that returns a
// DOM node. The tests below build a fresh harness, render a node, and
// assert that the structural and textual contract holds. The components
// have no fetch / network surface — they are pure functions over
// inputs — so the tests run fully offline.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { renderMetricTile, renderMetricStrip } = await import('../js/components/tile.js');
const { renderCard, renderCalloutCard } = await import('../js/components/card.js');
const { renderPill, renderInlinePill, renderEventChip } = await import('../js/components/pill.js');
const { renderHeatmapTable } = await import('../js/components/heatmap.js');
const { renderSparkline } = await import('../js/components/sparkline.js');
const { renderSpine } = await import('../js/components/spine.js');
const { renderLiveIndicator } = await import('../js/components/live_indicator.js');

// ---------------------------------------------------------------------
// tile
// ---------------------------------------------------------------------

test('renderMetricTile paints the label and value with the tile classes', () => {
  const node = renderMetricTile({ label: 'drift loss', value: '0.42' });
  assert(node.classList.contains('tile'), 'tile class present');
  assert(node.textContent.includes('drift loss'), 'label rendered');
  assert(node.textContent.includes('0.42'), 'value rendered');
});

test('renderMetricTile renders a positive delta with up arrow + good sentiment', () => {
  const node = renderMetricTile({
    label: 'pass rate', value: '85%', delta: '+5%', sentiment: 'good',
  });
  assert(node.textContent.includes('+5%'), 'delta text rendered');
  assert(node.textContent.includes('↑') || node.textContent.includes('+5%'),
    'arrow or signed delta rendered');
  // Find the delta child.
  const deltas = node.querySelectorAll('[class]');
  let foundGood = false;
  for (const c of deltas) {
    if ((c.className || '').includes('tile-delta-good')) foundGood = true;
  }
  assert(foundGood, 'good sentiment class applied');
});

test('renderMetricTile handles a numeric delta, picking sign + arrow itself', () => {
  const node = renderMetricTile({ label: 'x', value: 12, delta: -3, sentiment: 'good' });
  assert(node.textContent.includes('−'), 'minus prefix on negative delta');
  assert(node.textContent.includes('↓'), 'down arrow on negative delta');
});

test('renderMetricStrip wraps tiles in a tile-strip container', () => {
  const node = renderMetricStrip([
    { label: 'a', value: 1 }, { label: 'b', value: 2 },
  ]);
  assert(node.classList.contains('tile-strip'), 'tile-strip class');
  assertEqual(node.children.length, 2, 'two child tiles');
});

// ---------------------------------------------------------------------
// card
// ---------------------------------------------------------------------

test('renderCard wraps title + body in a card with the card class', () => {
  const node = renderCard({
    title: 'Goal', body: document.createTextNode('hello'),
  });
  assert(node.classList.contains('card'), 'card class');
  assert(node.textContent.includes('Goal'), 'title rendered');
  assert(node.textContent.includes('hello'), 'body rendered');
});

test('renderCard applies an accent class for the accent variant', () => {
  const node = renderCard({ title: 't', body: 'b', accent: 'accent' });
  assert(node.className.includes('card-accent-indigo'), 'accent class applied');
});

test('renderCalloutCard adds the callout class with left-rail accent', () => {
  const node = renderCalloutCard({ title: 'l', body: 'b', accent: 'warning' });
  assert(node.className.includes('card-callout'), 'callout class');
  assert(node.className.includes('card-callout-warning'), 'warning variant');
});

// ---------------------------------------------------------------------
// pill
// ---------------------------------------------------------------------

test('renderPill maps "promoted" to the success variant class', () => {
  const node = renderPill('promoted', 'promoted');
  assert(node.classList.contains('pill'), 'pill base class');
  assert(node.classList.contains('pill-success'), 'success variant for promoted');
});

test('renderPill maps "rejected" to the error variant class', () => {
  const node = renderPill('rejected', 'rejected');
  assert(node.classList.contains('pill-error'), 'error variant for rejected');
});

test('renderPill unknown variant falls back to neutral', () => {
  const node = renderPill('whatever', 'unknown_thing');
  assert(node.classList.contains('pill-neutral'),
    'neutral fallback for unknown variant');
});

test('renderPill live variant carries the dot child', () => {
  const node = renderPill('live', 'live');
  assert(node.classList.contains('pill-live'), 'live variant class');
  const dots = node.querySelectorAll('[class]');
  let hasDot = false;
  for (const d of dots) {
    if ((d.className || '').includes('pill-dot-live')) hasDot = true;
  }
  assert(hasDot, 'live dot rendered');
});

test('renderInlinePill is smaller — carries pill-sm class', () => {
  const node = renderInlinePill('pass', 'pass');
  assert(node.classList.contains('pill-sm'), 'small pill class');
  assert(node.classList.contains('pill-success'), 'pass maps to success');
});

test('renderEventChip maps drift_detected to the error class', () => {
  const node = renderEventChip('drift_detected');
  assert(node.classList.contains('event-chip'), 'event-chip class');
  assert(node.classList.contains('pill-error'), 'drift_detected maps to error');
});

// ---------------------------------------------------------------------
// heatmap
// ---------------------------------------------------------------------

test('renderHeatmapTable renders header row + data rows with colored cells', () => {
  const node = renderHeatmapTable({
    rows: ['judge_a', 'judge_b'],
    cols: ['v0', 'v1', 'v2'],
    valueAt: (r, c) => {
      const tbl = { 'judge_a': { v0: 0.1, v1: 0.2, v2: 0.3 },
                    'judge_b': { v0: 0.4, v1: 0.5 } };
      return tbl[r] && tbl[r][c];
    },
    scale: 'sequential',
    rowLabel: 'judge',
  });
  assert(node.textContent.includes('judge_a'), 'judge_a row');
  assert(node.textContent.includes('judge_b'), 'judge_b row');
  assert(node.textContent.includes('v0') && node.textContent.includes('v2'),
    'v0/v2 columns');
  // Missing j_b/v2 should render as "—".
  const tdsAll = node.querySelectorAll('[class]');
  let foundEmpty = false;
  for (const c of tdsAll) {
    if ((c.className || '').includes('heatmap-cell-empty')) foundEmpty = true;
  }
  assert(foundEmpty, 'missing cell renders the empty marker');
});

test('renderHeatmapTable empties to a single dash when missing data only', () => {
  const node = renderHeatmapTable({
    rows: ['x'], cols: ['v0'], valueAt: () => null, scale: 'sequential',
  });
  assert(node.textContent.includes('—'), 'missing cell text');
});

// ---------------------------------------------------------------------
// sparkline
// ---------------------------------------------------------------------

test('renderSparkline emits an SVG with a single path for finite values', () => {
  const node = renderSparkline([1, 2, 3, 4], { width: 80, height: 22 });
  assert(node.classList.contains('sparkline'), 'sparkline wrapper class');
  const svg = node.firstChild;
  assert(svg && svg.localName === 'svg', 'SVG child present');
});

test('renderSparkline collapses to an empty marker for an empty series', () => {
  const node = renderSparkline([], { width: 80, height: 22 });
  assert(node.classList.contains('sparkline-empty'), 'empty class');
});

test('renderSparkline survives non-finite mid-series values (gaps)', () => {
  const node = renderSparkline([1, NaN, 3], { width: 80, height: 22 });
  assert(node.classList.contains('sparkline'), 'wrapper class still present');
});

// ---------------------------------------------------------------------
// spine
// ---------------------------------------------------------------------

test('renderSpine paints one node per promoted entry, rejected footnoted', () => {
  const node = renderSpine({
    nodes: [
      { id: 'v0', scalar: 0.62, promoted: true },
      { id: 'v1', scalar: 0.48, promoted: true },
      { id: 'v2', scalar: 0.55, promoted: false, decision: 'rejected' },
      { id: 'v3', scalar: 0.23, promoted: true },
    ],
  });
  assert(node.textContent.includes('v0'), 'v0 painted');
  assert(node.textContent.includes('v3'), 'v3 painted');
  // v2 should still be referenced (as rejected footnote).
  assert(node.textContent.includes('v2'),
    'rejected node referenced in footnote');
});

test('renderSpine marks the live node with a LIVE tag', () => {
  const node = renderSpine({
    nodes: [
      { id: 'v0', scalar: 0.5, promoted: true },
      { id: 'v1', scalar: null, live: true },
    ],
  });
  assert(node.textContent.includes('LIVE'),
    'LIVE tag rendered on live node');
});

test('renderSpine empty state renders cleanly', () => {
  const node = renderSpine({ nodes: [] });
  assert(node.classList.contains('spine-empty'), 'empty spine wrapper');
  assert(node.textContent.includes('No generations'), 'empty message text');
});

test('renderSpine when ALL nodes are rejected, all render inline (no empty spine)', () => {
  const node = renderSpine({
    nodes: [
      { id: 'v1', scalar: 0.6, promoted: false, decision: 'rejected' },
      { id: 'v2', scalar: 0.7, promoted: false, decision: 'rejected' },
    ],
  });
  assert(node.textContent.includes('v1'), 'v1 still inline');
  assert(node.textContent.includes('v2'), 'v2 still inline');
});

// ---------------------------------------------------------------------
// live_indicator
// ---------------------------------------------------------------------

test('renderLiveIndicator paints is-live class + label', () => {
  const node = renderLiveIndicator({ live: true });
  assert(node.classList.contains('is-live'), 'is-live class');
  assert(node.textContent.toLowerCase().includes('live'), 'live label');
});

test('renderLiveIndicator paints is-stale class for stale state', () => {
  const node = renderLiveIndicator({ live: false });
  assert(node.classList.contains('is-stale'), 'is-stale class');
});

await run();
