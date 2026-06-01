// variants/J/ui.js — shared chrome helpers for the Console variant.
//
// Self-contained for Variant J. Small, pure builders every view composes:
// the digest-gated content swap (the no-flash guarantee), section headers,
// verdict pills, a defensive tiny-markdown renderer, and honest empty /
// loading states. No data fetching, no state mutation.

import { el, clearChildren } from '../../core/dom.js';
import { href } from './router.js';

// ---- digest-gated content swap (the no-flash guarantee) -------------
//
// A view computes a stable digest of ONLY its structural/content data
// (timestamps / heartbeat fields EXCLUDED), then calls gatedSwap(host,
// digest, build). If the digest equals the one this host last painted AND
// the host still has children, NOTHING is written — so a steady heartbeat
// re-dispatch is a true no-op and the screen cannot flash. Only when the
// digest changes does the host get cleared and rebuilt once from build().
export function gatedSwap(host, digest, build) {
  if (!host) return false;
  const next = String(digest);
  if (host.getAttribute('data-j-digest') === next && host.firstChild) return false;
  clearChildren(host);
  const built = build();
  const nodes = Array.isArray(built) ? built : [built];
  for (const n of nodes) { if (n) host.appendChild(n); }
  host.setAttribute('data-j-digest', next);
  return true;
}

export function section(titleText, ...children) {
  return el('section', { class: 'dj-section' }, [
    el('h2', { class: 'dj-h2', text: titleText }),
    ...children.filter(Boolean),
  ]);
}

export function empty(text) {
  return el('p', { class: 'dj-empty', text: text || 'No data yet.' });
}

export function loading(text) {
  return el('p', { class: 'dj-empty', text: text || 'Loading…' });
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
  return el('span', { class: `dj-pill dj-${d}`, text: label });
}

export function stat(value, key) {
  return el('div', { class: 'dj-stat' }, [
    el('span', { class: 'v', text: value }),
    el('span', { class: 'k', text: key }),
  ]);
}

// A themed link/button (the E bug fix: the "open full transcript" link must
// be a properly styled themed control, never an unstyled anchor).
export function linkButton(text, hrefStr, attrs) {
  return el('a', Object.assign({ class: 'dj-linkbtn', href: hrefStr }, attrs || {}), [text]);
}

// ---- tiny markdown → DOM -------------------------------------------
//
// Renders a SAFE subset to DOM nodes — headings, paragraphs, bullet lists,
// inline code/bold, fenced code — without ever using innerHTML on untrusted
// text. Anything unrecognised is emitted as a paragraph.
export function renderMarkdown(md) {
  const root = el('div', { class: 'dj-md-body' });
  const text = String(md || '').replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  let i = 0;
  let para = [];
  let list = null;

  const flushPara = () => { if (para.length) { root.appendChild(el('p', null, inline(para.join(' ')))); para = []; } };
  const flushList = () => { if (list) { root.appendChild(list); list = null; } };

  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line.trim())) {
      flushPara(); flushList();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) { buf.push(lines[i]); i++; }
      i++;
      root.appendChild(el('pre', null, [el('code', { text: buf.join('\n') })]));
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushPara(); flushList();
      const lvl = Math.min(4, heading[1].length);
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
    root.appendChild(el('p', { class: 'dj-faint', text: 'Nothing to render.' }));
  }
  return root;
}

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

export { href };
