// test/evidence_cockpit_fixes.test.mjs — the six evidence-cockpit liveness /
// transcript fixes, each with its anti-flash (no-op-beat) guard.
//
// Covers, one section per fix:
//   1. CHROME CONSOLIDATION — the three competing "live" signals fold into ONE
//      `dt-run-state` pill that carries the PHASE (state word · structure ·
//      phase · count) and the "last seen Ns ago" affordance INSIDE it; a no-op
//      beat churns ZERO DOM.
//   2. HERO VISIBILITY — the hero is gated on the orchestrator being ALIVE (a
//      fresh heartbeat pulse / LIVE-or-STALLED) rather than the narrower `running`, so
//      it does NOT flicker out when `running` momentarily drops mid long-call.
//   4. IN-FLIGHT MATCHES — the "what's running" block shows the live matches
//      whenever runs are in flight, even when a fresh epoch roll
//      transiently leaves the heartbeat's epoch tag out of step.
//   5. PROGRESS — a TERMINAL run reads 100%, keyed to completion rather than
//      to the wall-clock budget, so "1/1 tasks completed" never reads 0%.
//   6. TRANSCRIPT DEDUP — consecutive identical goal turns (goldfive emits the
//      goal on BOTH runStarted + goalDerived) collapse to one.
//
// (The epoch-objective truncation is a pure CSS change that the DOM harness
// cannot reach; console.css is inspected for it instead.)

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const livestatus = await import('../js/livestatus.js');
const shell = await import('../js/shell.js');
const live = await import('../js/live.js');
const STRUCT = { ...await import('../js/tournament_model.js'), ...await import('../js/views/structure.js') };
const candidate = await import('../js/views/candidate.js');
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
  // exactly one run-state pill: no separate `dt-run-badge` wrapper and no
  // separate pulse element, so the three competing "live" signals are one.
  assertEqual(allByClass(root, 'dt-run-state').length, 1, 'exactly one liveness pill in the chrome');
  assertEqual(allByClass(root, 'dt-run-badge').length, 0, 'the redundant standalone run-badge is removed');
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
  state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', seq: 3, ts: Date.now() });
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
  state.setHeartbeat({ phase: 'tournament:round_0:final', ts: Date.now() - 120_000 });
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
  state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', seq: 4, ts: t0 });
  state._changed();
  await new Promise((r) => setTimeout(r, 0));
  const statusEl = allByClass(root, 'dt-status')[0];
  const writesBefore = statusEl.innerHTMLWriteCount();
  const pill = allByClass(root, 'dt-run-state')[0];
  const labelNodeBefore = allByClass(pill, 'dt-run-label')[0].firstChild;

  // a NO-OP beat: same seq, a newer-but-same-bucket heartbeat ts.
  state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', seq: 4, ts: t0 + 500 });
  state._changed();
  await new Promise((r) => setTimeout(r, 0));
  assertEqual(state.lastSeq, 4, 'the cursor is unchanged by the no-op beat');
  assertEqual(statusEl.innerHTMLWriteCount(), writesBefore, 'NO innerHTML writes in the pill on a no-op beat');
  assert(allByClass(pill, 'dt-run-label')[0].firstChild === labelNodeBefore,
    'the phase-label text node identity is preserved on a no-op beat (no flash)');
});

// ════════════════════════════════════════════════════════════════════
// hero visibility gated on the orchestrator pulse (alive) rather than seq
// ════════════════════════════════════════════════════════════════════

test('livestatus: `alive` is true for LIVE and STALLED, false for SETTLED and DEAD', () => {
  // LIVE — seq advancing, fresh heartbeat.
  const liveSt = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', ts: NOW - 1000 },
    activeRuns: [{ generation_id: 'v1' }], activeTournament: { structure: 'racing', phase: 'running' },
    seq: 12, terminal: false, lastSeqAdvanceAt: NOW - 1000,
  }, NOW);
  assertEqual(liveSt.runState, livestatus.RUN_STATE.LIVE, 'LIVE');
  assertEqual(liveSt.alive, true, 'LIVE ⇒ alive');

  // STALLED — seq frozen past budget, but the heartbeat STILL pulses (a long
  // reasoning call): the orchestrator is alive, so the hero must NOT flicker out.
  const stalledSt = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', ts: NOW - 1000 /* fresh pulse */ },
    activeRuns: [], activeTournament: { structure: 'swiss', phase: 'running' },
    seq: 5, terminal: false, lastSeqAdvanceAt: NOW - (livestatus.SEQ_STALL_BUDGET_MS + 5000),
  }, NOW);
  assertEqual(stalledSt.runState, livestatus.RUN_STATE.STALLED, 'STALLED');
  assertEqual(stalledSt.alive, true, 'STALLED (orchestrator still pulsing) ⇒ alive — hero stays put');

  // SETTLED — terminal.
  const settledSt = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', ts: NOW - 1000 },
    activeRuns: [{ generation_id: 'v1' }], activeTournament: { structure: 'racing', phase: 'running' },
    seq: 20, terminal: true, lastSeqAdvanceAt: NOW - 500,
  }, NOW);
  assertEqual(settledSt.alive, false, 'SETTLED ⇒ not alive (hero hides)');

  // DEAD — seq frozen AND no fresh heartbeat.
  const deadSt = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', ts: NOW - (livestatus.STALE_HEARTBEAT_MS + 10_000) },
    activeRuns: [], activeTournament: { structure: 'swiss', phase: 'running' },
    seq: 5, terminal: false, lastSeqAdvanceAt: NOW - (livestatus.SEQ_STALL_BUDGET_MS + 5000),
  }, NOW);
  assertEqual(deadSt.alive, false, 'DEAD ⇒ not alive');
});

test('hero: a STALLED run (seq frozen, heartbeat still fresh) KEEPS the hero visible (dt-live-on)', () => {
  const c = new live.LiveController({});
  const at = { structure: 'racing', phase: 'running', epoch_id: 'e1', competitors: [{ generation_id: 'v0' }] };
  const heartbeat = { phase: 'tournament:round_0:rung0_m1', epoch_id: 'e1', ts: Date.now() };
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
// the "what's running" matches show while runs are live
// ════════════════════════════════════════════════════════════════════

// a racing tournament tagged to one epoch, with a heartbeat whose epoch tag is
// transiently DIFFERENT (a fresh roll), which an epoch gate would blank.
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
    heartbeat: { phase: 'tournament:rung1', epoch_id: hbEpochTag, ts: Date.now() },
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
  // tournament in 'e1'; the heartbeat AND the only run are in 'e2' (a truly
  // foreign run) — the tournament has no corroborating run, so no matches show.
  const at = racingInflight('e1', 'e2').at;
  const heartbeat = { phase: 'tournament:rung1', epoch_id: 'e2', ts: Date.now() };
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
  const digestBefore = c._matchesBody.getAttribute('data-t-digest');
  assert(firstNode && digestBefore, 'the matches block mounted');
  // an identical second tick → same digest → no rebuild (node identity preserved).
  c.update({ status: { running: true, alive: true, structure: 'racing' }, heartbeat, activeRuns, activeTournament: at });
  assertEqual(c._matchesBody.getAttribute('data-t-digest'), digestBefore, 'a no-op tick yields the SAME matches digest');
  assert(c._matchesBody.firstChild === firstNode, 'a no-op tick preserves the matches node (no flash)');
});

// ── W1 — the all-settled-rounds fallback (the between-rounds gap) ──────
//
// liveMatchBlocks derives ONLY from the published rounds; between rungs every
// published match is already SETTLED (winner/survivors) so it returns [] — yet a
// run is still in flight. The panel must NOT falsely read "no matches in flight".
function racingSettledRoundsInflight(epochTag, runEpochTag) {
  return {
    at: {
      structure: 'racing', phase: 'running', epoch_id: epochTag,
      structure_params: { rungs: [{ fraction: 0.5 }, { fraction: 1.0 }], board_size: 8 },
      champion_lineage: ['v0'],
      competitors: [
        { generation_id: 'v0', role: 'champion' }, { generation_id: 'v7', role: 'challenger' },
      ],
      // the ONLY published round is fully SETTLED (survivors + winner landed) —
      // liveMatchBlocks returns [] for it — but the next rung is warming up so a
      // run is in flight on /api/active-runs.
      rounds: [
        { round_index: 0, label: 'Rung 1', matches: [
          { match_id: 'rung1_m0', competitors: ['v0', 'v7'], board_fraction: 0.5,
            survivors: ['v7'], winner: 'v7', done: 8, total: 8 },
        ] },
      ],
      standings: [],
    },
    heartbeat: { phase: 'tournament:rung2', epoch_id: epochTag, ts: Date.now() },
    activeRuns: [{ generation_id: 'v7', entry_id: 'b0', run_id: 'r7', epoch_id: runEpochTag, boards_done: 2, boards_total: 8 }],
  };
}

test('matches: the "what\'s running" block falls back to the live runs when EVERY published round is settled but a run is still in flight', () => {
  const c = new live.LiveController({});
  // all published rounds settled → liveMatchBlocks() === [] → without the
  // fallback the panel reads "no matches in flight right now…" while v7 runs.
  const { at, heartbeat, activeRuns } = racingSettledRoundsInflight('e1', 'e1');
  c.update({ status: { running: true, alive: true, structure: 'racing' }, heartbeat, activeRuns, activeTournament: at });
  const body = c._matchesBody;
  const empty = allByClass(body, 'dt-live-matches-empty')[0];
  assert(!empty, 'no false "no matches in flight right now…" placeholder while a run is live');
  const block = allByClass(body, 'dt-live-match')[0];
  assert(block, 'a synthesized fallback match block renders for the in-flight run');
  assert(/v7/.test(textOf(body)), 'the fallback block names the in-flight competitor (v7)');
});

test('matches: the all-settled fallback stays scoped — a known-FOREIGN-epoch run does NOT synthesize a block', () => {
  const c = new live.LiveController({});
  // tournament in 'e1'; the only run is in 'e2' (truly foreign) — the fallback
  // must honour the same epoch guard as above and show the placeholder.
  const { at, heartbeat, activeRuns } = racingSettledRoundsInflight('e1', 'e2');
  c.update({ status: { running: true, alive: true, structure: 'racing' }, heartbeat, activeRuns, activeTournament: at });
  const empty = allByClass(c._matchesBody, 'dt-live-matches-empty')[0];
  assert(empty, 'a known-foreign-epoch run does not light up the all-settled tournament (placeholder shows)');
});

test('matches: a NO-OP beat over the synthesized all-settled fallback churns ZERO DOM', () => {
  const c = new live.LiveController({});
  const { at, heartbeat, activeRuns } = racingSettledRoundsInflight('e1', 'e1');
  c.update({ status: { running: true, alive: true, structure: 'racing' }, heartbeat, activeRuns, activeTournament: at });
  const firstNode = c._matchesBody.firstChild;
  const digestBefore = c._matchesBody.getAttribute('data-t-digest');
  assert(firstNode && digestBefore, 'the fallback block mounted');
  c.update({ status: { running: true, alive: true, structure: 'racing' }, heartbeat, activeRuns, activeTournament: at });
  assertEqual(c._matchesBody.getAttribute('data-t-digest'), digestBefore, 'a no-op tick yields the SAME fallback digest');
  assert(c._matchesBody.firstChild === firstNode, 'a no-op tick preserves the fallback node (no flash)');
});

// ════════════════════════════════════════════════════════════════════
// a terminal run reads 100% from completion rather than wall-clock budget
// ════════════════════════════════════════════════════════════════════

test('progress: a TERMINAL run reads 100% regardless of its elapsed/budget time-fraction', () => {
  // finished early, tiny elapsed/budget — completion wins over the time bar.
  // ONE spelling on the wire: `status` (the canonical entry vocabulary) and
  // `boards_done`/`boards_total` — the speculative tasks_*/done aliases no
  // server ever wrote are DELETED and must NOT read terminal.
  assertEqual(STRUCT.runProgressRatio({ status: 'completed', elapsed_seconds: 5, budget_seconds: 600 }), 1, 'status:completed ⇒ 100%');
  assertEqual(STRUCT.runProgressRatio({ status: 'pass' }), 1, 'a terminal pass ⇒ 100%');
  assertEqual(STRUCT.runProgressRatio({ boards_done: 8, boards_total: 8, elapsed_seconds: 2, budget_seconds: 600 }), 1,
    'all boards scored ⇒ 100% (not the 0%-ish time fraction)');
  // runIsTerminal mirrors the verdict — off the canonical fields only.
  assert(STRUCT.runIsTerminal({ boards_done: 8, boards_total: 8 }), 'runIsTerminal true once every board scored');
  assert(!STRUCT.runIsTerminal({ tasks_completed: 1, tasks_total: 1 }), 'the retired tasks_* alias never reads terminal');
  assert(!STRUCT.runIsTerminal({ done: true }), 'the retired done flag never reads terminal');
});

test('progress: an in-flight (non-terminal) run still reads its live time/board fraction', () => {
  assertEqual(STRUCT.runProgressRatio({ progress: 0.4 }), 0.4, 'an explicit fraction is honoured');
  assertEqual(STRUCT.runProgressRatio({ elapsed_seconds: 30, budget_seconds: 120 }), 0.25, 'elapsed/budget fallback');
  assertEqual(STRUCT.runProgressRatio({ boards_done: 1, boards_total: 3, progress: 0.33 }), 0.33,
    'a partial board count is NOT terminal → the live fraction reads through');
  assertEqual(STRUCT.runProgressRatio({}), null, 'a bare run with no progress signal reads null (running…)');
  assert(!STRUCT.runIsTerminal({ boards_done: 1, boards_total: 3 }), 'runIsTerminal false while boards remain');
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
// H1 — the BT gate's ratingProbBar thr label stays inside the W=260 box
// ════════════════════════════════════════════════════════════════════
//
// The Bradley-Terry gate's P(stronger)-vs-threshold bar (ratingProbBar, via the
// exported ratingBlock) must keep the `thr 0.NN` label INSIDE its W=260,
// preserveAspectRatio:'none' box. For the common high promote threshold
// (0.90-0.95) the mark sits near the right edge; a middle-anchored label there
// clips past the viewBox. The fix end-anchors + clamps the label inside the
// right padding near the edge, leaving the common centred case untouched.
{
  // Geometry mirrored from ratingProbBar (kept in sync by these assertions).
  const W = 260, padX = 4, axW = W - 2 * padX;

  // A credible BT rating that drives ratingProbBar with the given threshold.
  const ratingFixture = (threshold, p_stronger) => ({
    present: true, credible: true, n_duels: 8, decision: 'promoted',
    p_stronger, threshold,
    champion: { theta: 0.0, ci_lo: -0.4, ci_hi: 0.4 },
    challenger: { theta: 0.6, ci_lo: 0.2, ci_hi: 1.0 },
  });

  const thrLabel = (rating) => {
    const block = candidate.ratingBlock(rating);
    assert(block, 'ratingBlock renders for a credible rating');
    const lab = allByClass(block, 'dn-bt-prob-thrlab')[0];
    assert(lab, 'the thr label is present');
    return lab;
  };

  // H1 — the high-threshold label must NOT clip past the right viewBox edge.
  test('H1 ratingProbBar: high promote threshold (0.95) label stays inside the W=260 box', () => {
    const lab = thrLabel(ratingFixture(0.95, 0.97));
    assertEqual((lab.textContent || '').trim(), 'thr 0.95', 'the label reads the threshold');

    const anchor = lab.getAttribute('text-anchor');
    const x = Number(lab.getAttribute('x'));
    const tx = padX + 0.95 * axW; // the true threshold mark x (~243.4)
    assert(tx > W - 26, 'precondition: the threshold mark sits near the right edge');

    // Routed onto svg.edgeText: the near-edge label keeps its natural 'middle'
    // anchor and clamps x INWARD so its FULL rendered extent stays inside the box
    // (the no-clip invariant itself, rather than an end-anchor-plus-clamp proxy
    // for it). Extent computed with the same mono model edgeText uses (9px font,
    // CHAR_EM=0.6).
    const w = ('thr 0.95').length * 9 * 0.6;
    const left = anchor === 'end' ? x - w : anchor === 'start' ? x : x - w / 2;
    const right = left + w;
    assert(x <= W - padX + 0.001, 'the label anchor x is clamped inside the right padding');
    assert(left >= padX - 0.001 && right <= W - padX + 0.001, 'the full label extent stays inside [padX, W - padX] (no right-edge clip)');
  });

  // guard: the common centred case must be UNCHANGED (no regression).
  test('H1 ratingProbBar: a mid threshold (0.50) keeps the centred label on the mark', () => {
    const lab = thrLabel(ratingFixture(0.5, 0.55));
    const anchor = lab.getAttribute('text-anchor');
    const x = Number(lab.getAttribute('x'));
    const tx = padX + 0.5 * axW; // 130 — comfortably away from the edge

    assertEqual(anchor, 'middle', 'a mid threshold stays middle-anchored (common-case path untouched)');
    assert(Math.abs(x - tx) < 0.001, 'the centred label sits on the true threshold mark x');
  });
}

// ════════════════════════════════════════════════════════════════════
// H2 — the overlapMeter tolerance label stays inside the W=260 box
// ════════════════════════════════════════════════════════════════════
//
// The diversity-ribbon tolerance label ("tol 0.95") is drawn in a fixed-width,
// preserveAspectRatio:'none' viewBox (W=260). A near-1.0 tolerance puts the
// marker at tx≈243; a text-anchor:middle label there overruns the right edge
// and clips. The fix anchors/clamps the LABEL inside the box near an edge while
// leaving the common mid-bar case (middle @ tx) untouched.
function tolLabelOf(host) {
  return allByClass(host, 'dn-div-tollab')[0] || null;
}
function renderDiversity(tolerance) {
  const st = {
    structure: 'swiss', live: true, source: 'active', tournament_id: 't1',
    competitors: [{ generation_id: 'c1', seed: 1 }, { generation_id: 'c2', seed: 2 }],
    rounds: [], standings: [
      { generation_id: 'c1', rank: 1, scalar: 47.5, wins: 1, losses: 0, status: 'competing' },
      { generation_id: 'c2', rank: 2, scalar: 52.1, wins: 0, losses: 1, status: 'competing' },
    ],
    field_status: [
      { generation_id: 'c1', status: 'applied', diversity_status: 'applied' },
      { generation_id: 'c2', status: 'applied', diversity_status: 'applied' },
    ],
    diversity: { field_size: 2, distinct_ideas: 2, mean_overlap: 0.9, max_overlap: 0.97,
      max_overlap_pair: ['c1', 'c2'], tolerance, soft_rejected_count: 0 },
  };
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: (p) => String(p) };
  const nodes = STRUCT.renderStructure(st, ctx, '2026-05-30_e0');
  for (const n of (Array.isArray(nodes) ? nodes : [nodes])) if (n) host.appendChild(n);
  return host;
}

test('H2 · overlapMeter: a near-1.0 tolerance label is anchored/clamped INSIDE the W=260 bar', async () => {
  resetState();
  const W = 260, padX = 4, axW = W - 2 * padX;
  const host = renderDiversity(0.95);
  const lab = tolLabelOf(host);
  assert(lab, 'the diversity-ribbon tolerance label renders');
  assertEqual(lab.textContent, 'tol 0.95', 'the label reads the tolerance');
  const tx = padX + 0.95 * axW; // ≈ 243.4 — within ~26 of the W=260 right edge
  assert(tx > W - 26, 'precondition: the tol marker sits near the right edge');
  // Routed onto svg.edgeText: the label is clamped so its FULL rendered extent
  // stays inside [padX, W-padX] — the no-clip invariant, asserted directly off
  // the shared mono model (0.6 em/char @ 11px) rather than the prior end-anchor
  // mechanism. (BEFORE the fix a middle label here overran 260 and clipped.)
  const lx = Number(lab.getAttribute('x'));
  const anchor = lab.getAttribute('text-anchor');
  const w = lab.textContent.length * 11 * 0.6; // svg.textPx('tol 0.95', 11) = 52.8
  const left = anchor === 'middle' ? lx - w / 2 : anchor === 'end' ? lx - w : lx;
  const right = anchor === 'middle' ? lx + w / 2 : anchor === 'end' ? lx : lx + w;
  assert(left >= padX - 0.001, 'the label left extent stays inside the left pad (>= padX)');
  assert(right <= W - padX + 0.001, 'the label right extent stays inside the right pad (<= W - padX) — no clip');
  assert(lx < tx + 0.001, 'the clamped label was pulled INWARD from the near-edge mark (lx <= tx)');
});

test('H2 · overlapMeter: a mid-bar tolerance is UNCHANGED (middle-anchored at tx) — no regression', async () => {
  resetState();
  const W = 260, padX = 4, axW = W - 2 * padX;
  const host = renderDiversity(0.5);
  const lab = tolLabelOf(host);
  assert(lab, 'the mid-bar tolerance label renders');
  // The common mid-bar case is byte-identical through svg.edgeText: a centered
  // label comfortably inside the box keeps anchor='middle' and x=tx untouched.
  assertEqual(lab.getAttribute('text-anchor'), 'middle', 'a mid-bar tolerance keeps the original middle anchor');
  assertEqual(Number(lab.getAttribute('x')), padX + 0.5 * axW, 'a mid-bar tolerance label keeps x = tx (byte-identical render)');
});

// ════════════════════════════════════════════════════════════════════
// M9 · rungProgression stage labels — a long swiss/elim run (n≥7) in the
// narrow compare width (w=480) shrinks `step` to ~64px, where the middle-
// anchored 14-char stage labels (and their Δ sublabels) run into their
// neighbours. The label cap is tightened ∝ step (min(14,max(4,floor(step/6)))),
// bounded ABOVE by the prior 14 so every wide/few-stage figure is unchanged.
// ════════════════════════════════════════════════════════════════════

function rungLabelsOf(svg, cls) {
  return svg.querySelectorAll('[class]')
    .filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls))
    .map((n) => n.textContent);
}

test('M9 · rungProgression: a long run (n=8) in the narrow compare width tightens the label clip ∝ step (no neighbour collision)', async () => {
  const dag = await import('../js/dag.js');
  // n=8 at w=480: usable = 480-28 = 452, step = 452/7 ≈ 64.57 → labelCap = floor(64.57/6) = 10.
  const LONG = 'metrics_outline_v3'; // 18 chars — exceeds both the old (14) and new (10) caps.
  const stages = Array.from({ length: 8 }, (_, i) => ({
    label: LONG, kind: i === 7 ? 'final' : 'rung', delta: -3.2, verdict: i === 7 ? 'promoted' : 'survived',
  }));
  const svg = dag.rungProgression({ stages, width: 480 });

  const labels = rungLabelsOf(svg, 'ezn-rungprog-label');
  const subs = rungLabelsOf(svg, 'ezn-rungprog-sub');
  assertEqual(labels.length, 8, 'one label per stage');
  // BEFORE the fix every label is clip(LONG,14) → length 14; AFTER it is clip(LONG,10) → length 10.
  for (const t of labels) assert(t.length <= 10, 'each narrow-step stage label is clipped to the step-proportional cap (≤10, not 14): got ' + JSON.stringify(t));
  for (const t of subs) assert(t.length <= 10, 'each narrow-step Δ sublabel is clipped to the SAME cap (≤10): got ' + JSON.stringify(t));
});

test('M9 · rungProgression: a wide / few-stage strip is UNCHANGED — the prior 14-char cap still applies (no regression)', async () => {
  const dag = await import('../js/dag.js');
  // n=3 at the default w=720: usable = 692, step = 346 → floor(346/6)=57, min(14,57)=14 → original cap.
  const LABEL = 'rung-zero-1234'; // exactly 14 chars: untouched by a 14-cap, would be cut by a tighter one.
  const stages = ['won', 'survived', 'promoted'].map((v, i) => ({ label: LABEL, kind: i === 2 ? 'final' : 'rung', verdict: v }));
  const svg = dag.rungProgression({ stages, width: 720 });
  const labels = rungLabelsOf(svg, 'ezn-rungprog-label');
  assertEqual(labels.length, 3, 'one label per stage');
  for (const t of labels) assertEqual(t, LABEL, 'a wide-step 14-char label renders in full (byte-identical to the pre-fix cap of 14)');
});

// ════════════════════════════════════════════════════════════════════
// M10 — the raced-entry per-run hover panel stays LEFT of the Σ node in
// the narrow compare-split (lifecycleDag width 560)
// ════════════════════════════════════════════════════════════════════
//
// A re-raced board entry reveals a per-run stack panel anchored to the RIGHT of
// the board disc (X.board + r + 6). With a FIXED panelW=150 in the narrow
// compare width (560) the panel ran to ~426 and overlapped the Σ node
// (X.agg = 0.66·560 = 369.6) and its Σ→GATE edge. The fix clamps panelW so the
// panel's right edge stays a pad short of X.agg; it ONLY shrinks, so the wide
// width-900 layout (where the panel already clears Σ) is unchanged.
{
  const dagM10 = await import('../js/dag.js');
  const racedEntries = [
    { entry_id: 'q3_metrics_outline', run_id: 'r0', drift_loss: 4.0, pass_fail: true, wall_clock_budget_exceeded: false },
    { entry_id: 'q3_metrics_outline', run_id: 'r1', drift_loss: 64.0, pass_fail: false, wall_clock_budget_exceeded: false },
    { entry_id: 'q3_metrics_outline', run_id: 'r2', drift_loss: 63.5, pass_fail: false, wall_clock_budget_exceeded: false },
  ];
  const panelBoxOf = (svgNode) =>
    svgNode.querySelectorAll('[class]').filter((n) =>
      (n.getAttribute('class') || '').split(/\s+/).includes('ezn-board-runs-box'))[0] || null;

  test('M10 lifecycle per-run panel: in the narrow compare-split (width 560) the panel stays LEFT of the Σ node', () => {
    const w = 560;
    const svgNode = dagM10.lifecycleDag({ genId: 'v3', parentId: 'v0', entries: racedEntries, decision: 'rejected', width: w });
    const box = panelBoxOf(svgNode);
    assert(box, 'the raced entry carries a per-run panel box');
    const x = Number(box.getAttribute('x'));
    const pw = Number(box.getAttribute('width'));
    const Xagg = 0.66 * w; // 369.6 — the Σ node centre
    // BEFORE the fix: x (275.6) + 150 = 425.6 > Xagg → overlaps Σ. AFTER: clamped.
    assert(x + pw <= Xagg + 0.001, 'the panel right edge does not cross the Σ node x (no Σ / Σ→GATE overlap)');
    assert(pw >= 64, 'the clamped panel keeps a legible minimum width');
  });

  test('M10 lifecycle per-run panel: the WIDE width-900 layout is UNCHANGED (panelW stays 150) — no regression', () => {
    const svgNode = dagM10.lifecycleDag({ genId: 'v3', parentId: 'v0', entries: racedEntries, decision: 'rejected', width: 900 });
    const box = panelBoxOf(svgNode);
    assert(box, 'the raced entry carries a per-run panel box at the wide width');
    assertEqual(Number(box.getAttribute('width')), 150, 'the wide layout keeps the default 150px panel (clamp only shrinks)');
  });
}

// ════════════════════════════════════════════════════════════════════
// M11 — the generalizationPanel holdout value stays inside the W=240 box
// ════════════════════════════════════════════════════════════════════
//
// The train→holdout slope's holdout value (dn-gen-val) was text-anchor:start at
// xH+10=178 in a W=240 viewBox, so a long 3-dp value ("-123.456", 8 chars) grew
// RIGHTWARD to ~228-235 and grazed/clipped the right edge. The fix right-anchors
// it (text-anchor:end) at the box right margin (W-4) so it grows leftward and
// stays on-canvas; short values still render just right of the holdout dot. Same
// anti-clip pattern as H1/H2 above.
{
  const W = 240; // mirrors generalizationPanel's viewBox width (kept in sync here)

  const holdoutVal = (host) =>
    allByClass(host, 'dn-gen-val').find((n) =>
      !(n.getAttribute('class') || '').split(/\s+/).includes('dn-gen-train-t')) || null;

  test('M11 generalizationPanel: a long 3-dp holdout value is end-anchored at the W=240 right margin (no right-edge clip)', async () => {
    const svg = await import('../js/svg.js');
    // a holdout far below train → an 8-char value "-123.456" that would clip if left-anchored.
    const card = candidate.generalizationPanel({ train: 0.5, holdout: -123.456, gap: -123.956, tolerance: 0.05 });
    const lab = holdoutVal(card);
    assert(lab, 'the holdout value label renders');
    assertEqual(lab.textContent, svg.fmt(-123.456, 3), 'the holdout value text is unchanged (fmt(v,3) = "-123.456")');
    // BEFORE the fix: text-anchor:start at xH+10=178. AFTER: end-anchored at the right margin.
    assertEqual(lab.getAttribute('text-anchor'), 'end', 'the holdout value is end-anchored (grows leftward, never off the right edge)');
    const x = Number(lab.getAttribute('x'));
    assert(x <= W - 4 + 0.001, `the end-anchored value x (${x}) sits at/inside the right margin (W-4=${W - 4}) — it cannot run off the ${W}px viewBox`);
  });

  test('M11 generalizationPanel: a normal short holdout value renders unchanged text on-canvas (no regression)', async () => {
    const svg = await import('../js/svg.js');
    const card = candidate.generalizationPanel({ train: 0.60, holdout: 0.62, gap: 0.02, tolerance: 0.05 });
    const lab = holdoutVal(card);
    assert(lab, 'the holdout value label renders for the common short value');
    assertEqual(lab.textContent, svg.fmt(0.62, 3), 'the short holdout value text is unchanged (fmt(v,3))');
    const x = Number(lab.getAttribute('x'));
    assert(x > 0 && x <= W - 4 + 0.001, `a short holdout value (x=${x}) also stays inside the box`);
    // the train value label is untouched (still end-anchored, left of the train dot).
    const train = allByClass(card, 'dn-gen-train-t')[0];
    assert(train, 'the train value label renders');
    assertEqual(train.getAttribute('text-anchor'), 'end', 'the train value stays end-anchored (untouched by the holdout fix)');
  });
}

// ════════════════════════════════════════════════════════════════════
// L10 — the BT ratingWhisker marker stays ROUND (no none-stretch ellipse)
// ════════════════════════════════════════════════════════════════════
//
// The two θ̂ whiskers (ratingWhisker, via the exported ratingBlock) draw a round
// θ̂ <circle r=3.5> + vertical CI end-caps inside a width:100%,
// preserveAspectRatio:'none', viewBox 0 0 220 26 box. With a FIXED `height` the
// flexible grid column stretches X past Y, so the round marker renders as a
// horizontal ellipse. The fix drops the fixed height and pins an inline
// `aspect-ratio: 220 / 26` so the 'none' scale is UNIFORM (no shear) — the
// marker stays round, with NO change to the rail/CI/θ̂ x-positions.
{
  const W = 220, H = 26;
  // a credible rating that drives two ratingWhiskers (both sides fit, CIs present).
  const credibleRating = () => ({
    present: true, credible: true, n_duels: 8, decision: 'deferred',
    p_stronger: 0.82, threshold: 0.9, ci_overlap: true,
    champion: { theta: 0.0, se: 0.2, ci_lo: -0.4, ci_hi: 0.4 },
    challenger: { theta: 0.5, se: 0.25, ci_lo: 0.1, ci_hi: 0.9 },
  });
  const whiskerSvgs = (rating) => {
    const block = candidate.ratingBlock(rating);
    assert(block, 'ratingBlock renders for a credible rating');
    return allByClass(block, 'dn-bt-whisker');
  };

  test('L10 ratingWhisker: the whisker box pins aspect-ratio so a none-scale stays UNIFORM (round θ̂)', () => {
    const svgs = whiskerSvgs(credibleRating());
    assertEqual(svgs.length, 2, 'two whiskers — champion + challenger');
    for (const fig of svgs) {
      assertEqual(fig.getAttribute('preserveAspectRatio'), 'none', 'the box keeps preserveAspectRatio:none (full-width rail)');
      // the aspect lock that makes the 'none' scale uniform (== the viewBox aspect).
      const style = fig.getAttribute('style') || '';
      assert(/aspect-ratio:\s*220\s*\/\s*26/.test(style), 'the box pins aspect-ratio: 220 / 26 (uniform scale → round marker)');
      // a fixed height attr alongside width:100% is what produced the X-stretch — it must be gone.
      assert(fig.getAttribute('height') == null, 'no fixed height attr (the aspect-ratio drives the box height)');
      assertEqual(fig.getAttribute('width'), '100%', 'the box still spans the full column width');
      assertEqual(fig.getAttribute('viewBox'), `0 0 ${W} ${H}`, 'the viewBox is unchanged (x-domain mapping preserved)');
    }
  });

  test('L10 ratingWhisker: the θ̂ marker + CI end-caps are drawn at their unchanged viewBox coords', () => {
    // the fix is box-level only: the geometry inside the box is byte-identical.
    const block = candidate.ratingBlock(credibleRating());
    const theta = allByClass(block, 'dn-bt-theta')[0];
    assert(theta, 'the θ̂ marker is drawn');
    assertEqual(theta.getAttribute('r'), '3.5', 'the θ̂ circle keeps its r=3.5 (round in a uniform box)');
    const caps = allByClass(block, 'dn-bt-cap');
    assert(caps.length >= 2, 'the CI end-caps are drawn');
  });
}

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
