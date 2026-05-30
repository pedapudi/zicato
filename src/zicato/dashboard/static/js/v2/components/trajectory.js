// js/v2/components/trajectory.js — the spine / lineage as an optimization
// curve. DASHBOARD-V2 §4.6 + §5.
//
// This is the persistent backbone of v2: the lineage being climbed,
// drawn as the optimization trajectory.
//
//   * x = lineage order (left → right, oldest → newest).
//   * y = the node's scalar (loss). LOWER loss sits HIGHER on screen, so
//     the promoted lineage literally traces the curve being descended.
//
// Promoted lineage is the through-line (the spine); rejected challengers
// branch OFF their parent, drawn subordinate; the live node pulses
// (gated by prefers-reduced-motion in CSS). Every node is clickable →
// onSelect(id).
//
// CRITICAL — the v1 lineageRibbon failed at small scale: a single node /
// mid-first-run rendered as a stray glyph floating in empty space with a
// degenerate scalar axis. v2 MUST render well at EVERY scale. So the
// factory has THREE layouts:
//
//   * 0 nodes      → a labeled empty state (never blank).
//   * 1 node OR a degenerate scalar domain → the SOLO layout: a single
//     compact, CENTERED, LABELED node + a one-line hint. No axis, no
//     stray geometry.
//   * N nodes      → the full plotted trajectory.
//
// Pure factory: returns a fresh detached, re-render-safe node every
// call; never mounts itself.
//
//   trajectory({ nodes, zoom, onSelect })
//     nodes    — [{ id, parentId, scalar, verdict, live, label }]
//                verdict ∈ 'promoted' | 'rejected' | (else → open)
//     zoom     — 'epochs' | 'generations' (label density; default 'generations')
//     onSelect — (id) => void, fired on node click / Enter / Space

import { el, svgEl } from '../../core/dom.js';
import { fmtScalar } from '../../core/format.js';

// Geometry — every coordinate derives from these so one tweak rebalances
// the whole picture.
const COL_W = 92;        // px between adjacent spine column centers
const DOT_R = 9;         // spine / live dot radius
const BRANCH_R = 6;      // rejected-challenger dot radius (subordinate)
const PAD_X = 40;        // horizontal padding inside the plot
const PLOT_TOP = 22;     // top of the scalar band
const PLOT_BOTTOM = 116; // bottom of the scalar band
const BRANCH_DX = 24;    // horizontal nudge of a rejected branch off parent
const BRANCH_DY = 26;    // vertical nudge when a branch has no finite scalar
const LABEL_Y = 138;     // baseline y of spine node labels
const HEIGHT = 168;      // total stage height (matches --v2-spine-height)

const VERDICT_GLYPH = { promoted: '✓', rejected: '✗', open: '◦' };

function verdictOf(node) {
  const v = String((node && node.verdict) || '').toLowerCase();
  if (v === 'promoted') return 'promoted';
  if (v === 'rejected') return 'rejected';
  return 'open';
}

function isSpineNode(n) {
  return verdictOf(n) === 'promoted' || n.live === true;
}

function hasFiniteScalar(n) {
  return n && typeof n.scalar === 'number' && isFinite(n.scalar);
}

// ---------------------------------------------------------------------------
// Layout — pure geometry, no DOM. Exported so a regression cannot
// silently slide the dots out of place (and so the single-node fallback
// path is unit-testable).
//
// Returns { mode, ... }:
//   mode 'empty'  → no nodes.
//   mode 'solo'   → exactly one drawable node OR a degenerate scalar
//                   domain (every scalar equal / non-finite): a single
//                   centered node. { node, verdict, isLive }
//   mode 'plot'   → { columns, branches, spinePath, liveLinkPath,
//                     scalarDomain, width }
// ---------------------------------------------------------------------------
export function computeTrajectoryLayout(nodes, opts) {
  const o = opts || {};
  const zoom = o.zoom === 'epochs' ? 'epochs' : 'generations';
  const all = Array.isArray(nodes) ? nodes.filter((n) => n && n.id != null) : [];

  if (all.length === 0) return { mode: 'empty', zoom };

  // Spine = promoted ∪ live (in input order). Everything else is a
  // rejected challenger that branches off its parent. Never collapse to
  // nothing — if nothing has made the spine yet, treat all as spine so a
  // trajectory still draws.
  let spineNodes = all.filter(isSpineNode);
  let rejected = all.filter((n) => !isSpineNode(n));
  if (spineNodes.length === 0) {
    spineNodes = all;
    rejected = [];
  }

  // Scalar domain across every finite-scalar node.
  const scalars = all.filter(hasFiniteScalar).map((n) => n.scalar);
  const sMin = scalars.length ? Math.min(...scalars) : 0;
  const sMax = scalars.length ? Math.max(...scalars) : 1;
  const degenerateDomain = !(sMax > sMin); // equal or no finite scalars

  // SOLO fallback: one spine node OR a degenerate scalar axis. Plotting a
  // curve through a single point (or a flat axis) is the v1 bug — a lone
  // floating glyph. Instead, render one centered, labeled node. We pick
  // the live node if present, else the last spine node (the newest).
  if (spineNodes.length === 1
      || (spineNodes.length > 0 && rejected.length === 0 && degenerateDomain)) {
    // When the domain is degenerate but there are several equal-scalar
    // spine nodes, still prefer the meaningful one to feature.
    const live = spineNodes.find((n) => n.live === true);
    const node = live || spineNodes[spineNodes.length - 1];
    return {
      mode: 'solo',
      zoom,
      node,
      verdict: verdictOf(node),
      isLive: node.live === true,
      // How many nodes the lineage actually has, so the hint can be honest.
      count: all.length,
    };
  }

  // Plot mode: map the scalar onto the band, inverted (lower = higher).
  const sRange = (sMax - sMin) || 1;
  const midY = (PLOT_TOP + PLOT_BOTTOM) / 2;
  const yFor = (s) => {
    if (typeof s !== 'number' || !isFinite(s)) return midY;
    return PLOT_TOP + ((s - sMin) / sRange) * (PLOT_BOTTOM - PLOT_TOP);
  };

  const columns = [];
  const byId = new Map();
  for (let i = 0; i < spineNodes.length; i += 1) {
    const node = spineNodes[i];
    const x = PAD_X + (i + 0.5) * COL_W;
    const y = yFor(node.scalar);
    const col = { node, verdict: verdictOf(node), index: i, x, y, isLive: node.live === true };
    columns.push(col);
    byId.set(String(node.id), col);
  }

  // Branch each rejected challenger off its parent column.
  const branches = [];
  const childCount = new Map();
  for (const rn of rejected) {
    const pid = rn.parentId != null ? String(rn.parentId) : null;
    const parent = pid ? byId.get(pid) : null;
    const n = parent ? (childCount.get(pid) || 0) : 0;
    if (parent) childCount.set(pid, n + 1);
    const baseX = parent ? parent.x : PAD_X;
    const baseY = parent ? parent.y : midY;
    const x = baseX + BRANCH_DX;
    const y = hasFiniteScalar(rn) ? yFor(rn.scalar) : baseY - BRANCH_DY - n * (BRANCH_DY * 0.7);
    branches.push({ node: rn, verdict: verdictOf(rn), parent, x, y });
  }

  // Spine connector path through column centers; the final hop into a
  // live node is split out so it can render dashed.
  let spinePath = '';
  let liveLinkPath = '';
  for (let i = 0; i < columns.length - 1; i += 1) {
    const seg = connectorPath(columns[i], columns[i + 1]);
    if (columns[i + 1].isLive) liveLinkPath = seg;
    else spinePath += (spinePath ? ' ' : '') + seg;
  }

  const width = PAD_X * 2 + columns.length * COL_W;
  return {
    mode: 'plot',
    zoom,
    columns,
    branches,
    spinePath,
    liveLinkPath,
    scalarDomain: { min: sMin, max: sMax },
    width,
  };
}

// A smooth bezier between two dot centers, meeting each dot at its edge.
export function connectorPath(from, to) {
  const x1 = from.x + DOT_R;
  const y1 = from.y;
  const x2 = to.x - DOT_R;
  const y2 = to.y;
  const mid = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
}

export function branchPath(branch) {
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
export function trajectory(opts) {
  const o = opts || {};
  const onSelect = typeof o.onSelect === 'function' ? o.onSelect : null;
  const layout = computeTrajectoryLayout(o.nodes, { zoom: o.zoom });

  if (layout.mode === 'empty') {
    return el('div', { class: 'v2-trajectory v2-trajectory-empty', 'data-mode': 'empty' }, [
      el('span', { class: 'v2-state-glyph', 'aria-hidden': 'true' }, ['◌']),
      el('span', {}, ['No lineage yet — the first generation will anchor the trajectory.']),
    ]);
  }

  if (layout.mode === 'solo') {
    // The corrected single-node case: a compact, CENTERED, LABELED node
    // (NOT empty space with a stray glyph). A short hint frames it as the
    // start of a trajectory rather than an error.
    const node = renderNode({
      node: layout.node,
      kind: layout.isLive ? 'live' : layout.verdict,
      verdict: layout.verdict,
      radius: DOT_R,
      zoom: layout.zoom,
      onSelect,
      // solo node is statically laid out by flexbox, no absolute coords
      absolute: false,
    });
    const hint = layout.isLive
      ? 'First run in flight — the trajectory begins here.'
      : 'Lineage just begun — more generations will extend the curve.';
    return el('div', {
      class: 'v2-trajectory v2-trajectory-solo',
      'data-mode': 'solo',
    }, [
      node,
      el('p', { class: 'v2-trajectory-solo-hint' }, [hint]),
    ]);
  }

  // Plot mode.
  const width = layout.width;
  const stage = el('div', {
    class: 'v2-trajectory-stage',
    style: `position: relative; width: ${width}px; height: ${HEIGHT}px;`,
  });

  const svg = svgEl('svg', {
    class: 'v2-trajectory-svg',
    width,
    height: HEIGHT,
    viewBox: `0 0 ${width} ${HEIGHT}`,
    'aria-hidden': 'true',
  });
  if (layout.spinePath) {
    svg.appendChild(svgEl('path', { class: 'v2-traj-spine-path', d: layout.spinePath }));
  }
  if (layout.liveLinkPath) {
    svg.appendChild(svgEl('path', {
      class: 'v2-traj-spine-path v2-traj-spine-path-live',
      d: layout.liveLinkPath,
    }));
  }
  for (const br of layout.branches) {
    if (!br.parent) continue;
    svg.appendChild(svgEl('path', { class: 'v2-traj-branch-path', d: branchPath(br) }));
  }
  stage.appendChild(svg);

  for (const col of layout.columns) {
    stage.appendChild(renderNode({
      node: col.node,
      kind: col.isLive ? 'live' : col.verdict,
      verdict: col.verdict,
      x: col.x,
      y: col.y,
      radius: DOT_R,
      labelY: LABEL_Y,
      zoom: layout.zoom,
      onSelect,
      absolute: true,
    }));
  }
  for (const br of layout.branches) {
    stage.appendChild(renderNode({
      node: br.node,
      kind: 'branch',
      verdict: br.verdict,
      x: br.x,
      y: br.y,
      radius: BRANCH_R,
      labelY: null,
      zoom: layout.zoom,
      onSelect,
      absolute: true,
    }));
  }

  return el('div', { class: `v2-trajectory v2-trajectory-zoom-${layout.zoom}`, 'data-mode': 'plot' }, [
    el('div', { class: 'v2-trajectory-scroll' }, [stage]),
  ]);
}

// One node: a button carrying the verdict glyph + a label (id + scalar).
// `absolute: true` anchors it on (x, y) for the plot; `absolute: false`
// lets flexbox center it (the solo fallback).
function renderNode(spec) {
  const { node, kind, verdict, radius, zoom, onSelect, absolute } = spec;
  const id = String(node.id != null ? node.id : '?');
  const glyph = VERDICT_GLYPH[verdict] || VERDICT_GLYPH.open;
  const scalarStr = fmtScalar(node.scalar);
  const isLive = kind === 'live';

  const dot = el('span', {
    class: 'v2-traj-dot',
    'aria-hidden': 'true',
    style: `width: ${radius * 2}px; height: ${radius * 2}px;`,
  }, [glyph]);

  const labelText = (zoom === 'epochs' && node.label) ? String(node.label) : id;
  const labelChildren = [];
  if (isLive) labelChildren.push(el('span', { class: 'v2-traj-live-tag' }, ['LIVE']));
  labelChildren.push(el('span', { class: 'v2-traj-label-id' }, [labelText]));
  labelChildren.push(el('span', { class: 'v2-traj-label-scalar' }, [scalarStr]));
  const label = el('span', { class: 'v2-traj-label' }, labelChildren);

  const props = {
    class: `v2-traj-node v2-traj-node-${kind}`,
    type: 'button',
    'data-node-id': id,
    'data-verdict': verdict,
    'data-live': isLive ? 'true' : null,
    'aria-label': `${verdict} ${kind === 'branch' ? 'challenger' : 'generation'} ${id}`
      + (scalarStr !== '—' ? `, scalar ${scalarStr}` : ''),
  };

  if (absolute) {
    // Anchor the dot center on (x, y); pin the label to a shared baseline
    // (spine) so labels line up regardless of dot y.
    props.style = `position: absolute; left: ${spec.x - radius}px; top: ${spec.y - radius}px;`;
    if (spec.labelY != null) {
      label.setAttribute(
        'style',
        `position: absolute; left: 50%; top: ${spec.labelY - (spec.y - radius)}px; transform: translateX(-50%);`,
      );
    }
  }

  if (onSelect) {
    props.onclick = (ev) => { if (ev && ev.preventDefault) ev.preventDefault(); onSelect(id); };
    props.onkeydown = (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        if (ev.preventDefault) ev.preventDefault();
        onSelect(id);
      }
    };
  }

  return el('button', props, [dot, label]);
}

// Test exports — pin the geometry so the layout cannot drift.
export const TRAJECTORY_GEOMETRY = Object.freeze({
  COL_W, DOT_R, BRANCH_R, PAD_X, PLOT_TOP, PLOT_BOTTOM,
  BRANCH_DX, BRANCH_DY, LABEL_Y, HEIGHT,
});
