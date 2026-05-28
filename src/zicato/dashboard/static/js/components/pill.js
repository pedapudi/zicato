// components/pill.js — status / verdict pill.
//
// A pill is a small text-on-color affordance for status. We use it
// everywhere a single word answers "what happened": pass/fail per
// expectation, promoted/rejected per generation, live/stale on the
// header.
//
// Variants:
//   success | error | warning | info | neutral
//   promoted (= success) | rejected (= error) | deferred (= warning)
//   live (animated dot + label) | stale (gray dot + label)

import { el } from '../core/dom.js';

const VARIANT_CLASS = {
  success:  'pill-success',
  error:    'pill-error',
  warning:  'pill-warning',
  info:     'pill-info',
  neutral:  'pill-neutral',
  promoted: 'pill-success',
  rejected: 'pill-error',
  deferred: 'pill-warning',
  pass:     'pill-success',
  fail:     'pill-error',
  live:     'pill-live',
  stale:    'pill-stale',
  pending:  'pill-info',
  running:  'pill-info',
  done:     'pill-success',
};

const DOT_VARIANTS = new Set(['live', 'stale']);

/**
 * Render a pill.
 *
 * text    — string label
 * variant — one of VARIANT_CLASS keys. Unknown variants fall back to neutral.
 */
export function renderPill(text, variant) {
  const key = (variant || 'neutral').toLowerCase();
  const cls = VARIANT_CLASS[key] || VARIANT_CLASS.neutral;
  const wantDot = DOT_VARIANTS.has(key);
  const dot = wantDot
    ? el('span', { class: 'pill-dot pill-dot-' + key, 'aria-hidden': 'true' })
    : null;
  return el('span', { class: 'pill ' + cls }, [dot, String(text || '')]);
}

/**
 * Render a small inline status pill — used in dense tables where the
 * regular pill would be too large.
 */
export function renderInlinePill(text, variant) {
  const key = (variant || 'neutral').toLowerCase();
  const cls = VARIANT_CLASS[key] || VARIANT_CLASS.neutral;
  return el('span', { class: 'pill pill-sm ' + cls }, [String(text || '')]);
}

/**
 * Render an event-type chip — same shape as a pill but with a colored
 * left rail. Used on the L4 event stream.
 *
 * Maps common event kinds onto sentiment automatically:
 *   drift_detected         → error
 *   plan_revised           → warning
 *   judgement_emitted      → info
 *   run_started/completed  → neutral / success
 */
export function renderEventChip(kind) {
  const k = String(kind || '').toLowerCase();
  const map = {
    drift_detected:    'error',
    drift_event:       'error',
    plan_revised:      'warning',
    plan_revision:     'warning',
    judgement_emitted: 'info',
    judgement:         'info',
    judge:             'info',
    steering_event:    'warning',
    run_started:       'info',
    run_completed:     'success',
    run_failed:        'error',
    error:             'error',
    warn:              'warning',
    ok:                'success',
  };
  const variant = map[k] || 'neutral';
  const cls = VARIANT_CLASS[variant] || VARIANT_CLASS.neutral;
  return el('span', { class: 'event-chip ' + cls }, [k || 'event']);
}
