// variants/R/views/epoch.js — the EPOCH OVERVIEW detail pane.
//
// Shown in the detail pane when an epoch is selected (col2) but no section item
// drives a deeper view — the at-a-glance substrate of one epoch: the
// objective, the proposer brief, the lineage trajectory, and the board-entries
// × generations drift-loss HEATMAP (theme-aware).
//
// FIX #6 — TRELLIS vs HEATMAP de-dup: the compact HEATMAP stays HERE at the
// epoch overview; the board TRELLIS (small-multiples) lives in the Board detail
// pane. Never both on one page.
//
// Bind: /api/epoch, /api/lineage, /api/score-trajectory, per-entry.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, stat, renderMarkdown, normaliseDecision } from '../ui.js';

export async function render(host, ctx, path) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dr-empty', text: 'Reading epoch contract…' }));
  const [ep, lin, traj] = await Promise.all([D.epoch(), D.lineage(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dr-h1', text: 'Epoch' }), empty('No current epoch.')]);
    return;
  }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const board = Array.isArray(ep.board) ? ep.board : [];
  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
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
    board: board.map((b) => [b.entry_id || b.id, b.kind, b.weight]),
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'dr-pagehead' }, [
      el('h1', { class: 'dr-h1', text: `Epoch ${epochId}` }),
      el('div', { class: 'dr-objective' }, [
        el('div', { class: 'lab', text: 'objective' }),
        el('div', { class: 'txt', text: ep.goal && ep.goal.trim() ? ep.goal : '(no objective recorded)' }),
      ]),
    ]));

    const promotedCount = gens.filter((g) => g.promoted).length;
    nodes.push(el('div', { class: 'dr-panel dr-row', style: 'margin-top:12px;' }, [
      stat(String(board.length), 'board entries'),
      stat(String(experiments.length), 'experiments'),
      stat(String(promotedCount), 'promoted'),
      stat(ep.closed ? 'closed' : 'open', 'state'),
    ]));

    const briefText = ep.brief || '';
    const briefDetails = el('details', { class: 'dr-brief', open: briefText.length < 1200 ? '' : null }, [
      el('summary', null, [
        el('span', { class: 'chev', text: '▸' }), 'Proposer brief',
        el('span', { class: 'dr-faint', style: 'font-weight:400;font-size:11px;', text: briefText ? `· ${briefText.split(/\n/).length} lines` : '· none' }),
      ]),
      renderMarkdown(briefText),
    ]);
    nodes.push(section('Operator’s brief to the proposer', briefDetails));

    // lineage trajectory (a sparkline of the champion-best scalar)
    const trendVals = gens.map((g) => scalarByGen.get(g.id)).filter(svg.isNum);
    if (trendVals.length >= 2) {
      const card = el('div', { class: 'dr-panel' });
      card.appendChild(svg.sparkline({ width: 560, height: 70, values: trendVals, band: true }));
      card.appendChild(el('p', { class: 'dr-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'scalar (loss) per generation · lower is better · open a generation in the Generations column for its lifecycle' }));
      nodes.push(section('Lineage trajectory', card));
    }

    // board entries × generations heatmap (FIX #6: heatmap HERE, trellis in board detail)
    const rows = [...entryIds].sort().map((id) => ({ id, label: id }));
    const cols = gens.map((g) => ({ id: g.id, label: g.id }));
    const hmCard = el('div', { class: 'dr-panel', style: 'overflow-x:auto;' });
    if (rows.length && cols.length) {
      hmCard.appendChild(svg.heatmap({
        rows, cols,
        value: (r, c) => (lossLookup.has(`${r}|${c}`) ? lossLookup.get(`${r}|${c}`) : null),
        onClick: (rId) => ctx.navigate({ section: 'boards', entry: rId }),
      }));
      hmCard.appendChild(el('p', { class: 'dr-faint', style: 'font-size:11px;margin:8px 0 0;', text: 'cell = drift loss for one board entry in one generation · denser ink = more drift · click a row → that board across every candidate (its trellis lives there)' }));
    } else {
      hmCard.appendChild(empty('No per-entry loss profiles yet (the index may not be built).'));
    }
    nodes.push(section('Board entries × generations · drift loss', hmCard));
    return nodes;
  });
}
