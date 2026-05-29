// components/status_pill.js — the state-aware top-bar status pill.
//
// Single pill, four possible states:
//   CONNECTING — muted gray dot, pre-SSE hydration (state.connecting)
//   STALE      — amber dot, last heartbeat older than ~90s
//   RUNNING    — green dot, runs in flight (active_run_count > 0 OR
//                heartbeat carries an active generation_id)
//   IDLE       — neutral gray, supervisor alive but no active run
//
// Clicking the pill toggles a dropdown panel anchored just below the
// top bar (#phase0-status-dropdown) with current-run details + a small
// recent-decisions feed + an "Open current X" CTA.
//
// The pill itself is rendered fresh into a host on every relevant
// digest tick; the click toggle is wired against the pill's anchor so
// pill-state changes do not leak listeners. The dropdown is rendered by
// status_pill_dropdown.js when opened.

import { el } from '../core/dom.js';
import { parseIso, nowMs } from '../core/format.js';
import { state } from '../core/state.js';
import { renderStatusDropdown } from './status_pill_dropdown.js';

// Stale threshold matches the legacy header bar — heartbeat.json is
// rewritten on a short cadence, so 90s leaves slack for a slow tick.
export const STALE_HEARTBEAT_MS = 90_000;

// Resolve the canonical state of the supervisor + active tournament
// from app state. Exported so tests can assert the state transitions
// independently of the rendered DOM.
export function resolveStatusState() {
  if (state.connecting) return 'connecting';
  const hb = state.heartbeat || {};
  // Stale check first — if the heartbeat is old, the green dot lies.
  const lastHbMs = parseIso(hb.last_heartbeat != null ? hb.last_heartbeat : hb.timestamp);
  if (isFinite(lastHbMs) && (nowMs() - lastHbMs) > STALE_HEARTBEAT_MS) {
    return 'stale';
  }
  const activeCount = Array.isArray(state.activeRuns) ? state.activeRuns.length : 0;
  // RUNNING when the heartbeat carries an active generation AND at
  // least one run / tournament is in flight. A bare epoch_id without
  // any runs is still IDLE — the supervisor is alive but quiescent.
  if (activeCount > 0 || state.activeTournament) return 'running';
  return 'idle';
}

// Compute the visible label for the pill. ``running`` carries the
// generation id ("v8") when the heartbeat surfaces it.
export function statusPillLabel(stateName) {
  switch (stateName) {
    case 'connecting': return 'CONNECTING';
    case 'stale': return 'STALE';
    case 'running': {
      const gen = state.heartbeat && state.heartbeat.generation_id;
      return gen ? 'RUNNING ' + gen : 'RUNNING';
    }
    case 'idle': return 'IDLE';
    default: return 'IDLE';
  }
}

// Render the pill into a fresh node. The caller mounts the result into
// its slot; clicking the pill flips the dropdown's visibility.
export function renderStatusPill() {
  const stateName = resolveStatusState();
  const label = statusPillLabel(stateName);
  const cls = 'phase0-status-pill phase0-status-pill-' + stateName;
  const node = el('button', {
    type: 'button',
    class: cls,
    'data-state': stateName,
    'aria-label': 'Status: ' + label + ' — click to open detail',
    'aria-haspopup': 'dialog',
    'aria-expanded': 'false',
    onClick: (ev) => {
      if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
      toggleStatusDropdown(node);
    },
  }, [
    el('span', { class: 'phase0-status-pill-dot', 'aria-hidden': 'true' }),
    el('span', { class: 'phase0-status-pill-label' }, [label]),
    el('span', { class: 'phase0-status-pill-chevron', 'aria-hidden': 'true' }, ['›']),
  ]);
  return node;
}

// Toggle the dropdown panel anchored below the top bar. Re-rendered on
// every open so the contents are fresh; the click-outside listener is
// re-wired idempotently.
let _dropdownOpenFor = null;

export function toggleStatusDropdown(anchorNode) {
  const panel = document.getElementById('phase0-status-dropdown');
  if (!panel) return;
  const wasOpen = !panel.hasAttribute('hidden');
  if (wasOpen) {
    closeStatusDropdown();
    return;
  }
  // Open: populate the panel via the dropdown renderer and reveal.
  panel.textContent = '';
  panel.appendChild(renderStatusDropdown());
  panel.removeAttribute('hidden');
  if (anchorNode && typeof anchorNode.setAttribute === 'function') {
    anchorNode.setAttribute('aria-expanded', 'true');
    _dropdownOpenFor = anchorNode;
  }
  // Click-outside to close. Bound on the document; removed on close.
  _installClickAway();
}

export function closeStatusDropdown() {
  const panel = document.getElementById('phase0-status-dropdown');
  if (!panel) return;
  panel.setAttribute('hidden', '');
  panel.textContent = '';
  if (_dropdownOpenFor && typeof _dropdownOpenFor.setAttribute === 'function') {
    _dropdownOpenFor.setAttribute('aria-expanded', 'false');
  }
  _dropdownOpenFor = null;
  _removeClickAway();
}

let _clickAwayHandler = null;
function _installClickAway() {
  if (_clickAwayHandler) return;
  _clickAwayHandler = (ev) => {
    const panel = document.getElementById('phase0-status-dropdown');
    if (!panel || panel.hasAttribute('hidden')) return;
    const t = ev && ev.target;
    if (!t) return;
    // Walk up the click target's ancestry; if neither the panel nor the
    // pill anchor are on the path, treat as click-away.
    let n = t;
    while (n) {
      if (n === panel) return;
      if (n === _dropdownOpenFor) return;
      n = n.parentNode;
    }
    closeStatusDropdown();
  };
  if (typeof document !== 'undefined'
      && typeof document.addEventListener === 'function') {
    document.addEventListener('click', _clickAwayHandler);
  }
}

function _removeClickAway() {
  if (!_clickAwayHandler) return;
  if (typeof document !== 'undefined'
      && typeof document.removeEventListener === 'function') {
    document.removeEventListener('click', _clickAwayHandler);
  }
  _clickAwayHandler = null;
}

// Reset internal module state. Used by tests so a fresh case does not
// inherit a stale click-away handler.
export function _resetStatusPill() {
  _dropdownOpenFor = null;
  _removeClickAway();
}
