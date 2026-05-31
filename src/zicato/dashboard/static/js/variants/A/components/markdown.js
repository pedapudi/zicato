// variants/A/components/markdown.js — tiny, safe markdown -> DOM.
//
// The proposer brief is authored markdown and can be LONG. It deserves
// a real, readable home — not a truncated line. This renders a useful
// subset (headings, paragraphs, lists, fenced code, inline code, bold,
// italic) into DOM nodes WITHOUT innerHTML, so it cannot inject markup.

import { el } from '../../../core/dom.js';

function inline(text) {
  // Returns an array of DOM nodes/strings for one line, handling
  // `code`, **bold**, *italic*. Order matters; code first so its
  // contents are not re-parsed.
  const nodes = [];
  let rest = String(text);
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)/;
  let m;
  while ((m = rest.match(re))) {
    const before = rest.slice(0, m.index);
    if (before) nodes.push(before);
    const tok = m[0];
    if (tok.startsWith('`')) nodes.push(el('code', null, [tok.slice(1, -1)]));
    else if (tok.startsWith('**')) nodes.push(el('strong', null, [tok.slice(2, -2)]));
    else nodes.push(el('em', null, [tok.slice(1, -1)]));
    rest = rest.slice(m.index + tok.length);
  }
  if (rest) nodes.push(rest);
  return nodes;
}

export function renderMarkdown(md) {
  const root = el('div', { class: 'mcA-brief-prose' });
  const lines = String(md || '').replace(/\r\n/g, '\n').split('\n');
  let i = 0;
  let para = [];
  let list = null;

  const flushPara = () => {
    if (para.length) {
      root.appendChild(el('p', null, inline(para.join(' '))));
      para = [];
    }
  };
  const flushList = () => {
    if (list) { root.appendChild(list); list = null; }
  };

  while (i < lines.length) {
    const line = lines[i];
    // fenced code
    if (/^```/.test(line)) {
      flushPara(); flushList();
      const buf = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1;
      root.appendChild(el('pre', null, [el('code', { class: 'mono' }, [buf.join('\n')])]));
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushPara(); flushList();
      const lvl = Math.min(h[1].length, 3);
      root.appendChild(el('h' + (lvl + 1), null, inline(h[2])));
      i += 1;
      continue;
    }
    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) {
      flushPara();
      if (!list) list = el('ul');
      list.appendChild(el('li', null, inline(li[1])));
      i += 1;
      continue;
    }
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ol) {
      flushPara();
      if (!list || list.tagName !== 'OL') { flushList(); list = el('ol'); }
      list.appendChild(el('li', null, inline(ol[1])));
      i += 1;
      continue;
    }
    if (line.trim() === '') { flushPara(); flushList(); i += 1; continue; }
    para.push(line.trim());
    i += 1;
  }
  flushPara(); flushList();
  if (root.childNodes.length === 0) {
    return el('div', { class: 'mcA-brief-empty' }, ['(empty)']);
  }
  return root;
}
