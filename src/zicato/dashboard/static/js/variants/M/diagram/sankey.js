// variants/M/diagram/sankey.js — the Tufte causal-flow Sankey (Ledger II).
//
// The causal-flow Sankey: CANDIDATE → per-board LOSS → aggregate SCALAR.
// Redrawn Tufte-style, FIT-TO-WIDTH (viewBox + width:100%), with NO pan/zoom
// viewport. Reuses the three-stage flow layout (proportional node heights,
// stacked ribbons), ported here so Variant M stays self-contained.
//
// Convergence-II fix #5 — LABEL / VALUE ALIGNMENT. The old Ledger sankey
// stacked the entry label and its loss value on two baselines inside the
// node, but on a short node the value text overran the label (e.g.
// "picky_stakeholder_emu643…"). M fixes this by giving the per-board node a
// TWO-COLUMN line: the entry id is left-aligned (clipped to fit the column),
// the loss value is RIGHT-ALIGNED at the node's right edge — they share a
// baseline but never overlap because they anchor from opposite edges and the
// label is clipped to leave room. The pass/fail/timeout flag rides on a
// second sub-line.
//
// `buildTufteSankey(spec)` returns a detached <svg>. `spec`:
//   { genId, rows:[{entryId, driftLoss, passFail, budgetExceeded, runId}],
//     width, onBoard(row) }.

import { svgEl } from '../../../core/dom.js';
import { isNum, fmt } from '../svg.js';

export function layoutSankey(spec) {
  const colW = spec.colW || 150;
  const colGap = spec.colGap || 190;
  const nodeW = spec.nodeW || 150;
  const top = spec.top || 36;
  const colHeight = spec.colHeight || 420;
  const minNodeH = spec.minNodeH || 26;
  const gap = spec.nodeGap || 12;

  const stages = ['cand', 'board', 'agg'];
  const cols = { cand: spec.cand || [], board: spec.board || [], agg: spec.agg || [] };
  const links = spec.links || [];

  const throughput = (nodeId) => {
    let t = 0;
    for (const l of links) if (l.source === nodeId || l.target === nodeId) t += Math.abs(l.value || 0);
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
        label: n.label != null ? n.label : n.id, sub: n.sub || '', value: n.value,
        valueText: n.valueText != null ? n.valueText : '', ref: n.ref || null, cls: n.cls || '',
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
    const sBand = s.h * (v / (outSum.get(l.source) || v));
    const tBand = t.h * (v / (inSum.get(l.target) || v));
    const sx = s.x + s.w; const tx = t.x;
    const sy = s.y + s._outCursor + sBand / 2;
    const ty = t.y + t._inCursor + tBand / 2;
    s._outCursor += sBand; t._inCursor += tBand;
    linksOut.push({
      id: l.id || `${l.source}__${l.target}`, source: l.source, target: l.target,
      sx, sy, tx, ty, hwS: Math.max(0.6, sBand / 2), hwT: Math.max(0.6, tBand / 2), value: l.value, cls: l.cls || '',
    });
  }
  const totalW = 2 * (colW + colGap) + nodeW;
  return { nodes: nodesOut, links: linksOut, box: { x: 0, y: 0, w: totalW, h: colHeight + top * 2 } };
}

function ribbonPath(sx, sy, tx, ty, hwS, hwT) {
  const mx = (sx + tx) / 2;
  return `M ${sx} ${sy - hwS}`
    + ` C ${mx} ${sy - hwS}, ${mx} ${ty - hwT}, ${tx} ${ty - hwT}`
    + ` L ${tx} ${ty + hwT}`
    + ` C ${mx} ${ty + hwT}, ${mx} ${sy + hwS}, ${sx} ${sy + hwS} Z`;
}

function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

export function buildTufteSankey(spec) {
  const o = spec || {};
  const rows = (Array.isArray(o.rows) ? o.rows : []).filter((r) => r);
  const total = rows.reduce((a, r) => a + (isNum(r.driftLoss) ? r.driftLoss : 0), 0) || 0.0001;
  const genId = o.genId || 'candidate';

  const candNode = { id: 'cand', label: genId, sub: 'candidate', valueText: fmt(total, 1), cls: 'i-flow-cand', value: total };
  const boardNodes = rows.map((r) => {
    const cls = r.passFail === 1 ? 'i-flow-good' : (r.budgetExceeded ? 'i-flow-warn' : 'i-flow-bad');
    const passLabel = r.passFail === 1 ? 'pass' : (r.passFail === 0 ? 'fail' : 'no predicate');
    const flags = r.budgetExceeded ? ' · timeout' : '';
    return {
      id: 'b:' + r.entryId, label: r.entryId,
      // sub carries ONLY the qualitative flag; the numeric loss is its own
      // right-aligned value text (the fix: label and value never overlap).
      sub: passLabel + flags, valueText: fmt(r.driftLoss, 1),
      cls, value: Math.max(0.0001, isNum(r.driftLoss) ? r.driftLoss : 0), ref: r,
    };
  });
  const aggNode = { id: 'agg', label: 'aggregate', sub: 'Σ scalar', valueText: fmt(total, 1), cls: 'i-flow-agg', value: total };

  const links = [];
  for (const b of boardNodes) {
    links.push({ source: 'cand', target: b.id, value: b.value, cls: b.cls });
    links.push({ source: b.id, target: 'agg', value: b.value, cls: b.cls });
  }

  const colHeight = Math.max(300, boardNodes.length * 58);
  const layout = layoutSankey({
    cand: [candNode], board: boardNodes, agg: [aggNode], links,
    colW: 138, colGap: 200, nodeW: 140, colHeight,
  });

  const W = layout.box.w; const H = layout.box.h;
  const svg = svgEl('svg', {
    class: 'i-sankey', viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': `Causal flow for ${genId}: candidate to per-board loss to aggregate scalar`,
  });
  svg.style.setProperty('width', '100%');
  svg.style.setProperty('height', 'auto');

  const headers = [
    { x: layout.nodes.find((n) => n.stage === 'cand')?.x ?? 0, t: 'CANDIDATE' },
    { x: layout.nodes.find((n) => n.stage === 'board')?.x ?? 0, t: 'PER-BOARD LOSS' },
    { x: layout.nodes.find((n) => n.stage === 'agg')?.x ?? 0, t: 'AGGREGATE' },
  ];
  for (const hd of headers) svg.appendChild(svgEl('text', { x: hd.x, y: 16, class: 'i-sankey-col-head' }, [hd.t]));

  const ribbonLayer = svgEl('g', { class: 'i-ribbon-layer' });
  for (const l of layout.links) {
    ribbonLayer.appendChild(svgEl('path', {
      d: ribbonPath(l.sx, l.sy, l.tx, l.ty, l.hwS, l.hwT),
      class: 'i-ribbon ' + (l.cls || ''), 'data-source': l.source, 'data-target': l.target,
    }, [titleNode(`${l.source.replace(/^b:/, '')} → ${l.target.replace(/^b:/, '')}: ${fmt(l.value, 1)}`)]));
  }
  svg.appendChild(ribbonLayer);

  const nodeLayer = svgEl('g', { class: 'i-node-layer' });
  for (const n of layout.nodes) {
    const clickable = n.ref != null && typeof o.onBoard === 'function';
    const padL = 9; const padR = 8;
    const labelBaseline = n.y + Math.min(n.h / 2, 16);
    // The fix: the entry id anchors at the LEFT edge (clipped to leave room
    // for the value), and the loss value anchors at the RIGHT edge — they
    // share a baseline but anchor from opposite edges, so they cannot overlap.
    const grp = svgEl('g', {
      class: 'i-sankey-node ' + (n.cls || '') + (clickable ? ' i-clickable' : ''),
      'data-id': n.id, 'data-cz': clickable ? 'sankey-board-node' : 'sankey-node',
      'data-key': n.id, tabindex: clickable ? '0' : null,
      role: clickable ? 'button' : 'group', 'aria-label': `${n.label} ${n.sub} ${n.valueText}`,
    }, [
      svgEl('rect', { x: n.x, y: n.y, width: n.w, height: n.h, rx: 3, class: 'i-sankey-rect' }),
      svgEl('text', { x: n.x + padL, y: labelBaseline, class: 'i-sankey-label', 'text-anchor': 'start' }, [clip(n.label, 13)]),
      n.valueText ? svgEl('text', { x: n.x + n.w - padR, y: labelBaseline, class: 'i-sankey-value', 'text-anchor': 'end' }, [n.valueText]) : null,
      n.sub ? svgEl('text', { x: n.x + padL, y: labelBaseline + 13, class: 'i-sankey-sub', 'text-anchor': 'start' }, [clip(n.sub, 18)]) : null,
    ].filter(Boolean));
    if (clickable) {
      const open = () => o.onBoard(n.ref);
      grp.style.cursor = 'pointer';
      grp.addEventListener('click', open);
      grp.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); } });
    }
    nodeLayer.appendChild(grp);
  }
  svg.appendChild(nodeLayer);
  return svg;
}

function titleNode(text) {
  const t = svgEl('title', null);
  t.textContent = text == null ? '' : String(text);
  return t;
}
