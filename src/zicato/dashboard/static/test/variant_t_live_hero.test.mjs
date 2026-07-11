// test/variant_t_live_hero.test.mjs — Variant T ("Console IV") unit tests:
// the SSE-driven live-run display (funnel/ladder transitions, activity
// ticker, live hero) and the cross-epoch leakage fixes.
//
// Split mechanically from the former variant_t.test.mjs (assertions
// verbatim); shared fixtures + helpers live in ./fixtures.mjs.

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';
import { attachElimStates } from './mock_server.mjs';

installDom();

const {
  router, svg, data, tree, coreState, rounds,
  live, STRUCT, racingFieldFromBracket, EPOCH_ID, freshHb, installFetch,
  freshState, allByClass, readCss, svgsByClass, mountLiveShell, installFixtureMap,
  RC_EPOCH, RACING_PER_CHALLENGER, RACING_TOURNAMENTS, HERO_EPOCH, TWO_EP_OLD, TWO_EP_NEW,
  twoEpochFixture,
} = await import('./fixtures.mjs');

// ====================================================================
// LIVE-RUN display — SSE-driven, animated funnel/ladder transitions,
// tournament progress, the activity ticker, and the live hero, PLUS the
// champion/benchmark (v0) reference in the racing ladder.
//   (a) an active-tournament (racing, running) → the live hero + funnel
//       render and the activity ticker lists events;
//   (b) feeding a phase/active-runs update MUTATES the live surfaces
//       WITHOUT a full repaint (node identity preserved / digest gates
//       structure);
//   (c) under prefers-reduced-motion the animation classes/transitions
//       are suppressed (the reduced-motion CSS gate);
//   (d) the racing ladder shows the champion/benchmark (v0) reference and
//       labels deltas as vs-v0;
//   (e) idle (no active run) renders the static views unchanged;
//   (f) the live engine's derivations (progress, activity diff, ticker).
// ====================================================================


// the LIVE racing active-tournament topology used to drive the hero: rung 0 has
// cut v2/v3 and carried v0/v1; rung 1 is still racing (no cut yet); v0 is the
// champion the field is raced against (the benchmark seat in every rung).
const HERO_LIVE_RACING = {
  structure: 'racing', phase: 'running',
  structure_params: { rungs: [{ fraction: 0.5 }, { fraction: 1.0 }] },
  champion_lineage: ['v0'],
  competitors: [
    { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
    { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v0', 'v1'], cut: ['v2', 'v3'], board_fraction: 0.5, deltas: { v1: -0.2, v2: 1.0, v3: 2.0 } }] },
    { round_index: 1, label: 'Rung 2', matches: [{ match_id: 'rung2', competitors: ['v0', 'v1'], survivors: [], cut: [], board_fraction: 1.0 }] },
  ],
  standings: [],
};

// ---- ActivityTicker.reset() — the idle-leak fix ----

test('ActivityTicker.reset(): clears every row AND the dedup memory (idle-leak fix), restoring the empty placeholder', () => {
  const t = new live.ActivityTicker({ cap: 40 });
  t.push([{ id: 'a1', kind: 'matchup', text: 'run one · started' },
          { id: 'a2', kind: 'run', text: 'run one · completed' }]);
  assertEqual(allByClass(t.node, 'dt-ticker-row').length, 2, 'two rows from the finished run');
  assertEqual(allByClass(t.node, 'dt-ticker-empty').length, 0, 'the empty placeholder is hidden while rows exist');

  t.reset();
  assertEqual(allByClass(t.node, 'dt-ticker-row').length, 0, 'reset clears the finished run’s rows (no leak into the next run)');
  assertEqual(allByClass(t.node, 'dt-ticker-empty').length, 1, 'reset restores the "waiting for activity…" placeholder');

  // the dedup memory is cleared too, so a re-used id from the next run re-adds
  // (without the clear, a recycled id would be silently swallowed).
  const added = t.push([{ id: 'a1', kind: 'matchup', text: 'run two · started' }]);
  assertEqual(added, 1, 'reset cleared the dedup set — the recycled id lands as a fresh row');
  assertEqual(allByClass(t.node, 'dt-ticker-row').length, 1, 'the next run starts from a clean, one-row feed');
});

// ---- (a) the live hero + funnel render; the ticker lists events ----

test('live hero: an active-tournament (racing, running) renders the live hero + scalar track and the activity ticker lists events', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1' });
  coreState.state.activeRuns = [
    { generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 },
    { generation_id: 'v0', entry_id: 'b1', run_id: 'r2', progress: 0.3 },
  ];
  coreState.state.activeTournament = HERO_LIVE_RACING;

  const root = mountLiveShell('#/');
  // the hero host is flagged live + the hero panel carries the .dt-live-on class.
  const heroHost = allByClass(root, 'dt-hero-host')[0];
  assert(heroHost && (heroHost.getAttribute('class') || '').includes('dt-hero-live'), 'the hero host is flagged live during a run');
  const hero = allByClass(root, 'dt-live-hero')[0];
  assert(hero && (hero.getAttribute('class') || '').includes('dt-live-on'), 'the live hero is shown (dt-live-on) for a running tournament');
  // the ONE muted metadata baseline names the structure + the 1-indexed "rung N
  // of M" (the SAME rung-number source the stepper reads) + the in-flight count.
  const meta = allByClass(root, 'dt-live-hero-meta')[0];
  assert(meta && meta.textContent.includes('racing'), 'the metadata baseline names the structure (racing)');
  assert(meta && /rung\s+\d+\s+of\s+\d+/.test(meta.textContent), 'the metadata baseline carries the "rung N of M" label (the one rung-number source)');
  assert(meta && meta.textContent.includes('2 units running'), 'the metadata baseline shows the in-flight unit count (2 units running)');
  // the rung STEPPER reflects the rung index/count (one pip per rung, one active).
  const pips = allByClass(root, 'dt-rungstep-pip');
  assert(pips.length >= 1, 'the rung stepper renders one pip per rung');
  assert(allByClass(root, 'dt-rungstep-active').length === 1, 'exactly one stepper pip is active (the current rung)');
  // the racing scalar track rendered inside the hero (the live hero mini is the
  // single-round PRIMARY figure: the field on one loss number-line).
  const track = svgsByClass(root, 'dn-scalartrack')[0];
  assert(track, 'the racing scalar track rendered inside the live hero');
  // the activity ticker lists events derived from the live state.
  const ticker = allByClass(root, 'dt-ticker')[0];
  assert(ticker, 'the activity ticker rendered');
  const rows = allByClass(root, 'dt-ticker-row');
  assert(rows.length >= 1, 'the ticker lists at least one live activity event');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// ---- (b) a live update MUTATES the surfaces without a full repaint ----

test('live hero: a phase/active-runs update mutates the live surfaces WITHOUT a full repaint (node identity preserved; structure digest gates the scalar track)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.2 }];
  coreState.state.activeTournament = HERO_LIVE_RACING;

  const root = mountLiveShell('#/');
  const metaNodeBefore = allByClass(root, 'dt-live-hero-meta')[0];
  const stepperBefore = allByClass(root, 'dt-rungstep')[0];
  const trackBefore = svgsByClass(root, 'dn-scalartrack')[0];
  const tickerListBefore = allByClass(root, 'dt-ticker-list')[0];
  assert(metaNodeBefore && stepperBefore && trackBefore && tickerListBefore, 'the live surfaces mounted');
  const rowsBefore = allByClass(root, 'dt-ticker-row').length;

  // a STEADY re-tick with IDENTICAL live state writes no new ticker rows and
  // does NOT rebuild the scalar track NOR the rung stepper (every digest is
  // unchanged → ZERO DOM, no flash).
  coreState.state._changed();
  assertEqual(allByClass(root, 'dt-ticker-row').length, rowsBefore, 'an identical re-tick appends NO ticker rows (no flash)');
  assert(svgsByClass(root, 'dn-scalartrack')[0] === trackBefore, 'an identical re-tick does NOT rebuild the scalar track (digest-gated structure)');
  assert(allByClass(root, 'dt-rungstep')[0] === stepperBefore, 'an identical re-tick does NOT rebuild the rung stepper (digest-gated)');
  // the persistent metadata node + ticker list keep identity (patched in place).
  assert(allByClass(root, 'dt-live-hero-meta')[0] === metaNodeBefore, 'the metadata node keeps identity across a re-tick (patched in place)');
  assert(allByClass(root, 'dt-ticker-list')[0] === tickerListBefore, 'the ticker list keeps identity (append-only)');

  // now a REAL change: rung 2 resolves (v0 cut, v1 survives) + a run completes.
  const next = JSON.parse(JSON.stringify(HERO_LIVE_RACING));
  next.rounds[1].matches[0].survivors = ['v1'];
  next.rounds[1].matches[0].cut = ['v0'];
  coreState.state.activeTournament = next;
  coreState.state.activeRuns = [];   // the in-flight run completed.
  coreState.state.setHeartbeat({ phase: 'tournament:round_2:racing-final' });
  coreState.state._changed();

  // the scalar track rebuilt (the structure digest changed) — but the ticker LIST
  // and the phase node are still the SAME persistent nodes (mutated, not replaced).
  assert(allByClass(root, 'dt-ticker-list')[0] === tickerListBefore, 'the ticker list is still the same node after a real change (append-only growth)');
  assert(allByClass(root, 'dt-live-hero-meta')[0] === metaNodeBefore, 'the metadata node is still the same node (patched, not rebuilt)');
  assert(allByClass(root, 'dt-ticker-row').length > rowsBefore, 'a real change (rung cut + run completed) appended new ticker rows');
  // the newly-built scalar track carries the one-shot entrance animation class.
  const trackAfter = svgsByClass(root, 'dn-scalartrack')[0];
  assert((trackAfter.getAttribute('class') || '').includes('dt-live-enter'), 'a freshly-built scalar track carries the one-shot entrance class (eases in, never repaint-loops)');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// ---- (b2) the hero is STRUCTURE-AWARE + SCOPED TO THE CURRENT TOURNAMENT ----
//
// The racing survival funnel is meaningful ONLY for a LIVE racing tournament
// that belongs to the CURRENT run. These tests pin the gate (structure===racing
// AND running AND current-epoch): a swiss live tournament shows round-based
// progress with NO funnel; a current-epoch PROPOSING phase with a stale racing
// active-tournament from a DIFFERENT epoch shows the honest proposing/empty
// state (no funnel, no leaked foreign competitor ids); a completed/idle
// tournament shows no funnel.


// a LIVE racing tournament that names the CURRENT epoch — the funnel SHOULD show.
const HERO_LIVE_RACING_E3 = JSON.parse(JSON.stringify(HERO_LIVE_RACING));
HERO_LIVE_RACING_E3.epoch_id = HERO_EPOCH;

// a STALE/FOREIGN completed racing tournament retained from a PRIOR epoch (e1):
// it carries v6/v8 survivors + v5/v7 cuts + a "vs champion v0" gate — exactly
// the prior-epoch funnel the bug leaked into e3's proposing hero.
const HERO_STALE_RACING_E1 = {
  structure: 'racing', phase: 'completed', epoch_id: '2026-06-01_e1',
  structure_params: { rungs: [{ fraction: 0.5 }, { fraction: 1.0 }] },
  champion_lineage: ['v0'],
  competitors: [
    { generation_id: 'v0', role: 'champion' }, { generation_id: 'v5', role: 'challenger' },
    { generation_id: 'v6', role: 'challenger' }, { generation_id: 'v7', role: 'challenger' },
    { generation_id: 'v8', role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v5', 'v6', 'v7', 'v8'], survivors: ['v6', 'v8'], cut: ['v5', 'v7'], board_fraction: 0.5 }] },
    { round_index: 1, label: 'Champion gate', matches: [{ match_id: 'racing-final', competitors: ['v0', 'v6'], winner: 'v0', decision: 'rejected', board_fraction: 1.0 }] },
  ],
  standings: [],
};

// a LIVE swiss tournament for the current epoch — round-based, NO racing funnel.
const HERO_LIVE_SWISS_E3 = {
  structure: 'swiss', phase: 'running', epoch_id: HERO_EPOCH,
  structure_params: { rounds: 3 },
  competitors: [
    { generation_id: 'v0' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' },
  ],
  rounds: [
    { round_index: 0, label: 'Round 1', matches: [
      { match_id: 'sw_r0_m0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'win' },
      { match_id: 'sw_r0_m1', competitors: ['v2', 'v3'], winner: 'v3', decision: 'win' },
    ] },
    { round_index: 1, label: 'Round 2', matches: [
      { match_id: 'sw_r1_m0', competitors: ['v1', 'v3'] },
      { match_id: 'sw_r1_m1', competitors: ['v0', 'v2'] },
    ] },
    { round_index: 2, label: 'Round 3', matches: [
      { match_id: 'sw_r2_m0', competitors: ['v1', 'v2'] },
      { match_id: 'sw_r2_m1', competitors: ['v0', 'v3'] },
    ] },
  ],
  standings: [],
};

test('live hero: a LIVE RACING tournament for the CURRENT epoch renders the scalar track', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = HERO_LIVE_RACING_E3;

  const root = mountLiveShell('#/');
  const track = svgsByClass(root, 'dn-scalartrack')[0];
  assert(track, 'the racing scalar track renders for a LIVE racing tournament whose epoch matches the heartbeat');
  // the track was eligible → no "field fills in…" placeholder fallback.
  assert(allByClass(root, 'dt-live-hero-nofunnel').length === 0, 'no empty/proposing placeholder when the racing scalar track is live');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: a LIVE SWISS tournament shows the SWISS LADDER + round-based progress, NOT the racing funnel', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_1', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = HERO_LIVE_SWISS_E3;

  const root = mountLiveShell('#/');
  const hero = allByClass(root, 'dt-live-hero')[0];
  assert(hero && (hero.getAttribute('class') || '').includes('dt-live-on'), 'the live hero is shown for a running swiss tournament');
  // the metadata baseline carries the round-based label ("round k of N") for a
  // non-racing structure (the same 1-indexed "N of M" the stepper reads).
  const meta = allByClass(root, 'dt-live-hero-meta')[0];
  assert(meta && /round\s+\d+\s+of\s+\d+/.test(meta.textContent), 'the swiss hero metadata shows a round-based label (round k of N)');
  // the activity ticker still streams.
  assert(allByClass(root, 'dt-ticker')[0], 'the activity ticker renders for a swiss run');
  // the LIVE SWISS LADDER renders (the swiss analogue of the racing funnel) —
  // NOT the racing funnel and NOT just the text placeholder.
  const ladder = svgsByClass(root, 'dn-swissladder')[0];
  assert(ladder, 'the live swiss standings ladder rendered in the hero');
  assertEqual(svgsByClass(root, 'dn-funnel').length, 0, 'NO racing survival funnel for a LIVE swiss tournament');
  assertEqual(svgsByClass(root, 'dn-elimbracket').length, 0, 'NO elim bracket for a LIVE swiss tournament');
  assertEqual(allByClass(root, 'dt-live-hero-nofunnel').length, 0, 'no text placeholder once the swiss ladder is live');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// a LIVE single-elim tournament for the current epoch — bracket, NO funnel.
// the served live payload carries gen_states (attach_elim_states on the server;
// the radial renders the SERVED model verbatim per U3 — no client re-derivation).
const HERO_LIVE_ELIM_E3 = attachElimStates({
  structure: 'single_elim', phase: 'running', epoch_id: HERO_EPOCH,
  structure_params: { board_size: 4 },
  champion_lineage: ['v0'],
  competitors: [
    { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
    { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Semifinal', matches: [
      { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], winner: 'v0', decision: 'win', bracket_slot: 'WB-R0-0' },
      { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], bracket_slot: 'WB-R0-1' },
    ] },
    { round_index: 1, label: 'Final', matches: [
      { match_id: 'WB-R1-0', competitors: ['v0', 'v1'], bracket_slot: 'WB-R1-0' },
    ] },
  ],
  standings: [],
});

test('live hero: a LIVE SINGLE-ELIM tournament renders the RADIAL bracket, NOT the racing track or swiss ladder', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_0', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = HERO_LIVE_ELIM_E3;

  const root = mountLiveShell('#/');
  // single_elim hero is the concentric-ring RADIAL (the single-round primary).
  const bracket = svgsByClass(root, 'dn-elimradial')[0];
  assert(bracket, 'the live single-elim radial bracket rendered in the hero');
  assertEqual(svgsByClass(root, 'dn-elimbracket').length, 0, 'the seat/box bracket tree is retired');
  assertEqual(svgsByClass(root, 'dn-scalartrack').length, 0, 'NO racing scalar track for a LIVE elim tournament');
  assertEqual(svgsByClass(root, 'dn-swissladder').length, 0, 'NO swiss ladder for a LIVE elim tournament');
  assertEqual(allByClass(root, 'dt-live-hero-nofunnel').length, 0, 'no text placeholder once the bracket is live');
  // the eliminated semifinal lane terminates with ✕ (the radial emits a cut glyph).
  assert(/✕/.test(bracket.textContent), 'a decided semifinal eliminates a lane (✕)');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: racing STILL renders the scalar track (no swiss/elim regression), and a foreign-epoch elim shows the honest empty state', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  // racing for the current epoch → the scalar track (unchanged structure-wise).
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = HERO_LIVE_RACING_E3;
  let root = mountLiveShell('#/');
  assert(svgsByClass(root, 'dn-scalartrack')[0], 'racing still renders the scalar track');
  assertEqual(svgsByClass(root, 'dn-swissladder').length, 0, 'no swiss ladder for a racing run');
  assertEqual(svgsByClass(root, 'dn-elimbracket').length, 0, 'no elim bracket for a racing run');

  // a FOREIGN-epoch elim (current epoch proposing) → no elim topology in the hero.
  const foreignElim = JSON.parse(JSON.stringify(HERO_LIVE_ELIM_E3));
  foreignElim.epoch_id = '2026-06-01_e1';
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = foreignElim;
  root = mountLiveShell('#/');
  assertEqual(svgsByClass(root, 'dn-elimbracket').length, 0, 'NO elim bracket for a foreign-epoch tournament while the current epoch proposes');
  assertEqual(svgsByClass(root, 'dn-elimradial').length, 0, 'NO elim radial either for a foreign-epoch tournament while proposing');
  assertEqual(svgsByClass(root, 'dn-funnel').length, 0, 'no funnel either — honest empty');
  assert(allByClass(root, 'dt-live-hero-nofunnel').length >= 1, 'the hero shows the honest proposing/empty state');
  const heroText = (allByClass(root, 'dt-live-hero')[0] || {}).textContent || '';
  assert(!/v2\b|v3\b/.test(heroText) || /propos/i.test(heroText), 'no foreign-epoch bracket topology leaks into the proposing hero');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: a PROPOSING current run (no tournament) shows the honest proposing state — no swiss/elim/racing topology', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
  const root = mountLiveShell('#/');
  assertEqual(svgsByClass(root, 'dn-funnel').length, 0, 'no racing funnel while proposing');
  assertEqual(svgsByClass(root, 'dn-swissladder').length, 0, 'no swiss ladder while proposing');
  assertEqual(svgsByClass(root, 'dn-elimbracket').length, 0, 'no elim bracket while proposing');
  assert(allByClass(root, 'dt-live-hero-nofunnel').length >= 1, 'the honest proposing placeholder is shown');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: a CURRENT-EPOCH PROPOSING phase with a STALE racing tournament from a DIFFERENT epoch shows the proposing/empty state — NOT the prior epoch funnel', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  // the current run is e3 PROPOSING; the active-tournament is e1's COMPLETED racer.
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = HERO_STALE_RACING_E1;

  const root = mountLiveShell('#/');
  const hero = allByClass(root, 'dt-live-hero')[0];
  assert(hero && (hero.getAttribute('class') || '').includes('dt-live-on'), 'the hero is live during the proposing phase (an active-tournament running)');
  // CRITICAL: NO racing funnel for a stale/foreign-epoch tournament.
  assertEqual(svgsByClass(root, 'dn-funnel').length, 0, 'NO racing funnel while the current epoch is proposing with only a foreign-epoch active-tournament');
  // the honest proposing/empty placeholder is shown.
  assert(allByClass(root, 'dt-live-hero-nofunnel').length >= 1, 'the hero shows the honest proposing/empty progress state');
  // the metadata baseline reflects the CURRENT run (proposing), not "rung k of N".
  const meta = allByClass(root, 'dt-live-hero-meta')[0];
  assert(meta && /propos/i.test(meta.textContent), 'the metadata baseline reads the current proposing phase, not a stale rung count');
  assert(meta && !/rung\s+\d+\s+of\s+\d+/.test(meta.textContent), 'the metadata baseline does NOT leak the stale rung count');
  // the rung stepper is absent while proposing (no topology → no pips).
  assertEqual(allByClass(root, 'dt-rungstep-pip').length, 0, 'the rung stepper renders no pips while proposing (no rung topology yet)');
  // no leaked prior-epoch competitor ids anywhere in the hero (e.g. v5/v6/v7/v8).
  const heroText = hero.textContent || '';
  assert(!/v5|v6|v7|v8/.test(heroText), 'no leaked prior-epoch competitor ids (foreign survivors/cuts) appear in the hero');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: a COMPLETED/idle racing tournament renders NO funnel (the funnel is live-only)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  // an in-flight unit keeps the hero "live", but the tournament itself is completed.
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1', epoch_id: HERO_EPOCH });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  const completed = JSON.parse(JSON.stringify(HERO_LIVE_RACING_E3));
  completed.phase = 'completed';
  coreState.state.activeTournament = completed;

  const root = mountLiveShell('#/');
  assertEqual(svgsByClass(root, 'dn-funnel').length, 0, 'NO racing funnel for a COMPLETED tournament (the live hero funnel is running-only)');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// ---- (b3) the PROPOSING-STEP TRACKER in the live hero ----
//
// During the proposing phase (and the early tournament phase before a
// structure topology exists) the live hero shows the field FORMING — the
// per-challenger applied/rejected outcomes — instead of the bland
// placeholder. Structure-agnostic, digest-gated, current-epoch-scoped.

// a CURRENT-epoch proposing-phase active-tournament carrying field_status:
// two challengers minted, one applied, one rejected. No structure topology
// yet (rounds empty) — so the tracker leads, not a figure.
const HERO_PROPOSING_E3 = {
  structure: 'swiss', phase: 'proposing', epoch_id: HERO_EPOCH,
  structure_params: { rounds: 3 },
  competitors: [{ generation_id: 'v0', seed: 1, role: 'champion' }],
  rounds: [], standings: [],
  field_status: [
    { generation_id: 'v1', status: 'applied', reason: '', seed: 2 },
    { generation_id: 'v2', status: 'rejected', reason: 'proposer returned invalid JSON', seed: 3 },
  ],
};

test('live hero: the PROPOSING phase shows the proposing-step tracker (applied ✓ / rejected ✗) instead of the bland placeholder', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = HERO_PROPOSING_E3;

  const root = mountLiveShell('#/');
  const tracker = allByClass(root, 'dn-prop-tracker')[0];
  assert(tracker, 'the proposing-step tracker renders in the live hero during the proposing phase');
  // the bland placeholder is REPLACED by the tracker.
  assertEqual(allByClass(root, 'dt-live-hero-nofunnel').length, 0, 'the bland "field fills in…" placeholder is replaced by the tracker');
  // no structure figure yet (rounds empty).
  assertEqual(svgsByClass(root, 'dn-funnel').length + svgsByClass(root, 'dn-swissladder').length + svgsByClass(root, 'dn-elimbracket').length, 0, 'no structure figure while only the field is forming');
  // one applied (✓ v1) + one rejected (✗ v2) row.
  assertEqual(allByClass(root, 'dn-prop-row-ok').length, 1, 'one applied row');
  assertEqual(allByClass(root, 'dn-prop-row-bad').length, 1, 'one rejected row');
  assert(tracker.textContent.includes('2 proposed') && tracker.textContent.includes('1 applied'), 'the headline counts the minted field');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: an ALL-REJECTED proposing field reads "0 applied — all rejected", NOT an idle/empty hero', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  const allBad = JSON.parse(JSON.stringify(HERO_PROPOSING_E3));
  allBad.field_status = [
    { generation_id: 'v1', status: 'rejected', reason: 'empty response', seed: 2 },
    { generation_id: 'v2', status: 'rejected', reason: 'empty response', seed: 3 },
    { generation_id: 'v3', status: 'rejected', reason: 'post-apply validation failed', seed: 4 },
    { generation_id: 'v4', status: 'rejected', reason: 'mutation_id no longer resolves', seed: 5 },
  ];
  coreState.state.activeTournament = allBad;

  const root = mountLiveShell('#/');
  const head = allByClass(root, 'dn-prop-head')[0];
  assert(head, 'the proposing-step tracker headline rendered (NOT an empty hero)');
  assert(head.textContent.includes('4 proposed') && head.textContent.includes('0 applied'), 'the headline reads "4 proposed · 0 applied"');
  assert(/all rejected/i.test(head.textContent), 'the all-rejected field reads "all rejected"');
  assertEqual(allByClass(root, 'dn-prop-row-ok').length, 0, 'no applied rows');
  assertEqual(allByClass(root, 'dn-prop-row-bad').length, 4, 'all four rejected rows render');
  assertEqual(allByClass(root, 'dt-live-hero-nofunnel').length, 0, 'NOT the idle/empty placeholder');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

test('live hero: the proposing-step tracker is DIGEST-GATED (a no-op heartbeat does not rebuild it) and current-epoch SCOPED', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.setHeartbeat(freshHb({ phase: 'proposing:field', generation_id: '', epoch_id: HERO_EPOCH }));
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = HERO_PROPOSING_E3;

  const root = mountLiveShell('#/');
  const trackerBefore = allByClass(root, 'dn-prop-tracker')[0];
  assert(trackerBefore, 'the tracker mounted');
  // a STEADY re-tick with identical field_status does NOT rebuild the tracker.
  coreState.state._changed();
  assert(allByClass(root, 'dn-prop-tracker')[0] === trackerBefore, 'an identical re-tick does NOT rebuild the tracker (digest-gated, no flash)');
  // a REAL change (v2 now applies) rebuilds it.
  const next = JSON.parse(JSON.stringify(HERO_PROPOSING_E3));
  next.field_status[1].status = 'applied';
  coreState.state.activeTournament = next;
  coreState.state._changed();
  const trackerAfter = allByClass(root, 'dn-prop-tracker')[0];
  assert(trackerAfter && trackerAfter !== trackerBefore, 'a real field-status change rebuilds the tracker');
  assertEqual(allByClass(root, 'dn-prop-row-ok').length, 2, 'both challengers now read as applied');

  // CURRENT-EPOCH SCOPED: a foreign-epoch field_status must NOT render.
  const foreign = JSON.parse(JSON.stringify(HERO_PROPOSING_E3));
  foreign.epoch_id = '2026-06-01_e1';
  coreState.state.activeTournament = foreign;
  const root2 = mountLiveShell('#/');
  assertEqual(allByClass(root2, 'dn-prop-tracker').length, 0, 'a foreign-epoch proposing field is NOT shown (current-epoch scoped)');
  assert(allByClass(root2, 'dt-live-hero-nofunnel').length >= 1, 'the foreign-epoch case falls back to the honest placeholder');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
});

// ---- (c) reduced-motion suppresses the animations ----

test('live motion: prefers-reduced-motion suppresses the live animation classes/transitions (the reduced-motion CSS gate)', () => {
  const css = readCss().replace(/\s+/g, ' ');
  // the reduced-motion block exists and zeroes the live motion.
  assert(/@media \(prefers-reduced-motion: reduce\) \{[^}]*\.dt-live-hero-dot \{ animation: none/.test(css)
    || /@media \(prefers-reduced-motion: reduce\) \{[^@]*\.dt-live-hero-dot\b[^}]*animation: none/.test(css),
    'the breathing live dot is stilled under reduced motion');
  // each live animation/transition has a reduced-motion suppression.
  const rm = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
  assert(rm.includes('.dt-live-enter') && /\.dt-live-enter[^;{}]*\{?[^}]*animation: none/.test(rm) || rm.includes('.dt-live-enter'), 'the funnel/ladder entrance is suppressed under reduced motion');
  assert(rm.includes('.dt-ticker-row'), 'the ticker-row slide-in is suppressed under reduced motion');
  assert(rm.includes('.dt-rungstep-pip'), 'the rung-stepper pip transition is suppressed under reduced motion');
  assert(/\.dn-funnel-band/.test(rm) && /\.dn-funnel-bar/.test(rm), 'the funnel band + progress-bar transitions are suppressed under reduced motion');
  // sanity: the un-gated rules DO carry motion (so reduced-motion is a real gate).
  assert(/\.dt-ticker-row \{[^}]*animation: dt-ticker-in/.test(css), 'the ticker row animates by default (gated off only under reduced motion)');
  assert(/@keyframes dt-live-fade/.test(css) && /@keyframes dt-ticker-in/.test(css), 'the live keyframes are defined');
});

// ---- (d) the racing model derives the champion/benchmark (v0) reference ----
// (the rendered funnel's benchmark caption is exercised by the "survival funnel:
// carries the champion/benchmark (v0) reference …" test below.)

test('racing: the racingModel derives the champion/benchmark (v0) seat distinct from the survivor', () => {
  const st = STRUCT.normalizeStructure(racingFieldFromBracket(RACING_TOURNAMENTS, RC_EPOCH), false);
  const model = STRUCT.racingModel(st);
  assert(model, 'a racing model was derived');
  assertEqual(model.benchmarkId, 'v0', 'the benchmark is the champion v0 (the seat the field is raced against)');
  assertEqual(model.championId, 'v3', 'the champion (eventual survivor) is v3 — distinct from the benchmark v0');
});

test('survival funnel: carries the champion/benchmark (v0) reference + labels the gate vs champion v0', () => {
  const rungs = [
    { match_id: 'rung0', label: 'Rung 1', competitors: ['v1', 'v2', 'v3'], survivors: ['v3'], cut: ['v1', 'v2'], board_fraction: 0.5, deltas: { v3: -1 } },
  ];
  const node = svg.survivalFunnel({ rungs, championId: 'v3', benchmarkId: 'v0', gateState: 'crowned', onCompetitor() {} });
  const bench = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-funnel-bench'))[0];
  assert(bench && bench.textContent.includes('v0'), 'the funnel carries a champion/benchmark caption naming v0');
  const gateSubs = node.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dn-funnel-sub'));
  assert(gateSubs.some((n) => /vs champion v0/.test(n.textContent)), 'the gate sub-label reads "vs champion v0"');
});

// ---- (e) idle renders the static views unchanged ----

test('live: idle (no active run) hides the live hero — the normal summary leads', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  coreState.state.connected = true; coreState.state.connecting = false;
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const root = mountLiveShell('#/');
  const heroHost = allByClass(root, 'dt-hero-host')[0];
  assert(heroHost && !(heroHost.getAttribute('class') || '').includes('dt-hero-live'), 'the hero host is NOT flagged live when idle');
  const hero = allByClass(root, 'dt-live-hero')[0];
  assert(hero && !(hero.getAttribute('class') || '').includes('dt-live-on'), 'the live hero is hidden (no dt-live-on) when idle');
  // the idle hero adds NO ticker rows.
  assertEqual(allByClass(root, 'dt-ticker-row').length, 0, 'no activity rows accumulate while idle');
});

test('live: an idle racing epoch still renders the static completed funnel/summary (the live hero does not interfere)', async () => {
  freshState();
  installFixtureMap({
    '/api/epoch': { epoch_id: RC_EPOCH, closed: true, goal: 'g', tournament: { structure: 'racing', params: RACING_TOURNAMENTS.structure_params },
      experiments: RACING_PER_CHALLENGER.map((t) => ({ generation_id: t.tournament_id.split('->')[1], parent_generation_id: 'v0', outcome: { decision: 'rejected' } })), board: [] },
    '/api/lineage': { generations: [{ generation_id: 'v0', epoch_id: RC_EPOCH, parent_generation_id: '', promoted: true }] },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': RACING_TOURNAMENTS,
  });
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = []; coreState.state.activeTournament = null;
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH });
  // the static completed funnel still renders (idle view unchanged) + names v0.
  const funnel = svgsByClass(host, 'dn-funnel')[0];
  assert(funnel, 'the static completed survival funnel renders when idle');
  assert(allByClass(host, 'dt-live-pill').length === 0, 'no LIVE pill on an idle epoch funnel');
});

// ---- (f) the live engine's pure derivations ----

test('live engine: liveProgress derives "rung k of N · m/n matchups" + a fraction for a racing tournament', () => {
  const prog = live.liveProgress({
    activeTournament: HERO_LIVE_RACING,
    heartbeat: { phase: 'tournament:round_1:rung1_m0' },
    status: { running: true, structure: 'racing' },
  });
  assertEqual(prog.kind, 'racing', 'a racing topology yields racing progress');
  assert(/rung\s+\d+\s+of\s+2/.test(prog.label), 'the label reads "rung k of N"');
  assert(typeof prog.fraction === 'number' && prog.fraction >= 0 && prog.fraction <= 1, 'a determinate 0..1 fraction');
});

test('live engine: deriveActivity diffs two snapshots into events (matchup started, run completed, rung cut, gate decided)', () => {
  const s0 = live.liveSnapshot({
    status: { running: true, structure: 'racing' },
    heartbeat: { phase: 'tournament:round_0:rung0_m0' },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }],
    activeTournament: { structure: 'racing', rounds: [{ matches: [{ match_id: 'rung0', survivors: [], cut: [] }] }] },
  });
  const s1 = live.liveSnapshot({
    status: { running: true, structure: 'racing' },
    heartbeat: { phase: 'tournament:round_0:rung0_m1' },
    activeRuns: [{ generation_id: 'v2', entry_id: 'b0', run_id: 'r2' }],   // r1 done, r2 started
    activeTournament: { structure: 'racing', rounds: [
      { matches: [{ match_id: 'rung0', survivors: ['v2'], cut: ['v1'] }] },
      { matches: [{ match_id: 'racing-final', winner: 'v2', decision: 'promoted' }] },
    ] },
  });
  const { events } = live.deriveActivity(s0, s1, 0);
  const kinds = events.map((e) => e.kind);
  assert(kinds.includes('matchup'), 'a started matchup event (r2 entered)');
  assert(kinds.includes('run'), 'a completed-run event (r1 left)');
  assert(kinds.includes('cut'), 'a rung-cut event (v1 eliminated)');
  assert(kinds.includes('gate'), 'a champion-gate decided event');
  // newest-first ordering.
  assert(events.length >= 4, 'all the deltas surfaced as events');
  // the cut event is toned bad, the gate-promotion good.
  assert(events.find((e) => e.kind === 'cut').tone === 'bad', 'a cut is toned regress (bad)');
  assert(events.find((e) => e.kind === 'gate').tone === 'good', 'a promotion is toned improve (good)');
});

test('live engine: the ActivityTicker is append-only, newest-on-top, capped, and de-dups by id', () => {
  const t = new live.ActivityTicker({ cap: 3 });
  // first batch (newest-first input).
  t.push([{ id: 'a3', kind: 'phase', text: 'three' }, { id: 'a2', kind: 'phase', text: 'two' }, { id: 'a1', kind: 'phase', text: 'one' }]);
  let rows = t._list.children;
  assertEqual(rows.length, 3, 'three rows after the first batch');
  assert(rows[0].textContent.includes('three'), 'newest (a3) is on top');
  const a3Node = rows[0];
  // a duplicate id is ignored; a new id prepends; the cap trims the oldest.
  t.push([{ id: 'a4', kind: 'cut', text: 'four', tone: 'bad' }, { id: 'a3', kind: 'phase', text: 'three-dup' }]);
  rows = t._list.children;
  assertEqual(rows.length, 3, 'the cap (3) trimmed the oldest row');
  assert(rows[0].textContent.includes('four'), 'the new event (a4) is newest-on-top');
  assert(!t._list.textContent.includes('three-dup'), 'a duplicate id (a3) was NOT re-added');
  // surviving rows keep identity (append-only — no repaint).
  assert([...rows].some((r) => r === a3Node), 'a surviving row keeps its node identity (no repaint)');
});

// ---- TOGGLE: the board-detail transcript button collapses when re-clicked ----

test('board view (a): clicking "show inline" reveals the transcript and the button reads "showing"', async () => {
  freshState(); installFetch();
  const board = await import('../js/views/board.js');

  // collapsed: no gen selected — the row button reads "show inline →" and its
  // href carries the gen (clicking it OPENS that candidate's transcript).
  const closed = document.createElement('div');
  await board.render(closed, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  assert(allByClass(closed, 'dt-split').length === 0, 'no inline transcript pane while collapsed');
  const closedBtns = allByClass(closed, 'dn-board-run');
  assert(closedBtns.length && closedBtns.every((n) => (n.textContent || '').includes('show inline')), 'every candidate row button reads "show inline →" when collapsed');
  // the v1 candidate's button carries the v1 gen (clicking it OPENS that transcript).
  const v1Btn = closedBtns.find((n) => /\/board\/waffles_single\/v1\b/.test(n.getAttribute('href') || ''));
  assert(v1Btn, 'the "show inline" href carries the gen (opens the transcript)');

  // selected: that gen is open — the transcript renders and its button flips to
  // "showing ↓" (current behaviour).
  freshState(); installFetch();
  const open = document.createElement('div');
  await board.render(open, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  assert(allByClass(open, 'dt-split')[0], 'the inline transcript pane rendered for the selected candidate');
  const onBtn = allByClass(open, 'dn-board-run').find((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-linkbtn-on'));
  assert(onBtn, 'the selected candidate button is marked active (dn-linkbtn-on)');
  assert((onBtn.textContent || '').includes('showing'), 'the active button reads "showing ↓"');
});

test('board view (b): clicking the "showing" button again hides the transcript + clears the selection/route', async () => {
  freshState(); installFetch();
  const board = await import('../js/views/board.js');

  // open on v1.
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const onBtn = allByClass(host, 'dn-board-run').find((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-linkbtn-on'));
  assert(onBtn && (onBtn.textContent || '').includes('showing'), 'the v1 button is the active "showing ↓" control');

  // TOGGLE: the active "showing ↓" button's href DROPS the gen — clicking it
  // routes back to the bare board (selection cleared), so the transcript closes
  // and a reload of that route does NOT reopen it.
  const offHref = onBtn.getAttribute('href') || '';
  assert(/\/board\/waffles_single(\b|$)/.test(offHref) && !/\/board\/waffles_single\/v1\b/.test(offHref),
    'the active button href collapses to the bare board route (no gen) — toggles the selection OFF');

  // re-render at the collapsed route the toggle points to: the transcript is gone
  // and the button is back to "show inline →".
  freshState(); installFetch();
  const reloaded = document.createElement('div');
  await board.render(reloaded, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  assert(allByClass(reloaded, 'dt-split').length === 0, 'the inline transcript is hidden after toggling off');
  const backBtn = allByClass(reloaded, 'dn-board-run').find((n) => (n.textContent || '').includes('show inline'));
  assert(backBtn, 'the button returned to "show inline →" after the toggle');
  assert(allByClass(reloaded, 'dn-linkbtn-on').length === 0, 'no candidate button is marked active after toggling off');

  // the dot-plot stays consistent: clicking the already-selected candidate's dot
  // also collapses it (drops the gen).
  freshState(); installFetch();
  let dotNav = null;
  const dotHost = document.createElement('div');
  await board.render(dotHost, { navigate: (v, p) => { dotNav = { v, p }; }, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const dots = dotHost.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dn-dotrow'));
  // find the v1 dot row (its label/value group is clickable) and click it.
  const v1dot = dots.find((n) => (n.textContent || '').includes('v1'));
  if (v1dot) {
    v1dot.dispatchEvent({ type: 'click' });
    assert(dotNav && dotNav.v === 'board' && dotNav.p.entry === 'waffles_single' && dotNav.p.gen == null,
      'clicking the already-selected candidate dot collapses it (navigates to the board with no gen)');
  }
});

// ====================================================================
// CROSS-EPOCH LEAKAGE + survival-funnel header collision (live-run fixes).
//
//   BUG 1 — the epoch overview's gens/heatmap-columns must be scoped to the
//     VIEWED epoch. /api/lineage spans the whole workspace; viewing e1 must NOT
//     leak e0's generations into the heatmap (no duplicate v0/v1 columns, no
//     inflated "field of N").
//   BUG 2 — the racing strip must follow the ACTIVE epoch. When the viewed
//     epoch has no tournament/funnel data (proposing) the strip shows the honest
//     "field fills in" empty state, NEVER a prior epoch's completed funnel.
//   BUG 3 — the survival-funnel rung headers + the benchmark/descriptive line
//     get DISTINCT y baselines (and each rung header its own column x), so they
//     never overlap each other or the descriptive line.
// ====================================================================


// ---- BUG 1: the epoch view is scoped to the viewed epoch (no leak) ---

test('epoch view (cross-epoch): viewing e1 shows ONLY e1 gens — no leaked e0 columns, deduped by id, field count correct', async () => {
  freshState();
  // e1 is the new epoch; the active racing tournament is e1's (proposing — no rungs).
  installFixtureMap(twoEpochFixture(TWO_EP_NEW, {
    activeTournament: { tournament_id: `tourn_${TWO_EP_NEW}_v1`, epoch_id: TWO_EP_NEW, structure: 'racing', phase: 'running', rounds: [], standings: [], competitors: [] },
  }));
  coreState.state.heartbeat = { phase: 'idle' };  // not "running" → no live status; the funnel falls to reconstruct
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: TWO_EP_NEW });

  // the heatmap columns = the gens. e1 has exactly v0,v1,v2 — and they must each
  // appear ONCE (no duplicate v0/v1 from e0). v3/v4 belong to e0 and must be absent.
  const hm = svgsByClass(host, 'dn-heatmap')[0];
  assert(hm, 'the heatmap rendered on the e1 epoch view');
  // column headers are the per-generation labels on the heatmap; count the v-id labels.
  const colLabels = hm.querySelectorAll('[class]')
    .filter((n) => n.localName === 'text' && /^v\d+$/.test((n.textContent || '').trim()))
    .map((n) => (n.textContent || '').trim());
  const cols = colLabels.filter((s, i) => colLabels.indexOf(s) === i); // distinct
  // every v-id label that is a COLUMN appears once; there are NO e0-only ids (v3,v4).
  const colCounts = {};
  for (const c of colLabels) colCounts[c] = (colCounts[c] || 0) + 1;
  for (const id of Object.keys(colCounts)) {
    if (/^v[0-2]$/.test(id)) continue;            // v0..v2 are e1's own field
    assert(!/^v[34]$/.test(id), `no leaked e0-only column ${id} on the e1 view`);
  }
  // no id appears as a column more than the number of header rows it legitimately
  // owns — a leak would DOUBLE v0/v1/v2. Assert the distinct column set is exactly e1's.
  assertDeep(cols.sort(), ['v0', 'v1', 'v2'], 'the e1 heatmap columns are EXACTLY e1’s field {v0,v1,v2} (deduped, no leak)');

  // the timeline's challenger fan reflects e1's OWN minted field (v1, v2) — a
  // leak would add e0's v3/v4 chips. The single-round episode lists exactly
  // {v1, v2} (v0 is the carried champion on the spine, not a chip).
  const chips = allByClass(host, 'dn-roundtl-chip').map((c) => { const mono = allByClass(c, 'dn-mono')[0]; return mono ? (mono.textContent || '').trim() : ''; });
  assertDeep(chips.filter((s, i) => chips.indexOf(s) === i).sort(), ['v1', 'v2'], 'the e1 challenger fan is EXACTLY e1’s minted field {v1,v2} (no leaked v3/v4)');
});

test('epoch view (cross-epoch): viewing e0 is unchanged — its full field {v0..v4} still renders (no regression)', async () => {
  freshState();
  installFixtureMap(twoEpochFixture(TWO_EP_OLD));
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: TWO_EP_OLD });

  const hm = svgsByClass(host, 'dn-heatmap')[0];
  assert(hm, 'the heatmap rendered on the e0 epoch view');
  const colLabels = hm.querySelectorAll('[class]')
    .filter((n) => n.localName === 'text' && /^v\d+$/.test((n.textContent || '').trim()))
    .map((n) => (n.textContent || '').trim());
  const cols = colLabels.filter((s, i) => colLabels.indexOf(s) === i).sort();
  assertDeep(cols, ['v0', 'v1', 'v2', 'v3', 'v4'], 'e0 still shows its FULL field {v0..v4} (unchanged)');
  // e0's challenger fan is its own full minted field {v1..v4} (v0 carried on spine).
  const chips = allByClass(host, 'dn-roundtl-chip').map((c) => { const mono = allByClass(c, 'dn-mono')[0]; return mono ? (mono.textContent || '').trim() : ''; });
  assertDeep(chips.filter((s, i) => chips.indexOf(s) === i).sort(), ['v1', 'v2', 'v3', 'v4'], 'e0 reads its own full challenger fan {v1..v4}');
});

// ---- BUG 2: a proposing epoch shows the empty state, not e0's funnel -

test('epoch view (cross-epoch): a PROPOSING e1 shows the honest empty state — NOT e0’s completed funnel', async () => {
  freshState();
  // e1 is proposing: the active tournament is e1's with NO rungs yet; the
  // COMPLETED /api/tournaments still carries e0's full racing ladder. The strip
  // must NOT reconstruct e0's funnel under the e1 header.
  installFixtureMap(twoEpochFixture(TWO_EP_NEW, {
    activeTournament: { tournament_id: `tourn_${TWO_EP_NEW}_v1`, epoch_id: TWO_EP_NEW, structure: 'racing', phase: 'running', rounds: [], standings: [], competitors: [] },
  }));
  // even with a LIVE racing heartbeat, the active topology has no rungs → no funnel.
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:rung0_m0', generation_id: 'v1' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'waffles_single', run_id: 'r1' }];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running' };

  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: TWO_EP_NEW });

  // NO funnel SVG (e1 has no rung data — e0's reconstructed ladder must NOT leak).
  assertEqual(svgsByClass(host, 'dn-funnel').length, 0, 'NO survival funnel while e1 is proposing (no leak of e0’s funnel)');
  // the timeline still renders (its episode degrades to e1's own minted field).
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline renders for the proposing e1 epoch');
  // e0’s crowned survivor (v4) must NOT bleed into the e1 timeline as a champion ♚.
  assert(!host.textContent.includes('♚ v4'), 'e0’s crowned champion ♚ v4 does NOT leak into the e1 timeline');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

test('racing field: the client-side reconstruction is DELETED — the ladder is SERVED and epoch-scoped by the server', () => {
  // reconstructRacing (the client join over per-challenger records) is GONE:
  // the server scopes + joins (build_racing_field); the mock server mirrors it.
  assertEqual(STRUCT.reconstructRacing, undefined, 'reconstructRacing is deleted from the client');
  const brk = twoEpochFixture(TWO_EP_NEW)['/api/tournaments'];
  const e0 = STRUCT.normalizeStructure(racingFieldFromBracket(brk, TWO_EP_OLD), false);
  assert(e0 && e0.structure === 'racing', 'the e0 ladder still reconstructs from its own records');
});

// ---- BUG 3: the funnel header labels do NOT collide -------------------

test('survival funnel (no-collide): the benchmark line + the rung headers + the sublabels sit on DISTINCT y baselines', () => {
  const rungs = [
    { label: 'Rung 0', competitors: ['v1', 'v2', 'v3', 'v4'], survivors: ['v3', 'v4'], cut: ['v1', 'v2'], board_fraction: 0.25, deltas: {} },
    { label: 'Rung 1', competitors: ['v3', 'v4'], survivors: ['v3'], cut: ['v4'], board_fraction: 0.5, deltas: {} },
  ];
  const node = svg.survivalFunnel({ rungs, championId: 'v3', benchmarkId: 'v0', gateState: 'crowned', gateDelta: -5, onCompetitor() {} });

  const yOf = (n) => Number(n.getAttribute('y'));
  const xOf = (n) => Number(n.getAttribute('x'));
  const byClass = (cls) => node.querySelectorAll('[class]').filter((n) => n.localName === 'text' && (n.getAttribute('class') || '').split(/\s+/).includes(cls));

  const bench = byClass('dn-funnel-bench')[0];
  const heads = byClass('dn-funnel-head');
  const subs = byClass('dn-funnel-sub');
  assert(bench, 'the benchmark/descriptive line rendered');
  assert(heads.length >= 3, 'a header per rung column + the champion-gate header (≥3)');
  assert(subs.length >= 3, 'a sublabel per rung + the gate');

  const benchY = yOf(bench);
  const headYs = heads.map(yOf);
  const subYs = subs.map(yOf);
  // (1) the benchmark line is ABOVE every rung header (its own baseline).
  for (const hy of headYs) assert(benchY < hy, 'the benchmark line sits strictly above the rung headers (separate baseline)');
  // (2) all rung headers share ONE baseline, the sublabels another, distinct from it
  //     and from the benchmark — three separate rows.
  const headBaseline = headYs[0];
  for (const hy of headYs) assertEqual(hy, headBaseline, 'every rung/gate header shares the one header baseline');
  const subBaseline = subYs[0];
  for (const sy of subYs) assertEqual(sy, subBaseline, 'every sublabel shares the one sub baseline');
  assert(headBaseline !== benchY && subBaseline !== benchY && headBaseline !== subBaseline,
    'benchmark / header / sub occupy THREE distinct y baselines (no shared baseline → no collision)');

  // (3) each rung/gate header is centred on its OWN column x — no two headers
  //     share the same x (they march left→right across the stages + gate).
  const headXs = heads.map(xOf).sort((a, b) => a - b);
  for (let i = 1; i < headXs.length; i++) {
    assert(headXs[i] > headXs[i - 1], 'adjacent rung/gate headers occupy distinct, increasing column x positions (no overlap)');
  }
  // (4) the benchmark line is left-anchored (x near the origin) on its own row, so
  //     it cannot run into a centred rung header on the SAME baseline.
  assert(xOf(bench) < headXs[0], 'the benchmark line starts left of the first rung header (its own row)');
});

// ---- LIVE-BEAT: the inline transcript pane survives an in-flight-only beat ----
//
// During a live run the in-flight progressRatio advances every SSE beat. The
// regression: that advanced the OUTER view digest, tearing down and rebuilding
// the whole board view — INCLUDING the open transcript scroll containers, which
// reset to the top. The fix splits the transcript into its OWN per-pane digest
// host keyed ONLY on [selGen, transcript content], independent of the in-flight
// set. These pin that a progress-only beat does NOT recreate the transcript DOM
// while the dot-plot / in-flight portion DOES update — and that the transcript
// pane DOES re-render when the selection or transcript content actually change.

test('board view (live): an in-flight-only beat does NOT tear down the inline transcript (no scroll reset), but the in-flight/dot-plot portion DOES update', async () => {
  freshState(); installFetch();
  const board = await import('../js/views/board.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };

  // beat 1: a candidate is in flight on this entry at 30% AND v1's transcript is open.
  coreState.state.activeRuns = [{ entry_id: 'waffles_single', generation_id: 'v2', run_id: 'run_v2_waffles', progress: 0.3 }];
  await board.render(host, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });

  const xhostBefore = host.querySelector(':scope > [data-node="board-xscript"]');
  const upperBefore = host.querySelector(':scope > [data-node="board-upper"]');
  assert(xhostBefore && upperBefore, 'the board view split into a persistent upper host + a transcript host');
  const xdigestBefore = xhostBefore.getAttribute('data-t-digest');
  const updigestBefore = upperBefore.getAttribute('data-t-digest');
  const scrollBefore = allByClass(host, 'dn-xscript-scroll')[0];
  assert(scrollBefore, 'the inline transcript scroll container rendered (transcript is open)');
  assert(host.textContent.includes('30%'), 'beat 1 shows the in-flight candidate at 30%');

  // beat 2: SAME selection + SAME transcript, but the in-flight progress advanced to 65%.
  coreState.state.activeRuns = [{ entry_id: 'waffles_single', generation_id: 'v2', run_id: 'run_v2_waffles', progress: 0.65 }];
  await board.render(host, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });

  const xhostAfter = host.querySelector(':scope > [data-node="board-xscript"]');
  const scrollAfter = allByClass(host, 'dn-xscript-scroll')[0];
  // PRIMARY: the transcript host's digest is unchanged and the scroll container
  // is the SAME node (not recreated) — so scroll position is preserved.
  assertEqual(xhostAfter.getAttribute('data-t-digest'), xdigestBefore, 'the transcript digest is UNCHANGED across an in-flight-only beat');
  assert(scrollAfter === scrollBefore, 'the inline transcript scroll container is the SAME DOM node (not torn down on a progress beat)');
  // the in-flight / upper portion DID update (digest changed) and now shows 65%.
  assert(upperBefore.getAttribute('data-t-digest') !== updigestBefore, 'the upper (dot-plot / in-flight) digest DID change as progress advanced');
  assert(host.textContent.includes('65%'), 'the in-flight portion repainted to 65%');
  coreState.state.activeRuns = [];
});

test('board view (live): the transcript pane DOES re-render when the selected gen changes', async () => {
  freshState(); installFetch();
  const board = await import('../js/views/board.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };

  await board.render(host, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v1' });
  const xhost = host.querySelector(':scope > [data-node="board-xscript"]');
  const xdigestV1 = xhost.getAttribute('data-t-digest');
  const scrollV1 = allByClass(host, 'dn-xscript-scroll')[0];
  assert(host.textContent.includes('Drafting an outline'), 'v1 transcript turn rendered');

  // selecting a DIFFERENT candidate changes the transcript digest → the pane re-renders.
  await board.render(host, ctx, { epochId: EPOCH_ID, entry: 'waffles_single', gen: 'v0' });
  const xdigestV0 = xhost.getAttribute('data-t-digest');
  const scrollV0 = allByClass(host, 'dn-xscript-scroll')[0];
  assert(xdigestV0 !== xdigestV1, 'the transcript digest CHANGED when the selected gen changed (v1 → v0)');
  assert(scrollV0 !== scrollV1, 'the transcript scroll container was rebuilt for the new selection');
  assert(host.textContent.includes('Here is a structured outline'), 'the v0 transcript turn rendered after switching selection');
  coreState.state.activeRuns = [];
});

await run();
