// js/compare.js — the side-by-side COMPARE primitives (S's signature,
// ported self-contained into Console IV).
//
// S's first-class comparison is folded into the P anchor: a candidate's detail
// can SPLIT into two candidates read side by side. Two builders:
//
//   comparePicker(o)  — a "compare with…" affordance: a labelled <select> of
//     candidates that, on change, sets/clears the `cmp` target on the route
//     (URL-encoded via the router, so the comparison deep-links). It never
//     navigates away — it splits the SAME detail pane.
//
//   splitFrame(o)     — a two-column frame. Side A renders the primary
//     selection; side B renders the comparison target (or a prompt when none is
//     chosen). Each side gets its OWN host so its digest gate fires
//     independently (one side changing does not rebuild the other).
//
// Q's generous spacing/proportion governs the gutter + side widths (see
// css/console.css, the `.dt-split*` rules).

import { el } from './core/dom.js';

// A candidate picker that sets the `cmp` route param. `current` is the primary
// gen (excluded from the options); `value` is the currently-compared gen.
export function comparePicker(o) {
  const wrap = el('label', { class: 'dt-cmp-picker' }, [
    el('span', { class: 'dt-cmp-picker-lab', text: o.label || 'compare with…' }),
  ]);
  const sel = el('select', { class: 'dt-cmp-select', 'aria-label': o.label || 'compare with' });
  sel.appendChild(el('option', { value: '', text: o.noneLabel || '— none —' }));
  for (const opt of (o.options || [])) {
    if (opt.id === o.current) continue;
    const node = el('option', { value: opt.id, text: opt.label || opt.id });
    if (opt.id === o.value) node.setAttribute('selected', 'selected');
    sel.appendChild(node);
  }
  sel.addEventListener('change', (ev) => {
    const v = (ev && ev.target && ev.target.value) || sel.value || '';
    o.onChange(v || null);
  });
  // harness: reflect the chosen value so a test can read it.
  sel.value = o.value || '';
  wrap.appendChild(sel);
  return wrap;
}

// A two-column comparison frame. `a` and `b` are { title, sub, build(host) } —
// each build paints into its own host so the digest gate is per-side. When
// there is no comparison target, side B shows `emptyPrompt`.
export function splitFrame(o) {
  const frame = el('div', { class: 'dt-split' + (o.b ? '' : ' dt-split-single') });
  frame.appendChild(splitSide(o.a, 'a'));
  frame.appendChild(splitSide(o.b || { title: o.emptyTitle || '', empty: o.emptyPrompt || 'Pick a candidate to compare side by side.' }, 'b'));
  return frame;
}

function splitSide(side, which) {
  const col = el('section', { class: 'dt-split-side dt-split-' + which });
  if (side.title) {
    col.appendChild(el('div', { class: 'dt-split-head' }, [
      el('span', { class: 'dt-split-tag', text: which === 'a' ? 'A' : 'B' }),
      el('span', { class: 'dt-split-title', text: side.title }),
      side.sub ? el('span', { class: 'dt-split-sub', text: side.sub }) : null,
    ].filter(Boolean)));
  }
  const host = el('div', { class: 'dt-split-host' });
  if (side.build) side.build(host);
  else if (side.empty) host.appendChild(el('p', { class: 'dn-empty', text: side.empty }));
  col.appendChild(host);
  return col;
}
