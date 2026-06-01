// variants/J/shell.js — the Console shell + digest-gated view dispatch.
//
// Variant J ("Console") is a dense, data-ink-maximal observatory for power
// users, built on Variant E's IA/flow. The shell owns:
//   * a COMPACT top bar — branding · hierarchical breadcrumb · a dense
//     primary-nav row (incl. the two NEW views: Mutations + Report) · a
//     three-theme switcher (monokai default) · a connection status pill;
//   * ONE persistent content host (never recreated per repaint);
//   * digest-gated dispatch (DASHBOARD-V2 render discipline): a
//     `state:changed` tick that only re-stamps a heartbeat writes ZERO DOM.
//
// Render discipline enforced here (the recurring flashing bugs MUST NOT
// reappear):
//   1. Breadcrumb + status pill each digest-gate their own writes.
//   2. On a VIEW switch the content host is cleared before the new view
//      renders, so a digest-gated view never skips its first paint.
//   3. Drill-down caches are invalidated ONLY on a view change or an
//      explicit user action — NEVER on every heartbeat.
//   4. Re-renders are debounced + routed through the active view's own
//      digest-gated render; the host is reused, never recreated.
//   5. Theme is a CSS `transition`, never an infinite animation.

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
import * as mutations from './views/mutations.js';
import * as report from './views/report.js';
import * as run from './views/run.js';

const RENDERERS = { home, epoch, candidate, matchups, mutations, report, run };
const NAV_ITEMS = [
  ['home', 'env'],
  ['epoch', 'epoch'],
  ['candidate', 'candidate'],
  ['matchups', 'match-ups'],
  ['mutations', 'mutations'],
  ['report', 'report'],
];

// The three themes (monokai is J's default). Persisted in localStorage.
export const THEMES = ['monokai', 'solarized-dark', 'solarized-light'];
const THEME_LABEL = { monokai: 'monokai', 'solarized-dark': 'sol·dark', 'solarized-light': 'sol·light' };
const THEME_KEY = 'zicato.J.theme';

let _root = null;
let _viewHost = null;
let _crumbHost = null;
let _navEl = [];
let _statusEl = null;
let _themeEl = [];
let _renderToken = 0;
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
const _ctx = { navigate, href };

function readTheme() {
  try {
    const t = window.localStorage.getItem(THEME_KEY);
    if (t && THEMES.includes(t)) return t;
  } catch (e) { /* private mode */ }
  return 'monokai';
}

// Apply (and persist) a theme by swapping [data-j-theme] on the variant
// root — the single attribute the whole console.css keys on, so one swap
// restyles every mark. `rootEl` lets callers (and the test suite) target a
// root without going through the full SSE/router boot.
export function applyTheme(theme, rootEl) {
  const t = THEMES.includes(theme) ? theme : 'monokai';
  const root = rootEl || _root;
  if (root) root.setAttribute('data-j-theme', t);
  try { window.localStorage.setItem(THEME_KEY, t); } catch (e) { /* ignore */ }
  for (const b of _themeEl) patchClass(b, 'dj-theme-active', b.getAttribute('data-theme') === t);
  return t;
}

export function mountShell(root) {
  _root = root;
  clearChildren(root);
  root.setAttribute('data-variant', 'J');
  root.setAttribute('data-j-theme', readTheme());

  // ---- compact top bar ----------------------------------------------
  _crumbHost = el('nav', { class: 'dj-crumbs', 'aria-label': 'Breadcrumb' });
  _navEl = NAV_ITEMS.map(([view, label]) =>
    el('a', { class: 'dj-nav-link', href: href(view, {}), 'data-view': view, text: label }));

  _themeEl = THEMES.map((t) =>
    el('button', { class: 'dj-theme-btn', type: 'button', 'data-theme': t, title: 'theme: ' + t, text: THEME_LABEL[t] || t }));
  for (const b of _themeEl) b.addEventListener('click', () => applyTheme(b.getAttribute('data-theme')));
  const themeSwitch = el('div', { class: 'dj-theme-switch', role: 'group', 'aria-label': 'Theme' }, _themeEl);

  _statusEl = el('span', { class: 'dj-status' }, [
    el('span', { class: 'dj-status-dot' }),
    el('span', { class: 'dj-status-text', text: 'connecting…' }),
  ]);

  const topbar = el('header', { class: 'dj-topbar' }, [
    el('div', { class: 'dj-brand' }, [
      el('span', { class: 'dj-brand-name', text: 'zicato' }),
      el('span', { class: 'dj-brand-variant', text: 'console' }),
    ]),
    _crumbHost,
    el('span', { class: 'dj-topbar-spacer' }),
    el('nav', { class: 'dj-nav', 'aria-label': 'Primary' }, _navEl),
    themeSwitch,
    _statusEl,
  ]);
  root.appendChild(topbar);

  // ---- the single persistent content host ---------------------------
  _viewHost = el('main', { class: 'dj-viewhost', role: 'main' });
  root.appendChild(_viewHost);

  applyTheme(readTheme());

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/J')) location.hash = '#/J/';
  else dispatch();
}

function setActiveNav(view) {
  for (const a of _navEl) {
    // run sits under candidate; light candidate for both.
    const target = view === 'run' ? 'candidate' : view;
    patchClass(a, 'dj-active', a.getAttribute('data-view') === target);
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
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'dj-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'dj-crumb dj-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'dj-crumb', href: href(c.view, c.params), text: c.label }));
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
  patchText(_statusEl.querySelector('.dj-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'dj-connected', state.connected);
  patchClass(_statusEl, 'dj-running', live);
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
    _viewHost.appendChild(el('p', { class: 'dj-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-J render error', err);
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
