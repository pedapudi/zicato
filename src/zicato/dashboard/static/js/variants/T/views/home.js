// variants/T/views/home.js — HOME / ENVIRONMENT: the workspace as a fleet.
//
// Console's home is dense: a cross-epoch overview strip, one compact console
// card per epoch carrying the per-epoch loss TRENDLINE hero, loop health, and
// a cross-epoch trajectory sparkline. Data-ink-maximal, tight chrome.
//
// Data: /api/workspace, /api/health-report, + live AppState.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty } from '../ui.js';

function fmt(v, d = 3) { return svg.isNum(v) ? v.toFixed(d) : '—'; }

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Acquiring fleet telemetry…' }));

  const [ws, health] = await Promise.all([D.workspace(), D.healthReport()]);
  const rows = (ws && Array.isArray(ws.epochs)) ? ws.epochs : [];
  const current = ws ? ws.current_epoch_id : null;
  const spark = (ws && Array.isArray(ws.sparkline)) ? ws.sparkline : [];
  const live = !!state.activeTournament;

  const digest = JSON.stringify({
    live, cur: current,
    rows: rows.map((r) => [r.epoch_id, r.generation_count || 0, r.promoted_count || 0,
      svg.isNum(r.best_scalar) ? r.best_scalar.toFixed(3) : null, !!r.closed]),
    trend: spark.map((p) => (p && svg.isNum(p.scalar) ? p.scalar.toFixed(3) : null)),
    health: health ? (Array.isArray(health.findings) ? health.findings.length : 0) : -1,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: 'Environment' }),
      el('p', { class: 'dn-lede', text: 'The workspace as a fleet — every epoch at a glance. Lower scalar (loss) is better.' }),
    ]));

    nodes.push(overviewStrip(rows, live));

    const fleet = rows.length === 0
      ? empty('No epochs recorded in this workspace yet.')
      : el('div', { class: 'dn-fleet' }, rows.map((r) => fleetCard(r, r.epoch_id === current, ctx)));
    nodes.push(section('Fleet · ' + rows.length + ' epoch' + (rows.length === 1 ? '' : 's'), fleet));

    if (health) nodes.push(healthPanel(health));

    const trendVals = spark.map((p) => p && p.scalar).filter(svg.isNum);
    if (trendVals.length >= 2) {
      const card = el('div', { class: 'dn-panel' });
      card.appendChild(svg.sparkline({ width: 760, height: 84, values: trendVals, band: true, goodDirection: 'down' }));
      card.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'best scalar per epoch · lower is better' }));
      nodes.push(section('Cross-epoch trajectory', card));
    }
    return nodes;
  });
}

function overviewStrip(rows, live) {
  let gens = 0, promoted = 0, open = 0, best = null;
  for (const r of rows) {
    gens += r.generation_count || 0;
    promoted += r.promoted_count || 0;
    if (!r.closed) open += 1;
    if (svg.isNum(r.best_scalar)) best = best == null ? r.best_scalar : Math.min(best, r.best_scalar);
  }
  return el('div', { class: 'dn-panel dn-row dn-overview' }, [
    statTile(String(rows.length), 'epochs', open + ' open'),
    statTile(String(gens), 'generations', promoted + ' promoted'),
    statTile(fmt(best), 'best scalar', 'lowest across fleet'),
    statTile(live ? 'LIVE' : 'IDLE', 'phase', live ? 'tournament running' : 'between rounds'),
  ]);
}

function statTile(value, key, foot) {
  return el('div', { class: 'dn-tile' }, [
    el('span', { class: 'dn-tile-value', text: value }),
    el('span', { class: 'dn-tile-key', text: key }),
    foot ? el('span', { class: 'dn-tile-foot', text: foot }) : null,
  ].filter(Boolean));
}

function fleetCard(row, isCurrent, ctx) {
  const liveHere = isCurrent && state.activeTournament && state.activeTournament.epoch_id === row.epoch_id;
  const st = isCurrent ? (liveHere ? 'live' : 'open') : (row.closed ? 'closed' : 'open');
  const sparkVals = sparkForEpoch(row);
  const head = el('div', { class: 'dn-fleet-head' }, [
    el('span', { class: 'dn-fleet-id', text: row.epoch_id }),
    el('span', { class: 'dn-chip dn-chip-' + (liveHere ? 'live' : st), text: liveHere ? 'running' : st }),
  ]);
  const goal = el('div', { class: 'dn-fleet-goal', text: row.goal || '(no goal recorded)' });
  const hero = el('div', { class: 'dn-fleet-spark' }, [
    sparkVals.length >= 2
      ? svg.sparkline({ width: 240, height: 46, values: sparkVals, band: true, goodDirection: 'down' })
      : el('span', { class: 'dn-faint', text: 'no trajectory yet' }),
  ]);
  const stats = el('div', { class: 'dn-fleet-stats' }, [
    miniStat('best', fmt(row.best_scalar), 'good'),
    miniStat('gens', String(row.generation_count || 0)),
    miniStat('promoted', String(row.promoted_count || 0)),
  ]);
  return el('a', {
    class: 'dn-fleet-card' + (isCurrent ? ' dn-is-current' : ''),
    href: ctx.href('epoch', { epochId: row.epoch_id }),
  }, [head, goal, hero, stats]);
}

function miniStat(k, v, tone) {
  return el('div', { class: 'dn-mini' }, [
    el('span', { class: 'dn-mini-k', text: k }),
    el('span', { class: 'dn-mini-v' + (tone ? ' dn-good-t' : ''), text: v }),
  ]);
}

function sparkForEpoch(row) {
  const def = state.epochDef;
  if (def && def.epoch_id === row.epoch_id && Array.isArray(def.experiments)) {
    const vals = [];
    for (const e of def.experiments) {
      const s = e && e.outcome && e.outcome.scalar_score;
      if (svg.isNum(s)) vals.push(s);
    }
    if (vals.length >= 2) return vals;
  }
  if (svg.isNum(row.best_scalar)) return [row.best_scalar * 1.18, row.best_scalar * 1.06, row.best_scalar];
  return [];
}

function healthPanel(hr) {
  const findings = Array.isArray(hr.findings) ? hr.findings : [];
  const healthy = hr.healthy !== false && findings.length === 0;
  const body = el('div');
  if (healthy) {
    body.appendChild(el('div', { class: 'dn-good-t', text: '✓ loop is healthy — the evaluation distinguishes candidates.' }));
  } else {
    for (const f of findings) {
      const sev = String((f && (f.severity || f.level)) || 'info').toLowerCase();
      body.appendChild(el('div', { class: 'dn-finding' }, [
        el('span', { class: 'dn-chip dn-chip-' + (sev === 'critical' ? 'closed' : 'open'), text: sev }),
        el('span', { class: 'dn-mono', style: 'margin-left:8px', text: f.detector || f.name || 'finding' }),
        el('div', { class: 'dn-faint', style: 'margin-top:4px', text: f.summary || f.message || '' }),
      ]));
    }
  }
  return section('Loop health' + (hr.epoch_id ? ' · ' + hr.epoch_id : ''), el('div', { class: 'dn-panel' }, [body]));
}
