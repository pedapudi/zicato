// variants/T/live.js — the LIVE-RUN engine: make a run feel ALIVE without
// reintroducing flashing.
//
// THE PROBLEM this addresses: T's live surfaces were digest-gated (good: no
// flashing) but felt STATIC — no motion, no sense of progress, and updates
// lagged on the poll tick. The fix is to animate the actual state *changes*
// (transitions / deltas), never repaint-loop, and to prefer push (SSE) over
// poll.
//
// This module is the structure-agnostic distillation of the three live read
// signals into:
//   * liveProgress(...)   — a tournament-level progress verdict ("rung k of N ·
//                           m/n matchups", a 0..1 fraction) derived from the
//                           active-tournament rounds + the heartbeat phase.
//   * deriveActivity(...) — a PURE diff of two live snapshots → the activity
//                           events that fired between them (matchup started, run
//                           completed w/ loss, rung cut, gate decided, promotion,
//                           phase change). Newest-first, append-only.
//   * ActivityTicker      — an append-only, capped feed that NEVER repaints: new
//                           rows are prepended, old rows trimmed, surviving rows
//                           untouched (so it cannot flash or reorder).
//   * liveSnapshot(...)   — the small comparable snapshot deriveActivity diffs.
//
// Everything here is dependency-light and DOM-optional where it can be — the
// derivations are pure so they unit-test without a DOM. `prefers-reduced-motion`
// is honoured in CSS (the JS only adds/keeps stable nodes; CSS gates ALL motion).

import { el, patchText, patchClass } from '../../core/dom.js';
import { isNum, survivalFunnel, swissLadder, elimBracket } from './svg.js';
import {
  racingModel, swissModel, elimModel, normalizeStructure,
  buildLiveSwissModel, buildLiveElimModel,
} from './views/structure.js';

// ── tournament-level progress ────────────────────────────────────────
//
// Derive a tournament-level progress indicator from the LIVE active-tournament
// topology + the heartbeat phase. Racing speaks RUNGS (k of N) + per-rung
// matchups (m/n); the elimination/swiss structures speak ROUNDS. Returns a
// plain verdict the hero + the chrome can render:
//
//   { kind, label, detail, fraction }   (fraction ∈ [0,1] or null)
//
// `label` is the headline ("rung 2 of 3"); `detail` is the matchup tally
// ("m/n matchups" / "field of N"); `fraction` drives a determinate bar.
export function liveProgress({ activeTournament, heartbeat, status } = {}) {
  const rawAt = (activeTournament && typeof activeTournament === 'object') ? activeTournament : null;
  // SCOPE TO THE CURRENT RUN: ignore an active-tournament retained from a
  // DIFFERENT (foreign) epoch — its rung/round topology must not drive the live
  // progress of the current run (e.g. e1's completed racing ladder leaking "rung
  // k of N" while e3 is proposing). Fall through to the heartbeat phase string
  // ("proposing field…") for the honest current-run state.
  const at = (rawAt && liveBelongsToEpoch(rawAt, heartbeat)) ? rawAt : null;
  const structure = (status && status.structure) || (at && at.structure) || null;
  const phase = (heartbeat && heartbeat.phase) || (status && status.phase) || null;

  // racing: rung k of N + per-rung matchups.
  if (String(structure) === 'racing' && at) {
    const model = racingModel(normalizeForModel(at));
    if (model && Array.isArray(model.rungs) && model.rungs.length) {
      const total = model.rungs.length;
      // the CURRENT rung = the first still-pending rung; else the last (gate).
      let cur = model.rungs.findIndex((r) => r.pending);
      if (cur < 0) cur = total - 1;
      const rung = model.rungs[cur];
      const fieldN = Array.isArray(rung.competitors) ? rung.competitors.length : 0;
      const decided = (Array.isArray(rung.survivors) ? rung.survivors.length : 0)
        + (Array.isArray(rung.cut) ? rung.cut.length : 0);
      // resolved rungs + this rung's matchup fraction → an overall fraction.
      const resolved = model.rungs.filter((r) => !r.pending).length;
      const within = fieldN > 0 ? Math.min(1, decided / fieldN) : 0;
      const fraction = total > 0 ? Math.min(1, (resolved + (rung.pending ? within : 1)) / total) : null;
      return {
        kind: 'racing',
        label: `rung ${cur + 1} of ${total}`,
        detail: rung.pending && fieldN
          ? `${decided}/${fieldN} matchups`
          : (fieldN ? `field of ${fieldN}` : ''),
        fraction,
      };
    }
  }

  // generic round-based progress from the active-tournament rounds.
  if (at && Array.isArray(at.rounds) && at.rounds.length) {
    const rounds = at.rounds;
    const total = rounds.length;
    // a round is "done" when every match carries a winner / decision.
    const done = rounds.filter((r) => {
      const ms = Array.isArray(r.matches) ? r.matches : [];
      return ms.length && ms.every((m) => m && (m.winner || m.decision));
    }).length;
    const cur = Math.min(total - 1, done);
    const curRound = rounds[cur] || {};
    const ms = Array.isArray(curRound.matches) ? curRound.matches : [];
    const decided = ms.filter((m) => m && (m.winner || m.decision)).length;
    return {
      kind: 'round',
      label: `round ${cur + 1} of ${total}`,
      detail: ms.length ? `${decided}/${ms.length} matchups` : '',
      fraction: total > 0 ? Math.min(1, done / total) : null,
    };
  }

  // no topology yet — fall back to the phase string (e.g. "proposing field").
  if (phase) {
    return { kind: 'phase', label: String(phase).split(':').slice(0, 2).join(' · ').replace(/_/g, ' '), detail: '', fraction: null };
  }
  return { kind: 'idle', label: '', detail: '', fraction: null };
}

// normalize an active-tournament payload into the racingModel input shape (it
// is forgiving — racingModel reads .structure/.rounds/.champion_lineage).
function normalizeForModel(at) {
  return {
    structure: at.structure || 'racing',
    rounds: Array.isArray(at.rounds) ? at.rounds : [],
    champion_lineage: Array.isArray(at.champion_lineage) ? at.champion_lineage : [],
    live: true,
  };
}

// ── a comparable live snapshot ───────────────────────────────────────
//
// The minimal projection of the live state that deriveActivity() diffs. Keeping
// it small + plain means the diff is cheap and the snapshot is trivially cloned
// across ticks. Each in-flight run is keyed by run_id (or gen|entry).
export function liveSnapshot({ heartbeat, activeRuns, activeTournament, status } = {}) {
  const rawAt = (activeTournament && typeof activeTournament === 'object') ? activeTournament : null;
  // SCOPE TO THE CURRENT RUN: a foreign-epoch active-tournament must not feed the
  // activity ticker — otherwise a prior epoch's retained rung topology replays as
  // "rung cut · vN eliminated" events under the current run (leaking foreign
  // competitor ids). The in-flight RUNS are kept regardless (active-runs is
  // ground truth for the current run); only the tournament-derived rung/lineage
  // signal is dropped when the tournament belongs to a different epoch.
  const at = (rawAt && liveBelongsToEpoch(rawAt, heartbeat)) ? rawAt : null;
  const runs = Array.isArray(activeRuns) ? activeRuns : [];
  const phase = (heartbeat && heartbeat.phase) || (status && status.phase) || null;

  // per-rung survivor/cut tallies (racing) keyed by match_id.
  const rungs = {};
  if (at && Array.isArray(at.rounds)) {
    for (const r of at.rounds) {
      const m = (Array.isArray(r.matches) && r.matches[0]) ? r.matches[0] : null;
      if (!m || !m.match_id) continue;
      rungs[m.match_id] = {
        survivors: (Array.isArray(m.survivors) ? m.survivors.map(String) : []).slice().sort(),
        cut: (Array.isArray(m.cut) ? m.cut.map(String) : []).slice().sort(),
        winner: m.winner || null,
        decision: m.decision || null,
      };
    }
  }
  return {
    phase: phase != null ? String(phase) : null,
    structure: (status && status.structure) || (at && at.structure) || null,
    running: !!(status && status.running),
    runs: runs.map((r) => ({
      key: String(r.run_id || (String(r.generation_id || r.gen || '') + '|' + String(r.entry_id || r.board_entry_id || r.entry || ''))),
      gen: r.generation_id || r.gen || null,
      entry: r.entry_id || r.board_entry_id || r.entry || null,
    })),
    rungs,
    lineageLen: (at && Array.isArray(at.champion_lineage)) ? at.champion_lineage.length : 0,
  };
}

// ── derive activity events from a prev→next snapshot diff ─────────────
//
// PURE: given the previous + the current snapshot it returns the events that
// fired in between, newest-first. Each event is { id, kind, text, gen, tone }
// where tone ∈ 'good'|'bad'|'neutral'. `seq` is a monotonic counter the caller
// owns so two identical-looking events still get distinct keys (the ticker is
// append-only and de-dups by id).
export function deriveActivity(prev, next, seq) {
  const out = [];
  let n = seq || 0;
  const push = (kind, text, tone, gen) => { out.push({ id: 'a' + (++n), kind, text, tone: tone || 'neutral', gen: gen || null }); };
  if (!next) return { events: out, seq: n };

  // a phase transition (e.g. proposing → tournament:rung0).
  if (!prev || prev.phase !== next.phase) {
    if (next.phase) {
      const head = String(next.phase).split(':')[0];
      if (!prev && next.running) push('phase', 'run started · ' + humanPhase(next.phase), 'neutral');
      else if (head === 'proposing') push('phase', 'proposing · ' + humanPhase(next.phase), 'neutral');
      else if (prev) push('phase', 'phase · ' + humanPhase(next.phase), 'neutral');
    } else if (prev && prev.phase && next.running === false) {
      push('phase', 'run idle', 'neutral');
    }
  }

  // matchups started / completed: diff the in-flight run set.
  if (prev) {
    const prevKeys = new Set(prev.runs.map((r) => r.key));
    const nextKeys = new Set(next.runs.map((r) => r.key));
    for (const r of next.runs) {
      if (!prevKeys.has(r.key)) {
        push('matchup', 'matchup started · ' + label(r.gen) + (r.entry ? ' on ' + label(r.entry) : ''), 'neutral', r.gen);
      }
    }
    for (const r of prev.runs) {
      if (!nextKeys.has(r.key)) {
        push('run', 'run completed · ' + label(r.gen) + (r.entry ? ' on ' + label(r.entry) : ''), 'neutral', r.gen);
      }
    }
  } else {
    for (const r of next.runs) push('matchup', 'matchup running · ' + label(r.gen), 'neutral', r.gen);
  }

  // rung cuts: a rung that gained survivors/cut since last tick.
  const prevRungs = (prev && prev.rungs) || {};
  for (const [mid, cur] of Object.entries(next.rungs)) {
    const was = prevRungs[mid] || { survivors: [], cut: [], winner: null, decision: null };
    const newlyCut = cur.cut.filter((c) => !was.cut.includes(c));
    const newlySurv = cur.survivors.filter((c) => !was.survivors.includes(c));
    if (String(mid) === 'racing-final') {
      // the champion gate decided.
      const dec = String(cur.decision || '').toLowerCase();
      if (dec && dec !== String(was.decision || '').toLowerCase()) {
        if (dec.includes('promot')) push('gate', 'champion-gate · ' + label(cur.winner) + ' promoted ♚', 'good', cur.winner);
        else push('gate', 'champion-gate · champion stands', 'neutral', cur.winner);
      }
      continue;
    }
    if (newlyCut.length) push('cut', 'rung cut · ' + newlyCut.map(label).join(', ') + ' eliminated ✕', 'bad', newlyCut[0]);
    if (newlySurv.length && was.survivors.length) push('survive', 'rung · ' + newlySurv.map(label).join(', ') + ' survive ↑', 'good', newlySurv[0]);
  }

  // a promotion confirmed via lineage growth.
  if (prev && next.lineageLen > prev.lineageLen) push('promote', 'promotion · the lineage advanced ♚', 'good');

  // newest-first.
  out.reverse();
  return { events: out, seq: n };
}

function humanPhase(p) {
  return String(p || '').replace(/:/g, ' · ').replace(/_/g, ' ');
}
function label(s) { return s == null ? '—' : String(s); }

// ── the activity ticker (append-only, capped, NEVER repaints) ─────────
//
// A compact streaming feed. New events are PREPENDED (newest on top), the list
// is capped, and surviving rows are left strictly untouched — so the ticker
// grows without flashing or reordering. The empty state is a single placeholder
// that is removed once the first event lands.
export class ActivityTicker {
  constructor(opts) {
    const o = opts || {};
    this.cap = o.cap || 40;
    this.node = el('div', { class: 'dt-ticker', role: 'log', 'aria-live': 'polite', 'aria-label': 'Live activity' });
    this._list = el('div', { class: 'dt-ticker-list' });
    this._empty = el('div', { class: 'dt-ticker-empty dn-faint', text: o.emptyText || 'waiting for activity…' });
    this.node.appendChild(this._empty);
    this.node.appendChild(this._list);
    this._count = 0;
    this._seen = new Set();
  }

  // Prepend new events (newest-first array). De-dups by id. Returns how many
  // rows were actually added.
  push(events) {
    const list = Array.isArray(events) ? events : [events];
    let added = 0;
    // events arrive newest-first; insert each at the TOP so the final order is
    // newest-on-top with the batch's own ordering preserved.
    for (let i = list.length - 1; i >= 0; i--) {
      const ev = list[i];
      if (!ev || ev.id == null || this._seen.has(ev.id)) continue;
      this._seen.add(ev.id);
      const row = this._buildRow(ev);
      row.setAttribute('data-key', String(ev.id));
      // PREPEND — surviving rows are untouched (no repaint).
      if (this._list.firstChild) this._list.insertBefore(row, this._list.firstChild);
      else this._list.appendChild(row);
      added += 1;
      this._count += 1;
    }
    if (added) {
      // hide the empty placeholder once we have content.
      if (this._empty && this._empty.parentNode) this._empty.parentNode.removeChild(this._empty);
      // trim oldest (the BOTTOM) beyond the cap — surviving rows untouched.
      while (this._count > this.cap && this._list.lastChild) {
        this._list.removeChild(this._list.lastChild);
        this._count -= 1;
      }
    }
    return added;
  }

  _buildRow(ev) {
    return el('div', { class: 'dt-ticker-row dt-ticker-' + (ev.tone || 'neutral'), 'data-kind': ev.kind || '' }, [
      el('span', { class: 'dt-ticker-glyph', 'aria-hidden': 'true', text: glyphFor(ev) }),
      el('span', { class: 'dt-ticker-text', text: ev.text || '' }),
    ]);
  }
}

function glyphFor(ev) {
  switch (ev && ev.kind) {
    case 'cut': return '✕';
    case 'survive': return '↑';
    case 'gate': return '♚';
    case 'promote': return '♚';
    case 'run': return '✓';
    case 'matchup': return '▸';
    case 'phase': return '·';
    default: return '·';
  }
}

// ── the LIVE-RUN HERO + controller ───────────────────────────────────
//
// A persistent, shell-owned focal panel that LEADS the page while a run is in
// flight. It is built ONCE (its nodes keep identity across ticks) and the
// controller patches it IN PLACE on each SSE-driven tick:
//   * the LIVE pill breathes (CSS) + the current phase reads prominently;
//   * a determinate tournament-progress bar animates toward 100% (CSS width
//     transition) with a "rung k of N · m/n matchups" caption;
//   * the survival funnel re-renders ONLY when the structure digest changes
//     (digest-gated swap — a steady tick writes ZERO funnel DOM), so the funnel
//     animates as rungs resolve / cuts fire without flashing;
//   * an in-flight unit count + the activity ticker stream live events.
//
// When idle, the hero hides itself (display gated by the `.dt-live-on` class)
// so the normal summary leads. The controller is structure-agnostic — it leans
// on racing's funnel where a racing topology is live, and degrades to the
// progress + ticker for any other structure.
export class LiveController {
  constructor(opts) {
    const o = opts || {};
    this.onCompetitor = typeof o.onCompetitor === 'function' ? o.onCompetitor : null;
    this._seq = 0;
    this._prevSnap = null;
    this._funnelDigest = null;
    this._lastProgressKey = null;
    this.ticker = new ActivityTicker({ cap: o.cap || 40 });
    this._build();
  }

  _build() {
    this._pill = el('span', { class: 'dt-live-hero-pill' }, [
      el('span', { class: 'dt-live-hero-dot', 'aria-hidden': 'true' }),
      el('span', { class: 'dt-live-hero-pilltext', text: 'LIVE' }),
    ]);
    this._phase = el('span', { class: 'dt-live-hero-phase', text: '' });
    this._count = el('span', { class: 'dt-live-hero-count', text: '' });
    this._progLabel = el('span', { class: 'dt-live-hero-proglab', text: '' });
    this._progDetail = el('span', { class: 'dt-live-hero-progdetail dn-faint', text: '' });
    this._progFill = el('span', { class: 'dt-live-hero-progfill' });
    this._progBar = el('span', { class: 'dt-live-hero-progbar' }, [this._progFill]);

    const head = el('div', { class: 'dt-live-hero-head' }, [
      this._pill, this._phase, this._count,
    ]);
    const prog = el('div', { class: 'dt-live-hero-prog' }, [
      el('div', { class: 'dt-live-hero-progrow' }, [this._progLabel, this._progDetail]),
      this._progBar,
    ]);

    this._funnelHost = el('div', { class: 'dt-live-hero-funnel dn-figpane' });
    this._tickerHost = el('div', { class: 'dt-live-hero-ticker' }, [
      el('div', { class: 'dt-live-hero-tickerhead dn-faint', text: 'live activity' }),
      this.ticker.node,
    ]);

    const body = el('div', { class: 'dt-live-hero-body' }, [this._funnelHost, this._tickerHost]);
    this.node = el('section', { class: 'dt-live-hero', 'aria-label': 'Live run', role: 'region' }, [head, prog, body]);
  }

  // Drive the hero from the current live state. Returns true when a run is live
  // (so the shell can toggle the hero's visibility class).
  update({ status, heartbeat, activeRuns, activeTournament } = {}) {
    const running = !!(status && status.running);
    patchClass(this.node, 'dt-live-on', running);

    // ── the activity ticker: diff the snapshot, append the new events ──
    const snap = liveSnapshot({ status, heartbeat, activeRuns, activeTournament });
    const { events, seq } = deriveActivity(this._prevSnap, snap, this._seq);
    this._seq = seq;
    if (events.length) this.ticker.push(events);
    this._prevSnap = running ? snap : null;
    if (!running) { this._funnelDigest = null; return false; }

    // ── prominent phase ──
    patchText(this._phase, (status && status.label) || (heartbeat && heartbeat.phase) || 'running');

    // ── in-flight unit count ──
    const inFlight = status && isNum(status.inFlight) ? status.inFlight : (Array.isArray(activeRuns) ? activeRuns.length : 0);
    patchText(this._count, inFlight > 0 ? (inFlight + (inFlight === 1 ? ' unit running' : ' units running')) : '');

    // ── tournament-level progress (determinate bar + caption) ──
    const prog = liveProgress({ activeTournament, heartbeat, status });
    patchText(this._progLabel, prog.label || '');
    patchText(this._progDetail, prog.detail ? '· ' + prog.detail : '');
    const pct = isNum(prog.fraction) ? Math.round(Math.max(0, Math.min(1, prog.fraction)) * 100) : null;
    const progKey = (prog.label || '') + '|' + (prog.detail || '') + '|' + pct;
    if (progKey !== this._lastProgressKey) {
      this._lastProgressKey = progKey;
      // width transition (CSS) animates the bar smoothly toward the new value;
      // an indeterminate (unknown) fraction shows a thin pending bar. Set via
      // setProperty so the width lands in the style declaration (and survives a
      // patch without rebuilding the node — that is what makes the CSS width
      // transition fire rather than a node swap).
      this._progFill.style.setProperty('width', (pct != null ? pct : 8) + '%');
      patchClass(this._progFill, 'dt-live-hero-progfill-pending', pct == null);
    }

    // ── the structure FIGURE: digest-gated swap (animate only on real change) ──
    // STRUCTURE-DISPATCHED + CURRENT-RUN-ONLY (see structureEligible): racing →
    // funnel, swiss → live standings ladder, elim → live bracket; anything else /
    // completed / stale-foreign shows the honest placeholder.
    this._updateStructure(activeTournament, heartbeat, activeRuns);
    return true;
  }

  _updateStructure(activeTournament, heartbeat, activeRuns) {
    const at = (activeTournament && typeof activeTournament === 'object') ? activeTournament : null;
    const fig = structureEligible(at, heartbeat)
      ? this._buildLiveFigure(at, heartbeat, activeRuns) : null;
    if (!fig) {
      // no LIVE topology for the CURRENT run — drop any prior figure, show the
      // honest progress state (never a stale/foreign topology).
      if (this._funnelDigest !== 'none') {
        this._funnelDigest = 'none';
        clear(this._funnelHost);
        this._funnelHost.appendChild(el('p', { class: 'dn-faint dt-live-hero-nofunnel', text: 'the field fills in as the first round runs…' }));
      }
      return;
    }
    const { node, digest } = fig;
    if (digest === this._funnelDigest && this._funnelHost.firstChild) return; // no real change → no DOM, no flash.
    this._funnelDigest = digest;
    clear(this._funnelHost);
    // a one-shot entrance class lets CSS ease the new figure in (reduced-motion
    // suppresses it) — never an infinite/repaint loop.
    if (node.classList) node.classList.add('dt-live-enter');
    this._funnelHost.appendChild(node);
  }

  // Build the live structure figure (racing funnel / swiss ladder / elim bracket)
  // + its digest; the epoch gen scope is the live field. Null when no topology yet.
  _buildLiveFigure(at, heartbeat, activeRuns) {
    const structure = String(at.structure || '');
    const epochGens = (Array.isArray(at.competitors) ? at.competitors : [])
      .map((c) => c && c.generation_id).filter((g) => g != null).map(String);
    const gens = epochGens.length ? epochGens : null;
    const onCompetitor = this.onCompetitor || undefined;

    if (structure === 'racing') {
      const model = racingModel(normalizeStructure(at, true));
      if (!model || !model.hasRungs) return null;
      const node = survivalFunnel({
        rungs: model.rungs, championId: model.championId, benchmarkId: model.benchmarkId,
        live: model.live, gateState: model.gateState, gateDelta: model.gateDelta, onCompetitor,
      });
      return { node, digest: 'racing|' + structDigest(model.rungs, model) };
    }
    if (structure === 'swiss') {
      const st = buildLiveSwissModel({ at, heartbeat, activeRuns, epochGens: gens }) || normalizeStructure(at, true);
      const model = swissModel(st);
      if (!model || !model.hasRounds) return null;
      const node = swissLadder({
        rounds: model.rounds, standings: model.standings,
        championId: model.championId, benchmarkId: model.benchmarkId,
        live: model.live, gateState: model.gateState, gateDelta: model.gateDelta, onCompetitor,
      });
      return { node, digest: 'swiss|' + swissDigest(model) };
    }
    if (structure === 'single_elim' || structure === 'double_elim') {
      const st = buildLiveElimModel({ at, heartbeat, activeRuns, epochGens: gens }) || normalizeStructure(at, true);
      const model = elimModel(st);
      if (!model || !model.hasMatches) return null;
      const node = elimBracket({
        winners: model.winners, losers: model.losers,
        championId: model.championId, benchmarkId: model.benchmarkId,
        live: model.live, gateState: model.gateState, gateDelta: model.gateDelta,
        onCompetitor, onMatch: onCompetitor ? (m) => { const g = m.winner || (Array.isArray(m.competitors) && m.competitors[0]); if (g) onCompetitor(String(g)); } : undefined,
      });
      return { node, digest: 'elim|' + elimDigest(model) };
    }
    return null;
  }
}

function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

// ── the structure-figure eligibility gate ────────────────────────────
//
// A LIVE structure figure (racing funnel / swiss ladder / elim bracket) renders
// iff: (1) there is an active tournament, (2) its `structure` is one we draw,
// (3) its `phase` is RUNNING (not completed/done/idle — a settled tournament
// keeps its topology but must not show the LIVE figure), and (4) it is scoped to
// the CURRENT live epoch (its `epoch_id` matches the heartbeat's). A stale/foreign
// tournament from a prior epoch is rejected here. Epoch scoping is permissive
// only when neither side names an epoch; a known-and-different pair is rejected.
function structureEligible(at, heartbeat) {
  if (!at || typeof at !== 'object') return false;
  const s = String(at.structure);
  if (s !== 'racing' && s !== 'swiss' && s !== 'single_elim' && s !== 'double_elim') return false;
  const phase = String(at.phase == null ? '' : at.phase).trim().toLowerCase();
  const running = phase === 'running' || (phase !== '' && phase !== 'idle'
    && phase !== 'complete' && phase !== 'completed' && phase !== 'done');
  if (!running) return false;
  return liveBelongsToEpoch(at, heartbeat);
}

// The active-tournament belongs to the current run iff its epoch matches the
// heartbeat's epoch. When EITHER side carries no epoch_id we cannot prove the
// tournament is foreign, so we keep it (legacy single-epoch tolerance); a pair
// of KNOWN-and-DIFFERENT epoch_ids is always rejected.
function liveBelongsToEpoch(at, heartbeat) {
  const tEpoch = (at && at.epoch_id != null && String(at.epoch_id) !== '') ? String(at.epoch_id) : null;
  const hbEpoch = (heartbeat && heartbeat.epoch_id != null && String(heartbeat.epoch_id) !== '') ? String(heartbeat.epoch_id) : null;
  if (tEpoch == null || hbEpoch == null) return true;
  return tEpoch === hbEpoch;
}

// stable digests of the structure-relevant model so the swap fires only on a
// real change (a steady heartbeat writes ZERO DOM): each captures the gate + the
// per-rung/round/match progress (a board landing fires it; a no-op stays equal).
function structDigest(rungs, model) {
  return JSON.stringify({
    b: model.benchmarkId || null, c: model.championId || null, g: model.gateState || null,
    r: (rungs || []).map((r) => [r.match_id, (r.competitors || []).join('/'), (r.survivors || []).join('/'), (r.cut || []).join('/'), r.pending,
      r.live_progress ? Object.keys(r.live_progress).sort().map((k) => { const p = r.live_progress[k]; return k + ':' + (p.done || 0) + '/' + (p.total == null ? '?' : p.total) + ':' + (p.inflight || 0); }).join(',') : '']),
  });
}
function swissDigest(model) {
  return JSON.stringify({
    b: model.benchmarkId || null, c: model.championId || null, g: model.gateState || null,
    s: (model.standings || []).map((s) => [s.id, s.points, s.wins, s.draws, s.losses, s.rank]),
    r: (model.rounds || []).map((r) => [r.label, r.queued,
      (r.pairings || []).map((p) => [p.a, p.b, p.winner, p.bye, p.pending, p.done, p.total, p.inflight])]),
  });
}
function elimDigest(model) {
  const band = (rs) => (rs || []).map((r) => [r.label, r.queued,
    (r.matches || []).map((m) => [m.match_id, m.bracket_slot, (m.competitors || []).join('/'), m.winner, m.decision, m.bye, m.pending, m.done, m.total, m.inflight, m.queued])]);
  return JSON.stringify({
    b: model.benchmarkId || null, c: model.championId || null, g: model.gateState || null,
    w: band(model.winners), l: band(model.losers),
  });
}
