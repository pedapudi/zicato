// views/render.js — the dashboard render layer.
//
// This module owns every panel's DOM rendering. It is a pure consumer
// of the core spine: it imports AppState, the DOM helpers, the format
// helpers, the harmonograf builders and the api drill-down fetches —
// it never re-implements them. `app.js` is the thin entry point that
// imports the spine, wires the bus, and calls into this module.
//
// The render functions are idempotent — calling them again with the
// same state produces the same DOM. The activity-log tail GROWS by
// appending keyed rows (see core/dom.js appendRows) so it never
// flashes; a delta patches affected nodes rather than rebuilding a
// panel's innerHTML.
//
// NOTE (architecture): the per-view physical file split documented in
// js/CONTRACTS.md is staged behind this consolidated module. The render
// functions are grouped by view and self-contained; splitting them into
// js/views/<name>.js is mechanical and tracked as follow-up. The
// modular boundary that matters — core spine vs. components vs. render
// vs. entry point — is already enforced here.

import {
  $, el, svgEl, clearChildren, mount, patchText, patchAttr, patchClass,
  reconcileList, appendRows, trimRows,
} from '../core/dom.js';
import {
  SVG_NS, COLORS, DEFAULT_MARGIN, fmtDelta, fmtRate, fmtDuration,
  fmtScalar, truncate, parseIso, nowMs, fmtClock,
} from '../core/format.js';
import { state } from '../core/state.js';
import { VIEWS, DEFAULT_VIEW } from '../core/router.js';
import {
  harmonografBase, deriveRunId, harmonografSessionId, harmonografRunUrl,
  harmonografLink, harmonografMini, harmonografGenLink,
} from '../core/harmonograf.js';
import { fetchJson, loadMatchupDetail } from '../core/api.js';
import { diff as splitDiff } from '../components/index.js';
import {
  predictedGateVerdict, tournamentVerdict, dataQuality,
  entryStatus, entryIsDone, entryFailed,
  liveChampionId, liveChallengerId, liveRoundLabel,
} from './shared.js';
import { mockConversation } from './mock.js';

// The active view — owned by this module's showView/applyRoute. VIEWS
// and DEFAULT_VIEW are pinned by the router contract.
let currentView = DEFAULT_VIEW;

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
  // state.service mirrors it and stands in until /api/health lands.
  const h = state.health || {};
  const pick = (a, b) => {
    if (a != null && a !== '') return a;
    if (b != null && b !== '') return b;
    return '—';
  };
  $('dashboard-version').textContent =
    'dashboard · ' + pick(h.version, state.service.version);
  $('dashboard-port').textContent =
    'port · ' + pick(h.port, state.service.port);
  $('dashboard-build').textContent =
    'build · ' + pick(h.build, state.service.build);
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

// --- Render: Overview — the environment home
//
// The Overview is a one-glance home for the whole zicato environment.
// It is NOT the Tournament board: the full champion/challenger board
// lives ONLY in the Tournament view. The Overview carries the identity
// block, the loop-health line, a COMPACT live-activity card (a small
// summary of the active round with a link through to the Tournament
// view), the score trajectory, an epochs table and recent experiments.

// Resolve a board-entry's scalar across every producer shape. The live
// runtime `ActiveTournamentEntry` carries `loss_summary.drift_loss`;
// the contract / mock shape uses `scalar_score`; older shapes used a
// bare `score` or a nested `child.drift_loss`. Reading all of them
// means a finished entry shows its score rather than a bare "done".
function boardEntryScalar(e) {
  if (!e) return null;
  let v = null;
  if (e.scalar_score != null) v = e.scalar_score;
  else if (e.score != null) v = e.score;
  else if (e.loss_summary && typeof e.loss_summary === 'object'
           && typeof e.loss_summary.drift_loss === 'number') {
    v = e.loss_summary.drift_loss;
  } else if (e.child && typeof e.child.drift_loss === 'number') {
    v = e.child.drift_loss;
  }
  return (typeof v === 'number' && isFinite(v)) ? v : null;
}

// The Overview's identity block — workspace, epoch id, current
// generation, registered inner-harness entrypoint, mutation-site count
// and epoch count. Every field degrades to an em-dash when absent.
function renderIdentityPanel() {
  const wrap = $('identity-panel');
  if (!wrap) return;
  clearChildren(wrap);

  const hb = state.heartbeat || {};
  const def = state.epochDef || {};
  const epochId = state.epoch.id && state.epoch.id !== '—'
    ? state.epoch.id : (def.epoch_id || null);
  const genId = hb.generation_id || state.epoch.generation;
  const harness = (def.harness && typeof def.harness === 'object')
    ? def.harness : null;
  const mutationCount = Array.isArray(def.mutations) ? def.mutations.length : null;
  const gens = (state.lineage && Array.isArray(state.lineage.generations))
    ? state.lineage.generations : [];
  const epochIds = new Set();
  for (const g of gens) { if (g && g.epoch_id) epochIds.add(g.epoch_id); }
  if (epochId) epochIds.add(epochId);

  const grid = el('div', { class: 'identity-grid' });
  const row = (label, value) => {
    grid.appendChild(el('div', { class: 'identity-row' }, [
      el('span', { class: 'identity-label' }, [label]),
      el('span', { class: 'identity-value mono' }, [
        value != null && value !== '' ? String(value) : '—',
      ]),
    ]));
  };
  row('workspace', state.workspace);
  row('epoch', epochId);
  row('generation', genId != null && genId !== '' ? genId : null);
  row('inner harness', harness ? harness.entrypoint : null);
  row('mutation sites', mutationCount != null ? String(mutationCount) : null);
  row('epochs', epochIds.size > 0 ? String(epochIds.size) : null);
  wrap.appendChild(grid);
}

// A compact summary of one tournament side — champion or challenger —
// for the live-activity card: a status pill plus the scalar once done.
function liveActivitySide(label, genId, side) {
  const st = sideStatus(side);
  const sc = boardEntryScalar(side);
  return el('div', { class: 'live-side st-' + st }, [
    el('span', { class: 'live-side-label' }, [label]),
    el('span', { class: 'live-side-gen mono' }, [genId || '—']),
    el('span', { class: 'pill pill-' + st }, [st]),
    sc != null
      ? el('span', { class: 'live-side-score mono' }, ['scalar ' + fmtRate(sc)])
      : null,
  ]);
}

// The compact live-activity card — a SMALL one-glance summary of the
// active tournament: round, champion vs challenger, N of total runs
// done, the current aggregate scalar, and a link through to the full
// Tournament view. The full board is NOT rendered here.
function renderLiveActivity() {
  const wrap = $('live-activity');
  if (!wrap) return;
  clearChildren(wrap);

  const t = state.activeTournament;
  if (!t || !Array.isArray(t.entries) || t.entries.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, [
      'No active tournament. ',
      el('a', { class: 'live-link', href: '#/tournament' }, ['Open the Tournament view']),
      ' for the bracket history.',
    ]));
    return;
  }

  const roundLabel = liveRoundLabel(t);
  const champId = liveChampionId(t);
  const childId = liveChallengerId(t);
  const dq = dataQuality(t.entries);

  const card = el('div', { class: 'live-card' });

  // Head — round label + a one-line run census.
  const census = `${dq.completed} of ${dq.total} runs done` +
    (dq.running > 0 ? ` · ${dq.running} running` : '') +
    (dq.failed > 0 ? ` · ${dq.failed} failed` : '');
  card.appendChild(el('div', { class: 'live-card-head' }, [
    el('h3', { class: 'live-card-title' }, [
      roundLabel != null ? 'Round ' + roundLabel : 'Active round',
    ]),
    el('span', { class: 'live-card-census meta mono' }, [census]),
  ]));

  // Hypothesis core idea — a single line of context, truncated.
  if (t.hypothesis && typeof t.hypothesis.core_idea === 'string'
      && t.hypothesis.core_idea.trim() !== '') {
    card.appendChild(el('p', { class: 'live-card-hyp meta' }, [
      truncate(t.hypothesis.core_idea, 140),
    ]));
  }

  // Champion vs challenger — one compact row each. The board entries
  // are summed per side to a representative top-line, not enumerated.
  const champSides = t.entries.filter((e) => {
    const s = String(e && e.side || '').toLowerCase();
    return s === 'parent' || s === 'champion' || s === '';
  });
  const childSides = t.entries.filter((e) => {
    const s = String(e && e.side || '').toLowerCase();
    return s === 'child' || s === 'challenger';
  });
  const repr = (sides) => {
    // Prefer a finished side (carries a scalar); else the first.
    return sides.find(entryIsDone) || sides.find((e) => sideStatus(e) === 'running')
      || sides[0] || null;
  };
  const matchup = el('div', { class: 'live-matchup' }, [
    liveActivitySide('Champion', champId, repr(champSides)),
    el('span', { class: 'live-vs', 'aria-hidden': 'true' }, ['vs']),
    liveActivitySide('Challenger', childId, repr(childSides)),
  ]);
  card.appendChild(matchup);

  // Current aggregate scalar — the mean drift-loss scalar across every
  // finished challenger-side entry (lower is better). Shown only when
  // at least one challenger run has finished.
  const finishedChild = childSides.filter(entryIsDone);
  const childScalars = finishedChild
    .map(boardEntryScalar).filter((v) => v != null);
  if (childScalars.length > 0) {
    const agg = childScalars.reduce((a, b) => a + b, 0) / childScalars.length;
    card.appendChild(el('div', { class: 'live-aggregate mono' }, [
      el('span', { class: 'meta' }, ['aggregate scalar ']),
      fmtRate(agg),
      el('span', { class: 'meta' }, [
        ` · ${childScalars.length} of ${childSides.length} challenger runs scored`,
      ]),
    ]));
  }

  // Link through to the full Tournament view — the board belongs there.
  card.appendChild(el('a', {
    class: 'live-link live-card-link',
    href: '#/tournament',
    'aria-label': 'open the full Tournament view',
  }, ['View the full tournament board →']));

  wrap.appendChild(card);
}

// Pull (drift_loss_mean, pass_rate) for one side, preferring the
// server-computed running partial aggregate. The runner rewrites
// `partial_parent_agg` / `partial_child_agg` the instant each board
// unit settles (see runner._IncrementalScorer), so this number climbs
// as the tournament runs rather than sitting at 0.00 until round end.
// `agg` is the aggregate_generation_score dict shape; missing fields
// degrade to null so fmtRate renders an em-dash, not a false zero.
function partialSide(agg) {
  if (!agg || typeof agg !== 'object') return null;
  const dm = agg.drift_loss_mean;
  const pr = agg.pass_rate;
  return {
    drift_loss_mean: (typeof dm === 'number' && isFinite(dm)) ? dm : null,
    pass_rate: (typeof pr === 'number' && isFinite(pr)) ? pr : null,
    entry_count: (typeof agg.entry_count === 'number') ? agg.entry_count : 0,
  };
}

// The partial aggregate panel for the in-progress tournament — the
// running scalar the gate will decide on. It belongs to the Tournament
// view's hall (the Overview is a compact environment home and does not
// render the full board).
function renderAggregate(t) {
  const wrap = el('div', { class: 'aggregate' });
  wrap.appendChild(el('h4', null, [
    `Partial aggregate (${(t.entries || []).filter(entryIsDone).length} of ${t.entries ? t.entries.length : 0})`
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

  // Prefer the server-side running partial aggregate the runner
  // persists per board unit. Fall back to a client-side derivation
  // over the per-side entry rows only when the server fields are
  // absent (a legacy active_tournament.json written before the
  // incremental-scorer change).
  const serverParent = partialSide(t.partial_parent_agg);
  const serverChild = partialSide(t.partial_child_agg);

  let parentDM, parentPR, childDM, childPR;
  if (serverParent || serverChild) {
    parentDM = serverParent ? serverParent.drift_loss_mean : null;
    parentPR = serverParent ? serverParent.pass_rate : null;
    childDM = serverChild ? serverChild.drift_loss_mean : null;
    childPR = serverChild ? serverChild.pass_rate : null;
  } else {
    const finished = (t.entries || []).filter(entryIsDone);
    let parentDriftSum = 0, parentPassSum = 0;
    let childDriftSum = 0, childPassSum = 0;
    for (const e of finished) {
      if (e.parent) { parentDriftSum += e.parent.drift_loss || 0; parentPassSum += e.parent.pass ? 1 : 0; }
      if (e.child)  { childDriftSum  += e.child.drift_loss || 0;  childPassSum  += e.child.pass  ? 1 : 0; }
    }
    const n = Math.max(1, finished.length);
    parentDM = parentDriftSum / n;
    parentPR = parentPassSum / n;
    childDM = childDriftSum / n;
    childPR = childPassSum / n;
  }
  const regression =
    (typeof childPR === 'number' && typeof parentPR === 'number' && childPR < parentPR) ||
    (typeof childDM === 'number' && typeof parentDM === 'number' && childDM > parentDM);

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

  // Prefer the server-side scalars when the running partial aggregate
  // carried them — they are the gate's exact scalar, not the
  // drift-minus-pass approximation. Fall back to the approximation
  // (legacy active_tournament.json with no partial aggregate).
  const pScalar = (t.partial_parent_agg && typeof t.partial_parent_agg.scalar === 'number')
    ? t.partial_parent_agg.scalar
    : (typeof parentDM === 'number' && typeof parentPR === 'number') ? parentDM - parentPR : null;
  const cScalar = (t.partial_child_agg && typeof t.partial_child_agg.scalar === 'number')
    ? t.partial_child_agg.scalar
    : (typeof childDM === 'number' && typeof childPR === 'number') ? childDM - childPR : null;
  const dScalar = (typeof pScalar === 'number' && typeof cScalar === 'number')
    ? cScalar - pScalar
    : NaN;
  wrap.appendChild(el('p', { class: 'mono' }, [
    `Δscalar ${fmtDelta(dScalar)}`,
  ]));
  return wrap;
}

// The Overview score trajectory — the scalar across every generation,
// sourced from `state.scoreTrajectory.points` (build_score_trajectory).
// Painted into its own #overview-trajectory-svg so it never collides
// with the Tree view's trajectory chart.
function renderOverviewTrajectory() {
  const svg = $('overview-trajectory-svg');
  if (!svg) return;
  clearChildren(svg);

  const width = 720, height = 220;
  sizeSvg(svg, width, height);

  const points = (state.scoreTrajectory && Array.isArray(state.scoreTrajectory.points))
    ? state.scoreTrajectory.points : [];
  const scored = points
    .map((p) => ({
      id: p.generation_id,
      v: typeof p.scalar === 'number' && isFinite(p.scalar) ? p.scalar : null,
      promoted: p.promoted,
    }))
    .filter((p) => p.v != null);

  if (scored.length === 0) {
    svg.appendChild(svgEl('text', {
      class: 'svg-axis', x: width / 2, y: height / 2, 'text-anchor': 'middle',
    }, ['No scored generations yet.']));
    return;
  }

  const values = scored.map((s) => s.v);
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
  const n = scored.length;
  const xStep = n > 1 ? plotW / (n - 1) : 0;
  const toX = (i) => (n === 1 ? marginL + plotW / 2 : marginL + i * xStep);
  const toY = (v) => marginT + (vmax - v) / (vmax - vmin) * plotH;

  // axes
  svg.appendChild(svgEl('line', {
    class: 'svg-axis', x1: marginL, y1: marginT + plotH,
    x2: marginL + plotW, y2: marginT + plotH,
    stroke: COLORS.grid, 'stroke-width': '1',
  }));
  svg.appendChild(svgEl('line', {
    class: 'svg-axis', x1: marginL, y1: marginT,
    x2: marginL, y2: marginT + plotH,
    stroke: COLORS.grid, 'stroke-width': '1',
  }));

  // the curve
  const d = scored.map((s, i) =>
    `${i === 0 ? 'M' : 'L'}${toX(i).toFixed(1)},${toY(s.v).toFixed(1)}`).join(' ');
  svg.appendChild(svgEl('path', {
    d, fill: 'none', stroke: COLORS.running, 'stroke-width': '2',
  }));

  // markers, coloured by verdict
  scored.forEach((s, i) => {
    const fill = s.promoted === true ? COLORS.promoted
      : s.promoted === false ? COLORS.rejected
        : COLORS.running;
    svg.appendChild(svgEl('circle', {
      cx: toX(i).toFixed(1), cy: toY(s.v).toFixed(1), r: '3.5', fill,
    }));
    svg.appendChild(svgEl('text', {
      class: 'svg-axis', x: toX(i).toFixed(1), y: (marginT + plotH + 16).toFixed(1),
      'text-anchor': 'middle',
    }, [String(s.id || '')]));
  });
}

// The epochs table — every epoch the lineage feed knows about, with its
// generation count and how many were promoted. The current epoch is
// flagged. Sourced from `state.lineage.generations` (build_lineage_view
// walks every epoch) folded with the current `epoch_id`.
function renderEpochsPanel() {
  const wrap = $('epochs-panel');
  if (!wrap) return;
  clearChildren(wrap);

  const gens = (state.lineage && Array.isArray(state.lineage.generations))
    ? state.lineage.generations : [];
  const currentEpoch = state.epoch.id && state.epoch.id !== '—'
    ? state.epoch.id
    : (state.epochDef && state.epochDef.epoch_id) || null;

  // Group generations into epoch buckets, preserving first-seen order.
  const order = [];
  const byEpoch = new Map();
  for (const g of gens) {
    if (!g || !g.epoch_id) continue;
    const id = String(g.epoch_id);
    if (!byEpoch.has(id)) { byEpoch.set(id, []); order.push(id); }
    byEpoch.get(id).push(g);
  }
  // The current epoch may have no generation directory yet — still list it.
  if (currentEpoch && !byEpoch.has(currentEpoch)) {
    byEpoch.set(currentEpoch, []);
    order.push(currentEpoch);
  }

  if (order.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No epochs recorded yet.']));
    return;
  }

  const tbl = el('table', { class: 'data-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['epoch']),
    el('th', null, ['generations']),
    el('th', null, ['promoted']),
    el('th', null, ['']),
  ])]));
  const tbody = el('tbody');
  for (const id of order) {
    const list = byEpoch.get(id) || [];
    const promoted = list.filter((g) => g && g.promoted === true).length;
    const isCurrent = id === currentEpoch;
    tbody.appendChild(el('tr', { class: isCurrent ? 'epoch-row-current' : '' }, [
      el('td', { class: 'mono' }, [id]),
      el('td', { class: 'mono' }, [String(list.length)]),
      el('td', { class: 'mono' }, [String(promoted)]),
      el('td', null, [
        isCurrent
          ? el('span', { class: 'badge running' }, ['current'])
          : el('a', { class: 'live-link', href: '#/epoch/' + encodeURIComponent(id) },
              ['view']),
      ]),
    ]));
  }
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
}

// Recent experiments — the same per-generation experiment records the
// Epoch view's experiment log renders (`state.epochDef.experiments`).
// The Overview shows a compact, read-only digest of the most recent
// few, each with its hypothesis core-idea and tournament outcome.
function renderRecentExperiments() {
  const wrap = $('recent-experiments');
  if (!wrap) return;
  clearChildren(wrap);

  const def = state.epochDef;
  const experiments = (def && Array.isArray(def.experiments)) ? def.experiments : [];
  if (experiments.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, [
      'No experiments recorded yet.',
    ]));
    return;
  }

  // Most-recent-first; cap at six so the Overview stays a digest.
  const recent = experiments.slice(-6).reverse();
  const list = el('div', { class: 'recent-exp-list' });
  for (const exp of recent) {
    const genId = exp.generation_id || '?';
    const hyp = (exp.hypothesis && typeof exp.hypothesis === 'object')
      ? exp.hypothesis : {};
    const coreIdea = hyp.core_idea || '—';
    const outcome = (exp.outcome && typeof exp.outcome === 'object')
      ? exp.outcome : null;
    const decision = outcome
      ? (outcome.tournament_decision || outcome.decision || null) : null;
    const delta = outcome ? outcome.scalar_score_delta : null;
    const decisionClass = decision
      ? (String(decision).toLowerCase().includes('promot') ? 'promoted' : 'rejected')
      : 'pending';

    list.appendChild(el('div', { class: 'recent-exp-row' }, [
      el('span', { class: 'recent-exp-gen mono' }, [genId]),
      el('span', { class: 'recent-exp-idea' }, [truncate(coreIdea, 110)]),
      outcome
        ? el('span', { class: 'recent-exp-verdict badge ' + decisionClass }, [
            (decision || '?') +
            (typeof delta === 'number' && isFinite(delta)
              ? ' ' + (delta > 0 ? '+' : '') + delta.toFixed(3) : ''),
          ])
        : el('span', { class: 'recent-exp-verdict badge pending' }, ['in progress']),
    ]));
  }
  wrap.appendChild(list);

  // A link through to the Epoch view's full experiment log.
  wrap.appendChild(el('a', {
    class: 'live-link recent-exp-link',
    href: '#/epoch',
    'aria-label': 'open the full experiment log in the Epoch view',
  }, ['Full experiment log →']));
}

// The Overview render entry point — paints every environment-home panel.
function renderOverview() {
  renderIdentityPanel();
  renderHealthPanel();
  renderLiveActivity();
  renderOverviewTrajectory();
  renderEpochsPanel();
  renderRecentExperiments();
  renderLogTail();
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

  // The environment-wide evolution curve: the ABSOLUTE per-generation
  // scalar (drift-loss aggregate — lower is better), plotted in lineage
  // order. Sourced from GET /api/score-trajectory, NOT from the
  // per-round scalar_score_delta — a delta is not an evolution curve.
  const points = (state.scoreTrajectory && state.scoreTrajectory.points) || [];

  const width = 720, height = 220;
  sizeSvg(svg, width, height);
  if (points.length === 0) {
    svg.appendChild(svgEl('text', {
      class: 'svg-axis', x: width / 2, y: height / 2, 'text-anchor': 'middle',
    }, ['No generations to plot.']));
    return;
  }

  // A generation with no scored runs yet carries scalar === null — it
  // is kept on the x-axis (as a gap) so the lineage stays continuous.
  const series = points.map((p, i) => ({
    i,
    id: p.generation_id,
    decision: p.promoted === true ? 'promoted'
      : p.promoted === false ? 'rejected'
        : 'in_flight',
    v: typeof p.scalar === 'number' ? p.scalar : null,
    entryCount: p.entry_count || 0,
  }));

  const values = series.map(s => s.v).filter(v => v !== null);
  if (values.length === 0) {
    svg.appendChild(svgEl('text', {
      class: 'svg-axis', x: width / 2, y: height / 2, 'text-anchor': 'middle',
    }, ['No scored generations yet.']));
    return;
  }
  // The scalar is a loss >= 0; anchor the axis at 0 so the curve's
  // magnitude reads honestly.
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
    // Absolute loss tick — no leading '+', this is a magnitude.
    const tickVal = vmax - (vmax - vmin) * k / 4;
    svg.appendChild(svgEl('text', {
      class: 'svg-axis',
      x: marginL - 6, y: (gy + 3).toFixed(1), 'text-anchor': 'end',
    }, [tickVal.toFixed(2)]));
  }

  // x-axis
  svg.appendChild(svgEl('line', {
    x1: marginL, y1: (marginT + plotH).toFixed(1),
    x2: marginL + plotW, y2: (marginT + plotH).toFixed(1),
    stroke: COLORS.grid, 'stroke-width': 1,
  }));

  // The evolution curve: connect every SCORED generation in lineage
  // order. A generation still being scored (scalar === null) is a gap
  // — the line picks up again at the next scored point.
  const scored = series.filter(s => s.v !== null)
    .map(s => [toX(s.i), toY(s.v)]);
  if (scored.length >= 2) {
    const d = 'M ' + scored.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(' L ');
    svg.appendChild(svgEl('path', {
      d, fill: 'none', stroke: COLORS.baseline, 'stroke-width': 2,
    }));
  }

  for (const s of series) {
    const cx = toX(s.i);
    if (s.v === null) {
      // Unscored generation — mark its x-axis slot, no data point.
      svg.appendChild(svgEl('text', {
        class: 'svg-axis',
        x: cx.toFixed(1), y: (marginT + plotH + 14).toFixed(1),
        'text-anchor': 'middle',
      }, [s.id]));
      continue;
    }
    const cy = toY(s.v);
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
    }, [s.v.toFixed(2)]));
  }

  svg.appendChild(svgEl('text', {
    class: 'svg-axis',
    x: marginL - 36, y: (marginT + plotH / 2).toFixed(1),
    'text-anchor': 'middle',
    transform: `rotate(-90 ${marginL - 36} ${(marginT + plotH / 2).toFixed(1)})`,
  }, ['scalar (loss)']));
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

// The tournament-level harmonograf jump-off — a clearly-visible link
// in the hall head that opens harmonograf for the tournament as a
// whole. harmonograf has no per-tournament filter URL, so this lands on
// the bare base; the challenger generation id scopes the aria-label.
// Returns null when the heartbeat carries no harmonograf url at all.
function tournamentHarmonografLink(childId) {
  const base = harmonografBase();
  if (!base) return null;
  return el('a', {
    class: 'harmonograf-link harmonograf-tournament',
    href: base, target: '_blank', rel: 'noopener',
    'aria-label': 'open harmonograf traces for the tournament'
      + (childId ? ' challenging with ' + childId : ''),
  }, ['Open tournament in harmonograf ↗']);
}

// The hall: occupancy header + the board-card grid.
function renderHall(t) {
  const boards = hallBoards(t);
  const hall = el('section', { class: 'tournament-hall', 'aria-label': 'tournament hall' });

  const childId = liveChallengerId(t);
  const champId = liveChampionId(t);
  const headTitle = el('div', { class: 'hall-head-title' }, [
    el('h3', { class: 'hall-title' }, ['Tournament hall']),
  ]);
  // Tournament-overall harmonograf jump-off, surfaced beside the title.
  const tHg = tournamentHarmonografLink(childId);
  if (tHg) headTitle.appendChild(tHg);
  hall.appendChild(el('div', { class: 'hall-head' }, [
    headTitle,
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

  // The running partial aggregate — the scalar the gate will decide on,
  // folded per board unit as each settles (runner._IncrementalScorer).
  hall.appendChild(renderAggregate(t));
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

// Resolve the harmonograf jump-off for one board side. The active-
// tournament entry carries the run's real ADK session id once the run
// finishes (the runner stamps `adk_session_id` from the LossProfile);
// while the run is in flight we fall back to its active-run record.
// Either way `harmonografRunUrl` resolves the deep-link, falling back
// to the bare base when no session id is known yet — so the link is
// always present whenever the heartbeat carries a harmonograf url.
function boardSideHarmonografLink(entry, label, genId) {
  if (harmonografBase() == null) return null;
  // Prefer the entry itself — it carries `adk_session_id` post-run.
  let target = entry;
  if (!harmonografSessionId(entry)) {
    // In flight: try the live active-run record for this side.
    const run = findActiveRunForEntry(entry);
    if (run && harmonografSessionId(run)) target = run;
  }
  const link = harmonografMini(target, 'harmonograf',
    'open harmonograf trace for the ' + label + ' run of '
      + ((entry && entry.entry_id) || 'this board')
      + (genId ? ' (' + genId + ')' : ''));
  if (!link) return null;
  // The board card's own click opens the conversation diff; the
  // harmonograf link must not also trigger it.
  link.addEventListener('click', (ev) => ev.stopPropagation());
  return link;
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
  // Per-board / per-run harmonograf jump-off — a clearly-distinct
  // element on the side's header row, deep-linked by the run's ADK
  // session id. Kept separate from the status pill (a sibling agent
  // owns the per-entry status label) so a merge stays clean.
  const sideHg = boardSideHarmonografLink(entry, label.toLowerCase(), genId);
  if (sideHg) head.appendChild(sideHg);
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
    // A finished side shows the canonical "done" status plus its
    // scalar. boardEntryScalar reads every producer shape — the live
    // runtime's `loss_summary.drift_loss`, the contract `scalar_score`,
    // a bare `score`, or a nested `child.drift_loss` — so a completed
    // run is never mislabelled or shown bare just because the producer
    // wrote the score under a different key.
    const sc = boardEntryScalar(entry);
    row.appendChild(el('div', { class: 'board-side-score mono' }, [
      sc != null ? 'done · scalar ' + fmtRate(sc) : 'done',
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
  const champSc = boardEntryScalar(board.parent);
  const childSc = boardEntryScalar(board.child);

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

// Board card click → show the conversation diff inline in the Tournament
// detail panel. Sets the selected entry, kicks off the transcript fetch,
// re-renders the detail panel, and scrolls it into view. The URL hash is
// updated to #/tournament so the view does not change; the separate
// #conversation/{id} deep-link route still works for direct navigation.
function openBoardConversation(entryId) {
  if (entryId == null) return;
  state.selectedEntry = entryId;
  // selectConversation manages the fetch and sets convEntryId / convData.
  selectConversation(entryId);
  renderMatchupDetail();
  const section = $('tournament-detail-section');
  if (section && section.scrollIntoView) {
    section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  // Update the hash to record the open entry without leaving the tournament
  // view. The `conv` kind segment is handled by applyRoute so reloading or
  // sharing the URL restores the inline conversation. Deep-links using
  // #conversation/{id} still work unchanged.
  location.hash = '#/tournament/conv/' + encodeURIComponent(String(entryId));
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
    // Canonical bucket — a 'completed' entry paints a done dot, not a
    // queued one.
    dots.appendChild(el('span', { class: 'bracket-dot ' + entryStatus(e) }, ['']));
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

// Render the champion->challenger per-drift-kind movement table into
// `wrap`. Data comes from GET /api/drift-movements/:gen (cached on
// state.driftMovements). Each row names a drift kind, the champion's
// and challenger's event counts, and the movement — "worsened" (more
// drift on the challenger), "improved" (fewer), or "unchanged".
function renderDriftMovements(wrap, genId, champ) {
  const dm = state.driftMovements[genId];
  if (!dm) {
    wrap.appendChild(el('p', { class: 'empty' }, ['Loading drift movements…']));
    return;
  }
  const movements = Array.isArray(dm.movements) ? dm.movements : [];
  if (movements.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, [
      dm.note || 'No drift-kind movements recorded for this matchup.',
    ]));
    return;
  }
  const championLabel = dm.champion || champ || 'champion';
  const challengerLabel = dm.challenger || genId || 'challenger';
  const tbl = el('table', { class: 'data-table drift-movements' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['drift kind']),
    el('th', null, [championLabel + ' (champion)']),
    el('th', null, [challengerLabel + ' (challenger)']),
    el('th', null, ['Δ']),
    el('th', null, ['movement']),
  ])]));
  const tbody = el('tbody');
  for (const mv of movements) {
    const dir = String(mv.direction || 'unchanged');
    const delta = Number(mv.delta || 0);
    const deltaStr = delta > 0 ? '+' + delta : String(delta);
    tbody.appendChild(el('tr', { class: 'drift-mv drift-mv-' + dir }, [
      el('td', null, [el('code', { class: 'mono' }, [mv.kind || '—'])]),
      el('td', { class: 'mono' }, [String(mv.champion_count != null ? mv.champion_count : 0)]),
      el('td', { class: 'mono' }, [String(mv.challenger_count != null ? mv.challenger_count : 0)]),
      el('td', { class: 'mono' }, [deltaStr]),
      el('td', null, [el('span', { class: 'drift-dir drift-dir-' + dir }, [dir])]),
    ]));
  }
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
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

  // --- Drift-kind movements (champion -> challenger)
  wrap.appendChild(el('h3', null, ['Drift-kind movements']));
  renderDriftMovements(wrap, genId, champ);

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

  // --- Inline conversation diff
  // Rendered when the operator clicked a board card in the hall. The
  // transcript data lives in the conversation-view module vars (convEntryId,
  // convData, convError) and is fetched by openBoardConversation via
  // selectConversation. renderMatchupDetail is called again on every
  // loadConversation completion so the columns grow live.
  if (convEntryId) {
    wrap.appendChild(renderInlineConversation());
  }
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

// --- Inline conversation diff (rendered inside the tournament detail panel)
//
// Reuses the module-level conversation vars (convEntryId, convData,
// convError) set by selectConversation / loadConversation. When a board
// card is clicked openBoardConversation calls selectConversation(entryId)
// and then renderMatchupDetail, which calls this function. loadConversation
// re-calls renderMatchupDetail on completion so the columns populate without
// a separate render path.

function renderInlineConversation() {
  const wrap = el('div', { class: 'inline-conversation' });

  const entryId = convEntryId;
  const conv = convData;

  wrap.appendChild(el('h3', null, [
    'Conversation diff ',
    el('code', { class: 'mono' }, [entryId || '']),
  ]));

  if (convError) {
    wrap.appendChild(el('p', { class: 'empty conversation-unavailable' }, [
      'Conversation data unavailable — the transcript endpoint did not ' +
      'respond. The runs may not have started yet.',
    ]));
    return wrap;
  }

  if (!conv) {
    wrap.appendChild(el('p', { class: 'empty' }, [
      'Loading conversation…',
    ]));
    return wrap;
  }

  const champion = conv && conv.champion;
  const challenger = conv && conv.challenger;

  const champGen = (champion && champion.generation_id) || '—';
  const chalGen = (challenger && challenger.generation_id) || '—';
  wrap.appendChild(el('div', { class: 'conversation-versus' }, [
    el('span', { class: 'conversation-side-tag champion' }, [
      'champion ', el('code', { class: 'mono' }, [champGen]),
    ]),
    el('span', { class: 'conversation-vs' }, ['vs']),
    el('span', { class: 'conversation-side-tag challenger' }, [
      'challenger ', el('code', { class: 'mono' }, [chalGen]),
    ]),
  ]));

  wrap.appendChild(el('div', { class: 'conversation-columns' }, [
    renderTranscriptColumn('champion', champion),
    renderTranscriptColumn('challenger', challenger),
  ]));

  // Keep the live poll running: schedule a re-fetch while either side
  // is still in progress. The poll targets renderMatchupDetail (which
  // re-calls this function) instead of renderConversationView.
  if (!state.mock && convInProgress(conv)) {
    const since = Date.now() - convLastFetch;
    if (since >= CONV_POLL_MS) {
      loadConversation();
    } else {
      setTimeout(() => {
        if (currentView === 'tournament' && convEntryId === entryId
            && convInProgress(convData)) {
          loadConversation();
        }
      }, CONV_POLL_MS - since);
    }
  }

  return wrap;
}

// --- Render: Epoch view
//
// The Epoch view is the epoch's NARRATIVE: the operator's goal (the
// proposer brief) then the ordered story of every experiment — what it
// tried, the hypothesis written BEFORE the run, the change it made, and
// the outcome recorded AFTER. All data comes from `state.epochDef`
// (`GET /api/epoch` / the `epoch` key on `/api/environment`); every
// field is read defensively — any block may be absent on a fresh epoch.

function renderEpochView() {
  const def = state.epochDef;
  renderEpochHeader(def);
  renderEpochBrief(def);
  renderEpochExperimentLog(def);
  renderEpochHarness(def);
  renderEpochBoard(def);
  renderEpochScoring(def);
  renderEpochMutations(def);
  renderEpochJournal(def);
  renderEpochAnalysis(def);
}

function kv(label, value) {
  return el('div', { class: 'kv' }, [
    el('span', { class: 'kv-label' }, [label]),
    el('span', { class: 'kv-value mono' }, [value]),
  ]);
}

// Normalise an experiment's outcome decision to one of the three
// TournamentDecision values, or null when the experiment has not run.
function _epochDecision(outcome) {
  if (!outcome || typeof outcome !== 'object') return null;
  const raw = outcome.tournament_decision || outcome.decision || '';
  const d = String(raw).toLowerCase();
  if (d.includes('promot')) return 'promoted';
  if (d.includes('reject')) return 'rejected';
  if (d.includes('defer')) return 'deferred';
  return raw ? d : null;
}

// Map a decision to a badge kind class.
function _epochDecisionKind(decision) {
  if (decision === 'promoted') return 'promoted';
  if (decision === 'rejected') return 'rejected';
  if (decision === 'deferred') return 'deferred';
  return 'pending';
}

// Format a signed scalar delta. Improvement is a negative loss delta.
function _fmtSignedDelta(v, digits) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  const d = digits == null ? 3 : digits;
  return (v > 0 ? '+' : '') + v.toFixed(d);
}

// The epoch header: identity, status, and a one-line tally of the
// experiments — how many ran, how many were promoted / rejected, and
// the net scalar movement across the epoch. This is the at-a-glance
// summary that frames the narrative below it.
function renderEpochHeader(def) {
  const wrap = $('epoch-overview');
  clearChildren(wrap);
  wrap.classList.add('epoch-header');
  if (!def || !def.epoch_id) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No epoch loaded.']));
    return;
  }

  const closed = def.closed === true;
  const experiments = Array.isArray(def.experiments) ? def.experiments : [];
  let promoted = 0;
  let rejected = 0;
  let ran = 0;
  let netScalar = 0;
  let haveScalar = false;
  for (const exp of experiments) {
    const decision = _epochDecision(exp.outcome);
    if (decision) {
      ran += 1;
      if (decision === 'promoted') promoted += 1;
      else if (decision === 'rejected') rejected += 1;
    }
    const ds = exp.outcome && exp.outcome.scalar_score_delta;
    if (typeof ds === 'number' && isFinite(ds)) { netScalar += ds; haveScalar = true; }
  }
  const pending = experiments.length - ran;

  // Identity line — the epoch id large, status pill alongside.
  const idLine = el('div', { class: 'epoch-header-id' }, [
    el('h2', { class: 'epoch-header-name mono' }, [def.epoch_id]),
    el('span', { class: 'badge ' + (closed ? 'muted' : 'promoted') },
      [closed ? 'closed' : 'open']),
  ]);
  wrap.appendChild(idLine);

  // Sub-line — created timestamp + contract hash, muted.
  const subBits = [];
  if (def.created_at) subBits.push('created ' + def.created_at);
  if (def.contract_hash) subBits.push('contract ' + truncate(def.contract_hash, 12));
  if (subBits.length) {
    wrap.appendChild(el('p', { class: 'epoch-header-sub meta mono' },
      [subBits.join('  ·  ')]));
  }

  // Stat strip — the experiment tally.
  const stats = el('div', { class: 'epoch-stat-strip' });
  const stat = (value, label, cls) => el('div', { class: 'epoch-stat' }, [
    el('span', { class: 'epoch-stat-value' + (cls ? ' ' + cls : '') },
      [String(value)]),
    el('span', { class: 'epoch-stat-label' }, [label]),
  ]);
  stats.appendChild(stat(experiments.length, 'experiments'));
  stats.appendChild(stat(promoted, 'promoted', promoted > 0 ? 'good' : null));
  stats.appendChild(stat(rejected, 'rejected', rejected > 0 ? 'bad' : null));
  if (pending > 0) stats.appendChild(stat(pending, 'in progress', 'pend'));
  if (haveScalar) {
    // Net scalar: negative is improvement (loss went down).
    const cls = netScalar < 0 ? 'good' : (netScalar > 0 ? 'bad' : null);
    stats.appendChild(stat(_fmtSignedDelta(netScalar), 'net Δscalar', cls));
  }
  wrap.appendChild(stats);
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

function renderEpochBrief(def) {
  const wrap = $('epoch-brief');
  clearChildren(wrap);
  // Centering / max-width is owned by the `.epoch-brief` CSS rule;
  // ensure the container carries the class even if markup drifts, and
  // always wrap content in a `brief-block` so the readable-block
  // styling applies (no lopsided narrow column).
  wrap.classList.add('epoch-brief');
  // The epoch contract carries the proposer brief under `brief`;
  // `rubric` is the legacy key and is read as a fallback so a snapshot
  // from a pre-rename build still renders.
  let brief = '';
  if (def && typeof def.brief === 'string') {
    brief = def.brief;
  } else if (def && typeof def.rubric === 'string') {
    brief = def.rubric;
  }
  if (!brief.trim()) {
    const empty = el('div', { class: 'brief-block' }, [
      el('p', { class: 'empty' }, ['No proposer brief recorded.']),
    ]);
    wrap.appendChild(empty);
    return;
  }
  const block = el('div', { class: 'brief-block' });
  // A lead caption frames the brief as the operator's goal for the
  // epoch — the thing every experiment below is reaching for.
  block.appendChild(el('p', { class: 'epoch-brief-lead meta' }, [
    'The operator’s goal handed to the proposer for this epoch.',
  ]));
  renderMinimalMarkdown(brief, block);
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

// --- Render: Epoch experiment log
//
// The narrative core of the Epoch view. Each experiment (one generation
// evaluated by a tournament) renders as a card told in four beats:
// (1) WHAT — the proposer's core idea + generation id + lineage;
// (2) HYPOTHESIS — the pre-run structured prediction; (3) CHANGE — the
// patch set, with a line diff that expands on click; (4) OUTCOME — the
// tournament verdict, scalar Δ, rejection reason, tournament jump.
// All story data is on `state.epochDef.experiments`; the baseline fetch
// for the diff is lazy and only powers beat 3.

// Cache for baseline mutation contents so we can diff without a fetch.
// Key: epochId. Value: { mutation_id -> content }.
const _baselineCache = {};

// Fetch and cache the v0 baseline mutation contents for an epoch.
// Resolves to { mutation_id -> content } (empty object on failure).
async function _loadBaselineContents(epochId) {
  if (_baselineCache[epochId]) return _baselineCache[epochId];
  let result = {};
  try {
    const idx = await fetchJson('/api/mutations/' + encodeURIComponent(epochId));
    const sites = (idx && idx.mutations) || [];
    for (const site of sites) {
      if (!site.mutation_id) continue;
      try {
        const detail = await fetchJson(
          '/api/mutations/' + encodeURIComponent(epochId)
          + '/' + encodeURIComponent(site.mutation_id)
        );
        if (detail && detail.baseline && detail.baseline.content != null) {
          result[site.mutation_id] = detail.baseline.content;
        }
      } catch (_) { /* best-effort */ }
    }
  } catch (_) { /* best-effort */ }
  _baselineCache[epochId] = result;
  return result;
}

// Which experiment card has its CHANGE (diff) section expanded
// (generation_id or null).
let _expandedExperiment = null;
// Baseline contents for the current epoch (loaded on first expand).
let _experimentBaseline = null;

// A small labelled "field" line used inside the hypothesis / outcome
// blocks — a bold lead word followed by a free-text value.
function _expField(lead, value) {
  if (value == null || value === '') return null;
  return el('p', { class: 'exp-field' }, [
    el('span', { class: 'exp-field-lead' }, [lead]),
    String(value),
  ]);
}

// Build the HYPOTHESIS beat — the proposer's pre-run prediction.
function _renderExperimentHypothesis(hyp, modulating) {
  const block = el('div', { class: 'exp-beat exp-beat-hypothesis' });
  block.appendChild(el('div', { class: 'exp-beat-label' }, ['Hypothesis']));
  block.appendChild(el('p', { class: 'exp-beat-caption meta' }, [
    'Written before the run.',
  ]));
  let any = false;
  const why = _expField('Why. ', hyp.why);
  if (why) { block.appendChild(why); any = true; }
  const exp = _expField('Expected. ', hyp.expected_pass_rate_delta);
  if (exp) { block.appendChild(exp); any = true; }
  // Expected drift movements — concise typed predictions.
  const moves = Array.isArray(hyp.expected_drift_movements)
    ? hyp.expected_drift_movements : [];
  if (moves.length > 0) {
    const items = moves
      .map((m) => (m && m.kind)
        ? `${m.kind} ${m.direction || '?'}${m.magnitude ? ' (' + m.magnitude + ')' : ''}`
        : null)
      .filter(Boolean);
    if (items.length) {
      block.appendChild(_expField('Predicted drift. ', items.join('; ')));
      any = true;
    }
  }
  const risks = _expField('Risks. ', hyp.risks);
  if (risks) { block.appendChild(risks); any = true; }
  if (modulating.length > 0) {
    const sites = el('p', { class: 'exp-field' }, [
      el('span', { class: 'exp-field-lead' }, ['Modulating. ']),
    ]);
    for (const m of modulating) {
      sites.appendChild(el('code', { class: 'mono code-pill' }, [m]));
    }
    block.appendChild(sites);
    any = true;
  }
  if (!any) {
    block.appendChild(el('p', { class: 'meta' }, [
      'No structured rationale recorded beyond the core idea.',
    ]));
  }
  return block;
}

// Build the OUTCOME beat — the tournament's post-run verdict.
function _renderExperimentOutcome(outcome, decision, genId) {
  const block = el('div', { class: 'exp-beat exp-beat-outcome' });
  block.appendChild(el('div', { class: 'exp-beat-label' }, ['Outcome']));
  if (!outcome) {
    block.appendChild(el('p', { class: 'exp-beat-caption meta' }, [
      'This experiment has not finished evaluating yet.',
    ]));
    block.appendChild(el('p', { class: 'exp-pending-line' }, [
      el('span', { class: 'badge pending' }, ['in progress']),
    ]));
    return block;
  }
  block.appendChild(el('p', { class: 'exp-beat-caption meta' }, [
    'Recorded by the tournament after the run.',
  ]));

  // Verdict line: did the challenger beat the champion?
  const won = decision === 'promoted';
  const verdictText = won
    ? 'Challenger beat the champion — promoted to the new lineage head.'
    : (decision === 'rejected'
      ? 'Challenger did not beat the champion — rejected.'
      : (decision === 'deferred'
        ? 'No decisive winner — kept for analysis, lineage head unchanged.'
        : 'Decision recorded.'));
  block.appendChild(el('p', { class: 'exp-verdict-line' }, [
    el('span', { class: 'badge ' + _epochDecisionKind(decision) },
      [decision || '?']),
    el('span', { class: 'exp-verdict-text' }, [verdictText]),
  ]));

  // Scalar delta + its components, as a compact metric strip.
  const metrics = el('div', { class: 'exp-metric-strip' });
  const metric = (label, value, goodIsNeg) => {
    let cls = '';
    if (typeof value === 'number' && isFinite(value) && value !== 0) {
      const good = goodIsNeg ? value < 0 : value > 0;
      cls = good ? ' good' : ' bad';
    }
    return el('div', { class: 'exp-metric' }, [
      el('span', { class: 'exp-metric-value mono' + cls },
        [typeof value === 'number' ? _fmtSignedDelta(value) : '—']),
      el('span', { class: 'exp-metric-label' }, [label]),
    ]);
  };
  // Δscalar: negative is improvement (combined loss fell).
  metrics.appendChild(metric('Δscalar', outcome.scalar_score_delta, true));
  // Δpass rate: positive is improvement.
  metrics.appendChild(metric('Δpass rate', outcome.pass_rate_delta, false));
  // Δdrift loss: negative is improvement.
  metrics.appendChild(metric('Δdrift loss', outcome.drift_loss_delta, true));
  block.appendChild(metrics);

  if (outcome.rejection_reason) {
    block.appendChild(el('p', { class: 'exp-field exp-rejection' }, [
      el('span', { class: 'exp-field-lead' }, ['Rejection reason. ']),
      outcome.rejection_reason,
    ]));
  }
  if (outcome.ran_at) {
    block.appendChild(el('p', { class: 'meta mono exp-ran-at' }, [
      'evaluated ' + outcome.ran_at,
    ]));
  }
  block.appendChild(el('a', {
    href: '#/tournament/' + encodeURIComponent(genId),
    class: 'exp-tournament-link',
  }, ['Open the full tournament for ' + genId + ' →']));
  return block;
}

// Build the CHANGE beat — the concrete patch set, expandable to a diff.
function _renderExperimentChange(exp, genId, def) {
  const patches = (exp.patches && typeof exp.patches === 'object')
    ? Object.values(exp.patches) : [];
  const block = el('div', { class: 'exp-beat exp-beat-change' });
  const isExpanded = _expandedExperiment === genId;

  const header = el('div', { class: 'exp-beat-label exp-change-header' }, [
    el('span', null, ['Change']),
    el('span', { class: 'exp-change-count meta' },
      [patches.length === 1 ? '1 patch' : patches.length + ' patches']),
  ]);
  block.appendChild(header);

  if (patches.length === 0) {
    block.appendChild(el('p', { class: 'meta' }, ['No patch recorded.']));
    return block;
  }

  // A compact, always-visible summary of each patch (site + op).
  for (const patch of patches) {
    const mutId = patch.mutation_id || '?';
    const row = el('div', { class: 'exp-patch-summary' }, [
      el('code', { class: 'mono code-pill' }, [mutId]),
      patch.op ? el('span', { class: 'mutations-version-op' }, [patch.op]) : null,
      patch.rationale
        ? el('span', { class: 'exp-patch-rationale-inline meta' }, [patch.rationale])
        : null,
    ]);
    block.appendChild(row);
  }

  // The diff toggle — the diff can be long, so it expands on demand.
  const toggleBtn = el('button', {
    type: 'button',
    class: 'exp-diff-toggle',
    'aria-expanded': isExpanded ? 'true' : 'false',
    'data-genid': genId,
  }, [isExpanded ? 'Hide the diff' : 'Show the diff']);

  const epochId = def && def.epoch_id;
  const onToggle = () => {
    _expandedExperiment = (_expandedExperiment === genId) ? null : genId;
    // Re-render synchronously so the diff opens immediately — with the
    // patch's `new_content` shown as an addition when no baseline is
    // cached yet. If the baseline has not been fetched, kick off the
    // lazy load and re-render once it lands so the diff fills in.
    renderEpochExperimentLog(def);
    if (_expandedExperiment === genId && epochId && !_experimentBaseline) {
      _loadBaselineContents(epochId).then((baseline) => {
        _experimentBaseline = baseline;
        renderEpochExperimentLog(def);
      }).catch(() => { /* keep the addition-only diff already shown */ });
    }
  };
  toggleBtn.addEventListener('click', onToggle);
  block.appendChild(toggleBtn);

  if (isExpanded) {
    const diffWrap = el('div', { class: 'exp-diff-wrap' });
    for (const patch of patches) {
      const mutId = patch.mutation_id || '?';
      const patchBlock = el('div', { class: 'exp-patch-block' });
      patchBlock.appendChild(el('div', { class: 'exp-patch-header' }, [
        el('code', { class: 'mono code-pill' }, [mutId]),
        patch.op ? el('span', { class: 'mutations-version-op' }, [patch.op]) : null,
      ]));
      const baselineContent = (_experimentBaseline && _experimentBaseline[mutId]) || null;
      const newContent = patch.new_content != null ? String(patch.new_content) : null;
      if (baselineContent != null && newContent != null) {
        patchBlock.appendChild(renderMutationDiff(baselineContent, newContent));
      } else if (newContent != null) {
        patchBlock.appendChild(renderMutationDiff('', newContent));
      } else if (patch.new_numeric != null) {
        patchBlock.appendChild(el('p', { class: 'mono' }, [
          'numeric → ' + String(patch.new_numeric),
        ]));
      } else if (patch.new_enum != null) {
        patchBlock.appendChild(el('p', { class: 'mono' }, [
          'enum → ' + String(patch.new_enum),
        ]));
      } else {
        patchBlock.appendChild(el('p', { class: 'empty' }, [
          'Patch content not available.',
        ]));
      }
      diffWrap.appendChild(patchBlock);
    }
    block.appendChild(diffWrap);
  }
  return block;
}

function renderEpochExperimentLog(def) {
  const wrap = $('epoch-experiment-log');
  if (!wrap) return;
  clearChildren(wrap);
  wrap.classList.add('exp-narrative');

  const experiments = def && Array.isArray(def.experiments) ? def.experiments : [];
  if (experiments.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, [
      'No experiments recorded this epoch yet — the proposer has not run.',
    ]));
    return;
  }

  const list = el('div', { class: 'exp-card-list' });

  experiments.forEach((exp, idx) => {
    const genId = exp.generation_id || '?';
    const hyp = (exp.hypothesis && typeof exp.hypothesis === 'object')
      ? exp.hypothesis : {};
    const outcome = (exp.outcome && typeof exp.outcome === 'object')
      ? exp.outcome : null;
    const decision = _epochDecision(outcome);
    const modulating = Array.isArray(hyp.modulating) ? hyp.modulating : [];
    const coreIdea = (typeof hyp.core_idea === 'string' && hyp.core_idea.trim())
      ? hyp.core_idea.trim() : 'No description recorded.';

    // The card carries an accent stripe coloured by the decision so the
    // promoted / rejected / pending arc is scannable down the column.
    const card = el('article', {
      class: 'exp-card exp-card-' + _epochDecisionKind(decision),
      'data-genid': genId,
    });

    // -- Beat 1: WHAT (the description / header) -------------------
    const head = el('header', { class: 'exp-card-head' });
    const titleRow = el('div', { class: 'exp-card-titlerow' }, [
      el('span', { class: 'exp-card-ordinal' }, ['#' + (idx + 1)]),
      el('span', { class: 'exp-card-gen mono' }, [genId]),
      el('span', { class: 'badge ' + _epochDecisionKind(decision) },
        [decision || 'in progress']),
      (outcome && typeof outcome.scalar_score_delta === 'number')
        ? el('span', {
            class: 'exp-card-delta mono '
              + (outcome.scalar_score_delta < 0 ? 'good'
                : (outcome.scalar_score_delta > 0 ? 'bad' : '')),
          }, ['Δscalar ' + _fmtSignedDelta(outcome.scalar_score_delta)])
        : null,
    ]);
    head.appendChild(titleRow);
    head.appendChild(el('p', { class: 'exp-card-idea' }, [coreIdea]));
    if (exp.parent_generation_id) {
      head.appendChild(el('p', { class: 'exp-card-lineage meta mono' }, [
        'challenger ' + genId + '   vs  champion ' + exp.parent_generation_id,
      ]));
    }
    card.appendChild(head);

    // -- Beats 2-4: hypothesis · change · outcome ------------------
    const body = el('div', { class: 'exp-card-body' });
    body.appendChild(_renderExperimentHypothesis(hyp, modulating));
    body.appendChild(_renderExperimentChange(exp, genId, def));
    body.appendChild(_renderExperimentOutcome(outcome, decision, genId));
    card.appendChild(body);

    list.appendChild(card);
  });

  wrap.appendChild(list);
}

// --- Render: Epoch journal
//
// Renders the epoch's journal.md using renderMinimalMarkdown.
// The journal is a round-by-round log of hypothesis + outcome entries.

function renderEpochJournal(def) {
  const wrap = $('epoch-journal');
  if (!wrap) return;
  clearChildren(wrap);
  wrap.classList.add('epoch-journal');

  const journal = (def && typeof def.journal === 'string') ? def.journal : '';
  if (!journal.trim()) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No journal recorded.']));
    return;
  }
  const block = el('div', { class: 'brief-block' });
  renderMinimalMarkdown(journal, block);
  wrap.appendChild(block);
}

// --- Render: Epoch analysis
//
// Renders the epoch's analysis.md and (if the HTML exists) offers a
// link to open the self-contained analysis.html in a new tab.

function renderEpochAnalysis(def) {
  const wrap = $('epoch-analysis');
  if (!wrap) return;
  clearChildren(wrap);

  const md = (def && typeof def.analysis_md === 'string') ? def.analysis_md : '';
  const htmlAvail = def && def.analysis_html_available;
  const epochId = def && def.epoch_id;

  if (!md.trim() && !htmlAvail) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No analysis recorded.']));
    return;
  }

  if (htmlAvail && epochId) {
    const bar = el('div', { class: 'analysis-html-bar' }, [
      el('a', {
        href: '/api/epoch/' + encodeURIComponent(epochId) + '/analysis.html',
        target: '_blank',
        rel: 'noopener',
        class: 'harmonograf-link',
      }, ['Open full analysis report ↗']),
    ]);
    wrap.appendChild(bar);
  }

  if (md.trim()) {
    const block = el('div', { class: 'brief-block' });
    renderMinimalMarkdown(md, block);
    wrap.appendChild(block);
  }
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

// Normalise the shared log-tail contract — { events:[{seq,kind,ts,
// summary}] } — with a fallback to the legacy `state.logLines` shape.
function _logTailEvents() {
  if (state.logTail && Array.isArray(state.logTail.events)) {
    return state.logTail.events.map((e) => ({
      seq: e.seq != null ? e.seq : null,
      kind: e.kind || 'event',
      ts: e.ts || null,
      summary: e.summary != null ? e.summary : '',
    }));
  }
  if (Array.isArray(state.logLines)) {
    return state.logLines.map((line) => ({
      seq: null,
      kind: (line.level || 'log'),
      ts: line.ts || null,
      summary: line.message != null ? line.message : (line.line || ''),
    }));
  }
  return [];
}

// Build one log-line DOM node from a normalised event.
function _logLineEl(ev) {
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
  lineEl.appendChild(el('span', { class: 'log-kind badge ' + cls }, [kind]));
  lineEl.appendChild(el('span', { class: 'log-summary' }, [
    String(ev.summary || ''),
  ]));
  return lineEl;
}

// A stable key for one log event. The run-log carries a monotone
// `seq`; un-sequenced synthetic events fall back to a content digest
// so a re-feed of the same event is recognised and NOT re-appended.
let _logSyntheticCursor = 0;
const _logSyntheticKeys = new WeakMap();
function _logKey(ev) {
  if (ev.seq != null) return 'seq:' + ev.seq;
  // A synthetic (seq-less) event: digest its content so an identical
  // re-feed maps to the same key. Different content -> different key.
  return 'syn:' + (ev.ts || '') + '|' + (ev.kind || '') + '|' + (ev.summary || '');
}

// Render the log tail. STRUCTURAL no-flash guarantee: the tail is grown
// by appendRows() — keyed by event seq — so rows already on screen are
// left strictly untouched. The panel is NEVER cleared-and-rebuilt on a
// delta; only genuinely-new events are appended. The `.empty`
// placeholder is removed once, lazily, when the first real row lands.
//
// `renderLogTail` and `appendLogTail` are now the SAME operation — both
// are append-only. The old full-repaint path (the flashing source) is
// gone: even the first paint just appends into an empty host.
function renderLogTail() {
  const wrap = $('log-tail');
  if (!wrap) return;
  const events = _logTailEvents();
  // Drop the empty placeholder exactly once, when real rows arrive.
  if (events.length > 0) {
    for (const child of [...wrap.children]) {
      if (child.classList && child.classList.contains('empty')) {
        wrap.removeChild(child);
      }
    }
  }
  if (events.length === 0) {
    if (!wrap.querySelector('[data-key]') && !wrap.querySelector('.empty')) {
      wrap.appendChild(el('p', { class: 'empty' }, ['No events yet.']));
    }
    return;
  }
  const wasAtBottom =
    wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 24;
  const added = appendRows(wrap, events, _logKey, _logLineEl);
  // Bound the tail without disturbing surviving rows.
  trimRows(wrap, 200);
  // Keep the freshest event in view only if the operator was already
  // parked at the bottom — never yank a scrolled-up reader down. Only
  // scroll when a row was actually added, so a no-op render does not
  // move the viewport.
  if (added > 0 && wasAtBottom) wrap.scrollTop = wrap.scrollHeight;
}

// appendLogTail is retained as an alias — the run_log SSE frame and the
// steady-state render both route through the one append-only path.
function appendLogTail() { renderLogTail(); }

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
  } else if (segs[0] === 'files') {
    // Files view — #/files  ·  #/files/{epoch_id}/{generation_id}.
    // The epoch + generation are real route segments (NOT a drill-down
    // kind/id pair): the view is route-driven, so a reload or a shared
    // link lands on the same generation. Bare #/files resolves to a
    // sensible default — the current epoch and its latest generation —
    // which is then written back into the hash as a deep link.
    view = 'files';
    const routeEpoch = segs[1] ? decodeURIComponent(segs[1]) : null;
    const routeGen = segs[2] ? decodeURIComponent(segs[2]) : null;
    if (view !== currentView) showView(view);
    else showViewClassesOnly(view);
    applyDrill(null, null);
    applyFilesRoute(routeEpoch, routeGen);
    return;
  } else if (VIEWS.includes(segs[0])) {
    view = segs[0];
    if (segs.length >= 3) {
      kind = segs[1];
      id = decodeURIComponent(segs.slice(2).join('/'));
    }
    // #/tournament/conv/{entry_id} — restore the inline conversation diff
    // from a deep-link or a board-card click without switching views.
    if (view === 'tournament' && kind === 'conv' && id) {
      if (view !== currentView) showView(view);
      else showViewClassesOnly(view);
      state.selectedEntry = id;
      selectConversation(id);
      applyDrill(null, null);
      return;
    }
    // #/tournament/{generation_id} — open the matchup detail directly
    // (the router contract's tournament deep-link). A single trailing
    // segment that is not the `conv` kind is a challenger generation id.
    if (view === 'tournament' && segs.length === 2 && segs[1] !== 'conv') {
      const genId = decodeURIComponent(segs[1]);
      if (view !== currentView) showView(view);
      else showViewClassesOnly(view);
      // openMatchup sets state.selectedMatchup, lazily loads the detail
      // and re-renders — the matchup panel opens from the deep-link.
      openMatchup(genId);
      applyDrill(null, null);
      return;
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
    // The entry's scalar score — under whichever key the producer used
    // (`loss_summary.drift_loss` for a live runtime entry, `scalar_score`
    // for the contract shape, a bare `score`, or a nested side object).
    const sc = boardEntryScalar(entry);
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
  // Paint the relevant view immediately so loading state is visible.
  if (currentView === 'conversation') {
    renderConversationView();
  } else if (currentView === 'tournament') {
    renderMatchupDetail();
  }
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
    if (currentView === 'tournament') renderMatchupDetail();
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
  if (currentView === 'tournament') renderMatchupDetail();
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

// --- Files view -----------------------------------------------------
//
// The Files view browses a generation's source tree and applied patch
// set, AND shows a side-by-side (split) diff of the files the
// generation changed relative to its parent (or the v0 baseline). It
// reads through /api/files* — the server resolves those through the
// GenerationStore seam, so this works identically for the
// directory-snapshot and git storage backends.
//
// The view is ROUTE-DRIVEN: the selected epoch + generation live in the
// hash (#/files/{epoch}/{generation}), so a reload or a shared link
// lands on the same generation. Bare #/files resolves to a sensible
// default — the current epoch and its latest generation — written back
// into the hash by applyFilesRoute as a deep link.

const filesState = {
  index: null,        // { epochs: [{ epoch_id, generations: [...] }] }
  selectedGen: null,  // { epoch_id, generation_id } — driven by the route
  tree: null,         // { entries: [{ path, is_dir, size }] }
  openFile: null,     // rel path of the file shown in the content pane
  changes: null,      // { parent_generation_id, files: [...] } diff payload
  changesKey: null,   // epoch/generation the cached `changes` belongs to
};

function fmtFileSize(bytes) {
  if (typeof bytes !== 'number' || bytes < 0) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// The last generation in an epoch index entry — the picker's default.
// The /api/files index lists generations in store order (v0, v1, ...),
// so the final element is the latest.
function latestGenerationOf(epochEntry) {
  const gens = (epochEntry && epochEntry.generations) || [];
  return gens.length ? gens[gens.length - 1].generation_id : null;
}

// Resolve the route's (epoch, generation) against the loaded index,
// filling in defaults: the current epoch (or the last epoch in the
// index) and that epoch's latest generation. Returns null when there
// is no generation to show at all.
function resolveFilesTarget(routeEpoch, routeGen) {
  const epochs = (filesState.index && filesState.index.epochs) || [];
  if (epochs.length === 0) return null;
  // Prefer the routed epoch, then the live current epoch, then the last
  // epoch in the index — but always one that actually exists.
  const byId = (id) => epochs.find((e) => e.epoch_id === id) || null;
  let epoch = (routeEpoch && byId(routeEpoch))
    || (state.epoch && byId(state.epoch.id))
    || epochs[epochs.length - 1];
  if (!epoch) return null;
  const gens = (epoch.generations || []).map((g) => g.generation_id);
  if (gens.length === 0) return null;
  const generationId = (routeGen && gens.includes(routeGen))
    ? routeGen
    : latestGenerationOf(epoch);
  return { epoch_id: epoch.epoch_id, generation_id: generationId };
}

// Route entry point for the Files view. Called by applyRoute with the
// raw hash segments; resolves them to a concrete (epoch, generation),
// rewrites the hash as a canonical deep link when it was bare or stale,
// and loads the selected generation.
async function applyFilesRoute(routeEpoch, routeGen) {
  if (filesState.index == null) {
    try {
      filesState.index = await fetchJson('/api/files');
    } catch (err) {
      filesState.index = { epochs: [] };
    }
  }
  const target = resolveFilesTarget(routeEpoch, routeGen);
  if (!target) {
    // Nothing to show — paint the empty states and stop.
    filesState.selectedGen = null;
    renderFilesView();
    return;
  }
  // Canonicalise the hash: a bare #/files, or one whose epoch/generation
  // did not resolve exactly, becomes #/files/{epoch}/{generation} so the
  // link is deep-linkable and the nav highlight stays stable. The hash
  // is updated in place; the load runs unconditionally below (it is a
  // no-op when the selection is unchanged), so it does not depend on a
  // hashchange re-entry.
  const wantHash = `#/files/${encodeURIComponent(target.epoch_id)}`
    + `/${encodeURIComponent(target.generation_id)}`;
  if (location.hash !== wantHash) {
    location.hash = wantHash;
  }
  await loadFilesGeneration(target.epoch_id, target.generation_id);
}

async function renderFilesView() {
  // Re-render entry point used by SSE-driven repaints. Re-resolve the
  // current hash so a state delta keeps the view consistent without a
  // re-fetch when the selection is unchanged.
  await applyFilesRoute(
    filesState.selectedGen ? filesState.selectedGen.epoch_id : null,
    filesState.selectedGen ? filesState.selectedGen.generation_id : null,
  );
}

// Load a generation's tree + changed-files diff and paint every Files
// panel. Cheap when the selection is unchanged — the tree and diff are
// cached, keyed by epoch/generation.
async function loadFilesGeneration(epochId, generationId) {
  const changed = !filesState.selectedGen
    || filesState.selectedGen.epoch_id !== epochId
    || filesState.selectedGen.generation_id !== generationId;
  if (changed) {
    filesState.selectedGen = { epoch_id: epochId, generation_id: generationId };
    filesState.openFile = null;
    filesState._content = null;
    filesState.tree = null;
    filesState.changes = null;
    filesState.changesKey = `${epochId}/${generationId}`;
    const base = `/api/files/${encodeURIComponent(epochId)}/${encodeURIComponent(generationId)}`;
    try {
      filesState.tree = await fetchJson(base + '/tree');
    } catch (err) {
      filesState.tree = { entries: [], error: String(err) };
    }
    try {
      filesState.changes = await fetchJson(base + '/diff');
    } catch (err) {
      filesState.changes = { files: [], error: String(err) };
    }
    // The mutation surface is per-epoch; a generation switch within the
    // same epoch reuses the cached index, a switch to a new epoch reloads.
    if (mutationsState.epochId !== epochId) {
      mutationsState.epochId = epochId;
      mutationsState.index = null;
      mutationsState.selectedId = null;
      mutationsState.detail = null;
    }
  }
  renderFilesChanges();
  renderFilesIndex();
  renderFilesTree();
  renderFilesContent();
  renderFilesPatches();
  renderMutationsView();
}

// Navigate the picker by hash — the view is route-driven, so a click
// just changes the fragment and applyRoute re-enters applyFilesRoute.
function selectFilesGeneration(epochId, generationId) {
  location.hash = `#/files/${encodeURIComponent(epochId)}`
    + `/${encodeURIComponent(generationId)}`;
}

// The "What changed" section: a generation picker, then a side-by-side
// (split) diff of every file the selected generation changed relative
// to its parent generation (or the v0 baseline).
function renderFilesChanges() {
  renderFilesChangesControls();
  renderFilesChangesDiff();
}

// The generation picker for the changes section.
function renderFilesChangesControls() {
  const pane = $('files-changes-controls');
  if (!pane) return;
  clearChildren(pane);

  const epochs = (filesState.index && filesState.index.epochs) || [];
  if (epochs.length === 0) {
    pane.appendChild(el('p', { class: 'empty' }, ['No generations yet.']));
    return;
  }
  const sel = filesState.selectedGen;
  const picker = el('div', { class: 'files-gen-picker' });
  for (const epoch of epochs) {
    picker.appendChild(
      el('div', { class: 'files-epoch-label' }, [epoch.epoch_id])
    );
    for (const gen of epoch.generations || []) {
      const selected = sel
        && sel.epoch_id === epoch.epoch_id
        && sel.generation_id === gen.generation_id;
      picker.appendChild(el('button', {
        type: 'button',
        class: 'files-gen-button' + (selected ? ' active' : ''),
        onclick: () => selectFilesGeneration(epoch.epoch_id, gen.generation_id),
      }, [
        el('span', { class: 'files-gen-id' }, [gen.generation_id]),
        el('span', { class: 'files-gen-meta' }, [
          `${gen.file_count} files · ${gen.patch_count} patches`,
        ]),
      ]));
    }
  }
  pane.appendChild(picker);
}

// The side-by-side diff: one split diff per file the generation changed.
function renderFilesChangesDiff() {
  const pane = $('files-changes-diff');
  if (!pane) return;
  clearChildren(pane);

  if (!filesState.selectedGen) {
    pane.appendChild(
      el('p', { class: 'empty' }, ['Select a generation to see what it changed.'])
    );
    return;
  }
  const changes = filesState.changes || { files: [] };
  if (changes.error) {
    pane.appendChild(el('p', { class: 'empty' }, [changes.error]));
    return;
  }
  const parent = changes.parent_generation_id;
  pane.appendChild(el('div', { class: 'files-changes-summary' }, [
    parent
      ? `${filesState.selectedGen.generation_id} vs ${parent}`
      : `${filesState.selectedGen.generation_id} (seed — every file is new)`,
  ]));
  const files = changes.files || [];
  if (files.length === 0) {
    pane.appendChild(
      el('p', { class: 'empty' }, ['No file changes for this generation.'])
    );
    return;
  }
  for (const f of files) {
    const oldLabel = parent ? `${parent} · old` : 'baseline';
    const newLabel = `${filesState.selectedGen.generation_id} · new`;
    const block = el('div', { class: 'files-change-block' }, [
      el('div', { class: 'files-change-head' }, [
        el('span', { class: 'files-change-path' }, [f.path]),
        el('span', { class: `files-change-status status-${f.status}` }, [f.status]),
      ]),
    ]);
    if (f.old_binary || f.new_binary) {
      block.appendChild(
        el('p', { class: 'empty' }, ['Binary file — diff not shown.'])
      );
    } else {
      block.appendChild(el('div', { class: 'files-change-cols' }, [
        el('div', { class: 'files-change-col-label' }, [oldLabel]),
        el('div', { class: 'files-change-col-label' }, [newLabel]),
      ]));
      // The shared diff component in split mode: old on the left, new
      // on the right.
      block.appendChild(splitDiff(f.old_content, f.new_content, { mode: 'split' }));
    }
    pane.appendChild(block);
  }
}

// Left pane top: the epoch -> generation picker, then the file tree.
function renderFilesIndex() {
  const pane = $('files-tree-pane');
  if (!pane) return;
  clearChildren(pane);

  const index = filesState.index || { epochs: [] };
  const epochs = index.epochs || [];
  if (epochs.length === 0) {
    pane.appendChild(el('p', { class: 'empty' }, ['No generations yet.']));
    return;
  }

  const picker = el('div', { class: 'files-gen-picker' });
  for (const epoch of epochs) {
    picker.appendChild(
      el('div', { class: 'files-epoch-label' }, [epoch.epoch_id])
    );
    for (const gen of epoch.generations || []) {
      const selected = filesState.selectedGen
        && filesState.selectedGen.epoch_id === epoch.epoch_id
        && filesState.selectedGen.generation_id === gen.generation_id;
      const btn = el('button', {
        type: 'button',
        class: 'files-gen-button' + (selected ? ' active' : ''),
        onclick: () => selectFilesGeneration(epoch.epoch_id, gen.generation_id),
      }, [
        el('span', { class: 'files-gen-id' }, [gen.generation_id]),
        el('span', { class: 'files-gen-meta' }, [
          `${gen.file_count} files · ${gen.patch_count} patches`,
        ]),
      ]);
      picker.appendChild(btn);
    }
  }
  pane.appendChild(picker);

  // The file tree of the selected generation, below the picker.
  pane.appendChild(el('div', { id: 'files-tree-root', class: 'files-tree-root' }));
}

// The selected generation's source tree, rendered from the flat entry
// list the server returns (paths are '/'-separated; we group by dir).
function renderFilesTree() {
  const root = $('files-tree-root');
  if (!root) return;
  clearChildren(root);

  if (!filesState.selectedGen) {
    root.appendChild(
      el('p', { class: 'empty' }, ['Select a generation.'])
    );
    return;
  }
  const tree = filesState.tree || { entries: [] };
  if (tree.error) {
    root.appendChild(el('p', { class: 'empty' }, [tree.error]));
    return;
  }
  const entries = tree.entries || [];
  if (entries.length === 0) {
    root.appendChild(el('p', { class: 'empty' }, ['Empty tree.']));
    return;
  }
  const list = el('ul', { class: 'files-tree-list' });
  for (const entry of entries) {
    const depth = entry.path.split('/').length - 1;
    const name = entry.path.split('/').pop();
    if (entry.is_dir) {
      list.appendChild(
        el('li', {
          class: 'files-tree-dir',
          style: `padding-left:${depth * 12}px`,
        }, [name + '/'])
      );
    } else {
      const open = filesState.openFile === entry.path;
      list.appendChild(
        el('li', {
          class: 'files-tree-file' + (open ? ' active' : ''),
          style: `padding-left:${depth * 12}px`,
        }, [
          el('button', {
            type: 'button',
            class: 'files-tree-file-button',
            onclick: () => openFilesFile(entry.path),
          }, [
            el('span', { class: 'files-tree-file-name' }, [name]),
            el('span', { class: 'files-tree-file-size' }, [
              fmtFileSize(entry.size),
            ]),
          ]),
        ])
      );
    }
  }
  root.appendChild(list);
}

async function openFilesFile(relPath) {
  if (!filesState.selectedGen) return;
  const { epoch_id, generation_id } = filesState.selectedGen;
  const base = `/api/files/${encodeURIComponent(epoch_id)}/${encodeURIComponent(generation_id)}`;
  const url = base + '/content?path=' + encodeURIComponent(relPath);
  filesState.openFile = relPath;
  let payload;
  try {
    payload = await fetchJson(url);
  } catch (err) {
    payload = { path: relPath, error: String(err) };
  }
  filesState._content = payload;
  renderFilesTree();
  renderFilesContent();
}

// The right pane: the open file's content.
function renderFilesContent() {
  const pane = $('files-content-pane');
  if (!pane) return;
  clearChildren(pane);

  const payload = filesState._content;
  if (!filesState.openFile || !payload) {
    pane.appendChild(
      el('p', { class: 'empty' }, ['Select a file to view its contents.'])
    );
    return;
  }
  pane.appendChild(
    el('div', { class: 'files-content-header' }, [filesState.openFile])
  );
  if (payload.error) {
    pane.appendChild(el('p', { class: 'empty' }, [payload.error]));
    return;
  }
  if (payload.binary) {
    pane.appendChild(
      el('p', { class: 'empty' }, ['Binary file — not shown.'])
    );
    return;
  }
  if (payload.truncated) {
    pane.appendChild(
      el('div', { class: 'files-content-note' }, [
        `Truncated — showing the first part of a ${fmtFileSize(payload.size)} file.`,
      ])
    );
  }
  pane.appendChild(
    el('pre', { class: 'files-content-body' }, [payload.content || ''])
  );
}

// The patch-set section: the patches that derived the selected generation.
async function renderFilesPatches() {
  const pane = $('files-patches');
  if (!pane) return;
  clearChildren(pane);

  if (!filesState.selectedGen) {
    pane.appendChild(
      el('p', { class: 'empty' }, ['Select a generation to view its patches.'])
    );
    return;
  }
  const { epoch_id, generation_id } = filesState.selectedGen;
  const base = `/api/files/${encodeURIComponent(epoch_id)}/${encodeURIComponent(generation_id)}`;
  let payload;
  try {
    payload = await fetchJson(base + '/patches');
  } catch (err) {
    payload = { patches: [], error: String(err) };
  }
  const patches = payload.patches || [];
  if (patches.length === 0) {
    pane.appendChild(
      el('p', { class: 'empty' }, [
        payload.error || 'No patches — this is a seed generation.',
      ])
    );
    return;
  }
  const list = el('ul', { class: 'files-patch-list' });
  for (const patch of patches) {
    list.appendChild(
      el('li', { class: 'files-patch-item' }, [
        el('div', { class: 'files-patch-head' }, [
          el('span', { class: 'files-patch-id' }, [patch.id || '?']),
          el('span', { class: 'files-patch-op' }, [patch.op || '']),
          el('span', { class: 'files-patch-target' }, [
            patch.mutation_id || '',
          ]),
        ]),
        patch.rationale
          ? el('div', { class: 'files-patch-rationale' }, [patch.rationale])
          : null,
      ])
    );
  }
  pane.appendChild(list);
}

// --- Mutation-site browser (Files view) ----------------------------
//
// The mutation-site browser sits below the file tree and patch set. It
// reads /api/mutations/{epoch} for the epoch's mutation surface — every
// `# zicato:mutable` annotated span — and /api/mutations/{epoch}/{id}
// for one site's v0-baseline content plus the patched content in any
// generation whose patch touched that id. The detail pane renders a
// line-level diff of baseline vs patched; a site with no patch just
// shows its current (baseline) content.
//
// State is per-epoch: the index is fetched once per epoch, the detail
// lazily on site selection.

const mutationsState = {
  epochId: null,    // epoch the surface belongs to
  index: null,      // { mutations: [...] } for the epoch
  selectedId: null, // mutation_id of the open site
  detail: null,     // { baseline, versions: [...] } for the open site
};

// A minimal LCS line-diff: returns a list of { tag, text } rows where
// tag is 'same' | 'add' | 'del'. Used to render the baseline-vs-patched
// mutation-site diff without pulling in a diff library — the spans are
// small (a prompt string), so the O(n*m) table is cheap.
function diffLines(oldText, newText) {
  const a = (oldText || '').split('\n');
  const b = (newText || '').split('\n');
  const n = a.length;
  const m = b.length;
  // LCS length table.
  const lcs = [];
  for (let i = 0; i <= n; i++) lcs.push(new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j]
        ? lcs[i + 1][j + 1] + 1
        : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ tag: 'same', text: a[i] });
      i++; j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      rows.push({ tag: 'del', text: a[i] });
      i++;
    } else {
      rows.push({ tag: 'add', text: b[j] });
      j++;
    }
  }
  while (i < n) { rows.push({ tag: 'del', text: a[i] }); i++; }
  while (j < m) { rows.push({ tag: 'add', text: b[j] }); j++; }
  return rows;
}

async function renderMutationsView() {
  const listPane = $('mutations-list-pane');
  const detailPane = $('mutations-detail-pane');
  if (!listPane || !detailPane) return;

  if (!mutationsState.epochId) {
    clearChildren(listPane);
    listPane.appendChild(
      el('p', { class: 'empty' }, ['Select a generation to browse its mutation sites.'])
    );
    clearChildren(detailPane);
    detailPane.appendChild(
      el('p', { class: 'empty' }, ['Select a mutation site to view its diff.'])
    );
    return;
  }
  // Load the epoch's mutation surface once; cached across re-renders.
  if (mutationsState.index == null) {
    try {
      mutationsState.index = await fetchJson(
        '/api/mutations/' + encodeURIComponent(mutationsState.epochId)
      );
    } catch (err) {
      mutationsState.index = { mutations: [], error: String(err) };
    }
  }
  renderMutationsList();
  renderMutationsDetail();
}

// Left pane: the list of mutation sites for the epoch's baseline.
function renderMutationsList() {
  const pane = $('mutations-list-pane');
  if (!pane) return;
  clearChildren(pane);

  const index = mutationsState.index || { mutations: [] };
  const sites = index.mutations || [];
  if (sites.length === 0) {
    pane.appendChild(
      el('p', { class: 'empty' }, [
        index.error || 'No mutation sites in this epoch.',
      ])
    );
    return;
  }
  const list = el('ul', { class: 'mutations-list' });
  for (const site of sites) {
    const selected = mutationsState.selectedId === site.mutation_id;
    const patched = (site.patched_generation_ids || []).length > 0;
    list.appendChild(
      el('li', { class: 'mutations-list-item' }, [
        el('button', {
          type: 'button',
          class: 'mutations-site-button' + (selected ? ' active' : ''),
          onclick: () => selectMutationSite(site.mutation_id),
        }, [
          el('span', { class: 'mutations-site-id' }, [site.mutation_id]),
          el('span', { class: 'mutations-site-meta' }, [
            (site.role || site.kind || '') + ' · ' + (site.file || ''),
          ]),
          patched
            ? el('span', { class: 'mutations-site-badge' }, [
                site.patched_generation_ids.join(', '),
              ])
            : el('span', { class: 'mutations-site-badge unpatched' }, ['baseline']),
        ]),
      ])
    );
  }
  pane.appendChild(list);
}

async function selectMutationSite(mutationId) {
  mutationsState.selectedId = mutationId;
  mutationsState.detail = null;
  const url = '/api/mutations/'
    + encodeURIComponent(mutationsState.epochId) + '/'
    + encodeURIComponent(mutationId);
  try {
    mutationsState.detail = await fetchJson(url);
  } catch (err) {
    mutationsState.detail = { mutation_id: mutationId, error: String(err) };
  }
  renderMutationsList();
  renderMutationsDetail();
}

// Right pane: the selected site's baseline content and, per patching
// generation, a line-level diff of baseline vs patched content.
function renderMutationsDetail() {
  const pane = $('mutations-detail-pane');
  if (!pane) return;
  clearChildren(pane);

  if (!mutationsState.selectedId) {
    pane.appendChild(
      el('p', { class: 'empty' }, ['Select a mutation site to view its diff.'])
    );
    return;
  }
  const detail = mutationsState.detail;
  if (!detail) {
    pane.appendChild(el('p', { class: 'empty' }, ['Loading…']));
    return;
  }
  if (detail.error) {
    pane.appendChild(el('p', { class: 'empty' }, [detail.error]));
    return;
  }
  const baseline = detail.baseline || {};
  pane.appendChild(
    el('div', { class: 'mutations-detail-header' }, [
      el('span', { class: 'mutations-detail-id' }, [detail.mutation_id || '']),
      el('span', { class: 'mutations-detail-loc' }, [
        (baseline.role ? baseline.role + ' · ' : '')
          + (baseline.file || '')
          + (baseline.line_start
              ? ':' + baseline.line_start + '-' + baseline.line_end
              : ''),
      ]),
    ])
  );

  const versions = detail.versions || [];
  if (versions.length === 0) {
    // No patch ever touched this site — show the baseline content as-is.
    pane.appendChild(
      el('div', { class: 'mutations-version-label' }, [
        'v0 baseline — no patch has touched this site',
      ])
    );
    pane.appendChild(
      el('pre', { class: 'files-content-body' }, [baseline.content || ''])
    );
    return;
  }
  // One diff block per generation whose patch touched this id.
  for (const version of versions) {
    pane.appendChild(
      el('div', { class: 'mutations-version-label' }, [
        el('span', {}, ['v0 → ' + (version.generation_id || '?')]),
        version.op
          ? el('span', { class: 'mutations-version-op' }, [version.op])
          : null,
      ])
    );
    if (version.rationale) {
      pane.appendChild(
        el('div', { class: 'mutations-version-rationale' }, [version.rationale])
      );
    }
    if (version.error || version.content == null) {
      pane.appendChild(
        el('p', { class: 'empty' }, [
          version.error || 'No patched content available for this generation.',
        ])
      );
      continue;
    }
    pane.appendChild(renderMutationDiff(baseline.content || '', version.content));
  }
}

// Render a unified line-level diff of baseline vs patched span content.
function renderMutationDiff(baselineText, patchedText) {
  const rows = diffLines(baselineText, patchedText);
  const block = el('div', { class: 'mutations-diff' });
  for (const row of rows) {
    const sign = row.tag === 'add' ? '+' : (row.tag === 'del' ? '-' : ' ');
    block.appendChild(
      el('div', { class: 'mutations-diff-line ' + row.tag }, [
        el('span', { class: 'mutations-diff-sign' }, [sign]),
        el('span', { class: 'mutations-diff-text' }, [row.text]),
      ])
    );
  }
  return block;
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
      'dashboard build, or the runs have not started.',
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
    renderOverview();
  } else if (currentView === 'tree') {
    renderLineage();
    renderTrajectory();
  } else if (currentView === 'tournament') {
    renderTournamentView();
  } else if (currentView === 'epoch') {
    renderEpochView();
  } else if (currentView === 'files') {
    renderFilesView();
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

// --- Module exports — the entry points app.js drives.
export {
  renderAll, renderActiveView, renderHeader, renderFooter, showView,
  applyRoute, setupLineageInteractions, closeDrill,
  renderLogTail, appendLogTail,
  // Files view — exported for the JS test harness (the route-driven
  // entry point and the scratch state it resolves into).
  applyFilesRoute, filesState,
};
