// variants/C/diagram/topology.js — tournament-style layouts (theme 4).
//
// THE showcase. One candidate set, five DIFFERENT graph topologies. Each
// function is PURE layout: given the same `cands` (an array of node specs
// { id, role, ... }) it returns positioned nodes + edges + a bounding box
// in world coordinates, in the SAME { nodes, edges, box } shape, so the
// view can cross-fade between them by re-running layout on the same ids.
//
// Only the gauntlet carries REAL paired per-board data (champion at the
// hub, real edges from /api/tournaments). The other four are honest
// CONCEPTUAL overlays of the same generations under documented selection
// structures (SELECTION.md §2/§5/§6) — the view labels them as such.
//
//   node: { id, x, y, r?, w?, h?, label, sub, cls, role }
//   edge: { id, x1, y1, x2, y2, cls, label?, kind? }
//
// `cls` is a verdict/role class the view maps to colour; layout never
// touches colour.

const SEED = (c) => c.role === 'champion' || c.role === 'seed';

// ---- ① gauntlet — king-of-the-hill star/hub (REAL) ------------------
//
// The reigning champion sits at the centre; every challenger is a spoke,
// the edge carrying the round's verdict + delta. This is the actual
// shipped structure: one defender, N challengers, paired duels.
export function layoutGauntlet(cands, opts = {}) {
  const cx = opts.cx || 480;
  const cy = opts.cy || 250;
  const radius = opts.radius || 180;
  const champ = cands.find(SEED) || cands[0] || null;
  const challengers = cands.filter((c) => c !== champ);
  const nodes = [];
  const edges = [];
  if (champ) {
    nodes.push({ id: champ.id, x: cx, y: cy, r: 40, label: champ.id, sub: 'champion', cls: champ.cls || 'cz-v-promoted', role: 'champion' });
  }
  const n = Math.max(1, challengers.length);
  challengers.forEach((c, i) => {
    // Spread challengers over an arc so spokes never overlap.
    const span = n === 1 ? 0 : Math.PI * 1.25;
    const a = -span / 2 + (n === 1 ? 0 : (span * i) / (n - 1));
    const x = cx + Math.cos(a) * radius;
    const y = cy + Math.sin(a) * radius;
    nodes.push({ id: c.id, x, y, r: 30, label: c.id, sub: c.decision || 'challenger', cls: c.cls || 'cz-v-rejected', role: 'challenger' });
    if (champ) {
      edges.push({
        id: 'g:' + c.id, x1: cx, y1: cy, x2: x, y2: y,
        cls: c.cls || 'cz-edge-rejected', label: c.deltaLabel || null, kind: 'duel',
      });
    }
  });
  return { nodes, edges, box: { x: cx - radius - 60, y: cy - radius - 60, w: 2 * (radius + 60), h: 2 * (radius + 60) } };
}

// ---- ② single-elimination — binary bracket tree (conceptual) --------
//
// Seed the field into a left column; winners advance rightward, one node
// per match, to a single final. Pure power-of-two padding with byes.
export function layoutSingleElim(cands, opts = {}) {
  const colW = opts.colW || 200;
  const rowH = opts.rowH || 70;
  const top = opts.top || 30;
  const left = opts.left || 30;
  const field = cands.slice();
  // Pad to next power of two with bye slots.
  let size = 1;
  while (size < Math.max(2, field.length)) size *= 2;
  const slots = field.slice();
  while (slots.length < size) slots.push(null);

  const nodes = [];
  const edges = [];
  const rounds = Math.log2(size);
  // Round 0: the seeds.
  let prev = slots.map((c, i) => {
    const x = left;
    const y = top + i * rowH;
    const id = c ? c.id : 'bye:' + i;
    nodes.push({ id, x, y, w: 130, h: 40, label: c ? c.id : 'bye', sub: c ? (c.role === 'champion' ? 'top seed' : 'seed') : 'bye', cls: c ? (c.cls || 'cz-v-neutral') : 'cz-v-baseline', role: 'seed' });
    return { id, x, y, cand: c };
  });
  // Advance rounds: the higher-seeded (champion, else first) "wins".
  for (let r = 1; r <= rounds; r++) {
    const x = left + r * colW;
    const next = [];
    for (let i = 0; i < prev.length; i += 2) {
      const a = prev[i];
      const b = prev[i + 1];
      const y = (a.y + b.y) / 2;
      const winner = pickWinner(a.cand, b.cand);
      const id = 'se:r' + r + ':' + i;
      nodes.push({ id, x, y, w: 130, h: 40, label: winner ? winner.id : 'tbd', sub: r === rounds ? 'champion' : 'advances', cls: winner ? (winner.cls || 'cz-v-neutral') : 'cz-v-neutral', role: r === rounds ? 'final' : 'match' });
      edges.push({ id: id + ':a', x1: a.x + 130, y1: a.y + 20, x2: x, y2: y + 20, cls: 'cz-edge-rejected', kind: 'advance' });
      edges.push({ id: id + ':b', x1: b.x + 130, y1: b.y + 20, x2: x, y2: y + 20, cls: 'cz-edge-rejected', kind: 'advance' });
      next.push({ id, x, y, cand: winner });
    }
    prev = next;
  }
  const w = left + (rounds + 1) * colW;
  const h = top + size * rowH;
  return { nodes, edges, box: { x: 0, y: 0, w, h } };
}

// ---- ③ double-elimination — two coupled trees (conceptual) ----------
//
// A winners' bracket on top, a losers' bracket below; the loser of a
// winners' match drops into the losers' bracket (the "second life").
export function layoutDoubleElim(cands, opts = {}) {
  const colW = opts.colW || 190;
  const rowH = opts.rowH || 64;
  const top = opts.top || 24;
  const left = opts.left || 30;
  const field = cands.slice();
  let size = 1;
  while (size < Math.max(2, field.length)) size *= 2;
  const slots = field.slice();
  while (slots.length < size) slots.push(null);

  const nodes = [];
  const edges = [];

  // Winners' bracket (upper half).
  let prev = slots.map((c, i) => {
    const x = left; const y = top + i * rowH;
    const id = c ? c.id : 'wbye:' + i;
    nodes.push({ id, x, y, w: 120, h: 36, label: c ? c.id : 'bye', sub: 'WB seed', cls: c ? (c.cls || 'cz-v-neutral') : 'cz-v-baseline', role: 'wb' });
    return { id, x, y, cand: c };
  });
  const rounds = Math.log2(size);
  const losersDrop = [];
  for (let r = 1; r <= rounds; r++) {
    const x = left + r * colW;
    const next = [];
    for (let i = 0; i < prev.length; i += 2) {
      const a = prev[i]; const b = prev[i + 1];
      const y = (a.y + b.y) / 2;
      const winner = pickWinner(a.cand, b.cand);
      const loser = winner === a.cand ? b.cand : a.cand;
      const id = 'de:wb:r' + r + ':' + i;
      nodes.push({ id, x, y, w: 120, h: 36, label: winner ? winner.id : 'tbd', sub: 'WB', cls: winner ? (winner.cls || 'cz-v-neutral') : 'cz-v-neutral', role: 'wb' });
      edges.push({ id: id + ':a', x1: a.x + 120, y1: a.y + 18, x2: x, y2: y + 18, cls: 'cz-edge-promoted', kind: 'wb' });
      edges.push({ id: id + ':b', x1: b.x + 120, y1: b.y + 18, x2: x, y2: y + 18, cls: 'cz-edge-promoted', kind: 'wb' });
      if (loser) losersDrop.push({ cand: loser, fromX: x, fromY: y, round: r });
      next.push({ id, x, y, cand: winner });
    }
    prev = next;
  }

  // Losers' bracket (lower half) — a simplified linear consolation chain.
  const lbY = top + size * rowH + 60;
  let lbPrev = null;
  losersDrop.forEach((d, i) => {
    const x = left + (i + 0.5) * colW;
    const id = 'de:lb:' + i;
    const c = d.cand;
    nodes.push({ id, x, y: lbY, w: 120, h: 36, label: c ? c.id : 'tbd', sub: 'LB · second life', cls: c ? (c.cls || 'cz-v-rejected') : 'cz-v-neutral', role: 'lb' });
    // Coupling edge: the WB loser drops into the LB.
    edges.push({ id: id + ':drop', x1: d.fromX, y1: d.fromY + 18, x2: x + 60, y2: lbY, cls: 'cz-edge-cross', kind: 'drop' });
    if (lbPrev) edges.push({ id: id + ':adv', x1: lbPrev.x + 120, y1: lbY + 18, x2: x, y2: lbY + 18, cls: 'cz-edge-rejected', kind: 'lb' });
    lbPrev = { x };
  });

  const w = left + (rounds + 1) * colW;
  const h = lbY + rowH + 30;
  return { nodes, edges, box: { x: 0, y: 0, w, h } };
}

// ---- ④ Swiss — round-by-round bipartite pairing (conceptual) --------
//
// Every candidate plays every round; pairings are drawn as a bipartite
// graph between adjacent round columns. No elimination — full ranking.
export function layoutSwiss(cands, opts = {}) {
  const colW = opts.colW || 230;
  const rowH = opts.rowH || 64;
  const top = opts.top || 40;
  const left = opts.left || 40;
  const field = cands.filter(Boolean);
  const k = field.length;
  // ceil(log2 k) rounds is the Swiss convention to resolve a ranking.
  const rounds = Math.max(1, Math.ceil(Math.log2(Math.max(2, k))));
  const nodes = [];
  const edges = [];
  for (let r = 0; r <= rounds; r++) {
    const x = left + r * colW;
    field.forEach((c, i) => {
      const y = top + i * rowH;
      const id = 'sw:r' + r + ':' + c.id;
      nodes.push({ id, x, y, w: 110, h: 38, label: c.id, sub: r === 0 ? 'entry' : 'round ' + r, cls: c.cls || 'cz-v-neutral', role: 'swiss' });
    });
    if (r < rounds) {
      // Pair adjacent ranks (i, i+1) — the Swiss "play someone on your score".
      for (let i = 0; i + 1 < field.length; i += 2) {
        const a = field[i]; const b = field[i + 1];
        const ya = top + i * rowH; const yb = top + (i + 1) * rowH;
        const nx = left + (r + 1) * colW;
        edges.push({ id: 'sw:p:' + r + ':' + i + ':a', x1: x + 110, y1: ya + 19, x2: nx, y2: ya + 19, cls: 'cz-edge-rejected', kind: 'swiss' });
        edges.push({ id: 'sw:p:' + r + ':' + i + ':b', x1: x + 110, y1: yb + 19, x2: nx, y2: yb + 19, cls: 'cz-edge-rejected', kind: 'swiss' });
        // The pairing link itself.
        edges.push({ id: 'sw:m:' + r + ':' + i, x1: x + 55, y1: ya + 38, x2: x + 55, y2: yb, cls: 'cz-edge-cross', kind: 'pair' });
        void a; void b;
      }
    }
  }
  const w = left + (rounds + 1) * colW;
  const h = top + k * rowH + 20;
  return { nodes, edges, box: { x: 0, y: 0, w, h } };
}

// ---- ⑤ racing / successive-halving — parallel lanes (conceptual) ----
//
// Every candidate is a horizontal lane; confidence bands tighten left→
// right as instances accumulate; an elimination cut-line drops the worst
// lanes at each rung. zicato's recommended direction (SELECTION.md §5/§7).
export function layoutRacing(cands, opts = {}) {
  const laneH = opts.laneH || 70;
  const top = opts.top || 50;
  const left = opts.left || 120;
  const trackW = opts.trackW || 620;
  const field = cands.filter(Boolean);
  const nodes = [];
  const edges = [];
  const rungs = Math.max(2, Math.min(4, field.length));
  // Cut-lines: vertical rungs where the worst lane is eliminated.
  for (let g = 1; g < rungs; g++) {
    const x = left + (trackW * g) / rungs;
    edges.push({ id: 'rc:cut:' + g, x1: x, y1: top - 14, x2: x, y2: top + field.length * laneH, cls: 'cz-edge-cross', kind: 'cut', label: 'rung ' + g });
  }
  field.forEach((c, i) => {
    const y = top + i * laneH + laneH / 2;
    // A lane: start marker, a confidence band that narrows, an end marker.
    // `survive` = how far the lane runs before its cut (worst lanes cut early).
    const survive = c.role === 'champion' ? rungs : Math.max(1, rungs - (i % rungs));
    const endX = left + (trackW * survive) / rungs;
    nodes.push({
      id: 'rc:lane:' + c.id, x: left, y, w: endX - left, h: laneH - 22,
      label: c.id, sub: c.role === 'champion' ? 'survives' : (survive < rungs ? 'cut @ rung ' + survive : 'racing'),
      cls: c.cls || 'cz-v-neutral', role: 'lane', endX, survive, rungs,
    });
    edges.push({ id: 'rc:track:' + c.id, x1: left, y1: y, x2: endX, y2: y, cls: c.cls || 'cz-edge-rejected', kind: 'lane' });
  });
  const w = left + trackW + 40;
  const h = top + field.length * laneH + 20;
  return { nodes, edges, box: { x: 0, y: 0, w, h } };
}

// Deterministic "winner" of a conceptual match: the champion/seed always
// advances, else the candidate marked promoted, else the first present.
// (These brackets are illustrative — a real run would replicate.)
function pickWinner(a, b) {
  if (a && !b) return a;
  if (b && !a) return b;
  if (!a && !b) return null;
  if (a.role === 'champion' || a.promoted === true) return a;
  if (b.role === 'champion' || b.promoted === true) return b;
  return a;
}

export const TOURNAMENT_STYLES = [
  { id: 'gauntlet', label: 'Gauntlet', topology: 'star/hub', real: true, fn: layoutGauntlet,
    blurb: 'King-of-the-hill: one champion defends, each challenger a spoke. The shipped structure — real paired per-board duels.' },
  { id: 'single', label: 'Single-elim', topology: 'binary tree', real: false, fn: layoutSingleElim,
    blurb: 'Cheap triage of a large field. Noise-fragile at the boundary — a strong candidate can die to one unlucky run. Illustrative.' },
  { id: 'double', label: 'Double-elim', topology: 'two coupled trees', real: false, fn: layoutDoubleElim,
    blurb: 'A "second life" via a losers’ bracket. Its robustness is delivered more cheaply by replication. Illustrative.' },
  { id: 'swiss', label: 'Swiss', topology: 'bipartite rounds', real: false, fn: layoutSwiss,
    blurb: 'Fixed rounds, no elimination, full ranking. Right goal, superseded by racing. Illustrative.' },
  { id: 'racing', label: 'Racing', topology: 'parallel lanes', real: false, fn: layoutRacing,
    blurb: 'Successive halving: lanes race on shared instances, cut-lines drop the dominated. zicato’s recommended direction. Illustrative.' },
];
