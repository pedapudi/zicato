// js/live.js — the LIVE-RUN engine: make a run feel ALIVE without
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

import { el, patchText, patchClass } from './core/dom.js';
import {
  isNum, swissLadder, elimFlow,
  racingScalarTrack, racingScalarTrackDigest,
  elimRadial, elimRadialDigest,
  gauntletFieldBars,
  proposingTracker, proposingDigest, CROWN,
} from './svg.js';
import { fieldStatus as readFieldStatus } from './data.js';
import { gatedSwap } from './ui.js';
import { runStateLabel, LIVENESS, livenessBandText } from './livestatus.js';
import {
  racingModel, swissModel, elimModel, gauntletModel, gauntletModelDigest, normalizeStructure,
  buildLiveRacingModel, buildLiveSwissModel, buildLiveElimModel, buildLiveModel,
  liveMatchBlocks, liveMatchBlocksDigest,
} from './views/structure.js';

// ── tournament-level progress ────────────────────────────────────────
//
// Derive a tournament-level progress indicator from the LIVE active-tournament
// topology + the heartbeat phase. Racing speaks RUNGS (k of N) + per-rung
// matchups (m/n); the elimination/swiss structures speak ROUNDS. Returns a
// plain verdict the hero + the chrome can render:
//
//   { kind, label, detail, fraction, stepIndex, stepCount, fieldN }
//
// `label` is the headline ("rung 2 of 3"); `detail` is the matchup tally
// ("m/n matchups" / "field of N"); `fraction` drives a determinate bar.
//
// `stepIndex` / `stepCount` are the 1-INDEXED "N of M" the hero header + the rung
// stepper BOTH read — the ONE rung-number source of truth (so the header label
// and the stepper can never contradict each other, and never disagree with the
// 0-indexed raw phase string). `stepIndex` is the current rung/round (1..M),
// `stepCount` is the total (M); `fieldN` is the current step's field size.
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
        stepIndex: cur + 1, stepCount: total, fieldN,
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
      stepIndex: cur + 1, stepCount: total, fieldN: ms.length,
    };
  }

  // no topology yet — fall back to the phase string (e.g. "proposing field").
  if (phase) {
    return { kind: 'phase', label: String(phase).split(':').slice(0, 2).join(' · ').replace(/_/g, ' '), detail: '', fraction: null, stepIndex: null, stepCount: null, fieldN: 0 };
  }
  return { kind: 'idle', label: '', detail: '', fraction: null, stepIndex: null, stepCount: null, fieldN: 0 };
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
      key: String(r.run_id || (String(r.generation_id || '') + '|' + String(r.entry_id || ''))),
      gen: r.generation_id || null,
      entry: r.entry_id || null,
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
        if (dec.includes('promot')) push('gate', 'champion-gate · ' + label(cur.winner) + ' promoted ' + CROWN.current, 'good', cur.winner);
        else push('gate', 'champion-gate · champion stands', 'neutral', cur.winner);
      }
      continue;
    }
    if (newlyCut.length) push('cut', 'rung cut · ' + newlyCut.map(label).join(', ') + ' eliminated ✕', 'bad', newlyCut[0]);
    if (newlySurv.length && was.survivors.length) push('survive', 'rung · ' + newlySurv.map(label).join(', ') + ' survive ↑', 'good', newlySurv[0]);
  }

  // a promotion confirmed via lineage growth.
  if (prev && next.lineageLen > prev.lineageLen) push('promote', 'promotion · the lineage advanced ' + CROWN.current, 'good');

  // newest-first.
  out.reverse();
  return { events: out, seq: n };
}

function humanPhase(p) {
  // The heartbeat phase counts EVOLVE-LOOP rounds (the env › epoch › round ›
  // generation hierarchy), which are DISTINCT from the in-tournament BRACKET
  // rounds (WB R0 / LB R1 / GF) shown in the bracket figure. In this flat
  // activity context — with no epoch node to nest under — spell the evolve round
  // as "epoch round N" so the two never read as the same "round".
  return String(p || '')
    .replace(/(^|:)after_round_(\d+)/g, '$1after epoch round $2')
    .replace(/(^|:)round_(\d+)/g, '$1epoch round $2')
    .replace(/:/g, ' · ')
    .replace(/_/g, ' ');
}
function label(s) { return s == null ? '—' : String(s); }

// the structure word for the metadata baseline (racing / single elim / …).
function prettyStructureName(structure) {
  const s = String(structure || '').toLowerCase();
  switch (s) {
    case 'single_elim': return 'single elim';
    case 'double_elim': return 'double elim';
    case '': return '';
    default: return s.replace(/_/g, ' ');
  }
}

// ── the RUNG STEPPER (the structural progress cap on the race track) ──
//
// Replaces the anonymous full-width percentage bar: ONE pip per rung/round, the
// completed ones FILLED (muted ink — they are settled history), the CURRENT one
// ACTIVE (accent green — alive/leading), the not-yet-reached ones HOLLOW. So
// progress reads STRUCTURALLY ("rung 2 of 2" = the second of two pips active),
// not as a faceless 73%. Pure: builds detached DOM; the stepIndex/stepCount come
// from liveProgress (the ONE rung-number source), so the stepper can never
// disagree with the header's rung label.
//
// COLOR ROLES (the hero's fixed palette): accent green = the active rung
// (alive/leading); muted ink = a completed rung (settled history) + the rail.
// No caution/bad here — a cut/eliminated competitor reads on the track, not on
// the stepper (the stepper is structural, not a verdict).
function rungStepper({ stepIndex, stepCount } = {}) {
  const n = (stepCount != null && stepCount > 0) ? stepCount : 0;
  const cur = (stepIndex != null && stepIndex > 0) ? stepIndex : 0;
  const wrap = el('div', { class: 'dt-rungstep', role: 'img',
    'aria-label': n > 0 ? `rung ${cur} of ${n}` : 'rung progress' });
  if (n <= 0) return wrap;
  for (let i = 1; i <= n; i++) {
    const done = i < cur;
    const active = i === cur;
    const cls = 'dt-rungstep-pip'
      + (done ? ' dt-rungstep-done' : '')
      + (active ? ' dt-rungstep-active' : '');
    wrap.appendChild(el('span', { class: cls, 'aria-hidden': 'true' }));
  }
  return wrap;
}
// the stepper digest — changes only when the structural position moves, so the
// digest-gated cap repaints on a real rung advance, never on a no-op heartbeat.
function rungStepperDigest({ stepIndex, stepCount } = {}) {
  return 'step|' + (stepIndex == null ? '?' : stepIndex) + '/' + (stepCount == null ? '?' : stepCount);
}

// ── the ROUND-PIPELINE stepper (WS4-A item 4) ─────────────────────────
//
// A compact propose → apply → run → gate stepper rendering the SERVER's
// authoritative /api/live/pipeline projection VERBATIM — the reader owns
// the phase-string inference; this builder never re-derives loop position
// from phase tokens. One pip+label per step (done = filled/muted, active =
// accent, pending = hollow), the active step's detail beside it, and the
// gate decision word once the round settles. An `epoch_open_step` (the A/A
// noise-floor calibration) leads the strip as the active element while the four
// pipeline steps sit pending — that stretch is real work, not a stalled round.
// Pure: builds detached DOM.
export function pipelineStepper(pipe) {
  const steps = (pipe && Array.isArray(pipe.steps)) ? pipe.steps : [];
  const open = (pipe && pipe.epoch_open_step) ? pipe.epoch_open_step : null;
  const wrap = el('div', { class: 'dt-pipe', role: 'img', 'aria-label': 'round pipeline' });
  if (open && open.id) {
    wrap.appendChild(el('span', { class: 'dt-pipe-step dt-pipe-active', 'data-step': String(open.id) }, [
      el('span', { class: 'dt-pipe-dot', 'aria-hidden': 'true' }),
      el('span', { class: 'dt-pipe-label', text: open.label || open.id }),
      open.detail ? el('span', { class: 'dt-pipe-detail dn-faint', text: open.detail }) : null,
    ].filter(Boolean)));
  }
  steps.forEach((s, i) => {
    if (!s || !s.id) return;
    const state = (s.state === 'done' || s.state === 'active') ? s.state : 'pending';
    if (i > 0 || open) wrap.appendChild(el('span', { class: 'dt-pipe-sep', 'aria-hidden': 'true', text: '→' }));
    const node = el('span', { class: 'dt-pipe-step dt-pipe-' + state, 'data-step': String(s.id) }, [
      el('span', { class: 'dt-pipe-dot', 'aria-hidden': 'true' }),
      el('span', { class: 'dt-pipe-label', text: s.label || s.id }),
      (state === 'active' && s.detail)
        ? el('span', { class: 'dt-pipe-detail dn-faint', text: s.detail }) : null,
    ].filter(Boolean));
    wrap.appendChild(node);
  });
  if (pipe && pipe.decision) {
    wrap.appendChild(el('span', {
      class: 'dt-pipe-decision' + (pipe.decision === 'promoted' ? ' dn-good-t' : ' dn-faint'),
      text: '· ' + pipe.decision,
    }));
  }
  return wrap;
}

// The stepper digest: step states + the active detail + the decision — a
// steady heartbeat re-serving the same projection is byte-identical (zero
// DOM); a step advancing / a detail moving / the verdict landing flips it.
export function pipelineStepperDigest(pipe) {
  if (!pipe || !Array.isArray(pipe.steps)) return 'none';
  const open = pipe.epoch_open_step;
  return 'pipe|' + pipe.steps.map((s) => [
    s && s.id, s && s.state, (s && s.state === 'active' && s.detail) ? s.detail : '',
  ].join(':')).join('|') + '|' + (open ? [open.id, open.detail || ''].join(':') : '')
    + '|' + (pipe.decision || '') + '|' + (pipe.running ? 'R' : '-');
}

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

  // Clear EVERY row + the dedup memory (the alive→idle transition). Without
  // this the finished run's rows persist in `_list`/`_seen` and the NEXT run's
  // events prepend on top of the dead ones — the idle-leak. The empty
  // placeholder is restored so the feed reads "waiting for activity…" again.
  reset() {
    while (this._list.firstChild) this._list.removeChild(this._list.firstChild);
    this._count = 0;
    this._seen.clear();
    if (this._empty && !this._empty.parentNode) this.node.insertBefore(this._empty, this._list);
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
    case 'gate': return CROWN.current;
    case 'promote': return CROWN.current;
    case 'run': return '✓';
    case 'matchup': return '▸';
    case 'phase': return '·';
    default: return '·';
  }
}

// ── the MATCH-GROUPED "what's running" block (Task 1) ────────────────
//
// One DOM block per in-flight match, each board entry showing a live progress
// bar (animated via CSS width) + an outcome glyph once it settles. The bars are
// set via style.setProperty so a re-render is not required to advance them — but
// the host is digest-gated on the live CONTENT (the bucketed progress) so the
// DOM is rebuilt only on a real bucket change, never on a no-op heartbeat. This
// complements (does not replace) the "N units running" count + the activity
// ticker: the ticker is the STREAM, this is the STATE.
//
// `blocks` is liveMatchBlocks(model)'s output; `onCompetitor(id)` opens a
// candidate. Pure: returns a detached node.
function blockOutcomeGlyph(outcome) {
  switch (outcome) {
    case 'win': return '✓';
    case 'loss': return '✗';
    case 'timeout': return '⏱';
    case 'queued': return '·';
    default: return '';
  }
}

export function liveMatchGroupedBlocks(blocks, onCompetitor, ctl) {
  const list = Array.isArray(blocks) ? blocks : [];
  const wrap = el('div', { class: 'dt-live-matches' });
  if (!list.length) {
    wrap.appendChild(el('p', { class: 'dn-faint dt-live-matches-empty', text: 'no matches in flight right now…' }));
    return wrap;
  }
  for (const b of list) {
    const block = el('div', { class: 'dt-live-match dt-live-match-' + (b.kind || 'pair'), 'data-match': b.match_id || '' });
    block.appendChild(el('div', { class: 'dt-live-match-head', text: b.label || (b.match_id || 'match') }));
    const rows = el('div', { class: 'dt-live-match-rows' });
    for (const e of (Array.isArray(b.entries) ? b.entries : [])) {
      rows.appendChild(liveMatchRow(e, onCompetitor, ctl));
    }
    block.appendChild(rows);
    wrap.appendChild(block);
  }
  return wrap;
}

// ── the per-run KILL affordance (WS4-A item 3d) ──────────────────────
//
// One small ✕ per IN-FLIGHT run on a competitor's row, posting the file-based
// kill marker (postControl('kill/'+runId) at the call site). Confirm-on-click:
// the first click ARMS ("kill?"), the second fires; an armed button auto-
// disarms. Hidden entirely when the workspace is read-only (the caller passes
// no ctl / canControl:false). Clicks never bubble into the row's competitor
// navigation. Exported for the node behaviour tests.
export function killRunButton(runInfo, onKill) {
  const runId = String((runInfo && runInfo.run_id) || '');
  const entry = (runInfo && runInfo.entry_id) ? String(runInfo.entry_id) : '';
  const btn = el('button', {
    class: 'dt-live-kill', type: 'button', 'data-run': runId,
    title: 'kill run ' + runId + (entry ? ' (' + entry + ')' : '') + ' — the supervisor tears the unit down',
    'aria-label': 'kill run ' + runId,
    text: '✕',
  });
  let armed = false;
  let timer = null;
  const disarm = () => {
    armed = false;
    if (timer != null) { clearTimeout(timer); timer = null; }
    patchText(btn, '✕');
    btn.classList.remove('dt-live-kill-armed');
  };
  btn.addEventListener('click', (ev) => {
    if (ev && ev.stopPropagation) ev.stopPropagation();
    if (!armed) {
      armed = true;
      patchText(btn, 'kill?');
      btn.classList.add('dt-live-kill-armed');
      timer = setTimeout(disarm, 4000);
      return;
    }
    disarm();
    if (onKill && runId) onKill(runId);
  });
  return btn;
}

// One "follow" affordance for one in-flight run — opens that unit's live
// conversation pane (issue #194 §2). No confirm step: reading a conversation is
// harmless and reversible, unlike the kill beside it. The click never bubbles
// into the row's competitor navigation, which would take the operator to the
// candidate page instead. Exported for the node behaviour tests.
export function followRunButton(gen, runInfo, onFollow) {
  const entry = String((runInfo && runInfo.entry_id) || '');
  const runId = (runInfo && runInfo.run_id) ? String(runInfo.run_id) : null;
  const btn = el('button', {
    class: 'dt-live-follow', type: 'button', 'data-follow-run': entry,
    title: 'follow the live conversation for ' + gen + ' on ' + entry,
    'aria-label': 'follow live conversation for ' + gen + ' on ' + entry,
    text: '💬',
  });
  btn.addEventListener('click', (ev) => {
    if (ev && ev.stopPropagation) ev.stopPropagation();
    onFollow(gen, entry, runId);
  });
  return btn;
}

// The in-flight runs that belong to each competitor generation, epoch-guarded
// exactly like tournamentHasActiveRuns (a KNOWN-and-different epoch run never
// lights up a stale tournament). Returns { '<gen>': [{run_id, entry_id}] }.
export function runsByGeneration(activeRuns, at) {
  const out = {};
  const runs = Array.isArray(activeRuns) ? activeRuns : [];
  const tEpoch = (at && at.epoch_id != null && String(at.epoch_id) !== '') ? String(at.epoch_id) : null;
  for (const r of runs) {
    if (!r || typeof r !== 'object' || !r.run_id) continue;
    const rEpoch = (r.epoch_id != null && String(r.epoch_id) !== '') ? String(r.epoch_id) : null;
    if (tEpoch != null && rEpoch != null && rEpoch !== tEpoch) continue;
    const gen = r.generation_id != null ? r.generation_id : r.gen;
    if (gen == null || String(gen) === '') continue;
    const key = String(gen);
    (out[key] = out[key] || []).push({
      run_id: String(r.run_id),
      entry_id: r.entry_id != null ? String(r.entry_id) : null,
    });
  }
  return out;
}

// ── ONE dense competitor row — fixed columns, no far-right floating ──
//
// Each competitor (champion / challenger / a rung lane) is ONE aligned row with
// a fixed left-to-right column order that fills the hero's full width:
//
//   [ vN ] [ ▓▓▓░░ progress bar — the width-filling element ] [ ~proj scalar ]
//   [ k/N boards ] [ PROJ tag ]
//
// The bar is the only flexible column (`1fr`) so it eats the freed middle space;
// every other column is content-sized and right-anchored AS A COLUMN — nothing
// floats against the card edge, and the boards-done `k/N` is a first-class
// column (never the clipped trailing state glyph it used to be). When a side has
// no projection the scalar column is a faint placeholder so the grid stays
// aligned across rows; once a side SETTLES the trailing tag carries its verdict
// glyph (✓/✗/⏱) instead of PROJ, and the boards column reads its final k/N.
function liveMatchRow(e, onCompetitor, ctl) {
  const queued = e.outcome === 'queued';
  const settled = e.outcome === 'win' || e.outcome === 'loss' || e.outcome === 'timeout';
  // PROJECTED — an in-flight side with a server-side projected scalar reads
  // "~proj" (dimmed/dashed) so the hero distinguishes a climbing projection
  // from a settled verdict.
  const proj = !!(e.projected && isNum(e.projected_scalar) && !settled);
  // the per-run KILL cluster (writable workspace + attributed in-flight runs).
  const killRuns = (ctl && ctl.canControl && Array.isArray(e.runs)) ? e.runs.filter((r) => r && r.run_id) : [];
  const row = el('div', {
    class: 'dt-live-match-row' + (proj ? ' dt-proj' : '') + (killRuns.length ? ' dt-live-match-row-kill' : ''),
    tabindex: onCompetitor ? '0' : null,
  });

  // 1 — the competitor id (fixed-width mono column).
  const name = el('span', { class: 'dt-live-match-name dn-mono', text: String(e.id) });

  // 2 — the progress bar: the width-filling element (the only flexible column).
  const fill = el('span', { class: 'dt-live-match-fill' + (e.inflight ? ' dt-live-match-fill-live' : '') });
  const pct = isNum(e.ratio) ? Math.round(Math.max(0, Math.min(1, e.ratio)) * 100) : (e.inflight ? 50 : 0);
  fill.style.setProperty('width', pct + '%');
  const bar = el('span', { class: 'dt-live-match-bar' + (queued ? ' dt-live-match-bar-queued' : ''), 'aria-hidden': 'true' }, [fill]);

  // 3 — the ~projected scalar column (faint placeholder when none, so the grid
  // column stays aligned across rows).
  const scalar = proj
    ? el('span', { class: 'dt-live-match-scalar dt-proj-val dn-mono', title: 'projected — boards still streaming',
        text: '~' + (Math.round(e.projected_scalar * 10) / 10) })
    : el('span', { class: 'dt-live-match-scalar dn-faint dn-mono', 'aria-hidden': 'true', text: '·' });

  // 4 — the boards-done k/N column (FIRST-CLASS, never clipped). Prefer the
  // explicit boards_done/boards_total a projection carries; else the per-match
  // done/total tally. A faint placeholder keeps the column aligned when neither
  // is known yet.
  const bDone = isNum(e.boards_done) ? e.boards_done : (isNum(e.done) ? e.done : null);
  const bTotal = isNum(e.boards_total) ? e.boards_total : (isNum(e.total) && e.total > 0 ? e.total : null);
  const boardsText = (bTotal != null) ? `${bDone == null ? 0 : bDone}/${bTotal}`
    : (bDone != null ? String(bDone) : '·');
  const boards = el('span', { class: 'dt-live-match-boards dn-mono' + (bTotal == null && !isNum(bDone) ? ' dn-faint' : ''),
    title: 'boards done', text: boardsText });

  // 5 — the trailing tag column: PROJ while projecting, the settled verdict
  // glyph (✓/✗/⏱) once decided, "queued" before the side starts.
  let tag;
  if (proj) {
    tag = el('span', { class: 'dt-live-match-tag dt-proj-badge', text: 'PROJ' });
  } else if (settled) {
    tag = el('span', { class: 'dt-live-match-tag dt-live-match-state' + (e.outcome === 'win' ? ' dn-good' : e.outcome === 'loss' ? ' dn-bad' : ''),
      text: blockOutcomeGlyph(e.outcome) });
  } else if (queued) {
    tag = el('span', { class: 'dt-live-match-tag dt-live-match-state dn-faint', text: 'queued' });
  } else {
    tag = el('span', { class: 'dt-live-match-tag dt-live-match-state dn-faint', text: 'live' });
  }

  row.appendChild(name);
  row.appendChild(bar);
  row.appendChild(scalar);
  row.appendChild(boards);
  row.appendChild(tag);
  // THE FOLLOW AFFORDANCE (issue #194 §2). One per in-flight run on this
  // competitor: the operator can see a unit is running here, so this is where
  // they should be able to open its conversation. Offered ONLY while the run
  // is in flight — a settled unit's transcript is reached through its board,
  // and offering "follow" against a dead loop is the §1 lie in miniature.
  const followRuns = (ctl && typeof ctl.onFollow === 'function' && !settled && Array.isArray(e.runs))
    ? e.runs.filter((r) => r && r.entry_id) : [];
  if (followRuns.length) {
    const cluster = el('span', { class: 'dt-live-follow-cluster' });
    for (const r of followRuns) cluster.appendChild(followRunButton(String(e.id), r, ctl.onFollow));
    row.appendChild(cluster);
  }
  if (killRuns.length) {
    const cluster = el('span', { class: 'dt-live-kill-cluster' });
    for (const r of killRuns) cluster.appendChild(killRunButton(r, ctl.onKill));
    row.appendChild(cluster);
  }
  if (onCompetitor && e.id && e.id !== 'tbd') {
    row.addEventListener('click', () => onCompetitor(String(e.id)));
    row.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onCompetitor(String(e.id)); } });
  }
  return row;
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
// THE DEMOTION (issue #194 §1). The hero used to be the top ~45% of every
// view, and it appeared whenever the runtime FILES looked busy — so a
// workspace dead since June opened every page with a breathing LIVE pill and
// seven units "running". It is now two separate things:
//
//   * a ONE-LINE STATUS BAND, always present, that speaks in whatever tense
//     is true: `● LIVE · racing · rung 1 · 7 units` while live,
//     `last run · Jun 8 · interrupted mid-round` when it is not;
//   * the FULL LIVE SURFACE (race track, what's running, live activity) as a
//     drawer beneath it that EXISTS ONLY while liveness is `live`, and which
//     the operator can collapse to the band alone.
//
// The controller is structure-agnostic — it leans on racing's funnel where a
// racing topology is live, and degrades to the progress + ticker for any
// other structure.
const BAND_COLLAPSE_KEY = 'zicato.live.collapsed';

export class LiveController {
  constructor(opts) {
    const o = opts || {};
    // The operator's drawer preference, remembered across navigation and
    // reloads. Default OPEN: a run in flight is what the operator asked for.
    this._collapsed = readCollapsed();
    this._bandKey = null;
    this.onCompetitor = typeof o.onCompetitor === 'function' ? o.onCompetitor : null;
    // the per-run kill sink (postControl('kill/'+runId) at the shell); null
    // (or canControl:false on update) hides every kill affordance.
    this.onKill = typeof o.onKill === 'function' ? o.onKill : null;
    // the per-run FOLLOW sink (issue #194 §2) — navigates to the unit's live
    // conversation pane. Read-only, so it is NOT gated on canControl.
    this.onFollow = typeof o.onFollow === 'function' ? o.onFollow : null;
    this._canControl = false;
    this._seq = 0;
    this._prevSnap = null;
    // tracks the alive→idle edge so the ticker is cleared exactly once on the
    // transition (not every idle beat).
    this._wasAlive = false;
    // the run-key set from the last ALIVE tick — the "pre-idle" set the idle→
    // alive edge compares against so a flap of the SAME runs keeps the feed.
    this._preIdleRuns = null;
    this._funnelDigest = null;
    this._metaKey = null;
    this._pipeDigest = null;
    this.ticker = new ActivityTicker({ cap: o.cap || 40 });
    this._build();
  }

  _build() {
    // ── 0. THE STATUS BAND — one line, on every view, in every state ────
    // The only part of the hero that is unconditional. Its text is the
    // tri-state sentence (livestatus.livenessBandText), so a dead workspace
    // reads `last run · Jun 8 · interrupted mid-round` here and nothing
    // else on the page claims otherwise. The drawer toggle appears only
    // when there IS a drawer (i.e. only when live).
    this._bandDot = el('span', { class: 'dt-live-band-dot', 'aria-hidden': 'true' });
    this._bandText = el('span', { class: 'dt-live-band-text', text: '' });
    this._bandToggle = el('button', {
      class: 'dt-live-band-toggle', type: 'button', text: '',
      'aria-expanded': 'false',
    });
    this._bandToggle.addEventListener('click', () => {
      this._collapsed = !this._collapsed;
      writeCollapsed(this._collapsed);
      this._applyDrawer(this.node.classList.contains('dt-live-live'));
    });
    this._band = el('div', { class: 'dt-live-band' }, [
      this._bandDot, this._bandText, this._bandToggle,
    ]);

    // ── 1. THE STATUS HEADER — ONE muted metadata baseline ──────────────
    // `● LIVE · racing · rung 2 of 2 · field of 2 · 7 units running`. NO
    // competing big phase title; every token reads in the same muted mono type.
    // The rung label here is the SAME 1-indexed "N of M" the rung stepper reads
    // (liveProgress.stepIndex/stepCount) — never the 0-indexed raw phase string —
    // so the header and the stepper can never contradict ("rung 0" vs "rung 2 of
    // 2" was the bug: the title read the phase string, the subline read topology).
    // The pill carries the SAME four-state liveness word the chrome reads
    // (LIVE / STALLED), keyed off the derived run-state — NOT a hard-coded
    // "LIVE". This is the hero's single liveness read; the phase rides in
    // `_meta` beside it (so the two never duplicate a bare "LIVE").
    this._pillText = el('span', { class: 'dt-live-hero-pilltext', text: 'LIVE' });
    this._pill = el('span', { class: 'dt-live-hero-pill' }, [
      el('span', { class: 'dt-live-hero-dot', 'aria-hidden': 'true' }),
      this._pillText,
    ]);
    this._meta = el('span', { class: 'dt-live-hero-meta', text: '' });
    // the ROUND-PIPELINE stepper host (propose→apply→run→gate) — filled by
    // updatePipeline() with the server's authoritative projection, digest-
    // gated; empty (and invisible) when the endpoint is absent (Rust
    // supervisor) or the pipeline is idle.
    this._pipeHost = el('span', { class: 'dt-live-hero-pipe' });
    const head = el('div', { class: 'dt-live-hero-head' }, [this._pill, this._meta, this._pipeHost]);

    // ── 2. THE RACE STATE — the PRIMARY, FULL-WIDTH viz ─────────────────
    // The scalar number-line fills the hero width (it IS the hero). A compact
    // rung STEPPER caps its left edge as the structural progress annotation
    // (one pip per rung; completed filled, current active) — replacing the
    // anonymous full-width percentage bar. Both are digest-gated swaps.
    this._stepHost = el('div', { class: 'dt-live-hero-step' });
    this._trackHost = el('div', { class: 'dt-live-hero-track dn-figpane' });
    // `_funnelHost` is kept as an ALIAS of the track host so the digest-gated
    // figure machinery (and the existing tests) keep their handle on it.
    this._funnelHost = this._trackHost;
    const race = el('div', { class: 'dt-live-hero-race' }, [this._stepHost, this._trackHost]);

    // ── 3. THE DETAIL ROW — two BALANCED columns, shared panel chrome ────
    // LEFT: the densified champion-gate "what's running" rows. RIGHT: the live
    // activity log. Equal visual weight (≈55/45), aligned tops, one eyebrow style.
    this._matchesHost = el('section', { class: 'dt-live-hero-panel dt-live-hero-matches' }, [
      el('div', { class: 'dt-live-hero-eyebrow', text: 'what’s running' }),
      el('div', { class: 'dt-live-hero-matchesbody' }),
    ]);
    this._matchesBody = this._matchesHost.childNodes[1];

    this._tickerHost = el('section', { class: 'dt-live-hero-panel dt-live-hero-ticker' }, [
      el('div', { class: 'dt-live-hero-eyebrow', text: 'live activity' }),
      this.ticker.node,
    ]);

    const detail = el('div', { class: 'dt-live-hero-detail' }, [this._matchesHost, this._tickerHost]);

    // reading order: status baseline → the full-width race → the balanced detail.
    this._body = el('div', { class: 'dt-live-hero-body' }, [head, race, detail]);
    this.node = el('section', { class: 'dt-live-hero', 'aria-label': 'Live run', role: 'region' }, [this._band, this._body]);
  }

  // Paint the always-present status band from the tri-state verdict, and
  // show/hide the drawer + its toggle. Digest-gated on the sentence itself
  // plus the two booleans, so a steady heartbeat writes ZERO DOM.
  _updateBand(liveness, status) {
    const lv = liveness || { state: LIVENESS.LIVE, live: true, endedAt: null };
    const text = livenessBandText(lv, status);
    const key = lv.state + '|' + text + '|' + (this._collapsed ? 'c' : 'o');
    if (key === this._bandKey) return;
    this._bandKey = key;
    patchText(this._bandText, text);
    patchClass(this._band, 'dt-live-band-live', lv.state === LIVENESS.LIVE);
    patchClass(this._band, 'dt-live-band-interrupted', lv.state === LIVENESS.INTERRUPTED);
    // The toggle is meaningless without a drawer to toggle.
    patchText(this._bandToggle, lv.live ? (this._collapsed ? '▸ details' : '▾ details') : '');
    this._bandToggle.setAttribute('aria-expanded', lv.live && !this._collapsed ? 'true' : 'false');
    patchClass(this._bandToggle, 'dt-live-band-toggle-on', !!lv.live);
  }

  // The drawer exists ONLY while live, and only while the operator has not
  // collapsed it. `dt-live-live` records liveness itself (the band reads it
  // back on a toggle click); `dt-live-on` is what CSS shows the body on.
  _applyDrawer(live) {
    patchClass(this.node, 'dt-live-live', !!live);
    patchClass(this.node, 'dt-live-on', !!live && !this._collapsed);
  }

  // Drive the hero from the current live state. Returns true when a run is live
  // (so the shell can toggle the hero's visibility class).
  update({ status, heartbeat, activeRuns, activeTournament, canControl, liveness } = {}) {
    // whether the workspace accepts control POSTs (read_only:false) — gates
    // the per-run kill affordances in the "what's running" rows.
    if (canControl != null) this._canControl = !!canControl;
    const running = !!(status && status.running);
    // THE DRAWER GATE is the served tri-state, not file presence: the drawer
    // exists only while liveness reads `live`. A caller that supplies no
    // verdict (the structure fixtures) falls back to the four-state `alive`
    // flag, which is what gated it before.
    const alive = liveness
      ? !!liveness.live
      : !!(status && (status.alive != null ? status.alive : status.running));
    this._updateBand(liveness, status);
    this._applyDrawer(alive);

    // ── the activity ticker: diff the snapshot, append the new events ──
    const snap = liveSnapshot({ status, heartbeat, activeRuns, activeTournament });
    // On the idle→alive edge, clear the PREVIOUS run's dead rows BEFORE this
    // tick's events land — but ONLY when the incoming run set is a genuinely NEW
    // run (disjoint from the pre-idle set). A brief alive→idle→alive FLAP of the
    // SAME still-running runs shares run keys with the pre-idle set, so the feed
    // must survive it rather than momentarily blanking. Clearing here (not on the
    // alive→idle edge) preserves a finishing run's completion events, which land
    // in the same tick it settles.
    if (alive && !this._wasAlive) {
      const runKeys = snap.runs.map((r) => r.key);
      const prior = this._preIdleRuns;
      const carriedOver = !!(prior && runKeys.some((k) => prior.has(k)));
      if (!carriedOver) this.ticker.reset();
    }
    const { events, seq } = deriveActivity(this._prevSnap, snap, this._seq);
    this._seq = seq;
    if (events.length) this.ticker.push(events);
    // KEEP the last snapshot even when idle (do NOT null it). Nulling forced the
    // next alive tick to cold-start deriveActivity, which re-emits EVERY
    // in-flight run as a phantom "matchup running" burst — the second half of
    // the idle-leak. Keeping it means re-activation diffs against the true prior
    // snapshot: genuinely-new runs still surface, already-running ones do not.
    this._prevSnap = snap;
    if (!alive) {
      this._wasAlive = false;
      // _matchesDigest/_stepDigest are gone (those gates now ride gatedSwap's
      // host-attribute digest); the funnel + meta line still use their vars.
      this._funnelDigest = null; this._metaKey = null;
      this.updatePipeline(null);
      // TEAR THE DRAWER DOWN, don't just hide it — but only on an
      // AUTHORITATIVE end. A tri-state verdict of settled/interrupted means
      // the server watched the loop stop; there is then no live chrome worth
      // keeping, and none for a later repaint to resurrect. The legacy
      // `alive` flag (no verdict supplied) still FLICKERS between
      // transitions, and tearing down on that would blank a running feed —
      // so without a verdict we only hide.
      if (liveness) this._teardownDrawer();
      return false;
    }
    this._wasAlive = true;
    // Remember this alive tick's run set. Idle ticks return above without
    // touching it, so at the next idle→alive edge it still holds the pre-idle
    // run set the reset gate compares against.
    this._preIdleRuns = new Set(snap.runs.map((r) => r.key));

    // ── tournament-level progress: the ONE rung-number source of truth ──
    // liveProgress reads the live TOPOLOGY (resolved rungs + the active rung),
    // so its 1-indexed stepIndex/stepCount + label feed BOTH the header metadata
    // line AND the rung stepper — they can never disagree, and never fall back
    // to the contradictory 0-indexed raw phase string.
    const prog = liveProgress({ activeTournament, heartbeat, status });

    // ── 1. the status header — ONE muted metadata baseline (digest-gated) ──
    // `● LIVE · <structure> · rung N of M · field of K · J units running`.
    const inFlight = status && isNum(status.inFlight) ? status.inFlight : (Array.isArray(activeRuns) ? activeRuns.length : 0);
    const structure = (status && status.structure) || (activeTournament && activeTournament.structure) || null;
    const meta = this._metaLine(prog, structure, inFlight);
    // the four-state liveness word for the pill (LIVE / STALLED), defaulting to
    // LIVE when a caller supplies no runState. Folded into the meta digest so a
    // steady tick writes ZERO DOM and a real transition flips word + line.
    const pillWord = (status && status.runState && runStateLabel(status.runState))
      ? runStateLabel(status.runState) : 'LIVE';
    const metaKey = pillWord + ' ' + meta.join('|');
    if (metaKey !== this._metaKey) {
      this._metaKey = metaKey;
      if (this._pillText) patchText(this._pillText, pillWord);
      patchText(this._meta, meta.length ? meta.join(' · ') : '');
    }

    // ── 2. the rung STEPPER caps the race track (digest-gated swap) ──
    // structural progress: one pip per rung, completed filled, current active.
    // the second-idiom digest gate folded onto gatedSwap: the same firstChild +
    // digest-attribute no-flash contract, so a steady tick writes ZERO DOM.
    gatedSwap(this._stepHost, rungStepperDigest(prog),
      () => (prog.stepCount != null && prog.stepCount > 0) ? rungStepper(prog) : null);

    // ── the MATCH-GROUPED "what's running" block: digest-gated on live CONTENT ──
    // (which matches exist + each board's progress BUCKET) so the DOM is rebuilt
    // only on a real change, never on a no-op heartbeat; the bars animate via CSS.
    this._updateMatches(activeTournament, heartbeat, activeRuns);

    // ── the structure FIGURE: digest-gated swap (animate only on real change) ──
    // STRUCTURE-DISPATCHED + CURRENT-RUN-ONLY (see structureEligible): racing →
    // funnel, swiss → live standings ladder, elim → live bracket; anything else /
    // completed / stale-foreign shows the honest placeholder.
    this._updateStructure(activeTournament, heartbeat, activeRuns);
    return true;
  }

  // Empty every drawer host so a not-live hero holds no live chrome. The
  // gatedSwap hosts also lose their digest attribute, so the next live tick
  // rebuilds rather than skipping on a matching digest against an empty host.
  _teardownDrawer() {
    for (const host of [this._stepHost, this._trackHost, this._matchesBody, this._pipeHost]) {
      if (!host) continue;
      clear(host);
      if (host.removeAttribute) host.removeAttribute('data-t-digest');
    }
    this._pipeDigest = null;
    this.ticker.reset();
    patchText(this._meta, '');
  }

  // Drive the round-pipeline stepper from the server's /api/live/pipeline
  // projection. Digest-gated: a steady heartbeat re-serving the same
  // projection writes ZERO DOM; a step advancing repaints. `null` (endpoint
  // absent — the Rust supervisor — or the hero going idle) clears the host,
  // degrading the head to exactly its pre-stepper reading.
  updatePipeline(pipe) {
    if (!this._pipeHost) return;
    // an idle projection (all steps pending, nothing decided, no epoch-open
    // step in flight) reads as no stepper — the meta line already says
    // "proposing …" etc. while filling.
    const idle = !pipe || !Array.isArray(pipe.steps)
      || (!pipe.decision && !pipe.epoch_open_step
        && pipe.steps.every((s) => !s || (s.state !== 'active' && s.state !== 'done')));
    const digest = idle ? 'none' : pipelineStepperDigest(pipe);
    if (digest === this._pipeDigest && (idle || this._pipeHost.firstChild)) return;
    this._pipeDigest = digest;
    clear(this._pipeHost);
    if (idle) return;
    this._pipeHost.appendChild(pipelineStepper(pipe));
  }

  // Build the ONE muted metadata baseline as an ordered token list:
  //   `racing · rung N of M · field of K · J units running`
  // The rung token is liveProgress.label (the SAME 1-indexed "N of M" the rung
  // stepper reads — never the contradictory 0-indexed phase string). When the
  // topology has not minted rungs yet, the phase verdict (e.g. "proposing field")
  // stands in for the rung token. The field tally rides only when it is NOT
  // already implied by the rung label's matchup detail.
  _metaLine(prog, structure, inFlight) {
    const out = [];
    if (structure) out.push(prettyStructureName(structure));
    if (prog.label) out.push(prog.label);
    // the per-step detail ("m/n matchups" / "field of K") — the rung label's
    // companion. Skip the bare "field of K" when there is no rung label (avoids a
    // lone "field of K" with no structural context).
    if (prog.detail && (prog.label || /matchups/.test(prog.detail))) out.push(prog.detail);
    if (inFlight > 0) out.push(inFlight + (inFlight === 1 ? ' unit running' : ' units running'));
    return out;
  }

  // Build the match-grouped "what's running" block from the UNIFIED live model
  // (buildLiveModel — the single source the structure figures use too) and swap
  // it in ONLY when the live-content digest changes. A steady heartbeat with the
  // same matches + the same progress buckets writes ZERO DOM (the bars are
  // CSS-animated). The panel is always present in the balanced detail row; when
  // no match is in flight the body shows its own honest placeholder.
  _updateMatches(activeTournament, heartbeat, activeRuns) {
    const at = (activeTournament && typeof activeTournament === 'object') ? activeTournament : null;
    let blocks = [];
    // Show the in-flight matches when the structure is drawable+running AND
    // corroborated by the epoch gate OR by genuine active runs (ground truth,
    // bumped by per-run beaters) — so a fresh epoch roll that briefly desyncs
    // the heartbeat epoch tag never blanks "what's running" while runs are live.
    if (structureEligible(at, heartbeat)
        || (structureDrawableRunning(at) && tournamentHasActiveRuns(at, activeRuns))) {
      const epochGens = (Array.isArray(at.competitors) ? at.competitors : [])
        .map((c) => c && c.generation_id).filter((g) => g != null).map(String);
      const model = buildLiveModel(at, heartbeat, activeRuns, epochGens.length ? epochGens : null);
      blocks = liveMatchBlocks(model) || [];
    }
    // FALLBACK — the published rounds can ALL be settled between rungs/rounds
    // (every match carries a winner/survivors/cut) while runs are STILL in
    // flight: liveMatchBlocks then returns [] yet "N units running" + the activity
    // ticker read live (they consume activeRuns, not the settled rounds). Rather
    // than falsely show "no matches in flight…", synthesize ONE honest block from
    // the in-flight runs that genuinely BELONG to this tournament. The belonging
    // guard mirrors tournamentHasActiveRuns' epoch+roster check verbatim, so a
    // known-foreign-epoch run never lights up a stale tournament (the FIX 4 gate).
    if (!blocks.length && structureDrawableRunning(at)
        && tournamentHasActiveRuns(at, activeRuns)) {
      const fb = runningEntriesFallbackBlock(at, activeRuns);
      if (fb) blocks = [fb];
    }
    // ANNOTATE each competitor row with its attributed in-flight runs so the
    // per-run kill affordance can render (epoch-guarded — a foreign-epoch run
    // never earns a kill button on a stale tournament). Folded into the digest
    // (alongside the canControl gate) so a run starting/finishing — or the
    // workspace flipping writable — repaints, while a steady beat stays no-op.
    const canKill = !!(this._canControl && this.onKill);
    const byGen = canKill ? runsByGeneration(activeRuns, at) : {};
    if (canKill) {
      for (const b of blocks) {
        for (const e of (Array.isArray(b.entries) ? b.entries : [])) {
          if (e && e.id != null && byGen[String(e.id)]) e.runs = byGen[String(e.id)];
        }
      }
    }
    const runsKey = Object.keys(byGen).sort()
      .map((g) => g + ':' + byGen[g].map((r) => r.run_id).sort().join('+')).join(',');
    // the second-idiom digest gate folded onto gatedSwap (same no-flash contract).
    const digest = liveMatchBlocksDigest(blocks) + '|kill:' + (canKill ? runsKey : '-')
      + '|follow:' + (this.onFollow ? runsKey : '-');
    gatedSwap(this._matchesBody, digest, () => {
      const ctl = (canKill || this.onFollow)
        ? { canControl: canKill, onKill: this.onKill, onFollow: this.onFollow }
        : undefined;
      const node = liveMatchGroupedBlocks(blocks, this.onCompetitor || undefined, ctl);
      if (node.classList) node.classList.add('dt-live-enter');
      return node;
    });
  }

  _updateStructure(activeTournament, heartbeat, activeRuns) {
    const at = (activeTournament && typeof activeTournament === 'object') ? activeTournament : null;
    const fig = structureEligible(at, heartbeat)
      ? this._buildLiveFigure(at, heartbeat, activeRuns) : null;
    if (!fig) {
      // No LIVE topology yet for the CURRENT run. Before falling back to the
      // bland placeholder, render the PROPOSING-STEP TRACKER when the field is
      // being minted — the per-challenger applied/rejected outcomes — so the
      // candidate-generation step is visible (and an all-rejected field reads
      // as "0 applied — all rejected", never an idle/empty state). Scoped to
      // the current epoch (liveBelongsToEpoch) and digest-gated like the figure.
      const prop = this._buildProposingTracker(at, heartbeat);
      if (prop) {
        const { node, digest } = prop;
        if (digest === this._funnelDigest && this._funnelHost.firstChild) return;
        this._funnelDigest = digest;
        clear(this._funnelHost);
        if (node.classList) node.classList.add('dt-live-enter');
        this._funnelHost.appendChild(node);
        return;
      }
      // no field topology AND no proposing field — the honest placeholder.
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

  // Build the FULL-WIDTH LIVE structure figure (the study "hero opt 3"): the SAME
  // final tournament-viz design the single-round page leads with, rendered
  // RESPONSIVE so it scales aspect-locked to fill the hero width up to its
  // `svg.dn-*-hero` max-width cap — every structure matching the racing scalar
  // track's full-width treatment (wide figures fill to their cap). The hero and
  // the full page agree on the model + read consistently.
  //
  //   racing      → racingScalarTrack({ mini, responsive })   (the single-round PRIMARY)
  //   single_elim → elimRadial({ mini, responsive })          (square — centres under its cap)
  //   double_elim → elimFlow combo ({ responsive }, WB/LB bands) (the single-round DEFAULT)
  //   gauntlet    → gauntletFieldBars({ mini, responsive })   (the wave-vs-standard hero)
  //   swiss       → swissLadder({ responsive })               (no mini mode in the builder)
  //
  // The model is reused from views/structure.js (buildLive*Model + the *Model
  // helpers + championScalarOf) so the hero mini stays byte-consistent with the
  // full figure through all four lifecycle states (queued / in-flight via
  // live_progress / projected / settled, converging once settled). The digest the
  // gated swap compares is the NEW builders' own `*Digest` (or the structure.js
  // model digest), so a real content change repaints and a no-op heartbeat does
  // NOT — keyed by structure so a structure change is itself a digest change.
  //
  // Returns null when the structure carries no topology yet (the caller then
  // falls through to the proposing tracker / honest placeholder).
  _buildLiveFigure(at, heartbeat, activeRuns) {
    const structure = String(at.structure || '');
    const epochGens = (Array.isArray(at.competitors) ? at.competitors : [])
      .map((c) => c && c.generation_id).filter((g) => g != null).map(String);
    const gens = epochGens.length ? epochGens : null;
    const onCompetitor = this.onCompetitor || undefined;

    if (structure === 'racing') {
      // build the unified LIVE model (published rounds + active-runs overlay) so
      // the rungs carry live_progress + the widened entering field, then derive
      // the racing model (which recovers championScalar from the live aggregate /
      // projected standings — degrades gracefully to a delta-only domain when the
      // champion scalar is unrecoverable mid-race).
      const st = buildLiveRacingModel({ at, heartbeat, activeRuns, epochGens: gens }) || normalizeStructure(at, true);
      const model = racingModel(st);
      if (!model || !model.hasRungs) return null;
      // FULL-WIDTH HERO: the scalar number-line IS the primary viz, so it scales
      // aspect-locked to fill the hero width (`responsive` → the svg.dn-scalartrack
      // -hero max-width cap governs). `mini` keeps the compact label/tick treatment
      // (the rung stepper carries the structural progress, not a wide caption).
      const opts = {
        rungs: model.rungs, championId: model.championId, benchmarkId: model.benchmarkId,
        championScalar: model.championScalar, live: model.live, gateState: model.gateState,
        mini: true, responsive: true, onCompetitor,
      };
      const node = racingScalarTrack(opts);
      return { node, digest: 'racing|' + racingScalarTrackDigest(opts) };
    }
    if (structure === 'swiss') {
      // FULL-WIDTH HERO: the swiss ladder scales aspect-locked to fill the hero
      // width (`responsive` → the svg.dn-swissladder-hero max-width cap governs),
      // matching racing's full-width track. A wide figure → fills to its cap.
      const st = buildLiveSwissModel({ at, heartbeat, activeRuns, epochGens: gens }) || normalizeStructure(at, true);
      const model = swissModel(st);
      if (!model || !model.hasRounds) return null;
      const opts = {
        rounds: model.rounds, standings: model.standings,
        championId: model.championId, benchmarkId: model.benchmarkId,
        live: model.live, gateState: model.gateState, gateDelta: model.gateDelta,
        responsive: true, onCompetitor,
      };
      const node = swissLadder(opts);
      return { node, digest: 'swiss|' + swissDigest(model) };
    }
    if (structure === 'single_elim' || structure === 'double_elim') {
      const st = buildLiveElimModel({ at, heartbeat, activeRuns, epochGens: gens }) || normalizeStructure(at, true);
      const model = elimModel(st);
      if (!model || !model.hasMatches) return null;
      const isDouble = structure === 'double_elim';
      if (isDouble) {
        // DOUBLE-ELIM hero: the refined orthogonal-pipe elimFlow combo WITH the
        // WB/LB bands — the SAME figure the single-round page leads with by
        // DEFAULT for double-elim. At hero size the WB/LB band split + life
        // glyphs read more truthfully than a tiny radial (a mini radial would
        // collapse two interleaved arcs into an unreadable knot), so we keep the
        // combo for consistency-with-default AND legibility.
        // FULL-WIDTH HERO: the WB/LB elimFlow combo scales aspect-locked to fill
        // the hero width (`responsive` → svg.dn-elimflow-hero cap governs), a wide
        // figure filling to its cap — matching racing's full-width treatment.
        const opts = {
          rounds: model.rounds, gen_states: model.gen_states,
          championId: model.championId, benchmarkId: model.benchmarkId,
          live: model.live, gateState: model.gateState, responsive: true, onCompetitor,
        };
        return { node: elimFlow(opts), digest: 'elim|' + elimDigest(model) };
      }
      // SINGLE-ELIM hero: the concentric-ring radial — the single-round PRIMARY,
      // reading the SERVED gen_states verbatim (the same model elimFlow gates on).
      // FULL-WIDTH HERO: aspect-locked + responsive; as a SQUARE figure the
      // svg.dn-elimradial-hero cap centres it under the cap (margin-inline:auto).
      const opts = {
        rounds: model.rounds, gen_states: model.gen_states,
        championId: model.championId, benchmarkId: model.benchmarkId,
        gateState: model.gateState, live: model.live, double: false, mini: true,
        responsive: true, onCompetitor,
      };
      return { node: elimRadial(opts), digest: 'elim|' + elimRadialDigest(opts) };
    }
    if (structure === 'gauntlet') {
      // GAUNTLET hero: the wave-of-challengers-vs-the-champion-standard field
      // bars — the SAME final liked gauntlet figure, in mini. Built from the
      // unified live model so in-flight challengers carry their board-progress
      // lane + projected scalar.
      const st = buildLiveModel(at, heartbeat, activeRuns, gens) || normalizeStructure(at, true);
      const model = gauntletModel(st);
      if (!model || !model.hasField) return null;
      // FULL-WIDTH HERO: the field bars scale aspect-locked to fill the hero width
      // (`responsive` → svg.dn-fieldbars-hero cap governs), a wide figure filling
      // to its cap — matching racing's full-width treatment.
      const opts = {
        championId: model.championId, championScalar: model.championScalar,
        promoteMargin: model.promoteMargin, challengers: model.challengers,
        live: model.live, mini: true, responsive: true, onCompetitor,
      };
      const node = gauntletFieldBars(opts);
      return { node, digest: 'gauntlet|' + gauntletModelDigest(model) };
    }
    return null;
  }

  // Build the proposing-step tracker + its digest from the active tournament's
  // `field_status`. Returns null when there is no field to show (so the caller
  // falls through to the placeholder): either no active tournament for the
  // CURRENT epoch, or a tournament that already has a running structure figure
  // (handled before this is reached). Structure-agnostic — the field is minted
  // identically for racing / swiss / elim.
  _buildProposingTracker(at, heartbeat) {
    if (!at || typeof at !== 'object') return null;
    if (!liveBelongsToEpoch(at, heartbeat)) return null;
    const fs = readFieldStatus(at);
    if (!fs.length) return null;
    const node = proposingTracker({ fieldStatus: fs, onCompetitor: this.onCompetitor || undefined });
    return { node, digest: proposingDigest(fs) };
  }
}

function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

// The drawer-collapse preference. Storage is best-effort on both sides (a
// private-mode browser throws on read AND write); a failure just means the
// drawer opens, which is the default anyway.
function readCollapsed() {
  try { return localStorage.getItem(BAND_COLLAPSE_KEY) === '1'; } catch { return false; }
}
function writeCollapsed(on) {
  try { localStorage.setItem(BAND_COLLAPSE_KEY, on ? '1' : '0'); } catch { /* best-effort */ }
}

// ── the structure-figure eligibility gate ────────────────────────────
//
// A LIVE structure figure (racing scalar-track / swiss ladder / elim radial-or-
// flow / gauntlet field-bars) renders iff: (1) there is an active tournament,
// (2) its `structure` is one we draw, (3) its `phase` is RUNNING (not completed/
// done/idle — a settled tournament keeps its topology but must not show the LIVE
// figure), and (4) it is scoped to the CURRENT live epoch (its `epoch_id`
// matches the heartbeat's). A stale/foreign tournament from a prior epoch is
// rejected here. Epoch scoping is permissive only when neither side names an
// epoch; a known-and-different pair is rejected.
//
// GAUNTLET is included now that the hero leads with gauntletFieldBars (mini) —
// but it earns the figure only when the gauntlet model actually builds a field
// (handled in _buildLiveFigure → gauntletModel().hasField; an empty/early
// gauntlet returns null there and falls through to the proposing tracker /
// generic summary). The eligibility gate itself is structure + running + epoch.
function structureEligible(at, heartbeat) {
  if (!structureDrawableRunning(at)) return false;
  return liveBelongsToEpoch(at, heartbeat);
}

// The structure + phase half of the gate, WITHOUT the epoch scope: a tournament
// structure we draw, in a running (non-settled) phase. The "what's running"
// panel pairs this with active-runs corroboration so a present in-flight run can
// stand in for a transiently-lagging epoch tag.
function structureDrawableRunning(at) {
  if (!at || typeof at !== 'object') return false;
  const s = String(at.structure);
  if (s !== 'racing' && s !== 'swiss' && s !== 'single_elim' && s !== 'double_elim' && s !== 'gauntlet') return false;
  const phase = String(at.phase == null ? '' : at.phase).trim().toLowerCase();
  const running = phase === 'running' || (phase !== '' && phase !== 'idle'
    && phase !== 'complete' && phase !== 'completed' && phase !== 'done');
  return running;
}

// Are there in-flight runs that BELONG to this tournament? A run belongs when
// its generation_id matches a competitor, or — when no roster is published — any
// present run corroborates. A run whose epoch_id is KNOWN-and-different from the
// tournament's is excluded, so a foreign run never lights up a stale tournament.
function tournamentHasActiveRuns(at, activeRuns) {
  const runs = Array.isArray(activeRuns) ? activeRuns : [];
  if (!runs.length || !at || typeof at !== 'object') return false;
  const tEpoch = (at.epoch_id != null && String(at.epoch_id) !== '') ? String(at.epoch_id) : null;
  const compIds = new Set(
    (Array.isArray(at.competitors) ? at.competitors : [])
      .map((c) => c && c.generation_id).filter((g) => g != null).map(String),
  );
  for (const r of runs) {
    if (!r || typeof r !== 'object') continue;
    // drop a run whose epoch is KNOWN-and-different from the tournament's.
    const rEpoch = (r.epoch_id != null && String(r.epoch_id) !== '') ? String(r.epoch_id) : null;
    if (tEpoch != null && rEpoch != null && rEpoch !== tEpoch) continue;
    if (!compIds.size) return true; // no roster to match against → trust the run.
    const gen = r.generation_id != null ? r.generation_id : r.gen;
    if (gen != null && compIds.has(String(gen))) return true;
  }
  return false;
}

// Synthesize ONE honest "running" block from the in-flight runs that BELONG to
// this tournament — the fallback the "what's running" panel shows when the
// published rounds are ALL settled (liveMatchBlocks → []) but runs are still in
// flight (the between-rounds gap). The belonging filter is the SAME epoch+roster
// guard as tournamentHasActiveRuns: a run whose epoch is KNOWN-and-different is
// dropped, and (when a roster is published) only a run whose generation matches a
// competitor is kept — so a foreign-epoch run never lights up a stale tournament.
// The block matches the shape liveMatchGroupedBlocks renders + that
// liveMatchBlocksDigest covers (so a steady beat over the same fallback stays a
// no-op): {kind, structure, match_id, label, entries:[{id, outcome:'pending', …}]}.
function runningEntriesFallbackBlock(at, activeRuns) {
  const runs = Array.isArray(activeRuns) ? activeRuns : [];
  if (!runs.length || !at || typeof at !== 'object') return null;
  const tEpoch = (at.epoch_id != null && String(at.epoch_id) !== '') ? String(at.epoch_id) : null;
  const compIds = new Set(
    (Array.isArray(at.competitors) ? at.competitors : [])
      .map((c) => c && c.generation_id).filter((g) => g != null).map(String),
  );
  const entries = [];
  const seen = new Set();
  for (const r of runs) {
    if (!r || typeof r !== 'object') continue;
    // drop a run whose epoch is KNOWN-and-different from the tournament's.
    const rEpoch = (r.epoch_id != null && String(r.epoch_id) !== '') ? String(r.epoch_id) : null;
    if (tEpoch != null && rEpoch != null && rEpoch !== tEpoch) continue;
    const gen = r.generation_id != null ? r.generation_id : (r.gen != null ? r.gen : r.entry_id);
    // with a published roster, only a run whose generation matches a competitor
    // belongs; with no roster to match against, trust the (non-foreign) run.
    if (compIds.size && !(gen != null && compIds.has(String(gen)))) continue;
    const id = gen != null ? String(gen) : null;
    if (id == null || seen.has(id)) continue;
    seen.add(id);
    // per-board progress (when the run carries it) drives the bar + the digest
    // bucket; a bare running run reads as in-flight (no ratio) — still honest.
    const done = isNum(r.boards_done) ? r.boards_done : (isNum(r.done) ? r.done : null);
    const total = isNum(r.boards_total) ? r.boards_total : (isNum(r.total) && r.total > 0 ? r.total : null);
    const ratio = (total != null && total > 0)
      ? Math.max(0, Math.min(1, (done == null ? 0 : done) / total))
      : (isNum(r.progress) ? Math.max(0, Math.min(1, r.progress)) : null);
    entries.push({
      id, outcome: 'pending', inflight: 1,
      ratio, done, total, boards_done: done, boards_total: total,
    });
  }
  if (!entries.length) return null;
  return {
    kind: 'rung', structure: String(at.structure || ''), match_id: 'running',
    label: 'running' + (entries.length ? ` · ${entries.length} in flight` : ''),
    entries,
  };
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
// per-round/match progress (a board landing fires it; a no-op stays equal).
// ROUNDED projection encoders so a no-op heartbeat yields a byte-identical
// digest (ZERO DOM writes) but a real board landing / re-rank fires the swap.
// `.toFixed(3)` the scalar; integer board counts.
//
// NOTE: racing + single-elim + gauntlet now digest via the NEW builders' own
// `*Digest` (racingScalarTrackDigest / elimRadialDigest / gauntletModelDigest)
// so the hero mini's swap compares the exact model those builders draw. Swiss +
// double-elim still use these local model digests (swissLadder / the elimFlow
// combo, which carry no companion `*Digest` export).
function projMatch(m) {
  return m && m.projected ? Object.keys(m.projected).sort().map((g) => {
    const p = m.projected[g];
    return g + ':' + (isNum(p.scalar) ? p.scalar.toFixed(3) : '?')
      + '/' + (p.boards_done == null ? '?' : p.boards_done) + '/' + (p.boards_total == null ? '?' : p.boards_total);
  }).join(',') : '';
}
function swissDigest(model) {
  return JSON.stringify({
    b: model.benchmarkId || null, c: model.championId || null, g: model.gateState || null,
    // points are NOT projected (swiss); but the per-row projected scalar +
    // boards progress is part of the digest so a board landing repaints.
    s: (model.standings || []).map((s) => [s.id, s.points, s.wins, s.draws, s.losses, s.rank,
      s.in_flight ? 'j' + (isNum(s.projected_scalar) ? s.projected_scalar.toFixed(3) : '?') + '/' + (s.boards_done == null ? '?' : s.boards_done) + '/' + (s.boards_total == null ? '?' : s.boards_total) : '']),
    r: (model.rounds || []).map((r) => [r.label, r.queued,
      (r.pairings || []).map((p) => [p.a, p.b, p.winner, p.bye, p.pending, p.done, p.total, p.inflight, projMatch(p)])]),
  });
}
function elimDigest(model) {
  const band = (rs) => (rs || []).map((r) => [r.label, r.queued,
    (r.matches || []).map((m) => [m.match_id, m.bracket_slot, (m.competitors || []).join('/'), m.winner, m.decision, m.bye, m.pending, m.done, m.total, m.inflight, m.queued, projMatch(m)])]);
  return JSON.stringify({
    b: model.benchmarkId || null, c: model.championId || null, g: model.gateState || null,
    w: band(model.winners), l: band(model.losers),
  });
}
