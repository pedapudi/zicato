// variants/T/ui.js — shared chrome helpers for the Console III variant.
//
// Self-contained for Variant P (ported from N's helpers). Small, pure builders every view composes:
// the digest-gated content swap (the no-flash guarantee), section headers,
// verdict pills, a GFM-capable tiny-markdown renderer (TABLES render — fix #3),
// honest empty / loading states, and the colour + typeface theme tables. No
// data fetching, no state mutation.

import { el, clearChildren } from '../../core/dom.js';
import { href } from './router.js';

// ---- digest-gated content swap (the no-flash guarantee) -------------
//
// A view computes a stable digest of ONLY its structural/content data
// (timestamps / heartbeat fields EXCLUDED), then calls gatedSwap(host, digest,
// build). If the digest equals the one this host last painted AND the host
// still has children, NOTHING is written — a steady heartbeat re-dispatch is a
// true no-op and the screen cannot flash.
export function gatedSwap(host, digest, build) {
  if (!host) return false;
  const next = String(digest);
  if (host.getAttribute('data-t-digest') === next && host.firstChild) return false;
  clearChildren(host);
  const built = build();
  const nodes = Array.isArray(built) ? built : [built];
  for (const n of nodes) { if (n) host.appendChild(n); }
  host.setAttribute('data-t-digest', next);
  return true;
}

// ---- colour themes (monokai is the default) -------------------------
//
// THIRTEEN themes now: the three originals plus ten Gogh palettes
// (https://gogh-co.github.io/Gogh/), each mapped to T's `--v2-*` token
// contract in console4.css. Because there are many, the colour picker is a
// SWATCH DROPDOWN (not inline buttons): each option shows a 5-swatch strip
// (ground · surface · ink · improve · regress) as a legibility preview hint,
// plus the theme name. The swatch tuples below FEED that preview — they mirror
// the [paper, panel, ink, good, bad] tokens each theme defines in the CSS.
export const COLOR_THEMES = [
  ['monokai',         'monokai',          ['#1e1f1c', '#272822', '#f8f8f2', '#a6e22e', '#f92672']],
  ['solarized-dark',  'solarized dark',   ['#04222B', '#0A2D38', '#93A1A1', '#8BB80E', '#E0483C']],
  ['solarized-light', 'solarized light',  ['#FDF6E3', '#FBF1D6', '#586E75', '#6B9B0B', '#DC322F']],
  ['google-light',    'google light',     ['#FFFFFF', '#F4F4F4', '#474A4E', '#34A853', '#EA4335']],
  ['google-dark',     'google dark',      ['#202124', '#2C2D30', '#FFFFFF', '#34A853', '#EA4335']],
  ['lunaria-light',   'lunaria light',    ['#EBE4E1', '#E2DCD9', '#363434', '#497D46', '#783C1F']],
  ['lunaria-eclipse', 'lunaria eclipse',  ['#323F46', '#3B484F', '#DFE2ED', '#BEDBC1', '#BA9088']],
  ['belafonte-day',   'belafonte day',    ['#D5CCBA', '#CCC3B2', '#34292D', '#858162', '#BE100E']],
  ['belafonte-night', 'belafonte night',  ['#20111B', '#271821', '#D5CCBA', '#858162', '#BE100E']],
  ['paper',           'paper',            ['#F2EEDE', '#E6E2D3', '#1A1A1A', '#216609', '#CC3E28']],
  ['zenburn',         'zenburn',          ['#3A3A3A', '#424241', '#DCDCCC', '#8FB28F', '#CC9393']],
  ['selenized-black', 'selenized black',  ['#181818', '#202020', '#DEDEDE', '#83C746', '#FF5E56']],
  ['relaxed',         'relaxed',          ['#353A44', '#3D424B', '#F7F7F7', '#A0AC77', '#BC5653']],
];
const COLOR_IDS = COLOR_THEMES.map((t) => t[0]);
export const DEFAULT_COLOR = 'monokai';
const COLOR_KEY = 'zicato.T.theme';

export function normaliseColor(t) { return COLOR_IDS.includes(t) ? t : DEFAULT_COLOR; }
export function readColor() {
  let stored = null;
  try { stored = window.localStorage.getItem(COLOR_KEY); } catch (e) { /* private mode */ }
  return normaliseColor(stored);
}
export function persistColor(t) {
  try { window.localStorage.setItem(COLOR_KEY, normaliseColor(t)); } catch (e) { /* ignore */ }
  return normaliseColor(t);
}

// ---- typeface themes (Technical is the default) ---------------------
//
// Each maps to a `[data-t-type]` value the stylesheet keys on (swapping the
// --n-font-* custom properties). All are Open-Sans-based for the body; the
// distinction is the heading / data voice. (The old "Sans" option is dropped —
// it was redundant with Technical's Open-Sans body; Technical now covers it.)
//   Editorial — Open Sans body + Source Serif 4 headings & publication.
//   Technical — Open Sans body + JetBrains Mono for data / labels / code.
//   Display   — Open Sans body + Archivo Narrow (condensed) headings & big nums.
export const TYPE_THEMES = [
  ['editorial', 'Editorial'],
  ['technical', 'Technical'],
  ['display', 'Display'],
];
const TYPE_IDS = TYPE_THEMES.map((t) => t[0]);
export const DEFAULT_TYPE = 'technical';
const TYPE_KEY = 'zicato.T.typeface';

export function normaliseType(t) { return TYPE_IDS.includes(t) ? t : DEFAULT_TYPE; }
export function readType() {
  let stored = null;
  try { stored = window.localStorage.getItem(TYPE_KEY); } catch (e) { /* private mode */ }
  return normaliseType(stored);
}
export function persistType(t) {
  try { window.localStorage.setItem(TYPE_KEY, normaliseType(t)); } catch (e) { /* ignore */ }
  return normaliseType(t);
}

// ---- density: REMOVED — COZY is the permanent baseline --------------
//
// The density picker (compact / cozy / roomy) is gone. Cozy — the calm,
// mid-air rhythm — is now baked in as the ONE permanent spacing baseline: the
// `--dt-*` spacing tokens live unconditionally on the variant root in
// console4.css (no `[data-t-density]` selector), and the SIZE tokens below are
// fixed at the cozy values. The page-scale pill is the sizing control now.
//
// `DENSITY` names that constant so any caller (and the size-token table) has a
// single source of truth for "the active density is always cozy".
export const DENSITY = 'cozy';

// ---- PAGE-WIDE SCALE (the draggable scale pill + reset) -------------
//
// The page scale is one master multiplier on the WHOLE rendered page (text AND
// diagrams), applied via `zoom` on the Variant-T app root, so the operator can
// fill a wide monitor or shrink to fit a laptop. With density removed, this is
// the SOLE sizing control. A small RESET affordance beside the pill snaps the
// scale back to 100% (DEFAULT_SCALE) and persists. Range 70 %–150 % in 5-point
// steps; default 100 %. Because `zoom` reflows (it is not a transform), the
// page never clips — content re-wraps at the scaled size.
export const SCALE_MIN = 70;
export const SCALE_MAX = 150;
export const SCALE_STEP = 5;
export const DEFAULT_SCALE = 100;
const SCALE_KEY = 'zicato.T.scale';

// Clamp to the range AND snap to the step grid, so a restored / typed value is
// always a legal stop. Total — a non-numeric value falls back to the default.
export function normaliseScale(v) {
  let n = Number(v);
  if (!isFinite(n)) n = DEFAULT_SCALE;
  n = Math.round(n / SCALE_STEP) * SCALE_STEP;
  if (n < SCALE_MIN) n = SCALE_MIN;
  if (n > SCALE_MAX) n = SCALE_MAX;
  return n;
}
export function readScale() {
  let stored = null;
  try { stored = window.localStorage.getItem(SCALE_KEY); } catch (e) { /* private mode */ }
  return normaliseScale(stored == null ? DEFAULT_SCALE : stored);
}
export function persistScale(v) {
  const n = normaliseScale(v);
  try { window.localStorage.setItem(SCALE_KEY, String(n)); } catch (e) { /* ignore */ }
  return n;
}

// ---- LEFT SIDE-PANEL (rail) WIDTH (the draggable rail handle) -------
//
// Distinct from the page-scale pill (which zooms the WHOLE page): this resizes
// only the LEFT tree side-panel width (the `--dt-rail` grid column), so the
// operator can widen the tree to read long generation / entry ids or narrow it
// to give the detail pane more room. A draggable handle on the rail's right
// edge drives it; the chosen width is persisted to localStorage and restored on
// load. Clamped to a sensible min/max so the rail can never collapse or eat the
// page. The detail pane reflows to fill the rest (the grid's 1fr column).
export const RAIL_MIN = 200;
export const RAIL_MAX = 520;
export const DEFAULT_RAIL = 288;
const RAIL_KEY = 'zicato.T.rail';

export function normaliseRail(v) {
  let n = Number(v);
  if (!isFinite(n)) n = DEFAULT_RAIL;
  n = Math.round(n);
  if (n < RAIL_MIN) n = RAIL_MIN;
  if (n > RAIL_MAX) n = RAIL_MAX;
  return n;
}
export function readRail() {
  let stored = null;
  try { stored = window.localStorage.getItem(RAIL_KEY); } catch (e) { /* private mode */ }
  return normaliseRail(stored == null ? DEFAULT_RAIL : stored);
}
export function persistRail(v) {
  const n = normaliseRail(v);
  try { window.localStorage.setItem(RAIL_KEY, String(n)); } catch (e) { /* ignore */ }
  return n;
}

// ---- VISUAL-ELEMENT SIZE tokens (fixed at the cozy baseline) --------
//
// The SVG figures are laid out in JS, so their intrinsic SIZE tokens live HERE.
// With the density picker removed, these are FIXED at the cozy values — the one
// permanent baseline. They drive diagram heights, node radii, heatmap cell
// size, dot-plot row height, the reel/DAG vertical scale, and a figure font
// scale. Every figure stays fit-to-width (Problem 1 holds) — only the INTRINSIC
// (vertical / cell / radius) dimensions are set here; the width is always 100%
// of the pane via the viewBox. The page-scale pill scales the WHOLE page on top
// of these. `sizeScale` is the master multiplier the views apply to row heights
// / cell sizes so the whole composition stays coherent.
const COZY_SIZES = { sizeScale: 1, fontScale: 1, nodeRadius: 1, dagRowStep: 34, heatCell: 16, dotRow: 22, sparkbarH: 42, reelScale: 1.18 };

// The SIZE tokens — always the cozy baseline. The optional argument is ignored
// (kept so existing call sites that passed a density id still work); pure +
// total.
export function densityTokens() {
  return COZY_SIZES;
}

// ---- small builders -------------------------------------------------

export function section(titleText, ...children) {
  return el('section', { class: 'dn-section' }, [
    el('h2', { class: 'dn-h2', text: titleText }),
    ...children.filter(Boolean),
  ]);
}

export function subhead(text) { return el('div', { class: 'dn-subhead', text }); }

export function empty(text) {
  return el('p', { class: 'dn-empty', text: text || 'No data yet.' });
}

export function loading(text) {
  return el('p', { class: 'dn-empty', text: text || 'Loading…' });
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
  return el('span', { class: `dn-pill dn-${d}`, text: label });
}

export function stat(value, key) {
  return el('div', { class: 'dn-stat' }, [
    el('span', { class: 'v', text: value }),
    el('span', { class: 'k', text: key }),
  ]);
}

// A themed link/button (the E bug fix: the "open full transcript" link must be
// a properly styled themed control, never an unstyled anchor).
export function linkButton(text, hrefStr, attrs) {
  return el('a', Object.assign({ class: 'dn-linkbtn', href: hrefStr }, attrs || {}), [text]);
}

// ---- tiny markdown → DOM (GFM tables render — fix #3) ---------------
//
// Renders a SAFE subset to DOM nodes — headings, paragraphs, ordered + bullet
// lists, blockquotes, inline code/bold/italic/links, fenced code, AND GFM
// TABLES — without ever using innerHTML on untrusted text. The figure marker
// `<!-- FIGURE:name -->` invokes opts.onFigure(name) so a live figure can be
// spliced in. Anything unrecognised becomes a paragraph.
export function renderMarkdown(md, opts) {
  const o = opts || {};
  const root = el('div', { class: 'dn-md-body' });
  const text = String(md || '').replace(/\r\n/g, '\n');
  const lines = text.split('\n');
  let i = 0;
  let para = [];
  let list = null; let listOrdered = false;

  const flushPara = () => { if (para.length) { root.appendChild(el('p', null, inline(para.join(' ')))); para = []; } };
  const flushList = () => { if (list) { root.appendChild(list); list = null; } };

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // figure marker → callback
    const fig = /^<!--\s*FIGURE:([A-Za-z0-9_-]+)\s*-->$/.exec(trimmed);
    if (fig) {
      flushPara(); flushList();
      if (o.onFigure) { const node = o.onFigure(fig[1]); if (node) root.appendChild(node); }
      i++; continue;
    }
    if (/^<!--.*-->$/.test(trimmed)) { i++; continue; } // skip other markers

    if (/^```/.test(trimmed)) {
      flushPara(); flushList();
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) { buf.push(lines[i]); i++; }
      i++;
      root.appendChild(el('pre', null, [el('code', { text: buf.join('\n') })]));
      continue;
    }
    // GFM table: a header row, a `| --- |` separator, then body rows.
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
      const lvl = Math.min(4, heading[1].length);
      root.appendChild(el('h' + lvl, null, inline(heading[2])));
      i++; continue;
    }
    if (/^>\s?/.test(line)) {
      flushPara(); flushList();
      root.appendChild(el('blockquote', null, inline(line.replace(/^>\s?/, ''))));
      i++; continue;
    }
    const ordered = /^\s*\d+\.\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    if (ordered || bullet) {
      flushPara();
      const wantOrdered = !!ordered;
      if (!list || listOrdered !== wantOrdered) { flushList(); list = el(wantOrdered ? 'ol' : 'ul'); listOrdered = wantOrdered; }
      list.appendChild(el('li', null, inline((ordered || bullet)[1])));
      i++; continue;
    }
    if (trimmed === '') { flushPara(); flushList(); i++; continue; }
    flushList();
    para.push(trimmed);
    i++;
  }
  flushPara(); flushList();
  if (!root.firstChild) root.appendChild(el('p', { class: 'dn-faint', text: 'Nothing to render.' }));
  return root;
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

function table(header, rows) {
  const t = el('table', { class: 'dn-md-table' });
  t.appendChild(el('thead', null, [el('tr', null, header.map((c) => el('th', null, inline(c))))]));
  t.appendChild(el('tbody', null, rows.map((r) => el('tr', null, r.map((c) => el('td', null, inline(c)))))));
  // contain wide GFM tables (e.g. in the publication body) so they scroll
  // WITHIN their own box and never push the page/panel layout sideways.
  return el('div', { class: 'dn-table-scroll' }, [t]);
}

function inline(s) {
  const out = [];
  const str = String(s == null ? '' : s);
  const re = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/g;
  let last = 0; let m;
  while ((m = re.exec(str)) !== null) {
    if (m.index > last) out.push(str.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith('`')) out.push(el('code', { text: tok.slice(1, -1) }));
    else if (tok.startsWith('**')) out.push(el('strong', { text: tok.slice(2, -2) }));
    else if (tok.startsWith('*')) out.push(el('em', { text: tok.slice(1, -1) }));
    else {
      const lm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      if (lm) out.push(el('a', { class: 'dn-md-link', href: lm[2], text: lm[1] }));
      else out.push(tok);
    }
    last = m.index + tok.length;
  }
  if (last < str.length) out.push(str.slice(last));
  return out.length ? out : [str];
}

export { href };
