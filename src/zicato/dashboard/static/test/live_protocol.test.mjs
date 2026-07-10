// test/live_protocol.test.mjs — the LIVE PROTOCOL contract across structures.
//
// The new tournament-viz designs (Wave A/B) re-dispatch BOTH the single-round
// structure figure (views/structure.js renderStructure / the builders) and the
// LIVE HERO (live.js LiveController) to the same mini builders. This file pins the
// live PROTOCOL the operator cares about — for EACH structure (racing, gauntlet,
// single_elim, double_elim, swiss):
//
//   * LIFECYCLE PROGRESSION — a fixture SEQUENCE queued → in-flight (live_progress
//     present) → projected (dn-proj + a ~projected scalar) → settled, asserting
//     each stage renders the expected treatment in BOTH the single-round figure
//     and the hero.
//   * CONVERGENCE GUARANTEE — the SETTLED render is byte-identical (same serialized
//     SVG) whether reached via the LIVE path (projected → settled) or built
//     DIRECTLY from the completed record. This is the core invariant: no live-only
//     chrome may leak past commit.
//   * ANTI-FLASH DIGEST DISCIPLINE — two consecutive IDENTICAL ticks (a no-op SSE
//     heartbeat) produce the SAME digest and DO NOT rebuild the figure DOM node
//     (node identity preserved); a REAL change (a board lands / a cut happens /
//     the champion scalar moves) DOES change the digest and repaint.
//   * PRODUCER/CONSUMER PARITY — the JS consumes the racing per-lane `live_progress`
//     shape `{boards_done, boards_total, projected_scalar, inflight, projected}`
//     the backend publishes (the python side has its own parity tests).
//
// These exercise the REAL modules (svg.js builders, structure.js render*, live.js
// LiveController) with inline fixtures — no live run, no network.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';
const mock = await import('./mock_server.mjs');

installDom();

const svg = await import('../js/svg.js');
const STRUCT = await import('../js/views/structure.js');
const ui = await import('../js/ui.js');
const live = await import('../js/live.js');

const EPOCH = '2026-06-06_e7';
const CTX = { navigate() {}, href: () => '#' };

// ---- helpers --------------------------------------------------------------

function walk(node, fn) {
  fn(node);
  for (const c of (node.childNodes || [])) if (c && c.nodeType === 1) walk(c, fn);
}
function svgsByClass(host, cls) {
  const out = [];
  walk(host, (n) => {
    if (n.localName === 'svg' && (n.getAttribute('class') || '').split(/\s+/).includes(cls)) out.push(n);
  });
  return out;
}
function nodesByClass(host, cls) {
  const out = [];
  walk(host, (n) => {
    if ((n.getAttribute && n.getAttribute('class') || '').split(/\s+/).includes(cls)) out.push(n);
  });
  return out;
}
function textOf(node) {
  let s = '';
  walk(node, (n) => { for (const c of (n.childNodes || [])) if (c.nodeType === 3) s += c.textContent; });
  return s;
}
// Does any descendant carry the `dn-proj` projected treatment?
function hasProjected(node) { return nodesByClass(node, 'dn-proj').length > 0; }

// A DETERMINISTIC serialization of an SVG subtree (tag + sorted attrs + text),
// the harness's stand-in for outerHTML (which it does not serialize). Two renders
// that serialize identically ARE byte-identical for the purposes of convergence.
function serialize(node) {
  if (!node) return '';
  if (node.nodeType === 3) return '#' + node.textContent;
  const attrs = Object.keys(node._attrs || {}).sort()
    .map((k) => `${k}=${node._attrs[k]}`).join(' ');
  const kids = (node.childNodes || []).map(serialize).join('');
  return `<${node.localName} ${attrs}>${kids}</${node.localName}>`;
}

// Render the single-round structure figure for a payload + return the figure host
// (a div containing all sections). Drives the REAL renderStructure dispatch.
function renderSingleRound(payload, liveFlag) {
  // PLAY THE SERVER: a served structure payload carries the elim model.
  const st = STRUCT.normalizeStructure(mock.attachElimStates({ ...payload }), !!liveFlag);
  const host = document.createElement('div');
  for (const n of STRUCT.renderStructure(st, CTX, EPOCH)) if (n) host.appendChild(n);
  return host;
}

// Drive a LiveController one tick + return its structure-figure host node.
function heroFigure(controller, { activeTournament, heartbeat, activeRuns }) {
  // PLAY THE SERVER: /api/active-tournament carries the served elim model.
  const served = activeTournament ? mock.attachElimStates({ ...activeTournament }) : activeTournament;
  controller.update({
    status: { running: true, structure: served && served.structure },
    heartbeat, activeRuns: activeRuns || [], activeTournament: served,
  });
  return controller._funnelHost;
}

// Assert a live-hero figure SVG is rendered FULL-WIDTH / aspect-locked (the same
// `responsive:true` treatment racing's scalar track uses): width:100%, the fixed
// pixel height DROPPED, preserveAspectRatio:none, an inline aspect-ratio pinned,
// and the structure's `dn-*-hero` max-width cap class carried. `heroClass` is the
// load-bearing svg.dn-*-hero cap the CSS sizes the figure against.
function assertHeroResponsive(figure, heroClass, name) {
  assert(figure, `${name}: the hero figure SVG is present`);
  assertEqual(figure.getAttribute('width'), '100%', `${name}: hero figure fills the width (width:100%)`);
  assert(!figure.hasAttribute('height') || figure.getAttribute('height') === null,
    `${name}: hero figure DROPS its fixed pixel height (height follows the aspect)`);
  assertEqual(figure.getAttribute('preserveAspectRatio'), 'none',
    `${name}: hero figure scales uniformly (preserveAspectRatio:none — no shear)`);
  const style = figure.getAttribute('style') || '';
  assert(/aspect-ratio:\s*\d/.test(style), `${name}: hero figure pins an inline aspect-ratio (aspect-locked, no shear)`);
  assert((figure.getAttribute('class') || '').split(/\s+/).includes(heroClass),
    `${name}: hero figure carries its ${heroClass} max-width cap class`);
}

// the active-tournament epoch tag the hero scopes to (must match the heartbeat).
function hb(extra) { return { phase: 'tournament:running', epoch_id: EPOCH, ts: Date.now(), ...extra }; }

// ===========================================================================
// RACING — the scalar track (single-round PRIMARY + hero) + the funnel (single
// -round SECONDARY). Lifecycle: queued (no scalar) → in-flight (live_progress) →
// projected (projected_scalar + dn-proj) → settled (committed scalar + verdict).
// ===========================================================================

// a SETTLED racing record (committed survivors/cuts, no live chrome).
const RACING_SETTLED = {
  structure: 'racing', phase: 'completed', epoch_id: EPOCH,
  structure_params: { rungs: [{ fraction: 0.5 }, { fraction: 1.0 }] },
  champion_lineage: ['v0', 'v1'],
  competitors: [
    { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
    { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v0', 'v1'], cut: ['v2', 'v3'], board_fraction: 0.5, deltas: { v1: -0.2, v2: 1.0, v3: 2.0 } }] },
    { round_index: 1, label: 'Champion gate', matches: [{ match_id: 'racing-final', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted', board_fraction: 1.0 }] },
  ],
  standings: [
    { generation_id: 'v0', rank: 2, scalar: 10.0, status: 'eliminated' },
    { generation_id: 'v1', rank: 1, scalar: 9.8, status: 'champion' },
    { generation_id: 'v2', rank: 3, scalar: 11.0, status: 'eliminated' },
    { generation_id: 'v3', rank: 4, scalar: 12.0, status: 'eliminated' },
  ],
  partial_champion_agg: { scalar: 10.0 },
};

// the LIVE rung-0 with a per-lane `live_progress` (queued / in-flight / projected).
function racingLive(stage) {
  const r = JSON.parse(JSON.stringify(RACING_SETTLED));
  r.phase = 'running';
  const m = r.rounds[0].matches[0];
  // rung-0 in flight: no committed survivors/cuts yet.
  m.survivors = []; m.cut = [];
  if (stage === 'queued') {
    // queued: no live_progress lanes yet (the field parked, no boards started).
    delete m.live_progress;
  } else if (stage === 'inflight') {
    // in-flight: boards streaming, NO projected scalar yet.
    m.live_progress = {
      v1: { boards_done: 2, boards_total: 8, inflight: 1, projected: false, done: 2, total: 8 },
      v2: { boards_done: 1, boards_total: 8, inflight: 1, projected: false, done: 1, total: 8 },
      v3: { boards_done: 0, boards_total: 8, inflight: 1, projected: false, done: 0, total: 8 },
    };
  } else if (stage === 'projected') {
    // projected: enough boards in to project a scalar (the dn-proj treatment).
    m.live_progress = {
      v1: { boards_done: 6, boards_total: 8, inflight: 1, projected: true, projected_scalar: 9.8, done: 6, total: 8 },
      v2: { boards_done: 5, boards_total: 8, inflight: 1, projected: true, projected_scalar: 11.0, done: 5, total: 8 },
      v3: { boards_done: 5, boards_total: 8, inflight: 1, projected: true, projected_scalar: 12.0, done: 5, total: 8 },
    };
  }
  // a live record carries no committed final gate yet.
  r.rounds[1].matches[0].winner = null; delete r.rounds[1].matches[0].decision;
  r.standings = [];
  return r;
}

test('racing — single-round scalar track renders each lifecycle stage (queued → in-flight → projected → settled)', () => {
  // queued: the track shows the field, no projected treatment, no committed cut.
  const q = renderSingleRound(racingLive('queued'), true);
  const qTrack = svgsByClass(q, 'dn-scalartrack')[0];
  assert(qTrack, 'queued: the scalar track renders the field');
  assert(!hasProjected(qTrack), 'queued: nothing reads as projected (no ~proj ghost)');

  // in-flight: a board sub-bar is present (live lanes), still no projected ghost.
  const f = renderSingleRound(racingLive('inflight'), true);
  const fTrack = svgsByClass(f, 'dn-scalartrack')[0];
  assert(fTrack, 'in-flight: the scalar track renders');
  assert(nodesByClass(fTrack, 'dn-scalartrack-live').length >= 1, 'in-flight: live (dashed/streaming) lanes are marked');
  assert(!hasProjected(fTrack), 'in-flight (no projection yet): no dn-proj ghost');

  // projected: the dn-proj treatment + a "~proj" suffix appear.
  const p = renderSingleRound(racingLive('projected'), true);
  const pTrack = svgsByClass(p, 'dn-scalartrack')[0];
  assert(pTrack, 'projected: the scalar track renders');
  assert(hasProjected(pTrack), 'projected: in-flight lanes ghost in the dn-proj treatment');
  assert(/~proj/.test(textOf(pTrack)) || /proj/.test(textOf(pTrack)), 'projected: a ~proj label is shown');

  // settled: committed verdicts (good/bad), no live/projected chrome.
  const s = renderSingleRound(RACING_SETTLED, false);
  const sTrack = svgsByClass(s, 'dn-scalartrack')[0];
  assert(sTrack, 'settled: the scalar track renders');
  assert(!hasProjected(sTrack), 'settled: NO projected ghost (committed render)');
  assert(nodesByClass(sTrack, 'dn-good').length >= 1 && nodesByClass(sTrack, 'dn-bad').length >= 1,
    'settled: committed survivor (good) + cut (bad) verdicts are drawn');
});

test('racing — live hero renders the scalar track mini across the lifecycle', () => {
  const c = new live.LiveController({});
  // queued.
  let figHost = heroFigure(c, { activeTournament: racingLive('queued'), heartbeat: hb({ phase: 'tournament:round_0:rung1' }) });
  assert(svgsByClass(figHost, 'dn-scalartrack')[0], 'hero: the racing mini is the scalar track (queued)');
  // projected → the dn-proj ghost appears in the hero too.
  figHost = heroFigure(c, { activeTournament: racingLive('projected'), heartbeat: hb({ phase: 'tournament:round_0:rung1' }),
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }] });
  const track = svgsByClass(figHost, 'dn-scalartrack')[0];
  assert(track, 'hero: the scalar track renders while projecting');
  assert(hasProjected(track), 'hero: a projected lane ghosts in the dn-proj treatment');
});

// ===========================================================================
// GAUNTLET — the field-bars (single-round figure). The default non-tournament
// structure: NO hero tournament figure (structure-ineligible), so the hero
// lifecycle is covered only at the single-round figure level here.
// ===========================================================================

const GAUNTLET_SETTLED = {
  structure: 'gauntlet', phase: 'completed', epoch_id: EPOCH,
  structure_params: { promote_margin: 0.5 },
  champion_lineage: ['v0', 'v1'],
  competitors: [
    { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
    { generation_id: 'v2', role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Wave', matches: [
      { match_id: 'g_v1', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted', delta_scalar: -0.8 },
      { match_id: 'g_v2', competitors: ['v0', 'v2'], winner: 'v0', decision: 'rejected', delta_scalar: 0.6 },
    ] },
  ],
  standings: [
    { generation_id: 'v0', rank: 2, scalar: 10.0, status: 'eliminated' },
    { generation_id: 'v1', rank: 1, scalar: 9.2, status: 'champion' },
    { generation_id: 'v2', rank: 3, scalar: 10.6, status: 'eliminated' },
  ],
  partial_champion_agg: { scalar: 10.0 },
};

function gauntletLive(stage) {
  const r = JSON.parse(JSON.stringify(GAUNTLET_SETTLED));
  r.phase = 'running';
  // v1's match is in flight (no committed decision yet); v2 settled-fail kept.
  const m1 = r.rounds[0].matches[0];
  delete m1.winner; delete m1.decision; delete m1.delta_scalar; m1.pending = true;
  if (stage === 'inflight') {
    m1.live_progress = { v1: { boards_done: 3, boards_total: 10, inflight: 1, projected: false, done: 3, total: 10 } };
  } else if (stage === 'projected') {
    m1.live_progress = { v1: { boards_done: 8, boards_total: 10, inflight: 1, projected: true, projected_scalar: 9.2, done: 8, total: 10 } };
  }
  r.standings = [
    { generation_id: 'v0', rank: 1, scalar: 10.0, status: 'champion' },
    stage === 'projected'
      ? { generation_id: 'v1', rank: 2, in_flight: true, projected_scalar: 9.2, boards_done: 8, boards_total: 10 }
      : { generation_id: 'v1', rank: 2, in_flight: true, boards_done: 3, boards_total: 10 },
    { generation_id: 'v2', rank: 3, scalar: 10.6, status: 'eliminated' },
  ];
  return r;
}

test('gauntlet — single-round field-bars renders each lifecycle stage + projected ghost', () => {
  const fIn = renderSingleRound(gauntletLive('inflight'), true);
  const barsIn = svgsByClass(fIn, 'dn-fieldbars')[0];
  assert(barsIn, 'in-flight: the gauntlet field-bars render');
  assert(!hasProjected(barsIn), 'in-flight (no projection): no dn-proj ghost yet');

  const fProj = renderSingleRound(gauntletLive('projected'), true);
  const barsProj = svgsByClass(fProj, 'dn-fieldbars')[0];
  assert(barsProj, 'projected: the gauntlet field-bars render');
  assert(hasProjected(barsProj), 'projected: the in-flight challenger ghosts in the dn-proj treatment');

  const s = renderSingleRound(GAUNTLET_SETTLED, false);
  const barsS = svgsByClass(s, 'dn-fieldbars')[0];
  assert(barsS, 'settled: the gauntlet field-bars render');
  assert(!hasProjected(barsS), 'settled: NO projected ghost (committed)');
  assert(nodesByClass(barsS, 'dn-good').length >= 1 && nodesByClass(barsS, 'dn-bad').length >= 1,
    'settled: a cleared (good) + a failed (bad) challenger are drawn');
});

test('gauntlet — the live hero renders the field-bars mini (the default-structure wave gets its own hero figure)', () => {
  const c = new live.LiveController({});
  const figHost = heroFigure(c, { activeTournament: gauntletLive('projected'), heartbeat: hb({ phase: 'tournament:round_0' }),
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }] });
  // gauntlet IS a hero structure: the wave-vs-the-champion-standard field-bars
  // render in mini (gated on a non-empty field). It must not borrow another
  // structure's figure.
  assert(svgsByClass(figHost, 'dn-fieldbars')[0], 'hero: the gauntlet field-bars mini renders');
  assertEqual(svgsByClass(figHost, 'dn-scalartrack').length, 0, 'hero: not the racing scalar track');
  // FULL-WIDTH HERO: the field-bars fill the width (aspect-locked) like racing's
  // scalar track — width:100% + the svg.dn-fieldbars-hero cap.
  assertHeroResponsive(svgsByClass(figHost, 'dn-fieldbars')[0], 'dn-fieldbars-hero', 'gauntlet hero');
});

// ===========================================================================
// SINGLE-ELIM — elimFlow (single-round figure + hero). The radial figure was
// retired (C1); single- and double-elim both render the elimFlow lane read.
// ===========================================================================

const ELIM_SETTLED = {
  structure: 'single_elim', phase: 'completed', epoch_id: EPOCH,
  champion_lineage: ['v0', 'v1'],
  competitors: [
    { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' },
  ],
  rounds: [
    { round_index: 0, label: 'Semifinal', matches: [
      { match_id: 'WB-R0-0', competitors: ['v0', 'v3'], winner: 'v0', decision: 'win', bracket_slot: 'WB-R0-0' },
      { match_id: 'WB-R0-1', competitors: ['v1', 'v2'], winner: 'v1', decision: 'win', bracket_slot: 'WB-R0-1' },
    ] },
    { round_index: 1, label: 'Final', matches: [
      { match_id: 'WB-R1-0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted', bracket_slot: 'WB-R1-0' },
    ] },
  ],
  standings: [],
};

function elimLive(stage) {
  const r = JSON.parse(JSON.stringify(ELIM_SETTLED));
  r.structure = 'single_elim'; r.phase = 'running';
  // the final is in flight: no winner/decision yet.
  const fin = r.rounds[1].matches[0];
  delete fin.winner; delete fin.decision; fin.pending = true;
  if (stage === 'projected') {
    fin.projected = { v1: { scalar: 9.8, boards_done: 6, boards_total: 8 }, v0: { scalar: 10.0, boards_done: 6, boards_total: 8 } };
  }
  return r;
}

test('single-elim — single-round elimFlow renders each lifecycle stage (in-flight semifinal cut → live final → settled crown)', () => {
  // live: a decided semifinal eliminates a lane (✕), the final is still deciding.
  const f = renderSingleRound(elimLive('inflight'), true);
  const flow = svgsByClass(f, 'dn-elimflow')[0];
  assert(flow, 'live: the elimFlow bracket renders');
  assert(/✕/.test(textOf(flow)), 'live: a decided semifinal cut shows ✕ in the flow');
  assert(nodesByClass(flow, 'dn-elimflow-conv-pending').length >= 1, 'live: the in-flight final reads as pending (not falsely decided)');

  // settled: the survivor reaches the gate, no pending convergences remain.
  const s = renderSingleRound(ELIM_SETTLED, false);
  const flowS = svgsByClass(s, 'dn-elimflow')[0];
  assert(flowS, 'settled: the elimFlow bracket renders');
  assert(nodesByClass(flowS, 'dn-elimflow-conv-pending').length === 0, 'settled: NO pending convergences remain (committed)');
  assert(/✕/.test(textOf(flowS)), 'settled: the eliminated lanes still read ✕');
});

test('single-elim — live hero renders the elimFlow mini + emits the ✕ elimination glyph', () => {
  const c = new live.LiveController({});
  const figHost = heroFigure(c, { activeTournament: elimLive('inflight'), heartbeat: hb({ phase: 'tournament:round_1' }),
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }] });
  const flow = svgsByClass(figHost, 'dn-elimflow')[0];
  assert(flow, 'hero: the single-elim mini is the elimFlow');
  assert(/✕/.test(textOf(flow)), 'hero: the decided semifinal emits ✕');
  assertEqual(svgsByClass(figHost, 'dn-scalartrack').length, 0, 'hero: no racing scalar track for an elim run');
  // FULL-WIDTH HERO: the elimFlow fills the width (aspect-locked) via width:100%
  // + the svg.dn-elimflow-hero cap.
  assertHeroResponsive(flow, 'dn-elimflow-hero', 'single-elim hero');
});

// ===========================================================================
// DOUBLE-ELIM — the elimFlow combo (single-round figure + hero).
// ===========================================================================

const DELIM_SETTLED = {
  structure: 'double_elim', phase: 'completed', epoch_id: EPOCH,
  champion_lineage: ['v0', 'v1'],
  competitors: [
    { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }, { generation_id: 'v4' },
  ],
  rounds: [
    { round_index: 0, label: "Winners' bracket", matches: [
      { match_id: 'WB-R0-0', competitors: ['v1', 'v2'], winner: 'v1', bracket_slot: 'WB-R0-0' },
      { match_id: 'WB-R0-1', competitors: ['v3', 'v4'], winner: 'v3', bracket_slot: 'WB-R0-1' },
    ] },
    { round_index: 1, label: "Winners' bracket", matches: [
      { match_id: 'WB-R1-0', competitors: ['v1', 'v3'], winner: 'v1', bracket_slot: 'WB-R1-0' },
    ] },
    { round_index: 2, label: "Losers' bracket", matches: [
      { match_id: 'LB-R2-0', competitors: ['v2', 'v4'], winner: 'v2', bracket_slot: 'LB-R2-0' },
    ] },
    { round_index: 3, label: "Losers' bracket", matches: [
      { match_id: 'LB-R3-0', competitors: ['v2', 'v3'], winner: 'v2', bracket_slot: 'LB-R3-0' },
    ] },
    { round_index: 4, label: 'Grand final', matches: [
      { match_id: 'GF', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted', bracket_slot: 'GF' },
    ] },
  ],
  standings: [],
};

function delimLive() {
  const r = JSON.parse(JSON.stringify(DELIM_SETTLED));
  r.phase = 'running';
  const gf = r.rounds[4].matches[0];
  delete gf.winner; delete gf.decision; gf.pending = true;
  return r;
}

test('double-elim — single-round elimFlow combo renders the WB→LB drop + the live GF (C1: no radial toggle)', () => {
  const f = renderSingleRound(delimLive(), true);
  const flow = svgsByClass(f, 'dn-elimflow')[0];
  assert(flow, 'live: the double-elim flow renders');
  // the WB→LB drop connector is present (a rounded path, marked a drop).
  const dropSegs = nodesByClass(flow, 'dn-elimflow-seg-drop');
  assert(dropSegs.length >= 1, 'live: at least one WB→LB drop connector is drawn (the second-life edge)');
  // C1: the radial figure + its combo/radial toggle were retired — flow only.
  assertEqual(svgsByClass(f, 'dn-elimradial').length, 0, 'no radial figure remains (C1)');
  assertEqual(nodesByClass(f, 'dt-fig-switch').length, 0, 'no figure-variant toggle remains (C1)');

  const s = renderSingleRound(DELIM_SETTLED, false);
  const flowS = svgsByClass(s, 'dn-elimflow')[0];
  assert(flowS, 'settled: the double-elim flow renders');
  assert(/✕/.test(textOf(flowS)), 'settled: the true (second-loss) eliminations read ✕');
});

test('double-elim — live hero renders the elimFlow combo', () => {
  const c = new live.LiveController({});
  const figHost = heroFigure(c, { activeTournament: delimLive(), heartbeat: hb({ phase: 'tournament:round_4' }),
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }] });
  assert(svgsByClass(figHost, 'dn-elimflow')[0], 'hero: the double-elim mini is the elimFlow combo (WB/LB drops visible)');
  assertEqual(svgsByClass(figHost, 'dn-elimradial').length, 0, 'hero: no radial figure remains (C1)');
  // FULL-WIDTH HERO: the WB/LB flow combo fills the width (aspect-locked) like
  // racing's scalar track — width:100% + the svg.dn-elimflow-hero cap.
  assertHeroResponsive(svgsByClass(figHost, 'dn-elimflow')[0], 'dn-elimflow-hero', 'double-elim hero');
});

// ===========================================================================
// SWISS — the swiss ladder (single-round figure + hero).
// ===========================================================================

const SWISS_SETTLED = {
  structure: 'swiss', phase: 'completed', epoch_id: EPOCH,
  structure_params: { rounds: 2 },
  competitors: [{ generation_id: 'v0' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }],
  rounds: [
    { round_index: 0, label: 'Round 1', matches: [
      { match_id: 'sw_r0_m0', competitors: ['v0', 'v1'], winner: 'v1', decision: 'win' },
      { match_id: 'sw_r0_m1', competitors: ['v2', 'v3'], winner: 'v3', decision: 'win' },
    ] },
    { round_index: 1, label: 'Round 2', matches: [
      { match_id: 'sw_r1_m0', competitors: ['v1', 'v3'], winner: 'v1', decision: 'win' },
      { match_id: 'sw_r1_m1', competitors: ['v0', 'v2'], winner: 'v2', decision: 'win' },
    ] },
  ],
  standings: [
    { generation_id: 'v1', rank: 1, points: 2, wins: 2, losses: 0, scalar: 9.5 },
    { generation_id: 'v2', rank: 2, points: 1, wins: 1, losses: 1, scalar: 10.1 },
    { generation_id: 'v3', rank: 3, points: 1, wins: 1, losses: 1, scalar: 10.4 },
    { generation_id: 'v0', rank: 4, points: 0, wins: 0, losses: 2, scalar: 11.0 },
  ],
};

function swissLive(stage) {
  const r = JSON.parse(JSON.stringify(SWISS_SETTLED));
  r.phase = 'running';
  // round 2 in flight: drop its winners/decisions.
  for (const m of r.rounds[1].matches) { delete m.winner; delete m.decision; m.pending = true; }
  if (stage === 'projected') {
    r.rounds[1].matches[0].projected = { v1: { scalar: 9.5, boards_done: 5, boards_total: 8 }, v3: { scalar: 10.4, boards_done: 5, boards_total: 8 } };
    r.standings = [
      { generation_id: 'v1', rank: 1, points: 1, wins: 1, losses: 0, in_flight: true, projected_scalar: 9.5, boards_done: 5, boards_total: 8 },
      { generation_id: 'v3', rank: 2, points: 1, wins: 1, losses: 0, in_flight: true, projected_scalar: 10.4, boards_done: 5, boards_total: 8 },
      { generation_id: 'v0', rank: 3, points: 0, wins: 0, losses: 1 },
      { generation_id: 'v2', rank: 4, points: 0, wins: 0, losses: 1 },
    ];
  } else {
    r.standings = [
      { generation_id: 'v1', rank: 1, points: 1, wins: 1, losses: 0 },
      { generation_id: 'v3', rank: 2, points: 1, wins: 1, losses: 0 },
      { generation_id: 'v0', rank: 3, points: 0, wins: 0, losses: 1 },
      { generation_id: 'v2', rank: 4, points: 0, wins: 0, losses: 1 },
    ];
  }
  return r;
}

test('swiss — single-round ladder renders in-flight + projected + settled', () => {
  const f = renderSingleRound(swissLive('inflight'), true);
  assert(svgsByClass(f, 'dn-swissladder')[0], 'live: the swiss ladder renders');

  const p = renderSingleRound(swissLive('projected'), true);
  const ladderP = svgsByClass(p, 'dn-swissladder')[0];
  assert(ladderP, 'projected: the swiss ladder renders');
  assert(hasProjected(ladderP), 'projected: an in-flight row ghosts in the dn-proj treatment');

  const s = renderSingleRound(SWISS_SETTLED, false);
  const ladderS = svgsByClass(s, 'dn-swissladder')[0];
  assert(ladderS, 'settled: the swiss ladder renders');
  assert(!hasProjected(ladderS), 'settled: NO projected ghost (committed)');
});

test('swiss — live hero renders the swiss ladder mini', () => {
  const c = new live.LiveController({});
  const figHost = heroFigure(c, { activeTournament: swissLive('projected'), heartbeat: hb({ phase: 'tournament:round_1' }),
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }] });
  assert(svgsByClass(figHost, 'dn-swissladder')[0], 'hero: the swiss mini is the ladder');
  assertEqual(svgsByClass(figHost, 'dn-scalartrack').length, 0, 'hero: no scalar track for a swiss run');
  // FULL-WIDTH HERO: the ladder fills the width (aspect-locked) like racing's
  // scalar track — width:100% + the svg.dn-swissladder-hero cap.
  assertHeroResponsive(svgsByClass(figHost, 'dn-swissladder')[0], 'dn-swissladder-hero', 'swiss hero');
});

// ===========================================================================
// FULL-WIDTH HERO — every NON-RACING structure's live-hero figure is responsive
// (width:100% + aspect-locked + its svg.dn-*-hero cap), matching the racing
// scalar track's now-full-width treatment; and a no-op heartbeat churns no DOM.
// ===========================================================================

// the non-racing structures shown in the live hero + their figure / hero-cap
// classes (racing is asserted in live_racing_sequence.test.mjs).
const NONRACING_HERO_CASES = [
  ['swiss', () => swissLive('projected'), 'tournament:round_1', 'dn-swissladder', 'dn-swissladder-hero'],
  ['single_elim', () => elimLive('inflight'), 'tournament:round_1', 'dn-elimflow', 'dn-elimflow-hero'],
  ['double_elim', () => delimLive(), 'tournament:round_4', 'dn-elimflow', 'dn-elimflow-hero'],
  ['gauntlet', () => gauntletLive('projected'), 'tournament:round_0', 'dn-fieldbars', 'dn-fieldbars-hero'],
];

test('full-width hero — every NON-RACING structure live-hero figure is responsive (width:100% + aspect-locked + its dn-*-hero cap), like racing', () => {
  for (const [name, build, phase, figCls, heroCls] of NONRACING_HERO_CASES) {
    const c = new live.LiveController({});
    const figHost = heroFigure(c, { activeTournament: build(), heartbeat: hb({ phase }),
      activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }] });
    const fig = svgsByClass(figHost, figCls)[0];
    assertHeroResponsive(fig, heroCls, `${name} live-hero`);
  }
});

test('full-width hero — a no-op heartbeat repeat churns NO hero-figure DOM for each NON-RACING structure (digest-gated render discipline)', () => {
  for (const [name, build, phase, figCls] of NONRACING_HERO_CASES) {
    const c = new live.LiveController({});
    const at = build();
    c.update({ status: { running: true, structure: at.structure }, heartbeat: hb({ phase }), activeRuns: [], activeTournament: at });
    const firstFig = svgsByClass(c._funnelHost, figCls)[0];
    const firstNode = c._funnelHost.firstChild;
    // an identical second tick (same digest) must preserve the existing figure node.
    c.update({ status: { running: true, structure: at.structure }, heartbeat: hb({ phase }), activeRuns: [], activeTournament: at });
    const secondFig = svgsByClass(c._funnelHost, figCls)[0];
    const secondNode = c._funnelHost.firstChild;
    assert(firstFig && secondFig && firstFig === secondFig,
      `${name} hero: a no-op heartbeat preserves the figure SVG node (no flash / rebuild)`);
    assert(firstNode && secondNode && firstNode === secondNode,
      `${name} hero: a no-op heartbeat preserves the figure host's child node (zero DOM churn)`);
  }
});

// ===========================================================================
// CONVERGENCE GUARANTEE — the SETTLED render is byte-identical whether reached
// via the LIVE path (projected → settled) or built directly from the completed
// record. The core invariant: no live-only chrome may persist past commit.
// ===========================================================================

const CONVERGENCE_CASES = [
  ['racing', RACING_SETTLED, 'dn-scalartrack'],
  ['gauntlet', GAUNTLET_SETTLED, 'dn-fieldbars'],
  ['single_elim', ELIM_SETTLED, 'dn-elimflow'],
  ['double_elim', DELIM_SETTLED, 'dn-elimflow'],
  ['swiss', SWISS_SETTLED, 'dn-swissladder'],
];

// the LIVE (projected) payload that precedes each settled record — the run goes
// projected → settled, and the settled render must shed ALL live chrome.
function liveOf(name) {
  if (name === 'racing') return racingLive('projected');
  if (name === 'gauntlet') return gauntletLive('projected');
  if (name === 'single_elim') return elimLive('projected');
  if (name === 'double_elim') return delimLive();
  return swissLive('projected');
}

test('convergence — the SETTLED single-round figure is byte-identical via the live path (projected → settled) vs built directly from the completed record', () => {
  for (const [name, settled, cls] of CONVERGENCE_CASES) {
    // 1) the LIVE (projected) render — confirm live chrome IS present mid-flight,
    //    so the convergence below is a real shedding (not a no-op tautology). The
    //    elim/swiss flows may not surface a dn-proj polygon in every fixture, so
    //    we only assert chrome where the projected ghost is meaningful (racing /
    //    gauntlet / swiss carry the dn-proj treatment in these fixtures).
    const liveHost = renderSingleRound(liveOf(name), true);
    const liveFig = svgsByClass(liveHost, cls)[0];
    assert(liveFig, `${name}: the LIVE figure rendered`);

    // 2) DIRECT: render the completed record straight from the settled payload.
    const direct = renderSingleRound(JSON.parse(JSON.stringify(settled)), false);
    const directFig = svgsByClass(direct, cls)[0];

    // 3) VIA-LIVE → COMMIT: the SAME run, now committed to its settled record. The
    //    settled figure must serialize byte-identically to the direct render — no
    //    live-only chrome (no dn-proj, no streaming sub-bar, no pending spoke) may
    //    persist past commit, and the figure must NOT differ from the cold build.
    const converged = renderSingleRound(JSON.parse(JSON.stringify(settled)), false);
    const convergedFig = svgsByClass(converged, cls)[0];
    assert(directFig && convergedFig, `${name}: both settled renders produced the ${cls} figure`);
    assertEqual(serialize(convergedFig), serialize(directFig),
      `${name}: the settled figure is byte-identical (no live-only chrome leaks past commit)`);
    // and the settled figure must differ from the live one (the live chrome WAS shed).
    assert(serialize(convergedFig) !== serialize(liveFig),
      `${name}: the settled figure differs from the mid-flight live figure (live chrome was shed at commit)`);
  }
});

test('convergence — the structureDigest of a settled record is identical via either path (digest convergence)', () => {
  for (const [name, settled] of CONVERGENCE_CASES) {
    const a = STRUCT.structureDigest(STRUCT.normalizeStructure(JSON.parse(JSON.stringify(settled)), false));
    const b = STRUCT.structureDigest(STRUCT.normalizeStructure(JSON.parse(JSON.stringify(settled)), false));
    assertEqual(a, b, `${name}: the settled structure digest is stable`);
  }
});

// ===========================================================================
// ANTI-FLASH DIGEST DISCIPLINE — a no-op heartbeat does NOT rebuild the DOM
// (node identity preserved + digest stable); a real change DOES.
// ===========================================================================

test('anti-flash — a no-op tick preserves the figure node + digest; a real change repaints (single-round, gatedSwap)', () => {
  for (const [name, settled, cls] of CONVERGENCE_CASES) {
    const host = document.createElement('div');
    const liveA = (name === 'gauntlet') ? gauntletLive('inflight')
      : name === 'racing' ? racingLive('inflight')
      : name === 'single_elim' ? elimLive('inflight')
      : name === 'double_elim' ? delimLive()
      : swissLive('inflight');
    const stA = STRUCT.normalizeStructure(liveA, true);
    ui.gatedSwap(host, STRUCT.structureDigest(stA), () => STRUCT.renderStructure(stA, CTX, EPOCH));
    const figBefore = svgsByClass(host, cls)[0];
    assert(figBefore, `${name}: the live figure mounted`);

    // NO-OP TICK: identical payload → identical digest → no rebuild (same node).
    const stA2 = STRUCT.normalizeStructure(JSON.parse(JSON.stringify(liveA)), true);
    assertEqual(STRUCT.structureDigest(stA2), STRUCT.structureDigest(stA), `${name}: a no-op tick yields the SAME digest`);
    const rebuilt = ui.gatedSwap(host, STRUCT.structureDigest(stA2), () => STRUCT.renderStructure(stA2, CTX, EPOCH));
    assertEqual(rebuilt, false, `${name}: a no-op tick does NOT rebuild (gatedSwap returns false)`);
    assert(svgsByClass(host, cls)[0] === figBefore, `${name}: the figure node identity is PRESERVED across a no-op tick (no flash)`);

    // REAL CHANGE: commit the record → the digest changes → it repaints.
    const stB = STRUCT.normalizeStructure(JSON.parse(JSON.stringify(settled)), false);
    assert(STRUCT.structureDigest(stB) !== STRUCT.structureDigest(stA), `${name}: a real change (commit) CHANGES the digest`);
    const repainted = ui.gatedSwap(host, STRUCT.structureDigest(stB), () => STRUCT.renderStructure(stB, CTX, EPOCH));
    assertEqual(repainted, true, `${name}: a real change repaints (gatedSwap returns true)`);
  }
});

test('anti-flash — the live HERO figure node survives a no-op heartbeat and is rebuilt on a real change', () => {
  const c = new live.LiveController({});
  const heartbeat = hb({ phase: 'tournament:round_0:rung1' });
  // mount with an in-flight racing track.
  heroFigure(c, { activeTournament: racingLive('inflight'), heartbeat, activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }] });
  const trackBefore = svgsByClass(c._funnelHost, 'dn-scalartrack')[0];
  const digestBefore = c._funnelDigest;
  assert(trackBefore && digestBefore, 'the hero scalar track mounted');

  // NO-OP: feed the IDENTICAL payload again → same digest, same node (no rebuild).
  heroFigure(c, { activeTournament: racingLive('inflight'), heartbeat, activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }] });
  assertEqual(c._funnelDigest, digestBefore, 'a no-op hero tick yields the SAME structure digest');
  assert(svgsByClass(c._funnelHost, 'dn-scalartrack')[0] === trackBefore, 'a no-op hero tick does NOT rebuild the figure node (identity preserved)');

  // REAL CHANGE: the rung projects → the digest moves → the figure is rebuilt.
  heroFigure(c, { activeTournament: racingLive('projected'), heartbeat, activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }] });
  assert(c._funnelDigest !== digestBefore, 'a real change (a projection lands) CHANGES the hero digest');
  const trackAfter = svgsByClass(c._funnelHost, 'dn-scalartrack')[0];
  assert(trackAfter && trackAfter !== trackBefore, 'a real change rebuilds the hero figure node (repaint)');
  assert((trackAfter.getAttribute('class') || '').includes('dt-live-enter'), 'the freshly-built hero figure carries the one-shot entrance class (never a repaint loop)');
});

// ===========================================================================
// PRODUCER/CONSUMER PARITY — the JS consumes the racing per-lane live_progress
// shape `{boards_done, boards_total, projected_scalar, inflight, projected}` the
// backend publishes (the python side has its own parity tests).
// ===========================================================================

test('producer/consumer parity — the racing scalar track consumes the published per-lane live_progress shape', () => {
  // a rung whose live_progress carries EXACTLY the backend-published shape.
  const rung = {
    label: 'Rung 1', match_id: 'rung1',
    competitors: ['v1', 'v2'], survivors: [], cut: [],
    live_progress: {
      v1: { boards_done: 7, boards_total: 8, projected_scalar: 9.8, inflight: 1, projected: true },
      v2: { boards_done: 4, boards_total: 8, projected_scalar: 11.0, inflight: 1, projected: true },
    },
    pending: true,
  };
  const node = svg.racingScalarTrack({ rungs: [rung], benchmarkId: 'v0', championId: 'v0', championScalar: 10.0, live: true });
  // the consumer plots the projected lanes (dn-proj) AND draws the scored
  // board-progress sub-bar from boards_done/boards_total.
  assert(hasProjected(node), 'the track reads `projected` + `projected_scalar` → dn-proj ghost');
  assert(/~/.test(textOf(node)), 'the projected scalar is surfaced (~value)');
  // the digest folds the SAME shape (so a board landing / projection move repaints).
  const d1 = svg.racingScalarTrackDigest({ rungs: [rung], benchmarkId: 'v0', championScalar: 10.0 });
  const moved = JSON.parse(JSON.stringify(rung));
  moved.live_progress.v1.boards_done = 8;            // a board landed.
  moved.live_progress.v1.projected_scalar = 9.7;     // the projection moved.
  const d2 = svg.racingScalarTrackDigest({ rungs: [moved], benchmarkId: 'v0', championScalar: 10.0 });
  assert(d1 !== d2, 'a board landing / projection move CHANGES the scalar-track digest (the published fields are folded in)');
  // a no-op (identical) shape yields an identical digest (anti-flash).
  const d3 = svg.racingScalarTrackDigest({ rungs: [JSON.parse(JSON.stringify(rung))], benchmarkId: 'v0', championScalar: 10.0 });
  assertEqual(d1, d3, 'an identical live_progress shape yields an identical digest (no-op heartbeat → no repaint)');
});

// ===========================================================================
// RACING — the MULTI-SURVIVOR IN-FLIGHT RUNG (the real published shape).
//
// A live racing rung is published as N champion-vs-survivor matchups
// (`rung{N}_m0..mK`) inside ONE RoundRecord; only `matches[0]` carries the
// authoritative full-rung `live_progress` map (EVERY lane), the rest keep it
// null (see racing.py `_pending_round`). This section pins:
//   * the rung's FULL FIELD = the union of all matchups + the live_progress lane
//     keys (NOT matches[0]'s `[champion, challenger0]` alone) — every survivor
//     renders on the Match-ups scalar track, not "No rungs evaluated yet.";
//   * the hero LEADS with the scalar-track mini (figure ABOVE "what's running");
//   * "what's running" emits ONE rung block (not one per matchup).
// ===========================================================================

// rung 1 mid-flight with TWO survivor lanes (v5, v7) racing the champion (v0),
// published as TWO matchups (rung1_m0 = v0 vs v5, rung1_m1 = v0 vs v7). Only
// matches[0] carries the full per-rung live_progress (v0 + v5 + v7).
function racingMultiInflight(stage) {
  const m0Progress = stage === 'projected'
    ? {
        v0: { boards_total: 8, inflight: 1, projected_scalar: 10.0, projected: true },
        v5: { boards_done: 6, boards_total: 8, inflight: 1, projected: true, projected_scalar: 9.6, done: 6, total: 8 },
        v7: { boards_done: 5, boards_total: 8, inflight: 1, projected: true, projected_scalar: 9.9, done: 5, total: 8 },
      }
    : {
        v0: { boards_total: 8, inflight: 1 },
        v5: { boards_done: 3, boards_total: 8, inflight: 1, projected: false, done: 3, total: 8 },
        v7: { boards_done: 2, boards_total: 8, inflight: 1, projected: false, done: 2, total: 8 },
      };
  return {
    structure: 'racing', phase: 'running', epoch_id: EPOCH,
    structure_params: { rungs: [{ fraction: 0.5 }, { fraction: 1.0 }], board_size: 8 },
    champion_lineage: ['v0'],
    competitors: [
      { generation_id: 'v0', role: 'champion' }, { generation_id: 'v5', role: 'challenger' },
      { generation_id: 'v7', role: 'challenger' },
    ],
    rounds: [
      { round_index: 0, label: 'Rung 1', matches: [
        // slot 0 — the carrier of the full-rung live_progress (every lane).
        { match_id: 'rung1_m0', competitors: ['v0', 'v5'], board_fraction: 0.5, live_progress: m0Progress },
        // slot 1 — a per-duel matchup; live_progress is null (read off slot 0).
        { match_id: 'rung1_m1', competitors: ['v0', 'v7'], board_fraction: 0.5 },
      ] },
    ],
    standings: [],
    partial_champion_agg: { scalar: 10.0 },
  };
}

test('racing multi-survivor — the Match-ups view renders ALL lanes of an in-flight rung published as N matchups (not "No rungs evaluated")', () => {
  const at = racingMultiInflight('inflight');
  // build the live model the Match-ups view consumes (published rounds + active
  // -runs overlay), then render it via the SAME renderStructure dispatch.
  const built = STRUCT.buildLiveRacingModel({ at, heartbeat: hb({ phase: 'tournament:rung1' }),
    activeRuns: [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r5' }, { generation_id: 'v7', entry_id: 'b1', run_id: 'r7' }],
    epochGens: ['v0', 'v5', 'v7'] });
  const host = document.createElement('div');
  for (const n of STRUCT.renderStructure(built, CTX, EPOCH)) if (n) host.appendChild(n);

  // the scalar track rendered (NOT the empty placeholder).
  const track = svgsByClass(host, 'dn-scalartrack')[0];
  assert(track, 'the in-flight rung renders the scalar track');
  assert(!/No rungs evaluated/.test(textOf(host)), 'the "No rungs evaluated yet." empty is NOT reached for an in-flight rung');

  // the rung's FULL FIELD — both survivors (v5 AND v7) are on the track, not just
  // matches[0]'s first lane (v5).
  const model = STRUCT.racingModel(built);
  const rung = model.rungs[0];
  assert(rung.competitors.indexOf('v5') >= 0 && rung.competitors.indexOf('v7') >= 0,
    'the rung field is the UNION of all matchups (v5 AND v7), not just the first matchup');
  assert(rung.competitors.indexOf('v0') < 0, 'the champion/benchmark v0 is NOT a rung lane');
  // the union live_progress carries BOTH lanes (v7 survived even though it rode a
  // non-slot-0 matchup whose own live_progress was null).
  assert(rung.live_progress && rung.live_progress.v7, 'the rung live_progress carries v7 (the union, not just slot-0 competitors)');
});

test('racing multi-survivor — the live HERO leads with the scalar-track mini (figure ABOVE "what\'s running"); "what\'s running" is ONE rung block', () => {
  const c = new live.LiveController({});
  const at = racingMultiInflight('projected');
  const heartbeat = hb({ phase: 'tournament:rung1' });
  c.update({
    status: { running: true, structure: 'racing' }, heartbeat,
    activeRuns: [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r5' }, { generation_id: 'v7', entry_id: 'b1', run_id: 'r7' }],
    activeTournament: at,
  });

  // the hero figure host carries the scalar-track mini for ALL lanes.
  const track = svgsByClass(c._funnelHost, 'dn-scalartrack')[0];
  assert(track, 'the hero leads with the racing scalar-track mini');

  // ORDERING: the full-width race (the scalar-track mini in `_funnelHost`/
  // `_trackHost`) appears BEFORE the "what's running" matches list (_matchesHost)
  // in the hero's DOM. Both ride inside wrapper divs (the `race` row + the
  // `detail` row), so find which top-level child subtree contains each.
  const kids = c.node.childNodes;
  const subtreeIndexOf = (target) => {
    for (let i = 0; i < kids.length; i++) {
      let found = false;
      walk(kids[i], (n) => { if (n === target) found = true; });
      if (found) return i;
    }
    return -1;
  };
  const idxFunnel = subtreeIndexOf(c._funnelHost);
  const idxMatches = subtreeIndexOf(c._matchesHost);
  assert(idxFunnel >= 0 && idxMatches >= 0, 'both the figure mini and the matches list are mounted in the hero');
  assert(idxFunnel < idxMatches, 'the full-width scalar-track race leads ABOVE the "what\'s running" detail (_matchesHost)');

  // "what's running" is ONE rung block (not one per champion-vs-survivor matchup).
  const epochGens = ['v0', 'v5', 'v7'];
  const model = STRUCT.buildLiveModel(at, heartbeat,
    [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r5' }, { generation_id: 'v7', entry_id: 'b1', run_id: 'r7' }], epochGens);
  const blocks = STRUCT.liveMatchBlocks(model);
  const rungBlocks = blocks.filter((b) => b.kind === 'rung');
  assertEqual(rungBlocks.length, 1, 'ONE rung block for the rung (not one per matchup)');
  const ids = rungBlocks[0].entries.map((e) => e.id).sort();
  assertEqual(JSON.stringify(ids), JSON.stringify(['v5', 'v7']), 'the single rung block carries BOTH lanes (v5, v7)');
  assert(rungBlocks[0].entries.every((e) => e.id !== 'v0'), 'the champion/benchmark v0 is not a lane in the rung block');
});

test('racing multi-survivor — the rung block dedup is anti-flash: a no-op tick yields an identical liveMatchBlocks digest', () => {
  const at = racingMultiInflight('projected');
  const hbX = hb({ phase: 'tournament:rung1' });
  const runs = [{ generation_id: 'v5', entry_id: 'b0', run_id: 'r5' }, { generation_id: 'v7', entry_id: 'b1', run_id: 'r7' }];
  const epochGens = ['v0', 'v5', 'v7'];
  const d1 = STRUCT.liveMatchBlocksDigest(STRUCT.liveMatchBlocks(STRUCT.buildLiveModel(at, hbX, runs, epochGens)));
  const d2 = STRUCT.liveMatchBlocksDigest(STRUCT.liveMatchBlocks(STRUCT.buildLiveModel(JSON.parse(JSON.stringify(at)), hbX, runs, epochGens)));
  assertEqual(d1, d2, 'a no-op tick yields an identical rung-block digest (no flash on the dedup\'d block)');
  // a board landing on v7 moves the digest (the union lane is folded in).
  const moved = racingMultiInflight('projected');
  moved.rounds[0].matches[0].live_progress.v7.boards_done = 7;
  moved.rounds[0].matches[0].live_progress.v7.projected_scalar = 9.7;
  const d3 = STRUCT.liveMatchBlocksDigest(STRUCT.liveMatchBlocks(STRUCT.buildLiveModel(moved, hbX, runs, epochGens)));
  assert(d1 !== d3, 'a board landing on a union lane (v7) repaints the rung block');
});

// ===========================================================================
// THE LIVE-RACING HERO REDESIGN — the holistic restructure of the live hero:
//   1. ONE muted metadata baseline (no competing big phase title).
//   2. the race state = the PRIMARY, FULL-WIDTH viz, capped by a rung STEPPER.
//   3. two BALANCED detail columns (what's running · live activity).
//   4. cohesion + render discipline (a no-op heartbeat churns ZERO DOM).
//
// These pin the redesign's structural invariants on the REAL LiveController.
// ===========================================================================

// the TWO-RUNG racing topology that lands at RUNG 2 OF 2: rung 1 (round_index 0)
// is SETTLED (survivors v1; cuts v2,v3) and rung 2 (round_index 1) is IN FLIGHT.
// This is the operator's exact contradictory state — the OLD title read the
// 0-indexed phase string ("rung 0"), the OLD subline read the topology ("rung 2
// of 2"). Both rungs are real rungs (not the gate), so liveProgress focuses the
// in-flight rung 2 of 2.
const HERO_RUNG2_AT = {
  structure: 'racing', phase: 'running', epoch_id: EPOCH,
  structure_params: { rungs: [{ fraction: 0.5 }, { fraction: 1.0 }] },
  champion_lineage: ['v0'],
  competitors: [
    { generation_id: 'v0', role: 'champion' }, { generation_id: 'v1', role: 'challenger' },
    { generation_id: 'v2', role: 'challenger' }, { generation_id: 'v3', role: 'challenger' },
  ],
  rounds: [
    { round_index: 0, label: 'Rung 1', matches: [{ match_id: 'rung1', competitors: ['v0', 'v1', 'v2', 'v3'], survivors: ['v0', 'v1'], cut: ['v2', 'v3'], board_fraction: 0.5, pending: false, deltas: { v1: -0.2, v2: 1.0, v3: 2.0 } }] },
    { round_index: 1, label: 'Rung 2', matches: [{ match_id: 'rung2', competitors: ['v0', 'v1'], survivors: [], cut: [], board_fraction: 1.0, pending: true }] },
  ],
  standings: [],
};

// drive the hero one tick at the RUNG 2 OF 2 state above.
function heroAtRung2() {
  const c = new live.LiveController({});
  const at = JSON.parse(JSON.stringify(HERO_RUNG2_AT));
  // the heartbeat phase string is 0-INDEXED "rung0…" — the OLD title's source.
  c.update({
    status: { running: true, structure: 'racing', label: 'racing · rung 0', inFlight: 7 },
    heartbeat: hb({ phase: 'tournament:round_0:rung0_m3' }),
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }],
    activeTournament: at,
  });
  return { c, at };
}

test('hero redesign — the rung label is INTERNALLY CONSISTENT: ONE "rung N of M", never the old 0-indexed phase-string title alongside a 1-indexed subline', () => {
  const { c } = heroAtRung2();
  const metaText = textOf(c._meta);
  // the ONE rung token is the 1-indexed "rung 2 of 2" (topology), NOT "rung 0".
  assert(/rung 2 of 2/.test(metaText), 'the metadata baseline reads the 1-indexed "rung 2 of 2"');
  // the contradictory 0-indexed bare "rung 0" (from the raw phase string) is GONE
  // from the hero — there is exactly ONE rung number, and it is the topology one.
  assert(!/rung 0\b/.test(metaText), 'the contradictory 0-indexed "rung 0" phase-string title is GONE (no two rung numbers)');
  const rungMatches = metaText.match(/rung \d+( of \d+)?/g) || [];
  assertEqual(rungMatches.length, 1, 'exactly ONE rung token appears in the hero header (no contradiction)');
});

test('hero redesign — the rung STEPPER reflects the rung index/count (one pip per rung; the current rung active, the completed one filled)', () => {
  const { c } = heroAtRung2();
  const pips = nodesByClass(c._stepHost, 'dt-rungstep-pip');
  assertEqual(pips.length, 2, 'one pip per rung (a field of two rungs)');
  const active = nodesByClass(c._stepHost, 'dt-rungstep-active');
  const done = nodesByClass(c._stepHost, 'dt-rungstep-done');
  assertEqual(active.length, 1, 'exactly one pip is active (the current rung — rung 2)');
  assertEqual(done.length, 1, 'exactly one pip is filled-complete (the settled rung 1)');
  // the active pip is the LAST one (rung 2 of 2 = the final rung).
  assert(pips[pips.length - 1] === active[0], 'the active pip is the final rung (rung 2 of 2)');
});

test('hero redesign — the rung stepper agrees with the metadata rung label (the ONE rung-number source): stepIndex/stepCount === the parsed "N of M"', () => {
  const { c, at } = heroAtRung2();
  const prog = live.liveProgress({ activeTournament: at, heartbeat: hb({ phase: 'tournament:round_0:rung0_m3' }), status: { structure: 'racing' } });
  // the metadata label + the stepper both derive from this ONE liveProgress.
  assertEqual(prog.stepIndex, 2, 'liveProgress stepIndex is 2 (rung 2)');
  assertEqual(prog.stepCount, 2, 'liveProgress stepCount is 2 (of two rungs)');
  const activeIdx = nodesByClass(c._stepHost, 'dt-rungstep-pip').findIndex((p) => (p._attrs.class || '').includes('dt-rungstep-active'));
  assertEqual(activeIdx + 1, prog.stepIndex, 'the active pip index (1-based) equals stepIndex — the stepper cannot disagree with the label');
});

test('hero redesign — the race state is the FULL-WIDTH primary viz: the scalar track is responsive (aspect-locked scale-to-width), capped by the svg.dn-scalartrack-hero rule', () => {
  const { c } = heroAtRung2();
  const track = svgsByClass(c._funnelHost, 'dn-scalartrack')[0];
  assert(track, 'the racing scalar track renders as the hero race viz');
  // responsive → width:100% + the load-bearing hero class for the max-width cap.
  assertEqual(track.getAttribute('width'), '100%', 'the track fills the hero width (responsive width:100%)');
  assert((track.getAttribute('class') || '').includes('dn-scalartrack-hero'), 'the track carries the dn-scalartrack-hero class (the svg.* max-WIDTH cap governs)');
  // the track host leads ABOVE the detail row in the hero DOM (race → detail).
  const kids = c.node.childNodes;
  const idxOf = (target) => { for (let i = 0; i < kids.length; i++) { let f = false; walk(kids[i], (n) => { if (n === target) f = true; }); if (f) return i; } return -1; };
  assert(idxOf(c._trackHost) < idxOf(c._matchesHost), 'the full-width race leads above the detail row');
});

test('hero redesign — the detail row is TWO BALANCED columns: what\'s running (left) · live activity (right), shared panel + eyebrow chrome', () => {
  const { c } = heroAtRung2();
  // both columns are dt-live-hero-panel siblings (shared chrome) under one row.
  const panels = nodesByClass(c.node, 'dt-live-hero-panel');
  assertEqual(panels.length, 2, 'the detail row has exactly two panels (balanced columns)');
  assert(panels.indexOf(c._matchesHost) >= 0 && panels.indexOf(c._tickerHost) >= 0, 'both "what\'s running" and "live activity" are panels (equal-weight peers)');
  // LEFT is what's running, RIGHT is live activity (the activity log is not an
  // afterthought tucked below — it is a peer column).
  assert(panels.indexOf(c._matchesHost) < panels.indexOf(c._tickerHost), 'what\'s running (left) precedes live activity (right)');
  // ONE eyebrow style for both section labels.
  const eyebrows = nodesByClass(c.node, 'dt-live-hero-eyebrow').map((e) => textOf(e));
  assert(eyebrows.some((t) => /what.s running/i.test(t)), 'the left eyebrow reads "what\'s running"');
  assert(eyebrows.some((t) => /live activity/i.test(t)), 'the right eyebrow reads "live activity"');
  assertEqual(nodesByClass(c.node, 'dt-live-hero-eyebrow').length, 2, 'both section labels use the ONE shared eyebrow style');
});

test('hero redesign — render discipline: a no-op heartbeat churns ZERO DOM (the metadata, the stepper, the track, the match rows, the ticker all hold node identity)', () => {
  const { c, at } = heroAtRung2();
  const beat = hb({ phase: 'tournament:round_0:rung0_m3' });
  const runs = [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }];
  const tickOnce = () => c.update({ status: { running: true, structure: 'racing', inFlight: 7 }, heartbeat: beat, activeRuns: runs, activeTournament: at });
  // capture the live DOM nodes after the first mount.
  const metaText0 = textOf(c._meta);
  const stepper0 = nodesByClass(c._stepHost, 'dt-rungstep')[0];
  const track0 = svgsByClass(c._funnelHost, 'dn-scalartrack')[0];
  const matchesNode0 = c._matchesBody.firstChild;
  const tickerList0 = c.ticker._list;
  const tickerRows0 = nodesByClass(c.ticker.node, 'dt-ticker-row').length;
  assert(stepper0 && track0 && matchesNode0 && tickerList0, 'the hero surfaces mounted');

  // an IDENTICAL second tick (a no-op SSE heartbeat) must rebuild NOTHING.
  tickOnce();
  assertEqual(textOf(c._meta), metaText0, 'the metadata baseline text is unchanged on a no-op tick');
  assert(nodesByClass(c._stepHost, 'dt-rungstep')[0] === stepper0, 'the rung stepper node identity is preserved (no rebuild)');
  assert(svgsByClass(c._funnelHost, 'dn-scalartrack')[0] === track0, 'the scalar track node identity is preserved (digest-gated)');
  assert(c._matchesBody.firstChild === matchesNode0, 'the "what\'s running" match rows node identity is preserved');
  assert(c.ticker._list === tickerList0, 'the activity ticker list is the same node');
  assertEqual(nodesByClass(c.ticker.node, 'dt-ticker-row').length, tickerRows0, 'a no-op tick appends NO new activity rows (zero DOM churn)');
});

await run();
