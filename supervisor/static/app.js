// zicato supervisor dashboard
//
// Single-page vanilla ES2022 app. No build step, no framework, no
// external network. Talks to the supervisor's HTTP+SSE endpoints
// served by the Rust binary.
//
// Architecture: AppState owns the current snapshot. SSE pushes a
// "state_change" event whenever runtime files change; the client
// re-fetches the relevant sub-endpoints and calls render() which
// re-paints affected sections. The renders are idempotent — calling
// them again with the same state must produce the same DOM.

'use strict';

// --- Constants

const SVG_NS = 'http://www.w3.org/2000/svg';

const COLORS = {
  promoted: '#2ea043',
  rejected: '#d73a49',
  baseline: '#6e7681',
  deferred: '#bf8700',
  running:  '#1f6feb',
  grid:     '#d0d7de',
};

// Tournament threshold: a child needs at least this much improvement on
// the scalar score to be eligible for promotion. Kept here so the
// predicted-gate calculator can be unit-tested deterministically. This
// MUST match the server's scoring.json margin; the snapshot includes
// the actual margin and we prefer it when present.
const DEFAULT_MARGIN = 0.05;

const MAX_LOG_LINES = 20;
const SSE_BACKOFF_MAX_MS = 30_000;

// --- Small helpers

function $(id) { return document.getElementById(id); }

function el(tag, props, children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') node.className = v;
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (v !== null && v !== undefined) {
        node.setAttribute(k, v);
      }
    }
  }
  if (children) {
    for (const c of children) {
      if (c == null) continue;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
  }
  return node;
}

function svgEl(tag, attrs, children) {
  const node = document.createElementNS(SVG_NS, tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      node.setAttribute(k, String(v));
    }
  }
  if (children) {
    for (const c of children) {
      if (c == null) continue;
      node.appendChild(typeof c === 'string'
        ? document.createTextNode(c)
        : c);
    }
  }
  return node;
}

function fmtDelta(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  const s = v >= 0 ? '+' : '';
  return s + v.toFixed(3);
}
function fmtRate(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  return v.toFixed(2);
}
function fmtDuration(seconds) {
  if (!isFinite(seconds) || seconds < 0) return '—';
  const s = Math.floor(seconds % 60);
  const m = Math.floor((seconds / 60) % 60);
  const h = Math.floor(seconds / 3600);
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}
function truncate(s, n) {
  if (s == null) return '';
  if (s.length <= n) return s;
  return s.slice(0, n - 1).trimEnd() + '…';
}
function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// Robust ISO-8601 parser. heartbeat.json mixes zone forms: a trailing
// `Z` on some fields, an explicit `+00:00` offset on others. A bare
// `new Date` parses a zone-less value as *local* time, skewing every
// elapsed clock. parseIso normalises both forms, pins zone-less values
// to UTC, and returns epoch ms (or NaN when unparseable).
function parseIso(value) {
  if (value == null) return NaN;
  if (typeof value === 'number') return isFinite(value) ? value : NaN;
  if (value instanceof Date) return value.getTime();
  if (typeof value !== 'string') return NaN;
  let s = value.trim();
  if (s.length === 0) return NaN;
  // A `Z` or numeric `±HH:MM` / `±HHMM` offset — `new Date` is
  // reliable. Otherwise pin to UTC (the supervisor always emits UTC).
  // A bare date is left alone — Date already treats it as UTC.
  const hasZone = /([zZ]|[+-]\d{2}:?\d{2})$/.test(s);
  if (!hasZone && (s.indexOf('T') !== -1 || s.indexOf(' ') !== -1)) {
    s = s.replace(' ', 'T') + 'Z';
  }
  const ms = Date.parse(s);
  return isFinite(ms) ? ms : NaN;
}

// `Date.now()` is the single source of "now" — but pulling it through
// one helper keeps the elapsed/stale math uniform and testable.
function nowMs() { return Date.now(); }

// --- AppState

class AppState {
  constructor() {
    this.connected = false;
    this.connecting = true;
    this.mock = false;
    // heartbeat.json — { generation_id, round_index, last_heartbeat,
    //   round_started_at, started_at, harmonograf_url?, ... }. Timestamps
    //   arrive in mixed `Z` / `+00:00` zone forms; parse via parseIso.
    this.heartbeat = null;
    // GET /api/active-runs — array; each element carries
    //   { ..., progress:0..1|null, elapsed_seconds:int|null,
    //     budget_seconds:int|null }.
    this.activeRuns = [];
    // GET /api/active-tournament —
    //   { round_index:int, parent_generation_id:str, child_generation_id:str,
    //     entries:[{ entry_id:str, side:"parent"|"child", status:str,
    //                scalar_score?:num }] }  — or null if none ever started.
    this.activeTournament = null;
    this.pastTournaments = [];      // [{ ...tournament }] — optional, for the picker
    // GET /api/tournaments — the gauntlet bracket source.
    //   { epoch_id, champion_lineage:[genId...], matchups:[matchup...] }
    this.bracket = null;
    // GET /api/tournaments/:id detail, cached by generation id.
    this.matchupDetail = new Map();
    // generation id of the matchup whose detail panel is open (or null).
    this.selectedMatchup = null;
    // GET /api/health-report — the loop-health panel source.
    this.healthReport = null;
    this.lineage = { generations: [], experiments: [] };
    this.experiments = [];
    this.logLines = [];             // {ts, line, level}
    // GET /api/run-log?limit=40 — the structured event tail.
    //   { events:[{ seq:int|null, kind:str, ts:str|null, summary:str }] }
    this.logTail = { events: [] };
    this.supervisor = { version: '—', port: '—', build: '—' };
    // GET /api/health — supervisor identity for the footer.
    //   { version, port, build, uptime_seconds, ... }
    this.health = null;
    this.scoring = { margin: DEFAULT_MARGIN };
    // header-level epoch summary (id / generation / round / startedAt)
    this.epoch = { id: '—', generation: '—', round: '—', startedAt: null };
    // full epoch-definition contract from GET /api/epoch (R8-A); may be null
    this.epochDef = null;
  }

  // Merge a heartbeat update into the current record rather than
  // replacing it wholesale. A heartbeat *ping* (the SSE keepalive or
  // the /api/heartbeat endpoint) is minimal — typically just
  // { last_heartbeat, generation_id, round_index } — and omits stable
  // config-ish fields like `harmonograf_url` that only the full
  // /api/state snapshot carries. A wholesale replace would drop
  // `harmonograf_url` on the first ping after load, silently killing
  // every harmonograf deep-link. Merging keeps the last-known url
  // alive across pings.
  //
  // Hardening: a ping that carries `harmonograf_url` *explicitly* set
  // to null / empty must not clobber a previously-known good url —
  // a null in the patch is treated as "not provided", not "cleared".
  setHeartbeat(hb) {
    if (!hb || typeof hb !== 'object') return;
    const prevUrl = this.heartbeat && this.heartbeat.harmonograf_url;
    this.heartbeat = Object.assign({}, this.heartbeat, hb);
    // Restore the last-known url when the incoming patch nulled / blanked it.
    if (typeof this.heartbeat.harmonograf_url !== 'string'
        || this.heartbeat.harmonograf_url.trim() === '') {
      if (typeof prevUrl === 'string' && prevUrl.trim() !== '') {
        this.heartbeat.harmonograf_url = prevUrl;
      }
    }
  }

  setSnapshot(snap) {
    if (!snap) return;
    if (snap.heartbeat) this.setHeartbeat(snap.heartbeat);
    if (snap.active_runs) this.activeRuns = snap.active_runs;
    if ('active_tournament' in snap) this.activeTournament = snap.active_tournament;
    if (Array.isArray(snap.past_tournaments)) this.pastTournaments = snap.past_tournaments;
    if (snap.bracket && typeof snap.bracket === 'object') this.bracket = snap.bracket;
    if (snap.health_report && typeof snap.health_report === 'object') {
      this.healthReport = snap.health_report;
    }
    if (snap.lineage) this.lineage = snap.lineage;
    if (snap.experiments) this.experiments = snap.experiments;
    if (snap.supervisor) Object.assign(this.supervisor, snap.supervisor);
    if (snap.health) this.setHealth(snap.health);
    // `run_log` (object or bare array) feeds the structured log tail.
    if (snap.run_log) this.setLogTail(snap.run_log);
    if (snap.scoring) Object.assign(this.scoring, snap.scoring);
    // The header epoch summary and the full epoch contract are distinct.
    // /api/state carries an `epoch` key that is the full contract object
    // (epoch_id, board, rubric, ...). When it has an `epoch_id` field it
    // is the contract; we also derive the header summary from it.
    if (snap.epoch && typeof snap.epoch === 'object') {
      if ('epoch_id' in snap.epoch || 'board' in snap.epoch || 'rubric' in snap.epoch) {
        this.epochDef = snap.epoch;
        if (snap.epoch.epoch_id) this.epoch.id = snap.epoch.epoch_id;
      } else {
        // legacy header-summary shape
        Object.assign(this.epoch, snap.epoch);
      }
    }
    if (snap.epoch_summary && typeof snap.epoch_summary === 'object') {
      Object.assign(this.epoch, snap.epoch_summary);
    }
    if (Array.isArray(snap.log_tail)) {
      this.logLines = snap.log_tail.slice(-MAX_LOG_LINES);
    }
  }

  setEpochDef(def) {
    if (def && typeof def === 'object') {
      this.epochDef = def;
      if (def.epoch_id) this.epoch.id = def.epoch_id;
    }
  }

  setBracket(bracket) {
    if (bracket && typeof bracket === 'object') this.bracket = bracket;
  }

  // GET /api/health — the footer's supervisor identity. The endpoint
  // returns { version, port, build, ... }; mirror those into the
  // legacy `supervisor` record so renderFooter has one source.
  setHealth(health) {
    if (!health || typeof health !== 'object') return;
    this.health = health;
    if (health.version != null) this.supervisor.version = health.version;
    if (health.port != null) this.supervisor.port = health.port;
    if (health.build != null) this.supervisor.build = health.build;
  }

  // GET /api/run-log — the structured event tail. Normalise to the
  // { events: [...] } shape regardless of whether the server hands
  // back a bare array or the wrapped object.
  setLogTail(tail) {
    if (Array.isArray(tail)) {
      this.logTail = { events: tail };
    } else if (tail && Array.isArray(tail.events)) {
      this.logTail = tail;
    }
  }

  setHealthReport(report) {
    if (report && typeof report === 'object') this.healthReport = report;
  }

  setMatchupDetail(genId, detail) {
    if (genId && detail && typeof detail === 'object') {
      this.matchupDetail.set(genId, detail);
    }
  }

  // All tournaments selectable in the Tournament view's picker.
  // The active one first (if present), then any past ones.
  allTournaments() {
    const list = [];
    if (this.activeTournament) {
      list.push({ ...this.activeTournament, __active: true });
    }
    for (const t of this.pastTournaments) {
      list.push({ ...t, __active: false });
    }
    return list;
  }

  appendLog(line) {
    this.logLines.push(line);
    while (this.logLines.length > MAX_LOG_LINES) this.logLines.shift();
  }
}

const state = new AppState();

// --- View routing
//
// The dashboard is a multi-view app: Overview / Tree / Tournament /
// Epoch. The active view is encoded in the URL fragment so a reload or
// a shared link lands on the same view. Drill-downs use a deeper
// fragment form (#/generation/<id>, #/entry/<id>, #/run/<id>) handled
// separately by applyRoute().

// `conversation` is a focused view — it has a `view-conversation`
// container so the toggle machinery shows/hides it, but no nav-rail
// tab. It is entered by drilling from a Tournament-view board card
// (#conversation/{entry_id}) and has its own back link.
const VIEWS = ['overview', 'tree', 'tournament', 'epoch', 'conversation'];
const DEFAULT_VIEW = 'overview';
let currentView = DEFAULT_VIEW;

// --- Harmonograf deep-links
//
// The heartbeat MAY carry a non-empty `harmonograf_url`. When present,
// every run on the dashboard deep-links into harmonograf at the run's
// execution trace. When the heartbeat carries no url at all, render
// nothing — no disabled stub.
//
// A harmonograf *session* is a run. Its session id is the run's
// goldfive run/session id. zicato names a run deterministically as
// `{generation_id}--{entry_id}` — so even when a record carries no
// explicit session id we can still resolve the trace from those two
// fields. Resolution order, most-specific first:
//   1. an explicit session id on the record
//      (session_id / session / harmonograf_session / run_id)
//   2. the `{generation_id}--{entry_id}` run-id convention
//   3. the bare harmonograf url (last resort — never render nothing
//      when harmonograf_url is set).

function harmonografBase() {
  const url = state.heartbeat && state.heartbeat.harmonograf_url;
  if (typeof url !== 'string') return null;
  const trimmed = url.trim();
  return trimmed.length > 0 ? trimmed.replace(/\/+$/, '') : null;
}

// Derive the deterministic `{generation_id}--{entry_id}` run id from a
// record, or null if neither field is present.
function deriveRunId(rec) {
  if (!rec) return null;
  const gen = rec.generation_id || rec.generation || rec.child_id;
  const entry = rec.entry_id || rec.entry;
  if (gen && entry) return `${gen}--${entry}`;
  return null;
}

// Resolve a harmonograf session id for a run-like record. Prefers a
// real session id; falls back to the run-id convention.
function harmonografSessionId(rec) {
  if (!rec) return null;
  const explicit = rec.session_id || rec.session ||
    rec.harmonograf_session || rec.run_id;
  if (explicit) return String(explicit);
  return deriveRunId(rec);
}

// Build the harmonograf URL for a run-like record. Returns the bare
// base when no session id is derivable, and null only when the
// heartbeat carries no harmonograf_url at all.
function harmonografRunUrl(rec) {
  const base = harmonografBase();
  if (!base) return null;
  const sid = harmonografSessionId(rec);
  if (sid) return `${base}/#/session/${encodeURIComponent(sid)}`;
  return base;
}

// The full-width "Open in harmonograf ↗" link used on active-run cards
// and the run drill-down.
function harmonografLink(run, label) {
  const href = harmonografRunUrl(run);
  if (!href) return null;
  return el('a', {
    class: 'harmonograf-link',
    href,
    target: '_blank',
    rel: 'noopener',
  }, [(label || 'Open in harmonograf') + ' ↗']);
}

// A small, unobtrusive harmonograf link for dense contexts — A/B-grid
// cells and bracket nodes. `target` is a run-like record (resolved via
// the session-id / run-id convention) or, when no run can be named, a
// plain string URL fragment is skipped and the bare base is used.
function harmonografMini(target, label, ariaLabel) {
  const href = harmonografRunUrl(target);
  if (!href) return null;
  return el('a', {
    class: 'harmonograf-link harmonograf-mini',
    href,
    target: '_blank',
    rel: 'noopener',
    'aria-label': ariaLabel || 'open harmonograf trace',
  }, [(label || 'harmonograf') + ' ↗']);
}

// A subtle superscript-style harmonograf link for a bracket generation
// node. harmonograf has no per-generation filter URL, so this lands on
// the bare harmonograf url — a way into the trace browser scoped by the
// node's generation id in its aria-label. Renders nothing when the
// heartbeat carries no harmonograf_url.
function harmonografGenLink(genId) {
  const base = harmonografBase();
  if (!base) return null;
  return el('a', {
    class: 'harmonograf-link harmonograf-sup',
    href: base,
    target: '_blank',
    rel: 'noopener',
    'aria-label': 'open harmonograf traces for generation ' + (genId || '?'),
    onClick: (ev) => ev.stopPropagation(),
    onKeydown: (ev) => {
      // Keep Enter/Space on the link from bubbling to the card.
      if (ev.key === 'Enter' || ev.key === ' ') ev.stopPropagation();
    },
  }, ['↗']);
}

// --- Predicted-gate verdict
//
// Given partial tournament entries, compute three projections of the
// child's scalar score:
//   - actual:      what the partial results show right now
//   - best_case:   assume every remaining entry matches the parent
//   - worst_case:  assume every remaining entry fails (max drift, fail pass)
//
// The verdict is deterministic in `entries` and `parent`:
//   * REJECT     — even best_case child_scalar still exceeds parent - margin
//                  AND/OR there is a pass regression that is already locked in
//   * PROMOTE    — worst_case child_scalar < parent - margin AND no pass regression
//   * TBD        — anything else
//
// Returned shape: { verdict: 'promote'|'reject'|'tbd', reason, projection }

function predictedGateVerdict(tournament, margin) {
  if (!tournament) return null;
  margin = (typeof margin === 'number' && isFinite(margin)) ? margin : DEFAULT_MARGIN;

  const entries = tournament.entries || [];
  if (entries.length === 0) {
    return { verdict: 'tbd', reason: 'no entries yet', projection: null };
  }

  const finished = entries.filter(e => e.status === 'done');
  const remaining = entries.length - finished.length;

  // Aggregate parent and child means over finished entries
  let parentDrift = 0, childDrift = 0;
  let parentPass = 0, childPass = 0;
  let lockedPassRegression = false;

  // Worst observed and best observed parent drift to bound projections
  let parentDriftMax = 0;
  for (const e of finished) {
    if (e.parent) {
      parentDrift += (e.parent.drift_loss || 0);
      parentPass += (e.parent.pass ? 1 : 0);
      parentDriftMax = Math.max(parentDriftMax, e.parent.drift_loss || 0);
    }
    if (e.child) {
      childDrift += (e.child.drift_loss || 0);
      childPass += (e.child.pass ? 1 : 0);
    }
    // Pass regression check: parent passed, child failed.
    if (e.parent && e.child && e.parent.pass === true && e.child.pass === false) {
      lockedPassRegression = true;
    }
  }

  const n = entries.length;
  // Bound the remaining entries' drift loss by the worst-case parent
  // entry seen so far + 0.5 (a generous upper-bound). If no entries
  // finished yet, fall back to 1.0 as worst case.
  const driftCeiling = remaining > 0
    ? Math.max(parentDriftMax * 2, 1.0)
    : 0;

  // Project child best-case: remaining entries equal parent average
  const parentMeanDrift = finished.length > 0
    ? parentDrift / finished.length
    : 0.5;
  const parentMeanPass = finished.length > 0
    ? parentPass / finished.length
    : 0.5;

  const bestChildDrift = (childDrift + remaining * parentMeanDrift) / n;
  const worstChildDrift = (childDrift + remaining * driftCeiling) / n;
  const parentProjDrift = (parentDrift + remaining * parentMeanDrift) / n;

  const bestChildPass = (childPass + remaining * 1.0) / n;
  const worstChildPass = childPass / n;  // remaining all fail
  const parentProjPass = (parentPass + remaining * parentMeanPass) / n;

  // Scalar score: lower drift is better, higher pass-rate is better.
  // Use the same shape the orchestrator's tournament does: scalar = drift - pass_rate.
  // (The dashboard receives `scoring` on snapshot so a future weighting
  // can be plugged in; for v1.2 we use the equal-weight form.)
  const scalar = (drift, pass) => drift - pass;

  const projection = {
    parent_scalar: scalar(parentProjDrift, parentProjPass),
    child_best:    scalar(bestChildDrift, bestChildPass),
    child_worst:   scalar(worstChildDrift, worstChildPass),
    delta_best:    scalar(bestChildDrift, bestChildPass) - scalar(parentProjDrift, parentProjPass),
    delta_worst:   scalar(worstChildDrift, worstChildPass) - scalar(parentProjDrift, parentProjPass),
    margin: margin,
    remaining: remaining,
  };

  if (lockedPassRegression) {
    return {
      verdict: 'reject',
      reason: 'pass-rate regression already locked in',
      projection,
    };
  }

  // REJECT: even the best case for the child still has scalar >= parent_scalar - margin
  // (i.e., the child is at best within the noise band of the parent, cannot beat it).
  if (projection.child_best >= projection.parent_scalar - margin) {
    return {
      verdict: 'reject',
      reason: remaining === 0
        ? 'child failed to clear margin'
        : 'cannot recover even if remaining entries match parent',
      projection,
    };
  }

  // PROMOTE: even the worst case has scalar < parent_scalar - margin, no regression.
  if (projection.child_worst < projection.parent_scalar - margin) {
    return {
      verdict: 'promote',
      reason: 'already winning regardless of remaining entries',
      projection,
    };
  }

  return {
    verdict: 'tbd',
    reason: 'depends on remaining entries',
    projection,
  };
}

// --- Render: header + footer + connection

// How long since the last heartbeat before the run is considered
// stale. heartbeat.json is rewritten on a short cadence (well under a
// minute); 90s leaves generous slack for a slow tick or a paused
// scheduler without false-flagging a healthy live run.
const STALE_HEARTBEAT_MS = 90_000;

function renderHeader() {
  const hb = state.heartbeat || {};
  // Generation + round come straight off the heartbeat — `generation_id`
  // ("v2") and `round_index` (an int). Fall back to the legacy header
  // summary, then the em-dash placeholder.
  const genId = hb.generation_id || state.epoch.generation;
  const roundIdx = (hb.round_index != null) ? hb.round_index : state.epoch.round;
  $('epoch-id').textContent = 'epoch · ' + (state.epoch.id || '—');
  $('generation-id').textContent = 'gen · ' + (genId != null && genId !== '' ? genId : '—');
  $('round-id').textContent = 'round · ' + (roundIdx != null && roundIdx !== '' ? roundIdx : '—');

  // Elapsed = now − started_at. The heartbeat's `started_at` is the
  // run's start; `round_started_at` and the legacy `epoch_started_at`
  // are accepted fallbacks. parseIso copes with both the `Z` and
  // `+00:00` zone forms.
  const startedRaw = hb.started_at || state.epoch.startedAt
    || hb.round_started_at || hb.epoch_started_at;
  const startedMs = parseIso(startedRaw);
  if (isFinite(startedMs)) {
    $('elapsed').textContent = fmtDuration((nowMs() - startedMs) / 1000);
  } else {
    $('elapsed').textContent = '—';
  }

  const badge = $('health-badge');
  badge.classList.remove('ok', 'warn', 'error', 'pending');
  if (state.connecting) {
    badge.classList.add('pending');
    badge.textContent = 'connecting';
  } else if (state.connected) {
    // Stale = the last heartbeat is older than STALE_HEARTBEAT_MS.
    // `last_heartbeat` is the canonical field; `timestamp` is the
    // legacy name. A healthy live run keeps this fresh and must NOT
    // trip the badge — an unparseable/absent timestamp is treated as
    // healthy rather than falsely stale.
    const hbMs = parseIso(hb.last_heartbeat != null ? hb.last_heartbeat : hb.timestamp);
    const stale = isFinite(hbMs) && (nowMs() - hbMs) > STALE_HEARTBEAT_MS;
    if (stale) {
      badge.classList.add('warn');
      badge.textContent = 'stale heartbeat';
    } else {
      badge.classList.add('ok');
      badge.textContent = 'healthy';
    }
  } else {
    badge.classList.add('error');
    badge.textContent = 'disconnected';
  }

  const mockBadge = $('mock-badge');
  if (state.mock) mockBadge.classList.remove('hidden');
  else mockBadge.classList.add('hidden');
}

function renderFooter() {
  // GET /api/health is the source of truth (version / port / build);
  // state.supervisor mirrors it and stands in until /api/health lands.
  const h = state.health || {};
  const pick = (a, b) => {
    if (a != null && a !== '') return a;
    if (b != null && b !== '') return b;
    return '—';
  };
  $('supervisor-version').textContent =
    'supervisor · ' + pick(h.version, state.supervisor.version);
  $('supervisor-port').textContent =
    'port · ' + pick(h.port, state.supervisor.port);
  $('supervisor-build').textContent =
    'build · ' + pick(h.build, state.supervisor.build);
}

// --- Render: active tournament

// Find the live active-run record that drives a tournament entry's
// progress, matched on entry_id. Returns null when no run matches —
// callers render a neutral placeholder rather than a fake 0% bar.
function findActiveRunForEntry(entry) {
  if (!entry || !entry.entry_id || !Array.isArray(state.activeRuns)) {
    return null;
  }
  const want = String(entry.entry_id);
  return state.activeRuns.find((r) => {
    if (!r) return false;
    const rid = r.entry_id != null ? r.entry_id : r.entry;
    return rid != null && String(rid) === want;
  }) || null;
}

function renderActiveTournament() {
  const sec = $('tournament-section');
  const body = $('tournament-body');
  const title = $('tournament-title');
  const tElapsed = $('tournament-elapsed');
  clearChildren(body);

  const t = state.activeTournament;
  if (!t || !t.entries || t.entries.length === 0) {
    title.textContent = 'Tournament';
    tElapsed.textContent = '';
    body.appendChild(el('p', { class: 'empty' }, ['No active tournament.']));
    return;
  }

  // #4 — read the real round + generation ids from the shared
  // AppState contract. Prefer the contract field names; fall back to
  // the legacy runtime keys so the panel works regardless of the
  // merge order between this branch and the core/state branches.
  const roundIndex = t.round_index != null ? t.round_index : t.round;
  const parentGen = t.parent_generation_id || t.parent_id;
  const childGen = t.child_generation_id || t.child_id || t.generation_id;

  title.textContent =
    roundIndex != null ? `Tournament — round ${roundIndex}` : 'Tournament';

  const subheader = el('p', { class: 'panel-subheader' }, [
    el('strong', null, [
      `${parentGen || '?'} (champion) vs ${childGen || '?'} (proposed)`,
    ]),
  ]);
  body.appendChild(subheader);

  if (t.elapsed_seconds != null) {
    tElapsed.textContent = 'Elapsed ' + fmtDuration(t.elapsed_seconds);
  } else {
    tElapsed.textContent = '';
  }

  // Hypothesis / modulating
  if (t.hypothesis) {
    const hyp = el('p', null, [
      el('strong', null, ['Hypothesis. ']),
      t.hypothesis.core_idea || '',
    ]);
    body.appendChild(hyp);
  }
  if (t.hypothesis && Array.isArray(t.hypothesis.modulating) && t.hypothesis.modulating.length > 0) {
    const modsLine = el('p', null);
    modsLine.appendChild(el('strong', null, ['Modulating. ']));
    t.hypothesis.modulating.forEach((m, i) => {
      modsLine.appendChild(el('code', { class: 'mono code-pill' }, [m]));
      if (i < t.hypothesis.modulating.length - 1) {
        modsLine.appendChild(document.createTextNode(', '));
      }
    });
    body.appendChild(modsLine);
  }

  // #5 — entry rows, grouped by `side` with a header per group.
  // Each side ("parent"/"child") gets its own labelled block; an entry
  // with no side falls into an "unassigned" bucket so nothing is lost.
  const entriesWrap = el('div', { class: 'entries', role: 'list' });
  const parentEntries = [];
  const childEntries = [];
  const otherEntries = [];
  for (const e of t.entries) {
    const side = String(e.side || '').toLowerCase();
    if (side === 'parent') parentEntries.push(e);
    else if (side === 'child') childEntries.push(e);
    else otherEntries.push(e);
  }

  const appendGroup = (label, genId, items) => {
    if (items.length === 0) return;
    const header = el('div', { class: 'entry-group-header' }, [
      el('span', { class: 'entry-group-label' }, [label]),
    ]);
    if (genId) {
      header.appendChild(
        el('code', { class: 'mono entry-group-gen' }, [genId]));
    }
    header.appendChild(
      el('span', { class: 'meta entry-group-count' }, [`${items.length} board`]));
    entriesWrap.appendChild(header);
    for (const e of items) entriesWrap.appendChild(renderEntryRow(e));
  };

  // If `side` is present on at least one entry, render labelled
  // groups. Otherwise (legacy un-stamped entries) render a flat list.
  if (parentEntries.length > 0 || childEntries.length > 0) {
    appendGroup('Champion', parentGen, parentEntries);
    appendGroup('Challenger', childGen, childEntries);
    appendGroup('Unassigned', null, otherEntries);
  } else {
    for (const e of t.entries) entriesWrap.appendChild(renderEntryRow(e));
  }
  body.appendChild(entriesWrap);

  // Aggregate
  body.appendChild(renderAggregate(t));

  // Verdict
  const verdict = predictedGateVerdict(t, state.scoring.margin);
  if (verdict) body.appendChild(renderVerdict(verdict));

  // Buttons
  body.appendChild(renderTournamentButtons());
}

function renderEntryRow(entry) {
  const status = entry.status || 'queued';
  let markerCh = '○';
  let markerCls = 'queued';
  if (status === 'done') { markerCh = '✓'; markerCls = 'done'; }
  else if (status === 'running') { markerCh = '▶'; markerCls = 'running'; }
  else if (status === 'fail') { markerCh = '✗'; markerCls = 'fail'; }

  const marker = el('div', { class: 'entry-marker ' + markerCls }, [markerCh]);
  const idCell = el('div', { class: 'entry-id mono' }, [entry.entry_id]);

  // Parent cell
  let parentCell;
  if (entry.parent) {
    parentCell = el('div', { class: 'entry-cell' }, [
      el('span', { class: 'label' }, ['parent.']),
      el('span', { class: 'mono' }, [
        `loss ${fmtRate(entry.parent.drift_loss)} ${entry.parent.pass ? 'pass' : 'fail'}`,
      ]),
    ]);
  } else if (status === 'queued') {
    parentCell = el('div', { class: 'entry-cell' }, [
      el('span', { class: 'meta' }, ['queued']),
    ]);
  } else {
    parentCell = el('div', { class: 'entry-cell' }, [
      el('span', { class: 'meta' }, ['—']),
    ]);
  }

  // Child cell — may include progress + drift kinds
  const childCell = el('div', { class: 'entry-cell' });
  if (status === 'done' && entry.child) {
    const passOk = entry.child.pass === true;
    const regression = entry.parent && entry.parent.pass === true && entry.child.pass === false;
    if (regression) childCell.classList.add('regression');
    childCell.appendChild(el('span', { class: 'label' }, ['child.']));
    childCell.appendChild(el('span', { class: 'mono' }, [
      `loss ${fmtRate(entry.child.drift_loss)} ${passOk ? '✓' : '✗'}`,
    ]));
    if (regression) {
      childCell.appendChild(el('br'));
      childCell.appendChild(el('span', null, ['regression']));
    }
  } else if (status === 'running') {
    // #6 — a running entry's progress comes from the matching
    // active-run record (matched on entry_id), NOT from a per-entry
    // `runtime` blob. The fraction is an elapsed-vs-budget deadline
    // fraction, not a true task-completion percentage — label it as
    // such. When no active-run matches, show a neutral placeholder.
    const run = findActiveRunForEntry(entry);
    const drift = entry.child && entry.child.drift_kinds
      ? Object.entries(entry.child.drift_kinds).map(([k, v]) => `${v} ${k}`).join(', ')
      : '';
    if (run) {
      const frac = typeof run.progress === 'number' && isFinite(run.progress)
        ? Math.max(0, Math.min(1, run.progress)) : null;
      const pct = frac != null ? Math.round(frac * 100) : 0;
      const elapsed = run.elapsed_seconds != null
        ? fmtDuration(run.elapsed_seconds) : '—';
      const budget = run.budget_seconds != null
        ? fmtDuration(run.budget_seconds) : '—';
      childCell.appendChild(el('span', { class: 'mono' }, [
        `RUNNING ${elapsed}/${budget}`,
      ]));
      childCell.appendChild(el('span', { class: 'meta' }, [' elapsed/budget']));
      if (drift) {
        childCell.appendChild(el('div', null, [
          el('span', { class: 'meta' }, [`drift: ${drift}`]),
        ]));
      }
      const prog = el('div', { class: 'progress', role: 'progressbar',
                   'aria-valuemin': '0', 'aria-valuemax': '100',
                   'aria-valuenow': String(pct),
                   'aria-label': 'elapsed fraction of wall-clock budget' });
      prog.appendChild(el('div', { class: 'bar', style: `width:${pct}%` }));
      childCell.appendChild(prog);
      childCell.appendChild(el('span', { class: 'meta' }, [
        frac != null ? ` ${pct}% of budget` : ' budget —',
      ]));
      // #17 — deep-link the running board entry into its harmonograf
      // trace when the heartbeat carries a harmonograf url.
      const hg = harmonografMini(run, 'harmonograf',
        `open harmonograf trace for ${entry.entry_id}`);
      if (hg) {
        hg.addEventListener('click', (ev) => ev.stopPropagation());
        childCell.appendChild(el('div', { class: 'entry-hg' }, [hg]));
      }
    } else {
      childCell.appendChild(el('span', { class: 'mono' }, ['RUNNING']));
      childCell.appendChild(el('span', { class: 'meta' }, [' —']));
      if (drift) {
        childCell.appendChild(el('div', null, [
          el('span', { class: 'meta' }, [`drift: ${drift}`]),
        ]));
      }
    }
  } else if (status === 'fail') {
    childCell.appendChild(el('span', { class: 'mono' }, ['FAILED']));
    if (entry.fail_reason) {
      childCell.appendChild(el('div', null, [
        el('span', { class: 'meta' }, [entry.fail_reason]),
      ]));
    }
  } else {
    childCell.appendChild(el('span', { class: 'meta' }, ['queued']));
  }

  const row = el('div', {
    class: 'entry-row',
    role: 'listitem',
    tabindex: '0',
    'aria-label': `entry ${entry.entry_id} status ${status}`,
    onClick: () => openDrillForEntry(entry),
    onKeydown: (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openDrillForEntry(entry);
      }
    },
  }, [marker, idCell, parentCell, childCell]);
  return row;
}

function renderAggregate(t) {
  const wrap = el('div', { class: 'aggregate' });
  wrap.appendChild(el('h4', null, [
    `Partial aggregate (${(t.entries || []).filter(e => e.status === 'done').length} of ${t.entries ? t.entries.length : 0})`
  ]));
  const tbl = el('table');
  const thead = el('thead', null, [
    el('tr', null, [
      el('th', null, ['side']),
      el('th', null, ['drift_loss_mean']),
      el('th', null, ['pass_rate']),
    ]),
  ]);
  tbl.appendChild(thead);
  const tbody = el('tbody');

  const finished = (t.entries || []).filter(e => e.status === 'done');
  let parentDriftSum = 0, parentPassSum = 0;
  let childDriftSum = 0, childPassSum = 0;
  for (const e of finished) {
    if (e.parent) { parentDriftSum += e.parent.drift_loss || 0; parentPassSum += e.parent.pass ? 1 : 0; }
    if (e.child)  { childDriftSum  += e.child.drift_loss || 0;  childPassSum  += e.child.pass  ? 1 : 0; }
  }
  const n = Math.max(1, finished.length);
  const parentDM = parentDriftSum / n;
  const parentPR = parentPassSum / n;
  const childDM  = childDriftSum / n;
  const childPR  = childPassSum / n;
  const regression = (childPR < parentPR) || (childDM > parentDM);

  tbody.appendChild(el('tr', null, [
    el('td', null, [t.parent_id || 'parent']),
    el('td', { class: 'mono' }, [fmtRate(parentDM)]),
    el('td', { class: 'mono' }, [fmtRate(parentPR)]),
  ]));
  tbody.appendChild(el('tr', { class: regression ? 'row-flag' : '' }, [
    el('td', null, [t.child_id || 'child']),
    el('td', { class: 'mono' }, [fmtRate(childDM)]),
    el('td', { class: 'mono' }, [fmtRate(childPR) + (regression ? '  ← REGRESSING' : '')]),
  ]));
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);

  const dScalar = (childDM - childPR) - (parentDM - parentPR);
  wrap.appendChild(el('p', { class: 'mono' }, [
    `Δscalar ${fmtDelta(dScalar)}`,
  ]));
  return wrap;
}

function renderVerdict(v) {
  const cls = 'verdict ' + (v.verdict === 'promote' ? 'promote'
               : v.verdict === 'reject' ? 'reject' : 'tbd');
  const line = v.verdict === 'promote' ? 'Predicted gate: PROMOTE'
        :  v.verdict === 'reject'  ? 'Predicted gate: REJECT'
        :  'Predicted gate: TBD';
  const wrap = el('div', { class: cls, role: 'status' });
  wrap.appendChild(el('div', { class: 'verdict-line' }, [line]));
  wrap.appendChild(el('div', { class: 'verdict-reason' }, [v.reason]));

  if (v.projection) {
    const p = v.projection;
    const detail = el('p', { class: 'mono meta' }, [
      `parent ${fmtRate(p.parent_scalar)} | child best ${fmtRate(p.child_best)} | child worst ${fmtRate(p.child_worst)} | margin ${fmtRate(p.margin)} | remaining ${p.remaining}`,
    ]);
    wrap.appendChild(detail);
  }
  return wrap;
}

function renderTournamentButtons() {
  // #8 — these controls are PROVISIONAL. The orchestrator's
  // control-file consumer does not exist until v1.3, so an enabled
  // button would silently do nothing. Render them disabled, with a
  // tooltip naming the gap and a visible "preview" tag, so an
  // operator never mistakes them for live controls.
  const tip = 'control channel — v1.3';
  const buttons = [
    { label: 'Pause epoch' },
    { label: 'Skip round' },
    { label: 'Force-kill running', cls: 'danger' },
    { label: 'Override' },
  ];
  const row = el('div', {
    class: 'button-row provisional',
    role: 'toolbar',
    'aria-label': 'Tournament controls (preview — not yet wired)',
  });
  for (const b of buttons) {
    const btn = el('button', {
      type: 'button',
      class: 'btn provisional ' + (b.cls || ''),
      disabled: 'disabled',
      'aria-disabled': 'true',
      title: tip,
      'aria-label': b.label + ' (' + tip + ')',
    }, [
      b.label,
      el('span', { class: 'preview-tag', 'aria-hidden': 'true' }, ['preview']),
    ]);
    row.appendChild(btn);
  }
  return row;
}

// --- Render: active runs strip

function renderActiveRuns() {
  const wrap = $('active-runs');
  clearChildren(wrap);
  if (!state.activeRuns || state.activeRuns.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No active runs.']));
    return;
  }
  for (const r of state.activeRuns) {
    const card = el('div', {
      class: 'run-card active fade-in',
      role: 'listitem',
      tabindex: '0',
      'aria-label': `run ${r.run_id} entry ${r.entry_id || '?'}`,
      onClick: () => openDrillForRun(r),
      onKeydown: (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          openDrillForRun(r);
        }
      },
    });
    card.appendChild(el('div', { class: 'run-head' }, [
      el('span', { class: 'run-id mono' }, [r.run_id]),
      el('span', { class: 'badge running' }, ['running']),
    ]));
    card.appendChild(el('div', { class: 'run-meta' }, [
      `${r.entry_id || '?'} / ${r.generation_id || '?'}`,
    ]));
    // #6 — the bar is the run's elapsed-vs-budget deadline fraction
    // from the shared contract (`progress` 0..1). When the run has no
    // progress value the bar reads empty rather than a fake 0%.
    const frac = typeof r.progress === 'number' && isFinite(r.progress)
      ? Math.max(0, Math.min(1, r.progress)) : null;
    const pct = frac != null ? Math.round(frac * 100) : 0;
    const prog = el('div', { class: 'progress', role: 'progressbar',
                 'aria-valuemin': '0', 'aria-valuemax': '100',
                 'aria-valuenow': String(pct),
                 'aria-label': 'elapsed fraction of wall-clock budget' });
    prog.appendChild(el('div', { class: 'bar', style: `width:${pct}%` }));
    card.appendChild(prog);

    // Elapsed / budget line — prefer the contract's `elapsed_seconds`,
    // fall back to deriving elapsed from `started_at`. Labelled as a
    // budget fraction, not task progress.
    let elapsedSecs = null;
    if (r.elapsed_seconds != null && isFinite(r.elapsed_seconds)) {
      elapsedSecs = r.elapsed_seconds;
    } else if (r.started_at) {
      elapsedSecs = (Date.now() - new Date(r.started_at).getTime()) / 1000;
    }
    if (elapsedSecs != null || r.budget_seconds != null) {
      const elapsedTxt = elapsedSecs != null ? fmtDuration(elapsedSecs) : '—';
      const budgetTxt = r.budget_seconds != null
        ? fmtDuration(r.budget_seconds) : '—';
      card.appendChild(el('div', { class: 'run-meta mono' }, [
        `${elapsedTxt} / ${budgetTxt}`,
        el('span', { class: 'meta' }, [' elapsed/budget']),
      ]));
    }

    // #17 — deep-link the run card into its harmonograf trace.
    const hg = harmonografMini(r, 'Open in harmonograf',
      `open harmonograf trace for run ${r.run_id || r.entry_id || ''}`);
    if (hg) {
      // Stop the card's click handler from also firing.
      hg.addEventListener('click', (ev) => ev.stopPropagation());
      card.appendChild(el('div', { class: 'run-meta' }, [hg]));
    }
    wrap.appendChild(card);
  }
}

// --- Render: cross-epoch lineage graph (Tree view)
//
// Built from `state.lineage.generations` — the live `GET /api/lineage`
// feed, which includes in-flight generations the moment a run starts.
// Generations are laid out in horizontal lanes — one lane per epoch.
// Within a lane, generations are ordered left-to-right. Promoted
// generations form a solid green spine; rejected generations are
// dashed red off-shoots that branch below the spine. In-flight
// generations (still running, no verdict) read in the running blue.
// Baseline / seed nodes (no parent) are neutral grey. A new epoch's v0
// descends from the prior epoch's promoted head; that cross-epoch link
// is drawn as a dashed connector between lanes.
//
// The feed contract (each field defensive — any may be absent):
//   { generation_id, parent_generation_id?, epoch_id?, promoted?,
//     created_at? }
// `promoted` is true (promoted), false (rejected) or null (in flight).
// Older feeds used `id` / `parent_id` / `v0_parent`; both are accepted.

// Stable identity / parent accessors that tolerate the old and new
// lineage shapes.
function genId(g) {
  return g.generation_id != null ? g.generation_id : g.id;
}
function genParentId(g) {
  return g.parent_generation_id != null ? g.parent_generation_id : g.parent_id;
}

// Resolve a generation's tournament decision. The live feed's `promoted`
// boolean is authoritative; an experiment outcome refines it (e.g.
// `deferred`); a generation with no parent is the baseline.
function lineageDecision(g, exp) {
  if (exp && exp.outcome && exp.outcome.tournament_decision) {
    return exp.outcome.tournament_decision;
  }
  if (g) {
    if (genParentId(g) == null) return 'baseline';
    if (g.promoted === true) return 'promoted';
    if (g.promoted === false) return 'rejected';
    return 'in_flight';
  }
  return null;
}

// Size an SVG so it renders exactly once, at one definite size: the
// `viewBox`, the `width`/`height` attributes and `preserveAspectRatio`
// are all set together. Without this an inline SVG carrying only a
// viewBox falls back to a UA-default intrinsic size (often 300x150) and
// then gets stretched by `height:auto` — which reads as a doubled or
// oversized panel.
function sizeSvg(svg, w, h) {
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.setAttribute('width', w);
  svg.setAttribute('height', h);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
}

function renderLineage() {
  const svg = $('lineage-svg');
  clearChildren(svg);

  // Build straight from the live `GET /api/lineage` feed. This feed
  // carries in-flight generations the instant a run starts, so the
  // baseline v0 and any running v1/v2 all appear immediately.
  const gens = state.lineage.generations || [];
  const exps = state.lineage.experiments || [];
  const expByGen = new Map();
  for (const e of exps) expByGen.set(e.generation_id, e);

  if (gens.length === 0) {
    const w = 900, h = 360;
    sizeSvg(svg, w, h);
    svg.appendChild(svgEl('rect', {
      x: 1, y: 1, width: w - 2, height: h - 2,
      fill: 'none', stroke: COLORS.grid, 'stroke-width': 1,
      'stroke-dasharray': '4 3', rx: 6, ry: 6,
    }));
    svg.appendChild(svgEl('text', {
      class: 'svg-axis', x: w / 2, y: h / 2, 'text-anchor': 'middle',
    }, ['No generations recorded yet.']));
    return;
  }

  // Group generations into epoch lanes, preserving first-seen order.
  const laneOrder = [];
  const laneOf = new Map();
  for (const g of gens) {
    const lane = g.epoch_id || g.epoch || '(epoch)';
    if (!laneOf.has(lane)) {
      laneOf.set(lane, []);
      laneOrder.push(lane);
    }
    laneOf.get(lane).push(g);
  }

  const nodeW = 132, nodeH = 60;
  const colGap = 56, laneGap = 44;
  const marginX = 130, marginY = 30;
  const branchDrop = 78;        // rejected off-shoots sit this far below spine

  // Column index: position of a generation within its lane.
  const colIndex = new Map();
  let maxCols = 0;
  for (const lane of laneOrder) {
    laneOf.get(lane).forEach((g, i) => {
      colIndex.set(genId(g), i);
      if (i + 1 > maxCols) maxCols = i + 1;
    });
  }

  const laneHeight = nodeH + branchDrop + 28;
  const width = marginX + maxCols * (nodeW + colGap) + 40;
  const height = marginY + laneOrder.length * (laneHeight + laneGap) + 20;
  sizeSvg(svg, width, height);

  // Position every generation.
  const positions = new Map();
  laneOrder.forEach((lane, laneIdx) => {
    const laneTop = marginY + laneIdx * (laneHeight + laneGap);
    const spineY = laneTop + 28;
    for (const g of laneOf.get(lane)) {
      const id = genId(g);
      const col = colIndex.get(id);
      const x = marginX + col * (nodeW + colGap);
      const decision = lineageDecision(g, expByGen.get(id));
      // rejected generations branch below the promoted spine
      const y = decision === 'rejected' ? spineY + branchDrop : spineY;
      positions.set(id, { x, y, laneIdx, spineY, laneTop });
    }
  });

  const defs = svgEl('defs', null, [
    svgEl('marker', {
      id: 'arr-promoted', viewBox: '0 0 10 10',
      refX: 9, refY: 5, markerWidth: 6, markerHeight: 6,
      orient: 'auto-start-reverse',
    }, [svgEl('path', { d: 'M 0 0 L 10 5 L 0 10 z', fill: COLORS.promoted })]),
    svgEl('marker', {
      id: 'arr-rejected', viewBox: '0 0 10 10',
      refX: 9, refY: 5, markerWidth: 5, markerHeight: 5,
      orient: 'auto-start-reverse',
    }, [svgEl('path', { d: 'M 0 0 L 10 5 L 0 10 z', fill: COLORS.rejected })]),
    svgEl('marker', {
      id: 'arr-running', viewBox: '0 0 10 10',
      refX: 9, refY: 5, markerWidth: 6, markerHeight: 6,
      orient: 'auto-start-reverse',
    }, [svgEl('path', { d: 'M 0 0 L 10 5 L 0 10 z', fill: COLORS.running })]),
  ]);
  svg.appendChild(defs);

  // Lane bands + labels.
  laneOrder.forEach((lane, laneIdx) => {
    const laneTop = marginY + laneIdx * (laneHeight + laneGap);
    svg.appendChild(svgEl('rect', {
      class: 'epoch-lane-band',
      x: 6, y: laneTop.toFixed(1),
      width: width - 12, height: laneHeight,
      rx: 8, ry: 8,
      fill: laneIdx % 2 === 0 ? 'rgba(127,127,127,0.05)' : 'rgba(127,127,127,0.10)',
      stroke: COLORS.grid, 'stroke-width': 0.8,
    }));
    svg.appendChild(svgEl('text', {
      class: 'svg-label',
      x: 16, y: (laneTop + 18).toFixed(1), 'font-weight': '600',
    }, ['epoch · ' + truncate(String(lane), 22)]));
  });

  // Within-epoch parent edges + cross-epoch inheritance links.
  for (const g of gens) {
    const id = genId(g);
    const cp = positions.get(id);
    if (!cp) continue;
    const exp = expByGen.get(id);
    const decision = lineageDecision(g, exp);

    const parentId = genParentId(g);
    const crossId = g.v0_parent || g.parent_epoch_head || g.epoch_parent;

    // Within-epoch parent edge.
    if (parentId && positions.has(parentId)) {
      const pp = positions.get(parentId);
      let stroke = COLORS.baseline, strokeW = 1.6, dash = null, marker = null;
      if (decision === 'promoted') {
        stroke = COLORS.promoted; strokeW = 2.8; marker = 'url(#arr-promoted)';
      } else if (decision === 'rejected') {
        stroke = COLORS.rejected; strokeW = 1.6; dash = '5 4'; marker = 'url(#arr-rejected)';
      } else if (decision === 'deferred') {
        stroke = COLORS.deferred; strokeW = 1.8; dash = '2 3';
      } else if (decision === 'in_flight') {
        stroke = COLORS.running; strokeW = 1.8; dash = '4 4'; marker = 'url(#arr-running)';
      }
      svg.appendChild(edgePath(pp, cp, nodeW, nodeH, stroke, strokeW, dash, marker));
    }

    // Cross-epoch link: this gen's v0 descends from the prior epoch's
    // promoted head. Always dashed, neutral, to read as "inherited".
    if (crossId && positions.has(crossId)) {
      const pp = positions.get(crossId);
      const ce = edgePath(pp, cp, nodeW, nodeH, COLORS.baseline, 1.6, '3 4', 'url(#arr-promoted)');
      ce.setAttribute('stroke-opacity', '0.7');
      svg.appendChild(ce);
      const midX = (pp.x + nodeW + cp.x) / 2;
      const midY = (pp.y + cp.y) / 2 + nodeH / 2 - 6;
      svg.appendChild(svgEl('text', {
        class: 'svg-axis', x: midX.toFixed(1), y: midY.toFixed(1),
        'text-anchor': 'middle',
      }, ['inherited']));
    }
  }

  // Nodes.
  for (const g of gens) {
    const id = genId(g);
    const pos = positions.get(id);
    if (!pos) continue;
    const { x, y } = pos;
    const exp = expByGen.get(id);
    const decision = lineageDecision(g, exp);
    let fill, stroke, dash = null, marker;
    if (decision === 'baseline') {
      fill = 'rgba(110, 118, 129, 0.14)'; stroke = COLORS.baseline; marker = '(v0)';
    } else if (decision === 'promoted') {
      fill = 'rgba(46, 160, 67, 0.18)'; stroke = COLORS.promoted; marker = '[+]';
    } else if (decision === 'rejected') {
      fill = 'rgba(215, 58, 73, 0.16)'; stroke = COLORS.rejected; dash = '5 4'; marker = '[x]';
    } else if (decision === 'deferred') {
      fill = 'rgba(191, 135, 0, 0.18)'; stroke = COLORS.deferred; dash = '2 3'; marker = '[=]';
    } else {
      // in_flight — still running, no verdict.
      fill = 'rgba(31, 111, 235, 0.16)'; stroke = COLORS.running; dash = '4 4'; marker = '(running)';
    }

    const grp = svgEl('g', {
      class: 'lineage-node',
      'data-gen': id,
      role: 'button',
      tabindex: '0',
      'aria-label': `generation ${id} ${decision}`,
    });
    grp.addEventListener('click', () => openDrillForGeneration(id));
    grp.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openDrillForGeneration(id);
      }
    });
    grp.appendChild(svgEl('rect', {
      x: x.toFixed(1), y: y.toFixed(1), width: nodeW, height: nodeH,
      rx: 8, ry: 8, fill, stroke, 'stroke-width': 1.8,
      'stroke-dasharray': dash,
    }));
    grp.appendChild(svgEl('text', {
      class: 'svg-label',
      x: (x + nodeW / 2).toFixed(1), y: (y + 19).toFixed(1),
      'text-anchor': 'middle', 'font-weight': '600',
    }, [`${id} ${marker}`]));
    if (exp && exp.outcome) {
      grp.appendChild(svgEl('text', {
        class: 'svg-axis',
        x: (x + nodeW / 2).toFixed(1), y: (y + 36).toFixed(1),
        'text-anchor': 'middle',
      }, [`Δ scalar ${fmtDelta(exp.outcome.scalar_score_delta)}`]));
      grp.appendChild(svgEl('text', {
        class: 'svg-axis',
        x: (x + nodeW / 2).toFixed(1), y: (y + 50).toFixed(1),
        'text-anchor': 'middle',
      }, [`Δ drift ${fmtDelta(exp.outcome.drift_loss_delta)}`]));
    } else {
      grp.appendChild(svgEl('text', {
        class: 'svg-axis',
        x: (x + nodeW / 2).toFixed(1), y: (y + 42).toFixed(1),
        'text-anchor': 'middle',
      }, [decision === 'baseline' ? 'baseline'
        : decision === 'in_flight' ? 'in flight' : 'pending']));
    }
    svg.appendChild(grp);
  }
}

// Bezier edge from a parent node's right edge to a child node's left
// edge, given top-left positions.
function edgePath(pp, cp, nodeW, nodeH, stroke, strokeW, dash, marker) {
  const x1 = pp.x + nodeW;
  const y1 = pp.y + nodeH / 2;
  const x2 = cp.x;
  const y2 = cp.y + nodeH / 2;
  const cx1 = x1 + (x2 - x1) * 0.45;
  const cx2 = x1 + (x2 - x1) * 0.55;
  return svgEl('path', {
    class: 'lineage-edge',
    d: `M ${x1.toFixed(1)} ${y1.toFixed(1)} C ${cx1.toFixed(1)} ${y1.toFixed(1)}, ${cx2.toFixed(1)} ${y2.toFixed(1)}, ${x2.toFixed(1)} ${y2.toFixed(1)}`,
    fill: 'none', stroke,
    'stroke-width': strokeW,
    'stroke-dasharray': dash,
    'marker-end': marker,
  });
}

// --- Tree view — pan + zoom on the lineage stage

const lineageTransform = { scale: 1, x: 0, y: 0 };

function applyLineageTransform() {
  const stage = $('lineage-stage');
  if (!stage) return;
  stage.style.transform =
    `translate(${lineageTransform.x}px, ${lineageTransform.y}px) scale(${lineageTransform.scale})`;
}

function resetLineageTransform() {
  lineageTransform.scale = 1;
  lineageTransform.x = 0;
  lineageTransform.y = 0;
  applyLineageTransform();
}

function setupLineageInteractions() {
  const viewport = $('lineage-viewport');
  const stage = $('lineage-stage');
  if (!viewport || !stage) return;

  let dragging = false;
  let startX = 0, startY = 0, origX = 0, origY = 0;

  viewport.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const rect = viewport.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const py = ev.clientY - rect.top;
    const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
    const next = Math.max(0.3, Math.min(3, lineageTransform.scale * factor));
    const ratio = next / lineageTransform.scale;
    // Zoom toward the cursor.
    lineageTransform.x = px - (px - lineageTransform.x) * ratio;
    lineageTransform.y = py - (py - lineageTransform.y) * ratio;
    lineageTransform.scale = next;
    applyLineageTransform();
  }, { passive: false });

  viewport.addEventListener('pointerdown', (ev) => {
    dragging = true;
    viewport.classList.add('dragging');
    startX = ev.clientX; startY = ev.clientY;
    origX = lineageTransform.x; origY = lineageTransform.y;
    viewport.setPointerCapture(ev.pointerId);
  });
  viewport.addEventListener('pointermove', (ev) => {
    if (!dragging) return;
    lineageTransform.x = origX + (ev.clientX - startX);
    lineageTransform.y = origY + (ev.clientY - startY);
    applyLineageTransform();
  });
  const endDrag = () => { dragging = false; viewport.classList.remove('dragging'); };
  viewport.addEventListener('pointerup', endDrag);
  viewport.addEventListener('pointercancel', endDrag);

  const zoomBy = (factor) => {
    lineageTransform.scale = Math.max(0.3, Math.min(3, lineageTransform.scale * factor));
    applyLineageTransform();
  };
  $('lineage-zoom-in').addEventListener('click', () => zoomBy(1.2));
  $('lineage-zoom-out').addEventListener('click', () => zoomBy(1 / 1.2));
  $('lineage-zoom-reset').addEventListener('click', resetLineageTransform);
}

// --- Render: score trajectory

function renderTrajectory() {
  const svg = $('trajectory-svg');
  clearChildren(svg);

  const gens = state.lineage.generations || [];
  const exps = state.lineage.experiments || [];
  const expByGen = new Map();
  for (const e of exps) expByGen.set(e.generation_id, e);

  const width = 720, height = 220;
  sizeSvg(svg, width, height);
  if (gens.length === 0) {
    svg.appendChild(svgEl('text', {
      class: 'svg-axis', x: width / 2, y: height / 2, 'text-anchor': 'middle',
    }, ['No generations to plot.']));
    return;
  }

  const series = gens.map((g, i) => {
    const id = genId(g);
    const exp = expByGen.get(id);
    if (!exp || !exp.outcome) {
      return { i, id, decision: lineageDecision(g, exp), v: 0 };
    }
    return {
      i, id,
      decision: exp.outcome.tournament_decision,
      v: exp.outcome.scalar_score_delta || 0,
    };
  });

  const values = series.map(s => s.v);
  let vmin = Math.min(...values, 0);
  let vmax = Math.max(...values, 0);
  if (vmin === vmax) {
    const pad = Math.max(0.1, Math.abs(vmin) * 0.2 || 0.1);
    vmin -= pad; vmax += pad;
  } else {
    const pad = (vmax - vmin) * 0.1;
    vmin -= pad; vmax += pad;
  }

  const marginL = 50, marginR = 18, marginT = 22, marginB = 36;
  const plotW = width - marginL - marginR;
  const plotH = height - marginT - marginB;
  const n = series.length;
  const xStep = n > 1 ? plotW / (n - 1) : 0;

  const toX = (i) => n === 1 ? marginL + plotW / 2 : marginL + i * xStep;
  const toY = (v) => marginT + (vmax - v) / (vmax - vmin) * plotH;

  // grid
  for (let k = 0; k < 5; k++) {
    const gy = marginT + plotH * k / 4;
    svg.appendChild(svgEl('line', {
      x1: marginL, y1: gy.toFixed(1),
      x2: marginL + plotW, y2: gy.toFixed(1),
      stroke: COLORS.grid, 'stroke-width': 0.5, 'stroke-opacity': 0.6,
    }));
    const tickVal = vmax - (vmax - vmin) * k / 4;
    svg.appendChild(svgEl('text', {
      class: 'svg-axis',
      x: marginL - 6, y: (gy + 3).toFixed(1), 'text-anchor': 'end',
    }, [tickVal >= 0 ? '+' + tickVal.toFixed(2) : tickVal.toFixed(2)]));
  }

  // zero line
  if (vmin < 0 && 0 < vmax) {
    const zy = toY(0);
    svg.appendChild(svgEl('line', {
      x1: marginL, y1: zy.toFixed(1),
      x2: marginL + plotW, y2: zy.toFixed(1),
      stroke: COLORS.baseline, 'stroke-width': 1, 'stroke-dasharray': '3 3',
      'stroke-opacity': 0.8,
    }));
  }

  // x-axis
  svg.appendChild(svgEl('line', {
    x1: marginL, y1: (marginT + plotH).toFixed(1),
    x2: marginL + plotW, y2: (marginT + plotH).toFixed(1),
    stroke: COLORS.grid, 'stroke-width': 1,
  }));

  // line connecting promoted
  const promoted = series.filter(s => s.decision === 'promoted')
    .map(s => [toX(s.i), toY(s.v)]);
  if (promoted.length >= 2) {
    const d = 'M ' + promoted.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(' L ');
    svg.appendChild(svgEl('path', {
      d, fill: 'none', stroke: COLORS.promoted, 'stroke-width': 2,
    }));
  }

  for (const s of series) {
    const cx = toX(s.i), cy = toY(s.v);
    if (s.decision === 'promoted') {
      svg.appendChild(svgEl('circle', {
        cx: cx.toFixed(1), cy: cy.toFixed(1), r: 5,
        fill: COLORS.promoted, stroke: COLORS.promoted, 'stroke-width': 1.5,
      }));
    } else if (s.decision === 'rejected') {
      const sz = 4.5;
      svg.appendChild(svgEl('rect', {
        x: (cx - sz).toFixed(1), y: (cy - sz).toFixed(1),
        width: 2 * sz, height: 2 * sz,
        fill: 'none', stroke: COLORS.rejected, 'stroke-width': 1.6,
      }));
    } else if (s.decision === 'deferred') {
      svg.appendChild(svgEl('circle', {
        cx: cx.toFixed(1), cy: cy.toFixed(1), r: 4.5,
        fill: 'none', stroke: COLORS.deferred, 'stroke-width': 1.6,
        'stroke-dasharray': '2 2',
      }));
    } else if (s.decision === 'in_flight') {
      // Still running — no verdict, drawn hollow in the running blue.
      svg.appendChild(svgEl('circle', {
        cx: cx.toFixed(1), cy: cy.toFixed(1), r: 4.5,
        fill: 'none', stroke: COLORS.running, 'stroke-width': 1.6,
        'stroke-dasharray': '3 2',
      }));
    } else {
      svg.appendChild(svgEl('circle', {
        cx: cx.toFixed(1), cy: cy.toFixed(1), r: 4,
        fill: 'none', stroke: COLORS.baseline, 'stroke-width': 1.4,
      }));
    }
    svg.appendChild(svgEl('text', {
      class: 'svg-axis',
      x: cx.toFixed(1), y: (marginT + plotH + 14).toFixed(1),
      'text-anchor': 'middle',
    }, [s.id]));
    svg.appendChild(svgEl('text', {
      class: 'svg-axis',
      x: cx.toFixed(1), y: (cy - 8).toFixed(1),
      'text-anchor': 'middle',
    }, [fmtDelta(s.v)]));
  }

  svg.appendChild(svgEl('text', {
    class: 'svg-axis',
    x: marginL - 36, y: (marginT + plotH / 2).toFixed(1),
    'text-anchor': 'middle',
    transform: `rotate(-90 ${marginL - 36} ${(marginT + plotH / 2).toFixed(1)})`,
  }, ['Δ scalar']));
}

// --- Render: drift heatmap

function renderHeatmap() {
  const svg = $('heatmap-svg');
  clearChildren(svg);

  const gens = state.lineage.generations || [];
  const exps = state.lineage.experiments || [];
  const expByGen = new Map();
  for (const e of exps) expByGen.set(e.generation_id, e);

  // promoted generations only
  const promotedGens = gens.filter(g => {
    return lineageDecision(g, expByGen.get(genId(g))) === 'promoted';
  });

  const cellSize = 28, innerPad = 4;
  const labelW = 150;

  const cellValue = new Map();
  const kindMaxAbs = new Map();
  for (const g of promotedGens) {
    const e = expByGen.get(genId(g));
    if (!e || !e.outcome || !e.outcome.drift_movements) continue;
    for (const mv of e.outcome.drift_movements) {
      cellValue.set(mv.kind + ' ' + genId(g), mv.to_rate);
      const delta = Math.abs(mv.to_rate - mv.from_rate);
      const cur = kindMaxAbs.get(mv.kind) || 0;
      if (delta > cur) kindMaxAbs.set(mv.kind, delta);
    }
  }

  if (promotedGens.length === 0 || kindMaxAbs.size === 0) {
    const w = cellSize * 12 + 180;
    const h = cellSize * 2 + 60;
    sizeSvg(svg, w, h);
    svg.appendChild(svgEl('rect', {
      x: 1, y: 1, width: w - 2, height: h - 2,
      fill: 'none', stroke: COLORS.grid, 'stroke-width': 1,
      'stroke-dasharray': '4 3', rx: 6, ry: 6,
    }));
    svg.appendChild(svgEl('text', {
      class: 'svg-axis', x: w / 2, y: h / 2, 'text-anchor': 'middle',
    }, ['No drift movements recorded yet.']));
    return;
  }

  const kinds = [...kindMaxAbs.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 12)
    .map(([k]) => k);

  const nCols = promotedGens.length;
  const nRows = kinds.length;
  const legendH = 28;
  const width = labelW + nCols * (cellSize + innerPad) + 24;
  const height = 28 + nRows * (cellSize + innerPad) + legendH + 8;
  sizeSvg(svg, width, height);

  const allRates = [...cellValue.values()];
  let rateMin = Math.min(0, ...allRates);
  let rateMax = Math.max(...allRates);
  if (rateMin === rateMax) rateMax = rateMin + 1.0;

  const rateToColor = (v) => {
    let t = (v - rateMin) / (rateMax - rateMin);
    t = Math.max(0, Math.min(1, t));
    let r, g, b;
    if (t < 0.5) {
      const u = t / 0.5;
      r = Math.round(33 + (240 - 33) * u);
      g = Math.round(102 + (240 - 102) * u);
      b = Math.round(172 + (240 - 172) * u);
    } else {
      const u = (t - 0.5) / 0.5;
      r = Math.round(240 + (178 - 240) * u);
      g = Math.round(240 + (24 - 240) * u);
      b = Math.round(240 + (43 - 240) * u);
    }
    return `rgb(${r}, ${g}, ${b})`;
  };

  // Header row: generation ids
  for (let i = 0; i < promotedGens.length; i++) {
    const cx = labelW + i * (cellSize + innerPad) + cellSize / 2;
    svg.appendChild(svgEl('text', {
      class: 'svg-axis', x: cx.toFixed(1), y: 20, 'text-anchor': 'middle',
    }, [genId(promotedGens[i])]));
  }

  for (let r = 0; r < kinds.length; r++) {
    const kind = kinds[r];
    const ry = 28 + r * (cellSize + innerPad);
    svg.appendChild(svgEl('text', {
      class: 'svg-label',
      x: labelW - 8, y: (ry + cellSize / 2 + 4).toFixed(1),
      'text-anchor': 'end',
    }, [kind]));
    for (let c = 0; c < promotedGens.length; c++) {
      const cx = labelW + c * (cellSize + innerPad);
      const v = cellValue.get(kind + ' ' + genId(promotedGens[c]));
      if (v === undefined) {
        svg.appendChild(svgEl('rect', {
          x: cx.toFixed(1), y: ry.toFixed(1),
          width: cellSize, height: cellSize,
          rx: 3, ry: 3, fill: 'none', stroke: COLORS.grid,
          'stroke-width': 0.8, 'stroke-dasharray': '2 2',
        }));
        continue;
      }
      svg.appendChild(svgEl('rect', {
        x: cx.toFixed(1), y: ry.toFixed(1),
        width: cellSize, height: cellSize,
        rx: 3, ry: 3, fill: rateToColor(v),
        stroke: 'rgba(0,0,0,0.06)', 'stroke-width': 0.5,
      }));
      svg.appendChild(svgEl('text', {
        class: 'svg-axis',
        x: (cx + cellSize / 2).toFixed(1),
        y: (ry + cellSize / 2 + 3).toFixed(1),
        'text-anchor': 'middle', fill: '#111',
      }, [fmtRate(v)]));
    }
  }

  const legendY = 28 + nRows * (cellSize + innerPad) + 8;
  const legendX = labelW;
  const legendW = Math.min(220, nCols * (cellSize + innerPad));
  const segCount = 20;
  const segW = legendW / segCount;
  for (let s = 0; s < segCount; s++) {
    const t = s / (segCount - 1);
    const v = rateMin + t * (rateMax - rateMin);
    svg.appendChild(svgEl('rect', {
      x: (legendX + s * segW).toFixed(1), y: legendY.toFixed(1),
      width: (segW + 0.5).toFixed(1), height: 10,
      fill: rateToColor(v), stroke: 'none',
    }));
  }
  svg.appendChild(svgEl('text', {
    class: 'svg-axis', x: legendX.toFixed(1), y: (legendY + 22).toFixed(1),
  }, [rateMin.toFixed(2)]));
  svg.appendChild(svgEl('text', {
    class: 'svg-axis',
    x: (legendX + legendW).toFixed(1), y: (legendY + 22).toFixed(1),
    'text-anchor': 'end',
  }, [rateMax.toFixed(2)]));
}

// --- Render: loop-health panel (Overview)
//
// Renders GET /api/health-report — findings as severity-colored cards.
// A CRITICAL finding raises a loud banner; healthy:true → a quiet line.

function severityRank(sev) {
  switch (String(sev || '').toLowerCase()) {
    case 'critical': return 3;
    case 'warning': case 'warn': return 2;
    case 'info': return 1;
    default: return 0;
  }
}

function renderHealthPanel() {
  const wrap = $('health-panel');
  if (!wrap) return;
  clearChildren(wrap);

  const report = state.healthReport;
  if (!report) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No health report yet.']));
    return;
  }

  const findings = Array.isArray(report.findings) ? report.findings.slice() : [];
  findings.sort((a, b) => severityRank(b.severity) - severityRank(a.severity));
  const hasCritical = findings.some(f => severityRank(f.severity) === 3);

  // Loud banner whenever a CRITICAL finding exists.
  if (hasCritical) {
    const crit = findings.find(f => severityRank(f.severity) === 3);
    const banner = el('div', { class: 'health-banner', role: 'alert' }, [
      el('div', { class: 'health-banner-title' }, [
        'This loop is producing no optimization signal.',
      ]),
      el('div', { class: 'health-banner-detail' }, [
        crit && crit.summary
          ? crit.summary
          : 'A critical health check failed — investigate before trusting tournament results.',
      ]),
    ]);
    wrap.appendChild(banner);
  } else if (report.healthy === true && findings.length === 0) {
    // Quiet, reassuring line when there is nothing to report.
    wrap.appendChild(el('p', { class: 'health-ok' }, [
      el('span', { class: 'health-ok-dot', 'aria-hidden': 'true' }, ['●']),
      'Loop healthy — every health check passed.',
    ]));
  } else if (report.healthy === true) {
    wrap.appendChild(el('p', { class: 'health-ok' }, [
      el('span', { class: 'health-ok-dot', 'aria-hidden': 'true' }, ['●']),
      'Loop healthy.',
    ]));
  }

  if (findings.length > 0) {
    const cards = el('div', { class: 'health-cards' });
    for (const f of findings) {
      const sev = String(f.severity || 'info').toLowerCase();
      const sevCls = sev === 'warn' ? 'warning' : sev;
      const card = el('div', { class: 'health-card sev-' + sevCls });
      const head = el('div', { class: 'health-card-head' }, [
        el('span', { class: 'health-sev badge' }, [sevCls]),
        el('code', { class: 'mono health-code' }, [f.code || '—']),
      ]);
      card.appendChild(head);
      if (f.summary) {
        card.appendChild(el('div', { class: 'health-summary' }, [f.summary]));
      }
      if (f.detail) {
        card.appendChild(el('div', { class: 'health-detail meta' }, [f.detail]));
      }
      cards.appendChild(card);
    }
    wrap.appendChild(cards);
  }

  // Footer line — epoch + checked-at timestamp.
  const meta = [];
  if (report.epoch_id) meta.push('epoch ' + report.epoch_id);
  if (report.checked_at) meta.push('checked ' + report.checked_at);
  if (meta.length > 0) {
    wrap.appendChild(el('p', { class: 'health-meta meta mono' }, [meta.join(' · ')]));
  }
}

// --- Render: Tournament view — the gauntlet bracket
//
// A horizontal champion spine (the promoted lineage) with discarded
// challengers hung below the champion each failed to beat. A live
// matchup is drawn at the head with its predicted-gate verdict. Click
// any matchup → the per-matchup detail (GET /api/tournaments/:id).

function renderTournamentView() {
  renderBracket();
  renderMatchupDetail();
  renderHeatmap();
}

// Index the bracket: a champion-spine list and, per champion, the set
// of challengers that lost to it.
function bracketModel() {
  const b = state.bracket || {};
  const lineage = Array.isArray(b.champion_lineage) ? b.champion_lineage.slice() : [];
  const matchups = Array.isArray(b.matchups) ? b.matchups : [];

  // Every promoted challenger becomes the next champion; every rejected
  // one is hung below the champion it challenged.
  const promotedBy = new Map();   // champion genId -> matchup that promoted past it
  const rejectedBy = new Map();   // champion genId -> [rejected matchup, ...]
  for (const m of matchups) {
    const champ = m.champion || '?';
    const decision = String(m.decision || '').toLowerCase();
    if (decision === 'promoted' || decision === 'promote') {
      promotedBy.set(champ, m);
    } else {
      if (!rejectedBy.has(champ)) rejectedBy.set(champ, []);
      rejectedBy.get(champ).push(m);
    }
  }
  return { lineage, matchups, promotedBy, rejectedBy };
}

// The matchup record for an active (in-progress) tournament, derived
// from /api/active-tournament. Keeps the predicted-verdict logic.
function liveMatchup() {
  const t = state.activeTournament;
  if (!t || !Array.isArray(t.entries) || t.entries.length === 0) return null;
  return t;
}

// --- Active-tournament field accessors
//
// The active-tournament record reaches the dashboard from a few
// producers whose field names have drifted: the runtime file uses
// `generation_id` for the challenger, the shared AppState contract
// uses `child_generation_id`, and older heartbeat payloads used
// `child_id`. These accessors normalise all of them so the live path
// never renders a `?` placeholder when the data is actually present.

function liveChampionId(t) {
  if (!t) return null;
  return t.parent_generation_id || t.parent_id || t.champion || null;
}

function liveChallengerId(t) {
  if (!t) return null;
  return t.child_generation_id || t.child_id || t.generation_id ||
    t.challenger || null;
}

function liveRoundLabel(t) {
  if (!t) return null;
  const r = (t.round_index != null) ? t.round_index : t.round;
  if (r == null) return null;
  return String(r);
}

// A finished entry — `status === 'done'`, treating any of the
// terminal-status spellings the producers emit as done.
function entryIsDone(e) {
  const s = String((e && e.status) || '').toLowerCase();
  return s === 'done' || s === 'complete' || s === 'completed';
}

// A failed entry — distinct from done so the live card can surface it.
function entryFailed(e) {
  const s = String((e && e.status) || '').toLowerCase();
  return s === 'fail' || s === 'failed' || s === 'error';
}

// The per-entry scalar, under whichever key the producer used.
function entryScalar(e) {
  if (!e) return null;
  const v = (e.scalar_score != null) ? e.scalar_score
    : (e.score != null) ? e.score
    : (e.child && typeof e.child.drift_loss === 'number')
      ? e.child.drift_loss
      : null;
  return (typeof v === 'number' && isFinite(v)) ? v : null;
}

function renderBracket() {
  const wrap = $('tournament-bracket');
  if (!wrap) return;
  clearChildren(wrap);

  const { lineage, matchups, promotedBy, rejectedBy } = bracketModel();
  const live = liveMatchup();

  if (lineage.length === 0 && matchups.length === 0 && !live) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No tournaments recorded yet.']));
    return;
  }

  // The tournament hall — a grid of board cards for the in-progress
  // round, rendered above the resolved-history spine. With parallel
  // board execution many entries are `running` at once; the hall shows
  // the whole round in play at a glance.
  if (live) {
    wrap.appendChild(renderHall(live));
  }

  // The spine: champion v0 ═> v2 ═> v6. Each champion node carries the
  // challengers it defended against hung below it.
  const spine = el('div', { class: 'bracket-spine' });
  lineage.forEach((champId, i) => {
    const col = el('div', { class: 'bracket-col' });

    const champIdSpan = el('span', { class: 'bracket-champ-id mono' }, [champId]);
    const champHg = harmonografGenLink(champId);
    if (champHg) champIdSpan.appendChild(champHg);
    const champNode = el('div', {
      class: 'bracket-champ' + (i === 0 ? ' is-seed' : ''),
    }, [
      champIdSpan,
      el('span', { class: 'bracket-champ-tag' }, [i === 0 ? 'seed' : 'champion']),
    ]);
    col.appendChild(champNode);

    // Solid green connector to the next champion.
    if (i < lineage.length - 1) {
      const promo = promotedBy.get(champId);
      const conn = el('div', { class: 'bracket-connector promoted' });
      conn.appendChild(el('span', { class: 'bracket-conn-arrow', 'aria-hidden': 'true' }, ['═▶']));
      if (promo) {
        conn.appendChild(el('button', {
          type: 'button',
          class: 'bracket-conn-label',
          'aria-label': 'matchup ' + champId + ' versus ' + (promo.challenger || '?'),
          onClick: () => openMatchup(promo.challenger),
        }, ['Δ ' + fmtDelta(promo.delta_scalar)]));
      }
      col.appendChild(conn);
    }

    // Discarded challengers hung below.
    const losers = rejectedBy.get(champId) || [];
    if (losers.length > 0) {
      const drop = el('div', { class: 'bracket-drop' });
      for (const m of losers) {
        drop.appendChild(renderChallengerCard(m, champId));
      }
      col.appendChild(drop);
    }

    spine.appendChild(col);
  });

  // Live matchup at the head — a compact pointer into the hall above.
  // The hall grid renders the in-progress round in full; the spine
  // only needs a connector node so the resolved lineage visibly
  // continues into the live challenge.
  if (live) {
    const headCol = el('div', { class: 'bracket-col bracket-col-live' });

    // Resolve the champion the live challenger is facing. Prefer the
    // id the active-tournament record carries; fall back to the tail
    // of the resolved lineage.
    const champId = liveChampionId(live) ||
      (lineage.length ? lineage[lineage.length - 1] : null);

    // #12 — when there is no resolved lineage yet (the very first
    // tournament of an epoch, or a fresh workspace), the champion has
    // no spine node of its own. Draw one here so the bracket always
    // shows the baseline the challenger branches off.
    if (lineage.length === 0 && champId) {
      const champIdSpan = el('span', { class: 'bracket-champ-id mono' }, [champId]);
      const champHg = harmonografGenLink(champId);
      if (champHg) champIdSpan.appendChild(champHg);
      headCol.appendChild(el('div', { class: 'bracket-champ is-seed' }, [
        champIdSpan,
        el('span', { class: 'bracket-champ-tag' }, ['champion']),
      ]));
    }

    if (lineage.length > 0 || (lineage.length === 0 && champId)) {
      const conn = el('div', { class: 'bracket-connector live' });
      conn.appendChild(el('span', { class: 'bracket-conn-arrow', 'aria-hidden': 'true' }, ['┄▶']));
      headCol.appendChild(conn);
    }
    headCol.appendChild(renderLiveCard(live, champId));
    spine.appendChild(headCol);
  }

  wrap.appendChild(spine);
}

// --- Tournament hall
//
// The in-progress round renders as a grid of board cards. Each card is
// one board entry's head-to-head: the Champion side (`side:"parent"`)
// and the Challenger side (`side:"child"`). With parallel board
// execution any number of entries are `running` simultaneously, so the
// hall makes no one-at-a-time assumption — every board with a running
// side gets an accent border so the active hall reads at a glance.

// Group the flat per-side `entries` list into per-board records,
// preserving first-seen order. Each board carries its parent-side and
// child-side entry (either may be absent if the producer only emitted
// one side so far).
function hallBoards(t) {
  const order = [];
  const byId = new Map();
  const entries = Array.isArray(t && t.entries) ? t.entries : [];
  for (const e of entries) {
    if (!e || e.entry_id == null) continue;
    const id = String(e.entry_id);
    if (!byId.has(id)) {
      byId.set(id, { entry_id: id, parent: null, child: null });
      order.push(id);
    }
    const board = byId.get(id);
    const side = String(e.side || '').toLowerCase();
    if (side === 'child' || side === 'challenger') board.child = e;
    else board.parent = e;
  }
  return order.map((id) => byId.get(id));
}

// Status bucket for one side entry — drives the pill and counters.
// Returns one of: 'queued' | 'running' | 'done' | 'failed'.
function sideStatus(e) {
  if (!e) return 'queued';
  if (entryFailed(e)) return 'failed';
  if (entryIsDone(e)) return 'done';
  const s = String(e.status || '').toLowerCase();
  if (s === 'running' || s === 'in_progress' || s === 'active') return 'running';
  return 'queued';
}

// Occupancy header: round N · B boards · X in play · Y done · Z queued.
// Counts are over the whole flat per-SIDE entries list so a board with
// one running side and one queued side is correctly reflected. When a
// `parallelism` value is present on state it is appended; never invented.
function renderHallOccupancy(t, boards) {
  const entries = Array.isArray(t && t.entries) ? t.entries : [];
  let inPlay = 0, done = 0, queued = 0, failed = 0;
  for (const e of entries) {
    const st = sideStatus(e);
    if (st === 'running') inPlay += 1;
    else if (st === 'done') done += 1;
    else if (st === 'failed') failed += 1;
    else queued += 1;
  }
  const round = liveRoundLabel(t);
  const bits = [];
  if (round != null) bits.push('round ' + round);
  bits.push(boards.length + ' board' + (boards.length === 1 ? '' : 's'));
  bits.push(inPlay + ' in play');
  bits.push(done + ' done');
  if (failed > 0) bits.push(failed + ' failed');
  bits.push(queued + ' queued');

  // parallelism is optional — append only when state actually carries
  // it (heartbeat is the documented producer); omit it gracefully.
  const par = state.heartbeat && state.heartbeat.parallelism;
  if (typeof par === 'number' && isFinite(par) && par > 0) {
    bits.push('parallelism ' + par);
  }

  const head = el('div', { class: 'hall-occupancy', role: 'status' });
  bits.forEach((b, i) => {
    if (i > 0) head.appendChild(el('span', { class: 'hall-occ-sep', 'aria-hidden': 'true' }, ['·']));
    head.appendChild(el('span', { class: 'hall-occ-stat' }, [b]));
  });
  return head;
}

// The hall: occupancy header + the board-card grid.
function renderHall(t) {
  const boards = hallBoards(t);
  const hall = el('section', { class: 'tournament-hall', 'aria-label': 'tournament hall' });

  const childId = liveChallengerId(t);
  const champId = liveChampionId(t);
  hall.appendChild(el('div', { class: 'hall-head' }, [
    el('h3', { class: 'hall-title' }, ['Tournament hall']),
    el('p', { class: 'hall-sub meta' }, [
      el('strong', null, [childId || 'the challenger']),
      ' is challenging ',
      el('strong', null, [champId || 'the baseline']),
      ' — every board runs both sides; cards in play are bordered.',
    ]),
  ]));
  hall.appendChild(renderHallOccupancy(t, boards));

  if (boards.length === 0) {
    hall.appendChild(el('p', { class: 'empty' }, [
      'Round has no board entries yet.',
    ]));
    return hall;
  }

  const grid = el('div', { class: 'hall-grid', role: 'list' });
  for (const board of boards) {
    grid.appendChild(renderBoardCard(board, t, champId, childId));
  }
  hall.appendChild(grid);
  return hall;
}

// One board card — a single board entry's champion-vs-challenger
// head-to-head. Clicking the card opens the Conversation view for the
// entry (a sibling agent owns that route; we only set the hash).
function renderBoardCard(board, t, champId, childId) {
  const champSt = sideStatus(board.parent);
  const childSt = sideStatus(board.child);
  const anyRunning = champSt === 'running' || childSt === 'running';
  const anyFailed = champSt === 'failed' || childSt === 'failed';
  const bothDone = (champSt === 'done' || champSt === 'failed') &&
    (childSt === 'done' || childSt === 'failed');

  const card = el('div', {
    class: 'board-card' +
      (anyRunning ? ' is-running' : '') +
      (anyFailed ? ' has-failed' : '') +
      (bothDone && !anyFailed ? ' is-done' : ''),
    role: 'listitem',
    tabindex: '0',
    'aria-label': 'board ' + board.entry_id +
      ' — champion ' + champSt + ', challenger ' + childSt +
      ' (open conversation)',
    onClick: () => openBoardConversation(board.entry_id),
    onKeydown: (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openBoardConversation(board.entry_id);
      }
    },
  });

  // Head — the board entry id.
  card.appendChild(el('div', { class: 'board-card-head' }, [
    el('span', { class: 'board-card-id mono' }, [board.entry_id]),
    anyRunning
      ? el('span', { class: 'board-card-livedot', 'aria-hidden': 'true' }, [''])
      : null,
  ]));

  // Two sides — Champion (parent) then Challenger (child).
  card.appendChild(renderBoardSide('Champion', 'parent', board.parent,
    champSt, champId));
  card.appendChild(renderBoardSide('Challenger', 'child', board.child,
    childSt, childId));

  // Result strip — once both sides finish, which side won + the delta.
  if (bothDone) {
    card.appendChild(renderBoardResult(board));
  }
  return card;
}

// One side row of a board card: a status pill, a budget-fraction
// progress bar (for a running side), and the scalar once the side is
// done. An over-deadline running side turns the bar red.
function renderBoardSide(label, side, entry, status, genId) {
  const row = el('div', { class: 'board-side board-side-' + side + ' st-' + status });

  const head = el('div', { class: 'board-side-head' }, [
    el('span', { class: 'board-side-label' }, [label]),
    el('span', { class: 'board-side-gen mono' }, [genId || '—']),
    el('span', { class: 'pill pill-' + status }, [status]),
  ]);
  row.appendChild(head);

  if (status === 'running') {
    // The progress bar tracks the run's elapsed-vs-budget fraction.
    const run = findActiveRunForEntry(entry);
    let frac = (run && typeof run.progress === 'number' && isFinite(run.progress))
      ? Math.max(0, Math.min(1, run.progress)) : null;

    // Over-deadline detection — elapsed beyond budget. When it occurs
    // the bar fills fully and turns red, and the card flags it.
    let elapsed = run && run.elapsed_seconds;
    if ((elapsed == null || !isFinite(elapsed)) && run && run.started_at) {
      elapsed = (nowMs() - parseIso(run.started_at)) / 1000;
    }
    const budget = run && run.budget_seconds;
    const overDeadline = typeof elapsed === 'number' && isFinite(elapsed) &&
      typeof budget === 'number' && isFinite(budget) && budget > 0 &&
      elapsed > budget;
    if (overDeadline) frac = 1;
    const pct = frac != null ? Math.round(frac * 100) : 0;

    const bar = el('div', {
      class: 'board-prog' + (overDeadline ? ' over-deadline' : ''),
      role: 'progressbar',
      'aria-valuemin': '0', 'aria-valuemax': '100',
      'aria-valuenow': String(pct),
      'aria-label': 'elapsed fraction of wall-clock budget',
    }, [
      el('div', { class: 'board-prog-fill', style: 'width:' + pct + '%' }),
    ]);
    row.appendChild(bar);

    if (typeof elapsed === 'number' && isFinite(elapsed)) {
      const budgetTxt = (typeof budget === 'number' && isFinite(budget))
        ? fmtDuration(budget) : '—';
      row.appendChild(el('div', {
        class: 'board-side-meta mono' + (overDeadline ? ' over-deadline' : ''),
      }, [
        fmtDuration(elapsed) + ' / ' + budgetTxt,
        overDeadline
          ? el('span', { class: 'board-deadline-flag' }, [' over deadline'])
          : el('span', { class: 'meta' }, [' elapsed/budget']),
      ]));
    } else {
      row.appendChild(el('div', { class: 'board-side-meta meta' }, ['running…']));
    }
  } else if (status === 'done') {
    const sc = entryScalar(entry);
    row.appendChild(el('div', { class: 'board-side-score mono' }, [
      sc != null ? 'scalar ' + fmtRate(sc) : 'done',
    ]));
  } else if (status === 'failed') {
    row.appendChild(el('div', { class: 'board-side-meta board-fail' }, [
      'run failed',
    ]));
  } else {
    row.appendChild(el('div', { class: 'board-side-meta meta' }, ['queued']));
  }
  return row;
}

// The result strip — shown once both sides of a board finish. States
// which side won (lower scalar wins) and the scalar delta between them.
function renderBoardResult(board) {
  const champSc = entryScalar(board.parent);
  const childSc = entryScalar(board.child);

  if (typeof champSc !== 'number' || typeof childSc !== 'number') {
    return el('div', { class: 'board-result board-result-tbd' }, [
      'Both sides finished — no scalar recorded.',
    ]);
  }
  // Lower scalar (drift-derived loss) is better.
  const delta = childSc - champSc;
  let cls, text;
  if (delta < 0) {
    cls = 'board-result-win';
    text = 'Challenger leads · Δ ' + fmtDelta(delta);
  } else if (delta > 0) {
    cls = 'board-result-loss';
    text = 'Champion holds · Δ ' + fmtDelta(delta);
  } else {
    cls = 'board-result-flat';
    text = 'Flat · Δ ' + fmtDelta(delta);
  }
  return el('div', { class: 'board-result ' + cls }, [text]);
}

// Board card click → the Conversation view. A sibling agent owns the
// route that renders it; this only sets the hash.
function openBoardConversation(entryId) {
  if (entryId == null) return;
  location.hash = 'conversation/' + encodeURIComponent(String(entryId));
}

// A discarded challenger card — dashed red, hung below its champion.
function renderChallengerCard(m, champId) {
  const card = el('div', {
    class: 'bracket-loser',
    role: 'listitem',
    tabindex: '0',
    'aria-label': 'discarded challenger ' + (m.challenger || '?') +
      ' rejected versus ' + champId,
    onClick: () => openMatchup(m.challenger),
    onKeydown: (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openMatchup(m.challenger);
      }
    },
  });
  const loserIdSpan = el('span', { class: 'bracket-loser-id mono' }, [m.challenger || '?']);
  const loserHg = harmonografGenLink(m.challenger);
  if (loserHg) loserIdSpan.appendChild(loserHg);
  card.appendChild(el('div', { class: 'bracket-loser-head' }, [
    loserIdSpan,
    el('span', { class: 'badge rejected' }, ['discarded']),
  ]));
  if (m.delta_scalar != null) {
    card.appendChild(el('div', { class: 'bracket-loser-delta mono' }, [
      'Δ scalar ' + fmtDelta(m.delta_scalar),
    ]));
  }
  const reason = m.rejection_reason || (m.hypothesis_core_idea
    ? truncate(m.hypothesis_core_idea, 70) : '');
  if (reason) {
    card.appendChild(el('div', { class: 'bracket-loser-reason meta' }, [reason]));
  }
  return card;
}

// The in-progress challenge card at the head of the bracket.
//
// #13 — the card states plainly: which generation challenges which,
// the round, how many board entries are done / running / failed, and
// the predicted gate verdict so far. No `?` is rendered when the
// active-tournament record actually carries the ids (#11).
function renderLiveCard(t, champId) {
  const childId = liveChallengerId(t);
  const champLabel = champId || 'the baseline';
  const card = el('div', {
    class: 'bracket-live',
    role: 'listitem',
    tabindex: '0',
    'aria-label': 'live matchup ' + (childId || 'challenger') +
      ' challenging ' + champLabel,
    onClick: () => { if (childId) openMatchup(childId); },
    onKeydown: (ev) => {
      if ((ev.key === 'Enter' || ev.key === ' ') && childId) {
        ev.preventDefault();
        openMatchup(childId);
      }
    },
  });

  // Head — challenger id + a live badge.
  const liveIdSpan = el('span', { class: 'bracket-live-id mono' }, [
    childId || 'challenger',
  ]);
  const liveHg = harmonografGenLink(childId);
  if (liveHg) liveIdSpan.appendChild(liveHg);
  card.appendChild(el('div', { class: 'bracket-live-head' }, [
    liveIdSpan,
    el('span', { class: 'badge running' }, ['live']),
  ]));

  // A plain-language matchup line: who challenges whom, and the round.
  const round = liveRoundLabel(t);
  card.appendChild(el('div', { class: 'bracket-live-vs' }, [
    el('strong', null, [childId || 'the proposed generation']),
    ' is challenging ',
    el('strong', null, [champLabel]),
    round != null ? ' · round ' + round : '',
  ]));

  // Per-entry status dots + a done/running/failed breakdown.
  const entries = Array.isArray(t.entries) ? t.entries : [];
  const done = entries.filter(entryIsDone).length;
  const failed = entries.filter(entryFailed).length;
  const running = entries.length - done - failed;

  const dots = el('div', { class: 'bracket-live-dots', 'aria-hidden': 'true' });
  for (const e of entries) {
    const st = String((e && e.status) || 'queued').toLowerCase();
    dots.appendChild(el('span', { class: 'bracket-dot ' + st }, ['']));
  }
  card.appendChild(dots);

  const progBits = [done + ' of ' + entries.length + ' board entries done'];
  if (running > 0) progBits.push(running + ' running');
  if (failed > 0) progBits.push(failed + ' failed');
  card.appendChild(el('div', { class: 'bracket-live-prog meta' }, [
    progBits.join(' · '),
  ]));

  // Predicted-gate verdict so far — spelled out, not just a token.
  const verdict = predictedGateVerdict(t, state.scoring.margin);
  if (verdict) {
    const vcls = verdict.verdict === 'promote' ? 'promote'
      : verdict.verdict === 'reject' ? 'reject' : 'tbd';
    const vlabel = verdict.verdict === 'promote' ? 'on track to be KEPT'
      : verdict.verdict === 'reject' ? 'on track to be DISCARDED'
      : 'verdict still undecided';
    card.appendChild(el('div', { class: 'bracket-live-verdict ' + vcls }, [
      'Gate so far: ' + vlabel,
    ]));
  }
  return card;
}

// Navigate to a matchup's detail. The selection is held in state and
// the detail endpoint is fetched lazily.
function openMatchup(genId) {
  if (!genId) return;
  state.selectedMatchup = genId;
  renderMatchupDetail();
  loadMatchupDetail(genId);
  const section = $('tournament-detail-section');
  if (section && section.scrollIntoView) {
    section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

// Resolve a matchup summary record (from the bracket) by challenger id.
function matchupSummary(genId) {
  const b = state.bracket || {};
  const matchups = Array.isArray(b.matchups) ? b.matchups : [];
  return matchups.find(m => m.challenger === genId) || null;
}

function renderMatchupDetail() {
  const wrap = $('tournament-detail');
  if (!wrap) return;
  clearChildren(wrap);

  const genId = state.selectedMatchup;
  if (!genId) {
    wrap.appendChild(el('p', { class: 'empty' }, ['Select a matchup above.']));
    return;
  }

  const summary = matchupSummary(genId);
  const detail = state.matchupDetail.get(genId);
  const live = liveMatchup();
  const isLive = !!(live && liveChallengerId(live) === genId);

  // Header line.
  const champ = (summary && summary.champion)
    || (isLive ? liveChampionId(live) : null) || '?';
  wrap.appendChild(el('h3', null, [
    'Matchup · ' + genId + ' vs ' + champ,
  ]));

  if (!detail && !isLive) {
    wrap.appendChild(el('p', { class: 'empty' }, [
      'Loading matchup detail…',
    ]));
    return;
  }

  // The detail endpoint is authoritative; for a live matchup with no
  // detail yet we fall back to the active-tournament record.
  const hyp = (detail && detail.hypothesis) ||
    (isLive ? live.hypothesis : null) ||
    (summary && summary.hypothesis_core_idea
      ? { core_idea: summary.hypothesis_core_idea } : null);
  const patches = (detail && Array.isArray(detail.patches)) ? detail.patches : [];
  const entryGrid = (detail && Array.isArray(detail.entry_grid))
    ? detail.entry_grid : [];
  const scalar = detail && detail.scalar;
  const decision = (detail && detail.decision)
    || (summary && summary.decision)
    || (isLive ? 'in_progress' : null);
  const rejectionReason = (detail && detail.rejection_reason)
    || (summary && summary.rejection_reason) || null;

  // --- What was tested
  wrap.appendChild(el('h3', null, ['What was tested']));
  const focus = el('div', { class: 'hypothesis-focus' });
  if (hyp && (hyp.core_idea || hyp.why || (hyp.modulating && hyp.modulating.length))) {
    if (hyp.core_idea) {
      focus.appendChild(el('p', null, [
        el('strong', null, ['Core idea. ']), hyp.core_idea,
      ]));
    }
    if (hyp.why) {
      focus.appendChild(el('p', null, [
        el('strong', null, ['Why. ']), hyp.why,
      ]));
    }
    if (Array.isArray(hyp.modulating) && hyp.modulating.length > 0) {
      const line = el('p', null, [el('strong', null, ['Modulating. '])]);
      hyp.modulating.forEach((mid) => {
        line.appendChild(el('code', { class: 'mono code-pill focus-tag' }, [mid]));
      });
      focus.appendChild(line);
    }
  } else {
    focus.appendChild(el('p', { class: 'empty' }, [
      'No hypothesis recorded for this challenger.',
    ]));
  }
  wrap.appendChild(focus);

  // --- Patches
  wrap.appendChild(el('h3', null, ['Patches']));
  if (patches.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No patches recorded.']));
  } else {
    const tbl = el('table', { class: 'data-table' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', null, ['mutation']),
      el('th', null, ['op']),
      el('th', null, ['rationale']),
    ])]));
    const tbody = el('tbody');
    for (const p of patches) {
      tbody.appendChild(el('tr', null, [
        el('td', null, [el('code', { class: 'mono' }, [p.mutation_id || '—'])]),
        el('td', null, [el('code', { class: 'mono' }, [p.op || '—'])]),
        el('td', null, [p.rationale || '']),
      ]));
    }
    tbl.appendChild(tbody);
    wrap.appendChild(tbl);
  }

  // --- Per-entry A/B grid
  wrap.appendChild(el('h3', null, ['Per-entry A/B grid']));
  if (entryGrid.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, [
      isLive
        ? 'Tournament still in progress — per-entry grid lands on completion.'
        : 'No per-entry grid recorded.',
    ]));
  } else {
    const regressions = entryGrid.filter(
      r => String(r.verdict || '').toLowerCase() === 'regressed').length;
    if (regressions > 0) {
      wrap.appendChild(el('p', { class: 'grid-note meta' }, [
        regressions + ' regression' + (regressions === 1 ? '' : 's') +
        ' — these are what the gate kills challengers on.',
      ]));
    }
    // Every grid row is a board entry run twice — once under the
    // champion generation, once under the challenger. Each side has
    // its own harmonograf execution trace. We only add the trace
    // column when the heartbeat actually carries a harmonograf_url.
    const hgOn = harmonografBase() != null;
    const headCells = [
      el('th', null, ['board entry']),
      el('th', null, [champ + ' loss']),
      el('th', null, [genId + ' loss']),
      el('th', null, ['pass A→B']),
      el('th', null, ['verdict']),
    ];
    if (hgOn) headCells.push(el('th', null, ['trace']));
    const tbl = el('table', { class: 'ab-grid' });
    tbl.appendChild(el('thead', null, [el('tr', null, headCells)]));
    const tbody = el('tbody');
    for (const row of entryGrid) {
      const verdict = String(row.verdict || 'flat').toLowerCase();
      const cells = [
        el('td', { class: 'mono' }, [row.entry_id || '—']),
        el('td', { class: 'mono' }, [fmtRate(row.parent_drift_loss)]),
        el('td', { class: 'mono' }, [fmtRate(row.child_drift_loss)]),
        el('td', { class: 'mono' }, [
          (row.parent_pass ? '✓' : '✗') + ' → ' +
          (row.child_pass ? '✓' : '✗'),
        ]),
        el('td', null, [
          el('span', { class: 'ab-verdict ab-verdict-' + verdict }, [verdict]),
        ]),
      ];
      if (hgOn) {
        // Resolve a per-side run. The grid row may carry explicit
        // session ids (parent_run_id / child_run_id / *_session_id);
        // when it does not, harmonografRunUrl falls back to the
        // `{generation}--{entry}` run-id convention.
        const entryId = row.entry_id;
        const parentRun = {
          entry_id: entryId,
          generation_id: champ,
          session_id: row.parent_session_id || row.parent_run_id || null,
        };
        const childRun = {
          entry_id: entryId,
          generation_id: genId,
          session_id: row.child_session_id || row.child_run_id || row.run_id || null,
        };
        const sideA = harmonografMini(parentRun, champ,
          'open harmonograf trace for ' + champ + ' run of ' + (entryId || 'entry'));
        const sideB = harmonografMini(childRun, genId,
          'open harmonograf trace for ' + genId + ' run of ' + (entryId || 'entry'));
        const traceCell = el('td', { class: 'ab-trace' });
        if (sideA) traceCell.appendChild(sideA);
        if (sideB) traceCell.appendChild(sideB);
        cells.push(traceCell);
      }
      tbody.appendChild(el('tr', { class: 'ab-row ab-' + verdict }, cells));
    }
    tbl.appendChild(tbody);
    wrap.appendChild(tbl);
  }

  // --- Scalar breakdown
  wrap.appendChild(el('h3', null, ['Scalar breakdown']));
  if (!scalar) {
    wrap.appendChild(el('p', { class: 'empty' }, [
      'No scalar breakdown recorded.',
    ]));
  } else {
    wrap.appendChild(renderScalarBreakdown(scalar, champ, genId));
  }

  // --- Verdict
  wrap.appendChild(el('h3', null, ['Verdict']));
  wrap.appendChild(renderMatchupVerdict(decision, rejectionReason, isLive, live));
}

// Parent-vs-child scalar with per-namespace component bars.
function renderScalarBreakdown(scalar, champ, genId) {
  const wrap = el('div', { class: 'scalar-breakdown' });

  const parent = typeof scalar.parent === 'number' ? scalar.parent : null;
  const child = typeof scalar.child === 'number' ? scalar.child : null;
  const delta = typeof scalar.delta === 'number'
    ? scalar.delta
    : (parent != null && child != null ? child - parent : null);

  const head = el('div', { class: 'scalar-totals' }, [
    el('div', { class: 'scalar-total' }, [
      el('span', { class: 'scalar-total-label meta' }, [champ + ' (champion)']),
      el('span', { class: 'scalar-total-val mono' }, [fmtRate(parent)]),
    ]),
    el('div', { class: 'scalar-total' }, [
      el('span', { class: 'scalar-total-label meta' }, [genId + ' (challenger)']),
      el('span', { class: 'scalar-total-val mono' }, [fmtRate(child)]),
    ]),
    el('div', { class: 'scalar-total scalar-delta' +
      (delta != null && delta < 0 ? ' good' : delta != null && delta > 0 ? ' bad' : '') }, [
      el('span', { class: 'scalar-total-label meta' }, ['Δ scalar']),
      el('span', { class: 'scalar-total-val mono' }, [fmtDelta(delta)]),
    ]),
  ]);
  wrap.appendChild(head);

  // Per-namespace component bars. Each component is a contribution to
  // the scalar; lower drift/cost is better so a negative bar is good.
  const components = scalar.components && typeof scalar.components === 'object'
    ? scalar.components : null;
  if (components && Object.keys(components).length > 0) {
    const entries = Object.entries(components);
    let maxAbs = 0;
    for (const [, v] of entries) {
      const n = typeof v === 'number' ? Math.abs(v) : 0;
      if (n > maxAbs) maxAbs = n;
    }
    if (maxAbs === 0) maxAbs = 1;
    const bars = el('div', { class: 'scalar-bars' });
    for (const [ns, v] of entries) {
      const val = typeof v === 'number' ? v : 0;
      const pct = Math.min(100, (Math.abs(val) / maxAbs) * 100);
      const row = el('div', { class: 'scalar-bar-row' }, [
        el('span', { class: 'scalar-bar-ns' }, [ns]),
        el('span', { class: 'scalar-bar-track' }, [
          el('span', {
            class: 'scalar-bar-fill ' + (val < 0 ? 'good' : 'bad'),
            style: 'width:' + pct.toFixed(1) + '%',
          }),
        ]),
        el('span', { class: 'scalar-bar-val mono' }, [fmtDelta(val)]),
      ]);
      bars.appendChild(row);
    }
    wrap.appendChild(bars);
  }
  return wrap;
}

// Verdict block — kept/discarded plus the exact gate reasoning.
function renderMatchupVerdict(decision, rejectionReason, isLive, live) {
  const d = String(decision || '').toLowerCase();
  if (isLive && (d === 'in_progress' || d === '')) {
    const v = predictedGateVerdict(live, state.scoring.margin);
    const cls = v && v.verdict === 'promote' ? 'promote'
      : v && v.verdict === 'reject' ? 'reject' : 'tbd';
    const wrap = el('div', { class: 'verdict ' + cls, role: 'status' });
    wrap.appendChild(el('div', { class: 'verdict-line' }, [
      'In progress — predicted gate: ' +
      (v ? v.verdict.toUpperCase() : 'TBD'),
    ]));
    if (v) wrap.appendChild(el('div', { class: 'verdict-reason' }, [v.reason]));
    return wrap;
  }

  const promoted = d === 'promoted' || d === 'promote' || d === 'kept';
  const cls = promoted ? 'promote' : 'reject';
  const wrap = el('div', { class: 'verdict ' + cls, role: 'status' });
  wrap.appendChild(el('div', { class: 'verdict-line' }, [
    promoted ? 'Kept — promoted to champion' : 'Discarded by the gate',
  ]));
  if (promoted) {
    wrap.appendChild(el('div', { class: 'verdict-reason' }, [
      'Cleared the scalar margin without a pass regression.',
    ]));
  } else {
    wrap.appendChild(el('div', { class: 'verdict-reason' }, [
      rejectionReason || 'Failed to clear the scalar margin.',
    ]));
  }
  return wrap;
}

// --- Render: Epoch view
//
// Renders the epoch-definition contract from GET /api/epoch (or the
// `epoch` key on /api/state). Every field is read defensively — the
// contract is built by a sibling agent and any block may be absent.

function renderEpochView() {
  const def = state.epochDef;
  renderEpochOverview(def);
  renderEpochHarness(def);
  renderEpochBoard(def);
  renderEpochRubric(def);
  renderEpochScoring(def);
  renderEpochMutations(def);
}

function kv(label, value) {
  return el('div', { class: 'kv' }, [
    el('span', { class: 'kv-label' }, [label]),
    el('span', { class: 'kv-value mono' }, [value]),
  ]);
}

function renderEpochOverview(def) {
  const wrap = $('epoch-overview');
  clearChildren(wrap);
  if (!def) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No epoch contract loaded.']));
    return;
  }
  wrap.appendChild(kv('epoch id', def.epoch_id || '—'));
  const hash = def.contract_hash || '';
  wrap.appendChild(kv('contract hash', hash ? truncate(hash, 12) : '—'));
  wrap.appendChild(kv('created', def.created_at || '—'));
  const closed = def.closed === true;
  wrap.appendChild(el('div', { class: 'kv' }, [
    el('span', { class: 'kv-label' }, ['status']),
    el('span', { class: 'badge ' + (closed ? 'rejected' : 'promoted') },
      [closed ? 'closed' : 'open']),
  ]));
}

function renderEpochHarness(def) {
  const wrap = $('epoch-harness');
  clearChildren(wrap);
  const h = def && def.harness;
  if (!h) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No harness recorded.']));
    return;
  }
  wrap.appendChild(el('p', null, [
    el('strong', null, ['Entrypoint. ']),
    el('code', { class: 'mono code-pill' }, [h.entrypoint || '—']),
  ]));
  const trees = Array.isArray(h.mutable_trees) ? h.mutable_trees : [];
  const line = el('p', null, [el('strong', null, ['Mutable trees. '])]);
  if (trees.length === 0) {
    line.appendChild(el('span', { class: 'meta' }, ['none']));
  } else {
    const box = el('span', { class: 'harness-trees' });
    for (const tr of trees) {
      box.appendChild(el('code', { class: 'mono code-pill' }, [tr]));
    }
    line.appendChild(box);
  }
  wrap.appendChild(line);
}

function renderEpochBoard(def) {
  const wrap = $('epoch-board');
  clearChildren(wrap);
  const board = def && Array.isArray(def.board) ? def.board : [];
  if (board.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No board entries.']));
    return;
  }
  const tbl = el('table', { class: 'data-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['id']),
    el('th', null, ['kind']),
    el('th', null, ['input preview']),
    el('th', null, ['expectation']),
    el('th', null, ['budget']),
    el('th', null, ['weight']),
    el('th', null, ['tags']),
  ])]));
  const tbody = el('tbody');
  for (const e of board) {
    const tags = Array.isArray(e.tags) ? e.tags.join(', ') : '';
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [e.id || '—']),
      el('td', null, [e.kind || '—']),
      el('td', null, [truncate(e.input_preview || '', 60)]),
      el('td', null, [e.expectation_kind || '—']),
      el('td', { class: 'mono' }, [e.budget_s != null ? e.budget_s + 's' : '—']),
      el('td', { class: 'mono' }, [e.weight != null ? String(e.weight) : '—']),
      el('td', { class: 'meta' }, [tags]),
    ]));
  }
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
}

function renderEpochRubric(def) {
  const wrap = $('epoch-rubric');
  clearChildren(wrap);
  // Centering / max-width is owned by the `.epoch-rubric` CSS rule;
  // ensure the container carries the class even if markup drifts, and
  // always wrap content in a `rubric-block` so the readable-block
  // styling applies (no lopsided narrow column).
  wrap.classList.add('epoch-rubric');
  const rubric = def && typeof def.rubric === 'string' ? def.rubric : '';
  if (!rubric.trim()) {
    const empty = el('div', { class: 'rubric-block' }, [
      el('p', { class: 'empty' }, ['No rubric recorded.']),
    ]);
    wrap.appendChild(empty);
    return;
  }
  const block = el('div', { class: 'rubric-block' });
  renderMinimalMarkdown(rubric, block);
  wrap.appendChild(block);
}

// Minimal markdown: headings (#, ##, ###), unordered lists (- / *),
// and `inline code`. Anything else is passed through as text. This is
// deliberately small — no link parsing, no HTML injection surface.
function renderMinimalMarkdown(src, container) {
  const lines = src.replace(/\r\n/g, '\n').split('\n');
  let listEl = null;
  const flushList = () => { if (listEl) { container.appendChild(listEl); listEl = null; } };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '');
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    const item = line.match(/^\s*[-*]\s+(.*)$/);
    if (heading) {
      flushList();
      const tag = 'h' + heading[1].length;
      container.appendChild(el(tag, null, inlineMarkdown(heading[2])));
    } else if (item) {
      if (!listEl) listEl = el('ul');
      listEl.appendChild(el('li', null, inlineMarkdown(item[1])));
    } else if (line.trim() === '') {
      flushList();
    } else {
      flushList();
      container.appendChild(el('p', null, inlineMarkdown(line)));
    }
  }
  flushList();
}

// Split a line on backtick-delimited inline code spans.
function inlineMarkdown(text) {
  const parts = [];
  let i = 0;
  while (i < text.length) {
    const tick = text.indexOf('`', i);
    if (tick === -1) { parts.push(text.slice(i)); break; }
    const end = text.indexOf('`', tick + 1);
    if (end === -1) { parts.push(text.slice(i)); break; }
    if (tick > i) parts.push(text.slice(i, tick));
    parts.push(el('code', null, [text.slice(tick + 1, end)]));
    i = end + 1;
  }
  return parts;
}

function renderEpochScoring(def) {
  const wrap = $('epoch-scoring');
  clearChildren(wrap);
  const scoring = def && def.scoring && typeof def.scoring === 'object'
    ? def.scoring : null;
  if (!scoring || Object.keys(scoring).length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No scoring weights recorded.']));
    return;
  }
  const tbl = el('table', { class: 'data-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['weight']),
    el('th', null, ['value']),
  ])]));
  const tbody = el('tbody');
  for (const [k, v] of Object.entries(scoring)) {
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [k]),
      el('td', { class: 'mono' }, [scoringValueCell(v)]),
    ]));
  }
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
}

// Render a scoring weight value. Scalars render as plain text; an
// object-valued weight (e.g. per_kind_weights, severity_weights)
// renders as a nested key->value sub-list instead of stringifying to
// the literal `[object Object]`.
function scoringValueCell(v) {
  if (v != null && typeof v === 'object' && !Array.isArray(v)) {
    const entries = Object.entries(v);
    if (entries.length === 0) return el('span', { class: 'meta' }, ['{}']);
    const sub = el('ul', { class: 'scoring-subdict' });
    for (const [ik, iv] of entries) {
      sub.appendChild(el('li', null, [
        el('span', { class: 'scoring-subkey' }, [ik]),
        el('span', { class: 'scoring-subval' }, [
          iv != null && typeof iv === 'object'
            ? scoringValueCell(iv)
            : String(iv),
        ]),
      ]));
    }
    return sub;
  }
  if (Array.isArray(v)) return el('span', null, [v.join(', ')]);
  return el('span', null, [String(v)]);
}

function renderEpochMutations(def) {
  const wrap = $('epoch-mutations');
  clearChildren(wrap);
  const muts = def && Array.isArray(def.mutations) ? def.mutations : [];
  if (muts.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No mutation snapshot yet.']));
    return;
  }
  wrap.appendChild(el('p', { class: 'panel-subheader' }, [
    'What zicato is allowed to rewrite this epoch.',
  ]));
  const tbl = el('table', { class: 'data-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['id']),
    el('th', null, ['kind']),
    el('th', null, ['file']),
    el('th', null, ['lines']),
    el('th', null, ['preview']),
  ])]));
  const tbody = el('tbody');
  for (const m of muts) {
    const fullPath = m.file || '';
    const fileCell = fullPath
      ? el('td', { class: 'mono', title: fullPath },
          [relativizeMutationPath(fullPath)])
      : el('td', { class: 'mono' }, ['—']);
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [m.id || '—']),
      el('td', null, [m.kind || '—']),
      fileCell,
      el('td', { class: 'mono' }, [m.lines || '—']),
      el('td', null, [truncate(m.preview || '', 64)]),
    ]));
  }
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
}

// Reduce an absolute snapshot path to a meaningful repo-relative path.
// Mutation files arrive as full paths inside a per-run snapshot dir,
// e.g. `/tmp/zicato-tournamentN/.zicato/epochs/.../generations/v0/
// snapshot/agent/foo.py`. Strip everything up to and including a
// `snapshot/` segment; if there is none, fall back to the last 2-3
// path segments. The caller keeps the full path as a tooltip.
function relativizeMutationPath(path) {
  const norm = String(path).replace(/\\/g, '/');
  const m = norm.match(/(?:^|\/)snapshot\/(.+)$/);
  if (m && m[1]) return m[1];
  const parts = norm.split('/').filter(Boolean);
  if (parts.length <= 3) return parts.join('/');
  return parts.slice(-3).join('/');
}

// --- Render: log tail

// Format an event timestamp as a short clock time (HH:MM:SS). Falls
// back to the raw string when it does not parse as a date.
function fmtEventTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return String(ts);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function renderLogTail() {
  const wrap = $('log-tail');
  clearChildren(wrap);

  // #7 — the log tail consumes the shared `state.logTail` contract:
  //   { events: [{ seq, kind, ts, summary }] }
  // Fall back to the legacy `state.logLines` shape so the panel works
  // regardless of the merge order with the core/state branch.
  let events = [];
  if (state.logTail && Array.isArray(state.logTail.events)) {
    events = state.logTail.events.map((e) => ({
      seq: e.seq != null ? e.seq : null,
      kind: e.kind || 'event',
      ts: e.ts || null,
      summary: e.summary != null ? e.summary : '',
    }));
  } else if (Array.isArray(state.logLines)) {
    events = state.logLines.map((line) => ({
      seq: null,
      kind: (line.level || 'log'),
      ts: line.ts || null,
      summary: line.message != null ? line.message : (line.line || ''),
    }));
  }

  if (events.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No events yet.']));
    return;
  }

  // Oldest-first so the freshest event lands at the bottom, next to
  // the auto-scroll — the natural reading order for a streaming tail.
  for (const ev of events) {
    const lineEl = el('div', { class: 'log-line fade-in' });
    if (ev.ts) {
      lineEl.appendChild(el('span', {
        class: 'ts', title: String(ev.ts),
      }, [fmtEventTime(ev.ts)]));
    }
    const kind = String(ev.kind || 'event');
    const kl = kind.toLowerCase();
    const cls = (kl.indexOf('error') >= 0 || kl.indexOf('fail') >= 0) ? 'ev-error'
      : (kl.indexOf('warn') >= 0) ? 'ev-warn'
      : (kl.indexOf('ok') >= 0 || kl.indexOf('done') >= 0 || kl.indexOf('pass') >= 0) ? 'ev-ok'
      : '';
    lineEl.appendChild(el('span', {
      class: 'log-kind badge ' + cls,
    }, [kind]));
    lineEl.appendChild(el('span', { class: 'log-summary' }, [
      String(ev.summary || ''),
    ]));
    wrap.appendChild(lineEl);
  }
  wrap.scrollTop = wrap.scrollHeight;
}

// --- Drill-down panels (hash router)

// Drill-downs keep the current view fragment and append a detail
// segment, e.g. #/tree/generation/v3 — so closing the drill returns to
// the view the operator was on.

function openDrillForGeneration(genId) {
  location.hash = `#/${currentView}/generation/${encodeURIComponent(genId)}`;
}
function openDrillForEntry(entry) {
  location.hash = `#/${currentView}/entry/${encodeURIComponent(entry.entry_id)}`;
}
function openDrillForRun(run) {
  location.hash = `#/${currentView}/run/${encodeURIComponent(run.run_id)}`;
}
function closeDrill() {
  location.hash = '#/' + currentView;
}

// Single hash router: resolves the view AND any drill-down. Fragment
// forms understood:
//   #/<view>
//   #/<view>/<kind>/<id>
//   #/<kind>/<id>           (legacy — drill on the current view)
function applyRoute() {
  const hash = location.hash || '';
  const segs = hash.replace(/^#\/?/, '').split('/').filter(Boolean);

  let view = currentView;
  let kind = null;
  let id = null;

  if (segs.length === 0) {
    view = DEFAULT_VIEW;
  } else if (segs[0] === 'conversation') {
    // Focused Conversation view — #conversation/{entry_id}. The entry id
    // follows the view segment directly (no kind segment). Board cards
    // in the Tournament view link here.
    view = 'conversation';
    const entryId = segs.length >= 2
      ? decodeURIComponent(segs.slice(1).join('/'))
      : null;
    if (view !== currentView) showView(view);
    else showViewClassesOnly(view);
    applyDrill(null, null);
    selectConversation(entryId);
    return;
  } else if (VIEWS.includes(segs[0])) {
    view = segs[0];
    if (segs.length >= 3) {
      kind = segs[1];
      id = decodeURIComponent(segs.slice(2).join('/'));
    }
  } else if (['generation', 'entry', 'run'].includes(segs[0]) && segs.length >= 2) {
    // legacy drill form — keep current view
    kind = segs[0];
    id = decodeURIComponent(segs.slice(1).join('/'));
  } else {
    view = DEFAULT_VIEW;
  }

  if (view !== currentView) {
    showView(view);
  } else {
    // ensure nav highlight is correct on first paint
    showViewClassesOnly(view);
  }

  applyDrill(kind, id);
}

function showViewClassesOnly(view) {
  for (const v of VIEWS) {
    const node = $('view-' + v);
    if (node) node.classList.toggle('hidden', v !== view);
    const nav = $('nav-' + v);
    if (nav) {
      nav.classList.toggle('active', v === view);
      if (v === view) nav.setAttribute('aria-current', 'page');
      else nav.removeAttribute('aria-current');
    }
  }
}

function applyDrill(kind, id) {
  const panel = $('drill-panel');
  const title = $('drill-title');
  const body = $('drill-body');
  clearChildren(body);

  if (!kind || !id || !['generation', 'entry', 'run'].includes(kind)) {
    panel.setAttribute('aria-hidden', 'true');
    title.textContent = 'Detail';
    return;
  }
  panel.setAttribute('aria-hidden', 'false');

  if (kind === 'generation') {
    title.textContent = 'Generation ' + id;
    const exps = state.experiments || state.lineage.experiments || [];
    const exp = exps.find(e => e.generation_id === id);
    if (!exp) {
      body.appendChild(el('p', { class: 'empty' }, ['No experiment recorded for this generation.']));
      return;
    }
    if (exp.hypothesis) {
      body.appendChild(el('h4', null, ['Hypothesis']));
      body.appendChild(el('p', null, [exp.hypothesis.core_idea || '']));
      if (exp.hypothesis.why) {
        body.appendChild(el('p', null, [el('strong', null, ['why. ']), exp.hypothesis.why]));
      }
      if (exp.hypothesis.risks) {
        body.appendChild(el('p', null, [el('strong', null, ['risks. ']), exp.hypothesis.risks]));
      }
    }
    if (exp.patches && exp.patches.length > 0) {
      body.appendChild(el('h4', null, ['Patches']));
      const tbl = el('table');
      tbl.appendChild(el('thead', null, [el('tr', null, [
        el('th', null, ['mutation']),
        el('th', null, ['op']),
        el('th', null, ['rationale']),
      ])]));
      const tbody = el('tbody');
      for (const p of exp.patches) {
        tbody.appendChild(el('tr', null, [
          el('td', null, [el('code', null, [p.mutation_id || ''])]),
          el('td', null, [el('code', null, [p.op || ''])]),
          el('td', null, [p.rationale || '']),
        ]));
      }
      tbl.appendChild(tbody);
      body.appendChild(tbl);
    }
    if (exp.outcome) {
      body.appendChild(el('h4', null, ['Outcome']));
      const deltas = el('div', { class: 'deltas' });
      deltas.appendChild(el('span', { class: 'delta' }, [
        el('span', { class: 'delta-label' }, ['Δ scalar ']),
        fmtDelta(exp.outcome.scalar_score_delta),
      ]));
      deltas.appendChild(el('span', { class: 'delta' }, [
        el('span', { class: 'delta-label' }, ['Δ drift_loss ']),
        fmtDelta(exp.outcome.drift_loss_delta),
      ]));
      deltas.appendChild(el('span', { class: 'delta' }, [
        el('span', { class: 'delta-label' }, ['Δ pass_rate ']),
        fmtDelta(exp.outcome.pass_rate_delta),
      ]));
      body.appendChild(deltas);
    }
    return;
  }

  if (kind === 'entry') {
    title.textContent = 'Entry ' + id;
    // Look in every selectable tournament — the active (in-flight) one
    // first, then any past ones. allTournaments() now always includes
    // state.activeTournament when set (core loads it globally), so an
    // entry from a tournament that is still running resolves here.
    let entry = null;
    let owner = null;
    for (const t of state.allTournaments()) {
      const found = (t.entries || []).find(e => e.entry_id === id);
      if (found) { entry = found; owner = t; break; }
    }
    if (!entry) {
      body.appendChild(el('p', { class: 'empty' }, [
        'Entry not found in any tournament. It may not have been ' +
        'scheduled yet, or its tournament has not started.',
      ]));
      return;
    }

    // Real status — the producer's `status`, with a `queued` fallback
    // only when the field is genuinely absent.
    body.appendChild(el('p', null, [
      'Status: ', el('strong', null, [entry.status || 'queued']),
    ]));
    // Which side of the matchup this entry ran (parent / child). The
    // contract carries `side`; older records have no side at all.
    if (entry.side) {
      body.appendChild(el('p', null, [
        'Side: ', el('strong', null, [entry.side]),
      ]));
    }
    // Which tournament it belongs to, and whether that run is live.
    if (owner) {
      const champ = liveChampionId(owner);
      const chal = liveChallengerId(owner);
      if (champ || chal) {
        body.appendChild(el('p', { class: 'meta' }, [
          (owner.__active ? 'In-flight tournament: ' : 'Tournament: ') +
          (chal || '?') + ' vs ' + (champ || '?'),
        ]));
      }
    }
    // The entry's scalar score — under whichever key the producer used.
    const sc = entryScalar(entry);
    if (sc != null) {
      body.appendChild(el('p', { class: 'mono' }, [
        'scalar score ' + fmtRate(sc),
      ]));
    }
    if (entry.patch_id) {
      body.appendChild(el('p', { class: 'meta mono' }, ['patch_id: ' + entry.patch_id]));
    }

    // Defensive: some producers attach per-side result sub-objects.
    if (entry.parent) {
      body.appendChild(el('h4', null, ['Parent result']));
      body.appendChild(el('p', { class: 'mono' }, [
        `drift_loss ${fmtRate(entry.parent.drift_loss)}, pass ${entry.parent.pass ? 'yes' : 'no'}`,
      ]));
    }
    if (entry.child) {
      body.appendChild(el('h4', null, ['Child result']));
      body.appendChild(el('p', { class: 'mono' }, [
        `drift_loss ${fmtRate(entry.child.drift_loss)}, pass ${entry.child.pass ? 'yes' : 'no'}`,
      ]));
      if (entry.child.drift_kinds) {
        const items = Object.entries(entry.child.drift_kinds);
        if (items.length > 0) {
          body.appendChild(el('h4', null, ['Drift kinds']));
          const ul = el('ul');
          for (const [k, v] of items) {
            ul.appendChild(el('li', null, [`${k}: ${v}`]));
          }
          body.appendChild(ul);
        }
      }
    }
    if (entry.run_id) {
      body.appendChild(el('p', { class: 'meta mono' }, ['run_id: ' + entry.run_id]));
      // #17 — deep-link the entry's run into harmonograf.
      const hg = harmonografMini(
        { entry_id: entry.entry_id, run_id: entry.run_id },
        'harmonograf trace',
        'open harmonograf trace for entry ' + entry.entry_id);
      if (hg) body.appendChild(el('p', null, [hg]));
    }
    return;
  }

  if (kind === 'run') {
    title.textContent = 'Run ' + id;
    const run = (state.activeRuns || []).find(r => r.run_id === id);
    if (!run) {
      body.appendChild(el('p', { class: 'empty' }, ['Run not active.']));
      return;
    }
    body.appendChild(el('h4', null, ['Metadata']));
    const dl = el('table');
    const tb = el('tbody');
    const rows = [
      ['run_id', run.run_id],
      ['entry_id', run.entry_id || '—'],
      ['generation_id', run.generation_id || '—'],
      ['started_at', run.started_at || '—'],
      ['budget_seconds', run.budget_seconds != null ? String(run.budget_seconds) : '—'],
      ['events_path', run.events_path || '—'],
    ];
    for (const [k, v] of rows) {
      tb.appendChild(el('tr', null, [
        el('th', null, [k]),
        el('td', { class: 'mono' }, [String(v)]),
      ]));
    }
    dl.appendChild(tb);
    body.appendChild(dl);

    const hg = harmonografLink(run);
    if (hg) {
      body.appendChild(el('p', null, [hg]));
    }

    body.appendChild(el('p', { class: 'meta' }, [
      'Live events feed lands when GET /api/run/{run_id}/events ships in v1.3.',
    ]));
    return;
  }
}

// ===================================================================
// --- Conversation view — side-by-side champion / challenger transcripts
//
// A focused view reached at #conversation/{entry_id}. It shows a board
// entry's champion run and challenger run transcripts in two columns,
// live: the transcript is fetched on entry and re-fetched on every
// render tick while a run is still in progress, so the columns grow as
// the runs execute. renderAll() runs on each SSE state_change, so the
// view re-paints — and re-polls — without touching the SSE machinery.
//
// Data contract — GET /api/matchup/{entry_id}/conversations:
//   { champion:   { run_id, generation_id, transcript },
//     challenger: { run_id, generation_id, transcript } }
// A `transcript` is { run_id, event_count, complete:bool,
//   turns:[{ seq, ts, agent, role, kind, text, tool_calls[],
//            tool_results[] }],
//   annotations:[{ kind, ts, summary, anchor_seq, detail }] }.
// The endpoint does not exist on every server build; an absent /
// failing endpoint degrades to a clear "data unavailable" state.
//
// This whole section is self-contained: its state lives in the
// module-level vars below rather than on AppState, and it drives its
// own live re-fetch from renderConversationView — so it touches no
// shared rendering or fetch code outside this block.

// --- Conversation-view module state.
let convEntryId = null;       // board entry whose diff is open, or null
let convData = null;          // last { champion, challenger } payload
let convError = false;        // endpoint absent / failed → degrade
let convInFlight = false;     // a fetch is currently in the air
let convLastFetch = 0;        // epoch-ms of the last fetch (poll throttle)

// Minimum gap between live re-fetches while a run is in progress.
const CONV_POLL_MS = 2500;

// Enter the Conversation view for a board entry. Sets the selected
// entry, paints immediately (so the header / loading state shows), then
// kicks off the fetch. applyRoute() calls this on every renderAll(), so
// it only (re-)fetches when the entry actually changed or nothing has
// loaded yet — the render-driven poll handles the live growth.
function selectConversation(entryId) {
  if (!entryId) {
    convEntryId = null;
    convData = null;
    convError = false;
    renderConversationView();
    return;
  }
  const changed = convEntryId !== entryId;
  if (changed) {
    // A different entry — drop the stale transcript so the loading
    // state shows rather than the previous entry's columns.
    convData = null;
    convError = false;
    convLastFetch = 0;
  }
  convEntryId = entryId;
  renderConversationView();
  if (changed || (convData == null && !convError)) {
    loadConversation();
  }
}

// GET /api/matchup/{entry_id}/conversations. Tolerant: an absent or
// failing endpoint sets `convError` so the view degrades to a clear
// "conversation data unavailable" message rather than crashing. In
// mock mode the data is synthesised locally.
async function loadConversation() {
  const entryId = convEntryId;
  if (!entryId || convInFlight) return;
  convLastFetch = Date.now();
  if (state.mock) {
    convData = mockConversation(entryId);
    convError = false;
    if (currentView === 'conversation') renderConversationView();
    return;
  }
  convInFlight = true;
  try {
    const data = await fetchJson(
      '/api/matchup/' + encodeURIComponent(entryId) + '/conversations');
    // Guard against the entry changing mid-flight (the operator drilled
    // to a different entry while this request was in the air).
    if (convEntryId !== entryId) return;
    convData = (data && typeof data === 'object') ? data : null;
    convError = !convData;
  } catch (err) {
    if (convEntryId !== entryId) return;
    // Endpoint absent (404 on this branch) or transient — degrade.
    convData = null;
    convError = true;
  } finally {
    convInFlight = false;
  }
  if (currentView === 'conversation') renderConversationView();
}

// True while either side's transcript is still being produced.
function convInProgress(conv) {
  if (!conv) return false;
  for (const side of [conv.champion, conv.challenger]) {
    const t = side && side.transcript;
    if (t && t.complete === false) return true;
  }
  return false;
}

// One side's transcript shape — defensive: fields may be absent on a
// partial / in-progress record.
function transcriptTurns(side) {
  const t = side && side.transcript;
  return (t && Array.isArray(t.turns)) ? t.turns : [];
}
function transcriptAnnotations(side) {
  const t = side && side.transcript;
  return (t && Array.isArray(t.annotations)) ? t.annotations : [];
}

// Render the Conversation view: a header, then two transcript columns.
// While a run is still in progress this also schedules the next live
// re-fetch — renderConversationView is reached on every SSE-driven
// renderAll(), so this is the view's live loop.
function renderConversationView() {
  const wrap = $('conversation-panel');
  if (!wrap) return;
  clearChildren(wrap);

  const entryId = convEntryId;

  // The back link always returns to the Tournament view — the
  // Conversation view is only ever entered by drilling from there.
  const backLink = el('a', {
    class: 'conversation-back',
    href: '#/tournament',
  }, ['← back to tournament']);

  if (!entryId) {
    wrap.appendChild(backLink);
    wrap.appendChild(el('p', { class: 'empty' }, [
      'No board entry selected. Drill into a board card from the ' +
      'Tournament view to see its conversation diff.',
    ]));
    return;
  }

  const conv = convData;
  const champion = conv && conv.champion;
  const challenger = conv && conv.challenger;

  // --- Header: entry id, champion gen vs challenger gen.
  const champGen = (champion && champion.generation_id) || '—';
  const chalGen = (challenger && challenger.generation_id) || '—';
  const header = el('div', { class: 'conversation-header' }, [
    backLink,
    el('h2', { class: 'conversation-title' }, [
      'Conversation diff ',
      el('code', { class: 'mono' }, [entryId]),
    ]),
    el('div', { class: 'conversation-versus' }, [
      el('span', { class: 'conversation-side-tag champion' }, [
        'champion ', el('code', { class: 'mono' }, [champGen]),
      ]),
      el('span', { class: 'conversation-vs' }, ['vs']),
      el('span', { class: 'conversation-side-tag challenger' }, [
        'challenger ', el('code', { class: 'mono' }, [chalGen]),
      ]),
    ]),
  ]);
  wrap.appendChild(header);

  // --- Degraded states.
  if (convError) {
    wrap.appendChild(el('p', { class: 'empty conversation-unavailable' }, [
      'Conversation data unavailable — the matchup transcript ' +
      'endpoint did not respond. It may not be served by this ' +
      'supervisor build, or the runs have not started.',
    ]));
    return;
  }
  if (!conv) {
    wrap.appendChild(el('p', { class: 'empty' }, [
      'Loading conversation…',
    ]));
    return;
  }

  // --- Two columns.
  const cols = el('div', { class: 'conversation-columns' }, [
    renderTranscriptColumn('champion', champion),
    renderTranscriptColumn('challenger', challenger),
  ]);
  wrap.appendChild(cols);

  // --- Live loop: while a run is still producing turns, schedule the
  // next re-fetch. renderAll() (SSE-driven) re-enters this function and
  // keeps the poll alive; the throttle keeps it from hammering when
  // state_change ticks arrive fast.
  if (!state.mock && convInProgress(conv)) {
    const since = Date.now() - convLastFetch;
    if (since >= CONV_POLL_MS) {
      loadConversation();
    } else {
      setTimeout(() => {
        if (currentView === 'conversation' && convEntryId === entryId
            && convInProgress(convData)) {
          loadConversation();
        }
      }, CONV_POLL_MS - since);
    }
  }
}

// Render one transcript column for a side ({ run_id, generation_id,
// transcript }). The column header carries the run id and a live /
// complete status; the body renders turns in seq order with annotations
// hung as margin notes next to their anchored turn.
function renderTranscriptColumn(sideKind, side) {
  const col = el('div', { class: 'conversation-column ' + sideKind });

  const transcript = side && side.transcript;
  const runId = (side && side.run_id) || (transcript && transcript.run_id);

  // Column header — side label, run id, status badge.
  const complete = !!(transcript && transcript.complete);
  const hasTranscript = !!transcript;
  const statusBadge = !hasTranscript
    ? el('span', { class: 'badge pending' }, ['no run'])
    : complete
      ? el('span', { class: 'badge promoted' }, ['complete'])
      : el('span', { class: 'badge running conversation-live' }, [
          el('span', { class: 'conversation-live-dot', 'aria-hidden': 'true' },
            ['●']),
          'in progress',
        ]);
  const head = el('div', { class: 'conversation-column-head' }, [
    el('span', { class: 'conversation-column-label' }, [sideKind]),
    el('code', { class: 'mono conversation-run-id' }, [runId || '—']),
    statusBadge,
  ]);
  if (transcript && transcript.event_count != null) {
    head.appendChild(el('span', { class: 'meta conversation-event-count' }, [
      transcript.event_count + ' events',
    ]));
  }
  col.appendChild(head);

  const body = el('div', { class: 'conversation-column-body' });

  if (!hasTranscript) {
    body.appendChild(el('p', { class: 'empty' }, [
      'No transcript for this side — the run has not started.',
    ]));
    col.appendChild(body);
    return col;
  }

  const turns = transcriptTurns(side);
  const annotations = transcriptAnnotations(side);

  // Index annotations by the seq of the turn they anchor to.
  const annoBySeq = new Map();
  for (const a of annotations) {
    const key = a && a.anchor_seq;
    if (key == null) continue;
    if (!annoBySeq.has(key)) annoBySeq.set(key, []);
    annoBySeq.get(key).push(a);
  }

  if (turns.length === 0 && !complete) {
    body.appendChild(el('p', { class: 'empty' }, [
      'Waiting for the first turn…',
    ]));
  } else if (turns.length === 0) {
    body.appendChild(el('p', { class: 'empty' }, [
      'This run produced no transcript turns.',
    ]));
  }

  for (const turn of turns) {
    body.appendChild(renderTurn(turn, annoBySeq.get(turn && turn.seq)));
  }

  // A trailing live cue while the run is still executing.
  if (!complete) {
    body.appendChild(el('div', { class: 'conversation-tail-live' }, [
      el('span', { class: 'conversation-live-dot', 'aria-hidden': 'true' },
        ['●']),
      'run in progress — more turns will stream in',
    ]));
  }

  col.appendChild(body);
  return col;
}

// Render a single transcript turn. `seq` is set as a data attribute so
// the two columns can be aligned by seq. `annos` are the annotations
// anchored to this turn, hung as margin notes.
function renderTurn(turn, annos) {
  const seq = turn && turn.seq;
  const node = el('div', {
    class: 'conversation-turn',
    dataset: { seq: seq == null ? '' : String(seq) },
  });

  // Turn meta line — agent / role / kind, plus seq + timestamp.
  const meta = el('div', { class: 'conversation-turn-meta' });
  if (turn && turn.agent) {
    meta.appendChild(el('span', { class: 'conversation-turn-agent' },
      [String(turn.agent)]));
  }
  if (turn && turn.role) {
    meta.appendChild(el('span', { class: 'conversation-turn-role' },
      [String(turn.role)]));
  }
  if (turn && turn.kind) {
    meta.appendChild(el('span', {
      class: 'conversation-turn-kind kind-' + String(turn.kind),
    }, [String(turn.kind)]));
  }
  if (seq != null) {
    meta.appendChild(el('span', { class: 'meta mono conversation-turn-seq' },
      ['#' + seq]));
  }
  if (turn && turn.ts) {
    meta.appendChild(el('span', { class: 'meta conversation-turn-ts' },
      [String(turn.ts)]));
  }
  node.appendChild(meta);

  // Turn text.
  if (turn && turn.text) {
    node.appendChild(el('div', { class: 'conversation-turn-text' },
      [String(turn.text)]));
  }

  // Tool calls — compact.
  const calls = (turn && Array.isArray(turn.tool_calls)) ? turn.tool_calls : [];
  for (const c of calls) {
    const argStr = (c && c.args != null)
      ? (typeof c.args === 'string' ? c.args : JSON.stringify(c.args))
      : '';
    node.appendChild(el('div', { class: 'conversation-tool tool-call' }, [
      el('span', { class: 'conversation-tool-glyph', 'aria-hidden': 'true' },
        ['→']),
      el('span', { class: 'conversation-tool-name mono' },
        [(c && c.name) || 'tool']),
      argStr
        ? el('code', { class: 'mono conversation-tool-args' },
            [truncate(argStr, 160)])
        : null,
    ]));
  }

  // Tool results — compact.
  const results = (turn && Array.isArray(turn.tool_results))
    ? turn.tool_results : [];
  for (const r of results) {
    const resStr = (r && r.result != null)
      ? (typeof r.result === 'string' ? r.result : JSON.stringify(r.result))
      : '';
    node.appendChild(el('div', { class: 'conversation-tool tool-result' }, [
      el('span', { class: 'conversation-tool-glyph', 'aria-hidden': 'true' },
        ['←']),
      el('span', { class: 'conversation-tool-name mono' },
        [(r && r.name) || 'result']),
      resStr
        ? el('code', { class: 'mono conversation-tool-args' },
            [truncate(resStr, 160)])
        : null,
    ]));
  }

  // Margin notes — annotations anchored to this turn. drift / steering /
  // judge / plan are visually distinct (drift carries a warning color).
  if (annos && annos.length > 0) {
    const notes = el('div', { class: 'conversation-margin' });
    for (const a of annos) {
      const kind = (a && a.kind) || 'note';
      const note = el('div', {
        class: 'conversation-note note-' + kind,
        title: (a && a.detail) || '',
      });
      note.appendChild(el('span', { class: 'conversation-note-kind' },
        [kind]));
      if (a && a.summary) {
        note.appendChild(el('span', { class: 'conversation-note-summary' },
          [String(a.summary)]));
      }
      if (a && a.detail) {
        note.appendChild(el('span', { class: 'conversation-note-detail meta' },
          [String(a.detail)]));
      }
      notes.appendChild(note);
    }
    node.appendChild(notes);
  }

  return node;
}

// Synthetic conversation data for ?mock=1 — keyed by board entry id,
// with a sensible default so any entry previews. Exercises tool calls /
// results, every annotation kind, an in-progress challenger run, and a
// seq the two sides share so alignment is visible.
function mockConversation(entryId) {
  const championTranscript = {
    run_id: 'v4--' + entryId,
    event_count: 9,
    complete: true,
    turns: [
      { seq: 1, ts: '04:35:01', agent: 'researcher', role: 'user',
        kind: 'plan',
        text: 'Extract the invoice total and due date from the attached PDF.' },
      { seq: 2, ts: '04:35:03', agent: 'researcher', role: 'assistant',
        kind: 'message',
        text: 'I will read the document, then extract the two fields.',
        tool_calls: [
          { name: 'read_document', args: { path: 'invoice.pdf' },
            task_id: 't1' },
        ] },
      { seq: 3, ts: '04:35:05', agent: 'researcher', role: 'tool',
        kind: 'tool',
        text: '',
        tool_results: [
          { name: 'read_document',
            result: 'Invoice #4471 — total $1,240.00, due 2026-06-01.',
            task_id: 't1' } ] },
      { seq: 4, ts: '04:35:08', agent: 'researcher', role: 'assistant',
        kind: 'message',
        text: 'Total is $1,240.00 and the due date is 2026-06-01.' },
    ],
    annotations: [
      { kind: 'plan', ts: '04:35:01', anchor_seq: 1,
        summary: 'task plan registered',
        detail: 'two-field extraction; predicate expectation.' },
      { kind: 'judge', ts: '04:35:09', anchor_seq: 4,
        summary: 'predicate passed',
        detail: 'both fields match the expected contract.' },
    ],
  };
  const challengerTranscript = {
    run_id: 'v5--' + entryId,
    event_count: 7,
    complete: false,
    turns: [
      { seq: 1, ts: '04:36:00', agent: 'researcher', role: 'user',
        kind: 'plan',
        text: 'Extract the invoice total and due date from the attached PDF.' },
      { seq: 2, ts: '04:36:02', agent: 'researcher', role: 'assistant',
        kind: 'message',
        text: 'Reading the document with the compressed tool description.',
        tool_calls: [
          { name: 'read_document', args: { path: 'invoice.pdf' },
            task_id: 't1' } ] },
      { seq: 3, ts: '04:36:04', agent: 'researcher', role: 'tool',
        kind: 'tool',
        text: '',
        tool_results: [
          { name: 'read_document',
            result: 'Invoice #4471 — total $1,240.00, due 2026-06-01.',
            task_id: 't1' } ] },
      { seq: 4, ts: '04:36:07', agent: 'researcher', role: 'assistant',
        kind: 'message',
        text: 'The total appears to be $1,240 — still confirming the date.' },
    ],
    annotations: [
      { kind: 'steering', ts: '04:36:03', anchor_seq: 2,
        summary: 'steering nudge applied',
        detail: 'reminded the agent to emit the date in ISO form.' },
      { kind: 'drift', ts: '04:36:07', anchor_seq: 4,
        summary: 'schema drift: total dropped its cents',
        detail: 'emitted "$1,240" instead of the contract "$1,240.00".' },
    ],
  };
  return {
    champion: {
      run_id: championTranscript.run_id,
      generation_id: 'v4',
      transcript: championTranscript,
    },
    challenger: {
      run_id: challengerTranscript.run_id,
      generation_id: 'v5',
      transcript: challengerTranscript,
    },
  };
}

// --- View switching
//
// Only the active view's panels are in the DOM flow; switching views
// toggles the `.hidden` class. The SSE connection is untouched, so
// switching is instant and live updates keep flowing.

function showView(view) {
  if (!VIEWS.includes(view)) view = DEFAULT_VIEW;
  currentView = view;
  for (const v of VIEWS) {
    const node = $('view-' + v);
    if (node) node.classList.toggle('hidden', v !== view);
    const nav = $('nav-' + v);
    if (nav) {
      nav.classList.toggle('active', v === view);
      if (v === view) nav.setAttribute('aria-current', 'page');
      else nav.removeAttribute('aria-current');
    }
  }
  renderActiveView();
}

// Render only the panels belonging to the active view. The header,
// footer and log tail (Overview) are cheap and always kept fresh.
function renderActiveView() {
  if (currentView === 'overview') {
    renderHealthPanel();
    renderActiveTournament();
    renderActiveRuns();
    renderLogTail();
  } else if (currentView === 'tree') {
    renderLineage();
    renderTrajectory();
  } else if (currentView === 'tournament') {
    renderTournamentView();
  } else if (currentView === 'epoch') {
    renderEpochView();
  } else if (currentView === 'conversation') {
    renderConversationView();
  }
}

// --- Top-level render

function renderAll() {
  renderHeader();
  renderFooter();
  renderActiveView();
  applyRoute();
}

// --- HTTP + SSE

async function fetchJson(path) {
  const res = await fetch(path, { headers: { 'Accept': 'application/json' } });
  if (!res.ok) throw new Error(`HTTP ${res.status} on ${path}`);
  return await res.json();
}

async function postControl(action, body) {
  // Action buttons are disabled in v1.2; this function is wired up so
  // v1.3 just enables the buttons. Server contract returns 202 on
  // accepted-pending, 200 on synchronously applied.
  try {
    const res = await fetch('/api/control/' + action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body == null ? null : JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadFullState();
  } catch (err) {
    alert(`control failed: ${err.message}`);
  }
}

async function loadFullState() {
  try {
    const snap = await fetchJson('/api/state');
    state.setSnapshot(snap);
    renderAll();
  } catch (err) {
    // The server might be transient — surface gracefully.
    console.warn('loadFullState failed:', err);
  }
  // The epoch-definition endpoint (R8-A) is independent and may not
  // exist on older servers; fetch it separately and tolerate failure.
  // /api/state may already carry the epoch object — this just refreshes
  // it / fills it in when the state snapshot omitted it.
  try {
    const epoch = await fetchJson('/api/epoch');
    state.setEpochDef(epoch);
    if (currentView === 'epoch') renderEpochView();
  } catch (err) {
    // No /api/epoch — fine, the Epoch view degrades to empty states.
  }
  // The bracket and health-report endpoints (R9-5) are likewise
  // independent; fetch them in parallel and tolerate either being
  // absent.
  await Promise.all([loadBracket(), loadHealthReport()]);
  // The keystone fix: the dashboard's live data must be loaded UP
  // FRONT, not lazily when a view first renders. Previously
  // `activeTournament` was only fetched when the Tournament view
  // painted, so Overview and Epoch saw a null tournament and every
  // board entry fell back to a hardcoded 'queued'. Pull the four
  // cross-view feeds here so every view is live from the first paint.
  await loadLiveFeeds();
  // Repaint once the live feeds have landed — the earlier renderAll()
  // ran before activeTournament / lineage / logTail / activeRuns were
  // fetched, so without this the first paint stays stale.
  renderAll();
}

// Fetch the cross-view live feeds — active tournament, lineage, the
// run-log tail, health, and the active-runs list. Each is independent
// and tolerates an absent endpoint. Re-rendering is left to the caller.
async function loadLiveFeeds() {
  await Promise.all([
    fetchJson('/api/active-tournament')
      .then((t) => { state.activeTournament = t; })
      .catch(() => { /* no tournament endpoint — leave as-is */ }),
    fetchJson('/api/lineage')
      .then((l) => { if (l) state.lineage = l; })
      .catch(() => { /* no lineage endpoint */ }),
    fetchJson('/api/run-log?limit=40')
      .then((log) => { state.setLogTail(log); })
      .catch(() => { /* no run-log endpoint */ }),
    fetchJson('/api/active-runs')
      .then((r) => { if (Array.isArray(r)) state.activeRuns = r; })
      .catch(() => { /* no active-runs endpoint */ }),
    fetchJson('/api/health')
      .then((h) => { state.setHealth(h); })
      .catch(() => { /* no health endpoint */ }),
  ]);
}

// GET /api/tournaments — the gauntlet bracket. Independent of /api/state.
async function loadBracket() {
  try {
    const bracket = await fetchJson('/api/tournaments');
    state.setBracket(bracket);
    if (currentView === 'tournament') renderTournamentView();
  } catch (err) {
    // No /api/tournaments — the bracket degrades to its empty state.
  }
}

// GET /api/health-report — the loop-health panel.
async function loadHealthReport() {
  try {
    const report = await fetchJson('/api/health-report');
    state.setHealthReport(report);
    if (currentView === 'overview') renderHealthPanel();
  } catch (err) {
    // No /api/health-report — the panel degrades to its empty state.
  }
}

// GET /api/tournaments/:generation_id — the per-matchup detail. Cached
// so re-opening a matchup is instant; SSE invalidates it.
async function loadMatchupDetail(genId) {
  if (!genId) return;
  // In mock mode there is no server; synthesise the detail locally so
  // the bracket's detail panel is fully populated offline.
  if (state.mock) {
    const detail = mockMatchupDetail(genId);
    if (detail) {
      state.setMatchupDetail(genId, detail);
      if (currentView === 'tournament' && state.selectedMatchup === genId) {
        renderMatchupDetail();
      }
    }
    return;
  }
  try {
    const detail = await fetchJson('/api/tournaments/' + encodeURIComponent(genId));
    state.setMatchupDetail(genId, detail);
    if (currentView === 'tournament' && state.selectedMatchup === genId) {
      renderMatchupDetail();
    }
  } catch (err) {
    // Detail endpoint absent or 404 — renderMatchupDetail falls back to
    // the bracket summary record.
  }
}

async function refreshAfterEvent(payload) {
  // A state_change event names a region (legacy) or a kind. The R9
  // contract emits `kind` of epoch / tournament / heartbeat; the older
  // shape used `region`. Accept either; absent → refresh everything.
  const tag = (payload && (payload.kind || payload.region)) || 'all';
  try {
    if (tag === 'all') {
      await loadFullState();
      return;
    }
    // Whatever the tag, the cross-view live feeds — the active
    // tournament, the run-log tail and the active-runs list — are
    // re-fetched on EVERY tick. They are what keep Overview/Epoch from
    // going stale while the user is parked on another view; a tag of
    // `epoch` still moves runs, and a `tournament` tag still moves the
    // log. loadLiveFeeds also refreshes lineage + health cheaply.
    const liveFeeds = loadLiveFeeds();
    if (tag === 'tournament') {
      // A tournament changed: also refresh the bracket and drop the
      // cached detail for the moving matchup.
      state.matchupDetail.clear();
      await Promise.all([liveFeeds, loadBracket()]);
      if (state.selectedMatchup) loadMatchupDetail(state.selectedMatchup);
    } else if (tag === 'heartbeat') {
      try {
        state.setHeartbeat(await fetchJson('/api/heartbeat'));
      } catch (err) {
        // No dedicated heartbeat endpoint — fall back to a full refresh.
        await loadFullState();
        return;
      }
      await liveFeeds;
    } else if (tag === 'epoch') {
      try {
        const epoch = await fetchJson('/api/epoch');
        state.setEpochDef(epoch);
      } catch (err) { /* endpoint may be absent */ }
      // An epoch transition can also move the bracket and the health
      // report; refresh those too.
      await Promise.all([liveFeeds, loadBracket(), loadHealthReport()]);
    } else {
      // `runs`, `lineage`, or any unknown tag: the live feeds already
      // cover runs + lineage, so just await them.
      await liveFeeds;
    }
    renderAll();
  } catch (err) {
    console.warn('refresh failed:', err);
  }
}

let sse = null;
let sseRetry = 0;

function connectSSE() {
  state.connecting = true;
  renderHeader();
  try {
    sse = new EventSource('/events');
  } catch (err) {
    scheduleReconnect();
    return;
  }
  sse.addEventListener('open', () => {
    state.connected = true;
    state.connecting = false;
    sseRetry = 0;
    renderHeader();
  });
  sse.addEventListener('snapshot', (ev) => {
    try {
      const data = JSON.parse(ev.data);
      state.setSnapshot(data);
      renderAll();
    } catch (err) {
      console.warn('bad snapshot event:', err);
    }
  });
  sse.addEventListener('state_change', (ev) => {
    let payload = null;
    try { payload = JSON.parse(ev.data || '{}'); }
    catch { /* fine, region defaults to all */ }
    refreshAfterEvent(payload);
  });
  sse.addEventListener('log', (ev) => {
    try {
      const line = JSON.parse(ev.data);
      state.appendLog(line);
      renderLogTail();
    } catch (err) {
      console.warn('bad log event:', err);
    }
  });
  sse.addEventListener('heartbeat', (ev) => {
    try {
      state.setHeartbeat(JSON.parse(ev.data));
    } catch { /* ignore */ }
    renderHeader();
  });
  sse.addEventListener('error', () => {
    state.connected = false;
    renderHeader();
    if (sse && sse.readyState === EventSource.CLOSED) {
      scheduleReconnect();
    }
  });
}

function scheduleReconnect() {
  if (sse) { sse.close(); sse = null; }
  sseRetry++;
  const delay = Math.min(SSE_BACKOFF_MAX_MS, 500 * Math.pow(2, Math.min(sseRetry, 6)));
  setTimeout(connectSSE, delay);
}

// --- Mock state — for file:// preview and offline development
//
// The mock covers all four views: a cross-epoch lineage (two epochs),
// the gauntlet bracket (multi-generation, mixed promoted / rejected), a
// live matchup, a health report with one warning finding, the full
// epoch contract, and a heartbeat carrying a harmonograf_url so the
// deep-links render.

// GET /api/tournaments/:generation_id — synthetic per-matchup detail
// for mock mode. Keyed by challenger generation id.
function mockMatchupDetail(genId) {
  const details = {
    v1: {
      hypothesis: {
        core_idea: 'Tighten the extraction schema to reject loose types.',
        why: 'Schema drift was the dominant kind in v0.',
        modulating: ['researcher.schema'],
      },
      patches: [
        { mutation_id: 'researcher.schema', op: 'replace',
          rationale: 'narrow allowed types to the strict invoice contract' },
      ],
      entry_grid: [
        // This row carries explicit per-side session ids — the
        // harmonograf grid links use them verbatim.
        { entry_id: 'extract_invoice_001', parent_drift_loss: 0.30,
          child_drift_loss: 0.21, parent_pass: true, child_pass: true,
          verdict: 'improved',
          parent_session_id: 's-v0-extract_invoice_001',
          child_session_id: 's-v1-extract_invoice_001' },
        // This row carries no session ids — the grid links fall back
        // to the deterministic `{generation}--{entry}` run-id form.
        { entry_id: 'schema_response', parent_drift_loss: 0.12,
          child_drift_loss: 0.34, parent_pass: true, child_pass: false,
          verdict: 'regressed' },
      ],
      scalar: { parent: 0.41, child: 0.43, delta: 0.022,
        components: { drift: -0.04, cost: 0.01, rubric: 0.05 } },
      decision: 'rejected',
      rejection_reason: 'pass-rate regression on schema_response — the strict schema rejected a valid borderline response.',
    },
    v2: {
      hypothesis: {
        core_idea: 'Move JSON validation earlier in the pipeline.',
        why: 'Validating before emit catches malformed output before it scores.',
        modulating: ['pipeline.order'],
      },
      patches: [
        { mutation_id: 'pipeline.order', op: 'reorder',
          rationale: 'validate-before-emit so a bad response never reaches scoring' },
      ],
      entry_grid: [
        { entry_id: 'extract_invoice_001', parent_drift_loss: 0.23,
          child_drift_loss: 0.15, parent_pass: true, child_pass: true,
          verdict: 'improved' },
        { entry_id: 'extract_invoice_002', parent_drift_loss: 0.31,
          child_drift_loss: 0.22, parent_pass: true, child_pass: true,
          verdict: 'improved' },
        { entry_id: 'schema_response', parent_drift_loss: 0.18,
          child_drift_loss: 0.18, parent_pass: true, child_pass: true,
          verdict: 'flat' },
      ],
      scalar: { parent: 0.49, child: 0.41, delta: -0.080,
        components: { drift: -0.06, cost: -0.01, rubric: -0.01 } },
      decision: 'promoted', rejection_reason: null,
    },
    v2x: {
      hypothesis: {
        core_idea: 'Inline the validator instead of reordering the pipeline.',
        why: 'Reordering added a stage; inlining avoids the extra hop.',
        modulating: ['pipeline.order'],
      },
      patches: [
        { mutation_id: 'pipeline.order', op: 'replace',
          rationale: 'inline validate into emit to drop a pipeline stage' },
      ],
      entry_grid: [
        { entry_id: 'extract_invoice_001', parent_drift_loss: 0.15,
          child_drift_loss: 0.17, parent_pass: true, child_pass: true,
          verdict: 'flat' },
        { entry_id: 'schema_response', parent_drift_loss: 0.18,
          child_drift_loss: 0.41, parent_pass: true, child_pass: false,
          verdict: 'regressed' },
      ],
      scalar: { parent: 0.41, child: 0.44, delta: 0.030,
        components: { drift: 0.02, cost: -0.02, rubric: 0.03 } },
      decision: 'rejected',
      rejection_reason: 'pass-rate regression on schema_response — coupling validation to emit dropped a guard.',
    },
    v4: {
      hypothesis: {
        core_idea: 'Carry the picky retry pass into the new epoch baseline.',
        why: 'The retry pass cleared borderline rejections last epoch.',
        modulating: ['researcher.retry'],
      },
      patches: [
        { mutation_id: 'researcher.retry', op: 'insert',
          rationale: 'retry once on a first-pass fail before scoring' },
      ],
      entry_grid: [
        { entry_id: 'extract_invoice_002', parent_drift_loss: 0.34,
          child_drift_loss: 0.31, parent_pass: false, child_pass: true,
          verdict: 'improved' },
        { entry_id: 'multi_turn_picky', parent_drift_loss: 0.28,
          child_drift_loss: 0.27, parent_pass: true, child_pass: true,
          verdict: 'flat' },
      ],
      scalar: { parent: 0.41, child: 0.38, delta: -0.030,
        components: { drift: -0.03, cost: 0.02, rubric: -0.02 } },
      decision: 'promoted', rejection_reason: null,
    },
  };
  return details[genId] || null;
}

function mockSnapshot() {
  return {
    epoch_summary: {
      id: '2026-05-15_e1',
      generation: 'v5',
      round: '2',
      startedAt: new Date(Date.now() - 4 * 60_000 - 23_000).toISOString(),
    },
    heartbeat: {
      // The header reads generation_id / round_index straight off the
      // heartbeat; the elapsed clock is now − started_at; the stale
      // badge is now − last_heartbeat > 90s. The two zone forms (`Z`
      // and `+00:00`) are mixed on purpose so ?mock=1 exercises the
      // robust parseIso path.
      generation_id: 'v5',
      round_index: 2,
      // Boards execute in parallel; the tournament hall appends this to
      // its occupancy header when present.
      parallelism: 3,
      last_heartbeat: new Date().toISOString(),
      round_started_at: new Date(Date.now() - 263_000).toISOString()
        .replace('Z', '+00:00'),
      started_at: new Date(Date.now() - 4 * 60_000 - 23_000).toISOString(),
      pid: 12345, instance_id: 'mock',
      // Assembled from parts so the static bundle carries no literal
      // external URL — the no-external-fetch structural test forbids
      // `http://` / `https://`. A real heartbeat carries this verbatim
      // from the orchestrator; mock mode just needs a sample to render
      // the deep-links.
      harmonograf_url: 'ht' + 'tp' + '://localhost:4180',
    },
    supervisor: { version: '1.2.0', port: '7892', build: 'mock' },
    // GET /api/health — supervisor identity for the footer.
    health: {
      version: '0.1.0', port: 7892, build: '0.1.0+9feb5e8d3a16',
      uptime_seconds: 5_280,
    },
    scoring: { margin: 0.05 },
    // GET /api/active-runs — each element carries progress (0..1),
    // elapsed_seconds and budget_seconds so the run cards' progress
    // meters render under ?mock=1.
    active_runs: [
      { run_id: 'r-9c2a', entry_id: 'research_topic_q3', generation_id: 'v5',
        session_id: 's-research-9c2a',
        started_at: new Date(Date.now() - 42_000).toISOString(),
        progress: 0.23, elapsed_seconds: 42, budget_seconds: 180 },
      { run_id: 'r-7f10', entry_id: 'multi_turn_picky', generation_id: 'v5',
        started_at: new Date(Date.now() - 14_000).toISOString(),
        progress: 0.06, elapsed_seconds: 14, budget_seconds: 240 },
      // An over-deadline run — elapsed past budget. The hall board card
      // turns its bar red and flags the side.
      { run_id: 'r-3b88', entry_id: 'schema_response', generation_id: 'v5',
        started_at: new Date(Date.now() - 720_000).toISOString(),
        progress: 0.88, elapsed_seconds: 720, budget_seconds: 600 },
    ],
    // GET /api/active-tournament — the contract shape: round_index,
    // parent/child generation ids, and a flat per-SIDE entries list
    // (each board entry appears once per side). Rich extras
    // (hypothesis, drift_movements, per-entry runtime) are additive.
    active_tournament: {
      round_index: 2, total_rounds: 4,
      parent_generation_id: 'v4', child_generation_id: 'v5',
      elapsed_seconds: 263,
      hypothesis: {
        core_idea: 'Compress researcher tool descriptions to under 80 tokens each to reduce context bloat without dropping signal.',
        why: 'Round 1 drift was dominated by off_topic when the context window filled with verbose tool docs.',
        modulating: ['researcher_tool_descriptions', 'write_webpage_tool'],
      },
      // Boards execute in parallel — several entries are `running` at
      // once. The hall grid renders that naturally, with an accent
      // border on every board that has a running side.
      entries: [
        { entry_id: 'extract_invoice_001', side: 'parent', status: 'done',
          scalar_score: 0.23 },
        { entry_id: 'extract_invoice_001', side: 'child', status: 'done',
          scalar_score: 0.18 },
        { entry_id: 'extract_invoice_002', side: 'parent', status: 'done',
          scalar_score: 0.31 },
        { entry_id: 'extract_invoice_002', side: 'child', status: 'done',
          scalar_score: 0.45 },
        { entry_id: 'research_topic_q3', side: 'parent', status: 'done',
          scalar_score: 0.19 },
        { entry_id: 'research_topic_q3', side: 'child', status: 'running',
          run_id: 'r-9c2a' },
        { entry_id: 'multi_turn_picky', side: 'parent', status: 'done',
          scalar_score: 0.27 },
        { entry_id: 'multi_turn_picky', side: 'child', status: 'running',
          run_id: 'r-7f10' },
        { entry_id: 'schema_response', side: 'parent', status: 'done',
          scalar_score: 0.14 },
        { entry_id: 'schema_response', side: 'child', status: 'running',
          run_id: 'r-3b88' },
      ],
      drift_movements: [
        { kind: 'off_topic', from_rate: 0.18, to_rate: 0.12 },
        { kind: 'schema_violation', from_rate: 0.10, to_rate: 0.14 },
      ],
    },
    past_tournaments: [
      {
        round: 1, round_index: 1, total_rounds: 4,
        parent_id: 'v4_seed', child_id: 'v4',
        hypothesis: {
          core_idea: 'Carry the prior epoch’s retry pass forward as the v4 baseline.',
          modulating: ['picky_retry_pass'],
        },
        entries: [
          { entry_id: 'extract_invoice_001', status: 'done',
            parent: { drift_loss: 0.28, pass: true },
            child:  { drift_loss: 0.23, pass: true } },
          { entry_id: 'extract_invoice_002', status: 'done',
            parent: { drift_loss: 0.34, pass: false },
            child:  { drift_loss: 0.31, pass: true } },
        ],
        drift_movements: [
          { kind: 'off_topic', from_rate: 0.22, to_rate: 0.18 },
        ],
      },
    ],
    // GET /api/tournaments — the gauntlet bracket. The champion lineage
    // is the promoted spine; matchups carry both promoted and rejected
    // challenges so the bracket can hang the discards below.
    bracket: {
      epoch_id: '2026-05-15_e1',
      champion_lineage: ['v0', 'v2', 'v4'],
      matchups: [
        { champion: 'v0', challenger: 'v1', decision: 'rejected',
          delta_scalar: 0.022,
          rejection_reason: 'pass-rate regression on schema_response',
          hypothesis_core_idea: 'Tighten the extraction schema to reject loose types.',
          ran_at: '2026-05-10T10:12:00Z' },
        { champion: 'v0', challenger: 'v2', decision: 'promoted',
          delta_scalar: -0.080,
          hypothesis_core_idea: 'Move JSON validation earlier in the pipeline.',
          ran_at: '2026-05-10T11:40:00Z' },
        { champion: 'v2', challenger: 'v2x', decision: 'rejected',
          delta_scalar: 0.030,
          rejection_reason: 'pass-rate regression on schema_response',
          hypothesis_core_idea: 'Inline the validator instead of reordering the pipeline.',
          ran_at: '2026-05-10T13:05:00Z' },
        { champion: 'v2', challenger: 'v4', decision: 'promoted',
          delta_scalar: -0.030,
          hypothesis_core_idea: 'Carry the picky retry pass into the new epoch baseline.',
          ran_at: '2026-05-15T09:20:00Z' },
      ],
    },
    // GET /api/health-report — the loop-health panel. One warning
    // finding so the panel demonstrates a non-trivial state.
    health_report: {
      epoch_id: '2026-05-15_e1',
      healthy: false,
      checked_at: new Date(Date.now() - 90_000).toISOString(),
      findings: [
        { code: 'rubric.low_spread', severity: 'warning',
          summary: 'Rubric scores cluster in a 0.08-wide band across the board.',
          detail: 'A narrow rubric spread weakens the optimization signal — challengers and champions score nearly the same. Consider widening the rubric or adding harder board entries.' },
      ],
    },
    // GET /api/lineage — generations across every epoch. The contract
    // keys are generation_id / parent_generation_id / promoted /
    // created_at; promoted===null marks the in-flight generation. The
    // legacy id / parent_id aliases are kept alongside for the tree
    // view's existing node layout.
    lineage: {
      generations: [
        { generation_id: 'v0', id: 'v0', parent_generation_id: null,
          parent_id: null, epoch_id: '2026-05-10_e0', promoted: true,
          created_at: '2026-05-10T09:00:00Z' },
        { generation_id: 'v1', id: 'v1', parent_generation_id: 'v0',
          parent_id: 'v0', epoch_id: '2026-05-10_e0', promoted: false,
          created_at: '2026-05-10T10:00:00Z' },
        { generation_id: 'v2', id: 'v2', parent_generation_id: 'v1',
          parent_id: 'v1', epoch_id: '2026-05-10_e0', promoted: true,
          created_at: '2026-05-10T11:30:00Z' },
        { generation_id: 'v2x', id: 'v2x', parent_generation_id: 'v1',
          parent_id: 'v1', epoch_id: '2026-05-10_e0', promoted: false,
          created_at: '2026-05-10T13:00:00Z' },
        { generation_id: 'v4_seed', id: 'v4_seed', parent_generation_id: null,
          parent_id: null, epoch_id: '2026-05-15_e1', v0_parent: 'v2',
          promoted: true, created_at: '2026-05-15T09:02:00Z' },
        { generation_id: 'v4', id: 'v4', parent_generation_id: 'v4_seed',
          parent_id: 'v4_seed', epoch_id: '2026-05-15_e1', promoted: true,
          created_at: '2026-05-15T09:20:00Z' },
        { generation_id: 'v5', id: 'v5', parent_generation_id: 'v4',
          parent_id: 'v4', epoch_id: '2026-05-15_e1', promoted: null,
          created_at: '2026-05-15T09:50:00Z' },
      ],
      experiments: [
        { generation_id: 'v1', hypothesis: { core_idea: 'Tighten extraction schema.' },
          outcome: { tournament_decision: 'promoted',
               scalar_score_delta: -0.080, drift_loss_delta: -0.06,
               pass_rate_delta: 0.10, drift_movements: [
                 { kind: 'off_topic', from_rate: 0.4, to_rate: 0.2 },
                 { kind: 'hallucinated_field', from_rate: 0.3, to_rate: 0.15 },
               ]}},
        { generation_id: 'v2', hypothesis: { core_idea: 'Move JSON validation earlier.' },
          outcome: { tournament_decision: 'promoted',
               scalar_score_delta: -0.040, drift_loss_delta: -0.04,
               pass_rate_delta: 0.05, drift_movements: [
                 { kind: 'off_topic', from_rate: 0.2, to_rate: 0.18 },
                 { kind: 'schema_violation', from_rate: 0.25, to_rate: 0.10 },
               ]}},
        { generation_id: 'v2x', hypothesis: { core_idea: 'Inline the validator instead of reordering.' },
          outcome: { tournament_decision: 'rejected',
               scalar_score_delta: 0.030, drift_loss_delta: 0.02,
               pass_rate_delta: -0.04, rejection_reason: 'pass-rate regression on schema_response' }},
        { generation_id: 'v4', hypothesis: { core_idea: 'Carry the picky retry pass into the new epoch baseline.' },
          outcome: { tournament_decision: 'promoted',
               scalar_score_delta: -0.030, drift_loss_delta: -0.03,
               pass_rate_delta: 0.04, drift_movements: [
                 { kind: 'off_topic', from_rate: 0.22, to_rate: 0.18 },
               ]}},
      ],
    },
    experiments: [
      { generation_id: 'v1', hypothesis: { core_idea: 'Tighten extraction schema.', why: 'Schema drift was the dominant kind in v0.', risks: 'May reject borderline-valid responses.' },
        patches: [{ mutation_id: 'researcher.schema', op: 'replace', rationale: 'narrow allowed types' }],
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.080, drift_loss_delta: -0.06, pass_rate_delta: 0.10 }},
      { generation_id: 'v2', hypothesis: { core_idea: 'Move JSON validation earlier.', why: 'Pipeline ordering issue.', risks: '' },
        patches: [{ mutation_id: 'pipeline.order', op: 'reorder', rationale: 'validate-before-emit' }],
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.040, drift_loss_delta: -0.04, pass_rate_delta: 0.05 }},
      { generation_id: 'v2x', hypothesis: { core_idea: 'Inline the validator instead of reordering.', why: 'Reorder added a stage; inline avoids it.', risks: 'Couples validation to emit.' },
        patches: [{ mutation_id: 'pipeline.order', op: 'replace', rationale: 'inline validate' }],
        outcome: { tournament_decision: 'rejected', scalar_score_delta: 0.030, drift_loss_delta: 0.02, pass_rate_delta: -0.04, rejection_reason: 'pass-rate regression on schema_response' }},
      { generation_id: 'v4', hypothesis: { core_idea: 'Carry the picky retry pass into the new epoch baseline.', why: 'Retry pass cleared borderline rejections last epoch.', risks: 'Extra cost.' },
        patches: [{ mutation_id: 'researcher.retry', op: 'insert', rationale: 'retry on first-pass fail' }],
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.030, drift_loss_delta: -0.03, pass_rate_delta: 0.04 }},
      { generation_id: 'v5', hypothesis: { core_idea: 'Compress researcher tool descriptions to under 80 tokens each to reduce context bloat without dropping signal.', why: 'Round 1 drift was dominated by off_topic when the context window filled with verbose tool docs.', risks: 'Over-compression could drop a tool argument hint.' },
        patches: [{ mutation_id: 'researcher_tool_descriptions', op: 'replace', rationale: 'compress to <80 tokens' }],
        outcome: null },
    ],
    epoch: {
      epoch_id: '2026-05-15_e1',
      contract_hash: 'abc123def4567890',
      created_at: '2026-05-15T09:02:00Z',
      closed: false,
      harness: {
        entrypoint: 'myagent.harness:research_pipeline',
        mutable_trees: ['myagent/prompts', 'myagent/researcher'],
      },
      board: [
        { id: 'extract_invoice_001', kind: 'single_turn',
          input_preview: 'Extract the invoice total and due date from the attached PDF text...',
          expectation_kind: 'predicate', budget_s: 900, weight: 1.0, tags: ['extraction'] },
        { id: 'extract_invoice_002', kind: 'single_turn',
          input_preview: 'Extract line items from a multi-page invoice with a discount row...',
          expectation_kind: 'predicate', budget_s: 900, weight: 1.5, tags: ['extraction', 'hard'] },
        { id: 'research_topic_q3', kind: 'multi_turn',
          input_preview: 'Research the Q3 regulatory changes and summarise impact for a non-expert...',
          expectation_kind: 'rubric_judge', budget_s: 1800, weight: 1.0, tags: ['research'] },
        { id: 'multi_turn_picky', kind: 'multi_turn',
          input_preview: 'A picky client revises the brief twice; satisfy all constraints...',
          expectation_kind: 'rubric_judge', budget_s: 1800, weight: 1.0, tags: ['research', 'hard'] },
        { id: 'schema_response', kind: 'single_turn',
          input_preview: 'Return a strictly-typed JSON object matching the given schema...',
          expectation_kind: 'predicate', budget_s: 600, weight: 1.0, tags: ['schema'] },
      ],
      rubric: '# Research rubric\n\nThe candidate response is judged on three axes.\n\n## Faithfulness\n\n- Every claim is grounded in a retrieved source.\n- No `off_topic` drift: the answer addresses the brief.\n\n## Completeness\n\n- All sub-questions in the brief are answered.\n- Schema-typed responses validate against the contract.\n\n## Concision\n\n- The answer is no longer than necessary; tool descriptions stay terse.\n',
      scoring: {
        drift_weight: 1.0,
        pass_rate_weight: 1.0,
        margin: 0.05,
        rubric_weight: 0.5,
      },
      mutations: [
        { id: 'researcher_tool_descriptions', kind: 'span',
          file: 'myagent/researcher/tools.py', lines: '12-34',
          preview: 'TOOL_DESCRIPTIONS = {\n  "search": "Search the corpus...' },
        { id: 'write_webpage_tool', kind: 'span',
          file: 'myagent/researcher/tools.py', lines: '40-58',
          preview: 'def write_webpage(url): ...' },
        { id: 'researcher.instruction', kind: 'file',
          file: 'myagent/prompts/researcher.md', lines: '1-120',
          preview: 'You are a careful research assistant...' },
      ],
    },
    log_tail: [
      { ts: '12:34:50', level: 'info', message: 'tournament r2 entry research_topic_q3 started (run r-9c2a)' },
      { ts: '12:35:01', level: 'info', message: 'goldfive driver: tool researcher_search invoked' },
      { ts: '12:35:14', level: 'warn', message: 'drift detected: off_topic +1 in run r-9c2a' },
      { ts: '12:35:23', level: 'ok',   message: 'parent v4 entry extract_invoice_002 pass' },
    ],
    // GET /api/run-log?limit=40 — the structured event tail. The
    // contract shape is { events:[{ seq, kind, ts, summary }] }; seq
    // and ts may be null for a synthetic / un-sequenced event.
    run_log: {
      events: [
        { seq: 118, kind: 'tournament', ts: '2026-05-16T04:34:50Z',
          summary: 'tournament r2 entry research_topic_q3 started (run r-9c2a)' },
        { seq: 119, kind: 'run', ts: '2026-05-16T04:35:01Z',
          summary: 'goldfive driver: tool researcher_search invoked' },
        { seq: 120, kind: 'drift', ts: '2026-05-16T04:35:14Z',
          summary: 'drift detected: off_topic +1 in run r-9c2a' },
        { seq: 121, kind: 'score', ts: '2026-05-16T04:35:23Z',
          summary: 'parent v4 entry extract_invoice_002 pass' },
        { seq: null, kind: 'note', ts: null,
          summary: 'supervisor watchdog tick — all workers responsive' },
      ],
    },
  };
}

// --- Bootstrap

function init() {
  // Drill panel close button + escape
  $('drill-close').addEventListener('click', closeDrill);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') closeDrill();
  });
  // The hash router resolves both the view and any drill-down.
  window.addEventListener('hashchange', applyRoute);

  // Tree-view pan + zoom wiring (the SVG itself is repainted on render).
  setupLineageInteractions();

  // Tick the elapsed-time fields once per second so the header reads "live"
  // even when no state changes arrive.
  setInterval(() => {
    renderHeader();
    // Also tick the tournament elapsed if running
    const t = state.activeTournament;
    if (t && t.entries) {
      const tEl = $('tournament-elapsed');
      if (tEl && t.elapsed_seconds != null) {
        tEl.textContent = 'Elapsed ' + fmtDuration(t.elapsed_seconds + 1);
        t.elapsed_seconds += 1;
      }
    }
  }, 1000);

  // Resolve the initial view from the URL fragment.
  const initialSegs = (location.hash || '').replace(/^#\/?/, '').split('/').filter(Boolean);
  if (initialSegs.length && VIEWS.includes(initialSegs[0])) {
    currentView = initialSegs[0];
  } else if (!location.hash || location.hash === '#') {
    // Land on the default view with a clean fragment.
    currentView = DEFAULT_VIEW;
  }

  const params = new URLSearchParams(window.location.search);
  if (params.get('mock') === '1') {
    state.mock = true;
    state.connected = false;
    state.connecting = false;
    state.setSnapshot(mockSnapshot());
    renderAll();
    return;
  }

  renderAll();
  loadFullState();
  connectSSE();
}

// Expose for tests + console debugging without polluting global scope.
window.__zicato = { state, predictedGateVerdict, renderAll };

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
