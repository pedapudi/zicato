// variants/L/svg.js — dependency-free SVG data-viz primitives (Atlas III).
//
// Self-contained for Variant L. Ported from Variant E's Tufte toolkit
// (high data-ink, small multiples, non-colliding bumps/slopegraphs) and
// extended with the convergence-II marks: a fit-to-width Tufte SANKEY whose
// per-board label and loss value never overlap, a theme-aware HEATMAP whose
// ramp is read from the active color theme's tokens at draw time, a mutation
// SITE × GENERATION matrix, and a line-diff used by the side-by-side
// mutation diff. Mark classes are `vl-*`, styled — scoped under the variant
// root — by css/variants/L/atlas.css.
//
// Conventions: lower drift/loss is BETTER; every primitive is total
// (empty/NaN input yields a quiet empty mark, never a throw); colours come
// from CSS custom properties (`--l-*`) so the palette is themed in one place.

import { svgEl, el } from '../../core/dom.js';

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
  const max = isNum(n) ? n : 12;
  const str = s == null ? '' : String(s);
  return str.length > max ? str.slice(0, max - 1) + '…' : str;
}

// ---- sparkline ------------------------------------------------------
export function sparkline(opts) {
  const o = opts || {};
  const w = o.width || 120;
  const h = o.height || 28;
  const pad = 2;
  const raw = Array.isArray(o.values) ? o.values : [];
  const fin = finiteValues(raw);
  const svg = svgEl('svg', {
    class: 'vl-spark', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'none', role: 'img',
  });
  if (fin.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h / 2, x2: w - pad, y2: h / 2, class: 'vl-spark-empty' }));
    return svg;
  }
  const [lo, hi] = extent(fin);
  const x = scale([0, Math.max(1, raw.length - 1)], [pad, w - pad]);
  const y = scale([lo, hi], [h - pad, pad]);
  if (o.band) svg.appendChild(svgEl('rect', { x: pad, y: pad, width: w - 2 * pad, height: h - 2 * pad, class: 'vl-spark-band' }));
  if (isNum(o.baseline)) {
    const by = y(o.baseline);
    svg.appendChild(svgEl('line', { x1: pad, x2: w - pad, y1: by, y2: by, class: 'vl-spark-baseline' }));
  }
  let d = ''; let penDown = false;
  raw.forEach((v, i) => {
    if (!isNum(v)) { penDown = false; return; }
    d += `${penDown ? 'L' : 'M'}${x(i).toFixed(2)},${y(v).toFixed(2)} `;
    penDown = true;
  });
  svg.appendChild(svgEl('path', { d: d.trim(), class: 'vl-spark-line', fill: 'none' }));
  if (o.endDot !== false) {
    let lastI = -1;
    for (let i = raw.length - 1; i >= 0; i--) { if (isNum(raw[i])) { lastI = i; break; } }
    if (lastI >= 0) {
      const dir = o.goodDirection || 'down';
      let firstI = -1;
      for (let i = 0; i < raw.length; i++) { if (isNum(raw[i])) { firstI = i; break; } }
      const improved = firstI >= 0 && lastI !== firstI
        ? (dir === 'down' ? raw[lastI] < raw[firstI] : raw[lastI] > raw[firstI])
        : null;
      const cls = improved === null ? 'vl-spark-dot' : improved ? 'vl-spark-dot vl-good' : 'vl-spark-dot vl-bad';
      svg.appendChild(svgEl('circle', { cx: x(lastI), cy: y(raw[lastI]), r: 2.2, class: cls }, [title(fmt(raw[lastI]))]));
    }
  }
  return svg;
}

// ---- non-collision helpers (shared by slopegraphs + bumps) ----------
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
    idxs.forEach((idx, k) => { out[idx] = ys[idx] + (k - mid) * step; });
  }
  return out;
}

// ---- bumps chart (lineage as ranked lanes; de-collided + clickable) -
export function bumps(opts) {
  const o = opts || {};
  const nodes = (Array.isArray(o.nodes) ? o.nodes : []).filter((n) => n);
  const w = o.width || 640;
  const h = o.height || 180;
  const padX = 44; const spineY = h * 0.40; const challY = h * 0.80;
  const svg = svgEl('svg', { class: 'vl-bumps', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (nodes.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'vl-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no generations yet';
    svg.appendChild(t);
    return svg;
  }
  const maxX = Math.max(1, ...nodes.map((n) => n.x || 0));
  const X = scale([0, maxX], [padX, w - padX]);
  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: spineY, y2: spineY, class: 'vl-lane-guide vl-spine-guide' }));
  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: challY, y2: challY, class: 'vl-lane-guide' }));
  const lblS = svgEl('text', { x: 6, y: spineY - 8, class: 'vl-lane-label' }); lblS.textContent = 'champion';
  const lblC = svgEl('text', { x: 6, y: challY - 8, class: 'vl-lane-label' }); lblC.textContent = 'challenger';
  svg.appendChild(lblS); svg.appendChild(lblC);

  const laneY = (n) => (n.promoted ? spineY : challY);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const promoted = nodes.filter((n) => n.promoted).sort((a, b) => (a.x || 0) - (b.x || 0));
  for (let i = 1; i < promoted.length; i++) {
    svg.appendChild(svgEl('line', { x1: X(promoted[i - 1].x), y1: spineY, x2: X(promoted[i].x), y2: spineY, class: 'vl-spine-line' }));
  }
  // de-collide coincident challenger x's so two siblings at the same depth
  // do not draw on top of one another (F's collision bug).
  const challengers = nodes.filter((n) => !n.promoted);
  const baseX = new Map(challengers.map((n) => [n, X(n.x)]));
  const jittered = jitterColumn(challengers.map((n) => baseX.get(n)), 18);
  const cxFor = new Map();
  nodes.forEach((n) => { if (n.promoted) cxFor.set(n, X(n.x)); });
  challengers.forEach((n, i) => cxFor.set(n, jittered[i]));

  for (const n of nodes) {
    if (n.promoted) continue;
    const p = n.parent ? byId.get(n.parent) : null;
    const px = p ? cxFor.get(p) : X((n.x || 1) - 1);
    const py = p ? laneY(p) : spineY;
    const nx = cxFor.get(n);
    const path = `M${px},${py} C${(px + nx) / 2},${py} ${(px + nx) / 2},${challY} ${nx},${challY}`;
    svg.appendChild(svgEl('path', { d: path, class: 'vl-branch', fill: 'none' }));
  }
  for (const n of nodes) {
    const cy = laneY(n);
    const cx = cxFor.get(n);
    const cls = 'vl-bump-node ' + (n.promoted ? 'vl-promoted' : 'vl-rejected');
    const c = svgEl('circle', { cx, cy, r: n.promoted ? 4.5 : 3.5, class: cls, 'data-vl': 'bump-node', 'data-gen': n.id },
      [title(`${n.id}${isNum(n.scalar) ? ' · ' + fmt(n.scalar) : ''} · ${n.promoted ? 'promoted' : 'rejected'}`)]);
    if (o.onClick) { c.style.cursor = 'pointer'; c.addEventListener('click', () => o.onClick(n)); }
    svg.appendChild(c);
    const t = svgEl('text', { x: cx, y: cy + 16, class: 'vl-bump-label', 'text-anchor': 'middle' });
    t.textContent = shortLabel(n.id);
    svg.appendChild(t);
  }
  return svg;
}

// ---- Tufte sankey (fit-to-width; label / value never overlap) -------
//
// candidate → per-board loss → aggregate scalar. The fix carried in: each
// per-board node's LABEL sits at the node's left, and its loss VALUE is
// RIGHT-ALIGNED to the node's right edge on the SAME baseline — they share
// a row but occupy disjoint x-ranges, so a long entry id and its value can
// never render on top of each other (the "picky_stakeholder_emu643…" bug).
export function sankey(opts) {
  const o = opts || {};
  const boards = (Array.isArray(o.boards) ? o.boards : []).filter((b) => b);
  const w = o.width || 720;
  const top = 30;
  const colH = Math.max(180, boards.length * 40 + 40);
  const h = colH + top * 2;
  const nodeW = 168;
  const colX = [16, (w - nodeW) / 2, w - nodeW - 16];

  const svg = svgEl('svg', {
    class: 'vl-sankey', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': 'Causal flow: candidate to per-board loss to aggregate scalar',
  });
  if (boards.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'vl-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no per-board flow yet';
    svg.appendChild(t);
    return svg;
  }
  const total = boards.reduce((a, b) => a + Math.max(0.0001, Math.abs(b.value) || 0), 0) || 1;
  const gap = 12;
  const avail = colH - gap * Math.max(0, boards.length - 1);
  const heights = boards.map((b) => Math.max(20, (Math.max(0.0001, Math.abs(b.value) || 0) / total) * avail));
  const blockH = heights.reduce((a, b) => a + b, 0) + gap * Math.max(0, boards.length - 1);
  let y = top + Math.max(0, (colH - blockH) / 2);
  const driftNodes = boards.map((b, i) => { const node = { ...b, x: colX[1], y, h: heights[i] }; y += heights[i] + gap; return node; });
  const spanTop = driftNodes.length ? driftNodes[0].y : top;
  const spanBot = driftNodes.length ? driftNodes[driftNodes.length - 1].y + driftNodes[driftNodes.length - 1].h : top + colH;
  const candNode = { x: colX[0], y: spanTop, h: spanBot - spanTop, label: (o.candidate && o.candidate.label) || 'candidate', sub: o.candidate && o.candidate.sub };
  const aggNode = { x: colX[2], y: spanTop, h: spanBot - spanTop, label: (o.aggregate && o.aggregate.label) || 'aggregate', sub: o.aggregate && o.aggregate.sub };

  const heads = [['CANDIDATE', colX[0]], ['PER-BOARD LOSS', colX[1]], ['AGGREGATE', colX[2]]];
  for (const [t, x] of heads) svg.appendChild(svgEl('text', { x: x + 2, y: 18, class: 'vl-sankey-head' }, [t]));

  let candCursor = candNode.y; let aggCursor = aggNode.y;
  const ribbon = (sx, sy, sh, tx, ty, th, cls) => {
    const d = `M${sx},${sy} C${(sx + tx) / 2},${sy} ${(sx + tx) / 2},${ty} ${tx},${ty} `
      + `L${tx},${ty + th} C${(sx + tx) / 2},${ty + th} ${(sx + tx) / 2},${sy + sh} ${sx},${sy + sh} Z`;
    return svgEl('path', { d, class: 'vl-ribbon ' + (cls || '') });
  };
  driftNodes.forEach((b, i) => {
    const bh = heights[i]; const cls = b.cls || '';
    svg.appendChild(ribbon(candNode.x + nodeW, candCursor, bh, b.x, b.y, bh, cls));
    candCursor += bh;
    svg.appendChild(ribbon(b.x + nodeW, b.y, bh, aggNode.x, aggCursor, bh, cls));
    aggCursor += bh;
  });

  const drawNode = (n, opts2) => {
    const g = svgEl('g', {
      class: 'vl-sankey-node ' + ((opts2 && opts2.cls) || '') + (opts2 && opts2.clickable ? ' vl-clickable' : ''),
      tabindex: opts2 && opts2.clickable ? '0' : null, role: opts2 && opts2.clickable ? 'button' : 'group',
      'data-vl': opts2 && opts2.clickable ? 'sankey-board' : 'sankey-node', 'data-id': n.id || n.label,
      'aria-label': `${n.label}${n.value != null ? ' ' + fmt(n.value, 1) : ''}`,
    });
    g.appendChild(svgEl('rect', { x: n.x, y: n.y, width: nodeW, height: Math.max(2, n.h), rx: 3, class: 'vl-sankey-rect' },
      [title(`${n.label}${isNum(n.value) ? ' · ' + fmt(n.value, 1) + ' loss' : (n.sub ? ' · ' + n.sub : '')}`)]));
    const ty = n.y + Math.min(n.h / 2, 16);
    // LABEL: left-anchored at the node's left, capped so it cannot reach the
    // value column. VALUE: right-anchored to the node's right edge on the
    // SAME baseline — disjoint x-ranges, so they never overlap.
    g.appendChild(svgEl('text', { x: n.x + 8, y: ty, class: 'vl-sankey-label', 'text-anchor': 'start' }, [shortLabel(n.label, 16)]));
    if (isNum(n.value)) {
      g.appendChild(svgEl('text', { x: n.x + nodeW - 8, y: ty, class: 'vl-sankey-value', 'text-anchor': 'end' }, [fmt(n.value, 1)]));
    }
    if (n.sub && !isNum(n.value)) {
      g.appendChild(svgEl('text', { x: n.x + 8, y: ty + 13, class: 'vl-sankey-sub', 'text-anchor': 'start' }, [shortLabel(n.sub, 20)]));
    }
    if (opts2 && opts2.onClick) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => opts2.onClick(n));
      g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); opts2.onClick(n); } });
    }
    svg.appendChild(g);
  };
  drawNode(candNode, { cls: 'vl-cand' });
  driftNodes.forEach((n) => drawNode(n, { cls: n.cls, clickable: !!o.onBoard, onClick: () => o.onBoard && o.onBoard(n.ref != null ? n.ref : n.id) }));
  drawNode(aggNode, { cls: 'vl-agg' });
  return svg;
}

// ---- heatmap (theme-aware ramp; proportional) -----------------------
//
// opts: { rows, cols, value(rowId,colId), ramp:[lo,hi], cellW, cellH,
//         labelWidth, headHeight, onClick }. The `ramp` MUST be derived from
// the active color theme's tokens by the caller (ui.themeRamp) — there is no
// fixed orange/brown here.
export function heatmap(opts) {
  const o = opts || {};
  const rows = Array.isArray(o.rows) ? o.rows : [];
  const cols = Array.isArray(o.cols) ? o.cols : [];
  const cw = o.cellW || 44;
  const ch = o.cellH || 20;
  const labelW = o.labelWidth || 170;
  const headH = o.headHeight || 46;
  const w = labelW + cols.length * cw + 8;
  const h = headH + rows.length * ch + 8;
  const svg = svgEl('svg', { class: 'vl-heatmap', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (rows.length === 0 || cols.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'vl-empty-label' });
    t.textContent = 'no profiles yet';
    svg.appendChild(t);
    return svg;
  }
  const vals = [];
  for (const r of rows) for (const c of cols) { const v = o.value(r.id, c.id); if (isNum(v)) vals.push(v); }
  const [lo, hi] = extent(vals);
  const span = hi - lo || 1;
  const ramp = (Array.isArray(o.ramp) && o.ramp.length === 2) ? o.ramp : ['#0a3b47', '#e8736a'];

  cols.forEach((c, j) => {
    const cx = labelW + j * cw + cw / 2;
    const t = svgEl('text', { x: cx, y: headH - 6, class: 'vl-hm-col', transform: `rotate(-45 ${cx} ${headH - 6})`, 'text-anchor': 'start' });
    t.textContent = shortLabel(c.label, 12);
    svg.appendChild(t);
  });
  rows.forEach((r, i) => {
    const ry = headH + i * ch;
    const lbl = svgEl('text', { x: labelW - 6, y: ry + ch - 5, class: 'vl-hm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(r.label, 18);
    svg.appendChild(lbl);
    cols.forEach((c, j) => {
      const v = o.value(r.id, c.id);
      const cx = labelW + j * cw;
      const t = isNum(v) ? (v - lo) / span : null;
      const fill = t == null ? 'var(--l-cell-empty)' : lerpHex(ramp[0], ramp[1], t);
      const cell = svgEl('rect', { x: cx + 1, y: ry + 1, width: cw - 2, height: ch - 2, rx: 2, class: 'vl-hm-cell', fill, 'data-vl': 'hm-cell', 'data-entry': r.id, 'data-gen': c.id },
        [title(`${r.label} × ${c.label}: ${isNum(v) ? fmt(v) : '—'}`)]);
      if (o.onClick) { cell.style.cursor = 'pointer'; cell.addEventListener('click', () => o.onClick(r.id, c.id)); }
      svg.appendChild(cell);
    });
  });
  return svg;
}

// ---- mutation site × generation matrix ------------------------------
export function mutationMatrix(opts) {
  const o = opts || {};
  const sites = Array.isArray(o.sites) ? o.sites : [];
  const gens = Array.isArray(o.gens) ? o.gens : [];
  const cw = o.cellW || 56;
  const ch = o.cellH || 26;
  const labelW = o.labelWidth || 240;
  const headH = o.headHeight || 28;
  const w = labelW + gens.length * cw + 8;
  const h = headH + sites.length * ch + 8;
  const svg = svgEl('svg', { class: 'vl-mutmatrix', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img', 'aria-label': 'Mutation sites by generation' });
  if (sites.length === 0 || gens.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'vl-empty-label' });
    t.textContent = 'no mutation surface recorded';
    svg.appendChild(t);
    return svg;
  }
  gens.forEach((g, j) => {
    const cx = labelW + j * cw + cw / 2;
    const t = svgEl('text', { x: cx, y: headH - 8, class: 'vl-mm-col' + (g.promoted ? ' vl-promoted' : ''), 'text-anchor': 'middle' });
    t.textContent = shortLabel(g.label || g.id, 8);
    svg.appendChild(t);
  });
  const selSite = o.selectedSite; const selGen = o.selectedGen;
  sites.forEach((s, i) => {
    const ry = headH + i * ch;
    const lg = svgEl('g', null, [title(`${s.label}${s.sub ? ' · ' + s.sub : ''}`)]);
    const lbl = svgEl('text', { x: labelW - 8, y: ry + ch / 2, class: 'vl-mm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(s.label, 32);
    lg.appendChild(lbl);
    if (s.sub) {
      lbl.setAttribute('y', ry + ch / 2 - 3);
      const sub = svgEl('text', { x: labelW - 8, y: ry + ch / 2 + 9, class: 'vl-mm-row-sub', 'text-anchor': 'end' });
      sub.textContent = shortLabel(s.sub, 32);
      lg.appendChild(sub);
    }
    svg.appendChild(lg);
    gens.forEach((g, j) => {
      const cx = labelW + j * cw;
      const on = !!o.patched(s.id, g.id);
      const isSel = selSite === s.id && selGen === g.id;
      const cell = svgEl('rect', {
        x: cx + 2, y: ry + 2, width: cw - 4, height: ch - 4, rx: 3,
        class: 'vl-mm-cell ' + (on ? 'vl-mm-on' : 'vl-mm-off') + (isSel ? ' vl-mm-sel' : ''),
        'data-vl': 'mut-cell', 'data-gen': g.id, 'data-site': s.id,
      }, [title(`${g.id} ${on ? 'patched' : 'did not touch'} ${s.label}`)]);
      if (on && o.onCell) { cell.style.cursor = 'pointer'; cell.addEventListener('click', () => o.onCell(g.id, s.id)); }
      svg.appendChild(cell);
      if (on) svg.appendChild(svgEl('circle', { cx: cx + cw / 2, cy: ry + ch / 2, r: 3.4, class: 'vl-mm-dot' }));
    });
  });
  return svg;
}

// ---- value dot-plot with a reference line ---------------------------
export function valueDotPlot(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d);
  const w = o.width || 460;
  const rh = o.rowHeight || 22;
  const labelW = o.labelWidth || 180;
  const glyphW = 16;
  const h = Math.max(rh, items.length * rh + 8);
  const svg = svgEl('svg', { class: 'vl-valdot', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'vl-empty-label' });
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
    svg.appendChild(svgEl('line', { x1: rx, x2: rx, y1: 2, y2: h - 2, class: 'vl-ref-rule' }, [title(`${o.reference.label || 'reference'}: ${fmt(ref)}`)]));
  }
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 4;
    const g = svgEl('g', { class: 'vl-dotrow', tabindex: o.onClick ? '0' : null, 'data-vl': 'valdot-row', 'data-id': d.id != null ? d.id : '' });
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'vl-dot-label', 'text-anchor': 'end' });
    lbl.textContent = d.label != null ? shortLabel(String(d.label), 22) : '';
    g.appendChild(lbl);
    if (isNum(d.value)) {
      const dx = x(d.value);
      g.appendChild(svgEl('line', { x1: x(lo), x2: dx, y1: cy, y2: cy, class: 'vl-dot-connector' }));
      const good = ref != null ? d.value < ref : false;
      const worse = ref != null ? d.value > ref : false;
      const cls = 'vl-dot ' + (good ? 'vl-good' : worse ? 'vl-bad' : '');
      g.appendChild(svgEl('circle', { cx: dx, cy, r: 3.2, class: cls }, [title(`${d.label}: ${fmt(d.value)}${ref != null ? ` (vs ${fmt(ref)})` : ''}`)]));
      g.appendChild(outcomeGlyph(d, w - glyphW + 2, cy));
    } else {
      const t = svgEl('text', { x: x(lo) + 6, y: cy + 3, class: 'vl-dot-missing' });
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

// ---- horizontal value bars (per-judge losses, scalar components) ----
export function valueBars(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d && isNum(d.value));
  const w = o.width || 360;
  const rh = o.rowHeight || 18;
  const labelW = o.labelWidth || 150;
  const h = Math.max(rh, items.length * rh + 6);
  const svg = svgEl('svg', { class: 'vl-vbars', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 14, class: 'vl-empty-label' });
    t.textContent = 'no values';
    svg.appendChild(t);
    return svg;
  }
  const hi = Math.max(1e-9, ...items.map((d) => Math.abs(d.value)));
  const x0 = labelW + 4;
  const x = scale([0, hi], [x0, w - 40]);
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 3;
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'vl-dot-label', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(d.label), 20);
    svg.appendChild(lbl);
    const bx = x(Math.abs(d.value));
    svg.appendChild(svgEl('rect', { x: x0, y: cy - 4, width: Math.max(1, bx - x0), height: 8, rx: 1, class: 'vl-vbar' + (d.cls ? ' ' + d.cls : '') }, [title(`${d.label}: ${fmt(d.value)}`)]));
    const vt = svgEl('text', { x: bx + 4, y: cy + 3, class: 'vl-vbar-val' });
    vt.textContent = fmt(d.value, d.digits == null ? 1 : d.digits);
    svg.appendChild(vt);
  });
  return svg;
}

// ---- sparkbar (micro loss bars for the board trellis) ---------------
export function sparkbar(opts) {
  const o = opts || {};
  const bars = (Array.isArray(o.bars) ? o.bars : []).filter((b) => b);
  const w = o.width || 120;
  const h = o.height || 30;
  const pad = 2; const footH = 2;
  const svg = svgEl('svg', { class: 'vl-sparkbar', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', role: 'img' });
  if (bars.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h - footH, x2: w - pad, y2: h - footH, class: 'vl-spark-empty' }));
    return svg;
  }
  const dom = o.domain && o.domain.length === 2 && isNum(o.domain[0]) && isNum(o.domain[1]) ? o.domain : extent(bars.map((b) => b.value));
  const [lo, hi] = dom[0] === dom[1] ? [dom[0], dom[0] + 1] : dom;
  const base = Math.min(lo, 0);
  const yTop = scale([base, hi], [h - footH, pad + 2]);
  const n = bars.length;
  const slot = (w - 2 * pad) / n;
  const bw = Math.max(1.5, Math.min(slot * 0.7, 10));
  const y0 = yTop(base);
  bars.forEach((b, i) => {
    const cx = pad + slot * (i + 0.5);
    if (isNum(b.value)) {
      const yv = yTop(b.value);
      const cls = 'vl-sparkbar-bar' + (b.timeout ? ' vl-timeout' : '') + (b.fail ? ' vl-fail' : '');
      svg.appendChild(svgEl('rect', { x: cx - bw / 2, y: Math.min(yv, y0), width: bw, height: Math.max(1, Math.abs(y0 - yv)), class: cls },
        [title(`${b.label}: ${fmt(b.value)}${b.timeout ? ' · timed out' : ''}${b.fail ? ' · failed' : ''}`)]));
    } else {
      svg.appendChild(svgEl('line', { x1: cx, y1: y0 - 1, x2: cx, y2: y0 - 4, class: 'vl-sparkbar-missing' }, [title(`${b.label}: no run`)]));
    }
  });
  svg.appendChild(svgEl('line', { x1: pad, y1: y0, x2: w - pad, y2: y0, class: 'vl-sparkbar-foot' }));
  return svg;
}

export function genDots(opts) {
  const o = opts || {};
  const cells = Array.isArray(o.cells) ? o.cells : [];
  const w = o.width || 200;
  const h = o.height || 14;
  const pad = 2;
  const svg = svgEl('svg', { class: 'vl-genrow', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', role: 'img' });
  const n = Math.max(1, cells.length);
  const slot = (w - 2 * pad) / n;
  cells.forEach((c, i) => { const cx = pad + slot * (i + 0.5); svg.appendChild(outcomeGlyph(c, cx, h / 2)); });
  return svg;
}

function outcomeGlyph(d, x, cy) {
  if (d && d.ran === false) return svgEl('circle', { cx: x, cy, r: 2.2, class: 'vl-glyph-none' }, [title('no run')]);
  if (d.timeout) return svgEl('text', { x, y: cy + 3, class: 'vl-glyph-timeout', 'text-anchor': 'middle' }, [title('budget exceeded (timeout)'), '⏱']);
  if (d.pass === true || d.pass === 1) return svgEl('circle', { cx: x, cy, r: 2.4, class: 'vl-glyph-pass' }, [title('passed')]);
  if (d.pass === false || d.pass === 0) {
    const g = svgEl('g', null, [title('failed')]);
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy - 2.4, x2: x + 2.4, y2: cy + 2.4, class: 'vl-glyph-fail' }));
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy + 2.4, x2: x + 2.4, y2: cy - 2.4, class: 'vl-glyph-fail' }));
    return g;
  }
  return svgEl('circle', { cx: x, cy, r: 2.2, class: 'vl-glyph-none' }, [title('no predicate')]);
}

// ---- paired per-board slopegraph (non-colliding) --------------------
export function pairedSlopegraph(opts) {
  const o = opts || {};
  const series = (Array.isArray(o.series) ? o.series : []).filter((s) => s && (isNum(s.a) || isNum(s.b)));
  const w = o.width || 520;
  const h = o.height || 300;
  const padTop = 28; const padBottom = 18;
  const colGap = o.labelGap || 150;
  const leftX = colGap; const rightX = w - colGap;
  const goodDown = (o.goodDirection || 'down') === 'down';
  const svg = svgEl('svg', { class: 'vl-pslope', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (series.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'vl-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no paired board duels yet';
    svg.appendChild(t);
    return svg;
  }
  const allVals = [];
  for (const s of series) { if (isNum(s.a)) allVals.push(s.a); if (isNum(s.b)) allVals.push(s.b); }
  const [lo, hi] = extent(allVals);
  const y = scale([lo, hi], [h - padBottom, padTop]);
  const hL = svgEl('text', { x: leftX, y: 15, class: 'vl-slope-col', 'text-anchor': 'end' });
  hL.textContent = (o.left && o.left.title) || 'champion';
  const hR = svgEl('text', { x: rightX, y: 15, class: 'vl-slope-col', 'text-anchor': 'start' });
  hR.textContent = (o.right && o.right.title) || 'challenger';
  svg.appendChild(hL); svg.appendChild(hR);
  svg.appendChild(svgEl('line', { x1: leftX, x2: leftX, y1: y(hi), y2: y(lo), class: 'vl-slope-axis' }));
  svg.appendChild(svgEl('line', { x1: rightX, x2: rightX, y1: y(hi), y2: y(lo), class: 'vl-slope-axis' }));
  const minGap = 14;
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
    const dirCls = verdict === 'improved' ? 'vl-good' : verdict === 'regressed' ? 'vl-bad' : 'vl-flat';
    const g = svgEl('g', { class: 'vl-pslope-series' });
    if (ay != null && by != null) {
      const line = svgEl('line', { x1: leftX, y1: ay, x2: rightX, y2: by, class: 'vl-pslope-line ' + dirCls });
      line.appendChild(title(`${s.label}: ${fmt(s.a)} → ${fmt(s.b)} (${fmtSigned(s.b - s.a)}; ${verdict})`));
      g.appendChild(line);
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'vl-pslope-node ' + dirCls }));
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'vl-pslope-node ' + dirCls }));
    } else if (ay != null) {
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'vl-pslope-node vl-flat' }, [title(`${s.label}: champion only ${fmt(s.a)}`)]));
    } else if (by != null) {
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'vl-pslope-node vl-flat' }, [title(`${s.label}: challenger only ${fmt(s.b)}`)]));
    }
    const ll = leftLabels[i];
    if (isNum(s.a)) {
      if (Math.abs(ll - y(s.a)) > 1.5) g.appendChild(svgEl('line', { x1: leftX - 4, y1: ll, x2: leftX, y2: y(s.a), class: 'vl-leader' }));
      const tx = svgEl('text', { x: leftX - 8, y: ll + 3, class: 'vl-pslope-label', 'text-anchor': 'end' });
      tx.textContent = `${shortLabel(s.label, 14)}  ${fmt(s.a, 1)}`;
      g.appendChild(tx);
    }
    const rl = rightLabels[i];
    if (isNum(s.b)) {
      if (Math.abs(rl - y(s.b)) > 1.5) g.appendChild(svgEl('line', { x1: rightX, y1: y(s.b), x2: rightX + 4, y2: rl, class: 'vl-leader' }));
      const tx = svgEl('text', { x: rightX + 8, y: rl + 3, class: 'vl-pslope-label', 'text-anchor': 'start' });
      tx.textContent = `${fmt(s.b, 1)}  ${shortLabel(s.label, 14)}`;
      g.appendChild(tx);
    }
    if (o.onClick) { g.style.cursor = 'pointer'; g.addEventListener('click', () => o.onClick(s)); }
    svg.appendChild(g);
  });
  return svg;
}

// ---- comparative bar plot (per-board cross-candidate view) ----------
//
// A sorted horizontal bar per candidate for ONE board entry: bar length =
// drift loss (shorter = better), with a pass/fail/timeout glyph and click →
// that candidate's run. Used by the NEW board view.
//
// opts: { width, rowHeight, labelWidth, items:[{label,id,value,pass,timeout,
//         best}], onClick }
export function comparativeBars(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d);
  const w = o.width || 480;
  const rh = o.rowHeight || 26;
  const labelW = o.labelWidth || 90;
  const glyphW = 18;
  const h = Math.max(rh, items.length * rh + 8);
  const svg = svgEl('svg', { class: 'vl-cmpbars', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'vl-empty-label' });
    t.textContent = 'no candidates ran this entry';
    svg.appendChild(t);
    return svg;
  }
  const vals = items.map((d) => d.value).filter(isNum);
  const hi = vals.length ? Math.max(...vals) : 1;
  const x0 = labelW + 6;
  const x = scale([0, hi || 1], [x0, w - glyphW - 30]);
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 4;
    const g = svgEl('g', { class: 'vl-cmprow', tabindex: o.onClick ? '0' : null, 'data-vl': 'cmp-row', 'data-id': d.id != null ? d.id : '' });
    const lbl = svgEl('text', { x: labelW, y: cy + 4, class: 'vl-dot-label' + (d.best ? ' vl-best' : ''), 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(d.label), 12);
    g.appendChild(lbl);
    if (isNum(d.value)) {
      const bx = x(d.value);
      const cls = 'vl-cmpbar' + (d.best ? ' vl-good' : '') + (d.timeout ? ' vl-timeout' : '') + (d.pass === 0 ? ' vl-fail' : '');
      g.appendChild(svgEl('rect', { x: x0, y: cy - 7, width: Math.max(1, bx - x0), height: 14, rx: 2, class: cls }, [title(`${d.label}: ${fmt(d.value, 1)}`)]));
      const vt = svgEl('text', { x: bx + 4, y: cy + 4, class: 'vl-cmpbar-val' });
      vt.textContent = fmt(d.value, 1);
      g.appendChild(vt);
      g.appendChild(outcomeGlyph(d, w - glyphW + 4, cy));
    } else {
      const t = svgEl('text', { x: x0, y: cy + 4, class: 'vl-dot-missing' });
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

// ---- illustrative tournament topologies (ported from E) -------------
export function bracketMini(opts) {
  const o = opts || {};
  const w = o.width || 360;
  const challengers = Array.isArray(o.challengers) ? o.challengers : [];
  const seats = [{ id: o.champion, champion: true }, ...challengers.map((c) => ({ id: c.id }))];
  const rowH = 26;
  const h = o.height || Math.max(80, seats.length * rowH + 20);
  const svg = svgEl('svg', { class: 'vl-bracket', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (seats.length === 0) return svg;
  const seatW = 96; const col1 = 8; const col2 = w - seatW - 8; const midX = (col1 + seatW + col2) / 2;
  const winnerY = h / 2;
  seats.forEach((s, i) => {
    const y = 12 + i * rowH;
    const won = s.id === o.winner;
    svg.appendChild(svgEl('rect', { x: col1, y, width: seatW, height: 18, rx: 2, class: 'vl-bracket-seat' + (won ? ' vl-win' : '') + (s.champion ? ' vl-champ' : '') }));
    const t = svgEl('text', { x: col1 + 6, y: y + 13, class: 'vl-bracket-label' });
    t.textContent = shortLabel(String(s.id), 12);
    svg.appendChild(t);
    const cy = y + 9;
    svg.appendChild(svgEl('path', { d: `M${col1 + seatW},${cy} H${midX} V${winnerY + 9} H${col2}`, class: 'vl-bracket-edge' + (won ? ' vl-win' : ''), fill: 'none' }));
  });
  const fy = winnerY;
  svg.appendChild(svgEl('rect', { x: col2, y: fy, width: seatW, height: 18, rx: 2, class: 'vl-bracket-seat vl-win vl-champ' }));
  const wt = svgEl('text', { x: col2 + 6, y: fy + 13, class: 'vl-bracket-label' });
  wt.textContent = o.winner ? `${shortLabel(String(o.winner), 9)} ✦` : 'tbd';
  svg.appendChild(wt);
  return svg;
}

export function roundRobinMatrix(opts) {
  const o = opts || {};
  const ids = Array.isArray(o.ids) ? o.ids : [];
  const cw = o.cell || 30;
  const labelW = o.label || 44;
  const headH = 18;
  const w = labelW + ids.length * cw + 4;
  const h = headH + ids.length * cw + 4;
  const svg = svgEl('svg', { class: 'vl-rrmatrix', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (ids.length === 0) return svg;
  const loss = o.lossById || {};
  ids.forEach((cid, j) => {
    const t = svgEl('text', { x: labelW + j * cw + cw / 2, y: headH - 5, class: 'vl-rr-head', 'text-anchor': 'middle' });
    t.textContent = shortLabel(String(cid), 5);
    svg.appendChild(t);
  });
  ids.forEach((rid, i) => {
    const ry = headH + i * cw;
    const lbl = svgEl('text', { x: labelW - 5, y: ry + cw / 2 + 3, class: 'vl-rr-head', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(rid), 6);
    svg.appendChild(lbl);
    ids.forEach((cid, j) => {
      const cx = labelW + j * cw;
      if (i === j) { svg.appendChild(svgEl('rect', { x: cx + 1, y: ry + 1, width: cw - 2, height: cw - 2, rx: 1, class: 'vl-rr-diag' })); return; }
      const rl = loss[rid]; const cl = loss[cid];
      let cls = 'vl-rr-cell vl-flat';
      if (isNum(rl) && isNum(cl)) cls = 'vl-rr-cell ' + (rl < cl ? 'vl-good' : rl > cl ? 'vl-bad' : 'vl-flat');
      svg.appendChild(svgEl('rect', { x: cx + 1, y: ry + 1, width: cw - 2, height: cw - 2, rx: 1, class: cls },
        [title(`${rid} vs ${cid}: ${isNum(rl) ? fmt(rl) : '—'} vs ${isNum(cl) ? fmt(cl) : '—'}`)]));
    });
  });
  return svg;
}

export function raceLanes(opts) {
  const o = opts || {};
  const runners = (Array.isArray(o.runners) ? o.runners : []).filter((r) => r);
  const w = o.width || 420;
  const lh = o.laneHeight || 22;
  const labelW = o.labelWidth || 60;
  const h = Math.max(lh, runners.length * lh + 22);
  const svg = svgEl('svg', { class: 'vl-race', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (runners.length === 0) return svg;
  const vals = runners.map((r) => r.loss).filter(isNum);
  let [lo, hi] = extent(vals);
  lo = Math.min(lo, 0);
  if (lo === hi) hi += 1;
  const x = scale([lo, hi], [labelW + 6, w - 10]);
  const best = vals.length ? Math.min(...vals) : null;
  if (best != null) svg.appendChild(svgEl('line', { x1: x(best), x2: x(best), y1: 14, y2: h - 4, class: 'vl-race-finish' }, [title(`leader: ${fmt(best)}`)]));
  if (isNum(o.cut)) svg.appendChild(svgEl('line', { x1: x(o.cut), x2: x(o.cut), y1: 14, y2: h - 4, class: 'vl-race-cut' }, [title(`elimination cut: ${fmt(o.cut)}`)]));
  const head = svgEl('text', { x: labelW + 6, y: 10, class: 'vl-race-head' });
  head.textContent = 'loss → (left = ahead)';
  svg.appendChild(head);
  runners.forEach((r, i) => {
    const cy = 20 + i * lh + lh / 2;
    svg.appendChild(svgEl('line', { x1: labelW + 6, x2: w - 10, y1: cy, y2: cy, class: 'vl-race-lane' }));
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'vl-dot-label', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(r.id), 8);
    svg.appendChild(lbl);
    if (isNum(r.loss)) {
      const cls = 'vl-race-dot' + (r.eliminated ? ' vl-bad' : ' vl-good');
      svg.appendChild(svgEl('circle', { cx: x(r.loss), cy, r: 3.4, class: cls }, [title(`${r.id}: ${fmt(r.loss)}${r.eliminated ? ' · eliminated' : ' · survives'}`)]));
    }
  });
  return svg;
}

// ---- line diff (side-by-side mutation diff) -------------------------
//
// A line-level LCS diff of two STRINGS → two aligned column arrays. Returns
// { left:[{n,text,cls}], right:[{n,text,cls}] } where cls ∈ same / del / add
// and a placeholder row (n=null, text='') keeps the columns row-aligned.
// Pure + total (null/empty inputs yield empty columns). Exported so the diff
// is unit-testable without the DOM.
export function lineDiff(baseStr, newStr) {
  const a = String(baseStr == null ? '' : baseStr).replace(/\r\n/g, '\n').split('\n');
  const b = String(newStr == null ? '' : newStr).replace(/\r\n/g, '\n').split('\n');
  // LCS table.
  const m = a.length; const n = b.length;
  const dp = Array.from({ length: m + 1 }, () => new Int32Array(n + 1));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const left = []; const right = [];
  let i = 0; let j = 0; let ln = 1; let rn = 1;
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      left.push({ n: ln++, text: a[i], cls: 'same' });
      right.push({ n: rn++, text: b[j], cls: 'same' });
      i++; j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      left.push({ n: ln++, text: a[i], cls: 'del' });
      right.push({ n: null, text: '', cls: 'empty' });
      i++;
    } else {
      left.push({ n: null, text: '', cls: 'empty' });
      right.push({ n: rn++, text: b[j], cls: 'add' });
      j++;
    }
  }
  while (i < m) { left.push({ n: ln++, text: a[i], cls: 'del' }); right.push({ n: null, text: '', cls: 'empty' }); i++; }
  while (j < n) { left.push({ n: null, text: '', cls: 'empty' }); right.push({ n: rn++, text: b[j], cls: 'add' }); j++; }
  return { left, right };
}

// Build the side-by-side diff DOM from two STRINGS. The baseline column is
// the champion's baseline content (a STRING — never the `.baseline` object,
// which produced "[object Object]"); the new column is the challenger's
// `.new_content`. Two aligned columns, line-diffed.
export function sideBySideDiff(opts) {
  const o = opts || {};
  const { left, right } = lineDiff(o.baseline, o.next);
  const col = (rows, side, headLabel) => {
    const c = el('div', { class: 'vl-diff-col vl-diff-' + side });
    c.appendChild(el('div', { class: 'vl-diff-colhead', text: headLabel }));
    const body = el('div', { class: 'vl-diff-lines' });
    for (const r of rows) {
      body.appendChild(el('div', { class: 'vl-diff-line vl-diff-' + r.cls }, [
        el('span', { class: 'vl-diff-gutter', text: r.n == null ? '' : String(r.n) }),
        el('code', { class: 'vl-diff-code', text: r.text }),
      ]));
    }
    c.appendChild(body);
    return c;
  };
  return el('div', { class: 'vl-sbs', 'data-vl': 'sbs-diff' }, [
    col(left, 'base', o.leftLabel || 'champion baseline'),
    col(right, 'next', o.rightLabel || 'challenger new'),
  ]);
}

// ---- small-multiple wrapper -----------------------------------------
export function smallMultiple(caption, mark, sub) {
  return el('figure', { class: 'vl-sm' }, [
    el('figcaption', { class: 'vl-sm-cap' }, [
      el('span', { class: 'vl-sm-title', text: caption == null ? '' : String(caption) }),
      sub ? el('span', { class: 'vl-sm-sub', text: String(sub) }) : null,
    ].filter(Boolean)),
    mark,
  ]);
}

function lerpHex(a, b, t) {
  const x = Math.max(0, Math.min(1, t));
  const pa = parseColor(a); const pb = parseColor(b);
  return `rgb(${Math.round(pa[0] + (pb[0] - pa[0]) * x)},${Math.round(pa[1] + (pb[1] - pa[1]) * x)},${Math.round(pa[2] + (pb[2] - pa[2]) * x)})`;
}
function parseColor(c) {
  const s = String(c || '').trim();
  const rgb = /^rgba?\(([^)]+)\)$/.exec(s);
  if (rgb) { const p = rgb[1].split(',').map((x) => parseFloat(x)); return [p[0] || 0, p[1] || 0, p[2] || 0]; }
  const h = s.replace('#', '');
  if (h.length === 3) return [parseInt(h[0] + h[0], 16), parseInt(h[1] + h[1], 16), parseInt(h[2] + h[2], 16)];
  return [parseInt(h.slice(0, 2), 16) || 0, parseInt(h.slice(2, 4), 16) || 0, parseInt(h.slice(4, 6), 16) || 0];
}
