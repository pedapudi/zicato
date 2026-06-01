// variants/J/views/matchups.js — MATCH-UPS across tournament styles.
//
// zicato RUNS a king-of-the-hill gauntlet. Console's match-ups screen shows:
//   0. the Tufte SANKEY (fit-to-width, NO viewport): the causal flow
//      candidate → per-board drift loss → aggregate scalar / gate verdict;
//   1. the gauntlet LADDER — a non-colliding bumps chart;
//   2. per-round PAIRED SLOPEGRAPHS — champion → challenger per board entry,
//      de-collided + jittered; click a line → its run;
//   3. ALTERNATIVE styles (illustrative overlays on the SAME candidate set).
//
// Data: /api/epoch, /api/score-trajectory, /api/tournaments,
// /api/matchup-grid/{e}/{champ}/{chall}.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, normaliseDecision } from '../ui.js';

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dj-empty', text: 'Reading tournaments…' }));

  const [ep, traj, bracket] = await Promise.all([D.epoch(), D.scoreTrajectory(), D.bracket()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dj-h1', text: 'Match-ups' }), empty('No current epoch.')]);
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

  // The Sankey reads the FIRST decided matchup's per-board grid as the causal
  // flow (challenger → per-board drift → gate verdict). It's the heart of one
  // round, and the fit-to-width Tufte figure the brief leads with.
  const focusIdx = matchups.length ? 0 : -1;
  const focusGrid = focusIdx >= 0 ? grids[focusIdx] : null;
  const focus = focusIdx >= 0 ? matchups[focusIdx] : null;

  const digest = JSON.stringify({
    nodes: nodes.map((n) => [n.id, n.x, n.promoted, svg.isNum(n.scalar) ? n.scalar.toFixed(3) : null, n.parent]),
    matchups: matchups.map((m, i) => [m.champion, m.challenger, m.decision,
      grids[i] && Array.isArray(grids[i].entry_grid) ? grids[i].entry_grid.map((r) => [r.entry_id, r.parent_drift_loss, r.child_drift_loss, r.verdict]) : null]),
    championId,
    loss: allIds.map((id) => [id, svg.isNum(lossById[id]) ? lossById[id].toFixed(3) : null]),
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'dj-pagehead' }, [
      el('h1', { class: 'dj-h1', text: 'Match-ups across tournament styles' }),
      el('p', { class: 'dj-lede', text: 'zicato runs a king-of-the-hill gauntlet — one champion, one challenger per round, paired per-board duels. Below: the causal flow, the real gauntlet, then the same candidates under other structures.' }),
    ]));

    // 0. Tufte Sankey (fit-to-width, NO viewport)
    out.push(section('Causal flow · challenger → per-board drift → gate', sankeyCard(focus, focusGrid, championId, ctx)));

    // 1. gauntlet ladder
    const ladder = el('div', { class: 'dj-panel' });
    ladder.appendChild(svg.bumps({ width: 760, height: 170, nodes, onClick: (n) => ctx.navigate('candidate', { gen: n.id }) }));
    ladder.appendChild(el('div', { class: 'dj-legend' }, [
      el('span', null, [el('i', { class: 'spine' }), 'champion spine (king of the hill)']),
      el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'challenger that fell']),
      el('span', { class: 'dj-faint', text: 'real gauntlet · click a node → candidate' }),
    ]));
    out.push(section('The gauntlet ladder · real data', ladder));

    // 2. paired slopegraphs
    const slopeWrap = el('div', { class: 'dj-pslope-grid' });
    let anyGrid = false;
    matchups.forEach((m, i) => {
      const grid = grids[i];
      const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
      const series = rows
        .filter((r) => svg.isNum(r.parent_drift_loss) || svg.isNum(r.child_drift_loss))
        .map((r) => ({ label: r.entry_id, id: r.entry_id, a: svg.isNum(r.parent_drift_loss) ? r.parent_drift_loss : NaN, b: svg.isNum(r.child_drift_loss) ? r.child_drift_loss : NaN, verdict: r.verdict }));
      const dec = m.decision || 'rejected';
      const cell = el('div', { class: 'dj-pslope-cell' }, [
        el('div', { class: 'dj-pslope-title' }, [
          el('span', { class: 'dj-mono', text: `${m.champion} → ${m.challenger}` }),
          el('span', { class: `dj-pill dj-${dec}`, text: dec }),
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
    const slopeCard = el('div', { class: 'dj-panel' });
    slopeCard.appendChild(slopeWrap);
    if (anyGrid) slopeCard.appendChild(el('div', { class: 'dj-legend' }, [
      el('span', null, [el('i', { class: 'good' }), 'entry improved (loss ↓)']),
      el('span', null, [el('i', { class: 'bad' }), 'entry regressed (loss ↑)']),
      el('span', { class: 'dj-faint', text: 'lines de-collided + jittered so coincident values stay legible · click → run' }),
    ]));
    out.push(section('Per-round paired duels · champion → challenger, per board', slopeCard));

    // 3. alternative styles (illustrative)
    const challengerIds = allIds.filter((id) => id !== championId);
    const altWrap = el('div', { class: 'dj-alt-grid' });
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
    const altCard = el('div', { class: 'dj-panel' });
    altCard.appendChild(el('div', { class: 'dj-illustrative-banner', text: 'Illustrative only — these arrange the SAME candidate set under alternative structures. Only the gauntlet above has real per-round data.' }));
    altCard.appendChild(altWrap);
    out.push(section('Alternative tournament styles', altCard));
    return out;
  });
}

// The fit-to-width Tufte Sankey for one round: challenger PATCH → per-board
// DRIFT (each entry's child loss) → GATE verdict. NO viewport.
function sankeyCard(focus, grid, championId, ctx) {
  const card = el('div', { class: 'dj-panel' });
  const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
  if (!focus || !rows.length) {
    card.appendChild(empty('No round with a paired per-board grid yet — the Sankey needs at least one decided matchup.'));
    return card;
  }
  const challenger = focus.challenger;
  const champion = focus.champion;
  const dec = focus.decision || 'rejected';

  const patchNodes = [{ id: 'patch', label: `patch ${champion}→${challenger}`, sub: 'mutation', value: 0, cls: 'dj-sankey-patch' }];
  const driftNodes = [];
  const links = [];
  let total = 0;
  for (const r of rows) {
    const v = svg.isNum(r.child_drift_loss) ? r.child_drift_loss : 0;
    if (!(v > 0)) continue;
    total += v;
    const verdict = r.verdict || 'flat';
    const cls = verdict === 'improved' ? 'dj-good' : verdict === 'regressed' ? 'dj-bad' : 'dj-flat';
    driftNodes.push({ id: 'd_' + r.entry_id, label: r.entry_id, sub: svg.fmt(v, 0) + ' loss', value: v, cls, ref: r.entry_id });
    links.push({ source: 'patch', target: 'd_' + r.entry_id, value: v, cls });
  }
  patchNodes[0].value = total || 1;
  const gateCls = dec === 'promoted' ? 'dj-good' : 'dj-bad';
  const gateNodes = [{ id: 'gate', label: dec === 'promoted' ? '♛ promoted' : '✕ rejected', sub: 'Σ ' + svg.fmt(total, 0), value: total || 1, cls: gateCls }];
  for (const d of driftNodes) links.push({ source: d.id, target: 'gate', value: d.value, cls: d.cls });

  card.appendChild(svg.sankey({
    width: 760, colHeight: Math.max(220, 40 + driftNodes.length * 30), nodeW: 150,
    patch: patchNodes, drift: driftNodes, gate: gateNodes, links,
    onNode: (n) => { if (n.ref) ctx.navigate('run', { gen: challenger, entry: n.ref }); },
  }));
  card.appendChild(el('div', { class: 'dj-legend' }, [
    el('span', null, [el('i', { class: 'good' }), 'entry improved']),
    el('span', null, [el('i', { class: 'bad' }), 'entry regressed']),
    el('span', { class: 'dj-faint', text: 'fit-to-width · no pan/zoom · ribbon width = per-board loss share · click a drift node → its run' }),
  ]));
  return card;
}

function altCell(title, note, mark) {
  return el('figure', { class: 'dj-alt-cell' }, [
    el('figcaption', { class: 'dj-alt-cap' }, [
      el('span', { class: 'dj-alt-title', text: title }),
      el('span', { class: 'dj-pill dj-baseline', text: 'illustrative' }),
    ]),
    mark,
    el('p', { class: 'dj-alt-note dj-faint', text: note }),
  ]);
}

function median(xs) {
  const s = xs.slice().sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
