// test/l0_recent_decisions.test.mjs — Recent Decisions card on L0.
//
// The clean-slate nav rework adds a Recent Decisions card to the L0
// workspace view, sourced from ``state.epochDef.experiments``,
// reverse-chronological, capped at 10. Each row links to L2 for that
// generation; the card footer offers a "view all generations →" link
// to L1.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const ws = await import('../js/views/phase0_workspace.js');

function installL0Slots() {
  for (const id of [
    'phase0-workspace-env',
    'phase0-workspace-lineage',
    'phase0-workspace-sparkline',
    'phase0-workspace-recent',
  ]) {
    let stale = document.getElementById(id);
    while (stale) {
      if (stale.parentNode) stale.parentNode.removeChild(stale);
      stale = document.getElementById(id);
    }
    const node = document.createElement('div');
    node.id = id;
    document.body.appendChild(node);
  }
  return document.getElementById('phase0-workspace-recent');
}

function resetState() {
  state.heartbeat = null;
  state.epochDef = null;
  state.workspace = { root: '/x' };
  ws.resetWorkspaceCache();
}

// --- structural contract --------------------------------------------

test('Recent Decisions card renders nothing meaningful when epochDef is null',
  () => {
    resetState();
    const slot = installL0Slots();
    ws.renderPhase0Workspace();
    const text = slot.textContent;
    // While the epochDef is null we render a loading placeholder so the
    // operator does not see "no decisions" before the snapshot lands.
    assert(text.toLowerCase().includes('loading'),
      `expected loading placeholder; got ${text.slice(0, 200)}`);
  });

test('Recent Decisions surfaces experiments reverse-chronologically', () => {
  resetState();
  state.heartbeat = { epoch_id: '2026-05-20_presn' };
  state.epochDef = {
    epoch_id: '2026-05-20_presn',
    experiments: [
      { generation_id: 'v1', verdict: 'promoted', scalar: -10.0 },
      { generation_id: 'v2', verdict: 'rejected', scalar: 5.0 },
      { generation_id: 'v3', verdict: 'promoted', scalar: -24.33 },
    ],
  };
  const slot = installL0Slots();
  ws.renderPhase0Workspace();
  // The first rendered row must be v3 (most recent).
  const rows = slot.querySelectorAll(
    '[class="phase0-recent-decisions-row"]');
  assert(rows.length >= 3, `expected >=3 rows; got ${rows.length}`);
  const firstText = rows[0].textContent;
  assert(firstText.includes('v3'),
    `first row must be the most recent (v3); got ${firstText}`);
});

test('Recent Decisions caps the rendered rows at 10', () => {
  resetState();
  state.heartbeat = { epoch_id: 'e0' };
  const xs = [];
  for (let i = 0; i < 25; i += 1) {
    xs.push({ generation_id: 'v' + i, verdict: 'rejected', scalar: 1.0 });
  }
  state.epochDef = { epoch_id: 'e0', experiments: xs };
  const slot = installL0Slots();
  ws.renderPhase0Workspace();
  const rows = slot.querySelectorAll(
    '[class="phase0-recent-decisions-row"]');
  assertEqual(rows.length, 10,
    `Recent Decisions must cap at 10 rows; got ${rows.length}`);
});

test('Recent Decisions row links to the L2 generation page', () => {
  resetState();
  state.heartbeat = { epoch_id: '2026-05-20_presn' };
  state.epochDef = {
    epoch_id: '2026-05-20_presn',
    experiments: [
      { generation_id: 'v8', verdict: 'promoted', scalar: -24.33 },
    ],
  };
  const slot = installL0Slots();
  ws.renderPhase0Workspace();
  const rows = slot.querySelectorAll(
    '[class="phase0-recent-decisions-row"]');
  assertEqual(rows.length, 1, 'one row must render');
  const href = rows[0].getAttribute('href');
  assertEqual(href, '#/gen/2026-05-20_presn/v8',
    'row href must point at the L2 generation page');
});

test('Recent Decisions card carries a "view all generations" link to L1',
  () => {
    resetState();
    state.heartbeat = { epoch_id: '2026-05-20_presn' };
    state.epochDef = {
      epoch_id: '2026-05-20_presn',
      experiments: [
        { generation_id: 'v1', verdict: 'promoted', scalar: -1.0 },
      ],
    };
    const slot = installL0Slots();
    ws.renderPhase0Workspace();
    const allgens = slot.querySelector(
      '[class="phase0-recent-decisions-allgens"]');
    assert(allgens != null,
      'a "view all generations" affordance must render');
    assertEqual(allgens.getAttribute('href'), '#/epoch/2026-05-20_presn',
      '"view all generations" must point at the L1 epoch page');
  });

test('Recent Decisions row carries verdict marks for promoted/rejected',
  () => {
    resetState();
    state.heartbeat = { epoch_id: 'e0' };
    state.epochDef = {
      epoch_id: 'e0',
      experiments: [
        { generation_id: 'v1', verdict: 'promoted', scalar: -1.0 },
        { generation_id: 'v2', verdict: 'rejected', scalar: 1.0 },
      ],
    };
    const slot = installL0Slots();
    ws.renderPhase0Workspace();
    const promoted = slot.querySelectorAll(
      '[data-variant="promoted"]');
    const rejected = slot.querySelectorAll(
      '[data-variant="rejected"]');
    assert(promoted.length >= 1, 'a promoted row must wear data-variant=promoted');
    assert(rejected.length >= 1, 'a rejected row must wear data-variant=rejected');
  });

test('recentDecisionsCount caps at 10 even when more experiments exist',
  () => {
    resetState();
    state.epochDef = {
      epoch_id: 'e0',
      experiments: Array.from({ length: 12 }).map((_, i) => ({
        generation_id: 'v' + i, verdict: 'open',
      })),
    };
    assertEqual(ws.recentDecisionsCount(), 10,
      `count must cap at 10; got ${ws.recentDecisionsCount()}`);
  });

await run();
