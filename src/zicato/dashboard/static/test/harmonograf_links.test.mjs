// test/harmonograf_links.test.mjs — restored harmonograf deep-links.
//
// The legacy 5-tab UI rendered "Open in harmonograf" links on every
// run-like card; the phase0 redesign briefly dropped them. These tests
// pin the link rendering on the four restored surfaces:
//
//   * L0 workspace Live Activity card
//   * L1 epoch Generation spine card (header actions)
//   * L4 run header card
//   * L3 round per-side blocks (champion + challenger)
//
// Each surface must:
//   1. render a link when ``state.heartbeat.harmonograf_url`` is set
//      and the contextual record carries an ``adk_session_id``;
//   2. render no link at all when ``harmonograf_url`` is missing /
//      empty (no disabled stub);
//   3. emit a correct ``/#/session/<adk_session_id>`` href;
//   4. add ``target="_blank"`` + ``rel="noopener"`` so the link opens
//      safely in a new tab.
//
// LIVENESS GATE (DASHBOARD-V2 fix). harmonograf's server dies with the
// run, so a deep-link is only valid while a run is LIVE. ``harmonografBase``
// now gates on liveness (an active tournament OR active runs) — not on
// the lingering ``harmonograf_url`` alone. So every surface that should
// render a link must have a live signal set; with no live run, no link
// renders (the dead-port bug is gone).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/views/phase0_router.js');
void router;

const { state } = await import('../js/core/state.js');
const {
  harmonografIsLive, harmonografBase, harmonografLiveNote, harmonografRunUrl,
} = await import('../js/core/harmonograf.js');
const ws = await import('../js/views/phase0_workspace.js');
const epoch = await import('../js/views/phase0_epoch.js');
const round = await import('../js/views/phase0_round.js');
const runV = await import('../js/views/phase0_run.js');

function installNode(id, tag = 'div') {
  const node = document.createElement(tag);
  node.id = id;
  document.body.appendChild(node);
  return node;
}

// Same mockFetch shape as phase1_5.test.mjs — never reaches the
// network; resolves immediately with a synthetic body.
function mockFetch(handler) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const body = handler(url);
    return {
      ok: true,
      status: 200,
      headers: new Map(),
      json: async () => body,
      text: async () => JSON.stringify(body),
    };
  };
  return () => { globalThis.fetch = original; };
}

// Find the first <a class="harmonograf-link"> descendant of ``node``.
function findHarmonografLink(node) {
  const stack = [node];
  while (stack.length > 0) {
    const cur = stack.pop();
    if (!cur || !cur.childNodes) continue;
    for (const child of cur.childNodes) {
      if (!child) continue;
      const cls = child._attrs && child._attrs.class;
      if (child.tagName === 'A' && cls && cls.split(/\s+/).includes('harmonograf-link')) {
        return child;
      }
      stack.push(child);
    }
  }
  return null;
}

// ---------------------------------------------------------------------
// The liveness gate (unit) — the dead-port fix at the source.
// ---------------------------------------------------------------------

test('harmonograf liveness gate: base is null unless a run is live', () => {
  state.activeTournament = null;
  state.activeRuns = [];
  state.heartbeat = { harmonograf_url: 'https://hg.example.com' };
  assert(!harmonografIsLive(), 'no tournament + no runs is not live');
  assertEqual(harmonografBase(), null,
    'a lingering url with no live run resolves to NO base (dead port)');
  assertEqual(harmonografRunUrl({ adk_session_id: 'x' }), null,
    'no run url when not live');

  // An active tournament makes it live.
  state.activeTournament = { champion: 'v0', challenger: 'v1' };
  assert(harmonografIsLive(), 'an active tournament is live');
  assertEqual(harmonografBase(), 'https://hg.example.com',
    'a live run resolves the (trimmed) base');

  // Active runs alone also count.
  state.activeTournament = null;
  state.activeRuns = [{ entry_id: 'e1' }];
  assert(harmonografIsLive(), 'active runs are live');

  state.activeTournament = null;
  state.activeRuns = [];
  state.heartbeat = null;
});

test('harmonografLiveNote: a muted note post-run, nothing while live', () => {
  state.activeTournament = null;
  state.activeRuns = [];
  const note = harmonografLiveNote();
  assert(note != null, 'a muted note renders when not live');
  assert(note.className.includes('harmonograf-note'));
  assert(note.textContent.toLowerCase().includes('live'),
    'the note explains it is available during live runs');

  state.activeTournament = { champion: 'v0', challenger: 'v1' };
  assertEqual(harmonografLiveNote(), null,
    'no note while live — the real link should render instead');
  state.activeTournament = null;
});

// ---------------------------------------------------------------------
// L4 run header
// ---------------------------------------------------------------------

test('L4 run header renders harmonograf link with correct href + target + rel', async () => {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
  runV.resetRunCaches();
  // A run is live (active tournament) — harmonograf's server is up, so
  // the deep-link is valid.
  state.activeRuns = [];
  state.activeTournament = { champion: 'v2', challenger: 'v3' };
  state.logTail = { events: [] };
  state.heartbeat = {
    harmonograf_url: 'https://harmonograf.example.com',
    epoch_id: 'e0', generation_id: 'v3',
  };
  const headerPayload = {
    epoch_id: 'e0', generation_id: 'v3', entry_id: 'predicate_fail',
    drift_loss: 0.5, pass_fail: false,
    runtime_ms: 1000, tokens_spent: 100, output_chars: 50,
    turns_completed: null, plan_revisions: 0,
    wall_clock_budget_exceeded: false,
    run_id: 'run_alpha',
    adk_session_id: 'adk-session-aaa',
  };
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/header')) return headerPayload;
    if (url.includes('/expectations')) return { outcomes: [] };
    return { run_id: null, judges: [] };
  });
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail',
    });
    const node = document.getElementById('phase0-run-header');
    const link = findHarmonografLink(node);
    assert(link != null,
      'L4 run header must render a harmonograf link when harmonograf_url + adk_session_id are set');
    assertEqual(
      link.getAttribute('href'),
      'https://harmonograf.example.com/#/session/adk-session-aaa',
      'harmonograf href must encode the adk_session_id under /#/session/',
    );
    assertEqual(link.getAttribute('target'), '_blank',
      'harmonograf link must open in a new tab');
    assertEqual(link.getAttribute('rel'), 'noopener',
      'harmonograf link must carry rel=noopener');
  } finally {
    restoreFetch();
    state.heartbeat = null;
    state.activeTournament = null;
  }
});

test('L4 run header renders NO harmonograf link when harmonograf_url is missing', async () => {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
  runV.resetRunCaches();
  state.activeRuns = [];
  state.logTail = { events: [] };
  // Heartbeat without harmonograf_url — the helper must return null
  // and the renderer must not paint anything for harmonograf.
  state.heartbeat = { epoch_id: 'e0', generation_id: 'v3' };
  const headerPayload = {
    epoch_id: 'e0', generation_id: 'v3', entry_id: 'predicate_fail',
    drift_loss: 0.5, pass_fail: false,
    runtime_ms: 1000, tokens_spent: 100, output_chars: 50,
    turns_completed: null, plan_revisions: 0,
    wall_clock_budget_exceeded: false,
    run_id: 'run_alpha',
    adk_session_id: 'adk-session-aaa',
  };
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/header')) return headerPayload;
    if (url.includes('/expectations')) return { outcomes: [] };
    return { run_id: null, judges: [] };
  });
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail',
    });
    const node = document.getElementById('phase0-run-header');
    const link = findHarmonografLink(node);
    assertEqual(link, null,
      'L4 run header must render NO harmonograf link when harmonograf_url is unset');
    const text = node.textContent;
    // The completed-run header still rendered — only the harmonograf
    // bit is gone. Sanity-check the verdict tile is present.
    assert(text.includes('FAIL'),
      'completed-run verdict tile must still render');
  } finally {
    restoreFetch();
    state.heartbeat = null;
  }
});

test('L4 run header harmonograf link falls back to base url when adk_session_id is missing', async () => {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
  runV.resetRunCaches();
  state.activeRuns = [];
  state.activeTournament = { champion: 'v2', challenger: 'v3' };
  state.logTail = { events: [] };
  state.heartbeat = {
    harmonograf_url: 'https://harmonograf.example.com/',
    epoch_id: 'e0', generation_id: 'v3',
  };
  // Completed run without an adk_session_id (older runs, or the field
  // never landed in loss.json). The link must still render — pointing
  // at the bare base url with the trailing slash trimmed.
  const headerPayload = {
    epoch_id: 'e0', generation_id: 'v3', entry_id: 'predicate_fail',
    drift_loss: 0.5, pass_fail: true,
    runtime_ms: 1000, tokens_spent: 100, output_chars: 50,
    turns_completed: null, plan_revisions: 0,
    wall_clock_budget_exceeded: false,
    run_id: 'run_alpha',
    adk_session_id: null,
  };
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/header')) return headerPayload;
    if (url.includes('/expectations')) return { outcomes: [] };
    return { run_id: null, judges: [] };
  });
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail',
    });
    const node = document.getElementById('phase0-run-header');
    const link = findHarmonografLink(node);
    assert(link != null,
      'harmonograf link must still render with only the base url when no session id is known');
    assertEqual(
      link.getAttribute('href'),
      'https://harmonograf.example.com',
      'fallback href must be the base url with trailing slash trimmed');
    assertEqual(link.getAttribute('target'), '_blank');
    assertEqual(link.getAttribute('rel'), 'noopener');
  } finally {
    restoreFetch();
    state.heartbeat = null;
    state.activeTournament = null;
  }
});

// The dead-port fix: a COMPLETED run (no live tournament, no active runs)
// must render NO harmonograf link even though the heartbeat still carries
// the (now-dead) harmonograf_url. This is the bug the liveness gate fixes.
test('L4 run header renders NO harmonograf link once the run is over (dead-port gate)', async () => {
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
  runV.resetRunCaches();
  // Run is OVER: nothing live, but the heartbeat url lingers.
  state.activeRuns = [];
  state.activeTournament = null;
  state.logTail = { events: [] };
  state.heartbeat = {
    harmonograf_url: 'https://harmonograf.example.com',
    epoch_id: 'e0', generation_id: 'v3',
  };
  const headerPayload = {
    epoch_id: 'e0', generation_id: 'v3', entry_id: 'predicate_fail',
    drift_loss: 0.5, pass_fail: false,
    runtime_ms: 1000, tokens_spent: 100, output_chars: 50,
    turns_completed: null, plan_revisions: 0,
    wall_clock_budget_exceeded: false,
    run_id: 'run_alpha', adk_session_id: 'adk-session-aaa',
  };
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/header')) return headerPayload;
    if (url.includes('/expectations')) return { outcomes: [] };
    return { run_id: null, judges: [] };
  });
  try {
    runV.renderPhase0Run({ epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail' });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    runV.renderPhase0Run({ epochId: 'e0', generationId: 'v3', entryId: 'predicate_fail' });
    const node = document.getElementById('phase0-run-header');
    const link = findHarmonografLink(node);
    assertEqual(link, null,
      'a closed run must render NO harmonograf link — the server is dead even though the url lingers');
    assert(node.textContent.includes('FAIL'),
      'the completed-run verdict tile must still render');
  } finally {
    restoreFetch();
    state.heartbeat = null;
  }
});

// ---------------------------------------------------------------------
// L0 workspace
//
// Post-#206 the in-content Live Activity card on L0 was retired — the
// redesigned sidebar (#198) already owns live-run state across every
// level, so duplicating it under the env column was redundant. L0 no
// longer surfaces a harmonograf link directly; the L4 run header and
// the L1 spine actions still do (asserted below). The sidebar's
// "View current run" jump CTA carries the user one click into L4
// where the harmonograf link is rendered.
// ---------------------------------------------------------------------

test('L0 workspace renders NO harmonograf link (in-content live card was retired)', () => {
  installNode('phase0-workspace-env');
  installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  state.workspace = { root: '/tmp/.zicato' };
  // Even with a fully-configured live run + harmonograf url, the L0
  // env / glance / lineage / trend slots must NOT carry a harmonograf
  // link — the in-content live activity card is gone.
  state.heartbeat = {
    harmonograf_url: 'https://harmonograf.example.com',
    epoch_id: 'e0', generation_id: 'v3',
  };
  state.activeRuns = [
    { entry_id: 'live_entry', adk_session_id: 'adk-live-xyz',
      progress: 0.2, elapsed_seconds: 5, status: 'running' },
  ];
  ws.resetWorkspaceCache();
  ws.renderPhase0Workspace();
  for (const id of [
    'phase0-workspace-env',
    'phase0-workspace-lineage',
    'phase0-workspace-sparkline',
  ]) {
    const node = document.getElementById(id);
    const link = findHarmonografLink(node);
    assertEqual(link, null,
      `L0 slot #${id} must NOT carry a harmonograf link after the in-content live card was retired`);
  }
  state.heartbeat = null;
  state.activeRuns = [];
});

// ---------------------------------------------------------------------
// L1 epoch spine card actions
// ---------------------------------------------------------------------

test('L1 epoch spine card surfaces harmonograf link as actions when run is live on this epoch', () => {
  installNode('phase0-epoch-goal');
  installNode('phase0-epoch-contract-diff');
  installNode('phase0-epoch-spine');
  installNode('phase0-epoch-heatmap-entries');
  installNode('phase0-epoch-heatmap-judges');
  installNode('phase0-epoch-experiments');
  installNode('phase0-epoch-analysis');
  epoch.resetContractDiffCache();
  epoch.resetPerJudgeTrendCache();
  epoch.resetAnalysisCache();
  state.epochDef = {
    epoch_id: 'e0',
    goal: 'Test goal',
    experiments: [{ generation_id: 'v0', outcome: null }],
  };
  state.heartbeat = {
    harmonograf_url: 'https://harmonograf.example.com',
    epoch_id: 'e0', generation_id: 'v3',
  };
  state.activeRuns = [
    { entry_id: 'live_entry', adk_session_id: 'adk-live-xyz',
      progress: 0.2, elapsed_seconds: 5, status: 'running' },
  ];
  epoch.renderPhase0Epoch({ epochId: 'e0' });
  const node = document.getElementById('phase0-epoch-spine');
  const link = findHarmonografLink(node);
  assert(link != null,
    'L1 spine card must surface a harmonograf link when a run is live on the focused epoch');
  assertEqual(
    link.getAttribute('href'),
    'https://harmonograf.example.com/#/session/adk-live-xyz');
  assertEqual(link.getAttribute('target'), '_blank');
  assertEqual(link.getAttribute('rel'), 'noopener');
  state.heartbeat = null;
  state.activeRuns = [];
  state.epochDef = null;
});

test('L1 epoch spine card renders NO harmonograf link when no run is live on this epoch', () => {
  installNode('phase0-epoch-goal');
  installNode('phase0-epoch-contract-diff');
  installNode('phase0-epoch-spine');
  installNode('phase0-epoch-heatmap-entries');
  installNode('phase0-epoch-heatmap-judges');
  installNode('phase0-epoch-experiments');
  installNode('phase0-epoch-analysis');
  epoch.resetContractDiffCache();
  epoch.resetPerJudgeTrendCache();
  epoch.resetAnalysisCache();
  state.epochDef = {
    epoch_id: 'e0',
    goal: 'Test goal',
    experiments: [{ generation_id: 'v0', outcome: null }],
  };
  // harmonograf_url is set but there is no live heartbeat on this
  // epoch (no generation_id) — the link must not paint.
  state.heartbeat = {
    harmonograf_url: 'https://harmonograf.example.com',
    epoch_id: 'e0',
  };
  state.activeRuns = [];
  epoch.renderPhase0Epoch({ epochId: 'e0' });
  const node = document.getElementById('phase0-epoch-spine');
  const link = findHarmonografLink(node);
  assertEqual(link, null,
    'L1 spine card must not render a harmonograf link when no live generation is on this epoch');
  state.heartbeat = null;
  state.epochDef = null;
});

// ---------------------------------------------------------------------
// L3 round per-side blocks
// ---------------------------------------------------------------------

test('L3 round per-side blocks render gen-scoped harmonograf links on champion + challenger', async () => {
  installNode('phase0-round-vs');
  installNode('phase0-round-entries');
  installNode('phase0-round-judges');
  installNode('phase0-round-decision');
  round.resetRoundCaches();
  // This round is the live one — harmonograf's server is up.
  state.activeTournament = { champion: 'v3', challenger: 'v4' };
  state.heartbeat = {
    harmonograf_url: 'https://harmonograf.example.com',
  };
  state.epochDef = { epoch_id: 'e0' };
  state.bracket = {
    epoch_id: 'e0',
    matchups: [{
      champion: 'v3', challenger: 'v4',
      decision: 'promoted', delta_scalar: -0.1,
    }],
  };
  // Per-entry data so the matchup card has something to render. The
  // per-side renderer is what carries the harmonograf gen link — we
  // do not need adk_session_id on the per-entry rows for that.
  const champData = { entries: [{ entry_id: 'e1', drift_loss: 0.2, pass_fail: true }] };
  const chalData = { entries: [{ entry_id: 'e1', drift_loss: 0.1, pass_fail: true }] };
  const restoreFetch = mockFetch((url) => {
    if (url.includes('/v3/per-entry')) return champData;
    if (url.includes('/v4/per-entry')) return chalData;
    return { judges: [], primary_driver: null };
  });
  try {
    round.renderPhase0Round({
      epochId: 'e0', championId: 'v3', challengerId: 'v4',
    });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    round.renderPhase0Round({
      epochId: 'e0', championId: 'v3', challengerId: 'v4',
    });
    const node = document.getElementById('phase0-round-vs');
    // Collect every harmonograf link in the matchup card.
    const links = [];
    const stack = [node];
    while (stack.length > 0) {
      const cur = stack.pop();
      if (!cur || !cur.childNodes) continue;
      for (const child of cur.childNodes) {
        if (!child) continue;
        const cls = child._attrs && child._attrs.class;
        if (child.tagName === 'A' && cls && cls.split(/\s+/).includes('harmonograf-link')) {
          links.push(child);
        }
        stack.push(child);
      }
    }
    assert(links.length === 2,
      `L3 must render exactly two harmonograf gen-links (champion + challenger); got ${links.length}`);
    for (const link of links) {
      // Gen links use the bare base url (harmonograf has no per-gen
      // filter route); the helper trims the trailing slash.
      assertEqual(link.getAttribute('href'), 'https://harmonograf.example.com');
      assertEqual(link.getAttribute('target'), '_blank');
      assertEqual(link.getAttribute('rel'), 'noopener');
    }
    // The aria-label scopes the link to the right generation id so a
    // reader can tell champion / challenger apart.
    const ariaLabels = links.map((l) => l.getAttribute('aria-label'));
    assert(ariaLabels.some((a) => a && a.includes('v3')),
      `champion link aria-label must mention v3; got: ${ariaLabels.join(' | ')}`);
    assert(ariaLabels.some((a) => a && a.includes('v4')),
      `challenger link aria-label must mention v4; got: ${ariaLabels.join(' | ')}`);
  } finally {
    restoreFetch();
    state.heartbeat = null;
    state.epochDef = null;
    state.bracket = null;
    state.activeTournament = null;
  }
});

await run();
