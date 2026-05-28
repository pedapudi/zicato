// components/loading.js — shared loading / empty state helpers.
//
// The dashboard distinguishes two states that previously fell through
// to the same fallback copy:
//
//   * Loading — the backing payload is `null` / `undefined` because the
//     SSE snapshot has not landed yet (or the per-section fetch is in
//     flight). The user is told the data is on its way.
//   * Empty   — the backing payload is a *container* (array / object)
//     that is genuinely empty. The user is told there is nothing here.
//
// Before this split, both branches said e.g. "No generations yet." even
// on a workspace with eight generations, because the SSE socket had not
// yet delivered the epoch definition. That is misleading.
//
// `renderLoadingState({ label })` returns a muted single-line node with
// an animated ellipsis. `renderEmptyState(text)` returns the existing
// muted empty-line. Both reuse the `.empty` design token so the spacing
// matches every other muted line in a card body.

import { el } from '../core/dom.js';

// A single-line muted "Loading…" placeholder. The ellipsis is rendered
// inline so the line lays out identically to the empty-state line it
// replaces — no card-body height jumps when the data lands.
export function renderLoadingState({ label } = {}) {
  return el('p', { class: 'empty loading-line' }, [
    el('span', { class: 'loading-dot', 'aria-hidden': 'true' }),
    el('span', null, [String(label || 'Loading') + '…']),
  ]);
}

// The genuine empty-state line: backing data has loaded and is empty.
// Kept separate from `loading-line` so a downstream test can pin one
// shape without matching the other by accident.
export function renderEmptyState(text) {
  return el('p', { class: 'empty empty-line' }, [String(text || 'Nothing here yet.')]);
}
