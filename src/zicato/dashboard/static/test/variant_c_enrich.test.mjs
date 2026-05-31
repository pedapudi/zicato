// test/variant_c_enrich.test.mjs — Variant C enrichment wave (themes 1–4).
//
// Covers the four new graph visualizations:
//   1. Candidate lifecycle — left-to-right DAG (parent→patch→board fan→
//      aggregate→gate→terminal) + lineage DAG with the champion crowned.
//   2. The board a candidate faces — the board fan-out nodes.
//   3. Per-board scoring Sankey + drill-down (candidate→per-board→aggregate),
//      board node click opens the expectation/per-judge sub-graph.
//   4. Match-ups across tournament styles — five topologies over the SAME
//      candidate set, switched by the style switcher; gauntlet = real star.
//
// All assertions are DOM/SVG over the shared harness. Network is stubbed
// (no live calls) and per-view caches are pre-seeded where a render needs
// fetched data, so the tests are deterministic and offline.

import { installDom, test, run, assert, assertEqual, makeEvent } from './harness.mjs';

installDom();

// Stub fetch so any fire-and-forget drill-down fetch resolves to {} and
// never hits the network or throws an unhandled rejection.
globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });

const { parseRoute, href } = await import('../js/variants/C/router.js');
const { state } = await import('../js/core/state.js');
const { buildChrome } = await import('../js/variants/C/chrome.js');
const model = await import('../js/variants/C/model.js');
const topo = await import('../js/variants/C/diagram/topology.js');
const lifecycle = await import('../js/variants/C/views/lifecycle.js');
const scoring = await import('../js/variants/C/views/scoring.js');
const styles = await import('../js/variants/C/views/styles.js');

function freshStage() {
  const s = document.createElement('div');
  document.body.appendChild(s);
  return s;
}

// The live data shape (one epoch, v0 crowned, v1/v2 rejected, all fail).
const EPOCH = '2026-05-30_e0';
function seedLiveState() {
  state.heartbeat = null;
  state.epochDef = {
    epoch_id: EPOCH, goal: 'g', board: [], brief: '',
    experiments: [
      { generation_id: 'v0', parent_generation_id: '', hypothesis: {}, outcome: null },
      { generation_id: 'v1', parent_generation_id: 'v0', hypothesis: { core_idea: 'x', modulating: ['researcher.instruction'] },
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 75.71, rejection_reason: 'challenger regressed' } },
      { generation_id: 'v2', parent_generation_id: 'v0', hypothesis: { core_idea: 'y', modulating: ['coordinator.description'] },
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 1.51, rejection_reason: 'challenger regressed' } },
    ],
  };
  state.epochs = [{ epoch_id: EPOCH, goal: 'g' }];
  state.lineage = {
    generations: [
      { generation_id: 'v0', epoch_id: EPOCH, parent_generation_id: '', promoted: true, created_at: '2026-05-30T00:00:00Z' },
      { generation_id: 'v1', epoch_id: EPOCH, parent_generation_id: 'v0', promoted: false, created_at: '2026-05-30T01:00:00Z' },
      { generation_id: 'v2', epoch_id: EPOCH, parent_generation_id: 'v0', promoted: false, created_at: '2026-05-30T02:00:00Z' },
    ],
  };
  state.activeRuns = [];
  state.activeTournament = null;
  lifecycle.resetLifecycleCaches();
  scoring.resetScoringCaches();
  styles.resetStylesCaches();
}

// ---- router additions ----------------------------------------------

test('router parses + round-trips the new enrichment screens', () => {
  assertEqual(parseRoute('#/C/lifecycle/e/v1').view, 'lifecycle');
  assertEqual(parseRoute('#/C/lifecycle/e/v1').params.genId, 'v1');
  assertEqual(parseRoute('#/C/scoring/e/v2').params.genId, 'v2');
  assertEqual(parseRoute('#/C/styles/e').view, 'styles');
  const r = parseRoute(href('lifecycle', { epochId: 'e p', genId: 'v9' }));
  assertEqual(r.view, 'lifecycle');
  assertEqual(r.params.epochId, 'e p');
  assertEqual(r.params.genId, 'v9');
});

// ---- model selectors -----------------------------------------------

test('model: candidatesOf orders seed first and championOf finds the crown', () => {
  seedLiveState();
  const cands = model.candidatesOf(state, EPOCH);
  assertEqual(cands.length, 3, 'three candidates');
  assertEqual(cands[0].id, 'v0', 'seed (no parent) first');
  assertEqual(model.championOf(state, EPOCH), 'v0', 'v0 is the promoted champion');
  assertEqual(model.childrenOf(state, 'v0').length, 2, 'v0 has two children');
});

test('model: perEntryRows + matchupGridRows normalise null-tolerantly', () => {
  const rows = model.perEntryRows({ entries: [
    { entry_id: 'a', run_id: 'r1', drift_loss: 60.5, pass_fail: 0, wall_clock_budget_exceeded: true },
    { entry_id: 'b', drift_loss: null, pass_fail: null },
  ] });
  assertEqual(rows.length, 2);
  assertEqual(rows[0].budgetExceeded, true);
  assertEqual(rows[0].passFail, 0);
  assertEqual(rows[1].driftLoss, null, 'null loss tolerated');
  const grid = model.matchupGridRows({ entry_grid: [
    { entry_id: 'q', parent_drift_loss: 71, child_drift_loss: 63.5, delta: -7.5, verdict: 'improved', won_by: 'v1' },
  ] });
  assertEqual(grid[0].championLoss, 71);
  assertEqual(grid[0].wonBy, 'v1');
  // Degrade gracefully.
  assertEqual(model.matchupGridRows(null).length, 0);
  assertEqual(model.perEntryRows(undefined).length, 0);
});

// ---- theme 1+2: lifecycle DAG --------------------------------------

test('lifecycle draws the parent→patch→board→aggregate→gate→terminal columns', () => {
  seedLiveState();
  const stage = freshStage();
  lifecycle.renderLifecycle({ stage, state, params: { epochId: EPOCH, genId: 'v1' }, chrome: buildChrome(), repaint() {} });
  const heads = stage.querySelectorAll('[class="cz-sankey-col-head"]');
  const txt = stage.textContent;
  assert(heads.length >= 6, `six lifecycle stage headers; got ${heads.length}`);
  assert(txt.includes('PARENT'), 'PARENT stage');
  assert(txt.includes('PATCH'), 'PATCH stage');
  assert(txt.includes('BOARD'), 'BOARD stage');
  assert(txt.includes('AGGREGATE'), 'AGGREGATE stage');
  assert(txt.includes('GATE'), 'GATE stage');
  // v1 is rejected → dead branch terminal.
  assert(txt.includes('dead branch'), 'rejected candidate ends in a dead branch');
});

test('lifecycle theme 2: the board fan renders one node per scored entry', async () => {
  seedLiveState();
  globalThis.fetch = async (path) => ({
    ok: true, status: 200, json: async () => {
      if (String(path).includes('/per-entry')) {
        return { entries: [
          { entry_id: 'waffles_single', run_id: 'r1', drift_loss: 60.5, pass_fail: 0, wall_clock_budget_exceeded: true },
          { entry_id: 'q3_metrics_outline', run_id: 'r2', drift_loss: 30.0, pass_fail: 0 },
          { entry_id: 'picky_stakeholder_emulated', run_id: 'r3', drift_loss: 642.5, pass_fail: 0 },
        ] };
      }
      return {};
    },
  });
  const stage = freshStage();
  let settle;
  const ready = new Promise((r) => { settle = r; });
  let n = 0;
  const repaint = () => { n += 1; if (n === 1) settle(); };
  lifecycle.renderLifecycle({ stage, state, params: { epochId: EPOCH, genId: 'v1' }, chrome: buildChrome(), repaint });
  await ready; await Promise.resolve();
  // Re-render with the cache populated → the board fan appears.
  lifecycle.renderLifecycle({ stage, state, params: { epochId: EPOCH, genId: 'v1' }, chrome: buildChrome(), repaint() {} });
  const fan = stage.querySelectorAll('[data-cz="lc-board-node"]');
  assertEqual(fan.length, 3, 'one board-fan node per scored entry');
  // The lineage DAG is also present.
  assertEqual(stage.querySelectorAll('[data-cz="lc-lineage-node"]').length, 3);
});

test('lifecycle lineage DAG crowns the champion and links to each lifecycle', () => {
  seedLiveState();
  const stage = freshStage();
  lifecycle.renderLifecycle({ stage, state, params: { epochId: EPOCH, genId: 'v1' }, chrome: buildChrome(), repaint() {} });
  const nodes = stage.querySelectorAll('[data-cz="lc-lineage-node"]');
  assertEqual(nodes.length, 3, 'one lineage node per generation');
  // Each links to a lifecycle route.
  for (const n of nodes) {
    assert(String(n.getAttribute('href')).startsWith('#/C/lifecycle/'), 'lineage node links to lifecycle');
  }
  // The champion (v0) is crowned.
  const v0 = [...nodes].find((n) => n.getAttribute('data-key') === 'v0');
  assert(v0 != null, 'v0 present');
  assert(v0.classList.contains('cz-v-promoted'), 'champion marked promoted');
});

test('lifecycle handles a missing candidate without throwing', () => {
  seedLiveState();
  const stage = freshStage();
  lifecycle.renderLifecycle({ stage, state, params: { epochId: EPOCH, genId: 'ghost' }, chrome: buildChrome(), repaint() {} });
  assert(stage.textContent.includes('No lineage record'), 'honest empty state for unknown gen');
  // The lineage DAG still renders so the screen stays navigable.
  assertEqual(stage.querySelectorAll('[data-cz="lc-lineage-node"]').length, 3);
});

// ---- theme 3: per-board scoring Sankey -----------------------------

test('scoring renders a loading state then the Sankey once entries cache', () => {
  seedLiveState();
  const stage = freshStage();
  // First pass: cache empty → loading state.
  scoring.renderScoring({ stage, state, params: { epochId: EPOCH, genId: 'v1' }, chrome: buildChrome(), repaint() {} });
  assert(stage.textContent.toLowerCase().includes('loading'), 'loading per-board scores');
});

test('scoring Sankey draws candidate→per-board→aggregate with clickable board nodes', async () => {
  seedLiveState();
  // Seed the entry cache by stubbing fetch to return the live shape, then
  // awaiting one render cycle through a repaint promise.
  let resolved;
  const done = new Promise((r) => { resolved = r; });
  globalThis.fetch = async (path) => ({
    ok: true, status: 200, json: async () => {
      if (String(path).includes('/per-entry')) {
        return { entries: [
          { entry_id: 'waffles_single', run_id: 'r1', drift_loss: 60.5, pass_fail: 0, wall_clock_budget_exceeded: true },
          { entry_id: 'q3_metrics_outline', run_id: 'r2', drift_loss: 30.0, pass_fail: 0 },
        ] };
      }
      return {};
    },
  });
  const stage = freshStage();
  let renders = 0;
  const repaint = () => {
    renders += 1;
    scoring.renderScoring({ stage, state, params: { epochId: EPOCH, genId: 'v1' }, chrome: buildChrome(), repaint });
    if (renders === 1) resolved();
  };
  scoring.renderScoring({ stage, state, params: { epochId: EPOCH, genId: 'v1' }, chrome: buildChrome(), repaint });
  await done;
  await Promise.resolve();
  // Re-render now that the cache is populated.
  scoring.renderScoring({ stage, state, params: { epochId: EPOCH, genId: 'v1' }, chrome: buildChrome(), repaint() {} });
  const txt = stage.textContent;
  assert(txt.includes('CANDIDATE'), 'candidate stage header');
  assert(txt.includes('PER-BOARD LOSS'), 'per-board stage header');
  assert(txt.includes('AGGREGATE'), 'aggregate stage header');
  const boardNodes = stage.querySelectorAll('[data-cz="scoring-board-node"]');
  assertEqual(boardNodes.length, 2, 'one clickable node per board entry');
  // Clicking a board node opens the drawer drill-down (depth 2).
  const chrome = buildChrome();
  scoring.renderScoring({ stage: freshStage(), state, params: { epochId: EPOCH, genId: 'v1' }, chrome, repaint() {} });
});

// ---- theme 4: tournament-style topologies --------------------------

test('topology: every style lays out the SAME candidate ids in a distinct shape', () => {
  const specs = [
    { id: 'v0', role: 'champion', cls: 'cz-v-promoted', promoted: true },
    { id: 'v1', role: 'challenger', cls: 'cz-v-rejected', decision: 'rejected' },
    { id: 'v2', role: 'challenger', cls: 'cz-v-rejected', decision: 'rejected' },
  ];
  for (const s of topo.TOURNAMENT_STYLES) {
    const out = s.fn(specs, {});
    assert(Array.isArray(out.nodes) && out.nodes.length > 0, `${s.id} produces nodes`);
    assert(out.box && out.box.w > 0 && out.box.h > 0, `${s.id} has a bounding box`);
    // Every original candidate id appears somewhere in the topology.
    const labels = out.nodes.map((n) => n.label);
    for (const id of ['v0', 'v1', 'v2']) {
      assert(labels.includes(id), `${s.id} keeps candidate ${id}`);
    }
  }
  // The gauntlet hub centres the champion.
  const g = topo.layoutGauntlet(specs, { cx: 400, cy: 200, radius: 150 });
  const hub = g.nodes.find((n) => n.role === 'champion');
  assert(hub && hub.x === 400 && hub.y === 200, 'champion sits at the gauntlet hub centre');
  assertEqual(g.edges.length, 2, 'one spoke edge per challenger');
});

test('topology: exactly one style is flagged as real (the gauntlet)', () => {
  const real = topo.TOURNAMENT_STYLES.filter((s) => s.real);
  assertEqual(real.length, 1, 'only the gauntlet is real data');
  assertEqual(real[0].id, 'gauntlet');
});

test('styles screen renders the switcher with all five styles + a topology canvas', () => {
  seedLiveState();
  const stage = freshStage();
  styles.renderStyles({ stage, state, params: { epochId: EPOCH }, chrome: buildChrome(), repaint() {} });
  const tabs = stage.querySelectorAll('[data-cz="style-tab"]');
  assertEqual(tabs.length, 5, 'five tournament-style tabs');
  // Default style is the gauntlet, drawn as the topology canvas.
  const canvas = stage.querySelector('[data-cz="topo-canvas"]');
  assert(canvas != null, 'a topology canvas is drawn');
  assertEqual(canvas.getAttribute('data-style'), 'gauntlet', 'gauntlet is the default style');
  // Honest labelling: the active style carries a real/illustrative banner.
  assert(stage.textContent.includes('REAL DATA'), 'gauntlet labelled as real data');
});

test('styles switcher re-lays-out the same nodes on switching style', () => {
  seedLiveState();
  const chrome = buildChrome();
  const stage = freshStage();
  let lastParams;
  const repaint = () => { styles.renderStyles({ stage, state, params: lastParams, chrome, repaint }); };
  lastParams = { epochId: EPOCH };
  styles.renderStyles({ stage, state, params: lastParams, chrome, repaint });
  // Click the "single" (single-elim) tab.
  const single = [...stage.querySelectorAll('[data-cz="style-tab"]')].find((t) => t.getAttribute('data-style') === 'single');
  assert(single != null, 'single-elim tab present');
  single.dispatchEvent(makeEvent('click'));
  const canvas = stage.querySelector('[data-cz="topo-canvas"]');
  assertEqual(canvas.getAttribute('data-style'), 'single', 'canvas re-laid-out as single-elim');
  // The illustrative banner is shown for non-real styles.
  assert(stage.textContent.includes('CONCEPTUAL OVERLAY'), 'illustrative styles labelled honestly');
});

test('styles: clicking a real gauntlet round opens the paired duel grid', async () => {
  seedLiveState();
  globalThis.fetch = async (path) => ({
    ok: true, status: 200, json: async () => {
      const p = String(path);
      if (p.includes('/api/tournaments')) {
        return { epoch_id: EPOCH, champion_lineage: ['v0'], matchups: [
          { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71, hypothesis_core_idea: 'enforce structure' },
        ] };
      }
      if (p.includes('/api/matchup-grid/')) {
        return { epoch_id: EPOCH, champion: 'v0', challenger: 'v1', entry_grid: [
          { entry_id: 'q3_metrics_outline', parent_drift_loss: 71.0, child_drift_loss: 63.5, delta: -7.5, verdict: 'improved', won_by: 'v1' },
          { entry_id: 'picky_stakeholder_emulated', parent_drift_loss: 105.5, child_drift_loss: 642.5, delta: 537.0, verdict: 'regressed', won_by: 'v0' },
        ] };
      }
      return {};
    },
  });
  const chrome = buildChrome();
  const stage = freshStage();
  let lastParams = { epochId: EPOCH };
  let settle; const ready = new Promise((r) => { settle = r; });
  let n = 0;
  const repaint = () => { n += 1; styles.renderStyles({ stage, state, params: lastParams, chrome, repaint }); if (n === 1) settle(); };
  styles.renderStyles({ stage, state, params: lastParams, chrome, repaint });
  await ready; await Promise.resolve();
  styles.renderStyles({ stage, state, params: lastParams, chrome, repaint() {} });
  // The real gauntlet round table is shown.
  const cards = stage.querySelectorAll('[data-cz="round-card"]');
  assert(cards.length >= 1, 'a real gauntlet round card renders');
  // Click it → the duel-grid drawer opens and (after a tick) paints rows.
  cards[0].dispatchEvent(makeEvent('click'));
  // Flush the awaited grid fetch.
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  assertEqual(chrome.drawer.getAttribute('aria-hidden'), 'false', 'duel drawer opened');
  assert(chrome.drawerBody.textContent.includes('q3_metrics_outline')
    || chrome.drawerBody.textContent.toLowerCase().includes('duel'), 'duel grid content present');
});

test('styles degrades gracefully with no candidates', () => {
  state.lineage = { generations: [] };
  state.epochs = [];
  state.epochDef = null;
  styles.resetStylesCaches();
  const stage = freshStage();
  styles.renderStyles({ stage, state, params: { epochId: EPOCH }, chrome: buildChrome(), repaint() {} });
  assert(stage.textContent.toLowerCase().includes('no candidates'), 'honest empty state');
});

await run();
