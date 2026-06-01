// variants/N/views/publication.js — ACM-style epoch publication, as a TAB.
//
// The operator REJECTED K's paper-FIRST metaphor but judged K's publication
// RENDERER the best of all variants — so N reuses K's approach (parse the
// section markers; typeset eyebrow / title / meta / abstract / body; splice
// live Tufte figures at <!-- FIGURE:NAME --> markers) but as a TAB, not the
// home (fix #3). GFM **tables render** (N's ui.renderMarkdown). The aggregate
// generation-scores TABLE and its summary BAR CHART are COMBINED into ONE
// cohesive visual; per-matchup detail (champion vs challenger per board) is
// appended from the matchup grid.
//
// Bind: /api/epoch/{epoch_id}/analysis → { analysis_md }. Cold deep-link safe.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, empty, subhead, renderMarkdown } from '../ui.js';

// Parse analysis_md into { eyebrow, title, meta:[{label,value}], abstract,
// body } — the same marker scheme K uses.
export function parsePaper(md) {
  const text = String(md || '').replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  const out = { eyebrow: '', title: '', meta: [], abstract: '', body: '' };
  let i = 0; const bodyLines = []; let inBody = false;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();
    if (!inBody) {
      if (/^<!--\s*EYEBROW\s*-->$/.test(trimmed)) {
        i++; while (i < lines.length && lines[i].trim() === '') i++;
        out.eyebrow = (lines[i] || '').trim(); i++; continue;
      }
      const h1 = /^#\s+(.*)$/.exec(line);
      if (h1 && !out.title) { out.title = h1[1].trim(); i++; continue; }
      if (/^<!--\s*META\s*-->$/.test(trimmed)) {
        i++; const buf = [];
        while (i < lines.length && lines[i].trim() !== '') { buf.push(lines[i].trim()); i++; }
        out.meta = buf.join('\n').split(/\n|\s{2,}/).map((s) => parseMetaPair(s)).filter(Boolean);
        continue;
      }
      const h2 = /^##\s+(.*)$/.exec(line);
      if (h2) {
        inBody = true;
        if (/abstract/i.test(h2[1])) {
          const buf = []; i++;
          while (i < lines.length && !/^##\s+/.test(lines[i])) { buf.push(lines[i]); i++; }
          out.abstract = buf.join('\n').trim(); continue;
        }
        bodyLines.push(line); i++; continue;
      }
      bodyLines.push(line); i++; continue;
    }
    bodyLines.push(line); i++;
  }
  out.body = bodyLines.join('\n').trim();
  return out;
}
function parseMetaPair(s) {
  const str = String(s || '').trim();
  if (!str) return null;
  const m = /^\*\*([^*]+)\*\*\s*:?\s*(.*)$/.exec(str);
  if (m) return { label: m[1].trim().replace(/:$/, ''), value: stripTicks(m[2].trim()) };
  return { label: '', value: stripTicks(str) };
}
function stripTicks(s) { return String(s || '').replace(/`/g, ''); }

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dn-empty', text: 'Reading epoch publication…' }));

  let epochId = (params && params.epochId) || null;
  if (!epochId) {
    const ep = await D.epoch();
    epochId = (ep && ep.epoch_id) || (state.epochDef && state.epochDef.epoch_id) || null;
  }
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Epoch publication' }), empty('No epoch selected.')]);
    return;
  }

  const [analysis, lin, traj, bracket] = await Promise.all([
    D.analysis(epochId), D.lineage(), D.scoreTrajectory(), D.bracket(),
  ]);
  const md = (analysis && typeof analysis.analysis_md === 'string') ? analysis.analysis_md : '';

  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted })) : [];
  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups : [];

  // grids for EVERY decided matchup (per-matchup detail).
  const grids = await Promise.all(matchups.map((m) => (m.champion && m.challenger)
    ? D.matchupGrid(epochId, m.champion, m.challenger) : Promise.resolve(null)));

  const figures = { gens, scalarByGen, matchups, grids, ctx, epochId };

  const digest = JSON.stringify({
    epochId, mdLen: md.length,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    grids: grids.map((gr) => gr && Array.isArray(gr.entry_grid) ? gr.entry_grid.map((r) => [r.entry_id, r.parent_drift_loss, r.child_drift_loss, r.verdict]) : null),
  });

  gatedSwap(host, digest, () => {
    const paper = parsePaper(md);
    const article = el('article', { class: 'dn-paper' });

    // masthead
    const masthead = el('header', { class: 'dn-paper-masthead' });
    if (paper.eyebrow) masthead.appendChild(el('div', { class: 'dn-paper-eyebrow', text: paper.eyebrow }));
    masthead.appendChild(el('h1', { class: 'dn-paper-title', text: paper.title || `Epoch ${epochId}` }));
    masthead.appendChild(el('div', { class: 'dn-paper-rule' }));
    if (paper.meta.length) {
      masthead.appendChild(el('div', { class: 'dn-paper-metagrid' }, paper.meta.map((m) => el('div', { class: 'dn-paper-meta-cell' }, [
        m.label ? el('span', { class: 'dn-paper-meta-label', text: m.label }) : null,
        el('span', { class: 'dn-paper-meta-value', text: m.value }),
      ].filter(Boolean)))));
    }
    article.appendChild(masthead);

    if (!md.trim()) {
      article.appendChild(el('div', { class: 'dn-paper-statebox' }, [
        el('p', { class: 'dn-paper-statebox-h', text: 'The narrative report has not been written yet for ' + epochId + '.' }),
        el('p', { class: 'dn-faint' }, [
          'Run ', el('code', { class: 'dn-paper-code', text: 'zicato epoch analyze --epoch ' + epochId }),
          ' to build it. The live figures below are drawn from the run data regardless.',
        ]),
      ]));
      appendCanonicalFigures(article, figures);
      return [article];
    }

    if (paper.abstract) {
      article.appendChild(el('section', { class: 'dn-paper-abstract' }, [
        el('div', { class: 'dn-paper-abstract-label', text: 'Abstract' }),
        renderMarkdown(paper.abstract),
      ]));
    }

    // the body — GFM tables render; figure markers splice live figures.
    let figuresUsed = 0;
    const body = renderMarkdown(paper.body, {
      onFigure: (name) => { const node = figureFor(name, figures); if (node) figuresUsed += 1; return node; },
    });
    article.appendChild(el('div', { class: 'dn-paper-body' }, [body]));
    if (figuresUsed === 0) appendCanonicalFigures(article, figures);

    // per-matchup detail (always appended — the brief mandates it in the paper).
    appendMatchupDetail(article, figures);
    return [article];
  });
}

// A live Tufte figure for a FIGURE marker. The "scores" / "aggregate" figure
// COMBINES the aggregate-generation-scores TABLE and a summary BAR CHART into
// ONE cohesive visual (fix #3).
function figureFor(name, figures) {
  const key = String(name).toLowerCase();
  const { gens, scalarByGen, matchups, grids, ctx, epochId } = figures;

  if (key.includes('score') || key.includes('aggregate') || key.includes('summary')) {
    return aggregateScoresFigure(gens, scalarByGen);
  }
  if ((key.includes('lineage') || key.includes('bump')) && gens.length) {
    const fig = el('figure', { class: 'dn-paper-fig' });
    const bumpNodes = gens.map((g, i) => ({ id: g.id, x: i, promoted: g.promoted, scalar: scalarByGen.get(g.id), parent: g.parent }));
    if (!bumpNodes.some((n) => n.promoted)) bumpNodes[0].promoted = true;
    fig.appendChild(svg.bumps({ width: 640, height: 160, nodes: bumpNodes, onClick: (n) => ctx.navigate('candidate', { epochId, gen: n.id }) }));
    fig.appendChild(el('figcaption', { class: 'dn-paper-figcap', text: 'Figure · lineage bumps — champion spine vs rejected challengers (click a node → candidate).' }));
    return fig;
  }
  const g0 = grids && grids.find((gr) => gr && Array.isArray(gr.entry_grid));
  const m0 = grids ? matchups[grids.indexOf(g0)] : null;
  if ((key.includes('matchup') || key.includes('slope') || key.includes('duel')) && g0 && m0) {
    const fig = el('figure', { class: 'dn-paper-fig' });
    const series = g0.entry_grid
      .filter((r) => svg.isNum(r.parent_drift_loss) || svg.isNum(r.child_drift_loss))
      .map((r) => ({ label: r.entry_id, id: r.entry_id, a: svg.isNum(r.parent_drift_loss) ? r.parent_drift_loss : NaN, b: svg.isNum(r.child_drift_loss) ? r.child_drift_loss : NaN, verdict: r.verdict }));
    fig.appendChild(svg.pairedSlopegraph({ width: 520, height: Math.max(200, 50 + series.length * 26),
      left: { title: `champion ${m0.champion}` }, right: { title: `challenger ${m0.challenger}` }, labelGap: 150, goodDirection: 'down', series,
      onClick: (s) => ctx.navigate('board', { epochId, entry: s.id, gen: m0.challenger }) }));
    fig.appendChild(el('figcaption', { class: 'dn-paper-figcap', text: `Figure · paired per-board duel ${m0.champion} → ${m0.challenger}.` }));
    return fig;
  }
  if ((key.includes('drift') || key.includes('heatmap')) && g0 && m0) {
    const fig = el('figure', { class: 'dn-paper-fig' });
    const rows = g0.entry_grid.map((r) => ({ id: r.entry_id, label: r.entry_id }));
    const cols = [{ id: 'champion', label: m0.champion }, { id: 'challenger', label: m0.challenger }];
    const lossLookup = new Map();
    for (const r of g0.entry_grid) {
      if (svg.isNum(r.parent_drift_loss)) lossLookup.set(r.entry_id + '|champion', r.parent_drift_loss);
      if (svg.isNum(r.child_drift_loss)) lossLookup.set(r.entry_id + '|challenger', r.child_drift_loss);
    }
    fig.appendChild(svg.heatmap({ rows, cols, value: (rid, cid) => (lossLookup.has(rid + '|' + cid) ? lossLookup.get(rid + '|' + cid) : null) }));
    fig.appendChild(el('figcaption', { class: 'dn-paper-figcap', text: 'Figure · per-board drift loss heatmap (champion vs challenger; theme-aware ink).' }));
    return fig;
  }
  return null;
}

// ONE cohesive visual: the aggregate generation scores TABLE and its summary
// BAR CHART, side by side (not two redundant blocks).
function aggregateScoresFigure(gens, scalarByGen) {
  const fig = el('figure', { class: 'dn-paper-fig dn-scores-fig' });
  const items = gens.map((g) => ({ id: g.id, label: g.id, promoted: g.promoted, value: scalarByGen.get(g.id) }))
    .filter((it) => svg.isNum(it.value));
  if (!items.length) {
    fig.appendChild(el('figcaption', { class: 'dn-paper-figcap dn-faint', text: 'Figure · aggregate generation scores (no trajectory data yet).' }));
    return fig;
  }
  const combined = el('div', { class: 'dn-scores-combined' });
  // the bar chart (one bar per generation, scalar / loss)
  combined.appendChild(svg.valueBars({
    width: 300, rowHeight: 24, labelWidth: 60,
    items: items.map((it) => ({ label: it.label + (it.promoted ? ' ♛' : ''), value: it.value })),
  }));
  // the table, sharing the same data
  const tbl = el('table', { class: 'dn-md-table dn-scores-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [el('th', { text: 'generation' }), el('th', { class: 'dn-num', text: 'scalar (loss)' }), el('th', { text: 'outcome' })])]));
  const tbody = el('tbody');
  for (const it of items) {
    tbody.appendChild(el('tr', null, [
      el('td', { class: 'dn-mono', text: it.label }),
      el('td', { class: 'dn-num dn-mono', text: svg.fmt(it.value, 2) }),
      el('td', { text: it.promoted ? 'promoted ♛' : 'rejected' }),
    ]));
  }
  tbl.appendChild(tbody);
  combined.appendChild(tbl);
  fig.appendChild(combined);
  fig.appendChild(el('figcaption', { class: 'dn-paper-figcap', text: 'Figure · aggregate generation scores — the summary bar chart and its table are one visual (lower scalar is better).' }));
  return fig;
}

function appendCanonicalFigures(article, figures) {
  const plates = el('section', { class: 'dn-paper-plates' }, [
    el('h2', { class: 'dn-paper-h2', text: 'Figures' }),
    el('p', { class: 'dn-faint', text: 'Live, interactive — click any figure to drill into the dashboard.' }),
  ]);
  for (const name of ['aggregate-scores', 'lineage', 'matchup-slope', 'drift-heatmap']) {
    const f = figureFor(name, figures);
    if (f) plates.appendChild(f);
  }
  article.appendChild(plates);
}

// Per-matchup detail — champion vs challenger per board, for EVERY decided
// round (the brief mandates this in the paper).
function appendMatchupDetail(article, figures) {
  const { matchups, grids, ctx, epochId } = figures;
  const sec = el('section', { class: 'dn-paper-matchups' });
  sec.appendChild(el('h2', { class: 'dn-paper-h2', text: 'Per-match-up detail' }));
  let any = false;
  matchups.forEach((m, i) => {
    const grid = grids[i];
    const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
    if (!rows.length) return;
    any = true;
    sec.appendChild(subhead(`${m.champion} → ${m.challenger}${m.decision ? ' · ' + m.decision : ''}`));
    const tbl = el('table', { class: 'dn-md-table' });
    tbl.appendChild(el('thead', null, [el('tr', null, [
      el('th', { text: 'board entry' }), el('th', { class: 'dn-num', text: m.champion }),
      el('th', { class: 'dn-num', text: m.challenger }), el('th', { class: 'dn-num', text: 'Δ' }), el('th', { text: 'verdict' }),
    ])]));
    const tbody = el('tbody');
    for (const r of rows) {
      const d = svg.isNum(r.delta) ? r.delta : (svg.isNum(r.child_drift_loss) && svg.isNum(r.parent_drift_loss) ? r.child_drift_loss - r.parent_drift_loss : NaN);
      const vCls = r.verdict === 'improved' ? 'dn-good-t' : r.verdict === 'regressed' ? 'dn-bad-t' : '';
      const tr = el('tr', null, [
        el('td', { class: 'dn-mono', text: r.entry_id }),
        el('td', { class: 'dn-num dn-mono', text: svg.isNum(r.parent_drift_loss) ? svg.fmt(r.parent_drift_loss, 1) : '—' }),
        el('td', { class: 'dn-num dn-mono', text: svg.isNum(r.child_drift_loss) ? svg.fmt(r.child_drift_loss, 1) : '—' }),
        el('td', { class: 'dn-num dn-mono ' + vCls, text: svg.isNum(d) ? svg.fmtSigned(d, 1) : '—' }),
        el('td', { class: vCls, text: r.verdict || '—' }),
      ]);
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', () => ctx.navigate('board', { epochId, entry: r.entry_id, gen: m.challenger }));
      tbody.appendChild(tr);
    }
    tbl.appendChild(tbody);
    sec.appendChild(tbl);
  });
  if (any) article.appendChild(sec);
}
