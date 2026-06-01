// variants/R/svg.js — dependency-free SVG data-viz primitives (Strata).
//
// Self-contained for Variant R ("Strata"). Mark classes are `dr-*` and are
// styled — scoped under the variant root — by css/variants/R/strata.css.
//
// Same data-ink discipline as Variant N: high data-ink, NO pan/zoom viewports
// (every mark fits its container), a THEME-AWARE heatmap (token ink at
// value-driven opacity), and a fit-to-width Tufte Sankey with a per-board loss
// VALUE that is a distinct mark from its label. Lower drift/loss is BETTER.

import { svgEl, el } from '../../core/dom.js';

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
  const svg = svgEl('svg', { class: 'dr-spark', width: w, height: h, viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', role: 'img' });
  if (fin.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h / 2, x2: w - pad, y2: h / 2, class: 'dr-spark-empty' }));
    return svg;
  }
  const [lo, hi] = extent(fin);
  const x = scale([0, Math.max(1, raw.length - 1)], [pad, w - pad]);
  const y = scale([lo, hi], [h - pad, pad]);
  if (o.band) svg.appendChild(svgEl('rect', { x: pad, y: pad, width: w - 2 * pad, height: h - 2 * pad, class: 'dr-spark-band' }));
  let d = ''; let penDown = false;
  raw.forEach((v, i) => {
    if (!isNum(v)) { penDown = false; return; }
    d += `${penDown ? 'L' : 'M'}${x(i).toFixed(2)},${y(v).toFixed(2)} `;
    penDown = true;
  });
  svg.appendChild(svgEl('path', { d: d.trim(), class: 'dr-spark-line', fill: 'none' }));
  let lastI = -1;
  for (let i = raw.length - 1; i >= 0; i--) { if (isNum(raw[i])) { lastI = i; break; } }
  if (lastI >= 0) {
    let firstI = -1;
    for (let i = 0; i < raw.length; i++) { if (isNum(raw[i])) { firstI = i; break; } }
    const improved = firstI >= 0 && lastI !== firstI ? raw[lastI] < raw[firstI] : null;
    const cls = improved === null ? 'dr-spark-dot' : improved ? 'dr-spark-dot dr-good' : 'dr-spark-dot dr-bad';
    svg.appendChild(svgEl('circle', { cx: x(lastI), cy: y(raw[lastI]), r: 2.2, class: cls }, [title(fmt(raw[lastI]))]));
  }
  return svg;
}

// ---- theme-aware heatmap (epoch overview — fix #6) ------------------

export function heatmap(opts) {
  const o = opts || {};
  const rows = Array.isArray(o.rows) ? o.rows : [];
  const cols = Array.isArray(o.cols) ? o.cols : [];
  const cw = o.cellW || 24;
  const ch = o.cellH || 16;
  const labelW = o.labelWidth || 128;
  const headH = o.headHeight || 44;
  const w = labelW + cols.length * cw + 6;
  const h = headH + rows.length * ch + 6;
  const svg = svgEl('svg', { class: 'dr-heatmap', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (rows.length === 0 || cols.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'dr-empty-label' });
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
    const t = svgEl('text', { x: cx, y: headH - 6, class: 'dr-hm-col', transform: `rotate(-45 ${cx} ${headH - 6})`, 'text-anchor': 'start' });
    t.textContent = shortLabel(c.label);
    svg.appendChild(t);
  });
  rows.forEach((r, i) => {
    const ry = headH + i * ch;
    const lbl = svgEl('text', { x: labelW - 6, y: ry + ch - 4, class: 'dr-hm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(r.label, 18);
    svg.appendChild(lbl);
    cols.forEach((c, j) => {
      const v = o.value(r.id, c.id);
      const cx = labelW + j * cw;
      const t = isNum(v) ? (v - lo) / span : null;
      const cls = t == null ? 'dr-hm-cell dr-hm-empty' : 'dr-hm-cell';
      const op = t == null ? null : (0.18 + 0.82 * Math.max(0, Math.min(1, t))).toFixed(3);
      const attrs = { x: cx + 1, y: ry + 1, width: cw - 2, height: ch - 2, rx: 1.5, class: cls };
      if (op != null) attrs['fill-opacity'] = op;
      const cell = svgEl('rect', attrs, [title(`${r.label} × ${c.label}: ${isNum(v) ? fmt(v) : '—'}`)]);
      if (o.onClick) { cell.style.cursor = 'pointer'; cell.addEventListener('click', () => o.onClick(r.id, c.id)); }
      svg.appendChild(cell);
    });
  });
  return svg;
}

// ---- value dot-plot with a reference line ---------------------------

export function valueDotPlot(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d);
  const w = o.width || 460;
  const rh = o.rowHeight || 19;
  const labelW = o.labelWidth || 170;
  const glyphW = 16;
  const h = Math.max(rh, items.length * rh + 8);
  const svg = svgEl('svg', { class: 'dr-valdot', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'dr-empty-label' });
    t.textContent = 'no scored entries';
    svg.appendChild(t);
    return svg;
  }
  const ref = o.reference && isNum(o.reference.value) ? o.reference.value : null;
  const vals = items.map((d) => d.value).filter(isNum);
  if (ref != null) vals.push(ref);
  let [lo, hi] = extent(vals);
  lo = Math.min(lo, 0);
  if (lo === hi) hi += 1;
  const x = scale([lo, hi], [labelW + 4, w - 4 - glyphW]);
  if (ref != null) {
    const rx = x(ref);
    svg.appendChild(svgEl('line', { x1: rx, x2: rx, y1: 2, y2: h - 2, class: 'dr-ref-rule' }, [title(`${(o.reference.label || 'reference')}: ${fmt(ref)}`)]));
  }
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 4;
    const g = svgEl('g', { class: 'dr-dotrow', tabindex: o.onClick ? '0' : null });
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'dr-dot-label', 'text-anchor': 'end' });
    lbl.textContent = d.label != null ? shortLabel(String(d.label), 22) : '';
    g.appendChild(lbl);
    if (isNum(d.value)) {
      const dx = x(d.value);
      g.appendChild(svgEl('line', { x1: x(lo), x2: dx, y1: cy, y2: cy, class: 'dr-dot-connector' }));
      const good = ref != null ? d.value < ref : false;
      const worse = ref != null ? d.value > ref : false;
      const cls = 'dr-dot ' + (good ? 'dr-good' : worse ? 'dr-bad' : '');
      g.appendChild(svgEl('circle', { cx: dx, cy, r: 3.2, class: cls }, [title(`${d.label}: ${fmt(d.value)}${ref != null ? ` (vs champ ${fmt(ref)})` : ''}`)]));
      g.appendChild(outcomeGlyph(d, w - glyphW + 2, cy));
    } else {
      const t = svgEl('text', { x: x(lo) + 6, y: cy + 3, class: 'dr-dot-missing' });
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
  const svg = svgEl('svg', { class: 'dr-sparkbar', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (bars.length === 0) {
    svg.appendChild(svgEl('line', { x1: pad, y1: h - footH, x2: w - pad, y2: h - footH, class: 'dr-spark-empty' }));
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
      const cls = 'dr-sparkbar-bar' + (b.timeout ? ' dr-timeout' : '') + (b.fail ? ' dr-fail' : '');
      svg.appendChild(svgEl('rect', { x: cx - bw / 2, y: Math.min(y, y0), width: bw, height: Math.max(1, Math.abs(y0 - y)), class: cls },
        [title(`${b.label}: ${fmt(b.value)}${b.timeout ? ' · timed out' : ''}${b.fail ? ' · failed' : ''}`)]));
    } else {
      svg.appendChild(svgEl('line', { x1: cx, y1: y0 - 1, x2: cx, y2: y0 - 4, class: 'dr-sparkbar-missing' }, [title(`${b.label}: no run`)]));
    }
  });
  svg.appendChild(svgEl('line', { x1: pad, y1: y0, x2: w - pad, y2: y0, class: 'dr-sparkbar-foot' }));
  return svg;
}

export function genDots(opts) {
  const o = opts || {};
  const cells = Array.isArray(o.cells) ? o.cells : [];
  const w = o.width || 200;
  const h = o.height || 14;
  const pad = 2;
  const svg = svgEl('svg', { class: 'dr-genrow', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  const n = Math.max(1, cells.length);
  const slot = (w - 2 * pad) / n;
  cells.forEach((c, i) => { svg.appendChild(outcomeGlyph(c, pad + slot * (i + 0.5), h / 2)); });
  return svg;
}

function outcomeGlyph(d, x, cy) {
  if (d && d.ran === false) return svgEl('circle', { cx: x, cy, r: 2.2, class: 'dr-glyph-none' }, [title('no run')]);
  if (d.timeout) return svgEl('text', { x, y: cy + 3, class: 'dr-glyph-timeout', 'text-anchor': 'middle' }, [title('budget exceeded (timeout)'), '⏱']);
  if (d.pass === true || d.pass === 1) return svgEl('circle', { cx: x, cy, r: 2.4, class: 'dr-glyph-pass' }, [title('passed')]);
  if (d.pass === false || d.pass === 0) {
    const g = svgEl('g', null, [title('failed')]);
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy - 2.4, x2: x + 2.4, y2: cy + 2.4, class: 'dr-glyph-fail' }));
    g.appendChild(svgEl('line', { x1: x - 2.4, y1: cy + 2.4, x2: x + 2.4, y2: cy - 2.4, class: 'dr-glyph-fail' }));
    return g;
  }
  return svgEl('circle', { cx: x, cy, r: 2.2, class: 'dr-glyph-none' }, [title('no predicate')]);
}

// ---- horizontal value bars (per-judge losses) ----------------------

export function valueBars(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d && isNum(d.value));
  const w = o.width || 360;
  const rh = o.rowHeight || 18;
  const labelW = o.labelWidth || 150;
  const h = Math.max(rh, items.length * rh + 6);
  const svg = svgEl('svg', { class: 'dr-vbars', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 14, class: 'dr-empty-label' });
    t.textContent = 'no values';
    svg.appendChild(t);
    return svg;
  }
  const hi = Math.max(1e-9, ...items.map((d) => Math.abs(d.value)));
  const x0 = labelW + 4;
  const x = scale([0, hi], [x0, w - 36]);
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 3;
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'dr-dot-label', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(d.label), 20);
    svg.appendChild(lbl);
    const bx = x(Math.abs(d.value));
    svg.appendChild(svgEl('rect', { x: x0, y: cy - 4, width: Math.max(1, bx - x0), height: 8, rx: 1, class: 'dr-vbar' }, [title(`${d.label}: ${fmt(d.value)}`)]));
    const vt = svgEl('text', { x: bx + 4, y: cy + 3, class: 'dr-vbar-val' });
    vt.textContent = fmt(d.value, 1);
    svg.appendChild(vt);
  });
  return svg;
}

// ---- collision helpers (exported for the tests) ---------------------

export function decollide(items, y, minGap, top, bottom) {
  const idx = items.map((it, i) => ({ i, pos: isNum(it.v) ? y(it.v) : (top + bottom) / 2 }));
  idx.sort((p, q) => p.pos - q.pos);
  for (let k = 1; k < idx.length; k++) if (idx[k].pos - idx[k - 1].pos < minGap) idx[k].pos = idx[k - 1].pos + minGap;
  if (idx.length && idx[idx.length - 1].pos > bottom) {
    idx[idx.length - 1].pos = bottom;
    for (let k = idx.length - 2; k >= 0; k--) if (idx[k + 1].pos - idx[k].pos < minGap) idx[k].pos = idx[k + 1].pos - minGap;
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
  const svg = svgEl('svg', { class: 'dr-pslope', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (series.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'dr-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no paired board duels yet';
    svg.appendChild(t);
    return svg;
  }
  const allVals = [];
  for (const s of series) { if (isNum(s.a)) allVals.push(s.a); if (isNum(s.b)) allVals.push(s.b); }
  const [lo, hi] = extent(allVals);
  const y = scale([lo, hi], [h - padBottom, padTop]);
  const hL = svgEl('text', { x: leftX, y: 15, class: 'dr-slope-col', 'text-anchor': 'end' });
  hL.textContent = (o.left && o.left.title) || 'champion';
  const hR = svgEl('text', { x: rightX, y: 15, class: 'dr-slope-col', 'text-anchor': 'start' });
  hR.textContent = (o.right && o.right.title) || 'challenger';
  svg.appendChild(hL); svg.appendChild(hR);
  svg.appendChild(svgEl('line', { x1: leftX, x2: leftX, y1: y(hi), y2: y(lo), class: 'dr-slope-axis' }));
  svg.appendChild(svgEl('line', { x1: rightX, x2: rightX, y1: y(hi), y2: y(lo), class: 'dr-slope-axis' }));
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
    const dirCls = verdict === 'improved' ? 'dr-good' : verdict === 'regressed' ? 'dr-bad' : 'dr-flat';
    const g = svgEl('g', { class: 'dr-pslope-series' });
    if (ay != null && by != null) {
      const line = svgEl('line', { x1: leftX, y1: ay, x2: rightX, y2: by, class: 'dr-pslope-line ' + dirCls });
      line.appendChild(title(`${s.label}: ${fmt(s.a)} → ${fmt(s.b)} (${fmtSigned(s.b - s.a)}; ${verdict})`));
      g.appendChild(line);
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'dr-pslope-node ' + dirCls }));
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'dr-pslope-node ' + dirCls }));
    } else if (ay != null) {
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.4, class: 'dr-pslope-node dr-flat' }, [title(`${s.label}: champion only ${fmt(s.a)}`)]));
    } else if (by != null) {
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.4, class: 'dr-pslope-node dr-flat' }, [title(`${s.label}: challenger only ${fmt(s.b)}`)]));
    }
    const ll = leftLabels[i];
    if (isNum(s.a)) {
      if (Math.abs(ll - y(s.a)) > 1.5) g.appendChild(svgEl('line', { x1: leftX - 4, y1: ll, x2: leftX, y2: y(s.a), class: 'dr-leader' }));
      const tx = svgEl('text', { x: leftX - 8, y: ll + 3, class: 'dr-pslope-label', 'text-anchor': 'end' });
      tx.textContent = `${shortLabel(s.label, 14)}  ${fmt(s.a, 1)}`;
      g.appendChild(tx);
    }
    const rl = rightLabels[i];
    if (isNum(s.b)) {
      if (Math.abs(rl - y(s.b)) > 1.5) g.appendChild(svgEl('line', { x1: rightX, y1: y(s.b), x2: rightX + 4, y2: rl, class: 'dr-leader' }));
      const tx = svgEl('text', { x: rightX + 8, y: rl + 3, class: 'dr-pslope-label', 'text-anchor': 'start' });
      tx.textContent = `${fmt(s.b, 1)}  ${shortLabel(s.label, 14)}`;
      g.appendChild(tx);
    }
    if (o.onClick) { g.style.cursor = 'pointer'; g.addEventListener('click', () => o.onClick(s)); }
    svg.appendChild(g);
  });
  return svg;
}

// ---- mutation-site × generation MATRIX (reused by the diff view) ----
//
// A clickable filled-cell grid: a row per mutation site, a column per
// generation; a cell is "patched" when that generation touched that site.
// onCell(genId, siteId) selects BOTH → fills the per-candidate diff.
export function mutationMatrix(opts) {
  const o = opts || {};
  const sites = Array.isArray(o.sites) ? o.sites : [];
  const gens = Array.isArray(o.gens) ? o.gens : [];
  const cw = o.cellW || 34;
  const ch = o.cellH || 26;
  const labelW = o.labelWidth || 190;
  const headH = o.headHeight || 22;
  const w = labelW + gens.length * cw + 4;
  const h = headH + sites.length * ch + 4;
  const svg = svgEl('svg', { class: 'dr-mtxsvg', width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' });
  if (!sites.length || !gens.length) {
    const t = svgEl('text', { x: 4, y: 16, class: 'dr-empty-label' });
    t.textContent = 'no mutation surface';
    svg.appendChild(t);
    return svg;
  }
  gens.forEach((g, j) => {
    const cx = labelW + j * cw + cw / 2;
    const t = svgEl('text', { x: cx, y: headH - 6, class: 'dr-mtx-head' + (g.promoted ? ' dr-good' : ''), 'text-anchor': 'middle' });
    t.textContent = shortLabel(String(g.label || g.id), 6) + (g.promoted ? ' ♛' : '');
    svg.appendChild(t);
  });
  sites.forEach((s, i) => {
    const ry = headH + i * ch;
    const lbl = svgEl('text', { x: labelW - 6, y: ry + ch / 2 + 3, class: 'dr-mtx-rowlabel' + (o.selectedSite === s.id ? ' dr-sel' : ''), 'text-anchor': 'end' });
    lbl.textContent = shortLabel(String(s.label || s.id), 24);
    svg.appendChild(lbl);
    gens.forEach((g, j) => {
      const cx = labelW + j * cw;
      const on = o.patched ? !!o.patched(s.id, g.id) : false;
      const sel = o.selectedSite === s.id && o.selectedGen === g.id;
      const cls = 'dr-mtx-cell' + (on ? ' dr-mtx-on' : ' dr-mtx-off') + (sel ? ' dr-mtx-sel' : '');
      const rect = svgEl('rect', { x: cx + 1, y: ry + 1, width: cw - 2, height: ch - 2, rx: 2, class: cls },
        [title(`${g.id} ${on ? 'patched' : 'did not patch'} ${s.id}`)]);
      if (on) {
        svg.appendChild(rect);
        const dot = svgEl('rect', { x: cx + cw / 2 - 4, y: ry + ch / 2 - 4, width: 8, height: 8, rx: 1.5, class: 'dr-mtx-square' });
        if (o.onCell) {
          rect.style.cursor = 'pointer'; dot.style.cursor = 'pointer';
          const fire = () => o.onCell(g.id, s.id);
          rect.addEventListener('click', fire); dot.addEventListener('click', fire);
        }
        svg.appendChild(dot);
      } else {
        svg.appendChild(rect);
      }
    });
  });
  return svg;
}

// ---- Tufte Sankey (fit-to-width, NO viewport) -----------------------

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
      const hh = heights[i];
      const node = { id: n.id, stage, x, y, h: hh, w: colW, label: n.label != null ? n.label : n.id, sub: n.sub || '', cls: n.cls || '', value: n.value, ref: n.ref || null, _outCursor: 0, _inCursor: 0 };
      positioned.set(n.id, node);
      nodesOut.push(node);
      y += hh + gap;
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
    linksOut.push({ id: l.id || `${l.source}__${l.target}`, source: l.source, target: l.target, sx, sy, tx, ty, hwS: Math.max(0.6, sBand / 2), hwT: Math.max(0.6, tBand / 2), value: l.value, cls: l.cls || '' });
  }
  const box = { x: 0, y: 0, w: totalW, h: colHeight + top * 2 };
  return { nodes: nodesOut, links: linksOut, box };
}

export function sankey(opts) {
  const o = opts || {};
  const { nodes, links, box } = layoutSankey(o);
  const svg = svgEl('svg', { class: 'dr-sankey', width: '100%', height: box.h, viewBox: `0 0 ${box.w} ${box.h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (nodes.length === 0) {
    const t = svgEl('text', { x: box.w / 2, y: box.h / 2, class: 'dr-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no causal flow yet';
    svg.appendChild(t);
    return svg;
  }
  const stageHead = { patch: 'PATCH', drift: 'PER-BOARD DRIFT', gate: 'GATE' };
  const byStage = {};
  for (const n of nodes) (byStage[n.stage] = byStage[n.stage] || []).push(n);
  for (const stage of Object.keys(byStage)) {
    const x = byStage[stage][0].x + byStage[stage][0].w / 2;
    const t = svgEl('text', { x, y: 14, class: 'dr-sankey-head', 'text-anchor': 'middle' });
    t.textContent = stageHead[stage] || stage;
    svg.appendChild(t);
  }
  const linkLayer = svgEl('g', { class: 'dr-sankey-links' });
  for (const l of links) {
    const mx = (l.sx + l.tx) / 2;
    const d = `M ${l.sx} ${l.sy - l.hwS} `
      + `C ${mx} ${l.sy - l.hwS}, ${mx} ${l.ty - l.hwT}, ${l.tx} ${l.ty - l.hwT} `
      + `L ${l.tx} ${l.ty + l.hwT} `
      + `C ${mx} ${l.ty + l.hwT}, ${mx} ${l.sy + l.hwS}, ${l.sx} ${l.sy + l.hwS} Z`;
    linkLayer.appendChild(svgEl('path', { d, class: 'dr-sankey-ribbon ' + (l.cls || ''), fill: 'currentColor' }, [title(`${l.source} → ${l.target}: ${fmt(l.value, 1)}`)]));
  }
  svg.appendChild(linkLayer);
  const nodeLayer = svgEl('g', { class: 'dr-sankey-nodes' });
  for (const n of nodes) {
    const g = svgEl('g', { class: 'dr-sankey-node ' + (n.cls || ''), tabindex: o.onNode ? '0' : null });
    g.appendChild(svgEl('rect', { x: n.x, y: n.y, width: 6, height: n.h, rx: 1, class: 'dr-sankey-bar' }, [title(`${n.label}${isNum(n.value) ? ' · ' + fmt(n.value, 1) : ''}`)]));
    const anchor = n.stage === 'gate' ? 'end' : 'start';
    const lx = n.stage === 'gate' ? n.x - 6 : n.x + 12;
    const ty = n.y + n.h / 2;
    const hasValue = n.stage === 'drift' && isNum(n.value);
    const lbl = svgEl('text', { x: lx, y: ty - 1, class: 'dr-sankey-label', 'text-anchor': anchor });
    lbl.textContent = shortLabel(String(n.label), hasValue ? 16 : 22);
    g.appendChild(lbl);
    if (hasValue) {
      const vx = n.x + n.w; // far edge — the loss VALUE is its OWN mark (label ≠ value)
      const val = svgEl('text', { x: vx, y: ty - 1, class: 'dr-sankey-value', 'text-anchor': 'end' });
      val.textContent = fmt(n.value, 0);
      g.appendChild(val);
    }
    if (n.sub) {
      const sub = svgEl('text', { x: lx, y: ty + 11, class: 'dr-sankey-sub', 'text-anchor': anchor });
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

// ---- SIDE-BY-SIDE line diff (mutation view — fix #2) ----------------
//
// Two columns — champion baseline (left) | challenger new (right) — line-diffed
// by the classic LCS so common lines align row-for-row. Both arguments are
// STRINGS (the "[object Object]" bug = passing the baseline OBJECT instead of
// `.baseline.content`). Returns a DOM node (a real two-column block).
export function sideBySideDiff(opts) {
  const o = opts || {};
  const leftText = o.baseline == null ? '' : String(o.baseline);
  const rightText = (o.challenger != null ? o.challenger : o.next) == null ? '' : String(o.challenger != null ? o.challenger : o.next);
  const a = leftText.replace(/\r\n/g, '\n').split('\n');
  const b = rightText.replace(/\r\n/g, '\n').split('\n');
  const rows = lcsDiff(a, b);
  const wrap = el('div', { class: 'dr-sxs' });
  wrap.appendChild(el('div', { class: 'dr-sxs-head' }, [
    el('span', { class: 'dr-sxs-col-h dr-sxs-old', text: o.leftLabel || 'champion baseline' }),
    el('span', { class: 'dr-sxs-col-h dr-sxs-new', text: o.rightLabel || 'challenger new' }),
  ]));
  const body = el('div', { class: 'dr-sxs-body', role: 'list' });
  let ln = 0; let rn = 0;
  for (const r of rows) {
    const cls = r.type === 'same' ? '' : ' dr-sxs-changed';
    const lhsText = r.left != null ? r.left : '';
    const rhsText = r.right != null ? r.right : '';
    const lGutter = r.left != null ? String(++ln) : '';
    const rGutter = r.right != null ? String(++rn) : '';
    body.appendChild(el('div', { class: 'dr-sxs-row' + cls, role: 'listitem' }, [
      el('span', { class: 'dr-sxs-gutter', 'aria-hidden': 'true', text: lGutter }),
      el('span', { class: 'dr-sxs-cell dr-sxs-old' + (r.type === 'del' || r.type === 'mod' ? ' dr-sxs-del' : ''), text: r.left == null ? '' : (lhsText === '' ? '​' : lhsText) }),
      el('span', { class: 'dr-sxs-gutter', 'aria-hidden': 'true', text: rGutter }),
      el('span', { class: 'dr-sxs-cell dr-sxs-new' + (r.type === 'add' || r.type === 'mod' ? ' dr-sxs-add' : ''), text: r.right == null ? '' : (rhsText === '' ? '​' : rhsText) }),
    ]));
  }
  wrap.appendChild(body);
  return wrap;
}

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
  const out = [];
  for (let k = 0; k < rows.length; k++) {
    const cur = rows[k]; const nxt = rows[k + 1];
    if (cur.type === 'del' && nxt && nxt.type === 'add') { out.push({ type: 'mod', left: cur.left, right: nxt.right }); k++; }
    else out.push(cur);
  }
  return out;
}
