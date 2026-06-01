// variants/Q/shell.js — the Atlas IV shell: persistent TREE sidebar + one
// well-spaced detail pane + digest-gated view dispatch.
//
// Variant Q ("Atlas IV") is the roomy, comfortable convergence-III dashboard:
// N's content/diagrams with M's generous spacing/proportion, Solarized-Dark +
// Sans by default, L's mutation-viewer quality throughout. The HEADLINE is a
// persistent LEFT TREE grounded in the data model (see tree.js) that replaces
// N's top-tab nav and can navigate MULTIPLE epochs AND generations.
//
// The shell owns:
//   * a slim top bar — branding · breadcrumb · COLOUR picker (sol-dark default)
//     · TYPEFACE picker (Sans default) · status pill · a sidebar collapse toggle;
//   * the persistent TREE sidebar (rebuilt only when its model digest changes);
//   * ONE persistent detail host (never recreated per repaint);
//   * digest-gated dispatch — a `state:changed` tick that only re-stamps a
//     heartbeat writes ZERO DOM.
//
// Theme + typeface are CSS-only swaps (data-q-theme / data-q-type on the root).

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail } from './router.js';
import * as D from './data.js';
import { invalidateLive } from './data.js';
import { buildTree } from './tree.js';
import {
  COLOR_THEMES, DEFAULT_COLOR, normaliseColor, readColor, persistColor,
  TYPE_THEMES, DEFAULT_TYPE, normaliseType, readType, persistType,
} from './ui.js';

import * as home from './views/home.js';
import * as epoch from './views/epoch.js';
import * as candidate from './views/candidate.js';
import * as matchups from './views/matchups.js';
import * as mutations from './views/mutations.js';
import * as board from './views/board.js';
import * as publication from './views/publication.js';
import * as run from './views/run.js';

const RENDERERS = { home, epoch, gen: candidate, matchups, mutations, board, publication, run };

export const THEMES = COLOR_THEMES.map((t) => t[0]);
export const TYPEFACES = TYPE_THEMES.map((t) => t[0]);

const SIDEBAR_KEY = 'zicato.Q.sidebar';

let _root = null;
let _viewHost = null;
let _treeHost = null;
let _crumbHost = null;
let _statusEl = null;
let _colorEl = [];
let _typeEl = [];
let _collapseBtn = null;
let _renderToken = 0;
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
let _lastTreeDigest = null;
let _treeModel = null;
const _ctx = { navigate, href, rerenderTree: () => renderTree(true) };

export function applyTheme(theme, rootEl) {
  const t = normaliseColor(theme);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-q-theme', t);
  persistColor(t);
  for (const b of _colorEl) patchClass(b, 'dq-theme-active', b.getAttribute('data-theme') === t);
  return t;
}

export function applyTypeface(typeface, rootEl) {
  const t = normaliseType(typeface);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-q-type', t);
  persistType(t);
  for (const b of _typeEl) patchClass(b, 'dq-type-active', b.getAttribute('data-type') === t);
  return t;
}

function readSidebar() {
  try { return window.localStorage.getItem(SIDEBAR_KEY) !== 'collapsed'; } catch (e) { return true; }
}
function persistSidebar(open) {
  try { window.localStorage.setItem(SIDEBAR_KEY, open ? 'open' : 'collapsed'); } catch (e) { /* ignore */ }
}
function applySidebar(open) {
  if (_root) patchClass(_root, 'dq-sidebar-collapsed', !open);
  if (_collapseBtn) patchText(_collapseBtn, open ? '⟨' : '⟩');
  persistSidebar(open);
}

export function mountShell(root) {
  _root = root;
  clearChildren(root);
  root.setAttribute('data-variant', 'Q');
  root.setAttribute('data-q-theme', readColor());
  root.setAttribute('data-q-type', readType());

  _crumbHost = el('nav', { class: 'dq-crumbs', 'aria-label': 'Breadcrumb' });

  _colorEl = COLOR_THEMES.map(([id, label]) =>
    el('button', { class: 'dq-theme-btn', type: 'button', 'data-theme': id, title: 'colour: ' + id, text: label }));
  for (const b of _colorEl) b.addEventListener('click', () => applyTheme(b.getAttribute('data-theme')));
  const colorSwitch = el('div', { class: 'dq-theme-switch', role: 'group', 'aria-label': 'Colour theme' }, _colorEl);

  _typeEl = TYPE_THEMES.map(([id, label]) =>
    el('button', { class: 'dq-type-btn', type: 'button', 'data-type': id, title: 'typeface: ' + id, text: label }));
  for (const b of _typeEl) b.addEventListener('click', () => applyTypeface(b.getAttribute('data-type')));
  const typeSwitch = el('div', { class: 'dq-type-switch', role: 'group', 'aria-label': 'Typeface' }, _typeEl);

  _statusEl = el('span', { class: 'dq-status' }, [
    el('span', { class: 'dq-status-dot' }),
    el('span', { class: 'dq-status-text', text: 'connecting…' }),
  ]);

  _collapseBtn = el('button', { class: 'dq-collapse', type: 'button', title: 'Toggle sidebar', text: '⟨' });
  _collapseBtn.addEventListener('click', () => applySidebar(_root.classList.contains('dq-sidebar-collapsed')));

  const topbar = el('header', { class: 'dq-topbar' }, [
    _collapseBtn,
    el('div', { class: 'dq-brand' }, [
      el('span', { class: 'dq-brand-name', text: 'zicato' }),
      el('span', { class: 'dq-brand-variant', text: 'atlas iv' }),
    ]),
    _crumbHost,
    el('span', { class: 'dq-topbar-spacer' }),
    colorSwitch,
    typeSwitch,
    _statusEl,
  ]);
  root.appendChild(topbar);

  // The two-column body: persistent tree aside + one detail host.
  _treeHost = el('aside', { class: 'dq-sidebar', 'aria-label': 'Navigation tree' });
  _viewHost = el('main', { class: 'dq-viewhost', role: 'main' });
  root.appendChild(el('div', { class: 'dq-body' }, [_treeHost, _viewHost]));

  applyTheme(readColor());
  applyTypeface(readType());
  applySidebar(readSidebar());

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/Q')) location.hash = '#/Q/';
  else dispatch();
}

// ---- the tree model -------------------------------------------------
//
// Loads the workspace epochs and, per epoch, its lineage (generations) and
// board (entries). Live data has one epoch — non-current epochs may lack a
// board endpoint; we degrade gracefully (empty group, still navigable).
async function loadTreeModel() {
  const [ws, lin, ep] = await Promise.all([D.workspace(), D.lineage(), D.epoch()]);
  const epochs = (ws && Array.isArray(ws.epochs) && ws.epochs.length)
    ? ws.epochs.slice()
    : (ep && ep.epoch_id ? [{ epoch_id: ep.epoch_id, closed: !!ep.closed }] : []);

  const lineageByEpoch = {};
  const boardByEpoch = {};
  const allGens = (lin && Array.isArray(lin.generations)) ? lin.generations : [];
  for (const e of epochs) {
    const eid = e.epoch_id;
    lineageByEpoch[eid] = allGens
      .filter((g) => !g.epoch_id || g.epoch_id === eid)
      .map((g) => ({ id: g.generation_id, parent: g.parent_generation_id || null, promoted: !!g.promoted }));
    // the board is per-epoch; we have the CURRENT epoch's board from /api/epoch.
    if (ep && ep.epoch_id === eid && Array.isArray(ep.board)) {
      boardByEpoch[eid] = ep.board.slice();
    } else {
      boardByEpoch[eid] = [];
    }
  }
  return { epochs, lineageByEpoch, boardByEpoch };
}

function treeDigest(model, route) {
  const sel = JSON.stringify(route.params || {}) + '|' + route.view;
  if (!model) return 'no-model|' + sel;
  return JSON.stringify({
    epochs: model.epochs.map((e) => [e.epoch_id, !!e.closed,
      (model.lineageByEpoch[e.epoch_id] || []).map((g) => [g.id, g.promoted]),
      (model.boardByEpoch[e.epoch_id] || []).map((b) => b.entry_id || b.id)]),
    sel,
  });
}

async function renderTree(force) {
  if (!_treeHost) return;
  if (force || !_treeModel) _treeModel = await loadTreeModel();
  const route = parseRoute(location.hash);
  const digest = treeDigest(_treeModel, route);
  if (!force && digest === _lastTreeDigest && _treeHost.firstChild) return;
  _lastTreeDigest = digest;
  clearChildren(_treeHost);
  _treeHost.appendChild(buildTree(_treeModel, _ctx, route));
}

function renderCrumbs(route) {
  if (!_crumbHost) return;
  const trail = crumbTrail(route);
  const digest = JSON.stringify(trail.map((c) => [c.label, c.view || '', c.current || false]));
  if (digest === _lastCrumbDigest && _crumbHost.firstChild) return;
  _lastCrumbDigest = digest;
  clearChildren(_crumbHost);
  trail.forEach((c, i) => {
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'dq-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'dq-crumb dq-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'dq-crumb', href: href(c.view, c.params), text: c.label }));
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
  patchText(_statusEl.querySelector('.dq-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'dq-connected', state.connected);
  patchClass(_statusEl, 'dq-running', live);
}

async function dispatch() {
  const route = parseRoute(location.hash);
  const renderer = RENDERERS[route.view] || RENDERERS.home;
  const viewKey = route.view + '|' + JSON.stringify(route.params || {});

  renderCrumbs(route);
  renderStatus();
  renderTree(false);

  const prevView = _lastViewKey == null ? null : String(_lastViewKey).split('|')[0];
  const prevParams = _lastViewKey == null ? null : _lastViewKey.slice(String(prevView).length + 1);
  const nextParams = JSON.stringify(route.params || {});
  if (prevView !== route.view || prevParams !== nextParams) {
    // Selection changed — clear the host and bust the per-selection caches.
    clearChildren(_viewHost);
    invalidateLive();
  }
  _lastViewKey = viewKey;

  const token = ++_renderToken;
  try {
    await renderer.render(_viewHost, _ctx, route.params);
  } catch (err) {
    if (token !== _renderToken) return;
    clearChildren(_viewHost);
    _viewHost.appendChild(el('p', { class: 'dq-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-Q render error', err);
  }
}

let _reRenderTimer = null;
function onStateChanged() {
  renderStatus();
  if (_reRenderTimer != null) return;
  _reRenderTimer = setTimeout(() => {
    _reRenderTimer = null;
    renderTree(true);
    dispatch();
  }, 400);
}

export { DEFAULT_COLOR, DEFAULT_TYPE };
