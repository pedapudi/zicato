// variants/D/views/environment.js — the workspace AS A WHOLE.
//
// The cross-epoch screen the current UI loses: the operator sees every
// epoch at once. Two coordinated Tufte marks:
//   1. A grid of per-epoch SMALL MULTIPLES — one sparkline per epoch of
//      its per-generation scalar trajectory, captioned with the epoch's
//      goal and its best scalar.
//   2. A master cross-epoch SLOPEGRAPH of best-scalar before→after along
//      the epoch lineage, so the whole environment's direction reads in
//      one glance (down = improving).
//
// Data: /api/workspace (per-epoch best scalars + goals + lineage edges),
// /api/score-trajectory (per-generation scalars for the active epoch),
// /api/health-report (loop health findings).

import { el, clearChildren } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { section, crumb, empty, loading, stat } from '../ui.js';

export async function render(host, ctx) {
  clearChildren(host);
  host.appendChild(crumb([{ label: 'environment' }]));
  host.appendChild(el('h1', { class: 'd-h1', text: 'Environment' }));
  host.appendChild(el('p', { class: 'd-lede' }, [
    'The whole workspace across epochs. Each epoch mutates an agent and is judged by drift / loss — ',
    el('em', { text: 'lower is better' }),
    '. Every number that has a trend is a small chart.',
  ]));

  const body = el('div');
  host.appendChild(body);
  body.appendChild(loading('Reading workspace…'));

  const [ws, traj, health] = await Promise.all([
    D.workspace(), D.scoreTrajectory(), D.healthReport(),
  ]);
  clearChildren(body);

  if (!ws || !Array.isArray(ws.epochs) || ws.epochs.length === 0) {
    body.appendChild(empty('No epochs recorded yet. Start an epoch to populate the environment.'));
    return;
  }
  const epochs = ws.epochs;
  const current = ws.current_epoch_id;

  // ---- headline stats ----
  const finiteBest = epochs.map((e) => e.best_scalar).filter(svg.isNum);
  const bestEver = finiteBest.length ? Math.min(...finiteBest) : null;
  const totalGen = epochs.reduce((a, e) => a + (e.generation_count || 0), 0);
  const totalProm = epochs.reduce((a, e) => a + (e.promoted_count || 0), 0);
  body.appendChild(el('div', { class: 'd-panel d-row' }, [
    stat(String(epochs.length), 'epochs'),
    stat(String(totalGen), 'generations'),
    stat(String(totalProm), 'promotions'),
    stat(svg.fmt(bestEver), 'best scalar'),
    stat(current || '—', 'live epoch'),
  ]));

  // ---- master cross-epoch slopegraph (best scalar over the lineage) ----
  // Use the workspace lineage order (directory order is creation order).
  const slopeSeries = [];
  for (let k = 1; k < epochs.length; k++) {
    const a = epochs[k - 1]; const b = epochs[k];
    slopeSeries.push({
      label: b.epoch_id, id: b.epoch_id,
      a: a.best_scalar, b: b.best_scalar,
      emphasis: b.epoch_id === current,
    });
  }
  const slopeCard = el('div', { class: 'd-panel' });
  if (slopeSeries.length === 0) {
    // Single epoch — show its best as one labelled node.
    slopeCard.appendChild(el('p', { class: 'd-faint',
      text: 'A cross-epoch slope needs at least two epochs. Best scalar so far: ' + svg.fmt(epochs[0].best_scalar) }));
  } else {
    slopeCard.appendChild(svg.slopegraph({
      width: 560, height: Math.max(180, 40 + slopeSeries.length * 26),
      left: { title: 'prev epoch' }, right: { title: 'next epoch' },
      labelGap: 130, goodDirection: 'down', series: slopeSeries,
      onClick: () => ctx.navigate('epoch'),
    }));
    slopeCard.appendChild(el('div', { class: 'd-legend' }, [
      el('span', null, [el('i', { class: 'good' }), 'improved (loss ↓)']),
      el('span', null, [el('i', { class: 'bad' }), 'regressed (loss ↑)']),
      el('span', { class: 'd-faint', text: 'emphasised line = live epoch' }),
    ]));
  }
  body.appendChild(section('Cross-epoch trajectory · best scalar', slopeCard));

  // ---- per-epoch small multiples ----
  // For the LIVE epoch we have per-generation scalars from
  // /api/score-trajectory; for others we draw the best-scalar as a flat
  // single mark (the consolidated workspace read carries only the best).
  const trajByEpoch = new Map();
  if (traj && Array.isArray(traj.points) && traj.epoch_id) {
    trajByEpoch.set(traj.epoch_id, traj.points.map((p) => p.scalar));
  }
  const grid = el('div', { class: 'd-sm-grid' });
  for (const e of epochs) {
    const series = trajByEpoch.get(e.epoch_id)
      || (svg.isNum(e.best_scalar) ? [e.best_scalar, e.best_scalar] : []);
    const mark = svg.sparkline({
      width: 150, height: 30, values: series, band: true, goodDirection: 'down',
    });
    const sub = svg.isNum(e.best_scalar) ? svg.fmt(e.best_scalar) : '—';
    const cap = (e.epoch_id === current ? '● ' : '') + e.epoch_id;
    const fig = svg.smallMultiple(cap, mark, sub);
    fig.style.cursor = 'pointer';
    fig.title = e.goal || e.epoch_id;
    fig.addEventListener('click', () => ctx.navigate('epoch'));
    if (e.goal) {
      fig.appendChild(el('div', { class: 'd-faint',
        style: 'font-size:10.5px;margin-top:2px;line-height:1.3;',
        text: e.goal.length > 70 ? e.goal.slice(0, 69) + '…' : e.goal }));
    }
    grid.appendChild(fig);
  }
  body.appendChild(section('Per-epoch trajectories', grid));

  // ---- loop-health findings (honest state) ----
  const healthCard = el('div', { class: 'd-panel' });
  if (health && Array.isArray(health.findings) && health.findings.length) {
    const ul = el('ul', { style: 'margin:0;padding-left:18px;font-size:12.5px;line-height:1.6;' });
    for (const f of health.findings.slice(0, 8)) {
      const txt = typeof f === 'string' ? f
        : (f && (f.message || f.detail || f.kind)) || JSON.stringify(f);
      ul.appendChild(el('li', { text: String(txt) }));
    }
    healthCard.appendChild(ul);
  } else if (health && health.healthy) {
    healthCard.appendChild(el('p', { class: 'd-good-t', style: 'font-size:13px;margin:0;',
      text: '✓ No loop-health findings — the meta-loop looks healthy.' }));
  } else {
    healthCard.appendChild(empty('No health report yet.'));
  }
  body.appendChild(section('Loop health', healthCard));
}
