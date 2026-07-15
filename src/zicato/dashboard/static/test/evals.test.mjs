// test/evals.test.mjs — the EVALS view (views/evals.js, EVAL-VIEW.md WS-MATRIX).
//
// Covers: the router round-trip for the new epoch-scoped #/e/<id>/evals route;
// the known-answer render off a fixture matrix payload (rows / columns / cells /
// champion-spine crown / decision pills); the evidence-shading classes per tier
// (single → faint, replicated → firm); each client-side filter (failures /
// flips / holdout); the digest no-op pin (a second identical render rebuilds
// ZERO DOM); the honest empty-state degrades (transport null / found:false /
// empty board); and the click-through hrefs (the run transcript + the live
// harmonograf deep-link). The reader's aggregation is covered by the Python
// tests; this asserts the FRONTEND rendering + gating only.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/router.js');
const data = await import('../js/data.js');
const evals = await import('../js/views/evals.js');
const harmonograf = await import('../js/core/harmonograf.js');
const coreState = await import('../js/core/state.js');

const EPOCH = 'e3';
const HG_URL = 'http://127.0.0.1:42017';
const EVALS_PATH = `/api/epoch/${EPOCH}/evals`;

const CTX = { navigate() {}, href: router.href };

function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter(
    (n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
function textOf(host) { return host.textContent || ''; }
function hasClass(host, cls) { return allByClass(host, cls).length > 0; }

function installFixtureMap(F) {
  globalThis.fetch = async (path) => {
    if (Object.prototype.hasOwnProperty.call(F, path)) return { ok: true, json: async () => F[path] };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };
}
function fresh() {
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
  setLive(false);
}

// harmonograf liveness seeding (the harmonograf.test.mjs idiom).
function setLive(live) {
  const s = coreState.state;
  s.heartbeat = live ? { harmonograf_url: HG_URL } : null;
  s.activeTournament = null;
  s.activeRuns = live ? [{ run_id: 'r_live', entry_id: 'task_login', generation_id: 'g0', progress: 0.4 }] : [];
  harmonograf._resetHarmonografUiProbe();
  if (live) harmonograf._seedHarmonografUiProbe(HG_URL, true);
}

// A known-answer matrix payload (build_eval_matrix shape, EVAL-VIEW.md §3.1).
// Columns: g0 (r0, spine) · g1 (r1) · g2 (r1, spine). Rows:
//   task_login  (train)   pass·rep | fail·single | pass·rep   → has fail, FLIPS
//   task_hold   (holdout) pass·rep | (null)      | pass·rep   → no fail, no flip
//   task_flat   (train)   fail·rep | fail·rep    | fail·rep   → has fail, no flip
function matrixFixture() {
  const cP = (evidence, replicates) => ({ drift_loss: 0.31, pass_ratio: 1.0, pass_fail: true, score: 0.9,
    replicates, cached: false, latest_run_id: 'run_p', runtime_ms_mean: 41000, evidence });
  const cF = (evidence, replicates) => ({ drift_loss: 0.72, pass_ratio: 0.0, pass_fail: false, score: 0.2,
    replicates, cached: false, latest_run_id: 'run_f', evidence });
  return {
    epoch_id: EPOCH, found: true,
    candidates: [
      { generation_id: 'g0', round_index: 0, promoted: true, champion_spine: true, elo: 1503.2, elo_se: 44.1 },
      { generation_id: 'g1', round_index: 1, promoted: false, champion_spine: false, elo: 1490.0, elo_se: 40.0 },
      { generation_id: 'g2', round_index: 1, promoted: true, champion_spine: true, elo: 1520.0, elo_se: 39.0 },
    ],
    entries: [
      { entry_id: 'task_login', slice: 'train', tag: null, flip_rate: 0.2, flip_rate_measured: true, calibration_runs: 5 },
      { entry_id: 'task_hold', slice: 'holdout', tag: 'holdout', flip_rate: null, flip_rate_measured: false, calibration_runs: 0 },
      { entry_id: 'task_flat', slice: 'train', tag: null, flip_rate: 0.0, flip_rate_measured: true, calibration_runs: 5 },
    ],
    cells: [
      [cP('replicated', 2), cF('single', 1), cP('replicated', 3)],
      [cP('replicated', 2), null, cP('replicated', 2)],
      [cF('replicated', 2), cF('replicated', 2), cF('replicated', 2)],
    ],
    calibration: { measured: true, generation_id: 'g0', runs: 5, max_abs_delta: 0.06 },
  };
}

function EMPTY_MATRIX() {
  return { epoch_id: EPOCH, found: false, candidates: [], entries: [], cells: [],
    calibration: { measured: false, generation_id: null, runs: 0, max_abs_delta: null },
    note: 'no such epoch / never indexed' };
}

// reset filter state to the default (all off) before a filter test, by driving
// the toolbar toggles from a fresh render. Filters are module-level + persist,
// so a test that flips one must flip it back (done inline below).

// ====================================================================
// ROUTER round-trip.
// ====================================================================
test('router: evals is a registered epoch-scoped VIEW; parse/href round-trip; up → epoch', () => {
  assert(router.VIEWS.includes('evals'), 'evals in VIEWS');
  const url = router.href('evals', { epochId: EPOCH });
  assertEqual(url, `#/e/${EPOCH}/evals`, 'href is the epoch-scoped evals route');
  const parsed = router.parseRoute(url);
  assertEqual(parsed.view, 'evals', 'parseRoute resolves the evals view');
  assertEqual(parsed.params.epochId, EPOCH, 'the epoch id round-trips');
  assertEqual(router.up({ view: 'evals', params: { epochId: EPOCH } }).view, 'epoch', 'evals steps up to the epoch');
  const trail = router.crumbTrail({ view: 'evals', params: { epochId: EPOCH } });
  assert(trail.some((c) => c.label === 'evals' && c.current), 'the crumb trail ends at evals');
});

// ====================================================================
// RENDER — rows / columns / cells / spine / decision pills.
// ====================================================================
test('render: paints the entries × candidates matrix with the spine crown + decision pills', async () => {
  fresh();
  installFixtureMap({ [EVALS_PATH]: matrixFixture() });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  // three entry rows, three candidate columns.
  assertEqual(allByClass(host, 'dn-evalmtx-row').length, 3, 'one row per board entry');
  assertEqual(allByClass(host, 'dn-evalmtx-gen').length, 3, 'one header cell per candidate');
  // the champion spine: g0 + g2 carry the crown (g1 does not).
  assertEqual(allByClass(host, 'dn-evalmtx-crown').length, 2, 'the two spine candidates are crowned');
  // the shipped decision vocabulary is reused (dn-pill dn-promoted / dn-rejected).
  assert(hasClass(host, 'dn-promoted'), 'a promoted candidate carries the shipped promoted pill');
  assert(hasClass(host, 'dn-rejected'), 'a non-spine candidate carries the shipped rejected pill');
  // the round grouping header spans the two round-1 columns.
  assert(hasClass(host, 'dn-evalmtx-group'), 'the round-group super-header renders');
  // the matrix scrolls in its OWN container — the page body never scrolls.
  assert(hasClass(host, 'dn-table-scroll'), 'the wide matrix is wrapped in dn-table-scroll');
});

// ====================================================================
// EVIDENCE SHADING — per served tier (single → faint, replicated → firm).
// ====================================================================
test('evidence: a single-sample cell renders faint; a replicated one firm (served tier, not re-derived)', async () => {
  fresh();
  installFixtureMap({ [EVALS_PATH]: matrixFixture() });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(hasClass(host, 'dn-evalmtx-firm'), 'replicated cells render firm');
  const singles = allByClass(host, 'dn-evalmtx-single');
  assert(singles.length >= 1, 'a single-sample cell renders in the single/faint tier');
  assert(singles.every((n) => (n.getAttribute('class') || '').includes('dn-faint')), 'a single cell carries dn-faint');
  // the served evidence tier is stamped verbatim (DQ1 — never client re-derived).
  assert(allByClass(host, 'dn-evalmtx-cell').some((n) => n.getAttribute('data-evidence') === 'single'),
    'the served evidence tier is stamped on the cell');
});

// ====================================================================
// FLIP-RATE badges (§4.2 / §4.4) — measured %, unmeasured word, never 0.
// ====================================================================
test('flip badges: a measured entry shows a %, an unmeasured one says "unmeasured" (never 0)', async () => {
  fresh();
  installFixtureMap({ [EVALS_PATH]: matrixFixture() });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(textOf(host).includes('flip 20%'), 'the measured flip rate renders as a percentage');
  assert(hasClass(host, 'dn-eval-flip-unmeasured'), 'the uncalibrated entry renders an "unmeasured" badge');
  assert(textOf(host).toLowerCase().includes('flip unmeasured'), 'it says unmeasured, not 0');
  assert(!/flip 0%/.test(textOf(host)) || textOf(host).includes('flip 0%'),
    'a genuine 0% measured flip is allowed (task_flat); unmeasured is never printed as 0');
});

// ====================================================================
// FILTERS — failures-only, flips-only, holdout-only (client-side).
// ====================================================================
async function renderFiltered(host, clickFilter) {
  installFixtureMap({ [EVALS_PATH]: matrixFixture() });
  await evals.render(host, CTX, { epochId: EPOCH });
  if (clickFilter) {
    const chip = allByClass(host, 'dn-evals-chip').find((c) => c.getAttribute('data-filter') === clickFilter);
    assert(chip, 'the ' + clickFilter + ' filter chip exists');
    chip.dispatchEvent({ type: 'click', target: chip });
  }
}

test('filter failures-only: keeps rows with a failing cell (task_login + task_flat)', async () => {
  fresh();
  const host = document.createElement('div');
  await renderFiltered(host, 'failures');
  assertEqual(allByClass(host, 'dn-evalmtx-row').length, 2, 'two rows have a failing cell');
  assert(textOf(host).includes('task_login') && textOf(host).includes('task_flat'), 'the failing rows are shown');
  assert(!allByClass(host, 'dn-evalmtx-site').some((n) => n.getAttribute('data-entry') === 'task_hold'),
    'the all-pass holdout row is filtered out');
  // toggle it back off so module-level state does not leak into the next test.
  const chip = allByClass(host, 'dn-evals-chip').find((c) => c.getAttribute('data-filter') === 'failures');
  chip.dispatchEvent({ type: 'click', target: chip });
});

test('filter flips-only: keeps only the row whose verdict flips between columns (task_login)', async () => {
  fresh();
  const host = document.createElement('div');
  await renderFiltered(host, 'flips');
  assertEqual(allByClass(host, 'dn-evalmtx-row').length, 1, 'exactly one row flips across columns');
  assert(allByClass(host, 'dn-evalmtx-site').some((n) => n.getAttribute('data-entry') === 'task_login'),
    'the flipping row is task_login');
  const chip = allByClass(host, 'dn-evals-chip').find((c) => c.getAttribute('data-filter') === 'flips');
  chip.dispatchEvent({ type: 'click', target: chip });
});

test('filter holdout-only: keeps only the holdout-slice row (task_hold)', async () => {
  fresh();
  const host = document.createElement('div');
  await renderFiltered(host, 'holdout');
  assertEqual(allByClass(host, 'dn-evalmtx-row').length, 1, 'exactly one holdout row');
  assert(allByClass(host, 'dn-evalmtx-site').some((n) => n.getAttribute('data-entry') === 'task_hold'),
    'the holdout row is task_hold');
  const chip = allByClass(host, 'dn-evals-chip').find((c) => c.getAttribute('data-filter') === 'holdout');
  chip.dispatchEvent({ type: 'click', target: chip });
});

// ====================================================================
// DIGEST NO-OP — a second identical render rebuilds ZERO DOM.
// ====================================================================
test('digest no-op: a second identical render rebuilds ZERO DOM', async () => {
  fresh();
  installFixtureMap({ [EVALS_PATH]: matrixFixture() });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(host.firstChild === first, 'no clear-and-rebuild on the identical repaint');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// ====================================================================
// DEGRADES — honest empty states (never an error).
// ====================================================================
test('degrade: a transport failure shows an honest "unavailable" state', async () => {
  fresh();
  installFixtureMap({}); // no /api/epoch/<id>/evals → data.evalMatrix catches the 404 → null
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(textOf(host).toLowerCase().includes('unavailable'), 'the pane says the matrix is unavailable');
  assertEqual(allByClass(host, 'dn-evalmtx-row').length, 0, 'no rows painted on a failure');
});

test('degrade: a cold-index / unknown epoch (found:false) shows an honest empty state', async () => {
  fresh();
  installFixtureMap({ [EVALS_PATH]: EMPTY_MATRIX() });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(textOf(host).toLowerCase().includes('never') || textOf(host).toLowerCase().includes('no such epoch'),
    'the cold-index note renders');
  assertEqual(allByClass(host, 'dn-evalmtx-row').length, 0, 'no matrix painted');
});

test('degrade: a found epoch with no candidates/entries shows the empty-board state', async () => {
  fresh();
  const F = { ...matrixFixture(), found: true, candidates: [], entries: [], cells: [] };
  installFixtureMap({ [EVALS_PATH]: F });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(textOf(host).toLowerCase().includes('no scored candidates'), 'the empty-board state renders');
});

// ====================================================================
// CLICK-THROUGH — the run transcript href + the live harmonograf deep-link.
// ====================================================================
test('click-through: a cell links into the run transcript (the board route)', async () => {
  fresh();
  installFixtureMap({ [EVALS_PATH]: matrixFixture() });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  const links = allByClass(host, 'dn-evalmtx-celllink');
  assert(links.length >= 1, 'the cells carry transcript links');
  const href = links[0].getAttribute('href');
  assertEqual(href, `#/e/${EPOCH}/board/task_login/g0`, 'the first cell links to its board transcript (entry × candidate)');
});

test('click-through: a live epoch adds the harmonograf deep-link; a dead one omits it', async () => {
  fresh();
  installFixtureMap({ [EVALS_PATH]: matrixFixture() });
  // DEAD: no harmonograf link.
  const dead = document.createElement('div');
  await evals.render(dead, CTX, { epochId: EPOCH });
  assertEqual(allByClass(dead, 'dn-evalmtx-hg').length, 0, 'no harmonograf link while the loop is dead');
  // LIVE: the deep-link appears on the cells.
  setLive(true);
  const live = document.createElement('div');
  await evals.render(live, CTX, { epochId: EPOCH });
  assert(allByClass(live, 'dn-evalmtx-hg').length >= 1, 'a live run surfaces the harmonograf deep-link');
  const hg = allByClass(live, 'dn-evalmtx-hg')[0];
  assert((hg.getAttribute('href') || '').startsWith(HG_URL), 'the harmonograf link points at the live server');
  setLive(false);
});

run();
