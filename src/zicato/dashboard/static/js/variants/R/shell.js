// variants/R/shell.js — the Strata shell + Miller-columns dispatch.
//
// Variant R ("Strata") navigates the SAME data model as N, but as cascading
// MILLER COLUMNS. The shell owns:
//   * a compact top bar — branding · a path breadcrumb · a COLOUR-theme picker
//     (solarized-dark default) · a TYPEFACE picker (Display default) · a status
//     pill;
//   * a persistent COLUMNS rail (col1 environment ▸ col2 sections ▸ col3 items),
//     each column independently scrollable + digest-gated;
//   * ONE persistent detail pane (never recreated per repaint), digest-gated;
//   * a `state:changed` tick that only re-stamps a heartbeat writes ZERO DOM.
//
// Theme + typeface are CSS-only swaps (data-r-theme / data-r-type on the root).

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parsePath, href, navigate, detailKind } from './router.js';
import * as D from './data.js';
import {
  COLOR_THEMES, DEFAULT_COLOR, normaliseColor, readColor, persistColor,
  TYPE_THEMES, DEFAULT_TYPE, normaliseType, readType, persistType,
} from './ui.js';
import { deriveModel, renderEpochColumn, renderSectionColumn, renderItemColumn } from './columns.js';

import * as environment from './views/environment.js';
import * as epoch from './views/epoch.js';
import * as candidate from './views/candidate.js';
import * as board from './views/board.js';
import * as mutations from './views/mutations.js';
import * as publication from './views/publication.js';

const DETAIL = { candidate, board, mutations, publication };

export const THEMES = COLOR_THEMES.map((t) => t[0]);
export const TYPEFACES = TYPE_THEMES.map((t) => t[0]);

let _root = null;
let _detailHost = null;
let _col1 = null, _col2 = null, _col3 = null;
let _crumbHost = null;
let _statusEl = null;
let _colorEl = [];
let _typeEl = [];
let _renderToken = 0;
let _lastDetailKey = null;
let _lastCrumbDigest = null;
let _lastStatusDigest = null;
const _ctx = { navigate, href };

export function applyTheme(theme, rootEl) {
  const t = normaliseColor(theme);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-r-theme', t);
  persistColor(t);
  for (const b of _colorEl) patchClass(b, 'dr-theme-active', b.getAttribute('data-theme') === t);
  return t;
}

export function applyTypeface(typeface, rootEl) {
  const t = normaliseType(typeface);
  const root = rootEl || _root;
  if (root) root.setAttribute('data-r-type', t);
  persistType(t);
  for (const b of _typeEl) patchClass(b, 'dr-type-active', b.getAttribute('data-type') === t);
  return t;
}

export function mountShell(root) {
  _root = root;
  clearChildren(root);
  root.setAttribute('data-variant', 'R');
  root.setAttribute('data-r-theme', readColor());
  root.setAttribute('data-r-type', readType());

  _crumbHost = el('nav', { class: 'dr-crumbs', 'aria-label': 'Breadcrumb' });

  _colorEl = COLOR_THEMES.map(([id, label]) => el('button', { class: 'dr-theme-btn', type: 'button', 'data-theme': id, title: 'colour: ' + id, text: label }));
  for (const b of _colorEl) b.addEventListener('click', () => applyTheme(b.getAttribute('data-theme')));
  const colorSwitch = el('div', { class: 'dr-theme-switch', role: 'group', 'aria-label': 'Colour theme' }, _colorEl);

  _typeEl = TYPE_THEMES.map(([id, label]) => el('button', { class: 'dr-type-btn', type: 'button', 'data-type': id, title: 'typeface: ' + id, text: label }));
  for (const b of _typeEl) b.addEventListener('click', () => applyTypeface(b.getAttribute('data-type')));
  const typeSwitch = el('div', { class: 'dr-type-switch', role: 'group', 'aria-label': 'Typeface' }, _typeEl);

  _statusEl = el('span', { class: 'dr-status' }, [
    el('span', { class: 'dr-status-dot' }),
    el('span', { class: 'dr-status-text', text: 'connecting…' }),
  ]);

  const topbar = el('header', { class: 'dr-topbar' }, [
    el('div', { class: 'dr-brand' }, [
      el('span', { class: 'dr-brand-name', text: 'zicato' }),
      el('span', { class: 'dr-brand-variant', text: 'strata' }),
    ]),
    _crumbHost,
    el('span', { class: 'dr-topbar-spacer' }),
    colorSwitch,
    typeSwitch,
    _statusEl,
  ]);
  root.appendChild(topbar);

  _col1 = el('div', { class: 'dr-col dr-col-1', 'aria-label': 'Environment' });
  _col2 = el('div', { class: 'dr-col dr-col-2', 'aria-label': 'Sections' });
  _col3 = el('div', { class: 'dr-col dr-col-3', 'aria-label': 'Items' });
  _detailHost = el('main', { class: 'dr-detail', role: 'main' });
  const body = el('div', { class: 'dr-body' }, [
    el('div', { class: 'dr-columns' }, [_col1, _col2, _col3]),
    _detailHost,
  ]);
  root.appendChild(body);

  applyTheme(readColor());
  applyTypeface(readType());

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/R')) location.hash = '#/R/';
  else dispatch();
}

function renderCrumbs(path) {
  if (!_crumbHost) return;
  const trail = [{ label: 'environment', path: {} }];
  if (path.epoch) trail.push({ label: path.epoch, path: { epoch: path.epoch } });
  if (path.section) trail.push({ label: path.section, path: { epoch: path.epoch, section: path.section } });
  if (path.section === 'generations' && path.gen) trail.push({ label: path.gen, path: { epoch: path.epoch, section: 'generations', gen: path.gen } });
  if (path.section === 'boards' && path.entry) trail.push({ label: path.entry, path: { epoch: path.epoch, section: 'boards', entry: path.entry } });
  if (path.section === 'mutations' && path.mutationId) trail.push({ label: path.mutationId, path: { epoch: path.epoch, section: 'mutations', mutationId: path.mutationId } });

  const digest = JSON.stringify(trail.map((c) => c.label));
  if (digest === _lastCrumbDigest && _crumbHost.firstChild) return;
  _lastCrumbDigest = digest;
  clearChildren(_crumbHost);
  trail.forEach((c, i) => {
    if (i > 0) _crumbHost.appendChild(el('span', { class: 'dr-crumb-sep', 'aria-hidden': 'true', text: '›' }));
    if (i === trail.length - 1) {
      _crumbHost.appendChild(el('span', { class: 'dr-crumb dr-crumb-current', 'aria-current': 'page', text: c.label }));
    } else {
      _crumbHost.appendChild(el('a', { class: 'dr-crumb', href: href(c.path), text: c.label }));
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
  patchText(_statusEl.querySelector('.dr-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'dr-connected', state.connected);
  patchClass(_statusEl, 'dr-running', live);
}

async function dispatch() {
  const path = parsePath(location.hash);
  renderCrumbs(path);
  renderStatus();

  // Load the structural payloads the columns need (cached + digest-gated).
  const [ws, ep, lin] = await Promise.all([D.workspace(), D.epoch(), D.lineage()]);
  const model = deriveModel(path, ep, lin, ws);

  renderEpochColumn(_col1, _ctx, model);
  renderSectionColumn(_col2, _ctx, model);
  renderItemColumn(_col3, _ctx, model);

  // The detail pane: dispatch by the detail kind, else the section/epoch/env
  // default. Clear the pane on a kind change so caches/listeners don't leak.
  const kind = detailKind(path);
  const detailKey = (kind || 'default') + '|' + path.epoch + '|' + path.section + '|' + (path.gen || path.entry || path.mutationId || '');
  const kindChanged = _lastDetailKey == null ? true : String(_lastDetailKey).split('|')[0] !== (kind || 'default');
  if (kindChanged) { clearChildren(_detailHost); D.invalidateLive(); }
  _lastDetailKey = detailKey;

  let renderer = null;
  if (kind && DETAIL[kind]) renderer = DETAIL[kind];
  else if (path.epoch) renderer = epoch;          // epoch selected, no item → overview
  else renderer = environment;                    // nothing selected → fleet

  const token = ++_renderToken;
  try {
    await renderer.render(_detailHost, _ctx, path);
  } catch (err) {
    if (token !== _renderToken) return;
    clearChildren(_detailHost);
    _detailHost.appendChild(el('p', { class: 'dr-empty', text: 'This pane hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-R render error', err);
  }
}

let _reRenderTimer = null;
function onStateChanged() {
  renderStatus();
  if (_reRenderTimer != null) return;
  _reRenderTimer = setTimeout(() => { _reRenderTimer = null; dispatch(); }, 400);
}

export { DEFAULT_COLOR, DEFAULT_TYPE };
