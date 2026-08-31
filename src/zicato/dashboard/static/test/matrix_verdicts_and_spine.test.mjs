// test/matrix_verdicts_and_spine.test.mjs — the tense of a matrix verdict, and
// a one-generation champion spine.
//
// Two failure modes this file guards against:
//
//   A matrix that says "racing…" for every candidate. That needs two faults at
//   once: a decision source that bypasses the shared classifier, so a settled
//   rejection recorded in lineage.json arrives as `promoted: null`; and a null
//   that maps to the PENDING pill, whose label is the present-tense "racing…".
//   An epoch whose loop stopped months ago is racing nothing. The server half
//   is pinned in tests/test_query_eval_view.py.
//
//   "No champion-spine trajectory" while the spine is present. A spine of [v0]
//   means the seed reigned and every challenger was rejected. A derivation that
//   drops the seed reads a one-generation reign as no reign at all, and an
//   empty state that says "yet" promises a future that has already not happened.
//
// Pinned here (the CLIENT half; the server half lives in the Python suite):
//   * the pending pill is tense-bound: "racing…" only under a live-for-this-
//     epoch loop, "undecided" on a settled or interrupted one, with the SAME
//     `dn-pending` class either way, so the vocabulary stays unforked;
//   * the seed column reads as the SEED rather than as a candidate that won a
//     gate;
//   * the digest moves when the tense moves, leaving no stale "racing…" on
//     screen;
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
    s.liveness = { state: 'live', epoch_id: EPOCH };
  } else {
    s.heartbeat = { epoch_id: EPOCH, phase: 'tournament:round_0:racing-final', ts: JUNE };
    s.activeTournament = { epoch_id: EPOCH, structure: 'racing', phase: 'running' };
    s.activeRuns = [{ run_id: 'r0', entry_id: 'e', generation_id: 'v7', last_progress_ts: JUNE }];
    s.liveness = { state: 'interrupted', ended_at: '2026-06-08T03:58:49Z' };
  }
  data.invalidate();
  globalThis.window.location = { hash: '', search: '' };
}

// The e4 matrix as the server serves it: the seed on the spine, the challengers
// settled-rejected, and one candidate still undecided.
function e4Matrix(pendingLabel = 'undecided') {
  const cell = (pass) => ({ drift_loss: pass ? 0.2 : 0.9, pass_ratio: pass ? 1 : 0, pass_fail: pass,
    replicates: 2, cached: false, latest_run_id: 'r', evidence: 'replicated' });
  return {
    epoch_id: EPOCH, found: true,
    candidates: [
      { generation_id: 'v0', round_index: 0, promoted: true, decision: 'baseline', decision_label: 'seed (v0)', seed: true, champion_spine: true },
      { generation_id: 'v3', round_index: 0, promoted: false, decision: 'rejected', decision_label: 'rejected', seed: false, champion_spine: false },
      { generation_id: 'v7', round_index: 0, promoted: null, decision: 'pending', decision_label: pendingLabel, seed: false, champion_spine: false },
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

test('verdictPill preserves server-owned labels without consulting liveness', () => {
  assertEqual(ui.verdictPill('pending', { label: 'racing…' }).textContent, 'racing…', 'live label');
  assertEqual(ui.verdictPill('pending', { label: 'undecided' }).textContent, 'undecided', 'settled label');
  assertEqual(ui.verdictPill('baseline', { label: 'seed (v0)' }).textContent, 'seed (v0)', 'seed label');
});

test('evals matrix: an INTERRUPTED epoch reads "undecided", never "racing…"', async () => {
  setClock('interrupted');
  installFixtureMap({ [EVALS_PATH]: e4Matrix('undecided') });
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
  installFixtureMap({ [EVALS_PATH]: e4Matrix('racing…') });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(/racing…/.test(textOf(host)), 'an undecided candidate in a running epoch IS racing');
  assert(!/undecided/.test(textOf(host)), 'and is not described as finished');
});

test('evals matrix: liveness for ANOTHER epoch does not lend this one the present tense', async () => {
  setClock('live');
  coreState.state.activeTournament = { epoch_id: 'some-other-epoch', structure: 'racing', phase: 'running' };
  coreState.state.heartbeat = Object.assign({}, coreState.state.heartbeat, { epoch_id: 'some-other-epoch' });
  coreState.state.liveness = { state: 'live', epoch_id: 'some-other-epoch' };
  installFixtureMap({ [EVALS_PATH]: e4Matrix('undecided') });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(/undecided/.test(textOf(host)), 'a race in e5 does not make e4 racing');
});

test('evals matrix: the SEED column reads "seed (v0)", not "promoted"', async () => {
  setClock('interrupted');
  installFixtureMap({ [EVALS_PATH]: e4Matrix('undecided') });
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
  installFixtureMap({ [EVALS_PATH]: e4Matrix('racing…') });
  const host = document.createElement('div');
  await evals.render(host, CTX, { epochId: EPOCH });
  assert(/racing…/.test(textOf(host)), 'live first');
  // The loop dies and the server changes the presentation-ready label.
  setClock('interrupted');
  installFixtureMap({ [EVALS_PATH]: e4Matrix('undecided') });
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
  assertEqual(series.values[0], 90.5, "carrying the seed's own reading on this entry");
  const host = renderSections(d);
  const text = textOf(host);
  assert(!/No champion-spine trajectory/.test(text),
    'the panel renders the reading — it does not claim the spine is absent');
  // One point is a reading rather than a trend, so it is stated as a NUMBER. A
  // sparkline of one point is a lone dot in a wide frame: a chart that looks
  // broken while carrying one fact.
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
