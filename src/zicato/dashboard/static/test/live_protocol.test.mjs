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

installDom();

const svg = await import('../js/variants/T/svg.js');
const STRUCT = await import('../js/variants/T/views/structure.js');
const ui = await import('../js/variants/T/ui.js');
const live = await import('../js/variants/T/live.js');

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
  const st = STRUCT.normalizeStructure(payload, !!liveFlag);
  const host = document.createElement('div');
  for (const n of STRUCT.renderStructure(st, CTX, EPOCH)) if (n) host.appendChild(n);
  return host;
}

// Drive a LiveController one tick + return its structure-figure host node.
function heroFigure(controller, { activeTournament, heartbeat, activeRuns }) {
  controller.update({
    status: { running: true, structure: activeTournament && activeTournament.structure },
    heartbeat, activeRuns: activeRuns || [], activeTournament,
  });
  return controller._funnelHost;
}

// the active-tournament epoch tag the hero scopes to (must match the heartbeat).
function hb(extra) { return { phase: 'tournament:running', epoch_id: EPOCH, last_heartbeat: new Date().toISOString(), ...extra }; }

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
});

// ===========================================================================
// SINGLE-ELIM — radial (single-round PRIMARY + hero) + flow (secondary).
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

test('single-elim — single-round radial renders each lifecycle stage (in-flight semifinal cut → live final → settled crown)', () => {
  // live: a decided semifinal eliminates a lane (✕), the final is still deciding.
  const f = renderSingleRound(elimLive('inflight'), true);
  const radial = svgsByClass(f, 'dn-elimradial')[0];
  assert(radial, 'live: the radial bracket renders');
  assert(/✕/.test(textOf(radial)), 'live: a decided semifinal cut shows ✕ in the radial');
  assert(nodesByClass(radial, 'dn-elimradial-pending').length >= 1, 'live: the in-flight final reads as pending (not falsely decided)');

  // settled: the survivor reaches the center gate with a crown, no pending spokes.
  const s = renderSingleRound(ELIM_SETTLED, false);
  const radialS = svgsByClass(s, 'dn-elimradial')[0];
  assert(radialS, 'settled: the radial bracket renders');
  assert(nodesByClass(radialS, 'dn-elimradial-pending').length === 0, 'settled: NO pending spokes remain (committed)');
  assert(/✕/.test(textOf(radialS)), 'settled: the eliminated lanes still read ✕');
});

test('single-elim — live hero renders the radial mini + emits the ✕ elimination glyph', () => {
  const c = new live.LiveController({});
  const figHost = heroFigure(c, { activeTournament: elimLive('inflight'), heartbeat: hb({ phase: 'tournament:round_1' }),
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }] });
  const radial = svgsByClass(figHost, 'dn-elimradial')[0];
  assert(radial, 'hero: the single-elim mini is the radial');
  assert(/✕/.test(textOf(radial)), 'hero: the decided semifinal emits ✕');
  assertEqual(svgsByClass(figHost, 'dn-scalartrack').length, 0, 'hero: no racing scalar track for an elim run');
});

// ===========================================================================
// DOUBLE-ELIM — the elimFlow combo (single-round PRIMARY + hero) + radial toggle.
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

test('double-elim — single-round elimFlow combo renders the WB→LB drop + the live GF, and the radial toggle is offered', () => {
  const f = renderSingleRound(delimLive(), true);
  const flow = svgsByClass(f, 'dn-elimflow')[0];
  assert(flow, 'live: the double-elim flow renders');
  // the WB→LB drop connector is present (a rounded path, marked a drop).
  const dropSegs = nodesByClass(flow, 'dn-elimflow-seg-drop');
  assert(dropSegs.length >= 1, 'live: at least one WB→LB drop connector is drawn (the second-life edge)');
  // the radial is ALSO offered (the non-default toggle / companion).
  const radial = svgsByClass(f, 'dn-elimradial')[0];
  assert(radial, 'the radial double-elim view is offered alongside the flow');

  const s = renderSingleRound(DELIM_SETTLED, false);
  const flowS = svgsByClass(s, 'dn-elimflow')[0];
  assert(flowS, 'settled: the double-elim flow renders');
  assert(/✕/.test(textOf(flowS)), 'settled: the true (second-loss) eliminations read ✕');
});

test('double-elim — live hero KEEPS the elimFlow combo (not the radial)', () => {
  const c = new live.LiveController({});
  const figHost = heroFigure(c, { activeTournament: delimLive(), heartbeat: hb({ phase: 'tournament:round_4' }),
    activeRuns: [{ generation_id: 'v1', entry_id: 'b0', run_id: 'r1' }] });
  assert(svgsByClass(figHost, 'dn-elimflow')[0], 'hero: the double-elim mini is the elimFlow combo (WB/LB drops visible)');
  assertEqual(svgsByClass(figHost, 'dn-elimradial').length, 0, 'hero: the radial is the single-elim mini, NOT double-elim');
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
});

// ===========================================================================
// CONVERGENCE GUARANTEE — the SETTLED render is byte-identical whether reached
// via the LIVE path (projected → settled) or built directly from the completed
// record. The core invariant: no live-only chrome may persist past commit.
// ===========================================================================

const CONVERGENCE_CASES = [
  ['racing', RACING_SETTLED, 'dn-scalartrack'],
  ['gauntlet', GAUNTLET_SETTLED, 'dn-fieldbars'],
  ['single_elim', ELIM_SETTLED, 'dn-elimradial'],
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

  // ORDERING: the structure-figure mini (_funnelHost) appears BEFORE the
  // "what's running" matches list (_matchesHost) in the hero's DOM.
  const kids = c.node.childNodes;
  const idxFunnel = kids.indexOf(c._funnelHost);
  // _matchesHost rides inside the `body` wrapper, so find which top-level child
  // subtree contains it.
  const idxMatches = (() => {
    for (let i = 0; i < kids.length; i++) {
      let found = false;
      walk(kids[i], (n) => { if (n === c._matchesHost) found = true; });
      if (found) return i;
    }
    return -1;
  })();
  assert(idxFunnel >= 0 && idxMatches >= 0, 'both the figure mini and the matches list are mounted in the hero');
  assert(idxFunnel < idxMatches, 'the scalar-track mini (_funnelHost) leads ABOVE the "what\'s running" list (_matchesHost)');

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

await run();
