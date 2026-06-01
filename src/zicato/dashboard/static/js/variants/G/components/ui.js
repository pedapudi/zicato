// variants/G/components/ui.js — the Bridge visual vocabulary.
//
// Calm command-center chrome: panels, readout strips, status chips,
// loading / empty states. This is A's structure (a panel with a head +
// body, a readout strip) rebuilt in B/D's quieter typographic language
// (hairline rules, generous whitespace, the Solarized-Dark token set)
// rather than A's heavy live-ops console look. Pure builders:
// (data) -> DOM node, no state, no fetch.

import { el } from '../../../core/dom.js';

// Append a body (node | string | array of either) into a host, coercing
// bare strings to text nodes the way `el`'s children do — a panel/drawer
// body is often a plain string.
function appendBody(host, body) {
  const one = (c) => {
    if (c == null) return;
    if (typeof c === 'string') host.appendChild(document.createTextNode(c));
    else host.appendChild(c);
  };
  if (Array.isArray(body)) for (const c of body) one(c);
  else one(body);
}

// -- panel frame ------------------------------------------------------
export function panel({ title, sub, accent, actions, body }) {
  const head = el('div', { class: 'g-panel-head' }, [
    el('div', { class: 'g-panel-titles' }, [
      el('span', { class: 'g-panel-title' }, [title || '']),
      sub ? el('span', { class: 'g-panel-sub' }, [sub]) : null,
    ]),
    actions ? el('div', { class: 'g-panel-actions' },
      Array.isArray(actions) ? actions : [actions]) : null,
  ]);
  const p = el('div', { class: 'g-panel', dataset: accent ? { accent } : {} }, [head]);
  const b = el('div', { class: 'g-panel-body' });
  appendBody(b, body);
  p.appendChild(b);
  return p;
}

// -- readout strip ----------------------------------------------------
export function readout({ label, value, tone, foot }) {
  return el('div', { class: 'g-readout' }, [
    el('div', { class: 'g-readout-label' }, [label || '']),
    el('div', { class: 'g-readout-value' + (tone ? ' is-' + tone : '') },
      [value == null ? '—' : String(value)]),
    foot ? el('div', { class: 'g-readout-foot' }, [foot]) : null,
  ]);
}

export function readouts(items) {
  return el('div', { class: 'g-readouts' }, items.map(readout));
}

// -- status chip ------------------------------------------------------
export function chip(label, kind) {
  return el('span', { class: 'g-chip', dataset: { kind: kind || 'idle' } }, [label]);
}

// -- empty / loading --------------------------------------------------
export function empty(msg) { return el('div', { class: 'g-empty' }, [msg || 'No data.']); }
export function loading(msg) { return el('div', { class: 'g-loading' }, [msg || 'Loading…']); }

// -- progress bar -----------------------------------------------------
export function bar(frac, tone) {
  const f = Math.max(0, Math.min(1, typeof frac === 'number' ? frac : 0));
  const fill = el('div', { class: 'g-bar-fill' + (tone ? ' is-' + tone : ''), style: `width:${(f * 100).toFixed(1)}%` });
  return el('div', { class: 'g-bar' }, [fill]);
}

// -- a pass / fail / no-predicate lamp --------------------------------
export function passLamp(v, timeout) {
  if (timeout) return chip('timeout', 'caution');
  if (v === 1 || v === true) return chip('pass', 'improve');
  if (v === 0 || v === false) return chip('fail', 'regress');
  return chip('no predicate', 'idle');
}

// -- collapsible drawer (brief, diff) ---------------------------------
// Self-managing: the toggle flips a `hidden` attr on the body and the
// label text in place — no parent re-render, so it never fights the
// digest-gated repaint.
export function drawer({ title, sub, openByDefault, body }) {
  let open = !!openByDefault;
  const toggle = el('span', { class: 'g-drawer-toggle' }, [open ? 'collapse ∧' : 'expand ∨']);
  const head = el('div', { class: 'g-drawer-head' }, [
    el('div', { class: 'g-panel-titles' }, [
      el('span', { class: 'g-panel-title' }, [title || '']),
      sub ? el('span', { class: 'g-panel-sub' }, [sub]) : null,
    ]),
    toggle,
  ]);
  const bodyHost = el('div', { class: 'g-drawer-body' });
  if (!open) bodyHost.setAttribute('hidden', 'true');
  appendBody(bodyHost, body);
  head.addEventListener('click', () => {
    open = !open;
    if (open) bodyHost.removeAttribute('hidden'); else bodyHost.setAttribute('hidden', 'true');
    toggle.textContent = open ? 'collapse ∧' : 'expand ∨';
  });
  return el('div', { class: 'g-drawer' }, [head, bodyHost]);
}

export function fmt(v, d = 3) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : '—'; }
export function signed(v, d = 3) { return (typeof v === 'number' && isFinite(v)) ? (v > 0 ? '+' : '') + v.toFixed(d) : '—'; }
