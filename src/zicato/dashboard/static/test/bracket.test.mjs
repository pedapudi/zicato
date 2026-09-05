// test/bracket.test.mjs — the radial elimination bracket's connectivity guard.
//
// The operator must be able to trace who advanced, who was eliminated where,
// and who dropped into the losers' bracket from the rendered figure alone. The
// properties this pins (all in svg.elimRadial over the served elimination
// model `elimModel` shapes):
//
//   * RING ORDER — rings are the served rounds in payload order, outermost
//     first, narrowing to the champion seat; every survival segment runs
//     INWARD (from an outer ring to the next ring in), never outward.
//   * WINNERS→LOSERS DROP — a generation that loses in the winners' bracket is
//     NOT eliminated (it gets a second life in the losers' bracket). Its spoke
//     sits on the lower (losers') arc, a transfer arc ends on its outer node
//     and starts on that node's equator mirror, it draws no phantom ✕ for the
//     first loss, and its TRUE elimination ✕ sits at its LAST (losers') loss.
//   * BYE — a competitor with a bye advances cleanly and does not desync the
//     ring/spoke mapping of the other spokes.
//   * LIVE vs SETTLED — the live `pending` final maps the same as a settled one
//     (the in-flight spoke dashes, nothing is falsely eliminated).
//
// The assertions read the rendered SVG geometry directly (ring radii, spoke
// segment endpoints, node tones, ✕ marks, transfer-arc endpoints) so a
// regression that strands a spoke or orphans a drop fails here.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';
import { elimPayload } from './recorded.mjs';

installDom();

const svg = await import('../js/svg.js');
const structure = await import('../js/tournament_model.js');

// ---- SVG geometry readers ------------------------------------------------

function walk(node, fn) {
  fn(node);
  for (const c of (node.childNodes || [])) if (c.nodeType === 1) walk(c, fn);
}
function clsOf(n) { return ((n.getAttribute && n.getAttribute('class')) || '').split(/\s+/); }
function num(n, a) { const v = n.getAttribute(a); return v == null ? null : Number(v); }

// Read the rendered radial into a structured, assertable shape. Ring k is the
// k-th ring circle from the outside (ring 0 = the outer node ring, ring k =
// "entered round k"); the champion seat is the centre. A point maps to the
// ring whose radius is nearest to its distance from the centre.
function readRadial(node) {
  const rings = [];      // ring radii, outermost first
  let cx = null; let cy = null;
  walk(node, (n) => {
    if (clsOf(n).includes('dn-elimradial-ring')) {
      rings.push(num(n, 'r'));
      if (cx == null) { cx = num(n, 'cx'); cy = num(n, 'cy'); }
    }
  });
  rings.sort((a, b) => b - a);
  const ringOf = (x, y) => {
    const r = Math.hypot(x - cx, y - cy);
    if (r < 1) return 'centre';
    let best = 0;
    rings.forEach((rr, k) => { if (Math.abs(rr - r) < Math.abs(rings[best] - r)) best = k; });
    // a radius past the innermost ring (the seat) is the centre gate.
    if (r < rings[rings.length - 1] - 2) return 'centre';
    return best;
  };
  const spokes = [];     // { id, side, segs:[{from,to,tone}], nodes:[{ring,tone}], cut:ring|null, gate:bool, label }
  const transfers = [];  // { start:{x,y}, end:{x,y}, r }
  let equator = false;
  walk(node, (n) => {
    const cls = clsOf(n);
    if (cls.includes('dn-elimradial-equator')) equator = true;
    if (cls.includes('dn-elimradial-transfer')) {
      const m = (n.getAttribute('d') || '').match(/^M([-\d.]+) ([-\d.]+) A([\d.]+) [\d.]+ 0 \d \d ([-\d.]+) ([-\d.]+)$/);
      assert(m, `a transfer arc path parses (d=${n.getAttribute('d')})`);
      transfers.push({ start: { x: Number(m[1]), y: Number(m[2]) }, end: { x: Number(m[4]), y: Number(m[5]) }, r: Number(m[3]) });
    }
    if (!cls.includes('dn-elimradial-spoke')) return;
    const sp = { id: null, label: '', segs: [], nodes: [], cut: null, gate: false, side: null, outer: null };
    walk(n, (c) => {
      const cc = clsOf(c);
      const tone = cc.includes('dn-good') ? 'good' : cc.includes('dn-bad') ? 'bad'
        : cc.includes('dn-elimradial-pending') ? 'pending' : cc.includes('dn-elimradial-gateline') ? 'gate' : 'none';
      if (cc.includes('dn-elimradial-seg')) {
        const from = ringOf(num(c, 'x1'), num(c, 'y1'));
        const to = ringOf(num(c, 'x2'), num(c, 'y2'));
        sp.segs.push({ from, to, tone });
        if (tone === 'gate') sp.gate = true;
      } else if (cc.includes('dn-elimradial-node')) {
        const ring = ringOf(num(c, 'cx'), num(c, 'cy'));
        sp.nodes.push({ ring, tone });
        if (ring === 0) sp.outer = { x: num(c, 'cx'), y: num(c, 'cy') };
      } else if (cc.includes('dn-elimradial-cut')) {
        sp.cut = ringOf(num(c, 'x'), num(c, 'y') - 3.2);
      } else if (cc.includes('dn-elimradial-name')) {
        sp.label = c.textContent;
        sp.id = String(c.textContent).trim().split(/\s+/)[0];
      }
    });
    sp.side = sp.outer ? (sp.outer.y < cy ? 'upper' : 'lower') : null;
    spokes.push(sp);
  });
  return { cx, cy, rings, spokes, transfers, equator, byId: (id) => spokes.find((s) => s.id === id) || null };
}

// the rings a spoke survived: the good segments as [from, to] ring pairs.
function goodSegs(sp) { return sp.segs.filter((s) => s.tone === 'good'); }
// did the spoke survive round k (a good segment from ring k to ring k+1)?
function survived(sp, k) { return goodSegs(sp).some((s) => s.from === k && s.to === k + 1); }
function radial(model, extra) {
  return svg.elimRadial({
    rounds: model.rounds, gen_states: model.gen_states, championId: model.championId,
    benchmarkId: model.benchmarkId, gateState: model.gateState, ...(extra || {}),
  });
}

// ---- SINGLE-ELIM (settled, with a champion crowned) ----------------------

test('single-elim radial: each spoke survives inward ring by ring, the loser ends with ✕ at its loss, the champion dashes into the seat', () => {
  // v0 = champion (benchmark). 4 challengers v1..v4.
  //   R0: WB-R0-0 v1>v2, WB-R0-1 v3>v4
  //   R1: WB-R1-0 v1>v3
  //   Final: v0 vs v1 → v1 promoted (crowned).
  const st = {
    structure: 'single_elim',
    champion_lineage: ['v0', 'v1'],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }, { generation_id: 'v4' }],
  };
  const model = structure.elimModel(elimPayload('single_elim_four_crowned', st));
  assertEqual(model.championId, 'v1', 'v1 is crowned');
  assertEqual(model.benchmarkId, 'v0', 'v0 is the benchmark/incumbent');
  assertEqual(model.gateState, 'crowned', 'the gate crowned the survivor');
  assert(model.losers == null, 'single-elim has no losers band');
  const f = readRadial(radial(model));

  // rings: one per served round + the centre gate ring.
  assertEqual(f.rings.length, 4, 'three rounds draw three rings plus the gate ring');
  assertEqual(f.spokes.length, 5, 'one spoke per generation');
  assert(!f.equator, 'single-elim draws no equator');

  // v1 advances every round → a continuous good chain into the seat.
  const v1 = f.byId('v1');
  assert(survived(v1, 0) && survived(v1, 1) && survived(v1, 2), 'v1 survives R0, R1 and the final (good segments ring 0→1→2→3)');
  assert(v1.gate, 'v1 dashes from the innermost ring into the champion seat');
  assertEqual(v1.cut, null, 'the champion is never marked eliminated');
  assert(v1.label.includes(svg.CROWN.current), 'the champion spoke carries the current crown');

  // v3 wins R0 then loses R1: its true (and only) elimination ✕ is at R1.
  const v3 = f.byId('v3');
  assert(survived(v3, 0), 'v3 survived R0');
  assert(!survived(v3, 1), 'v3 did not survive R1');
  assert(v3.nodes.some((n) => n.ring === 1 && n.tone === 'bad'), 'v3 lost at ring 1 (bad node)');
  assertEqual(v3.cut, 2, 'v3 eliminated at R1 (the ✕ caps the loss segment leaving ring 1)');
  assert(!v3.gate, 'an eliminated spoke never reaches the seat');

  // v2 loses R0 immediately → no survival segment, ✕ at its first ring.
  const v2 = f.byId('v2');
  assertEqual(goodSegs(v2).length, 0, 'a first-round loser survives no ring');
  assert(v2.nodes.some((n) => n.ring === 0 && n.tone === 'bad'), 'v2 lost at ring 0');
  assertEqual(v2.cut, 1, 'v2 eliminated at R0');

  // the two R0 feeder matches converge into the single R1 match — a winner from
  // each (v1, v3) carries forward and they MEET at ring 1; v2 & v4 stay outside.
  assert(v1.nodes.some((n) => n.ring === 1) && v3.nodes.some((n) => n.ring === 1), 'both R0 winners carry a node on ring 1 (the shared next match)');
  assert(!v2.nodes.some((n) => n.ring === 1) && !f.byId('v4').nodes.some((n) => n.ring === 1), 'R0 losers carry no node on ring 1');

  // every survival segment runs inward (outer ring → the next ring in).
  for (const sp of f.spokes) for (const s of goodSegs(sp)) assert(s.to === s.from + 1, `${sp.id}: a survival segment steps one ring inward (${s.from}→${s.to})`);
  // the displaced incumbent reads the former crown.
  assert(f.byId('v0').label.includes(svg.CROWN.former), 'the displaced incumbent v0 reads ♔');
});

// ---- DOUBLE-ELIM (settled, with a losers-bracket drop) -------------------

test('double-elim radial: winners on the upper arc, losers on the lower, drops as transfer arcs onto real nodes, true eliminations at the second loss', () => {
  // v0 = champion. v1..v4 challengers.
  //   WB: WB-R0-0 v1>v2 (v2 drops), WB-R0-1 v3>v4 (v4 drops); WB-R1-0 v1>v3 (v3 drops). v1 = WB survivor.
  //   LB: LB-R2-0 v2>v4 (v4 OUT), LB-R3-0 v2>v3 (v3 OUT). v2 = LB survivor.
  //   GF: v0 vs v1 → v1 promoted.
  const st = {
    structure: 'double_elim',
    champion_lineage: ['v0', 'v1'],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }, { generation_id: 'v4' }],
  };
  const model = structure.elimModel(elimPayload('double_elim_crowned', st));
  assertEqual(model.championId, 'v1', 'v1 promoted at the grand final');
  assertEqual(model.benchmarkId, 'v0', 'v0 is the benchmark/incumbent');
  assert(Array.isArray(model.losers) && model.losers.length === 2, 'the losers bracket has two rounds');
  const f = readRadial(radial(model, { double: true }));

  // RINGS by served round order: 0=WB-R0, 1=WB-R1, 2=LB-R2, 3=LB-R3, 4=GF, then the gate ring.
  assertEqual(f.rings.length, 6, 'five served rounds draw five rings plus the gate ring');
  assert(f.equator, 'double-elim draws the dashed equator between the two arcs');

  // v1: never enters the losers' bracket → upper arc; survives to the seat.
  const v1 = f.byId('v1');
  assertEqual(v1.side, 'upper', 'v1 sits on the upper (winners\') arc');
  assert([0, 1, 2, 3, 4].every((k) => survived(v1, k)), 'v1 survives every ring inward to the gate ring');
  assert(v1.gate && v1.cut == null, 'v1 dashes into the seat and is never cut');

  // v0 plays ONLY the grand final and loses it → its ✕ caps the GF ring.
  const v0 = f.byId('v0');
  assertEqual(v0.side, 'upper', 'the incumbent never drops, so it sits on the upper arc');
  assert(v0.nodes.some((n) => n.ring === 4 && n.tone === 'bad'), 'v0 lost at the grand final ring');
  assertEqual(v0.cut, 5, 'v0 (incumbent) eliminated at the GF');

  // v2 — the DROP case: loses WB-R0, drops to the LB, wins LB-R2 + LB-R3.
  const v2 = f.byId('v2');
  assertEqual(v2.side, 'lower', 'a dropped lane sits on the lower (losers\') arc');
  assertEqual(v2.cut, null, 'the WB loss is NOT a phantom elimination — v2 has a second life and is never cut');
  assert(!v2.nodes.some((n) => n.tone === 'bad'), 'the LB survivor carries no loss node');
  assert(survived(v2, 2) && survived(v2, 3), 'v2 survives both losers\' rounds (rings 2→3→4)');

  // v3 — drops then is OUT: wins WB-R0, loses WB-R1 (DROP), loses LB-R3 (ELIMINATED).
  const v3 = f.byId('v3');
  assertEqual(v3.side, 'lower', 'v3 dropped, so it sits on the lower arc');
  assert(survived(v3, 0) && survived(v3, 1) && survived(v3, 2), 'v3\'s spoke reaches the LB-R3 ring: the WB-R1 loss is a drop, not a ✕');
  assertEqual(v3.cut, 4, "v3's TRUE elimination ✕ caps its SECOND loss (LB-R3), not the WB");

  // v4 — drops then OUT at the FIRST losers round: loses WB-R0, loses LB-R2.
  const v4 = f.byId('v4');
  assertEqual(v4.side, 'lower', 'v4 dropped, so it sits on the lower arc');
  assert(survived(v4, 0) && survived(v4, 1), 'v4\'s spoke reaches the LB-R2 ring (its WB-R0 loss is a drop)');
  assertEqual(v4.cut, 3, 'v4 eliminated at LB-R2 (its second loss)');

  // one transfer arc per drop, each anchored on the dropped spoke's outer node
  // and starting on that node's equator mirror on the upper arc.
  assertEqual(f.transfers.length, 3, 'three WB→LB drops draw three transfer arcs');
  for (const id of ['v2', 'v3', 'v4']) {
    const sp = f.byId(id);
    const arc = f.transfers.find((t) => Math.hypot(t.end.x - sp.outer.x, t.end.y - sp.outer.y) < 0.6);
    assert(arc, `${id}: a transfer arc ends exactly on its outer (losers\'-bracket entry) node`);
    assert(arc.start.y < f.cy && arc.end.y > f.cy, `${id}: the arc runs from the upper arc down to the lower arc`);
    assert(Math.abs(arc.start.x - arc.end.x) < 0.2 && Math.abs((arc.start.y - f.cy) + (arc.end.y - f.cy)) < 0.2,
      `${id}: the arc starts on the equator mirror of its LB node`);
  }
  // every survival segment steps inward.
  for (const sp of f.spokes) for (const s of goodSegs(sp)) assert(s.to === s.from + 1, `${sp.id}: a survival segment steps one ring inward (${s.from}→${s.to})`);
});

// ---- BYE (settled) -------------------------------------------------------

test('single-elim radial: a BYE advances cleanly and does not desync the ring/spoke mapping', () => {
  // 3 challengers: R0 v1>v2, v3 BYE. R1 v1 vs v3 → v3 wins. Final v0 vs v3 → champion stands.
  const st = {
    structure: 'single_elim',
    champion_lineage: ['v0'],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }],
  };
  const model = structure.elimModel(elimPayload('single_elim_bye_stands', st));
  assertEqual(model.gateState, 'stands', 'the survivor lost the gate — champion stands');
  assertEqual(model.championId, null, 'no new champion crowned');
  const f = readRadial(radial(model));

  // v3's BYE: it survives R0 without a loss node and reaches its real R1 match.
  const v3 = f.byId('v3');
  assert(survived(v3, 0), 'v3 advances via the bye (survives ring 0→1)');
  assert(!v3.nodes.some((n) => n.ring === 0 && n.tone === 'bad'), 'a bye is not a loss');
  assert(survived(v3, 1), 'v3 won R1 (survives ring 1→2)');
  // v3 reaches the final and loses it → its only ✕ caps the final ring.
  assert(v3.nodes.some((n) => n.ring === 2 && n.tone === 'bad'), 'v3 lost the final');
  assertEqual(v3.cut, 3, 'v3 eliminated at the final');

  // v1 is unaffected by v3's bye: won R0, lost R1.
  const v1 = f.byId('v1');
  assert(survived(v1, 0), 'v1 won R0');
  assert(v1.nodes.some((n) => n.ring === 1 && n.tone === 'bad'), 'v1 lost R1');
  assertEqual(v1.cut, 2, 'v1 eliminated at R1 (the bye did not shift its mapping)');
  // v2 lost R0.
  assertEqual(f.byId('v2').cut, 1, 'v2 eliminated at R0');
  // nobody is crowned: no spoke dashes into the seat and the seat reads the former crown.
  assert(f.spokes.every((s) => !s.gate), 'no spoke reaches the seat while the champion stands');
});

// ---- LIVE vs SETTLED -----------------------------------------------------

test('live elim radial: the in-flight (pending) final maps the same as a settled one — nothing falsely eliminated', () => {
  // R0 v1>v2 settled; the Final v0 vs v1 is IN FLIGHT (winner:null, pending).
  const live = {
    structure: 'single_elim', live: true, phase: 'running',
    champion_lineage: ['v0'],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }],
  };
  const lm = structure.elimModel(structure.normalizeStructure(elimPayload('single_elim_final_pending', live), true));
  assert(lm.live, 'the model is live');
  assertEqual(lm.gateState, 'deciding', 'a live bracket is deciding');
  const f = readRadial(radial(lm, { live: true }));

  // v1 won R0 (good), races the pending final (a dashed spoke), never cut.
  const v1 = f.byId('v1');
  assert(survived(v1, 0), 'v1 won R0 (good)');
  assert(v1.segs.some((s) => s.tone === 'pending'), 'v1 is racing the in-flight final (a dashed pending segment, not falsely won/lost)');
  assert(v1.cut == null && !v1.gate, 'v1 is neither cut nor committed to the seat while the final is in flight');
  // v0 sits ONLY at the pending final — never falsely eliminated.
  const v0 = f.byId('v0');
  assert(v0.segs.some((s) => s.tone === 'pending'), 'v0 races the pending final (dashed)');
  assert(v0.cut == null && !v0.nodes.some((n) => n.tone === 'bad'), 'v0 is not eliminated while the final is in flight');
  // v2 lost R0 → its (settled) elimination still reads correctly under a live run.
  const v2 = f.byId('v2');
  assertEqual(goodSegs(v2).length, 0, 'v2 survived no ring');
  assertEqual(v2.cut, 1, 'a settled R0 loss is still a clean elimination during a live run');
});

// The persisted within-tournament stage key is `stage_index`; normalizeStructure
// maps it to the renderer's internal `round_index`, while still accepting the
// `round_index` key, which a workspace written under that name still uses.
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

// ---- DOUBLE-ELIM DROP ROUTING (≥2 losers demoted: nested, non-overlapping) ----
//
// Regression guard for the WB→LB transfer arcs. Two losers demoted from the
// SAME winners' column into the SAME losers' column (the two-loser case) plus
// a third drop from the next column: every arc must start and end ON the outer
// node ring (never on a staggered rim outside it, where no node exists), end
// exactly on its own losers'-bracket entry node, and the arcs must nest on
// DISTINCT radii so no two overlap.

test('double-elim drop routing: ≥2 losers demoted draw nested transfer arcs — each anchored on real nodes, distinct radii, no overlap', () => {
  // Two losers (v2, v4) drop from the SAME WB column (WB-R0) into the SAME LB
  // column (LB-R2); a third (v3) drops WB-R1 → LB-R3. Three demotion arcs total.
  const st = {
    structure: 'double_elim',
    champion_lineage: ['v0', 'v1'],
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v1' }, { generation_id: 'v2' }, { generation_id: 'v3' }, { generation_id: 'v4' }],
  };
  const model = structure.elimModel(elimPayload('double_elim_crowned', st));
  const f = readRadial(radial(model, { double: true }));
  assertEqual(f.transfers.length, 3, `three WB→LB demotion arcs render (got ${f.transfers.length})`);

  // (1) BOTH endpoints of every arc sit ON the outer node ring.
  const nodeR = f.rings[0];
  for (const t of f.transfers) {
    assert(Math.abs(Math.hypot(t.start.x - f.cx, t.start.y - f.cy) - nodeR) < 0.6, 'an arc START sits on the outer node ring');
    assert(Math.abs(Math.hypot(t.end.x - f.cx, t.end.y - f.cy) - nodeR) < 0.6, 'an arc END sits on the outer node ring');
  }
  // (2) each arc owns a DISTINCT radius — the arcs nest rather than overlap.
  const radii = new Set(f.transfers.map((t) => t.r.toFixed(1)));
  assertEqual(radii.size, 3, `each demotion arc rides its OWN radius; got ${[...radii].join(', ')}`);
  // (3) the two losers sharing the SAME WB→LB columns (v2, v4) each have an arc
  // ending on their OWN outer node — two arcs, never one shared connector.
  const ends = ['v2', 'v4'].map((id) => f.byId(id).outer);
  assert(Math.hypot(ends[0].x - ends[1].x, ends[0].y - ends[1].y) > 5, 'v2 and v4 own distinct outer nodes');
  for (const id of ['v2', 'v3', 'v4']) {
    const o = f.byId(id).outer;
    assert(f.transfers.some((t) => Math.hypot(t.end.x - o.x, t.end.y - o.y) < 0.6), `${id}: a transfer arc terminates exactly on its losers'-bracket entry node`);
  }
});

await run();
