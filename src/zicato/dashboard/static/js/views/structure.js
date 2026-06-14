// js/views/structure.js — the configured tournament STRUCTURE.
//
// The match-ups page (views/gens.js) renders the gauntlet ladder for the
// (width:100% + viewBox, no pan/zoom, token-themed, page-scale aware).

import { el, svgEl } from '../core/dom.js';
import * as svg from '../svg.js';
import { section, empty, stat, verdictPill, overrideChip, overrideDigest, overrideControlCell, pendingOverride, pendingOverrideDigest, clearPendingOverride } from '../ui.js';
import { structureStatusLabel } from '../livestatus.js';
import { attachHovercard } from '../hovercard.js';
import { state } from '../core/state.js';
import { postFieldOverride } from '../core/api.js';
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

// Terminal tokens a run's `status` / `state` field may carry once it has ended.
const RUN_TERMINAL_STATUS = new Set([
  'done', 'complete', 'completed', 'finished', 'pass', 'fail', 'failed',
  'error', 'timeout', 'cached', 'skipped',
]);

// Is this run record TERMINAL (ended)? True on an explicit terminal flag/status
// or when every task/board has landed — keyed to COMPLETION, not the wall-clock
// budget (the "1/1 tasks completed but 0%" bug: the bar read elapsed/budget and
// ignored the completed work).
export function runIsTerminal(r) {
  if (!r || typeof r !== 'object') return false;
  if (r.completed === true || r.terminal === true || r.done === true) return true;
  const tok = String(r.status != null ? r.status : (r.state != null ? r.state : '')).trim().toLowerCase();
  if (tok && RUN_TERMINAL_STATUS.has(tok)) return true;
  // all tasks landed (e.g. the "1/1 tasks completed" case).
  if (svg.isNum(r.tasks_total) && r.tasks_total > 0) {
    const td = svg.isNum(r.tasks_completed) ? r.tasks_completed
      : (svg.isNum(r.tasks_done) ? r.tasks_done : null);
    if (td != null && td >= r.tasks_total) return true;
  }
  // all boards scored.
  if (svg.isNum(r.boards_total) && r.boards_total > 0
      && svg.isNum(r.boards_done) && r.boards_done >= r.boards_total) return true;
  return false;
}

// A normalised 0..1 progress ratio for an in-flight run (some payloads send
// 0..100 — clamp + normalise; fall back to elapsed/budget). A run that has
// reached a TERMINAL state reads a FULL 100% — its work is done regardless of
// how much of its wall-clock budget elapsed (so a completed run never shows the
// stale low time-fraction).
export function runProgressRatio(r) {
  if (runIsTerminal(r)) return 1;
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
    // the per-(entry × side) rows the backend publishes live; carried through so
    // the racing model can recover the FULL challenger field when the published
    // rounds are sparse/degenerate (issue #8).
    entries: Array.isArray(st.entries) ? st.entries : [],
    // The persisted within-tournament stage key is `stage_index` (a bracket
    // round / Swiss round / racing rung). Normalize it to the renderer's
    // internal `round_index` here — accepting the legacy `round_index` key so
    // workspaces written before the rename still render. (This axis is DISTINCT
    // from a generation's evolve `round_index`, which never flows through here.)
    rounds: (Array.isArray(st.rounds) ? st.rounds : []).map((r) =>
      (r && typeof r === 'object' && r.round_index == null && r.stage_index != null)
        ? { ...r, round_index: r.stage_index }
        : r),
    standings: Array.isArray(st.standings) ? st.standings : [],
    // the per-challenger proposing-step outcomes (applied/rejected + reason),
    // carried through so the "Proposed field" section + the live tracker can
    // read them from a normalized structure too.
    field_status: Array.isArray(st.field_status) ? st.field_status : [],
    // the epoch's champion succession; the LAST id is the reigning champion
    // (the promoted survivor of a settled racing tournament). Carried through
    // so the racing renderer can confirm the gate's crowned id.
    champion_lineage: Array.isArray(st.champion_lineage) ? st.champion_lineage.map(String) : [],
    // the LIVE aggregate scalars + per-gen projection maps — carried through
    // (additively) so championScalarOf can anchor the racing scalar track /
    // gauntlet field bars on the champion's running scalar mid-race, not just on
    // a settled standings row. Absent on a static index payload (⇒ null).
    partial_champion_agg: (st.partial_champion_agg && typeof st.partial_champion_agg === 'object') ? st.partial_champion_agg : null,
    projected: (st.projected && typeof st.projected === 'object') ? st.projected : null,
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

// ── the ONE non-gauntlet structure RESOLVER (live ↔ recorded) ───────
//
// THE SINGLE SOURCE OF TRUTH for "what tournament payload renders for this
// epoch". Both the ALL-ROUNDS Match-ups page (renderConfiguredStructure) AND the
// PER-ROUND drill-down (renderRoundDrilldown) MUST resolve their racing/swiss/
// elim `st` THROUGH HERE, so the two paths can never drift (the recurring round-
// view-empty bug class: the round view used to read the per-round FIELD record
// directly, but a racing field record carries `rounds: []` — rungs live in the
// per-challenger records + the live envelope, NOT the aggregate field record —
// so the round view came up with zero rungs while the epoch view, which went
// live-first → reconstructRacing, showed them).
//
// PURE + SYNCHRONOUS: it consumes ALREADY-FETCHED inputs (the live
// active-tournament payload, the /api/tournaments bracket, the heartbeat /
// active-runs, and an OPTIONAL already-fetched completed per-tournament record)
// and returns { st, source } — so it unit-tests without a DOM and the caller
// owns the async fetch. The CONVERGENCE GUARANTEE holds because a SETTLED epoch
// (no live run) resolves the SAME reconstructed/recorded `st` regardless of
// which page asks.
//
//   structure      — the configured structure ('racing'|'swiss'|'single_elim'|…)
//   bracket        — the /api/tournaments payload (for reconstructRacing + the
//                    per-tournament fallback record selection)
//   epochId        — scopes reconstructRacing + the live guard
//   liveRaw        — the /api/active-tournament payload (null when idle / foreign)
//   heartbeat,     — the live signals the progressive builders overlay
//   activeRuns
//   params         — the contract's structure params (fallback for a sparse record)
//   completedRecord— an OPTIONAL already-fetched per-tournament structure record
//                    (swiss/elim completed fallback); racing uses reconstructRacing
//                    so it does not need this.
//
// → { st, source } where source ∈ 'live' | 'reconstructed' | 'record' | null.
export function resolveNonGauntletSt(opts) {
  const o = opts || {};
  const structure = String(o.structure || 'gauntlet');
  const bracket = (o.bracket && typeof o.bracket === 'object') ? o.bracket : {};
  const epochId = o.epochId != null ? o.epochId : null;
  const liveRaw = (o.liveRaw && typeof o.liveRaw === 'object') ? o.liveRaw : null;

  // (1) LIVE-FIRST — a run in flight for THIS epoch governs the topology so the
  // ladder fills in rung/round-by-round and in-flight competitors are shown
  // racing, never prematurely crowned/rejected. The progressive builders
  // accumulate completed rounds + fill the active one board-by-board; a plain
  // normalize is the fallback when the structure is not one they handle.
  let liveSt = null;
  if (liveRaw && (String(liveRaw.structure) === structure || isNonGauntlet(String(liveRaw.structure)))) {
    const ls = String(liveRaw.structure);
    const epochGens = (Array.isArray(liveRaw.competitors) ? liveRaw.competitors : [])
      .map((c) => c && c.generation_id).filter((g) => g != null).map(String);
    const args = { at: liveRaw, heartbeat: o.heartbeat, activeRuns: o.activeRuns, epochGens: epochGens.length ? epochGens : null };
    if (ls === 'racing') liveSt = buildLiveRacingModel(args) || normalizeStructure(liveRaw, true);
    else if (ls === 'swiss') liveSt = buildLiveSwissModel(args) || normalizeStructure(liveRaw, true);
    else if (ls === 'single_elim' || ls === 'double_elim') liveSt = buildLiveElimModel(args) || normalizeStructure(liveRaw, true);
    else liveSt = normalizeStructure(liveRaw, true);
  }
  // ADOPT the live model when it is flagged live OR when it carries an IN-FLIGHT,
  // STREAMING racing rung (the authoritative streaming-rung signal even when the
  // phase string has not flipped `live` — mirrors renderConfiguredStructure).
  const hasStreamingRacingRung = (s) => {
    if (!s || String(s.structure) !== 'racing') return false;
    const m = racingModel(s);
    return !!(m && Array.isArray(m.rungs) && m.rungs.some((r) =>
      r && r.pending && r.live_progress && typeof r.live_progress === 'object'
      && Object.keys(r.live_progress).length > 0));
  };
  // Adopt the live model only when it actually has bracket CONTENT to show (a
  // committed/in-flight match, or a streaming racing rung) — OR when there is
  // no settled record to fall back to (the FIRST round's own proposing, where
  // an empty "being seeded" ladder IS the correct live state). This preserves a
  // just-SETTLED round's bracket when the NEXT round has only begun proposing
  // (an empty active-tournament envelope, phase="proposing", rounds: []),
  // instead of letting that empty envelope overwrite the prior round's results
  // with "being seeded" (the round-N "stuck on seeding" regression / issue #16).
  const liveHasContent = (s) => {
    if (!s) return false;
    if (hasStreamingRacingRung(s)) return true;
    return (Array.isArray(s.rounds) ? s.rounds : [])
      .some((r) => r && Array.isArray(r.matches) && r.matches.length > 0);
  };
  if (liveSt && (liveSt.live || hasStreamingRacingRung(liveSt))
      && (liveHasContent(liveSt) || !o.completedRecord)) {
    return { st: liveSt, source: 'live' };
  }

  // (2) RACING — reconstruct the rung/gate ladder from the per-challenger records
  // on the bracket (the aggregate field record carries `rounds: []` by design).
  if (structure === 'racing') {
    const recon = reconstructRacing(bracket, epochId);
    if (recon) return { st: recon, source: 'reconstructed' };
  }

  // (3) the COMPLETED per-tournament record (swiss/elim — or racing when no
  // per-challenger records reconstruct), already fetched + normalized by the
  // caller. Null ⇒ no recorded structure for this epoch.
  if (o.completedRecord) return { st: o.completedRecord, source: 'record' };
  return { st: null, source: null };
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
            + (svg.isNum(p.partialDelta) ? ':' + p.partialDelta.toFixed(2) : '')
            // the per-lane PROJECTED standing — ROUNDED so a no-op beat stays
            // byte-identical but a real projection change repaints (anti-flash).
            + (p.projected ? ':j' + (svg.isNum(p.projected_scalar) ? p.projected_scalar.toFixed(3) : '?')
              + '/' + (p.boards_done == null ? '?' : p.boards_done) + '/' + (p.boards_total == null ? '?' : p.boards_total) : '');
        }).join(',') : '',
        // the per-match (swiss/elim) projected map — rounded scalar + integer
        // board counts, so the gated swap fires on real progress only.
        m.projected ? Object.keys(m.projected).sort().map((g) => {
          const p = m.projected[g];
          return g + ':' + (svg.isNum(p.scalar) ? p.scalar.toFixed(3) : '?')
            + '/' + (p.boards_done == null ? '?' : p.boards_done) + '/' + (p.boards_total == null ? '?' : p.boards_total);
        }).join(',') : '',
      ]),
    ]),
    standings: (Array.isArray(st.standings) ? st.standings : []).map((s) => [s.generation_id, s.rank, s.scalar, s.wins, s.losses, s.status,
      // the projected-standing overlay — ROUNDED scalar + integer board counts +
      // the in_flight flag, so an identical projection yields an identical digest
      // (no repaint) but a board landing or a re-rank fires the swap.
      s.in_flight ? 'j' + (svg.isNum(s.projected_scalar) ? s.projected_scalar.toFixed(3) : '?')
        + '/' + (s.boards_done == null ? '?' : s.boards_done) + '/' + (s.boards_total == null ? '?' : s.boards_total) : '',
      // operator-override provenance folded in (kind+action+state+reason, NO
      // timestamp) so an override appearing/changing repaints while a no-op
      // beat stays byte-identical. null (none) → pre-override digest (back-compat).
      (st.override_status && typeof st.override_status === 'object')
        ? overrideDigest(st.override_status[String(s.generation_id)]) : null]),
    // the OPTIMISTIC 'queued' override stamps (the operator's own, pre-readback)
    // folded in by generation_id — NO timestamp — so a freshly-fired override
    // repaints the standings (queued stamp + drained transition) but a no-op beat
    // stays byte-identical. [] when none are pending → pre-control digest.
    pending_overrides: pendingOverrideDigest((Array.isArray(st.standings) ? st.standings : []).map((s) => s.generation_id)),
    // the advanced SET at settle (supports MULTIPLE promoted / ties) — folded so a
    // settle that drains a queued override (gid not in the set) repaints. Sorted;
    // absent → null (back-compat with pre-override-readback runs).
    promoted: Array.isArray(st.promoted_generation_ids)
      ? st.promoted_generation_ids.map((g) => String(g)).slice().sort() : null,
    // the proposing-step field — so the "Proposed field" section's gated
    // swap fires when a challenger is minted / applied / rejected, but stays
    // stable on a no-op heartbeat.
    field_status: (Array.isArray(st.field_status) ? st.field_status : []).map((f) => [f && f.generation_id, f && f.status]),
    // the live champion aggregate scalar — anchors the racing scalar track /
    // gauntlet field bars; ROUNDED so a no-op beat stays byte-identical but a
    // real champion-scalar move repaints (anti-flash).
    champ_agg: (st.partial_champion_agg && svg.isNum(st.partial_champion_agg.scalar)) ? st.partial_champion_agg.scalar.toFixed(3) : null,
    // the FIELD-DIVERSITY block — ROUNDED overlap scalars + integer counts + the
    // max-overlap pair + the per-slot diversity_status + the overlap-matrix
    // membership digest. Folded so a real diversity move (a soft-reject landing,
    // the overlap shifting, a slot's status changing) repaints the ribbon while a
    // no-op beat stays byte-identical. Absent → null (back-compat — gauntlet /
    // single-challenger / pre-feature digest is byte-identical to today).
    diversity: diversityDigest(st),
    source: st.source,
  });
}

// The diversity fold for structureDigest: the `diversity` block's overlap
// scalars (ROUNDED), the integer counts + max-overlap pair, the per-slot
// diversity_status, and the overlap-matrix membership — NO floats beyond the
// rounded overlaps, NO timestamps. null when the diversity block is absent.
function diversityDigest(st) {
  const d = (st && st.diversity && typeof st.diversity === 'object') ? st.diversity : null;
  if (!d || !svg.isNum(d.field_size) || d.field_size < 2) return null;
  const statuses = (Array.isArray(st.field_status) ? st.field_status : [])
    .filter((f) => f && f.generation_id != null && (f.diversity_status === 'soft_rejected' || f.diversity_status === 'penalized'))
    .map((f) => [String(f.generation_id), f.diversity_status])
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return {
    fs: d.field_size,
    di: svg.isNum(d.distinct_ideas) ? d.distinct_ideas : null,
    mean: svg.isNum(d.mean_overlap) ? d.mean_overlap.toFixed(3) : null,
    max: svg.isNum(d.max_overlap) ? d.max_overlap.toFixed(3) : null,
    pair: Array.isArray(d.max_overlap_pair) ? d.max_overlap_pair.map(String) : null,
    tol: svg.isNum(d.tolerance) ? d.tolerance.toFixed(3) : null,
    soft: svg.isNum(d.soft_rejected_count) ? d.soft_rejected_count : 0,
    st: statuses,
    mtx: svg.diversityMatrixDigest({ membership: diversityMembership(st), highlightPair: d.max_overlap_pair }),
  };
}

// ── the structure render dispatch — DOM sections per structure ──────
export function renderStructure(st, ctx, epochId) {
  const structure = String((st && st.structure) || 'gauntlet');
  let nodes;
  if (structure === 'swiss') nodes = renderSwiss(st, ctx, epochId);
  else if (structure === 'racing') nodes = renderRacing(st, ctx, epochId);
  else if (structure === 'gauntlet') nodes = renderGauntlet(st, ctx, epochId);
  // single_elim + double_elim share the bracket renderer.
  else nodes = renderBracket(st, ctx, epochId, structure);
  // The PROPOSED FIELD section leads so a completed epoch's proposing
  // outcomes are visible (e.g. "4 proposed · 0 applied — all rejected" with
  // per-challenger reasons). Absent field_status ⇒ no section (back-compat).
  const proposed = proposedFieldSection(st, ctx, epochId);
  // The FIELD-DIVERSITY ribbon rides directly UNDER the proposed-field section:
  // the mean/max pairwise-Jaccard overlap of the minted field + the soft-reject
  // count, with the overlap matrix beneath it. Absent (single-challenger / pre-
  // feature / gauntlet) → renders nothing (byte-identical to today).
  const diversity = diversitySection(st, ctx, epochId);
  const lead = [proposed, diversity].filter(Boolean);
  return lead.length ? [...lead, ...nodes] : nodes;
}

// ── the FIELD-DIVERSITY ribbon — mean/max pairwise-Jaccard + overlap matrix ──
//
// Reads the additive `diversity` block VERBATIM (build_tournament_structure →
// _enrich_diversity / _compute_field_diversity): `{field_size, distinct_ideas,
// mean_overlap, max_overlap, max_overlap_pair, tolerance, soft_rejected_count}`.
// KEY-ABSENT on a gauntlet / single-challenger / pre-feature run → render NOTHING
// (byte-identical to today). Higher overlap is WORSE (a field of N collapses to
// fewer real experiments), so the meter earns its tone BY DIRECTION: at/above the
// tolerance reads caution, below it reads good.
function diversitySection(st, ctx, epochId) {
  const d = (st && st.diversity && typeof st.diversity === 'object') ? st.diversity : null;
  if (!d || !svg.isNum(d.field_size) || d.field_size < 2) return null;
  const tol = svg.isNum(d.tolerance) ? d.tolerance : null;
  const soft = svg.isNum(d.soft_rejected_count) ? d.soft_rejected_count : 0;
  const meanO = svg.isNum(d.mean_overlap) ? d.mean_overlap : 0;
  const maxO = svg.isNum(d.max_overlap) ? d.max_overlap : 0;
  const distinct = svg.isNum(d.distinct_ideas) ? d.distinct_ideas : null;
  const pair = Array.isArray(d.max_overlap_pair) ? d.max_overlap_pair.map(String) : null;

  // the headline stat strip — distinct ideas / field size + soft-rejects.
  const stats = el('div', { class: 'dn-divstats' }, [
    (distinct != null) ? stat(distinct + ' / ' + d.field_size, 'distinct ideas') : null,
    stat(svg.fmt(meanO, 2), 'mean overlap'),
    stat(svg.fmt(maxO, 2), 'max overlap'),
    soft > 0 ? stat(String(soft), 'soft-rejected') : null,
  ].filter(Boolean));

  // the dual mean/max overlap meter against the tolerance marker.
  const meter = overlapMeter(meanO, maxO, tol);
  if (pair && pair.length === 2) {
    attachHovercard(meter, () => el('div', { class: 'dn-hc-body' }, [
      el('div', { class: 'dn-hc-title', text: 'most-overlapping pair' }),
      el('div', { class: 'dn-hc-row dn-mono', text: pair[0] + ' ⇄ ' + pair[1] }),
      el('div', { class: 'dn-hc-row dn-faint', text: 'Jaccard ' + svg.fmt(maxO, 2)
        + (tol != null ? ' · tolerance ' + svg.fmt(tol, 2) : ' · enforcement off') }),
    ]));
  }

  // a soft-reject chip reuses the DEFERRED pill vocabulary (held, not promoted).
  const softChip = soft > 0
    ? (() => { const p = verdictPill('deferred'); p.textContent = soft + ' soft-rejected'; return p; })()
    : null;

  // the overlap matrix — challenger × mutation-site (the dn-mtx grammar). The
  // per-challenger site membership is NOT on the diversity block, so the
  // dashboard derives it from any membership the payload carries; absent →
  // svg.diversityMatrix returns null → no matrix (byte-identical). FOLLOWUP:
  // wire per-challenger mutation_ids onto the structure payload (Python).
  const membership = diversityMembership(st);
  const matrix = svg.diversityMatrix({
    membership, highlightPair: pair,
    onCompetitor: (gen) => { if (gen && ctx && ctx.navigate) ctx.navigate('candidate', { epochId, gen }); },
  });

  const cap = el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
    'pairwise idea overlap (Jaccard) across the minted field — higher overlap means the field is converging on the same idea'
    + (tol != null ? ' · soft-rejected above ' + svg.fmt(tol, 2) : ' · overlap enforcement off (diagnostic only)') });

  const panel = el('div', { class: 'dn-panel dn-divribbon' }, [
    el('div', { class: 'dn-divribbon-head' }, [stats, softChip].filter(Boolean)),
    meter, cap,
    matrix,
  ].filter(Boolean));
  return section('Field diversity', panel);
}

// The dual overlap meter: a single track with the MEAN-overlap fill + a MAX-
// overlap notch, the tolerance drawn as the dashed promote-threshold marker. The
// fill earns its tone BY DIRECTION — at/above the tolerance is caution (the field
// is collapsing), below is good. No tolerance (enforcement off) → a neutral fill
// (the overlap is diagnostic, not a gate). Returns a Node.
function overlapMeter(mean, max, tol) {
  const W = 260, H = 30, padX = 4, axW = W - 2 * padX;
  const fig = svgEl('svg', {
    class: 'dn-div-meter', width: '100%', height: H, viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: 'none', role: 'img',
    'aria-label': 'mean and max pairwise idea overlap vs the diversity tolerance',
  });
  const top = 6, barH = 12;
  fig.appendChild(svgEl('rect', { x: padX, y: top, width: axW, height: barH, class: 'dn-div-track' }));
  const over = svg.isNum(tol) ? (mean >= tol) : null;
  const mw = Math.max(0, Math.min(1, mean)) * axW;
  fig.appendChild(svgEl('rect', { x: padX, y: top, width: mw, height: barH,
    class: 'dn-div-fill ' + (over === true ? 'dn-caution-fill' : over === false ? 'dn-good-fill' : 'dn-flat-fill') }));
  // the MAX-overlap notch.
  const mx = padX + Math.max(0, Math.min(1, max)) * axW;
  fig.appendChild(svgEl('line', { x1: mx, y1: top - 3, x2: mx, y2: top + barH + 3, class: 'dn-div-max' }));
  if (svg.isNum(tol)) {
    const tx = padX + Math.max(0, Math.min(1, tol)) * axW;
    fig.appendChild(svgEl('line', { x1: tx, y1: top - 4, x2: tx, y2: top + barH + 4, class: 'dn-div-tol' }));
    const tl = svgEl('text', { x: tx, y: top + barH + 14, class: 'dn-div-tollab', 'text-anchor': 'middle' });
    tl.textContent = 'tol ' + svg.fmt(tol, 2);
    fig.appendChild(tl);
  }
  return el('div', { class: 'dn-div-meterwrap' }, [
    el('div', { class: 'dn-div-meterhead' }, [
      el('span', { class: 'dn-div-meterlab', text: 'mean overlap' }),
      el('span', { class: 'dn-div-meterval dn-mono', text: svg.fmt(mean, 2)
        + ' · max ' + svg.fmt(max, 2) }),
    ]),
    fig,
  ]);
}

// Derive the per-challenger mutation-site membership for the overlap matrix from
// whatever the structure payload carries. The `diversity` block summarises the
// field but does NOT carry per-challenger membership; a payload may still expose
// it on `field_status[].mutation_ids` / `competitors[].mutation_ids` (a forward-
// compatible additive field). Absent → [] → svg.diversityMatrix renders nothing.
function diversityMembership(st) {
  if (!st || typeof st !== 'object') return [];
  const out = [];
  const seen = new Set();
  const take = (rec) => {
    if (!rec || typeof rec !== 'object') return;
    const gid = rec.generation_id;
    if (gid == null || gid === '' || seen.has(String(gid))) return;
    const sites = Array.isArray(rec.mutation_ids) ? rec.mutation_ids.filter((s) => s != null && s !== '').map(String)
      : (Array.isArray(rec.mutation_sites) ? rec.mutation_sites.filter((s) => s != null && s !== '').map(String) : null);
    if (!sites || !sites.length) return;
    seen.add(String(gid));
    out.push({ generation_id: String(gid), sites });
  };
  (Array.isArray(st.field_status) ? st.field_status : []).forEach(take);
  (Array.isArray(st.competitors) ? st.competitors : []).forEach(take);
  return out;
}

// Read the per-challenger proposing outcomes (the v5 `field_status`) off a
// tournament-structure payload — same shape data.fieldStatus() produces, []
// if absent. Carries the v6 observability fields (status "proposing",
// attempts, attempt_reasons, hypothesis) so the proposal phase is legible
// post-hoc, not only live.
export function fieldStatusOf(st) {
  const fs = st && st.field_status;
  if (!Array.isArray(fs)) return [];
  const out = [];
  for (const f of fs) {
    if (!f || typeof f !== 'object') continue;
    const gid = f.generation_id;
    if (gid == null || gid === '') continue;
    let status;
    if (f.status === 'applied') status = 'applied';
    else if (f.status === 'proposing') status = 'proposing';
    else status = 'rejected';
    const reasons = Array.isArray(f.attempt_reasons)
      ? f.attempt_reasons.filter((r) => r != null && String(r) !== '').map((r) => String(r))
      : [];
    out.push({
      generation_id: String(gid),
      status,
      reason: f.reason == null ? '' : String(f.reason),
      attempts: (typeof f.attempts === 'number' && f.attempts >= 0) ? f.attempts : reasons.length,
      attempt_reasons: reasons,
      hypothesis: f.hypothesis == null ? '' : String(f.hypothesis),
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
  const proposing = fs.filter((f) => f.status === 'proposing').length;
  // A slot still proposing means the field is forming RIGHT NOW — treat as
  // live even if the payload's own `live` flag has not flipped yet.
  const live = !!(st && st.live) || proposing > 0;
  const applied = fs.filter((f) => f.status === 'applied').length;
  const rejected = fs.filter((f) => f.status === 'rejected').length;
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

// Build the elimRadial/elimFlow `rounds` (the elimModel-band shape both
// renderers consume): the winners' band followed by the losers' band, each round
// carrying its label + matches. ONE source, two renderers (radial ↔ flow).
function elimBands(model) {
  return model.winners.concat(Array.isArray(model.losers) ? model.losers : []);
}

// The shared figure CAPTION for the bracket flow/radial figures.
function bracketCaption(model) {
  const gateNote = model.gateState === 'crowned' ? ` · champion-gate: ${model.championId} promoted ${CROWN.current}`
    : model.gateState === 'stands' ? ' · champion-gate: champion stands'
    : model.gateState === 'deciding' ? ' · champion-gate: deciding…' : '';
  return el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
    'each lane is a generation; two lanes converge at a match — the winner’s lane continues ↑, the loser’s ends with ✕ — and the champion’s lane reaches the gate ' + CROWN.current
    + (model.benchmarkId ? ' · ' + CROWN.former + ' = displaced incumbent' : '')
    + (model.losers && model.losers.length ? ' · the losers’ bracket re-converges as a second band (double-elim)' : '')
    + gateNote
    + (model.live ? ' · LIVE — in-flight legs are dashed' : '') });
}

function renderBracket(st, ctx, epochId, structure) {
  const model = elimModel(st) || { winners: splitBand((st && st.rounds) || [], () => true), losers: null, live: !!(st && st.live) };
  const openGen = (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); };
  const bands = elimBands(model);
  const hasFigure = model.hasMatches !== false && model.winners.length;
  const isDouble = structure === 'double_elim';

  // single_elim → the RADIAL bracket leads (single-elim.html opt 6): concentric
  // rings narrowing to a center champion seat. double_elim → the orthogonal-pipe
  // elimFlow combo leads (double-elim.html opt 7, the DEFAULT), with the radial
  // (opt 8, {double:true}) offered as a NON-default toggle on the figure.
  return isDouble
    ? renderDoubleElim(st, ctx, epochId, model, bands, hasFigure, openGen)
    : renderSingleElim(st, ctx, epochId, model, bands, hasFigure, openGen);
}

// single_elim — RADIAL bracket PRIMARY (the liked opt 6), the bracket-as-FLOW
// (elimFlow) retained as a secondary companion view (who-converged-with-whom,
// the lane convergence read).
function renderSingleElim(st, ctx, epochId, model, bands, hasFigure, openGen) {
  const nodes = [];

  // the PRIMARY figure: the concentric-ring radial bracket.
  const radialCard = el('div', { class: 'dn-panel dn-figpane' });
  radialCard.appendChild(hasFigure
    ? svg.elimRadial({
        rounds: bands, championId: model.championId, benchmarkId: model.benchmarkId,
        gateState: model.gateState, live: model.live, double: false, onCompetitor: openGen,
      })
    : empty(model.live ? 'The bracket is being seeded — matches fill in as runs land.' : 'No bracket rounds recorded yet.'));
  if (model.winners.length) {
    const gateNote = model.gateState === 'crowned' ? ` · champion-gate: ${model.championId} promoted ${CROWN.current}`
      : model.gateState === 'stands' ? ' · champion-gate: champion stands'
      : model.gateState === 'deciding' ? ' · champion-gate: deciding…' : '';
    radialCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      'rounds are concentric rings narrowing to the champion seat at the center; each spoke is a generation — the rings it survived read green, the ring it was eliminated at turns red ✕, and the survivor dashes into the center gate ' + CROWN.current
      + (model.benchmarkId ? ' · ' + CROWN.former + ' = displaced incumbent' : '')
      + gateNote
      + (model.live ? ' · LIVE — still-racing spokes are dashed' : '') }));
  }
  nodes.push(section(model.live ? 'Bracket · LIVE — rings narrowing to the champion gate' : 'Bracket · rings narrowing to the champion gate', radialCard));

  // SECONDARY: the bracket-as-FLOW (lane convergences) — the who-played-whom read
  // the radial cannot show. Retained as a companion view below the radial.
  if (hasFigure) {
    const flowCard = el('div', { class: 'dn-panel dn-figpane' });
    flowCard.appendChild(svg.elimFlow({
      winners: bands, championId: model.championId, benchmarkId: model.benchmarkId,
      gateState: model.gateState, live: model.live, onCompetitor: openGen,
    }));
    flowCard.appendChild(bracketCaption(model));
    nodes.push(section(model.live ? 'Bracket flow · LIVE — lane convergences, click a lane to open the candidate' : 'Bracket flow · lane convergences, click a lane to open the candidate', flowCard));
  }

  const standings = standingsTable(st, ctx, epochId, !!(st && st.live));
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// double_elim — the orthogonal-pipe FLOW combo is the DEFAULT figure (the liked
// opt 7: WB/LB bands + life glyphs). The RADIAL ({double:true}, opt 8) is offered
// as an OPTIONAL, NON-default variant via a small segmented toggle ON the figure
// — both are rendered into the card, the radial hidden until the toggle flips it.
// The toggle is pure client-side visibility (no data change) so it never
// disturbs the digest-gated swap.
function renderDoubleElim(st, ctx, epochId, model, bands, hasFigure, openGen) {
  const nodes = [];
  const flowCard = el('div', { class: 'dn-panel dn-figpane' });
  if (!hasFigure) {
    flowCard.appendChild(empty(model.live ? 'The bracket is being seeded — matches fill in as runs land.' : 'No bracket rounds recorded yet.'));
    nodes.push(section(model.live ? 'Bracket flow · LIVE — winners’ + losers’ lanes' : 'Bracket flow · winners’ + losers’ lanes', flowCard));
    const standingsE = standingsTable(st, ctx, epochId, !!(st && st.live));
    if (standingsE) nodes.push(section('Standings', standingsE));
    return nodes;
  }

  // the segmented toggle: COMBO (default) ↔ RADIAL — a small control on the
  // figure, mirroring the chrome's .dn-theme-switch idiom.
  const flowPane = el('div', { class: 'dt-figview dt-figview-on' }, [
    svg.elimFlow({
      winners: bands, championId: model.championId, benchmarkId: model.benchmarkId,
      gateState: model.gateState, live: model.live, onCompetitor: openGen,
    }),
  ]);
  const radialPane = el('div', { class: 'dt-figview' }, [
    svg.elimRadial({
      rounds: bands, championId: model.championId, benchmarkId: model.benchmarkId,
      gateState: model.gateState, live: model.live, double: true, onCompetitor: openGen,
    }),
  ]);
  const comboBtn = el('button', { class: 'dt-fig-btn dt-fig-active', type: 'button', text: 'combo' });
  const radialBtn = el('button', { class: 'dt-fig-btn', type: 'button', text: 'radial' });
  const show = (which) => {
    const combo = which === 'combo';
    flowPane.classList.toggle('dt-figview-on', combo);
    radialPane.classList.toggle('dt-figview-on', !combo);
    comboBtn.classList.toggle('dt-fig-active', combo);
    radialBtn.classList.toggle('dt-fig-active', !combo);
  };
  comboBtn.addEventListener('click', () => show('combo'));
  radialBtn.addEventListener('click', () => show('radial'));
  flowCard.appendChild(el('div', { class: 'dt-fig-switchrow' }, [
    el('span', { class: 'dt-fig-switchlab', text: 'figure' }),
    el('div', { class: 'dt-fig-switch' }, [comboBtn, radialBtn]),
  ]));
  flowCard.appendChild(flowPane);
  flowCard.appendChild(radialPane);
  flowCard.appendChild(bracketCaption(model));
  flowCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:4px 0 0;', text:
    'default: the winners’ + losers’ orthogonal-pipe combo · switch to “radial” for the concentric-ring (polar) view of the same bracket' }));
  nodes.push(section(model.live ? 'Bracket flow · LIVE — winners’ + losers’ lanes' : 'Bracket flow · winners’ + losers’ lanes', flowCard));

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
          // the per-side live PROJECTED standing on an in-flight pairing.
          projected: (m.projected && typeof m.projected === 'object') ? m.projected : null,
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
      // the live PROJECTED standing carried through from the overlay so the
      // swiss ladder marks an in-flight competitor "projected" (dashed/~).
      in_flight: !!s.in_flight,
      projected_scalar: svg.isNum(s.projected_scalar) ? s.projected_scalar : null,
      boards_done: svg.isNum(s.boards_done) ? s.boards_done : null,
      boards_total: svg.isNum(s.boards_total) ? s.boards_total : null,
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
  // the Standings table rides BELOW the ladder so the per-challenger override
  // CONTROL plane (force promote/reject + provenance) is consistent across EVERY
  // structure, not only the bracket/racing/gauntlet ones. The ladder already
  // lays out pairings; this table carries the actionable per-row controls.
  const standings = standingsTable(st, ctx, epochId, !!(st && st.live));
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// ── the FULL FIELD of a racing rung — the UNION of all its matchups ──
//
// A LIVE racing rung is published as N matchups (`rung{N}_m0..mK` = champion vs
// EACH survivor) inside ONE RoundRecord; only `matches[0]` carries the
// authoritative full-rung `live_progress` map (every lane), while the remaining
// per-duel matches keep an empty progress map (see racing.py `_pending_round`).
// So `firstMatch(r)` alone sees only the FIRST matchup's competitors
// (`[champion, challenger0]`) — a single lane. The rung's TRUE field is the
// UNION across every matchup's competitors AND the lane keys of the authoritative
// `live_progress`, minus the champion(s). This is what every lane racing the rung
// is — feed it to the builders so EVERY survivor renders, not just the first.
//
// `championIds` is the set of gate-defender ids — the champion is the shared
// `left` of EVERY rung matchup (the benchmark, not a lane), so it is excluded
// from the field UNLESS it is itself part of the rung's authoritative
// survivors/cut (a settled `rung{N}` record ranks the champion among the field
// too, so it can legitimately survive a rung). Returns { field, liveProgress }:
// `field` is the de-duped lane list (survivors ∪ cut ∪ every matchup's lanes ∪
// the live_progress lane keys, in stable order) and `liveProgress` is the
// authoritative per-lane map lifted off the rung (the union across every match —
// `matches[0]` carries the full one, the rest are empty). PURE.
export function rungFullField(round, championIds) {
  const champ = championIds instanceof Set ? championIds : new Set(championIds || []);
  const matches = (round && Array.isArray(round.matches)) ? round.matches : [];
  // ids the SETTLED rung itself names — always lanes (the champion can survive a
  // rung, so a champion id present here is NOT excluded).
  const named = new Set();
  for (const m of matches) {
    for (const g of (Array.isArray(m && m.survivors) ? m.survivors : [])) named.add(String(g));
    for (const g of (Array.isArray(m && m.cut) ? m.cut : [])) named.add(String(g));
  }
  const seen = new Set();
  const field = [];
  const add = (id) => {
    const s = String(id);
    if (!s || seen.has(s)) return;
    // exclude a pure gate-defender (champion id NOT in the rung's own
    // survivors/cut — i.e. the repeated `left` benchmark of a live rung).
    if (champ.has(s) && !named.has(s)) return;
    seen.add(s); field.push(s);
  };
  // 1) the authoritative survivors + cut (the settled rung outcome) lead so a
  //    settled rung's field is exactly its ranked competitors.
  for (const m of matches) {
    for (const g of (Array.isArray(m && m.survivors) ? m.survivors : [])) add(g);
    for (const g of (Array.isArray(m && m.cut) ? m.cut : [])) add(g);
  }
  // 2) the union of every matchup's competitors (the per-duel challenger lanes —
  //    `rung{N}_m0`'s right side, `rung{N}_m1`'s, …; the in-flight case).
  for (const m of matches) {
    for (const g of (Array.isArray(m && m.competitors) ? m.competitors : [])) add(g);
  }
  // 3) the authoritative full-rung live_progress lane keys — the SOURCE OF TRUTH
  //    for the rung field while in flight (a still-pending duel whose challenger
  //    has not surfaced on a per-duel match still appears as a live_progress
  //    lane). Union across matches (matches[0] carries the full one).
  const liveProgress = {};
  for (const m of matches) {
    const lp = (m && m.live_progress && typeof m.live_progress === 'object') ? m.live_progress : null;
    if (!lp) continue;
    for (const k of Object.keys(lp)) { liveProgress[k] = lp[k]; add(k); }
  }
  return { field, liveProgress: Object.keys(liveProgress).length ? liveProgress : null };
}

// ---- racing — the rung/gate model from a normalized payload ────────
// gateState ∈ 'crowned' | 'stands' | 'deciding' | 'pending'.
export function racingModel(st) {
  if (!st || String(st.structure) !== 'racing') return null;
  const live = !!st.live;
  const rounds = Array.isArray(st.rounds) ? st.rounds : [];
  const lineage = Array.isArray(st.champion_lineage) ? st.champion_lineage.map(String) : [];
  const firstMatch = (r) => (r && Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : {};
  const isFinal = (mid) => String(mid || '') === 'racing-final';
  const gateRound = rounds.find((r) => isFinal(firstMatch(r).match_id)) || null;
  const rungRounds = rounds.filter((r) => !isFinal(firstMatch(r).match_id));

  // the champion/benchmark id(s) — the gate defender, never a rung lane: from
  // competitors (role/side === champion), the published gate competitors, and
  // the lineage head.
  const championIds = (() => {
    const ids = new Set();
    for (const c of (Array.isArray(st.competitors) ? st.competitors : [])) {
      if (c && c.generation_id != null && String(c.role || c.side || '').toLowerCase() === 'champion') ids.add(String(c.generation_id));
    }
    const gm = gateRound ? firstMatch(gateRound) : null;
    if (gm && Array.isArray(gm.competitors)) for (const g of gm.competitors) ids.add(String(g));
    if (lineage.length) ids.add(lineage[lineage.length - 1]);
    return ids;
  })();
  // the FULL challenger field — challengers from competitors + entries, minus
  // the champion(s). Used to WIDEN a degenerate live entering rung (issue #8).
  const challengerField = (() => {
    const seen = new Set();
    const out = [];
    const add = (id) => { const s = String(id); if (s && !seen.has(s) && !championIds.has(s)) { seen.add(s); out.push(s); } };
    for (const c of (Array.isArray(st.competitors) ? st.competitors : [])) {
      if (c && c.generation_id != null && String(c.role || c.side || '').toLowerCase() !== 'champion') add(c.generation_id);
    }
    for (const e of (Array.isArray(st.entries) ? st.entries : [])) {
      if (!e) continue;
      const side = String(e.side || e.role || '').toLowerCase();
      if (side === 'champion' || side === 'parent') continue;
      const id = e.entry_id != null ? e.entry_id : e.generation_id;
      if (id != null) add(id);
    }
    return out;
  })();

  const rungs = rungRounds.map((r, ri) => {
    const m = firstMatch(r);
    // a rung's lanes are its NON-champion competitors. A live rung is published
    // as N matchups (champion vs EACH survivor); the rung's TRUE field is the
    // UNION across every matchup + the authoritative full-rung live_progress
    // lane keys — NOT just matches[0]'s `[champion, challenger0]`. So a rung with
    // survivors v5+v7 yields a field of {v5, v7}, both fed to the builder.
    const { field: unionField, liveProgress } = rungFullField(r, championIds);
    let competitors = unionField;
    const survivors = Array.isArray(m.survivors) ? m.survivors : [];
    const cut = Array.isArray(m.cut) ? m.cut : [];
    const pending = !(survivors.length) && !(cut.length);
    // ISSUE #8: a LIVE, entering (rung-0), still-pending rung whose published
    // field is a degenerate subset of the real challenger field is WIDENED to
    // the whole field so every challenger races (≥ the full field), never just
    // champion + first challenger.
    if (live && ri === 0 && pending && challengerField.length > competitors.length
      && competitors.every((g) => challengerField.indexOf(g) >= 0)) {
      const merged = challengerField.slice();
      for (const g of competitors) if (merged.indexOf(g) < 0) merged.push(g);
      competitors = merged;
    }
    return {
      label: r.label || `Rung ${(r.round_index || 0) + 1}`,
      match_id: m.match_id,
      competitors,
      survivors,
      cut,
      deltas: (m.deltas && typeof m.deltas === 'object') ? m.deltas : null,
      board_fraction: svg.isNum(m.board_fraction) ? m.board_fraction : null,
      live_progress: liveProgress,
      pending,
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

  // THE CHAMPION'S ABSOLUTE SCALAR — the benchmark line the racing scalar track
  // anchors on, and the reference a competitor's absolute scalar is recovered
  // from (championScalar + Δ-vs-champion). Additive (live.js ignores it).
  const championScalar = championScalarOf(st, benchmarkId);

  return { rungs, championId, benchmarkId, championScalar, gateState, gateDelta, live, hasRungs: rungs.length > 0 };
}

// Recover the champion/benchmark's ABSOLUTE scalar from a normalized structure
// payload — the anchor for the racing scalar track + gauntlet field bars (a
// competitor's absolute scalar is championScalar + its Δ-vs-champion). Reads, in
// order: the benchmark's settled standings scalar; the live partial champion
// aggregate (`partial_champion_agg.scalar`); the live projected champion row;
// and finally the benchmark's strategy-seeded champion lane on an in-flight rung
// (`live_progress[bid].projected_scalar`). Returns null when the champion scalar
// is genuinely unknown (the builders then fall back to a delta-only domain), so
// no fabricated benchmark (e.g. a 10.000-style default) ever leaks. PURE.
export function championScalarOf(st, benchmarkId) {
  if (!st || typeof st !== 'object') return null;
  const bid = benchmarkId != null ? String(benchmarkId) : null;
  const standings = Array.isArray(st.standings) ? st.standings : [];
  if (bid != null) {
    const row = standings.find((s) => s && String(s.generation_id) === bid && svg.isNum(s.scalar));
    if (row) return row.scalar;
  }
  // the champion is the only competitor whose role/status names it; its settled
  // standings scalar is the standard when the benchmark id was not resolved.
  const champRow = standings.find((s) => s && String(s.status || '').toLowerCase() === 'champion' && svg.isNum(s.scalar));
  if (champRow) return champRow.scalar;
  // LIVE: the runner publishes the running champion aggregate as a dict.
  const agg = st.partial_champion_agg;
  if (agg && typeof agg === 'object' && svg.isNum(agg.scalar)) return agg.scalar;
  // the projected champion scalar (live), if the runner wrote one for the bench.
  const proj = (bid != null && st.projected && typeof st.projected === 'object') ? st.projected[bid] : null;
  if (proj && typeof proj === 'object' && svg.isNum(proj.scalar)) return proj.scalar;
  // LIVE FALLBACK: the strategy-seeded champion lane on an in-flight rung. When
  // the per-board `partial_champion_agg` / `projected` map has not been written
  // yet (the operator's empty-agg case), the champion lane's `projected_scalar`
  // (seeded from the strategy's own champion scalar) is the REAL benchmark — read
  // it off any rung's `live_progress` so the dashed champion line shows the true
  // loss instead of being silently omitted.
  if (bid != null) {
    const rounds = Array.isArray(st.rounds) ? st.rounds : [];
    for (const r of rounds) {
      for (const m of (Array.isArray(r.matches) ? r.matches : [])) {
        const lp = (m && m.live_progress && typeof m.live_progress === 'object') ? m.live_progress : null;
        const lane = lp ? lp[bid] : null;
        if (lane && typeof lane === 'object' && svg.isNum(lane.projected_scalar)) return lane.projected_scalar;
      }
    }
  }
  return null;
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
// Merge a reconstructed lane (computed from active-runs + the projected map)
// with the strategy's AUTHORITATIVE published lane (racing B1 producer): the
// published projection / scalar / board-progress win when present; the
// active-run reconstruction supplies only what the publisher omitted (e.g. the
// running-board count, the partial Δ). A published lane that is in-flight stays
// in-flight even when no active-run row exists yet.
function mergeLaneProgress(computed, pub) {
  if (!pub || typeof pub !== 'object') return computed;
  const out = Object.assign({}, computed);
  if (pub.projected != null) out.projected = pub.projected;
  if (pub.projected_scalar != null) out.projected_scalar = pub.projected_scalar;
  if (pub.boards_done != null) out.boards_done = pub.boards_done;
  if (pub.boards_total != null) out.boards_total = pub.boards_total;
  if (pub.partialDelta != null && out.partialDelta == null) out.partialDelta = pub.partialDelta;
  if (pub.inflight) out.inflight = out.inflight || pub.inflight;
  return out;
}

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

  // ── ISSUE #8: the FULL challenger field for a racing run ──
  // The champion (v0) is the benchmark/gate defender, NOT a rung lane. Every
  // OTHER competitor is a challenger that races the entering (rung-0) field.
  // Derived from `competitors` (role/side !== champion) and `entries`
  // (side === challenger), unioned + de-duped, so a DEGENERATE published rung-0
  // (sparser than the real field — e.g. only the first challenger) can be
  // WIDENED to the whole field below. Order follows competitors, then entries.
  // the champion/benchmark id(s) — the gate defender(s), never a rung lane.
  // Drawn from competitors (role/side === champion), the champion-gate match
  // competitors (the published gate lists v0), and the lineage head.
  const racingChampions = (() => {
    if (!isRacing) return new Set();
    const ids = new Set();
    for (const c of competitors) {
      if (c && c.generation_id != null && String(c.role || c.side || '').toLowerCase() === 'champion') {
        ids.add(String(c.generation_id));
      }
    }
    for (const r of rawRounds) {
      const m = firstMatch(r);
      if (isFinal(m.match_id) && Array.isArray(m.competitors)) for (const g of m.competitors) ids.add(String(g));
    }
    const lineage = Array.isArray(at.champion_lineage) ? at.champion_lineage : [];
    if (lineage.length) ids.add(String(lineage[lineage.length - 1]));
    return ids;
  })();
  const racingChallengers = (() => {
    if (!isRacing) return [];
    const seen = new Set();
    const out = [];
    const add = (id) => { const s = String(id); if (s && !seen.has(s) && !racingChampions.has(s)) { seen.add(s); out.push(s); } };
    for (const c of competitors) {
      if (c && c.generation_id != null && String(c.role || c.side || '').toLowerCase() !== 'champion') add(c.generation_id);
    }
    const entries = Array.isArray(at.entries) ? at.entries : [];
    for (const e of entries) {
      if (!e) continue;
      const side = String(e.side || e.role || '').toLowerCase();
      const id = e.entry_id != null ? e.entry_id : e.generation_id;
      if (id == null) continue;
      // a champion/parent entry defends the gate — never a rung lane.
      if (side === 'champion' || side === 'parent') continue;
      add(id);
    }
    return out;
  })();

  // ── the strategy-seeded CHAMPION BENCHMARK off the raw published rungs ──
  // The champion is the gate defender, so the per-rung field-overlay below DROPS
  // its lane from the rebuilt `live_progress` (a champion is never a rung lane).
  // But its strategy-seeded `projected_scalar` (the real champion loss the field
  // races against) lives on the raw published `live_progress[champion]` — capture
  // it HERE, before the overlay discards it, so the benchmark line survives even
  // when the runner has not written `partial_champion_agg` yet (the operator's
  // empty-agg case). Used only as a FALLBACK seed for partial_champion_agg below.
  const seededChampScalar = (() => {
    if (!isRacing || !racingChampions.size) return null;
    for (const r of rawRounds) {
      for (const m of (Array.isArray(r.matches) ? r.matches : [])) {
        const lp = (m && m.live_progress && typeof m.live_progress === 'object') ? m.live_progress : null;
        if (!lp) continue;
        for (const cid of racingChampions) {
          const lane = lp[String(cid)];
          if (lane && typeof lane === 'object' && svg.isNum(lane.projected_scalar)) return lane.projected_scalar;
        }
      }
    }
    return null;
  })();

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
  // FIX: `partial_*_agg` is a DICT ({scalar, ...}), NOT a number — the old
  // `svg.isNum(at.partial_*_agg)` guard was ALWAYS false, so partialDelta was
  // dead. Read the `.scalar` off the dict.
  const aggScalar = (a) => (a && typeof a === 'object' && svg.isNum(a.scalar)) ? a.scalar : null;
  const champAgg = aggScalar(at.partial_champion_agg);
  const challAgg = aggScalar(at.partial_challenger_agg);
  const partialDelta = (challAgg != null && champAgg != null) ? (challAgg - champAgg) : null;

  // ── the live PROJECTED standing per in-flight competitor ──
  // `at.projected` is the runner's `{generation_id: {scalar, boards_done,
  // boards_total, pass_rate}}` map, rewritten as each board lands. Read a
  // per-gen projection so a still-running competitor shows a climbing,
  // visibly-"projected" standing (dashed, ~prefix, scored sub-bar).
  const projectedMap = (at.projected && typeof at.projected === 'object') ? at.projected : {};
  const projFor = (gid) => {
    const p = projectedMap[String(gid)];
    return (p && typeof p === 'object' && svg.isNum(p.scalar)) ? p : null;
  };

  // a match is SETTLED when it carries a winner / decision (a racing rung is
  // settled once survivors/cut land; a bye settles a swiss/elim slot).
  const settled = (m) => !!(m.winner || m.decision
    || (Array.isArray(m.survivors) && m.survivors.length)
    || (Array.isArray(m.cut) && m.cut.length)
    || m.bye);

  // overlay in-flight board progress onto a still-pending PUBLISHED match.
  // `entering` marks the rung-0 (the first entering rung of a racing run) — its
  // field is WIDENED to the full challenger set when the publisher emitted a
  // degenerate subset (issue #8): the live funnel's first rung must show ALL
  // challengers racing, not just champion + first challenger.
  // `rungField` / `rungPublished` (racing only) carry the rung's AUTHORITATIVE
  // FULL field + the union published live_progress, computed over the WHOLE rung
  // round (all N champion-vs-survivor matchups) BEFORE the per-match split — so
  // the rung's slot-0 match becomes the single carrier of every lane (not just
  // matches[0]'s `[champion, challenger0]`). `slot0` marks that carrier match;
  // the other per-duel matches drop their live_progress (the rung is read off
  // slot 0). When omitted (swiss/elim/gate, or a degenerate single-match rung)
  // overlay falls back to the per-match field as before.
  const overlay = (m, queued, entering, opts) => {
    if (settled(m)) return m;
    const total = totalFor(m);
    // the champion-GATE (`racing-final`) is a 1v1 full-board duel, NOT a rung —
    // it carries both sides (champion + lone survivor). Route it through the
    // pairwise path (below) so both seats show their board progress + projected
    // scalar, reading the gate's OWN published `live_progress` (the per-board
    // `at.projected` map may carry only the survivor). Falling through the racing-
    // rung path would strip the champion seat (a rung excludes the champion) and,
    // for a non-carrier match, drop live_progress entirely.
    const isGateMatch = isRacing && isFinal(m.match_id);
    if (isRacing && !isGateMatch) {
      const o = opts || {};
      const slot0 = !!o.slot0;
      // a racing rung's field is its NON-champion lanes. The rung is published as
      // N matchups (champion vs EACH survivor); the rung's TRUE field is the union
      // across every matchup + the published live_progress lane keys (rungField),
      // attached to the slot-0 carrier match — NOT just this match's
      // `[champion, challenger0]`. Non-carrier matches keep their own single lane
      // but DROP live_progress (the rung is read off slot 0).
      let field = (slot0 && Array.isArray(o.rungField))
        ? o.rungField.slice()
        : (Array.isArray(m.competitors) ? m.competitors : []).map(String).filter((g) => !racingChampions.has(g));
      // ISSUE #8: widen a degenerate entering rung to the full challenger field
      // — only when every published lane IS a known challenger (never clobber a
      // legitimately-narrowed downstream rung) and the full field is larger.
      if (entering && racingChallengers.length > field.length
        && field.every((g) => racingChallengers.indexOf(g) >= 0)) {
        const merged = racingChallengers.slice();
        for (const g of field) if (merged.indexOf(g) < 0) merged.push(g);
        field = merged;
      }
      if (!slot0) {
        // a non-carrier per-duel match: keep its single lane, drop live_progress
        // (the whole rung's progress rides on the slot-0 carrier).
        return Object.assign({}, m, { competitors: field, winner: null, pending: true, queued, live_progress: null });
      }
      // the AUTHORITATIVE per-lane live_progress the strategy already published on
      // this rung (racing B1 producer / issue #16): the active-runs reconstruction
      // below only FILLS fields the publisher omitted — it must never clobber the
      // projected / projected_scalar / board-progress the backend already owns,
      // else the hero (which feeds through buildLiveModel) silently drops the live
      // projection the single-round figure shows. Use the UNION published map
      // (rungPublished) so EVERY lane's authoritative projection survives — not
      // only matches[0]'s `[champion, challenger0]` (the publisher pins the full
      // map on slot 0, whose competitors alone are a degenerate subset).
      const published = (o.rungPublished && typeof o.rungPublished === 'object') ? o.rungPublished
        : ((m.live_progress && typeof m.live_progress === 'object') ? m.live_progress : null);
      const progress = {};
      for (const g of field) {
        const inf = inflight.get(g);
        // PER-LANE projected: each lane's own server-side projected scalar (Δ
        // vs champion = lane − champion) beats the single champion/challenger
        // partialDelta when present. boards_done/boards_total drive the scored
        // sub-bar + mark the lane "projected" until its rung settles.
        const lp = projFor(g);
        const laneDelta = (lp != null && champAgg != null) ? (lp.scalar - champAgg) : partialDelta;
        const computed = inf
          ? { inflight: inf.count, done: Math.max(0, Math.floor(inf.sumProgress)), total, partialDelta: laneDelta,
              projected: lp != null, projected_scalar: lp != null ? lp.scalar : null,
              boards_done: lp != null ? lp.boards_done : null, boards_total: lp != null ? lp.boards_total : total }
          : { inflight: 0, done: 0, total, partialDelta: null,
              projected: lp != null, projected_scalar: lp != null ? lp.scalar : null,
              boards_done: lp != null ? lp.boards_done : null, boards_total: lp != null ? lp.boards_total : total };
        progress[g] = mergeLaneProgress(computed, published ? published[String(g)] : null);
      }
      return Object.assign({}, m, { competitors: field, winner: null, pending: true, queued, live_progress: queued ? null : progress });
    }
    // swiss / elim / racing-gate: a per-match done/inflight tally over the
    // pairing's gens, plus a per-competitor PROJECTED standing so an in-flight
    // pairing shows each side's climbing projected scalar (dashed/~prefix) before
    // the duel commits. The racing-gate reads its OWN published `live_progress`
    // (the per-board `at.projected` map may carry only the survivor, never the
    // champion seat), falling back to `at.projected` for swiss/elim.
    const comps = (Array.isArray(m.competitors) ? m.competitors : []).map(String);
    const gateLanes = (isGateMatch && m.live_progress && typeof m.live_progress === 'object') ? m.live_progress : null;
    let done = 0; let inf = 0;
    const projected = {};
    for (const g of comps) {
      const u = inflight.get(g); if (u) { inf += u.count; done += Math.floor(u.sumProgress); }
      if (queued) continue;
      // prefer the gate's own published lane, else the per-board projected map.
      const laneG = gateLanes ? gateLanes[g] : null;
      const lp = (laneG && typeof laneG === 'object' && svg.isNum(laneG.projected_scalar))
        ? { scalar: laneG.projected_scalar, boards_done: laneG.boards_done, boards_total: laneG.boards_total }
        : projFor(g);
      if (lp != null) projected[g] = { scalar: lp.scalar, boards_done: lp.boards_done, boards_total: lp.boards_total != null ? lp.boards_total : total };
    }
    return Object.assign({}, m, { winner: null, pending: true,
      inflight: queued ? 0 : inf, done: queued ? 0 : done, total, queued,
      projected: Object.keys(projected).length ? projected : null });
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

  // the index of the FIRST entering (non-gate) racing rung — the only rung
  // whose field may be widened to the full challenger set (issue #8).
  const firstRungIdx = isRacing
    ? rawRounds.findIndex((r) => !isFinal(firstMatch(r).match_id))
    : -1;

  const rounds = rawRounds.map((r, i) => {
    if (roundSettled(r)) return r;
    const m0 = firstMatch(r);
    // a racing round the backend is ALREADY streaming boards on (a non-empty
    // published live_progress) is inherently in-flight: never let a phase-derived
    // activeIdx mismatch (e.g. a "rungN" phase that disagrees with round_index)
    // suppress its authoritative live projection. This holds for the gate too —
    // once every rung has settled, the champion-gate IS the running round.
    const isGate = isRacing && isFinal(m0.match_id);
    const ri = svg.isNum(r.round_index) ? r.round_index : i;
    const streaming = isRacing
      && m0.live_progress && typeof m0.live_progress === 'object'
      && Object.keys(m0.live_progress).length > 0;
    // The gate becomes ACTIVE only once every preceding rung has settled (it must
    // not light up while a rung is still running), OR when the backend is already
    // streaming the gate's full-board duel / the phase points at `racing-final`.
    const gatePhase = isGate && /racing-final/.test(phase);
    const gateActive = isGate && (streaming || gatePhase || rawRounds.slice(0, i).every(roundSettled));
    const isActive = isGate
      ? gateActive
      : (activeIdx != null ? ri === activeIdx : rawRounds.slice(0, i).every(roundSettled));
    const queued = !isActive && !streaming;
    const entering = isRacing && !isGate && i === firstRungIdx;
    // ── RACING RUNG: compute the rung's AUTHORITATIVE full field + union
    // live_progress ONCE over the whole rung round (every champion-vs-survivor
    // matchup), then attach it to the slot-0 carrier match — so EVERY lane (every
    // survivor) renders with its published projection, not just matches[0]'s first
    // lane. The remaining per-duel matches keep their own lane but drop progress.
    let rungOpts = null;
    if (isRacing && !isGate) {
      const { field: rungField, liveProgress: rungPublished } = rungFullField(r, racingChampions);
      rungOpts = { rungField, rungPublished };
    }
    const matches = (Array.isArray(r.matches) ? r.matches : []).map((m, mi) =>
      overlay(m, queued, entering, rungOpts ? Object.assign({ slot0: mi === 0 }, rungOpts) : null));
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

  // ── OVERLAY the live PROJECTED standing onto the standings rows ──
  // A competitor IN a still-pending (active, not queued) match is IN FLIGHT;
  // overlay its server-side projected scalar + boards progress and mark the row
  // `in_flight` so the table renders the "projected" treatment (dashed row,
  // ~prefix, proj badge, scored sub-bar). Settled rows are left untouched.
  // Per-structure RANKING: elim/racing re-sort on the projected scalar (lower is
  // better) for the in-flight rows; SWISS does NOT project Copeland points — it
  // keeps the points-rank and only nudges the mean-scalar tiebreak. The backend
  // already does this server-side; the client mirror keeps the LIVE read honest
  // when the runner wrote `projected` AFTER the orchestrator's last publish.
  const inFlightGens = new Set();
  for (const r of rounds) {
    if (r.queued) continue;
    for (const m of (Array.isArray(r.matches) ? r.matches : [])) {
      if (m.queued || settled(m)) continue;
      for (const g of (Array.isArray(m.competitors) ? m.competitors : [])) inFlightGens.add(String(g));
    }
  }
  if (standings.length && inFlightGens.size) {
    standings = standings.map((s) => {
      const gid = String(s.generation_id != null ? s.generation_id : '');
      const lp = (gid && inFlightGens.has(gid)) ? projFor(gid) : null;
      if (lp == null) return s;
      return Object.assign({}, s, {
        in_flight: true, projected_scalar: lp.scalar,
        boards_done: lp.boards_done, boards_total: lp.boards_total != null ? lp.boards_total : null,
      });
    });
    const projKey = (s) => (s.in_flight && svg.isNum(s.projected_scalar)) ? s.projected_scalar
      : (svg.isNum(s.scalar) ? s.scalar : Infinity);
    if (structure === 'single_elim' || structure === 'double_elim' || structure === 'racing') {
      standings = standings.slice().sort((a, b) => projKey(a) - projKey(b));
      standings = standings.map((s, i) => Object.assign({}, s, { rank: i + 1 }));
    } else if (structure === 'swiss') {
      // points authoritative; projected scalar only breaks ties on equal wins.
      const w = (s) => svg.isNum(s.wins) ? s.wins : (svg.isNum(s.points) ? s.points : 0);
      standings = standings.slice().sort((a, b) => (w(b) - w(a)) || (projKey(a) - projKey(b)));
      standings = standings.map((s, i) => Object.assign({}, s, { rank: i + 1 }));
    }
  }

  return normalizeStructure({
    structure,
    structure_params: params,
    competitors,
    rounds,
    standings,
    champion_lineage: Array.isArray(at.champion_lineage) ? at.champion_lineage : [],
    // carry the live champion aggregate + projection map so championScalarOf can
    // anchor the scalar track / field bars mid-race (additive — see normalize).
    // When the runner has not written the aggregate yet, FALL BACK to the
    // strategy-seeded champion lane scalar (captured before the rung overlay
    // dropped the champion's lane), so the benchmark line shows the REAL champion
    // loss rather than being omitted — never a fabricated default.
    partial_champion_agg: (at.partial_champion_agg && typeof at.partial_champion_agg === 'object'
      && svg.isNum(at.partial_champion_agg.scalar))
      ? at.partial_champion_agg
      : (seededChampScalar != null ? { scalar: seededChampScalar } : null),
    projected: projectedMap && Object.keys(projectedMap).length ? projectedMap : null,
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

  // the champion/benchmark id(s) for a racing run — the shared gate-defender of
  // every rung matchup, excluded from a rung's lane FIELD (it is the benchmark,
  // not a competing lane). Drawn from competitors (role/side === champion), the
  // champion-gate match competitors, and the lineage head — the same derivation
  // racingModel uses so the rung block's field matches the figure's field.
  const racingChampions = (() => {
    if (!isRacing) return new Set();
    const ids = new Set();
    for (const c of (Array.isArray(model.competitors) ? model.competitors : [])) {
      if (c && c.generation_id != null && String(c.role || c.side || '').toLowerCase() === 'champion') ids.add(String(c.generation_id));
    }
    for (const r of rounds) {
      const m0 = (Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : {};
      if (isFinal(m0.match_id) && Array.isArray(m0.competitors)) for (const g of m0.competitors) ids.add(String(g));
    }
    const lineage = Array.isArray(model.champion_lineage) ? model.champion_lineage.map(String) : [];
    if (lineage.length) ids.add(lineage[lineage.length - 1]);
    return ids;
  })();

  const blocks = [];
  for (const r of rounds) {
    if (r.queued) continue; // a future, not-yet-started round carries no LIVE block.
    const matches = Array.isArray(r.matches) ? r.matches : [];
    // ── RACING: ONE block per RUNG (not one per champion-vs-survivor matchup) ──
    // A live rung is published as N matchups (`rung{N}_m0..mK`); buildLiveModel
    // splits the rung's lanes across those per-duel matches, each carrying only
    // its own lane. So we group the WHOLE rung round here and build a single block
    // from the rung's AUTHORITATIVE full field + the union live_progress
    // (rungFullField), feeding EVERY lane (every survivor) into ONE block — never
    // one degenerate block per matchup. A rung whose every matchup has SETTLED
    // (survivors/cut landed) is no longer in flight → no block.
    if (isRacing) {
      // the gate (`racing-final`) is a 1v1 duel, not a rung — fall through to the
      // pairwise path below for it.
      const m0 = matches[0] || {};
      if (!isFinal(m0.match_id)) {
        const liveMatches = matches.filter((m) => !m.queued && !settled(m));
        if (!liveMatches.length) continue; // the whole rung settled / is queued.
        const { field, liveProgress } = rungFullField({ matches: liveMatches }, racingChampions);
        const prog = liveProgress || {};
        const total = svg.isNum(m0.total) ? m0.total : null;
        const entries = field.map((g) => {
          const lp = prog[g] || {};
          return {
            id: g, done: svg.isNum(lp.done) ? lp.done : 0,
            total: svg.isNum(lp.total) ? lp.total : total,
            inflight: svg.isNum(lp.inflight) ? lp.inflight : 0,
            ratio: ratio(lp.done, lp.total != null ? lp.total : total),
            outcome: 'pending',
            // the live PROJECTED standing for this lane (when the runner has
            // landed at least one board) — drives the "~proj" treatment.
            projected: !!lp.projected,
            projected_scalar: svg.isNum(lp.projected_scalar) ? lp.projected_scalar : null,
            boards_done: svg.isNum(lp.boards_done) ? lp.boards_done : null,
            boards_total: svg.isNum(lp.boards_total) ? lp.boards_total : null,
          };
        });
        if (!entries.length) continue;
        // the rung's stable id is the bare `rung{N}` prefix (NOT `rung{N}_m0`) so
        // the dedup is keyed on the rung, and the digest stays stable as the per-
        // matchup decomposition shifts under it.
        const rungId = String(m0.match_id || '').replace(/_m\d+$/, '') || (`rung${svg.isNum(r.round_index) ? r.round_index : ''}`);
        blocks.push({
          kind: 'rung', structure, match_id: rungId,
          label: (r.label || `rung ${svg.isNum(r.round_index) ? r.round_index : ''}`).trim()
            + (entries.length ? ` · field of ${entries.length}` : ''),
          entries,
        });
      }
    }
    for (const m of matches) {
      if (m.queued) continue;        // a queued match in the active round is not running.
      if (settled(m)) continue;      // a settled match is no longer in flight.
      // racing rungs were emitted as ONE block above; only the gate (1v1) reaches
      // the pairwise path here.
      if (isRacing && !isFinal(m.match_id)) continue;
      const comps = (Array.isArray(m.competitors) ? m.competitors : []).map(String);
      // pairwise (swiss / elim / gauntlet / racing-gate): a two-sided duel. Split the per-match
      // done tally evenly across the two seats (the contract pins per-match
      // done/total, not per-seat), so each side reads the same board progress.
      const total = svg.isNum(m.total) ? m.total : null;
      const done = svg.isNum(m.done) ? m.done : 0;
      const seats = comps.length ? comps : ['tbd'];
      const mproj = (m.projected && typeof m.projected === 'object') ? m.projected : {};
      const entries = seats.slice(0, 2).map((g) => {
        const p = mproj[g];
        return {
          id: g, done, total, inflight: svg.isNum(m.inflight) ? m.inflight : 0,
          ratio: ratio(done, total), outcome: 'pending',
          projected: !!(p && svg.isNum(p.scalar)),
          projected_scalar: (p && svg.isNum(p.scalar)) ? p.scalar : null,
          boards_done: (p && svg.isNum(p.boards_done)) ? p.boards_done : null,
          boards_total: (p && svg.isNum(p.boards_total)) ? p.boards_total : null,
        };
      });
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
      // the PROJECTED standing — ROUNDED scalar + integer board counts so an
      // identical projection stays a no-op but a board landing repaints.
      e.projected ? 'j' + (svg.isNum(e.projected_scalar) ? e.projected_scalar.toFixed(3) : '?')
        + '/' + (e.boards_done == null ? '?' : e.boards_done) + '/' + (e.boards_total == null ? '?' : e.boards_total) : '',
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

  // The rung/gate model is the SINGLE source — racingModel builds each rung from
  // the FULL FIELD (the union of every rung matchup + the authoritative full-rung
  // live_progress), so an IN-FLIGHT rung published as N champion-vs-survivor
  // matchups renders ALL lanes (every survivor), not just matches[0]'s first
  // lane. The figures (scalar track + funnel) read straight off these rungs.
  const rm = racingModel(st) || {};
  const rungs = Array.isArray(rm.rungs) ? rm.rungs : [];
  const championId = rm.championId || null;
  const gateState = rm.gateState || (live ? 'deciding' : 'pending');
  const gateDelta = svg.isNum(rm.gateDelta) ? rm.gateDelta : null;
  const benchmarkId = rm.benchmarkId || null;
  const championScalar = svg.isNum(rm.championScalar) ? rm.championScalar : championScalarOf(st, benchmarkId);
  const openGen = (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); };

  // ── THE PRIMARY FIGURE: the SCALAR TRACK (racing.html opt 1) ─────────
  // Every gen on a shared scalar number-line; marker SIZE = inverse loss (bigger
  // = better) so the surviving leader is the fattest dot and the cuts shrink
  // away; the champion v0 is a dashed benchmark. The track honours all four
  // lifecycle states (the rungs carry live_progress per the live producer).
  const trackCard = el('div', { class: 'dn-panel dn-figpane' });
  // RENDER THE IN-FLIGHT RUNG: racingModel builds a rung (pending=true, full
  // field, live_progress) for a still-streaming rung even before any survivor/cut
  // commits — so `rungs.length > 0` and the scalar track renders ALL lanes
  // racing. The "No rungs evaluated yet." empty is reachable ONLY when there is
  // genuinely no rung in any source (no published/streaming rung, no completed
  // record) — never while a multi-survivor rung is in flight.
  trackCard.appendChild(rungs.length
    ? svg.racingScalarTrack({
        rungs, championId, benchmarkId, championScalar, live, gateState,
        responsive: true, onCompetitor: openGen,
      })
    : empty(live ? 'The race is being seeded — the first rung fills in as runs land.' : 'No rungs evaluated yet.'));
  if (rungs.length) {
    const gateNote = gateState === 'crowned' ? ` · champion-gate: ${championId} promoted ${CROWN.current}`
      : gateState === 'stands' ? ' · champion-gate: champion stands'
      : gateState === 'deciding' ? ' · champion-gate: deciding…'
      : '';
    trackCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      (benchmarkId ? `every gen is plotted on a shared scalar number-line (lower = better); the dashed line is the champion v0 = ${benchmarkId} benchmark · ` : '')
      + 'marker size = inverse loss (bigger = better) — the survivor is the fattest dot, the cuts shrink away past the cut tick · click a competitor → open'
      + gateNote
      + (live ? ' · LIVE — markers grow as boards land; the winner is not committed until the final gate' : '') }));
  }
  nodes.push(section(live ? 'Scalar track · LIVE — the field on one number-line (lower = better)' : 'Scalar track · the field on one number-line (lower = better)', trackCard));

  // ── SECONDARY: the SURVIVAL FUNNEL (the rung-by-rung FLOW view) ──────
  // The scalar track shows WHERE each gen lands on the loss axis; the funnel
  // shows the FLOW of the field narrowing rung-by-rung (who survived each cut,
  // who was eliminated, per-lane "k/N boards" live progress). It adds live value
  // the single-axis track cannot — the structural narrowing — so it rides below.
  if (rungs.length) {
    const flowCard = el('div', { class: 'dn-panel dn-figpane' });
    flowCard.appendChild(svg.survivalFunnel({
      rungs, championId, benchmarkId, live, gateState, gateDelta,
      responsive: true, onCompetitor: openGen,
    }));
    flowCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      'each rung races the field on a fraction of the board, then cuts the worst by η · ✕ = cut · ↑ = survives · ' + CROWN.current + ' = champion-gate winner'
      + (live ? ' · LIVE — in-flight lanes read "k/N boards"' : '') }));
    nodes.push(section(live ? 'Survival funnel · LIVE — field narrowing rung-by-rung' : 'Survival funnel · field narrowing rung-by-rung', flowCard));
  }

  const standings = standingsTable(st, ctx, epochId, live);
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// ---- gauntlet — the challenger-WAVE model (gauntlet.html opt 5) -----
//
// The structure-LEVEL gauntlet figure: ONE wave of challengers measured against
// the FIXED champion standard on a shared scalar axis (lower = better). Distinct
// from the gens.js match ladder (the per-duel list) — this is the field-at-a-
// glance read: who cleared the gate, by how much, who survived. Built PURELY
// from the normalized structure's rounds + standings + the promote_margin param.
//
// → { championId, championScalar, promoteMargin,
//     challengers:[{ id, scalar|delta, outcome:'cleared'|'failed'|'tied'|null,
//                    survivor, lane:{inflight,done,total,projected,
//                    projected_scalar,boards_done,boards_total} }],
//     live, hasField }
//
// FOUR lifecycle states (the gauntletFieldBars builder renders each): a queued
// challenger has no scalar (parked at the standard); an in-flight one carries a
// `lane` board tally; a projected one carries lane.projected_scalar; a settled
// one carries its committed scalar + outcome + survivor mark. The settled render
// is byte-identical via the live or the completed path (the lane chrome drops
// once a scalar commits).
export function gauntletModel(st) {
  if (!st || String(st.structure || 'gauntlet') !== 'gauntlet') return null;
  const live = !!st.live;
  const rounds = Array.isArray(st.rounds) ? st.rounds : [];
  const standings = Array.isArray(st.standings) ? st.standings : [];
  const competitors = Array.isArray(st.competitors) ? st.competitors : [];
  const lineage = Array.isArray(st.champion_lineage) ? st.champion_lineage.map(String) : [];
  const params = (st.structure_params && typeof st.structure_params === 'object') ? st.structure_params : {};

  // the champion id (the standard the wave is measured against) + its scalar.
  let championId = null;
  const champComp = competitors.find((c) => c && String(c.role || c.side || '').toLowerCase() === 'champion');
  if (champComp && champComp.generation_id != null) championId = String(champComp.generation_id);
  if (!championId) {
    const champStand = standings.find((s) => s && String(s.status || '').toLowerCase() === 'champion');
    if (champStand && champStand.generation_id != null) championId = String(champStand.generation_id);
  }
  if (!championId && lineage.length) championId = lineage[0];
  const championScalar = championScalarOf(st, championId);

  // the promote gate margin (gate = championScalar − margin, lower-is-better).
  const promoteMargin = svg.isNum(params.promote_margin) ? params.promote_margin
    : (svg.isNum(params.margin) ? params.margin : null);

  // settled standings (id → row) so the wave reads each challenger's scalar +
  // outcome from the authoritative standings when the rounds are sparse.
  const standById = new Map();
  for (const s of standings) if (s && s.generation_id != null) standById.set(String(s.generation_id), s);

  // collect the wave's challenger ids — every non-champion that played a match,
  // appears in standings, or is a non-champion competitor. Order: competitors,
  // then any standings/round id not yet seen (a stable, deterministic order).
  const order = [];
  const seen = new Set();
  const addId = (id) => { const k = String(id); if (k && k !== championId && !seen.has(k)) { seen.add(k); order.push(k); } };
  for (const c of competitors) if (c && c.generation_id != null) addId(c.generation_id);
  for (const r of rounds) for (const m of (Array.isArray(r.matches) ? r.matches : [])) {
    for (const g of (Array.isArray(m.competitors) ? m.competitors : [])) addId(g);
  }
  for (const s of standings) if (s && s.generation_id != null) addId(s.generation_id);

  // pull a challenger's per-match record (its duel vs the champion) from the
  // rounds — the match whose competitors include this id (a 1v1 wave).
  const matchFor = (id) => {
    for (const r of rounds) for (const m of (Array.isArray(r.matches) ? r.matches : [])) {
      const comps = (Array.isArray(m.competitors) ? m.competitors : []).map(String);
      if (comps.indexOf(id) >= 0) return m;
    }
    return null;
  };

  const challengers = order.map((id) => {
    const m = matchFor(id);
    const s = standById.get(id) || null;
    // scalar: prefer the settled standings scalar, else recover from the match Δ
    // (champion + delta_scalar), else leave null (queued).
    let scalar = (s && svg.isNum(s.scalar)) ? s.scalar : null;
    let delta = (m && svg.isNum(m.delta_scalar)) ? m.delta_scalar : null;
    if (scalar == null && championScalar != null && delta != null) scalar = championScalar + delta;
    if (delta == null && scalar != null && championScalar != null) delta = scalar - championScalar;

    // outcome: an explicit gate decision wins; else derive from scalar vs gate.
    const dec = m ? String(m.decision || '').toLowerCase() : '';
    let outcome = null;
    if (dec === 'promoted') outcome = 'cleared';
    else if (dec === 'rejected') outcome = 'failed';
    else if (s && String(s.status || '').toLowerCase() === 'champion') outcome = 'cleared';
    else if (s && String(s.status || '').toLowerCase() === 'eliminated') outcome = 'failed';
    // a settled scalar with a known gate resolves the outcome when no decision.
    if (outcome == null && !live && svg.isNum(scalar) && championScalar != null) {
      const gate = promoteMargin != null ? championScalar - promoteMargin : championScalar;
      if (Math.abs(scalar - gate) < 1e-9) outcome = 'tied';
      else outcome = scalar < gate ? 'cleared' : 'failed';
    }
    const survivor = outcome === 'cleared'
      || (lineage.length && lineage[lineage.length - 1] === id && id !== championId);

    // LIVE lane: an in-flight (still-pending) match overlays a board tally +
    // projected standing; a settled challenger carries NO lane (byte-identical
    // settled render via either path).
    let lane = null;
    if (live && outcome == null) {
      const lp = (m && m.live_progress && typeof m.live_progress === 'object') ? m.live_progress[id] : null;
      const inFlight = !!(s && s.in_flight);
      if (lp || inFlight || (m && (svg.isNum(m.done) || svg.isNum(m.inflight) || m.pending))) {
        const projScalar = lp && svg.isNum(lp.projected_scalar) ? lp.projected_scalar
          : (s && svg.isNum(s.projected_scalar) ? s.projected_scalar : null);
        lane = {
          inflight: lp && svg.isNum(lp.inflight) ? lp.inflight : (m && svg.isNum(m.inflight) ? m.inflight : 0),
          done: lp && svg.isNum(lp.done) ? lp.done : (m && svg.isNum(m.done) ? m.done : 0),
          total: lp && svg.isNum(lp.total) ? lp.total : (m && svg.isNum(m.total) ? m.total : null),
          projected: projScalar != null,
          projected_scalar: projScalar,
          boards_done: lp && svg.isNum(lp.boards_done) ? lp.boards_done : (s && svg.isNum(s.boards_done) ? s.boards_done : null),
          boards_total: lp && svg.isNum(lp.boards_total) ? lp.boards_total : (s && svg.isNum(s.boards_total) ? s.boards_total : null),
        };
        // a live, still-running challenger has no committed scalar — let the
        // projected scalar (if any) drive its plotted x via the builder.
        if (projScalar != null && scalar == null) { /* builder reads lane.projected_scalar */ }
        else if (scalar == null) { delta = null; }
      }
    }
    const out = { id, outcome, survivor, lane };
    if (svg.isNum(scalar)) out.scalar = scalar;
    else if (svg.isNum(delta)) out.delta = delta;
    return out;
  });

  return {
    championId, championScalar, promoteMargin, challengers, live,
    hasField: challengers.length > 0,
  };
}

// A stable digest of the gauntlet wave model — composed into the gauntlet
// section so the gated swap fires on a real change but stays stable on a no-op
// heartbeat. (structureDigest already covers rounds/standings/competitors; this
// folds the DERIVED wave fields the figure actually draws.)
export function gauntletModelDigest(model) {
  if (!model || typeof model !== 'object') return 'no-gauntlet';
  return svg.gauntletFieldBarsDigest({
    championId: model.championId, championScalar: model.championScalar,
    promoteMargin: model.promoteMargin, challengers: model.challengers,
  });
}

// gauntlet — the structure-LEVEL field-bars figure (gauntlet.html opt 5). One
// wave of challengers vs the champion standard on a shared scalar axis, the gate
// threshold line, outcome colours, survivor marks, the projected ghost. This is
// ADDED alongside (not in place of) the gens.js match ladder.
function renderGauntlet(st, ctx, epochId) {
  const nodes = [];
  const live = !!(st && st.live);
  const model = gauntletModel(st) || { challengers: [], live, hasField: false };
  const openGen = (gen) => { if (gen) ctx.navigate('candidate', { epochId, gen }); };

  const card = el('div', { class: 'dn-panel dn-figpane' });
  card.appendChild(model.hasField
    ? svg.gauntletFieldBars({
        championId: model.championId, championScalar: model.championScalar,
        promoteMargin: model.promoteMargin, challengers: model.challengers,
        live: model.live, onCompetitor: openGen,
      })
    : empty(live ? 'The gauntlet is being seeded — challengers fill in as runs land.' : 'No challengers recorded for this gauntlet.'));
  if (model.hasField) {
    card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text:
      (model.championId ? `the wave is measured against the champion standard = ${model.championId}` + (svg.isNum(model.championScalar) ? ` (${svg.fmt(model.championScalar, 2)})` : '') + ' · ' : '')
      + 'each bar runs from the standard out to a challenger’s scalar (lower = better); a bar that clears the dashed promote gate reads ↑ survivor · ✕ = failed the gate · click a challenger → open'
      + (live ? ' · LIVE — in-flight challengers ghost in with a "k/N boards" sub-bar; the winner is not committed until the gate' : '') }));
  }
  nodes.push(section(live ? 'Gauntlet field · LIVE — the wave vs the champion standard' : 'Gauntlet field · the wave vs the champion standard', card));

  const standings = standingsTable(st, ctx, epochId, live);
  if (standings) nodes.push(section('Standings', standings));
  return nodes;
}

// ---- shared: the standings leaderboard table -----------------------

// Resolve a standing's override prov: the DURABLE readback wins; else the
// operator's optimistic queued stamp; else — on a SETTLED round whose queued
// promote never landed in the advanced set — the DRAINED state (queued, never
// fired). null when neither exists (back-compat clean).
function resolveOverrideProv(gid, durable, pending, settled, promotedSet) {
  if (durable) return durable;
  if (!pending) return null;
  if (settled && pending.action === 'promote' && promotedSet && !promotedSet.has(String(gid))) {
    return { action: pending.action, reason: pending.reason, state: 'drained' };
  }
  return pending;
}

function standingsTable(st, ctx, epochId, live) {
  const standings = (st && Array.isArray(st.standings)) ? st.standings.slice() : [];
  if (!standings.length) return null;
  const structure = (st && st.structure) || 'gauntlet';
  // track whether any row resolves to the DEFERRED status-pill state (still in
  // contention — no crown / no elimination committed) so the table can carry an
  // explicit field-level "deferred · winner resolves after the duels" caption,
  // making a held-but-not-rejected field read intentionally rather than blank.
  let anyDeferred = false;
  // operator-override readback (durable field record): {gid: {action, ts,
  // reason, state}}. KEY-ABSENT on every gate-decided / single-challenger /
  // pre-feature run → no chip → byte-identical to today.
  const overrides = (st && st.override_status && typeof st.override_status === 'object') ? st.override_status : null;
  // per-slot diversity status (field_status[].diversity_status ∈ applied /
  // penalized / soft_rejected), keyed by generation_id for a per-row badge. Only
  // attached for a real field with the diversity block (≥2 challengers) → no
  // badge on a gauntlet / single-challenger / pre-feature run (byte-identical).
  const divStatus = diversityStatusByGen(st);
  // the advanced SET at settle (supports MULTIPLE promoted / ties) — resolves the
  // DRAINED state for an optimistic stamp that never landed.
  const promotedSet = (st && Array.isArray(st.promoted_generation_ids))
    ? new Set(st.promoted_generation_ids.map((g) => String(g))) : null;
  // the CONTROL plane: a live field accepts operator overrides; a read-only
  // workspace shows the control DISABLED (never POST-and-fail). The POST body
  // names the field round so the readback can attribute it.
  const settled = !live;
  const readOnly = !!(state.health && state.health.read_only);
  const tournamentId = (st && st.tournament_id != null) ? String(st.tournament_id) : null;
  const bodyBase = {};
  if (epochId != null) bodyBase.epoch = String(epochId);
  if (tournamentId) bodyBase.tournament_id = tournamentId;
  if (structure) bodyBase.structure = String(structure);
  const onPost = (action, gid, reason) =>
    postFieldOverride(action, gid, Object.assign({}, bodyBase, reason ? { reason } : {}));
  const onChange = () => { if (state && typeof state._changed === 'function') state._changed(); };
  // Racing (successive-halving / best-arm) has NO head-to-head winner/loser —
  // each rung ranks survivors by SCALAR and cuts the worst; the promote/reject
  // is the gate, not a match record. So W/L are structurally always 0 for
  // racing and a permanently-zero column reads as broken. Drop W/L for racing
  // (scalar + status carry the standing); keep them for the bracket structures
  // that actually populate them (single_elim / double_elim / swiss).
  const showWL = structure !== 'racing';
  standings.sort((a, b) => (svg.isNum(a.rank) ? a.rank : 1e9) - (svg.isNum(b.rank) ? b.rank : 1e9));
  const tbl = el('table', { class: 'dn-board-table dt-standings' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'rank' }), el('th', { text: 'generation' }), el('th', { text: 'status' }),
    el('th', { class: 'dn-num', text: 'scalar' }),
    ...(showWL ? [el('th', { class: 'dn-num', text: 'W' }), el('th', { class: 'dn-num', text: 'L' })] : []),
    el('th', { text: '' }),
    el('th', { class: 'dn-ovr-col', text: 'override' }),
  ])]));
  const tbody = el('tbody');
  // running tally for the field-level caption ('gate said X · operator forced Y')
  const forced = { promote: 0, reject: 0, drained: 0 };
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
    // the statusPill verdict-state mirror: anything that is NOT a committed
    // champion / eliminated reads as the DEFERRED pill (still in contention).
    if (status !== 'champion' && status !== 'eliminated') anyDeferred = true;
    // PROJECTED — an in-flight row (boards still streaming) shows a projected
    // scalar, not a settled one: dashed/dimmed row + a "proj" badge + the
    // ~prefix on the number + a scored board-progress sub-bar.
    const proj = !!(s.in_flight && svg.isNum(s.projected_scalar));
    const rowCls = (status === 'champion' ? 'dn-board-champ' : status === 'eliminated' ? 'dt-standings-out' : '')
      + (proj ? ' dt-proj-row' : '');
    const bd = svg.isNum(s.boards_done) ? s.boards_done : null;
    const bt = svg.isNum(s.boards_total) ? s.boards_total : null;
    const frac = (bd != null && bt != null && bt > 0) ? Math.min(1, bd / bt) : null;
    const scalarCell = proj
      ? el('td', { class: 'dn-num dn-mono dt-proj-val', title: 'projected — boards still streaming in' }, [
          el('span', { text: '~' + svg.fmt(s.projected_scalar, 1) }),
          el('span', { class: 'dt-proj-badge', text: 'proj' }),
        ])
      : el('td', { class: 'dn-num dn-mono', text: svg.isNum(s.scalar) ? svg.fmt(s.scalar, 1) : '—' });
    // operator-override provenance rides BESIDE the status pill (overrideChip),
    // never recoloring the verdict — durable readback wins, else the optimistic
    // queued stamp, else (settled never-landed promote) drained. Absent → null.
    const gidStr = String(s.generation_id);
    const durable = overrides ? overrides[gidStr] : null;
    // once the durable readback carries this override, drop the optimistic stamp
    // so they never double up (the readback is now authoritative).
    if (durable) clearPendingOverride(gidStr);
    const ovProv = resolveOverrideProv(gidStr, durable, pendingOverride(gidStr), settled, promotedSet);
    if (ovProv) {
      const a = String(ovProv.action || '');
      if (String(ovProv.state || 'applied') === 'drained') forced.drained += 1;
      else if (a === 'promote') forced.promote += 1;
      else if (a === 'reject') forced.reject += 1;
    }
    const ovChip = overrideChip(ovProv);
    if (ovChip && ovProv) {
      const act = ovProv.action === 'promote' ? 'force-promoted' : 'force-rejected';
      attachHovercard(ovChip, () => el('div', { class: 'dn-hc-body' }, [
        el('div', { class: 'dn-hc-title', text: 'operator override · ' + act }),
        (typeof ovProv.reason === 'string' && ovProv.reason)
          ? el('div', { class: 'dn-hc-row', text: ovProv.reason })
          : el('div', { class: 'dn-hc-row dn-faint', text: 'no reason recorded' }),
      ]));
    }
    // the per-challenger override CONTROL cell (confirm-inline arm→reason→POST,
    // optimistic queued stamp, disabled when read_only/settled/overridden).
    // existingOverride = the durable readback only (the cell reads its own stamp).
    const ctlCell = s.generation_id ? overrideControlCell({
      gid: gidStr, epochId, tournamentId, structure,
      readOnly, settled, existingOverride: durable, onPost, onChange,
    }) : null;
    // the per-row diversity badge — soft-rejected reuses the DEFERRED pill
    // (held, not promoted); penalized reads as a caution chip. Absent → null.
    const divBadge = diversityBadge(divStatus ? divStatus[gidStr] : null);
    tbody.appendChild(el('tr', { class: rowCls }, [
      el('td', { class: 'dn-mono', text: svg.isNum(s.rank) ? String(s.rank) : '—' }),
      el('td', { class: 'dn-mono', text: (s.generation_id || '—') + (status === 'champion' ? ' ' + CROWN.current : '') }),
      el('td', null, [statusPill(status), ovChip, divBadge].filter(Boolean)),
      scalarCell,
      ...(showWL ? [
        el('td', { class: 'dn-num dn-mono', text: svg.isNum(s.wins) ? String(s.wins) : '—' }),
        el('td', { class: 'dn-num dn-mono', text: svg.isNum(s.losses) ? String(s.losses) : '—' }),
      ] : []),
      el('td', null, [
        proj && frac != null ? el('span', { class: 'dt-proj-bar', title: bd + '/' + bt + ' boards scored' }, [
          el('span', { class: 'dt-proj-bar-fill', style: 'width:' + Math.round(frac * 100) + '%;' }),
        ]) : null,
        proj && frac != null ? el('span', { class: 'dt-proj-bar-lab', text: bd + '/' + bt }) : null,
        s.generation_id ? el('a', { class: 'dn-linkbtn', href: ctx.href('candidate', { epochId, gen: s.generation_id }), text: 'open →' }) : null,
      ].filter(Boolean)),
      el('td', { class: 'dn-ovr-col' }, [ctlCell].filter(Boolean)),
    ]));
  }
  tbl.appendChild(tbody);
  const caps = [];
  // a field-level DEFERRED caption — when at least one standing is held in
  // contention (the deferred pill state) and nothing has yet been crowned /
  // eliminated, surface WHY the field reads unsettled: the winner resolves once
  // the duels separate the strengths. Only while LIVE (an uncommitted run) and
  // only when no terminal verdict has landed, so a settled board stays quiet.
  const anyTerminal = standings.some((s) => {
    const r = String(s.status || '').toLowerCase();
    return r === 'champion' || r === 'eliminated';
  });
  if (live && anyDeferred && !anyTerminal) {
    caps.push(el('p', { class: 'dn-faint dt-standings-deferred', style: 'font-size:11px;margin:8px 0 0;',
      text: 'deferred — no winner committed yet · the standing resolves once the duels separate the strengths (held, not rejected)' }));
  }
  // the OVERRIDE PROVENANCE caption — 'gate said X · operator forced Y' — reads
  // only when an override is present (durable/queued/drained); a clean gate-
  // decided field stays byte-identical.
  if (forced.promote || forced.reject || forced.drained) {
    const verbs = [];
    if (forced.promote) verbs.push('forced ' + forced.promote + (forced.promote > 1 ? ' promotions' : ' promotion'));
    if (forced.reject) verbs.push('forced ' + forced.reject + (forced.reject > 1 ? ' rejections' : ' rejection'));
    if (forced.drained) verbs.push(forced.drained + ' queued ' + (forced.drained > 1 ? 'overrides' : 'override') + ' drained (never fired)');
    caps.push(el('p', { class: 'dn-faint dt-standings-override', style: 'font-size:11px;margin:6px 0 0;',
      text: 'gate said settle on the standings · operator ' + verbs.join(' · ') }));
  }
  if (caps.length) return el('div', { class: 'dt-standings-wrap' }, [tbl, ...caps]);
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

// {gid: diversity_status} off the field_status records, ONLY when the diversity
// block is attached (a real ≥2-challenger field). Absent / single-challenger /
// pre-feature → null → no per-row badge (byte-identical to today).
function diversityStatusByGen(st) {
  if (!st || !st.diversity || !Array.isArray(st.field_status)) return null;
  const by = {};
  let any = false;
  for (const f of st.field_status) {
    if (!f || typeof f !== 'object' || f.generation_id == null) continue;
    const ds = f.diversity_status;
    if (ds === 'soft_rejected' || ds === 'penalized') { by[String(f.generation_id)] = ds; any = true; }
  }
  return any ? by : null;
}

// The per-row diversity badge. `soft_rejected` reuses the DEFERRED pill (held,
// not promoted — the field's most legible "this idea was cut for overlap"
// signal); `penalized` is a softer caution chip. `applied` / absent → null (no
// badge), so a clean diverse field is byte-identical to today.
function diversityBadge(ds) {
  if (ds === 'soft_rejected') {
    const p = verdictPill('deferred');
    p.textContent = 'soft-rejected';
    p.setAttribute('class', (p.getAttribute('class') || '') + ' dn-div-softrej');
    attachHovercard(p, () => el('div', { class: 'dn-hc-body' }, [
      el('div', { class: 'dn-hc-title', text: 'diversity · soft-rejected' }),
      el('div', { class: 'dn-hc-row dn-faint', text: 'idea overlap exceeded the diversity tolerance — held out of the field (not gate-rejected)' }),
    ]));
    return p;
  }
  if (ds === 'penalized') {
    const c = el('span', { class: 'dn-chip dn-chip-live dn-div-penalized', text: 'div-penalized' });
    attachHovercard(c, () => el('div', { class: 'dn-hc-body' }, [
      el('div', { class: 'dn-hc-title', text: 'diversity · penalized' }),
      el('div', { class: 'dn-hc-row dn-faint', text: 'idea overlap incurred a diversity penalty but the challenger still entered the field' }),
    ]));
    return c;
  }
  return null;
}

function linkGen(gen, ctx, epochId) {
  if (!gen) return el('span', { class: 'dn-faint', text: 'bye' });
  return el('a', { class: 'dn-linkbtn dn-mono', href: ctx.href('candidate', { epochId, gen }), text: String(gen) });
}
