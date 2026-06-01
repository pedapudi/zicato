// variants/H/diagram/sankey.js — the Tufte causal-flow Sankey (Atlas II).
//
// A three-stage causal flow, re-skinned Tufte from Variant C's signature
// sankey:
//
//     CANDIDATE  →  PER-BOARD LOSS  →  AGGREGATE SCALAR
//      the source       the effect          the verdict
//
// Round-3 discipline (the operator's two complaints about F/G):
//   * FIT-TO-WIDTH, NO pan/zoom viewport. The view passes the measured
//     container width; the layout fills exactly that width so the diagram
//     never overflows into a pannable scroller. The <svg> is `width:100%;
//     height:auto` (in CSS) so it also stays responsive on resize.
//   * HIGH DATA-INK / Tufte: thin flows (stroke, not filled gradient
//     ribbons), DIRECT in-place labels at each node (no legend lookup),
//     minimal chrome (a hairline column header, no axes/boxes/shadows),
//     restrained improve/regress colour (a passing board entry's flow reads
//     `good`, a failing/timed-out one reads `bad`, otherwise neutral).
//
// `layoutSankey` is the PURE plumbing (ported from C, kept as the layout
// math); `tufteSankey` turns that layout into a detached, fit-to-width SVG
// of THIN stroked flows + direct labels. Mark classes are `hs-*`, styled by
// css/variants/H/atlas2.css.

import { svgEl } from '../../../core/dom.js';
import { isNum, fmt } from '../svg.js';

// ---- pure layout (ported from variants/C/diagram/sankey.js) ---------
// Given three node columns + link magnitudes, returns positioned nodes +
// ribbon anchor points in world coordinates. Node heights are proportional
// to throughput within a column (with a floor so a tiny node stays legible).
export function layoutSankey(spec) {
  const colW = spec.colW || 8;          // a thin node "tick", not a fat box
  const colGap = spec.colGap || 200;
  const nodeW = spec.nodeW || colW;
  const top = spec.top || 40;
  const colHeight = spec.colHeight || 460;
  const minNodeH = spec.minNodeH || 14;
  const gap = spec.nodeGap || 14;

  const stages = ['source', 'effect', 'verdict'];
  const cols = {
    source: spec.source || [],
    effect: spec.effect || [],
    verdict: spec.verdict || [],
  };
  const links = spec.links || [];

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
    const raw = list.map((n) => Math.max(0.0001, n.value != null ? Math.abs(n.value) : throughput(n.id)));
    const total = raw.reduce((a, b) => a + b, 0) || 1;
    const avail = colHeight - gap * Math.max(0, list.length - 1);
    const heights = raw.map((r) => Math.max(minNodeH, (r / total) * avail));
    const blockH = heights.reduce((a, b) => a + b, 0) + gap * Math.max(0, list.length - 1);
    let y = top + Math.max(0, (colHeight - blockH) / 2);
    list.forEach((n, i) => {
      const h = heights[i];
      const node = {
        id: n.id, stage, x, y, h, w: nodeW,
        label: n.label != null ? n.label : n.id,
        sub: n.sub || '',
        cls: n.cls || '',
        value: n.value,
        _outCursor: 0, _inCursor: 0,
      };
      positioned.set(n.id, node);
      nodesOut.push(node);
      y += h + gap;
    });
  });

  const linksOut = [];
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
      hwS: Math.max(0.6, sBand / 2), hwT: Math.max(0.6, tBand / 2),
      value: l.value, cls: l.cls || '',
    });
  }

  const totalW = 2 * (colW + colGap) + nodeW;
  const box = { x: 0, y: 0, w: totalW, h: colHeight + top * 2 };
  return { nodes: nodesOut, links: linksOut, box };
}

function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

// Build the candidate→per-board→aggregate flow spec from a candidate's
// per-entry rows. Each board entry is one `effect` node whose value is its
// drift loss; the flow's class reads pass/fail/timeout (restrained colour).
//
// rows: [{ entry_id, drift_loss, pass_fail, wall_clock_budget_exceeded }]
export function buildCandidateFlow(genId, rows) {
  const entries = (Array.isArray(rows) ? rows : []).filter((r) => r && isNum(r.drift_loss));
  const total = entries.reduce((a, r) => a + Math.abs(r.drift_loss), 0);
  const source = [{ id: 'cand', label: genId || 'candidate', sub: 'candidate', cls: 'hs-accent', value: total || 1 }];
  const effect = entries.map((r) => ({
    id: 'e:' + r.entry_id, label: r.entry_id, sub: fmt(r.drift_loss, 0),
    value: Math.abs(r.drift_loss) || 0.0001,
    cls: r.pass_fail === 1 ? 'hs-good' : (r.wall_clock_budget_exceeded ? 'hs-bad' : (r.pass_fail === 0 ? 'hs-bad' : 'hs-neutral')),
  }));
  const verdict = [{ id: 'agg', label: 'Σ scalar', sub: fmt(total, 0), cls: 'hs-accent', value: total || 1 }];
  const links = [];
  for (const e of effect) {
    links.push({ id: 'in:' + e.id, source: 'cand', target: e.id, value: e.value, cls: 'hs-neutral' });
    links.push({ id: 'out:' + e.id, source: e.id, target: 'agg', value: e.value, cls: e.cls });
  }
  return { source, effect, verdict, links };
}

// A smooth cubic between two world points (left→right flow).
function flowPath(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
}

// tufteSankey — the fit-to-width, high-data-ink renderer.
//
// opts: { width, flow (a buildCandidateFlow spec), onEntry(entryId) }.
// The SVG carries a viewBox sized to the layout and is laid out to the
// requested `width`; CSS keeps it `width:100%;height:auto` so it always
// fits the container and never needs a pan/zoom surface.
export function tufteSankey(opts) {
  const o = opts || {};
  const flow = o.flow || { source: [], effect: [], verdict: [], links: [] };
  const W = Math.max(360, o.width || 900);

  const effect = flow.effect || [];
  if (effect.length === 0) {
    const svg = svgEl('svg', { class: 'hs-sankey', width: W, height: 80, viewBox: `0 0 ${W} 80`, role: 'img' });
    const t = svgEl('text', { x: W / 2, y: 44, class: 'hs-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no per-board loss profile yet';
    svg.appendChild(t);
    return svg;
  }

  // Reserve label gutters: source label on the left, aggregate on the right,
  // per-entry labels to the right of the middle ticks. The flow band sits
  // between two fixed gutters so the whole figure fits `W` exactly.
  const leftGutter = 96;
  const rightGutter = 110;
  const colGap = Math.max(120, (W - leftGutter - rightGutter) / 2 - 8);
  const top = 30;
  const rowH = 22;
  const colHeight = Math.max(rowH, effect.length * (rowH + 6));

  const layout = layoutSankey({
    colW: 7, colGap, nodeW: 7, top, colHeight, minNodeH: 12, nodeGap: 8,
    source: flow.source, effect: flow.effect, verdict: flow.verdict, links: flow.links,
  });

  // World→placed transform: shift everything right by leftGutter.
  const ox = leftGutter;
  const H = layout.box.h;
  const svg = svgEl('svg', {
    class: 'hs-sankey', width: W, height: H,
    viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': 'causal flow: candidate to per-board loss to aggregate scalar',
  });

  // Column headers (hairline chrome only).
  const xs = { source: ox, effect: ox + 7 + colGap, verdict: ox + 2 * (7 + colGap) };
  const heads = [[xs.source, 'CANDIDATE'], [xs.effect, 'PER-BOARD LOSS'], [xs.verdict, 'Σ SCALAR']];
  for (const [x, t] of heads) {
    const th = svgEl('text', { x, y: 16, class: 'hs-col-head' });
    th.textContent = t;
    svg.appendChild(th);
  }

  // Thin stroked flows (data-ink, not filled ribbons). Stroke width scales
  // gently with the band so magnitude still reads, but stays thin.
  const flowLayer = svgEl('g', { class: 'hs-flow-layer' });
  for (const l of layout.links) {
    const sw = Math.max(1, Math.min(6, (l.hwS + l.hwT)));
    const p = svgEl('path', {
      d: flowPath(l.sx + ox, l.sy, l.tx + ox, l.ty),
      class: 'hs-flow ' + (l.cls || 'hs-neutral'), 'stroke-width': sw,
    });
    flowLayer.appendChild(p);
  }
  svg.appendChild(flowLayer);

  // Node ticks + direct labels.
  const nodeLayer = svgEl('g', { class: 'hs-node-layer' });
  for (const n of layout.nodes) {
    const x = n.x + ox;
    const yTop = n.y;
    const yBot = n.y + n.h;
    const cy = n.y + n.h / 2;
    const tickCls = n.cls && n.cls.startsWith('hs-') ? n.cls.replace('hs-', '') : 'accent';
    nodeLayer.appendChild(svgEl('line', {
      x1: x, y1: yTop, x2: x, y2: yBot, class: 'hs-node-tick hs-' + tickCls,
    }));
    if (n.stage === 'source') {
      const t = svgEl('text', { x: x - 6, y: cy + 3, class: 'hs-node-label', 'text-anchor': 'end' });
      t.textContent = clip(n.label, 14);
      nodeLayer.appendChild(t);
    } else if (n.stage === 'verdict') {
      const t = svgEl('text', { x: x + 8, y: cy, class: 'hs-node-label' });
      t.textContent = clip(n.label, 12);
      nodeLayer.appendChild(t);
      const s = svgEl('text', { x: x + 8, y: cy + 12, class: 'hs-node-sub' });
      s.textContent = n.sub || '';
      nodeLayer.appendChild(s);
    } else {
      // effect — direct label + value, to the right of the tick.
      const g = svgEl('g', {
        class: 'hs-effect-node', tabindex: o.onEntry ? '0' : null,
        'data-cz': 'sankey-effect', 'data-key': String(n.id).replace(/^e:/, ''),
      });
      const t = svgEl('text', { x: x + 8, y: cy + 3, class: 'hs-node-label' });
      t.textContent = clip(n.label, 22);
      g.appendChild(t);
      const s = svgEl('text', { x: x + 8 + labelW(n.label), y: cy + 3, class: 'hs-node-sub' });
      s.textContent = '  ' + (n.sub || '');
      g.appendChild(s);
      g.appendChild(svgEl('title', null, [`${n.label}: loss ${n.sub}`]));
      if (o.onEntry) {
        g.style.cursor = 'pointer';
        const eid = String(n.id).replace(/^e:/, '');
        g.addEventListener('click', () => o.onEntry(eid));
        g.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onEntry(eid); }
        });
      }
      nodeLayer.appendChild(g);
    }
  }
  svg.appendChild(nodeLayer);
  return svg;
}

// Rough monospace advance (px) for placing the value after a label.
function labelW(s) { return Math.min(22, String(s == null ? '' : s).length) * 6.2; }
