// views/shared.js — helpers shared across more than one view.
//
// These are the small cross-view functions the render layer relies on:
// the deterministic predicted-gate calculator, the canonical entry-
// status bucket, and the active-tournament field accessors. They are
// pure (no DOM, no state mutation) so every view can import them.

import { DEFAULT_MARGIN } from '../core/format.js';

// --- Canonical entry-status bucket ----------------------------------
//
// The dashboard service normalizes statuses at the read layer, but a
// producer's spelling can still vary. Mapping every spelling here means
// a finished run can NEVER mislabel as queued just because a renderer
// compared against a literal 'done'.
const _ENTRY_STATUS_BUCKET = {
  queued: 'queued', pending: 'queued',
  running: 'running', in_progress: 'running', active: 'running',
  done: 'done', complete: 'done', completed: 'done', finished: 'done',
  // Fast-mode champion rows: the run was not executed this round; the
  // cached per-entry scalar is reused. Bucket with `done` (it has a
  // known scalar) but the producer's `cached` spelling survives on
  // status_raw, so a renderer can show a distinct label if it wants.
  cached: 'done',
  failed: 'failed', fail: 'failed', error: 'failed', aborted: 'failed',
};

// One of 'queued' | 'running' | 'done' | 'failed'.
export function entryStatus(e) {
  const s = String((e && e.status) || '').trim().toLowerCase();
  return _ENTRY_STATUS_BUCKET[s] || 'queued';
}

export function entryIsDone(e) { return entryStatus(e) === 'done'; }
export function entryFailed(e) { return entryStatus(e) === 'failed'; }

// The per-entry scalar, under whichever key the producer used.
export function entryScalar(e) {
  if (!e) return null;
  const v = (e.scalar_score != null) ? e.scalar_score
    : (e.score != null) ? e.score
      : (e.child && typeof e.child.drift_loss === 'number') ? e.child.drift_loss
        : null;
  return (typeof v === 'number' && isFinite(v)) ? v : null;
}

// --- Active-tournament field accessors ------------------------------
//
// The active-tournament record reaches the dashboard from producers
// whose field names have drifted. These normalise all of them.

export function liveChampionId(t) {
  if (!t) return null;
  return t.parent_generation_id || t.parent_id || t.champion || null;
}

export function liveChallengerId(t) {
  if (!t) return null;
  return t.child_generation_id || t.child_id || t.generation_id || t.challenger || null;
}

export function liveRoundLabel(t) {
  if (!t) return null;
  const r = (t.round_index != null) ? t.round_index : t.round;
  return r == null ? null : String(r);
}

// --- Data-quality summary -------------------------------------------
//
// A tournament's run population, split by terminal state — the source
// for the Tournament view's "14 runs: 9 completed / 5 failed" indicator.
export function dataQuality(entries) {
  const list = Array.isArray(entries) ? entries : [];
  let completed = 0;
  let failed = 0;
  let running = 0;
  let queued = 0;
  for (const e of list) {
    const s = entryStatus(e);
    if (s === 'done') completed += 1;
    else if (s === 'failed') failed += 1;
    else if (s === 'running') running += 1;
    else queued += 1;
  }
  return { total: list.length, completed, failed, running, queued };
}

// --- Predicted-gate verdict -----------------------------------------
//
// Given partial tournament entries, project the child's scalar three
// ways (actual / best-case / worst-case) and return a deterministic
// verdict { verdict:'promote'|'reject'|'tbd', reason, projection }.
export function predictedGateVerdict(tournament, margin) {
  if (!tournament) return null;
  margin = (typeof margin === 'number' && isFinite(margin)) ? margin : DEFAULT_MARGIN;

  const entries = tournament.entries || [];
  if (entries.length === 0) {
    return { verdict: 'tbd', reason: 'no entries yet', projection: null };
  }

  const finished = entries.filter(entryIsDone);
  const remaining = entries.length - finished.length;

  let parentDrift = 0;
  let childDrift = 0;
  let parentPass = 0;
  let childPass = 0;
  let lockedPassRegression = false;
  let parentDriftMax = 0;

  for (const e of finished) {
    if (e.parent) {
      parentDrift += (e.parent.drift_loss || 0);
      parentPass += (e.parent.pass ? 1 : 0);
      parentDriftMax = Math.max(parentDriftMax, e.parent.drift_loss || 0);
    }
    if (e.child) {
      childDrift += (e.child.drift_loss || 0);
      childPass += (e.child.pass ? 1 : 0);
    }
    if (e.parent && e.child && e.parent.pass === true && e.child.pass === false) {
      lockedPassRegression = true;
    }
  }

  const n = entries.length;
  const driftCeiling = remaining > 0 ? Math.max(parentDriftMax * 2, 1.0) : 0;
  const parentMeanDrift = finished.length > 0 ? parentDrift / finished.length : 0.5;
  const parentMeanPass = finished.length > 0 ? parentPass / finished.length : 0.5;

  const bestChildDrift = (childDrift + remaining * parentMeanDrift) / n;
  const worstChildDrift = (childDrift + remaining * driftCeiling) / n;
  const parentProjDrift = (parentDrift + remaining * parentMeanDrift) / n;

  const bestChildPass = (childPass + remaining * 1.0) / n;
  const worstChildPass = childPass / n;
  const parentProjPass = (parentPass + remaining * parentMeanPass) / n;

  const scalar = (drift, pass) => drift - pass;

  const projection = {
    parent_scalar: scalar(parentProjDrift, parentProjPass),
    child_best: scalar(bestChildDrift, bestChildPass),
    child_worst: scalar(worstChildDrift, worstChildPass),
    delta_best: scalar(bestChildDrift, bestChildPass) - scalar(parentProjDrift, parentProjPass),
    delta_worst: scalar(worstChildDrift, worstChildPass) - scalar(parentProjDrift, parentProjPass),
    margin,
    remaining,
  };

  if (lockedPassRegression) {
    return { verdict: 'reject', reason: 'pass-rate regression already locked in', projection };
  }
  if (projection.child_best >= projection.parent_scalar - margin) {
    return {
      verdict: 'reject',
      reason: remaining === 0
        ? 'child failed to clear margin'
        : 'cannot recover even if remaining entries match parent',
      projection,
    };
  }
  if (projection.child_worst < projection.parent_scalar - margin) {
    return {
      verdict: 'promote',
      reason: 'already winning regardless of remaining entries',
      projection,
    };
  }
  return { verdict: 'tbd', reason: 'depends on remaining entries', projection };
}

// Classify a resolved tournament's outcome into a fine verdict that
// distinguishes a regression from a near-miss. `deltaScalar` is
// child − champion; a negative delta is an improvement (lower loss).
//   promoted   — decision promoted
//   regression — rejected AND a clear loss past the margin
//   near_miss  — rejected but within the margin band (a near miss)
//   rejected   — rejected, indeterminate
export function tournamentVerdict(decision, deltaScalar, margin) {
  const d = String(decision || '').toLowerCase();
  if (d === 'promoted' || d === 'promote') return 'promoted';
  const m = (typeof margin === 'number' && isFinite(margin)) ? margin : DEFAULT_MARGIN;
  if (typeof deltaScalar === 'number' && isFinite(deltaScalar)) {
    // delta > 0 means the challenger scored worse (higher loss).
    if (deltaScalar > m) return 'regression';
    if (deltaScalar > -m) return 'near_miss';
  }
  return 'rejected';
}
