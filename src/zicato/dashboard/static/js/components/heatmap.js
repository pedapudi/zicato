// components/heatmap.js — colored-cell heatmap table.
//
// Cells encode magnitude as opacity over a base color:
//   - "sequential": 0 → neutral, max → accent at full opacity
//   - "symmetric":  negative → cool, positive → warm, zero → neutral
//
// The cell text remains the formatted numeric so the value is always
// readable; color is *redundant* information so the table degrades
// gracefully for color-blind users (and screenshots).

import { el } from '../core/dom.js';

function _clamp01(v) {
  if (!isFinite(v)) return 0;
  if (v < 0) return 0;
  if (v > 1) return 1;
  return v;
}

function _formatDefault(v) {
  if (v == null || (typeof v === 'number' && !isFinite(v))) return '—';
  if (typeof v !== 'number') return String(v);
  if (Math.abs(v) < 0.001 && v !== 0) return v.toExponential(1);
  if (Math.abs(v) >= 100) return v.toFixed(1);
  return v.toFixed(3);
}

/**
 * Render a colored-cell heatmap.
 *
 * opts:
 *   rows         — array of row keys (strings)
 *   cols         — array of column keys (strings)
 *   valueAt      — (row, col) -> number | null
 *   scale        — "sequential" | "symmetric" (default: sequential)
 *   formatValue  — optional formatter (number -> string)
 *   rowLabel     — optional header for the row-label column (e.g. "judge")
 *   maxAbs       — optional explicit ceiling for color magnitude. When
 *                  omitted we compute the absolute max from values.
 *   rowHref      — optional (rowKey) -> string — turns the row label
 *                  into an anchor.
 *   colHref      — optional (colKey) -> string — turns the column header
 *                  into an anchor.
 *   ariaLabel    — optional aria-label for the table
 */
export function renderHeatmapTable(opts) {
  const o = opts || {};
  const rows = Array.isArray(o.rows) ? o.rows : [];
  const cols = Array.isArray(o.cols) ? o.cols : [];
  const valueAt = typeof o.valueAt === 'function' ? o.valueAt : () => null;
  const scale = o.scale === 'symmetric' ? 'symmetric' : 'sequential';
  const fmt = typeof o.formatValue === 'function' ? o.formatValue : _formatDefault;

  // Compute the magnitude ceiling.
  let maxAbs = (typeof o.maxAbs === 'number' && o.maxAbs > 0) ? o.maxAbs : 0;
  if (!maxAbs) {
    for (const r of rows) {
      for (const c of cols) {
        const v = valueAt(r, c);
        if (typeof v === 'number' && isFinite(v)) {
          const a = Math.abs(v);
          if (a > maxAbs) maxAbs = a;
        }
      }
    }
  }
  if (maxAbs === 0) maxAbs = 1;

  const tbl = el('table', { class: 'heatmap', 'aria-label': o.ariaLabel || 'heatmap' });
  const thead = el('thead');
  const headRow = el('tr', null, [
    el('th', { class: 'heatmap-corner' }, [o.rowLabel || '']),
  ]);
  for (const c of cols) {
    const headerNode = (typeof o.colHref === 'function')
      ? el('a', { class: 'heatmap-col-link', href: o.colHref(c) }, [String(c)])
      : String(c);
    headRow.appendChild(el('th', { class: 'heatmap-col mono' }, [headerNode]));
  }
  thead.appendChild(headRow);
  tbl.appendChild(thead);

  const tbody = el('tbody');
  for (const r of rows) {
    const tr = el('tr');
    const labelNode = (typeof o.rowHref === 'function')
      ? el('a', { class: 'heatmap-row-link mono', href: o.rowHref(r) }, [String(r)])
      : el('span', { class: 'mono' }, [String(r)]);
    tr.appendChild(el('th', { class: 'heatmap-row' }, [labelNode]));
    for (const c of cols) {
      const v = valueAt(r, c);
      const cell = _renderCell(v, maxAbs, scale, fmt);
      tr.appendChild(cell);
    }
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);

  // Wrap with a small legend strip when there is data.
  const wrap = el('div', { class: 'heatmap-wrap' }, [
    tbl,
    _renderLegend(scale, maxAbs, fmt),
  ]);
  return wrap;
}

function _renderCell(v, maxAbs, scale, fmt) {
  if (v == null || (typeof v === 'number' && !isFinite(v))) {
    return el('td', { class: 'heatmap-cell heatmap-cell-empty mono' }, ['—']);
  }
  if (typeof v !== 'number') {
    return el('td', { class: 'heatmap-cell mono' }, [String(v)]);
  }
  const mag = _clamp01(Math.abs(v) / maxAbs);
  // Opacity floor so a non-zero cell is still visibly tinted.
  const alpha = v === 0 ? 0 : Math.max(0.08, mag);
  let bg;
  if (scale === 'symmetric') {
    bg = v >= 0
      ? `color-mix(in srgb, var(--color-heatmap-positive) ${(alpha * 100).toFixed(0)}%, var(--color-heatmap-neutral))`
      : `color-mix(in srgb, var(--color-heatmap-negative) ${(alpha * 100).toFixed(0)}%, var(--color-heatmap-neutral))`;
  } else {
    bg = `color-mix(in srgb, var(--color-heatmap-positive) ${(alpha * 100).toFixed(0)}%, var(--color-heatmap-neutral))`;
  }
  const text = fmt(v);
  // Cells with high opacity get inverted text for contrast.
  const fgClass = alpha > 0.5 ? 'heatmap-cell-fg-light' : '';
  return el('td', {
    class: 'heatmap-cell mono ' + fgClass,
    style: `background:${bg}`,
    title: text,
  }, [text]);
}

function _renderLegend(scale, maxAbs, fmt) {
  const wrap = el('div', { class: 'heatmap-legend' });
  if (scale === 'symmetric') {
    wrap.appendChild(el('span', { class: 'heatmap-legend-label' }, ['−' + fmt(maxAbs)]));
    wrap.appendChild(el('span', { class: 'heatmap-legend-bar heatmap-legend-bar-sym' }));
    wrap.appendChild(el('span', { class: 'heatmap-legend-label' }, ['+' + fmt(maxAbs)]));
  } else {
    wrap.appendChild(el('span', { class: 'heatmap-legend-label' }, ['0']));
    wrap.appendChild(el('span', { class: 'heatmap-legend-bar heatmap-legend-bar-seq' }));
    wrap.appendChild(el('span', { class: 'heatmap-legend-label' }, [fmt(maxAbs)]));
  }
  return wrap;
}
