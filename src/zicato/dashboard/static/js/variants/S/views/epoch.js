// variants/S/views/epoch.js — EPOCH overview.
//
// The epoch's substrate: objective + proposer brief, a non-colliding clickable
// lineage BUMPS chart, and the board×generation drift-loss HEATMAP. Per fix #6
// the compact HEATMAP lives HERE (epoch overview); the board TRELLIS lives in
// the Boards view — NEVER both on one page. Heatmap cells route to the
// per-board cross-candidate view, keyed by the entry (row) id.
//
// Data: /api/epoch, /api/lineage, /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import * as model from '../model.js';
import { gatedSwap, section, empty, stat, renderMarkdown } from '../ui.js';

export async function render(host, ctx, route) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading epoch contract…' }));
  const epochId = (route.params && route.params.epochId) || null;

  const [ep, { gens, championId, scalarByGen }] = await Promise.all([D.epoch(), model.generations()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Epoch' }), empty('No current epoch.')]);
    return;
  }
  const resolvedId = epochId || ep.epoch_id;
  const board = Array.isArray(ep.board) ? ep.board : [];

  const perEntries = await Promise.all(gens.map((g) => D.perEntry(ep.epoch_id, g.id)));
  const lossLookup = new Map();
  const entryIds = new Set();
  const allLoss = [];
  gens.forEach((g, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) for (const r of pe.entries) {
      entryIds.add(r.entry_id);
      if (svg.isNum(r.drift_loss)) { lossLookup.set(`${r.entry_id}|${g.id}`, r.drift_loss); allLoss.push(r.drift_loss); }
    }
  });
  for (const b of board) { const id = b.entry_id || b.id; if (id) entryIds.add(id); }

  const digest = JSON.stringify({
    epochId: resolvedId, goal: ep.goal || '', briefLen: (ep.brief || '').length, closed: !!ep.closed,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    loss: [...lossLookup.entries()].sort(),
    board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dn-pagehead' }, [
      el('h1', { class: 'dn-h1', text: `Epoch ${resolvedId}` }),
      el('div', { class: 'dn-objective' }, [
        el('div', { class: 'lab', text: 'objective' }),
        el('div', { class: 'txt', text: ep.goal && ep.goal.trim() ? ep.goal : '(no objective recorded)' }),
      ]),
    ]));

    const promotedCount = gens.filter((g) => g.promoted).length;
    nodes.push(el('div', { class: 'dn-panel dn-row', style: 'margin-top:12px;' }, [
      stat(String(board.length), 'board entries'),
      stat(String(gens.length), 'generations'),
      stat(String(promotedCount), 'promoted'),
      stat(ep.closed ? 'closed' : 'open', 'state'),
    ]));

    nodes.push(el('div', { class: 'dn-quicklinks' }, [
      el('a', { class: 'dn-linkbtn', href: ctx.href('mutations', { epochId: resolvedId }), text: 'Mutation surface + diff →' }),
      el('a', { class: 'dn-linkbtn', href: ctx.href('publication', { epochId: resolvedId }), text: 'Epoch publication (ACM) →' }),
    ]));

    const briefText = ep.brief || '';
    nodes.push(section('Operator’s brief to the proposer', el('details', { class: 'dn-brief', open: briefText.length < 1200 ? '' : null }, [
      el('summary', null, [
        el('span', { class: 'chev', text: '▸' }), 'Proposer brief',
        el('span', { class: 'dn-faint', style: 'font-weight:400;font-size:11px;', text: briefText ? `· ${briefText.split(/\n/).length} lines` : '· none' }),
      ]),
      renderMarkdown(briefText),
    ])));

    // lineage bumps (non-colliding, clickable → candidate)
    const bumpNodes = gens.map((g, i) => ({ id: g.id, x: i, promoted: g.promoted, scalar: scalarByGen.get(g.id), parent: g.parent }));
    if (bumpNodes.length && !bumpNodes.some((n) => n.promoted)) bumpNodes[0].promoted = true;
    const bumpsCard = el('div', { class: 'dn-panel' });
    bumpsCard.appendChild(svg.bumps({ width: 760, height: 180, nodes: bumpNodes, onClick: (n) => ctx.navigate('candidate', { epochId: resolvedId, gen: n.id }) }));
    bumpsCard.appendChild(el('div', { class: 'dn-legend' }, [
      el('span', null, [el('i', { class: 'spine' }), 'champion spine (promoted lineage)']),
      el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'rejected challenger']),
      el('span', { class: 'dn-faint', text: 'click a node → its candidate' }),
    ]));
    nodes.push(section('Lineage', bumpsCard));

    // board×generation heatmap (ONLY here — fix #6)
    const rows = [...entryIds].sort().map((id) => ({ id, label: id }));
    const cols = gens.map((g) => ({ id: g.id, label: g.id }));
    const hmCard = el('div', { class: 'dn-panel', style: 'overflow-x:auto;' });
    if (rows.length && cols.length) {
      hmCard.appendChild(svg.heatmap({
        rows, cols,
        value: (r, c) => (lossLookup.has(`${r}|${c}`) ? lossLookup.get(`${r}|${c}`) : null),
        onClick: (rId) => ctx.navigate('board', { epochId: resolvedId, entry: rId }),
      }));
      hmCard.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'cell = drift loss for one board entry in one generation · denser ink = more drift · click a row → that board across every candidate (the trellis lives in the Boards view)' }));
    } else {
      hmCard.appendChild(empty('No per-entry loss profiles yet (the index may not be built).'));
    }
    nodes.push(section('Board entries × generations · drift loss', hmCard));
    return nodes;
  });
}
