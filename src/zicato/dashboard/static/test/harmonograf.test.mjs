// test/harmonograf.test.mjs — the harmonograf deep-link builders + their
// wiring into the variant-T candidate view.
//
// Two concerns:
//   1. The liveness-gated builders in core/harmonograf.js: a link renders
//      ONLY while a run is live AND a harmonograf_url is in scope, deep-linking
//      `/#/session/<adk_session_id>`; nothing renders otherwise.
//   2. The candidate view (variants/T/views/candidate.js) actually RENDERS the
//      per-run execution link in the entry drill-down (the dead-code bug fix):
//      it appears for a live run with an adk_session_id, and NOT when the loop
//      is dead / there is no session.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const harmonograf = await import('../js/core/harmonograf.js');
const coreState = await import('../js/core/state.js');
const router = await import('../js/variants/T/router.js');
const data = await import('../js/variants/T/data.js');

const EPOCH_ID = 'crisper-presentations';
const HG_URL = 'http://127.0.0.1:42017';
const ADK_SID = 'adk-sess-abc123';

// --- state helpers ---------------------------------------------------------

// Make the loop LIVE (an active run is in flight) and stamp the heartbeat with
// a harmonograf_url, OR clear both for the dead path.
function setLive(live, { url = HG_URL } = {}) {
  const s = coreState.state;
  s.heartbeat = url ? { harmonograf_url: url } : null;
  s.activeTournament = null;
  s.activeRuns = live
    ? [{ run_id: 'run_v1_waffles', entry_id: 'waffles_single', generation_id: 'v1', progress: 0.4 }]
    : [];
}

function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter(
    (n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls),
  );
}

// --- builder-level gating --------------------------------------------------

test('harmonograf builders: a live run with an adk_session_id deep-links /#/session/<id>', () => {
  setLive(true);
  assert(harmonograf.harmonografIsLive(), 'a run in flight reads as live');
  const href = harmonograf.harmonografRunUrl({ adk_session_id: ADK_SID });
  assertEqual(href, `${HG_URL}/#/session/${encodeURIComponent(ADK_SID)}`,
    'the run url deep-links the ADK session');
  const link = harmonograf.harmonografLink({ adk_session_id: ADK_SID }, 'Open this run in harmonograf');
  assert(link, 'a link element is produced while live');
  assertEqual(link.getAttribute('href'), href, 'the link carries the session href');
  assert(link.textContent.includes('Open this run in harmonograf'), 'the label renders');
});

test('harmonograf builders: NOTHING renders when the loop is not live', () => {
  setLive(false);
  assert(!harmonograf.harmonografIsLive(), 'no run in flight reads as not-live');
  // Even with a lingering heartbeat url, a dead loop yields no base / no link.
  coreState.state.heartbeat = { harmonograf_url: HG_URL };
  assertEqual(harmonograf.harmonografBase(), null, 'no base while dead (stale url ignored)');
  assertEqual(harmonograf.harmonografRunUrl({ adk_session_id: ADK_SID }), null, 'no run url while dead');
  assertEqual(harmonograf.harmonografLink({ adk_session_id: ADK_SID }), null, 'no link while dead');
  assertEqual(harmonograf.harmonografMini({ adk_session_id: ADK_SID }), null, 'no mini link while dead');
});

test('harmonograf builders: no harmonograf_url ⇒ nothing, even while live', () => {
  setLive(true, { url: '' });
  assert(harmonograf.harmonografIsLive(), 'still live');
  assertEqual(harmonograf.harmonografBase(), null, 'no url ⇒ no base');
  assertEqual(harmonograf.harmonografLink({ adk_session_id: ADK_SID }), null, 'no url ⇒ no link');
});

// --- candidate-view wiring (the dead-code fix) -----------------------------

const FIX = {
  '/api/epoch': {
    epoch_id: EPOCH_ID, closed: false, goal: 'crisper',
    experiments: [
      { generation_id: 'v0', parent_generation_id: '', outcome: { decision: 'baseline' } },
      { generation_id: 'v1', parent_generation_id: 'v0', outcome: { decision: 'rejected' } },
    ],
    board: [{ id: 'waffles_single', kind: 'single_turn', budget_s: 180, weight: 1 }],
  },
  '/api/tournaments': { epoch_id: EPOCH_ID, champion_lineage: ['v0'], matchups: [
    { champion: 'v0', challenger: 'v1', decision: 'rejected', delta_scalar: 5.0 },
  ] },
  '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 70 }, { generation_id: 'v1', scalar: 72 }] },
};
FIX[`/api/generation/${EPOCH_ID}/v0/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v0', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v0_waffles', drift_loss: 60.0, pass_fail: 0, runtime_ms: 180000 },
] };
FIX[`/api/generation/${EPOCH_ID}/v1/per-entry`] = { epoch_id: EPOCH_ID, generation_id: 'v1', entries: [
  { entry_id: 'waffles_single', run_id: 'run_v1_waffles', drift_loss: 62.0, pass_fail: 0, runtime_ms: 180000 },
] };
FIX[`/api/round/${EPOCH_ID}/v0/v1/gate`] = { decision: 'rejected', delta_scalar: 5.0, rules: [] };
FIX[`/api/run/${EPOCH_ID}/v1/waffles_single/expectations`] = { outcomes: [] };
FIX[`/api/run/${EPOCH_ID}/v1/waffles_single/per-judge`] = { judges: [] };
// The run HEADER carries the adk_session_id the harmonograf link keys on.
FIX[`/api/run/${EPOCH_ID}/v1/waffles_single/header`] = {
  epoch_id: EPOCH_ID, generation_id: 'v1', entry_id: 'waffles_single',
  run_id: 'run_v1_waffles', drift_loss: 62.0, pass_fail: 0, runtime_ms: 180000,
  adk_session_id: ADK_SID,
};

function installFetch(fixtures) {
  globalThis.fetch = async (path) => {
    let v = fixtures[path];
    if (v === undefined) {
      const q = path.indexOf('?');
      if (q >= 0) v = fixtures[path.slice(0, q)];
    }
    if (v !== undefined) return { ok: true, json: async () => v };
    return { ok: false, status: 404, json: async () => ({ error: 'not found: ' + path }) };
  };
}

test('candidate view: the per-run harmonograf execution link RENDERS for a live run with a session', async () => {
  data.invalidate();
  installFetch(FIX);
  setLive(true);
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  // drill into the board entry so the entry drill-down (the link's home) renders.
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1', entry: 'waffles_single' });

  const links = allByClass(host, 'harmonograf-link');
  assert(links.length >= 1, 'a harmonograf link rendered on the live candidate drill-down');
  const exec = links.find((a) => (a.getAttribute('href') || '').includes('/#/session/'));
  assert(exec, 'the execution link deep-links a /#/session/ route');
  assertEqual(exec.getAttribute('href'), `${HG_URL}/#/session/${encodeURIComponent(ADK_SID)}`,
    'the href targets the run’s ADK session');
  assertEqual(exec.getAttribute('target'), '_blank', 'opens in a new tab');
});

test('candidate view: NO harmonograf link when the loop is not live', async () => {
  data.invalidate();
  installFetch(FIX);
  setLive(false);
  // a stale url lingers on the heartbeat — the gate must still suppress the link.
  coreState.state.heartbeat = { harmonograf_url: HG_URL };
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1', entry: 'waffles_single' });

  const links = allByClass(host, 'harmonograf-link');
  assertEqual(links.length, 0, 'no harmonograf link renders when the loop is dead');
});

await run();
