// Harmonograf navigation. Links require a live/persistent server whose root
// serves HTML; session ids and namespaced filter labels come from the backend.

import { el } from './dom.js';
import { state } from './state.js';
import { bus } from './bus.js';

const _uiProbe = new Map();

export function _resetHarmonografUiProbe() {
  _uiProbe.clear();
}

export function _seedHarmonografUiProbe(baseUrl, ok) {
  if (baseUrl == null) return;
  const base = String(baseUrl).trim().replace(/\/+$/, '');
  if (base !== '') _uiProbe.set(base, !!ok);
}

async function _probeUi(base) {
  try {
    const resp = await fetch(base + '/', { method: 'GET', redirect: 'follow' });
    if (!resp || !resp.ok) return false;
    const ctype = (resp.headers && resp.headers.get && resp.headers.get('content-type')) || '';
    return /text\/html/i.test(String(ctype));
  } catch (e) {
    return false;
  }
}

function _ensureProbe(base) {
  if (base == null || base === '') return;
  if (_uiProbe.has(base)) return;
  _uiProbe.set(base, 'pending');
  _probeUi(base).then((ok) => {
    _uiProbe.set(base, !!ok);
    try { bus.emit('state:changed'); } catch (e) { /* best-effort */ }
  });
}

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

export function harmonografIsLive() {
  if (state.activeTournament != null) return true;
  if (Array.isArray(state.activeRuns) && state.activeRuns.length > 0) return true;
  if (state.heartbeat && state.heartbeat.harmonograf_persistent === true) return true;
  return false;
}

export function harmonografBase() {
  if (!harmonografIsLive()) return null;
  if (!harmonografUiAvailable()) return null;
  const url = state.heartbeat && state.heartbeat.harmonograf_url;
  if (typeof url !== 'string') return null;
  const trimmed = url.trim();
  return trimmed.length > 0 ? trimmed.replace(/\/+$/, '') : null;
}

export function harmonografSessionId(rec) {
  if (!rec) return null;
  const adk = rec.adk_session_id || rec.child_adk_session_id || rec.parent_adk_session_id;
  if (adk && String(adk).trim()) return String(adk).trim();
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

// Open Harmonograf's ordinary session picker with exact metadata predicates.
// Harmonograf treats the keys as opaque; zicato owns their vocabulary.
export function harmonografFilterUrl(metadata) {
  const base = harmonografBase();
  if (!base || !metadata || typeof metadata !== 'object') return null;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(metadata)) {
    if (key && value != null && String(value) !== '') {
      query.set(`metadata.${key}`, String(value));
    }
  }
  const encoded = query.toString();
  return encoded ? `${base}/#/sessions?${encoded}` : null;
}

export function harmonografTournamentLink(tournamentId) {
  const href = harmonografFilterUrl({ 'zicato.tournament_id': tournamentId });
  if (!href) return null;
  return el('a', {
    class: 'harmonograf-link', href, target: '_blank', rel: 'noopener',
  }, ['Open tournament traces ↗']);
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

export function harmonografMetaSession() {
  const hb = state.heartbeat;
  if (!hb) return null;
  const sid = hb.harmonograf_meta_session;
  if (typeof sid !== 'string') return null;
  const trimmed = sid.trim();
  return trimmed.length > 0 ? trimmed : null;
}

export function harmonografMetaUrl() {
  const base = harmonografBase();
  if (!base) return null;
  const sid = harmonografMetaSession();
  if (!sid) return null;
  return `${base}/#/session/${encodeURIComponent(sid)}`;
}

export function harmonografMetaLink(label, ariaLabel) {
  const href = harmonografMetaUrl();
  if (!href) return null;
  return el('a', {
    class: 'harmonograf-link harmonograf-meta', href,
    target: '_blank', rel: 'noopener',
    'aria-label': ariaLabel || 'open the zicato execution timeline in harmonograf',
  }, [(label || 'execution') + ' ↗']);
}

export function harmonografGenLink(genId) {
  const href = harmonografFilterUrl({ 'zicato.generation_id': genId });
  if (!href) return null;
  return el('a', {
    class: 'harmonograf-link harmonograf-sup', href,
    target: '_blank', rel: 'noopener',
    'aria-label': 'open harmonograf traces for generation ' + (genId || '?'),
    onClick: (ev) => ev.stopPropagation(),
    onKeydown: (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') ev.stopPropagation();
    },
  }, ['↗']);
}
