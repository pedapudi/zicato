// core/state.js — AppState: the single client state object.
//
// One source of truth. Views never fetch and never mutate state — they
// read it in a pure render(state, route). Only the api/sse layer calls
// the mutation methods here. Every mutation that should re-paint emits
// `state:changed` on the bus.

import { bus } from './bus.js';

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

    // The SERVER's liveness verdict — { state, last_heartbeat?, ended_at? }
    // from runtime_view.derive_liveness, riding on /api/state + the SSE
    // snapshot + /api/environment. It is the only reader that can see the
    // terminal progress marker; freshness still ages on the client clock.
    // Read it through livestatus.deriveLiveness, never raw. Null on a
    // server that does not serve the block (the fold degrades).
    this.liveness = null;

    // ── the orchestrator PROGRESS cursor ─────────────────────────────
    // `lastSeq` is the highest progress `seq` seen (SSE frame or heartbeat)
    // — the TRUE liveness cursor: it advances ONLY on a genuine transition,
    // never on the heartbeat timer. Backs the seq no-op-skip gate (sse.js)
    // + the four-state run pill. -1 = no seq seen yet (vs a real seq 0 = a
    // never-run / empty log). `terminal` flips once a frame marks a cleanly-
    // ended loop; `lastSeqAdvanceAt` = wall-clock ms the cursor last advanced.
    this.lastSeq = -1;
    this.terminal = false;
    this.lastSeqAdvanceAt = NaN;

    // /api/active-runs — each: { ..., progress, elapsed_seconds, budget_seconds }
    this.activeRuns = [];

    // /api/active-tournament — flat per-side entries list, or null.
    this.activeTournament = null;
    this.pastTournaments = [];

    // The bracket, the health report, the score trajectory and the epoch
    // contract are not state: a view reads each through the cached
    // per-route accessor in data.js when it renders, so the beat payload
    // does not carry them. (The same holds for the per-matchup detail and
    // drift movements; see core/api.js.)
    // board entry whose inline conversation diff is open.
    this.selectedEntry = null;
    // generation id whose tournament is selected in the picker.
    this.selectedTournament = null;

    // /api/lineage — generations + experiments.
    this.lineage = { generations: [], experiments: [] };
    this.experiments = [];

    // run-log tail + append cursor.
    this.logLines = [];
    this.logTail = { events: [] };
    this.logCursor = null;
    this.logEventsPath = null;

    // /api/health — dashboard service identity (footer).
    this.health = null;
    this.service = { version: '—', port: '—', build: '—' };


    // header epoch summary; `id` follows the served `epoch_id`.
    this.epoch = { id: '—', generation: '—', round: '—', startedAt: null };
    // Lightweight per-epoch summary list — [{ epoch_id, goal }] — from
    // the /api/environment `epochs` key. Lets the Overview's epochs
    // table annotate each row with the epoch's goal without a
    // per-epoch /api/epoch fetch.
    this.epochs = [];

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
    // The zicato-level meta-loop session id (top-bar "execution" link) is
    // a stable field too — preserve it across a minimal beat exactly like
    // harmonograf_url so the execution link doesn't blink out on a ping.
    const prevMeta = this.heartbeat && this.heartbeat.harmonograf_meta_session;
    this.heartbeat = Object.assign({}, this.heartbeat, hb);
    if (typeof this.heartbeat.harmonograf_url !== 'string'
        || this.heartbeat.harmonograf_url.trim() === '') {
      if (typeof prevUrl === 'string' && prevUrl.trim() !== '') {
        this.heartbeat.harmonograf_url = prevUrl;
      }
    }
    if (typeof this.heartbeat.harmonograf_meta_session !== 'string'
        || this.heartbeat.harmonograf_meta_session.trim() === '') {
      if (typeof prevMeta === 'string' && prevMeta.trim() !== '') {
        this.heartbeat.harmonograf_meta_session = prevMeta;
      }
    }
    // The heartbeat `seq` MIRRORS the SSE frame seq (Heartbeat.to_dict),
    // so fold it into the progress cursor too — this keeps the cursor
    // current under plain /api/environment polling (no SSE) and gives the
    // run-state pill a consistent advance timestamp. A heartbeat with no
    // seq key reads back as 0 server-side; the merge above
    // may also leave `seq` undefined on a minimal beat — noteProgress no-ops
    // on a non-numeric/unchanged seq, so a steady beat never moves the cursor.
    if (typeof this.heartbeat.seq === 'number') {
      this.noteProgress(this.heartbeat.seq, undefined);
    }
  }

  // -- progress seq cursor -----------------------------------------

  // Fold a frame's progress `(seq, terminal)` into the cursor. Returns
  // `{ advanced, rollover, present }` the SSE no-op-skip gate reads:
  //   present  — the frame carried a numeric seq (false ⇒ DEGRADE to the
  //              timestamp-plus-signature path).
  //   advanced — seq strictly INCREASED (forward progress), or it is the
  //              first seq ever seen.
  //   rollover — seq went BACKWARDS = the log was cleared on a fresh evolve
  //              boot (seq restarts from 1) ⇒ the run restarted; the caller
  //              FORCES a full re-apply + the cursor resets to the low seq.
  // `lastSeqAdvanceAt` is stamped only on a real advance (the "advancing
  // within budget?" input). A repeat seq (a no-op beat) is neither ⇒ skip.
  noteProgress(seq, terminal, now = Date.now()) {
    if (typeof seq !== 'number' || !isFinite(seq)) {
      return { advanced: false, rollover: false, present: false };
    }
    const prev = this.lastSeq;
    let advanced = false;
    let rollover = false;
    if (prev < 0) {
      // first seq ever — adopt it; treat as an advance so a fresh load paints.
      advanced = true;
    } else if (seq > prev) {
      advanced = true;
    } else if (seq < prev) {
      // backwards ⇒ the log was cleared + restarted (seq begins again at 1).
      rollover = true;
    }
    if (advanced || rollover) {
      this.lastSeq = seq;
      this.lastSeqAdvanceAt = now;
    }
    if (typeof terminal === 'boolean') this.terminal = terminal;
    return { advanced, rollover, present: true };
  }

  // -- snapshot / environment --------------------------------------

  // Fold an SSE `snapshot` frame (build_snapshot shape) into state.
  applySnapshot(snap) {
    if (!snap || typeof snap !== 'object') return;
    if (snap.heartbeat) this.setHeartbeat(snap.heartbeat);
    if (snap.liveness && typeof snap.liveness === 'object') this.liveness = snap.liveness;
    if (snap.active_runs) this.activeRuns = snap.active_runs;
    if ('active_tournament' in snap) this.activeTournament = snap.active_tournament;
    if (Array.isArray(snap.past_tournaments)) this.pastTournaments = snap.past_tournaments;
    if (snap.lineage) this.lineage = snap.lineage;
    if (snap.generations) this.lineage = snap.generations;
    if (snap.experiments) this.experiments = snap.experiments;
    if (snap.service) Object.assign(this.service, snap.service);
    if (snap.health) this.setHealth(snap.health);
    if (snap.run_log) this.setLogTail(snap.run_log);
    if (snap.workspace) this.workspace = snap.workspace;
    if (Array.isArray(snap.epochs)) this.epochs = snap.epochs;
    if (snap.epoch_id) this.epoch.id = snap.epoch_id;
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
    if (env.liveness && typeof env.liveness === 'object') this.liveness = env.liveness;
    if ('active_tournament' in env) this.activeTournament = env.active_tournament;
    if (env.generations && typeof env.generations === 'object') {
      this.lineage = env.generations;
    }
    if (Array.isArray(env.active_runs)) this.activeRuns = env.active_runs;
    if (env.run_log) this.setLogTail(env.run_log);
    if (env.workspace) this.workspace = env.workspace;
    if (Array.isArray(env.epochs)) this.epochs = env.epochs;
    if (env.epoch_id) this.epoch.id = env.epoch_id;
    this._changed();
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
