// variants/O/ui.js — shared chrome + builders for the Compass variant.
//
// Self-contained for Variant O. Carries: the digest-gated swap (no-flash
// guarantee), the COLOR theme picker (3 themes) + the NEW TYPEFACE picker
// (4 Open-Sans-based pairings, persisted, O defaults to "Display"), a
// GFM-capable tiny-markdown renderer (tables MUST render) with a figure
// callback, verdict pills / stats, the side-by-side line diff, and the
// promote-gate panel laid out as clean STACKED sections. No data fetching,
// no AppState mutation.

import { el, clearChildren, patchClass } from '../../core/dom.js';

// ---- digest-gated swap (the no-flash guarantee) ---------------------
//
// A pane computes a stable digest of ONLY structural/content data
// (timestamps / heartbeat EXCLUDED), then calls gatedSwap(host, digest,
// build). If the digest equals the one this host last painted AND the
// host still has children, NOTHING is written — a steady heartbeat tick
// is a true no-op and the screen cannot flash. The digest lives on the
// host's `data-o-digest` attribute, so it survives across re-renders.
export function gatedSwap(host, digest, build) {
  if (!host) return false;
  const next = String(digest);
  if (host.getAttribute('data-o-digest') === next && host.firstChild) return false;
  clearChildren(host);
  const built = build();
  const nodes = Array.isArray(built) ? built : [built];
  for (const n of nodes) { if (n) host.appendChild(n); }
  host.setAttribute('data-o-digest', next);
  return true;
}

// ---- color theme -----------------------------------------------------

const THEME_KEY = 'zicato.vo.theme';
export const THEMES = [
  ['solarized-dark', 'Dark'],
  ['solarized-light', 'Light'],
  ['monokai', 'Monokai'],
];
const THEME_IDS = THEMES.map((t) => t[0]);
export const DEFAULT_THEME = 'solarized-dark';

export function normaliseTheme(t) { return THEME_IDS.includes(t) ? t : DEFAULT_THEME; }

export function readTheme() {
  let stored = null;
  try { stored = window.localStorage.getItem(THEME_KEY); } catch (e) { /* private mode */ }
  return normaliseTheme(stored);
}

export function applyTheme(root, theme) {
  const t = normaliseTheme(theme);
  if (root) root.setAttribute('data-vo-theme', t);
  try { window.localStorage.setItem(THEME_KEY, t); } catch (e) { /* ignore */ }
  return t;
}

// Move the active pill (the `vo-active` class + `aria-pressed`) to the
// button whose `data-<attr>` equals `id`. Shared by BOTH pickers so they
// behave identically (the bug: the typeface picker applied the font but
// left the pill on the previous button — it never called this).
function setPickerActive(wrap, attr, id) {
  const btns = wrap.querySelectorAll('[' + attr + ']');
  for (const b of btns) {
    const on = b.getAttribute(attr) === id;
    patchClass(b, 'vo-active', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
}

export function themeSwitcher(active, onPick) {
  const cur = normaliseTheme(active);
  const wrap = el('div', { class: 'vo-picker', role: 'group', 'aria-label': 'Color theme' });
  for (const [id, label] of THEMES) {
    const b = el('button', {
      class: 'vo-picker-btn' + (id === cur ? ' vo-active' : ''),
      type: 'button', 'data-theme': id, 'aria-pressed': id === cur ? 'true' : 'false', text: label,
    });
    b.addEventListener('click', () => {
      setPickerActive(wrap, 'data-theme', id);
      if (onPick) onPick(id);
    });
    wrap.appendChild(b);
  }
  return wrap;
}

// ---- typeface theme (NEW) -------------------------------------------
//
// Four Open-Sans-based pairings. The actual font families are switched in
// CSS via the `data-vo-type` attribute; the Google-Fonts <link> is injected
// once by the entry (app_O.js). O defaults to "Display".
const TYPE_KEY = 'zicato.vo.type';
export const TYPEFACES = [
  ['sans', 'Sans'],
  ['editorial', 'Editorial'],
  ['technical', 'Technical'],
  ['display', 'Display'],
];
const TYPE_IDS = TYPEFACES.map((t) => t[0]);
export const DEFAULT_TYPEFACE = 'display';

export function normaliseTypeface(t) { return TYPE_IDS.includes(t) ? t : DEFAULT_TYPEFACE; }

export function readTypeface() {
  let stored = null;
  try { stored = window.localStorage.getItem(TYPE_KEY); } catch (e) { /* private mode */ }
  return normaliseTypeface(stored);
}

export function applyTypeface(root, face) {
  const t = normaliseTypeface(face);
  if (root) root.setAttribute('data-vo-type', t);
  try { window.localStorage.setItem(TYPE_KEY, t); } catch (e) { /* ignore */ }
  return t;
}

export function typefaceSwitcher(active, onPick) {
  const cur = normaliseTypeface(active);
  const wrap = el('div', { class: 'vo-picker', role: 'group', 'aria-label': 'Typeface' });
  for (const [id, label] of TYPEFACES) {
    const b = el('button', {
      class: 'vo-picker-btn' + (id === cur ? ' vo-active' : ''),
      type: 'button', 'data-type': id, 'aria-pressed': id === cur ? 'true' : 'false', text: label,
    });
    // FIX: on click, move the active pill to the clicked button (mirrors
    // the color picker) AND apply the font. The old handler only applied
    // the font, so the pill never moved off the previously-active button.
    b.addEventListener('click', () => {
      setPickerActive(wrap, 'data-type', id);
      if (onPick) onPick(id);
    });
    wrap.appendChild(b);
  }
  return wrap;
}

// ---- theme-aware heatmap ramp ---------------------------------------
//
// The drift-loss heatmap ramp must derive from the ACTIVE theme tokens at
// draw time so it reads in all three themes (no fixed orange/brown ramp).
// We read the two ramp tokens off the variant root via getComputedStyle
// (with a per-theme fallback so the no-DOM test harness still resolves).
const RAMP_FALLBACK = {
  'solarized-dark': ['#0a3b47', '#e8736a'],
  'solarized-light': ['#dfe9e6', '#b1372c'],
  'monokai': ['#3a3b32', '#f92672'],
};
export function heatRamp(root, theme) {
  const t = normaliseTheme(theme);
  let lo = null; let hi = null;
  try {
    if (root && typeof getComputedStyle === 'function') {
      const cs = getComputedStyle(root);
      lo = (cs.getPropertyValue('--vo-heat-lo') || '').trim() || null;
      hi = (cs.getPropertyValue('--vo-heat-hi') || '').trim() || null;
    }
  } catch (e) { /* harness has no getComputedStyle */ }
  const fb = RAMP_FALLBACK[t] || RAMP_FALLBACK['solarized-dark'];
  return [toHex(lo) || fb[0], toHex(hi) || fb[1]];
}
// The SVG ramp lerps hex strings; normalise an `rgb(...)` token to hex.
function toHex(s) {
  if (!s) return null;
  const str = String(s).trim();
  if (/^#[0-9a-fA-F]{6}$/.test(str)) return str;
  const m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/.exec(str);
  if (m) {
    const h = (n) => Number(n).toString(16).padStart(2, '0');
    return '#' + h(m[1]) + h(m[2]) + h(m[3]);
  }
  return null;
}

// ---- small builders --------------------------------------------------

export function section(titleText, ...children) {
  return el('section', { class: 'vo-section' }, [
    titleText ? el('h2', { class: 'vo-h2', text: titleText }) : null,
    ...children.filter(Boolean),
  ].filter(Boolean));
}

export function empty(text) { return el('p', { class: 'vo-empty', text: text || 'No data yet.' }); }
export function loading(text) { return el('p', { class: 'vo-empty', text: text || 'Loading…' }); }

export function stat(value, key) {
  return el('div', { class: 'vo-stat' }, [
    el('span', { class: 'vo-stat-v', text: value }),
    el('span', { class: 'vo-stat-k', text: key }),
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
  return el('span', { class: `vo-pill vo-${d}`, text: label });
}

export function linkButton(label, hrefStr, onClick) {
  const a = el('a', { class: 'vo-linkbtn', href: hrefStr || '#', text: label });
  if (onClick) a.addEventListener('click', (ev) => { ev.preventDefault(); onClick(ev); });
  return a;
}

// A figure caption + an optional "drill into the live view" affordance,
// inside a consistently-sized frame so plates stay proportional.
export function figureFrame(opts) {
  const o = opts || {};
  const fig = el('figure', { class: 'vo-figure' + (o.onOpen ? ' vo-figure-live' : '') });
  if (o.mark) fig.appendChild(el('div', { class: 'vo-figure-mark' }, [o.mark]));
  const cap = el('figcaption', { class: 'vo-figcaption' }, [
    o.number != null ? el('span', { class: 'vo-fig-num', text: `Figure ${o.number}. ` }) : null,
    o.caption ? el('span', { text: o.caption }) : null,
    o.openLabel ? linkButton(o.openLabel, o.openHref, o.onOpen) : null,
  ].filter(Boolean));
  fig.appendChild(cap);
  if (o.onOpen && o.mark && o.mark.addEventListener) {
    fig.style.cursor = 'pointer';
    o.mark.addEventListener('click', (ev) => {
      const t = ev.target;
      const fine = t && t.getAttribute && (t.getAttribute('data-vo') || (t.parentNode && t.parentNode.getAttribute && t.parentNode.getAttribute('data-vo')));
      if (!fine) o.onOpen(ev);
    });
  }
  return fig;
}

// ---- the promote gate (FIX 1) ---------------------------------------
//
// K's gate had the rule labels colliding with the scalar-components
// dot-plot. Here it is laid out as clean STACKED sections, each properly
// sized & fit-to-width:
//   (a) decision pill + Δscalar / Δpass-rate header,
//   (b) the rules ladder — each rule its OWN row (label · status · detail),
//       nothing overlapping,
//   (c) a SEPARATE champion-vs-challenger scalar-components comparison
//       block below (a labelled two-column table, never overlaid on the
//       rules).
export function gatePanel(gate, championId, challengerId, fmtFns) {
  const F = fmtFns || {};
  const fmtSigned = F.fmtSigned || ((v) => String(v));
  const fmt = F.fmt || ((v) => String(v));
  const card = el('div', { class: 'vo-gate' });

  // (a) decision header.
  card.appendChild(el('div', { class: 'vo-gate-head' }, [
    verdictPill(normaliseDecision(gate) || gate.decision),
    el('div', { class: 'vo-gate-stats' }, [
      isNum(gate.delta_scalar) ? stat(fmtSigned(gate.delta_scalar, 2), 'Δ scalar') : null,
      isNum(gate.delta_pass_rate) ? stat(fmtSigned(gate.delta_pass_rate, 2), 'Δ pass rate') : null,
      championId && challengerId ? stat(`${championId} → ${challengerId}`, 'round') : null,
    ].filter(Boolean)),
  ]));
  if (gate.reason) card.appendChild(el('p', { class: 'vo-gate-reason', text: gate.reason }));

  // (b) the rules ladder — one row per rule.
  const rules = Array.isArray(gate.rules) ? gate.rules : [];
  if (rules.length) {
    const ladder = el('div', { class: 'vo-gate-block' }, [
      el('div', { class: 'vo-gate-blockhead', text: 'Rules · short-circuiting, in order' }),
    ]);
    const list = el('ol', { class: 'vo-rules' });
    for (const r of rules) {
      list.appendChild(el('li', { class: 'vo-rule vo-rule-' + (r.status || 'pending') }, [
        el('span', { class: 'vo-rule-label', text: r.label || r.id }),
        el('span', { class: 'vo-rule-status', text: r.status || '—' }),
        el('span', { class: 'vo-rule-detail', text: r.detail || '' }),
      ]));
    }
    ladder.appendChild(list);
    card.appendChild(ladder);
  }

  // (c) the scalar-components comparison — its OWN block below, a clean
  // two-column champion vs challenger table with the per-key delta.
  const sc = gate.scalar_components;
  if (sc && sc.champion && sc.challenger) {
    const keys = [...new Set([...Object.keys(sc.champion), ...Object.keys(sc.challenger)])]
      .filter((k) => isNum(sc.champion[k]) || isNum(sc.challenger[k]));
    if (keys.length) {
      const block = el('div', { class: 'vo-gate-block' }, [
        el('div', { class: 'vo-gate-blockhead', text: 'Scalar components · champion vs challenger' }),
      ]);
      const table = el('table', { class: 'vo-sc-table' });
      table.appendChild(el('thead', null, [el('tr', null, [
        el('th', { text: 'component' }),
        el('th', { class: 'vo-num', text: championId || 'champion' }),
        el('th', { class: 'vo-num', text: challengerId || 'challenger' }),
        el('th', { class: 'vo-num', text: 'Δ' }),
      ])]));
      const tbody = el('tbody');
      for (const k of keys) {
        const a = sc.champion[k]; const b = sc.challenger[k];
        const d = (isNum(a) && isNum(b)) ? b - a : null;
        const dCls = d == null ? '' : d > 0 ? ' vo-bad' : d < 0 ? ' vo-good' : '';
        tbody.appendChild(el('tr', null, [
          el('td', { class: 'vo-mono', text: k }),
          el('td', { class: 'vo-num', text: isNum(a) ? fmt(a, 2) : '—' }),
          el('td', { class: 'vo-num', text: isNum(b) ? fmt(b, 2) : '—' }),
          el('td', { class: 'vo-num' + dCls, text: d == null ? '—' : fmtSigned(d, 2) }),
        ]));
      }
      table.appendChild(tbody);
      block.appendChild(table);
      if (gate.primary_driver && gate.primary_driver.judge) {
        block.appendChild(el('p', { class: 'vo-faint', text:
          `Primary driver: ${gate.primary_driver.judge}${isNum(gate.primary_driver.delta) ? ' (' + fmtSigned(gate.primary_driver.delta, 2) + ')' : ''}` }));
      }
      card.appendChild(block);
    }
  }
  return card;
}

// ---- side-by-side line diff (FIX 2) ---------------------------------
//
// Two columns: champion baseline (left) | challenger new (right), line-by
// -line. Both inputs are STRINGS — the caller resolves the baseline from
// `/api/mutations/{e}/{mid}` `.baseline.content` and the challenger from
// the matching patch's `.new_content`, so the "[object Object]" bug
// (rendering the baseline OBJECT) cannot recur. A simple line LCS marks
// added / removed / unchanged rows.
export function sideBySideDiff(baseline, challenger) {
  const a = splitLines(baseline);
  const b = splitLines(challenger);
  const ops = lineDiff(a, b);
  const wrap = el('div', { class: 'vo-sxs' });
  wrap.appendChild(el('div', { class: 'vo-sxs-head' }, [
    el('div', { class: 'vo-sxs-col-head vo-sxs-old', text: 'champion baseline' }),
    el('div', { class: 'vo-sxs-col-head vo-sxs-new', text: 'challenger new' }),
  ]));
  const body = el('div', { class: 'vo-sxs-body' });
  for (const op of ops) {
    const row = el('div', { class: 'vo-sxs-row vo-sxs-' + op.kind });
    row.appendChild(diffCell(op.left, op.kind === 'add' ? 'gap' : op.kind, 'old'));
    row.appendChild(diffCell(op.right, op.kind === 'del' ? 'gap' : op.kind, 'new'));
    body.appendChild(row);
  }
  wrap.appendChild(body);
  return wrap;
}

function diffCell(text, kind, side) {
  const cls = 'vo-sxs-cell vo-sxs-cell-' + side + (kind === 'gap' ? ' vo-sxs-gap' : '');
  if (kind === 'gap') return el('div', { class: cls });
  return el('div', { class: cls }, [el('code', { text: text == null ? '' : String(text) })]);
}

function splitLines(s) {
  if (s == null) return [];
  return String(s).replace(/\r\n/g, '\n').split('\n');
}

// A compact line-level diff via longest-common-subsequence. Returns an
// ordered list of { kind:'same'|'add'|'del', left, right }. Total: empty
// inputs yield an empty list.
export function lineDiff(a, b) {
  const m = a.length; const n = b.length;
  const lcs = [];
  for (let i = 0; i <= m; i++) lcs.push(new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out = [];
  let i = 0; let j = 0;
  while (i < m && j < n) {
    if (a[i] === b[j]) { out.push({ kind: 'same', left: a[i], right: b[j] }); i++; j++; }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) { out.push({ kind: 'del', left: a[i], right: null }); i++; }
    else { out.push({ kind: 'add', left: null, right: b[j] }); j++; }
  }
  while (i < m) { out.push({ kind: 'del', left: a[i], right: null }); i++; }
  while (j < n) { out.push({ kind: 'add', left: null, right: b[j] }); j++; }
  return out;
}

// ---- GFM-capable tiny markdown (FIX 3: tables MUST render) ----------
//
// Renders a SAFE subset to DOM nodes — headings, paragraphs, lists,
// blockquotes, fenced code, inline code/bold/italic/links, AND GFM
// pipe TABLES (the bug: I's table rendered as raw "| … |"). A
// `<!-- FIGURE:name -->` marker invokes opts.onFigure(name) to splice a
// live figure in place. Never uses innerHTML on untrusted text.
export function renderMarkdown(md, opts) {
  const o = opts || {};
  const root = el('div', { class: 'vo-md' });
  const text = String(md || '').replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  let i = 0;
  let para = [];
  let list = null; let listOrdered = false;

  const flushPara = () => {
    if (para.length) { root.appendChild(el('p', { class: 'vo-md-p' }, inline(para.join(' ')))); para = []; }
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
      root.appendChild(el('pre', { class: 'vo-md-pre' }, [el('code', { text: buf.join('\n') })]));
      continue;
    }
    // GFM table: header row, then a |---|---| separator, then body rows.
    if (trimmed.startsWith('|') && i + 1 < lines.length
        && /^\|?[\s:|-]+\|?$/.test(lines[i + 1].trim()) && lines[i + 1].includes('-')) {
      flushPara(); flushList();
      const header = splitRow(trimmed);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith('|')) { rows.push(splitRow(lines[i].trim())); i++; }
      root.appendChild(mdTable(header, rows));
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushPara(); flushList();
      const lvl = Math.min(6, heading[1].length);
      root.appendChild(el('h' + Math.max(3, lvl), { class: 'vo-md-h vo-md-h' + lvl }, inline(heading[2])));
      i++; continue;
    }
    if (/^>\s?/.test(line)) {
      flushPara(); flushList();
      root.appendChild(el('blockquote', { class: 'vo-md-quote' }, inline(line.replace(/^>\s?/, ''))));
      i++; continue;
    }
    const ordered = /^\s*\d+\.\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (ordered || bullet) {
      flushPara();
      const wantOrdered = !!ordered;
      if (!list || listOrdered !== wantOrdered) {
        flushList(); list = el(wantOrdered ? 'ol' : 'ul', { class: 'vo-md-list' }); listOrdered = wantOrdered;
      }
      list.appendChild(el('li', null, inline((ordered || bullet)[1])));
      i++; continue;
    }
    if (trimmed === '') { flushPara(); flushList(); i++; continue; }
    flushList();
    para.push(trimmed);
    i++;
  }
  flushPara(); flushList();
  if (!root.firstChild) root.appendChild(el('p', { class: 'vo-faint', text: '(empty section)' }));
  return root;
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

function mdTable(header, rows) {
  const t = el('table', { class: 'vo-md-table' });
  t.appendChild(el('thead', null, [el('tr', null, header.map((c) => el('th', null, inline(c))))]));
  t.appendChild(el('tbody', null, rows.map((r) => el('tr', null, r.map((c) => el('td', null, inline(c)))))));
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
    if (tok.startsWith('`')) out.push(el('code', { class: 'vo-md-code', text: tok.slice(1, -1) }));
    else if (tok.startsWith('**')) out.push(el('strong', { text: tok.slice(2, -2) }));
    else if (tok.startsWith('*')) out.push(el('em', { text: tok.slice(1, -1) }));
    else {
      const lm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      if (lm) out.push(el('a', { class: 'vo-md-link', href: lm[2], text: lm[1] }));
      else out.push(tok);
    }
    last = m.index + tok.length;
  }
  if (last < str.length) out.push(str.slice(last));
  return out.length ? out : [str];
}

function isNum(v) { return typeof v === 'number' && isFinite(v); }
