// variants/L/views/matchups.js — MATCH-UPS across tournament styles.
//
// zicato RUNS a king-of-the-hill gauntlet. This screen shows the real
// gauntlet ladder (bumps), per-round PAIRED SLOPEGRAPHS (champion → challenger
// per board, de-collided + jittered; click → run), and ILLUSTRATIVE
// alternative structures over the same candidate set (bracket / round-robin /
// race lanes), clearly labelled illustrative.
//
// Data: /api/epoch, /api/score-trajectory, /api/tournaments,
// /api/matchup-grid/{e}/{champ}/{chall}.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, normaliseDecision } from '../ui.js';

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'vl-empty', text: 'Reading tournaments…' }));

  const [ep, traj, bracket] = await Promise.all([D.epoch(), D.scoreTrajectory(), D.bracket()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'vl-h1', text: 'Match-ups' }), empty('No current epoch.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  const nodes = experiments.map((x, i) => ({
    id: x.generation_id, x: i,
    promoted: normaliseDecision(x.outcome) === 'promoted',
    scalar: scalarByGen.get(x.generation_id),
    parent: x.parent_generation_id || null,
  }));
  if (nodes.length && !nodes.some((n) => n.promoted)) nodes[0].promoted = true;

  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups
    : experiments.filter((x) => x.parent_generation_id).map((x) => ({ champion: x.parent_generation_id, challenger: x.generation_id, decision: normaliseDecision(x.outcome) }));
  const grids = await Promise.all(matchups.map((m) => (m.champion && m.challenger) ? D.matchupGrid(epochId, m.champion, m.challenger) : Promise.resolve(null)));

  const allIds = experiments.map((x) => x.generation_id);
  const champNode = nodes.find((n) => n.promoted);
  const championId = champNode ? champNode.id : (allIds[0] || null);
  const lossById = {};
  for (const id of allIds) lossById[id] = scalarByGen.get(id);

  const digest = JSON.stringify({
    nodes: nodes.map((n) => [n.id, n.x, n.promoted, svg.isNum(n.scalar) ? n.scalar.toFixed(3) : null, n.parent]),
    matchups: matchups.map((m, i) => [m.champion, m.challenger, m.decision,
      grids[i] && Array.isArray(grids[i].entry_grid) ? grids[i].entry_grid.map((r) => [r.entry_id, r.parent_drift_loss, r.child_drift_loss, r.verdict]) : null]),
    championId,
    loss: allIds.map((id) => [id, svg.isNum(lossById[id]) ? lossById[id].toFixed(3) : null]),
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'vl-pagehead' }, [
      el('h1', { class: 'vl-h1', text: 'Match-ups across tournament styles' }),
      el('p', { class: 'vl-lede', text: 'zicato runs a king-of-the-hill gauntlet — one champion, one challenger per round, paired per-board duels. Below: the real gauntlet, then the same candidates under other structures.' }),
    ]));

    const ladder = el('div', { class: 'vl-panel' });
    ladder.appendChild(svg.bumps({ width: 720, height: 200, nodes, onClick: (n) => ctx.navigate('candidate', { gen: n.id }) }));
    ladder.appendChild(el('p', { class: 'vl-faint vl-fignote', text: 'champion spine (king of the hill) + challengers that fell · real gauntlet · click a node → candidate' }));
    out.push(section('The gauntlet ladder · real data', ladder));

    const slopeWrap = el('div', { class: 'vl-pslope-grid' });
    let anyGrid = false;
    matchups.forEach((m, i) => {
      const grid = grids[i];
      const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
      const series = rows
        .filter((r) => svg.isNum(r.parent_drift_loss) || svg.isNum(r.child_drift_loss))
        .map((r) => ({ label: r.entry_id, id: r.entry_id, a: svg.isNum(r.parent_drift_loss) ? r.parent_drift_loss : NaN, b: svg.isNum(r.child_drift_loss) ? r.child_drift_loss : NaN, verdict: r.verdict }));
      const dec = m.decision || 'rejected';
      const cell = el('div', { class: 'vl-pslope-cell' }, [
        el('div', { class: 'vl-pslope-title' }, [
          el('span', { class: 'vl-mono', text: `${m.champion} → ${m.challenger}` }),
          el('span', { class: `vl-pill vl-${dec}`, text: dec }),
        ]),
      ]);
      if (series.length) {
        anyGrid = true;
        cell.appendChild(svg.pairedSlopegraph({
          width: 460, height: Math.max(220, 50 + series.length * 26),
          left: { title: `champion ${m.champion}` }, right: { title: `challenger ${m.challenger}` },
          labelGap: 140, goodDirection: 'down', series,
          onClick: (s) => ctx.navigate('run', { gen: m.challenger, entry: s.id }),
        }));
      } else {
        cell.appendChild(empty('No paired per-board grid for this round (loss files missing).'));
      }
      slopeWrap.appendChild(cell);
    });
    const slopeCard = el('div', { class: 'vl-panel' });
    slopeCard.appendChild(slopeWrap);
    if (anyGrid) slopeCard.appendChild(el('p', { class: 'vl-faint vl-fignote', text: 'improved (loss ↓) teal · regressed (loss ↑) rose · lines de-collided + jittered so coincident values stay legible · click → run' }));
    out.push(section('Per-round paired duels · champion → challenger, per board', slopeCard));

    const challengerIds = allIds.filter((id) => id !== championId);
    const altWrap = el('div', { class: 'vl-alt-grid' });
    altWrap.appendChild(altCell('Single-elimination bracket',
      'A bracket triages a large field by discarding losers — the wrong primitive here (noise-fragile at the boundary). Illustrative.',
      svg.bracketMini({ width: 340, champion: championId, challengers: challengerIds.map((id) => ({ id })), winner: championId })));
    altWrap.appendChild(altCell('Round-robin / Swiss matrix',
      'Every candidate vs every other on aggregate loss — ranks a whole field with no single-loss death, but superseded by iterated racing. Illustrative.',
      svg.roundRobinMatrix({ ids: allIds, lossById, cell: 34 })));
    const finite = allIds.map((id) => lossById[id]).filter(svg.isNum);
    const cut = finite.length ? median(finite) : null;
    const runners = allIds.map((id) => ({ id, loss: lossById[id], eliminated: svg.isNum(lossById[id]) && cut != null ? lossById[id] > cut : false }));
    altWrap.appendChild(altCell('Racing / successive-halving lanes',
      'Each candidate on its own lane; the cut eliminates the worse half, survivors keep racing. The convergent recommendation. Illustrative.',
      svg.raceLanes({ width: 380, runners, cut })));
    const altCard = el('div', { class: 'vl-panel' });
    altCard.appendChild(el('div', { class: 'vl-illustrative-banner', text: 'Illustrative only — these arrange the SAME candidate set under alternative structures. Only the gauntlet above has real per-round data.' }));
    altCard.appendChild(altWrap);
    out.push(section('Alternative tournament styles', altCard));
    return out;
  });
}

function altCell(title, note, mark) {
  return el('figure', { class: 'vl-alt-cell' }, [
    el('figcaption', { class: 'vl-alt-cap' }, [
      el('span', { class: 'vl-alt-title', text: title }),
      el('span', { class: 'vl-pill vl-baseline', text: 'illustrative' }),
    ]),
    mark,
    el('p', { class: 'vl-alt-note vl-faint', text: note }),
  ]);
}

function median(xs) {
  const s = xs.slice().sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
