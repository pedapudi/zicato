// test/live_surface.test.mjs — the console's live surface:
// live-status for any structure, structure-aware polish, the progressive
// live racing/swiss/elim models, projected standings, and the racing-ladder
// reconstruction from per-challenger match records.
//
// Shared fixtures and helpers live in ./fixtures.mjs.

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';
import { elimPayload, recorded } from './recorded.mjs';

installDom();

const {
  router, svg, ui, shell, data, tree,
  livestatus, coreState, rounds, live, STRUCT,
  EPOCH_ID, installFetch, freshState, allByClass, svgsByClass, mountLiveShell,
  SE_STRUCT, SWISS_STRUCT, RACING_STRUCT, structureFixture, racingLadderFixture, installFixtureMap, LIVE_RACING,
  liveRacingField, liveElimField, RC_EPOCH, HERO_EPOCH,
} = await import('./fixtures.mjs');

// ====================================================================
// LIVE-STATUS — surfacing an ACTIVE run for ANY tournament structure
// (the gauntlet-shaped status pill missed live racing/swiss/elim runs).
// ====================================================================

// ---- (a) a non-idle heartbeat + active-runs + active-tournament running ----

test('live-status: a live RACING run (non-idle phase + in-flight runs + tournament running) shows a RUNNING state with structure + phase', () => {
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m3', generation_id: 'v3', round_index: 0, epoch_id: EPOCH_ID },
    activeRuns: Array.from({ length: 14 }, (_, i) => ({ generation_id: 'v' + (i % 4), entry_id: 'b' + i, run_id: 'run_' + i, progress: 0.3 })),
    activeTournament: { structure: 'racing', phase: 'running', competitors: [{ generation_id: 'v0' }], rounds: [], standings: [] },
  });
  assertEqual(status.running, true, 'a live racing run reads as RUNNING (not "nothing is running")');
  assertEqual(status.structure, 'racing', 'the structure is surfaced from active-tournament');
  assertEqual(status.inFlight, 14, 'the in-flight board-unit count is surfaced');
  assertEqual(status.tournamentRunning, true, 'active-tournament phase "running" corroborates');
  assert(status.label.includes('racing'), 'the readable label names the structure (racing)');
  assert(status.label.includes('rung 0'), 'the readable label derives the rung from the phase string');
});

test('live-status: a FRESH heartbeat phase ALONE (proposing) lights the running state even before a tournament exists', () => {
  const now = 1_000_000_000_000;
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'proposing:field', generation_id: null, ts: now - 2_000 /* fresh */ },
    activeRuns: [],
    activeTournament: null,
  }, now);
  assertEqual(status.running, true, 'a fresh non-idle proposing phase ⇒ running even with no tournament + no active-runs');
  assert(status.label.includes('proposing'), 'the proposing phase yields a readable "proposing …" label');
  assertEqual(status.inFlight, 0, 'no board-units in flight during proposing');
});

test('live-status: in-flight active-runs alone (no heartbeat) still read as running, structure-agnostic', () => {
  const status = livestatus.deriveLiveStatus({
    heartbeat: null,
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.5 }],
    activeTournament: { structure: 'swiss', phase: 'running' },
  });
  assertEqual(status.running, true, 'a non-empty active-runs feed alone reads as running');
  assertEqual(status.structure, 'swiss', 'the structure is taken from active-tournament');
});

// ---- (b) idle heartbeat + empty active-runs ⇒ idle/done ----

test('live-status: an IDLE heartbeat + empty active-runs + null tournament shows idle/done (not running)', () => {
  const idle = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'idle' }, activeRuns: [], activeTournament: null,
  });
  assertEqual(idle.running, false, 'an idle phase + nothing in flight is NOT running');
  assertEqual(idle.inFlight, 0, 'no in-flight units when idle');
  assertEqual(idle.label, 'idle', 'the idle label reads "idle"');

  const doneTok = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'done' }, activeRuns: [], activeTournament: { structure: 'racing', phase: 'complete' },
  });
  assertEqual(doneTok.running, false, 'a done phase + a completed (not "running") tournament is NOT running');

  // a fully-absent set (no heartbeat, no runs, no tournament) → "done".
  const empty = livestatus.deriveLiveStatus({});
  assertEqual(empty.running, false, 'an empty environment is not running');
  assertEqual(empty.label, 'done', 'an empty environment reads "done"');
});

test('live-status: isActivePhase distinguishes running phases from idle/terminal ones', () => {
  assertEqual(livestatus.isActivePhase('tournament:round_0:rung0_m3'), true, 'a tournament phase is active');
  assertEqual(livestatus.isActivePhase('proposing:field'), true, 'a proposing phase is active');
  assertEqual(livestatus.isActivePhase('idle'), false, 'idle is not active');
  assertEqual(livestatus.isActivePhase('done'), false, 'done is not active');
  assertEqual(livestatus.isActivePhase(''), false, 'an empty phase is not active');
  assertEqual(livestatus.isActivePhase(null), false, 'an absent phase is not active');
});

// ---- (a′) terminal-TAIL phases read as idle (the false-LIVE bug) ----

test('live-status: isActivePhase treats terminal-TAIL phase paths as idle', () => {
  // the terminal signal lives in the tail segment rather than the head.
  assertEqual(livestatus.isActivePhase('evolve_n_rounds:done'), false, 'evolve_n_rounds:done is terminal (tail = done)');
  assertEqual(livestatus.isActivePhase('tournament:round_0:done'), false, 'a tournament path ending in :done is terminal');
  assertEqual(livestatus.isActivePhase('evolve_n_rounds:completed'), false, 'a :completed tail is terminal');
  // an active phase keeps no idle token in any segment.
  assertEqual(livestatus.isActivePhase('tournament:round_0:rung0_m3'), true, 'an in-flight tournament rung is active');
  assertEqual(livestatus.isActivePhase('proposing:field'), true, 'a proposing phase is active');
});

// ---- (a″) heartbeat-staleness gates the live read ----

test('live-status: a STALE heartbeat + terminal phase + 0 runs + completed tournament reads idle/done (no false LIVE)', () => {
  // mirrors the real completed-run case: frozen terminal heartbeat from a
  // finished process, no in-flight units, a completed (not running) tournament.
  const now = 1_000_000_000_000; // fixed epoch ms for determinism
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'evolve_n_rounds:done', ts: now - 120_000 /* 2 min old */ },
    activeRuns: [],
    activeTournament: { structure: 'racing', phase: 'completed' },
  }, now);
  assertEqual(status.running, false, 'a stale terminal heartbeat with nothing in flight is NOT live');
  assert(status.label === 'idle' || status.label === 'done', 'a finished run reads idle/done, not a running label');
});

test('live-status: a FRESH heartbeat + active phase reads as RUNNING', () => {
  const now = 1_000_000_000_000;
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m3', ts: now - 2_000 /* 2s old, fresh */ },
    activeRuns: [],
    activeTournament: { structure: 'racing', phase: 'running' },
  }, now);
  assertEqual(status.running, true, 'a fresh heartbeat on an active phase is live');
});

test('live-status: an in-flight active-run forces RUNNING even with an old timestamp (ground truth)', () => {
  const now = 1_000_000_000_000;
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'evolve_n_rounds:done', ts: now - 600_000 /* 10 min old */ },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.5 }],
    activeTournament: null,
  }, now);
  assertEqual(status.running, true, 'an actively-running unit is ground truth — live even if the heartbeat looks dead');
  assertEqual(status.inFlight, 1, 'the in-flight unit is counted');
});

test('live-status: a completed active-tournament ALONE (no fresh phase, no runs) does NOT read live', () => {
  const now = 1_000_000_000_000;
  const status = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'idle', ts: now - 5_000 },
    activeRuns: [],
    activeTournament: { structure: 'swiss', phase: 'completed' },
  }, now);
  assertEqual(status.running, false, 'a completed tournament is not a running signal');
});

test('live-status: a heartbeat with NO parseable timestamp reads NOT live (missing ts ⇒ stale, never default-to-live)', () => {
  // a realistic 2026 epoch-ms `now` (>1e12) so a numeric `last_heartbeat`
  // delta is read as ms rather than rescaled from "seconds".
  const now = 1_780_455_964_000;
  // THE BUG: a killed run leaves a heartbeat whose ts cannot be parsed. It must
  // NOT default to live off an active phase — a heartbeat that cannot be aged
  // out is stale rather than fresh.
  const noTs = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:final' }, activeRuns: [], activeTournament: null,
  }, now);
  assertEqual(noTs.running, false, 'an active phase with NO timestamp reads NOT live (missing ts ⇒ stale)');
  assertEqual(noTs.heartbeatStale, true, 'a heartbeat with no parseable timestamp is flagged stale');
  // a garbage timestamp is likewise unparseable ⇒ stale ⇒ not live.
  const badTs = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m3', ts: 'not-a-date' }, activeRuns: [], activeTournament: null,
  }, now);
  assertEqual(badTs.running, false, 'an unparseable timestamp reads NOT live (stale)');
});

test('live-status (clean break): ONLY the typed ms-epoch `ts` ages the heartbeat — the alias keys + magnitude guessing are DELETED', () => {
  const now = 1_780_455_964_000;
  // the retired alternate keys (last_heartbeat / emitted_at / updated_at)
  // carry perfectly-fresh timestamps — they must NOT read live any more (the
  // server stamps `ts`; a payload without it has no ageable timestamp).
  for (const key of ['last_heartbeat', 'emitted_at', 'updated_at']) {
    const st = livestatus.deriveLiveStatus({
      heartbeat: { phase: 'tournament:round_0', [key]: new Date(now - 1000).toISOString() },
      activeRuns: [], activeTournament: null,
    }, now);
    assertEqual(st.running, false, `a fresh ${key} WITHOUT ts reads stale (the alias is dead)`);
  }
  // an ISO string in `ts` is NOT parsed — ts is a typed NUMBER (ms).
  const iso = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0', ts: new Date(now - 1000).toISOString() },
    activeRuns: [], activeTournament: null,
  }, now);
  assertEqual(iso.running, false, 'an ISO-string ts is not a typed ms number → stale');
  // a SECONDS-magnitude value is NOT scaled up to ms (the guessing is gone):
  // it reads as an ancient ms timestamp → stale.
  const secs = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0', ts: Math.floor((now - 1000) / 1000) },
    activeRuns: [], activeTournament: null,
  }, now);
  assertEqual(secs.running, false, 'a seconds-magnitude ts is NOT rescaled — the ms contract is typed');
});

test('live-status: a heartbeat OLDER than STALE_HEARTBEAT_MS reads NOT live (the one staleness rule)', () => {
  const now = 1_780_455_964_000;
  // just past the staleness window on an otherwise-active phase ⇒ not live.
  const old = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:final', ts: now - (livestatus.STALE_HEARTBEAT_MS + 1_000) },
    activeRuns: [],
    activeTournament: null,
  }, now);
  assertEqual(old.running, false, 'an active phase with a too-old timestamp reads NOT live');
  assertEqual(old.heartbeatStale, true, 'a too-old heartbeat is flagged stale');
  // a FRESH timestamp on the same active phase reads live (the positive case).
  const fresh = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:final', ts: now - 2_000 },
    activeRuns: [],
    activeTournament: null,
  }, now);
  assertEqual(fresh.running, true, 'a fresh timestamp on an active phase reads LIVE');
  assertEqual(fresh.heartbeatStale, false, 'a fresh heartbeat is not stale');
});

test('live-status: a DEAD run (stale phase + frozen active_tournament phase:running, 0 in-flight) reads NOT live — the repro', () => {
  const now = 1_780_455_964_000;
  // the exact on-disk shape a killed run leaves: a stale heartbeat with an
  // active-looking phase, an orphaned active_tournament.events.jsonl still saying
  // phase:"running", and ZERO in-flight board-units. The frozen tournament
  // file must NOT keep it live now that the orchestrator heartbeat is stale.
  const dead = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:final', generation_id: 'v3', epoch_id: '2026-06-03_e3', ts: now - 800_000 /* ~13 min */ },
    activeRuns: [],
    activeTournament: { structure: 'single_elim', phase: 'running' },
  }, now);
  assertEqual(dead.running, false, 'a dead run with a frozen running-tournament file but a stale heartbeat is NOT live');
  assertEqual(dead.tournamentRunning, true, 'the frozen tournament file is still reported as phase:running');
  assert(dead.label === 'idle' || dead.label === 'done', 'the dead run reads idle/done, not a running label');
});

test('live-status: a stale run exposes a heartbeat AGE + a "last seen Ns ago" affordance (not a silent freeze)', () => {
  const now = 1_780_455_964_000;
  const dead = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:racing-final', ts: now - 90_000 /* 90s */ },
    activeRuns: [],
    activeTournament: { structure: 'racing', phase: 'running' },
  }, now);
  assertEqual(dead.running, false, 'a 90s-old heartbeat reads NOT live');
  assertEqual(dead.heartbeatStale, true, 'flagged stale');
  assertEqual(dead.heartbeatAgeMs, 90_000, 'the heartbeat age is exposed for the affordance');
  assertEqual(livestatus.staleLabel(dead.heartbeatAgeMs), 'last seen 90s ago',
    'the affordance reads "last seen 90s ago"');
  // minutes / hours bucketing + the untimestamped fallback.
  assertEqual(livestatus.staleLabel(125_000), 'last seen 2m ago', 'minutes bucket');
  assertEqual(livestatus.staleLabel(2 * 3600_000 + 5000), 'last seen 2h ago', 'hours bucket');
  assertEqual(livestatus.staleLabel(NaN), 'stale', 'an untimestamped frozen heartbeat reads a bare "stale"');
});

test('live-status: the chrome shows the stale affordance + a non-running dot when the heartbeat freezes (no LIVE banner)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const now = Date.now();
  coreState.state.connected = true;
  coreState.state.connecting = false;
  // a FROZEN heartbeat: an active-looking phase + a running tournament file,
  // but a last_heartbeat well past the staleness window → NOT live.
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:racing-final', generation_id: 'v3', ts: now - 120_000 });
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running', competitors: [{ generation_id: 'v0' }] };

  const root = mountLiveShell('#/');

  const statusEl = allByClass(root, 'dt-status')[0];
  assert(statusEl, 'the chrome status pill mounted');
  const cls = statusEl.getAttribute('class') || '';
  assert(!cls.split(/\s+/).includes('dt-running'), 'a frozen heartbeat does NOT light the running (LIVE) chrome');
  assert(cls.split(/\s+/).includes('dt-stale'), 'the chrome carries the dt-stale class');
  const staleEl = allByClass(root, 'dt-status-stale')[0];
  assert(staleEl && /last seen/.test(staleEl.textContent),
    'the chrome shows a "last seen Ns ago" affordance rather than a silent freeze');
  // the live run label must NOT read as running.
  const runLabel = allByClass(root, 'dt-run-label')[0];
  assert(!runLabel || runLabel.textContent.trim() === '', 'no running label for a stale run');

  // reset to an idle environment so other tests start clean.
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// ---- (b′) the chrome status pill reflects the running state, digest-gated ----

test('live-status: the chrome RUN badge lights for a live racing run and the status digest is gated (no flash)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  // seed the shared AppState with a live racing run BEFORE mounting the shell.
  coreState.state.connected = true;
  coreState.state.connecting = false;
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:rung0_m3', generation_id: 'v3' });
  coreState.state.activeRuns = [
    { generation_id: 'v1', entry_id: 'waffles_single', run_id: 'r1', progress: 0.4 },
    { generation_id: 'v2', entry_id: 'waffles_single', run_id: 'r2', progress: 0.2 },
  ];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running' };

  const root = mountLiveShell('#/');
  const statusEl = allByClass(root, 'dt-status')[0];
  assert(statusEl, 'the chrome status pill rendered');
  assert((statusEl.getAttribute('class') || '').includes('dt-running'), 'the status pill carries the dt-running state for a live racing run');
  const label = allByClass(root, 'dt-run-label')[0];
  assert(label && label.textContent.includes('racing'), 'the run badge names the structure (racing)');
  const count = allByClass(root, 'dt-run-count')[0];
  assert(count && count.textContent.includes('2'), 'the run badge shows the in-flight board-unit count (2)');

  // DIGEST-GATE: a steady heartbeat re-tick with IDENTICAL live signals must not
  // rewrite the badge text node (no flash). The same derived verdict ⇒ no DOM.
  const labelNodeBefore = label.firstChild;
  coreState.state._changed();           // a heartbeat-style re-tick, same data.
  assert(allByClass(root, 'dt-run-label')[0].firstChild === labelNodeBefore,
    'an unchanged live status is a digest no-op — the run-label text node is not rewritten');

  // reset to an idle environment so other tests start clean.
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// ---- (c) board-detail surfaces the in-flight runs for an entry ----

test('board view: an entry mid-run with NO completed results renders its in-flight candidates as breakdown rows with a live progress column (not empty)', async () => {
  freshState(); installFetch();
  // no completed per-entry rows for this fresh entry, but two runs are live on it.
  const board = await import('../js/views/board.js');
  coreState.state.activeRuns = [
    { generation_id: 'v3', entry_id: 'waffles_single', run_id: 'run_v3_waffles', progress: 0.65 },
    { generation_id: 'v4', entry_id: 'waffles_single', run_id: 'run_v4_waffles', progress: 0.1 },
    { generation_id: 'v5', entry_id: 'some_other_entry', run_id: 'run_v5_other', progress: 0.5 },
  ];
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });

  // C4: the running candidates surface as RUNNING rows in the breakdown table
  // with a live-gated progress column — the separate in-flight table was merged.
  assertEqual(allByClass(host, 'dn-board-inflight').length, 0, 'no separate in-flight table — merged into the breakdown (C4)');
  assert(allByClass(host, 'dn-board-table').length >= 1, 'the breakdown table renders the running candidates');
  assert(host.textContent.includes('2 running'), 'the breakdown title reads "2 running" (filtered to THIS entry)');
  assert(host.textContent.includes('v3') && host.textContent.includes('v4'), 'both in-flight candidates on this entry are listed');
  assert(!host.textContent.includes('v5'), 'a run on a DIFFERENT entry is excluded');
  const fills = allByClass(host, 'dn-progress-fill');
  assert(fills.length >= 2, 'each in-flight candidate shows a progress bar in the progress column');
  assert(host.textContent.includes('65%'), 'a candidate progress percentage is surfaced');

  coreState.state.activeRuns = [];
});

test('board view: a run that has ENDED reads a full progress bar, not the wall-clock fraction it stopped at', async () => {
  // Issue #359. A run still listed in the active-tournament payload after it
  // reached a terminal state has done all its work; its progress is 100%
  // whatever share of the wall-clock budget it happened to use. Reading the
  // budget fraction instead paints a finished run as barely started — worst
  // for a run that finished fast against a generous budget, which is the
  // shape asserted here (12s of a 60s budget = the stale 20%).
  freshState(); installFetch();
  const board = await import('../js/views/board.js');
  coreState.state.activeRuns = [
    // terminal by STATUS token, no explicit progress: falls through to elapsed/budget.
    { generation_id: 'v7', entry_id: 'waffles_single', run_id: 'run_v7', status: 'done',
      elapsed_seconds: 12, budget_seconds: 60 },
    // terminal by BOARD COUNT (every board landed), same stale fraction available.
    { generation_id: 'v8', entry_id: 'waffles_single', run_id: 'run_v8',
      boards_done: 4, boards_total: 4, elapsed_seconds: 12, budget_seconds: 60 },
  ];
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });

  const widths = allByClass(host, 'dn-progress-fill')
    .map((n) => ((n.getAttribute('style') || '').match(/width:\s*(\d+)%/) || [])[1]);
  assert(widths.length >= 2, 'both ended runs render a progress bar');
  assertDeep(widths.slice(0, 2), ['100', '100'],
    'an ended run fills its bar completely — terminal by status token and by board count alike');
  assert(host.textContent.includes('100%'), 'the percent readout agrees with the bar');
  assert(!host.textContent.includes('20%'), 'the wall-clock fraction it stopped at is not shown');

  coreState.state.activeRuns = [];
});

test('board view: the inflightForEntry filter matches the CANONICAL entry_id ONLY — the alias keys are dead', async () => {
  const runs = [
    { generation_id: 'v1', entry_id: 'e1', run_id: 'r1' },
    // the retired alias spellings NO server writes — they must NOT match.
    { generation_id: 'v2', board_entry_id: 'e1', run_id: 'r2' },
    { generation_id: 'v3', entry: 'e1', run_id: 'r3' },
    { generation_id: 'v4', entry_id: 'e2', run_id: 'r4' },
  ];
  const b = await import('../js/views/board.js');
  assertEqual(b.inflightForEntry(runs, 'e1').length, 1, 'ONLY the canonical entry_id spelling matches (aliases deleted)');
  assertEqual(b.inflightForEntry(runs, 'nope').length, 0, 'no match for an unknown entry');
  assertEqual(b.inflightForEntry(null, 'e1').length, 0, 'a null active-runs feed yields no in-flight runs');
});

// ---- (d) board-detail still renders completed results for finished runs ----

test('board view: an entry WITH completed results still renders the per-candidate breakdown (regression)', async () => {
  freshState(); installFetch();
  coreState.state.activeRuns = [];   // nothing live — pure completed view.
  const board = await import('../js/views/board.js');
  const host = document.createElement('div');
  await board.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, entry: 'waffles_single' });
  assert(host.textContent.includes('Board · waffles_single'), 'the completed-results board view still renders');
  assert(allByClass(host, 'dn-board-table').length >= 1, 'the completed per-candidate breakdown table still renders');
  assertEqual(allByClass(host, 'dn-board-inflight').length, 0, 'no in-flight panel when nothing is live on the entry');
});

// ====================================================================
// STRUCTURE-AWARE polish (round: tournament structures render correctly
// both DURING a live run and after).
//   (a) the epoch round timeline leads with the per-round structure figure;
//   (b) a LIVE /api/active-tournament fills the ladder (not "nothing ran")
//       and in-flight competitors are not mislabeled rejected;
//   (c) the richer racing ladder renders rungs with cut/survivor + board
//       fraction + a champion-gate;
//   (d) only the CURRENT champion (last in champion_lineage) is badged
//       "champion ♚"; FORMER champions get a distinct "former" marker.
// ====================================================================

// ---- (a) the epoch round timeline subsumes the reel + structure strip --

test('epoch timeline: a NON-gauntlet (racing) epoch leads with the round timeline (one renderer for all structures, no separate strip)', async () => {
  freshState();
  installFixtureMap(structureFixture('racing_field'));
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // ONE timeline, for every structure — no separate reel or structure strip.
  assert(allByClass(host, 'dn-roundtl')[0], 'the round timeline rendered for a racing epoch');
  // a single-round epoch degrades to just its episode — the spine + waterfall
  // (a trajectory across ≥2 rounds) are correctly omitted, so the figure is not
  // a lonely point floating in empty space.
  assert(allByClass(host, 'dn-roundtl-ep')[0], 'the timeline renders the round episode (single round → no empty spine)');
  assertEqual(allByClass(host, 'dn-roundtl-spine').length, 0, 'a single-round epoch shows NO champion spine (nothing to plot a trajectory over)');
  assertEqual(allByClass(host, 'tr-reel').length, 0, 'NO gauntlet champion-spine reel for a racing epoch');
  assert(host.textContent.includes('Racing'), 'the timeline names the racing structure');
  // the epoch overview structure is otherwise unchanged (objective + brief).
  assert(host.textContent.includes('objective'), 'the epoch overview keeps its objective block');
});

test('epoch timeline: a GAUNTLET epoch renders the round timeline (a single episode for --rounds 1), NOT the old reel', async () => {
  freshState(); installFetch();   // the default gauntlet fixture (no tournament block)
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  assert(allByClass(host, 'dn-roundtl')[0], 'the gauntlet epoch renders the round timeline');
  assert(allByClass(host, 'dn-roundtl-spine')[0], 'the timeline keeps its champion spine');
  assertEqual(allByClass(host, 'tr-reel').length, 0, 'the old reel is gone (subsumed)');
  // a gauntlet single round → no embedded structure figure (the fan tells the story).
  assertEqual(allByClass(host, 'dn-funnel').length, 0, 'NO structure figure embedded for a gauntlet round');
});

// ---- (b) a LIVE active-tournament fills the ladder during a run -----


test('live tournament: during a racing RUN the match-ups ladder fills from /api/active-tournament (not "nothing ran")', async () => {
  freshState();
  // the COMPLETED record is empty (the run has not committed any tournament);
  // only the LIVE active-tournament carries the topology.
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g', tournament: { structure: 'racing', params: LIVE_RACING.structure_params }, experiments: [], board: [] },
    '/api/lineage': { generations: LIVE_RACING.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: EPOCH_ID, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: false })) },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure: 'racing', champion_lineage: [], matchups: [], tournaments: [] },
    '/api/active-tournament': LIVE_RACING,
  };
  installFixtureMap(F);
  // seed the live signals so deriveLiveStatus() reports a running racing run.
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running' };

  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the funnel rendered from the LIVE record — NOT an empty "nothing ran" state.
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'the survival funnel rendered from the live active-tournament');
  assert(!/No tournament has run|unavailable/i.test(host.textContent), 'NOT the empty "nothing ran yet" state during a live run');
  assert(allByClass(host, 'dt-live-pill')[0], 'a LIVE badge marks the in-flight tournament');
  assert(host.textContent.includes('Rung 1') && host.textContent.includes('Rung 2'), 'both rungs (incl. the still-racing one) render');

  // the eventual winner v1 is NOT mislabeled rejected/eliminated mid-run: the
  // live standings show everyone racing, not "eliminated".
  assert(!host.textContent.includes('eliminated'), 'no competitor is mislabeled "eliminated" during a live run');
  // v1 is NOT struck through as a cut runner anywhere (it survives rung 1, races rung 2).
  const cutNames = allByClass(host, 'dn-out');
  for (const n of cutNames) assert((n.textContent || '').indexOf('v1') < 0, 'the eventual winner v1 is never struck through (cut) mid-run');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// ====================================================================
// Progressive racing rounds with active board progress.
//
// The backend now PUBLISHES the live tournament topology on
// /api/active-tournament DURING the run: `rounds` with each rung's matches (an
// in-flight rung's match carries no survivors/cut + pending). The unified live
// model consumes the published rounds verbatim and OVERLAYS the per-board
// PROGRESS that still lives in /api/active-runs (per-lane k/N + a partial Δ),
// then carries committed rungs through untouched (ACCUMULATION).
// ====================================================================


// (a) published rung-0 (pending) → the field + rung-0 lanes render with progress
// overlaid from active-runs; NOT the "being seeded" empty state.
test('live racing model: published rung-0 (pending) renders the field with active-runs progress overlaid — not the "being seeded" empty state', () => {
  const at = liveRacingField();
  const model = STRUCT.buildLiveModel(
    at,
    { phase: 'tournament:round_0:rung0_m1', generation_id: 'v5' },
    [
      { generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.4 },
      { generation_id: 'v6', entry_id: 'b1', run_id: 'r1', progress: 0.9 },
    ],
    ['v0', 'v5', 'v6', 'v7', 'v8'],
  );
  assert(model, 'a live racing model was built from the published rounds');
  assertEqual(model.live, true, 'the model is marked live');
  const rungRounds = model.rounds.filter((r) => String(r.matches[0].match_id) !== 'racing-final');
  assert(rungRounds.length >= 1, 'at least rung 0 is present from the published rounds');
  const r0 = rungRounds[0].matches[0];
  assertDeep([...r0.competitors].sort(), ['v5', 'v6', 'v7', 'v8'], 'rung-0 field is the published challenger set (champion v0 is the benchmark, not a lane)');
  assert(r0.live_progress && r0.live_progress.v5 && r0.live_progress.v6, 'rung-0 carries per-lane live progress overlaid from active-runs');
  assertEqual(r0.queued, false, 'rung-0 is the ACTIVE rung (not queued)');

  // it renders rung-0 lanes (not the empty state) when fed to renderStructure.
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'a survival funnel SVG rendered from the published-rounds live model');
  assert(!/being seeded/i.test(host.textContent), 'NOT the "being seeded" empty state once the field exists');
  for (const id of ['v5', 'v6', 'v7', 'v8']) assert(ladder.textContent.includes(id), 'rung-0 names the live challenger lane — ' + id);
});

// (b) rung-0 partially done (active-runs at <1.0) → lanes show "k/N boards".
test('live racing model: an in-flight rung shows per-lane "k/N boards" progress + a partial Δ', () => {
  // the backend writes `partial_*_agg` as DICTS ({scalar, ...}); the model
  // reads `.scalar` (the dead `svg.isNum(dict)` plumbing has been fixed).
  const at = liveRacingField({ partial_champion_agg: { scalar: 12.0 }, partial_challenger_agg: { scalar: 9.5 } });
  at.rounds[0].matches[0].live_progress.v5.partialDelta = -2.5;
  const model = STRUCT.buildLiveModel(
    at,
    { phase: 'tournament:round_0:rung0_m2', generation_id: 'v5' },
    [
      { generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.5 },  // 1 of 2 board units done-ish
      { generation_id: 'v5', entry_id: 'b1', run_id: 'r1', progress: 0.0 },
    ],
    ['v0', 'v5', 'v6', 'v7', 'v8'],
  );
  const r0 = model.rounds.find((r) => String(r.matches[0].match_id) === 'rung0').matches[0];
  const laneV5 = r0.live_progress.v5;
  assertEqual(laneV5.inflight, 2, 'v5 has two in-flight board units this rung');
  assertEqual(laneV5.total, 2, 'the rung-0 board total is board_size·fraction = 8·0.25 = 2');
  assertEqual(laneV5.partialDelta, -2.5, 'the partial Δ = partial_challenger_agg − partial_champion_agg = 9.5 − 12.0');

  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(/boards/.test(ladder.textContent), 'a live lane reads "k/N boards" (progressive fill, not blank)');
  assert(allByClass(ladder, 'dn-funnel-bar').length >= 1, 'a per-lane in-flight progress bar renders');
  assert(!/✕/.test(ladder.textContent), 'a mid-run lane is NOT struck through as cut');
});

// (c) rung-0 complete (rounds has rung0) → survivors ↑ / cuts ✗; then rung-1
// starts (new active-runs) → rung-0's completed result is STILL present.
test('live racing model: a completed rung ACCUMULATES — when rung-1 starts, rung-0 survivors/cuts persist (not discarded)', () => {
  // rung-0 has COMPLETED (committed in the published rounds): v7,v8 survive,
  // v5,v6 cut. The backend now publishes rung-1 (active), with v7,v8 racing.
  const at = liveRacingField({
    round_index: 1,
    rounds: [
      { round_index: 0, label: 'Rung 0', matches: [{ match_id: 'rung0', competitors: ['v5', 'v6', 'v7', 'v8'], survivors: ['v7', 'v8'], cut: ['v5', 'v6'], board_fraction: 0.25 }] },
      { round_index: 1, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v7', 'v8'], survivors: [], cut: [], board_fraction: 0.5, pending: true, queued: false, total: 4, live_progress: {v7: {done: 0, total: 4}, v8: {done: 0, total: 4}} }] },
      { round_index: 2, label: 'Champion gate', matches: [{ match_id: 'racing-final', competitors: ['v0'], board_fraction: 1.0, winner: null, pending: true }] },
    ],
  });
  const model = STRUCT.buildLiveModel(
    at,
    { phase: 'tournament:round_1:rung1_m0', generation_id: 'v7' },
    [{ generation_id: 'v7', entry_id: 'b0', run_id: 'r0', progress: 0.3 }],
    ['v0', 'v5', 'v6', 'v7', 'v8'],
  );
  const r0 = model.rounds.find((r) => String(r.matches[0].match_id) === 'rung0').matches[0];
  // the COMPLETED rung-0 is carried verbatim — survivors/cuts persist.
  assertDeep([...r0.cut].sort(), ['v5', 'v6'], 'the completed rung-0 cuts (v5,v6) persist when rung-1 starts');
  assertDeep([...r0.survivors].sort(), ['v7', 'v8'], 'the completed rung-0 survivors (v7,v8) persist');
  const r1 = model.rounds.find((r) => String(r.matches[0].match_id) === 'rung1').matches[0];
  assertDeep([...r1.competitors].sort(), ['v7', 'v8'], 'rung-1 races ONLY the rung-0 survivors (the field narrowed by η)');
  assertEqual(r1.queued, false, 'rung-1 is now the active rung');

  // the rendered ladder keeps BOTH rungs — the cut marks survive the new tick.
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(/✕/.test(ladder.textContent), 'rung-0 cut marks (✕) are STILL present after rung-1 starts (accumulation, no discard)');
  assert(/↑/.test(ladder.textContent), 'rung-0 survivor marks (↑) persist');
  for (const id of ['v5', 'v6', 'v7', 'v8']) assert(ladder.textContent.includes(id), 'every competitor remains legible across rungs — ' + id);
});

// (d) a no-op repeat render (same digest) does NOT rebuild the ladder.
function completedBoards(field, done) {
  const match = field.rounds.flatMap((round) => round.matches).find((match) => match.pending && !match.queued);
  match.done = done;
  for (const lane of Object.values(match.live_progress || {})) lane.done = done;
  return field;
}

test('live racing model: a no-op heartbeat (same progress) yields a STABLE digest — the ladder is not rebuilt', () => {
  const heartbeat = { phase: 'tournament:round_0:rung0_m1', generation_id: 'v5' };
  const activeRuns = [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.4 }];
  const epochGens = ['v0', 'v5', 'v6', 'v7', 'v8'];
  const a = STRUCT.buildLiveModel(liveRacingField(), heartbeat, activeRuns, epochGens);
  const b = STRUCT.buildLiveModel(liveRacingField(), heartbeat, activeRuns, epochGens);
  assertEqual(STRUCT.structureDigest(a), STRUCT.structureDigest(b), 'two identical live ticks produce the SAME digest (digest-gated — no DOM rebuild)');

  // a REAL change (a board landed → done count grows) MUST change the digest.
  const c = STRUCT.buildLiveModel(
    completedBoards(liveRacingField(), 1),
    heartbeat,
    [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 1.0 }],
    epochGens,
  );
  assert(STRUCT.structureDigest(a) !== STRUCT.structureDigest(c), 'a board landing (progress advanced) DOES change the digest');

  // node-identity check: a gated re-render with the same digest keeps the ladder node.
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  ui.gatedSwap(host, STRUCT.structureDigest(a), () => STRUCT.renderStructure(a, ctx, EPOCH_ID));
  const first = svgsByClass(host, 'dn-funnel')[0];
  ui.gatedSwap(host, STRUCT.structureDigest(b), () => STRUCT.renderStructure(b, ctx, EPOCH_ID));
  const second = svgsByClass(host, 'dn-funnel')[0];
  assert(first === second, 'the funnel SVG node identity is preserved across a no-op tick (digest-gated, zero rebuild)');
});

// (e) champion-gate pending vs decided renders correctly (pending ≠ rejected).
test('live racing model: the champion-gate is PENDING (deciding…) during the race — never "rejected"', () => {
  const model = STRUCT.buildLiveModel(
    liveRacingField(),
    { phase: 'tournament:round_0:rung0_m1', generation_id: 'v5' },
    [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.4 }],
    ['v0', 'v5', 'v6', 'v7', 'v8'],
  );
  const gate = model.rounds.find((r) => String(r.matches[0].match_id) === 'racing-final');
  assert(gate, 'a champion-gate round is present');
  assert(!gate.matches[0].decision, 'the live gate has NO committed decision (not promoted/rejected)');
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(/deciding/i.test(ladder.textContent), 'the live gate reads "deciding…"');
  assert(!/rejected/i.test(ladder.textContent), 'a live undecided gate is NEVER labeled "rejected"');
});

// (f) the fully-completed race still renders the full ladder (no regression).
test('live racing model: a fully-completed race (all rounds, no live) still reconstructs the full ladder (no regression)', () => {
  // this is the existing reconstruct path — assert it is unaffected.
  const st = STRUCT.normalizeStructure(recorded('racing_ladder/racing_field'), false);
  assert(st && st.structure === 'racing', 'the completed reconstruction still yields a racing ladder');
  const model = STRUCT.racingModel(st);
  assert(model.hasRungs, 'the completed ladder has rungs');
  assertEqual(model.gateState, 'crowned', 'the completed gate crowns the promoted survivor (not deciding/rejected)');
  const nodes = STRUCT.renderStructure(st, { navigate() {}, href: router.href }, RC_EPOCH);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'the completed funnel still renders');
  assert(/♛/.test(ladder.textContent), 'the completed gate still crowns the champion ♛');
  assert(!/queued/.test(ladder.textContent), 'a completed funnel shows NO queued rungs');
});

// (g) end-to-end through the match-ups page: a live race with PUBLISHED rounds
// fills progressively rather than sitting on "being seeded".
test('live racing (e2e): the match-ups page fills progressively from the published live rounds', async () => {
  freshState();
  const at = liveRacingField({ partial_champion_agg: { scalar: 10 }, partial_challenger_agg: { scalar: 7 } });
  const F = {
    '/api/epoch': { epoch_id: EPOCH_ID, closed: false, goal: 'g', tournament: { structure: 'racing', params: at.structure_params }, experiments: [], board: [] },
    '/api/lineage': { generations: at.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: EPOCH_ID, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: false })) },
    '/api/score-trajectory': { points: [] },
    '/api/tournaments': { epoch_id: EPOCH_ID, structure: 'racing', champion_lineage: ['v0'], matchups: [], tournaments: [] },
    '/api/active-tournament': at,
  };
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', generation_id: 'v5' });
  coreState.state.activeRuns = [
    { generation_id: 'v5', entry_id: 'b0', run_id: 'r0', progress: 0.5 },
    { generation_id: 'v6', entry_id: 'b1', run_id: 'r1', progress: 0.2 },
  ];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running' };

  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'a survival funnel rendered on the match-ups page (not the empty state)');
  assert(!/being seeded|No tournament has run|unavailable/i.test(host.textContent), 'NOT the "being seeded"/"nothing ran" empty state during a live race with empty rounds');
  assert(allByClass(host, 'dt-live-pill')[0], 'a LIVE badge marks the in-flight tournament');
  for (const id of ['v5', 'v6', 'v7', 'v8']) assert(ladder.textContent.includes(id), 'the full challenger field renders — ' + id);
  assert(/boards|running/.test(ladder.textContent), 'lanes show live board progress');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// (h) ISSUE #8: a DEGENERATE published rung-0 (only champion + first
// challenger) must NOT under-render to "2 field". The full challenger field is
// carried on `competitors` (role !== champion) AND `entries` (side ===
// challenger); the live funnel's entering rung is WIDENED to the whole field so
// EVERY challenger races (≥4 lanes), with the champion v0 kept as the gate
// benchmark — never a rung lane.

// ====================================================================
// LIVE PROJECTED STANDINGS — an in-flight candidate (boards streaming) shows a
// PROJECTED standing (dashed/~prefix/proj badge/scored sub-bar), distinct from
// settled, across every structure + viz level. The runner writes
// `at.projected` ({gen: {scalar, boards_done, boards_total}}); the frontend
// folds it onto standings + pending matches, re-ranks per-structure (elim/racing
// on the projected scalar; swiss NEVER on Copeland points), and quantizes it
// into every digest so a no-op heartbeat is a true no-op (the anti-flash rule).
// ====================================================================

function publishedElimination() {
  const at = elimPayload('lone_final_pending', {
    structure: 'single_elim', phase: 'running', epoch_id: EPOCH_ID,
    competitors: [{generation_id: 'v0', role: 'champion'}, {generation_id: 'v1', role: 'challenger'}],
    standings: [
      {generation_id: 'v1', rank: 1, scalar: 0, wins: 0, in_flight: true, projected_scalar: 1, boards_done: 3, boards_total: 5},
      {generation_id: 'v0', rank: 2, scalar: 0, wins: 0, in_flight: true, projected_scalar: 2, boards_done: 3, boards_total: 5},
    ],
    champion_lineage: ['v0'],
  });
  Object.assign(at.rounds[0].matches[0], {
    pending: true, queued: false, total: 5, done: 6,
    projected: {v0: {scalar: 2, boards_done: 3, boards_total: 5}, v1: {scalar: 1, boards_done: 3, boards_total: 5}},
  });
  return at;
}

test('published standings retain their order and measured progress', () => {
  const at = publishedElimination();
  const model = STRUCT.buildLiveModel(at, null, [], ['v0', 'v1']);
  assertDeep(model.standings, at.standings, 'the browser preserves the published standings');
  const nodes = STRUCT.renderStructure(model, {navigate() {}, href: router.href}, EPOCH_ID);
  const host = document.createElement('div');
  for (const node of nodes) host.appendChild(node);
  assert(allByClass(host, 'dt-proj-row').length >= 1, 'running standings are marked');
  assert(allByClass(host, 'dt-proj-bar').length >= 1, 'measured board progress renders');
  assert(/~/.test(host.textContent), 'running scalars have the projection prefix');
  const em = STRUCT.elimModel(model);
  const bracket = svg.elimRadial({rounds: em.rounds, gen_states: em.gen_states, live: true});
  assert(allByClass(bracket, 'dn-proj').length >= 1, 'the bracket marks running competitors');
});

test('published Swiss standings render measured scalars beside completed match points', () => {
  const at = {
    structure: 'swiss', phase: 'running', competitors: [{generation_id: 'v1'}, {generation_id: 'v2'}],
    rounds: [{stage_index: 0, matches: [{match_id: 'match', competitors: ['v1','v2'], pending: true, total: 5}]}],
    standings: [
      {generation_id: 'v1', rank: 1, scalar: 0.4, wins: 1, losses: 0, status: 'alive'},
      {generation_id: 'v2', rank: 2, scalar: 0, wins: 0, losses: 0, status: 'alive', in_flight: true, projected_scalar: 0.01, boards_done: 4, boards_total: 5},
    ],
  };
  const model = STRUCT.buildLiveModel(at, null, [], null);
  assertDeep(model.standings, at.standings, 'published point order is preserved');
  const sm = STRUCT.swissModel(model);
  const node = svg.swissLadder({rounds: sm.rounds, standings: sm.standings, live: true});
  assert(allByClass(node, 'dn-proj').length >= 1, 'the running scalar is marked');
  assert(/~proj/.test(node.textContent), 'the projection label renders');
});

test('published racing progress renders scores and board counts for every lane', () => {
  const at = liveRacingField({partial_champion_agg: {scalar: 8}});
  Object.assign(at.rounds[0].matches[0].live_progress.v5, {
    projected: true, projected_scalar: 6, boards_done: 1, done: 1, partialDelta: -2,
  });
  const model = STRUCT.buildLiveModel(at, null, [], null);
  const host = document.createElement('div');
  for (const node of STRUCT.renderStructure(model, {navigate() {}, href: router.href}, EPOCH_ID)) host.appendChild(node);
  const funnel = svgsByClass(host, 'dn-funnel')[0];
  assert(/~/.test(funnel.textContent) && /proj/.test(funnel.textContent), 'the measured score is marked');
  assert(allByClass(funnel, 'dn-proj-bar').length >= 1, 'the measured board count renders');
  for (const id of ['v5','v6','v7','v8']) assert(funnel.textContent.includes(id), 'the complete field remains visible');
});

test('published progress changes redraw; repeated publications preserve the DOM', () => {
  const at = publishedElimination();
  const host = document.createElement('div');
  const paint = () => {
    const model = STRUCT.buildLiveModel(at, null, [], null);
    ui.gatedSwap(host, STRUCT.structureDigest(model), () => STRUCT.renderStructure(model, {navigate() {}, href: router.href}, EPOCH_ID));
  };
  paint(); const first = host.firstChild;
  paint(); assert(host.firstChild === first, 'identical progress preserves the DOM');
  at.standings[0].boards_done = 4;
  at.standings[0].projected_scalar = 0.9;
  paint(); assert(host.firstChild !== first, 'a published result redraws');
  const updated = host.firstChild;
  paint(); assert(host.firstChild === updated, 'the repeated result preserves the DOM');
});

// ====================================================================
// SWISS + ELIM — completed structure visuals (swissLadder / elimBracket)
// and progressive live models from buildLiveModel,
// parallel to the racing ladder/funnel. The completed views build a model
// from /api/tournament-structure rounds/standings; the live models
// accumulate completed rounds, fill the active round board-by-board, and
// queue the future — exactly the racing-ladder discipline.
// ====================================================================

// ---- completed SWISS → the standings ladder -------------------------

test('swiss (completed): swissModel + renderSwiss produce the standings ladder (rounds + Copeland standings + champion-gate)', () => {
  const st = STRUCT.normalizeStructure(SWISS_STRUCT, false);
  const model = STRUCT.swissModel(st);
  assert(model && model.hasRounds, 'a swiss model was derived');
  assertEqual(model.rounds.length, 2, 'both swiss rounds are in the model');
  assert(model.standings.length >= 1, 'the accumulating Copeland-point standings are present');
  // v1 won both pairings → leads the standings.
  assertEqual(String(model.standings[0].id), 'v1', 'the swiss leader (v1, 2 wins) tops the standings');
  assert(svg.isNum(model.standings[0].points), 'the leader carries Copeland points');

  const nodes = STRUCT.renderStructure(st, { navigate() {}, href: router.href }, EPOCH_ID);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-swissladder')[0];
  assert(ladder, 'the swiss standings ladder SVG rendered');
  assertEqual(ladder.getAttribute('width'), '100%', 'the swiss ladder is fit-to-width');
  assert(ladder.textContent.toLowerCase().includes('round 1') && ladder.textContent.toLowerCase().includes('round 2'), 'a column per swiss round');
  assert(allByClass(ladder, 'dn-swissladder-stand').length >= 1, 'the standings column rendered');
  assert(ladder.textContent.toLowerCase().includes('champion-gate'), 'the leader flows into a champion-gate node');
});

test('swiss (completed): a swiss winner that does NOT beat the incumbent is NOT promoted (gate "stands")', () => {
  // the leader is the incumbent v0 itself / lineage unchanged → no promotion.
  const sw = JSON.parse(JSON.stringify(SWISS_STRUCT));
  sw.champion_lineage = ['v0'];
  sw.standings = [
    { generation_id: 'v0', rank: 1, points: 2, wins: 2, losses: 0, status: 'champion' },
    { generation_id: 'v1', rank: 2, points: 1, wins: 1, losses: 1, status: 'alive' },
  ];
  const st = STRUCT.normalizeStructure(sw, false);
  const model = STRUCT.swissModel(st);
  assertEqual(model.gateState, 'stands', 'the incumbent leading the swiss → champion stands (no new crown)');
  assertEqual(model.championId, null, 'no challenger is crowned when the incumbent wins the swiss');
  const node = svg.swissLadder({ rounds: model.rounds, standings: model.standings, championId: model.championId, benchmarkId: model.benchmarkId, gateState: model.gateState });
  assert(/champion stands/.test(node.textContent), 'the gate reads "champion stands"');
});

// ---- completed ELIM → the radial bracket (elimBracket retired) -----

test('elim (completed): single-elim → elimModel + the radial bracket with a champion seat', () => {
  const st = STRUCT.normalizeStructure(elimPayload('single_elim_semifinals', { ...SE_STRUCT }), false);
  const model = STRUCT.elimModel(st);
  assert(model && model.hasMatches, 'a single-elim model was derived');
  assertEqual(model.losers, null, 'single-elim has NO losers band');
  assertEqual(model.winners.length, 2, 'two winners-bracket rounds (semifinal + final)');
  assertEqual(typeof svg.elimBracket, 'undefined', 'the elimBracket renderer is deleted (retired)');
  assertEqual(typeof svg.elimFlow, 'undefined', 'the lane-flow renderer is deleted');
  const node = svg.elimRadial({ rounds: model.rounds, gen_states: model.gen_states, championId: model.championId, benchmarkId: model.benchmarkId, gateState: model.gateState, onCompetitor() {} });
  assertEqual(node.localName, 'svg', 'the radial is an SVG');
  assertEqual(node.getAttribute('width'), '100%', 'fit-to-width');
  assertEqual(allByClass(node, 'dn-elimradial-ring').length, 3, 'both rounds render as rings, plus the gate ring');
  // winner continues inward (good), loser terminates (✕), champion → crowned seat.
  assert(/✕/.test(node.textContent), 'an eliminated spoke terminates with ✕');
  assertEqual(allByClass(node, 'dn-elimradial-seatlab')[0].textContent, svg.CROWN.current, 'the champion seat reads the crown ♛');
  assertEqual(allByClass(node, 'dn-elimradial-gateline').length, 1, 'the champion spoke dashes into the seat');
  assertEqual(allByClass(node, 'dn-elimradial-spoke').length, 4, 'one spoke per competitor');
});

test('elim (completed): double-elim → ONE radial SVG carrying the losers’ bracket on the lower arc', () => {
  const DE = JSON.parse(JSON.stringify(SE_STRUCT));
  DE.structure = 'double_elim';
  const st = STRUCT.normalizeStructure(elimPayload('double_elim_losers_round', DE), false);
  const model = STRUCT.elimModel(st);
  assert(Array.isArray(model.losers) && model.losers.length >= 1, 'double-elim carries a losers band in the model');
  const node = svg.elimRadial({ rounds: model.rounds, gen_states: model.gen_states, championId: model.championId, benchmarkId: model.benchmarkId, gateState: model.gateState, double: true, onCompetitor() {} });
  assertEqual(allByClass(node, 'dn-elimradial-equator').length, 1, 'the dashed equator splits the winners’ arc from the losers’ arc');
  assertEqual(allByClass(node, 'dn-elimradial-ring').length, 4, 'the losers’ bracket round adds a ring (three rounds + the gate ring)');
  const cy = Number(allByClass(node, 'dn-elimradial-ring')[0].getAttribute('cy'));
  const outer = allByClass(node, 'dn-elimradial-node').map((n) => Number(n.getAttribute('cy')));
  assert(outer.some((y) => y > cy) && outer.some((y) => y < cy), 'spokes sit on both arcs: the dropped lane on the lower, the winners’ on the upper');
});

// ---- progressive LIVE swiss model -----------------------------------

// a live swiss field per the NEW contract: v0..v3, the backend PUBLISHES the
// active round 0 (paired but undecided) plus the next round 1 queued (its
// pairings published as the bracket fills). Future rounds appear as the backend
// publishes them — the dashboard renders what is published, no synthesis.
function liveSwissField(extra) {
  return Object.assign({
    structure: 'swiss', phase: 'running', epoch_id: HERO_EPOCH,
    structure_params: { rounds: 3, board_size: 4 },
    round_index: 0,
    competitors: [{ generation_id: 'v0' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }],
    rounds: [
      { round_index: 0, label: 'Round 1', matches: [
        { match_id: 'sw_r0_m0', competitors: ['v0', 'v1'], pending: true, queued: false, total: 4, done: 0 },
        { match_id: 'sw_r0_m1', competitors: ['v2', 'v3'], pending: true, queued: false, total: 4, done: 0 },
      ] },
      { round_index: 1, label: 'Round 2', queued: true, matches: [
        { match_id: 'sw_r1_m0', competitors: ['v0', 'v2'], pending: true, queued: true, total: 4, done: 0 },
        { match_id: 'sw_r1_m1', competitors: ['v1', 'v3'], pending: true, queued: true, total: 4, done: 0 },
      ] },
    ],
    standings: [],
    champion_lineage: ['v0'],
  }, extra || {});
}

test('live swiss model: the active published round fills in board-by-board (overlaid progress), later published rounds queue', () => {
  const model = STRUCT.buildLiveModel(
    liveSwissField(),
    { phase: 'tournament:round_0', generation_id: 'v1' },
    [
      { generation_id: 'v0', entry_id: 'b0', run_id: 'r0', progress: 0.5 },
      { generation_id: 'v1', entry_id: 'b1', run_id: 'r1', progress: 0.0 },
    ],
    ['v0', 'v1', 'v2', 'v3'],
  );
  assert(model && model.live, 'a live swiss model built from the published rounds');
  const m = STRUCT.swissModel(model);
  assert(m.rounds.length >= 2, 'the published swiss rounds present (active + queued)');
  const r0 = m.rounds[0];
  assertEqual(r0.queued, false, 'round 0 is the ACTIVE round (not queued)');
  const p0 = r0.pairings[0];
  assert(p0.pending, 'an undecided active pairing is pending (not struck as decided)');
  assert(p0.inflight >= 1 || p0.done >= 1, 'the active pairing carries in-flight board progress overlaid from active-runs');
  assert(m.rounds[1].queued, 'the next published swiss round is queued (board progress not overlaid yet)');

  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, HERO_EPOCH);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const ladder = svgsByClass(host, 'dn-swissladder')[0];
  assert(ladder, 'the live swiss ladder rendered (not the being-seeded empty state)');
  assert(!/being seeded/i.test(host.textContent), 'NOT the being-seeded placeholder once pairings exist');
});

test('live swiss model: a completed round PERSISTS when the next round starts (accumulation)', () => {
  const at = liveSwissField({
    round_index: 1,
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
  });
  const model = STRUCT.buildLiveModel(
    at,
    { phase: 'tournament:round_1', generation_id: 'v1' },
    [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.3 }],
    ['v0', 'v1', 'v2', 'v3'],
  );
  const m = STRUCT.swissModel(model);
  // round 0 is carried verbatim — its winners persist.
  const r0 = m.rounds[0];
  assert(r0.pairings.every((p) => p.winner && !p.pending), 'the completed round-0 pairings persist with their winners (no blanking)');
  assertEqual(m.rounds[1].queued, false, 'round 1 is now the active round');
  // standings accumulate v1 + v3 as the round-0 winners.
  const v1 = m.standings.find((s) => s.id === 'v1');
  assert(v1 && v1.points >= 1, 'round-0 winner v1 has accumulated a Copeland point');
});

test('live swiss model: a no-op repeat render leaves the swiss-ladder node identity unchanged (digest-gated)', () => {
  const heartbeat = { phase: 'tournament:round_0', generation_id: 'v1' };
  const activeRuns = [{ generation_id: 'v0', entry_id: 'b0', run_id: 'r0', progress: 0.5 }];
  const epochGens = ['v0', 'v1', 'v2', 'v3'];
  const a = STRUCT.buildLiveModel(liveSwissField(), heartbeat, activeRuns, epochGens);
  const b = STRUCT.buildLiveModel(liveSwissField(), heartbeat, activeRuns, epochGens);
  assertEqual(STRUCT.structureDigest(a), STRUCT.structureDigest(b), 'two identical live swiss ticks share a digest');
  const c = STRUCT.buildLiveModel(completedBoards(liveSwissField(), 1), heartbeat, [{ generation_id: 'v0', entry_id: 'b0', run_id: 'r0', progress: 1.0 }], epochGens);
  assert(STRUCT.structureDigest(a) !== STRUCT.structureDigest(c), 'a board landing changes the swiss digest');

  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  ui.gatedSwap(host, STRUCT.structureDigest(a), () => STRUCT.renderStructure(a, ctx, HERO_EPOCH));
  const first = svgsByClass(host, 'dn-swissladder')[0];
  ui.gatedSwap(host, STRUCT.structureDigest(b), () => STRUCT.renderStructure(b, ctx, HERO_EPOCH));
  const second = svgsByClass(host, 'dn-swissladder')[0];
  assert(first === second, 'the swiss-ladder node identity is preserved across a no-op tick');
});

// ---- progressive LIVE elim model ------------------------------------


test('live elim model: an undecided round fills in board-by-board (active round, not empty)', () => {
  const model = STRUCT.buildLiveModel(
    liveElimField(),
    { phase: 'tournament:round_0', generation_id: 'v1' },
    [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.5 }],
    ['v0', 'v1', 'v2', 'v3'],
  );
  assert(model && model.live, 'a live elim model built from the field');
  const m = STRUCT.elimModel(model);
  assert(m.hasMatches, 'the active round has matches');
  const active = m.winners[0].matches.find((mm) => (mm.competitors || []).includes('v1'));
  assert(active && active.pending, 'an undecided active match is pending (not struck as decided)');
  const nodes = STRUCT.renderStructure(model, { navigate() {}, href: router.href }, HERO_EPOCH);
  const host = document.createElement('div');
  for (const n of nodes) host.appendChild(n);
  const bracket = svgsByClass(host, 'dn-elimradial')[0];
  assert(bracket, 'the live radial bracket rendered (not the being-seeded empty state)');
  assert(!/being seeded/i.test(host.textContent), 'NOT the being-seeded placeholder once matches exist');
});

test('live elim model: a completed round PERSISTS when the next round starts (accumulation)', () => {
  const at = liveElimField({
    round_index: 1,
    rounds: [
      { round_index: 0, label: 'Semifinal', matches: [
        { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], winner: 'v0', decision: 'win', bracket_slot: 'WB-R0-0' },
        { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], winner: 'v1', decision: 'win', bracket_slot: 'WB-R0-1' },
      ] },
      { round_index: 1, label: 'Final', matches: [
        { match_id: 'WB-R1-0', competitors: ['v0', 'v1'], bracket_slot: 'WB-R1-0', pending: true, queued: false, total: 4, done: 0 },
      ] },
    ],
  });
  const model = STRUCT.buildLiveModel(
    at,
    { phase: 'tournament:round_1', generation_id: 'v1' },
    [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.3 }],
    ['v0', 'v1', 'v2', 'v3'],
  );
  const m = STRUCT.elimModel(model);
  const r0 = m.winners[0];
  assert(r0.matches.every((mm) => mm.winner && !mm.pending), 'the completed semifinal matches persist with their winners (no blanking)');
  const fin = m.winners[1].matches[0];
  assert(fin.pending, 'the active final is pending (filling in)');
});

test('live elim model: a no-op repeat render leaves the bracket node identity unchanged (digest-gated)', () => {
  const heartbeat = { phase: 'tournament:round_0', generation_id: 'v1' };
  const activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 0.5 }];
  const epochGens = ['v0', 'v1', 'v2', 'v3'];
  const a = STRUCT.buildLiveModel(liveElimField(), heartbeat, activeRuns, epochGens);
  const b = STRUCT.buildLiveModel(liveElimField(), heartbeat, activeRuns, epochGens);
  assertEqual(STRUCT.structureDigest(a), STRUCT.structureDigest(b), 'two identical live elim ticks share a digest');
  const c = STRUCT.buildLiveModel(completedBoards(liveElimField(), 1), heartbeat, [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r0', progress: 1.0 }], epochGens);
  assert(STRUCT.structureDigest(a) !== STRUCT.structureDigest(c), 'a board landing changes the elim digest');

  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  ui.gatedSwap(host, STRUCT.structureDigest(a), () => STRUCT.renderStructure(a, ctx, HERO_EPOCH));
  const first = svgsByClass(host, 'dn-elimradial')[0];
  ui.gatedSwap(host, STRUCT.structureDigest(b), () => STRUCT.renderStructure(b, ctx, HERO_EPOCH));
  const second = svgsByClass(host, 'dn-elimradial')[0];
  assert(first === second, 'the bracket node identity is preserved across a no-op tick');
});

// ---- (c) the richer racing survival-funnel render -------------------
// (the static funnel render is exercised by the "survival funnel: the SVG
// narrows N→…→1 …" test below; here we cover the LIVE pending-rung case.)

test('survival funnel: a LIVE race leaves the pending rung neutral (nobody cut) and the gate reads "deciding…"', () => {
  const rungs = [
    { label: 'Rung 1', competitors: ['v1', 'v2', 'v3'], survivors: ['v1'], cut: ['v2', 'v3'], board_fraction: 0.5 },
    { label: 'Rung 2', competitors: ['v1'], survivors: [], cut: [], board_fraction: 1.0, pending: true },
  ];
  const node = svg.survivalFunnel({ rungs, championId: null, benchmarkId: 'v0', live: true, onCompetitor() {} });
  // only the DECIDED rung shows cuts; the pending rung does not strike anyone.
  const cutNames = node.querySelectorAll('[class]')
    .filter((n) => (n.getAttribute('class') || '').includes('dn-funnel-name') && (n.getAttribute('class') || '').includes('dn-bad'));
  assertEqual(cutNames.length, 2, 'only the two decided cuts (v2, v3) peel off — the pending rung strikes nobody');
  // the pending stage renders as a neutral (non-cut) band.
  assert(allByClass(node, 'dn-funnel-pending').length >= 1, 'the pending (still-racing) stage renders neutral (no premature cut)');
  assert(node.textContent.includes('deciding'), 'the champion-gate reads "deciding…" while the race is live (no premature crown)');
});

// ====================================================================
// RACING LADDER reconstruction from the PER-CHALLENGER match records.
//
// A racing tournament is persisted as ONE record PER CHALLENGER on
// /api/tournaments (NOT one assembled-rung record). Each entry is the
// flattened view from that challenger's seat:
//   { tournament_id:"<epoch>:<champ>-><chall>", structure:"racing",
//     competitors:[champ, chall], standings:[],
//     rounds:[ {match_id:"rungN_mK"|"racing-final", opponent, won, delta_scalar} ] }
// The ladder must AGGREGATE every record and GROUP matches by the `match_id`
// rung prefix to rebuild rung0 → … → champion-gate. This mirrors the LIVE
// epoch 2026-06-01_e0 (champion v0; field v1–v4; v3 promoted).
// ====================================================================


// ---- (a) reconstruct the rungs/field/cuts/survivors -----------------

test('racing field (SERVED): rung0 {v1,v2,v3,v4} (v1/v2 cut ✕, v3/v4 survive ↑) and rung1 {v3,v4} (v4 cut, v3 survives) read verbatim', () => {
  const st = STRUCT.normalizeStructure(recorded('racing_ladder/racing_field'), false);
  assert(st, 'a racing structure was reconstructed from the per-challenger records');
  assertEqual(st.structure, 'racing', 'the reconstructed structure is racing');
  // only the RUNG rounds (racing-final is the gate rather than a rung).
  const rungRounds = st.rounds.filter((r) => String(r.matches[0].match_id) !== 'racing-final');
  assertEqual(rungRounds.length, 2, 'two rungs reconstructed (rung0, rung1)');

  const r0 = rungRounds[0].matches[0];
  assertDeep([...r0.competitors].sort(), ['v1', 'v2', 'v3', 'v4'], 'rung0 field is the full challenger set {v1,v2,v3,v4}');
  assertDeep([...r0.cut].sort(), ['v1', 'v2'], 'rung0 cuts v1 and v2 (no match at rung1 or the final)');
  assertDeep([...r0.survivors].sort(), ['v3', 'v4'], 'rung0 survivors are v3 and v4 (they appear at rung1)');

  const r1 = rungRounds[1].matches[0];
  assertDeep([...r1.competitors].sort(), ['v3', 'v4'], 'rung1 field narrows to {v3,v4}');
  assertDeep([...r1.cut].sort(), ['v4'], 'rung1 cuts v4');
  assertDeep([...r1.survivors].sort(), ['v3'], 'rung1 survivor is v3 (it reaches the champion gate)');
  // each rung carries the board fraction (budget escalation: 25% → 50%).
  assertEqual(r0.board_fraction, 0.25, 'rung0 covers 25% of the board');
  assertEqual(r1.board_fraction, 0.5, 'rung1 escalates to 50% of the board (×η)');
  // and each runner's Δ-vs-champion at the rung is carried for the mark.
  assertEqual(r0.deltas.v1, 25.0, 'rung0 carries v1’s Δ-vs-champion');
  assertEqual(r0.deltas.v3, -0.16, 'rung0 carries v3’s (winning) Δ-vs-champion');
});

// ---- (b) the champion-gate crowns v3 (NOT "tbd") --------------------

test('racing field (SERVED): the champion-gate names v3 as the promoted champion ♛, NOT "tbd"', () => {
  const st = STRUCT.normalizeStructure(recorded('racing_ladder/racing_field'), false);
  const gate = st.rounds.find((r) => String(r.matches[0].match_id) === 'racing-final');
  assert(gate, 'a racing-final champion-gate round was reconstructed');
  const gm = gate.matches[0];
  assertEqual(gm.winner, 'v3', 'the gate winner is the promoted survivor v3');
  assertEqual(gm.decision, 'promoted', 'the gate decision is promoted (racing-final won, Δ negative)');
  assertDeep([...gm.competitors].sort(), ['v0', 'v3'], 'the gate pits the champion v0 against the survivor v3');
  assertEqual(st.champion_lineage[st.champion_lineage.length - 1], 'v3', 'champion_lineage confirms v3 is the new champion');

  // and the rendered ladder shows v3 crowned ♚ — never "tbd".
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  const nodes = STRUCT.renderStructure(st, ctx, RC_EPOCH);
  const wrap = document.createElement('div');
  for (const n of nodes) wrap.appendChild(n);
  const ladder = svgsByClass(wrap, 'dn-funnel')[0];
  assert(ladder, 'the survival funnel rendered from the reconstruction');
  assert(ladder.textContent.includes('champion-gate'), 'a champion-gate stage rendered');
  assert(ladder.textContent.includes('♛ v3'), 'the gate crowns v3 as the new champion ♛');
  assert(!ladder.textContent.includes('tbd'), 'the gate is NOT the empty "tbd" skeleton');
  assert(wrap.textContent.includes('v3 promoted'), 'the caption states the champion-gate outcome (v3 promoted)');
});

// ---- (c) competitors are clickable to their candidate ---------------

test('racing field (SERVED): each competitor in the funnel is clickable → its candidate page', () => {
  const st = STRUCT.normalizeStructure(recorded('racing_ladder/racing_field'), false);
  let navTo = null;
  const ctx = { navigate: (v, p) => { navTo = { v, p }; }, href: router.href };
  const nodes = STRUCT.renderStructure(st, ctx, RC_EPOCH);
  const wrap = document.createElement('div');
  for (const n of nodes) wrap.appendChild(n);
  const runner = allByClass(wrap, 'dn-funnel-runner')[0];
  assert(runner, 'a clickable competitor row exists');
  runner.dispatchEvent({ type: 'click' });
  assert(navTo && navTo.v === 'candidate' && navTo.p.epochId === RC_EPOCH, 'clicking a competitor routes to its candidate page');
  assert(/^v\d+$/.test(navTo.p.gen), 'the navigation carries the competitor generation id');
});

// ---- (d) the LIVE path still renders an in-progress ladder ----------

test('racing reconstruct: the LIVE /api/active-tournament path still renders the in-progress ladder with a pending rung + a "deciding…" gate', async () => {
  freshState();
  const F = {
    '/api/epoch': { epoch_id: RC_EPOCH, closed: false, goal: 'g', tournament: { structure: 'racing', params: LIVE_RACING.structure_params }, experiments: [], board: [] },
    '/api/lineage': { generations: LIVE_RACING.competitors.map((c) => ({ generation_id: c.generation_id, epoch_id: RC_EPOCH, parent_generation_id: c.role === 'champion' ? '' : 'v0', promoted: false })) },
    '/api/score-trajectory': { points: [] },
    // the COMPLETED record is empty — only the LIVE active-tournament has the topology.
    '/api/tournaments': { epoch_id: RC_EPOCH, structure: 'racing', champion_lineage: [], matchups: [], tournaments: [] },
    '/api/active-tournament': LIVE_RACING,
  };
  installFixtureMap(F);
  coreState.state.setHeartbeat({ phase: 'tournament:round_1:rung1_m0', generation_id: 'v1' });
  coreState.state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1', progress: 0.5 }];
  coreState.state.activeTournament = { structure: 'racing', phase: 'running' };

  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH });

  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'the LIVE survival funnel rendered from /api/active-tournament');
  assert(allByClass(host, 'dt-live-pill')[0], 'a LIVE badge marks the in-flight tournament');
  assert(host.textContent.includes('Rung 1') && host.textContent.includes('Rung 2'), 'both rungs render (incl. the still-racing one)');
  // the not-yet-decided rung stays neutral (nobody struck) and the gate reads "deciding…".
  const struck = allByClass(host, 'dn-out');
  for (const n of struck) assert((n.textContent || '').indexOf('v1') < 0, 'the leader v1 is never struck (cut) mid-run');
  assert(ladder.textContent.includes('deciding'), 'the live champion-gate reads "deciding…" — no premature crown');
  assert(!ladder.textContent.includes('♚'), 'no champion is crowned ♚ while the race is live');

  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;
});

// ---- (e) the match-ups page reconstructs the ladder end-to-end ------

test('racing reconstruct: the match-ups page rebuilds the full ladder from the per-challenger /api/tournaments records (not an empty skeleton)', async () => {
  freshState();
  const F = racingLadderFixture();
  installFixtureMap(F);
  // idle — no live run; the ladder must reconstruct from the completed records.
  coreState.state.heartbeat = { phase: 'idle' };
  coreState.state.activeRuns = [];
  coreState.state.activeTournament = null;

  const gens = await import('../js/views/gens.js');
  const host = document.createElement('div');
  await gens.render(host, { navigate() {}, href: router.href }, { epochId: RC_EPOCH });

  const ladder = svgsByClass(host, 'dn-funnel')[0];
  assert(ladder, 'the survival funnel rendered on the match-ups page from the per-challenger records');
  assert(!/No tournament|No rungs|unavailable/i.test(host.textContent), 'NOT the empty "RUNG · RUNG · CHAMPION-GATE: tbd" skeleton');
  assert(host.textContent.includes('Rung 0') && host.textContent.includes('Rung 1'), 'both reconstructed rungs render as stages');
  // the full rung0 field + the cut/survivor marks made it through to the SVG.
  for (const id of ['v1', 'v2', 'v3', 'v4']) assert(ladder.textContent.includes(id), 'rung0 names the full field — ' + id);
  assert(ladder.textContent.includes('✕'), 'cut runners are struck ✕');
  assert(ladder.textContent.includes('↑'), 'survivors are marked ↑');
  assert(ladder.textContent.includes('♛ v3'), 'the champion-gate crowns v3 as the new champion ♛ (not tbd)');
  assert(allByClass(host, 'dt-live-pill').length === 0, 'idle reconstruction carries NO live badge');
});

// ---- (d) current-vs-former champion badge in the tree ---------------

test('tree champion badge: with champion_lineage ["v0","v3"], ONLY v3 is the current "champion"; v0 is a "former champion"', () => {
  const host = document.createElement('div');
  // the model the shell assembles: v0 + v3 both promoted, but the lineage's
  // LAST id (v3) is the current crown.
  const model = {
    epochs: [{ id: EPOCH_ID, current: true }],
    byEpoch: { [EPOCH_ID]: {
      gens: [
        { id: 'v0', promoted: true, parent: null, currentChampion: false, formerChampion: true },
        { id: 'v1', promoted: false, parent: 'v0', currentChampion: false, formerChampion: false },
        { id: 'v3', promoted: true, parent: 'v0', currentChampion: true, formerChampion: false },
      ],
      boards: [],
    } },
  };
  const toggles = new Set(['e:' + EPOCH_ID, 'e:' + EPOCH_ID + '/gens']);
  tree.buildTree(host, model, router.parseRoute(`#/e/${EPOCH_ID}`), toggles, { navigate() {}, href: router.href }, () => {});

  // exactly ONE current champion (v3) carries the gen-champ badge…
  const champs = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-leaf') && n.getAttribute('data-kind') === 'gen-champ');
  assertEqual(champs.length, 1, 'exactly one CURRENT champion badge (v3)');
  assert((champs[0].textContent || '').includes('v3'), 'the current champion is v3');
  assert((champs[0].textContent || '').includes('champion') && !(champs[0].textContent || '').includes('former'), 'v3 reads "champion" (not "former")');
  // …and the FORMER champion (v0) carries the distinct former-champion marker.
  const formers = host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-leaf') && n.getAttribute('data-kind') === 'gen-former');
  assertEqual(formers.length, 1, 'exactly one FORMER champion marker (v0)');
  assert((formers[0].textContent || '').includes('v0') && (formers[0].textContent || '').includes('former'), 'v0 reads "former champion"');
  // a rejected branch is unchanged.
  assert(host.textContent.includes('rejected'), 'the rejected branch (v1) is unchanged');
});

test('tree champion badge: the shell tree model marks the current champion from champion_lineage (and the digest re-stamps when the crown moves)', () => {
  // two promoted gens, lineage crowns the LAST (v3); the digest must differ
  // from a model where v0 is the current crown (so the badge re-stamps).
  const cur3 = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: { gens: [
    { id: 'v0', promoted: true, parent: null, currentChampion: false, formerChampion: true },
    { id: 'v3', promoted: true, parent: 'v0', currentChampion: true, formerChampion: false },
  ], boards: [] } } };
  const cur0 = { epochs: [{ id: EPOCH_ID, current: true }], byEpoch: { [EPOCH_ID]: { gens: [
    { id: 'v0', promoted: true, parent: null, currentChampion: true, formerChampion: false },
    { id: 'v3', promoted: true, parent: 'v0', currentChampion: false, formerChampion: true },
  ], boards: [] } } };
  const route = router.parseRoute(`#/e/${EPOCH_ID}`);
  const toggles = new Set();
  assert(tree.treeDigest(cur3, route, toggles) !== tree.treeDigest(cur0, route, toggles),
    'the tree digest changes when the crown moves (v3 current vs v0 current) — the badge re-stamps');
});


await run();
