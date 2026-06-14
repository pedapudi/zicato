// test/seq_render_gate.test.mjs — the SEQ-DRIVEN liveness + render gate
// (RUNTIME-V2 Phase 4), the render-discipline BACKBONE.
//
// Pins, in four parts:
//  1. state.noteProgress — the progress cursor: advance / repeat-no-op /
//     rollover (restarted log) / absent-seq-degrade semantics.
//  2. core/sse.js — the SEQ NO-OP-SKIP GATE: a `state_change` frame whose seq
//     does NOT advance issues NO /api/environment fetch (zero work, zero DOM);
//     a real advance fetches; a rollover forces a fetch + resets the cursor; a
//     frame with NO seq DEGRADES to the legacy always-refresh path.
//  3. livestatus.deriveLiveStatus — the FOUR-STATE run verdict (LIVE / STALLED
//     / SETTLED / DEAD) keyed on the seq cursor + terminal marker, with the
//     legacy timestamp degrade when no seq is known; + liveStatusDigest folds
//     the discrete run-state but NOT the climbing advance-age (no re-stamp on a
//     steady tick), + runStateLabel.
//  4. the chrome — shell.mountShell paints the `dt-run-state` pill; a no-op beat
//     (same seq) churns ZERO DOM (the digest is byte-identical); a real
//     transition flips the pill.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const livestatus = await import('../js/livestatus.js');
const { state } = await import('../js/core/state.js');
const { bus } = await import('../js/core/bus.js');
const data = await import('../js/data.js');
const sse = await import('../js/core/sse.js');
const shell = await import('../js/shell.js');

const NOW = 1_700_000_000_000;

function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}

// Reset the AppState progress cursor + live signals between tests (the
// AppState singleton is SHARED across every test file/case, so a prior test's
// heartbeat / tournament / runs must be cleared or they leak in).
function resetCursor() {
  state.lastSeq = -1;
  state.terminal = false;
  state.lastSeqAdvanceAt = NaN;
  state.heartbeat = null;
  state.activeTournament = null;
  state.activeRuns = [];
  state.connected = false;
}

// ════════════════════════════════════════════════════════════════════
// PART 1 — state.noteProgress (the pure cursor)
// ════════════════════════════════════════════════════════════════════

test('noteProgress: the FIRST seq seen is adopted + counts as an advance', () => {
  resetCursor();
  const v = state.noteProgress(5, false, NOW);
  assert(v.present, 'a numeric seq is present');
  assert(v.advanced, 'the first seq ever counts as an advance (a fresh load must paint)');
  assert(!v.rollover, 'the first seq is not a rollover');
  assertEqual(state.lastSeq, 5, 'the cursor adopts the first seq');
  assertEqual(state.lastSeqAdvanceAt, NOW, 'the advance timestamp is stamped');
});

test('noteProgress: a strictly GREATER seq advances the cursor + restamps the advance time', () => {
  resetCursor();
  state.noteProgress(5, false, NOW);
  const v = state.noteProgress(6, false, NOW + 1000);
  assert(v.advanced && !v.rollover, 'a greater seq is an advance, not a rollover');
  assertEqual(state.lastSeq, 6, 'the cursor moves to the new seq');
  assertEqual(state.lastSeqAdvanceAt, NOW + 1000, 'the advance time restamps on a real advance');
});

test('noteProgress: a REPEAT seq is a no-op — no advance, no rollover, cursor + advance-time unchanged', () => {
  resetCursor();
  state.noteProgress(7, false, NOW);
  const v = state.noteProgress(7, false, NOW + 9000);
  assert(!v.advanced, 'a repeat seq is NOT an advance (the no-op-skip case)');
  assert(!v.rollover, 'a repeat seq is NOT a rollover');
  assertEqual(state.lastSeq, 7, 'the cursor stays put on a repeat');
  assertEqual(state.lastSeqAdvanceAt, NOW, 'the advance time is NOT restamped by a repeat beat');
});

test('noteProgress: a BACKWARDS seq is a ROLLOVER (the log was cleared → restarted at a low seq)', () => {
  resetCursor();
  state.noteProgress(42, false, NOW);
  const v = state.noteProgress(1, false, NOW + 5000);
  assert(v.rollover, 'a lower seq than the cursor is a rollover (restarted progress log)');
  assert(!v.advanced, 'a rollover is flagged as rollover, not advance');
  assertEqual(state.lastSeq, 1, 'the cursor RESETS to the new low seq after a rollover');
  assertEqual(state.lastSeqAdvanceAt, NOW + 5000, 'a rollover restamps the advance time (fresh run)');
});

test('noteProgress: a NON-numeric / absent seq is not present (degrade signal) + leaves the cursor', () => {
  resetCursor();
  state.noteProgress(3, true, NOW);
  const abs = state.noteProgress(undefined, undefined, NOW + 1000);
  assert(!abs.present, 'an absent seq reports present:false → the caller must degrade');
  assert(!abs.advanced && !abs.rollover, 'an absent seq neither advances nor rolls over');
  assertEqual(state.lastSeq, 3, 'an absent seq does not disturb the cursor');
  assertEqual(state.terminal, true, 'the terminal marker from the prior frame is retained');
  const nan = state.noteProgress(NaN, false, NOW + 2000);
  assert(!nan.present, 'a NaN seq is treated as absent (present:false)');
});

test('noteProgress: the terminal marker tracks the latest boolean, untouched by an absent one', () => {
  resetCursor();
  state.noteProgress(1, false, NOW);
  assertEqual(state.terminal, false, 'terminal starts false');
  state.noteProgress(2, true, NOW + 1);
  assertEqual(state.terminal, true, 'a terminal frame flips the marker');
  state.noteProgress(2, undefined, NOW + 2);
  assertEqual(state.terminal, true, 'a non-boolean terminal leaves the marker as-is');
});

test('setHeartbeat folds the heartbeat seq into the cursor (environment-poll path, no SSE)', () => {
  resetCursor();
  // a heartbeat carries seq mirroring the SSE frame — folding it keeps the
  // cursor current even with no SSE wiring.
  state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', seq: 11, last_heartbeat: NOW });
  assertEqual(state.lastSeq, 11, 'the heartbeat seq advances the cursor');
  // a minimal beat with no seq must NOT move the cursor.
  state.setHeartbeat({ last_heartbeat: NOW + 1000 });
  assertEqual(state.lastSeq, 11, 'a seq-less minimal beat does not move the cursor');
});

// ════════════════════════════════════════════════════════════════════
// PART 2 — the SSE no-op-skip gate (core/sse.js)
// ════════════════════════════════════════════════════════════════════

// A mock EventSource that records its listeners so a test can dispatch frames.
function installMockSse() {
  const listeners = {};
  let envFetches = 0;
  globalThis.EventSource = function EventSource() {
    this.readyState = 0;
    this.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
    this.close = () => {};
  };
  globalThis.EventSource.CLOSED = 2;
  // spy on /api/environment fetches — the gate's observable effect.
  globalThis.fetch = async (path) => {
    if (String(path).startsWith('/api/environment')) envFetches += 1;
    return { ok: true, async json() { return {}; } };
  };
  const fire = (type, dataObj) => {
    for (const fn of (listeners[type] || [])) fn({ type, data: JSON.stringify(dataObj) });
  };
  return { fire, envFetches: () => envFetches };
}

// Let the 400ms-debounced environment refresh fire.
async function settleDebounce() { await new Promise((r) => setTimeout(r, 480)); }

test('sse gate: a NO-OP state_change (same seq) issues NO environment fetch — zero work', async () => {
  resetCursor();
  const m = installMockSse();
  sse.connectSSE();
  // first frame at seq 4 — a genuine advance → one fetch.
  m.fire('state_change', { type: 'state_change', kind: 'progress', seq: 4, terminal: false, ts: 'x' });
  await settleDebounce();
  assertEqual(m.envFetches(), 1, 'the first (advancing) frame fetches the environment once');
  // a coalesced no-op beat re-emitting the SAME seq → NO fetch.
  m.fire('state_change', { type: 'state_change', kind: 'heartbeat', seq: 4, terminal: false, ts: 'y' });
  await settleDebounce();
  assertEqual(m.envFetches(), 1, 'a repeat-seq state_change writes ZERO DOM + issues no fetch');
  assertEqual(state.lastSeq, 4, 'the cursor is unchanged by the no-op beat');
});

test('sse gate: a state_change whose seq ADVANCES issues exactly one environment fetch', async () => {
  resetCursor();
  const m = installMockSse();
  sse.connectSSE();
  m.fire('state_change', { type: 'state_change', kind: 'progress', seq: 1, terminal: false });
  await settleDebounce();
  const before = m.envFetches();
  m.fire('state_change', { type: 'state_change', kind: 'progress', seq: 2, terminal: false });
  await settleDebounce();
  assertEqual(m.envFetches(), before + 1, 'an advancing seq fetches the environment');
  assertEqual(state.lastSeq, 2, 'the cursor advances to the new seq');
});

test('sse gate: a ROLLOVER (seq goes backwards = restarted log) FORCES a refresh + resets the cursor', async () => {
  resetCursor();
  const m = installMockSse();
  sse.connectSSE();
  m.fire('state_change', { type: 'state_change', kind: 'progress', seq: 30, terminal: false });
  await settleDebounce();
  const before = m.envFetches();
  // the run restarted: the progress log was cleared, seq begins again at 1.
  m.fire('state_change', { type: 'state_change', kind: 'progress', seq: 1, terminal: false });
  await settleDebounce();
  assertEqual(m.envFetches(), before + 1, 'a rollover forces a full re-apply (one fetch)');
  assertEqual(state.lastSeq, 1, 'the cursor resets to the restarted low seq');
});

test('sse gate: a frame with NO seq DEGRADES to the legacy always-refresh path', async () => {
  resetCursor();
  const m = installMockSse();
  sse.connectSSE();
  // a pre-RUNTIME-V2 server sends no seq — every beat must still refresh.
  m.fire('state_change', { type: 'state_change', kind: 'progress', kinds: ['progress'], ts: 'a' });
  await settleDebounce();
  const after1 = m.envFetches();
  assert(after1 >= 1, 'a seq-less frame fetches (legacy path)');
  m.fire('state_change', { type: 'state_change', kind: 'lineage', kinds: ['lineage'], ts: 'b' });
  await settleDebounce();
  assertEqual(m.envFetches(), after1 + 1, 'each seq-less beat refreshes (no skip without a seq)');
  assertEqual(state.lastSeq, -1, 'the cursor is never moved by a seq-less frame');
});

test('sse gate: a terminal SETTLED state_change advances the cursor + marks terminal', async () => {
  resetCursor();
  const m = installMockSse();
  sse.connectSSE();
  m.fire('state_change', { type: 'state_change', kind: 'progress', seq: 8, terminal: true });
  await settleDebounce();
  assertEqual(state.lastSeq, 8, 'the terminal frame advanced the cursor');
  assertEqual(state.terminal, true, 'the terminal marker is recorded so the pill reads SETTLED');
});

// ════════════════════════════════════════════════════════════════════
// PART 3 — deriveLiveStatus four-state + digest + label
// ════════════════════════════════════════════════════════════════════

const RS = livestatus.RUN_STATE;

test('run-state: a fresh seq advance reads LIVE', () => {
  const s = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - 1000 },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0' }],
    activeTournament: { structure: 'racing', phase: 'running' },
    seq: 12, terminal: false, lastSeqAdvanceAt: NOW - 2000,
  }, NOW);
  assertEqual(s.runState, RS.LIVE, 'seq advanced within budget ⇒ LIVE');
});

test('run-state: a TERMINAL marker reads SETTLED — authoritative over everything else', () => {
  const s = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - 1000 },
    activeRuns: [{ generation_id: 'v1' }],
    activeTournament: { structure: 'racing', phase: 'running' },
    seq: 20, terminal: true, lastSeqAdvanceAt: NOW - 500 /* even a recent advance */,
  }, NOW);
  assertEqual(s.runState, RS.SETTLED, 'a terminal progress marker ⇒ SETTLED regardless of a recent advance');
});

test('run-state: seq frozen past budget but heartbeat STILL pulsing reads STALLED', () => {
  const s = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - 1000 /* fresh pulse */ },
    activeRuns: [],
    activeTournament: { structure: 'swiss', phase: 'running' },
    seq: 5, terminal: false,
    lastSeqAdvanceAt: NOW - (livestatus.SEQ_STALL_BUDGET_MS + 5000) /* stale advance */,
  }, NOW);
  assertEqual(s.runState, RS.STALLED, 'no advance within budget + a live heartbeat ⇒ STALLED');
});

test('run-state: seq frozen AND no fresh heartbeat (no pulse) reads DEAD', () => {
  const s = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - (livestatus.STALE_HEARTBEAT_MS + 10_000) },
    activeRuns: [],
    activeTournament: { structure: 'swiss', phase: 'running' },
    seq: 5, terminal: false,
    lastSeqAdvanceAt: NOW - (livestatus.SEQ_STALL_BUDGET_MS + 5000),
  }, NOW);
  assertEqual(s.runState, RS.DEAD, 'no advance within budget + no fresh heartbeat ⇒ DEAD');
});

test('run-state: an in-flight board unit keeps a frozen-seq run STALLED (a worker pulse, not DEAD)', () => {
  const s = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - (livestatus.STALE_HEARTBEAT_MS + 10_000) },
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0' }] /* a per-run beater pulse */,
    activeTournament: { structure: 'racing', phase: 'running' },
    seq: 5, terminal: false,
    lastSeqAdvanceAt: NOW - (livestatus.SEQ_STALL_BUDGET_MS + 5000),
  }, NOW);
  assertEqual(s.runState, RS.STALLED, 'an in-flight board unit corroborates a pulse ⇒ STALLED, not DEAD');
});

test('run-state DEGRADE: with NO seq known (seq -1) it falls back to the timestamp verdict', () => {
  // running per the timestamp path ⇒ LIVE.
  const live = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'proposing:field', last_heartbeat: NOW - 1000 },
    activeRuns: [], activeTournament: null,
    seq: -1, terminal: false, lastSeqAdvanceAt: NaN,
  }, NOW);
  assertEqual(live.runState, RS.LIVE, 'no seq + a running timestamp verdict ⇒ LIVE (byte-identical degrade)');
  assertEqual(live.seqKnown, false, 'seqKnown is false on the degrade path');
  // a frozen heartbeat (stale, not running) ⇒ DEAD.
  const dead = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:final', last_heartbeat: NOW - (livestatus.STALE_HEARTBEAT_MS + 5000) },
    activeRuns: [], activeTournament: null,
    seq: -1, terminal: false, lastSeqAdvanceAt: NaN,
  }, NOW);
  assertEqual(dead.runState, RS.DEAD, 'no seq + a stale frozen heartbeat ⇒ DEAD');
  // an idle/done workspace ⇒ SETTLED.
  const settled = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'idle', last_heartbeat: NOW - 1000 },
    activeRuns: [], activeTournament: null,
    seq: -1, terminal: false, lastSeqAdvanceAt: NaN,
  }, NOW);
  assertEqual(settled.runState, RS.SETTLED, 'no seq + an idle (not stale) workspace ⇒ SETTLED');
});

test('runStateLabel maps each state to its uppercased chrome word', () => {
  assertEqual(livestatus.runStateLabel(RS.LIVE), 'LIVE');
  assertEqual(livestatus.runStateLabel(RS.STALLED), 'STALLED');
  assertEqual(livestatus.runStateLabel(RS.SETTLED), 'SETTLED');
  assertEqual(livestatus.runStateLabel(RS.DEAD), 'DEAD');
  assertEqual(livestatus.runStateLabel('nonsense'), '', 'an unknown token reads as no label');
});

test('liveStatusDigest: a steady tick (same run-state, climbing advance-age) is BYTE-IDENTICAL', () => {
  // Two derivations one tick apart: the seq advance-age climbs, but the
  // discrete run-state is unchanged ⇒ the digest must NOT flip (the
  // render-discipline rule: never fold the climbing age).
  const a = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - 1000 },
    activeRuns: [{ generation_id: 'v1' }],
    activeTournament: { structure: 'racing', phase: 'running' },
    seq: 9, terminal: false, lastSeqAdvanceAt: NOW - 3000,
  }, NOW);
  const b = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW + 2000 },
    activeRuns: [{ generation_id: 'v1' }],
    activeTournament: { structure: 'racing', phase: 'running' },
    seq: 9, terminal: false, lastSeqAdvanceAt: NOW - 3000,
  }, NOW + 3000 /* a tick later — advance-age climbed by 3s */);
  assertEqual(a.runState, RS.LIVE, 'both ticks read LIVE');
  assertEqual(
    livestatus.liveStatusDigest('live', a),
    livestatus.liveStatusDigest('live', b),
    'the digest is byte-identical across a steady tick (the climbing advance-age is NOT folded)',
  );
});

test('liveStatusDigest: a run-state TRANSITION flips the digest', () => {
  const liveSt = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - 1000 },
    activeRuns: [{ generation_id: 'v1' }],
    activeTournament: { structure: 'racing', phase: 'running' },
    seq: 9, terminal: false, lastSeqAdvanceAt: NOW - 2000,
  }, NOW);
  const settledSt = livestatus.deriveLiveStatus({
    heartbeat: { phase: 'tournament:round_0:rung0_m1', last_heartbeat: NOW - 1000 },
    activeRuns: [{ generation_id: 'v1' }],
    activeTournament: { structure: 'racing', phase: 'running' },
    seq: 9, terminal: true, lastSeqAdvanceAt: NOW - 2000,
  }, NOW);
  assert(
    livestatus.liveStatusDigest('live', liveSt) !== livestatus.liveStatusDigest('live', settledSt),
    'a LIVE → SETTLED transition flips the status digest (the pill repaints)',
  );
});

// ════════════════════════════════════════════════════════════════════
// PART 4 — the chrome pill via the real shell + digest stability
// ════════════════════════════════════════════════════════════════════

function mountChrome() {
  const document = installDom();
  resetCursor();
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
  });
  globalThis.location = loc;
  globalThis.window.location = loc;
  globalThis.window.dispatchEvent = () => {};
  const root = document.createElement('div');
  document.body.appendChild(root);
  shell.mountShell(root);
  return root;
}

test('chrome: the dt-run-state pill paints LIVE on an advancing seq + flips to SETTLED on terminal', async () => {
  const root = mountChrome();
  await new Promise((r) => setTimeout(r, 0));
  const pill = allByClass(root, 'dt-run-state')[0];
  assert(pill, 'the four-state run pill is mounted in the chrome');

  // drive a LIVE run: an advancing seq + a fresh heartbeat.
  state.connected = true;
  state.activeTournament = { structure: 'racing', phase: 'running' };
  state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0' }];
  state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', seq: 3, last_heartbeat: Date.now() });
  state._changed();
  await new Promise((r) => setTimeout(r, 0));
  assert(allByClass(root, 'dt-rs-live')[0], 'an advancing seq lights the pill LIVE');
  assert((pill.textContent || '').toUpperCase().includes('LIVE'), 'the pill word reads LIVE');

  // settle the run: a terminal frame.
  state.terminal = true;
  state._changed();
  await new Promise((r) => setTimeout(r, 0));
  assert(allByClass(root, 'dt-rs-settled')[0], 'a terminal marker flips the pill to SETTLED');
  assert(!allByClass(root, 'dt-rs-live')[0], 'the LIVE class is cleared on SETTLED');
  assert((pill.textContent || '').toUpperCase().includes('SETTLED'), 'the pill word reads SETTLED');
});

test('chrome: a NO-OP heartbeat beat (same seq) churns ZERO DOM in the status pill', async () => {
  const root = mountChrome();
  await new Promise((r) => setTimeout(r, 0));
  // establish a LIVE state.
  state.connected = true;
  state.activeTournament = { structure: 'racing', phase: 'running' };
  state.activeRuns = [{ generation_id: 'v1', entry_id: 'b0' }];
  const t0 = Date.now();
  state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', seq: 4, last_heartbeat: t0 });
  state._changed();
  await new Promise((r) => setTimeout(r, 0));
  const statusEl = allByClass(root, 'dt-status')[0];
  assert(statusEl, 'the status pill mounted');
  const writesBefore = statusEl.innerHTMLWriteCount();
  const pill = allByClass(root, 'dt-run-state')[0];
  const firstChildBefore = pill.firstChild;

  // a NO-OP beat: same seq, a slightly newer (but same-bucket) heartbeat ts.
  state.setHeartbeat({ phase: 'tournament:round_0:rung0_m1', seq: 4, last_heartbeat: t0 + 500 });
  state._changed();
  await new Promise((r) => setTimeout(r, 0));
  assertEqual(state.lastSeq, 4, 'the cursor is unchanged by the no-op beat');
  assertEqual(statusEl.innerHTMLWriteCount(), writesBefore, 'NO innerHTML writes in the status pill on a no-op beat');
  assert(pill.firstChild === firstChildBefore, 'the pill node identity is preserved on a no-op beat');
  assert(allByClass(root, 'dt-rs-live')[0], 'the pill is still LIVE after the no-op beat');
});

test('chrome: a never-run workspace shows NO run-state word (SETTLED would be misleading)', async () => {
  const root = mountChrome();
  // no heartbeat, no tournament, no runs, no seq → nothing to report.
  state._changed();
  await new Promise((r) => setTimeout(r, 0));
  const pill = allByClass(root, 'dt-run-state')[0];
  assert(pill, 'the pill element exists');
  assert(!allByClass(root, 'dt-rs-on')[0], 'the pill is not "on" for a never-run workspace');
  assertEqual((pill.textContent || '').trim(), '', 'no run-state word on a never-run workspace');
});

// TEARDOWN — the AppState / bus / SSE module are PROCESS-WIDE singletons shared
// with every other test file (run-all imports them in turn). This file mounted
// the shell (which subscribes `state:changed` on the bus) and drove the SSE
// debounce (a pending 400ms timer). Reset them so the NEXT file starts clean —
// no stray `state:changed` listener firing into a detached shell, no late
// environment refresh landing mid-render.
test('teardown: reset the shared AppState / bus subscriptions for the next file', async () => {
  // let any in-flight debounce settle, then drop every bus subscription this
  // file's shell mount registered.
  await new Promise((r) => setTimeout(r, 480));
  bus._reset();
  // mounting the shell warmed the data.js module cache (a PROCESS-WIDE Map
  // keyed by path) with this file's mock-fetch responses — bust it so the
  // next file's data.epoch() / data.* re-fetch against ITS own mock fetch
  // rather than reading a stale cached `{}`.
  if (typeof data.invalidate === 'function') data.invalidate();
  resetCursor();
  assert(true, 'shared singletons reset');
});

await run();
