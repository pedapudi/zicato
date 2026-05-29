// test/l0_dedup_live_activity.test.mjs — L0 in-content Live Activity
// card was retired (#206).
//
// Background: post-#198 the redesigned sidebar made Live Activity a
// persistent fixture of the left rail. The L0 workspace view used to
// also render an in-content "Live activity" card in the env-strip
// right column, duplicating the same heartbeat / runs / "jump to
// current epoch" affordance. This file pins the dedup contract:
//
//   1. No matter how live the state is, L0 main NEVER renders an
//      in-content "Live activity" card or its "jump to current epoch"
//      CTA (those belong to the sidebar now).
//   2. The freed space surfaces a "Workspace at a glance" tile strip
//      that shows lifetime workspace totals (epochs / generations /
//      promoted), with current-epoch and open/closed breakdown.
//   3. The Epoch lineage card still renders end-to-end.
//
// The sidebar is exercised in sidebar_redesign.test.mjs and the
// loading-states fallbacks in loading_states.test.mjs — this file is
// the L0-main companion.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/views/phase0_router.js');
void router;

const { state } = await import('../js/core/state.js');
const ws = await import('../js/views/phase0_workspace.js');

function installNode(id, tag = 'div') {
  // Tests share document.body; strip any stale node with the same id
  // before installing a fresh one so a later render does not write
  // into a previous test's container.
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
  state.workspace = null;
  state.activeRuns = [];
  ws.resetWorkspaceCache();
}

function mockFetchOnce(payload) {
  const original = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true, status: 200, headers: new Map(),
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
  return () => { globalThis.fetch = original; };
}

// -- L0 no longer renders the in-content Live Activity card ---------

test('L0 main does NOT render an in-content Live activity card even with a live heartbeat', () => {
  resetState();
  installNode('phase0-workspace-env');
  installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  state.workspace = { root: '/tmp/.zicato', adk_entrypoint: 'mod:agent' };
  // Fully-populated heartbeat with active run — the sidebar's Live
  // Activity card will surface this; L0 main must not duplicate it.
  state.heartbeat = {
    epoch_id: 'e_alpha', generation_id: 'v3',
    last_heartbeat: '2026-05-27T00:00:00Z',
  };
  state.activeRuns = [
    { entry_id: 'live_entry', adk_session_id: 'adk-xyz',
      progress: 0.4, elapsed_seconds: 10, status: 'running' },
  ];
  ws.renderPhase0Workspace();
  const envText = document.getElementById('phase0-workspace-env').textContent;
  assert(!envText.includes('Live activity'),
    `L0 env slot must not render a "Live activity" card title (sidebar owns it); got: ${envText.slice(0, 240)}`);
  assert(!envText.includes('jump to current epoch'),
    `L0 env slot must not render the "jump to current epoch" CTA (sidebar owns the jump); got: ${envText.slice(0, 240)}`);
  // The lineage and sparkline slots should also be free of any
  // live-activity card chrome.
  const lineageText = document.getElementById('phase0-workspace-lineage').textContent;
  assert(!lineageText.includes('Live activity'),
    `L0 lineage slot must not render a "Live activity" card title; got: ${lineageText.slice(0, 240)}`);
  const sparkText = document.getElementById('phase0-workspace-sparkline').textContent;
  assert(!sparkText.includes('Live activity'),
    `L0 sparkline slot must not render a "Live activity" card title; got: ${sparkText.slice(0, 240)}`);
});

// -- L0 reclaims the freed space with a workspace-totals tile -------

test('L0 main renders a "Workspace at a glance" tile strip with workspace totals', () => {
  resetState();
  installNode('phase0-workspace-env');
  installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  state.workspace = { root: '/tmp/.zicato', adk_entrypoint: 'mod:agent' };
  // Seed /api/workspace via mocked fetch — 3 epochs, varied counts so
  // the totals are meaningful.
  const payload = {
    current_epoch_id: 'e_beta',
    epochs: [
      {
        epoch_id: 'e_alpha', goal: 'Iterate.',
        best_scalar: 0.42, generation_count: 4, promoted_count: 2,
        closed: true, parent_epoch_id: null,
      },
      {
        epoch_id: 'e_beta', goal: 'Tighten.',
        best_scalar: 0.31, generation_count: 5, promoted_count: 3,
        closed: false, parent_epoch_id: 'e_alpha',
      },
      {
        epoch_id: 'e_gamma', goal: null,
        best_scalar: 0.30, generation_count: 1, promoted_count: 0,
        closed: false, parent_epoch_id: 'e_beta',
      },
    ],
    sparkline: [
      { epoch_id: 'e_alpha', scalar: 0.42 },
      { epoch_id: 'e_beta', scalar: 0.31 },
      { epoch_id: 'e_gamma', scalar: 0.30 },
    ],
  };
  const restore = mockFetchOnce(payload);
  try {
    ws.renderPhase0Workspace();
    return new Promise((resolve) => {
      setTimeout(() => {
        ws.renderPhase0Workspace();
        const envText = document.getElementById('phase0-workspace-env').textContent;
        assert(envText.includes('Workspace at a glance'),
          `glance card title must render; got: ${envText.slice(0, 240)}`);
        // Lifetime totals: 3 epochs · 4+5+1=10 gens · 2+3+0=5 promoted.
        // The renderMetricTile chrome puts each value next to its
        // label; we assert the numeric text is present.
        assert(envText.includes('10'),
          `total generations (10) must surface in glance tiles; got: ${envText.slice(0, 240)}`);
        assert(envText.includes('5'),
          `total promoted (5) must surface in glance tiles; got: ${envText.slice(0, 240)}`);
        // open / closed breakdown: 2 open + 1 closed.
        assert(envText.includes('open'),
          `glance meta must mention "open"; got: ${envText.slice(0, 240)}`);
        assert(envText.includes('closed'),
          `glance meta must mention "closed"; got: ${envText.slice(0, 240)}`);
        // Current-epoch deep-link.
        assert(envText.includes('e_beta'),
          `glance meta must surface current epoch id; got: ${envText.slice(0, 240)}`);
        restore();
        resolve();
      }, 20);
    });
  } catch (err) {
    restore();
    throw err;
  }
});

test('L0 main still renders the Epoch lineage card (untouched by the dedup)', () => {
  resetState();
  installNode('phase0-workspace-env');
  installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  state.workspace = { root: '/tmp/.zicato' };
  const payload = {
    current_epoch_id: 'e_alpha',
    epochs: [
      {
        epoch_id: 'e_alpha', goal: 'Iterate.',
        best_scalar: 0.42, generation_count: 3, promoted_count: 1,
        closed: false, parent_epoch_id: null,
      },
      {
        epoch_id: 'e_beta', goal: 'Tighten.',
        best_scalar: 0.30, generation_count: 2, promoted_count: 1,
        closed: false, parent_epoch_id: 'e_alpha',
      },
    ],
    sparkline: [
      { epoch_id: 'e_alpha', scalar: 0.42 },
      { epoch_id: 'e_beta', scalar: 0.30 },
    ],
  };
  const restore = mockFetchOnce(payload);
  try {
    ws.renderPhase0Workspace();
    return new Promise((resolve) => {
      setTimeout(() => {
        ws.renderPhase0Workspace();
        const lineageText = document.getElementById('phase0-workspace-lineage').textContent;
        assert(lineageText.includes('Epoch lineage'),
          `lineage card title must render; got: ${lineageText.slice(0, 240)}`);
        assert(lineageText.includes('e_alpha'),
          `lineage must render first epoch row; got: ${lineageText.slice(0, 240)}`);
        assert(lineageText.includes('e_beta'),
          `lineage must render second epoch row; got: ${lineageText.slice(0, 240)}`);
        // Parent → child arrow on the older epoch.
        assert(lineageText.includes('→ e_beta'),
          `parent_epoch_id arrow e_alpha → e_beta must render; got: ${lineageText.slice(0, 240)}`);
        restore();
        resolve();
      }, 20);
    });
  } catch (err) {
    restore();
    throw err;
  }
});

// -- The trend card grew into the reclaimed space -------------------

test('L0 trend card sparkline canvas is wider (≥360 px) after #206 reclaim', () => {
  resetState();
  installNode('phase0-workspace-env');
  installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  state.workspace = { root: '/tmp/.zicato' };
  const payload = {
    current_epoch_id: 'e_alpha',
    epochs: [
      {
        epoch_id: 'e_alpha', goal: 'g', best_scalar: 0.40,
        generation_count: 1, promoted_count: 0, closed: false,
        parent_epoch_id: null,
      },
      {
        epoch_id: 'e_beta', goal: 'g', best_scalar: 0.35,
        generation_count: 1, promoted_count: 0, closed: false,
        parent_epoch_id: 'e_alpha',
      },
    ],
    sparkline: [
      { epoch_id: 'e_alpha', scalar: 0.40 },
      { epoch_id: 'e_beta', scalar: 0.35 },
    ],
  };
  const restore = mockFetchOnce(payload);
  try {
    ws.renderPhase0Workspace();
    return new Promise((resolve) => {
      setTimeout(() => {
        ws.renderPhase0Workspace();
        // Walk the sparkline slot looking for an <svg width="360"...>.
        const slot = document.getElementById('phase0-workspace-sparkline');
        const stack = [slot];
        let widest = 0;
        while (stack.length > 0) {
          const cur = stack.pop();
          if (!cur || !cur.childNodes) continue;
          for (const child of cur.childNodes) {
            if (!child) continue;
            if (child.tagName === 'SVG') {
              const w = parseInt(child.getAttribute('width') || '0', 10);
              if (w > widest) widest = w;
            }
            stack.push(child);
          }
        }
        assert(widest >= 360,
          `trend sparkline must be ≥360px wide after #206 (got ${widest})`);
        restore();
        resolve();
      }, 20);
    });
  } catch (err) {
    restore();
    throw err;
  }
});

await run();
