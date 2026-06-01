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

// ---- colour themes (monokai is N's default) -------------------------
export const COLOR_THEMES = [
  ['monokai', 'monokai'],
  ['solarized-dark', 'sol·dark'],
  ['solarized-light', 'sol·light'],
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

// ---- typeface themes (Technical is N's default) ---------------------
//
// Each maps to a `[data-n-type]` value the stylesheet keys on (swapping the
// --n-font-* custom properties). All are Open-Sans-based for the body; the
// distinction is the heading / data voice:
//   Sans      — Open Sans throughout (tabular figures for data).
//   Editorial — Open Sans body + Source Serif 4 headings & publication.
//   Technical — Open Sans body + JetBrains Mono for data / labels / code.
//   Display   — Open Sans body + Archivo Narrow (condensed) headings & big nums.
export const TYPE_THEMES = [
  ['sans', 'Sans'],
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

// ---- density / "roominess" (compact is T's Console default) ---------
//
// The THIRD chrome picker. Each value maps to a `[data-t-density]` attribute on
// the root that the stylesheet keys on, swapping the spacing/size custom
// properties (padding, gaps, font-size scale, card min-width, rail width, the
// reel's vertical scale) so the WHOLE UI — reel, match cards, tables, gate,
// tree — re-breathes. Compact = the dense Console default; Roomy = Atlas-like
// air (Q's generous proportion); Cozy sits between.
export const DENSITY_THEMES = [
  ['compact', 'compact'],
  ['cozy', 'cozy'],
  ['roomy', 'roomy'],
];
const DENSITY_IDS = DENSITY_THEMES.map((t) => t[0]);
export const DEFAULT_DENSITY = 'compact';
const DENSITY_KEY = 'zicato.T.density';

export function normaliseDensity(t) { return DENSITY_IDS.includes(t) ? t : DEFAULT_DENSITY; }
export function readDensity() {
  let stored = null;
  try { stored = window.localStorage.getItem(DENSITY_KEY); } catch (e) { /* private mode */ }
  return normaliseDensity(stored);
}
export function persistDensity(t) {
  try { window.localStorage.setItem(DENSITY_KEY, normaliseDensity(t)); } catch (e) { /* ignore */ }
  return normaliseDensity(t);
}

// ---- PAGE-WIDE SCALE (the draggable scale pill) ---------------------
//
// A continuous control DISTINCT from density. Density picks a spacing /
// proportion RHYTHM (compact/cozy/roomy) — how the layout breathes. The page
// scale is one master multiplier on the WHOLE rendered page (text AND
// diagrams), applied via `zoom` on the Variant-T app root, so the operator can
// fill a wide monitor or shrink to fit a laptop. The two COMPOSE: scaling
// applies ON TOP of whatever density layout is in effect, and changing density
// never resets the scale (they persist under separate keys). Range 70 %–150 %
// in 5-point steps; default 100 %. Because `zoom` reflows (it is not a
// transform), the page never clips — content re-wraps at the scaled size.
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

// ---- density → VISUAL-ELEMENT SIZE tokens ---------------------------
//
// The density picker scales spacing via CSS custom properties (--dt-*), but it
// must ALSO scale the SIZE of the rendered visual elements — the SVG figures
// are laid out in JS, so the size tokens live HERE (a pure table keyed by the
// same density id). Compact → roomy grows diagram heights, node radii, heatmap
// cell size, dot-plot row height, the reel/DAG vertical scale, and a global
// figure font scale; compact shrinks them. Every figure stays fit-to-width at
// every density (Problem 1 holds in compact AND roomy) — only the INTRINSIC
// (vertical / cell / radius) dimensions scale, the width is always 100% of the
// pane via the viewBox. `sizeScale` is the master multiplier the views apply to
// row heights / cell sizes so the whole composition grows coherently.
const DENSITY_SIZES = {
  compact: { sizeScale: 0.88, fontScale: 0.92, nodeRadius: 0.9, dagRowStep: 30, heatCell: 13, dotRow: 19, sparkbarH: 36, reelScale: 1 },
  cozy: { sizeScale: 1, fontScale: 1, nodeRadius: 1, dagRowStep: 34, heatCell: 16, dotRow: 22, sparkbarH: 42, reelScale: 1.18 },
  roomy: { sizeScale: 1.18, fontScale: 1.1, nodeRadius: 1.18, dagRowStep: 40, heatCell: 20, dotRow: 26, sparkbarH: 50, reelScale: 1.4 },
};

// The SIZE tokens for one density (defaults to the persisted value). Pure +
// total — an unknown density falls back to the compact default.
export function densityTokens(density) {
  const d = normaliseDensity(density || readDensity());
  return DENSITY_SIZES[d] || DENSITY_SIZES[DEFAULT_DENSITY];
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
