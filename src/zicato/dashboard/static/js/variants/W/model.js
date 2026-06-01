// variants/W/model.js — the shared data-model resolver for Variant W ("Arena").
//
// The standings hero, the tree sidebar, and the comparison-first detail pane all
// need the SAME view of the hierarchy and the tournament: which epochs exist,
// which generations live in the selected epoch, who reigns, and which match-ups
// each candidate fought. This module centralises that resolution so the
// broadcast standings and the detail never disagree, and so the model is
// "all-epochs-first" even though the live data carries one epoch. Pure reads —
// no DOM, no mutation.

import * as D from '../P/data.js';
import { normaliseDecision } from './ui.js';

export function isNum(v) { return typeof v === 'number' && isFinite(v); }

// The generations of an epoch, normalised to { id, parent, promoted }, plus the
// resolved champion id and a generation→scalar map.
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

// Every matchup a candidate was IN — champion OR challenger. v0 must surface
// BOTH v0-vs-v1 and v0-vs-v2.
export async function matchupsFor(genId) {
  const br = await D.bracket();
  let all = (br && Array.isArray(br.matchups)) ? br.matchups.slice() : [];
  if (!all.length) {
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
