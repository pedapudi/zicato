// js/shell.js — the Console III shell: a data-model TREE sidebar +
// one persistent detail pane, with digest-gated dispatch.
//
// Variant P ("Console III") is the direct successor to Variant N — the same
// dense, data-ink-maximal aesthetic (Monokai default + Technical typeface) —
// but it REPLACES N's top-tab nav with a persistent, collapsible LEFT TREE
// grounded in the real data model: Environment → Epoch(s) → {Rounds → Round n
// → <gen>; Boards → <entry>; Evals; Instrument; Mutation surface; Publication}.
// Selecting any tree node drives the single detail pane. The tree navigates
// MULTIPLE epochs AND generations (N could not). Selection is explicit + URL-encoded, so
// a cold deep-link hydrates BOTH the open tree branches and the detail pane.
//
// The shell owns:
//   * a COMPACT top bar — branding · breadcrumb · colour-theme picker (monokai
//     default) · status pill. The TYPEFACE and PAGE-SCALE controls live in
//     Settings → Appearance now (not the top bar), driving the same stores;
//   * the persistent tree sidebar (its own digest gate);
//   * ONE persistent detail host (never recreated per repaint);
//   * digest-gated dispatch — a `state:changed` tick that only re-stamps a
//     heartbeat writes ZERO DOM to either the tree or the detail pane.
//
// Theme + typeface are CSS-only swaps (data-t-theme / data-t-type on the root).

import { el, svgEl, clearChildren, patchText, patchClass } from './core/dom.js';
import { harmonografMetaUrl } from './core/harmonograf.js';
import { state } from './core/state.js';
import { bus } from './core/bus.js';
import { loadEnvironment, loadServiceIdentity, postControl } from './core/api.js';
import { connectSSE } from './core/sse.js';
import { parseRoute, navigate, href, crumbTrail, up } from './router.js';
import * as D from './data.js';
import { invalidateLive, liveDataSignature } from './data.js';
import { buildTree, treeDigest } from './tree.js';
import { roundsForTree } from './rounds.js';
import { livenessFor, liveStatusDigest, treeLiveSet, staleLabel, runStateLabel, LIVENESS } from './livestatus.js';
import { LiveController } from './live.js';
import { buildSwatchDropdown, syncSwatchDropdowns } from './swatchdropdown.js';
import { syncTypefaceDropdowns, syncFontSizeSegments } from './typefacedropdown.js';
import {
  COLOR_THEMES, DEFAULT_COLOR, normaliseColor, readColor, persistColor,
  TYPE_THEMES, DEFAULT_TYPE, normaliseType, readType, persistType,
  SCALE_MIN, SCALE_MAX, SCALE_STEP, DEFAULT_SCALE, normaliseScale, readScale, persistScale,
  normaliseFontSize, readFontSize, persistFontSize, fontSizeScale,
  RAIL_MIN, RAIL_MAX, DEFAULT_RAIL, normaliseRail, readRail, persistRail, pageScaleOf,
  gatedSwap,
} from './ui.js';

import * as home from './views/home.js';
import * as epoch from './views/epoch.js';
import * as gens from './views/gens.js';
import * as candidate from './views/candidate.js';
import * as diff from './views/diff.js';
import * as boards from './views/boards.js';
import * as board from './views/board.js';
import * as mutations from './views/mutations.js';
import * as instrument from './views/instrument.js';
import * as traces from './views/traces.js';
import * as evals from './views/evals.js';
import * as publication from './views/publication.js';
import * as builder from './views/builder.js';
import * as logs from './views/logs.js';
import * as settings from './views/settings.js';

const RENDERERS = { home, epoch, gens, candidate, diff, boards, board, mutations, instrument, traces, evals, publication, builder, logs, settings };

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
let _statusTextEl = null;     // the connection word (connected/connecting/offline)
let _runStateEl = null;       // the four-state run pill (LIVE/STALLED/SETTLED/DEAD)
let _runStateTextEl = null;   // the run-state WORD inside the pill
let _runLabelEl = null;       // the structure+phase run label
let _runCountEl = null;       // the in-flight board-unit count
let _staleEl = null;          // the "last seen Ns ago / stale" affordance
let _loopCtlHost = null;      // topbar loop-control cluster (pause/resume/skip)
let _lastLoopCtlDigest = null;
let _pausedOverride = null;   // optimistic paused verdict after a control POST
let _colorDropdown = null;     // the swatch-dropdown controller (Change 6)
let _railHandle = null;        // the draggable rail-resize handle (Change 2)
let _railDragging = false;     // true while a live rail drag is in flight
let _backBtn = null;
let _execHost = null;          // top-bar host for the zicato-level harmonograf link
let _lastExecHref = null;      // digest gate for the execution link (no-beat churn)
let _renderToken = 0;
let _live = null;             // the persistent LIVE-RUN controller (live hero + ticker)
let _heroHost = null;         // the persistent host the live hero leads from
let _lastViewKey = null;
// THE SETTINGS OVERLAY (Change 1). Settings renders into a routed DRAWER that
// paints over the current view rather than taking over `_viewHost`. The shell
// owns the scrim + panel + the section host the settings view paints into.
let _settingsOverlay = null;  // the overlay root (scrim + drawer panel)
let _settingsScrim = null;    // the click-to-close backdrop
let _settingsPanelHost = null; // the host the settings view's render() paints into
let _settingsOpen = false;    // true while the drawer is open (gates Esc + the body host)
// The last NON-settings route — the view the overlay paints over. A bare
// `#/settings` loaded cold (no prior view) opens over Environment (home).
let _underlyingRoute = { view: 'home', params: {}, cmp: null };
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
  navigate(dest.view, dest.params, navOpts(dest));
  return true;
}

// The hash-suffix options a destination carries (`~cmp=` / `~follow=1`).
// undefined when it carries neither, so href() emits a bare path.
function navOpts(dest) {
  if (!dest) return undefined;
  if (dest.cmp) return { cmp: dest.cmp };
  if (dest.follow) return { follow: true };
  return undefined;
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
  return t;
}

// GLOBAL TEXT FONT-SIZE — the S/M/L control in the typeface picker. DISTINCT
// from the page scale (which `zoom`s the WHOLE page incl. figures): this is
// a TEXT-ONLY multiplier. It stamps `--dt-font-scale` (the number every html
// `font-size: calc(Npx * var(--dt-font-scale,1))` rule reads) + a `data-t-fontsize`
// attribute on the app root, persists under its own key, and syncs every live
// S/M/L segment (top bar + Settings). SMALL (1.0) is the current look — the
// literal px render unchanged — so the default is byte-identical to today. SVG
// figures are sized in svg.js (the `font:` shorthand), so they never grow here.
export function applyFontSize(size, rootEl) {
  const v = normaliseFontSize(size);
  const root = rootEl || _root;
  if (root) {
    root.style.setProperty('--dt-font-scale', String(fontSizeScale(v)));
    root.setAttribute('data-t-fontsize', v);
  }
  persistFontSize(v);
  // Sync EVERY live S/M/L segment (top bar AND settings) — one source of truth.
  syncFontSizeSegments(v);
  return v;
}

// PAGE-WIDE SCALE. Distinct from density: this is a single master multiplier on
// the ENTIRE page (text AND diagrams), applied as `zoom` on the console's app
// ROOT (NOT per-pane). `zoom` reflows rather than transforms, so the layout
// re-wraps at the scaled size and never clips. We also stamp `--dt-page-scale`
// (a 0–1 ratio) for any rule that wants the raw factor. Persisted under its own
// key, so it composes with — and survives — colour / typeface changes.
//
// The CONTROL lives in Settings → Appearance (views/settings.js scalePicker),
// which owns its own readout; this function only stamps + persists, so it is
// equally the restore path and the programmatic/keyboard path.
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
  return n;
}

// RESET the page scale back to 100% (DEFAULT_SCALE) + persist. Backs the small
// reset affordance beside the Settings → Appearance scale range; keyboard-
// accessible (it is a real <button>). Returns the applied value.
export function resetScale(rootEl) {
  return applyScale(DEFAULT_SCALE, rootEl || _root);
}

// LEFT SIDE-PANEL (rail) WIDTH — set the `--dt-rail` grid column on the app root
// + persist. Distinct from the page scale (this resizes ONLY the tree
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

// Wire the rail-resize handle. Two hazards make a drag jump, and the handle
// avoids both:
//   (1) ZOOM MISMATCH. The handle lives inside the app root, which carries a
//       page-wide `zoom` (the page scale). `event.clientX` is a VIEWPORT CSS-px
//       coordinate, but `--dt-rail` is laid out in the root's UNSCALED layout
//       space. Setting the width straight from `clientX − railLeft` makes the
//       width over- or under-track the pointer at zoom ≠ 1, since render then
//       re-multiplies it by `zoom`. The handle instead works in DELTA space and
//       divides the pointer delta by the live page-scale factor, so a given
//       pointer travel maps 1:1 onto layout-space rail travel at ANY zoom.
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
// in console.css), comfortably above the ~24px floor, so the scroll, pluck,
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

// THE RESEARCH-PREVIEW PILL — a quiet product-status tag pinned NEXT TO the
// "zıcato console" wordmark in the top bar. It echoes the wordmark's own
// register (the .dt-brand-tag "console" tag): small, faint, monospace,
// uppercase tracking, theme-adaptive (the muted ink-faint token + currentColor).
// The label is STACKED on two lines ("research" / "preview") so it reads as a
// compact corner tag beside the wordmark rather than a wide strip. It is purely
// informational (role="note"); built ONCE as static chrome, never rebuilt on an
// SSE heartbeat (digest discipline). The styling lives in console.css
// (.dt-respreview + children).
function researchPreviewPill() {
  return el('span', { class: 'dt-respreview', role: 'note', 'aria-label': 'research preview' }, [
    el('span', { class: 'dt-respreview-line', text: 'research' }),
    el('span', { class: 'dt-respreview-line', text: 'preview' }),
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
  // Reset the settings-overlay state on a fresh mount.
  _settingsOpen = false;
  _underlyingRoute = { view: 'home', params: {}, cmp: null };
  root.setAttribute('data-t-theme', readColor());
  root.setAttribute('data-t-type', readType());
  root.setAttribute('data-t-scale', String(readScale()));
  root.setAttribute('data-t-fontsize', readFontSize());

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

  // PAGE-SCALE pill — REMOVED from the top-bar chrome, following the typeface
  // picker: it is a set-once appearance preference, and a live range slider is
  // the widest, busiest control on a bar that has to make room for the run
  // state. It now lives ONLY in Settings → Appearance (views/settings.js's
  // scalePicker), which already drives the same applyScale/resetScale path.
  // applyScale stamps and persists without syncing any top-bar node, so every
  // apply path (restore, keyboard, the Settings picker) shares one route.

  // The live-status pill: a connection dot + the connection word, plus a
  // RUN badge that lights up whenever the loop is active for ANY tournament
  // structure (read from the live APIs in renderStatus rather than the gauntlet-only
  // activeTournament). The run badge carries the structure + phase label and an
  // in-flight board-unit count; it is hidden when idle/done.
  _statusTextEl = el('span', { class: 'dt-status-text', text: 'connecting…' });
  // ONE consolidated LIVENESS pill — the three competing "live" signals (the
  // bare four-state word, a separate run-badge phase label, a separate "last
  // seen Ns ago" affordance) fold into a single `dt-run-state` pill reading
  // `● <STATE> · <structure · phase> · <N units>` (or `· last seen Ns ago` when
  // frozen). The four-state word keeps its `dt-rs-<state>` CSS modifier.
  _runStateTextEl = el('span', { class: 'dt-rs-text', text: '' });
  _runLabelEl = el('span', { class: 'dt-run-label', text: '' });
  _runCountEl = el('span', { class: 'dt-run-count', text: '' });
  _staleEl = el('span', { class: 'dt-status-stale', text: '' });
  _runStateEl = el('span', { class: 'dt-run-state', 'aria-live': 'polite' }, [
    el('span', { class: 'dt-rs-dot dt-status-dot', 'aria-hidden': 'true' }),
    _runStateTextEl,
    _runLabelEl,
    _runCountEl,
    _staleEl,
  ]);
  _statusEl = el('span', { class: 'dt-status' }, [
    el('span', { class: 'dt-status-dot' }),
    _statusTextEl,
    _runStateEl,
  ]);

  // THE LOOP CONTROLS: Pause/Resume toggle + Skip-round, beside the status pill. Rendered ONLY while the loop is controllable
  // (live + a writable workspace); read-only / idle keeps the host empty.
  // renderLoopControls fills it, digest-gated on {shown, paused} so a
  // steady heartbeat writes zero DOM here.
  _loopCtlHost = el('span', { class: 'dt-loopctl', role: 'group', 'aria-label': 'Loop controls' });
  _lastLoopCtlDigest = null;
  _pausedOverride = null;

  // top-left UP control — navigates UP the selection hierarchy (the parent
  // route); dispatch then repaints the destination into the MAIN detail pane
  // (never the sidebar). Labelled "↑ up" because it climbs the hierarchy rather
  // than stepping back through browser history.
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
      el('span', { class: 'dt-brand-tag', text: 'console' }),
      researchPreviewPill(),
    ]),
    _crumbHost,
    el('span', { class: 'dt-topbar-spacer' }),
    // the ZICATO-LEVEL harmonograf entry — a liveness-gated "execution ▸"
    // link into the meta-loop session (the proposer + judge timeline of the
    // evolution itself). Filled by renderExecLink, digest-gated so a no-op
    // heartbeat never repaints it. See docs/design/HARMONOGRAF.md §3b.
    (_execHost = el('span', { class: 'dt-nav-exec', 'aria-live': 'polite' })),
    // the TOURNAMENT BUILDER entry — a ⚒ that opens the builder as its OWN
    // first-class, full-width view (`#/builder`). It is promoted out of Settings
    // (where it was nested behind the settings rail — double rails + a cramped
    // centre) so its four panes get the whole viewport. Sits beside the ⚙ so the
    // two top-level surfaces read as peers; the same route-agnostic builder
    // module backs this entry, the Settings launcher, and the CLI deep-link.
    el('a', { class: 'dt-nav-builder', href: href('builder', {}), title: 'Tournament builder (compose the evaluation contract)', 'aria-label': 'Open the tournament builder' }, [
      el('span', { class: 'dt-nav-builder-glyph', 'aria-hidden': 'true', text: '⚒' }),
      el('span', { class: 'dt-nav-builder-text', text: 'builder' }),
    ]),
    // the OPERATOR-LOG entry — the workspace-level `#/logs` pane (LOGGING.md).
    // A peer of the builder / settings surfaces; reads the structured stream
    // for one evolve / reflect invocation.
    el('a', { class: 'dt-nav-logs', href: href('logs', {}), title: 'Operator log (the structured log stream for one invocation)', 'aria-label': 'Open the operator log' }, [
      el('span', { class: 'dt-nav-logs-glyph', 'aria-hidden': 'true', text: '☰' }),
      el('span', { class: 'dt-nav-logs-text', text: 'log' }),
    ]),
    // the SETTINGS entry — a ⚙ that opens the Settings surface (contract roll-up
    // · models / LLM endpoints · appearance). Settings keeps a launcher to the
    // standalone `#/builder` view, which is where the builder lives. Uses
    // the router href so the route stays the single source of truth.
    el('a', { class: 'dt-nav-build', href: href('settings', {}), title: 'Settings (contract · models · appearance)', 'aria-label': 'Open settings' }, [
      el('span', { class: 'dt-nav-build-glyph', 'aria-hidden': 'true', text: '⚙' }),
      el('span', { class: 'dt-nav-build-text', text: 'settings' }),
    ]),
    colorSwitch,
    _loopCtlHost,
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
    // the per-run kill sink (confirm-on-click lives in the row's button) —
    // routes through the file-based control channel; refresh afterwards so
    // the torn-down run leaves the in-flight rows promptly.
    onKill: (runId) => fireLoopControl('kill/' + encodeURIComponent(runId), undefined, null),
    // the per-run FOLLOW sink — opens that unit's live conversation on its
    // board, deep-linked so the followed conversation survives a reload and
    // can be shared.
    onFollow: (gen, entry, runId) => {
      if (!gen || !entry) return;
      const r = parseRoute(location.hash);
      const epochId = (r.params && r.params.epochId) || state.epoch.id || null;
      if (epochId) navigate('board', { epochId, entry, gen }, { follow: true });
    },
  });
  _heroHost = el('div', { class: 'dt-hero-host' }, [_live.node]);
  root.appendChild(_heroHost);

  root.appendChild(el('div', { class: 'dt-body' }, [_treeHost, _railHandle, _viewHost]));

  // THE SETTINGS OVERLAY. Settings is a routed right-side DRAWER that paints
  // OVER the current view rather than a full-page view. The shell
  // owns a single scrim + drawer-panel pair, mounted once here and hidden until
  // `#/settings[/<section>]` is the route. The underlying view stays rendered in
  // `_viewHost` behind a scrim, so an Appearance change (theme / typeface / font
  // size) applies LIVE to the page visible behind the panel. Esc, a scrim click,
  // and the × all close the overlay by navigating to the underlying route.
  _settingsPanelHost = el('div', { class: 'dt-drawer-body', role: 'region', 'aria-label': 'Settings' });
  const closeBtn = el('button', {
    class: 'dt-drawer-x', type: 'button', title: 'Close settings', 'aria-label': 'Close settings', text: '×',
  });
  closeBtn.addEventListener('click', () => closeSettingsOverlay());
  const panel = el('div', {
    class: 'dt-drawer-panel', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Settings',
  }, [
    el('div', { class: 'dt-drawer-head' }, [
      el('span', { class: 'dt-drawer-title', text: 'settings' }),
      closeBtn,
    ]),
    _settingsPanelHost,
  ]);
  _settingsScrim = el('div', { class: 'dt-drawer-scrim', 'aria-hidden': 'true' });
  _settingsScrim.addEventListener('click', () => closeSettingsOverlay());
  _settingsOverlay = el('div', { class: 'dt-drawer', 'data-open': '0' }, [_settingsScrim, panel]);
  root.appendChild(_settingsOverlay);
  // Esc closes the overlay (only while it is open). Bound once on the window.
  window.addEventListener('keydown', (ev) => {
    if (!_settingsOpen) return;
    const k = ev && ev.key;
    if (k === 'Escape' || k === 'Esc') {
      if (ev.preventDefault) ev.preventDefault();
      closeSettingsOverlay();
    }
  });

  // (The research-preview status tag is a pill NEXT TO the wordmark in the top
  // bar — see researchPreviewPill() in brandWordmark's topbar block.)

  applyTheme(readColor());
  applyTypeface(readType());
  applyFontSize(readFontSize());
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
// THE EPOCH LIST unions FOUR authoritative sources: /api/lineage generations
// grouped by epoch_id, /api/workspace.epochs, /api/epoch, AND the currently
// routed epochId. An existing epoch therefore ALWAYS lists, and the empty state
// shows only when all four are empty. Reading /api/workspace.epochs ∪ /api/epoch
// alone is not enough: both can be empty or stale on some routes — a workspace
// digest that omits `epochs`, or an /api/epoch that 404s for a non-current
// epoch — which blanks the tree even while /api/lineage returns that epoch's
// generations and the breadcrumb names it.
export async function buildTreeModel(route) {
  const [ws, lin, ep, brk, refl] = await Promise.all([D.workspace(), D.lineage(), D.epoch(), D.bracket(), D.reflections()]);
  // Which epochs carry at least one reflection — ONE workspace-wide read of
  // /api/reflections (each item is epoch-tagged), grouped here so the Instrument
  // tree node shows only when the epoch actually has reflections. Cheaper than a
  // per-epoch probe; the tree model already unions API sources this way.
  const reflEpochs = new Set();
  if (refl && Array.isArray(refl.reflections)) {
    for (const r of refl.reflections) if (r && r.epoch_id != null) reflEpochs.add(String(r.epoch_id));
  }
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

  // CANONICAL CHRONOLOGICAL ORDER. The UNION above lists `epochs` in
  // first-appearance order across the sparse feeds, which DISAGREES with the
  // fleet (painted from the timestamp-ordered /api/workspace.epochs): a
  // ZERO-generation epoch never appears in /api/lineage, so step (2) APPENDS it
  // LAST regardless of when it was minted. Re-sort to the ws order so sidebar ==
  // fleet == chronological. Stable decorate-sort-undecorate: epochs in ws.epochs
  // sort by ws index; epochs ABSENT from ws.epochs (a routed epoch missing from a
  // stale digest) sort AFTER, keeping insertion order (Infinity rank, original
  // index the deterministic tiebreaker throughout).
  const wsOrder = new Map();
  if (ws && Array.isArray(ws.epochs)) {
    ws.epochs.forEach((e, i) => { if (e && e.epoch_id != null) wsOrder.set(String(e.epoch_id), i); });
  }
  if (wsOrder.size) {
    epochs
      .map((e, i) => ({ e, i, rank: wsOrder.has(String(e.id)) ? wsOrder.get(String(e.id)) : Infinity }))
      .sort((a, b) => (a.rank - b.rank) || (a.i - b.i))
      .forEach((d, i) => { epochs[i] = d.e; });
  }

  // Generations + boards: the workspace can carry MORE THAN ONE epoch, so we
  // resolve EACH epoch node's bundle from /api/lineage filtered by THAT node's
  // epoch_id (never the single current-epoch assumption that left the
  // non-current epoch's GENERATIONS node empty). The contract-scoped extras
  // (/api/epoch.board + /api/epoch.experiments + the bracket's champion
  // lineage) belong to the epoch /api/epoch resolved (and, when no epoch tag is
  // present, to the routed or sole epoch — the untagged single-epoch case), so they
  // attach only to that node; every OTHER epoch node still fills its own
  // generations from the lineage. This degrades gracefully — an epoch with no
  // lineage rows and no contract extras resolves to an honest empty group.
  const byEpoch = {};
  // the epoch the contract extras (board + experiments) belong to: the
  // contract's own epoch when /api/epoch resolved, else the routed epoch (so a
  // deep-link / the publication route still attaches the board to its node).
  const contractEpochId = (ep && ep.epoch_id != null) ? ep.epoch_id : routeEpochId;
  // The CURRENT champion — the SERVER-STAMPED `current_champion` pointer on
  // the epoch payload (the end of the promoted spine, or the seed). Every
  // OTHER promoted generation is a FORMER champion (it held the title, then
  // was succeeded). Never re-derived from the lineage / promoted flags here.
  const currentChampionId = (ep && ep.current_champion != null) ? String(ep.current_champion) : null;
  // the SERVED per-epoch round timelines (cached reads; null when unserved) —
  // the tree's round grouping is a projection of these, never a client join.
  const timelineByEpoch = new Map(
    (await Promise.all(epochs.map(async (e) => [e.id, await D.roundTimeline(e.id)])))
  );
  // The champion pointer is PER EPOCH. Each epoch node reads its OWN
  // `?epoch=`-scoped payload — the same scoping the drill-down views use. The
  // contract epoch reuses the payload fetched above.
  //
  // A bare `D.epoch()` answers for the CURRENT epoch alone. Holding ONE such
  // pointer and gating the crown on the contract epoch marked every OTHER
  // epoch's reigning champion a FORMER champion, and crowned nothing there.
  // A failed read resolves to `null` (cachedJson swallows it), which the stamp
  // below reads as "pointer unknown", never as "not the champion".
  // A CLOSED epoch promotes nothing more, so its pointer is read through the
  // memoized `closedEpochChampion` — one contract build per closed epoch per
  // page, rather than one per epoch node on every live bust.
  const closedEpochs = new Set();
  if (ws && Array.isArray(ws.epochs)) {
    for (const e of ws.epochs) if (e && e.closed === true && e.epoch_id != null) closedEpochs.add(String(e.epoch_id));
  }
  const championByEpoch = new Map(
    (await Promise.all(epochs.map(async (e) => {
      if (String(e.id) === String(contractEpochId)) return [e.id, currentChampionId];
      if (closedEpochs.has(String(e.id))) return [e.id, await D.closedEpochChampion(e.id)];
      const scoped = await D.epoch(e.id);
      return [e.id, (scoped && scoped.current_champion != null) ? String(scoped.current_champion) : null];
    })))
  );
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
          promoted: x.promoted == null ? null : !!x.promoted,
          round_index: Number.isInteger(x.round_index) ? x.round_index : null,
        })) : []);
    // Separate THIS epoch's CURRENT champion from its FORMER champions (the
    // hollow crown) off THIS epoch's own pointer. An unknown pointer stamps
    // NEITHER flag, so tree.js falls back to `legacyChamp`: the promoted
    // generations keep the solid crown. An unserved pointer must never turn a
    // whole epoch "former".
    const epochChampionId = championByEpoch.get(id) || null;
    if (epochChampionId != null) {
      for (const g of gensList) {
        const champ = g.promoted === true && String(g.id) === String(epochChampionId);
        g.currentChampion = champ;
        g.formerChampion = g.promoted === true && !champ;
      }
    }
    // An ORPHAN is a parentless generation that nothing descends from and that
    // never recorded an outcome — a stray from an aborted run rather than the baseline
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
      id: b.entry_id, kindTag: KIND_TAG[b.kind] || null,
    })).filter((b) => b.id);
    // ROUND GROUPING (Task 5): Epoch → Round 0 / Round 1 / … → {challengers
    // minted that round}, read off the SERVED per-epoch round timeline
    // (/api/epoch/{id}/round-timeline). A missing timeline (the endpoint
    // absent — e.g. the Rust supervisor) yields no round nodes: the tree
    // renders its flat generation list, never a re-derived grouping.
    const epochStructure = (isContractEpoch && ep && ep.tournament && ep.tournament.structure) || 'gauntlet';
    const treeRounds = roundsForTree({
      timeline: timelineByEpoch.get(id) || null,
      gens: gensList,
      bracket: isContractEpoch ? brk : null,
      structure: epochStructure,
      championId: epochChampionId,
    });
    byEpoch[id] = { gens: gensList, boards: boardList, rounds: treeRounds, hasReflections: reflEpochs.has(String(id)) };
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
  const { status, liveness } = livenessFor(state);
  const routeEpochId = (route && route.params) ? route.params.epochId : null;
  const live = treeLiveSet({
    // Liveness comes from the tri-state verdict rather than from file
    // presence: a leftover active-run record must not leave tree rows pulsing
    // months after the run died.
    activeRuns: state.activeRuns, running: liveness.live && status.running,
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
  // The second-idiom digest gate folded onto gatedSwap (the same firstChild +
  // digest-attribute no-flash contract the views use) — a pure clear-and-rebuild
  // with no external invalidation, so it drops in cleanly.
  const digest = JSON.stringify(trail.map((c) => [c.label, c.view || '', c.current || false]));
  gatedSwap(_crumbHost, digest, () => {
    const out = [];
    trail.forEach((c, i) => {
      if (i > 0) out.push(el('span', { class: 'dt-crumb-sep', 'aria-hidden': 'true', text: '›' }));
      if (c.current || !c.view) {
        out.push(el('span', { class: 'dt-crumb dt-crumb-current', 'aria-current': 'page', text: c.label }));
      } else {
        out.push(el('a', { class: 'dt-crumb', href: href(c.view, c.params), text: c.label }));
      }
    });
    return out;
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
  // The CONNECTION word is the SSE/transport status — distinct from the run-state
  // pill's LIVE/STALLED/SETTLED/DEAD verdict that rides right after it. It must
  // NOT also say "live" (two adjacent "live" markers read as a redundant bug);
  // "connected" names the transport without colliding with the run pill.
  // TRANSPORT SURFACES ONLY WHEN BROKEN. A healthy socket is silence: the
  // operator saw "connected / STALLED / · racing · rung 0 / · 7 units" — four
  // status tokens, three truth sources, no hierarchy, and the tail of it
  // contradicted the head. "connected" describes the BROWSER's socket and reads
  // as a claim about the RUN, so it says nothing while the socket is fine.
  const conn = state.connected ? '' : state.connecting ? 'connecting…' : 'disconnected — retrying';
  // THE TRI-STATE — the one verdict every present-tense claim in the chrome
  // consumes, so the pill cannot read LIVE against a
  // workspace the server has already called interrupted. The four-state
  // verdict rides alongside it; it only refines a LIVE run into LIVE vs
  // STALLED and supplies the phase label.
  const { status, liveness } = livenessFor(state);
  // The loop-control cluster gates on its OWN digest ({shown, paused}) —
  // paused is not part of the status digest, so it must render before the
  // status early-return below.
  renderLoopControls(status, liveness);
  const digest = liveStatusDigest(conn, status) + '|' + liveness.state;
  if (digest === _lastStatusDigest) return;
  _lastStatusDigest = digest;

  patchText(_statusTextEl || _statusEl, conn);
  patchClass(_statusEl, 'dt-connected', state.connected);
  // The transport DOT is the healthy socket's only trace; the word is empty.
  patchClass(_statusEl, 'dt-transport-quiet', !conn);
  patchClass(_statusEl, 'dt-running', liveness.live && status.running);
  // A frozen heartbeat (stale rather than live) gets a distinct chrome class so the
  // dot/badge can read "not live" rather than borrowing the running accent.
  patchClass(_statusEl, 'dt-stale', !liveness.live && !!status.heartbeatStale);

  // The FOUR-STATE run pill — show the LIVE/STALLED/SETTLED/DEAD word only
  // while there is SOMETHING to report (a never-run workspace would read
  // SETTLED, which is misleading). One `dt-rs-<state>` modifier maps onto
  // the console states (no new hue — the class is toggled, never the accent).
  if (_runStateEl) {
    const everSeen = !!(state.heartbeat || state.activeTournament
      || (state.activeRuns && state.activeRuns.length) || state.lastSeq >= 0);
    // The tri-state decides WHETHER the run is live; the four-state verdict
    // only refines a live one into LIVE vs STALLED (alive, no progress).
    // A not-live workspace reads its own word — never a borrowed LIVE.
    const rs = liveness.live ? status.runState
      : (liveness.state === LIVENESS.SETTLED ? 'settled' : 'dead');
    const word = everSeen
      ? (liveness.state === LIVENESS.INTERRUPTED ? 'INTERRUPTED' : runStateLabel(rs))
      : '';
    if (_runStateTextEl) patchText(_runStateTextEl, word);
    patchClass(_runStateEl, 'dt-rs-live', word ? rs === 'live' : false);
    patchClass(_runStateEl, 'dt-rs-stalled', word ? rs === 'stalled' : false);
    patchClass(_runStateEl, 'dt-rs-settled', word ? rs === 'settled' : false);
    patchClass(_runStateEl, 'dt-rs-dead', word ? rs === 'dead' : false);
    patchClass(_runStateEl, 'dt-rs-on', !!word);
  }

  // THE PHASE rides INSIDE the pill after the state word ("LIVE · racing · rung
  // 0"); shown only while alive (LIVE / STALLED) so a settled/dead pill carries
  // no stale phase. The leading "· " is the in-pill separator.
  if (_runLabelEl) {
    patchText(_runLabelEl, liveness.live && status.label ? ('· ' + status.label) : '');
  }
  if (_runCountEl) {
    const n = status.inFlight;
    patchText(_runCountEl, liveness.live && n > 0 ? ('· ' + n + (n === 1 ? ' unit' : ' units')) : '');
  }
  // "· last seen Ns ago" inside the pill when the heartbeat has frozen (not
  // alive) — never a silent freeze; cleared while alive / when no heartbeat.
  if (_staleEl) {
    patchText(_staleEl, (!liveness.live && status.heartbeatStale)
      ? ('· ' + staleLabel(status.heartbeatAgeMs)) : '');
  }
}

// ── THE TOPBAR LOOP CONTROLS ─────────────────────────────────────────
//
// Pause/Resume toggle + Skip-round, driven through postControl. Shown ONLY when the workspace is writable (read_only:false
// from /api/health) AND the loop is alive (or already paused — a paused
// loop's heartbeat may be held, and the resume affordance must survive
// that). The pause flag state comes from the runtime payload's `paused`
// (readers/runtime_view.py) with a short optimistic override after a
// successful POST — a control write does not advance the orchestrator seq,
// so the SSE no-op-skip gate would otherwise delay the readback.

// The pure control cluster: a pause OR resume toggle (reflecting `paused`)
// plus a two-step confirm-on-click skip-round. Exported for the node tests.
export function buildLoopControls(opts) {
  const o = opts || {};
  const wrap = el('span', { class: 'dt-loopctl-group' });
  const toggle = el('button', {
    class: 'dt-loopctl-btn ' + (o.paused ? 'dt-loopctl-resume' : 'dt-loopctl-pause'),
    type: 'button',
    title: o.paused
      ? 'Resume — clear the pause flag; the orchestrator continues at its next poll'
      : 'Pause — hold scheduling at the next between-rounds safe point',
    text: o.paused ? '▶ resume' : '⏸ pause',
  });
  toggle.addEventListener('click', () => {
    if (o.paused) { if (o.onResume) o.onResume(); } else if (o.onPause) o.onPause();
  });
  wrap.appendChild(toggle);

  // Skip-round is destructive-ish (aborts the in-flight round like a budget
  // cut), so it takes a TWO-STEP confirm: first click arms, second fires;
  // an armed button auto-disarms after a few seconds.
  const skip = el('button', {
    class: 'dt-loopctl-btn dt-loopctl-skip', type: 'button',
    title: 'Skip the current round — aborts it cleanly, exactly like a wall-clock budget cut',
    text: '⏭ skip round',
  });
  let armed = false;
  let timer = null;
  const disarm = () => {
    armed = false;
    if (timer != null) { clearTimeout(timer); timer = null; }
    patchText(skip, '⏭ skip round');
    skip.classList.remove('dt-loopctl-armed');
  };
  skip.addEventListener('click', () => {
    if (!armed) {
      armed = true;
      patchText(skip, 'confirm skip?');
      skip.classList.add('dt-loopctl-armed');
      timer = setTimeout(disarm, 4000);
      return;
    }
    disarm();
    if (o.onSkip) o.onSkip();
  });
  wrap.appendChild(skip);
  return wrap;
}

async function fireLoopControl(action, body, pausedAfter) {
  let res = { ok: false, status: 0 };
  try { res = await postControl(action, body); } catch (err) { res = { ok: false, status: 0 }; }
  if (res.ok && pausedAfter != null) _pausedOverride = pausedAfter;
  // A control write does not advance the orchestrator progress seq, so the
  // SSE no-op-skip gate drops its state_change — refresh explicitly so the
  // paused readback converges (and the button flips) promptly.
  try { await loadEnvironment(); } catch (err) { /* transient — next beat retries */ }
  _lastLoopCtlDigest = null;
  renderStatus();
}

function renderLoopControls(status, liveness) {
  if (!_loopCtlHost) return;
  const canControl = !!(state.health && state.health.read_only === false);
  const serverPaused = !!(state.heartbeat && state.heartbeat.paused);
  // The optimistic override retires the moment the server agrees with it.
  if (_pausedOverride != null && serverPaused === _pausedOverride) _pausedOverride = null;
  const paused = _pausedOverride != null ? _pausedOverride : serverPaused;
  // Visible only against a LIVE loop on a writable workspace — pausing or
  // skipping a round of a run that died in June does nothing, and offering
  // it says the run is still going.
  //
  // The one exception is a PAUSED loop, which stays reachable regardless:
  // `block_while_paused` blocks the orchestrator thread, starving the
  // asyncio heartbeat beater, so a paused run ages into `interrupted` — and
  // resume must not become unreachable because of it. `paused` is the live
  // pause-FLAG presence, re-read server-side on every runtime payload, rather
  // than a value frozen into a dead heartbeat.
  const show = canControl && (!!(liveness && liveness.live) || paused);
  const digest = (show ? 'S' : '-') + (paused ? 'P' : '-');
  if (digest === _lastLoopCtlDigest && (!show || _loopCtlHost.firstChild)) return;
  _lastLoopCtlDigest = digest;
  clearChildren(_loopCtlHost);
  if (!show) return;
  _loopCtlHost.appendChild(buildLoopControls({
    paused,
    onPause: () => fireLoopControl('pause', { reason: 'operator pause (dashboard topbar)' }, true),
    onResume: () => fireLoopControl('resume', undefined, false),
    onSkip: () => fireLoopControl('skip-round', { reason: 'operator skip (dashboard topbar)' }, null),
  }));
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
// directly (core/sse.js), this runs sub-second: it is pushed rather than polled.
function refreshLive() {
  if (!_live) return;
  const { status, liveness } = livenessFor(state);
  _live.update({
    status,
    liveness,
    heartbeat: state.heartbeat,
    activeRuns: state.activeRuns,
    activeTournament: state.activeTournament,
    // the kill affordances render only on a writable workspace.
    canControl: !!(state.health && state.health.read_only === false),
  });
  // The host is always laid out (it carries the status band); the class only
  // tints it while a run is in flight.
  if (_heroHost) patchClass(_heroHost, 'dt-hero-live', liveness.live);
  // The AUTHORITATIVE round-pipeline stepper: fetched server-side (the reader
  // owns the phase-string inference) on each live tick, single-flight so a
  // burst of state:changed pulses never stacks fetches; the controller's
  // digest gate makes a re-served identical projection a zero-DOM no-op.
  refreshPipeline(liveness.live);
}

let _pipeInFlight = false;
async function refreshPipeline(alive) {
  if (!_live) return;
  if (!alive) { _live.updatePipeline(null); return; }
  if (_pipeInFlight) return;
  _pipeInFlight = true;
  try {
    const pipe = await D.livePipeline();
    _live.updatePipeline(pipe);
  } catch (err) {
    // absent endpoint (Rust supervisor) / transient failure → no stepper.
    _live.updatePipeline(null);
  } finally {
    _pipeInFlight = false;
  }
}

// Enable/disable the back control: it is inert at the environment root (no
// parent to climb to) and active everywhere else.
function renderBack(route) {
  if (!_backBtn) return;
  const dest = up(route);
  _backBtn.disabled = !dest;
  patchClass(_backBtn, 'dt-back-off', !dest);
}

// ── the settings overlay (Change 1) ──────────────────────────────────
//
// `#/settings` opens the drawer OVER the underlying view; closing it returns to
// the underlying route. The underlying route = the last non-settings route the
// shell dispatched (or home if loaded cold straight onto `#/settings`).

function openSettingsOverlay() {
  _settingsOpen = true;
  if (_settingsOverlay) {
    _settingsOverlay.setAttribute('data-open', '1');
    if (_settingsOverlay.classList) _settingsOverlay.classList.add('dt-drawer-open');
  }
}

function closeSettingsOverlay() {
  // Navigate back to the route the overlay paints over; dispatch then hides it.
  if (!_settingsOpen) return;
  const dest = _underlyingRoute || { view: 'home', params: {} };
  // Restore the follow flag too, or closing Settings would quietly stop a
  // conversation the operator left following.
  navigate(dest.view, dest.params, navOpts(dest));
}

function hideSettingsOverlay() {
  _settingsOpen = false;
  if (_settingsOverlay) {
    _settingsOverlay.setAttribute('data-open', '0');
    if (_settingsOverlay.classList) _settingsOverlay.classList.remove('dt-drawer-open');
  }
}

// Render the underlying view (the one the overlay sits over) into `_viewHost`,
// then the settings view into the drawer panel host. Tracked separately from
// the normal dispatch path so an Appearance change applies live to the page
// visible behind the scrim.
async function dispatchSettingsOverlay(route) {
  // The view the overlay paints over: the tracked underlying route. Render it
  // into the main host (so the page behind the scrim is the real underlying
  // view), reusing the normal renderer path, then paint settings into the panel.
  const under = _underlyingRoute || { view: 'home', params: {}, cmp: null };
  const underRenderer = RENDERERS[under.view] || RENDERERS.home;
  // The SAME key composition dispatch() uses for this route — so opening the
  // overlay over a view that is already painted leaves its DOM untouched
  // behind the scrim. A distinct 'under|'-prefixed key could never match the
  // key the normal dispatch stamped, which forced a clear + re-render of an
  // already-correct page on every first open.
  const underKey = under.view + '|' + JSON.stringify(under.params || {}) + '|' + (under.cmp || '') + '|' + (under.follow ? 'f' : '');
  if (_lastViewKey !== underKey) {
    clearChildren(_viewHost);
    _lastViewKey = underKey;
    try {
      await underRenderer.render(_viewHost, _ctx, under.params || {}, under);
    } catch (err) {
      clearChildren(_viewHost);
      _viewHost.appendChild(el('p', { class: 'dt-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
      // eslint-disable-next-line no-console
      console.error('console render error', err);
    }
  }

  openSettingsOverlay();
  // The settings view rebuilds its chrome ONCE per mount (it keys on
  // `host.firstChild`); the panel host is cleared on a non-settings dispatch
  // (when the overlay closes), so the next open rebuilds fresh. While the
  // overlay stays open, a section deep-link / a no-op heartbeat re-dispatch
  // keeps the existing chrome and only swaps the section (digest-gated) — no
  // DOM rebuild on a no-op beat (render discipline).
  const token = ++_renderToken;
  try {
    await RENDERERS.settings.render(_settingsPanelHost, _ctx, route.params || {}, route);
  } catch (err) {
    if (token !== _renderToken) return;
    clearChildren(_settingsPanelHost);
    _settingsPanelHost.appendChild(el('p', { class: 'dt-empty', text: 'Settings hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('console settings render error', err);
  }
}

async function dispatch() {
  const route = parseRoute(location.hash);

  renderCrumbs(route);
  renderStatus();
  renderExecLink();
  refreshLive();
  renderBack(route);
  renderTree(route);

  // SETTINGS is an OVERLAY rather than a `_viewHost` view: paint it into the
  // drawer over the underlying view rather than taking over the main host.
  if (route.view === 'settings') {
    await dispatchSettingsOverlay(route);
    return;
  }

  // A non-settings route closes the overlay (if open) and becomes the route the
  // overlay will paint over next time it opens.
  hideSettingsOverlay();
  _underlyingRoute = { view: route.view, params: route.params || {}, cmp: route.cmp || null, follow: !!route.follow };
  // Clear the settings panel host so the drawer re-mounts fresh next open.
  if (_settingsPanelHost && _settingsPanelHost.firstChild) clearChildren(_settingsPanelHost);

  const renderer = RENDERERS[route.view] || RENDERERS.home;
  // the compare target is part of the selection — a cmp change must clear +
  // repaint the detail pane (the split appears/disappears). So is the follow
  // flag: opening or closing the live conversation pane changes what the
  // board route shows, and without it here the pane would not appear until
  // some other selection changed.
  const viewKey = route.view + '|' + JSON.stringify(route.params || {}) + '|' + (route.cmp || '') + '|' + (route.follow ? 'f' : '');

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
    // target; a view that needs no compare target reads only `route.params`
    // (3rd arg).
    await renderer.render(_viewHost, _ctx, route.params, route);
  } catch (err) {
    if (token !== _renderToken) return;
    clearChildren(_viewHost);
    _viewHost.appendChild(el('p', { class: 'dt-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('console render error', err);
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
