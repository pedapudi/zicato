// variants/D/views/tournament.js — THEME 4: match-ups across styles.
//
// zicato RUNS a king-of-the-hill gauntlet (one reigning champion per
// epoch, one challenger per round, paired / common-random-number duels —
// SELECTION.md §3). This view shows the ACTUAL gauntlet from real data,
// then the same candidate set under the OTHER documented structures,
// clearly labelled "illustrative".
//
//   1. The gauntlet ladder — a non-colliding BUMPS chart (champion spine
//      lane + rejected-challenger lane).
//   2. Per-round PAIRED SLOPEGRAPHS — one per matchup, champion loss → the
//      challenger loss for every board entry. The operator flagged
//      COLLIDING slopegraph lines as a defect; pairedSlopegraph fixes it
//      with per-column label de-collision + leaders, node jitter for
//      coincident values, and direct labelling. Real data from
//      /api/matchup-grid.
//   3. ALTERNATIVE styles (illustrative overlays on the same set, a
//      different topology each): a single-elim BRACKET tree, a round-robin
//      MATRIX heat grid, and a racing / successive-halving RACE-LANE dot
//      plot (SELECTION.md §2/§5/§6). Honest banner: only the gauntlet has
//      real per-round data.
//
// Data: /api/tournaments (the gauntlet matchups + decisions),
// /api/matchup-grid/{e}/{champ}/{chall} (paired per-board duel),
// /api/epoch, /api/score-trajectory.

import { el, clearChildren } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { section, crumb, empty, loading, normaliseDecision } from '../ui.js';

export async function render(host, ctx) {
  clearChildren(host);
  host.appendChild(crumb([{ label: 'environment', view: 'environment' }, { label: 'match-ups' }]));
  host.appendChild(el('h1', { class: 'd-h1', text: 'Match-ups across tournament styles' }));
  host.appendChild(el('p', { class: 'd-lede', text: 'zicato runs a king-of-the-hill gauntlet — one champion, one challenger per round, paired per-board duels. Below: the real gauntlet, then how the same candidates would be arranged under other structures.' }));

  const body = el('div'); host.appendChild(body);
  body.appendChild(loading('Reading tournaments…'));

  const [ep, traj, bracket] = await Promise.all([D.epoch(), D.scoreTrajectory(), D.bracket()]);
  clearChildren(body);
  if (!ep || ep.epoch_id == null) { body.appendChild(empty('No current epoch.')); return; }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) {
    for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  }

  // ---- 1. the gauntlet ladder (bumps) ----
  const nodes = experiments.map((x, i) => ({
    id: x.generation_id, x: i,
    promoted: normaliseDecision(x.outcome) === 'promoted',
    scalar: scalarByGen.get(x.generation_id),
    parent: x.parent_generation_id || null,
  }));
  if (nodes.length && !nodes.some((n) => n.promoted)) nodes[0].promoted = true;
  const ladderCard = el('div', { class: 'd-panel' });
  ladderCard.appendChild(svg.bumps({
    width: 720, height: 190, nodes,
    onClick: (n) => ctx.navigate('experiment', { gen: n.id }),
  }));
  ladderCard.appendChild(el('div', { class: 'd-legend' }, [
    el('span', null, [el('i', { class: 'spine' }), 'champion spine (king of the hill)']),
    el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'challenger that fell']),
    el('span', { class: 'd-faint', text: 'real gauntlet · click a node → experiment' }),
  ]));
  body.appendChild(section('The gauntlet ladder · real data', ladderCard));

  // ---- 2. per-round paired slopegraphs (real matchup grids) ----
  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups
    : experiments.filter((x) => x.parent_generation_id).map((x) => ({
        champion: x.parent_generation_id, challenger: x.generation_id,
        decision: normaliseDecision(x.outcome),
      }));
  const grids = await Promise.all(matchups.map((m) =>
    (m.champion && m.challenger) ? D.matchupGrid(epochId, m.champion, m.challenger) : Promise.resolve(null)));

  const slopeWrap = el('div', { class: 'd-pslope-grid' });
  let anyGrid = false;
  matchups.forEach((m, i) => {
    const grid = grids[i];
    const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
    const series = rows
      .filter((r) => svg.isNum(r.parent_drift_loss) || svg.isNum(r.child_drift_loss))
      .map((r) => ({
        label: r.entry_id, id: r.entry_id,
        a: svg.isNum(r.parent_drift_loss) ? r.parent_drift_loss : NaN,
        b: svg.isNum(r.child_drift_loss) ? r.child_drift_loss : NaN,
        verdict: r.verdict,
      }));
    const cell = el('div', { class: 'd-pslope-cell' });
    const dec = m.decision || 'rejected';
    cell.appendChild(el('div', { class: 'd-pslope-title' }, [
      el('span', { class: 'd-mono', text: `${m.champion} → ${m.challenger}` }),
      el('span', { class: `d-pill d-${dec}`, text: dec }),
    ]));
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
  const slopeCard = el('div', { class: 'd-panel' });
  slopeCard.appendChild(slopeWrap);
  if (anyGrid) {
    slopeCard.appendChild(el('div', { class: 'd-legend' }, [
      el('span', null, [el('i', { class: 'good' }), 'entry improved (loss ↓)']),
      el('span', null, [el('i', { class: 'bad' }), 'entry regressed (loss ↑)']),
      el('span', { class: 'd-faint', text: 'lines de-collided + jittered so coincident values stay legible · click → run' }),
    ]));
  }
  body.appendChild(section('Per-round paired duels · champion → challenger, per board', slopeCard));

  // ---- 3. alternative styles (illustrative) ----
  const allIds = experiments.map((x) => x.generation_id);
  const champ = nodes.find((n) => n.promoted);
  const championId = champ ? champ.id : (allIds[0] || null);
  const challengerIds = allIds.filter((id) => id !== championId);
  const lossById = {};
  for (const id of allIds) lossById[id] = scalarByGen.get(id);

  const altWrap = el('div', { class: 'd-alt-grid' });

  // 3a. single-elim bracket
  const winnerId = championId; // the gauntlet's standing champion
  altWrap.appendChild(altCell(
    'Single-elimination bracket',
    'A bracket triages a large field by discarding losers — the wrong primitive here (noise-fragile at the boundary; a strong candidate dies to one unlucky run). SELECTION.md §6.',
    svg.bracketMini({ width: 340, champion: championId, challengers: challengerIds.map((id) => ({ id })), winner: winnerId }),
  ));

  // 3b. round-robin matrix
  altWrap.appendChild(altCell(
    'Round-robin / Swiss matrix',
    'Every candidate vs every other on aggregate loss. The right instinct (rank a whole field, no single-loss death) but superseded by iterated racing. SELECTION.md §5.',
    svg.roundRobinMatrix({ ids: allIds, lossById, cell: 34 }),
  ));

  // 3c. race lanes (successive halving)
  const finiteLoss = allIds.map((id) => lossById[id]).filter(svg.isNum);
  const cut = finiteLoss.length ? median(finiteLoss) : null;
  const runners = allIds.map((id) => ({
    id, loss: lossById[id],
    eliminated: svg.isNum(lossById[id]) && cut != null ? lossById[id] > cut : false,
  }));
  altWrap.appendChild(altCell(
    'Racing / successive-halving lanes',
    'Each candidate on its own lane; the cut eliminates the worse half, survivors keep racing. The convergent recommendation is racing + replication. SELECTION.md §2/§5.',
    svg.raceLanes({ width: 380, runners, cut }),
  ));

  const altCard = el('div', { class: 'd-panel' });
  altCard.appendChild(el('div', { class: 'd-illustrative-banner', text: 'Illustrative only — these arrange the SAME candidate set under alternative structures. Only the gauntlet above has real per-round data.' }));
  altCard.appendChild(altWrap);
  body.appendChild(section('Alternative tournament styles', altCard));
}

function altCell(title, note, mark) {
  return el('figure', { class: 'd-alt-cell' }, [
    el('figcaption', { class: 'd-alt-cap' }, [
      el('span', { class: 'd-alt-title', text: title }),
      el('span', { class: 'd-pill d-baseline', text: 'illustrative' }),
    ]),
    mark,
    el('p', { class: 'd-alt-note d-faint', text: note }),
  ]);
}

function median(xs) {
  const s = xs.slice().sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
