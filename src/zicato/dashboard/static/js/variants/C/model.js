// variants/C/model.js — derive view models from AppState.
//
// Pure selectors over `state`. Views never reach into the raw shapes;
// they read these. Every selector is total — a missing slice yields an
// empty model, never a throw — so a screen renders an honest empty state
// rather than crashing under partial / pre-snapshot data.

// All generations across all epochs, from /api/lineage.
export function lineageNodes(state) {
  const lin = state.lineage || {};
  const gens = Array.isArray(lin.generations) ? lin.generations : [];
  return gens.map((g) => ({
    id: g.generation_id,
    epochId: g.epoch_id,
    parent: g.parent_generation_id || null,
    promoted: g.promoted, // true | false | null
    createdAt: g.created_at || null,
  }));
}

// Group lineage by epoch, preserving on-disk order.
export function epochsFromLineage(state) {
  const nodes = lineageNodes(state);
  const order = [];
  const byEpoch = new Map();
  for (const n of nodes) {
    if (!byEpoch.has(n.epochId)) { byEpoch.set(n.epochId, []); order.push(n.epochId); }
    byEpoch.get(n.epochId).push(n);
  }
  // Merge any epochs known only from the workspace summary.
  const summaries = Array.isArray(state.epochs) ? state.epochs : [];
  for (const s of summaries) {
    if (s && s.epoch_id && !byEpoch.has(s.epoch_id)) { byEpoch.set(s.epoch_id, []); order.push(s.epoch_id); }
  }
  return order.map((id) => ({
    epochId: id,
    goal: goalForEpoch(state, id),
    generations: byEpoch.get(id) || [],
  }));
}

export function goalForEpoch(state, epochId) {
  const summaries = Array.isArray(state.epochs) ? state.epochs : [];
  const hit = summaries.find((s) => s && s.epoch_id === epochId);
  if (hit && hit.goal) return hit.goal;
  if (state.epochDef && state.epochDef.epoch_id === epochId && state.epochDef.goal) {
    return state.epochDef.goal;
  }
  return null;
}

// The "current" epoch id — the live heartbeat's, else the loaded epochDef's,
// else the last epoch in the lineage.
export function currentEpochId(state) {
  const hb = state.heartbeat || {};
  if (hb.epoch_id) return hb.epoch_id;
  if (state.epochDef && state.epochDef.epoch_id) return state.epochDef.epoch_id;
  const eps = epochsFromLineage(state);
  return eps.length ? eps[eps.length - 1].epochId : null;
}

// The experiments of the loaded epoch contract (state.epochDef).
export function experimentsOf(state) {
  const def = state.epochDef;
  if (!def || !Array.isArray(def.experiments)) return [];
  return def.experiments;
}

export function experimentById(state, genId) {
  return experimentsOf(state).find((e) => e && e.generation_id === genId) || null;
}

export function decisionOf(exp) {
  if (!exp || !exp.outcome) return null;
  const raw = exp.outcome.tournament_decision || exp.outcome.decision || '';
  const d = String(raw).toLowerCase();
  if (d.includes('promot')) return 'promoted';
  if (d.includes('reject')) return 'rejected';
  if (d.includes('defer')) return 'deferred';
  return raw ? d : null;
}

// v0-style baseline seed: no parent, no outcome.
export function isBaselineSeed(exp) {
  if (!exp || typeof exp !== 'object') return false;
  const parent = exp.parent_generation_id;
  if (typeof parent === 'string' && parent !== '') return false;
  return exp.outcome == null;
}

// The mutation points an experiment touched — from `hypothesis.modulating`
// (a list of paths) falling back to the patches the outcome references.
export function mutationPointsOf(exp) {
  if (!exp) return [];
  const hyp = exp.hypothesis || {};
  const mod = hyp.modulating || hyp.mutation_points || hyp.targets;
  if (Array.isArray(mod)) return mod.map((m) => String(m)).filter(Boolean);
  if (typeof mod === 'string' && mod.trim()) {
    return mod.split(/[;,]/).map((s) => s.trim()).filter(Boolean);
  }
  return [];
}

export function scalarOf(exp) {
  if (!exp || !exp.outcome) return null;
  const v = exp.outcome.scalar_score;
  return (typeof v === 'number' && isFinite(v)) ? v : null;
}

export function liveGenId(state) {
  const hb = state.heartbeat || {};
  return hb.generation_id || null;
}
