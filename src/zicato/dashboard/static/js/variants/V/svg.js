// variants/N/svg.js — dependency-free SVG data-viz primitives (Console).
//
// Self-contained for Variant N ("Console II"). Mark CSS classes are `dn-*` and
// are styled — scoped under the variant root — by css/variants/N/console.css.
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
//     themed in one place, swapped by the [data-n-theme] attribute.

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
    class: 'dn-spark', width: w, height: h,
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
  const svg = svgEl('svg', { class: 'dn-bumps', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
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
    const c = svgEl('circle', { cx: px, cy, r: n.promoted ? 4.5 : 3.5, class: cls, tabindex: o.onClick ? '0' : null },
      [title(`${n.id}${isNum(n.scalar) ? ' · ' + fmt(n.scalar) : ''} · ${n.promoted ? 'promoted' : 'rejected'}`)]);
    if (o.onClick) {
      c.style.cursor = 'pointer';
      c.addEventListener('click', () => o.onClick(n));
      c.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onClick(n); } });
    }
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
  const svg = svgEl('svg', { class: 'dn-heatmap', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
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
      // Theme-safe: empty cells get the empty token; valued cells use the
      // ink-loss token at a floor-lifted opacity so even a low cell is
      // visible and a high cell reads as denser ink in ALL three themes.
      const cls = t == null ? 'dn-hm-cell dn-hm-empty' : 'dn-hm-cell';
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
  const svg = svgEl('svg', { class: 'dn-valdot', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
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
    svg.appendChild(svgEl('line', { x1: rx, x2: rx, y1: 2, y2: h - 2, class: 'dn-ref-rule' },
      [title(`${(o.reference.label || 'reference')}: ${fmt(ref)}`)]));
  }
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 4;
    const g = svgEl('g', { class: 'dn-dotrow', tabindex: o.onClick ? '0' : null });
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'dn-dot-label', 'text-anchor': 'end' });
    lbl.textContent = d.label != null ? shortLabel(String(d.label), 22) : '';
    g.appendChild(lbl);
    if (isNum(d.value)) {
      const dx = x(d.value);
      g.appendChild(svgEl('line', { x1: x(lo), x2: dx, y1: cy, y2: cy, class: 'dn-dot-connector' }));
      const good = ref != null ? d.value < ref : false;
      const worse = ref != null ? d.value > ref : false;
      const cls = 'dn-dot ' + (good ? 'dn-good' : worse ? 'dn-bad' : '');
      g.appendChild(svgEl('circle', { cx: dx, cy, r: 3.2, class: cls },
        [title(`${d.label}: ${fmt(d.value)}${ref != null ? ` (vs champ ${fmt(ref)})` : ''}`)]));
      g.appendChild(outcomeGlyph(d, w - glyphW + 2, cy));
    } else {
      const t = svgEl('text', { x: x(lo) + 6, y: cy + 3, class: 'dn-dot-missing' });
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
  const svg = svgEl('svg', { class: 'dn-sparkbar', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
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
      svg.appendChild(svgEl('rect', { x: cx - bw / 2, y: Math.min(y, y0), width: bw, height: Math.max(1, Math.abs(y0 - y)), class: cls },
        [title(`${b.label}: ${fmt(b.value)}${b.timeout ? ' · timed out' : ''}${b.fail ? ' · failed' : ''}`)]));
    } else {
      svg.appendChild(svgEl('line', { x1: cx, y1: y0 - 1, x2: cx, y2: y0 - 4, class: 'dn-sparkbar-missing' }, [title(`${b.label}: no run`)]));
    }
  });
  svg.appendChild(svgEl('line', { x1: pad, y1: y0, x2: w - pad, y2: y0, class: 'dn-sparkbar-foot' }));
  if (o.verdict === 'promoted' || o.verdict === 'rejected') {
    const good = o.verdict === 'promoted';
    const gx = w - pad - 3; const gy = pad + 4; const r = 3.2;
    const tri = good ? `${gx},${gy - r} ${gx - r},${gy + r} ${gx + r},${gy + r}` : `${gx},${gy + r} ${gx - r},${gy - r} ${gx + r},${gy - r}`;
    svg.appendChild(svgEl('polygon', { points: tri, class: 'dn-verdict-glyph ' + (good ? 'dn-good' : 'dn-bad') }, [title(o.verdict)]));
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
  const svg = svgEl('svg', { class: 'dn-genrow', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  const n = Math.max(1, cells.length);
  const slot = (w - 2 * pad) / n;
  cells.forEach((c, i) => { svg.appendChild(outcomeGlyph(c, pad + slot * (i + 0.5), h / 2)); });
  return svg;
}

function outcomeGlyph(d, x, cy) {
  if (d && d.ran === false) return svgEl('circle', { cx: x, cy, r: 2.2, class: 'dn-glyph-none' }, [title('no run')]);
  if (d.timeout) return svgEl('text', { x, y: cy + 3, class: 'dn-glyph-timeout', 'text-anchor': 'middle' }, [title('budget exceeded (timeout)'), '⏱']);
  if (d.pass === true || d.pass === 1) return svgEl('circle', { cx: x, cy, r: 2.4, class: 'dn-glyph-pass' }, [title('passed')]);
  if (d.pass === false || d.pass === 0) {
    const g = svgEl('g', null, [title('failed')]);
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy - 2.4, x2: x + 2.4, y2: cy + 2.4, class: 'dn-glyph-fail' }));
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy + 2.4, x2: x + 2.4, y2: cy - 2.4, class: 'dn-glyph-fail' }));
    return g;
  }
  return svgEl('circle', { cx: x, cy, r: 2.2, class: 'dn-glyph-none' }, [title('no predicate')]);
}

// ---- horizontal value bars (per-judge losses) ----------------------

export function valueBars(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d && isNum(d.value));
  const w = o.width || 360;
  const rh = o.rowHeight || 18;
  const labelW = o.labelWidth || 150;
  const h = Math.max(rh, items.length * rh + 6);
  const svg = svgEl('svg', { class: 'dn-vbars', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
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
    svg.appendChild(svgEl('rect', { x: x0, y: cy - 4, width: Math.max(1, bx - x0), height: 8, rx: 1, class: 'dn-vbar' }, [title(`${d.label}: ${fmt(d.value)}`)]));
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

  const svg = svgEl('svg', { class: 'dn-pslope', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
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
      line.appendChild(title(`${s.label}: ${fmt(s.a)} → ${fmt(s.b)} (${fmtSigned(s.b - s.a)}; ${verdict})`));
      g.appendChild(line);
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'dn-pslope-node ' + dirCls }));
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'dn-pslope-node ' + dirCls }));
    } else if (ay != null) {
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'dn-pslope-node dn-flat' }, [title(`${s.label}: champion only ${fmt(s.a)}`)]));
    } else if (by != null) {
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'dn-pslope-node dn-flat' }, [title(`${s.label}: challenger only ${fmt(s.b)}`)]));
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
    if (o.onClick) { g.style.cursor = 'pointer'; g.addEventListener('click', () => o.onClick(s)); }
    svg.appendChild(g);
  });
  return svg;
}

// (Removed for Variant V: the illustrative tournament-style marks
// (bracketMini / roundRobinMatrix / raceLanes), the Sankey, and the
// small-multiple wrapper are not rendered by any Reel view — trimmed to
// keep the variant lean.)

// ---- SIDE-BY-SIDE line diff (mutation view, fix #2) -----------------
//
// Two columns — champion baseline (left) | challenger new (right) — line-diffed
// by the classic LCS so common lines align row-for-row and changed lines are
// marked. Both arguments are STRINGS (the "[object Object]" bug = passing the
// `baseline` object instead of `.baseline.content`). Returns a DOM node (NOT an
// SVG) — a real two-column table so long lines wrap inside each cell.
//
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
