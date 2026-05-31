// variants/D/views/epoch.js — the data substrate of one epoch.
//
// Leads with the OBJECTIVE (prominent banner) and gives the long
// proposer BRIEF a clean, readable, collapsible home. Then the epoch's
// data substrate as Tufte marks:
//   * the lineage as a NON-COLLIDING bumps chart — champion spine in its
//     own lane, rejected challengers branched into a distinct lane;
//   * board entries × generations as a themed small-multiples heatmap of
//     drift loss (hover → exact value, click → that experiment);
//   * the per-judge × generation trend as a second quiet heatmap.
//
// Data: /api/epoch (goal, brief, board, experiments, delta summary),
// /api/score-trajectory (per-gen scalars), /api/lineage,
// /api/generation/{e}/{g}/per-entry (board × gen losses),
// /api/epoch/{e}/per-judge-trend.

import { el, clearChildren } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { section, crumb, empty, loading, stat, renderMarkdown, verdictPill, normaliseDecision } from '../ui.js';

export async function render(host, ctx) {
  clearChildren(host);
  host.appendChild(crumb([{ label: 'environment', view: 'environment' }, { label: 'epoch' }]));
  const head = el('div');
  host.appendChild(head);
  const body = el('div');
  host.appendChild(body);
  body.appendChild(loading('Reading epoch contract…'));

  const ep = await D.epoch();
  clearChildren(body);
  if (!ep || ep.epoch_id == null) {
    head.appendChild(el('h1', { class: 'd-h1', text: 'Epoch' }));
    body.appendChild(empty('No current epoch.'));
    return;
  }
  const epochId = ep.epoch_id;

  // ---- objective banner (prominent) ----
  head.appendChild(el('h1', { class: 'd-h1', text: `Epoch ${epochId}` }));
  head.appendChild(el('div', { class: 'd-objective' }, [
    el('div', { class: 'lab', text: 'objective' }),
    el('div', { class: 'txt', text: ep.goal && ep.goal.trim() ? ep.goal : '(no objective recorded)' }),
  ]));

  // ---- headline aggregates ----
  const ds = ep.delta_scalar_summary || {};
  const board = Array.isArray(ep.board) ? ep.board : [];
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const promoted = experiments.filter((x) => normaliseDecision(x.outcome) === 'promoted').length;
  head.appendChild(el('div', { class: 'd-panel d-row', style: 'margin-top:12px;' }, [
    stat(String(board.length), 'board entries'),
    stat(String(experiments.length), 'experiments'),
    stat(String(promoted), 'promoted'),
    stat(svg.fmtSigned(ds.champion_spine), 'Δ spine'),
    stat(ep.closed ? 'closed' : 'open', 'state'),
  ]));

  // ---- proposer brief (collapsible, readable) ----
  const briefText = ep.brief || '';
  const briefDetails = el('details', { class: 'd-brief', open: briefText.length < 1200 ? '' : null });
  briefDetails.appendChild(el('summary', null, [
    el('span', { class: 'chev', text: '▸' }),
    'Proposer brief',
    el('span', { class: 'd-faint', style: 'font-weight:400;font-size:11px;',
      text: briefText ? `· ${briefText.split(/\n/).length} lines` : '· none' }),
  ]));
  briefDetails.appendChild(renderMarkdown(briefText));
  body.appendChild(section('Operator’s brief to the proposer', briefDetails));

  // ---- lineage as a non-colliding bumps chart ----
  const [traj, perEntries] = await Promise.all([
    D.scoreTrajectory(),
    Promise.all(experiments.map((x) => D.perEntry(epochId, x.generation_id))),
  ]);
  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) {
    for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  }
  const nodes = experiments.map((x, i) => ({
    id: x.generation_id,
    x: i,
    promoted: normaliseDecision(x.outcome) === 'promoted',
    scalar: scalarByGen.get(x.generation_id),
    parent: x.parent_generation_id || null,
  }));
  // v0 seed is the spine origin even without an explicit promotion record.
  if (nodes.length && !nodes.some((n) => n.promoted)) nodes[0].promoted = true;
  const bumpsCard = el('div', { class: 'd-panel' });
  bumpsCard.appendChild(svg.bumps({
    width: 720, height: 190, nodes,
    onClick: (n) => ctx.navigate('experiment', { gen: n.id }),
  }));
  bumpsCard.appendChild(el('div', { class: 'd-legend' }, [
    el('span', null, [el('i', { class: 'spine' }), 'champion spine (promoted lineage)']),
    el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'rejected challenger']),
    el('span', { class: 'd-faint', text: 'click a node → its experiment' }),
  ]));
  body.appendChild(section('Lineage', bumpsCard));

  // ---- board entries × generations heatmap (loss profiles) ----
  const entryIds = new Set();
  const lossLookup = new Map(); // `${entry}|${gen}` -> drift_loss
  experiments.forEach((x, i) => {
    const pe = perEntries[i];
    if (pe && Array.isArray(pe.entries)) {
      for (const row of pe.entries) {
        entryIds.add(row.entry_id);
        if (svg.isNum(row.drift_loss)) lossLookup.set(`${row.entry_id}|${x.generation_id}`, row.drift_loss);
      }
    }
  });
  // Seed the row set from the board too, so entries that never ran still show.
  for (const b of board) { if (b && b.entry_id) entryIds.add(b.entry_id); else if (b && b.id) entryIds.add(b.id); }
  const rows = [...entryIds].sort().map((id) => ({ id, label: id }));
  const cols = experiments.map((x) => ({ id: x.generation_id, label: x.generation_id }));
  const hmCard = el('div', { class: 'd-panel', style: 'overflow-x:auto;' });
  if (rows.length && cols.length) {
    hmCard.appendChild(svg.heatmap({
      rows, cols, diverging: false,
      value: (r, c) => lossLookup.has(`${r}|${c}`) ? lossLookup.get(`${r}|${c}`) : null,
      onClick: (rId, cId) => ctx.navigate('experiment', { gen: cId }),
    }));
    hmCard.appendChild(el('p', { class: 'd-faint', style: 'font-size:11px;margin:8px 0 0;',
      text: 'cell = drift loss for one board entry in one generation · darker = more drift · click → experiment' }));
  } else {
    hmCard.appendChild(empty('No per-entry loss profiles yet (the index may not be built).'));
  }
  body.appendChild(section('Board entries × generations · drift loss', hmCard));

  // ---- per-judge × generation trend ----
  const trend = await D.perJudgeTrend(epochId);
  const tCard = el('div', { class: 'd-panel', style: 'overflow-x:auto;' });
  if (trend && Array.isArray(trend.judges) && trend.judges.length && Array.isArray(trend.generations) && trend.generations.length) {
    const jrows = trend.judges.map((j) => ({ id: j.judge_name, label: j.judge_name }));
    const jcols = trend.generations.map((g) => ({ id: g, label: g }));
    const byGen = new Map(trend.judges.map((j) => [j.judge_name, j.by_generation || {}]));
    tCard.appendChild(svg.heatmap({
      rows: jrows, cols: jcols, diverging: false,
      labelWidth: 160,
      value: (r, c) => { const m = byGen.get(r); const v = m && m[c]; return svg.isNum(v) ? v : null; },
    }));
    tCard.appendChild(el('p', { class: 'd-faint', style: 'font-size:11px;margin:8px 0 0;',
      text: 'weighted loss per judge across the champion spine · darker = more loss' }));
  } else {
    tCard.appendChild(empty('No per-judge trend yet (index not built).'));
  }
  body.appendChild(section('Per-judge trend', tCard));
}
