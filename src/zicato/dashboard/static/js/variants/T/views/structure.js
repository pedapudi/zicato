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
  // SCOPE TO THE VIEWED EPOCH. /api/tournaments can carry records from MORE than
  // the epoch on screen (the workspace's whole history), so we MUST drop any
  // record that does not belong to `epochId` — otherwise a prior epoch's
  // COMPLETED racing ladder reconstructs and renders under the current epoch's
  // header (e.g. e0's survival funnel shown while e1 is still proposing). A
  // record names its epoch via an explicit `epoch_id`, or as the prefix of its
  // tournament_id (`<epoch>:<champ>-><chall>` or `tourn_<epoch>_<gen>`). When a
  // record carries NO epoch signal at all we keep it (legacy single-epoch
  // payloads), but a record whose epoch is KNOWN and DIFFERENT is always dropped.
  const inEpoch = (t) => {
    if (epochId == null) return true;
    const want = String(epochId);
    if (t && t.epoch_id != null) return String(t.epoch_id) === want;
    const tid = String((t && t.tournament_id) || '');
    if (tid.indexOf(want) >= 0) return true;
    // a record whose tournament_id plainly names a DIFFERENT epoch is excluded;
    // one with no recognisable epoch token is kept (cannot prove it is foreign).
    return !/(^tourn_|:)/.test(tid);
  };
  const racing = all.filter((t) => t && String(t.structure) === 'racing'
    && Array.isArray(t.rounds) && t.rounds.length && inEpoch(t));
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
      (Array.isArray(r.matches) ? r.matches : []).map((m) => [m.match_id, (m.competitors || []).join('/'), m.winner, m.decision, m.bracket_slot, m.bye, m.survivors && m.survivors.join('/'), m.cut && m.cut.join('/'),
        // progressive LIVE racing fields: the queued flag + each lane's
        // boards-done / in-flight count / partial Δ. Including these makes the
        // gated swap fire when a board lands (real progress) but stay STABLE on a
        // no-op heartbeat that changes nothing (same counts ⇒ same digest).
        m.queued ? 'Q' : '',
        m.live_progress ? Object.keys(m.live_progress).sort().map((g) => {
          const p = m.live_progress[g];
          return g + ':' + (p.done || 0) + '/' + (p.total == null ? '?' : p.total) + ':' + (p.inflight || 0)
            + (svg.isNum(p.partialDelta) ? ':' + p.partialDelta.toFixed(2) : '');
        }).join(',') : '',
      ]),
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

  // THE BENCHMARK (champion v0) the field is raced against. Every rung Δ is
  // Δ-vs-this-id, and this id defends at the champion-gate — so the funnel /
  // ladder can show it as a persistent reference even though it is NOT one of
  // the rung competitors. It is the gate's champion seat (competitors[0]), else
  // the seed competitor common to every rung, else the first promoted lineage
  // entry. Distinct from `championId`, which is the eventual SURVIVOR/crowned id.
  let benchmarkId = null;
  if (gateMatch && Array.isArray(gateMatch.competitors) && gateMatch.competitors.length) {
    benchmarkId = String(gateMatch.competitors[0]);
  }
  if (!benchmarkId && rungs.length) {
    // the id present in EVERY rung's competitors (the seed champion).
    const sets = rungs.map((r) => new Set((r.competitors || []).map(String)));
    const common = [...sets[0]].filter((c) => sets.every((s) => s.has(c)));
    if (common.length === 1) benchmarkId = common[0];
    else if (common.length > 1 && lineage.length) {
      benchmarkId = common.find((c) => c === lineage[0]) || null;
    }
  }
  if (!benchmarkId && lineage.length) benchmarkId = lineage[0];

  return { rungs, championId, benchmarkId, gateState, gateDelta, live, hasRungs: rungs.length > 0 };
}

// ── BUILD a PROGRESSIVE live racing model ───────────────────────────
//
// THE BUG this fixes: during an IN-FLIGHT racing run the live
// /api/active-tournament `rounds` array is EMPTY until each matchup COMPLETES,
// so the plain normalizeStructure() produced no rungs and renderRacing() sat on
// the "the race is being seeded" empty state for the whole race — never filling
// progressively, then only showing results after completion.
//
// This builds an ACCUMULATING racing `st` from the UNION of every live signal,
// so the ladder fills rung-by-rung WHILE the race runs and never discards a
// completed rung:
//
//   • FIELD — `competitors` (champion + challenger lanes), available from the
//     very start. The challengers are the rung-0 field; the champion is the
//     persistent benchmark (NOT a rung competitor).
//   • COMPLETED rungs — any `rounds` the active-tournament has committed are
//     carried VERBATIM (survivors ↑ / cuts ✗), so a finished rung persists when
//     the next rung starts.
//   • ACTIVE rung — derived from the heartbeat `phase` (`…:rung0_m3` → rung 0).
//     If `rounds` has not yet recorded that rung, we SYNTHESISE a pending rung
//     whose field is the survivors of the previous completed rung (or the whole
//     challenger field at rung 0). Its per-lane board progress is driven by
//     /api/active-runs filtered to THIS epoch's gens (attributed to the active
//     rung since active-runs' own `rung`/`match_id` are usually null mid-phase),
//     and a partial Δ-vs-champion from partial_challenger_agg−partial_champion_agg.
//   • QUEUED future rungs — the remaining successive-halving rungs (from
//     structure_params field_size/eta) shown as `queued` so the whole shape is
//     legible from the start.
//
// The result is the SAME normalized {structure, competitors, rounds, …} shape
// the renderers already consume — renderRacing()/racingModel() handle it without
// special-casing. Each synthesised rung match carries a `progress` map
// (gen → {done,total,inflight,partialDelta}) + `queued` flag the ladder reads.
// Returns null when `at` is not a live racing payload.
//
//   at         — the raw /api/active-tournament object.
//   heartbeat  — /api/heartbeat ({ phase, generation_id, … }).
//   activeRuns — /api/active-runs (in-flight board units).
//   epochGens  — the set/array of generation ids that belong to THIS epoch
//                (so foreign in-flight runs are not attributed to this race).
export function buildLiveRacingModel({ at, heartbeat, activeRuns, epochGens } = {}) {
  if (!at || typeof at !== 'object' || String(at.structure) !== 'racing') return null;
  const competitors = Array.isArray(at.competitors) ? at.competitors : [];
  const rawRounds = Array.isArray(at.rounds) ? at.rounds : [];
  const params = (at.structure_params && typeof at.structure_params === 'object')
    ? at.structure_params : (at.params && typeof at.params === 'object' ? at.params : {});

  // the field: challengers race; the champion is the benchmark seat (not a rung
  // runner). Roles come from `competitors[].role`; fall back to seed order
  // (seed 1 / first = champion) when roles are absent.
  const championComp = competitors.find((c) => String(c.role || '').toLowerCase() === 'champion')
    || (competitors.length ? competitors.slice().sort((a, b) => (svg.isNum(a.seed) ? a.seed : 1e9) - (svg.isNum(b.seed) ? b.seed : 1e9))[0] : null);
  const championId = championComp ? String(championComp.generation_id) : null;
  const challengerIds = competitors
    .filter((c) => c !== championComp && c.generation_id != null)
    .map((c) => String(c.generation_id));

  // No field AND no rounds yet → nothing to show progressively (let the caller
  // fall through to the honest "starting" placeholder).
  if (!challengerIds.length && !rawRounds.length) return null;

  const firstMatch = (r) => (r && Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : {};
  const rungIndexOf = (mid) => { const m = /^rung(\d+)/.exec(String(mid || '')); return m ? Number(m[1]) : null; };
  const isFinal = (mid) => String(mid || '') === 'racing-final';

  // index the COMPLETED rounds the active-tournament already recorded.
  const completedRungs = new Map();  // rungIdx → round (verbatim)
  let gateRound = null;
  for (const r of rawRounds) {
    const mid = firstMatch(r).match_id;
    if (isFinal(mid)) { gateRound = r; continue; }
    const ri = rungIndexOf(mid);
    if (ri != null) completedRungs.set(ri, r);
  }

  // the ACTIVE rung index from the heartbeat phase (`…:rung2_m1` → 2).
  const phase = String((heartbeat && heartbeat.phase) || at.phase || '');
  let activeRung = null;
  { const m = /rung(\d+)/.exec(phase); if (m) activeRung = Number(m[1]); }
  // when the phase names no rung but a rounds index is present, fall to it.
  if (activeRung == null && svg.isNum(at.round_index)) activeRung = at.round_index;

  // the in-flight board units that belong to THIS epoch's gens. active-runs'
  // own rung/match_id are usually null mid-phase, so we attribute them all to
  // the heartbeat's active rung.
  const genSet = epochGens ? new Set([...epochGens].map(String)) : null;
  const runs = (Array.isArray(activeRuns) ? activeRuns : []).filter((r) => {
    const g = String(r.generation_id || r.gen || '');
    if (!g) return false;
    return genSet ? genSet.has(g) : true;
  });
  // per-gen in-flight progress (boards executing right now).
  const inflightByGen = new Map();   // gen → { count, sumProgress }
  for (const r of runs) {
    const g = String(r.generation_id || r.gen);
    const p = svg.isNum(r.progress) ? r.progress : 0;
    const cur = inflightByGen.get(g) || { count: 0, sumProgress: 0 };
    cur.count += 1; cur.sumProgress += p;
    inflightByGen.set(g, cur);
  }

  // partial aggregate Δ-vs-champion (challenger − champion) as boards land.
  const champAgg = svg.isNum(at.partial_champion_agg) ? at.partial_champion_agg : null;
  const challAgg = svg.isNum(at.partial_challenger_agg) ? at.partial_challenger_agg : null;
  const partialDelta = (challAgg != null && champAgg != null) ? (challAgg - champAgg) : null;

  // the per-rung board fraction (successive halving): rung N covers
  // min(1, base·η^N). Honour structure_params.rungs[] fractions when given.
  const eta = svg.isNum(params.eta) && params.eta >= 2 ? params.eta : 2;
  const baseFrac = svg.isNum(params.board_fraction) && params.board_fraction > 0 ? params.board_fraction : null;
  const rungsParam = Array.isArray(params.rungs) ? params.rungs : null;
  const fracFor = (ri) => {
    if (rungsParam && rungsParam[ri] && svg.isNum(rungsParam[ri].fraction)) return rungsParam[ri].fraction;
    return baseFrac == null ? null : Math.min(1, baseFrac * Math.pow(eta, ri));
  };
  // total board units per rung for the k/N progress label, if the contract
  // pins board_size; else null (the label degrades to just the in-flight count).
  const boardSize = svg.isNum(params.board_size) ? params.board_size
    : (svg.isNum(at.board_size) ? at.board_size : null);
  const totalFor = (ri) => {
    const f = fracFor(ri);
    if (boardSize != null && f != null) return Math.max(1, Math.round(boardSize * f));
    if (boardSize != null) return boardSize;
    return null;
  };

  // how many rungs does the successive halving have? from explicit rungs[] or
  // ceil(log_η(field_size)). At minimum, cover the active rung + every completed.
  const fieldSize = svg.isNum(params.field_size) ? params.field_size : challengerIds.length;
  let totalRungs = rungsParam ? rungsParam.length
    : (svg.isNum(at.total_rounds) ? at.total_rounds
      : (fieldSize > 1 ? Math.ceil(Math.log(fieldSize) / Math.log(eta)) : 1));
  totalRungs = Math.max(totalRungs, (activeRung != null ? activeRung + 1 : 0),
    completedRungs.size ? Math.max(...completedRungs.keys()) + 1 : 0, 1);

  // walk rungs 0..totalRungs-1, deriving each rung's field from the survivors of
  // the previous COMPLETED rung (so the field narrows by η as cuts land). The
  // active rung gets live board progress; rungs after the active one are queued.
  const rounds = [];
  let runningField = challengerIds.slice();   // the field entering the current rung
  for (let ri = 0; ri < totalRungs; ri++) {
    if (completedRungs.has(ri)) {
      // carry the committed rung VERBATIM — survivors/cuts persist untouched.
      const r = completedRungs.get(ri);
      const m = firstMatch(r);
      rounds.push(r);
      runningField = Array.isArray(m.survivors) && m.survivors.length
        ? m.survivors.map(String) : runningField;
      continue;
    }
    const field = runningField.slice();
    if (!field.length) break;
    const isActive = (activeRung != null) ? ri === activeRung
      : (rounds.every((rr) => completedRungs.has(rr.round_index)) ? ri === completedRungs.size : false);
    const queued = !isActive;
    const frac = fracFor(ri);
    const total = totalFor(ri);
    // per-lane live progress for the ACTIVE rung.
    const progress = {};
    if (isActive) {
      for (const g of field) {
        const inf = inflightByGen.get(g);
        if (inf) {
          const done = Math.max(0, Math.floor(inf.sumProgress));
          progress[g] = {
            inflight: inf.count,
            done,
            total: total,
            partialDelta: partialDelta,
          };
        } else {
          progress[g] = { inflight: 0, done: 0, total: total, partialDelta: null };
        }
      }
    }
    rounds.push({
      round_index: ri,
      label: `Rung ${ri}`,
      matches: [{
        match_id: `rung${ri}`,
        competitors: field,
        survivors: [],
        cut: [],
        board_fraction: frac,
        // progressive live fields (the ladder reads these; the completed-record
        // path leaves them undefined and renders as before):
        live_progress: isActive ? progress : null,
        queued,
      }],
    });
    // a queued/active rung has no committed survivors; the next rung's field is
    // the η-cut of THIS field (best-effort: keep the whole field — the real cut
    // lands when the rung completes and replaces this synthesised rung).
    if (!isActive && !queued) runningField = field;
  }

  // the champion gate — carry a committed `racing-final` verbatim; else a
  // pending gate the renderer reads as "deciding…" (NEVER "rejected" while live).
  if (gateRound) {
    rounds.push(gateRound);
  } else {
    rounds.push({
      round_index: totalRungs,
      label: 'Champion gate',
      matches: [{ match_id: 'racing-final', competitors: [championId].filter(Boolean), board_fraction: 1.0 }],
    });
  }

  return normalizeStructure({
    structure: 'racing',
    structure_params: params,
    competitors,
    rounds,
    standings: Array.isArray(at.standings) ? at.standings : [],
    champion_lineage: Array.isArray(at.champion_lineage) ? at.champion_lineage : [],
    phase: at.phase != null ? at.phase : (heartbeat && heartbeat.phase) || 'running',
    source: 'live',
  }, true);
}

// ── ONE candidate's PATH through the racing tournament ──────────────
//
// The lifecycle DAG (views/candidate.js) shows a small rung-progression strip
// relating a candidate's board runs to the tournament rounds even when the
// per-run records carry no rung tags. This derives that path for ONE candidate
// (genId) from the SAME per-challenger /api/tournaments records the racing
// ladder reconstructs from: rung 0 → rung 1 → racing-final, each with the
// candidate's Δ-vs-champion at that rung and a won/cut/promoted/rejected
// verdict. Returns null when the candidate ran no racing matches (gauntlet, or
// a candidate that never raced) — the strip is then suppressed.
//
//   → { stages:[{ label, kind:'rung'|'final', delta, verdict }] } | null
export function candidateProgression(brk, genId) {
  if (!brk || typeof brk !== 'object' || genId == null) return null;
  const id = String(genId);
  const all = Array.isArray(brk.tournaments) ? brk.tournaments : [];
  // find THIS candidate's per-challenger racing record (challenger == genId).
  const challengerOf = (t) => {
    const tid = String(t.tournament_id || '');
    const arrow = tid.lastIndexOf('->');
    if (arrow >= 0) return tid.slice(arrow + 2);
    const comps = Array.isArray(t.competitors) ? t.competitors.map(String) : [];
    return comps.length > 1 ? comps[1] : (comps[0] || null);
  };
  const rec = all.find((t) => t && String(t.structure) === 'racing'
    && Array.isArray(t.rounds) && t.rounds.length && challengerOf(t) === id);
  if (!rec) return null;

  const rungIndexOf = (mid) => { const m = /^rung(\d+)/.exec(String(mid || '')); return m ? Number(m[1]) : null; };
  const isFinal = (mid) => String(mid || '') === 'racing-final';
  const lineage = Array.isArray(brk.champion_lineage) ? brk.champion_lineage.map(String) : [];
  const crowned = lineage.length ? lineage[lineage.length - 1] : null;

  const rungs = [];
  let final = null;
  for (const r of rec.rounds) {
    const mid = r && r.match_id;
    const delta = svg.isNum(r && r.delta_scalar) ? r.delta_scalar : null;
    if (isFinal(mid)) { final = { delta, won: !!(r && r.won) }; continue; }
    const ri = rungIndexOf(mid);
    if (ri == null) continue;
    rungs.push({ ri, delta, won: !!(r && r.won) });
  }
  rungs.sort((a, b) => a.ri - b.ri);
  if (!rungs.length && !final) return null;

  const stages = [];
  rungs.forEach((rg, k) => {
    // a rung is "survived" when there is a later rung or a final; else "cut".
    const survived = k < rungs.length - 1 || !!final;
    stages.push({ label: `rung ${rg.ri}`, kind: 'rung', delta: rg.delta, verdict: survived ? 'survived' : 'cut' });
  });
  if (final) {
    const promoted = final.won || (crowned === id);
    stages.push({ label: 'final', kind: 'final', delta: final.delta, verdict: promoted ? 'promoted' : 'rejected' });
  } else if (rungs.length) {
    // no final reached → the candidate was cut at its last rung.
    stages[stages.length - 1].verdict = 'cut';
  }
  return { stages };
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
      // progressive LIVE fields (present only for the active/queued rungs of a
      // live race) — the ladder shows each lane's "racing · k/N boards" + a
      // partial Δ; queued rungs read "queued".
      live_progress: (m.live_progress && typeof m.live_progress === 'object') ? m.live_progress : null,
      queued: !!m.queued,
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
  // the champion v0 the field is raced against (the persistent benchmark line) —
  // distinct from championId (the eventual survivor). Reuse racingModel's
  // derivation so the ladder + funnel agree on the benchmark id.
  const benchmarkId = (racingModel(st) || {}).benchmarkId || null;
  const card = el('div', { class: 'dn-panel dn-figpane' });
  card.appendChild(rungs.length
    ? svg.racingLadder({
        rungs, championId, benchmarkId, live, gateState, gateDelta,
        onCompetitor: (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); },
      })
    : empty(live ? 'The race is being seeded — the first rung fills in as runs land.' : 'No rungs evaluated yet.'));
  if (rungs.length) {
    const gateNote = gateState === 'crowned' ? ` · champion-gate: ${championId} promoted ♚`
      : gateState === 'stands' ? ' · champion-gate: champion stands'
      : gateState === 'deciding' ? ' · champion-gate: deciding…'
      : '';
    card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      (benchmarkId ? `the field is raced vs the champion v0 = ${benchmarkId}; every rung Δ is Δ-vs-v0 and v0 defends at the champion-gate · ` : '')
      + 'each rung races the field on a fraction of the board, then cuts the worst by η · ✕ = cut · ↑ = survives · ♚ = champion-gate winner · click a competitor → open'
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
