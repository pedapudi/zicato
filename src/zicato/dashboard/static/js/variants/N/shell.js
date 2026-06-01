// variants/N/shell.js — the Console II shell + digest-gated view dispatch.
//
// Variant N ("Console II") is a dense, data-ink-maximal convergence-II
// observatory built on Variant E's IA/flow. The shell owns:
//   * a COMPACT top bar — branding · breadcrumb · dense primary-nav (incl. the
//     NEW Mutations, Board, and Publication tabs) · a COLOUR-theme picker
//     (monokai default) · a TYPEFACE picker (Technical default) · a status pill;
//   * ONE persistent content host (never recreated per repaint);
//   * digest-gated dispatch — a `state:changed` tick that only re-stamps a
//     heartbeat writes ZERO DOM.
//
// Theme + typeface are CSS-only swaps (data-n-theme / data-n-type on the root):
// the whole sheet re-skins without rebuilding any view. Both persist.

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail } from './router.js';
import { invalidateLive } from './data.js';
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

const RENDERERS = { home, epoch, candidate, matchups, mutations, board, publication, run };
const NAV_ITEMS = [
  ['home', 'env'],
  ['epoch', 'epoch'],
  ['candidate', 'candidate'],
  ['matchups', 'match-ups'],
  ['mutations', 'mutations'],
  ['board', 'board'],
  ['publication', 'paper'],
];

export const THEMES = COLOR_THEMES.map((t) => t[0]);
export const TYPEFACES = TYPE_THEMES.map((t) => t[0]);

let _root = null;
let _viewHost = null;
let _crumbHost = null;
let _navEl = [];
let _statusEl = null;
let _colorEl = [];
let _typeEl = [];
let _renderToken = 0;
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
const _ctx = { navigate, href };

// Apply (and persist) the COLOUR theme by swapping [data-n-theme] on the root.
export function applyTheme(theme, rootEl) {
  const t = normaliseColor(theme);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-n-theme', t);
  persistColor(t);
  for (const b of _colorEl) patchClass(b, 'dn-theme-active', b.getAttribute('data-theme') === t);
  return t;
}

// Apply (and persist) the TYPEFACE theme by swapping [data-n-type] on the root.
export function applyTypeface(typeface, rootEl) {
  const t = normaliseType(typeface);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-n-type', t);
  persistType(t);
  for (const b of _typeEl) patchClass(b, 'dn-type-active', b.getAttribute('data-type') === t);
  return t;
}

export function mountShell(root) {
  _root = root;
  clearChildren(root);
  root.setAttribute('data-variant', 'N');
  root.setAttribute('data-n-theme', readColor());
  root.setAttribute('data-n-type', readType());

  _crumbHost = el('nav', { class: 'dn-crumbs', 'aria-label': 'Breadcrumb' });
  _navEl = NAV_ITEMS.map(([view, label]) =>
    el('a', { class: 'dn-nav-link', href: href(view, {}), 'data-view': view, text: label }));

  _colorEl = COLOR_THEMES.map(([id, label]) =>
    el('button', { class: 'dn-theme-btn', type: 'button', 'data-theme': id, title: 'colour: ' + id, text: label }));
  for (const b of _colorEl) b.addEventListener('click', () => applyTheme(b.getAttribute('data-theme')));
  const colorSwitch = el('div', { class: 'dn-theme-switch', role: 'group', 'aria-label': 'Colour theme' }, _colorEl);

  _typeEl = TYPE_THEMES.map(([id, label]) =>
    el('button', { class: 'dn-type-btn', type: 'button', 'data-type': id, title: 'typeface: ' + id, text: label }));
  for (const b of _typeEl) b.addEventListener('click', () => applyTypeface(b.getAttribute('data-type')));
  const typeSwitch = el('div', { class: 'dn-type-switch', role: 'group', 'aria-label': 'Typeface' }, _typeEl);

  _statusEl = el('span', { class: 'dn-status' }, [
    el('span', { class: 'dn-status-dot' }),
    el('span', { class: 'dn-status-text', text: 'connecting…' }),
  ]);

  const topbar = el('header', { class: 'dn-topbar' }, [
    el('div', { class: 'dn-brand' }, [
      el('span', { class: 'dn-brand-name', text: 'zicato' }),
      el('span', { class: 'dn-brand-variant', text: 'console ii' }),
    ]),
    _crumbHost,
    el('span', { class: 'dn-topbar-spacer' }),
    el('nav', { class: 'dn-nav', 'aria-label': 'Primary' }, _navEl),
    colorSwitch,
    typeSwitch,
    _statusEl,
  ]);
  root.appendChild(topbar);

  _viewHost = el('main', { class: 'dn-viewhost', role: 'main' });
  root.appendChild(_viewHost);

  applyTheme(readColor());
  applyTypeface(readType());

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/N')) location.hash = '#/N/';
  else dispatch();
}

function setActiveNav(view) {
  for (const a of _navEl) {
    // run + board sit under the candidate/epoch context; light their nearest tab.
    let target = view;
    if (view === 'run') target = 'candidate';
    patchClass(a, 'dn-active', a.getAttribute('data-view') === target);
  }
}

function renderCrumbs(route) {
  if (!_crumbHost) return;
  const trail = crumbTrail(route);
  const digest = JSON.stringify(trail.map((c) => [c.label, c.view || '', c.current || false]));
  if (digest === _lastCrumbDigest && _crumbHost.firstChild) return;
  _lastCrumbDigest = digest;
  clearChildren(_crumbHost);
  trail.forEach((c, i) => {
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'dn-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'dn-crumb dn-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'dn-crumb', href: href(c.view, c.params), text: c.label }));
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
  patchText(_statusEl.querySelector('.dn-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'dn-connected', state.connected);
  patchClass(_statusEl, 'dn-running', live);
}

async function dispatch() {
  const route = parseRoute(location.hash);
  const renderer = RENDERERS[route.view] || RENDERERS.home;
  const viewKey = route.view + '|' + JSON.stringify(route.params || {});

  setActiveNav(route.view);
  renderCrumbs(route);
  renderStatus();

  const prevView = _lastViewKey == null ? null : String(_lastViewKey).split('|')[0];
  if (prevView !== route.view) {
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
    _viewHost.appendChild(el('p', { class: 'dn-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-N render error', err);
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

export { DEFAULT_COLOR, DEFAULT_TYPE };
