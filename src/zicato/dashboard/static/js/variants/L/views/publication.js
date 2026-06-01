// variants/L/views/publication.js — the ACM-style epoch publication, as a TAB.
//
// K's paper renderer (judged best of the round) reused via L's paper.js. The
// publication is a TAB here (dashboard-first), not the home. Fixes carried in
// (fix #3): GFM tables RENDER (the renderMarkdown table path), and the
// aggregate-generation-scores TABLE + its summary BAR CHART are COMBINED into
// ONE cohesive figure (not two redundant blocks). Per-matchup detail
// (champion vs challenger per board) is added from /api/matchup-grid.
//
// Data: /api/epoch/{e}/analysis (analysis_md), /api/lineage, /api/tournaments,
// /api/score-trajectory, /api/generation/{e}/{g}/per-entry,
// /api/matchup-grid/{e}/{champ}/{chall}, /api/mutations.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, loading, figureFrame } from '../ui.js';
import { parsePaper, renderPaper } from '../paper.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(loading('Typesetting the epoch report…'));

  const ep = await D.epoch();
  const epochId = (params && params.epochId) || (ep && ep.epoch_id) || (state.epochDef && state.epochDef.epoch_id) || null;
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [el('div', { class: 'vl-paper' }, [
      el('div', { class: 'vl-statebox vl-notyet' }, [
        el('p', { class: 'vl-statebox-h', text: 'No epoch selected.' }),
        el('p', { class: 'vl-faint', text: 'Open or start an epoch, then its report becomes available here.' }),
      ]),
    ])]);
    return;
  }

  const [analysis, lineage, tours, traj] = await Promise.all([
    D.analysis(epochId), D.lineage(epochId), D.bracket(), D.scoreTrajectory(),
  ]);
  const md = analysis && typeof analysis.analysis_md === 'string' ? analysis.analysis_md : '';
  const broken = analysis === null;
  const missing = !broken && !md.trim();
  const paper = parsePaper(md);

  const gens = (lineage && Array.isArray(lineage.generations)) ? lineage.generations : [];
  const ordered = orderGenerations(gens);
  const scalarById = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarById.set(p.generation_id, p.scalar);

  const perEntries = await Promise.all(ordered.map((g) => D.perEntry(epochId, g.generation_id)));
  const peByGen = new Map(ordered.map((g, i) => [g.generation_id, perEntries[i]]));

  // Per-matchup grids for the per-matchup detail figure.
  const matchups = (tours && Array.isArray(tours.matchups)) ? tours.matchups : [];
  const grids = await Promise.all(matchups.map((m) => (m.champion && m.challenger) ? D.matchupGrid(epochId, m.champion, m.challenger) : Promise.resolve(null)));

  const nav = ctx.navigate;
  const figures = {
    lineage: (n) => figLineage(n, ordered, scalarById, nav),
    'per-board-heatmap': (n) => figHeatmap(n, ordered, peByGen, nav),
    'mutation-surface': (n) => figMutation(n, nav, ordered),
    'mutation-impact-matrix': (n) => figMutation(n, nav, ordered),
    'score-trajectory': (n) => figSankey(n, ordered, peByGen, nav),
    'aggregate-generation-scores': (n) => figAggregateScores(n, ordered, scalarById, peByGen),
    'aggregate-scores': (n) => figAggregateScores(n, ordered, scalarById, peByGen),
    'hypothesis-vs-outcome': (n) => figMatchup(n, matchups, grids, nav),
    'matchup-detail': (n) => figMatchup(n, matchups, grids, nav),
  };
  const canonical = [
    (n) => figLineage(n, ordered, scalarById, nav),
    (n) => figAggregateScores(n, ordered, scalarById, peByGen),
    (n) => figSankey(n, ordered, peByGen, nav),
    (n) => figMatchup(n, matchups, grids, nav),
    (n) => figHeatmap(n, ordered, peByGen, nav),
  ];

  const digest = JSON.stringify({
    epochId, broken, missing, title: paper.title, eyebrow: paper.eyebrow,
    meta: paper.meta.map((m) => [m.label, m.value]),
    bodyLen: paper.body.length,
    gens: ordered.map((g) => [g.generation_id, g.parent_generation_id, g.promoted]),
    scalars: ordered.map((g) => scalarById.get(g.generation_id) ?? null),
    matchups: matchups.map((m, i) => [m.champion, m.challenger, m.decision, grids[i] && Array.isArray(grids[i].entry_grid) ? grids[i].entry_grid.length : 0]),
  });

  gatedSwap(host, digest, () => [renderPaper({ epochId, paper, figures, canonicalFigures: canonical, broken, missing })]);
}

// ---- figure builders -----------------------------------------------

function figLineage(n, ordered, scalarById, nav) {
  const nodes = ordered.map((g) => ({ id: g.generation_id, x: lineageX(g, ordered), promoted: !!g.promoted, parent: g.parent_generation_id || null, scalar: scalarById.get(g.generation_id) }));
  return figureFrame({
    number: n, mark: svg.bumps({ width: 720, height: 200, nodes, onClick: (node) => nav('candidate', { gen: node.id }) }),
    caption: 'Lineage as ranked lanes — the champion spine, challengers branching off. ',
    openLabel: 'open candidates →', onOpen: () => nav('candidate', { gen: (ordered[0] && ordered[0].generation_id) || null }),
  });
}

// COMBINED aggregate-generation-scores: the TABLE and its summary BAR CHART
// in ONE cohesive figure (fix #3) — not two redundant blocks.
function figAggregateScores(n, ordered, scalarById, peByGen) {
  const rows = ordered.map((g) => {
    const pe = peByGen.get(g.generation_id);
    const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];
    const losses = entries.map((e) => e.drift_loss).filter(svg.isNum);
    const agg = losses.length ? losses.reduce((a, b) => a + b, 0) : null;
    const passes = entries.filter((e) => e.pass_fail === 1).length;
    const scored = entries.filter((e) => e.pass_fail === 0 || e.pass_fail === 1).length;
    return {
      gen: g.generation_id, promoted: !!g.promoted,
      scalar: scalarById.has(g.generation_id) ? scalarById.get(g.generation_id) : agg,
      agg, passRate: scored ? passes / scored : null,
    };
  });
  const wrap = el('div', { class: 'vl-aggfig' });
  // the table
  const tbl = el('table', { class: 'vl-md-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'generation' }), el('th', { class: 'vl-num', text: 'scalar (loss)' }),
    el('th', { class: 'vl-num', text: 'Σ board loss' }), el('th', { class: 'vl-num', text: 'pass-rate' }), el('th', { text: 'role' }),
  ])]));
  const tb = el('tbody');
  for (const r of rows) {
    tb.appendChild(el('tr', { class: r.promoted ? 'vl-is-champ' : '' }, [
      el('td', { class: 'vl-mono', text: r.gen }),
      el('td', { class: 'vl-num', text: svg.isNum(r.scalar) ? svg.fmt(r.scalar, 1) : '—' }),
      el('td', { class: 'vl-num', text: svg.isNum(r.agg) ? svg.fmt(r.agg, 1) : '—' }),
      el('td', { class: 'vl-num', text: svg.isNum(r.passRate) ? (r.passRate * 100).toFixed(0) + '%' : '—' }),
      el('td', null, [el('span', { class: 'vl-pill vl-' + (r.promoted ? 'promoted' : 'rejected'), text: r.promoted ? 'champion' : 'challenger' })]),
    ]));
  }
  tbl.appendChild(tb);
  wrap.appendChild(tbl);
  // the summary bar chart — same data, one cohesive figure
  const items = rows.filter((r) => svg.isNum(r.scalar)).map((r) => ({ label: r.gen, value: r.scalar, cls: r.promoted ? 'vl-good' : '' }));
  if (items.length) wrap.appendChild(svg.valueBars({ width: 560, rowHeight: 24, labelWidth: 90, items }));
  return figureFrame({ number: n, mark: wrap, caption: 'Aggregate generation scores — the table and its summary bar chart, one figure. Lower scalar is better. ' });
}

function figSankey(n, ordered, peByGen, nav) {
  const champ = ordered.find((g) => g.promoted) || ordered[0];
  const pe = champ ? peByGen.get(champ.generation_id) : null;
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];
  const boards = entries.filter((e) => svg.isNum(e.drift_loss)).map((e) => ({ id: e.entry_id, label: e.entry_id, value: e.drift_loss, ref: e.entry_id, cls: e.wall_clock_budget_exceeded ? 'vl-bad' : (e.pass_fail === 1 ? 'vl-good' : '') }));
  const agg = boards.reduce((a, b) => a + b.value, 0);
  return figureFrame({
    number: n, mark: svg.sankey({ width: 760, candidate: { label: champ ? champ.generation_id : 'candidate', sub: 'patch on mutation sites' }, boards, aggregate: { label: 'scalar', sub: svg.fmt(agg, 1) + ' loss' }, onBoard: (entryId) => nav('run', { gen: champ ? champ.generation_id : null, entry: entryId }) }),
    caption: `Causal flow for ${champ ? champ.generation_id : 'the champion'} — per-board loss summing to the aggregate scalar. `,
    openLabel: 'open candidate →', onOpen: () => nav('candidate', { gen: champ ? champ.generation_id : null }),
  });
}

// Per-matchup detail: champion vs challenger per board (a paired slopegraph).
function figMatchup(n, matchups, grids, nav) {
  const idx = matchups.findIndex((m, i) => grids[i] && Array.isArray(grids[i].entry_grid) && grids[i].entry_grid.length);
  const i = idx >= 0 ? idx : 0;
  const m = matchups[i];
  const grid = grids[i];
  const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
  const series = rows.filter((r) => svg.isNum(r.parent_drift_loss) || svg.isNum(r.child_drift_loss))
    .map((r) => ({ label: r.entry_id, id: r.entry_id, a: svg.isNum(r.parent_drift_loss) ? r.parent_drift_loss : NaN, b: svg.isNum(r.child_drift_loss) ? r.child_drift_loss : NaN, verdict: r.verdict }));
  const mark = svg.pairedSlopegraph({
    width: 580, height: Math.max(260, 60 + series.length * 26), series,
    left: { title: (m && m.champion) || 'champion' }, right: { title: (m && m.challenger) || 'challenger' },
    onClick: (s) => nav('run', { gen: m && m.challenger, entry: s.id }),
  });
  return figureFrame({
    number: n, mark,
    caption: `Per-matchup detail — ${(m && m.champion) || 'champion'} vs ${(m && m.challenger) || 'challenger'}, paired per board entry. Slope down = the challenger improved. `,
    openLabel: 'open match-ups →', onOpen: () => nav('matchups', {}),
  });
}

function figHeatmap(n, ordered, peByGen, nav) {
  const cols = ordered.map((g) => ({ id: g.generation_id, label: g.generation_id }));
  const entryIds = []; const seen = new Set();
  for (const g of ordered) {
    const pe = peByGen.get(g.generation_id);
    if (pe && Array.isArray(pe.entries)) for (const e of pe.entries) if (!seen.has(e.entry_id)) { seen.add(e.entry_id); entryIds.push(e.entry_id); }
  }
  const rows = entryIds.map((id) => ({ id, label: id }));
  const lookup = new Map();
  for (const g of ordered) {
    const pe = peByGen.get(g.generation_id);
    if (pe && Array.isArray(pe.entries)) for (const e of pe.entries) lookup.set(g.generation_id + ' ' + e.entry_id, e.drift_loss);
  }
  // The publication's figures are read in whatever theme is active; the ramp
  // is resolved by the heatmap default (theme tokens drive it on the page).
  return figureFrame({
    number: n, mark: svg.heatmap({ rows, cols, cellW: 44, cellH: 20, labelWidth: 170, value: (rowId, colId) => { const v = lookup.get(colId + ' ' + rowId); return svg.isNum(v) ? v : null; }, onClick: (rowId) => nav('board', { entry: rowId }) }),
    caption: 'Board entry × generation drift loss — theme-aware ramp. Click a cell → that board entry across candidates. ',
    openLabel: 'open board →', onOpen: () => nav('board', { entry: (rows[0] && rows[0].id) || null }),
  });
}

function figMutation(n, nav, ordered) {
  const mark = el('div', { class: 'vl-mut-preview' }, [
    el('p', { class: 'vl-faint', text: 'Which mutation sites each generation patched — with baseline-vs-challenger diffs.' }),
    el('div', { class: 'vl-mut-chips' }, ordered.map((g) => el('span', { class: 'vl-chip' + (g.promoted ? ' vl-chip-live' : ''), text: g.generation_id }))),
  ]);
  return figureFrame({ number: n, mark, caption: 'Methods — the mutation surface: mutation-site × generation. ', openLabel: 'open mutations →', onOpen: () => nav('mutations', {}) });
}

// ---- helpers --------------------------------------------------------

function orderGenerations(gens) {
  const byId = new Map(gens.map((g) => [g.generation_id, g]));
  const seen = new Set(); const out = [];
  const visit = (g) => {
    if (!g || seen.has(g.generation_id)) return;
    if (g.parent_generation_id && byId.has(g.parent_generation_id)) visit(byId.get(g.parent_generation_id));
    if (seen.has(g.generation_id)) return;
    seen.add(g.generation_id); out.push(g);
  };
  for (const g of gens) visit(g);
  return out;
}

function lineageX(g, ordered) {
  const byId = new Map(ordered.map((n) => [n.generation_id, n]));
  let depth = 0; let cur = g;
  while (cur && cur.parent_generation_id && byId.has(cur.parent_generation_id)) { depth += 1; cur = byId.get(cur.parent_generation_id); }
  return depth;
}
