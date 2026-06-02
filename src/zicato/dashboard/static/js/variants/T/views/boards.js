// variants/T/views/boards.js — BOARDS group: the board TRELLIS (small-multiples).
//
// The detail pane for the tree's "Boards" group. Per the round-5 de-dup
// decision (fix #6) the board TRELLIS lives HERE — not at the epoch overview
// (where the compact heatmap stays). One small-multiple per board entry: a
// per-candidate drift-loss sparkbar + a pass/fail/timeout dot strip. Every
// card routes to the per-board cross-candidate view (views/board.js) by its
// entry id (fix #7 — never an arbitrary candidate).
//
// Data: /api/epoch (the board), /api/lineage, /api/generation/{e}/{g}/per-entry.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, normaliseDecision, densityTokens } from '../ui.js';

const KIND_ORDER = { multi_turn_emulated: 0, multi_turn_scripted: 1, single_turn: 2 };
const KIND_LABEL = {
  single_turn: 'single-turn', multi_turn_scripted: 'scripted multi-turn',
  multi_turn_emulated: 'emulated multi-turn',
};

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading board trellis…' }));
  const epochParam = (params && params.epochId) || null;

  // Class A: scope to the viewed epoch (route param first).
  const ep = await D.epoch(epochParam);
  const epochId = epochParam || (ep && ep.epoch_id) || null;
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Boards' }), empty('No current epoch.')]);
    return;
  }
  const board = Array.isArray(ep.board) ? ep.board : [];
  const rows = await D.generationsForEpoch(epochId);
  const gens = rows.length
    ? rows.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: g.promoted == null ? null : !!g.promoted }))
    : (Array.isArray(ep.experiments) ? ep.experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' })) : []);

  const perEntries = await Promise.all(gens.map((g) => D.perEntry(epochId, g.id)));
  const rowByGenEntry = new Map();
  const allLoss = [];
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) {
      rowByGenEntry.set(`${g.id}|${r.entry_id}`, r);
      if (svg.isNum(r.drift_loss)) allLoss.push(r.drift_loss);
    }
  });
  const domain = allLoss.length ? svg.extent(allLoss) : null;

  const digest = JSON.stringify({
    epochId,
    board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight, b.budget_s]),
    gens: gens.map((g) => g.id),
    loss: [...rowByGenEntry.entries()].map(([k, r]) => [k, svg.isNum(r.drift_loss) ? r.drift_loss.toFixed(3) : null, r.pass_fail, !!r.wall_clock_budget_exceeded]).sort(),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Boards · ${epochId}` }),
      el('p', { class: 'dn-lede', text: 'The fixed task board for this epoch as small-multiples — one card per entry, every candidate’s drift loss on a shared scale. Open a card for the per-board cross-candidate view + inline transcripts.' }),
    ]));

    nodes.push(el('div', { class: 'dn-panel dn-row' }, [
      stat(String(board.length), 'board entries'),
      stat(String(gens.length), 'candidates'),
      stat(String(board.filter((b) => b.kind === 'single_turn').length), 'single-turn'),
      stat(String(board.filter((b) => b.kind && b.kind.startsWith('multi')).length), 'multi-turn'),
    ]));

    nodes.push(section('Board trellis · drift loss across candidates', trellis(board, gens, rowByGenEntry, domain, epochId, ctx)));
    return nodes;
  });
}

function trellis(board, gens, rowByGenEntry, domain, epochId, ctx) {
  if (!board.length) return el('div', { class: 'dn-panel' }, [empty('No board entries recorded.')]);
  const dt = densityTokens();
  const sorted = board.slice().sort((a, b) => {
    const ka = KIND_ORDER[a.kind] ?? 9; const kb = KIND_ORDER[b.kind] ?? 9;
    if (ka !== kb) return ka - kb;
    const wa = svg.isNum(a.weight) ? a.weight : 0; const wb = svg.isNum(b.weight) ? b.weight : 0;
    if (wa !== wb) return wb - wa;
    return String(a.entry_id || a.id).localeCompare(String(b.entry_id || b.id));
  });
  const grid = el('div', { class: 'dn-trellis' });
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
    const cell = el('figure', { class: 'dn-trellis-cell' }, [
      el('figcaption', { class: 'dn-trellis-cap' }, [
        el('span', { class: 'dn-trellis-id', text: String(eid) }),
        el('span', { class: 'dn-trellis-meta' }, [
          el('span', { class: 'dn-kind-tag dn-kind-' + (b.kind || 'unknown'), text: KIND_LABEL[b.kind] || b.kind || '—' }),
          b.expectation_kind ? el('span', { class: 'dn-faint', text: ' · ' + b.expectation_kind }) : null,
        ].filter(Boolean)),
      ]),
      svg.sparkbar({ width: 200, height: dt.sparkbarH, bars, domain: domain || undefined }),
      svg.genDots({ width: 200, height: Math.round(14 * dt.sizeScale), cells }),
      el('div', { class: 'dn-trellis-foot dn-faint' }, [
        el('span', { text: svg.isNum(b.budget_s) ? `${b.budget_s}s budget` : 'no budget' }),
        el('span', { text: svg.isNum(b.weight) ? `w ${svg.fmt(b.weight, 1)}` : 'w —' }),
        Array.isArray(b.tags) && b.tags.length ? el('span', { class: 'dn-trellis-tags', text: b.tags.slice(0, 3).join(' ') }) : null,
      ].filter(Boolean)),
      b.input_preview ? el('div', { class: 'dn-trellis-preview dn-faint', title: b.input_preview, text: '“' + (b.input_preview.length > 64 ? b.input_preview.slice(0, 63) + '…' : b.input_preview) + '”' }) : null,
    ].filter(Boolean));
    cell.style.cursor = 'pointer';
    // fix #7: route by entry id → per-board cross-candidate view (never v2).
    cell.addEventListener('click', () => ctx.navigate('board', { epochId, entry: eid }));
    grid.appendChild(cell);
  }
  const card = el('div', { class: 'dn-panel' });
  card.appendChild(grid);
  card.appendChild(el('div', { class: 'dn-legend' }, [
    el('span', null, [el('i', { class: 'spine' }), 'one bar per candidate · drift loss (shared scale)']),
    el('span', null, [el('i', { class: 'dotact' }), 'pass']),
    el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
    el('span', { class: 'dn-faint', text: '⏱ timeout · click a board → that entry across every candidate' }),
  ]));
  return card;
}
