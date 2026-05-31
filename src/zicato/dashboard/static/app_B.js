// app_B.js — entry point for Variant B ("Editorial Lab Notebook").
//
// One of four parallel dashboard explorations. The orchestrator wires
// `?ui=B` and reveals `#variant-root`; this entry paints the whole Variant
// B shell into that container and never runs alongside another variant.
//
// Variant B REUSES the shared data layer wholesale: the same core/ modules
// (state, bus, api, sse) and every /api/* endpoint. Only the presentation
// is new — a typography-led, whitespace-rich research-magazine reading of
// the same environment. The router prefix is `#/B/...` so it never collides
// with v1 (`#/...`) or v2 (`#/v2/...`).

import { bus } from './js/core/bus.js';
import { state } from './js/core/state.js';
import { loadEnvironment, loadServiceIdentity } from './js/core/api.js';
import { connectSSE } from './js/core/sse.js';
import { mockSnapshot } from './js/views/mock.js';

import { bRouter } from './js/variants/B/router.js';
import { renderBShell, applyTheme, readTheme } from './js/variants/B/shell.js';

// View modules self-register with the shell on import.
import './js/variants/B/views/environment.js';
import './js/variants/B/views/epoch.js';
import './js/variants/B/views/experiment.js';
import './js/variants/B/views/board.js';
import './js/variants/B/views/tournament.js';
import './js/variants/B/views/run.js';
import './js/variants/B/views/bench.js';

// Variant B owns its own stylesheets. Because index.html is shared across
// the four explorations (and must not be edited per-variant), the entry
// injects its <link>s once at boot — scoped to `.vb-shell` / `[data-vb-*]`
// so they never touch another variant's surface.
function ensureStyles() {
  if (typeof document === 'undefined') return;
  const head = document.head || document.getElementsByTagName('head')[0];
  if (!head) return;
  for (const href of ['css/variants/B/tokens.css', 'css/variants/B/notebook.css']) {
    const id = 'vb-css-' + href.split('/').pop().replace('.css', '');
    if (document.getElementById(id)) continue;
    const link = document.createElement('link');
    link.id = id;
    link.rel = 'stylesheet';
    link.href = href;
    head.appendChild(link);
  }
}

let _renderQueued = false;
function scheduleRender() {
  if (_renderQueued) return;
  _renderQueued = true;
  queueMicrotask(() => {
    _renderQueued = false;
    renderBShell(bRouter.current());
  });
}

export function initB() {
  ensureStyles();
  applyTheme(readTheme());

  bus.on('state:changed', scheduleRender);
  bus.on('B:route', () => scheduleRender());
  bus.on('log:appended', () => scheduleRender());

  // Resolve mock BEFORE the router starts (a route emit can kick a
  // route-driven load; with state.mock false that would hit the empty
  // real endpoint and cache a degraded payload over the mock).
  const params = new URLSearchParams(window.location.search);
  const mock = params.get('mock') === '1';
  if (mock) {
    state.mock = true;
    state.connected = false;
    state.connecting = false;
    state.applySnapshot(mockSnapshot());
  }

  bRouter.start();

  if (mock) {
    renderBShell(bRouter.current());
    return;
  }

  renderBShell(bRouter.current());
  loadEnvironment();
  loadServiceIdentity();
  connectSSE();
}

if (typeof window !== 'undefined') {
  window.__zicato_B = { state, router: bRouter, bus };
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initB);
  } else {
    initB();
  }
}
