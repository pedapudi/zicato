// variants/R/views/publication.js — the epoch-scoped ACM publication.
//
// The Publication section goes straight to detail (no item column). Reuses K's
// publication approach (parse the section markers; typeset eyebrow / title /
// meta / abstract / body; splice live Tufte figures at <!-- FIGURE:NAME -->),
// GFM tables render. The aggregate generation-scores TABLE + summary BAR CHART
// are COMBINED into one cohesive figure; per-matchup detail is appended.
//
// Bind: /api/epoch/{epoch_id}/analysis → { analysis_md }. Cold deep-link safe.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, empty, subhead, renderMarkdown } from '../ui.js';

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

export async function render(host, ctx, path) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'dr-empty', text: 'Reading epoch publication…' }));
  let epochId = (path && path.epoch) || null;
  if (!epochId) { const ep = await D.epoch(); epochId = ep && ep.epoch_id; }
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dr-h1', text: 'Epoch publication' }), empty('No epoch selected.')]);
    return;
  }

  const [analysis, lin, traj, bracket] = await Promise.all([D.analysis(epochId), D.lineage(), D.scoreTrajectory(), D.bracket()]);
  const md = (analysis && typeof analysis.analysis_md === 'string') ? analysis.analysis_md : '';
  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted })) : [];
  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups : [];
  const grids = await Promise.all(matchups.map((m) => (m.champion && m.challenger) ? D.matchupGrid(epochId, m.champion, m.challenger) : Promise.resolve(null)));
  const figures = { gens, scalarByGen, matchups, grids, ctx };

  const digest = JSON.stringify({
    epochId, mdLen: md.length,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    grids: grids.map((gr) => gr && Array.isArray(gr.entry_grid) ? gr.entry_grid.map((r) => [r.entry_id, r.parent_drift_loss, r.child_drift_loss, r.verdict]) : null),
  });

  gatedSwap(host, digest, () => {
    const paper = parsePaper(md);
    const article = el('article', { class: 'dr-paper' });
    const masthead = el('header', { class: 'dr-paper-masthead' });
    if (paper.eyebrow) masthead.appendChild(el('div', { class: 'dr-paper-eyebrow', text: paper.eyebrow }));
    masthead.appendChild(el('h1', { class: 'dr-paper-title', text: paper.title || `Epoch ${epochId}` }));
    masthead.appendChild(el('div', { class: 'dr-paper-rule' }));
    if (paper.meta.length) {
      masthead.appendChild(el('div', { class: 'dr-paper-metagrid' }, paper.meta.map((m) => el('div', { class: 'dr-paper-meta-cell' }, [
        m.label ? el('span', { class: 'dr-paper-meta-label', text: m.label }) : null,
        el('span', { class: 'dr-paper-meta-value', text: m.value }),
      ].filter(Boolean)))));
    }
    article.appendChild(masthead);

    if (!md.trim()) {
      article.appendChild(el('div', { class: 'dr-paper-statebox' }, [
        el('p', { class: 'dr-paper-statebox-h', text: 'The narrative report has not been written yet for ' + epochId + '.' }),
        el('p', { class: 'dr-faint' }, [
          'Run ', el('code', { class: 'dr-paper-code', text: 'zicato epoch analyze --epoch ' + epochId }),
          ' to build it. The live figures below are drawn from the run data regardless.',
        ]),
      ]));
      appendCanonicalFigures(article, figures);
      appendMatchupDetail(article, figures);
      return [article];
    }

    if (paper.abstract) {
      article.appendChild(el('section', { class: 'dr-paper-abstract' }, [
        el('div', { class: 'dr-paper-abstract-label', text: 'Abstract' }),
        renderMarkdown(paper.abstract),
      ]));
    }
    let figuresUsed = 0;
    const body = renderMarkdown(paper.body, { onFigure: (name) => { const node = figureFor(name, figures); if (node) figuresUsed += 1; return node; } });
    article.appendChild(el('div', { class: 'dr-paper-body' }, [body]));
    if (figuresUsed === 0) appendCanonicalFigures(article, figures);
    appendMatchupDetail(article, figures);
    return [article];
  });
}

function figureFor(name, figures) {
  const key = String(name).toLowerCase();
  const { gens, scalarByGen, matchups, grids, ctx } = figures;
  if (key.includes('score') || key.includes('aggregate') || key.includes('summary')) return aggregateScoresFigure(gens, scalarByGen);
  const g0 = grids && grids.find((gr) => gr && Array.isArray(gr.entry_grid));
  const m0 = grids ? matchups[grids.indexOf(g0)] : null;
  if ((key.includes('matchup') || key.includes('slope') || key.includes('duel')) && g0 && m0) {
    const fig = el('figure', { class: 'dr-paper-fig' });
    const series = g0.entry_grid.filter((r) => svg.isNum(r.parent_drift_loss) || svg.isNum(r.child_drift_loss))
      .map((r) => ({ label: r.entry_id, id: r.entry_id, a: svg.isNum(r.parent_drift_loss) ? r.parent_drift_loss : NaN, b: svg.isNum(r.child_drift_loss) ? r.child_drift_loss : NaN, verdict: r.verdict }));
    fig.appendChild(svg.pairedSlopegraph({ width: 520, height: Math.max(200, 50 + series.length * 26), left: { title: `champion ${m0.champion}` }, right: { title: `challenger ${m0.challenger}` }, labelGap: 150, goodDirection: 'down', series, onClick: (s) => ctx.navigate({ section: 'boards', entry: s.id, runGen: m0.challenger }) }));
    fig.appendChild(el('figcaption', { class: 'dr-paper-figcap', text: `Figure · paired per-board duel ${m0.champion} → ${m0.challenger}.` }));
    return fig;
  }
  if ((key.includes('drift') || key.includes('heatmap')) && g0 && m0) {
    const fig = el('figure', { class: 'dr-paper-fig' });
    const rows = g0.entry_grid.map((r) => ({ id: r.entry_id, label: r.entry_id }));
    const cols = [{ id: 'champion', label: m0.champion }, { id: 'challenger', label: m0.challenger }];
    const lossLookup = new Map();
    for (const r of g0.entry_grid) {
      if (svg.isNum(r.parent_drift_loss)) lossLookup.set(r.entry_id + '|champion', r.parent_drift_loss);
      if (svg.isNum(r.child_drift_loss)) lossLookup.set(r.entry_id + '|challenger', r.child_drift_loss);
    }
    fig.appendChild(svg.heatmap({ rows, cols, value: (rid, cid) => (lossLookup.has(rid + '|' + cid) ? lossLookup.get(rid + '|' + cid) : null) }));
    fig.appendChild(el('figcaption', { class: 'dr-paper-figcap', text: 'Figure · per-board drift loss heatmap (champion vs challenger; theme-aware ink).' }));
    return fig;
  }
  return null;
}

function aggregateScoresFigure(gens, scalarByGen) {
  const fig = el('figure', { class: 'dr-paper-fig dr-scores-fig' });
  const items = gens.map((g) => ({ id: g.id, label: g.id, promoted: g.promoted, value: scalarByGen.get(g.id) })).filter((it) => svg.isNum(it.value));
  if (!items.length) { fig.appendChild(el('figcaption', { class: 'dr-paper-figcap dr-faint', text: 'Figure · aggregate generation scores (no trajectory data yet).' })); return fig; }
  const combined = el('div', { class: 'dr-scores-combined' });
  combined.appendChild(svg.valueBars({ width: 300, rowHeight: 24, labelWidth: 60, items: items.map((it) => ({ label: it.label + (it.promoted ? ' ♛' : ''), value: it.value })) }));
  const tbl = el('table', { class: 'dr-md-table dr-scores-table' });
  tbl.appendChild(el('thead', null, [el('tr', null, [el('th', { text: 'generation' }), el('th', { class: 'dr-num', text: 'scalar (loss)' }), el('th', { text: 'outcome' })])]));
  const tbody = el('tbody');
  for (const it of items) tbody.appendChild(el('tr', null, [el('td', { class: 'dr-mono', text: it.label }), el('td', { class: 'dr-num dr-mono', text: svg.fmt(it.value, 2) }), el('td', { text: it.promoted ? 'promoted ♛' : 'rejected' })]));
  tbl.appendChild(tbody);
  combined.appendChild(tbl);
  fig.appendChild(combined);
  fig.appendChild(el('figcaption', { class: 'dr-paper-figcap', text: 'Figure · aggregate generation scores — the summary bar chart and its table are one visual (lower scalar is better).' }));
  return fig;
}

function appendCanonicalFigures(article, figures) {
  const plates = el('section', { class: 'dr-paper-plates' }, [
    el('h2', { class: 'dr-paper-h2', text: 'Figures' }),
    el('p', { class: 'dr-faint', text: 'Live, interactive — click any figure to drill into the dashboard.' }),
  ]);
  for (const name of ['aggregate-scores', 'matchup-slope', 'drift-heatmap']) {
    const f = figureFor(name, figures);
    if (f) plates.appendChild(f);
  }
  article.appendChild(plates);
}

function appendMatchupDetail(article, figures) {
  const { matchups, grids, ctx } = figures;
  const sec = el('section', { class: 'dr-paper-matchups' });
  sec.appendChild(el('h2', { class: 'dr-paper-h2', text: 'Per-match-up detail' }));
  let any = false;
  matchups.forEach((m, i) => {
    const grid = grids[i];
    const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
    if (!rows.length) return;
    any = true;
    sec.appendChild(subhead(`${m.champion} → ${m.challenger}${m.decision ? ' · ' + m.decision : ''}`));
    const tbl = el('table', { class: 'dr-md-table' });
    tbl.appendChild(el('thead', null, [el('tr', null, [el('th', { text: 'board entry' }), el('th', { class: 'dr-num', text: m.champion }), el('th', { class: 'dr-num', text: m.challenger }), el('th', { class: 'dr-num', text: 'Δ' }), el('th', { text: 'verdict' })])]));
    const tbody = el('tbody');
    for (const r of rows) {
      const d = svg.isNum(r.delta) ? r.delta : (svg.isNum(r.child_drift_loss) && svg.isNum(r.parent_drift_loss) ? r.child_drift_loss - r.parent_drift_loss : NaN);
      const vCls = r.verdict === 'improved' ? 'dr-good-t' : r.verdict === 'regressed' ? 'dr-bad-t' : '';
      const tr = el('tr', null, [
        el('td', { class: 'dr-mono', text: r.entry_id }),
        el('td', { class: 'dr-num dr-mono', text: svg.isNum(r.parent_drift_loss) ? svg.fmt(r.parent_drift_loss, 1) : '—' }),
        el('td', { class: 'dr-num dr-mono', text: svg.isNum(r.child_drift_loss) ? svg.fmt(r.child_drift_loss, 1) : '—' }),
        el('td', { class: 'dr-num dr-mono ' + vCls, text: svg.isNum(d) ? svg.fmtSigned(d, 1) : '—' }),
        el('td', { class: vCls, text: r.verdict || '—' }),
      ]);
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', () => ctx.navigate({ section: 'boards', entry: r.entry_id, runGen: m.challenger }));
      tbody.appendChild(tr);
    }
    tbl.appendChild(tbody);
    sec.appendChild(tbl);
  });
  if (any) article.appendChild(sec);
}
