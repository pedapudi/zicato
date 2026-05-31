// variants/B/lib/charts.js — the editorial chart toolkit.
//
// Variant B ("Editorial Lab Notebook") is typography + whitespace + a few
// beautiful, restrained charts embedded in flowing prose. This module is
// the chart vocabulary: pure SVG factories returning detached, re-render-
// safe nodes (the established convention). NO tables here — these are the
// word-sized and figure-sized graphics the notebook entries lean on.
//
// Every factory is total: a degenerate input (no points, all-null, a
// single node) yields a tasteful, labeled fallback rather than an empty
// box or a thrown error. Color is read from CSS custom properties so the
// charts restyle with the theme; the semantic mapping (improve / regress /
// caution) is always redundant to a label or glyph for accessibility.

import { svgEl, el } from '../../../core/dom.js';
import { SVG_NS } from '../../../core/format.js';

// A finite-number guard used everywhere.
export function fin(v) { return typeof v === 'number' && isFinite(v); }

// ---------------------------------------------------------------------------
// sparkline — a word-sized loss/score trajectory. Lower is better
// (the tournament ranks by loss), so the line falling is improvement; we
// mark the min point and the endpoints. `points` is an array of numbers
// (nulls allowed — they break the line into segments honestly).
// ---------------------------------------------------------------------------
export function sparkline(points, opts = {}) {
  const width = opts.width || 220;
  const height = opts.height || 44;
  const pad = opts.pad == null ? 4 : opts.pad;
  const vals = Array.isArray(points) ? points.slice() : [];
  const finite = vals.filter(fin);
  const svg = svgEl('svg', {
    class: 'vb-spark', width, height,
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': opts.ariaLabel || 'trend sparkline',
    preserveAspectRatio: 'none',
  });
  if (finite.length === 0) {
    svg.appendChild(svgEl('line', {
      x1: pad, y1: height / 2, x2: width - pad, y2: height / 2,
      class: 'vb-spark-empty',
    }));
    return svg;
  }
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const span = max - min || 1;
  const n = vals.length;
  const xOf = (i) => pad + (n <= 1 ? (width - 2 * pad) / 2 : (i / (n - 1)) * (width - 2 * pad));
  // lower value → lower y-pixel; we invert so an improving (falling) loss
  // curve visually descends toward the bottom-right.
  const yOf = (v) => pad + ((v - min) / span) * (height - 2 * pad);

  // Build line segments split on nulls.
  let d = '';
  let pen = false;
  vals.forEach((v, i) => {
    if (!fin(v)) { pen = false; return; }
    d += `${pen ? 'L' : 'M'}${xOf(i).toFixed(1)} ${yOf(v).toFixed(1)} `;
    pen = true;
  });
  // A faint area under the curve for body.
  if (opts.area !== false && finite.length > 1) {
    const firstI = vals.findIndex(fin);
    const lastI = vals.length - 1 - vals.slice().reverse().findIndex(fin);
    const area = `M${xOf(firstI).toFixed(1)} ${(height - pad).toFixed(1)} `
      + d.replace(/^M/, 'L')
      + `L${xOf(lastI).toFixed(1)} ${(height - pad).toFixed(1)} Z`;
    svg.appendChild(svgEl('path', { d: area, class: 'vb-spark-area' }));
  }
  svg.appendChild(svgEl('path', { d: d.trim(), class: 'vb-spark-line', fill: 'none' }));

  // Endpoints + the best (lowest) point.
  const minI = vals.indexOf(min);
  const lastI = vals.length - 1 - vals.slice().reverse().findIndex(fin);
  svg.appendChild(svgEl('circle', {
    cx: xOf(minI), cy: yOf(min), r: 2.6, class: 'vb-spark-best',
  }));
  svg.appendChild(svgEl('circle', {
    cx: xOf(lastI), cy: yOf(vals[lastI]), r: 2.2, class: 'vb-spark-last',
  }));
  return svg;
}

// ---------------------------------------------------------------------------
// slopegraph — Tufte's two-column slope: a value moving from `from` to `to`.
// Used as the hero comparative figure (champion → challenger). Each `series`
// is { label, from, to, verdict }. verdict ∈ improve|regress|neutral and is
// rendered redundantly (color + a ↑/↓/→ glyph in the value).
// ---------------------------------------------------------------------------
export function slopegraph(series, opts = {}) {
  const rows = Array.isArray(series) ? series.filter((s) => s && (fin(s.from) || fin(s.to))) : [];
  const width = opts.width || 460;
  const height = opts.height || (70 + rows.length * 10);
  const padX = 120;
  const padY = 26;
  const fromX = padX;
  const toX = width - padX;
  const fig = el('figure', { class: 'vb-slope' });
  if (rows.length === 0) {
    fig.appendChild(el('figcaption', { class: 'vb-fig-empty' }, [
      opts.emptyLabel || 'No comparable values yet.',
    ]));
    return fig;
  }
  const svg = svgEl('svg', {
    width: '100%', height, viewBox: `0 0 ${width} ${height}`,
    role: 'img', 'aria-label': opts.ariaLabel || 'champion to challenger slopegraph',
    class: 'vb-slope-svg',
  });
  // Shared scale across all series so slopes are comparable.
  const allVals = [];
  for (const s of rows) { if (fin(s.from)) allVals.push(s.from); if (fin(s.to)) allVals.push(s.to); }
  const min = Math.min(...allVals);
  const max = Math.max(...allVals);
  const span = max - min || 1;
  const yOf = (v) => padY + ((v - min) / span) * (height - 2 * padY);

  // Column captions.
  svg.appendChild(svgEl('text', {
    x: fromX, y: 14, class: 'vb-slope-col', 'text-anchor': 'start',
  }, [opts.fromLabel || 'champion']));
  svg.appendChild(svgEl('text', {
    x: toX, y: 14, class: 'vb-slope-col', 'text-anchor': 'end',
  }, [opts.toLabel || 'challenger']));

  for (const s of rows) {
    const v = s.verdict || 'neutral';
    const y1 = fin(s.from) ? yOf(s.from) : null;
    const y2 = fin(s.to) ? yOf(s.to) : null;
    if (y1 != null && y2 != null) {
      svg.appendChild(svgEl('line', {
        x1: fromX, y1, x2: toX, y2,
        class: `vb-slope-line vb-${v}`,
      }));
    }
    if (y1 != null) {
      svg.appendChild(svgEl('circle', { cx: fromX, cy: y1, r: 3, class: `vb-slope-dot vb-${v}` }));
    }
    if (y2 != null) {
      svg.appendChild(svgEl('circle', { cx: toX, cy: y2, r: 3, class: `vb-slope-dot vb-${v}` }));
    }
    const glyph = v === 'improve' ? '↓' : v === 'regress' ? '↑' : '→';
    svg.appendChild(svgEl('text', {
      x: fromX - 8, y: (y1 != null ? y1 : y2) + 3, class: 'vb-slope-label', 'text-anchor': 'end',
    }, [`${s.label}`]));
    svg.appendChild(svgEl('text', {
      x: toX + 8, y: (y2 != null ? y2 : y1) + 3, class: `vb-slope-val vb-${v}`, 'text-anchor': 'start',
    }, [`${glyph} ${fmtNum(s.to != null ? s.to : s.from, opts.digits)}`]));
  }
  fig.appendChild(svg);
  if (opts.caption) fig.appendChild(el('figcaption', { class: 'vb-fig-cap' }, [opts.caption]));
  return fig;
}

// ---------------------------------------------------------------------------
// divergingBars — per-entry / per-kind A/B movement as horizontal bars
// growing left (improved) or right (worsened) from a center axis. Each
// `item` is { label, delta, verdict, onClick? }. A data-dense, beautiful
// alternative to a table of numbers.
// ---------------------------------------------------------------------------
export function divergingBars(items, opts = {}) {
  const rows = Array.isArray(items) ? items.filter((d) => d && fin(d.delta)) : [];
  const wrap = el('div', { class: 'vb-diverge' });
  if (rows.length === 0) {
    wrap.appendChild(el('p', { class: 'vb-fig-empty' }, [opts.emptyLabel || 'No movement recorded.']));
    return wrap;
  }
  const maxAbs = Math.max(...rows.map((d) => Math.abs(d.delta))) || 1;
  for (const d of rows) {
    const v = d.verdict || (d.delta < 0 ? 'improve' : d.delta > 0 ? 'regress' : 'neutral');
    const frac = Math.abs(d.delta) / maxAbs;
    const row = el('div', {
      class: 'vb-diverge-row' + (d.onClick ? ' vb-clickable' : ''),
      role: d.onClick ? 'button' : null,
      tabindex: d.onClick ? '0' : null,
      title: d.title || `${d.label}: ${fmtSigned(d.delta, opts.digits)}`,
    }, [
      el('span', { class: 'vb-diverge-label' }, [String(d.label)]),
      el('span', { class: 'vb-diverge-track' }, [
        el('span', {
          class: `vb-diverge-fill vb-diverge-${d.delta <= 0 ? 'left' : 'right'} vb-${v}`,
          style: `width:${(frac * 50).toFixed(1)}%`,
        }),
      ]),
      el('span', { class: `vb-diverge-val vb-${v}` }, [fmtSigned(d.delta, opts.digits)]),
    ]);
    if (d.onClick) {
      row.addEventListener('click', d.onClick);
      row.addEventListener('keydown', (ev) => {
        if (ev && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); d.onClick(ev); }
      });
    }
    wrap.appendChild(row);
  }
  return wrap;
}

// ---------------------------------------------------------------------------
// trajectoryStory — the lineage as an elegant, non-colliding optimization
// curve. Nodes laid on an x-spine in id order; y = scalar (lower → higher
// on screen). Promoted nodes ride the bold through-line; rejected nodes
// branch slightly off their parent. The live node pulses. Clickable.
// Renders well at 1..N nodes (a single node centers, labeled).
// ---------------------------------------------------------------------------
export function trajectoryStory(nodes, opts = {}) {
  const list = Array.isArray(nodes) ? nodes.filter((n) => n && n.id != null) : [];
  const width = opts.width || 720;
  const height = opts.height || 200;
  const padX = 48;
  const padY = 44;
  const svg = svgEl('svg', {
    class: 'vb-traj', width: '100%', height,
    viewBox: `0 0 ${width} ${height}`,
    role: 'img', 'aria-label': opts.ariaLabel || 'lineage trajectory',
  });
  if (list.length === 0) {
    svg.appendChild(svgEl('text', {
      x: width / 2, y: height / 2, class: 'vb-traj-empty', 'text-anchor': 'middle',
    }, [opts.emptyLabel || 'No generations yet — the lineage begins with the first run.']));
    return svg;
  }
  const n = list.length;
  const scalars = list.map((nd) => (fin(nd.scalar) ? nd.scalar : null)).filter((v) => v != null);
  const min = scalars.length ? Math.min(...scalars) : 0;
  const max = scalars.length ? Math.max(...scalars) : 1;
  const span = max - min || 1;
  const xOf = (i) => (n <= 1 ? width / 2 : padX + (i / (n - 1)) * (width - 2 * padX));
  const yOf = (v) => (v == null ? height / 2 : padY + ((v - min) / span) * (height - 2 * padY));

  const idIndex = new Map(list.map((nd, i) => [String(nd.id), i]));
  const xy = list.map((nd, i) => ({ x: xOf(i), y: yOf(fin(nd.scalar) ? nd.scalar : null), nd, i }));

  // Promoted spine = bold path through promoted nodes in order.
  const promoted = xy.filter((p) => p.nd.verdict === 'promoted');
  if (promoted.length > 1) {
    let d = '';
    promoted.forEach((p, i) => { d += `${i ? 'L' : 'M'}${p.x.toFixed(1)} ${p.y.toFixed(1)} `; });
    svg.appendChild(svgEl('path', { d: d.trim(), class: 'vb-traj-spine', fill: 'none' }));
  }
  // Edges to parents (rejected branch off, faint).
  for (const p of xy) {
    const pid = p.nd.parentId != null ? String(p.nd.parentId) : null;
    if (pid == null || !idIndex.has(pid)) continue;
    const parent = xy[idIndex.get(pid)];
    if (p.nd.verdict === 'promoted' && parent.nd.verdict === 'promoted') continue; // spine drew it
    svg.appendChild(svgEl('path', {
      d: `M${parent.x.toFixed(1)} ${parent.y.toFixed(1)} L${p.x.toFixed(1)} ${p.y.toFixed(1)}`,
      class: `vb-traj-edge vb-${p.nd.verdict === 'rejected' ? 'regress' : 'neutral'}`,
      fill: 'none',
    }));
  }
  // Nodes.
  for (const p of xy) {
    const v = p.nd.verdict;
    const cls = v === 'promoted' ? 'improve' : v === 'rejected' ? 'regress' : 'neutral';
    const g = svgEl('g', {
      class: 'vb-traj-node' + (opts.onSelect ? ' vb-clickable' : '')
        + (p.nd.live ? ' vb-traj-live' : ''),
      role: opts.onSelect ? 'button' : null,
      tabindex: opts.onSelect ? '0' : null,
      'aria-label': `generation ${p.nd.label || p.nd.id}`,
    });
    if (p.nd.live) {
      g.appendChild(svgEl('circle', { cx: p.x, cy: p.y, r: 9, class: 'vb-traj-pulse' }));
    }
    g.appendChild(svgEl('circle', { cx: p.x, cy: p.y, r: 5.5, class: `vb-traj-dot vb-${cls}` }));
    g.appendChild(svgEl('text', {
      x: p.x, y: height - 14, class: 'vb-traj-id', 'text-anchor': 'middle',
    }, [String(p.nd.label || p.nd.id)]));
    if (fin(p.nd.scalar)) {
      g.appendChild(svgEl('text', {
        x: p.x, y: p.y - 11, class: 'vb-traj-scalar', 'text-anchor': 'middle',
      }, [fmtNum(p.nd.scalar, 3)]));
    }
    if (opts.onSelect) {
      g.addEventListener('click', () => opts.onSelect(String(p.nd.id)));
      g.addEventListener('keydown', (ev) => {
        if (ev && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); opts.onSelect(String(p.nd.id)); }
      });
    }
    svg.appendChild(g);
  }
  return svg;
}

// ---------------------------------------------------------------------------
// progressRing — a small SVG ring for live run progress (0..1).
// ---------------------------------------------------------------------------
export function progressRing(frac, opts = {}) {
  const size = opts.size || 28;
  const r = (size - 6) / 2;
  const c = 2 * Math.PI * r;
  const f = fin(frac) ? Math.max(0, Math.min(1, frac)) : 0;
  const svg = svgEl('svg', {
    class: 'vb-ring', width: size, height: size, viewBox: `0 0 ${size} ${size}`,
    role: 'img', 'aria-label': opts.ariaLabel || `progress ${Math.round(f * 100)}%`,
  });
  svg.appendChild(svgEl('circle', {
    cx: size / 2, cy: size / 2, r, class: 'vb-ring-track', fill: 'none',
  }));
  svg.appendChild(svgEl('circle', {
    cx: size / 2, cy: size / 2, r, class: 'vb-ring-fill', fill: 'none',
    'stroke-dasharray': `${(c * f).toFixed(2)} ${c.toFixed(2)}`,
    transform: `rotate(-90 ${size / 2} ${size / 2})`,
  }));
  return svg;
}

// ---------------------------------------------------------------------------
// formatting helpers (local — kept here so the toolkit is self-contained).
// ---------------------------------------------------------------------------
export function fmtNum(v, digits) {
  if (!fin(v)) return '—';
  return v.toFixed(digits == null ? 3 : digits);
}
export function fmtSigned(v, digits) {
  if (!fin(v)) return '—';
  return (v >= 0 ? '+' : '') + v.toFixed(digits == null ? 3 : digits);
}

// Re-export the SVG namespace so consumers can build raw nodes if needed.
export { SVG_NS };
