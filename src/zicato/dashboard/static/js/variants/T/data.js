// variants/T/data.js — Variant N's ("Console II") read layer.
//
// Self-contained for Variant N. Reuses the SHARED core data layer wholesale
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
      || key.startsWith('/api/tournament-structure/')
      || key.startsWith('/api/tournaments')) {
      _cache.delete(key);
    }
  }
}

// ---- typed drill-down reads ----------------------------------------

export function workspace() { return cachedJson('/api/workspace'); }
export function healthReport() { return cachedJson('/api/health-report'); }
export function lineage() { return cachedJson('/api/lineage'); }

// ---- epoch-scoped reads --------------------------------------------
//
// /api/lineage is workspace-GLOBAL (every generation across every epoch),
// but each row carries `epoch_id`. The Console screens must show only the
// gens of the epoch they are VIEWING — not always the current one — so the
// cross-epoch leakage (Class A) is fixed at the read layer: filter the
// global lineage to one epoch, deduped by generation id. Fall back to the
// unfiltered list ONLY when NO row carries an epoch_id (a pre-feature
// payload), so a single-epoch workspace keeps working unchanged.
export async function generationsForEpoch(epochId) {
  const lin = await lineage();
  const gens = (lin && Array.isArray(lin.generations)) ? lin.generations : [];
  if (!gens.length) return [];
  const anyTagged = gens.some((g) => g && g.epoch_id != null);
  const rows = (anyTagged && epochId != null)
    ? gens.filter((g) => g && g.epoch_id === epochId)
    : gens;
  const seen = new Set();
  const out = [];
  for (const g of rows) {
    const id = g && g.generation_id;
    if (id == null || seen.has(id)) continue;
    seen.add(id);
    out.push(g);
  }
  return out;
}

// The epoch CONTRACT, score TRAJECTORY, and BRACKET — backend-scoped to the
// CURRENT epoch when called with no argument (unchanged), or to a NAMED epoch
// when given an id (the `?epoch=<id>` backend scoping is the only true fix for
// viewing a non-current epoch). Each remains a thin, cached, failure-tolerant
// GET; the `?epoch` variant caches under its own key so the current-epoch and
// scoped reads never collide.
export function epoch(epochId) {
  return cachedJson(epochId != null ? `/api/epoch?epoch=${enc(epochId)}` : '/api/epoch');
}
export function scoreTrajectory(epochId) {
  return cachedJson(epochId != null ? `/api/score-trajectory?epoch=${enc(epochId)}` : '/api/score-trajectory');
}
export function bracket(epochId) {
  return cachedJson(epochId != null ? `/api/tournaments?epoch=${enc(epochId)}` : '/api/tournaments');
}

// The LIVE tournament state during a run (§ live racing/swiss/elim): the
// SAME structure shape as /api/tournament-structure ({structure, phase,
// competitors, rounds, standings}) but for the IN-FLIGHT tournament, so the
// match-ups ladder fills in rung-by-rung and the in-flight competitors are
// not mislabeled as rejected. NEVER cached — it changes on every heartbeat,
// and a failure (404 when idle) degrades to null so callers fall back to the
// completed /api/tournaments record. The shell already reads state.heartbeat
// / state.activeRuns to decide whether a run is active; this fetch carries
// the live topology those signals do not.
export async function activeTournament() {
  try { return await fetchJson('/api/active-tournament'); } catch (err) { return null; }
}

// The actual configured tournament STRUCTURE for one tournament — the
// full bracket / standings / racing state (§3.2). Resolves from the
// index → live active record → per-run loss files, so a completed
// tournament renders even without the SQLite index. Absent / malformed
// degrades to an empty gauntlet envelope (HTTP 200), so callers can read
// it defensively.
export function tournamentStructure(epochId, tournamentId) {
  return cachedJson(`/api/tournament-structure/${enc(epochId)}/${enc(tournamentId)}`);
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
// ONE mutation site's baseline (v0) string content + per-generation versions —
// the LEFT column of the side-by-side diff (.baseline.content, NOT the object).
export function mutationDetail(epochId, mutationId) {
  return cachedJson(`/api/mutations/${enc(epochId)}/${enc(mutationId)}`);
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
