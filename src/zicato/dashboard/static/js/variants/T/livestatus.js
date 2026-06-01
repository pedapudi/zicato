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

// Is a heartbeat `phase` string a NON-idle (i.e. running) phase?
export function isActivePhase(phase) {
  if (phase == null) return false;
  const p = String(phase).trim().toLowerCase();
  if (p === '') return false;
  // the phase may be a colon-delimited path (`tournament:round_0:rung0_m3`);
  // the FIRST segment names the stage.
  const head = p.split(':')[0];
  if (IDLE_PHASES.has(p) || IDLE_PHASES.has(head)) return false;
  return true;
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
export function deriveLiveStatus({ heartbeat, activeRuns, activeTournament } = {}) {
  const hb = (heartbeat && typeof heartbeat === 'object') ? heartbeat : null;
  const phase = hb ? hb.phase : null;
  const runs = Array.isArray(activeRuns) ? activeRuns : [];
  const at = (activeTournament && typeof activeTournament === 'object') ? activeTournament : null;

  const phaseActive = isActivePhase(phase);
  const inFlight = runs.length;
  const tournamentRunning = !!(at && String(at.phase || '').toLowerCase() === 'running');

  // ANY of the three signals means a run is in flight — the heartbeat phase is
  // the primary signal (it covers proposing + every tournament structure), and
  // the in-flight count / active-tournament corroborate it.
  const running = phaseActive || inFlight > 0 || tournamentRunning;

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
    label,
  };
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
