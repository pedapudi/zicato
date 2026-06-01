// variants/M/views/paper.js — ACM-STYLE EPOCH PUBLICATION (the signature).
//
// CONVERGENCE-II FIX #3 — the publication REUSES K's renderer (judged the
// best of the round): K's `paper.js` parses the masthead + sections, and K's
// `renderMarkdown` renders GFM — crucially **tables** (Variant I's "Aggregate
// generation scores" table rendered as raw `| … |`; reusing K's renderer
// fixes that). On top of K's renderer M:
//   * COMBINES the aggregate-generation-scores TABLE + its summary BAR CHART
//     into ONE cohesive figure (not two redundant blocks);
//   * adds per-matchup DETAIL (champion vs challenger per-board, from
//     /api/matchup-grid/…) as a live paired slopegraph figure;
//   * embeds the dashboard's own live Tufte figures inline at the markdown's
//     FIGURE markers, and weaves a Figures movement when there are none.
//
// (/api/epoch/{e}/analysis/html may 404 — we use analysis_md.) Honest states
// throughout: not-built / unavailable degrade to a clear note.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, empty, pageHead } from '../ui.js';
// REUSE K's publication renderer + chrome (the brief's mandate).
import { parsePaper, renderPaper } from '../../K/paper.js';
import { figureFrame } from '../../K/ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'd-empty', text: 'Typesetting the publication…' }));

  const ep = await D.epoch();
  const epochId = (params && params.epochId) || (ep && ep.epoch_id)
    || (state.epochDef && state.epochDef.epoch_id) || null;
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [pageHead('Publication', 'Publication', ''), empty('No epoch selected — open an epoch first.')]);
    return;
  }

  const [analysisResp, lin, traj, bracket] = await Promise.all([
    D.analysis(epochId), D.lineage(), D.scoreTrajectory(), D.bracket(),
  ]);
  const md = analysisResp && typeof analysisResp.analysis_md === 'string' ? analysisResp.analysis_md : '';
  const broken = analysisResp === null;
  const missing = !broken && !md.trim();

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  const gens = (lin && Array.isArray(lin.generations)) ? lin.generations.map((g, i) => ({
    id: g.generation_id, x: i, promoted: !!g.promoted, parent: g.parent_generation_id || null,
    scalar: scalarByGen.get(g.generation_id),
  })) : [];

  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups : [];
  const firstM = matchups[0] || null;
  const grid = firstM && firstM.champion && firstM.challenger
    ? await D.matchupGrid(epochId, firstM.champion, firstM.challenger) : null;

  const digest = JSON.stringify({
    epochId, broken, missing, mdLen: md.length,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, svg.isNum(g.scalar) ? g.scalar.toFixed(3) : null]),
    m: firstM ? [firstM.champion, firstM.challenger, firstM.decision, firstM.delta_scalar] : null,
    grid: grid && Array.isArray(grid.entry_grid) ? grid.entry_grid.map((r) => [r.entry_id, r.parent_drift_loss, r.child_drift_loss, r.verdict]) : null,
  });

  // ---- live figure factories ----------------------------------------
  const nav = ctx.navigate;
  const lineageFig = (n) => figureFrame({
    number: n,
    mark: svg.bumps({ width: 700, height: 190, nodes: gens, onClick: (node) => nav('candidate', { gen: node.id }) }),
    caption: 'Lineage as ranked lanes — the champion spine versus rejected challengers. ',
    openLabel: 'open candidates →', openHref: '#/M/candidate',
    onOpen: () => nav('candidate', { gen: (gens[0] && gens[0].id) || null }),
  });

  // The COMBINED aggregate-generation-scores visual: one figure that pairs a
  // per-generation scalar BAR CHART with the SAME numbers as a table (not two
  // redundant blocks).
  const aggregateScoresFig = (n) => {
    const items = gens.filter((g) => svg.isNum(g.scalar))
      .map((g) => ({ label: g.id + (g.promoted ? ' ♛' : ''), value: g.scalar }));
    const mark = el('div', { class: 'm-aggscore' });
    if (items.length) {
      mark.appendChild(svg.valueBars({ width: 520, rowHeight: 22, labelWidth: 120, items }));
    }
    const table = el('table', { class: 'm-aggscore-table' });
    table.appendChild(el('thead', null, [el('tr', null, [
      el('th', { text: 'generation' }), el('th', { text: 'scalar (loss)' }),
      el('th', { text: 'role' }),
    ])]));
    const tbody = el('tbody');
    for (const g of gens) {
      tbody.appendChild(el('tr', null, [
        el('td', { class: 'd-mono', text: g.id }),
        el('td', { class: 'd-mono', text: svg.isNum(g.scalar) ? svg.fmt(g.scalar, 2) : '—' }),
        el('td', { text: g.promoted ? 'champion' : 'challenger' }),
      ]));
    }
    table.appendChild(tbody);
    mark.appendChild(table);
    return figureFrame({
      number: n, mark,
      caption: 'Aggregate generation scores — the per-generation scalar (loss; lower is better) as one cohesive visual: the bar chart and the table read off the same numbers. ',
      openLabel: 'open candidates →', openHref: '#/M/candidate',
      onOpen: () => nav('candidate', { gen: (gens[0] && gens[0].id) || null }),
    });
  };

  // Per-matchup detail: the decisive round's paired per-board duel.
  const matchupFig = (n) => {
    const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
    const series = rows
      .filter((r) => svg.isNum(r.parent_drift_loss) || svg.isNum(r.child_drift_loss))
      .map((r) => ({ label: r.entry_id, id: r.entry_id, a: svg.isNum(r.parent_drift_loss) ? r.parent_drift_loss : NaN, b: svg.isNum(r.child_drift_loss) ? r.child_drift_loss : NaN, verdict: r.verdict }));
    if (!series.length || !firstM) return null;
    return figureFrame({
      number: n,
      mark: svg.pairedSlopegraph({
        width: 540, height: Math.max(220, 50 + series.length * 26),
        left: { title: 'champion ' + firstM.champion }, right: { title: 'challenger ' + firstM.challenger },
        labelGap: 150, goodDirection: 'down', series,
        onClick: (s) => nav('run', { gen: firstM.challenger, entry: s.id }),
      }),
      caption: `Per-matchup detail — ${firstM.champion} vs ${firstM.challenger}, paired per board entry; slope down = the challenger improved. `,
      openLabel: 'open match-up →', openHref: '#/M/matchups',
      onOpen: () => nav('matchups', {}),
    });
  };

  const driftFig = (n) => {
    const trendVals = gens.map((g) => g.scalar).filter(svg.isNum);
    if (trendVals.length < 2) return null;
    return figureFrame({
      number: n,
      mark: svg.sparkline({ width: 520, height: 90, values: trendVals, band: true, goodDirection: 'down' }),
      caption: 'Best scalar per generation across the epoch; lower is better. ',
    });
  };

  // Map FIGURE marker ids → a live figure (loose match).
  const figures = {};
  const bindFig = (id, fac) => { figures[id] = fac; };
  for (const id of ['lineage', 'lineage-bumps']) bindFig(id, lineageFig);
  for (const id of ['aggregate-generation-scores', 'aggregate-scores', 'scores', 'summary']) bindFig(id, aggregateScoresFig);
  for (const id of ['matchup', 'matchup-detail', 'duel', 'slopegraph', 'hypothesis-vs-outcome']) bindFig(id, (n) => matchupFig(n) || lineageFig(n));
  for (const id of ['drift', 'trajectory', 'score-trajectory', 'loss']) bindFig(id, (n) => driftFig(n) || lineageFig(n));

  const canonicalFigures = [
    lineageFig,
    aggregateScoresFig,
    (n) => matchupFig(n) || lineageFig(n),
    (n) => driftFig(n) || lineageFig(n),
  ];

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'm-paper-pagehead' }, [
      pageHead('Epoch publication', 'The ACM-style epoch paper',
        'The analyzer’s write-up of this epoch, typeset as a publication via the round’s best renderer, with the dashboard’s own live Tufte figures embedded — including the combined aggregate-generation-scores visual and the per-matchup duel.'),
      el('div', { class: 'm-paper-actions' }, [
        el('a', { class: 'm-btn m-btn-ghost', href: ctx.href('epoch', { epochId }), text: '← Back to epoch' }),
      ]),
    ]));

    const paper = parsePaper(md);
    const sheet = renderPaper({
      epochId, paper, figures, canonicalFigures, broken, missing,
    });
    // K's renderer is scoped to [data-variant="K"]; M re-skins it with its
    // own paper styles by tagging the article — so the publication reads in
    // M's editorial voice + colour/typeface themes.
    sheet.classList.add('m-paper');
    nodes.push(el('div', { class: 'd-panel m-paper-sheet' }, [sheet]));
    return nodes;
  });
}
