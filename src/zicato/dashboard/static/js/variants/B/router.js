// variants/B/router.js — Variant B's hash router.
//
// Variant B is one of four parallel dashboard explorations the operator
// chooses between; it is reached via `?ui=B` and mounts into
// `#variant-root`. Its routes are prefixed `#/B/...` so they never collide
// with v1 (`#/...`) or v2 (`#/v2/...`).
//
// The IA follows the editorial "lab notebook" framing:
//   #/B                      → environment  (the home — the whole workspace
//                              across epochs, as a story)
//   #/B/environment          → environment
//   #/B/epoch/{epochId}       → epoch        (the chapter)
//   #/B/experiment/{genId}    → experiment   (the notebook entry)
//   #/B/tournament[/{genId}]  → tournament   (the lineage slopegraph)
//   #/B/run/{entryId}[/{genId}] → run        (the transcript)
//   #/B/bench                → bench        (live ops, reachable from nav)
//
// The router emits `B:route` on the shared bus; the shell renders the
// matching view. go() always re-resolves (idempotent) so a same-hash click
// is a dependable re-render even when the browser fires no `hashchange`.

import { bus } from '../../core/bus.js';

export const B_PREFIX = 'B';
export const B_VIEWS = ['environment', 'epoch', 'experiment', 'tournament', 'run', 'bench'];
export const B_DEFAULT = 'environment';

export const B_VIEW_LABELS = {
  environment: 'Environment',
  epoch: 'Epoch',
  experiment: 'Experiment',
  tournament: 'Lineage',
  run: 'Run',
  bench: 'Bench',
};

let _current = { view: B_DEFAULT, params: {}, raw: '' };

function safeDecode(s) { try { return decodeURIComponent(s); } catch { return s; } }

export function parseBHash(hash) {
  const raw = hash || '';
  const segs = raw.replace(/^#\/?/, '').split('/').filter(Boolean);
  if (segs[0] === B_PREFIX) segs.shift();
  if (segs.length === 0 || !B_VIEWS.includes(segs[0])) {
    return { view: B_DEFAULT, params: {}, raw };
  }
  const view = segs[0];
  const rest = segs.slice(1).map(safeDecode);
  const params = {};
  switch (view) {
    case 'epoch': if (rest[0]) params.epochId = rest[0]; break;
    case 'experiment': if (rest[0]) params.generationId = rest[0]; break;
    case 'tournament': if (rest[0]) params.generationId = rest[0]; break;
    case 'run':
      if (rest[0]) params.entryId = rest[0];
      if (rest[1]) params.generationId = rest[1];
      break;
    default: break;
  }
  return { view, params, raw };
}

export function bHref(view, ...segs) {
  const v = B_VIEWS.includes(view) ? view : B_DEFAULT;
  const tail = segs.filter((s) => s != null && s !== '').map((s) => encodeURIComponent(String(s)));
  return '#/' + [B_PREFIX, v, ...tail].join('/');
}

export const bRouter = {
  resolve() {
    _current = parseBHash(window.location.hash);
    bus.emit('B:route', _current);
    return _current;
  },
  current() { return _current; },
  go(view, ...segs) {
    const next = bHref(view, ...segs);
    if (window.location.hash !== next) window.location.hash = next;
    this.resolve();
  },
  start() {
    window.addEventListener('hashchange', () => this.resolve());
    return this.resolve();
  },
};

// The ancestor crumb trail for the masthead: Environment › Epoch ›
// Experiment › Run. Each crumb is { view, label, segs, current }.
export function crumbTrail(route) {
  const r = route || _current;
  const view = B_VIEWS.includes(r.view) ? r.view : B_DEFAULT;
  const p = r.params || {};
  const crumb = (v, label, current, ...segs) => ({
    view: v, label, segs: segs.filter((s) => s != null && s !== ''), current: !!current,
  });
  const trail = [crumb('environment', 'Environment', view === 'environment')];
  if (view === 'environment') return trail;
  switch (view) {
    case 'bench':
      trail.push(crumb('bench', 'Bench', true));
      break;
    case 'tournament':
      trail.push(crumb('tournament', 'Lineage', true, p.generationId));
      break;
    case 'epoch':
      trail.push(crumb('epoch', p.epochId ? `Epoch ${p.epochId}` : 'Epoch', true, p.epochId));
      break;
    case 'experiment':
      trail.push(crumb('experiment',
        p.generationId ? `Experiment ${p.generationId}` : 'Experiment', true, p.generationId));
      break;
    case 'run':
      if (p.generationId) {
        trail.push(crumb('experiment', `Experiment ${p.generationId}`, false, p.generationId));
      }
      trail.push(crumb('run', p.entryId ? `Run ${p.entryId}` : 'Run', true, p.entryId, p.generationId));
      break;
    default: break;
  }
  return trail;
}
