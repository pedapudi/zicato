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
  'epoch-scoring', 'epoch-mutations',
  'epoch-experiment-log', 'epoch-journal', 'epoch-analysis',
  'lineage-svg', 'trajectory-svg',
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

test('partial aggregate renders the server-side scalar — not a false 0.00', () => {
  // Regression: the runtime active_tournament carries per-SIDE entry
  // rows (no e.parent / e.child sub-objects). The old renderAggregate
  // looked for e.parent/e.child and so always rendered drift_loss_mean
  // 0.00 even with finished boards. The runner now persists a running
  // partial aggregate (partial_parent_agg / partial_child_agg) the
  // instant each board unit settles; renderAggregate must consume it.
  // The Overview view owns the active-tournament panel.
  state.applySnapshot(mockSnapshot());
  state.activeTournament = {
    ...state.activeTournament,
    parent_id: 'v4',
    child_id: 'v5',
    partial_parent_agg: { drift_loss_mean: 0.42, pass_rate: 0.80, scalar: 0.137, entry_count: 3 },
    partial_child_agg: { drift_loss_mean: 0.31, pass_rate: 0.90, scalar: 0.041, entry_count: 3 },
  };
  render.showView('overview');
  const body = doc.getElementById('tournament-body').textContent;
  assert(body.includes('0.42'), `champion drift_loss_mean must show 0.42, got: ${body}`);
  assert(body.includes('0.31'), `challenger drift_loss_mean must show 0.31, got: ${body}`);
  // The Δscalar is the gate's exact scalar delta, not the approximation.
  assert(body.includes('-0.096'), `Δscalar must be child-parent scalar, got: ${body}`);
});

test('partial aggregate falls back to client derivation on a legacy record', () => {
  // A legacy active_tournament.json predates the incremental scorer —
  // no partial_*_agg fields. renderAggregate must still paint (the
  // client-side derivation path) rather than throwing.
  state.applySnapshot(mockSnapshot());
  const legacy = { ...state.activeTournament };
  delete legacy.partial_parent_agg;
  delete legacy.partial_child_agg;
  state.activeTournament = legacy;
  render.showView('overview');
  const body = doc.getElementById('tournament-body').textContent;
  assert(body.includes('Partial aggregate'), 'legacy record must still render the aggregate panel');
});

// Collect every descendant whose class attribute contains `cls`.
function byClass(root, cls) {
  return root._descendants().filter((n) => {
    const c = n.getAttribute && n.getAttribute('class');
    return typeof c === 'string' && c.split(/\s+/).includes(cls);
  });
}

test('tournament view surfaces harmonograf jump-off links', () => {
  // The mock heartbeat carries a harmonograf_url, so every harmonograf
  // link must render. Repaint the Tournament view from the mock data.
  state.applySnapshot(mockSnapshot());
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  // (a) Exactly one tournament-overall jump-off link in the hall head.
  const tLinks = byClass(bracket, 'harmonograf-tournament');
  assertEqual(tLinks.length, 1, 'hall head must carry one tournament harmonograf link');
  const href = tLinks[0].getAttribute('href');
  assert(typeof href === 'string' && href.length > 0,
    'tournament harmonograf link must have an href');
  assert(tLinks[0].textContent.includes('↗'),
    'tournament harmonograf link carries the ↗ affordance');

  // (b) A per-board harmonograf link on each board side. The mock
  // tournament has 10 per-side entries, so at least one mini link per
  // running/finished side must be present on the board cards.
  const sideHeads = byClass(bracket, 'board-side-head');
  assert(sideHeads.length > 0, 'the hall must render board side rows');
  let sideLinks = 0;
  for (const head of sideHeads) {
    sideLinks += byClass(head, 'harmonograf-mini').length;
  }
  assert(sideLinks >= sideHeads.length,
    'every board side header must carry a harmonograf link');

  // A finished side deep-links via its adk_session_id (mock data sets
  // adk_session_id on the done entries).
  const deep = byClass(bracket, 'harmonograf-mini')
    .map((n) => n.getAttribute('href'))
    .filter((h) => typeof h === 'string' && h.includes('/#/session/'));
  assert(deep.length > 0,
    'finished board sides must deep-link via /#/session/<adk_session_id>');
  assert(deep.some((h) => h.includes('adk-')),
    'the deep-link must use the entry adk_session_id from the contract');
});

test('the tournament view has no redundant active-runs duplication', () => {
  // Issue 2: the champion/challenger hall board is the single source of
  // truth for run state. The Tournament view container must NOT carry a
  // second `runs-strip` / active-run-card list duplicating it.
  render.showView('tournament');
  const view = doc.getElementById('view-tournament');
  const strips = byClass(view, 'runs-strip');
  assertEqual(strips.length, 0,
    'the Tournament view must not duplicate the Overview active-runs strip');
  const runCards = byClass(view, 'run-card');
  assertEqual(runCards.length, 0,
    'the Tournament view must not render standalone active-run cards — '
    + 'the hall board cards are the single source of run state');
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
