// variants/I/ui.js — shared editorial chrome helpers for the Ledger variant.
//
// Self-contained for Variant I. Small, pure builders every view composes:
// the digest-gated content swap (the no-flash guarantee), section headers,
// the breadcrumb, verdict pills, an honest empty/loading state, a tiny safe
// markdown renderer for the proposer brief, AND the editorial primitives
// that give Ledger its publication voice — an eyebrow, a lede paragraph, a
// figure caption, and a pull-quote (for the hypothesis + rejection reason).
// No data fetching, no state mutation.

import { el, clearChildren } from '../../core/dom.js';
import { href } from './router.js';

// ---- digest-gated content swap (the no-flash guarantee) -------------
//
// A view computes a stable digest of ONLY its structural/content data
// (timestamps / heartbeat fields EXCLUDED), then calls gatedSwap(host,
// digest, build). If the digest equals the one this host last painted AND
// the host still has children, NOTHING is written — so a steady heartbeat
// tick that re-dispatches the active view is a true no-op and the screen
// cannot flash. Only when the digest changes is the host cleared and
// rebuilt once from `build()` (a node or array of nodes). The digest is
// stored on `data-i-digest`, so the gate survives without module state.
export function gatedSwap(host, digest, build) {
  if (!host) return false;
  const next = String(digest);
  if (host.getAttribute('data-i-digest') === next && host.firstChild) return false;
  clearChildren(host);
  const built = build();
  const nodes = Array.isArray(built) ? built : [built];
  for (const n of nodes) { if (n) host.appendChild(n); }
  host.setAttribute('data-i-digest', next);
  return true;
}

export function section(titleText, ...children) {
  return el('section', { class: 'd-section' }, [
    el('h2', { class: 'd-h2', text: titleText }),
    ...children.filter(Boolean),
  ]);
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

// ---- editorial primitives (Ledger's publication voice) -------------

// A small-caps eyebrow line that sits above a page title.
export function eyebrow(text) {
  return el('div', { class: 'i-eyebrow', text: String(text || '') });
}

// A page head: eyebrow + serif title + lede. The lede is the report-style
// framing sentence the round-3 brief asks for "throughout the dashboard".
export function pageHead(eyebrowText, title, ledeText) {
  return el('div', { class: 'i-pagehead' }, [
    eyebrowText ? eyebrow(eyebrowText) : null,
    el('h1', { class: 'i-title', text: String(title || '') }),
    ledeText ? el('p', { class: 'i-lede' }, Array.isArray(ledeText) ? ledeText : [String(ledeText)]) : null,
  ].filter(Boolean));
}

// A figure caption — "Figure N. <text>" in the editorial caption voice,
// used beneath every live Tufte figure.
export function figure(mark, captionText, opts = {}) {
  const o = opts || {};
  return el('figure', { class: 'i-figure' + (o.class ? ' ' + o.class : '') }, [
    mark,
    captionText ? el('figcaption', { class: 'i-figcap' }, [
      o.label ? el('span', { class: 'i-figcap-label', text: o.label + ' ' }) : null,
      el('span', { text: String(captionText) }),
    ].filter(Boolean)) : null,
  ].filter(Boolean));
}

// The pull-quote — a hypothesis bet or a rejection reason set large, as a
// magazine pull-quote with an optional attribution caption.
export function pullQuote(text, opts = {}) {
  const o = opts || {};
  return el('blockquote', { class: 'i-pullquote' + (o.class ? ' ' + o.class : '') }, [
    el('p', { class: 'i-pullquote-text', text: String(text || '') }),
    o.attribution ? el('cite', { class: 'i-pullquote-cite', text: String(o.attribution) }) : null,
  ].filter(Boolean));
}

// ---- tiny markdown → DOM (proposer brief; SAFE subset) -------------
//
// Headings, paragraphs, bullet lists, inline code, fenced code — never
// innerHTML on untrusted text. Anything unrecognised is a paragraph, so
// the brief is always readable, never raw and never an injection.
export function renderMarkdown(md) {
  const root = el('div', { class: 'i-brief-body' });
  const text = String(md || '').replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  let i = 0;
  let para = [];
  let list = null;

  const flushPara = () => {
    if (para.length) { root.appendChild(el('p', null, inline(para.join(' ')))); para = []; }
  };
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
  if (!root.childNodes || root.childNodes.length === 0) {
    root.appendChild(el('p', { class: 'd-faint', text: 'No brief recorded for this epoch.' }));
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
