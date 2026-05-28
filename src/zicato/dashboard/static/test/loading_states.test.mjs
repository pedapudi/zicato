// test/loading_states.test.mjs — loading-vs-empty fallback distinction.
//
// Earlier the phase-0 views fell through to "No X yet." copy for both
// `state.X == null` (SSE has not landed) and `state.X.items == []`
// (loaded, genuinely empty). A screenshot taken before the first
// snapshot then showed "No generations yet" on a workspace with eight
// generations — misleading.
//
// These tests pin the new contract on every L0..L4 fallback:
//   * the backing state is `null`/`undefined`     → loading copy
//   * the backing state is loaded + empty         → empty copy

import { installDom, test, run, assert } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const shell = await import('../js/views/phase0_shell.js');
const ws = await import('../js/views/phase0_workspace.js');
const epoch = await import('../js/views/phase0_epoch.js');
const generation = await import('../js/views/phase0_generation.js');
const round = await import('../js/views/phase0_round.js');
const runV = await import('../js/views/phase0_run.js');

function installNode(id, tag = 'div') {
  // The harness's getElementById walks DFS and returns the first match;
  // tests share document.body, so we must strip any stale node with the
  // same id before installing a fresh one. Otherwise a later render
  // writes into the previous test's container and this test reads ''.
  let stale = document.getElementById(id);
  while (stale) {
    if (stale.parentNode) stale.parentNode.removeChild(stale);
    stale = document.getElementById(id);
  }
  const node = document.createElement(tag);
  node.id = id;
  document.body.appendChild(node);
  return node;
}

function resetState() {
  state.heartbeat = null;
  state.epochDef = null;
  state.workspace = null;
  state.bracket = null;
  state.activeRuns = [];
  state.activeTournament = null;
  state.logTail = { events: [] };
  state.logCursor = null;
  state.logEventsPath = null;
  ws.resetWorkspaceCache();
  epoch.resetContractDiffCache();
  epoch.resetPerJudgeTrendCache();
  epoch.resetPerEntryTrendCache();
  generation.resetGenerationCaches();
  round.resetRoundCaches();
  runV.resetRunCaches();
  shell.resetSidebarDigest();
}

// -- L0 sidebar live-activity card -----------------------------------

test('L0 sidebar shows Loading when heartbeat is null (SSE not yet settled)', () => {
  resetState();
  const body = installNode('phase0-live-body');
  shell.renderSidebarLive();
  const text = body.textContent;
  assert(text.includes('Loading'),
    `sidebar must show Loading when state.heartbeat == null; got: ${text}`);
  assert(!text.includes('No active run'),
    `sidebar must NOT say "No active run" while loading; got: ${text}`);
});

test('L0 sidebar shows "No active run" when heartbeat is loaded but empty', () => {
  resetState();
  // Heartbeat object is present but carries no live-run fields.
  state.heartbeat = { last_heartbeat: '2026-05-27T00:00:00Z' };
  const body = installNode('phase0-live-body');
  shell.renderSidebarLive();
  const text = body.textContent;
  assert(text.includes('No active run'),
    `sidebar must say "No active run" once heartbeat loaded; got: ${text}`);
  assert(!text.includes('Loading'),
    `sidebar must NOT say "Loading" after heartbeat lands; got: ${text}`);
});

// -- L0 workspace lineage --------------------------------------------

test('L0 workspace lineage shows Loading when /api/workspace not yet landed', () => {
  resetState();
  // Workspace identity is null too; the lineage card body still must
  // render the loading placeholder (not "No epochs recorded.").
  state.workspace = null;
  installNode('phase0-workspace-env');
  const lineage = installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  ws.renderPhase0Workspace();
  const text = lineage.textContent;
  assert(text.includes('Loading'),
    `lineage must show Loading when cache is null; got: ${text.slice(0, 200)}`);
  assert(!text.includes('No epochs recorded'),
    `lineage must NOT say "No epochs recorded" while loading; got: ${text.slice(0, 200)}`);
});

// -- L1 epoch spine + experiments + journal --------------------------

test('L1 epoch spine shows Loading when state.epochDef is null', () => {
  resetState();
  installNode('phase0-epoch-goal');
  installNode('phase0-epoch-contract-diff');
  const spine = installNode('phase0-epoch-spine');
  installNode('phase0-epoch-heatmap-entries');
  installNode('phase0-epoch-heatmap-judges');
  installNode('phase0-epoch-experiments');
  installNode('phase0-epoch-journal');
  epoch.renderPhase0Epoch({ epochId: 'e0' });
  const text = spine.textContent;
  assert(text.includes('Loading'),
    `spine must show Loading when state.epochDef == null; got: ${text}`);
  assert(!text.includes('No generations yet'),
    `spine must NOT say "No generations yet" while loading; got: ${text}`);
});

test('L1 epoch spine shows "No generations yet" when epochDef loaded with empty experiments', () => {
  resetState();
  installNode('phase0-epoch-goal');
  installNode('phase0-epoch-contract-diff');
  const spine = installNode('phase0-epoch-spine');
  installNode('phase0-epoch-heatmap-entries');
  installNode('phase0-epoch-heatmap-judges');
  installNode('phase0-epoch-experiments');
  installNode('phase0-epoch-journal');
  state.epochDef = { epoch_id: 'e0', goal: 'g', experiments: [] };
  epoch.renderPhase0Epoch({ epochId: 'e0' });
  const text = spine.textContent;
  assert(text.includes('No generations yet'),
    `spine must say "No generations yet" when experiments == []; got: ${text}`);
  assert(!text.includes('Loading'),
    `spine must NOT say "Loading" after epochDef lands; got: ${text}`);
});

test('L1 epoch experiments shows Loading when state.epochDef is null', () => {
  resetState();
  installNode('phase0-epoch-goal');
  installNode('phase0-epoch-contract-diff');
  installNode('phase0-epoch-spine');
  installNode('phase0-epoch-heatmap-entries');
  installNode('phase0-epoch-heatmap-judges');
  const experiments = installNode('phase0-epoch-experiments');
  installNode('phase0-epoch-journal');
  epoch.renderPhase0Epoch({ epochId: 'e0' });
  const text = experiments.textContent;
  assert(text.includes('Loading'),
    `experiments slot must show Loading when state.epochDef == null; got: ${text}`);
  assert(!text.includes('No experiments recorded'),
    `experiments must NOT say "No experiments recorded" while loading; got: ${text}`);
});

test('L1 epoch journal shows Loading when state.epochDef is null', () => {
  resetState();
  installNode('phase0-epoch-goal');
  installNode('phase0-epoch-contract-diff');
  installNode('phase0-epoch-spine');
  installNode('phase0-epoch-heatmap-entries');
  installNode('phase0-epoch-heatmap-judges');
  installNode('phase0-epoch-experiments');
  const journal = installNode('phase0-epoch-journal');
  epoch.renderPhase0Epoch({ epochId: 'e0' });
  const text = journal.textContent;
  assert(text.includes('Loading'),
    `journal slot must show Loading when state.epochDef == null; got: ${text}`);
  assert(!text.includes('No journal preview'),
    `journal must NOT say "No journal preview" while loading; got: ${text}`);
});

// -- L2 generation hypothesis card -----------------------------------

test('L2 generation hypothesis shows Loading when state.epochDef is null', () => {
  resetState();
  const hyp = installNode('phase0-gen-hypothesis');
  installNode('phase0-gen-patches');
  installNode('phase0-gen-entries');
  installNode('phase0-gen-judges');
  installNode('phase0-gen-compare');
  generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v1' });
  const text = hyp.textContent;
  assert(text.includes('Loading'),
    `hypothesis must show Loading when state.epochDef == null; got: ${text}`);
  assert(!text.includes('No hypothesis recorded'),
    `hypothesis must NOT say "No hypothesis recorded" while loading; got: ${text}`);
});

test('L2 generation hypothesis shows "No hypothesis recorded" when epochDef loaded but generation absent', () => {
  resetState();
  const hyp = installNode('phase0-gen-hypothesis');
  installNode('phase0-gen-patches');
  installNode('phase0-gen-entries');
  installNode('phase0-gen-judges');
  installNode('phase0-gen-compare');
  state.epochDef = { epoch_id: 'e0', experiments: [] };
  generation.renderPhase0Generation({ epochId: 'e0', generationId: 'v1' });
  const text = hyp.textContent;
  assert(text.includes('No hypothesis recorded'),
    `hypothesis must say "No hypothesis recorded" when epochDef loaded; got: ${text}`);
  assert(!text.includes('Loading'),
    `hypothesis must NOT say "Loading" after epochDef lands; got: ${text}`);
});

// -- L3 round decision callout ---------------------------------------

test('L3 round decision shows Loading when state.bracket is null', () => {
  resetState();
  installNode('phase0-round-vs');
  installNode('phase0-round-entries');
  installNode('phase0-round-judges');
  const decision = installNode('phase0-round-decision');
  round.renderPhase0Round({ epochId: 'e0', championId: 'v1', challengerId: 'v2' });
  const text = decision.textContent;
  assert(text.includes('Loading'),
    `decision must show Loading when state.bracket == null; got: ${text}`);
  assert(!text.includes('No decision yet'),
    `decision must NOT say "No decision yet" while loading; got: ${text}`);
});

test('L3 round decision shows "No decision yet" when bracket loaded but matchup absent', () => {
  resetState();
  installNode('phase0-round-vs');
  installNode('phase0-round-entries');
  installNode('phase0-round-judges');
  const decision = installNode('phase0-round-decision');
  state.bracket = { matchups: [] };
  round.renderPhase0Round({ epochId: 'e0', championId: 'v1', challengerId: 'v2' });
  const text = decision.textContent;
  assert(text.includes('No decision yet'),
    `decision must say "No decision yet" when bracket loaded; got: ${text}`);
  assert(!text.includes('Loading'),
    `decision must NOT say "Loading" after bracket lands; got: ${text}`);
});

// -- L4 run header ---------------------------------------------------

test('L4 run header shows Loading until /header lands', () => {
  resetState();
  const header = installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  installNode('phase0-run-events');
  // Stub fetch so the header endpoint never resolves synchronously
  // (we want to assert the initial "Loading" branch).
  const origFetch = globalThis.fetch;
  globalThis.fetch = () => new Promise(() => {}); // forever-pending
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v1', entryId: 'entry_alpha',
    });
    const text = header.textContent;
    assert(text.includes('Loading'),
      `run header must show Loading until /header lands; got: ${text}`);
    assert(!text.includes('No completed-run metrics'),
      `run header must NOT say "No completed-run metrics" while loading; got: ${text}`);
  } finally {
    globalThis.fetch = origFetch;
  }
});

test('L4 run events shows Loading when log tail has not been fetched yet', () => {
  resetState();
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  const events = installNode('phase0-run-events');
  const origFetch = globalThis.fetch;
  globalThis.fetch = () => new Promise(() => {});
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v1', entryId: 'entry_alpha',
    });
    const text = events.textContent;
    assert(text.includes('Loading'),
      `events must show Loading before log-tail lands; got: ${text}`);
    assert(!text.includes('No events yet'),
      `events must NOT say "No events yet" while loading; got: ${text}`);
  } finally {
    globalThis.fetch = origFetch;
  }
});

test('L4 run events shows "No events yet" once log tail loaded but empty', () => {
  resetState();
  installNode('phase0-run-header');
  installNode('phase0-run-expectation');
  installNode('phase0-run-judges');
  installNode('phase0-run-transcript');
  const events = installNode('phase0-run-events');
  // Simulate /api/log-tail returning an empty payload — logEventsPath
  // is now set, so the events stream is "loaded + empty", not "loading".
  state.logTail = { events: [] };
  state.logEventsPath = '/tmp/run.jsonl';
  const origFetch = globalThis.fetch;
  globalThis.fetch = () => new Promise(() => {});
  try {
    runV.renderPhase0Run({
      epochId: 'e0', generationId: 'v1', entryId: 'entry_alpha',
    });
    const text = events.textContent;
    assert(text.includes('No events yet'),
      `events must say "No events yet" once log-tail loaded; got: ${text}`);
    assert(!text.includes('Loading'),
      `events must NOT say "Loading" after log-tail lands; got: ${text}`);
  } finally {
    globalThis.fetch = origFetch;
  }
});

await run();
