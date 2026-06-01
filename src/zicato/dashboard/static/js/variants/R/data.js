// variants/R/data.js — Variant R's ("Strata") read layer.
//
// Self-contained for Variant R. Reuses the SHARED core data layer wholesale
// (core/api.js) and adds the small set of cached, failure-tolerant drill-down
// GETs the Miller-columns screens need — the exact convergence-III contract:
//   * /api/workspace · /api/lineage · /api/score-trajectory · /api/epoch
//   * /api/tournaments                         — ALL match-ups for a candidate
//   * /api/mutations/{epoch}                   — the mutation surface
//   * /api/mutations/{epoch}/{id}              — one site's baseline.content STRING
//   * /api/files/{epoch}/{gen}/patches         — what a generation changed
//   * /api/files/{epoch}/{gen}/diff            — full-file fallback
//   * /api/generation/{e}/{g}/per-entry        — per-board scores
//   * /api/round/{e}/{champ}/{chall}/gate      — the promote-gate decomposition
//   * /api/conversation/{run_id}               — the inline transcript
//   * /api/epoch/{epoch}/analysis              — the ACM publication
//
// Each is a thin, cached, failure-tolerant GET. Nothing here mutates AppState;
// callers own their cache. The cache invalidates on selection change.

import { fetchJson } from '../../core/api.js';

const _cache = new Map();

export async function cachedJson(path) {
  if (_cache.has(path)) return _cache.get(path);
  try {
    const data = await fetchJson(path);
    _cache.set(path, data);
    return data;
  } catch (err) {
    _cache.set(path, null);
    return null;
  }
}

export function invalidate(prefix) {
  if (!prefix) { _cache.clear(); return; }
  for (const key of [..._cache.keys()]) if (key.startsWith(prefix)) _cache.delete(key);
}

// Bust the keys that can change while a run is live (the env read is always
// fresh via core/api). Called by the shell on a live SSE tick.
export function invalidateLive() {
  for (const key of [..._cache.keys()]) {
    if (key.startsWith('/api/workspace')
      || key.startsWith('/api/health-report')
      || key.startsWith('/api/epoch')
      || key.startsWith('/api/score-trajectory')
      || key.startsWith('/api/generation/')
      || key.startsWith('/api/matchup-grid/')
      || key.startsWith('/api/round/')
      || key.startsWith('/api/lineage')
      || key.startsWith('/api/mutations/')
      || key.startsWith('/api/files/')
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
export function epoch() { return cachedJson('/api/epoch'); }

export function perEntry(epochId, genId) {
  return cachedJson(`/api/generation/${enc(epochId)}/${enc(genId)}/per-entry`);
}
export function matchupGrid(epochId, championId, challengerId) {
  return cachedJson(`/api/matchup-grid/${enc(epochId)}/${enc(championId)}/${enc(challengerId)}`);
}
export function diff(epochId, genId) {
  return cachedJson(`/api/files/${enc(epochId)}/${enc(genId)}/diff`);
}
export function mutations(epochId) {
  return cachedJson(`/api/mutations/${enc(epochId)}`);
}
export function patches(epochId, genId) {
  return cachedJson(`/api/files/${enc(epochId)}/${enc(genId)}/patches`);
}
export function mutationDetail(epochId, mutationId) {
  return cachedJson(`/api/mutations/${enc(epochId)}/${enc(mutationId)}`);
}
export function analysis(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/analysis`);
}
export function gate(epochId, championId, challengerId) {
  return cachedJson(`/api/round/${enc(epochId)}/${enc(championId)}/${enc(challengerId)}/gate`);
}
export function expectations(epochId, genId, entryId) {
  return cachedJson(`/api/run/${enc(epochId)}/${enc(genId)}/${enc(entryId)}/expectations`);
}
export function perJudgeForRun(epochId, genId, entryId) {
  return cachedJson(`/api/run/${enc(epochId)}/${enc(genId)}/${enc(entryId)}/per-judge`);
}
export function conversation(runId) {
  return cachedJson(`/api/conversation/${enc(runId)}`);
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
