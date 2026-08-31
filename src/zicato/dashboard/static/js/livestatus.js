// js/livestatus.js — derive a STRUCTURE-AGNOSTIC live-run status.
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

// SEQ-ADVANCE BUDGET. How long the orchestrator progress
// `seq` may sit unchanged before the run reads STALLED rather than LIVE. A single
// transition can legitimately run a while, so this is generous — distinctly LONGER
// than the heartbeat-staleness window: a frozen-seq run whose heartbeat still
// pulses is STALLED (alive, no progress); only once the heartbeat ALSO freezes is
// it DEAD. ~90s is a few × a per-transition time without masking a wedged loop.
export const SEQ_STALL_BUDGET_MS = 90_000;

// The four chrome run-states. Lowercase so the chrome class is `dt-rs-<state>`;
// the visible label is uppercased by the chrome.
export const RUN_STATE = Object.freeze({
  LIVE: 'live', STALLED: 'stalled', SETTLED: 'settled', DEAD: 'dead',
});

// Is a heartbeat `phase` string a NON-idle (i.e. running) phase?
//
// The phase may be a colon-delimited path (`tournament:round_0:rung0_m3`,
// `evolve_n_rounds:done`). The TERMINAL signal lives in the TAIL segment
// (`…:done`) as well as the head, so a phase is idle when its FULL string, its
// HEAD segment, or its TAIL segment is in IDLE_PHASES. An active phase
// (`tournament:round_0:rung0_m3`, `proposing:field`) carries no idle token in
// any segment and stays active.
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

// The heartbeat's ONE typed liveness timestamp: `ts`, integer MILLISECONDS
// since the epoch, stamped SERVER-SIDE (both the Python reader and the Rust
// supervisor derive it from `last_heartbeat`). The old sec-vs-ms magnitude
// guessing + the four alternate keys are DELETED — a heartbeat without a
// numeric `ts` has no ageable timestamp and reads STALE, never fresh.
function heartbeatTs(hb) {
  const v = hb ? hb.ts : null;
  return (typeof v === 'number' && isFinite(v)) ? v : NaN;
}

// An active-run record's last known progress, as ms-epoch. The server stamps
// `last_progress_ts` (read_active_runs_view) the same way it stamps the
// heartbeat's `ts`; an older server without it degrades to parsing the ISO
// stamps it does send.
function runTs(r) {
  if (!r || typeof r !== 'object') return NaN;
  if (typeof r.last_progress_ts === 'number' && isFinite(r.last_progress_ts)) return r.last_progress_ts;
  const iso = r.last_progress || r.started_at;
  const t = iso ? Date.parse(iso) : NaN;
  return isFinite(t) ? t : NaN;
}

// Active-run records that are ACTUALLY still beating.
//
// `active_runs/*.json` outlives the process that wrote it: a killed run leaves
// its records on disk indefinitely. Counting them makes a long-dead workspace
// report units in flight, which forces `pulsing`, which settles the run-state
// pill on STALLED (alive, no progress) instead of DEAD and keeps the hero up.
// Each record carries the per-run beater's `last_progress`, so a record is aged
// the same way the orchestrator heartbeat is.
//
// A record with NO ageable timestamp at all counts as fresh: the real producer
// (ActiveRun) always writes `started_at`, so an untimestamped record is a
// hand-built or minimal payload, and dropping it would silently under-report a
// live run. A record that lingers past its process always HAS a timestamp; it
// is simply an old one.
//
// The SERVER now decides this per row and sends its verdict as `fresh`
// (read_active_runs_view), and that verdict wins whenever it is present. It
// applies a second gate the browser CANNOT: the record names a worker pid,
// and the server — running on the worker's own host — can ask whether that
// exact process still exists, so a record whose worker is provably gone drops
// out at once instead of waiting out the staleness window. Ageing timestamps
// is the fallback for a server that sends no verdict (the Rust supervisor, or
// any build predating the field); the two agree on every record the client can
// judge for itself.
function runIsFresh(r, now) {
  if (r && typeof r === 'object' && typeof r.fresh === 'boolean') return r.fresh;
  const t = runTs(r);
  return !isFinite(t) || (now - t) <= STALE_HEARTBEAT_MS;
}

function freshRunCount(runs, now) {
  let n = 0;
  for (const r of runs) if (runIsFresh(r, now)) n += 1;
  return n;
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
//   seq              — the progress cursor (state.lastSeq); -1 / absent ⇒ no seq
//                      known yet → the run-state DEGRADES to the
//                      timestamp-derived running/idle/stale verdict.
//   terminal         — the latest frame's terminal marker (state.terminal).
//   lastSeqAdvanceAt — wall-clock ms the cursor last advanced (state); NaN until
//                      the first advance.
//   now              — current epoch ms (defaults to Date.now(); injectable for
//                      deterministic tests).
export function deriveLiveStatus(
  { heartbeat, activeRuns, activeTournament, seq, terminal, lastSeqAdvanceAt } = {},
  now = Date.now(),
) {
  const hb = (heartbeat && typeof heartbeat === 'object') ? heartbeat : null;
  const phase = hb ? hb.phase : null;
  const runs = Array.isArray(activeRuns) ? activeRuns : [];
  const at = (activeTournament && typeof activeTournament === 'object') ? activeTournament : null;

  const phaseActive = isActivePhase(phase);
  // Only runs that are STILL BEATING count as in flight. `runs.length` is the
  // record count on disk, which a dead run never cleans up.
  const inFlight = freshRunCount(runs, now);
  const tournamentRunning = !!(at && String(at.phase || '').toLowerCase() === 'running');

  // Heartbeat freshness: a FROZEN heartbeat from a dead/torn-down process must
  // NOT read "live" — no matter how its phase/tournament file froze. The
  // server stamps the single typed `ts` (ms epoch) on every heartbeat payload.
  //
  // THE ONE STALENESS RULE: a heartbeat is FRESH only when it carries a
  // PARSEABLE timestamp AND that timestamp is within STALE_HEARTBEAT_MS of now.
  // A missing/unparseable timestamp is NOT fresh (it is stale) — it must never
  // default to live. This closes the dead-run-shows-LIVE bug: a killed run
  // leaves a heartbeat whose ts cannot be aged out, so treating "no readable
  // ts" as fresh let an active/terminal phase (and a frozen
  // active_tournament.json with phase:"running") read live forever.
  const hbTs = heartbeatTs(hb);
  const heartbeatFresh = isFinite(hbTs) && (now - hbTs) <= STALE_HEARTBEAT_MS;
  // `heartbeatStale` is the public flag the chrome/digest read: a heartbeat
  // that EXISTS but is not fresh (old OR untimestamped) is stale.
  const heartbeatStale = hb != null && !heartbeatFresh;
  // The heartbeat AGE in ms (for the "last seen Ns ago" affordance), or NaN
  // when there is no ageable timestamp (an untimestamped frozen heartbeat —
  // stale, but with no age to report).
  const heartbeatAgeMs = isFinite(hbTs) ? Math.max(0, now - hbTs) : NaN;

  // The orchestrator-derived live signals (the heartbeat `phase` and the
  // active-tournament `phase === "running"`) only count as live while the
  // heartbeat is FRESH — both are written by the same orchestrator process, so
  // a frozen heartbeat means a frozen tournament file too. A FRESH in-flight
  // board-unit is the one exception: those records are bumped by per-run
  // worker beaters independent of the orchestrator heartbeat, so a run still
  // beating is ground truth and forces live on its own (that is what carries a
  // long model call through a starved orchestrator beat).
  const phaseLive = phaseActive && heartbeatFresh;
  const tournamentLive = tournamentRunning && heartbeatFresh;

  const running = phaseLive || inFlight > 0 || tournamentLive;

  const structure = at && at.structure ? String(at.structure) : null;
  const label = running
    ? phaseLabel(phase, structure)
    : (heartbeat || activeRuns || activeTournament ? 'idle' : 'done');

  // ── the FOUR-STATE run verdict (seq-driven rather than timestamped) ──
  //   SETTLED — a terminal progress marker (cleanly ended). Authoritative.
  //   LIVE    — seq advanced within SEQ_STALL_BUDGET_MS (genuine progress).
  //   STALLED — no advance within budget, but the heartbeat still pulses.
  //   DEAD    — no advance within budget AND no fresh heartbeat.
  // With NO seq known (absent or -1) the run-state DEGRADES to the timestamp
  // verdict: running ⇒ LIVE, a frozen heartbeat ⇒ DEAD, idle ⇒ SETTLED.
  // A real transition is seq >= 1. `progress_log.tail_seq` returns 0 for an
  // ABSENT or empty log, which is exactly the workspace written before the
  // progress log existed — so seq 0 means "no progress recorded" rather than a
  // known cursor. Treating it as known makes the client count the first seq it
  // ever sees as an advance and stamp `lastSeqAdvanceAt = now`, so a long-dead
  // workspace reports that it has just progressed and the pill reads LIVE.
  const seqKnown = typeof seq === 'number' && isFinite(seq) && seq > 0;
  const advanceAge = isFinite(lastSeqAdvanceAt) ? Math.max(0, now - lastSeqAdvanceAt) : NaN;
  const seqAdvancingFresh = seqKnown && isFinite(advanceAge) && advanceAge <= SEQ_STALL_BUDGET_MS;
  // A heartbeat is "pulsing" when it exists AND is fresh (the run-state DEAD/
  // STALLED split). In-flight board units corroborate a pulse too (those are
  // bumped by per-run beaters independent of the orchestrator heartbeat).
  const pulsing = heartbeatFresh || inFlight > 0;

  let runState;
  if (terminal === true) {
    runState = RUN_STATE.SETTLED;
  } else if (!seqKnown) {
    // no seq cursor: derive from the timestamp verdict instead.
    runState = running ? RUN_STATE.LIVE
      : (heartbeatStale ? RUN_STATE.DEAD : RUN_STATE.SETTLED);
  } else if (seqAdvancingFresh) {
    runState = RUN_STATE.LIVE;
  } else if (pulsing) {
    runState = RUN_STATE.STALLED;
  } else {
    runState = RUN_STATE.DEAD;
  }

  // ORCHESTRATOR-ALIVE — the hero-visibility gate. True while the run is LIVE or
  // STALLED (the orchestrator is still pulsing, whether or not the seq is
  // advancing); false once it SETTLES (terminal) or goes DEAD (no fresh pulse).
  // This is BROADER than `running` by design: `running` drops the instant the
  // heartbeat `phase` reads a non-active token and no run is in flight, which
  // happens momentarily between transitions and during a long reasoning call,
  // and that makes the hero FLICKER. Keying the hero on the live PULSE rather
  // than on the advancing seq holds it steady through a long call. STALLED already means
  // "alive, no progress", so a STALLED run keeps the hero (with its STALLED
  // chrome) instead of blinking the whole panel out.
  const alive = runState === RUN_STATE.LIVE || runState === RUN_STATE.STALLED;

  return {
    running,
    alive,
    structure,
    phase: phase != null ? String(phase) : null,
    inFlight,
    tournamentRunning,
    phaseActive,
    heartbeatStale,
    heartbeatAgeMs,
    label,
    runState,
    terminal: terminal === true,
    seqKnown,
    seqAdvanceAgeMs: advanceAge,
  };
}

// The server owns this tri-state. Client ageing may demote a stale `live`
// photograph, but never promotes a terminal verdict.
export const LIVENESS = Object.freeze({
  LIVE: 'live', SETTLED: 'settled', INTERRUPTED: 'interrupted',
});

export function deriveLiveness({ liveness, status } = {}) {
  const served = (liveness && typeof liveness === 'object') ? liveness : null;
  const s = status || {};
  const servedState = served ? String(served.state || '') : '';

  let state;
  if (servedState === LIVENESS.SETTLED || servedState === LIVENESS.INTERRUPTED) {
    state = servedState;
  } else if (servedState === LIVENESS.LIVE) {
    // The server saw a pulse; the client keeps ageing it.
    state = s.alive === false ? LIVENESS.INTERRUPTED : LIVENESS.LIVE;
  } else if (s.runState === RUN_STATE.SETTLED) {
    state = LIVENESS.SETTLED;
  } else {
    state = s.alive ? LIVENESS.LIVE : LIVENESS.INTERRUPTED;
  }

  // When did it end? The server names the moment it saw (the terminal
  // event's stamp, or the last beat before it went cold); the last
  // heartbeat is the fallback. A live run reports no end.
  let endedAt = null;
  if (state !== LIVENESS.LIVE && served) {
    endedAt = served.ended_at || served.last_heartbeat || null;
  }
  return {
    state,
    live: state === LIVENESS.LIVE,
    endedAt,
    lastHeartbeat: served ? (served.last_heartbeat || null) : null,
    epochId: served ? (served.epoch_id || null) : null,
  };
}

// The views' one-line entry point: both verdicts straight off an
// AppState-shaped object. Still pure — it takes the state, never imports it —
// so a fixture object drives it in a test exactly like the real app does.
// EVERY present-tense claim in the console goes through this: if it says
// "racing", "deciding…", "N boards running", or animates a bar, it must first
// have asked here whether anything is actually running.
export function livenessFor(appState, now = Date.now()) {
  const s = appState || {};
  const status = deriveLiveStatus({
    heartbeat: s.heartbeat,
    activeRuns: s.activeRuns,
    activeTournament: s.activeTournament,
    seq: s.lastSeq,
    terminal: s.terminal,
    lastSeqAdvanceAt: s.lastSeqAdvanceAt,
  }, now);
  return { status, liveness: deriveLiveness({ liveness: s.liveness, status }) };
}

// The server owns both clock and epoch scope. An absent epoch id is the
// untagged single-epoch shape and stays trusted; the browser only compares
// identity.
export function epochIsLive(appState, epochId, now = Date.now()) {
  const s = appState || {};
  const verdict = livenessFor(s, now).liveness;
  if (!verdict.live) return false;
  if (epochId == null) return true;
  return verdict.epochId == null || String(verdict.epochId) === String(epochId);
}

// The one-line status band's text. Live surfaces the phase + in-flight
// count; a dead workspace speaks in the PAST TENSE with the date it
// stopped, so no view claims anything is happening when nothing is.
//   live        → "racing · rung 1 · 7 units"
//   settled     → "last run · Jun 8 · settled"
//   interrupted → "last run · Jun 8 · interrupted mid-round"
export function livenessBandText(liveness, status) {
  const s = status || {};
  if (liveness.state === LIVENESS.LIVE) {
    const bits = [s.label || 'running'];
    if (s.inFlight > 0) bits.push(s.inFlight + ' unit' + (s.inFlight === 1 ? '' : 's'));
    return bits.join(' · ');
  }
  const when = shortDate(liveness.endedAt);
  const verdict = liveness.state === LIVENESS.INTERRUPTED
    ? 'interrupted mid-round' : 'settled';
  // No timestamp at all ⇒ nothing ever ran here; say that rather than
  // dating a run that never happened.
  if (!when) return liveness.state === LIVENESS.INTERRUPTED ? verdict : 'no run yet';
  return 'last run · ' + when + ' · ' + verdict;
}

// An ISO stamp as a short "Jun 8" (or "Jun 8, 2025" across a year
// boundary). Empty string when unparseable — callers drop the date rather
// than print "Invalid Date".
//
// The rendering is UTC. Every timestamp zicato writes is UTC, epoch ids are
// UTC-dated (`2026-06-07_e4`), and the run-log rows render the ISO stamp
// verbatim (views/logs.js). Rendering this one date in the viewer's local
// zone would put a workspace 7 hours west into a state where the band says
// the run stopped "Jun 7" while the log line beside it reads
// `2026-06-08T03:58:49Z` — the operator doing timezone arithmetic to
// reconcile two surfaces is the same self-contradiction this whole change
// set out to remove. It also means two people in different zones reading
// one workspace name the same day.
export function shortDate(iso, now = new Date()) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const base = MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate();
  return d.getUTCFullYear() === now.getUTCFullYear() ? base : base + ', ' + d.getUTCFullYear();
}

// Format a heartbeat age (ms) into a short "last seen Ns ago" affordance.
// `NaN` (an untimestamped frozen heartbeat) reads as a bare "stale" — there
// is no age to report but the run is still not live. Used by the chrome to
// show WHY a frozen run has stopped, rather than freezing silently.
export function staleLabel(ageMs) {
  if (!isFinite(ageMs)) return 'stale';
  const s = Math.round(ageMs / 1000);
  // Keep seconds precision under two minutes — for a just-killed run the exact
  // "last seen 90s ago" is more actionable than a coarse "1m ago".
  if (s < 120) return `last seen ${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `last seen ${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `last seen ${h}h ago`;
  // A workspace can sit dead for months. "last seen 1512h ago" is technically
  // true and useless; days is the unit an operator reads at that distance.
  return `last seen ${Math.floor(h / 24)}d ago`;
}

// ── the TREE LIVE-ACTIVITY set (Task 2) ──────────────────────────────
//
// The set of gen ids AND board-entry ids that currently have an in-flight run,
// derived purely from /api/active-runs (the ground-truth in-flight board units).
// The tree sidebar pulses the rows in this set. It is gated on `running` — an
// idle workspace yields the empty set so no stale pulse lingers — and optionally
// scoped to a viewed epoch when the active-runs records carry an epoch tag (a
// foreign-epoch run must not light up the viewed epoch's rows; records with no
// epoch tag are kept, the untagged single-epoch case). Returns a Set of
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
    const gen = r.generation_id;
    const entry = r.entry_id;
    if (gen != null && String(gen) !== '') out.add(String(gen));
    if (entry != null && String(entry) !== '') out.add(String(entry));
  }
  return out;
}

// The visible chrome label for a four-state run verdict (uppercased token).
// SETTLED on a never-run / empty workspace would read oddly, so the chrome
// only shows the run-state word while there is SOMETHING to report; the
// mapping itself is total.
export function runStateLabel(runState) {
  switch (runState) {
    case RUN_STATE.LIVE: return 'LIVE';
    case RUN_STATE.STALLED: return 'STALLED';
    case RUN_STATE.SETTLED: return 'SETTLED';
    case RUN_STATE.DEAD: return 'DEAD';
    default: return '';
  }
}

// A stable digest of the derived status so the chrome only re-stamps on a real
// change (digest-gated — a steady heartbeat ping writes ZERO DOM).
export function liveStatusDigest(conn, status) {
  const s = status || {};
  // Bucket the heartbeat age to ~5s so a steady tick does not re-stamp the
  // chrome every frame, but a transition into the stale window (and the
  // coarse "last seen Ns ago" climb) still flips the digest.
  const ageBucket = (s.heartbeatStale && isFinite(s.heartbeatAgeMs))
    ? Math.floor(s.heartbeatAgeMs / 5000) : '';
  // The run-state is a DISCRETE token, so fold it raw — it already captures
  // every LIVE/STALLED/SETTLED/DEAD transition. The seq advance AGE climbs
  // every frame and is INTENTIONALLY NOT folded (folding it would re-stamp
  // the chrome on every tick — the render-discipline bug); the discrete
  // runState transition is the only thing that should flip the pill.
  return [
    conn,
    s.running ? 'R' : '-',
    s.structure || '',
    s.phase || '',
    s.inFlight || 0,
    s.tournamentRunning ? 'T' : '',
    s.heartbeatStale ? 'S' + ageBucket : '',
    s.runState || '',
    s.label || '',
  ].join('|');
}
