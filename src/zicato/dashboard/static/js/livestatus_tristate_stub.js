// js/livestatus_tristate_stub.js — a LOCAL stand-in for issue #194 §1's
// per-run tri-state, to be DELETED when ui-live's helper lands.
//
// §1 makes liveness a property of files: one server-derived tri-state —
// live (heartbeat fresh) / settled (terminal) / interrupted (stale but
// non-terminal) — consumed by every live surface. The follow pane in §2 is
// one of those consumers: it offers FOLLOW only when a unit is live, and
// opens the same component in settled mode otherwise.
//
// That helper is being built in parallel. Rather than block, the pane
// imports the verdict from HERE and this module derives it from signals
// already on the wire. THE JOIN POINT: replace this module's body with a
// re-export of ui-live's helper (or delete it and repoint the two importers)
// — nothing else in the pane needs to change, because the pane only ever
// reads the three-word verdict.
//
// Deriving it from what exists today:
//   * the transcript's own `complete` flag is the TERMINAL signal — a run
//     that emitted run_completed / run_aborted / conversation_ended is
//     settled no matter what the runtime files say;
//   * an active-run record + a fresh heartbeat is the LIVE signal;
//   * non-terminal with a stale heartbeat is INTERRUPTED — the June-dead
//     workspace rendering "7 units running" forever is exactly the bug §1
//     names, so the stale case must NOT read live.

import { STALE_HEARTBEAT_MS } from './livestatus.js';

export const RUN_TRI = Object.freeze({
  LIVE: 'live', SETTLED: 'settled', INTERRUPTED: 'interrupted',
});

// runTriState({ complete, hasActiveRun, lastHeartbeat, now }) → tri-state.
//
// `complete` comes from the transcript payload (a terminal event was seen),
// `hasActiveRun` from whether this unit has a record under
// runtime/active_runs/, `lastHeartbeat` from the heartbeat payload.
export function runTriState(sig) {
  const s = sig || {};
  if (s.complete) return RUN_TRI.SETTLED;
  if (!s.hasActiveRun) return RUN_TRI.SETTLED;
  return heartbeatFresh(s.lastHeartbeat, s.now) ? RUN_TRI.LIVE : RUN_TRI.INTERRUPTED;
}

// Is the heartbeat within the staleness window? An unparseable or absent
// stamp is NOT fresh — the honest reading of "we cannot tell" is "not live".
export function heartbeatFresh(lastHeartbeat, now) {
  if (!lastHeartbeat) return false;
  const t = Date.parse(lastHeartbeat);
  if (!Number.isFinite(t)) return false;
  const ref = typeof now === 'number' ? now : Date.now();
  return (ref - t) <= STALE_HEARTBEAT_MS;
}
