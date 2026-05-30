// test/l0_dedup_live_activity.test.mjs — L0 redesign dedup contract.
//
// Two waves of dedup converge here:
//
//   * (#206) The redesigned sidebar made Live Activity a persistent
//     fixture of the left rail, so L0 main NEVER renders an in-content
//     "Live activity" card or its "jump to current epoch" CTA.
//
//   * (L0 redesign) The epoch-lineage TIMELINE and the cross-epoch
//     best-scalar SPARKLINE were two pictures of one thing. They are
//     replaced by a single lineage ribbon (epoch zoom), whose y-axis
//     encodes the scalar — so it subsumes the sparkline. The standalone
//     trend/sparkline card is GONE, and a loop-health banner now leads
//     the view.
//
// This file pins:
//   1. L0 main never renders an in-content "Live activity" card / CTA.
//   2. The freed space surfaces a "Workspace at a glance" tile strip
//      (lifetime totals: epochs / generations / promoted; open/closed
//      breakdown; current-epoch deep-link).
//   3. The lineage RIBBON renders into the lineage slot (not the old
//      .epoch-timeline list).
//   4. The loop-health banner renders at the top of the view.
//   5. The standalone cross-epoch trend/sparkline card is removed (no
//      <svg> sparkline in the sparkline slot anymore).
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

// Route by URL so the workspace payload and the health report can be
// mocked independently (the view fetches /api/workspace AND
// /api/health-report). A bare payload (no `health` key) serves the same
// body to any URL, preserving the old single-payload call sites.
function mockFetchOnce(payload, health) {
  const original = globalThis.fetch;
  const respond = (body) => ({
    ok: true, status: 200, headers: new Map(),
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
  globalThis.fetch = async (url) => {
    const u = String(url || '');
    if (health !== undefined && u.includes('/api/health-report')) {
      return respond(health);
    }
    return respond(payload);
  };
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

test('L0 lineage slot renders the unified lineage ribbon (epoch zoom)', () => {
  resetState();
  installNode('phase0-workspace-env');
  installNode('phase0-workspace-lineage');
  installNode('phase0-workspace-sparkline');
  state.workspace = { root: '/tmp/.zicato' };
  const payload = {
    current_epoch_id: 'e_beta',
    epochs: [
      {
        epoch_id: 'e_alpha', goal: 'Iterate.',
        best_scalar: 0.42, generation_count: 3, promoted_count: 1,
        closed: true, parent_epoch_id: null,
      },
      {
        epoch_id: 'e_beta', goal: 'Tighten.',
        best_scalar: 0.30, generation_count: 2, promoted_count: 1,
        closed: false, parent_epoch_id: 'e_alpha',
      },
    ],
  };
  const restore = mockFetchOnce(payload);
  try {
    ws.renderPhase0Workspace();
    return new Promise((resolve) => {
      setTimeout(() => {
        ws.renderPhase0Workspace();
        const slot = document.getElementById('phase0-workspace-lineage');
        const lineageText = slot.textContent;
        assert(lineageText.includes('Epoch lineage'),
          `lineage card title must render; got: ${lineageText.slice(0, 240)}`);
        // The ribbon (not the old .epoch-timeline list) is the body.
        const ribbon = slot.querySelector('[class="ribbon ribbon-zoom-epochs"]');
        assert(ribbon != null,
          `the lineage slot must render a lineageRibbon at epoch zoom; got: ${lineageText.slice(0, 240)}`);
        // Every epoch surfaces as a node carrying its id.
        assert(lineageText.includes('e_alpha') && lineageText.includes('e_beta'),
          `ribbon must surface both epoch ids; got: ${lineageText.slice(0, 240)}`);
        // The live (current) epoch is tagged LIVE by the ribbon.
        assert(lineageText.includes('LIVE'),
          `the current epoch must read as LIVE on the ribbon; got: ${lineageText.slice(0, 240)}`);
        // The old timeline list must be gone.
        const staleTimeline = slot.querySelector('[class="epoch-timeline"]');
        assert(staleTimeline == null,
          'the legacy .epoch-timeline list must NOT render (ribbon replaces it)');
        restore();
        resolve();
      }, 20);
    });
  } catch (err) {
    restore();
    throw err;
  }
});

// -- The standalone sparkline / trend card is gone ------------------

test('L0 no longer renders a separate cross-epoch sparkline/trend card', () => {
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
  };
  const restore = mockFetchOnce(payload);
  try {
    ws.renderPhase0Workspace();
    return new Promise((resolve) => {
      setTimeout(() => {
        ws.renderPhase0Workspace();
        // The sparkline slot must be empty — the ribbon's y-axis is the
        // trajectory now, so a standalone trend card would be redundant.
        const slot = document.getElementById('phase0-workspace-sparkline');
        let svgCount = 0;
        const stack = [slot];
        while (stack.length > 0) {
          const cur = stack.pop();
          if (!cur || !cur.childNodes) continue;
          for (const child of cur.childNodes) {
            if (!child) continue;
            if (child.tagName === 'SVG') svgCount += 1;
            stack.push(child);
          }
        }
        assertEqual(svgCount, 0,
          `the sparkline slot must hold no <svg> (trend card removed); got ${svgCount}`);
        assert(!slot.textContent.includes('Cross-epoch trend'),
          `the "Cross-epoch trend" card title must NOT render; got: ${slot.textContent.slice(0, 160)}`);
        restore();
        resolve();
      }, 20);
    });
  } catch (err) {
    restore();
    throw err;
  }
});

// -- The loop-health banner leads the view --------------------------

test('L0 renders the loop-health banner at the top of the env slot', () => {
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
        generation_count: 1, promoted_count: 1, closed: false,
        parent_epoch_id: null,
      },
    ],
  };
  // A warning-severity finding so the banner paints a non-trivial state.
  const health = {
    epoch_id: 'e_alpha',
    healthy: false,
    findings: [
      {
        code: 'flat_loss',
        severity: 'warning',
        summary: 'loss surface is flat across the last 3 generations',
        detail: { window: 3 },
      },
    ],
  };
  const restore = mockFetchOnce(payload, health);
  try {
    ws.renderPhase0Workspace();
    return new Promise((resolve) => {
      setTimeout(() => {
        ws.renderPhase0Workspace();
        const env = document.getElementById('phase0-workspace-env');
        const banner = env.querySelector('[role="status"]');
        assert(banner != null,
          'a loop-health banner (role=status) must render in the env slot');
        assertEqual(banner.getAttribute('data-tone'), 'warn',
          'a warning finding must paint the banner amber (data-tone=warn)');
        assert(env.textContent.includes('loss surface is flat'),
          `the banner must surface the top finding summary; got: ${env.textContent.slice(0, 200)}`);
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
