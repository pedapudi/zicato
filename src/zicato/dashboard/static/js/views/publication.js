// js/views/publication.js — ACM-style epoch publication, as a TAB.
//
// The publication is a TAB rather than the home page. It parses the section
// markers; typesets eyebrow / title / meta / abstract / body; and splices live
// Tufte figures at the <!-- FIGURE:NAME --> markers. GitHub-flavoured markdown
// **tables render** (ui.renderMarkdown). The aggregate
// generation-scores TABLE and its summary BAR CHART are COMBINED into ONE
// cohesive visual; per-matchup detail (champion vs challenger per board) is
// appended from the matchup grid.
//
// Bind: /api/epoch/{epoch_id}/analysis → { analysis_html_inline, analysis_md }.
// Cold deep-link safe.
//
// THE SERVER RENDER IS PREFERRED. `/api/epoch` and `/api/epoch/{id}/analysis`
// BOTH run the full report renderer (analyzer.report.render_report_html_fragment
// over gather_epoch_report_data) on every call to produce `analysis_html_inline`
// — a paper-styled, self-contained fragment with its own scoped <style> and
// server-drawn figures. Re-rendering the markdown client-side threw that entire
// render away and printed a lesser paper. So: prefer the fragment when it is
// non-empty, and fall back to the markdown path (parsePaper + renderMarkdown)
// only when the server did not produce one (no analysis yet, a render failure,
// or the Rust supervisor). The live interactive figures are appended either way
// — they are the thing the static fragment cannot carry.

import { el } from '../core/dom.js';
import { state } from '../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { harmonografIsLive, harmonografMini } from '../core/harmonograf.js';
import { gatedSwap, empty, subhead, renderMarkdown, densityTokens, dataTable, deltaCell } from '../ui.js';
import { epochIsLive } from '../livestatus.js';

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
    epochId = (ep && ep.epoch_id) || null;
  }
  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'dn-h1', text: 'Epoch publication' }), empty('No epoch selected.')]);
    return;
  }

  // Class A: scope lineage / trajectory / bracket to THIS epoch.
  const [analysis, rows, traj, bracket] = await Promise.all([
    D.analysis(epochId), D.generationsForEpoch(epochId), D.scoreTrajectory(epochId), D.bracket(epochId),
  ]);
  const md = (analysis && typeof analysis.analysis_md === 'string') ? analysis.analysis_md : '';
  // The SERVER-rendered paper fragment — preferred over re-rendering `md`
  // client-side (see the header note). Empty string = no server render.
  const inlineHtml = (analysis && typeof analysis.analysis_html_inline === 'string')
    ? analysis.analysis_html_inline : '';

  const gens = rows.length
    ? rows.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: g.promoted == null ? null : !!g.promoted, decision: g.decision, decisionLabel: g.decision_label })) : [];
  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  const matchups = (bracket && Array.isArray(bracket.matchups)) ? bracket.matchups : [];

  // grids for EVERY decided matchup (per-matchup detail).
  const grids = await Promise.all(matchups.map((m) => (m.champion && m.challenger)
    ? D.matchupGrid(epochId, m.champion, m.challenger) : Promise.resolve(null)));

  // Is the loop running FOR THIS EPOCH? A publication of a settled epoch that
  // labels an undecided candidate "racing…" is describing a race that ended
  // `epochLive` is what puts that label in the past tense.
  const epochLive = epochIsLive(state, epochId);
  const figures = { gens, scalarByGen, matchups, grids, ctx, epochId, epochLive };

  const digest = JSON.stringify({
    epochId, mdLen: md.length,
    // the SERVER-rendered fragment is what actually paints when present, so its
    // length gates the swap too — a re-run `epoch analyze` that changes the
    // rendered paper repaints, an identical re-render stays byte-identical.
    htmlLen: inlineHtml.length,
    gens: gens.map((g) => [g.id, g.parent, g.promoted, scalarByGen.has(g.id) ? scalarByGen.get(g.id).toFixed(3) : null]),
    // every entry_grid field the per-match-up table RENDERS folds here: the two
    // drift losses + verdict, the continuous #18 score pair and their Δ, the
    // replicate spread, the precision/recall metrics, `won_by`, `decided_by`,
    // and the two session ids that decide whether a harmonograf deep link
    // paints — plus the grid-level `drift_present`, which decides whether the
    // drift columns exist at all.
    grids: grids.map((gr) => gr ? [gr.drift_present !== false, Array.isArray(gr.entry_grid) ? gr.entry_grid.map((r) => [
      r.entry_id, r.parent_drift_loss, r.child_drift_loss, r.verdict, r.decided_by || null,
      svg.isNum(r.parent_score) ? r.parent_score.toFixed(3) : null,
      svg.isNum(r.child_score) ? r.child_score.toFixed(3) : null,
      svg.isNum(r.delta_score) ? r.delta_score.toFixed(4) : null,
      svg.isNum(r.score_se) ? r.score_se.toFixed(4) : null,
      metricsDigest(r.parent_metrics), metricsDigest(r.child_metrics),
      r.won_by == null ? null : String(r.won_by),
      r.parent_session_id || null, r.child_session_id || null,
    ]) : null] : null),
    // the harmonograf deep links are liveness-gated (core/harmonograf.js), so a
    // server coming up / a run ending must repaint the link column. Same fold
    // the candidate dossier uses (`hgLive`).
    hgLive: harmonografIsLive(),
    // the scores table's pending label is tense-bound, so its liveness folds too.
    epochLive: epochLive ? 1 : 0,
  });

  gatedSwap(host, digest, () => {
    // ── PREFERRED PATH: the server's own paper render ────────────────────
    if (inlineHtml.trim()) {
      const served = el('article', { class: 'dn-paper' });
      // the fragment ships its own scoped <style> + article markup; it scrolls
      // inside its OWN container so a wide server-rendered table can never make
      // the page scroll sideways (the never-overflow house rule).
      served.appendChild(el('div', { class: 'dn-paper-served dn-table-scroll', html: inlineHtml }));
      // the LIVE, interactive figures the static fragment cannot carry.
      appendCanonicalFigures(served, figures);
      appendMatchupDetail(served, figures);
      return [served];
    }

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
  const { gens, scalarByGen, matchups, grids, ctx, epochId, epochLive } = figures;

  if (key.includes('score') || key.includes('aggregate') || key.includes('summary')) {
    return aggregateScoresFigure(gens, scalarByGen, epochLive);
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
    fig.appendChild(svg.pairedSlopegraph({ width: 520, height: Math.max(Math.round(200 * densityTokens().sizeScale), 50 + series.length * Math.round(26 * densityTokens().sizeScale)),
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
function aggregateScoresFigure(gens, scalarByGen, epochLive) {
  const fig = el('figure', { class: 'dn-paper-fig dn-scores-fig' });
  const items = gens.map((g) => ({ id: g.id, label: g.id, promoted: g.promoted, parent: g.parent, decision: g.decision, decisionLabel: g.decisionLabel, value: scalarByGen.get(g.id) }))
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
  const tbl = dataTable({
    class: 'dn-md-table dn-scores-table',
    columns: [{ label: 'generation' }, { label: 'scalar (loss)', class: 'dn-num' }, { label: 'outcome' }],
    rows: items.map((it) => {
      // Class B: an unscored candidate reads pending, never rejected. And the
      // pending WORD is tense-bound: a publication of a settled epoch
      // that says "racing…" is describing a race that finished. The pill's own
      // liveness-aware vocabulary decides it; this table only re-skins the two
      // labels it renders differently (the ♛ and the short "seed").
      const dec = it.decision || 'pending';
      const label = dec === 'promoted' ? it.decisionLabel + ' ♛' : it.decisionLabel;
      return [
        { class: 'dn-mono', text: it.label },
        { class: 'dn-num dn-mono', text: svg.fmt(it.value, 2) },
        { text: label },
      ];
    }),
  });
  combined.appendChild(el('div', { class: 'dn-table-scroll' }, [tbl]));
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

// The precision/recall decomposition a scorer may hang off an entry_grid row
// (`parent_metrics` / `child_metrics`, #18). Rendered as "P .82 / R .60" and
// folded into the digest at the SAME precision it prints, so a no-op beat is
// byte-identical. Null when the scorer exposed neither number.
export function prText(metrics) {
  if (!metrics || typeof metrics !== 'object') return null;
  const p = svg.isNum(metrics.precision) ? metrics.precision : null;
  const rc = svg.isNum(metrics.recall) ? metrics.recall : null;
  if (p == null && rc == null) return null;
  return 'P ' + (p == null ? '—' : svg.fmt(p, 2)) + ' / R ' + (rc == null ? '—' : svg.fmt(rc, 2));
}
export function metricsDigest(metrics) { return prText(metrics); }

// Per-matchup detail — champion vs challenger per board, for EVERY decided
// round (the brief mandates this in the paper).
//
// The table honours the FULL entry_grid row contract (query/tournament_view.py
// build_matchup_grid): the continuous score pair with its Δ (positive = better)
// and replicate spread, the drift-loss pair with its Δ (positive = worse) when
// the workspace has a drift stream at all, the precision/recall decomposition,
// and the server's `verdict` / `won_by` / `decided_by` — the last naming which
// channel resolved the row, so the tone rides only the column that decided it.
// Each block of columns appears only when its channel carries values, so a
// bool-only board renders exactly as it did before the columns existed.
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
    // the continuous-score columns only appear when SOMETHING on this grid was
    // scored — a bool-only board must not grow four empty columns.
    const scored = rows.some((r) => svg.isNum(r.parent_score) || svg.isNum(r.child_score));
    // The DRIFT columns are dropped when the server reports the channel absent:
    // an adapter that emits no drift stream writes a structural 0.0 on every
    // entry, and three columns of zeroes beside a Δ of 0.0 say nothing.
    const showDrift = grid.drift_present !== false;
    const anySe = rows.some((r) => svg.isNum(r.score_se));
    const anyMetrics = rows.some((r) => prText(r.parent_metrics) || prText(r.child_metrics));
    const anySession = rows.some((r) => r.parent_session_id || r.child_session_id);
    sec.appendChild(subhead(`${m.champion} → ${m.challenger}${m.decision ? ' · ' + m.decision : ''}`));
    const columns = [{ label: 'board entry' }];
    if (showDrift) {
      columns.push({ label: 'loss ' + m.champion, class: 'dn-num' },
        { label: 'loss ' + m.challenger, class: 'dn-num' }, { label: 'Δ loss', class: 'dn-num' });
    }
    if (scored) {
      columns.push({ label: 'score ' + m.champion, class: 'dn-num' },
        { label: 'score ' + m.challenger, class: 'dn-num' }, { label: 'Δ score', class: 'dn-num' });
      if (anySe) columns.push({ label: '± se', class: 'dn-num' });
    }
    if (anyMetrics) columns.push({ label: 'precision / recall' });
    columns.push({ label: 'verdict' }, { label: 'won by' }, { label: 'decided by' });
    if (anySession) columns.push({ label: 'trace' });
    const tbl = dataTable({
      class: 'dn-md-table',
      columns,
      rows: rows.map((r) => {
        const vCls = r.verdict === 'improved' ? 'dn-good-t' : r.verdict === 'regressed' ? 'dn-bad-t' : '';
        const cells = [{ class: 'dn-mono', text: r.entry_id }];
        if (showDrift) {
          const d = svg.isNum(r.delta) ? r.delta : NaN;
          cells.push({ class: 'dn-num dn-mono', text: svg.isNum(r.parent_drift_loss) ? svg.fmt(r.parent_drift_loss, 1) : '—' });
          cells.push({ class: 'dn-num dn-mono', text: svg.isNum(r.child_drift_loss) ? svg.fmt(r.child_drift_loss, 1) : '—' });
          // Δ loss keeps the LOSS convention: positive = worse, so the verdict
          // tone only rides the column that decided the row.
          cells.push({ class: 'dn-num dn-mono ' + (r.decided_by === 'drift' ? vCls : ''), text: svg.isNum(d) ? svg.fmtSigned(d, 1) : '—' });
        }
        if (scored) {
          cells.push({ class: 'dn-num dn-mono', text: svg.isNum(r.parent_score) ? svg.fmt(r.parent_score, 2) : '—' });
          cells.push({ class: 'dn-num dn-mono', text: svg.isNum(r.child_score) ? svg.fmt(r.child_score, 2) : '—' });
          // Δ score runs the other way: positive = better.
          cells.push({ class: 'dn-num dn-mono ' + (r.decided_by === 'score' ? vCls : ''), text: svg.isNum(r.delta_score) ? svg.fmtSigned(r.delta_score, 3) : '—' });
          // Replicate spread on the challenger's score. `--`, never ±0.000: a
          // single draw measured no spread and must not imply a precision.
          if (anySe) cells.push({ class: 'dn-num dn-mono dn-faint', text: svg.isNum(r.score_se) ? '±' + r.score_se.toFixed(3) : '--' });
        }
        if (anyMetrics) {
          // champion / challenger precision-recall, in that order.
          const pp = prText(r.parent_metrics);
          const cp = prText(r.child_metrics);
          cells.push({ class: 'dn-mono dn-faint', text: (pp || '—') + '  →  ' + (cp || '—') });
        }
        cells.push({ class: vCls, text: r.verdict || '—' });
        // `verdict` / `won_by` / `decided_by` are the SERVER's call — it resolves
        // the entry against the channel the contract populates (score, then the
        // pass predicate, then drift) and names which one decided. Rendered,
        // never re-derived here from the numbers in the row.
        const wonCls = r.won_by == null ? 'dn-faint'
          : (String(r.won_by) === String(m.challenger) ? 'dn-good-t' : '');
        cells.push({ class: 'dn-mono ' + wonCls, text: r.won_by == null ? '—' : String(r.won_by) });
        cells.push({ class: 'dn-mono dn-faint', text: r.decided_by || '—' });
        if (anySession) {
          const links = [];
          const pl = r.parent_session_id
            ? harmonografMini({ adk_session_id: r.parent_session_id }, m.champion, `open ${m.champion}'s ${r.entry_id} trace in harmonograf`) : null;
          const cl = r.child_session_id
            ? harmonografMini({ adk_session_id: r.child_session_id }, m.challenger, `open ${m.challenger}'s ${r.entry_id} trace in harmonograf`) : null;
          if (pl) links.push(pl);
          if (cl) { if (links.length) links.push(el('span', { class: 'dn-faint', text: ' · ' })); links.push(cl); }
          cells.push(links.length
            ? { el: el('span', { class: 'dn-paper-trace' }, links) }
            : { class: 'dn-faint', text: '—' });
        }
        return {
          style: 'cursor: pointer',
          onClick: () => ctx.navigate('board', { epochId, entry: r.entry_id, gen: m.challenger }),
          cells,
        };
      }),
    });
    sec.appendChild(el('div', { class: 'dn-table-scroll' }, [tbl]));
  });
  if (any) {
    sec.appendChild(el('p', { class: 'dn-faint', style: 'font-size:11px;margin:8px 0 0;',
      text: 'drift loss (lower = better) · score is the continuous per-entry outcome (higher = better) · "won by" is the server\'s per-board call · a trace link opens that side\'s run in harmonograf while a server is reachable' }));
    article.appendChild(sec);
  }
}
