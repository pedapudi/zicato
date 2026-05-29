// test/top_bar.test.mjs — clean-slate top-bar contracts.
//
// The redesign moves every global affordance into the top bar:
//   * branding ``zicato`` (left)
//   * a breadcrumb (left, after branding)
//   * a ⌘K palette button (right cluster)
//   * a state-aware status pill (right cluster)
//   * a Files icon link (right cluster)
//   * a Harmonograf ↗ link (right cluster) — only when the heartbeat
//     surfaces a harmonograf_url.
//
// These tests pin the rendered structure + the digest gating + the
// state-aware label flips of the status pill.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const { state } = await import('../js/core/state.js');
const shell = await import('../js/views/phase0_shell.js');
const router = await import('../js/views/phase0_router.js');
const pillMod = await import('../js/components/status_pill.js');

function installTopBarSlot() {
  // Strip + reinstall ``#phase0-topbar`` so each test runs against a
  // fresh container without inheriting nodes from the previous case.
  let stale = document.getElementById('phase0-topbar');
  while (stale) {
    if (stale.parentNode) stale.parentNode.removeChild(stale);
    stale = document.getElementById('phase0-topbar');
  }
  const node = document.createElement('div');
  node.id = 'phase0-topbar';
  document.body.appendChild(node);
  return node;
}

function resetState() {
  state.heartbeat = null;
  state.activeRuns = [];
  state.activeTournament = null;
  state.connecting = false;
  state.connected = true;
}

// --- structural contract --------------------------------------------

test('renderTopBar paints branding, breadcrumb, palette button, status pill and files icon',
  () => {
    resetState();
    state.heartbeat = { last_heartbeat: new Date(Date.now() - 5_000).toISOString() };
    const slot = installTopBarSlot();
    shell.resetSidebarDigest();
    shell.renderTopBar(router.parsePhase0Hash('#/workspace'));

    // Branding anchor — must point at workspace.
    const brand = slot.querySelector('[class="phase0-topbar-brand"]');
    assert(brand != null, 'branding anchor must render');
    assertEqual(brand.getAttribute('href'), '#/workspace',
      'branding href must point at workspace');

    // Breadcrumb must render.
    const crumb = slot.querySelector('[class="phase0-breadcrumb"]');
    assert(crumb != null, 'breadcrumb container must render');

    // ⌘K button.
    const palette = slot.querySelector(
      '[class="phase0-topbar-palette-btn"]');
    assert(palette != null, '⌘K palette button must render');

    // Status pill (resolved state is "idle" since no runs).
    const pill = slot.querySelector('[data-state="idle"]');
    assert(pill != null, 'a status pill must render in idle state');

    // Files icon link — every link has aria-label, so the Files one is
    // findable via the aria-label attribute.
    const files = slot.querySelectorAll('[aria-label="Files"]');
    assertEqual(files.length, 1, 'one Files icon link must render');
    assertEqual(files[0].getAttribute('href'), '#/files',
      'Files link must point at the Files route');
  });

test('renderTopBar surfaces the harmonograf link only when URL is set',
  () => {
    resetState();
    state.heartbeat = { last_heartbeat: new Date(Date.now() - 5_000).toISOString() };
    const slot = installTopBarSlot();
    shell.resetSidebarDigest();
    shell.renderTopBar(router.parsePhase0Hash('#/workspace'));
    let externals = slot.querySelectorAll(
      '[data-link="harmonograf"]');
    assertEqual(externals.length, 0,
      'no harmonograf link must render when URL is unset');

    // Spoof the URL — the next render must add the link.
    state.heartbeat = {
      last_heartbeat: new Date(Date.now() - 5_000).toISOString(),
      harmonograf_url: 'http://localhost:9999',
    };
    shell.resetSidebarDigest();
    shell.renderTopBar(router.parsePhase0Hash('#/workspace'));
    externals = slot.querySelectorAll(
      '[data-link="harmonograf"]');
    assertEqual(externals.length, 1,
      'harmonograf link must render when URL is set');
  });

test('renderTopBar digest gates: same heartbeat tick writes zero DOM nodes',
  () => {
    resetState();
    state.heartbeat = { last_heartbeat: new Date(Date.now() - 5_000).toISOString() };
    const slot = installTopBarSlot();
    shell.resetSidebarDigest();
    shell.renderTopBar(router.parsePhase0Hash('#/workspace'));
    const childCountAfterFirst = slot.children.length;
    // The first node identity captures the inner content.
    const left = slot.children[0];
    // Same call again — same digest, must be a no-op.
    shell.renderTopBar(router.parsePhase0Hash('#/workspace'));
    assertEqual(slot.children.length, childCountAfterFirst,
      'a same-digest tick must not rebuild the top bar');
    assert(slot.children[0] === left,
      'a same-digest tick must not replace the left cluster node');
  });

test('breadcrumb segments render as clickable links per route', () => {
  resetState();
  state.heartbeat = { last_heartbeat: new Date(Date.now() - 5_000).toISOString() };
  installTopBarSlot();
  shell.resetSidebarDigest();
  shell.renderTopBar(router.parsePhase0Hash('#/gen/e0/v3'));
  const crumbs = document.body.querySelectorAll('[class="phase0-crumb"]');
  // workspace + e0 + gen v3 → at least 3 anchored crumbs.
  assert(crumbs.length >= 3,
    `expected at least 3 breadcrumb anchors; got ${crumbs.length}`);
  // The first crumb must link to workspace.
  assertEqual(crumbs[0].getAttribute('href'), '#/workspace',
    'first breadcrumb is workspace');
});

// --- status pill state transitions ----------------------------------

test('status pill state: CONNECTING when state.connecting is true', () => {
  resetState();
  state.connecting = true;
  state.heartbeat = null;
  assertEqual(pillMod.resolveStatusState(), 'connecting',
    'connecting state must resolve while SSE is hydrating');
  assertEqual(pillMod.statusPillLabel('connecting'), 'CONNECTING',
    'connecting label must read CONNECTING');
});

test('status pill state: IDLE when supervisor is alive but no run', () => {
  resetState();
  state.heartbeat = { last_heartbeat: new Date(Date.now() - 5_000).toISOString() };
  state.activeRuns = [];
  state.activeTournament = null;
  assertEqual(pillMod.resolveStatusState(), 'idle',
    'idle state must resolve when no runs are in flight');
});

test('status pill state: RUNNING when activeRuns is non-empty', () => {
  resetState();
  // Use a fresh ISO timestamp so the stale check does not trigger; the
  // harness has no clock control, so we use a timestamp far in the
  // future relative to the test run epoch.
  const future = new Date(Date.now() + 1000).toISOString();
  state.heartbeat = {
    last_heartbeat: future,
    epoch_id: 'e0', generation_id: 'v8',
  };
  state.activeRuns = [{ entry_id: 'entry_alpha' }];
  assertEqual(pillMod.resolveStatusState(), 'running',
    'running state must resolve when activeRuns is non-empty');
  assertEqual(pillMod.statusPillLabel('running'), 'RUNNING v8',
    'running label must carry the generation id');
});

test('status pill state: STALE when last heartbeat exceeds threshold', () => {
  resetState();
  // 2000s old — comfortably past the 90s threshold.
  const ancient = new Date(Date.now() - 2000 * 1000).toISOString();
  state.heartbeat = { last_heartbeat: ancient, epoch_id: 'e0' };
  state.activeRuns = [];
  assertEqual(pillMod.resolveStatusState(), 'stale',
    'stale state must resolve when heartbeat is older than threshold');
});

test('renderStatusPill emits a button with the resolved data-state', () => {
  resetState();
  state.heartbeat = { last_heartbeat: new Date(Date.now() - 5_000).toISOString() };
  const pill = pillMod.renderStatusPill();
  assertEqual(pill.localName, 'button',
    'the pill must render as a button so click + keyboard semantics work');
  assertEqual(pill.getAttribute('data-state'), 'idle',
    'data-state must reflect the resolved status');
  const label = pill.querySelector('[class="phase0-status-pill-label"]');
  assert(label != null && label.textContent.includes('IDLE'),
    `label span must read IDLE; got ${label && label.textContent}`);
});

await run();
