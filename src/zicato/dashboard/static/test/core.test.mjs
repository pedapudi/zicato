// test/core.test.mjs — the core-module foundation tests.
//
// These pin the element builders + patch helpers (core/dom.js), the
// event bus, and the harmonograf session-id resolution:
//   * patchText writes only on a real change (the no-repaint helper the
//     long-lived chrome nodes use — the digest no-op discipline itself
//     lives in gatedSwap, pinned by the view suites).

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';

installDom();

const dom = await import('../js/core/dom.js');
const { bus } = await import('../js/core/bus.js');

// --- patchText writes only on change ---------------------------------

test('patchText writes only when the text differs', () => {
  const n = document.createElement('span');
  dom.patchText(n, 'hello');
  assertEqual(n.textContent, 'hello');
  // Mark the existing text node; a no-op patch must not replace it.
  const before = n.firstChild;
  dom.patchText(n, 'hello');
  assert(n.firstChild === before, 'unchanged patchText must not rebuild');
  dom.patchText(n, 'world');
  assertEqual(n.textContent, 'world');
});

// --- bus -------------------------------------------------------------

test('bus delivers to every subscriber and supports off()', () => {
  bus._reset();
  let a = 0;
  let b = 0;
  const offA = bus.on('topic', () => { a += 1; });
  bus.on('topic', () => { b += 1; });
  bus.emit('topic');
  assertEqual(a, 1);
  assertEqual(b, 1);
  offA();
  bus.emit('topic');
  assertEqual(a, 1, 'unsubscribed handler must not fire');
  assertEqual(b, 2);
});

const { harmonografSessionId } = await import('../js/core/harmonograf.js');

test('harmonografSessionId: prefers adk_session_id over all other fields', () => {
  const rec = {
    adk_session_id: 'real-adk-id',
    session_id: 'legacy-id',
    harmonograf_session: 'hg-id',
  };
  assertEqual(harmonografSessionId(rec), 'real-adk-id');
});

test('harmonografSessionId: accepts child_adk_session_id', () => {
  const rec = { child_adk_session_id: 'child-adk-abc' };
  assertEqual(harmonografSessionId(rec), 'child-adk-abc');
});

test('harmonografSessionId: accepts parent_adk_session_id', () => {
  const rec = { parent_adk_session_id: 'parent-adk-xyz' };
  assertEqual(harmonografSessionId(rec), 'parent-adk-xyz');
});

test('harmonografSessionId: returns null for empty record', () => {
  assertEqual(harmonografSessionId({}), null);
  assertEqual(harmonografSessionId(null), null);
});

test('harmonografSessionId: does NOT fall back to synthetic run-id', () => {
  const rec = { generation_id: 'v0', entry_id: 'waffles_single' };
  assertEqual(harmonografSessionId(rec), null);
});

// --- A1/A2: the beat path makes ONE consolidated read, never fetch-and-discard.
//
// No `loadMatchupDetail()` pulls `/api/tournaments/{gen}` (the whole
// matchup-detail payload, ab_grid included) AND `/api/drift-movements/{gen}`
// into `state.matchupDetail` / `state.driftMovements` on EVERY SSE beat — and no
// view read either field. Both the loader and the caches are gone; these pin
// that they cannot come back silently.

const api = await import('../js/core/api.js');
const { state } = await import('../js/core/state.js');

test('A1/A2: the fetch-and-discard matchup-detail loader and its caches are GONE', () => {
  assertEqual(typeof api.loadMatchupDetail, 'undefined',
    'core/api.js exports no loadMatchupDetail — the per-beat loader is deleted');
  assertEqual(typeof state.setMatchupDetail, 'undefined',
    'AppState has no setMatchupDetail mutator');
  assert(!('matchupDetail' in state), 'AppState declares no matchupDetail cache');
  assert(!('driftMovements' in state), 'AppState declares no driftMovements cache');
  assert(!('selectedMatchup' in state), 'AppState declares no selectedMatchup key (it only kept the dead cache)');
});

test('A1/A2: a debounced SSE refresh fetches /api/environment and NOTHING else', async () => {
  const sse = await import('../js/core/sse.js');
  const seen = [];
  const prevFetch = globalThis.fetch;
  globalThis.fetch = async (path) => {
    seen.push(String(path));
    return { ok: true, json: async () => ({}) };
  };
  // Drive the loader the beat path actually calls. The per-beat matchup /
  // drift-movement round-trips must not appear.
  await api.loadEnvironment();
  globalThis.fetch = prevFetch;
  assertDeep(seen, ['/api/environment'], 'exactly one consolidated read per beat');
  assert(!seen.some((p) => p.startsWith('/api/tournaments/')), 'no per-beat matchup-detail fetch');
  assert(!seen.some((p) => p.startsWith('/api/drift-movements/')), 'no per-beat drift-movements fetch');
  void sse;
});

// --- the superseded contract-diff fetcher is deleted rather than merely unused.

test('A3: data.contractDiff is deleted (superseded by the /api/workspace ledger)', async () => {
  const data = await import('../js/data.js');
  assertEqual(typeof data.contractDiff, 'undefined',
    'the dead /api/contract-diff fetcher is gone; views/home.js reads ws.ledger instead');
});

// --- the beat payload carries only what the client folds into state.

test('applyEnvironment folds no bracket, trajectory, health report or epoch contract', () => {
  for (const field of ['bracket', 'scoreTrajectory', 'healthReport', 'epochDef']) {
    assert(!(field in state), `AppState declares no ${field} field`);
  }
  state.applyEnvironment({
    epoch_id: 'e9',
    epoch: { epoch_id: 'e9', board: [] },
    tournaments: { champion_lineage: ['v0'], matchups: [] },
    score_trajectory: { points: [{ generation_id: 'v0' }] },
    health_report: { verdict: 'ok' },
  });
  assertEqual(state.epoch.id, 'e9', 'the served epoch_id names the current epoch');
  for (const field of ['bracket', 'scoreTrajectory', 'healthReport', 'epochDef']) {
    assert(!(field in state), `folding ${field}'s payload key creates no ${field} field`);
  }
  assertEqual(typeof state.setBracket, 'undefined', 'no bracket mutator');
  assertEqual(typeof state.setHealthReport, 'undefined', 'no health-report mutator');
  assertEqual(typeof state.setEpochDef, 'undefined', 'no epoch-contract mutator');
});

await run();
