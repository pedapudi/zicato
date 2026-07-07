// test/mock_server.mjs — the MOCK SERVER for the two SERVED joins.
//
// The prod frontend no longer joins rounds / racing ladders client-side: the
// server serves them (`build_round_timeline` / `build_racing_field`, pinned by
// tests/test_dashboard_racing_and_rounds.py). The fixture maps in these node
// tests still describe workspaces in terms of the granular endpoints, so this
// module PLAYS THE SERVER: it derives the two served payloads from a fixture
// map exactly as the Python readers do. It is TEST-ONLY scaffolding — nothing
// under js/ imports it — and any behavioural divergence from the Python
// readers is a bug in THIS file, never grounds to re-derive in prod code.

function isNum(v) { return typeof v === 'number' && Number.isFinite(v); }

function lookup(F, base, epochId) {
  if (epochId != null && Object.prototype.hasOwnProperty.call(F, `${base}?epoch=${encodeURIComponent(epochId)}`)) {
    return F[`${base}?epoch=${encodeURIComponent(epochId)}`];
  }
  const v = F[base];
  // The REAL readers are epoch-scoped in SQL — a payload tagged with a
  // DIFFERENT epoch must not leak into this epoch's served join.
  if (v && typeof v === 'object' && v.epoch_id != null && epochId != null
    && String(v.epoch_id) !== String(epochId)) return undefined;
  return v;
}

function gensFor(F, epochId) {
  const lin = lookup(F, '/api/lineage', epochId);
  const rows = (lin && Array.isArray(lin.generations)) ? lin.generations : [];
  const anyTagged = rows.some((g) => g && g.epoch_id != null);
  const scoped = (anyTagged && epochId != null) ? rows.filter((g) => g && g.epoch_id === epochId) : rows;
  const seen = new Set();
  const out = [];
  for (const g of scoped) {
    const id = g && g.generation_id;
    if (id == null || seen.has(String(id))) continue;
    seen.add(String(id));
    out.push(g);
  }
  return out;
}

function scalarByFor(F, epochId) {
  const traj = lookup(F, '/api/score-trajectory', epochId);
  const m = new Map();
  for (const p of (traj && Array.isArray(traj.points)) ? traj.points : []) {
    if (p && isNum(p.scalar)) m.set(String(p.generation_id), p.scalar);
  }
  return m;
}

function competitorIds(record) {
  const comps = Array.isArray(record && record.competitors) ? record.competitors : [];
  return comps
    .map((c) => (c && typeof c === 'object' ? c.generation_id : c))
    .filter((g) => g != null && String(g))
    .map(String);
}

function matchTournamentForField(tournaments, challengerIds) {
  if (!tournaments.length || !challengerIds.length) return null;
  const want = new Set(challengerIds.map(String));
  let best = null;
  let bestHits = 0;
  for (const t of tournaments) {
    const hits = competitorIds(t).filter((c) => want.has(c)).length;
    if (hits > bestHits) { bestHits = hits; best = t; }
  }
  return bestHits > 0 ? best : null;
}

// ── GET /api/epoch/{id}/round-timeline (mirror of build_round_timeline) ──
export function roundTimelineFromFixtures(F, epochId) {
  const bracket = lookup(F, '/api/tournaments', epochId) || {};
  const ep = lookup(F, '/api/epoch', epochId) || {};
  const structure = String(bracket.structure
    || (ep.tournament && ep.tournament.structure) || 'gauntlet');
  const gens = gensFor(F, epochId);
  const scalarBy = scalarByFor(F, epochId);
  const tournaments = (Array.isArray(bracket.tournaments) ? bracket.tournaments : []).filter((t) => t && typeof t === 'object');
  const lineageIds = Array.isArray(bracket.champion_lineage) ? bracket.champion_lineage.map(String) : [];
  const byId = new Map();
  for (const g of gens) if (g && g.generation_id != null) byId.set(String(g.generation_id), g);
  const scalarOf = (id) => (id != null && scalarBy.has(String(id)) ? scalarBy.get(String(id)) : null);
  const promotedOf = (id) => { const g = byId.get(String(id)); return !!(g && g.promoted); };

  const parentless = gens.find((g) => !g.parent_generation_id);
  const seedId = lineageIds.length ? lineageIds[0]
    : (parentless ? String(parentless.generation_id) : null);

  const buildRounds = (perRound, source) => {
    let carried = seedId;
    return perRound.map((r, i) => {
      const challengers = r.challengerIds
        .map(String)
        .filter((id) => carried == null || id !== String(carried))
        .map((id) => ({ id, scalar: scalarOf(id), promoted: promotedOf(id) }));
      const promoted = challengers.find((c) => c.promoted) || null;
      const gate = promoted ? { kind: 'promoted', gen: promoted.id } : { kind: 'held', gen: null };
      const ref = r.tournamentRef || null;
      const refChamp = (ref && ref.champion && ref.champion.id != null) ? ref.champion : null;
      const champId = refChamp ? String(refChamp.id) : carried;
      const round = {
        round_index: isNum(r.round_index) ? r.round_index : i,
        champion: {
          id: champId,
          scalar: (refChamp && isNum(refChamp.scalar)) ? refChamp.scalar : scalarOf(champId),
          eval_mode: refChamp ? (refChamp.eval_mode || null) : null,
          run_ref: refChamp ? (refChamp.run_ref || null) : null,
          from_record: !!refChamp,
        },
        challengers,
        structure,
        gate,
        tournament_id: (ref && ref.tournament_id != null) ? String(ref.tournament_id) : null,
        source,
      };
      if (promoted) carried = promoted.id;
      return round;
    });
  };

  const waterfall = (rounds) => rounds.map((r) => {
    const frm = r.champion && isNum(r.champion.scalar) ? r.champion.scalar : null;
    const promoted = !!(r.gate && r.gate.kind === 'promoted' && r.gate.gen != null);
    const gen = promoted ? r.gate.gen : null;
    let to = frm;
    if (promoted && gen != null) {
      const winner = r.challengers.find((c) => String(c.id) === String(gen));
      if (winner && isNum(winner.scalar)) to = winner.scalar;
    }
    const delta = (isNum(frm) && isNum(to)) ? to - frm : null;
    return { round_index: r.round_index, from: frm, to, delta, promoted: promoted && delta != null && delta !== 0, gen };
  });

  const payload = (rounds, source) => ({ epoch_id: epochId, structure, source, rounds, waterfall: waterfall(rounds) });

  // (1) round_index stamps.
  if (gens.some((g) => g && isNum(g.round_index))) {
    const buckets = new Map();
    for (const g of gens) {
      if (!g || g.generation_id == null) continue;
      if (String(g.generation_id) === String(seedId) && !isNum(g.round_index)) continue;
      const ri = isNum(g.round_index) ? g.round_index : 0;
      if (!buckets.has(ri)) buckets.set(ri, []);
      buckets.get(ri).push(String(g.generation_id));
    }
    const perRound = [...buckets.keys()].sort((a, b) => a - b).map((ri) => ({
      round_index: ri, challengerIds: buckets.get(ri),
      tournamentRef: matchTournamentForField(tournaments, buckets.get(ri)),
    }));
    return payload(buildRounds(perRound, 'round_index'), 'round_index');
  }

  if (structure !== 'racing') {
    // (2) field records.
    const fieldRecords = tournaments.filter((t) => competitorIds(t).length);
    if (fieldRecords.length) {
      const seen = new Set();
      const perRound = fieldRecords.map((t, i) => {
        const comps = competitorIds(t);
        const fresh = comps.filter((id) => !seen.has(id));
        for (const id of comps) seen.add(id);
        return { round_index: i, challengerIds: fresh.length ? fresh : comps, tournamentRef: t };
      });
      return payload(buildRounds(perRound, 'field'), 'field');
    }
    // (3) matchups.
    const matchups = (Array.isArray(bracket.matchups) ? bracket.matchups : []).slice()
      .sort((a, b) => String(a.ran_at || '').localeCompare(String(b.ran_at || '')));
    if (matchups.length) {
      const perRound = matchups.map((m, i) => ({
        round_index: i, challengerIds: [String(m.challenger)], tournamentRef: null,
      }));
      return payload(buildRounds(perRound, 'matchups'), 'matchups');
    }
  }

  // (4) single round 0.
  const challengerIds = gens
    .filter((g) => g && g.generation_id != null && String(g.generation_id) !== String(seedId)
      && (isNum(scalarOf(g.generation_id)) || g.promoted))
    .map((g) => String(g.generation_id));
  const singleRef = (structure !== 'racing')
    ? tournaments.find((t) => competitorIds(t).length) || null
    : null;
  return payload(buildRounds([{ round_index: 0, challengerIds, tournamentRef: singleRef }], 'single'), 'single');
}

// ── GET /api/epoch/{id}/racing-field (mirror of build_racing_field) ──
export function racingFieldFromFixtures(F, epochId) {
  const bracket = lookup(F, '/api/tournaments', epochId) || {};
  return racingFieldFromBracket(bracket, epochId);
}

// Build the served racing-field payload straight from an /api/tournaments
// payload — the exact join build_racing_field performs server-side.
export function racingFieldFromBracket(brk, epochId) {
  const absent = { epoch_id: epochId, present: false };
  const all = Array.isArray(brk && brk.tournaments) ? brk.tournaments : [];
  const racing = all.filter((t) => t && String(t.structure) === 'racing'
    && Array.isArray(t.rounds) && t.rounds.length);
  if (!racing.length) return absent;
  const lineage = Array.isArray(brk.champion_lineage) ? brk.champion_lineage.map(String) : [];
  const envelope = (rounds, params, competitors, standings) => ({
    epoch_id: epochId, present: true, structure: 'racing',
    structure_params: params || {}, competitors: competitors || [],
    rounds, standings: standings || [], champion_lineage: lineage,
    source: 'reconstructed',
  });
  const firstMatch = (r) => (r && Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : {};
  const isFinal = (mid) => String(mid || '') === 'racing-final';

  // FAST PATH — an assembled record with rung-shaped matches.
  const assembled = racing.find((t) => (t.rounds || []).some((r) => {
    const m = firstMatch(r);
    return Array.isArray(m.survivors) || Array.isArray(m.cut) || Array.isArray(m.competitors);
  }));
  if (assembled) {
    const rounds = (assembled.rounds || []).slice();
    const hasFinal = rounds.some((r) => isFinal(firstMatch(r).match_id));
    if (!hasFinal) {
      let lastSurv = [];
      for (let i = rounds.length - 1; i >= 0; i--) {
        const m = firstMatch(rounds[i]);
        if (Array.isArray(m.survivors) && m.survivors.length) { lastSurv = m.survivors.map(String); break; }
      }
      if (lastSurv.length === 1) {
        const survivor = lastSurv[0];
        const promoted = lineage.length ? lineage[lineage.length - 1] === survivor : false;
        const champ = (Array.isArray(assembled.competitors) ? assembled.competitors.map(String) : [])
          .find((c) => c !== survivor) || null;
        rounds.push({
          stage_index: rounds.length, label: 'Champion gate',
          matches: [{ match_id: 'racing-final', competitors: [champ, survivor].filter(Boolean),
            winner: promoted ? survivor : (champ || ''), decision: promoted ? 'promoted' : 'rejected',
            board_fraction: 1.0 }],
        });
      }
    }
    return envelope(rounds, assembled.structure_params || brk.structure_params,
      assembled.competitors, assembled.standings || brk.standings);
  }

  // PER-CHALLENGER JOIN.
  const championOf = (t) => {
    const comps = Array.isArray(t.competitors) ? t.competitors.map(String) : [];
    return comps.length ? comps[0] : null;
  };
  const challengerOf = (t) => {
    const id = String(t.tournament_id || '');
    const arrow = id.lastIndexOf('->');
    if (arrow >= 0) return id.slice(arrow + 2);
    const comps = Array.isArray(t.competitors) ? t.competitors.map(String) : [];
    return comps.length > 1 ? comps[1] : (comps[0] || null);
  };
  const rungIndexOf = (mid) => { const m = /^rung(\d+)/.exec(String(mid || '')); return m ? Number(m[1]) : null; };

  const byRung = new Map();
  const finalists = [];
  const finalMatch = new Map();
  let championId = null;
  for (const t of racing) {
    const chall = challengerOf(t);
    if (!chall) continue;
    const champ = championOf(t);
    if (champ && !championId) championId = champ;
    for (const r of (t.rounds || [])) {
      const mid = r && r.match_id;
      if (isFinal(mid)) {
        if (finalists.indexOf(chall) < 0) finalists.push(chall);
        finalMatch.set(chall, {
          won: !!(r && r.won),
          delta: isNum(r && r.delta_scalar) ? r.delta_scalar : null,
          opponent: (r && r.opponent) || champ || null,
        });
        continue;
      }
      const ri = rungIndexOf(mid);
      if (ri == null) continue;
      if (!byRung.has(ri)) byRung.set(ri, new Map());
      byRung.get(ri).set(chall, {
        delta: isNum(r && r.delta_scalar) ? r.delta_scalar : null,
        won: !!(r && r.won),
      });
    }
  }
  if (!byRung.size && !finalists.length) return absent;

  const params = (brk.structure_params && typeof brk.structure_params === 'object') ? brk.structure_params : {};
  const eta = isNum(params.eta) && params.eta >= 2 ? params.eta : 2;
  const baseFrac = isNum(params.board_fraction) && params.board_fraction > 0 ? params.board_fraction : null;
  const fracFor = (ri) => (baseFrac == null ? null : Math.min(1, baseFrac * Math.pow(eta, ri)));

  const rungIdxs = [...byRung.keys()].sort((a, b) => a - b);
  const rounds = [];
  for (let k = 0; k < rungIdxs.length; k++) {
    const ri = rungIdxs[k];
    const fieldMap = byRung.get(ri);
    const field = [...fieldMap.keys()];
    const nextField = rungIdxs[k + 1] != null ? byRung.get(rungIdxs[k + 1]) : null;
    const survivors = [];
    const cut = [];
    for (const c of field) {
      ((nextField && nextField.has(c)) || finalists.indexOf(c) >= 0 ? survivors : cut).push(c);
    }
    rounds.push({
      stage_index: ri, label: `Rung ${ri}`,
      matches: [{ match_id: `rung${ri}`, competitors: field, survivors, cut,
        board_fraction: fracFor(ri),
        deltas: Object.fromEntries(field.map((c) => [c, fieldMap.get(c).delta])) }],
    });
  }
  if (finalists.length) {
    const crowned = lineage.length ? lineage[lineage.length - 1] : null;
    const survivor = (crowned && finalists.indexOf(crowned) >= 0) ? crowned : finalists[0];
    const fm = finalMatch.get(survivor) || {};
    const promoted = !!fm.won;
    const champ = championId || fm.opponent || null;
    rounds.push({
      stage_index: (rungIdxs.length ? rungIdxs[rungIdxs.length - 1] : 0) + 1,
      label: 'Champion gate',
      matches: [{ match_id: 'racing-final', competitors: [champ, survivor].filter(Boolean),
        winner: promoted ? survivor : (champ || ''), decision: promoted ? 'promoted' : 'rejected',
        delta_scalar: isNum(fm.delta) ? fm.delta : null, board_fraction: 1.0 }],
    });
  }
  return envelope(rounds, params, [], Array.isArray(brk.standings) ? brk.standings : []);
}
