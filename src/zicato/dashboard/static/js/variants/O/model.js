// variants/O/model.js — shared structural reads for Compass.
//
// Both panes (the rail + the detail) need the same epoch / generation /
// board skeleton. This module fetches it once (through the cached data
// layer) and shapes it into the small structures the rail + views consume.

import { state } from '../../core/state.js';
import * as D from './data.js';
import { isNum } from './svg.js';

export async function loadEpochId() {
  const ep = await D.epoch();
  return (ep && ep.epoch_id)
    || (state.epochDef && state.epochDef.epoch_id)
    || (state.heartbeat && state.heartbeat.epoch_id)
    || null;
}

// Parent-before-child ordering, falling back to input order.
export function orderGenerations(gens) {
  const byId = new Map(gens.map((g) => [g.generation_id, g]));
  const seen = new Set();
  const out = [];
  const visit = (g) => {
    if (!g || seen.has(g.generation_id)) return;
    if (g.parent_generation_id && byId.has(g.parent_generation_id)) visit(byId.get(g.parent_generation_id));
    if (seen.has(g.generation_id)) return;
    seen.add(g.generation_id); out.push(g);
  };
  for (const g of gens) visit(g);
  return out;
}

export function lineageX(g, ordered) {
  const byId = new Map(ordered.map((n) => [n.generation_id, n]));
  let depth = 0; let cur = g;
  while (cur && cur.parent_generation_id && byId.has(cur.parent_generation_id)) { depth += 1; cur = byId.get(cur.parent_generation_id); }
  return depth;
}

// The challenger's delta_scalar is the only per-generation scalar the
// tournament payload carries; the seed has none.
export function scalarsFromTournaments(tours) {
  const m = new Map();
  if (tours && Array.isArray(tours.matchups)) {
    for (const t of tours.matchups) if (isNum(t.delta_scalar)) m.set(t.challenger, t.delta_scalar);
  }
  return m;
}

// The full structural model the rail consumes. `forEpochId` scopes the
// generation/board skeleton to ONE epoch (the rail uses this for the
// expanded epoch); when omitted it falls back to the live epoch from
// /api/epoch (back-compat for the candidate / board / run views).
export async function loadRailModel(forEpochId) {
  const epochId = forEpochId || await loadEpochId();
  const [lineage, ep, tours] = await Promise.all([
    D.lineage(), epochId ? D.epoch() : Promise.resolve(null), D.tournaments(),
  ]);
  let rawGens = (lineage && Array.isArray(lineage.generations)) ? lineage.generations : [];
  // Scope to the requested epoch when the lineage carries epoch_id and the
  // generation set spans more than one epoch.
  if (forEpochId && rawGens.some((g) => g.epoch_id)) {
    rawGens = rawGens.filter((g) => !g.epoch_id || g.epoch_id === forEpochId);
  }
  const ordered = orderGenerations(rawGens);
  const scalarById = scalarsFromTournaments(tours);
  const gens = ordered.map((g) => ({
    id: g.generation_id,
    promoted: !!g.promoted,
    parent: g.parent_generation_id || null,
    x: lineageX(g, ordered),
    scalar: scalarById.has(g.generation_id) ? scalarById.get(g.generation_id) : null,
  }));
  const board = (ep && Array.isArray(ep.board)) ? ep.board.map((b) => ({
    id: b.id, kind: b.kind, weight: b.weight, budget_s: b.budget_s,
    input_preview: b.input_preview, expectation_kind: b.expectation_kind, tags: b.tags || [],
  })) : [];
  return { epochId, gens, ordered, rawGens, board, scalarById, tours };
}

// The ALL-EPOCHS workspace model — the root of the rail + the workspace
// detail. Groups every generation from /api/lineage by its `epoch_id`
// (parent-before-child within each group). Degrades gracefully when the
// live data carries only ONE epoch (one group) — the STRUCTURE stays
// all-epochs-first regardless. The active/live epoch (from /api/epoch) is
// flagged so the rail can pre-expand and the workspace can mark it.
export async function loadWorkspaceModel() {
  const liveEpochId = await loadEpochId();
  const [lineage, tours, traj] = await Promise.all([
    D.lineage(), D.tournaments(), D.scoreTrajectory(),
  ]);
  const rawGens = (lineage && Array.isArray(lineage.generations)) ? lineage.generations : [];
  const scalarById = scalarsFromTournaments(tours);

  // Group by epoch_id, preserving first-seen epoch order; generations with
  // no epoch_id fall under the live epoch (single-epoch live data).
  const groupsMap = new Map();
  const epochOrder = [];
  for (const g of rawGens) {
    const eid = g.epoch_id || liveEpochId || '—';
    if (!groupsMap.has(eid)) { groupsMap.set(eid, []); epochOrder.push(eid); }
    groupsMap.get(eid).push(g);
  }
  // Ensure the live epoch shows even if it has no generations recorded yet.
  if (liveEpochId && !groupsMap.has(liveEpochId)) { groupsMap.set(liveEpochId, []); epochOrder.push(liveEpochId); }

  const epochs = epochOrder.map((eid) => {
    const ordered = orderGenerations(groupsMap.get(eid));
    const gens = ordered.map((g) => ({
      id: g.generation_id, promoted: !!g.promoted,
      parent: g.parent_generation_id || null, x: lineageX(g, ordered),
      scalar: scalarById.has(g.generation_id) ? scalarById.get(g.generation_id) : null,
    }));
    const promoted = gens.filter((g) => g.promoted).length;
    return { epochId: eid, live: eid === liveEpochId, ordered, gens, promoted };
  });

  const trajPoints = (traj && Array.isArray(traj.points)) ? traj.points : [];
  return { liveEpochId, epochs, scalarById, tours, trajPoints };
}
