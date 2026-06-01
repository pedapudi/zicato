// app_F.js — entry point for Variant F ("Current — causal narrative").
//
// A synthesis variant: it leads with the causal flow + lifecycle DAG (the
// backbone), embeds compact data-viz evidence inline, frames everything in
// an airy editorial voice, and navigates through a hierarchical breadcrumb
// IA. It REUSES the shared data layer verbatim — core/{state,api,sse,format,
// dom,bus}.js — so live data + SSE + the no-flash render spine come for
// free. It is SELF-CONTAINED within js/variants/F/ (it imports no other
// variant's modules) and touches nothing in js/core, js/v2, or the shell.
//
// Render discipline (the recurring flashing/refresh bugs MUST NOT appear):
//   1. Every screen is digest-gated: an identical-data / heartbeat-only
//      re-render returns early without rebuilding DOM (see view modules).
//   2. On a VIEW switch we clear the persistent stage host before painting.
//   3. `state:changed` is debounced into a single queued render; ONE
//      persistent content host (the chrome's stage) is reused, never
//      recreated.
//   4. Drill-down caches invalidate only on route/view change, never on a
//      heartbeat (see resetCaches()).
//   5. Hover/flow effects are CSS transitions; the single marching-ants
//      edge animation is digest-gated so it cannot re-fire each heartbeat.
//   6. Deep links hydrate their own data on cold load (see run view).

import { bus } from './js/core/bus.js';
import { state } from './js/core/state.js';
import { loadEnvironment, loadServiceIdentity } from './js/core/api.js';
import { connectSSE } from './js/core/sse.js';

import { parseRoute, href } from './js/variants/F/router.js';
import { buildChrome, updateNavContext, closeDrawer } from './js/variants/F/chrome.js';
import { renderEnvironment } from './js/variants/F/views/environment.js';
import { renderEpoch } from './js/variants/F/views/epoch.js';
import { renderExperiment, resetExperimentCaches } from './js/variants/F/views/experiment.js';
import { renderLifecycle, resetLifecycleCaches } from './js/variants/F/views/lifecycle.js';
import { renderScoring, resetScoringCaches } from './js/variants/F/views/scoring.js';
import { renderStyles, resetStylesCaches } from './js/variants/F/views/styles.js';
import { renderTournament } from './js/variants/F/views/tournament.js';
import { renderRun, resetRunCaches } from './js/variants/F/views/run.js';
import { renderBench } from './js/variants/F/views/bench.js';
import { currentEpochId, liveGenId } from './js/variants/F/model.js';

let _chrome = null;
let _renderQueued = false;
let _lastView = null; // for view-switch host clears + cache invalidation

// The variant stylesheet. Self-injected so a bare `?ui=F` load paints
// correctly. Idempotent.
function ensureStylesheet() {
  if (typeof document.querySelector !== 'function') return; // harness
  const HREF = 'css/variants/F/variant.css';
  if (document.querySelector('link[data-variant-f-css]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = HREF;
  link.setAttribute('data-variant-f-css', '1');
  (document.head || document.documentElement || document.body).appendChild(link);
}

function mountHost() {
  let host = document.getElementById('variant-root');
  if (!host) {
    host = document.createElement('div');
    host.id = 'variant-root';
    document.body.appendChild(host);
  }
  return host;
}

function ensureChrome(host) {
  if (_chrome) return _chrome;
  _chrome = buildChrome();
  while (host.firstChild) host.removeChild(host.firstChild);
  host.classList.add('czF-root', 'cz-root');
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

  // On a VIEW switch: clear the persistent stage host (so a digest-gated
  // view that returns early does not leave the previous screen on screen),
  // and invalidate the drill-down caches (NEVER on a heartbeat).
  if (route.view !== _lastView) {
    while (chrome.stage.firstChild) chrome.stage.removeChild(chrome.stage.firstChild);
    resetExperimentCaches();
    resetLifecycleCaches();
    resetScoringCaches();
    resetStylesCaches();
    resetRunCaches();
    _lastView = route.view;
  }

  chrome.setActive(route.view);
  chrome.setPill(state);

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
    case 'lifecycle': renderLifecycle(ctx); break;
    case 'scoring': renderScoring(ctx); break;
    case 'styles': renderStyles(ctx); break;
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

  // Keep the pill clock honest even when no state arrives — pill-only, no
  // view repaint (the view's own digest gate would no-op anyway).
  setInterval(() => { if (_chrome) _chrome.setPill(state); }, 1000);

  window.addEventListener('hashchange', () => {
    if (_chrome) closeDrawer(_chrome.drawer);
    scheduleRender();
  });

  // First paint before data lands — honest "loading" / empty states.
  render();

  loadEnvironment();
  loadServiceIdentity();
  connectSSE();
}

// Console / test debugging hook, namespaced so it cannot collide with the
// shipped shell or any other variant.
window.__zicatoF = { state, bus, render: scheduleRender, parseRoute };

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
