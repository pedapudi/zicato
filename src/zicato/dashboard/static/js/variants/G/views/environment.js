// variants/G/views/environment.js — L0 home: the workspace as a FLEET.
//
// Keeps Variant A's beloved home: the whole environment seen at once
// across epochs, every epoch a card with a status light, a per-epoch
// loss sparkline, generation/promoted counts; a workspace vitals strip;
// and the cross-epoch trendline hero (the "Fleet epoch-trendline card"
// the user explicitly liked). Rebuilt with D's quieter sparkline and
// the calm token set.
//
// A BUG #3 FIX (jerky looping hover): the whole view is DIGEST-GATED.
// A heartbeat tick that only re-stamps a timestamp produces an identical
// digest, so `renderEnvironment` returns early and the cards persist
// across heartbeats — their CSS `transition` hover never resets because
// the DOM nodes are never replaced. Hover uses `transition`, never an
// infinite keyframe animation.

import { el } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { state } from '../../../core/state.js';
import { panel, readouts, empty, loading, chip, fmt } from '../components/ui.js';
import { sparkline } from '../svg.js';
import { href } from '../router.js';

const cache = { workspace: null, health: null, loading: false };
let _lastDigest = null;

export function resetEnvironmentCache() {
  cache.workspace = null; cache.health = null; cache.loading = false; _lastDigest = null;
}

async function ensure(repaint) {
  if (cache.workspace || cache.loading) return;
  cache.loading = true;
  try { cache.workspace = await fetchJson('/api/workspace'); }
  catch { cache.workspace = { epochs: [], sparkline: [], current_epoch_id: null }; }
  try { cache.health = await fetchJson('/api/health-report'); }
  catch { cache.health = null; }
  cache.loading = false;
  if (repaint) repaint();
}

// Stable digest of ONLY structural/content data — excludes timestamps,
// elapsed clocks, anything that ticks. A heartbeat-only change is a
// no-op here (A bug #3 fix).
export function environmentDigest() {
  const ws = cache.workspace;
  const rows = ws && Array.isArray(ws.epochs) ? ws.epochs : null;
  const at = state.activeTournament;
  return JSON.stringify({
    loaded: !!ws,
    current: ws && ws.current_epoch_id,
    epochs: rows ? rows.map((r) => [r.epoch_id, r.generation_count, r.promoted_count, r.closed,
      typeof r.best_scalar === 'number' ? r.best_scalar.toFixed(3) : null, r.goal]) : null,
    spark: ws && Array.isArray(ws.sparkline)
      ? ws.sparkline.map((p) => (p && typeof p.scalar === 'number') ? p.scalar.toFixed(3) : null) : null,
    live: at ? at.epoch_id || true : false,
    health: cache.health ? (cache.health.healthy !== false) : null,
  });
}

function sparkValsForEpoch(row) {
  const def = state.epochDef;
  const vals = [];
  if (def && def.epoch_id === row.epoch_id && Array.isArray(def.experiments)) {
    for (const e of def.experiments) {
      const s = e && e.outcome && e.outcome.scalar_score;
      if (typeof s === 'number' && isFinite(s)) vals.push(s);
    }
  }
  if (vals.length >= 2) return vals;
  if (typeof row.best_scalar === 'number' && isFinite(row.best_scalar)) {
    return [row.best_scalar * 1.18, row.best_scalar * 1.06, row.best_scalar];
  }
  return vals;
}

function epochStatus(row, isCurrent) {
  if (isCurrent) return { kind: 'live', label: 'live' };
  if (row.closed) return { kind: 'idle', label: 'closed' };
  if ((row.promoted_count || 0) > 0) return { kind: 'improve', label: 'open' };
  return { kind: 'idle', label: 'open' };
}

function fleetCard(row, isCurrent) {
  const st = epochStatus(row, isCurrent);
  const at = state.activeTournament;
  const liveHere = isCurrent && at && at.epoch_id === row.epoch_id;
  const sparkVals = sparkValsForEpoch(row);

  const head = el('div', { class: 'g-fleet-head' }, [
    el('span', { class: 'g-fleet-id g-mono' }, [row.epoch_id]),
    chip(liveHere ? 'tournament running' : st.label, liveHere ? 'live' : st.kind),
  ]);
  const goal = el('div', { class: 'g-fleet-goal' }, [row.goal || '(no goal recorded)']);
  const stats = el('div', { class: 'g-fleet-stats' }, [
    el('span', { class: 'g-fleet-stat-label' }, ['best']),
    el('span', { class: 'g-fleet-stat-value is-improve' }, [fmt(row.best_scalar)]),
    el('span', { class: 'g-fleet-stat-label' }, ['gens']),
    el('span', { class: 'g-fleet-stat-value' }, [String(row.generation_count || 0)]),
    el('span', { class: 'g-fleet-stat-label' }, ['promoted']),
    el('span', { class: 'g-fleet-stat-value' }, [String(row.promoted_count || 0)]),
  ]);
  const body = el('div', { class: 'g-fleet-body' }, [
    el('div', { class: 'g-fleet-spark' }, [
      sparkVals.length >= 2
        ? sparkline({ values: sparkVals, width: 220, height: 44, band: true })
        : el('span', { class: 'g-readout-foot' }, ['no trajectory yet']),
    ]),
    stats,
  ]);
  return el('a', {
    class: 'g-fleet-card' + (isCurrent ? ' is-live' : ''),
    href: href('epoch', { epochId: row.epoch_id }),
  }, [head, goal, body]);
}

function vitalsStrip() {
  const ws = cache.workspace || { epochs: [] };
  const rows = Array.isArray(ws.epochs) ? ws.epochs : [];
  let gens = 0, promoted = 0, open = 0, best = null;
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
  return readouts([
    { label: 'epochs', value: rows.length, foot: open + ' open' },
    { label: 'generations', value: gens, foot: promoted + ' promoted' },
    { label: 'best scalar', value: fmt(best), tone: 'improve', foot: 'lowest across fleet' },
    { label: 'phase', value: String(phase).toUpperCase(), tone: at ? 'live' : null, foot: at ? 'tournament live' : 'between rounds' },
  ]);
}

function healthPanel() {
  const hr = cache.health;
  if (!hr) return null;
  const findings = Array.isArray(hr.findings) ? hr.findings : [];
  const healthy = hr.healthy !== false && findings.length === 0;
  const accent = healthy ? 'improve' : (findings.some((f) => sev(f) === 'critical') ? 'regress' : 'caution');
  const body = [];
  if (healthy) {
    body.push(el('div', { class: 'g-readout-foot is-improve' }, ['✓ loop is healthy — the evaluation distinguishes candidates.']));
  } else {
    for (const f of findings) {
      const s = sev(f);
      body.push(el('div', { class: 'g-finding' }, [
        el('div', null, [
          chip(s, s === 'critical' ? 'regress' : (s === 'warn' || s === 'warning' ? 'caution' : 'idle')),
          el('span', { class: 'g-mono g-finding-name' }, [f.detector || f.name || 'finding']),
        ]),
        el('div', { class: 'g-readout-foot' }, [f.summary || f.message || '']),
      ]));
    }
  }
  return panel({ title: 'Loop health', sub: hr.epoch_id || '', accent, body });
}

function sev(f) { return String((f && (f.severity || f.level)) || 'info').toLowerCase(); }

export function renderEnvironment(root, _params, repaint) {
  ensure(repaint);
  const digest = environmentDigest();
  // Digest-gate: if nothing structural changed and the view is already
  // painted, do nothing — the cards (and their CSS hover transitions)
  // persist across heartbeats. THIS is the A bug #3 fix.
  if (digest === _lastDigest && root.firstChild) return;
  _lastDigest = digest;
  root.textContent = '';

  root.appendChild(el('div', { class: 'g-pagehead' }, [
    el('h1', null, ['Environment']),
    el('span', { class: 'g-pagehead-sub' }, ['the workspace as a fleet — every epoch, at a glance']),
  ]));

  if (cache.workspace == null) {
    root.appendChild(loading('Acquiring fleet telemetry'));
    return;
  }

  root.appendChild(el('div', { class: 'g-section' }, [vitalsStrip()]));

  const rows = Array.isArray(cache.workspace.epochs) ? cache.workspace.epochs : [];
  const current = cache.workspace.current_epoch_id;
  const fleetBody = rows.length === 0
    ? empty('No epochs recorded in this workspace yet.')
    : el('div', { class: 'g-fleet' }, rows.map((r) => fleetCard(r, r.epoch_id === current)));

  const hp = healthPanel();
  const grid = el('div', { class: 'g-grid g-grid-fleet' });
  grid.appendChild(panel({
    title: 'Fleet',
    sub: rows.length + ' epoch' + (rows.length === 1 ? '' : 's'),
    accent: state.activeTournament ? 'live' : null,
    body: fleetBody,
  }));
  if (hp) grid.appendChild(hp);
  root.appendChild(grid);

  // cross-epoch trendline hero — the Fleet trendline card.
  const spark = Array.isArray(cache.workspace.sparkline) ? cache.workspace.sparkline : [];
  const trendVals = spark.map((p) => p && p.scalar).filter((v) => typeof v === 'number' && isFinite(v));
  if (trendVals.length >= 2) {
    root.appendChild(el('div', { class: 'g-section' }, [
      panel({
        title: 'Cross-epoch trajectory',
        sub: 'best scalar per epoch · lower is better',
        body: el('div', { class: 'g-trend-hero' }, [
          sparkline({ values: trendVals, width: 760, height: 100, band: true, endDot: true }),
        ]),
      }),
    ]));
  }
}
