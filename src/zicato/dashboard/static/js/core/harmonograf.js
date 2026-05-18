// core/harmonograf.js — harmonograf deep-link builders.
//
// The heartbeat MAY carry a non-empty `harmonograf_url`. When present,
// every run on the dashboard deep-links into harmonograf at the run's
// execution trace. When the heartbeat carries no url at all, render
// nothing — no disabled stub.
//
// A harmonograf *session* is a run. zicato names a run deterministically
// as `{generation_id}--{entry_id}`, so even a record with no explicit
// session id resolves a trace. Resolution order, most-specific first:
//   1. explicit session id (session_id / session / harmonograf_session / run_id)
//   2. the `{generation}--{entry}` run-id convention
//   3. the bare harmonograf url (last resort — never render nothing
//      when harmonograf_url is set).
//
// NOTED CONTRACT GAP: the run-id ↔ harmonograf-route integration is
// one-sided. The dashboard builds `/#/session/<id>`; harmonograf must
// accept that id form. Flagged for the harmonograf side.

import { el } from './dom.js';
import { state } from './state.js';

export function harmonografBase() {
  const url = state.heartbeat && state.heartbeat.harmonograf_url;
  if (typeof url !== 'string') return null;
  const trimmed = url.trim();
  return trimmed.length > 0 ? trimmed.replace(/\/+$/, '') : null;
}

// Derive the deterministic `{generation_id}--{entry_id}` run id.
export function deriveRunId(rec) {
  if (!rec) return null;
  const gen = rec.generation_id || rec.generation || rec.child_id;
  const entry = rec.entry_id || rec.entry;
  if (gen && entry) return `${gen}--${entry}`;
  return null;
}

// Resolve a harmonograf session id — explicit, else the run-id form.
export function harmonografSessionId(rec) {
  if (!rec) return null;
  const explicit = rec.session_id || rec.session
    || rec.harmonograf_session || rec.run_id;
  if (explicit) return String(explicit);
  return deriveRunId(rec);
}

// Build the harmonograf URL for a run-like record. Falls back to the
// bare base; returns null only when no harmonograf_url exists at all.
export function harmonografRunUrl(rec) {
  const base = harmonografBase();
  if (!base) return null;
  const sid = harmonografSessionId(rec);
  if (sid) return `${base}/#/session/${encodeURIComponent(sid)}`;
  return base;
}

// The full "Open in harmonograf ↗" link — active-run cards, run drill.
export function harmonografLink(run, label) {
  const href = harmonografRunUrl(run);
  if (!href) return null;
  return el('a', {
    class: 'harmonograf-link', href, target: '_blank', rel: 'noopener',
  }, [(label || 'Open in harmonograf') + ' ↗']);
}

// A small unobtrusive link for dense contexts — A/B-grid cells.
export function harmonografMini(target, label, ariaLabel) {
  const href = harmonografRunUrl(target);
  if (!href) return null;
  return el('a', {
    class: 'harmonograf-link harmonograf-mini', href,
    target: '_blank', rel: 'noopener',
    'aria-label': ariaLabel || 'open harmonograf trace',
  }, [(label || 'harmonograf') + ' ↗']);
}

// A subtle superscript link for a bracket / tournament generation node.
// harmonograf has no per-generation filter URL, so this lands on the
// bare url, scoped by the generation id in its aria-label.
export function harmonografGenLink(genId) {
  const base = harmonografBase();
  if (!base) return null;
  return el('a', {
    class: 'harmonograf-link harmonograf-sup', href: base,
    target: '_blank', rel: 'noopener',
    'aria-label': 'open harmonograf traces for generation ' + (genId || '?'),
    onClick: (ev) => ev.stopPropagation(),
    onKeydown: (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') ev.stopPropagation();
    },
  }, ['↗']);
}
