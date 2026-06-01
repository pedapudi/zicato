// variants/K/paper.js — the ACM-style epoch publication (K's centerpiece).

import { el } from '../../core/dom.js';
import { renderMarkdown, figureFrame } from './ui.js';

export function parsePaper(md) {
  const text = String(md || '').replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  const out = { eyebrow: '', title: '', meta: [], abstract: '', body: '' };
  let i = 0;
  const bodyLines = [];
  let inBody = false;
  let abstractLines = null;

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
        // meta pairs may be `  `-joined on one logical block; split per pair.
        out.meta = buf.join('\n').split(/\n|\s{2,}/).map((s) => parseMetaPair(s)).filter(Boolean);
        continue;
      }
      // The first H2 starts the body proper. An "## Abstract" H2 is captured
      // separately as the lede.
      const h2 = /^##\s+(.*)$/.exec(line);
      if (h2) {
        inBody = true;
        if (/abstract/i.test(h2[1])) {
          abstractLines = [];
          i++;
          while (i < lines.length && !/^##\s+/.test(lines[i])) { abstractLines.push(lines[i]); i++; }
          out.abstract = abstractLines.join('\n').trim();
          continue;
        }
        bodyLines.push(line); i++; continue;
      }
      // pre-body prose before any H2 (e.g. the "### Goal" block) → body.
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
  const article = el('article', { class: 'vk-paper' });

  const masthead = el('header', { class: 'vk-masthead' });
  if (p.eyebrow) masthead.appendChild(el('div', { class: 'vk-eyebrow', text: p.eyebrow }));
  masthead.appendChild(el('h1', { class: 'vk-paper-title', text: p.title || (o.epochId ? `Epoch ${o.epochId}` : 'Epoch analysis') }));
  masthead.appendChild(el('div', { class: 'vk-rule' }));
  if (Array.isArray(p.meta) && p.meta.length) {
    masthead.appendChild(el('div', { class: 'vk-meta' }, p.meta.map((m) => el('div', { class: 'vk-meta-cell' }, [
      m.label ? el('span', { class: 'vk-meta-label', text: m.label }) : null,
      el('span', { class: 'vk-meta-value', text: m.value }),
    ].filter(Boolean)))));
  }
  article.appendChild(masthead);

  if (o.broken) {
    article.appendChild(el('div', { class: 'vk-statebox vk-broken' }, [
      el('p', { class: 'vk-statebox-h', text: 'The analysis report could not be loaded.' }),
      el('p', { class: 'vk-faint', text: o.broken === true ? 'The analysis endpoint failed.' : String(o.broken) }),
    ]));
    appendFigureGallery(article, o);
    return article;
  }
  if (o.missing) {
    article.appendChild(el('div', { class: 'vk-statebox vk-notyet' }, [
      el('p', { class: 'vk-statebox-h', text: 'The narrative report has not been written yet.' }),
      el('p', { class: 'vk-faint' }, [
        'Run ', el('code', { class: 'vk-md-code', text: 'zicato epoch analyze --epoch ' + (o.epochId || '<epoch>') }),
        ' to build it. The live figures below are drawn from the run data regardless.',
      ]),
    ]));
    appendFigureGallery(article, o);
    return article;
  }

  if (p.abstract) {
    const abs = el('section', { class: 'vk-abstract' }, [
      el('div', { class: 'vk-abstract-label', text: 'Abstract' }),
      renderMarkdown(p.abstract),
    ]);
    article.appendChild(abs);
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
  article.appendChild(el('div', { class: 'vk-paper-body' }, [body]));

  // If the markdown carried NO figure markers we could resolve, splice the
  if (figuresUsed === 0) appendFigureGallery(article, o, masthead);

  return article;
}

// The canonical figure gallery — the live figures embedded as the paper's
// plates when the markdown gave us no marker positions to splice into.
function appendFigureGallery(article, o, after) {
  const facs = o.canonicalFigures;
  if (!Array.isArray(facs) || !facs.length) return;
  const plates = el('section', { class: 'vk-plates' }, [
    el('h2', { class: 'vk-h2', text: 'Figures' }),
    el('p', { class: 'vk-faint vk-plates-note', text: 'Live, interactive — click any figure to drill into the dashboard.' }),
    ...facs.map((f, i) => f(i + 1)).filter(Boolean),
  ]);
  if (after && after.nextSibling) article.insertBefore(plates, after.nextSibling);
  else article.appendChild(plates);
}

// A convenience the view uses to wrap a built mark in the standard live
export { figureFrame };
