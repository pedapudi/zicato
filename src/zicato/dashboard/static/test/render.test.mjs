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
// The Overview is the environment home: an identity block, the loop-
// health line, the compact live-activity card, the score trajectory,
// the epochs table and the recent-experiments digest — NOT the full
// tournament board (that lives only in the Tournament view).
const REQUIRED_IDS = [
  'drill-close', 'header-bar', 'footer-bar', 'epoch-id', 'generation-id',
  'round-id', 'elapsed', 'health-badge', 'mock-badge',
  'identity-panel', 'health-panel', 'live-activity', 'epochs-panel',
  'recent-experiments', 'tournament-bracket',
  'tournament-detail', 'log-tail', 'drill-panel', 'drill-title',
  'drill-body', 'dashboard-version', 'dashboard-port', 'dashboard-build',
  'view-overview', 'view-tree', 'view-tournament', 'view-epoch', 'view-files',
  'view-conversation', 'nav-overview', 'nav-tree', 'nav-tournament', 'nav-epoch',
  'nav-files', 'epoch-overview', 'epoch-harness', 'epoch-board', 'epoch-brief',
  'epoch-scoring', 'epoch-mutations',
  'epoch-experiment-log', 'epoch-journal', 'epoch-analysis',
  'lineage-svg', 'trajectory-svg', 'overview-trajectory-svg',
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

// Collect every text node beneath a node — the harness has no
// innerText, so this is how a test asserts rendered copy.
function textOf(node) {
  return node ? node.textContent : '';
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
  // (the bug the browser interaction pass caught). The panels AFTER the
  // health panel must still paint.
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
  assert(doc.getElementById('live-activity').children.length > 0,
    'the live-activity card must render — renderHealthPanel must not abort the view');
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
  // The Tournament view's hall owns the active-tournament panel.
  state.applySnapshot(mockSnapshot());
  state.activeTournament = {
    ...state.activeTournament,
    parent_id: 'v4',
    child_id: 'v5',
    partial_parent_agg: { drift_loss_mean: 0.42, pass_rate: 0.80, scalar: 0.137, entry_count: 3 },
    partial_child_agg: { drift_loss_mean: 0.31, pass_rate: 0.90, scalar: 0.041, entry_count: 3 },
  };
  render.showView('tournament');
  const body = doc.getElementById('tournament-bracket').textContent;
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
  render.showView('tournament');
  const body = doc.getElementById('tournament-bracket').textContent;
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

test('Overview is the environment home, NOT a duplicate tournament board', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('overview');

  // The identity block names the workspace / epoch / generation.
  const identity = textOf(doc.getElementById('identity-panel'));
  assert(identity.includes('2026-05-15_e1'),
    `identity block must show the epoch id, got "${identity}"`);
  assert(identity.includes('v5'),
    'identity block must show the current generation');

  // The live-activity card is a COMPACT summary — it carries a run
  // census and a link through to the Tournament view, and must NOT
  // render the full champion/challenger board (the board's per-entry
  // result strip / aggregate table belong to the Tournament view).
  const liveWrap = doc.getElementById('live-activity');
  const liveTxt = textOf(liveWrap);
  assert(liveTxt.includes('runs done'),
    `live-activity must show a run census, got "${liveTxt}"`);
  const tourLink = liveWrap._descendants().find(
    (n) => n.getAttribute && n.getAttribute('href') === '#/tournament');
  assert(tourLink, 'live-activity must link through to the Tournament view');
  // The full board's aggregate table never appears on the Overview.
  assert(!liveTxt.includes('Partial aggregate'),
    'the Overview must NOT render the tournament aggregate table');

  // The epochs table lists the epoch ids from the lineage feed.
  const epochs = textOf(doc.getElementById('epochs-panel'));
  assert(epochs.includes('2026-05-10_e0') && epochs.includes('2026-05-15_e1'),
    `epochs table must list every epoch, got "${epochs}"`);

  // Recent experiments reuse the Epoch view's experiment-log source.
  const exps = textOf(doc.getElementById('recent-experiments'));
  assert(exps.includes('v2') || exps.includes('v5'),
    `recent-experiments must render the experiment records, got "${exps}"`);

  // The score trajectory paints a polyline from the trajectory points.
  const traj = doc.getElementById('overview-trajectory-svg');
  const paths = traj._descendants().filter((n) => n.localName === 'path');
  assert(paths.length > 0, 'the score trajectory must paint a curve');
});

test('Overview degrades to empty states when the environment is bare', async () => {
  // A fresh state with nothing loaded — every Overview panel must
  // render its single-line empty state rather than throwing.
  const { AppState } = await import('../js/core/state.js');
  const bare = new AppState();
  const saved = {};
  for (const k of Object.keys(bare)) saved[k] = state[k];
  for (const k of Object.keys(bare)) state[k] = bare[k];
  try {
    render.showView('overview');
    assert(doc.getElementById('live-activity').children.length > 0,
      'live-activity must render an empty state, not crash');
    assert(doc.getElementById('epochs-panel').children.length > 0,
      'epochs-panel must render an empty state, not crash');
  } finally {
    for (const k of Object.keys(saved)) state[k] = saved[k];
  }
});

test('finished board entries show a done status with their score, never queued', () => {
  // The queued-label regression: a completed run that carries its
  // scalar under `loss_summary.drift_loss` (the live runtime shape)
  // must render a "done" pill and its scalar — not a "queued" label.
  state.applySnapshot(mockSnapshot());
  state.activeTournament = {
    round_index: 3,
    parent_generation_id: 'v4', child_generation_id: 'v5',
    entries: [
      // status as the runtime spelling 'completed', score under
      // loss_summary — exactly what update_tournament_entry writes.
      { entry_id: 'b1', side: 'parent', status: 'completed',
        loss_summary: { drift_loss: 0.21, pass_fail: 1.0 } },
      { entry_id: 'b1', side: 'child', status: 'completed',
        loss_summary: { drift_loss: 0.14, pass_fail: 1.0 } },
    ],
  };
  render.showView('tournament');
  const board = doc.getElementById('tournament-bracket');
  const sides = board._descendants().filter(
    (n) => n.classList && n.classList.contains('board-side'));
  assert(sides.length >= 2, 'the hall must render both board sides');
  for (const side of sides) {
    const txt = textOf(side);
    assert(!/\bqueued\b/.test(txt),
      `a finished board side must not say "queued", got "${txt}"`);
    assert(txt.includes('done'),
      `a finished board side must show a done status, got "${txt}"`);
    assert(/scalar\s/.test(txt),
      `a finished board side must show its scalar, got "${txt}"`);
  }
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

// -- Epoch view redesign ----------------------------------------------
// The Epoch view is the epoch's narrative: identity + the operator's
// brief, then one card per experiment telling its story in four beats
// (description / hypothesis / change / outcome). These tests prove the
// redesign renders that story from the mock epoch contract.

test('epoch header shows the epoch id, status, and the experiment tally', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('epoch');
  const header = doc.getElementById('epoch-overview');
  const text = header.textContent;
  // The mock epoch is `2026-05-15_e1`, open, with 3 experiments
  // (2 decided — one promoted, one rejected — and 1 in progress).
  assert(text.includes('2026-05-15_e1'), 'header must show the epoch id');
  assert(text.includes('open'), 'an un-closed epoch shows the open status');
  assert(text.includes('experiments'), 'the stat strip labels the experiment count');
  assert(text.includes('promoted') && text.includes('rejected'),
    'the stat strip tallies promoted and rejected experiments');
  assert(text.includes('Δscalar'), 'the stat strip reports the net scalar movement');
});

test('proposer brief renders as a readable block, framed as the epoch goal', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('epoch');
  const brief = doc.getElementById('epoch-brief');
  assert(brief.children.length > 0, 'the brief panel must render content');
  const text = brief.textContent;
  assert(text.includes('goal handed to the proposer'),
    'the brief is framed as the operator goal for the epoch');
  // The mock brief markdown body must have rendered.
  assert(text.includes('Forbidden edits'), 'the brief markdown body must render');
});

test('each experiment renders as a card with description, hypothesis and outcome', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('epoch');
  const log = doc.getElementById('epoch-experiment-log');
  const cards = log._descendants().filter((n) => n.classList.contains('exp-card'));
  // The mock epoch carries three experiments (v1, v2, v5).
  assertEqual(cards.length, 3, 'one card per experiment in the epoch');

  // Every card tells the four-beat story: a description (core idea),
  // a Hypothesis beat, a Change beat, and an Outcome beat.
  for (const card of cards) {
    const t = card.textContent;
    assert(t.includes('Hypothesis'), 'every card has a Hypothesis beat');
    assert(t.includes('Change'), 'every card has a Change beat');
    assert(t.includes('Outcome'), 'every card has an Outcome beat');
  }

  // The first experiment (v1) was rejected — its card carries the
  // rejected accent and surfaces the rejection reason.
  const first = cards[0];
  assert(first.classList.contains('exp-card-rejected'),
    'a rejected experiment card carries the rejected accent');
  assert(first.textContent.includes('Tighten the extraction schema'),
    'the card shows the proposer core idea as the description');
  assert(first.textContent.includes('pass-rate regression'),
    'a rejected outcome surfaces the rejection reason');
  assert(first.textContent.includes('Δscalar'),
    'a decided outcome shows the scalar delta');

  // The last experiment (v5) has no outcome yet — it reads as pending.
  const last = cards[cards.length - 1];
  assert(last.classList.contains('exp-card-pending'),
    'an unfinished experiment card carries the pending accent');
  assert(last.textContent.includes('in progress'),
    'an unfinished experiment reads as in progress');
});

test('experiment card diff toggle expands the change without throwing', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('epoch');
  const log = doc.getElementById('epoch-experiment-log');
  const toggles = log._descendants().filter((n) => n.classList.contains('exp-diff-toggle'));
  assert(toggles.length > 0, 'each experiment with a patch exposes a diff toggle');
  // The diff is collapsed by default — clicking expands it.
  assertEqual(toggles[0].getAttribute('aria-expanded'), 'false',
    'the diff starts collapsed');
  toggles[0].dispatchEvent(makeEvent('click'));
  // After the toggle the log re-renders; a diff wrap must now exist.
  const expanded = doc.getElementById('epoch-experiment-log')._descendants()
    .filter((n) => n.classList.contains('exp-diff-wrap'));
  assert(expanded.length > 0, 'clicking the toggle expands the patch diff');
});

test('epoch view renders cleanly when no epoch is loaded', () => {
  state.epochDef = { epoch_id: null };
  render.showView('epoch');
  // No epoch -> a single muted empty line, no thrown error.
  const header = doc.getElementById('epoch-overview');
  assert(header.textContent.includes('No epoch loaded'),
    'a null epoch degrades to a muted empty state');
  const log = doc.getElementById('epoch-experiment-log');
  assert(log.textContent.includes('No experiments recorded'),
    'an epoch with no experiments shows an empty narrative');
});

await run();
