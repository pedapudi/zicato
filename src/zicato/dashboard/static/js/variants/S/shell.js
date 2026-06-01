// variants/S/shell.js — the "Lens" shell: tree sidebar + comparison-first detail.
//
// Variant S replaces top-tab navigation with a persistent LEFT TREE (the data
// model) and a detail pane whose signature is FIRST-CLASS SIDE-BY-SIDE
// COMPARISON. The shell owns:
//   * a compact top bar — branding · breadcrumb · colour-theme picker
//     (solarized-light default) · typeface picker (Editorial default) · status;
//   * a persistent tree sidebar (never recreated; digest-gated per the model);
//   * ONE persistent detail host (never recreated per repaint);
//   * digest-gated dispatch — a `state:changed` tick that only re-stamps a
//     heartbeat writes ZERO DOM (tree + detail both gate).
//
// Theme + typeface are CSS-only swaps (data-s-theme / data-s-type on the root).

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail } from './router.js';
import { invalidateLive } from './data.js';
import { buildModel, paintTree } from './tree.js';
import {
  COLOR_THEMES, DEFAULT_COLOR, normaliseColor, readColor, persistColor,
  TYPE_THEMES, DEFAULT_TYPE, normaliseType, readType, persistType,
} from './ui.js';

import * as env from './views/env.js';
import * as epoch from './views/epoch.js';
import * as candidate from './views/candidate.js';
import * as board from './views/board.js';
import * as mutations from './views/mutations.js';
import * as publication from './views/publication.js';

const RENDERERS = { env, epoch, candidate, board, mutations, publication };

export const THEMES = COLOR_THEMES.map((t) => t[0]);
export const TYPEFACES = TYPE_THEMES.map((t) => t[0]);

let _root = null;
let _treeHost = null;
let _detailHost = null;
let _crumbHost = null;
let _statusEl = null;
let _colorEl = [];
let _typeEl = [];
let _renderToken = 0;
let _lastViewKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
const _ctx = { navigate, href };

export function applyTheme(theme, rootEl) {
  const t = normaliseColor(theme);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-s-theme', t);
  persistColor(t);
  for (const b of _colorEl) patchClass(b, 'vs-theme-active', b.getAttribute('data-theme') === t);
  return t;
}

export function applyTypeface(typeface, rootEl) {
  const t = normaliseType(typeface);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-s-type', t);
  persistType(t);
  for (const b of _typeEl) patchClass(b, 'vs-type-active', b.getAttribute('data-type') === t);
  return t;
}

export function mountShell(root) {
  _root = root;
  clearChildren(root);
  root.setAttribute('data-variant', 'S');
  root.setAttribute('data-s-theme', readColor());
  root.setAttribute('data-s-type', readType());

  _crumbHost = el('nav', { class: 'vs-crumbs', 'aria-label': 'Breadcrumb' });

  _colorEl = COLOR_THEMES.map(([id, label]) =>
    el('button', { class: 'vs-theme-btn', type: 'button', 'data-theme': id, title: 'colour: ' + id, text: label }));
  for (const b of _colorEl) b.addEventListener('click', () => applyTheme(b.getAttribute('data-theme')));
  const colorSwitch = el('div', { class: 'vs-theme-switch', role: 'group', 'aria-label': 'Colour theme' }, _colorEl);

  _typeEl = TYPE_THEMES.map(([id, label]) =>
    el('button', { class: 'vs-type-btn', type: 'button', 'data-type': id, title: 'typeface: ' + id, text: label }));
  for (const b of _typeEl) b.addEventListener('click', () => applyTypeface(b.getAttribute('data-type')));
  const typeSwitch = el('div', { class: 'vs-type-switch', role: 'group', 'aria-label': 'Typeface' }, _typeEl);

  _statusEl = el('span', { class: 'vs-status' }, [
    el('span', { class: 'vs-status-dot' }),
    el('span', { class: 'vs-status-text', text: 'connecting…' }),
  ]);

  const topbar = el('header', { class: 'vs-topbar' }, [
    el('div', { class: 'vs-brand' }, [
      el('span', { class: 'vs-brand-name', text: 'zicato' }),
      el('span', { class: 'vs-brand-variant', text: 'lens' }),
    ]),
    _crumbHost,
    el('span', { class: 'vs-topbar-spacer' }),
    colorSwitch,
    typeSwitch,
    _statusEl,
  ]);
  root.appendChild(topbar);

  const body = el('div', { class: 'vs-body' });
  _treeHost = el('aside', { class: 'vs-sidebar', 'aria-label': 'Data model tree' });
  _detailHost = el('main', { class: 'vs-detail', role: 'main' });
  body.appendChild(_treeHost);
  body.appendChild(_detailHost);
  root.appendChild(body);

  applyTheme(readColor());
  applyTypeface(readType());

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/S')) location.hash = '#/S/';
  else dispatch();
}

function renderCrumbs(route) {
  if (!_crumbHost) return;
  const trail = crumbTrail(route);
  const digest = JSON.stringify(trail.map((c) => [c.label, c.view || '', c.current || false]));
  if (digest === _lastCrumbDigest && _crumbHost.firstChild) return;
  _lastCrumbDigest = digest;
  clearChildren(_crumbHost);
  trail.forEach((c, i) => {
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'vs-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'vs-crumb vs-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'vs-crumb', href: href(c.view, c.params), text: c.label }));
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
  patchText(_statusEl.querySelector('.vs-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'vs-connected', state.connected);
  patchClass(_statusEl, 'vs-running', live);
}

async function dispatch() {
  const route = parseRoute(location.hash);
  const renderer = RENDERERS[route.view] || RENDERERS.env;
  const viewKey = route.view + '|' + JSON.stringify(route.params || {}) + '|cmp=' + (route.cmp || '') + '|runs=' + (route.runs ? route.runs.join(',') : '');

  renderCrumbs(route);
  renderStatus();

  // The tree: rebuild the model (cached reads) then digest-gated paint. The
  // tree persists across view changes — it is the navigation.
  try {
    const m = await buildModel();
    paintTree(_treeHost, m, route, navigate);
  } catch (err) {
    // a transient model failure leaves the last-painted tree in place.
  }

  // The detail pane is cleared only on a SELECTION change (the no-flash rule:
  // caches invalidate only on selection change).
  const prevKey = _lastViewKey;
  if (prevKey !== viewKey) {
    clearChildren(_detailHost);
    invalidateLive();
  }
  _lastViewKey = viewKey;

  const token = ++_renderToken;
  try {
    await renderer.render(_detailHost, _ctx, route);
  } catch (err) {
    if (token !== _renderToken) return;
    clearChildren(_detailHost);
    _detailHost.appendChild(el('p', { class: 'dn-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-S render error', err);
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
