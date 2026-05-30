// test/v2_primitives.test.mjs — unit tests for the v2 dense data-viz
// primitives: dataTable, liveMatrix, smallMultiples.
//
// These are pure factories over inputs (no fetch / network), so the
// tests build a fresh harness DOM, render, and assert the structural,
// textual, behavioural (click), and reconcile-in-place contracts hold.
// Run directly: `node static/test/v2_primitives.test.mjs`.

import { installDom, makeEvent, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { dataTable, deltaCell } = await import('../js/v2/components/dataTable.js');
const { liveMatrix, CELL_STATES } = await import('../js/v2/components/liveMatrix.js');
const { smallMultiples } = await import('../js/v2/components/smallMultiples.js');

// Walk a subtree collecting <td>/<th> rows; helper to grab body rows.
function bodyRows(table) {
  const tbody = table.children.find((c) => c.localName === 'tbody');
  return tbody ? tbody.children : [];
}
function tableText(node) { return node.textContent; }

// =====================================================================
// dataTable
// =====================================================================

test('dataTable renders a header + one row per datum', () => {
  const tbl = dataTable({
    columns: [
      { key: 'gen', header: 'gen' },
      { key: 'd', header: 'Δscalar', semantic: 'delta' },
    ],
    rows: [
      { gen: 'v1', d: -0.31 },
      { gen: 'v2', d: 0.02 },
      { gen: 'v3', d: -0.18 },
    ],
  });
  assert(tbl.classList.contains('v2-dt'), 'v2-dt class present');
  const rows = bodyRows(tbl);
  assertEqual(rows.length, 3, 'three body rows');
  assert(tableText(tbl).includes('v1'), 'gen v1 rendered');
  assert(tableText(tbl).includes('Δscalar'), 'header rendered');
});

test('dataTable delta cells carry redundant glyph + sentiment', () => {
  const tbl = dataTable({
    columns: [{ key: 'd', header: 'Δ', semantic: 'delta' }],
    rows: [{ d: -0.5 }, { d: 0.5 }, { d: 0 }],
  });
  const txt = tableText(tbl);
  assert(txt.includes('▼'), 'improvement glyph (negative is good)');
  assert(txt.includes('▲'), 'regress glyph');
  assert(txt.includes('−0.500') || txt.includes('-0.500'), 'signed magnitude');
  // sentiment data-attr is set on the delta span.
  const improve = tbl.querySelectorAll('[data-sentiment]');
  let sentiments = improve.map((n) => n.getAttribute('data-sentiment'));
  assert(sentiments.includes('improve'), 'improve sentiment present');
  assert(sentiments.includes('regress'), 'regress sentiment present');
  assert(sentiments.includes('flat'), 'flat sentiment present');
});

test('dataTable fires onRowClick with the row datum + index', () => {
  let got = null;
  const tbl = dataTable({
    columns: [{ key: 'gen', header: 'gen' }],
    rows: [{ gen: 'v1' }, { gen: 'v2' }],
    onRowClick: (row, i) => { got = { row, i }; },
  });
  assert(tbl.classList.contains('v2-dt-drillable'), 'drillable class on table');
  const rows = bodyRows(tbl);
  assert(rows[1].classList.contains('v2-dt-row-drillable'), 'row marked drillable');
  assertEqual(rows[1].getAttribute('role'), 'button', 'row is a button for a11y');
  // A drill cue column was appended.
  assert(tableText(tbl).includes('›'), 'drill cue glyph present');
  rows[1].dispatchEvent(makeEvent('click'));
  assert(got != null, 'onRowClick fired');
  assertEqual(got.row.gen, 'v2', 'correct row datum');
  assertEqual(got.i, 1, 'correct index');
});

test('dataTable onRowClick fires on keyboard Enter', () => {
  let fired = 0;
  const tbl = dataTable({
    columns: [{ key: 'x', header: 'x' }],
    rows: [{ x: 1 }],
    onRowClick: () => { fired += 1; },
  });
  const rows = bodyRows(tbl);
  rows[0].dispatchEvent(makeEvent('keydown', { key: 'Enter' }));
  assertEqual(fired, 1, 'Enter triggers the row');
});

test('dataTable sorts by a column and toggles direction on header click', () => {
  const tbl = dataTable({
    columns: [
      { key: 'gen', header: 'gen' },
      { key: 'd', header: 'Δ', value: (r) => r.d },
    ],
    rows: [
      { gen: 'a', d: 0.3 },
      { gen: 'b', d: -0.5 },
      { gen: 'c', d: 0.1 },
    ],
    sort: { key: 'd', dir: 'asc' },
  });
  // asc: -0.5, 0.1, 0.3  -> rows b, c, a
  let rows = bodyRows(tbl);
  assertEqual(rows[0].getAttribute('data-key'), '1', 'asc: b (index 1) first');
  assertEqual(rows[2].getAttribute('data-key'), '0', 'asc: a (index 0) last');

  // Click the Δ header → toggles to desc.
  const headRow = tbl.children.find((c) => c.localName === 'thead').children[0];
  const dHeader = headRow.children[1];
  dHeader.dispatchEvent(makeEvent('click'));
  rows = bodyRows(tbl);
  assertEqual(rows[0].getAttribute('data-key'), '0', 'desc: a (largest 0.3) first');
  assertEqual(dHeader.getAttribute('aria-sort'), 'descending', 'aria-sort flips to descending');
});

test('dataTable renders an empty state with no rows', () => {
  const tbl = dataTable({
    columns: [{ key: 'x', header: 'x' }],
    rows: [],
    emptyText: 'no experiments yet',
  });
  assert(tableText(tbl).includes('no experiments yet'), 'empty text shown');
});

test('deltaCell direction respects improveWhenNegative=false', () => {
  const up = deltaCell(0.2, { improveWhenNegative: false });
  assertEqual(up.getAttribute('data-sentiment'), 'improve', 'positive is improve when flag false');
});

// =====================================================================
// liveMatrix
// =====================================================================

function fourStateMatrix() {
  const entries = [
    { entry_id: 'short_solar', weight: 1.0 },
    { entry_id: 'long_solar', weight: 1.5 },
    { entry_id: 'contradictory', weight: 1.0 },
    { entry_id: 'revision', weight: 1.0 },
  ];
  // champion column exercises done + aborted; challenger exercises
  // running + queued — all four honest states appear.
  const cells = {
    'short_solar champion': { state: 'done', loss: 0.42, pass: true },
    'short_solar challenger': { state: 'done', loss: 0.31, pass: true },
    'long_solar champion': { state: 'done', loss: 0.55, pass: false },
    'long_solar challenger': { state: 'running', progress: 0.73 },
    'contradictory champion': { state: 'running', progress: 0.12 },
    'contradictory challenger': { state: 'queued' },
    'revision champion': { state: 'aborted', note: 'killed' },
    'revision challenger': { state: 'queued' },
  };
  const cellFor = (e, side) => cells[`${e.entry_id} ${side}`] || { state: 'queued' };
  return { entries, cells, cellFor };
}

test('liveMatrix renders all four honest cell states', () => {
  assertEqual(CELL_STATES.join(','), 'queued,running,done,aborted', 'state vocabulary');
  const { entries, cellFor } = fourStateMatrix();
  const { node } = liveMatrix({ entries, cellFor });
  assert(node.classList.contains('v2-lm'), 'v2-lm class');
  // Each state class appears somewhere.
  for (const st of CELL_STATES) {
    const hit = node.querySelectorAll(`[data-state]`).some((n) => n.getAttribute('data-state') === st);
    assert(hit, `state ${st} rendered`);
  }
  const txt = node.textContent;
  assert(txt.includes('queued'), 'queued word');
  assert(txt.includes('running'), 'running word');
  assert(txt.includes('done'), 'done word');
  assert(txt.includes('aborted'), 'aborted word');
  assert(txt.includes('73%'), 'running shows budget percent');
  assert(txt.includes('0.420'), 'done shows loss summary');
  assert(txt.includes('✓ pass'), 'done shows pass verdict glyph');
  assert(txt.includes('✗ fail'), 'done shows fail verdict glyph');
  assert(txt.includes('killed'), 'aborted note shown');
});

test('liveMatrix running cell draws a budget progress bar with width', () => {
  const { entries, cellFor } = fourStateMatrix();
  const { node } = liveMatrix({ entries, cellFor });
  // Find a running cell and check its bar fill width.
  const running = node.querySelectorAll('[data-state]').find((n) => n.getAttribute('data-state') === 'running');
  assert(running, 'a running cell exists');
  const bar = running.querySelectorAll('[class]').find((n) => n.classList.contains('v2-lm-bar'));
  assert(bar.classList.contains('v2-lm-bar-on'), 'bar shown for running state');
  const fill = bar.children[0];
  assert(fill.style.cssText.includes('width:73%'), 'fill width is the budget fraction');
  assert(bar.getAttribute('aria-valuenow') === '73', 'progressbar aria-valuenow set');
});

test('liveMatrix updates a cell in place without rebuilding the table', () => {
  const { entries } = fourStateMatrix();
  // Mutable cell store so update() picks up a state transition.
  const store = {
    'long_solar challenger': { state: 'running', progress: 0.73 },
  };
  const cellFor = (e, side) => store[`${e.entry_id} ${side}`] || { state: 'queued' };
  const { node, update } = liveMatrix({ entries, cellFor });

  const tbody = node.children.find((c) => c.localName === 'tbody');
  const rowsBefore = tbody.children;
  const longRow = rowsBefore.find((r) => r.getAttribute('data-key') === 'long_solar');
  const challengerCellBefore = longRow.children[2]; // entry th, champion, challenger
  assertEqual(challengerCellBefore.getAttribute('data-state'), 'running', 'starts running');

  // Transition that cell to done; update in place.
  store['long_solar challenger'] = { state: 'done', loss: 0.21, pass: true };
  update();

  const tbodyAfter = node.children.find((c) => c.localName === 'tbody');
  assert(tbodyAfter === tbody, 'tbody node identity preserved');
  const longRowAfter = tbodyAfter.children.find((r) => r.getAttribute('data-key') === 'long_solar');
  assert(longRowAfter === longRow, 'row node identity preserved across update');
  const challengerCellAfter = longRowAfter.children[2];
  assert(challengerCellAfter === challengerCellBefore, 'cell node identity preserved');
  assertEqual(challengerCellAfter.getAttribute('data-state'), 'done', 'cell transitioned to done in place');
  assert(challengerCellAfter.textContent.includes('0.210'), 'cell shows new loss');
  assertEqual(node.innerHTMLWriteCount(), 0, 'no innerHTML writes — no flash');
});

test('liveMatrix reconciles added + removed entries', () => {
  const store = {};
  let entries = [{ entry_id: 'a' }, { entry_id: 'b' }];
  const cellFor = () => ({ state: 'queued' });
  const { node, update } = liveMatrix({ entries, cellFor });
  let tbody = node.children.find((c) => c.localName === 'tbody');
  assertEqual(tbody.children.length, 2, 'two rows initially');

  update([{ entry_id: 'a' }, { entry_id: 'c' }]); // b removed, c added
  assertEqual(tbody.children.length, 2, 'still two rows');
  const keys = tbody.children.map((r) => r.getAttribute('data-key'));
  assert(keys.includes('a') && keys.includes('c') && !keys.includes('b'), 'b removed, c added');
  void store;
});

test('liveMatrix honours custom side labels', () => {
  const { entries, cellFor } = fourStateMatrix();
  const { node } = liveMatrix({
    entries, cellFor,
    sides: [{ key: 'champion', label: 'parent v4' }, { key: 'challenger', label: 'candidate v5' }],
  });
  assert(node.textContent.includes('parent v4'), 'custom champion label');
  assert(node.textContent.includes('candidate v5'), 'custom challenger label');
});

// =====================================================================
// smallMultiples
// =====================================================================

test('smallMultiples lays out N panels, one per item', () => {
  const items = [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }];
  const grid = smallMultiples({
    items,
    title: (it) => it.id,
    render: (it) => document.createTextNode(`chart:${it.id}`),
  });
  assert(grid.classList.contains('v2-sm'), 'v2-sm class');
  const panels = grid.children.filter((c) => c.classList.contains('v2-sm-panel'));
  assertEqual(panels.length, 4, 'four panels');
  assert(grid.textContent.includes('chart:a'), 'render output for a');
  assert(grid.textContent.includes('chart:d'), 'render output for d');
  // titles rendered
  assert(grid.textContent.includes('a') && grid.textContent.includes('d'), 'titles present');
});

test('smallMultiples uses a fixed column count when given', () => {
  const grid = smallMultiples({
    items: [{}, {}, {}],
    columns: 3,
    render: () => document.createTextNode('x'),
  });
  assert(grid.classList.contains('v2-sm-fixed'), 'fixed-layout class');
  assert(grid.style.cssText.includes('repeat(3'), 'three fixed columns');
});

test('smallMultiples auto-fills responsively without an explicit column count', () => {
  const grid = smallMultiples({ items: [{}], render: () => 'x', minWidth: '200px' });
  assert(grid.classList.contains('v2-sm-auto'), 'auto-layout class');
  assert(grid.style.cssText.includes('auto-fill'), 'responsive auto-fill track');
  assert(grid.style.cssText.includes('200px'), 'honours minWidth');
});

test('smallMultiples panels are drillable when onItem is supplied', () => {
  let clicked = null;
  const grid = smallMultiples({
    items: [{ id: 'a' }, { id: 'b' }],
    render: (it) => it.id,
    onItem: (it, i) => { clicked = { it, i }; },
  });
  const panel = grid.children.find((c) => c.getAttribute('data-key') === '1');
  assert(panel.classList.contains('v2-sm-panel-drillable'), 'panel marked drillable');
  panel.dispatchEvent(makeEvent('click'));
  assertEqual(clicked.it.id, 'b', 'onItem fired with correct item');
  assertEqual(clicked.i, 1, 'onItem fired with correct index');
});

test('smallMultiples renders an empty state with no items', () => {
  const grid = smallMultiples({ items: [], render: () => 'x', emptyText: 'nothing to compare' });
  assert(grid.textContent.includes('nothing to compare'), 'empty text shown');
});

await run();
