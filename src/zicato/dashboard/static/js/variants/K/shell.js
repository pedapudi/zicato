// variants/K/shell.js — the Monograph shell + digest-gated view dispatch.

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail } from './router.js';
import { invalidateLive } from './data.js';
import { readTheme, applyTheme, themeSwitcher } from './ui.js';

import * as paper from './views/home.js';
import * as candidate from './views/candidate.js';
import * as matchups from './views/matchups.js';
import * as mutations from './views/mutations.js';
import * as run from './views/run.js';

const RENDERERS = { paper, candidate, matchups, mutations, run };
const NAV_ITEMS = [
  ['paper', 'Paper'],
  ['candidate', 'Candidate'],
  ['matchups', 'Match-ups'],
  ['mutations', 'Mutation sites'],
];

let _root = null;
let _viewHost = null;
let _crumbHost = null;
let _navEl = [];
let _statusEl = null;
let _themeHost = null;
let _theme = null;
let _renderToken = 0;
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
const _ctx = { navigate, href };

export function mountShell(root) {
  clearChildren(root);
  _root = root;
  root.setAttribute('data-variant', 'K');
  _theme = applyTheme(root, readTheme());

  _crumbHost = el('nav', { class: 'vk-crumbs', 'aria-label': 'Breadcrumb' });
  _navEl = NAV_ITEMS.map(([view, label]) =>
    el('a', { class: 'vk-nav-link', href: href(view, {}), 'data-view': view, text: label }));
  _statusEl = el('span', { class: 'vk-status' }, [
    el('span', { class: 'vk-status-dot' }),
    el('span', { class: 'vk-status-text', text: 'connecting…' }),
  ]);
  _themeHost = el('span', { class: 'vk-theme-host' });
  renderThemeSwitcher();

  const topbar = el('header', { class: 'vk-topbar' }, [
    el('div', { class: 'vk-brand' }, [
      el('span', { class: 'vk-brand-name', text: 'zicato' }),
      el('span', { class: 'vk-brand-variant', text: 'monograph' }),
    ]),
    _crumbHost,
    el('span', { class: 'vk-topbar-spacer' }),
    el('nav', { class: 'vk-nav', 'aria-label': 'Primary' }, _navEl),
    _themeHost,
    _statusEl,
  ]);
  root.appendChild(topbar);

  _viewHost = el('main', { class: 'vk-viewhost', role: 'main' });
  root.appendChild(_viewHost);

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/K')) location.hash = '#/K/';
  else dispatch();
}

function renderThemeSwitcher() {
  if (!_themeHost) return;
  clearChildren(_themeHost);
  _themeHost.appendChild(themeSwitcher(_theme, (next) => {
    _theme = applyTheme(_root, next);
    renderThemeSwitcher(); // re-stamp the active button (CSS-only; no view rebuild)
  }));
}

function setActiveNav(view) {
  for (const a of _navEl) {
    const target = view === 'run' ? 'candidate' : view;
    patchClass(a, 'vk-active', a.getAttribute('data-view') === target);
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
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'vk-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'vk-crumb vk-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'vk-crumb', href: href(c.view, c.params), text: c.label }));
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
  patchText(_statusEl.querySelector('.vk-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'vk-connected', state.connected);
  patchClass(_statusEl, 'vk-running', live);
}

async function dispatch() {
  const route = parseRoute(location.hash);
  const renderer = RENDERERS[route.view] || RENDERERS.paper;
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
    _viewHost.appendChild(el('p', { class: 'vk-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-K render error', err);
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
