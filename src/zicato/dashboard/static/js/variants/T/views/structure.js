// variants/T/views/structure.js — the configured tournament STRUCTURE.
//
// The match-ups page (views/gens.js) renders the gauntlet ladder for the
// (width:100% + viewBox, no pan/zoom, token-themed, page-scale aware).

import { el } from '../../../core/dom.js';
import * as svg from '../svg.js';
import { section, empty, verdictPill } from '../ui.js';
import { structureStatusLabel } from '../livestatus.js';
const CROWN = svg.CROWN;

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

// ── shared LIVE-AWARENESS helpers (epoch-scoped, pure) ────────────────
//
// The candidate page + board trellis must reflect IN-FLIGHT board runs (which
// candidate×board is running now), but ONLY for the ACTIVE epoch — a live run
// in e1 must not light up e0's static cells. These helpers are pure (they take
// the raw live signals, never AppState) so they unit-test without a DOM, and
// they mirror the per-epoch guard views/gens.js + views/epoch.js already use.

// Does the live run (active tournament / heartbeat) belong to the VIEWED epoch?
// Keyed off the active-tournament's epoch_id, falling back to the heartbeat's.
// When NEITHER live signal carries an epoch tag it is a legacy single-epoch
// payload, so we trust it for the viewed epoch.
export function liveBelongsToEpoch(epochId, { heartbeat, activeTournament } = {}) {
  if (epochId == null) return false;
  const atEpoch = (activeTournament && activeTournament.epoch_id != null) ? activeTournament.epoch_id : null;
  const hbEpoch = (heartbeat && heartbeat.epoch_id != null) ? heartbeat.epoch_id : null;
  if (atEpoch != null) return String(atEpoch) === String(epochId);
  if (hbEpoch != null) return String(hbEpoch) === String(epochId);
  return true;
}

// The in-flight board-units scoped to the ACTIVE epoch — [] when the live run
// (if any) does not belong to the viewed epoch, so a foreign-epoch run never
// leaks in. `running` gates it: an idle workspace yields no in-flight set.
export function inflightForActiveEpoch(activeRuns, { heartbeat, activeTournament, running, epochId } = {}) {
  if (!running) return [];
  if (!liveBelongsToEpoch(epochId, { heartbeat, activeTournament })) return [];
  return Array.isArray(activeRuns) ? activeRuns.filter((r) => r && typeof r === 'object') : [];
}

// Pull the board-entry id off an in-flight run record (payloads vary).
function runEntryId(r) {
  if (!r || typeof r !== 'object') return null;
  return r.entry_id != null ? r.entry_id : (r.board_entry_id != null ? r.board_entry_id : (r.entry != null ? r.entry : null));
}
// Pull the generation id off an in-flight run record.
function runGenId(r) {
  if (!r || typeof r !== 'object') return null;
  return r.generation_id != null ? r.generation_id : (r.gen != null ? r.gen : null);
}

// Filter in-flight runs to a single candidate×board cell (gen + entry). Either
// key may be omitted to filter on just the other.
export function inflightForEntryGen(runs, entryId, genId) {
  const list = Array.isArray(runs) ? runs : [];
  return list.filter((r) => {
    if (entryId != null && runEntryId(r) !== entryId) return false;
    if (genId != null && String(runGenId(r)) !== String(genId)) return false;
    return true;
  });
}

// A normalised 0..1 progress ratio for an in-flight run (some payloads send
// 0..100 — clamp + normalise; fall back to elapsed/budget).
export function runProgressRatio(r) {
  let p = r && (r.progress != null ? r.progress : r.fraction);
  if (!svg.isNum(p)) {
    if (r && svg.isNum(r.elapsed_seconds) && svg.isNum(r.budget_seconds) && r.budget_seconds > 0) {
      p = r.elapsed_seconds / r.budget_seconds;
    } else return null;
  }
  if (p > 1) p = p / 100;
  if (p < 0) p = 0;
  if (p > 1) p = 1;
  return p;
}

// Normalize EITHER the LIVE /api/active-tournament OR the COMPLETED
// /api/tournament-structure into ONE renderer input. `live` ⇒ the payload came
// from active-tournament with a non-idle phase.
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
    // the per-challenger proposing-step outcomes (applied/rejected + reason),
    // carried through so the "Proposed field" section + the live tracker can
    // read them from a normalized structure too.
    field_status: Array.isArray(st.field_status) ? st.field_status : [],
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
// carries no racing records.
export function reconstructRacing(brk, epochId) {
  if (!brk || typeof brk !== 'object') return null;
  const all = Array.isArray(brk.tournaments) ? brk.tournaments : [];
  // SCOPE TO THE VIEWED EPOCH — /api/tournaments spans the whole workspace, so
  // drop any record whose epoch is KNOWN and DIFFERENT from `epochId`.
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

  // FAST PATH — an ASSEMBLED record whose rounds already hold the rung field
  // ({competitors, survivors, cut}); synthesise the gate from lineage if absent.
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

  // per-rung board fraction: rung N covers min(1, base · η^N) of the board.
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
        // progressive LIVE per-match fields (queued / board progress / partial Δ)
        // so the gated swap fires on real progress but stays stable on a heartbeat.
        m.queued ? 'Q' : '',
        (svg.isNum(m.done) || svg.isNum(m.total) || svg.isNum(m.inflight) || m.pending)
          ? 'P' + (m.done || 0) + '/' + (m.total == null ? '?' : m.total) + ':' + (m.inflight || 0) + (m.pending ? ':p' : '')
          : '',
        m.live_progress ? Object.keys(m.live_progress).sort().map((g) => {
          const p = m.live_progress[g];
          return g + ':' + (p.done || 0) + '/' + (p.total == null ? '?' : p.total) + ':' + (p.inflight || 0)
            + (svg.isNum(p.partialDelta) ? ':' + p.partialDelta.toFixed(2) : '');
        }).join(',') : '',
      ]),
    ]),
    standings: (Array.isArray(st.standings) ? st.standings : []).map((s) => [s.generation_id, s.rank, s.scalar, s.wins, s.losses, s.status]),
    // the proposing-step field — so the "Proposed field" section's gated
    // swap fires when a challenger is minted / applied / rejected, but stays
    // stable on a no-op heartbeat.
    field_status: (Array.isArray(st.field_status) ? st.field_status : []).map((f) => [f && f.generation_id, f && f.status]),
    source: st.source,
  });
}

// ── the structure render dispatch — DOM sections per structure ──────
export function renderStructure(st, ctx, epochId) {
  const structure = String((st && st.structure) || 'gauntlet');
  let nodes;
  if (structure === 'swiss') nodes = renderSwiss(st, ctx, epochId);
  else if (structure === 'racing') nodes = renderRacing(st, ctx, epochId);
  // single_elim + double_elim share the bracket renderer.
  else nodes = renderBracket(st, ctx, epochId, structure);
  // The PROPOSED FIELD section leads so a completed epoch's proposing
  // outcomes are visible (e.g. "4 proposed · 0 applied — all rejected" with
  // per-challenger reasons). Absent field_status ⇒ no section (back-compat).
  const proposed = proposedFieldSection(st, ctx, epochId);
  return proposed ? [proposed, ...nodes] : nodes;
}

// Read the per-challenger proposing outcomes (the v5 `field_status`) off a
// tournament-structure payload — same shape data.fieldStatus() produces, [] if absent.
export function fieldStatusOf(st) {
  const fs = st && st.field_status;
  if (!Array.isArray(fs)) return [];
  const out = [];
  for (const f of fs) {
    if (!f || typeof f !== 'object') continue;
    const gid = f.generation_id;
    if (gid == null || gid === '') continue;
    out.push({
      generation_id: String(gid),
      status: f.status === 'applied' ? 'applied' : 'rejected',
      reason: f.reason == null ? '' : String(f.reason),
      seed: (typeof f.seed === 'number') ? f.seed : null,
    });
  }
  return out;
}

// The "Proposed field" section — the candidate-generation step rendered via the
// shared proposingTracker (applied rows drill in; rejected rows show the reason).
function proposedFieldSection(st, ctx, epochId) {
  const fs = fieldStatusOf(st);
  if (!fs.length) return null;
  const live = !!(st && st.live);
  const applied = fs.filter((f) => f.status === 'applied').length;
  const rejected = fs.length - applied;
  // The proposing tracker earns its own section only when it has something to
  // SAY: LIVE (proposals applying/rejecting in real time — the count + per-row
  // states update as the field mints) or a COMPLETED run WITH REJECTIONS to
  // triage (which proposals failed to apply, and why). A completed, all-applied
  // field is already shown by the ladder/standings + the "field of N" pill, so a
  // lone "N proposed · N applied" line just reads as an empty section — omit it.
  if (!live && rejected === 0) return null;
  const onCompetitor = (gen) => { if (gen && ctx && ctx.navigate) ctx.navigate('candidate', { epochId, gen }); };
  const tracker = svg.proposingTracker({ fieldStatus: fs, onCompetitor });
  return section(live ? 'Proposed field · LIVE' : 'Proposed field', tracker);
}

// single/double elim — the bracket model: winners' band, optional losers' band,
// champion-gate state + benchmark (a winner that fails the gate → 'stands').
export function elimModel(st) {
  if (!st || (String(st.structure) !== 'single_elim' && String(st.structure) !== 'double_elim')) return null;
  const live = !!st.live;
  const isDouble = String(st.structure) === 'double_elim';
  const rounds = (Array.isArray(st.rounds) ? st.rounds : []);
  const lineage = Array.isArray(st.champion_lineage) ? st.champion_lineage.map(String) : [];
  // a match carries its progressive live fields (done/total/inflight/queued) so
  // the bracket tree can fill in board-by-board; map every match through verbatim.
  const winners = splitBand(rounds, (slot) => !slot.startsWith('LB'));
  const losers = isDouble ? splitBand(rounds, (slot) => slot.startsWith('LB')) : null;

  // the bracket champion: the winner of the LAST winners'/grand-final match.
  const wbFinal = winners.length ? winners[winners.length - 1] : null;
  const finalMatch = wbFinal && Array.isArray(wbFinal.matches) && wbFinal.matches.length
    ? wbFinal.matches[wbFinal.matches.length - 1] : null;
  // the incumbent (benchmark) the bracket winner must beat at the gate.
  let benchmarkId = null;
  const champComp = (Array.isArray(st.competitors) ? st.competitors : []).find((c) => String(c.role || '').toLowerCase() === 'champion');
  if (champComp && champComp.generation_id != null) benchmarkId = String(champComp.generation_id);
  if (!benchmarkId && lineage.length) benchmarkId = lineage[0];

  let championId = null;
  let gateState = live ? 'deciding' : (finalMatch && finalMatch.winner ? 'settled' : 'pending');
  if (!live && finalMatch && finalMatch.winner) {
    const winner = String(finalMatch.winner);
    const promoted = String(finalMatch.decision || '').toLowerCase() === 'promoted'
      || (lineage.length && lineage[lineage.length - 1] === winner && winner !== benchmarkId);
    if (promoted && winner !== benchmarkId) { championId = winner; gateState = 'crowned'; }
    else gateState = 'stands';
  }
  const gateDelta = (finalMatch && svg.isNum(finalMatch.delta_scalar)) ? finalMatch.delta_scalar : null;
  const hasMatches = winners.some((r) => r.matches.length) || (losers && losers.some((r) => r.matches.length));
  return { winners, losers, championId, benchmarkId, gateState, gateDelta, live, hasMatches };
}

function renderBracket(st, ctx, epochId, structure) {
  const model = elimModel(st) || { winners: splitBand((st && st.rounds) || [], () => true), losers: null, live: !!(st && st.live) };
  const open = (m) => {
    // open the (decided) winner's candidate, else the first competitor.
    const gen = m.winner || (Array.isArray(m.competitors) && m.competitors[0]) || null;
    if (gen) ctx.navigate('candidate', { epochId, gen });
  };
  const openGen = (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); };
  const nodes = [];
  const card = el('div', { class: 'dn-panel dn-figpane' });
  card.appendChild(model.hasMatches !== false && model.winners.length
    ? svg.elimBracket({
        winners: model.winners, losers: model.losers,
        championId: model.championId, benchmarkId: model.benchmarkId,
        live: model.live, gateState: model.gateState, gateDelta: model.gateDelta,
        onMatch: open, onCompetitor: openGen,
      })
    : empty(model.live ? 'The bracket is being seeded — matches fill in as runs land.' : 'No bracket rounds recorded yet.'));
  if (model.winners.length) {
    const gateNote = model.gateState === 'crowned' ? ` · champion-gate: ${model.championId} promoted ${CROWN.current}`
      : model.gateState === 'stands' ? ' · champion-gate: champion stands'
      : model.gateState === 'deciding' ? ' · champion-gate: deciding…' : '';
    card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      'winners advance right; ✦ = match winner · the bracket winner must beat the incumbent at the champion-gate ' + CROWN.current
      + (model.losers && model.losers.length ? ' · the losers’ bracket gives a second life (double-elim)' : '')
      + gateNote
      + (model.live ? ' · LIVE — the winner is not committed until the final gate' : '') }));
  }
  nodes.push(section(structure === 'double_elim'
    ? (model.live ? 'Bracket · LIVE — winners’ + losers’ tree' : 'Bracket · winners’ + losers’ tree')
    : (model.live ? 'Bracket · LIVE — click a match to open the candidate' : 'Bracket · click a match to open the candidate'), card));

  // the COMPANION generations-across-rounds FLOW (Task 3) — the elim analogue of
  // the racing survival funnel. Shown ALONGSIDE the bracket (not a toggle):
  // rounds as columns, one lane per generation, each line continuing while it
  // advances + terminating with ✕ when cut; the champion's lane reaches the gate
  // crowned. Derived purely from the SAME elimModel (single source). Only when
  // the bracket has matches to flow.
  const isElim = structure === 'single_elim' || structure === 'double_elim';
  if (isElim && model.hasMatches !== false && Array.isArray(model.winners) && model.winners.length) {
    const flowCard = el('div', { class: 'dn-panel dn-figpane' });
    flowCard.appendChild(svg.elimFlow({
      winners: model.winners, championId: model.championId, benchmarkId: model.benchmarkId,
      gateState: model.gateState, live: model.live, onCompetitor: openGen,
    }));
    flowCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      'each lane is a generation; its line advances right while it wins and ends with ✕ when eliminated — the champion’s lane reaches the gate ' + CROWN.current
      + (model.benchmarkId ? ' · ' + CROWN.former + ' = displaced incumbent' : '')
      + (model.live ? ' · LIVE — in-flight legs are dashed' : '') }));
    nodes.push(section(model.live ? 'Generations across rounds · LIVE — survival trajectory'
      : 'Generations across rounds · survival trajectory', flowCard));
  }

  const standings = standingsTable(st, ctx, epochId, !!(st && st.live));
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// Re-shape the structure rounds keeping only matches whose bracket_slot
// passes `keep`; drops any round left empty. Carries the progressive live
// fields (done/total/inflight/queued/pending) through verbatim.
function splitBand(rounds, keep) {
  const out = [];
  for (const r of (Array.isArray(rounds) ? rounds : [])) {
    const matches = (Array.isArray(r.matches) ? r.matches : []).filter((m) => keep(String(m.bracket_slot || '')));
    if (matches.length) out.push({ round_index: r.round_index, label: r.label, queued: !!r.queued, matches });
  }
  return out;
}

// swiss — the model: per-round pairings, accumulating Copeland-point standings
// (win 1, draw ½), and the champion-gate state (leader must beat the incumbent).
export function swissModel(st) {
  if (!st || String(st.structure) !== 'swiss') return null;
  const live = !!st.live;
  const rawRounds = Array.isArray(st.rounds) ? st.rounds : [];
  const lineage = Array.isArray(st.champion_lineage) ? st.champion_lineage.map(String) : [];

  const rounds = rawRounds.map((r) => {
    const matches = Array.isArray(r.matches) ? r.matches : [];
    return {
      label: r.label || `Round ${(r.round_index || 0) + 1}`,
      queued: !!r.queued,
      pairings: matches.map((m) => {
        const comps = Array.isArray(m.competitors) ? m.competitors.map(String) : [];
        return {
          a: comps[0] != null ? comps[0] : null,
          b: comps.length > 1 ? comps[1] : null,
          winner: m.winner || null,
          bye: !!m.bye,
          delta: svg.isNum(m.delta_scalar) ? m.delta_scalar : null,
          done: svg.isNum(m.done) ? m.done : null,
          total: svg.isNum(m.total) ? m.total : null,
          inflight: svg.isNum(m.inflight) ? m.inflight : 0,
          pending: !!m.pending,
        };
      }),
    };
  });

  // Copeland-point standings: the payload's standings, else accumulated from the
  // completed pairings (win = 1, draw = 0.5).
  let standings;
  const raw = Array.isArray(st.standings) ? st.standings : [];
  if (raw.length) {
    standings = raw.map((s) => ({
      id: String(s.generation_id),
      points: svg.isNum(s.points) ? s.points
        : (svg.isNum(s.wins) ? s.wins + 0.5 * (svg.isNum(s.draws) ? s.draws : 0) : null),
      wins: svg.isNum(s.wins) ? s.wins : 0,
      draws: svg.isNum(s.draws) ? s.draws : 0,
      losses: svg.isNum(s.losses) ? s.losses : 0,
      rank: svg.isNum(s.rank) ? s.rank : null,
      status: String(s.status || '').toLowerCase(),
    }));
  } else {
    const tally = new Map();   // id → { points, wins, draws, losses }
    const bump = (id) => { if (!tally.has(id)) tally.set(id, { points: 0, wins: 0, draws: 0, losses: 0 }); return tally.get(id); };
    for (const r of rounds) {
      for (const p of r.pairings) {
        if (p.a != null) bump(p.a);
        if (p.b != null) bump(p.b);
        if (!p.winner || p.pending) continue;
        const w = String(p.winner);
        const loser = w === p.a ? p.b : p.a;
        if (p.b == null || p.bye) { const e = bump(w); e.points += 1; e.wins += 1; continue; }
        if (w === 'draw' || w === 'tie') { bump(p.a).points += 0.5; bump(p.a).draws += 1; bump(p.b).points += 0.5; bump(p.b).draws += 1; continue; }
        const we = bump(w); we.points += 1; we.wins += 1;
        if (loser != null) { const le = bump(loser); le.losses += 1; }
      }
    }
    standings = [...tally.entries()].map(([id, t]) => ({ id, points: t.points, wins: t.wins, draws: t.draws, losses: t.losses, rank: null, status: '' }));
  }
  // sort by points desc (then wins desc) → rank.
  standings.sort((a, b) => (b.points || 0) - (a.points || 0) || (b.wins || 0) - (a.wins || 0) || String(a.id).localeCompare(String(b.id)));
  standings.forEach((s, i) => { if (s.rank == null) s.rank = i + 1; });

  // the incumbent (benchmark) the swiss winner must beat at the gate.
  let benchmarkId = null;
  const champComp = (Array.isArray(st.competitors) ? st.competitors : []).find((c) => String(c.role || '').toLowerCase() === 'champion');
  if (champComp && champComp.generation_id != null) benchmarkId = String(champComp.generation_id);
  if (!benchmarkId && lineage.length) benchmarkId = lineage[0];

  const leader = standings.length ? standings[0].id : null;
  let championId = null;
  let gateState = live ? 'deciding' : (standings.length ? 'settled' : 'pending');
  if (!live && standings.length) {
    const promotedLeader = lineage.length && leader && lineage[lineage.length - 1] === leader && leader !== benchmarkId;
    const champStatus = standings.find((s) => s.status === 'champion');
    if (promotedLeader || (champStatus && String(champStatus.id) !== benchmarkId)) {
      championId = promotedLeader ? leader : (champStatus ? champStatus.id : null);
      gateState = championId ? 'crowned' : 'stands';
    } else gateState = 'stands';
  }
  return {
    rounds, standings, championId, benchmarkId, gateState, gateDelta: null, live,
    hasRounds: rounds.some((r) => r.pairings.length) || standings.length > 0,
  };
}

// ── the COMPACT SWISS OVERVIEW model (epoch-card hero) ──────────────
// Standings-bump series (rank-per-round) + ranked Copeland bar, from the SAME
// swissModel the Match-ups ladder uses (no refetch). The leader emerges only
// from rounds that scored. series:[{id,champion,ranks}] (rank 1=top). Null when
// nothing to show.
export function swissOverviewModel(st) {
  const model = swissModel(st);
  if (!model || !model.hasRounds) return null;
  const benchmarkId = model.benchmarkId;
  const pts = new Map();    // id → running Copeland points
  const everyone = new Set();
  for (const s of model.standings) { everyone.add(String(s.id)); pts.set(String(s.id), 0); }
  const labels = [];
  const ranksById = new Map();   // id → [rank per scored round]
  const ensure = (id) => { if (!pts.has(id)) { pts.set(id, 0); everyone.add(id); } };

  for (const r of model.rounds) {
    let scored = false;
    for (const p of r.pairings) {
      if (p.a != null) ensure(String(p.a));
      if (p.b != null) ensure(String(p.b));
      if (!p.winner || p.pending) continue;
      scored = true;
      const w = String(p.winner);
      if (p.bye || p.b == null) { pts.set(w, (pts.get(w) || 0) + 1); continue; }
      if (w === 'draw' || w === 'tie') {
        pts.set(String(p.a), (pts.get(String(p.a)) || 0) + 0.5);
        pts.set(String(p.b), (pts.get(String(p.b)) || 0) + 0.5);
        continue;
      }
      pts.set(w, (pts.get(w) || 0) + 1);
    }
    if (!scored) continue;
    const ordered = [...everyone].sort((a, b) => (pts.get(b) || 0) - (pts.get(a) || 0) || a.localeCompare(b));
    ordered.forEach((id, i) => { if (!ranksById.has(id)) ranksById.set(id, []); ranksById.get(id).push(i + 1); });
    labels.push(r.label || `R${labels.length + 1}`);
  }
  if (!labels.length) return null;

  // CHAMPION ROLES (distinguish the NEW champion from the displaced incumbent):
  //   crownId  — the champion AFTER this epoch: the promoted winner (championId),
  //              or the incumbent if it defended ('stands'). Gets the ♛ crown.
  //   formerId — the incumbent it displaced (only when a NEW champion was crowned).
  //              Gets a dim "former" mark so v0-was-champion / v6-is-champion reads
  //              cleanly. (Without a crowning there is no "former".)
  const crownId = model.championId
    ? String(model.championId)
    : (model.gateState === 'stands' && benchmarkId != null ? String(benchmarkId) : null);
  const formerId = (crownId && benchmarkId != null && String(benchmarkId) !== crownId)
    ? String(benchmarkId) : null;

  // one line per competitor, ordered by FINAL standing; pad missing early ranks.
  const finalOrder = model.standings.map((s) => String(s.id)).filter((id) => ranksById.has(id));
  for (const id of ranksById.keys()) if (!finalOrder.includes(id)) finalOrder.push(id);
  const series = finalOrder.map((id) => {
    const raw = ranksById.get(id) || [];
    const ranks = [];
    for (let i = 0; i < labels.length; i++) ranks.push(raw[i] != null ? raw[i] : (raw.length ? raw[raw.length - 1] : null));
    return { id, crown: crownId != null && id === crownId, former: formerId != null && id === formerId, ranks };
  }).filter((s) => s.ranks.some((r) => r != null));

  const leaderId = model.standings.length ? String(model.standings[0].id) : null;
  const bars = model.standings.map((s) => ({
    id: String(s.id), points: svg.isNum(s.points) ? s.points : 0,
    wins: s.wins || 0, draws: s.draws || 0, losses: s.losses || 0,
    leader: String(s.id) === leaderId,
    crown: crownId != null && String(s.id) === crownId,
    former: formerId != null && String(s.id) === formerId,
  }));
  return {
    series, bars, labels,
    championId: model.championId, benchmarkId, crownId, formerId, gateState: model.gateState,
    gateDelta: model.gateDelta, live: model.live,
  };
}

function renderSwiss(st, ctx, epochId) {
  const nodes = [];
  const model = swissModel(st) || { rounds: [], standings: [], live: !!(st && st.live), hasRounds: false };
  const open = (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); };
  const lCard = el('div', { class: 'dn-panel dn-figpane' });
  lCard.appendChild(model.hasRounds
    ? svg.swissLadder({
        rounds: model.rounds, standings: model.standings,
        championId: model.championId, benchmarkId: model.benchmarkId,
        live: model.live, gateState: model.gateState, gateDelta: model.gateDelta,
        onCompetitor: open,
      })
    : empty(model.live ? 'The swiss is being seeded — pairings fill in as runs land.' : 'No swiss rounds recorded yet.'));
  if (model.hasRounds) {
    const gateNote = model.gateState === 'crowned' ? ` · champion-gate: ${model.championId} promoted ${CROWN.current}`
      : model.gateState === 'stands' ? ' · champion-gate: champion stands'
      : model.gateState === 'deciding' ? ' · champion-gate: deciding…' : '';
    lCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      'each round pairs the field; Copeland points accumulate (win 1 / draw ½) · hover a pairing for its Δ scalar · ' + CROWN.current + ' = champion · ' + CROWN.former + ' = former champion (displaced incumbent) — the swiss leader must beat the incumbent at the champion-gate'
      + gateNote
      + (model.live ? ' · LIVE — the winner is not committed until the final gate' : '') }));
  }
  // ONE view: the ladder already lays out every round's pairings (with winners
  // + Δ on hover) alongside the accumulating standings and the champion-gate —
  // so the old standalone "Pairings · round by round" tables only duplicated the
  // pairings. Collapsed into this single section.
  nodes.push(section(model.live ? 'Swiss · LIVE — rounds, standings & champion-gate' : 'Swiss · rounds, standings & champion-gate', lCard));
  return nodes;
}

// ---- racing — the rung/gate model from a normalized payload ────────
// gateState ∈ 'crowned' | 'stands' | 'deciding' | 'pending'.
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

  // THE BENCHMARK (champion v0) the field is raced against (every rung Δ is
  // Δ-vs-this-id; it defends at the gate). Distinct from the crowned championId.
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

// shared: attribute in-flight /api/active-runs to per-gen board units (gen →
// { count, sumProgress }), scoped to this epoch's gens. The unified live model
// folds these into per-match done counts.
function inflightByGen(activeRuns, epochGens) {
  const genSet = epochGens ? new Set([...epochGens].map(String)) : null;
  const map = new Map();
  for (const r of (Array.isArray(activeRuns) ? activeRuns : [])) {
    const g = String(r.generation_id || r.gen || '');
    if (!g) continue;
    if (genSet && !genSet.has(g)) continue;
    const p = svg.isNum(r.progress) ? r.progress : 0;
    const cur = map.get(g) || { count: 0, sumProgress: 0 };
    cur.count += 1; cur.sumProgress += p;
    map.set(g, cur);
  }
  return map;
}

// ── BUILD the unified LIVE model — published rounds + active-runs overlay ──
//
// The backend now PUBLISHES the live tournament topology on
// /api/active-tournament DURING the run: `rounds` (each round's matches, with
// in-flight matches carrying `winner: null` + `pending: true`) and `standings`.
// So the dashboard no longer SYNTHESISES rung/round topology from the field +
// heartbeat — it consumes the published rounds verbatim and only OVERLAYS the
// per-board PROGRESS that still lives in /api/active-runs (the contract pins
// per-board progress there, not on the tournament).
//
// This ONE path serves racing / swiss / single_elim / double_elim — the prior
// per-structure synthesis builders were workarounds for the missing live data
// and are gone. Each published, still-pending match is stamped with the
// in-flight board count + a partial `done` tally (and, for racing, a per-lane
// `live_progress` map) so the ladder/bracket/funnel fills board-by-board
// without flashing. A finished match is carried through untouched.
//
// Returns null only when the payload is not the matching structure OR carries
// NEITHER competitors NOR rounds yet — the caller then shows the honest
// "starting" placeholder.
export function buildLiveModel(at, heartbeat, activeRuns, epochGens) {
  if (!at || typeof at !== 'object') return null;
  const structure = String(at.structure || '');
  const competitors = Array.isArray(at.competitors) ? at.competitors : [];
  const rawRounds = Array.isArray(at.rounds) ? at.rounds : [];
  const params = (at.structure_params && typeof at.structure_params === 'object')
    ? at.structure_params : (at.params && typeof at.params === 'object' ? at.params : {});
  if (!competitors.length && !rawRounds.length) return null;

  const isRacing = structure === 'racing';
  const isFinal = (mid) => String(mid || '') === 'racing-final';
  const firstMatch = (r) => (r && Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : {};
  const inflight = inflightByGen(activeRuns, epochGens);

  // per-board total (k/N progress label): the contract pins board_size; for
  // racing each rung covers a board fraction (board_fraction on the match).
  const boardSize = svg.isNum(params.board_size) ? params.board_size
    : (svg.isNum(at.board_size) ? at.board_size : null);
  const totalFor = (m) => {
    if (boardSize != null && isRacing && svg.isNum(m.board_fraction)) {
      return Math.max(1, Math.round(boardSize * m.board_fraction));
    }
    return boardSize;
  };

  // a partial aggregate Δ-vs-champion (challenger − champion) for racing lanes.
  const champAgg = svg.isNum(at.partial_champion_agg) ? at.partial_champion_agg : null;
  const challAgg = svg.isNum(at.partial_challenger_agg) ? at.partial_challenger_agg : null;
  const partialDelta = (challAgg != null && champAgg != null) ? (challAgg - champAgg) : null;

  // a match is SETTLED when it carries a winner / decision (a racing rung is
  // settled once survivors/cut land; a bye settles a swiss/elim slot).
  const settled = (m) => !!(m.winner || m.decision
    || (Array.isArray(m.survivors) && m.survivors.length)
    || (Array.isArray(m.cut) && m.cut.length)
    || m.bye);

  // overlay in-flight board progress onto a still-pending PUBLISHED match.
  const overlay = (m, queued) => {
    if (settled(m)) return m;
    const total = totalFor(m);
    if (isRacing) {
      // a racing rung's field is its competitors; surface a per-lane
      // live_progress map so the ladder shows each lane racing k/N boards.
      const field = (Array.isArray(m.competitors) ? m.competitors : []).map(String);
      const progress = {};
      for (const g of field) {
        const inf = inflight.get(g);
        progress[g] = inf
          ? { inflight: inf.count, done: Math.max(0, Math.floor(inf.sumProgress)), total, partialDelta }
          : { inflight: 0, done: 0, total, partialDelta: null };
      }
      return Object.assign({}, m, { winner: null, pending: true, queued, live_progress: queued ? null : progress });
    }
    // swiss / elim: a per-match done/inflight tally over the pairing's gens.
    const comps = (Array.isArray(m.competitors) ? m.competitors : []).map(String);
    let done = 0; let inf = 0;
    for (const g of comps) { const u = inflight.get(g); if (u) { inf += u.count; done += Math.floor(u.sumProgress); } }
    return Object.assign({}, m, { winner: null, pending: true,
      inflight: queued ? 0 : inf, done: queued ? 0 : done, total, queued });
  };

  // the ACTIVE round/rung is the first PUBLISHED round whose matches are not all
  // settled; earlier rounds are committed, later ones queued. The heartbeat
  // phase (`…:rung2_m1` / `…:round_2`) confirms it when present.
  const roundSettled = (r) => {
    const ms = Array.isArray(r.matches) ? r.matches : [];
    return ms.length > 0 && ms.every(settled);
  };
  const phase = String((heartbeat && heartbeat.phase) || at.phase || '');
  let activeIdx = null;
  { const m = isRacing ? /rung(\d+)/.exec(phase) : /round[_:]?(\d+)/.exec(phase); if (m) activeIdx = Number(m[1]); }
  if (activeIdx == null && svg.isNum(at.round_index)) activeIdx = at.round_index;

  const rounds = rawRounds.map((r, i) => {
    if (roundSettled(r)) return r;
    const m0 = firstMatch(r);
    // the gate is never "active" until its rung field settles; keep it pending.
    const isGate = isRacing && isFinal(m0.match_id);
    const ri = svg.isNum(r.round_index) ? r.round_index : i;
    const isActive = isGate ? false : (activeIdx != null ? ri === activeIdx
      : rawRounds.slice(0, i).every(roundSettled));
    const queued = !isActive;
    const matches = (Array.isArray(r.matches) ? r.matches : []).map((m) => overlay(m, queued));
    return { round_index: ri, label: r.label || (isRacing ? `Rung ${ri}` : `Round ${ri + 1}`), queued, matches };
  });

  // BLOOM the standings from the applied FIELD: when the run is past proposing
  // but no match has scored yet AND the payload carries no standings, seed a
  // zero-point row per competitor so the swiss/elim ladder shows the applied
  // challengers as live competitors immediately — not a "being seeded" empty.
  // Once any match scores, the structure model accumulates the real points.
  let standings = Array.isArray(at.standings) ? at.standings : [];
  const anyScored = rounds.some((r) => (Array.isArray(r.matches) ? r.matches : []).some(settled));
  const isProposing = /(^|[:_-])propos/i.test(phase);
  const challengerCount = competitors.filter((c) => c && c.generation_id != null && String(c.role || '').toLowerCase() !== 'champion').length;
  if (!standings.length && !anyScored && !isProposing && challengerCount > 0) {
    standings = competitors
      .filter((c) => c && c.generation_id != null)
      .map((c) => ({
        generation_id: String(c.generation_id),
        points: 0, wins: 0, draws: 0, losses: 0,
        status: String(c.role || '').toLowerCase() === 'champion' ? 'champion' : '',
      }));
  }

  return normalizeStructure({
    structure,
    structure_params: params,
    competitors,
    rounds,
    standings,
    champion_lineage: Array.isArray(at.champion_lineage) ? at.champion_lineage : [],
    phase: at.phase != null ? at.phase : (heartbeat && heartbeat.phase) || 'running',
    source: 'live',
  }, true);
}

// Structure-typed wrappers over the ONE unified live-model builder. Each returns
// null when `at` is not its structure, so the caller can dispatch by shape.
export function buildLiveRacingModel({ at, heartbeat, activeRuns, epochGens } = {}) {
  if (!at || typeof at !== 'object' || String(at.structure) !== 'racing') return null;
  return buildLiveModel(at, heartbeat, activeRuns, epochGens);
}
export function buildLiveSwissModel({ at, heartbeat, activeRuns, epochGens } = {}) {
  if (!at || typeof at !== 'object' || String(at.structure) !== 'swiss') return null;
  return buildLiveModel(at, heartbeat, activeRuns, epochGens);
}
export function buildLiveElimModel({ at, heartbeat, activeRuns, epochGens } = {}) {
  if (!at || typeof at !== 'object'
    || (String(at.structure) !== 'single_elim' && String(at.structure) !== 'double_elim')) return null;
  return buildLiveModel(at, heartbeat, activeRuns, epochGens);
}

// ── the MATCH-GROUPED LIVE BLOCKS — one block per IN-FLIGHT match ────
//
// The live-hero "what's running" hero (Task 1) groups the live state BY the
// in-flight match so it is obvious which boards are running, in EVERY structure.
// This is the pure data derivation: it consumes the UNIFIED live model
// (buildLiveModel's published rounds + active-runs overlay — the single source)
// and emits one block per ACTIVE (pending, not queued) match. A block is either:
//   * pairwise (swiss / elim / gauntlet): two sides, each a board ENTRY with a
//     0..1 progress ratio + an outcome ('done'/'pending'/'queued'); or
//   * rung-field (racing): one entry per lane in the rung's field.
//
// `entries[].outcome` ∈ 'pending' (still running) | 'queued' (not started) and,
// once the match settles, the side's verdict is read off the model: 'win' (the
// winner / a survivor → ✓), 'loss' (the loser / a cut → ✗), 'timeout' (⏱). A
// SETTLED match is NOT a block (it is no longer "running"); only in-flight
// matches surface here. Returns [] when nothing is in flight.
//
// Pure (model → plain array) so it unit-tests without a DOM and the hero can
// digest-gate on it.
export function liveMatchBlocks(model) {
  if (!model || typeof model !== 'object') return null;
  const structure = String(model.structure || '');
  if (!structure) return null;
  const isRacing = structure === 'racing';
  const isFinal = (mid) => String(mid || '') === 'racing-final';
  const rounds = Array.isArray(model.rounds) ? model.rounds : [];

  // a match has SETTLED when it carries a winner / decision / survivors / cut /
  // bye — the same predicate buildLiveModel uses; settled matches are not "live".
  const settled = (m) => !!(m.winner || m.decision
    || (Array.isArray(m.survivors) && m.survivors.length)
    || (Array.isArray(m.cut) && m.cut.length)
    || m.bye);
  // an entry's progress ratio from a per-board done/total tally (0..1, clamped).
  const ratio = (done, total) => {
    if (!svg.isNum(total) || total <= 0) return null;
    const r = (svg.isNum(done) ? done : 0) / total;
    return r < 0 ? 0 : (r > 1 ? 1 : r);
  };

  const blocks = [];
  for (const r of rounds) {
    if (r.queued) continue; // a future, not-yet-started round carries no LIVE block.
    const matches = Array.isArray(r.matches) ? r.matches : [];
    for (const m of matches) {
      if (m.queued) continue;        // a queued match in the active round is not running.
      if (settled(m)) continue;      // a settled match is no longer in flight.
      const comps = (Array.isArray(m.competitors) ? m.competitors : []).map(String);
      if (isRacing) {
        // a racing rung block: one entry per lane in the field; the per-lane
        // live_progress carries the board done/total tally.
        const prog = (m.live_progress && typeof m.live_progress === 'object') ? m.live_progress : {};
        const entries = comps.map((g) => {
          const lp = prog[g] || {};
          return {
            id: g, done: svg.isNum(lp.done) ? lp.done : 0,
            total: svg.isNum(lp.total) ? lp.total : (svg.isNum(m.total) ? m.total : null),
            inflight: svg.isNum(lp.inflight) ? lp.inflight : 0,
            ratio: ratio(lp.done, lp.total != null ? lp.total : m.total),
            outcome: 'pending',
          };
        });
        blocks.push({
          kind: 'rung', structure, match_id: m.match_id || null,
          label: (r.label || `rung ${svg.isNum(r.round_index) ? r.round_index : ''}`).trim()
            + (entries.length ? ` · field of ${entries.length}` : ''),
          entries,
        });
        continue;
      }
      // pairwise (swiss / elim / gauntlet): a two-sided duel. Split the per-match
      // done tally evenly across the two seats (the contract pins per-match
      // done/total, not per-seat), so each side reads the same board progress.
      const total = svg.isNum(m.total) ? m.total : null;
      const done = svg.isNum(m.done) ? m.done : 0;
      const seats = comps.length ? comps : ['tbd'];
      const entries = seats.slice(0, 2).map((g) => ({
        id: g, done, total, inflight: svg.isNum(m.inflight) ? m.inflight : 0,
        ratio: ratio(done, total), outcome: 'pending',
      }));
      const tag = isFinal(m.match_id)
        ? 'champion-gate'
        : (m.bracket_slot ? String(m.bracket_slot)
          : (r.label || (`round ${svg.isNum(r.round_index) ? r.round_index : ''}`)).trim());
      blocks.push({
        kind: 'pair', structure, match_id: m.match_id || null,
        label: tag + (seats.length >= 2 ? ` · ${seats[0]} vs ${seats[1]}` : (seats.length ? ` · ${seats[0]}` : '')),
        entries,
      });
    }
  }
  return blocks;
}

// A stable digest of the live match blocks — the live CONTENT (which matches
// exist + each board entry's progress BUCKET, not the raw float). Bucketed to
// ~10% so a steady heartbeat with no real progress is a no-op (the bars animate
// via CSS, the DOM is not rebuilt every tick). Mirrors the board-detail
// live-transcript render discipline.
export function liveMatchBlocksDigest(blocks) {
  const list = Array.isArray(blocks) ? blocks : [];
  return JSON.stringify(list.map((b) => [
    b.kind, b.match_id, b.label,
    (Array.isArray(b.entries) ? b.entries : []).map((e) => [
      e.id, e.outcome,
      // bucket the progress so only a REAL bucket change re-stamps the DOM.
      svg.isNum(e.ratio) ? Math.round(e.ratio * 10) : (e.inflight ? 'r' : 'q'),
    ]),
  ]));
}

// ── ONE candidate's MATCH-UPS from the LIVE published rounds ─────────
//
// The completed match-up feed (`/api/tournaments` → bracket.matchups) is EMPTY
// until matches commit, so a candidate running its FIRST round read "did not
// run in any round" even while the per-board scoring showed it racing `WB-R0-0`.
// The backend now PUBLISHES the live rounds on /api/active-tournament, so derive
// THIS candidate's match-ups from those rounds: every published match whose
// `competitors` includes `genId`. The first competitor is the champion seat, the
// rest are challengers (the gauntlet/elim/swiss convention the static feed uses);
// `winner: null` ⇒ pending. Returns [] when the live payload carries no rounds
// for this candidate (so the caller can fall back to the static feed). When
// `genId` is null EVERY published match-up is returned (the caller filters per
// candidate via the `champion`/`challenger` fields, exactly as for the static
// feed).
export function liveMatchupsForCandidate(at, genId) {
  if (!at || typeof at !== 'object') return [];
  const id = genId == null ? null : String(genId);
  const rounds = Array.isArray(at.rounds) ? at.rounds : [];
  const out = [];
  for (const r of rounds) {
    for (const m of (Array.isArray(r.matches) ? r.matches : [])) {
      const comps = (Array.isArray(m.competitors) ? m.competitors : []).map(String);
      if (id != null && comps.indexOf(id) < 0) continue;
      // champion = the first seat; each other competitor is a challenger.
      const champion = comps[0] != null ? comps[0] : null;
      const challenger = comps.length > 1 ? comps[comps.length - 1] : (comps[0] || null);
      // a racing rung is a multi-way cut, not a 1v1 — surface it as a row whose
      // champion seat is the field's benchmark when present, else the first id.
      out.push({
        champion, challenger,
        decision: m.decision || (m.winner ? 'promoted' : null),
        delta_scalar: svg.isNum(m.delta_scalar) ? m.delta_scalar : null,
        match_id: m.match_id || null,
        hypothesis_core_idea: null,
        live: true,
      });
    }
  }
  return out;
}

// ── ONE candidate's PATH through the racing tournament ──────────────
// → { stages:[{ label, kind:'rung'|'final', delta, verdict }] } | null
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

  // Split rounds into RUNG rounds (`rungN`) and the lone CHAMPION-GATE
  // (`racing-final`) — the gate is the full-board duel, not a rung.
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
    const gateNote = gateState === 'crowned' ? ` · champion-gate: ${championId} promoted ${CROWN.current}`
      : gateState === 'stands' ? ' · champion-gate: champion stands'
      : gateState === 'deciding' ? ' · champion-gate: deciding…'
      : '';
    card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      (benchmarkId ? `the field is raced vs the champion v0 = ${benchmarkId}; every rung Δ is Δ-vs-v0 and v0 defends at the champion-gate · ` : '')
      + 'each rung races the field on a fraction of the board, then cuts the worst by η · ✕ = cut · ↑ = survives · ' + CROWN.current + ' = champion-gate winner · click a competitor → open'
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
  const structure = (st && st.structure) || 'gauntlet';
  standings.sort((a, b) => (svg.isNum(a.rank) ? a.rank : 1e9) - (svg.isNum(b.rank) ? b.rank : 1e9));
  const tbl = el('table', { class: 'dn-board-table dt-standings' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'rank' }), el('th', { text: 'generation' }), el('th', { text: 'status' }),
    el('th', { class: 'dn-num', text: 'scalar' }), el('th', { class: 'dn-num', text: 'W' }), el('th', { class: 'dn-num', text: 'L' }), el('th', { text: '' }),
  ])]));
  const tbody = el('tbody');
  for (const s of standings) {
    let raw = String(s.status || '').toLowerCase();
    // LIVE — the verdicts have not committed; a standing tagged champion /
    // eliminated mid-run is the EVENTUAL outcome read from a half-finished
    // record. Treat everyone as still in contention so nobody is mislabeled —
    // and route through the SHARED structure mapper so the in-contention word
    // is structure-correct (elim → "in bracket", swiss → "playing", racing →
    // "racing"), NEVER a blanket "racing" for a non-racing tournament.
    if (live && (raw === 'champion' || raw === 'eliminated')) raw = 'competing';
    const status = structureStatusLabel(raw, structure);
    const rowCls = status === 'champion' ? 'dn-board-champ' : status === 'eliminated' ? 'dt-standings-out' : '';
    tbody.appendChild(el('tr', { class: rowCls }, [
      el('td', { class: 'dn-mono', text: svg.isNum(s.rank) ? String(s.rank) : '—' }),
      el('td', { class: 'dn-mono', text: (s.generation_id || '—') + (status === 'champion' ? ' ' + CROWN.current : '') }),
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
  // (alive / playing / in bracket / racing — still in contention).
  const verdict = s === 'champion' ? 'promoted' : s === 'eliminated' ? 'rejected' : 'deferred';
  const pill = verdictPill(verdict);
  pill.textContent = s;
  return pill;
}

function linkGen(gen, ctx, epochId) {
  if (!gen) return el('span', { class: 'dn-faint', text: 'bye' });
  return el('a', { class: 'dn-linkbtn dn-mono', href: ctx.href('candidate', { epochId, gen }), text: String(gen) });
}
