// variants/T/shell.js — the Console III shell: a data-model TREE sidebar +
// one persistent detail pane, with digest-gated dispatch.
//
// Variant P ("Console III") is the direct successor to Variant N — the same
// dense, data-ink-maximal aesthetic (Monokai default + Technical typeface) —
// but it REPLACES N's top-tab nav with a persistent, collapsible LEFT TREE
// grounded in the real data model: Environment → Epoch(s) → {Generations →
// <gen>; Boards → <entry>; Mutation surface; Publication}. Selecting any tree
// node drives the single detail pane. The tree navigates MULTIPLE epochs AND
// MULTIPLE generations (N could not). Selection is explicit + URL-encoded, so
// a cold deep-link hydrates BOTH the open tree branches and the detail pane.
//
// The shell owns:
//   * a COMPACT top bar — branding · breadcrumb · colour-theme picker (monokai
//     default) · page-scale pill · status pill. The TYPEFACE picker lives in
//     Settings → Appearance now (not the top bar), driving the same store;
//   * the persistent tree sidebar (its own digest gate);
//   * ONE persistent detail host (never recreated per repaint);
//   * digest-gated dispatch — a `state:changed` tick that only re-stamps a
//     heartbeat writes ZERO DOM to either the tree or the detail pane.
//
// Theme + typeface are CSS-only swaps (data-t-theme / data-t-type on the root).

import { el, svgEl, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { harmonografMetaUrl } from '../../core/harmonograf.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail, up } from './router.js';
import * as D from './data.js';
import { invalidateLive, liveDataSignature } from './data.js';
import { buildTree, treeDigest } from './tree.js';
import { normaliseDecision } from './ui.js';
import { roundsForTree } from './views/rounds.js';
import { deriveLiveStatus, liveStatusDigest, treeLiveSet, staleLabel } from './livestatus.js';
import { LiveController } from './live.js';
import { buildSwatchDropdown, syncSwatchDropdowns } from './swatchdropdown.js';
import { syncTypefaceDropdowns } from './typefacedropdown.js';
import {
  COLOR_THEMES, DEFAULT_COLOR, normaliseColor, readColor, persistColor,
  TYPE_THEMES, DEFAULT_TYPE, normaliseType, readType, persistType,
  DENSITY,
  SCALE_MIN, SCALE_MAX, SCALE_STEP, DEFAULT_SCALE, normaliseScale, readScale, persistScale,
  RAIL_MIN, RAIL_MAX, DEFAULT_RAIL, normaliseRail, readRail, persistRail, pageScaleOf,
} from './ui.js';

import * as home from './views/home.js';
import * as epoch from './views/epoch.js';
import * as gens from './views/gens.js';
import * as candidate from './views/candidate.js';
import * as diff from './views/diff.js';
import * as boards from './views/boards.js';
import * as board from './views/board.js';
import * as mutations from './views/mutations.js';
import * as publication from './views/publication.js';
import * as builder from './views/builder.js';
import * as settings from './views/settings.js';

const RENDERERS = { home, epoch, gens, candidate, diff, boards, board, mutations, publication, builder, settings };

export const THEMES = COLOR_THEMES.map((t) => t[0]);
export const TYPEFACES = TYPE_THEMES.map((t) => t[0]);

const KIND_TAG = {
  single_turn: '1-turn', multi_turn_scripted: 'scripted', multi_turn_emulated: 'emulated',
};

let _root = null;
let _viewHost = null;
let _treeHost = null;
let _crumbHost = null;
let _statusEl = null;
let _statusTextEl = null;     // the connection word (live/connecting/offline)
let _runLabelEl = null;       // the structure+phase run label
let _runCountEl = null;       // the in-flight board-unit count
let _staleEl = null;          // the "last seen Ns ago / stale" affordance
let _colorDropdown = null;     // the swatch-dropdown controller (Change 6)
let _typeEl = [];
let _scaleInput = null;
let _scaleReadout = null;
let _railHandle = null;        // the draggable rail-resize handle (Change 2)
let _railDragging = false;     // true while a live rail drag is in flight
let _backBtn = null;
let _execHost = null;          // top-bar host for the zicato-level harmonograf link
let _lastExecHref = null;      // digest gate for the execution link (no-beat churn)
let _renderToken = 0;
let _live = null;             // the persistent LIVE-RUN controller (live hero + ticker)
let _heroHost = null;         // the persistent host the live hero leads from
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
let _lastTreeDigest = null;
// The signature of the live data AppState last folded in from /api/environment
// (gen set + statuses + epoch roster). A change means a candidate was added /
// settled, so the stale drill-down cache must be busted + the views repainted.
// Unchanged ⇒ a no-op beat ⇒ zero cache busts (no flash, no extra fetches).
let _lastLiveSig = null;
const _toggles = new Set();
const _ctx = { navigate, href };

// THE BACK-BUTTON FIX. The top-left back/up control navigates UP the selection
// hierarchy. Q's back button was buggy: it rendered the destination into the
// SIDE PANEL. T's back control instead navigates (changing the route) so the
// normal dispatch repaints the destination into the MAIN DETAIL PANE — the
// tree/rail host is never touched. `goBack(route)` is exported so a test can
// drive it and assert the destination landed in the detail host (not the rail).
export function goBack(route) {
  const r = route || parseRoute(location.hash);
  const dest = up(r);
  if (!dest) return false;
  navigate(dest.view, dest.params, dest.cmp ? { cmp: dest.cmp } : undefined);
  return true;
}

export function applyTheme(theme, rootEl) {
  const t = normaliseColor(theme);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-t-theme', t);
  persistColor(t);
  // Sync EVERY live swatch dropdown (top bar AND settings) — one source of truth.
  syncSwatchDropdowns(t);
  return t;
}

export function applyTypeface(typeface, rootEl) {
  const t = normaliseType(typeface);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-t-type', t);
  persistType(t);
  // Sync EVERY live typeface dropdown (top bar AND settings) — one source of
  // truth, so choosing in either place lockstep-updates the other.
  syncTypefaceDropdowns(t);
  // Legacy: keep any old button-group refs in lockstep (now an empty no-op list).
  for (const b of _typeEl) patchClass(b, 'dt-type-active', b.getAttribute('data-type') === t);
  return t;
}

// PAGE-WIDE SCALE — the draggable scale pill. Distinct from density: this is a
// single master multiplier on the ENTIRE page (text AND diagrams), applied as
// `zoom` on the Variant-T app ROOT (NOT per-pane). `zoom` reflows rather than
// transforms, so the layout re-wraps at the scaled size and never clips. We
// also stamp `--dt-page-scale` (a 0–1 ratio) for any rule that wants the raw
// factor. Persisted under its own key, so it composes with — and survives —
// density / colour / typeface changes. Drives the pill + its % readout when
// called programmatically (e.g. on restore or via the keyboard).
export function applyScale(scale, rootEl) {
  const n = normaliseScale(scale);
  const root = rootEl || _root;
  if (root) {
    const ratio = n / 100;
    root.style.zoom = String(ratio);
    root.style.setProperty('--dt-page-scale', String(ratio));
    root.setAttribute('data-t-scale', String(n));
  }
  persistScale(n);
  if (_scaleInput) {
    if (_scaleInput.value !== String(n)) _scaleInput.value = String(n);
    _scaleInput.setAttribute('value', String(n));
    _scaleInput.setAttribute('aria-valuenow', String(n));
  }
  if (_scaleReadout) patchText(_scaleReadout, n + '%');
  return n;
}

// RESET the page scale back to 100% (DEFAULT_SCALE) + persist. Backs the small
// reset affordance beside the scale pill (Change 4); keyboard-accessible (it is
// a real <button>). Returns the applied value.
export function resetScale(rootEl) {
  return applyScale(DEFAULT_SCALE, rootEl || _root);
}

// LEFT SIDE-PANEL (rail) WIDTH — set the `--dt-rail` grid column on the app root
// + persist. Distinct from the page-scale pill (this resizes ONLY the tree
// side-panel; the detail pane's 1fr column reflows to fill the rest). Backs the
// draggable handle on the rail's right edge; clamped to a sensible min/max so
// the rail can never collapse or eat the page. Returns the applied px width.
export function applyRail(px, rootEl) {
  const n = normaliseRail(px);
  const root = rootEl || _root;
  // SNAP-BACK GUARD: while a live drag is in flight, any re-render path that
  // tries to re-apply the PERSISTED width (e.g. a `state:changed` tick) must
  // NOT clobber the live drag value. The drag itself sets the width via
  // stampRail (not applyRail), so this guard only fires on a competing caller.
  if (_railDragging) return readRailFromRoot(root);
  stampRail(n, root);
  persistRail(n);
  return n;
}

// Write the clamped rail width to the root (the `--dt-rail` grid column + the
// mirrored attribute + the handle's aria-valuenow) WITHOUT persisting. The live
// drag uses this on every `pointermove` so the rail tracks the pointer with no
// per-frame localStorage churn; the final width is persisted once on pointerup.
function stampRail(px, rootEl) {
  const n = normaliseRail(px);
  const root = rootEl || _root;
  if (root) {
    root.style.setProperty('--dt-rail', n + 'px');
    root.setAttribute('data-t-rail', String(n));
  }
  if (_railHandle) _railHandle.setAttribute('aria-valuenow', String(n));
  return n;
}

// Read back the rail width currently stamped on the root (the live `--dt-rail`
// or its mirrored attribute) — used by the pointerup fallback when the up event
// carries no clientX so we persist whatever the last live move stamped.
function readRailFromRoot(rootEl) {
  const root = rootEl || _root;
  if (root) {
    if (root.getAttribute) {
      const a = root.getAttribute('data-t-rail');
      if (a != null) return normaliseRail(a);
    }
    const st = root.style;
    if (st && typeof st.getPropertyValue === 'function') {
      const v = st.getPropertyValue('--dt-rail');
      if (v) return normaliseRail(parseFloat(v));
    }
  }
  return readRail();
}

// Wire the rail-resize handle (Change 2). THE JUMPINESS FIX.
//
// Two bugs made the old drag jump:
//   (1) ZOOM MISMATCH. The handle lives inside the app root, which carries a
//       page-wide `zoom` (the scale pill). `event.clientX` is a VIEWPORT CSS-px
//       coordinate, but `--dt-rail` is laid out in the root's UNSCALED layout
//       space. The old code set the width straight from `clientX − railLeft`,
//       so at zoom ≠ 1 the width over-/under-tracked the pointer (it was then
//       re-multiplied by `zoom` on render) → visible jump. We now work in DELTA
//       space and divide the pointer delta by the live page-scale factor, so a
//       given pointer travel maps 1:1 onto layout-space rail travel at ANY zoom.
//   (2) LOST POINTER EVENTS. Without pointer capture, a fast drag (or sliding
//       off the 4-px handle) drops `pointermove`s → the rail stutters. We now
//       `setPointerCapture` on pointerdown so every move is delivered to the
//       handle until pointerup.
//
// The drag records the start pointer-x + start width on pointerdown, sets the
// width LIVE (no persist) on each move = clamp(startW + Δx/scale), and persists
// once on pointerup. A `_railDragging` guard stops any mid-drag re-render from
// snapping the width back to the persisted value. Arrow keys nudge ±16
// (Home/End jump to min/max). Defensive so the harness (no real layout / no
// PointerEvent) can drive it via keyboard or a synthetic pointer event.
function wireRailHandle(handle, root) {
  if (!handle) return;
  const RAIL_KEY_STEP = 16;
  let pointerId = null;
  let startX = 0;
  let startW = readRail();

  const onMove = (ev) => {
    if (!_railDragging) return;
    if (ev && ev.preventDefault) ev.preventDefault();
    const cx = ev && typeof ev.clientX === 'number' ? ev.clientX : null;
    if (cx == null) return;
    // Divide the VIEWPORT-px pointer delta by the page-scale factor so it maps
    // onto the root's UNSCALED layout space (where `--dt-rail` lives). At 100%
    // scale this is an identity; at any other scale it is what stops the jump.
    const scale = pageScaleOf(root) || 1;
    const next = startW + (cx - startX) / scale;
    stampRail(next, root);          // live, no persist (avoid churn / snap-back)
  };
  const onUp = (ev) => {
    if (!_railDragging) return;
    _railDragging = false;
    if (handle.classList) handle.classList.remove('dt-rail-dragging');
    if (pointerId != null && typeof handle.releasePointerCapture === 'function') {
      try { handle.releasePointerCapture(pointerId); } catch (e) { /* ignore */ }
    }
    pointerId = null;
    // Persist the final width (clamped) exactly once.
    if (ev && typeof ev.clientX === 'number') {
      const scale = pageScaleOf(root) || 1;
      applyRail(startW + (ev.clientX - startX) / scale, root);
    } else {
      applyRail(readRailFromRoot(root), root);
    }
    if (typeof window !== 'undefined' && window.removeEventListener) {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    }
  };
  const onDown = (ev) => {
    _railDragging = true;
    startX = ev && typeof ev.clientX === 'number' ? ev.clientX : 0;
    startW = readRail();
    if (handle.classList) handle.classList.add('dt-rail-dragging');
    if (ev && ev.preventDefault) ev.preventDefault();
    // Capture the pointer so EVERY move is delivered to the handle even if the
    // cursor outruns the thin hit-area (this is the lost-events fix).
    const pid = ev && (ev.pointerId != null ? ev.pointerId : 0);
    if (pid != null && typeof handle.setPointerCapture === 'function') {
      try { handle.setPointerCapture(pid); pointerId = pid; } catch (e) { pointerId = null; }
    }
    // With capture, the handle itself receives the moves; also bind on window
    // as a belt-and-braces fallback (mouse path / no-capture environments).
    if (typeof window !== 'undefined' && window.addEventListener) {
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
    }
  };
  handle.addEventListener('pointerdown', onDown);
  handle.addEventListener('mousedown', onDown);
  // Captured pointer events fire on the handle itself; route them through too.
  handle.addEventListener('pointermove', onMove);
  handle.addEventListener('pointerup', onUp);

  handle.addEventListener('keydown', (ev) => {
    const k = ev.key;
    const cur = readRail();
    if (k === 'ArrowLeft') { ev.preventDefault && ev.preventDefault(); applyRail(cur - RAIL_KEY_STEP, root); }
    else if (k === 'ArrowRight') { ev.preventDefault && ev.preventDefault(); applyRail(cur + RAIL_KEY_STEP, root); }
    else if (k === 'Home') { ev.preventDefault && ev.preventDefault(); applyRail(RAIL_MIN, root); }
    else if (k === 'End') { ev.preventDefault && ev.preventDefault(); applyRail(RAIL_MAX, root); }
  });
}

// ---- the colour SWATCH DROPDOWN (Change 6) --------------------------
//
// The swatch dropdown is now the SHARED component in ./swatchdropdown.js, used
// IDENTICALLY by the top bar (below) and Settings → Appearance — not forked.
// We pass applyTheme as the onChoose, so choosing in EITHER place applies +
// persists + syncs every live instance via syncSwatchDropdowns (one store).

// THE BRAND MARK — the zicato logo as INLINE SVG in the top bar (never an
// <img>: an external image can't inherit `currentColor`).
//
// SIZE RULE. The canonical mark is the full golden-spiral line art (one
// continuous stroke — scroll → string → pluck → damped-sine sparkline → bridge
// tick). That construction is glorious at ≥24px but MUDDIES into noise below it
// — the fine spiral whorl and the sparkline ripples collapse at small raster
// rule we render the FULL SPIRAL here at ~26px (`.dt-brand-mark { height:26px }`
// in console4.css), comfortably above the ~24px floor, so the scroll, pluck,
// sparkline, and bridge all read. Operator's call: keep the spiral in the chrome
// rather than swap to the bold-z. The bold-z favicon form still ships for sub-24px
// raster uses (docs/brand/zicato-favicon.svg), where the fine spiral muddies.
// It is the exact geometry of docs/brand/zicato-mark.svg: stroke 5.0 with the
// single green plucked-note dot (r 5.5) at the pluck vertex. Theme-adaptive: the
// stroke uses `currentColor` so it follows the bar's text colour (dark/light with
// the theme), and the dot fills `var(--zicato-accent)` (per-theme). Built ONCE as
// static chrome, never rebuilt on an SSE heartbeat (digest discipline).
const _MARK_SPIRAL_PATH = 'M94,52.5 L93.9,52.7 L93.7,52.9 L93.6,53.1 L93.5,53.4 L93.5,53.7 L93.4,54 L93.4,54.3 L93.5,54.6 L93.6,55 L93.7,55.3 L93.9,55.6 L94.1,55.9 L94.4,56.2 L94.7,56.5 L95.1,56.7 L95.4,56.9 L95.9,57.1 L96.3,57.2 L96.8,57.2 L97.3,57.2 L97.9,57.2 L98.4,57 L98.9,56.8 L99.4,56.5 L99.9,56.2 L100.4,55.8 L100.9,55.3 L101.2,54.7 L101.6,54.1 L101.8,53.4 L102,52.6 L102.1,51.9 L102.1,51 L102,50.2 L101.8,49.3 L101.5,48.5 L101.1,47.6 L100.5,46.8 L99.9,46 L99.1,45.3 L98.2,44.7 L97.2,44.1 L96.1,43.7 L94.9,43.4 L93.6,43.2 L92.3,43.2 L91,43.3 L89.6,43.6 L88.2,44.1 L86.8,44.8 L85.5,45.6 L84.2,46.7 L83,47.9 L82,49.3 L81.1,50.9 L80.3,52.7 L79.8,54.6 L79.5,56.6 L79.4,58.7 L79.5,60.9 L80,63.2 L80.7,65.4 L81.8,67.6 L83.1,69.8 L84.8,71.9 L86.7,73.8 L89,75.5 L91.5,77 L94.3,78.3 L97.3,79.2 L100.6,79.8 L104,80 L150,80 L170,102 L190,80 Q206,56 222,80 Q236,102 250,80 Q261,68 272,80 L292,80';
const _MARK_BRIDGE_PATH = 'M292,66 L292,94';

function brandMark() {
  const stroke = svgEl('g', {
    fill: 'none', stroke: 'currentColor', 'stroke-width': '5.0',
    'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  }, [
    svgEl('path', { d: _MARK_SPIRAL_PATH }),
    svgEl('path', { d: _MARK_BRIDGE_PATH }),
  ]);
  const dot = svgEl('circle', { cx: '170', cy: '102', r: '5.5', fill: 'var(--zicato-accent)' });
  return svgEl('svg', {
    class: 'dt-brand-mark', viewBox: '71 35 229 75',
    role: 'img', 'aria-label': 'zicato', focusable: 'false',
  }, [stroke, dot]);
}

// THE WORDMARK — "zıcato" with a DOTLESS ı (U+0131) carrying the green accent
// dot over its stem, matching docs/brand/zicato-lockup.svg. The wordmark is an
// inline SVG (not a styled text span): an external/text dot can't be pinned to
// the glyph stem and can't inherit `currentColor` for the letters + the accent
// token for the dot at once. Centering was a prior pain point, so the dot is
// pinned GEOMETRICALLY rather than by eye:
//
//   * the letters render in a FIXED BRAND MONOSPACE (`--v2-brand-mono`) — NOT
//     the user-selectable `--v2-mono` — so every glyph has the SAME advance
//     width AND the brand never reflows with the UI typeface choice; the ı stem
//     center is therefore deterministic, theme-independent, and not subject to
//     per-glyph kerning;
//   * with a left text anchor at x=WORDMARK_X0 and a per-glyph advance of
//     WORDMARK_ADV, the centre of the i-th glyph is x0 + (i + 0.5)·adv. The ı is
//     index 1 ("z" is 0), so its stem centre — and the dot cx — is
//     `wordmarkDotCx()` below. The export lets a unit test assert the dot's
//     x-position EQUALS that computed stem centre (the centering guarantee).
//
// Theme-adaptive: the text fills with `currentColor` (flips dark/light with the
// theme); the dot fills with `var(--zicato-accent)`. Built ONCE as static
// chrome — never rebuilt on an SSE heartbeat (digest discipline).
export const WORDMARK_TEXT = 'zıcato';   // z + dotless ı + cato
const WORDMARK_X0 = 1;       // left text anchor (viewBox units)
const WORDMARK_ADV = 9.6;    // per-glyph advance for the monospace face
const WORDMARK_BASELINE = 15;
const WORDMARK_DOT_CY = 3.0; // the accent dot sits above the x-height
const WORDMARK_DOT_R = 1.7;
const WORDMARK_DOTLESS_I_INDEX = 1; // "z"=0, "ı"=1

// The geometric centre of the dotless ı stem (= the accent dot cx). Pinned to
// the monospace advance grid so the dot is PERFECTLY centred over the stem.
export function wordmarkDotCx() {
  return WORDMARK_X0 + (WORDMARK_DOTLESS_I_INDEX + 0.5) * WORDMARK_ADV;
}

function brandWordmark() {
  const w = WORDMARK_X0 * 2 + WORDMARK_TEXT.length * WORDMARK_ADV;
  const text = svgEl('text', {
    class: 'dt-brand-letters',
    x: String(WORDMARK_X0), y: String(WORDMARK_BASELINE),
    // PIN to the FIXED brand mono (not the user-selectable --v2-mono) so the dot
    // stays centred on the advance grid regardless of the chosen UI typeface.
    'font-family': 'var(--v2-brand-mono)', 'font-size': '15', 'font-weight': '700',
    'letter-spacing': '0', 'textLength': String(WORDMARK_TEXT.length * WORDMARK_ADV),
    'lengthAdjust': 'spacing', fill: 'currentColor', 'xml:space': 'preserve',
  });
  text.textContent = WORDMARK_TEXT;
  const dot = svgEl('circle', {
    class: 'dt-brand-dot',
    cx: String(wordmarkDotCx()), cy: String(WORDMARK_DOT_CY), r: String(WORDMARK_DOT_R),
    fill: 'var(--zicato-accent)',
  });
  return svgEl('svg', {
    class: 'dt-brand-name', viewBox: `0 0 ${w} 18`,
    role: 'img', 'aria-label': 'zicato', focusable: 'false',
  }, [text, dot]);
}

// THE RESEARCH-PREVIEW NOTE — a quiet, app-wide product-status mark pinned to
// the LOWER-RIGHT of the chrome (position:fixed, bottom-right). It is the OPPOSITE
// of the old accent-tinted, pulsing Settings card: a small, faint, monospace
// lowercase "research preview" label in the understated idiom of the existing
// captions/pills (the muted ink-faint token + currentColor, theme-adaptive across
// all themes), with a tiny static accent dot — NO glow, NO pulse, NO animation. It
// is purely informational, so the whole note is `pointer-events: none` and never
// blocks a click. Mounted ONCE in the shell so it persists across every view;
// static chrome, never rebuilt on an SSE heartbeat (digest discipline). The
// styling lives in console4.css (.dt-respreview + children).
function researchPreviewNote() {
  return el('div', { class: 'dt-respreview', role: 'note', 'aria-label': 'research preview' }, [
    el('span', { class: 'dt-respreview-dot', 'aria-hidden': 'true' }),
    el('span', { class: 'dt-respreview-text', text: 'research preview' }),
  ]);
}

export function mountShell(root) {
  _root = root;
  clearChildren(root);
  _toggles.clear();
  _lastTreeDigest = null;
  // Reset the live-data signature: a fresh mount must treat the first
  // post-mount environment fold as a change so the initial cache busts cleanly.
  _lastLiveSig = null;
  // Reset the exec-link digest: mountShell rebuilds the top bar (a fresh,
  // empty _execHost), so a stale digest must not skip the first paint.
  _lastExecHref = null;
  root.setAttribute('data-variant', 'T');
  root.setAttribute('data-t-theme', readColor());
  root.setAttribute('data-t-type', readType());
  // density is fixed at the cozy baseline (no picker) — stamp it for any rule
  // that still keys on it, but it never changes.
  root.setAttribute('data-t-density', DENSITY);
  root.setAttribute('data-t-scale', String(readScale()));

  _crumbHost = el('nav', { class: 'dt-crumbs', 'aria-label': 'Breadcrumb' });

  // COLOUR PICKER — a SWATCH DROPDOWN (Change 6). Sixteen themes now, so the inline
  // buttons are replaced by a keyboard-accessible dropdown: each option shows a
  // small swatch strip (ground · surface · ink · improve · regress · accent) plus
  // the theme name; the closed control echoes the current theme's swatch + name.
  _colorDropdown = buildSwatchDropdown(readColor(), (id) => applyTheme(id));
  const colorSwitch = _colorDropdown.node;

  // TYPEFACE PICKER — REMOVED from the top-bar chrome (operator's call): it now
  // lives ONLY in Settings → Appearance (views/settings.js's typefacePicker).
  // The store + sync path is unchanged: applyTypeface still calls
  // syncTypefaceDropdowns, which fans out to every LIVE dropdown via the
  // typefacedropdown.js instance registry — now just the Settings one — so
  // choosing a face in Settings still applies live + persists, and any other
  // apply path (keyboard / restore) keeps the Settings picker in lockstep.
  // `_typeEl` stays empty (the old button-group sync loop is now a no-op).
  _typeEl = [];

  // The PAGE-WIDE SCALE pill: a draggable range slider that scales the WHOLE
  // page (text + diagrams) via `zoom` on the app root. With density removed this
  // is the sole sizing control. Keyboard-accessible (a native range input:
  // arrows step ±5); a % readout + a RESET button (→ 100%) sit beside it.
  const initialScale = readScale();
  _scaleInput = el('input', {
    class: 'dt-scale-range', type: 'range',
    min: String(SCALE_MIN), max: String(SCALE_MAX), step: String(SCALE_STEP),
    value: String(initialScale),
    'aria-label': 'Page scale (whole-page size)',
    'aria-valuemin': String(SCALE_MIN), 'aria-valuemax': String(SCALE_MAX),
    'aria-valuenow': String(initialScale),
    title: 'Page scale — overall page size (text + diagrams); composes with density',
  });
  _scaleReadout = el('span', { class: 'dt-scale-readout', text: initialScale + '%' });
  // Read the live value from the event target / input — fall back to the
  // `value` attribute so a synthetic event (and the test harness, whose range
  // input exposes `value` only as an attribute) still drives the scale.
  const onScale = (ev) => {
    const raw = (ev && ev.target && ev.target.value != null) ? ev.target.value
      : (_scaleInput.value != null ? _scaleInput.value : _scaleInput.getAttribute('value'));
    applyScale(raw);
  };
  _scaleInput.addEventListener('input', onScale);
  _scaleInput.addEventListener('change', onScale);
  // RESET affordance (Change 4): a real <button> beside the pill that snaps the
  // page scale back to 100% and persists. A button is inherently keyboard-
  // accessible (focusable + Enter/Space activate).
  const scaleReset = el('button', {
    class: 'dt-scale-reset', type: 'button',
    title: 'Reset page scale to 100%', 'aria-label': 'Reset page scale to 100%',
    text: '⟲',
  });
  scaleReset.addEventListener('click', () => resetScale());
  const scalePill = el('div', { class: 'dt-scale-pill', role: 'group', 'aria-label': 'Page scale', title: 'Page scale — overall page size' }, [
    el('span', { class: 'dt-scale-lab', text: 'scale', 'aria-hidden': 'true' }),
    _scaleInput,
    _scaleReadout,
    scaleReset,
  ]);

  // The live-status pill: a connection dot + the connection word, plus a
  // RUN badge that lights up whenever the loop is active for ANY tournament
  // structure (read from the live APIs in renderStatus, not the gauntlet-only
  // activeTournament). The run badge carries the structure + phase label and an
  // in-flight board-unit count; it is hidden when idle/done.
  _statusTextEl = el('span', { class: 'dt-status-text', text: 'connecting…' });
  _runLabelEl = el('span', { class: 'dt-run-label', text: '' });
  _runCountEl = el('span', { class: 'dt-run-count', text: '' });
  // The stale affordance: when a heartbeat exists but is FROZEN (older than
  // the staleness window), the run is no longer live — surface "last seen Ns
  // ago" / "stale" rather than silently freezing the live chrome.
  _staleEl = el('span', { class: 'dt-status-stale', 'aria-live': 'polite', text: '' });
  _statusEl = el('span', { class: 'dt-status' }, [
    el('span', { class: 'dt-status-dot' }),
    _statusTextEl,
    el('span', { class: 'dt-run-badge', 'aria-live': 'polite' }, [
      el('span', { class: 'dt-run-pulse', 'aria-hidden': 'true' }),
      _runLabelEl,
      _runCountEl,
    ]),
    _staleEl,
  ]);

  // top-left UP control — navigates UP the selection hierarchy (the parent
  // route); dispatch then repaints the destination into the MAIN detail pane
  // (never the sidebar). Labelled "↑ up" to reflect its function (it climbs the
  // hierarchy, NOT browser-back).
  _backBtn = el('button', { class: 'dt-back', type: 'button', title: 'Navigate up one level', 'aria-label': 'Navigate up' }, [
    el('span', { class: 'dt-back-glyph', 'aria-hidden': 'true', text: '↑' }),
    el('span', { class: 'dt-back-text', text: 'up' }),
  ]);
  _backBtn.addEventListener('click', () => goBack(parseRoute(location.hash)));

  const topbar = el('header', { class: 'dt-topbar' }, [
    _backBtn,
    el('div', { class: 'dt-brand' }, [
      brandMark(),
      brandWordmark(),
      el('span', { class: 'dt-brand-variant', text: 'console' }),
    ]),
    _crumbHost,
    el('span', { class: 'dt-topbar-spacer' }),
    // the ZICATO-LEVEL harmonograf entry — a liveness-gated "execution ▸"
    // link into the meta-loop session (the proposer + judge timeline of the
    // evolution itself). Filled by renderExecLink, digest-gated so a no-op
    // heartbeat never repaints it. See docs/design/HARMONOGRAF.md §3b.
    (_execHost = el('span', { class: 'dt-nav-exec', 'aria-live': 'polite' })),
    // the SETTINGS entry (B3) — a ⚙ that opens the Settings surface, which now
    // HOMES the tournament builder (the flagship section) alongside contract /
    // assistant / appearance (editable). Uses the router href so the route
    // stays the single source of truth; `#/builder` still deep-links the
    // builder section directly.
    el('a', { class: 'dt-nav-build', href: href('settings', {}), title: 'Settings (tournament builder + preferences)', 'aria-label': 'Open settings' }, [
      el('span', { class: 'dt-nav-build-glyph', 'aria-hidden': 'true', text: '⚙' }),
      el('span', { class: 'dt-nav-build-text', text: 'settings' }),
    ]),
    colorSwitch,
    scalePill,
    _statusEl,
  ]);
  root.appendChild(topbar);

  _treeHost = el('aside', { class: 'dt-sidebar', 'aria-label': 'Data model navigation' });
  _viewHost = el('main', { class: 'dt-viewhost', role: 'main' });

  // CHANGE 2 — the draggable RAIL-RESIZE handle on the rail's right edge. It is
  // a focusable separator (role="separator") so it is keyboard-accessible
  // (arrows nudge the width ±16); a pointer drag sets the width live. Persisted
  // to localStorage + restored on mount; the detail pane's 1fr column reflows.
  const initialRail = readRail();
  _railHandle = el('div', {
    class: 'dt-rail-handle', role: 'separator', tabindex: '0',
    'aria-orientation': 'vertical', 'aria-label': 'Resize the navigation side panel',
    'aria-valuemin': String(RAIL_MIN), 'aria-valuemax': String(RAIL_MAX), 'aria-valuenow': String(initialRail),
    title: 'Drag to resize the side panel',
  });
  wireRailHandle(_railHandle, root);

  // THE LIVE-RUN HERO. A persistent, shell-owned focal panel that LEADS the
  // page while a run is in flight (current phase + tournament progress + the
  // animating survival funnel + in-flight count + the activity ticker), so a
  // live run has an animated home that survives view navigation. It is hidden
  // (display:none via the absence of `.dt-live-on`) when idle, so the normal
  // summary leads. SSE-driven: refreshLive() patches it IN PLACE on every tick.
  _live = new LiveController({
    onCompetitor: (gen) => {
      if (!gen) return;
      const r = parseRoute(location.hash);
      const epochId = (r.params && r.params.epochId) || state.epoch.id || null;
      if (epochId) navigate('candidate', { epochId, gen });
    },
  });
  _heroHost = el('div', { class: 'dt-hero-host' }, [_live.node]);
  root.appendChild(_heroHost);

  root.appendChild(el('div', { class: 'dt-body' }, [_treeHost, _railHandle, _viewHost]));

  // The quiet, app-wide RESEARCH-PREVIEW note — pinned to the lower-right of the
  // chrome, present on every view (mounted once, never rebuilt on a heartbeat).
  root.appendChild(researchPreviewNote());

  applyTheme(readColor());
  applyTypeface(readType());
  applyScale(readScale());
  applyRail(initialRail, root);

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  // bare `#/` prefix — only T's UI loads, so no `/T` namespacing is needed.
  // A missing / non-app hash gets normalised to the environment root.
  const h = String(location.hash);
  if (h === '' || h === '#' || !h.startsWith('#/')) location.hash = '#/';
  else dispatch();
}

// Assemble the tree's structural model: every epoch the workspace knows, each
// with its generations and board entries. Failure-tolerant — a missing
// drill-down degrades to an empty group, never a blank tree.
//
// THE TREE-EMPTY FIX. The epoch list must derive from AUTHORITATIVE data and be
// reliable on EVERY route (including the publication view). The old build read
// ONLY /api/workspace.epochs ∪ /api/epoch — both of which can be empty/stale on
// some routes (a workspace digest that omitted `epochs`, or an /api/epoch that
// 404s for a non-current epoch), leaving the tree blank even though /api/lineage
// plainly returns that epoch's generations and the breadcrumb names it. We now
// union FOUR authoritative sources: /api/lineage generations grouped by
// epoch_id, /api/workspace.epochs, /api/epoch, AND the currently-routed epochId
// — so an existing epoch ALWAYS lists, and the empty state shows only when there
// are genuinely zero epochs across all of them.
export async function buildTreeModel(route) {
  const [ws, lin, ep, brk] = await Promise.all([D.workspace(), D.lineage(), D.epoch(), D.bracket()]);
  const epochs = [];
  const seen = new Set();
  const current = (ws && ws.current_epoch_id) || (ep && ep.epoch_id) || null;
  const addEpoch = (id) => {
    if (id == null || seen.has(id)) return;
    seen.add(id);
    epochs.push({ id, current: id === current });
  };
  // (1) /api/lineage generations grouped by epoch_id — the authoritative source
  // that is populated on every route the tree paints over (it backs the detail
  // panes that DO show the epoch). This is what makes the empty state reliable.
  if (lin && Array.isArray(lin.generations)) {
    for (const g of lin.generations) if (g && g.epoch_id != null) addEpoch(g.epoch_id);
  }
  // (2) /api/workspace.epochs — the multi-epoch roster (when present).
  if (ws && Array.isArray(ws.epochs)) {
    for (const e of ws.epochs) if (e && e.epoch_id != null) addEpoch(e.epoch_id);
  }
  // (3) the current epoch contract.
  if (ep && ep.epoch_id != null) addEpoch(ep.epoch_id);
  // (4) the epoch the route is pointing AT — so a deep-link / the publication
  // view always shows its own epoch node even if every feed above was sparse.
  const routeEpochId = route && route.params ? route.params.epochId : null;
  if (routeEpochId != null) addEpoch(routeEpochId);

  // If no authoritative `current` resolved (sparse workspace/epoch feeds) fall
  // back to the routed epoch, else the sole epoch — so exactly one node carries
  // the "current" marker rather than none.
  if (current == null && epochs.length) {
    const fallbackCurrentId = routeEpochId != null ? routeEpochId : epochs[0].id;
    for (const e of epochs) e.current = e.id === fallbackCurrentId;
  }

  // Generations + boards: the workspace can carry MORE THAN ONE epoch, so we
  // resolve EACH epoch node's bundle from /api/lineage filtered by THAT node's
  // epoch_id (never the single current-epoch assumption that left the
  // non-current epoch's GENERATIONS node empty). The contract-scoped extras
  // (/api/epoch.board + /api/epoch.experiments + the bracket's champion
  // lineage) belong to the epoch /api/epoch resolved (and, when no epoch tag is
  // present, to the routed/sole epoch — the legacy single-epoch case), so they
  // attach only to that node; every OTHER epoch node still fills its own
  // generations from the lineage. This degrades gracefully — an epoch with no
  // lineage rows and no contract extras resolves to an honest empty group.
  const byEpoch = {};
  // the epoch the contract extras (board + experiments) belong to: the
  // contract's own epoch when /api/epoch resolved, else the routed epoch (so a
  // deep-link / the publication route still attaches the board to its node).
  const contractEpochId = (ep && ep.epoch_id != null) ? ep.epoch_id : routeEpochId;
  // The CURRENT champion = the LAST id in champion_lineage (the epoch's
  // reigning generation). Every OTHER promoted generation is a FORMER champion
  // (it held the title, then was succeeded). When the lineage is absent, fall
  // back to the last-promoted generation as the current champion so a
  // pre-feature index still disambiguates one current crown. (The bracket is
  // fetched for the contract epoch, so the lineage applies to that epoch's
  // champion disambiguation.)
  const lineage = (brk && Array.isArray(brk.champion_lineage)) ? brk.champion_lineage.map(String) : [];
  const currentChampionId = lineage.length ? lineage[lineage.length - 1] : null;
  for (const e of epochs) {
    const id = e.id;
    // THE PER-EPOCH FIX: each node lists its OWN generations — the lineage rows
    // tagged with THIS epoch_id (keeping the `!g.epoch_id` tolerance for an old
    // single-epoch workspace whose rows omit the tag).
    const linForEpoch = (lin && Array.isArray(lin.generations))
      ? lin.generations.filter((g) => (id === contractEpochId ? !g.epoch_id || g.epoch_id === id : g.epoch_id === id)) : [];
    const isContractEpoch = id === contractEpochId;
    // Preserve the tri-state `promoted` (true / false / null) so the tree can
    // render an unscored child as PENDING rather than a default rejected (Class
    // B). A pre-feature row that omits the field reads null ⇒ pending. The
    // /api/epoch experiments are a fallback ONLY for the contract epoch (they
    // describe its run); other epochs rely on the lineage alone.
    const gensList = linForEpoch.length
      ? linForEpoch.map((g) => ({ id: g.generation_id, promoted: g.promoted == null ? null : !!g.promoted, parent: g.parent_generation_id || null, round_index: Number.isInteger(g.round_index) ? g.round_index : null }))
      : (isContractEpoch && ep && Array.isArray(ep.experiments) ? ep.experiments.map((x) => ({
          id: x.generation_id, parent: x.parent_generation_id || null,
          promoted: normaliseDecision(x.outcome) === 'promoted' ? true : (normaliseDecision(x.outcome) === 'rejected' ? false : null),
          round_index: Number.isInteger(x.round_index) ? x.round_index : null,
        })) : []);
    // disambiguate the CURRENT champion (♛) from FORMER champions (hollow
    // crown ♔) — the champion lineage applies to the contract epoch's bracket.
    const fallbackCurrent = currentChampionId == null
      ? (gensList.filter((g) => g.promoted === true).map((g) => g.id).pop() || null)
      : currentChampionId;
    for (const g of gensList) {
      const champ = isContractEpoch && g.promoted === true && String(g.id) === String(fallbackCurrent);
      g.currentChampion = champ;
      g.formerChampion = g.promoted === true && !champ;
    }
    // An ORPHAN is a parentless generation that nothing descends from and that
    // never recorded an outcome — a stray from an aborted run, NOT the baseline
    // seed. The true seed is the lineage ROOT that challengers build on (it is
    // some other gen's parent), so labelling a childless, outcome-less, rootless
    // gen "seed" is misleading; mark it so the tree can say "unscored".
    const parentIds = new Set(gensList.map((g) => g.parent).filter(Boolean).map(String));
    for (const g of gensList) {
      g.orphan = !g.parent && g.promoted == null
        && !g.currentChampion && !g.formerChampion && !parentIds.has(String(g.id));
    }
    // Boards come from the epoch contract — attach them only to the contract
    // epoch's node (the other epochs' boards resolve when that epoch is viewed).
    const boardList = (isContractEpoch && ep && Array.isArray(ep.board) ? ep.board : []).map((b) => ({
      id: b.entry_id || b.id, kindTag: KIND_TAG[b.kind] || null,
    })).filter((b) => b.id);
    // ROUND GROUPING (Task 5): Epoch → Round 0 / Round 1 / … → {challengers
    // minted that round}. Derived from per-gen round_index (+ the field-record /
    // matchup fallback for the contract epoch, where the bracket is in scope).
    // Degrades to a single round 0 when round_index is absent and no records
    // resolve — the tree then renders a flat list (the round node is suppressed
    // below when there is only one round and no round_index stamp).
    const epochStructure = (isContractEpoch && ep && ep.tournament && ep.tournament.structure) || 'gauntlet';
    const treeRounds = roundsForTree({
      gens: gensList,
      bracket: isContractEpoch ? brk : null,
      structure: epochStructure,
      championId: fallbackCurrent,
    });
    byEpoch[id] = { gens: gensList, boards: boardList, rounds: treeRounds };
  }
  return { epochs, byEpoch, current };
}

async function renderTree(route) {
  if (!_treeHost) return;
  const model = await buildTreeModel(route);
  // the LIVE-ACTIVITY set (running gen / board-entry ids) drives a subtle pulse
  // on the active rows. Gated on the structure-agnostic running verdict +, when
  // tagged, scoped to the viewed epoch. Folded into the digest so the pulse
  // re-stamps when the set changes — a steady beat with the same set is a no-op.
  const status = deriveLiveStatus({
    heartbeat: state.heartbeat,
    activeRuns: state.activeRuns,
    activeTournament: state.activeTournament,
  });
  const routeEpochId = (route && route.params) ? route.params.epochId : null;
  const live = treeLiveSet({
    activeRuns: state.activeRuns, running: status.running,
    epochId: routeEpochId != null ? routeEpochId : model.current,
  });
  const digest = treeDigest(model, route, _toggles, live);
  if (digest === _lastTreeDigest && _treeHost.firstChild) return;
  _lastTreeDigest = digest;
  buildTree(_treeHost, model, route, _toggles, _ctx, (key) => {
    if (_toggles.has(key)) _toggles.delete(key); else _toggles.add(key);
    _lastTreeDigest = null;
    renderTree(parseRoute(location.hash));
  }, live);
}

function renderCrumbs(route) {
  if (!_crumbHost) return;
  const trail = crumbTrail(route);
  const digest = JSON.stringify(trail.map((c) => [c.label, c.view || '', c.current || false]));
  if (digest === _lastCrumbDigest && _crumbHost.firstChild) return;
  _lastCrumbDigest = digest;
  clearChildren(_crumbHost);
  trail.forEach((c, i) => {
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'dt-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'dt-crumb dt-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'dt-crumb', href: href(c.view, c.params), text: c.label }));
    }
  });
}

// THE LIVE-STATUS FIX. Derive a STRUCTURE-AGNOSTIC run verdict from the three
// live signals already folded into AppState by /api/environment + the SSE
// heartbeat (state.heartbeat.phase, state.activeRuns, state.activeTournament).
// A non-idle heartbeat phase ⇒ running for ANY structure (gauntlet / racing /
// swiss / single_elim / double_elim); the in-flight count + active-tournament
// `phase === "running"` corroborate. Digest-gated: a steady heartbeat ping that
// leaves the derived verdict unchanged writes ZERO DOM (no flash).
function renderStatus() {
  if (!_statusEl) return;
  const conn = state.connected ? 'live' : state.connecting ? 'connecting…' : 'offline';
  const status = deriveLiveStatus({
    heartbeat: state.heartbeat,
    activeRuns: state.activeRuns,
    activeTournament: state.activeTournament,
  });
  const digest = liveStatusDigest(conn, status);
  if (digest === _lastStatusDigest) return;
  _lastStatusDigest = digest;

  patchText(_statusTextEl || _statusEl, conn);
  patchClass(_statusEl, 'dt-connected', state.connected);
  patchClass(_statusEl, 'dt-running', status.running);
  // A frozen heartbeat (stale, not live) gets a distinct chrome class so the
  // dot/badge can read "not live" rather than borrowing the running accent.
  patchClass(_statusEl, 'dt-stale', !status.running && !!status.heartbeatStale);

  if (_runLabelEl) patchText(_runLabelEl, status.running ? status.label : '');
  if (_runCountEl) {
    const n = status.inFlight;
    patchText(_runCountEl, status.running && n > 0 ? ('· ' + n + (n === 1 ? ' unit' : ' units')) : '');
  }
  // The stale affordance: surface "last seen Ns ago" when a heartbeat exists
  // but has frozen (so the run is NOT live) — never a silent freeze. Cleared
  // while live or when no heartbeat exists at all.
  if (_staleEl) {
    patchText(_staleEl, (!status.running && status.heartbeatStale)
      ? staleLabel(status.heartbeatAgeMs) : '');
  }
}

// THE ZICATO-LEVEL HARMONOGRAF LINK. The top-bar "execution ▸" entry deep-links
// into the meta-loop session (zicato's own proposer + judge timeline). It is
// liveness-gated (via harmonografMetaUrl → harmonografBase) so it renders only
// while a harmonograf server is reachable AND a meta-loop session id is known —
// during a live evolve OR a standalone dashboard that resolved a persistent
// per-workspace server. Digest-gated on the resolved href so a no-op heartbeat
// never repaints it (render discipline). See docs/design/HARMONOGRAF.md §3b.
function renderExecLink() {
  if (!_execHost) return;
  const url = harmonografMetaUrl();
  if (url === _lastExecHref) return;
  _lastExecHref = url;
  clearChildren(_execHost);
  if (!url) return;
  _execHost.appendChild(el('a', {
    class: 'harmonograf-link harmonograf-meta dt-exec-link',
    href: url, target: '_blank', rel: 'noopener',
    title: 'Open the zicato execution timeline (meta-loop) in harmonograf',
    'aria-label': 'open the zicato execution timeline in harmonograf',
  }, ['execution ↗']));
}

// THE SSE-DRIVEN LIVE REFRESH. Distinct from renderStatus (which is gated on the
// COARSE status digest): the live hero must update on EVERY tick (a steady
// heartbeat that does not change the status digest can still carry progress /
// active-runs deltas), so it is driven separately and is itself diff-gated
// internally — a steady tick with identical live state writes ZERO DOM (the
// ticker appends nothing, the funnel's digest is unchanged, the progress key is
// unchanged). Because the SSE `heartbeat` frame fires `state._changed()`
// directly (core/sse.js), this runs sub-second — push, not poll.
function refreshLive() {
  if (!_live) return;
  const status = deriveLiveStatus({
    heartbeat: state.heartbeat,
    activeRuns: state.activeRuns,
    activeTournament: state.activeTournament,
  });
  _live.update({
    status,
    heartbeat: state.heartbeat,
    activeRuns: state.activeRuns,
    activeTournament: state.activeTournament,
  });
  if (_heroHost) patchClass(_heroHost, 'dt-hero-live', !!status.running);
}

// Enable/disable the back control: it is inert at the environment root (no
// parent to climb to) and active everywhere else.
function renderBack(route) {
  if (!_backBtn) return;
  const dest = up(route);
  _backBtn.disabled = !dest;
  patchClass(_backBtn, 'dt-back-off', !dest);
}

async function dispatch() {
  const route = parseRoute(location.hash);
  const renderer = RENDERERS[route.view] || RENDERERS.home;
  // the compare target is part of the selection — a cmp change must clear +
  // repaint the detail pane (the split appears/disappears).
  const viewKey = route.view + '|' + JSON.stringify(route.params || {}) + '|' + (route.cmp || '');

  renderCrumbs(route);
  renderStatus();
  renderExecLink();
  refreshLive();
  renderBack(route);
  renderTree(route);

  const prevView = _lastViewKey == null ? null : String(_lastViewKey).split('|')[0];
  const prevKey = _lastViewKey;
  // Clear the host (and bust caches) on ANY selection change — not just a view
  // change — so a per-pane host never carries stale content across selections.
  if (prevKey !== viewKey) {
    clearChildren(_viewHost);
    if (prevView !== route.view) invalidateLive();
  }
  _lastViewKey = viewKey;

  const token = ++_renderToken;
  try {
    // pass the FULL route (4th arg) so the candidate view sees the compare
    // target; legacy views read only `route.params` (3rd arg) unchanged.
    await renderer.render(_viewHost, _ctx, route.params, route);
  } catch (err) {
    if (token !== _renderToken) return;
    clearChildren(_viewHost);
    _viewHost.appendChild(el('p', { class: 'dt-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-T render error', err);
  }
}

let _reRenderTimer = null;
function onStateChanged() {
  renderStatus();
  // The zicato-level execution link flips with liveness (server up ⇄ run
  // ended ⇄ meta-session learned) — refresh it on every tick alongside the
  // status pill. Digest-gated internally so a no-op beat writes zero DOM.
  renderExecLink();
  // SSE-DRIVEN: refresh the live surfaces on EVERY tick (sub-second — the SSE
  // heartbeat frame fires state:changed directly), so live state (phase /
  // progress / funnel / activity) animates as it lands rather than waiting on
  // the 400 ms re-dispatch debounce. The hero patches in place (no full
  // repaint); the structure swap inside it stays digest-gated.
  refreshLive();
  // THE UNDER-RENDER FIX. The tree + candidate-listing views read data.js's
  // module cache, which invalidateLive() busts ONLY on a view change — so a NEW
  // candidate folded into AppState by /api/environment never reached those panes
  // (stale cache → gen-keyed digests never flipped → hard-refresh needed). Detect
  // a real live-data change (gen set / statuses / epoch roster) via a signature
  // off the just-refreshed AppState, and ONLY THEN drop the stale cache + force
  // the tree to recompute. A no-op beat leaves the signature identical ⇒ no bust,
  // no fetch, no repaint (no flash); the view/tree digests still gate after a
  // bust, so only a true add repaints.
  const sig = liveDataSignature();
  if (sig !== _lastLiveSig) {
    _lastLiveSig = sig;
    invalidateLive();
    _lastTreeDigest = null;   // force renderTree to rebuild off the fresh cache
  }
  if (_reRenderTimer != null) return;
  _reRenderTimer = setTimeout(() => {
    _reRenderTimer = null;
    dispatch();
  }, 400);
}

export { DEFAULT_COLOR, DEFAULT_TYPE, DEFAULT_SCALE, SCALE_MIN, SCALE_MAX, SCALE_STEP };
export { RAIL_MIN, RAIL_MAX, DEFAULT_RAIL };
