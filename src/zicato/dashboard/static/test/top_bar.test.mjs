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

// --- persistent live rail -------------------------------------------

function installLiveRailSlot() {
  let stale = document.getElementById('phase0-live-rail');
  while (stale) {
    if (stale.parentNode) stale.parentNode.removeChild(stale);
    stale = document.getElementById('phase0-live-rail');
  }
  const node = document.createElement('div');
  node.id = 'phase0-live-rail';
  node.classList.add('hidden');
  document.body.appendChild(node);
  return node;
}

function hasClass(node, cls) {
  return (node.getAttribute('class') || '').split(/\s+/).includes(cls);
}

test('live rail is hidden when idle (no active tournament)', () => {
  resetState();
  state.heartbeat = { last_heartbeat: new Date(Date.now() - 5_000).toISOString() };
  state.activeTournament = null;
  const rail = installLiveRailSlot();
  shell.resetShellDigest();
  shell.renderLiveRail();
  assert(!shell.liveRailActive(),
    'liveRailActive must be false with no tournament');
  assert(hasClass(rail, 'hidden'),
    'idle rail must carry the hidden class');
  assertEqual(rail.children.length, 0,
    'idle rail must paint no content');
});

test('live rail renders champion vs challenger + entry tally when a tournament is active', () => {
  resetState();
  const future = new Date(Date.now() + 1000).toISOString();
  state.heartbeat = { last_heartbeat: future, epoch_id: 'e0' };
  state.activeTournament = {
    round_index: 2, total_rounds: 4,
    parent_generation_id: 'v4', child_generation_id: 'v5',
    entries: [
      { entry_id: 'a', side: 'parent', status: 'done' },
      { entry_id: 'a', side: 'child', status: 'done' },
      { entry_id: 'b', side: 'parent', status: 'running' },
      { entry_id: 'b', side: 'child', status: 'queued' },
    ],
  };
  const rail = installLiveRailSlot();
  shell.resetShellDigest();
  shell.renderLiveRail();

  assert(shell.liveRailActive(),
    'liveRailActive must be true with a live tournament');
  assert(!hasClass(rail, 'hidden'),
    'active rail must not carry the hidden class');

  const champ = rail.querySelector('[data-side="champion"]');
  const chal = rail.querySelector('[data-side="challenger"]');
  assert(champ != null && champ.textContent.includes('v4'),
    `champion side must read v4; got ${champ && champ.textContent}`);
  assert(chal != null && chal.textContent.includes('v5'),
    `challenger side must read v5; got ${chal && chal.textContent}`);

  // Tally counts per-side entry rows (each board entry appears once per
  // side): 2 done / 1 running / 1 queued.
  const prog = rail.querySelector('[data-tally="2/1/1"]');
  assert(prog != null,
    'entry progress must report 2 done / 1 running / 1 queued');

  // Jump-to-decision CTA points at the L3 round route for the matchup.
  const cta = rail.querySelector('[data-link="jump-to-decision"]');
  assert(cta != null, 'jump-to-decision CTA must render');
  assertEqual(cta.getAttribute('href'),
    router.phase0Href('round', { epochId: 'e0', championId: 'v4', challengerId: 'v5' }),
    'CTA must deep-link into the L3 round view for the live matchup');
});

test('live rail digest gates: a same-tick re-render writes zero DOM nodes', () => {
  resetState();
  const future = new Date(Date.now() + 1000).toISOString();
  state.heartbeat = { last_heartbeat: future, epoch_id: 'e0' };
  state.activeTournament = {
    parent_generation_id: 'v4', child_generation_id: 'v5',
    entries: [{ entry_id: 'a', side: 'child', status: 'running' }],
  };
  const rail = installLiveRailSlot();
  shell.resetShellDigest();
  shell.renderLiveRail();
  const firstChild = rail.children[0];
  const count = rail.children.length;
  // Same state → same digest → no-op.
  shell.renderLiveRail();
  assertEqual(rail.children.length, count,
    'a same-digest rail tick must not rebuild');
  assert(rail.children[0] === firstChild,
    'a same-digest rail tick must not replace the left cluster');
});

test('live rail hides again when the tournament resolves to idle', () => {
  resetState();
  const future = new Date(Date.now() + 1000).toISOString();
  state.heartbeat = { last_heartbeat: future, epoch_id: 'e0' };
  state.activeTournament = {
    parent_generation_id: 'v4', child_generation_id: 'v5',
    entries: [{ entry_id: 'a', side: 'child', status: 'running' }],
  };
  const rail = installLiveRailSlot();
  shell.resetShellDigest();
  shell.renderLiveRail();
  assert(!hasClass(rail, 'hidden'), 'precondition: rail visible while live');

  // Tournament resolves — the next tick must hide + clear the rail.
  state.activeTournament = null;
  shell.renderLiveRail();
  assert(hasClass(rail, 'hidden'), 'rail must hide once the tournament resolves');
  assertEqual(rail.children.length, 0, 'hidden rail must clear its content');
});

// --- verdict glyph in the breadcrumb --------------------------------

test('breadcrumb routes the gen verdict through the shared verdict glyph', () => {
  resetState();
  state.heartbeat = { last_heartbeat: new Date(Date.now() - 5_000).toISOString() };
  state.epochDef = {
    epoch_id: 'e0',
    experiments: [
      { generation_id: 'v3', parent_generation_id: 'v1',
        outcome: { tournament_decision: 'promoted' } },
    ],
  };
  // verdictForGeneration resolves the recorded outcome.
  assertEqual(shell.verdictForGeneration('v3'), 'promoted',
    'a promoted gen must resolve to the promoted verdict');
  assertEqual(shell.verdictForGeneration('v9'), null,
    'an unknown gen must resolve to null');

  installTopBarSlot();
  shell.resetShellDigest();
  shell.renderTopBar(router.parsePhase0Hash('#/gen/e0/v3'));
  const crumb = document.body.querySelector('[data-verdict="promoted"]');
  assert(crumb != null, 'the gen crumb must carry the promoted verdict marker');
  const glyph = crumb.querySelector('[class="vglyph-mark"]');
  assert(glyph != null && glyph.textContent.includes('✓'),
    `the crumb must render the shared ✓ glyph; got ${glyph && glyph.textContent}`);
  state.epochDef = null;
});

await run();
