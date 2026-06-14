// core/api.js — the HTTP data layer.
//
// The dashboard reads the whole environment through ONE consolidated
// endpoint (/api/environment) and refreshes on a single coalesced poll.
// It does NOT fan out to many per-section endpoints and does NOT poll
// on a tight timer. Drill-downs use the lazy per-resource endpoints.
//
// Every function here mutates AppState (which emits `state:changed`)
// and returns; views never call api.* directly except for explicit
// drill-down fetches.

import { state } from './state.js';

export async function fetchJson(path) {
  const res = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

// The single coalesced environment read. Tolerates a transient failure
// — the last-known environment stays painted.
export async function loadEnvironment() {
  let env;
  try {
    env = await fetchJson('/api/environment');
  } catch (err) {
    console.warn('loadEnvironment failed:', err);
    return;
  }
  state.applyEnvironment(env);
}

// /api/health — dashboard-service identity. Fixed for the process
// lifetime, so fetched ONCE at bootstrap. Tolerates an absent endpoint.
// Emits `state:changed` so the footer repaints with the real identity.
export async function loadServiceIdentity() {
  try {
    state.setHealth(await fetchJson('/api/health'));
    state._changed();
  } catch (err) {
    // Footer degrades to its placeholder.
  }
}

// Append-only run-log poll: ask only for events past the cursor we
// already have and merge them. The merge emits `log:appended`, which
// the chrome's activity-log drawer answers with an append-only render.
export async function pollLogTailAppend() {
  if (state.mock) return;
  const after = state.logCursor;
  if (after == null) return;  // a full environment read seeds the cursor
  try {
    const batch = await fetchJson('/api/run-log?after=' + encodeURIComponent(after));
    state.mergeLogTail(batch);
  } catch (err) {
    // Transient — the next run_log frame retries.
  }
}

// /api/tournaments/:gen — per-matchup detail, plus the drift-movements
// for the same challenger. Both cached; a failure degrades the panel
// to the bracket summary rather than breaking it.
export async function loadMatchupDetail(genId) {
  if (!genId || state.mock) return;
  try {
    const detail = await fetchJson('/api/tournaments/' + encodeURIComponent(genId));
    state.setMatchupDetail(genId, detail);
  } catch (err) {
    // renderMatchupDetail falls back to the bracket summary.
  }
  try {
    const dm = await fetchJson('/api/drift-movements/' + encodeURIComponent(genId));
    state.driftMovements[genId] = dm;
    state._changed();
  } catch (err) {
    // Section degrades to "no movements".
  }
}

// POST a control marker (pause / skip-round / kill / promote / reject /
// brief). Read-only workspaces answer 403 — surfaced to the caller.
export async function postControl(action, body) {
  const res = await fetch('/api/control/' + action, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  let payload = null;
  try { payload = await res.json(); } catch { /* empty body */ }
  return { ok: res.ok, status: res.status, payload };
}

// POST a per-challenger FIELD override — the operator force-promote/reject that
// rides BESIDE the gate verdict. The route carries the generation_id as a PATH
// param (POST /api/control/{promote|reject}/{generation_id}) and an extended
// JSON body {reason, epoch, tournament_id, structure} the readback uses to name
// which field round / structure the override targeted (the gauntlet path reads
// only `reason`; the extra keys are inert there). Read-only workspaces answer
// 403 — surfaced to the caller so the cell can flag the rejection rather than
// silently failing. `action` ∈ "promote" | "reject"; `gid` is the challenger.
export async function postFieldOverride(action, gid, body) {
  const path = '/api/control/' + encodeURIComponent(action) + '/' + encodeURIComponent(gid);
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  let payload = null;
  try { payload = await res.json(); } catch { /* empty body */ }
  return { ok: res.ok, status: res.status, payload };
}
