// variants/G/shell.js — the Bridge command-center shell.
//
// Keeps Variant A's shell STRUCTURE (top strip: brand · breadcrumb ·
// live status pill · ⌘K · bench; one persistent content host every view
// paints into) but rebuilds it with the render discipline the v2 shell
// proved out, so A's flashing / jerky-hover bugs cannot occur:
//
//   * ONE persistent content host, created once, NEVER recreated on a
//     repaint (recreating it was the A flashing bug).
//   * On a VIEW switch, the host is cleared before the new view paints
//     (so a digest-gated view cannot wrongly skip its first paint).
//   * `state:changed` is subscribed once and COALESCED into one repaint
//     per frame; each view's render is DIGEST-GATED, so a heartbeat tick
//     that only re-stamps a timestamp writes zero DOM.
//   * The chrome (breadcrumb, status pill, clock) is patched in place,
//     not rebuilt — and the clock ticks via its own text patch, never a
//     view repaint.

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
let lastViewName = null;
let repaintScheduled = false;

function statusInfo() {
  const hb = state.heartbeat || {};
  const at = state.activeTournament;
  const phase = String(hb.phase || (at ? 'running' : 'idle')).toLowerCase();
  let pulse = 'idle', label = phase.toUpperCase() || 'IDLE';
  if (at || phase === 'running' || phase.includes('tournament')) { pulse = 'live'; label = at ? 'TOURNAMENT' : 'RUNNING'; }
  else if (phase === 'proposing' || phase === 'applying' || phase === 'journaling') { pulse = 'live'; }
  else if (phase === 'paused') { pulse = 'caution'; }
  else if (phase === 'stalled') { pulse = 'regress'; }
  else if (phase === 'idle' || phase === '') { pulse = 'idle'; label = 'IDLE'; }
  else { pulse = 'go'; }
  return { pulse, label };
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
  if (crumbsHost) {
    crumbsHost.textContent = '';
    crumbsFor(currentRoute).forEach((c, i) => {
      if (i > 0) crumbsHost.appendChild(el('span', { class: 'g-crumb-sep', 'aria-hidden': 'true' }, ['›']));
      if (c.current || !c.href) {
        crumbsHost.appendChild(el('span', { class: 'g-crumb', 'aria-current': 'page' }, [c.label]));
      } else {
        crumbsHost.appendChild(el('a', { class: 'g-crumb', href: c.href }, [c.label]));
      }
    });
  }
  if (statusHost) {
    const { pulse, label } = statusInfo();
    statusHost.setAttribute('data-pulse', pulse);
    const hb = state.heartbeat || {};
    const clock = elapsedClock();
    statusHost.textContent = '';
    statusHost.appendChild(el('span', { class: 'g-status-light' }));
    statusHost.appendChild(el('span', { class: 'g-status-phase' }, [label]));
    if (hb.epoch_id) statusHost.appendChild(el('span', { class: 'g-status-meta g-mono' }, ['· ' + hb.epoch_id]));
    if (clock) statusHost.appendChild(el('span', { class: 'g-status-clock g-mono' }, ['· ' + clock]));
    if (!state.connected) statusHost.appendChild(el('span', { class: 'g-status-meta is-caution' }, ['· reconnecting']));
  }
}

const VIEWS = {
  environment: (host, route, repaint) => renderEnvironment(host, route.params, repaint),
  epoch: (host, route, repaint) => renderEpoch(host, route.params, repaint),
  experiment: (host, route, repaint) => renderExperiment(host, route.params, repaint),
  tournament: (host, route, repaint) => renderTournament(host, route.params, repaint),
  run: (host, route, repaint) => renderRun(host, route.params, repaint),
  bench: (host, route, repaint) => renderBench(host, route.params, repaint),
};

function repaintView() {
  if (!contentHost) return;
  // On a VIEW switch, clear the persistent host first so the incoming
  // (digest-gated) view always paints its first frame. Within a view the
  // host is NEVER recreated — the view patches/diffs into it.
  if (currentRoute.name !== lastViewName) {
    contentHost.textContent = '';
    lastViewName = currentRoute.name;
  }
  const fn = VIEWS[currentRoute.name] || VIEWS.environment;
  try { fn(contentHost, currentRoute, scheduleRepaint); }
  catch (err) {
    contentHost.textContent = '';
    contentHost.appendChild(el('div', { class: 'g-empty' }, ['Render error: ' + (err && err.message)]));
    if (globalThis.console) console.error('[G] view error', err);
  }
}

// Coalesce repaints so an SSE burst + async fetch resolve into one paint.
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
  root.classList.add('mcG');
  root.textContent = '';

  const brand = el('div', { class: 'g-brand' }, [
    el('span', { class: 'g-brand-mark' }),
    el('div', null, [
      el('div', { class: 'g-brand-name' }, ['ZICATO']),
      el('div', { class: 'g-brand-sub' }, ['bridge']),
    ]),
  ]);
  brand.addEventListener('click', () => { window.location.hash = href('environment'); });

  crumbsHost = el('nav', { class: 'g-crumbs', 'aria-label': 'breadcrumb' });
  statusHost = el('div', { class: 'g-status', dataset: { pulse: 'idle' } });

  const kbtn = el('button', { class: 'g-navbtn', type: 'button', 'aria-label': 'command palette' }, [
    'jump', el('kbd', null, ['⌘K']),
  ]);
  kbtn.addEventListener('click', () => openPalette());

  const benchBtn = el('button', { class: 'g-navbtn', type: 'button' }, ['bench']);
  benchBtn.addEventListener('click', () => { window.location.hash = href('bench'); });

  const top = el('header', { class: 'g-top', role: 'banner' }, [brand, crumbsHost, statusHost, benchBtn, kbtn]);

  contentHost = el('main', { class: 'g-main', id: 'mcG-content', role: 'main' });

  const foot = el('footer', { class: 'g-foot' }, [
    el('span', null, ['Variant G · Bridge']),
    el('span', null, ['command center']),
  ]);

  root.appendChild(top);
  root.appendChild(contentHost);
  root.appendChild(foot);

  mountPalette(root);

  // ONE subscription; coalesced + digest-gated repaint.
  bus.on('state:changed', scheduleRepaint);

  // The clock ticks via a chrome-only paint — never a view repaint, so a
  // tick cannot rebuild a view (and thus cannot flash a panel).
  if (typeof setInterval === 'function') setInterval(() => { paintChrome(); }, 1000);

  startRouter(onRoute);
}
