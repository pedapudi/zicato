// variants/P/views/gens.js — GENERATIONS group landing.
//
// The detail pane for the tree's "Generations" group: a compact roster of
// every generation in the epoch — lineage role (seed / champion / rejected),
// parent, scalar (loss), and Δ vs the previous champion — each row linking
// into that candidate's detail (lifecycle + gate + per-board scoring). This is
// the "pick a candidate" surface the tree's Generations node opens.
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, verdictPill, normaliseDecision } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading generations…' }));
  const epochId = (params && params.epochId) || null;

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  const id = epochId || (ep && ep.epoch_id) || null;
  if (!id) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Generations' }), empty('No epoch selected.')]);
    return;
  }
  const experiments = (ep && Array.isArray(ep.experiments)) ? ep.experiments : [];
  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  const champ = gens.find((g) => g.promoted) || gens.find((g) => !g.parent) || null;
  const championId = champ ? champ.id : null;
  const champScalar = championId ? scalarByGen.get(championId) : null;

  const digest = JSON.stringify({
    id, championId,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Generations · ${id}` }),
      el('p', { class: 'dn-lede', text: 'Every candidate in this epoch. Open one for its lifecycle, promote gate, all match-ups, per-board scoring, and patch diff.' }),
    ]));

    nodes.push(el('div', { class: 'dn-panel dn-row' }, [
      stat(String(gens.length), 'generations'),
      stat(String(gens.filter((g) => g.promoted).length), 'promoted'),
      stat(championId || '—', 'reigning champion'),
      stat(svg.isNum(champScalar) ? svg.fmt(champScalar, 1) : '—', 'loss floor'),
    ]));

    const tblCard = el('div', { class: 'dn-panel' });
    if (!gens.length) {
      tblCard.appendChild(empty('No generations recorded for this epoch.'));
    } else {
      const tbl = el('table', { class: 'dn-board-table' });
      tbl.appendChild(el('thead', null, [el('tr', null, [
        el('th', { text: 'generation' }), el('th', { text: 'role' }), el('th', { text: 'parent' }),
        el('th', { class: 'dn-num', text: 'scalar (loss)' }), el('th', { class: 'dn-num', text: 'Δ vs champion' }), el('th', { text: '' }),
      ])]));
      const tbody = el('tbody');
      for (const g of gens) {
        const sc = scalarByGen.get(g.id);
        const baseline = !g.parent;
        const decision = baseline ? 'baseline' : (g.promoted ? 'promoted' : 'rejected');
        const delta = (svg.isNum(sc) && svg.isNum(champScalar) && !baseline) ? sc - champScalar : null;
        tbody.appendChild(el('tr', { class: g.promoted ? 'dn-board-champ' : '' }, [
          el('td', { class: 'dn-mono', text: g.id + (g.promoted ? ' ♛' : '') }),
          el('td', null, [verdictPill(decision)]),
          el('td', { class: 'dn-mono', text: g.parent || 'seed' }),
          el('td', { class: 'dn-num dn-mono', text: svg.isNum(sc) ? svg.fmt(sc, 1) : '—' }),
          el('td', { class: 'dn-num dn-mono ' + (delta > 0 ? 'dn-bad-t' : delta < 0 ? 'dn-good-t' : ''), text: svg.isNum(delta) ? svg.fmtSigned(delta, 1) : '—' }),
          el('td', null, [el('a', { class: 'dn-linkbtn', href: ctx.href('candidate', { epochId: id, gen: g.id }), text: 'open →' })]),
        ]));
      }
      tbl.appendChild(tbody);
      tblCard.appendChild(tbl);
    }
    nodes.push(section('Roster · click a candidate to open its detail', tblCard));
    return nodes;
  });
}
