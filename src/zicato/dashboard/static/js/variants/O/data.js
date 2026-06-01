// variants/O/data.js — Variant O's ("Compass") read layer.
//
// Self-contained for Variant O. Reuses the SHARED core data layer
// (core/api → core/state via the consolidated /api/environment read +
// the SSE stream) and adds the small set of cached, failure-tolerant
// per-resource drill-downs the master-detail panes need. Nothing here
// mutates AppState; the caller owns the cache (busted only on a user
// navigation / selection change — never on a heartbeat).

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

// Bust the per-resource drill-downs that can change while a run is live.
export function invalidateLive() {
  for (const key of [..._cache.keys()]) {
    if (key.startsWith('/api/workspace')
      || key.startsWith('/api/health-report')
      || key.startsWith('/api/epoch')
      || key.startsWith('/api/score-trajectory')
      || key.startsWith('/api/generation/')
      || key.startsWith('/api/matchup-grid/')
      || key.startsWith('/api/mutations/')
      || key.startsWith('/api/files/')
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
export function scoreTrajectory() { return cachedJson('/api/score-trajectory'); }
export function lineage() { return cachedJson('/api/lineage'); }
export function tournaments() { return cachedJson('/api/tournaments'); }

// The FULL epoch contract — board, scoring, goal, experiments, proposer
// brief — heavier than the /api/environment summary.
export function epoch() { return cachedJson('/api/epoch'); }

export function analysis(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/analysis`);
}

// The mutation surface for the epoch: { generations:[…], mutations:[{
//   mutation_id, kind, file, role, line_start, line_end, patched_by,
//   patched_generation_ids }] }.
export function mutations(epochId) {
  return cachedJson(`/api/mutations/${enc(epochId)}`);
}

// The BASELINE for one mutation site — the champion's content for that
// region as a STRING at `.baseline.content`. (Rendering the `baseline`
// OBJECT itself was the "[object Object]" bug; callers MUST read
// `.baseline.content`.)
export function mutationDetail(epochId, mutationId) {
  return cachedJson(`/api/mutations/${enc(epochId)}/${enc(mutationId)}`);
}

// What one generation actually changed: { patches:[{ id, mutation_id,
//   op, new_content, rationale }] } — the challenger STRING is
// `.new_content` of the matching `mutation_id`.
export function patches(epochId, genId) {
  return cachedJson(`/api/files/${enc(epochId)}/${enc(genId)}/patches`);
}
// Full-file fallback diff: { files:[{ path, old_content, new_content }] }.
export function diff(epochId, genId) {
  return cachedJson(`/api/files/${enc(epochId)}/${enc(genId)}/diff`);
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
