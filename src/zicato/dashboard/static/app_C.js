// app_C.js — entry point for Variant C ("Causal Flow / diagram-first").
//
// One of four parallel dashboard explorations. The orchestrator wires
// `?ui=C` and provides `#variant-root`; this module paints the entire
// variant into that host. It REUSES the shared data layer verbatim —
// core/{state,api,sse,format,dom}.js — so live data + SSE + the
// no-flash render spine all come for free. Nothing in js/core, js/v2, or
// the shipped shell is touched.
//
// Architecture:
//   * chrome.js  — the persistent shell (nav, pill, drawer), built ONCE.
//   * router.js  — `#/C/...` hash routes.
//   * views/*    — one renderer per screen, each fully re-render-safe.
//
// A `state:changed` (SSE delta) or a `hashchange` schedules a single
// debounced render. The chrome is patched in place; only the active
// screen's stage is rebuilt (each view clears + repaints its own stage,
// which is cheap and self-contained, while the diagram surfaces preserve
// their own pan/zoom within a render).

import { bus } from './js/core/bus.js';
import { state } from './js/core/state.js';
import { loadEnvironment, loadServiceIdentity, loadMatchupDetail } from './js/core/api.js';
import { connectSSE } from './js/core/sse.js';

import { parseRoute, href } from './js/variants/C/router.js';
import { buildChrome, updateNavContext, closeDrawer } from './js/variants/C/chrome.js';
import { renderEnvironment } from './js/variants/C/views/environment.js';
import { renderEpoch } from './js/variants/C/views/epoch.js';
import { renderExperiment } from './js/variants/C/views/experiment.js';
import { renderTournament } from './js/variants/C/views/tournament.js';
import { renderRun } from './js/variants/C/views/run.js';
import { renderBench } from './js/variants/C/views/bench.js';
import { currentEpochId, liveGenId } from './js/variants/C/model.js';

let _chrome = null;
let _renderQueued = false;

// The variant stylesheet. The orchestrator may inject it; if not, we
// self-inject so a bare `?ui=C` load still paints correctly. Idempotent.
function ensureStylesheet() {
  if (typeof document.querySelector !== 'function') return; // harness
  const HREF = 'css/variants/C/variant.css';
  const already = document.querySelector('link[data-variant-c-css]');
  if (already) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = HREF;
  link.setAttribute('data-variant-c-css', '1');
  (document.head || document.documentElement || document.body).appendChild(link);
}

function mountHost() {
  let host = document.getElementById('variant-root');
  if (!host) {
    // Standalone fallback (direct load without the orchestrator wiring):
    // create the host so the variant is still reachable.
    host = document.createElement('div');
    host.id = 'variant-root';
    document.body.appendChild(host);
  }
  return host;
}

function ensureChrome(host) {
  if (_chrome) return _chrome;
  _chrome = buildChrome();
  // Clear the host once, then attach the chrome.
  while (host.firstChild) host.removeChild(host.firstChild);
  host.classList.add('cz-root');
  host.appendChild(_chrome.root);
  return _chrome;
}

function scheduleRender() {
  if (_renderQueued) return;
  _renderQueued = true;
  queueMicrotask(() => { _renderQueued = false; render(); });
}

function render() {
  const host = mountHost();
  const chrome = ensureChrome(host);
  const route = parseRoute(window.location.hash);

  chrome.setActive(route.view);
  chrome.setPill(state);

  // Resolve nav context (current epoch / gen / run) so top-nav clicks
  // land on real content.
  const epochId = route.params.epochId || currentEpochId(state);
  const genId = route.params.genId || liveGenId(state);
  const runId = route.params.runId
    || (Array.isArray(state.activeRuns) && state.activeRuns[0]
      ? (state.activeRuns[0].run_id || state.activeRuns[0].id) : null);
  updateNavContext(chrome, { epochId, genId, runId });

  const ctx = {
    stage: chrome.stage,
    state,
    chrome,
    params: route.params,
    repaint: scheduleRender,
    onNavigate: (v, p) => { window.location.hash = href(v, p); },
  };

  switch (route.view) {
    case 'env': renderEnvironment(ctx); break;
    case 'epoch': renderEpoch(ctx); break;
    case 'experiment': renderExperiment(ctx); break;
    case 'tournament': renderTournament(ctx); break;
    case 'run': renderRun(ctx); break;
    case 'bench': renderBench(ctx); break;
    default: renderEnvironment(ctx); break;
  }
}

function init() {
  ensureStylesheet();
  bus.on('state:changed', scheduleRender);
  bus.on('log:appended', scheduleRender);

  // Keep the pill clock honest even when no state arrives.
  setInterval(() => { if (_chrome) _chrome.setPill(state); }, 1000);

  window.addEventListener('hashchange', () => {
    if (_chrome) closeDrawer(_chrome.drawer);
    scheduleRender();
  });

  // First paint before data lands — honest "loading" / empty states.
  render();

  // Live data + SSE (reused verbatim from the shared layer).
  loadEnvironment();
  loadServiceIdentity();
  connectSSE();

  // When a matchup detail is requested by a screen it flows through the
  // shared cache; expose the loader for completeness.
  void loadMatchupDetail;
}

// Console / test debugging hook, namespaced so it cannot collide with
// the shipped shell's window.__zicato.
window.__zicatoC = { state, bus, render: scheduleRender, parseRoute };

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
