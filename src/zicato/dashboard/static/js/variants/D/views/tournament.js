// variants/D/views/tournament.js — the lineage as a slopegraph done right.
//
// Every champion→challenger matchup of the current epoch as a single
// non-colliding slopegraph: each line is one tournament, champion scalar
// on the left, challenger scalar on the right, slope = the verdict's
// direction. De-collided labels, Tufte range-frame axis, clickable lines
// drilling to the experiment. A companion bumps chart gives the same
// lineage as ranked lanes.
//
// Data: /api/epoch (experiments + parents), /api/score-trajectory
// (per-gen scalars).

import { el, clearChildren } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { section, crumb, empty, loading, normaliseDecision } from '../ui.js';

export async function render(host, ctx) {
  clearChildren(host);
  host.appendChild(crumb([{ label: 'environment', view: 'environment' }, { label: 'tournament' }]));
  host.appendChild(el('h1', { class: 'd-h1', text: 'Tournament · lineage' }));
  host.appendChild(el('p', { class: 'd-lede',
    text: 'Each line is one champion-vs-challenger matchup. Slope down = the challenger lowered loss and was promoted.' }));

  const body = el('div'); host.appendChild(body);
  body.appendChild(loading('Reading lineage…'));

  const [ep, traj] = await Promise.all([D.epoch(), D.scoreTrajectory()]);
  clearChildren(body);
  if (!ep || ep.epoch_id == null) { body.appendChild(empty('No current epoch.')); return; }
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) {
    for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  }

  // ---- slopegraph of matchups ----
  const series = [];
  for (const x of experiments) {
    const parent = x.parent_generation_id;
    if (!parent) continue; // seed has no matchup
    const a = scalarByGen.get(parent);
    const b = scalarByGen.get(x.generation_id);
    if (!svg.isNum(a) && !svg.isNum(b)) continue;
    series.push({
      label: x.generation_id, id: x.generation_id, a, b,
      emphasis: normaliseDecision(x.outcome) === 'promoted',
    });
  }
  const slopeCard = el('div', { class: 'd-panel' });
  if (series.length) {
    slopeCard.appendChild(svg.slopegraph({
      width: 560, height: Math.max(220, 40 + series.length * 22),
      left: { title: 'champion' }, right: { title: 'challenger' },
      labelGap: 150, goodDirection: 'down', series,
      onClick: (s) => ctx.navigate('experiment', { gen: s.id }),
    }));
    slopeCard.appendChild(el('div', { class: 'd-legend' }, [
      el('span', null, [el('i', { class: 'good' }), 'challenger improved']),
      el('span', null, [el('i', { class: 'bad' }), 'challenger regressed']),
      el('span', { class: 'd-faint', text: 'bold line = promoted · click → experiment' }),
    ]));
  } else {
    slopeCard.appendChild(empty('No completed matchups yet (need per-generation scalars; the index may not be built).'));
  }
  body.appendChild(section('Matchups', slopeCard));

  // ---- bumps chart of the lineage ----
  const nodes = experiments.map((x, i) => ({
    id: x.generation_id, x: i,
    promoted: normaliseDecision(x.outcome) === 'promoted',
    scalar: scalarByGen.get(x.generation_id),
    parent: x.parent_generation_id || null,
  }));
  if (nodes.length && !nodes.some((n) => n.promoted)) nodes[0].promoted = true;
  const bumpsCard = el('div', { class: 'd-panel' });
  bumpsCard.appendChild(svg.bumps({
    width: 720, height: 190, nodes,
    onClick: (n) => ctx.navigate('experiment', { gen: n.id }),
  }));
  body.appendChild(section('Lineage lanes', bumpsCard));
}
