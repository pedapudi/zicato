// variants/O/svg.js — Variant O's ("Compass") Tufte figure toolkit.
//
// Self-contained for Variant O. Dependency-free SVG primitives that fit
// to their container (NO pan/zoom viewport). Carries the round-4 fixes:
//   * sankey (FIX 5): the per-board node LABEL and its loss VALUE never
//     overlap — the value is right-aligned on its own baseline inside the
//     node, with the label left-aligned and truncated to leave room.
//   * heatmap (FIX 4): accepts a `ramp:[loHex, hiHex]` derived from the
//     ACTIVE color theme's tokens at draw time (the caller resolves it via
//     ui.heatRamp), so it reads in all three themes.
//   * mutationMatrix: the site × generation surface (FIX 2's matrix half).
//   * sortedBars: the per-board cross-candidate comparative chart (FIX 7).
// Marks are addressable SVG nodes so hover (<title>) + click are
// first-class. Lower loss is BETTER.

import { svgEl } from '../../core/dom.js';

export const NS = 'http://www.w3.org/2000/svg';

export function isNum(v) { return typeof v === 'number' && isFinite(v); }
export function finiteValues(arr) { return (Array.isArray(arr) ? arr : []).filter(isNum); }

export function extent(values) {
  const v = finiteValues(values);
  if (v.length === 0) return [0, 1];
  let lo = v[0]; let hi = v[0];
  for (const x of v) { if (x < lo) lo = x; if (x > hi) hi = x; }
  if (lo === hi) { lo -= 0.5; hi += 0.5; }
  return [lo, hi];
}

export function scale(domain, range) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  return (x) => r0 + ((x - d0) / span) * (r1 - r0);
}

export function fmt(v, digits) {
  if (!isNum(v)) return '—';
  const d = digits == null ? 3 : digits;
  return v.toFixed(d);
}
export function fmtSigned(v, digits) {
  if (!isNum(v)) return '—';
  const d = digits == null ? 3 : digits;
  return (v > 0 ? '+' : '') + v.toFixed(d);
}

export function title(text) {
  const t = svgEl('title', null);
  t.textContent = text == null ? '' : String(text);
  return t;
}

export function shortLabel(s, n) {
  const max = isNum(n) ? n : 14;
  const str = s == null ? '' : String(s);
  return str.length > max ? str.slice(0, max - 1) + '…' : str;
}

export function decollide(items, y, minGap, top, bottom) {
  const idx = items.map((it, i) => ({ i, pos: isNum(it.v) ? y(it.v) : (top + bottom) / 2 }));
  idx.sort((p, q) => p.pos - q.pos);
  for (let k = 1; k < idx.length; k++) {
    if (idx[k].pos - idx[k - 1].pos < minGap) idx[k].pos = idx[k - 1].pos + minGap;
  }
  if (idx.length && idx[idx.length - 1].pos > bottom) {
    idx[idx.length - 1].pos = bottom;
    for (let k = idx.length - 2; k >= 0; k--) {
      if (idx[k + 1].pos - idx[k].pos < minGap) idx[k].pos = idx[k + 1].pos - minGap;
    }
  }
  if (idx.length && idx[0].pos < top) idx[0].pos = top;
  const out = new Array(items.length);
  for (const p of idx) out[p.i] = p.pos;
  return out;
}

export function jitterColumn(ys, step) {
  const out = ys.slice();
  const groups = new Map();
  ys.forEach((v, i) => {
    if (!isNum(v)) return;
    const key = Math.round(v * 2) / 2;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(i);
  });
  for (const idxs of groups.values()) {
    if (idxs.length < 2) continue;
    const n = idxs.length;
    const mid = (n - 1) / 2;
    idxs.forEach((idx, k) => { out[idx] = ys[idx] + (k - mid) * (step || 3); });
  }
  return out;
}

// ---- lineage bumps (non-colliding, clickable) -----------------------

export function bumps(opts) {
  const o = opts || {};
  const nodes = (Array.isArray(o.nodes) ? o.nodes : []).filter((n) => n);
  const w = o.width || 640;
  const h = o.height || 190;
  const padX = 48; const spineY = h * 0.38; const challY = h * 0.78;
  const svg = svgEl('svg', { class: 'vo-bumps', width: w, height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (nodes.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'vo-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no generations yet';
    svg.appendChild(t);
    return svg;
  }
  const maxX = Math.max(1, ...nodes.map((n) => n.x || 0));
  const X = scale([0, maxX], [padX, w - padX]);

  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: spineY, y2: spineY, class: 'vo-lane-guide vo-spine-guide' }));
  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: challY, y2: challY, class: 'vo-lane-guide' }));
  const lblS = svgEl('text', { x: 6, y: spineY - 9, class: 'vo-lane-label' }); lblS.textContent = 'champion';
  const lblC = svgEl('text', { x: 6, y: challY - 9, class: 'vo-lane-label' }); lblC.textContent = 'challenger';
  svg.appendChild(lblS); svg.appendChild(lblC);

  const laneY = (n) => (n.promoted ? spineY : challY);
  const byId = new Map(nodes.map((n) => [n.id, n]));

  const promoted = nodes.filter((n) => n.promoted).sort((a, b) => (a.x || 0) - (b.x || 0));
  for (let i = 1; i < promoted.length; i++) {
    svg.appendChild(svgEl('line', { x1: X(promoted[i - 1].x), y1: spineY, x2: X(promoted[i].x), y2: spineY, class: 'vo-spine-line' }));
  }
  for (const n of nodes) {
    if (n.promoted) continue;
    const p = n.parent ? byId.get(n.parent) : null;
    const px = p ? X(p.x) : X((n.x || 1) - 1);
    const py = p ? laneY(p) : spineY;
    const nx = X(n.x);
    const path = `M${px},${py} C${(px + nx) / 2},${py} ${(px + nx) / 2},${challY} ${nx},${challY}`;
    svg.appendChild(svgEl('path', { d: path, class: 'vo-branch', fill: 'none' }));
  }

  // De-collide nodes that share an x WITHIN a lane (v1/v2 off one parent).
  const laneGroups = new Map();
  for (const n of nodes) {
    const key = (n.promoted ? 'c' : 'h') + ':' + (n.x || 0);
    if (!laneGroups.has(key)) laneGroups.set(key, []);
    laneGroups.get(key).push(n);
  }
  const nodeX = new Map();
  for (const grp of laneGroups.values()) {
    const n = grp.length; const mid = (n - 1) / 2;
    grp.forEach((node, k) => { nodeX.set(node, X(node.x) + (k - mid) * 24); });
  }

  for (const n of nodes) {
    const cx = nodeX.get(n);
    const cy = laneY(n);
    const sel = o.selected && n.id === o.selected;
    const cls = 'vo-bump-node ' + (n.promoted ? 'vo-promoted' : 'vo-rejected') + (sel ? ' vo-sel' : '');
    const c = svgEl('circle', { cx, cy, r: n.promoted ? 5 : 3.8, class: cls, tabindex: o.onClick ? '0' : null,
      role: o.onClick ? 'button' : 'img', 'data-vo': 'bump-node', 'data-gen': n.id, 'aria-label': `candidate ${n.id}` },
    [title(`${n.id}${isNum(n.scalar) ? ' · ' + fmt(n.scalar) : ''} · ${n.promoted ? 'promoted' : 'rejected'}`)]);
    if (o.onClick) {
      c.style.cursor = 'pointer';
      c.addEventListener('click', () => o.onClick(n));
      c.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onClick(n); } });
    }
    svg.appendChild(c);
    const t = svgEl('text', { x: cx, y: cy + 17, class: 'vo-bump-label', 'text-anchor': 'middle' });
    t.textContent = shortLabel(n.id, 8);
    svg.appendChild(t);
  }
  return svg;
}

// ---- Tufte sankey (FIX 5: label ≠ value) ----------------------------
//
// candidate → per-board loss → aggregate scalar. Fit-to-width, thin
// ribbons ∝ loss share. CRITICAL: the per-board node's LABEL is left
// -aligned and truncated; its loss VALUE is RIGHT-aligned at the node's
// inner edge on the SAME baseline — they share a row but never overlap
// (the old bug rendered "picky_stakeholder_emu643…" with the value on top
// of the label). Tall nodes also drop the value to a second baseline.
export function sankey(opts) {
  const o = opts || {};
  const boards = (Array.isArray(o.boards) ? o.boards : []).filter((b) => b);
  const w = o.width || 760;
  const top = 30;
  const colH = Math.max(180, boards.length * 42 + 40);
  const h = colH + top * 2;
  const nodeW = 150;
  const colX = [16, (w - nodeW) / 2, w - nodeW - 16];

  const svg = svgEl('svg', { class: 'vo-sankey', width: w, height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': 'Causal flow: candidate to per-board loss to aggregate scalar' });
  if (boards.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'vo-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no per-board flow yet';
    svg.appendChild(t);
    return svg;
  }

  const total = boards.reduce((a, b) => a + Math.max(0.0001, Math.abs(b.value) || 0), 0) || 1;
  const gap = 10;
  const avail = colH - gap * Math.max(0, boards.length - 1);
  const heights = boards.map((b) => Math.max(18, (Math.max(0.0001, Math.abs(b.value) || 0) / total) * avail));
  const blockH = heights.reduce((a, b) => a + b, 0) + gap * Math.max(0, boards.length - 1);
  let y = top + Math.max(0, (colH - blockH) / 2);
  const driftNodes = boards.map((b, i) => { const node = { ...b, x: colX[1], y, h: heights[i] }; y += heights[i] + gap; return node; });
  const spanTop = driftNodes[0].y;
  const spanBot = driftNodes[driftNodes.length - 1].y + driftNodes[driftNodes.length - 1].h;
  const candNode = { x: colX[0], y: spanTop, h: spanBot - spanTop, label: (o.candidate && o.candidate.label) || 'candidate', sub: o.candidate && o.candidate.sub };
  const aggNode = { x: colX[2], y: spanTop, h: spanBot - spanTop, label: (o.aggregate && o.aggregate.label) || 'aggregate', sub: o.aggregate && o.aggregate.sub };

  const heads = [['CANDIDATE', colX[0]], ['PER-BOARD LOSS', colX[1]], ['AGGREGATE', colX[2]]];
  for (const [t, x] of heads) svg.appendChild(svgEl('text', { x: x + 2, y: 18, class: 'vo-sankey-head' }, [t]));

  let candCursor = candNode.y;
  let aggCursor = aggNode.y;
  const ribbon = (sx, sy, sh, tx, ty, th, cls) => {
    const d = `M${sx},${sy} C${(sx + tx) / 2},${sy} ${(sx + tx) / 2},${ty} ${tx},${ty} `
      + `L${tx},${ty + th} C${(sx + tx) / 2},${ty + th} ${(sx + tx) / 2},${sy + sh} ${sx},${sy + sh} Z`;
    return svgEl('path', { d, class: 'vo-ribbon ' + (cls || '') });
  };
  driftNodes.forEach((b, i) => {
    const bh = heights[i];
    const cls = b.cls || '';
    svg.appendChild(ribbon(candNode.x + nodeW, candCursor, bh, b.x, b.y, bh, cls)); candCursor += bh;
    svg.appendChild(ribbon(b.x + nodeW, b.y, bh, aggNode.x, aggCursor, bh, cls)); aggCursor += bh;
  });

  const drawNode = (n, opts2) => {
    const g = svgEl('g', { class: 'vo-sankey-node ' + ((opts2 && opts2.cls) || '') + (opts2 && opts2.clickable ? ' vo-clickable' : ''),
      tabindex: opts2 && opts2.clickable ? '0' : null, role: opts2 && opts2.clickable ? 'button' : 'group',
      'data-vo': opts2 && opts2.clickable ? 'sankey-board' : 'sankey-node', 'data-id': n.id || n.label,
      'aria-label': `${n.label}${n.value != null ? ' ' + fmt(n.value, 1) : ''}` });
    g.appendChild(svgEl('rect', { x: n.x, y: n.y, width: nodeW, height: Math.max(2, n.h), rx: 3, class: 'vo-sankey-rect' },
      [title(`${n.label}${n.value != null ? ' · ' + fmt(n.value) : (n.sub ? ' · ' + n.sub : '')}`)]));
    const labelMaxX = n.x + nodeW - 10;
    const baseY = n.y + Math.min(n.h / 2, 17);
    // The value (right-aligned) reserves the right ~46px; the label is
    // truncated to never reach it. On tall nodes the value drops below.
    const valueStr = (opts2 && opts2.value != null) ? opts2.value : (n.value != null ? fmt(n.value, 1) : (n.sub || ''));
    const tall = n.h >= 34;
    const labelChars = tall ? 18 : 13;
    g.appendChild(svgEl('text', { x: n.x + 9, y: baseY, class: 'vo-sankey-label', 'text-anchor': 'start' }, [shortLabel(n.label, labelChars)]));
    if (valueStr) {
      if (tall) {
        g.appendChild(svgEl('text', { x: n.x + 9, y: baseY + 14, class: 'vo-sankey-val', 'text-anchor': 'start' }, [String(valueStr)]));
      } else {
        g.appendChild(svgEl('text', { x: labelMaxX, y: baseY, class: 'vo-sankey-val', 'text-anchor': 'end' }, [String(valueStr)]));
      }
    }
    if (opts2 && opts2.onClick) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => opts2.onClick(n));
      g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); opts2.onClick(n); } });
    }
    svg.appendChild(g);
  };
  drawNode(candNode, { cls: 'vo-cand', value: candNode.sub });
  driftNodes.forEach((n) => drawNode(n, { cls: n.cls, clickable: !!o.onBoard, value: fmt(n.value, 1),
    onClick: () => o.onBoard && o.onBoard(n.ref != null ? n.ref : n.id) }));
  drawNode(aggNode, { cls: 'vo-agg', value: aggNode.sub });
  return svg;
}

// ---- value dot-plot with a reference line ---------------------------

export function valueDotPlot(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d);
  const w = o.width || 480;
  const rh = o.rowHeight || 22;
  const labelW = o.labelWidth || 180;
  const glyphW = 16;
  const h = Math.max(rh, items.length * rh + 8);
  const svg = svgEl('svg', { class: 'vo-valdot', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'vo-empty-label' });
    t.textContent = 'no scored entries';
    svg.appendChild(t);
    return svg;
  }
  const ref = o.reference && isNum(o.reference.value) ? o.reference.value : null;
  const vals = items.map((d) => d.value).filter(isNum);
  if (ref != null) vals.push(ref);
  let [lo, hi] = extent(vals);
  lo = Math.min(lo, 0);
  if (lo === hi) { hi += 1; }
  const x = scale([lo, hi], [labelW + 4, w - 4 - glyphW]);

  if (ref != null) {
    const rx = x(ref);
    svg.appendChild(svgEl('line', { x1: rx, x2: rx, y1: 2, y2: h - 2, class: 'vo-ref-rule' },
      [title(`${o.reference.label || 'reference'}: ${fmt(ref)}`)]));
  }
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 4;
    const g = svgEl('g', { class: 'vo-dotrow', tabindex: o.onClick ? '0' : null, 'data-vo': 'dotrow', 'data-id': d.id });
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'vo-dot-label', 'text-anchor': 'end' });
    lbl.textContent = d.label != null ? shortLabel(String(d.label), 24) : '';
    g.appendChild(lbl);
    if (isNum(d.value)) {
      const dx = x(d.value);
      g.appendChild(svgEl('line', { x1: x(lo), x2: dx, y1: cy, y2: cy, class: 'vo-dot-connector' }));
      const good = ref != null ? d.value < ref : false;
      const worse = ref != null ? d.value > ref : false;
      const cls = 'vo-dot ' + (good ? 'vo-good' : worse ? 'vo-bad' : '');
      g.appendChild(svgEl('circle', { cx: dx, cy, r: 3.4, class: cls },
        [title(`${d.label}: ${fmt(d.value)}${ref != null ? ` (vs ref ${fmt(ref)})` : ''}`)]));
      g.appendChild(outcomeGlyph(d, w - glyphW + 2, cy));
    } else {
      const t = svgEl('text', { x: x(lo) + 6, y: cy + 3, class: 'vo-dot-missing' });
      t.textContent = 'no run';
      g.appendChild(t);
    }
    if (o.onClick) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => o.onClick(d));
      g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onClick(d); } });
    }
    svg.appendChild(g);
  });
  return svg;
}

// ---- sorted bars (FIX 7's per-board comparative chart) --------------
//
// One horizontal bar per candidate on ONE board entry, sorted best-first
// (lowest loss). Each row: candidate label · bar ∝ loss · value · a
// pass/fail/timeout glyph. Click → that candidate's run for the board.
export function sortedBars(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d);
  const w = o.width || 480;
  const rh = o.rowHeight || 26;
  const labelW = o.labelWidth || 90;
  const valW = 52; const glyphW = 16;
  const h = Math.max(rh, items.length * rh + 8);
  const svg = svgEl('svg', { class: 'vo-sortbars', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'vo-empty-label' });
    t.textContent = 'no candidate ran this entry';
    svg.appendChild(t);
    return svg;
  }
  const ref = o.reference && isNum(o.reference.value) ? o.reference.value : null;
  const vals = items.map((d) => d.value).filter(isNum);
  const hi = Math.max(1e-9, ...vals, ref != null ? ref : 0);
  const x0 = labelW + 6;
  const x = scale([0, hi], [x0, w - valW - glyphW]);
  if (ref != null) {
    const rx = x(ref);
    svg.appendChild(svgEl('line', { x1: rx, x2: rx, y1: 2, y2: h - 2, class: 'vo-ref-rule' },
      [title(`${o.reference.label || 'champion'}: ${fmt(ref)}`)]));
  }
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 4;
    const g = svgEl('g', { class: 'vo-sortrow', tabindex: o.onClick ? '0' : null, 'data-vo': 'sortrow', 'data-id': d.id });
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'vo-sort-label' + (d.promoted ? ' vo-promoted' : ''), 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(d.label != null ? d.label : d.id), 12);
    g.appendChild(lbl);
    if (isNum(d.value)) {
      const bx = x(d.value);
      const cls = 'vo-sort-bar' + (d.timeout ? ' vo-timeout' : '') + ((d.pass === 0 || d.pass === false) ? ' vo-fail' : '');
      g.appendChild(svgEl('rect', { x: x0, y: cy - 6, width: Math.max(1, bx - x0), height: 12, rx: 2, class: cls },
        [title(`${d.id}: ${fmt(d.value)}${d.timeout ? ' · timed out' : ''}`)]));
      const vt = svgEl('text', { x: bx + 5, y: cy + 3, class: 'vo-sort-val', 'text-anchor': 'start' });
      vt.textContent = fmt(d.value, 1);
      g.appendChild(vt);
      g.appendChild(outcomeGlyph(d, w - glyphW + 2, cy));
    } else {
      const t = svgEl('text', { x: x0 + 4, y: cy + 3, class: 'vo-dot-missing' });
      t.textContent = 'no run';
      g.appendChild(t);
    }
    if (o.onClick) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => o.onClick(d));
      g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onClick(d); } });
    }
    svg.appendChild(g);
  });
  return svg;
}

function outcomeGlyph(d, x, cy) {
  if (d && d.ran === false) return svgEl('circle', { cx: x, cy, r: 2.4, class: 'vo-glyph-none' }, [title('no run')]);
  if (d && d.timeout) return svgEl('text', { x, y: cy + 3, class: 'vo-glyph-timeout', 'text-anchor': 'middle' }, [title('budget exceeded (timeout)'), '⏱']);
  if (d && (d.pass === true || d.pass === 1)) return svgEl('circle', { cx: x, cy, r: 2.6, class: 'vo-glyph-pass' }, [title('passed')]);
  if (d && (d.pass === false || d.pass === 0)) {
    const g = svgEl('g', null, [title('failed')]);
    g.appendChild(svgEl('line', { x1: x - 2.6, y1: cy - 2.6, x2: x + 2.6, y2: cy + 2.6, class: 'vo-glyph-fail' }));
    g.appendChild(svgEl('line', { x1: x - 2.6, y1: cy + 2.6, x2: x + 2.6, y2: cy - 2.6, class: 'vo-glyph-fail' }));
    return g;
  }
  return svgEl('circle', { cx: x, cy, r: 2.4, class: 'vo-glyph-none' }, [title('no predicate')]);
}

// ---- paired per-board slopegraph (non-colliding) -------------------

export function pairedSlopegraph(opts) {
  const o = opts || {};
  const series = (Array.isArray(o.series) ? o.series : []).filter((s) => s && (isNum(s.a) || isNum(s.b)));
  const w = o.width || 560;
  const h = o.height || 320;
  const padTop = 28; const padBottom = 18;
  const colGap = o.labelGap || 160;
  const leftX = colGap;
  const rightX = w - colGap;
  const goodDown = (o.goodDirection || 'down') === 'down';

  const svg = svgEl('svg', { class: 'vo-pslope', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (series.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'vo-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no paired board duels yet';
    svg.appendChild(t);
    return svg;
  }
  const allVals = [];
  for (const s of series) { if (isNum(s.a)) allVals.push(s.a); if (isNum(s.b)) allVals.push(s.b); }
  const [lo, hi] = extent(allVals);
  const y = scale([lo, hi], [h - padBottom, padTop]);

  const hL = svgEl('text', { x: leftX, y: 15, class: 'vo-slope-col', 'text-anchor': 'end' });
  hL.textContent = (o.left && o.left.title) || 'champion';
  const hR = svgEl('text', { x: rightX, y: 15, class: 'vo-slope-col', 'text-anchor': 'start' });
  hR.textContent = (o.right && o.right.title) || 'challenger';
  svg.appendChild(hL); svg.appendChild(hR);
  svg.appendChild(svgEl('line', { x1: leftX, x2: leftX, y1: y(hi), y2: y(lo), class: 'vo-slope-axis' }));
  svg.appendChild(svgEl('line', { x1: rightX, x2: rightX, y1: y(hi), y2: y(lo), class: 'vo-slope-axis' }));

  const minGap = 15;
  const leftY = series.map((s) => (isNum(s.a) ? y(s.a) : (isNum(s.b) ? y(s.b) : (padTop + h - padBottom) / 2)));
  const rightY = series.map((s) => (isNum(s.b) ? y(s.b) : (isNum(s.a) ? y(s.a) : (padTop + h - padBottom) / 2)));
  const leftNode = jitterColumn(leftY, 3.2);
  const rightNode = jitterColumn(rightY, 3.2);
  const leftLabels = decollide(series.map((s) => ({ v: isNum(s.a) ? s.a : s.b })), y, minGap, padTop, h - padBottom);
  const rightLabels = decollide(series.map((s) => ({ v: isNum(s.b) ? s.b : s.a })), y, minGap, padTop, h - padBottom);

  series.forEach((s, i) => {
    const ay = isNum(s.a) ? leftNode[i] : null;
    const by = isNum(s.b) ? rightNode[i] : null;
    const verdict = s.verdict || (isNum(s.a) && isNum(s.b)
      ? (s.b === s.a ? 'flat' : (goodDown ? (s.b < s.a ? 'improved' : 'regressed') : (s.b > s.a ? 'improved' : 'regressed')))
      : 'flat');
    const dirCls = verdict === 'improved' ? 'vo-good' : verdict === 'regressed' ? 'vo-bad' : 'vo-flat';
    const g = svgEl('g', { class: 'vo-pslope-series', tabindex: o.onClick ? '0' : null, 'data-vo': 'pslope-series', 'data-id': s.id || s.label });

    if (ay != null && by != null) {
      const line = svgEl('line', { x1: leftX, y1: ay, x2: rightX, y2: by, class: 'vo-pslope-line ' + dirCls });
      line.appendChild(title(`${s.label}: ${fmt(s.a)} → ${fmt(s.b)} (${fmtSigned(s.b - s.a)}; ${verdict})`));
      g.appendChild(line);
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.6, class: 'vo-pslope-node ' + dirCls }));
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.6, class: 'vo-pslope-node ' + dirCls }));
    } else if (ay != null) {
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.6, class: 'vo-pslope-node vo-flat' }, [title(`${s.label}: champion only ${fmt(s.a)}`)]));
    } else if (by != null) {
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.6, class: 'vo-pslope-node vo-flat' }, [title(`${s.label}: challenger only ${fmt(s.b)}`)]));
    }

    const ll = leftLabels[i];
    if (isNum(s.a)) {
      if (Math.abs(ll - y(s.a)) > 1.5) g.appendChild(svgEl('line', { x1: leftX - 4, y1: ll, x2: leftX, y2: y(s.a), class: 'vo-leader' }));
      const tx = svgEl('text', { x: leftX - 8, y: ll + 3, class: 'vo-pslope-label', 'text-anchor': 'end' });
      tx.textContent = `${shortLabel(s.label, 16)}  ${fmt(s.a, 1)}`;
      g.appendChild(tx);
    }
    const rl = rightLabels[i];
    if (isNum(s.b)) {
      if (Math.abs(rl - y(s.b)) > 1.5) g.appendChild(svgEl('line', { x1: rightX, y1: y(s.b), x2: rightX + 4, y2: rl, class: 'vo-leader' }));
      const tx = svgEl('text', { x: rightX + 8, y: rl + 3, class: 'vo-pslope-label', 'text-anchor': 'start' });
      tx.textContent = `${fmt(s.b, 1)}  ${shortLabel(s.label, 16)}`;
      g.appendChild(tx);
    }
    if (o.onClick) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => o.onClick(s));
      g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onClick(s); } });
    }
    svg.appendChild(g);
  });
  return svg;
}

// ---- heatmap (FIX 4: theme-aware ramp) ------------------------------
//
// rows × cols coloured by value. The ramp is `[loHex, hiHex]` derived from
// the ACTIVE theme tokens by the caller (ui.heatRamp) — NOT a fixed
// orange/brown ramp.
export function heatmap(opts) {
  const o = opts || {};
  const rows = Array.isArray(o.rows) ? o.rows : [];
  const cols = Array.isArray(o.cols) ? o.cols : [];
  const cw = o.cellW || 30;
  const ch = o.cellH || 18;
  const labelW = o.labelWidth || 150;
  const headH = o.headHeight || 46;
  const w = labelW + cols.length * cw + 8;
  const h = headH + rows.length * ch + 8;
  const svg = svgEl('svg', { class: 'vo-heatmap', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (rows.length === 0 || cols.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'vo-empty-label' });
    t.textContent = 'no profiles yet';
    svg.appendChild(t);
    return svg;
  }
  const vals = [];
  for (const r of rows) for (const c of cols) { const v = o.value(r.id, c.id); if (isNum(v)) vals.push(v); }
  const [lo, hi] = extent(vals);
  const span = hi - lo || 1;
  const ramp = Array.isArray(o.ramp) && o.ramp.length === 2 ? o.ramp : ['#dfe9e6', '#b14a4a'];

  cols.forEach((c, j) => {
    const cx = labelW + j * cw + cw / 2;
    const t = svgEl('text', { x: cx, y: headH - 6, class: 'vo-hm-col',
      transform: `rotate(-45 ${cx} ${headH - 6})`, 'text-anchor': 'start' });
    t.textContent = shortLabel(c.label, 12);
    svg.appendChild(t);
  });
  rows.forEach((r, i) => {
    const ry = headH + i * ch;
    const lbl = svgEl('text', { x: labelW - 6, y: ry + ch - 5, class: 'vo-hm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(r.label, 18);
    svg.appendChild(lbl);
    cols.forEach((c, j) => {
      const v = o.value(r.id, c.id);
      const cx = labelW + j * cw;
      const t = isNum(v) ? (v - lo) / span : null;
      const fill = t == null ? 'var(--vo-cell-empty)' : lerpHex(ramp[0], ramp[1], t);
      const cell = svgEl('rect', { x: cx + 1, y: ry + 1, width: cw - 2, height: ch - 2, rx: 2, class: 'vo-hm-cell',
        fill, 'data-vo': 'hm-cell', 'data-row': r.id, 'data-col': c.id },
      [title(`${r.label} × ${c.label}: ${isNum(v) ? fmt(v) : '—'}`)]);
      if (o.onClick) { cell.style.cursor = 'pointer'; cell.addEventListener('click', () => o.onClick(r.id, c.id)); }
      svg.appendChild(cell);
    });
  });
  return svg;
}

// ---- mutation-site × generation matrix (FIX 2's matrix half) --------

export function mutationMatrix(opts) {
  const o = opts || {};
  const sites = Array.isArray(o.sites) ? o.sites : [];
  const gens = Array.isArray(o.gens) ? o.gens : [];
  const cw = o.cellW || 56;
  const ch = o.cellH || 26;
  const labelW = o.labelWidth || 230;
  const headH = o.headHeight || 28;
  const w = labelW + gens.length * cw + 8;
  const h = headH + sites.length * ch + 8;
  const svg = svgEl('svg', { class: 'vo-mutmatrix', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img', 'aria-label': 'Mutation sites by generation' });
  if (sites.length === 0 || gens.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'vo-empty-label' });
    t.textContent = 'no mutation surface recorded';
    svg.appendChild(t);
    return svg;
  }
  gens.forEach((g, j) => {
    const cx = labelW + j * cw + cw / 2;
    const t = svgEl('text', { x: cx, y: headH - 8, class: 'vo-mm-col' + (g.promoted ? ' vo-promoted' : ''), 'text-anchor': 'middle' });
    t.textContent = shortLabel(g.label || g.id, 8);
    svg.appendChild(t);
  });
  sites.forEach((s, i) => {
    const ry = headH + i * ch;
    const lg = svgEl('g', null, [title(`${s.label}${s.sub ? ' · ' + s.sub : ''}`)]);
    const lbl = svgEl('text', { x: labelW - 8, y: ry + ch / 2 - 3, class: 'vo-mm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(s.label, 30);
    lg.appendChild(lbl);
    if (s.sub) {
      const sub = svgEl('text', { x: labelW - 8, y: ry + ch / 2 + 9, class: 'vo-mm-row-sub', 'text-anchor': 'end' });
      sub.textContent = shortLabel(s.sub, 30);
      lg.appendChild(sub);
    }
    svg.appendChild(lg);
    gens.forEach((g, j) => {
      const cx = labelW + j * cw;
      const on = !!o.patched(s.id, g.id);
      const sel = o.selected && o.selected.site === s.id && o.selected.gen === g.id;
      const cell = svgEl('rect', { x: cx + 2, y: ry + 2, width: cw - 4, height: ch - 4, rx: 3,
        class: 'vo-mm-cell ' + (on ? 'vo-mm-on' : 'vo-mm-off') + (sel ? ' vo-sel' : ''),
        'data-vo': 'mut-cell', 'data-gen': g.id, 'data-site': s.id },
      [title(`${g.id} ${on ? 'patched' : 'did not touch'} ${s.label}`)]);
      if (on && o.onCell) { cell.style.cursor = 'pointer'; cell.addEventListener('click', () => o.onCell(g.id, s.id)); }
      svg.appendChild(cell);
      if (on) svg.appendChild(svgEl('circle', { cx: cx + cw / 2, cy: ry + ch / 2, r: 3.6, class: 'vo-mm-dot' }));
    });
  });
  return svg;
}

// Linear hex interpolation for the heatmap ramp.
function lerpHex(a, b, t) {
  const x = Math.max(0, Math.min(1, t));
  const pa = hexToRgb(a); const pb = hexToRgb(b);
  return `rgb(${Math.round(pa[0] + (pb[0] - pa[0]) * x)},${Math.round(pa[1] + (pb[1] - pa[1]) * x)},${Math.round(pa[2] + (pb[2] - pa[2]) * x)})`;
}
function hexToRgb(hex) {
  const hh = String(hex).replace('#', '');
  return [parseInt(hh.slice(0, 2), 16), parseInt(hh.slice(2, 4), 16), parseInt(hh.slice(4, 6), 16)];
}
