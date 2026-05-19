// core/state.js — AppState: the single client state object.
//
// One source of truth. Views never fetch and never mutate state — they
// read it in a pure render(state, route). Only the api/sse layer calls
// the mutation methods here. Every mutation that should re-paint emits
// `state:changed` on the bus.

import { bus } from './bus.js';
import { DEFAULT_MARGIN } from './format.js';

// Rolling window the run-log tail keeps in memory.
export const RUN_LOG_WINDOW = 200;
const MAX_LOG_LINES = 20;

export class AppState {
  constructor() {
    this.connected = false;
    this.connecting = true;
    this.mock = false;

    // heartbeat.json — merged, never replaced (see setHeartbeat).
    this.heartbeat = null;

    // /api/active-runs — each: { ..., progress, elapsed_seconds, budget_seconds }
    this.activeRuns = [];

    // /api/active-tournament — flat per-side entries list, or null.
    this.activeTournament = null;
    this.pastTournaments = [];

    // /api/tournaments — the gauntlet bracket.
    this.bracket = null;
    // /api/tournaments/:id detail, cached by generation id.
    this.matchupDetail = new Map();
    // /api/drift-movements/:id, cached by challenger generation id.
    this.driftMovements = {};
    // generation id of the matchup whose detail panel is open.
    this.selectedMatchup = null;
    // board entry whose inline conversation diff is open.
    this.selectedEntry = null;
    // generation id whose tournament is selected in the picker.
    this.selectedTournament = null;

    // /api/health-report — loop-health panel source.
    this.healthReport = null;

    // /api/lineage — generations + experiments.
    this.lineage = { generations: [], experiments: [] };
    this.experiments = [];

    // /api/score-trajectory — environment-wide evolution curve.
    this.scoreTrajectory = { points: [] };

    // run-log tail + append cursor.
    this.logLines = [];
    this.logTail = { events: [] };
    this.logCursor = null;
    this.logEventsPath = null;

    // /api/health — dashboard service identity (footer).
    this.health = null;
    this.service = { version: '—', port: '—', build: '—' };

    this.scoring = { margin: DEFAULT_MARGIN };

    // header epoch summary + full epoch contract.
    this.epoch = { id: '—', generation: '—', round: '—', startedAt: null };
    this.epochDef = null;

    this.workspace = null;

    // Files-view + mutation-browser scratch state (owned by the Files
    // view, but parked here so a route deep-link can seed it).
    this.files = { index: null, selected: null, tree: null, content: null, patches: null };
    this.mutations = { epochId: null, index: null, selected: null, detail: null };
  }

  // -- emit helper -------------------------------------------------

  _changed() { bus.emit('state:changed', this); }

  // -- heartbeat ---------------------------------------------------

  // Merge rather than replace: a heartbeat ping is minimal and omits
  // stable fields like harmonograf_url. A wholesale replace would drop
  // it and kill every deep-link. A null harmonograf_url in the patch
  // is treated as "not provided", not "cleared".
  setHeartbeat(hb) {
    if (!hb || typeof hb !== 'object') return;
    const prevUrl = this.heartbeat && this.heartbeat.harmonograf_url;
    this.heartbeat = Object.assign({}, this.heartbeat, hb);
    if (typeof this.heartbeat.harmonograf_url !== 'string'
        || this.heartbeat.harmonograf_url.trim() === '') {
      if (typeof prevUrl === 'string' && prevUrl.trim() !== '') {
        this.heartbeat.harmonograf_url = prevUrl;
      }
    }
  }

  // -- snapshot / environment --------------------------------------

  // Fold an SSE `snapshot` frame (build_snapshot shape) into state.
  applySnapshot(snap) {
    if (!snap || typeof snap !== 'object') return;
    if (snap.heartbeat) this.setHeartbeat(snap.heartbeat);
    if (snap.active_runs) this.activeRuns = snap.active_runs;
    if ('active_tournament' in snap) this.activeTournament = snap.active_tournament;
    if (Array.isArray(snap.past_tournaments)) this.pastTournaments = snap.past_tournaments;
    if (snap.bracket && typeof snap.bracket === 'object') this.bracket = snap.bracket;
    if (snap.tournaments && typeof snap.tournaments === 'object') this.bracket = snap.tournaments;
    if (snap.health_report && typeof snap.health_report === 'object') {
      this.healthReport = snap.health_report;
    }
    if (snap.lineage) this.lineage = snap.lineage;
    if (snap.generations) this.lineage = snap.generations;
    if (snap.experiments) this.experiments = snap.experiments;
    if (snap.score_trajectory && Array.isArray(snap.score_trajectory.points)) {
      this.scoreTrajectory = snap.score_trajectory;
    }
    if (snap.service) Object.assign(this.service, snap.service);
    if (snap.health) this.setHealth(snap.health);
    if (snap.run_log) this.setLogTail(snap.run_log);
    if (snap.scoring) Object.assign(this.scoring, snap.scoring);
    if (snap.workspace) this.workspace = snap.workspace;
    this._foldEpoch(snap.epoch);
    if (snap.epoch_summary && typeof snap.epoch_summary === 'object') {
      Object.assign(this.epoch, snap.epoch_summary);
    }
    if (Array.isArray(snap.log_tail)) {
      this.logLines = snap.log_tail.slice(-MAX_LOG_LINES);
    }
    this._changed();
  }

  // Fold an /api/environment response into state. Each component
  // degrades independently — a missing key leaves the prior value.
  applyEnvironment(env) {
    if (!env || typeof env !== 'object') return;
    if (env.heartbeat) this.setHeartbeat(env.heartbeat);
    if ('active_tournament' in env) this.activeTournament = env.active_tournament;
    if (env.tournaments && typeof env.tournaments === 'object') {
      this.bracket = env.tournaments;
    }
    if (env.generations && typeof env.generations === 'object') {
      this.lineage = env.generations;
    }
    if (env.score_trajectory && Array.isArray(env.score_trajectory.points)) {
      this.scoreTrajectory = env.score_trajectory;
    }
    if (Array.isArray(env.active_runs)) this.activeRuns = env.active_runs;
    if (env.health_report && typeof env.health_report === 'object') {
      this.healthReport = env.health_report;
    }
    if (env.run_log) this.setLogTail(env.run_log);
    if (env.workspace) this.workspace = env.workspace;
    this._foldEpoch(env.epoch);
    this._changed();
  }

  // The environment / snapshot `epoch` key is the full contract object.
  _foldEpoch(epoch) {
    if (!epoch || typeof epoch !== 'object') return;
    if ('epoch_id' in epoch || 'board' in epoch || 'brief' in epoch || 'rubric' in epoch) {
      this.epochDef = epoch;
      if (epoch.epoch_id) this.epoch.id = epoch.epoch_id;
    } else {
      Object.assign(this.epoch, epoch);
    }
  }

  setEpochDef(def) {
    if (def && typeof def === 'object') {
      this.epochDef = def;
      if (def.epoch_id) this.epoch.id = def.epoch_id;
      this._changed();
    }
  }

  setBracket(bracket) {
    if (bracket && typeof bracket === 'object') { this.bracket = bracket; this._changed(); }
  }

  // /api/health — footer service identity.
  setHealth(health) {
    if (!health || typeof health !== 'object') return;
    this.health = health;
    if (health.version != null) this.service.version = health.version;
    if (health.port != null) this.service.port = health.port;
    if (health.build != null) this.service.build = health.build;
  }

  // -- run-log -----------------------------------------------------

  // Normalise to { events:[...] }; capture the append cursor.
  setLogTail(tail) {
    if (Array.isArray(tail)) {
      this.logTail = { events: tail };
      this.logCursor = null;
      return;
    }
    if (tail && Array.isArray(tail.events)) {
      this.logTail = { events: tail.events };
      this.logCursor = (tail.cursor != null) ? tail.cursor : this.logCursor;
      if (tail.events_path != null) this.logEventsPath = tail.events_path;
    }
  }

  // Merge an `?after=<cursor>` batch into the tail. A changed
  // events_path (run rolled over) resets to the incoming batch. Returns
  // the newly-appended events so the caller can append-only render.
  mergeLogTail(tail) {
    if (!tail || !Array.isArray(tail.events)) return [];
    let appended = tail.events;
    if (tail.events_path != null && this.logEventsPath != null
        && tail.events_path !== this.logEventsPath) {
      this.logTail = { events: tail.events };
    } else if (tail.events.length > 0) {
      const merged = this.logTail.events.concat(tail.events);
      this.logTail = { events: merged.slice(-RUN_LOG_WINDOW) };
    } else {
      appended = [];
    }
    if (tail.events_path != null) this.logEventsPath = tail.events_path;
    if (tail.cursor != null) this.logCursor = tail.cursor;
    if (appended.length) bus.emit('log:appended', { events: appended });
    return appended;
  }

  setHealthReport(report) {
    if (report && typeof report === 'object') { this.healthReport = report; this._changed(); }
  }

  setMatchupDetail(genId, detail) {
    if (genId && detail && typeof detail === 'object') {
      this.matchupDetail.set(genId, detail);
      this._changed();
    }
  }

  // -- selectors ---------------------------------------------------

  // Every tournament selectable in the picker: active first, then past.
  allTournaments() {
    const list = [];
    if (this.activeTournament) list.push({ ...this.activeTournament, __active: true });
    for (const t of this.pastTournaments) list.push({ ...t, __active: false });
    return list;
  }

  appendLog(line) {
    this.logLines.push(line);
    while (this.logLines.length > MAX_LOG_LINES) this.logLines.shift();
  }
}

// The process-wide singleton.
export const state = new AppState();
