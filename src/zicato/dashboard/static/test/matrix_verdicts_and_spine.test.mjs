// test/matrix_verdicts_and_spine.test.mjs — issue #207 §2 + §3.
//
// TWO REDS, both found by browsing the post-#194 console against the June
// workspace, both verified against its files before a line was changed:
//
//   §2  THE EVALS MATRIX SAID "racing…" FOR EVERY CANDIDATE. Two bugs stacked.
//       The matrix's decision source bypassed the shared classifier, so a
//       settled rejection recorded in lineage.json arrived as `promoted: null`
//       (fixed server-side; pinned in tests/test_query_eval_view.py). And null
//       mapped to the PENDING pill, whose label is the present-tense "racing…"
//       — the one inventory surface the #203 liveness sweep did not reach. An
//       epoch that stopped in June is not racing anything.
//
//   §3  "No champion-spine trajectory" WITH THE SPINE PRESENT. e4's spine is
//       [v0]: the seed reigned and every challenger was rejected. The spine
//       derivation dropped the seed, so a one-generation reign read as no reign
//       at all — and the empty state said "yet", promising a future that had
//       already not happened.
//
// Pinned here (the CLIENT half — the server half lives in the Python suite):
//   * the pending pill is tense-bound: "racing…" only under a live-for-this-
//     epoch loop, "undecided" on a settled / interrupted one, with the SAME
//     `dn-pending` class either way (the vocabulary is not forked);
//   * the seed column reads as the SEED, not as a candidate that won a gate;
//   * the digest moves when the tense moves (no stale "racing…" left on screen);
//   * every empty spine / trajectory / attribution panel renders the SERVER's
//     reason, and different causes render different sentences.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const router = await import('../js/router.js');
const data = await import('../js/data.js');
const evals = await import('../js/views/evals.js');
const board = await import('../js/views/board.js');
const ui = await import('../js/ui.js');
const coreState = await import('../js/core/state.js');

const EPOCH = '2026-06-07_e4';
const EVALS_PATH = `/api/epoch/${EPOCH}/evals`;
const CTX = { navigate() {}, href: router.href };

// ── DOM helpers (the evals.test.mjs idiom) ────────────────────────────
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter(
    (n) => (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
function hasClass(host, cls) { return allByClass(host, cls).length > 0; }
function textOf(host) { return host.textContent || ''; }
function installFixtureMap(F) {
  globalThis.fetch = async (path) => {
    if (Object.prototype.hasOwnProperty.call(F, path)) return { ok: true, json: async () => F[path] };
    return { ok: false, status: 404, json: async () => ({ error: 'nf' }) };
  };
}

// ── the two workspace clocks ──────────────────────────────────────────
//
// INTERRUPTED is the June workspace verbatim in shape: every runtime file
// still says "running", every timestamp two months stale, and the server's own
// tri-state says interrupted. LIVE is the same epoch with a heartbeat from a
// second ago.
const JUNE = Date.parse('2026-06-08T03:58:49Z');

function setClock(kind) {
  const s = coreState.state;
  s.connected = true;
  s.lastSeq = 0;
  s.terminal = false;
  s.lastSeqAdvanceAt = Date.now();
  if (kind === 'live') {
    s.heartbeat = { epoch_id: EPOCH, phase: 'tournament:round_0:racing-final', ts: Date.now() - 1000 };
    s.activeTournament = { epoch_id: EPOCH, structure: 'racing', phase: 'running' };
    s.activeRuns = [{ run_id: 'r0', entry_id: 'e', generation_id: 'v7', last_progress_ts: Date.now() - 500 }];
    s.liveness = { state: 'live' };
  } else {
    s.heartbeat = { epoch_id: EPOCH, phase: 'tournament:round_0:racing-final', ts: JUNE };
    s.activeTournament = { epoch_id: EPOCH, structure: 'racing', phase: 'running' };
    s.activeRuns = [{ run_id: 'r0', entry_id: 'e', generation_id: 'v7', last_progress_ts: JUNE }];
    s.liveness = { state: 'interrupted', ended_at: '2026-06-08T03:58:49Z' };
  }
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
}

// The e4 matrix as the FIXED server now serves it: the seed on the spine, the
// challengers settled-rejected, and one candidate genuinely undecided.
function e4Matrix() {
  const cell = (pass) => ({ drift_loss: pass ? 0.2 : 0.9, pass_ratio: pass ? 1 : 0, pass_fail: pass,
    replicates: 2, cached: false, latest_run_id: 'r', evidence: 'replicated' });
  return {
    epoch_id: EPOCH, found: true,
    candidates: [
      { generation_id: 'v0', round_index: 0, promoted: true, seed: true, champion_spine: true },
      { generation_id: 'v3', round_index: 0, promoted: false, seed: false, champion_spine: false },
      { generation_id: 'v7', round_index: 0, promoted: null, seed: false, champion_spine: false },
    ],
    entries: [
      { entry_id: 'waffles_single', slice: 'train', tag: null, flip_rate: null,
        flip_rate_measured: false, calibration_runs: 0, calibration_generation: null },
    ],
    cells: [[cell(false), cell(true), null]],
    calibration: { measured: false, generation_id: null, runs: 0, max_abs_delta: null },
  };
}

// ════════════════════════════════════════════════════════════════════
// 1 — the pill vocabulary is tense-bound
// ════════════════════════════════════════════════════════════════════

test('verdictPill: pending reads "racing…" live and "undecided" settled — same class, one vocabulary', () => {
  assertEqual(ui.verdictPill('pending').textContent, 'racing…', 'the default stays present-tense');
  assertEqual(ui.verdictPill('pending', { live: true }).textContent, 'racing…', 'live is explicit too');
  assertEqual(ui.verdictPill('pending', { live: false }).textContent, 'undecided',
    'a settled context says what happened, not what is happening');
  // The DECISION token and its class are untouched — only the word moves. A
  // forked vocabulary would have had to invent a colour for "undecided".
  const settled = ui.verdictPill('pending', { live: false }).getAttribute('class');
  assertEqual(settled, ui.verdictPill('pending', { live: true }).getAttribute('class'),
    'both tenses carry the shipped dn-pending class');
  // Every other decision is a settled fact already; liveness cannot touch it.
  for (const d of ['promoted', 'rejected', 'deferred']) {
    assertEqual(ui.verdictPill(d, { live: false }).textContent, d, d + ' reads the same either way');
  }
  assertEqual(ui.verdictPill('baseline', { live: false }).textContent, 'seed (v0)', 'the seed is the seed');
});

test('evals matrix: an INTERRUPTED epoch reads "undecided", never "racing…"', async () => {
  setClock('interrupted');
  installFixtureMap({ [EVALS_PATH]: e4Matrix() });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  const text = textOf(host);
  assert(!/racing…/.test(text), 'nothing on a June epoch claims to be racing');
  assert(/undecided/.test(text), 'the genuinely-unsettled candidate reads past-tense');
  // The settled rejection is a REJECTION — the §2 red was every column reading
  // pending because the decision never reached the client.
  assert(hasClass(host, 'dn-rejected'), 'v3 carries the rejected pill');
  assert(hasClass(host, 'dn-pending'), 'v7 keeps the pending class (undecided is a LABEL, not a state)');
});

test('evals matrix: a LIVE epoch keeps the present tense', async () => {
  setClock('live');
  installFixtureMap({ [EVALS_PATH]: e4Matrix() });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(/racing…/.test(textOf(host)), 'an undecided candidate in a running epoch IS racing');
  assert(!/undecided/.test(textOf(host)), 'and is not described as finished');
});

test('evals matrix: liveness for ANOTHER epoch does not lend this one the present tense', async () => {
  setClock('live');
  coreState.state.activeTournament = { epoch_id: 'some-other-epoch', structure: 'racing', phase: 'running' };
  coreState.state.heartbeat = Object.assign({}, coreState.state.heartbeat, { epoch_id: 'some-other-epoch' });
  installFixtureMap({ [EVALS_PATH]: e4Matrix() });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(/undecided/.test(textOf(host)), 'a race in e5 does not make e4 racing');
});

test('evals matrix: the SEED column reads "seed (v0)", not "promoted"', async () => {
  setClock('interrupted');
  installFixtureMap({ [EVALS_PATH]: e4Matrix() });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  const crowns = allByClass(host, 'dn-evalmtx-crown');
  assertEqual(crowns.length, 1, 'the seed is the whole spine, and it is crowned');
  assertEqual(crowns[0].getAttribute('title'), 'the seed — the champion this epoch started from',
    'the crown says WHY it is crowned — a seed did not win a gate');
  assert(/seed \(v0\)/.test(textOf(host)), 'the seed pill names it the seed');
});

test('digest: the render repaints when the loop stops — no stale "racing…" left on screen', async () => {
  setClock('live');
  installFixtureMap({ [EVALS_PATH]: e4Matrix() });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(/racing…/.test(textOf(host)), 'live first');
  // The loop dies. Same payload, same filters — only the clock moved.
  setClock('interrupted');
  installFixtureMap({ [EVALS_PATH]: e4Matrix() });
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(/undecided/.test(textOf(host)), 'the gate let the repaint through');
  assert(!/racing…/.test(textOf(host)), 'and the present-tense claim is gone');
});

// ════════════════════════════════════════════════════════════════════
// 2 — the seed spine + the reasoned empty states
// ════════════════════════════════════════════════════════════════════

function renderSections(dossier) {
  const host = document.createElement('div');
  for (const n of board.evalDossierSections(dossier, CTX, EPOCH, dossier.entry_id)) host.appendChild(n);
  return host;
}

// The e4 dossier as the FIXED server serves it: a ONE-GENERATION spine (the
// seed), which is a real trajectory — v0's own reading on the entry.
function seedSpineDossier(overrides) {
  return Object.assign({
    epoch_id: EPOCH, entry_id: 'waffles_single', found: true, slice: 'train', tag: null,
    instrument: {
      flip_rate: null, flip_rate_measured: false, calibration_runs: 0, calibration_generation: null,
      discrimination: null, discrimination_pairs: 0,
      runtime_ms_mean: 500, runtime_ms_p50: 500, runtime_ms_max: 500,
      replicate_total: 2, cached_share: 0,
    },
    trajectory: [
      { generation_id: 'v0', round_index: 0, champion_spine: true, seed: true,
        drift_loss: 90.5, pass_ratio: 0.0, replicates: 2, cached: false },
      { generation_id: 'v3', round_index: 0, champion_spine: false, seed: false,
        drift_loss: 10.5, pass_ratio: 1.0, replicates: 1, cached: false },
    ],
    trajectory_reason: null,
    attribution: {
      first_passed_by: null, regressed_by: [],
      first_passed_reason: 'The seed (v0) did not pass this entry, and no later generation was promoted.',
      regressed_reason: 'A one-generation spine cannot regress — only the seed (v0) ever reigned.',
    },
    reflection_findings: [],
  }, overrides || {});
}

test('trajectory: a ONE-GENERATION spine is a real trajectory — the seed is spine point 1', () => {
  const d = seedSpineDossier();
  const series = board.trajectorySeries(d);
  assertEqual(series.gens.length, 1, 'the spine is [v0]');
  assertEqual(series.gens[0], 'v0', 'and its one point IS the seed');
  assertEqual(series.loss[0], 90.5, "carrying the seed's own reading on this entry");
  const host = renderSections(d);
  const text = textOf(host);
  assert(!/No champion-spine trajectory/.test(text),
    'the panel renders the reading — it does not claim the spine is absent');
  // One point is a reading, not a trend: it is stated as a NUMBER, because a
  // sparkline of one point is a lone dot in a wide frame — a broken-looking
  // chart carrying one fact.
  assert(/90\.50/.test(text), "the seed's reading on this entry is the content");
  assert(/drift loss · v0/.test(text), 'and it is attributed to the generation that took it');
  assert(/the reign never changed hands/.test(text), 'with the reason there is no slope');
  assertEqual(host.querySelectorAll('svg').length, 0, 'no sparkline is drawn for a single point');
});

test('attribution: an empty first-passed row says WHY, in the past tense', () => {
  const host = renderSections(seedSpineDossier());
  const text = textOf(host);
  assert(/The seed \(v0\) did not pass this entry, and no later generation was promoted\./.test(text),
    'the served reason is rendered verbatim — a finding, not a stall notice');
  assert(!/passed this entry yet/.test(text), 'nothing promises a future that is not coming');
  assert(/one-generation spine cannot regress/.test(text),
    'and the regression row explains its own emptiness differently');
});

test('empty states: different causes render DIFFERENT reasons, all server-derived', () => {
  const reasons = [
    'The champion spine (v0) never ran this entry.',
    'The champion spine ran this entry but recorded no drift loss — the loss records are unavailable.',
    'No generation was promoted in this epoch and no seed is on record, so the epoch has no champion spine to plot.',
  ];
  const seen = new Set();
  for (const reason of reasons) {
    const host = renderSections(seedSpineDossier({
      trajectory: [{ generation_id: 'v0', round_index: 0, champion_spine: true, seed: true,
        drift_loss: null, pass_ratio: null, replicates: 0, cached: false }],
      trajectory_reason: reason,
    }));
    const text = textOf(host);
    assert(text.includes(reason), 'the panel carries its own cause: ' + reason);
    assert(!/No champion-spine trajectory for this entry/.test(text), 'never the generic line when a reason is served');
    seen.add(reason);
  }
  assertEqual(seen.size, 3, 'three causes, three distinct sentences');
});

test('empty states: a pre-reason payload still degrades honestly, without "yet"', () => {
  // An older server that serves no reason at all. The fallback must still not
  // claim the emptiness is temporary.
  const d = seedSpineDossier({
    trajectory: [{ generation_id: 'v0', round_index: 0, champion_spine: true, seed: true,
      drift_loss: null, pass_ratio: null, replicates: 0, cached: false }],
  });
  delete d.trajectory_reason;
  delete d.attribution.first_passed_reason;
  delete d.attribution.regressed_reason;
  const text = textOf(renderSections(d));
  assert(/No champion-spine trajectory for this entry\./.test(text), 'the generic line renders');
  assert(!/\byet\b/.test(text), 'and it is still past-tense');
});

test('digest: a changed empty-state REASON repaints, even though every number stayed null', () => {
  const base = seedSpineDossier({
    trajectory: [{ generation_id: 'v0', round_index: 0, champion_spine: true, seed: true,
      drift_loss: null, pass_ratio: null, replicates: 0, cached: false }],
    trajectory_reason: 'The champion spine (v0) never ran this entry.',
  });
  const moved = seedSpineDossier({
    trajectory: [{ generation_id: 'v0', round_index: 0, champion_spine: true, seed: true,
      drift_loss: null, pass_ratio: null, replicates: 0, cached: false }],
    trajectory_reason: 'The champion spine ran this entry but recorded no drift loss — the loss records are unavailable.',
  });
  assertEqual(board.evalDossierDigest(base, EPOCH, 'waffles_single'),
    board.evalDossierDigest(base, EPOCH, 'waffles_single'), 'a no-op beat is byte-identical');
  assert(board.evalDossierDigest(base, EPOCH, 'waffles_single')
    !== board.evalDossierDigest(moved, EPOCH, 'waffles_single'),
    'a changed reason is a changed render');
});

// The app state is a SHARED module singleton and run-all imports every test
// file into ONE process, so a file that seeds a clock must hand it back clean —
// otherwise every later file inherits a workspace that died in June.
const _pristine = {
  connected: coreState.state.connected,
  heartbeat: coreState.state.heartbeat,
  activeTournament: coreState.state.activeTournament,
  activeRuns: coreState.state.activeRuns,
  liveness: coreState.state.liveness,
  lastSeq: coreState.state.lastSeq,
  terminal: coreState.state.terminal,
  lastSeqAdvanceAt: coreState.state.lastSeqAdvanceAt,
};

await run();
Object.assign(coreState.state, _pristine);
data.invalidate();
