// test/variant_t_live_waves.test.mjs — Variant T ("Console IV") unit tests:
// the live-run UX wave, the consolidation wave (live tournament
// truthfulness), and integration wave 8 (match-grouped block, tree pulse,
// elim generations-across-rounds).
//
// Split mechanically from the former variant_t.test.mjs (assertions
// verbatim); shared fixtures + helpers live in ./fixtures.mjs.

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';

installDom();

const {
  router, svg, data, tree, livestatus, coreState,
  rounds, dag, live, STRUCT, EPOCH_ID, freshHb,
  freshState, allByClass, svgsByClass, mountLiveShell, SE_STRUCT, RACING_STRUCT,
  installFixtureMap, liveRacingField, liveElimField, HERO_EPOCH,
} = await import('./fixtures.mjs');
const mock = await import('./mock_server.mjs');

// ====================================================================
// LIVE-RUN UX wave — five coordinated fixes on the live/structure/svg surface.
// ====================================================================

// (1) proportional trellis glyphs — the round status marks render as TRUE
// circles (equal x/y scale) even when the bars stretch to fill a WIDE cell.
test('svg.genDots: status glyphs are FIXED-ASPECT (1:1 viewBox, equal x/y) — round, not stretched, at a wide card', () => {
  // a WIDE card: the row is an HTML flex container (no non-uniform SVG stretch),
  // each glyph a 1:1-aspect SVG so the mark keeps equal x/y scale.
  const row = svg.genDots({ width: 800, height: 14, cells: [
    { label: 'v0', pass: 1, ran: true }, { label: 'v1', pass: 0, ran: true },
    { label: 'v2', timeout: true, ran: true }, { label: 'v3', ran: false },
  ] });
  // the row spans the full width (flex, width:100%) but is NOT itself a
  // preserveAspectRatio:'none' svg (which is what sheared the old glyphs).
  assert(row.localName !== 'svg', 'genDots returns an HTML flex row, not a stretched svg');
  const glyphs = svgsByClass(row, 'dn-glyph');
  assertEqual(glyphs.length, 4, 'one fixed-aspect glyph svg per candidate');
  for (const g of glyphs) {
    const vb = (g.getAttribute('viewBox') || '').split(/\s+/).map(Number);
    assertEqual(vb[2], vb[3], 'the glyph viewBox is SQUARE (1:1) → equal x/y scale → a true circle');
    assertEqual(g.getAttribute('preserveAspectRatio'), 'xMidYMid meet', 'the glyph keeps its aspect (no shear)');
    assertEqual(g.getAttribute('width'), g.getAttribute('height'), 'the glyph is painted at a 1:1 box');
  }
});

test('svg.sparkbar: bars still SPAN the width (stretch) but the verdict glyph is a SEPARATE fixed-aspect overlay (true triangle)', () => {
  const node = svg.sparkbar({ width: 800, height: 30, verdict: 'promoted', bars: [
    { label: 'v0', value: 10 }, { label: 'v1', value: 20 }, { label: 'v2', value: 5 },
  ] });
  // a wrapper holds the stretched bars + the fixed-aspect glyph.
  assert(node.localName !== 'svg', 'a verdict sparkbar returns a positioning wrapper (bars + glyph)');
  const bars = svgsByClass(node, 'dn-sparkbar')[0];
  assert(bars, 'the bars layer is present');
  assertEqual(bars.getAttribute('preserveAspectRatio'), 'none', 'the BARS still fill the cell width (stretch is fine for rectangles)');
  assertEqual(bars.getAttribute('width'), '100%', 'the bars span the full card width');
  const glyph = svgsByClass(node, 'dn-sparkbar-verdict')[0];
  assert(glyph, 'the verdict glyph rides in its own overlay svg');
  const vb = (glyph.getAttribute('viewBox') || '').split(/\s+/).map(Number);
  assertEqual(vb[2], vb[3], 'the verdict glyph viewBox is SQUARE → a true (un-sheared) triangle');
  assertEqual(glyph.getAttribute('width'), glyph.getAttribute('height'), 'the verdict glyph is a 1:1 box');
});

// (2) candidate page + trellis are live-aware (current-epoch-scoped, digest-gated).
const LIVE_UX_EPOCH = '2026-06-02_e9';
function liveUxFixture() {
  return {
    '/api/epoch': {
      epoch_id: LIVE_UX_EPOCH, closed: false, goal: 'g',
      tournament: { structure: 'swiss', params: { rounds: 3 } },
      experiments: [
        { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
        { generation_id: 'v1', parent_generation_id: 'v0', outcome: {} },
      ],
      board: [
        { entry_id: 'b0', kind: 'single_turn', budget_s: 180, weight: 1 },
        { entry_id: 'b1', kind: 'single_turn', budget_s: 180, weight: 1 },
      ],
    },
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: LIVE_UX_EPOCH, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: LIVE_UX_EPOCH, parent_generation_id: 'v0', promoted: null },
    ] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 50 }] },
    '/api/tournaments': { epoch_id: LIVE_UX_EPOCH, champion_lineage: ['v0'], matchups: [] },
    [`/api/generation/${LIVE_UX_EPOCH}/v0/per-entry`]: { entries: [{ entry_id: 'b0', run_id: 'r0', drift_loss: 40, pass_fail: true }] },
    [`/api/generation/${LIVE_UX_EPOCH}/v1/per-entry`]: { entries: [] },
  };
}

test('candidate page (LIVE): in-flight board runs for THIS candidate show "N running" with progress; foreign-epoch runs ignored; structure-aware pending label', async () => {
  freshState();
  installFixtureMap(liveUxFixture());
  const candidate = await import('../js/views/candidate.js');
  // a CURRENT-epoch run in flight on v1 (swiss).
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: LIVE_UX_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b1', run_id: 'rr1', progress: 0.5, epoch_id: LIVE_UX_EPOCH }];
  coreState.state.activeTournament = { epoch_id: LIVE_UX_EPOCH, structure: 'swiss', phase: 'running' };

  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH, gen: 'v1' });
  assert(allByClass(host, 'dn-board-inflight')[0], 'the candidate shows its in-flight board run card');
  assert(/board running|boards running/.test(host.textContent), 'reads "N board(s) running"');
  assert(/50%/.test(host.textContent), 'the in-flight board shows its progress (50%)');
  // a swiss candidate awaiting the gate must NOT read "racing".
  assert(!/⋯ racing/.test(host.textContent), 'the pending terminal label is structure-aware (swiss → not "racing")');
  assert(/⋯ competing/.test(host.textContent), 'a swiss candidate reads "⋯ competing"');

  // FOREIGN-epoch run must NOT light up this candidate.
  freshState();
  installFixtureMap(liveUxFixture());
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: 'some_other_epoch' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b1', run_id: 'rr1', progress: 0.5 }];
  coreState.state.activeTournament = { epoch_id: 'some_other_epoch', structure: 'swiss', phase: 'running' };
  const host2 = document.createElement('div');
  await candidate.render(host2, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH, gen: 'v1' });
  assertEqual(allByClass(host2, 'dn-board-inflight').length, 0, 'a FOREIGN-epoch run does not light up this candidate');

  coreState.state.heartbeat = { phase: 'idle' }; coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('board trellis (LIVE): in-flight cells light up from state.activeRuns (current epoch); a no-op beat does NOT rebuild; foreign epoch ignored', async () => {
  freshState();
  installFixtureMap(liveUxFixture());
  const boards = await import('../js/views/boards.js');
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: LIVE_UX_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b1', run_id: 'rr1', progress: 0.4, epoch_id: LIVE_UX_EPOCH }];
  coreState.state.activeTournament = { epoch_id: LIVE_UX_EPOCH, structure: 'swiss', phase: 'running' };

  const host = document.createElement('div');
  await boards.render(host, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH });
  const lit = allByClass(host, 'dn-trellis-live');
  assertEqual(lit.length, 1, 'exactly the in-flight entry (b1) cell lights up');
  assert(/running/.test(host.textContent), 'the lit cell carries an in-flight "running" tag');
  const digestAfterFirst = host.getAttribute('data-t-digest');

  // a NO-OP beat (identical live state) must NOT rebuild the trellis DOM.
  const trellisBefore = allByClass(host, 'dn-trellis')[0];
  const firstChildBefore = host.firstChild;
  const writesBefore = host.innerHTMLWriteCount();
  await boards.render(host, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH });
  assertEqual(host.getAttribute('data-t-digest'), digestAfterFirst, 'a no-op beat leaves the digest unchanged');
  assert(allByClass(host, 'dn-trellis')[0] === trellisBefore, 'a no-op beat does NOT rebuild the trellis (node identity preserved)');
  // §9.15-step-7 no-op identity: the renderView scaffold must not clear-and-rebuild.
  assert(host.firstChild === firstChildBefore, 'no clear-and-rebuild on the no-op beat (host firstChild identity)');
  assertEqual(host.innerHTMLWriteCount(), writesBefore, 'no innerHTML writes on the no-op beat');

  // FOREIGN-epoch run ignored — no lit cell.
  freshState();
  installFixtureMap(liveUxFixture());
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: 'foreign_e' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b1', run_id: 'rr1', progress: 0.4 }];
  coreState.state.activeTournament = { epoch_id: 'foreign_e', structure: 'swiss', phase: 'running' };
  const host2 = document.createElement('div');
  await boards.render(host2, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH });
  assertEqual(allByClass(host2, 'dn-trellis-live').length, 0, 'a foreign-epoch run does not light up the trellis');

  coreState.state.heartbeat = { phase: 'idle' }; coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// (3) Match-ups live swiss — the ACTIVE round's pairings show in-flight progress
// (NOT "being seeded"); "being seeded" only with no competitors.
test('Match-ups (LIVE swiss): active-round pairings show in-flight board progress (NOT "being seeded"); accumulating points; seeded-empty only with no field', async () => {
  freshState();
  const F = liveUxFixture();
  F['/api/active-tournament'] = {
    epoch_id: LIVE_UX_EPOCH, structure: 'swiss', phase: 'running', structure_params: { rounds: 3, board_size: 4 },
    competitors: [
      { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
      { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
    ],
    rounds: [
      { round_index: 0, label: 'Round 1', matches: [
        { match_id: 'sw_r0_m0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'win' },
        { match_id: 'sw_r0_m1', competitors: ['v2', 'v3'] },  // in flight
      ] },
    ],
    standings: [], champion_lineage: ['v0'],
  };
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v2', epoch_id: LIVE_UX_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v2', entry_id: 'b0', run_id: 'rr', progress: 1, epoch_id: LIVE_UX_EPOCH }];
  coreState.state.activeTournament = F['/api/active-tournament'];

  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH });
  assert(allByClass(host, 'dt-live-pill')[0], 'the live pill is shown');
  assert(svgsByClass(host, 'dn-swissladder')[0], 'the live swiss ladder renders (NOT a being-seeded empty)');
  assert(!/being seeded/i.test(host.textContent), 'NOT "being seeded" once the field + active round exist');
  // the in-flight pairing reads its board progress (running) INSIDE the ladder
  // (the dense tables were collapsed away) — a live progress bar + "running".
  assert(svgsByClass(host, 'dn-swissladder')[0] && allByClass(host, 'dn-swissladder-bar-live')[0], 'the in-flight pairing shows a live progress bar in the ladder');
  assert(/running/.test(host.textContent), 'the active pairing reads "running"');
  // a decided pairing's winner accumulates a Copeland point (v1 beat v0).
  assert(host.textContent.includes('v1'), 'the decided pairing winner (v1) is shown');

  // "being seeded" ONLY when there is NO competitor/round yet.
  freshState();
  const F2 = liveUxFixture();
  F2['/api/active-tournament'] = { epoch_id: LIVE_UX_EPOCH, structure: 'swiss', phase: 'running', structure_params: { rounds: 3 }, competitors: [], rounds: [], standings: [] };
  installFixtureMap(F2);
  // a genuinely-live just-started run carries a FRESH heartbeat (the staleness
  // gate now reads a no-timestamp heartbeat as stale ⇒ not live).
  coreState.state.setHeartbeat(freshHb({ phase: 'tournament:round_0', generation_id: '', epoch_id: LIVE_UX_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = F2['/api/active-tournament'];
  const host2 = document.createElement('div');
  await gens.render(host2, { navigate() {}, href: router.href }, { epochId: LIVE_UX_EPOCH });
  assert(/being seeded|run is starting|fills in/i.test(host2.textContent), '"being seeded"/starting shows ONLY when no competitor/round exists yet');

  coreState.state.heartbeat = { phase: 'idle' }; coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// (4) the live hero BLOOMS from the proposed field → the live ladder once the
// tournament is running (proposing tracker is the SEED of the ladder).
test('live hero (BLOOM): a RUNNING swiss with the applied field as competitors (no round scored yet) shows the live LADDER, not the proposing tracker', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  // the tournament has STARTED (running) — the applied field (v1..v3) are
  // competitors but no pairing has scored yet. The hero must BLOOM into the
  // ladder seeded by these competitors, not stay on the proposing tracker.
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = {
    structure: 'swiss', phase: 'running', epoch_id: HERO_EPOCH, structure_params: { rounds: 3 },
    competitors: [
      { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
      { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
    ],
    rounds: [], standings: [], champion_lineage: ['v0'],
    field_status: [
      { generation_id: 'v1', status: 'applied', seed: 2 },
      { generation_id: 'v2', status: 'applied', seed: 3 },
      { generation_id: 'v3', status: 'applied', seed: 4 },
    ],
  };

  const root = mountLiveShell('#/');
  assert(svgsByClass(root, 'dn-swissladder')[0], 'the hero BLOOMED into the live swiss ladder (applied field → competitors)');
  assertEqual(allByClass(root, 'dn-prop-tracker').length, 0, 'the proposing tracker is REPLACED by the ladder once the tournament runs');
  assertEqual(allByClass(root, 'dt-live-hero-nofunnel').length, 0, 'not the bland placeholder');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// (5) the lifecycle DAG's pending terminal label is STRUCTURE-AWARE.
test('lifecycle DAG: the pending terminal label is structure-aware (swiss → "⋯ competing", elim → "⋯ in bracket", racing → "⋯ racing", unknown → "⋯ awaiting gate")', () => {
  const entries = [{ entry_id: 'b0', drift_loss: 10, pass_fail: true }];
  const swiss = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', decision: 'pending', entries, structure: 'swiss' });
  assert(swiss.textContent.includes('⋯ competing'), 'a pending swiss candidate reads "⋯ competing"');
  assert(!swiss.textContent.includes('⋯ racing'), 'a pending swiss candidate does NOT read "racing"');

  const elim = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', decision: 'pending', entries, structure: 'single_elim' });
  assert(elim.textContent.includes('⋯ in bracket'), 'a pending elim candidate reads "⋯ in bracket"');

  const racing = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', decision: 'pending', entries, structure: 'racing' });
  assert(racing.textContent.includes('⋯ racing'), 'a pending racing candidate still reads "⋯ racing"');

  const unknown = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', decision: 'pending', entries });
  assert(unknown.textContent.includes('⋯ awaiting gate'), 'an unknown structure degrades to "⋯ awaiting gate"');
});

// ====================================================================
// CONSOLIDATION WAVE — live tournament truthfulness + visual consistency.
// ====================================================================

// ---- Task 1: live bracket / ladder / funnel fill from PUBLISHED rounds ----

test('live elim model: PUBLISHED single_elim rounds render the bracket (not "being seeded") with active-runs progress overlaid', () => {
  const at = {
    structure: 'single_elim', phase: 'running', epoch_id: HERO_EPOCH,
    structure_params: { board_size: 4 }, round_index: 0,
    competitors: [
      { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
      { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
    ],
    rounds: [
      { round_index: 0, label: 'Semifinal', matches: [
        { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], bracket_slot: 'WB-R0-0', winner: null, pending: true },
        { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], bracket_slot: 'WB-R0-1', winner: null, pending: true },
      ] },
    ],
    standings: [], champion_lineage: ['v0'],
  };
  const model = STRUCT.buildLiveElimModel({
    at, heartbeat: { phase: 'tournament:round_0', generation_id: 'v1' },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.5 }],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  });
  assert(model && model.live, 'a live elim model built from the published rounds');
  const m = STRUCT.elimModel(model);
  assert(m.hasMatches, 'the published round has matches');
  const active = m.winners[0].matches.find((mm) => (mm.competitors || []).includes('v1'));
  assert(active && active.pending, 'the active published match is pending');
  assert(active.inflight >= 1 || active.done >= 1, 'active-runs progress is overlaid onto the pending match');
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, HERO_EPOCH);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  assert(svgsByClass(host, 'dn-elimflow')[0], 'the bracket-as-flow SVG rendered from the published rounds');
  assert(!/being seeded/i.test(host.textContent), 'NOT the "being seeded" state once the rounds are published');
});

// ---- Task 1: the candidate page's match-ups populate from LIVE rounds ----

test('candidate match-ups: a candidate running its first round populates from the LIVE published rounds (NOT "did not run in any round")', async () => {
  freshState();
  const CM_EPOCH = '2026-06-02_cm';
  const F = {
    '/api/epoch': { epoch_id: CM_EPOCH, closed: false, goal: 'g',
      tournament: { structure: 'single_elim', params: { board_size: 4 } },
      experiments: [
        { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
        { generation_id: 'v1', parent_generation_id: 'v0', outcome: {} },
      ], board: [] },
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: CM_EPOCH, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: CM_EPOCH, parent_generation_id: 'v0', promoted: null },
    ] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 50 }] },
    // the COMPLETED bracket feed is EMPTY — nothing has committed yet.
    '/api/tournaments': { epoch_id: CM_EPOCH, champion_lineage: ['v0'], matchups: [], tournaments: [] },
    [`/api/generation/${CM_EPOCH}/v0/per-entry`]: { entries: [{ entry_id: 'b0', run_id: 'r0', drift_loss: 40, pass_fail: true }] },
    [`/api/generation/${CM_EPOCH}/v1/per-entry`]: { entries: [{ entry_id: 'b0', run_id: 'r1', drift_loss: 38, pass_fail: true, match_id: 'WB-R0-0' }] },
  };
  installFixtureMap(F);
  // a LIVE run for THIS epoch: the published rounds carry v0 vs v1 in flight.
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: CM_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5, epoch_id: CM_EPOCH }];
  coreState.state.activeTournament = {
    epoch_id: CM_EPOCH, structure: 'single_elim', phase: 'running',
    rounds: [{ round_index: 0, label: 'Semifinal', matches: [
      { match_id: 'WB-R0-0', competitors: ['v0', 'v1'], bracket_slot: 'WB-R0-0', winner: null, pending: true },
    ] }],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' }],
  };

  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: CM_EPOCH, gen: 'v1' });
  assert(!/did not run in any tournament round/i.test(host.textContent),
    'a live candidate is NOT reported as "did not run in any round" while it is plainly racing');
  assert(host.textContent.includes('v0 → v1') || /v0.*v1/.test(host.textContent),
    'the live match-up (v0 → v1) populates the candidate match-ups table');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// ---- Task 2: structure-correct live status (no "racing" for elim) ----

test('live status mapper: structure-correct labels — elim → "in bracket"/"competing", swiss → "playing", racing → "racing"; terminals pass through', () => {
  assertEqual(livestatus.structureStatusLabel('competing', 'single_elim'), 'in bracket', 'elim in-contention reads "in bracket"');
  assertEqual(livestatus.structureStatusLabel('competing', 'double_elim'), 'in bracket', 'double-elim too');
  assertEqual(livestatus.structureStatusLabel('competing', 'swiss'), 'playing', 'swiss in-contention reads "playing"');
  assertEqual(livestatus.structureStatusLabel('competing', 'racing'), 'racing', 'racing in-contention reads "racing"');
  // terminals + alive pass through in EVERY structure.
  for (const st of ['single_elim', 'swiss', 'racing']) {
    assertEqual(livestatus.structureStatusLabel('champion', st), 'champion', 'champion passes through (' + st + ')');
    assertEqual(livestatus.structureStatusLabel('eliminated', st), 'eliminated', 'eliminated passes through (' + st + ')');
    assertEqual(livestatus.structureStatusLabel('alive', st), 'alive', 'alive passes through (' + st + ')');
  }
});

test('standings table (LIVE elim): a mid-run champion/eliminated standing is NOT mislabeled "racing" — uses the elim word "in bracket"', () => {
  const st = STRUCT.normalizeStructure({
    structure: 'single_elim', phase: 'running',
    rounds: [{ round_index: 0, label: 'Semifinal', matches: [
      { match_id: 'WB-R0-0', competitors: ['v0', 'v1'], bracket_slot: 'WB-R0-0', winner: null, pending: true },
    ] }],
    standings: [
      { generation_id: 'v0', rank: 1, scalar: 40, wins: 1, losses: 0, status: 'champion' },
      { generation_id: 'v1', rank: 2, scalar: 45, wins: 0, losses: 1, status: 'eliminated' },
    ],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' }],
  }, true);
  const nodes = STRUCT.renderStructure(st, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const standings = allByClass(host, 'dt-standings')[0];
  assert(standings, 'the standings table rendered');
  assert(!/racing/.test(standings.textContent), 'a LIVE elim standings table NEVER reads "racing"');
  assert(/in bracket/.test(standings.textContent), 'a LIVE elim in-contention standing reads "in bracket"');
});

// ---- Task 3: cached-champion badge from provenance ----

test('cached champion: per-entry cached/source_epoch surfaces a "cached · from <epoch>" badge + a fast eval-mode tag (no "no entries scored")', async () => {
  freshState();
  const CC_EPOCH = '2026-06-02_cc';
  const F = {
    '/api/epoch': { epoch_id: CC_EPOCH, closed: true, goal: 'g',
      tournament: { structure: 'racing', params: {} },
      experiments: [{ generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } }], board: [] },
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: CC_EPOCH, parent_generation_id: '', promoted: true },
    ] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 50 }] },
    '/api/tournaments': { epoch_id: CC_EPOCH, champion_lineage: ['v0'], matchups: [], tournaments: [] },
    // the champion v0's per-board results are CACHED from a prior epoch.
    [`/api/generation/${CC_EPOCH}/v0/per-entry`]: { entries: [
      { entry_id: 'b0', run_id: 'r0', drift_loss: 40, pass_fail: true, cached: true, source_epoch: '2026-06-01_e0', source_run: 'run_prior' },
    ] },
  };
  installFixtureMap(F);
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: CC_EPOCH, gen: 'v0' });
  assert(/cached/i.test(host.textContent), 'a cached champion shows a "cached" badge');
  assert(/2026-06-01_e0/.test(host.textContent), 'the badge names the source epoch');
  assert(/fast — champion reused/.test(host.textContent), 'a fast-mode header tag reads "fast — champion reused"');
  assert(!/no per-entry scores|no board entries scored/i.test(host.textContent),
    'a cached champion does NOT read "no board entries scored"');
});

// ---- Task 4: objective falls back to the brief H1 title ----

test('epoch objective: falls back to the brief H1 title (stripping "Epoch eN — ") when no explicit goal', async () => {
  const epoch = await import('../js/views/epoch.js');
  // explicit goal wins.
  assertEqual(epoch.objectiveText({ goal: 'crisper slides', brief: '# Epoch e3 — Tighten oversight\n' }), 'crisper slides');
  // no goal → the brief H1 title, prefix stripped.
  assertEqual(epoch.objectiveText({ goal: '', brief: '# Epoch e3 — Tighten oversight\n\n## Goal\nx' }), 'Tighten oversight');
  assertEqual(epoch.objectiveText({ goal: null, brief: '# Reduce hallucination\n' }), 'Reduce hallucination');
  // an H2 is NOT a title; with no H1 and no goal → the honest placeholder.
  assertEqual(epoch.objectiveText({ goal: '', brief: '## Goal\nx' }), '(no objective recorded)');
  assertEqual(epoch.objectiveText({ goal: '', brief: '' }), '(no objective recorded)');
  // a colon-separated prefix is also stripped.
  assertEqual(epoch.briefTitle('# Epoch 2026-06-02_e1: Add a judge'), 'Add a judge');
});

// ---- Task 5: "field of N" excludes unscored orphans ----

test('epoch overview: "field of N" counts champion + applied challengers, EXCLUDING unscored orphans', async () => {
  freshState();
  const FN_EPOCH = '2026-06-02_fn';
  const F = {
    '/api/epoch': { epoch_id: FN_EPOCH, closed: false, goal: 'g',
      tournament: { structure: 'swiss', params: { rounds: 3 } },
      experiments: [], board: [] },
    '/api/lineage': { generations: [
      { generation_id: 'v0', epoch_id: FN_EPOCH, parent_generation_id: '', promoted: true },
      { generation_id: 'v1', epoch_id: FN_EPOCH, parent_generation_id: 'v0', promoted: false },
      { generation_id: 'v2', epoch_id: FN_EPOCH, parent_generation_id: 'v0', promoted: false },
      // v9 is an UNSCORED ORPHAN — proposed but never entered the tournament.
      { generation_id: 'v9', epoch_id: FN_EPOCH, parent_generation_id: 'v0', promoted: null },
    ] },
    // v0/v1/v2 scored; v9 has NO scalar (orphan).
    '/api/score-trajectory': { points: [
      { generation_id: 'v0', scalar: 50 }, { generation_id: 'v1', scalar: 60 }, { generation_id: 'v2', scalar: 55 },
    ] },
    '/api/tournaments': { epoch_id: FN_EPOCH, champion_lineage: ['v0'], matchups: [], tournaments: [] },
    [`/api/generation/${FN_EPOCH}/v0/per-entry`]: { entries: [] },
    [`/api/generation/${FN_EPOCH}/v1/per-entry`]: { entries: [] },
    [`/api/generation/${FN_EPOCH}/v2/per-entry`]: { entries: [] },
    [`/api/generation/${FN_EPOCH}/v9/per-entry`]: { entries: [] },
  };
  installFixtureMap(F);
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: FN_EPOCH });
  // the real field is the challenger fan {v1, v2} (v0 is the carried champion on
  // the spine); the unscored orphan v9 is EXCLUDED from the minted field.
  const chips = allByClass(host, 'dn-roundtl-chip').map((c) => { const mono = allByClass(c, 'dn-mono')[0]; return mono ? (mono.textContent || '').trim() : ''; });
  assertDeep(chips.filter((s, i) => chips.indexOf(s) === i).sort(), ['v1', 'v2'], 'the field is {v1,v2} — the unscored orphan v9 is excluded');
  assert(!chips.includes('v9'), 'the orphan v9 is NOT a minted-field chip');
});

// ---- Task 6: crown glyph is ♛ for current / ♔ for former everywhere ----

test('crown glyphs: the shared CROWN constant is ♛ current / ♔ former; no ♚ is emitted by any gate label', () => {
  assertEqual(svg.CROWN.current, '♛', 'the current-champion crown is ♛');
  assertEqual(svg.CROWN.former, '♔', 'the former-champion crown is ♔');

  // a crowned racing gate (survival funnel) shows ♛, never ♚.
  const rungs = [{ label: 'Rung 0', match_id: 'rung0', competitors: ['v1', 'v2'], survivors: ['v1'], cut: ['v2'], board_fraction: 0.5 }];
  const funnel = svg.survivalFunnel({ rungs, championId: 'v1', benchmarkId: 'v0', gateState: 'crowned', gateDelta: -2 });
  assert(funnel.textContent.includes('♛'), 'a crowned funnel gate emits ♛');
  assert(!funnel.textContent.includes('♚'), 'a crowned funnel gate does NOT emit ♚');

  // a crowned elim bracket-as-flow gate.
  const winners = [{ round_index: 0, label: 'Final', matches: [{ match_id: 'WB-R0-0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted', bracket_slot: 'WB-R0-0' }] }];
  const servedCrown = mock.deriveElimStates(winners);
  const bracket = svg.elimFlow({ rounds: servedCrown.rounds, gen_states: servedCrown.gen_states, championId: 'v1', benchmarkId: 'v0', gateState: 'crowned' });
  assert(bracket.textContent.includes('♛'), 'a crowned elim flow gate emits ♛');
  assert(!bracket.textContent.includes('♚'), 'a crowned elim flow gate does NOT emit ♚');

  // the tree current/former champion glyphs.
  const thost = document.createElement('div');
  tree.buildTree(thost, {
    epochs: [{ id: EPOCH_ID, current: true }],
    byEpoch: { [EPOCH_ID]: { gens: [
      { id: 'v0', promoted: true, parent: null, formerChampion: true },
      { id: 'v6', promoted: true, parent: 'v0', currentChampion: true },
    ], boards: [] } },
  }, { view: 'gens', params: { epochId: EPOCH_ID } }, new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/gens']),
    { navigate() {}, href: router.href }, () => {});
  assert(thost.textContent.includes('♛'), 'the tree marks the current champion ♛');
  assert(thost.textContent.includes('♔'), 'the tree marks the former champion ♔');
  assert(!thost.textContent.includes('♚'), 'the tree emits no ♚');
});

// =====================================================================
// INTEGRATION WAVE 8 — the LIVE match-grouped block (Task 1), the tree
// live-activity pulse (Task 2), and the elim generations-across-rounds
// flow (Task 3). All build on the consolidated live machinery
// (buildLiveModel + the published rounds + active-runs overlay) and the
// shared CROWN / glyph vocabulary — no per-structure synthesis, no new
// glyph literals.
// =====================================================================

// ── Task 1 — the match-grouped "what's running" block ──

// a LIVE swiss field with an ACTIVE round (round 1 pending) the block groups by.
const LIVE_SWISS_BLOCK = {
  structure: 'swiss', phase: 'running', epoch_id: HERO_EPOCH,
  structure_params: { board_size: 4, rounds: 3 },
  competitors: [{ generation_id: 'v0' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }],
  rounds: [
    { round_index: 0, label: 'Round 1', matches: [
      { match_id: 'sw_r0_m0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'win' },
      { match_id: 'sw_r0_m1', competitors: ['v2', 'v3'], winner: 'v3', decision: 'win' },
    ] },
    { round_index: 1, label: 'Round 2', matches: [
      { match_id: 'sw_r1_m0', competitors: ['v1', 'v3'] },
      { match_id: 'sw_r1_m1', competitors: ['v0', 'v2'] },
    ] },
  ],
  standings: [], champion_lineage: ['v0'],
};

test('Task 1 — match blocks (swiss): one block per IN-FLIGHT match, two sides, with per-board progress; settled rounds are NOT blocks', () => {
  const model = STRUCT.buildLiveSwissModel({
    at: LIVE_SWISS_BLOCK,
    heartbeat: { phase: 'tournament:round_1', generation_id: 'v1', epoch_id: HERO_EPOCH },
    activeRuns: [
      { generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 2.0 }, // 2 of 4 boards done
      { generation_id: 'v1', entry_id: 'b1', run_id: 'r1', progress: 0.0 },
    ],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  });
  const blocks = STRUCT.liveMatchBlocks(model);
  // round 0 is settled (winners decided) → no block; round 1 is the active round
  // with TWO pending pairings → two blocks.
  assertEqual(blocks.length, 2, 'one block per IN-FLIGHT match (the two pending round-2 pairings); settled round 0 is excluded');
  for (const b of blocks) {
    assertEqual(b.kind, 'pair', 'a swiss block is a pairwise (two-sided) block');
    assertEqual(b.entries.length, 2, 'a pairwise block shows two sides');
  }
  const v1v3 = blocks.find((b) => b.entries.some((e) => e.id === 'v1') && b.entries.some((e) => e.id === 'v3'));
  assert(v1v3, 'a block names the in-flight pairing v1 vs v3');
  assert(/v1 vs v3/.test(v1v3.label), 'the block header names the match — "… · v1 vs v3"');
  const e = v1v3.entries.find((x) => x.id === 'v1');
  assertEqual(e.total, 4, 'the side carries the board total (board_size)');
  assert(svg.isNum(e.ratio) && e.ratio > 0, 'the side carries a live progress ratio from active-runs (2/4 boards done)');
});

test('Task 1 — match blocks (elim): blocks group by in-flight WB match, named WB-R0-0 · v0 vs v3', () => {
  const model = STRUCT.buildLiveElimModel({
    at: liveElimField(),
    heartbeat: { phase: 'tournament:round_0', generation_id: 'v1', epoch_id: HERO_EPOCH },
    activeRuns: [
      { generation_id: 'v0', entry_id: 'b0', run_id: 'r0', progress: 0.25 },
      { generation_id: 'v1', entry_id: 'b1', run_id: 'r1', progress: 0.75 },
    ],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  });
  const blocks = STRUCT.liveMatchBlocks(model);
  assertEqual(blocks.length, 2, 'two in-flight WB-R0 matches → two blocks');
  const wb0 = blocks.find((b) => /WB-R0-0/.test(b.label));
  assert(wb0, 'a block is named by its bracket slot WB-R0-0');
  assert(/v0 vs v3/.test(wb0.label), 'the elim block header reads "WB-R0-0 · v0 vs v3"');
});

test('Task 1 — match blocks (racing): a rung-FIELD block (one entry per lane), header "rung 0 · field of N"', () => {
  const model = STRUCT.buildLiveRacingModel({
    at: liveRacingField(),
    heartbeat: { phase: 'tournament:round_0:rung0_m1', generation_id: 'v5', epoch_id: '2026-06-02_eR' },
    activeRuns: [
      { generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.4 },
      { generation_id: 'v6', entry_id: 'b1', run_id: 'r1', progress: 0.9 },
    ],
    epochGens: ['v0', 'v5', 'v6', 'v7', 'v8'],
  });
  const blocks = STRUCT.liveMatchBlocks(model);
  const rung = blocks.find((b) => b.kind === 'rung');
  assert(rung, 'racing yields a rung-field block (not a pairwise block)');
  assertEqual(rung.entries.length, 4, 'the rung block shows one entry per lane in the field of 4');
  assert(/field of 4/.test(rung.label), 'the header reads "rung … · field of 4"');
  const v5 = rung.entries.find((e) => e.id === 'v5');
  assert(v5 && svg.isNum(v5.ratio), 'a lane carries its live progress ratio');
});

test('Task 1 — the match-grouped block RENDERS: one DOM block per match, a progress bar + a state per side; clickable', () => {
  let opened = null;
  const node = live.liveMatchGroupedBlocks(
    STRUCT.liveMatchBlocks(STRUCT.buildLiveSwissModel({
      at: LIVE_SWISS_BLOCK,
      heartbeat: { phase: 'tournament:round_1', epoch_id: HERO_EPOCH },
      activeRuns: [{ generation_id: 'v1', entry_id: 'b0', progress: 0.5 }],
      epochGens: ['v0', 'v1', 'v2', 'v3'],
    })),
    (id) => { opened = id; },
  );
  const host = document.createElement('div');
  host.appendChild(node);
  assertEqual(allByClass(host, 'dt-live-match').length, 2, 'one DOM block per in-flight match');
  assert(allByClass(host, 'dt-live-match-fill').length >= 2, 'each side has an animated progress fill');
  // the fill width is set inline (CSS-animated; the DOM is not rebuilt per tick).
  const fill = allByClass(host, 'dt-live-match-fill')[0];
  assert(/width:\s*\d+%/.test(fill.style.cssText), 'the progress fill width is set in the style (CSS width transition, not a node swap)');
  // a side row is clickable → opens the candidate.
  const row = allByClass(host, 'dt-live-match-row')[0];
  row.dispatchEvent(makeEvent('click'));
  assert(opened != null, 'clicking a side opens the candidate');
});

test('Task 1 — the block is digest-gated on the live CONTENT: a no-op heartbeat is a no-op; a progress-bucket change re-stamps', () => {
  const at = LIVE_SWISS_BLOCK;
  const beat = (progress) => STRUCT.liveMatchBlocksDigest(STRUCT.liveMatchBlocks(STRUCT.buildLiveSwissModel({
    at, heartbeat: { phase: 'tournament:round_1', epoch_id: HERO_EPOCH },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', progress }],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  })));
  // board_size 4: progress 0.5 → done 0 vs 0.55 → still bucket 0 (no rebuild),
  // but a real bucket jump (progress that lands a board) re-stamps.
  assertEqual(beat(0.0), beat(0.0), 'identical state → identical digest (a no-op heartbeat writes ZERO DOM)');
  const d0 = beat(0.0);
  const dBig = beat(3.5); // ~3-4 of 4 boards done → a real progress bucket
  assert(d0 !== dBig, 'a real per-board progress change re-stamps the digest');
});

// ── Task 2 — the tree live-activity pulse ──

test('Task 2 — tree pulse: a running gen / board entry gets a CSS pulse badge; the digest re-stamps on set change but NOT on a no-op beat', () => {
  const model = {
    epochs: [{ id: EPOCH_ID, current: true }],
    byEpoch: { [EPOCH_ID]: {
      gens: [{ id: 'v0', promoted: true, parent: null, currentChampion: true }, { id: 'v1', promoted: null, parent: 'v0' }],
      boards: [{ id: 'b0' }, { id: 'b1' }],
    } },
    current: EPOCH_ID,
  };
  const route = { view: 'gens', params: { epochId: EPOCH_ID } };
  const toggles = new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/gens', 'e:' + EPOCH_ID + '/boards']);
  const ctx = { navigate() {}, href: router.href };

  // v1 + b0 are running → the badge appears on those rows only.
  const liveSet = new Set(['v1', 'b0']);
  const host = document.createElement('div');
  tree.buildTree(host, model, route, toggles, ctx, () => {}, liveSet);
  const pulses = allByClass(host, 'dt-node-pulse');
  assert(pulses.length === 2, 'exactly the two running rows (v1, b0) get a pulse badge');
  assert(pulses.every((p) => (p.getAttribute('class') || '').includes('dn-inflight-pulse')), 'the pulse REUSES dn-inflight-pulse (the existing CSS-animated clue)');

  // digest discipline: same set → same digest (no-op beat); set change → new digest.
  const dA = tree.treeDigest(model, route, toggles, liveSet);
  const dA2 = tree.treeDigest(model, route, toggles, new Set(['b0', 'v1'])); // same set, different order
  assertEqual(dA, dA2, 'a steady beat with the SAME live set (order-insensitive) is a digest no-op');
  const dB = tree.treeDigest(model, route, toggles, new Set(['v1'])); // b0 LEAVES the running set
  assert(dA !== dB, 'a gen/entry LEAVING the running set re-stamps the digest');
  const dC = tree.treeDigest(model, route, toggles, new Set(['v1', 'b0', 'v0'])); // v0 ENTERS
  assert(dA !== dC, 'a gen/entry ENTERING the running set re-stamps the digest');

  // idle (empty set) → no pulse.
  const idle = document.createElement('div');
  tree.buildTree(idle, model, route, toggles, ctx, () => {}, new Set());
  assertEqual(allByClass(idle, 'dt-node-pulse').length, 0, 'an idle workspace shows no pulse');
});

test('Task 2 — treeLiveSet: derives the running gen+entry ids from active-runs, gated on running + scoped to the epoch', () => {
  const runs = [
    { generation_id: 'v1', entry_id: 'b0', epoch_id: EPOCH_ID },
    { generation_id: 'v2', entry_id: 'b1' }, // no epoch tag → kept (legacy tolerance)
    { generation_id: 'v9', entry_id: 'bX', epoch_id: 'OTHER' }, // foreign epoch → dropped
  ];
  const set = livestatus.treeLiveSet({ activeRuns: runs, running: true, epochId: EPOCH_ID });
  assert(set.has('v1') && set.has('b0'), 'the running gen + entry of the viewed epoch are in the set');
  assert(set.has('v2') && set.has('b1'), 'an untagged run is kept (legacy single-epoch tolerance)');
  assert(!set.has('v9') && !set.has('bX'), 'a foreign-epoch run is excluded');
  const idle = livestatus.treeLiveSet({ activeRuns: runs, running: false, epochId: EPOCH_ID });
  assertEqual(idle.size, 0, 'an idle workspace (running=false) yields the empty set');
});

// ── Task 3 — the elim generations-across-rounds flow ──

test('Task 3 — elimFlow: rounds as columns, one lane per generation; advancing lines + a terminating ✕, the crown at the gate', () => {
  const model = STRUCT.elimModel(STRUCT.normalizeStructure(mock.attachElimStates({ ...SE_STRUCT }), false));
  const flow = svg.elimFlow({
    rounds: model.rounds, gen_states: model.gen_states, championId: model.championId, benchmarkId: model.benchmarkId,
    gateState: model.gateState, live: false, onCompetitor() {},
  });
  assertEqual(flow.getAttribute('class'), 'dn-elimflow', 'the flow is its own renderer (dn-elimflow)');
  assert((flow.getAttribute('width') || '') === '100%' && (flow.getAttribute('viewBox') || ''), 'fit-to-width: width:100% + a viewBox');
  // rounds-as-columns headers + the gate column.
  const cols = allByClass(flow, 'dn-elimflow-col').map((c) => c.textContent);
  assert(cols.some((t) => /Semifinal|R0/.test(t)) && cols.some((t) => /Final|R1/.test(t)), 'rounds are columns (R0 · R1 · …)');
  assert(cols.some((t) => /champion-gate/i.test(t)), 'the champion-gate is the trailing column');
  // an advancing leg (good) + a terminating ✕ (bad) exist.
  assert(allByClass(flow, 'dn-elimflow-good').length >= 1, 'an advancing line/marker reads --v2-good');
  assert(flow.textContent.includes('✕'), 'an eliminated generation terminates with ✕');
  // the champion (v1) reaches the gate with the current crown; v0 (displaced
  // incumbent / benchmark) reads the former crown.
  assertEqual(String(model.championId), 'v1', 'v1 is the bracket champion');
  assert(flow.textContent.includes(svg.CROWN.current), 'the champion lane reaches the gate marked ♛ (CROWN.current)');
  assert(flow.textContent.includes(svg.CROWN.former), 'the displaced incumbent (benchmark v0) reads ♔ (CROWN.former)');
  assert(!flow.textContent.includes('♚'), 'no stray ♚ glyph literal');
});

test('Task 3 — the elim figure is the bracket-as-FLOW (elimFlow), the seat/box tree retired; ABSENT for non-elim (racing)', () => {
  // elim: the bracket-as-flow IS the figure (no seat/box tree).
  const elimNodes = STRUCT.renderStructure(STRUCT.normalizeStructure(SE_STRUCT, false), { navigate() {}, href: router.href }, EPOCH_ID);
  const elimHost = document.createElement('div');
  for (const n of elimNodes) elimHost.appendChild(n);
  assertEqual(svgsByClass(elimHost, 'dn-elimbracket').length, 0, 'the seat/box bracket tree is retired');
  assert(svgsByClass(elimHost, 'dn-elimflow')[0], 'the bracket-as-flow (elimFlow) is the elim figure');
  assert(/Bracket flow/i.test(elimHost.textContent), 'the section carries its bracket-flow title');

  // racing: NO elim flow (it is elim-only).
  const racingNodes = STRUCT.renderStructure(STRUCT.normalizeStructure(RACING_STRUCT, false), { navigate() {}, href: router.href }, EPOCH_ID);
  const racingHost = document.createElement('div');
  for (const n of racingNodes) racingHost.appendChild(n);
  assertEqual(svgsByClass(racingHost, 'dn-elimflow').length, 0, 'the elim flow is ABSENT for a non-elim (racing) structure');
});

test('Task 3 — a LIVE elim flow draws in-flight legs as DASHED (pending convention) from the published rounds', () => {
  const model = STRUCT.elimModel(STRUCT.buildLiveElimModel({
    at: mock.attachElimStates(liveElimField()),
    heartbeat: { phase: 'tournament:round_0', epoch_id: HERO_EPOCH },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', progress: 0.5 }],
    epochGens: ['v0', 'v1', 'v2', 'v3'],
  }));
  const flow = svg.elimFlow({
    rounds: model.rounds, gen_states: model.gen_states, championId: model.championId, benchmarkId: model.benchmarkId,
    gateState: model.gateState, live: true,
  });
  assert(allByClass(flow, 'dn-elimflow-seg-pending').length >= 1, 'an in-flight (pending) leg is drawn with the pending (dashed) class');
});

test('Task 3 — elimFlow: an UNDECIDED match draws a "deciding" node + a SHORT in-flight stub, NOT a premature leg to the champion-gate', () => {
  // a just-seeded WB-R0 head-to-head, undecided (no winner): v12 vs v13. In a
  // double-elim only the WINNER advances toward the gate and the loser drops to
  // the losers' bracket — so NEITHER lane may draw a full leg to the gate yet.
  // The in-flight signal is the "deciding" match node + a short dashed stub
  // (mirroring elimRadial's one-ring pending spoke).
  const winners = [
    { round_index: 0, label: 'Round 1', matches: [
      { competitors: ['v12', 'v13'], winner: null, pending: true, bracket_slot: 'WB-R0-0' },
    ] },
  ];
  const servedU = mock.deriveElimStates(winners);
  const flow = svg.elimFlow({ rounds: servedU.rounds, gen_states: servedU.gen_states, championId: 'v3', benchmarkId: 'v3', live: true });
  // the undecided match node reads "deciding" — the figure's in-flight signal.
  assert(allByClass(flow, 'dn-elimflow-deciding').length >= 1, 'an undecided match node is marked deciding');
  // NO committed (good) advance is drawn while the match is undecided.
  const segs = allByClass(flow, 'dn-elimflow-seg');
  const good = segs.filter((s) => (s.getAttribute('class') || '').includes('dn-elimflow-good'));
  assertEqual(good.length, 0, 'an undecided match draws no committed advance (no good segment)');
  // each pending leg is a SHORT stub, not a full leg to the gate.
  const stubs = allByClass(flow, 'dn-elimflow-seg-pending');
  assert(stubs.length >= 1, 'an undecided lane draws a short in-flight stub');
  for (const s of stubs) {
    const len = Math.abs(parseFloat(s.getAttribute('x2')) - parseFloat(s.getAttribute('x1')));
    assert(len < 60, `the in-flight stub is SHORT (${len.toFixed(1)}px), not a full gate-bound leg`);
  }
});

test('Task 3 — elimFlow: the over-long "Winners\'/Losers\' bracket" round labels compact to "WB R0"/"LB R0" (no "Winners\' br…" truncation); meaningful short labels are KEPT', () => {
  const winners = [
    { round_index: 0, label: "Winners' bracket", matches: [
      { competitors: ['v0', 'v1'], winner: 'v1', decision: 'win', bracket_slot: 'WB-R0-0' },
    ] },
    { round_index: 1, label: "Losers' bracket", matches: [
      { competitors: ['v0', 'v2'], winner: 'v0', decision: 'win', bracket_slot: 'LB-R0-0' },
    ] },
  ];
  const servedL = mock.deriveElimStates(winners);
  const flow = svg.elimFlow({ rounds: servedL.rounds, gen_states: servedL.gen_states, championId: 'v1', benchmarkId: 'v0', gateState: 'crowned' });
  const cols = allByClass(flow, 'dn-elimflow-col').map((c) => c.textContent);
  assert(cols.includes('WB R0'), `the winners' bracket round compacts to "WB R0" (got ${JSON.stringify(cols)})`);
  assert(cols.includes('LB R0'), `the losers' bracket round compacts to "LB R0" (got ${JSON.stringify(cols)})`);
  assert(!cols.some((t) => t.includes('…')), `no header truncates mid-word (got ${JSON.stringify(cols)})`);
});

test('Task 3 — elimFlow: a lane ELIMINATED in a column draws NO phantom advancing segment, even when it also WON a sibling match in the same column (degenerate live champion-vs-field round)', () => {
  // v1 plays TWO matches in column 0 — WINS vs v2, LOSES vs v3 — so it is marked
  // BOTH `advanced` and `eliminated` at column 0. Pre-fix, the won-match drew a
  // solid green "advanced" segment leaving v1's eliminated dot and landing at a
  // column with NO dot (the dangling "disconnected" line). Only the champion
  // v3's path (col0→col1, col1→gate) should draw advancing segments.
  const winners = [
    { round_index: 0, label: 'Round 1', matches: [
      { competitors: ['v1', 'v2'], winner: 'v1', decision: 'win', bracket_slot: 'WB-R0-0' },
      { competitors: ['v3', 'v1'], winner: 'v3', decision: 'win', bracket_slot: 'WB-R0-1' },
    ] },
    { round_index: 1, label: 'Final', matches: [
      { competitors: ['v3', 'v0'], winner: 'v3', decision: 'win', bracket_slot: 'WB-R1-0' },
    ] },
  ];
  const servedD = mock.deriveElimStates(winners);
  const flow = svg.elimFlow({ rounds: servedD.rounds, gen_states: servedD.gen_states, championId: 'v3', benchmarkId: 'v0', gateState: 'crowned' });
  const segs = allByClass(flow, 'dn-elimflow-seg');
  const goodSegs = segs.filter((s) => (s.getAttribute('class') || '').includes('dn-elimflow-good'));
  assertEqual(goodSegs.length, 2, 'only the champion v3 advances (col0→col1, col1→gate) — the eliminated v1 draws no phantom segment');
  assert(flow.textContent.includes('✕'), 'the eliminated lanes still terminate with ✕');
});

await run();
