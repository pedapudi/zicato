// js/ui.js — shared chrome helpers for the Console III variant.
//
// Self-contained for Variant P (ported from N's helpers). Small, pure builders every view composes:
// the digest-gated content swap (the no-flash guarantee), section headers,
// verdict pills, a GFM-capable tiny-markdown renderer (TABLES render — fix #3),
// honest empty / loading states, and the colour + typeface theme tables. No
// data fetching, no state mutation.

import { el, clearChildren } from './core/dom.js';
import { href } from './router.js';
import { isNum, fmt, fmtSigned } from './svg.js';
import * as D from './data.js';

// svg.js owns the canonical numeric formatters (isNum/fmt/fmtSigned). ui.js
// re-exports them so a view can pull "one formatter home" from ui.js without a
// second svg.js import; the definitions live in svg.js and never fork.
export { isNum, fmt, fmtSigned };

// ---- continuous per-entry score + precision/recall (#18) -------------
//
// Shared helpers for surfacing the continuous outcome `score` (∈ [0,1]) and its
// optional `metrics` precision/recall decomposition wherever the dashboard
// shows a per-entry / per-board outcome. All three degrade cleanly when the
// fields are absent (the bool-only / pre-score path) so a board with no scores
// reads exactly as before.

// finite-number guard — the svg.js canonical (aliased so the many local call
// sites below read unchanged).
const _isNum = isNum;

// format a 0–1 score (or any finite number) to N decimals; '—' when absent.
// A thin wrapper over the canonical fmt (svg.js) at the 2-decimal default this
// score axis uses.
export function scoreFmt(v, n) {
  return _isNum(v) ? fmt(v, _isNum(n) ? n : 2) : '—';
}

// truncate(s, n) — the ONE clip/shorten home. Coerces to string, returns the
// first n-1 chars + '…' when longer than n, else the string verbatim. Replaces
// the four hand-copied clip/shorten helpers (dag.js / candidate.js /
// boardstatus.js and builder.js's '—'-defaulting variant via the `fallback`
// option).
export function truncate(s, n, opts) {
  const o = opts || {};
  const str = (s == null) ? (o.fallback != null ? o.fallback : '') : String(s);
  return str.length > n ? str.slice(0, n - 1) + '…' : str;
}

// a compact precision/recall tag from a per-entry `metrics` map:
// "P 0.70 / R 0.55" when both present, "P 0.70" / "R 0.55" when only one is,
// '' when the entry carries no precision/recall (the bool-only path). Other
// metric keys are ignored — this is the two-axis indicator only.
export function prText(metrics) {
  if (!metrics || typeof metrics !== 'object') return '';
  const parts = [];
  if (_isNum(metrics.precision)) parts.push('P ' + scoreFmt(metrics.precision, 2));
  if (_isNum(metrics.recall)) parts.push('R ' + scoreFmt(metrics.recall, 2));
  return parts.join(' / ');
}

// a stable digest signature for a per-entry `metrics` map: sorted
// [key, rounded-value] pairs, or null when absent — folded into a view's
// content digest so a change to any metric repaints while a no-op heartbeat
// stays byte-identical. A bool-only entry (no metrics) folds null, leaving the
// digest unchanged vs the pre-score path.
export function metricsDigest(metrics) {
  if (!metrics || typeof metrics !== 'object') return null;
  const keys = Object.keys(metrics).filter((k) => _isNum(metrics[k])).sort();
  if (!keys.length) return null;
  return keys.map((k) => [k, metrics[k].toFixed(3)]);
}

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
// SIXTEEN themes now: the three originals plus thirteen Gogh palettes
// (https://gogh-co.github.io/Gogh/), each mapped to T's `--v2-*` token
// contract in console.css. Because there are many, the colour picker is a
// SWATCH DROPDOWN (not inline buttons): each option shows a 6-swatch strip
// (ground · surface · ink · improve · regress · accent) as a legibility preview
// hint, plus the theme name. The swatch tuples below FEED that preview — they
// mirror the [paper, panel, ink, good, bad, accent] tokens each theme defines
// in the CSS. The 6th element is the theme's signature accent (`--v2-accent`).
//
// NOTE: lunaria-eclipse's true --v2-accent (#BCDBFF, a pale blue) is visually
// near-identical to its pale ink (#DFE2ED) — they would read as one swatch in
// the 6-strip preview. So for THE PREVIEW ONLY we substitute a more distinct
// hue from the Lunaria Eclipse Gogh palette (its magenta #C8429F). The live
// --v2-accent token in console.css is unchanged.
export const COLOR_THEMES = [
  ['monokai',         'monokai',          ['#1e1f1c', '#272822', '#f8f8f2', '#a6e22e', '#f92672', '#66d9ef']],
  ['solarized-dark',  'solarized dark',   ['#04222B', '#0A2D38', '#93A1A1', '#8BB80E', '#E0483C', '#2AA198']],
  ['solarized-light', 'solarized light',  ['#FDF6E3', '#FBF1D6', '#586E75', '#6B9B0B', '#DC322F', '#268BD2']],
  ['google-light',    'google light',     ['#FFFFFF', '#F4F4F4', '#474A4E', '#34A853', '#EA4335', '#1B9CB8']],
  ['google-dark',     'google dark',      ['#202124', '#2C2D30', '#FFFFFF', '#34A853', '#EA4335', '#24C1E0']],
  ['lunaria-light',   'lunaria light',    ['#EBE4E1', '#E2DCD9', '#363434', '#497D46', '#783C1F', '#3778A9']],
  ['lunaria-eclipse', 'lunaria eclipse',  ['#323F46', '#3B484F', '#DFE2ED', '#BEDBC1', '#BA9088', '#C8429F']],
  ['belafonte-day',   'belafonte day',    ['#D5CCBA', '#CCC3B2', '#34292D', '#6e6a4e', '#BE100E', '#426A79']],
  ['belafonte-night', 'belafonte night',  ['#20111B', '#271821', '#D5CCBA', '#a6a07a', '#d6403e', '#6F8E97']],
  ['paper',           'paper',            ['#F2EEDE', '#E6E2D3', '#1A1A1A', '#216609', '#CC3E28', '#1E6FCC']],
  ['zenburn',         'zenburn',          ['#3A3A3A', '#424241', '#DCDCCC', '#8FB28F', '#CC9393', '#8CD0D3']],
  ['selenized-black', 'selenized black',  ['#181818', '#202020', '#DEDEDE', '#83C746', '#FF5E56', '#56D8C9']],
  ['relaxed',         'relaxed',          ['#353A44', '#3D424B', '#F7F7F7', '#A0AC77', '#BC5653', '#7EAAC7']],
  ['espresso',        'espresso',         ['#323232', '#3A3A3A', '#FFFFFF', '#A5C261', '#D25252', '#6C99BB']],
  ['dracula',         'dracula',          ['#282A36', '#343746', '#F8F8F2', '#50FA7B', '#FF5555', '#BD93F9']],
  ['ubuntu',          'ubuntu',           ['#300A24', '#3D1530', '#EEEEEC', '#8AE234', '#CC0000', '#34E2E2']],
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

// ---- typeface OPTIONS (the operator's finalized 12 faces) -----------
//
// The typeface picker is a GROUPED POPOVER offering the operator's finalized
// TWELVE faces — FOUR per mode across THREE modes (Technical · Editorial ·
// Display) — lifted byte-for-byte from the typeface study (FONT_STACKS /
// TYPEFACE_MODES in compose.html, OPTIONS in index.html). Each option carries
// its id (the `[data-t-type]` value the stylesheet keys on), its mode group,
// a human label, and the FOUR font-role stacks the dashboard tokens map to:
//   head  → --n-font-head   (headings / big numerals)
//   prose → --v2-sans + --n-font-paper   (body / publication voice)
//   data  → --v2-mono       (data / labels / code)
//   code  → (currently folded into --v2-mono; kept for parity with the study)
//
// Selecting an option stamps `data-t-type="<id>"` on the root (e.g. `T7`) and
// the per-id CSS rule in console.css swaps the four font-role vars to the
// matching stacks. The micro-preview in each row renders in that option's REAL
// faces, so the popover reads as a true type specimen.
//
// The exact stacks below MIRROR the study's FONT_STACKS so the dashboard
// matches it byte-for-byte. Self-hosted JetBrains/iA faces are untouched; the
// Google-Fonts families these reference are loaded by app_T.js's ensureFonts().
const TF = {
  GSMONO: "'Google Sans Mono', 'Noto Sans Mono', ui-monospace, monospace",
  SRCS: "'Source Sans 3', system-ui, sans-serif",
  SRCC: "'Source Code Pro', ui-monospace, monospace",
  INCON: "'Inconsolata', ui-monospace, monospace",
  UBUNTU: "'Ubuntu', system-ui, sans-serif",
  UBUM: "'Ubuntu Mono', ui-monospace, monospace",
  FRAUN: "'Fraunces', Georgia, serif",
  BITTER: "'Bitter', Georgia, serif",
  LITER: "'Literata', Georgia, serif",
  DOMINE: "'Domine', Georgia, serif",
  AN: "'Archivo Narrow', 'Space Grotesk', system-ui, sans-serif",
  SG: "'Space Grotesk', system-ui, sans-serif",
  HANKEN: "'Hanken Grotesk', system-ui, sans-serif",
  BARLOWC: "'Barlow Condensed', 'Archivo Narrow', system-ui, sans-serif",
  BRICO: "'Bricolage Grotesque', system-ui, sans-serif",
};

// The mode groups, in display order. Each option: {id, mode, label, head,
// prose, data, code}. FOUR options per mode = TWELVE total.
export const TYPE_MODE_ORDER = ['technical', 'editorial', 'display'];
export const TYPE_MODE_LABEL = { technical: 'Technical', editorial: 'Editorial', display: 'Display' };

export const TYPE_OPTIONS = [
  // Technical
  { id: 'T7',  mode: 'technical', label: 'T7 · Google Sans Mono',                head: TF.GSMONO, prose: TF.GSMONO, data: TF.GSMONO, code: TF.GSMONO },
  { id: 'T9',  mode: 'technical', label: 'T9 · Source Sans 3 + Source Code Pro', head: TF.SRCS,   prose: TF.SRCS,   data: TF.SRCC,   code: TF.SRCC  },
  { id: 'T12', mode: 'technical', label: 'T12 · Inconsolata',                    head: TF.INCON,  prose: TF.INCON,  data: TF.INCON,  code: TF.INCON },
  { id: 'T14', mode: 'technical', label: 'T14 · Ubuntu + Ubuntu Mono',          head: TF.UBUNTU, prose: TF.UBUNTU, data: TF.UBUM,   code: TF.UBUM  },
  // Editorial
  { id: 'E5',  mode: 'editorial', label: 'E5 · Fraunces',                        head: TF.FRAUN,  prose: TF.FRAUN,  data: TF.FRAUN,  code: TF.FRAUN  },
  { id: 'E7',  mode: 'editorial', label: 'E7 · Bitter',                          head: TF.BITTER, prose: TF.BITTER, data: TF.BITTER, code: TF.BITTER },
  { id: 'E8',  mode: 'editorial', label: 'E8 · Literata',                        head: TF.LITER,  prose: TF.LITER,  data: TF.LITER,  code: TF.LITER  },
  { id: 'E15', mode: 'editorial', label: 'E15 · Domine',                         head: TF.DOMINE, prose: TF.DOMINE, data: TF.DOMINE, code: TF.DOMINE },
  // Display
  { id: 'D2',  mode: 'display',   label: 'D2 · Archivo Narrow + Space Grotesk',  head: TF.AN,     prose: TF.SG,     data: TF.SG,     code: TF.SG     },
  { id: 'D12', mode: 'display',   label: 'D12 · Hanken Grotesk',                 head: TF.HANKEN, prose: TF.HANKEN, data: TF.HANKEN, code: TF.HANKEN },
  { id: 'D14', mode: 'display',   label: 'D14 · Barlow Condensed + Space Grotesk', head: TF.BARLOWC, prose: TF.SG,  data: TF.SG,     code: TF.SG     },
  { id: 'D5',  mode: 'display',   label: 'D5 · Bricolage Grotesque',             head: TF.BRICO,  prose: TF.BRICO,  data: TF.BRICO,  code: TF.BRICO },
];

const TYPE_IDS = TYPE_OPTIONS.map((o) => o.id);
const TYPE_BY_ID = new Map(TYPE_OPTIONS.map((o) => [o.id, o]));

// BACK-COMPAT shape: `[id, label]` pairs (the prior TYPE_THEMES contract) so
// existing call sites — shell.js's `TYPEFACES = TYPE_THEMES.map(t => t[0])` and
// any consumer expecting the tuple form — keep working against the 12 options.
export const TYPE_THEMES = TYPE_OPTIONS.map((o) => [o.id, o.label]);

// The DEFAULT is T7 · Google Sans Mono (first Technical). FLAGGED in the report
// so the operator can change it.
export const DEFAULT_TYPE = 'T7';
const TYPE_KEY = 'zicato.T.typeface';

// Migrate the THREE legacy mode ids (technical / editorial / display) to a
// sensible finalized default in their group, so a stored old value keeps a
// coherent voice instead of snapping back to the global default.
const LEGACY_TYPE_MAP = { technical: 'T7', editorial: 'E5', display: 'D2' };

// Normalise any stored / passed value to a known option id. Unknown ⇒ default;
// a legacy mode id ⇒ its migrated finalized id.
export function normaliseType(t) {
  if (TYPE_IDS.includes(t)) return t;
  if (t && LEGACY_TYPE_MAP[t]) return LEGACY_TYPE_MAP[t];
  return DEFAULT_TYPE;
}
// Resolve a value to its full option object (real font stacks). Useful for the
// picker's micro-previews and for any caller that wants the resolved faces.
export function typeOption(t) { return TYPE_BY_ID.get(normaliseType(t)) || TYPE_BY_ID.get(DEFAULT_TYPE); }
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
// console.css (no `[data-t-density]` selector), and the SIZE tokens below are
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

// ---- GLOBAL TEXT FONT-SIZE (the S/M/L control in the typeface picker) ----
//
// DISTINCT from the page-scale pill: the scale pill `zoom`s the WHOLE page
// (text AND figures); this is a TEXT-ONLY multiplier — it scales the HTML text
// (every `font-size` in console.css is `calc(Npx * var(--dt-font-scale,1))`)
// WITHOUT touching the SVG figures (their text is sized by svg.js / the `font:`
// shorthand, not those rules). Some faces (e.g. Ubuntu, Inconsolata) read tiny
// at the baseline; this lets the operator step the text up a notch or two.
//
// THREE stops — small / medium / large — each mapping to a `--dt-font-scale`
// number. The raw literal-px baseline (scale 1.0) read too small for the low-
// x-height faces, so the ladder starts ABOVE it: small 1.15 (the comfortable
// floor + the default), medium 1.3, large 1.45 — even 0.15 steps. applyFontSize
// (shell.js) stamps the var + a `data-t-fontsize` attribute on the app root and
// persists; the picker's segmented control reads/sets it.
export const FONTSIZE_OPTIONS = [
  { id: 'small',  label: 'S', title: 'Small text',  scale: 1.15 },
  { id: 'medium', label: 'M', title: 'Medium text', scale: 1.3 },
  { id: 'large',  label: 'L', title: 'Large text',  scale: 1.45 },
];
const FONTSIZE_IDS = FONTSIZE_OPTIONS.map((o) => o.id);
const FONTSIZE_BY_ID = new Map(FONTSIZE_OPTIONS.map((o) => [o.id, o]));
export const DEFAULT_FONTSIZE = 'small';
const FONTSIZE_KEY = 'zicato.T.fontsize';

// Normalise any stored / passed value to a known size id. Unknown ⇒ small.
export function normaliseFontSize(v) {
  return FONTSIZE_IDS.includes(v) ? v : DEFAULT_FONTSIZE;
}
// The `--dt-font-scale` NUMBER for a size id (small ⇒ 1, the byte-identical
// baseline). Used by applyFontSize to stamp the root + by tests.
export function fontSizeScale(v) {
  const o = FONTSIZE_BY_ID.get(normaliseFontSize(v));
  return o ? o.scale : 1;
}
export function readFontSize() {
  let stored = null;
  try { stored = window.localStorage.getItem(FONTSIZE_KEY); } catch (e) { /* private mode */ }
  return normaliseFontSize(stored);
}
export function persistFontSize(v) {
  const n = normaliseFontSize(v);
  try { window.localStorage.setItem(FONTSIZE_KEY, n); } catch (e) { /* ignore */ }
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

// THE PAGE-SCALE FACTOR a coordinate must be divided by to convert a viewport
// (CSS-px) pointer position into the LAYOUT space that `--dt-rail` lives in.
// The Variant-T app root carries a page-wide `zoom` (the scale pill) plus a
// mirrored `--dt-page-scale` ratio; both are the same number. The rail handle
// sits INSIDE that zoomed root, so `event.clientX` (viewport CSS px) is the
// LAID-OUT position multiplied by `zoom`. Dividing the pointer delta by this
// factor recovers the unscaled layout-space delta the grid column expects —
// THIS is the fix for the jumpy / over-tracking drag at a non-100% scale.
//
// Reads the live factor defensively: first the inline `zoom`, then the
// `--dt-page-scale` custom property, then the `data-t-scale` percent attribute;
// falls back to 1 (no scale) when nothing is set or the value is bogus.
export function pageScaleOf(root) {
  if (!root) return 1;
  let raw = null;
  const st = root.style;
  if (st) {
    if (st.zoom != null && st.zoom !== '') raw = st.zoom;
    else if (typeof st.getPropertyValue === 'function') {
      const v = st.getPropertyValue('--dt-page-scale');
      if (v) raw = v;
    } else if (st._props && st._props['--dt-page-scale'] != null) {
      raw = st._props['--dt-page-scale'];
    }
  }
  if (raw == null && root.getAttribute) {
    const pct = root.getAttribute('data-t-scale');
    if (pct != null) raw = Number(pct) / 100;
  }
  const n = Number(raw);
  return isFinite(n) && n > 0 ? n : 1;
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

// The SERVER-STAMPED decision token off any payload record that carries one —
// an epoch experiment (readers stamp `decision` via the canonical classifier),
// a gate breakdown, or a tournament matchup row. Read VERBATIM: the server
// owns the vocabulary (promoted / rejected / deferred); the client never
// re-classifies a nested outcome or substring-matches free text. Absent /
// non-string → null (the caller renders unknown/pending, never a guess).
export function decisionOf(rec) {
  const d = rec && typeof rec === 'object' ? rec.decision : null;
  return (typeof d === 'string' && d) ? d : null;
}

// The ONE place that turns a generation's tri-state outcome into a decision
// label. `promoted` is tri-state in lineage AND on the stamped experiment
// records: `true` (won the gate), `false` (lost), or `null`/absent
// (in-flight / not yet raced). The Class-B bug was treating an ABSENT outcome
// as `'rejected'` ("dead branch") on candidates that have not raced yet — so
// this NEVER defaults null/absent to rejected:
//   * no parent                          → 'baseline' (the seed / loss floor)
//   * promoted === true                  → 'promoted'
//   * promoted === false                 → 'rejected'
//   * a stamped exp/gate `decision`      → that token, verbatim
//   * otherwise (promoted == null, no stamped decision) → 'pending'
export function decisionFor(spec) {
  const s = spec || {};
  // A seed (no parent) defines the loss floor — it is the baseline, never a
  // verdict. An explicit `baseline:true` also forces it.
  if (s.baseline === true || (s.baseline == null && s.parent == null)) return 'baseline';
  if (s.promoted === true) return 'promoted';
  if (s.promoted === false) return 'rejected';
  // promoted is null/absent — defer to the SERVER-STAMPED decision token.
  const resolved = decisionOf(s.exp) || decisionOf(s.gate);
  if (resolved === 'promoted') return 'promoted';
  if (resolved === 'rejected') return 'rejected';
  if (resolved === 'deferred') return 'deferred';
  // genuinely unresolved (in-flight / not yet raced) → pending, NOT rejected.
  return 'pending';
}

export function verdictPill(decision) {
  const d = decision || 'baseline';
  const label = d === 'baseline' ? 'seed (v0)' : d === 'pending' ? 'racing…' : d;
  return el('span', { class: `dn-pill dn-${d}`, text: label });
}

// ---- operator-override provenance (the overrideChip primitive) -------
//
// The GATE owns the verdict (verdictPill); an OPERATOR override is a SEPARATE
// provenance fact that rides BESIDE the verdict and must NOT recolor it.
// `overrideChip` is the sibling primitive carrying that fact. `prov` accepts
// either contract shape verbatim:
//   * gate.override        — {present, action: "promote"|"reject", reason}
//   * override_status[gid] — {action: "promote"|"reject", state, reason, ts}
// Returns null (renders NOTHING — byte-identical to today) when no override is
// present. The four operator states: `forced↑` (force-promote applied),
// `forced✕` (force-reject applied), `queued` (recorded, not yet fired),
// `drained` (queued but the round resolved without it — forward-compat).
// Direction earns the colour (promote good / reject bad / queued caution /
// drained faint) — never a new hue, never recoloring the verdict beside it.
export function normaliseOverride(prov) {
  if (!prov || typeof prov !== 'object') return null;
  // gate.override carries an explicit `present` flag; absent → nothing.
  if ('present' in prov && !prov.present) return null;
  const action = String(prov.action || '').toLowerCase();
  if (action !== 'promote' && action !== 'reject') return null;
  const state = String(prov.state || 'applied').toLowerCase();
  const reason = (typeof prov.reason === 'string' && prov.reason) ? prov.reason : null;
  let kind;
  let label;
  let glyph;
  if (state === 'queued' || state === 'pending') {
    kind = 'queued'; label = 'queued'; glyph = '⋯'; // operator action recorded, not yet fired
  } else if (state === 'drained' || state === 'expired') {
    kind = 'drained'; label = 'drained'; glyph = '∅'; // queued, never fired this round
  } else if (action === 'promote') {
    kind = 'promote'; label = 'forced'; glyph = '↑'; // force-promoted
  } else {
    kind = 'reject'; label = 'forced'; glyph = '✕'; // force-rejected
  }
  return { kind, action, state, label, glyph, reason };
}

// Build the override chip — a `dn-chip dn-override dn-override-<kind>` span that
// reads "⟳ forced↑ · operator" beside the verdict. Returns null when there is
// no override (back-compat: absent → byte-identical). The chip's `kind` class
// earns its tone by DIRECTION (promote good / reject bad / queued caution /
// drained faint); it never touches the verdict pill's class.
export function overrideChip(prov) {
  const o = normaliseOverride(prov);
  if (!o) return null;
  const chip = el('span', {
    class: `dn-chip dn-override dn-override-${o.kind}`,
    'data-override': o.kind,
    title: o.reason ? ('operator override · ' + o.reason) : 'operator override',
  }, [
    el('span', { class: 'dn-override-mark', 'aria-hidden': 'true', text: '⟳' }),
    el('span', { class: 'dn-override-label', text: o.label + o.glyph }),
    el('span', { class: 'dn-override-by dn-faint', text: ' · operator' }),
  ]);
  return chip;
}

// A content digest of an override (rounded/stable, no timestamps) so it folds
// into a structural digest: a real override appearing/changing repaints, a
// no-op beat stays byte-identical. null (no override) contributes nothing —
// back-compat with the pre-override digest. NOTE: deliberately drops `ts` (a
// timestamp would bust the no-op-beat-skip), keeping only kind + reason.
export function overrideDigest(prov) {
  const o = normaliseOverride(prov);
  return o ? [o.kind, o.action, o.state, o.reason] : null;
}

// ---- the FIELD-OVERRIDE CONTROL PLANE (the operator action cell) ------
//
// The override CHIP renders the FACT of an override; this CONTROL creates one —
// the per-challenger force-promote/reject the operator fires against the gate.
// CONFIRM-INLINE (arm → reason → POST, never one-click) and OPTIMISTIC: a
// 202-accepted POST stamps a local 'queued' override that survives the digest-
// gated re-render via this registry (keyed by gid, NO timestamp, so it folds into
// structureDigest cleanly — a queued override repaints, a no-op beat is stable).
const _pendingOverrides = new Map(); // gid -> {action, reason, state:'queued'}

// The optimistic prov for a gid (override_status shape), or null — flows straight
// into overrideChip / overrideDigest like the durable readback.
export function pendingOverride(gid) {
  const p = _pendingOverrides.get(String(gid));
  return p ? { action: p.action, reason: p.reason, state: 'queued' } : null;
}

// Mark a gid optimistically overridden (on a 202-accepted POST); returns the prov.
export function markPendingOverride(gid, action, reason) {
  const prov = { action: String(action), reason: (typeof reason === 'string' && reason) ? reason : null, state: 'queued' };
  _pendingOverrides.set(String(gid), prov);
  return prov;
}

// Drop a gid's optimistic stamp once the durable readback supersedes it.
export function clearPendingOverride(gid) { _pendingOverrides.delete(String(gid)); }

// Test-only: drop every optimistic stamp.
export function _resetPendingOverrides() { _pendingOverrides.clear(); }

// A stable digest fragment for the optimistic overrides over a set of gids —
// folds into structureDigest (NO timestamp, sorted by gid; [] when none pending).
export function pendingOverrideDigest(gids) {
  const out = [];
  for (const gid of (Array.isArray(gids) ? gids : [])) {
    const p = _pendingOverrides.get(String(gid));
    if (p) out.push([String(gid), p.action, p.reason]);
  }
  out.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  return out;
}

// Build the per-challenger override CONTROL cell (a stable wrapper for the
// standings action column). `opts`: gid (required); epochId/tournamentId/
// structure (ride into the POST body so the readback names the field round);
// readOnly (a DISABLED affordance — visible, never POSTs); settled (the round
// resolved — no re-arm); existingOverride (the durable readback prov — the
// control is spent); onPost(action, gid, reason) → {ok, status} (on ok the cell
// stamps the optimistic queued state + calls onChange); onChange (re-render hook).
export function overrideControlCell(opts) {
  const o = opts || {};
  const gid = String(o.gid || '');
  const cell = el('span', { class: 'dn-ovr-ctl', 'data-ovr-ctl': gid });
  // an override already recorded (durable readback OR the local optimistic
  // stamp) — the control is spent; show its state, no re-arm.
  const recorded = o.existingOverride || pendingOverride(gid);
  if (recorded) {
    cell.appendChild(el('span', { class: 'dn-ovr-spent dn-faint', text: 'overridden' }));
    return cell;
  }
  // a settled round takes no new override (the gate / field has resolved).
  if (o.settled) {
    cell.appendChild(el('span', { class: 'dn-ovr-na dn-faint', text: '—' }));
    return cell;
  }
  // read-only: a DISABLED control (visible, never POSTs).
  if (o.readOnly) {
    const btn = el('button', { class: 'dn-ovr-arm', type: 'button', disabled: 'disabled',
      title: 'read-only workspace — overrides are disabled' }, [el('span', { text: 'override' })]);
    cell.appendChild(btn);
    return cell;
  }

  // the live, two-step arm → reason → confirm flow. State lives on the wrapper
  // so a re-render rebuilds the disarmed cell (the registry carries the only
  // durable fact — the optimistic stamp).
  const reasonInput = el('input', { class: 'dn-ovr-reason', type: 'text',
    placeholder: 'reason (recorded)', 'aria-label': 'override reason' });
  let armedAction = null; // 'promote' | 'reject' while armed

  const fire = async (action) => {
    const reason = String(reasonInput.value || '').trim();
    let res = { ok: false, status: 0 };
    try { res = await (o.onPost ? o.onPost(action, gid, reason) : { ok: false, status: 0 }); }
    catch { res = { ok: false, status: 0 }; }
    if (res && res.ok) {
      markPendingOverride(gid, action, reason);
      if (o.onChange) o.onChange();
    } else if (res && res.status === 403) {
      // a workspace that flipped read-only between paint and POST — flag it.
      cell.setAttribute('data-ovr-error', 'read-only');
      const err = el('span', { class: 'dn-ovr-err dn-faint', text: 'read-only' });
      cell.appendChild(err);
    } else {
      cell.setAttribute('data-ovr-error', 'failed');
      const err = el('span', { class: 'dn-ovr-err dn-faint', text: 'failed' });
      cell.appendChild(err);
    }
  };

  const disarm = () => { armedAction = null; cell.setAttribute('data-armed', '0'); paint(); };
  const arm = () => { cell.setAttribute('data-armed', '1'); paint(); };

  function paint() {
    clearChildren(cell);
    if (cell.getAttribute('data-armed') === '1') {
      // the confirm row: promote ↑ / reject ✕ direction buttons + reason + cancel.
      const confirmPromote = el('button', { class: 'dn-ovr-confirm dn-ovr-promote', type: 'button',
        title: 'force-promote this challenger over the gate' }, [el('span', { text: 'promote ↑' })]);
      confirmPromote.addEventListener('click', () => { armedAction = 'promote'; fire('promote'); });
      const confirmReject = el('button', { class: 'dn-ovr-confirm dn-ovr-reject', type: 'button',
        title: 'force-reject this challenger against the gate' }, [el('span', { text: 'reject ✕' })]);
      confirmReject.addEventListener('click', () => { armedAction = 'reject'; fire('reject'); });
      const cancel = el('button', { class: 'dn-ovr-cancel', type: 'button', title: 'cancel' }, [el('span', { text: '×' })]);
      cancel.addEventListener('click', disarm);
      cell.appendChild(el('span', { class: 'dn-ovr-confirmrow' }, [reasonInput, confirmPromote, confirmReject, cancel]));
    } else {
      const armBtn = el('button', { class: 'dn-ovr-arm', type: 'button', title: 'operator override (force promote / reject)' },
        [el('span', { text: 'override' })]);
      armBtn.addEventListener('click', arm);
      cell.appendChild(armBtn);
    }
    void armedAction;
  }
  cell.setAttribute('data-armed', '0');
  paint();
  return cell;
}

export function stat(value, key) {
  return el('div', { class: 'dn-stat' }, [
    el('span', { class: 'v', text: value }),
    el('span', { class: 'k', text: key }),
  ]);
}

// ---- the candidate rating (visibility-only; never the gate) ----------
//
// The index-derived Bradley–Terry rating, served on the wire as the triple
// `elo` / `elo_se` / `elo_games` (lineage nodes + tournament standings). The
// server derives; these helpers only FORMAT (DQ1). Quiet-precision register:
// mono `1512 ±34`, a faint `provisional` suffix while the sample is thin,
// `—` when the fold has not rated the generation (zero settled duels, cold
// index, or the Rust lineage view, which omits the keys — absence reads as
// null). NO chips.

// THE one home of the provisionality floor (the views import it from here;
// the server never thresholds — provisionality is a display register, the
// per-candidate analogue of the per-pair MIN_CREDIBLE_DUELS honesty states
// in candidate.js's ratingBlock). Below this many settled duels the ridge
// prior still dominates the fit, so the number renders with the faint
// `provisional` suffix rather than as a settled strength.
export const MIN_RATING_GAMES = 5;

// Normalize a rating-bearing row ({elo, elo_se, elo_games}, keys optional)
// into a render model: null when unrated, else
// {text: '1512 ±34', elo, se, games, provisional}. Integer register — a
// rating is a legibility number, never false precision.
export function ratingModel(src) {
  if (!src || !isNum(src.elo)) return null;
  const eloInt = Math.round(src.elo);
  const se = isNum(src.elo_se) ? Math.round(src.elo_se) : null;
  const games = isNum(src.elo_games) ? src.elo_games : null;
  return {
    elo: eloInt, se, games,
    provisional: games != null && games < MIN_RATING_GAMES,
    text: String(eloInt) + (se != null ? ' ±' + se : ''),
  };
}

// A table-cell node for the rating column (standings + gens roster):
// mono value + faint `provisional` suffix; a plain '—' when unrated.
export function ratingCellEl(src) {
  const m = ratingModel(src);
  if (!m) return el('span', { class: 'dn-mono dt-rating', text: '—' });
  return el('span', { class: 'dt-rating' }, [
    el('span', { class: 'dn-mono', text: m.text }),
    m.provisional ? el('span', { class: 'dn-faint dt-rating-prov', text: ' provisional' }) : null,
  ].filter(Boolean));
}

// The digest fold for a rating TRIPLE (int convention — the render
// register): a [elo, se, games, provisional] tuple, or null when unrated so
// a pre-rating payload digests exactly like an unrated one. (Named for the
// per-candidate triple — candidate.js's ratingDigest is the separate
// per-PAIR gate-block digest.)
export function ratingTripleDigest(src) {
  const m = ratingModel(src);
  return m ? [m.elo, m.se, m.games, m.provisional] : null;
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

// ---- chip + hovercard-body builders ---------------------------------
//
// chip(cls, word) — the ONE `dn-chip dn-chip-<cls>` span builder, folding the
// five inlined `el('span', { class: 'dn-chip dn-chip-' + x, text: w })` sites.
// `cls` is the tone suffix (live / open / closed / a verdict class); `word` the
// label. An optional third `extra` string appends further classes verbatim (the
// `dn-looptraj-verdict` / `dn-div-penalized` modifier sites).
export function chip(cls, word, extra) {
  const c = 'dn-chip dn-chip-' + cls + (extra ? ' ' + extra : '');
  return el('span', { class: c, text: word == null ? '' : String(word) });
}

// pill(cls, word) — the `dn-pill dn-<cls>` span with a CUSTOM word. This is the
// sibling of verdictPill: verdictPill(decision) colours by a decision AND
// derives the label from it ('seed (v0)' / 'racing…'), whereas these sites want
// the decision COLOUR under a caller-chosen word (a role / 'champion' / a raw
// token) — swapping them onto verdictPill would silently rewrite that label. An
// optional `extra` appends further classes verbatim.
export function pill(cls, word, extra) {
  const c = 'dn-pill dn-' + cls + (extra ? ' ' + extra : '');
  return el('span', { class: c, text: word == null ? '' : String(word) });
}

// hovercardBody(...children) — the `dn-hc-body` wrapper the seven hovercard
// sites build by hand. Falsy children are dropped (matching the `.filter(Boolean)`
// some sites already applied); a single array argument is also accepted (the
// two `lines`-array sites).
export function hovercardBody(...children) {
  const kids = (children.length === 1 && Array.isArray(children[0])) ? children[0] : children;
  return el('div', { class: 'dn-hc-body' }, kids.filter(Boolean));
}

// ---- dataTable — the generic el()-based table builder ---------------
//
// Folds the ~14 hand-rolled thead/tbody scaffolds. `spec`:
//   * class    — the table's class (default 'dn-board-table')
//   * columns  — [{label, class?}] (falsy entries dropped, so a conditional
//                column composes via `cond && {label:…}`); `class` lands on the
//                <th> (e.g. 'dn-num').
//   * rows     — [row]; a row is either an array of cells OR
//                {class?, dataset?, style?, onClick?, cells:[…]} for a per-row
//                class / data-attrs / inline style / whole-row click handler.
//   * A CELL is one of: a string (plain <td> text); {text, class?} (a classed
//     text cell — the sign-coloured delta cell rides this shape via deltaCell);
//     {el, class?} (an el-returning cell factory — `el` may be one node or an
//     array, so a composed cell with a control/badge composes); null → empty <td>.
// No querySelector; strictly within the DOM-stub budget.
function _cellNode(c) {
  if (c == null) return el('td');
  if (typeof c === 'string' || typeof c === 'number') return el('td', null, [String(c)]);
  const attrs = {};
  if (c.class) attrs.class = c.class;
  if (c.title != null) attrs.title = c.title;
  const props = Object.keys(attrs).length ? attrs : null;
  if (c.el != null) return el('td', props, Array.isArray(c.el) ? c.el.filter(Boolean) : [c.el]);
  return el('td', props, [c.text == null ? '' : String(c.text)]);
}
export function dataTable(spec) {
  const s = spec || {};
  const cols = (s.columns || []).filter(Boolean);
  const thead = el('thead', null, [el('tr', null, cols.map((col) =>
    el('th', col.class ? { class: col.class } : null, [col.label == null ? '' : String(col.label)])))]);
  const rows = (s.rows || []).filter(Boolean);
  const tbody = el('tbody', null, rows.map((r) => {
    const cells = Array.isArray(r) ? r : (r.cells || []);
    const attrs = {};
    if (!Array.isArray(r)) {
      if (r.class) attrs.class = r.class;
      if (r.dataset) attrs.dataset = r.dataset;
      if (r.style) attrs.style = r.style;
      if (typeof r.onClick === 'function') attrs.onclick = r.onClick;
    }
    // Falsy cells are DROPPED (matching the hand tables' `.filter(Boolean)` for
    // conditional columns) — an intentionally blank cell is `{text:''}` as an
    // object, which survives the filter.
    return el('tr', Object.keys(attrs).length ? attrs : null, cells.filter(Boolean).map(_cellNode));
  }));
  return el('table', { class: s.class || 'dn-board-table' }, [thead, tbody]);
}

// deltaCell(value) — the sign-coloured delta cell spec (gens / publication /
// candidate matchup rows). Positive → `dn-bad-t` (a regression), negative →
// `dn-good-t` (an improvement), non-finite → neither. Returns a {class, text}
// cell for dataTable; `opts.text` overrides the rendered label (some sites show
// a pre-formatted string), `opts.base` sets the base class (default 'dn-num').
export function deltaCell(value, opts) {
  const o = opts || {};
  const cls = [o.base || 'dn-num'];
  if (isNum(value)) { if (value > 0) cls.push(o.badClass || 'dn-bad-t'); else if (value < 0) cls.push(o.goodClass || 'dn-good-t'); }
  return { class: cls.join(' '), text: o.text != null ? o.text : fmtSigned(value) };
}

// ---- loop-communication helpers (pure — moved here from home.js) -----
//
// These read one epoch's /api/epoch/{id}/trajectory (+ cost) and shape the
// verdict chip / promotion-rate / cost / noise-band the fleet cards AND the
// epoch view both render. They lived in home.js, which made epoch.js import
// UPWARD from a sibling view (a reverse dependency); the shared home is ui.js.
// The verdict words BOTH the fleet card and the epoch panel print. Only the
// words that report a problem live here: "improving" / "warming_up" return
// null so a healthy or undecided loop stays quiet on the fleet card (the epoch
// panel adds "improving" on top — see epoch.js verdictLine). "stalled" is the
// no-floor sibling of "no_signal": challengers fielded, none promoted, and no
// A/A floor measured, so the phrase names the promotions that did not happen
// and claims nothing about noise.
export function loopVerdict(traj) {
  const v = traj && typeof traj === 'object' ? traj.verdict : null;
  if (v === 'no_signal') return { word: 'no detectable signal (below noise floor)', cls: 'nosignal' };
  if (v === 'stalled') return { word: 'stalled (no promotions)', cls: 'stalled' };
  if (v === 'plateaued') return { word: 'plateaued', cls: 'plateau' };
  return null;
}
export function promotionRateLabel(traj) {
  if (!traj || typeof traj !== 'object') return null;
  if (!isNum(traj.promotion_rate) || !(traj.challenger_count > 0)) return null;
  return (traj.promoted_count || 0) + '/' + traj.challenger_count
    + ' · ' + Math.round(traj.promotion_rate * 100) + '%';
}
export function costPerPromotionLabel(cost) {
  const v = cost && typeof cost === 'object' ? cost.cost_per_promotion_ms : null;
  return isNum(v) ? fmtDurationMs(v) : null;
}
export function fmtDurationMs(ms) {
  if (!isNum(ms) || ms < 0) return '—';
  if (ms < 1000) return Math.round(ms) + 'ms';
  const s = ms / 1000;
  if (s < 90) return (Math.round(s * 10) / 10) + 's';
  const m = s / 60;
  if (m < 90) return (Math.round(m * 10) / 10) + 'm';
  return (Math.round((m / 60) * 10) / 10) + 'h';
}
export function noiseBandFor(traj, sparkVals) {
  const nf = traj && typeof traj === 'object' ? traj.noise_floor : null;
  const half = nf && isNum(nf.max_abs_delta) ? nf.max_abs_delta : null;
  const vals = (Array.isArray(sparkVals) ? sparkVals : []).filter((v) => isNum(v));
  if (!isNum(half) || half <= 0 || !vals.length) return null;
  return { center: vals[vals.length - 1], half };
}
export function loopStatsDigest(traj, cost) {
  return [
    traj && traj.verdict ? String(traj.verdict) : null,
    traj && isNum(traj.promotion_rate) ? traj.promotion_rate.toFixed(3) : null,
    traj && isNum(traj.challenger_count) ? traj.challenger_count : null,
    traj && isNum(traj.promoted_count) ? traj.promoted_count : null,
    (traj && traj.noise_floor && isNum(traj.noise_floor.max_abs_delta))
      ? traj.noise_floor.max_abs_delta.toFixed(4) : null,
    cost && isNum(cost.cost_per_promotion_ms) ? Math.round(cost.cost_per_promotion_ms) : null,
  ];
}

// ---- renderView — the ONE view scaffold ----------------------------
//
// The eleven data views repeated the same opening: paint a first-paint
// placeholder into an empty host, (optionally) resolve the route epoch via
// D.epoch and gate on its absence, fetch the view's data, fold a content
// digest, and gatedSwap. renderView captures that spine; a view supplies only
// the parts that differ via `spec`:
//   * loading   — first-paint placeholder text (uses the orphaned loading()).
//   * epoch     — truthy ⇒ resolve `await D.epoch(spec.routeEpoch ?? null)` and
//                 gate: an absent epoch (falsy or epoch_id == null) paints
//                 [h1(title), empty(emptyText)] under a stable 'no-epoch' digest
//                 and returns. `spec.routeEpoch` is the caller's resolved route
//                 param (null ⇒ current epoch); the resolved id is passed on.
//   * title / emptyText — the no-epoch gate's heading + line.
//   * guard({ep, epochId}) — OPTIONAL secondary gate run after the epoch
//                 resolves: return `{digest, build}` to paint that gate + stop
//                 (e.g. board.js's "no entry selected"), or null to proceed.
//   * load(ctxObj) — async; returns the view's `data`. ctxObj carries
//                 {ep, epochId, ctx, params}. A no-epoch view (epoch falsy)
//                 gets {ep:null, epochId:null, …}.
//   * digest(data) — the content digest (timestamp-free) for gatedSwap.
//   * build(data)  — the node(s) to paint. Passed straight to gatedSwap.
// A view whose flow genuinely diverges (parallel-fused fetches, multiple hosts,
// a non-epoch gate) keeps its hand-rolled scaffold.
export async function renderView(host, ctx, spec) {
  if (!host) return;
  const s = spec || {};
  if (!host.firstChild) host.appendChild(loading(s.loading));
  let ep = null;
  let epochId = null;
  if (s.epoch) {
    const routeEpoch = s.routeEpoch != null ? s.routeEpoch : null;
    ep = await D.epoch(routeEpoch);
    if (!ep || ep.epoch_id == null) {
      gatedSwap(host, s.emptyDigest || 'no-epoch', () =>
        [el('h1', { class: 'dn-h1', text: s.title || 'Epoch' }), empty(s.emptyText || 'No current epoch.')]);
      return;
    }
    epochId = routeEpoch != null ? routeEpoch : ep.epoch_id;
  }
  if (s.guard) {
    const g = s.guard({ ep, epochId });
    if (g) { gatedSwap(host, g.digest, g.build); return; }
  }
  const data = await s.load({ ep, epochId, ctx, params: s.params || null });
  gatedSwap(host, s.digest(data), () => s.build(data));
}

export { href };
