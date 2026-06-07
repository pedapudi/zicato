// variants/T/views/rounds.js — the EPOCH ROUND MODEL.
//
// Within ONE epoch the outer evolve loop runs N ROUNDS. Each round =
//   (an incoming CHAMPION, carried in from the prior round)
//   + (a freshly-minted FIELD of challengers, parented to this round)
//   → a TOURNAMENT (the configured structure)
//   → a GATE (one challenger may be promoted to become the next round's champion).
//
// So the epoch is a CHAMPION SPINE threaded through the rounds:
//   v0 (round 0's incoming champion) → promoted → … → reigning champion.
// A challenger has exactly ONE birth-round; the round's champion is a CARRIED-IN
// node (NOT re-parented). Lineage (patched-from) is orthogonal to the spine.
//
// `epochRoundModel` is PURE (plain inputs → a plain rounds[] array) so it
// unit-tests without a DOM and the timeline / tree can digest-gate on it.
//
// SOURCE PRIORITY (degrade gracefully — never break when round_index is absent):
//   (1) per-gen `round_index` (the backend stamp) — the authoritative birth round;
//   (2) the per-round FIELD-TOURNAMENT records (`/api/tournaments` tournaments[]),
//       one field record per round, each listing that round's competitors — map a
//       challenger to the round whose field it first appears in;
//   (3) the gauntlet matchups, round-ordered by ran_at — each matchup is its own
//       single-challenger round;
//   (4) nothing → the whole epoch is a single round 0.

import { isNum } from '../svg.js';

// ── inputs ──────────────────────────────────────────────────────────
//   gens:     [{ id, parent, promoted, round_index? }]  (epoch-scoped)
//   scalarBy: Map(genId → scalar)  (the score trajectory; loss == scalar)
//   bracket:  the /api/tournaments payload (matchups[], tournaments[],
//             champion_lineage[]) — used for the fallback round derivation.
//   structure: the configured structure ('gauntlet' | 'swiss' | …).
//   championId: the SEED champion (round 0's incoming champion).
//
// → rounds: [{
//     round_index,
//     champion:   { id, scalar }            — the carried-in champion this round
//     challengers:[{ id, scalar, promoted }] — the field MINTED this round
//     structure,
//     gateOutcome:{ kind:'promoted'|'held', gen|null }
//     tournamentRef: the matching tournaments[] record (or null)
//     source: 'round_index' | 'field' | 'matchups' | 'single'
//   }]
export function epochRoundModel(opts) {
  const o = opts || {};
  const gens = Array.isArray(o.gens) ? o.gens : [];
  const scalarBy = o.scalarBy instanceof Map ? o.scalarBy : new Map();
  const bracket = (o.bracket && typeof o.bracket === 'object') ? o.bracket : {};
  const structure = String(o.structure || 'gauntlet');
  const lineage = Array.isArray(bracket.champion_lineage) ? bracket.champion_lineage.map(String) : [];
  // The SEED = round 0's INCOMING champion = the epoch's FIRST champion, NOT the
  // current/reigning one. Derive it from the lineage ROOT (lineage[0]) or the
  // parentless seed gen; the caller's `championId` is only a last-resort fallback
  // (callers pass the *current* champion there, which is the wrong end of the
  // spine — using it as the seed scrambles the whole champion reconstruction).
  const parentless = gens.find((g) => !g.parent);
  const seedId = lineage.length ? lineage[0]
    : (parentless ? String(parentless.id)
      : (o.championId != null ? String(o.championId) : null));

  const byId = new Map();
  for (const g of gens) if (g && g.id != null) byId.set(String(g.id), g);
  // the live PROJECTED map ({generation_id: {scalar, boards_done, boards_total}})
  // from the active tournament: an in-flight round's challenger has no SETTLED
  // scalar yet, so fall back to its projected scalar (marked `projected`) so the
  // cross-round timeline shows a climbing standing instead of a blank "—".
  const projected = (o.projected && typeof o.projected === 'object') ? o.projected : {};
  const projOf = (id) => {
    const p = id != null ? projected[String(id)] : null;
    return (p && typeof p === 'object' && isNum(p.scalar)) ? p : null;
  };
  const scalarOf = (id) => (id != null && scalarBy.has(String(id)) ? scalarBy.get(String(id)) : null);
  const promotedOf = (id) => { const g = byId.get(String(id)); return g ? !!g.promoted : false; };

  // each round's challenger ids → its full round record. `champ` is the carried-in
  // champion id for that round (resolved from the spine below).
  const buildRounds = (perRound, source) => {
    // perRound: [{ round_index, challengerIds:[…], tournamentRef? }]
    // the CHAMPION SPINE: round 0's champion is the seed; each subsequent round's
    // champion is the PROMOTED challenger of the prior round (else the prior
    // champion carries on — a held gate).
    let carried = seedId;
    return perRound.map((r, i) => {
      const challengers = r.challengerIds
        .map((id) => String(id))
        .filter((id) => id !== String(carried))   // the carried champion is never a "minted" challenger
        .map((id) => {
          const settled = scalarOf(id);
          // an in-flight challenger with no settled scalar falls back to its
          // live PROJECTED scalar, flagged so the timeline marks it "projected".
          if (settled == null) {
            const p = projOf(id);
            if (p != null) return { id, scalar: p.scalar, promoted: promotedOf(id), projected: true,
              boards_done: isNum(p.boards_done) ? p.boards_done : null,
              boards_total: isNum(p.boards_total) ? p.boards_total : null };
          }
          return { id, scalar: settled, promoted: promotedOf(id) };
        });
      const promotedChallenger = challengers.find((c) => c.promoted) || null;
      const gateOutcome = promotedChallenger
        ? { kind: 'promoted', gen: promotedChallenger.id }
        : { kind: 'held', gen: null };
      // Prefer the CANONICAL per-round champion from the tournament record
      // (`tournamentRef.champion` = {id, scalar, eval_mode, run_ref} surfaced by
      // /api/tournaments) over the reconstructed spine + gen-level scalar — so a
      // champion defending multiple rounds carries its REAL per-round eval
      // (cached vs re-run), not one averaged number. Reconstruction (`carried`
      // + scalarOf) remains the pre-feature fallback when no record resolves.
      const ref = r.tournamentRef || null;
      const refChamp = (ref && ref.champion && ref.champion.id != null) ? ref.champion : null;
      const champId = refChamp ? String(refChamp.id) : carried;
      const round = {
        round_index: isNum(r.round_index) ? r.round_index : i,
        champion: {
          id: champId,
          scalar: (refChamp && isNum(refChamp.scalar)) ? refChamp.scalar : scalarOf(champId),
          evalMode: refChamp ? (refChamp.eval_mode || null) : null,
          runRef: refChamp ? (refChamp.run_ref || null) : null,
          fromRecord: !!refChamp,
        },
        challengers,
        structure,
        gateOutcome,
        tournamentRef: ref,
        source,
      };
      // promote the spine for the next round.
      if (promotedChallenger) carried = promotedChallenger.id;
      return round;
    });
  };

  // ── (1) per-gen round_index — the authoritative birth round ─────────
  const haveRoundIndex = gens.some((g) => g && isNum(g.round_index));
  if (haveRoundIndex) {
    const buckets = new Map();   // round_index → [challenger gen ids]
    for (const g of gens) {
      if (!g || g.id == null) continue;
      // the seed champion is carried, not minted — it has no birth round.
      if (String(g.id) === String(seedId) && !isNum(g.round_index)) continue;
      const ri = isNum(g.round_index) ? g.round_index : 0;
      if (!buckets.has(ri)) buckets.set(ri, []);
      buckets.get(ri).push(String(g.id));
    }
    const ordered = [...buckets.keys()].sort((a, b) => a - b);
    const tournaments = Array.isArray(bracket.tournaments) ? bracket.tournaments : [];
    const perRound = ordered.map((ri) => ({
      round_index: ri,
      challengerIds: buckets.get(ri),
      tournamentRef: matchTournamentForField(tournaments, buckets.get(ri)),
    }));
    return buildRounds(perRound, 'round_index');
  }

  // RACING is persisted as ONE record PER CHALLENGER (not per round), so its
  // records are NOT distinct rounds and its matchups are not distinct rounds —
  // without a round_index stamp a racing epoch is a SINGLE round (the
  // reconstructed aggregate ladder is the round's figure). Short-circuit to (4).
  const tournaments = Array.isArray(bracket.tournaments) ? bracket.tournaments : [];
  if (structure !== 'racing') {
    // ── (2) per-round FIELD-TOURNAMENT records ────────────────────────
    // Each non-gauntlet round persists ONE field record listing that round's
    // competitors; map a record → a round, ordered as recorded. A challenger
    // belongs to the FIRST round whose field it appears in (its birth round).
    const fieldRecords = tournaments.filter((t) => t && Array.isArray(t.competitors) && t.competitors.length);
    if (fieldRecords.length) {
      const seen = new Set();
      const perRound = [];
      fieldRecords.forEach((t, i) => {
        const comps = t.competitors.map((c) => String(c && c.generation_id != null ? c.generation_id : c)).filter(Boolean);
        const fresh = comps.filter((id) => !seen.has(id));
        for (const id of comps) seen.add(id);
        perRound.push({ round_index: i, challengerIds: fresh.length ? fresh : comps, tournamentRef: t });
      });
      return buildRounds(perRound, 'field');
    }

    // ── (3) gauntlet matchups — each is its own single-challenger round ──
    const matchups = Array.isArray(bracket.matchups) ? bracket.matchups.slice() : [];
    matchups.sort((a, b) => String(a.ran_at || '').localeCompare(String(b.ran_at || '')));
    if (matchups.length) {
      const perRound = matchups.map((m, i) => ({
        round_index: i, challengerIds: [String(m.challenger)], tournamentRef: null,
      }));
      return buildRounds(perRound, 'matchups');
    }
  }

  // ── (4) single round 0 — every minted challenger entered one tournament ──
  // (racing always lands here without a round_index stamp; the figure is the
  // reconstructed aggregate ladder, built by the caller from the bracket.)
  // The field is the challengers that ACTUALLY entered the tournament — a gen
  // counts when it scored (has a scalar) or was promoted; an UNSCORED ORPHAN
  // (proposed-but-dropped, no scalar, not promoted) is excluded from the field
  // (it is still surfaced under its round in the tree's trailing orphan bucket).
  const challengerIds = gens
    .filter((g) => g && g.id != null && String(g.id) !== String(seedId) && (isNum(scalarOf(g.id)) || g.promoted))
    .map((g) => String(g.id));
  const singleRef = (structure !== 'racing')
    ? tournaments.find((t) => t && Array.isArray(t.competitors) && t.competitors.length) || null
    : null;
  return buildRounds([{ round_index: 0, challengerIds, tournamentRef: singleRef }], 'single');
}

// Pick the tournaments[] field record whose competitors best match a round's
// challenger field (keyed by the round's first challenger appearing in the
// record's competitors). Null when no record overlaps.
function matchTournamentForField(tournaments, challengerIds) {
  if (!Array.isArray(tournaments) || !challengerIds || !challengerIds.length) return null;
  const want = new Set(challengerIds.map(String));
  let best = null;
  let bestHits = 0;
  for (const t of tournaments) {
    const comps = (Array.isArray(t && t.competitors) ? t.competitors : [])
      .map((c) => String(c && c.generation_id != null ? c.generation_id : c));
    const hits = comps.filter((c) => want.has(c)).length;
    if (hits > bestHits) { bestHits = hits; best = t; }
  }
  return bestHits > 0 ? best : null;
}

// A stable structural digest of the rounds model — round ids + champion (id +
// loss bucket) + minted challenger ids + gate outcome + structure. The loss is
// rounded so a steady heartbeat with no real change is a true no-op.
export function roundModelDigest(rounds) {
  const list = Array.isArray(rounds) ? rounds : [];
  return JSON.stringify(list.map((r) => [
    r.round_index, r.structure, r.source,
    r.champion ? [r.champion.id, isNum(r.champion.scalar) ? r.champion.scalar.toFixed(2) : null, r.champion.evalMode || ''] : null,
    (Array.isArray(r.challengers) ? r.challengers : []).map((c) => [
      c.id, c.promoted, isNum(c.scalar) ? c.scalar.toFixed(2) : null,
      // a PROJECTED (in-flight) challenger — flag + ROUNDED scalar + integer
      // board counts so a no-op beat is byte-identical, a board landing repaints.
      c.projected ? 'j' + (isNum(c.scalar) ? c.scalar.toFixed(3) : '?')
        + '/' + (c.boards_done == null ? '?' : c.boards_done) + '/' + (c.boards_total == null ? '?' : c.boards_total) : '',
    ]),
    r.gateOutcome ? [r.gateOutcome.kind, r.gateOutcome.gen] : null,
  ]));
}

// The LOSS-FLOOR WATERFALL steps from an epoch round model. Each round is a
// step: `from` = the incoming champion's loss floor, `to` = the OUTGOING floor
// (the promoted challenger's loss when the gate promoted, else the floor holds);
// `delta` = to - from (negative = the floor dropped = an improvement); `promoted`
// flags a gate promotion; `gen` is the winning mutation (the promoted challenger).
// PURE — plain rounds[] → plain steps[] — so it unit-tests without a DOM.
export function waterfallModel(rounds) {
  const list = Array.isArray(rounds) ? rounds : [];
  return list.map((r) => {
    const from = r.champion && isNum(r.champion.scalar) ? r.champion.scalar : null;
    const promoted = !!(r.gateOutcome && r.gateOutcome.kind === 'promoted' && r.gateOutcome.gen != null);
    const gen = promoted ? r.gateOutcome.gen : null;
    // the outgoing floor: the promoted challenger's loss when known, else the
    // incoming floor holds (a held round keeps the floor flat).
    let to = from;
    if (promoted && gen != null) {
      const winner = (Array.isArray(r.challengers) ? r.challengers : []).find((c) => String(c.id) === String(gen));
      if (winner && isNum(winner.scalar)) to = winner.scalar;
    }
    const delta = (isNum(from) && isNum(to)) ? to - from : null;
    return { round_index: r.round_index, from, to, delta, promoted: promoted && delta != null && delta !== 0, gen };
  });
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

// Group an epoch's generations BY their birth round (for the tree). Returns
// [{ round_index, challengers:[gen], gateOutcome, championId, source }] using
// the SAME derivation as epochRoundModel, but each `challengers` entry is the
// FULL gen object (so the tree can read promoted / orphan / champion flags).
// Degrades to a single round 0 holding every minted gen when round_index is
// absent and no field/matchup records exist.
export function roundsForTree(opts) {
  const o = opts || {};
  const gens = Array.isArray(o.gens) ? o.gens : [];
  const byId = new Map();
  for (const g of gens) if (g && g.id != null) byId.set(String(g.id), g);
  const model = epochRoundModel({
    gens: gens.map((g) => ({ id: g.id, parent: g.parent, promoted: g.promoted, round_index: g.round_index })),
    scalarBy: o.scalarBy, bracket: o.bracket, structure: o.structure, championId: o.championId,
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
