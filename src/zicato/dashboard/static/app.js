// zicato dashboard — entry point.
//
// app.js is the thin orchestrator. The dashboard has two shells:
//
//   * Phase-0 shell (DEFAULT) — the level-aligned redesign that maps
//     directly onto the environment → epoch → generation → round → run
//     hierarchy. Five new view modules under ``js/views/phase0_*.js``,
//     a fixed sidebar (Live Activity card + Files + Search), and a
//     breadcrumb as the primary navigation.
//
//   * Legacy 5-tab shell — Overview, Tree, Tournament, Epoch, Files.
//     Selectable with ``?legacy=1`` for the duration of the phase-0
//     rollout. Mounted exactly as before so a fresh page-load with
//     ``?legacy=1`` walks the identical code path the dashboard has
//     used since the modular boundary landed.
//
// The shell pick is resolved BEFORE any view module runs so each
// shell's containers are wired only when its DOM is visible. The bus
// is the spine: a state mutation or a route change drives a render.

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

import { parsePhase0Hash } from './js/views/phase0_router.js';
import {
  renderBreadcrumb, showPhase0View, renderSidebarLive,
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
let _shell = 'phase0'; // resolved in init()

function scheduleRender() {
  if (_renderQueued) return;
  _renderQueued = true;
  queueMicrotask(() => {
    _renderQueued = false;
    if (_shell === 'legacy') {
      renderAll();
    } else {
      renderPhase0All();
    }
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

// Persisted shell-pick lives in js/core/shell.js so test modules can
// import it without triggering app.js's init() side effects.
import { resolveShell, setShellPreference } from './js/core/shell.js';

function mountShell(shell) {
  const legacy = document.getElementById('legacy-shell');
  const phase0 = document.getElementById('phase0-shell');
  if (shell === 'legacy') {
    if (legacy) legacy.classList.remove('hidden');
    if (phase0) phase0.classList.add('hidden');
  } else {
    if (legacy) legacy.classList.add('hidden');
    if (phase0) phase0.classList.remove('hidden');
  }
}

function _wireShellToggles() {
  // Phase-0 sidebar link: "Use legacy UI →" — persists the choice and
  // reloads. We intercept the click and call setShellPreference so the
  // bare ``?legacy=1`` href degrades gracefully when JS is off.
  const legacyLink = document.getElementById('phase0-nav-legacy');
  if (legacyLink) {
    legacyLink.addEventListener('click', (ev) => {
      ev.preventDefault();
      setShellPreference('legacy');
    });
  }
  // Legacy shell footer link: "Use new UI →" — symmetric toggle back to
  // phase-0. Wired only when the legacy shell is mounted (the element
  // is added below in mountShell when shell === 'legacy').
  const newLink = document.getElementById('legacy-nav-phase0');
  if (newLink) {
    newLink.addEventListener('click', (ev) => {
      ev.preventDefault();
      setShellPreference('phase0');
    });
  }
}

function init() {
  _shell = resolveShell();
  mountShell(_shell);
  _wireShellToggles();

  // The drill panel close affordance — used by the legacy shell. Harmless
  // when the phase-0 shell is active (the panel is shared chrome).
  const drillClose = document.getElementById('drill-close');
  if (drillClose) drillClose.addEventListener('click', closeDrill);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') closeDrill();
  });

  bus.on('state:changed', scheduleRender);
  bus.on('route:changed', () => {
    if (_shell === 'legacy') applyRoute();
    else scheduleRender();
  });
  bus.on('log:appended', () => {
    if (_shell === 'legacy') appendLogTail();
    else scheduleRender();
  });

  if (_shell === 'legacy') setupLineageInteractions();

  // Tick the header's elapsed clock once per second so it reads "live"
  // even when no state changes arrive. The renderHeader function is
  // cheap and idempotent in both shells.
  setInterval(() => { renderHeader(); }, 1000);

  // Mock mode must be resolved BEFORE router.start(): it emits
  // `route:changed` -> applyRoute(), which may kick off a route-driven
  // load (e.g. the conversation diff for a `#/tournament/conv/{entry}`
  // deep-link). With `state.mock` still false the load would hit the
  // real (empty) endpoint and cache its degraded payload over the mock.
  const params = new URLSearchParams(window.location.search);
  const mock = params.get('mock') === '1';
  if (mock) {
    state.mock = true;
    state.connected = false;
    state.connecting = false;
    state.applySnapshot(mockSnapshot());
  }

  // Resolve the initial route before the first paint. The legacy router
  // emits `route:changed` -> applyRoute; the phase-0 shell just installs
  // its own hashchange listener so a fragment update repaints.
  router.start();
  if (_shell === 'phase0') {
    window.addEventListener('hashchange', () => scheduleRender());
  }

  if (mock) {
    if (_shell === 'legacy') renderAll(); else renderPhase0All();
    return;
  }

  if (_shell === 'legacy') renderAll(); else renderPhase0All();
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
