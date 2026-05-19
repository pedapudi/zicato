// core/router.js — hash routing + deep links.
//
// The dashboard is a multi-view app. The active view + any drill-down
// is encoded in the URL fragment so a reload or a shared link lands on
// the same place. The router resolves the fragment to a structured
// { view, params } and emits `route:changed` on the bus — views
// subscribe and render against it.
//
// Routes (all deep-linkable):
//   #/overview
//   #/tree                       Lineage
//   #/tournament                 ·  #/tournament/{genId}
//   #/epoch                      ·  #/epoch/{epochId}
//   #/files                      ·  #/files/{epochId}/{genId}
//   #/conversation/{entryId}     focused conversation diff

import { bus } from './bus.js';

// Canonical view list. `conversation` is a focused view: it has a
// container the shell toggles, but no nav-rail tab.
export const VIEWS = ['overview', 'tree', 'tournament', 'epoch', 'files', 'conversation'];
export const DEFAULT_VIEW = 'overview';

let _current = { view: DEFAULT_VIEW, params: {}, raw: '' };

// Parse the fragment into { view, params, raw }.
function parse(hash) {
  const segs = (hash || '').replace(/^#\/?/, '').split('/').filter(Boolean);
  if (segs.length === 0 || !VIEWS.includes(segs[0])) {
    return { view: DEFAULT_VIEW, params: {}, raw: hash || '' };
  }
  const view = segs[0];
  const rest = segs.slice(1).map(decodeURIComponent);
  const params = {};
  switch (view) {
    case 'tournament':
      if (rest[0]) params.generationId = rest[0];
      break;
    case 'epoch':
      if (rest[0]) params.epochId = rest[0];
      break;
    case 'files':
      if (rest[0]) params.epochId = rest[0];
      if (rest[1]) params.generationId = rest[1];
      break;
    case 'conversation':
      if (rest[0]) params.entryId = rest[0];
      break;
    default:
      break;
  }
  return { view, params, raw: hash || '' };
}

export const router = {
  // Resolve the current fragment and broadcast it. Called on
  // `hashchange` and once at bootstrap.
  resolve() {
    _current = parse(window.location.hash);
    bus.emit('route:changed', _current);
    return _current;
  },
  current() { return _current; },
  // Programmatic navigation. Setting location.hash fires `hashchange`,
  // which calls resolve() — so go() never double-emits.
  go(hash) {
    const next = hash.startsWith('#') ? hash : '#' + hash;
    if (window.location.hash === next) {
      // Same hash — hashchange won't fire; resolve explicitly.
      this.resolve();
    } else {
      window.location.hash = next;
    }
  },
  // Build a fragment for a view + optional path segments.
  href(view, ...segs) {
    const tail = segs.filter((s) => s != null && s !== '')
      .map((s) => encodeURIComponent(String(s)));
    return '#/' + [view, ...tail].join('/');
  },
  // Wire the router to the window. Idempotent.
  start() {
    window.addEventListener('hashchange', () => this.resolve());
    return this.resolve();
  },
};
