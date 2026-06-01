// variants/M/ui.js — shared editorial chrome helpers for Ledger II.
//
// Self-contained for Variant M. Small, pure builders every view composes:
// the digest-gated content swap (the no-flash guarantee), section headers,
// verdict pills, honest empty/loading states, the editorial primitives that
// give Ledger II its publication voice (eyebrow / lede / figure caption /
// pull-quote), a safe markdown renderer for the proposer brief, AND a SIDE-
// BY-SIDE line diff used by the mutation view. No data fetching, no state
// mutation.

import { el, clearChildren } from '../../core/dom.js';

// ---- digest-gated content swap (the no-flash guarantee) -------------
//
// A view computes a stable digest of ONLY its structural/content data, then
// calls gatedSwap(host, digest, build). If the digest equals the one this
// host last painted AND the host still has children, NOTHING is written — so
// a steady heartbeat tick that re-dispatches the active view is a true no-op
// and the screen cannot flash. The digest lives on `data-m-digest`.
export function gatedSwap(host, digest, build) {
  if (!host) return false;
  const next = String(digest);
  if (host.getAttribute('data-m-digest') === next && host.firstChild) return false;
  clearChildren(host);
  const built = build();
  const nodes = Array.isArray(built) ? built : [built];
  for (const n of nodes) { if (n) host.appendChild(n); }
  host.setAttribute('data-m-digest', next);
  return true;
}

export function section(titleText, ...children) {
  return el('section', { class: 'd-section' }, [
    el('h2', { class: 'd-h2', text: titleText }),
    ...children.filter(Boolean),
  ]);
}

export function empty(text) { return el('p', { class: 'd-empty', text: text || 'No data yet.' }); }
export function loading(text) { return el('p', { class: 'd-empty', text: text || 'Loading…' }); }

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

// ---- editorial primitives (Ledger II's publication voice) ----------

export function eyebrow(text) { return el('div', { class: 'm-eyebrow', text: String(text || '') }); }

export function pageHead(eyebrowText, title, ledeText) {
  return el('div', { class: 'm-pagehead' }, [
    eyebrowText ? eyebrow(eyebrowText) : null,
    el('h1', { class: 'm-title', text: String(title || '') }),
    ledeText ? el('p', { class: 'm-lede' }, Array.isArray(ledeText) ? ledeText : [String(ledeText)]) : null,
  ].filter(Boolean));
}

export function figure(mark, captionText, opts = {}) {
  const o = opts || {};
  return el('figure', { class: 'm-figure' + (o.class ? ' ' + o.class : '') }, [
    mark,
    captionText ? el('figcaption', { class: 'm-figcap' }, [
      o.label ? el('span', { class: 'm-figcap-label', text: o.label + ' ' }) : null,
      el('span', { text: String(captionText) }),
    ].filter(Boolean)) : null,
  ].filter(Boolean));
}

export function pullQuote(text, opts = {}) {
  const o = opts || {};
  return el('blockquote', { class: 'm-pullquote' + (o.class ? ' ' + o.class : '') }, [
    el('p', { class: 'm-pullquote-text', text: String(text || '') }),
    o.attribution ? el('cite', { class: 'm-pullquote-cite', text: String(o.attribution) }) : null,
  ].filter(Boolean));
}

// ---- side-by-side line diff (the mutation view's signature) ---------
//
// Two columns: baseline (champion) | new (challenger), line-diffed by a small
// LCS so unchanged/added/removed lines line up. Both inputs MUST be strings
// (the convergence-II contract — the "[object Object]" bug was rendering the
// baseline OBJECT instead of `.baseline.content`). A non-string input is
// treated as empty so the diff degrades honestly rather than printing
// "[object Object]".
//
// opts: { baseline (string), challenger (string), leftLabel, rightLabel }
export function sideBySideDiff(opts) {
  const o = opts || {};
  const leftStr = typeof o.baseline === 'string' ? o.baseline : '';
  const rightStr = typeof o.challenger === 'string' ? o.challenger : '';
  const left = leftStr.replace(/\r\n/g, '\n').split('\n');
  const right = rightStr.replace(/\r\n/g, '\n').split('\n');
  const rows = lcsDiff(left, right);

  const wrap = el('div', { class: 'm-sbs' });
  wrap.appendChild(el('div', { class: 'm-sbs-head' }, [
    el('div', { class: 'm-sbs-colhead m-sbs-base', text: o.leftLabel || 'champion baseline' }),
    el('div', { class: 'm-sbs-colhead m-sbs-new', text: o.rightLabel || 'challenger new' }),
  ]));
  const body = el('div', { class: 'm-sbs-body' });
  for (const r of rows) {
    body.appendChild(el('div', { class: 'm-sbs-row' }, [
      el('pre', { class: 'm-sbs-cell m-sbs-base m-sbs-' + r.leftKind }, [
        el('code', { text: r.left == null ? '' : r.left }),
      ]),
      el('pre', { class: 'm-sbs-cell m-sbs-new m-sbs-' + r.rightKind }, [
        el('code', { text: r.right == null ? '' : r.right }),
      ]),
    ]));
  }
  wrap.appendChild(body);
  return wrap;
}

// A tiny LCS line aligner → rows of { left, right, leftKind, rightKind }.
// kind ∈ same | removed | added | empty.
function lcsDiff(a, b) {
  const n = a.length; const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0; let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ left: a[i], right: b[j], leftKind: 'same', rightKind: 'same' });
      i++; j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ left: a[i], right: null, leftKind: 'removed', rightKind: 'empty' });
      i++;
    } else {
      rows.push({ left: null, right: b[j], leftKind: 'empty', rightKind: 'added' });
      j++;
    }
  }
  while (i < n) { rows.push({ left: a[i], right: null, leftKind: 'removed', rightKind: 'empty' }); i++; }
  while (j < m) { rows.push({ left: null, right: b[j], leftKind: 'empty', rightKind: 'added' }); j++; }
  if (rows.length === 0) rows.push({ left: '', right: '', leftKind: 'same', rightKind: 'same' });
  return rows;
}

// ---- tiny markdown → DOM (proposer brief; SAFE subset) -------------
//
// Headings, paragraphs, bullet lists, inline code/bold — never innerHTML on
// untrusted text. (The PUBLICATION uses K's richer renderer; this is just for
// the inline brief.)
export function renderMarkdown(md) {
  const root = el('div', { class: 'm-brief-body' });
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
      const buf = []; i++;
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
