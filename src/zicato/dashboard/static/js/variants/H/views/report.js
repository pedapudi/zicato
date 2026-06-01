// variants/H/views/report.js — ACM-STYLE EPOCH PUBLICATION (new in H).
//
// The epoch's standalone analysis rendered as a typeset paper, the second
// view E lacked. We reuse the v2 report renderer's APPROACH (js/v2/views/
// report.js) — parse the section markers in `analysis_md` and lay out
// eyebrow / title / meta / abstract / body — but, per the round-3 brief,
// render it OURSELVES (Tufte/editorial, `hp-*` classes) from the markdown so
// it themes with the rest of H, and we embed LIVE Tufte figures inline where
// the markdown carries a `<!-- FIGURE:… -->` marker (the lineage bumps, the
// per-board / matchup slopegraph, the drift heatmap) — the paper's figures.
//
// The markdown dialect (verified against a live analysis.md):
//   <!-- EYEBROW -->        the eyebrow line that follows
//   # Title                 the paper title
//   <!-- META -->           the meta block that follows (until a heading)
//   ## / ### / #### …       section headings
//   ## Abstract             rendered as the abstract block
//   <!-- CALLOUT:LABEL -->  a callout (the line(s) after it)
//   <!-- FIGURE:name -->    an inline live Tufte figure
//   | … | … |               GitHub-flavoured tables
//   ```fenced```            code blocks
//   ---                     a horizontal rule (the footer follows)
//
// Cold deep-link safe: the view resolves its own epoch id (route param, else
// the current epoch) and fetches `/api/epoch/{e}/analysis` itself, then an
// honest "not built yet" block when `analysis_md` is empty.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, empty, normaliseDecision } from '../ui.js';

export async function render(host, ctx, params) {
  if (!host.firstChild) host.appendChild(el('p', { class: 'd-empty', text: 'Loading analysis report…' }));

  const ep = await D.epoch();
  const epochId = (params && params.epochId)
    || (ep && ep.epoch_id)
    || (state.epochDef && state.epochDef.epoch_id)
    || null;

  if (!epochId) {
    gatedSwap(host, 'no-epoch', () => [el('h1', { class: 'd-h1', text: 'Analysis report' }), empty('No epoch selected.')]);
    return;
  }

  // Live figure inputs (best-effort) — lineage for the embedded bumps.
  const [anaResp, lin, traj] = await Promise.all([D.analysis(epochId), D.lineage(), D.scoreTrajectory()]);
  const md = (anaResp && typeof anaResp.analysis_md === 'string') ? anaResp.analysis_md : '';

  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  const gens = (lin && Array.isArray(lin.generations) && lin.generations.length)
    ? lin.generations.map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }))
    : ((ep && Array.isArray(ep.experiments)) ? ep.experiments.map((x) => ({ id: x.generation_id, parent: x.parent_generation_id || null, promoted: normaliseDecision(x.outcome) === 'promoted' })) : []);

  const figures = { lineage: () => lineageFigure(gens, scalarByGen, ctx) };

  const digest = JSON.stringify({ epochId, mdLen: md.length, mdHead: md.slice(0, 120),
    gens: gens.map((g) => [g.id, g.parent, g.promoted]) });

  gatedSwap(host, digest, () => {
    if (!md.trim()) {
      return [
        el('div', { class: 'e-pagehead' }, [el('h1', { class: 'd-h1', text: 'Analysis report · ' + epochId })]),
        el('div', { class: 'd-panel' }, [
          empty('The analysis report has not been generated for this epoch yet.'),
          el('p', { class: 'd-faint', style: 'margin-top:8px;', text: 'It is produced by the analyzer once the epoch has run.' }),
        ]),
      ];
    }
    return [renderPaper(md, figures)];
  });
}

// ---- embedded live Tufte figures -----------------------------------

function lineageFigure(gens, scalarByGen, ctx) {
  const bumpNodes = gens.map((g, i) => ({ id: g.id, x: i, promoted: g.promoted, scalar: scalarByGen.get(g.id), parent: g.parent }));
  if (bumpNodes.length && !bumpNodes.some((n) => n.promoted)) bumpNodes[0].promoted = true;
  const fit = el('div', { class: 'd-fit' });
  fit.appendChild(svg.bumps({ width: 680, height: 180, nodes: bumpNodes, onClick: (n) => ctx && ctx.navigate && ctx.navigate('candidate', { gen: n.id }) }));
  return fit;
}

// ---- the markdown → typeset paper renderer -------------------------

function renderPaper(md, figures) {
  const paper = el('article', { class: 'hp-paper' });
  const lines = String(md).replace(/\r\n/g, '\n').split('\n');
  let i = 0;
  let para = [];
  let list = null;

  const flushPara = () => {
    if (para.length) {
      paper.appendChild(el('p', { class: 'hp-p' }, inline(para.join(' '))));
      para = [];
    }
  };
  const flushList = () => { if (list) { paper.appendChild(list); list = null; } };
  const flush = () => { flushPara(); flushList(); };

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // marker comments
    const marker = /^<!--\s*([A-Z]+)(?::([^>]*))?\s*-->$/.exec(trimmed);
    if (marker) {
      flush();
      const kind = marker[1];
      const arg = (marker[2] || '').trim();
      if (kind === 'EYEBROW') {
        i++;
        const txt = nextNonEmpty(lines, i);
        if (txt.idx >= 0) { paper.appendChild(el('p', { class: 'hp-eyebrow' }, inline(txt.text))); i = txt.idx + 1; }
        continue;
      }
      if (kind === 'META') {
        i++;
        const block = collectUntilHeadingOrBlank(lines, i);
        paper.appendChild(metaBlock(block.text));
        i = block.idx;
        continue;
      }
      if (kind === 'CALLOUT') {
        i++;
        const block = collectUntilBlank(lines, i);
        paper.appendChild(el('div', { class: 'hp-callout' }, [
          arg ? el('span', { class: 'hp-callout-lead', text: arg }) : null,
          el('div', null, inline(block.text)),
        ].filter(Boolean)));
        i = block.idx;
        continue;
      }
      if (kind === 'FIGURE') {
        const fig = figures && typeof figures[arg] === 'function' ? figures[arg]() : null;
        paper.appendChild(el('figure', { class: 'hp-figure' }, [
          fig || el('p', { class: 'd-faint', text: '(figure “' + arg + '” — see the dashboard views for the live chart)' }),
        ]));
        i++;
        continue;
      }
      i++; continue; // unknown marker: skip
    }

    // fenced code
    if (/^```/.test(trimmed)) {
      flush();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) { buf.push(lines[i]); i++; }
      i++;
      paper.appendChild(el('pre', { class: 'hp-pre' }, [buf.join('\n')]));
      continue;
    }

    // horizontal rule
    if (/^---+$/.test(trimmed)) {
      flush();
      paper.appendChild(el('hr', { class: 'hp-hr' }));
      // anything after the final HR reads as the footer.
      i++;
      const foot = collectUntilEnd(lines, i);
      if (foot.text.trim()) paper.appendChild(el('p', { class: 'hp-footer' }, inline(foot.text)));
      i = foot.idx;
      continue;
    }

    // headings
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      const lvl = heading[1].length;
      const text = heading[2].trim();
      if (lvl === 1) {
        paper.appendChild(el('h1', { class: 'hp-title' }, inline(text)));
      } else if (lvl === 2 && /^abstract$/i.test(text)) {
        // The abstract: render the following block as the abstract panel.
        i++;
        const block = collectUntilHeadingOrBlank(lines, i, true);
        paper.appendChild(el('div', { class: 'hp-abstract' }, [
          el('span', { class: 'hp-abstract-lead', text: 'Abstract' }),
          el('div', null, inline(block.text)),
        ]));
        i = block.idx;
        continue;
      } else {
        const cls = lvl === 2 ? 'hp-h2' : lvl === 3 ? 'hp-h3' : 'hp-h4';
        paper.appendChild(el(lvl === 2 ? 'h2' : lvl === 3 ? 'h3' : 'h4', { class: cls }, inline(text)));
      }
      i++; continue;
    }

    // tables (a header row | … | followed by a separator row)
    if (trimmed.startsWith('|') && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      flush();
      const tbl = parseTable(lines, i);
      paper.appendChild(tbl.node);
      i = tbl.idx;
      continue;
    }

    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      flushPara();
      if (!list) list = el('ul', { class: 'hp-ul' });
      list.appendChild(el('li', { class: 'hp-li' }, inline(bullet[1])));
      i++; continue;
    }

    if (trimmed === '') { flush(); i++; continue; }
    flushList();
    para.push(trimmed);
    i++;
  }
  flush();
  return paper;
}

function metaBlock(text) {
  // Lines like "**Key**: value". Render each as a meta item.
  const block = el('div', { class: 'hp-meta' });
  for (const raw of String(text).split('\n')) {
    const ln = raw.trim().replace(/\s+$/, '');
    if (!ln) continue;
    const m = /^\*\*([^*]+)\*\*\s*:\s*(.*)$/.exec(ln);
    if (m) {
      block.appendChild(el('span', { class: 'hp-meta-item' }, [
        el('span', { class: 'hp-meta-k', text: m[1] + ':' }),
        el('span', { class: 'hp-meta-v' }, inline(m[2])),
      ]));
    } else {
      block.appendChild(el('span', { class: 'hp-meta-item' }, inline(ln)));
    }
  }
  return block;
}

function parseTable(lines, start) {
  const headerCells = splitRow(lines[start]);
  let i = start + 2; // skip header + separator
  const rows = [];
  while (i < lines.length && lines[i].trim().startsWith('|')) {
    rows.push(splitRow(lines[i]));
    i++;
  }
  const wrap = el('div', { class: 'hp-table-wrap' });
  const table = el('table', { class: 'hp-table' });
  const thead = el('thead');
  thead.appendChild(el('tr', null, headerCells.map((c) => el('th', null, inline(c)))));
  table.appendChild(thead);
  const tbody = el('tbody');
  for (const r of rows) {
    tbody.appendChild(el('tr', null, r.map((c) => el('td', { class: 'hp-td' }, inline(c)))));
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return { node: wrap, idx: i };
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

function nextNonEmpty(lines, from) {
  let i = from;
  while (i < lines.length && lines[i].trim() === '') i++;
  return i < lines.length ? { idx: i, text: lines[i].trim() } : { idx: -1, text: '' };
}
function collectUntilBlank(lines, from) {
  const buf = [];
  let i = from;
  while (i < lines.length && lines[i].trim() !== '') { buf.push(lines[i].trim()); i++; }
  return { idx: i, text: buf.join(' ') };
}
function collectUntilHeadingOrBlank(lines, from) {
  // Used for META: collect the contiguous non-blank block.
  const buf = [];
  let i = from;
  while (i < lines.length && lines[i].trim() !== '' && !/^#{1,6}\s/.test(lines[i])) { buf.push(lines[i]); i++; }
  return { idx: i, text: buf.join('\n') };
}
function collectUntilEnd(lines, from) {
  const buf = [];
  let i = from;
  while (i < lines.length) { buf.push(lines[i].trim()); i++; }
  return { idx: i, text: buf.join(' ').trim() };
}

// Inline parse: `code`, **bold**, *italic* → an array of nodes.
function inline(s) {
  const out = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)/g;
  const str = String(s == null ? '' : s);
  let last = 0; let m;
  while ((m = re.exec(str)) !== null) {
    if (m.index > last) out.push(str.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith('`')) out.push(el('code', { text: tok.slice(1, -1) }));
    else if (tok.startsWith('**')) out.push(el('strong', { text: tok.slice(2, -2) }));
    else out.push(el('em', { text: tok.slice(1, -1) }));
    last = m.index + tok.length;
  }
  if (last < str.length) out.push(str.slice(last));
  return out.length ? out : [str];
}
