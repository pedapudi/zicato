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
import {
  COLOR_THEMES, DEFAULT_COLOR, normaliseColor, readColor, persistColor,
  TYPE_THEMES, DEFAULT_TYPE, normaliseType, readType, persistType,
  DENSITY,
  SCALE_MIN, SCALE_MAX, SCALE_STEP, DEFAULT_SCALE, normaliseScale, readScale, persistScale,
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
let _colorDropdown = null;     // the swatch-dropdown controller (Change 6)
let _typeEl = [];
let _scaleInput = null;
let _scaleReadout = null;
let _backBtn = null;
let _renderToken = 0;
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

// ---- the colour SWATCH DROPDOWN (Change 6) --------------------------
//
// Nine themes is too many for an inline button row, so the colour picker is a
// dropdown. The CLOSED control is a button showing the current theme's swatch
// strip + name. Opening reveals a listbox; each option is a row with its own
// swatch strip (ground · surface · ink · improve · regress — the legibility
// hint) + name. Fully keyboard-accessible: Enter/Space/ArrowDown open; within
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

  // COLOUR PICKER — a SWATCH DROPDOWN (Change 6). Nine themes now, so the inline
  // buttons are replaced by a keyboard-accessible dropdown: each option shows a
  // small swatch strip (ground · surface · ink · improve · regress) plus the
  // theme name; the closed control echoes the current theme's swatch + name.
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

  _statusEl = el('span', { class: 'dt-status' }, [
    el('span', { class: 'dt-status-dot' }),
    el('span', { class: 'dt-status-text', text: 'connecting…' }),
  ]);

  // top-left back/up control — navigates UP the hierarchy; dispatch then
  // repaints the destination into the MAIN detail pane (never the sidebar).
  _backBtn = el('button', { class: 'dt-back', type: 'button', title: 'Back / up one level', 'aria-label': 'Back' }, [
    el('span', { class: 'dt-back-glyph', 'aria-hidden': 'true', text: '‹' }),
    el('span', { class: 'dt-back-text', text: 'back' }),
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
  root.appendChild(el('div', { class: 'dt-body' }, [_treeHost, _viewHost]));

  applyTheme(readColor());
  applyTypeface(readType());
  applyScale(readScale());

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
async function buildTreeModel() {
  const [ws, lin, ep] = await Promise.all([D.workspace(), D.lineage(), D.epoch()]);
  const epochs = [];
  const seen = new Set();
  const current = ws ? ws.current_epoch_id : (ep && ep.epoch_id) || null;
  if (ws && Array.isArray(ws.epochs)) {
    for (const e of ws.epochs) {
      if (e && e.epoch_id != null && !seen.has(e.epoch_id)) {
        seen.add(e.epoch_id);
        epochs.push({ id: e.epoch_id, current: e.epoch_id === current });
      }
    }
  }
  if (ep && ep.epoch_id != null && !seen.has(ep.epoch_id)) {
    epochs.push({ id: ep.epoch_id, current: ep.epoch_id === current });
  }

  // Generations + boards: the live data has one epoch, so we resolve the
  // current epoch's bundle from /api/lineage + /api/epoch.board. Other epochs
  // appear as nodes that resolve their bundle when selected (degrade
  // gracefully — structure all-epochs-first).
  const byEpoch = {};
  for (const e of epochs) byEpoch[e.id] = { gens: [], boards: [] };
  if (ep && ep.epoch_id != null) {
    const id = ep.epoch_id;
    const gensList = (lin && Array.isArray(lin.generations) && lin.generations.length)
      ? lin.generations
        .filter((g) => !g.epoch_id || g.epoch_id === id)
        .map((g) => ({ id: g.generation_id, promoted: !!g.promoted, parent: g.parent_generation_id || null }))
      : (Array.isArray(ep.experiments) ? ep.experiments.map((x) => ({
          id: x.generation_id, parent: x.parent_generation_id || null,
          promoted: normaliseDecision(x.outcome) === 'promoted',
        })) : []);
    const boardList = (Array.isArray(ep.board) ? ep.board : []).map((b) => ({
      id: b.entry_id || b.id, kindTag: KIND_TAG[b.kind] || null,
    })).filter((b) => b.id);
    byEpoch[id] = { gens: gensList, boards: boardList };
  }
  return { epochs, byEpoch, current };
}

async function renderTree(route) {
  if (!_treeHost) return;
  const model = await buildTreeModel();
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

function renderStatus() {
  if (!_statusEl) return;
  const conn = state.connected ? 'live' : state.connecting ? 'connecting…' : 'offline';
  const live = !!state.activeTournament;
  const digest = conn + '|' + (live ? 'L' : '');
  if (digest === _lastStatusDigest) return;
  _lastStatusDigest = digest;
  patchText(_statusEl.querySelector('.dt-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'dt-connected', state.connected);
  patchClass(_statusEl, 'dt-running', live);
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
  if (_reRenderTimer != null) return;
  _reRenderTimer = setTimeout(() => {
    _reRenderTimer = null;
    dispatch();
  }, 400);
}

export { DEFAULT_COLOR, DEFAULT_TYPE, DEFAULT_SCALE, SCALE_MIN, SCALE_MAX, SCALE_STEP };
