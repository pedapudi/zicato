// variants/O/paper.js — the ACM-style epoch publication (reused as a TAB).
//
// The operator judged K's publication renderer the best of all variants but
// REJECTED the paper-first metaphor — so Compass embeds the publication as
// the "publication" FACET of a selected generation's detail pane, reusing
// this K-grade renderer. It parses the analysis markdown's section markers
// (EYEBROW / META / ## Abstract / body), renders eyebrow · title · meta ·
// abstract · typeset body, and splices LIVE figures at `<!-- FIGURE:name -->`
// markers. GFM tables render (the renderMarkdown in ui.js handles them).

import { el } from '../../core/dom.js';
import { renderMarkdown } from './ui.js';

export function parsePaper(md) {
  const text = String(md || '').replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  const out = { eyebrow: '', title: '', meta: [], abstract: '', body: '' };
  let i = 0;
  const bodyLines = [];
  let inBody = false;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();
    if (!inBody) {
      if (/^<!--\s*EYEBROW\s*-->$/.test(trimmed)) {
        i++;
        while (i < lines.length && lines[i].trim() === '') i++;
        out.eyebrow = (lines[i] || '').trim();
        i++; continue;
      }
      const h1 = /^#\s+(.*)$/.exec(line);
      if (h1 && !out.title) { out.title = h1[1].trim(); i++; continue; }
      if (/^<!--\s*META\s*-->$/.test(trimmed)) {
        i++;
        const buf = [];
        while (i < lines.length && lines[i].trim() !== '') { buf.push(lines[i].trim()); i++; }
        out.meta = buf.join('\n').split(/\n|\s{2,}/).map((s) => parseMetaPair(s)).filter(Boolean);
        continue;
      }
      const h2 = /^##\s+(.*)$/.exec(line);
      if (h2) {
        inBody = true;
        if (/abstract/i.test(h2[1])) {
          const abstractLines = [];
          i++;
          while (i < lines.length && !/^##\s+/.test(lines[i])) { abstractLines.push(lines[i]); i++; }
          out.abstract = abstractLines.join('\n').trim();
          continue;
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

export function renderPaper(opts) {
  const o = opts || {};
  const p = o.paper || { eyebrow: '', title: '', meta: [], abstract: '', body: '' };
  const article = el('article', { class: 'vo-paper' });

  const masthead = el('header', { class: 'vo-masthead' });
  if (p.eyebrow) masthead.appendChild(el('div', { class: 'vo-eyebrow', text: p.eyebrow }));
  masthead.appendChild(el('h1', { class: 'vo-paper-title', text: p.title || (o.epochId ? `Epoch ${o.epochId}` : 'Epoch analysis') }));
  masthead.appendChild(el('div', { class: 'vo-rule' }));
  if (Array.isArray(p.meta) && p.meta.length) {
    masthead.appendChild(el('div', { class: 'vo-meta' }, p.meta.map((m) => el('div', { class: 'vo-meta-cell' }, [
      m.label ? el('span', { class: 'vo-meta-label', text: m.label }) : null,
      el('span', { class: 'vo-meta-value', text: m.value }),
    ].filter(Boolean)))));
  }
  article.appendChild(masthead);

  if (o.broken) {
    article.appendChild(el('div', { class: 'vo-statebox' }, [
      el('p', { class: 'vo-statebox-h', text: 'The analysis report could not be loaded.' }),
      el('p', { class: 'vo-faint', text: o.broken === true ? 'The analysis endpoint failed.' : String(o.broken) }),
    ]));
    appendFigureGallery(article, o);
    return article;
  }
  if (o.missing) {
    article.appendChild(el('div', { class: 'vo-statebox' }, [
      el('p', { class: 'vo-statebox-h', text: 'The narrative report has not been written yet.' }),
      el('p', { class: 'vo-faint' }, [
        'Run ', el('code', { class: 'vo-md-code', text: 'zicato epoch analyze --epoch ' + (o.epochId || '<epoch>') }),
        ' to build it. The live figures below are drawn from the run data regardless.',
      ]),
    ]));
    appendFigureGallery(article, o);
    return article;
  }

  if (p.abstract) {
    article.appendChild(el('section', { class: 'vo-abstract' }, [
      el('div', { class: 'vo-abstract-label', text: 'Abstract' }),
      renderMarkdown(p.abstract),
    ]));
  }

  let figuresUsed = 0;
  const body = renderMarkdown(p.body, {
    onFigure: (name) => {
      const fac = o.figures && o.figures[name];
      if (!fac) return null;
      figuresUsed += 1;
      const node = fac(figuresUsed);
      return node || null;
    },
  });
  article.appendChild(el('div', { class: 'vo-paper-body' }, [body]));

  if (figuresUsed === 0) appendFigureGallery(article, o, masthead);
  return article;
}

function appendFigureGallery(article, o, after) {
  const facs = o.canonicalFigures;
  if (!Array.isArray(facs) || !facs.length) return;
  const plates = el('section', { class: 'vo-plates' }, [
    el('h2', { class: 'vo-h2', text: 'Figures' }),
    el('p', { class: 'vo-faint vo-plates-note', text: 'Live, interactive — click any figure to drill into the detail pane.' }),
    ...facs.map((f, i) => f(i + 1)).filter(Boolean),
  ]);
  if (after && after.nextSibling) article.insertBefore(plates, after.nextSibling);
  else article.appendChild(plates);
}
