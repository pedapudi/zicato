// variants/L/shell.js — the Atlas III shell + digest-gated view dispatch.
//
// Dashboard-first (E's flow), refined and balanced. The shell owns:
//   * a calm top bar — branding · hierarchical breadcrumb · primary nav ·
//     a COLOUR theme picker AND a TYPEFACE theme picker · a status pill;
//   * ONE persistent content host (never recreated per repaint);
//   * digest-gated dispatch (DASHBOARD-V2 render discipline): a
//     `state:changed` heartbeat tick that does not change structural data
//     writes ZERO DOM. The shell coalesces ticks, then re-runs the ACTIVE
//     view's render — each view digest-gates its own repaint internally, so
//     a steady heartbeat never flashes anything.
//
// Render discipline enforced here:
//   1. Breadcrumb + status pill each digest-gate their own writes.
//   2. On a VIEW switch the content host is cleared before the new view
//      renders, AND the live drill-down cache is invalidated (a user
//      navigation is an explicit action — never a heartbeat).
//   3. Re-renders are debounced + routed through the active view's own
//      digest-gated render; the host is reused, never recreated.
//   4. Both pickers re-skin via root data-attributes — CSS only, no view
//      rebuild — so switching theme or typeface never flashes the screen.

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail } from './router.js';
import { invalidateLive } from './data.js';
import {
  readColor, applyColor, colorSwitcher,
  readType, applyType, typeSwitcher,
} from './ui.js';

import * as home from './views/home.js';
import * as epoch from './views/epoch.js';
import * as candidate from './views/candidate.js';
import * as board from './views/board.js';
import * as matchups from './views/matchups.js';
import * as mutations from './views/mutations.js';
import * as publication from './views/publication.js';
import * as run from './views/run.js';

const RENDERERS = { home, epoch, candidate, board, matchups, mutations, publication, run };
const NAV_ITEMS = [
  ['home', 'Environment'],
  ['epoch', 'Epoch'],
  ['candidate', 'Candidate'],
  ['matchups', 'Match-ups'],
  ['mutations', 'Mutations'],
  ['publication', 'Publication'],
];

let _root = null;
let _viewHost = null;
let _crumbHost = null;
let _navEl = [];
let _statusEl = null;
let _colorHost = null;
let _typeHost = null;
let _color = null;
let _type = null;
let _renderToken = 0;
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
const _ctx = { navigate, href };

export function mountShell(root) {
  clearChildren(root);
  _root = root;
  root.setAttribute('data-variant', 'L');
  _color = applyColor(root, readColor());
  _type = applyType(root, readType());

  _crumbHost = el('nav', { class: 'vl-crumbs', 'aria-label': 'Breadcrumb' });
  _navEl = NAV_ITEMS.map(([view, label]) =>
    el('a', { class: 'vl-nav-link', href: href(view, {}), 'data-view': view, text: label }));
  _statusEl = el('span', { class: 'vl-status' }, [
    el('span', { class: 'vl-status-dot' }),
    el('span', { class: 'vl-status-text', text: 'connecting…' }),
  ]);
  _colorHost = el('span', { class: 'vl-picker-host' });
  _typeHost = el('span', { class: 'vl-picker-host' });
  renderColorSwitcher();
  renderTypeSwitcher();

  const topbar = el('header', { class: 'vl-topbar' }, [
    el('div', { class: 'vl-brand' }, [
      el('span', { class: 'vl-brand-name', text: 'zicato' }),
      el('span', { class: 'vl-brand-variant', text: 'atlas iii' }),
    ]),
    _crumbHost,
    el('span', { class: 'vl-topbar-spacer' }),
    el('nav', { class: 'vl-nav', 'aria-label': 'Primary' }, _navEl),
    el('span', { class: 'vl-pickers' }, [_typeHost, _colorHost]),
    _statusEl,
  ]);
  root.appendChild(topbar);

  _viewHost = el('main', { class: 'vl-viewhost', role: 'main' });
  root.appendChild(_viewHost);

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/L')) location.hash = '#/L/';
  else dispatch();
}

function renderColorSwitcher() {
  if (!_colorHost) return;
  clearChildren(_colorHost);
  _colorHost.appendChild(colorSwitcher(_color, (next) => {
    _color = applyColor(_root, next);
    renderColorSwitcher(); // re-stamp the active button (CSS-only; no view rebuild)
  }));
}
function renderTypeSwitcher() {
  if (!_typeHost) return;
  clearChildren(_typeHost);
  _typeHost.appendChild(typeSwitcher(_type, (next) => {
    _type = applyType(_root, next);
    renderTypeSwitcher();
  }));
}

function setActiveNav(view) {
  for (const a of _navEl) {
    // 'run' + 'board' sit under 'candidate'/'epoch'; light the closest nav.
    const target = view === 'run' ? 'candidate' : (view === 'board' ? 'epoch' : view);
    patchClass(a, 'vl-active', a.getAttribute('data-view') === target);
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
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'vl-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'vl-crumb vl-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'vl-crumb', href: href(c.view, c.params), text: c.label }));
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
  patchText(_statusEl.querySelector('.vl-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'vl-connected', state.connected);
  patchClass(_statusEl, 'vl-running', live);
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
    _viewHost.appendChild(el('p', { class: 'vl-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-L render error', err);
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
