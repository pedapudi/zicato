// components/spine.js — lineage spine visualization.
//
// The spine is the dashboard's signature L1 element. It tells the
// story of an epoch as a left-to-right walk through generations: the
// promoted lineage forms a solid horizontal spine, and each rejected
// challenger is drawn as a small branch chip ABOVE its parent on the
// spine so a viewer can instantly see "v4-v7 all challenged v3, none
// beat it." The live node (if any) is the right-most node on the
// spine row with a dashed border + pulse.
//
//        v2(R)         v4(R) v5(R) v6(R) v7(R)
//         │             │     │     │     │
//   v0 ── v1 ───────── v3 ────────────── v8 LIVE
//
// Render shape:
//   * A single SVG canvas draws every connector — promoted spine
//     curves, rejected-child drops, and the live transition stub —
//     between actual dot centers. No floating arrow stubs in the
//     gaps, no border-left red lines on chips.
//   * HTML node divs are ABSOLUTELY POSITIONED on top of the SVG.
//     pointer-events: auto on the divs keeps them clickable and
//     accessible (semantic markup, selectable label text). The SVG
//     itself is pointer-events: none so the overlay always wins.
//   * All geometry derives from a small set of JS constants
//     (COLUMN_WIDTH, DOT_RADIUS, etc.) shared with CSS via inline
//     style on the wrapper — no magic CSS-side numbers.
//
// A workspace with no parent metadata (legacy data) degrades to the
// previous "rejected footnote" form so the spine still renders.

import { el, svgEl } from '../core/dom.js';

// ---------------------------------------------------------------------------
// Geometry constants — every coordinate the spine draws derives from
// these. Tweaking one number rebalances the whole picture.
// ---------------------------------------------------------------------------
const COLUMN_WIDTH = 100;        // px between adjacent promoted-column centers
const DOT_RADIUS = 7;            // matches CSS .spine-dot width: 14px
const NODE_BOX_WIDTH = 88;       // promoted/live HTML box width
const NODE_BOX_HEIGHT = 64;      // promoted/live HTML box height (dot+label+scalar)
const CHIP_WIDTH = 56;           // rejected-branch chip width
const CHIP_HEIGHT = 22;          // rejected-branch chip height
const CHIP_COL_GAP = 4;          // horizontal gap between chips in a chip grid
const CHIP_ROW_GAP = 4;          // vertical gap between chip rows
const CHIPS_PER_ROW = 2;         // grid width for the chip arrangement
const CHIP_AREA_BOTTOM_GAP = 22; // pixels from the lowest chip row to the spine dot
const PROMOTED_DOT_Y = 110;      // Y of every promoted/live dot center
const CANVAS_PAD_X = 16;         // pixels of horizontal padding inside the canvas
const CANVAS_BOTTOM = 200;       // total canvas height (chip area + dot + label + scalar)

/**
 * Render a horizontal lineage spine.
 *
 * opts:
 *   nodes — [{id, scalar, promoted, decision, live, parent_id, href}]
 *   liveId — optional id of the live node (also flagged via node.live)
 *   onNodeClick — optional handler (id) => void
 *   showRejectedFootnote — if true (default), rejected nodes that have
 *     no known parent in the spine still appear in the rejected footer.
 *     Rejected nodes WITH a known parent always render as parent
 *     branches above the spine instead.
 */
export function renderSpine(opts) {
  const o = opts || {};
  const allNodes = Array.isArray(o.nodes) ? o.nodes.slice() : [];
  const showFootnote = o.showRejectedFootnote !== false;

  if (allNodes.length === 0) {
    return el('div', { class: 'spine spine-empty' }, [
      el('p', { class: 'spine-empty-msg' }, ['No generations yet.']),
    ]);
  }

  // Promoted (and live) nodes form the inline spine row.
  let spineNodes = allNodes.filter((n) => n && (n.promoted || n.live));
  let rejectedNodes = allNodes.filter((n) => n && !n.promoted && !n.live);

  // If nothing made it onto the spine (e.g. only rejected so far) show
  // ALL of them inline so the spine never collapses to nothing.
  if (spineNodes.length === 0) {
    spineNodes = allNodes;
    rejectedNodes = [];
  }

  // Bucket rejected challengers by their parent id.
  const spineIds = new Set(spineNodes.map((n) => n.id));
  const branchesByParent = new Map();
  const orphanRejected = [];
  for (const rn of rejectedNodes) {
    const pid = rn && rn.parent_id ? rn.parent_id : null;
    if (pid && spineIds.has(pid)) {
      if (!branchesByParent.has(pid)) branchesByParent.set(pid, []);
      branchesByParent.get(pid).push(rn);
    } else {
      orphanRejected.push(rn);
    }
  }

  // Sort branch chips into natural id order.
  for (const list of branchesByParent.values()) list.sort(_byGenId);

  // Layout pass — assign absolute (x, y) coordinates to every node.
  const layout = _computeLayout(spineNodes, branchesByParent);

  // Build the rendered tree: SVG canvas first, then the HTML overlay.
  const canvasWidth = layout.canvasWidth;
  const canvasHeight = CANVAS_BOTTOM;
  const stage = el('div', {
    class: 'spine-stage',
    style: `position: relative; width: ${canvasWidth}px; height: ${canvasHeight}px;`,
  }, []);

  // -- SVG connectors layer ------------------------------------------
  const svg = svgEl('svg', {
    class: 'spine-svg',
    width: canvasWidth,
    height: canvasHeight,
    viewBox: `0 0 ${canvasWidth} ${canvasHeight}`,
    'aria-hidden': 'true',
    // Make sure clicks pass through the SVG into the HTML overlay.
    style: 'position: absolute; left: 0; top: 0; pointer-events: none;',
  }, [_buildDefs()]);

  // Promoted → promoted curves + the live transition stub.
  for (let i = 0; i < layout.spineCols.length - 1; i += 1) {
    const from = layout.spineCols[i];
    const to = layout.spineCols[i + 1];
    svg.appendChild(_buildSpineConnector(from, to));
  }
  // Rejected child drops — one curve per chip.
  for (const chip of layout.chips) {
    svg.appendChild(_buildChipConnector(chip));
  }
  stage.appendChild(svg);

  // -- HTML overlay layer --------------------------------------------
  // Each promoted/live node gets a positioned spine-col container so
  // existing tests + the e2e tour can still locate "the column for v3"
  // semantically. Both the chip area (when present) and the spine
  // node box are absolutely positioned inside the column so each
  // lands on the exact (x, y) the SVG drew a connector to. This
  // sidesteps any flex-wrap reflow inside a narrow column container.
  for (const col of layout.spineCols) {
    const branches = col.branches;
    // Compute the chip-grid dimensions (matches _computeLayout).
    const childNodes = [];
    if (branches.length > 0) {
      const nC = Math.min(CHIPS_PER_ROW, branches.length);
      const nR = Math.ceil(branches.length / CHIPS_PER_ROW);
      const gridWidth = nC * CHIP_WIDTH + (nC - 1) * CHIP_COL_GAP;
      const gridHeight = nR * CHIP_HEIGHT + (nR - 1) * CHIP_ROW_GAP;
      const gridLeft = col.dotX - gridWidth / 2;
      const gridTop = col.dotY - DOT_RADIUS - CHIP_AREA_BOTTOM_GAP - gridHeight;
      const branchRow = _renderBranchGrid(col, branches, o.onNodeClick);
      branchRow.setAttribute(
        'style',
        `position: absolute; left: ${gridLeft - (col.dotX - NODE_BOX_WIDTH / 2)}px; `
          + `top: ${gridTop - (col.dotY - DOT_RADIUS - _nodeTopPadding())}px; `
          + `width: ${gridWidth}px;`,
      );
      childNodes.push(branchRow);
      childNodes.push(el('div', { class: 'spine-branch-tee', 'aria-hidden': 'true' }));
    }
    const nodeBox = _renderSpineNode(col.node, o.onNodeClick);
    // Anchor the node box so its dot center sits on (dotX, dotY).
    const colLeft = col.dotX - NODE_BOX_WIDTH / 2;
    const colTop = col.dotY - DOT_RADIUS - _nodeTopPadding();
    nodeBox.setAttribute(
      'style',
      `position: absolute; left: 0; top: 0; width: ${NODE_BOX_WIDTH}px;`,
    );
    childNodes.push(nodeBox);
    const colEl = el('div', {
      class: 'spine-col',
      style: `position: absolute; left: ${colLeft}px; top: ${colTop}px; `
        + `width: ${NODE_BOX_WIDTH}px;`,
    }, childNodes);
    stage.appendChild(colEl);
  }

  // Wrapper carries the horizontal scroll affordance when the spine
  // outgrows the viewport.
  const children = [
    el('div', { class: 'spine-scroll' }, [stage]),
  ];

  if (showFootnote && orphanRejected.length > 0) {
    const footChildren = [
      el('span', { class: 'spine-footer-label' }, ['rejected (no parent):']),
    ];
    for (const rn of orphanRejected) {
      footChildren.push(_renderRejectedChip(rn, o.onNodeClick));
    }
    children.push(el('div', { class: 'spine-footer' }, footChildren));
  }

  return el('div', { class: 'spine' }, children);
}

// ---------------------------------------------------------------------------
// Layout — pure geometry, no DOM. Exported below for unit tests.
// ---------------------------------------------------------------------------

function _computeLayout(spineNodes, branchesByParent) {
  const spineCols = [];
  for (let i = 0; i < spineNodes.length; i += 1) {
    const node = spineNodes[i];
    const dotX = CANVAS_PAD_X + (i + 0.5) * COLUMN_WIDTH;
    const dotY = PROMOTED_DOT_Y;
    const branches = branchesByParent.get(node.id) || [];
    spineCols.push({ node, index: i, dotX, dotY, branches });
  }

  // For each parent column with branches, lay out the chip grid above
  // its dot and remember each chip's absolute center.
  const chips = [];
  for (const col of spineCols) {
    if (col.branches.length === 0) continue;
    const n = col.branches.length;
    const cols = Math.min(CHIPS_PER_ROW, n);
    const rows = Math.ceil(n / CHIPS_PER_ROW);
    const gridWidth = cols * CHIP_WIDTH + (cols - 1) * CHIP_COL_GAP;
    const gridHeight = rows * CHIP_HEIGHT + (rows - 1) * CHIP_ROW_GAP;
    // Bottom of the chip grid sits CHIP_AREA_BOTTOM_GAP above the
    // parent dot's top edge.
    const gridBottom = col.dotY - DOT_RADIUS - CHIP_AREA_BOTTOM_GAP;
    const gridTop = gridBottom - gridHeight;
    const gridLeft = col.dotX - gridWidth / 2;
    for (let i = 0; i < n; i += 1) {
      const child = col.branches[i];
      const r = Math.floor(i / CHIPS_PER_ROW);
      const c = i % CHIPS_PER_ROW;
      const chipX = gridLeft + c * (CHIP_WIDTH + CHIP_COL_GAP);
      const chipY = gridTop + r * (CHIP_HEIGHT + CHIP_ROW_GAP);
      const chipCx = chipX + CHIP_WIDTH / 2;
      const chipCy = chipY + CHIP_HEIGHT / 2;
      const chipBottomY = chipY + CHIP_HEIGHT;
      chips.push({
        child, parentCol: col,
        x: chipX, y: chipY, cx: chipCx, cy: chipCy, bottomY: chipBottomY,
      });
    }
  }

  const canvasWidth = CANVAS_PAD_X * 2 + spineCols.length * COLUMN_WIDTH;
  return { spineCols, chips, canvasWidth };
}

// Pixel padding above the dot inside the spine-node HTML box (CSS
// .spine-node has padding-top ≈ var(--space-3) ≈ 12px). Kept as a
// helper so a CSS tweak is easy to mirror here.
function _nodeTopPadding() { return 12; }

// ---------------------------------------------------------------------------
// SVG path builders
// ---------------------------------------------------------------------------

function _buildDefs() {
  // One arrowhead marker per connector color. The marker is sized
  // small (4×4) so it reads as a finishing tip, not a giant arrow.
  const defs = svgEl('defs', {}, [
    _arrowMarker('spine-arrow-promoted', 'var(--color-promoted)'),
    _arrowMarker('spine-arrow-live', 'var(--color-accent)'),
    _arrowMarker('spine-arrow-rejected', 'var(--color-rejected)'),
  ]);
  return defs;
}

function _arrowMarker(id, fill) {
  // refX positions the arrow tip on the path endpoint. We use
  // orient="auto" so the tip rotates to follow the path direction.
  const marker = svgEl('marker', {
    id,
    markerWidth: 6,
    markerHeight: 6,
    refX: 5,
    refY: 3,
    orient: 'auto',
    markerUnits: 'userSpaceOnUse',
  }, [
    svgEl('path', {
      d: 'M0,0 L6,3 L0,6 Z',
      fill,
    }),
  ]);
  return marker;
}

function _buildSpineConnector(from, to) {
  const fromIsPromoted = !!from.node.promoted;
  const toIsLive = !!to.node.live;
  const toIsPromoted = !!to.node.promoted;
  const x1 = from.dotX + DOT_RADIUS;
  const y1 = from.dotY;
  const x2 = to.dotX - DOT_RADIUS;
  const y2 = to.dotY;
  // Use a gentle smooth curve so a long row still feels continuous
  // instead of a strict ruled line. Control points sit at the
  // horizontal midpoint, same Y as the endpoints.
  const mid = (x1 + x2) / 2;
  const d = `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
  const attrs = {
    d,
    fill: 'none',
    'stroke-linecap': 'round',
    'stroke-width': 2.25,
  };
  if (toIsLive) {
    attrs.class = 'spine-svg-conn spine-svg-conn-live';
    attrs.stroke = 'var(--color-accent)';
    attrs['stroke-dasharray'] = '6 4';
    attrs['marker-end'] = 'url(#spine-arrow-live)';
  } else if (fromIsPromoted && toIsPromoted) {
    attrs.class = 'spine-svg-conn spine-svg-conn-promoted';
    attrs.stroke = 'var(--color-promoted)';
    attrs['marker-end'] = 'url(#spine-arrow-promoted)';
  } else {
    attrs.class = 'spine-svg-conn spine-svg-conn-dashed';
    attrs.stroke = 'var(--color-border-strong)';
    attrs['stroke-dasharray'] = '5 4';
    attrs['marker-end'] = 'url(#spine-arrow-promoted)';
  }
  return svgEl('path', attrs);
}

function _buildChipConnector(chip) {
  // The chip's bottom-center drops down to the top of its parent's
  // dot. The path is a soft S so it feels natural, never a hard
  // right-angle. Tail at the parent dot's top.
  const x1 = chip.cx;
  const y1 = chip.bottomY;
  const x2 = chip.parentCol.dotX;
  const y2 = chip.parentCol.dotY - DOT_RADIUS;
  // Vertical drop with a slight lateral curve when chip and parent
  // are offset (chip in a multi-column grid).
  const dy = y2 - y1;
  const c1y = y1 + dy * 0.45;
  const c2y = y2 - dy * 0.35;
  const d = `M ${x1} ${y1} C ${x1} ${c1y}, ${x2} ${c2y}, ${x2} ${y2}`;
  return svgEl('path', {
    class: 'spine-svg-conn spine-svg-conn-rejected',
    d,
    fill: 'none',
    stroke: 'var(--color-rejected)',
    'stroke-linecap': 'round',
    'stroke-width': 1.4,
    'stroke-opacity': 0.7,
  });
}

// ---------------------------------------------------------------------------
// HTML overlay — node boxes + chip pills
// ---------------------------------------------------------------------------

function _renderBranchGrid(col, branches, onClick) {
  const chips = [];
  for (const b of branches) chips.push(_renderBranchChip(b, onClick));
  return el('div', {
    class: 'spine-branches',
    role: 'list',
    'aria-label': 'rejected challengers of ' + String(col.node.id || ''),
  }, chips);
}

function _byGenId(a, b) {
  const ai = String(a && a.id || '');
  const bi = String(b && b.id || '');
  // Natural sort on the numeric suffix when both ids look like vN; fall
  // back to string compare otherwise.
  const ma = ai.match(/^v(\d+)$/);
  const mb = bi.match(/^v(\d+)$/);
  if (ma && mb) return Number(ma[1]) - Number(mb[1]);
  return ai < bi ? -1 : ai > bi ? 1 : 0;
}

function _renderSpineNode(node, onClick) {
  const id = String(node.id || '?');
  const scalar = (typeof node.scalar === 'number' && isFinite(node.scalar))
    ? node.scalar.toFixed(3) : '—';
  const isLive = !!node.live;
  const isPromoted = !!node.promoted;

  const dotCls = ['spine-dot'];
  if (isLive) dotCls.push('spine-dot-live');
  else if (isPromoted) dotCls.push('spine-dot-promoted');
  else dotCls.push('spine-dot-rejected');

  const nodeCls = ['spine-node'];
  if (isLive) nodeCls.push('spine-node-live');
  else if (isPromoted) nodeCls.push('spine-node-promoted');
  else nodeCls.push('spine-node-rejected');

  const handler = onClick
    ? ((ev) => { ev.preventDefault(); onClick(id); })
    : null;

  const versionLabelChildren = [id];
  if (isLive) {
    versionLabelChildren.unshift(el('span', { class: 'spine-live-tag' }, ['LIVE ']));
  }

  const inner = el('div', { class: 'spine-node-inner' }, [
    el('div', { class: dotCls.join(' '), 'aria-hidden': 'true' }),
    el('div', { class: 'spine-node-label mono' }, versionLabelChildren),
    el('div', { class: 'spine-node-scalar mono' }, [scalar]),
  ]);

  if (node.href) {
    return el('a', {
      class: nodeCls.join(' ') + ' spine-node-link',
      href: node.href,
      role: 'listitem',
      'aria-label': `generation ${id}, scalar ${scalar}`,
    }, [inner]);
  }
  if (handler) {
    return el('div', {
      class: nodeCls.join(' ') + ' spine-node-clickable',
      role: 'listitem button',
      tabindex: '0',
      onclick: handler,
      'aria-label': `generation ${id}, scalar ${scalar}`,
    }, [inner]);
  }
  return el('div', {
    class: nodeCls.join(' '),
    role: 'listitem',
    'aria-label': `generation ${id}, scalar ${scalar}`,
  }, [inner]);
}

// A rejected challenger drawn as a small branch chip above its parent
// on the spine. Visually distinct from the orphan-rejected pill in the
// footer: the branch chip carries the implicit affordance "I challenged
// the parent below me".
function _renderBranchChip(node, onClick) {
  const id = String(node.id || '?');
  const scalar = (typeof node.scalar === 'number' && isFinite(node.scalar))
    ? node.scalar.toFixed(3) : null;
  const inner = [
    el('span', { class: 'spine-branch-id mono' }, [id]),
    scalar ? el('span', { class: 'spine-branch-scalar mono' }, [scalar]) : null,
  ].filter(Boolean);
  const ariaLabel = `rejected challenger ${id}`
    + (scalar ? `, scalar ${scalar}` : '');
  if (node.href) {
    return el('a', {
      class: 'spine-branch',
      href: node.href,
      role: 'listitem',
      'aria-label': ariaLabel,
    }, inner);
  }
  if (onClick) {
    const handler = (ev) => { ev.preventDefault(); onClick(id); };
    return el('span', {
      class: 'spine-branch',
      role: 'listitem button',
      tabindex: '0',
      onclick: handler,
      'aria-label': ariaLabel,
    }, inner);
  }
  return el('span', {
    class: 'spine-branch',
    role: 'listitem',
    'aria-label': ariaLabel,
  }, inner);
}

function _renderRejectedChip(node, onClick) {
  const id = String(node.id || '?');
  const scalar = (typeof node.scalar === 'number' && isFinite(node.scalar))
    ? node.scalar.toFixed(3) : null;
  const handler = onClick
    ? ((ev) => { ev.preventDefault(); onClick(id); })
    : null;
  const inner = [
    el('span', { class: 'spine-rejected-id mono' }, [id]),
    scalar ? el('span', { class: 'spine-rejected-scalar mono' }, [scalar]) : null,
  ];
  if (node.href) {
    return el('a', {
      class: 'spine-rejected-chip',
      href: node.href,
    }, inner);
  }
  if (handler) {
    return el('span', {
      class: 'spine-rejected-chip',
      role: 'button',
      tabindex: '0',
      onclick: handler,
    }, inner);
  }
  return el('span', { class: 'spine-rejected-chip' }, inner);
}

// ---------------------------------------------------------------------------
// Exports for tests — the layout pass is pure geometry and the tests
// pin the column / chip coordinates so a regression cannot slip the
// dots out of alignment.
// ---------------------------------------------------------------------------
export const SPINE_GEOMETRY = Object.freeze({
  COLUMN_WIDTH,
  DOT_RADIUS,
  NODE_BOX_WIDTH,
  NODE_BOX_HEIGHT,
  CHIP_WIDTH,
  CHIP_HEIGHT,
  CHIP_COL_GAP,
  CHIP_ROW_GAP,
  CHIPS_PER_ROW,
  CHIP_AREA_BOTTOM_GAP,
  PROMOTED_DOT_Y,
  CANVAS_PAD_X,
  CANVAS_BOTTOM,
});

export function computeSpineLayout(spineNodes, branchesByParent) {
  return _computeLayout(spineNodes, branchesByParent || new Map());
}

export function buildSpineConnectorPath(from, to) {
  const x1 = from.dotX + DOT_RADIUS;
  const x2 = to.dotX - DOT_RADIUS;
  const y1 = from.dotY;
  const y2 = to.dotY;
  const mid = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
}

export function buildChipConnectorPath(chip) {
  const x1 = chip.cx;
  const y1 = chip.bottomY;
  const x2 = chip.parentCol.dotX;
  const y2 = chip.parentCol.dotY - DOT_RADIUS;
  const dy = y2 - y1;
  const c1y = y1 + dy * 0.45;
  const c2y = y2 - dy * 0.35;
  return `M ${x1} ${y1} C ${x1} ${c1y}, ${x2} ${c2y}, ${x2} ${y2}`;
}
