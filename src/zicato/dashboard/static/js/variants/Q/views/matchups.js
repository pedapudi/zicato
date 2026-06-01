// variants/Q/views/matchups.js — MATCH-UPS across tournament styles, focused
// on ONE candidate (fix #3: ALL of its match-ups).
//
// zicato RUNS a king-of-the-hill gauntlet. Atlas IV's match-ups screen, reached
// from a candidate, shows EVERY round that candidate appeared in (as champion
// OR challenger) — never just one (the O bug). For each:
//   0. the Tufte SANKEY (fit-to-width, NO viewport) for the focus round;
//   1. the gauntlet LADDER — a non-colliding bumps chart;
//   2. per-round PAIRED SLOPEGRAPHS — champion → challenger per board entry;
//   3. the PROMOTE GATE for each round — stacked, non-overlapping (fix #1);
//   4. ALTERNATIVE styles (illustrative overlays on the SAME candidate set).
//
// Also exports gatePanel() so the candidate page can mount the same stacked,
// non-overlapping gate (fix #1) without duplicating the layout.
//
// Data: /api/epoch, /api/score-trajectory, /api/tournaments,
// /api/matchup-grid/{e}/{champ}/{chall}, /api/round/{e}/{champ}/{chall}/gate.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, subhead, verdictPill, normaliseDecision } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dq-empty', text: 'Reading tournaments…' }));

  const [ep, traj, bracket] = await Promise.all([D.epoch(), D.scoreTrajectory(), D.bracket()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dq-h1', text: 'Match-ups' }), empty('No current epoch.')]);
    return;
  }
  const epochId = (params && params.epochId) || ep.epoch_id;
  const focusGen = (params && params.gen) || null;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  const ladderNodes = experiments.map((x, i) => ({
    id: x.generation_id, x: i,
    promoted: normaliseDecision(x.outcome) === 'promoted',
    scalar: scalarByGen.get(x.generation_id),
    parent: x.parent_generation_id || null,
  }));
  if (ladderNodes.length && !ladderNodes.some((n) => n.promoted)) ladderNodes[0].promoted = true;

  // ALL match-ups (filtered to the focus candidate when one is selected).
  const allMatchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups
    : experiments.filter((x) => x.parent_generation_id).map((x) => ({ champion: x.parent_generation_id, challenger: x.generation_id, decision: normaliseDecision(x.outcome) }));
  const matchups = focusGen
    ? allMatchups.filter((m) => m.champion === focusGen || m.challenger === focusGen)
    : allMatchups;

  const grids = await Promise.all(matchups.map((m) => (m.champion && m.challenger) ? D.matchupGrid(epochId, m.champion, m.challenger) : Promise.resolve(null)));
  const gates = await Promise.all(matchups.map((m) => (m.champion && m.challenger) ? D.gate(epochId, m.champion, m.challenger) : Promise.resolve(null)));

  const allIds = experiments.map((x) => x.generation_id);
  const champNode = ladderNodes.find((n) => n.promoted);
  const championId = champNode ? champNode.id : (allIds[0] || null);
  const lossById = {};
  for (const id of allIds) lossById[id] = scalarByGen.get(id);

  const focusIdx = matchups.length ? 0 : -1;
  const focusGrid = focusIdx >= 0 ? grids[focusIdx] : null;
  const focus = focusIdx >= 0 ? matchups[focusIdx] : null;

  const digest = JSON.stringify({
    epochId, focusGen,
    nodes: ladderNodes.map((n) => [n.id, n.x, n.promoted, svg.isNum(n.scalar) ? n.scalar.toFixed(3) : null, n.parent]),
    matchups: matchups.map((m, i) => [m.champion, m.challenger, m.decision,
      grids[i] && Array.isArray(grids[i].entry_grid) ? grids[i].entry_grid.map((r) => [r.entry_id, r.parent_drift_loss, r.child_drift_loss, r.verdict]) : null]),
    championId,
    gates: gates.map((g) => g && Array.isArray(g.rules)
      ? [g.decision, svg.isNum(g.delta_scalar) ? g.delta_scalar.toFixed(3) : null, g.rules.map((r) => [r.id, r.status, r.fired])]
      : null),
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'dq-pagehead' }, [
      el('h1', { class: 'dq-h1', text: focusGen ? `Match-ups · ${focusGen}` : 'Match-ups across tournament styles' }),
      el('p', { class: 'dq-lede', text: focusGen
        ? `Every round ${focusGen} appeared in — the causal flow, the paired per-board duels, and the promote gate for each. Lower drift loss is better.`
        : 'zicato runs a king-of-the-hill gauntlet — one champion, one challenger per round, paired per-board duels.' }),
    ]));

    if (!matchups.length) {
      out.push(section('Match-ups', el('div', { class: 'dq-panel' }, [empty('This candidate did not run as part of any tournament round.')])));
      return out;
    }

    // 0. Tufte Sankey
    out.push(section('Causal flow · challenger → per-board drift → gate', sankeyCard(focus, focusGrid, ctx, epochId)));

    // 1. gauntlet ladder
    const ladder = el('div', { class: 'dq-panel' });
    ladder.appendChild(svg.bumps({ width: 760, height: 180, nodes: ladderNodes, onClick: (n) => ctx.navigate('matchups', { epochId, gen: n.id }) }));
    ladder.appendChild(el('div', { class: 'dq-legend' }, [
      el('span', null, [el('i', { class: 'spine' }), 'champion spine (king of the hill)']),
      el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'challenger that fell']),
      el('span', { class: 'dq-faint', text: 'real gauntlet · click a node → its match-ups' }),
    ]));
    out.push(section('The gauntlet ladder · real data', ladder));

    // 2. paired slopegraphs
    const slopeWrap = el('div', { class: 'dq-pslope-grid' });
    let anyGrid = false;
    matchups.forEach((m, i) => {
      const grid = grids[i];
      const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
      const series = rows
        .filter((r) => svg.isNum(r.parent_drift_loss) || svg.isNum(r.child_drift_loss))
        .map((r) => ({ label: r.entry_id, id: r.entry_id, a: svg.isNum(r.parent_drift_loss) ? r.parent_drift_loss : NaN, b: svg.isNum(r.child_drift_loss) ? r.child_drift_loss : NaN, verdict: r.verdict }));
      const dec = m.decision || 'rejected';
      const cell = el('div', { class: 'dq-pslope-cell' }, [
        el('div', { class: 'dq-pslope-title' }, [
          el('span', { class: 'dq-mono', text: `${m.champion} → ${m.challenger}` }),
          el('span', { class: `dq-pill dq-${dec}`, text: dec }),
        ]),
      ]);
      if (series.length) {
        anyGrid = true;
        cell.appendChild(svg.pairedSlopegraph({
          width: 460, height: Math.max(220, 50 + series.length * 28),
          left: { title: `champion ${m.champion}` }, right: { title: `challenger ${m.challenger}` },
          labelGap: 140, goodDirection: 'down', series,
          onClick: (s) => ctx.navigate('run', { epochId, gen: m.challenger, entry: s.id }),
        }));
      } else {
        cell.appendChild(empty('No paired per-board grid for this round (loss files missing).'));
      }
      slopeWrap.appendChild(cell);
    });
    const slopeCard = el('div', { class: 'dq-panel' });
    slopeCard.appendChild(slopeWrap);
    if (anyGrid) slopeCard.appendChild(el('div', { class: 'dq-legend' }, [
      el('span', null, [el('i', { class: 'good' }), 'entry improved (loss ↓)']),
      el('span', null, [el('i', { class: 'bad' }), 'entry regressed (loss ↑)']),
      el('span', { class: 'dq-faint', text: 'lines de-collided + jittered · click → run' }),
    ]));
    out.push(section('Per-round paired duels · champion → challenger, per board', slopeCard));

    // 3. the PROMOTE GATE — clean STACKED sections (fix #1).
    matchups.forEach((m, i) => {
      const gate = gates[i];
      if (!gate || !Array.isArray(gate.rules)) return;
      out.push(section(`Promote gate · ${m.champion} → ${m.challenger}`, gatePanel(gate, m)));
    });

    // 4. alternative styles (illustrative)
    const challengerIds = allIds.filter((id) => id !== championId);
    const altWrap = el('div', { class: 'dq-alt-grid' });
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
    const altCard = el('div', { class: 'dq-panel' });
    altCard.appendChild(el('div', { class: 'dq-illustrative-banner', text: 'Illustrative only — these arrange the SAME candidate set under alternative structures. Only the gauntlet above has real per-round data.' }));
    altCard.appendChild(altWrap);
    out.push(section('Alternative tournament styles', altCard));
    return out;
  });
}

function sankeyCard(focus, grid, ctx, epochId) {
  const card = el('div', { class: 'dq-panel' });
  const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
  if (!focus || !rows.length) {
    card.appendChild(empty('No round with a paired per-board grid yet — the Sankey needs at least one decided matchup.'));
    return card;
  }
  const challenger = focus.challenger;
  const champion = focus.champion;
  const dec = focus.decision || 'rejected';

  const patchNodes = [{ id: 'patch', label: `patch ${champion}→${challenger}`, sub: 'mutation', value: 0, cls: 'dn-sankey-patch' }];
  const driftNodes = [];
  const links = [];
  let total = 0;
  for (const r of rows) {
    const v = svg.isNum(r.child_drift_loss) ? r.child_drift_loss : 0;
    if (!(v > 0)) continue;
    total += v;
    const verdict = r.verdict || 'flat';
    const cls = verdict === 'improved' ? 'dn-good' : verdict === 'regressed' ? 'dn-bad' : 'dn-flat';
    driftNodes.push({ id: 'd_' + r.entry_id, label: r.entry_id, sub: svg.fmt(v, 0) + ' loss', value: v, cls, ref: r.entry_id });
    links.push({ source: 'patch', target: 'd_' + r.entry_id, value: v, cls });
  }
  patchNodes[0].value = total || 1;
  const gateCls = dec === 'promoted' ? 'dn-good' : 'dn-bad';
  const gateNodes = [{ id: 'gate', label: dec === 'promoted' ? '♛ promoted' : '✕ rejected', sub: 'Σ ' + svg.fmt(total, 0), value: total || 1, cls: gateCls }];
  for (const d of driftNodes) links.push({ source: d.id, target: 'gate', value: d.value, cls: d.cls });

  card.appendChild(svg.sankey({
    width: 760, colHeight: Math.max(220, 40 + driftNodes.length * 32), nodeW: 150,
    patch: patchNodes, drift: driftNodes, gate: gateNodes, links,
    onNode: (n) => { if (n.ref) ctx.navigate('run', { epochId, gen: challenger, entry: n.ref }); },
  }));
  card.appendChild(el('div', { class: 'dq-legend' }, [
    el('span', null, [el('i', { class: 'good' }), 'entry improved']),
    el('span', null, [el('i', { class: 'bad' }), 'entry regressed']),
    el('span', { class: 'dq-faint', text: 'fit-to-width · no pan/zoom · ribbon width = per-board loss share · click a drift node → its run' }),
  ]));
  return card;
}

// The promote-gate decomposition, laid out as clean STACKED sections so nothing
// overlaps (fix #1): (a) decision header — pill + Δscalar / Δpass-rate + reason;
// (b) the rules ladder, each rule its OWN row; (c) a SEPARATE
// champion-vs-challenger scalar-components comparison block below. Exported so
// the candidate page mounts the same gate.
export function gatePanel(gate, m) {
  const card = el('div', { class: 'dq-panel dq-gate' });

  const decision = normaliseDecision(gate) || gate.decision || 'rejected';
  card.appendChild(el('div', { class: 'dq-gate-head' }, [
    el('div', { class: 'dq-gate-decision' }, [verdictPill(decision)]),
    el('div', { class: 'dq-row dq-gate-deltas' }, [
      svg.isNum(gate.delta_scalar) ? stat(svg.fmtSigned(gate.delta_scalar, 2), 'Δ scalar (loss)') : null,
      svg.isNum(gate.delta_pass_rate) ? stat(svg.fmtSigned(gate.delta_pass_rate, 2), 'Δ pass rate') : null,
      gate.primary_driver && gate.primary_driver.judge
        ? stat(gate.primary_driver.judge, 'primary driver') : null,
    ].filter(Boolean)),
  ].filter(Boolean)));
  if (gate.reason) card.appendChild(el('p', { class: 'dq-gate-reason', text: gate.reason }));

  const rules = Array.isArray(gate.rules) ? gate.rules : [];
  if (rules.length) {
    card.appendChild(subhead('Rules · short-circuiting, in order'));
    const ladder = el('ol', { class: 'dq-rules' });
    for (const r of rules) {
      const st = String(r.status || 'pending');
      ladder.appendChild(el('li', { class: 'dq-rule dq-rule-' + st }, [
        el('span', { class: 'dq-rule-dot', 'aria-hidden': 'true' }),
        el('span', { class: 'dq-rule-label', text: r.label || r.id }),
        el('span', { class: 'dq-rule-status', text: st.replace(/_/g, ' ') }),
        el('span', { class: 'dq-rule-detail dq-faint', text: r.detail || '' }),
      ]));
    }
    card.appendChild(ladder);
  }

  const sc = gate.scalar_components;
  if (sc && sc.champion && sc.challenger) {
    const keys = [...new Set([...Object.keys(sc.champion), ...Object.keys(sc.challenger)])].sort();
    if (keys.length) {
      card.appendChild(subhead(`Scalar components · champion ${m.champion} vs challenger ${m.challenger}`));
      const tbl = el('table', { class: 'dq-sc-table' });
      tbl.appendChild(el('thead', null, [el('tr', null, [
        el('th', { text: 'component' }),
        el('th', { class: 'dq-num', text: m.champion }),
        el('th', { class: 'dq-num', text: m.challenger }),
        el('th', { class: 'dq-num', text: 'Δ' }),
      ])]));
      const tbody = el('tbody');
      for (const k of keys) {
        const a = svg.isNum(sc.champion[k]) ? sc.champion[k] : 0;
        const b = svg.isNum(sc.challenger[k]) ? sc.challenger[k] : 0;
        const d = b - a;
        const dCls = d > 0 ? 'dq-bad-t' : d < 0 ? 'dq-good-t' : '';
        tbody.appendChild(el('tr', null, [
          el('td', { class: 'dq-mono', text: k }),
          el('td', { class: 'dq-num dq-mono', text: svg.fmt(a, 2) }),
          el('td', { class: 'dq-num dq-mono', text: svg.fmt(b, 2) }),
          el('td', { class: 'dq-num dq-mono ' + dCls, text: svg.fmtSigned(d, 2) }),
        ]));
      }
      tbl.appendChild(tbody);
      card.appendChild(tbl);
    }
  }
  return card;
}

function altCell(title, note, mark) {
  return el('figure', { class: 'dq-alt-cell' }, [
    el('figcaption', { class: 'dq-alt-cap' }, [
      el('span', { class: 'dq-alt-title', text: title }),
      el('span', { class: 'dq-pill dq-baseline', text: 'illustrative' }),
    ]),
    mark,
    el('p', { class: 'dq-alt-note dq-faint', text: note }),
  ]);
}

function median(xs) {
  const s = xs.slice().sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}
