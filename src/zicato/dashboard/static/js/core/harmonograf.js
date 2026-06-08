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
import { bus } from './bus.js';

// ── UI-PRESENCE PROBE (dead-link gate) ────────────────────────────────
//
// THE BUG this gate fixes: the installed harmonograf-server serves NO
// browser UI — only gRPC-Web + a /healthz endpoint. So a deep-link built
// from `harmonograf_url` (e.g. `<url>/#/session/<id>`) 404s: there is no SPA
// to route the hash. Liveness alone is NOT enough — a server can be very much
// alive and still serve no browser UI.
//
// The fix: before rendering ANY harmonograf deep-link, probe the web port for
// a REAL browser UI — a GET that returns an HTML document, not merely a 200
// from /healthz. With today's no-UI server the probe fails, so every link is
// HIDDEN (no dead link). When a real harmonograf SPA is served later the probe
// succeeds and the links reappear — no code change needed.
//
// The probe is async + cached per-base-url; the sync link builders read the
// cached verdict, so they stay synchronous. A base whose probe has not
// resolved reads as "no UI yet" (the safe default: hide the link rather than
// render a maybe-dead one). When the probe resolves it fires `state:changed`
// so the chrome re-renders and a real link appears.

// Per-base probe cache: base-url → 'pending' | true | false. `true` means a
// browser UI was confirmed; `false` means the probe ran and found none.
const _uiProbe = new Map();

// Reset the probe cache — for tests + a workspace switch.
export function _resetHarmonografUiProbe() {
  _uiProbe.clear();
}

// Test/debug seam: seed a probe verdict synchronously for a base url (with or
// without a trailing slash — normalised to the cache key form). Lets a test
// assert the link-gating logic without an async fetch round-trip.
export function _seedHarmonografUiProbe(baseUrl, ok) {
  if (baseUrl == null) return;
  const base = String(baseUrl).trim().replace(/\/+$/, '');
  if (base !== '') _uiProbe.set(base, !!ok);
}

// Did the probe confirm an HTML response (a real browser UI)? Heuristic:
// a 2xx response whose Content-Type is text/html. A 404 / a JSON-or-text
// /healthz body / a network error all read as "no UI".
async function _probeUi(base) {
  try {
    // GET (not HEAD) — some servers answer HEAD differently than GET, and we
    // need the Content-Type. `redirect: 'follow'` so a `/` → `/index.html`
    // SPA root still resolves. Same-origin by default for the bound localhost
    // server; CORS is not in play for the auto-launched workspace server.
    const resp = await fetch(base + '/', { method: 'GET', redirect: 'follow' });
    if (!resp || !resp.ok) return false;
    const ctype = (resp.headers && resp.headers.get && resp.headers.get('content-type')) || '';
    return /text\/html/i.test(String(ctype));
  } catch (e) {
    return false;
  }
}

// Kick a probe for `base` if one is not already cached / in flight. On
// resolution, cache the verdict and (when it changes the answer) fire
// `state:changed` so any chrome reading harmonografUiAvailable() re-renders.
function _ensureProbe(base) {
  if (base == null || base === '') return;
  if (_uiProbe.has(base)) return;
  _uiProbe.set(base, 'pending');
  _probeUi(base).then((ok) => {
    _uiProbe.set(base, !!ok);
    // Repaint so a now-available (or now-confirmed-absent) link updates.
    try { bus.emit('state:changed'); } catch (e) { /* best-effort */ }
  });
}

// Is a REAL harmonograf browser UI available for deep-links at the current
// live base? Synchronous: reads the probe cache. Triggers the probe lazily
// the first time it is asked about a base. Returns false until a probe has
// CONFIRMED an HTML UI — so a no-UI server (today's install) hides the link.
export function harmonografUiAvailable() {
  if (!harmonografIsLive()) return false;
  const url = state.heartbeat && state.heartbeat.harmonograf_url;
  if (typeof url !== 'string') return false;
  const base = url.trim().replace(/\/+$/, '');
  if (base === '') return false;
  const verdict = _uiProbe.get(base);
  if (verdict === undefined) { _ensureProbe(base); return false; }
  if (verdict === 'pending') return false;
  return verdict === true;
}

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
  // Gate on a REAL browser UI being served: the install may answer /healthz
  // but serve NO SPA, in which case every deep-link 404s. Only resolve a base
  // once the UI probe has confirmed an HTML document (until then, and for a
  // no-UI server permanently, no link renders — no dead link). See
  // harmonografUiAvailable() / the UI-probe block above.
  if (!harmonografUiAvailable()) return null;
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

// ── ZICATO-LEVEL (meta-loop) surface ──────────────────────────────────
// harmonograf "at the zicato level": the orchestrator's own proposer +
// process-judge timeline, bucketed under ONE stable session id per evolve
// (`meta_loop_session_id`). The backend surfaces that id on the heartbeat
// as `harmonograf_meta_session` (live evolve writes it; the standalone
// dashboard recovers it off `meta_loop_events.jsonl` for post-mortem).
// See docs/design/HARMONOGRAF.md §2b/§3b.

// The meta-loop session id off the (merged) heartbeat, or null.
export function harmonografMetaSession() {
  const hb = state.heartbeat;
  if (!hb) return null;
  const sid = hb.harmonograf_meta_session;
  if (typeof sid !== 'string') return null;
  const trimmed = sid.trim();
  return trimmed.length > 0 ? trimmed : null;
}

// The deep-link URL for the zicato-level execution (meta-loop) session.
// Liveness-gated like every other builder; null when no server is live or
// no meta-loop session id is known.
export function harmonografMetaUrl() {
  const base = harmonografBase();
  if (!base) return null;
  const sid = harmonografMetaSession();
  if (!sid) return null;
  return `${base}/#/session/${encodeURIComponent(sid)}`;
}

// The top-bar "execution ▸" link into the zicato-level meta-loop session.
// Returns null (renders nothing) when the surface isn't available — the
// caller MUST tolerate null so the top bar stays clean on a degraded /
// pre-run workspace.
export function harmonografMetaLink(label, ariaLabel) {
  const href = harmonografMetaUrl();
  if (!href) return null;
  return el('a', {
    class: 'harmonograf-link harmonograf-meta', href,
    target: '_blank', rel: 'noopener',
    'aria-label': ariaLabel || 'open the zicato execution timeline in harmonograf',
  }, [(label || 'execution') + ' ↗']);
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
