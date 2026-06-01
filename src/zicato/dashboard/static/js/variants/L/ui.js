// variants/L/ui.js — shared chrome for Atlas III.
//
// Self-contained for Variant L. Small, pure builders + the two pickers:
//   * a COLOR theme picker (solarized-light / solarized-dark / monokai),
//   * a TYPEFACE theme picker (Sans / Editorial / Technical / Display —
//     Open-Sans-based Google-Fonts pairings).
// Both persist their choice and re-skin via data-attributes on the root, so
// the same DOM re-paints without any view rebuild.
//
// Also here: the digest-gated swap (no-flash guarantee), a GFM markdown
// renderer with real tables, verdict pills, stat tiles, honest empty/
// loading states, and the theme-aware heatmap ramp helper (the ramp colours
// are read from the ACTIVE color theme's tokens at draw time).

import { el, clearChildren } from '../../core/dom.js';
import { href } from './router.js';

// ---- color theme --------------------------------------------------------

const COLOR_KEY = 'zicato.vl.theme';
export const COLOR_THEMES = [
  ['solarized-light', 'Light'],
  ['solarized-dark', 'Dark'],
  ['monokai', 'Monokai'],
];
const COLOR_IDS = COLOR_THEMES.map((t) => t[0]);
export const DEFAULT_COLOR = 'solarized-dark'; // L defaults to clean dark

export function normaliseColor(t) { return COLOR_IDS.includes(t) ? t : DEFAULT_COLOR; }
export function readColor() {
  let stored = null;
  try { stored = window.localStorage.getItem(COLOR_KEY); } catch (e) { /* private mode */ }
  return normaliseColor(stored);
}
export function applyColor(root, theme) {
  const t = normaliseColor(theme);
  if (root) root.setAttribute('data-vl-theme', t);
  try { window.localStorage.setItem(COLOR_KEY, t); } catch (e) { /* ignore */ }
  return t;
}
export function colorSwitcher(active, onPick) {
  const cur = normaliseColor(active);
  const wrap = el('div', { class: 'vl-picker vl-color', role: 'group', 'aria-label': 'Colour theme' });
  for (const [id, label] of COLOR_THEMES) {
    const b = el('button', {
      class: 'vl-picker-btn' + (id === cur ? ' vl-active' : ''),
      type: 'button', 'data-theme': id, 'aria-pressed': id === cur ? 'true' : 'false', text: label,
    });
    b.addEventListener('click', () => onPick && onPick(id));
    wrap.appendChild(b);
  }
  return wrap;
}

// ---- typeface theme -----------------------------------------------------

const TYPE_KEY = 'zicato.vl.type';
// Each option re-maps the --l-sans / --l-serif / --l-mono / --l-display
// family tokens (the CSS keys off data-vl-type). All four are Open-Sans-
// based per the brief; the system fallbacks live in the stylesheet.
export const TYPE_THEMES = [
  ['sans', 'Sans'],
  ['editorial', 'Editorial'],
  ['technical', 'Technical'],
  ['display', 'Display'],
];
const TYPE_IDS = TYPE_THEMES.map((t) => t[0]);
export const DEFAULT_TYPE = 'sans'; // L defaults to "Sans" (Open Sans)

export function normaliseType(t) { return TYPE_IDS.includes(t) ? t : DEFAULT_TYPE; }
export function readType() {
  let stored = null;
  try { stored = window.localStorage.getItem(TYPE_KEY); } catch (e) { /* private mode */ }
  return normaliseType(stored);
}
export function applyType(root, type) {
  const t = normaliseType(type);
  if (root) root.setAttribute('data-vl-type', t);
  try { window.localStorage.setItem(TYPE_KEY, t); } catch (e) { /* ignore */ }
  return t;
}
export function typeSwitcher(active, onPick) {
  const cur = normaliseType(active);
  const wrap = el('div', { class: 'vl-picker vl-type', role: 'group', 'aria-label': 'Typeface' });
  for (const [id, label] of TYPE_THEMES) {
    const b = el('button', {
      class: 'vl-picker-btn' + (id === cur ? ' vl-active' : ''),
      type: 'button', 'data-type': id, 'aria-pressed': id === cur ? 'true' : 'false', text: label,
    });
    b.addEventListener('click', () => onPick && onPick(id));
    wrap.appendChild(b);
  }
  return wrap;
}

// ---- theme-aware heatmap ramp ------------------------------------------
//
// The drift heatmap's colour ramp MUST derive from the ACTIVE color theme's
// tokens at draw time (no fixed orange/brown — legible in all three themes).
// We read the --l-heat-lo / --l-heat-hi custom properties off the variant
// root via getComputedStyle. In a non-browser (the test harness) there is no
// getComputedStyle, so we fall back to a per-theme lookup keyed by the
// root's data-vl-theme attribute, then to a safe neutral pair. Either way
// the returned ramp is theme-keyed, never a hard-coded constant.
const RAMP_BY_THEME = {
  'solarized-light': ['#dfe9e6', '#b1372c'],
  'solarized-dark': ['#0a3b47', '#e8736a'],
  monokai: ['#3a3b32', '#f92672'],
};
export function themeRamp(root) {
  const node = root || (typeof document !== 'undefined' ? document.getElementById('variant-root') : null);
  // 1) live CSS custom properties (the source of truth in a browser).
  try {
    if (typeof getComputedStyle === 'function' && node) {
      const cs = getComputedStyle(node);
      const lo = (cs.getPropertyValue('--l-heat-lo') || '').trim();
      const hi = (cs.getPropertyValue('--l-heat-hi') || '').trim();
      if (lo && hi) return [lo, hi];
    }
  } catch (e) { /* fall through */ }
  // 2) data-attribute-keyed fallback (the harness path) — still theme-aware.
  const theme = node && node.getAttribute ? node.getAttribute('data-vl-theme') : null;
  return RAMP_BY_THEME[theme] || RAMP_BY_THEME['solarized-dark'];
}

// ---- digest-gated content swap (the no-flash guarantee) ----------------

export function gatedSwap(host, digest, build) {
  if (!host) return false;
  const next = String(digest);
  if (host.getAttribute('data-vl-digest') === next && host.firstChild) return false;
  clearChildren(host);
  const built = build();
  const nodes = Array.isArray(built) ? built : [built];
  for (const n of nodes) { if (n) host.appendChild(n); }
  host.setAttribute('data-vl-digest', next);
  return true;
}

export function section(titleText, ...children) {
  return el('section', { class: 'vl-section' }, [
    el('h2', { class: 'vl-h2', text: titleText }),
    ...children.filter(Boolean),
  ]);
}

export function empty(text) { return el('p', { class: 'vl-empty', text: text || 'No data yet.' }); }
export function loading(text) { return el('p', { class: 'vl-empty', text: text || 'Loading…' }); }

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
  return el('span', { class: `vl-pill vl-${d}`, text: label });
}

export function stat(value, key) {
  return el('div', { class: 'vl-stat' }, [
    el('span', { class: 'vl-stat-v', text: value }),
    el('span', { class: 'vl-stat-k', text: key }),
  ]);
}

// A clearly-themed link/button (never an unstyled anchor — the E bug).
export function linkButton(label, hrefStr, onClick) {
  const a = el('a', { class: 'vl-linkbtn', href: hrefStr || '#', text: label });
  if (onClick) a.addEventListener('click', (ev) => { ev.preventDefault(); onClick(ev); });
  return a;
}

// A figure caption + an optional "drill into the live view" affordance.
export function figureFrame(opts) {
  const o = opts || {};
  const fig = el('figure', { class: 'vl-figure' + (o.onOpen ? ' vl-figure-live' : '') });
  if (o.mark) fig.appendChild(el('div', { class: 'vl-figure-mark' }, [o.mark]));
  const cap = el('figcaption', { class: 'vl-figcaption' }, [
    o.number != null ? el('span', { class: 'vl-fig-num', text: `Figure ${o.number}. ` }) : null,
    o.caption ? el('span', { text: o.caption }) : null,
    o.openLabel ? linkButton(o.openLabel, o.openHref, o.onOpen) : null,
  ].filter(Boolean));
  fig.appendChild(cap);
  if (o.onOpen && o.mark && o.mark.addEventListener) {
    fig.style.cursor = 'pointer';
    o.mark.addEventListener('click', (ev) => {
      const t = ev.target;
      const fine = t && t.getAttribute && (t.getAttribute('data-vl') || (t.parentNode && t.parentNode.getAttribute && t.parentNode.getAttribute('data-vl')));
      if (!fine) o.onOpen(ev);
    });
  }
  return fig;
}

// ---- GFM markdown → DOM (proposer brief + publication) -----------------
//
// A SAFE subset rendered to DOM nodes (never innerHTML): headings,
// paragraphs, ordered/unordered lists, blockquotes, fenced code, inline
// code / bold / italic / links, AND GFM tables (the I-variant bug was a
// table rendering as raw `| … |`). A `<!-- FIGURE:name -->` marker invokes
// opts.onFigure(name) so the publication can splice a live figure in place.
export function renderMarkdown(md, opts) {
  const o = opts || {};
  const root = el('div', { class: 'vl-md' });
  const text = String(md || '').replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  let i = 0;
  let para = [];
  let list = null; let listOrdered = false;

  const flushPara = () => {
    if (para.length) { root.appendChild(el('p', { class: 'vl-md-p' }, inline(para.join(' ')))); para = []; }
  };
  const flushList = () => { if (list) { root.appendChild(list); list = null; } };

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    const fig = /^<!--\s*FIGURE:([a-zA-Z0-9_-]+)\s*-->$/.exec(trimmed);
    if (fig) {
      flushPara(); flushList();
      if (o.onFigure) { const node = o.onFigure(fig[1]); if (node) root.appendChild(node); }
      i++; continue;
    }
    if (/^<!--.*-->$/.test(trimmed)) { i++; continue; }

    if (/^```/.test(trimmed)) {
      flushPara(); flushList();
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) { buf.push(lines[i]); i++; }
      i++;
      root.appendChild(el('pre', { class: 'vl-md-pre' }, [el('code', { text: buf.join('\n') })]));
      continue;
    }
    // GFM table: a header row `| … |` followed by a `| --- | --- |` rule.
    if (trimmed.startsWith('|') && i + 1 < lines.length
      && /^\|?[\s:|-]+\|?$/.test(lines[i + 1].trim()) && lines[i + 1].includes('-')) {
      flushPara(); flushList();
      const header = splitRow(trimmed);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith('|')) { rows.push(splitRow(lines[i].trim())); i++; }
      root.appendChild(table(header, rows));
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushPara(); flushList();
      const lvl = Math.min(6, heading[1].length);
      root.appendChild(el('h' + Math.max(3, lvl), { class: 'vl-md-h vl-md-h' + lvl }, inline(heading[2])));
      i++; continue;
    }
    if (/^>\s?/.test(line)) {
      flushPara(); flushList();
      root.appendChild(el('blockquote', { class: 'vl-md-quote' }, inline(line.replace(/^>\s?/, ''))));
      i++; continue;
    }
    const ordered = /^\s*\d+\.\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (ordered || bullet) {
      flushPara();
      const wantOrdered = !!ordered;
      if (!list || listOrdered !== wantOrdered) { flushList(); list = el(wantOrdered ? 'ol' : 'ul', { class: 'vl-md-list' }); listOrdered = wantOrdered; }
      list.appendChild(el('li', null, inline((ordered || bullet)[1])));
      i++; continue;
    }
    if (trimmed === '') { flushPara(); flushList(); i++; continue; }
    flushList();
    para.push(trimmed);
    i++;
  }
  flushPara(); flushList();
  if (!root.firstChild) root.appendChild(el('p', { class: 'vl-faint', text: '(empty section)' }));
  return root;
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

function table(header, rows) {
  const t = el('table', { class: 'vl-md-table' });
  const thead = el('thead', null, [el('tr', null, header.map((c) => el('th', null, inline(c))))]);
  const tbody = el('tbody', null, rows.map((r) => el('tr', null, r.map((c) => el('td', null, inline(c))))));
  t.appendChild(thead); t.appendChild(tbody);
  return t;
}

function inline(s) {
  const out = [];
  const str = String(s == null ? '' : s);
  const re = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/g;
  let last = 0; let m;
  while ((m = re.exec(str)) !== null) {
    if (m.index > last) out.push(str.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith('`')) out.push(el('code', { class: 'vl-md-code', text: tok.slice(1, -1) }));
    else if (tok.startsWith('**')) out.push(el('strong', { text: tok.slice(2, -2) }));
    else if (tok.startsWith('*')) out.push(el('em', { text: tok.slice(1, -1) }));
    else {
      const lm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      if (lm) out.push(el('a', { class: 'vl-md-link', href: lm[2], text: lm[1] }));
      else out.push(tok);
    }
    last = m.index + tok.length;
  }
  if (last < str.length) out.push(str.slice(last));
  return out.length ? out : [str];
}
