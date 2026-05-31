// variants/D/svg.js — dependency-free SVG data-viz primitives.
//
// Variant D is "Tufte data-visualization, done beautifully": high
// data-ink, minimal chrome, small multiples everywhere, sparklines,
// range-frames, and a *non-colliding* slopegraph / bumps chart. This
// module is the entire drawing toolkit — pure functions that return
// detached SVG nodes built with the core `svgEl` helper. No external
// charting library, no canvas; every mark is an addressable SVG node so
// hover and click are first-class.
//
// Conventions:
//   * Lower drift/loss is BETTER (the tournament gate ranks by it).
//     Helpers that know the polarity (sparkline trend colour,
//     slopegraph direction) treat DOWN as good.
//   * Every primitive is total: empty/NaN input yields a quiet empty
//     mark, never a throw.
//   * Colours come from CSS custom properties (`--v2-*`) where possible,
//     so the palette is themed in one place.

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

// A linear scale from a [domainLo, domainHi] to [rangeLo, rangeHi].
export function scale(domain, range) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  return (x) => r0 + ((x - d0) / span) * (r1 - r0);
}

// Format a number compactly for hover titles + Tufte direct labels.
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

// A native <title> child — the dependency-free hover tooltip. Every
// interactive mark gets one so "hover → exact value" works everywhere.
export function title(text) {
  const t = svgEl('title', null);
  t.textContent = text == null ? '' : String(text);
  return t;
}

// ---- sparkline ------------------------------------------------------
//
// Tufte's signature mark: a small, intense, word-sized graphic. We draw
// the line, an optional faint range-band (min..max), and a single
// emphasised end dot (Tufte's "current value" dot). Gaps (null/NaN)
// break the line rather than interpolating a lie.
//
// opts: { width, height, values, band, endDot,
//         goodDirection:'down'|'up', baseline }
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
    svg.appendChild(svgEl('line', {
      x1: pad, y1: h / 2, x2: w - pad, y2: h / 2, class: 'd-spark-empty',
    }));
    return svg;
  }
  const [lo, hi] = extent(fin);
  const x = scale([0, Math.max(1, raw.length - 1)], [pad, w - pad]);
  const y = scale([lo, hi], [h - pad, pad]);

  if (o.band) {
    svg.appendChild(svgEl('rect', {
      x: pad, y: pad, width: w - 2 * pad, height: h - 2 * pad, class: 'd-spark-band',
    }));
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
      svg.appendChild(svgEl('circle', { cx: x(lastI), cy: y(raw[lastI]), r: 2.2, class: cls },
        [title(fmt(raw[lastI]))]));
    }
  }
  return svg;
}

// ---- range-frame dot/bar plot --------------------------------------
//
// A sorted horizontal dot plot for per-entry deltas. Each row is a
// label + a dot positioned on a shared signed axis with a zero rule.
// Negative (improved) dots are coloured 'good', positive 'bad'.
//
// opts: { width, rowHeight, labelWidth, items:[{label, value, id}],
//         onClick(item), goodDirection, valueFmt }
export function dotPlot(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d);
  const w = o.width || 360;
  const rh = o.rowHeight || 18;
  const labelW = o.labelWidth || 120;
  const h = Math.max(rh, items.length * rh + 6);
  const vfmt = o.valueFmt || fmtSigned;
  const goodDown = (o.goodDirection || 'down') === 'down';

  const svg = svgEl('svg', { class: 'd-dotplot', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (items.length === 0) return svg;

  const vals = items.map((d) => d.value);
  let [lo, hi] = extent(vals);
  lo = Math.min(lo, 0); hi = Math.max(hi, 0);
  if (lo === hi) { lo -= 0.5; hi += 0.5; }
  const x = scale([lo, hi], [labelW + 4, w - 4]);
  const zx = x(0);

  svg.appendChild(svgEl('line', { x1: zx, x2: zx, y1: 2, y2: h - 2, class: 'd-zero-rule' }));

  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 3;
    const g = svgEl('g', { class: 'd-dotrow', tabindex: o.onClick ? '0' : null });
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'd-dot-label', 'text-anchor': 'end' });
    lbl.textContent = d.label != null ? String(d.label) : '';
    g.appendChild(lbl);
    if (isNum(d.value)) {
      const dx = x(d.value);
      g.appendChild(svgEl('line', { x1: zx, x2: dx, y1: cy, y2: cy, class: 'd-dot-connector' }));
      const good = goodDown ? d.value < 0 : d.value > 0;
      const cls = d.value === 0 ? 'd-dot' : good ? 'd-dot d-good' : 'd-dot d-bad';
      g.appendChild(svgEl('circle', { cx: dx, cy, r: 3, class: cls },
        [title(`${d.label}: ${vfmt(d.value)}`)]));
    } else {
      const t = svgEl('text', { x: zx + 6, y: cy + 3, class: 'd-dot-missing' });
      t.textContent = '—';
      g.appendChild(t);
    }
    if (o.onClick) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => o.onClick(d));
      g.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onClick(d); }
      });
    }
    svg.appendChild(g);
  });
  return svg;
}

// ---- slopegraph (non-colliding) ------------------------------------
//
// Tufte's slopegraph: two columns of values joined by lines whose SLOPE
// is the story. The classic failure is colliding/overlapping labels. We
// fix it the right way: after positioning labels at their value we run a
// one-dimensional "push apart" pass on EACH column so labels keep a
// minimum vertical gap, then draw a small leader from the de-collided
// label position back to the true datum on the axis. The connecting line
// still terminates at the TRUE value, never the nudged label.
//
// opts: { width, height, left:{title}, right:{title}, labelGap,
//         series:[{label, id, a, b, emphasis}], goodDirection, onClick }
export function slopegraph(opts) {
  const o = opts || {};
  const series = (Array.isArray(o.series) ? o.series : [])
    .filter((s) => s && (isNum(s.a) || isNum(s.b)));
  const w = o.width || 420;
  const h = o.height || 260;
  const padTop = 26; const padBottom = 16;
  const colGap = o.labelGap || 150;
  const leftX = colGap;
  const rightX = w - colGap;
  const goodDown = (o.goodDirection || 'down') === 'down';

  const svg = svgEl('svg', { class: 'd-slope', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (series.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'd-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no lineage yet';
    svg.appendChild(t);
    return svg;
  }

  const allVals = [];
  for (const s of series) { if (isNum(s.a)) allVals.push(s.a); if (isNum(s.b)) allVals.push(s.b); }
  const [lo, hi] = extent(allVals);
  const y = scale([lo, hi], [h - padBottom, padTop]);

  const hL = svgEl('text', { x: leftX, y: 14, class: 'd-slope-col', 'text-anchor': 'end' });
  hL.textContent = (o.left && o.left.title) || 'before';
  const hR = svgEl('text', { x: rightX, y: 14, class: 'd-slope-col', 'text-anchor': 'start' });
  hR.textContent = (o.right && o.right.title) || 'after';
  svg.appendChild(hL); svg.appendChild(hR);

  svg.appendChild(svgEl('line', { x1: leftX, x2: leftX, y1: y(hi), y2: y(lo), class: 'd-slope-axis' }));
  svg.appendChild(svgEl('line', { x1: rightX, x2: rightX, y1: y(hi), y2: y(lo), class: 'd-slope-axis' }));

  const minGap = 13;
  const leftLabels = decollide(series.map((s) => ({ v: isNum(s.a) ? s.a : s.b })), y, minGap, padTop, h - padBottom);
  const rightLabels = decollide(series.map((s) => ({ v: isNum(s.b) ? s.b : s.a })), y, minGap, padTop, h - padBottom);

  series.forEach((s, i) => {
    const ay = isNum(s.a) ? y(s.a) : null;
    const by = isNum(s.b) ? y(s.b) : null;
    const g = svgEl('g', { class: 'd-slope-series' + (s.emphasis ? ' d-emph' : '') });

    if (ay != null && by != null) {
      const improved = goodDown ? s.b < s.a : s.b > s.a;
      const lineCls = 'd-slope-line ' + (s.b === s.a ? 'd-flat' : improved ? 'd-good' : 'd-bad')
        + (s.emphasis ? ' d-emph' : '');
      const line = svgEl('line', { x1: leftX, y1: ay, x2: rightX, y2: by, class: lineCls });
      line.appendChild(title(`${s.label}: ${fmt(s.a)} → ${fmt(s.b)} (${fmtSigned(s.b - s.a)})`));
      g.appendChild(line);
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'd-slope-node' }));
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'd-slope-node' }));
    }

    const ll = leftLabels[i];
    if (isNum(s.a)) {
      if (Math.abs(ll - ay) > 1.5) {
        g.appendChild(svgEl('line', { x1: leftX - 4, y1: ll, x2: leftX, y2: ay, class: 'd-leader' }));
      }
      const tx = svgEl('text', { x: leftX - 8, y: ll + 3, class: 'd-slope-label', 'text-anchor': 'end' });
      tx.textContent = `${shortLabel(s.label)}  ${fmt(s.a, 2)}`;
      g.appendChild(tx);
    }
    const rl = rightLabels[i];
    if (isNum(s.b)) {
      if (Math.abs(rl - by) > 1.5) {
        g.appendChild(svgEl('line', { x1: rightX, y1: by, x2: rightX + 4, y2: rl, class: 'd-leader' }));
      }
      const tx = svgEl('text', { x: rightX + 8, y: rl + 3, class: 'd-slope-label', 'text-anchor': 'start' });
      tx.textContent = `${fmt(s.b, 2)}  ${shortLabel(s.label)}`;
      g.appendChild(tx);
    }
    if (o.onClick) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => o.onClick(s));
    }
    svg.appendChild(g);
  });
  return svg;
}

// One-dimensional collision resolver: given target y positions, push
// neighbours apart so each keeps `minGap`, clamped to [top, bottom].
// Returns adjusted positions in the SAME order as input. Exported for
// the test suite (this is the non-collision guarantee being asserted).
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

function shortLabel(s) {
  const str = s == null ? '' : String(s);
  return str.length > 12 ? str.slice(0, 11) + '…' : str;
}

// ---- bumps chart (lineage as ranked lanes) --------------------------
//
// The champion lineage as a thick spine in its OWN lane, with rejected
// challengers branching into a distinct offset lane so nothing collides
// (the current UI's collision bug). x = generation order, lane = role.
// Clickable nodes.
//
// opts: { width, height, nodes:[{id, x, promoted, scalar, parent}], onClick }
export function bumps(opts) {
  const o = opts || {};
  const nodes = (Array.isArray(o.nodes) ? o.nodes : []).filter((n) => n);
  const w = o.width || 640;
  const h = o.height || 180;
  const padX = 44; const spineY = h * 0.40; const challY = h * 0.80;
  const svg = svgEl('svg', { class: 'd-bumps', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
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
    svg.appendChild(svgEl('line', {
      x1: X(promoted[i - 1].x), y1: spineY, x2: X(promoted[i].x), y2: spineY, class: 'd-spine-line',
    }));
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
  for (const n of nodes) {
    const cy = laneY(n);
    const cls = 'd-bump-node ' + (n.promoted ? 'd-promoted' : 'd-rejected');
    const c = svgEl('circle', { cx: X(n.x), cy, r: n.promoted ? 4.5 : 3.5, class: cls },
      [title(`${n.id}${isNum(n.scalar) ? ' · ' + fmt(n.scalar) : ''} · ${n.promoted ? 'promoted' : 'rejected'}`)]);
    if (o.onClick) {
      c.style.cursor = 'pointer';
      c.addEventListener('click', () => o.onClick(n));
    }
    svg.appendChild(c);
    const t = svgEl('text', { x: X(n.x), y: cy + 16, class: 'd-bump-label', 'text-anchor': 'middle' });
    t.textContent = shortLabel(n.id);
    svg.appendChild(t);
  }
  return svg;
}

// ---- heatmap (themed, quiet) ----------------------------------------
//
// Small-multiples heatmap: rows × columns coloured by value. Used for
// board-entry × generation loss profiles and per-judge × generation
// trends. Hover → exact value.
//
// opts: { rows:[{label,id}], cols:[{label,id}], value(rowId,colId),
//         diverging, cellW, cellH, labelWidth, headHeight, onClick }
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
  const svg = svgEl('svg', { class: 'd-heatmap', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
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
    const t = svgEl('text', {
      x: cx, y: headH - 6, class: 'd-hm-col',
      transform: `rotate(-45 ${cx} ${headH - 6})`, 'text-anchor': 'start',
    });
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
      const cell = svgEl('rect', {
        x: cx + 1, y: ry + 1, width: cw - 2, height: ch - 2, rx: 1.5, class: 'd-hm-cell', fill,
      }, [title(`${r.label} × ${c.label}: ${isNum(v) ? fmt(v) : '—'}`)]);
      if (o.onClick) {
        cell.style.cursor = 'pointer';
        cell.addEventListener('click', () => o.onClick(r.id, c.id));
      }
      svg.appendChild(cell);
    });
  });
  return svg;
}

// Quiet sequential / diverging ramp tuned to the --v2 palette.
function rampColor(t, diverging) {
  const x = Math.max(0, Math.min(1, t));
  if (diverging) {
    if (x < 0.5) return lerpHex('#1f7a6b', '#e9e3d6', x * 2);
    return lerpHex('#e9e3d6', '#b14a4a', (x - 0.5) * 2);
  }
  // HIGH loss = more ink (warmer / darker).
  return lerpHex('#f2ede1', '#9c5a3c', x);
}
function lerpHex(a, b, t) {
  const pa = hexToRgb(a); const pb = hexToRgb(b);
  return `rgb(${Math.round(pa[0] + (pb[0] - pa[0]) * t)},${Math.round(pa[1] + (pb[1] - pa[1]) * t)},${Math.round(pa[2] + (pb[2] - pa[2]) * t)})`;
}
function hexToRgb(hex) {
  const h = hex.replace('#', '');
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

// ---- predicted-vs-actual bullet ------------------------------------
//
// A compact "did the bet pay off" mark: a thin axis with the PREDICTED
// value as a hollow target and the ACTUAL value as a filled dot, the gap
// between them drawn as an error connector. Used in the Experiment
// view's predicted-vs-actual small multiples.
//
// opts: { width, height, predicted, actual, domain, goodDirection, label }
export function predictedActual(opts) {
  const o = opts || {};
  const w = o.width || 168;
  const h = o.height || 34;
  const svg = svgEl('svg', { class: 'd-pva', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  const pad = 6;
  const vals = [0];
  if (isNum(o.predicted)) vals.push(o.predicted);
  if (isNum(o.actual)) vals.push(o.actual);
  const dom = o.domain && o.domain.length === 2 ? o.domain : extent(vals);
  const x = scale(dom, [pad, w - pad]);
  const midY = h - 12;

  if (o.label) {
    const t = svgEl('text', { x: pad, y: 9, class: 'd-pva-label' });
    t.textContent = o.label;
    svg.appendChild(t);
  }
  svg.appendChild(svgEl('line', { x1: pad, x2: w - pad, y1: midY, y2: midY, class: 'd-pva-axis' }));
  if (dom[0] <= 0 && dom[1] >= 0) {
    svg.appendChild(svgEl('line', { x1: x(0), x2: x(0), y1: midY - 5, y2: midY + 5, class: 'd-zero-rule' }));
  }
  if (isNum(o.predicted)) {
    svg.appendChild(svgEl('circle', { cx: x(o.predicted), cy: midY, r: 4.5, class: 'd-pva-pred' },
      [title(`predicted ${fmtSigned(o.predicted)}`)]));
  }
  if (isNum(o.actual)) {
    const goodDown = (o.goodDirection || 'down') === 'down';
    const good = goodDown ? o.actual < 0 : o.actual > 0;
    const cls = 'd-pva-actual ' + (o.actual === 0 ? '' : good ? 'd-good' : 'd-bad');
    if (isNum(o.predicted)) {
      svg.appendChild(svgEl('line', { x1: x(o.predicted), x2: x(o.actual), y1: midY, y2: midY, class: 'd-pva-err' }));
    }
    svg.appendChild(svgEl('circle', { cx: x(o.actual), cy: midY, r: 3, class: cls },
      [title(`actual ${fmtSigned(o.actual)}`)]));
  }
  return svg;
}

// ---- small-multiple wrapper -----------------------------------------
//
// A titled cell for a grid of small multiples: a tiny caption + the
// supplied mark, chrome reduced to a hairline.
export function smallMultiple(caption, mark, sub) {
  return el('figure', { class: 'd-sm' }, [
    el('figcaption', { class: 'd-sm-cap' }, [
      el('span', { class: 'd-sm-title', text: caption == null ? '' : String(caption) }),
      sub ? el('span', { class: 'd-sm-sub', text: String(sub) }) : null,
    ]),
    mark,
  ]);
}
