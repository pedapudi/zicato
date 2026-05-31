// js/v2/router.js — the v2 hash router.
//
// v2's information architecture is TWO MODES unified by the spine
// (DASHBOARD-V2 §4): the Bench (live) and the Notebook (post-hoc). The
// router resolves the URL fragment into a structured { view, params,
// raw } and emits `v2:route` on the shared bus — the shell subscribes
// and renders the matching view container.
//
// We deliberately do NOT reuse core/router.js's emit channel: v1's
// router owns `route:changed`, and both apps may be parsed during the
// feature-flag handoff. v2 emits its own `v2:route` topic so the two
// never cross-drive each other.
//
// Routes (all deep-linkable):
//   #/v2                         → overview     (default landing)
//   #/v2/overview                → overview     — is it progressing & healthy?
//   #/v2/bench                   → bench        — the LIVE operations view
//   #/v2/epoch                   → epoch        — what are we learning?
//   #/v2/epoch/{epochId}         ·
//   #/v2/experiment/{genId}      → experiment   — was the bet right & why?
//   #/v2/run/{entryId}           → run          — what actually happened?
//        #/v2/run/{entryId}/{genId}  (optional generation context)
//   #/v2/report                  → report       — the standalone ACM report
//        #/v2/report/{epochId}
//
// Every v2 fragment is prefixed `#/v2` so a v1 fragment (`#/overview`,
// `#/epoch/...`) and a v2 fragment never collide while both entries can
// theoretically be loaded during the flag handoff.

import { bus } from '../core/bus.js';

export const V2_PREFIX = 'v2';
export const V2_VIEWS = ['overview', 'tournament', 'bench', 'epoch', 'experiment', 'run', 'report'];
export const V2_DEFAULT_VIEW = 'overview';

// Which mode a view belongs to — the shell shows a Bench/Notebook
// indicator from this. `bench` is the live mode; everything else is the
// post-hoc Notebook.
export const V2_MODE = {
  overview: 'notebook',
  tournament: 'notebook',
  bench: 'bench',
  epoch: 'notebook',
  experiment: 'notebook',
  run: 'notebook',
  report: 'notebook',
};

let _current = { view: V2_DEFAULT_VIEW, params: {}, raw: '' };

// Parse a fragment into { view, params, raw }. Tolerant: an unknown or
// absent view resolves to the default (overview) rather than throwing.
export function parseV2Hash(hash) {
  const raw = hash || '';
  const segs = raw.replace(/^#\/?/, '').split('/').filter(Boolean);
  // Strip the leading `v2` prefix when present so the parser works the
  // same whether called with `#/v2/epoch/e0` or a pre-stripped path.
  if (segs[0] === V2_PREFIX) segs.shift();

  if (segs.length === 0 || !V2_VIEWS.includes(segs[0])) {
    return { view: V2_DEFAULT_VIEW, params: {}, raw };
  }
  const view = segs[0];
  const rest = segs.slice(1).map(safeDecode);
  const params = {};
  switch (view) {
    case 'epoch':
      if (rest[0]) params.epochId = rest[0];
      break;
    case 'experiment':
      if (rest[0]) params.generationId = rest[0];
      break;
    case 'run':
      if (rest[0]) params.entryId = rest[0];
      if (rest[1]) params.generationId = rest[1];
      break;
    case 'report':
      if (rest[0]) params.epochId = rest[0];
      break;
    default:
      break;
  }
  return { view, params, raw };
}

function safeDecode(s) {
  try { return decodeURIComponent(s); }
  catch { return s; }
}

export const v2Router = {
  // Resolve the current fragment and broadcast it.
  resolve() {
    _current = parseV2Hash(window.location.hash);
    bus.emit('v2:route', _current);
    return _current;
  },
  current() { return _current; },
  // Programmatic navigation. We set the hash (so the URL is correct +
  // deep-linkable) AND call resolve() unconditionally. resolve() is
  // idempotent — it re-parses the (now-updated) hash and re-emits — so
  // navigation NEVER depends on the browser actually firing `hashchange`.
  // This is what makes the brand / breadcrumb a dependable home even on a
  // same-hash click (where no hashchange fires) and in any environment
  // whose hash assignment is silent. The hashchange listener may also
  // fire and resolve again; the render layer is debounced + digest-gated,
  // so a redundant resolve is a no-op paint.
  go(view, ...segs) {
    const next = v2Href(view, ...segs);
    if (window.location.hash !== next) window.location.hash = next;
    this.resolve();
  },
  // The mode (`bench` | `notebook`) for the current view.
  mode() { return V2_MODE[_current.view] || 'notebook'; },
  // Wire to the window. Idempotent enough for a single bootstrap.
  start() {
    window.addEventListener('hashchange', () => this.resolve());
    return this.resolve();
  },
};

// Human label for a view (the breadcrumb + chrome use these).
export const V2_VIEW_LABELS = {
  overview: 'Overview',
  tournament: 'Tournament',
  bench: 'Bench',
  epoch: 'Epoch',
  experiment: 'Experiment',
  run: 'Run',
  report: 'Report',
};

// ---------------------------------------------------------------------------
// Breadcrumb / level map (DASHBOARD-V2 §4). The operator must always see
// WHERE they are in the spine. From a parsed route, build the ancestor
// trail down the lineage hierarchy:
//
//   Overview › Epoch › Experiment › Run
//
// Bench is the live sibling of the Notebook (not an ancestor of a run),
// so it trails as `Overview › Bench`. Report trails off its Epoch.
// Each crumb is { view, label, href, current } — `current` marks the
// active (non-link) leaf; ancestors are links back up.
// ---------------------------------------------------------------------------
export function crumbTrail(route) {
  const r = route || _current;
  const view = V2_VIEWS.includes(r.view) ? r.view : V2_DEFAULT_VIEW;
  const p = r.params || {};
  const crumb = (v, label, current, ...segs) => ({
    view: v,
    label: label || V2_VIEW_LABELS[v] || v,
    href: v2Href(v, ...segs),
    // The raw segments (sans empties) so a renderer can drive the router
    // directly — a same-hash crumb click must still re-resolve, which a
    // plain href cannot do (no hashchange fires).
    segs: segs.filter((s) => s != null && s !== ''),
    current: !!current,
  });

  // Overview is the root of every trail.
  const trail = [crumb('overview', 'Overview', view === 'overview')];
  if (view === 'overview') return trail;

  switch (view) {
    case 'bench':
      trail.push(crumb('bench', 'Bench', true));
      break;
    case 'epoch':
      trail.push(crumb('epoch', p.epochId ? `Epoch ${p.epochId}` : 'Epoch', true,
        p.epochId));
      break;
    case 'report':
      if (p.epochId) trail.push(crumb('epoch', `Epoch ${p.epochId}`, false, p.epochId));
      trail.push(crumb('report', 'Report', true, p.epochId));
      break;
    case 'experiment':
      trail.push(crumb('experiment',
        p.generationId ? `Experiment ${p.generationId}` : 'Experiment', true,
        p.generationId));
      break;
    case 'run':
      if (p.generationId) {
        trail.push(crumb('experiment', `Experiment ${p.generationId}`, false,
          p.generationId));
      }
      trail.push(crumb('run', p.entryId ? `Run ${p.entryId}` : 'Run', true,
        p.entryId, p.generationId));
      break;
    default:
      break;
  }
  return trail;
}

// Build a v2 fragment for a view + optional path segments.
export function v2Href(view, ...segs) {
  const v = V2_VIEWS.includes(view) ? view : V2_DEFAULT_VIEW;
  const tail = segs
    .filter((s) => s != null && s !== '')
    .map((s) => encodeURIComponent(String(s)));
  return '#/' + [V2_PREFIX, v, ...tail].join('/');
}
