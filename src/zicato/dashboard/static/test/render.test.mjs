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
  'tournament-detail', 'tournament-detail-section',
  'log-tail', 'drill-panel', 'drill-title',
  'drill-body', 'dashboard-version', 'dashboard-port', 'dashboard-build',
  'view-overview', 'view-tree', 'view-tournament', 'view-epoch', 'view-files',
  'view-conversation', 'nav-overview', 'nav-tree', 'nav-tournament', 'nav-epoch',
  'nav-files', 'epoch-overview', 'epoch-harness', 'epoch-board', 'epoch-brief',
  'epoch-scoring', 'epoch-mutations',
  'epoch-experiment-log', 'epoch-journal', 'epoch-analysis',
  'lineage-svg', 'overview-trajectory-svg',
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
  // partial aggregate (partial_champion_agg / partial_challenger_agg)
  // the instant each board unit settles; renderAggregate must consume
  // it. The Tournament view's hall owns the active-tournament panel.
  state.applySnapshot(mockSnapshot());
  state.activeTournament = {
    ...state.activeTournament,
    parent_id: 'v4',
    child_id: 'v5',
    partial_champion_agg: { drift_loss_mean: 0.42, pass_rate: 0.80, scalar: 0.137, entry_count: 3 },
    partial_challenger_agg: { drift_loss_mean: 0.31, pass_rate: 0.90, scalar: 0.041, entry_count: 3 },
  };
  render.showView('tournament');
  const body = doc.getElementById('tournament-bracket').textContent;
  assert(body.includes('0.42'), `champion drift_loss_mean must show 0.42, got: ${body}`);
  assert(body.includes('0.31'), `challenger drift_loss_mean must show 0.31, got: ${body}`);
  // The Δscalar is the gate's exact scalar delta, not the approximation.
  assert(body.includes('-0.096'), `Δscalar must be child-parent scalar, got: ${body}`);
});

test('partial aggregate table labels its rows champion / challenger', () => {
  // Terminology: tournament-framed UI uses champion/challenger, never
  // parent/child. The partial-aggregate table's two rows are the two
  // tournament sides. Falls back to those literal labels when the
  // active-tournament record carries no generation ids.
  state.applySnapshot(mockSnapshot());
  state.activeTournament = {
    ...state.activeTournament,
    parent_id: '',
    child_id: '',
    partial_champion_agg: { drift_loss_mean: 0.42, pass_rate: 0.80, scalar: 0.137, entry_count: 3 },
    partial_challenger_agg: { drift_loss_mean: 0.31, pass_rate: 0.90, scalar: 0.041, entry_count: 3 },
  };
  render.showView('tournament');
  const body = doc.getElementById('tournament-bracket').textContent;
  assert(body.includes('champion'), `partial aggregate must label a row "champion", got: ${body}`);
  assert(body.includes('challenger'),
    `partial aggregate must label a row "challenger", got: ${body}`);
  assert(!/\bside\s+drift_loss_mean\s+pass_rate\s+parent\b/.test(body.replace(/\s+/g, ' ')),
    `partial aggregate must NOT label a row "parent", got: ${body}`);
});

test('partial aggregate falls back to client derivation on a legacy record', () => {
  // A legacy active_tournament.json predates the incremental scorer —
  // no partial_*_agg fields. renderAggregate must still paint (the
  // client-side derivation path) rather than throwing.
  state.applySnapshot(mockSnapshot());
  const legacy = { ...state.activeTournament };
  delete legacy.partial_champion_agg;
  delete legacy.partial_challenger_agg;
  state.activeTournament = legacy;
  render.showView('tournament');
  const body = doc.getElementById('tournament-bracket').textContent;
  assert(body.includes('Partial aggregate'), 'legacy record must still render the aggregate panel');
});

test('gauntlet live-card "running" count excludes queued board entries', () => {
  // Regression: renderLiveCard derived `running` as total - done -
  // failed, which folded QUEUED entries into the running tally — the
  // card read "12 running" when 6 ran and 6 were queued. The count must
  // come from dataQuality so running and queued stay distinct, matching
  // the hall occupancy header.
  state.applySnapshot(mockSnapshot());
  state.activeTournament = {
    ...state.activeTournament,
    entries: [
      { entry_id: 'a', side: 'parent', status: 'done' },
      { entry_id: 'a', side: 'child', status: 'done' },
      { entry_id: 'b', side: 'parent', status: 'running' },
      { entry_id: 'b', side: 'child', status: 'running' },
      { entry_id: 'c', side: 'parent', status: 'queued' },
      { entry_id: 'c', side: 'child', status: 'queued' },
    ],
  };
  render.showView('tournament');
  const body = doc.getElementById('tournament-bracket').textContent;
  assert(body.includes('2 running'),
    `live-card must report only the 2 running entries, got: ${body}`);
  assert(!body.includes('4 running'),
    `live-card must NOT count queued entries as running, got: ${body}`);
  assert(body.includes('2 queued'),
    `live-card must report the 2 queued entries separately, got: ${body}`);
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

test('gauntlet: the champion lineage renders as a green spine', () => {
  // Regression: the caption promises "Champion lineage runs the green
  // spine". Every node on the champion lineage — the seed and every
  // promoted generation — must render as a green spine node
  // (`bracket-champ.is-spine`). The seed must NOT be styled as a
  // neutral gray box; the seed tag is a label, not a different node.
  state.applySnapshot(mockSnapshot());
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  const spineNodes = byClass(bracket, 'is-spine');
  // The mock lineage is [v0, v2, v4] — three green spine nodes.
  assertEqual(spineNodes.length, 3,
    `every champion-lineage node must be a green spine node, got ${spineNodes.length}`);
  // Each spine node is a champion box (the green-styled class).
  for (const n of spineNodes) {
    assert(n.getAttribute('class').split(/\s+/).includes('bracket-champ'),
      'a spine node must carry the green bracket-champ class');
  }
  // The reigning (tail) champion is marked is-current.
  const current = byClass(bracket, 'is-current');
  assertEqual(current.length, 1, 'exactly one node is the reigning champion');

  // The seed must still be present and still labelled "seed" — but it
  // is a spine node, never a bare gray seed box.
  const seeds = byClass(bracket, 'is-seed');
  assertEqual(seeds.length, 1, 'the lineage has one seed node');
  assert(seeds[0].getAttribute('class').split(/\s+/).includes('is-spine'),
    'the seed must be ON the green spine — not a neutral gray box');
});

test('gauntlet: a seed that is also the reigning champion is a green spine', () => {
  // The live bug: v0 is both the seed AND the reigning champion
  // (nothing promoted past it). It must render green — a single green
  // spine node — not a neutral gray "SEED" box.
  state.applySnapshot(mockSnapshot());
  state.bracket = { epoch_id: 'e', champion_lineage: ['v0'], matchups: [] };
  state.lineage = { generations: [], experiments: [] };
  state.activeTournament = null;
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  const champs = byClass(bracket, 'bracket-champ');
  assertEqual(champs.length, 1, 'a single-node lineage draws one champion box');
  const cls = champs[0].getAttribute('class').split(/\s+/);
  assert(cls.includes('is-spine'),
    'the lone seed-champion must be a green spine node');
  assert(cls.includes('is-current'),
    'the lone seed-champion is the reigning champion');
  // It is tagged as both seed and champion so the role is unambiguous.
  assert(champs[0].textContent.toLowerCase().includes('champion'),
    `the seed-champion node must read as the champion, got "${champs[0].textContent}"`);
});

test('gauntlet: an aborted challenger (no decided verdict) renders as its own node', () => {
  // Regression: a challenger that ran but never reached a final verdict
  // — the run torn down mid-tournament, or still in progress — was
  // omitted entirely. It must surface on the gauntlet as a distinct
  // node (bracket-aborted), NOT as a red discarded node, hanging below
  // the champion it was challenging.
  state.applySnapshot(mockSnapshot());
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  // The mock bracket carries challenger v3x with decision:null.
  const aborted = byClass(bracket, 'bracket-aborted');
  assert(aborted.length >= 1,
    `an aborted challenger must render its own node, got ${aborted.length}`);
  const node = aborted[0];
  // It is distinct from a discarded node — it must NOT be a red
  // "discarded" node.
  assert(!node.textContent.toLowerCase().includes('discarded'),
    'an aborted challenger must not read as a discarded (rejected) node');
  assert(node.textContent.toLowerCase().includes('incomplete'),
    `an aborted challenger carries a distinct status, got "${node.textContent}"`);
  assert(node.textContent.includes('v3x'),
    `the aborted node names the challenger, got "${node.textContent}"`);
  // It is clickable — it routes to the matchup like any other node.
  assert(node._listeners && (node._listeners.click || node._listeners.keydown),
    'the aborted challenger node must be clickable');

  // The red discarded rendering is NOT regressed — v1 / v2x still
  // render as discarded nodes.
  const body = bracket.textContent.toLowerCase();
  assert(body.includes('discarded'),
    'decided rejections must still render as discarded nodes');
});

test('gauntlet: a challenger that ran with no matchup row still appears', () => {
  // The torn-down case proper: a challenger generation that ran but has
  // NO matchup record at all (the verdict row was never written). It is
  // synthesized onto the gauntlet from the lineage feed — a non-promoted
  // generation whose parent is a champion-lineage node.
  state.applySnapshot(mockSnapshot());
  state.bracket = {
    epoch_id: 'e',
    champion_lineage: ['v0', 'v1'],
    matchups: [
      { champion: 'v0', challenger: 'v1', decision: 'promoted',
        delta_scalar: -0.05, ran_at: '2026-05-10T10:00:00Z' },
    ],
  };
  // v2 ran against champion v1; the run was torn down before any
  // tournament row was written — it exists only in the lineage feed.
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null, promoted: true },
      { generation_id: 'v1', parent_generation_id: 'v0', promoted: true },
      { generation_id: 'v2', parent_generation_id: 'v1', promoted: false },
    ],
    experiments: [],
  };
  state.activeTournament = null;
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  const aborted = byClass(bracket, 'bracket-aborted');
  assertEqual(aborted.length, 1,
    `a torn-down challenger must surface from the lineage feed, got ${aborted.length}`);
  assert(aborted[0].textContent.includes('v2'),
    `the synthesized node names the torn-down challenger, got "${aborted[0].textContent}"`);
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

  // The chart must be LABELLED: a reader can tell which generation and
  // what loss each point is. The x-axis carries the generation ids, the
  // y-axis carries scalar tick values, and each axis is titled.
  const trajText = traj._descendants()
    .filter((n) => n.localName === 'text')
    .map((n) => n.textContent);
  assert(
    ['v0', 'v1', 'v2', 'v4'].every((id) => trajText.includes(id)),
    `the trajectory x-axis must label every scored generation, got ${JSON.stringify(trajText)}`,
  );
  assert(
    trajText.some((t) => /loss/i.test(t)),
    'the trajectory must carry a y-axis title naming the scalar as a loss',
  );
  assert(
    trajText.some((t) => /generation/i.test(t)),
    'the trajectory must carry an x-axis title naming the generation dimension',
  );
  // Per-point scalar value labels — the mock points carry 0.49 / 0.51 /
  // 0.43 / 0.38, each formatted to two decimals.
  assert(
    ['0.49', '0.51', '0.43', '0.38'].every((v) => trajText.includes(v)),
    `each trajectory point must show its scalar value, got ${JSON.stringify(trajText)}`,
  );
});

test('the Tree view is purely the lineage DAG — no score-trajectory chart', () => {
  // The score trajectory lives ONLY on the Overview. The Tree view
  // renders the lineage graph and nothing else trajectory-shaped: the
  // duplicate #trajectory-svg / #trajectory-section is gone, and the
  // render layer no longer paints into one.
  state.applySnapshot(mockSnapshot());
  render.showView('tree');

  assert(
    doc.getElementById('trajectory-svg') == null,
    'the Tree view must not carry a #trajectory-svg score-trajectory chart',
  );
  assert(
    doc.getElementById('trajectory-section') == null,
    'the duplicate #trajectory-section must be removed from the Tree view',
  );
  // The lineage graph itself still renders.
  const lineage = doc.getElementById('lineage-svg');
  assert(lineage != null, 'the Tree view must still render the lineage graph');
  assert(
    lineage._descendants().length > 0,
    'the Tree view lineage graph must paint its DAG',
  );
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

test('selecting a board matchup renders the Matchup detail, not the placeholder', () => {
  // Regression: clicking a board card routes to #/tournament/conv/<entry>
  // and selects a board ENTRY (never state.selectedMatchup). The detail
  // panel must render that entry's champion-vs-challenger head-to-head —
  // it used to silently stay on the "Select a matchup above." placeholder
  // because renderMatchupDetail only keyed off state.selectedMatchup.
  state.mock = true;
  state.applySnapshot(mockSnapshot());
  state.selectedMatchup = null;
  // Clear any board-entry selection leaked from an earlier test — a
  // bare #/conversation routes to selectConversation(null).
  globalThis.location.hash = '#/conversation';
  render.applyRoute();
  render.showView('tournament');
  const detail = doc.getElementById('tournament-detail');

  // Bare #/tournament with nothing selected keeps the placeholder.
  globalThis.location.hash = '#/tournament';
  render.applyRoute();
  assert(textOf(detail).includes('Select a matchup above'),
    'a bare #/tournament must keep the placeholder');

  // A #/tournament/conv/<entry> route must render the matchup detail —
  // exercise a single-turn-style board entry first.
  globalThis.location.hash = '#/tournament/conv/extract_invoice_001';
  render.applyRoute();
  const single = doc.getElementById('tournament-detail');
  assert(!textOf(single).includes('Select a matchup above'),
    'a selected matchup must NOT show the placeholder');
  assert(textOf(single).includes('Matchup detail'),
    'the panel must render a Matchup detail heading');
  assert(textOf(single).includes('extract_invoice_001'),
    'the panel must name the selected board entry');
  assert(textOf(single).includes('Champion vs challenger'),
    'the panel must render the champion-vs-challenger sides');
  assert(textOf(single).includes('Verdict'),
    'the panel must render the per-matchup verdict');
  // extract_invoice_001 has both sides scored in the mock (champion
  // 0.23 vs challenger 0.18) — the verdict line resolves a winner.
  assert(/Challenger leads|Champion holds|Flat/.test(textOf(single)),
    'a both-sides-done matchup must render a resolved verdict line');

  // The same path for a multi-turn board entry (the cards emit the
  // `conv` kind for both — the entry id distinguishes them).
  globalThis.location.hash = '#/tournament/conv/multi_turn_picky';
  render.applyRoute();
  const multi = doc.getElementById('tournament-detail');
  assert(!textOf(multi).includes('Select a matchup above'),
    'a multi-turn matchup must also render its detail');
  assert(textOf(multi).includes('multi_turn_picky'),
    'the panel must name the multi-turn board entry');
});

test('conversation diff survives a #/tournament/{gen} → conv route switch', () => {
  // BUG 2: a #/tournament/{genId} deep-link sets state.selectedMatchup.
  // renderMatchupDetail checks that FIRST, so a later board-card drill
  // (#/tournament/conv/<entry>) used to render the stale gen-keyed panel
  // and the conversation diff landed under the wrong matchup. Each
  // tournament sub-route must clear the other's selection so the
  // conversation diff renders against its own board entry.
  state.mock = true;
  state.applySnapshot(mockSnapshot());

  // Open a matchup BY GENERATION first — this sets state.selectedMatchup.
  globalThis.location.hash = '#/tournament/v1';
  render.applyRoute();
  assertEqual(state.selectedMatchup, 'v1',
    'a #/tournament/{genId} deep-link must select that matchup');

  // Now drill into a board card — the conv route must take over the
  // detail panel, not leave it shadowed by the stale gen selection.
  globalThis.location.hash = '#/tournament/conv/extract_invoice_001';
  render.applyRoute();
  assertEqual(state.selectedMatchup, null,
    'the conv route must clear the stale matchup-by-gen selection');
  const detail = doc.getElementById('tournament-detail');
  assert(textOf(detail).includes('extract_invoice_001'),
    'the detail panel must render the board entry the conv route names');
  assert(!textOf(detail).includes('v1 vs'),
    'the detail panel must NOT still show the stale v1 matchup heading');
  // The conversation diff (mock transcripts) must actually populate —
  // both sides carry a run, so neither column degrades to "no run".
  assert(textOf(detail).includes('Conversation diff'),
    'the board-entry detail must include the inline conversation diff');
  assert(!textOf(detail).includes('the run has not started'),
    'the conversation columns must show the mock transcripts, not "no run"');
});

test('a #/tournament/{gen} route does not yank the scroll on every render', () => {
  // BUG 1: openMatchup() scrolled the detail panel into view, and
  // applyRoute() re-runs openMatchup on EVERY render (each SSE delta) for
  // a #/tournament/{genId} deep-link. Re-opening the SAME matchup must be
  // idempotent — scrollIntoView fires only when the matchup changes.
  state.mock = true;
  state.applySnapshot(mockSnapshot());

  let scrolls = 0;
  const section = doc.getElementById('tournament-detail-section');
  section.scrollIntoView = () => { scrolls += 1; };

  globalThis.location.hash = '#/tournament/v1';
  render.applyRoute();
  assertEqual(scrolls, 1, 'opening a new matchup scrolls it into view once');

  // Subsequent renders (SSE deltas) re-run applyRoute for the same route.
  render.applyRoute();
  render.applyRoute();
  render.renderAll();
  assertEqual(scrolls, 1,
    're-rendering the same #/tournament/{genId} route must NOT re-scroll');
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

// -- Epoch journal ----------------------------------------------------
// The journal renders the epoch's round-by-round markdown narrative.
// The bug being guarded: the markdown was emitted verbatim, so the
// `**field**:` markers showed as literal asterisks on the page. The
// journal must now render as a clean labelled timeline — no raw `**`.

test('epoch journal renders without any literal ** markdown markers', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('epoch');
  const journal = doc.getElementById('epoch-journal');
  assert(journal.children.length > 0, 'the journal panel must render content');
  const text = journal.textContent;
  // The defect: raw markdown bold markers leaking onto the page.
  assert(!text.includes('**'),
    'no literal ** markers — the journal markdown must be rendered');
  // The journal field labels must survive as readable text (stripped
  // of their `**` fence), not vanish.
  assert(text.includes('proposed_at') && text.includes('modulating')
    && text.includes('why') && text.includes('outcome'),
    'the journal field labels must render as labelled key/value rows');
  // The prose body of an entry must also render.
  assert(text.includes('Validate-before-emit cleared'),
    'free prose in a journal entry must render');
});

test('epoch journal renders one timeline entry per round section', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('epoch');
  const journal = doc.getElementById('epoch-journal');
  const entries = journal._descendants()
    .filter((n) => n.classList.contains('journal-entry'));
  // The mock journal carries two `## v{N}` sections.
  assertEqual(entries.length, 2, 'one timeline entry per journal section');
  // Each entry surfaces its heading and a key/value field list.
  assert(entries[0].textContent.includes('v1'), 'an entry shows its round heading');
  const labels = journal._descendants()
    .filter((n) => n.classList.contains('journal-field-label'));
  assert(labels.length > 0, 'field lines render as labelled key/value rows');
  // The rejected entry surfaces its rejection_reason field.
  assert(journal.textContent.includes('rejection_reason'),
    'a rejected round surfaces its rejection_reason field');
});

test('epoch journal degrades to a muted empty line when absent', () => {
  state.epochDef = { epoch_id: '2026-05-15_e1', journal: '' };
  render.showView('epoch');
  const journal = doc.getElementById('epoch-journal');
  assert(journal.textContent.includes('No journal recorded'),
    'an epoch with no journal shows a muted empty state');
});

// -- Completed-tournament per-entry outcomes --------------------------
// A completed (non-live) matchup must show its per-board outcomes: the
// matchup-detail panel populates the Per-entry A/B grid and the Scalar
// breakdown rather than rendering "No per-entry grid recorded".

test('completed matchup grid populates from the index ab_grid shape', () => {
  // The /api/tournaments/{gen} endpoint sources its grid from the
  // SQLite index as `ab_grid`, whose cells carry `parent_pass_fail` /
  // `child_pass_fail`. The matchup-detail panel must fold that shape
  // into the rendered grid — not drop it on "No per-entry grid".
  state.mock = true;
  state.applySnapshot(mockSnapshot());
  // Seed the detail cache with a backend (index) shaped payload for the
  // already-completed v1 matchup.
  state.matchupDetail.set('v1', {
    epoch_id: '2026-05-15_e1',
    generation_id: 'v1',
    champion: 'v0',
    decision: 'rejected',
    rejection_reason: 'pass-rate regression on schema_response',
    patches: [],
    ab_grid: [
      { entry_id: 'extract_invoice_001', parent_drift_loss: 0.30,
        child_drift_loss: 0.21, parent_pass_fail: true, child_pass_fail: true,
        verdict: 'improved' },
      { entry_id: 'schema_response', parent_drift_loss: 0.12,
        child_drift_loss: 0.34, parent_pass_fail: true, child_pass_fail: false,
        verdict: 'regressed' },
    ],
  });
  globalThis.location.hash = '#/tournament/v1';
  render.applyRoute();
  const detail = doc.getElementById('tournament-detail');
  const text = textOf(detail);
  assert(!text.includes('No per-entry grid recorded'),
    'a completed matchup with an index ab_grid must NOT show the empty grid');
  assert(text.includes('extract_invoice_001') && text.includes('schema_response'),
    'every board entry must appear in the per-entry grid');
  // The grid carries the per-entry Δ and "won by" columns.
  const grid = detail._descendants().find(
    (n) => n.classList && n.classList.contains('ab-grid'));
  assert(grid, 'the per-entry A/B grid table must render');
  const wonCells = grid._descendants().filter(
    (n) => n.classList && n.classList.contains('ab-won'));
  assert(wonCells.length === 2, 'each board row shows which side won it');
  // extract_invoice_001 improved -> challenger v1 won; schema_response
  // regressed -> champion v0 won.
  const wonText = wonCells.map((n) => n.textContent);
  assert(wonText.includes('v1') && wonText.includes('v0'),
    'the won-by column names the winning generation per board');
});

test('completed matchup grid falls back to the persisted loss-file endpoint', async () => {
  // When the index-sourced detail carries NO grid (an empty ab_grid —
  // the index was never built for this finished tournament) the panel
  // must fetch /api/matchup-grid and render the per-entry outcomes the
  // persisted loss.json files hold.
  state.applySnapshot(mockSnapshot());
  state.mock = false;   // loadMatchupGrid only fetches for a real workspace
  const requested = [];
  const gridPayload = {
    epoch_id: '2026-05-15_e1', champion: 'v0', challenger: 'v1',
    source: 'loss_files',
    entry_grid: [
      { entry_id: 'extract_invoice_001', parent_drift_loss: 0.30,
        child_drift_loss: 0.21, parent_pass: true, child_pass: true,
        delta: -0.09, verdict: 'improved', won_by: 'v1' },
      { entry_id: 'schema_response', parent_drift_loss: 0.12,
        child_drift_loss: 0.34, parent_pass: true, child_pass: false,
        delta: 0.22, verdict: 'regressed', won_by: 'v0' },
    ],
    scalar: { parent: 0.41, child: 0.43, delta: 0.022,
      components: { drift: -0.04, cost: 0.01 } },
  };
  globalThis.fetch = async (path) => {
    requested.push(path);
    // The detail / drift-movements drill-downs keep their empty index
    // shapes — only the matchup-grid endpoint returns the loss-file grid.
    let body = { ab_grid: [], entry_grid: [], movements: [] };
    if (String(path).includes('/api/matchup-grid/')) body = gridPayload;
    return { ok: true, json: async () => body };
  };
  // The detail endpoint answered, but with an EMPTY index grid.
  state.matchupDetail.set('v1', {
    epoch_id: '2026-05-15_e1', generation_id: 'v1', champion: 'v0',
    decision: 'rejected', patches: [], ab_grid: [],
  });
  globalThis.location.hash = '#/tournament/v1';
  render.applyRoute();
  // The fetch is async — let the loadMatchupGrid promise settle, then
  // it re-renders the detail panel itself.
  await new Promise((resolve) => setTimeout(resolve, 5));
  const gridReq = requested.find((p) => String(p).includes('/api/matchup-grid/'));
  assert(gridReq, 'an empty index grid must trigger the persisted-loss-file fetch');
  assert(gridReq.includes('2026-05-15_e1') && gridReq.includes('/v0/') &&
    gridReq.endsWith('/v1'),
    'the matchup-grid request carries the epoch, champion and challenger');
  const detail = doc.getElementById('tournament-detail');
  const text = textOf(detail);
  assert(!text.includes('No per-entry grid recorded'),
    'the persisted loss files must populate the grid for a completed matchup');
  assert(!text.includes('No scalar breakdown recorded'),
    'the persisted gen_score aggregates must populate the scalar breakdown');
  assert(text.includes('extract_invoice_001') && text.includes('schema_response'),
    'the persisted grid lists every board entry');
  state.mock = true;
  delete globalThis.fetch;
});

await run();
