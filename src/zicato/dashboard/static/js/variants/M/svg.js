// variants/M/svg.js — dependency-free SVG data-viz primitives (Ledger II).
//
// Self-contained for Variant M. Mark CSS classes are `d-*` (shared Tufte
// language, reused from the Ledger lineage) and are styled — scoped under the
// variant root — by css/variants/M/ledger2.css.
//
// Conventions:
//   * Lower drift/loss is BETTER (the tournament gate ranks by it). Helpers
//     that know the polarity treat DOWN as good.
//   * Every primitive is total: empty/NaN input yields a quiet empty mark,
//     never a throw.
//   * Heatmap/dot ramp colours interpolate between LIVE theme tokens
//     (`--m-ramp-lo` … `--m-ramp-hi`, `--m-ramp-good` / `--m-ramp-bad`) read
//     at draw time, so a mark re-skins for ALL THREE colour themes with
//     sufficient contrast — the convergence-II "theme-aware ramp, no fixed
//     orange/brown" requirement.

import { svgEl } from '../../core/dom.js';

export const NS = 'http://www.w3.org/2000/svg';

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

// ---- sparkline ------------------------------------------------------

export function sparkline(opts) {
  const o = opts || {};
  const w = o.width || 120;
  const h = o.height || 28;
  const pad = 2;
  const raw = Array.isArray(o.values) ? o.values : [];
  const fin = finiteValues(raw);
  const svg = svgEl('svg', {
    class: 'd-spark', width: w, height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', role: 'img',
  });
  if (fin.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h / 2, x2: w - pad, y2: h / 2, class: 'd-spark-empty' }));
    return svg;
  }
  const [lo, hi] = extent(fin);
  const x = scale([0, Math.max(1, raw.length - 1)], [pad, w - pad]);
  const y = scale([lo, hi], [h - pad, pad]);

  if (o.band) {
    svg.appendChild(svgEl('rect', { x: pad, y: pad, width: w - 2 * pad, height: h - 2 * pad, class: 'd-spark-band' }));
  }
  if (isNum(o.baseline)) {
    const by = y(o.baseline);
    svg.appendChild(svgEl('line', { x1: pad, x2: w - pad, y1: by, y2: by, class: 'd-spark-baseline' }));
  }
  let d = '';
  let penDown = false;
  raw.forEach((v, i) => {
    if (!isNum(v)) { penDown = false; return; }
    d += `${penDown ? 'L' : 'M'}${x(i).toFixed(2)},${y(v).toFixed(2)} `;
    penDown = true;
  });
  svg.appendChild(svgEl('path', { d: d.trim(), class: 'd-spark-line', fill: 'none' }));

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
      const cls = improved === null ? 'd-spark-dot'
        : improved ? 'd-spark-dot d-good' : 'd-spark-dot d-bad';
      svg.appendChild(svgEl('circle', { cx: x(lastI), cy: y(raw[lastI]), r: 2.2, class: cls }, [title(fmt(raw[lastI]))]));
    }
  }
  return svg;
}

// One-dimensional collision resolver. Returns adjusted positions in input
// order. Exported for the test suite (the non-collision guarantee).
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

// ---- bumps chart (lineage as ranked lanes) --------------------------

export function bumps(opts) {
  const o = opts || {};
  const nodes = (Array.isArray(o.nodes) ? o.nodes : []).filter((n) => n);
  const w = o.width || 640;
  const h = o.height || 180;
  const padX = 44; const spineY = h * 0.40; const challY = h * 0.80;
  const svg = svgEl('svg', { class: 'd-bumps', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (nodes.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'd-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no generations yet';
    svg.appendChild(t);
    return svg;
  }
  const maxX = Math.max(1, ...nodes.map((n) => n.x || 0));
  const X = scale([0, maxX], [padX, w - padX]);

  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: spineY, y2: spineY, class: 'd-lane-guide d-spine-guide' }));
  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: challY, y2: challY, class: 'd-lane-guide' }));
  const lblS = svgEl('text', { x: 6, y: spineY - 8, class: 'd-lane-label' }); lblS.textContent = 'champion';
  const lblC = svgEl('text', { x: 6, y: challY - 8, class: 'd-lane-label' }); lblC.textContent = 'challenger';
  svg.appendChild(lblS); svg.appendChild(lblC);

  const laneY = (n) => (n.promoted ? spineY : challY);
  const byId = new Map(nodes.map((n) => [n.id, n]));

  const promoted = nodes.filter((n) => n.promoted).sort((a, b) => (a.x || 0) - (b.x || 0));
  for (let i = 1; i < promoted.length; i++) {
    svg.appendChild(svgEl('line', { x1: X(promoted[i - 1].x), y1: spineY, x2: X(promoted[i].x), y2: spineY, class: 'd-spine-line' }));
  }
  for (const n of nodes) {
    if (n.promoted) continue;
    const p = n.parent ? byId.get(n.parent) : null;
    const px = p ? X(p.x) : X((n.x || 1) - 1);
    const py = p ? laneY(p) : spineY;
    const nx = X(n.x);
    const path = `M${px},${py} C${(px + nx) / 2},${py} ${(px + nx) / 2},${challY} ${nx},${challY}`;
    svg.appendChild(svgEl('path', { d: path, class: 'd-branch', fill: 'none' }));
  }
  // De-collide challengers that share an x (v1/v2 off the same parent).
  const laneGroups = new Map();
  for (const n of nodes) {
    const key = (n.promoted ? 'c' : 'h') + ':' + (n.x || 0);
    if (!laneGroups.has(key)) laneGroups.set(key, []);
    laneGroups.get(key).push(n);
  }
  const nodeX = new Map();
  for (const grp of laneGroups.values()) {
    const n = grp.length; const mid = (n - 1) / 2;
    grp.forEach((node, k) => { nodeX.set(node, X(node.x) + (k - mid) * 22); });
  }
  for (const n of nodes) {
    const cx = nodeX.get(n);
    const cy = laneY(n);
    const cls = 'd-bump-node ' + (n.promoted ? 'd-promoted' : 'd-rejected');
    const c = svgEl('circle', { cx, cy, r: n.promoted ? 4.5 : 3.5, class: cls, tabindex: o.onClick ? '0' : null,
      role: o.onClick ? 'button' : 'img', 'data-gen': n.id, 'aria-label': `candidate ${n.id}` },
    [title(`${n.id}${isNum(n.scalar) ? ' · ' + fmt(n.scalar) : ''} · ${n.promoted ? 'promoted' : 'rejected'}`)]);
    if (o.onClick) {
      c.style.cursor = 'pointer';
      c.addEventListener('click', () => o.onClick(n));
      c.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onClick(n); } });
    }
    svg.appendChild(c);
    const t = svgEl('text', { x: cx, y: cy + 16, class: 'd-bump-label', 'text-anchor': 'middle' });
    t.textContent = shortLabel(n.id);
    svg.appendChild(t);
  }
  return svg;
}

// ---- heatmap (themed, quiet) ----------------------------------------

export function heatmap(opts) {
  const o = opts || {};
  const rows = Array.isArray(o.rows) ? o.rows : [];
  const cols = Array.isArray(o.cols) ? o.cols : [];
  const cw = o.cellW || 26;
  const ch = o.cellH || 16;
  const labelW = o.labelWidth || 130;
  const headH = o.headHeight || 44;
  const w = labelW + cols.length * cw + 6;
  const h = headH + rows.length * ch + 6;
  const svg = svgEl('svg', { class: 'd-heatmap', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (rows.length === 0 || cols.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'd-empty-label' });
    t.textContent = 'no profiles yet';
    svg.appendChild(t);
    return svg;
  }
  const vals = [];
  for (const r of rows) for (const c of cols) {
    const v = o.value(r.id, c.id);
    if (isNum(v)) vals.push(v);
  }
  const [lo, hi] = extent(vals);
  const span = hi - lo || 1;

  cols.forEach((c, j) => {
    const cx = labelW + j * cw + cw / 2;
    const t = svgEl('text', { x: cx, y: headH - 6, class: 'd-hm-col', transform: `rotate(-45 ${cx} ${headH - 6})`, 'text-anchor': 'start' });
    t.textContent = shortLabel(c.label);
    svg.appendChild(t);
  });
  rows.forEach((r, i) => {
    const ry = headH + i * ch;
    const lbl = svgEl('text', { x: labelW - 6, y: ry + ch - 4, class: 'd-hm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(r.label);
    svg.appendChild(lbl);
    cols.forEach((c, j) => {
      const v = o.value(r.id, c.id);
      const cx = labelW + j * cw;
      const t = isNum(v) ? (v - lo) / span : null;
      const fill = t == null ? 'var(--v2-cell-empty)' : rampColor(t, o.diverging);
      const cell = svgEl('rect', { x: cx + 1, y: ry + 1, width: cw - 2, height: ch - 2, rx: 1.5, class: 'd-hm-cell', fill },
        [title(`${r.label} × ${c.label}: ${isNum(v) ? fmt(v) : '—'}`)]);
      if (o.onClick) {
        cell.style.cursor = 'pointer';
        cell.addEventListener('click', () => o.onClick(r.id, c.id));
      }
      svg.appendChild(cell);
    });
  });
  return svg;
}

// Quiet sequential / diverging ramp. Reads LIVE theme tokens off the variant
// root so the same loss-cell reads with sufficient contrast in all three M
// colour themes. Light-theme fallbacks are baked in so a unit test without a
// stylesheet still draws a sane ramp.
function themeVar(name, fallback) {
  try {
    const root = (typeof document !== 'undefined' && document.documentElement) ? document.documentElement : null;
    if (root && typeof getComputedStyle === 'function') {
      const v = getComputedStyle(root).getPropertyValue(name);
      if (v && v.trim()) return v.trim();
    }
  } catch { /* no live styles (unit test) — fall back */ }
  return fallback;
}
export function rampColor(t, diverging) {
  const x = Math.max(0, Math.min(1, t));
  if (diverging) {
    const good = themeVar('--m-ramp-good', '#1f7a6b');
    const mid = themeVar('--m-ramp-lo', '#e9e3d6');
    const bad = themeVar('--m-ramp-bad', '#b14a4a');
    if (x < 0.5) return lerpHex(good, mid, x * 2);
    return lerpHex(mid, bad, (x - 0.5) * 2);
  }
  return lerpHex(themeVar('--m-ramp-lo', '#f2ede1'), themeVar('--m-ramp-hi', '#9c5a3c'), x);
}
function lerpHex(a, b, t) {
  const pa = hexToRgb(a); const pb = hexToRgb(b);
  return `rgb(${Math.round(pa[0] + (pb[0] - pa[0]) * t)},${Math.round(pa[1] + (pb[1] - pa[1]) * t)},${Math.round(pa[2] + (pb[2] - pa[2]) * t)})`;
}
function hexToRgb(hex) {
  let h = String(hex == null ? '' : hex).trim().replace('#', '');
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return [Number.isFinite(r) ? r : 0, Number.isFinite(g) ? g : 0, Number.isFinite(b) ? b : 0];
}

// ---- sparkbar (micro loss bars + verdict marker) --------------------

export function sparkbar(opts) {
  const o = opts || {};
  const bars = (Array.isArray(o.bars) ? o.bars : []).filter((b) => b);
  const w = o.width || 120;
  const h = o.height || 30;
  const pad = 2;
  const footH = 2;
  const svg = svgEl('svg', { class: 'd-sparkbar', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (bars.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h - footH, x2: w - pad, y2: h - footH, class: 'd-spark-empty' }));
    return svg;
  }
  const dom = o.domain && o.domain.length === 2 && isNum(o.domain[0]) && isNum(o.domain[1]) ? o.domain : extent(bars.map((b) => b.value));
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
      const cls = 'd-sparkbar-bar' + (b.timeout ? ' d-timeout' : '') + (b.fail ? ' d-fail' : '');
      svg.appendChild(svgEl('rect', { x: cx - bw / 2, y: Math.min(y, y0), width: bw, height: Math.max(1, Math.abs(y0 - y)), class: cls },
        [title(`${b.label}: ${fmt(b.value)}${b.timeout ? ' · timed out' : ''}${b.fail ? ' · failed' : ''}`)]));
    } else {
      svg.appendChild(svgEl('line', { x1: cx, y1: y0 - 1, x2: cx, y2: y0 - 4, class: 'd-sparkbar-missing' }, [title(`${b.label}: no run`)]));
    }
  });
  svg.appendChild(svgEl('line', { x1: pad, y1: y0, x2: w - pad, y2: y0, class: 'd-sparkbar-foot' }));
  if (o.verdict === 'promoted' || o.verdict === 'rejected') {
    const good = o.verdict === 'promoted';
    const gx = w - pad - 3; const gy = pad + 4; const r = 3.2;
    const tri = good ? `${gx},${gy - r} ${gx - r},${gy + r} ${gx + r},${gy + r}` : `${gx},${gy + r} ${gx - r},${gy - r} ${gx + r},${gy - r}`;
    svg.appendChild(svgEl('polygon', { points: tri, class: 'd-verdict-glyph ' + (good ? 'd-good' : 'd-bad') }, [title(o.verdict)]));
  }
  return svg;
}

// ---- value dot-plot with a reference line ---------------------------

export function valueDotPlot(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d);
  const w = o.width || 460;
  const rh = o.rowHeight || 20;
  const labelW = o.labelWidth || 170;
  const glyphW = 16;
  const h = Math.max(rh, items.length * rh + 8);
  const svg = svgEl('svg', { class: 'd-valdot', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'd-empty-label' });
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
    svg.appendChild(svgEl('line', { x1: rx, x2: rx, y1: 2, y2: h - 2, class: 'd-ref-rule' }, [title(`${(o.reference.label || 'reference')}: ${fmt(ref)}`)]));
  }
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 4;
    const g = svgEl('g', { class: 'd-dotrow', tabindex: o.onClick ? '0' : null, 'data-id': d.id });
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'd-dot-label', 'text-anchor': 'end' });
    lbl.textContent = d.label != null ? shortLabel(String(d.label), 22) : '';
    g.appendChild(lbl);
    if (isNum(d.value)) {
      const dx = x(d.value);
      g.appendChild(svgEl('line', { x1: x(lo), x2: dx, y1: cy, y2: cy, class: 'd-dot-connector' }));
      const good = ref != null ? d.value < ref : false;
      const worse = ref != null ? d.value > ref : false;
      const cls = 'd-dot ' + (good ? 'd-good' : worse ? 'd-bad' : '');
      g.appendChild(svgEl('circle', { cx: dx, cy, r: 3.2, class: cls }, [title(`${d.label}: ${fmt(d.value)}${ref != null ? ` (vs champ ${fmt(ref)})` : ''}`)]));
      g.appendChild(outcomeGlyph(d, w - glyphW + 2, cy));
    } else {
      const t = svgEl('text', { x: x(lo) + 6, y: cy + 3, class: 'd-dot-missing' });
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

export function genDots(opts) {
  const o = opts || {};
  const cells = Array.isArray(o.cells) ? o.cells : [];
  const w = o.width || 200;
  const h = o.height || 14;
  const pad = 2;
  const svg = svgEl('svg', { class: 'd-genrow', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  const n = Math.max(1, cells.length);
  const slot = (w - 2 * pad) / n;
  cells.forEach((c, i) => {
    const cx = pad + slot * (i + 0.5);
    svg.appendChild(outcomeGlyph(c, cx, h / 2));
  });
  return svg;
}

function outcomeGlyph(d, x, cy) {
  if (d && d.ran === false) return svgEl('circle', { cx: x, cy, r: 2.2, class: 'd-glyph-none' }, [title('no run')]);
  if (d && d.timeout) return svgEl('text', { x, y: cy + 3, class: 'd-glyph-timeout', 'text-anchor': 'middle' }, [title('budget exceeded (timeout)'), '⏱']);
  if (d && (d.pass === true || d.pass === 1)) return svgEl('circle', { cx: x, cy, r: 2.4, class: 'd-glyph-pass' }, [title('passed')]);
  if (d && (d.pass === false || d.pass === 0)) {
    const g = svgEl('g', null, [title('failed')]);
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy - 2.4, x2: x + 2.4, y2: cy + 2.4, class: 'd-glyph-fail' }));
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy + 2.4, x2: x + 2.4, y2: cy - 2.4, class: 'd-glyph-fail' }));
    return g;
  }
  return svgEl('circle', { cx: x, cy, r: 2.2, class: 'd-glyph-none' }, [title('no predicate')]);
}

// ---- horizontal value bars ------------------------------------------

export function valueBars(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d && isNum(d.value));
  const w = o.width || 360;
  const rh = o.rowHeight || 18;
  const labelW = o.labelWidth || 150;
  const h = Math.max(rh, items.length * rh + 6);
  const svg = svgEl('svg', { class: 'd-vbars', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 14, class: 'd-empty-label' });
    t.textContent = 'no values';
    svg.appendChild(t);
    return svg;
  }
  const hi = Math.max(1e-9, ...items.map((d) => Math.abs(d.value)));
  const x0 = labelW + 4;
  const x = scale([0, hi], [x0, w - 36]);
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 3;
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'd-dot-label', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(d.label), 20);
    svg.appendChild(lbl);
    const bx = x(Math.abs(d.value));
    svg.appendChild(svgEl('rect', { x: x0, y: cy - 4, width: Math.max(1, bx - x0), height: 8, rx: 1, class: 'd-vbar' }, [title(`${d.label}: ${fmt(d.value)}`)]));
    const vt = svgEl('text', { x: bx + 4, y: cy + 3, class: 'd-vbar-val' });
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

  const svg = svgEl('svg', { class: 'd-pslope', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (series.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'd-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no paired board duels yet';
    svg.appendChild(t);
    return svg;
  }
  const allVals = [];
  for (const s of series) { if (isNum(s.a)) allVals.push(s.a); if (isNum(s.b)) allVals.push(s.b); }
  const [lo, hi] = extent(allVals);
  const y = scale([lo, hi], [h - padBottom, padTop]);

  const hL = svgEl('text', { x: leftX, y: 15, class: 'd-slope-col', 'text-anchor': 'end' });
  hL.textContent = (o.left && o.left.title) || 'champion';
  const hR = svgEl('text', { x: rightX, y: 15, class: 'd-slope-col', 'text-anchor': 'start' });
  hR.textContent = (o.right && o.right.title) || 'challenger';
  svg.appendChild(hL); svg.appendChild(hR);
  svg.appendChild(svgEl('line', { x1: leftX, x2: leftX, y1: y(hi), y2: y(lo), class: 'd-slope-axis' }));
  svg.appendChild(svgEl('line', { x1: rightX, x2: rightX, y1: y(hi), y2: y(lo), class: 'd-slope-axis' }));

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
    const dirCls = verdict === 'improved' ? 'd-good' : verdict === 'regressed' ? 'd-bad' : 'd-flat';
    const g = svgEl('g', { class: 'd-pslope-series', tabindex: o.onClick ? '0' : null, 'data-id': s.id || s.label });

    if (ay != null && by != null) {
      const line = svgEl('line', { x1: leftX, y1: ay, x2: rightX, y2: by, class: 'd-pslope-line ' + dirCls });
      line.appendChild(title(`${s.label}: ${fmt(s.a)} → ${fmt(s.b)} (${fmtSigned(s.b - s.a)}; ${verdict})`));
      g.appendChild(line);
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'd-pslope-node ' + dirCls }));
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'd-pslope-node ' + dirCls }));
    } else if (ay != null) {
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'd-pslope-node d-flat' }, [title(`${s.label}: champion only ${fmt(s.a)}`)]));
    } else if (by != null) {
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'd-pslope-node d-flat' }, [title(`${s.label}: challenger only ${fmt(s.b)}`)]));
    }

    const ll = leftLabels[i];
    if (isNum(s.a)) {
      if (Math.abs(ll - y(s.a)) > 1.5) g.appendChild(svgEl('line', { x1: leftX - 4, y1: ll, x2: leftX, y2: y(s.a), class: 'd-leader' }));
      const tx = svgEl('text', { x: leftX - 8, y: ll + 3, class: 'd-pslope-label', 'text-anchor': 'end' });
      tx.textContent = `${shortLabel(s.label, 14)}  ${fmt(s.a, 1)}`;
      g.appendChild(tx);
    }
    const rl = rightLabels[i];
    if (isNum(s.b)) {
      if (Math.abs(rl - y(s.b)) > 1.5) g.appendChild(svgEl('line', { x1: rightX, y1: y(s.b), x2: rightX + 4, y2: rl, class: 'd-leader' }));
      const tx = svgEl('text', { x: rightX + 8, y: rl + 3, class: 'd-pslope-label', 'text-anchor': 'start' });
      tx.textContent = `${fmt(s.b, 1)}  ${shortLabel(s.label, 14)}`;
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
    const n = idxs.length; const mid = (n - 1) / 2;
    idxs.forEach((idx, k) => { out[idx] = ys[idx] + (k - mid) * (step || 3); });
  }
  return out;
}

// ---- mutation-site × generation matrix ------------------------------
//
// The convergence-II combined-visual root: a filled cell = that generation
// patched that site. Clicking a cell selects (site, gen) so the SIDE-BY-SIDE
// diff pane fills against that exact challenger.
//
// opts: { sites:[{id,label,sub,patched:Set}], gens:[{id,label,promoted}],
//         patched(siteId,genId), selSite, selGen, onCell(siteId,genId) }
export function mutationMatrix(opts) {
  const o = opts || {};
  const sites = Array.isArray(o.sites) ? o.sites : [];
  const gens = Array.isArray(o.gens) ? o.gens : [];
  const cw = o.cellW || 64;
  const ch = o.cellH || 30;
  const labelW = o.labelWidth || 240;
  const headH = o.headHeight || 30;
  const w = labelW + gens.length * cw + 8;
  const h = headH + sites.length * ch + 8;
  const svg = svgEl('svg', { class: 'd-mutmatrix', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img', 'aria-label': 'Mutation sites by generation' });
  if (sites.length === 0 || gens.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'd-empty-label' });
    t.textContent = 'no mutation surface recorded';
    svg.appendChild(t);
    return svg;
  }
  gens.forEach((g, j) => {
    const cx = labelW + j * cw + cw / 2;
    const t = svgEl('text', { x: cx, y: headH - 8, class: 'd-mm-col' + (g.promoted ? ' d-promoted' : ''), 'text-anchor': 'middle' });
    t.textContent = shortLabel(g.label || g.id, 8);
    svg.appendChild(t);
  });
  sites.forEach((s, i) => {
    const ry = headH + i * ch;
    const lg = svgEl('g', null, [title(`${s.label}${s.sub ? ' · ' + s.sub : ''}`)]);
    const lbl = svgEl('text', { x: labelW - 8, y: ry + ch / 2, class: 'd-mm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(s.label, 30);
    lg.appendChild(lbl);
    if (s.sub) {
      lbl.setAttribute('y', ry + ch / 2 - 3);
      const sub = svgEl('text', { x: labelW - 8, y: ry + ch / 2 + 9, class: 'd-mm-row-sub', 'text-anchor': 'end' });
      sub.textContent = shortLabel(s.sub, 30);
      lg.appendChild(sub);
    }
    svg.appendChild(lg);
    gens.forEach((g, j) => {
      const cx = labelW + j * cw;
      const on = !!o.patched(s.id, g.id);
      const sel = o.selSite === s.id && o.selGen === g.id;
      const cell = svgEl('rect', { x: cx + 2, y: ry + 2, width: cw - 4, height: ch - 4, rx: 3,
        class: 'd-mm-cell ' + (on ? 'd-mm-on' : 'd-mm-off') + (sel ? ' d-mm-sel' : ''),
        'data-gen': g.id, 'data-site': s.id },
      [title(`${g.id} ${on ? 'patched' : 'did not touch'} ${s.label}`)]);
      svg.appendChild(cell);
      if (on) {
        const dot = svgEl('circle', { cx: cx + cw / 2, cy: ry + ch / 2, r: 3.4, class: 'd-mm-dot' });
        svg.appendChild(dot);
        if (o.onCell) {
          cell.style.cursor = 'pointer';
          cell.addEventListener('click', () => o.onCell(s.id, g.id));
        }
      }
    });
  });
  return svg;
}

// ---- mini single-elimination bracket (ILLUSTRATIVE) -----------------

export function bracketMini(opts) {
  const o = opts || {};
  const w = o.width || 360;
  const challengers = Array.isArray(o.challengers) ? o.challengers : [];
  const seats = [{ id: o.champion, champion: true }, ...challengers.map((c) => ({ id: c.id }))];
  const rowH = 26;
  const h = o.height || Math.max(80, seats.length * rowH + 20);
  const svg = svgEl('svg', { class: 'd-bracket', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (seats.length === 0) return svg;
  const seatW = 96; const col1 = 8; const col2 = w - seatW - 8; const midX = (col1 + seatW + col2) / 2;
  const winnerY = h / 2;
  seats.forEach((s, i) => {
    const y = 12 + i * rowH;
    const won = s.id === o.winner;
    const box = svgEl('rect', { x: col1, y, width: seatW, height: 18, rx: 2, class: 'd-bracket-seat' + (won ? ' d-win' : '') + (s.champion ? ' d-champ' : '') });
    svg.appendChild(box);
    const t = svgEl('text', { x: col1 + 6, y: y + 13, class: 'd-bracket-label' });
    t.textContent = shortLabel(String(s.id), 12);
    svg.appendChild(t);
    const cy = y + 9;
    svg.appendChild(svgEl('path', { d: `M${col1 + seatW},${cy} H${midX} V${winnerY + 9} H${col2}`, class: 'd-bracket-edge' + (won ? ' d-win' : ''), fill: 'none' }));
  });
  const fy = winnerY;
  svg.appendChild(svgEl('rect', { x: col2, y: fy, width: seatW, height: 18, rx: 2, class: 'd-bracket-seat d-win d-champ' }));
  const wt = svgEl('text', { x: col2 + 6, y: fy + 13, class: 'd-bracket-label' });
  wt.textContent = o.winner ? `${shortLabel(String(o.winner), 9)} ✦` : 'tbd';
  svg.appendChild(wt);
  return svg;
}

// ---- round-robin matrix (ILLUSTRATIVE heat grid) -------------------

export function roundRobinMatrix(opts) {
  const o = opts || {};
  const ids = Array.isArray(o.ids) ? o.ids : [];
  const cw = o.cell || 30;
  const labelW = o.label || 44;
  const headH = 18;
  const w = labelW + ids.length * cw + 4;
  const h = headH + ids.length * cw + 4;
  const svg = svgEl('svg', { class: 'd-rrmatrix', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (ids.length === 0) return svg;
  const loss = o.lossById || {};
  ids.forEach((cid, j) => {
    const t = svgEl('text', { x: labelW + j * cw + cw / 2, y: headH - 5, class: 'd-rr-head', 'text-anchor': 'middle' });
    t.textContent = shortLabel(String(cid), 5);
    svg.appendChild(t);
  });
  ids.forEach((rid, i) => {
    const ry = headH + i * cw;
    const lbl = svgEl('text', { x: labelW - 5, y: ry + cw / 2 + 3, class: 'd-rr-head', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(rid), 6);
    svg.appendChild(lbl);
    ids.forEach((cid, j) => {
      const cx = labelW + j * cw;
      if (i === j) { svg.appendChild(svgEl('rect', { x: cx + 1, y: ry + 1, width: cw - 2, height: cw - 2, rx: 1, class: 'd-rr-diag' })); return; }
      const rl = loss[rid]; const cl = loss[cid];
      let cls = 'd-rr-cell d-flat';
      if (isNum(rl) && isNum(cl)) cls = 'd-rr-cell ' + (rl < cl ? 'd-good' : rl > cl ? 'd-bad' : 'd-flat');
      const cell = svgEl('rect', { x: cx + 1, y: ry + 1, width: cw - 2, height: cw - 2, rx: 1, class: cls },
        [title(`${rid} vs ${cid}: ${isNum(rl) ? fmt(rl) : '—'} vs ${isNum(cl) ? fmt(cl) : '—'}`)]);
      svg.appendChild(cell);
    });
  });
  return svg;
}

// ---- race lanes (ILLUSTRATIVE successive-halving) -------------------

export function raceLanes(opts) {
  const o = opts || {};
  const runners = (Array.isArray(o.runners) ? o.runners : []).filter((r) => r);
  const w = o.width || 420;
  const lh = o.laneHeight || 22;
  const labelW = o.labelWidth || 60;
  const h = Math.max(lh, runners.length * lh + 22);
  const svg = svgEl('svg', { class: 'd-race', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (runners.length === 0) return svg;
  const vals = runners.map((r) => r.loss).filter(isNum);
  let [lo, hi] = extent(vals);
  lo = Math.min(lo, 0);
  if (lo === hi) hi += 1;
  const x = scale([lo, hi], [labelW + 6, w - 10]);
  const best = vals.length ? Math.min(...vals) : null;
  if (best != null) svg.appendChild(svgEl('line', { x1: x(best), x2: x(best), y1: 14, y2: h - 4, class: 'd-race-finish' }, [title(`leader: ${fmt(best)}`)]));
  if (isNum(o.cut)) svg.appendChild(svgEl('line', { x1: x(o.cut), x2: x(o.cut), y1: 14, y2: h - 4, class: 'd-race-cut' }, [title(`elimination cut: ${fmt(o.cut)}`)]));
  const head = svgEl('text', { x: labelW + 6, y: 10, class: 'd-race-head' });
  head.textContent = 'loss → (left = ahead)';
  svg.appendChild(head);
  runners.forEach((r, i) => {
    const cy = 20 + i * lh + lh / 2;
    svg.appendChild(svgEl('line', { x1: labelW + 6, x2: w - 10, y1: cy, y2: cy, class: 'd-race-lane' }));
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'd-dot-label', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(r.id), 8);
    svg.appendChild(lbl);
    if (isNum(r.loss)) {
      const cls = 'd-race-dot' + (r.eliminated ? ' d-bad' : ' d-good');
      svg.appendChild(svgEl('circle', { cx: x(r.loss), cy, r: 3.4, class: cls }, [title(`${r.id}: ${fmt(r.loss)}${r.eliminated ? ' · eliminated' : ' · survives'}`)]));
    }
  });
  return svg;
}
