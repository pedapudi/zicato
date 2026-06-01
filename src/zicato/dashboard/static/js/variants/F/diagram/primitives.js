// variants/F/diagram/primitives.js — pure diagram math + small SVG parts.
//
// No state, no fetch. Verdict palette (mapped onto the --v2-* tokens via
// CSS classes), bezier flow-path builders, and a layered DAG layout that
// guarantees non-colliding edges (the "no colliding lines" requirement).

import { svgEl } from '../../../core/dom.js';

// Verdict → semantic class. The actual colour comes from --v2-* tokens
// (see css/variants/F/variant.css); the class is the single source of
// the cause→effect→verdict colour language across every screen.
export function verdictClass(verdict) {
  const v = String(verdict || '').toLowerCase();
  if (v.includes('promot')) return 'cz-v-promoted';
  if (v.includes('reject')) return 'cz-v-rejected';
  if (v.includes('defer')) return 'cz-v-deferred';
  if (v === 'running' || v.includes('flight') || v === 'live') return 'cz-v-running';
  if (v === 'baseline' || v === 'seed') return 'cz-v-baseline';
  return 'cz-v-neutral';
}

export function verdictLabel(verdict) {
  const v = String(verdict || '').toLowerCase();
  if (v.includes('promot')) return 'PROMOTED';
  if (v.includes('reject')) return 'REJECTED';
  if (v.includes('defer')) return 'DEFERRED';
  if (v === 'running' || v.includes('flight') || v === 'live') return 'RUNNING';
  if (v === 'baseline' || v === 'seed') return 'BASELINE';
  return 'PENDING';
}

// A horizontal flow link between two points, as a smooth cubic bezier.
// Used for both the lineage DAG edges and the Sankey flow ribbons (when
// `thickness` is given, the link is rendered as a filled ribbon rather
// than a stroked path).
export function flowPath(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
}

// A filled Sankey ribbon of vertical half-width `hw` from (x1,y1) to
// (x2,y2). Top edge forward, bottom edge back — a closed band.
export function ribbonPath(x1, y1, x2, y2, hw1, hw2) {
  const mx = (x1 + x2) / 2;
  const topA = y1 - hw1;
  const botA = y1 + hw1;
  const topB = y2 - hw2;
  const botB = y2 + hw2;
  return [
    `M ${x1} ${topA}`,
    `C ${mx} ${topA}, ${mx} ${topB}, ${x2} ${topB}`,
    `L ${x2} ${botB}`,
    `C ${mx} ${botB}, ${mx} ${botA}, ${x1} ${botA}`,
    'Z',
  ].join(' ');
}

// Build an SVG <path> animated edge. `dashed` draws a marching-ants flow
// when `animated` is true (CSS animates stroke-dashoffset on .cz-edge-live).
export function edgeEl(d, { cls = '', animated = false, width = 2 } = {}) {
  const klass = ['cz-edge', cls, animated ? 'cz-edge-live' : ''].filter(Boolean).join(' ');
  return svgEl('path', { d, class: klass, 'stroke-width': width, fill: 'none' });
}

// -- Layered DAG layout -------------------------------------------------
//
// Assigns each node an integer column (depth from its root) and a row
// (0,1,2,...) within that column so siblings never overlap. Returns a
// Map id -> { col, row, colCount } plus the per-column row counts. The
// caller turns (col, row) into pixel (x, y); because every node owns a
// distinct (col,row) cell and edges only ever go left→right between
// adjacent-or-later columns, edges drawn as horizontal beziers cannot
// collide into an unreadable tangle.
//
// `nodes`: [{ id, parent }]. A missing/blank parent makes the node a
// root (col 0). Cycles are impossible in a lineage but guarded anyway.
export function layoutDag(nodes) {
  const byId = new Map();
  for (const n of nodes) byId.set(n.id, { ...n, children: [] });
  const roots = [];
  for (const n of byId.values()) {
    const p = n.parent && byId.has(n.parent) ? byId.get(n.parent) : null;
    if (p && p.id !== n.id) p.children.push(n);
    else roots.push(n);
  }

  // Column = longest path from a root (BFS depth), stable + cycle-safe.
  const col = new Map();
  const queue = roots.map((r) => ({ node: r, depth: 0 }));
  const seen = new Set();
  while (queue.length) {
    const { node, depth } = queue.shift();
    const prev = col.get(node.id);
    if (prev == null || depth > prev) col.set(node.id, depth);
    if (seen.has(node.id + '@' + depth)) continue;
    seen.add(node.id + '@' + depth);
    for (const c of node.children) queue.push({ node: c, depth: depth + 1 });
  }
  // Any node never reached (orphan parent ref) lands in col 0.
  for (const n of byId.values()) if (!col.has(n.id)) col.set(n.id, 0);

  // Row assignment: a stable per-column running index, ordered by a
  // depth-first walk from roots so a parent's children cluster near it.
  const rowCursor = new Map();
  const row = new Map();
  const visited = new Set();
  const assign = (node) => {
    if (visited.has(node.id)) return;
    visited.add(node.id);
    const c = col.get(node.id);
    const next = rowCursor.get(c) || 0;
    row.set(node.id, next);
    rowCursor.set(c, next + 1);
    // Children sorted by id for determinism.
    const kids = [...node.children].sort((a, b) => String(a.id).localeCompare(String(b.id)));
    for (const k of kids) assign(k);
  };
  const orderedRoots = [...roots].sort((a, b) => String(a.id).localeCompare(String(b.id)));
  for (const r of orderedRoots) assign(r);
  for (const n of byId.values()) if (!row.has(n.id)) {
    const c = col.get(n.id);
    const next = rowCursor.get(c) || 0;
    row.set(n.id, next);
    rowCursor.set(c, next + 1);
  }

  const maxCol = Math.max(0, ...[...col.values()]);
  const colCount = [];
  for (let i = 0; i <= maxCol; i++) colCount[i] = rowCursor.get(i) || 0;
  const maxRow = Math.max(1, ...colCount);

  const pos = new Map();
  for (const n of byId.values()) {
    pos.set(n.id, { col: col.get(n.id), row: row.get(n.id) });
  }
  return { pos, colCount, maxCol, maxRow };
}

// Normalise a numeric series to [0,1] (for node sizing / y by scalar).
export function normalize(values) {
  const nums = values.filter((v) => typeof v === 'number' && isFinite(v));
  if (nums.length === 0) return () => 0.5;
  const lo = Math.min(...nums);
  const hi = Math.max(...nums);
  if (hi === lo) return () => 0.5;
  return (v) => (typeof v === 'number' && isFinite(v)) ? (v - lo) / (hi - lo) : 0.5;
}
