// variants/I/views/paper.js — ACM-STYLE EPOCH PUBLICATION (NEW, signature).
//
// E lacked this; in Ledger it is a PROMINENT first-class tab and the
// variant's signature. We bind the analyzer's markdown
// (/api/epoch/{id}/analysis → analysis_md) and render it as a typeset
// publication via report.js (the same section-marker model the standalone
// report renderer uses): eyebrow / title / metadata masthead / numbered
// body sections / figures. The paper's figures are the dashboard's OWN
// live Tufte charts, embedded inline at FIGURE markers AND, when the
// markdown carries no figure slots, woven in as an explicit "Figures"
// movement so the publication always shows live evidence.
//
// (/api/epoch/{e}/analysis/html may 404 — we use analysis_md, never the
// iframe.) Honest states throughout: not-built / unavailable degrade to a
// clear note, never a blank page.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, empty, pageHead, figure } from '../ui.js';
import { parseAnalysis, renderPaper } from '../report.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'd-empty', text: 'Typesetting the publication…' }));

  const ep = await D.epoch();
  const epochId = (params && params.epochId)
    || (ep && ep.epoch_id)
    || (state.epochDef && state.epochDef.epoch_id) || null;
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [pageHead('Publication', 'Publication', ''), empty('No epoch selected — open an epoch first.')]);
    return;
  }

  // Live figure ingredients (the dashboard's own Tufte charts).
  const [analysisResp, lin, traj, bracket] = await Promise.all([
    D.analysis(epochId), D.lineage(), D.scoreTrajectory(), D.bracket(),
  ]);
  const md = analysisResp && typeof analysisResp.analysis_md === 'string' ? analysisResp.analysis_md : '';

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  const gens = (lin && Array.isArray(lin.generations)) ? lin.generations : [];

  // Pull a matchup grid for the first decided round (the inline slopegraph).
  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups : [];
  const firstM = matchups[0] || null;
  const grid = firstM && firstM.champion && firstM.challenger
    ? await D.matchupGrid(epochId, firstM.champion, firstM.challenger) : null;

  const digest = JSON.stringify({
    epochId, mdLen: md.length,
    gens: gens.map((g) => [g.generation_id, g.parent_generation_id, !!g.promoted, scalarByGen.has(g.generation_id) ? scalarByGen.get(g.generation_id).toFixed(3) : null]),
    m: firstM ? [firstM.champion, firstM.challenger] : null,
    grid: grid && Array.isArray(grid.entry_grid) ? grid.entry_grid.map((r) => [r.entry_id, r.parent_drift_loss, r.child_drift_loss, r.verdict]) : null,
  });

  // The live-figure factory: build the dashboard's Tufte figures on demand.
  const lineageFig = () => {
    const nodes = gens.map((g, i) => ({
      id: g.generation_id, x: i, promoted: !!g.promoted,
      scalar: scalarByGen.get(g.generation_id), parent: g.parent_generation_id || null,
    }));
    if (nodes.length && !nodes.some((n) => n.promoted)) nodes[0].promoted = true;
    return svg.bumps({ width: 680, height: 180, nodes, onClick: (n) => ctx.navigate('candidate', { gen: n.id }) });
  };
  const matchupFig = () => {
    const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
    const series = rows
      .filter((r) => svg.isNum(r.parent_drift_loss) || svg.isNum(r.child_drift_loss))
      .map((r) => ({ label: r.entry_id, id: r.entry_id, a: svg.isNum(r.parent_drift_loss) ? r.parent_drift_loss : NaN, b: svg.isNum(r.child_drift_loss) ? r.child_drift_loss : NaN, verdict: r.verdict }));
    if (!series.length) return null;
    return svg.pairedSlopegraph({
      width: 520, height: Math.max(220, 50 + series.length * 26),
      left: { title: 'champion ' + firstM.champion }, right: { title: 'challenger ' + firstM.challenger },
      labelGap: 150, goodDirection: 'down', series,
      onClick: (s) => ctx.navigate('run', { gen: firstM.challenger, entry: s.id }),
    });
  };
  const driftFig = () => {
    const cols = gens.map((g) => ({ id: g.generation_id, label: g.generation_id }));
    const trendVals = gens.map((g) => scalarByGen.get(g.generation_id)).filter(svg.isNum);
    if (trendVals.length < 2) return null;
    return svg.sparkline({ width: 520, height: 90, values: trendVals, band: true, goodDirection: 'down' });
  };

  // Map FIGURE marker ids → a live figure. The analyzer uses ids like
  // "lineage" / "trajectory" / "matchup"; we match loosely and fall back
  // to the lineage bumps so any figure slot shows live evidence.
  const figureFor = (id) => {
    const key = String(id || '').toLowerCase();
    if (key.includes('matchup') || key.includes('slope') || key.includes('duel')) return matchupFig() || lineageFig();
    if (key.includes('drift') || key.includes('traject') || key.includes('scalar') || key.includes('loss')) return driftFig() || lineageFig();
    return lineageFig();
  };

  gatedSwap(host, digest, () => {
    const nodes = [];
    // Prominent tab head + the action rail (back to the epoch).
    nodes.push(el('div', { class: 'i-paper-pagehead' }, [
      pageHead('Epoch publication', 'The ACM-style epoch paper',
        'The analyzer’s write-up of this epoch, typeset as a publication, with the dashboard’s own live Tufte figures embedded as its figures.'),
      el('div', { class: 'i-paper-actions' }, [
        el('a', { class: 'i-btn i-btn-ghost', href: ctx.href('epoch', { epochId }), text: '← Back to epoch' }),
      ]),
    ]));

    if (!md || !md.trim()) {
      nodes.push(el('div', { class: 'd-panel i-paper-notyet' }, [
        el('p', { class: 'i-lede', text: 'Analysis report not built yet for ' + epochId + '.' }),
        el('p', { class: 'd-faint', text: 'It is generated by the analyzer — run `zicato epoch analyze --epoch ' + epochId + '` to build it. The live figures below preview what the paper will carry.' }),
      ]));
      // Even without a paper, show the live figures so the tab is never blank.
      const lf = lineageFig();
      nodes.push(el('div', { class: 'd-panel' }, [figure(lf, 'Lineage — the champion spine versus rejected challengers.', { label: 'Figure 1.' })]));
      const mf = matchupFig();
      if (mf) nodes.push(el('div', { class: 'd-panel' }, [figure(mf, 'A paired per-board duel from the first decided round.', { label: 'Figure 2.' })]));
      return nodes;
    }

    const doc = parseAnalysis(md);
    const paper = renderPaper(doc, { figureFor });

    // If the markdown carried NO figure slots, weave a "Figures" movement
    // of live Tufte charts in so the publication always shows live evidence.
    const hadFigures = doc.blocks.some((b) => b.kind === 'figure');
    if (!hadFigures) {
      const movement = el('section', { class: 'i-paper-figures' }, [
        el('h2', { class: 'i-paper-h i-paper-h2' }, [el('span', { class: 'i-paper-secnum', text: '★ ' }), 'Figures']),
      ]);
      const lf = lineageFig();
      movement.appendChild(el('figure', { class: 'i-paper-figure' }, [lf,
        el('figcaption', { class: 'i-paper-figcap' }, [el('span', { class: 'i-paper-figcap-label', text: 'Figure 1. ' }), 'Lineage bumps — the champion spine versus rejected challengers.'])]));
      const mf = matchupFig();
      if (mf) movement.appendChild(el('figure', { class: 'i-paper-figure' }, [mf,
        el('figcaption', { class: 'i-paper-figcap' }, [el('span', { class: 'i-paper-figcap-label', text: 'Figure 2. ' }), 'Paired per-board duel for the first decided round.'])]));
      const df = driftFig();
      if (df) movement.appendChild(el('figure', { class: 'i-paper-figure' }, [df,
        el('figcaption', { class: 'i-paper-figcap' }, [el('span', { class: 'i-paper-figcap-label', text: 'Figure 3. ' }), 'Best scalar per generation; lower is better.'])]));
      paper.appendChild(movement);
    }

    nodes.push(el('div', { class: 'd-panel i-paper-sheet' }, [paper]));
    return nodes;
  });
}
