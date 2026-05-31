// variants/B/lib/prose.js — the editorial typography primitives.
//
// Variant B reads like a research magazine, so its building blocks are
// typographic: a pull-quote, a verdict badge, a section with a hung label,
// an honest-state note — and a lightweight, SAFE Markdown renderer for the
// proposer brief (which can be long and complex; the spec demands a real,
// well-typeset home for it with a table-of-contents and collapsible
// sections, never a truncated line).
//
// The Markdown renderer is deliberately small and builds real DOM nodes
// (never innerHTML) so it is XSS-safe by construction and re-render-safe.
// It supports the subset a brief uses: ATX headings, paragraphs, unordered
// and ordered lists, blockquotes, fenced + inline code, bold/italic, and
// links. Anything it does not recognise degrades to a plain paragraph —
// the text is always shown, never dropped.

import { el } from '../../../core/dom.js';

// ---------------------------------------------------------------------------
// Inline formatting: **bold**, *italic* / _italic_, `code`, [text](href).
// Returns an array of text + element nodes. Order of operations matters;
// we tokenise with a single combined regex pass.
// ---------------------------------------------------------------------------
const INLINE_RE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|\*[^*]+\*|_[^_]+_)/g;

export function inlineProse(text) {
  const src = String(text == null ? '' : text);
  const out = [];
  let last = 0;
  let m;
  INLINE_RE.lastIndex = 0;
  while ((m = INLINE_RE.exec(src)) !== null) {
    if (m.index > last) out.push(src.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith('**')) {
      out.push(el('strong', null, [tok.slice(2, -2)]));
    } else if (tok.startsWith('`')) {
      out.push(el('code', { class: 'vb-code-inline' }, [tok.slice(1, -1)]));
    } else if (tok.startsWith('[')) {
      const mm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      if (mm) {
        out.push(el('a', {
          class: 'vb-prose-link', href: mm[2], target: '_blank', rel: 'noopener',
        }, inlineProse(mm[1])));
      } else { out.push(tok); }
    } else if (tok.startsWith('*') || tok.startsWith('_')) {
      out.push(el('em', null, [tok.slice(1, -1)]));
    } else { out.push(tok); }
    last = m.index + tok.length;
  }
  if (last < src.length) out.push(src.slice(last));
  return out.length ? out : [src];
}

// A stable slug for a heading (used as the TOC anchor target).
export function slug(text) {
  return String(text || '')
    .toLowerCase().trim()
    .replace(/[^\w\s-]/g, '')
    .replace(/\s+/g, '-')
    .slice(0, 60) || 'section';
}

// ---------------------------------------------------------------------------
// Block-level Markdown → an array of { node, heading } where `heading`,
// when present, is { level, text, id } so the caller can build a TOC and
// wire collapsible sections. Returns { blocks, headings }.
// ---------------------------------------------------------------------------
export function parseMarkdown(md) {
  const text = String(md == null ? '' : md).replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  const blocks = [];
  const headings = [];
  let i = 0;
  const usedIds = new Map();
  const uniqueId = (base) => {
    const n = usedIds.get(base) || 0;
    usedIds.set(base, n + 1);
    return n === 0 ? base : `${base}-${n}`;
  };

  while (i < lines.length) {
    let line = lines[i];
    // Blank line — skip.
    if (/^\s*$/.test(line)) { i += 1; continue; }

    // Fenced code block.
    const fence = /^(```|~~~)(.*)$/.exec(line);
    if (fence) {
      const mark = fence[1];
      const lang = fence[2].trim();
      const buf = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith(mark)) { buf.push(lines[i]); i += 1; }
      i += 1; // closing fence
      blocks.push({
        node: el('pre', { class: 'vb-code-block', 'data-lang': lang || null }, [
          el('code', null, [buf.join('\n')]),
        ]),
      });
      continue;
    }

    // ATX heading.
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length;
      const htext = h[2].replace(/\s+#+\s*$/, '').trim();
      const id = uniqueId(slug(htext));
      const tag = level <= 2 ? 'h2' : level === 3 ? 'h3' : 'h4';
      const node = el(tag, {
        class: `vb-prose-h vb-prose-h${level}`, id: `vb-brief-${id}`,
      }, inlineProse(htext));
      blocks.push({ node, heading: { level, text: htext, id } });
      headings.push({ level, text: htext, id });
      i += 1;
      continue;
    }

    // Blockquote.
    if (/^\s*>/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ''));
        i += 1;
      }
      blocks.push({
        node: el('blockquote', { class: 'vb-prose-quote' }, inlineProse(buf.join(' '))),
      });
      continue;
    }

    // Unordered list.
    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, ''));
        i += 1;
      }
      blocks.push({
        node: el('ul', { class: 'vb-prose-list' },
          items.map((it) => el('li', null, inlineProse(it)))),
      });
      continue;
    }

    // Ordered list.
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+[.)]\s+/, ''));
        i += 1;
      }
      blocks.push({
        node: el('ol', { class: 'vb-prose-list vb-prose-list-ol' },
          items.map((it) => el('li', null, inlineProse(it)))),
      });
      continue;
    }

    // Paragraph — gather consecutive non-blank, non-special lines.
    const buf = [line];
    i += 1;
    while (i < lines.length && !/^\s*$/.test(lines[i])
      && !/^(#{1,6})\s|^\s*[-*+]\s|^\s*\d+[.)]\s|^\s*>|^(```|~~~)/.test(lines[i])) {
      buf.push(lines[i]); i += 1;
    }
    blocks.push({ node: el('p', { class: 'vb-prose-p' }, inlineProse(buf.join(' '))) });
  }

  return { blocks, headings };
}

// ---------------------------------------------------------------------------
// renderBrief — a beautifully typeset home for the proposer brief, with a
// table-of-contents rail and collapsible sections (the spec's requirement).
// `onNavigate(id)` is optional; default scrolls to the anchor.
// ---------------------------------------------------------------------------
export function renderBrief(md, opts = {}) {
  const { blocks, headings } = parseMarkdown(md);
  const wrap = el('div', { class: 'vb-brief' });

  // The table of contents — only when there are >= 2 headings worth listing.
  const tocHeadings = headings.filter((h) => h.level <= 3);
  if (tocHeadings.length >= 2) {
    const toc = el('nav', { class: 'vb-brief-toc', 'aria-label': 'Brief contents' }, [
      el('p', { class: 'vb-brief-toc-title' }, ['Contents']),
      el('ol', { class: 'vb-brief-toc-list' }, tocHeadings.map((h) => el('li', {
        class: `vb-brief-toc-item vb-brief-toc-l${h.level}`,
      }, [
        el('a', {
          class: 'vb-brief-toc-link', href: `#vb-brief-${h.id}`,
          onclick: (ev) => {
            if (opts.onNavigate) { if (ev && ev.preventDefault) ev.preventDefault(); opts.onNavigate(h.id); }
          },
        }, [h.text]),
      ]))),
    ]);
    wrap.appendChild(toc);
  }

  // The body. We group blocks under their nearest preceding h2 as
  // collapsible <details> sections so a long brief stays scannable.
  const body = el('article', { class: 'vb-brief-body' });
  let currentSection = null;
  let currentBody = null;
  const flush = () => {
    if (currentSection) body.appendChild(currentSection);
    currentSection = null; currentBody = null;
  };
  for (const b of blocks) {
    if (b.heading && b.heading.level <= 2) {
      flush();
      currentBody = el('div', { class: 'vb-brief-sec-body' });
      currentSection = el('details', { class: 'vb-brief-sec', open: 'open' }, [
        el('summary', { class: 'vb-brief-sec-summary' }, [
          el('span', { class: 'vb-brief-sec-marker', 'aria-hidden': 'true' }, ['▾']),
          el('span', { class: 'vb-brief-sec-name' }, inlineProse(b.heading.text)),
        ]),
        currentBody,
      ]);
      // Re-id the summary's heading anchor target onto the details element
      // so a TOC jump lands on the section.
      currentSection.setAttribute('id', `vb-brief-${b.heading.id}`);
    } else if (currentBody) {
      currentBody.appendChild(b.node);
    } else {
      // Pre-amble before any h2 — render flat.
      body.appendChild(b.node);
    }
  }
  flush();
  if (!body.firstChild) {
    body.appendChild(el('p', { class: 'vb-prose-p vb-muted' }, [
      'This epoch carries no proposer brief.',
    ]));
  }
  wrap.appendChild(body);
  return wrap;
}

// ---------------------------------------------------------------------------
// Small editorial primitives shared across views.
// ---------------------------------------------------------------------------

// A section with a hung label in the margin (the magazine-column look).
export function section(label, children, opts = {}) {
  return el('section', { class: 'vb-section' + (opts.class ? ' ' + opts.class : '') }, [
    label ? el('div', { class: 'vb-section-rail' }, [
      el('span', { class: 'vb-section-label' }, [label]),
      opts.sub ? el('span', { class: 'vb-section-sub' }, [opts.sub]) : null,
    ].filter(Boolean)) : null,
    el('div', { class: 'vb-section-body' }, Array.isArray(children) ? children : [children]),
  ].filter(Boolean));
}

// The pull-quote — the hypothesis/bet rendered large, as a magazine
// pull-quote. `attribution` (optional) is a small caption beneath.
export function pullQuote(text, opts = {}) {
  return el('blockquote', { class: 'vb-pullquote' + (opts.class ? ' ' + opts.class : '') }, [
    el('p', { class: 'vb-pullquote-text' }, inlineProse(text || '')),
    opts.attribution ? el('cite', { class: 'vb-pullquote-cite' }, [opts.attribution]) : null,
  ].filter(Boolean));
}

// The verdict badge — promoted / rejected / deferred / open / running.
const VERDICT_META = {
  promoted: { glyph: '✓', label: 'Promoted', cls: 'improve' },
  rejected: { glyph: '✗', label: 'Rejected', cls: 'regress' },
  deferred: { glyph: '~', label: 'Deferred', cls: 'caution' },
  open: { glyph: '◦', label: 'Open', cls: 'neutral' },
  running: { glyph: '●', label: 'Running', cls: 'running' },
  baseline: { glyph: '◆', label: 'Baseline', cls: 'neutral' },
};
export function verdictBadge(verdict, opts = {}) {
  const m = VERDICT_META[verdict] || VERDICT_META.open;
  return el('span', {
    class: `vb-verdict vb-${m.cls}` + (opts.large ? ' vb-verdict-lg' : ''),
    role: 'status',
  }, [
    el('span', { class: 'vb-verdict-glyph', 'aria-hidden': 'true' }, [m.glyph]),
    el('span', { class: 'vb-verdict-label' }, [opts.label || m.label]),
  ]);
}

// Honest-state note. kind ∈ not_yet | running | empty | broken.
export function note(kind, opts = {}) {
  const meta = {
    not_yet: { tag: 'not yet', cls: 'neutral' },
    running: { tag: 'running', cls: 'running' },
    empty: { tag: 'nothing here', cls: 'neutral' },
    broken: { tag: 'unavailable', cls: 'regress' },
  }[kind] || { tag: '', cls: 'neutral' };
  const detail = kind === 'broken' && opts.reason ? opts.reason
    : kind === 'running' && opts.done != null && opts.total != null
      ? `${opts.done} / ${opts.total} complete` : opts.detail;
  return el('div', { class: `vb-note vb-note-${kind} vb-${meta.cls}`, role: 'status' }, [
    el('span', { class: 'vb-note-tag' }, [meta.tag]),
    el('span', { class: 'vb-note-body' }, [
      el('span', { class: 'vb-note-label' }, [opts.label || meta.tag]),
      detail ? el('span', { class: 'vb-note-detail' }, [detail]) : null,
    ].filter(Boolean)),
  ]);
}

// A small stat — a number with a label beneath, for the airy stat row.
export function stat(value, label, opts = {}) {
  return el('div', { class: 'vb-stat' + (opts.class ? ' ' + opts.class : '') }, [
    el('span', { class: 'vb-stat-value vb-' + (opts.tone || 'neutral') }, [String(value)]),
    el('span', { class: 'vb-stat-label' }, [label]),
  ]);
}
