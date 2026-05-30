// js/v2/components/stateBlock.js — the canonical honest-state renderer.
//
// DASHBOARD-V2 §2 principle 4 + §5: EVERY async section in v2 renders
// its not-yet / running / empty / broken condition through THIS factory,
// never an ad-hoc "No data" string. That is what makes the dashboard
// honest while data streams: a section that is queued reads "queued", a
// section in flight shows running N/M, a genuinely-empty section says so
// plainly, and a failure shows the reason — four visually distinct,
// glyph-labeled states.
//
// Pure factory: builds and returns a fresh detached, re-render-safe DOM
// node every call; never mounts itself (the established convention).
//
//   stateBlock(kind, opts) — kind ∈ 'not_yet' | 'running' | 'empty' | 'broken'
//     opts.label   — override the default headline for the kind
//     opts.detail  — a secondary line of context (any kind)
//     opts.done    — running: units complete   (with .total → "N/M")
//     opts.total   — running: total units
//     opts.reason  — broken: the error reason (shown verbatim)

import { el } from '../../core/dom.js';

// Each kind has a glyph (redundant to color, grayscale-safe) + a default
// headline. Glyphs are plain text so they render in the mono data face.
const KINDS = {
  not_yet: { glyph: '◌', label: 'Queued' },
  running: { glyph: '◇', label: 'Running' },
  empty:   { glyph: '–', label: 'Nothing here' },
  broken:  { glyph: '!', label: 'Error' },
};

// Normalize an arbitrary kind to a known one; an unknown kind degrades
// to `broken` (a surprising state is a broken state, not a silent blank).
export function normalizeKind(kind) {
  const k = String(kind || '').toLowerCase();
  return KINDS[k] ? k : 'broken';
}

export function stateBlock(kind, opts) {
  const k = normalizeKind(kind);
  const o = opts || {};
  const spec = KINDS[k];

  const rowChildren = [
    el('span', { class: 'v2-state-glyph', 'aria-hidden': 'true' }, [spec.glyph]),
    el('span', { class: 'v2-state-label' }, [o.label != null ? String(o.label) : spec.label]),
  ];

  // running: surface progress as "N/M" + a fractional bar when we know
  // the total. Without a total we still say "running" honestly.
  let progressNode = null;
  if (k === 'running') {
    const total = Number(o.total);
    const done = Number(o.done);
    const haveTotal = isFinite(total) && total > 0;
    const haveDone = isFinite(done) && done >= 0;
    if (haveTotal && haveDone) {
      rowChildren.push(el('span', { class: 'v2-state-count v2-num' }, [
        `${done}/${total}`,
      ]));
      const frac = Math.max(0, Math.min(1, done / total));
      progressNode = el('div', {
        class: 'v2-state-progress',
        role: 'progressbar',
        'aria-valuenow': String(done),
        'aria-valuemin': '0',
        'aria-valuemax': String(total),
      }, [
        el('div', {
          class: 'v2-state-progress-fill',
          style: `width: ${(frac * 100).toFixed(1)}%;`,
        }),
      ]);
    } else if (haveDone) {
      rowChildren.push(el('span', { class: 'v2-state-count v2-num' }, [String(done)]));
    }
  }

  const children = [el('div', { class: 'v2-state-row' }, rowChildren)];
  if (progressNode) children.push(progressNode);

  // broken: the reason is load-bearing — show it verbatim. Other kinds
  // may carry an optional detail line.
  const detailText = k === 'broken'
    ? (o.reason != null ? String(o.reason) : (o.detail != null ? String(o.detail) : null))
    : (o.detail != null ? String(o.detail) : null);
  if (detailText) {
    children.push(el('div', {
      class: 'v2-state-detail',
      // broken reasons are often code-ish; render them in the data face.
      ...(k === 'broken' ? { class: 'v2-state-detail v2-mono' } : {}),
    }, [detailText]));
  }

  return el('div', {
    class: 'v2-state',
    'data-kind': k,
    role: k === 'broken' ? 'alert' : 'status',
    'aria-busy': k === 'running' ? 'true' : null,
  }, children);
}
