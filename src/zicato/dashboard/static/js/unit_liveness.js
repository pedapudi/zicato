// js/unit_liveness.js — is THIS board unit live, settled, or interrupted?
//
// Liveness is a property of files, derived on the SERVER, per WORKSPACE: one
// evolve loop holds the workspace lock, so `liveness` answers whether the loop
// is running rather than whether this unit is. The live conversation pane needs
// the second question, because it offers FOLLOW for one (gen, entry) at a time.
// The answer COMPOSES the loop's verdict with whether this unit has an
// active-run record, rather than deriving liveness a second time. Deriving it
// twice is how two surfaces come to disagree about the same run.
//
// `unitLiveness` is that composition. It takes the loop's liveness OBJECT
// (from `livenessFor`) rather than reaching for app state, so it is pure and
// testable with a plain fixture.

// The verdict vocabulary comes from livestatus.js — re-exported, never
// redefined, so there is one spelling of these three words in the console.
export { LIVENESS } from './livestatus.js';

import { LIVENESS as L } from './livestatus.js';

// unitLiveness({ liveness, hasActiveRun }) → one of LIVENESS.
//
//   * the loop is live AND this unit has an active-run record → LIVE. Only
//     here is FOLLOW offered.
//   * no active-run record → SETTLED. The unit finished; its transcript is
//     final whatever the loop is doing.
//   * an active-run record but the loop is NOT live → INTERRUPTED. This unit
//     was mid-run when the loop stopped, so its score was never committed, and
//     it must not render as "running".
//
// Note there is no lookup for a settled per-entry record. The pane's own
// transcript carries the authoritative terminal signal: a run_completed,
// run_aborted or conversation_ended event. That signal outranks any runtime
// file, so a stale active-run record can never resurrect a run the events
// stream says finished.
export function unitLiveness(sig) {
  const s = sig || {};
  const live = !!(s.liveness && s.liveness.live);
  if (!s.hasActiveRun) return L.SETTLED;
  return live ? L.LIVE : L.INTERRUPTED;
}

// Does `activeRuns` carry a record for this exact (gen, entry)? The pane
// follows ONE unit, so a sibling unit's record must not make it read live.
export function hasActiveRunFor(activeRuns, gen, entry) {
  return (Array.isArray(activeRuns) ? activeRuns : []).some(
    (r) => r && r.generation_id === gen && r.entry_id === entry,
  );
}
