// variants/J/data.js — Variant J's ("Console") read layer.
//
// Self-contained for Variant J. Reuses the SHARED core data layer wholesale
// (core/api.js, core/state.js, core/sse.js) and adds the small set of
// cached, failure-tolerant drill-down GETs the dense Console screens need —
// including the two NEW convergence reads the brief mandates:
//
//   * /api/mutations/{epoch_id}            — the mutation surface (sites +
//     which generations patched each).
//   * /api/files/{epoch}/{generation}/patches — what each generation
//     actually changed (per-mutation patch ops).
//   * /api/epoch/{epoch_id}/analysis       — the ACM publication `analysis_md`.
//   * /api/contract-diff/{epoch_id}        — the contract delta.
//
// Each is a thin, cached, failure-tolerant GET — the same discipline as
// core/api.js. Nothing here mutates AppState; callers own their cache.

import { fetchJson } from '../../core/api.js';

// A tiny module-level cache keyed by URL. Drill-down payloads are immutable
// for a completed generation, so caching avoids re-fetching on every
// SSE-driven re-render. The SSE refresh path busts keys via invalidate().
const _cache = new Map();

export async function cachedJson(path) {
  if (_cache.has(path)) return _cache.get(path);
  try {
    const data = await fetchJson(path);
    _cache.set(path, data);
    return data;
  } catch (err) {
    // A transient failure is cached as null so the view paints an honest
    // "unavailable" rather than spinning forever; a later invalidate() retries.
    _cache.set(path, null);
    return null;
  }
}

export function invalidate(prefix) {
  if (!prefix) { _cache.clear(); return; }
  for (const key of [..._cache.keys()]) {
    if (key.startsWith(prefix)) _cache.delete(key);
  }
}

// Bust the keys that can change while a run is live. The consolidated
// environment read (handled by core/api) is always fresh; these are the
// per-resource drill-downs we cache. The ACM analysis + mutation surface are
// epoch-stable but cheap to refetch, so they ride along.
export function invalidateLive() {
  for (const key of [..._cache.keys()]) {
    if (key.startsWith('/api/workspace')
      || key.startsWith('/api/health-report')
      || key.startsWith('/api/epoch')
      || key.startsWith('/api/score-trajectory')
      || key.startsWith('/api/generation/')
      || key.startsWith('/api/matchup-grid/')
      || key.startsWith('/api/drift-movements/')
      || key.startsWith('/api/run/')
      || key.startsWith('/api/lineage')
      || key.startsWith('/api/mutations/')
      || key.startsWith('/api/files/')
      || key.startsWith('/api/contract-diff/')
      || key.startsWith('/api/conversation/')
      || key.startsWith('/api/tournaments')) {
      _cache.delete(key);
    }
  }
}

// ---- typed drill-down reads ----------------------------------------

export function workspace() { return cachedJson('/api/workspace'); }
export function healthReport() { return cachedJson('/api/health-report'); }
export function scoreTrajectory() { return cachedJson('/api/score-trajectory'); }
export function lineage() { return cachedJson('/api/lineage'); }
export function bracket() { return cachedJson('/api/tournaments'); }

// The FULL epoch contract — goal, brief, board, scoring, experiments.
export function epoch() { return cachedJson('/api/epoch'); }

export function perJudgeTrend(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/per-judge-trend`);
}
export function perEntry(epochId, genId) {
  return cachedJson(`/api/generation/${enc(epochId)}/${enc(genId)}/per-entry`);
}
export function perJudgeForGen(epochId, genId) {
  return cachedJson(`/api/generation/${enc(epochId)}/${enc(genId)}/per-judge`);
}
export function matchupGrid(epochId, championId, challengerId) {
  return cachedJson(`/api/matchup-grid/${enc(epochId)}/${enc(championId)}/${enc(challengerId)}`);
}
export function diff(epochId, genId) {
  return cachedJson(`/api/files/${enc(epochId)}/${enc(genId)}/diff`);
}

// ---- NEW (convergence) reads ---------------------------------------

// The mutation surface for an epoch: the enumerated mutation sites and which
// generations patched each — the substrate for the site × generation matrix.
export function mutations(epochId) {
  return cachedJson(`/api/mutations/${enc(epochId)}`);
}
// What ONE generation actually changed — the per-mutation patch ops (op,
// new_content, mutation_id) the matrix drills into.
export function patches(epochId, genId) {
  return cachedJson(`/api/files/${enc(epochId)}/${enc(genId)}/patches`);
}
// The epoch's ACM publication, as markdown with section markers.
export function analysis(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/analysis`);
}
// The epoch contract delta (added / removed board entries, scoring moves).
export function contractDiff(epochId) {
  return cachedJson(`/api/contract-diff/${enc(epochId)}`);
}

// The promote-gate decomposition for one round.
export function gate(epochId, championId, challengerId) {
  return cachedJson(`/api/round/${enc(epochId)}/${enc(championId)}/${enc(challengerId)}/gate`);
}

// One entry's expectation outcomes + per-judge losses (keyed by board entry).
export function expectations(epochId, genId, entryId) {
  return cachedJson(`/api/run/${enc(epochId)}/${enc(genId)}/${enc(entryId)}/expectations`);
}
export function perJudgeForRun(epochId, genId, entryId) {
  return cachedJson(`/api/run/${enc(epochId)}/${enc(genId)}/${enc(entryId)}/per-judge`);
}

// The reconstructed transcript for a run.
export function conversation(runId) {
  return cachedJson(`/api/conversation/${enc(runId)}`);
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
