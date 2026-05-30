// components/scalar_waterfall.js — per-component scalar decomposition.
//
// The combined scalar is a weighted sum over components (the drift
// term, the pass term, each scored namespace). When a child's scalar
// moves, this waterfall shows WHICH component moved it: each component's
// champion→challenger contribution is a stacked horizontal segment,
// colored by sign, ending in the running total.
//
// The scalar is a LOSS — lower is better — so a NEGATIVE contribution is
// an improvement (green) and a POSITIVE contribution is a regression
// (red). The segments are laid out left→right in the order given; the
// final total bar reads as the net champion→challenger delta.

import { el } from '../core/dom.js';
import { fmtDelta } from '../core/format.js';

/**
 * Render a scalar waterfall.
 *
 * components — [{ name, delta }] per-component champion→challenger
 *   contribution. These sum to the total scalar delta.
 * label — optional caption for the whole figure.
 */
export function scalarWaterfall({ components, label } = {}) {
  const list = (Array.isArray(components) ? components : [])
    .filter((c) => c && typeof c.delta === 'number' && isFinite(c.delta));

  const wrap = el('div', { class: 'swfall' });
  if (label != null) {
    wrap.appendChild(el('div', { class: 'swfall-caption' }, [String(label)]));
  }
  if (list.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No scalar components.']));
    return wrap;
  }

  const total = list.reduce((acc, c) => acc + c.delta, 0);
  // Scale segment widths against the largest single |contribution| so
  // the dominant mover is visually full-width.
  let ceiling = 0;
  for (const c of list) {
    const a = Math.abs(c.delta);
    if (a > ceiling) ceiling = a;
  }
  if (ceiling === 0) ceiling = 1;

  const body = el('div', { class: 'swfall-rows', role: 'list' });
  for (const c of list) {
    const good = c.delta < 0;                 // loss fell → improvement
    const cls = c.delta === 0 ? 'swfall-flat' : (good ? 'swfall-good' : 'swfall-bad');
    const pct = Math.min(1, Math.abs(c.delta) / ceiling) * 100;
    const fill = el('span', { class: 'swfall-fill ' + cls });
    fill.style.width = pct.toFixed(1) + '%';
    body.appendChild(el('div', { class: 'swfall-row', role: 'listitem' }, [
      el('span', { class: 'swfall-name' }, [String(c.name == null ? '' : c.name)]),
      el('span', { class: 'swfall-track' }, [fill]),
      el('span', { class: 'swfall-value mono ' + cls }, [fmtDelta(c.delta)]),
    ]));
  }
  wrap.appendChild(body);

  const totalGood = total < 0;
  const totalCls = total === 0 ? 'swfall-flat' : (totalGood ? 'swfall-good' : 'swfall-bad');
  wrap.appendChild(el('div', { class: 'swfall-total' }, [
    el('span', { class: 'swfall-name' }, ['total']),
    el('span', { class: 'swfall-track' }),
    el('span', { class: 'swfall-value mono ' + totalCls }, [fmtDelta(total)]),
  ]));
  return wrap;
}
