// variants/K/svg.js — Variant K's ("Monograph") Tufte figure toolkit.

import { svgEl } from '../../core/dom.js';

export const NS = 'http://www.w3.org/2000/svg';


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

// Spread coincident node y's a hair apart (a centred fan) so lines ending
// at the same value don't overdraw. Returns adjusted y's in input order.
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

export function bumps(opts) {
  const o = opts || {};
  const nodes = (Array.isArray(o.nodes) ? o.nodes : []).filter((n) => n);
  const w = o.width || 640;
  const h = o.height || 190;
  const padX = 48; const spineY = h * 0.38; const challY = h * 0.78;
  const svg = svgEl('svg', {
    class: 'vk-bumps', width: w, height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
  });
  if (nodes.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'vk-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no generations yet';
    svg.appendChild(t);
    return svg;
  }
  const maxX = Math.max(1, ...nodes.map((n) => n.x || 0));
  const X = scale([0, maxX], [padX, w - padX]);

  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: spineY, y2: spineY, class: 'vk-lane-guide vk-spine-guide' }));
  svg.appendChild(svgEl('line', { x1: padX, x2: w - padX, y1: challY, y2: challY, class: 'vk-lane-guide' }));
  const lblS = svgEl('text', { x: 6, y: spineY - 9, class: 'vk-lane-label' }); lblS.textContent = 'champion';
  const lblC = svgEl('text', { x: 6, y: challY - 9, class: 'vk-lane-label' }); lblC.textContent = 'challenger';
  svg.appendChild(lblS); svg.appendChild(lblC);

  const laneY = (n) => (n.promoted ? spineY : challY);
  const byId = new Map(nodes.map((n) => [n.id, n]));

  const promoted = nodes.filter((n) => n.promoted).sort((a, b) => (a.x || 0) - (b.x || 0));
  for (let i = 1; i < promoted.length; i++) {
    svg.appendChild(svgEl('line', {
      x1: X(promoted[i - 1].x), y1: spineY, x2: X(promoted[i].x), y2: spineY, class: 'vk-spine-line',
    }));
  }
  for (const n of nodes) {
    if (n.promoted) continue;
    const p = n.parent ? byId.get(n.parent) : null;
    const px = p ? X(p.x) : X((n.x || 1) - 1);
    const py = p ? laneY(p) : spineY;
    const nx = X(n.x);
    const path = `M${px},${py} C${(px + nx) / 2},${py} ${(px + nx) / 2},${challY} ${nx},${challY}`;
    svg.appendChild(svgEl('path', { d: path, class: 'vk-branch', fill: 'none' }));
  }

  // De-collide labels WITHIN each lane: challengers that share an x (v1/v2
  // off the same parent — exactly F's collision) get nudged apart in x.
  const laneGroups = new Map();
  for (const n of nodes) {
    const key = (n.promoted ? 'c' : 'h') + ':' + (n.x || 0);
    if (!laneGroups.has(key)) laneGroups.set(key, []);
    laneGroups.get(key).push(n);
  }
  const nodeX = new Map();
  for (const grp of laneGroups.values()) {
    const n = grp.length;
    const mid = (n - 1) / 2;
    grp.forEach((node, k) => { nodeX.set(node, X(node.x) + (k - mid) * 22); });
  }

  for (const n of nodes) {
    const cx = nodeX.get(n);
    const cy = laneY(n);
    const cls = 'vk-bump-node ' + (n.promoted ? 'vk-promoted' : 'vk-rejected');
    const c = svgEl('circle', { cx, cy, r: n.promoted ? 4.6 : 3.6, class: cls, tabindex: o.onClick ? '0' : null,
      role: o.onClick ? 'button' : 'img', 'data-vk': 'bump-node', 'data-gen': n.id, 'aria-label': `candidate ${n.id}` },
    [title(`${n.id}${isNum(n.scalar) ? ' · ' + fmt(n.scalar) : ''} · ${n.promoted ? 'promoted' : 'rejected'}`)]);
    if (o.onClick) {
      c.style.cursor = 'pointer';
      c.addEventListener('click', () => o.onClick(n));
      c.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); o.onClick(n); } });
    }
    svg.appendChild(c);
    const t = svgEl('text', { x: cx, y: cy + 17, class: 'vk-bump-label', 'text-anchor': 'middle' });
    t.textContent = shortLabel(n.id, 8);
    svg.appendChild(t);
  }
  return svg;
}

export function sankey(opts) {
  const o = opts || {};
  const boards = (Array.isArray(o.boards) ? o.boards : []).filter((b) => b);
  const w = o.width || 720;
  const top = 30;
  const colH = Math.max(180, boards.length * 40 + 40);
  const h = colH + top * 2;
  const nodeW = 132;
  const colX = [16, (w - nodeW) / 2, w - nodeW - 16]; // patch / drift / gate

  const svg = svgEl('svg', {
    class: 'vk-sankey', width: w, height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMidYMid meet', role: 'img',
    'aria-label': 'Causal flow: candidate to per-board loss to aggregate scalar',
  });
  if (boards.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'vk-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no per-board flow yet';
    svg.appendChild(t);
    return svg;
  }

  const total = boards.reduce((a, b) => a + Math.max(0.0001, Math.abs(b.value) || 0), 0) || 1;
  const gap = 10;
  const avail = colH - gap * Math.max(0, boards.length - 1);

  // Board (drift) column heights ∝ loss; the candidate + aggregate nodes
  // span the whole stack (their throughput is the total).
  const heights = boards.map((b) => Math.max(16, (Math.max(0.0001, Math.abs(b.value) || 0) / total) * avail));
  const blockH = heights.reduce((a, b) => a + b, 0) + gap * Math.max(0, boards.length - 1);
  let y = top + Math.max(0, (colH - blockH) / 2);
  const driftNodes = boards.map((b, i) => {
    const node = { ...b, x: colX[1], y, h: heights[i] };
    y += heights[i] + gap;
    return node;
  });
  const spanTop = driftNodes.length ? driftNodes[0].y : top;
  const spanBot = driftNodes.length ? driftNodes[driftNodes.length - 1].y + driftNodes[driftNodes.length - 1].h : top + colH;
  const candNode = { x: colX[0], y: spanTop, h: spanBot - spanTop, label: (o.candidate && o.candidate.label) || 'candidate', sub: o.candidate && o.candidate.sub };
  const aggNode = { x: colX[2], y: spanTop, h: spanBot - spanTop, label: (o.aggregate && o.aggregate.label) || 'aggregate', sub: o.aggregate && o.aggregate.sub };

  // Column heads (minimal chrome).
  const heads = [['CANDIDATE', colX[0]], ['PER-BOARD LOSS', colX[1]], ['AGGREGATE', colX[2]]];
  for (const [t, x] of heads) {
    svg.appendChild(svgEl('text', { x: x + 2, y: 18, class: 'vk-sankey-head' }, [t]));
  }

  // Ribbons: candidate → board, board → aggregate. Thin (data-ink), half-
  // width ∝ the board's loss share, attaching at each node's stacked edge.
  let candCursor = candNode.y;
  let aggCursor = aggNode.y;
  const ribbon = (sx, sy, sh, tx, ty, th, cls) => {
    const d = `M${sx},${sy} C${(sx + tx) / 2},${sy} ${(sx + tx) / 2},${ty} ${tx},${ty} `
      + `L${tx},${ty + th} C${(sx + tx) / 2},${ty + th} ${(sx + tx) / 2},${sy + sh} ${sx},${sy + sh} Z`;
    return svgEl('path', { d, class: 'vk-ribbon ' + (cls || '') });
  };
  driftNodes.forEach((b, i) => {
    const bh = heights[i];
    const cls = b.cls || '';
    // candidate → board
    svg.appendChild(ribbon(candNode.x + nodeW, candCursor, bh, b.x, b.y, bh, cls));
    candCursor += bh;
    // board → aggregate
    svg.appendChild(ribbon(b.x + nodeW, b.y, bh, aggNode.x, aggCursor, bh, cls));
    aggCursor += bh;
  });

  // Nodes (rects) + direct in-place labels.
  const drawNode = (n, opts2) => {
    const g = svgEl('g', { class: 'vk-sankey-node ' + ((opts2 && opts2.cls) || '') + (opts2 && opts2.clickable ? ' vk-clickable' : ''),
      tabindex: opts2 && opts2.clickable ? '0' : null, role: opts2 && opts2.clickable ? 'button' : 'group',
      'data-vk': opts2 && opts2.clickable ? 'sankey-board' : 'sankey-node', 'data-id': n.id || n.label,
      'aria-label': `${n.label}${n.sub ? ' ' + n.sub : ''}` });
    g.appendChild(svgEl('rect', { x: n.x, y: n.y, width: nodeW, height: Math.max(2, n.h), rx: 3, class: 'vk-sankey-rect' },
      [title(`${n.label}${n.sub ? ' · ' + n.sub : ''}`)]));
    const ty = n.y + Math.min(n.h / 2, 16);
    g.appendChild(svgEl('text', { x: n.x + 8, y: ty, class: 'vk-sankey-label' }, [shortLabel(n.label, 18)]));
    if (n.sub) g.appendChild(svgEl('text', { x: n.x + 8, y: ty + 13, class: 'vk-sankey-sub' }, [shortLabel(n.sub, 20)]));
    if (opts2 && opts2.onClick) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', () => opts2.onClick(n));
      g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); opts2.onClick(n); } });
    }
    svg.appendChild(g);
  };
  drawNode(candNode, { cls: 'vk-cand' });
  driftNodes.forEach((n) => drawNode(n, { cls: n.cls, clickable: !!o.onBoard, onClick: () => o.onBoard && o.onBoard(n.ref != null ? n.ref : n.id) }));
  drawNode(aggNode, { cls: 'vk-agg' });
  return svg;
}

export function valueDotPlot(opts) {
  const o = opts || {};
  const items = (Array.isArray(o.items) ? o.items : []).filter((d) => d);
  const w = o.width || 480;
  const rh = o.rowHeight || 22;
  const labelW = o.labelWidth || 180;
  const glyphW = 16;
  const h = Math.max(rh, items.length * rh + 8);
  const svg = svgEl('svg', { class: 'vk-valdot', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (items.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'vk-empty-label' });
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
    svg.appendChild(svgEl('line', { x1: rx, x2: rx, y1: 2, y2: h - 2, class: 'vk-ref-rule' },
      [title(`${o.reference.label || 'reference'}: ${fmt(ref)}`)]));
  }
  items.forEach((d, i) => {
    const cy = i * rh + rh / 2 + 4;
    const g = svgEl('g', { class: 'vk-dotrow', tabindex: o.onClick ? '0' : null, 'data-vk': 'dotrow', 'data-id': d.id });
    const lbl = svgEl('text', { x: labelW, y: cy + 3, class: 'vk-dot-label', 'text-anchor': 'end' });
    lbl.textContent = d.label != null ? shortLabel(String(d.label), 24) : '';
    g.appendChild(lbl);
    if (isNum(d.value)) {
      const dx = x(d.value);
      g.appendChild(svgEl('line', { x1: x(lo), x2: dx, y1: cy, y2: cy, class: 'vk-dot-connector' }));
      const good = ref != null ? d.value < ref : false;
      const worse = ref != null ? d.value > ref : false;
      const cls = 'vk-dot ' + (good ? 'vk-good' : worse ? 'vk-bad' : '');
      g.appendChild(svgEl('circle', { cx: dx, cy, r: 3.4, class: cls },
        [title(`${d.label}: ${fmt(d.value)}${ref != null ? ` (vs champ ${fmt(ref)})` : ''}`)]));
      g.appendChild(outcomeGlyph(d, w - glyphW + 2, cy));
    } else {
      const t = svgEl('text', { x: x(lo) + 6, y: cy + 3, class: 'vk-dot-missing' });
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
  if (d && d.ran === false) return svgEl('circle', { cx: x, cy, r: 2.4, class: 'vk-glyph-none' }, [title('no run')]);
  if (d && d.timeout) {
    return svgEl('text', { x, y: cy + 3, class: 'vk-glyph-timeout', 'text-anchor': 'middle' }, [title('budget exceeded (timeout)'), '⏱']);
  }
  if (d && (d.pass === true || d.pass === 1)) return svgEl('circle', { cx: x, cy, r: 2.6, class: 'vk-glyph-pass' }, [title('passed')]);
  if (d && (d.pass === false || d.pass === 0)) {
    const g = svgEl('g', null, [title('failed')]);
    g.appendChild(svgEl('line', { x1: x - 2.6, y1: cy - 2.6, x2: x + 2.6, y2: cy + 2.6, class: 'vk-glyph-fail' }));
    g.appendChild(svgEl('line', { x1: x - 2.6, y1: cy + 2.6, x2: x + 2.6, y2: cy - 2.6, class: 'vk-glyph-fail' }));
    return g;
  }
  return svgEl('circle', { cx: x, cy, r: 2.4, class: 'vk-glyph-none' }, [title('no predicate')]);
}

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

  const svg = svgEl('svg', { class: 'vk-pslope', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (series.length === 0) {
    const t = svgEl('text', { x: w / 2, y: h / 2, class: 'vk-empty-label', 'text-anchor': 'middle' });
    t.textContent = 'no paired board duels yet';
    svg.appendChild(t);
    return svg;
  }
  const allVals = [];
  for (const s of series) { if (isNum(s.a)) allVals.push(s.a); if (isNum(s.b)) allVals.push(s.b); }
  const [lo, hi] = extent(allVals);
  const y = scale([lo, hi], [h - padBottom, padTop]);

  const hL = svgEl('text', { x: leftX, y: 15, class: 'vk-slope-col', 'text-anchor': 'end' });
  hL.textContent = (o.left && o.left.title) || 'champion';
  const hR = svgEl('text', { x: rightX, y: 15, class: 'vk-slope-col', 'text-anchor': 'start' });
  hR.textContent = (o.right && o.right.title) || 'challenger';
  svg.appendChild(hL); svg.appendChild(hR);
  svg.appendChild(svgEl('line', { x1: leftX, x2: leftX, y1: y(hi), y2: y(lo), class: 'vk-slope-axis' }));
  svg.appendChild(svgEl('line', { x1: rightX, x2: rightX, y1: y(hi), y2: y(lo), class: 'vk-slope-axis' }));

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
    const dirCls = verdict === 'improved' ? 'vk-good' : verdict === 'regressed' ? 'vk-bad' : 'vk-flat';
    const g = svgEl('g', { class: 'vk-pslope-series', tabindex: o.onClick ? '0' : null, 'data-vk': 'pslope-series', 'data-id': s.id || s.label });

    if (ay != null && by != null) {
      const line = svgEl('line', { x1: leftX, y1: ay, x2: rightX, y2: by, class: 'vk-pslope-line ' + dirCls });
      line.appendChild(title(`${s.label}: ${fmt(s.a)} → ${fmt(s.b)} (${fmtSigned(s.b - s.a)}; ${verdict})`));
      g.appendChild(line);
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.6, class: 'vk-pslope-node ' + dirCls }));
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.6, class: 'vk-pslope-node ' + dirCls }));
    } else if (ay != null) {
      g.appendChild(svgEl('circle', { cx: leftX, cy: ay, r: 2.6, class: 'vk-pslope-node vk-flat' }, [title(`${s.label}: champion only ${fmt(s.a)}`)]));
    } else if (by != null) {
      g.appendChild(svgEl('circle', { cx: rightX, cy: by, r: 2.6, class: 'vk-pslope-node vk-flat' }, [title(`${s.label}: challenger only ${fmt(s.b)}`)]));
    }

    const ll = leftLabels[i];
    if (isNum(s.a)) {
      if (Math.abs(ll - y(s.a)) > 1.5) g.appendChild(svgEl('line', { x1: leftX - 4, y1: ll, x2: leftX, y2: y(s.a), class: 'vk-leader' }));
      const tx = svgEl('text', { x: leftX - 8, y: ll + 3, class: 'vk-pslope-label', 'text-anchor': 'end' });
      tx.textContent = `${shortLabel(s.label, 16)}  ${fmt(s.a, 1)}`;
      g.appendChild(tx);
    }
    const rl = rightLabels[i];
    if (isNum(s.b)) {
      if (Math.abs(rl - y(s.b)) > 1.5) g.appendChild(svgEl('line', { x1: rightX, y1: y(s.b), x2: rightX + 4, y2: rl, class: 'vk-leader' }));
      const tx = svgEl('text', { x: rightX + 8, y: rl + 3, class: 'vk-pslope-label', 'text-anchor': 'start' });
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
  const svg = svgEl('svg', { class: 'vk-heatmap', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img' });
  if (rows.length === 0 || cols.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'vk-empty-label' });
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
  // Default ramp: low loss → cool, high loss → warm. Themes can pass an
  const ramp = Array.isArray(o.ramp) && o.ramp.length === 2 ? o.ramp : ['#dfe9e6', '#b14a4a'];

  cols.forEach((c, j) => {
    const cx = labelW + j * cw + cw / 2;
    const t = svgEl('text', { x: cx, y: headH - 6, class: 'vk-hm-col',
      transform: `rotate(-45 ${cx} ${headH - 6})`, 'text-anchor': 'start' });
    t.textContent = shortLabel(c.label, 12);
    svg.appendChild(t);
  });
  rows.forEach((r, i) => {
    const ry = headH + i * ch;
    const lbl = svgEl('text', { x: labelW - 6, y: ry + ch - 5, class: 'vk-hm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(r.label, 18);
    svg.appendChild(lbl);
    cols.forEach((c, j) => {
      const v = o.value(r.id, c.id);
      const cx = labelW + j * cw;
      const t = isNum(v) ? (v - lo) / span : null;
      const fill = t == null ? 'var(--vk-cell-empty)' : lerpHex(ramp[0], ramp[1], t);
      const cell = svgEl('rect', { x: cx + 1, y: ry + 1, width: cw - 2, height: ch - 2, rx: 2, class: 'vk-hm-cell', fill },
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

export function mutationMatrix(opts) {
  const o = opts || {};
  const sites = Array.isArray(o.sites) ? o.sites : [];
  const gens = Array.isArray(o.gens) ? o.gens : [];
  const cw = o.cellW || 56;
  const ch = o.cellH || 24;
  const labelW = o.labelWidth || 230;
  const headH = o.headHeight || 28;
  const w = labelW + gens.length * cw + 8;
  const h = headH + sites.length * ch + 8;
  const svg = svgEl('svg', { class: 'vk-mutmatrix', width: w, height: h, viewBox: `0 0 ${w} ${h}`,
    preserveAspectRatio: 'xMidYMid meet', role: 'img', 'aria-label': 'Mutation sites by generation' });
  if (sites.length === 0 || gens.length === 0) {
    const t = svgEl('text', { x: 4, y: 16, class: 'vk-empty-label' });
    t.textContent = 'no mutation surface recorded';
    svg.appendChild(t);
    return svg;
  }
  gens.forEach((g, j) => {
    const cx = labelW + j * cw + cw / 2;
    const t = svgEl('text', { x: cx, y: headH - 8, class: 'vk-mm-col' + (g.promoted ? ' vk-promoted' : ''), 'text-anchor': 'middle' });
    t.textContent = shortLabel(g.label || g.id, 8);
    svg.appendChild(t);
  });
  sites.forEach((s, i) => {
    const ry = headH + i * ch;
    const lg = svgEl('g', null, [title(`${s.label}${s.sub ? ' · ' + s.sub : ''}`)]);
    const lbl = svgEl('text', { x: labelW - 8, y: ry + ch / 2, class: 'vk-mm-row', 'text-anchor': 'end' });
    lbl.textContent = shortLabel(s.label, 30);
    lg.appendChild(lbl);
    if (s.sub) {
      lbl.setAttribute('y', ry + ch / 2 - 3);
      const sub = svgEl('text', { x: labelW - 8, y: ry + ch / 2 + 9, class: 'vk-mm-row-sub', 'text-anchor': 'end' });
      sub.textContent = shortLabel(s.sub, 30);
      lg.appendChild(sub);
    }
    svg.appendChild(lg);
    gens.forEach((g, j) => {
      const cx = labelW + j * cw;
      const on = !!o.patched(s.id, g.id);
      const cell = svgEl('rect', { x: cx + 2, y: ry + 2, width: cw - 4, height: ch - 4, rx: 3,
        class: 'vk-mm-cell ' + (on ? 'vk-mm-on' : 'vk-mm-off'), 'data-vk': 'mut-cell',
        'data-gen': g.id, 'data-site': s.id },
      [title(`${g.id} ${on ? 'patched' : 'did not touch'} ${s.label}`)]);
      if (on) {
        const dot = svgEl('circle', { cx: cx + cw / 2, cy: ry + ch / 2, r: 3.4, class: 'vk-mm-dot' });
        if (o.onCell) { cell.style.cursor = 'pointer'; cell.addEventListener('click', () => o.onCell(g.id, s.id)); }
        svg.appendChild(cell); svg.appendChild(dot);
      } else {
        svg.appendChild(cell);
      }
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
