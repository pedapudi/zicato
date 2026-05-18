// core/sse.js — EventSource wiring + typed delta dispatch.
//
// On connect the server sends a `snapshot`; thereafter it sends
// coalesced `state_change` deltas and `run_log` deltas.
//
//   * snapshot       → state.applySnapshot()
//   * state_change   → debounced ONE /api/environment fetch
//   * run_log        → append-only /api/run-log?after=<cursor> poll
//   * heartbeat      → state.setHeartbeat() (merge)
//   * : ping         → keepalive, ignored
//
// A burst of state_change frames collapses into AT MOST ONE environment
// fetch per REFRESH_DEBOUNCE_MS — the dashboard never fans a file
// change out into a wave of per-endpoint polls. Because applyEnvironment
// only mutates state and the render layer patches keyed nodes, a delta
// never rebuilds a panel's innerHTML: no flash.

import { state } from './state.js';
import { loadEnvironment, loadMatchupDetail, pollLogTailAppend } from './api.js';

const REFRESH_DEBOUNCE_MS = 400;
const SSE_BACKOFF_MAX_MS = 30_000;

let _sse = null;
let _retry = 0;
let _refreshTimer = null;
let _refreshPending = false;

// Debounced coalesced environment refresh. The frame's `kinds` is
// advisory only — the single consolidated read refreshes the whole view.
function refreshAfterEvent() {
  _refreshPending = true;
  if (_refreshTimer != null) return;
  _refreshTimer = setTimeout(async () => {
    _refreshTimer = null;
    if (!_refreshPending) return;
    _refreshPending = false;
    try {
      await loadEnvironment();
      if (state.selectedMatchup) loadMatchupDetail(state.selectedMatchup);
    } catch (err) {
      console.warn('refresh failed:', err);
    }
  }, REFRESH_DEBOUNCE_MS);
}

function scheduleReconnect() {
  if (_sse) { _sse.close(); _sse = null; }
  _retry += 1;
  const delay = Math.min(SSE_BACKOFF_MAX_MS, 500 * Math.pow(2, Math.min(_retry, 6)));
  setTimeout(connectSSE, delay);
}

export function connectSSE() {
  state.connecting = true;
  state._changed();
  try {
    _sse = new EventSource('/events');
  } catch (err) {
    scheduleReconnect();
    return;
  }
  _sse.addEventListener('open', () => {
    state.connected = true;
    state.connecting = false;
    _retry = 0;
    state._changed();
  });
  _sse.addEventListener('snapshot', (ev) => {
    try { state.applySnapshot(JSON.parse(ev.data)); }
    catch (err) { console.warn('bad snapshot event:', err); }
  });
  _sse.addEventListener('state_change', () => {
    refreshAfterEvent();
  });
  _sse.addEventListener('run_log', () => {
    pollLogTailAppend();
  });
  _sse.addEventListener('heartbeat', (ev) => {
    try { state.setHeartbeat(JSON.parse(ev.data)); state._changed(); }
    catch { /* ignore */ }
  });
  _sse.addEventListener('error', () => {
    state.connected = false;
    state._changed();
    if (_sse && _sse.readyState === EventSource.CLOSED) scheduleReconnect();
  });
}
