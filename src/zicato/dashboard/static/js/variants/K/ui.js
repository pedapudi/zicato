// variants/K/ui.js — shared chrome for the Monograph variant.

import { el, clearChildren } from '../../core/dom.js';

const THEME_KEY = 'zicato.vk.theme';
export const THEMES = [
  ['solarized-light', 'Light'],
  ['solarized-dark', 'Dark'],
  ['monokai', 'Monokai'],
];
const THEME_IDS = THEMES.map((t) => t[0]);
export const DEFAULT_THEME = 'solarized-light';

export function gatedSwap(host, digest, build) {
  if (!host) return false;
  const next = String(digest);
  if (host.getAttribute('data-vk-digest') === next && host.firstChild) return false;
  clearChildren(host);
  const built = build();
  const nodes = Array.isArray(built) ? built : [built];
  for (const n of nodes) { if (n) host.appendChild(n); }
  host.setAttribute('data-vk-digest', next);
  return true;
}


export function normaliseTheme(t) {
  return THEME_IDS.includes(t) ? t : DEFAULT_THEME;
}

export function readTheme() {
  let stored = null;
  try { stored = window.localStorage.getItem(THEME_KEY); } catch (e) { /* private mode */ }
  return normaliseTheme(stored);
}

export function applyTheme(root, theme) {
  const t = normaliseTheme(theme);
  if (root) root.setAttribute('data-vk-theme', t);
  try { window.localStorage.setItem(THEME_KEY, t); } catch (e) { /* ignore */ }
  return t;
}

// A small inline switcher; onPick(theme) lets the shell re-apply + re-stamp
// the active button. Pure DOM, no global side effects.
export function themeSwitcher(active, onPick) {
  const cur = normaliseTheme(active);
  const wrap = el('div', { class: 'vk-theme', role: 'group', 'aria-label': 'Theme' });
  for (const [id, label] of THEMES) {
    const b = el('button', {
      class: 'vk-theme-btn' + (id === cur ? ' vk-active' : ''),
      type: 'button', 'data-theme': id, 'aria-pressed': id === cur ? 'true' : 'false', text: label,
    });
    b.addEventListener('click', () => onPick && onPick(id));
    wrap.appendChild(b);
  }
  return wrap;
}


export function section(titleText, ...children) {
  return el('section', { class: 'vk-section' }, [
    el('h2', { class: 'vk-h2', text: titleText }),
    ...children.filter(Boolean),
  ]);
}

export function empty(text) {
  return el('p', { class: 'vk-empty', text: text || 'No data yet.' });
}

export function loading(text) {
  return el('p', { class: 'vk-empty', text: text || 'Loading…' });
}

export function stat(value, key) {
  return el('div', { class: 'vk-stat' }, [
    el('span', { class: 'vk-stat-v', text: value }),
    el('span', { class: 'vk-stat-k', text: key }),
  ]);
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
  return el('span', { class: `vk-pill vk-${d}`, text: label });
}

// A clearly-themed button/link (the E bug: an unstyled "open transcript"
// anchor). Use for figure → live-view drill links + "open full transcript".
export function linkButton(label, hrefStr, onClick) {
  const a = el('a', { class: 'vk-linkbtn', href: hrefStr || '#', text: label });
  if (onClick) a.addEventListener('click', (ev) => { ev.preventDefault(); onClick(ev); });
  return a;
}

// A figure caption + a "drill into the live view" affordance, so the paper's
export function figureFrame(opts) {
  const o = opts || {};
  const fig = el('figure', { class: 'vk-figure' + (o.onOpen ? ' vk-figure-live' : '') });
  if (o.mark) fig.appendChild(el('div', { class: 'vk-figure-mark' }, [o.mark]));
  const cap = el('figcaption', { class: 'vk-figcaption' }, [
    o.number != null ? el('span', { class: 'vk-fig-num', text: `Figure ${o.number}. ` }) : null,
    o.caption ? el('span', { text: o.caption }) : null,
    o.openLabel ? linkButton(o.openLabel, o.openHref, o.onOpen) : null,
  ].filter(Boolean));
  fig.appendChild(cap);
  if (o.onOpen) {
    fig.style.cursor = 'pointer';
    // The whole figure surface drills in (the mark's own marks may also
    if (o.mark) o.mark.addEventListener && o.mark.addEventListener('click', (ev) => {
      // Only fire the figure-level open when the click didn't land on a
      // finer interactive mark (those carry their own data-vk handlers).
      const t = ev.target;
      const fine = t && t.getAttribute && (t.getAttribute('data-vk') || (t.parentNode && t.parentNode.getAttribute && t.parentNode.getAttribute('data-vk')));
      if (!fine) o.onOpen(ev);
    });
  }
  return fig;
}

export function renderMarkdown(md, opts) {
  const o = opts || {};
  const root = el('div', { class: 'vk-md' });
  const text = String(md || '').replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  let i = 0;
  let para = [];
  let list = null; let listOrdered = false;

  const flushPara = () => {
    if (para.length) { root.appendChild(el('p', { class: 'vk-md-p' }, inline(para.join(' ')))); para = []; }
  };
  const flushList = () => { if (list) { root.appendChild(list); list = null; } };

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // figure placeholder marker → callback (splice a live figure here).
    const fig = /^<!--\s*FIGURE:([a-zA-Z0-9_-]+)\s*-->$/.exec(trimmed);
    if (fig) {
      flushPara(); flushList();
      if (o.onFigure) { const node = o.onFigure(fig[1]); if (node) root.appendChild(node); }
      i++; continue;
    }
    // skip any other HTML-comment markers in body fragments.
    if (/^<!--.*-->$/.test(trimmed)) { i++; continue; }

    // fenced code
    if (/^```/.test(trimmed)) {
      flushPara(); flushList();
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) { buf.push(lines[i]); i++; }
      i++;
      root.appendChild(el('pre', { class: 'vk-md-pre' }, [el('code', { text: buf.join('\n') })]));
      continue;
    }
    // markdown table (header row | --- | rows)
    if (trimmed.startsWith('|') && i + 1 < lines.length && /^\|?[\s:|-]+\|?$/.test(lines[i + 1].trim()) && lines[i + 1].includes('-')) {
      flushPara(); flushList();
      const rows = [];
      const header = splitRow(trimmed);
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith('|')) { rows.push(splitRow(lines[i].trim())); i++; }
      root.appendChild(table(header, rows));
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushPara(); flushList();
      const lvl = Math.min(6, heading[1].length);
      root.appendChild(el('h' + Math.max(3, lvl), { class: 'vk-md-h vk-md-h' + lvl }, inline(heading[2])));
      i++; continue;
    }
    if (/^>\s?/.test(line)) {
      flushPara(); flushList();
      root.appendChild(el('blockquote', { class: 'vk-md-quote' }, inline(line.replace(/^>\s?/, ''))));
      i++; continue;
    }
    const ordered = /^\s*\d+\.\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (ordered || bullet) {
      flushPara();
      const wantOrdered = !!ordered;
      if (!list || listOrdered !== wantOrdered) { flushList(); list = el(wantOrdered ? 'ol' : 'ul', { class: 'vk-md-list' }); listOrdered = wantOrdered; }
      list.appendChild(el('li', null, inline((ordered || bullet)[1])));
      i++; continue;
    }
    if (trimmed === '') { flushPara(); flushList(); i++; continue; }
    flushList();
    para.push(trimmed);
    i++;
  }
  flushPara(); flushList();
  if (!root.firstChild) root.appendChild(el('p', { class: 'vk-faint', text: '(empty section)' }));
  return root;
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

function table(header, rows) {
  const t = el('table', { class: 'vk-md-table' });
  const thead = el('thead', null, [el('tr', null, header.map((c) => el('th', null, inline(c))))]);
  const tbody = el('tbody', null, rows.map((r) => el('tr', null, r.map((c) => el('td', null, inline(c))))));
  t.appendChild(thead); t.appendChild(tbody);
  return t;
}

// Inline parse: `code`, **bold**, *italic*, [link](href). Returns nodes.
function inline(s) {
  const out = [];
  const str = String(s == null ? '' : s);
  const re = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/g;
  let last = 0; let m;
  while ((m = re.exec(str)) !== null) {
    if (m.index > last) out.push(str.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith('`')) out.push(el('code', { class: 'vk-md-code', text: tok.slice(1, -1) }));
    else if (tok.startsWith('**')) out.push(el('strong', { text: tok.slice(2, -2) }));
    else if (tok.startsWith('*')) out.push(el('em', { text: tok.slice(1, -1) }));
    else {
      const lm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      if (lm) out.push(el('a', { class: 'vk-md-link', href: lm[2], text: lm[1] }));
      else out.push(tok);
    }
    last = m.index + tok.length;
  }
  if (last < str.length) out.push(str.slice(last));
  return out.length ? out : [str];
}
