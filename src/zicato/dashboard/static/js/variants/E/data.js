// variants/E/data.js — Variant E's (Atlas) read layer.
//
// Self-contained for Variant E. Reuses the SHARED core data layer wholesale:
//   * core/api.js     — loadEnvironment / loadServiceIdentity (the
//                       single coalesced /api/environment read).
//   * core/state.js   — the AppState singleton (epoch, epochDef,
//                       workspace, lineage, …) the whole UI reads.
//   * core/sse.js     — the /events stream + debounced refresh.
//
// On top of that shared substrate this module adds only the small set
// of per-resource drill-down fetches the Tufte screens need that the
// consolidated environment read does not carry (per-entry grids,
// per-judge trends, matchup grids, drift movements, the full epoch
// contract with its proposer brief, the file/diff payloads). Each is a
// thin, cached, failure-tolerant GET — the same discipline as
// core/api.js. Nothing here mutates AppState; callers own their cache.

import { fetchJson } from '../../core/api.js';

// A tiny module-level cache keyed by URL. Drill-down payloads are
// immutable for a completed generation, so caching avoids re-fetching
// on every SSE-driven re-render. The SSE refresh path can bust a key
// via `invalidate(prefix)` when live data may have changed.
const _cache = new Map();

export async function cachedJson(path) {
  if (_cache.has(path)) return _cache.get(path);
  try {
    const data = await fetchJson(path);
    _cache.set(path, data);
    return data;
  } catch (err) {
    // A transient failure is cached as null so the view paints an
    // honest "unavailable" rather than spinning forever; a later
    // invalidate() lets it retry.
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
// per-resource drill-downs we cache.
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

// The FULL epoch contract — this is where the proposer brief lives
// (`brief` key), along with the board, scoring, goal, experiments, and
// the inline analysis fragment. The consolidated /api/environment only
// carries a lighter epoch summary, so the Epoch view reads this.
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
export function driftMovements(genId) {
  return cachedJson(`/api/drift-movements/${enc(genId)}`);
}
export function diff(epochId, genId) {
  return cachedJson(`/api/files/${enc(epochId)}/${enc(genId)}/diff`);
}
export function patches(epochId, genId) {
  return cachedJson(`/api/files/${enc(epochId)}/${enc(genId)}/patches`);
}

// The promote-gate decomposition for one round — three short-circuiting
// rules + the scalar-component split for both sides + the primary driver.
export function gate(epochId, championId, challengerId) {
  return cachedJson(`/api/round/${enc(epochId)}/${enc(championId)}/${enc(challengerId)}/gate`);
}

// Theme-3 drill-down depth 2: one entry's expectation outcomes and the
// per-judge losses for that single run (keyed by board-entry id).
export function expectations(epochId, genId, entryId) {
  return cachedJson(`/api/run/${enc(epochId)}/${enc(genId)}/${enc(entryId)}/expectations`);
}
export function perJudgeForRun(epochId, genId, entryId) {
  return cachedJson(`/api/run/${enc(epochId)}/${enc(genId)}/${enc(entryId)}/per-judge`);
}

// Theme-3 drill-down depth 3: the reconstructed transcript for a run.
export function conversation(runId) {
  return cachedJson(`/api/conversation/${enc(runId)}`);
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
