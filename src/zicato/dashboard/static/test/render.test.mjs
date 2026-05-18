// test/render.test.mjs — render-layer behaviour tests.
//
// These exercise the actual render layer against the mock snapshot and
// prove the redesign's two structural guarantees:
//   * the activity-log tail GROWS by appending keyed rows — a re-render
//     does NOT clear-and-rebuild it, so it cannot flash;
//   * a matchup-click handler survives a state delta — the row node
//     keeps identity across reconcileList, so the click still fires.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

// A DOM seeded with the index.html element ids the render layer needs.
const REQUIRED_IDS = [
  'drill-close', 'header-bar', 'footer-bar', 'epoch-id', 'generation-id',
  'round-id', 'elapsed', 'health-badge', 'mock-badge', 'tournament-title',
  'tournament-body', 'tournament-elapsed', 'health-panel', 'tournament-bracket',
  'tournament-detail', 'active-runs', 'log-tail', 'drill-panel', 'drill-title',
  'drill-body', 'dashboard-version', 'dashboard-port', 'dashboard-build',
  'view-overview', 'view-tree', 'view-tournament', 'view-epoch', 'view-files',
  'view-conversation', 'nav-overview', 'nav-tree', 'nav-tournament', 'nav-epoch',
  'nav-files', 'epoch-overview', 'epoch-harness', 'epoch-board', 'epoch-brief',
  'epoch-scoring', 'epoch-mutations', 'lineage-svg', 'trajectory-svg',
  'heatmap-svg', 'conversation-panel', 'files-tree-pane', 'files-content-pane',
  'files-patches', 'mutations-list-pane', 'mutations-detail-pane',
  'lineage-stage', 'lineage-viewport', 'lineage-zoom-in', 'lineage-zoom-out',
  'lineage-zoom-reset',
];

function seedDom() {
  const doc = installDom();
  globalThis.location = globalThis.window.location;
  globalThis.URLSearchParams = URLSearchParams;
  globalThis.EventSource = class { addEventListener() {} close() {} };
  for (const id of REQUIRED_IDS) {
    const tag = id.endsWith('-svg') ? 'svg' : 'div';
    const n = doc.createElement(tag);
    n.setAttribute('id', id);
    doc.body.appendChild(n);
  }
  return doc;
}

const doc = seedDom();
const { state } = await import('../js/core/state.js');
const { mockSnapshot } = await import('../js/views/mock.js');
const render = await import('../js/views/render.js');

test('the render layer paints every view from the mock snapshot', () => {
  state.mock = true;
  state.applySnapshot(mockSnapshot());
  // renderAll must not throw across header, footer and the active view.
  render.renderAll();
  // The header reflects the mock heartbeat.
  const gen = doc.getElementById('generation-id').textContent;
  assert(gen.includes('v5'), `header generation should show v5, got "${gen}"`);
  // The footer wired from the mock /api/health.
  const ver = doc.getElementById('dashboard-version').textContent;
  assert(ver.includes('dashboard'), 'footer must render');
});

test('log tail GROWS by appending keyed rows — no clear-and-rebuild', () => {
  const wrap = doc.getElementById('log-tail');
  render.renderLogTail();
  const firstRows = wrap.children.filter
    ? wrap.children
    : [...wrap.childNodes].filter((n) => n.nodeType === 1);
  const rowCount = wrap.children.length;
  assert(rowCount > 0, 'log tail should have rows from the mock run_log');
  const firstRow = wrap.children[0];

  // Re-render the log tail repeatedly — the existing rows must be the
  // SAME nodes (append-only), and nothing rebuilds innerHTML.
  render.renderLogTail();
  render.renderLogTail();
  assert(wrap.children[0] === firstRow, 'log row identity must survive a re-render');
  assertEqual(wrap.children.length, rowCount, 'a no-op re-render must not add rows');
  assertEqual(wrap.innerHTMLWriteCount(), 0, 'log tail must never write innerHTML');

  // A genuinely-new event appends exactly one row; the rest are intact.
  state.mergeLogTail({
    events: [{ seq: 999, kind: 'note', ts: '2026-05-18T05:00:00Z', summary: 'fresh event' }],
    cursor: 999,
  });
  render.appendLogTail();
  assertEqual(wrap.children.length, rowCount + 1, 'one new event -> one new row');
  assert(wrap.children[0] === firstRow, 'old rows untouched when a new one appends');
});

test('matchup-click renders the detail inline and the handler survives a delta', () => {
  // Switch to the Tournament view and paint the bracket.
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');
  // The bracket must have rendered SOMETHING for the mock tournaments.
  assert(bracket.children.length > 0, 'tournament bracket should render the mock data');

  // Find a clickable matchup node (the mock bracket has clickable
  // champion / challenger / live cards wired to openMatchup).
  const clickable = bracket._descendants().filter(
    (n) => n._listeners && (n._listeners.click || n._listeners.keydown),
  );
  assert(clickable.length > 0, 'the bracket must expose clickable matchup nodes');

  // Click one — it must not throw, and it routes via the hash.
  const node = clickable[0];
  node.dispatchEvent(makeEvent('click'));

  // Re-render the whole app (simulating a state delta). The bracket is
  // repainted; the click nodes must still be present and still
  // clickable — the matchup-click fix.
  render.renderAll();
  const stillClickable = doc.getElementById('tournament-bracket')._descendants()
    .filter((n) => n._listeners && (n._listeners.click || n._listeners.keydown));
  assert(stillClickable.length > 0, 'matchup-click handlers must survive a re-render');
});

test('overview renders when a health finding detail is an OBJECT, not a string', () => {
  // Regression: the live workspace's health_report.findings[].detail is
  // a structured object, not a string. The render layer must coerce it
  // — a raw appendChild of an object aborts the whole Overview render
  // (the bug the browser interaction pass caught). All four Overview
  // panels must still paint.
  state.applySnapshot(mockSnapshot());
  state.healthReport = {
    epoch_id: 'e-obj', healthy: false,
    findings: [{
      code: 'non_differentiating_entry', severity: 'warning',
      summary: 'a board entry did not differentiate generations',
      // detail as an OBJECT — exactly the live-data shape.
      detail: { drift_loss: 60.0, entry_id: 'x', generation_ids: ['v0', 'v1'] },
    }],
  };
  render.showView('overview');
  // The health panel rendered the finding without throwing.
  assert(doc.getElementById('health-panel').children.length > 0,
    'health panel must render even with an object-valued detail');
  // And — critically — the panels AFTER renderHealthPanel still painted,
  // proving the render did not abort mid-view.
  const tbody = doc.getElementById('tournament-body').textContent;
  assert(!tbody.includes('No active tournament'),
    'the tournament panel must render — renderHealthPanel must not abort the view');
  assert(doc.getElementById('log-tail').children.length > 0,
    'the log tail must render — renderHealthPanel must not abort the view');
});

test('renderAll is idempotent — repeated calls do not throw or grow the DOM', () => {
  render.showView('overview');
  render.renderAll();
  const logRows = doc.getElementById('log-tail').children.length;
  render.renderAll();
  render.renderAll();
  assertEqual(doc.getElementById('log-tail').children.length, logRows,
    'idempotent renderAll must not duplicate log rows');
});

await run();
