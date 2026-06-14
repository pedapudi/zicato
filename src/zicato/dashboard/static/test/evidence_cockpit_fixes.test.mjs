// test/evidence_cockpit_fixes.test.mjs — the six evidence-cockpit liveness /
// transcript fixes, each with its anti-flash (no-op-beat) guard.
//
// Covers, one section per fix:
//   1. CHROME CONSOLIDATION — the three competing "live" signals fold into ONE
//      `dt-run-state` pill that carries the PHASE (state word · structure ·
//      phase · count) and the "last seen Ns ago" affordance INSIDE it; a no-op
//      beat churns ZERO DOM.
//   2. HERO VISIBILITY — the hero is gated on the orchestrator being ALIVE (a
//      fresh heartbeat pulse / LIVE-or-STALLED), NOT the narrower `running`, so
//      it does NOT flicker out when `running` momentarily drops mid long-call.
//   4. IN-FLIGHT MATCHES — the "what's running" block shows the live matches
//      whenever runs are genuinely in flight, even when a fresh epoch roll
//      transiently leaves the heartbeat's epoch tag out of step.
//   5. PROGRESS — a TERMINAL run reads 100% (keyed to completion, not the
//      wall-clock budget) — the "1/1 tasks completed but 0%" bug.
//   6. TRANSCRIPT DEDUP — consecutive identical goal turns (goldfive emits the
//      goal on BOTH runStarted + goalDerived) collapse to one.
//
// (Fix 3 — the epoch-objective truncation — is a pure CSS change, not reachable
// from the DOM harness; it is verified by inspection of console.css.)

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const livestatus = await import('../js/livestatus.js');
const shell = await import('../js/shell.js');
const live = await import('../js/live.js');
const STRUCT = await import('../js/views/structure.js');
const board = await import('../js/views/board.js');
const { state } = await import('../js/core/state.js');
const { bus } = await import('../js/core/bus.js');
const data = await import('../js/data.js');

const NOW = 1_700_000_000_000;

function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
function textOf(node) {
  let s = '';
  const walk = (n) => {
    for (const c of (n.childNodes || [])) {
      if (c.nodeType === 3) s += c.textContent; else walk(c);
    }
  };
  walk(node);
  return s;
}

function resetState() {
  state.lastSeq = -1;
  state.terminal = false;
  state.lastSeqAdvanceAt = NaN;
  state.heartbeat = null;
  state.activeTournament = null;
  state.activeRuns = [];
  state.connected = false;
  state.connecting = false;
}

function mountChrome() {
  const document = installDom();
  resetState();
  const listeners = { hashchange: [] };
  globalThis.HashChangeEvent = function HashChangeEvent() {};
  globalThis.EventSource = function EventSource() { this.readyState = 0; this.addEventListener = () => {}; this.close = () => {}; };
  globalThis.EventSource.CLOSED = 2;
  globalThis.fetch = async () => ({ ok: true, async json() { return {}; } });
  globalThis.window = globalThis.window || {};
  globalThis.window.localStorage = globalThis.window.localStorage || { getItem() { return null; }, setItem() {} };
  globalThis.window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
  const loc = { _hash: '#/' };
  Object.defineProperty(loc, 'hash', {
    get() { return this._hash; },
    set(v) { this._hash = v; for (const fn of (listeners.hashchange || [])) fn(); },
    configurable: true,
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => {};
  const root = document.createElement('div');
  document.body.appendChild(root);
  shell.mountShell(root);
  return root;
}

// ════════════════════════════════════════════════════════════════════
// FIX 1 — the ONE consolidated liveness pill (phase rides inside it)
// ════════════════════════════════════════════════════════════════════

test('chrome: the liveness pill is the SINGLE run-state badge — the redundant standalone run-badge is gone', async () => {
  const root = mountChrome();
  await new Promise((r) => setTimeout(r, 0));
  // exactly one run-state pill; the old separate `dt-run-badge` wrapper + the
  // old pulse element are gone (the three competing "live" signals are one).
  assertEqual(allByClass(root, 'dt-run-state').length, 1, 'exactly one liveness pill in the chrome');
  assertEqual(allByClass(root, 'dt-run-badge').length, 0, 'the redundant standalone run-badge is removed');
  assertEqual(allByClass(root, 'dt-run-pulse').length, 0, 'the redundant run-badge pulse is removed');
  // the phase label + count + stale affordance ride INSIDE the one pill.
  const pill = allByClass(root, 'dt-run-state')[0];
  assert(allByClass(pill, 'dt-run-label')[0], 'the phase label is inside the pill');
  assert(allByClass(pill, 'dt-run-count')[0], 'the in-flight count is inside the pill');
  assert(allByClass(pill, 'dt-status-stale')[0], 'the stale affordance is inside the pill');
});

test('chrome: a LIVE run reads STATE + PHASE in the one pill (e.g. "LIVE · racing · rung 0")', async () => {
  const root = mountChrome();
  await new Promise((r) => setTimeout(r, 0));
  state.connected = true;
  state.activeTournament = { structure: 'racing', phase: 'running', competitors: [{ generation_id: 'v0' }] };
  state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0' }, { generation_id: 'v2', entry_id: 'b1' }];
  state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', seq: 3, last_heartbeat: Date.now() });
  state._changed();
  await new Promise((r) => setTimeout(r, 0));
  const pill = allByClass(root, 'dt-run-state')[0];
  const txt = (textOf(pill) || '').toUpperCase();
  assert(txt.includes('LIVE'), 'the pill reads the LIVE state word');
  assert(txt.includes('RACING'), 'the pill carries the PHASE (racing) inside it');
  const count = allByClass(pill, 'dt-run-count')[0];
  assert((count.textContent || '').includes('2'), 'the in-flight count (2 units) reads inside the pill');
});

test('chrome: a frozen run shows "last seen Ns ago" INSIDE the one pill — no separate badge', async () => {
  const root = mountChrome();
  await new Promise((r) => setTimeout(r, 0));
  state.connected = true;
  // a frozen heartbeat (120s old) — not alive → stale affordance shows.
  state.setHeartbeat({ phase: 'tournament:round_0:final', last_heartbeat: Date.now() - 120_000 });
  state.activeTournament = { structure: 'racing', phase: 'running' };
  state.activeRuns = [];
  state._changed();
  await new Promise((r) => setTimeout(r, 0));
  const pill = allByClass(root, 'dt-run-state')[0];
  const stale = allByClass(pill, 'dt-status-stale')[0];
  assert(stale && /last seen/.test(stale.textContent), 'the stale affordance reads "last seen…" inside the pill');
  // the phase label is cleared once the run is not alive (no stale phase caption).
  const label = allByClass(pill, 'dt-run-label')[0];
  assertEqual((label.textContent || '').trim(), '', 'no live phase label once the run is not alive');
});

test('chrome: a NO-OP beat (same seq) churns ZERO DOM in the consolidated pill', async () => {
  const root = mountChrome();
  await new Promise((r) => setTimeout(r, 0));
  state.connected = true;
  state.activeTournament = { structure: 'racing', phase: 'running', competitors: [{ generation_id: 'v0' }] };
  state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0' }];
  const t0 = Date.now();
  state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', seq: 4, last_heartbeat: t0 });
  state._changed();
  await new Promise((r) => setTimeout(r, 0));
  const statusEl = allByClass(root, 'dt-status')[0];
  const writesBefore = statusEl.innerHTMLWriteCount();
  const pill = allByClass(root, 'dt-run-state')[0];
  const labelNodeBefore = allByClass(pill, 'dt-run-label')[0].firstChild;

  // a NO-OP beat: same seq, a newer-but-same-bucket heartbeat ts.
  state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', seq: 4, last_heartbeat: t0 + 500 });
  state._changed();
  await new Promise((r) => setTimeout(r, 0));
  assertEqual(state.lastSeq, 4, 'the cursor is unchanged by the no-op beat');
  assertEqual(statusEl.innerHTMLWriteCount(), writesBefore, 'NO innerHTML writes in the pill on a no-op beat');
  assert(allByClass(pill, 'dt-run-label')[0].firstChild === labelNodeBefore,
    'the phase-label text node identity is preserved on a no-op beat (no flash)');
});

// ════════════════════════════════════════════════════════════════════
// FIX 2 — hero visibility gated on the orchestrator pulse (alive), not seq
// ════════════════════════════════════════════════════════════════════

test('livestatus: `alive` is true for LIVE and STALLED, false for SETTLED and DEAD', () => {
  // LIVE — seq advancing, fresh heartbeat.
  const liveSt = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - 1000 },
    activeRuns: [{ generation_id: 'v1' }], activeTournament: { structure: 'racing', phase: 'running' },
    seq: 12, terminal: false, lastSeqAdvanceAt: NOW - 1000,
  }, NOW);
  assertEqual(liveSt.runState, livestatus.RUN_STATE.LIVE, 'LIVE');
  assertEqual(liveSt.alive, true, 'LIVE ⇒ alive');

  // STALLED — seq frozen past budget, but the heartbeat STILL pulses (a long
  // reasoning call): the orchestrator is alive, so the hero must NOT flicker out.
  const stalledSt = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - 1000 /* fresh pulse */ },
    activeRuns: [], activeTournament: { structure: 'swiss', phase: 'running' },
    seq: 5, terminal: false, lastSeqAdvanceAt: NOW - (livestatus.SEQ_STALL_BUDGET_MS + 5000),
  }, NOW);
  assertEqual(stalledSt.runState, livestatus.RUN_STATE.STALLED, 'STALLED');
  assertEqual(stalledSt.alive, true, 'STALLED (orchestrator still pulsing) ⇒ alive — hero stays put');

  // SETTLED — terminal.
  const settledSt = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - 1000 },
    activeRuns: [{ generation_id: 'v1' }], activeTournament: { structure: 'racing', phase: 'running' },
    seq: 20, terminal: true, lastSeqAdvanceAt: NOW - 500,
  }, NOW);
  assertEqual(settledSt.alive, false, 'SETTLED ⇒ not alive (hero hides)');

  // DEAD — seq frozen AND no fresh heartbeat.
  const deadSt = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - (livestatus.STALE_HEARTBEAT_MS + 10_000) },
    activeRuns: [], activeTournament: { structure: 'swiss', phase: 'running' },
    seq: 5, terminal: false, lastSeqAdvanceAt: NOW - (livestatus.SEQ_STALL_BUDGET_MS + 5000),
  }, NOW);
  assertEqual(deadSt.alive, false, 'DEAD ⇒ not alive');
});

test('hero: a STALLED run (seq frozen, heartbeat still fresh) KEEPS the hero visible (dt-live-on)', () => {
  const c = new live.LiveController({});
  const at = { structure: 'racing', phase: 'running', epoch_id: 'e1', competitors: [{ generation_id: 'v0' }] };
  const heartbeat = { phase: 'tournament:round_0:rung0_m1', epoch_id: 'e1', last_heartbeat: new Date().toISOString() };
  // a STALLED status: running falls to false (no in-flight, phase still active),
  // but alive stays true because the heartbeat pulses.
  c.update({ status: { running: false, alive: true, structure: 'racing', runState: 'stalled' }, heartbeat, activeRuns: [], activeTournament: at });
  assert((c.node.getAttribute('class') || '').split(/\s+/).includes('dt-live-on'),
    'the hero stays ON while the orchestrator is alive (STALLED), not flickering out on a frozen seq');
  // the pill carries the STALLED word (not a hard-coded "LIVE").
  assertEqual((c._pillText.textContent || '').toUpperCase(), 'STALLED', 'the hero pill reads the four-state word (STALLED)');
});

test('hero: a SETTLED run hides the hero (alive:false) + resets the digests', () => {
  const c = new live.LiveController({});
  const at = { structure: 'racing', phase: 'running', epoch_id: 'e1', competitors: [{ generation_id: 'v0' }] };
  c.update({ status: { running: false, alive: false, structure: 'racing', runState: 'settled' }, heartbeat: { phase: 'idle' }, activeRuns: [], activeTournament: at });
  assert(!(c.node.getAttribute('class') || '').split(/\s+/).includes('dt-live-on'),
    'a settled (not-alive) run hides the hero');
});

// ════════════════════════════════════════════════════════════════════
// FIX 4 — the "what's running" matches show while runs are genuinely live
// ════════════════════════════════════════════════════════════════════

// a racing tournament tagged to one epoch, with a heartbeat whose epoch tag is
// transiently DIFFERENT (a fresh roll) — the old epoch gate blanked the matches.
function racingInflight(epochTag, hbEpochTag) {
  return {
    at: {
      structure: 'racing', phase: 'running', epoch_id: epochTag,
      structure_params: { rungs: [{ fraction: 0.5 }, { fraction: 1.0 }], board_size: 8 },
      champion_lineage: ['v0'],
      competitors: [
        { generation_id: 'v0', role: 'champion' }, { generation_id: 'v5', role: 'challenger' },
      ],
      rounds: [
        { round_index: 0, label: 'Rung 1', matches: [
          { match_id: 'rung1_m0', competitors: ['v0', 'v5'], board_fraction: 0.5,
            live_progress: { v0: { boards_total: 8, inflight: 1 }, v5: { boards_done: 3, boards_total: 8, inflight: 1, done: 3, total: 8 } } },
        ] },
      ],
      standings: [], partial_champion_agg: { scalar: 10.0 },
    },
    heartbeat: { phase: 'tournament:rung1', epoch_id: hbEpochTag, last_heartbeat: new Date().toISOString() },
    activeRuns: [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r5', epoch_id: epochTag }],
  };
}

test('matches: the "what\'s running" block shows in-flight matches while runs are active, even with a transiently-mismatched heartbeat epoch', () => {
  const c = new live.LiveController({});
  // heartbeat epoch ('e2', a fresh roll) != tournament epoch ('e1') — the old
  // epoch gate would have blanked the block; the active run (in 'e1') corroborates.
  const { at, heartbeat, activeRuns } = racingInflight('e1', 'e2');
  c.update({ status: { running: true, alive: true, structure: 'racing' }, heartbeat, activeRuns, activeTournament: at });
  const body = c._matchesBody;
  const empty = allByClass(body, 'dt-live-matches-empty')[0];
  assert(!empty, 'the "no matches in flight right now…" placeholder is NOT shown while a run is live');
  assert(allByClass(body, 'dt-live-match')[0], 'an in-flight match block renders for the active run');
});

test('matches: a FOREIGN-epoch run (known-different) does NOT light up a stale tournament', () => {
  const c = new live.LiveController({});
  // tournament in 'e1'; the heartbeat AND the only run are in 'e2' (a genuinely
  // foreign run) — the tournament has no corroborating run, so no matches show.
  const at = racingInflight('e1', 'e2').at;
  const heartbeat = { phase: 'tournament:rung1', epoch_id: 'e2', last_heartbeat: new Date().toISOString() };
  const foreignRuns = [{ generation_id: 'v9', entry_id: 'bx', run_id: 'r9', epoch_id: 'e2' }];
  c.update({ status: { running: true, alive: true, structure: 'racing' }, heartbeat, activeRuns: foreignRuns, activeTournament: at });
  const empty = allByClass(c._matchesBody, 'dt-live-matches-empty')[0];
  assert(empty, 'a known-foreign-epoch run does not light up the stale tournament (placeholder shows)');
});

test('matches: a NO-OP beat over the same live matches churns ZERO DOM', () => {
  const c = new live.LiveController({});
  const { at, heartbeat, activeRuns } = racingInflight('e1', 'e2');
  c.update({ status: { running: true, alive: true, structure: 'racing' }, heartbeat, activeRuns, activeTournament: at });
  const firstNode = c._matchesBody.firstChild;
  const digestBefore = c._matchesDigest;
  assert(firstNode && digestBefore, 'the matches block mounted');
  // an identical second tick → same digest → no rebuild (node identity preserved).
  c.update({ status: { running: true, alive: true, structure: 'racing' }, heartbeat, activeRuns, activeTournament: at });
  assertEqual(c._matchesDigest, digestBefore, 'a no-op tick yields the SAME matches digest');
  assert(c._matchesBody.firstChild === firstNode, 'a no-op tick preserves the matches node (no flash)');
});

// ════════════════════════════════════════════════════════════════════
// FIX 5 — a terminal run reads 100% (completion, not wall-clock budget)
// ════════════════════════════════════════════════════════════════════

test('progress: a TERMINAL run reads 100% regardless of its elapsed/budget time-fraction', () => {
  // the "1/1 tasks completed but 0%" bug: finished early, tiny elapsed/budget.
  assertEqual(STRUCT.runProgressRatio({ tasks_completed: 1, tasks_total: 1, elapsed_seconds: 2, budget_seconds: 600 }), 1,
    'all tasks complete ⇒ 100% (not the 0%-ish time fraction)');
  assertEqual(STRUCT.runProgressRatio({ status: 'completed', elapsed_seconds: 5, budget_seconds: 600 }), 1, 'status:completed ⇒ 100%');
  assertEqual(STRUCT.runProgressRatio({ status: 'pass' }), 1, 'a terminal pass ⇒ 100%');
  assertEqual(STRUCT.runProgressRatio({ done: true }), 1, 'done:true ⇒ 100%');
  assertEqual(STRUCT.runProgressRatio({ boards_done: 8, boards_total: 8 }), 1, 'all boards scored ⇒ 100%');
  // runIsTerminal mirrors the verdict.
  assert(STRUCT.runIsTerminal({ tasks_completed: 1, tasks_total: 1 }), 'runIsTerminal true for all tasks done');
});

test('progress: an in-flight (non-terminal) run still reads its live time/board fraction', () => {
  assertEqual(STRUCT.runProgressRatio({ progress: 0.4 }), 0.4, 'an explicit fraction is honoured');
  assertEqual(STRUCT.runProgressRatio({ elapsed_seconds: 30, budget_seconds: 120 }), 0.25, 'elapsed/budget fallback');
  assertEqual(STRUCT.runProgressRatio({ tasks_completed: 1, tasks_total: 3, progress: 0.33 }), 0.33,
    'a partial task count is NOT terminal → the live fraction reads through');
  assertEqual(STRUCT.runProgressRatio({}), null, 'a bare run with no progress signal reads null (running…)');
  assert(!STRUCT.runIsTerminal({ tasks_completed: 1, tasks_total: 3 }), 'runIsTerminal false while tasks remain');
  assert(!STRUCT.runIsTerminal({ status: 'running' }), 'runIsTerminal false for a running status');
});

// ════════════════════════════════════════════════════════════════════
// FIX 6 — dedup consecutive identical goal turns in the transcript
// ════════════════════════════════════════════════════════════════════

test('transcript: consecutive identical goal turns collapse to one (the runStarted + goalDerived duplicate)', () => {
  const turns = [
    { seq: 0, role: 'user', text: 'Make the agent faster.' },           // runStarted.goalSummary
    { seq: 1, role: 'user', text: 'Make the agent faster.' },           // goalDerived (identical)
    { seq: 2, role: 'agent', text: 'Working on it.' },
  ];
  const out = board.dedupConsecutiveTurns(turns);
  assertEqual(out.length, 2, 'the duplicate goal line is folded (3 → 2 turns)');
  assertEqual(out[0].text, 'Make the agent faster.', 'the goal is kept once');
  assertEqual(out[1].text, 'Working on it.', 'the distinct agent turn survives');
});

test('transcript: dedup is CONSERVATIVE — different text, tool calls, or a gap are all kept', () => {
  // different text → both kept.
  assertEqual(board.dedupConsecutiveTurns([
    { role: 'user', text: 'A' }, { role: 'user', text: 'B' },
  ]).length, 2, 'different text is not a duplicate');
  // a turn carrying tool calls is never folded (distinct content).
  assertEqual(board.dedupConsecutiveTurns([
    { role: 'agent', text: 'X' }, { role: 'agent', text: 'X', tool_calls: [{ name: 't' }] },
  ]).length, 2, 'a tool-call turn is always kept');
  // a non-consecutive echo (a turn between) is kept.
  assertEqual(board.dedupConsecutiveTurns([
    { role: 'user', text: 'G' }, { role: 'agent', text: 'mid' }, { role: 'user', text: 'G' },
  ]).length, 3, 'only CONSECUTIVE duplicates fold; a later echo is kept');
  // empty-text turns are never collapsed into each other.
  assertEqual(board.dedupConsecutiveTurns([
    { role: 'agent', text: '' }, { role: 'agent', text: '' },
  ]).length, 2, 'empty turns are not treated as duplicates');
});

// ════════════════════════════════════════════════════════════════════
// TEARDOWN — reset the process-wide AppState / bus the shell mount touched.
// ════════════════════════════════════════════════════════════════════

test('teardown: reset the shared AppState / bus for the next file', async () => {
  await new Promise((r) => setTimeout(r, 480));
  bus._reset();
  if (typeof data.invalidate === 'function') data.invalidate();
  resetState();
  assert(true, 'shared singletons reset');
});

await run();
