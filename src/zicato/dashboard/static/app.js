// zicato dashboard — entry point.
//
// app.js is the thin orchestrator. It imports the modular core spine
// (state, bus, router, api, sse, dom, format, harmonograf), the shared
// component library, and the render layer, then wires them together:
//
//   * the SSE / api layer mutates AppState;
//   * AppState emits `state:changed` on the bus;
//   * the router emits `route:changed` on the bus;
//   * this entry point subscribes to both and calls the render layer.
//
// The render layer is a pure consumer — given (state, route) it paints
// keyed DOM nodes. A delta patches only the affected node; the activity
// log appends keyed rows. Nothing here re-implements rendering or data
// fetching: the modular boundary (core spine | components | render |
// entry) is the architecture. See js/CONTRACTS.md.

import { bus } from './js/core/bus.js';
import { state } from './js/core/state.js';
import { router } from './js/core/router.js';
import { loadEnvironment, loadServiceIdentity } from './js/core/api.js';
import { connectSSE } from './js/core/sse.js';
import {
  renderAll, renderHeader, renderFooter, applyRoute,
  setupLineageInteractions, closeDrill, appendLogTail,
} from './js/views/render.js';
import { mockSnapshot } from './js/views/mock.js';

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
    renderAll();
  });
}

function init() {
  // The drill panel close affordance.
  const drillClose = document.getElementById('drill-close');
  if (drillClose) drillClose.addEventListener('click', closeDrill);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') closeDrill();
  });

  // The bus is the spine: a state mutation or a route change drives a
  // render. The render layer reads the router's current route itself.
  bus.on('state:changed', scheduleRender);
  bus.on('route:changed', () => applyRoute());
  // A `log:appended` emission (the run-log ?after= poll merged new
  // events) drives a strictly append-only log-tail render — the panel
  // grows by keyed rows and never flashes.
  bus.on('log:appended', () => appendLogTail());

  // Tree-view pan + zoom wiring (the SVG itself is repainted on render).
  setupLineageInteractions();

  // Tick the header's elapsed clock once per second so it reads "live"
  // even when no state changes arrive. The render layer's renderHeader
  // is cheap and idempotent.
  setInterval(() => { renderHeader(); }, 1000);

  // Resolve the initial route before the first paint.
  router.start();

  const params = new URLSearchParams(window.location.search);
  if (params.get('mock') === '1') {
    state.mock = true;
    state.connected = false;
    state.connecting = false;
    state.applySnapshot(mockSnapshot());
    renderAll();
    return;
  }

  renderAll();
  loadEnvironment();
  loadServiceIdentity();
  connectSSE();
}

// Expose for tests + console debugging without polluting global scope.
window.__zicato = { state, router, bus, renderAll };

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
