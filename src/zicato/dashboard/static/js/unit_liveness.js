// js/unit_liveness.js — is THIS board unit live, settled, or interrupted?
//
// Issue #194 §1 makes liveness a property of files and derives it on the
// SERVER, per WORKSPACE: one evolve loop holds the workspace lock, so
// `liveness` answers "is the loop running", not "is this unit running". The
// live conversation pane needs the second question — it offers FOLLOW for one
// (gen, entry) at a time — and the answer is a COMPOSITION of the loop's
// verdict with whether this unit has an active-run record, not a second
// derivation. Deriving it twice is how two surfaces end up disagreeing about
// the same run.
//
// `unitLiveness` is that composition and is permanent. It takes the loop's
// liveness OBJECT rather than reaching for app state, so it is pure, and so it
// does not care whether that object came from §1's helper or from the shim
// below.

//: The three verdicts, matching §1's `LIVENESS` vocabulary exactly.
export const LIVENESS = Object.freeze({
  LIVE: 'live', SETTLED: 'settled', INTERRUPTED: 'interrupted',
});

// unitLiveness({ liveness, hasActiveRun }) → one of LIVENESS.
//
//   * the loop is live AND this unit has an active-run record → LIVE. Only
//     here is FOLLOW offered.
//   * no active-run record → SETTLED. The unit finished; its transcript is
//     final whatever the loop is doing.
//   * an active-run record but the loop is NOT live → INTERRUPTED. This unit
//     was mid-run when the loop died, so its score was never committed — the
//     case §1 exists to stop rendering as "running".
export function unitLiveness(sig) {
  const s = sig || {};
  const live = !!(s.liveness && s.liveness.live);
  if (!s.hasActiveRun) return LIVENESS.SETTLED;
  return live ? LIVENESS.LIVE : LIVENESS.INTERRUPTED;
}

// Does `activeRuns` carry a record for this exact (gen, entry)? The pane
// follows ONE unit, so a sibling unit's record must not make it read live.
export function hasActiveRunFor(activeRuns, gen, entry) {
  return (Array.isArray(activeRuns) ? activeRuns : []).some(
    (r) => r && r.generation_id === gen && r.entry_id === entry,
  );
}

// ---------------------------------------------------------------------------
// TEMPORARY — delete when §1's helper lands
// ---------------------------------------------------------------------------
//
// §1 exports `livenessFor(appState, now)` from js/livestatus.js, returning
// `{ status, liveness }` where `liveness` is
// `{ state, live, endedAt, lastHeartbeat }`. That work is on its own branch and
// not yet on main, so this shim answers the SAME shape from the same signals
// the server already publishes, and every consumer here imports `livenessFor`
// rather than reading app state directly — which is the seam §1 asks for.
//
// THE SWAP, when it merges, is two lines and no logic:
//   1. in js/convo.js and js/views/board.js, import { livenessFor } from
//      './livestatus.js' instead of from here;
//   2. delete everything below this banner.
// `unitLiveness` / `hasActiveRunFor` above are unaffected — they take the
// liveness object as an argument precisely so this swap cannot reach them.
//
// The one semantic difference to know about: §1's server derivation can see
// the progress event log's TERMINAL marker, so it can say `settled` where this
// shim can only say "the heartbeat went stale" (`interrupted`). The client
// only ever DEMOTES live and never promotes it, so the shim is conservative in
// the safe direction: it can read interrupted where the server would read
// settled, never live where the server would not.

import { STALE_HEARTBEAT_MS } from './livestatus.js';

export function livenessFor(appState, now) {
  const st = appState || {};
  // Prefer the server's verdict the moment it exists — an older server simply
  // has no `liveness` key, and then the heartbeat age is all we have.
  const served = st.liveness;
  if (served && typeof served === 'object' && typeof served.state === 'string') {
    return {
      status: null,
      liveness: {
        state: served.state,
        live: served.state === LIVENESS.LIVE,
        endedAt: served.ended_at != null ? served.ended_at : null,
        lastHeartbeat: served.last_heartbeat != null ? served.last_heartbeat : null,
      },
    };
  }
  const hb = (st.heartbeat && st.heartbeat.last_heartbeat) || null;
  const fresh = heartbeatFresh(hb, now);
  return {
    status: null,
    liveness: {
      state: fresh ? LIVENESS.LIVE : LIVENESS.INTERRUPTED,
      live: fresh,
      endedAt: null,
      lastHeartbeat: hb,
    },
  };
}

// Is the heartbeat within the staleness window? An unparseable or absent stamp
// is NOT fresh — the honest reading of "we cannot tell" is "not live". This is
// the ageing §1 calls the client's only liveness job.
export function heartbeatFresh(lastHeartbeat, now) {
  if (!lastHeartbeat) return false;
  const t = Date.parse(lastHeartbeat);
  if (!Number.isFinite(t)) return false;
  const ref = typeof now === 'number' ? now : Date.now();
  return (ref - t) <= STALE_HEARTBEAT_MS;
}
