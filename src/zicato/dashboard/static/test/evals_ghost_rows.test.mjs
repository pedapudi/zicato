// test/evals_ghost_rows.test.mjs — the Evals GHOST ROWS (TRAJECTORY-UI.md §2.2b).
//
// Suggested board entries (the "board being created") render as pending-styled
// ghost rows appended below the real rows: the entry id + a "suggested" marker,
// the admission visuals rendered IN the row (flip whisker where the flip badge
// sits; discrimination pips + evidence tier where cells would be), an apply
// affordance, and NOTHING mistakable for measured tournament data (§4 honesty).
// Pins: the render + honesty classes, the apply href, the digest no-op, the
// BYTE-IDENTICAL no-ghost case (the pre-feature matrix is untouched), and XSS.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/router.js');
const data = await import('../js/data.js');
const evals = await import('../js/views/evals.js');
const harmonograf = await import('../js/core/harmonograf.js');
const coreState = await import('../js/core/state.js');

const EPOCH = 'e3';
const EVALS_PATH = `/api/epoch/${EPOCH}/evals`;
const CTX = { navigate() {}, href: router.href };

function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
function hasClass(host, cls) { return allByClass(host, cls).length > 0; }

function installMatrixOnly() {
  globalThis.fetch = async (path) => {
    if (path === EVALS_PATH) return { ok: true, json: async () => matrixFixture() };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };
}
function fresh() {
  data.invalidate();
  evals._resetGhostFeedForTest();
  globalThis.window.location = { hash: '', search: '' };
  const s = coreState.state;
  s.heartbeat = null; s.activeTournament = null; s.activeRuns = [];
  harmonograf._resetHarmonografUiProbe();
}

function matrixFixture() {
  const cP = (evidence, replicates) => ({ drift_loss: 0.31, pass_ratio: 1.0, pass_fail: true, score: 0.9, replicates, cached: false, latest_run_id: 'run_p', evidence });
  const cF = (evidence, replicates) => ({ drift_loss: 0.72, pass_ratio: 0.0, pass_fail: false, score: 0.2, replicates, cached: false, latest_run_id: 'run_f', evidence });
  return {
    epoch_id: EPOCH, found: true,
    candidates: [
      { generation_id: 'g0', round_index: 0, promoted: true, champion_spine: true },
      { generation_id: 'g2', round_index: 1, promoted: true, champion_spine: true },
    ],
    entries: [
      { entry_id: 'task_login', slice: 'train', flip_rate: 0.2, flip_rate_measured: true, calibration_runs: 5, calibration_generation: 'g0' },
      { entry_id: 'task_hold', slice: 'holdout', flip_rate: null, flip_rate_measured: false, calibration_runs: 0 },
    ],
    cells: [[cP('replicated', 2), cP('replicated', 2)], [cP('replicated', 2), cF('replicated', 2)]],
    calibration: { measured: true, generation_id: 'g0', runs: 5, max_abs_delta: 0.06 },
  };
}

// The ghost feed: a MEASURED board_entry draft (renders + fills pips), an
// UNMEASURED one (planned), one already on the board (excluded), and a judge
// suggestion (not a board entry → excluded). Two ghost rows survive.
function ghostFeed() {
  return {
    epoch_id: EPOCH, reflection_id: 'refl-x',
    suggestions: [
      { suggestion_id: 'g1', artifact_kind: 'board_entry', target_slice: 'train', draft_artifact: { id: 'ghost_probe' },
        admission: { noise: { flip_rate: 0.1, runs: 5, measured: true }, discrimination: { separated: 2, pairs: 3, measured: true }, leakage: { target_slice_ok: true } } },
      { suggestion_id: 'g2', artifact_kind: 'board_entry', target_slice: 'train',
        proposed_op: { op: 'add_board_entry', args: { entry: { id: 'ghost_plan' } } }, admission: null },
      { suggestion_id: 'g3', artifact_kind: 'board_entry', draft_artifact: { id: 'task_login' }, admission: null },
      { suggestion_id: 'g4', artifact_kind: 'judge', draft_artifact: { id: 'jx' }, admission: null },
    ],
  };
}

async function renderWith(feed) {
  const host = globalThis.document.createElement('div');
  if (feed) evals._setGhostFeedForTest(feed, EPOCH);
  await evals.render(host, CTX, { epochId: EPOCH });
  return host;
}

test('ghost rows: suggested board entries render below the real rows (already-on-board + judges excluded)', async () => {
  fresh(); installMatrixOnly();
  const host = await renderWith(ghostFeed());
  const ghosts = allByClass(host, 'dn-evalmtx-ghost');
  assertEqual(ghosts.length, 2, 'exactly the two new board_entry drafts render as ghost rows');
  const ids = allByClass(host, 'dn-evalmtx-ghost-site').map((n) => n.getAttribute('data-entry'));
  assert(ids.includes('ghost_probe') && ids.includes('ghost_plan'), 'both drafted ids show');
  assert(!ids.includes('task_login'), 'a draft whose id is already a board row is NOT ghosted');
  assert(!ids.includes('jx'), 'a judge suggestion never becomes a ghost row');
  // the group caption states these are drafts rather than scored entries.
  assert(hasClass(host, 'dn-evalmtx-ghostcaption'), 'the ghost group caption renders');
  assert(/drafts, not scored/.test(host.textContent), 'the caption states drafts-not-scored');
});

test('ghost rows: honesty styling — pending/ghost classes, admission marks IN the row, NO scored glyphs', async () => {
  fresh(); installMatrixOnly();
  const host = await renderWith(ghostFeed());
  const probe = allByClass(host, 'dn-evalmtx-ghost').find((r) => r.textContent.includes('ghost_probe'));
  assert(probe, 'the measured ghost row exists');
  // the honesty-styling class pin (a ghost row is visually unambiguous).
  assert(probe.getAttribute('class').includes('dn-evalmtx-ghost'), 'the row carries the ghost class');
  assert(hasClass(probe, 'dn-evalmtx-ghost-tag'), 'a "suggested" marker rides the row');
  assert(/proposed — not yet on the board/.test(probe.textContent), 'the honest not-yet-scored note renders');
  // the flip whisker sits in the row header (where the flip badge sits); the pips
  // + tier sit where cells would be.
  assert(hasClass(probe, 'dn-adm-whisker'), 'the flip whisker renders in the ghost row');
  assertEqual(allByClass(probe, 'dt-rungstep-done').length, 2, 'sep 2/3 → two filled pips');
  assert(/probed/.test(probe.textContent), 'the measured ghost reads the probed tier');
  // NOTHING mistakable for a scored verdict: no pass/fail cell glyphs, no drift.
  assertEqual(allByClass(probe, 'dn-evalmtx-square').length, 0, 'no pass square in a ghost row');
  assertEqual(allByClass(probe, 'dn-evalmtx-glyph').length, 0, 'no fail glyph in a ghost row');
  assertEqual(allByClass(probe, 'dn-evalmtx-drift').length, 0, 'no drift number in a ghost row');
});

test('ghost rows: an unmeasured draft is honest — planned tier, "unmeasured", no fabricated 0', async () => {
  fresh(); installMatrixOnly();
  const host = await renderWith(ghostFeed());
  const plan = allByClass(host, 'dn-evalmtx-ghost').find((r) => r.textContent.includes('ghost_plan'));
  assert(plan, 'the unmeasured ghost row exists');
  assert(/planned/.test(plan.textContent), 'the unmeasured draft reads the planned tier');
  assert(/unmeasured/.test(plan.textContent), 'it reads unmeasured');
  assertEqual(allByClass(plan, 'dt-rungstep-done').length, 0, 'no filled pips fabricated');
});

test('ghost rows: the apply affordance links to the builder (consistent with the inbox)', async () => {
  fresh(); installMatrixOnly();
  const host = await renderWith(ghostFeed());
  const apply = allByClass(host, 'dn-evalmtx-ghost-apply');
  assertEqual(apply.length, 2, 'each ghost row carries an apply control');
  assertEqual(apply[0].getAttribute('href'), router.href('builder', {}), 'apply routes to the builder draft');
});

test('ghost rows: BYTE-IDENTICAL no-ghost case — no feed leaves the matrix untouched + digest-stable', async () => {
  fresh(); installMatrixOnly();
  const host = await renderWith(null);   // no ghost feed
  assertEqual(allByClass(host, 'dn-evalmtx-ghost').length, 0, 'no ghost rows');
  assertEqual(allByClass(host, 'dn-evalmtx-ghosthead').length, 0, 'no ghost caption row');
  // the real matrix is intact.
  assertEqual(allByClass(host, 'dn-evalmtx-row').length, 2, 'the two real rows render');
  // a second identical no-ghost render reuses the Matrix DOM (digest no-op — the
  // ghost component never entered the digest, so it is byte-identical to today).
  const sec1 = host.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-section'))[0];
  await evals.render(host, CTX, { epochId: EPOCH });
  const sec2 = host.querySelectorAll('[class]').filter((n) => n.classList.contains('dn-section'))[0];
  assert(sec1 === sec2, 'the no-ghost render is digest-stable (DOM reused)');
});

test('ghost rows: a feed scoped to ANOTHER epoch contributes zero ghosts', async () => {
  fresh(); installMatrixOnly();
  const other = ghostFeed(); other.epoch_id = 'other-epoch';
  const host = await renderWith(other);
  assertEqual(allByClass(host, 'dn-evalmtx-ghost').length, 0, 'a foreign-epoch feed never ghosts this matrix');
});

test('ghost rows: a second identical render with ghosts churns no DOM (digest no-op)', async () => {
  fresh(); installMatrixOnly();
  const host = await renderWith(ghostFeed());
  const g1 = allByClass(host, 'dn-evalmtx-ghost')[0];
  await evals.render(host, CTX, { epochId: EPOCH });   // feed still seeded
  const g2 = allByClass(host, 'dn-evalmtx-ghost')[0];
  assert(g1 === g2, 'the ghost rows are reused on an identical repaint (digest-gated)');
});

test('ghost rows: a drafted entry id with markup stays inert text (XSS)', async () => {
  fresh(); installMatrixOnly();
  const feed = ghostFeed();
  feed.suggestions[0].draft_artifact.id = '<img src=x onerror=alert(1)>';
  const host = await renderWith(feed);
  assert(host.textContent.includes('<img src=x onerror=alert(1)>'), 'the drafted id renders as inert text');
  assertEqual(host.innerHTMLWriteCount(), 0, 'the ghost rows never write innerHTML');
});

await run();
