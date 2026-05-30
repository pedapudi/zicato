// app2.js — the v2 dashboard entry point (DASHBOARD-V2 §6).
//
// v2 ships behind a feature flag so v1 stays the default until v2 is
// proven. index.html's bootstrap chooses ONE entry: it loads this module
// only when the v2 flag is set (`?ui=v2` or the persisted localStorage
// toggle); otherwise it loads v1's app.js. This entry never runs
// alongside v1.
//
// v2 REUSES the v1 data layer wholesale (§6): the same core/ modules
// (state, bus, router-less hash handling, api, sse) and every /api/*
// endpoint. The miss in v1 was presentation, not data — so the backbone
// here is the new shell + spine + router, fed by the existing state.

import { bus } from './js/core/bus.js';
import { state } from './js/core/state.js';
import { loadEnvironment, loadServiceIdentity } from './js/core/api.js';
import { connectSSE } from './js/core/sse.js';
import { mockSnapshot } from './js/views/mock.js';

import { v2Router } from './js/v2/router.js';
import { renderShell } from './js/v2/shell.js';

// v2 view modules — each self-registers with the shell via
// registerView() at import time (side-effect imports, no bindings used).
import './js/v2/views/overview.js';
import './js/v2/views/tournament.js';
import './js/v2/views/bench.js';
import './js/v2/views/epoch.js';
import './js/v2/views/report.js';
import './js/v2/views/experiment.js';
import './js/v2/views/run.js';

// A single render is debounced across a burst of `state:changed`
// emissions in one tick. The shell is itself digest-gated + idempotent,
// so this is purely an efficiency floor.
let _renderQueued = false;
function scheduleRender() {
  if (_renderQueued) return;
  _renderQueued = true;
  queueMicrotask(() => {
    _renderQueued = false;
    renderShell(v2Router.current());
  });
}

export function initV2() {
  bus.on('state:changed', scheduleRender);
  bus.on('v2:route', () => scheduleRender());
  bus.on('log:appended', () => scheduleRender());

  // Mock mode must resolve BEFORE the router starts: a route emit can
  // kick off a route-driven load, and with state.mock still false that
  // load would hit the real (empty) endpoint and cache a degraded
  // payload over the mock. (Mirrors app.js's ordering discipline.)
  const params = new URLSearchParams(window.location.search);
  const mock = params.get('mock') === '1';
  if (mock) {
    state.mock = true;
    state.connected = false;
    state.connecting = false;
    state.applySnapshot(mockSnapshot());
  }

  v2Router.start();

  if (mock) {
    renderShell(v2Router.current());
    return;
  }

  renderShell(v2Router.current());
  loadEnvironment();
  loadServiceIdentity();
  connectSSE();
}

// Expose for tests + console debugging without polluting global scope.
if (typeof window !== 'undefined') {
  window.__zicato_v2 = { state, router: v2Router, bus };
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initV2);
  } else {
    initV2();
  }
}
