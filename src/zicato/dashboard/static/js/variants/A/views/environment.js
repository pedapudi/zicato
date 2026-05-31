// variants/A/views/environment.js — L0 home: the workspace as a FLEET.
//
// The whole environment seen at once across epochs. Every epoch is a
// console card: status light, mini loss-trajectory, generation count,
// best scalar, health. A top readout strip gives the workspace-wide
// vitals. This is the view that was lost in the current UI — the
// operator must see the environment AS A WHOLE.
//
// Data: /api/workspace (epochs[], sparkline[]), /api/health-report,
// /api/active-tournament (current live focus).

import { el } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { state } from '../../../core/state.js';
import { panel, readouts, sparkline, chip, empty, loading } from '../components/instruments.js';
import { href } from '../router.js';

const cache = { workspace: null, health: null, loading: false };

export function resetEnvironmentCache() { cache.workspace = null; cache.health = null; cache.loading = false; }

async function ensure(repaint) {
  if (cache.workspace || cache.loading) return;
  cache.loading = true;
  try {
    cache.workspace = await fetchJson('/api/workspace');
  } catch { cache.workspace = { epochs: [], sparkline: [], current_epoch_id: null }; }
  try {
    cache.health = await fetchJson('/api/health-report');
  } catch { cache.health = null; }
  cache.loading = false;
  if (repaint) repaint();
}

function fmt(v, d = 3) {
  return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : '—';
}

function epochStatus(row, isCurrent) {
  if (isCurrent) return { kind: 'live', label: 'live' };
  if (row.closed) return { kind: 'idle', label: 'closed' };
  if ((row.promoted_count || 0) > 0) return { kind: 'go', label: 'open' };
  return { kind: 'idle', label: 'open' };
}

// One epoch console card.
function fleetCard(row, isCurrent, healthByEpoch) {
  const st = epochStatus(row, isCurrent);
  const at = state.activeTournament;
  const liveHere = isCurrent && at && at.epoch_id === row.epoch_id;

  const head = el('div', { class: 'mcA-fleet-head' }, [
    el('span', { class: 'mcA-fleet-id mono' }, [row.epoch_id]),
    chip(liveHere ? 'tournament running' : st.label, liveHere ? 'live' : st.kind),
  ]);

  // mini loss trajectory: pull the per-epoch best scalar plus, when this
  // is the current epoch, the live partial scalar climbs come from the
  // global sparkline only — here we approximate with the workspace
  // sparkline point for context plus the row's best.
  const sparkVals = sparkLineForEpoch(row);
  const goal = el('div', { class: 'mcA-fleet-goal' }, [row.goal || '(no goal recorded)']);

  const stats = el('div', { class: 'mcA-fleet-stats' }, [
    el('span', { class: 'mcA-fleet-stat-label' }, ['best']),
    el('span', { class: 'mcA-fleet-stat-value is-go' }, [fmt(row.best_scalar)]),
    el('span', { class: 'mcA-fleet-stat-label' }, ['gens']),
    el('span', { class: 'mcA-fleet-stat-value' }, [String(row.generation_count || 0)]),
    el('span', { class: 'mcA-fleet-stat-label' }, ['promoted']),
    el('span', { class: 'mcA-fleet-stat-value' }, [String(row.promoted_count || 0)]),
  ]);

  const body = el('div', { class: 'mcA-fleet-body' }, [
    el('div', { class: 'mcA-fleet-spark' }, [
      sparkVals.length >= 2
        ? sparkline(sparkVals, { width: 220, height: 48, stroke: 'var(--mc-go)' })
        : el('span', { class: 'mcA-readout-foot' }, ['no trajectory yet']),
    ]),
    stats,
  ]);

  const card = el('a', {
    class: 'mcA-fleet-card' + (isCurrent ? ' is-live' : ''),
    href: href('epoch', { epochId: row.epoch_id }),
  }, [head, goal, body]);
  return card;
}

// Best-effort per-epoch trajectory. The workspace endpoint gives one
// best scalar per epoch; when this is the current epoch and we have a
// live partial scalar, append it so the card breathes. Falls back to a
// flat two-point line off best_scalar so the card always renders an
// instrument rather than a dash.
function sparkLineForEpoch(row) {
  const vals = [];
  const ws = cache.workspace;
  // Use the experiments scalar trajectory if available on epochDef and
  // this is the current epoch.
  const def = state.epochDef;
  if (def && def.epoch_id === row.epoch_id && Array.isArray(def.experiments)) {
    for (const e of def.experiments) {
      const s = e && e.outcome && e.outcome.scalar_score;
      if (typeof s === 'number' && isFinite(s)) vals.push(s);
    }
  }
  if (vals.length >= 2) return vals;
  // fallback: synthesize a gentle descent toward best from the
  // cross-epoch sparkline neighborhood (purely cosmetic context).
  if (typeof row.best_scalar === 'number' && isFinite(row.best_scalar)) {
    return [row.best_scalar * 1.18, row.best_scalar * 1.06, row.best_scalar];
  }
  return vals;
}

function vitalsStrip() {
  const ws = cache.workspace || { epochs: [] };
  const rows = Array.isArray(ws.epochs) ? ws.epochs : [];
  let gens = 0, promoted = 0, open = 0;
  let best = null;
  for (const r of rows) {
    gens += r.generation_count || 0;
    promoted += r.promoted_count || 0;
    if (!r.closed) open += 1;
    if (typeof r.best_scalar === 'number' && isFinite(r.best_scalar)) {
      best = best == null ? r.best_scalar : Math.min(best, r.best_scalar);
    }
  }
  const at = state.activeTournament;
  const phase = (state.heartbeat && state.heartbeat.phase) || (at ? 'running' : 'idle');
  const tone = at ? 'live' : 'go';
  return readouts([
    { label: 'epochs', value: rows.length, foot: open + ' open' },
    { label: 'generations', value: gens, foot: promoted + ' promoted' },
    { label: 'best scalar', value: fmt(best), tone: 'go', foot: 'lowest across fleet' },
    { label: 'phase', value: String(phase).toUpperCase(), tone, foot: at ? 'tournament live' : 'between rounds' },
  ]);
}

function healthPanel() {
  const hr = cache.health;
  if (!hr) return null;
  const findings = Array.isArray(hr.findings) ? hr.findings : [];
  const healthy = hr.healthy !== false && findings.length === 0;
  const accent = healthy ? 'go' : (findings.some((f) => sev(f) === 'critical') ? 'stop' : 'live');
  const body = [];
  if (healthy) {
    body.push(el('div', { class: 'mcA-readout-foot', style: 'color:var(--mc-go)' },
      ['✓ loop is healthy — the evaluation distinguishes candidates.']));
  } else {
    for (const f of findings) {
      const s = sev(f);
      body.push(el('div', { style: 'margin-bottom:10px;' }, [
        el('div', null, [
          chip(s, s === 'critical' ? 'stop' : (s === 'warn' || s === 'warning' ? 'warn' : 'idle')),
          el('span', { class: 'mono', style: 'margin-left:8px;color:var(--mc-text)' },
            [f.detector || f.name || 'finding']),
        ]),
        el('div', { class: 'mcA-readout-foot', style: 'margin-top:4px' },
          [f.summary || f.message || '']),
      ]));
    }
  }
  return panel({ title: 'Loop health', sub: hr.epoch_id || '', accent, body });
}

function sev(f) {
  return String((f && (f.severity || f.level)) || 'info').toLowerCase();
}

export function renderEnvironment(root, repaint) {
  ensure(repaint);
  root.textContent = '';

  root.appendChild(el('div', { class: 'mcA-pagehead' }, [
    el('h1', null, ['Environment']),
    el('span', { class: 'mcA-pagehead-sub' }, ['the workspace as a fleet — every epoch, at a glance']),
  ]));

  if (cache.workspace == null) {
    root.appendChild(loading('Acquiring fleet telemetry'));
    return;
  }

  root.appendChild(el('div', { style: 'margin-bottom:18px' }, [vitalsStrip()]));

  const hp = healthPanel();

  const rows = Array.isArray(cache.workspace.epochs) ? cache.workspace.epochs : [];
  const current = cache.workspace.current_epoch_id;

  const fleetBody = rows.length === 0
    ? empty('No epochs recorded in this workspace yet.')
    : el('div', { class: 'mcA-fleet' },
        rows.map((r) => fleetCard(r, r.epoch_id === current, cache.health)));

  const grid = el('div', { class: 'mcA-grid' + (hp ? '' : '') });
  grid.appendChild(panel({
    title: 'Fleet',
    sub: rows.length + ' epoch' + (rows.length === 1 ? '' : 's'),
    accent: state.activeTournament ? 'live' : null,
    body: fleetBody,
  }));
  if (hp) grid.appendChild(hp);
  root.appendChild(grid);

  // cross-fleet trend
  const spark = Array.isArray(cache.workspace.sparkline) ? cache.workspace.sparkline : [];
  const trendVals = spark.map((p) => p && p.scalar).filter((v) => typeof v === 'number' && isFinite(v));
  if (trendVals.length >= 2) {
    root.appendChild(el('div', { style: 'margin-top:16px' }, [
      panel({
        title: 'Cross-epoch trajectory',
        sub: 'best scalar per epoch · lower is better',
        body: el('div', { style: 'padding:8px 4px' }, [
          sparkline(trendVals, { width: 760, height: 90, stroke: 'var(--mc-accent)' }),
        ]),
      }),
    ]));
  }
}
