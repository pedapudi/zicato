// js/rounds.js — the EPOCH ROUND-TIMELINE consumer.
//
// Within ONE epoch the outer evolve loop runs N ROUNDS along a CHAMPION SPINE
// (incoming champion + a freshly-minted field → tournament → gate). The
// settled and live rounds plus the loss-floor waterfall are SERVED by
// `GET /api/epoch/{id}/round-timeline`. The client performs no four-endpoint
// join of epoch, lineage, score-trajectory and tournaments.
//
// This module only renames served fields for the renderer. Tournament records,
// projected standings, gate outcomes, and live rounds arrive already joined.
//
// A NULL timeline (the endpoint absent — e.g. the Rust supervisor) yields an
// EMPTY settled list: the views render their honest empty state; the rounds
// are never re-derived client-side.

import { isNum } from './svg.js';

// ── the timeline consumer ────────────────────────────────────────────
//   timeline: the /api/epoch/{id}/round-timeline payload (or null).
// → rounds: [{ round_index, champion: {id, scalar, evalMode, runRef,
//     fromRecord}, challengers: [{id, scalar, promoted, projected?,
//     boards_done?, boards_total?, status?}], structure, gateOutcome:
//     {kind, gen}, tournamentRef, source, inflight? }]
export function roundsFromTimeline(opts) {
  const o = opts || {};
  const timeline = (o.timeline && typeof o.timeline === 'object') ? o.timeline : null;
  const structure = String(o.structure || (timeline && timeline.structure) || 'gauntlet');

  const serverRounds = (timeline && Array.isArray(timeline.rounds)) ? timeline.rounds : [];
  const settled = serverRounds.map((r, i) => {
    const champ = (r && r.champion && typeof r.champion === 'object') ? r.champion : null;
    const challengers = (Array.isArray(r && r.challengers) ? r.challengers : []).map((c) => {
      const id = String(c && c.id);
      const settledScalar = isNum(c && c.scalar) ? c.scalar : null;
      return { id, scalar: settledScalar, promoted: !!(c && c.promoted),
        projected: !!(c && c.projected), status: c && c.status,
        boards_done: isNum(c && c.boards_done) ? c.boards_done : null,
        boards_total: isNum(c && c.boards_total) ? c.boards_total : null };
    });
    const gate = (r && r.gate && typeof r.gate === 'object') ? r.gate : null;
    return {
      round_index: isNum(r && r.round_index) ? r.round_index : i,
      champion: champ ? {
        id: champ.id != null ? String(champ.id) : null,
        scalar: isNum(champ.scalar) ? champ.scalar : null,
        evalMode: champ.eval_mode || null,
        runRef: champ.run_ref || null,
        fromRecord: !!champ.from_record,
      } : null,
      challengers,
      structure: (r && r.structure) || structure,
      gateOutcome: gate
        ? { kind: String(gate.kind || 'held'), gen: gate.gen != null ? String(gate.gen) : null }
        : { kind: 'held', gen: null },
      // ID-KEYED lookup — the server names the round's field record.
      tournamentRef: (r && r.tournament) || null,
      source: (r && r.source) || 'server',
      inflight: !!(r && r.inflight),
      phase: (r && r.phase) || null,
    };
  });

  return settled;
}

// A stable structural digest of the rounds model — round ids + champion (id +
// loss bucket) + minted challenger ids + gate outcome + structure. The loss is
// rounded so a steady heartbeat with no real change is a true no-op.
export function roundModelDigest(rounds) {
  const list = Array.isArray(rounds) ? rounds : [];
  return JSON.stringify(list.map((r) => [
    r.round_index, r.structure, r.source,
    // the IN-FLIGHT flag folds into the digest so a settling-in transition (a
    // proposing round → a recorded round) repaints, but a steady proposing beat
    // with the SAME field stays byte-identical (anti-flash).
    r.inflight ? 'L' : '',
    r.champion ? [r.champion.id, isNum(r.champion.scalar) ? r.champion.scalar.toFixed(2) : null, r.champion.evalMode || ''] : null,
    (Array.isArray(r.challengers) ? r.challengers : []).map((c) => [
      c.id, c.promoted, isNum(c.scalar) ? c.scalar.toFixed(2) : null,
      // the per-challenger proposing-step status (proposing/applied/rejected) on
      // an in-flight round, so the banner's "N proposed · M applied" repaints as
      // a slot transitions but stays stable on a no-op beat.
      c.status || '',
      // a PROJECTED (in-flight) challenger — flag + ROUNDED scalar + integer
      // board counts so a no-op beat is byte-identical, a board landing repaints.
      c.projected ? 'j' + (isNum(c.scalar) ? c.scalar.toFixed(3) : '?')
        + '/' + (c.boards_done == null ? '?' : c.boards_done) + '/' + (c.boards_total == null ? '?' : c.boards_total) : '',
    ]),
    r.gateOutcome ? [r.gateOutcome.kind, r.gateOutcome.gen] : null,
  ]));
}

// The LOSS-FLOOR WATERFALL steps: SERVED on the round-timeline payload
// (`timeline.waterfall`, computed by the reader beside the rounds). This
// accessor only type-guards the read — the steps are never re-derived.
export function waterfallSteps(timeline) {
  const wf = timeline && typeof timeline === 'object' ? timeline.waterfall : null;
  return Array.isArray(wf) ? wf.filter((s) => s && typeof s === 'object') : [];
}

// The CHAMPION REIGN model from an epoch round model: one entry per champion in
// succession order, each spanning the rounds it held the title. A generation is
// the round's champion when it is that round's carried-in `champion.id`; its
// reign runs from the first such round to the last consecutive one. The LAST
// champion (the still-reigning one) is flagged `current`. PURE.
//   → [{ id, fromRound, toRound, current }]
export function reignModel(rounds) {
  const list = Array.isArray(rounds) ? rounds : [];
  const reigns = [];
  for (const r of list) {
    const id = r.champion && r.champion.id != null ? String(r.champion.id) : null;
    if (id == null) continue;
    const last = reigns[reigns.length - 1];
    if (last && last.id === id) last.toRound = r.round_index;
    else reigns.push({ id, fromRound: r.round_index, toRound: r.round_index, current: false });
  }
  if (reigns.length) reigns[reigns.length - 1].current = true;
  return reigns;
}

// Group an epoch's generations BY the SERVED round timeline (for the tree).
// Returns [{ round_index, challengers:[gen], gateOutcome, championId, source }]
// where each `challengers` entry is the FULL gen object (so the tree can read
// promoted / orphan / champion flags). A NULL timeline yields [] — the tree
// then renders its flat generation list (the honest degrade; rounds are never
// re-derived client-side).
export function roundsForTree(opts) {
  const o = opts || {};
  const gens = Array.isArray(o.gens) ? o.gens : [];
  const byId = new Map();
  for (const g of gens) if (g && g.id != null) byId.set(String(g.id), g);
  if (!o.timeline) return [];
  const model = roundsFromTimeline({
    timeline: o.timeline, bracket: o.bracket, gens, structure: o.structure, championId: o.championId,
  });
  return model.map((r) => ({
    round_index: r.round_index,
    championId: r.champion ? r.champion.id : null,
    championEvalMode: r.champion ? r.champion.evalMode : null,
    championRunRef: r.champion ? r.champion.runRef : null,
    gateOutcome: r.gateOutcome,
    source: r.source,
    challengers: r.challengers.map((c) => byId.get(String(c.id))).filter(Boolean),
  }));
}
