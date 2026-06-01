// variants/J/svg.js — dependency-free SVG data-viz primitives (Console).
//
// Self-contained for Variant J ("Console"). Mark CSS classes are `dj-*` and
// are styled — scoped under the variant root — by css/variants/J/console.css.
//
// Console is the dense, data-ink-maximal observatory: high data-ink, minimal
// chrome, small multiples packed tight, and — crucially for the convergence
// brief — NO pan/zoom viewports. Every mark fits its container. Three marks
// matter most here:
//   * a NON-COLLIDING bumps lineage (clickable nodes, de-collided lanes);
//   * a THEME-AWARE heatmap (opacity over a token fill, so it reads in
//     solarized-light / solarized-dark / monokai — never a hardcoded hex);
//   * a fit-to-width Tufte SANKEY (thin flows, direct labels, no viewport).
//
// Conventions:
//   * Lower drift/loss is BETTER (the gate ranks by it). Polarity-aware
//     helpers treat DOWN as good.
//   * Every primitive is total: empty/NaN input yields a quiet empty mark.
//   * Colours come from CSS custom properties (`--v2-*`) so the palette is
//     themed in one place, swapped by the [data-j-theme] attribute.

import { svgEl, el } from '../../core/dom.js';

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

// ---- sparkline ------------------------------------------------------

export function sparkline(opts) {
  const o = opts || {};
  const w = o.width || 120;
  const h = o.height || 28;
  const pad = 2;
  const raw = Array.isArray(o.values) ? o.values : [];
  const fin = finiteValues(raw);
  const svg = svgEl('svg', {
    class: 'dj-spark', width: w, height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', role: 'img',
  });
  if (fin.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h / 2, x2: w - pad, y2: h / 2, class: 'dj-spark-empty' }));
    return svg;
  }
  const [lo, hi] = extent(fin);
  const x = scale([0, Math.max(1, raw.length - 1)], [pad, w - pad]);
  const y = scale([lo, hi], [h - pad, pad]);
  if (o.band) {
    svg.appendChild(svgEl('rect', { x: pad, y: pad, width: w - 2 * pad, height: h - 2 * pad, class: 'dj-spark-band' }));
  }
  if (isNum(o.baseline)) {
    const by = y(o.baseline);
    svg.appendChild(svgEl('line', { x1: pad, x2: w - pad, y1: by, y2: by, class: 'dj-spark-baseline' }));
  }
  let d = '';
  let penDown = false;
  raw.forEach((v, i) => {
    if (!isNum(v)) { penDown = false; return; }
    d += `${penDown ? 'L' : 'M'}${x(i).toFixed(2)},${y(v).toFixed(2)} `;
    penDown = true;
  });
  svg.appendChild(svgEl('path', { d: d.trim(), class: 'dj-spark-line', fill: 'none' }));
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
      const cls = improved === null ? 'dj-spark-dot'
        : improved ? 'dj-spark-dot dj-good' : 'dj-spark-dot dj-bad';
      svg.appendChild(svgEl('circle', { cx: x(lastI), cy: y(raw[lastI]), r: 2.2, class: cls }, [title(fmt(raw[lastI]))]));
    }
  }
  return svg;
}

// ---- bumps chart (lineage as ranked lanes) --------------------------
//
// Champion lineage as a spine in its OWN lane; rejected challengers branch
// into a distinct offset lane so nothing collides. Within each lane we run a
// de-collision pass on the x positions so two nodes that share a generation
// index never overdraw (F's v1/v2 collided — they MUST NOT here). Clickable.
//
// opts: { width, height, nodes:[{id, x, promoted, scalar, parent}], onClick }
export function bumps(opts) {
  const o = opts || {};
  const nodes = (Array.isArray(o.nodes) ? o.nodes : []).filter((n) => n);
  const w = o.width || 640;
  const h = o.height || 170;
  const padX = 44; const spineY = h * 0.40; const challY = h * 0.80;
  const svg = svgEl('svg', { class: 'dj-bumps', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (nodes.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dj-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no generations yet';
    svg.appendChild(t);
    return svg;
  }
  const maxX = Math.max(1, ...nodes.map((n) => n.x || 0));
  const X = scale([0, maxX], [padX, w - padX]);

  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: spineY, y2: spineY, class: 'dj-lane-guide dj-spine-guide' }));
  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: challY, y2: challY, class: 'dj-lane-guide' }));
  const lblS = svgEl('text', { x: 6, y: spineY - 8, class: 'dj-lane-label' }); lblS.textContent = 'champion';
  const lblC = svgEl('text', { x: 6, y: challY - 8, class: 'dj-lane-label' }); lblC.textContent = 'challenger';
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
    svg.appendChild(svgEl('line', { x1: nodeX(promoted[i - 1]), y1: spineY, x2: nodeX(promoted[i]), y2: spineY, class: 'dj-spine-line' }));
  }
  for (const n of nodes) {
    if (n.promoted) continue;
    const p = n.parent ? byId.get(n.parent) : null;
    const px = p ? nodeX(p) : nodeX(n) - 40;
    const py = p ? laneY(p) : spineY;
    const nx = nodeX(n);
    const path = `M${px},${py} C${(px + nx) / 2},${py} ${(px + nx) / 2},${challY} ${nx},${challY}`;
    svg.appendChild(svgEl('path', { d: path, class: 'dj-branch', fill: 'none' }));
  }
  for (const n of nodes) {
    const cy = laneY(n);
    const px = nodeX(n);
    const cls = 'dj-bump-node ' + (n.promoted ? 'dj-promoted' : 'dj-rejected');
    const c = svgEl('circle', { cx: px, cy, r: n.promoted ? 4.5 : 3.5, class: cls, tabindex: o.onClick ? '0' : null },
      [title(`${n.id}${isNum(n.scalar) ? ' · ' + fmt(n.scalar) : ''} · ${n.promoted ? 'promoted' : 'rejected'}`)]);
    if (o.onClick) {
      c.style.cursor = 'pointer';
      c.addEventListener('click', () => o.onClick(n));
      c.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onClick(n); } });
    }
    svg.appendChild(c);
    const t = svgEl('text', { x: px, y: cy + 16, class: 'dj-bump-label', 'text-anchor': 'middle' });
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

// ---- theme-aware heatmap --------------------------------------------
//
// Small-multiples heatmap: rows × cols coloured by value. Console makes it
// THEME-SAFE — the cell fill is the themed ink token at a value-driven
// OPACITY (instead of a hardcoded hex ramp), so a high-loss cell reads as
// "more ink" in every theme (solarized-light / -dark / monokai). Hover →
// exact value. Click → drill.
//
// opts: { rows, cols, value(rowId,colId), cellW, cellH, labelWidth,
//         headHeight, onClick }
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
  const svg = svgEl('svg', { class: 'dj-heatmap', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (rows.length === 0 || cols.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'dj-empty-label' });
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
    const t = svgEl('text', { x: cx, y: headH - 6, class: 'dj-hm-col', transform: `rotate(-45 ${cx} ${headH - 6})`, 'text-anchor': 'start' });
    t.textContent = shortLabel(c.label);
    svg.appendChild(t);
  });
  rows.forEach((r, i) => {
    const ry = headH + i * ch;
    const lbl = svgEl('text', { x: labelW - 6, y: ry + ch - 4, class: 'dj-hm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(r.label);
    svg.appendChild(lbl);
    cols.forEach((c, j) => {
      const v = o.value(r.id, c.id);
      const cx = labelW + j * cw;
      const t = isNum(v) ? (v - lo) / span : null;
      // Theme-safe: empty cells get the empty token; valued cells use the
      // ink-loss token at a floor-lifted opacity so even a low cell is
      // visible and a high cell reads as denser ink in ALL three themes.
      const cls = t == null ? 'dj-hm-cell dj-hm-empty' : 'dj-hm-cell';
      const op = t == null ? null : (0.18 + 0.82 * Math.max(0, Math.min(1, t))).toFixed(3);
      const attrs = { x: cx + 1, y: ry + 1, width: cw - 2, height: ch - 2, rx: 1.5, class: cls };
      if (op != null) attrs['fill-opacity'] = op;
      const cell = svgEl('rect', attrs, [title(`${r.label} × ${c.label}: ${isNum(v) ? fmt(v) : '—'}`)]);
      if (o.onClick) {
        cell.style.cursor = 'pointer';
        cell.addEventListener('click', () => o.onClick(r.id, c.id));
      }
      svg.appendChild(cell);
    });
  });
  return svg;
}

// ---- value dot-plot with a reference line ---------------------------
//
// Per-board scoring (the D dot-plot the brief insists must read in all three
// themes, esp. monokai): absolute per-entry loss, with a reference line at
// the champion's level. Dots below the reference are 'good', above 'bad'. A
// pass/fail/timeout glyph trails each row. Click → drill.
//
// opts: { width, rowHeight, labelWidth, items:[{label,value,id,pass,timeout}],
//         reference:{value,label}, onClick }
export function valueDotPlot(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d);
  const w = o.width || 460;
  const rh = o.rowHeight || 19;
  const labelW = o.labelWidth || 170;
  const glyphW = 16;
  const h = Math.max(rh, items.length * rh + 8);
  const svg = svgEl('svg', { class: 'dj-valdot', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'dj-empty-label' });
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
    svg.appendChild(svgEl('line', { x1: rx, x2: rx, y1: 2, y2: h - 2, class: 'dj-ref-rule' },
      [title(`${(o.reference.label || 'reference')}: ${fmt(ref)}`)]));
  }
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 4;
    const g = svgEl('g', { class: 'dj-dotrow', tabindex: o.onClick ? '0' : null });
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'dj-dot-label', 'text-anchor': 'end' });
    lbl.textContent = d.label != null ? shortLabel(String(d.label), 22) : '';
    g.appendChild(lbl);
    if (isNum(d.value)) {
      const dx = x(d.value);
      g.appendChild(svgEl('line', { x1: x(lo), x2: dx, y1: cy, y2: cy, class: 'dj-dot-connector' }));
      const good = ref != null ? d.value < ref : false;
      const worse = ref != null ? d.value > ref : false;
      const cls = 'dj-dot ' + (good ? 'dj-good' : worse ? 'dj-bad' : '');
      g.appendChild(svgEl('circle', { cx: dx, cy, r: 3.2, class: cls },
        [title(`${d.label}: ${fmt(d.value)}${ref != null ? ` (vs champ ${fmt(ref)})` : ''}`)]));
      g.appendChild(outcomeGlyph(d, w - glyphW + 2, cy));
    } else {
      const t = svgEl('text', { x: x(lo) + 6, y: cy + 3, class: 'dj-dot-missing' });
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

// ---- sparkbar (micro loss bars + a verdict marker) ------------------

export function sparkbar(opts) {
  const o = opts || {};
  const bars = (Array.isArray(o.bars) ? o.bars : []).filter((b) => b);
  const w = o.width || 120;
  const h = o.height || 30;
  const pad = 2;
  const footH = 2;
  const svg = svgEl('svg', { class: 'dj-sparkbar', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (bars.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h - footH, x2: w - pad, y2: h - footH, class: 'dj-spark-empty' }));
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
      const cls = 'dj-sparkbar-bar' + (b.timeout ? ' dj-timeout' : '') + (b.fail ? ' dj-fail' : '');
      svg.appendChild(svgEl('rect', { x: cx - bw / 2, y: Math.min(y, y0), width: bw, height: Math.max(1, Math.abs(y0 - y)), class: cls },
        [title(`${b.label}: ${fmt(b.value)}${b.timeout ? ' · timed out' : ''}${b.fail ? ' · failed' : ''}`)]));
    } else {
      svg.appendChild(svgEl('line', { x1: cx, y1: y0 - 1, x2: cx, y2: y0 - 4, class: 'dj-sparkbar-missing' }, [title(`${b.label}: no run`)]));
    }
  });
  svg.appendChild(svgEl('line', { x1: pad, y1: y0, x2: w - pad, y2: y0, class: 'dj-sparkbar-foot' }));
  if (o.verdict === 'promoted' || o.verdict === 'rejected') {
    const good = o.verdict === 'promoted';
    const gx = w - pad - 3; const gy = pad + 4; const r = 3.2;
    const tri = good ? `${gx},${gy - r} ${gx - r},${gy + r} ${gx + r},${gy + r}` : `${gx},${gy + r} ${gx - r},${gy - r} ${gx + r},${gy - r}`;
    svg.appendChild(svgEl('polygon', { points: tri, class: 'dj-verdict-glyph ' + (good ? 'dj-good' : 'dj-bad') }, [title(o.verdict)]));
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
  const svg = svgEl('svg', { class: 'dj-genrow', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  const n = Math.max(1, cells.length);
  const slot = (w - 2 * pad) / n;
  cells.forEach((c, i) => { svg.appendChild(outcomeGlyph(c, pad + slot * (i + 0.5), h / 2)); });
  return svg;
}

function outcomeGlyph(d, x, cy) {
  if (d && d.ran === false) return svgEl('circle', { cx: x, cy, r: 2.2, class: 'dj-glyph-none' }, [title('no run')]);
  if (d.timeout) return svgEl('text', { x, y: cy + 3, class: 'dj-glyph-timeout', 'text-anchor': 'middle' }, [title('budget exceeded (timeout)'), '⏱']);
  if (d.pass === true || d.pass === 1) return svgEl('circle', { cx: x, cy, r: 2.4, class: 'dj-glyph-pass' }, [title('passed')]);
  if (d.pass === false || d.pass === 0) {
    const g = svgEl('g', null, [title('failed')]);
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy - 2.4, x2: x + 2.4, y2: cy + 2.4, class: 'dj-glyph-fail' }));
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy + 2.4, x2: x + 2.4, y2: cy - 2.4, class: 'dj-glyph-fail' }));
    return g;
  }
  return svgEl('circle', { cx: x, cy, r: 2.2, class: 'dj-glyph-none' }, [title('no predicate')]);
}

// ---- horizontal value bars (per-judge losses) ----------------------

export function valueBars(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d && isNum(d.value));
  const w = o.width || 360;
  const rh = o.rowHeight || 18;
  const labelW = o.labelWidth || 150;
  const h = Math.max(rh, items.length * rh + 6);
  const svg = svgEl('svg', { class: 'dj-vbars', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 14, class: 'dj-empty-label' });
    t.textContent = 'no values';
    svg.appendChild(t);
    return svg;
  }
  const hi = Math.max(1e-9, ...items.map((d) => Math.abs(d.value)));
  const x0 = labelW + 4;
  const x = scale([0, hi], [x0, w - 36]);
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 3;
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'dj-dot-label', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(d.label), 20);
    svg.appendChild(lbl);
    const bx = x(Math.abs(d.value));
    svg.appendChild(svgEl('rect', { x: x0, y: cy - 4, width: Math.max(1, bx - x0), height: 8, rx: 1, class: 'dj-vbar' }, [title(`${d.label}: ${fmt(d.value)}`)]));
    const vt = svgEl('text', { x: bx + 4, y: cy + 3, class: 'dj-vbar-val' });
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

  const svg = svgEl('svg', { class: 'dj-pslope', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (series.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dj-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no paired board duels yet';
    svg.appendChild(t);
    return svg;
  }
  const allVals = [];
  for (const s of series) { if (isNum(s.a)) allVals.push(s.a); if (isNum(s.b)) allVals.push(s.b); }
  const [lo, hi] = extent(allVals);
  const y = scale([lo, hi], [h - padBottom, padTop]);

  const hL = svgEl('text', { x: leftX, y: 15, class: 'dj-slope-col', 'text-anchor': 'end' });
  hL.textContent = (o.left && o.left.title) || 'champion';
  const hR = svgEl('text', { x: rightX, y: 15, class: 'dj-slope-col', 'text-anchor': 'start' });
  hR.textContent = (o.right && o.right.title) || 'challenger';
  svg.appendChild(hL); svg.appendChild(hR);
  svg.appendChild(svgEl('line', { x1: leftX, x2: leftX, y1: y(hi), y2: y(lo), class: 'dj-slope-axis' }));
  svg.appendChild(svgEl('line', { x1: rightX, x2: rightX, y1: y(hi), y2: y(lo), class: 'dj-slope-axis' }));

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
    const dirCls = verdict === 'improved' ? 'dj-good' : verdict === 'regressed' ? 'dj-bad' : 'dj-flat';
    const g = svgEl('g', { class: 'dj-pslope-series' });
    if (ay != null && by != null) {
      const line = svgEl('line', { x1: leftX, y1: ay, x2: rightX, y2: by, class: 'dj-pslope-line ' + dirCls });
      line.appendChild(title(`${s.label}: ${fmt(s.a)} → ${fmt(s.b)} (${fmtSigned(s.b - s.a)}; ${verdict})`));
      g.appendChild(line);
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'dj-pslope-node ' + dirCls }));
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'dj-pslope-node ' + dirCls }));
    } else if (ay != null) {
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'dj-pslope-node dj-flat' }, [title(`${s.label}: champion only ${fmt(s.a)}`)]));
    } else if (by != null) {
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'dj-pslope-node dj-flat' }, [title(`${s.label}: challenger only ${fmt(s.b)}`)]));
    }
    const ll = leftLabels[i];
    if (isNum(s.a)) {
      if (Math.abs(ll - y(s.a)) > 1.5) g.appendChild(svgEl('line', { x1: leftX - 4, y1: ll, x2: leftX, y2: y(s.a), class: 'dj-leader' }));
      const tx = svgEl('text', { x: leftX - 8, y: ll + 3, class: 'dj-pslope-label', 'text-anchor': 'end' });
      tx.textContent = `${shortLabel(s.label, 14)}  ${fmt(s.a, 1)}`;
      g.appendChild(tx);
    }
    const rl = rightLabels[i];
    if (isNum(s.b)) {
      if (Math.abs(rl - y(s.b)) > 1.5) g.appendChild(svgEl('line', { x1: rightX, y1: y(s.b), x2: rightX + 4, y2: rl, class: 'dj-leader' }));
      const tx = svgEl('text', { x: rightX + 8, y: rl + 3, class: 'dj-pslope-label', 'text-anchor': 'start' });
      tx.textContent = `${fmt(s.b, 1)}  ${shortLabel(s.label, 14)}`;
      g.appendChild(tx);
    }
    if (o.onClick) { g.style.cursor = 'pointer'; g.addEventListener('click', () => o.onClick(s)); }
    svg.appendChild(g);
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
  const svg = svgEl('svg', { class: 'dj-bracket', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (seats.length === 0) return svg;
  const seatW = 96; const col1 = 8; const col2 = w - seatW - 8; const midX = (col1 + seatW + col2) / 2;
  const winnerY = h / 2;
  seats.forEach((s, i) => {
    const y = 12 + i * rowH;
    const won = s.id === o.winner;
    svg.appendChild(svgEl('rect', { x: col1, y, width: seatW, height: 18, rx: 2, class: 'dj-bracket-seat' + (won ? ' dj-win' : '') + (s.champion ? ' dj-champ' : '') }));
    const t = svgEl('text', { x: col1 + 6, y: y + 13, class: 'dj-bracket-label' });
    t.textContent = shortLabel(String(s.id), 12);
    svg.appendChild(t);
    const cy = y + 9;
    svg.appendChild(svgEl('path', { d: `M${col1 + seatW},${cy} H${midX} V${winnerY + 9} H${col2}`, class: 'dj-bracket-edge' + (won ? ' dj-win' : ''), fill: 'none' }));
  });
  const fy = winnerY;
  svg.appendChild(svgEl('rect', { x: col2, y: fy, width: seatW, height: 18, rx: 2, class: 'dj-bracket-seat dj-win dj-champ' }));
  const wt = svgEl('text', { x: col2 + 6, y: fy + 13, class: 'dj-bracket-label' });
  wt.textContent = o.winner ? `${shortLabel(String(o.winner), 9)} ✦` : 'tbd';
  svg.appendChild(wt);
  return svg;
}

// ---- round-robin matrix (ILLUSTRATIVE) ------------------------------

export function roundRobinMatrix(opts) {
  const o = opts || {};
  const ids = Array.isArray(o.ids) ? o.ids : [];
  const cw = o.cell || 30;
  const labelW = o.label || 44;
  const headH = 18;
  const w = labelW + ids.length * cw + 4;
  const h = headH + ids.length * cw + 4;
  const svg = svgEl('svg', { class: 'dj-rrmatrix', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (ids.length === 0) return svg;
  const loss = o.lossById || {};
  ids.forEach((cid, j) => {
    const t = svgEl('text', { x: labelW + j * cw + cw / 2, y: headH - 5, class: 'dj-rr-head', 'text-anchor': 'middle' });
    t.textContent = shortLabel(String(cid), 5);
    svg.appendChild(t);
  });
  ids.forEach((rid, i) => {
    const ry = headH + i * cw;
    const lbl = svgEl('text', { x: labelW - 5, y: ry + cw / 2 + 3, class: 'dj-rr-head', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(rid), 6);
    svg.appendChild(lbl);
    ids.forEach((cid, j) => {
      const cx = labelW + j * cw;
      if (i === j) { svg.appendChild(svgEl('rect', { x: cx + 1, y: ry + 1, width: cw - 2, height: cw - 2, rx: 1, class: 'dj-rr-diag' })); return; }
      const rl = loss[rid]; const cl = loss[cid];
      let cls = 'dj-rr-cell dj-flat';
      if (isNum(rl) && isNum(cl)) cls = 'dj-rr-cell ' + (rl < cl ? 'dj-good' : rl > cl ? 'dj-bad' : 'dj-flat');
      svg.appendChild(svgEl('rect', { x: cx + 1, y: ry + 1, width: cw - 2, height: cw - 2, rx: 1, class: cls },
        [title(`${rid} vs ${cid}: ${isNum(rl) ? fmt(rl) : '—'} vs ${isNum(cl) ? fmt(cl) : '—'} — row ${isNum(rl) && isNum(cl) ? (rl < cl ? 'wins' : rl > cl ? 'loses' : 'ties') : '?'}`)]));
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
  const svg = svgEl('svg', { class: 'dj-race', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (runners.length === 0) return svg;
  const vals = runners.map((r) => r.loss).filter(isNum);
  let [lo, hi] = extent(vals);
  lo = Math.min(lo, 0);
  if (lo === hi) hi += 1;
  const x = scale([lo, hi], [labelW + 6, w - 10]);
  const best = vals.length ? Math.min(...vals) : null;
  if (best != null) svg.appendChild(svgEl('line', { x1: x(best), x2: x(best), y1: 14, y2: h - 4, class: 'dj-race-finish' }, [title(`leader: ${fmt(best)}`)]));
  if (isNum(o.cut)) svg.appendChild(svgEl('line', { x1: x(o.cut), x2: x(o.cut), y1: 14, y2: h - 4, class: 'dj-race-cut' }, [title(`elimination cut: ${fmt(o.cut)}`)]));
  const head = svgEl('text', { x: labelW + 6, y: 10, class: 'dj-race-head' });
  head.textContent = 'loss → (left = ahead)';
  svg.appendChild(head);
  runners.forEach((r, i) => {
    const cy = 20 + i * lh + lh / 2;
    svg.appendChild(svgEl('line', { x1: labelW + 6, x2: w - 10, y1: cy, y2: cy, class: 'dj-race-lane' }));
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'dj-dot-label', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(r.id), 8);
    svg.appendChild(lbl);
    if (isNum(r.loss)) {
      const cls = 'dj-race-dot' + (r.eliminated ? ' dj-bad' : ' dj-good');
      svg.appendChild(svgEl('circle', { cx: x(r.loss), cy, r: 3.4, class: cls }, [title(`${r.id}: ${fmt(r.loss)}${r.eliminated ? ' · eliminated' : ' · survives'}`)]));
    }
  });
  return svg;
}

// ---- Tufte Sankey (fit-to-width, NO viewport) -----------------------
//
// THE convergence brief's headline figure: the causal flow
//   PATCH (mutation points) → DRIFT KINDS (per-board loss) → GATE verdict
// re-skinned Tufte. It is laid out to FIT the container width (responsive,
// no pan/zoom), with thin flows, direct in-place labels, restrained
// improve/regress colour, and no decorative gradients/shadows. The layout
// math is the same proportional-throughput algorithm the original sankey
// used, re-implemented here so Variant J stays self-contained.
//
// opts: { width, height, patch:[{id,label,sub,value}], drift:[…], gate:[…],
//         links:[{source,target,value,cls}], onNode(node) }
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
    class: 'dj-sankey', width: '100%', height: box.h,
    viewBox: `0 0 ${box.w} ${box.h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
  });
  if (nodes.length === 0) {
    const t = svgEl('text', { x: box.w / 2, y: box.h / 2, class: 'dj-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no causal flow yet';
    svg.appendChild(t);
    return svg;
  }
  const stageHead = { patch: 'PATCH', drift: 'PER-BOARD DRIFT', gate: 'GATE' };
  const byStage = {};
  for (const n of nodes) (byStage[n.stage] = byStage[n.stage] || []).push(n);
  for (const stage of Object.keys(byStage)) {
    const x = byStage[stage][0].x + byStage[stage][0].w / 2;
    const t = svgEl('text', { x, y: 14, class: 'dj-sankey-head', 'text-anchor': 'middle' });
    t.textContent = stageHead[stage] || stage;
    svg.appendChild(t);
  }
  // ribbons (drawn first, behind nodes) — thin filled paths.
  const linkLayer = svgEl('g', { class: 'dj-sankey-links' });
  for (const l of links) {
    const mx = (l.sx + l.tx) / 2;
    const d = `M ${l.sx} ${l.sy - l.hwS} `
      + `C ${mx} ${l.sy - l.hwS}, ${mx} ${l.ty - l.hwT}, ${l.tx} ${l.ty - l.hwT} `
      + `L ${l.tx} ${l.ty + l.hwT} `
      + `C ${mx} ${l.ty + l.hwT}, ${mx} ${l.sy + l.hwS}, ${l.sx} ${l.sy + l.hwS} Z`;
    linkLayer.appendChild(svgEl('path', { d, class: 'dj-sankey-ribbon ' + (l.cls || ''), fill: 'currentColor' }, [title(`${l.source} → ${l.target}: ${fmt(l.value, 1)}`)]));
  }
  svg.appendChild(linkLayer);
  // nodes — thin bars + direct in-place labels.
  const nodeLayer = svgEl('g', { class: 'dj-sankey-nodes' });
  for (const n of nodes) {
    const g = svgEl('g', { class: 'dj-sankey-node ' + (n.cls || ''), tabindex: o.onNode ? '0' : null });
    g.appendChild(svgEl('rect', { x: n.x, y: n.y, width: 6, height: n.h, rx: 1, class: 'dj-sankey-bar' }, [title(`${n.label}${isNum(n.value) ? ' · ' + fmt(n.value, 1) : ''}`)]));
    const anchor = n.stage === 'gate' ? 'end' : 'start';
    const lx = n.stage === 'gate' ? n.x - 6 : n.x + 12;
    const ty = n.y + n.h / 2;
    const lbl = svgEl('text', { x: lx, y: ty - 1, class: 'dj-sankey-label', 'text-anchor': anchor });
    lbl.textContent = shortLabel(String(n.label), 22);
    g.appendChild(lbl);
    if (n.sub) {
      const sub = svgEl('text', { x: lx, y: ty + 11, class: 'dj-sankey-sub', 'text-anchor': anchor });
      sub.textContent = shortLabel(String(n.sub), 24);
      g.appendChild(sub);
    }
    if (o.onNode) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => o.onNode(n));
      g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onNode(n); } });
    }
    nodeLayer.appendChild(g);
  }
  svg.appendChild(nodeLayer);
  return svg;
}

// ---- small-multiple wrapper -----------------------------------------

export function smallMultiple(caption, mark, sub) {
  return el('figure', { class: 'dj-sm' }, [
    el('figcaption', { class: 'dj-sm-cap' }, [
      el('span', { class: 'dj-sm-title', text: caption == null ? '' : String(caption) }),
      sub ? el('span', { class: 'dj-sm-sub', text: String(sub) }) : null,
    ]),
    mark,
  ]);
}
