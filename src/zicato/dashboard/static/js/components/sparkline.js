// components/sparkline.js — minimal inline SVG sparkline.
//
// A sparkline is "a small, simple, word-sized graphic" (Tufte). For
// the dashboard, that means an ~80×20 line chart with no axes, no
// labels — just shape. We emit pure SVG so it inlines into a card or
// table cell without runtime cost.
//
// The function returns a Node, not a string, so dom.js can append it
// directly into the reconcile spine.

import { svgEl, el } from '../core/dom.js';

/**
 * Render a sparkline.
 *
 * values — array of numbers; non-finite entries are treated as gaps.
 * opts:
 *   width        — default 80
 *   height       — default 22
 *   color        — stroke color, default var(--color-accent)
 *   fill         — area fill color (optional)
 *   strokeWidth  — default 1.5
 *   showLastDot  — default true
 *   ariaLabel    — accessibility label
 */
export function renderSparkline(values, opts) {
  const o = opts || {};
  const W = o.width || 80;
  const H = o.height || 22;
  const stroke = o.color || 'var(--color-accent)';
  const fill = o.fill || 'none';
  const sw = o.strokeWidth || 1.5;
  const showDot = o.showLastDot !== false;

  const series = Array.isArray(values) ? values : [];
  const finite = series.filter((v) => typeof v === 'number' && isFinite(v));
  if (finite.length === 0) {
    return el('span', {
      class: 'sparkline sparkline-empty',
      'aria-label': o.ariaLabel || 'no data',
    }, ['—']);
  }
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const range = (max - min) || 1;

  // Inner padding so the stroke and dot are not clipped.
  const pad = sw + 1;
  const innerW = W - 2 * pad;
  const innerH = H - 2 * pad;

  const stepX = series.length > 1 ? innerW / (series.length - 1) : 0;
  const points = [];
  for (let i = 0; i < series.length; i += 1) {
    const v = series[i];
    if (typeof v !== 'number' || !isFinite(v)) {
      points.push(null);
      continue;
    }
    const x = pad + i * stepX;
    const y = pad + innerH * (1 - (v - min) / range);
    points.push([x, y]);
  }

  // Build path; gaps break into a new subpath.
  let path = '';
  let pending = 'M';
  for (const pt of points) {
    if (pt == null) { pending = 'M'; continue; }
    path += `${pending}${pt[0].toFixed(2)},${pt[1].toFixed(2)} `;
    pending = 'L';
  }
  path = path.trim();

  // Optional fill: close the path to the baseline.
  let areaPath = null;
  if (fill !== 'none') {
    let ap = '';
    let started = false;
    for (let i = 0; i < points.length; i += 1) {
      const pt = points[i];
      if (pt == null) continue;
      ap += `${started ? 'L' : 'M'}${pt[0].toFixed(2)},${pt[1].toFixed(2)} `;
      started = true;
    }
    // Close to baseline.
    const lastIdx = points.length - 1;
    const firstIdx = points.findIndex((p) => p != null);
    if (started && lastIdx >= 0 && firstIdx >= 0) {
      const last = points[lastIdx];
      const first = points[firstIdx];
      const baseY = (pad + innerH).toFixed(2);
      ap += `L${last[0].toFixed(2)},${baseY} L${first[0].toFixed(2)},${baseY} Z`;
    }
    areaPath = ap;
  }

  const children = [];
  if (areaPath) {
    children.push(svgEl('path', {
      d: areaPath, fill, stroke: 'none',
    }));
  }
  children.push(svgEl('path', {
    d: path, fill: 'none', stroke, 'stroke-width': sw,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  }));
  if (showDot) {
    // Find last finite point.
    let lastPt = null;
    for (let i = points.length - 1; i >= 0; i -= 1) {
      if (points[i] != null) { lastPt = points[i]; break; }
    }
    if (lastPt) {
      children.push(svgEl('circle', {
        cx: lastPt[0].toFixed(2),
        cy: lastPt[1].toFixed(2),
        r: sw + 0.6,
        fill: stroke,
      }));
    }
  }
  const svg = svgEl('svg', {
    class: 'sparkline-svg',
    width: W, height: H, viewBox: `0 0 ${W} ${H}`,
    'aria-label': o.ariaLabel || 'sparkline',
    role: 'img',
  }, children);
  return el('span', { class: 'sparkline' }, [svg]);
}
