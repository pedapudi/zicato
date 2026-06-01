// variants/R/views/environment.js — the ENVIRONMENT detail pane.
//
// Shown when col1 (Environment) is selected but no epoch is picked yet — the
// workspace as a fleet: a cross-epoch overview strip, a per-epoch trajectory
// sparkline, and loop health. Selecting an epoch in col2 drives the rest of
// the cascade.
//
// Bind: /api/workspace, /api/health-report, /api/score-trajectory.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty } from '../ui.js';

function fmt(v, d = 3) { return svg.isNum(v) ? v.toFixed(d) : '—'; }

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dr-empty', text: 'Acquiring fleet telemetry…' }));
  const [ws, health] = await Promise.all([D.workspace(), D.healthReport()]);
  const rows = (ws && Array.isArray(ws.epochs)) ? ws.epochs : [];
  const current = ws ? ws.current_epoch_id : null;
  const spark = (ws && Array.isArray(ws.sparkline)) ? ws.sparkline : [];

  const digest = JSON.stringify({
    cur: current,
    rows: rows.map((r) => [r.epoch_id, r.generation_count || 0, r.promoted_count || 0, svg.isNum(r.best_scalar) ? r.best_scalar.toFixed(3) : null, !!r.closed]),
    trend: spark.map((p) => (p && svg.isNum(p.scalar) ? p.scalar.toFixed(3) : null)),
    health: health ? (Array.isArray(health.findings) ? health.findings.length : 0) : -1,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dr-pagehead' }, [
      el('h1', { class: 'dr-h1', text: 'Environment' }),
      el('p', { class: 'dr-lede', text: 'The workspace as a fleet — pick an epoch in the next column to cascade into its sections. Lower scalar (loss) is better.' }),
    ]));

    let gens = 0, promoted = 0, open = 0, best = null;
    for (const r of rows) {
      gens += r.generation_count || 0;
      promoted += r.promoted_count || 0;
      if (!r.closed) open += 1;
      if (svg.isNum(r.best_scalar)) best = best == null ? r.best_scalar : Math.min(best, r.best_scalar);
    }
    nodes.push(el('div', { class: 'dr-panel dr-row dr-overview' }, [
      statTile(String(rows.length), 'epochs', open + ' open'),
      statTile(String(gens), 'generations', promoted + ' promoted'),
      statTile(fmt(best), 'best scalar', 'lowest across fleet'),
      statTile(current || '—', 'current epoch', 'selected by default'),
    ]));

    if (rows.length) {
      const fleet = el('div', { class: 'dr-fleet' }, rows.map((r) => fleetCard(r, r.epoch_id === current, ctx)));
      nodes.push(section('Fleet · ' + rows.length + ' epoch' + (rows.length === 1 ? '' : 's'), fleet));
    } else {
      nodes.push(section('Fleet', empty('No epochs recorded in this workspace yet.')));
    }

    const trendVals = spark.map((p) => p && p.scalar).filter(svg.isNum);
    if (trendVals.length >= 2) {
      const card = el('div', { class: 'dr-panel' });
      card.appendChild(svg.sparkline({ width: 560, height: 80, values: trendVals, band: true }));
      card.appendChild(el('p', { class: 'dr-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'best scalar per epoch · lower is better' }));
      nodes.push(section('Cross-epoch trajectory', card));
    }

    if (health) nodes.push(healthPanel(health));
    return nodes;
  });
}

function statTile(value, key, foot) {
  return el('div', { class: 'dr-tile' }, [
    el('span', { class: 'dr-tile-value', text: value }),
    el('span', { class: 'dr-tile-key', text: key }),
    foot ? el('span', { class: 'dr-tile-foot', text: foot }) : null,
  ].filter(Boolean));
}

function fleetCard(row, isCurrent, ctx) {
  const st = isCurrent ? 'open' : (row.closed ? 'closed' : 'open');
  const head = el('div', { class: 'dr-fleet-head' }, [
    el('span', { class: 'dr-fleet-id', text: row.epoch_id }),
    el('span', { class: 'dr-chip dr-chip-' + st, text: st }),
  ]);
  const goal = el('div', { class: 'dr-fleet-goal', text: row.goal || '(no goal recorded)' });
  const stats = el('div', { class: 'dr-fleet-stats' }, [
    miniStat('best', fmt(row.best_scalar), 'good'),
    miniStat('gens', String(row.generation_count || 0)),
    miniStat('promoted', String(row.promoted_count || 0)),
  ]);
  const card = el('button', { class: 'dr-fleet-card' + (isCurrent ? ' dr-is-current' : ''), type: 'button' }, [head, goal, stats]);
  card.addEventListener('click', () => ctx.navigate({ epoch: row.epoch_id }));
  return card;
}

function miniStat(k, v, tone) {
  return el('div', { class: 'dr-mini' }, [
    el('span', { class: 'dr-mini-k', text: k }),
    el('span', { class: 'dr-mini-v' + (tone ? ' dr-good-t' : ''), text: v }),
  ]);
}

function healthPanel(hr) {
  const findings = Array.isArray(hr.findings) ? hr.findings : [];
  const healthy = hr.healthy !== false && findings.length === 0;
  const body = el('div');
  if (healthy) {
    body.appendChild(el('div', { class: 'dr-good-t', text: '✓ loop is healthy — the evaluation distinguishes candidates.' }));
  } else {
    for (const f of findings) {
      const sev = String((f && (f.severity || f.level)) || 'info').toLowerCase();
      body.appendChild(el('div', { class: 'dr-finding' }, [
        el('span', { class: 'dr-chip dr-chip-' + (sev === 'critical' ? 'closed' : 'open'), text: sev }),
        el('span', { class: 'dr-mono', style: 'margin-left:8px', text: f.detector || f.name || 'finding' }),
        el('div', { class: 'dr-faint', style: 'margin-top:4px', text: f.summary || f.message || '' }),
      ]));
    }
  }
  return section('Loop health' + (hr.epoch_id ? ' · ' + hr.epoch_id : ''), el('div', { class: 'dr-panel' }, [body]));
}
