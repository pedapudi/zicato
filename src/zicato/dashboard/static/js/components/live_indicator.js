// components/live_indicator.js — small dot + label for live/stale status.
//
// Two states: live (emerald, pulsing) and stale (gray, static). The
// indicator is a span so it inlines into a card header or sidebar title.

import { el } from '../core/dom.js';

/**
 * Render a liveness indicator.
 *
 * opts:
 *   live   — boolean
 *   label  — optional text; defaults to "LIVE" / "STALE"
 *   size   — "sm" | "md" — controls dot diameter
 *   showLabel — default true; set false for a bare dot
 */
export function renderLiveIndicator(opts) {
  const o = opts || {};
  const live = !!o.live;
  const size = o.size || 'md';
  const showLabel = o.showLabel !== false;
  const labelText = o.label || (live ? 'live' : 'stale');
  const cls = `live-indicator live-indicator-${size}` + (live ? ' is-live' : ' is-stale');
  return el('span', { class: cls }, [
    el('span', { class: 'live-indicator-dot', 'aria-hidden': 'true' }),
    showLabel ? el('span', { class: 'live-indicator-label' }, [labelText]) : null,
  ]);
}
