// variants/T/svg.js — dependency-free SVG data-viz primitives (Console).
//
// Self-contained for Variant N ("Console II"). Mark CSS classes are `dn-*` and
//     themed in one place, swapped by the [data-n-theme] attribute.

import { svgEl, el } from '../../core/dom.js';
import { attachHovercard } from './hovercard.js';

export const NS = 'http://www.w3.org/2000/svg';

// ── CROWN GLYPHS — the SINGLE source of truth (CONSOLE-IV §9) ─────────
//
// The rule, defined ONCE so it cannot drift across files again:
//   CROWN.current — the CURRENT champion (the crowned survivor of the gate;
//                   the last id in champion_lineage). Solid crown.
//   CROWN.former  — a FORMER champion (the displaced incumbent) OR a transient
//                   round-leader before the gate decides. Hollow crown.
// A just-crowned gate winner IS the current champion, so gate labels use
// CROWN.current too (the historical `♚` mix is retired). Every file that emits
// a crown imports from here.
export const CROWN = { current: '♛', former: '♔' };

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
      // outcomeGlyph() returns a fixed 1:1-aspect <svg> sized `gsz`; position it
      // at the row's right edge via the nested-svg x/y attrs (NOT by passing the
      // chart x-coordinate as the size — that blew each glyph up to ~chart width).
      const gsz = glyphW - 4;
      const gl = outcomeGlyph(d, gsz);
      gl.setAttribute('x', w - glyphW + 2);
      gl.setAttribute('y', cy - gsz / 2);
      g.appendChild(gl);
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
  // The BARS layer stretches non-uniformly to fill the cell width (bars are
  // rectangles — stretching them is fine). The verdict GLYPH must stay a true
  // triangle, so it rides in a SEPARATE fixed-aspect overlay (see below), NOT
  // inside this `preserveAspectRatio:'none'` viewBox.
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
  if (o.verdict !== 'promoted' && o.verdict !== 'rejected') return svg;

  // The verdict triangle as a FIXED-ASPECT (1:1 viewBox) overlay so it renders
  // as a true triangle — never sheared by the bars' non-uniform width stretch.
  // The bars SVG + the glyph SVG share an HTML positioning wrapper; the glyph
  // pins to the top-right corner (where it sat inside the old stretched viewBox).
  const good = o.verdict === 'promoted';
  const r = 3.2;
  const tri = good ? `5,${5 - r} ${5 - r},${5 + r} ${5 + r},${5 + r}` : `5,${5 + r} ${5 - r},${5 - r} ${5 + r},${5 - r}`;
  const gsz = 12;
  const glyph = svgEl('svg', { class: 'dn-sparkbar-verdict', width: gsz, height: gsz, viewBox: '0 0 10 10', preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  glyph.appendChild(svgEl('polygon', { points: tri, class: 'dn-verdict-glyph ' + (good ? 'dn-good' : 'dn-bad') }));
  hov(glyph, o.verdict);
  return el('div', { class: 'dn-sparkbar-wrap' }, [svg, glyph]);
}

// A row of pass/fail/timeout glyphs — PROPORTIONAL (true circles, no oval
// distortion). The round status marks must NOT inherit the trellis cell's
// non-uniform width stretch, so each glyph is a FIXED 1:1-aspect SVG laid out
// in an HTML flex row (one equal-flex cell per candidate, glyphs aligned under
// their bars). The row still spans the full cell width; only the inner glyphs
// keep their aspect, so a ✓/✕/⏱/○ renders round, never elliptical.
export function genDots(opts) {
  const o = opts || {};
  const cells = Array.isArray(o.cells) ? o.cells : [];
  const h = o.height || 14;
  const row = el('div', { class: 'dn-genrow', role: 'img' });
  // a fixed mark side so the 1:1 viewBox never stretches with the cell width;
  // capped by the row height so dense rows stay compact.
  const mark = Math.max(8, Math.min(h, 14));
  for (const c of cells) {
    const slot = el('span', { class: 'dn-genrow-slot' });
    slot.appendChild(outcomeGlyph(c, mark));
    row.appendChild(slot);
  }
  if (!cells.length) row.appendChild(el('span', { class: 'dn-genrow-slot' }));
  return row;
}

// One fixed-aspect (1:1 viewBox) glyph SVG, so the mark renders as a TRUE
// circle / square-cornered cross regardless of the parent's width stretch.
function outcomeGlyph(d, side) {
  const s = side || 14;
  const svg = svgEl('svg', { class: 'dn-glyph', width: s, height: s, viewBox: '0 0 10 10', preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  const cx = 5, cy = 5;
  if (d && d.ran === false) { svg.appendChild(svgEl('circle', { cx, cy, r: 2.2, class: 'dn-glyph-none' })); return hov(svg, 'no run'); }
  if (d && d.timeout) { svg.appendChild(svgEl('text', { x: cx, y: cy + 3.2, class: 'dn-glyph-timeout', 'text-anchor': 'middle' }, ['⏱'])); return hov(svg, 'budget exceeded (timeout)'); }
  if (d && (d.pass === true || d.pass === 1)) { svg.appendChild(svgEl('circle', { cx, cy, r: 2.6, class: 'dn-glyph-pass' })); return hov(svg, 'passed'); }
  if (d && (d.pass === false || d.pass === 0)) {
    svg.appendChild(svgEl('line', { x1: cx - 2.6, y1: cy - 2.6, x2: cx + 2.6, y2: cy + 2.6, class: 'dn-glyph-fail' }));
    svg.appendChild(svgEl('line', { x1: cx - 2.6, y1: cy + 2.6, x2: cx + 2.6, y2: cy - 2.6, class: 'dn-glyph-fail' }));
    return hov(svg, 'failed');
  }
  svg.appendChild(svgEl('circle', { cx, cy, r: 2.2, class: 'dn-glyph-none' }));
  return hov(svg, 'no predicate');
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
    // a LIVE racing rung carries a per-lane `live_progress` map; an active
    // (not queued) lane reads "racing · k/N boards" + a partial Δ-vs-champion
    // and grows a thin in-flight progress bar as boards land.
    const prog = (rung.live_progress && typeof rung.live_progress === 'object') ? rung.live_progress : null;
    const survRunners = pending ? comps.map(String) : surv;
    survRunners.forEach((sid, i) => {
      const cy = survRunners.length === 1 ? midY
        : midY - hOut + 8 + (i * (Math.max(1, 2 * hOut - 16)) / Math.max(1, survRunners.length - 1));
      const lane = prog ? prog[String(sid)] : null;
      funnelRunner(svg, o, sid, rung, j, x0 + 8, cy, pending ? 'racing' : 'survives', lane, stageW - 16);
    });

    // ── eliminated competitors peel off as labelled dead-end branches (✕) ──
    if (!pending) {
      [...cut].forEach((cid, i) => {
        const sid = String(cid);
        const branchY = top + laneH + 6 + i * deadH;
        const elbowX = x0 + stageW * 0.5;
        // anchor each branch ON the band's lower edge at the elbow x so it peels
        // off the funnel with no gap. The lower edge runs from (x0, midY+hIn) to
        // (x1, midY+hOut); at fraction f along the stage its y is interpolated.
        const f = (elbowX - x0) / stageW;
        const edgeYAtElbow = midY + hIn + (hOut - hIn) * f;
        const labelX = elbowX + 12;
        // a dead-end branch that drops from the band's lower edge and then a SHORT
        // stub that stops just LEFT of the label — the connector must lead INTO
        // the cut name, never run through it (it used to extend the full stage
        // width at the label's own baseline, slashing across the text).
        svg.appendChild(svgEl('path', {
          d: `M${elbowX},${edgeYAtElbow} V${branchY} H${labelX - 4}`,
          class: 'dn-funnel-deadedge', fill: 'none',
        }));
        funnelRunner(svg, o, sid, rung, j, labelX, branchY, 'cut');
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
    label = CROWN.current + ' ' + shortLabel(champId, 12);
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
// its candidate. `verdict` ∈ {survives, cut, racing}. A LIVE racing lane passes
// its `lane` ({inflight, done, total, partialDelta}) so the runner reads
// "racing · k/N boards" + a partial Δ and grows an in-flight progress bar
// (`barW` is the band-bounded bar width); `lane` is null for settled/non-live.
function funnelRunner(svg, o, sid, rung, j, x, cy, verdict, lane, barW) {
  // partial Δ-vs-champion (live) falls back to the committed rung Δ.
  const partial = lane && isNum(lane.partialDelta) ? lane.partialDelta : null;
  const delta = (rung.deltas && isNum(rung.deltas[sid])) ? rung.deltas[sid] : partial;
  const glyph = verdict === 'cut' ? ' ✕' : verdict === 'survives' ? ' ↑' : '';
  // a LIVE racing lane with a server-side PROJECTED standing reads as projected:
  // a ~prefix on the scalar + a "proj" suffix + the dashed/dimmed dn-proj
  // treatment + a SCORED board-progress sub-bar, distinct from a settled lane.
  const projected = !!(lane && lane.projected && verdict === 'racing');
  // a live racing lane appends its "k/N boards" progress to the label.
  const laneSuffix = (verdict === 'racing' && lane) ? ' · ' + laneProgressText(lane) : '';
  const projSuffix = projected && isNum(lane.projected_scalar)
    ? ' · ~' + fmt(lane.projected_scalar, 1) + ' proj' : '';
  const cls = 'dn-funnel-name'
    + (verdict === 'cut' ? ' dn-out dn-bad' : verdict === 'survives' ? ' dn-good' : ' dn-racing')
    + (projected ? ' dn-proj' : '');
  const tip = `${sid} · ${rung.label || 'rung ' + j}`
    + (isNum(rung.board_fraction) ? ` · ${(rung.board_fraction * 100).toFixed(0)}% board` : '')
    + (projected && isNum(lane.projected_scalar) ? ` · projected scalar ~${fmt(lane.projected_scalar, 2)} (boards still streaming)` : '')
    + (delta != null ? ` · Δ ${fmtSigned(delta, 2)} vs champion` : '')
    + (laneSuffix ? ` · ${laneProgressText(lane)}` : '')
    + ` · ${projected ? 'projected' : verdict}`;
  const g = svgEl('g', { class: 'dn-funnel-runner', tabindex: o.onCompetitor ? '0' : null });
  const t = hov(svgEl('text', { x, y: cy + 3, class: cls }), tip);
  t.textContent = shortLabel(sid, lane ? 8 : 13) + glyph + laneSuffix + projSuffix;
  g.appendChild(t);
  // a thin SCORED board-progress sub-bar under a live lane (boards done / total).
  // A projected lane draws it in the projected (dashed/amber) treatment; a plain
  // live lane keeps the accent in-flight bar.
  if (lane && (lane.inflight || lane.done || projected)) {
    const bw = Math.max(20, barW || 80);
    // prefer the scored boards_done/boards_total when present (the projected
    // standing's own progress); else the live activeRuns done/total tally.
    const sd = isNum(lane.boards_done) ? lane.boards_done : lane.done;
    const stot = isNum(lane.boards_total) ? lane.boards_total : lane.total;
    const frac = (isNum(stot) && stot > 0)
      ? Math.min(1, (sd || 0) / stot)
      : (lane.inflight ? 0.5 : 0);
    if (projected) {
      g.appendChild(svgEl('rect', { x, y: cy + 5, width: bw, height: 2.4, rx: 1, class: 'dn-proj-bar-bg' }));
      g.appendChild(svgEl('rect', { x, y: cy + 5, width: Math.max(1, bw * frac), height: 2.4, rx: 1, class: 'dn-proj-bar' }));
    } else {
      g.appendChild(svgEl('rect', { x, y: cy + 5, width: bw, height: 2, rx: 1, class: 'dn-funnel-bar-bg' }));
      g.appendChild(svgEl('rect', { x, y: cy + 5, width: Math.max(1, bw * frac), height: 2, rx: 1,
        class: 'dn-funnel-bar' + (lane.inflight ? ' dn-funnel-bar-live' : '') }));
    }
  }
  clickable(g, o.onCompetitor && (() => o.onCompetitor(sid)));
  svg.appendChild(g);
}

// ---- swiss STANDINGS LADDER (DATA-DRIVEN, live + completed) ----------
//
// The swiss analogue of the racing survivalFunnel: a column per round (its
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
        : inflight ? ' · running' + (isNum(p.total) && p.total > 0 ? ` ${p.done || 0}/${p.total}` : (p.inflight ? ` · ${p.inflight} board${p.inflight === 1 ? '' : 's'}` : ''))
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
  // distinguish the NEW champion (♛, accent) from the displaced incumbent
  // (♔ "former", dim). A bare round-leader gets ♔ only while no champion is
  // crowned yet (live).
  const ladChampId = o.championId ? String(o.championId) : null;
  const ladBenchId = o.benchmarkId != null ? String(o.benchmarkId) : null;
  const ladFormerId = (ladChampId && ladBenchId && ladBenchId !== ladChampId) ? ladBenchId : null;
  standings.forEach((s, i) => {
    const cy = rowY(i);
    const sid = String(s.id);
    const isChamp = sid === ladChampId;
    const isFormer = sid === ladFormerId;
    const isLeader = sid === leaderId && !ladChampId;
    const emph = isChamp || isLeader;
    // PROJECTED — an in-flight competitor's mean-scalar is projected (Copeland
    // points are NOT — a half-finished duel has crowned no winner). Mark the
    // row "projected" (dashed/~) but never re-rank it on the projection.
    const proj = !!(s.in_flight && isNum(s.projected_scalar));
    const g = svgEl('g', { class: 'dn-swissladder-stand' + (proj ? ' dn-proj' : ''), tabindex: o.onCompetitor ? '0' : null });
    const lab = hov(svgEl('text', { x: sx + 6, y: cy + 3, class: 'dn-swissladder-standlab' + (emph ? ' dn-good' : (isFormer ? ' dn-faint' : '')) + (proj ? ' dn-proj' : '') }),
      `${sid} · ${isNum(s.points) ? fmt(s.points, 1) : '?'} pts · ${s.wins || 0}W ${s.draws || 0}D ${s.losses || 0}L${isFormer ? ' · former champion' : ''}${proj ? ` · projected scalar ~${fmt(s.projected_scalar, 2)} (boards streaming; points not projected)` : ''}`);
    lab.textContent = `${i + 1}. ${shortLabel(sid, 9)}` + (isChamp ? ' ' + CROWN.current : (isFormer || isLeader ? ' ' + CROWN.former : '')) + (proj ? ' ~proj' : '');
    g.appendChild(lab);
    const pts = svgEl('text', { x: sx + standW - 6, y: cy + 3, 'text-anchor': 'end', class: 'dn-swissladder-pts' + (emph ? ' dn-good' : '') });
    pts.textContent = isNum(s.points) ? fmt(s.points, s.points % 1 ? 1 : 0) : '—';
    g.appendChild(pts);
    // a SCORED board-progress sub-bar for a projected row (boards_done/total).
    if (proj && isNum(s.boards_total) && s.boards_total > 0) {
      const barW = standW - 12;
      const frac = Math.min(1, (s.boards_done || 0) / s.boards_total);
      g.appendChild(svgEl('rect', { x: sx + 6, y: cy + 7, width: barW, height: 2.4, rx: 1, class: 'dn-proj-bar-bg' }));
      g.appendChild(svgEl('rect', { x: sx + 6, y: cy + 7, width: Math.max(1, barW * frac), height: 2.4, rx: 1, class: 'dn-proj-bar' }));
    }
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
  if (crowned) { label = CROWN.current + ' ' + shortLabel(champId, 11); tip = `${champId} won the swiss + cleared the gate → new champion${dStr}`; }
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

// ---- elim FLOW (Tufte slopegraph / bipartite — generations across rounds) --
//
// The COMPANION to the bracket tree on the generations-overview page (Task 3):
// the elimination analogue of the racing survival funnel. The tree shows
// who-played-whom; this shows each generation's SURVIVAL TRAJECTORY through the
// rounds.
//
//   * ROUNDS are columns (R0 · R1 · … · champion-gate).
//   * ONE LANE per generation (a horizontal row).
//   * a generation's line CONTINUES to the next column when it WON (advanced),
//     drawn with --v2-good; it TERMINATES with ✕ (--v2-bad) when ELIMINATED.
//   * the champion's line reaches the gate marked with CROWN.current; the
//     displaced incumbent (benchmark) reads CROWN.former.
//   * a still-pending (live) leg is drawn dashed (the pending convention).
//
// Derived PURELY from elimModel(st)'s winners rounds + competitors (single
// source — no new data path). `opts`:
//   { winners:[{label, matches:[{competitors, winner, decision, pending, bye}]}],
//     championId, benchmarkId, gateState, live, onCompetitor(id) }
export function elimFlow(opts) {
  const o = opts || {};
  // COLUMN ORDER must be TEMPORAL (by round_index), not the caller's band
  // concatenation order. The double-elim caller passes winners.concat(losers),
  // which lists the GRAND FINAL (a winners' band) BEFORE the losers' bracket
  // rounds — so the losers' columns rendered to the RIGHT of the gate-bound
  // final, the winners→losers DROP edges pointed backwards, and a dropped lane's
  // dots were left orphaned. Sorting by round_index restores WB → LB → GF order
  // so every advancement / drop edge runs left-to-right into its real target.
  const rounds = (Array.isArray(o.winners) ? o.winners : [])
    .filter((r) => r && Array.isArray(r.matches))
    .map((r, i) => ({ r, i }))
    .sort((a, b) => {
      const ra = isNum(a.r.round_index) ? a.r.round_index : a.i;
      const rb = isNum(b.r.round_index) ? b.r.round_index : b.i;
      return ra - rb || a.i - b.i;
    })
    .map((x) => x.r);
  const live = !!o.live;
  const champId = o.championId != null ? String(o.championId) : null;
  const benchId = o.benchmarkId != null ? String(o.benchmarkId) : null;

  // ── derive each generation's per-round state from the winners rounds ──
  // For each round we record, per competitor that PLAYED in it: advanced (won),
  // eliminated (lost a decided match), or pending (the match is still in flight).
  // R = rounds.length columns + 1 gate column.
  const nCols = rounds.length;
  // gen id → { firstCol, lastCol, eliminatedAt, advancedThrough:Set, pendingAt:Set }
  const genState = new Map();
  const ensure = (id) => {
    const k = String(id);
    if (!genState.has(k)) genState.set(k, { id: k, played: new Set(), advanced: new Set(), lostAt: new Set(), eliminatedAt: null, pendingAt: new Set() });
    return genState.get(k);
  };
  // the per-round MATCHES (a two-lane convergence each): two competitors meet, the
  // winner's lane continues, the loser's terminates. Captured here so the figure
  // can draw the bracket-as-flow convergence node + carry the pairing onto HOVER.
  const matchesByCol = rounds.map(() => []);
  rounds.forEach((r, ci) => {
    for (const m of (Array.isArray(r.matches) ? r.matches : [])) {
      const comps = (Array.isArray(m.competitors) ? m.competitors : []).map(String).filter((c) => c && c !== 'tbd');
      const winner = m.winner ? String(m.winner) : null;
      const pending = !!m.pending || (!winner && !m.bye && !m.decision);
      // a real two-lane convergence (not a bye / placeholder) is recorded for the
      // match-node layer; a winner+loser pair, with the live state per leg.
      if (comps.length >= 2 && !m.bye) {
        const loser = winner ? comps.find((c) => c !== winner) || null : null;
        matchesByCol[ci].push({ comps, winner, loser, pending, delta: isNum(m.delta_scalar) ? m.delta_scalar : null,
          slot: m.bracket_slot || m.match_id || '',
          // the per-side live PROJECTED standing on an in-flight match.
          projected: (m.projected && typeof m.projected === 'object') ? m.projected : null });
      }
      for (const c of comps) {
        const g = ensure(c);
        g.played.add(ci);
        if (pending) { g.pendingAt.add(ci); continue; }
        if (m.bye) { g.advanced.add(ci); continue; }
        if (winner && c === winner) g.advanced.add(ci);
        else if (winner) g.lostAt.add(ci);   // a decided loss in THIS column
      }
    }
  });
  // ELIMINATION vs DROP (double-elim correctness): a generation is ELIMINATED at
  // a column only when it lost there AND never plays again in a LATER column. An
  // earlier loss that is followed by a later appearance is a winners→losers DROP
  // (the "second life"), not a termination — so it must keep its lane, connect to
  // its losers'-bracket entry by a drop edge, and NOT draw a phantom ✕ in the WB.
  // A single-elim loss has no later column, so it stays a true elimination.
  for (const g of genState.values()) {
    const lost = [...g.lostAt].sort((a, b) => a - b);
    const lastPlayed = g.played.size ? Math.max(...g.played) : -1;
    for (const ci of lost) {
      if (ci >= lastPlayed) { g.eliminatedAt = ci; break; }  // no later column → eliminated here
    }
  }
  // per-generation live PROJECTED standing (from an in-flight match's
  // `projected` map): the latest column's projected row wins. Drives the
  // lane's "projected" treatment (dashed/~prefix) + scored sub-bar.
  const projByGen = new Map();
  matchesByCol.forEach((matches) => {
    for (const m of matches) {
      if (!m.projected || !m.pending) continue;
      for (const c of m.comps) {
        const p = m.projected[c];
        if (p && isNum(p.scalar)) projByGen.set(String(c), p);
      }
    }
  });
  const gens = [...genState.values()];
  // order lanes: survivors / champion first (by deepest round reached), then the
  // earlier-eliminated; the champion lane floats to the top.
  const reach = (g) => (g.eliminatedAt == null ? nCols + 1 : g.eliminatedAt);
  gens.sort((a, b) => reach(b) - reach(a)
    || (a.id === champId ? -1 : b.id === champId ? 1 : 0)
    || a.id.localeCompare(b.id));
  // lane index per generation id — so a match can draw a convergence between the
  // two competitors' lanes (winner above/below the loser, whichever order).
  const laneOf = new Map();
  gens.forEach((g, li) => laneOf.set(g.id, li));

  // ── geometry: columns × lanes, fit-to-width ──
  const colW = 116;
  const padL = 16;
  const padR = 116;          // gutter for the lane labels + gate marks
  const top = 30;
  const laneH = 22;
  const w = padL + Math.max(1, nCols) * colW + padR + 8;
  const h = top + Math.max(1, gens.length) * laneH + 18;
  const svg = svgEl('svg', {
    class: 'dn-elimflow', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
  });
  if (!nCols || !gens.length) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no bracket rounds yet';
    svg.appendChild(t);
    return svg;
  }
  const colX = (ci) => padL + ci * colW + 8;       // a round column's node x
  const gateX = padL + nCols * colW + 8;           // the champion-gate column x
  const laneY = (li) => top + li * laneH + laneH / 2;

  // round-axis headers (R0 · R1 · … · champion-gate).
  rounds.forEach((r, ci) => {
    const hx = colX(ci);
    const head = svgEl('text', { x: hx, y: top - 12, class: 'dn-elimflow-col', 'text-anchor': 'middle' });
    head.textContent = shortLabel(r.label || `R${ci}`, 12);
    svg.appendChild(head);
  });
  const gateHead = svgEl('text', { x: gateX, y: top - 12, class: 'dn-elimflow-col', 'text-anchor': 'middle' });
  gateHead.textContent = 'champion-gate';
  svg.appendChild(gateHead);

  // ── the MATCH CONVERGENCES (bracket-as-flow): at each round column the two
  // competitors' lanes meet at a match node — a short bracket joining the two
  // lane-ys to the node x. The winner's lane continues (good); the loser's
  // terminates (✕, drawn on its lane below). The pairing + Δ live on HOVER. ──
  matchesByCol.forEach((matches, ci) => {
    const x = colX(ci);
    for (const m of matches) {
      const lys = m.comps.map((c) => laneOf.has(c) ? laneY(laneOf.get(c)) : null).filter((v) => v != null);
      if (lys.length < 2) continue;
      const yTop = Math.min(...lys);
      const yBot = Math.max(...lys);
      const ymid = (yTop + yBot) / 2;
      // a projected (in-flight, with a server-side projected scalar) match draws
      // the convergence in the projected (dashed/amber) treatment.
      const projMatch = !!(m.pending && m.projected
        && m.comps.some((c) => m.projected[c] && isNum(m.projected[c].scalar)));
      // a small convergence elbow: the two lanes pinch toward the node at x.
      const cls = 'dn-elimflow-conv' + (m.pending ? ' dn-elimflow-conv-pending' : '') + (projMatch ? ' dn-proj' : '');
      svg.appendChild(svgEl('path', {
        d: `M${x - 8},${yTop} Q${x},${yTop} ${x},${ymid} Q${x},${yBot} ${x - 8},${yBot}`,
        class: cls, fill: 'none',
      }));
      const projTip = projMatch
        ? ' · projected: ' + m.comps.filter((c) => m.projected[c] && isNum(m.projected[c].scalar))
            .map((c) => `${shortLabel(c, 8)} ~${fmt(m.projected[c].scalar, 2)}`).join(', ')
        : '';
      const tip = `${m.slot ? m.slot + ': ' : ''}${m.comps.join(' vs ')}`
        + (m.winner ? ` → ${m.winner} ↑` : m.pending ? (projMatch ? ' · projected (boards streaming)' : ' · racing') : '')
        + (m.delta != null ? ` · Δ ${fmtSigned(m.delta, 2)}` : '') + projTip;
      const node = svgEl('circle', { cx: x, cy: ymid, r: m.pending ? 2.6 : 3,
        class: 'dn-elimflow-convnode' + (m.pending ? ' dn-elimflow-pending' : m.winner ? ' dn-elimflow-good' : '') + (projMatch ? ' dn-proj' : '') });
      svg.appendChild(hov(node, tip));
    }
  });

  // ── one lane per generation: dots at each round it played, a segment to the
  // next column when it advanced, a ✕ where it was cut, the crown at the gate ──
  gens.forEach((g, li) => {
    const y = laneY(li);
    const isChamp = champId != null && g.id === champId;
    const isFormer = benchId != null && g.id === benchId && !isChamp;
    const lane = svgEl('g', { class: 'dn-elimflow-lane', tabindex: o.onCompetitor ? '0' : null });

    // the lane's played columns, sorted.
    const cols = [...g.played].sort((a, b) => a - b);
    for (const ci of cols) {
      const x = colX(ci);
      const advanced = g.advanced.has(ci);
      const pending = g.pendingAt.has(ci);
      const eliminated = g.eliminatedAt === ci;
      // a DROP: lost this column but plays again later (winners→losers second
      // life) — not a terminal cut, not pending; its dot reads as a loss and a
      // drop edge carries the lane into its next (losers'-bracket) column.
      const dropped = g.lostAt.has(ci) && !eliminated;
      // the node dot at this round.
      const dotCls = 'dn-elimflow-dot ' + (eliminated || dropped ? 'dn-elimflow-bad' : advanced ? 'dn-elimflow-good' : 'dn-elimflow-pending');
      lane.appendChild(hov(svgEl('circle', { cx: x, cy: y, r: 2.8, class: dotCls }),
        `${g.id} · ${rounds[ci] ? (rounds[ci].label || 'R' + ci) : 'R' + ci} · ${eliminated ? 'eliminated' : dropped ? 'lost → losers’ bracket' : advanced ? 'advanced' : 'racing'}`));
      // a segment to the NEXT column the lane plays (a later round, or the gate)
      // whenever the lane CONTINUES: it advanced, it is racing, OR it dropped to
      // the losers' bracket. Without the drop case the dropped lane's WB dot was
      // orphaned from its LB entry, so the bracket "couldn't tell what connects".
      if (advanced || pending || dropped) {
        const nextCi = cols.find((c) => c > ci);
        // a lane reaches the GATE from the last column only when it WON / is still
        // racing there (advanced or pending) — never on a drop (a dropped lane
        // always has a later played column, so it never falls through to here).
        const toX = (nextCi != null) ? colX(nextCi)
          : ((advanced || pending) && ci === nCols - 1 ? gateX : null);
        if (toX != null) {
          const segCls = 'dn-elimflow-seg ' + (dropped ? 'dn-elimflow-seg-drop dn-elimflow-bad'
            : pending ? 'dn-elimflow-seg-pending' : 'dn-elimflow-good');
          lane.appendChild(svgEl('line', { x1: x, y1: y, x2: toX, y2: y, class: segCls }));
        }
      }
    }

    // the terminating ✕ at the elimination column.
    if (g.eliminatedAt != null) {
      const x = colX(g.eliminatedAt) + 8;
      const xm = svgEl('text', { x, y: y + 3.2, class: 'dn-elimflow-cut dn-elimflow-bad', 'text-anchor': 'start' });
      xm.textContent = '✕';
      lane.appendChild(xm);
    } else if (isChamp || isFormer || g.advanced.size) {
      // a survivor reaching the gate column: the champion gets CROWN.current, the
      // displaced incumbent CROWN.former; any other survivor a neutral arrival.
      const gx = gateX;
      const crowned = isChamp && (o.gateState === 'crowned' || (!o.gateState && !live));
      const mark = isChamp ? CROWN.current : isFormer ? CROWN.former : '→';
      const cls = 'dn-elimflow-gate' + (crowned ? ' dn-elimflow-good' : isFormer ? ' dn-elimflow-former' : '');
      const gm = hov(svgEl('text', { x: gx + 6, y: y + 3.2, class: cls, 'text-anchor': 'start' }),
        isChamp ? `${g.id} · champion ${CROWN.current}` : isFormer ? `${g.id} · former champion (displaced incumbent)` : `${g.id} · reached the gate`);
      gm.textContent = mark;
      lane.appendChild(gm);
    }

    // the lane label at the right gutter. A lane with a live PROJECTED standing
    // (in-flight, boards streaming) reads "~proj" in the projected treatment.
    const proj = g.eliminatedAt == null && projByGen.has(g.id) ? projByGen.get(g.id) : null;
    const lblCls = 'dn-elimflow-name' + (isChamp ? ' dn-elimflow-good' : isFormer ? ' dn-elimflow-former' : g.eliminatedAt != null ? ' dn-elimflow-bad' : '') + (proj ? ' dn-proj' : '');
    const lbl = hov(svgEl('text', { x: w - 6, y: y + 3.2, class: lblCls, 'text-anchor': 'end' }),
      proj ? `${g.id} · projected scalar ~${fmt(proj.scalar, 2)} (boards still streaming)` : g.id);
    lbl.textContent = shortLabel(g.id, 11) + (isChamp ? ' ' + CROWN.current : isFormer ? ' ' + CROWN.former : '') + (proj ? ' ~proj' : '');
    lane.appendChild(lbl);
    // a SCORED board-progress sub-bar under a projected lane (boards_done/total).
    if (proj && isNum(proj.boards_total) && proj.boards_total > 0) {
      const barW = 40;
      const bx = w - 6 - barW;
      const frac = Math.min(1, (proj.boards_done || 0) / proj.boards_total);
      lane.appendChild(svgEl('rect', { x: bx, y: y + 6, width: barW, height: 2.2, rx: 1, class: 'dn-proj-bar-bg' }));
      lane.appendChild(svgEl('rect', { x: bx, y: y + 6, width: Math.max(1, barW * frac), height: 2.2, rx: 1, class: 'dn-proj-bar' }));
    }

    clickable(lane, o.onCompetitor && (() => o.onCompetitor(g.id)));
    svg.appendChild(lane);
  });
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
  // bump panel geometry. bumpTop leaves a clear band under the section title
  // (baseline y=14) so the round-axis labels (drawn at bumpTop-8) never collide
  // with it.
  const bumpTop = 42;
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
    // compact axis ticks — "Swiss round 2" → "R2", "Champion gate" → "Gate" —
    // so the labels never truncate to an ambiguous "Swiss r…".
    const ls = String(lab);
    const rm = ls.match(/(\d+)/);
    tk.textContent = /gate/i.test(ls) ? 'Gate' : (/round/i.test(ls) && rm ? 'R' + rm[1] : shortLabel(ls, 8));
    svg.appendChild(tk);
    svg.appendChild(svgEl('line', { x1: x, x2: x, y1: bumpTop - 4, y2: bumpTop + (nC - 1) * rowH + 4, class: 'dn-swissover-grid' }));
  });
  // one polyline per competitor; champion emphasised.
  series.forEach((s) => {
    const pts = [];
    s.ranks.forEach((r, j) => { if (isNum(r)) pts.push([X(j), Y(r)]); });
    if (!pts.length) return;
    // The CURRENT champion's line is emphasised (bold + ♛); the displaced
    // incumbent reads dim with a "former" mark so the two never look alike.
    const champ = !!s.crown;
    const former = !!s.former;
    const cls = 'dn-swissover-line' + (champ ? ' dn-swissover-line-champ' : (former ? ' dn-swissover-line-former' : ''));
    const d = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
    const path = clickable(hov(svgEl('path', { d, class: cls, fill: 'none', tabindex: o.onCompetitor ? '0' : null }),
      `${s.id}${champ ? ' · new champion' : (former ? ' · former champion' : '')} · finishes rank ${s.ranks[s.ranks.length - 1] || '?'}`),
      o.onCompetitor && (() => o.onCompetitor(s.id)));
    svg.appendChild(path);
    // end-dots (start + final rank) + left name label + right rank label.
    const [x0, y0] = pts[0];
    const [xn, yn] = pts[pts.length - 1];
    const dotCls = 'dn-swissover-dot' + (champ ? ' dn-swissover-dot-champ' : '');
    const r = champ ? 3.4 : 2.6;
    svg.appendChild(svgEl('circle', { cx: x0, cy: y0, r, class: dotCls }));
    svg.appendChild(svgEl('circle', { cx: xn, cy: yn, r, class: dotCls }));
    const lL = svgEl('text', { x: x0 - 6, y: y0 + 3, class: 'dn-swissover-name' + (champ ? ' dn-swissover-name-champ' : (former ? ' dn-swissover-name-former' : '')), 'text-anchor': 'end' });
    lL.textContent = shortLabel(s.id, 11) + (champ ? ' ' + CROWN.current : (former ? ' ' + CROWN.former : ''));
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
    const champ = !!b.crown;
    const former = !!b.former;
    // a transient round-leader ♔ shows only BEFORE the gate decides; once a
    // champion is crowned the ♛ takes over (no double crown).
    const lead = !champ && !former && b.leader && live;
    const g = svgEl('g', { class: 'dn-swissover-barrow', tabindex: o.onCompetitor ? '0' : null });
    const lab = svgEl('text', { x: barX0 - 6, y: y + barH / 2 + 3, class: 'dn-swissover-barname' + (champ ? ' dn-swissover-name-champ' : (former ? ' dn-swissover-name-former' : '')), 'text-anchor': 'end' });
    lab.textContent = (i + 1) + '. ' + shortLabel(b.id, 9) + (champ ? ' ' + CROWN.current : (former || lead ? ' ' + CROWN.former : ''));
    g.appendChild(lab);
    g.appendChild(svgEl('rect', { x: barX0, y, width: barMaxW, height: barH, rx: 3, class: 'dn-swissover-bar-bg' }));
    g.appendChild(hov(svgEl('rect', { x: barX0, y, width: bw, height: barH, rx: 3, class: 'dn-swissover-bar' + (champ || lead ? ' dn-swissover-bar-lead' : '') }),
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
  if (crowned) verdict = `${CROWN.current} ${shortLabel(champId, 12)} promoted`;
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

// ── the GAUNTLET DUEL FLOW — the field as Δ-vs-champion lanes ────────
//
// The gauntlet structure-flow that REPLACES the boxed champion banner + the
// per-challenger match cards: the round's field as a column of lanes, each a
// challenger DUELLING the reigning champion. A horizontal REFERENCE RULE at Δ=0
// is the champion (the crowned gate node on the right); each challenger sits a
// dot at its Δ-vs-champion — BELOW the rule (good, lower loss) when it improved,
// ABOVE (bad) when it regressed — with a status glyph (↑ promoted / ✕ cut / ○
// pending). The promoted challenger's lane reaches the crowned gate (♛). The
// per-challenger hypothesis + the exact Δ live ON HOVER.
//
//   opts: {
//     championId, championScalar,
//     challengers: [{ id, delta, verdict:'promoted'|'rejected'|'pending',
//                     hypothesis, driver }],
//     onCompetitor(id).
//   }
export function duelFlow(opts) {
  const o = opts || {};
  const challengers = (Array.isArray(o.challengers) ? o.challengers : []).filter((c) => c && c.id != null);
  const champId = o.championId != null ? String(o.championId) : null;
  const w = o.width || 720;
  const padTop = 34;
  const padBottom = 22;
  const laneGap = 26;
  const nameW = 60;                              // left gutter for challenger labels
  const gateW = 124;
  const plotLeft = nameW + 18;                   // start of the measured band
  const fieldRight = w - gateW - 28;             // end of the improvement zone (before the gate)
  // The Δ=0 rule sits inside the band with a regression zone to its LEFT and a
  // (larger) improvement zone to its RIGHT running toward the gate.
  const refX = Math.round(plotLeft + 0.34 * (fieldRight - plotLeft));
  const leftSpan = refX - plotLeft;              // |Δ| range for regressions (left)
  const rightSpan = fieldRight - refX;           // |Δ| range for improvements (right)
  const h = padTop + Math.max(1, challengers.length) * laneGap + padBottom;
  const svg = svgEl('svg', {
    class: 'dn-duelflow', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
    'aria-label': 'The field duelling the champion',
  });
  // the champion REFERENCE rule (Δ=0) — a VERTICAL spine the field is measured
  // against: a lane reaches RIGHT toward the gate when it improved, LEFT when it
  // regressed; the bar length encodes |Δ|.
  svg.appendChild(hov(svgEl('line', { x1: refX, x2: refX, y1: padTop - 10, y2: h - padBottom + 4, class: 'dn-duelflow-ref' }),
    champId ? `champion ${champId}${isNum(o.championScalar) ? ' · loss ' + fmt(o.championScalar, 1) : ''} · Δ=0 reference` : 'champion · Δ=0 reference'));
  svg.appendChild(svgEl('text', { x: refX, y: padTop - 16, class: 'dn-duelflow-axis', 'text-anchor': 'middle' }, ['champion · Δ=0']));
  svg.appendChild(svgEl('text', { x: plotLeft, y: padTop - 16, class: 'dn-duelflow-dir dn-bad', 'text-anchor': 'start' }, ['← worse']));
  svg.appendChild(svgEl('text', { x: fieldRight, y: padTop - 16, class: 'dn-duelflow-dir dn-good', 'text-anchor': 'end' }, ['better → (gate)']));

  if (!challengers.length) {
    const t = svgEl('text', { x: (refX + fieldRight) / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no challenger has entered the ring';
    svg.appendChild(t);
  }
  // |Δ| → a SIGNED horizontal offset from the rule: improvements (Δ<0) ride RIGHT
  // toward the gate, regressions (Δ>0) ride LEFT, magnitude scaled to the largest
  // |Δ| in the field so the lanes are comparable. A pending / no-Δ lane sits on
  // the rule.
  const deltas = challengers.map((c) => c.delta).filter(isNum).map(Math.abs);
  const maxAbs = Math.max(1e-9, ...deltas);
  // Reserve a margin at each band edge so the OUTBOARD Δ label never collides
  // with the name gutter (left) or the gate (right) even at the max |Δ|.
  const labelPad = 32;
  const offsetOf = (d) => {
    if (!isNum(d) || d === 0) return 0;
    const frac = Math.min(1, Math.abs(d) / maxAbs);
    return d < 0 ? frac * Math.max(12, rightSpan - labelPad) : -(frac * Math.max(12, leftSpan - labelPad));
  };

  let promotedX = null, promotedY = null;
  challengers.forEach((c, i) => {
    const cy = padTop + i * laneGap + laneGap / 2;
    const verdict = c.verdict || 'pending';
    const won = verdict === 'promoted';
    const cut = verdict === 'rejected';
    const good = isNum(c.delta) ? c.delta < 0 : won;
    const bad = isNum(c.delta) ? c.delta > 0 : cut;
    const dx = refX + offsetOf(c.delta);
    const cls = 'dn-duelflow-dot ' + (good ? 'dn-good' : bad ? 'dn-bad' : 'dn-duelflow-pending');
    const glyph = won ? ' ↑' : cut ? ' ✕' : ' ○';
    const g = svgEl('g', { class: 'dn-duelflow-lane', tabindex: o.onCompetitor ? '0' : null,
      'aria-label': `${c.id} vs champion${isNum(c.delta) ? ', Δ ' + fmtSigned(c.delta, 1) : ''}, ${verdict}` });
    // the lane bar from the rule out to the dot — its direction IS the sign of Δ.
    g.appendChild(svgEl('line', { x1: refX, x2: dx, y1: cy, y2: cy, class: 'dn-duelflow-laneline ' + (good ? 'dn-good' : bad ? 'dn-bad' : '') }));
    const tip = `${c.id} vs ${champId || 'champion'}`
      + (isNum(c.delta) ? ` · Δ ${fmtSigned(c.delta, 2)} (${good ? 'improved' : bad ? 'regressed' : 'flat'})` : '')
      + ` · ${verdict}`
      + (c.hypothesis ? ` · hypothesis: ${c.hypothesis}` : '')
      + (c.driver ? ` · decisive driver: ${c.driver}` : '');
    g.appendChild(hov(svgEl('circle', { cx: dx, cy, r: won ? 4.4 : 3.4, class: cls }), tip));
    // the challenger label + status glyph in the left gutter.
    const lbl = svgEl('text', { x: nameW, y: cy + 3, class: 'dn-duelflow-name ' + (good ? 'dn-good' : bad ? 'dn-bad' : ''), 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(c.id), 9) + glyph;
    g.appendChild(lbl);
    // the Δ value, just OUTBOARD of the dot (away from the rule) so it never
    // collides with the spine.
    if (isNum(c.delta) && c.delta !== 0) {
      const rightward = c.delta < 0;
      const dt = svgEl('text', { x: dx + (rightward ? 7 : -7), y: cy + 3,
        class: 'dn-duelflow-delta ' + (good ? 'dn-good' : bad ? 'dn-bad' : ''),
        'text-anchor': rightward ? 'start' : 'end' });
      dt.textContent = fmtSigned(c.delta, Math.abs(c.delta) < 0.1 ? 2 : 1);
      g.appendChild(dt);
    }
    clickable(g, o.onCompetitor && (() => o.onCompetitor(String(c.id))));
    svg.appendChild(g);
    if (won) { promotedX = dx; promotedY = cy; }
  });

  // ── the crowned CHAMPION GATE on the right ──
  const gx = fieldRight + 18;
  const gateCy = promotedY != null ? promotedY : padTop + (Math.max(1, challengers.length) * laneGap) / 2;
  const promotedAny = challengers.some((c) => (c.verdict || '') === 'promoted');
  const gateG = svgEl('g', { class: 'dn-duelflow-gate', tabindex: (champId && o.onCompetitor) ? '0' : null });
  gateG.appendChild(svgEl('rect', { x: gx, y: gateCy - 14, width: gateW, height: 28, rx: 5,
    class: 'dn-duelflow-gatebox' + (promotedAny ? ' dn-good' : '') }));
  // the converging flow from the promoted lane's dot into the gate.
  if (promotedY != null) {
    svg.appendChild(svgEl('path', { d: `M${promotedX != null ? promotedX : fieldRight},${promotedY} H${gx}`, class: 'dn-duelflow-gateflow dn-good', fill: 'none' }));
  }
  const gt = hov(svgEl('text', { x: gx + gateW / 2, y: gateCy + 4, class: 'dn-duelflow-gatelab' + (promotedAny ? ' dn-good' : ''), 'text-anchor': 'middle' }),
    champId ? `champion-gate · ${promotedAny ? 'a challenger was promoted' : champId + ' defends the title'}` : 'champion-gate');
  gt.textContent = (champId ? CROWN.current + ' ' + shortLabel(champId, 11) : 'champion-gate');
  gateG.appendChild(gt);
  clickable(gateG, (champId && o.onCompetitor) && (() => o.onCompetitor(champId)));
  svg.appendChild(gateG);
  return svg;
}

// The elim epoch overview + Match-ups both render the BRACKET-AS-FLOW
// (`elimFlow`) — the seat/box bracket tree (`elimBracket`) is retired.

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

// ── the CHAMPION-SPINE ROUND TIMELINE — the epoch overview hero ──────
//
// The epoch is N evolve ROUNDS along a horizontal CHAMPION SPINE: one node per
// round's incoming champion (v0 → promoted → …), each annotated with its loss so
// the DESCENDING LOSS-FLOOR reads as the headline "is it improving?" signal.
//
// Each round is an EPISODE card below its spine node:
//   incoming champion + a fan of that round's MINTED challengers + a COMPACT
//   per-round STRUCTURE FIGURE (the caller passes it — swissOverview / survival
//   funnel / elimFlow / a single duel) + the GATE OUTCOME (promoted onto the
//   spine, or held). Clicking the episode (or its spine node) drills into that
//   round's tournament.
//
// SUBSUMES the gauntlet reel: a single round (every run so far, --rounds 1)
// degrades to ONE episode ≈ today's overview; N rounds → the full spine.
//
//   opts: {
//     rounds: [{ round_index, champion:{id,scalar}, challengers:[{id,scalar,promoted}],
//                structure, gateOutcome:{kind,gen} }],
//     selected:   the selected round_index (or null),
//     figureFor(round) → a DOM node (the per-round structure figure) | null,
//     onRound(round_index), onCompetitor(genId),
//   }
export function roundTimeline(opts) {
  const o = opts || {};
  const rounds = Array.isArray(o.rounds) ? o.rounds : [];
  const wrap = el('div', { class: 'dn-roundtl', role: 'group', 'aria-label': 'Champion-spine round timeline' });
  if (!rounds.length) {
    wrap.appendChild(el('p', { class: 'dn-empty', text: 'No rounds have run in this epoch yet — the timeline fills as the evolve loop mints fields.' }));
    return wrap;
  }
  const single = rounds.length === 1;

  // ── the fit-to-width SPINE (champion node per round) ────────────────
  // A FIXED viewBox; the champion node of round 0 is the seed, each subsequent
  // node the round's incoming (carried) champion. The loss annotation under each
  // node lets the descending floor read at a glance.
  const VBW = 1000;
  const VBH = 96;
  const spineY = 54;
  const x0 = 64;
  const xMax = VBW - 56;
  const stationCount = Math.max(1, rounds.length);
  const step = stationCount > 1 ? (xMax - x0) / (stationCount - 1) : 0;
  const xAt = (i) => (stationCount > 1 ? x0 + i * step : (x0 + xMax) / 2);
  const svg = svgEl('svg', {
    class: 'dn-roundtl-spine', viewBox: `0 0 ${VBW} ${VBH}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': `Champion spine across ${rounds.length} round${single ? '' : 's'}`,
  });
  if (stationCount > 1) {
    svg.appendChild(svgEl('line', {
      x1: xAt(0), y1: spineY, x2: xAt(stationCount - 1), y2: spineY, class: 'dn-roundtl-spineline',
    }));
  }
  svg.appendChild(svgEl('text', { x: x0, y: 18, class: 'dn-roundtl-axis' }, ['champion spine · loss floor · rounds →']));

  rounds.forEach((r, i) => {
    const cx = xAt(i);
    const champId = r.champion && r.champion.id != null ? String(r.champion.id) : 'seed';
    const promoted = r.gateOutcome && r.gateOutcome.kind === 'promoted';
    const selected = o.selected != null && String(o.selected) === String(r.round_index);
    const g = svgEl('g', {
      class: 'dn-roundtl-node' + (promoted ? ' dn-roundtl-promote' : '') + (selected ? ' dn-roundtl-sel' : ''),
      tabindex: o.onRound ? '0' : null, role: o.onRound ? 'button' : null,
      'data-round': String(r.round_index),
      'aria-label': `Round ${r.round_index}: champion ${champId}`
        + (isNum(r.champion && r.champion.scalar) ? `, loss ${fmt(r.champion.scalar, 1)}` : '')
        + (promoted ? `, promoted ${r.gateOutcome.gen}` : ', champion held'),
    }, [
      svgEl('circle', { cx, cy: spineY, r: 8, class: 'dn-roundtl-disc' }),
      svgEl('text', { x: cx, y: spineY + 3.5, class: 'dn-roundtl-glyph', 'text-anchor': 'middle' }, [CROWN.current]),
      svgEl('text', { x: cx, y: spineY - 16, class: 'dn-roundtl-champid', 'text-anchor': 'middle' }, [shortLabel(champId, 10)]),
      svgEl('text', { x: cx, y: spineY + 26, class: 'dn-roundtl-loss', 'text-anchor': 'middle' },
        [isNum(r.champion && r.champion.scalar) ? fmt(r.champion.scalar, 1) : '·']),
      svgEl('text', { x: cx, y: spineY + 38, class: 'dn-roundtl-rord', 'text-anchor': 'middle' }, ['r' + r.round_index]),
    ]);
    clickable(g, o.onRound && (() => o.onRound(r.round_index)));
    svg.appendChild(g);
  });
  // The spine plots a champion trajectory across rounds — meaningless for a
  // single round (one node floating in a wide empty viewBox reads as broken).
  // A single-round epoch shows just its episode card below.
  if (!single) wrap.appendChild(el('div', { class: 'dn-roundtl-spineframe' }, [svg]));

  // ── one EPISODE card per round ──────────────────────────────────────
  const episodes = el('div', { class: 'dn-roundtl-episodes' + (single ? ' dn-roundtl-single' : '') });
  rounds.forEach((r) => {
    const champId = r.champion && r.champion.id != null ? String(r.champion.id) : 'seed';
    const promoted = r.gateOutcome && r.gateOutcome.kind === 'promoted';
    const selected = o.selected != null && String(o.selected) === String(r.round_index);
    const card = el('div', {
      class: 'dn-roundtl-ep' + (selected ? ' dn-roundtl-ep-sel' : ''),
      'data-round': String(r.round_index), role: 'group',
      'aria-label': `Round ${r.round_index} episode`,
    });
    // episode header: round ordinal + the incoming champion + a drill link.
    const head = el('div', { class: 'dn-roundtl-ephead' }, [
      el('span', { class: 'dn-roundtl-eptag', text: 'round ' + r.round_index }),
      el('span', { class: 'dn-roundtl-epchamp' }, [
        el('span', { class: 'dn-roundtl-epcrown', 'aria-hidden': 'true', text: CROWN.current }),
        el('span', { class: 'dn-mono', text: champId }),
        isNum(r.champion && r.champion.scalar)
          ? el('span', { class: 'dn-faint', text: ' · loss ' + fmt(r.champion.scalar, 1) }) : null,
      ].filter(Boolean)),
    ]);
    if (o.onRound) {
      const link = el('button', { class: 'dn-linkbtn dn-roundtl-epdrill', type: 'button', text: 'open round →' });
      link.addEventListener('click', () => o.onRound(r.round_index));
      head.appendChild(link);
    }
    card.appendChild(head);

    // the fan of MINTED challengers (chips) — each opens its candidate.
    const fan = el('div', { class: 'dn-roundtl-fan' });
    if (r.challengers.length) {
      for (const c of r.challengers) {
        const chip = el('button', {
          class: 'dn-roundtl-chip' + (c.promoted ? ' dn-roundtl-chip-win' : ''),
          type: 'button',
          'aria-label': `Challenger ${c.id}` + (isNum(c.scalar) ? `, loss ${fmt(c.scalar, 1)}` : '')
            + (c.promoted ? ' — promoted' : ''),
        }, [
          el('span', { class: 'dn-mono', text: shortLabel(String(c.id), 12) }),
          c.promoted ? el('span', { class: 'dn-roundtl-chipcrown', 'aria-hidden': 'true', text: CROWN.current }) : null,
          isNum(c.scalar) ? el('span', { class: 'dn-faint dn-roundtl-chiploss', text: fmt(c.scalar, 1) }) : null,
        ].filter(Boolean));
        if (o.onCompetitor) chip.addEventListener('click', () => o.onCompetitor(String(c.id)));
        fan.appendChild(chip);
      }
    } else {
      fan.appendChild(el('span', { class: 'dn-faint', text: 'no challengers minted this round' }));
    }
    card.appendChild(el('div', { class: 'dn-roundtl-fanrow' }, [
      el('span', { class: 'dn-faint dn-roundtl-fanlab', text: 'field' }),
      fan,
    ]));

    // the compact per-round structure figure (caller-built; null → omitted).
    const fig = o.figureFor ? o.figureFor(r) : null;
    if (fig) card.appendChild(el('div', { class: 'dn-roundtl-fig dn-figpane' }, [fig]));

    // the GATE OUTCOME — promoted (merges onto the spine) or held.
    card.appendChild(el('div', { class: 'dn-roundtl-gate' + (promoted ? ' dn-roundtl-gate-win' : '') }, [
      el('span', { class: 'dn-roundtl-gatemark', 'aria-hidden': 'true', text: promoted ? CROWN.current : '=' }),
      el('span', {
        text: promoted
          ? `${r.gateOutcome.gen} promoted → next round's champion`
          : 'champion held — no promotion this round',
      }),
    ]));
    episodes.appendChild(card);
  });
  wrap.appendChild(episodes);
  return wrap;
}

// ── the LOSS-FLOOR WATERFALL — the epoch's descent across rounds ─────
//
// The headline "is it improving + what drove each gain" figure: each ROUND is a
// step. A round that PROMOTED drops the running loss floor by its promotion Δ
// (a downward step, `good` by DIRECTION — a lower floor is the better outcome);
// a HELD round keeps the floor flat (no step). The running floor is annotated at
// each station; the champion SPINE baseline runs in `--v2-accent`. The winning
// mutation (the promoted gen) per step lives on HOVER.
//
//   opts: {
//     steps: [{ round_index, from, to, delta, promoted, gen }],
//        from/to = the loss floor BEFORE / AFTER the round; delta = to - from
//        (negative = improvement); promoted = a gate promotion this round;
//        gen = the promoted challenger (the winning mutation) | null.
//     onRound(round_index), onCompetitor(genId).
//   }
export function waterfall(opts) {
  const o = opts || {};
  const steps = (Array.isArray(o.steps) ? o.steps : []).filter((s) => s);
  const w = o.width || 720;
  const padL = 56;
  const padR = 18;
  const padTop = 26;
  const padBottom = 28;
  const colW = steps.length ? (w - padL - padR) / steps.length : (w - padL - padR);
  const barW = Math.max(8, Math.min(colW * 0.6, 54));
  const h = (o.height || 220);
  const svg = svgEl('svg', {
    class: 'dn-waterfall', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': 'Loss-floor descent across rounds',
  });
  if (!steps.length) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no rounds yet';
    svg.appendChild(t);
    return svg;
  }
  // the loss domain spans every from/to floor; lower loss sits LOWER on the y
  // axis (a descent reads as a downward staircase).
  const floors = [];
  for (const s of steps) { if (isNum(s.from)) floors.push(s.from); if (isNum(s.to)) floors.push(s.to); }
  let [lo, hi] = extent(floors);
  lo = Math.min(lo, hi);
  const pad = (hi - lo) * 0.12 || 1;
  const y = scale([lo - pad, hi + pad], [h - padBottom, padTop]);
  const colX = (i) => padL + i * colW + colW / 2;

  // the SPINE baseline (accent): the champion floor connecting station to
  // station — the structural highlight of the figure.
  let spineD = '';
  steps.forEach((s, i) => {
    const cx = colX(i);
    const yFrom = isNum(s.from) ? y(s.from) : null;
    const yTo = isNum(s.to) ? y(s.to) : null;
    if (yFrom != null) spineD += `${spineD ? 'L' : 'M'}${(cx - barW / 2).toFixed(1)},${yFrom.toFixed(1)} `;
    if (yTo != null) spineD += `L${(cx + barW / 2).toFixed(1)},${yTo.toFixed(1)} `;
  });
  if (spineD) svg.appendChild(svgEl('path', { d: spineD.trim(), class: 'dn-waterfall-spine', fill: 'none' }));

  svg.appendChild(svgEl('text', { x: padL - 2, y: padTop - 12, class: 'dn-waterfall-axis' }, ['loss floor ↓ improving · rounds →']));

  steps.forEach((s, i) => {
    const cx = colX(i);
    const yFrom = isNum(s.from) ? y(s.from) : null;
    const yTo = isNum(s.to) ? y(s.to) : null;
    const improved = isNum(s.delta) && s.delta < 0;
    const regressed = isNum(s.delta) && s.delta > 0;
    const held = !s.promoted || !isNum(s.delta) || s.delta === 0;
    const g = svgEl('g', {
      class: 'dn-waterfall-step', tabindex: o.onRound ? '0' : null,
      'aria-label': `Round ${s.round_index}: ` + (held ? 'champion held' : `${s.gen} promoted, Δ ${fmtSigned(s.delta, 1)}`),
    });
    // the step bar: from the incoming floor DOWN to the new floor (a promotion);
    // a held round is a flat tick at the floor.
    if (!held && yFrom != null && yTo != null) {
      const yA = Math.min(yFrom, yTo);
      const yB = Math.max(yFrom, yTo);
      const cls = 'dn-waterfall-bar ' + (improved ? 'dn-good' : regressed ? 'dn-bad' : 'dn-flat');
      g.appendChild(hov(svgEl('rect', { x: cx - barW / 2, y: yA, width: barW, height: Math.max(2, yB - yA), rx: 2, class: cls }),
        `round ${s.round_index} · ${s.gen ? s.gen + ' promoted' : 'promoted'} · floor ${fmt(s.from, 1)} → ${fmt(s.to, 1)} · Δ ${fmtSigned(s.delta, 1)}`));
      // the connector from the prior floor into this step's top.
    } else if (yTo != null) {
      g.appendChild(hov(svgEl('line', { x1: cx - barW / 2, x2: cx + barW / 2, y1: yTo, y2: yTo, class: 'dn-waterfall-held' }),
        `round ${s.round_index} · champion held · floor ${fmt(s.to, 1)}`));
    }
    // the running-floor annotation under the station.
    const flLbl = svgEl('text', { x: cx, y: (yTo != null ? yTo : h - padBottom) - 6, class: 'dn-waterfall-floor', 'text-anchor': 'middle' });
    flLbl.textContent = isNum(s.to) ? fmt(s.to, 1) : '·';
    g.appendChild(flLbl);
    // the round ordinal on the x axis.
    const rord = svgEl('text', { x: cx, y: h - padBottom + 14, class: 'dn-waterfall-rord', 'text-anchor': 'middle' });
    rord.textContent = 'r' + s.round_index;
    g.appendChild(rord);
    // the winning-mutation glyph (the promoted gen) — a crown over a promoting step.
    if (!held && s.gen != null && yTo != null) {
      const cr = svgEl('text', { x: cx, y: y(s.to) - (isNum(s.from) && isNum(s.to) && s.to < s.from ? 8 : -14), class: 'dn-waterfall-crown', 'text-anchor': 'middle' });
      cr.textContent = CROWN.current;
      g.appendChild(cr);
    }
    clickable(g, o.onRound && (() => o.onRound(s.round_index)));
    svg.appendChild(g);
  });
  return svg;
}

// ── the CHAMPION REIGN GANTT — tenure across rounds ─────────────────
//
// One BAR per champion spanning the rounds it HELD the title. The CURRENT
// champion's bar is `--v2-accent` + ♛; every FORMER champion's bar is ink / dim
// + ♔. The reign of one generation reads as a highlighted SEGMENT of the spine —
// the candidate page passes a single generation's reign as the "reign ribbon".
//
//   opts: {
//     reigns: [{ id, fromRound, toRound, current }]  — fromRound..toRound inclusive
//     rounds: total round count (the x extent), or inferred from reigns.
//     onCompetitor(id).
//   }
export function reignGantt(opts) {
  const o = opts || {};
  const reigns = (Array.isArray(o.reigns) ? o.reigns : []).filter((r) => r && r.id != null);
  const w = o.width || 640;
  const rowH = o.rowHeight || 22;
  const padL = o.labelWidth || 120;
  const padR = 18;
  const top = 22;
  const h = top + Math.max(1, reigns.length) * rowH + 10;
  const svg = svgEl('svg', {
    class: 'dn-reigngantt', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
    'aria-label': 'Champion reign across rounds',
  });
  if (!reigns.length) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dn-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no reign yet';
    svg.appendChild(t);
    return svg;
  }
  const maxRound = isNum(o.rounds) ? o.rounds
    : Math.max(1, ...reigns.map((r) => (isNum(r.toRound) ? r.toRound : 0)));
  const x = scale([0, Math.max(1, maxRound)], [padL + 4, w - padR]);
  // round-axis ticks along the top.
  for (let ri = 0; ri <= maxRound; ri++) {
    const tx = x(ri);
    const tk = svgEl('text', { x: tx, y: top - 8, class: 'dn-reigngantt-axis', 'text-anchor': 'middle' });
    tk.textContent = 'r' + ri;
    svg.appendChild(tk);
    svg.appendChild(svgEl('line', { x1: tx, x2: tx, y1: top - 4, y2: h - 6, class: 'dn-reigngantt-grid' }));
  }
  reigns.forEach((r, i) => {
    const cy = top + i * rowH + rowH / 2;
    const x0 = x(isNum(r.fromRound) ? r.fromRound : 0);
    const x1 = x(isNum(r.toRound) ? r.toRound : maxRound);
    const current = !!r.current;
    const g = svgEl('g', { class: 'dn-reigngantt-row', tabindex: o.onCompetitor ? '0' : null });
    const lbl = svgEl('text', { x: padL - 8, y: cy + 3, class: 'dn-reigngantt-name' + (current ? ' dn-reigngantt-current' : ' dn-reigngantt-former'), 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(r.id), 12) + ' ' + (current ? CROWN.current : CROWN.former);
    g.appendChild(lbl);
    const span = Math.max(4, x1 - x0);
    g.appendChild(hov(svgEl('rect', {
      x: x0, y: cy - rowH * 0.32, width: span, height: rowH * 0.64, rx: 3,
      class: 'dn-reigngantt-bar' + (current ? ' dn-reigngantt-bar-current' : ' dn-reigngantt-bar-former'),
    }), `${r.id} ${current ? CROWN.current + ' current champion' : CROWN.former + ' former champion'} · held r${isNum(r.fromRound) ? r.fromRound : 0}`
      + (isNum(r.toRound) && r.toRound !== r.fromRound ? `–r${r.toRound}` : '')));
    clickable(g, o.onCompetitor && (() => o.onCompetitor(String(r.id))));
    svg.appendChild(g);
  });
  return svg;
}
