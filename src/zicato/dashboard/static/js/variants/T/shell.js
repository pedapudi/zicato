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
  DENSITY_THEMES, DEFAULT_DENSITY, normaliseDensity, readDensity, persistDensity,
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
export const TYPEFACES = TYPE_THEMES.map((t) => t[0]);
export const DENSITIES = DENSITY_THEMES.map((t) => t[0]);

const KIND_TAG = {
  single_turn: '1-turn', multi_turn_scripted: 'scripted', multi_turn_emulated: 'emulated',
};

let _root = null;
let _viewHost = null;
let _treeHost = null;
let _crumbHost = null;
let _statusEl = null;
let _colorEl = [];
let _typeEl = [];
let _densityEl = [];
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
  for (const b of _colorEl) patchClass(b, 'dt-theme-active', b.getAttribute('data-theme') === t);
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

// The THIRD picker — density / "roominess". A pure CSS-only swap: the root's
// `data-t-density` attribute drives the spacing/size custom properties, so the
// whole UI re-breathes without any re-render. Persisted like the other pickers.
export function applyDensity(density, rootEl) {
  const t = normaliseDensity(density);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-t-density', t);
  persistDensity(t);
  for (const b of _densityEl) patchClass(b, 'dt-density-active', b.getAttribute('data-density') === t);
  return t;
}

export function mountShell(root) {
  _root = root;
  clearChildren(root);
  _toggles.clear();
  _lastTreeDigest = null;
  root.setAttribute('data-variant', 'T');
  root.setAttribute('data-t-theme', readColor());
  root.setAttribute('data-t-type', readType());
  root.setAttribute('data-t-density', readDensity());

  _crumbHost = el('nav', { class: 'dt-crumbs', 'aria-label': 'Breadcrumb' });

  _colorEl = COLOR_THEMES.map(([id, label]) =>
    el('button', { class: 'dt-theme-btn', type: 'button', 'data-theme': id, title: 'colour: ' + id, text: label }));
  for (const b of _colorEl) b.addEventListener('click', () => applyTheme(b.getAttribute('data-theme')));
  const colorSwitch = el('div', { class: 'dt-theme-switch', role: 'group', 'aria-label': 'Colour theme' }, _colorEl);

  _typeEl = TYPE_THEMES.map(([id, label]) =>
    el('button', { class: 'dt-type-btn', type: 'button', 'data-type': id, title: 'typeface: ' + id, text: label }));
  for (const b of _typeEl) b.addEventListener('click', () => applyTypeface(b.getAttribute('data-type')));
  const typeSwitch = el('div', { class: 'dt-type-switch', role: 'group', 'aria-label': 'Typeface' }, _typeEl);

  _densityEl = DENSITY_THEMES.map(([id, label]) =>
    el('button', { class: 'dt-density-btn', type: 'button', 'data-density': id, title: 'density: ' + id, text: label }));
  for (const b of _densityEl) b.addEventListener('click', () => applyDensity(b.getAttribute('data-density')));
  const densitySwitch = el('div', { class: 'dt-density-switch', role: 'group', 'aria-label': 'Density' }, _densityEl);

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
    densitySwitch,
    _statusEl,
  ]);
  root.appendChild(topbar);

  _treeHost = el('aside', { class: 'dt-sidebar', 'aria-label': 'Data model navigation' });
  _viewHost = el('main', { class: 'dt-viewhost', role: 'main' });
  root.appendChild(el('div', { class: 'dt-body' }, [_treeHost, _viewHost]));

  applyTheme(readColor());
  applyTypeface(readType());
  applyDensity(readDensity());

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/T')) location.hash = '#/T/';
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

export { DEFAULT_COLOR, DEFAULT_TYPE, DEFAULT_DENSITY };
