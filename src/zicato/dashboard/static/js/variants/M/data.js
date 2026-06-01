// variants/M/data.js — Variant M's (Ledger II) read layer.
//
// Self-contained for Variant M. Reuses the SHARED core data layer (api.js
// / state.js / sse.js) and adds the thin, cached, failure-tolerant
// drill-down GETs the editorial screens need — including the convergence-II
// surfaces: the combined mutation surface + SIDE-BY-SIDE patch diff (baseline
// content string + challenger new_content string), the per-board
// cross-candidate pivot, and the epoch's ACM-style analysis markdown.
//
// Nothing here mutates AppState; callers own their cache. A transient
// failure is cached as null so a view paints an honest "unavailable" rather
// than spinning; a later invalidate() lets it retry.

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

// Bust the keys that can change while a run is live.
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
      || key.startsWith('/api/lineage')
      || key.startsWith('/api/mutations')
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

// The FULL epoch contract — board, goal, brief, experiments.
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
export function patches(epochId, genId) {
  return cachedJson(`/api/files/${enc(epochId)}/${enc(genId)}/patches`);
}

// The mutation-site index — every `# zicato:mutable` region in the epoch's
// baseline surface + which generations patched each.
export function mutations(epochId) {
  return cachedJson(`/api/mutations/${enc(epochId)}`);
}
// One mutation site's detail. The convergence-II contract: the BASELINE
// content is a STRING at `.baseline.content` (NOT the `baseline` object —
// rendering the object was the "[object Object]" bug). The challenger's new
// content comes from the per-generation patches endpoint.
export function mutationDetail(epochId, mutationId) {
  return cachedJson(`/api/mutations/${enc(epochId)}/${enc(mutationId)}`);
}
// The contract diff (illustrative context alongside the matrix).
export function contractDiff(epochId) {
  return cachedJson(`/api/contract-diff/${enc(epochId)}`);
}

// The ACM-style epoch publication. We bind `analysis_md` (the raw markdown
// with EYEBROW/META/section markers); K's renderer parses it.
export function analysis(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/analysis`);
}

// The promote-gate decomposition for one round.
export function gate(epochId, championId, challengerId) {
  return cachedJson(`/api/round/${enc(epochId)}/${enc(championId)}/${enc(challengerId)}/gate`);
}

// One entry's expectation outcomes + per-judge losses.
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

// Resolve the STRING baseline content for a mutation site from its detail
// payload — robust to the several shapes the API has used. The contract:
// `.baseline.content` is the canonical string; we fall back to a top-level
// `baseline_content` string, but NEVER render the `baseline` object itself.
export function baselineContent(detail) {
  if (!detail || typeof detail !== 'object') return null;
  const b = detail.baseline;
  if (b && typeof b === 'object' && typeof b.content === 'string') return b.content;
  if (typeof detail.baseline_content === 'string') return detail.baseline_content;
  if (typeof b === 'string') return b; // legacy string form
  return null;
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
