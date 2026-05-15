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

// --- AppState

class AppState {
  constructor() {
    this.connected = false;
    this.connecting = true;
    this.mock = false;
    this.heartbeat = null;          // { timestamp, pid, instance_id }
    this.activeRuns = [];           // [{ run_id, entry_id, generation_id, started_at, progress, drift_kinds: {kind: count} }]
    this.activeTournament = null;   // see schema in mock-state
    this.lineage = { generations: [], experiments: [] };
    this.experiments = [];
    this.logLines = [];             // {ts, line, level}
    this.supervisor = { version: '—', port: '—', build: '—' };
    this.scoring = { margin: DEFAULT_MARGIN };
    this.epoch = { id: '—', generation: '—', round: '—', startedAt: null };
  }

  setSnapshot(snap) {
    if (!snap) return;
    if (snap.heartbeat) this.heartbeat = snap.heartbeat;
    if (snap.active_runs) this.activeRuns = snap.active_runs;
    if ('active_tournament' in snap) this.activeTournament = snap.active_tournament;
    if (snap.lineage) this.lineage = snap.lineage;
    if (snap.experiments) this.experiments = snap.experiments;
    if (snap.supervisor) Object.assign(this.supervisor, snap.supervisor);
    if (snap.scoring) Object.assign(this.scoring, snap.scoring);
    if (snap.epoch) Object.assign(this.epoch, snap.epoch);
    if (Array.isArray(snap.log_tail)) {
      this.logLines = snap.log_tail.slice(-MAX_LOG_LINES);
    }
  }

  appendLog(line) {
    this.logLines.push(line);
    while (this.logLines.length > MAX_LOG_LINES) this.logLines.shift();
  }
}

const state = new AppState();

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

function renderHeader() {
  $('epoch-id').textContent = 'epoch · ' + (state.epoch.id || '—');
  $('generation-id').textContent = 'gen · ' + (state.epoch.generation || '—');
  $('round-id').textContent = 'round · ' + (state.epoch.round || '—');
  const startedAt = state.epoch.startedAt || (state.heartbeat && state.heartbeat.epoch_started_at);
  if (startedAt) {
    const seconds = (Date.now() - new Date(startedAt).getTime()) / 1000;
    $('elapsed').textContent = fmtDuration(seconds);
  }

  const badge = $('health-badge');
  badge.classList.remove('ok', 'warn', 'error', 'pending');
  if (state.connecting) {
    badge.classList.add('pending');
    badge.textContent = 'connecting';
  } else if (state.connected) {
    // Check heartbeat freshness (must be within 15s)
    const fresh = state.heartbeat
      && (Date.now() - new Date(state.heartbeat.timestamp).getTime()) < 15_000;
    if (fresh) {
      badge.classList.add('ok');
      badge.textContent = 'healthy';
    } else {
      badge.classList.add('warn');
      badge.textContent = 'stale heartbeat';
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
  $('supervisor-version').textContent = 'supervisor · ' + state.supervisor.version;
  $('supervisor-port').textContent = 'port · ' + state.supervisor.port;
  $('supervisor-build').textContent = 'build · ' + state.supervisor.build;
}

// --- Render: active tournament

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

  title.textContent =
    `Tournament — round ${t.round || '?'} of ${t.total_rounds || '?'}`;

  const subheader = el('p', { class: 'panel-subheader' }, [
    el('strong', null, [`${t.parent_id || '?'} (parent) vs ${t.child_id || '?'} (proposed)`]),
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

  // Entry rows
  const entriesWrap = el('div', { class: 'entries', role: 'list' });
  for (const e of t.entries) {
    entriesWrap.appendChild(renderEntryRow(e));
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
    const r = entry.runtime || {};
    const pct = typeof r.percent === 'number' ? Math.max(0, Math.min(100, r.percent)) : 0;
    const drift = entry.child && entry.child.drift_kinds
      ? Object.entries(entry.child.drift_kinds).map(([k, v]) => `${v} ${k}`).join(', ')
      : '';
    const elapsed = r.elapsed_seconds != null ? fmtDuration(r.elapsed_seconds) : '—';
    const budget = r.budget_seconds != null ? fmtDuration(r.budget_seconds) : '—';
    childCell.appendChild(el('span', { class: 'mono' }, [
      `RUNNING ${elapsed}/${budget}`,
    ]));
    if (drift) {
      childCell.appendChild(el('div', null, [
        el('span', { class: 'meta' }, [`drift: ${drift}`]),
      ]));
    }
    const prog = el('div', { class: 'progress', role: 'progressbar',
                 'aria-valuemin': '0', 'aria-valuemax': '100',
                 'aria-valuenow': String(pct) });
    prog.appendChild(el('div', { class: 'bar', style: `width:${pct}%` }));
    childCell.appendChild(prog);
    childCell.appendChild(el('span', { class: 'meta' }, [` ${pct}%`]));
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
  // All buttons disabled in v1.2 — the wiring lands in v1.3. The
  // POST handlers are present so flipping `disabled` is the only diff.
  const tip = 'feature pending v1.3';
  const buttons = [
    { label: 'Pause epoch', action: () => postControl('pause', null) },
    { label: 'Skip round',  action: () => postControl('skip-round', null) },
    { label: 'Force-kill running', action: () => {
      const running = (state.activeTournament && state.activeTournament.entries || [])
        .find(e => e.status === 'running');
      if (running && running.run_id) postControl('kill/' + encodeURIComponent(running.run_id), null);
    }, cls: 'danger' },
    { label: 'Override', action: () => alert('Override is reserved — needs operator confirmation; v1.3.') },
  ];
  const row = el('div', { class: 'button-row', role: 'toolbar', 'aria-label': 'Tournament controls' });
  for (const b of buttons) {
    row.appendChild(el('button', {
      type: 'button',
      class: 'btn ' + (b.cls || ''),
      disabled: 'disabled',
      title: tip,
      'aria-label': b.label + ' (' + tip + ')',
      onClick: (ev) => {
        ev.preventDefault();
        alert(tip);
      },
    }, [b.label]));
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
    const pct = typeof r.percent === 'number' ? Math.max(0, Math.min(100, r.percent)) : 0;
    const prog = el('div', { class: 'progress', role: 'progressbar',
                 'aria-valuemin': '0', 'aria-valuemax': '100',
                 'aria-valuenow': String(pct) });
    prog.appendChild(el('div', { class: 'bar', style: `width:${pct}%` }));
    card.appendChild(prog);
    if (r.started_at) {
      const elapsed = (Date.now() - new Date(r.started_at).getTime()) / 1000;
      card.appendChild(el('div', { class: 'run-meta mono' }, [
        `${fmtDuration(elapsed)}${r.budget_seconds ? ' / ' + fmtDuration(r.budget_seconds) : ''}`,
      ]));
    }
    wrap.appendChild(card);
  }
}

// --- Render: lineage SVG

function renderLineage() {
  const svg = $('lineage-svg');
  clearChildren(svg);

  const gens = state.lineage.generations || [];
  const exps = state.lineage.experiments || [];
  const expByGen = new Map();
  for (const e of exps) expByGen.set(e.generation_id, e);

  if (gens.length === 0) {
    const w = 900, h = 360;
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
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

  const n = gens.length;
  const width = 900;
  const height = 360;
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);

  const marginX = 36, marginY = 50;
  const usableW = width - 2 * marginX;
  const usableH = height - 2 * marginY;
  const nodeW = 130, nodeH = 56;
  const xStep = n === 1 ? 0 : (usableW - nodeW) / (n - 1);
  const centerY = marginY + usableH / 2 - nodeH / 2;
  const branchOffset = Math.min(usableH / 3, 90);

  const positions = new Map();
  for (let i = 0; i < n; i++) {
    const g = gens[i];
    const x = marginX + i * xStep;
    const exp = expByGen.get(g.id);
    const decision = exp && exp.outcome ? exp.outcome.tournament_decision : null;
    const y = (!exp || !exp.outcome || decision === 'promoted')
      ? centerY
      : centerY + branchOffset;
    positions.set(g.id, { x, y });
  }

  // <defs> with arrow markers
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
  ]);
  svg.appendChild(defs);

  // Edges
  for (const g of gens) {
    if (!g.parent_id || !positions.has(g.parent_id)) continue;
    const pp = positions.get(g.parent_id);
    const cp = positions.get(g.id);
    const exp = expByGen.get(g.id);
    const decision = exp && exp.outcome ? exp.outcome.tournament_decision : 'pending';
    let stroke = COLORS.baseline, strokeW = 1.4, dash = null, marker = null, label = 'pending';
    if (decision === 'promoted') {
      stroke = COLORS.promoted; strokeW = 2.6; marker = 'url(#arr-promoted)'; label = 'promoted';
    } else if (decision === 'rejected') {
      stroke = COLORS.rejected; strokeW = 1.4; dash = '5 4'; marker = 'url(#arr-rejected)';
      label = (exp && exp.outcome && exp.outcome.rejection_reason) || 'rejected';
    } else if (decision === 'deferred') {
      stroke = COLORS.deferred; strokeW = 1.6; dash = '2 3'; label = 'deferred';
    }
    const x1 = pp.x + nodeW;
    const y1 = pp.y + nodeH / 2;
    const x2 = cp.x;
    const y2 = cp.y + nodeH / 2;
    const cx1 = x1 + (x2 - x1) * 0.45;
    const cx2 = x1 + (x2 - x1) * 0.55;
    const path = svgEl('path', {
      class: 'lineage-edge',
      d: `M ${x1.toFixed(1)} ${y1.toFixed(1)} C ${cx1.toFixed(1)} ${y1.toFixed(1)}, ${cx2.toFixed(1)} ${y2.toFixed(1)}, ${x2.toFixed(1)} ${y2.toFixed(1)}`,
      fill: 'none',
      stroke,
      'stroke-width': strokeW,
      'stroke-dasharray': dash,
      'marker-end': marker,
      'data-from': g.parent_id,
      'data-to': g.id,
    });
    svg.appendChild(path);
    const midX = (x1 + x2) / 2;
    const midY = (y1 + y2) / 2 - 6;
    svg.appendChild(svgEl('text', {
      class: 'svg-axis', x: midX.toFixed(1), y: midY.toFixed(1),
      'text-anchor': 'middle',
    }, [truncate(label, 24)]));
  }

  // Nodes
  for (const g of gens) {
    const { x, y } = positions.get(g.id);
    const exp = expByGen.get(g.id);
    const decision = exp && exp.outcome ? exp.outcome.tournament_decision : 'baseline';
    let fill, stroke, dash = null, marker = '(seed)';
    if (!g.parent_id) {
      fill = 'rgba(110, 118, 129, 0.12)'; stroke = COLORS.baseline; marker = '(seed)';
    } else if (decision === 'promoted') {
      fill = 'rgba(46, 160, 67, 0.18)'; stroke = COLORS.promoted; marker = '[+]';
    } else if (decision === 'rejected') {
      fill = 'rgba(215, 58, 73, 0.16)'; stroke = COLORS.rejected; dash = '5 4'; marker = '[x]';
    } else if (decision === 'deferred') {
      fill = 'rgba(191, 135, 0, 0.18)'; stroke = COLORS.deferred; dash = '2 3'; marker = '[=]';
    } else {
      fill = 'rgba(110, 118, 129, 0.12)'; stroke = COLORS.baseline; marker = '(pending)';
    }
    const groupAttrs = {
      class: 'lineage-node',
      'data-gen': g.id,
      role: 'button',
      tabindex: '0',
      'aria-label': `generation ${g.id} ${decision}`,
    };
    const grp = svgEl('g', groupAttrs);
    grp.addEventListener('click', () => openDrillForGeneration(g.id));
    grp.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openDrillForGeneration(g.id);
      }
    });
    grp.appendChild(svgEl('rect', {
      x: x.toFixed(1), y: y.toFixed(1), width: nodeW, height: nodeH,
      rx: 8, ry: 8, fill, stroke, 'stroke-width': 1.8,
      'stroke-dasharray': dash,
    }));
    grp.appendChild(svgEl('text', {
      class: 'svg-label',
      x: (x + nodeW / 2).toFixed(1), y: (y + 18).toFixed(1),
      'text-anchor': 'middle', 'font-weight': '600',
    }, [`${g.id} ${marker}`]));
    if (exp && exp.outcome) {
      grp.appendChild(svgEl('text', {
        class: 'svg-axis',
        x: (x + nodeW / 2).toFixed(1), y: (y + 34).toFixed(1),
        'text-anchor': 'middle',
      }, [`Δ scalar ${fmtDelta(exp.outcome.scalar_score_delta)}`]));
      grp.appendChild(svgEl('text', {
        class: 'svg-axis',
        x: (x + nodeW / 2).toFixed(1), y: (y + 48).toFixed(1),
        'text-anchor': 'middle',
      }, [`Δ drift ${fmtDelta(exp.outcome.drift_loss_delta)}`]));
    } else {
      grp.appendChild(svgEl('text', {
        class: 'svg-axis',
        x: (x + nodeW / 2).toFixed(1), y: (y + 40).toFixed(1),
        'text-anchor': 'middle',
      }, ['baseline']));
    }
    svg.appendChild(grp);
  }
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
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  if (gens.length === 0) {
    svg.appendChild(svgEl('text', {
      class: 'svg-axis', x: width / 2, y: height / 2, 'text-anchor': 'middle',
    }, ['No generations to plot.']));
    return;
  }

  const series = gens.map((g, i) => {
    const exp = expByGen.get(g.id);
    if (!exp || !exp.outcome) return { i, id: g.id, decision: 'baseline', v: 0 };
    return {
      i, id: g.id,
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
    const e = expByGen.get(g.id);
    return e && e.outcome && e.outcome.tournament_decision === 'promoted';
  });

  const cellSize = 28, innerPad = 4;
  const labelW = 150;

  const cellValue = new Map();
  const kindMaxAbs = new Map();
  for (const g of promotedGens) {
    const e = expByGen.get(g.id);
    if (!e || !e.outcome || !e.outcome.drift_movements) continue;
    for (const mv of e.outcome.drift_movements) {
      cellValue.set(mv.kind + ' ' + g.id, mv.to_rate);
      const delta = Math.abs(mv.to_rate - mv.from_rate);
      const cur = kindMaxAbs.get(mv.kind) || 0;
      if (delta > cur) kindMaxAbs.set(mv.kind, delta);
    }
  }

  if (promotedGens.length === 0 || kindMaxAbs.size === 0) {
    const w = cellSize * 12 + 180;
    const h = cellSize * 2 + 60;
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
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
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);

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
    }, [promotedGens[i].id]));
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
      const v = cellValue.get(kind + ' ' + promotedGens[c].id);
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

// --- Render: experiments list

function renderExperiments() {
  const wrap = $('experiments-list');
  clearChildren(wrap);
  const exps = state.experiments || state.lineage.experiments || [];
  if (exps.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No experiments recorded.']));
    return;
  }
  // newest first; first three open
  const reversed = [...exps].reverse();
  reversed.forEach((exp, i) => {
    wrap.appendChild(renderExperimentCard(exp, i < 3));
  });
}

function renderExperimentCard(exp, openCard) {
  const decision = (exp.outcome && exp.outcome.tournament_decision) || 'pending';
  const badgeCls = ['promoted', 'rejected', 'deferred'].includes(decision) ? decision : 'pending';
  const details = el('details', { class: 'experiment-card' });
  if (openCard) details.setAttribute('open', '');
  const summary = el('summary');
  summary.appendChild(el('span', { class: 'badge ' + badgeCls }, [decision]));
  summary.appendChild(el('span', { class: 'mono code-pill' }, [exp.generation_id]));
  summary.appendChild(document.createTextNode(' ' + truncate(
    (exp.hypothesis && exp.hypothesis.core_idea) || '(no hypothesis)', 80)));
  if (exp.outcome) {
    summary.appendChild(el('span', { class: 'delta-label' }, [' Δ scalar ']));
    summary.appendChild(el('span', { class: 'mono' }, [fmtDelta(exp.outcome.scalar_score_delta)]));
  }
  details.appendChild(summary);

  const body = el('div', { class: 'card-body' });
  if (exp.hypothesis) {
    body.appendChild(el('h4', null, ['Hypothesis']));
    if (exp.hypothesis.core_idea) {
      body.appendChild(el('p', null, [
        el('strong', null, ['core idea. ']),
        exp.hypothesis.core_idea,
      ]));
    }
    if (exp.hypothesis.why) {
      body.appendChild(el('p', null, [el('strong', null, ['why. ']), exp.hypothesis.why]));
    }
    if (exp.hypothesis.risks) {
      body.appendChild(el('p', null, [el('strong', null, ['risks. ']), exp.hypothesis.risks]));
    }
  }
  if (exp.outcome) {
    const out = exp.outcome;
    const deltas = el('div', { class: 'deltas' });
    deltas.appendChild(el('span', { class: 'delta' }, [
      el('span', { class: 'delta-label' }, ['Δ pass_rate ']),
      fmtDelta(out.pass_rate_delta),
    ]));
    deltas.appendChild(el('span', { class: 'delta' }, [
      el('span', { class: 'delta-label' }, ['Δ drift_loss ']),
      fmtDelta(out.drift_loss_delta),
    ]));
    deltas.appendChild(el('span', { class: 'delta' }, [
      el('span', { class: 'delta-label' }, ['Δ scalar ']),
      fmtDelta(out.scalar_score_delta),
    ]));
    body.appendChild(deltas);
    if (out.tournament_decision === 'rejected' && out.rejection_reason) {
      body.appendChild(el('p', null, [
        el('strong', null, ['rejection reason. ']),
        out.rejection_reason,
      ]));
    }
  }
  details.appendChild(body);
  return details;
}

// --- Render: log tail

function renderLogTail() {
  const wrap = $('log-tail');
  clearChildren(wrap);
  if (!state.logLines || state.logLines.length === 0) {
    wrap.appendChild(el('p', { class: 'empty' }, ['No events yet.']));
    return;
  }
  for (const line of state.logLines) {
    const lineEl = el('div', { class: 'log-line fade-in' });
    if (line.ts) lineEl.appendChild(el('span', { class: 'ts' }, [line.ts]));
    const lvl = (line.level || '').toLowerCase();
    const cls = lvl === 'error' ? 'ev-error' : lvl === 'warn' ? 'ev-warn' : lvl === 'ok' ? 'ev-ok' : '';
    lineEl.appendChild(el('span', { class: cls }, [String(line.message || '')]));
    wrap.appendChild(lineEl);
  }
  wrap.scrollTop = wrap.scrollHeight;
}

// --- Drill-down panels (hash router)

function openDrillForGeneration(genId) {
  location.hash = '#/generation/' + encodeURIComponent(genId);
}
function openDrillForEntry(entry) {
  location.hash = '#/entry/' + encodeURIComponent(entry.entry_id);
}
function openDrillForRun(run) {
  location.hash = '#/run/' + encodeURIComponent(run.run_id);
}
function closeDrill() {
  location.hash = '';
}

function applyRoute() {
  const hash = location.hash;
  const panel = $('drill-panel');
  const title = $('drill-title');
  const body = $('drill-body');
  clearChildren(body);

  if (!hash || hash === '#' || hash === '') {
    panel.setAttribute('aria-hidden', 'true');
    title.textContent = 'Detail';
    return;
  }

  const m = hash.match(/^#\/(generation|entry|run)\/(.+)$/);
  if (!m) {
    panel.setAttribute('aria-hidden', 'true');
    return;
  }
  const kind = m[1];
  const id = decodeURIComponent(m[2]);
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
    const t = state.activeTournament;
    const entry = t && (t.entries || []).find(e => e.entry_id === id);
    if (!entry) {
      body.appendChild(el('p', { class: 'empty' }, ['Entry not found in current tournament.']));
      return;
    }
    const meta = el('p', null, [
      'Status: ', el('strong', null, [entry.status || 'queued']),
    ]);
    body.appendChild(meta);
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
    body.appendChild(el('p', { class: 'meta' }, [
      'Live events feed lands when GET /api/run/{run_id}/events ships in v1.3.',
    ]));
    return;
  }
}

// --- Top-level render

function renderAll() {
  renderHeader();
  renderActiveTournament();
  renderActiveRuns();
  renderLineage();
  renderTrajectory();
  renderHeatmap();
  renderExperiments();
  renderLogTail();
  renderFooter();
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
}

async function refreshAfterEvent(payload) {
  // Most state_change events name a region; if missing, refresh all.
  const region = (payload && payload.region) || 'all';
  try {
    if (region === 'all') {
      await loadFullState();
      return;
    }
    if (region === 'tournament') {
      const t = await fetchJson('/api/active-tournament');
      state.activeTournament = t;
    } else if (region === 'runs') {
      const r = await fetchJson('/api/active-runs');
      state.activeRuns = r;
    } else if (region === 'lineage') {
      const l = await fetchJson('/api/lineage');
      state.lineage = l;
    } else {
      await loadFullState();
      return;
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
      state.heartbeat = JSON.parse(ev.data);
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

function mockSnapshot() {
  return {
    epoch: {
      id: 'initial',
      generation: 'v3',
      round: '4',
      startedAt: new Date(Date.now() - 4 * 60_000 - 23_000).toISOString(),
    },
    heartbeat: { timestamp: new Date().toISOString(), pid: 12345, instance_id: 'mock' },
    supervisor: { version: '1.2.0', port: '7892', build: 'mock' },
    scoring: { margin: 0.05 },
    active_runs: [
      { run_id: 'r-9c2a', entry_id: 'research_topic_q3', generation_id: 'v4',
        started_at: new Date(Date.now() - 42_000).toISOString(),
        budget_seconds: 180, percent: 23 },
    ],
    active_tournament: {
      round: 4, total_rounds: 5,
      parent_id: 'v3', child_id: 'v4',
      elapsed_seconds: 263,
      hypothesis: {
        core_idea: 'Compress researcher tool descriptions to under 80 tokens each to reduce context bloat without dropping signal.',
        modulating: ['researcher_tool_descriptions', 'write_webpage_tool'],
      },
      entries: [
        { entry_id: 'extract_invoice_001', status: 'done',
          parent: { drift_loss: 0.23, pass: true },
          child:  { drift_loss: 0.18, pass: true } },
        { entry_id: 'extract_invoice_002', status: 'done',
          parent: { drift_loss: 0.31, pass: true },
          child:  { drift_loss: 0.45, pass: false } },
        { entry_id: 'research_topic_q3', status: 'running',
          run_id: 'r-9c2a',
          parent: { drift_loss: 0.19, pass: true },
          child:  { drift_loss: 0.0, pass: null, drift_kinds: { off_topic: 2 } },
          runtime: { elapsed_seconds: 42, budget_seconds: 180, percent: 23 } },
        { entry_id: 'multi_turn_picky', status: 'queued' },
        { entry_id: 'schema_response', status: 'queued' },
      ],
    },
    lineage: {
      generations: [
        { id: 'v0', parent_id: null },
        { id: 'v1', parent_id: 'v0' },
        { id: 'v2', parent_id: 'v1' },
        { id: 'v3', parent_id: 'v2' },
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
        { generation_id: 'v3', hypothesis: { core_idea: 'Add picky retry pass.' },
          outcome: { tournament_decision: 'promoted',
               scalar_score_delta: -0.020, drift_loss_delta: -0.03,
               pass_rate_delta: 0.02, drift_movements: [
                 { kind: 'off_topic', from_rate: 0.18, to_rate: 0.15 },
                 { kind: 'schema_violation', from_rate: 0.10, to_rate: 0.08 },
               ]}},
      ],
    },
    experiments: [
      { generation_id: 'v1', hypothesis: { core_idea: 'Tighten extraction schema.', why: 'Schema drift was the dominant kind in v0.', risks: 'May reject borderline-valid responses.' },
        patches: [{ mutation_id: 'm1', op: 'replace', rationale: 'narrow allowed types' }],
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.080, drift_loss_delta: -0.06, pass_rate_delta: 0.10 }},
      { generation_id: 'v2', hypothesis: { core_idea: 'Move JSON validation earlier.', why: 'Pipeline ordering issue.', risks: '' },
        patches: [{ mutation_id: 'm2', op: 'reorder', rationale: 'validate-before-emit' }],
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.040, drift_loss_delta: -0.04, pass_rate_delta: 0.05 }},
      { generation_id: 'v3', hypothesis: { core_idea: 'Add picky retry pass.', why: 'Borderline rejections leak through.', risks: 'Extra cost.' },
        patches: [{ mutation_id: 'm3', op: 'insert', rationale: 'retry on first-pass fail' }],
        outcome: { tournament_decision: 'promoted', scalar_score_delta: -0.020, drift_loss_delta: -0.03, pass_rate_delta: 0.02 }},
    ],
    log_tail: [
      { ts: '12:34:50', level: 'info', message: 'tournament r4 entry research_topic_q3 started (run r-9c2a)' },
      { ts: '12:35:01', level: 'info', message: 'goldfive driver: tool researcher_search invoked' },
      { ts: '12:35:14', level: 'warn', message: 'drift detected: off_topic +1 in run r-9c2a' },
      { ts: '12:35:23', level: 'ok',   message: 'parent v3 entry extract_invoice_002 pass' },
    ],
  };
}

// --- Bootstrap

function init() {
  // Drill panel close button + escape
  $('drill-close').addEventListener('click', closeDrill);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') closeDrill();
  });
  window.addEventListener('hashchange', applyRoute);

  // Tick the elapsed-time fields once per second so the header reads "live"
  // even when no state changes arrive.
  setInterval(() => {
    renderHeader();
    // Also tick the tournament elapsed if running
    const t = state.activeTournament;
    if (t && t.entries) {
      const tEl = $('tournament-elapsed');
      if (t.elapsed_seconds != null) {
        tEl.textContent = 'Elapsed ' + fmtDuration(t.elapsed_seconds + 1);
        t.elapsed_seconds += 1;
      }
    }
  }, 1000);

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
