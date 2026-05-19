// core/harmonograf.js — harmonograf deep-link builders.
//
// The heartbeat MAY carry a non-empty `harmonograf_url`. When present,
// every run on the dashboard deep-links into harmonograf at the run's
// execution trace. When the heartbeat carries no url at all, render
// nothing — no disabled stub.
//
// harmonograf keys its session views by the ADK session id carried on
// every goldfive event envelope (`sessionId`). The backend surfaces
// this as `adk_session_id` on run-like records (active-run rows,
// ab-grid cells). Resolution order, most-specific first:
//   1. `adk_session_id` — the real ADK/goldfive session id (preferred).
//   2. `session_id` / `session` / `harmonograf_session` — legacy aliases.
//   3. the bare harmonograf url (last resort — never render nothing
//      when harmonograf_url is set).
//
// The harmonograf route is `/#/session/<adk_session_id>`. No harmonograf-
// side change is needed — the integration is complete once the correct
// ADK session id is used.

import { el } from './dom.js';
import { state } from './state.js';

export function harmonografBase() {
  const url = state.heartbeat && state.heartbeat.harmonograf_url;
  if (typeof url !== 'string') return null;
  const trimmed = url.trim();
  return trimmed.length > 0 ? trimmed.replace(/\/+$/, '') : null;
}

// Derive the zicato synthetic `{generation_id}--{entry_id}` run-id
// convention. Kept for callers that need the run-id string directly;
// no longer used for harmonograf session resolution (the backend now
// surfaces the real ADK session id as `adk_session_id`).
export function deriveRunId(rec) {
  if (!rec) return null;
  const gen = rec.generation_id || rec.generation || rec.child_id;
  const entry = rec.entry_id || rec.entry;
  if (gen && entry) return `${gen}--${entry}`;
  return null;
}

// Resolve a harmonograf session id from a run-like record.
// Prefers the real ADK session id surfaced by the backend.
export function harmonografSessionId(rec) {
  if (!rec) return null;
  // adk_session_id is the canonical ADK/goldfive session id.
  const adk = rec.adk_session_id || rec.child_adk_session_id || rec.parent_adk_session_id;
  if (adk && String(adk).trim()) return String(adk).trim();
  // Legacy aliases — kept for back-compat with older backend responses.
  const legacy = rec.session_id || rec.session || rec.harmonograf_session;
  if (legacy) return String(legacy);
  return null;
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
