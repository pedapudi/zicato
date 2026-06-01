// variants/G/model.js — pure selectors over AppState for Variant G.
//
// Self-contained (no import from other variants). Every selector is
// total: a missing slice yields an empty model, never a throw — so a
// screen renders an honest empty state under partial / pre-snapshot
// data. These also back the digest functions the render spine uses to
// gate repaints (a heartbeat-only tick must produce an identical model
// digest, so the view is a no-op — A bug #1 / #3).

export function lineageNodes(state) {
  const lin = state.lineage || {};
  const gens = Array.isArray(lin.generations) ? lin.generations : [];
  return gens.map((g) => ({
    id: g.generation_id,
    epochId: g.epoch_id,
    parent: g.parent_generation_id || null,
    promoted: g.promoted,
    createdAt: g.created_at || null,
  }));
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

export function isBaselineSeed(exp) {
  if (!exp || typeof exp !== 'object') return false;
  const parent = exp.parent_generation_id;
  if (typeof parent === 'string' && parent !== '') return false;
  return exp.outcome == null;
}

export function experimentsOf(state) {
  const def = state.epochDef;
  if (!def || !Array.isArray(def.experiments)) return [];
  return def.experiments;
}

export function experimentById(state, genId) {
  return experimentsOf(state).find((e) => e && e.generation_id === genId) || null;
}

export function scalarOf(exp) {
  if (!exp || !exp.outcome) return null;
  const v = exp.outcome.scalar_score;
  return (typeof v === 'number' && isFinite(v)) ? v : null;
}

// Spine (champion lineage) + challengers (rejected / live) derived from
// the loaded epoch contract. Shared by the gauntlet, bumps and lineage
// DAG so every screen reads the same family tree.
export function lineageModel(state, epochId) {
  const exps = experimentsOf(state);
  const hb = state.heartbeat || {};
  const liveGen = (hb.epoch_id === epochId && hb.generation_id) ? hb.generation_id : null;
  const spine = [];
  const challengers = [];
  let last = null;
  for (const exp of exps) {
    const id = exp.generation_id || '?';
    const dec = decisionOf(exp);
    const seed = isBaselineSeed(exp);
    const scalar = scalarOf(exp);
    if (seed || dec === 'promoted') {
      spine.push({ id, scalar, seed });
      last = id;
    } else if (dec === 'rejected') {
      challengers.push({
        id, parentId: exp.parent_generation_id || last, decision: 'rejected',
        delta: exp.outcome ? exp.outcome.scalar_score_delta : null, scalar,
      });
    } else {
      challengers.push({
        id, parentId: exp.parent_generation_id || last, decision: null,
        delta: exp.outcome ? exp.outcome.scalar_score_delta : null, scalar,
        live: id === liveGen,
      });
    }
  }
  if (liveGen && !spine.find((n) => n.id === liveGen) && !challengers.find((c) => c.id === liveGen)) {
    challengers.push({ id: liveGen, parentId: last, decision: null, live: true, scalar: null });
  }
  return { spine, challengers, champion: spine[spine.length - 1] || null };
}

// Bumps nodes (champion lane + challenger lane) keyed by generation order.
export function bumpsNodes(state, epochId) {
  const { spine, challengers } = lineageModel(state, epochId);
  const out = [];
  spine.forEach((n, i) => out.push({ id: n.id, x: i, promoted: true, scalar: n.scalar, parent: null }));
  challengers.forEach((c) => {
    const parentX = spine.findIndex((s) => s.id === c.parentId);
    out.push({ id: c.id, x: (parentX >= 0 ? parentX : spine.length - 1) + 0.6, promoted: false, scalar: c.scalar, parent: c.parentId });
  });
  return out;
}

// The candidate set for the tournament topologies (champion first).
export function candidateSet(state, epochId) {
  const { spine, challengers, champion } = lineageModel(state, epochId);
  const out = [];
  if (champion) out.push({ id: champion.id, role: 'champion', cls: 'cz-v-promoted', promoted: true, scalar: champion.scalar });
  for (const c of challengers) {
    out.push({
      id: c.id, role: 'challenger',
      cls: c.decision === 'rejected' ? 'cz-v-rejected' : c.live ? 'cz-v-running' : 'cz-v-neutral',
      decision: c.decision, scalar: c.scalar,
      deltaLabel: (typeof c.delta === 'number' && isFinite(c.delta)) ? (c.delta > 0 ? '+' : '') + c.delta.toFixed(2) : null,
    });
  }
  return out;
}

// Per-entry payload → stable rows. Tolerates {entries:[...]} or a bare array.
export function perEntryRows(payload) {
  if (!payload) return [];
  const rows = Array.isArray(payload.entries) ? payload.entries
    : (Array.isArray(payload) ? payload : []);
  return rows.map((e) => ({
    entryId: e.entry_id || e.id || '?',
    runId: e.run_id || null,
    driftLoss: (typeof e.drift_loss === 'number') ? e.drift_loss : null,
    passFail: (e.pass_fail === 0 || e.pass_fail === 1) ? e.pass_fail : null,
    runtimeMs: (typeof e.runtime_ms === 'number') ? e.runtime_ms : null,
    timeout: e.wall_clock_budget_exceeded === true,
  }));
}

// Matchup-grid payload → paired duel rows (the slopegraph source).
export function matchupGridRows(payload) {
  if (!payload || !Array.isArray(payload.entry_grid)) return [];
  return payload.entry_grid.map((g) => ({
    entryId: g.entry_id || '?',
    championLoss: (typeof g.parent_drift_loss === 'number') ? g.parent_drift_loss : null,
    challengerLoss: (typeof g.child_drift_loss === 'number') ? g.child_drift_loss : null,
    delta: (typeof g.delta === 'number') ? g.delta : null,
    verdict: g.verdict || null,
    wonBy: g.won_by || null,
  }));
}
