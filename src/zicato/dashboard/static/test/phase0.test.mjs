// test/phase0.test.mjs — level-aligned shell tests.
//
// The shell:
//   * a hash router (parsePhase0Hash / phase0Href) for L0..L4 +
//     sidebar tools (Files, Search);
//   * a sidebar Live Activity card that subscribes to heartbeat
//     changes via a digest gate (mirrors the top-bar header pattern);
//   * a breadcrumb as the primary navigation;
//   * five view containers (phase0-view-<level>) the shell toggles.
//
// These tests pin those contracts so a downstream change does not
// silently regress them.

import { installDom, test, run, assert, assertEqual, assertDeep } from './harness.mjs';

installDom();

// Lazy-import the modules under test only after the DOM is installed
// — they touch globalThis.document during their own import-time setup.
const router = await import('../js/views/phase0_router.js');
const shell = await import('../js/views/phase0_shell.js');
const { state } = await import('../js/core/state.js');

// --- router ---------------------------------------------------------

test('parsePhase0Hash defaults to workspace on empty hash', () => {
  const r = router.parsePhase0Hash('');
  assertEqual(r.level, 'workspace');
  assertDeep(r.params, {});
});

test('parsePhase0Hash parses #/workspace', () => {
  const r = router.parsePhase0Hash('#/workspace');
  assertEqual(r.level, 'workspace');
});

test('parsePhase0Hash parses #/epoch/<id>', () => {
  const r = router.parsePhase0Hash('#/epoch/2026-05-16_e0');
  assertEqual(r.level, 'epoch');
  assertEqual(r.params.epochId, '2026-05-16_e0');
});

test('parsePhase0Hash parses #/gen/<epoch>/<gen> (with "gen" alias)', () => {
  const r = router.parsePhase0Hash('#/gen/e0/v3');
  assertEqual(r.level, 'generation');
  assertEqual(r.params.epochId, 'e0');
  assertEqual(r.params.generationId, 'v3');
});

test('parsePhase0Hash parses #/round/<epoch>/<champ>-><chal>', () => {
  const r = router.parsePhase0Hash('#/round/e0/v3->v4');
  assertEqual(r.level, 'round');
  assertEqual(r.params.epochId, 'e0');
  assertEqual(r.params.championId, 'v3');
  assertEqual(r.params.challengerId, 'v4');
});

test('parsePhase0Hash parses #/run/<epoch>/<gen>/<entry>', () => {
  const r = router.parsePhase0Hash('#/run/e0/v3/entry_alpha');
  assertEqual(r.level, 'run');
  assertEqual(r.params.epochId, 'e0');
  assertEqual(r.params.generationId, 'v3');
  assertEqual(r.params.entryId, 'entry_alpha');
});

test('parsePhase0Hash falls back to workspace on unknown segment', () => {
  const r = router.parsePhase0Hash('#/no-such-thing');
  assertEqual(r.level, 'workspace');
});

test('phase0Href round-trips per-level params', () => {
  assertEqual(router.phase0Href('workspace'), '#/workspace');
  assertEqual(
    router.phase0Href('epoch', { epochId: 'e0' }),
    '#/epoch/e0',
  );
  assertEqual(
    router.phase0Href('generation', { epochId: 'e0', generationId: 'v3' }),
    '#/gen/e0/v3',
  );
  assertEqual(
    router.phase0Href('round', { epochId: 'e0', championId: 'v3', challengerId: 'v4' }),
    '#/round/e0/v3->v4',
  );
  assertEqual(
    router.phase0Href('run', { epochId: 'e0', generationId: 'v3', entryId: 'a' }),
    '#/run/e0/v3/a',
  );
});

// --- breadcrumb -----------------------------------------------------

test('breadcrumbSegments yields workspace alone for the L0 route', () => {
  const r = router.parsePhase0Hash('#/workspace');
  const segs = shell.breadcrumbSegments(r);
  assertEqual(segs.length, 1);
  assertEqual(segs[0].label, 'workspace');
  assert(typeof segs[0].href === 'string');
});

test('breadcrumbSegments includes the epoch crumb for L1', () => {
  const r = router.parsePhase0Hash('#/epoch/e0');
  const segs = shell.breadcrumbSegments(r);
  // workspace › e0
  assertEqual(segs.length, 2);
  assertEqual(segs[1].label, 'e0');
});

test('breadcrumbSegments builds workspace › epoch › gen for L2', () => {
  const r = router.parsePhase0Hash('#/gen/e0/v3');
  const segs = shell.breadcrumbSegments(r);
  assertEqual(segs.length, 3);
  assertEqual(segs[2].label, 'gen v3');
});

test('breadcrumbSegments adds the run crumb when entry id is present', () => {
  const r = router.parsePhase0Hash('#/run/e0/v3/entry_alpha');
  const segs = shell.breadcrumbSegments(r);
  // workspace › e0 › gen v3 › run entry_alpha
  assertEqual(segs.length, 4);
  assertEqual(segs[3].label, 'run entry_alpha');
});

// --- sidebar Live Activity digest ------------------------------------

test('liveActivityDigest only depends on heartbeat structural fields', () => {
  // The first heartbeat lands at this point; the digest baseline is
  // taken *after* it lands so the test pins the steady-state contract:
  // re-stamping the heartbeat's churn fields (last_heartbeat, etc.)
  // must NOT flip the digest. The null→loaded transition is a separate
  // edge that the sidebar relies on to leave its "Loading…" placeholder.
  state.heartbeat = { last_heartbeat: '2026-05-27T00:00:00Z' };
  state.activeRuns = [];
  state.activeTournament = null;
  shell.resetSidebarDigest();
  const before = shell.liveActivityDigest();

  // Re-stamping a timestamp field on the heartbeat does NOT change the
  // digest — the contract says only the structural fields drive a
  // repaint.
  state.heartbeat = { last_heartbeat: '2026-05-27T00:00:05Z' };
  const after = shell.liveActivityDigest();
  assertEqual(before, after,
    'a heartbeat-timestamp-only tick must not flip the digest');
});

test('liveActivityDigest flips on the first heartbeat (null → loaded)', () => {
  // The null→loaded edge is precisely what tells the sidebar to swap
  // its "Loading…" placeholder for the real card. If the digest stayed
  // identical across this transition the loading text would persist
  // forever even after the first heartbeat lands.
  state.heartbeat = null;
  state.activeRuns = [];
  state.activeTournament = null;
  shell.resetSidebarDigest();
  const empty = shell.liveActivityDigest();
  state.heartbeat = { last_heartbeat: '2026-05-27T00:00:00Z' };
  const loaded = shell.liveActivityDigest();
  assert(empty !== loaded,
    'first heartbeat (null → loaded) MUST flip the digest');
});

test('liveActivityDigest changes when generation_id changes', () => {
  state.heartbeat = { epoch_id: 'e0', generation_id: 'v3' };
  const a = shell.liveActivityDigest();
  state.heartbeat = { epoch_id: 'e0', generation_id: 'v4' };
  const b = shell.liveActivityDigest();
  assert(a !== b, 'a structural gen flip MUST flip the digest');
});

// --- sidebar Live Activity rendering -------------------------------
//
// The clean-slate navigation rework dropped the sidebar entirely;
// renderSidebarLive is now a no-op shim kept for back-compat with the
// app.js render fan-out. The state-aware status pill in the top bar
// owns this surface now — see top_bar.test.mjs for the new contract.

// --- view container toggle -----------------------------------------

test('showPhase0View toggles the matching view container visible', () => {
  // Install the five view containers.
  const levels = ['workspace', 'epoch', 'generation', 'round', 'run'];
  for (const l of levels) {
    const node = document.createElement('section');
    node.id = 'phase0-view-' + l;
    document.body.appendChild(node);
  }
  shell.showPhase0View('round');
  for (const l of levels) {
    const node = document.getElementById('phase0-view-' + l);
    if (l === 'round') {
      assert(!node.classList.contains('hidden'),
        `round container must be visible`);
    } else {
      assert(node.classList.contains('hidden'),
        `${l} container must be hidden`);
    }
  }
});

await run();
