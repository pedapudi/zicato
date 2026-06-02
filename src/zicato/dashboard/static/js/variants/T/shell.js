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
//     default) · typeface picker (Technical default) · status pill;
//   * the persistent tree sidebar (its own digest gate);
//   * ONE persistent detail host (never recreated per repaint);
//   * digest-gated dispatch — a `state:changed` tick that only re-stamps a
//     heartbeat writes ZERO DOM to either the tree or the detail pane.
//
// Theme + typeface are CSS-only swaps (data-t-theme / data-t-type on the root).

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail, up } from './router.js';
import * as D from './data.js';
import { invalidateLive } from './data.js';
import { buildTree, treeDigest } from './tree.js';
import { normaliseDecision } from './ui.js';
import { deriveLiveStatus, liveStatusDigest } from './livestatus.js';
import { LiveController } from './live.js';
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

const RENDERERS = { home, epoch, gens, candidate, diff, boards, board, mutations, publication };

export const THEMES = COLOR_THEMES.map((t) => t[0]);
const COLOR_IDS = THEMES;
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
let _colorDropdown = null;     // the swatch-dropdown controller (Change 6)
let _typeEl = [];
let _scaleInput = null;
let _scaleReadout = null;
let _railHandle = null;        // the draggable rail-resize handle (Change 2)
let _railDragging = false;     // true while a live rail drag is in flight
let _backBtn = null;
let _renderToken = 0;
let _live = null;             // the persistent LIVE-RUN controller (live hero + ticker)
let _heroHost = null;         // the persistent host the live hero leads from
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
let _lastTreeDigest = null;
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
  if (_colorDropdown) _colorDropdown.setValue(t);
  return t;
}

export function applyTypeface(typeface, rootEl) {
  const t = normaliseType(typeface);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-t-type', t);
  persistType(t);
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
// Sixteen themes is too many for an inline button row, so the colour picker is
// a dropdown. The CLOSED control is a button showing the current theme's swatch
// strip + name. Opening reveals a listbox; each option is a row with its own
// swatch strip (ground · surface · ink · improve · regress · accent — the
// legibility hint; rendered generically from the tuple, so it is swatch-count
// agnostic) + name. Fully keyboard-accessible: Enter/Space/ArrowDown open; within
// the open list ArrowUp/ArrowDown move the active option, Enter/Space select
// (and apply), Esc closes back to the trigger; a click outside also closes.
// Returns { node, setValue } so applyTheme() can keep the trigger + the
// checked option in sync when the theme changes by any path.
function swatchStrip(swatches, cls) {
  return el('span', { class: cls || 'dt-swatch-strip', 'aria-hidden': 'true' },
    (swatches || []).map((c) => el('span', { class: 'dt-swatch', style: `background:${c}` })));
}

function buildColorDropdown(initial) {
  let value = normaliseColor(initial);
  let open = false;
  const byId = new Map(COLOR_THEMES.map((t) => [t[0], t]));

  const triggerSwatch = swatchStrip((byId.get(value) || COLOR_THEMES[0])[2], 'dt-swatch-strip dt-swatch-strip-sm');
  const triggerName = el('span', { class: 'dt-cd-name', text: (byId.get(value) || COLOR_THEMES[0])[1] });
  const trigger = el('button', {
    class: 'dt-cd-trigger', type: 'button',
    'aria-haspopup': 'listbox', 'aria-expanded': 'false',
    'aria-label': 'Colour theme', title: 'Colour theme',
  }, [triggerSwatch, triggerName, el('span', { class: 'dt-cd-caret', 'aria-hidden': 'true', text: '▾' })]);

  const options = COLOR_THEMES.map(([id, label, swatches]) => {
    const opt = el('div', {
      class: 'dt-cd-option', role: 'option', 'data-theme': id,
      'aria-selected': String(id === value), tabindex: '-1', title: 'colour: ' + label,
    }, [swatchStrip(swatches), el('span', { class: 'dt-cd-name', text: label })]);
    opt.addEventListener('click', () => { choose(id); });
    return opt;
  });
  const listbox = el('div', { class: 'dt-cd-list', role: 'listbox', 'aria-label': 'Colour theme' }, options);

  const node = el('div', { class: 'dt-cd', role: 'group', 'aria-label': 'Colour theme' }, [trigger, listbox]);

  let activeIdx = COLOR_IDS.indexOf(value);
  function setActive(i) {
    activeIdx = (i + options.length) % options.length;
    options.forEach((o, k) => patchClass(o, 'dt-cd-active', k === activeIdx));
  }
  function setOpen(next) {
    open = next;
    patchClass(node, 'dt-cd-open', open);
    trigger.setAttribute('aria-expanded', String(open));
    if (open) setActive(Math.max(0, COLOR_IDS.indexOf(value)));
  }
  function choose(id) {
    value = normaliseColor(id);
    applyTheme(value);            // applies to the root + persists + syncs us
    setOpen(false);
  }
  function setValue(v) {
    value = normaliseColor(v);
    const def = byId.get(value) || COLOR_THEMES[0];
    clearChildren(triggerSwatch);
    for (const c of def[2]) triggerSwatch.appendChild(el('span', { class: 'dt-swatch', style: `background:${c}` }));
    patchText(triggerName, def[1]);
    options.forEach((o) => o.setAttribute('aria-selected', String(o.getAttribute('data-theme') === value)));
  }

  trigger.addEventListener('click', () => setOpen(!open));
  trigger.addEventListener('keydown', (ev) => {
    const k = ev.key;
    if (k === 'ArrowDown' || k === 'Enter' || k === ' ' || k === 'Spacebar') {
      ev.preventDefault(); setOpen(true);
    }
  });
  listbox.addEventListener('keydown', (ev) => {
    const k = ev.key;
    if (k === 'Escape') { ev.preventDefault(); setOpen(false); }
    else if (k === 'ArrowDown') { ev.preventDefault(); setActive(activeIdx + 1); }
    else if (k === 'ArrowUp') { ev.preventDefault(); setActive(activeIdx - 1); }
    else if (k === 'Enter' || k === ' ' || k === 'Spacebar') {
      ev.preventDefault();
      const id = options[activeIdx] && options[activeIdx].getAttribute('data-theme');
      if (id) choose(id);
    }
  });
  // a click anywhere outside the control closes it.
  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('click', (ev) => {
      if (!open) return;
      let n = ev && ev.target;
      while (n) { if (n === node) return; n = n.parentNode; }
      setOpen(false);
    });
  }

  return { node, setValue };
}

export function mountShell(root) {
  _root = root;
  clearChildren(root);
  _toggles.clear();
  _lastTreeDigest = null;
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
  _colorDropdown = buildColorDropdown(readColor());
  const colorSwitch = _colorDropdown.node;

  _typeEl = TYPE_THEMES.map(([id, label]) =>
    el('button', { class: 'dt-type-btn', type: 'button', 'data-type': id, title: 'typeface: ' + id, text: label }));
  for (const b of _typeEl) b.addEventListener('click', () => applyTypeface(b.getAttribute('data-type')));
  const typeSwitch = el('div', { class: 'dt-type-switch', role: 'group', 'aria-label': 'Typeface' }, _typeEl);

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
  _statusEl = el('span', { class: 'dt-status' }, [
    el('span', { class: 'dt-status-dot' }),
    _statusTextEl,
    el('span', { class: 'dt-run-badge', 'aria-live': 'polite' }, [
      el('span', { class: 'dt-run-pulse', 'aria-hidden': 'true' }),
      _runLabelEl,
      _runCountEl,
    ]),
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
      el('span', { class: 'dt-brand-name', text: 'zicato' }),
      el('span', { class: 'dt-brand-variant', text: 'console iv' }),
    ]),
    _crumbHost,
    el('span', { class: 'dt-topbar-spacer' }),
    colorSwitch,
    typeSwitch,
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
      ? linForEpoch.map((g) => ({ id: g.generation_id, promoted: g.promoted == null ? null : !!g.promoted, parent: g.parent_generation_id || null }))
      : (isContractEpoch && ep && Array.isArray(ep.experiments) ? ep.experiments.map((x) => ({
          id: x.generation_id, parent: x.parent_generation_id || null,
          promoted: normaliseDecision(x.outcome) === 'promoted' ? true : (normaliseDecision(x.outcome) === 'rejected' ? false : null),
        })) : []);
    // disambiguate the CURRENT champion (♚) from FORMER champions (hollow
    // crown) — the champion lineage applies to the contract epoch's bracket.
    const fallbackCurrent = currentChampionId == null
      ? (gensList.filter((g) => g.promoted === true).map((g) => g.id).pop() || null)
      : currentChampionId;
    for (const g of gensList) {
      const champ = isContractEpoch && g.promoted === true && String(g.id) === String(fallbackCurrent);
      g.currentChampion = champ;
      g.formerChampion = g.promoted === true && !champ;
    }
    // Boards come from the epoch contract — attach them only to the contract
    // epoch's node (the other epochs' boards resolve when that epoch is viewed).
    const boardList = (isContractEpoch && ep && Array.isArray(ep.board) ? ep.board : []).map((b) => ({
      id: b.entry_id || b.id, kindTag: KIND_TAG[b.kind] || null,
    })).filter((b) => b.id);
    byEpoch[id] = { gens: gensList, boards: boardList };
  }
  return { epochs, byEpoch, current };
}

async function renderTree(route) {
  if (!_treeHost) return;
  const model = await buildTreeModel(route);
  const digest = treeDigest(model, route, _toggles);
  if (digest === _lastTreeDigest && _treeHost.firstChild) return;
  _lastTreeDigest = digest;
  buildTree(_treeHost, model, route, _toggles, _ctx, (key) => {
    if (_toggles.has(key)) _toggles.delete(key); else _toggles.add(key);
    _lastTreeDigest = null;
    renderTree(parseRoute(location.hash));
  });
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

  if (_runLabelEl) patchText(_runLabelEl, status.running ? status.label : '');
  if (_runCountEl) {
    const n = status.inFlight;
    patchText(_runCountEl, status.running && n > 0 ? ('· ' + n + (n === 1 ? ' unit' : ' units')) : '');
  }
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
  // SSE-DRIVEN: refresh the live surfaces on EVERY tick (sub-second — the SSE
  // heartbeat frame fires state:changed directly), so live state (phase /
  // progress / funnel / activity) animates as it lands rather than waiting on
  // the 400 ms re-dispatch debounce. The hero patches in place (no full
  // repaint); the structure swap inside it stays digest-gated.
  refreshLive();
  if (_reRenderTimer != null) return;
  _reRenderTimer = setTimeout(() => {
    _reRenderTimer = null;
    dispatch();
  }, 400);
}

export { DEFAULT_COLOR, DEFAULT_TYPE, DEFAULT_SCALE, SCALE_MIN, SCALE_MAX, SCALE_STEP };
export { RAIL_MIN, RAIL_MAX, DEFAULT_RAIL };
