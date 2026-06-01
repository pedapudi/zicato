// variants/O/shell.js — the Compass shell: master-detail two-pane workspace.
//
// A persistent LEFT SELECTOR RAIL (epoch → generation → board entry) + a
// RIGHT DETAIL PANE that follows the EXPLICIT, PERSISTENT selection encoded
// in the URL. The two-pane layout is a clean CSS grid (rail fixed-width +
// detail flexible); the detail pane scrolls independently and the rail is
// its own constrained-scroll column.
//
// Render discipline (mirrors the v2 / E digest-gate blueprint — the
// flashing bugs are designed out):
//   1. Each pane digest-gates its own repaint off STRUCTURAL data only
//      (selection + content; heartbeat timestamps excluded). A steady
//      heartbeat that re-dispatches the active selection writes ZERO DOM.
//   2. On a SELECTION CHANGE (the selectionKey changes) the detail pane's
//      host is cleared before the incoming view renders, and the live
//      drill-down cache is invalidated (a user navigation — the right time
//      to bust it, never on a heartbeat). The rail host is reused.
//   3. Re-renders are debounced and routed through the active selection's
//      own digest-gated render; both pane hosts are reused, never recreated.
//   4. Cold deep-link: the URL encodes the full selection, so both panes
//      hydrate from the URL on a cold load.
//   5. Hover via CSS `transition`, never `animation:…infinite`.

import { el, clearChildren, patchText, patchClass } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { loadEnvironment, loadServiceIdentity } from '../../core/api.js';
import { connectSSE } from '../../core/sse.js';
import { parseRoute, navigate, href, selectionKey } from './router.js';
import { invalidateLive } from './data.js';
import {
  readTheme, applyTheme, themeSwitcher,
  readTypeface, applyTypeface, typefaceSwitcher,
} from './ui.js';
import { loadRailModel, loadWorkspaceModel } from './model.js';
import { renderRail } from './rail.js';

import * as workspace from './views/workspace.js';
import * as epoch from './views/epoch.js';
import * as candidate from './views/candidate.js';
import * as board from './views/board.js';
import * as run from './views/run.js';

const VIEWS = { workspace, epoch, gen: candidate, board, run };

let _root = null;
let _railHost = null;
let _detailHost = null;
let _statusEl = null;
let _renderToken = 0;
let _lastSelKey = null;
let _lastStatusDigest = null;
const _ctx = { navigate, href };

export function mountShell(root) {
  clearChildren(root);
  _root = root;
  root.setAttribute('data-variant', 'O');
  applyTheme(root, readTheme());
  applyTypeface(root, readTypeface());

  // ---- top chrome ----------------------------------------------------
  _statusEl = el('span', { class: 'vo-status' }, [
    el('span', { class: 'vo-status-dot' }),
    el('span', { class: 'vo-status-text', text: 'connecting…' }),
  ]);
  const topbar = el('header', { class: 'vo-topbar' }, [
    el('a', { class: 'vo-brand', href: href('overview', {}) }, [
      el('span', { class: 'vo-brand-name', text: 'zicato' }),
      el('span', { class: 'vo-brand-variant', text: 'compass' }),
    ]),
    el('span', { class: 'vo-topbar-spacer' }),
    el('div', { class: 'vo-pickers' }, [
      el('div', { class: 'vo-picker-group' }, [
        el('span', { class: 'vo-picker-label', text: 'Type' }),
        typefaceSwitcher(readTypeface(), (face) => { applyTypeface(_root, face); }),
      ]),
      el('div', { class: 'vo-picker-group' }, [
        el('span', { class: 'vo-picker-label', text: 'Theme' }),
        themeSwitcher(readTheme(), (t) => { applyTheme(_root, t); }),
      ]),
    ]),
    _statusEl,
  ]);
  root.appendChild(topbar);

  // ---- the two-pane grid ---------------------------------------------
  _railHost = el('aside', { class: 'vo-rail', 'aria-label': 'Selector' });
  _detailHost = el('main', { class: 'vo-detail', role: 'main' });
  root.appendChild(el('div', { class: 'vo-workspace' }, [_railHost, _detailHost]));

  window.addEventListener('hashchange', dispatch);
  bus.on('state:changed', onStateChanged);

  loadServiceIdentity();
  loadEnvironment();
  connectSSE();

  if (!String(location.hash).startsWith('#/O')) location.hash = '#/O/';
  else dispatch();
}

function renderStatus() {
  if (!_statusEl) return;
  const conn = state.connected ? 'live' : state.connecting ? 'connecting…' : 'offline';
  const live = !!state.activeTournament;
  const digest = conn + '|' + (live ? 'L' : '');
  if (digest === _lastStatusDigest) return;
  _lastStatusDigest = digest;
  patchText(_statusEl.querySelector('.vo-status-text') || _statusEl, conn);
  patchClass(_statusEl, 'vo-connected', state.connected);
  patchClass(_statusEl, 'vo-running', live);
}

async function dispatch() {
  const route = parseRoute(location.hash);
  const renderer = VIEWS[route.view] || VIEWS.workspace;
  const selKey = selectionKey(route);

  renderStatus();

  // On a SELECTION change, clear the detail host first (so a digest-gated
  // view always paints its first frame) and bust the live cache.
  if (_lastSelKey !== selKey) {
    clearChildren(_detailHost);
    invalidateLive();
  }
  _lastSelKey = selKey;

  const token = ++_renderToken;
  try {
    // The rail is all-epochs-first: it lists every epoch, and EXPANDS the
    // one in scope (an explicitly-selected epoch, or — for a generation /
    // board / run selection — the live epoch) to its generations + board.
    const ws = await loadWorkspaceModel();
    const expandedEpochId = route.kind === 'epoch'
      ? route.epoch
      : (route.kind === 'gen' || route.kind === 'board' || route.kind === 'run')
        ? ws.liveEpochId
        : null;
    const railEpoch = expandedEpochId
      ? await loadRailModel(expandedEpochId)
      : { gens: [], board: [] };
    if (token !== _renderToken) return;
    renderRail(_railHost, _ctx, {
      epochs: ws.epochs, selectedEpochId: expandedEpochId,
      gens: railEpoch.gens, board: railEpoch.board, selection: route,
    });
    await renderer.render(_detailHost, _ctx, route);
  } catch (err) {
    if (token !== _renderToken) return;
    clearChildren(_detailHost);
    _detailHost.appendChild(el('p', { class: 'vo-empty', text: 'This view hit an error: ' + ((err && err.message) || err) }));
    // eslint-disable-next-line no-console
    console.error('variant-O render error', err);
  }
}

let _reRenderTimer = null;
function onStateChanged() {
  renderStatus();
  if (_reRenderTimer != null) return;
  _reRenderTimer = setTimeout(() => { _reRenderTimer = null; dispatch(); }, 400);
}
