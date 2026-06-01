// variants/I/shell.js — the Ledger shell + digest-gated view dispatch.
//
// Variant I ("Ledger") is the editorial, light-first publication skin built
// on Variant E's flow. The shell owns:
//   * an airy editorial top bar — a serif wordmark, the A-style
//     hierarchical breadcrumb (the IA that tested well), a primary-nav row
//     that surfaces the two NEW views (Mutations, Publication), a visible
//     THREE-THEME switcher (solarized-light default, solarized-dark,
//     monokai), and a connection status pill;
//   * ONE persistent content host (never recreated per repaint);
//   * digest-gated dispatch (DASHBOARD-V2 render discipline, mirrored from
//     js/v2/shell.js): a `state:changed` tick that only re-stamps a
//     heartbeat writes ZERO DOM — the shell coalesces ticks, re-runs the
//     ACTIVE view's render, and each view digest-gates its own repaint, so
//     a steady heartbeat never flashes anything.
//
// Render discipline enforced here (the recurring bugs MUST NOT reappear):
//   1. Breadcrumb + status pill each digest-gate their own writes.
//   2. On a VIEW switch the content host is cleared before the new view
//      renders, so a digest-gated view never wrongly skips its first paint.
//   3. Drill-down caches are invalidated ONLY on a view change / explicit
//      action — NEVER on a heartbeat.
//   4. Re-renders are debounced and routed through the active view's own
//      digest-gated render; the host is reused, never recreated.

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail } from './router.js';
import { invalidateLive } from './data.js';
import { initTheme, themeSwitcher } from './theme.js';

import * as home from './views/home.js';
import * as epoch from './views/epoch.js';
import * as candidate from './views/candidate.js';
import * as matchups from './views/matchups.js';
import * as mutations from './views/mutations.js';
import * as paper from './views/paper.js';
import * as run from './views/run.js';

const RENDERERS = { home, epoch, candidate, matchups, mutations, paper, run };
const NAV_ITEMS = [
  ['home', 'Environment'],
  ['epoch', 'Epoch'],
  ['candidate', 'Candidate'],
  ['matchups', 'Match-ups'],
  ['mutations', 'Mutations'],
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
  root.setAttribute('data-variant', 'I');
  initTheme(root);

  _crumbHost = el('nav', { class: 'i-crumbs', 'aria-label': 'Breadcrumb' });
  _navEl = NAV_ITEMS.map(([view, label]) =>
    el('a', { class: 'i-nav-link', href: href(view, {}), 'data-view': view, text: label }));
  _statusEl = el('span', { class: 'i-status' }, [
    el('span', { class: 'i-status-dot' }),
    el('span', { class: 'i-status-text', text: 'connecting…' }),
  ]);
  const topbar = el('header', { class: 'i-topbar' }, [
    el('div', { class: 'i-brand' }, [
      el('span', { class: 'i-brand-name', text: 'zicato' }),
      el('span', { class: 'i-brand-variant', text: 'ledger' }),
    ]),
    _crumbHost,
    el('span', { class: 'i-topbar-spacer' }),
    el('nav', { class: 'i-nav', 'aria-label': 'Primary' }, _navEl),
    themeSwitcher(el),
    _statusEl,
  ]);
  root.appendChild(topbar);

  _viewHost = el('main', { class: 'i-viewhost', role: 'main' });
  root.appendChild(_viewHost);

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/I')) location.hash = '#/I/';
  else dispatch();
}

function setActiveNav(view) {
  for (const a of _navEl) {
    const target = view === 'run' ? 'candidate' : view;
    patchClass(a, 'i-active', a.getAttribute('data-view') === target);
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
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'i-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'i-crumb i-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'i-crumb', href: href(c.view, c.params), text: c.label }));
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
  patchText(_statusEl.querySelector('.i-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'i-connected', state.connected);
  patchClass(_statusEl, 'i-running', live);
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
    console.error('variant-I render error', err);
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
