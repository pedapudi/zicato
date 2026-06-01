// variants/E/shell.js — the Atlas shell + digest-gated view dispatch.
//
// Variant E ("Atlas") is a data-first observatory. The shell owns:
//   * a calm top bar — branding · A-style hierarchical breadcrumb (the IA
//     that tested well) · a primary-nav row · a connection status pill;
//   * ONE persistent content host (never recreated per repaint);
//   * digest-gated dispatch (DASHBOARD-V2 render discipline, mirrored from
//     js/v2/shell.js): a `state:changed` tick that only re-stamps a
//     heartbeat writes ZERO DOM. The shell coalesces ticks, then re-runs
//     the ACTIVE view's render — and each view digest-gates its own
//     repaint internally, so a steady heartbeat never flashes anything.
//
// Render discipline enforced here (the recurring flashing bugs MUST NOT
// reappear):
//   1. Breadcrumb + status pill each digest-gate their own writes.
//   2. On a VIEW switch (route.view changes) the content host is cleared
//      before the new view renders — so a digest-gated view never wrongly
//      skips its first paint while the previous view's DOM is still up.
//   3. Drill-down caches are invalidated ONLY on a view change or an
//      explicit user action — NEVER on every heartbeat.
//   4. Re-renders are debounced and routed through the active view's own
//      digest-gated render; the host is reused, never recreated.

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

const RENDERERS = { home, epoch, candidate, matchups, run };
const NAV_ITEMS = [
  ['home', 'Environment'],
  ['epoch', 'Epoch'],
  ['candidate', 'Candidate'],
  ['matchups', 'Match-ups'],
];

let _viewHost = null;        // the ONE persistent content host
let _crumbHost = null;
let _navEl = [];
let _statusEl = null;
let _renderToken = 0;        // guards out-of-order async renders
let _lastViewKey = null;     // 'view|params' of the last dispatched route
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
const _ctx = { navigate, href };

export function mountShell(root) {
  clearChildren(root);
  root.setAttribute('data-variant', 'E');

  // ---- top bar -------------------------------------------------------
  _crumbHost = el('nav', { class: 'e-crumbs', 'aria-label': 'Breadcrumb' });
  _navEl = NAV_ITEMS.map(([view, label]) =>
    el('a', { class: 'e-nav-link', href: href(view, {}), 'data-view': view, text: label }));
  _statusEl = el('span', { class: 'e-status' }, [
    el('span', { class: 'e-status-dot' }),
    el('span', { class: 'e-status-text', text: 'connecting…' }),
  ]);
  const topbar = el('header', { class: 'e-topbar' }, [
    el('div', { class: 'e-brand' }, [
      el('span', { class: 'e-brand-name', text: 'zicato' }),
      el('span', { class: 'e-brand-variant', text: 'atlas' }),
    ]),
    _crumbHost,
    el('span', { class: 'e-topbar-spacer' }),
    el('nav', { class: 'e-nav', 'aria-label': 'Primary' }, _navEl),
    _statusEl,
  ]);
  root.appendChild(topbar);

  // ---- the single persistent content host ----------------------------
  _viewHost = el('main', { class: 'e-viewhost', role: 'main' });
  root.appendChild(_viewHost);

  // Routing + live data.
  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/E')) location.hash = '#/E/';
  else dispatch();
}

function setActiveNav(view) {
  for (const a of _navEl) {
    // The 'run' view sits under 'candidate'; light 'candidate' for both.
    const target = view === 'run' ? 'candidate' : view;
    patchClass(a, 'e-active', a.getAttribute('data-view') === target);
  }
}

// Breadcrumb — digest-gated: only rebuilt when the trail actually changes.
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

// Status pill — digest-gated: a heartbeat that only changes the timestamp
// leaves the pill's DOM untouched.
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

  // On a VIEW switch (the view component changed, not just params), clear
  // the host first so the incoming digest-gated view always paints, AND
  // invalidate the live drill-down cache (a user navigation is an explicit
  // action — the right time to bust it, never on a heartbeat).
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
    console.error('variant-E render error', err);
  }
}

let _reRenderTimer = null;
function onStateChanged() {
  renderStatus();
  // Debounced re-render of the ACTIVE view only, routed through its own
  // digest-gated render. We DO NOT invalidate caches here — a heartbeat is
  // not a user action; the view's digest gate makes an unchanged tick a
  // no-op. Coalesces a burst of ticks into one repaint.
  if (_reRenderTimer != null) return;
  _reRenderTimer = setTimeout(() => {
    _reRenderTimer = null;
    dispatch();
  }, 400);
}
