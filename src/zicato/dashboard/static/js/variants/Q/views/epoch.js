// variants/Q/views/epoch.js — EPOCH OVERVIEW: the data substrate of one epoch.
//
// Atlas IV's epoch screen leads with the OBJECTIVE, the collapsible proposer
// brief, then the figures: a non-colliding lineage BUMPS chart and the board
// entries × generations drift-loss HEATMAP (theme-aware). Quick links jump to
// the mutation surface, publication, and the Boards view.
//
// FIX #6 (trellis vs heatmap de-dup): the HEATMAP stays HERE at the epoch
// overview; the board TRELLIS (small multiples) lives in the Boards view —
// never both on one page.
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, renderMarkdown, normaliseDecision } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dq-empty', text: 'Reading epoch contract…' }));

  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dq-h1', text: 'Epoch' }), empty('No current epoch.')]);
    return;
  }
  // The selected epoch (tree-driven, URL-encoded); fall back to the current one.
  const epochId = (params && params.epochId) || ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const board = Array.isArray(ep.board) ? ep.board : [];

  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.filter((g) => !g.epoch_id || g.epoch_id === epochId)
      .map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' }));

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);

  const perEntries = await Promise.all(gens.map((g) => D.perEntry(epochId, g.id)));
  const lossLookup = new Map();
  const entryIds = new Set();
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) {
      entryIds.add(r.entry_id);
      if (svg.isNum(r.drift_loss)) lossLookup.set(`${r.entry_id}|${g.id}`, r.drift_loss);
    }
  });
  for (const b of board) { const id = b.entry_id || b.id; if (id) entryIds.add(id); }

  const digest = JSON.stringify({
    epochId, goal: ep.goal || '', briefLen: (ep.brief || '').length, closed: !!ep.closed,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    loss: [...lossLookup.entries()].sort(),
    board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight, b.budget_s]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dq-pagehead' }, [
      el('h1', { class: 'dq-h1', text: `Epoch ${epochId}` }),
      el('div', { class: 'dq-objective' }, [
        el('div', { class: 'lab', text: 'objective' }),
        el('div', { class: 'txt', text: ep.goal && ep.goal.trim() ? ep.goal : '(no objective recorded)' }),
      ]),
    ]));

    const promotedCount = gens.filter((g) => g.promoted).length;
    nodes.push(el('div', { class: 'dq-panel dq-row', style: 'margin-top:14px;' }, [
      stat(String(board.length), 'board entries'),
      stat(String(gens.length), 'generations'),
      stat(String(promotedCount), 'promoted'),
      stat(ep.closed ? 'closed' : 'open', 'state'),
    ]));

    nodes.push(el('div', { class: 'dq-quicklinks' }, [
      el('a', { class: 'dq-linkbtn', href: ctx.href('board', { epochId }), text: 'Boards · trellis + cross-candidate →' }),
      el('a', { class: 'dq-linkbtn', href: ctx.href('mutations', { epochId }), text: 'Mutation surface + diff →' }),
      el('a', { class: 'dq-linkbtn', href: ctx.href('publication', { epochId }), text: 'Epoch publication (ACM) →' }),
    ]));

    const briefText = ep.brief || '';
    const briefDetails = el('details', { class: 'dq-brief', open: briefText.length < 1200 ? '' : null }, [
      el('summary', null, [
        el('span', { class: 'chev', text: '▸' }), 'Proposer brief',
        el('span', { class: 'dq-faint', style: 'font-weight:400;font-size:11px;', text: briefText ? `· ${briefText.split(/\n/).length} lines` : '· none' }),
      ]),
      renderMarkdown(briefText),
    ]);
    nodes.push(section('Operator’s brief to the proposer', briefDetails));

    // ---- lineage bumps (non-colliding, clickable → candidate) ----
    const bumpNodes = gens.map((g, i) => ({ id: g.id, x: i, promoted: g.promoted, scalar: scalarByGen.get(g.id), parent: g.parent }));
    if (bumpNodes.length && !bumpNodes.some((n) => n.promoted)) bumpNodes[0].promoted = true;
    const bumpsCard = el('div', { class: 'dq-panel' });
    bumpsCard.appendChild(svg.bumps({ width: 760, height: 180, nodes: bumpNodes, onClick: (n) => ctx.navigate('gen', { epochId, gen: n.id }) }));
    bumpsCard.appendChild(legend([['spine', 'champion spine (promoted lineage)'], ['dotpred-bad', 'rejected challenger']], 'click a node → its candidate'));
    nodes.push(section('Lineage', bumpsCard));

    // ---- board entries × generations heatmap (de-dup: heatmap lives HERE) ----
    const rows = [...entryIds].sort().map((id) => ({ id, label: id }));
    const cols = gens.map((g) => ({ id: g.id, label: g.id }));
    const hmCard = el('div', { class: 'dq-panel', style: 'overflow-x:auto;' });
    if (rows.length && cols.length) {
      hmCard.appendChild(svg.heatmap({
        rows, cols,
        value: (r, c) => (lossLookup.has(`${r}|${c}`) ? lossLookup.get(`${r}|${c}`) : null),
        // a cell routes to the PER-BOARD cross-candidate view, keyed by entry id.
        onClick: (rId) => ctx.navigate('board', { epochId, entry: rId }),
      }));
      hmCard.appendChild(el('p', { class: 'dq-faint', style: 'font-size:11px;margin:10px 0 0;', text: 'cell = drift loss for one board entry in one generation · denser ink = more drift · click a row → that board across every candidate' }));
    } else {
      hmCard.appendChild(empty('No per-entry loss profiles yet (the index may not be built).'));
    }
    nodes.push(section('Board entries × generations · drift loss', hmCard));
    return nodes;
  });
}

function legend(items, foot) {
  const kids = items.map(([cls, label]) => {
    let i;
    if (cls === 'spine') i = el('i', { class: 'spine' });
    else if (cls === 'dotact') i = el('i', { class: 'dotact' });
    else i = el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' });
    return el('span', null, [i, label]);
  });
  if (foot) kids.push(el('span', { class: 'dq-faint', text: foot }));
  return el('div', { class: 'dq-legend' }, kids);
}
