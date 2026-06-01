// variants/K/views/matchups.js — MATCH-UPS: the gauntlet + paired duels.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, loading, stat, verdictPill, normaliseDecision } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(loading('Reading the gauntlet…'));

  const ep = await D.epoch();
  const epochId = (ep && ep.epoch_id) || (state.epochDef && state.epochDef.epoch_id) || null;
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'vk-h1', text: 'Match-ups' }), empty('No current epoch.')]);
    return;
  }

  const [lineage, tours] = await Promise.all([D.lineage(epochId), D.tournaments()]);
  const gens = (lineage && Array.isArray(lineage.generations)) ? lineage.generations : [];
  const matchups = (tours && Array.isArray(tours.matchups)) ? tours.matchups : [];

  // focused round (from route) or the first round.
  const focusC = (params && params.champion) || (matchups[0] && matchups[0].champion) || null;
  const focusX = (params && params.challenger) || (matchups[0] && matchups[0].challenger) || null;

  const grids = await Promise.all(matchups.map((m) => (m.champion && m.challenger)
    ? D.matchupGrid(epochId, m.champion, m.challenger) : Promise.resolve(null)));
  const gridBy = new Map(matchups.map((m, i) => [m.champion + '>' + m.challenger, grids[i]]));
  const focusGate = (focusC && focusX) ? await D.gate(epochId, focusC, focusX) : null;

  const byId = new Map(gens.map((g) => [g.generation_id, g]));
  const nodes = gens.map((g) => ({
    id: g.generation_id, x: depthOf(g, byId), promoted: !!g.promoted, parent: g.parent_generation_id || null,
  }));

  const digest = JSON.stringify({
    focusC, focusX,
    nodes: nodes.map((n) => [n.id, n.x, n.promoted, n.parent]),
    matchups: matchups.map((m) => [m.champion, m.challenger, m.decision, m.delta_scalar,
      gridRows(gridBy.get(m.champion + '>' + m.challenger))]),
    gate: focusGate && Array.isArray(focusGate.rules) ? focusGate.rules.map((r) => [r.id, r.status, r.fired]) : null,
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'vk-pagehead' }, [
      el('h1', { class: 'vk-h1', text: 'Match-ups' }),
      el('p', { class: 'vk-lede', text: 'The king-of-the-hill gauntlet — every challenger paired against the reigning champion on common random inputs.' }),
    ]));

    // 1. the ladder (bumps).
    const ladder = el('div', { class: 'vk-panel' }, [
      svg.bumps({ width: 720, height: 190, nodes, onClick: (n) => ctx.navigate('candidate', { gen: n.id }) }),
    ]);
    out.push(section('The gauntlet ladder', ladder));

    // 2. per-round paired duels.
    if (!matchups.length) {
      out.push(section('Rounds', empty('No rounds recorded yet.')));
    } else {
      for (const m of matchups) {
        const grid = gridBy.get(m.champion + '>' + m.challenger);
        const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
        const series = rows.map((r) => ({
          label: r.entry_id, id: r.entry_id, a: r.parent_drift_loss, b: r.child_drift_loss, verdict: r.verdict,
        }));
        const focused = m.champion === focusC && m.challenger === focusX;
        const card = el('div', { class: 'vk-panel' + (focused ? ' vk-focused' : '') });
        card.appendChild(el('div', { class: 'vk-round-head' }, [
          el('span', { class: 'vk-mono', text: `${m.champion} → ${m.challenger}` }),
          verdictPill(normaliseDecision(m) || m.decision),
          svg.isNum(m.delta_scalar) ? el('span', { class: 'vk-faint', text: `Δ ${svg.fmtSigned(m.delta_scalar, 2)} scalar` }) : null,
        ].filter(Boolean)));
        card.appendChild(svg.pairedSlopegraph({
          width: 580, height: 300, series,
          left: { title: m.champion }, right: { title: m.challenger },
          onClick: (s) => ctx.navigate('run', { gen: m.challenger, entry: s.id }),
        }));
        card.appendChild(el('p', { class: 'vk-faint vk-fignote', text: 'slope down = the challenger improved on that entry · click a line to open its run' }));
        out.push(section(`Round · ${m.challenger}`, card));
      }
    }

    // 3. the gate for the focused round.
    if (focusGate) out.push(section('The promote gate', gatePanel(focusGate, focusC, focusX)));

    return out;
  });
}

function gatePanel(gate, championId, challengerId) {
  const card = el('div', { class: 'vk-panel' });
  card.appendChild(el('div', { class: 'vk-row' }, [
    verdictPill(normaliseDecision(gate) || gate.decision),
    svg.isNum(gate.delta_scalar) ? stat(svg.fmtSigned(gate.delta_scalar, 2), 'Δ scalar') : null,
    svg.isNum(gate.delta_pass_rate) ? stat(svg.fmtSigned(gate.delta_pass_rate, 2), 'Δ pass rate') : null,
  ].filter(Boolean)));
  if (gate.reason) card.appendChild(el('p', { class: 'vk-soft', text: gate.reason }));

  const rules = Array.isArray(gate.rules) ? gate.rules : [];
  if (rules.length) {
    const ul = el('ol', { class: 'vk-rules' });
    for (const r of rules) {
      ul.appendChild(el('li', { class: 'vk-rule vk-rule-' + (r.status || 'pending') }, [
        el('span', { class: 'vk-rule-label', text: r.label || r.id }),
        el('span', { class: 'vk-rule-status', text: r.status || '—' }),
        r.detail ? el('span', { class: 'vk-rule-detail vk-faint', text: r.detail }) : null,
      ].filter(Boolean)));
    }
    card.appendChild(el('div', { class: 'vk-subhead', text: 'Rules (short-circuiting, in order)' }));
    card.appendChild(ul);
  }

  // scalar component split as a dot-plot of the deltas.
  const sc = gate.scalar_components;
  if (sc && sc.champion && sc.challenger) {
    const keys = [...new Set([...Object.keys(sc.champion), ...Object.keys(sc.challenger)])];
    const items = keys.map((k) => ({ label: k, id: k, value: (sc.challenger[k] || 0) - (sc.champion[k] || 0) }))
      .filter((it) => svg.isNum(it.value)).sort((a, b) => a.value - b.value);
    if (items.length) {
      card.appendChild(el('div', { class: 'vk-subhead', text: 'Scalar components · Δ champion → challenger' }));
      card.appendChild(svg.valueDotPlot({ width: 480, rowHeight: 20, labelWidth: 150, items, reference: { label: 'no change', value: 0 } }));
    }
  }
  return card;
}

function depthOf(g, byId) {
  let depth = 0; let cur = g;
  while (cur && cur.parent_generation_id && byId.has(cur.parent_generation_id)) { depth += 1; cur = byId.get(cur.parent_generation_id); }
  return depth;
}
function gridRows(grid) {
  return (grid && Array.isArray(grid.entry_grid))
    ? grid.entry_grid.map((r) => [r.entry_id, r.parent_drift_loss, r.child_drift_loss, r.verdict]) : null;
}
