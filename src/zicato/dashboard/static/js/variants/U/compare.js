// variants/U/compare.js — the comparison-first primitives (U's S-derived signature).
//
// U folds S's FIRST-CLASS comparison into the roomy Atlas V base. Two builders:
//
//   comparePicker(o)  — a "compare with…" affordance: a labelled <select> of
//     candidates that, on change, sets/clears the `cmp` target in the route
//     (URL-encoded, so the comparison deep-links). It never navigates away —
//     it splits the SAME detail pane.
//
//   splitFrame(o)     — a two-column frame. Side A renders the primary
//     selection; side B renders the comparison target (or a prompt when none is
//     chosen). Each side gets its OWN host so its digest gate fires
//     independently (one side changing does not rebuild the other).
//
// Q/M's generous spacing governs the gutter + side widths (set in atlasv.css).

import { el } from '../../core/dom.js';

// A candidate picker that sets the `cmp` route param. `current` is the primary
// gen (excluded from the options); `value` is the currently-compared gen.
export function comparePicker(o) {
  const wrap = el('label', { class: 'vu-cmp-picker' }, [
    el('span', { class: 'vu-cmp-picker-lab', text: o.label || 'compare with…' }),
  ]);
  const sel = el('select', { class: 'vu-cmp-select', 'aria-label': o.label || 'compare with' });
  sel.appendChild(el('option', { value: '', text: o.noneLabel || '— none —' }));
  for (const opt of (o.options || [])) {
    if (opt.id === o.current) continue;
    const attrs = { value: opt.id, text: opt.label || opt.id };
    const node = el('option', attrs);
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

// A two-column comparison frame. `a` and `b` are { title, build(host) } — each
// build paints into its own host so the digest gate is per-side. When there is
// no comparison target, side B shows `emptyPrompt`.
export function splitFrame(o) {
  const frame = el('div', { class: 'vu-split' + (o.b ? '' : ' vu-split-single') });
  frame.appendChild(splitSide(o.a, 'a'));
  frame.appendChild(splitSide(o.b || { title: o.emptyTitle || '', empty: o.emptyPrompt || 'Pick a candidate to compare side by side.' }, 'b'));
  return frame;
}

function splitSide(side, which) {
  const col = el('section', { class: 'vu-split-side vu-split-' + which });
  if (side.title) {
    col.appendChild(el('div', { class: 'vu-split-head' }, [
      el('span', { class: 'vu-split-tag', text: which === 'a' ? 'A' : 'B' }),
      el('span', { class: 'vu-split-title', text: side.title }),
      side.sub ? el('span', { class: 'vu-split-sub', text: side.sub }) : null,
    ].filter(Boolean)));
  }
  const host = el('div', { class: 'vu-split-host' });
  if (side.build) side.build(host);
  else if (side.empty) host.appendChild(el('p', { class: 'dn-empty', text: side.empty }));
  col.appendChild(host);
  return col;
}
