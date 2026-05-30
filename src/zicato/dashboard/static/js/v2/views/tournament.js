// js/v2/views/tournament.js — the Tournament view (DASHBOARD-V2 §3, §4.4).
//
// The headline visual of v2: the tournament & promotion rendered as a
// Tufte slopegraph / bumps chart (NOT a table — the corrected §3 visual
// language). Rounds on x, scalar (loss) on y, lower = higher; the
// champion is the bold through-line, each challenger a slope into a
// matchup; PROMOTE joins the line (green), REJECT falls away (red),
// RUNNING pulses (amber). Interactive: hover a slope/node for the verdict
// + Δscalar + fired gate rule; click a matchup → the experiment, click a
// node → the experiment.
//
// Data build (the `rounds` shape the slopegraph consumes):
//   * The settled rounds come from `state.epochDef.experiments` — each
//     non-seed generation is a matchup vs its lineage parent. The
//     champion scalar is the parent generation's lineage scalar; the
//     challenger scalar is the child generation's lineage scalar (or,
//     when a rejected child never recorded a gen_score, the champion
//     scalar + the experiment's scalar_score_delta, so we never fabricate
//     a NaN). decision / Δscalar / firedRule come from the outcome.
//   * The live in-flight matchup is appended from /api/active-tournament
//     (the partial champion/challenger aggregate scalars), decision
//     'running'.
//
// Honest at 0 / 1 / few rounds; the slopegraph primitive owns the empty
// state. Deep-link safe: if `state.epochDef.experiments` is empty we
// ensure-load /api/epoch and re-render on arrival (mirrors epoch.js).
// Digest-gated so SSE heartbeats do not flash the chart.

import { $, el, clearChildren, swapIfChanged } from '../../core/dom.js';
import { fetchJson } from '../../core/api.js';
import { state } from '../../core/state.js';
import { registerView } from '../shell.js';
import { v2Router } from '../router.js';
import { slopegraph } from '../components/slopegraph.js';
import { stateBlock } from '../components/stateBlock.js';

// =====================================================================
// Deep-link ensure-load — mirror epoch.js. A cold deep-link to
// #/v2/tournament arrives before the env poll has folded epochDef, so we
// fetch /api/epoch once and re-render in place when it lands.
// =====================================================================
let _epochLoading = false;
let _epochLoaded = false;

export function resetTournamentView() {
  _epochLoading = false;
  _epochLoaded = false;
  _lastDigest = null;
}

function _repaint() {
  const host = $('v2-view');
  if (host) renderTournament(host, v2Router.current());
}

function ensureEpochLoaded() {
  // Already have the contract folded into state → nothing to do.
  const def = state.epochDef;
  if (def && Array.isArray(def.experiments) && def.experiments.length > 0) return;
  if (_epochLoaded || _epochLoading) return;
  _epochLoading = true;
  fetchJson('/api/epoch').then((data) => {
    if (data && typeof data === 'object') state.setEpochDef(data);
  }).catch(() => {
    // Tolerant: a missing epoch simply leaves the slopegraph's honest
    // empty state in place — not a broken view.
  }).finally(() => {
    _epochLoading = false;
    _epochLoaded = true;
    _repaint();
  });
}

// =====================================================================
// Outcome readers — tolerant of the experiment.json shape variants
// (kept in lockstep with epoch.js).
// =====================================================================
function _num(v) { return (typeof v === 'number' && isFinite(v)) ? v : null; }

function _decisionOf(exp) {
  const o = exp && exp.outcome;
  if (!o || typeof o !== 'object') return null;
  const raw = String(o.tournament_decision || o.decision || o.verdict || '').toLowerCase();
  if (raw.includes('promot') || raw === 'accepted') return 'promoted';
  if (raw.includes('reject')) return 'rejected';
  if (raw.includes('defer')) return 'rejected'; // a deferral did not join the line
  return raw || null;
}

function _isBaselineSeed(exp) {
  if (!exp || typeof exp !== 'object') return false;
  const parent = exp.parent_generation_id;
  if (typeof parent === 'string' && parent !== '') return false;
  return exp.outcome == null;
}

function _outcomeNum(exp, key) {
  const o = exp && exp.outcome;
  return (o && typeof o === 'object') ? _num(o[key]) : null;
}

const _RULE_PATTERNS = [
  [/scalar|margin/i, 'scalar_margin'],
  [/pass[\s_-]*rate|monotonic.*pass|pass.*monoton/i, 'pass_rate_monotonicity'],
  [/namespace/i, 'namespace_monotonicity'],
  [/budget|wall[\s_-]*clock|timeout/i, 'budget'],
];
function _firedRule(exp) {
  const o = exp && exp.outcome;
  if (o && typeof o === 'object') {
    for (const k of ['fired_gate_rule', 'gate_rule', 'fired_rule']) {
      if (typeof o[k] === 'string' && o[k].trim()) return o[k].trim();
    }
    const reason = typeof o.rejection_reason === 'string' ? o.rejection_reason : '';
    if (_decisionOf(exp) === 'rejected' && reason) {
      for (const [re, name] of _RULE_PATTERNS) if (re.test(reason)) return name;
      return 'rejected';
    }
  }
  return null;
}

// A map of generation id → absolute scalar, from the lineage (gen_score).
// The lineage carries `scalar` / `best_scalar` per generation; this is the
// y-axis source the spine already uses.
export function scalarByGeneration() {
  const lin = state.lineage || {};
  const gens = Array.isArray(lin.generations) ? lin.generations : [];
  const out = new Map();
  for (const g of gens) {
    if (!g) continue;
    const id = g.id != null ? String(g.id) : (g.generation_id != null ? String(g.generation_id) : null);
    if (!id) continue;
    const raw = g.scalar != null ? g.scalar : (g.best_scalar != null ? g.best_scalar : null);
    const v = _num(raw) != null ? _num(raw) : (raw != null && isFinite(Number(raw)) ? Number(raw) : null);
    if (v != null) out.set(id, v);
  }
  return out;
}

// =====================================================================
// rounds build — the slopegraph contract.
// =====================================================================
export function buildRounds(epochDef, lineageScalars, activeTournament) {
  const def = epochDef || {};
  const experiments = Array.isArray(def.experiments) ? def.experiments : [];
  const scalarOf = lineageScalars instanceof Map ? lineageScalars : new Map();

  const rounds = [];
  let roundIdx = 0;
  for (const exp of experiments) {
    if (!exp || typeof exp !== 'object') continue;
    if (_isBaselineSeed(exp)) continue; // the seed is the first champion, not a matchup
    const championId = exp.parent_generation_id ? String(exp.parent_generation_id) : '?';
    const challengerId = exp.generation_id ? String(exp.generation_id) : '?';
    const decision = _decisionOf(exp) === 'promoted' ? 'promoted' : 'rejected';
    const deltaScalar = _outcomeNum(exp, 'scalar_score_delta');

    // Champion scalar from the lineage; challenger from the lineage too,
    // else derived from champion + Δ so a rejected child with no recorded
    // gen_score still plots honestly (never NaN).
    const championScalar = scalarOf.has(championId) ? scalarOf.get(championId) : null;
    let challengerScalar = scalarOf.has(challengerId) ? scalarOf.get(challengerId) : null;
    if (challengerScalar == null && championScalar != null && deltaScalar != null) {
      challengerScalar = championScalar + deltaScalar;
    }

    rounds.push({
      round: roundIdx,
      champion: { id: championId, scalar: championScalar },
      challenger: { id: challengerId, scalar: challengerScalar },
      decision,
      deltaScalar,
      firedRule: _firedRule(exp),
    });
    roundIdx += 1;
  }

  // Append the live in-flight matchup, if any. Its scalars come from the
  // running partial aggregates; the challenger may have no scalar yet
  // (before the first board unit settles) — that is honest.
  const at = activeTournament;
  if (at && String(at.phase || '').toLowerCase() === 'running') {
    const championId = at.parent_generation_id != null ? String(at.parent_generation_id) : '?';
    const challengerId = at.child_generation_id != null ? String(at.child_generation_id) : '?';
    // Skip if this matchup is already the last settled round (avoids a
    // double-draw when the env poll and the active-tournament file briefly
    // disagree mid-transition).
    const last = rounds[rounds.length - 1];
    const dup = last && last.challenger.id === challengerId && last.champion.id === championId;
    if (!dup) {
      const champAgg = at.partial_champion_agg || {};
      const chalAgg = at.partial_challenger_agg || {};
      let championScalar = _num(champAgg.scalar);
      if (championScalar == null && scalarOf.has(championId)) championScalar = scalarOf.get(championId);
      const challengerScalar = _num(chalAgg.scalar);
      const deltaScalar = (championScalar != null && challengerScalar != null)
        ? challengerScalar - championScalar : null;
      rounds.push({
        round: rounds.length,
        champion: { id: championId, scalar: championScalar },
        challenger: { id: challengerId, scalar: challengerScalar },
        decision: 'running',
        deltaScalar,
        firedRule: null,
      });
    }
  }

  return rounds;
}

// A cheap digest of the rounds so a heartbeat that changes nothing the
// chart draws writes zero DOM.
function _digestRounds(rounds) {
  return JSON.stringify(rounds.map((r) => [
    r.round, r.decision,
    r.champion.id, r.champion.scalar == null ? null : Math.round(r.champion.scalar * 1e4),
    r.challenger.id, r.challenger.scalar == null ? null : Math.round(r.challenger.scalar * 1e4),
    r.deltaScalar == null ? null : Math.round(r.deltaScalar * 1e4),
    r.firedRule || '',
  ]));
}

let _lastDigest = null;

// =====================================================================
// The view.
// =====================================================================
export function renderTournament(host, route) {
  if (!host) return;

  // Ensure the epoch contract is loaded on a cold deep-link.
  ensureEpochLoaded();

  // Pull the live active-tournament: prefer state (folded by the env
  // poll / SSE), but kick a direct read once so a deep-link sees the live
  // matchup even before the first env poll lands.
  const at = state.activeTournament;
  if (at === undefined) {
    fetchJson('/api/active-tournament').then((t) => {
      state.activeTournament = t || null;
      _repaint();
    }).catch(() => {});
  }

  const epochDef = state.epochDef || null;
  const lineageScalars = scalarByGeneration();
  const rounds = buildRounds(epochDef, lineageScalars, state.activeTournament);
  const live = rounds.length > 0 && rounds[rounds.length - 1].decision === 'running';

  const epochId = (epochDef && epochDef.epoch_id)
    || (state.epoch && state.epoch.id !== '—' ? state.epoch.id : null) || null;

  const loadingContract = (!epochDef
    || !Array.isArray(epochDef.experiments)
    || epochDef.experiments.length === 0) && _epochLoading;

  const digest = [
    route && route.view,
    epochId || '',
    loadingContract ? 'loading' : 'ready',
    _digestRounds(rounds),
  ].join('|');
  if (digest === _lastDigest && host.firstChild) return;
  _lastDigest = digest;

  swapIfChanged(host, digest, () => {
    const wrap = el('div', { class: 'v2-tournament' });
    wrap.appendChild(el('div', { class: 'v2-tournament-head' }, [
      el('h1', { class: 'v2-view-title' }, ['Tournament']),
      el('p', { class: 'v2-tournament-sub' }, [
        'Champion vs challenger across rounds — the optimization descent. ',
        'A promotion joins the through-line; a rejection falls away. ',
        'Hover a slope for the verdict; click to drill into the experiment.',
      ]),
      epochId ? el('span', { class: 'v2-tournament-epoch v2-mono' }, [String(epochId)]) : null,
    ].filter(Boolean)));

    // Honest loading state on a cold deep-link before the contract lands.
    if (rounds.length === 0 && loadingContract) {
      wrap.appendChild(stateBlock('running', { label: 'Loading tournament' }));
      return wrap;
    }

    wrap.appendChild(slopegraph({
      rounds,
      live,
      onMatchup: (challengerId) => { if (challengerId) v2Router.go('experiment', challengerId); },
      onGeneration: (id) => { if (id) v2Router.go('experiment', id); },
    }));
    return wrap;
  });
}

// Self-register with the shell so the router can mount this. The integrator
// adds `tournament` to V2_VIEWS (router.js) and imports this module in
// app2.js — registerView no-ops gracefully until the view name is valid.
registerView('tournament', renderTournament);

void clearChildren;
