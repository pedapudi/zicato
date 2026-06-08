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
// a harmonograf_url, OR clear both for the dead path. `ui` seeds the UI-probe
// verdict for the url so the dead-link gate is deterministic in tests (default
// true — a real browser UI is present; pass false to simulate the no-UI
// install that 404s every deep-link).
function setLive(live, { url = HG_URL, ui = true } = {}) {
  const s = coreState.state;
  s.heartbeat = url ? { harmonograf_url: url } : null;
  s.activeTournament = null;
  s.activeRuns = live
    ? [{ run_id: 'run_v1_waffles', entry_id: 'waffles_single', generation_id: 'v1', progress: 0.4 }]
    : [];
  harmonograf._resetHarmonografUiProbe();
  if (url) harmonograf._seedHarmonografUiProbe(url, ui);
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

// --- the UI-presence (dead-link) gate --------------------------------------
// The installed harmonograf-server serves NO browser UI (gRPC-Web + /healthz
// only), so a deep-link 404s. The gate hides every link until a probe confirms
// a real HTML UI is served.

test('harmonograf gate: a live server with NO browser UI HIDES every deep-link (no dead link)', () => {
  // live + a url, but the UI probe says NO browser UI (today's no-UI install).
  setLive(true, { ui: false });
  assert(harmonograf.harmonografIsLive(), 'the server is live');
  assertEqual(harmonograf.harmonografUiAvailable(), false, 'no browser UI is served');
  assertEqual(harmonograf.harmonografBase(), null, 'no base when no UI is served (link hidden)');
  assertEqual(harmonograf.harmonografRunUrl({ adk_session_id: ADK_SID }), null, 'no run url');
  assertEqual(harmonograf.harmonografLink({ adk_session_id: ADK_SID }), null, 'no run link');
  assertEqual(harmonograf.harmonografMetaUrl(), null, 'no meta url');
});

test('harmonograf gate: the link REAPPEARS once a real browser UI is served', () => {
  setLive(true, { ui: false });
  assertEqual(harmonograf.harmonografBase(), null, 'hidden while no UI');
  // a real harmonograf SPA is now served — the probe flips true.
  harmonograf._seedHarmonografUiProbe(HG_URL, true);
  assertEqual(harmonograf.harmonografUiAvailable(), true, 'a browser UI is now present');
  assertEqual(harmonograf.harmonografBase(), HG_URL, 'the base resolves once the UI is confirmed');
  const link = harmonograf.harmonografLink({ adk_session_id: ADK_SID });
  assert(link, 'the deep-link reappears when a real UI is served');
});

test('harmonograf gate: an UNPROBED base reads NOT-available (safe default: hide, then probe)', () => {
  // live + url but NO seeded verdict → the first read returns false (hide) and
  // schedules the async probe rather than rendering a maybe-dead link.
  const s = coreState.state;
  s.activeRuns = [{ run_id: 'r', entry_id: 'e', generation_id: 'v1' }];
  s.activeTournament = null;
  s.heartbeat = { harmonograf_url: HG_URL };
  harmonograf._resetHarmonografUiProbe();
  // stub fetch so the lazily-scheduled probe does not throw on an unset global.
  globalThis.fetch = async () => ({ ok: false, status: 404, headers: { get: () => '' } });
  assertEqual(harmonograf.harmonografUiAvailable(), false, 'an unprobed base hides the link (safe default)');
});

test('harmonograf probe: an HTML response confirms a UI; a /healthz JSON 200 does NOT', async () => {
  // GET "/" → text/html ⇒ a real SPA.
  globalThis.fetch = async () => ({ ok: true, headers: { get: (k) => (k.toLowerCase() === 'content-type' ? 'text/html; charset=utf-8' : '') } });
  harmonograf._resetHarmonografUiProbe();
  const s = coreState.state;
  s.activeRuns = [{ run_id: 'r', entry_id: 'e', generation_id: 'v1' }];
  s.activeTournament = null;
  s.heartbeat = { harmonograf_url: HG_URL };
  // first read schedules the probe + returns false; await a tick, then re-read.
  harmonograf.harmonografUiAvailable();
  await new Promise((r) => setTimeout(r, 0));
  assertEqual(harmonograf.harmonografUiAvailable(), true, 'an HTML response confirms a browser UI');

  // a /healthz-style JSON 200 (the today install) ⇒ NO UI.
  globalThis.fetch = async () => ({ ok: true, headers: { get: (k) => (k.toLowerCase() === 'content-type' ? 'application/json' : '') } });
  harmonograf._resetHarmonografUiProbe();
  harmonograf.harmonografUiAvailable();
  await new Promise((r) => setTimeout(r, 0));
  assertEqual(harmonograf.harmonografUiAvailable(), false, 'a JSON 200 (healthz, no SPA) does NOT confirm a UI — link stays hidden');
});

// --- standalone (persistent) server gating ---------------------------------
// A standalone dashboard has NO active runs, but a persistent per-workspace
// server (signalled by `harmonograf_persistent` on the injected heartbeat)
// must still read as live so the post-mortem deep-links render.
test('harmonograf builders: a persistent server reads live with NO active runs', () => {
  const s = coreState.state;
  s.activeTournament = null;
  s.activeRuns = [];
  s.heartbeat = { harmonograf_url: HG_URL, harmonograf_persistent: true };
  harmonograf._resetHarmonografUiProbe();
  harmonograf._seedHarmonografUiProbe(HG_URL, true);
  assert(harmonograf.harmonografIsLive(), 'a persistent server reads as live');
  const href = harmonograf.harmonografRunUrl({ adk_session_id: ADK_SID });
  assertEqual(href, `${HG_URL}/#/session/${encodeURIComponent(ADK_SID)}`,
    'the persistent server deep-links the persisted ADK session');
  const link = harmonograf.harmonografLink({ adk_session_id: ADK_SID });
  assert(link, 'a link renders against the persistent server');
});

test('harmonograf builders: NO persistent flag + no runs ⇒ not live', () => {
  const s = coreState.state;
  s.activeTournament = null;
  s.activeRuns = [];
  // A lingering url with NO persistent flag and no live run stays dead.
  s.heartbeat = { harmonograf_url: HG_URL };
  assert(!harmonograf.harmonografIsLive(), 'no persistent flag, no run ⇒ not live');
  assertEqual(harmonograf.harmonografBase(), null, 'no base for a dead, non-persistent server');
});

// --- zicato-level (meta-loop) builders -------------------------------------
// The top-bar "execution ↗" deep-link into the meta-loop session, keyed on the
// heartbeat's `harmonograf_meta_session`. Liveness-gated exactly like the
// per-run builders.

const META_SID = 'zicato-meta-loop-2026-06-06T12-00-00-00-00';

test('harmonograf meta: deep-links the meta-loop session while live', () => {
  setLive(true);
  coreState.state.heartbeat = { harmonograf_url: HG_URL, harmonograf_meta_session: META_SID };
  assertEqual(harmonograf.harmonografMetaSession(), META_SID, 'the meta session id is read off the heartbeat');
  const url = harmonograf.harmonografMetaUrl();
  assertEqual(url, `${HG_URL}/#/session/${encodeURIComponent(META_SID)}`,
    'the meta url deep-links the meta-loop session');
  const link = harmonograf.harmonografMetaLink('execution');
  assert(link, 'a meta link element is produced while live');
  assertEqual(link.getAttribute('href'), url, 'the meta link carries the session href');
  assert(link.textContent.includes('execution'), 'the label renders');
});

test('harmonograf meta: a PERSISTENT server (no active runs) resolves the meta link post-mortem', () => {
  const s = coreState.state;
  s.activeTournament = null;
  s.activeRuns = [];
  s.heartbeat = { harmonograf_url: HG_URL, harmonograf_persistent: true, harmonograf_meta_session: META_SID };
  harmonograf._resetHarmonografUiProbe();
  harmonograf._seedHarmonografUiProbe(HG_URL, true);
  const url = harmonograf.harmonografMetaUrl();
  assertEqual(url, `${HG_URL}/#/session/${encodeURIComponent(META_SID)}`,
    'the persistent server resolves the meta-loop deep-link post-mortem');
});

test('harmonograf meta: NOTHING renders when the loop is dead', () => {
  setLive(false);
  coreState.state.heartbeat = { harmonograf_url: HG_URL, harmonograf_meta_session: META_SID };
  assertEqual(harmonograf.harmonografMetaUrl(), null, 'no meta url while dead (stale url + session ignored)');
  assertEqual(harmonograf.harmonografMetaLink('execution'), null, 'no meta link while dead');
});

test('harmonograf meta: NOTHING renders without a meta session id, even while live', () => {
  setLive(true);
  coreState.state.heartbeat = { harmonograf_url: HG_URL };
  assertEqual(harmonograf.harmonografMetaSession(), null, 'no session id ⇒ null');
  assertEqual(harmonograf.harmonografMetaUrl(), null, 'no session id ⇒ no meta url');
  assertEqual(harmonograf.harmonografMetaLink('execution'), null, 'no session id ⇒ no meta link');
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

test('candidate view: the per-run link RENDERS against a persistent (post-mortem) server', async () => {
  data.invalidate();
  installFetch(FIX);
  // No active runs (post-mortem), but a persistent per-workspace server.
  const s = coreState.state;
  s.activeTournament = null;
  s.activeRuns = [];
  s.heartbeat = { harmonograf_url: HG_URL, harmonograf_persistent: true };
  harmonograf._resetHarmonografUiProbe();
  harmonograf._seedHarmonografUiProbe(HG_URL, true);
  const candidate = await import('../js/variants/T/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1', entry: 'waffles_single' });

  const links = allByClass(host, 'harmonograf-link');
  assert(links.length >= 1, 'a harmonograf link rendered against the persistent server');
  const exec = links.find((a) => (a.getAttribute('href') || '').includes('/#/session/'));
  assert(exec, 'the persisted-session execution link deep-links a /#/session/ route');
  assertEqual(exec.getAttribute('href'), `${HG_URL}/#/session/${encodeURIComponent(ADK_SID)}`,
    'the href targets the persisted run’s ADK session');
});

await run();
