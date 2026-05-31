// variants/A/shell.js — the Mission Control shell.
//
// Builds the persistent chrome (top strip: brand · breadcrumb · live
// status pill · ⌘K · nav) ONCE, and owns a single content host that
// every view paints into. The router hands the shell a parsed route;
// the shell repaints the chrome (cheap, in place) and dispatches to the
// matching view's render — into the SAME `#mcA-content` host. The host
// is never recreated, so navigation cannot orphan listeners or blank
// the page (the fresh-host bug the brief warns about).
//
// Re-render under SSE: the shell subscribes to `state:changed` and
// re-runs the active view. Views build fresh nodes but the content host
// swap is a single textContent='' + append, which the browser paints in
// one frame — no flashing of unchanged chrome (chrome is patched in
// place, not rebuilt).

import { el } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { bus } from '../../core/bus.js';
import { parseRoute, crumbsFor, href, startRouter } from './router.js';
import { mountPalette, openPalette } from './components/palette.js';

import { renderEnvironment } from './views/environment.js';
import { renderEpoch } from './views/epoch.js';
import { renderExperiment } from './views/experiment.js';
import { renderTournament } from './views/tournament.js';
import { renderRun } from './views/run.js';
import { renderBench } from './views/bench.js';

let contentHost = null;
let crumbsHost = null;
let statusHost = null;
let currentRoute = { name: 'environment', params: {} };
let repaintScheduled = false;

// status pill mapping from heartbeat phase + active tournament.
function statusInfo() {
  const hb = state.heartbeat || {};
  const at = state.activeTournament;
  const phase = String(hb.phase || (at ? 'running' : 'idle')).toLowerCase();
  let pulse = 'idle', label = phase.toUpperCase() || 'IDLE';
  if (at || phase === 'running' || phase.includes('tournament')) { pulse = 'live'; label = at ? 'TOURNAMENT' : 'RUNNING'; }
  else if (phase === 'proposing' || phase === 'applying' || phase === 'journaling') { pulse = 'live'; }
  else if (phase === 'paused') { pulse = 'warn'; }
  else if (phase === 'stalled') { pulse = 'stop'; }
  else if (phase === 'idle' || phase === '') { pulse = 'idle'; label = 'IDLE'; }
  else { pulse = 'go'; }
  return { pulse, label, phase };
}

function elapsedClock() {
  const hb = state.heartbeat || {};
  const startRaw = hb.evolve_started_at || hb.started_at;
  if (!startRaw) return '';
  const start = Date.parse(String(startRaw).replace(' ', 'T'));
  if (!isFinite(start)) return '';
  let s = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const pad = (n) => String(n).padStart(2, '0');
  return (h ? pad(h) + ':' : '') + pad(m) + ':' + pad(s);
}

function paintChrome() {
  // breadcrumb
  if (crumbsHost) {
    crumbsHost.textContent = '';
    const crumbs = crumbsFor(currentRoute);
    crumbs.forEach((c, i) => {
      if (i > 0) crumbsHost.appendChild(el('span', { class: 'mcA-crumb-sep' }, ['›']));
      if (c.current || !c.href) {
        crumbsHost.appendChild(el('span', { class: 'mcA-crumb', 'aria-current': 'page' }, [c.label]));
      } else {
        crumbsHost.appendChild(el('a', { class: 'mcA-crumb', href: c.href }, [c.label]));
      }
    });
  }
  // status pill
  if (statusHost) {
    const { pulse, label } = statusInfo();
    statusHost.setAttribute('data-pulse', pulse);
    const hb = state.heartbeat || {};
    const clock = elapsedClock();
    statusHost.textContent = '';
    statusHost.appendChild(el('span', { class: 'mcA-status-light' }));
    statusHost.appendChild(el('span', { class: 'mcA-status-phase' }, [label]));
    if (hb.epoch_id) statusHost.appendChild(el('span', { class: 'mcA-status-meta' }, ['· ' + hb.epoch_id]));
    if (clock) statusHost.appendChild(el('span', { class: 'mcA-status-clock' }, ['· ' + clock]));
    if (!state.connected) statusHost.appendChild(el('span', { class: 'mcA-status-meta', style: 'color:var(--mc-warn)' }, ['· reconnecting']));
  }
}

const VIEWS = {
  environment: (host, route, repaint) => renderEnvironment(host, repaint),
  epoch: (host, route, repaint) => renderEpoch(host, route.params, repaint),
  experiment: (host, route, repaint) => renderExperiment(host, route.params, repaint),
  tournament: (host, route, repaint) => renderTournament(host, route.params, repaint),
  run: (host, route, repaint) => renderRun(host, route.params, repaint),
  bench: (host, route, repaint) => renderBench(host, route.params, repaint),
};

function repaintView() {
  if (!contentHost) return;
  const fn = VIEWS[currentRoute.name] || VIEWS.environment;
  try { fn(contentHost, currentRoute, scheduleRepaint); }
  catch (err) {
    contentHost.textContent = '';
    contentHost.appendChild(el('div', { class: 'mcA-empty' }, ['Render error: ' + (err && err.message)]));
    if (globalThis.console) console.error('[mcA] view error', err);
  }
}

// Coalesce repaints so an SSE burst + async fetch resolves into one
// paint per frame.
function scheduleRepaint() {
  if (repaintScheduled) return;
  repaintScheduled = true;
  const run = () => { repaintScheduled = false; paintChrome(); repaintView(); };
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run);
  else setTimeout(run, 16);
}

function onRoute(route) {
  currentRoute = route;
  paintChrome();
  repaintView();
}

export function mountShell(root) {
  root.classList.add('mcA');
  root.textContent = '';

  // ---- top strip ----
  const brand = el('div', { class: 'mcA-brand' }, [
    el('span', { class: 'mcA-brand-mark' }),
    el('div', null, [
      el('div', { class: 'mcA-brand-name' }, ['ZICATO']),
      el('div', { class: 'mcA-brand-sub' }, ['mission control']),
    ]),
  ]);
  brand.addEventListener('click', () => { window.location.hash = href('environment'); });

  crumbsHost = el('nav', { class: 'mcA-crumbs', 'aria-label': 'breadcrumb' });
  statusHost = el('div', { class: 'mcA-status', dataset: { pulse: 'idle' } });

  const kbtn = el('button', { class: 'mcA-navbtn', type: 'button', 'aria-label': 'command palette' }, [
    'jump', el('kbd', null, ['⌘K']),
  ]);
  kbtn.addEventListener('click', () => openPalette());

  const benchBtn = el('button', { class: 'mcA-navbtn', type: 'button' }, ['bench']);
  benchBtn.addEventListener('click', () => { window.location.hash = href('bench'); });

  const top = el('header', { class: 'mcA-top', role: 'banner' }, [
    brand, crumbsHost, statusHost, benchBtn, kbtn,
  ]);

  // ---- persistent content host ----
  contentHost = el('main', { class: 'mcA-main', id: 'mcA-content', role: 'main' });

  const foot = el('footer', { class: 'mcA-foot' }, [
    el('span', null, ['Variant A · Mission Control']),
    el('span', null, ['live ops console']),
  ]);

  root.appendChild(top);
  root.appendChild(contentHost);
  root.appendChild(foot);

  mountPalette(root);

  // ---- wire SSE-driven re-render ----
  bus.on('state:changed', scheduleRepaint);

  // a slow clock so the elapsed timer ticks even without SSE frames.
  setInterval(() => { paintChrome(); }, 1000);

  // ---- start routing ----
  startRouter(onRoute);
}
