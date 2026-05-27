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
  // The Experiment-log and Journal panels are MERGED into one
  // `epoch-experiment-log` panel — there is no longer an
  // `epoch-journal` panel. `epoch-experiments-section` is the named
  // section wrapper the merged log lives in.
  'epoch-experiments-section', 'epoch-experiment-log', 'epoch-analysis',
  'lineage-svg', 'overview-trajectory-svg',
  'heatmap-svg', 'conversation-panel',
  'files-patches', 'files-patches-title',
  'files-cumulative', 'files-cumulative-title',
  'mutations-list-pane', 'mutations-detail-pane',
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

test('top bar tracks heartbeat changes between SSE ticks', () => {
  // Regression (bug #170): the dashboard top bar — epoch / gen / round —
  // stayed stale between SSE ticks. The body of the page kept updating
  // (gauntlet cards, partial-aggregates, board scalars) but the top-bar
  // nodes only refreshed when a route change forced a redigest. The fix:
  //   * renderHeader reads epoch / gen / round straight off state.heartbeat;
  //   * _relevantStateDigest's hbDigest captures epoch_id / generation_id /
  //     round_index so a heartbeat-only delta still flips the gate and
  //     re-runs renderHeader.
  state.mock = true;
  state.heartbeat = {
    epoch_id: '2026-05-20_presn',
    generation_id: 'v5',
    round_index: 0,
    last_heartbeat: '2026-05-19T00:00:00Z',
    started_at: '2026-05-19T00:00:00Z',
  };
  // Drop state.epoch.* so we prove the top bar reads from the heartbeat
  // and not from the legacy snapshot summary.
  state.epoch = { id: '—', generation: '—', round: '—', startedAt: null };
  render.renderAll();
  assert(
    doc.getElementById('epoch-id').textContent.includes('2026-05-20_presn'),
    `top-bar epoch should show 2026-05-20_presn, got "${doc.getElementById('epoch-id').textContent}"`,
  );
  assert(
    doc.getElementById('generation-id').textContent.includes('v5'),
    `top-bar generation should show v5, got "${doc.getElementById('generation-id').textContent}"`,
  );
  assert(
    doc.getElementById('round-id').textContent.includes('· 0'),
    `top-bar round should show 0, got "${doc.getElementById('round-id').textContent}"`,
  );

  // The orchestrator transitions to round 1 / generation v6 — the
  // heartbeat is the same delta a `state_change` SSE event eventually
  // folds in via applyEnvironment + setHeartbeat. The top bar must
  // re-render through renderAll's digest gate without a route change.
  state.heartbeat = {
    epoch_id: '2026-05-20_presn',
    generation_id: 'v6',
    round_index: 1,
    last_heartbeat: '2026-05-19T00:00:30Z',
    started_at: '2026-05-19T00:00:00Z',
  };
  render.renderAll();
  assert(
    doc.getElementById('generation-id').textContent.includes('v6'),
    `top-bar generation must update to v6, got "${doc.getElementById('generation-id').textContent}"`,
  );
  assert(
    doc.getElementById('round-id').textContent.includes('· 1'),
    `top-bar round must update to 1, got "${doc.getElementById('round-id').textContent}"`,
  );
  assert(
    !doc.getElementById('generation-id').textContent.includes('v5'),
    `top-bar must not carry the stale v5, got "${doc.getElementById('generation-id').textContent}"`,
  );

  // A heartbeat that bumps ONLY the epoch_id — without touching gen or
  // round — must still flip the digest gate. This pins the regression:
  // before the fix, hbDigest omitted epoch_id, so a heartbeat-only epoch
  // change could not break through the gate at all.
  state.heartbeat = {
    epoch_id: '2026-05-21_followup',
    generation_id: 'v6',
    round_index: 1,
    last_heartbeat: '2026-05-19T00:01:00Z',
    started_at: '2026-05-19T00:00:00Z',
  };
  render.renderAll();
  assert(
    doc.getElementById('epoch-id').textContent.includes('2026-05-21_followup'),
    `top-bar epoch must update from heartbeat alone, got "${doc.getElementById('epoch-id').textContent}"`,
  );
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

test('gauntlet: 2026-05-19_presn shape — v0 spine, v1 discarded, v2 incomplete', () => {
  // Mirrors the live `2026-05-19_presn` epoch:
  //   * v0 is the seed AND the reigning champion (lineage = ['v0']);
  //   * v1 ran and was rejected (matchup row carries decision='rejected');
  //   * v2 ran but no tournament row was written (outcome=null on its
  //     experiment — surfaces from the lineage feed as an aborted node).
  // The gauntlet must render: v0 as a green spine node, v1 as a red
  // discarded card hanging below v0, and v2 as an amber incomplete
  // card also hanging below v0.
  state.applySnapshot(mockSnapshot());
  state.bracket = {
    epoch_id: '2026-05-19_presn',
    champion_lineage: ['v0'],
    matchups: [
      { champion: 'v0', challenger: 'v1', decision: 'rejected',
        delta_scalar: 0.91, rejection_reason: 'loss rose',
        ran_at: '2026-05-19T02:02:28Z' },
    ],
  };
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null, promoted: true },
      { generation_id: 'v1', parent_generation_id: 'v0', promoted: false },
      { generation_id: 'v2', parent_generation_id: 'v0', promoted: null },
    ],
    experiments: [],
  };
  state.activeTournament = null;
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  // v0 — one green spine node, tagged both seed and champion.
  const spineNodes = byClass(bracket, 'is-spine');
  assertEqual(spineNodes.length, 1,
    `v0 is the only spine node, got ${spineNodes.length}`);
  const v0cls = spineNodes[0].getAttribute('class').split(/\s+/);
  assert(v0cls.includes('is-seed') && v0cls.includes('is-current'),
    'v0 is both the seed and the reigning champion');
  assert(spineNodes[0].textContent.includes('v0'),
    `the spine node names v0, got "${spineNodes[0].textContent}"`);

  // v1 — a red discarded card under v0.
  const discarded = byClass(bracket, 'bracket-loser')
    .filter((n) => !n.getAttribute('class').split(/\s+/).includes('bracket-aborted'));
  assertEqual(discarded.length, 1,
    `v1 is the only discarded card, got ${discarded.length}`);
  assert(discarded[0].textContent.includes('v1'),
    `the discarded card names v1, got "${discarded[0].textContent}"`);
  assert(discarded[0].textContent.toLowerCase().includes('discarded'),
    `v1 reads as discarded, got "${discarded[0].textContent}"`);

  // v2 — an amber incomplete card under v0, surfaced from the lineage
  // feed (no matchup row was written for it).
  const aborted = byClass(bracket, 'bracket-aborted');
  assertEqual(aborted.length, 1,
    `v2 is the only incomplete card, got ${aborted.length}`);
  assert(aborted[0].textContent.includes('v2'),
    `the incomplete card names v2, got "${aborted[0].textContent}"`);
  assert(aborted[0].textContent.toLowerCase().includes('incomplete'),
    `v2 reads as incomplete (not discarded), got "${aborted[0].textContent}"`);
  assert(!aborted[0].textContent.toLowerCase().includes('discarded'),
    'v2 must NOT read as discarded — it is an aborted / in-progress node');
});

test('gauntlet: the full epoch lineage walks past a stale bracket payload', () => {
  // Live bug `2026-05-20_presn`: the bracket payload's `champion_lineage`
  // lagged the runtime — the index DB carried only `v0` as promoted with
  // null `parent_generation_id` on every row, even though v1 and v3 had
  // been promoted between rounds. The gauntlet still drew `v0 (SEED ·
  // CHAMPION) → v4 (LIVE)`, hiding the entire v0 → v1 → v3 spine and the
  // discarded v2 between them. With the lineage feed as the source of
  // truth, the spine must rebuild to its full length.
  state.applySnapshot(mockSnapshot());
  state.bracket = {
    epoch_id: '2026-05-20_presn',
    // The lagging payload: only the seed is on the index-DB spine.
    champion_lineage: ['v0'],
    matchups: [
      { champion: 'v0', challenger: 'v1', decision: 'promoted',
        delta_scalar: -0.10, ran_at: '2026-05-20T01:12:42Z' },
      { champion: 'v1', challenger: 'v2', decision: 'rejected',
        delta_scalar: 0.04,
        rejection_reason: 'pass-rate regression',
        ran_at: '2026-05-20T01:19:47Z' },
      { champion: 'v1', challenger: 'v3', decision: 'promoted',
        delta_scalar: -0.08, ran_at: '2026-05-20T02:00:19Z' },
    ],
  };
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null,
        epoch_id: '2026-05-20_presn', promoted: true,
        created_at: '2026-05-20T01:11:18Z' },
      { generation_id: 'v1', parent_generation_id: 'v0',
        epoch_id: '2026-05-20_presn', promoted: true,
        created_at: '2026-05-20T01:12:42Z' },
      { generation_id: 'v2', parent_generation_id: 'v1',
        epoch_id: '2026-05-20_presn', promoted: false,
        created_at: '2026-05-20T01:19:47Z' },
      { generation_id: 'v3', parent_generation_id: 'v1',
        epoch_id: '2026-05-20_presn', promoted: true,
        created_at: '2026-05-20T02:00:19Z' },
      { generation_id: 'v4', parent_generation_id: 'v3',
        epoch_id: '2026-05-20_presn', promoted: null,
        created_at: '2026-05-20T02:07:39Z' },
    ],
    experiments: [],
  };
  // The live tournament — v4 challenging v3.
  state.activeTournament = {
    tournament_id: 'tour-v3-vs-v4',
    parent_generation_id: 'v3',
    child_generation_id: 'v4',
    epoch_id: '2026-05-20_presn',
    started_at: '2026-05-20T02:07:39Z',
    round_index: 1,
    total_rounds: 3,
    phase: 'running',
    entries: [
      { entry_id: 'a', side: 'parent', status: 'done' },
      { entry_id: 'a', side: 'child', status: 'running' },
    ],
  };
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  // The full spine must rebuild — v0, v1 and v3 are all green spine
  // nodes. The bracket payload alone says only v0; the lineage feed
  // promotes the walk past the stale tail.
  const spineNodes = byClass(bracket, 'is-spine');
  const spineText = spineNodes.map((n) => n.textContent);
  assert(spineText.some((t) => t.includes('v0')),
    `v0 must be a spine node, got ${JSON.stringify(spineText)}`);
  assert(spineText.some((t) => t.includes('v1')),
    `v1 must be a spine node — bracket payload lagged, lineage feed promoted it. `
      + `got ${JSON.stringify(spineText)}`);
  assert(spineText.some((t) => t.includes('v3')),
    `v3 must be a spine node — the current champion. got ${JSON.stringify(spineText)}`);
  assertEqual(spineNodes.length, 3,
    `the spine has exactly three promoted nodes (v0, v1, v3), got ${spineNodes.length}`);

  // The CHAMPION badge moves to the LATEST promoted generation (v3), NOT
  // the seed v0. The seed keeps its "seed" label but `is-current` lives
  // only on v3. This is the regression: `seed · champion` was glued to
  // v0 even when the spine had multiple promotions.
  const current = byClass(bracket, 'is-current');
  assertEqual(current.length, 1,
    'exactly one spine node is the reigning champion');
  assert(current[0].textContent.includes('v3'),
    `the CHAMPION badge sits on v3, not on v0. got "${current[0].textContent}"`);
  const seeds = byClass(bracket, 'is-seed');
  assertEqual(seeds.length, 1, 'exactly one spine node carries the seed tag');
  assert(seeds[0].textContent.includes('v0'),
    `the SEED tag sits on v0. got "${seeds[0].textContent}"`);
  // The seed must NOT also be marked as the reigning champion when a
  // later generation has been promoted past it.
  const v0cls = seeds[0].getAttribute('class').split(/\s+/);
  assert(!v0cls.includes('is-current'),
    'v0 must NOT carry the reigning-champion class when v1/v3 have been promoted');
  // The seed tag reads as just "seed", not "seed · champion", once the
  // spine has at least one promotion past the seed.
  assert(!seeds[0].textContent.toLowerCase().includes('champion'),
    `v0 reads as the seed only — "seed · champion" must move with the badge. `
      + `got "${seeds[0].textContent}"`);

  // v2 is a red discarded card hanging off v1 (the champion it failed
  // to beat) — NOT off v0. The rejected challenger between two
  // promotions must not pollute the spine.
  const discarded = byClass(bracket, 'bracket-loser')
    .filter((n) => !n.getAttribute('class').split(/\s+/).includes('bracket-aborted'));
  const v2card = discarded.find((n) => n.textContent.includes('v2'));
  assert(v2card, `v2 must render as a discarded card. got ${discarded.length} discarded`);
  // Walk up to find the spine column v2 lives in — it must contain v1.
  let col = v2card;
  while (col && !(col.getAttribute && col.getAttribute('class')
    && col.getAttribute('class').split(/\s+/).includes('bracket-col'))) {
    col = col.parentNode;
  }
  assert(col, 'the v2 card must live inside a bracket-col');
  // The column's spine node names v1 (the champion v2 challenged).
  const colSpine = col._descendants()
    .filter((n) => {
      const c = n.getAttribute && n.getAttribute('class');
      return typeof c === 'string' && c.split(/\s+/).includes('is-spine');
    });
  assert(colSpine.length === 1 && colSpine[0].textContent.includes('v1'),
    `v2 hangs off v1, not v0. got column spine `
      + `${JSON.stringify(colSpine.map((n) => n.textContent))}`);

  // The live challenger v4 hangs off the CURRENT champion v3 — the
  // matchup-headline names v3 AND v4 is visually grouped with v3, not
  // with the seed v0.
  const liveCol = byClass(bracket, 'bracket-col-live')[0];
  assert(liveCol, 'the live challenger sits in a bracket-col-live column');
  const liveText = liveCol.textContent;
  assert(liveText.includes('v4'),
    `the live column names v4. got "${liveText}"`);
  assert(liveText.includes('v3'),
    `the live column references v3 — the champion v4 is challenging. got "${liveText}"`);
});

test('gauntlet: CHAMPION badge moves to the latest promoted spine node', () => {
  // Regression: previously the `seed · champion` label was stuck on the
  // seed v0 even when v1 / v2 had been promoted past it. This test pins
  // the contract: the CHAMPION badge follows the LATEST promoted node.
  state.applySnapshot(mockSnapshot());
  state.bracket = {
    epoch_id: 'e',
    champion_lineage: ['v0', 'v1', 'v2'],
    matchups: [
      { champion: 'v0', challenger: 'v1', decision: 'promoted',
        delta_scalar: -0.05, ran_at: '2026-05-10T10:00:00Z' },
      { champion: 'v1', challenger: 'v2', decision: 'promoted',
        delta_scalar: -0.04, ran_at: '2026-05-10T11:00:00Z' },
    ],
  };
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null, epoch_id: 'e',
        promoted: true, created_at: '2026-05-10T09:00:00Z' },
      { generation_id: 'v1', parent_generation_id: 'v0', epoch_id: 'e',
        promoted: true, created_at: '2026-05-10T10:00:00Z' },
      { generation_id: 'v2', parent_generation_id: 'v1', epoch_id: 'e',
        promoted: true, created_at: '2026-05-10T11:00:00Z' },
    ],
    experiments: [],
  };
  state.activeTournament = null;
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  const current = byClass(bracket, 'is-current');
  assertEqual(current.length, 1,
    'exactly one spine node is the reigning champion');
  assert(current[0].textContent.includes('v2'),
    `the CHAMPION badge sits on v2 (the latest promoted), got "${current[0].textContent}"`);
  // v0 keeps "seed" but loses "champion" — both `is-seed` and
  // `is-current` cannot live on the same node here.
  const seeds = byClass(bracket, 'is-seed');
  assertEqual(seeds.length, 1, 'one spine node is the seed');
  assert(seeds[0].textContent.includes('v0'),
    `the seed tag is on v0, got "${seeds[0].textContent}"`);
  const seedCls = seeds[0].getAttribute('class').split(/\s+/);
  assert(!seedCls.includes('is-current'),
    'v0 must not be the reigning champion when later spine nodes exist');
});

test('gauntlet: a sole-seed epoch keeps SEED · CHAMPION (no promotions yet)', () => {
  // When the spine has only the seed — no promotions yet — the seed is
  // both the seed and the reigning champion. This is the ONLY case where
  // `seed · champion` is correct (the very first tournament of an epoch).
  state.applySnapshot(mockSnapshot());
  state.bracket = {
    epoch_id: 'e',
    champion_lineage: ['v0'],
    matchups: [],
  };
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null, epoch_id: 'e',
        promoted: true, created_at: '2026-05-10T09:00:00Z' },
      { generation_id: 'v1', parent_generation_id: 'v0', epoch_id: 'e',
        promoted: null, created_at: '2026-05-10T10:00:00Z' },
    ],
    experiments: [],
  };
  state.activeTournament = {
    tournament_id: 'tour-v0-vs-v1',
    parent_generation_id: 'v0',
    child_generation_id: 'v1',
    epoch_id: 'e',
    started_at: '2026-05-10T10:00:00Z',
    round_index: 1,
    total_rounds: 3,
    phase: 'running',
    entries: [
      { entry_id: 'a', side: 'parent', status: 'done' },
      { entry_id: 'a', side: 'child', status: 'running' },
    ],
  };
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  const spineNodes = byClass(bracket, 'is-spine');
  assertEqual(spineNodes.length, 1,
    `single-seed spine has one node, got ${spineNodes.length}`);
  const seedCls = spineNodes[0].getAttribute('class').split(/\s+/);
  assert(seedCls.includes('is-seed') && seedCls.includes('is-current'),
    'the lone seed is both the seed and the reigning champion');
  // The label reads "seed · champion" — both roles fused on one node.
  assert(spineNodes[0].textContent.toLowerCase().includes('seed'),
    `the lone seed-champion reads as "seed", got "${spineNodes[0].textContent}"`);
  assert(spineNodes[0].textContent.toLowerCase().includes('champion'),
    `the lone seed-champion reads as "champion", got "${spineNodes[0].textContent}"`);

  // v1 hangs off v0 as the live challenger.
  const liveCol = byClass(bracket, 'bracket-col-live')[0];
  assert(liveCol, 'the live challenger is rendered in a bracket-col-live column');
  assert(liveCol.textContent.includes('v1'),
    `the live column names v1, got "${liveCol.textContent}"`);
});

test('gauntlet spine alignment: live connector lives in the previous col, '
  + 'never at the top of the live col (task #175)', () => {
  // Regression: prior layout put the dashed `┄▶` connector at the TOP
  // of the `bracket-col-live` while every promoted connector sat
  // BELOW its champion box in the previous col. With the live card
  // taller than a closed champ box, that asymmetry rendered the
  // vN→vLIVE arrow one node-height above the v0→v1 / v1→v2 arrows —
  // visibly breaking the spine line. The fix moves the dashed
  // connector into the reigning-champion col so every connector sits
  // at the same vertical row, and leaves the live col with just the
  // live card on its top row.
  state.applySnapshot(mockSnapshot());
  state.bracket = {
    epoch_id: 'e',
    champion_lineage: ['v0', 'v1', 'v3'],
    matchups: [
      { champion: 'v0', challenger: 'v1', decision: 'promoted',
        delta_scalar: -0.10, ran_at: '2026-05-20T01:00:00Z' },
      { champion: 'v1', challenger: 'v3', decision: 'promoted',
        delta_scalar: -0.08, ran_at: '2026-05-20T02:00:00Z' },
    ],
  };
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null, epoch_id: 'e',
        promoted: true, created_at: '2026-05-20T00:00:00Z' },
      { generation_id: 'v1', parent_generation_id: 'v0', epoch_id: 'e',
        promoted: true, created_at: '2026-05-20T01:00:00Z' },
      { generation_id: 'v3', parent_generation_id: 'v1', epoch_id: 'e',
        promoted: true, created_at: '2026-05-20T02:00:00Z' },
      { generation_id: 'v4', parent_generation_id: 'v3', epoch_id: 'e',
        promoted: null, created_at: '2026-05-20T03:00:00Z' },
    ],
    experiments: [],
  };
  state.activeTournament = {
    tournament_id: 'tour-v3-vs-v4',
    parent_generation_id: 'v3',
    child_generation_id: 'v4',
    epoch_id: 'e',
    started_at: '2026-05-20T03:00:00Z',
    round_index: 1,
    total_rounds: 3,
    phase: 'running',
    entries: [
      { entry_id: 'a', side: 'parent', status: 'done' },
      { entry_id: 'a', side: 'child', status: 'running' },
    ],
  };
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  // Live col exists and contains the live card.
  const liveCol = byClass(bracket, 'bracket-col-live')[0];
  assert(liveCol, 'the live challenger sits in a bracket-col-live column');
  assert(byClass(liveCol, 'bracket-live').length === 1,
    'the live col carries exactly one live card');

  // The live col MUST NOT carry a connector — that would push the live
  // card down one row and misalign the spine. Every connector lives in
  // the previous col instead.
  assert(byClass(liveCol, 'bracket-connector').length === 0,
    'the live col must not host any connector — connectors live in the '
      + 'previous col so the spine top row stays uniform');

  // The live card must be the FIRST child of the live col (top row),
  // not buried under a connector.
  const liveColFirst = liveCol.children[0];
  assert(liveColFirst
    && liveColFirst.getAttribute('class').split(/\s+/).includes('bracket-live'),
    `the live col's first child must be the live card, got class `
      + `"${liveColFirst && liveColFirst.getAttribute('class')}"`);

  // The dashed `live` connector lives in the reigning-champion col
  // (the lineage tail). Find it by walking from the live card up to
  // its sibling tree: the connector should sit in the col immediately
  // before the live col, alongside the v3 spine node.
  const cols = byClass(bracket, 'bracket-col')
    .filter((c) => !c.getAttribute('class').split(/\s+/).includes('bracket-col-live'));
  // Last non-live col is the lineage tail's col (it carries v3).
  const tailCol = cols[cols.length - 1];
  assert(tailCol && tailCol.textContent.includes('v3'),
    `the lineage-tail col must carry v3, got "${tailCol && tailCol.textContent}"`);
  const tailConns = byClass(tailCol, 'bracket-connector');
  const liveConns = tailConns.filter((c) =>
    c.getAttribute('class').split(/\s+/).includes('live'));
  assert(liveConns.length === 1,
    `the lineage-tail col must carry exactly one dashed live connector, `
      + `got ${liveConns.length}`);
});

test('gauntlet spine alignment: every spine top-row node shares the same '
  + 'min-height class so connector arrows line up (task #175)', () => {
  // The closed champion box and the live card render at different
  // natural heights — `.bracket-champ` packs only an id + tag (~46px)
  // while `.bracket-live` adds dots, progress and verdict (~75px+).
  // With `align-items: flex-start` on the spine, that mismatch tilted
  // the connector arrows. The fix gives every spine-top node the same
  // `min-height` so the centerlines align. The harness can't measure
  // CSS but it CAN pin the class contract: every node on the spine
  // top row carries either `.bracket-champ` or `.bracket-live`, and
  // the CSS rules on those selectors share a `min-height`.
  state.applySnapshot(mockSnapshot());
  state.bracket = {
    epoch_id: 'e',
    champion_lineage: ['v0', 'v1'],
    matchups: [
      { champion: 'v0', challenger: 'v1', decision: 'promoted',
        delta_scalar: -0.10, ran_at: '2026-05-20T01:00:00Z' },
    ],
  };
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null, epoch_id: 'e',
        promoted: true, created_at: '2026-05-20T00:00:00Z' },
      { generation_id: 'v1', parent_generation_id: 'v0', epoch_id: 'e',
        promoted: true, created_at: '2026-05-20T01:00:00Z' },
      { generation_id: 'v2', parent_generation_id: 'v1', epoch_id: 'e',
        promoted: null, created_at: '2026-05-20T02:00:00Z' },
    ],
    experiments: [],
  };
  state.activeTournament = {
    tournament_id: 'tour-v1-vs-v2',
    parent_generation_id: 'v1',
    child_generation_id: 'v2',
    epoch_id: 'e',
    started_at: '2026-05-20T02:00:00Z',
    round_index: 1,
    total_rounds: 3,
    phase: 'running',
    entries: [
      { entry_id: 'a', side: 'parent', status: 'done' },
      { entry_id: 'a', side: 'child', status: 'running' },
    ],
  };
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  // Every spine col's FIRST child is a spine-top node, never a
  // connector. That is the structural invariant that keeps the top
  // row aligned across cols.
  const cols = byClass(bracket, 'bracket-col');
  assert(cols.length >= 3,
    `expected at least 3 spine cols (v0, v1, live), got ${cols.length}`);
  for (const col of cols) {
    const first = col.children[0];
    assert(first, `every spine col has a first child, got empty col`);
    const cls = (first.getAttribute('class') || '').split(/\s+/);
    assert(cls.includes('bracket-champ') || cls.includes('bracket-live'),
      `every spine col's first child is a champ or live card so the top `
        + `row stays homogenous; got class "${first.getAttribute('class')}"`);
  }
});

test('gauntlet spine alignment: a zero-lineage epoch keeps the live col '
  + 'aligned by splitting the synthetic champ into its own col (task #175)', () => {
  // Edge case: a fresh epoch (no resolved lineage yet) used to pack
  // BOTH the synthetic seed-champion box AND the live card into the
  // live col, with the connector wedged between them. That broke the
  // top-row contract (the synthetic champ and the live card occupied
  // different rows of the same col). The fix splits them: the
  // synthetic champ + connector live in their own `bracket-col`, the
  // live card lives alone in `bracket-col-live`.
  state.applySnapshot(mockSnapshot());
  state.bracket = {
    epoch_id: 'e',
    champion_lineage: [],
    matchups: [],
  };
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null, epoch_id: 'e',
        promoted: null, created_at: '2026-05-10T09:00:00Z' },
      { generation_id: 'v1', parent_generation_id: 'v0', epoch_id: 'e',
        promoted: null, created_at: '2026-05-10T10:00:00Z' },
    ],
    experiments: [],
  };
  state.activeTournament = {
    tournament_id: 'tour-v0-vs-v1',
    parent_generation_id: 'v0',
    child_generation_id: 'v1',
    epoch_id: 'e',
    started_at: '2026-05-10T10:00:00Z',
    round_index: 1,
    total_rounds: 3,
    phase: 'running',
    entries: [
      { entry_id: 'a', side: 'parent', status: 'done' },
      { entry_id: 'a', side: 'child', status: 'running' },
    ],
  };
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  // The synthetic seed-champion lives in its OWN bracket-col, not in
  // the live col.
  const liveCol = byClass(bracket, 'bracket-col-live')[0];
  assert(liveCol, 'the live challenger sits in a bracket-col-live column');
  assert(byClass(liveCol, 'bracket-champ').length === 0,
    'the live col must not host the synthetic seed-champion — that lives '
      + 'in its own col so the top-row stays homogenous');
  assert(byClass(liveCol, 'bracket-connector').length === 0,
    'the live col must not host the dashed connector either');

  // Exactly one is-spine node (the synthetic seed-champion v0) sits
  // outside the live col.
  const spineNodes = byClass(bracket, 'is-spine');
  assertEqual(spineNodes.length, 1,
    `zero-lineage epoch synthesizes exactly one spine node, got ${spineNodes.length}`);
  // Walk up: the spine node lives in a regular bracket-col (NOT
  // bracket-col-live).
  let owner = spineNodes[0];
  while (owner && !(owner.getAttribute && owner.getAttribute('class')
    && owner.getAttribute('class').split(/\s+/).includes('bracket-col'))) {
    owner = owner.parentNode;
  }
  assert(owner, 'the synthetic seed-champion lives in a bracket-col');
  const ownerCls = owner.getAttribute('class').split(/\s+/);
  assert(!ownerCls.includes('bracket-col-live'),
    'the synthetic seed-champion must NOT live in the live col');

  // The synthetic-champ col carries the dashed live connector pointing
  // into the live col.
  const ownerConns = byClass(owner, 'bracket-connector')
    .filter((c) => c.getAttribute('class').split(/\s+/).includes('live'));
  assertEqual(ownerConns.length, 1,
    `the synthetic-champ col carries the dashed live connector, got ${ownerConns.length}`);
});

test('gauntlet: a rejected challenger between two promotions stays off the spine', () => {
  // Regression: when v2 is rejected between two promoted generations
  // (v1 and v3), it must hang off v1 (the champion it failed to beat),
  // not pollute the spine. This is the live `2026-05-20_presn` shape in
  // miniature, without the live-tournament noise.
  state.applySnapshot(mockSnapshot());
  state.bracket = {
    epoch_id: 'e',
    champion_lineage: ['v0', 'v1', 'v3'],
    matchups: [
      { champion: 'v0', challenger: 'v1', decision: 'promoted',
        delta_scalar: -0.10, ran_at: '2026-05-20T01:00:00Z' },
      { champion: 'v1', challenger: 'v2', decision: 'rejected',
        delta_scalar: 0.04, ran_at: '2026-05-20T02:00:00Z' },
      { champion: 'v1', challenger: 'v3', decision: 'promoted',
        delta_scalar: -0.08, ran_at: '2026-05-20T03:00:00Z' },
    ],
  };
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null, epoch_id: 'e',
        promoted: true, created_at: '2026-05-20T00:00:00Z' },
      { generation_id: 'v1', parent_generation_id: 'v0', epoch_id: 'e',
        promoted: true, created_at: '2026-05-20T01:00:00Z' },
      { generation_id: 'v2', parent_generation_id: 'v1', epoch_id: 'e',
        promoted: false, created_at: '2026-05-20T02:00:00Z' },
      { generation_id: 'v3', parent_generation_id: 'v1', epoch_id: 'e',
        promoted: true, created_at: '2026-05-20T03:00:00Z' },
    ],
    experiments: [],
  };
  state.activeTournament = null;
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  // Spine has v0, v1, v3 — not v2.
  const spineNodes = byClass(bracket, 'is-spine');
  assertEqual(spineNodes.length, 3,
    `the spine has v0, v1 and v3 — three nodes, got ${spineNodes.length}`);
  for (const n of spineNodes) {
    assert(!n.textContent.includes('v2'),
      `v2 must NOT be a spine node — it was rejected. got "${n.textContent}"`);
  }
  // v2 is a red discarded card.
  const discarded = byClass(bracket, 'bracket-loser')
    .filter((n) => !n.getAttribute('class').split(/\s+/).includes('bracket-aborted'));
  const v2card = discarded.find((n) => n.textContent.includes('v2'));
  assert(v2card, `v2 must render as a discarded card. got ${discarded.length} discarded`);
});

test('gauntlet: a live fast-mode challenger reads as live, NOT incomplete', () => {
  // Regression: a fast-mode tournament round did not publish an
  // ActiveTournament; the gauntlet then synthesized a torn-down node
  // for the actively-running challenger (a non-baseline,
  // not-yet-promoted generation whose parent is the champion). The
  // runner now publishes the record, so the live challenger surfaces
  // at the head of the bracket — NOT as an amber incomplete node.
  state.applySnapshot(mockSnapshot());
  state.bracket = {
    epoch_id: '2026-05-19_presn',
    champion_lineage: ['v0'],
    matchups: [],
  };
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null, promoted: true },
      // v2 ran (or is running) but has no matchup row yet — the only
      // distinguishing signal that it is LIVE, not torn down, is the
      // active-tournament record.
      { generation_id: 'v2', parent_generation_id: 'v0', promoted: null },
    ],
    experiments: [],
  };
  // The fast-mode active-tournament shape the runner now publishes:
  // both sides present, champion entries cached, child entries live.
  state.activeTournament = {
    tournament_id: 'tour-v0-vs-v2',
    parent_generation_id: 'v0',
    child_generation_id: 'v2',
    epoch_id: '2026-05-19_presn',
    started_at: '2026-05-19T05:00:00Z',
    round_index: 1,
    total_rounds: 3,
    phase: 'running',
    entries: [
      { entry_id: 'a', side: 'parent', status: 'done',
        status_raw: 'cached', loss_summary: { drift_loss: 2.2, pass_fail: 1.0 } },
      { entry_id: 'b', side: 'parent', status: 'done',
        status_raw: 'cached', loss_summary: { drift_loss: 1.8, pass_fail: 0.0 } },
      { entry_id: 'a', side: 'child', status: 'running', status_raw: 'running' },
      { entry_id: 'b', side: 'child', status: 'queued', status_raw: 'queued' },
    ],
    partial_champion_agg: { drift_loss_mean: 2.0, pass_rate: 0.5,
      scalar: 2.0, entry_count: 2 },
    partial_challenger_agg: {},
  };
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  // The hall renders the per-board cards — the dashboard is no longer
  // blank for the fast round.
  const cards = byClass(bracket, 'board-card');
  assertEqual(cards.length, 2,
    `the hall must render one card per board entry, got ${cards.length}`);

  // The live challenger is NOT also synthesised as an aborted node:
  // bracketModel's liveChild guard skips v2 when it is the active
  // tournament's challenger.
  const aborted = byClass(bracket, 'bracket-aborted');
  for (const node of aborted) {
    assert(!node.textContent.includes('v2'),
      `v2 must NOT render as an incomplete/aborted node — it is the live `
        + `challenger. Got "${node.textContent}"`);
  }

  // The champion side reads as "cached" (the raw status), not bare
  // "done", so the operator can tell the score is from the cache.
  const cachedPills = byClass(bracket, 'pill-cached');
  assertEqual(cachedPills.length, 2,
    `every champion side must carry a cached pill, got ${cachedPills.length}`);
  // The cached side surfaces the cached scalar in its score row.
  const sideScores = byClass(bracket, 'board-side-score');
  const cachedScores = sideScores.filter(
    (n) => n.textContent.toLowerCase().includes('cached'),
  );
  assert(cachedScores.length >= 1,
    `at least one cached side must surface "cached" copy, got ${cachedScores.length}`);
});

test('gauntlet: discarded card truncates a long rejection reason — full text on title', () => {
  // Bug #168: the rejection-reason annotation on a discarded card had no
  // max-width and no text truncation, so a multi-line gate verdict
  // (e.g. the t6 round-1 prose "challenger regressed: loss rose by
  // 10.122619 ...") stretched its column to ~950 px and pushed every
  // node downstream off the right edge of the viewport. The reason now
  // renders truncated (CSS ellipsis on a fixed-width card) with the
  // FULL text retained on the `title` attribute so the operator can
  // hover-inspect it without losing layout.
  state.applySnapshot(mockSnapshot());
  const longReason = 'challenger regressed: loss rose by 10.122619 '
    + '(champion 47.580429 -> challenger 57.703048); a promotion needs '
    + 'the loss to drop by at least 0.010000';
  state.bracket = {
    epoch_id: 'e-long',
    champion_lineage: ['v0', 'v1', 'v3'],
    matchups: [
      { champion: 'v0', challenger: 'v1', decision: 'promoted',
        delta_scalar: -0.2 },
      { champion: 'v1', challenger: 'v2', decision: 'rejected',
        delta_scalar: 10.12, rejection_reason: longReason },
      { champion: 'v1', challenger: 'v3', decision: 'promoted',
        delta_scalar: -0.3 },
    ],
  };
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null, promoted: true },
      { generation_id: 'v1', parent_generation_id: 'v0', promoted: true },
      { generation_id: 'v2', parent_generation_id: 'v1', promoted: false },
      { generation_id: 'v3', parent_generation_id: 'v1', promoted: true },
    ],
    experiments: [],
  };
  state.activeTournament = null;
  render.showView('tournament');
  const bracket = doc.getElementById('tournament-bracket');

  const reasons = byClass(bracket, 'bracket-loser-reason');
  assert(reasons.length >= 1, 'at least one discarded card must render a reason line');
  // Every truncated reason carries the full message on `title` so the
  // operator can hover-read it. A non-empty `title` is the contract.
  for (const r of reasons) {
    const title = r.getAttribute('title');
    assert(title != null && title.length > 0,
      `discarded card reason must carry a non-empty title attribute, got "${title}"`);
  }
  // The long-reason card carries the full prose on its title — proves
  // the title is the full untruncated text, not a re-truncation.
  const longTitled = reasons.filter(
    (r) => (r.getAttribute('title') || '').includes('10.122619'),
  );
  assertEqual(longTitled.length, 1,
    `the long-reason card must carry the full untruncated reason on title, `
      + `got ${longTitled.length} matching cards`);
});

test('gauntlet: t6 round-1 shape — live node scrolls into view on first render', () => {
  // Bug #168: with 4 spine nodes + 3 discarded children, the live
  // challenger (v6) rendered entirely off-screen at the right of a
  // wide gauntlet container. The fix is two-fold: cap the discarded
  // card width (covered by the title/truncation test above) AND
  // auto-scroll the container so the live node lands in view on
  // first render. This test mirrors the t6 round-1 shape.
  state.applySnapshot(mockSnapshot());
  state.bracket = {
    epoch_id: 't6-round1',
    champion_lineage: ['v0', 'v1', 'v3'],
    matchups: [
      { champion: 'v0', challenger: 'v1', decision: 'promoted',
        delta_scalar: -0.2 },
      { champion: 'v1', challenger: 'v2', decision: 'rejected',
        delta_scalar: 10.12, rejection_reason: 'long reason A' },
      { champion: 'v1', challenger: 'v3', decision: 'promoted',
        delta_scalar: -0.3 },
      { champion: 'v3', challenger: 'v4', decision: 'rejected',
        delta_scalar: 5.0, rejection_reason: 'long reason B' },
      { champion: 'v3', challenger: 'v5', decision: 'rejected',
        delta_scalar: 7.0, rejection_reason: 'long reason C' },
    ],
  };
  state.lineage = {
    generations: [
      { generation_id: 'v0', parent_generation_id: null, promoted: true },
      { generation_id: 'v1', parent_generation_id: 'v0', promoted: true },
      { generation_id: 'v2', parent_generation_id: 'v1', promoted: false },
      { generation_id: 'v3', parent_generation_id: 'v1', promoted: true },
      { generation_id: 'v4', parent_generation_id: 'v3', promoted: false },
      { generation_id: 'v5', parent_generation_id: 'v3', promoted: false },
      { generation_id: 'v6', parent_generation_id: 'v3', promoted: null },
    ],
    experiments: [],
  };
  state.activeTournament = {
    tournament_id: 'tour-v3-vs-v6',
    parent_generation_id: 'v3',
    child_generation_id: 'v6',
    epoch_id: 't6-round1',
    started_at: '2026-05-19T05:00:00Z',
    round_index: 1, total_rounds: 3, phase: 'running',
    entries: [
      { entry_id: 'a', side: 'parent', status: 'done' },
      { entry_id: 'a', side: 'child', status: 'running' },
    ],
  };

  // Shim the layout properties the auto-scroll path reads. The minimal
  // harness has no real layout engine, so we attach a fake scrollWidth /
  // clientWidth pair representing a spine wider than the viewport.
  const wrap = doc.getElementById('tournament-bracket');
  wrap.scrollWidth = 1500;
  wrap.clientWidth = 800;
  wrap.scrollLeft = 0;

  // Pass 1 — first render. The live id (v6) is new, so the auto-scroll
  // path executes. No descendant carries `scrollIntoView` (the harness
  // doesn't implement it), so the renderer falls back to setting
  // wrap.scrollLeft = scrollWidth - clientWidth — the live node lands
  // flush with the right edge of the container.
  render.showView('tournament');
  assertEqual(wrap.scrollLeft, 1500 - 800,
    `on first render the live node must be in view — gauntlet scrollLeft `
      + `should land at scrollWidth - clientWidth = 700, got ${wrap.scrollLeft}`);

  // The DOM is correct — every spine node + the live card exists. (The
  // bug was layout only.)
  const spineNodes = byClass(wrap, 'is-spine');
  assertEqual(spineNodes.length, 3,
    `the spine must carry v0, v1, v3 — got ${spineNodes.length} nodes`);
  const liveCards = byClass(wrap, 'bracket-live');
  assertEqual(liveCards.length, 1,
    `exactly one live card (v6) must render, got ${liveCards.length}`);
  assert(liveCards[0].textContent.includes('v6'),
    `the live card names v6, got "${liveCards[0].textContent}"`);

  // Pass 2 — an SSE tick repaints with the SAME live id. The operator
  // has scrolled left to inspect history; that scrollLeft must survive
  // the re-render (a digest-changed but live-id-unchanged tick).
  wrap.scrollLeft = 120;
  render.renderBracket();
  assertEqual(wrap.scrollLeft, 120,
    `a re-render with the same live id must preserve the operator's `
      + `scrollLeft, got ${wrap.scrollLeft}`);

  // Pass 3 — a NEW live challenger replaces v6 with v7. The auto-scroll
  // path executes again, snapping the gauntlet to the right edge so the
  // new live node is in view.
  state.activeTournament = {
    ...state.activeTournament,
    tournament_id: 'tour-v3-vs-v7',
    child_generation_id: 'v7',
  };
  state.lineage = {
    ...state.lineage,
    generations: [
      ...state.lineage.generations,
      { generation_id: 'v7', parent_generation_id: 'v3', promoted: null },
    ],
  };
  wrap.scrollLeft = 0;
  render.renderBracket();
  assertEqual(wrap.scrollLeft, 1500 - 800,
    `a new live id must scroll the container so the live node is in view, `
      + `got scrollLeft=${wrap.scrollLeft}`);
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

test('the Overview epochs table shows each epoch goal', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('overview');
  const panel = doc.getElementById('epochs-panel');
  // The header row carries a `goal` column.
  const headers = panel._descendants()
    .filter((n) => n.localName === 'th')
    .map((n) => n.textContent);
  assert(headers.includes('goal'),
    `the epochs table must carry a goal column, got ${JSON.stringify(headers)}`);
  // Each epoch's distilled goal text renders in its row.
  const text = textOf(panel);
  assert(text.includes('Stabilise the extraction schema'),
    'the e0 epoch row shows its distilled goal');
  assert(text.includes('Cut off-topic drift'),
    'the e1 epoch row shows its distilled goal');
});

test('Recent experiments call an unfinished experiment incomplete, not in progress', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('overview');
  const wrap = doc.getElementById('recent-experiments');
  const text = textOf(wrap);
  // The mock epoch's v5 has no outcome — it must read as `incomplete`.
  assert(text.includes('incomplete'),
    'an unfinished experiment reads as incomplete on the Overview');
  assert(!text.toLowerCase().includes('in progress'),
    'an unfinished experiment never reads as "in progress" on the Overview');
  // The "Full experiment log" link targets the Epoch view's
  // Experiments section directly via the `experiments` section anchor
  // — applyRoute scrolls the section into view AFTER the Epoch render
  // lands. Click-handler-only scrolling was the prior approach; it was
  // fragile on direct URL access / reload / bookmark, so the anchor is
  // baked into the URL instead.
  const link = wrap._descendants().find(
    (n) => n.getAttribute && n.getAttribute('href') === '#/epoch/experiments');
  assert(link, 'the recent-experiments digest links through to the Experiments section');
});

test('router resolves the Epoch view experiments section anchor', async () => {
  // #/epoch/experiments  and  #/epoch/{id}/experiments  are recognised
  // by the router as a section anchor on the Epoch view; applyRoute is
  // expected to scroll the named section into view. The router's parse
  // step is what we verify here — the scroll itself is a side effect
  // the harness has no DOM-layout backing for.
  const { router } = await import('../js/core/router.js');

  globalThis.location.hash = '#/epoch/experiments';
  const a = router.resolve();
  assertEqual(a.view, 'epoch',
    `#/epoch/experiments resolves to the Epoch view, got ${a.view}`);
  assertEqual(a.params.section, 'experiments',
    `the section anchor is 'experiments', got ${a.params.section}`);
  assert(a.params.epochId == null,
    'the bare section anchor has no epoch id');

  globalThis.location.hash = '#/epoch/2026-05-19_presn/experiments';
  const b = router.resolve();
  assertEqual(b.view, 'epoch',
    '#/epoch/{id}/experiments resolves to the Epoch view');
  assertEqual(b.params.epochId, '2026-05-19_presn',
    `the epoch id is parsed out, got ${b.params.epochId}`);
  assertEqual(b.params.section, 'experiments',
    `the section anchor still resolves alongside the epoch id, got ${b.params.section}`);

  // A normal #/epoch route carries no section anchor.
  globalThis.location.hash = '#/epoch';
  const c = router.resolve();
  assertEqual(c.view, 'epoch', 'bare #/epoch is still the Epoch view');
  assert(c.params.section == null,
    'a bare #/epoch route carries no section anchor');
});

test('applyRoute switches to the Epoch view when the experiments anchor is in the URL', () => {
  // The route-extension approach: applyRoute recognises
  // `#/epoch/experiments` and `#/epoch/{id}/experiments`, switches in
  // the Epoch view, and (in the browser) scrolls the section into view
  // via a double-rAF. The harness has no rAF / layout, so we verify
  // the view-switch side of the contract — proof that the route is
  // wired through end-to-end, not only handled at click time.
  state.applySnapshot(mockSnapshot());
  globalThis.location.hash = '#/epoch/experiments';
  render.applyRoute();
  // The Epoch view is the active view.
  const epochView = doc.getElementById('view-epoch');
  assert(!epochView.classList.contains('hidden'),
    'applyRoute(#/epoch/experiments) must show the Epoch view');
  // And the experiments section exists in the DOM (so the scroll
  // target is reachable — the bug was a click-handler approach
  // running before the Epoch view's render had painted the section).
  const section = doc.getElementById('epoch-experiments-section');
  assert(section != null,
    'the experiments section must be present in the DOM after applyRoute');
});

test('a #/epoch/experiments route does not yank the scroll on every render', async () => {
  // BUG: applyRoute() called scrollEpochSectionIntoView unconditionally
  // on the experiments anchor, and applyRoute re-runs on EVERY render
  // (each SSE delta). Re-applying the SAME anchor must be idempotent —
  // scrollIntoView fires only on route TRANSITION. Mirrors the
  // openMatchup gating fix on state.selectedMatchup.
  state.applySnapshot(mockSnapshot());

  let scrolls = 0;
  const section = doc.getElementById('epoch-experiments-section');
  section.scrollIntoView = () => { scrolls += 1; };

  // First entry into #/epoch/experiments — must scroll exactly once.
  // The handler defers the scroll via setTimeout(0) when rAF is absent
  // (the test harness has no rAF), so flush the timer queue before
  // counting.
  globalThis.location.hash = '#/epoch/experiments';
  render.applyRoute();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assertEqual(scrolls, 1, 'opening the experiments anchor scrolls it into view once');

  // Subsequent renders (SSE deltas) re-run applyRoute for the SAME
  // route — must NOT re-scroll. Three more applyRoute calls and a
  // full renderAll should not bump the count.
  render.applyRoute();
  render.applyRoute();
  render.applyRoute();
  render.renderAll();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assertEqual(scrolls, 1,
    're-rendering the same #/epoch/experiments route must NOT re-scroll');

  // Leaving the anchor (e.g. #/epoch) and returning is a fresh
  // transition — the latch resets and the next entry scrolls again.
  globalThis.location.hash = '#/epoch';
  render.applyRoute();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assertEqual(scrolls, 1, 'navigating away from the anchor must not scroll');

  globalThis.location.hash = '#/epoch/experiments';
  render.applyRoute();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assertEqual(scrolls, 2,
    'returning to the experiments anchor counts as a fresh transition and scrolls again');
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

test('conversation view champion column renders cached transcript turns, not the "waiting" placeholder', () => {
  // BUG (live-verified, run #6 round 1): a fast-mode round's CACHED
  // champion side has events persisted on disk under its OWN generation
  // directory (the round it ran live as a challenger), but the
  // matchup-conversations fetcher was looking under the current round's
  // champion-of-this-round directory and finding an empty / missing
  // events.jsonl — so the conversation view painted the champion column
  // as "Waiting for the first turn… — run in progress — more turns will
  // stream in" for a generation that finished hours earlier.
  //
  // This test exercises the JS render path with a payload that mimics
  // the FIXED server response: champion side carries a populated
  // (non-empty) transcript. The render must show the turn content and
  // MUST NOT print the in-progress "Waiting for the first turn…"
  // placeholder for the cached side.
  state.mock = true;
  state.applySnapshot(mockSnapshot());

  // Drill into a board entry — selectConversation seeds convData from
  // mockConversation, which gives the champion side a non-empty
  // transcript (the post-fix server payload for a cached side that has
  // its persisted events on disk).
  globalThis.location.hash = '#/conversation/extract_invoice_001';
  render.applyRoute();

  const panel = doc.getElementById('conversation-panel');
  const text = textOf(panel);
  assert(text.includes('Conversation diff'),
    'the conversation view must render its header');
  // The champion column must show actual transcript content — the mock
  // champion transcript carries a turn with the goal text.
  assert(text.includes('Extract the invoice total'),
    'the champion column must render the cached transcript turns');
  // Critically: the in-progress placeholder must NOT appear for a side
  // whose transcript carries turns. ("Waiting for the first turn…" is
  // the exact failure mode the live bug showed for the cached side.)
  const champCol = panel.querySelector
    ? panel.querySelector('.conversation-column.champion')
    : null;
  // Fall back to the panel text scan when querySelector is unavailable
  // in the harness — the assertion is the same either way.
  const champText = champCol ? textOf(champCol) : text;
  assert(!champText.includes('Waiting for the first turn'),
    'the champion column must not render the in-progress placeholder ' +
    'when the API returns a non-empty cached transcript');
});

// -- Zero-turn complete-run "what actually happened" panel ----------
// Regression for the dashboard reading "Waiting for the first turn… ●
// run in progress — more turns will stream in" for a v1 run that
// actually finished by hitting its wall-clock budget. Once the runner
// emits a `run_aborted` terminal frame and the API surfaces the
// sibling loss.json on the side's `result` block, the column must
// render an honest "timed out" panel.

test('renderTranscriptColumn shows the timed-out panel for a complete zero-turn run with a result block', () => {
  const side = {
    run_id: 'r-timed-out',
    generation_id: 'v1',
    transcript: {
      run_id: 'r-timed-out',
      event_count: 2,
      complete: true,
      turns: [],
      annotations: [],
    },
    result: {
      wall_clock_budget_exceeded: true,
      runtime_ms: 180000,
      pass_fail: false,
      expectation_result: {
        kind: 'predicate',
        passed: false,
        detail: 'predicate returned False',
      },
      metric_counts: [
        { name: 'cost:llm_calls', count: 8.0, severity: '' },
        { name: 'output:chars', count: 7349.0, severity: '' },
      ],
    },
  };

  const col = render.renderTranscriptColumn('challenger', side);
  const text = textOf(col);

  // The honest "timed out" panel renders — not the bland "no transcript
  // turns" fallback and not the misleading "in progress" cue.
  assert(text.includes('Run terminated without structured turn events'),
    'the zero-turn complete-run panel headline must render, got: ' + text);
  assert(text.includes('timed out'),
    'the panel must say the run timed out');
  assert(text.includes('180'),
    'the panel must surface the runtime in seconds');
  assert(text.includes('8') && text.includes('LLM calls'),
    'the panel must surface the LLM-call count');
  assert(text.includes('7,349') || text.includes('7349'),
    'the panel must surface the output char count');
  assert(text.includes('predicate') && text.includes('failed'),
    'the panel must surface the expectation verdict');
  assert(!text.includes('This run produced no transcript turns'),
    'the bland fallback must NOT render when a result is present');
  assert(!text.includes('Waiting for the first turn'),
    'the in-progress placeholder must NOT render for a complete run');
  assert(!text.includes('run in progress'),
    'the misleading "in progress" cue must NOT render for a complete run');
});

test('renderTranscriptColumn emits a run-boundary separator between groups of a multi_turn_emulated transcript', () => {
  // Regression for bug #172. A ``multi_turn_emulated`` board entry
  // spawns N goldfive runs (one per emulated user turn). Each run owns
  // its own ``conversation_started`` lifecycle frame. The pre-fix
  // renderer painted all N "conversation started" turns stacked at the
  // top followed by interleaved per-run bodies. The reconstructor now
  // groups events by ``run_id`` and stamps each turn with a 1-based
  // ``run_index``; the renderer paints a visible
  // ``conversation-run-separator`` between groups.
  const side = {
    run_id: 'run_a',
    generation_id: 'v3',
    transcript: {
      run_id: 'run_a',
      event_count: 12,
      complete: true,
      turns: [
        // Run 1 — 3 turns.
        { seq: null, ts: 'T0.0', agent: '', role: 'system',
          kind: 'conversation_started', text: 'conversation started',
          run_id: 'run_a', run_index: 1 },
        { seq: 0, ts: 'T0.1', agent: '', role: 'user',
          kind: 'run_started', text: 'prompt A', run_id: 'run_a',
          run_index: 1 },
        { seq: 1, ts: 'T0.5', agent: 'alpha', role: 'agent',
          kind: 'goldfive_llm_call_end', text: 'reply A',
          run_id: 'run_a', run_index: 1 },
        // Run 2 — 3 turns.
        { seq: null, ts: 'T10.0', agent: '', role: 'system',
          kind: 'conversation_started', text: 'conversation started',
          run_id: 'run_b', run_index: 2 },
        { seq: 0, ts: 'T10.1', agent: '', role: 'user',
          kind: 'run_started', text: 'prompt B', run_id: 'run_b',
          run_index: 2 },
        { seq: 1, ts: 'T10.5', agent: 'alpha', role: 'agent',
          kind: 'goldfive_llm_call_end', text: 'reply B',
          run_id: 'run_b', run_index: 2 },
        // Run 3 — 3 turns.
        { seq: null, ts: 'T20.0', agent: '', role: 'system',
          kind: 'conversation_started', text: 'conversation started',
          run_id: 'run_c', run_index: 3 },
        { seq: 0, ts: 'T20.1', agent: '', role: 'user',
          kind: 'run_started', text: 'prompt C', run_id: 'run_c',
          run_index: 3 },
        { seq: 1, ts: 'T20.5', agent: 'alpha', role: 'agent',
          kind: 'goldfive_llm_call_end', text: 'reply C',
          run_id: 'run_c', run_index: 3 },
      ],
      annotations: [],
    },
  };

  const col = render.renderTranscriptColumn('champion', side);

  // Two separators appear (between runs 1↔2 and 2↔3) — NOT three
  // "conversation started" lines stacked at the top.
  const separators = col._descendants().filter(
    (n) => n.classList && n.classList.contains('conversation-run-separator'));
  assert(separators.length === 2,
    `expected 2 run-boundary separators (one between each pair of runs), ` +
    `got ${separators.length}`);

  // Each separator's text references the run index it opens.
  const sepTexts = separators.map((n) => n.textContent);
  assert(sepTexts[0].includes('Turn 2'),
    `first separator must reference Turn 2, got: ${sepTexts[0]}`);
  assert(sepTexts[1].includes('Turn 3'),
    `second separator must reference Turn 3, got: ${sepTexts[1]}`);
  // And the run_id badge appears alongside each label.
  assert(sepTexts[0].includes('run_b'),
    `first separator must surface the run_b id, got: ${sepTexts[0]}`);
  assert(sepTexts[1].includes('run_c'),
    `second separator must surface the run_c id, got: ${sepTexts[1]}`);

  // All three "conversation started" turns render — NOT stacked at
  // the top but each one inside its own group, in the document order:
  // [cs1, prompt A, reply A, SEP, cs2, prompt B, reply B, SEP, cs3, ...].
  const turnNodes = col._descendants().filter(
    (n) => n.classList && n.classList.contains('conversation-turn'));
  assert(turnNodes.length === 9,
    `expected 9 rendered turns, got ${turnNodes.length}`);
  const turnTexts = turnNodes.map((n) => n.textContent);
  // The three "conversation started" frames sit at positions 0, 3, 6
  // (one per group), not 0, 1, 2 (the broken pre-fix output).
  const csIndices = turnTexts
    .map((t, i) => (t.includes('conversation started') ? i : -1))
    .filter((i) => i >= 0);
  assert(JSON.stringify(csIndices) === JSON.stringify([0, 3, 6]),
    `conversation_started turns must be distributed across run groups ` +
    `(positions [0, 3, 6]), not stacked at the top — got ${JSON.stringify(csIndices)}`);

  // The user prompts of the three runs appear contiguously inside
  // their OWN groups, not interleaved.
  assert(turnTexts[1].includes('prompt A'),
    `position 1 must be run 1's user prompt, got: ${turnTexts[1]}`);
  assert(turnTexts[2].includes('reply A'),
    `position 2 must be run 1's agent reply, got: ${turnTexts[2]}`);
  assert(turnTexts[4].includes('prompt B'),
    `position 4 must be run 2's user prompt, got: ${turnTexts[4]}`);
  assert(turnTexts[5].includes('reply B'),
    `position 5 must be run 2's agent reply, got: ${turnTexts[5]}`);
});

test('renderTranscriptColumn emits NO run-boundary separator for a single-run transcript', () => {
  // Single-run transcripts (the common case) must NOT paint the
  // separator — the bug-fix only adds a boundary where one exists.
  const side = {
    run_id: 'r-solo',
    generation_id: 'v0',
    transcript: {
      run_id: 'r-solo',
      event_count: 3,
      complete: true,
      turns: [
        { seq: null, ts: 'T0', agent: '', role: 'system',
          kind: 'conversation_started', text: 'conversation started',
          run_id: 'r-solo', run_index: 1 },
        { seq: 0, ts: 'T1', agent: '', role: 'user',
          kind: 'run_started', text: 'solo prompt', run_id: 'r-solo',
          run_index: 1 },
        { seq: 1, ts: 'T2', agent: 'alpha', role: 'agent',
          kind: 'goldfive_llm_call_end', text: 'solo reply',
          run_id: 'r-solo', run_index: 1 },
      ],
      annotations: [],
    },
  };

  const col = render.renderTranscriptColumn('champion', side);
  const separators = col._descendants().filter(
    (n) => n.classList && n.classList.contains('conversation-run-separator'));
  assert(separators.length === 0,
    `single-run transcript must paint zero separators, got ${separators.length}`);
});

test('renderTranscriptColumn falls back to the bland message for a complete zero-turn run with NO result block', () => {
  const side = {
    run_id: 'r-zero',
    generation_id: 'v1',
    transcript: {
      run_id: 'r-zero',
      event_count: 0,
      complete: true,
      turns: [],
      annotations: [],
    },
    result: null,
  };

  const col = render.renderTranscriptColumn('challenger', side);
  const text = textOf(col);

  assert(text.includes('This run produced no transcript turns'),
    'the bland fallback renders when no result block is available');
  assert(!text.includes('Run terminated without structured turn events'),
    'the timed-out panel must NOT render without a result block');
});

test('matchupDetailKey includes champion / challenger result digest tokens', () => {
  // Drive the mock-mode conversation path so convData is seeded with the
  // standard mock shape (no result block — the mock's default state).
  state.mock = true;
  state.applySnapshot(mockSnapshot());
  globalThis.location.hash = '#/conversation/extract_invoice_001';
  render.applyRoute();

  const key = render.matchupDetailKey();
  // The new digest tokens must be present so a late-arriving loss.json
  // (which adds a `result` block to a side) re-keys the matchup-detail
  // subtree and the panel gets re-rendered. Without these tokens, a
  // result arriving after the transcript turns count stabilised would
  // be filtered out by swapIfChanged.
  assert(key.includes('champResult:'),
    'matchupDetailKey must include the champion result digest token');
  assert(key.includes('chalResult:'),
    'matchupDetailKey must include the challenger result digest token');

  // A repeated render of the same selection produces the same digest.
  const key2 = render.matchupDetailKey();
  assertEqual(key, key2,
    'a repeated render of the same selection must produce the same digest');
});

test('a result block changes the rendered column text — the digest is therefore load-bearing', () => {
  // Direct proof of why the digest must fold result into the key: the
  // rendered output depends on whether a side has a result block. The
  // digest contribution is what gates swapIfChanged into rebuilding.
  const sideNoResult = {
    run_id: 'r1',
    generation_id: 'v1',
    transcript: { event_count: 2, complete: true, turns: [], annotations: [] },
    result: null,
  };
  const sideWithResult = {
    run_id: 'r1',
    generation_id: 'v1',
    transcript: { event_count: 2, complete: true, turns: [], annotations: [] },
    result: {
      wall_clock_budget_exceeded: true,
      runtime_ms: 180000,
      pass_fail: false,
      expectation_result: { kind: 'predicate', passed: false, detail: '' },
      metric_counts: [
        { name: 'cost:llm_calls', count: 8.0, severity: '' },
      ],
    },
  };
  const t1 = textOf(render.renderTranscriptColumn('challenger', sideNoResult));
  const t2 = textOf(render.renderTranscriptColumn('challenger', sideWithResult));
  assert(t1 !== t2,
    'a result block must change the rendered column text — the digest must therefore include it');
  assert(t1.includes('This run produced no transcript turns'),
    'no-result column renders the bland fallback');
  assert(t2.includes('Run terminated without structured turn events'),
    'with-result column renders the timed-out panel');
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
  // bug #169: the headline Δscalar tile is framed as the champion-spine
  // net (the meta-loop's actual progress), not the all-experiments
  // gross — the gross is kept alongside as a secondary signal.
  assert(text.includes('champion spine'),
    'the headline Δscalar tile names the champion spine');
  assert(text.includes('all experiments'),
    'the secondary Δscalar tile carries the all-experiments sum');
});

test('epoch header — champion-spine Δscalar tile is the headline (bug #169)', () => {
  // The t6 run-#8 shape: 5 experiments, 2 promoted, 3 rejected. The
  // spine net (sum across promoted hops) is `-14.429 + -24.331 =
  // -38.760` and reads as an improvement (green). The gross net sums
  // every experiment and lands at `+19.482` — informative but the
  // *wrong* number to lead with. The fix splits the old single tile
  // into a primary spine tile + a secondary gross tile.
  state.epochDef = {
    epoch_id: '2026-05-20_presn',
    closed: false,
    experiments: [
      { generation_id: 'v1', parent_generation_id: 'v0',
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -14.429 } },
      { generation_id: 'v2', parent_generation_id: 'v1',
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 10.123 } },
      { generation_id: 'v3', parent_generation_id: 'v1',
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -24.331 } },
      { generation_id: 'v4', parent_generation_id: 'v3',
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 42.405 } },
      { generation_id: 'v5', parent_generation_id: 'v3',
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 5.714 } },
    ],
  };
  render.showView('epoch');
  const header = doc.getElementById('epoch-overview');
  const tiles = header._descendants().filter(
    (n) => n.classList && n.classList.contains('epoch-stat'));
  const tileByLabel = new Map();
  for (const t of tiles) {
    const label = t._descendants()
      .find((n) => n.classList && n.classList.contains('epoch-stat-label'));
    const value = t._descendants()
      .find((n) => n.classList && n.classList.contains('epoch-stat-value'));
    if (label && value) tileByLabel.set(label.textContent.trim(), { tile: t, value });
  }

  // The headline tile renders the spine net as `-38.760` and is green
  // (a negative loss delta is an improvement).
  const spine = tileByLabel.get('net Δscalar (champion spine)');
  assert(spine, 'the headline tile is labelled "net Δscalar (champion spine)"');
  assertEqual(spine.value.textContent, '-38.760',
    'spine net = sum of promoted hops only');
  assert(spine.value.classList.contains('good'),
    'a negative spine net (improvement) is coloured green');
  assert(!spine.value.classList.contains('bad'),
    'an improving spine must NOT carry the red `bad` class');
  // The headline tile carries the headline class so CSS can render it
  // larger / more prominently than the secondary tile.
  assert(spine.tile.classList.contains('epoch-stat-headline'),
    'the spine tile carries the `epoch-stat-headline` class for prominence');

  // The secondary tile renders the all-experiments gross at `+19.482`
  // and is NOT colour-coded — a rising gross can coexist with a
  // falling spine, which is exactly the bug-#169 misframing.
  const gross = tileByLabel.get('gross Δscalar (all experiments)');
  assert(gross, 'the secondary tile is labelled "gross Δscalar (all experiments)"');
  assertEqual(gross.value.textContent, '+19.482',
    'gross net = sum across every experiment, promoted or not');
  assert(!gross.value.classList.contains('good')
      && !gross.value.classList.contains('bad'),
    'the gross tile is neutral — not colour-coded by sign');
  assert(gross.tile.classList.contains('epoch-stat-secondary'),
    'the gross tile carries the `epoch-stat-secondary` class for de-emphasis');
});

test('epoch header — spine tile reads "—" when fewer than two promotions', () => {
  // A single promoted generation is the default first-tournament
  // outcome (baseline → first child). The meta-loop has not yet
  // chained two promotions, so the spine tile reads "—" rather than
  // misleadingly framing one tournament as the spine.
  state.epochDef = {
    epoch_id: '2026-05-20_solo',
    closed: false,
    experiments: [
      { generation_id: 'v1', parent_generation_id: 'v0',
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -2.0 } },
      { generation_id: 'v2', parent_generation_id: 'v1',
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 1.5 } },
    ],
  };
  render.showView('epoch');
  const header = doc.getElementById('epoch-overview');
  const tiles = header._descendants().filter(
    (n) => n.classList && n.classList.contains('epoch-stat'));
  const spine = tiles.find((t) => {
    const lbl = t._descendants()
      .find((n) => n.classList && n.classList.contains('epoch-stat-label'));
    return lbl && lbl.textContent.includes('champion spine');
  });
  assert(spine, 'the spine tile is rendered even when "—"');
  const value = spine._descendants()
    .find((n) => n.classList && n.classList.contains('epoch-stat-value'));
  assertEqual(value.textContent, '—',
    'one promotion = no spine comparison yet -> "—"');
  assert(!value.classList.contains('good')
      && !value.classList.contains('bad'),
    'a "—" spine tile is neutral — not coloured');
});

test('epoch header — spine/gross helper pins the exact t6 numbers', () => {
  // The pure helper is exported so a test can pin it directly without
  // routing through the DOM. Same fixture as the rendering test
  // above; matches the Python helper's t6 pin one-to-one.
  const summary = render.computeEpochDeltaScalarSummary([
    { generation_id: 'v1', parent_generation_id: 'v0',
      outcome: { tournament_decision: 'promoted', scalar_score_delta: -14.429 } },
    { generation_id: 'v2', parent_generation_id: 'v1',
      outcome: { tournament_decision: 'rejected', scalar_score_delta: 10.123 } },
    { generation_id: 'v3', parent_generation_id: 'v1',
      outcome: { tournament_decision: 'promoted', scalar_score_delta: -24.331 } },
    { generation_id: 'v4', parent_generation_id: 'v3',
      outcome: { tournament_decision: 'rejected', scalar_score_delta: 42.405 } },
    { generation_id: 'v5', parent_generation_id: 'v3',
      outcome: { tournament_decision: 'rejected', scalar_score_delta: 5.714 } },
  ]);
  // Use a tight tolerance: the operator caught this calc by inspection,
  // so the test must match to three decimals.
  assert(Math.abs(summary.champion_spine - (-38.760)) < 1e-6,
    `spine net must be -38.760, got ${summary.champion_spine}`);
  assert(Math.abs(summary.gross - 19.482) < 1e-6,
    `gross net must be +19.482, got ${summary.gross}`);
});

test('epoch header — backend-provided summary wins over client fallback', () => {
  // When the backend hands `delta_scalar_summary` down, the renderer
  // MUST use that — re-deriving from `experiments` is the fallback,
  // not the primary path. This pins the integration contract.
  state.epochDef = {
    epoch_id: '2026-05-20_be',
    closed: false,
    // The experiments deliberately disagree with the summary so the
    // test fails if the renderer re-derives client-side.
    experiments: [
      { generation_id: 'v1', parent_generation_id: 'v0',
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -999.0 } },
    ],
    delta_scalar_summary: {
      champion_spine: -1.234,
      gross: -1.234,
    },
  };
  render.showView('epoch');
  const header = doc.getElementById('epoch-overview');
  const tiles = header._descendants().filter(
    (n) => n.classList && n.classList.contains('epoch-stat'));
  const findValue = (labelSubstr) => {
    const tile = tiles.find((t) => {
      const lbl = t._descendants()
        .find((n) => n.classList && n.classList.contains('epoch-stat-label'));
      return lbl && lbl.textContent.includes(labelSubstr);
    });
    if (!tile) return null;
    return tile._descendants()
      .find((n) => n.classList && n.classList.contains('epoch-stat-value'));
  };
  assertEqual(findValue('champion spine').textContent, '-1.234',
    'the spine tile reflects the backend-provided summary');
  assertEqual(findValue('all experiments').textContent, '-1.234',
    'the gross tile reflects the backend-provided summary');
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

test('each experiment renders as a terse entry that expands to the four beats', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('epoch');
  const log = doc.getElementById('epoch-experiment-log');
  const cards = log._descendants().filter((n) => n.classList.contains('exp-card'));
  // The mock epoch carries three experiments (v1, v2, v5).
  assertEqual(cards.length, 3, 'one entry per experiment in the epoch');

  // Terse by default — the one-line summary shows the gen id, the core
  // idea, and the verdict, but NOT the full Hypothesis / Outcome beats.
  const firstSummary = cards[0]._descendants()
    .find((n) => n.classList.contains('exp-summary'));
  assert(firstSummary, 'each entry exposes a terse summary line');
  assert(firstSummary.textContent.includes('v1'),
    'the summary shows the generation id');
  assert(firstSummary.textContent.includes('Tighten the extraction schema'),
    'the summary shows the proposer core idea');
  assert(!cards[0].textContent.includes('Hypothesis'),
    'a collapsed entry does not show the Hypothesis beat');

  // Expanding an entry reveals the full four-beat detail.
  firstSummary.dispatchEvent(makeEvent('click'));
  const expanded = doc.getElementById('epoch-experiment-log')._descendants()
    .filter((n) => n.classList.contains('exp-card'))[0];
  const t = expanded.textContent;
  assert(t.includes('Hypothesis'), 'an expanded entry shows the Hypothesis beat');
  assert(t.includes('Change'), 'an expanded entry shows the Change beat');
  assert(t.includes('Outcome'), 'an expanded entry shows the Outcome beat');

  // The first experiment (v1) was rejected — its entry carries the
  // rejected accent and surfaces the rejection reason once expanded.
  assert(expanded.classList.contains('exp-card-rejected'),
    'a rejected experiment entry carries the rejected accent');
  assert(t.includes('pass-rate regression'),
    'a rejected outcome surfaces the rejection reason');
  assert(t.includes('Δscalar'), 'a decided outcome shows the scalar delta');
});

test('the merged Experiments section includes the incomplete experiment', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('epoch');
  const log = doc.getElementById('epoch-experiment-log');
  const cards = log._descendants().filter((n) => n.classList.contains('exp-card'));
  // v5 has no outcome — its tournament never reached a verdict. The raw
  // journal drops it; the merged Experiments section MUST include it.
  const v5 = cards.find((c) => c.getAttribute('data-genid') === 'v5');
  assert(v5, 'an experiment with no verdict still appears in the log');
  assert(v5.classList.contains('exp-card-pending'),
    'an incomplete experiment carries the pending accent');
  // Canonical term: `incomplete`, never "in progress".
  assert(v5.textContent.includes('incomplete'),
    'an unfinished experiment reads as incomplete');
  assert(!v5.textContent.toLowerCase().includes('in progress'),
    'an unfinished experiment never reads as "in progress"');
});

test('the Epoch view has no separate Journal section', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('epoch');
  // The Journal section is merged into Experiments — it must not exist
  // as its own section any more.
  assert(!doc.getElementById('epoch-journal'),
    'the standalone Journal panel is removed');
  assert(!doc.getElementById('epoch-journal-section'),
    'the standalone Journal section is removed');
  // The merged section is named "Experiments".
  const section = doc.getElementById('epoch-experiments-section');
  assert(section, 'the merged section is `epoch-experiments-section`');
  // A raw-journal jump-off link is still offered (not its own section).
  const log = doc.getElementById('epoch-experiment-log');
  const rawLink = log._descendants()
    .find((n) => n.classList && n.classList.contains('exp-journal-raw-link'));
  assert(rawLink, 'a "view raw journal" link is offered');
  // The link MUST target the markdown endpoint (text/markdown), NOT the
  // JSON-envelope endpoint that wraps the file in `{ epoch_id, journal }`
  // — the user-facing expectation is human-readable content.
  const href = rawLink.getAttribute('href');
  assert(href && /\/journal\.md(?:$|\?)/.test(href),
    `"View raw journal" must link to journal.md, got "${href}"`);
});

test('experiment entry diff toggle expands the change without throwing', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('epoch');
  // Expand the first entry so its Change beat (and diff toggle) render.
  // `aria-expanded` is checked first so the test is order-independent
  // (a prior test may have left this entry expanded).
  const summary = doc.getElementById('epoch-experiment-log')._descendants()
    .find((n) => n.classList.contains('exp-summary'));
  if (summary.getAttribute('aria-expanded') !== 'true') {
    summary.dispatchEvent(makeEvent('click'));
  }
  const log = doc.getElementById('epoch-experiment-log');
  const toggles = log._descendants().filter((n) => n.classList.contains('exp-diff-toggle'));
  assert(toggles.length > 0, 'an expanded entry with a patch exposes a diff toggle');
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

// -- Merged Experiments section — the journal, well-rendered ----------
// The journal is no longer its own section: it is folded into the
// chronological per-round Experiments log. A journal round's free prose
// renders as a "Journal note" beat inside the matching expanded entry,
// with no literal `**` markdown markers leaking onto the page.

test('a journal round prose renders as a note inside the matching entry', () => {
  state.applySnapshot(mockSnapshot());
  render.showView('epoch');
  // Expand v2 — its journal section carries the prose "Validate-before-
  // emit cleared the dominant schema_violation drift."
  const v2 = doc.getElementById('epoch-experiment-log')._descendants()
    .find((n) => n.classList.contains('exp-card')
      && n.getAttribute('data-genid') === 'v2');
  const summary = v2._descendants()
    .find((n) => n.classList.contains('exp-summary'));
  summary.dispatchEvent(makeEvent('click'));
  const expanded = doc.getElementById('epoch-experiment-log')._descendants()
    .find((n) => n.classList.contains('exp-card')
      && n.getAttribute('data-genid') === 'v2');
  const text = expanded.textContent;
  // The free prose from the journal round renders as a note.
  assert(text.includes('Validate-before-emit cleared'),
    'a journal round prose renders inside the matching experiment entry');
  // No literal markdown bold markers leak onto the page.
  assert(!text.includes('**'),
    'the merged log carries no literal ** markdown markers');
});

test('epoch experiments degrade to a muted empty line when absent', () => {
  state.epochDef = { epoch_id: '2026-05-15_e1', experiments: [], journal: '' };
  render.showView('epoch');
  const log = doc.getElementById('epoch-experiment-log');
  assert(log.textContent.includes('No experiments recorded'),
    'an epoch with no experiments shows a muted empty state');
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

test('conversation view renders "conversation started" first and the rest in order',
async () => {
  // The server-side transcript reconstructor (see
  // tests/test_dashboard_transcript.py) places the synthetic
  // ``conversation_started`` turn FIRST. The conversation view here is
  // a thin renderer over the transcript's `turns` array, so the
  // assertion is that the view paints the turns in the order it
  // received them — turn 0 ("conversation started") first, the real
  // user / agent / system turns following in their natural order.
  state.applySnapshot(mockSnapshot());
  state.mock = false;   // selectConversation only fetches in non-mock mode

  const transcript = {
    run_id: 'r-conv',
    event_count: 5,
    complete: true,
    turns: [
      { seq: null, ts: '2026-05-19T00:00:00.000Z', agent: '', role: 'system',
        kind: 'conversation_started', text: 'conversation started' },
      { seq: 1, ts: '2026-05-19T00:00:00.100Z', agent: '', role: 'user',
        kind: 'run_started', text: 'first user prompt' },
      { seq: 2, ts: '2026-05-19T00:00:01Z', agent: 'alpha', role: 'agent',
        kind: 'goldfive_llm_call_end', text: 'first assistant reply' },
      { seq: 3, ts: '2026-05-19T00:00:02Z', agent: 'alpha', role: 'agent',
        kind: 'goldfive_llm_call_end', text: 'final assistant reply' },
      { seq: 4, ts: '2026-05-19T00:00:03Z', agent: '', role: 'system',
        kind: 'run_completed', text: 'done' },
    ],
    annotations: [],
  };
  const payload = {
    champion: { run_id: 'r-conv', generation_id: 'v0', transcript },
    challenger: null,
  };
  globalThis.fetch = async (path) => {
    if (String(path).includes('/api/matchup/')
        && String(path).includes('/conversations')) {
      return { ok: true, json: async () => payload };
    }
    return { ok: true, json: async () => ({}) };
  };

  globalThis.location.hash = '#/conversation/turn_order_demo';
  render.applyRoute();
  // selectConversation kicks off the fetch; let it settle, then the
  // resolver re-renders the conversation view itself.
  await new Promise((resolve) => setTimeout(resolve, 5));

  const panel = doc.getElementById('conversation-panel');
  const turns = panel._descendants().filter(
    (n) => n.classList && n.classList.contains('conversation-turn'));
  assert(turns.length === 5,
    `expected 5 rendered turns, got ${turns.length}`);
  const turnTexts = turns.map((n) => n.textContent);

  // The synthetic "conversation started" turn is rendered FIRST — not
  // last (the bug) and not somewhere in the middle.
  assert(turnTexts[0].includes('conversation started'),
    `expected the first rendered turn to be "conversation started", ` +
    `got: ${turnTexts[0]}`);
  // The real turns follow in their input order.
  assert(turnTexts[1].includes('first user prompt'),
    `expected the second turn to be the user prompt, got: ${turnTexts[1]}`);
  assert(turnTexts[2].includes('first assistant reply'),
    `expected the third turn to be the first assistant reply, got: ${turnTexts[2]}`);
  assert(turnTexts[3].includes('final assistant reply'),
    `expected the fourth turn to be the final assistant reply, got: ${turnTexts[3]}`);
  assert(turnTexts[4].includes('done'),
    `expected the fifth turn to be the run_completed marker, got: ${turnTexts[4]}`);
  // And the run_completed terminal is rendered AFTER "conversation
  // started" — the bug had run_completed before the conversation
  // frame.
  const startedAt = turnTexts.findIndex((t) => t.includes('conversation started'));
  const completedAt = turnTexts.findIndex((t) => t.includes('done'));
  assert(startedAt >= 0 && completedAt >= 0,
    'both the conversation_started and run_completed turns must render');
  assert(startedAt < completedAt,
    `"conversation started" must render before "done"; ` +
    `got positions ${startedAt} and ${completedAt}`);

  delete globalThis.fetch;
  state.mock = true;
  // Reset conversation state so a later test isn't held on this entry.
  globalThis.location.hash = '#/conversation';
  render.applyRoute();
});

await run();
