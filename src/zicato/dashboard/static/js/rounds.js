// js/rounds.js — the EPOCH ROUND-TIMELINE consumer.
//
// Within ONE epoch the outer evolve loop runs N ROUNDS along a CHAMPION SPINE
// (incoming champion + a freshly-minted field → tournament → gate). The
// SETTLED rounds + the loss-floor waterfall are SERVED by
// `GET /api/epoch/{id}/round-timeline` — the old four-endpoint client join
// (epoch + lineage + score-trajectory + tournaments → rounds) is DELETED.
//
// This module only does what the server cannot:
//   * project the served timeline into the renderer shape, resolving each
//     round's `tournamentRef` by an ID-KEYED lookup into the /api/tournaments
//     payload (never a heuristic competitors-overlap match);
//   * overlay the LIVE projected standings on challengers whose scalar has
//     not settled (the SSE `projected` map);
//   * append the LIVE in-flight round (a field still proposing/applying) from
//     the SSE active-tournament envelope (issue #16).
//
// A NULL timeline (the endpoint absent — e.g. the Rust supervisor) yields an
// EMPTY settled list: the views render their honest empty state; the rounds
// are never re-derived client-side.

import { isNum } from './svg.js';

// ── the timeline consumer ────────────────────────────────────────────
//   timeline: the /api/epoch/{id}/round-timeline payload (or null).
//   bracket:  the /api/tournaments payload — ONLY for the id-keyed
//             tournamentRef lookup (r.tournament_id → tournaments[]).
//   gens:     [{ id, parent, promoted }] (epoch-scoped) — the in-flight
//             append reads promoted flags off it.
//   scalarBy: Map(genId → scalar) — the in-flight append's champion floor.
//   projected: { genId: {scalar, boards_done, boards_total} } (live).
//   inflight: the live active-tournament envelope (or null).
//
// → rounds: [{ round_index, champion: {id, scalar, evalMode, runRef,
//     fromRecord}, challengers: [{id, scalar, promoted, projected?,
//     boards_done?, boards_total?, status?}], structure, gateOutcome:
//     {kind, gen}, tournamentRef, source, inflight? }]
export function roundsFromTimeline(opts) {
  const o = opts || {};
  const timeline = (o.timeline && typeof o.timeline === 'object') ? o.timeline : null;
  const bracket = (o.bracket && typeof o.bracket === 'object') ? o.bracket : {};
  const tournaments = Array.isArray(bracket.tournaments) ? bracket.tournaments : [];
  const refById = new Map();
  for (const t of tournaments) {
    if (t && t.tournament_id != null) refById.set(String(t.tournament_id), t);
  }
  const structure = String(o.structure || (timeline && timeline.structure) || 'gauntlet');
  const gens = Array.isArray(o.gens) ? o.gens : [];
  const byId = new Map();
  for (const g of gens) if (g && g.id != null) byId.set(String(g.id), g);
  const scalarBy = o.scalarBy instanceof Map ? o.scalarBy : new Map();
  const projected = (o.projected && typeof o.projected === 'object') ? o.projected : {};
  const projOf = (id) => {
    const p = id != null ? projected[String(id)] : null;
    return (p && typeof p === 'object' && isNum(p.scalar)) ? p : null;
  };
  const scalarOf = (id) => (id != null && scalarBy.has(String(id)) ? scalarBy.get(String(id)) : null);
  const promotedOf = (id) => { const g = byId.get(String(id)); return g ? !!g.promoted : false; };

  const serverRounds = (timeline && Array.isArray(timeline.rounds)) ? timeline.rounds : [];
  const settled = serverRounds.map((r, i) => {
    const champ = (r && r.champion && typeof r.champion === 'object') ? r.champion : null;
    const challengers = (Array.isArray(r && r.challengers) ? r.challengers : []).map((c) => {
      const id = String(c && c.id);
      const settledScalar = isNum(c && c.scalar) ? c.scalar : null;
      // an in-flight challenger with no settled scalar overlays its LIVE
      // projected scalar, flagged so the timeline marks it "projected".
      if (settledScalar == null) {
        const p = projOf(id);
        if (p != null) {
          return { id, scalar: p.scalar, promoted: !!(c && c.promoted), projected: true,
            boards_done: isNum(p.boards_done) ? p.boards_done : null,
            boards_total: isNum(p.boards_total) ? p.boards_total : null };
        }
      }
      return { id, scalar: settledScalar, promoted: !!(c && c.promoted) };
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
      tournamentRef: (r && r.tournament_id != null)
        ? (refById.get(String(r.tournament_id)) || null) : null,
      source: (r && r.source) || 'server',
    };
  });

  // the SEED (spine root) for the in-flight append: the first served round's
  // champion, else the lineage root, else the parentless gen.
  const lineage = Array.isArray(bracket.champion_lineage) ? bracket.champion_lineage.map(String) : [];
  const parentless = gens.find((g) => !g.parent);
  const seedId = (settled.length && settled[0].champion && settled[0].champion.id != null)
    ? String(settled[0].champion.id)
    : (lineage.length ? lineage[0]
      : (parentless ? String(parentless.id)
        : (o.championId != null ? String(o.championId) : null)));

  return appendInflightRound(settled, {
    inflight: o.inflight, structure, scalarOf, promotedOf, projOf, seedId, isNum,
  });
}

// ── append the IN-FLIGHT round (issue #16) ───────────────────────────
//
// Given the SETTLED rounds + the live active-tournament envelope, append a NEW
// round for a field that is proposing/applying but has not settled. PURE: plain
// inputs → a new rounds[] (or the same array when there is nothing in flight).
//
// A round qualifies as in-flight when ALL hold:
//   * the envelope's phase is NON-TERMINAL (proposing / applying / running),
//   * it carries a `field_status` (the challengers being minted this round),
//   * its `round_index` is BEYOND the last settled round (APPEND — the issue-#16
//     mis-attribution case: a genuinely NEW round), OR equals the last round
//     when that round has NO settled challengers (IN-PLACE overlay — round 0's
//     own first-field proposing). A field that already SETTLED into a recorded
//     round, or a non-empty same-index round, is left untouched (no clobber, no
//     duplicate).
// The new round's `champion` is the carried-in spine champion (the last settled
// round's outgoing champion, or — for the in-place round-0 case — its own
// champion/seed). Its challengers are the field_status ids, each carrying its
// proposing/applied/rejected status + any projected scalar.
function appendInflightRound(settledRounds, ctx) {
  const rounds = Array.isArray(settledRounds) ? settledRounds : [];
  const at = (ctx && ctx.inflight && typeof ctx.inflight === 'object') ? ctx.inflight : null;
  if (!at) return rounds;
  const isNum = ctx.isNum;

  // NON-TERMINAL phase only — a settled / done / idle envelope must not spawn a
  // phantom round (the settled sources already own those).
  const phase = String(at.phase == null ? '' : at.phase).trim().toLowerCase();
  const head = phase.split(':')[0];
  const terminal = phase === '' || head === 'idle'
    || head === 'complete' || head === 'completed' || head === 'done' || head === 'tournament';
  if (terminal) return rounds;

  // the field the round is minting — the per-challenger field_status ids.
  const fs = Array.isArray(at.field_status) ? at.field_status : [];
  const fieldIds = fs.map((f) => (f && f.generation_id != null) ? String(f.generation_id) : null).filter(Boolean);
  if (!fieldIds.length) return rounds;

  // the in-flight round index — the envelope's stamp, else just past the last
  // settled round.
  const lastSettled = rounds.length ? rounds[rounds.length - 1] : null;
  const lastIdx = (lastSettled && isNum(lastSettled.round_index)) ? lastSettled.round_index : -1;
  const stampedIdx = isNum(at.round_index) ? at.round_index : null;
  const inflightIdx = stampedIdx != null ? stampedIdx : (lastIdx + 1);

  // GUARD: if any settled round already owns one of these challenger ids, the
  // field has begun settling into a recorded round — defer to that record (the
  // settled source is authoritative once the round lands).
  const settledIds = new Set();
  for (const r of rounds) for (const c of (Array.isArray(r.challengers) ? r.challengers : [])) settledIds.add(String(c.id));
  if (fieldIds.some((id) => settledIds.has(id))) return rounds;

  // IN-PLACE merge vs. APPEND. When the in-flight round is the SAME index as the
  // last settled round AND that round has NO settled challengers, it is that
  // round's OWN proposing (e.g. round 0's first field, or a re-derived single
  // round whose tournament has not run) — overlay the forming field IN PLACE so
  // the spine episode shows it minting (NOT a phantom duplicate round). When the
  // in-flight round is STRICTLY beyond the last settled one it is a genuinely NEW
  // round → append it (the issue-#16 mis-attribution case). An in-flight index
  // that is <= the last settled round but whose field is already settled is the
  // defer case above; a non-empty last round at the same index keeps its settled
  // field (do not clobber).
  const sameIdxRound = (rounds.length && inflightIdx === lastIdx) ? lastSettled : null;
  if (sameIdxRound && (Array.isArray(sameIdxRound.challengers) ? sameIdxRound.challengers.length : 0) > 0) return rounds;
  if (rounds.length && inflightIdx < lastIdx) return rounds;

  // the carried-in champion = for an APPENDED new round, the last settled round's
  // OUTGOING champion (its promoted challenger, else its own champion — the spine
  // continues); for an IN-PLACE round-0 proposing, that round's own champion/seed.
  let champId = null;
  if (sameIdxRound) {
    champId = sameIdxRound.champion ? String(sameIdxRound.champion.id) : (ctx.seedId != null ? String(ctx.seedId) : null);
  } else if (lastSettled) {
    const promoted = (Array.isArray(lastSettled.challengers) ? lastSettled.challengers : []).find((c) => c.promoted);
    champId = promoted ? String(promoted.id) : (lastSettled.champion ? String(lastSettled.champion.id) : null);
  } else if (ctx.seedId != null) {
    champId = String(ctx.seedId);
  }
  // the champion's incoming loss floor (the spine baseline) — read from the
  // carried champion's settled/projected scalar so the waterfall has a `from`.
  const champScalar = champId != null
    ? (ctx.scalarOf(champId) != null ? ctx.scalarOf(champId)
      : ((ctx.projOf(champId) || {}).scalar != null ? ctx.projOf(champId).scalar : null))
    : null;

  // the challengers minted this round — each carries its proposing-step status
  // (proposing / applied / rejected) so the timeline + the digest reflect the
  // field forming, and any live projected scalar (a climbing standing) it has.
  const challengers = fieldIds.map((id) => {
    const status = statusOf(fs, id);
    const settled = ctx.scalarOf(id);
    const p = ctx.projOf(id);
    const out = { id, scalar: settled != null ? settled : (p != null ? p.scalar : null), promoted: ctx.promotedOf(id), status };
    if (settled == null && p != null) {
      out.projected = true;
      out.boards_done = isNum(p.boards_done) ? p.boards_done : null;
      out.boards_total = isNum(p.boards_total) ? p.boards_total : null;
    }
    return out;
  });

  const inflightRound = {
    round_index: inflightIdx,
    champion: { id: champId, scalar: champScalar, evalMode: null, runRef: null, fromRecord: false },
    challengers,
    structure: ctx.structure,
    // a still-proposing field has not reached its gate — the outcome is pending.
    gateOutcome: { kind: 'pending', gen: null },
    tournamentRef: null,
    source: 'inflight',
    inflight: true,
    phase,
  };
  // IN-PLACE: replace the empty same-index round with the forming field (round 0's
  // own proposing). APPEND: add the new round (the issue-#16 mis-attribution case).
  if (sameIdxRound) {
    const out = rounds.slice();
    out[out.length - 1] = inflightRound;
    return out;
  }
  return rounds.concat([inflightRound]);
}

// the proposing-step status of a challenger id in a field_status list:
// 'applied' | 'proposing' | 'rejected' (default for anything else, mirroring
// data.fieldStatus's normalization).
function statusOf(fieldStatus, id) {
  for (const f of (Array.isArray(fieldStatus) ? fieldStatus : [])) {
    if (f && String(f.generation_id) === String(id)) {
      if (f.status === 'applied') return 'applied';
      if (f.status === 'proposing') return 'proposing';
      return 'rejected';
    }
  }
  return 'proposing';
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
