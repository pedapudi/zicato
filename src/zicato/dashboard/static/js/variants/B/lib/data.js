// variants/B/lib/data.js — shared data selectors + a tiny async-cache.
//
// Variant B reuses the v1/v2 data layer (core/state, core/api). The views
// read the same /api/* endpoints; this module concentrates the shape
// normalisation (decision parsing, lineage node mapping, hypothesis text,
// outcome deltas) so every view reads a generation the same way — and a
// small fetch-cache so each view can lazily pull a per-resource endpoint
// once and repaint when it lands.

import { fetchJson } from '../../../core/api.js';
import { state } from '../../../core/state.js';

// --- generation / lineage -------------------------------------------------
export function gens() {
  const lin = state.lineage || {};
  return Array.isArray(lin.generations) ? lin.generations : [];
}
export function genId(g) {
  if (!g) return null;
  const id = g.id != null ? g.id : g.generation_id;
  return id != null ? String(id) : null;
}
export function parentId(g) {
  if (!g) return null;
  const p = g.parent_id != null ? g.parent_id
    : (g.parentId != null ? g.parentId : g.parent_generation_id);
  return p != null ? String(p) : null;
}
export function findGen(id) {
  if (id == null) return null;
  const want = String(id);
  for (const g of gens()) if (genId(g) === want) return g;
  return null;
}
export function scalarOf(g) {
  if (!g) return null;
  const raw = g.scalar != null ? g.scalar : (g.best_scalar != null ? g.best_scalar : null);
  if (typeof raw === 'number' && isFinite(raw)) return raw;
  if (raw != null && isFinite(Number(raw))) return Number(raw);
  return null;
}

export function verdictKey(raw) {
  const v = String(raw == null ? '' : raw).toLowerCase();
  if (v.startsWith('prom') || v === 'accepted' || v === 'true') return 'promoted';
  if (v.startsWith('rej') || v === 'false') return 'rejected';
  if (v.startsWith('defer')) return 'deferred';
  return 'open';
}

// Map state.lineage into trajectory nodes the chart consumes.
export function lineageNodes() {
  const live = liveChallengerId();
  const nodes = gens().filter((g) => genId(g) != null).map((g) => {
    const v = (g.promoted === true) ? 'promoted'
      : (g.promoted === false) ? 'rejected'
        : verdictKey(g.verdict || g.outcome || g.tournament_decision);
    return {
      id: genId(g),
      parentId: parentId(g),
      scalar: scalarOf(g),
      verdict: v,
      live: live != null && genId(g) === String(live),
      label: genId(g),
    };
  });
  if (live != null && !nodes.some((n) => n.live)) {
    const champ = state.activeTournament
      && (state.activeTournament.champion_id || state.activeTournament.champion);
    nodes.push({
      id: String(live), parentId: champ != null ? String(champ) : null,
      scalar: null, verdict: 'open', live: true, label: String(live),
    });
  }
  return nodes;
}

export function liveChallengerId() {
  const at = state.activeTournament;
  if (at && (at.challenger_id || at.challenger)) return at.challenger_id || at.challenger;
  const hb = state.heartbeat;
  if (hb && hb.generation_id && runLive()) return hb.generation_id;
  return null;
}
function runLive() {
  if (state.activeTournament) return true;
  const hb = state.heartbeat;
  return !!(hb && typeof hb.status === 'string' && /run/i.test(hb.status));
}

// --- experiment record ----------------------------------------------------
export function experimentFor(id) {
  const def = state.epochDef || {};
  const exps = Array.isArray(def.experiments) ? def.experiments
    : (Array.isArray(state.experiments) ? state.experiments : []);
  const want = String(id);
  for (const e of exps) {
    if (e && String(e.generation_id != null ? e.generation_id : e.id) === want) return e;
  }
  return null;
}

export function decisionOf(exp) {
  const o = exp && exp.outcome;
  if (!o || typeof o !== 'object') return null;
  const raw = String(o.tournament_decision || o.decision || o.verdict || '').toLowerCase();
  if (raw.includes('promot') || raw === 'accepted') return 'promoted';
  if (raw.includes('reject')) return 'rejected';
  if (raw.includes('defer')) return 'deferred';
  return raw || null;
}

export function isBaselineSeed(exp) {
  if (!exp || typeof exp !== 'object') return false;
  const parent = exp.parent_generation_id;
  if (typeof parent === 'string' && parent !== '') return false;
  return exp.outcome == null;
}

export function num(v) { return (typeof v === 'number' && isFinite(v)) ? v : null; }
export function outcomeNum(exp, key) {
  const o = exp && exp.outcome;
  return (o && typeof o === 'object') ? num(o[key]) : null;
}

export function hypothesisText(exp) {
  if (!exp) return '';
  const h = exp.hypothesis;
  if (h && typeof h === 'object') {
    const idea = (typeof h.core_idea === 'string' && h.core_idea.trim()) ? h.core_idea.trim()
      : (typeof h.summary === 'string' ? h.summary.trim() : '');
    if (idea) return idea;
  }
  if (typeof h === 'string' && h.trim()) return h.trim();
  return '';
}
export function hypothesisPrediction(exp) {
  const h = exp && exp.hypothesis;
  if (h && typeof h === 'object') {
    for (const k of ['prediction', 'predicted', 'expected_effect', 'predicts']) {
      if (typeof h[k] === 'string' && h[k].trim()) return h[k].trim();
    }
  }
  return '';
}
export function hypothesisRationale(exp) {
  const h = exp && exp.hypothesis;
  if (h && typeof h === 'object') {
    for (const k of ['rationale', 'reasoning', 'why', 'motivation']) {
      if (typeof h[k] === 'string' && h[k].trim()) return h[k].trim();
    }
  }
  return '';
}

// The current epoch id from any available source.
export function currentEpochId() {
  return (state.epochDef && state.epochDef.epoch_id)
    || (state.heartbeat && state.heartbeat.epoch_id)
    || (state.epoch && state.epoch.id !== '—' ? state.epoch.id : null)
    || null;
}

// --- tiny async cache -----------------------------------------------------
// makeCache(repaint) returns { ensure(key, path, fallback) } that fetches
// once per key, repaints on settle, and exposes get/has on its map.
export function makeCache(repaint) {
  const cache = new Map();
  const loading = new Set();
  return {
    cache, loading,
    has(key) { return cache.has(key); },
    get(key) { return cache.get(key); },
    set(key, val) { cache.set(key, val); },
    clear() { cache.clear(); loading.clear(); },
    async ensure(key, path, fallback) {
      if (key == null) return null;
      if (cache.has(key)) return cache.get(key);
      if (loading.has(key)) return null;
      loading.add(key);
      try {
        const data = await fetchJson(path);
        cache.set(key, (data && typeof data === 'object') ? data : (fallback || {}));
      } catch {
        cache.set(key, typeof fallback === 'function' ? fallback() : (fallback || { __broken: true }));
      } finally {
        loading.delete(key);
        if (typeof repaint === 'function') repaint();
      }
      return cache.get(key);
    },
  };
}
