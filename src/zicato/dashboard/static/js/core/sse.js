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
//
// THE SEQ NO-OP-SKIP GATE (RUNTIME-V2 Phase 4). Every `state_change` /
// `snapshot` frame carries a top-level progress `seq` + `terminal` — the
// orchestrator's TRUE liveness cursor (advances only on a genuine
// transition, never on the heartbeat timer). A `state_change` whose seq
// does NOT advance (a coalesced beat re-emitting the same seq) writes ZERO
// DOM: we skip the refresh. A backwards seq = the log was cleared on a
// fresh boot (rollover) ⇒ we force a refresh + reset. A frame with NO seq
// (a pre-RUNTIME-V2 server) DEGRADES to the legacy always-refresh path.

import { state } from './state.js';
import { loadEnvironment, pollLogTailAppend } from './api.js';

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
      // ONE consolidated read per beat — nothing else. The per-matchup detail
      // + drift-movements re-fetch that used to ride here was FETCH-AND-DISCARD
      // (no view read either cache); it is gone. Drill-downs are on demand.
      await loadEnvironment();
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
    try {
      const frame = JSON.parse(ev.data);
      // Frame is `{ type, data, seq, terminal }`; older servers send the
      // bare snapshot. A snapshot is a full re-seed (always applied); the
      // cursor only informs the run-state pill + later skips.
      if (frame && typeof frame === 'object' && 'seq' in frame) {
        state.noteProgress(frame.seq, frame.terminal);
      }
      const payload = (frame && typeof frame === 'object' && frame.data != null)
        ? frame.data : frame;
      state.applySnapshot(payload);
    } catch (err) { console.warn('bad snapshot event:', err); }
  });
  _sse.addEventListener('state_change', (ev) => {
    // THE SEQ NO-OP-SKIP GATE. Refresh ONLY on a genuine seq advance (or a
    // rollover = restarted log); a repeat seq (a coalesced no-op beat)
    // writes ZERO DOM — no fetch, no state touched. A frame with no seq
    // (pre-RUNTIME-V2) degrades to the legacy always-refresh path. The
    // run-state pill stays current off the heartbeat frame's own
    // `_changed()` pulse, so STALLED/SETTLED still paint without this fetch.
    let frame = null;
    try { frame = ev && ev.data != null ? JSON.parse(ev.data) : null; }
    catch { frame = null; }
    if (frame && typeof frame === 'object' && 'seq' in frame) {
      const verdict = state.noteProgress(frame.seq, frame.terminal);
      if (verdict.advanced || verdict.rollover) {
        // A genuine advance nudges the chrome ahead of the debounced fetch
        // (digest-gated, so a no-op still writes zero DOM).
        state._changed();
        refreshAfterEvent();
      }
      return;
    }
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
