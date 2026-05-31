// variants/D/ui.js — shared chrome helpers for the Tufte variant.
//
// Small, pure builders that every view composes: section headers, the
// breadcrumb, verdict pills, a defensive tiny-markdown renderer for the
// proposer brief, and honest empty / loading states. No data fetching,
// no state mutation.

import { el } from '../../core/dom.js';
import { href } from './router.js';

export function section(titleText, ...children) {
  return el('section', { class: 'd-section' }, [
    el('h2', { class: 'd-h2', text: titleText }),
    ...children.filter(Boolean),
  ]);
}

export function crumb(trail) {
  // trail: [{ label, view, params }] — last entry rendered plain.
  const parts = [];
  trail.forEach((t, i) => {
    if (i > 0) parts.push(el('span', { class: 'sep', text: '›' }));
    if (i === trail.length - 1 || !t.view) {
      parts.push(el('span', { text: t.label }));
    } else {
      parts.push(el('a', { href: href(t.view, t.params), text: t.label }));
    }
  });
  return el('nav', { class: 'd-crumb' }, parts);
}

export function empty(text) {
  return el('p', { class: 'd-empty', text: text || 'No data yet.' });
}

export function loading(text) {
  return el('p', { class: 'd-empty', text: text || 'Loading…' });
}

export function normaliseDecision(outcome) {
  if (!outcome || typeof outcome !== 'object') return null;
  const raw = String(outcome.tournament_decision || outcome.decision || '').toLowerCase();
  if (raw.includes('promot')) return 'promoted';
  if (raw.includes('reject')) return 'rejected';
  if (raw.includes('defer')) return 'deferred';
  return raw || null;
}

export function verdictPill(decision) {
  const d = decision || 'baseline';
  const label = d === 'baseline' ? 'seed (v0)' : d;
  return el('span', { class: `d-pill d-${d}`, text: label });
}

export function stat(value, key) {
  return el('div', { class: 'd-stat' }, [
    el('span', { class: 'v', text: value }),
    el('span', { class: 'k', text: key }),
  ]);
}

// ---- tiny markdown → DOM (proposer brief) --------------------------
//
// The proposer brief is long markdown (Goal / context / constraints).
// We render a SAFE subset to DOM nodes — headings, paragraphs, bullet
// lists, inline code, fenced code — without ever using innerHTML on
// untrusted text. Anything unrecognised is emitted as a paragraph, so
// the brief is always readable, never raw and never an injection.
export function renderMarkdown(md) {
  const root = el('div', { class: 'd-brief-body' });
  const text = String(md || '').replace(/\r\n/g, '\n');
  const lines = text.split('\n');
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
    // fenced code block
    if (/^```/.test(line.trim())) {
      flushPara(); flushList();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) { buf.push(lines[i]); i++; }
      i++; // closing fence
      root.appendChild(el('pre', null, [el('code', { text: buf.join('\n') })]));
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      flushPara(); flushList();
      const lvl = Math.min(3, heading[1].length);
      root.appendChild(el('h' + lvl, null, inline(heading[2])));
      i++; continue;
    }
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      flushPara();
      if (!list) list = el('ul');
      list.appendChild(el('li', null, inline(bullet[1])));
      i++; continue;
    }
    if (line.trim() === '') { flushPara(); flushList(); i++; continue; }
    flushList();
    para.push(line.trim());
    i++;
  }
  flushPara(); flushList();
  if (!root.childNodes || (root.childNodes && root.childNodes.length === 0)) {
    root.appendChild(el('p', { class: 'd-faint', text: 'No brief recorded for this epoch.' }));
  }
  return root;
}

// Inline parse: `code` spans + **bold**, returning an array of nodes.
function inline(s) {
  const out = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  let last = 0; let m;
  while ((m = re.exec(s)) !== null) {
    if (m.index > last) out.push(s.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith('`')) out.push(el('code', { text: tok.slice(1, -1) }));
    else out.push(el('strong', { text: tok.slice(2, -2) }));
    last = m.index + tok.length;
  }
  if (last < s.length) out.push(s.slice(last));
  return out.length ? out : [s];
}
