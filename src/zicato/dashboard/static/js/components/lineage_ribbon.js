// components/lineage_ribbon.js — the unified lineage ribbon.
//
// The ribbon is the dashboard's signature structural element. It folds
// three things the dashboard used to draw separately into one picture:
//
//   * the L1 generation spine (the promoted lineage as a left-to-right
//     walk through generations),
//   * the L0 epoch timeline (the run of epochs over time), and
//   * the cross-epoch sparkline (the optimization trajectory of the
//     scalar / loss).
//
// It unifies them by letting GEOMETRY carry meaning that the spine kept
// flat:
//
//   * x = sequence / lineage order (left → right, oldest → newest).
//   * y = the node's scalar (loss). LOWER loss sits HIGHER on screen, so
//     the promoted lineage literally traces the optimization curve —
//     the spine IS the sparkline now.
//
// The promoted lineage is the main spine: smooth bezier connectors run
// dot-center to dot-center (the deliberate quality the spine.js rewrite
// fought for — no floating arrows in the gaps). Rejected challengers
// branch OFF their parent node, drawn subordinate (thinner, dimmer,
// nudged off the spine). The live node is the right-most node with a
// dashed border + a subtle pulse.
//
// Each node carries a verdict glyph from the dashboard's shared
// vocabulary — ✓ promoted / ✗ rejected / ◦ open — and is clickable,
// invoking onSelect(id).
//
// `zoom ∈ 'epochs' | 'generations'` tunes label density: 'epochs' shows
// epoch ids + their best scalar; 'generations' shows per-generation ids.
//
// Re-render safe: the factory is pure — it builds and returns a fresh
// detached node every call and never mounts itself.

import { el, svgEl } from '../core/dom.js';
import { fmtScalar } from '../core/format.js';

// ---------------------------------------------------------------------------
// Geometry — every coordinate derives from these constants so one tweak
// rebalances the whole picture (mirrors the spine.js discipline).
// ---------------------------------------------------------------------------
const COLUMN_WIDTH = 96;     // px between adjacent lineage-column centers
const DOT_RADIUS = 7;        // promoted/live dot radius
const BRANCH_RADIUS = 5;     // rejected challenger dot radius (subordinate)
const CANVAS_PAD_X = 36;     // horizontal padding inside the canvas
const PLOT_TOP = 28;         // top of the scalar plotting band
const PLOT_BOTTOM = 150;     // bottom of the scalar plotting band
const BRANCH_DX = 26;        // horizontal nudge of a rejected branch off parent
const BRANCH_DY = 30;        // vertical nudge of a rejected branch off parent
const LABEL_Y = 178;         // y of the node id/scalar labels
const CANVAS_HEIGHT = 200;   // total canvas height

// The shared verdict glyph vocabulary — identical to the rest of the
// dashboard. Anything unrecognised reads as "open".
const VERDICT_GLYPH = { promoted: '✓', rejected: '✗', open: '◦' };

function _verdictOf(node) {
  const v = String((node && node.verdict) || '').toLowerCase();
  if (v === 'promoted') return 'promoted';
  if (v === 'rejected') return 'rejected';
  return 'open';
}

// ---------------------------------------------------------------------------
// Layout — pure geometry, no DOM. Exported for unit tests so a
// regression cannot silently slide the dots out of place.
// ---------------------------------------------------------------------------

/**
 * Compute the (x, y) placement for every node.
 *
 * Promoted (and the live) nodes form the spine columns, x-ordered by
 * their position in `nodes`. Rejected challengers do NOT consume a
 * column; they are nudged off their parent's column so they read as a
 * branch. y is a linear map of the scalar across all finite scalars,
 * inverted so a LOWER scalar sits HIGHER on screen.
 *
 * Returns { columns, branches, spinePath, scalarDomain, canvasWidth }.
 *   columns  — [{ node, verdict, index, x, y, isLive }] spine nodes
 *   branches — [{ node, verdict, parent, x, y }] rejected challengers
 *   spinePath — the bezier `d` connecting consecutive columns, or ''
 */
export function computeRibbonLayout(nodes, opts) {
  const o = opts || {};
  const all = Array.isArray(nodes) ? nodes.filter((n) => n && n.id != null) : [];

  // Scalar domain across every node carrying a finite scalar. y inverts
  // it: yFor(min) = PLOT_TOP (best, highest), yFor(max) = PLOT_BOTTOM.
  const scalars = all
    .map((n) => n.scalar)
    .filter((s) => typeof s === 'number' && isFinite(s));
  const sMin = scalars.length ? Math.min(...scalars) : 0;
  const sMax = scalars.length ? Math.max(...scalars) : 1;
  const sRange = (sMax - sMin) || 1;
  const midY = (PLOT_TOP + PLOT_BOTTOM) / 2;
  // Lower loss sits HIGHER on screen (smaller y): the best (min) scalar
  // pins to PLOT_TOP, the worst (max) to PLOT_BOTTOM.
  const yFor = (scalar) => {
    if (typeof scalar !== 'number' || !isFinite(scalar)) return midY;
    return PLOT_TOP + ((scalar - sMin) / sRange) * (PLOT_BOTTOM - PLOT_TOP);
  };

  // Spine = promoted ∪ live, in input order. Everything else is a
  // rejected challenger that branches off its parent.
  const isSpine = (n) => _verdictOf(n) === 'promoted' || n.live === true;
  let spineNodes = all.filter(isSpine);
  let rejectedNodes = all.filter((n) => !isSpine(n));
  // Never collapse to nothing — if no node made the spine yet, show all
  // of them inline so the ribbon still draws a trajectory.
  if (spineNodes.length === 0) {
    spineNodes = all;
    rejectedNodes = [];
  }

  const columns = [];
  const byId = new Map();
  for (let i = 0; i < spineNodes.length; i += 1) {
    const node = spineNodes[i];
    const x = CANVAS_PAD_X + (i + 0.5) * COLUMN_WIDTH;
    const y = yFor(node.scalar);
    const col = {
      node, verdict: _verdictOf(node), index: i, x, y,
      isLive: node.live === true,
    };
    columns.push(col);
    byId.set(String(node.id), col);
  }

  // Branch each rejected challenger off its parent column. Multiple
  // children of the same parent stack with an increasing vertical nudge
  // so they never collide.
  const branches = [];
  const childCount = new Map();
  for (const rn of rejectedNodes) {
    const pid = rn.parentId != null ? String(rn.parentId) : null;
    const parent = pid ? byId.get(pid) : null;
    const n = parent ? (childCount.get(pid) || 0) : 0;
    if (parent) childCount.set(pid, n + 1);
    const baseX = parent ? parent.x : CANVAS_PAD_X;
    const baseY = parent ? parent.y : midY;
    // Branch sits up-and-to-the-right of its parent (subordinate, off
    // the spine). Stack siblings downward so a fan never overlaps.
    const x = baseX + BRANCH_DX;
    const y = (typeof rn.scalar === 'number' && isFinite(rn.scalar))
      ? yFor(rn.scalar)
      : baseY - BRANCH_DY - n * (BRANCH_DY * 0.7);
    branches.push({ node: rn, verdict: _verdictOf(rn), parent, x, y });
  }

  // Spine connector: a smooth left→right bezier through column centers.
  let spinePath = '';
  for (let i = 0; i < columns.length - 1; i += 1) {
    spinePath += (spinePath ? ' ' : '') + buildConnectorPath(columns[i], columns[i + 1]);
  }

  const canvasWidth = CANVAS_PAD_X * 2 + Math.max(1, columns.length) * COLUMN_WIDTH;
  return {
    columns,
    branches,
    spinePath,
    scalarDomain: { min: sMin, max: sMax },
    canvasWidth,
    zoom: o.zoom === 'epochs' ? 'epochs' : 'generations',
  };
}

// A smooth bezier connecting two column dot-centers, meeting each dot at
// its edge (so a marker / glyph sits flush, never crossing the center).
export function buildConnectorPath(from, to) {
  const x1 = from.x + DOT_RADIUS;
  const y1 = from.y;
  const x2 = to.x - DOT_RADIUS;
  const y2 = to.y;
  const mid = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
}

// A soft connector from a rejected branch dot back to its parent dot
// center, so the challenger visibly hangs off the lineage it lost to.
export function buildBranchPath(branch) {
  if (!branch.parent) return '';
  const x1 = branch.x;
  const y1 = branch.y;
  const x2 = branch.parent.x;
  const y2 = branch.parent.y;
  const cx = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`;
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

/**
 * Build a lineage ribbon node.
 *
 * opts:
 *   nodes    — [{ id, parentId, scalar, verdict, live, label }]
 *              verdict ∈ 'promoted' | 'rejected' | (anything else = open)
 *   zoom     — 'epochs' | 'generations' (default 'generations')
 *   onSelect — (id) => void, fired on node click / keyboard activation
 */
export function lineageRibbon(opts) {
  const o = opts || {};
  const onSelect = typeof o.onSelect === 'function' ? o.onSelect : null;
  const nodes = Array.isArray(o.nodes) ? o.nodes : [];

  if (nodes.length === 0) {
    return el('div', { class: 'ribbon ribbon-empty' }, [
      el('p', { class: 'ribbon-empty-msg' }, ['No lineage yet.']),
    ]);
  }

  const layout = computeRibbonLayout(nodes, { zoom: o.zoom });
  const width = layout.canvasWidth;

  const stage = el('div', {
    class: 'ribbon-stage',
    style: `position: relative; width: ${width}px; height: ${CANVAS_HEIGHT}px;`,
  });

  // -- SVG connector layer (clicks pass through to the HTML overlay) ---
  const svg = svgEl('svg', {
    class: 'ribbon-svg',
    width,
    height: CANVAS_HEIGHT,
    viewBox: `0 0 ${width} ${CANVAS_HEIGHT}`,
    'aria-hidden': 'true',
    style: 'position: absolute; left: 0; top: 0; pointer-events: none;',
  });

  // Promoted spine — one continuous trajectory.
  if (layout.spinePath) {
    svg.appendChild(svgEl('path', {
      class: 'ribbon-spine-path',
      d: layout.spinePath,
      fill: 'none',
      stroke: 'var(--color-promoted)',
      'stroke-width': 2.25,
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
    }));
  }
  // Live transition stub — the final hop into the live node is dashed.
  const liveCol = layout.columns.find((c) => c.isLive);
  if (liveCol && liveCol.index > 0) {
    const prev = layout.columns[liveCol.index - 1];
    svg.appendChild(svgEl('path', {
      class: 'ribbon-spine-path ribbon-spine-path-live',
      d: buildConnectorPath(prev, liveCol),
      fill: 'none',
      stroke: 'var(--color-accent)',
      'stroke-width': 2.25,
      'stroke-dasharray': '6 4',
      'stroke-linecap': 'round',
    }));
  }
  // Rejected branch drops — subordinate, thinner, dimmer.
  for (const br of layout.branches) {
    if (!br.parent) continue;
    svg.appendChild(svgEl('path', {
      class: 'ribbon-branch-path',
      d: buildBranchPath(br),
      fill: 'none',
      stroke: 'var(--color-rejected)',
      'stroke-width': 1.4,
      'stroke-opacity': 0.6,
      'stroke-dasharray': '4 3',
      'stroke-linecap': 'round',
    }));
  }
  stage.appendChild(svg);

  // -- HTML overlay layer ----------------------------------------------
  for (const col of layout.columns) {
    stage.appendChild(_renderNode({
      kind: col.isLive ? 'live' : col.verdict,
      verdict: col.verdict,
      node: col.node,
      x: col.x,
      y: col.y,
      radius: DOT_RADIUS,
      labelY: LABEL_Y,
      zoom: layout.zoom,
      onSelect,
    }));
  }
  for (const br of layout.branches) {
    stage.appendChild(_renderNode({
      kind: 'branch',
      verdict: br.verdict,
      node: br.node,
      x: br.x,
      y: br.y,
      radius: BRANCH_RADIUS,
      labelY: null, // branches keep an inline label, not a baseline one
      zoom: layout.zoom,
      onSelect,
    }));
  }

  return el('div', { class: `ribbon ribbon-zoom-${layout.zoom}` }, [
    el('div', { class: 'ribbon-scroll' }, [stage]),
  ]);
}

// One node = an absolutely-positioned button anchored on its (x, y) dot
// center. The dot carries the verdict glyph; the label sits below (spine
// nodes) or beside (branch nodes).
function _renderNode(spec) {
  const { node, x, y, radius, verdict, kind, zoom, onSelect, labelY } = spec;
  const id = String(node.id != null ? node.id : '?');
  const glyph = VERDICT_GLYPH[verdict] || VERDICT_GLYPH.open;
  const scalarStr = fmtScalar(node.scalar);

  const dotCls = ['ribbon-dot', `ribbon-dot-${kind}`];
  const dot = el('span', {
    class: dotCls.join(' '),
    'aria-hidden': 'true',
    style: `width: ${radius * 2}px; height: ${radius * 2}px;`,
  }, [el('span', { class: 'ribbon-glyph' }, [glyph])]);

  // Label density follows the zoom. Generations zoom always shows the
  // gen id; epochs zoom leads with the (epoch) id + its scalar.
  const labelText = (zoom === 'epochs' && node.label) ? String(node.label) : id;
  const labelChildren = [
    el('span', { class: 'ribbon-node-id mono' }, [labelText]),
  ];
  if (kind === 'live') {
    labelChildren.unshift(el('span', { class: 'ribbon-live-tag' }, ['LIVE']));
  }
  labelChildren.push(el('span', { class: 'ribbon-node-scalar mono' }, [scalarStr]));
  const label = el('span', {
    class: `ribbon-node-label${labelY == null ? ' ribbon-node-label-inline' : ''}`,
  }, labelChildren);

  // Position: dot centered on (x, y); label below the plot band (spine)
  // or just beside the dot (branch).
  const left = x - radius;
  const top = y - radius;
  const wrapStyle = `position: absolute; left: ${left}px; top: ${top}px;`;

  const ariaLabel = `${verdict} ${kind === 'branch' ? 'challenger' : 'generation'} ${id}`
    + (scalarStr !== '—' ? `, scalar ${scalarStr}` : '');

  const classes = ['ribbon-node', `ribbon-node-${kind}`, `ribbon-node-v-${verdict}`];

  const children = [dot];
  if (labelY != null) {
    // Anchor the baseline label absolutely under the plot band so every
    // spine label lines up regardless of its dot's y.
    label.setAttribute(
      'style',
      `position: absolute; left: 50%; top: ${labelY - top}px; transform: translateX(-50%);`,
    );
  }
  children.push(label);

  const props = {
    class: classes.join(' '),
    style: wrapStyle,
    role: 'button',
    tabindex: '0',
    'aria-label': ariaLabel,
    'data-node-id': id,
    'data-verdict': verdict,
  };
  if (onSelect) {
    props.onclick = (ev) => { ev.preventDefault(); onSelect(id); };
    props.onkeydown = (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onSelect(id); }
    };
  }
  return el('div', props, children);
}

// ---------------------------------------------------------------------------
// Test exports — pin the geometry constants so the layout cannot drift.
// ---------------------------------------------------------------------------
export const RIBBON_GEOMETRY = Object.freeze({
  COLUMN_WIDTH,
  DOT_RADIUS,
  BRANCH_RADIUS,
  CANVAS_PAD_X,
  PLOT_TOP,
  PLOT_BOTTOM,
  BRANCH_DX,
  BRANCH_DY,
  LABEL_Y,
  CANVAS_HEIGHT,
});
