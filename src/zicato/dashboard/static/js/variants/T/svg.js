// variants/T/svg.js — dependency-free SVG data-viz primitives (Console).
//
// Self-contained for Variant N ("Console II"). Mark CSS classes are `dn-*` and
//     themed in one place, swapped by the [data-n-theme] attribute.

import { svgEl, el } from '../../core/dom.js';
import { attachHovercard } from './hovercard.js';

export const NS = 'http://www.w3.org/2000/svg';

// Wire a mark with the styled, theme-aware HOVERCARD instead of a native,
// off-brand <title> tooltip (positioned card on hover/focus; keyboard- and
// reduced-motion-aware; a transient overlay, NOT part of the digest-gated
function hov(node, tip) { attachHovercard(node, tip); return node; }

// Wire a node as a pointer/keyboard activatable control (click + Enter/Space).
// Returns the node. No-op when `fn` is falsy.
function clickable(node, fn) {
  if (!fn) return node;
  node.style.cursor = 'pointer';
  node.addEventListener('click', () => fn());
  node.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); fn(); } });
  return node;
}

// ---- numeric helpers ------------------------------------------------

export function isNum(v) { return typeof v === 'number' && isFinite(v); }

export function finiteValues(arr) {
  return (Array.isArray(arr) ? arr : []).filter(isNum);
}

export function extent(values) {
  const v = finiteValues(values);
  if (v.length === 0) return [0, 1];
  let lo = v[0]; let hi = v[0];
  for (const x of v) { if (x < lo) lo = x; if (x > hi) hi = x; }
  if (lo === hi) { lo -= 0.5; hi += 0.5; }
  return [lo, hi];
}

// A linear scale from [d0,d1] to [r0,r1].
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

function shortLabel(s, n) {
  const max = isNum(n) ? n : 12;
  const str = s == null ? '' : String(s);
  return str.length > max ? str.slice(0, max - 1) + '…' : str;
}

// a live racing lane's progress label: "k/N boards" when the rung's board total
// is known, else "k running" when only the in-flight count is known.
function laneProgressText(lane) {
  if (!lane) return '';
  if (isNum(lane.total) && lane.total > 0) return `${lane.done || 0}/${lane.total} boards`;
  if (lane.inflight) return `${lane.inflight} running`;
  if (lane.done) return `${lane.done} boards`;
  return 'racing';
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
    // fit-to-width: width:100% so the trend sparkline scales to its pane.
    class: 'dn-spark', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', role: 'img',
  });
  if (fin.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h / 2, x2: w - pad, y2: h / 2, class: 'dn-spark-empty' }));
    return svg;
  }
  const [lo, hi] = extent(fin);
  const x = scale([0, Math.max(1, raw.length - 1)], [pad, w - pad]);
  const y = scale([lo, hi], [h - pad, pad]);
  if (o.band) {
    svg.appendChild(svgEl('rect', { x: pad, y: pad, width: w - 2 * pad, height: h - 2 * pad, class: 'dn-spark-band' }));
  }
  if (isNum(o.baseline)) {
    const by = y(o.baseline);
    svg.appendChild(svgEl('line', { x1: pad, x2: w - pad, y1: by, y2: by, class: 'dn-spark-baseline' }));
  }
  let d = '';
  let penDown = false;
  raw.forEach((v, i) => {
    if (!isNum(v)) { penDown = false; return; }
    d += `${penDown ? 'L' : 'M'}${x(i).toFixed(2)},${y(v).toFixed(2)} `;
    penDown = true;
  });
  svg.appendChild(svgEl('path', { d: d.trim(), class: 'dn-spark-line', fill: 'none' }));
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
      const cls = improved === null ? 'dn-spark-dot'
        : improved ? 'dn-spark-dot dn-good' : 'dn-spark-dot dn-bad';
      svg.appendChild(hov(svgEl('circle', { cx: x(lastI), cy: y(raw[lastI]), r: 2.2, class: cls }), fmt(raw[lastI])));
    }
  }
  return svg;
}

// ---- bumps chart (lineage as ranked lanes) --------------------------
// Champion lineage on its own spine lane; rejected challengers branch off.
// opts: { width, height, nodes:[{id, x, promoted, scalar, parent}], onClick }
export function bumps(opts) {
  const o = opts || {};
  const nodes = (Array.isArray(o.nodes) ? o.nodes : []).filter((n) => n);
  const w = o.width || 640;
  const h = o.height || 170;
  const padX = 44; const spineY = h * 0.40; const challY = h * 0.80;
  const svg = svgEl('svg', { class: 'dn-bumps', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (nodes.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no generations yet';
    svg.appendChild(t);
    return svg;
  }
  const maxX = Math.max(1, ...nodes.map((n) => n.x || 0));
  const X = scale([0, maxX], [padX, w - padX]);

  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: spineY, y2: spineY, class: 'dn-lane-guide dn-spine-guide' }));
  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: challY, y2: challY, class: 'dn-lane-guide' }));
  const lblS = svgEl('text', { x: 6, y: spineY - 8, class: 'dn-lane-label' }); lblS.textContent = 'champion';
  const lblC = svgEl('text', { x: 6, y: challY - 8, class: 'dn-lane-label' }); lblC.textContent = 'challenger';
  svg.appendChild(lblS); svg.appendChild(lblC);

  const laneY = (n) => (n.promoted ? spineY : challY);
  const byId = new Map(nodes.map((n) => [n.id, n]));

  // De-collide the screen-x of nodes WITHIN each lane (the F bug). Two
  // challengers branching off the same parent share a generation index and
  // would land on the same x; push them apart along the lane.
  const cx = new Map();
  for (const lanePromoted of [true, false]) {
    const lane = nodes.filter((n) => !!n.promoted === lanePromoted)
      .map((n) => ({ id: n.id, x: X(n.x || 0) }))
      .sort((a, b) => a.x - b.x);
    const minGap = 34;
    for (let i = 1; i < lane.length; i++) {
      if (lane[i].x - lane[i - 1].x < minGap) lane[i].x = lane[i - 1].x + minGap;
    }
    // clamp the trailing overflow back inside the frame
    const right = w - padX;
    if (lane.length && lane[lane.length - 1].x > right) {
      lane[lane.length - 1].x = right;
      for (let i = lane.length - 2; i >= 0; i--) {
        if (lane[i + 1].x - lane[i].x < minGap) lane[i].x = lane[i + 1].x - minGap;
      }
    }
    for (const n of lane) cx.set(n.id, n.x);
  }
  const nodeX = (n) => (cx.has(n.id) ? cx.get(n.id) : X(n.x || 0));

  const promoted = nodes.filter((n) => n.promoted).sort((a, b) => nodeX(a) - nodeX(b));
  for (let i = 1; i < promoted.length; i++) {
    svg.appendChild(svgEl('line', { x1: nodeX(promoted[i - 1]), y1: spineY, x2: nodeX(promoted[i]), y2: spineY, class: 'dn-spine-line' }));
  }
  for (const n of nodes) {
    if (n.promoted) continue;
    const p = n.parent ? byId.get(n.parent) : null;
    const px = p ? nodeX(p) : nodeX(n) - 40;
    const py = p ? laneY(p) : spineY;
    const nx = nodeX(n);
    const path = `M${px},${py} C${(px + nx) / 2},${py} ${(px + nx) / 2},${challY} ${nx},${challY}`;
    svg.appendChild(svgEl('path', { d: path, class: 'dn-branch', fill: 'none' }));
  }
  for (const n of nodes) {
    const cy = laneY(n);
    const px = nodeX(n);
    const cls = 'dn-bump-node ' + (n.promoted ? 'dn-promoted' : 'dn-rejected');
    const c = hov(svgEl('circle', { cx: px, cy, r: n.promoted ? 4.5 : 3.5, class: cls, tabindex: o.onClick ? '0' : null }),
      `${n.id}${isNum(n.scalar) ? ' · ' + fmt(n.scalar) : ''} · ${n.promoted ? 'promoted' : 'rejected'}`);
    clickable(c, o.onClick && (() => o.onClick(n)));
    svg.appendChild(c);
    const t = svgEl('text', { x: px, y: cy + 16, class: 'dn-bump-label', 'text-anchor': 'middle' });
    t.textContent = shortLabel(n.id);
    svg.appendChild(t);
  }
  return svg;
}

// One-dimensional collision resolver — exported for the test suite.
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

// Spread coincident node positions a hair apart — exported for the tests.
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

// ---- theme-aware heatmap (rows × cols coloured by value) ------------
export function heatmap(opts) {
  const o = opts || {};
  const rows = Array.isArray(o.rows) ? o.rows : [];
  const cols = Array.isArray(o.cols) ? o.cols : [];
  const cw = o.cellW || 24;
  const ch = o.cellH || 15;
  const labelW = o.labelWidth || 128;
  const headH = o.headHeight || 44;
  const w = labelW + cols.length * cw + 6;
  const h = headH + rows.length * ch + 6;
  // FIT-TO-WIDTH: width:100% + a viewBox so the matrix scales DOWN to its pane
  // (no fixed pixel width that overflows, no horizontal-scroll wrapper). The
  // intrinsic cell size (cw/ch) is density-scaled by the caller.
  const svg = svgEl('svg', { class: 'dn-heatmap', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img' });
  if (rows.length === 0 || cols.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'dn-empty-label' });
    t.textContent = 'no profiles yet';
    svg.appendChild(t);
    return svg;
  }
  const vals = [];
  for (const r of rows) for (const c of cols) { const v = o.value(r.id, c.id); if (isNum(v)) vals.push(v); }
  const [lo, hi] = extent(vals);
  const span = hi - lo || 1;

  cols.forEach((c, j) => {
    const cx = labelW + j * cw + cw / 2;
    const t = svgEl('text', { x: cx, y: headH - 6, class: 'dn-hm-col', transform: `rotate(-45 ${cx} ${headH - 6})`, 'text-anchor': 'start' });
    t.textContent = shortLabel(c.label);
    svg.appendChild(t);
  });
  rows.forEach((r, i) => {
    const ry = headH + i * ch;
    const lbl = svgEl('text', { x: labelW - 6, y: ry + ch - 4, class: 'dn-hm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(r.label);
    svg.appendChild(lbl);
    cols.forEach((c, j) => {
      const v = o.value(r.id, c.id);
      const cx = labelW + j * cw;
      const t = isNum(v) ? (v - lo) / span : null;
      // Theme-aware, HIGHER-CONTRAST cell scale. Two contrast axes, both driven
      // by the same per-theme CSS tokens (so it stays correct across all 16
      // themes, light and dark):
      // from an EMPTY cell (the flat --v2-cell-empty token at full opacity).
      const cls = t == null ? 'dn-hm-cell dn-hm-empty' : 'dn-hm-cell';
      const tc = t == null ? null : Math.max(0, Math.min(1, t));
      const e = tc == null ? null : Math.pow(tc, 0.8);
      const mixPct = e == null ? null : (8 + 92 * e).toFixed(2);
      const op = e == null ? null : (0.30 + 0.70 * e).toFixed(3);
      const attrs = { x: cx + 1, y: ry + 1, width: cw - 2, height: ch - 2, rx: 1.5, class: cls };
      if (op != null) attrs['fill-opacity'] = op;
      const cell = hov(svgEl('rect', attrs), `${r.label} × ${c.label}: ${isNum(v) ? fmt(v) : '—'}`);
      if (mixPct != null) {
        // theme-correct cool→hot gradient via CSS custom props (no hardcoded hex)
        cell.style.setProperty('fill', `color-mix(in srgb, var(--v2-hm-hot) ${mixPct}%, var(--v2-hm-cool))`);
        cell.setAttribute('data-hm-mix', mixPct);
      }
      clickable(cell, o.onClick && (() => o.onClick(r.id, c.id)));
      svg.appendChild(cell);
    });
  });
  return svg;
}

// ---- value dot-plot with a reference line (clickable; onClick → full item) --
export function valueDotPlot(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d);
  const w = o.width || 460;
  const rh = o.rowHeight || 19;
  const labelW = o.labelWidth || 170;
  const glyphW = 16;
  const h = Math.max(rh, items.length * rh + 8);
  // FIT-TO-WIDTH: width:100% + viewBox so the dot-plot scales to its pane (and
  // the narrower compare-split column) without overflowing.
  const svg = svgEl('svg', { class: 'dn-valdot', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMin meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'dn-empty-label' });
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
    svg.appendChild(hov(svgEl('line', { x1: rx, x2: rx, y1: 2, y2: h - 2, class: 'dn-ref-rule' }),
      `${(o.reference.label || 'reference')}: ${fmt(ref)}`));
  }
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 4;
    const hasCtx = d.context != null && String(d.context) !== '';
    const g = svgEl('g', { class: 'dn-dotrow', tabindex: o.onClick ? '0' : null });
    // With a context tag the name lifts onto its own baseline and the dim tag
    // sits just beneath it (two stacked right-anchored lines inside the gutter).
    const nameY = hasCtx ? cy - 2 : cy + 3;
    const lbl = svgEl('text', { x: labelW, y: nameY, class: 'dn-dot-label', 'text-anchor': 'end' });
    lbl.textContent = d.label != null ? shortLabel(String(d.label), 22) : '';
    g.appendChild(lbl);
    if (hasCtx) {
      // theme-aware (uses the faint ink token), no extra stylesheet rule.
      const ctx = svgEl('text', {
        x: labelW, y: cy + 9, class: 'dn-dot-ctx', 'text-anchor': 'end',
        fill: 'var(--v2-ink-faint)', 'font-size': '9px', 'font-family': 'var(--v2-mono)',
      });
      ctx.textContent = shortLabel(String(d.context), 22);
      g.appendChild(ctx);
    }
    if (isNum(d.value)) {
      const dx = x(d.value);
      g.appendChild(svgEl('line', { x1: x(lo), x2: dx, y1: cy, y2: cy, class: 'dn-dot-connector' }));
      const good = ref != null ? d.value < ref : false;
      const worse = ref != null ? d.value > ref : false;
      const cls = 'dn-dot ' + (good ? 'dn-good' : worse ? 'dn-bad' : '');
      g.appendChild(hov(svgEl('circle', { cx: dx, cy, r: 3.2, class: cls }),
        `${d.label}: ${fmt(d.value)}${ref != null ? ` (vs champ ${fmt(ref)})` : ''}`));
      g.appendChild(outcomeGlyph(d, w - glyphW + 2, cy));
    } else {
      const t = svgEl('text', { x: x(lo) + 6, y: cy + 3, class: 'dn-dot-missing' });
      t.textContent = 'no run';
      g.appendChild(t);
    }
    clickable(g, o.onClick && (() => o.onClick(d)));
    svg.appendChild(g);
  });
  return svg;
}

// ---- sparkbar (micro loss bars + a verdict marker) ------------------

export function sparkbar(opts) {
  const o = opts || {};
  const bars = (Array.isArray(o.bars) ? o.bars : []).filter((b) => b);
  const w = o.width || 120;
  const h = o.height || 30;
  const pad = 2;
  const footH = 2;
  // FIT-TO-WIDTH inside its trellis cell: width:100% + viewBox (height is the
  // density-scaled intrinsic dimension).
  const svg = svgEl('svg', { class: 'dn-sparkbar', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', role: 'img' });
  if (bars.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h - footH, x2: w - pad, y2: h - footH, class: 'dn-spark-empty' }));
    return svg;
  }
  const dom = o.domain && o.domain.length === 2 && isNum(o.domain[0]) && isNum(o.domain[1])
    ? o.domain : extent(bars.map((b) => b.value));
  const [lo, hi] = dom[0] === dom[1] ? [dom[0], dom[0] + 1] : dom;
  const base = Math.min(lo, 0);
  const yTop = scale([base, hi], [h - footH, pad + 5]);
  const n = bars.length;
  const slot = (w - 2 * pad) / n;
  const bw = Math.max(1.5, Math.min(slot * 0.7, 10));
  const y0 = yTop(base);
  bars.forEach((b, i) => {
    const cx = pad + slot * (i + 0.5);
    if (isNum(b.value)) {
      const y = yTop(b.value);
      const cls = 'dn-sparkbar-bar' + (b.timeout ? ' dn-timeout' : '') + (b.fail ? ' dn-fail' : '');
      svg.appendChild(hov(svgEl('rect', { x: cx - bw / 2, y: Math.min(y, y0), width: bw, height: Math.max(1, Math.abs(y0 - y)), class: cls }),
        `${b.label}: ${fmt(b.value)}${b.timeout ? ' · timed out' : ''}${b.fail ? ' · failed' : ''}`));
    } else {
      svg.appendChild(hov(svgEl('line', { x1: cx, y1: y0 - 1, x2: cx, y2: y0 - 4, class: 'dn-sparkbar-missing' }), `${b.label}: no run`));
    }
  });
  svg.appendChild(svgEl('line', { x1: pad, y1: y0, x2: w - pad, y2: y0, class: 'dn-sparkbar-foot' }));
  if (o.verdict === 'promoted' || o.verdict === 'rejected') {
    const good = o.verdict === 'promoted';
    const gx = w - pad - 3; const gy = pad + 4; const r = 3.2;
    const tri = good ? `${gx},${gy - r} ${gx - r},${gy + r} ${gx + r},${gy + r}` : `${gx},${gy + r} ${gx - r},${gy - r} ${gx + r},${gy - r}`;
    svg.appendChild(hov(svgEl('polygon', { points: tri, class: 'dn-verdict-glyph ' + (good ? 'dn-good' : 'dn-bad') }), o.verdict));
  }
  return svg;
}

// A row of pass/fail/timeout glyphs.
export function genDots(opts) {
  const o = opts || {};
  const cells = Array.isArray(o.cells) ? o.cells : [];
  const w = o.width || 200;
  const h = o.height || 14;
  const pad = 2;
  const svg = svgEl('svg', { class: 'dn-genrow', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', role: 'img' });
  const n = Math.max(1, cells.length);
  const slot = (w - 2 * pad) / n;
  cells.forEach((c, i) => { svg.appendChild(outcomeGlyph(c, pad + slot * (i + 0.5), h / 2)); });
  return svg;
}

function outcomeGlyph(d, x, cy) {
  if (d && d.ran === false) return hov(svgEl('circle', { cx: x, cy, r: 2.2, class: 'dn-glyph-none' }), 'no run');
  if (d.timeout) return hov(svgEl('text', { x, y: cy + 3, class: 'dn-glyph-timeout', 'text-anchor': 'middle' }, ['⏱']), 'budget exceeded (timeout)');
  if (d.pass === true || d.pass === 1) return hov(svgEl('circle', { cx: x, cy, r: 2.4, class: 'dn-glyph-pass' }), 'passed');
  if (d.pass === false || d.pass === 0) {
    const g = svgEl('g', null);
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy - 2.4, x2: x + 2.4, y2: cy + 2.4, class: 'dn-glyph-fail' }));
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy + 2.4, x2: x + 2.4, y2: cy - 2.4, class: 'dn-glyph-fail' }));
    return hov(g, 'failed');
  }
  return hov(svgEl('circle', { cx: x, cy, r: 2.2, class: 'dn-glyph-none' }), 'no predicate');
}

// ---- horizontal value bars (per-judge losses) ----------------------

export function valueBars(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d && isNum(d.value));
  const w = o.width || 360;
  const rh = o.rowHeight || 18;
  const labelW = o.labelWidth || 150;
  const h = Math.max(rh, items.length * rh + 6);
  const svg = svgEl('svg', { class: 'dn-vbars', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMin meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 14, class: 'dn-empty-label' });
    t.textContent = 'no values';
    svg.appendChild(t);
    return svg;
  }
  const hi = Math.max(1e-9, ...items.map((d) => Math.abs(d.value)));
  const x0 = labelW + 4;
  const x = scale([0, hi], [x0, w - 36]);
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 3;
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'dn-dot-label', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(d.label), 20);
    svg.appendChild(lbl);
    const bx = x(Math.abs(d.value));
    svg.appendChild(hov(svgEl('rect', { x: x0, y: cy - 4, width: Math.max(1, bx - x0), height: 8, rx: 1, class: 'dn-vbar' }), `${d.label}: ${fmt(d.value)}`));
    const vt = svgEl('text', { x: bx + 4, y: cy + 3, class: 'dn-vbar-val' });
    vt.textContent = fmt(d.value, 1);
    svg.appendChild(vt);
  });
  return svg;
}

// ---- paired per-board slopegraph (NON-COLLIDING) --------------------

export function pairedSlopegraph(opts) {
  const o = opts || {};
  const series = (Array.isArray(o.series) ? o.series : []).filter((s) => s && (isNum(s.a) || isNum(s.b)));
  const w = o.width || 520;
  const h = o.height || 300;
  const padTop = 28; const padBottom = 18;
  const colGap = o.labelGap || 150;
  const leftX = colGap;
  const rightX = w - colGap;
  const goodDown = (o.goodDirection || 'down') === 'down';

  const svg = svgEl('svg', { class: 'dn-pslope', width: '100%', height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMin meet', role: 'img' });
  if (series.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no paired board duels yet';
    svg.appendChild(t);
    return svg;
  }
  const allVals = [];
  for (const s of series) { if (isNum(s.a)) allVals.push(s.a); if (isNum(s.b)) allVals.push(s.b); }
  const [lo, hi] = extent(allVals);
  const y = scale([lo, hi], [h - padBottom, padTop]);

  const hL = svgEl('text', { x: leftX, y: 15, class: 'dn-slope-col', 'text-anchor': 'end' });
  hL.textContent = (o.left && o.left.title) || 'champion';
  const hR = svgEl('text', { x: rightX, y: 15, class: 'dn-slope-col', 'text-anchor': 'start' });
  hR.textContent = (o.right && o.right.title) || 'challenger';
  svg.appendChild(hL); svg.appendChild(hR);
  svg.appendChild(svgEl('line', { x1: leftX, x2: leftX, y1: y(hi), y2: y(lo), class: 'dn-slope-axis' }));
  svg.appendChild(svgEl('line', { x1: rightX, x2: rightX, y1: y(hi), y2: y(lo), class: 'dn-slope-axis' }));

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
    const dirCls = verdict === 'improved' ? 'dn-good' : verdict === 'regressed' ? 'dn-bad' : 'dn-flat';
    const g = svgEl('g', { class: 'dn-pslope-series' });
    if (ay != null && by != null) {
      const line = svgEl('line', { x1: leftX, y1: ay, x2: rightX, y2: by, class: 'dn-pslope-line ' + dirCls });
      hov(line, `${s.label}: ${fmt(s.a)} → ${fmt(s.b)} (${fmtSigned(s.b - s.a)}; ${verdict})`);
      g.appendChild(line);
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'dn-pslope-node ' + dirCls }));
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'dn-pslope-node ' + dirCls }));
    } else if (ay != null) {
      g.appendChild(hov(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'dn-pslope-node dn-flat' }), `${s.label}: champion only ${fmt(s.a)}`));
    } else if (by != null) {
      g.appendChild(hov(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'dn-pslope-node dn-flat' }), `${s.label}: challenger only ${fmt(s.b)}`));
    }
    const ll = leftLabels[i];
    if (isNum(s.a)) {
      if (Math.abs(ll - y(s.a)) > 1.5) g.appendChild(svgEl('line', { x1: leftX - 4, y1: ll, x2: leftX, y2: y(s.a), class: 'dn-leader' }));
      const tx = svgEl('text', { x: leftX - 8, y: ll + 3, class: 'dn-pslope-label', 'text-anchor': 'end' });
      tx.textContent = `${shortLabel(s.label, 14)}  ${fmt(s.a, 1)}`;
      g.appendChild(tx);
    }
    const rl = rightLabels[i];
    if (isNum(s.b)) {
      if (Math.abs(rl - y(s.b)) > 1.5) g.appendChild(svgEl('line', { x1: rightX, y1: y(s.b), x2: rightX + 4, y2: rl, class: 'dn-leader' }));
      const tx = svgEl('text', { x: rightX + 8, y: rl + 3, class: 'dn-pslope-label', 'text-anchor': 'start' });
      tx.textContent = `${fmt(s.b, 1)}  ${shortLabel(s.label, 14)}`;
      g.appendChild(tx);
    }
    clickable(g, o.onClick && (() => o.onClick(s)));
    svg.appendChild(g);
  });
  return svg;
}

// ---- racing ladder (DATA-DRIVEN successive-halving) -----------------
//
// One column per rung from the structure payload's `rounds[]`, escalating
//   (absent ⇒ inferred from championId + live, preserving the old behavior.)
export function racingLadder(opts) {
  const o = opts || {};
  const rungs = (Array.isArray(o.rungs) ? o.rungs : []).filter((r) => r);
  const live = !!o.live;
  const colW = 124;
  const colGap = 30;
  const gateW = 104;
  const rowH = 18;
  const headH = 34;
  const top = 6;
  // the v0 BENCHMARK reference (the reigning champion the field is raced vs; NOT
  // a rung competitor) — drawn as a persistent labelled pace line at Δ=0.
  const benchId = o.benchmarkId != null ? String(o.benchmarkId)
    : (o.championId != null ? String(o.championId) : null);
  const benchH = benchId ? 18 : 0;
  const maxRows = Math.max(1, ...rungs.map((r) => (Array.isArray(r.competitors) ? r.competitors.length : 0)), 1);
  const ladderW = rungs.length * colW + Math.max(0, rungs.length - 1) * colGap;
  const w = Math.max(colW, ladderW + colGap + gateW) + 8;
  const h = top + benchH + headH + maxRows * rowH + 8;
  const svg = svgEl('svg', {
    class: 'dn-raceladder', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
  });
  // the benchmark pace line + label spanning the whole ladder.
  if (benchId) {
    const by = top + 10;
    svg.appendChild(hov(svgEl('line', {
      x1: 2, y1: by + 4, x2: w - 4, y2: by + 4, class: 'dn-raceladder-bench-line',
    }), `champion v0 = ${benchId} · the field is raced vs this benchmark; every Δ is vs v0 · v0 defends at the champion-gate`));
    const bt = svgEl('text', { x: 4, y: by, class: 'dn-raceladder-bench' });
    bt.textContent = `▸ vs champion v0 = ${shortLabel(benchId, 16)} · Δ pace 0 (every Δ is vs v0)`;
    svg.appendChild(bt);
  }
  if (rungs.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no rungs yet';
    svg.appendChild(t);
    return svg;
  }
  // everything below the benchmark band is offset by benchH.
  const headTop = top + benchH;
  const colX = (j) => j * (colW + colGap) + 2;
  const rowY = (i) => headTop + headH + i * rowH + rowH / 2;
  // cache each competitor's row index per rung so connectors can be drawn from
  // a survivor's seat in rung j to the same competitor's seat in rung j+1.
  const rowIndex = rungs.map((rung) => {
    const map = new Map();
    (Array.isArray(rung.competitors) ? rung.competitors : []).forEach((cid, i) => map.set(String(cid), i));
    return map;
  });

  // ── connectors: each survivor of rung j → its seat in rung j+1 ──────
  for (let j = 0; j < rungs.length - 1; j++) {
    const surv = Array.isArray(rungs[j].survivors) ? rungs[j].survivors.map(String) : [];
    const x1 = colX(j) + colW - 6;
    const x2 = colX(j + 1) + 2;
    const mx = (x1 + x2) / 2;
    for (const sid of surv) {
      const yi = rowIndex[j].get(sid);
      const yj = rowIndex[j + 1].get(sid);
      if (yi == null || yj == null) continue;
      svg.appendChild(svgEl('path', {
        d: `M${x1},${rowY(yi)} H${mx} V${rowY(yj)} H${x2}`,
        class: 'dn-raceladder-edge', fill: 'none',
      }));
    }
  }

  rungs.forEach((rung, j) => {
    const x = colX(j);
    // a QUEUED future rung (live race, not yet started) is shown dimmed with its
    // board-fraction label so the whole successive-halving shape is legible.
    const queued = !!rung.queued;
    const prog = (rung.live_progress && typeof rung.live_progress === 'object') ? rung.live_progress : null;
    const head = svgEl('text', { x: x + colW / 2, y: headTop + 12, class: 'dn-raceladder-head' + (queued ? ' dn-raceladder-queued' : ''), 'text-anchor': 'middle' });
    head.textContent = shortLabel(rung.label || `Rung ${j + 1}`, 16) + (queued ? ' · queued' : '');
    svg.appendChild(head);
    if (isNum(rung.board_fraction)) {
      const sub = svgEl('text', { x: x + colW / 2, y: headTop + 26, class: 'dn-raceladder-frac', 'text-anchor': 'middle' });
      sub.textContent = `board ${(rung.board_fraction * 100).toFixed(0)}%`;
      svg.appendChild(sub);
    }
    const cutSet = new Set(Array.isArray(rung.cut) ? rung.cut.map(String) : []);
    const surv = new Set(Array.isArray(rung.survivors) ? rung.survivors.map(String) : []);
    const comps = Array.isArray(rung.competitors) ? rung.competitors : [];
    // a rung whose results are not in yet (no cut + no survivors) is PENDING:
    // every runner is shown neutral (racing), never cut, until the cut lands.
    const pending = !!rung.pending || (cutSet.size === 0 && surv.size === 0);
    comps.forEach((cid, i) => {
      const cy = rowY(i);
      const sid = String(cid);
      const eliminated = !pending && cutSet.has(sid);
      const survived = !pending && (surv.has(sid) || (!eliminated && surv.size === 0 && cutSet.size === 0));
      const racing = pending || (!eliminated && !survived);
      const lane = prog ? prog[sid] : null;
      const g = svgEl('g', { class: 'dn-raceladder-runner' + (queued ? ' dn-raceladder-lane-queued' : ''), tabindex: o.onCompetitor ? '0' : null });
      const cls = 'dn-raceladder-name'
        + (eliminated ? ' dn-out dn-bad' : survived ? ' dn-good' : queued ? ' dn-raceladder-queued' : racing ? ' dn-racing' : '');
      const verdict = eliminated ? 'cut' : survived ? 'survives'
        : queued ? 'queued' : (lane ? 'racing · ' + laneProgressText(lane) : 'racing');
      // partial Δ-vs-champion (live), else the committed rung Δ.
      const partial = lane && isNum(lane.partialDelta) ? lane.partialDelta : null;
      const delta = (rung.deltas && isNum(rung.deltas[sid])) ? rung.deltas[sid] : partial;
      const t = hov(svgEl('text', { x: x + 6, y: cy + 3, class: cls }),
        `${sid} · rung ${j + 1}${isNum(rung.board_fraction) ? ` · board ${(rung.board_fraction * 100).toFixed(0)}%` : ''}${delta != null ? ` · Δ ${fmtSigned(delta, 2)} vs champion` : ''} · ${verdict}`);
      // the lane label: a live lane reads "v3 · k/N", a queued lane "v3 · queued".
      const laneSuffix = eliminated ? ' ✕' : survived ? ' ↑'
        : (lane ? ' · ' + laneProgressText(lane) : (queued ? '' : ''));
      t.textContent = shortLabel(sid, lane ? 8 : 14) + laneSuffix;
      g.appendChild(t);
      // a thin in-flight PROGRESS BAR under a live lane (boards done / total).
      if (lane && (lane.inflight || lane.done)) {
        const barW = colW - 12;
        const frac = (isNum(lane.total) && lane.total > 0)
          ? Math.min(1, (lane.done || 0) / lane.total)
          : (lane.inflight ? 0.5 : 0);
        g.appendChild(svgEl('rect', { x: x + 6, y: cy + 5, width: barW, height: 2, rx: 1, class: 'dn-raceladder-bar-bg' }));
        g.appendChild(svgEl('rect', { x: x + 6, y: cy + 5, width: Math.max(1, barW * frac), height: 2, rx: 1,
          class: 'dn-raceladder-bar' + (lane.inflight ? ' dn-raceladder-bar-live' : '') }));
      }
      // the competitor's Δ-vs-champion at this rung, right-aligned in the column.
      if (delta != null) {
        const dt = svgEl('text', {
          x: x + colW - 6, y: cy + 3, 'text-anchor': 'end',
          class: 'dn-raceladder-delta ' + (delta > 0 ? 'dn-bad' : delta < 0 ? 'dn-good' : ''),
        });
        dt.textContent = fmtSigned(delta, delta !== 0 && Math.abs(delta) < 0.1 ? 3 : 1);
        g.appendChild(dt);
      }
      clickable(g, o.onCompetitor && (() => o.onCompetitor(sid)));
      svg.appendChild(g);
    });
  });

  // ── the trailing champion-gate column ───────────────────────────────
  //
  // The gate is the full-board confirmation duel: the lone survivor faces the
  // When gateState is absent, infer from championId + live (legacy callers).
  const gx = ladderW + colGap + 2;
  const gateHead = svgEl('text', { x: gx + gateW / 2, y: headTop + 12, class: 'dn-raceladder-head', 'text-anchor': 'middle' });
  gateHead.textContent = 'champion-gate';
  svg.appendChild(gateHead);
  const cy = rowY(0);
  const champId = o.championId ? String(o.championId) : null;
  const gateState = o.gateState || (live ? 'deciding'
    : (champId ? 'crowned' : 'pending'));
  const crowned = gateState === 'crowned' && !!champId;
  // connector from the final rung's survivor seat into the gate seat.
  const lastSurv = Array.isArray(rungs[rungs.length - 1].survivors) ? rungs[rungs.length - 1].survivors.map(String) : [];
  const seatId = champId || (lastSurv.length === 1 ? lastSurv[0] : null);
  if (seatId && lastSurv.indexOf(seatId) >= 0) {
    const x1 = colX(rungs.length - 1) + colW - 6;
    const fromY = rowY(rowIndex[rungs.length - 1].get(seatId) || 0);
    const mx = (x1 + gx) / 2;
    svg.appendChild(svgEl('path', {
      d: `M${x1},${fromY} H${mx} V${cy} H${gx}`,
      class: 'dn-raceladder-edge' + (crowned ? ' dn-raceladder-edge-champ' : ''), fill: 'none',
    }));
  }
  const clickId = champId || seatId;
  const gateG = svgEl('g', { class: 'dn-raceladder-gate', tabindex: (clickId && o.onCompetitor) ? '0' : null });
  gateG.appendChild(svgEl('rect', { x: gx, y: cy - rowH / 2 - 1, width: gateW, height: rowH + 2, rx: 3,
    class: 'dn-raceladder-gatebox' + (crowned ? ' dn-good' : '') }));
  const dStr = isNum(o.gateDelta) ? ` · Δ ${fmtSigned(o.gateDelta, 2)}` : '';
  let label;
  let tip;
  if (crowned) {
    label = '♚ ' + shortLabel(champId, 11);
    tip = `${champId} cleared the gate → new champion${dStr}`;
  } else if (gateState === 'stands') {
    label = 'champion stands';
    tip = `the survivor lost the full-board gate — champion stands${dStr}`;
  } else if (gateState === 'deciding') {
    label = 'deciding…';
    tip = champId ? `${champId} leads — gate not yet committed` : 'the final gate is deciding';
  } else {
    label = 'tbd';
    tip = 'awaiting the final survivor';
  }
  const gt = hov(svgEl('text', { x: gx + 6, y: cy + 3, class: 'dn-raceladder-gatelab' + (crowned ? ' dn-good' : '') }), tip);
  gt.textContent = label;
  gateG.appendChild(gt);
  clickable(gateG, (clickId && o.onCompetitor) && (() => o.onCompetitor(clickId)));
  svg.appendChild(gateG);
  return svg;
}

// ---- racing SURVIVAL FUNNEL (the at-a-glance epoch hero) -------------
//
// The successive-halving field rendered as a FLOW that narrows at each cut:
//     (absent ⇒ inferred from championId + live).
export function survivalFunnel(opts) {
  const o = opts || {};
  const rungs = (Array.isArray(o.rungs) ? o.rungs : []).filter((r) => r);
  const live = !!o.live;
  const stageW = 150;
  const stageGap = 20;
  const gateW = 132;
  // three stacked header baselines (bench / head / sub) so nothing collides.
  const benchY = 12;
  const headY = 30;
  const subY = 42;
  const top = 56;           // the flow lane begins below all three header rows
  const laneH = 132;        // the vertical band the surviving flow occupies
  const deadH = 18;         // per-eliminated-branch row height below the lane
  // the widest stack of dead-end branches across stages bounds the figure height.
  const maxDead = Math.max(0, ...rungs.map((r) => (Array.isArray(r.cut) ? r.cut.length : 0)));
  const w = rungs.length * stageW + Math.max(0, rungs.length - 1) * stageGap + stageGap + gateW + 8;
  const h = top + laneH + maxDead * deadH + 26;
  const svg = svgEl('svg', {
    class: 'dn-funnel', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
  });
  if (rungs.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no rungs yet';
    svg.appendChild(t);
    return svg;
  }
  // CHAMPION / v0 BENCHMARK caption — make explicit that the field is raced vs
  // the reigning champion (v0), that every Δ is vs v0, and that v0 defends at
  // the gate. v0 is the benchmark, not one of the rung competitors.
  const benchId = o.benchmarkId != null ? String(o.benchmarkId)
    : (o.championId != null ? String(o.championId) : null);
  if (benchId) {
    const bt = hov(svgEl('text', { x: 2, y: benchY, class: 'dn-funnel-bench' }),
      `champion v0 = ${benchId} · the field is raced vs this benchmark; every Δ is vs v0 · v0 defends at the champion-gate`);
    bt.textContent = `▸ vs champion v0 = ${shortLabel(benchId, 18)} · every Δ is vs v0`;
    svg.appendChild(bt);
  }
  const midY = top + laneH / 2;
  // the entering field of stage 0 sets the maximum flow width (100% lane).
  const field0 = Math.max(1, (Array.isArray(rungs[0].competitors) ? rungs[0].competitors.length : 1));
  // a stage's flow band half-height ∝ its entering field size.
  const bandHalf = (n) => Math.max(6, (laneH / 2) * (Math.max(0, n) / field0));
  const stageX = (j) => j * (stageW + stageGap) + 2;

  // ── the flowing band: one trapezoid per stage, narrowing at each cut ──
  // entering width = |competitors|; leaving width = |survivors| (the field
  // carried to the next stage). A pending (live, undecided) stage keeps a
  rungs.forEach((rung, j) => {
    const comps = Array.isArray(rung.competitors) ? rung.competitors : [];
    const cut = new Set(Array.isArray(rung.cut) ? rung.cut.map(String) : []);
    const surv = Array.isArray(rung.survivors) ? rung.survivors.map(String) : [];
    const pending = !!rung.pending || (cut.size === 0 && surv.length === 0);
    const enterN = comps.length;
    const leaveN = pending ? enterN : surv.length;
    const x0 = stageX(j);
    const x1 = x0 + stageW;
    const hIn = bandHalf(enterN);
    const hOut = bandHalf(leaveN);
    const cls = 'dn-funnel-band' + (pending ? ' dn-funnel-pending' : '');
    // a trapezoid: left edge full enter-height, right edge narrowed to leave-height.
    svg.appendChild(hov(svgEl('polygon', {
      points: `${x0},${midY - hIn} ${x1},${midY - hOut} ${x1},${midY + hOut} ${x0},${midY + hIn}`,
      class: cls,
    }), `${rung.label || 'rung ' + j}: ${enterN} in → ${pending ? '…' : leaveN + ' survive'}${isNum(rung.board_fraction) ? ` · ${(rung.board_fraction * 100).toFixed(0)}% board` : ''}`));
    // stage label + board fraction above the band — on the dedicated header /
    // sub baselines (headY / subY), CENTRED on this stage's column x, so they
    // never overlap each other, an adjacent column, or the benchmark line.
    const head = svgEl('text', { x: x0 + stageW / 2, y: headY, class: 'dn-funnel-head', 'text-anchor': 'middle' });
    head.textContent = shortLabel(rung.label || `Rung ${j}`, 16);
    svg.appendChild(head);
    const sub = svgEl('text', { x: x0 + stageW / 2, y: subY, class: 'dn-funnel-sub', 'text-anchor': 'middle' });
    sub.textContent = `${enterN} field` + (isNum(rung.board_fraction) ? ` · ${(rung.board_fraction * 100).toFixed(0)}/100 board` : '');
    svg.appendChild(sub);

    // ── the surviving runners ride INSIDE the band (↑), clickable ──
    const survRunners = pending ? comps.map(String) : surv;
    survRunners.forEach((sid, i) => {
      const cy = survRunners.length === 1 ? midY
        : midY - hOut + 8 + (i * (Math.max(1, 2 * hOut - 16)) / Math.max(1, survRunners.length - 1));
      funnelRunner(svg, o, sid, rung, j, x0 + 8, cy, pending ? 'racing' : 'survives');
    });

    // ── eliminated competitors peel off as labelled dead-end branches (✕) ──
    if (!pending) {
      [...cut].forEach((cid, i) => {
        const sid = String(cid);
        const branchY = top + laneH + 6 + i * deadH;
        const elbowX = x0 + stageW * 0.5;
        // a dead-end branch from the band's lower edge down to the cut row.
        svg.appendChild(svgEl('path', {
          d: `M${elbowX},${midY + hIn} V${branchY} H${x0 + stageW - 10}`,
          class: 'dn-funnel-deadedge', fill: 'none',
        }));
        funnelRunner(svg, o, sid, rung, j, elbowX + 4, branchY - 1, 'cut');
      });
    }
  });

  // ── the terminal CHAMPION-GATE ──
  const finalSurv = (() => {
    for (let i = rungs.length - 1; i >= 0; i--) {
      const s = Array.isArray(rungs[i].survivors) ? rungs[i].survivors.map(String) : [];
      if (s.length) return s;
    }
    return [];
  })();
  const champId = o.championId ? String(o.championId) : null;
  const gateState = o.gateState || (live ? 'deciding' : (champId ? 'crowned' : 'pending'));
  const crowned = gateState === 'crowned' && !!champId;
  const seatId = champId || (finalSurv.length === 1 ? finalSurv[0] : null);
  const gx = rungs.length * stageW + Math.max(0, rungs.length - 1) * stageGap + stageGap + 2;
  // the converging flow from the last stage's surviving band into the gate.
  const lastLeave = (() => {
    const r = rungs[rungs.length - 1];
    const c = Array.isArray(r.competitors) ? r.competitors : [];
    const s = Array.isArray(r.survivors) ? r.survivors.map(String) : [];
    const pend = !!r.pending || (s.length === 0 && (!Array.isArray(r.cut) || r.cut.length === 0));
    return pend ? c.length : s.length;
  })();
  const flowH = bandHalf(lastLeave);
  const lastX = stageX(rungs.length - 1) + stageW;
  svg.appendChild(svgEl('polygon', {
    points: `${lastX},${midY - flowH} ${gx},${midY - 11} ${gx},${midY + 11} ${lastX},${midY + flowH}`,
    class: 'dn-funnel-band dn-funnel-gateflow' + (crowned ? ' dn-good' : ''),
  }));
  const gHead = svgEl('text', { x: gx + gateW / 2, y: headY, class: 'dn-funnel-head', 'text-anchor': 'middle' });
  gHead.textContent = 'champion-gate';
  svg.appendChild(gHead);
  const gSub = svgEl('text', { x: gx + gateW / 2, y: subY, class: 'dn-funnel-sub', 'text-anchor': 'middle' });
  gSub.textContent = benchId ? 'full board · vs champion v0' : 'full board · vs champion';
  svg.appendChild(gSub);

  const clickId = champId || seatId;
  const gateG = svgEl('g', { class: 'dn-funnel-gate', tabindex: (clickId && o.onCompetitor) ? '0' : null });
  gateG.appendChild(svgEl('rect', {
    x: gx, y: midY - 16, width: gateW, height: 32, rx: 5,
    class: 'dn-funnel-gatebox' + (crowned ? ' dn-good' : ''),
  }));
  const dStr = isNum(o.gateDelta) ? ` · Δ ${fmtSigned(o.gateDelta, 2)}` : '';
  let label;
  let tip;
  if (crowned) {
    label = '♚ ' + shortLabel(champId, 12);
    tip = `${champId} cleared the full-board gate → crowned champion${dStr}`;
  } else if (gateState === 'stands') {
    label = 'champion stands';
    tip = `the survivor lost the full-board gate — champion stands${dStr}`;
  } else if (gateState === 'deciding') {
    label = 'deciding…';
    tip = champId ? `${champId} leads — the gate has not committed${dStr}` : 'the final gate is deciding';
  } else {
    label = 'tbd';
    tip = 'awaiting the final survivor';
  }
  const gt = hov(svgEl('text', { x: gx + gateW / 2, y: midY + 4, class: 'dn-funnel-gatelab' + (crowned ? ' dn-good' : ''), 'text-anchor': 'middle' }), tip);
  gt.textContent = label;
  gateG.appendChild(gt);
  clickable(gateG, (clickId && o.onCompetitor) && (() => o.onCompetitor(clickId)));
  svg.appendChild(gateG);
  return svg;
}

// One funnel competitor label (a survivor riding the band, or a peeled-off
// eliminated dead-end). Hover → its per-rung Δ + cut/survive verdict; click →
// its candidate. `verdict` ∈ {survives, cut, racing}.
function funnelRunner(svg, o, sid, rung, j, x, cy, verdict) {
  const delta = (rung.deltas && isNum(rung.deltas[sid])) ? rung.deltas[sid] : null;
  const glyph = verdict === 'cut' ? ' ✕' : verdict === 'survives' ? ' ↑' : '';
  const cls = 'dn-funnel-name'
    + (verdict === 'cut' ? ' dn-out dn-bad' : verdict === 'survives' ? ' dn-good' : ' dn-racing');
  const tip = `${sid} · ${rung.label || 'rung ' + j}`
    + (isNum(rung.board_fraction) ? ` · ${(rung.board_fraction * 100).toFixed(0)}% board` : '')
    + (delta != null ? ` · Δ ${fmtSigned(delta, 2)} vs champion` : '')
    + ` · ${verdict}`;
  const g = svgEl('g', { class: 'dn-funnel-runner', tabindex: o.onCompetitor ? '0' : null });
  const t = hov(svgEl('text', { x, y: cy + 3, class: cls }), tip);
  t.textContent = shortLabel(sid, 13) + glyph;
  g.appendChild(t);
  clickable(g, o.onCompetitor && (() => o.onCompetitor(sid)));
  svg.appendChild(g);
}

// ---- swiss STANDINGS LADDER (DATA-DRIVEN, live + completed) ----------
//
// The swiss analogue of racingLadder/survivalFunnel: a column per round (its
//   gateState ∈ 'crowned'|'stands'|'deciding'|'pending' (else inferred).
export function swissLadder(opts) {
  const o = opts || {};
  const rounds = (Array.isArray(o.rounds) ? o.rounds : []).filter((r) => r);
  const standings = (Array.isArray(o.standings) ? o.standings : []).filter((s) => s);
  const live = !!o.live;
  const colW = 150;
  const colGap = 22;
  const standW = 150;
  const gateW = 124;
  const pairH = 30;
  const headH = 32;
  const top = 8;
  const benchId = o.benchmarkId != null ? String(o.benchmarkId)
    : (o.championId != null ? String(o.championId) : null);
  const benchH = benchId ? 16 : 0;
  const maxPairs = Math.max(1, ...rounds.map((r) => (Array.isArray(r.pairings) ? r.pairings.length : 0)), 1);
  const maxRows = Math.max(maxPairs, standings.length, 1);
  const ladderW = rounds.length * colW + Math.max(0, rounds.length - 1) * colGap;
  // a standings column + a champion-gate column ride after the round columns.
  const w = Math.max(colW, ladderW + colGap + standW + colGap + gateW) + 8;
  const h = top + benchH + headH + maxRows * pairH + 8;
  const svg = svgEl('svg', {
    class: 'dn-swissladder', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
  });
  if (benchId) {
    const bt = hov(svgEl('text', { x: 4, y: top + 10, class: 'dn-swissladder-bench' }),
      `incumbent champion = ${benchId} · the swiss winner must beat the incumbent at the champion-gate to be promoted`);
    bt.textContent = `▸ incumbent champion = ${shortLabel(benchId, 18)} · defends at the gate`;
    svg.appendChild(bt);
  }
  if (rounds.length === 0 && standings.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no swiss rounds yet';
    svg.appendChild(t);
    return svg;
  }
  const headTop = top + benchH;
  const colX = (j) => j * (colW + colGap) + 2;
  const rowY = (i) => headTop + headH + i * pairH + pairH / 2;

  // ── round columns: each round's pairings (a vs b → winner) ──
  rounds.forEach((rnd, j) => {
    const x = colX(j);
    const queued = !!rnd.queued;
    const head = svgEl('text', { x: x + colW / 2, y: headTop + 12, class: 'dn-swissladder-head' + (queued ? ' dn-swissladder-queued' : ''), 'text-anchor': 'middle' });
    head.textContent = shortLabel(rnd.label || `Round ${j + 1}`, 16) + (queued ? ' · queued' : '');
    svg.appendChild(head);
    const pairings = Array.isArray(rnd.pairings) ? rnd.pairings : [];
    pairings.forEach((p, i) => {
      const cy = rowY(i);
      const decided = !!p.winner && !p.pending;
      const inflight = !!p.inflight || (isNum(p.total) && isNum(p.done) && p.done < p.total && !decided);
      const g = svgEl('g', { class: 'dn-swissladder-pair' + (queued ? ' dn-swissladder-lane-queued' : ''), tabindex: o.onCompetitor ? '0' : null });
      const a = p.a == null ? 'bye' : String(p.a);
      const b = p.bye ? 'bye' : (p.b == null ? '—' : String(p.b));
      const aWon = decided && p.winner === p.a;
      const bWon = decided && p.winner === p.b;
      const progText = decided ? (p.winner === p.a ? ` · ${shortLabel(a, 8)} ↑` : ` · ${shortLabel(String(p.winner), 8)} ↑`)
        : queued ? ' · queued'
        : inflight ? ' · ' + (isNum(p.total) && p.total > 0 ? `${p.done || 0}/${p.total} boards` : `${p.inflight || 0} running`)
        : ' · pairing';
      const cls = 'dn-swissladder-pairlab' + (queued ? ' dn-swissladder-queued' : (inflight ? ' dn-racing' : ''));
      const t = hov(svgEl('text', { x: x + 6, y: cy + 3, class: cls }),
        `${a} vs ${b}${decided ? ' → ' + p.winner : ''}${isNum(p.delta) ? ` · Δ ${fmtSigned(p.delta, 2)}` : ''}`);
      const aCls = aWon ? ' ↑' : '';
      t.textContent = shortLabel(a, 6) + ' v ' + shortLabel(b, 6) + (decided ? '' : progText.replace(' · pairing', ''));
      g.appendChild(t);
      if (decided) {
        const sub = svgEl('text', { x: x + 6, y: cy + 13, class: 'dn-swissladder-win dn-good' });
        sub.textContent = shortLabel(String(p.winner), 10) + ' ↑';
        g.appendChild(sub);
      } else if (inflight) {
        const barW = colW - 12;
        const frac = (isNum(p.total) && p.total > 0) ? Math.min(1, (p.done || 0) / p.total) : 0.5;
        g.appendChild(svgEl('rect', { x: x + 6, y: cy + 7, width: barW, height: 2, rx: 1, class: 'dn-swissladder-bar-bg' }));
        g.appendChild(svgEl('rect', { x: x + 6, y: cy + 7, width: Math.max(1, barW * frac), height: 2, rx: 1, class: 'dn-swissladder-bar dn-swissladder-bar-live' }));
      }
      { const open = p.winner || p.a || p.b;
        clickable(g, (o.onCompetitor && open) && (() => o.onCompetitor(String(open)))); }
      svg.appendChild(g);
    });
  });

  // ── the accumulating Copeland-point standings column ──
  const sx = ladderW + colGap + 2;
  const sHead = svgEl('text', { x: sx + standW / 2, y: headTop + 12, class: 'dn-swissladder-head', 'text-anchor': 'middle' });
  sHead.textContent = 'standings';
  svg.appendChild(sHead);
  const leaderId = standings.length ? String(standings[0].id) : null;
  standings.forEach((s, i) => {
    const cy = rowY(i);
    const sid = String(s.id);
    const isLeader = sid === leaderId;
    const g = svgEl('g', { class: 'dn-swissladder-stand', tabindex: o.onCompetitor ? '0' : null });
    const lab = hov(svgEl('text', { x: sx + 6, y: cy + 3, class: 'dn-swissladder-standlab' + (isLeader ? ' dn-good' : '') }),
      `${sid} · ${isNum(s.points) ? fmt(s.points, 1) : '?'} pts · ${s.wins || 0}W ${s.draws || 0}D ${s.losses || 0}L`);
    lab.textContent = `${i + 1}. ${shortLabel(sid, 9)}` + (isLeader ? ' ♔' : '');
    g.appendChild(lab);
    const pts = svgEl('text', { x: sx + standW - 6, y: cy + 3, 'text-anchor': 'end', class: 'dn-swissladder-pts' + (isLeader ? ' dn-good' : '') });
    pts.textContent = isNum(s.points) ? fmt(s.points, s.points % 1 ? 1 : 0) : '—';
    g.appendChild(pts);
    clickable(g, o.onCompetitor && (() => o.onCompetitor(sid)));
    svg.appendChild(g);
  });

  // ── the champion-gate column (the leader vs the incumbent) ──
  const champId = o.championId ? String(o.championId) : null;
  const gateState = o.gateState || (live ? 'deciding' : (champId ? 'crowned' : 'pending'));
  const crowned = gateState === 'crowned' && !!champId;
  const gx = sx + standW + colGap;
  const gateHead = svgEl('text', { x: gx + gateW / 2, y: headTop + 12, class: 'dn-swissladder-head', 'text-anchor': 'middle' });
  gateHead.textContent = 'champion-gate';
  svg.appendChild(gateHead);
  const cy = rowY(0);
  if (leaderId) {
    const x1 = sx + standW;
    const mx = (x1 + gx) / 2;
    svg.appendChild(svgEl('path', { d: `M${x1},${cy} H${mx} V${cy} H${gx}`, class: 'dn-swissladder-edge' + (crowned ? ' dn-swissladder-edge-champ' : ''), fill: 'none' }));
  }
  const clickId = champId || leaderId;
  const gateG = svgEl('g', { class: 'dn-swissladder-gate', tabindex: (clickId && o.onCompetitor) ? '0' : null });
  gateG.appendChild(svgEl('rect', { x: gx, y: cy - pairH / 2, width: gateW, height: pairH, rx: 4, class: 'dn-swissladder-gatebox' + (crowned ? ' dn-good' : '') }));
  const dStr = isNum(o.gateDelta) ? ` · Δ ${fmtSigned(o.gateDelta, 2)}` : '';
  let label;
  let tip;
  if (crowned) { label = '♚ ' + shortLabel(champId, 11); tip = `${champId} won the swiss + cleared the gate → new champion${dStr}`; }
  else if (gateState === 'stands') { label = 'champion stands'; tip = `the swiss winner did not beat the incumbent — champion stands${dStr}`; }
  else if (gateState === 'deciding') { label = 'deciding…'; tip = leaderId ? `${leaderId} leads — gate not yet committed` : 'the gate is deciding'; }
  else { label = 'tbd'; tip = 'awaiting the swiss leader'; }
  const gt = hov(svgEl('text', { x: gx + 6, y: cy + 3, class: 'dn-swissladder-gatelab' + (crowned ? ' dn-good' : '') }), tip);
  gt.textContent = label;
  gateG.appendChild(gt);
  clickable(gateG, (clickId && o.onCompetitor) && (() => o.onCompetitor(String(clickId))));
  svg.appendChild(gateG);
  return svg;
}

// ---- elim BRACKET TREE (DATA-DRIVEN single/double, live + completed) -
//
// The elimination analogue of survivalFunnel/racingLadder: a real bracket tree
//     live, gateState, gateDelta, onMatch, onCompetitor }
export function elimBracket(opts) {
  const o = opts || {};
  const winners = (Array.isArray(o.winners) ? o.winners : []).filter((r) => r && Array.isArray(r.matches));
  const losers = Array.isArray(o.losers) ? o.losers.filter((r) => r && Array.isArray(r.matches)) : null;
  const live = !!o.live;
  // COMPACT mode (the epoch-card mini-bracket overview) shrinks the geometry +
  // tags the root `dn-elimbracket-compact` so it reads as a glance, not the full
  // Match-ups tree. Same marks, same class family — one renderer, two scales.
  const compact = !!o.compact;
  const colW = compact ? 96 : 138;
  const colGap = compact ? 18 : 30;
  const matchH = compact ? 26 : 40;
  const matchGap = compact ? 8 : 14;
  const gateW = compact ? 96 : 120;
  const top = compact ? 20 : 22;
  const benchId = o.benchmarkId != null ? String(o.benchmarkId) : (o.championId != null ? String(o.championId) : null);
  const benchH = benchId ? 16 : 0;
  const wbMax = Math.max(1, ...winners.map((r) => r.matches.length || 0), 1);
  const lbMax = losers ? Math.max(0, ...losers.map((r) => r.matches.length || 0)) : 0;
  const treeW = winners.length * colW + Math.max(0, winners.length - 1) * colGap;
  const w = Math.max(colW, treeW + colGap + gateW) + 8;
  const wbBand = top + benchH + wbMax * (matchH + matchGap);
  const lbHeadH = (losers && losers.length) ? 22 : 0;
  const lbBand = (losers && losers.length) ? lbHeadH + lbMax * (matchH + matchGap) : 0;
  const h = wbBand + lbBand + 12;
  const svg = svgEl('svg', {
    class: 'dn-elimbracket' + (compact ? ' dn-elimbracket-compact' : ''), width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
  });
  if (benchId) {
    const bt = hov(svgEl('text', { x: 4, y: 12, class: 'dn-elimbracket-bench' }),
      `incumbent champion = ${benchId} · the bracket winner must beat the incumbent at the champion-gate to be promoted`);
    bt.textContent = (compact ? `▸ incumbent = ${shortLabel(benchId, 16)} · defends at the gate`
      : `▸ incumbent champion = ${shortLabel(benchId, 18)} · defends at the gate`);
    svg.appendChild(bt);
  }
  if (winners.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no bracket matches yet';
    svg.appendChild(t);
    return svg;
  }
  const colX = (j) => j * (colW + colGap) + 2;

  // draw a band (winners' or losers') of match columns, returning each round's
  // vertical centres so connectors + the gate can attach to the final match.
  function drawBand(band, bandTop, bandH, cls) {
    const centers = band.map((r) => {
      const n = Math.max(1, r.matches.length);
      const blockH = n * matchH + (n - 1) * matchGap;
      const y0 = bandTop + Math.max(0, (bandH - blockH) / 2);
      return r.matches.map((_, i) => y0 + i * (matchH + matchGap) + matchH / 2);
    });
    for (let j = 0; j < band.length - 1; j++) {
      const cur = centers[j];
      const nxt = centers[j + 1];
      cur.forEach((cy, i) => {
        const tgt = nxt[Math.floor(i / 2)];
        if (tgt == null) return;
        const x1 = colX(j) + colW;
        const x2 = colX(j + 1);
        const mx = (x1 + x2) / 2;
        svg.appendChild(svgEl('path', { d: `M${x1},${cy} H${mx} V${tgt} H${x2}`, class: 'dn-elimbracket-edge ' + cls, fill: 'none' }));
      });
    }
    band.forEach((r, j) => {
      const x = colX(j);
      const head = svgEl('text', { x: x + colW / 2, y: bandTop - 6, class: 'dn-elimbracket-head', 'text-anchor': 'middle' });
      head.textContent = shortLabel(r.label || `Round ${j + 1}`, 18) + (r.queued ? ' · queued' : '');
      svg.appendChild(head);
      r.matches.forEach((m, i) => {
        const cy = centers[j][i];
        drawMatch(m, x, cy);
      });
    });
    return centers;
  }

  function drawMatch(m, x, cy) {
    const y = cy - matchH / 2;
    const comps = Array.isArray(m.competitors) ? m.competitors : [];
    const winner = m.winner || '';
    const decided = !!winner && !m.pending;
    const inflight = !!m.inflight || (isNum(m.total) && isNum(m.done) && m.done < m.total && !decided);
    const queued = !!m.queued;
    const g = svgEl('g', { class: 'dn-elimbracket-match' + (queued ? ' dn-elimbracket-queued' : ''), tabindex: o.onMatch ? '0' : null });
    g.appendChild(hov(svgEl('rect', {
      x, y, width: colW, height: matchH, rx: 3,
      class: 'dn-elimbracket-box' + (m.bye ? ' dn-bye' : '') + (inflight ? ' dn-elimbracket-box-live' : ''),
    }), `${m.bracket_slot || m.match_id || ''}${comps.length ? ': ' + comps.join(' vs ') : ''}${winner ? ' → ' + winner : (inflight ? ' · racing' : '')}`));
    const seats = comps.length ? comps : ['tbd'];
    seats.slice(0, 2).forEach((cid, k) => {
      const sy = y + (k === 0 ? matchH * 0.30 : matchH * 0.66);
      const won = cid === winner;
      const seat = svgEl('text', {
        x: x + 8, y: sy + 3,
        class: 'dn-elimbracket-seat' + (won ? ' dn-win' : (decided ? ' dn-out' : (queued ? ' dn-elimbracket-queued' : ''))),
      });
      seat.textContent = shortLabel(String(cid), 13) + (won ? ' ✦' : '');
      g.appendChild(seat);
    });
    if (inflight) {
      const barW = colW - 12;
      const frac = (isNum(m.total) && m.total > 0) ? Math.min(1, (m.done || 0) / m.total) : 0.5;
      g.appendChild(svgEl('rect', { x: x + 6, y: y + matchH - 5, width: barW, height: 2, rx: 1, class: 'dn-elimbracket-bar-bg' }));
      g.appendChild(svgEl('rect', { x: x + 6, y: y + matchH - 5, width: Math.max(1, barW * frac), height: 2, rx: 1, class: 'dn-elimbracket-bar dn-elimbracket-bar-live' }));
      const pl = svgEl('text', { x: x + colW - 6, y: y + matchH - 7, 'text-anchor': 'end', class: 'dn-elimbracket-prog' });
      pl.textContent = (isNum(m.total) && m.total > 0) ? `${m.done || 0}/${m.total}` : `${m.inflight || 0}…`;
      g.appendChild(pl);
    }
    if (m.bye) {
      const b = svgEl('text', { x: x + colW - 6, y: y + matchH - 6, class: 'dn-elimbracket-bye', 'text-anchor': 'end' });
      b.textContent = 'bye';
      g.appendChild(b);
    }
    clickable(g, o.onMatch && (() => o.onMatch(m)));
    svg.appendChild(g);
  }

  const wbTop = top + benchH;
  const wbCenters = drawBand(winners, wbTop, wbMax * (matchH + matchGap), 'dn-elimbracket-wb');

  // ── the champion-gate node after the winners' final ──
  const champId = o.championId ? String(o.championId) : null;
  const gateState = o.gateState || (live ? 'deciding' : (champId ? 'crowned' : 'pending'));
  const crowned = gateState === 'crowned' && !!champId;
  const gx = treeW + colGap + 2;
  const lastCol = wbCenters[wbCenters.length - 1] || [];
  const gateCy = lastCol.length ? lastCol[Math.floor((lastCol.length - 1) / 2)] : wbTop + wbMax * (matchH + matchGap) / 2;
  if (winners.length) {
    const x1 = colX(winners.length - 1) + colW;
    const mx = (x1 + gx) / 2;
    svg.appendChild(svgEl('path', { d: `M${x1},${gateCy} H${mx} V${gateCy} H${gx}`, class: 'dn-elimbracket-edge dn-elimbracket-wb' + (crowned ? ' dn-elimbracket-edge-champ' : ''), fill: 'none' }));
  }
  const gateHead = svgEl('text', { x: gx + gateW / 2, y: wbTop - 6, class: 'dn-elimbracket-head', 'text-anchor': 'middle' });
  gateHead.textContent = 'champion-gate';
  svg.appendChild(gateHead);
  const clickId = champId || null;
  const gateG = svgEl('g', { class: 'dn-elimbracket-gate', tabindex: (clickId && o.onCompetitor) ? '0' : null });
  gateG.appendChild(svgEl('rect', { x: gx, y: gateCy - matchH / 2, width: gateW, height: matchH, rx: 4, class: 'dn-elimbracket-gatebox' + (crowned ? ' dn-good' : '') }));
  const dStr = isNum(o.gateDelta) ? ` · Δ ${fmtSigned(o.gateDelta, 2)}` : '';
  let label;
  let tip;
  if (crowned) { label = '♚ ' + shortLabel(champId, 11); tip = `${champId} won the bracket + cleared the gate → new champion${dStr}`; }
  else if (gateState === 'stands') { label = 'champion stands'; tip = `the bracket winner did not beat the incumbent — champion stands${dStr}`; }
  else if (gateState === 'deciding') { label = 'deciding…'; tip = 'the gate is deciding'; }
  else { label = 'tbd'; tip = 'awaiting the bracket winner'; }
  const gt = hov(svgEl('text', { x: gx + 6, y: gateCy + 3, class: 'dn-elimbracket-gatelab' + (crowned ? ' dn-good' : '') }), tip);
  gt.textContent = label;
  gateG.appendChild(gt);
  clickable(gateG, (clickId && o.onCompetitor) && (() => o.onCompetitor(String(clickId))));
  svg.appendChild(gateG);

  // ── the losers' band (double-elim only) ──
  if (losers && losers.length) {
    const lbTop = wbBand + lbHeadH;
    const sep = svgEl('text', { x: 4, y: wbBand + 12, class: 'dn-elimbracket-bandhead' });
    sep.textContent = 'LOSERS’ BRACKET';
    svg.appendChild(sep);
    drawBand(losers, lbTop, lbMax * (matchH + matchGap), 'dn-elimbracket-lb');
  }
  return svg;
}

// ---- COMPACT SWISS OVERVIEW (epoch-card hero) ----------------------
// (1) a STANDINGS BUMP CHART — one line per competitor, x = round, y = rank
//     (1 at top); lines cross as the leader emerges (champion line bold).
// (2) a RANKED COPELAND-POINT BAR — final standings, leader ♔, gate verdict.
//   { series:[{id,champion,ranks}], bars:[{id,points,wins,draws,losses,leader,
//     champion}], labels, championId, benchmarkId, gateState, gateDelta, live,
//     onCompetitor(id) }
export function swissOverview(opts) {
  const o = opts || {};
  const series = (Array.isArray(o.series) ? o.series : []).filter((s) => s && Array.isArray(s.ranks));
  const bars = (Array.isArray(o.bars) ? o.bars : []).filter((b) => b);
  const labels = Array.isArray(o.labels) ? o.labels : [];
  const live = !!o.live;
  const w = 640;
  const nR = Math.max(1, labels.length);
  const nC = Math.max(1, series.length, bars.length);
  // bump panel geometry
  const bumpTop = 26;
  const rowH = 22;
  const bumpH = bumpTop + nC * rowH + 10;
  const padL = 96;          // left gutter for the round-0 competitor labels
  const padR = 120;         // right gutter for the final-rank labels
  const barTop = bumpH + 30;
  const barH = 18;
  const barGap = 8;
  const barBandH = bars.length * (barH + barGap);
  const h = barTop + barBandH + 14;
  const svg = svgEl('svg', {
    class: 'dn-swissover', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
  });
  if (!series.length && !bars.length) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no swiss rounds yet';
    svg.appendChild(t);
    return svg;
  }

  // panel (1): the standings BUMP CHART
  const ttl = svgEl('text', { x: 2, y: 14, class: 'dn-swissover-title' });
  ttl.textContent = 'standings by round' + (live ? ' · LIVE' : '');
  svg.appendChild(ttl);
  const X = scale([0, Math.max(1, nR - 1)], [padL, w - padR]);
  const Y = scale([1, Math.max(2, nC)], [bumpTop, bumpTop + (nC - 1) * rowH]);
  labels.forEach((lab, j) => {
    const x = X(j);
    const tk = svgEl('text', { x, y: bumpTop - 8, class: 'dn-swissover-round', 'text-anchor': j === 0 ? 'start' : (j === labels.length - 1 ? 'end' : 'middle') });
    tk.textContent = shortLabel(String(lab), 8);
    svg.appendChild(tk);
    svg.appendChild(svgEl('line', { x1: x, x2: x, y1: bumpTop - 4, y2: bumpTop + (nC - 1) * rowH + 4, class: 'dn-swissover-grid' }));
  });
  // one polyline per competitor; champion emphasised.
  series.forEach((s) => {
    const pts = [];
    s.ranks.forEach((r, j) => { if (isNum(r)) pts.push([X(j), Y(r)]); });
    if (!pts.length) return;
    const champ = !!s.champion;
    const cls = 'dn-swissover-line' + (champ ? ' dn-swissover-line-champ' : '');
    const d = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
    const path = clickable(hov(svgEl('path', { d, class: cls, fill: 'none', tabindex: o.onCompetitor ? '0' : null }),
      `${s.id}${champ ? ' · champion' : ''} · finishes rank ${s.ranks[s.ranks.length - 1] || '?'}`),
      o.onCompetitor && (() => o.onCompetitor(s.id)));
    svg.appendChild(path);
    // end-dots (start + final rank) + left name label + right rank label.
    const [x0, y0] = pts[0];
    const [xn, yn] = pts[pts.length - 1];
    const dotCls = 'dn-swissover-dot' + (champ ? ' dn-swissover-dot-champ' : '');
    const r = champ ? 3.4 : 2.6;
    svg.appendChild(svgEl('circle', { cx: x0, cy: y0, r, class: dotCls }));
    svg.appendChild(svgEl('circle', { cx: xn, cy: yn, r, class: dotCls }));
    const lL = svgEl('text', { x: x0 - 6, y: y0 + 3, class: 'dn-swissover-name' + (champ ? ' dn-swissover-name-champ' : ''), 'text-anchor': 'end' });
    lL.textContent = shortLabel(s.id, 11) + (champ ? ' ♛' : '');
    svg.appendChild(lL);
    const lR = svgEl('text', { x: xn + 6, y: yn + 3, class: 'dn-swissover-rank', 'text-anchor': 'start' });
    lR.textContent = '#' + (s.ranks[s.ranks.length - 1] || '?');
    svg.appendChild(lR);
  });

  // ── panel (2): the RANKED COPELAND-POINT BAR ──
  const bt = svgEl('text', { x: 2, y: barTop - 10, class: 'dn-swissover-title' });
  bt.textContent = 'Copeland points · final standings';
  svg.appendChild(bt);
  const maxPts = Math.max(1, ...bars.map((b) => b.points || 0));
  const champId = o.championId ? String(o.championId) : null;
  const gateState = o.gateState || (live ? 'deciding' : 'pending');
  const barX0 = padL;
  const barMaxW = w - padR - barX0;
  bars.forEach((b, i) => {
    const y = barTop + i * (barH + barGap);
    const bw = Math.max(2, barMaxW * ((b.points || 0) / maxPts));
    const g = svgEl('g', { class: 'dn-swissover-barrow', tabindex: o.onCompetitor ? '0' : null });
    const lab = svgEl('text', { x: barX0 - 6, y: y + barH / 2 + 3, class: 'dn-swissover-barname' + (b.leader ? ' dn-swissover-name-champ' : ''), 'text-anchor': 'end' });
    lab.textContent = (i + 1) + '. ' + shortLabel(b.id, 9) + (b.leader ? ' ♔' : '');
    g.appendChild(lab);
    g.appendChild(svgEl('rect', { x: barX0, y, width: barMaxW, height: barH, rx: 3, class: 'dn-swissover-bar-bg' }));
    g.appendChild(hov(svgEl('rect', { x: barX0, y, width: bw, height: barH, rx: 3, class: 'dn-swissover-bar' + (b.leader ? ' dn-swissover-bar-lead' : '') }),
      `${b.id} · ${fmt(b.points, b.points % 1 ? 1 : 0)} pts · ${b.wins}W ${b.draws}D ${b.losses}L`));
    const pv = svgEl('text', { x: barX0 + bw + 5, y: y + barH / 2 + 3, class: 'dn-swissover-barval' });
    pv.textContent = fmt(b.points, b.points % 1 ? 1 : 0) + ' pts';
    g.appendChild(pv);
    clickable(g, o.onCompetitor && (() => o.onCompetitor(b.id)));
    svg.appendChild(g);
  });
  // the champion-gate verdict, anchored at the bottom-right.
  const crowned = gateState === 'crowned' && !!champId;
  const vy = barTop + barBandH + 4;
  let verdict;
  if (crowned) verdict = `♛ ${shortLabel(champId, 12)} promoted`;
  else if (gateState === 'stands') verdict = 'champion stands';
  else if (gateState === 'deciding') verdict = 'gate deciding…';
  else verdict = '';
  if (verdict) {
    const vt = svgEl('text', { x: w - padR, y: vy, class: 'dn-swissover-verdict' + (crowned ? ' dn-good' : ''), 'text-anchor': 'end' });
    vt.textContent = verdict + (isNum(o.gateDelta) ? ` · Δ ${fmtSigned(o.gateDelta, 2)}` : '');
    svg.appendChild(vt);
  }
  return svg;
}

// The compact ELIM mini-bracket (epoch overview) is just elimBracket at a small
// scale — call svg.elimBracket({ compact: true, …elimModel(st) }) directly.

// ---- Tufte Sankey (fit-to-width) — the causal patch→drift→gate flow -
export function layoutSankey(spec) {
  const colW = spec.nodeW || 150;
  const top = spec.top || 30;
  const colHeight = spec.colHeight || 360;
  const minNodeH = spec.minNodeH || 22;
  const gap = spec.nodeGap || 12;
  const totalW = spec.width || 720;

  const stages = ['patch', 'drift', 'gate'];
  const cols = { patch: spec.patch || [], drift: spec.drift || [], gate: spec.gate || [] };
  const links = spec.links || [];

  // Fit to width: three columns + two gaps fill the container exactly.
  const colGap = Math.max(40, (totalW - 3 * colW) / 2);

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
      const node = { id: n.id, stage, x, y, h, w: colW, label: n.label != null ? n.label : n.id, sub: n.sub || '', cls: n.cls || '', value: n.value, ref: n.ref || null, _outCursor: 0, _inCursor: 0 };
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
    const sx = s.x + s.w;
    const tx = t.x;
    const sy = s.y + s._outCursor + sBand / 2;
    const ty = t.y + t._inCursor + tBand / 2;
    s._outCursor += sBand;
    t._inCursor += tBand;
    linksOut.push({ id: l.id || `${l.source}__${l.target}`, source: l.source, target: l.target, sx, sy, tx, ty, hwS: Math.max(0.6, sBand / 2), hwT: Math.max(0.6, tBand / 2), value: l.value, cls: l.cls || '' });
  }
  const box = { x: 0, y: 0, w: totalW, h: colHeight + top * 2 };
  return { nodes: nodesOut, links: linksOut, box };
}

// Render a fit-to-width Tufte Sankey to an <svg>. Reads the layout above.
// opts: same as layoutSankey + { onNode }.
export function sankey(opts) {
  const o = opts || {};
  const { nodes, links, box } = layoutSankey(o);
  const svg = svgEl('svg', {
    class: 'dn-sankey', width: '100%', height: box.h,
    viewBox: `0 0 ${box.w} ${box.h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
  });
  if (nodes.length === 0) {
    const t = svgEl('text', { x: box.w / 2, y: box.h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no causal flow yet';
    svg.appendChild(t);
    return svg;
  }
  const stageHead = { patch: 'PATCH', drift: 'PER-BOARD DRIFT', gate: 'GATE' };
  const byStage = {};
  for (const n of nodes) (byStage[n.stage] = byStage[n.stage] || []).push(n);
  for (const stage of Object.keys(byStage)) {
    const x = byStage[stage][0].x + byStage[stage][0].w / 2;
    const t = svgEl('text', { x, y: 14, class: 'dn-sankey-head', 'text-anchor': 'middle' });
    t.textContent = stageHead[stage] || stage;
    svg.appendChild(t);
  }
  // ribbons (drawn first, behind nodes) — thin filled paths.
  const linkLayer = svgEl('g', { class: 'dn-sankey-links' });
  for (const l of links) {
    const mx = (l.sx + l.tx) / 2;
    const d = `M ${l.sx} ${l.sy - l.hwS} `
      + `C ${mx} ${l.sy - l.hwS}, ${mx} ${l.ty - l.hwT}, ${l.tx} ${l.ty - l.hwT} `
      + `L ${l.tx} ${l.ty + l.hwT} `
      + `C ${mx} ${l.ty + l.hwT}, ${mx} ${l.sy + l.hwS}, ${l.sx} ${l.sy + l.hwS} Z`;
    linkLayer.appendChild(hov(svgEl('path', { d, class: 'dn-sankey-ribbon ' + (l.cls || ''), fill: 'currentColor' }), `${l.source} → ${l.target}: ${fmt(l.value, 1)}`));
  }
  svg.appendChild(linkLayer);
  // nodes — thin bars + direct in-place labels. FIX #5: the per-board node's
  // LABEL and its loss VALUE must never overlap. The label sits on the top
  // baseline (truncated short so it cannot run under the value); the loss value
  // "picky_stakeholder_emu…" and its "642" can never collide.
  const nodeLayer = svgEl('g', { class: 'dn-sankey-nodes' });
  for (const n of nodes) {
    const g = svgEl('g', { class: 'dn-sankey-node ' + (n.cls || ''), tabindex: o.onNode ? '0' : null });
    g.appendChild(hov(svgEl('rect', { x: n.x, y: n.y, width: 6, height: n.h, rx: 1, class: 'dn-sankey-bar' }), `${n.label}${isNum(n.value) ? ' · ' + fmt(n.value, 1) : ''}`));
    const anchor = n.stage === 'gate' ? 'end' : 'start';
    const lx = n.stage === 'gate' ? n.x - 6 : n.x + 12;
    const ty = n.y + n.h / 2;
    // Drift (middle) nodes carry a numeric loss value; reserve room for it by
    // truncating the label harder, and right-align the value to the node's far
    // edge so the two strings sit on the same baseline without overlapping.
    const hasValue = n.stage === 'drift' && isNum(n.value);
    const lbl = svgEl('text', { x: lx, y: ty - 1, class: 'dn-sankey-label', 'text-anchor': anchor });
    lbl.textContent = shortLabel(String(n.label), hasValue ? 16 : 22);
    g.appendChild(lbl);
    if (hasValue) {
      const vx = n.x + n.w; // far (right) edge of this column's node band
      const val = svgEl('text', { x: vx, y: ty - 1, class: 'dn-sankey-value', 'text-anchor': 'end' });
      val.textContent = fmt(n.value, 0);
      g.appendChild(val);
    }
    if (n.sub) {
      const sub = svgEl('text', { x: lx, y: ty + 11, class: 'dn-sankey-sub', 'text-anchor': anchor });
      sub.textContent = shortLabel(String(n.sub), 24);
      g.appendChild(sub);
    }
    clickable(g, o.onNode && (() => o.onNode(n)));
    nodeLayer.appendChild(g);
  }
  svg.appendChild(nodeLayer);
  return svg;
}

// ---- small-multiple wrapper -----------------------------------------

export function smallMultiple(caption, mark, sub) {
  return el('figure', { class: 'dn-sm' }, [
    el('figcaption', { class: 'dn-sm-cap' }, [
      el('span', { class: 'dn-sm-title', text: caption == null ? '' : String(caption) }),
      sub ? el('span', { class: 'dn-sm-sub', text: String(sub) }) : null,
    ]),
    mark,
  ]);
}

// ---- SIDE-BY-SIDE line diff (champion baseline | challenger new) ----
// opts: { baseline: string, challenger: string, leftLabel, rightLabel }
export function sideBySideDiff(opts) {
  const o = opts || {};
  const leftText = o.baseline == null ? '' : String(o.baseline);
  const rightText = o.challenger == null ? '' : String(o.challenger);
  const a = leftText.replace(/\r\n/g, '\n').split('\n');
  const b = rightText.replace(/\r\n/g, '\n').split('\n');
  const rows = lcsDiff(a, b);

  const wrap = el('div', { class: 'dn-sxs' });
  const head = el('div', { class: 'dn-sxs-head' }, [
    el('span', { class: 'dn-sxs-col-h dn-sxs-old', text: o.leftLabel || 'champion baseline' }),
    el('span', { class: 'dn-sxs-col-h dn-sxs-new', text: o.rightLabel || 'challenger new' }),
  ]);
  wrap.appendChild(head);

  const body = el('div', { class: 'dn-sxs-body', role: 'list' });
  let ln = 0; let rn = 0;
  for (const r of rows) {
    const cls = r.type === 'same' ? '' : (r.type === 'del' ? ' dn-sxs-changed' : (r.type === 'add' ? ' dn-sxs-changed' : ' dn-sxs-changed'));
    const lhsText = r.left != null ? r.left : '';
    const rhsText = r.right != null ? r.right : '';
    const lGutter = r.left != null ? String(++ln) : '';
    const rGutter = r.right != null ? String(++rn) : '';
    body.appendChild(el('div', { class: 'dn-sxs-row' + cls, role: 'listitem' }, [
      el('span', { class: 'dn-sxs-gutter', 'aria-hidden': 'true', text: lGutter }),
      el('span', { class: 'dn-sxs-cell dn-sxs-old' + (r.type === 'del' || r.type === 'mod' ? ' dn-sxs-del' : ''), text: r.left == null ? '' : (lhsText === '' ? '​' : lhsText) }),
      el('span', { class: 'dn-sxs-gutter', 'aria-hidden': 'true', text: rGutter }),
      el('span', { class: 'dn-sxs-cell dn-sxs-new' + (r.type === 'add' || r.type === 'mod' ? ' dn-sxs-add' : ''), text: r.right == null ? '' : (rhsText === '' ? '​' : rhsText) }),
    ]));
  }
  wrap.appendChild(body);
  return wrap;
}

// A compact LCS line-diff → aligned rows: {type:'same'|'mod'|'del'|'add',
// left, right}. 'mod' pairs a deleted line with an added line on the same row.
function lcsDiff(a, b) {
  const n = a.length; const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0; let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { rows.push({ type: 'same', left: a[i], right: b[j] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { rows.push({ type: 'del', left: a[i], right: null }); i++; }
    else { rows.push({ type: 'add', left: null, right: b[j] }); j++; }
  }
  while (i < n) { rows.push({ type: 'del', left: a[i], right: null }); i++; }
  while (j < m) { rows.push({ type: 'add', left: null, right: b[j] }); j++; }
  // Coalesce an adjacent del+add into a single 'mod' row so a one-line edit
  // reads as a side-by-side replacement rather than two stacked rows.
  const out = [];
  for (let k = 0; k < rows.length; k++) {
    const cur = rows[k]; const nxt = rows[k + 1];
    if (cur.type === 'del' && nxt && nxt.type === 'add') {
      out.push({ type: 'mod', left: cur.left, right: nxt.right }); k++;
    } else out.push(cur);
  }
  return out;
}

// ---- proposing-step tracker -----------------------------------------
//
// The candidate-generation step rendered as the field FORMS: one row per
// `onCompetitor(gid)` (optional) makes an applied row a drill-in affordance.
export function proposingTracker(opts) {
  const o = opts || {};
  const list = (Array.isArray(o.fieldStatus) ? o.fieldStatus : []).filter((f) => f && f.generation_id);
  const applied = list.filter((f) => f.status === 'applied').length;
  const proposed = list.length;
  const allRejected = proposed > 0 && applied === 0;
  const onCompetitor = typeof o.onCompetitor === 'function' ? o.onCompetitor : null;

  // headline counts — never an empty/idle read for a field that minted rows.
  let head;
  if (proposed === 0) {
    head = 'minting the field…';
  } else {
    head = `${proposed} proposed · ${applied} applied`;
    if (allRejected) head += ' — all rejected';
  }

  const rows = list.map((f) => {
    const ok = f.status === 'applied';
    const glyph = el('span', {
      class: 'dn-prop-glyph ' + (ok ? 'dn-prop-ok' : 'dn-prop-bad'),
      'aria-hidden': 'true', text: ok ? '✓' : '✗',
    });
    const gid = el('span', { class: 'dn-prop-gen', text: shortLabel(String(f.generation_id), 16) });
    const verdict = el('span', {
      class: 'dn-prop-verdict ' + (ok ? 'dn-prop-ok' : 'dn-prop-bad'),
      text: ok ? 'applied' : 'rejected',
    });
    const reasonText = ok
      ? `${f.generation_id} applied cleanly`
      : `${f.generation_id} rejected: ${f.reason || 'no reason recorded'}`;
    const row = el('div', {
      class: 'dn-prop-row ' + (ok ? 'dn-prop-row-ok' : 'dn-prop-row-bad'),
      role: 'listitem',
    }, [glyph, gid, verdict]);
    // The reason lives on the existing hovercard (inline-elided rows stay tidy).
    hov(row, reasonText);
    if (ok && onCompetitor) {
      row.classList.add('dn-prop-clickable');
      row.tabIndex = 0;
      row.addEventListener('click', () => onCompetitor(String(f.generation_id)));
      row.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onCompetitor(String(f.generation_id)); }
      });
    }
    return row;
  });

  const headNode = el('div', {
    class: 'dn-prop-head' + (allRejected ? ' dn-prop-head-allbad' : ''),
    text: head,
  });
  const listNode = el('div', { class: 'dn-prop-list', role: 'list' }, rows);
  return el('div', { class: 'dn-prop-tracker', role: 'group', 'aria-label': 'Proposed field' }, [
    el('div', { class: 'dn-prop-caption dn-faint', text: 'proposed field' }),
    headNode,
    listNode,
  ]);
}

// A stable digest of the proposing-step field so the live hero can
// digest-gate the tracker swap (a no-op heartbeat writes ZERO DOM).
export function proposingDigest(fieldStatus) {
  const list = Array.isArray(fieldStatus) ? fieldStatus : [];
  return 'prop|' + list.map((f) => (f && f.generation_id) + ':' + (f && f.status)).join(',');
}
