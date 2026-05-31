// variants/D/app.js — Variant D orchestrator.
//
// Wires the shared data layer (core/api + core/sse + core/state) to the
// Tufte views behind a hash router scoped to `#/D/...`. The orchestrator
// owns:
//   * the persistent nav bar (fresh host on every view switch — each
//     view clears + repaints its own container, so a view switch is a
//     clean unmount, never a leak);
//   * a connection-aware status pill;
//   * SSE re-render safety — on a `state:changed` tick we bust the live
//     drill-down cache and re-run the CURRENT view's render, so the
//     screen stays honest without flashing other views.
//
// Each view exports `render(host, ctx, params)`; ctx = { navigate }.

import { el, clearChildren } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, VIEWS, href } from './router.js';
import { invalidateLive } from './data.js';

import * as environment from './views/environment.js';
import * as epoch from './views/epoch.js';
import * as lifecycle from './views/lifecycle.js';
import * as experiment from './views/experiment.js';
import * as tournament from './views/tournament.js';
import * as run from './views/run.js';
import * as bench from './views/bench.js';

const RENDERERS = { environment, epoch, lifecycle, experiment, tournament, run, bench };
const NAV_ITEMS = [
  ['environment', 'Environment'],
  ['epoch', 'Epoch'],
  ['lifecycle', 'Lifecycle'],
  ['bench', 'Boards'],
  ['tournament', 'Match-ups'],
  ['run', 'Scoring'],
];

let _viewHost = null;
let _navEl = null;
let _statusEl = null;
let _current = null;       // { view }
let _renderToken = 0;      // guards against out-of-order async renders

export function mount(root) {
  clearChildren(root);
  const shell = el('div', { class: 'd-shell' });

  _navEl = buildNav();
  _statusEl = el('span', { class: 'd-nav-status' }, [
    el('span', { class: 'd-dot-live' }), el('span', { class: 'd-status-text', text: 'connecting…' }),
  ]);
  const navBar = el('nav', { class: 'd-nav' }, [
    el('span', { class: 'd-nav-brand' }, ['zicato', el('span', { class: 'd-nav-variant', text: 'variant D · tufte' })]),
    ..._navEl,
    el('span', { class: 'd-nav-spacer' }),
    _statusEl,
  ]);
  root.appendChild(navBar);
  _viewHost = el('main', { class: 'd-viewhost' });
  shell.appendChild(_viewHost);
  root.appendChild(shell);

  // Routing.
  window.addEventListener('hashchange', dispatch);
  // State changes → update status + re-render the live view.
  bus.on('state:changed', onStateChanged);

  // Boot the shared data layer (idempotent if the shell already did it).
  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/D')) location.hash = '#/D/';
  else dispatch();
}

function buildNav() {
  return NAV_ITEMS.map(([view, label]) =>
    el('a', { href: href(view), 'data-view': view, text: label }));
}

function setActiveNav(view) {
  for (const a of _navEl) {
    if (a.getAttribute('data-view') === view) a.classList.add('d-active');
    else a.classList.remove('d-active');
  }
}

const ctx = { navigate };

async function dispatch() {
  const route = parseRoute(location.hash);
  const renderer = RENDERERS[route.view];
  if (!renderer) return;
  _current = route;
  setActiveNav(route.view);
  // Fresh host on view switch — a clean unmount of the previous view.
  const host = el('section', { class: `d-view d-view-${route.view}` });
  clearChildren(_viewHost);
  _viewHost.appendChild(host);
  const token = ++_renderToken;
  try {
    await renderer.render(host, ctx, route.params);
  } catch (err) {
    if (token !== _renderToken) return;
    clearChildren(host);
    host.appendChild(el('p', { class: 'd-empty', text: 'This view hit an error: ' + (err && err.message || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-D render error', err);
  }
}

let _reRenderTimer = null;
function onStateChanged() {
  // Status pill.
  if (_statusEl) {
    const t = _statusEl.querySelector('.d-status-text');
    if (state.connected) { _statusEl.classList.add('d-connected'); if (t) t.textContent = 'live'; }
    else if (state.connecting) { _statusEl.classList.remove('d-connected'); if (t) t.textContent = 'connecting…'; }
    else { _statusEl.classList.remove('d-connected'); if (t) t.textContent = 'offline'; }
  }
  // Debounced re-render of the CURRENT view only — bust the live cache
  // first so the re-render reads fresh drill-down data. Coalesces a
  // burst of state_change ticks into one repaint.
  if (_reRenderTimer != null) return;
  _reRenderTimer = setTimeout(() => {
    _reRenderTimer = null;
    invalidateLive();
    dispatch();
  }, 450);
}
