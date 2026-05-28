// components/card.js — the container primitive.
//
// A card is the dashboard's unit of visual grouping: a titled box with
// a subtle border, soft shadow, optional accent border-top, and a body
// that holds whatever the section paints. Every level uses cards so the
// dashboard reads as one coherent grid rather than a stack of stray
// tables.

import { el } from '../core/dom.js';

const ACCENT_CLASS = {
  default: 'card-accent-default',
  accent:  'card-accent-indigo',
  success: 'card-accent-success',
  warning: 'card-accent-warning',
  error:   'card-accent-error',
  promoted: 'card-accent-success',
  rejected: 'card-accent-error',
};

/**
 * Render a card container.
 *
 * opts:
 *   title    — string or node — appears at the top of the card
 *   subtitle — optional muted line beneath the title
 *   body     — node or array of nodes — the card's content
 *   footer   — optional footer node
 *   accent   — "default" | "accent" | "success" | "warning" | "error" |
 *              "promoted" | "rejected" — paints a 2px top border in the
 *              matching color
 *   variant  — "default" | "elevated" | "flush" — controls padding/shadow
 *   actions  — optional node (e.g. a button row) painted to the right of
 *              the title in the header
 *   id       — optional dom id
 *   class    — optional extra class string
 */
export function renderCard(opts) {
  const o = opts || {};
  const accent = o.accent ? (ACCENT_CLASS[o.accent] || ACCENT_CLASS.default)
                          : ACCENT_CLASS.default;
  const variant = o.variant || 'default';
  const extra = o.class ? ' ' + o.class : '';
  const cls = `card card-${variant} ${accent}${extra}`;

  const headerChildren = [];
  if (o.title) {
    const titleNode = (typeof o.title === 'string')
      ? el('h3', { class: 'card-title' }, [o.title])
      : o.title;
    headerChildren.push(titleNode);
  }
  if (o.actions) {
    headerChildren.push(el('div', { class: 'card-actions' },
      Array.isArray(o.actions) ? o.actions : [o.actions]));
  }
  const header = headerChildren.length
    ? el('div', { class: 'card-header' }, headerChildren)
    : null;
  const subtitle = o.subtitle
    ? el('p', { class: 'card-subtitle' }, [o.subtitle])
    : null;

  const bodyChildren = Array.isArray(o.body) ? o.body : (o.body ? [o.body] : []);
  const body = el('div', { class: 'card-body' }, bodyChildren);

  const footer = o.footer
    ? el('div', { class: 'card-footer' },
        Array.isArray(o.footer) ? o.footer : [o.footer])
    : null;

  const props = { class: cls };
  if (o.id) props.id = o.id;
  return el('div', props, [header, subtitle, body, footer]);
}

/**
 * Render a card with a left-rail accent — like a call-out box. Used for
 * the goal callout on L1 where the accent is on the left edge rather
 * than the top, signalling "here is the framing".
 */
export function renderCalloutCard(opts) {
  const o = opts || {};
  const accent = o.accent || 'accent';
  const cls = `card card-callout card-callout-${accent}`;
  const titleNode = o.title
    ? el('div', { class: 'card-callout-label' }, [o.title])
    : null;
  const bodyChildren = Array.isArray(o.body) ? o.body : (o.body ? [o.body] : []);
  const body = el('div', { class: 'card-callout-body' }, bodyChildren);
  return el('div', { class: cls }, [titleNode, body]);
}
