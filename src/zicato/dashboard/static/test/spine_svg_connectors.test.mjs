// test/spine_svg_connectors.test.mjs — Task #199 SVG-based spine.
//
// Pins the SVG-canvas redraw of the generation spine:
//   * Layout geometry yields (x, y) coordinates derived from the
//     COLUMN_WIDTH / DOT_RADIUS / PROMOTED_DOT_Y constants.
//   * The spine canvas paints one <svg> with <path> connectors per
//     promoted-promoted hop, one per chip-to-parent drop, and a
//     dashed live transition stub.
//   * Multiple rejected children fan into a centered grid above the
//     parent column; their chip centers are arranged symmetrically.
//   * The live node sits to the right of its parent with a dashed
//     indigo connector running between the two dot centers.
//   * The empty-no-rejected case still paints a single SVG with the
//     spine-spine connectors, no chip drops, and no orphan footer.
//
// Every connector path is checked AT THE PATH LEVEL — the M/L/C
// segment that runs between actual dot centers, not a decorative arrow
// floating in the gap.

import { installDom, test, run, assert } from './harness.mjs';

installDom();

const spine = await import('../js/components/spine.js');
const {
  renderSpine,
  computeSpineLayout,
  buildSpineConnectorPath,
  buildChipConnectorPath,
  SPINE_GEOMETRY,
} = spine;

// Walk the descendants and return every element matching localName.
function descendantsByTag(node, tag) {
  const out = [];
  const lname = tag.toLowerCase();
  const walk = (n) => {
    if (!n || n.nodeType !== 1) return;
    if (n.localName === lname) out.push(n);
    for (const c of n.children) walk(c);
  };
  walk(node);
  return out;
}

function descendantsWithClass(node, cls) {
  const out = [];
  const walk = (n) => {
    if (!n || n.nodeType !== 1) return;
    if (n.classList && n.classList.contains(cls)) out.push(n);
    for (const c of n.children) walk(c);
  };
  walk(node);
  return out;
}

// Parse the M/C cubic-bezier path: returns { startX, startY, endX, endY }.
function parseCubicEndpoints(d) {
  // Path format: "M x1 y1 C cx1 cy1, cx2 cy2, x2 y2"
  const m = String(d || '').match(/^M\s+(\S+)\s+(\S+)\s+C\s+\S+\s+\S+,\s+\S+\s+\S+,\s+(\S+)\s+(\S+)$/);
  if (!m) return null;
  return {
    startX: Number(m[1]),
    startY: Number(m[2]),
    endX: Number(m[3]),
    endY: Number(m[4]),
  };
}

// ---------------------------------------------------------------------------
// (1) Layout geometry — pure function.
// ---------------------------------------------------------------------------

test('computeSpineLayout assigns evenly-spaced dot coordinates on the spine row', () => {
  const spineNodes = [
    { id: 'v0', promoted: true, parent_id: null },
    { id: 'v1', promoted: true, parent_id: 'v0' },
    { id: 'v3', promoted: true, parent_id: 'v1' },
  ];
  const layout = computeSpineLayout(spineNodes, new Map());
  assert(layout.spineCols.length === 3,
    'must produce one column per spine node; got ' + layout.spineCols.length);
  const xs = layout.spineCols.map((c) => c.dotX);
  // First column center sits one half-column-width past the left pad.
  const expectedFirst = SPINE_GEOMETRY.CANVAS_PAD_X + SPINE_GEOMETRY.COLUMN_WIDTH * 0.5;
  assert(xs[0] === expectedFirst,
    'first column dotX must be CANVAS_PAD_X + COLUMN_WIDTH/2; got ' + xs[0]
      + ' expected ' + expectedFirst);
  // Subsequent columns are spaced exactly COLUMN_WIDTH apart.
  assert(xs[1] - xs[0] === SPINE_GEOMETRY.COLUMN_WIDTH,
    'adjacent columns must be COLUMN_WIDTH apart; got '
      + (xs[1] - xs[0]) + ' expected ' + SPINE_GEOMETRY.COLUMN_WIDTH);
  assert(xs[2] - xs[1] === SPINE_GEOMETRY.COLUMN_WIDTH,
    'adjacent columns must be COLUMN_WIDTH apart; got '
      + (xs[2] - xs[1]) + ' expected ' + SPINE_GEOMETRY.COLUMN_WIDTH);
  // Every dot Y is identical — the spine is a strict horizontal row.
  for (const c of layout.spineCols) {
    assert(c.dotY === SPINE_GEOMETRY.PROMOTED_DOT_Y,
      'every dot must sit at PROMOTED_DOT_Y; got ' + c.dotY);
  }
  // Canvas width fits all columns + horizontal pad on both sides.
  const expectedWidth = SPINE_GEOMETRY.CANVAS_PAD_X * 2
    + 3 * SPINE_GEOMETRY.COLUMN_WIDTH;
  assert(layout.canvasWidth === expectedWidth,
    'canvasWidth must accommodate every column; got ' + layout.canvasWidth
      + ' expected ' + expectedWidth);
});

// ---------------------------------------------------------------------------
// (2) Connector path shape — runs dot-center to dot-center.
// ---------------------------------------------------------------------------

test('buildSpineConnectorPath runs from the right edge of one dot to the left edge of the next', () => {
  const from = { dotX: 50, dotY: 100, node: { promoted: true } };
  const to   = { dotX: 150, dotY: 100, node: { promoted: true } };
  const d = buildSpineConnectorPath(from, to);
  const e = parseCubicEndpoints(d);
  assert(e !== null, 'path must be a cubic bezier; got ' + d);
  // The path starts at (from.dotX + DOT_RADIUS, from.dotY) and ends at
  // (to.dotX - DOT_RADIUS, to.dotY) so the line meets the dot edge,
  // not its center — leaving room for the arrowhead marker to sit
  // flush against the destination dot.
  assert(e.startX === 50 + SPINE_GEOMETRY.DOT_RADIUS,
    'start X must clear the source dot radius; got ' + e.startX);
  assert(e.endX === 150 - SPINE_GEOMETRY.DOT_RADIUS,
    'end X must clear the destination dot radius; got ' + e.endX);
  assert(e.startY === 100 && e.endY === 100,
    'connector must remain on the spine row Y');
});

// ---------------------------------------------------------------------------
// (3) Multi-rejected children arrangement — fanned into a grid.
// ---------------------------------------------------------------------------

test('computeSpineLayout fans 4 rejected children of v3 into a 2x2 grid centered above v3', () => {
  const spineNodes = [
    { id: 'v0', promoted: true, parent_id: null },
    { id: 'v3', promoted: true, parent_id: 'v0' },
  ];
  const branches = new Map([
    ['v3', [
      { id: 'v4', parent_id: 'v3' },
      { id: 'v5', parent_id: 'v3' },
      { id: 'v6', parent_id: 'v3' },
      { id: 'v7', parent_id: 'v3' },
    ]],
  ]);
  const layout = computeSpineLayout(spineNodes, branches);
  // 4 chips → 4 chip records emitted.
  assert(layout.chips.length === 4,
    'four rejected children must emit four chip records; got '
      + layout.chips.length);
  // Each chip references v3's column as its parent.
  const v3Col = layout.spineCols.find((c) => c.node.id === 'v3');
  assert(v3Col, 'v3 column must exist');
  for (const ch of layout.chips) {
    assert(ch.parentCol === v3Col,
      'every chip must point at v3 as parent; got ' + ch.parentCol.node.id);
  }
  // The chip grid is centered on v3's dotX — chips 0 & 2 sit to the
  // LEFT of dotX, chips 1 & 3 to the RIGHT, symmetric around it.
  const left = layout.chips[0].cx;
  const right = layout.chips[1].cx;
  const dotX = v3Col.dotX;
  assert(Math.abs((dotX - left) - (right - dotX)) < 0.001,
    'chips must straddle the parent dotX symmetrically; '
      + 'left=' + left + ' dotX=' + dotX + ' right=' + right);
  // Two rows: chips 0 & 1 sit on a HIGHER row than chips 2 & 3.
  assert(layout.chips[0].cy < layout.chips[2].cy,
    'second row of chips must sit below the first row; '
      + 'row1Y=' + layout.chips[0].cy + ' row2Y=' + layout.chips[2].cy);
  // Every chip's bottomY clears the parent dot top by at least the
  // configured gap so the connector curve has room to breathe.
  for (const ch of layout.chips) {
    const parentTop = v3Col.dotY - SPINE_GEOMETRY.DOT_RADIUS;
    assert(ch.bottomY <= parentTop - SPINE_GEOMETRY.CHIP_AREA_BOTTOM_GAP + 0.001,
      'chip bottom must sit at least CHIP_AREA_BOTTOM_GAP above parent dot; '
        + 'bottomY=' + ch.bottomY + ' parentTop=' + parentTop);
  }
});

// ---------------------------------------------------------------------------
// (4) Live node placement — sits to the right of its parent with a
//     dashed indigo connector.
// ---------------------------------------------------------------------------

test('renderSpine paints a dashed live transition connector from the spine parent to the live node', () => {
  const node = renderSpine({
    nodes: [
      { id: 'v0', scalar: null, promoted: true, parent_id: null },
      { id: 'v3', scalar: null, promoted: true, parent_id: 'v0' },
      { id: 'v8', scalar: null, promoted: false, live: true, parent_id: 'v3' },
    ],
  });
  const svgs = descendantsByTag(node, 'svg');
  assert(svgs.length === 1,
    'spine must paint exactly one SVG canvas; got ' + svgs.length);
  const paths = descendantsByTag(svgs[0], 'path');
  // We expect: defs arrowhead paths (3) + 2 spine-spine connectors
  // (v0->v3, v3->v8) + 0 chip drops. The connectors live inside path
  // elements with class starting with spine-svg-conn.
  const conns = paths.filter((p) =>
    String(p.getAttribute('class') || '').includes('spine-svg-conn'));
  assert(conns.length === 2,
    'two spine connectors expected (v0->v3, v3->v8); got ' + conns.length);
  // The live connector must be dashed.
  const live = conns.find((p) =>
    String(p.getAttribute('class') || '').includes('spine-svg-conn-live'));
  assert(live !== undefined, 'live connector must paint');
  const dash = live.getAttribute('stroke-dasharray');
  assert(dash && dash.length > 0,
    'live connector must carry a stroke-dasharray; got ' + JSON.stringify(dash));
  // The live connector starts at v3's right edge and ends at v8's
  // left edge — both at the same Y (the spine row).
  const liveEndpoints = parseCubicEndpoints(live.getAttribute('d'));
  assert(liveEndpoints, 'live connector path must be a cubic bezier');
  assert(liveEndpoints.startY === liveEndpoints.endY,
    'live connector must remain on the spine row Y');
  assert(liveEndpoints.startY === SPINE_GEOMETRY.PROMOTED_DOT_Y,
    'live connector must use the promoted-row Y');
  assert(liveEndpoints.endX > liveEndpoints.startX,
    'live connector must flow left to right; got '
      + JSON.stringify(liveEndpoints));
});

// ---------------------------------------------------------------------------
// (5) No-rejected empty case — clean spine, no chip drops, no orphan footer.
// ---------------------------------------------------------------------------

test('renderSpine paints a clean spine SVG with zero chip drops when there are no rejected children', () => {
  const node = renderSpine({
    nodes: [
      { id: 'v0', scalar: 0.5, promoted: true, parent_id: null },
      { id: 'v1', scalar: 0.4, promoted: true, parent_id: 'v0' },
    ],
  });
  const svgs = descendantsByTag(node, 'svg');
  assert(svgs.length === 1, 'exactly one SVG must paint');
  const conns = descendantsByTag(svgs[0], 'path').filter((p) =>
    String(p.getAttribute('class') || '').includes('spine-svg-conn'));
  assert(conns.length === 1,
    'only the v0->v1 spine-row connector expected; got ' + conns.length);
  // The single connector must NOT be a rejected/chip path.
  const isChip = String(conns[0].getAttribute('class') || '').includes('spine-svg-conn-rejected');
  assert(!isChip,
    'no chip-drop connector should paint when there are no rejected nodes');
  // No orphan-rejected footer either.
  assert(!node.textContent.includes('rejected (no parent)'),
    'no orphan footer expected; got: ' + node.textContent.slice(0, 200));
  // No spine-branch chips, no spine-branch-tee markers.
  assert(descendantsWithClass(node, 'spine-branch').length === 0,
    'no branch chips when zero rejected children');
  assert(descendantsWithClass(node, 'spine-branch-tee').length === 0,
    'no branch-tee markers when zero rejected children');
});

// ---------------------------------------------------------------------------
// (6) Chip connector path — starts at the chip bottom-center and ends
//     at the parent dot top-center.
// ---------------------------------------------------------------------------

test('buildChipConnectorPath drops from chip bottom-center into parent dot top', () => {
  const parentCol = { dotX: 200, dotY: 110 };
  const chip = { cx: 180, cy: 50, bottomY: 60, parentCol };
  const d = buildChipConnectorPath(chip);
  const e = parseCubicEndpoints(d);
  assert(e !== null, 'chip connector must be a cubic bezier; got ' + d);
  assert(e.startX === chip.cx,
    'chip connector must start at chip cx; got ' + e.startX);
  assert(e.startY === chip.bottomY,
    'chip connector must start at chip bottomY; got ' + e.startY);
  assert(e.endX === parentCol.dotX,
    'chip connector must end at parent dotX; got ' + e.endX);
  assert(e.endY === parentCol.dotY - SPINE_GEOMETRY.DOT_RADIUS,
    'chip connector must end at parent dot top edge; got ' + e.endY);
});

// ---------------------------------------------------------------------------
// (7) Full tour fixture — every connector exists and connects real dots.
// ---------------------------------------------------------------------------

test('renderSpine on the tour fixture paints every connector with real dot endpoints', () => {
  // v0 → v1 (rejected v2) → v3 (rejected v4..v7) → v8 LIVE.
  const node = renderSpine({
    nodes: [
      { id: 'v0', scalar: null, promoted: true, parent_id: null },
      { id: 'v1', scalar: null, promoted: true, parent_id: 'v0' },
      { id: 'v2', scalar: null, promoted: false, parent_id: 'v1' },
      { id: 'v3', scalar: null, promoted: true, parent_id: 'v1' },
      { id: 'v4', scalar: null, promoted: false, parent_id: 'v3' },
      { id: 'v5', scalar: null, promoted: false, parent_id: 'v3' },
      { id: 'v6', scalar: null, promoted: false, parent_id: 'v3' },
      { id: 'v7', scalar: null, promoted: false, parent_id: 'v3' },
      { id: 'v8', scalar: null, promoted: false, live: true, parent_id: 'v3' },
    ],
  });
  const svgs = descendantsByTag(node, 'svg');
  assert(svgs.length === 1, 'tour fixture must paint one SVG');
  const conns = descendantsByTag(svgs[0], 'path').filter((p) =>
    String(p.getAttribute('class') || '').includes('spine-svg-conn'));
  // 3 promoted-promoted/live spine hops: v0->v1, v1->v3, v3->v8
  // + 5 chip drops: v2 over v1, v4..v7 over v3.
  assert(conns.length === 3 + 5,
    'expected 3 spine hops + 5 chip drops = 8 connectors; got ' + conns.length);
  // Every connector must be a cubic-bezier path with finite endpoints.
  for (const c of conns) {
    const e = parseCubicEndpoints(c.getAttribute('d'));
    assert(e !== null,
      'every connector must be a parseable cubic bezier; got '
        + c.getAttribute('d'));
    assert(Number.isFinite(e.startX) && Number.isFinite(e.startY)
      && Number.isFinite(e.endX) && Number.isFinite(e.endY),
      'connector endpoints must be finite; got ' + JSON.stringify(e));
  }
  // Three spine-row hops have promoted/live classes; the other five
  // are rejected drops.
  const liveOrPromoted = conns.filter((p) => {
    const cls = String(p.getAttribute('class') || '');
    return cls.includes('spine-svg-conn-promoted')
      || cls.includes('spine-svg-conn-live');
  });
  assert(liveOrPromoted.length === 3,
    'three promoted-or-live hops expected; got ' + liveOrPromoted.length);
  const rejectedDrops = conns.filter((p) =>
    String(p.getAttribute('class') || '').includes('spine-svg-conn-rejected'));
  assert(rejectedDrops.length === 5,
    'five rejected-chip drops expected; got ' + rejectedDrops.length);
});

await run();
