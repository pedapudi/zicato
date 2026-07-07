// test/live_racing_sequence.test.mjs — the FULL multi-rung racing LIFECYCLE as a
// SEQUENCE of active-tournament snapshots, replayed through the REAL frontend.
//
// The prior live-racing tests (live_protocol.test.mjs) exercise a SINGLE
// idealized snapshot per lifecycle stage — one rung-0, never a SEQUENCE of
// settled-rung-0 + active-rung-1 + gate. That idealization hid the two operator
// findings:
//
//   1. FULL RUNG SEQUENCE — the funnel / scalar-track / epoch round-timeline must
//      render EVERY settled prior rung (with its committed survivors/cuts) PLUS
//      the active in-flight rung PLUS the champion-gate, not just the active one.
//   2. CHAMPION BENCHMARK — the live scalar-track champion line must be the REAL
//      champion loss (the strategy-seeded benchmark), never a fabricated default,
//      and must survive an EMPTY `partial_champion_agg` (the operator's case).
//
// This file replays a realistic sequence of the shapes the BACKEND actually
// publishes over time — `stage_index`, the slot-0 carrier `live_progress`, the
// champion lane's strategy-seeded `projected_scalar`, settled survivors/cut — and
// drives the REAL modules (normalizeStructure / buildLiveModel / racingModel /
// renderStructure / LiveController) at each step. The assertions pin: the right
// rungs present (full sequence), each rung's full field, real in-flight positions
// + the real champion benchmark, the hero scalar-track mini, one "what's running"
// block per rung, settled convergence, and anti-flash on no-op repeats.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const STRUCT = await import('../js/views/structure.js');
const svg = await import('../js/svg.js');
const live = await import('../js/live.js');

const EPOCH = '2026-06-07_e9';
const CTX = { navigate() {}, href: () => '#' };
const CHAMP_LOSS = 68.0;   // the REAL champion loss (round-timeline reads ~68-72).

// ---- generic DOM helpers (mirror live_protocol.test.mjs) -------------------
function walk(node, fn) { fn(node); for (const c of (node.childNodes || [])) if (c && c.nodeType === 1) walk(c, fn); }
function svgsByClass(host, cls) {
  const out = [];
  walk(host, (n) => { if (n.localName === 'svg' && (n.getAttribute('class') || '').split(/\s+/).includes(cls)) out.push(n); });
  return out;
}
function nodesByClass(host, cls) {
  const out = [];
  walk(host, (n) => { if (((n.getAttribute && n.getAttribute('class')) || '').split(/\s+/).includes(cls)) out.push(n); });
  return out;
}
function textOf(node) {
  let s = '';
  walk(node, (n) => { for (const c of (n.childNodes || [])) if (c.nodeType === 3) s += c.textContent; });
  return s;
}
function serialize(node) {
  if (!node) return '';
  if (node.nodeType === 3) return '#' + node.textContent;
  const attrs = Object.keys(node._attrs || {}).sort().map((k) => `${k}=${node._attrs[k]}`).join(' ');
  const kids = (node.childNodes || []).map(serialize).join('');
  return `<${node.localName} ${attrs}>${kids}</${node.localName}>`;
}
function hasProjected(node) { return nodesByClass(node, 'dn-proj').length > 0; }

// the heartbeat the hero scopes to (must match the active-tournament epoch).
function hb(phase) { return { phase, epoch_id: EPOCH, ts: Date.now() }; }

// ===========================================================================
// THE REALISTIC PUBLISHED SHAPES — what _publish_active_tournament writes over
// time. The field: champion v0 + four challengers v1..v4, eta=2, board_fraction
// 0.25 (rung 0 = 25% board, rung 1 = 50%). Rung 0 narrows 4 → 2 (survivors v1,v2;
// cut v3,v4); rung 1 races the two survivors; the gate confirms the winner.
//
// Each snapshot mirrors the backend EXACTLY:
//   * settled rungs carry `survivors`/`cut`, `pending:false`, empty live_progress;
//   * the active rung is published as N champion-vs-survivor matchups; ONLY the
//     slot-0 carrier match holds the full per-lane `live_progress` (the rest are
//     empty), and the champion lane carries its strategy-seeded projected_scalar;
//   * the pending gate is a single `racing-final` 1v1 with pending:true;
//   * `stage_index` is the persisted key (normalizeStructure maps → round_index).
// ===========================================================================

const COMPETITORS = [
  { generation_id: 'v0', role: 'champion' },
  { generation_id: 'v1', role: 'challenger' },
  { generation_id: 'v2', role: 'challenger' },
  { generation_id: 'v3', role: 'challenger' },
  { generation_id: 'v4', role: 'challenger' },
];

// a SETTLED rung-0 record (the first 4→2 narrowing) — exactly strategy.rounds()[0].
function rung0Settled() {
  return {
    stage_index: 0, label: 'Rung 0',
    matches: [{
      match_id: 'rung0', competitors: ['v1', 'v2', 'v3', 'v4'],
      winner: null, decision: '', survivors: ['v1', 'v2'], cut: ['v3', 'v4'],
      board_fraction: 0.25, pending: false, live_progress: {},
      deltas: { v1: -2.0, v2: -1.5, v3: 1.0, v4: 2.0 },
    }],
  };
}

// the ACTIVE rung-1 round: N champion-vs-survivor matchups; slot-0 carries the
// full per-lane live_progress (champion + every survivor lane), the rest empty.
// `lp` overrides per-lane fields so a step can grow boards_done / projected.
function rung1Active(lpOverrides) {
  const base = {
    v0: { boards_total: 4, inflight: 1, projected_scalar: CHAMP_LOSS, projected: true },
    v1: { boards_total: 4, inflight: 1 },
    v2: { boards_total: 4, inflight: 1 },
  };
  const lp = {};
  for (const k of Object.keys(base)) lp[k] = Object.assign({}, base[k], (lpOverrides && lpOverrides[k]) || {});
  return {
    stage_index: 1, label: 'Rung 1',
    matches: [
      { match_id: 'rung1_m0', competitors: ['v0', 'v1'], winner: null, decision: '', survivors: [], cut: [], board_fraction: 0.5, pending: true, live_progress: lp },
      { match_id: 'rung1_m1', competitors: ['v0', 'v2'], winner: null, decision: '', survivors: [], cut: [], board_fraction: 0.5, pending: true, live_progress: {} },
    ],
  };
}

// a SETTLED rung-1 record (2→1; survivor v1, cut v2).
function rung1Settled() {
  return {
    stage_index: 1, label: 'Rung 1',
    matches: [{
      match_id: 'rung1', competitors: ['v1', 'v2'],
      winner: null, decision: '', survivors: ['v1'], cut: ['v2'],
      board_fraction: 0.5, pending: false, live_progress: {},
      deltas: { v1: -2.2, v2: -0.5 },
    }],
  };
}

// the PENDING champion-gate (the lone survivor v1 vs champion v0 on the full board).
function gatePending() {
  return {
    stage_index: 2, label: 'Champion gate',
    matches: [{
      match_id: 'racing-final', competitors: ['v0', 'v1'], winner: null, decision: '',
      survivors: [], cut: [], board_fraction: 1.0, pending: true,
      live_progress: {
        v0: { boards_total: 16, inflight: 1, projected_scalar: CHAMP_LOSS, projected: true },
        v1: { boards_total: 16, inflight: 1, projected_scalar: 63.0, projected: true, boards_done: 8 },
      },
    }],
  };
}

// the SETTLED champion-gate (v1 promoted).
function gateSettled() {
  return {
    stage_index: 2, label: 'Champion gate',
    matches: [{
      match_id: 'racing-final', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted',
      survivors: [], cut: [], board_fraction: 1.0, pending: false, live_progress: {},
      delta_scalar: -5.0,
    }],
  };
}

// assemble one published active-tournament snapshot for a lifecycle step.
function snapshot({ phase, rounds, standings, agg, projected, completed }) {
  return {
    structure: 'racing', epoch_id: EPOCH,
    phase: completed ? 'completed' : 'running',
    structure_params: { eta: 2, board_fraction: 0.25, board_size: 16 },
    champion_lineage: completed ? ['v0', 'v1'] : ['v0'],
    competitors: COMPETITORS,
    rounds,
    standings: standings || [],
    // the operator's case: the runner has NOT written partial_champion_agg yet
    // for the early steps (agg omitted) → the benchmark must come from the
    // strategy-seeded champion lane, never a fabricated default.
    ...(agg != null ? { partial_champion_agg: { scalar: agg } } : {}),
    ...(projected ? { projected } : {}),
    _phase: phase,
  };
}

// THE SEQUENCE — the ordered steps a real racing run publishes.
const SEQUENCE = [
  // 0) QUEUED — field minted, nothing scheduled yet (no rounds).
  { name: 'queued', at: snapshot({ phase: 'tournament:round_0:proposing', rounds: [] }) },

  // 1) RUNG 0 STREAMING — rung 0 in flight, boards landing, NO projected scalars
  //    yet (early). Published as the single carrier match for the entering rung.
  {
    name: 'rung0 streaming',
    at: snapshot({
      phase: 'tournament:round_0:rung0_m0',
      rounds: [{
        stage_index: 0, label: 'Rung 0',
        matches: [{
          match_id: 'rung0_m0', competitors: ['v0', 'v1'], winner: null, decision: '', survivors: [], cut: [],
          board_fraction: 0.25, pending: true,
          live_progress: {
            v0: { boards_total: 4, inflight: 1 },
            v1: { boards_total: 4, inflight: 1, boards_done: 1 },
            v2: { boards_total: 4, inflight: 1, boards_done: 1 },
            v3: { boards_total: 4, inflight: 1, boards_done: 0 },
            v4: { boards_total: 4, inflight: 1, boards_done: 0 },
          },
        },
        { match_id: 'rung0_m1', competitors: ['v0', 'v2'], winner: null, decision: '', survivors: [], cut: [], board_fraction: 0.25, pending: true, live_progress: {} },
        { match_id: 'rung0_m2', competitors: ['v0', 'v3'], winner: null, decision: '', survivors: [], cut: [], board_fraction: 0.25, pending: true, live_progress: {} },
        { match_id: 'rung0_m3', competitors: ['v0', 'v4'], winner: null, decision: '', survivors: [], cut: [], board_fraction: 0.25, pending: true, live_progress: {} }],
      }],
    }),
  },

  // 2) RUNG 0 SETTLED + RUNG 1 STREAMING — the cut committed (v1,v2 survive;
  //    v3,v4 cut) AND the next rung races ONLY the survivors. The champion lane
  //    carries the strategy-seeded benchmark; partial_champion_agg STILL empty.
  {
    name: 'rung0 settled · rung1 streaming',
    at: snapshot({
      phase: 'tournament:round_0:rung1_m0',
      rounds: [rung0Settled(), rung1Active({ v1: { boards_done: 2, projected_scalar: 9.5, projected: true }, v2: { boards_done: 1, projected_scalar: 9.8, projected: true } })],
      projected: { v1: { scalar: 9.5, boards_done: 2, boards_total: 4 }, v2: { scalar: 9.8, boards_done: 1, boards_total: 4 } },
    }),
  },

  // 3) RUNG 1 SETTLED — both rungs settled; the survivor is v1.
  {
    name: 'rung1 settled',
    at: snapshot({
      phase: 'tournament:round_0:rung1',
      rounds: [rung0Settled(), rung1Settled()],
      standings: [
        { generation_id: 'v1', rank: 1, scalar: 63.0, status: 'alive' },
        { generation_id: 'v2', rank: 2, scalar: 64.5, status: 'eliminated' },
        { generation_id: 'v0', rank: 3, scalar: CHAMP_LOSS, status: 'champion' },
      ],
      agg: CHAMP_LOSS,
    }),
  },

  // 4) CHAMPION-GATE DECIDING — the lone survivor v1 races the champion on the
  //    full board; both prior rungs are settled, the gate is in flight.
  {
    name: 'gate deciding',
    at: snapshot({
      phase: 'tournament:round_0:racing-final',
      rounds: [rung0Settled(), rung1Settled(), gatePending()],
      agg: CHAMP_LOSS,
      projected: { v1: { scalar: 63.0, boards_done: 8, boards_total: 16 } },
    }),
  },

  // 5) GATE SETTLED — v1 promoted; the whole tournament is committed.
  {
    name: 'gate settled (promote)',
    at: snapshot({
      phase: 'completed', completed: true,
      rounds: [rung0Settled(), rung1Settled(), gateSettled()],
      standings: [
        { generation_id: 'v1', rank: 1, scalar: 63.0, status: 'champion' },
        { generation_id: 'v0', rank: 2, scalar: CHAMP_LOSS, status: 'eliminated' },
        { generation_id: 'v2', rank: 3, scalar: 64.5, status: 'eliminated' },
        { generation_id: 'v3', rank: 4, scalar: 69.0, status: 'eliminated' },
        { generation_id: 'v4', rank: 5, scalar: 70.0, status: 'eliminated' },
      ],
      agg: CHAMP_LOSS,
    }),
  },
];

// build the racing model for a step through the REAL live pipeline.
function modelFor(at) {
  const epochGens = COMPETITORS.map((c) => c.generation_id);
  const m = STRUCT.buildLiveModel(at, hb(at._phase), [], epochGens) || STRUCT.normalizeStructure(at, true);
  return STRUCT.racingModel(m);
}

// the funnel SVG for a step's model.
function funnelFor(rm) {
  return svg.survivalFunnel({ rungs: rm.rungs, championId: rm.championId, benchmarkId: rm.benchmarkId, live: rm.live, gateState: rm.gateState, gateDelta: rm.gateDelta });
}
// the scalar-track SVG for a step's model.
function trackFor(rm, mini) {
  return svg.racingScalarTrack({ rungs: rm.rungs, championId: rm.championId, benchmarkId: rm.benchmarkId, championScalar: rm.championScalar, live: rm.live, gateState: rm.gateState, mini: !!mini });
}

// ===========================================================================
// 1) FULL RUNG SEQUENCE — at each step the model + the funnel carry EVERY rung
//    that exists so far (settled + active + gate), in order.
// ===========================================================================

test('sequence — the racing model carries the FULL rung sequence at every step (settled + active + gate)', () => {
  // queued: no rungs yet.
  let rm = modelFor(SEQUENCE[0].at);
  assertEqual(rm.rungs.length, 0, 'queued: no rungs scheduled yet');

  // rung0 streaming: exactly one rung (rung 0, in flight, full field of 4).
  rm = modelFor(SEQUENCE[1].at);
  assertEqual(rm.rungs.length, 1, 'rung0 streaming: one rung present');
  assert(rm.rungs[0].pending, 'rung0 streaming: the rung is pending (in flight)');
  assertEqual(rm.rungs[0].competitors.slice().sort().join(','), 'v1,v2,v3,v4', 'rung0 streaming: full field of 4 (champion excluded)');

  // rung0 settled · rung1 streaming: TWO rungs — settled rung 0 + active rung 1.
  rm = modelFor(SEQUENCE[2].at);
  assertEqual(rm.rungs.length, 2, 'rung0 settled · rung1 streaming: BOTH rungs present (the operator-missing-rung-0 case)');
  assertEqual(rm.rungs[0].survivors.slice().sort().join(','), 'v1,v2', 'settled rung 0: survivors committed');
  assertEqual(rm.rungs[0].cut.slice().sort().join(','), 'v3,v4', 'settled rung 0: cut committed');
  assert(!rm.rungs[0].pending, 'settled rung 0: NOT pending');
  assert(rm.rungs[1].pending, 'active rung 1: pending (in flight)');
  assertEqual(rm.rungs[1].competitors.slice().sort().join(','), 'v1,v2', 'active rung 1: races ONLY the two survivors');

  // rung1 settled: two settled rungs.
  rm = modelFor(SEQUENCE[3].at);
  assertEqual(rm.rungs.length, 2, 'rung1 settled: both rungs present');
  assert(!rm.rungs[0].pending && !rm.rungs[1].pending, 'rung1 settled: both rungs settled');
  assertEqual(rm.rungs[1].survivors.join(','), 'v1', 'rung1 settled: lone survivor v1');

  // gate deciding: two rungs + the gate is recognized (gateState deciding).
  rm = modelFor(SEQUENCE[4].at);
  assertEqual(rm.rungs.length, 2, 'gate deciding: the two rungs remain (gate is not a rung)');
  assertEqual(rm.gateState, 'deciding', 'gate deciding: gateState is deciding');

  // gate settled: the gate crowns v1.
  rm = modelFor(SEQUENCE[5].at);
  assertEqual(rm.gateState, 'crowned', 'gate settled: crowned');
  assertEqual(rm.championId, 'v1', 'gate settled: v1 crowned champion');
});

test('sequence — the SURVIVAL FUNNEL renders the WHOLE rung sequence mid-flight (Rung 0 narrowed, Rung 1 racing, gate)', () => {
  // the operator's exact mid-sequence state: rung0 settled, rung1 in flight.
  const rm = modelFor(SEQUENCE[2].at);
  const funnel = funnelFor(rm);
  const t = textOf(funnel);
  assert(/Rung 0/.test(t), 'funnel: Rung 0 (the first 4→2 narrowing) is present');
  assert(/Rung 1/.test(t), 'funnel: Rung 1 (the active rung) is present');
  assert(/champion-gate/.test(t), 'funnel: the champion-gate is present');
  // Rung 0 shows its narrowing: two survivors (↑) + two cuts (✕).
  assert(/v1 ↑/.test(t) && /v2 ↑/.test(t), 'funnel: Rung 0 survivors v1,v2 ride the band (↑)');
  assert(/v3 ✕/.test(t) && /v4 ✕/.test(t), 'funnel: Rung 0 cuts v3,v4 peel off (✕)');
  // exactly one band polygon per rung + one converging gate-flow polygon.
  const bands = nodesByClass(funnel, 'dn-funnel-band').filter((n) => n.localName === 'polygon');
  assertEqual(bands.length, 3, 'funnel: 2 rung bands + 1 gate-flow band');
});

test('sequence — the EPOCH round-timeline figure (single round) renders the full rung sequence', () => {
  // the epoch round-timeline reuses the SAME racingModel → survivalFunnel as the
  // single-round page (views/epoch.js figureForRound). Replicate that call and
  // assert the full sequence renders there too (operator: it showed only Rung 0 +
  // gate, missing Rung 1).
  const rm = modelFor(SEQUENCE[2].at);
  const fig = svg.survivalFunnel({ rungs: rm.rungs, championId: rm.championId, benchmarkId: rm.benchmarkId, live: rm.live, gateState: rm.gateState, gateDelta: rm.gateDelta });
  const t = textOf(fig);
  assert(/Rung 0/.test(t) && /Rung 1/.test(t) && /champion-gate/.test(t),
    'round-timeline figure: Rung 0 + Rung 1 + gate all render');
});

// ===========================================================================
// 2) EACH RUNG RENDERS ITS FULL FIELD (all survivors across the rung's matchups)
// ===========================================================================

test('sequence — each rung renders its FULL field (the union of every champion-vs-survivor matchup)', () => {
  // rung 1 is published as TWO matchups (rung1_m0: v0-v1, rung1_m1: v0-v2) with
  // the full per-lane live_progress on slot 0 only. The model + the funnel must
  // surface BOTH survivor lanes, not just the slot-0 matchup's first challenger.
  const rm = modelFor(SEQUENCE[2].at);
  const rung1 = rm.rungs[1];
  assertEqual(rung1.competitors.slice().sort().join(','), 'v1,v2', 'rung 1 field = both survivors');
  const funnel = funnelFor(rm);
  const t = textOf(funnel);
  assert(/v1 ·/.test(t) && /v2 ·/.test(t), 'funnel: BOTH rung-1 lanes (v1, v2) render as racing runners');
  // the scalar-track focuses the DEEPEST rung with competitors → rung 1; both
  // lanes plot as markers.
  const track = trackFor(rm);
  const names = nodesByClass(track, 'dn-scalartrack-name').map((n) => textOf(n));
  assert(names.some((n) => /v1/.test(n)) && names.some((n) => /v2/.test(n)),
    'scalar-track: both rung-1 lanes plot as markers (full field on the focus rung)');
});

// ===========================================================================
// 3) IN-FLIGHT LANES SHOW REAL PROJECTED POSITIONS + THE REAL CHAMPION BENCHMARK;
//    no-scalar lanes spread (not piled at x=0).
// ===========================================================================

test('sequence — the CHAMPION BENCHMARK is the REAL champion loss, NOT a fabricated default, even with an empty partial_champion_agg', () => {
  // rung0-settled · rung1-streaming: partial_champion_agg is EMPTY (the runner
  // has not written it yet). The benchmark MUST come from the strategy-seeded
  // champion lane (CHAMP_LOSS = 68.0), never a 10.000-style placeholder.
  const at = SEQUENCE[2].at;
  assert(at.partial_champion_agg == null, 'precondition: this step publishes NO partial_champion_agg');
  const rm = modelFor(at);
  assertEqual(rm.championScalar, CHAMP_LOSS, 'championScalar resolves to the REAL champion loss (68.0) from the seeded champion lane');
  const track = trackFor(rm);
  const benchLab = nodesByClass(track, 'dn-scalartrack-benchlab').map((n) => textOf(n)).join(' ');
  assert(/champ 68\.000/.test(benchLab), 'scalar-track: the dashed champion line reads the real loss (champ 68.000)');
  assert(!/champ 10\.000/.test(benchLab), 'scalar-track: NO fabricated 10.000-style default leaks');
});

test('sequence — when the champion scalar is GENUINELY unknown the benchmark is OMITTED (honest), not faked', () => {
  // an early entering rung with NO champion-lane scalar anywhere (queued→rung0
  // streaming, before any board projects). The benchmark line must be absent —
  // never a fabricated number.
  const rm = modelFor(SEQUENCE[1].at);
  assertEqual(rm.championScalar, null, 'rung0 streaming: champion scalar genuinely unknown → null');
  const track = trackFor(rm);
  assertEqual(nodesByClass(track, 'dn-scalartrack-benchlab').length, 0, 'scalar-track: NO champion benchmark line is drawn (honest omission)');
});

test('sequence — in-flight rung-1 lanes show REAL projected positions; an early no-scalar entering rung SPREADS its lanes (not piled at x=0)', () => {
  // rung1 streaming: both lanes carry projected scalars → dn-proj treatment.
  const rmMid = modelFor(SEQUENCE[2].at);
  const trackMid = trackFor(rmMid);
  assert(hasProjected(trackMid), 'rung1 streaming: in-flight lanes carry the projected (dn-proj) treatment');

  // rung0 streaming: NO recoverable scalar yet → the lanes must SPREAD across the
  // axis by index, not collapse onto the left edge (x=padL).
  const rmEarly = modelFor(SEQUENCE[1].at);
  const trackEarly = trackFor(rmEarly);
  const dots = nodesByClass(trackEarly, 'dn-scalartrack-dot');
  const xs = dots.map((d) => Number(d.getAttribute('cx'))).filter((x) => !Number.isNaN(x));
  assert(xs.length >= 2, 'rung0 streaming: at least two lane markers drawn');
  const uniqueX = new Set(xs.map((x) => Math.round(x)));
  assert(uniqueX.size >= 2, 'rung0 streaming: no-scalar lanes are SPREAD across the axis (distinct x), not piled at one x');
});

// ===========================================================================
// 4) THE HERO leads with the scalar-track mini; "what's running" is one block
//    per rung.
// ===========================================================================

test('sequence — the LIVE HERO leads with the scalar-track mini across the whole sequence', () => {
  const c = new live.LiveController({ onCompetitor() {} });
  for (const step of SEQUENCE) {
    if (step.at.phase === 'completed') continue; // the hero only shows for a running tournament.
    c.update({ status: { running: true, structure: 'racing' }, heartbeat: hb(step.at._phase), activeRuns: [], activeTournament: step.at });
    // the figure host either holds the scalar-track mini (once a rung exists) or
    // the honest placeholder (queued, before any rung).
    const hasTrack = svgsByClass(c._funnelHost, 'dn-scalartrack').length > 0;
    const hasPlaceholder = nodesByClass(c._funnelHost, 'dt-live-hero-nofunnel').length > 0
      || textOf(c._funnelHost).indexOf('field fills in') >= 0;
    assert(hasTrack || hasPlaceholder, `hero[${step.name}]: shows the scalar-track mini OR the honest placeholder`);
    if (step.name !== 'queued') assert(hasTrack, `hero[${step.name}]: the racing mini IS the scalar track once a rung exists`);
  }
});

test('sequence — the hero "what\'s running" is ONE block per RUNG (not one per champion-vs-survivor matchup)', () => {
  const c = new live.LiveController({ onCompetitor() {} });
  // rung0 settled · rung1 streaming: rung 1 is published as two matchups but the
  // hero groups them into ONE rung block.
  const step = SEQUENCE[2];
  c.update({ status: { running: true, structure: 'racing' }, heartbeat: hb(step.at._phase), activeRuns: [], activeTournament: step.at });
  const blocks = STRUCT.liveMatchBlocks(STRUCT.buildLiveModel(step.at, hb(step.at._phase), [], COMPETITORS.map((x) => x.generation_id)));
  const rungBlocks = blocks.filter((b) => b.kind === 'rung');
  assertEqual(rungBlocks.length, 1, 'one rung block for the in-flight rung 1 (the settled rung 0 is NOT a live block)');
  assertEqual(rungBlocks[0].entries.length, 2, 'the rung block carries BOTH survivor lanes (v1, v2)');
  assert(/field of 2/.test(rungBlocks[0].label), 'the rung block reads "field of 2"');

  // gate deciding: the gate is a 1v1 pair block, not a rung block.
  const gateStep = SEQUENCE[4];
  const gateBlocks = STRUCT.liveMatchBlocks(STRUCT.buildLiveModel(gateStep.at, hb(gateStep.at._phase), [], COMPETITORS.map((x) => x.generation_id)));
  assert(gateBlocks.some((b) => b.kind === 'pair' && String(b.match_id) === 'racing-final'), 'gate deciding: the gate is a 1v1 pair block');
  assert(!gateBlocks.some((b) => b.kind === 'rung'), 'gate deciding: no rung block (both rungs settled)');
});

// ===========================================================================
// 5) CONVERGENCE — the settled record renders byte-identically via the LIVE path
//    vs built directly; ANTI-FLASH — a no-op repeat changes no digest.
// ===========================================================================

test('sequence — CONVERGENCE: the SETTLED tournament renders byte-identically via the live path vs the completed record (no leftover projected chrome)', () => {
  const settled = SEQUENCE[5].at;
  // VIA-LIVE: drive the full sequence, then commit to the settled record.
  const epochGens = COMPETITORS.map((c) => c.generation_id);
  // 1) render the settled record DIRECTLY (the completed-record path).
  const direct = STRUCT.normalizeStructure(JSON.parse(JSON.stringify(settled)), false);
  const directHost = document.createElement('div');
  for (const n of STRUCT.renderStructure(direct, CTX, EPOCH)) if (n) directHost.appendChild(n);
  // 2) render the settled record reached VIA the live builder (which strips the
  //    live chrome at commit because every match is settled / phase completed).
  const viaLiveModel = STRUCT.buildLiveModel(JSON.parse(JSON.stringify(settled)), hb('completed'), [], epochGens);
  const viaLiveHost = document.createElement('div');
  for (const n of STRUCT.renderStructure(viaLiveModel, CTX, EPOCH)) if (n) viaLiveHost.appendChild(n);

  const directTrack = svgsByClass(directHost, 'dn-scalartrack')[0];
  const viaTrack = svgsByClass(viaLiveHost, 'dn-scalartrack')[0];
  assert(directTrack && viaTrack, 'both paths produce the settled scalar-track');
  assertEqual(serialize(viaTrack), serialize(directTrack), 'the settled scalar-track is byte-identical via either path (no projected chrome leaks past commit)');
  // and the settled render shows NO projected ghost.
  assert(!hasProjected(directTrack), 'settled: NO dn-proj projected chrome remains');
});

test('sequence — ANTI-FLASH: a no-op heartbeat repeat at a mid-sequence step changes no digest + rebuilds no DOM', () => {
  // structureDigest is the gate the views use; the racingScalarTrackDigest is the
  // gate the hero mini uses. Both must be stable across an identical repeat.
  const step = SEQUENCE[2]; // rung0 settled · rung1 streaming
  const epochGens = COMPETITORS.map((c) => c.generation_id);
  const mA = STRUCT.buildLiveModel(JSON.parse(JSON.stringify(step.at)), hb(step.at._phase), [], epochGens);
  const mB = STRUCT.buildLiveModel(JSON.parse(JSON.stringify(step.at)), hb(step.at._phase), [], epochGens);
  assertEqual(STRUCT.structureDigest(mA), STRUCT.structureDigest(mB), 'structureDigest is stable across an identical no-op repeat');
  const rmA = STRUCT.racingModel(mA);
  const rmB = STRUCT.racingModel(mB);
  const optsA = { rungs: rmA.rungs, championId: rmA.championId, benchmarkId: rmA.benchmarkId, championScalar: rmA.championScalar, live: true, gateState: rmA.gateState, mini: true };
  const optsB = { rungs: rmB.rungs, championId: rmB.championId, benchmarkId: rmB.benchmarkId, championScalar: rmB.championScalar, live: true, gateState: rmB.gateState, mini: true };
  assertEqual(svg.racingScalarTrackDigest(optsA), svg.racingScalarTrackDigest(optsB), 'racingScalarTrackDigest is stable across a no-op repeat');

  // drive the REAL hero twice with the identical snapshot → the figure DOM node
  // identity is preserved (no rebuild) on the second, no-op tick.
  const c = new live.LiveController({ onCompetitor() {} });
  c.update({ status: { running: true, structure: 'racing' }, heartbeat: hb(step.at._phase), activeRuns: [], activeTournament: step.at });
  const firstFig = svgsByClass(c._funnelHost, 'dn-scalartrack')[0];
  c.update({ status: { running: true, structure: 'racing' }, heartbeat: hb(step.at._phase), activeRuns: [], activeTournament: step.at });
  const secondFig = svgsByClass(c._funnelHost, 'dn-scalartrack')[0];
  assert(firstFig && secondFig && firstFig === secondFig, 'hero: a no-op repeat preserves the figure DOM node (no flash / rebuild)');
});

test('sequence — a REAL change between steps DOES move the digest (the swap fires) — progress is not silently swallowed', () => {
  const epochGens = COMPETITORS.map((c) => c.generation_id);
  // rung0-streaming → rung0-settled is a real change (the cut committed).
  const m1 = STRUCT.buildLiveModel(SEQUENCE[1].at, hb(SEQUENCE[1].at._phase), [], epochGens);
  const m2 = STRUCT.buildLiveModel(SEQUENCE[2].at, hb(SEQUENCE[2].at._phase), [], epochGens);
  assert(STRUCT.structureDigest(m1) !== STRUCT.structureDigest(m2), 'the cut (rung0 settling) moves the structure digest');
  // and the champion benchmark appearing (null → 68.0) moves the track digest.
  const rm1 = STRUCT.racingModel(m1);
  const rm2 = STRUCT.racingModel(m2);
  const o1 = { rungs: rm1.rungs, championId: rm1.championId, benchmarkId: rm1.benchmarkId, championScalar: rm1.championScalar, live: true, gateState: rm1.gateState };
  const o2 = { rungs: rm2.rungs, championId: rm2.championId, benchmarkId: rm2.benchmarkId, championScalar: rm2.championScalar, live: true, gateState: rm2.gateState };
  assert(svg.racingScalarTrackDigest(o1) !== svg.racingScalarTrackDigest(o2), 'the benchmark appearing + the new rung moves the scalar-track digest');
});

// ===========================================================================
// THE DENSE "WHAT'S RUNNING" CHAMPION-GATE CARD — each competitor is ONE aligned
// row with fixed columns L→R: vN · progress bar (the width-filler) · ~projected
// scalar · k/N boards · PROJ tag. The k/N boards-done is a FIRST-CLASS column,
// not the clipped trailing afterthought it used to be.
// ===========================================================================

test('dense champion-gate card: each competitor row carries ALL fields inline — id · bar · ~scalar · k/N boards · PROJ — nothing clipped', () => {
  // SEQUENCE[4] is the gate-deciding step: the champion-gate (racing-final) with
  // the lone survivor v1 PROJECTED at 63.0, 8/16 boards in.
  const step = SEQUENCE[4];
  const c = new live.LiveController({ onCompetitor() {} });
  c.update({ status: { running: true, structure: 'racing' }, heartbeat: hb(step.at._phase), activeRuns: [], activeTournament: step.at });

  // the champion-gate block is present (`data-match="racing-final"`).
  const gateBlock = nodesByClass(c._matchesBody, 'dt-live-match')
    .find((b) => /champion-gate/i.test(textOf(b)));
  assert(gateBlock, 'the champion-gate block renders in "what’s running"');

  // the PROJECTED challenger row (v1) carries EVERY column, in order, inline.
  const rows = nodesByClass(gateBlock, 'dt-live-match-row');
  assert(rows.length >= 1, 'the gate card has at least one competitor row');
  const projRow = rows.find((r) => /v1/.test(textOf(nodesByClass(r, 'dt-live-match-name')[0] || r)));
  assert(projRow, 'the projected challenger v1 has a row');

  // 1 — id column.
  assertEqual(textOf(nodesByClass(projRow, 'dt-live-match-name')[0]), 'v1', 'col 1: the competitor id (vN)');
  // 2 — the width-filling progress bar.
  assert(nodesByClass(projRow, 'dt-live-match-bar').length === 1, 'col 2: exactly one progress bar (the width-filling element)');
  const fill = nodesByClass(projRow, 'dt-live-match-fill')[0];
  assert(fill && /width:\s*\d+%/.test(fill.style.cssText || ''), 'col 2: the bar fill width is set inline (CSS-animated, not a node swap)');
  // 3 — the ~projected scalar column.
  const scalar = nodesByClass(projRow, 'dt-live-match-scalar')[0];
  assert(scalar && /~63/.test(textOf(scalar)), 'col 3: the ~projected scalar reads "~63"');
  // 4 — the k/N boards column, FIRST-CLASS + not truncated (reads the full 8/16).
  const boards = nodesByClass(projRow, 'dt-live-match-boards')[0];
  assert(boards, 'col 4: a dedicated boards-done column exists (not the clipped trailing glyph)');
  assertEqual(textOf(boards), '8/16', 'col 4: the boards-done reads the FULL k/N (8/16) — never a truncated "8…"');
  // 5 — the PROJ tag column.
  const tag = nodesByClass(projRow, 'dt-live-match-tag')[0];
  assert(tag && /PROJ/i.test(textOf(tag)), 'col 5: the trailing tag reads PROJ for the in-flight projection');

  // the column ORDER on the DOM is id → bar → scalar → boards → tag (no far-right
  // floating: the five columns appear in that left-to-right sequence).
  const order = (projRow.childNodes || []).filter((n) => n.nodeType === 1)
    .map((n) => (n._attrs.class || '').split(/\s+/).find((k) => k.startsWith('dt-live-match-')));
  assertEqual(order.join(' '), 'dt-live-match-name dt-live-match-bar dt-live-match-scalar dt-live-match-boards dt-live-match-tag',
    'the five columns appear in the fixed L→R order (id · bar · scalar · boards · tag)');
});

test('dense champion-gate card: a no-op heartbeat repeat churns NO matches DOM (digest-gated render discipline)', () => {
  const step = SEQUENCE[4];
  const c = new live.LiveController({ onCompetitor() {} });
  c.update({ status: { running: true, structure: 'racing' }, heartbeat: hb(step.at._phase), activeRuns: [], activeTournament: step.at });
  const firstRows = nodesByClass(c._matchesBody, 'dt-live-match-row');
  const firstNode = c._matchesBody.firstChild;
  // an identical second tick (same digest) must preserve the existing DOM nodes.
  c.update({ status: { running: true, structure: 'racing' }, heartbeat: hb(step.at._phase), activeRuns: [], activeTournament: step.at });
  const secondNode = c._matchesBody.firstChild;
  assert(firstNode && secondNode && firstNode === secondNode,
    'a no-op heartbeat preserves the matches DOM node (no flash / rebuild)');
  const secondRows = nodesByClass(c._matchesBody, 'dt-live-match-row');
  assert(firstRows.length === secondRows.length && firstRows.every((r, i) => r === secondRows[i]),
    'every competitor row is the SAME node across the no-op repeat (zero DOM churn)');
});

test('dense champion-gate card: the CSS row is a 5-track grid with the bar as the only flexible (1fr) column', async () => {
  const fs = await import('node:fs');
  const css = fs.readFileSync(new URL('../css/console.css', import.meta.url), 'utf8');
  const m = css.match(/\.dt-live-match-row\s*\{[^}]*grid-template-columns:\s*([^;]+);/);
  assert(m, 'the .dt-live-match-row grid-template-columns rule is defined');
  const tracks = m[1].trim();
  assert(/1fr/.test(tracks), 'the row grid carries a 1fr track (the bar is the width-filling column)');
  assert((tracks.match(/max-content/g) || []).length >= 4, 'the other four columns are content-sized (no far-right floating)');
  // each new column has its own CSS rule.
  assert(/\.dt-live-match-scalar\s*\{/.test(css), 'the ~scalar column has a CSS rule');
  assert(/\.dt-live-match-boards\s*\{/.test(css), 'the k/N boards column has a CSS rule');
  assert(/\.dt-live-match-tag\s*\{/.test(css), 'the PROJ/verdict tag column has a CSS rule');
});

await run();
