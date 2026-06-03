// variants/T/livestatus.js — derive a STRUCTURE-AGNOSTIC live-run status.
//
// THE BUG this fixes: T's chrome status pill was gauntlet-shaped — it only lit
// up off `state.activeTournament` (which the gauntlet path populates). During a
// live NON-gauntlet run (racing / swiss / single_elim / double_elim) the pill
// read "nothing is running" even though the run was plainly in flight.
//
// The live read APIs already report activity for ANY structure:
//   * /api/heartbeat       → { phase, generation_id, round_index, … }; `phase`
//                            is a non-idle string while running
//                            (`proposing:…`, `tournament:round_0:rung0_m3`, …)
//                            and is idle/absent when the loop is done.
//   * /api/active-runs     → an array of in-flight board-units (each carries a
//                            generation_id / entry_id / run_id / progress).
//   * /api/active-tournament → { structure, phase: "running", … } while a
//                            tournament is live; null / empty when idle.
//
// `deriveLiveStatus(...)` folds those three into ONE plain, pure verdict the
// chrome (and a test) can read. It is intentionally dependency-free: it takes
// the raw payload values, never AppState, so it unit-tests without a DOM.

// Phases that mean "the loop is at rest" (NOT running). Anything else
// non-empty is treated as an active phase.
const IDLE_PHASES = new Set(['idle', 'done', 'complete', 'completed', 'finished', 'stopped', 'error']);

// Heartbeat-staleness window. The supervisor rewrites the heartbeat on a short
// cadence (a few seconds); a FROZEN heartbeat from a dead/finished process must
// stop reading "live". ~30s is a few × the heartbeat interval — long enough to
// tolerate a slow tick, short enough that a completed run reads idle promptly.
export const STALE_HEARTBEAT_MS = 30_000;

// Is a heartbeat `phase` string a NON-idle (i.e. running) phase?
//
// The phase may be a colon-delimited path (`tournament:round_0:rung0_m3`,
// `evolve_n_rounds:done`). The TERMINAL signal lives in the TAIL segment
// (`…:done`), not just the head — so a phase is idle when its FULL string, its
// HEAD segment, OR ITS TAIL (or any) segment is in IDLE_PHASES. Genuinely-active
// phases (`tournament:round_0:rung0_m3`, `proposing:field`) keep no idle token
// in any segment and stay active.
export function isActivePhase(phase) {
  if (phase == null) return false;
  const p = String(phase).trim().toLowerCase();
  if (p === '') return false;
  if (IDLE_PHASES.has(p)) return false;
  const segs = p.split(':');
  for (const seg of segs) {
    if (IDLE_PHASES.has(seg)) return false;
  }
  return true;
}

// Parse a heartbeat timestamp into epoch ms. Tolerates an ISO-8601 string or a
// numeric epoch (ms, or seconds — values that small are scaled up). Returns NaN
// when the value is absent/unparseable; a NaN result means the heartbeat has no
// ageable timestamp and is therefore treated as STALE (not live), never fresh.
function parseHeartbeatTs(value) {
  if (value == null) return NaN;
  if (typeof value === 'number') {
    if (!isFinite(value)) return NaN;
    // epoch seconds (10-digit) vs ms (13-digit): scale sub-1e12 values up.
    return value < 1e12 ? value * 1000 : value;
  }
  const s = String(value).trim();
  if (s === '') return NaN;
  // a bare numeric string is an epoch value too.
  if (/^\d+(\.\d+)?$/.test(s)) {
    const n = Number(s);
    return n < 1e12 ? n * 1000 : n;
  }
  const ms = Date.parse(s);
  return isFinite(ms) ? ms : NaN;
}

// Build a short, readable label from the heartbeat phase + the structure.
// Examples:
//   phase "tournament:round_0:rung0_m3", structure "racing" → "racing · rung 0"
//   phase "tournament:round_2"          , structure "swiss"   → "swiss · round 2"
//   phase "proposing:field"                                   → "proposing field"
//   phase "tournament:..."  , structure "single_elim"         → "single elim · …"
export function phaseLabel(phase, structure) {
  const p = String(phase == null ? '' : phase).trim();
  if (p === '') return structure ? prettyStructure(structure) : 'running';
  const segs = p.split(':').filter(Boolean);
  const head = (segs[0] || '').toLowerCase();

  if (head === 'proposing') {
    const what = segs.slice(1).join(' ').replace(/_/g, ' ').trim();
    return what ? 'proposing ' + what : 'proposing';
  }

  if (head === 'tournament') {
    const struct = prettyStructure(structure || 'tournament');
    // surface the most specific round/rung token we can find.
    const detail = roundDetail(segs.slice(1), structure);
    return detail ? struct + ' · ' + detail : struct;
  }

  // any other phase: humanise the colon path.
  return p.replace(/:/g, ' · ').replace(/_/g, ' ');
}

// Pull a "rung N" / "round N" style detail out of the tournament phase
// segments. Racing speaks rungs; the others speak rounds.
function roundDetail(segs, structure) {
  const struct = String(structure || '').toLowerCase();
  const wantRung = struct === 'racing';
  let round = null;
  let rung = null;
  for (const s of segs) {
    let m = /^round[_-]?(\d+)$/.exec(s);
    if (m) { round = +m[1]; continue; }
    m = /^rung(\d+)/.exec(s);
    if (m) { rung = +m[1]; continue; }
  }
  if (wantRung && rung != null) return 'rung ' + rung;
  if (round != null) return (wantRung ? 'rung ' : 'round ') + round;
  if (rung != null) return 'rung ' + rung;
  return null;
}

// ── the SINGLE structure-aware standings-status mapper ───────────────
//
// Map a RAW standings status (alive / eliminated / champion / competing,
// per /api/active-tournament's `standings[].status`) onto the word the
// standings table / pills show — STRUCTURE-CORRECT so a non-racing
// tournament never borrows racing vocabulary. The terminal verdicts
// (champion / eliminated) pass through unchanged in EVERY structure; only
// the "still in contention" word is structure-specific:
//   elim  → "in bracket" (a competitor still alive in the tree)
//   swiss → "playing"
//   racing→ "racing"
//   else  → "alive"
// This is the ONE place the mapping lives so the old `live && champion →
// "racing"` leak (and any other structure-blind word) cannot recur.
export function structureStatusLabel(rawStatus, structure) {
  const s = String(rawStatus == null ? '' : rawStatus).trim().toLowerCase();
  // terminal verdicts read identically in every structure.
  if (s === 'champion') return 'champion';
  if (s === 'eliminated') return 'eliminated';
  if (s === 'alive') return 'alive';
  // the "still competing" word is structure-specific. Raw 'competing' (or any
  // other non-terminal token) maps to the structure's in-contention word.
  const struct = String(structure || '').toLowerCase();
  if (struct === 'single_elim' || struct === 'double_elim') return 'in bracket';
  if (struct === 'swiss') return 'playing';
  if (struct === 'racing') return 'racing';
  // gauntlet / unknown: a neutral, structure-blind "alive".
  return s === 'competing' ? 'alive' : (s || 'alive');
}

function prettyStructure(structure) {
  const s = String(structure || '').toLowerCase();
  switch (s) {
    case 'gauntlet': return 'gauntlet';
    case 'single_elim': return 'single elim';
    case 'double_elim': return 'double elim';
    case 'swiss': return 'swiss';
    case 'racing': return 'racing';
    case '': return 'tournament';
    default: return s.replace(/_/g, ' ');
  }
}

// The structure-agnostic verdict. `running` is true when ANY live signal
// fires; the label + structure + in-flight count describe it for the chrome.
//
//   heartbeat        — the /api/heartbeat object (or state.heartbeat).
//   activeRuns       — the /api/active-runs array (or state.activeRuns).
//   activeTournament — the /api/active-tournament object (or null).
//   now              — current epoch ms (defaults to Date.now(); injectable for
//                      deterministic tests).
export function deriveLiveStatus({ heartbeat, activeRuns, activeTournament } = {}, now = Date.now()) {
  const hb = (heartbeat && typeof heartbeat === 'object') ? heartbeat : null;
  const phase = hb ? hb.phase : null;
  const runs = Array.isArray(activeRuns) ? activeRuns : [];
  const at = (activeTournament && typeof activeTournament === 'object') ? activeTournament : null;

  const phaseActive = isActivePhase(phase);
  const inFlight = runs.length;
  const tournamentRunning = !!(at && String(at.phase || '').toLowerCase() === 'running');

  // Heartbeat freshness: a FROZEN heartbeat from a dead/torn-down process must
  // NOT read "live" — no matter how its phase/tournament file froze. Read the
  // timestamp from any of the known fields (the backend stamps `last_heartbeat`
  // and, when the on-disk record carries no usable stamp, falls back to the
  // file mtime in the same field).
  //
  // THE ONE STALENESS RULE: a heartbeat is FRESH only when it carries a
  // PARSEABLE timestamp AND that timestamp is within STALE_HEARTBEAT_MS of now.
  // A missing/unparseable timestamp is NOT fresh (it is stale) — it must never
  // default to live. This closes the dead-run-shows-LIVE bug: a killed run
  // leaves a heartbeat whose ts cannot be aged out, so treating "no readable
  // ts" as fresh let an active/terminal phase (and a frozen
  // active_tournament.json with phase:"running") read live forever.
  const hbTs = hb
    ? parseHeartbeatTs(hb.last_heartbeat != null ? hb.last_heartbeat
        : hb.emitted_at != null ? hb.emitted_at
        : hb.ts != null ? hb.ts
        : hb.updated_at)
    : NaN;
  const heartbeatFresh = isFinite(hbTs) && (now - hbTs) <= STALE_HEARTBEAT_MS;
  // `heartbeatStale` is the public flag the chrome/digest read: a heartbeat
  // that EXISTS but is not fresh (old OR untimestamped) is stale.
  const heartbeatStale = hb != null && !heartbeatFresh;

  // The orchestrator-derived live signals (the heartbeat `phase` and the
  // active-tournament `phase === "running"`) only count as live while the
  // heartbeat is FRESH — both are written by the same orchestrator process, so
  // a frozen heartbeat means a frozen tournament file too. An in-flight
  // board-unit is the one exception: those records are bumped by per-run
  // worker beaters independent of the orchestrator heartbeat, so a present
  // active-run is ground truth and forces live on its own.
  const phaseLive = phaseActive && heartbeatFresh;
  const tournamentLive = tournamentRunning && heartbeatFresh;

  const running = phaseLive || inFlight > 0 || tournamentLive;

  const structure = at && at.structure ? String(at.structure) : null;
  const label = running
    ? phaseLabel(phase, structure)
    : (heartbeat || activeRuns || activeTournament ? 'idle' : 'done');

  return {
    running,
    structure,
    phase: phase != null ? String(phase) : null,
    inFlight,
    tournamentRunning,
    phaseActive,
    heartbeatStale,
    label,
  };
}

// ── the TREE LIVE-ACTIVITY set (Task 2) ──────────────────────────────
//
// The set of gen ids AND board-entry ids that currently have an in-flight run,
// derived purely from /api/active-runs (the ground-truth in-flight board units).
// The tree sidebar pulses the rows in this set. It is gated on `running` — an
// idle workspace yields the empty set so no stale pulse lingers — and optionally
// scoped to a viewed epoch when the active-runs records carry an epoch tag (a
// foreign-epoch run must not light up the viewed epoch's rows; records with no
// epoch tag are kept, the legacy single-epoch tolerance). Returns a Set of
// string ids (both the generation_id and the entry_id of each in-flight run).
export function treeLiveSet({ activeRuns, running, epochId } = {}) {
  const out = new Set();
  if (!running) return out;
  const runs = Array.isArray(activeRuns) ? activeRuns : [];
  const want = epochId == null ? null : String(epochId);
  for (const r of runs) {
    if (!r || typeof r !== 'object') continue;
    // drop a run whose epoch is KNOWN-and-DIFFERENT from the viewed epoch.
    if (want != null && r.epoch_id != null && String(r.epoch_id) !== want) continue;
    const gen = r.generation_id != null ? r.generation_id : r.gen;
    const entry = r.entry_id != null ? r.entry_id : (r.board_entry_id != null ? r.board_entry_id : r.entry);
    if (gen != null && String(gen) !== '') out.add(String(gen));
    if (entry != null && String(entry) !== '') out.add(String(entry));
  }
  return out;
}

// A stable digest of the derived status so the chrome only re-stamps on a real
// change (digest-gated — a steady heartbeat ping writes ZERO DOM).
export function liveStatusDigest(conn, status) {
  const s = status || {};
  return [
    conn,
    s.running ? 'R' : '-',
    s.structure || '',
    s.phase || '',
    s.inFlight || 0,
    s.tournamentRunning ? 'T' : '',
    s.label || '',
  ].join('|');
}
