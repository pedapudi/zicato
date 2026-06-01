// variants/T/views/structure.js — the configured tournament STRUCTURE.
//
// The match-ups page (views/gens.js) renders the gauntlet ladder for the
// default structure. When the epoch's contract names a NON-gauntlet
// structure (single_elim / double_elim / swiss / racing), this module
// renders the ACTUAL configured topology from the new
// /api/tournament-structure response (§3.2): a fit-to-width bracket for
// the elimination structures, a round-by-round standings table for swiss,
// and a successive-halving rung ladder for racing.
//
// Every renderer is driven by the structure payload and degrades
// gracefully when a field is absent (the live workspace is gauntlet-only,
// so the non-gauntlet renderers are exercised with mock payloads in the
// test suite). The SVG marks reuse svg.js's fit-to-width discipline
// (width:100% + viewBox, no pan/zoom, token-themed across all 9 themes,
// scaling with the page-scale pill).

import { el } from '../../../core/dom.js';
import * as svg from '../svg.js';
import { section, empty, verdictPill } from '../ui.js';

// A friendly label + key params for a structure.
export function structureLabel(structure, params) {
  const s = String(structure || 'gauntlet');
  const p = (params && typeof params === 'object') ? params : {};
  switch (s) {
    case 'gauntlet': {
      const reps = p.replications;
      return 'Gauntlet' + (svg.isNum(reps) && reps > 1 ? ` · ${reps}× replications` : '');
    }
    case 'single_elim':
      return 'Single elimination' + (p.seed_order ? ` · seed: ${p.seed_order}` : '');
    case 'double_elim':
      return 'Double elimination' + (p.grand_final_reset === false ? ' · no GF reset' : '');
    case 'swiss':
      return 'Swiss' + (svg.isNum(p.rounds) ? ` · ${p.rounds} rounds` : '');
    case 'racing': {
      const rungs = Array.isArray(p.rungs) ? p.rungs.length : null;
      return 'Racing (successive halving)' + (rungs ? ` · ${rungs} rungs` : '');
    }
    default:
      return s;
  }
}

// The structure pill (shown in the epoch header + the match-ups header).
export function structurePill(structure, params) {
  return el('span', { class: 'dt-structure-pill', 'data-structure': String(structure || 'gauntlet') }, [
    el('span', { class: 'dt-structure-pill-k', text: 'structure' }),
    el('span', { class: 'dt-structure-pill-v', text: structureLabel(structure, params) }),
  ]);
}

// Is this a structure the dedicated renderers below handle (i.e. NOT the
// default gauntlet, which views/gens.js renders with its own ladder)?
export function isNonGauntlet(structure) {
  const s = String(structure || 'gauntlet');
  return s === 'single_elim' || s === 'double_elim' || s === 'swiss' || s === 'racing';
}

// Normalize EITHER shape — the LIVE /api/active-tournament (carries `phase`
// while in flight) OR the COMPLETED /api/tournament-structure — into ONE
// renderer input. They already share the {structure, competitors, rounds,
// standings} shape; this just stamps `live` (so the renderers can suppress
// "rejected"/"eliminated" verdicts for an in-flight tournament — the promote
// decision only commits at the very end, so a half-finished record would
// otherwise mislabel the eventual winner as a dead branch).
//
//   st   — the raw structure payload (live or completed), or null.
//   live — true when this came from /api/active-tournament with a non-idle phase.
export function normalizeStructure(st, live) {
  if (!st || typeof st !== 'object') return null;
  const phase = String(st.phase || '').toLowerCase();
  const running = !!live && phase !== '' && phase !== 'idle'
    && phase !== 'complete' && phase !== 'completed' && phase !== 'done';
  return {
    structure: st.structure || 'gauntlet',
    structure_params: st.structure_params || st.params || {},
    competitors: Array.isArray(st.competitors) ? st.competitors : [],
    rounds: Array.isArray(st.rounds) ? st.rounds : [],
    standings: Array.isArray(st.standings) ? st.standings : [],
    // the epoch's champion succession; the LAST id is the reigning champion
    // (the promoted survivor of a settled racing tournament). Carried through
    // so the racing renderer can confirm the gate's crowned id.
    champion_lineage: Array.isArray(st.champion_lineage) ? st.champion_lineage.map(String) : [],
    source: running ? 'live' : (st.source || 'index'),
    phase: st.phase != null ? String(st.phase) : null,
    live: running,
  };
}

// ── RECONSTRUCT a racing ladder from the per-challenger records ─────
//
// A racing tournament is persisted as ONE record PER CHALLENGER on
// `/api/tournaments` → `{champion_lineage, tournaments:[…]}` — NOT as a single
// assembled-rung record. Each entry is the FLATTENED view from one
// challenger's seat:
//
//   { tournament_id:"<epoch>:<champ>-><chall>", structure:"racing",
//     competitors:[champ, chall, …], standings:[],
//     rounds:[ {match_id:"rung0_m2", opponent, won, delta_scalar}, … ] }
//
// `match_id` encodes the rung: `rungN_*` → rung N; `racing-final` → the
// full-board champion gate. We AGGREGATE every racing record of the epoch and
// GROUP its matches by that prefix to rebuild the successive-halving ladder:
//
//   • field(rung N)     = challengers that have a `rungN_*` match.
//   • survivors(rung N) = those that ALSO appear at rung N+1 (or in the final).
//   • cut(rung N)       = the rest.
//   • champion gate     = the lone survivor's `racing-final` match vs champion;
//     `won` (Δ negative ⇒ lower loss) ⇒ promoted, confirmed by `champion_lineage`.
//
// The result is normalized into the SAME {structure, competitors, rounds,
// standings} shape the LIVE `/api/active-tournament` produces (rung rounds
// carrying competitors/survivors/cut/board_fraction + a `racing-final` gate
// round), so a single renderer handles both. Returns null when the payload
// carries no racing records.
export function reconstructRacing(brk, epochId) {
  if (!brk || typeof brk !== 'object') return null;
  const all = Array.isArray(brk.tournaments) ? brk.tournaments : [];
  const racing = all.filter((t) => t && String(t.structure) === 'racing'
    && Array.isArray(t.rounds) && t.rounds.length);
  if (!racing.length) return null;
  const lineageIds = Array.isArray(brk.champion_lineage) ? brk.champion_lineage.map(String) : [];

  // FAST PATH — an ASSEMBLED record. Some readers (and the test harness) carry
  // a single tournament whose rounds ALREADY hold the rung field with
  // {competitors, survivors, cut} arrays (the same shape the LIVE
  // /api/active-tournament uses). When such a record is present, use its rounds
  // verbatim — there is nothing to aggregate — and only synthesise the
  // champion-gate from `champion_lineage` if the record has no `racing-final`.
  const assembled = racing.find((t) => (Array.isArray(t.rounds) ? t.rounds : []).some((r) => {
    const m = (r && Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : null;
    return m && (Array.isArray(m.survivors) || Array.isArray(m.cut) || Array.isArray(m.competitors));
  }));
  if (assembled) {
    const rounds = (Array.isArray(assembled.rounds) ? assembled.rounds : []).map((r) => r);
    const hasFinal = rounds.some((r) => {
      const m = (r && Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : {};
      return String(m.match_id || '') === 'racing-final';
    });
    // synthesise a gate from the lone final-rung survivor + lineage when the
    // assembled record itself recorded no `racing-final` match.
    if (!hasFinal) {
      let lastSurv = [];
      for (let i = rounds.length - 1; i >= 0; i--) {
        const m = (rounds[i] && Array.isArray(rounds[i].matches) && rounds[i].matches[0]) ? rounds[i].matches[0] : {};
        if (Array.isArray(m.survivors) && m.survivors.length) { lastSurv = m.survivors.map(String); break; }
      }
      if (lastSurv.length === 1) {
        const survivor = lastSurv[0];
        const promoted = lineageIds.length ? lineageIds[lineageIds.length - 1] === survivor : false;
        const champ = (Array.isArray(assembled.competitors) ? assembled.competitors.map(String) : [])
          .find((c) => c !== survivor) || null;
        rounds.push({
          round_index: rounds.length,
          label: 'Champion gate',
          matches: [{
            match_id: 'racing-final',
            competitors: [champ, survivor].filter(Boolean),
            winner: promoted ? survivor : (champ || ''),
            decision: promoted ? 'promoted' : 'rejected',
            board_fraction: 1.0,
          }],
        });
      }
    }
    return normalizeStructure({
      structure: 'racing',
      structure_params: assembled.structure_params || brk.structure_params || {},
      competitors: Array.isArray(assembled.competitors) ? assembled.competitors : [],
      rounds,
      standings: Array.isArray(assembled.standings) ? assembled.standings
        : (Array.isArray(brk.standings) ? brk.standings : []),
      champion_lineage: lineageIds,
      source: 'reconstructed',
    }, false);
  }

  // The challenger of a record is the child id — the suffix of the
  // `<epoch>:<champion>-><challenger>` tournament id, falling back to
  // competitors[1] (parent, child, …opponents). The champion is the opponent
  // each rung match was dueled against (competitors[0]).
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

  // rungIndex("rung3_m1") → 3 ; "racing-final" → Infinity (the gate).
  const rungIndexOf = (matchId) => {
    const m = /^rung(\d+)/.exec(String(matchId || ''));
    if (m) return Number(m[1]);
    return null; // not a rung (the gate, or unknown)
  };
  const isFinal = (matchId) => String(matchId || '') === 'racing-final';

  // For each rung index, collect the field + each challenger's Δ-vs-champion;
  // and track which challengers reached the final gate.
  const byRung = new Map();         // rungIdx → Map(challenger → {delta, won})
  const finalists = new Set();      // challengers with a `racing-final` match
  const finalMatch = new Map();     // challenger → {won, delta, opponent}
  let championId = null;
  for (const t of racing) {
    const chall = challengerOf(t);
    if (!chall) continue;
    const champ = championOf(t);
    if (champ && !championId) championId = champ;
    for (const r of (Array.isArray(t.rounds) ? t.rounds : [])) {
      const mid = r && r.match_id;
      if (isFinal(mid)) {
        finalists.add(chall);
        finalMatch.set(chall, {
          won: !!(r && r.won),
          delta: svg.isNum(r && r.delta_scalar) ? r.delta_scalar : null,
          opponent: (r && r.opponent) || champ || null,
        });
        continue;
      }
      const ri = rungIndexOf(mid);
      if (ri == null) continue;
      if (!byRung.has(ri)) byRung.set(ri, new Map());
      byRung.get(ri).set(chall, {
        delta: svg.isNum(r && r.delta_scalar) ? r.delta_scalar : null,
        won: !!(r && r.won),
      });
    }
  }
  if (!byRung.size && !finalists.size) return null;

  // The board fraction per rung, if the contract pins the geometry. The
  // schedule grows the slice by η each rung from a base fraction (default
  // 0.25); rung N covers min(1, base · η^N) of the board.
  const params = (brk.structure_params && typeof brk.structure_params === 'object')
    ? brk.structure_params : {};
  const eta = svg.isNum(params.eta) && params.eta >= 2 ? params.eta : 2;
  const baseFrac = svg.isNum(params.board_fraction) && params.board_fraction > 0
    ? params.board_fraction : null;
  const fracFor = (ri) => (baseFrac == null ? null : Math.min(1, baseFrac * Math.pow(eta, ri)));

  // Ordered rung indices.
  const rungIdxs = [...byRung.keys()].sort((a, b) => a - b);
  // Build the rung rounds. A challenger SURVIVES a rung when it also appears at
  // the NEXT rung, or (for the last rung) in the champion gate.
  const rounds = [];
  for (let k = 0; k < rungIdxs.length; k++) {
    const ri = rungIdxs[k];
    const fieldMap = byRung.get(ri);
    const field = [...fieldMap.keys()];
    const nextIdx = rungIdxs[k + 1];
    const nextField = nextIdx != null ? byRung.get(nextIdx) : null;
    const survivors = [];
    const cut = [];
    for (const c of field) {
      const carried = (nextField && nextField.has(c)) || finalists.has(c);
      (carried ? survivors : cut).push(c);
    }
    const frac = fracFor(ri);
    rounds.push({
      round_index: ri,
      label: `Rung ${ri}`,
      matches: [{
        match_id: `rung${ri}`,
        competitors: field,
        survivors,
        cut,
        board_fraction: frac,
        deltas: Object.fromEntries(field.map((c) => [c, fieldMap.get(c).delta])),
      }],
    });
  }

  // The champion gate (the `racing-final` match). The lone finalist faces the
  // champion on the full board; `won` (Δ negative ⇒ lower loss) ⇒ promoted.
  const finalList = [...finalists];
  const lineage = Array.isArray(brk.champion_lineage) ? brk.champion_lineage.map(String) : [];
  const crownedFromLineage = lineage.length ? lineage[lineage.length - 1] : null;
  if (finalList.length) {
    // Prefer the finalist the lineage crowned; else the lone finalist.
    const survivor = (crownedFromLineage && finalists.has(crownedFromLineage))
      ? crownedFromLineage : finalList[0];
    const fm = finalMatch.get(survivor) || {};
    const promoted = !!fm.won;
    const champ = championId || fm.opponent || null;
    rounds.push({
      round_index: (rungIdxs.length ? rungIdxs[rungIdxs.length - 1] : 0) + 1,
      label: 'Champion gate',
      matches: [{
        match_id: 'racing-final',
        competitors: [champ, survivor].filter(Boolean),
        winner: promoted ? survivor : (champ || ''),
        decision: promoted ? 'promoted' : 'rejected',
        delta_scalar: svg.isNum(fm.delta) ? fm.delta : null,
        board_fraction: 1.0,
      }],
    });
  }

  return normalizeStructure({
    structure: 'racing',
    structure_params: params,
    competitors: [],
    rounds,
    standings: Array.isArray(brk.standings) ? brk.standings : [],
    champion_lineage: lineage,
    source: 'reconstructed',
  }, false);
}

// A stable digest of a structure payload so the gated swap re-renders only
// on a real change.
export function structureDigest(st) {
  if (!st || typeof st !== 'object') return 'no-structure';
  return JSON.stringify({
    structure: st.structure,
    live: !!st.live, phase: st.phase || null,
    champion_lineage: Array.isArray(st.champion_lineage) ? st.champion_lineage : [],
    competitors: (Array.isArray(st.competitors) ? st.competitors : []).map((c) => [c.generation_id, c.seed, c.role]),
    rounds: (Array.isArray(st.rounds) ? st.rounds : []).map((r) => [
      r.round_index, r.label,
      (Array.isArray(r.matches) ? r.matches : []).map((m) => [m.match_id, (m.competitors || []).join('/'), m.winner, m.decision, m.bracket_slot, m.bye, m.survivors && m.survivors.join('/'), m.cut && m.cut.join('/')]),
    ]),
    standings: (Array.isArray(st.standings) ? st.standings : []).map((s) => [s.generation_id, s.rank, s.scalar, s.wins, s.losses, s.status]),
    source: st.source,
  });
}

// ── the structure render dispatch ──────────────────────────────────
//
// Returns an array of DOM nodes (sections) for the configured structure.
// `ctx` carries navigate/href; `epochId` routes a competitor / match to
// the candidate page (the gate is per-match, §3.3).
export function renderStructure(st, ctx, epochId) {
  const structure = String((st && st.structure) || 'gauntlet');
  if (structure === 'swiss') return renderSwiss(st, ctx, epochId);
  if (structure === 'racing') return renderRacing(st, ctx, epochId);
  // single_elim + double_elim share the bracket renderer.
  return renderBracket(st, ctx, epochId, structure);
}

// ---- single / double elimination — a fit-to-width bracket ----------

function renderBracket(st, ctx, epochId, structure) {
  const rounds = (st && Array.isArray(st.rounds)) ? st.rounds : [];
  const open = (m) => {
    // open the (decided) winner's candidate, else the first competitor.
    const gen = m.winner || (Array.isArray(m.competitors) && m.competitors[0]) || null;
    if (gen) ctx.navigate('candidate', { epochId, gen });
  };
  const nodes = [];
  if (structure === 'double_elim') {
    // Split the rounds into the winners' / losers' bands by bracket_slot
    // prefix; the grand final (GF) rides with the winners' band.
    const wb = splitBand(rounds, (slot) => !slot.startsWith('LB'));
    const lb = splitBand(rounds, (slot) => slot.startsWith('LB'));
    const wbCard = el('div', { class: 'dn-panel dn-figpane' });
    wbCard.appendChild(wb.length ? svg.structureBracket({ rounds: wb, onMatch: open }) : empty('No winners-bracket matches yet.'));
    nodes.push(section('Winners’ bracket', wbCard));
    if (lb.length) {
      const lbCard = el('div', { class: 'dn-panel dn-figpane' });
      lbCard.appendChild(svg.structureBracket({ rounds: lb, onMatch: open }));
      nodes.push(section('Losers’ bracket', lbCard));
    }
  } else {
    const card = el('div', { class: 'dn-panel dn-figpane' });
    card.appendChild(rounds.length ? svg.structureBracket({ rounds, onMatch: open }) : empty('No bracket rounds recorded yet.'));
    nodes.push(section('Bracket · click a match to open the candidate', card));
  }
  const standings = standingsTable(st, ctx, epochId, !!(st && st.live));
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// Re-shape the structure rounds keeping only matches whose bracket_slot
// passes `keep`; drops any round left empty.
function splitBand(rounds, keep) {
  const out = [];
  for (const r of (Array.isArray(rounds) ? rounds : [])) {
    const matches = (Array.isArray(r.matches) ? r.matches : []).filter((m) => keep(String(m.bracket_slot || '')));
    if (matches.length) out.push({ round_index: r.round_index, label: r.label, matches });
  }
  return out;
}

// ---- swiss — a standings table (hero) + per-round pairings ---------

function renderSwiss(st, ctx, epochId) {
  const nodes = [];
  const standings = standingsTable(st, ctx, epochId, !!(st && st.live));
  const sCard = el('div', { class: 'dn-panel' });
  sCard.appendChild(standings || empty('No standings recorded yet.'));
  nodes.push(section('Standings · the Swiss leaderboard', sCard));

  const rounds = (st && Array.isArray(st.rounds)) ? st.rounds : [];
  const pCard = el('div', { class: 'dn-panel' });
  if (!rounds.length) {
    pCard.appendChild(empty('No rounds paired yet.'));
  } else {
    for (const r of rounds) {
      pCard.appendChild(el('div', { class: 'dt-swiss-round-h', text: r.label || `Round ${(r.round_index || 0) + 1}` }));
      const matches = Array.isArray(r.matches) ? r.matches : [];
      const tbl = el('table', { class: 'dn-board-table dt-swiss-pairings' });
      tbl.appendChild(el('thead', null, [el('tr', null, [
        el('th', { text: 'pairing' }), el('th', { text: 'winner' }), el('th', { class: 'dn-num', text: 'Δ scalar' }),
      ])]));
      const tbody = el('tbody');
      for (const m of matches) {
        const comps = Array.isArray(m.competitors) ? m.competitors : [];
        const delta = svg.isNum(m.delta_scalar) ? m.delta_scalar : null;
        tbody.appendChild(el('tr', null, [
          el('td', { class: 'dn-mono' }, [
            linkGen(comps[0], ctx, epochId), comps.length > 1 ? el('span', { class: 'dn-faint', text: ' vs ' }) : null, comps.length > 1 ? linkGen(comps[1], ctx, epochId) : null,
          ].filter(Boolean)),
          el('td', { class: 'dn-mono', text: m.winner || (m.bye ? 'bye' : '—') }),
          el('td', { class: 'dn-num dn-mono ' + (delta > 0 ? 'dn-bad-t' : delta < 0 ? 'dn-good-t' : ''), text: svg.isNum(delta) ? svg.fmtSigned(delta, 1) : '—' }),
        ]));
      }
      tbl.appendChild(tbody);
      pCard.appendChild(tbl);
    }
  }
  nodes.push(section('Pairings · round by round', pCard));
  return nodes;
}

// ---- racing — derive the rung/gate model from a normalized payload ─
//
// Both the Match-ups ladder (renderRacing, below) AND the epoch-overview
// survival funnel (views/epoch.js) need the SAME derived view of a racing
// `st`: the ordered rung list (field/survivors/cut/Δ/board-fraction +
// pending flag) and the resolved champion-gate (state + crowned id + Δ). This
// is the single source of that derivation so the funnel and the ladder never
// disagree. Returns null when `st` is not a racing payload.
//
//   → { rungs, championId, gateState, gateDelta, live, hasRungs }
//   gateState ∈ 'crowned' | 'stands' | 'deciding' | 'pending'.
export function racingModel(st) {
  if (!st || String(st.structure) !== 'racing') return null;
  const live = !!st.live;
  const rounds = Array.isArray(st.rounds) ? st.rounds : [];
  const lineage = Array.isArray(st.champion_lineage) ? st.champion_lineage.map(String) : [];
  const firstMatch = (r) => (r && Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : {};
  const gateRound = rounds.find((r) => String(firstMatch(r).match_id || '') === 'racing-final') || null;
  const rungRounds = rounds.filter((r) => String(firstMatch(r).match_id || '') !== 'racing-final');

  const rungs = rungRounds.map((r) => {
    const m = firstMatch(r);
    return {
      label: r.label || `Rung ${(r.round_index || 0) + 1}`,
      match_id: m.match_id,
      competitors: Array.isArray(m.competitors) ? m.competitors : [],
      survivors: Array.isArray(m.survivors) ? m.survivors : [],
      cut: Array.isArray(m.cut) ? m.cut : [],
      deltas: (m.deltas && typeof m.deltas === 'object') ? m.deltas : null,
      board_fraction: svg.isNum(m.board_fraction) ? m.board_fraction : null,
      pending: !(Array.isArray(m.survivors) && m.survivors.length) && !(Array.isArray(m.cut) && m.cut.length),
    };
  });

  const gateMatch = gateRound ? firstMatch(gateRound) : null;
  const finalRungSurvivors = (() => {
    for (let i = rungs.length - 1; i >= 0; i--) {
      if (rungs[i].survivors.length) return rungs[i].survivors.map(String);
    }
    return [];
  })();
  let championId = null;
  let gateState = live ? 'deciding' : (gateMatch ? 'settled' : 'pending');
  if (!live && gateMatch) {
    const decided = String(gateMatch.decision || '').toLowerCase() === 'promoted'
      || (gateMatch.winner && gateMatch.winner !== (gateMatch.competitors || [])[0]);
    const survivor = String(gateMatch.winner || '')
      || (Array.isArray(gateMatch.competitors) && gateMatch.competitors[1]) || null;
    if (decided && survivor) {
      championId = survivor;
    } else if (lineage.length) {
      championId = (survivor && lineage[lineage.length - 1] === survivor) ? survivor : null;
    }
    if (!championId && lineage.length && finalRungSurvivors.indexOf(lineage[lineage.length - 1]) >= 0) {
      championId = lineage[lineage.length - 1];
    }
    gateState = championId ? 'crowned' : 'stands';
  } else if (live && finalRungSurvivors.length === 1) {
    championId = finalRungSurvivors[0];
  }
  const gateDelta = (gateMatch && svg.isNum(gateMatch.delta_scalar)) ? gateMatch.delta_scalar : null;
  return { rungs, championId, gateState, gateDelta, live, hasRungs: rungs.length > 0 };
}

// ---- racing — a successive-halving rung ladder ---------------------

function renderRacing(st, ctx, epochId) {
  const nodes = [];
  const live = !!(st && st.live);
  const rounds = (st && Array.isArray(st.rounds)) ? st.rounds : [];
  const lineage = (st && Array.isArray(st.champion_lineage)) ? st.champion_lineage.map(String) : [];

  // Split the rounds into RUNG rounds (`rungN`) and the lone CHAMPION-GATE
  // round (`racing-final`). The gate is NOT a rung — it is the full-board
  // confirmation duel between the lone survivor and the champion; rendering it
  // as a rung would invent a phantom column. Both the reconstructed completed
  // record and the LIVE `/api/active-tournament` use this same shape.
  const firstMatch = (r) => (r && Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : {};
  const gateRound = rounds.find((r) => String(firstMatch(r).match_id || '') === 'racing-final') || null;
  const rungRounds = rounds.filter((r) => String(firstMatch(r).match_id || '') !== 'racing-final');

  // each racing rung round has ONE match whose competitors/survivors/cut carry
  // the rung; flatten to a rung list for the ladder mark.
  const rungs = rungRounds.map((r) => {
    const m = firstMatch(r);
    return {
      label: r.label || `Rung ${(r.round_index || 0) + 1}`,
      match_id: m.match_id,
      competitors: Array.isArray(m.competitors) ? m.competitors : [],
      survivors: Array.isArray(m.survivors) ? m.survivors : [],
      cut: Array.isArray(m.cut) ? m.cut : [],
      deltas: (m.deltas && typeof m.deltas === 'object') ? m.deltas : null,
      board_fraction: svg.isNum(m.board_fraction) ? m.board_fraction : null,
      // a rung with NO recorded survivors/cut yet is still in flight — the mark
      // shows its field as racing (neutral), never as eliminated.
      pending: !(Array.isArray(m.survivors) && m.survivors.length) && !(Array.isArray(m.cut) && m.cut.length),
    };
  });

  // The CHAMPION-GATE outcome.
  //   • settled (idle): the gate match's winner/decision crowns the survivor;
  //     `champion_lineage`'s last id confirms a promotion.
  //   • live: the gate has NOT committed — crown nobody; show the leading
  //     survivor of the final rung and let the gate read "deciding…".
  const gateMatch = gateRound ? firstMatch(gateRound) : null;
  const finalRungSurvivors = (() => {
    for (let i = rungs.length - 1; i >= 0; i--) {
      if (rungs[i].survivors.length) return rungs[i].survivors.map(String);
    }
    return [];
  })();
  let championId = null;
  let gateState = live ? 'deciding' : (gateMatch ? 'settled' : 'pending');
  if (!live && gateMatch) {
    const decided = String(gateMatch.decision || '').toLowerCase() === 'promoted'
      || (gateMatch.winner && gateMatch.winner !== (gateMatch.competitors || [])[0]);
    const survivor = String(gateMatch.winner || '')
      || (Array.isArray(gateMatch.competitors) && gateMatch.competitors[1]) || null;
    if (decided && survivor) {
      championId = survivor;
    } else if (lineage.length) {
      // a settled gate where the survivor was promoted: confirm via lineage.
      championId = (survivor && lineage[lineage.length - 1] === survivor) ? survivor : null;
    }
    if (!championId && lineage.length && finalRungSurvivors.indexOf(lineage[lineage.length - 1]) >= 0) {
      championId = lineage[lineage.length - 1];
    }
    gateState = championId ? 'crowned' : 'stands';
  } else if (live && finalRungSurvivors.length === 1) {
    // the leader heading into the (not-yet-committed) gate.
    championId = finalRungSurvivors[0];
  }
  const gateDelta = (gateMatch && svg.isNum(gateMatch.delta_scalar)) ? gateMatch.delta_scalar : null;
  const card = el('div', { class: 'dn-panel dn-figpane' });
  card.appendChild(rungs.length
    ? svg.racingLadder({
        rungs, championId, live, gateState, gateDelta,
        onCompetitor: (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); },
      })
    : empty(live ? 'The race is being seeded — the first rung fills in as runs land.' : 'No rungs evaluated yet.'));
  if (rungs.length) {
    const gateNote = gateState === 'crowned' ? ` · champion-gate: ${championId} promoted ♚`
      : gateState === 'stands' ? ' · champion-gate: champion stands'
      : gateState === 'deciding' ? ' · champion-gate: deciding…'
      : '';
    card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      'each rung races the field on a fraction of the board, then cuts the worst by η · ✕ = cut · ↑ = survives · ♚ = champion-gate winner · click a competitor → open'
      + gateNote
      + (live ? ' · LIVE — the eventual winner is not committed until the final gate' : '') }));
  }
  nodes.push(section(live ? 'Racing ladder · LIVE — rungs, board fractions, cuts' : 'Racing ladder · rungs, board fractions, cuts', card));
  const standings = standingsTable(st, ctx, epochId, live);
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// ---- shared: the standings leaderboard table -----------------------

function standingsTable(st, ctx, epochId, live) {
  const standings = (st && Array.isArray(st.standings)) ? st.standings.slice() : [];
  if (!standings.length) return null;
  standings.sort((a, b) => (svg.isNum(a.rank) ? a.rank : 1e9) - (svg.isNum(b.rank) ? b.rank : 1e9));
  const tbl = el('table', { class: 'dn-board-table dt-standings' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'rank' }), el('th', { text: 'generation' }), el('th', { text: 'status' }),
    el('th', { class: 'dn-num', text: 'scalar' }), el('th', { class: 'dn-num', text: 'W' }), el('th', { class: 'dn-num', text: 'L' }), el('th', { text: '' }),
  ])]));
  const tbody = el('tbody');
  for (const s of standings) {
    let status = String(s.status || '').toLowerCase();
    // LIVE — the verdicts have not committed; a standing tagged champion /
    // eliminated mid-run is the EVENTUAL outcome read from a half-finished
    // record. Treat everyone as still racing so nobody is mislabeled.
    if (live && (status === 'champion' || status === 'eliminated')) status = 'racing';
    const rowCls = status === 'champion' ? 'dn-board-champ' : status === 'eliminated' ? 'dt-standings-out' : '';
    tbody.appendChild(el('tr', { class: rowCls }, [
      el('td', { class: 'dn-mono', text: svg.isNum(s.rank) ? String(s.rank) : '—' }),
      el('td', { class: 'dn-mono', text: (s.generation_id || '—') + (status === 'champion' ? ' ♛' : '') }),
      el('td', null, [statusPill(status)]),
      el('td', { class: 'dn-num dn-mono', text: svg.isNum(s.scalar) ? svg.fmt(s.scalar, 1) : '—' }),
      el('td', { class: 'dn-num dn-mono', text: svg.isNum(s.wins) ? String(s.wins) : '—' }),
      el('td', { class: 'dn-num dn-mono', text: svg.isNum(s.losses) ? String(s.losses) : '—' }),
      el('td', null, [s.generation_id ? el('a', { class: 'dn-linkbtn', href: ctx.href('candidate', { epochId, gen: s.generation_id }), text: 'open →' }) : null].filter(Boolean)),
    ]));
  }
  tbl.appendChild(tbody);
  return tbl;
}

function statusPill(status) {
  const s = status || 'alive';
  // map the standings vocabulary onto verdict-pill semantics so the pill
  // reads in every theme: champion→promoted, eliminated→rejected, else→deferred
  // (alive / racing — still in contention).
  const verdict = s === 'champion' ? 'promoted' : s === 'eliminated' ? 'rejected' : 'deferred';
  const pill = verdictPill(verdict);
  pill.textContent = s;
  return pill;
}

function linkGen(gen, ctx, epochId) {
  if (!gen) return el('span', { class: 'dn-faint', text: 'bye' });
  return el('a', { class: 'dn-linkbtn dn-mono', href: ctx.href('candidate', { epochId, gen }), text: String(gen) });
}
