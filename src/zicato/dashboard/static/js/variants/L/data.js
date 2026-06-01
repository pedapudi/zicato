// variants/L/data.js — Variant L's ("Atlas III") read layer.
//
// Self-contained for Variant L. Reuses the SHARED core data layer wholesale
// (core/api / core/state / core/sse) and adds only the small set of cached,
// failure-tolerant drill-down GETs the convergence-II screens need:
// per-entry grids, per-judge, matchup grids, the gate decomposition, the
// mutation surface + per-site baseline content, per-generation patches +
// full-file diffs, the ACM analysis markdown, and the transcript.
//
// Nothing here mutates AppState; callers own their cache. Each GET is cached
// by URL; `invalidateLive()` busts the keys that can change while a run is
// live (a user navigation or SSE refresh path calls it — never a heartbeat).

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
  for (const key of [..._cache.keys()]) {
    if (key.startsWith(prefix)) _cache.delete(key);
  }
}

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
      || key.startsWith('/api/round/')
      || key.startsWith('/api/files/')
      || key.startsWith('/api/mutations/')
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
export function epoch() { return cachedJson('/api/epoch'); }

export function analysis(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/analysis`);
}
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

// The mutation surface for the epoch: { generations:[…],
// mutations:[{mutation_id, kind, file, role, line_start, line_end,
// patched_by, patched_generation_ids, baseline:{content}}] }.
export function mutations(epochId) {
  return cachedJson(`/api/mutations/${enc(epochId)}`);
}
// One mutation site's detail — carries the champion BASELINE string at
// `.baseline.content` (NOT the `.baseline` object — that was the
// "[object Object]" bug). Falls back to scanning the surface payload.
export function mutationDetail(epochId, mutationId) {
  return cachedJson(`/api/mutations/${enc(epochId)}/${enc(mutationId)}`);
}
// What one generation actually changed: { patches:[{id, mutation_id, op,
// new_content, rationale}] } — the challenger NEW string lives here.
export function patches(epochId, genId) {
  return cachedJson(`/api/files/${enc(epochId)}/${enc(genId)}/patches`);
}
// Full-file fallback: { files:[{path, old_content, new_content}] }.
export function diff(epochId, genId) {
  return cachedJson(`/api/files/${enc(epochId)}/${enc(genId)}/diff`);
}
export function contractDiff(epochId) {
  return cachedJson(`/api/contract-diff/${enc(epochId)}`);
}

// The promote-gate decomposition for one round.
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
