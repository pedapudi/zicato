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
// `unitLiveness` is that composition. It takes the loop's liveness OBJECT
// (from §1's `livenessFor`) rather than reaching for app state, so it is pure
// and testable with a plain fixture.

// The verdict vocabulary is §1's — re-exported, never redefined, so there is
// exactly one spelling of these three words in the console.
export { LIVENESS } from './livestatus.js';

import { LIVENESS as L } from './livestatus.js';

// unitLiveness({ liveness, hasActiveRun }) → one of LIVENESS.
//
//   * the loop is live AND this unit has an active-run record → LIVE. Only
//     here is FOLLOW offered.
//   * no active-run record → SETTLED. The unit finished; its transcript is
//     final whatever the loop is doing.
//   * an active-run record but the loop is NOT live → INTERRUPTED. This unit
//     was mid-run when the loop died, so its score was never committed — the
//     case §1 exists to stop rendering as "running".
//
// Note there is deliberately no lookup for a settled per-entry record: the
// pane's own transcript carries the authoritative terminal signal (a
// run_completed / run_aborted / conversation_ended event), and that outranks
// any runtime file, so a stale active-run record can never resurrect a run the
// events stream says finished.
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
