// variants/H/shell.js — the Atlas II shell + digest-gated view dispatch.
//
// Variant H ("Atlas II") is E refined to completion. The shell keeps E's
// exact flow — a calm top bar (branding · hierarchical breadcrumb · a
// primary-nav row · a connection status pill) over ONE persistent content
// host, with digest-gated dispatch (a steady heartbeat writes ZERO DOM) —
// and adds:
//   * a visible THREE-theme switcher (solarized-light, solarized-dark
//     [default], monokai); the chosen theme sets `data-h-theme` on the
//     variant root and persists through localStorage. The switcher is
//     digest-free chrome (a click writes the attribute + repaints its own
//     active state) so it never participates in a view repaint.
//   * two new nav destinations — the mutation-site × generation matrix and
//     the ACM-style epoch publication.
//
// Render discipline (mirrored from E / js/v2/shell.js, the recurring
// flashing bugs MUST NOT reappear): breadcrumb + status pill digest-gate
// their own writes; on a VIEW switch the host is cleared and the drill-down
// cache invalidated (a user action, never a heartbeat); re-renders are
// debounced and routed through the active view's own digest-gated render;
// the host is reused, never recreated.

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail } from './router.js';
import { invalidateLive } from './data.js';

import * as home from './views/home.js';
import * as epoch from './views/epoch.js';
import * as candidate from './views/candidate.js';
import * as matchups from './views/matchups.js';
import * as run from './views/run.js';
import * as mutations from './views/mutations.js';
import * as report from './views/report.js';

const RENDERERS = { home, epoch, candidate, matchups, run, mutations, report };
const NAV_ITEMS = [
  ['home', 'Environment'],
  ['epoch', 'Epoch'],
  ['candidate', 'Candidate'],
  ['matchups', 'Match-ups'],
  ['mutations', 'Mutations'],
  ['report', 'Report'],
];

// The three themes. The first is the H DEFAULT (solarized-dark).
export const THEMES = [
  ['solarized-dark', 'dark'],
  ['solarized-light', 'light'],
  ['monokai', 'monokai'],
];
const THEME_KEY = 'zicato:variant-H:theme';
const DEFAULT_THEME = 'solarized-dark';

let _root = null;
let _viewHost = null;
let _crumbHost = null;
let _navEl = [];
let _statusEl = null;
let _themeBtns = [];
let _renderToken = 0;
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
let _theme = DEFAULT_THEME;
const _ctx = { navigate, href };

function readStoredTheme() {
  try {
    const v = window.localStorage && window.localStorage.getItem(THEME_KEY);
    if (v && THEMES.some(([id]) => id === v)) return v;
  } catch { /* ignore */ }
  return DEFAULT_THEME;
}

export function applyTheme(theme) {
  _theme = THEMES.some(([id]) => id === theme) ? theme : DEFAULT_THEME;
  if (_root) _root.setAttribute('data-h-theme', _theme);
  try { window.localStorage && window.localStorage.setItem(THEME_KEY, _theme); } catch { /* ignore */ }
  for (const b of _themeBtns) {
    patchClass(b, 'h-theme-active', b.getAttribute('data-theme') === _theme);
  }
}

export function currentTheme() { return _theme; }

export function mountShell(root) {
  _root = root;
  clearChildren(root);
  root.setAttribute('data-variant', 'H');
  _theme = readStoredTheme();
  root.setAttribute('data-h-theme', _theme);

  // ---- top bar -------------------------------------------------------
  _crumbHost = el('nav', { class: 'e-crumbs', 'aria-label': 'Breadcrumb' });
  _navEl = NAV_ITEMS.map(([view, label]) =>
    el('a', { class: 'e-nav-link', href: href(view, {}), 'data-view': view, text: label }));
  _statusEl = el('span', { class: 'e-status' }, [
    el('span', { class: 'e-status-dot' }),
    el('span', { class: 'e-status-text', text: 'connecting…' }),
  ]);
  _themeBtns = THEMES.map(([id, label]) =>
    el('button', {
      class: 'h-theme-btn' + (id === _theme ? ' h-theme-active' : ''),
      type: 'button', 'data-theme': id, title: 'theme: ' + id, text: label,
    }));
  for (const b of _themeBtns) b.addEventListener('click', () => applyTheme(b.getAttribute('data-theme')));
  const themeSwitch = el('div', { class: 'h-theme', role: 'group', 'aria-label': 'Theme' }, _themeBtns);

  const topbar = el('header', { class: 'e-topbar' }, [
    el('div', { class: 'e-brand' }, [
      el('span', { class: 'e-brand-name', text: 'zicato' }),
      el('span', { class: 'e-brand-variant', text: 'atlas ii' }),
    ]),
    _crumbHost,
    el('span', { class: 'e-topbar-spacer' }),
    el('nav', { class: 'e-nav', 'aria-label': 'Primary' }, _navEl),
    themeSwitch,
    _statusEl,
  ]);
  root.appendChild(topbar);

  // ---- the single persistent content host ----------------------------
  _viewHost = el('main', { class: 'e-viewhost', role: 'main' });
  root.appendChild(_viewHost);

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/H')) location.hash = '#/H/';
  else dispatch();
}

function setActiveNav(view) {
  for (const a of _navEl) {
    // The 'run' view sits under 'candidate'; light 'candidate' for both.
    const target = view === 'run' ? 'candidate' : view;
    patchClass(a, 'e-active', a.getAttribute('data-view') === target);
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
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'e-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'e-crumb e-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'e-crumb', href: href(c.view, c.params), text: c.label }));
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
  patchText(_statusEl.querySelector('.e-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'e-connected', state.connected);
  patchClass(_statusEl, 'e-running', live);
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
    console.error('variant-H render error', err);
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
