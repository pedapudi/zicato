// variants/I/report.js — the ACM-style epoch publication renderer (Ledger).
//
// Round-3 signature view. The analyzer writes the epoch's publication as
// markdown (`/api/epoch/{id}/analysis` → `analysis_md`) carrying the same
// document markers the standalone report renderer uses:
//
//   <!-- EYEBROW -->        the next paragraph is the small-caps masthead eyebrow
//   # Title                 the paper title (the epoch)
//   <!-- META -->           the next paragraph is `**Label**: value` pairs →
//                           a masthead metadata grid (stacked label/value cells)
//   ## Abstract / ## …      numbered body sections
//   <!-- FIGURE: id -->     a slot where a live Tufte figure is embedded
//   Caption: …              a caption line (auto-numbered Figure N. …)
//
// We REUSE that document model (the same section markers report.js parses)
// but render it as DOM nodes, Tufte/editorial — never innerHTML on
// untrusted text, so it is XSS-safe and re-render-safe. `figureFor(id)`
// (optional) lets the caller substitute a LIVE figure node at a FIGURE
// marker, so the paper's figures are the dashboard's own Tufte charts.

import { el } from '../../core/dom.js';

const EYEBROW = '<!-- EYEBROW -->';
const META = '<!-- META -->';
const FIGURE_RE = /^<!--\s*FIGURE:\s*([^>]*?)\s*-->$/;
const CALLOUT_RE = /^<!--\s*CALLOUT:\s*([^>]*?)\s*-->$/;

// Parse analysis markdown into a structured document:
//   { eyebrow, title, meta:[{label,value}], blocks:[…], headings:[…] }
// `blocks` is an ordered list of typed render instructions.
export function parseAnalysis(md) {
  const text = String(md == null ? '' : md).replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  const doc = { eyebrow: null, title: null, meta: [], blocks: [], headings: [] };
  let i = 0;
  let pendingEyebrow = false;
  let pendingMeta = false;
  let pendingCaption = null;

  const usedIds = new Map();
  const uid = (base) => {
    const n = usedIds.get(base) || 0; usedIds.set(base, n + 1);
    return n === 0 ? base : `${base}-${n}`;
  };
  const slug = (s) => String(s || '').toLowerCase().trim()
    .replace(/[^\w\s-]/g, '').replace(/\s+/g, '-').slice(0, 60) || 'section';

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (trimmed === EYEBROW) { pendingEyebrow = true; i++; continue; }
    if (trimmed === META) { pendingMeta = true; i++; continue; }

    const fig = FIGURE_RE.exec(trimmed);
    if (fig) {
      doc.blocks.push({ kind: 'figure', figureId: fig[1].trim(), caption: pendingCaption });
      pendingCaption = null; i++; continue;
    }
    const call = CALLOUT_RE.exec(trimmed);
    if (call) { doc.blocks.push({ kind: 'callout', text: call[1].trim() }); i++; continue; }

    // skip any other html comment markers we don't model
    if (/^<!--/.test(trimmed)) { i++; continue; }

    if (trimmed === '') { i++; continue; }

    // fenced code
    const fence = /^(```|~~~)(.*)$/.exec(trimmed);
    if (fence) {
      const mark = fence[1]; const buf = []; i++;
      while (i < lines.length && !lines[i].trim().startsWith(mark)) { buf.push(lines[i]); i++; }
      i++;
      doc.blocks.push({ kind: 'code', text: buf.join('\n') });
      continue;
    }

    // thematic break
    if (/^(---|\*\*\*|___)\s*$/.test(trimmed)) { doc.blocks.push({ kind: 'rule' }); i++; continue; }

    // heading
    const h = /^(#{1,6})\s+(.*)$/.exec(trimmed);
    if (h) {
      const level = h[1].length;
      const htext = h[2].replace(/\s+#+\s*$/, '').trim();
      if (level === 1 && doc.title == null) {
        doc.title = htext;
        if (pendingEyebrow) { /* eyebrow precedes title; already captured below */ }
      } else {
        const id = uid(slug(htext));
        doc.blocks.push({ kind: 'heading', level, text: htext, id });
        doc.headings.push({ level, text: htext, id });
      }
      i++; continue;
    }

    // caption line — auto-numbered, attaches to the next figure/table
    if (/^Caption:/.test(trimmed)) {
      pendingCaption = trimmed.replace(/^Caption:\s*/, '');
      i++; continue;
    }

    // unordered list
    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*+]\s+/, '')); i++; }
      doc.blocks.push({ kind: 'ul', items });
      continue;
    }
    // ordered list
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+[.)]\s+/, '')); i++; }
      doc.blocks.push({ kind: 'ol', items });
      continue;
    }
    // blockquote
    if (/^\s*>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
      doc.blocks.push({ kind: 'quote', text: buf.join(' ') });
      continue;
    }

    // paragraph — gather consecutive non-special lines
    const buf = [line]; i++;
    while (i < lines.length && lines[i].trim() !== ''
      && !/^(#{1,6})\s|^\s*[-*+]\s|^\s*\d+[.)]\s|^\s*>|^(```|~~~)/.test(lines[i])
      && !/^<!--/.test(lines[i].trim()) && !/^Caption:/.test(lines[i].trim())) {
      buf.push(lines[i]); i++;
    }
    const ptext = buf.join('\n');
    if (pendingMeta) {
      doc.meta = parseMeta(ptext);
      pendingMeta = false;
    } else if (pendingEyebrow && doc.title == null && doc.eyebrow == null) {
      doc.eyebrow = ptext.trim();
      pendingEyebrow = false;
    } else {
      doc.blocks.push({ kind: 'p', text: ptext });
    }
  }
  return doc;
}

// `**Label**: value  \n**Label**: value` → [{label, value}].
function parseMeta(text) {
  const out = [];
  for (const raw of String(text).split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const m = /^\*\*(.+?)\*\*\s*:?\s*(.*)$/.exec(line);
    if (m) out.push({ label: m[1].trim(), value: m[2].trim() });
    else out.push({ label: '', value: line });
  }
  return out;
}

// Inline parse: **bold**, *italic*, `code`, [text](href) → array of nodes.
const INLINE_RE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|\*[^*]+\*|_[^_]+_)/g;
export function inlineNodes(text) {
  const src = String(text == null ? '' : text);
  const out = [];
  let last = 0; let m;
  INLINE_RE.lastIndex = 0;
  while ((m = INLINE_RE.exec(src)) !== null) {
    if (m.index > last) out.push(src.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith('**')) out.push(el('strong', { text: tok.slice(2, -2) }));
    else if (tok.startsWith('`')) out.push(el('code', { class: 'i-code-inline', text: tok.slice(1, -1) }));
    else if (tok.startsWith('[')) {
      const mm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      if (mm) out.push(el('a', { class: 'i-paper-link', href: mm[2], target: '_blank', rel: 'noopener' }, inlineNodes(mm[1])));
      else out.push(tok);
    } else if (tok.startsWith('*') || tok.startsWith('_')) out.push(el('em', { text: tok.slice(1, -1) }));
    else out.push(tok);
    last = m.index + tok.length;
  }
  if (last < src.length) out.push(src.slice(last));
  return out.length ? out : [src];
}

// Render the parsed document to a typeset publication node. `opts`:
//   { figureFor(id) -> node|null }  embed a live figure at a FIGURE marker.
// Returns a detached <article>. Sections are auto-numbered; figures carry
// an auto-numbered "Figure N." caption.
export function renderPaper(doc, opts = {}) {
  const o = opts || {};
  const art = el('article', { class: 'i-paper' });

  // Masthead — eyebrow / title / thin rule / metadata grid.
  const masthead = el('header', { class: 'i-paper-masthead' }, [
    doc.eyebrow ? el('div', { class: 'i-paper-eyebrow', text: doc.eyebrow }) : null,
    el('h1', { class: 'i-paper-title', text: doc.title || 'Epoch analysis' }),
    el('div', { class: 'i-paper-rule', 'aria-hidden': 'true' }),
    (doc.meta && doc.meta.length) ? el('div', { class: 'i-paper-meta' }, doc.meta.map((mt) => el('div', { class: 'i-paper-meta-cell' }, [
      mt.label ? el('span', { class: 'i-paper-meta-label', text: mt.label }) : null,
      el('span', { class: 'i-paper-meta-value' }, inlineNodes(mt.value)),
    ].filter(Boolean)))) : null,
  ].filter(Boolean));
  art.appendChild(masthead);

  const body = el('div', { class: 'i-paper-body' });
  let sectionNum = 0;
  let figureNum = 0;

  for (const b of doc.blocks) {
    switch (b.kind) {
      case 'heading': {
        let label = '';
        if (b.level === 2) { sectionNum += 1; label = sectionNum + '. '; }
        const tag = b.level <= 2 ? 'h2' : b.level === 3 ? 'h3' : 'h4';
        body.appendChild(el(tag, { class: 'i-paper-h i-paper-h' + b.level, id: 'i-paper-' + b.id }, [
          label ? el('span', { class: 'i-paper-secnum', text: label }) : null,
          ...inlineNodes(b.text),
        ].filter(Boolean)));
        break;
      }
      case 'p':
        body.appendChild(el('p', { class: 'i-paper-p' }, inlineNodes(b.text)));
        break;
      case 'ul':
        body.appendChild(el('ul', { class: 'i-paper-list' }, b.items.map((it) => el('li', null, inlineNodes(it)))));
        break;
      case 'ol':
        body.appendChild(el('ol', { class: 'i-paper-list i-paper-list-ol' }, b.items.map((it) => el('li', null, inlineNodes(it)))));
        break;
      case 'quote':
        body.appendChild(el('blockquote', { class: 'i-paper-quote' }, inlineNodes(b.text)));
        break;
      case 'code':
        body.appendChild(el('pre', { class: 'i-paper-code' }, [el('code', { text: b.text })]));
        break;
      case 'rule':
        body.appendChild(el('hr', { class: 'i-paper-hr' }));
        break;
      case 'callout':
        body.appendChild(el('aside', { class: 'i-paper-callout' }, inlineNodes(b.text)));
        break;
      case 'figure': {
        figureNum += 1;
        const live = typeof o.figureFor === 'function' ? o.figureFor(b.figureId) : null;
        body.appendChild(el('figure', { class: 'i-paper-figure' }, [
          live || el('div', { class: 'i-paper-figure-missing', text: '[figure ' + b.figureId + ' — rendered live in the dashboard]' }),
          el('figcaption', { class: 'i-paper-figcap' }, [
            el('span', { class: 'i-paper-figcap-label', text: 'Figure ' + figureNum + '. ' }),
            ...inlineNodes(b.caption || (b.figureId || '')),
          ]),
        ]));
        break;
      }
      default:
        break;
    }
  }
  art.appendChild(body);
  return art;
}
