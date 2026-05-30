// components/diverging_bar.js — center-axis diverging bar chart.
//
// A row per measured quantity, each a horizontal bar growing from a
// shared center axis. The scalar is a LOSS (lower is better), so with
// `goodWhenNegative` (the default):
//
//   delta < 0  → improvement → GREEN bar growing to the RIGHT
//   delta > 0  → regression  → RED bar growing to the LEFT
//
// Rows sort by |delta| descending so the biggest movers read first. An
// optional per-row annotation (e.g. ⚠ with a title) flags a qualitative
// regression such as a pass→fail flip that the magnitude alone misses.
//
// Bars auto-scale to `max` when given, else to the largest |delta|.

import { el } from '../core/dom.js';
import { fmtDelta } from '../core/format.js';

/**
 * Render a diverging bar chart.
 *
 * rows — [{ label, delta, annotation }]
 *   annotation — optional { glyph, title } badge rendered beside a row.
 * goodWhenNegative — when true (default) negative deltas are the "good"
 *   direction (green/right); flip for higher-is-better quantities.
 * max — optional explicit magnitude ceiling for bar scaling.
 */
export function divergingBar({ rows, goodWhenNegative = true, max } = {}) {
  const list = (Array.isArray(rows) ? rows : [])
    .filter((r) => r && typeof r.delta === 'number' && isFinite(r.delta));
  // Largest |delta| first.
  const sorted = list.slice().sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));

  const wrap = el('div', { class: 'dbar', role: 'list' });
  if (sorted.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No deltas to compare.']));
    return wrap;
  }

  let ceiling = (typeof max === 'number' && max > 0) ? max : 0;
  if (!ceiling) {
    for (const r of sorted) {
      const a = Math.abs(r.delta);
      if (a > ceiling) ceiling = a;
    }
  }
  if (ceiling === 0) ceiling = 1;

  for (const r of sorted) {
    const good = goodWhenNegative ? r.delta < 0 : r.delta > 0;
    const cls = r.delta === 0 ? 'dbar-flat' : (good ? 'dbar-good' : 'dbar-bad');
    // Improvement bars sit on the right half, regressions on the left.
    const onRight = r.delta === 0 ? null : good;
    const pct = Math.min(1, Math.abs(r.delta) / ceiling) * 100;

    const fill = el('span', { class: 'dbar-fill ' + cls });
    fill.style.width = pct.toFixed(1) + '%';

    const half = el('span', {
      class: 'dbar-half ' + (onRight === true ? 'dbar-right' : onRight === false ? 'dbar-left' : 'dbar-center'),
    }, [fill]);

    const track = el('span', { class: 'dbar-track' }, [
      el('span', { class: 'dbar-axis', 'aria-hidden': 'true' }),
      half,
    ]);

    const ann = (r.annotation && r.annotation.glyph)
      ? el('span', {
          class: 'dbar-annotation',
          title: r.annotation.title || '',
          role: 'img',
          'aria-label': r.annotation.title || 'flag',
        }, [String(r.annotation.glyph)])
      : null;

    const row = el('div', { class: 'dbar-row', role: 'listitem' }, [
      el('span', { class: 'dbar-label' }, [
        String(r.label == null ? '' : r.label),
        ann,
      ]),
      track,
      el('span', { class: 'dbar-value mono ' + cls }, [fmtDelta(r.delta)]),
    ]);
    wrap.appendChild(row);
  }
  return wrap;
}
