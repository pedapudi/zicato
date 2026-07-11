// js/data.js — Variant N's ("Console II") read layer.
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

import { fetchJson } from './core/api.js';
import { state } from './core/state.js';

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

// Bust the cached transcript reads for ONE (epoch, gen, entry) run so the next
// fetch re-reads the still-growing events.jsonl. invalidateLive() only fires on
// a VIEW change, never on a no-op beat (that is the scroll-reset fix); but a
// candidate RUNNING on the board the operator is already watching must have its
// transcript re-read as new turns land, without busting every cached read. This
// drops just the two cache keys a live transcript flows through — the gen×entry
// /api/run/.../transcript (the primary path) and, when the row carried a run_id,
// the /api/conversation/<run_id> fallback. The transcript host stays digest-
// gated on CONTENT (transcriptDigest), so a re-read that yields no new turn is
// still a no-op repaint — scroll is preserved.
export function invalidateRunTranscript(epochId, genId, entryId, runId) {
  const run = `/api/run/${enc(epochId)}/${enc(genId)}/${enc(entryId)}/transcript`;
  for (const key of [..._cache.keys()]) {
    if (key === run || key.startsWith(run + '?')) _cache.delete(key);
  }
  if (runId) {
    const conv = `/api/conversation/${enc(runId)}`;
    for (const key of [..._cache.keys()]) {
      if (key === conv || key.startsWith(conv + '?')) _cache.delete(key);
    }
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
      || key.startsWith('/api/hypothesis-accuracy/')
      || key.startsWith('/api/calibration-trend')
      // the reflection LIST (plural) — so a reflection completed while the
      // dashboard is open surfaces on the next live bust. This prefix does NOT
      // match the singular, IMMUTABLE `/api/reflection/<id>/…` reads (those
      // start `/api/reflection/`, not `/api/reflections`), which stay cached.
      || key.startsWith('/api/reflections')
      || key.startsWith('/api/tournaments')) {
      _cache.delete(key);
    }
  }
}

// THE UNDER-RENDER FIX. The tree + every candidate-listing view read through the
// module cache above; invalidateLive() — the ONLY thing that busts those keys —
// fires solely on a VIEW change (shell.dispatch). So a NEW candidate surfaced
// mid-round by SSE refreshed AppState's lineage but NOT these cached reads → the
// tree/view digests never flipped → no repaint, forcing a hard-refresh (which
// clears this cache). A view-change-only invalidation cannot catch an in-place
// add. The shell now busts the cache when a candidate lands, keyed off this
// signature so a no-op beat (identical payload) busts nothing — no flash, no
// extra fetch. Signed off the data AppState folds from /api/environment (the gen
// SET: id + tri-state status + birth-round + epoch, plus the epoch roster),
// id-sorted so it is order-independent — only a real membership/status change
// flips it; the downstream gen-keyed digests still gate after a bust.
export function liveDataSignature() {
  const lin = (state.lineage && Array.isArray(state.lineage.generations))
    ? state.lineage.generations : [];
  const gens = lin
    .map((g) => [
      g && g.generation_id != null ? String(g.generation_id) : '',
      g && g.promoted == null ? 'p' : (g.promoted ? '1' : '0'),
      g && Number.isInteger(g.round_index) ? g.round_index : -1,
      g && g.epoch_id != null ? String(g.epoch_id) : '',
    ])
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const epochs = (Array.isArray(state.epochs) ? state.epochs : [])
    .map((e) => (e && e.epoch_id != null ? String(e.epoch_id) : ''))
    .sort();
  const wsEpoch = (state.workspace && state.workspace.current_epoch_id != null)
    ? String(state.workspace.current_epoch_id) : '';
  return JSON.stringify({ gens, epochs, wsEpoch });
}

// ---- typed drill-down reads ----------------------------------------

export function workspace() { return cachedJson('/api/workspace'); }
export function healthReport() { return cachedJson('/api/health-report'); }
export function lineage() { return cachedJson('/api/lineage'); }

// ---- epoch-scoped reads --------------------------------------------
//
// The SERVER scopes the generations feed: `/api/lineage?epoch=<id>` returns
// one epoch's generations (every row already carries the server-stamped
// tri-state `promoted` + `epoch_id`). The residual client-side epoch filter
// below is a SCOPING GUARD only — a degraded server (the Rust supervisor
// ignores the query param) still answers with the global feed, and a
// foreign-tagged row must not leak into the viewed epoch. It never
// re-derives any per-row field.
export async function generationsForEpoch(epochId) {
  const lin = await cachedJson(epochId != null ? `/api/lineage?epoch=${enc(epochId)}` : '/api/lineage');
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

// The AUTHORITATIVE live round-pipeline projection (propose → apply → run →
// gate), computed SERVER-side (build_round_pipeline) so the stepper never
// re-derives loop position by parsing phase strings client-side. NEVER cached
// — it moves on every heartbeat — and a failure (the Rust supervisor does not
// serve it) degrades to null so the hero simply omits the stepper.
export async function livePipeline() {
  try { return await fetchJson('/api/live/pipeline'); } catch (err) { return null; }
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

// The per-challenger proposing-step outcomes from either the live
// active-tournament payload or a completed tournament-structure payload.
// Each entry is {generation_id, status: "proposing"|"applied"|"rejected",
// reason, attempts, attempt_reasons, hypothesis, seed?}. The `status`
// "proposing" is the LIVE in-flight slot (the proposer is mid-attempt);
// `attempt_reasons` is the FULL per-attempt failure list (so the SPECIFIC
// validation/parse/post-apply message is visible, not just condensed);
// `attempts` is the retry count; `hypothesis` is the applied challenger's
// one-line core idea. Absent / malformed (old data, gauntlet) reads as []
// so callers render an honest empty tracker rather than throwing.
export function fieldStatus(payload) {
  if (!payload || typeof payload !== 'object') return [];
  const fs = payload.field_status;
  if (!Array.isArray(fs)) return [];
  const out = [];
  for (const f of fs) {
    if (!f || typeof f !== 'object') continue;
    const gid = f.generation_id;
    if (gid == null || gid === '') continue;
    // Normalise status: known tokens pass through, anything else (old
    // applied/rejected-only data) maps applied → applied, else rejected.
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

// Roll a field_status list into the tracker's headline counts:
// {proposed, applied, rejected, proposing, allRejected}. `proposing` is the
// count of in-flight slots (the proposer is mid-attempt). allRejected is
// true only when a non-empty, FULLY-SETTLED field (no slot still proposing)
// minted zero applied challengers — the "0 applied — all rejected" state the
// live hero must not mistake for idle, and must not declare prematurely
// while a slot is still being attempted.
export function fieldStatusSummary(fs) {
  const list = Array.isArray(fs) ? fs : [];
  const applied = list.filter((f) => f && f.status === 'applied').length;
  const proposing = list.filter((f) => f && f.status === 'proposing').length;
  const rejected = list.length - applied - proposing;
  return {
    proposed: list.length,
    applied,
    rejected,
    proposing,
    allRejected: list.length > 0 && applied === 0 && proposing === 0,
  };
}

// The SETTLED round timeline for one epoch — the champion-spine rounds + the
// loss-floor waterfall, JOINED SERVER-SIDE (build_round_timeline). The old
// four-endpoint client join is deleted; a null read (the endpoint absent —
// e.g. the Rust supervisor) renders the honest empty timeline, never a
// client-side re-derivation.
export function roundTimeline(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/round-timeline`);
}
// The SETTLED racing-field ladder for one epoch — the per-challenger racing
// records joined into ONE rung/gate payload SERVER-SIDE (build_racing_field).
// Returns null when absent or `present: false` (no racing records) so callers
// fall through to their empty state without reconstructing anything.
export async function racingField(epochId) {
  const payload = await cachedJson(`/api/epoch/${enc(epochId)}/racing-field`);
  return (payload && payload.present) ? payload : null;
}
export function perJudgeTrend(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/per-judge-trend`);
}
// The promoted-lineage OPTIMIZATION TRAJECTORY for one epoch — scalar points
// along the winners spine + promotion_rate + the UNCERTAINTY-HONEST verdict
// ("improving" / "plateaued" / "no_signal" when the recent movement sits below
// the measured A/A noise floor) + the floor itself (build_optimization_
// trajectory). Absent on the Rust supervisor → cachedJson null-degrades and
// the panels are simply omitted.
export function trajectory(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/trajectory`);
}
// The wall-clock + run-count COST accounting for one epoch's tournament —
// per-matchup runtime/runs/aborts + cost_per_promotion_ms (build_tournament_
// cost). Null-degrades on the Rust supervisor like every accessor here.
export function tournamentCost(epochId) {
  return cachedJson(`/api/epoch/${enc(epochId)}/cost`);
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

// ---- Instrument lens (board reflection) reads -----------------------
//
// Thin, cached, failure-tolerant GETs over query/reflection_view.py (the four
// /api/reflection* endpoints). A completed reflection is IMMUTABLE, so caching
// is safe and the views render fetch-once. Every reader degrades to a same-
// shape empty payload server-side (DQ3), so a null here means a transport
// failure only.

// Every reflection under the workspace, or one epoch when scoped (newest first).
export function reflections(epochId) {
  return cachedJson(epochId != null ? `/api/reflections?epoch=${enc(epochId)}` : '/api/reflections');
}
// The four-pillar bill of health for one reflection (+ its ranked findings).
export function reflectionSummary(reflectionId) {
  return cachedJson(`/api/reflection/${enc(reflectionId)}/summary`);
}
// The per-judge confusion-matrix scorecards for one reflection.
export function reflectionScorecards(reflectionId) {
  return cachedJson(`/api/reflection/${enc(reflectionId)}/scorecards`);
}
// The transcript x-ray for ONE adjudicated decision (judge + run_ref). The
// run_ref carries `:` — enc() keeps the path segment intact.
export function reflectionXray(reflectionId, judge, runRef) {
  return cachedJson(`/api/reflection/${enc(reflectionId)}/xray/${enc(judge)}/${enc(runRef)}`);
}

// The promote-gate decomposition for one round.
export function gate(epochId, championId, challengerId) {
  return cachedJson(`/api/round/${enc(epochId)}/${enc(championId)}/${enc(challengerId)}/gate`);
}

// The proposer's PREDICTION-ACCURACY scorecard for ONE generation — predicted
// vs realised movements + the calibration fraction (build_hypothesis_accuracy).
// DIAGNOSTIC: this never feeds the promote gate. Absent / malformed reads as a
// 200 empty scorecard, so the dossier paints an honest "no claims" rather than
// throwing.
export function hypothesisAccuracy(epochId, genId) {
  return cachedJson(`/api/hypothesis-accuracy/${enc(epochId)}/${enc(genId)}`);
}
// The per-generation calibration TREND across one epoch's lineage — the score
// fraction over generations + a trend sign (build_calibration_trend). Scoped to
// a NAMED epoch with `?epoch=<id>`, the current epoch when omitted. DIAGNOSTIC
// ONLY — never feeds the gate. Failure / unknown epoch reads as null.
export function calibrationTrend(epochId) {
  return cachedJson(epochId != null ? `/api/calibration-trend?epoch=${enc(epochId)}` : '/api/calibration-trend');
}

// One entry's expectation outcomes + per-judge losses (keyed by board entry).
export function expectations(epochId, genId, entryId) {
  return cachedJson(`/api/run/${enc(epochId)}/${enc(genId)}/${enc(entryId)}/expectations`);
}
export function perJudgeForRun(epochId, genId, entryId) {
  return cachedJson(`/api/run/${enc(epochId)}/${enc(genId)}/${enc(entryId)}/per-judge`);
}
// Per-run header (runtime/tokens/turns/...) AND the run's adk_session_id —
// the latter is what the harmonograf deep-link keys its session view on
// (the per-entry index rows do not carry it; this loss.json-backed read does).
export function runHeader(epochId, genId, entryId) {
  return cachedJson(`/api/run/${enc(epochId)}/${enc(genId)}/${enc(entryId)}/header`);
}

// Back-compat run_id-keyed transcript read. Resolution is now gen×entry-FIRST
// on the backend: when the caller knows the run's coordinates (the panes
// always do), pass gen+entry so the deterministic triple is the primary key
// and the opaque run_id is only a disambiguator. Kept for the few callers
// that hold only a run_id; prefer runTranscript() when the triple is known.
export function conversation(runId, gen, entry) {
  let url = `/api/conversation/${enc(runId)}`;
  if (gen && entry) url += `?gen=${enc(gen)}&entry=${enc(entry)}`;
  return cachedJson(url);
}

// PRIMARY transcript accessor: resolve directly by the deterministic
// (epoch, gen, entry) triple — the events file lives at
// generations/<gen>/runs/<entry>/events.jsonl, so this always lands on the
// one real transcript on disk, independent of how any run_id was minted.
// An optional runId is passed only as a DISAMBIGUATOR (?run=) for the
// successive-halving case where a gen×entry has been re-raced across rungs
// and carries multiple run records; the backend defaults to the entry's own
// events.jsonl when it is omitted or does not match a specific rung.
export function runTranscript(epochId, genId, entryId, runId) {
  let url = `/api/run/${enc(epochId)}/${enc(genId)}/${enc(entryId)}/transcript`;
  if (runId) url += `?run=${enc(runId)}`;
  return cachedJson(url);
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
