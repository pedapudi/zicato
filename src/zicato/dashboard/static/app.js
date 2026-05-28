// zicato dashboard — entry point.
//
// app.js is the thin orchestrator for the level-aligned shell. The
// shell maps directly onto the environment → epoch → generation →
// round → run hierarchy: five view modules under ``js/views/phase0_*.js``,
// a fixed sidebar (Live Activity card + Files + Search), and a
// breadcrumb as the primary navigation.
//
// The bus is the spine: a state mutation or a route change drives a
// render.

import { bus } from './js/core/bus.js';
import { state } from './js/core/state.js';
import { router } from './js/core/router.js';
import { loadEnvironment, loadServiceIdentity } from './js/core/api.js';
import { connectSSE } from './js/core/sse.js';
import { mockSnapshot } from './js/views/mock.js';

import { parsePhase0Hash } from './js/views/phase0_router.js';
import {
  renderBreadcrumb, showPhase0View, renderSidebarLive,
  renderHeader, renderFooter,
} from './js/views/phase0_shell.js';
import { renderPhase0Workspace } from './js/views/phase0_workspace.js';
import { renderPhase0Epoch } from './js/views/phase0_epoch.js';
import { renderPhase0Generation } from './js/views/phase0_generation.js';
import { renderPhase0Round } from './js/views/phase0_round.js';
import { renderPhase0Run } from './js/views/phase0_run.js';

// A single render is debounced across a burst of `state:changed`
// emissions inside one tick so a multi-field state update repaints
// once. The render layer is itself idempotent, so this is purely an
// efficiency floor.
let _renderQueued = false;

function scheduleRender() {
  if (_renderQueued) return;
  _renderQueued = true;
  queueMicrotask(() => {
    _renderQueued = false;
    renderPhase0All();
  });
}

// Render the entire phase-0 shell. Header / footer / sidebar / breadcrumb
// + the active L0..L4 view. Idempotent: each per-level module is itself
// digest-aware where the underlying data justifies it.
function renderPhase0All() {
  renderHeader();
  renderFooter();
  renderSidebarLive();
  const route = parsePhase0Hash(window.location.hash);
  renderBreadcrumb(route);
  showPhase0View(route.level);
  switch (route.level) {
    case 'workspace':
      renderPhase0Workspace(scheduleRender);
      break;
    case 'epoch':
      renderPhase0Epoch(route.params, scheduleRender);
      break;
    case 'generation':
      renderPhase0Generation(route.params, scheduleRender);
      break;
    case 'round':
      renderPhase0Round(route.params, scheduleRender);
      break;
    case 'run':
      renderPhase0Run(route.params, scheduleRender);
      break;
    default:
      break;
  }
}

function init() {
  bus.on('state:changed', scheduleRender);
  bus.on('route:changed', () => { scheduleRender(); });
  bus.on('log:appended', () => { scheduleRender(); });

  // Tick the header's elapsed clock once per second so it reads "live"
  // even when no state changes arrive. The renderHeader function is
  // cheap and idempotent.
  setInterval(() => { renderHeader(); }, 1000);

  // Mock mode must be resolved BEFORE router.start(): a `route:changed`
  // bus emit may kick off a route-driven load. With `state.mock` still
  // false the load would hit the real (empty) endpoint and cache its
  // degraded payload over the mock.
  const params = new URLSearchParams(window.location.search);
  const mock = params.get('mock') === '1';
  if (mock) {
    state.mock = true;
    state.connected = false;
    state.connecting = false;
    state.applySnapshot(mockSnapshot());
  }

  // Resolve the initial route before the first paint. The phase-0 shell
  // installs its own hashchange listener so a fragment update repaints.
  router.start();
  window.addEventListener('hashchange', () => scheduleRender());

  if (mock) {
    renderPhase0All();
    return;
  }

  renderPhase0All();
  loadEnvironment();
  loadServiceIdentity();
  connectSSE();
}

// Expose for tests + console debugging without polluting global scope.
window.__zicato = { state, router, bus };

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
