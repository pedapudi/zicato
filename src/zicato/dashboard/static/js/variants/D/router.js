// variants/D/router.js — a minimal hash router scoped to `#/D/...`.
//
// Routes (all prefixed `#/D/` so Variant D coexists with the shell and
// the other variant explorations under one document):
//
//   #/D/                                  → Environment (cross-epoch)
//   #/D/epoch                             → current Epoch
//   #/D/experiment/<gen>                  → Experiment (one generation)
//   #/D/tournament                        → Tournament / lineage
//   #/D/run                               → Run
//   #/D/bench                             → Bench
//
// parseRoute() turns the location hash into a `{ view, params }` record;
// it tolerates a missing / foreign hash (returns the Environment route)
// so a deep-link or a stale hash never lands on a blank screen.

export const VIEWS = ['environment', 'epoch', 'experiment', 'tournament', 'run', 'bench'];

export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  // Only claim hashes under our prefix; anything else is the default.
  if (!raw.startsWith('/D')) return { view: 'environment', params: {} };
  const parts = raw.replace(/^\/D\/?/, '').split('/').filter(Boolean);
  const head = parts[0] || 'environment';
  switch (head) {
    case 'epoch': return { view: 'epoch', params: {} };
    case 'experiment': return { view: 'experiment', params: { gen: dec(parts[1]) } };
    case 'tournament': return { view: 'tournament', params: {} };
    case 'run': return { view: 'run', params: { entry: dec(parts[1]) } };
    case 'bench': return { view: 'bench', params: {} };
    case 'environment': case '': return { view: 'environment', params: {} };
    default: return { view: 'environment', params: {} };
  }
}

export function href(view, params) {
  const p = params || {};
  switch (view) {
    case 'epoch': return '#/D/epoch';
    case 'experiment': return `#/D/experiment/${enc(p.gen)}`;
    case 'tournament': return '#/D/tournament';
    case 'run': return p.entry ? `#/D/run/${enc(p.entry)}` : '#/D/run';
    case 'bench': return '#/D/bench';
    default: return '#/D/';
  }
}

export function navigate(view, params) {
  const target = href(view, params);
  if (location.hash === target) {
    // Force a re-dispatch even when the hash is unchanged.
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    location.hash = target;
  }
}

function enc(s) { return encodeURIComponent(s == null ? '' : String(s)); }
function dec(s) { try { return s == null ? null : decodeURIComponent(s); } catch { return s || null; } }
