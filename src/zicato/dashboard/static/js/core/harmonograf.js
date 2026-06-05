// core/harmonograf.js — harmonograf deep-link builders.
//
// LIVENESS GATE (DASHBOARD-V2 bug fix). zicato auto-launches its own
// harmonograf server bound to the run; that server DIES when the run
// ends. The heartbeat's `harmonograf_url`, however, lingers (the
// heartbeat is merged, not replaced — see state.setHeartbeat), so a link
// built from it after a run completes points at a dead port. The fix:
// only ever resolve a harmonograf base while a run is actually LIVE.
// Liveness is read from the live-run signals the client already tracks —
// an active tournament or any active runs — NOT from the (stale)
// presence of the url. When nothing is live, every builder returns null,
// so a call site renders nothing (or its own muted "available during
// live runs" note via `harmonografLiveNote`).
//
// The heartbeat MAY carry a non-empty `harmonograf_url`. When present
// AND a run is live, every run on the dashboard deep-links into
// harmonograf at the run's execution trace. When the heartbeat carries
// no url at all, or no run is live, render nothing — no disabled stub.
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

// Is a harmonograf server currently reachable for deep-links?
//
// Two distinct sources of a LIVE server:
//   1. An evolve-launched server, which exists ONLY while the loop runs
//      and DIES with it — so it is "live" iff a run is actually in flight
//      (an active tournament OR at least one active run). A stale
//      `harmonograf_url` lingering on the merged heartbeat is NOT proof of
//      life here — that was the original bug this gate fixes.
//   2. A persistent per-workspace server the STANDALONE dashboard reused-
//      or-launched (`ensure_workspace_harmonograf`). It does NOT die with a
//      run — it lives for the dashboard process — so a post-mortem
//      dashboard with no active runs is still "live" for deep-link
//      purposes. The backend signals this with `harmonograf_persistent` on
//      the (dashboard-injected) heartbeat.
export function harmonografIsLive() {
  if (state.activeTournament != null) return true;
  if (Array.isArray(state.activeRuns) && state.activeRuns.length > 0) return true;
  if (state.heartbeat && state.heartbeat.harmonograf_persistent === true) return true;
  return false;
}

export function harmonografBase() {
  // Gate on liveness FIRST: a dead run's server is gone, so no link is
  // ever valid then — regardless of a lingering heartbeat url.
  if (!harmonografIsLive()) return null;
  const url = state.heartbeat && state.heartbeat.harmonograf_url;
  if (typeof url !== 'string') return null;
  const trimmed = url.trim();
  return trimmed.length > 0 ? trimmed.replace(/\/+$/, '') : null;
}

// A muted, non-link note for call sites that want to explain WHY the
// harmonograf link is absent post-run, rather than rendering nothing.
// Returns null while live (the real link should render instead).
export function harmonografLiveNote(text) {
  if (harmonografIsLive()) return null;
  return el('span', {
    class: 'harmonograf-note', 'aria-label': 'harmonograf available during live runs',
  }, [text || 'harmonograf available during live runs']);
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
