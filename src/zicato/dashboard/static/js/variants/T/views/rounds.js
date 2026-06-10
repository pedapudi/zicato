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
//     source: 'round_index' | 'field' | 'matchups' | 'single' | 'inflight'
//     inflight?: true — a NOT-YET-SETTLED round still proposing/applying its
//                field (no settled bracket, journal, or lineage entry yet); its
//                challengers come from the live envelope's `field_status`.
//   }]
//
// IN-FLIGHT ROUND (issue #16): while round N+1 is PROPOSING/APPLYING its field,
// its challengers exist in NEITHER the journal/lineage NOR a settled tournament
// record yet — so the settled-source derivations below fold them under round N
// (or drop them entirely). When the live envelope (`opts.inflight`) carries a
// non-terminal phase for a round STRICTLY BEYOND the last settled one, we append
// it as its OWN round so the spine timeline shows the new round forming live and
// its proposed/applied counts increment, instead of mis-attributing them.
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

  // ── the IN-FLIGHT round overlay (issue #16) ─────────────────────────
  // A new round that has begun PROPOSING/APPLYING but whose tournament has not
  // settled is invisible to every settled source above (no round_index stamp in
  // the journal yet, no field record, no matchup). Append it as its OWN round —
  // distinct from the last SETTLED one — so the spine shows it forming live and
  // the proposed/applied banner counts increment. The challengers come from the
  // live envelope's `field_status` (the per-challenger applied/rejected/proposing
  // outcomes the orchestrator publishes as the field mints). Idempotent: returns
  // the settled rounds unchanged when there is no genuinely-new in-flight round.
  const withInflight = (settledRounds) => appendInflightRound(settledRounds, {
    inflight: o.inflight, structure, scalarOf, promotedOf, projOf, seedId, isNum,
  });

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
    return withInflight(buildRounds(perRound, 'round_index'));
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
      return withInflight(buildRounds(perRound, 'field'));
    }

    // ── (3) gauntlet matchups — each is its own single-challenger round ──
    const matchups = Array.isArray(bracket.matchups) ? bracket.matchups.slice() : [];
    matchups.sort((a, b) => String(a.ran_at || '').localeCompare(String(b.ran_at || '')));
    if (matchups.length) {
      const perRound = matchups.map((m, i) => ({
        round_index: i, challengerIds: [String(m.challenger)], tournamentRef: null,
      }));
      return withInflight(buildRounds(perRound, 'matchups'));
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
  return withInflight(buildRounds([{ round_index: 0, challengerIds, tournamentRef: singleRef }], 'single'));
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
