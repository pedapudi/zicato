// variants/S/model.js — the shared data-model resolver for Variant S.
//
// The tree sidebar and the detail pane both need the SAME view of the
// hierarchy: which epochs exist, which generations live in the selected epoch,
// who is the champion, and the board entries. This module centralises that
// resolution so the tree and the detail never disagree, and so the model is
// "all-epochs-first" even though the live data carries one epoch. Pure reads —
// no DOM, no mutation.

import * as D from './data.js';
import { normaliseDecision } from './ui.js';

export function isNum(v) { return typeof v === 'number' && isFinite(v); }

// Resolve the list of epochs from the consolidated environment / workspace,
// degrading to the single live epoch. Returns [{ epoch_id, goal, closed,
// generation_count, promoted_count, best_scalar }], current epoch first.
export async function epochs() {
  const [env, ws, ep] = await Promise.all([D.environment(), D.workspace(), D.epoch()]);
  const src = (env && Array.isArray(env.epochs) && env.epochs.length) ? env.epochs
    : (ws && Array.isArray(ws.epochs) && ws.epochs.length) ? ws.epochs
      : (ep && ep.epoch_id ? [{ epoch_id: ep.epoch_id, goal: ep.goal, closed: ep.closed }] : []);
  const list = src.map((e) => ({
    epoch_id: e.epoch_id,
    goal: e.goal || (ep && e.epoch_id === ep.epoch_id ? ep.goal : '') || '',
    closed: !!e.closed,
    generation_count: isNum(e.generation_count) ? e.generation_count : null,
    promoted_count: isNum(e.promoted_count) ? e.promoted_count : null,
    best_scalar: isNum(e.best_scalar) ? e.best_scalar : null,
  }));
  const currentId = (env && env.current_epoch_id) || (ws && ws.current_epoch_id) || (ep && ep.epoch_id) || null;
  list.sort((a, b) => (a.epoch_id === currentId ? -1 : b.epoch_id === currentId ? 1 : String(b.epoch_id).localeCompare(String(a.epoch_id))));
  return { list, currentId };
}

// The generations of an epoch, normalised to { id, parent, promoted }, plus the
// resolved champion id and a generation→scalar map. The live `/api/epoch` and
// `/api/lineage` carry the one epoch; for any non-current epoch we still build
// from whatever lineage is available (graceful degradation).
export async function generations() {
  const [lin, ep, traj] = await Promise.all([D.lineage(), D.epoch(), D.scoreTrajectory()]);
  const experiments = (ep && Array.isArray(ep.experiments)) ? ep.experiments : [];
  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));
  const champ = gens.find((g) => g.promoted) || gens.find((g) => !g.parent) || null;
  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  return { gens, championId: champ ? champ.id : null, scalarByGen, experiments };
}

// The board entries of an epoch → [{ id, kind, weight, budget_s, input_preview,
// expectation_kind, tags }].
export async function board() {
  const ep = await D.epoch();
  const arr = (ep && Array.isArray(ep.board)) ? ep.board : [];
  return arr.map((b) => ({
    id: b.entry_id || b.id,
    kind: b.kind || null,
    weight: isNum(b.weight) ? b.weight : null,
    budget_s: isNum(b.budget_s) ? b.budget_s : null,
    input_preview: b.input_preview || null,
    expectation_kind: b.expectation_kind || null,
    tags: Array.isArray(b.tags) ? b.tags : [],
  }));
}

// Every matchup a candidate was IN — champion OR challenger (fix #3). v0 must
// surface BOTH v0-vs-v1 and v0-vs-v2.
export async function matchupsFor(genId) {
  const br = await D.bracket();
  let all = (br && Array.isArray(br.matchups)) ? br.matchups.slice() : [];
  if (!all.length) {
    // synthesise from lineage parents if /api/tournaments is unavailable.
    const { gens } = await generations();
    all = gens.filter((g) => g.parent).map((g) => ({ champion: g.parent, challenger: g.id, decision: g.promoted ? 'promoted' : 'rejected' }));
  }
  return all.filter((m) => m.champion === genId || m.challenger === genId);
}

// Decision label for one generation (baseline | promoted | rejected | …).
export function decisionFor(node, experiments) {
  if (!node) return null;
  if (!node.parent) return 'baseline';
  if (node.promoted) return 'promoted';
  const exp = (experiments || []).find((x) => x.generation_id === node.id);
  return (exp && normaliseDecision(exp.outcome)) || 'rejected';
}
