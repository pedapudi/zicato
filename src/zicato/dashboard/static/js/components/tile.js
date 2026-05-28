// components/tile.js — big-number metric tile.
//
// One tile carries a small uppercase label, a large numeric value, and
// (optionally) a unit suffix, a delta with an arrow, and a sentiment
// (good / bad / flat / neutral) that colors the delta.
//
// The tile is layout-stable: missing fields collapse to nothing, but
// the slot remains so a strip of tiles aligns vertically.

import { el } from '../core/dom.js';

const SENTIMENT_CLASS = {
  good: 'tile-delta-good',
  bad: 'tile-delta-bad',
  flat: 'tile-delta-flat',
  neutral: 'tile-delta-flat',
};

const ARROWS = {
  up: '↑',
  down: '↓',
  flat: '→',
};

function _arrow(delta) {
  if (typeof delta === 'string') {
    const trimmed = delta.trim();
    if (trimmed.startsWith('+')) return ARROWS.up;
    if (trimmed.startsWith('-') || trimmed.startsWith('−')) return ARROWS.down;
    return ARROWS.flat;
  }
  if (typeof delta === 'number' && isFinite(delta)) {
    if (delta > 0) return ARROWS.up;
    if (delta < 0) return ARROWS.down;
    return ARROWS.flat;
  }
  return ARROWS.flat;
}

function _fmtDelta(delta) {
  if (delta == null) return null;
  if (typeof delta === 'string') return delta;
  if (typeof delta === 'number' && isFinite(delta)) {
    const sign = delta > 0 ? '+' : (delta < 0 ? '−' : '');
    const mag = Math.abs(delta);
    const digits = mag >= 100 ? 1 : (mag >= 10 ? 2 : 3);
    return sign + mag.toFixed(digits);
  }
  return String(delta);
}

/**
 * Render a metric tile.
 *
 * opts:
 *   label      — small uppercase label (required)
 *   value      — primary value, will be stringified
 *   unit       — optional unit suffix ("%", "ms", "s")
 *   delta      — optional secondary value (number or pre-formatted string)
 *   direction  — optional explicit arrow override: "up" | "down" | "flat".
 *                When set, the rendered delta text is shown verbatim
 *                (no sign parsing). Useful when the caller wants the
 *                delta-line to be a human-readable verdict like "worse"
 *                without a leading sign char.
 *   sentiment  — "good" | "bad" | "flat" | "neutral" — colours the delta
 *   accent     — when truthy, the tile picks up the accent color treatment
 *   size       — "sm" | "md" | "lg" — controls value font size
 *   onClick    — optional click handler — makes the tile a button
 */
export function renderMetricTile(opts) {
  const o = opts || {};
  const label = o.label || '';
  const value = o.value == null ? '—' : String(o.value);
  const unit = o.unit ? String(o.unit) : null;
  const delta = _fmtDelta(o.delta);
  const sentiment = o.sentiment ? String(o.sentiment) : null;
  const size = o.size || 'md';
  const accent = !!o.accent;

  const cls = ['tile', `tile-size-${size}`];
  if (accent) cls.push('tile-accent');
  if (o.onClick) cls.push('tile-clickable');

  const labelNode = el('div', { class: 'tile-label' }, [label]);
  const valueRow = el('div', { class: 'tile-value-row' }, [
    el('span', { class: 'tile-value' }, [value]),
    unit ? el('span', { class: 'tile-unit' }, [unit]) : null,
  ]);
  let deltaNode = null;
  if (delta != null) {
    const arrow = o.direction ? (ARROWS[o.direction] || ARROWS.flat) : _arrow(o.delta);
    const sentClass = sentiment ? SENTIMENT_CLASS[sentiment] : '';
    deltaNode = el('div', { class: 'tile-delta ' + (sentClass || '') }, [
      el('span', { class: 'tile-delta-arrow' }, [arrow]),
      el('span', { class: 'tile-delta-value' }, [delta]),
    ]);
  }

  const props = { class: cls.join(' ') };
  if (o.onClick) {
    props.role = 'button';
    props.tabindex = '0';
    props.onclick = o.onClick;
  }
  return el('div', props, [labelNode, valueRow, deltaNode]);
}

/**
 * Render a horizontal row of metric tiles. Convenience wrapper that
 * spreads N tiles across a flex strip.
 */
export function renderMetricStrip(tiles) {
  const items = Array.isArray(tiles) ? tiles : [];
  return el('div', { class: 'tile-strip' },
    items.map((t) => renderMetricTile(t)));
}
