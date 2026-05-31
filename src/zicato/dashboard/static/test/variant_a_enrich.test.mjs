// variant_a_enrich.test.mjs — Variant A enrichment: the four themes.
//
// Covers: candidate lifecycle (mission track + command roster), the
// sortie board (status-light tile grid), the per-board drill-down
// (instrument panel: expectations + per-judge), and the tournament-style
// match-ups (real gauntlet ladder + conceptual style switcher).
//
// Run: node test/variant_a_enrich.test.mjs   (from static/)
// Uses the shared harness DOM (no jsdom).

import { installDom, test, assert, assertEqual, run } from './harness.mjs';

installDom();
// Stub fetch so view ensure() paths never throw; presentation is driven
// off injected state, not the network.
globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);

const lifecycle = await import('../js/variants/A/components/lifecycle.js');
const sortie = await import('../js/variants/A/components/sortie.js');
const drilldown = await import('../js/variants/A/components/drilldown.js');
const matchups = await import('../js/variants/A/components/matchups.js');
const { state } = await import('../js/core/state.js');

// helper: collect every node in a subtree carrying ALL of the given
// class tokens (token match, not substring — so `mcA-sortie-tile` does
// not also match `mcA-sortie-tile-head`).
function byClass(node, frag) {
  const want = String(frag).split(/\s+/).filter(Boolean);
  return node._descendants().filter((c) => {
    if (!c.className) return false;
    const have = new Set(String(c.className).split(/\s+/));
    return want.every((w) => have.has(w));
  });
}
function withLight(node, light) {
  return node._descendants().filter((c) => c.getAttribute && c.getAttribute('data-light') === light);
}

// ===================================================================
// THEME 1 — candidate lifecycle
// ===================================================================
test('lifecycle: stations resolve born→sortie→gate→outcome for a rejected challenger', () => {
  const { stations, reached } = lifecycle.lifecycleStations({
    parentId: 'v0', genId: 'v1', isSeed: false,
    sortieFired: true, entryCount: 4, decision: 'rejected', live: false,
  });
  assertEqual(stations.length, 4, 'four stations');
  assertEqual(stations[0].key, 'born');
  assertEqual(stations[2].light, 'stop', 'gate is NO-GO (stop) on reject');
  assertEqual(stations[3].label, 'Aborted', 'outcome is a dead branch');
  assertEqual(reached, 3, 'rejected candidate reached the outcome station');
});

test('lifecycle: seed is crowned by construction (gate GO, outcome crowned)', () => {
  const { stations } = lifecycle.lifecycleStations({ genId: 'v0', isSeed: true });
  assertEqual(stations[2].light, 'go');
  assertEqual(stations[3].label, 'Crowned');
});

test('lifecycle: mission track lights the rail up to the reached station', () => {
  const { stations, reached } = lifecycle.lifecycleStations({
    parentId: 'v0', genId: 'v1', sortieFired: true, decision: 'promoted',
  });
  const track = lifecycle.missionTrack(stations, reached);
  assertEqual(track.tagName, 'DIV');
  const lit = byClass(track, 'mcA-track-rail is-lit');
  assert(lit.length >= 1, 'at least one rail segment lit: ' + lit.length);
  // a promoted candidate ends on a GO outcome dot
  assert(withLight(track, 'go').length >= 1, 'a GO light is present');
});

test('roster: champion crowned, challengers listed, dead branch dimmed', () => {
  let picked = null;
  const node = lifecycle.commandRoster({
    champion: { id: 'v0' },
    challengers: [
      { id: 'v1', parentId: 'v0', decision: 'rejected', delta: 75.71 },
      { id: 'v2', parentId: 'v0', decision: 'rejected', delta: 1.51 },
    ],
    onSelect: (id) => { picked = id; },
  });
  assert(node.textContent.includes('v0'), 'champion shown');
  assert(node.textContent.includes('Reigning champion'), 'crown banner');
  const dead = byClass(node, 'mcA-roster-row is-dead');
  assertEqual(dead.length, 2, 'both rejected rows dimmed');
  // clicking a row navigates
  const rows = byClass(node, 'mcA-roster-row');
  rows[0].dispatchEvent({ type: 'click' });
  assertEqual(picked, 'v1', 'row click fires onSelect with the call-sign');
});

test('roster: degrades gracefully with no challengers', () => {
  const node = lifecycle.commandRoster({ champion: { id: 'v0' }, challengers: [] });
  assert(node.textContent.includes('No challengers'), 'honest empty state');
});

// ===================================================================
// THEME 2 — the boards a candidate faces (sortie board)
// ===================================================================
const BOARD = [
  { id: 'waffles_single', kind: 'single_turn', input_preview: 'Make a presentation about waffles.', budget_s: 180, weight: 1, tags: ['single_turn', 'smoke'] },
  { id: 'q3_metrics_outline', kind: 'single_turn', input_preview: 'Outline a deck on quarterly metrics for Q3.', budget_s: 180, weight: 1, tags: ['single_turn'] },
  { id: 'waffles_revision_scripted', kind: 'multi_turn_scripted', input_preview: null, budget_s: 240, weight: 1, tags: ['multi'] },
  { id: 'picky_stakeholder_emulated', kind: 'multi_turn_emulated', input_preview: null, budget_s: 360, weight: 1, tags: ['multi', 'emulated'] },
];

test('sortie: lampFor maps pass / fail / timeout / unflown', () => {
  assertEqual(sortie.lampFor({ pass_fail: 1 }).light, 'go');
  assertEqual(sortie.lampFor({ pass_fail: 0 }).light, 'stop');
  assertEqual(sortie.lampFor({ wall_clock_budget_exceeded: true, pass_fail: 0 }).light, 'warn', 'timeout beats fail');
  assertEqual(sortie.lampFor({ pass_fail: null }).light, 'idle');
  assertEqual(sortie.lampFor(null).light, 'idle');
});

test('sortie: board renders one tile per entry with kind + budget + tags', () => {
  const scores = new Map([
    ['waffles_single', { entry_id: 'waffles_single', drift_loss: 60.5, pass_fail: 0, wall_clock_budget_exceeded: true, run_id: 'f318' }],
  ]);
  const node = sortie.sortieBoard({ board: BOARD, scoresById: scores, onSelect: () => {} });
  const tiles = byClass(node, 'mcA-sortie-tile');
  assertEqual(tiles.length, 4, 'one tile per board entry');
  assert(node.textContent.includes('180s'), 'budget shown');
  assert(node.textContent.includes('emulated'), 'multi_turn_emulated kind labelled');
  // the scored entry shows its loss
  assert(node.textContent.includes('60.5'), 'drift loss surfaced on the tile');
});

test('sortie: tile lamp reflects timeout as caution (warn)', () => {
  const scores = new Map([
    ['waffles_single', { entry_id: 'waffles_single', drift_loss: 60.5, pass_fail: 0, wall_clock_budget_exceeded: true }],
  ]);
  const node = sortie.sortieBoard({ board: BOARD, scoresById: scores });
  // the warn lamp exists somewhere in the grid
  assert(withLight(node, 'warn').length >= 1, 'a caution lamp is present for the timeout');
});

test('sortie: clicking a tile fires onSelect with the entry', () => {
  let got = null;
  const node = sortie.sortieBoard({ board: BOARD, onSelect: (entry) => { got = entry.id; } });
  const tiles = byClass(node, 'mcA-sortie-tile');
  tiles[1].dispatchEvent({ type: 'click' });
  assertEqual(got, 'q3_metrics_outline', 'tile click hands back the entry id');
});

test('sortie: tally counts by lamp', () => {
  const scores = new Map([
    ['waffles_single', { pass_fail: 0 }],
    ['q3_metrics_outline', { pass_fail: 1 }],
    ['picky_stakeholder_emulated', { wall_clock_budget_exceeded: true, pass_fail: 0 }],
  ]);
  const node = sortie.sortieTally(BOARD, scores);
  const txt = node.textContent;
  assert(txt.includes('4 entries'), 'total count');
  assert(txt.includes('pass') && txt.includes('fail') && txt.includes('timeout'), 'segments labelled');
});

test('sortie: empty board degrades, does not throw', () => {
  const node = sortie.sortieBoard({ board: [] });
  assert(node.textContent.includes('No board entries'), 'honest empty');
});

// ===================================================================
// THEME 3 — per-board scoring drill-down (instrument panel)
// ===================================================================
test('drilldown: expectations render pass/fail marks + detail', () => {
  const node = drilldown.expectationsBlock({
    outcomes: [{ kind: 'predicate', passed: false, detail: 'predicate returned False', judge_name: null, score: null }],
  });
  assert(node.textContent.includes('predicate'), 'kind shown');
  assert(node.textContent.includes('returned False'), 'detail shown');
  assert(withLight(node, 'stop').length >= 1, 'a fail mark is red');
});

test('drilldown: per-judge bars render weighted loss', () => {
  const node = drilldown.perJudgeBars({
    judges: [{ judge_name: 'incorporates_feedback', weighted_loss: 27.0, raw_loss: 27.0, run_count: 1, weight: 1.0 }],
  });
  assert(node.textContent.includes('incorporates_feedback'), 'judge name');
  assert(node.textContent.includes('27.0'), 'weighted loss value');
  assert(byClass(node, 'mcA-bar-fill').length >= 1, 'a loss bar rendered');
});

test('drilldown: null payloads show loading, not a crash', () => {
  assert(drilldown.expectationsBlock(null).textContent.length > 0, 'expectations loading state');
  assert(drilldown.perJudgeBars(null).textContent.length > 0, 'per-judge loading state');
});

test('drilldown: instrument panel wires the run-transcript deep link', () => {
  let opened = null, closed = false;
  const node = drilldown.instrumentPanel({
    entry: { id: 'waffles_single', kind: 'single_turn', budget_s: 180 },
    score: { drift_loss: 60.5, pass_fail: 0, run_id: 'f318d5ae' },
    expectations: { outcomes: [{ kind: 'predicate', passed: false, detail: 'x' }] },
    perJudge: { judges: [] },
    runId: 'f318d5ae',
    onOpenRun: (rid) => { opened = rid; },
    onClose: () => { closed = true; },
  });
  assert(node.textContent.includes('waffles_single'), 'entry titled');
  // the deep-link button
  const btns = node._descendants().filter((c) => c.tagName === 'BUTTON' && c.textContent.includes('transcript'));
  assertEqual(btns.length, 1, 'one open-transcript button');
  btns[0].dispatchEvent({ type: 'click' });
  assertEqual(opened, 'f318d5ae', 'clicking opens the run by id (depth 3)');
  // the close button
  const close = node._descendants().find((c) => c.className && c.className.includes('mcA-drill-close'));
  close.dispatchEvent({ type: 'click' });
  assert(closed, 'close fires onClose');
});

// ===================================================================
// THEME 4 — match-ups across tournament styles
// ===================================================================
const MATCHUPS = [
  { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 75.71, rejection_reason: 'challenger regressed: loss rose by 75.71', hypothesis_core_idea: 'Enforce explicit slide-structure output' },
  { champion: 'v0', challenger: 'v2', decision: 'rejected', delta_scalar: 1.51, rejection_reason: 'narrow' },
];

test('matchups: real gauntlet ladder crowns the king and lists rungs', () => {
  const node = matchups.gauntletLadder({ champion: 'v0', matchups: MATCHUPS, grids: new Map() });
  assert(node.textContent.includes('king of the hill'), 'king banner');
  assert(node.textContent.includes('v0'), 'champion id');
  const rungs = byClass(node, 'mcA-ladder-rung');
  assertEqual(rungs.length, 2, 'one rung per matchup');
  assert(node.textContent.includes('champion holds'), 'both rejected → champion holds');
});

test('matchups: expanding a rung asks for its per-board duel', () => {
  let asked = null;
  const node = matchups.gauntletLadder({
    champion: 'v0', matchups: MATCHUPS, grids: new Map(),
    expanded: null, onSelectGrid: (id) => { asked = id; },
  });
  const heads = byClass(node, 'mcA-ladder-rung-head');
  heads[0].dispatchEvent({ type: 'click' });
  assertEqual(asked, 'v1', 'clicking a rung head requests that challenger duel');
});

test('matchups: an expanded rung renders the paired per-board duel', () => {
  const grids = new Map([['v1', [
    { entry_id: 'q3_metrics_outline', parent_drift_loss: 71.0, child_drift_loss: 63.5, delta: -7.5, verdict: 'improved', won_by: 'v1' },
    { entry_id: 'picky_stakeholder_emulated', parent_drift_loss: 105.5, child_drift_loss: 642.5, delta: 537.0, verdict: 'regressed', won_by: 'v0' },
  ]]]);
  const node = matchups.gauntletLadder({ champion: 'v0', matchups: MATCHUPS, grids, expanded: 'v1' });
  assert(node.textContent.includes('q3_metrics_outline'), 'duel lists board entries');
  assert(byClass(node, 'mcA-duel-row').length === 2, 'two duel rows');
  assert(byClass(node, 'mcA-duel-fill is-won').length >= 1, 'the winning side is highlighted');
});

test('matchups: style switcher exposes the real gauntlet + four conceptual styles', () => {
  let picked = null;
  const node = matchups.styleSwitcher('gauntlet', (k) => { picked = k; });
  const btns = node._descendants().filter((c) => c.tagName === 'BUTTON');
  assertEqual(btns.length, 5, 'gauntlet + single/double/swiss/racing');
  // the active (gauntlet) is marked real
  const active = btns.find((b) => b.className.includes('is-active'));
  assert(active.className.includes('is-real'), 'gauntlet flagged as the real run');
  btns[2].dispatchEvent({ type: 'click' });
  assert(picked && picked !== 'gauntlet', 'picking a style fires onPick: ' + picked);
});

const CANDS = [
  { id: 'v0', scalar: 70.94, role: 'champion' },
  { id: 'v1', scalar: 146.65, role: 'challenger' },
  { id: 'v2', scalar: 72.45, role: 'challenger' },
];

test('matchups: each conceptual style is labelled conceptual + a distinct topology', () => {
  for (const style of ['single_elim', 'double_elim', 'swiss', 'racing']) {
    const node = matchups.styleView(style, CANDS);
    assert(node.textContent.includes('CONCEPTUAL'), style + ' carries the conceptual label');
  }
  // single-elim → an SVG bracket tree
  const se = matchups.styleView('single_elim', CANDS);
  assert(se._descendants().some((c) => c.tagName === 'SVG'), 'single-elim is an SVG bracket');
  // double-elim → winners + losers rails
  const de = matchups.styleView('double_elim', CANDS);
  assert(de.textContent.includes('winners') && de.textContent.includes('losers'), 'double-elim has both rails');
  // swiss → a pairing table
  const sw = matchups.styleView('swiss', CANDS);
  assert(sw._descendants().some((c) => c.tagName === 'TABLE'), 'swiss is a pairing table');
  // racing → lanes + a cut-line
  const ra = matchups.styleView('racing', CANDS);
  assert(byClass(ra, 'mcA-race-lane').length === 3, 'one race lane per candidate');
  assert(ra.textContent.includes('cut-line'), 'racing shows an elimination cut');
});

test('matchups: racing eliminates lanes past the cut-line', () => {
  const node = matchups.styleView('racing', CANDS);
  // v1 (146.65) is the worst → past the median cut → eliminated.
  const out = byClass(node, 'mcA-race-lane is-out');
  assert(out.length >= 1, 'at least one lane dropped past the cut');
});

// ===================================================================
// View integration — the enriched experiment + tournament views render
// off injected state without innerHTML (no flash) and surface the themes.
// ===================================================================
test('experiment view: surfaces the lifecycle track + the sortie board', async () => {
  const { renderExperiment, resetExperimentCache } = await import('../js/variants/A/views/experiment.js');
  resetExperimentCache();
  state.epochDef = {
    epoch_id: '2026-05-30_e0',
    board: BOARD,
    experiments: [
      { generation_id: 'v0', parent_generation_id: null, outcome: null },
      { generation_id: 'v1', parent_generation_id: 'v0',
        hypothesis: { core_idea: 'Enforce slide structure.' },
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 75.71, drift_loss_delta: 75.0, pass_rate_delta: 0 } },
    ],
  };
  const host = document.createElement('div');
  renderExperiment(host, { epochId: '2026-05-30_e0', genId: 'v1' }, () => {});
  const text = host.textContent;
  assert(text.includes('Candidate lifecycle'), 'lifecycle track present');
  assert(text.includes('Sortie board'), 'sortie board present');
  assert(text.includes('born') || text.includes('Born'), 'lifecycle station labelled');
  assertEqual(host.innerHTMLWriteCount(), 0, 'no innerHTML / no flash path');
});

test('tournament view: surfaces the command roster + the match-up theatre', async () => {
  const { renderTournament, resetTournamentCache } = await import('../js/variants/A/views/tournament.js');
  resetTournamentCache();
  state.epochDef = {
    epoch_id: '2026-05-30_e0',
    board: BOARD,
    experiments: [
      { generation_id: 'v0', parent_generation_id: null, outcome: null },
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { tournament_decision: 'rejected', scalar_score_delta: 75.71, scalar_score: 146.65 } },
      { generation_id: 'v2', parent_generation_id: 'v0', outcome: { tournament_decision: 'rejected', scalar_score_delta: 1.51, scalar_score: 72.45 } },
    ],
  };
  state.heartbeat = { epoch_id: '2026-05-30_e0' };
  state.activeTournament = null;
  const host = document.createElement('div');
  renderTournament(host, { epochId: '2026-05-30_e0' }, () => {});
  const text = host.textContent;
  assert(text.includes('Command roster'), 'roster present');
  assert(text.includes('Match-up theatre'), 'match-up theatre present');
  assert(text.includes('Gauntlet') || text.includes('gauntlet'), 'gauntlet framing present');
  assertEqual(host.innerHTMLWriteCount(), 0, 'no innerHTML / no flash path');
});

await run();
