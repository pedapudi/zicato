// variants/E/views/epoch.js — EPOCH: the data substrate of one epoch.
//
// Atlas's epoch screen leads with the OBJECTIVE, then the three figures the
// synthesis asks for, all from D's data-viz:
//   * the lineage as a non-colliding BUMPS chart (champion spine lane +
//     rejected-challenger lane);
//   * the board entries × generations drift-loss HEATMAP (hover → exact
//     value, click → that candidate);
//   * the board TRELLIS — small multiples, one micro sparkbar per board
//     entry across the candidate generations, on a shared loss scale.
//
// Data: /api/epoch (goal, brief, board, experiments), /api/lineage,
// /api/score-trajectory, /api/generation/{e}/{g}/per-entry.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, renderMarkdown, normaliseDecision } from '../ui.js';

const KIND_ORDER = { multi_turn_emulated: 0, multi_turn_scripted: 1, single_turn: 2 };
const KIND_LABEL = {
  single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn',
  multi_turn_emulated: 'emulated multi-turn',
};

export async function render(host, ctx) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'd-empty', text: 'Reading epoch contract…' }));

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [
      el('h1', { class: 'd-h1', text: 'Epoch' }),
      empty('No current epoch.'),
    ]);
    return;
  }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const board = Array.isArray(ep.board) ? ep.board : [];

  // Candidate set + parent/promoted (prefer lineage endpoint).
  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  // Per-entry losses for every generation (cached).
  const perEntries = await Promise.all(gens.map((g) => D.perEntry(epochId, g.id)));
  const rowByGenEntry = new Map();
  const lossLookup = new Map();
  const entryIds = new Set();
  const allLoss = [];
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) {
      rowByGenEntry.set(`${g.id}|${r.entry_id}`, r);
      entryIds.add(r.entry_id);
      if (svg.isNum(r.drift_loss)) { lossLookup.set(`${r.entry_id}|${g.id}`, r.drift_loss); allLoss.push(r.drift_loss); }
    }
  });
  for (const b of board) { const id = b.entry_id || b.id; if (id) entryIds.add(id); }
  const domain = allLoss.length ? svg.extent(allLoss) : null;

  // Digest: epoch id + goal + brief length + gen/parent/promoted/scalar +
  // every (entry|gen) loss + board kinds. No timestamps.
  const digest = JSON.stringify({
    epochId, goal: ep.goal || '', briefLen: (ep.brief || '').length, closed: !!ep.closed,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    loss: [...lossLookup.entries()].sort(),
    board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight, b.budget_s]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'e-pagehead' }, [
      el('h1', { class: 'd-h1', text: `Epoch ${epochId}` }),
      el('div', { class: 'd-objective' }, [
        el('div', { class: 'lab', text: 'objective' }),
        el('div', { class: 'txt', text: ep.goal && ep.goal.trim() ? ep.goal : '(no objective recorded)' }),
      ]),
    ]));

    const promotedCount = gens.filter((g) => g.promoted).length;
    nodes.push(el('div', { class: 'd-panel d-row', style: 'margin-top:12px;' }, [
      stat(String(board.length), 'board entries'),
      stat(String(experiments.length), 'experiments'),
      stat(String(promotedCount), 'promoted'),
      stat(ep.closed ? 'closed' : 'open', 'state'),
    ]));

    // proposer brief (collapsible)
    const briefText = ep.brief || '';
    const briefDetails = el('details', { class: 'd-brief', open: briefText.length < 1200 ? '' : null }, [
      el('summary', null, [
        el('span', { class: 'chev', text: '▸' }), 'Proposer brief',
        el('span', { class: 'd-faint', style: 'font-weight:400;font-size:11px;', text: briefText ? `· ${briefText.split(/\n/).length} lines` : '· none' }),
      ]),
      renderMarkdown(briefText),
    ]);
    nodes.push(section('Operator’s brief to the proposer', briefDetails));

    // ---- lineage bumps ----
    const bumpNodes = gens.map((g, i) => ({ id: g.id, x: i, promoted: g.promoted, scalar: scalarByGen.get(g.id), parent: g.parent }));
    if (bumpNodes.length && !bumpNodes.some((n) => n.promoted)) bumpNodes[0].promoted = true;
    const bumpsCard = el('div', { class: 'd-panel' });
    bumpsCard.appendChild(svg.bumps({ width: 720, height: 190, nodes: bumpNodes, onClick: (n) => ctx.navigate('candidate', { gen: n.id }) }));
    bumpsCard.appendChild(legend([
      ['spine', 'champion spine (promoted lineage)'],
      ['dotpred-bad', 'rejected challenger'],
    ], 'click a node → its candidate'));
    nodes.push(section('Lineage', bumpsCard));

    // ---- board entries × generations heatmap ----
    const rows = [...entryIds].sort().map((id) => ({ id, label: id }));
    const cols = gens.map((g) => ({ id: g.id, label: g.id }));
    const hmCard = el('div', { class: 'd-panel', style: 'overflow-x:auto;' });
    if (rows.length && cols.length) {
      hmCard.appendChild(svg.heatmap({
        rows, cols, diverging: false,
        value: (r, c) => (lossLookup.has(`${r}|${c}`) ? lossLookup.get(`${r}|${c}`) : null),
        onClick: (rId, cId) => ctx.navigate('candidate', { gen: cId }),
      }));
      hmCard.appendChild(el('p', { class: 'd-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'cell = drift loss for one board entry in one generation · darker = more drift · click → candidate' }));
    } else {
      hmCard.appendChild(empty('No per-entry loss profiles yet (the index may not be built).'));
    }
    nodes.push(section('Board entries × generations · drift loss', hmCard));

    // ---- board trellis (small multiples) ----
    nodes.push(section('Board trellis · drift loss across candidates', boardTrellis(board, gens, rowByGenEntry, domain, ctx)));
    return nodes;
  });
}

function boardTrellis(board, gens, rowByGenEntry, domain, ctx) {
  if (!board.length) return el('div', { class: 'd-panel' }, [empty('No board entries recorded.')]);
  const sorted = board.slice().sort((a, b) => {
    const ka = KIND_ORDER[a.kind] ?? 9; const kb = KIND_ORDER[b.kind] ?? 9;
    if (ka !== kb) return ka - kb;
    const wa = svg.isNum(a.weight) ? a.weight : 0; const wb = svg.isNum(b.weight) ? b.weight : 0;
    if (wa !== wb) return wb - wa;
    return String(a.entry_id || a.id).localeCompare(String(b.entry_id || b.id));
  });
  const trellis = el('div', { class: 'd-trellis' });
  const genIds = gens.map((g) => g.id);
  for (const b of sorted) {
    const eid = b.entry_id || b.id;
    const bars = genIds.map((g) => {
      const r = rowByGenEntry.get(`${g}|${eid}`);
      return { label: g, value: r && svg.isNum(r.drift_loss) ? r.drift_loss : NaN, fail: r ? r.pass_fail === 0 : false, timeout: r ? !!r.wall_clock_budget_exceeded : false };
    });
    const cells = genIds.map((g) => {
      const r = rowByGenEntry.get(`${g}|${eid}`);
      return r ? { label: g, pass: r.pass_fail, timeout: !!r.wall_clock_budget_exceeded, ran: true } : { label: g, ran: false };
    });
    const cell = el('figure', { class: 'd-trellis-cell' }, [
      el('figcaption', { class: 'd-trellis-cap' }, [
        el('span', { class: 'd-trellis-id', text: String(eid) }),
        el('span', { class: 'd-trellis-meta' }, [
          el('span', { class: 'd-kind-tag d-kind-' + (b.kind || 'unknown'), text: KIND_LABEL[b.kind] || b.kind || '—' }),
          b.expectation_kind ? el('span', { class: 'd-faint', text: ' · ' + b.expectation_kind }) : null,
        ].filter(Boolean)),
      ]),
      svg.sparkbar({ width: 200, height: 42, bars, domain: domain || undefined }),
      svg.genDots({ width: 200, height: 14, cells }),
      el('div', { class: 'd-trellis-foot d-faint' }, [
        el('span', { text: svg.isNum(b.budget_s) ? `${b.budget_s}s budget` : 'no budget' }),
        el('span', { text: svg.isNum(b.weight) ? `w ${svg.fmt(b.weight, 1)}` : 'w —' }),
        Array.isArray(b.tags) && b.tags.length ? el('span', { class: 'd-trellis-tags', text: b.tags.slice(0, 3).join(' ') }) : null,
      ].filter(Boolean)),
      b.input_preview ? el('div', { class: 'd-trellis-preview d-faint', title: b.input_preview, text: '“' + (b.input_preview.length > 64 ? b.input_preview.slice(0, 63) + '…' : b.input_preview) + '”' }) : null,
    ].filter(Boolean));
    cell.style.cursor = 'pointer';
    cell.addEventListener('click', () => ctx.navigate('candidate', { gen: genIds[genIds.length - 1], entry: eid }));
    trellis.appendChild(cell);
  }
  const card = el('div', { class: 'd-panel' });
  card.appendChild(trellis);
  card.appendChild(legend([
    ['spine', 'one bar per candidate · drift loss (shared scale)'],
    ['dotact', 'pass'], ['dotpred-bad', 'fail'],
  ], '⏱ timeout · click a board → its candidate drill-down'));
  return card;
}

function legend(items, foot) {
  const kids = items.map(([cls, label]) => {
    let i;
    if (cls === 'spine') i = el('i', { class: 'spine' });
    else if (cls === 'dotact') i = el('i', { class: 'dotact' });
    else i = el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' });
    return el('span', null, [i, label]);
  });
  if (foot) kids.push(el('span', { class: 'd-faint', text: foot }));
  return el('div', { class: 'd-legend' }, kids);
}
