// variants/S/compare.js — the comparison-first primitives (S's signature).
//
// S's detail pane reads cleanly because comparison is FIRST-CLASS. Two builders:
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
// M's generous spacing/proportion governs the gutter + side widths.

import { el } from '../../core/dom.js';

// A candidate picker that sets the `cmp` route param. `current` is the primary
// gen (excluded from the options); `value` is the currently-compared gen.
export function comparePicker(o) {
  const wrap = el('label', { class: 'vs-cmp-picker' }, [
    el('span', { class: 'vs-cmp-picker-lab', text: o.label || 'compare with…' }),
  ]);
  const sel = el('select', { class: 'vs-cmp-select', 'aria-label': o.label || 'compare with' });
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
  const frame = el('div', { class: 'vs-split' + (o.b ? '' : ' vs-split-single') });
  frame.appendChild(splitSide(o.a, 'a'));
  frame.appendChild(splitSide(o.b || { title: o.emptyTitle || '', empty: o.emptyPrompt || 'Pick a candidate to compare side by side.' }, 'b'));
  return frame;
}

function splitSide(side, which) {
  const col = el('section', { class: 'vs-split-side vs-split-' + which });
  if (side.title) {
    col.appendChild(el('div', { class: 'vs-split-head' }, [
      el('span', { class: 'vs-split-tag', text: which === 'a' ? 'A' : 'B' }),
      el('span', { class: 'vs-split-title', text: side.title }),
      side.sub ? el('span', { class: 'vs-split-sub', text: side.sub }) : null,
    ].filter(Boolean)));
  }
  const host = el('div', { class: 'vs-split-host' });
  if (side.build) side.build(host);
  else if (side.empty) host.appendChild(el('p', { class: 'dn-empty', text: side.empty }));
  col.appendChild(host);
  return col;
}
