// variants/A/components/gauntlet.js — the bold lineage gauntlet bracket.
//
// The competition view rendered as a king-of-the-hill gauntlet with
// CLEAN LANES — the champion spine runs along a fixed top lane; each
// challenger that competed hangs in its own offset lane below, so lines
// NEVER collide. Promoted hops re-join the spine; rejected challengers
// terminate with an ✗. Optional in-flight tip pulses.
//
// nodes: ordered champion lineage [{ id, scalar, href, live }]
// challengers: [{ id, parentId, decision('promoted'|'rejected'|null),
//                 delta, href, live }]
// onSelect(id): click handler (also via href).

import { svgEl, el } from '../../../core/dom.js';

export function gauntlet({ spine, challengers, onSelect }) {
  spine = Array.isArray(spine) ? spine : [];
  challengers = Array.isArray(challengers) ? challengers : [];

  const COL = 150;       // horizontal step per champion node
  const SPINE_Y = 46;    // champion lane y
  const LANE_H = 64;     // vertical step per challenger lane
  const PAD_X = 30;
  const R = 19;          // node radius

  const cols = Math.max(spine.length, 1);
  // assign each rejected challenger a lane index under its parent col
  const parentCol = new Map();
  spine.forEach((n, i) => parentCol.set(n.id, i));

  // group rejected challengers by parent so we can stack lanes
  const rejected = challengers.filter((c) => c.decision !== 'promoted');
  let maxLane = 0;
  const laneOf = new Map();
  const perParentCount = new Map();
  for (const c of rejected) {
    const pc = parentCol.has(c.parentId) ? parentCol.get(c.parentId) : 0;
    const n = (perParentCount.get(pc) || 0) + 1;
    perParentCount.set(pc, n);
    laneOf.set(c, n);
    maxLane = Math.max(maxLane, n);
  }

  const W = PAD_X * 2 + (cols - 1) * COL + 160;
  const H = SPINE_Y + maxLane * LANE_H + 70;

  const svg = svgEl('svg', {
    width: W, height: H, viewBox: `0 0 ${W} ${H}`,
    class: 'mcA-gauntlet-svg', role: 'img',
    'aria-label': 'tournament gauntlet — champion spine with challenger lanes',
  });

  const cx = (i) => PAD_X + i * COL;

  // --- the spine line (champion through-line) ---
  if (spine.length >= 2) {
    let d = '';
    spine.forEach((n, i) => { d += (i === 0 ? 'M' : 'L') + cx(i) + ' ' + SPINE_Y + ' '; });
    svg.appendChild(svgEl('path', {
      d: d.trim(), fill: 'none',
      stroke: 'var(--mc-accent)', 'stroke-width': '3', 'stroke-linecap': 'round',
      opacity: '0.85',
    }));
  }

  // --- rejected challenger branches (own lanes, never colliding) ---
  for (const c of rejected) {
    const pc = parentCol.has(c.parentId) ? parentCol.get(c.parentId) : 0;
    const lane = laneOf.get(c);
    const y = SPINE_Y + lane * LANE_H;
    const x0 = cx(pc);
    const x1 = cx(pc) + COL * 0.62;
    // an elbow from the spine down into the lane
    const d = `M ${x0} ${SPINE_Y} C ${x0 + 36} ${SPINE_Y}, ${x1 - 36} ${y}, ${x1} ${y}`;
    svg.appendChild(svgEl('path', {
      d, fill: 'none', stroke: 'var(--mc-stop)',
      'stroke-width': '1.6', opacity: '0.5', 'stroke-dasharray': '4 3',
    }));
    appendNode(svg, x1, y, c, R, onSelect, 'rejected');
  }

  // --- champion spine nodes (drawn last, on top) ---
  spine.forEach((n, i) => {
    appendNode(svg, cx(i), SPINE_Y, n, R, onSelect, n.live ? 'live' : 'promoted');
  });

  return svg;
}

function appendNode(svg, x, y, n, R, onSelect, kind) {
  const g = svgEl('g', { class: 'mcA-gnode', style: onSelect || n.href ? 'cursor:pointer' : '' });
  const colorByKind = {
    promoted: 'var(--mc-go)',
    rejected: 'var(--mc-stop)',
    live: 'var(--mc-live)',
  };
  const stroke = colorByKind[kind] || 'var(--mc-idle)';

  if (kind === 'live') {
    const halo = svgEl('circle', { cx: x, cy: y, r: R + 4, fill: 'none', stroke, 'stroke-width': '1', opacity: '0.5' });
    halo.appendChild(svgEl('animate', { attributeName: 'r', values: `${R + 2};${R + 9};${R + 2}`, dur: '1.8s', repeatCount: 'indefinite' }));
    halo.appendChild(svgEl('animate', { attributeName: 'opacity', values: '0.6;0;0.6', dur: '1.8s', repeatCount: 'indefinite' }));
    g.appendChild(halo);
  }

  g.appendChild(svgEl('circle', {
    cx: x, cy: y, r: R,
    fill: 'var(--mc-bg-2)', stroke, 'stroke-width': '2.4',
  }));
  // glow ring
  g.appendChild(svgEl('circle', {
    cx: x, cy: y, r: R, fill: 'none', stroke, 'stroke-width': '6', opacity: '0.12',
  }));
  const label = svgEl('text', {
    x, y: y + 4, 'text-anchor': 'middle',
    fill: 'var(--mc-text)', 'font-size': '12', 'font-weight': '600',
    'font-family': 'var(--mc-mono)',
  });
  label.textContent = String(n.id || '?');
  g.appendChild(label);

  // scalar / delta caption below
  let caption = '';
  if (kind === 'rejected' && typeof n.delta === 'number' && isFinite(n.delta)) {
    caption = (n.delta > 0 ? '+' : '') + n.delta.toFixed(2) + '  ✗';
  } else if (typeof n.scalar === 'number' && isFinite(n.scalar)) {
    caption = n.scalar.toFixed(3);
  } else if (kind === 'live') {
    caption = '◀ running';
  }
  if (caption) {
    const cap = svgEl('text', {
      x, y: y + R + 15, 'text-anchor': 'middle',
      fill: kind === 'rejected' ? 'var(--mc-stop)' : 'var(--mc-text-3)',
      'font-size': '10.5', 'font-family': 'var(--mc-mono)',
    });
    cap.textContent = caption;
    g.appendChild(cap);
  }

  if (onSelect && n.id) g.addEventListener('click', () => onSelect(n.id));
  svg.appendChild(g);
}

// A compact legend strip for the gauntlet.
export function gauntletLegend() {
  const item = (color, label) => el('span', {
    class: 'mcA-readout-foot',
    style: 'display:inline-flex;align-items:center;gap:6px;margin-right:16px;',
  }, [
    el('span', { style: `width:9px;height:9px;border-radius:50%;background:${color};display:inline-block;` }),
    label,
  ]);
  return el('div', { style: 'margin-top:8px;' }, [
    item('var(--mc-accent)', 'champion spine'),
    item('var(--mc-go)', 'promoted'),
    item('var(--mc-stop)', 'rejected challenger'),
    item('var(--mc-live)', 'in flight'),
  ]);
}
