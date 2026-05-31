// variants/C/diagram/sankey.js — the causal-flow Sankey layout.
//
// THE signature data structure of Variant C. A three-stage flow:
//
//     PATCH (mutation points)  →  DRIFT KINDS that moved  →  GATE verdict
//        the cause                    the effect                the verdict
//
// This module is PURE layout: given the three node columns and the link
// magnitudes, it returns positioned nodes + ribbons in world coordinates.
// The view module turns that into SVG and wires interactivity.
//
// Returns { nodes: [{id, stage, x, y, h, w, label, sub, cls, value}],
//           links: [{id, source, target, sx, sy, tx, ty, hwS, hwT,
//                    value, cls}], box }.

export function layoutSankey(spec) {
  const colW = spec.colW || 200;
  const colGap = spec.colGap || 200;
  const nodeW = spec.nodeW || 150;
  const top = spec.top || 40;
  const colHeight = spec.colHeight || 460;
  const minNodeH = spec.minNodeH || 26;
  const gap = spec.nodeGap || 14;

  const stages = ['patch', 'drift', 'gate'];
  const cols = {
    patch: spec.patch || [],
    drift: spec.drift || [],
    gate: spec.gate || [],
  };
  const links = spec.links || [];

  // A node's "throughput" is the sum of the link values touching it (or
  // an explicit `value`). Heights are proportional to throughput within
  // a column, with a floor so a tiny node stays clickable.
  const throughput = (nodeId) => {
    let t = 0;
    for (const l of links) {
      if (l.source === nodeId || l.target === nodeId) t += Math.abs(l.value || 0);
    }
    return t;
  };

  const positioned = new Map();
  const nodesOut = [];

  stages.forEach((stage, si) => {
    const list = cols[stage];
    const x = si * (colW + colGap);
    // Compute proportional heights.
    const raw = list.map((n) => Math.max(0.0001, n.value != null ? Math.abs(n.value) : throughput(n.id)));
    const total = raw.reduce((a, b) => a + b, 0) || 1;
    const avail = colHeight - gap * Math.max(0, list.length - 1);
    let y = top;
    // Centre the column block vertically.
    const heights = raw.map((r) => Math.max(minNodeH, (r / total) * avail));
    const blockH = heights.reduce((a, b) => a + b, 0) + gap * Math.max(0, list.length - 1);
    y = top + Math.max(0, (colHeight - blockH) / 2);
    list.forEach((n, i) => {
      const h = heights[i];
      const node = {
        id: n.id, stage, x, y, h, w: nodeW,
        label: n.label != null ? n.label : n.id,
        sub: n.sub || '',
        cls: n.cls || '',
        value: n.value,
        ref: n.ref || null,
        // running cursors for stacking ribbons at the node edges
        _outCursor: 0, _inCursor: 0,
      };
      positioned.set(n.id, node);
      nodesOut.push(node);
      y += h + gap;
    });
  });

  // Build ribbons. A link's vertical half-width at each end is scaled to
  // its value relative to the node it attaches to, stacked so multiple
  // links share a node edge without overlapping.
  const linksOut = [];
  // Pre-sum out/in per node for proportional half-widths.
  const outSum = new Map();
  const inSum = new Map();
  for (const l of links) {
    outSum.set(l.source, (outSum.get(l.source) || 0) + Math.abs(l.value || 0));
    inSum.set(l.target, (inSum.get(l.target) || 0) + Math.abs(l.value || 0));
  }
  for (const l of links) {
    const s = positioned.get(l.source);
    const t = positioned.get(l.target);
    if (!s || !t) continue;
    const v = Math.abs(l.value || 0) || 0.0001;
    const sShare = v / (outSum.get(l.source) || v);
    const tShare = v / (inSum.get(l.target) || v);
    const sBand = s.h * sShare;
    const tBand = t.h * tShare;
    const sx = s.x + s.w;
    const tx = t.x;
    const sy = s.y + s._outCursor + sBand / 2;
    const ty = t.y + t._inCursor + tBand / 2;
    s._outCursor += sBand;
    t._inCursor += tBand;
    linksOut.push({
      id: l.id || `${l.source}__${l.target}`,
      source: l.source, target: l.target,
      sx, sy, tx, ty,
      hwS: Math.max(1, sBand / 2), hwT: Math.max(1, tBand / 2),
      value: l.value, cls: l.cls || '',
    });
  }

  const totalW = 2 * (colW + colGap) + nodeW;
  const box = { x: 0, y: 0, w: totalW, h: colHeight + top * 2 };
  return { nodes: nodesOut, links: linksOut, box };
}
