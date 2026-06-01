// variants/U/shell.js — the "Atlas V" shell: tree sidebar + comparison-first
// detail, rendered ROOMY and LIGHT, with a FIXED back/up control.
//
// Variant U is the comfortable convergence-IV sibling of the anchor: the SAME
// P+S+Q synthesis (a persistent LEFT data-model TREE driving a detail pane
// whose signature is FIRST-CLASS SIDE-BY-SIDE COMPARISON), but with Q/M-forward
// generous spacing/proportion, Solarized-Light + Sans by default. The shell
// owns:
//   * a compact top bar — a FIXED back/up button (top-left) · branding ·
//     breadcrumb · colour-theme picker (sol-light default) · typeface picker
//     (Sans default) · status;
//   * a persistent tree sidebar (never recreated; digest-gated per the model);
//   * ONE persistent detail host (never recreated per repaint);
//   * digest-gated dispatch — a `state:changed` tick that only re-stamps a
//     heartbeat writes ZERO DOM (tree + detail both gate).
//
// The back/up control (Round-6 NEW #3 — Q's bug, FIXED): it computes the
// destination ONE step UP the selection hierarchy (router.parentRoute) and
// `navigate()`s there, which flows through the standard dispatch and renders
// the destination into the MAIN DETAIL host — NEVER the sidebar. The tree rail
// is only ever painted by paintTree() in dispatch; the back action does not
// touch it. (A test asserts the rail host still holds the tree after a back
// action and the detail host holds the destination view.)
//
// Theme + typeface are CSS-only swaps (data-u-theme / data-u-type on the root).

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, crumbTrail, parentRoute } from './router.js';
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
let _backBtn = null;
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
  if (root) root.setAttribute('data-u-theme', t);
  persistColor(t);
  for (const b of _colorEl) patchClass(b, 'vu-theme-active', b.getAttribute('data-theme') === t);
  return t;
}

export function applyTypeface(typeface, rootEl) {
  const t = normaliseType(typeface);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-u-type', t);
  persistType(t);
  for (const b of _typeEl) patchClass(b, 'vu-type-active', b.getAttribute('data-type') === t);
  return t;
}

// The FIXED back/up handler — render the destination into the MAIN detail pane.
// It computes the parent of the CURRENT route and navigates; the normal
// dispatch then paints the destination into _detailHost. It NEVER writes to
// _treeHost (that is Q's bug). Exported so a test can drive it directly.
export function goBack() {
  const route = parseRoute(location.hash);
  const up = parentRoute(route);
  if (!up) return false;
  navigate(up.view, up.params, up.opts);
  return true;
}

function renderBack(route) {
  if (!_backBtn) return;
  const up = parentRoute(route);
  const enabled = !!up;
  _backBtn.disabled = !enabled;
  patchClass(_backBtn, 'vu-back-disabled', !enabled);
  _backBtn.title = enabled ? 'Up to ' + (up.view === 'env' ? 'environment' : up.view) : 'At the top';
  _backBtn.setAttribute('aria-disabled', String(!enabled));
}

export function mountShell(root) {
  _root = root;
  clearChildren(root);
  root.setAttribute('data-variant', 'U');
  root.setAttribute('data-u-theme', readColor());
  root.setAttribute('data-u-type', readType());

  // FIXED back/up control — top-left, navigates UP into the MAIN detail pane.
  _backBtn = el('button', { class: 'vu-back', type: 'button', title: 'Up', 'aria-label': 'Up one level' }, [
    el('span', { class: 'vu-back-glyph', 'aria-hidden': 'true', text: '‹' }),
    el('span', { class: 'vu-back-text', text: 'up' }),
  ]);
  _backBtn.addEventListener('click', (ev) => { ev.preventDefault(); goBack(); });

  _crumbHost = el('nav', { class: 'vu-crumbs', 'aria-label': 'Breadcrumb' });

  _colorEl = COLOR_THEMES.map(([id, label]) =>
    el('button', { class: 'vu-theme-btn', type: 'button', 'data-theme': id, title: 'colour: ' + id, text: label }));
  for (const b of _colorEl) b.addEventListener('click', () => applyTheme(b.getAttribute('data-theme')));
  const colorSwitch = el('div', { class: 'vu-theme-switch', role: 'group', 'aria-label': 'Colour theme' }, _colorEl);

  _typeEl = TYPE_THEMES.map(([id, label]) =>
    el('button', { class: 'vu-type-btn', type: 'button', 'data-type': id, title: 'typeface: ' + id, text: label }));
  for (const b of _typeEl) b.addEventListener('click', () => applyTypeface(b.getAttribute('data-type')));
  const typeSwitch = el('div', { class: 'vu-type-switch', role: 'group', 'aria-label': 'Typeface' }, _typeEl);

  _statusEl = el('span', { class: 'vu-status' }, [
    el('span', { class: 'vu-status-dot' }),
    el('span', { class: 'vu-status-text', text: 'connecting…' }),
  ]);

  const topbar = el('header', { class: 'vu-topbar' }, [
    _backBtn,
    el('div', { class: 'vu-brand' }, [
      el('span', { class: 'vu-brand-name', text: 'zicato' }),
      el('span', { class: 'vu-brand-variant', text: 'atlas v' }),
    ]),
    _crumbHost,
    el('span', { class: 'vu-topbar-spacer' }),
    colorSwitch,
    typeSwitch,
    _statusEl,
  ]);
  root.appendChild(topbar);

  const body = el('div', { class: 'vu-body' });
  _treeHost = el('aside', { class: 'vu-sidebar', 'aria-label': 'Data model tree' });
  _detailHost = el('main', { class: 'vu-detail', role: 'main' });
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

  if (!String(location.hash).startsWith('#/U')) location.hash = '#/U/';
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
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'vu-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (c.current || !c.view) {
      _crumbHost.appendChild(el('span', { class: 'vu-crumb vu-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'vu-crumb', href: href(c.view, c.params), text: c.label }));
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
  patchText(_statusEl.querySelector('.vu-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'vu-connected', state.connected);
  patchClass(_statusEl, 'vu-running', live);
}

async function dispatch() {
  const route = parseRoute(location.hash);
  const renderer = RENDERERS[route.view] || RENDERERS.env;
  const viewKey = route.view + '|' + JSON.stringify(route.params || {}) + '|cmp=' + (route.cmp || '') + '|runs=' + (route.runs ? route.runs.join(',') : '');

  renderCrumbs(route);
  renderStatus();
  renderBack(route);

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
    console.error('variant-U render error', err);
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
