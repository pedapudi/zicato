// test/bracket.test.mjs — Variant T elim BRACKET-FLOW connectivity guard.
//
// The operator could not trace who-plays-whom / who-advances in the single- and
// double-elimination bracket views: dots, labels and connector lines were
// mis-associated. The defects this pins (all in svg.elimFlow + the elimModel
// band split that feeds it):
//
//   * COLUMN ORDER — the double-elim caller passes winners.concat(losers), which
//     lists the GRAND FINAL (a winners' band) BEFORE the losers' rounds. The
//     renderer must lay columns out by round_index (WB → LB → GF), so every
//     advancement / drop edge runs left-to-right into its REAL target column, not
//     a neighbour, and the losers' bracket is not stranded to the right of the
//     gate.
//   * WINNERS→LOSERS DROP — a generation that loses in the WB is NOT eliminated
//     (it gets a second life in the LB). Its WB dot must connect to its LB entry
//     by a DROP edge (no orphan dot), it must NOT draw a phantom ✕ in the WB, and
//     its TRUE elimination ✕ sits at its LAST (losers') loss.
//   * BYE — a competitor with a bye advances cleanly and does not desync the rest
//     of the bracket's dot/label/line mapping.
//   * LIVE vs SETTLED — the live `pending` final maps the same as a settled one
//     (the in-flight leg reads pending, nothing is falsely eliminated).
//
// The assertions read the rendered SVG geometry directly (column x, lane y, dot
// class, segment endpoints, ✕ marks) so a regression that re-strands a column or
// re-orphans a dropped lane fails here.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const svg = await import('../js/variants/T/svg.js');
const structure = await import('../js/variants/T/views/structure.js');

// ---- SVG geometry readers ------------------------------------------------

function walk(node, fn) {
  fn(node);
  for (const c of (node.childNodes || [])) if (c.nodeType === 1) walk(c, fn);
}
function clsOf(n) { return (n.getAttribute && n.getAttribute('class')) || ''; }
function num(n, a) { const v = n.getAttribute(a); return v == null ? null : Number(v); }

// The double-elim WB→LB drop connector is NOT a <line> — Wave A refined it into a
// rounded orthogonal <path class="…dn-elimflow-seg-drop…"> built by elbowPath():
// it dips through a staggered bus and rises into the LB re-entry node, so the
// visual connection is intact (no orphan dot). A <path> has no x1/x2/y1 attrs, so
// we recover the seg's endpoints from its `d` grammar. elbowPath emits
//   M x0 y0  L x0 …  Q …  L …  Q …  L xt yt
// i.e. the FIRST coordinate pair (the M) is the WB-loss start (x1,y1) and the LAST
// coordinate pair (the terminal L) is the LB re-entry end (x2,y2). The drop runs
// on a single lane row, so y1 ≈ y2 ≈ the lane y — exactly what segFromTo matches.
function dropPathEndpoints(d) {
  const s = String(d || '');
  // first command is "M x0 y0"
  const m = s.match(/^\s*M\s*(-?[\d.]+)[ ,]+(-?[\d.]+)/);
  // last coordinate pair anywhere in the path is the terminal "L xt yt"
  const pairs = [...s.matchAll(/(-?[\d.]+)[ ,]+(-?[\d.]+)/g)];
  if (!m || pairs.length === 0) return null;
  const last = pairs[pairs.length - 1];
  return { x1: Number(m[1]), y1: Number(m[2]), x2: Number(last[1]), y2: Number(last[2]) };
}

// Read the rendered elim-flow into a structured, assertable shape.
function readFlow(flow) {
  const dots = [];     // { x, y, tone, tip }
  const segs = [];      // { x1, x2, y, drop, pending }
  const cuts = [];      // { x, y }
  const lanes = [];     // label text -> y, top-to-bottom render order
  const convs = [];     // convergence nodes { x, y }
  walk(flow, (n) => {
    const cls = clsOf(n);
    const list = cls.split(/\s+/);
    if (list.includes('dn-elimflow-dot')) {
      const tone = list.includes('dn-elimflow-good') ? 'good'
        : list.includes('dn-elimflow-bad') ? 'bad'
          : list.includes('dn-elimflow-pending') ? 'pending' : 'none';
      // the hovercard tip is attached to the node; read it from the title-ish
      // sibling the harness records is not available, so derive tone only.
      dots.push({ x: num(n, 'cx'), y: num(n, 'cy'), tone });
    } else if (list.includes('dn-elimflow-seg')) {
      // A <line> seg carries x1/x2/y1; a <path> drop seg (elbowPath) carries `d`
      // instead — parse its endpoints out of the path grammar so the drop edge is
      // tracked with real coordinates (not null) and segFromTo can match it.
      let x1 = num(n, 'x1');
      let x2 = num(n, 'x2');
      let y = num(n, 'y1');
      if (n.localName === 'path' && x1 == null) {
        const ep = dropPathEndpoints(n.getAttribute('d'));
        if (ep) { x1 = ep.x1; x2 = ep.x2; y = ep.y1; }
      }
      segs.push({
        x1, x2, y,
        drop: list.includes('dn-elimflow-seg-drop'),
        pending: list.includes('dn-elimflow-seg-pending'),
      });
    } else if (list.includes('dn-elimflow-cut')) {
      cuts.push({ x: num(n, 'x'), y: num(n, 'y') });
    } else if (list.includes('dn-elimflow-convnode')) {
      convs.push({ x: num(n, 'cx'), y: num(n, 'cy') });
    } else if (list.includes('dn-elimflow-name')) {
      lanes.push({ label: n.textContent, y: num(n, 'y') });
    }
  });
  return { dots, segs, cuts, lanes, convs };
}

// The y a lane label sits on (labels carry a trailing crown/marker — match by
// the leading id token).
function laneY(flow, id) {
  const f = flow._read || (flow._read = readFlow(flow));
  const row = f.lanes.find((l) => String(l.label).trim().split(/\s+/)[0] === id);
  return row ? row.y : null;
}
// A dot at (≈x, ≈y) — dots sit on the lane's exact y; the label baseline is the
// same y + ~3.2 so we match the lane y within a small tolerance.
function dotAt(flow, id, x) {
  const f = flow._read || (flow._read = readFlow(flow));
  const y = laneY(flow, id);
  if (y == null) return null;
  return f.dots.find((d) => Math.abs(d.x - x) < 1 && Math.abs(d.y - (y - 3.2)) < 4) || null;
}
// Is there a connector segment on `id`'s lane from x1≈ to x2≈ ?
function segFromTo(flow, id, x1, x2) {
  const f = flow._read || (flow._read = readFlow(flow));
  const y = laneY(flow, id);
  if (y == null) return null;
  return f.segs.find((s) => Math.abs(s.y - (y - 3.2)) < 4 && Math.abs(s.x1 - x1) < 1 && Math.abs(s.x2 - x2) < 1) || null;
}
function cutOn(flow, id, x) {
  const f = flow._read || (flow._read = readFlow(flow));
  const y = laneY(flow, id);
  if (y == null) return null;
  // the ✕ sits 8px right of the column node x, on the lane label baseline (y).
  return f.cuts.find((c) => Math.abs(c.x - (x + 8)) < 2 && Math.abs(c.y - y) < 4) || null;
}

// the column x the renderer uses: padL(16) + ci*colW(116) + 8.
function colX(ci) { return 16 + ci * 116 + 8; }
function gateX(nCols) { return 16 + nCols * 116 + 8; }
// the lane row height the renderer uses (lanes are laneH apart, centred on lane y).
function laneH() { return 22; }

// ---- SINGLE-ELIM (settled, with a champion crowned) ----------------------

test('single-elim bracket: dots/labels/edges connect each match into the next round, champion reaches the gate', () => {
  // v0 = champion (benchmark). 4 challengers v1..v4.
  //   R0: WB-R0-0 v1>v2, WB-R0-1 v3>v4
  //   R1: WB-R1-0 v1>v3
  //   Final: v0 vs v1 → v1 promoted (crowned).
  const st = {
    structure: 'single_elim',
    champion_lineage: ['v0', 'v1'],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }, { generation_id: 'v4' }],
    rounds: [
      { round_index: 0, label: 'Round 1', matches: [
        { match_id: 'WB-R0-0', competitors: ['v1', 'v2'], winner: 'v1', bracket_slot: 'WB-R0-0' },
        { match_id: 'WB-R0-1', competitors: ['v3', 'v4'], winner: 'v3', bracket_slot: 'WB-R0-1' },
      ] },
      { round_index: 1, label: 'Round 2', matches: [
        { match_id: 'WB-R1-0', competitors: ['v1', 'v3'], winner: 'v1', bracket_slot: 'WB-R1-0' },
      ] },
      { round_index: 2, label: 'Final', matches: [
        { match_id: 'final', competitors: ['v0', 'v1'], winner: 'v1', decision: 'promoted', bracket_slot: 'final' },
      ] },
    ],
  };
  const model = structure.elimModel(st);
  assertEqual(model.championId, 'v1', 'v1 is crowned');
  assertEqual(model.benchmarkId, 'v0', 'v0 is the benchmark/incumbent');
  assertEqual(model.gateState, 'crowned', 'the gate crowned the survivor');
  assert(model.losers == null, 'single-elim has no losers band');
  const flow = svg.elimFlow({ winners: model.winners.concat(model.losers || []), championId: model.championId, benchmarkId: model.benchmarkId, gateState: model.gateState });

  // columns: 0=R0, 1=R1, 2=Final ; gate after column 2.
  // v1 advances every round → a continuous good chain into the gate.
  assert(dotAt(flow, 'v1', colX(0)) && dotAt(flow, 'v1', colX(0)).tone === 'good', 'v1 won R0 (good dot)');
  assert(dotAt(flow, 'v1', colX(1)) && dotAt(flow, 'v1', colX(1)).tone === 'good', 'v1 won R1 (good dot)');
  assert(dotAt(flow, 'v1', colX(2)) && dotAt(flow, 'v1', colX(2)).tone === 'good', 'v1 won the final (good dot)');
  assert(segFromTo(flow, 'v1', colX(0), colX(1)), 'v1 R0→R1 advancement edge');
  assert(segFromTo(flow, 'v1', colX(1), colX(2)), 'v1 R1→Final advancement edge');
  assert(segFromTo(flow, 'v1', colX(2), gateX(3)), 'v1 Final→gate edge (the champion reaches the gate)');
  assert(!cutOn(flow, 'v1', colX(0)) && !cutOn(flow, 'v1', colX(2)), 'the champion is never marked eliminated');

  // v3 wins R0 then loses R1: its true (and only) elimination ✕ is at R1.
  assert(dotAt(flow, 'v3', colX(0)) && dotAt(flow, 'v3', colX(0)).tone === 'good', 'v3 won R0');
  assert(dotAt(flow, 'v3', colX(1)) && dotAt(flow, 'v3', colX(1)).tone === 'bad', 'v3 lost R1');
  assert(segFromTo(flow, 'v3', colX(0), colX(1)), 'v3 R0→R1 edge connects its two played columns');
  assert(cutOn(flow, 'v3', colX(1)), 'v3 eliminated at R1 (the ✕ sits at its only loss)');

  // v2 loses R0 immediately → eliminated at R0, single dot + ✕, no forward edge.
  assert(dotAt(flow, 'v2', colX(0)) && dotAt(flow, 'v2', colX(0)).tone === 'bad', 'v2 lost R0');
  assert(cutOn(flow, 'v2', colX(0)), 'v2 eliminated at R0');
  assert(!segFromTo(flow, 'v2', colX(0), colX(1)), 'a first-round loser has no forward edge');

  // the two R0 feeder matches converge into the single R1 match — a winner from
  // each (v1, v3) carries forward and they MEET at R1: assert v1 & v3 both have a
  // dot in column 1 (the shared next match), v2 & v4 do not.
  assert(dotAt(flow, 'v1', colX(1)) && dotAt(flow, 'v3', colX(1)), 'both R0 winners meet in the R1 match');
  assert(!dotAt(flow, 'v2', colX(1)) && !dotAt(flow, 'v4', colX(1)), 'R0 losers do not appear in R1');
});

// ---- DOUBLE-ELIM (settled, with a losers-bracket drop) -------------------

test('double-elim bracket: columns order WB→LB→GF, winners→losers DROP edges connect, true eliminations at the second loss', () => {
  // v0 = champion. v1..v4 challengers.
  //   WB: WB-R0-0 v1>v2 (v2 drops), WB-R0-1 v3>v4 (v4 drops); WB-R1-0 v1>v3 (v3 drops). v1 = WB survivor.
  //   LB: LB-R2-0 v2>v4 (v4 OUT), LB-R3-0 v2>v3 (v3 OUT). v2 = LB survivor.
  //   GF: v0 vs v1 → v1 promoted.
  const st = {
    structure: 'double_elim',
    champion_lineage: ['v0', 'v1'],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }, { generation_id: 'v4' }],
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
  };
  const model = structure.elimModel(st);
  assertEqual(model.championId, 'v1', 'v1 promoted at the grand final');
  assertEqual(model.benchmarkId, 'v0', 'v0 is the benchmark/incumbent');
  assert(Array.isArray(model.losers) && model.losers.length === 2, 'the losers bracket has two rounds');
  // the bands the caller passes (winners THEN losers — GF before LB on purpose).
  const flow = svg.elimFlow({ winners: model.winners.concat(model.losers), championId: model.championId, benchmarkId: model.benchmarkId, gateState: model.gateState });

  // COLUMNS, by round_index: 0=WB-R0, 1=WB-R1, 2=LB-R2, 3=LB-R3, 4=GF, gate after 4.
  // v1: WB-R0(c0)→WB-R1(c1)→GF(c4)→gate ; never touches the LB columns.
  assert(dotAt(flow, 'v1', colX(0)) && dotAt(flow, 'v1', colX(0)).tone === 'good', 'v1 won WB-R0');
  assert(dotAt(flow, 'v1', colX(1)) && dotAt(flow, 'v1', colX(1)).tone === 'good', 'v1 won WB-R1');
  assert(dotAt(flow, 'v1', colX(4)) && dotAt(flow, 'v1', colX(4)).tone === 'good', 'v1 won the grand final');
  assert(segFromTo(flow, 'v1', colX(1), colX(4)), 'v1 WB-R1→GF edge SKIPS the losers columns (left-to-right into the GF)');
  assert(segFromTo(flow, 'v1', colX(4), gateX(5)), 'v1 GF→gate edge');

  // v0 plays ONLY the grand final (one dot, loses) → eliminated at the GF column.
  assert(dotAt(flow, 'v0', colX(4)) && dotAt(flow, 'v0', colX(4)).tone === 'bad', 'v0 lost the grand final');
  assert(cutOn(flow, 'v0', colX(4)), 'v0 (incumbent) eliminated at the GF');

  // v2 — the DROP case: loses WB-R0 (c0), drops to the LB, wins LB-R2 (c2) + LB-R3 (c3).
  const v2c0 = dotAt(flow, 'v2', colX(0));
  assert(v2c0 && v2c0.tone === 'bad', 'v2 lost WB-R0 (its dot reads as a loss, not pending)');
  assert(!cutOn(flow, 'v2', colX(0)), 'the WB loss is NOT a phantom elimination — v2 has a second life');
  const drop = segFromTo(flow, 'v2', colX(0), colX(2));
  assert(drop && drop.drop, 'v2 WB-R0→LB-R2 DROP edge connects the WB loss to the LB entry (no orphan dot)');
  assert(dotAt(flow, 'v2', colX(2)) && dotAt(flow, 'v2', colX(2)).tone === 'good', 'v2 won LB-R2');
  assert(dotAt(flow, 'v2', colX(3)) && dotAt(flow, 'v2', colX(3)).tone === 'good', 'v2 won LB-R3');
  assert(segFromTo(flow, 'v2', colX(2), colX(3)), 'v2 LB-R2→LB-R3 edge');
  assert(!cutOn(flow, 'v2', colX(0)) && !cutOn(flow, 'v2', colX(2)) && !cutOn(flow, 'v2', colX(3)),
    'the LB survivor is never marked eliminated');

  // v3 — drops then is OUT: wins WB-R0 (c0), loses WB-R1 (c1, DROP), loses LB-R3 (c3, ELIMINATED).
  assert(dotAt(flow, 'v3', colX(0)) && dotAt(flow, 'v3', colX(0)).tone === 'good', 'v3 won WB-R0');
  assert(!cutOn(flow, 'v3', colX(1)), 'v3 WB-R1 loss is a drop, NOT a phantom elimination ✕');
  const v3drop = segFromTo(flow, 'v3', colX(1), colX(3));
  assert(v3drop && v3drop.drop, 'v3 WB-R1→LB-R3 DROP edge connects the WB loss to the LB re-entry');
  assert(cutOn(flow, 'v3', colX(3)), "v3's TRUE elimination ✕ is at its SECOND loss (LB-R3), not the WB");

  // v4 — drops then OUT at the FIRST losers round: loses WB-R0 (c0), loses LB-R2 (c2).
  assert(!cutOn(flow, 'v4', colX(0)), 'v4 WB-R0 loss is a drop, not a phantom elimination');
  const v4drop = segFromTo(flow, 'v4', colX(0), colX(2));
  assert(v4drop && v4drop.drop, 'v4 WB-R0→LB-R2 DROP edge');
  assert(cutOn(flow, 'v4', colX(2)), 'v4 eliminated at LB-R2 (its second loss)');

  // no advancement/drop edge points BACKWARDS (every connector runs left→right).
  const f = readFlow(flow);
  assert(f.segs.every((s) => s.x2 > s.x1), 'every connector runs left-to-right (no backwards edge into a stranded column)');
  // the losers' columns are LEFT of the gate (not stranded to its right): the
  // rightmost real column is the GF (c4) and the gate is to its right.
  const maxDotX = Math.max(...f.dots.map((d) => d.x));
  assertEqual(maxDotX, colX(4), 'the GF is the rightmost match column — the LB is not stranded past the gate');
});

// ---- BYE (settled) -------------------------------------------------------

test('single-elim bracket: a BYE advances cleanly and does not desync the dot/label/line mapping', () => {
  // 3 challengers: R0 v1>v2, v3 BYE. R1 v1 vs v3 → v3 wins. Final v0 vs v3 → champion stands.
  const st = {
    structure: 'single_elim',
    champion_lineage: ['v0'],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }],
    rounds: [
      { round_index: 0, label: 'Round 1', matches: [
        { match_id: 'WB-R0-0', competitors: ['v1', 'v2'], winner: 'v1', bracket_slot: 'WB-R0-0' },
        { match_id: 'WB-R0-1', competitors: ['v3'], winner: 'v3', bye: true, bracket_slot: 'WB-R0-1' },
      ] },
      { round_index: 1, label: 'Round 2', matches: [
        { match_id: 'WB-R1-0', competitors: ['v1', 'v3'], winner: 'v3', bracket_slot: 'WB-R1-0' },
      ] },
      { round_index: 2, label: 'Final', matches: [
        { match_id: 'final', competitors: ['v0', 'v3'], winner: 'v0', decision: 'rejected', bracket_slot: 'final' },
      ] },
    ],
  };
  const model = structure.elimModel(st);
  assertEqual(model.gateState, 'stands', 'the survivor lost the gate — champion stands');
  assertEqual(model.championId, null, 'no new champion crowned');
  const flow = svg.elimFlow({ winners: model.winners.concat(model.losers || []), championId: model.championId, benchmarkId: model.benchmarkId, gateState: model.gateState });

  // v3's BYE: a good dot at R0 that connects forward to its real R1 match.
  assert(dotAt(flow, 'v3', colX(0)) && dotAt(flow, 'v3', colX(0)).tone === 'good', 'v3 advances via the bye (good dot at R0)');
  assert(segFromTo(flow, 'v3', colX(0), colX(1)), 'the bye lane connects R0→R1 (no gap)');
  assert(!cutOn(flow, 'v3', colX(0)), 'a bye is not an elimination');
  // v3 reaches the final and loses it → its only ✕ is at the final column.
  assert(dotAt(flow, 'v3', colX(2)) && dotAt(flow, 'v3', colX(2)).tone === 'bad', 'v3 lost the final');
  assert(cutOn(flow, 'v3', colX(2)), 'v3 eliminated at the final');

  // v1 is unaffected by v3's bye: won R0, lost R1.
  assert(dotAt(flow, 'v1', colX(0)) && dotAt(flow, 'v1', colX(0)).tone === 'good', 'v1 won R0');
  assert(dotAt(flow, 'v1', colX(1)) && dotAt(flow, 'v1', colX(1)).tone === 'bad', 'v1 lost R1');
  assert(cutOn(flow, 'v1', colX(1)), 'v1 eliminated at R1 (the bye did not shift its mapping)');
  // v2 lost R0.
  assert(cutOn(flow, 'v2', colX(0)), 'v2 eliminated at R0');
});

// ---- LIVE vs SETTLED -----------------------------------------------------

test('live elim bracket: the in-flight (pending) final maps the same as a settled one — nothing falsely eliminated', () => {
  // R0 v1>v2 settled; the Final v0 vs v1 is IN FLIGHT (winner:null, pending).
  const live = {
    structure: 'single_elim', live: true, phase: 'running',
    champion_lineage: ['v0'],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }],
    rounds: [
      { round_index: 0, label: 'Round 1', matches: [
        { match_id: 'WB-R0-0', competitors: ['v1', 'v2'], winner: 'v1', bracket_slot: 'WB-R0-0' },
      ] },
      { round_index: 1, label: 'Final', matches: [
        { match_id: 'final', competitors: ['v0', 'v1'], winner: null, pending: true, bracket_slot: 'final' },
      ] },
    ],
  };
  const lm = structure.elimModel(structure.normalizeStructure(live, true));
  assert(lm.live, 'the model is live');
  assertEqual(lm.gateState, 'deciding', 'a live bracket is deciding');
  const flow = svg.elimFlow({ winners: lm.winners.concat(lm.losers || []), championId: lm.championId, benchmarkId: lm.benchmarkId, gateState: lm.gateState, live: true });

  // v1 won R0 (good), races the pending final (pending dot), connected R0→Final.
  assert(dotAt(flow, 'v1', colX(0)) && dotAt(flow, 'v1', colX(0)).tone === 'good', 'v1 won R0 (good)');
  const v1fin = dotAt(flow, 'v1', colX(1));
  assert(v1fin && v1fin.tone === 'pending', 'v1 is racing the in-flight final (pending dot, not falsely won/lost)');
  assert(segFromTo(flow, 'v1', colX(0), colX(1)), 'v1 R0→Final edge connects into the live match');
  // v0 sits ONLY at the pending final — never falsely eliminated.
  const v0fin = dotAt(flow, 'v0', colX(1));
  assert(v0fin && v0fin.tone === 'pending', 'v0 races the pending final (pending dot)');
  assert(!cutOn(flow, 'v0', colX(1)), 'v0 is not eliminated while the final is in flight');
  // v2 lost R0 → its (settled) elimination still reads correctly under a live run.
  assert(dotAt(flow, 'v2', colX(0)) && dotAt(flow, 'v2', colX(0)).tone === 'bad', 'v2 lost R0');
  assert(cutOn(flow, 'v2', colX(0)), 'a settled R0 loss is still a clean elimination during a live run');
});

// The persisted within-tournament stage key is `stage_index`; normalizeStructure
// maps it to the renderer's internal `round_index`, while still accepting the
// legacy `round_index` key so pre-rename workspaces keep rendering.
test('normalizeStructure: stage_index → round_index (new key + legacy fallback + mixed)', () => {
  const newKey = structure.normalizeStructure({
    structure: 'single_elim',
    rounds: [
      { stage_index: 0, label: 'Bracket round 1', matches: [] },
      { stage_index: 1, label: 'Final', matches: [] },
    ],
  }, false);
  assertEqual(newKey.rounds[0].round_index, 0, 'stage_index 0 maps to round_index 0');
  assertEqual(newKey.rounds[1].round_index, 1, 'stage_index 1 maps to round_index 1');

  const legacy = structure.normalizeStructure({
    structure: 'single_elim',
    rounds: [{ round_index: 2, label: 'Bracket round 3', matches: [] }],
  }, false);
  assertEqual(legacy.rounds[0].round_index, 2, 'a legacy round_index key is preserved');

  // If both are present (a transitional record), the explicit round_index wins.
  const both = structure.normalizeStructure({
    structure: 'single_elim',
    rounds: [{ round_index: 5, stage_index: 9, label: 'x', matches: [] }],
  }, false);
  assertEqual(both.rounds[0].round_index, 5, 'an explicit round_index is not overwritten by stage_index');
});

// ---- DOUBLE-ELIM DROP ROUTING (≥2 losers demoted: clean, non-crossing) ----
//
// Regression guard for the WB→LB demotion connectors. They used to dip a half-row
// beneath the SOURCE lane and run their horizontal bus THERE — straight across the
// rows (dots / labels / boxes) of every lane physically between the WB column and
// the LB re-entry column; with TWO losers demoted from one node the two buses
// straddled the intervening lanes and crossed each other. The fix routes every
// demotion through a reserved CHANNEL below the whole lane stack, one horizontal
// lane per drop, with a per-edge x-nudge so two drops that share a source column
// never share a vertical. This test renders a double-elim where TWO losers drop
// from the SAME WB column into the SAME LB column (the two-loser case) and asserts:
//   * every horizontal run of every drop path sits BELOW the bottom lane row (in
//     the channel) — so no run can cross a node box / label / dot,
//   * each drop owns a DISTINCT channel run-y (its own lane),
//   * the two same-column drops have DISTINCT source verticals (parallel, no overlap),
//   * the path endpoints still land on the WB-loss dot and the LB-entry node
//     (the visual connection is intact).

// Parse an elbow/channel drop `d` into its absolute vertices (M / L / Q endpoints).
function pathPoints(d) {
  const pts = [];
  const re = /([MLQ])\s*([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+)\s+([-\d.]+))?/g;
  let m;
  while ((m = re.exec(String(d || '')))) {
    if (m[1] === 'Q') pts.push({ x: Number(m[4]), y: Number(m[5]) });  // arc endpoint
    else pts.push({ x: Number(m[2]), y: Number(m[3]) });
  }
  return pts;
}
// the (near-)horizontal runs of a path: consecutive vertices with ~equal y.
function horizontalRuns(pts) {
  const runs = [];
  for (let i = 1; i < pts.length; i++) {
    if (Math.abs(pts[i].y - pts[i - 1].y) < 0.5 && Math.abs(pts[i].x - pts[i - 1].x) > 0.5) {
      runs.push({ y: pts[i].y, x1: Math.min(pts[i].x, pts[i - 1].x), x2: Math.max(pts[i].x, pts[i - 1].x) });
    }
  }
  return runs;
}
// the longest horizontal run (the channel "bus" run, not a tiny jog).
function busRun(pts) {
  return horizontalRuns(pts).sort((a, b) => (b.x2 - b.x1) - (a.x2 - a.x1))[0] || null;
}

test('double-elim drop routing: ≥2 losers demoted route in their own channel below the stack — no run crosses a node, distinct lanes, no overlap', () => {
  // Two losers (v2, v4) drop from the SAME WB column (WB-R0) into the SAME LB
  // column (LB-R2); a third (v3) drops WB-R1 → LB-R3. Three demotion edges total,
  // two of them sharing both endpoints' columns — the worst case for crossing.
  const st = {
    structure: 'double_elim',
    champion_lineage: ['v0', 'v1'],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }, { generation_id: 'v4' }],
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
  };
  const model = structure.elimModel(st);
  const flow = svg.elimFlow({ winners: model.winners.concat(model.losers), championId: model.championId, benchmarkId: model.benchmarkId, gateState: model.gateState });

  // collect the drop paths (raw `d`) so we can inspect their channel routing.
  const drops = [];
  walk(flow, (n) => {
    const list = clsOf(n).split(/\s+/);
    if (n.localName === 'path' && list.includes('dn-elimflow-seg-drop')) drops.push(n.getAttribute('d'));
  });
  assert(drops.length === 3, `three WB→LB demotion edges render (got ${drops.length})`);

  // the bottom lane row: the lowest lane y the renderer placed a competitor on.
  const f = readFlow(flow);
  const bottomLaneY = Math.max(...f.lanes.map((l) => l.y));   // label baseline ≈ lane y + 3.2
  const lastRowBottom = bottomLaneY + laneH() / 2;            // bottom edge of the last row's box

  // (1) the long channel BUS run of EVERY drop path sits BELOW the last lane row —
  // i.e. inside the reserved channel, so it cannot cross any node box / label / dot.
  // (The only other horizontals are the tiny source/target jogs, which sit just
  // below their OWN dot in the inter-row gap; the spanning run is what could cross.)
  for (const d of drops) {
    const pts = pathPoints(d);
    for (const run of horizontalRuns(pts)) {
      if (run.x2 - run.x1 > laneH()) {
        assert(run.y > lastRowBottom, `a drop's channel run (y=${run.y}) is below the last lane row (${lastRowBottom}) — never across a node`);
      }
    }
  }

  // (2) each drop owns a DISTINCT channel run-y (its own lane — no shared bus).
  const busYs = drops.map((d) => busRun(pathPoints(d)).y);
  const uniqYs = new Set(busYs.map((y) => y.toFixed(1)));
  assert(uniqYs.size === drops.length, `each demotion runs on its OWN channel lane (distinct run-y); got ${[...uniqYs].join(', ')}`);

  // (3) the TWO losers sharing the SAME WB→LB columns (v2, v4: c0→c2) have
  // DISTINCT source verticals — parallel pipes, never one overlapping vertical.
  const sameCol = drops.filter((d) => {
    const ep = dropPathEndpoints(d);
    return ep && Math.abs(ep.x1 - colX(0)) < 1 && Math.abs(ep.x2 - colX(2)) < 1;
  });
  assert(sameCol.length === 2, 'two losers demote from WB-R0 into LB-R2 (the two-loser case)');
  // the nudged source-vertical x (the x the path settles onto after the initial jog
  // off the dot: the 4th vertex of the channel grammar); the per-edge nudge makes
  // the two differ.
  const srcVertX = (d) => {
    const pts = pathPoints(d);
    return pts.length > 3 ? pts[3].x : pts[0].x;
  };
  const [ax, bx] = sameCol.map(srcVertX);
  assert(Math.abs(ax - bx) > 1.5, `the two same-column drops have DISTINCT source verticals (${ax} vs ${bx}) — no overlap`);

  // (4) the path ENDPOINTS still land on the WB-loss dot and the LB-entry node, so
  // the connection is intact (the routing change did not orphan any lane).
  assert(segFromTo(flow, 'v2', colX(0), colX(2)) && segFromTo(flow, 'v2', colX(0), colX(2)).drop, 'v2 WB-R0→LB-R2 drop still connects dot→node');
  assert(segFromTo(flow, 'v4', colX(0), colX(2)) && segFromTo(flow, 'v4', colX(0), colX(2)).drop, 'v4 WB-R0→LB-R2 drop still connects dot→node');
  assert(segFromTo(flow, 'v3', colX(1), colX(3)) && segFromTo(flow, 'v3', colX(1), colX(3)).drop, 'v3 WB-R1→LB-R3 drop still connects dot→node');
});

await run();
