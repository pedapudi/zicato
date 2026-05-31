// test/variant_b_enrich.test.mjs — Variant B enrichment wave.
//
// Covers the four new visualization themes, in the Editorial Lab Notebook
// idiom, against the shared mock snapshot + a fetch stub shaped to the real
// enrichment endpoints:
//   1. Candidate lifecycle — margin timeline, hypothesis + rejection
//      pull-quotes, the genealogy figure (champion crowned).
//   2. The board plate — entries grouped by kind, small-caps tags, marginal
//      budget/weight annotations, a figure caption (no table).
//   3. Per-board scoring + drill-down — inline dot-plot rows, an entry aside
//      with expectation outcomes + per-judge detail, a transcript link.
//   4. Tournament-style fixtures — the real gauntlet ladder + paired matchup
//      grid, plus the five illustrative alternative structures.
//
// Runs under the dependency-free harness via `node`. MUST end with run().

import { installDom, test, assert, assertEqual, run, makeEvent } from './harness.mjs';

installDom();

function findClass(node, cls, out = []) {
  if (!node || node.nodeType !== 1) return out;
  if (node.className && String(node.className).split(/\s+/).includes(cls)) out.push(node);
  for (const c of node.children) findClass(c, cls, out);
  return out;
}
function text(node) { return node ? node.textContent : ''; }
function hasText(node, sub) { return text(node).includes(sub); }

// ---------------------------------------------------------------------------
// New chart primitives — totality + structure.
// ---------------------------------------------------------------------------
const charts = await import('../js/variants/B/lib/charts.js');

test('marginTimeline: builds an ordered step list; empty input degrades', () => {
  const ol = charts.marginTimeline([
    { label: 'Conceived', state: 'done' },
    { label: 'Rejected', state: 'fail' },
  ]);
  assertEqual(ol.localName, 'ol', 'a list');
  assert(findClass(ol, 'vb-mt-step').length === 2, 'two steps');
  assert(findClass(ol, 'vb-mt-fail').length === 1, 'fail step toned');
  assert(findClass(ol, 'vb-mt-last').length === 1, 'last step marked');
  const empty = charts.marginTimeline([]);
  assert(findClass(empty, 'vb-mt-step').length === 1, 'empty placeholder');
});

test('genealogy: parent → children, champion crowned; empty degrades', () => {
  const fig = charts.genealogy([
    { id: 'v0', parentId: null, verdict: 'promoted', crowned: true },
    { id: 'v1', parentId: 'v0', verdict: 'rejected' },
    { id: 'v2', parentId: 'v0', verdict: 'rejected' },
  ]);
  assert(findClass(fig, 'vb-gen-dot').length === 3, 'three nodes');
  assert(findClass(fig, 'vb-gen-link').length === 2, 'two parent links');
  assert(findClass(fig, 'vb-gen-crown').length === 1, 'champion crowned');
  const empty = charts.genealogy([]);
  assert(findClass(empty, 'vb-fig-empty').length === 1, 'labeled empty');
});

test('genealogy: onSelect fires the node id on click', () => {
  let sel = null;
  const fig = charts.genealogy(
    [{ id: 'v0', parentId: null, verdict: 'promoted' }, { id: 'v1', parentId: 'v0', verdict: 'rejected' }],
    { onSelect: (id) => { sel = id; } },
  );
  findClass(fig, 'vb-gen-node')[0].dispatchEvent(makeEvent('click'));
  assert(sel === 'v0' || sel === 'v1', 'a node id selected');
});

test('dotPlot: finite value places a toned dot; null yields an n/a track', () => {
  const ok = charts.dotPlot(60.5, { max: 100, pass: false });
  assert(findClass(ok, 'vb-dotplot-dot').length === 1, 'a dot');
  assert(findClass(ok, 'vb-regress').length === 1, 'fail toned regress');
  const na = charts.dotPlot(null);
  assert(findClass(na, 'vb-dotplot-na').length === 1, 'n/a track');
});

test('headToHead: draws paired bars; winner toned improve', () => {
  const svg = charts.headToHead(105.5, 642.5, { champId: 'v0', challId: 'v1', wonBy: 'v0' });
  assert(findClass(svg, 'vb-h2h-bar').length === 2, 'two bars');
  assert(findClass(svg, 'vb-improve').length === 1, 'winner toned');
});

// ---------------------------------------------------------------------------
// Fixtures — every alternative structure is total + a different topology.
// ---------------------------------------------------------------------------
const fixtures = await import('../js/variants/B/lib/fixtures.js');
const FIELD = [
  { id: 'v0', verdict: 'promoted', loss: 70.9 },
  { id: 'v1', verdict: 'rejected', loss: 146.6 },
  { id: 'v2', verdict: 'rejected', loss: 72.4 },
];

test('gauntletFixture: champion spine + a clickable rung per round', () => {
  let sel = null;
  const fig = fixtures.gauntletFixture('v0', [
    { challenger: 'v1', decision: 'rejected', deltaScalar: 75.71 },
    { challenger: 'v2', decision: 'rejected', deltaScalar: 1.51 },
  ], { onSelect: (id) => { sel = id; } });
  assert(findClass(fig, 'vb-gauntlet-champ').length === 1, 'champion spine');
  assert(findClass(fig, 'vb-gauntlet-rung').length === 2, 'two rounds');
  assert(findClass(fig, 'vb-gauntlet-crown').length === 1, 'crown on champion');
  findClass(fig, 'vb-gauntlet-rung')[0].dispatchEvent(makeEvent('click'));
  assertEqual(sel, 'v1', 'rung click drills to the challenger');
  // The real gauntlet is NOT marked illustrative.
  assert(findClass(fig, 'vb-illustrative').length === 0, 'gauntlet carries no illustrative mark');
});

test('matchupGridFigure: paired rows with bars + won-by; clickable', () => {
  let sel = null;
  const grid = {
    champion: 'v0', challenger: 'v1',
    entry_grid: [
      { entry_id: 'q3', parent_drift_loss: 71, child_drift_loss: 63.5, delta: -7.5, verdict: 'improved', won_by: 'v1' },
      { entry_id: 'picky', parent_drift_loss: 105.5, child_drift_loss: 642.5, delta: 537, verdict: 'regressed', won_by: 'v0' },
    ],
  };
  const fig = fixtures.matchupGridFigure(grid, charts.headToHead, { onSelect: (e) => { sel = e; } });
  assert(findClass(fig, 'vb-matchup-row').length === 2, 'two entries');
  assert(findClass(fig, 'vb-h2h').length === 2, 'paired bars per row');
  findClass(fig, 'vb-matchup-row')[0].dispatchEvent(makeEvent('click'));
  assertEqual(sel, 'q3', 'row click opens the run');
});

test('alternative fixtures: each is a DIFFERENT topology, all illustrative', () => {
  const bracket = fixtures.bracketFixture(FIELD);
  const dbl = fixtures.doubleElimFixture(FIELD);
  const rr = fixtures.roundRobinFixture(FIELD);
  const swiss = fixtures.swissFixture(FIELD);
  const race = fixtures.raceFixture(FIELD);
  assert(findClass(bracket, 'vb-bracket-svg').length === 1, 'bracket tree svg');
  assert(findClass(dbl, 'vb-double-rail').length === 2, "winners' + losers' rails");
  assert(findClass(rr, 'vb-rr-grid').length === 1, 'round-robin matrix');
  assert(findClass(swiss, 'vb-swiss-round').length >= 1, 'swiss ledger rounds');
  assert(findClass(race, 'vb-race-lane').length === FIELD.length, 'a race lane per candidate');
  // Every alternative is honestly marked illustrative.
  for (const f of [bracket, dbl, rr, swiss, race]) {
    assert(findClass(f, 'vb-illustrative').length === 1, 'illustrative mark present');
  }
});

test('fixtures: degenerate empty field yields labeled fallbacks, never throws', () => {
  for (const fn of ['bracketFixture', 'doubleElimFixture', 'roundRobinFixture', 'swissFixture', 'raceFixture']) {
    const f = fixtures[fn]([]);
    assert(findClass(f, 'vb-fig-empty').length === 1, fn + ' empty fallback');
  }
  assert(findClass(fixtures.gauntletFixture(null, []), 'vb-fig-empty').length === 1, 'gauntlet empty fallback');
});

// ---------------------------------------------------------------------------
// Data selectors.
// ---------------------------------------------------------------------------
const data = await import('../js/variants/B/lib/data.js');
const { state } = await import('../js/core/state.js');
const { mockSnapshot } = await import('../js/views/mock.js');

function seed() { state.applySnapshot(mockSnapshot()); }

test('gauntlet(): folds state.bracket into champion + rounds with deltas', () => {
  seed();
  const g = data.gauntlet();
  assert(g.rounds.length >= 1, 'rounds present');
  assert(g.champion != null, 'a champion');
  assert(g.rounds.some((r) => r.deltaScalar != null), 'a delta carried');
});

test('boardEntries(): reads the epoch board; lifecycleSteps total', () => {
  seed();
  assert(data.boardEntries().length >= 1, 'board entries from epochDef');
  const steps = data.lifecycleSteps({ outcome: { tournament_decision: 'rejected' } }, 'rejected', false);
  assert(steps.length === 5, 'five lifecycle beats');
  assertEqual(steps[steps.length - 1].state, 'fail', 'rejected terminal beat fails');
});

// ---------------------------------------------------------------------------
// Views — render under the shell with a fetch stub for the new endpoints.
// ---------------------------------------------------------------------------
const router = await import('../js/variants/B/router.js');
const { bRouter } = router;

globalThis.fetch = async (path) => ({ ok: true, async json() { return mockJsonFor(path); } });
function mockJsonFor(path) {
  if (path.includes('/per-entry')) {
    return {
      epoch_id: '2026-05-15_e1', generation_id: 'v1',
      entries: [
        { entry_id: 'extract_invoice_001', run_id: 'r-1', drift_loss: 60.5, pass_fail: 0, runtime_ms: 180000, wall_clock_budget_exceeded: true },
        { entry_id: 'schema_response', run_id: 'r-2', drift_loss: 12.0, pass_fail: 1, runtime_ms: 5000, wall_clock_budget_exceeded: false },
      ],
    };
  }
  if (path.includes('/expectations')) {
    return { epoch_id: 'e', generation_id: 'v1', entry_id: 'extract_invoice_001',
      outcomes: [{ kind: 'predicate', passed: false, detail: 'predicate returned False', judge_name: null, score: null }] };
  }
  if (path.match(/\/per-judge$/)) {
    return { epoch_id: 'e', generation_id: 'v1',
      judges: [{ judge_name: 'incorporates_feedback', weighted_loss: 27.0, raw_loss: 27.0, run_count: 1, weight: 1.0 }] };
  }
  if (path.includes('/api/matchup-grid/')) {
    return {
      epoch_id: 'e', champion: 'v0', challenger: 'v1',
      entry_grid: [
        { entry_id: 'q3_metrics_outline', parent_drift_loss: 71.0, child_drift_loss: 63.5, delta: -7.5, verdict: 'improved', won_by: 'v1' },
        { entry_id: 'picky_stakeholder_emulated', parent_drift_loss: 105.5, child_drift_loss: 642.5, delta: 537.0, verdict: 'regressed', won_by: 'v0' },
      ],
    };
  }
  if (path.endsWith('/gate')) {
    return {
      decision: 'rejected', reason: 'challenger regressed: loss rose by 75.71',
      delta_scalar: 75.71, delta_pass_rate: 0.0,
      rules: [
        { id: 'scalar_margin', label: 'Scalar margin', status: 'fail', fired: true, detail: '70.94 → 146.65' },
        { id: 'pass_rate_monotonicity', label: 'Pass-rate monotonicity', status: 'not_reached', fired: false },
      ],
      scalar_components: { champion: { drift: 68.5 }, challenger: { drift: 145.64 } },
      primary_driver: { judge: 'incorporates_feedback', delta: 24.0 },
    };
  }
  if (path.includes('/diff')) return { diff: '@@ -1,2 +1,2 @@\n-old\n+new\n ctx' };
  if (path.includes('/api/epoch')) return {};
  return {};
}

const shell = await import('../js/variants/B/shell.js');
await import('../js/variants/B/views/environment.js');
await import('../js/variants/B/views/epoch.js');
const experimentView = await import('../js/variants/B/views/experiment.js');
const boardView = await import('../js/variants/B/views/board.js');
const tournamentView = await import('../js/variants/B/views/tournament.js');
await import('../js/variants/B/views/run.js');
await import('../js/variants/B/views/bench.js');

// run-all imports test files in turn against the SHARED module graph, so a
// prior file's fetch stub may have cached the per-resource payloads with its
// own (different) shapes. Drop the view caches so this file's stub lands.
function resetCaches() {
  experimentView.resetExperimentView();
  boardView.resetBoardView();
  tournamentView.resetTournamentView();
}

function pageHost() { return document.getElementById('vb-page') || document.createElement('div'); }
function mountShell(route) {
  let root = document.getElementById('variant-root');
  if (!root) { root = document.createElement('div'); root.id = 'variant-root'; document.body.appendChild(root); document.registerId('variant-root', root); }
  window.location.hash = route;
  bRouter.resolve();
  shell.resetBShellDigest();
  shell.renderBShell(bRouter.current());
}
async function settle() { await new Promise((r) => setTimeout(r, 0)); shell.renderBShell(bRouter.current()); }

test('router: the board view resolves + crumbs under environment', () => {
  assertEqual(router.parseBHash('#/B/board').view, 'board');
  const trail = router.crumbTrail({ view: 'board', params: {} });
  assertEqual(trail[0].view, 'environment');
  assertEqual(trail[1].view, 'board');
});

test('theme 2 — board plate: grouped by kind, small-caps tags, marginal annos, caption', () => {
  seed();
  resetCaches();
  mountShell('#/B/board');
  const host = pageHost();
  assert(findClass(host, 'vb-board-lead').length === 1, 'board lead');
  assert(findClass(host, 'vb-plate').length === 1, 'the plate');
  assert(findClass(host, 'vb-plate-group').length >= 1, 'grouped by kind');
  assert(findClass(host, 'vb-plate-card').length >= 1, 'entry cards (figure, not table)');
  assert(findClass(host, 'vb-smallcaps-tag').length >= 1, 'tags in small caps');
  assert(findClass(host, 'vb-plate-anno').length >= 1, 'budget/weight marginal annotations');
  assert(findClass(host, 'vb-plate-caption').length === 1, 'a figure caption');
});

test('theme 1 — experiment: margin timeline, hypothesis + rejection pull-quotes, genealogy', async () => {
  seed();
  resetCaches();
  mountShell('#/B/experiment/v2x'); // v2x is rejected in the mock
  await settle();
  const host = pageHost();
  assert(findClass(host, 'vb-margin-timeline').length === 1, 'lifecycle margin timeline');
  assert(findClass(host, 'vb-mt-step').length === 5, 'five lifecycle beats');
  assert(findClass(host, 'vb-exp-bet').length === 1, 'hypothesis pull-quote');
  assert(findClass(host, 'vb-exp-rejection').length === 1, 'rejection-reason pull-quote');
  assert(findClass(host, 'vb-genealogy').length === 1, 'genealogy figure');
});

test('theme 3 — experiment: inline per-entry rows; aside opens with outcomes + judges + run link', async () => {
  seed();
  resetCaches();
  mountShell('#/B/experiment/v1');
  await settle();
  let host = pageHost();
  assert(findClass(host, 'vb-score-list').length === 1, 'per-entry scoring list');
  const rows = findClass(host, 'vb-score-row');
  assert(rows.length >= 1, 'a scoring row per board entry');
  assert(findClass(host, 'vb-dotplot').length >= 1, 'inline dot-plot figures');
  // Drill in: click the first row header → its aside expands.
  findClass(host, 'vb-score-row-head')[0].dispatchEvent(makeEvent('click'));
  await settle();
  host = pageHost();
  assert(findClass(host, 'vb-score-aside').length === 1, 'an entry aside opened');
  assert(findClass(host, 'vb-aside-outcomes').length === 1, 'expectation outcomes in the aside');
  assert(findClass(host, 'vb-aside-judges').length === 1, 'per-judge detail in the aside');
  const deeper = findClass(host, 'vb-link-arrow').find((a) => hasText(a, 'transcript'));
  assert(deeper, 'a transcript drill-down link');
  deeper.dispatchEvent(makeEvent('click', { button: 0 }));
  assertEqual(bRouter.current().view, 'run', 'transcript link reaches the run dialogue (depth 3)');
});

test('theme 4 — tournament: real gauntlet ladder + matchup grid + five illustrative fixtures', async () => {
  seed();
  resetCaches();
  mountShell('#/B/tournament');
  await settle();
  const host = pageHost();
  assert(findClass(host, 'vb-fixture-gauntlet').length === 1, 'the real gauntlet ladder');
  assert(findClass(host, 'vb-gauntlet-rung').length >= 1, 'gauntlet rounds');
  assert(findClass(host, 'vb-fixture-matchup').length === 1, 'the paired matchup grid');
  // The five alternatives, each a different topology.
  assert(findClass(host, 'vb-fixture-bracket').length === 1, 'single-elim bracket');
  assert(findClass(host, 'vb-fixture-double').length === 1, 'double-elim');
  assert(findClass(host, 'vb-fixture-rr').length === 1, 'round-robin matrix');
  assert(findClass(host, 'vb-fixture-swiss').length === 1, 'swiss ledger');
  assert(findClass(host, 'vb-fixture-race').length === 1, 'racing lanes');
  // The alternatives are marked illustrative; the real gauntlet + grid are not.
  assert(findClass(host, 'vb-illustrative').length === 5, 'exactly the five alternatives are illustrative');
});

test('re-render safe: an enriched view survives a double shell paint', async () => {
  seed();
  resetCaches();
  mountShell('#/B/tournament');
  await settle();
  const before = pageHost().children.length;
  shell.renderBShell(bRouter.current());
  shell.renderBShell(bRouter.current());
  assert(pageHost().children.length >= before, 'page stays populated');
});

await run();
