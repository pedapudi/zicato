// variants/M/shell.js — the Ledger II shell + digest-gated view dispatch.
//
// Variant M ("Ledger II") is the editorial, light-first publication skin
// built on Variant E's flow. The shell owns:
//   * an airy editorial top bar — a serif wordmark, E's hierarchical
//     breadcrumb, a primary nav that surfaces the convergence-II views
//     (Mutations, Board, Publication), TWO pickers (a colour-theme switcher
//     and the NEW typeface switcher), and a connection status pill;
//   * ONE persistent content host (never recreated per repaint);
//   * digest-gated dispatch (mirrored from js/v2/shell.js): a `state:changed`
//     tick that only re-stamps a heartbeat writes ZERO DOM.
//
// Render discipline: breadcrumb + status pill digest-gate their own writes;
// on a VIEW switch the host is cleared + caches invalidated; re-renders are
// debounced and routed through the active view's own digest-gated render.

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail } from './router.js';
import { invalidateLive } from './data.js';
import { initTheme, themeSwitcher } from './theme.js';
import { initFace, faceSwitcher } from './typeface.js';

import * as home from './views/home.js';
import * as epoch from './views/epoch.js';
import * as candidate from './views/candidate.js';
import * as matchups from './views/matchups.js';
import * as mutations from './views/mutations.js';
import * as board from './views/board.js';
import * as paper from './views/paper.js';
import * as run from './views/run.js';

const RENDERERS = { home, epoch, candidate, matchups, mutations, board, paper, run };
const NAV_ITEMS = [
  ['home', 'Environment'],
  ['epoch', 'Epoch'],
  ['candidate', 'Candidate'],
  ['matchups', 'Match-ups'],
  ['mutations', 'Mutations'],
  ['board', 'Board'],
  ['paper', 'Publication'],
];

let _viewHost = null;
let _crumbHost = null;
let _navEl = [];
let _statusEl = null;
let _renderToken = 0;
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
const _ctx = { navigate, href };

export function mountShell(root) {
  clearChildren(root);
  root.setAttribute('data-variant', 'M');
  initTheme(root);
  initFace(root);

  _crumbHost = el('nav', { class: 'm-crumbs', 'aria-label': 'Breadcrumb' });
  _navEl = NAV_ITEMS.map(([view, label]) =>
    el('a', { class: 'm-nav-link', href: href(view, {}), 'data-view': view, text: label }));
  _statusEl = el('span', { class: 'm-status' }, [
    el('span', { class: 'm-status-dot' }),
    el('span', { class: 'm-status-text', text: 'connecting…' }),
  ]);
  const topbar = el('header', { class: 'm-topbar' }, [
    el('div', { class: 'm-brand' }, [
      el('span', { class: 'm-brand-name', text: 'zicato' }),
      el('span', { class: 'm-brand-variant', text: 'ledger ii' }),
    ]),
    _crumbHost,
    el('span', { class: 'm-topbar-spacer' }),
    el('nav', { class: 'm-nav', 'aria-label': 'Primary' }, _navEl),
    el('div', { class: 'm-pickers' }, [faceSwitcher(el), themeSwitcher(el)]),
    _statusEl,
  ]);
  root.appendChild(topbar);

  _viewHost = el('main', { class: 'm-viewhost', role: 'main' });
  root.appendChild(_viewHost);

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/M')) location.hash = '#/M/';
  else dispatch();
}

function setActiveNav(view) {
  for (const a of _navEl) {
    const target = view === 'run' ? 'candidate' : view;
    patchClass(a, 'm-active', a.getAttribute('data-view') === target);
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
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'm-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'm-crumb m-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'm-crumb', href: href(c.view, c.params), text: c.label }));
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
  patchText(_statusEl.querySelector('.m-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'm-connected', state.connected);
  patchClass(_statusEl, 'm-running', live);
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
    _viewHost.appendChild(el('p', { class: 'd-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-M render error', err);
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
