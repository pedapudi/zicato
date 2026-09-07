// core/sse.js — EventSource wiring + typed delta dispatch.
//
// On connect the server sends a `snapshot`; thereafter it sends
// coalesced `state_change` deltas and `run_log` deltas.
//
//   * snapshot       → state.applySnapshot()
//   * state_change   → debounced ONE /api/environment fetch
//   * run_log        → append-only /api/run-log?after=<cursor> poll, plus a
//                      `run_log:grew` bus emit carrying the frame (which names
//                      the events.jsonl that grew) for the live conversation
//                      pane's cursor-append pull
//   * heartbeat      → state.setHeartbeat() (merge)
//   * : ping         → keepalive, ignored
//
// A burst of state_change frames collapses into AT MOST ONE environment
// fetch per REFRESH_DEBOUNCE_MS — the dashboard never fans a file
// change out into a wave of per-endpoint polls. Because applyEnvironment
// only mutates state and the render layer patches keyed nodes, a delta
// never rebuilds a panel's innerHTML: no flash.
//
// Progress sequence controls liveness. Content revision invalidates reads;
// view digests decide whether the resulting content needs to be painted.

import { state } from './state.js';
import { bus } from './bus.js';
import { loadEnvironment, pollLogTailAppend } from './api.js';

const REFRESH_DEBOUNCE_MS = 400;
const SSE_BACKOFF_MAX_MS = 30_000;

let _sse = null;
let _retry = 0;
let _refreshTimer = null;
let _refreshInFlight = false;
let _requested = 0;
let _applied = 0;
let _desiredRevision;
let _appliedRevision;
let _connection = 0;
let _snapshot = 0;
let _failures = 0;

function refreshAfterEvent(delay = REFRESH_DEBOUNCE_MS) {
  if (_refreshTimer != null || _refreshInFlight || _requested === _applied) return;
  _refreshTimer = setTimeout(async () => {
    _refreshTimer = null;
    _refreshInFlight = true;
    const requested = _requested;
    const revision = _desiredRevision;
    const connection = _connection;
    const snapshot = _snapshot;
    try {
      const success = await loadEnvironment({
        accept: () => connection === _connection && snapshot === _snapshot,
        contentChanged: revision !== _appliedRevision,
      });
      if (success) {
        _applied = requested;
        _appliedRevision = revision;
        _failures = 0;
      } else if (connection === _connection) {
        _failures += 1;
      }
    } finally {
      _refreshInFlight = false;
      refreshAfterEvent(Math.min(SSE_BACKOFF_MAX_MS,
        REFRESH_DEBOUNCE_MS * Math.pow(2, Math.min(_failures, 7))));
    }
  }, delay);
}

function scheduleReconnect() {
  if (_sse) { _sse.close(); _sse = null; }
  _retry += 1;
  const delay = Math.min(SSE_BACKOFF_MAX_MS, 500 * Math.pow(2, Math.min(_retry, 6)));
  setTimeout(connectSSE, delay);
}

export function connectSSE() {
  _connection += 1;
  _requested = _applied = 0;
  _desiredRevision = _appliedRevision = undefined;
  _failures = 0;
  if (_refreshTimer != null) clearTimeout(_refreshTimer);
  _refreshTimer = null;
  if (_sse) _sse.close();
  state.connecting = true;
  state._changed();
  try {
    _sse = new EventSource('/events');
  } catch (err) {
    scheduleReconnect();
    return;
  }
  const source = _sse;
  const listen = (name, callback) => source.addEventListener(name, (event) => {
    if (source === _sse) callback(event);
  });
  listen('open', () => {
    state.connected = true;
    state.connecting = false;
    _retry = 0;
    state._changed();
  });
  listen('snapshot', (ev) => {
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
      _snapshot += 1;
      _desiredRevision = _appliedRevision = frame.content_revision;
      state.contentRevision += 1;
      state.applySnapshot(payload);
    } catch (err) { console.warn('bad snapshot event:', err); }
  });
  listen('state_change', (ev) => {
    let frame;
    try { frame = JSON.parse(ev.data); } catch { frame = {}; }
    const verdict = state.noteProgress(frame?.seq, frame?.terminal);
    const kinds = Array.isArray(frame?.kinds) ? frame.kinds : [frame?.kind];
    const revision = frame?.content_revision;
    const contentChanged = revision != null
      ? revision !== _desiredRevision
      : kinds.some((kind) => kind && kind !== 'heartbeat' && kind !== 'progress');
    if (contentChanged) _desiredRevision = revision ?? Symbol('content');
    if (contentChanged || verdict.advanced || verdict.rollover || !verdict.present) {
      _requested += 1;
      if (verdict.advanced || verdict.rollover) state._changed();
    }
    refreshAfterEvent();
  });
  listen('run_log', (ev) => {
    pollLogTailAppend();
    // The same frame is the LIVE CONVERSATION signal: it fires when an
    // events.jsonl GREW and names which one. Re-emitted on the bus so a
    // follow pane can filter to its own run's file and pull its cursor
    // delta — a pane watching one unit must not refetch on a sibling's
    // growth. A frame we cannot parse is dropped rather than fanned out
    // as an unfiltered wake-up.
    try {
      const frame = ev && ev.data != null ? JSON.parse(ev.data) : null;
      if (frame && typeof frame === 'object') bus.emit('run_log:grew', frame);
    } catch { /* not a frame we can route */ }
  });
  listen('heartbeat', (ev) => {
    try { state.setHeartbeat(JSON.parse(ev.data)); state._changed(); }
    catch { /* ignore */ }
  });
  listen('error', () => {
    state.connected = false;
    state._changed();
    if (_sse && _sse.readyState === EventSource.CLOSED) scheduleReconnect();
  });
}
